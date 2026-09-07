from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sidera_control_plane.application.notification_policy import (
    NotificationAttention,
    NotificationObservation,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.notifications import NotificationTransition

RESULT_NOTIFICATION_EVENT_TYPES = frozenset(
    {
        "CodeResultCaptured",
        "IntegrationCandidatePrepared",
        "TaskResultReintegrationRequested",
        "VerificationExecutionScheduled",
        "VerificationRetryRequested",
        "VerificationResultFinished",
        "ResultReworkRequested",
        "ResultReworkAttentionRequired",
        "ResultReworkBudgetAuthorized",
        "ResultApplyRequested",
        "ResultApplyReceiptRecorded",
        "TaskAcceptanceRecorded",
        "TaskCompleted",
    }
)

_CHANGE_EVENT_TYPES = {
    "code_result_captured": "CodeResultCaptured",
    "integration_candidate_prepared": "IntegrationCandidatePrepared",
    "task_result_reintegration_requested": "TaskResultReintegrationRequested",
    "verification_execution_scheduled": "VerificationExecutionScheduled",
    "verification_retry_requested": "VerificationRetryRequested",
    "verification_result_finished": "VerificationResultFinished",
    "result_rework_requested": "ResultReworkRequested",
    "result_rework_attention_required": "ResultReworkAttentionRequired",
    "result_rework_budget_authorized": "ResultReworkBudgetAuthorized",
    "result_apply_requested": "ResultApplyRequested",
    "result_apply_receipt_recorded": "ResultApplyReceiptRecorded",
    "task_acceptance_recorded": "TaskAcceptanceRecorded",
    "task_completed": "TaskCompleted",
}

_ATTENTION_EVENTS = frozenset(
    {
        "VerificationResultFinished",
        "ResultReworkAttentionRequired",
        "ResultReworkBudgetAuthorized",
        "ResultApplyReceiptRecorded",
    }
)
_SAFE_KEY = re.compile(r"[^a-z0-9_.:-]+")


@dataclass(frozen=True, slots=True)
class _Target:
    subtask_id: UUID | None
    worker_item_id: UUID | None
    subtask_title: str | None
    worker_item_title: str | None
    resource_version: int
    cause_key: str


def result_notification_facts(
    event: object, overview: Mapping[str, object]
) -> tuple[NotificationObservation, ...]:
    """Build scoped result observations from one event and current result state.

    The event identity is deliberately not part of the semantic fingerprint. A
    replay or a different projection invalidation for the same authoritative
    candidate therefore cannot create another notification fact.
    """

    if not isinstance(overview, Mapping):
        raise ValidationError("result notification overview must be a mapping")
    event_type = _event_type(event)
    if event_type not in RESULT_NOTIFICATION_EVENT_TYPES:
        return ()
    if event_type == "TaskCompleted":
        # Root completion is owned by the existing TaskCompleted notification.
        return ()

    workspace_id = _uuid(
        overview.get("workspace_id"), "result notification workspace_id"
    )
    root_task_id = _uuid(
        overview.get("root_task_id"), "result notification root_task_id"
    )
    event_workspace_id = _optional_uuid(_event_value(event, "workspace_id"))
    if event_workspace_id is not None and event_workspace_id != workspace_id:
        raise ValidationError("result notification workspace scope mismatch")
    event_root_id = _optional_uuid(_event_value(event, "root_task_id"))
    if event_root_id is not None and event_root_id != root_task_id:
        raise ValidationError("result notification root scope mismatch")

    candidate = overview.get("current_candidate")
    candidate_id = _optional_uuid(
        candidate.get("candidate_id") if isinstance(candidate, Mapping) else None
    )
    candidate_hash = (
        _text(candidate.get("candidate_hash"))
        if isinstance(candidate, Mapping)
        else None
    )
    stage = (_text(overview.get("result_stage")) or "unknown").lower()
    result_version = _positive_int(overview.get("result_version")) or _positive_int(
        _event_value(event, "resource_version")
    ) or 1
    title = _text(overview.get("task_title")) or "任务成果"
    blocking_reasons = _safe_reasons(overview.get("blocking_reasons"))
    targets = _targets(overview.get("attention_items"), candidate_id, blocking_reasons)

    if event_type == "TaskAcceptanceRecorded":
        if candidate_id is None:
            return ()
        return (
            _observation(
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                title=title,
                candidate_id=candidate_id,
                candidate_hash=candidate_hash,
                stage="accepted",
                result_version=result_version,
                target=_Target(
                    subtask_id=None,
                    worker_item_id=None,
                    subtask_title=None,
                    worker_item_title=None,
                    resource_version=result_version,
                    cause_key=f"result:{candidate_id}:accepted",
                ),
                event=event,
                kind="result_acceptance_recorded",
                transition=NotificationTransition.COMPLETED,
                requires_operator=False,
                blocking_reasons=blocking_reasons,
                overview=overview,
            ),
        )

    if targets and event_type in _ATTENTION_EVENTS:
        return tuple(
            _observation(
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                title=title,
                candidate_id=candidate_id,
                candidate_hash=candidate_hash,
                stage=stage,
                result_version=result_version,
                target=target,
                event=event,
                kind="result_attention",
                transition=NotificationTransition.OPENED,
                requires_operator=True,
                blocking_reasons=blocking_reasons,
                overview=overview,
            )
            for target in targets
        )

    if candidate_id is None:
        return ()
    if stage == "awaiting_acceptance":
        target = _Target(
            subtask_id=None,
            worker_item_id=None,
            subtask_title=None,
            worker_item_title=None,
            resource_version=result_version,
            cause_key=f"result:{candidate_id}:awaiting_acceptance",
        )
        return (
            _observation(
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                title=title,
                candidate_id=candidate_id,
                candidate_hash=candidate_hash,
                stage=stage,
                result_version=result_version,
                target=target,
                event=event,
                kind="result_candidate_ready",
                transition=NotificationTransition.OPENED,
                requires_operator=True,
                blocking_reasons=blocking_reasons,
                overview=overview,
            ),
        )
    if stage in {"attention", "blocked", "unknown", "failed"} or (
        event_type in _ATTENTION_EVENTS and blocking_reasons
    ):
        cause = blocking_reasons[0] if blocking_reasons else stage
        target = _Target(
            subtask_id=None,
            worker_item_id=None,
            subtask_title=None,
            worker_item_title=None,
            resource_version=result_version,
            cause_key=f"result:{candidate_id}:{_safe_key(cause)}",
        )
        return (
            _observation(
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                title=title,
                candidate_id=candidate_id,
                candidate_hash=candidate_hash,
                stage=stage,
                result_version=result_version,
                target=target,
                event=event,
                kind="result_attention",
                transition=NotificationTransition.OPENED,
                requires_operator=True,
                blocking_reasons=blocking_reasons,
                overview=overview,
            ),
        )
    return ()


def _observation(
    *,
    workspace_id: UUID,
    root_task_id: UUID,
    title: str,
    candidate_id: UUID | None,
    candidate_hash: str | None,
    stage: str,
    result_version: int,
    target: _Target,
    event: object,
    kind: str,
    transition: NotificationTransition,
    requires_operator: bool,
    blocking_reasons: tuple[str, ...],
    overview: Mapping[str, object],
) -> NotificationObservation:
    event_cursor = _nonnegative_int(_event_value(event, "cursor"))
    correlation_id = _text(_event_value(event, "correlation_id")) or str(root_task_id)
    identity = {
        "root_task_id": str(root_task_id),
        "subtask_id": None if target.subtask_id is None else str(target.subtask_id),
        "worker_item_id": (
            None if target.worker_item_id is None else str(target.worker_item_id)
        ),
        "candidate_id": None if candidate_id is None else str(candidate_id),
        "candidate_hash": candidate_hash,
        "stage": stage,
        "result_version": result_version,
        "cause_key": target.cause_key,
        "blocking_reasons": blocking_reasons,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    deep_link = f"/workspaces/{workspace_id}/tasks/{root_task_id}"
    if candidate_id is not None:
        deep_link += f"?view=results&candidate={candidate_id}"
    if target.worker_item_id is not None:
        separator = "&" if "?" in deep_link else "?"
        deep_link += f"{separator}worker_item={target.worker_item_id}"
    attention_items = ()
    if target.worker_item_id is not None:
        attention_items = (
            NotificationAttention(
                kind=target.cause_key,
                resource_id=target.worker_item_id,
                resource_version=target.resource_version,
                requires_operator=requires_operator,
                safe_path=("worker_item", str(target.worker_item_id)),
            ),
        )
    return NotificationObservation(
        workspace_id=workspace_id,
        recipient_type="workspace_operator",
        recipient_id="local",
        subject_type=("worker_item" if target.worker_item_id is not None else "root_task"),
        subject_id=str(target.worker_item_id or root_task_id),
        topic=target.cause_key,
        root_task_id=root_task_id,
        subtask_id=target.subtask_id,
        worker_item_id=target.worker_item_id,
        source_projection_cursor=event_cursor,
        source_projection_type="result_overview",
        source_resource_id=root_task_id,
        source_resource_version=result_version,
        kind=kind,
        observed_status=stage,
        authoritative_fingerprint=fingerprint,
        root_task_title=title,
        subtask_path=(() if target.subtask_title is None else (target.subtask_title,)),
        worker_item_title=target.worker_item_title,
        deep_link=deep_link,
        correlation_id=correlation_id,
        transition=transition,
        requires_operator=requires_operator,
        automatic_recovery=False,
        source_type=_text(overview.get("source_type")) or "sidera_web_chat",
        origin_route_active=bool(overview.get("origin_route_active", False)),
        attention_items=attention_items,
    )


def _targets(
    value: object,
    candidate_id: UUID | None,
    blocking_reasons: tuple[str, ...],
) -> tuple[_Target, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[_Target] = []
    for item in value[:16]:
        if not isinstance(item, Mapping):
            raise ValidationError("result notification attention item is invalid")
        worker_item_id = _optional_uuid(item.get("worker_item_id"))
        if worker_item_id is None:
            continue
        cause = _text(item.get("cause_key"))
        if cause is None:
            reason = blocking_reasons[0] if blocking_reasons else "attention"
            prefix = f"result:{candidate_id}:" if candidate_id is not None else "result:"
            cause = prefix + _safe_key(reason)
        result.append(
            _Target(
                subtask_id=_optional_uuid(item.get("subtask_id")),
                worker_item_id=worker_item_id,
                subtask_title=_text(item.get("subtask_title")),
                worker_item_title=_text(item.get("worker_item_title")),
                resource_version=_positive_int(item.get("resource_version")) or 1,
                cause_key=_safe_key(cause),
            )
        )
    return tuple(result)


def _event_type(event: object) -> str | None:
    value = _event_value(event, "event_type")
    if not isinstance(value, str):
        value = _CHANGE_EVENT_TYPES.get(
            str(_event_value(event, "change_type") or "").lower()
        )
    return value if value in RESULT_NOTIFICATION_EVENT_TYPES else None


def _event_value(event: object, name: str) -> object:
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _uuid(value: object, name: str) -> UUID:
    parsed = _optional_uuid(value)
    if parsed is None:
        raise ValidationError(f"{name} must be a UUID")
    return parsed


def _optional_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _safe_reasons(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(_safe_key(item) for item in value[:16] if isinstance(item, str) and item.strip())


def _safe_key(value: str) -> str:
    normalized = _SAFE_KEY.sub("_", value.strip().lower()).strip("_")
    return normalized[:200] or "attention"


__all__ = ["RESULT_NOTIFICATION_EVENT_TYPES", "result_notification_facts"]
