from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.notification_observations import (
    NotificationAuthoritativeRead,
    NotificationObservationComposer,
    observation_subject_key,
    projection_change_is_observable,
)
from sidera_control_plane.domain.notifications import (
    NotificationKind,
    NotificationTransition,
)
from sidera_control_plane.domain.projections import ProjectionChange
from sidera_control_plane.persistence.notification_observation_store import (
    SqlAlchemyNotificationObservationStore,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("10000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("10000000-0000-0000-0000-000000000002")


def _change(
    projection_type: str,
    *,
    resource_id: UUID | None = ROOT_TASK_ID,
    cursor: int = 7,
) -> ProjectionChange:
    return ProjectionChange(
        workspace_id=WORKSPACE_ID,
        cursor=cursor,
        schema_version=1,
        projection_type=projection_type,
        resource_id=resource_id,
        change_type="task_attention",
        resource_version=4,
        occurred_at=NOW,
        correlation_id=uuid4(),
    )


def _read() -> NotificationAuthoritativeRead:
    return NotificationAuthoritativeRead(
        recipient_type="workspace_operator",
        recipient_id="local",
        subject_type="root_task",
        subject_id=str(ROOT_TASK_ID),
        topic="execution_attention",
        root_task_id=ROOT_TASK_ID,
        subtask_id=None,
        worker_item_id=None,
        kind=NotificationKind.EXECUTION_ATTENTION,
        observed_status="attention",
        root_task_title="实现通知网关",
        subtask_path=(),
        worker_item_title=None,
        deep_link=f"/workspaces/{WORKSPACE_ID}/tasks/{ROOT_TASK_ID}",
        correlation_id="source-correlation",
        transition=NotificationTransition.OPENED,
        requires_operator=True,
        automatic_recovery=False,
        completed_milestones=(),
        attention_items=(),
        source_type="sidera_web_chat",
        baseline_import=False,
    )


def test_composer_creates_typed_observation_from_authoritative_read() -> None:
    observation = NotificationObservationComposer().compose(
        _change("task_overview"), _read()
    )

    assert observation.workspace_id == WORKSPACE_ID
    assert observation.source_projection_cursor == 7
    assert observation.source_projection_type == "task_overview"
    assert observation.source_resource_id == ROOT_TASK_ID
    assert observation.source_resource_version == 4
    assert observation.kind == NotificationKind.EXECUTION_ATTENTION.value
    assert observation.root_task_title == "实现通知网关"
    assert observation.correlation_id == "source-correlation"


@pytest.mark.parametrize(
    "projection_type",
    (
        "notification_thread",
        "notification_delivery",
        "worker_trace",
        "worker_step",
        "heartbeat",
        "token_usage",
    ),
)
def test_notification_observation_filters_own_and_noisy_changes(
    projection_type: str,
) -> None:
    change = _change(projection_type)

    assert not projection_change_is_observable(change)
    assert observation_subject_key(change) is None


def test_equivalent_root_projection_invalidations_share_one_subject_key() -> None:
    overview = _change("task_overview")
    graph = _change("task_graph", cursor=8)

    assert observation_subject_key(overview) == observation_subject_key(graph)


def test_task_overview_resolves_a_placed_subtask_to_its_root_scope() -> None:
    store = object.__new__(SqlAlchemyNotificationObservationStore)
    store._session = Mock()
    store._session.scalar.return_value = SimpleNamespace(root_task_id=ROOT_TASK_ID)

    root_task_id, subtask_id = store._task_scope(
        _change("task_overview", resource_id=SUBTASK_ID)
    )

    assert root_task_id == ROOT_TASK_ID
    assert subtask_id == SUBTASK_ID


def test_explicit_worker_input_is_immediate_attention() -> None:
    store = object.__new__(SqlAlchemyNotificationObservationStore)

    kind, topic, status, transition, requires_operator, automatic_recovery = (
        store._classify(
            _change("worker_item_activity"),
            root_task_status="pending",
            planning_request_status=None,
            worker_item_status="active",
            execution_status="running",
            scheduling_block=None,
            result_input_change=False,
            worker_requires_user_input=True,
        )
    )

    assert kind == NotificationKind.EXECUTION_ATTENTION.value
    assert topic == "execution_attention"
    assert status == "running"
    assert transition is None
    assert requires_operator is True
    assert automatic_recovery is False


def test_completed_worker_session_recovers_the_execution_attention_topic() -> None:
    store = object.__new__(SqlAlchemyNotificationObservationStore)

    kind, topic, status, transition, requires_operator, automatic_recovery = (
        store._classify(
            _change("worker_item_activity", resource_id=uuid4()),
            root_task_status="waiting",
            planning_request_status=None,
            worker_item_status="active",
            execution_status=None,
            scheduling_block=None,
            result_input_change=False,
            worker_requires_user_input=False,
            worker_execution_completed=True,
        )
    )

    assert kind == NotificationKind.EXECUTION_ATTENTION.value
    assert topic == "execution_attention"
    assert status == "active"
    assert transition is NotificationTransition.RECOVERED
    assert requires_operator is False
    assert automatic_recovery is False
