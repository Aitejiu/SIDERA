from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.domain.collaboration_governance import GovernanceTransport
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    EventModel,
    RuntimeIntentApplicationModel,
    RuntimeReleaseModel,
)

from .test_collaboration_decision_application import _invoker, _service
from .test_collaboration_evaluation_recovery import _prepare

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_valid_held_intent_creates_deferred_application_without_release(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    hold = CollaborationGovernanceHoldModel(
        id=uuid4(),
        workspace_id=workspace_id,
        root_task_id=prepared.run.root_task_id,
        run_id=prepared.run.id,
        plan_snapshot_id=prepared.run.plan_snapshot_id,
        plan_generation=prepared.run.plan_generation,
        plan_content_hash=prepared.run.plan_content_hash,
        worker_item_id=ids["worker_item"],
        status="active",
        created_by_actor_id="developer",
        created_via=GovernanceTransport.WEB.value,
        reason="inspect the result before releasing",
        resource_version=1,
    )
    with database.session() as session:
        session.add(hold)
        session.commit()

    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared,
        now=NOW,
    )
    result = _service(uow_factory).apply(prepared, invocation, now=NOW)

    assert result.status == "deferred"
    assert result.releases == ()
    assert len(result.proposals) == 1
    assert result.applications[0].status.value == "deferred"
    assert result.applications[0].applied_at is None

    with database.session() as session:
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.run_id == prepared.run.id,
            )
        ).all()
        applications = session.scalars(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
            )
        ).all()
        events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.correlation_id == prepared.run.id,
                EventModel.event_type == "CollaborationReleaseProposalCreated",
            )
        ).all()

    assert releases == []
    assert len(applications) == 1
    assert len(events) == 1
    assert "inspect the result" not in str(events[0].payload)
    assert "projection" not in str(events[0].payload).lower()
