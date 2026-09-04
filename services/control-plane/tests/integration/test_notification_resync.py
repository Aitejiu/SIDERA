from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from sidera_control_plane.application.notification_reconciler import (
    NotificationReconciler,
)
from sidera_control_plane.persistence.models import (
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationReconciliationCursorModel,
    ProjectionChangeModel,
    TaskModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _task(task_id: UUID, workspace_id: UUID, *, title: str, status: str) -> TaskModel:
    return TaskModel(
        id=task_id,
        workspace_id=workspace_id,
        title=title,
        description=title,
        acceptance_criteria=["notification baseline"],
        status=status,
        priority=50,
        requested_by="notification-resync-test",
        version=1,
    )


def test_cursor_gap_requires_explicit_baseline_and_rebuilds_current_actions(
    database, workspace_id, uow_factory
) -> None:
    blocked_task_id = uuid4()
    completed_task_id = uuid4()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.session.add_all(
            (
                _task(
                    blocked_task_id,
                    workspace_id,
                    title="仍然阻塞的任务",
                    status="blocked",
                ),
                _task(
                    completed_task_id,
                    workspace_id,
                    title="已经完成的任务",
                    status="succeeded",
                ),
            )
        )
        uow.flush()
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="task_overview",
            resource_id=completed_task_id,
            change_type="task_completed",
            resource_version=1,
            correlation_id=uuid4(),
        )
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="task_overview",
            resource_id=blocked_task_id,
            change_type="task_blocked",
            resource_version=1,
            correlation_id=uuid4(),
        )

    with database.session() as session:
        session.execute(
            delete(ProjectionChangeModel).where(ProjectionChangeModel.cursor == 1)
        )
        session.commit()

    reconciler = NotificationReconciler(
        uow_factory,
        clock=lambda: NOW,
        owner="notification-resync-test",
    )
    gap = reconciler.reconcile_workspace(workspace_id)

    assert gap.diagnostic_code == "notification_resync_required"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(NotificationFactModel)) == 0

    baseline = reconciler.baseline_workspace(workspace_id)

    assert baseline.diagnostic_code is None
    assert baseline.facts_created == 1
    assert baseline.cursor == 2
    with database.session() as session:
        facts = session.scalars(
            select(NotificationFactModel).order_by(NotificationFactModel.created_at)
        ).all()
        assert len(facts) == 1
        assert facts[0].baseline_import is True
        assert facts[0].root_task_id == blocked_task_id
        assert session.scalar(
            select(func.count()).select_from(NotificationDeliveryIntentModel)
        ) == 0
        cursor = session.scalar(
            select(NotificationReconciliationCursorModel).where(
                NotificationReconciliationCursorModel.workspace_id == workspace_id,
                NotificationReconciliationCursorModel.policy_version == 1,
            )
        )
        assert cursor is not None
        assert cursor.last_projection_cursor == 2

    repeated = reconciler.baseline_workspace(workspace_id)

    assert repeated.diagnostic_code is None
    assert repeated.facts_created == 0
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(NotificationFactModel)) == 1
