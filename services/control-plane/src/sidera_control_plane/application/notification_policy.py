from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.notifications import (
    NotificationAttemptErrorCode,
    NotificationFact,
    NotificationKind,
    NotificationPolicyState,
    NotificationPriority,
    NotificationThreadBusinessStatus,
    NotificationTransition,
    normalize_notification_text,
)

POLICY_VERSION = 1
DEFAULT_BLOCKED_GRACE_SECONDS = 300
MIN_BLOCKED_GRACE_SECONDS = 60
MAX_BLOCKED_GRACE_SECONDS = 86_400

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_MARKER_PATTERN = re.compile(
    r"(?:api[_ -]?key|authorization|bearer|password|secret|token)", re.IGNORECASE
)


class NotificationEvaluationMode(StrEnum):
    LIVE = "live"
    RESYNC = "resync"


class NotificationRouteEligibility(StrEnum):
    WEB_ONLY = "web_only"
    ORIGIN_IF_ACTIVE = "origin_if_active"


class NotificationDueStatus(StrEnum):
    NOT_DUE = "not_due"
    DUE = "due"


@dataclass(frozen=True, slots=True)
class NotificationMilestone:
    key: str
    title: str
    completed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _safe_text(self.key, "milestone key", 256))
        object.__setattr__(
            self,
            "title",
            _safe_text(self.title, "milestone title", 512),
        )
        _boolean(self.completed, "milestone completed")

    def to_mapping(self) -> dict[str, object]:
        return {"key": self.key, "title": self.title, "completed": self.completed}


@dataclass(frozen=True, slots=True)
class NotificationAttention:
    kind: str
    resource_id: UUID
    resource_version: int
    requires_operator: bool
    safe_path: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _safe_text(self.kind, "attention kind", 256))
        _uuid(self.resource_id, "attention resource_id")
        _positive(self.resource_version, "attention resource_version")
        _boolean(self.requires_operator, "attention requires_operator")
        object.__setattr__(
            self,
            "safe_path",
            _text_tuple(self.safe_path, "attention safe_path", 512),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "resource_id": str(self.resource_id),
            "resource_version": self.resource_version,
            "requires_operator": self.requires_operator,
            "safe_path": list(self.safe_path),
        }


@dataclass(frozen=True, slots=True)
class NotificationObservation:
    workspace_id: UUID
    recipient_type: str
    recipient_id: str
    subject_type: str
    subject_id: str
    topic: str
    root_task_id: UUID
    subtask_id: UUID | None
    worker_item_id: UUID | None
    source_projection_cursor: int
    source_projection_type: str
    source_resource_id: UUID
    source_resource_version: int
    kind: NotificationKind | str
    observed_status: str
    authoritative_fingerprint: str
    root_task_title: str
    subtask_path: tuple[str, ...]
    worker_item_title: str | None
    deep_link: str
    correlation_id: str
    transition: NotificationTransition | None = None
    requires_operator: bool = False
    automatic_recovery: bool = False
    baseline_import: bool = False
    source_type: str = "sidera_web_chat"
    origin_route_active: bool = False
    completed_milestones: tuple[NotificationMilestone, ...] = ()
    attention_items: tuple[NotificationAttention, ...] = ()

    def __post_init__(self) -> None:
        _uuid(self.workspace_id, "observation workspace_id")
        object.__setattr__(
            self,
            "recipient_type",
            _safe_text(self.recipient_type, "observation recipient_type", 256),
        )
        object.__setattr__(
            self,
            "recipient_id",
            _safe_text(self.recipient_id, "observation recipient_id", 512),
        )
        object.__setattr__(
            self,
            "subject_type",
            _safe_text(self.subject_type, "observation subject_type", 256),
        )
        object.__setattr__(
            self,
            "subject_id",
            _safe_text(self.subject_id, "observation subject_id", 512),
        )
        object.__setattr__(self, "topic", _safe_text(self.topic, "observation topic"))
        _uuid(self.root_task_id, "observation root_task_id")
        _optional_uuid(self.subtask_id, "observation subtask_id")
        _optional_uuid(self.worker_item_id, "observation worker_item_id")
        _nonnegative(
            self.source_projection_cursor, "observation source_projection_cursor"
        )
        object.__setattr__(
            self,
            "source_projection_type",
            _safe_identifier(
                self.source_projection_type,
                "observation source_projection_type",
            ),
        )
        _uuid(self.source_resource_id, "observation source_resource_id")
        _positive(
            self.source_resource_version, "observation source_resource_version"
        )
        object.__setattr__(
            self,
            "kind",
            _safe_identifier(
                self.kind.value if isinstance(self.kind, NotificationKind) else self.kind,
                "observation kind",
            ),
        )
        object.__setattr__(
            self,
            "observed_status",
            _safe_text(self.observed_status, "observation observed_status", 256),
        )
        _hash(self.authoritative_fingerprint, "observation authoritative_fingerprint")
        object.__setattr__(
            self,
            "root_task_title",
            _safe_text(self.root_task_title, "observation root_task_title"),
        )
        object.__setattr__(
            self,
            "subtask_path",
            _text_tuple(self.subtask_path, "observation subtask_path"),
        )
        if self.worker_item_title is not None:
            object.__setattr__(
                self,
                "worker_item_title",
                _safe_text(
                    self.worker_item_title,
                    "observation worker_item_title",
                ),
            )
        object.__setattr__(
            self,
            "deep_link",
            _safe_text(self.deep_link, "observation deep_link"),
        )
        object.__setattr__(
            self,
            "correlation_id",
            _safe_text(self.correlation_id, "observation correlation_id", 512),
        )
        if self.transition is not None:
            object.__setattr__(
                self,
                "transition",
                _enum(self.transition, NotificationTransition, "observation transition"),
            )
        for name in (
            "requires_operator",
            "automatic_recovery",
            "baseline_import",
            "origin_route_active",
        ):
            _boolean(getattr(self, name), f"observation {name}")
        milestones = tuple(self.completed_milestones)
        if any(not isinstance(item, NotificationMilestone) for item in milestones):
            raise ValidationError("observation contains an invalid milestone")
        milestone_keys = tuple(item.key for item in milestones)
        if len(set(milestone_keys)) != len(milestone_keys):
            raise ValidationError("observation milestone keys must be unique")
        object.__setattr__(self, "completed_milestones", milestones)
        attention_items = tuple(self.attention_items)
        if any(not isinstance(item, NotificationAttention) for item in attention_items):
            raise ValidationError("observation contains an invalid attention")
        object.__setattr__(self, "attention_items", attention_items)
        object.__setattr__(
            self,
            "source_type",
            _safe_identifier(self.source_type, "observation source_type"),
        )


@dataclass(frozen=True, slots=True)
class NotificationDueDecision:
    status: NotificationDueStatus
    not_before: datetime | None
    reason: str


@dataclass(frozen=True, slots=True)
class NotificationPolicyMutation:
    policy_state: NotificationPolicyState
    fact: NotificationFact | None
    thread_business_status: NotificationThreadBusinessStatus | None
    route_eligibility: NotificationRouteEligibility
    external_delivery_forbidden: bool
    due: NotificationDueDecision
    reason: str


class NotificationPolicyV1:
    policy_version = POLICY_VERSION

    def __init__(
        self, *, blocked_grace_seconds: int = DEFAULT_BLOCKED_GRACE_SECONDS
    ) -> None:
        if (
            not isinstance(blocked_grace_seconds, int)
            or isinstance(blocked_grace_seconds, bool)
            or not MIN_BLOCKED_GRACE_SECONDS <= blocked_grace_seconds <= MAX_BLOCKED_GRACE_SECONDS
        ):
            raise ValidationError(
                "blocked_grace_seconds must be between 60 and 86400 seconds"
            )
        self._blocked_grace = timedelta(seconds=blocked_grace_seconds)

    def accepts_observation(self, observation: NotificationObservation) -> bool:
        """Return whether this policy owns the observation's business reason."""

        return _kind_value(observation.kind) != NotificationKind.RESULT_INPUTS_BLOCKED.value

    def due_condition(
        self, state: NotificationPolicyState, *, now: datetime
    ) -> NotificationDueDecision:
        _aware(now, "policy due check time")
        if state.not_before is None:
            return NotificationDueDecision(
                NotificationDueStatus.NOT_DUE,
                None,
                "no_pending_condition",
            )
        if state.not_before > now:
            return NotificationDueDecision(
                NotificationDueStatus.NOT_DUE,
                state.not_before,
                "grace_period_active",
            )
        return NotificationDueDecision(
            NotificationDueStatus.DUE,
            state.not_before,
            "blocked_condition_due",
        )

    def evaluate(
        self,
        observation: NotificationObservation,
        *,
        state: NotificationPolicyState | None = None,
        now: datetime,
        mode: NotificationEvaluationMode | str = NotificationEvaluationMode.LIVE,
    ) -> NotificationPolicyMutation:
        if not isinstance(observation, NotificationObservation):
            raise ValidationError("policy requires a NotificationObservation")
        _aware(now, "policy evaluation time")
        evaluation_mode = _enum(mode, NotificationEvaluationMode, "evaluation mode")
        self._validate_state_scope(observation, state)

        kind = _kind_value(observation.kind)
        previous_fingerprint = state.observed_fingerprint if state else None
        same_fingerprint = previous_fingerprint == observation.authoritative_fingerprint
        previous_status = state.observed_status if state else None
        previous_notified = state.last_notified_fingerprint if state else None
        due = self.due_condition(state, now=now) if state else NotificationDueDecision(
            NotificationDueStatus.NOT_DUE, None, "new_condition"
        )
        is_silent = self._is_silent(observation, kind)
        is_recovery = self._is_recovery(observation, state)
        is_blocked = self._is_blocked(observation, kind)
        has_operator_attention = observation.requires_operator or any(
            item.requires_operator for item in observation.attention_items
        )

        first_observed_at, not_before = self._pending_window(
            observation,
            state=state,
            now=now,
            is_blocked=is_blocked,
            is_recovery=is_recovery,
            same_fingerprint=same_fingerprint,
        )
        state_for_evaluation = self._build_state(
            observation,
            state=state,
            now=now,
            first_observed_at=first_observed_at,
            not_before=not_before,
            last_notified_fingerprint=previous_notified,
        )

        fact_kind = self._fact_kind(kind, has_operator_attention)
        emit_reason = "not_actionable"
        priority: NotificationPriority | None = None
        transition: NotificationTransition | None = None
        topic = observation.topic
        should_emit = False

        if not is_silent:
            if observation.baseline_import:
                should_emit = (
                    not is_recovery
                    and self._is_actionable_kind(fact_kind, has_operator_attention)
                    and not same_fingerprint
                )
                priority = NotificationPriority.ACTION
                transition = NotificationTransition.OPENED
                emit_reason = "baseline_open_action" if should_emit else "baseline_seen"
            elif is_recovery:
                should_emit = previous_notified is not None and not same_fingerprint
                priority = NotificationPriority.PROGRESS
                transition = NotificationTransition.RECOVERED
                emit_reason = "recovery_after_notified_condition"
            elif evaluation_mode is NotificationEvaluationMode.RESYNC and not observation.baseline_import:
                emit_reason = "resync_suppresses_historical_fact"
            elif is_blocked and observation.automatic_recovery:
                should_emit = (
                    due.status is NotificationDueStatus.DUE
                    and same_fingerprint
                )
                priority = NotificationPriority.ACTION
                transition = self._change_transition(state, observation)
                emit_reason = "blocked_condition_due" if should_emit else "blocked_grace"
            elif fact_kind == NotificationKind.TASK_MILESTONE_COMPLETED.value:
                new_milestones = self._new_milestones(observation, state)
                should_emit = bool(new_milestones)
                if should_emit:
                    topic = f"milestone:{new_milestones[-1].key}"
                priority = NotificationPriority.PROGRESS
                transition = NotificationTransition.COMPLETED
                emit_reason = "new_milestone" if should_emit else "milestone_already_seen"
            elif fact_kind == NotificationKind.TASK_COMPLETED.value:
                should_emit = previous_status not in {"completed", "succeeded"}
                priority = NotificationPriority.PROGRESS
                transition = NotificationTransition.COMPLETED
                emit_reason = "root_task_completed" if should_emit else "root_completion_seen"
            elif fact_kind == "subtask_completed":
                emit_reason = "ordinary_subtask_completion_is_silent"
            elif fact_kind == NotificationKind.NOTIFICATION_CHANNEL_HEALTH.value:
                should_emit = not same_fingerprint
                priority = NotificationPriority.OPERATIONS
                transition = self._change_transition(state, observation)
                emit_reason = "channel_health_fact" if should_emit else "channel_health_seen"
            elif self._is_actionable_kind(fact_kind, has_operator_attention):
                should_emit = not same_fingerprint
                priority = NotificationPriority.ACTION
                transition = self._change_transition(state, observation)
                emit_reason = "actionable_condition" if should_emit else "actionable_condition_seen"
            elif self._is_progress_kind(fact_kind):
                should_emit = not same_fingerprint
                priority = NotificationPriority.PROGRESS
                transition = self._change_transition(state, observation)
                emit_reason = "progress_fact" if should_emit else "progress_fact_seen"

        fact = None
        if should_emit and priority is not None and transition is not None:
            fact = self._fact(
                observation,
                kind=fact_kind,
                priority=priority,
                transition=transition,
                topic=topic,
                now=now,
            )
            state_for_evaluation = self._build_state(
                observation,
                state=state_for_evaluation,
                now=now,
                first_observed_at=None
                if is_blocked and observation.automatic_recovery
                else state_for_evaluation.first_observed_at,
                not_before=None
                if is_blocked and observation.automatic_recovery
                else state_for_evaluation.not_before,
                last_notified_fingerprint=observation.authoritative_fingerprint,
            )

        route_eligibility = self._route_eligibility(observation)
        external_delivery_forbidden = (
            route_eligibility is NotificationRouteEligibility.WEB_ONLY
            or observation.baseline_import
        )
        final_due = self.due_condition(state_for_evaluation, now=now)
        return NotificationPolicyMutation(
            policy_state=state_for_evaluation,
            fact=fact,
            thread_business_status=_thread_business_status_for_fact(fact),
            route_eligibility=route_eligibility,
            external_delivery_forbidden=external_delivery_forbidden,
            due=final_due,
            reason=emit_reason if fact is None else "fact_created",
        )

    def _validate_state_scope(
        self,
        observation: NotificationObservation,
        state: NotificationPolicyState | None,
    ) -> None:
        if state is None:
            return
        if state.policy_version != self.policy_version:
            raise ValidationError("unsupported notification policy version")
        if (
            state.workspace_id != observation.workspace_id
            or state.recipient_type != observation.recipient_type
            or state.recipient_id != observation.recipient_id
            or state.subject_type != observation.subject_type
            or state.subject_id != observation.subject_id
            or state.topic != observation.topic
        ):
            raise ValidationError("policy state does not match observation scope")

    def _is_silent(
        self, observation: NotificationObservation, kind: str
    ) -> bool:
        return kind in {
            "worker_step",
            "trace_chunk",
            "tool_call",
            "heartbeat",
            "token_usage",
            "retry_queued",
            "notification_thread",
            "notification_delivery",
            NotificationKind.RESULT_INPUTS_BLOCKED.value,
        } or observation.source_projection_type in {
            "notification_thread",
            "notification_delivery",
            "worker_trace",
            "worker_step",
            "heartbeat",
        }

    @staticmethod
    def _is_blocked(observation: NotificationObservation, kind: str) -> bool:
        return observation.observed_status.lower() in {
            "blocked",
            "waiting",
            "waiting_for_capacity",
            "waiting_for_dependency",
        } or kind in {NotificationKind.TASK_BLOCKED.value, "execution_blocked"}

    def _is_recovery(
        self,
        observation: NotificationObservation,
        state: NotificationPolicyState | None,
    ) -> bool:
        kind = _kind_value(observation.kind)
        return observation.transition is NotificationTransition.RECOVERED or (
            state is not None
            and state.observed_status.lower() in {"blocked", "failed", "degraded"}
            and observation.observed_status.lower()
            in {"running", "ready", "converged", "completed", "recovered"}
            and kind
            in {
                NotificationKind.TASK_BLOCKED.value,
                NotificationKind.EXECUTION_FAILURE.value,
                NotificationKind.RESULT_INPUTS_BLOCKED.value,
                "task_blocked",
                "execution_failure",
            }
        )

    def _pending_window(
        self,
        observation: NotificationObservation,
        *,
        state: NotificationPolicyState | None,
        now: datetime,
        is_blocked: bool,
        is_recovery: bool,
        same_fingerprint: bool,
    ) -> tuple[datetime | None, datetime | None]:
        if not is_blocked or not observation.automatic_recovery or is_recovery:
            return None, None
        if state is None or not same_fingerprint or state.first_observed_at is None:
            return now, now + self._blocked_grace
        return state.first_observed_at, state.not_before

    def _build_state(
        self,
        observation: NotificationObservation,
        *,
        state: NotificationPolicyState | None,
        now: datetime,
        first_observed_at: datetime | None,
        not_before: datetime | None,
        last_notified_fingerprint: str | None,
    ) -> NotificationPolicyState:
        kind = _kind_value(observation.kind)
        observed_payload = {
            "kind": "silent_observation"
            if self._is_silent(observation, kind)
            else kind,
            "status": observation.observed_status,
            "resource_version": observation.source_resource_version,
            "source_resource_id": str(observation.source_resource_id),
            "requires_operator": observation.requires_operator
            or any(item.requires_operator for item in observation.attention_items),
            "completed_milestones": [
                item.to_mapping()
                for item in observation.completed_milestones
                if item.completed
            ],
            "attention": [item.to_mapping() for item in observation.attention_items],
        }
        return NotificationPolicyState(
            workspace_id=observation.workspace_id,
            recipient_type=observation.recipient_type,
            recipient_id=observation.recipient_id,
            subject_type=observation.subject_type,
            subject_id=observation.subject_id,
            topic=observation.topic,
            policy_version=self.policy_version,
            observed_status=observation.observed_status,
            observed_fingerprint=observation.authoritative_fingerprint,
            observed_payload=observed_payload,
            first_observed_at=first_observed_at,
            not_before=not_before,
            last_notified_fingerprint=last_notified_fingerprint,
            active_thread_id=state.active_thread_id if state else None,
            resource_version=state.resource_version + 1 if state else 1,
            updated_at=now,
        )

    @staticmethod
    def _fact_kind(kind: str, has_operator_attention: bool) -> str:
        if kind == "attention" and has_operator_attention:
            return NotificationKind.EXECUTION_ATTENTION.value
        return kind

    def _is_actionable_kind(self, kind: str, has_operator_attention: bool) -> bool:
        return has_operator_attention or kind in {
            NotificationKind.PLANNING_CLARIFICATION.value,
            NotificationKind.PLANNING_PROPOSAL_READY.value,
            NotificationKind.PLANNING_PROPOSAL_CONFLICTED.value,
            NotificationKind.PLANNING_PROPOSAL_BLOCKED.value,
            NotificationKind.PLAN_TRANSITION_DEGRADED.value,
            NotificationKind.WORKER_TRANSITION_BLOCKED_UNKNOWN.value,
            NotificationKind.HANDOFF_ATTENTION.value,
            NotificationKind.HANDOFF_FAILED.value,
            NotificationKind.HANDOFF_UNKNOWN.value,
            NotificationKind.COLLABORATION_RELEASE_PENDING.value,
            NotificationKind.GOVERNANCE_DECISION_REQUIRED.value,
            NotificationKind.EXECUTION_ATTENTION.value,
            NotificationKind.EXECUTION_FAILURE.value,
            NotificationKind.TASK_BLOCKED.value,
            "attention",
        }

    @staticmethod
    def _is_progress_kind(kind: str) -> bool:
        return kind in {
            NotificationKind.TASK_MILESTONE_COMPLETED.value,
            NotificationKind.TASK_COMPLETED.value,
            "task_recovered",
            "plan_generation_converged",
            "handoff_converged",
            "planning_materialized",
            "result_acceptance_recorded",
        }

    @staticmethod
    def _change_transition(
        state: NotificationPolicyState | None,
        observation: NotificationObservation,
    ) -> NotificationTransition:
        if observation.transition is not None:
            return observation.transition
        if state is None or state.last_notified_fingerprint is None:
            return NotificationTransition.OPENED
        return NotificationTransition.UPDATED

    @staticmethod
    def _new_milestones(
        observation: NotificationObservation,
        state: NotificationPolicyState | None,
    ) -> tuple[NotificationMilestone, ...]:
        previous_keys: set[str] = set()
        if state is not None:
            raw = state.observed_payload.get("completed_milestones", ())
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if not isinstance(item, Mapping):
                        continue
                    key = item.get("key")
                    if isinstance(key, str):
                        previous_keys.add(key)
        return tuple(
            item
            for item in observation.completed_milestones
            if item.completed and item.key not in previous_keys
        )

    @staticmethod
    def _route_eligibility(
        observation: NotificationObservation,
    ) -> NotificationRouteEligibility:
        if observation.baseline_import:
            return NotificationRouteEligibility.WEB_ONLY
        if _kind_value(observation.kind) in {
            NotificationKind.NOTIFICATION_CHANNEL_HEALTH.value,
            NotificationKind.NOTIFICATION_ROUTE_UNAVAILABLE.value,
        }:
            return NotificationRouteEligibility.WEB_ONLY
        if observation.source_type == "akashic_private":
            return NotificationRouteEligibility.ORIGIN_IF_ACTIVE
        return NotificationRouteEligibility.WEB_ONLY

    def _fact(
        self,
        observation: NotificationObservation,
        *,
        kind: str,
        priority: NotificationPriority,
        transition: NotificationTransition,
        topic: str,
        now: datetime,
    ) -> NotificationFact:
        summary, action = _STATIC_COPY.get(
            kind,
            (
                "任务状态发生了需要关注的变化。",
                "打开任务查看",
            ),
        )
        if priority is NotificationPriority.PROGRESS and kind not in _STATIC_COPY:
            summary, action = "任务进度已更新。", "查看任务进展"
        if priority is NotificationPriority.OPERATIONS:
            summary, action = "通知渠道状态需要检查。", "查看通知运维状态"
        return NotificationFact.create(
            workspace_id=observation.workspace_id,
            policy_version=self.policy_version,
            recipient_type=observation.recipient_type,
            recipient_id=observation.recipient_id,
            kind=kind,
            priority=priority,
            transition=transition,
            topic=topic,
            root_task_id=observation.root_task_id,
            subtask_id=observation.subtask_id,
            worker_item_id=observation.worker_item_id,
            source_projection_cursor=observation.source_projection_cursor,
            source_projection_type=observation.source_projection_type,
            source_resource_id=observation.source_resource_id,
            source_resource_version=observation.source_resource_version,
            authoritative_fingerprint=observation.authoritative_fingerprint,
            title=observation.root_task_title,
            summary=summary,
            action=action,
            deep_link=observation.deep_link,
            correlation_id=observation.correlation_id,
            baseline_import=observation.baseline_import,
            occurred_at=now,
            created_at=now,
        )


class NotificationPolicyV2(NotificationPolicyV1):
    """Own result-input delivery failures without replaying V1 history."""

    policy_version = 2

    def accepts_observation(self, observation: NotificationObservation) -> bool:
        return (
            _kind_value(observation.kind)
            == NotificationKind.RESULT_INPUTS_BLOCKED.value
        )

    def _is_silent(self, observation: NotificationObservation, kind: str) -> bool:
        return kind != NotificationKind.RESULT_INPUTS_BLOCKED.value

    def _is_actionable_kind(self, kind: str, has_operator_attention: bool) -> bool:
        del has_operator_attention
        return kind == NotificationKind.RESULT_INPUTS_BLOCKED.value


def build_notification_channel_health_fact(
    source_fact: NotificationFact,
    *,
    route_id: UUID,
    delivery_round: int,
    error_code: NotificationAttemptErrorCode | str,
    transition: NotificationTransition | str,
    now: datetime,
) -> NotificationFact:
    """Build the Web-only operational fact for a delivery channel condition."""

    if not isinstance(source_fact, NotificationFact):
        raise ValidationError("channel health requires a source notification fact")
    if not isinstance(route_id, UUID):
        raise ValidationError("channel health route_id must be a UUID")
    if not isinstance(delivery_round, int) or isinstance(delivery_round, bool) or delivery_round < 1:
        raise ValidationError("channel health delivery_round must be positive")
    try:
        parsed_error = (
            error_code
            if isinstance(error_code, NotificationAttemptErrorCode)
            else NotificationAttemptErrorCode(error_code)
        )
        parsed_transition = (
            transition
            if isinstance(transition, NotificationTransition)
            else NotificationTransition(transition)
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError("channel health identity is invalid") from exc
    if parsed_transition not in {
        NotificationTransition.DEGRADED,
        NotificationTransition.RECOVERED,
    }:
        raise ValidationError("channel health transition is invalid")

    identity = (
        f"notification-channel-health:{source_fact.id}:{route_id}:"
        f"{delivery_round}:{parsed_error.value}:{parsed_transition.value}"
    )
    authoritative_fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    if parsed_transition is NotificationTransition.RECOVERED:
        summary = "通知渠道已恢复。"
        action = "查看通知运维状态"
    else:
        summary = "通知渠道状态需要检查。"
        action = "查看通知运维状态"
    return NotificationFact.create(
        workspace_id=source_fact.workspace_id,
        policy_version=source_fact.policy_version,
        recipient_type=source_fact.recipient_type,
        recipient_id=source_fact.recipient_id,
        kind=NotificationKind.NOTIFICATION_CHANNEL_HEALTH.value,
        priority=NotificationPriority.OPERATIONS,
        transition=parsed_transition,
        topic=f"notification_channel_health:{route_id}",
        root_task_id=source_fact.root_task_id,
        subtask_id=source_fact.subtask_id,
        worker_item_id=source_fact.worker_item_id,
        source_projection_cursor=source_fact.source_projection_cursor,
        source_projection_type="notification_delivery",
        source_resource_id=source_fact.source_resource_id,
        source_resource_version=delivery_round,
        authoritative_fingerprint=authoritative_fingerprint,
        title=source_fact.title,
        summary=summary,
        action=action,
        deep_link=source_fact.deep_link,
        correlation_id=source_fact.correlation_id,
        baseline_import=False,
        occurred_at=now,
        created_at=now,
    )


def _kind_value(kind: NotificationKind | str) -> str:
    return kind.value if isinstance(kind, NotificationKind) else kind


def _thread_business_status_for_fact(
    fact: NotificationFact | None,
) -> NotificationThreadBusinessStatus | None:
    if fact is None:
        return None
    if fact.priority in {
        NotificationPriority.ACTION,
        NotificationPriority.OPERATIONS,
    } and (
        fact.transition is not NotificationTransition.RECOVERED
    ):
        return NotificationThreadBusinessStatus.OPEN
    return NotificationThreadBusinessStatus.RESOLVED


_STATIC_COPY: dict[str, tuple[str, str]] = {
    NotificationKind.PLANNING_CLARIFICATION.value: ("规划需要你的确认。", "查看规划请求"),
    NotificationKind.PLANNING_PROPOSAL_READY.value: ("新的计划方案已就绪。", "审阅计划方案"),
    NotificationKind.PLANNING_PROPOSAL_CONFLICTED.value: ("计划基础已变化，需要重新审阅。", "重新审阅计划"),
    NotificationKind.PLANNING_PROPOSAL_BLOCKED.value: ("计划生成遇到阻塞，需要处理。", "查看计划阻塞"),
    NotificationKind.PLAN_TRANSITION_DEGRADED.value: ("计划切换出现降级，需要处理。", "检查计划切换"),
    NotificationKind.WORKER_TRANSITION_BLOCKED_UNKNOWN.value: ("Worker 切换结果未知，需要确认。", "检查 Worker 切换"),
    NotificationKind.HANDOFF_ATTENTION.value: ("WorkerItem 的 agent 切换需要确认。", "检查 WorkerItem 切换"),
    NotificationKind.HANDOFF_FAILED.value: ("WorkerItem 的 agent 切换失败，需要处理。", "处理 WorkerItem 切换"),
    NotificationKind.HANDOFF_UNKNOWN.value: ("WorkerItem 的 agent 切换结果未知。", "检查 WorkerItem 切换"),
    NotificationKind.COLLABORATION_RELEASE_PENDING.value: ("协作发布请求等待你的决定。", "查看协作发布请求"),
    NotificationKind.GOVERNANCE_DECISION_REQUIRED.value: ("协作治理需要你的决定。", "查看治理事项"),
    NotificationKind.EXECUTION_ATTENTION.value: ("任务有一项事项需要处理。", "打开任务查看"),
    NotificationKind.EXECUTION_FAILURE.value: ("任务执行失败，需要处理。", "检查执行状态"),
    NotificationKind.RESULT_INPUTS_BLOCKED.value: ("结果输入暂时无法交付，需要处理。", "检查结果输入"),
    NotificationKind.TASK_BLOCKED.value: ("任务持续阻塞，需要处理。", "检查任务阻塞"),
    NotificationKind.TASK_MILESTONE_COMPLETED.value: ("一个重要里程碑已完成。", "查看任务进展"),
    NotificationKind.TASK_COMPLETED.value: ("根任务已完成。", "查看任务交付结果"),
    "task_recovered": ("此前通知的问题已经恢复。", "查看任务当前状态"),
    "plan_generation_converged": ("计划已完成收敛。", "查看计划结果"),
    "handoff_converged": ("WorkerItem 的 agent 切换已完成。", "查看 WorkerItem"),
    "planning_materialized": ("任务已从计划生成。", "打开根任务"),
    "result_candidate_ready": ("新的成果候选已就绪。", "审阅成果候选"),
    "result_attention": ("成果处理需要关注。", "检查成果状态"),
    "result_acceptance_recorded": ("成果验收决定已记录。", "查看收尾状态"),
}


def _safe_text(value: object, name: str, maximum: int = 16_384) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    if _SECRET_MARKER_PATTERN.search(value):
        raise ValidationError(f"{name} contains a forbidden secret-like marker")
    normalized = normalize_notification_text(value, maximum=maximum)
    return normalized


def _safe_identifier(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    normalized = normalize_notification_text(value, maximum=256)
    if re.fullmatch(r"[a-z][a-z0-9_.:-]*", normalized) is None:
        raise ValidationError(f"{name} must be a safe identifier")
    return normalized


def _text_tuple(value: tuple[str, ...], name: str, maximum: int = 16_384) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValidationError(f"{name} must be a tuple")
    return tuple(_safe_text(item, f"{name} item", maximum) for item in value)


def _uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValidationError(f"{name} must be a UUID")
    return value


def _optional_uuid(value: object | None, name: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, name)


def _positive(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be positive")


def _nonnegative(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{name} must be non-negative")


def _boolean(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")


def _aware(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError(f"{name} must be timezone-aware")
    return value


def _enum[EnumType: StrEnum](
    value: object, enum_type: type[EnumType], name: str
) -> EnumType:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {name}: {value!r}") from exc


def _hash(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a lowercase SHA-256 hex digest")
