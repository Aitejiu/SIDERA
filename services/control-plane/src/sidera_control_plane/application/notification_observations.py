from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.notification_policy import (
    NotificationAttention,
    NotificationMilestone,
    NotificationObservation,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.notifications import (
    NotificationKind,
    NotificationTransition,
)
from sidera_control_plane.domain.projections import ProjectionChange

NOTIFICATION_OBSERVATION_PROJECTION_TYPES = frozenset(
    {
        "planning_request",
        "task_overview",
        "task_graph",
        "subtask_workspace",
        "worker_item_workspace",
        "worker_item_activity",
        "executions",
        "collaboration_runtime",
        "collaboration_governance",
        "workers",
        "worker_runtime",
    }
)

NOTIFICATION_OBSERVATION_IGNORED_PROJECTION_TYPES = frozenset(
    {
        "notification_thread",
        "notification_delivery",
        "worker_trace",
        "worker_step",
        "heartbeat",
        "token_usage",
    }
)

_ROOT_TASK_PROJECTION_TYPES = frozenset(
    {
        "task_overview",
        "task_graph",
        "subtask_workspace",
        "collaboration_runtime",
        "collaboration_governance",
    }
)
_WORKER_ITEM_PROJECTION_TYPES = frozenset(
    {"worker_item_workspace", "worker_item_activity"}
)


class NotificationObservationSubjectType(StrEnum):
    ROOT_TASK = "root_task"
    PLANNING_REQUEST = "planning_request"
    WORKER_ITEM = "worker_item"
    EXECUTION = "execution"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class NotificationObservationSubject:
    subject_type: NotificationObservationSubjectType
    resource_id: UUID | None


@dataclass(frozen=True, slots=True)
class NotificationBaselineCandidate:
    change: ProjectionChange
    topic: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationAuthoritativeRead:
    """Safe, typed state returned by one bounded authoritative DB read."""

    recipient_type: str
    recipient_id: str
    subject_type: str
    subject_id: str
    topic: str
    root_task_id: UUID
    subtask_id: UUID | None
    worker_item_id: UUID | None
    kind: NotificationKind | str
    observed_status: str
    root_task_title: str
    subtask_path: tuple[str, ...]
    worker_item_title: str | None
    deep_link: str
    correlation_id: str
    transition: NotificationTransition | None = None
    requires_operator: bool = False
    automatic_recovery: bool = False
    completed_milestones: tuple[NotificationMilestone, ...] = ()
    attention_items: tuple[NotificationAttention, ...] = ()
    source_type: str = "sidera_web_chat"
    origin_route_active: bool = False
    baseline_import: bool = False
    source_resource_version: int | None = None
    authoritative_fields: Mapping[str, object] = field(default_factory=dict)


class NotificationObservationSource(Protocol):
    def observe(self, change: ProjectionChange) -> NotificationObservation | None:
        """Turn one invalidation into a validated observation, or skip it."""

    def list_baseline_candidates(
        self,
        workspace_id: UUID,
        *,
        source_cursor: int,
        now: datetime,
    ) -> tuple[NotificationBaselineCandidate, ...]:
        """Enumerate current subjects for an explicit baseline rebuild."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class NotificationRouteBackfillCandidate:
    fact_id: UUID
    thread_id: UUID
    route_id: UUID


class NotificationObservationComposer:
    """Build policy input from invalidation metadata plus typed current state."""

    def compose(
        self,
        change: ProjectionChange,
        authoritative_read: NotificationAuthoritativeRead,
    ) -> NotificationObservation:
        if not isinstance(change, ProjectionChange):
            raise ValidationError("notification observation requires a projection change")
        if not projection_change_is_observable(change):
            raise ValidationError("projection change is not observable")
        if not isinstance(authoritative_read, NotificationAuthoritativeRead):
            raise ValidationError("notification observation requires an authoritative read")
        if change.resource_id is None:
            raise ValidationError("observable projection change requires a resource id")
        source_resource_version = (
            authoritative_read.source_resource_version
            or change.resource_version
            or 1
        )
        fields = _canonical_fields(authoritative_read.authoritative_fields)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "subject_type": authoritative_read.subject_type,
                    "subject_id": authoritative_read.subject_id,
                    "topic": authoritative_read.topic,
                    "root_task_id": str(authoritative_read.root_task_id),
                    "subtask_id": (
                        str(authoritative_read.subtask_id)
                        if authoritative_read.subtask_id
                        else None
                    ),
                    "worker_item_id": (
                        str(authoritative_read.worker_item_id)
                        if authoritative_read.worker_item_id
                        else None
                    ),
                    "kind": _enum_value(authoritative_read.kind),
                    "observed_status": authoritative_read.observed_status,
                    "source_resource_version": source_resource_version,
                    "fields": fields,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return NotificationObservation(
            workspace_id=change.workspace_id,
            recipient_type=authoritative_read.recipient_type,
            recipient_id=authoritative_read.recipient_id,
            subject_type=authoritative_read.subject_type,
            subject_id=authoritative_read.subject_id,
            topic=authoritative_read.topic,
            root_task_id=authoritative_read.root_task_id,
            subtask_id=authoritative_read.subtask_id,
            worker_item_id=authoritative_read.worker_item_id,
            source_projection_cursor=change.cursor,
            source_projection_type=change.projection_type,
            source_resource_id=change.resource_id,
            source_resource_version=source_resource_version,
            kind=authoritative_read.kind,
            observed_status=authoritative_read.observed_status,
            authoritative_fingerprint=fingerprint,
            root_task_title=authoritative_read.root_task_title,
            subtask_path=authoritative_read.subtask_path,
            worker_item_title=authoritative_read.worker_item_title,
            deep_link=authoritative_read.deep_link,
            correlation_id=authoritative_read.correlation_id,
            transition=authoritative_read.transition,
            requires_operator=authoritative_read.requires_operator,
            automatic_recovery=authoritative_read.automatic_recovery,
            baseline_import=authoritative_read.baseline_import,
            source_type=authoritative_read.source_type,
            origin_route_active=authoritative_read.origin_route_active,
            completed_milestones=authoritative_read.completed_milestones,
            attention_items=authoritative_read.attention_items,
        )


def projection_change_is_observable(change: ProjectionChange) -> bool:
    if not isinstance(change, ProjectionChange):
        return False
    if change.projection_type in NOTIFICATION_OBSERVATION_IGNORED_PROJECTION_TYPES:
        return False
    return (
        change.projection_type in NOTIFICATION_OBSERVATION_PROJECTION_TYPES
        and change.resource_id is not None
    )


def observation_subject_key(
    change: ProjectionChange,
) -> NotificationObservationSubject | None:
    if not projection_change_is_observable(change):
        return None
    if change.projection_type in _ROOT_TASK_PROJECTION_TYPES:
        subject_type = NotificationObservationSubjectType.ROOT_TASK
    elif change.projection_type in _WORKER_ITEM_PROJECTION_TYPES:
        subject_type = NotificationObservationSubjectType.WORKER_ITEM
    elif change.projection_type == "planning_request":
        subject_type = NotificationObservationSubjectType.PLANNING_REQUEST
    elif change.projection_type == "executions":
        subject_type = NotificationObservationSubjectType.EXECUTION
    else:
        subject_type = NotificationObservationSubjectType.WORKSPACE
    return NotificationObservationSubject(subject_type, change.resource_id)


def _enum_value(value: object) -> str:
    return value.value if isinstance(value, StrEnum) else str(value)


def _canonical_fields(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError("authoritative_fields must be a mapping")
    return {
        str(key): _canonical_value(item)
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    }


def _canonical_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _canonical_fields(value)
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValidationError("authoritative_fields contains unsupported data")
