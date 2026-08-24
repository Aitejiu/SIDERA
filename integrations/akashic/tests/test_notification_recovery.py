import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from agent.tools.base import ToolExecutionContext, tool_execution_context_scope

from sidera_task_bridge.client import (
    CommandExecution,
    CommandResponse,
    DispatchResult,
    ProjectionStreamDisconnected,
    TransportUnavailable,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.contracts import ProjectionChange
from sidera_task_bridge.notifications import (
    NotificationDelivery,
    NotificationEvaluator,
    NotificationQueryUnavailable,
    NotificationRunner,
)
from sidera_task_bridge.source_routes import SourceContextResolver
from sidera_task_bridge.state import BridgeStateStore, PrivateRoute
from sidera_task_bridge.tools import SideraTaskTools

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000040")
TASK_ID = UUID("00000000-0000-0000-0000-000000000041")
COMMAND_ID = UUID("00000000-0000-0000-0000-000000000030")


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def execute(self, resource_lock_key: str, command: object) -> DispatchResult:
        self.calls.append((resource_lock_key, command))
        return DispatchResult(
            status="applied",
            command_id=COMMAND_ID,
            execution=CommandExecution(
                status="applied",
                command_id=COMMAND_ID,
                result=CommandResponse(
                    command_id=COMMAND_ID,
                    aggregate_id=REQUEST_ID,
                    aggregate_version=1,
                    event_ids=(),
                    data={"planning_request_id": str(REQUEST_ID)},
                ),
            ),
        )


class FakeClient:
    async def get_planning_detail(self, _request_id: UUID) -> dict[str, Any]:
        return {}


def config() -> TaskBridgeConfig:
    return TaskBridgeConfig.model_validate(
        {
            "workspace_id": WORKSPACE_ID,
            "planning_profile_id": PROFILE_ID,
            "planning_profile_version": 1,
        }
    )


def test_submit_brief_records_private_owner_binding_after_applied_command() -> None:
    async def scenario() -> None:
        kv = MemoryKV()
        state = BridgeStateStore(kv)
        bridge = SideraTaskTools(
            config(),
            client=FakeClient(),
            dispatcher=FakeDispatcher(),
            state=state,
            source_resolver=SourceContextResolver(
                source_ref_factory=lambda: "src_private"
            ),
            session_metadata_provider=lambda _key: {},
        )
        context = ToolExecutionContext(
            origin_channel="telegram",
            origin_chat_id="chat-42",
            origin_session_key="telegram:chat-42",
        )
        with tool_execution_context_scope(context):
            result = json.loads(
                await bridge.sidera_submit_brief(
                    None,
                    "Build an API",
                    [],
                    ["Tests pass"],
                    [],
                    ["source code"],
                )
            )

        assert result["outcome"] == "ok"
        state_snapshot = await state.snapshot()
        binding = state_snapshot.resource_sources[f"planning_request:{REQUEST_ID}"]
        assert binding.source_type == "akashic_private"
        assert binding.source_ref == "src_private"

    asyncio.run(scenario())


def test_binding_timestamp_is_stable_when_recovered_result_is_replayed() -> None:
    async def scenario() -> None:
        store = BridgeStateStore(MemoryKV())
        request_id = UUID("00000000-0000-0000-0000-000000000040")
        bound_at = datetime(2026, 8, 24, 3, 30, tzinfo=UTC)
        await store.bind_resource_source(
            "planning_request",
            request_id,
            "akashic_private",
            "src_private",
            bound_at=bound_at,
        )
        await store.bind_resource_source(
            "planning_request",
            request_id,
            "akashic_private",
            "src_private",
            bound_at=datetime(2026, 8, 24, 4, 30, tzinfo=UTC),
        )
        binding = (await store.snapshot()).resource_sources[
            f"planning_request:{request_id}"
        ]
        assert binding.bound_at == bound_at

    asyncio.run(scenario())


class RunnerClient:
    def __init__(self) -> None:
        self.stream_calls: list[int] = []
        self.stream_error: Exception | None = None
        self.stream_plan: list[str] = []
        self.detail: dict[str, Any] = {}
        self.materialization: dict[str, Any] = {}
        self.overview: dict[str, Any] = {}

    async def iter_projection_changes(self, *, after: int):
        self.stream_calls.append(after)
        if self.stream_plan:
            mode = self.stream_plan.pop(0)
            if mode == "event":
                yield runner_change(1, "other_projection", TASK_ID)
                return
        if self.stream_error is not None:
            raise self.stream_error
        if False:
            yield None

    async def get_planning_detail(self, _request_id: UUID) -> dict[str, Any]:
        if self.stream_error is not None:
            raise self.stream_error
        return self.detail

    async def list_clarifications(self, _request_id: UUID) -> tuple[dict[str, Any], ...]:
        return ()

    async def get_materialization(self, _request_id: UUID) -> dict[str, Any]:
        return self.materialization

    async def get_task_overview(self, _task_id: UUID) -> dict[str, Any]:
        if self.stream_error is not None:
            raise self.stream_error
        return self.overview


class RunnerRegistry:
    def __init__(self, receipt: str = "消息已发送") -> None:
        self.receipt = receipt
        self.calls: list[dict[str, object]] = []

    async def execute(
        self,
        tool_name: str,
        payload: dict[str, object],
        *,
        raise_errors: bool,
    ) -> str:
        del tool_name, raise_errors
        self.calls.append(payload)
        return self.receipt


def runner_change(cursor: int, projection_type: str, resource_id: UUID) -> ProjectionChange:
    return ProjectionChange.model_validate(
        {
            "workspace_id": str(WORKSPACE_ID),
            "cursor": cursor,
            "schema_version": 1,
            "projection_type": projection_type,
            "resource_id": str(resource_id),
            "change_type": "resync",
            "resource_version": cursor,
            "occurred_at": "2026-08-24T03:30:00Z",
            "correlation_id": str(COMMAND_ID),
        }
    )


async def setup_runner_state() -> tuple[BridgeStateStore, MemoryKV]:
    kv = MemoryKV()
    state = BridgeStateStore(kv)
    await state.save_route(
        "src_private",
        PrivateRoute(channel="telegram", chat_id="chat-42", session_key="telegram:chat-42"),
    )
    return state, kv


def test_runner_waits_without_private_bindings_and_stops_on_cancel() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        client = RunnerClient()
        evaluator = NotificationEvaluator(config(), client=client, state=state)
        delivery = NotificationDelivery(
            config(), state=state, tool_registry=RunnerRegistry()
        )
        runner = NotificationRunner(
            config(),
            client=client,
            state=state,
            evaluator=evaluator,
            delivery=delivery,
        )
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0)
        assert client.stream_calls == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_runner_uses_capped_exponential_backoff_and_stops() -> None:
    async def scenario() -> None:
        state, _ = await setup_runner_state()
        client = RunnerClient()
        client.stream_error = ProjectionStreamDisconnected(0)
        evaluator = NotificationEvaluator(config(), client=client, state=state)
        delivery = NotificationDelivery(
            config(), state=state, tool_registry=RunnerRegistry()
        )
        delays: list[float] = []
        runner: NotificationRunner

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 8:
                runner.stop()

        runner = NotificationRunner(
            config(),
            client=client,
            state=state,
            evaluator=evaluator,
            delivery=delivery,
            sleeper=fake_sleep,
        )
        await state.bind_resource_source("task", TASK_ID, "akashic_private", "src_private")
        await runner.run()
        assert delays == [0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 15.0, 15.0]

    asyncio.run(scenario())


def test_runner_resets_backoff_after_a_successful_event() -> None:
    async def scenario() -> None:
        state, _ = await setup_runner_state()
        await state.bind_resource_source("task", TASK_ID, "akashic_private", "src_private")
        client = RunnerClient()
        client.stream_error = ProjectionStreamDisconnected(0)
        client.stream_plan = ["error", "error", "event"]
        evaluator = NotificationEvaluator(config(), client=client, state=state)
        delivery = NotificationDelivery(
            config(), state=state, tool_registry=RunnerRegistry()
        )
        delays: list[float] = []
        runner: NotificationRunner

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 3:
                runner.stop()

        runner = NotificationRunner(
            config(),
            client=client,
            state=state,
            evaluator=evaluator,
            delivery=delivery,
            sleeper=fake_sleep,
        )
        await runner.run()
        assert delays == [0.5, 1.0, 0.5]

    asyncio.run(scenario())


def test_resync_queries_current_facts_binds_materialized_task_and_uses_max_cursor() -> None:
    async def scenario() -> None:
        state, _ = await setup_runner_state()
        await state.bind_resource_source(
            "planning_request", REQUEST_ID, "akashic_private", "src_private"
        )
        client = RunnerClient()
        client.detail = {
            "status": "materialized",
            "version": 5,
            "last_change_cursor": 10,
            "source_type": "akashic_private",
            "source_ref": "src_private",
        }
        client.materialization = {"root_task_id": str(TASK_ID)}
        client.overview = {
            "root_task_id": str(TASK_ID),
            "title": "Release API",
            "generation": 2,
            "last_change_cursor": 11,
            "subtask_count": 2,
            "completed_subtask_count": 2,
            "failed_subtask_count": 0,
            "attention_items": [],
            "milestones": [
                {"key": "m1", "title": "Build", "state": "completed"}
            ],
        }
        registry = RunnerRegistry()
        evaluator = NotificationEvaluator(config(), client=client, state=state)
        delivery = NotificationDelivery(config(), state=state, tool_registry=registry)
        runner = NotificationRunner(
            config(), client=client, state=state, evaluator=evaluator, delivery=delivery
        )

        await runner.resync_current_facts()
        snapshot = await state.snapshot()
        assert snapshot.notification_cursor == 11
        assert f"task:{TASK_ID}" in snapshot.resource_sources
        assert len(registry.calls) == 2
        assert all("里程碑" not in str(call["message"]) for call in registry.calls)

    asyncio.run(scenario())


def test_resync_does_not_advance_cursor_when_query_or_delivery_fails() -> None:
    async def scenario() -> None:
        state, _ = await setup_runner_state()
        await state.bind_resource_source(
            "planning_request", REQUEST_ID, "akashic_private", "src_private"
        )
        client = RunnerClient()
        client.stream_error = TransportUnavailable("offline")
        evaluator = NotificationEvaluator(config(), client=client, state=state)
        delivery = NotificationDelivery(config(), state=state, tool_registry=RunnerRegistry())
        runner = NotificationRunner(
            config(), client=client, state=state, evaluator=evaluator, delivery=delivery
        )
        with pytest.raises(NotificationQueryUnavailable):
            await runner.resync_current_facts()
        assert (await state.snapshot()).notification_cursor == 0

        client.stream_error = None
        client.detail = {
            "status": "proposal_ready",
            "version": 2,
            "last_change_cursor": 5,
            "source_type": "akashic_private",
            "source_ref": "src_private",
        }
        failing_delivery = NotificationDelivery(
            config(), state=state, tool_registry=RunnerRegistry("发送失败：offline")
        )
        runner = NotificationRunner(
            config(), client=client, state=state, evaluator=evaluator, delivery=failing_delivery
        )
        with pytest.raises(RuntimeError, match="notification_delivery_pending"):
            await runner.resync_current_facts()
        assert (await state.snapshot()).notification_cursor == 0

    asyncio.run(scenario())
