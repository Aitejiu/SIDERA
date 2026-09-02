from uuid import UUID

from sidera_control_plane.application.handoff_notifications import (
    HandoffNotificationContext,
    HandoffNotificationKind,
    HandoffNotificationService,
)
from sidera_control_plane.domain.events import EventData

EVENT_ID = UUID("a1000000-0000-0000-0000-000000000001")
TRANSITION_ID = UUID("a2000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("a3000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("a4000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("a5000000-0000-0000-0000-000000000001")
CORRELATION_ID = UUID("a6000000-0000-0000-0000-000000000001")


def _event(event_type: str, payload: dict[str, object] | None = None) -> EventData:
    return EventData(
        id=EVENT_ID,
        workspace_id=WORKSPACE_ID,
        aggregate_type="handoff_transition",
        aggregate_id=TRANSITION_ID,
        aggregate_version=3,
        event_type=event_type,
        payload=payload or {"status": "blocked_unknown"},
        actor_type="system",
        actor_id="coordinator",
        causation_id=CORRELATION_ID,
        correlation_id=CORRELATION_ID,
    )


def _context(*, important: bool = True) -> HandoffNotificationContext:
    return HandoffNotificationContext(
        root_task_id=ROOT_TASK_ID,
        root_task_title="支付重构",
        subtask_path=("支付重构", "回调幂等", "回调实现"),
        worker_item_id=WORKER_ITEM_ID,
        worker_item_title="回调实现",
        deep_link=f"/tasks/{ROOT_TASK_ID}/worker-items/{WORKER_ITEM_ID}",
        correlation_id=CORRELATION_ID,
        resource_version=4,
        important=important,
    )


def test_handoff_attention_notification_targets_worker_item() -> None:
    notice = HandoffNotificationService().from_event(
        _event(
            "HandoffBlocked",
            {
                "status": "blocked_unknown",
                "provider_raw_response": "must never be copied",
            },
        ),
        _context(),
    )

    assert notice is not None
    assert notice.kind is HandoffNotificationKind.ATTENTION_REQUIRED
    assert notice.worker_item_id == WORKER_ITEM_ID
    assert notice.deep_link.endswith(str(WORKER_ITEM_ID))
    assert "must never be copied" not in str(notice.to_payload())


def test_handoff_milestone_notifications_are_sparse_and_deterministic() -> None:
    service = HandoffNotificationService()
    activated = service.from_event(_event("HandoffTargetActivated", {}), _context())
    converged = service.from_event(
        _event("HandoffConverged", {"private_bundle": "secret"}), _context()
    )

    assert activated is not None
    assert activated.kind is HandoffNotificationKind.TARGET_TOOK_OVER
    assert converged is not None
    assert converged.kind is HandoffNotificationKind.CONVERGED
    assert service.from_event(
        _event("HandoffConverged", {}), _context(important=False)
    ) is None
    assert activated.notification_id == service.from_event(
        _event("HandoffTargetActivated", {"irrelevant": "changed"}), _context()
    ).notification_id


def test_non_user_actionable_handoff_events_are_silent() -> None:
    for event_type in (
        "HandoffProposalPrepared",
        "HandoffConfirmed",
        "HandoffCheckpointCaptured",
        "HandoffSourceStopped",
        "HandoffWorkspaceFrozen",
        "HandoffTargetExecutionCreated",
        "HandoffContextBundleCreated",
        "HandoffWorkspaceTransferred",
        "HandoffTargetSessionStarted",
        "HandoffContextAcknowledged",
        "HandoffInputTransferred",
    ):
        assert HandoffNotificationService().from_event(
            _event(event_type, {}), _context()
        ) is None
