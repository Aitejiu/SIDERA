from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.plan_revision_notifications import (
    PlanRevisionNotificationContext,
    PlanRevisionNotificationKind,
    PlanRevisionNotificationService,
)
from sidera_control_plane.domain.events import EventData

pytestmark = pytest.mark.integration

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
CORRELATION_ID = UUID("41000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("51000000-0000-0000-0000-000000000001")


def _event(
    event_type: str,
    *,
    aggregate_type: str = "planning_request",
    aggregate_id: UUID | None = None,
    payload: dict[str, object] | None = None,
) -> EventData:
    return EventData(
        id=EVENT_ID,
        workspace_id=WORKSPACE_ID,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id or uuid4(),
        aggregate_version=3,
        event_type=event_type,
        payload=payload or {},
        actor_type="system",
        actor_id="control-plane",
        causation_id=uuid4(),
        correlation_id=CORRELATION_ID,
    )


def _context(*, important: bool = True) -> PlanRevisionNotificationContext:
    return PlanRevisionNotificationContext(
        root_task_id=ROOT_TASK_ID,
        root_task_title="支付平台迁移",
        subtask_path=("后端", "数据库迁移"),
        deep_link=(
            "/workspaces/00000000-0000-0000-0000-000000000001/"
            "tasks/31000000-0000-0000-0000-000000000001/revisions/request-1"
        ),
        correlation_id=CORRELATION_ID,
        resource_version=3,
        important=important,
    )


def test_revision_notification_contains_human_scope_and_is_stable() -> None:
    service = PlanRevisionNotificationService()
    event = _event("PlanningProposalReady")

    notice = service.from_event(event, _context())
    replay = service.from_event(event, _context())

    assert notice is not None
    assert replay is not None
    assert notice.notification_id == replay.notification_id
    assert notice.kind is PlanRevisionNotificationKind.PROPOSAL_READY
    assert notice.root_task_title == "支付平台迁移"
    assert notice.subtask_path == ("后端", "数据库迁移")
    assert notice.action == "review_revision"
    assert notice.deep_link.endswith("/revisions/request-1")
    assert notice.correlation_id == CORRELATION_ID
    assert notice.source_event_id == EVENT_ID

    payload = notice.to_payload()
    assert payload["root_task_title"] == "支付平台迁移"
    assert payload["subtask_path"] == ["后端", "数据库迁移"]
    assert "payload" not in payload


def test_revision_milestones_map_to_actionable_notification_kinds() -> None:
    service = PlanRevisionNotificationService()

    assert service.from_event(
        _event("PlanningProposalConflicted", payload={"reason": "stale_base"}),
        _context(),
    ).kind is PlanRevisionNotificationKind.PROPOSAL_STALE
    assert service.from_event(
        _event("PlanningMaterialized"), _context()
    ).kind is PlanRevisionNotificationKind.GENERATION_PUBLISHED
    assert service.from_event(
        _event(
            "PlanTransitionReconciled",
            aggregate_type="plan_transition",
            payload={"status": "degraded"},
        ),
        _context(),
    ).kind is PlanRevisionNotificationKind.TRANSITION_DEGRADED
    assert service.from_event(
        _event(
            "PlanTransitionReconciled",
            aggregate_type="plan_transition",
            payload={"status": "converged"},
        ),
        _context(),
    ).kind is PlanRevisionNotificationKind.ALL_CONVERGED

    materialized = service.from_event(
        _event(
            "PlanningMaterialized",
            payload={"plan_transition_id": "61000000-0000-0000-0000-000000000001"},
        ),
        _context(),
    )
    created = service.from_event(
        _event(
            "PlanTransitionCreated",
            aggregate_type="plan_transition",
            aggregate_id=UUID("61000000-0000-0000-0000-000000000001"),
            payload={"status": "converging"},
        ),
        _context(),
    )
    assert materialized is not None
    assert created is not None
    assert materialized.notification_id == created.notification_id


def test_worker_convergence_requires_an_important_branch() -> None:
    service = PlanRevisionNotificationService()
    event = _event(
        "WorkerTransitionActivated",
        aggregate_type="worker_transition",
        payload={"status": "converged"},
    )

    assert service.from_event(event, _context(important=False)) is None
    notice = service.from_event(event, _context(important=True))
    assert notice is not None
    assert notice.kind is PlanRevisionNotificationKind.SUBTASK_CONVERGED


def test_runtime_poll_retry_and_lease_events_are_silent() -> None:
    service = PlanRevisionNotificationService()

    for event_type in (
        "LeaseRenewed",
        "ExecutionRetryQueued",
        "WorkerTransitionCreated",
        "WorkerTransitionWaitingForActivation",
    ):
        assert service.from_event(
            _event(event_type, aggregate_type="worker_transition"), _context()
        ) is None


def test_unknown_event_payload_is_never_copied_to_notice() -> None:
    service = PlanRevisionNotificationService()
    notice = service.from_event(
        _event(
            "WorkerTransitionBlockedUnknown",
            aggregate_type="worker_transition",
            payload={
                "status": "blocked_unknown",
                "worker_item_id": "private-worker-id",
                "patch_body": "secret patch body",
                "provider_raw_response": "secret provider output",
            },
        ),
        _context(),
    )

    assert notice is not None
    serialized = str(notice.to_payload())
    assert "private-worker-id" not in serialized
    assert "secret patch body" not in serialized
    assert "secret provider output" not in serialized
