from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from sidera_task_bridge.client import (
    CommandExecution,
    CommandResponse,
    ControlPlaneClient,
    DispatchResult,
    NotificationDeliveryEnvelope,
    TransportUnavailable,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.notification_gateway import (
    GatewayNotificationRunner,
    GatewayNotReady,
    MalformedNotificationDelivery,
)
from sidera_task_bridge.state import (
    BridgeStateStore,
    PendingNotification,
    PrivateRoute,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
MODULE_ID = UUID("00000000-0000-0000-0000-000000000070")
ROUTE_ID = UUID("00000000-0000-0000-0000-000000000071")
DELIVERY_ID = UUID("00000000-0000-0000-0000-000000000072")
NOTIFICATION_ID = UUID("00000000-0000-0000-0000-000000000073")
THREAD_ID = UUID("00000000-0000-0000-0000-000000000074")
CLAIM_TOKEN = UUID("00000000-0000-0000-0000-000000000075")
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value


def config(**overrides: Any) -> TaskBridgeConfig:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "planning_profile_id": PROFILE_ID,
        "planning_profile_version": 1,
        "notification_mode": "gateway",
        "notification_channel_module_instance_id": MODULE_ID,
        "notification_delivery_timeout_seconds": 0.01,
        "notification_gateway_poll_seconds": 0.01,
    }
    values.update(overrides)
    return TaskBridgeConfig.model_validate(values)


def envelope(**overrides: Any) -> NotificationDeliveryEnvelope:
    values: dict[str, Any] = {
        "delivery_id": DELIVERY_ID,
        "notification_id": NOTIFICATION_ID,
        "thread_id": THREAD_ID,
        "contract_version": 1,
        "claim_token": CLAIM_TOKEN,
        "fencing_token": 2,
        "idempotency_key": "sidera:notification-delivery:delivery-72",
        "channel_type": "akashic_private",
        "source_ref": "src_" + "a" * 32,
        "kind": "task_needs_user",
        "priority": "action",
        "root_task_title": "Build <gateway>",
        "subtask_path": ("Transport & safety",),
        "worker_item_title": "Provider <adapter>",
        "title": "Review <route>",
        "summary": "The task needs a decision & review.",
        "next_action": "Open the task and choose a direction.",
        "deep_link": f"http://127.0.0.1:4174/tasks/{WORKSPACE_ID}",
        "correlation_id": "corr-72",
        "notification_ref": "NTF-7C21",
        "created_at": NOW,
    }
    values.update(overrides)
    return NotificationDeliveryEnvelope.model_validate(values)


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def execute(self, resource_lock_key: str, command: object) -> DispatchResult:
        self.calls.append((resource_lock_key, command))
        return DispatchResult(
            status="applied",
            command_id=UUID("00000000-0000-0000-0000-000000000076"),
            execution=CommandExecution(
                status="applied",
                command_id=UUID("00000000-0000-0000-0000-000000000076"),
                result=CommandResponse(
                    command_id=UUID("00000000-0000-0000-0000-000000000076"),
                    aggregate_id=ROUTE_ID,
                    aggregate_version=1,
                    event_ids=(),
                    data={"notification_route_id": str(ROUTE_ID)},
                ),
            ),
        )


class FakeClient:
    def __init__(self) -> None:
        self.claim_batches: list[tuple[object, ...]] = []
        self.claim_calls: list[dict[str, object]] = []
        self.acks: list[dict[str, object]] = []
        self.nacks: list[dict[str, object]] = []
        self.readiness: object = {"status": "ready"}
        self.modules: object = (
            {
                "id": str(MODULE_ID),
                "instance_name": "Akashic private channel",
                "provider": "akashic",
                "module_type": "notification_channel",
                "config_version": 1,
                "definition_lifecycle": "active",
                "instance_lifecycle": "active",
            },
        )
        self.ack_errors: list[Exception | None] = []

    async def get_notification_readiness(self) -> object:
        return self.readiness

    async def list_notification_modules(self) -> object:
        return self.modules

    async def claim_notification_deliveries(
        self, **kwargs: object
    ) -> tuple[object, ...]:
        self.claim_calls.append(kwargs)
        if self.claim_batches:
            return self.claim_batches.pop(0)
        return ()

    async def ack_notification_delivery(
        self, delivery_id: UUID, **kwargs: object
    ) -> object:
        self.acks.append({"delivery_id": delivery_id, **kwargs})
        if self.ack_errors:
            error = self.ack_errors.pop(0)
            if error is not None:
                raise error
        return {"delivery_id": str(delivery_id), "status": "delivered"}

    async def nack_notification_delivery(
        self, delivery_id: UUID, **kwargs: object
    ) -> object:
        self.nacks.append({"delivery_id": delivery_id, **kwargs})
        return {"delivery_id": str(delivery_id), "status": "retry_wait"}


class FakeRegistry:
    def __init__(self, result: object = "消息已发送") -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object], dict[str, object]]] = []

    async def execute(
        self,
        tool_name: str,
        payload: dict[str, object],
        *,
        internal_arguments: dict[str, object] | None = None,
        raise_errors: bool,
    ) -> object:
        del raise_errors
        self.calls.append((tool_name, payload, dict(internal_arguments or {})))
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            return await self.result()
        return self.result


async def make_runner(
    *,
    client: FakeClient | None = None,
    registry: FakeRegistry | None = None,
    state: BridgeStateStore | None = None,
    dispatcher: FakeDispatcher | None = None,
    **config_overrides: Any,
) -> tuple[
    GatewayNotificationRunner,
    FakeClient,
    FakeRegistry,
    BridgeStateStore,
    FakeDispatcher,
]:
    resolved_client = client or FakeClient()
    resolved_registry = registry or FakeRegistry()
    resolved_state = state or BridgeStateStore(MemoryKV())
    resolved_dispatcher = dispatcher or FakeDispatcher()
    runner = GatewayNotificationRunner(
        config(**config_overrides),
        client=resolved_client,
        state=resolved_state,
        dispatcher=resolved_dispatcher,
        tool_registry=resolved_registry,
        clock=lambda: NOW,
        sleeper=lambda _delay: asyncio.sleep(0),
    )
    return (
        runner,
        resolved_client,
        resolved_registry,
        resolved_state,
        resolved_dispatcher,
    )


def test_gateway_bootstrap_registers_missing_routes_idempotently_without_erasing_state() -> (
    None
):
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        source_ref = "src_" + "a" * 32
        await state.save_route(
            source_ref,
            PrivateRoute(
                channel="telegram",
                chat_id="chat-42",
                session_key="telegram:chat-42",
            ),
        )
        existing_ref = "src_" + "b" * 32
        await state.save_route(
            existing_ref,
            PrivateRoute(
                channel="telegram",
                chat_id="chat-43",
                session_key="telegram:chat-43",
                control_plane_route_id=UUID("00000000-0000-0000-0000-000000000077"),
            ),
        )
        before = await state.snapshot()
        runner, client, _, _, dispatcher = await make_runner(state=state)

        await runner.startup()
        await runner.startup()

        assert client.claim_calls == []
        assert len(dispatcher.calls) == 1
        command = dispatcher.calls[0][1]
        assert command.command_type == "RegisterNotificationRoute"
        assert command.payload["source_ref"] == source_ref
        after = await state.snapshot()
        assert after.notification_cursor == before.notification_cursor
        assert after.pending_notifications == before.pending_notifications
        assert after.notification_snapshots == before.notification_snapshots
        assert after.undeliverable_notifications == before.undeliverable_notifications
        assert after.source_routes[existing_ref] == before.source_routes[existing_ref]
        assert after.source_routes[source_ref].control_plane_route_id == ROUTE_ID

    asyncio.run(scenario())


def test_gateway_pending_legacy_queue_blocks_claim_and_preserves_old_state() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await state.enqueue_notification(
            PendingNotification(
                notice_id="legacy-notice",
                cursor=8,
                resource_key="task:00000000-0000-0000-0000-000000000078",
                source_ref="src_" + "a" * 32,
                kind="task_needs_user",
                message="legacy message",
                created_at=NOW,
            )
        )
        client = FakeClient()
        runner, client, _, state, _ = await make_runner(client=client, state=state)
        before = await state.snapshot()

        with pytest.raises(GatewayNotReady, match="legacy_pending_notifications"):
            await runner.run()

        assert client.claim_calls == []
        assert await state.snapshot() == before

    asyncio.run(scenario())


def test_gateway_rechecks_legacy_queue_before_each_claim() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        runner, client, _, state, _ = await make_runner()

        await runner.startup()
        await state.enqueue_notification(
            PendingNotification(
                notice_id="legacy-after-startup",
                cursor=9,
                resource_key="task:00000000-0000-0000-0000-000000000079",
                source_ref="src_" + "a" * 32,
                kind="task_needs_user",
                message="legacy message",
                created_at=NOW,
            )
        )

        with pytest.raises(GatewayNotReady, match="legacy_pending_notifications"):
            await runner.run_once()

        assert client.claim_calls == []

    asyncio.run(scenario())


def test_gateway_claim_renders_safe_message_and_acks_once() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        source_ref = "src_" + "a" * 32
        await state.save_route(
            source_ref,
            PrivateRoute(
                channel="telegram",
                chat_id="chat-42",
                session_key="telegram:chat-42",
            ),
        )
        client = FakeClient()
        client.claim_batches = [(envelope(),)]
        registry = FakeRegistry()
        runner, client, registry, _, _ = await make_runner(
            client=client, registry=registry, state=state
        )

        await runner.run_once()

        assert len(registry.calls) == 1
        tool_name, payload, internal = registry.calls[0]
        assert tool_name == "message_push"
        assert payload["target_channel"] == "telegram"
        assert payload["target_chat_id"] == "chat-42"
        message = str(payload["message"])
        assert "NTF-7C21" in message
        assert "http://127.0.0.1:4174/tasks/" in message
        assert "&lt;" in message
        assert "Build <gateway>" not in message
        assert len(message.encode()) <= 8192
        assert internal == {
            "_commit_role": "passive",
            "_outbound_metadata": {
                "delivery_id": str(DELIVERY_ID),
                "idempotency_key": "sidera:notification-delivery:delivery-72",
                "notification_ref": "NTF-7C21",
                "_channel_commit_role": "passive",
            },
        }
        assert client.acks[0]["delivery_id"] == DELIVERY_ID
        assert client.nacks == []

    asyncio.run(scenario())


def test_gateway_unknown_route_is_terminal_without_calling_provider() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.claim_batches = [(envelope(),)]
        registry = FakeRegistry()
        runner, client, registry, _, _ = await make_runner(
            client=client, registry=registry
        )

        await runner.run_once()

        assert registry.calls == []
        assert client.acks == []
        assert client.nacks[0]["outcome"] == "terminal_failure"
        assert client.nacks[0]["stable_error_code"] == "route_not_found"

    asyncio.run(scenario())


def test_gateway_rejects_an_unnegotiated_contract_version_before_provider_call() -> (
    None
):
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await state.save_route(
            "src_" + "a" * 32,
            PrivateRoute(
                channel="telegram",
                chat_id="chat-42",
                session_key="telegram:chat-42",
            ),
        )
        client = FakeClient()
        client.claim_batches = [(envelope(contract_version=2),)]
        registry = FakeRegistry()
        runner, client, registry, _, _ = await make_runner(
            client=client, registry=registry, state=state
        )

        await runner.run_once()

        assert registry.calls == []
        assert client.acks == []
        assert client.nacks[0]["outcome"] == "terminal_failure"
        assert client.nacks[0]["stable_error_code"] == "contract_unsupported"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("provider_result", "error_code", "outcome"),
    [
        (TimeoutError("provider timeout"), "timeout", "retryable_failure"),
        (
            RuntimeError("provider unavailable"),
            "provider_unavailable",
            "retryable_failure",
        ),
        (RuntimeError("rate limited: 429"), "rate_limited", "retryable_failure"),
        (PermissionError("permission denied"), "permission_denied", "terminal_failure"),
        (RuntimeError("route revoked"), "route_revoked", "terminal_failure"),
        (ValueError("payload rejected"), "payload_rejected", "terminal_failure"),
    ],
)
def test_gateway_normalizes_provider_failures_to_stable_nack(
    provider_result: object, error_code: str, outcome: str
) -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await state.save_route(
            "src_" + "a" * 32,
            PrivateRoute(
                channel="telegram",
                chat_id="chat-42",
                session_key="telegram:chat-42",
            ),
        )
        client = FakeClient()
        client.claim_batches = [(envelope(),)]
        runner, client, _, _, _ = await make_runner(
            client=client,
            registry=FakeRegistry(provider_result),
            state=state,
        )

        await runner.run_once()

        assert client.acks == []
        assert client.nacks[0]["stable_error_code"] == error_code
        assert client.nacks[0]["outcome"] == outcome
        assert "provider timeout" not in str(client.nacks[0])

    asyncio.run(scenario())


def test_gateway_success_then_settle_failure_leaves_claim_for_expiry_and_reuses_identity() -> (
    None
):
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await state.save_route(
            "src_" + "a" * 32,
            PrivateRoute(
                channel="telegram",
                chat_id="chat-42",
                session_key="telegram:chat-42",
            ),
        )
        client = FakeClient()
        client.claim_batches = [(envelope(),), (envelope(),)]
        client.ack_errors = [TransportUnavailable("settle unavailable"), None]
        registry = FakeRegistry()
        runner, client, registry, _, _ = await make_runner(
            client=client, registry=registry, state=state
        )

        await runner.run_once()
        await runner.run_once()

        assert client.nacks == []
        assert len(client.acks) == 2
        assert len(registry.calls) == 2
        assert registry.calls[0][1]["message"] == registry.calls[1][1]["message"]
        assert registry.calls[0][2] == registry.calls[1][2]
        assert (
            registry.calls[0][2]["_outbound_metadata"]["notification_ref"] == "NTF-7C21"
        )
        assert registry.calls[0][2]["_outbound_metadata"]["idempotency_key"] == (
            "sidera:notification-delivery:delivery-72"
        )

    asyncio.run(scenario())


def test_gateway_malformed_item_is_nacked_without_stopping_other_claims() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await state.save_route(
            "src_" + "a" * 32,
            PrivateRoute(
                channel="telegram",
                chat_id="chat-42",
                session_key="telegram:chat-42",
            ),
        )
        client = FakeClient()
        client.claim_batches = [
            (
                MalformedNotificationDelivery(
                    delivery_id=DELIVERY_ID,
                    claim_token=CLAIM_TOKEN,
                    fencing_token=2,
                    error_code="payload_rejected",
                ),
                envelope(),
            )
        ]
        registry = FakeRegistry()
        runner, client, registry, _, _ = await make_runner(
            client=client, registry=registry, state=state
        )

        await runner.run_once()

        assert client.nacks[0]["stable_error_code"] == "payload_rejected"
        assert len(registry.calls) == 1
        assert len(client.acks) == 1

    asyncio.run(scenario())


def test_gateway_never_consumes_projection_stream_or_constructs_evaluator() -> None:
    async def scenario() -> None:
        class ProjectionClient(FakeClient):
            async def iter_projection_changes(self, **_: object) -> object:
                raise AssertionError("gateway must not consume ProjectionChange")

        client = ProjectionClient()
        runner, client, _, _, _ = await make_runner(client=client)
        await runner.run_once()
        assert client.claim_calls

    asyncio.run(scenario())


def test_control_plane_client_uses_strict_gateway_delivery_contract() -> None:
    async def scenario() -> None:
        requests: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode()) if request.content else None
            requests.append(
                (request.method, request.url.path, dict(request.url.params), body)
            )
            if request.url.path.endswith("/notification-readiness"):
                return httpx.Response(
                    200,
                    json={
                        "component": "sidera-notification-worker",
                        "as_of": NOW.isoformat(),
                        "status": "ready",
                        "checks": [
                            {
                                "name": "migration",
                                "ready": True,
                                "code": "notification_migration_ready",
                                "summary": "schema ready",
                            }
                        ],
                    },
                )
            if request.url.path == "/api/v1/modules":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": str(MODULE_ID),
                            "instance_name": "Akashic private channel",
                            "provider": "akashic",
                            "module_type": "notification_channel",
                            "config_version": 1,
                            "definition_lifecycle": "active",
                            "instance_lifecycle": "active",
                        }
                    ],
                )
            if request.url.path.endswith("/notification-deliveries/claims"):
                valid = envelope().model_dump(mode="json")
                malformed = dict(valid)
                malformed["summary"] = "private body must not be retained"
                malformed["unexpected"] = "secret-body"
                return httpx.Response(200, json={"deliveries": [valid, malformed]})
            settlement = {
                "delivery_id": str(DELIVERY_ID),
                "status": "delivered"
                if request.url.path.endswith("/ack")
                else "retry_wait",
                "attempt_number": 1,
                "outcome": "delivered"
                if request.url.path.endswith("/ack")
                else "retryable_failure",
                "provider_message_ref": "provider-72"
                if request.url.path.endswith("/ack")
                else None,
                "next_attempt_at": None
                if request.url.path.endswith("/ack")
                else NOW.isoformat(),
                "fencing_token": 2,
                "error_code": None if request.url.path.endswith("/ack") else "timeout",
            }
            return httpx.Response(200, json=settlement)

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        readiness = await client.get_notification_readiness()
        modules = await client.list_notification_modules()
        items = await client.claim_notification_deliveries()
        ack = await client.ack_notification_delivery(
            DELIVERY_ID,
            claim_token=CLAIM_TOKEN,
            fencing_token=2,
            completed_at=NOW,
            provider_message_ref="provider-72",
        )
        nack = await client.nack_notification_delivery(
            DELIVERY_ID,
            claim_token=CLAIM_TOKEN,
            fencing_token=2,
            outcome="retryable_failure",
            stable_error_code="timeout",
            retry_after_seconds=10,
        )
        await client.close()

        assert readiness.status == "ready"
        assert modules[0].id == MODULE_ID
        assert isinstance(items[0], NotificationDeliveryEnvelope)
        assert isinstance(items[1], MalformedNotificationDelivery)
        assert items[1].delivery_id == DELIVERY_ID
        assert not hasattr(items[1], "summary")
        assert ack.status == "delivered"
        assert nack.status == "retry_wait"
        assert requests[0][:3] == (
            "GET",
            f"/api/v1/workspaces/{WORKSPACE_ID}/notification-readiness",
            {},
        )
        assert requests[1][:3] == (
            "GET",
            "/api/v1/modules",
            {
                "workspace_id": str(WORKSPACE_ID),
                "module_type": "notification_channel",
            },
        )
        assert requests[2][3] == {
            "module_instance_id": str(MODULE_ID),
            "supported_contract_versions": [1],
            "batch_size": 50,
            "lease_seconds": 30,
        }
        assert requests[3][3] == {
            "claim_token": str(CLAIM_TOKEN),
            "fencing_token": 2,
            "completed_at": NOW.isoformat(),
            "provider_message_ref": "provider-72",
        }
        assert requests[4][3] == {
            "claim_token": str(CLAIM_TOKEN),
            "fencing_token": 2,
            "outcome": "retryable_failure",
            "stable_error_code": "timeout",
            "retry_after_seconds": 10,
            "provider_message_ref": None,
        }

    asyncio.run(scenario())
