import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agent.tools.base import ToolExecutionContext, tool_execution_context_scope

from sidera_task_bridge.client import (
    CommandExecution,
    CommandResponse,
    DispatchResult,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.contracts import (
    NotificationDelivery,
    NotificationFact,
    NotificationFactPage,
    NotificationSummary,
    NotificationThread,
    NotificationThreadDetail,
    NotificationThreadPage,
)
from sidera_task_bridge.notification_tools import SideraNotificationTools
from sidera_task_bridge.source_routes import SourceContextResolver
from sidera_task_bridge.state import BridgeStateStore, PrivateRoute

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
MODULE_ID = UUID("00000000-0000-0000-0000-000000000003")
ROUTE_ID = UUID("00000000-0000-0000-0000-000000000004")
THREAD_ID = UUID("00000000-0000-0000-0000-000000000010")
OTHER_THREAD_ID = UUID("00000000-0000-0000-0000-000000000011")
FACT_ID = UUID("00000000-0000-0000-0000-000000000020")
DELIVERY_ID = UUID("00000000-0000-0000-0000-000000000030")
TASK_ID = UUID("00000000-0000-0000-0000-000000000040")
WORKER_ITEM_ID = UUID("00000000-0000-0000-0000-000000000050")
COMMAND_ID = UUID("00000000-0000-0000-0000-000000000060")
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


class FakeClient:
    def __init__(self, *, threads: tuple[NotificationThread, ...]) -> None:
        self.summary_calls = 0
        self.thread_list_calls: list[tuple[str, bool, str | None, int]] = []
        self.route_list_calls: list[tuple[UUID, str | None, int]] = []
        self.detail_calls: list[tuple[UUID, str | None, int]] = []
        self.summary = NotificationSummary(
            recipient_type="workspace_operator",
            recipient_id="local",
            open_count=len(threads),
            unread_count=len(threads),
            progress_count=0,
            channel_issue_count=0,
            last_change_cursor=3,
        )
        self.page = NotificationThreadPage(
            items=threads,
            next_cursor=None,
            last_change_cursor=3,
        )
        self.detail = make_detail()

    async def get_notification_summary(self) -> NotificationSummary:
        self.summary_calls += 1
        return self.summary

    async def list_notification_threads(
        self,
        *,
        category: str,
        unread_only: bool,
        cursor: str | None,
        limit: int,
    ) -> NotificationThreadPage:
        self.thread_list_calls.append((category, unread_only, cursor, limit))
        return self.page

    async def list_notification_route_threads(
        self, route_id: UUID, *, cursor: str | None, limit: int
    ) -> NotificationThreadPage:
        self.route_list_calls.append((route_id, cursor, limit))
        return self.page

    async def get_notification_thread(
        self, thread_id: UUID, *, fact_cursor: str | None, fact_limit: int
    ) -> NotificationThreadDetail:
        self.detail_calls.append((thread_id, fact_cursor, fact_limit))
        if thread_id != self.detail.thread.id:
            raise AssertionError("unexpected thread detail lookup")
        return self.detail


class FakeDispatcher:
    def __init__(self, result: DispatchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, resource_lock_key: str, command: Any) -> DispatchResult:
        self.calls.append((resource_lock_key, command))
        return self.result


def make_config(*, module: bool = True) -> TaskBridgeConfig:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "planning_profile_id": PROFILE_ID,
        "planning_profile_version": 1,
    }
    if module:
        values["notification_channel_module_instance_id"] = MODULE_ID
    return TaskBridgeConfig.model_validate(values)


def make_thread(
    *,
    thread_id: UUID = THREAD_ID,
    notification_ref: str = "NTF-1234",
    subtask_id: UUID | None = None,
    subtask_title: str | None = None,
    category: str = "action",
    unread_count: int = 1,
) -> NotificationThread:
    return NotificationThread(
        id=thread_id,
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        category=category,
        priority="action",
        topic="execution_attention",
        root_task_id=TASK_ID,
        root_task_title="Build API",
        subtask_id=subtask_id,
        subtask_title=subtask_title,
        worker_item_id=WORKER_ITEM_ID,
        worker_item_title="Backend Worker",
        latest_fact_id=FACT_ID,
        latest_fact_occurred_at=NOW,
        title="Execution needs attention",
        summary="The execution is waiting for review.",
        action="Open the task",
        deep_link=f"/workspaces/{WORKSPACE_ID}/tasks/{TASK_ID}",
        notification_ref=notification_ref,
        business_status="open",
        organization_status="active",
        last_read_fact_id=None,
        unread_count=unread_count,
        version=2,
        opened_at=NOW,
        resolved_at=None,
        archived_at=None,
        updated_at=NOW,
    )


def make_fact(*, projection_type: str = "collaboration_governance") -> NotificationFact:
    return NotificationFact(
        id=FACT_ID,
        thread_id=THREAD_ID,
        policy_version=1,
        kind="handoff_attention",
        priority="action",
        transition="opened",
        topic="execution_attention",
        root_task_id=TASK_ID,
        subtask_id=None,
        worker_item_id=WORKER_ITEM_ID,
        source_projection_cursor=3,
        source_projection_type=projection_type,
        source_resource_id=WORKER_ITEM_ID,
        source_resource_version=4,
        title="Execution needs attention",
        summary="The execution is waiting for review.",
        action="Open the task",
        deep_link=f"/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}",
        correlation_id="notification-correlation",
        baseline_import=False,
        occurred_at=NOW,
        created_at=NOW,
    )


def make_detail() -> NotificationThreadDetail:
    return NotificationThreadDetail(
        thread=make_thread(),
        facts=NotificationFactPage(items=(make_fact(),), next_cursor=None),
        deliveries=(
            NotificationDelivery(
                delivery_id=DELIVERY_ID,
                route_id=ROUTE_ID,
                channel_type="akashic_private",
                contract_version=1,
                status="exhausted",
                attempts=3,
                next_attempt_at=NOW,
                last_error_code="provider_unavailable",
                delivered_at=None,
                exhausted_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            ),
        ),
        notification_ref="NTF-1234",
        available_commands=("mark_read", "archive"),
        deep_link=f"/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}",
        last_change_cursor=3,
    )


def private_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        origin_channel="telegram",
        origin_chat_id="chat-42",
        origin_session_key="telegram:chat-42",
    )


def applied_result(*, data: dict[str, Any] | None = None) -> DispatchResult:
    return DispatchResult(
        status="applied",
        command_id=COMMAND_ID,
        execution=CommandExecution(
            status="applied",
            command_id=COMMAND_ID,
            result=CommandResponse(
                command_id=COMMAND_ID,
                aggregate_id=ROUTE_ID,
                aggregate_version=1,
                event_ids=(),
                data=data or {},
            ),
        ),
    )


def tools(
    *,
    client: FakeClient,
    dispatcher: FakeDispatcher,
    state: BridgeStateStore,
    module: bool = True,
) -> SideraNotificationTools:
    return SideraNotificationTools(
        make_config(module=module),
        client=client,
        dispatcher=dispatcher,
        state=state,
        source_resolver=SourceContextResolver(
            source_ref_factory=lambda: "src_private"
        ),
        session_metadata_provider=lambda _key: {},
    )


def decode(value: str) -> dict[str, Any]:
    return json.loads(value)


async def seed_private_route(
    state: BridgeStateStore, *, route_id: UUID | None = None
) -> None:
    await state.save_route(
        "src_private",
        PrivateRoute(
            channel="telegram",
            chat_id="chat-42",
            session_key="telegram:chat-42",
            control_plane_route_id=route_id,
        ),
    )


def test_private_inbox_uses_exact_route_and_persists_only_route_uuid() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await seed_private_route(state)
        client = FakeClient(threads=(make_thread(),))
        dispatcher = FakeDispatcher(
            applied_result(data={"notification_route_id": str(ROUTE_ID)})
        )
        bridge = tools(client=client, dispatcher=dispatcher, state=state)

        with tool_execution_context_scope(private_context()):
            result = decode(await bridge.sidera_list_inbox())

        assert result["outcome"] == "ok"
        assert result["data"]["scope"] == "origin_route"
        assert client.summary_calls == 0
        assert client.route_list_calls == [(ROUTE_ID, None, 20)]
        assert dispatcher.calls[0][1].payload == {
            "channel_module_instance_id": str(MODULE_ID),
            "channel_type": "akashic_private",
            "recipient_type": "workspace_operator",
            "recipient_id": "local",
            "source_ref": "src_private",
        }
        snapshot = await state.snapshot()
        assert snapshot.source_routes["src_private"].control_plane_route_id == ROUTE_ID
        assert "src_private" not in json.dumps(result)

    asyncio.run(scenario())


def test_private_inbox_without_route_configuration_is_bounded_unavailable() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await seed_private_route(state)
        client = FakeClient(threads=(make_thread(),))
        dispatcher = FakeDispatcher(applied_result())
        bridge = tools(
            client=client, dispatcher=dispatcher, state=state, module=False
        )

        with tool_execution_context_scope(private_context()):
            result = decode(await bridge.sidera_list_inbox())

        assert result["outcome"] == "unavailable"
        assert result["error_code"] == "notification_route_unavailable"
        assert client.summary_calls == 0
        assert client.route_list_calls == []
        assert dispatcher.calls == []

    asyncio.run(scenario())


def test_private_inbox_filters_category_and_unread_after_exact_route_query() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await seed_private_route(state, route_id=ROUTE_ID)
        client = FakeClient(
            threads=(
                make_thread(),
                make_thread(
                    thread_id=OTHER_THREAD_ID,
                    category="progress",
                    unread_count=1,
                ),
                make_thread(
                    thread_id=UUID("00000000-0000-0000-0000-000000000012"),
                    category="progress",
                    unread_count=0,
                ),
            )
        )
        bridge = tools(
            client=client,
            dispatcher=FakeDispatcher(applied_result()),
            state=state,
        )

        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_list_inbox(
                    category="progress", unread_only=True
                )
            )

        assert result["outcome"] == "ok"
        assert [item["id"] for item in result["data"]["items"]] == [
            str(OTHER_THREAD_ID)
        ]
        assert client.summary_calls == 0
        assert client.route_list_calls == [(ROUTE_ID, None, 20)]

    asyncio.run(scenario())


def test_notification_ref_ambiguity_returns_bounded_candidates_without_detail_read() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await seed_private_route(state, route_id=ROUTE_ID)
        client = FakeClient(
            threads=(
                make_thread(),
                make_thread(
                    thread_id=OTHER_THREAD_ID,
                    subtask_id=UUID("00000000-0000-0000-0000-000000000070"),
                    subtask_title="API",
                ),
            )
        )
        bridge = tools(
            client=client,
            dispatcher=FakeDispatcher(applied_result()),
            state=state,
        )

        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_get_notification_thread(
                    notification_ref="NTF-1234"
                )
            )

        assert result["outcome"] == "needs_user"
        assert result["error_code"] == "notification_ref_ambiguous"
        assert len(result["data"]["candidates"]) == 2
        assert result["data"]["candidates"][1]["subtask_title"] == "API"
        assert client.detail_calls == []

    asyncio.run(scenario())


def test_unique_thread_detail_includes_current_handoff_without_domain_action() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await seed_private_route(state, route_id=ROUTE_ID)
        client = FakeClient(threads=(make_thread(),))
        bridge = tools(
            client=client,
            dispatcher=FakeDispatcher(applied_result()),
            state=state,
        )

        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_get_notification_thread(
                    notification_ref="NTF-1234"
                )
            )

        assert result["outcome"] == "ok"
        assert result["data"]["thread"]["id"] == str(THREAD_ID)
        assert result["data"]["handoff"] == {
            "resource_ref": f"worker-item:{WORKER_ITEM_ID}",
            "deep_link": f"/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}",
            "suggested_tool": "sidera_get_collaboration_governance",
        }
        assert "approve" not in json.dumps(result)

    asyncio.run(scenario())


def test_manage_notification_allows_only_exact_read_archive_requeue_commands() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await seed_private_route(state, route_id=ROUTE_ID)
        client = FakeClient(threads=(make_thread(),))
        dispatcher = FakeDispatcher(applied_result())
        bridge = tools(client=client, dispatcher=dispatcher, state=state)

        with tool_execution_context_scope(private_context()):
            read = decode(
                await bridge.sidera_manage_notification(
                    action="read",
                    thread_id=str(THREAD_ID),
                    expected_version=2,
                    expected_latest_fact_id=str(FACT_ID),
                )
            )
            archive = decode(
                await bridge.sidera_manage_notification(
                    action="archive",
                    thread_id=str(THREAD_ID),
                    expected_version=2,
                )
            )
            requeue = decode(
                await bridge.sidera_manage_notification(
                    action="requeue",
                    delivery_intent_id=str(DELIVERY_ID),
                    expected_status="exhausted",
                    expected_route_version=4,
                    reason="retry after review",
                )
            )
            forbidden = decode(
                await bridge.sidera_manage_notification(
                    action="approve",
                    thread_id=str(THREAD_ID),
                    expected_version=2,
                )
            )

        assert read["outcome"] == "ok"
        assert archive["outcome"] == "ok"
        assert requeue["outcome"] == "ok"
        assert forbidden["outcome"] == "failed"
        assert forbidden["error_code"] == "unsupported_notification_action"
        assert len(dispatcher.calls) == 3
        read_command = dispatcher.calls[0][1]
        archive_command = dispatcher.calls[1][1]
        requeue_command = dispatcher.calls[2][1]
        assert read_command.command_type == "MarkNotificationThreadRead"
        assert read_command.payload == {"expected_latest_fact_id": str(FACT_ID)}
        assert archive_command.command_type == "ArchiveNotificationThread"
        assert archive_command.payload == {}
        assert requeue_command.command_type == "RequeueNotificationDelivery"
        assert requeue_command.target_id == DELIVERY_ID
        assert requeue_command.payload == {
            "expected_status": "exhausted",
            "expected_route_version": 4,
            "reason": "retry after review",
        }

    asyncio.run(scenario())
