import asyncio
import json
from typing import Any
from uuid import UUID

import httpx

from sidera_task_bridge.client import (
    CommandDispatcher,
    CommandEnvelope,
    CommandIntent,
    ControlPlaneClient,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.state import BridgeStateStore

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value


def config() -> TaskBridgeConfig:
    return TaskBridgeConfig.model_validate(
        {
            "workspace_id": WORKSPACE_ID,
            "planning_profile_id": PROFILE_ID,
            "planning_profile_version": 1,
        }
    )


def envelope(
    command_id: str = "00000000-0000-0000-0000-000000000010",
    *,
    correlation_id: str = "00000000-0000-0000-0000-000000000011",
    payload: str = "first",
    idempotency_key: str | None = None,
) -> CommandEnvelope:
    command_uuid = UUID(command_id)
    return CommandEnvelope(
        command_id=command_uuid,
        command_type="SubmitPlanningRequest",
        target_type="workspace",
        target_id=None,
        expected_version=None,
        idempotency_key=idempotency_key
        or f"sidera:SubmitPlanningRequest:{command_uuid}",
        correlation_id=UUID(correlation_id),
        payload={"objective": payload},
    )


def intent(payload: str = "first") -> CommandIntent:
    return CommandIntent(
        command_type="SubmitPlanningRequest",
        target_type="workspace",
        target_id=None,
        expected_version=None,
        payload={"objective": payload},
    )


def applied(command_id: UUID) -> dict[str, Any]:
    return {
        "command_id": str(command_id),
        "aggregate_id": str(UUID("00000000-0000-0000-0000-000000000020")),
        "aggregate_version": 1,
        "event_ids": [str(UUID("00000000-0000-0000-0000-000000000021"))],
        "data": {"planning_request_id": str(UUID("00000000-0000-0000-0000-000000000022"))},
    }


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_dispatch_applied_persists_actual_body_then_settles() -> None:
    async def scenario() -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(201, json=applied(envelope().command_id))

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        store = BridgeStateStore(MemoryKV())
        result = await CommandDispatcher(client, store).execute(
            "planning:request-1", envelope()
        )
        await client.close()

        assert result.status == "applied"
        assert seen[0]["workspace_id"] == str(WORKSPACE_ID)
        assert seen[0]["actor"] == {
            "type": "integration",
            "id": "akashic-task-bridge",
        }
        assert (await store.snapshot()).unresolved_commands == {}

    run(scenario())


def test_dispatch_rejected_settles_safe_error() -> None:
    async def scenario() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={"error": {"code": "planning_conflict", "details": {}}},
            )

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        store = BridgeStateStore(MemoryKV())
        result = await CommandDispatcher(client, store).execute(
            "planning:request-1", envelope()
        )
        await client.close()

        assert result.status == "rejected"
        assert result.error_code == "planning_conflict"
        state = await store.snapshot()
        assert state.unresolved_commands == {}
        assert state.settled_commands[-1].outcome == "rejected"

    run(scenario())


def test_unknown_post_queries_terminal_status_before_settling() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        item = envelope()

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "POST":
                raise httpx.ReadTimeout("wire detail", request=request)
            return httpx.Response(
                200,
                json={
                    "command_id": str(item.command_id),
                    "status": "applied",
                    "result": applied(item.command_id),
                },
            )

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        store = BridgeStateStore(MemoryKV())
        result = await CommandDispatcher(client, store).execute(
            "planning:request-1", item
        )
        await client.close()

        assert result.status == "applied"
        assert calls == ["POST", "GET"]
        assert (await store.snapshot()).unresolved_commands == {}

    run(scenario())


def test_unknown_submit_recovery_persists_private_source_binding() -> None:
    async def scenario() -> None:
        item = envelope()
        item = item.model_copy(
            update={
                "payload": {
                    "objective": "first",
                    "source_type": "akashic_private",
                    "source_ref": "src_private",
                }
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                raise httpx.ReadTimeout("wire detail", request=request)
            return httpx.Response(
                200,
                json={
                    "command_id": str(item.command_id),
                    "status": "applied",
                    "result": {
                        **applied(item.command_id),
                        "data": {
                            "planning_request_id": str(
                                UUID("00000000-0000-0000-0000-000000000022")
                            )
                        },
                    },
                },
            )

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        store = BridgeStateStore(MemoryKV())
        result = await CommandDispatcher(client, store).execute(
            "planning:source:src_private", item
        )
        await client.close()

        assert result.status == "applied"
        binding = (await store.snapshot()).resource_sources[
            "planning_request:00000000-0000-0000-0000-000000000022"
        ]
        assert binding.source_type == "akashic_private"
        assert binding.source_ref == "src_private"

    run(scenario())


def test_pending_result_keeps_exact_body_and_reuses_it_on_next_dispatch() -> None:
    async def scenario() -> None:
        bodies: list[dict[str, Any]] = []
        item = envelope()
        post_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal post_count
            if request.method == "POST":
                post_count += 1
                bodies.append(json.loads(request.content))
                if post_count == 1:
                    raise httpx.ReadTimeout("wire detail", request=request)
                return httpx.Response(201, json=applied(item.command_id))
            return httpx.Response(
                200,
                json={"command_id": str(item.command_id), "status": "pending"},
            )

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        store = BridgeStateStore(MemoryKV())
        dispatcher = CommandDispatcher(client, store)
        first = await dispatcher.execute("planning:request-1", item)
        second = await dispatcher.execute("planning:request-1", item)
        await client.close()

        assert first.status == "result_unknown"
        assert second.status == "applied"
        assert bodies[0] == bodies[1]
        assert (await store.snapshot()).unresolved_commands == {}

    run(scenario())


def test_same_logical_request_recovers_existing_command_without_new_post_body() -> None:
    async def scenario() -> None:
        bodies: list[dict[str, Any]] = []
        first = envelope()
        second = envelope(
            command_id="00000000-0000-0000-0000-000000000030",
            correlation_id="00000000-0000-0000-0000-000000000031",
            idempotency_key="sidera:SubmitPlanningRequest:000000000030",
        )
        post_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal post_count
            if request.method == "POST":
                post_count += 1
                bodies.append(json.loads(request.content))
                if post_count == 1:
                    raise httpx.ReadTimeout("wire detail", request=request)
                return httpx.Response(201, json=applied(first.command_id))
            return httpx.Response(
                200,
                json={"command_id": str(first.command_id), "status": "pending"},
            )

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        store = BridgeStateStore(MemoryKV())
        dispatcher = CommandDispatcher(client, store)
        assert (
            await dispatcher.execute("planning:request-1", first)
        ).status == "result_unknown"
        recovered = await dispatcher.execute("planning:request-1", second)
        await client.close()

        assert recovered.status == "applied"
        assert bodies[0] == bodies[1]
        assert recovered.command_id == first.command_id

    run(scenario())


def test_different_request_on_locked_resource_is_result_unknown_resource_busy() -> None:
    async def scenario() -> None:
        item = envelope()
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.method)
            if request.method == "POST":
                raise httpx.ReadTimeout("wire detail", request=request)
            return httpx.Response(
                200,
                json={"command_id": str(item.command_id), "status": "pending"},
            )

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        store = BridgeStateStore(MemoryKV())
        dispatcher = CommandDispatcher(client, store)
        first = await dispatcher.execute("planning:request-1", item)
        assert first.status == "result_unknown"

        different = envelope(
            command_id="00000000-0000-0000-0000-000000000030",
            correlation_id="00000000-0000-0000-0000-000000000031",
            payload="changed",
        )
        result = await dispatcher.execute("planning:request-1", different)
        await client.close()

        assert result.status == "result_unknown"
        assert result.error_code == "resource_busy"
        assert seen == ["POST", "GET"]

    run(scenario())


def test_new_intent_uses_one_uuid_for_command_and_correlation() -> None:
    async def scenario() -> None:
        calls = 0

        def factory() -> UUID:
            nonlocal calls
            calls += 1
            return UUID("00000000-0000-0000-0000-000000000099")

        client = ControlPlaneClient(
            config(),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
        dispatcher = CommandDispatcher(
            client,
            BridgeStateStore(MemoryKV()),
            uuid_factory=factory,
        )
        built = dispatcher.new_envelope(intent())
        await client.close()
        assert calls == 1
        assert built.command_id == built.correlation_id
        assert built.idempotency_key.endswith(str(built.command_id))

    run(scenario())
