import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agent.tools.base import ToolExecutionContext, tool_execution_context_scope

from sidera_task_bridge.client import CommandExecution, CommandResponse, DispatchResult
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.source_routes import SourceContextResolver
from sidera_task_bridge.state import BridgeStateStore
from sidera_task_bridge.tools import SideraTaskTools

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000040")
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
