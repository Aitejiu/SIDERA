from __future__ import annotations

import json
from uuid import UUID

import pytest
from tests.integration.test_plan_transition_repository import (
    _seed_records,
    _transition,
    _worker_transition,
)

from sidera_control_plane.config import Settings
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plan_transitions import (
    PlanTransitionStatus,
    WorkerTransitionStatus,
    WriterState,
)
from sidera_control_plane.persistence.models import RootTaskPlanHeadModel
from sidera_control_plane.persistence.plan_repository import PlanRepository
from sidera_control_plane.persistence.plan_transition_repository import (
    PlanTransitionRepository,
    WorkerTransitionRepository,
)
from sidera_control_plane.plan_transition_worker import (
    build_plan_transition_worker,
    summary_json,
)

pytestmark = pytest.mark.integration

ROOT_TASK_ID = UUID("43000000-0000-0000-0000-000000000001")


def test_plan_transition_worker_converges_preserve_transition(
    database, database_url: str, workspace_id: UUID
) -> None:
    request_id, proposal_id, first_id, second_id, _ = _seed_records(
        database, workspace_id
    )
    transition = _transition(
        workspace_id, request_id, proposal_id, first_id, second_id
    )
    worker_transition = _worker_transition(workspace_id)
    with database.session() as session:
        snapshot = PlanRepository(session).get(second_id, workspace_id)
        assert snapshot is not None
        session.add(
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=ROOT_TASK_ID,
                active_snapshot_id=snapshot.id,
                active_generation=snapshot.generation,
                active_snapshot_hash=plan_snapshot_content_hash(snapshot),
                version=1,
            )
        )
        PlanTransitionRepository(session).add(transition)
        session.flush()
        WorkerTransitionRepository(session).add(worker_transition)
        session.commit()

    runtime = build_plan_transition_worker(
        Settings(
            database_url=database_url,
            local_actor_id="integration-plan-transition",
        )
    )
    try:
        result = runtime.coordinator.run_once(limit=10)
    finally:
        runtime.database.dispose()

    assert result.claimed == 1
    assert result.converged == 1
    assert result.deferred == 0
    assert result.failed == 0

    with database.session() as session:
        loaded_worker = WorkerTransitionRepository(session).get(
            worker_transition.id, workspace_id
        )
        loaded_plan = PlanTransitionRepository(session).get(
            transition.id, workspace_id
        )
        assert loaded_worker is not None
        assert loaded_worker.status is WorkerTransitionStatus.CONVERGED
        assert loaded_worker.writer_state is WriterState.CLOSED
        assert loaded_plan is not None
        assert loaded_plan.status is PlanTransitionStatus.CONVERGED


def test_plan_transition_worker_summary_is_machine_readable() -> None:
    from sidera_control_plane.application.plan_transition_coordinator import (
        PlanTransitionRunResult,
    )

    payload = json.loads(
        summary_json(
            PlanTransitionRunResult(
                claimed=1,
                converged=1,
                deferred=0,
                failed=0,
                reconciled=1,
            )
        )
    )

    assert payload == {
        "claimed": 1,
        "component": "plan-transition-worker",
        "converged": 1,
        "deferred": 0,
        "failed": 0,
        "reconciled": 1,
        "status": "ok",
    }
