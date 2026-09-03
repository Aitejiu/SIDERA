from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from .client import (
    ControlPlaneRejected,
    ProjectionResyncRequired,
    ProjectionStreamDisconnected,
    TransportUnavailable,
)
from .config import TaskBridgeConfig
from .contracts import JsonValue, ProjectionChange
from .state import (
    BridgeStateStore,
    NotificationSnapshot,
    PendingNotification,
    ResourceSourceBinding,
    UndeliverableAudit,
)

type NoticeKind = Literal[
    "planning_clarification",
    "planning_proposal_ready",
    "planning_materialized",
    "plan_transition_degraded",
    "plan_transition_converged",
    "task_needs_user",
    "task_blocked",
    "task_failed",
    "task_milestone_completed",
    "task_completed",
]
type NotificationMode = Literal["live", "resync"]

logger = logging.getLogger(__name__)


class NotificationQueryUnavailable(RuntimeError):
    """The authoritative snapshot cannot be read yet and must be retried."""


class NotificationDeliveryPending(RuntimeError):
    """A pending notice could not settle, so its cursor must not advance."""


@dataclass(frozen=True, slots=True)
class NotificationNotice:
    notice_id: str
    cursor: int
    resource_key: str
    source_ref: str
    kind: NoticeKind
    message: str
    web_url: str


@dataclass(frozen=True, slots=True)
class NotificationEvaluation:
    resource_key: str
    source_ref: str
    cursor: int
    snapshot: NotificationSnapshot
    notice: NotificationNotice | None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    outcome: Literal["ignored", "sent", "pending", "undeliverable", "deferred"]
    notice_id: str | None


class NotificationDelivery:
    """Persist pending notices and deliver them serially through message_push."""

    def __init__(
        self,
        config: TaskBridgeConfig,
        *,
        state: BridgeStateStore,
        tool_registry: Any,
        clock: Any | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._tool_registry = tool_registry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timeout_seconds = timeout_seconds or float(
            getattr(config, "notification_delivery_timeout_seconds", 15.0)
        )

    async def deliver(
        self,
        evaluation: NotificationEvaluation,
        *,
        advance_cursor: bool = True,
    ) -> DeliveryResult:
        notice = evaluation.notice
        if notice is None:
            await self._state.save_notification_snapshot(
                evaluation.resource_key, evaluation.snapshot
            )
            if advance_cursor:
                await self._state.advance_notification_cursor(evaluation.cursor)
            return DeliveryResult("ignored", None)

        await self._state.enqueue_notification(
            PendingNotification(
                notice_id=notice.notice_id,
                cursor=notice.cursor,
                resource_key=notice.resource_key,
                source_ref=notice.source_ref,
                kind=notice.kind,
                message=notice.message,
                web_url=notice.web_url,
                created_at=self._clock(),
            )
        )
        pending = await self._state.list_pending_notifications()
        if not pending or pending[0].notice_id != notice.notice_id:
            return DeliveryResult("deferred", notice.notice_id)

        route = (await self._state.snapshot()).source_routes.get(notice.source_ref)
        if route is None:
            await self._state.record_undeliverable(
                UndeliverableAudit(
                    notice_id=notice.notice_id,
                    resource_key=notice.resource_key,
                    kind=notice.kind,
                    reason_code="route_missing",
                    recorded_at=self._clock(),
                )
            )
            await self._state.settle_notification(notice.notice_id)
            await self._state.save_notification_snapshot(
                evaluation.resource_key, evaluation.snapshot
            )
            if advance_cursor:
                await self._state.advance_notification_cursor(notice.cursor)
            return DeliveryResult("undeliverable", notice.notice_id)

        await self._state.record_notification_attempt(
            notice.notice_id, at=self._clock()
        )
        try:
            receipt = await asyncio.wait_for(
                self._tool_registry.execute(
                    "message_push",
                    {
                        "target_channel": route.channel,
                        "target_chat_id": route.chat_id,
                        "message": notice.message,
                    },
                    internal_arguments={
                        "_commit_role": "passive",
                        "_outbound_metadata": {
                            "delivery_id": notice.notice_id,
                            "_channel_commit_role": "passive",
                        },
                    },
                    raise_errors=True,
                ),
                timeout=self._timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - external delivery failures retain pending
            logger.warning(
                "通知出站失败 notice=%s error_type=%s error=%s",
                notice.notice_id,
                type(exc).__name__,
                str(exc)[:240],
            )
            return DeliveryResult("pending", notice.notice_id)
        if receipt != "消息已发送":
            return DeliveryResult("pending", notice.notice_id)

        await self._state.settle_notification(notice.notice_id)
        await self._state.save_notification_snapshot(
            evaluation.resource_key, evaluation.snapshot
        )
        if advance_cursor:
            await self._state.advance_notification_cursor(notice.cursor)
        return DeliveryResult("sent", notice.notice_id)


class NotificationRunner:
    """Run the private notification stream and current-fact recovery loop."""

    def __init__(
        self,
        config: TaskBridgeConfig,
        *,
        client: Any,
        state: BridgeStateStore,
        evaluator: NotificationEvaluator,
        delivery: NotificationDelivery,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._state = state
        self._evaluator = evaluator
        self._delivery = delivery
        self._sleeper = sleeper or asyncio.sleep
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if not await self._has_private_binding():
                await self._wait_for_state_change()
                failures = 0
                continue
            try:
                await self._drain_pending(advance_cursor=True)
                after = (await self._state.snapshot()).notification_cursor
                async for change in self._client.iter_projection_changes(after=after):
                    if self._stop.is_set():
                        return
                    evaluation = await self._evaluator.evaluate(change, mode="live")
                    if evaluation is None:
                        await self._state.advance_notification_cursor(change.cursor)
                    else:
                        self._require_delivery(
                            await self._delivery.deliver(evaluation)
                        )
                    failures = 0
                raise ProjectionStreamDisconnected(after)
            except ProjectionResyncRequired:
                try:
                    await self.resync_current_facts()
                    failures = 0
                except (
                    NotificationQueryUnavailable,
                    NotificationDeliveryPending,
                    ProjectionStreamDisconnected,
                ):
                    failures = await self._backoff(failures)
            except (
                NotificationQueryUnavailable,
                NotificationDeliveryPending,
                ProjectionStreamDisconnected,
            ):
                failures = await self._backoff(failures)

    async def resync_current_facts(self) -> int:
        """Re-evaluate owned resources, advancing only after the batch succeeds."""

        await self._drain_pending(advance_cursor=False)
        initial = await self._state.snapshot()
        max_cursor = initial.notification_cursor
        seed_cursor = max(1, initial.notification_cursor)

        for binding in await self._state.list_private_planning_bindings():
            evaluation = await self._evaluator.evaluate(
                _synthetic_change(
                    workspace_id=self._config.workspace_id,
                    cursor=seed_cursor,
                    projection_type="planning_request",
                    resource_id=binding.resource_id,
                ),
                mode="resync",
            )
            if evaluation is None:
                continue
            max_cursor = max(max_cursor, evaluation.cursor)
            self._require_delivery(
                await self._delivery.deliver(evaluation, advance_cursor=False)
            )

        resources = (
            await self._state.snapshot()
        ).resource_sources.values()
        task_bindings = sorted(
            (
                binding
                for binding in resources
                if binding.resource_kind == "task"
                and binding.source_type == "akashic_private"
            ),
            key=lambda item: str(item.resource_id),
        )
        for binding in task_bindings:
            evaluation = await self._evaluator.evaluate(
                _synthetic_change(
                    workspace_id=self._config.workspace_id,
                    cursor=seed_cursor,
                    projection_type="task_overview",
                    resource_id=binding.resource_id,
                ),
                mode="resync",
            )
            if evaluation is not None:
                max_cursor = max(max_cursor, evaluation.cursor)
                self._require_delivery(
                    await self._delivery.deliver(evaluation, advance_cursor=False)
                )

            list_transitions = getattr(self._client, "list_plan_transitions", None)
            if callable(list_transitions):
                transitions = await cast(
                    Callable[
                        [UUID], Awaitable[tuple[dict[str, JsonValue], ...]]
                    ],
                    list_transitions,
                )(binding.resource_id)
                for transition in transitions:
                    transition_id = _uuid_value(transition.get("plan_transition_id"))
                    if transition_id is None:
                        continue
                    transition_evaluation = await self._evaluator.evaluate(
                        _synthetic_change(
                            workspace_id=self._config.workspace_id,
                            cursor=seed_cursor,
                            projection_type="plan_transition",
                            resource_id=transition_id,
                        ),
                        mode="resync",
                    )
                    if transition_evaluation is None:
                        continue
                    max_cursor = max(max_cursor, transition_evaluation.cursor)
                    self._require_delivery(
                        await self._delivery.deliver(
                            transition_evaluation, advance_cursor=False
                        )
                    )

        await self._state.advance_notification_cursor(max_cursor)
        return max_cursor

    async def _drain_pending(self, *, advance_cursor: bool) -> None:
        while True:
            pending = await self._state.list_pending_notifications()
            if not pending:
                return
            head = pending[0]
            evaluation = await self._evaluation_for_pending(head)
            result = await self._delivery.deliver(
                evaluation,
                advance_cursor=advance_cursor,
            )
            self._require_delivery(result)

    async def _evaluation_for_pending(
        self, pending: PendingNotification
    ) -> NotificationEvaluation:
        kind, _, resource_id_value = pending.resource_key.partition(":")
        try:
            resource_id = UUID(resource_id_value)
        except ValueError:
            resource_id = UUID(int=0)
        projection_type = {
            "planning_request": "planning_request",
            "plan_transition": "plan_transition",
            "task": "task_overview",
        }.get(kind, "task_overview")
        current = await self._evaluator.evaluate(
            _synthetic_change(
                workspace_id=self._config.workspace_id,
                cursor=max(1, pending.cursor),
                projection_type=projection_type,
                resource_id=resource_id,
            ),
            mode="resync",
        )
        snapshot = (
            current.snapshot
            if current is not None
            else NotificationSnapshot(status=pending.kind)
        )
        notice = NotificationNotice(
            notice_id=pending.notice_id,
            cursor=pending.cursor,
            resource_key=pending.resource_key,
            source_ref=pending.source_ref,
            kind=pending.kind,  # type: ignore[arg-type]
            message=pending.message,
            web_url=pending.web_url or self._config.web_base_url.__str__(),
        )
        return NotificationEvaluation(
            resource_key=pending.resource_key,
            source_ref=pending.source_ref,
            cursor=pending.cursor,
            snapshot=snapshot,
            notice=notice,
        )

    async def _has_private_binding(self) -> bool:
        return bool(await self._state.list_private_planning_bindings()) or any(
            binding.source_type == "akashic_private"
            and binding.resource_kind == "task"
            for binding in (await self._state.snapshot()).resource_sources.values()
        )

    async def _wait_for_state_change(self) -> None:
        change_task = asyncio.create_task(self._state.wait_for_change())
        stop_task = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            (change_task, stop_task), return_when=asyncio.FIRST_COMPLETED
        )
        del done
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(
                *(cast(Awaitable[object], task) for task in pending),
                return_exceptions=True,
            )

    async def _backoff(self, failures: int) -> int:
        delay = min(
            self._config.notification_retry_base_seconds * (2**failures),
            self._config.notification_retry_max_seconds,
        )
        await self._wait_delay(delay)
        return failures + 1

    async def _wait_delay(self, delay: float) -> None:
        async def wait_for_delay() -> None:
            await self._sleeper(delay)

        sleep_task = asyncio.create_task(wait_for_delay())
        stop_task = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            (sleep_task, stop_task), return_when=asyncio.FIRST_COMPLETED
        )
        del done
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(
                *(cast(Awaitable[object], task) for task in pending),
                return_exceptions=True,
            )

    @staticmethod
    def _require_delivery(result: DeliveryResult) -> None:
        if result.outcome in {"pending", "deferred"}:
            raise NotificationDeliveryPending("notification_delivery_pending")


@dataclass(frozen=True, slots=True)
class _Fact:
    kind: NoticeKind | None
    version: int | None
    fingerprint: str
    snapshot: NotificationSnapshot
    title: str
    subtitle: str | None
    fact_text: str | None
    next_step: str
    web_url: str


class NotificationEvaluator:
    """Re-evaluate safe notices from authoritative Control Plane queries."""

    def __init__(
        self,
        config: TaskBridgeConfig,
        *,
        client: Any,
        state: BridgeStateStore,
    ) -> None:
        self._config = config
        self._client = client
        self._state = state

    async def evaluate(
        self,
        change: ProjectionChange,
        *,
        mode: NotificationMode,
    ) -> NotificationEvaluation | None:
        if change.workspace_id != self._config.workspace_id:
            return None
        if change.resource_id is None:
            return None
        if mode not in {"live", "resync"}:
            raise ValueError("unsupported notification evaluation mode")
        if change.projection_type == "planning_request":
            return await self._evaluate_planning(change, mode=mode)
        if change.projection_type == "task_overview":
            return await self._evaluate_task(change, mode=mode)
        if change.projection_type == "plan_transition":
            return await self._evaluate_transition(change, mode=mode)
        return None

    async def _evaluate_planning(
        self,
        change: ProjectionChange,
        *,
        mode: NotificationMode,
    ) -> NotificationEvaluation | None:
        request_id = change.resource_id
        assert request_id is not None
        resource_key = f"planning_request:{request_id}"
        binding = await self._private_binding("planning_request", request_id)
        if binding is None:
            return None
        detail = await self._read_planning_detail(request_id)
        if detail is None:
            return None
        if detail.get("source_type") not in {
            None,
            "akashic_private",
            "plan_revision",
        }:
            return None
        if not _owner_matches(detail, binding):
            raise ValueError("source_binding_mismatch")

        status = _text(detail.get("status")) or "unknown"
        version = _non_negative_int(detail.get("version"), change.resource_version)
        cursor = _cursor_value(detail.get("last_change_cursor"), change.cursor)
        root_task_id = _revision_root_id(detail)
        is_revision = (
            (_text(detail.get("kind")) or "").lower() == "revision"
            or root_task_id is not None
        )
        web_url = (
            self._revision_url(request_id, root_task_id)
            if is_revision
            else self._planning_url(request_id)
        )
        previous = (await self._state.snapshot()).notification_snapshots.get(resource_key)
        affected_branch_paths: tuple[str, ...] = ()
        if is_revision and status.lower() in {
            "proposal_ready",
            "awaiting_confirmation",
            "needs_confirmation",
        }:
            diff = await self._read_revision_diff(request_id)
            affected_branch_paths = _branch_paths(diff)
        fact = await self._planning_fact(
            detail,
            request_id=request_id,
            status=status,
            version=version,
            web_url=web_url,
            affected_branch_paths=affected_branch_paths,
        )
        if fact.kind == "planning_materialized":
            materialization = await self._read_materialization(request_id)
            root_task_id = _uuid_value(materialization.get("root_task_id"))
            if root_task_id is None:
                fact = _replace_fact(fact, kind=None)
            else:
                await self._state.bind_materialized_task(request_id, root_task_id)
                fact = _replace_fact(
                    fact,
                    web_url=self._task_url(root_task_id),
                    subtitle="任务已生成",
                )
        notice = _notice_for_fact(
            fact,
            previous=previous,
            source_ref=binding.source_ref,
            cursor=cursor,
            resource_key=resource_key,
            mode=mode,
        )
        return NotificationEvaluation(
            resource_key=resource_key,
            source_ref=binding.source_ref,
            cursor=cursor,
            snapshot=fact.snapshot,
            notice=notice,
        )

    async def _evaluate_transition(
        self,
        change: ProjectionChange,
        *,
        mode: NotificationMode,
    ) -> NotificationEvaluation | None:
        transition_id = change.resource_id
        assert transition_id is not None
        transition = await self._read_plan_transition(transition_id)
        if transition is None:
            return None
        root_task_id = _transition_root_id(transition)
        if root_task_id is None:
            return None
        binding = await self._private_binding("task", root_task_id)
        if binding is None:
            return None
        status = _text(transition.get("status")) or "unknown"
        version = _non_negative_int(transition.get("version"), change.resource_version)
        resource_key = f"plan_transition:{transition_id}"
        previous = (await self._state.snapshot()).notification_snapshots.get(resource_key)
        fact = _transition_fact(
            transition,
            status=status,
            version=version,
            web_url=self._task_url(root_task_id),
        )
        notice = _notice_for_fact(
            fact,
            previous=previous,
            source_ref=binding.source_ref,
            cursor=change.cursor,
            resource_key=resource_key,
            mode=mode,
        )
        return NotificationEvaluation(
            resource_key=resource_key,
            source_ref=binding.source_ref,
            cursor=change.cursor,
            snapshot=fact.snapshot,
            notice=notice,
        )

    async def _evaluate_task(
        self,
        change: ProjectionChange,
        *,
        mode: NotificationMode,
    ) -> NotificationEvaluation | None:
        task_id = change.resource_id
        assert task_id is not None
        resource_key = f"task:{task_id}"
        binding = await self._private_binding("task", task_id)
        if binding is None:
            return None
        overview = await self._read_task_overview(task_id)
        if overview is None:
            return None
        previous = (await self._state.snapshot()).notification_snapshots.get(resource_key)
        cursor = _cursor_value(overview.get("last_change_cursor"), change.cursor)
        fact = _task_fact(
            overview,
            task_id=task_id,
            change=change,
            web_url=self._task_url(task_id),
            mode=mode,
            previous=previous,
        )
        notice = _notice_for_fact(
            fact,
            previous=previous,
            source_ref=binding.source_ref,
            cursor=cursor,
            resource_key=resource_key,
            mode=mode,
        )
        return NotificationEvaluation(
            resource_key=resource_key,
            source_ref=binding.source_ref,
            cursor=cursor,
            snapshot=fact.snapshot,
            notice=notice,
        )

    async def _private_binding(
        self, kind: Literal["planning_request", "task"], resource_id: UUID
    ) -> ResourceSourceBinding | None:
        binding = (await self._state.snapshot()).resource_sources.get(
            f"{kind}:{resource_id}"
        )
        if binding is None or binding.source_type != "akashic_private":
            return None
        return binding

    async def _read_planning_detail(self, request_id: UUID) -> dict[str, Any] | None:
        try:
            return await self._client.get_planning_detail(request_id)
        except TransportUnavailable:
            raise NotificationQueryUnavailable from None
        except ControlPlaneRejected as exc:
            if exc.code == "not_found":
                return None
            raise

    async def _read_materialization(self, request_id: UUID) -> dict[str, Any]:
        try:
            return await self._client.get_materialization(request_id)
        except TransportUnavailable:
            raise NotificationQueryUnavailable from None
        except ControlPlaneRejected as exc:
            if exc.code == "not_found":
                return {}
            raise

    async def _read_task_overview(self, task_id: UUID) -> dict[str, Any] | None:
        try:
            return await self._client.get_task_overview(task_id)
        except TransportUnavailable:
            raise NotificationQueryUnavailable from None
        except ControlPlaneRejected as exc:
            if exc.code == "not_found":
                return None
            raise

    async def _read_revision_diff(self, request_id: UUID) -> dict[str, Any]:
        reader = getattr(self._client, "get_revision_diff", None)
        if not callable(reader):
            return {}
        try:
            return await cast(
                Callable[[UUID], Awaitable[dict[str, JsonValue]]], reader
            )(request_id)
        except TransportUnavailable:
            raise NotificationQueryUnavailable from None
        except ControlPlaneRejected as exc:
            if exc.code == "not_found":
                return {}
            raise

    async def _read_plan_transition(
        self, transition_id: UUID
    ) -> dict[str, Any] | None:
        try:
            return await self._client.get_plan_transition(transition_id)
        except TransportUnavailable:
            raise NotificationQueryUnavailable from None
        except ControlPlaneRejected as exc:
            if exc.code == "not_found":
                return None
            raise

    async def _planning_fact(
        self,
        detail: Mapping[str, Any],
        *,
        request_id: UUID,
        status: str,
        version: int | None,
        web_url: str,
        affected_branch_paths: tuple[str, ...] = (),
    ) -> _Fact:
        normalized = status.lower()
        title = _text(detail.get("title")) or "任务规划"
        if normalized in {"needs_clarification", "clarification_required"}:
            clarifications = detail.get("pending_clarifications")
            if not isinstance(clarifications, list):
                clarifications = list(await self._client.list_clarifications(request_id))
            blocking = [
                item
                for item in clarifications
                if isinstance(item, Mapping) and item.get("blocking", True)
            ]
            if blocking:
                fingerprint = _fingerprint(
                    [
                        (
                            _text(item.get("id")),
                            _non_negative_int(item.get("version"), None),
                        )
                        for item in blocking
                    ]
                )
                snapshot = NotificationSnapshot(
                    status="planning_clarification",
                    version=version,
                    attention_fingerprint=fingerprint,
                )
                return _Fact(
                    kind="planning_clarification",
                    version=version,
                    fingerprint=fingerprint,
                    snapshot=snapshot,
                    title=title,
                    subtitle=None,
                    fact_text=f"规划需要回答 {len(blocking)} 个阻塞问题。",
                    next_step="回答阻塞问题",
                    web_url=web_url,
                )
        if normalized in {
            "proposal_ready",
            "awaiting_confirmation",
            "needs_confirmation",
        }:
            fingerprint = _fingerprint(
                [
                    normalized,
                    _non_negative_int(detail.get("current_proposal_revision"), None),
                    _text(detail.get("current_proposal_content_hash")),
                ]
            )
            snapshot = NotificationSnapshot(
                status="planning_proposal_ready",
                version=version,
                attention_fingerprint=fingerprint,
            )
            return _Fact(
                kind="planning_proposal_ready",
                version=version,
                fingerprint=fingerprint,
                snapshot=snapshot,
                title=title,
                subtitle=(
                    f"涉及分支：{_branch_summary(affected_branch_paths)}"
                    if affected_branch_paths
                    else None
                ),
                fact_text="新的 Proposal 已准备好，等待你的确认。",
                next_step="查看并确认方案",
                web_url=web_url,
            )
        if normalized == "materialized":
            fingerprint = _fingerprint(
                [normalized, _text(detail.get("current_proposal_content_hash"))]
            )
            snapshot = NotificationSnapshot(
                status="planning_materialized",
                version=version,
                attention_fingerprint=fingerprint,
            )
            return _Fact(
                kind="planning_materialized",
                version=version,
                fingerprint=fingerprint,
                snapshot=snapshot,
                title=title,
                subtitle=None,
                fact_text="规划已物化为 Task，可以开始执行。",
                next_step="打开 Task 查看执行状态",
                web_url=web_url,
            )
        snapshot = NotificationSnapshot(status=normalized, version=version)
        return _Fact(
            kind=None,
            version=version,
            fingerprint=_fingerprint([normalized, version]),
            snapshot=snapshot,
            title=title,
            subtitle=None,
            fact_text=None,
            next_step="可在 SIDERA 查看",
            web_url=web_url,
        )

    def _planning_url(self, request_id: UUID) -> str:
        return (
            f"{str(self._config.web_base_url).rstrip('/')}/workspaces/"
            f"{self._config.workspace_id}/planning/{request_id}"
        )

    def _task_url(self, task_id: UUID) -> str:
        return (
            f"{str(self._config.web_base_url).rstrip('/')}/workspaces/"
            f"{self._config.workspace_id}/tasks/{task_id}"
        )

    def _revision_url(self, request_id: UUID, root_task_id: UUID | None) -> str:
        if root_task_id is None:
            return self._planning_url(request_id)
        return (
            f"{str(self._config.web_base_url).rstrip('/')}/workspaces/"
            f"{self._config.workspace_id}/tasks/{root_task_id}/revisions/{request_id}"
        )


def _task_fact(
    overview: Mapping[str, Any],
    *,
    task_id: UUID,
    change: ProjectionChange,
    web_url: str,
    mode: NotificationMode,
    previous: NotificationSnapshot | None,
) -> _Fact:
    title = _text(overview.get("title")) or "任务"
    version = _non_negative_int(
        overview.get("generation"), change.resource_version
    )
    attention_items = overview.get("attention_items")
    if not isinstance(attention_items, list):
        attention_items = []
    normalized_attention = [
        item for item in attention_items if isinstance(item, Mapping)
    ]
    attention_kind = _attention_kind(normalized_attention, overview)
    attention_fingerprint = (
        _fingerprint(
            [
                (
                    _text(item.get("kind")),
                    _text(item.get("task_title")),
                    _text(item.get("subtask_title")),
                    _text(item.get("reason")),
                    _text(item.get("resource_id")),
                    _non_negative_int(item.get("resource_version"), None),
                    bool(item.get("requires_operator")),
                )
                for item in normalized_attention
            ]
        )
        if normalized_attention
        else None
    )
    milestone_items = overview.get("milestones")
    if not isinstance(milestone_items, list):
        milestone_items = []
    completed_milestones = [
        (
            _text(item.get("key")),
            _text(item.get("title")),
        )
        for item in milestone_items
        if isinstance(item, Mapping)
        and (_text(item.get("state")) or "").lower()
        in {"completed", "succeeded", "complete"}
    ]
    milestone_fingerprint = (
        _fingerprint(completed_milestones) if completed_milestones else None
    )
    root_completed = _root_completed(overview)
    status = "completed" if root_completed else "running"
    kind: NoticeKind | None = None
    fact_text: str | None = None
    subtitle: str | None = None
    next_step = "可在 SIDERA 查看"
    fingerprint = _fingerprint([status, version])
    if attention_kind is not None and attention_fingerprint is not None:
        kind = attention_kind
        fingerprint = attention_fingerprint
        item = _best_attention_item(normalized_attention, attention_kind)
        subtitle = _text(item.get("subtask_title")) or None
        reason = _text(item.get("reason")) or "任务需要处理。"
        fact_text = reason
        next_step = "打开任务查看需要处理的事项"
    elif root_completed and (previous is None or previous.status != "completed"):
        kind = "task_completed"
        fingerprint = _fingerprint(["root_completed", version])
        fact_text = "根任务已完成。"
        next_step = "查看任务交付结果"
    elif (
        mode == "live"
        and milestone_fingerprint is not None
        and previous is not None
        and previous.milestone_fingerprint != milestone_fingerprint
    ):
        kind = "task_milestone_completed"
        fingerprint = milestone_fingerprint
        completed_key, completed_title = completed_milestones[-1]
        del completed_key
        subtitle = completed_title or None
        fact_text = "一个里程碑已完成。"
        next_step = "查看任务后续进展"
    snapshot = NotificationSnapshot(
        status=status if kind is None else kind,
        version=version,
        milestone_fingerprint=milestone_fingerprint,
        attention_fingerprint=attention_fingerprint,
    )
    return _Fact(
        kind=kind,
        version=version,
        fingerprint=fingerprint,
        snapshot=snapshot,
        title=title,
        subtitle=subtitle,
        fact_text=fact_text,
        next_step=next_step,
        web_url=web_url,
    )


def _notice_for_fact(
    fact: _Fact,
    *,
    previous: NotificationSnapshot | None,
    source_ref: str,
    cursor: int,
    resource_key: str,
    mode: NotificationMode,
) -> NotificationNotice | None:
    del mode
    if fact.kind is None or fact.fact_text is None:
        return None
    if previous is not None and _snapshot_fingerprint(previous) == _snapshot_fingerprint(
        fact.snapshot
    ):
        return None
    fact_version = fact.version if fact.version is not None else 0
    notice_id = hashlib.sha256(
        "|".join(
            [
                source_ref,
                fact.kind,
                resource_key,
                str(fact_version),
                fact.fingerprint,
            ]
        ).encode()
    ).hexdigest()
    lines = [fact.title]
    if fact.subtitle:
        lines.append(fact.subtitle)
    lines.append(fact.fact_text)
    lines.append(f"下一步：{fact.next_step}")
    lines.append(fact.web_url)
    message = _bounded_message(lines)
    return NotificationNotice(
        notice_id=notice_id,
        cursor=cursor,
        resource_key=resource_key,
        source_ref=source_ref,
        kind=fact.kind,
        message=message,
        web_url=fact.web_url,
    )


def _replace_fact(fact: _Fact, **updates: object) -> _Fact:
    values = {
        "kind": fact.kind,
        "version": fact.version,
        "fingerprint": fact.fingerprint,
        "snapshot": fact.snapshot,
        "title": fact.title,
        "subtitle": fact.subtitle,
        "fact_text": fact.fact_text,
        "next_step": fact.next_step,
        "web_url": fact.web_url,
    }
    values.update(updates)
    return _Fact(**values)


def _attention_kind(
    items: list[Mapping[str, Any]], overview: Mapping[str, Any]
) -> NoticeKind | None:
    values = [(_text(item.get("kind")) or "").lower() for item in items]
    if (_non_negative_int(overview.get("failed_subtask_count"), 0) or 0) > 0 or any(
        "fail" in value for value in values
    ):
        return "task_failed"
    if any("block" in value for value in values):
        return "task_blocked"
    if any(
        "wait" in value or "user" in value or "decision" in value
        for value in values
    ) or any(bool(item.get("requires_operator")) for item in items):
        return "task_needs_user"
    return None


def _best_attention_item(
    items: list[Mapping[str, Any]], kind: NoticeKind
) -> Mapping[str, Any]:
    if kind == "task_failed":
        priority = "fail"
    elif kind == "task_blocked":
        priority = "block"
    else:
        priority = ""
    for item in items:
        value = (_text(item.get("kind")) or "").lower()
        if priority and priority in value:
            return item
    return items[0] if items else {}


def _root_completed(overview: Mapping[str, Any]) -> bool:
    state = (_text(overview.get("status")) or _text(overview.get("state")) or "").lower()
    if state in {"completed", "succeeded", "complete"}:
        return True
    total = _non_negative_int(overview.get("subtask_count"), 0) or 0
    completed = _non_negative_int(overview.get("completed_subtask_count"), 0) or 0
    failed = _non_negative_int(overview.get("failed_subtask_count"), 0) or 0
    return total > 0 and completed >= total and failed == 0


def _owner_matches(
    detail: Mapping[str, Any], binding: ResourceSourceBinding
) -> bool:
    source_type = detail.get("source_type")
    source_ref = detail.get("source_ref")
    if source_type == "plan_revision":
        # Revision requests deliberately use the command id as their CP source
        # ref. The durable bridge binding remains the authority for the user route.
        return True
    if source_type is not None and source_type != binding.source_type:
        return False
    return source_ref is None or source_ref == binding.source_ref


def _revision_root_id(detail: Mapping[str, Any]) -> UUID | None:
    candidates: list[object] = [detail]
    for key in ("revision_base", "revision_freshness", "base"):
        nested = detail.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
            nested_base = nested.get("base")
            if isinstance(nested_base, Mapping):
                candidates.append(nested_base)
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            value = candidate.get("root_task_id")
            if isinstance(value, UUID):
                return value
            if isinstance(value, str):
                try:
                    return UUID(value)
                except ValueError:
                    continue
    return None


def _branch_paths(diff: Mapping[str, Any]) -> tuple[str, ...]:
    values = diff.get("affected_branch_paths")
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(
        path
        for path in values[:3]
        if isinstance(path, str) and _safe_text(path, 240)
    )


def _branch_summary(paths: tuple[str, ...]) -> str:
    if len(paths) <= 2:
        return "；".join(paths)
    return f"{paths[0]}；{paths[1]} 等 {len(paths)} 条分支"


def _transition_root_id(transition: Mapping[str, Any]) -> UUID | None:
    root = transition.get("root_task")
    if isinstance(root, Mapping):
        root_id = _uuid_value(root.get("id"))
        if root_id is not None:
            return root_id
    return _uuid_value(transition.get("root_task_id"))


def _transition_path(
    transition: Mapping[str, Any], *, degraded: bool
) -> str | None:
    workers = transition.get("workers")
    if not isinstance(workers, (list, tuple)):
        return None
    candidates = [item for item in workers if isinstance(item, Mapping)]
    if degraded:
        candidates = [
            item
            for item in candidates
            if _text(item.get("status")) in {"failed", "blocked_unknown"}
        ] or candidates
    for worker in candidates:
        path = _text(worker.get("path"))
        if path is not None:
            return path
        worker_item = worker.get("worker_item")
        if isinstance(worker_item, Mapping):
            path = _text(worker_item.get("path"))
            if path is not None:
                return path
    return None


def _transition_fact(
    transition: Mapping[str, Any],
    *,
    status: str,
    version: int | None,
    web_url: str,
) -> _Fact:
    normalized = status.lower()
    root = transition.get("root_task")
    title = (
        _text(root.get("title"))
        if isinstance(root, Mapping)
        else None
    ) or "计划切换"
    path = _transition_path(transition, degraded=normalized == "degraded")
    worker_fingerprint = []
    workers = transition.get("workers")
    if isinstance(workers, (list, tuple)):
        worker_fingerprint = [
            (
                _text(item.get("path")),
                _text(item.get("status")),
                _text(item.get("failure_code")),
            )
            for item in workers
            if isinstance(item, Mapping)
        ]
    kind: NoticeKind | None = None
    fact_text: str | None = None
    next_step = "打开任务查看收敛结果"
    if normalized == "degraded":
        kind = "plan_transition_degraded"
        fact_text = "计划切换出现降级，受影响分支保持可见并等待处理。"
        next_step = "打开任务查看并处理受影响分支"
    elif normalized == "converged":
        kind = "plan_transition_converged"
        fact_text = "计划切换已完成，受影响分支已经收敛。"
    snapshot = NotificationSnapshot(
        status=normalized if kind is None else kind,
        version=version,
        attention_fingerprint=_fingerprint([normalized, worker_fingerprint]),
    )
    return _Fact(
        kind=kind,
        version=version,
        fingerprint=_fingerprint([normalized, version, worker_fingerprint]),
        snapshot=snapshot,
        title=title,
        subtitle=f"涉及分支：{path}" if path is not None else None,
        fact_text=fact_text,
        next_step=next_step,
        web_url=web_url,
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _snapshot_fingerprint(snapshot: NotificationSnapshot) -> str:
    return _fingerprint(snapshot.model_dump(mode="json"))


def _bounded_message(lines: list[str]) -> str:
    message = "\n".join(_safe_text(line, 400) for line in lines if line)
    if len(message) <= 1200:
        return message
    return f"{message[:1197].rstrip()}..."


def _safe_text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    return cleaned[:limit]


def _text(value: object) -> str | None:
    result = _safe_text(value)
    return result or None


def _non_negative_int(value: object, fallback: int | None) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def _uuid_value(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _cursor_value(value: object, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return max(1, fallback)


def _synthetic_change(
    *,
    workspace_id: UUID,
    cursor: int,
    projection_type: str,
    resource_id: UUID,
) -> ProjectionChange:
    return ProjectionChange(
        workspace_id=workspace_id,
        cursor=max(1, cursor),
        schema_version=1,
        projection_type=projection_type,
        resource_id=resource_id,
        change_type="resync",
        resource_version=None,
        occurred_at=datetime.now(UTC),
        correlation_id=UUID(int=0),
    )
