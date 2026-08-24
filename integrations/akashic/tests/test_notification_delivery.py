import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.notifications import (
    NotificationDelivery,
    NotificationEvaluation,
    NotificationNotice,
)
from sidera_task_bridge.state import (
    BridgeStateStore,
    NotificationSnapshot,
    PrivateRoute,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None
        self.events: list[str] = []

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.events.append("state.set")
        self.value = value


class FakeRegistry:
    def __init__(self, result: object = "消息已发送") -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object], bool]] = []
        self.events: list[str] = []
        self.error: Exception | None = None
        self.wait_forever = False

    async def execute(
        self,
        tool_name: str,
        payload: dict[str, object],
        *,
        raise_errors: bool,
    ) -> object:
        self.events.append("message_push")
        self.calls.append((tool_name, payload, raise_errors))
        if self.error is not None:
            raise self.error
        if self.wait_forever:
            await asyncio.sleep(60)
        return self.result


class CrashAfterReceiptState(BridgeStateStore):
    def __init__(self, kv_store: MemoryKV) -> None:
        super().__init__(kv_store)
        self.crash_once = True

    async def settle_notification(self, notice_id: str, *, terminal: bool = True) -> None:
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("simulated crash window")
        await super().settle_notification(notice_id, terminal=terminal)


def config() -> TaskBridgeConfig:
    return TaskBridgeConfig.model_validate(
        {
            "workspace_id": WORKSPACE_ID,
            "planning_profile_id": PROFILE_ID,
            "planning_profile_version": 1,
        }
    )


def evaluation(
    *,
    notice_id: str = "notice-1",
    cursor: int = 4,
    source_ref: str = "src_private",
    resource_key: str = "planning_request:00000000-0000-0000-0000-000000000010",
) -> NotificationEvaluation:
    notice = NotificationNotice(
        notice_id=notice_id,
        cursor=cursor,
        resource_key=resource_key,
        source_ref=source_ref,
        kind="planning_proposal_ready",
        message="任务规划\n方案待确认\n下一步：查看并确认方案\nhttp://127.0.0.1:4174/workspaces/ws/planning/request",
        web_url="http://127.0.0.1:4174/workspaces/ws/planning/request",
    )
    return NotificationEvaluation(
        resource_key=resource_key,
        source_ref=source_ref,
        cursor=cursor,
        snapshot=NotificationSnapshot(
            status="planning_proposal_ready",
            version=2,
            attention_fingerprint="fingerprint",
        ),
        notice=notice,
    )


async def setup(
    *,
    registry: FakeRegistry | None = None,
) -> tuple[BridgeStateStore, MemoryKV, FakeRegistry, NotificationDelivery]:
    kv = MemoryKV()
    state = BridgeStateStore(kv)
    await state.save_route(
        "src_private",
        PrivateRoute(
            channel="telegram",
            chat_id="chat-42",
            session_key="telegram:chat-42",
        ),
    )
    resolved_registry = registry or FakeRegistry()
    delivery = NotificationDelivery(
        config(),
        state=state,
        tool_registry=resolved_registry,
        clock=lambda: datetime(2026, 8, 24, 3, 30, tzinfo=UTC),
        timeout_seconds=0.01,
    )
    return state, kv, resolved_registry, delivery


def test_exact_success_persists_pending_before_push_then_settles_and_advances() -> None:
    async def scenario() -> None:
        state, kv, registry, delivery = await setup()
        registry.events = kv.events
        current_evaluation = evaluation()
        assert current_evaluation.notice is not None
        result = await delivery.deliver(current_evaluation)

        assert result.outcome == "sent"
        assert kv.events.index("state.set") < kv.events.index("message_push")
        assert registry.calls == [
            (
                "message_push",
                {
                    "target_channel": "telegram",
                    "target_chat_id": "chat-42",
                    "message": current_evaluation.notice.message,
                    "_outbound_metadata": {"delivery_id": "notice-1"},
                },
                True,
            )
        ]
        snapshot = await state.snapshot()
        assert snapshot.pending_notifications == []
        assert snapshot.notification_cursor == 4
        assert snapshot.notification_snapshots[evaluation().resource_key].status == (
            "planning_proposal_ready"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", ["消息部分送达：渠道未提交全部内容", "发送失败：offline"])
def test_partial_or_failure_keeps_pending_and_cursor(receipt: str) -> None:
    async def scenario() -> None:
        state, _, _, delivery = await setup(registry=FakeRegistry(receipt))
        result = await delivery.deliver(evaluation())
        snapshot = await state.snapshot()

        assert result.outcome == "pending"
        assert len(snapshot.pending_notifications) == 1
        assert snapshot.notification_cursor == 0
        assert snapshot.notification_snapshots == {}

    asyncio.run(scenario())


def test_exception_and_timeout_keep_pending_without_advancing_cursor() -> None:
    async def scenario() -> None:
        registry = FakeRegistry()
        registry.error = RuntimeError("secret provider error")
        state, _, _, delivery = await setup(registry=registry)
        failed = await delivery.deliver(evaluation())
        assert failed.outcome == "pending"
        assert (await state.snapshot()).notification_cursor == 0

        timeout_registry = FakeRegistry()
        timeout_registry.wait_forever = True
        state, _, _, delivery = await setup(registry=timeout_registry)
        timed_out = await delivery.deliver(evaluation(notice_id="notice-timeout"))
        assert timed_out.outcome == "pending"
        assert (await state.snapshot()).notification_cursor == 0

    asyncio.run(scenario())


def test_missing_route_is_a_bounded_audit_and_does_not_block_cursor() -> None:
    async def scenario() -> None:
        state, _, registry, delivery = await setup()
        evaluation_without_route = evaluation(source_ref="src_missing")
        result = await delivery.deliver(evaluation_without_route)
        snapshot = await state.snapshot()

        assert result.outcome == "undeliverable"
        assert registry.calls == []
        assert snapshot.pending_notifications == []
        assert snapshot.notification_cursor == 4
        assert snapshot.undeliverable_notifications[0].reason_code == "route_missing"
        assert snapshot.undeliverable_notifications[0].notice_id == "notice-1"

    asyncio.run(scenario())


def test_only_pending_queue_head_can_call_message_push() -> None:
    async def scenario() -> None:
        registry = FakeRegistry("发送失败：temporary")
        state, _, registry, delivery = await setup(registry=registry)
        first = await delivery.deliver(evaluation())
        second = await delivery.deliver(evaluation(notice_id="notice-2", cursor=5))

        assert first.outcome == "pending"
        assert second.outcome == "deferred"
        assert [call[1]["_outbound_metadata"] for call in registry.calls] == [
            {"delivery_id": "notice-1"}
        ]
        assert [item.notice_id for item in await state.list_pending_notifications()] == [
            "notice-1",
            "notice-2",
        ]

    asyncio.run(scenario())


def test_crash_after_exact_receipt_leaves_pending_for_a_bounded_duplicate_retry() -> None:
    async def scenario() -> None:
        kv = MemoryKV()
        state = CrashAfterReceiptState(kv)
        await state.save_route(
            "src_private",
            PrivateRoute(
                channel="telegram",
                chat_id="chat-42",
                session_key="telegram:chat-42",
            ),
        )
        registry = FakeRegistry()
        delivery = NotificationDelivery(
            config(),
            state=state,
            tool_registry=registry,
            timeout_seconds=0.01,
        )
        with pytest.raises(RuntimeError, match="simulated crash window"):
            await delivery.deliver(evaluation())
        assert [item.notice_id for item in await state.list_pending_notifications()] == [
            "notice-1"
        ]

        result = await delivery.deliver(evaluation())
        assert result.outcome == "sent"
        assert len(registry.calls) == 2
        assert (await state.snapshot()).notification_cursor == 4

    asyncio.run(scenario())
