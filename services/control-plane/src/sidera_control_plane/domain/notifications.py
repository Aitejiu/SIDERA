from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    LeaseExpired,
    StaleFencingToken,
    ValidationError,
    VersionConflict,
)

_MAX_TEXT_LENGTH = 16_384
_MAX_REASON_LENGTH = 256
_MAX_OPAQUE_REF_LENGTH = 512
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:/+\-]*$")
_SECRET_MARKER_PATTERN = re.compile(
    r"(?:api[_ -]?key|authorization|bearer|password|secret|token)", re.IGNORECASE
)

class NotificationPriority(StrEnum):
    ACTION = "action"
    PROGRESS = "progress"
    OPERATIONS = "operations"


class NotificationTransition(StrEnum):
    OPENED = "opened"
    UPDATED = "updated"
    RECOVERED = "recovered"
    COMPLETED = "completed"
    DEGRADED = "degraded"


class NotificationKind(StrEnum):
    PLANNING_CLARIFICATION = "planning_clarification"
    PLANNING_PROPOSAL_READY = "planning_proposal_ready"
    PLANNING_PROPOSAL_CONFLICTED = "planning_proposal_conflicted"
    PLANNING_PROPOSAL_BLOCKED = "planning_proposal_blocked"
    PLAN_TRANSITION_DEGRADED = "plan_transition_degraded"
    WORKER_TRANSITION_BLOCKED_UNKNOWN = "worker_transition_blocked_unknown"
    HANDOFF_ATTENTION = "handoff_attention"
    HANDOFF_FAILED = "handoff_failed"
    HANDOFF_UNKNOWN = "handoff_unknown"
    COLLABORATION_RELEASE_PENDING = "collaboration_release_pending"
    GOVERNANCE_DECISION_REQUIRED = "governance_decision_required"
    EXECUTION_ATTENTION = "execution_attention"
    EXECUTION_FAILURE = "execution_failure"
    TASK_BLOCKED = "task_blocked"
    TASK_MILESTONE_COMPLETED = "task_milestone_completed"
    TASK_COMPLETED = "task_completed"
    NOTIFICATION_CHANNEL_HEALTH = "notification_channel_health"
    NOTIFICATION_ROUTE_UNAVAILABLE = "notification_route_unavailable"


NotificationFactKind = NotificationKind


class NotificationBindingKind(StrEnum):
    ORIGIN = "origin"


class NotificationThreadBusinessStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class NotificationThreadOrganizationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class NotificationDeliveryStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RETRY_WAIT = "retry_wait"
    DELIVERED = "delivered"
    EXHAUSTED = "exhausted"
    SUPPRESSED = "suppressed"


class NotificationAttemptOutcome(StrEnum):
    DELIVERED = "delivered"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    UNKNOWN_RESULT = "unknown_result"


class NotificationRouteStatus(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    DISABLED = "disabled"
    REVOKED = "revoked"


class NotificationAttemptErrorCode(StrEnum):
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNKNOWN_RESULT = "unknown_result"
    ROUTE_NOT_FOUND = "route_not_found"
    ROUTE_REVOKED = "route_revoked"
    PERMISSION_DENIED = "permission_denied"
    CONTRACT_UNSUPPORTED = "contract_unsupported"
    PAYLOAD_REJECTED = "payload_rejected"


# Both names are useful at different adapter boundaries; they share one wire enum.
NotificationDeliveryErrorCode = NotificationAttemptErrorCode


def normalize_notification_text(value: str, *, maximum: int = _MAX_TEXT_LENGTH) -> str:
    """Normalize user-visible text without splitting Unicode code points."""

    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 4:
        raise ValidationError("maximum must be at least 4")
    if not isinstance(value, str):
        raise ValidationError("notification text must be a string")
    cleaned = "".join(
        " " if character in "\t\n\r" else ""
        if ord(character) < 32 or ord(character) == 127
        else character
        for character in value
    )
    normalized = " ".join(cleaned.split())
    if not normalized:
        raise ValidationError("notification text is required")
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 3].rstrip() + "..."


def notification_identity_name(
    *,
    workspace_id: UUID,
    recipient_type: str,
    recipient_id: str,
    policy_version: int,
    root_task_id: UUID,
    subtask_id: UUID | None,
    worker_item_id: UUID | None,
    topic: str,
    transition: NotificationTransition | str,
    authoritative_fingerprint: str,
) -> str:
    """Return the frozen canonical name used for Fact identity and fingerprinting."""

    _uuid(workspace_id, "workspace_id")
    recipient_type_value = _identifier(recipient_type, "recipient_type")
    recipient_id_value = _identifier(recipient_id, "recipient_id")
    _positive(policy_version, "policy_version")
    _uuid(root_task_id, "root_task_id")
    _optional_uuid(subtask_id, "subtask_id")
    _optional_uuid(worker_item_id, "worker_item_id")
    topic_value = _safe_text(topic, "topic")
    transition_value = _enum(transition, NotificationTransition, "transition").value
    _hash(authoritative_fingerprint, "authoritative_fingerprint")
    subject = (
        f"root_task/{root_task_id}/subtask/{subtask_id or '-'}"
        f"/worker_item/{worker_item_id or '-'}"
    )
    return (
        f"sidera:notification:{workspace_id}:"
        f"{recipient_type_value}/{recipient_id_value}:{policy_version}:"
        f"{subject}:{topic_value}:{transition_value}:{authoritative_fingerprint}"
    )


def notification_fact_id(
    *,
    workspace_id: UUID,
    recipient_type: str,
    recipient_id: str,
    policy_version: int,
    root_task_id: UUID,
    subtask_id: UUID | None,
    worker_item_id: UUID | None,
    topic: str,
    transition: NotificationTransition | str,
    authoritative_fingerprint: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        notification_identity_name(
            workspace_id=workspace_id,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            policy_version=policy_version,
            root_task_id=root_task_id,
            subtask_id=subtask_id,
            worker_item_id=worker_item_id,
            topic=topic,
            transition=transition,
            authoritative_fingerprint=authoritative_fingerprint,
        ),
    )


def notification_semantic_fingerprint(
    *,
    workspace_id: UUID,
    recipient_type: str,
    recipient_id: str,
    policy_version: int,
    root_task_id: UUID,
    subtask_id: UUID | None,
    worker_item_id: UUID | None,
    topic: str,
    transition: NotificationTransition | str,
    authoritative_fingerprint: str,
) -> str:
    return hashlib.sha256(
        notification_identity_name(
            workspace_id=workspace_id,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            policy_version=policy_version,
            root_task_id=root_task_id,
            subtask_id=subtask_id,
            worker_item_id=worker_item_id,
            topic=topic,
            transition=transition,
            authoritative_fingerprint=authoritative_fingerprint,
        ).encode("utf-8")
    ).hexdigest()


def notification_thread_key(
    *,
    recipient_type: str,
    recipient_id: str,
    root_task_id: UUID,
    subtask_id: UUID | None,
    worker_item_id: UUID | None,
    topic: str,
) -> str:
    return (
        f"{_identifier(recipient_type, 'recipient_type')}/"
        f"{_identifier(recipient_id, 'recipient_id')}:"
        f"root_task/{_uuid(root_task_id, 'root_task_id')}:"
        f"subtask/{_optional_uuid(subtask_id, 'subtask_id') or '-'}:"
        f"worker_item/{_optional_uuid(worker_item_id, 'worker_item_id') or '-'}:"
        f"topic/{_safe_text(topic, 'topic')}"
    )


def notification_thread_id(thread_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"sidera:notification-thread:{_safe_text(thread_key, 'thread_key')}")


def notification_delivery_intent_id(
    *,
    workspace_id: UUID,
    notification_fact_id: UUID,
    notification_route_id: UUID,
    delivery_round: int,
) -> UUID:
    _uuid(workspace_id, "workspace_id")
    _uuid(notification_fact_id, "notification_fact_id")
    _uuid(notification_route_id, "notification_route_id")
    _positive(delivery_round, "delivery_round")
    return uuid5(
        NAMESPACE_URL,
        f"sidera:notification-delivery:{workspace_id}:{notification_fact_id}:"
        f"{notification_route_id}:{delivery_round}",
    )


@dataclass(frozen=True, slots=True)
class NotificationPolicyState:
    workspace_id: UUID
    recipient_type: str
    recipient_id: str
    subject_type: str
    subject_id: str
    topic: str
    policy_version: int
    observed_status: str
    observed_fingerprint: str | None
    observed_payload: Mapping[str, object]
    first_observed_at: datetime | None
    not_before: datetime | None
    last_notified_fingerprint: str | None
    active_thread_id: UUID | None
    resource_version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.workspace_id, "workspace_id")
        object.__setattr__(
            self,
            "recipient_type",
            _identifier(self.recipient_type, "recipient_type"),
        )
        object.__setattr__(
            self, "recipient_id", _identifier(self.recipient_id, "recipient_id")
        )
        object.__setattr__(
            self, "subject_type", _identifier(self.subject_type, "subject_type")
        )
        object.__setattr__(
            self, "subject_id", _identifier(self.subject_id, "subject_id")
        )
        object.__setattr__(self, "topic", _safe_text(self.topic, "topic"))
        _positive(self.policy_version, "policy_version")
        object.__setattr__(
            self,
            "observed_status",
            _safe_text(self.observed_status, "observed_status"),
        )
        object.__setattr__(
            self,
            "observed_fingerprint",
            _optional_hash(self.observed_fingerprint, "observed_fingerprint"),
        )
        object.__setattr__(
            self,
            "last_notified_fingerprint",
            _optional_hash(
                self.last_notified_fingerprint, "last_notified_fingerprint"
            ),
        )
        object.__setattr__(
            self,
            "observed_payload",
            _freeze_mapping(self.observed_payload, "observed_payload"),
        )
        _optional_aware(self.first_observed_at, "first_observed_at")
        _optional_aware(self.not_before, "not_before")
        if self.not_before is not None and self.first_observed_at is None:
            raise ValidationError("not_before requires first_observed_at")
        if (
            self.not_before is not None
            and self.first_observed_at is not None
            and self.not_before < self.first_observed_at
        ):
            raise ValidationError("not_before cannot precede first_observed_at")
        _optional_uuid(self.active_thread_id, "active_thread_id")
        _positive(self.resource_version, "resource_version")
        _aware(self.updated_at, "updated_at")

    def with_observation(
        self,
        *,
        observed_status: str,
        observed_fingerprint: str | None,
        observed_payload: Mapping[str, object],
        now: datetime,
        not_before: datetime | None = None,
    ) -> NotificationPolicyState:
        _aware(now, "updated_at")
        return replace(
            self,
            observed_status=observed_status,
            observed_fingerprint=observed_fingerprint,
            observed_payload=observed_payload,
            first_observed_at=self.first_observed_at or now,
            not_before=not_before,
            resource_version=self.resource_version + 1,
            updated_at=now,
        )


@dataclass(frozen=True, slots=True)
class NotificationFact:
    id: UUID
    workspace_id: UUID
    policy_version: int
    recipient_type: str
    recipient_id: str
    kind: str
    priority: NotificationPriority
    transition: NotificationTransition
    topic: str
    root_task_id: UUID
    subtask_id: UUID | None
    worker_item_id: UUID | None
    source_projection_cursor: int
    source_projection_type: str
    source_resource_id: UUID
    source_resource_version: int
    semantic_fingerprint: str
    title: str
    summary: str
    action: str
    deep_link: str
    correlation_id: str
    baseline_import: bool
    occurred_at: datetime
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        workspace_id: UUID,
        policy_version: int,
        recipient_type: str,
        recipient_id: str,
        kind: str,
        priority: NotificationPriority,
        transition: NotificationTransition,
        topic: str,
        root_task_id: UUID,
        subtask_id: UUID | None,
        worker_item_id: UUID | None,
        source_projection_cursor: int,
        source_projection_type: str,
        source_resource_id: UUID,
        source_resource_version: int,
        authoritative_fingerprint: str,
        title: str,
        summary: str,
        action: str,
        deep_link: str,
        correlation_id: str,
        baseline_import: bool,
        occurred_at: datetime,
        created_at: datetime,
    ) -> NotificationFact:
        identity = {
            "workspace_id": workspace_id,
            "recipient_type": recipient_type,
            "recipient_id": recipient_id,
            "policy_version": policy_version,
            "root_task_id": root_task_id,
            "subtask_id": subtask_id,
            "worker_item_id": worker_item_id,
            "topic": topic,
            "transition": transition,
            "authoritative_fingerprint": authoritative_fingerprint,
        }
        return cls(
            id=notification_fact_id(**identity),
            workspace_id=workspace_id,
            policy_version=policy_version,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            kind=kind,
            priority=priority,
            transition=transition,
            topic=topic,
            root_task_id=root_task_id,
            subtask_id=subtask_id,
            worker_item_id=worker_item_id,
            source_projection_cursor=source_projection_cursor,
            source_projection_type=source_projection_type,
            source_resource_id=source_resource_id,
            source_resource_version=source_resource_version,
            semantic_fingerprint=notification_semantic_fingerprint(**identity),
            title=title,
            summary=summary,
            action=action,
            deep_link=deep_link,
            correlation_id=correlation_id,
            baseline_import=baseline_import,
            occurred_at=occurred_at,
            created_at=created_at,
        )

    def __post_init__(self) -> None:
        _uuid(self.id, "notification fact id")
        _uuid(self.workspace_id, "workspace_id")
        _positive(self.policy_version, "policy_version")
        object.__setattr__(
            self,
            "recipient_type",
            _identifier(self.recipient_type, "recipient_type"),
        )
        object.__setattr__(
            self, "recipient_id", _identifier(self.recipient_id, "recipient_id")
        )
        object.__setattr__(self, "kind", _safe_text(self.kind, "kind"))
        object.__setattr__(
            self,
            "priority",
            _enum(self.priority, NotificationPriority, "priority"),
        )
        object.__setattr__(
            self,
            "transition",
            _enum(self.transition, NotificationTransition, "transition"),
        )
        object.__setattr__(self, "topic", _safe_text(self.topic, "topic"))
        _uuid(self.root_task_id, "root_task_id")
        _optional_uuid(self.subtask_id, "subtask_id")
        _optional_uuid(self.worker_item_id, "worker_item_id")
        _nonnegative(self.source_projection_cursor, "source_projection_cursor")
        object.__setattr__(
            self,
            "source_projection_type",
            _identifier(self.source_projection_type, "source_projection_type"),
        )
        _uuid(self.source_resource_id, "source_resource_id")
        _positive(self.source_resource_version, "source_resource_version")
        _hash(self.semantic_fingerprint, "semantic_fingerprint")
        for name in ("title", "summary", "action", "deep_link", "correlation_id"):
            object.__setattr__(
                self, name, _safe_text(getattr(self, name), name, _MAX_TEXT_LENGTH)
            )
        if not isinstance(self.baseline_import, bool):
            raise ValidationError("baseline_import must be a boolean")
        _aware(self.occurred_at, "occurred_at")
        _aware(self.created_at, "created_at")

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "policy_version": self.policy_version,
            "recipient_type": self.recipient_type,
            "recipient_id": self.recipient_id,
            "kind": self.kind,
            "priority": self.priority.value,
            "transition": self.transition.value,
            "topic": self.topic,
            "root_task_id": str(self.root_task_id),
            "subtask_id": str(self.subtask_id) if self.subtask_id else None,
            "worker_item_id": str(self.worker_item_id) if self.worker_item_id else None,
            "source_projection_cursor": self.source_projection_cursor,
            "source_projection_type": self.source_projection_type,
            "source_resource_id": str(self.source_resource_id),
            "source_resource_version": self.source_resource_version,
            "semantic_fingerprint": self.semantic_fingerprint,
            "title": self.title,
            "summary": self.summary,
            "action": self.action,
            "deep_link": self.deep_link,
            "correlation_id": self.correlation_id,
            "baseline_import": self.baseline_import,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "created_at": self.created_at.astimezone(UTC).isoformat(),
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class InboxThread:
    id: UUID
    workspace_id: UUID
    recipient_type: str
    recipient_id: str
    thread_key: str
    topic: str
    root_task_id: UUID
    subtask_id: UUID | None
    worker_item_id: UUID | None
    latest_fact_id: UUID
    business_status: NotificationThreadBusinessStatus
    organization_status: NotificationThreadOrganizationStatus
    last_read_fact_id: UUID | None
    unread_count: int
    version: int
    opened_at: datetime
    resolved_at: datetime | None
    archived_at: datetime | None
    updated_at: datetime

    @classmethod
    def open_from_fact(
        cls, fact: NotificationFact, *, thread_id: UUID, now: datetime
    ) -> InboxThread:
        if not isinstance(fact, NotificationFact):
            raise ValidationError("thread requires a NotificationFact")
        _aware(now, "thread opened_at")
        return cls(
            id=thread_id,
            workspace_id=fact.workspace_id,
            recipient_type=fact.recipient_type,
            recipient_id=fact.recipient_id,
            thread_key=notification_thread_key(
                recipient_type=fact.recipient_type,
                recipient_id=fact.recipient_id,
                root_task_id=fact.root_task_id,
                subtask_id=fact.subtask_id,
                worker_item_id=fact.worker_item_id,
                topic=fact.topic,
            ),
            topic=fact.topic,
            root_task_id=fact.root_task_id,
            subtask_id=fact.subtask_id,
            worker_item_id=fact.worker_item_id,
            latest_fact_id=fact.id,
            business_status=_business_status_for_fact(fact),
            organization_status=NotificationThreadOrganizationStatus.ACTIVE,
            last_read_fact_id=None,
            unread_count=1,
            version=1,
            opened_at=now,
            resolved_at=now
            if _business_status_for_fact(fact)
            is NotificationThreadBusinessStatus.RESOLVED
            else None,
            archived_at=None,
            updated_at=now,
        )

    @classmethod
    def from_fact(
        cls, fact: NotificationFact, *, thread_id: UUID, now: datetime
    ) -> InboxThread:
        return cls.open_from_fact(fact, thread_id=thread_id, now=now)

    def __post_init__(self) -> None:
        _uuid(self.id, "thread id")
        _uuid(self.workspace_id, "thread workspace_id")
        object.__setattr__(
            self,
            "recipient_type",
            _identifier(self.recipient_type, "thread recipient_type"),
        )
        object.__setattr__(
            self,
            "recipient_id",
            _identifier(self.recipient_id, "thread recipient_id"),
        )
        object.__setattr__(
            self,
            "thread_key",
            _safe_text(self.thread_key, "thread_key", _MAX_TEXT_LENGTH),
        )
        object.__setattr__(self, "topic", _safe_text(self.topic, "thread topic"))
        _uuid(self.root_task_id, "thread root_task_id")
        _optional_uuid(self.subtask_id, "thread subtask_id")
        _optional_uuid(self.worker_item_id, "thread worker_item_id")
        _uuid(self.latest_fact_id, "thread latest_fact_id")
        object.__setattr__(
            self,
            "business_status",
            _enum(
                self.business_status,
                NotificationThreadBusinessStatus,
                "thread business status",
            ),
        )
        object.__setattr__(
            self,
            "organization_status",
            _enum(
                self.organization_status,
                NotificationThreadOrganizationStatus,
                "thread organization status",
            ),
        )
        _optional_uuid(self.last_read_fact_id, "thread last_read_fact_id")
        _nonnegative(self.unread_count, "thread unread_count")
        _positive(self.version, "thread version")
        _aware(self.opened_at, "thread opened_at")
        _optional_aware(self.resolved_at, "thread resolved_at")
        _optional_aware(self.archived_at, "thread archived_at")
        _aware(self.updated_at, "thread updated_at")
        if self.business_status is NotificationThreadBusinessStatus.OPEN:
            if self.resolved_at is not None:
                raise ValidationError("open thread cannot have resolved_at")
        elif self.resolved_at is None:
            raise ValidationError("resolved thread requires resolved_at")
        if self.organization_status is NotificationThreadOrganizationStatus.ACTIVE:
            if self.archived_at is not None:
                raise ValidationError("active thread cannot have archived_at")
        elif self.archived_at is None:
            raise ValidationError("archived thread requires archived_at")

    def apply_fact(self, fact: NotificationFact, *, now: datetime) -> InboxThread:
        _aware(now, "thread updated_at")
        if not isinstance(fact, NotificationFact):
            raise ValidationError("thread requires a NotificationFact")
        if (
            fact.workspace_id != self.workspace_id
            or fact.recipient_type != self.recipient_type
            or fact.recipient_id != self.recipient_id
            or fact.root_task_id != self.root_task_id
            or fact.subtask_id != self.subtask_id
            or fact.worker_item_id != self.worker_item_id
            or fact.topic != self.topic
        ):
            raise ValidationError("fact does not belong to this thread topic")
        if fact.id == self.latest_fact_id:
            return self
        business_status = _business_status_for_fact(fact)
        return replace(
            self,
            latest_fact_id=fact.id,
            business_status=business_status,
            organization_status=NotificationThreadOrganizationStatus.ACTIVE,
            unread_count=self.unread_count + 1,
            version=self.version + 1,
            resolved_at=now
            if business_status is NotificationThreadBusinessStatus.RESOLVED
            else None,
            archived_at=None,
            updated_at=now,
        )

    def mark_read(
        self,
        *,
        expected_version: int,
        expected_latest_fact_id: UUID,
        now: datetime,
    ) -> InboxThread:
        _positive(expected_version, "expected_version")
        _aware(now, "thread read_at")
        if expected_version != self.version:
            raise VersionConflict(expected_version, self.version)
        if expected_latest_fact_id != self.latest_fact_id:
            raise InvalidTransition("expected latest fact does not match thread")
        if self.unread_count == 0 and self.last_read_fact_id == self.latest_fact_id:
            return self
        return replace(
            self,
            last_read_fact_id=self.latest_fact_id,
            unread_count=0,
            version=self.version + 1,
            updated_at=now,
        )

    def archive(self, *, now: datetime) -> InboxThread:
        _aware(now, "thread archived_at")
        if self.business_status is NotificationThreadBusinessStatus.OPEN:
            raise InvalidTransition("open thread cannot be archived")
        if self.organization_status is NotificationThreadOrganizationStatus.ARCHIVED:
            return self
        return replace(
            self,
            organization_status=NotificationThreadOrganizationStatus.ARCHIVED,
            archived_at=now,
            version=self.version + 1,
            updated_at=now,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "recipient_type": self.recipient_type,
            "recipient_id": self.recipient_id,
            "thread_key": self.thread_key,
            "topic": self.topic,
            "root_task_id": str(self.root_task_id),
            "subtask_id": str(self.subtask_id) if self.subtask_id else None,
            "worker_item_id": str(self.worker_item_id) if self.worker_item_id else None,
            "latest_fact_id": str(self.latest_fact_id),
            "business_status": self.business_status.value,
            "organization_status": self.organization_status.value,
            "last_read_fact_id": (
                str(self.last_read_fact_id) if self.last_read_fact_id else None
            ),
            "unread_count": self.unread_count,
            "version": self.version,
            "opened_at": self.opened_at.astimezone(UTC).isoformat(),
            "resolved_at": (
                self.resolved_at.astimezone(UTC).isoformat()
                if self.resolved_at
                else None
            ),
            "archived_at": (
                self.archived_at.astimezone(UTC).isoformat()
                if self.archived_at
                else None
            ),
            "updated_at": self.updated_at.astimezone(UTC).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class NotificationRoute:
    id: UUID
    workspace_id: UUID
    recipient_type: str
    recipient_id: str
    channel_type: str
    channel_module_instance_id: UUID
    source_ref: str
    status: NotificationRouteStatus
    version: int
    registered_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.id, "route id")
        _uuid(self.workspace_id, "route workspace_id")
        object.__setattr__(
            self,
            "recipient_type",
            _identifier(self.recipient_type, "route recipient_type"),
        )
        object.__setattr__(
            self,
            "recipient_id",
            _identifier(self.recipient_id, "route recipient_id"),
        )
        object.__setattr__(
            self,
            "channel_type",
            _identifier(self.channel_type, "channel_type"),
        )
        _uuid(self.channel_module_instance_id, "channel_module_instance_id")
        object.__setattr__(
            self,
            "source_ref",
            _opaque_ref(self.source_ref, "source_ref"),
        )
        object.__setattr__(
            self,
            "status",
            _enum(self.status, NotificationRouteStatus, "route status"),
        )
        _positive(self.version, "route version")
        _aware(self.registered_at, "route registered_at")
        _aware(self.updated_at, "route updated_at")

    def with_status(
        self, status: NotificationRouteStatus | str, *, now: datetime
    ) -> NotificationRoute:
        _aware(now, "route updated_at")
        return replace(
            self,
            status=_enum(status, NotificationRouteStatus, "route status"),
            version=self.version + 1,
            updated_at=now,
        )


@dataclass(frozen=True, slots=True)
class NotificationSourceBinding:
    workspace_id: UUID
    resource_type: str
    resource_id: UUID
    notification_route_id: UUID
    binding_kind: NotificationBindingKind
    bound_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.workspace_id, "binding workspace_id")
        resource_type = _identifier(self.resource_type, "resource_type")
        if resource_type not in {"planning_request", "root_task"}:
            raise ValidationError("unsupported notification binding resource_type")
        object.__setattr__(self, "resource_type", resource_type)
        _uuid(self.resource_id, "binding resource_id")
        _uuid(self.notification_route_id, "notification_route_id")
        object.__setattr__(
            self,
            "binding_kind",
            _enum(self.binding_kind, NotificationBindingKind, "binding kind"),
        )
        _aware(self.bound_at, "binding bound_at")


@dataclass(frozen=True, slots=True)
class NotificationReconciliationCursor:
    workspace_id: UUID
    policy_version: int
    last_projection_cursor: int
    last_reconciled_at: datetime
    lease_owner: str | None
    lease_until: datetime | None
    version: int

    def __post_init__(self) -> None:
        _uuid(self.workspace_id, "cursor workspace_id")
        _positive(self.policy_version, "cursor policy_version")
        _nonnegative(self.last_projection_cursor, "last_projection_cursor")
        _aware(self.last_reconciled_at, "last_reconciled_at")
        object.__setattr__(
            self, "lease_owner", _optional_identifier(self.lease_owner, "lease_owner")
        )
        _optional_aware(self.lease_until, "lease_until")
        if self.lease_until is not None and self.lease_owner is None:
            raise ValidationError("lease_until requires lease_owner")
        _positive(self.version, "cursor version")

    def claim(
        self, *, lease_owner: str, lease_until: datetime, now: datetime
    ) -> NotificationReconciliationCursor:
        _aware(now, "cursor claim time")
        _aware(lease_until, "cursor lease_until")
        if lease_until <= now:
            raise ValidationError("cursor lease_until must be after now")
        owner = _identifier(lease_owner, "lease_owner")
        if (
            self.lease_until is not None
            and self.lease_until > now
            and self.lease_owner != owner
        ):
            raise InvalidTransition("notification cursor is leased by another owner")
        return replace(
            self,
            lease_owner=owner,
            lease_until=lease_until,
            version=self.version + 1,
        )

    def release(self, *, now: datetime) -> NotificationReconciliationCursor:
        _aware(now, "cursor release time")
        return replace(
            self,
            lease_owner=None,
            lease_until=None,
            last_reconciled_at=now,
            version=self.version + 1,
        )

    def advance(
        self, *, cursor: int, now: datetime
    ) -> NotificationReconciliationCursor:
        _aware(now, "cursor reconciled_at")
        _nonnegative(cursor, "cursor")
        if cursor < self.last_projection_cursor:
            raise InvalidTransition("notification cursor cannot move backwards")
        return replace(
            self,
            last_projection_cursor=cursor,
            last_reconciled_at=now,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class NotificationDeliveryAttempt:
    delivery_intent_id: UUID
    attempt_number: int
    claim_token: UUID
    fencing_token: int
    outcome: NotificationAttemptOutcome | None
    error_code: NotificationAttemptErrorCode | None
    provider_message_ref: str | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def open(
        cls,
        *,
        delivery_intent_id: UUID,
        attempt_number: int,
        claim_token: UUID,
        fencing_token: int,
        started_at: datetime,
    ) -> NotificationDeliveryAttempt:
        return cls(
            delivery_intent_id=delivery_intent_id,
            attempt_number=attempt_number,
            claim_token=claim_token,
            fencing_token=fencing_token,
            outcome=None,
            error_code=None,
            provider_message_ref=None,
            started_at=started_at,
            finished_at=None,
        )

    def __post_init__(self) -> None:
        _uuid(self.delivery_intent_id, "attempt delivery_intent_id")
        _positive(self.attempt_number, "attempt_number")
        _uuid(self.claim_token, "attempt claim_token")
        _positive(self.fencing_token, "attempt fencing_token")
        object.__setattr__(
            self,
            "outcome",
            _optional_enum(
                self.outcome, NotificationAttemptOutcome, "attempt outcome"
            ),
        )
        object.__setattr__(
            self,
            "error_code",
            _optional_enum(
                self.error_code,
                NotificationAttemptErrorCode,
                "attempt error_code",
            ),
        )
        object.__setattr__(
            self,
            "provider_message_ref",
            _optional_opaque_ref(
                self.provider_message_ref, "provider_message_ref"
            ),
        )
        _aware(self.started_at, "attempt started_at")
        _optional_aware(self.finished_at, "attempt finished_at")
        if self.outcome is None and self.finished_at is not None:
            raise ValidationError("open attempt cannot have finished_at")
        if self.outcome is not None and self.finished_at is None:
            raise ValidationError("settled attempt requires finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValidationError("attempt finished_at cannot precede started_at")
        if (
            self.outcome is NotificationAttemptOutcome.DELIVERED
            and self.error_code is not None
        ):
            raise ValidationError("delivered attempt cannot have error_code")

    def settle(
        self,
        *,
        outcome: NotificationAttemptOutcome | str,
        now: datetime,
        error_code: NotificationAttemptErrorCode | str | None = None,
        provider_message_ref: str | None = None,
    ) -> NotificationDeliveryAttempt:
        if self.outcome is not None or self.finished_at is not None:
            raise InvalidTransition("delivery attempt is already settled")
        _aware(now, "attempt finished_at")
        if now < self.started_at:
            raise ValidationError("attempt finished_at cannot precede started_at")
        parsed_outcome = _enum(outcome, NotificationAttemptOutcome, "attempt outcome")
        parsed_error = _optional_enum(
            error_code, NotificationAttemptErrorCode, "attempt error_code"
        )
        if parsed_outcome is NotificationAttemptOutcome.DELIVERED and parsed_error:
            raise ValidationError("delivered attempt cannot have error_code")
        if parsed_outcome is not NotificationAttemptOutcome.DELIVERED and (
            parsed_error is None
        ):
            raise ValidationError("failed attempt requires error_code")
        return replace(
            self,
            outcome=parsed_outcome,
            error_code=parsed_error,
            provider_message_ref=provider_message_ref,
            finished_at=now,
        )


@dataclass(frozen=True, slots=True)
class NotificationDeliveryIntent:
    id: UUID
    workspace_id: UUID
    notification_fact_id: UUID
    notification_route_id: UUID
    channel_module_instance_id: UUID
    delivery_round: int
    contract_version: int
    status: NotificationDeliveryStatus
    attempts: int
    next_attempt_at: datetime
    claimed_until: datetime | None
    claim_token: UUID | None
    fencing_token: int
    provider_message_ref: str | None
    last_error_code: NotificationAttemptErrorCode | None
    delivered_at: datetime | None
    exhausted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.id, "delivery intent id")
        _uuid(self.workspace_id, "delivery workspace_id")
        _uuid(self.notification_fact_id, "notification_fact_id")
        _uuid(self.notification_route_id, "notification_route_id")
        _uuid(self.channel_module_instance_id, "channel_module_instance_id")
        _positive(self.delivery_round, "delivery_round")
        _positive(self.contract_version, "contract_version")
        object.__setattr__(
            self,
            "status",
            _enum(self.status, NotificationDeliveryStatus, "delivery status"),
        )
        _nonnegative(self.attempts, "attempts")
        _aware(self.next_attempt_at, "next_attempt_at")
        _optional_aware(self.claimed_until, "claimed_until")
        _optional_uuid(self.claim_token, "claim_token")
        _positive(self.fencing_token, "fencing_token")
        object.__setattr__(
            self,
            "provider_message_ref",
            _optional_opaque_ref(
                self.provider_message_ref, "provider_message_ref"
            ),
        )
        object.__setattr__(
            self,
            "last_error_code",
            _optional_enum(
                self.last_error_code,
                NotificationAttemptErrorCode,
                "last_error_code",
            ),
        )
        _optional_aware(self.delivered_at, "delivered_at")
        _optional_aware(self.exhausted_at, "exhausted_at")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        if self.status is NotificationDeliveryStatus.CLAIMED and (
            self.claimed_until is None or self.claim_token is None
        ):
            raise ValidationError("claimed delivery requires claim token and lease")
        if self.status is NotificationDeliveryStatus.DELIVERED and (
            self.delivered_at is None
        ):
            raise ValidationError("delivered delivery requires delivered_at")
        if self.status is NotificationDeliveryStatus.EXHAUSTED and (
            self.exhausted_at is None
        ):
            raise ValidationError("exhausted delivery requires exhausted_at")
        if self.status is not NotificationDeliveryStatus.CLAIMED and (
            self.claimed_until is not None
        ):
            raise ValidationError("only claimed delivery can have claimed_until")

    def claim(
        self,
        *,
        claim_token: UUID,
        fencing_token: int,
        claimed_until: datetime,
        now: datetime,
    ) -> NotificationDeliveryClaim:
        _uuid(claim_token, "claim_token")
        _positive(fencing_token, "fencing_token")
        _aware(now, "claim time")
        _aware(claimed_until, "claimed_until")
        if claimed_until <= now:
            raise ValidationError("claimed_until must be after now")
        if self.status not in {
            NotificationDeliveryStatus.PENDING,
            NotificationDeliveryStatus.RETRY_WAIT,
        }:
            raise InvalidTransition("delivery is not eligible for claim")
        if self.status is NotificationDeliveryStatus.RETRY_WAIT and now < self.next_attempt_at:
            raise InvalidTransition("delivery retry is not due")
        if fencing_token < self.fencing_token:
            raise StaleFencingToken("delivery fencing token is stale")
        attempt_number = self.attempts + 1
        next_intent = replace(
            self,
            status=NotificationDeliveryStatus.CLAIMED,
            attempts=attempt_number,
            claimed_until=claimed_until,
            claim_token=claim_token,
            fencing_token=fencing_token,
            updated_at=now,
        )
        attempt = NotificationDeliveryAttempt.open(
            delivery_intent_id=self.id,
            attempt_number=attempt_number,
            claim_token=claim_token,
            fencing_token=fencing_token,
            started_at=now,
        )
        return NotificationDeliveryClaim(intent=next_intent, attempt=attempt)

    def validate_attempt(self, attempt: NotificationDeliveryAttempt) -> None:
        if attempt.delivery_intent_id != self.id:
            raise ValidationError("attempt belongs to another delivery intent")
        if attempt.attempt_number != self.attempts:
            raise ValidationError("attempt_number does not match delivery attempts")
        if self.claim_token != attempt.claim_token:
            raise StaleFencingToken("delivery claim token is stale")
        if self.fencing_token != attempt.fencing_token:
            raise StaleFencingToken("delivery fencing token is stale")

    def ack(
        self,
        *,
        claim_token: UUID,
        fencing_token: int,
        provider_message_ref: str | None,
        now: datetime,
    ) -> NotificationDeliveryIntent:
        _uuid(claim_token, "claim_token")
        _positive(fencing_token, "fencing_token")
        _aware(now, "delivered_at")
        if self.status is NotificationDeliveryStatus.DELIVERED:
            if (
                self.claim_token == claim_token
                and self.fencing_token == fencing_token
                and self.provider_message_ref == _optional_opaque_ref(
                    provider_message_ref, "provider_message_ref"
                )
            ):
                return self
            raise InvalidTransition("duplicate ack does not match delivered result")
        self._assert_claim(claim_token, fencing_token, now)
        return replace(
            self,
            status=NotificationDeliveryStatus.DELIVERED,
            claimed_until=None,
            provider_message_ref=provider_message_ref,
            delivered_at=now,
            last_error_code=None,
            updated_at=now,
        )

    def nack(
        self,
        *,
        claim_token: UUID,
        fencing_token: int,
        outcome: NotificationAttemptOutcome | str | None,
        error_code: NotificationAttemptErrorCode | str | None,
        next_attempt_at: datetime | None,
        now: datetime,
    ) -> NotificationDeliveryIntent:
        _aware(now, "nack time")
        self._assert_claim(claim_token, fencing_token, now)
        parsed_outcome = _enum(
            outcome or NotificationAttemptOutcome.RETRYABLE_FAILURE,
            NotificationAttemptOutcome,
            "attempt outcome",
        )
        parsed_error = _optional_enum(
            error_code, NotificationAttemptErrorCode, "last_error_code"
        )
        if parsed_outcome is NotificationAttemptOutcome.DELIVERED:
            raise InvalidTransition("delivered outcome must use ack")
        if parsed_error is None:
            raise ValidationError("failed delivery requires error_code")
        if parsed_outcome in {
            NotificationAttemptOutcome.RETRYABLE_FAILURE,
            NotificationAttemptOutcome.UNKNOWN_RESULT,
        }:
            if next_attempt_at is None:
                raise ValidationError("retryable delivery requires next_attempt_at")
            _aware(next_attempt_at, "next_attempt_at")
            if next_attempt_at < now:
                raise ValidationError("next_attempt_at cannot precede now")
            return replace(
                self,
                status=NotificationDeliveryStatus.RETRY_WAIT,
                next_attempt_at=next_attempt_at,
                claimed_until=None,
                claim_token=None,
                last_error_code=parsed_error,
                updated_at=now,
            )
        if next_attempt_at is not None:
            raise ValidationError("terminal delivery cannot have next_attempt_at")
        return replace(
            self,
            status=NotificationDeliveryStatus.EXHAUSTED,
            next_attempt_at=now,
            claimed_until=None,
            claim_token=None,
            last_error_code=parsed_error,
            exhausted_at=now,
            updated_at=now,
        )

    def expire_claim(self, *, now: datetime) -> NotificationDeliveryIntent:
        _aware(now, "claim expiry time")
        if self.status is not NotificationDeliveryStatus.CLAIMED:
            raise InvalidTransition("only a claimed delivery can expire")
        if self.claimed_until is None or now < self.claimed_until:
            raise InvalidTransition("delivery claim has not expired")
        return replace(
            self,
            status=NotificationDeliveryStatus.RETRY_WAIT,
            next_attempt_at=now,
            claimed_until=None,
            claim_token=None,
            fencing_token=self.fencing_token + 1,
            last_error_code=NotificationAttemptErrorCode.TIMEOUT,
            updated_at=now,
        )

    def suppress(
        self,
        *,
        reason: NotificationAttemptErrorCode | str,
        now: datetime,
    ) -> NotificationDeliveryIntent:
        _aware(now, "suppressed_at")
        if self.status is NotificationDeliveryStatus.DELIVERED:
            raise InvalidTransition("delivered delivery cannot be suppressed")
        return replace(
            self,
            status=NotificationDeliveryStatus.SUPPRESSED,
            claimed_until=None,
            claim_token=None,
            last_error_code=_enum(
                reason, NotificationAttemptErrorCode, "suppression reason"
            ),
            updated_at=now,
        )

    def next_delivery_round(self, *, now: datetime) -> NotificationDeliveryIntent:
        _aware(now, "delivery round created_at")
        if self.status not in {
            NotificationDeliveryStatus.EXHAUSTED,
            NotificationDeliveryStatus.SUPPRESSED,
        }:
            raise InvalidTransition("only terminal delivery can be requeued")
        next_round = self.delivery_round + 1
        return NotificationDeliveryIntent(
            id=notification_delivery_intent_id(
                workspace_id=self.workspace_id,
                notification_fact_id=self.notification_fact_id,
                notification_route_id=self.notification_route_id,
                delivery_round=next_round,
            ),
            workspace_id=self.workspace_id,
            notification_fact_id=self.notification_fact_id,
            notification_route_id=self.notification_route_id,
            channel_module_instance_id=self.channel_module_instance_id,
            delivery_round=next_round,
            contract_version=self.contract_version,
            status=NotificationDeliveryStatus.PENDING,
            attempts=0,
            next_attempt_at=now,
            claimed_until=None,
            claim_token=None,
            fencing_token=1,
            provider_message_ref=None,
            last_error_code=None,
            delivered_at=None,
            exhausted_at=None,
            created_at=now,
            updated_at=now,
        )

    def _assert_claim(
        self, claim_token: UUID, fencing_token: int, now: datetime
    ) -> None:
        _uuid(claim_token, "claim_token")
        _positive(fencing_token, "fencing_token")
        if self.status is not NotificationDeliveryStatus.CLAIMED:
            if fencing_token < self.fencing_token:
                raise StaleFencingToken("delivery fencing token is stale")
            raise InvalidTransition("delivery is not claimed")
        if self.claim_token != claim_token or self.fencing_token != fencing_token:
            raise StaleFencingToken("delivery claim or fencing token is stale")
        if self.claimed_until is not None and now > self.claimed_until:
            raise LeaseExpired("delivery claim lease has expired")


@dataclass(frozen=True, slots=True)
class NotificationDeliveryClaim:
    intent: NotificationDeliveryIntent
    attempt: NotificationDeliveryAttempt

    def __iter__(self):
        yield self.intent
        yield self.attempt

    @property
    def status(self) -> NotificationDeliveryStatus:
        return self.intent.status

    @property
    def claim_token(self) -> UUID | None:
        return self.intent.claim_token

    @property
    def fencing_token(self) -> int:
        return self.intent.fencing_token


def _business_status_for_fact(
    fact: NotificationFact,
) -> NotificationThreadBusinessStatus:
    if fact.priority in {
        NotificationPriority.ACTION,
        NotificationPriority.OPERATIONS,
    } and (
        fact.transition is not NotificationTransition.RECOVERED
    ):
        return NotificationThreadBusinessStatus.OPEN
    return NotificationThreadBusinessStatus.RESOLVED


def _identifier(value: object, name: str) -> str:
    if isinstance(value, UUID):
        return str(value)
    return _safe_text(value, name, _MAX_REASON_LENGTH)


def _optional_identifier(value: object | None, name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, name)


def _safe_text(value: object, name: str, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    if len(value) > maximum:
        raise ValidationError(f"{name} exceeds maximum length")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError(f"{name} contains unsupported control characters")
    if _SECRET_MARKER_PATTERN.search(value):
        raise ValidationError(f"{name} contains a forbidden secret-like marker")
    return value.strip()


def _opaque_ref(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    normalized = value.strip()
    if len(normalized) > _MAX_OPAQUE_REF_LENGTH:
        raise ValidationError(f"{name} exceeds maximum length")
    if _OPAQUE_REF_PATTERN.fullmatch(normalized) is None:
        raise ValidationError(f"{name} must use bounded opaque syntax")
    return normalized


def _optional_opaque_ref(value: object | None, name: str) -> str | None:
    if value is None:
        return None
    return _opaque_ref(value, name)


def _uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValidationError(f"{name} must be a UUID")
    return value


def _optional_uuid(value: object | None, name: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, name)


def _aware(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError(f"{name} must be timezone-aware")
    return value


def _optional_aware(value: object | None, name: str) -> datetime | None:
    if value is None:
        return None
    return _aware(value, name)


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be positive")
    return value


def _nonnegative(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{name} must be non-negative")
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


def _optional_enum[EnumType: StrEnum](
    value: object | None, enum_type: type[EnumType], name: str
) -> EnumType | None:
    if value is None:
        return None
    return _enum(value, enum_type, name)


def _hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _optional_hash(value: object | None, name: str) -> str | None:
    if value is None:
        return None
    return _hash(value, name)


def _freeze_mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError(f"{name} keys must be non-empty strings")
        frozen[key] = _freeze_json(item, f"{name}.{key}")
    return MappingProxyType(frozen)


def _freeze_json(value: object, name: str, *, depth: int = 0) -> object:
    if depth > 8:
        raise ValidationError(f"{name} is too deeply nested")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValidationError(f"{name} keys must be strings")
        return MappingProxyType(
            {
                _safe_text(key, f"{name} key", _MAX_REASON_LENGTH): _freeze_json(
                    item, f"{name}.{key}", depth=depth + 1
                )
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, name, depth=depth + 1) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            return _safe_text(value, name, _MAX_TEXT_LENGTH)
        return value
    raise ValidationError(f"{name} contains unsupported JSON value")
