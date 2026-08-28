from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_revision_confirmation import (
    _base_plan,
    _confirm_command,
    _seed_revision_confirmation,
)
from test_scheduler import register_runtime_modules_and_configure_worker

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.plan_transition_gate import (
    PlanTransitionGate,
    PlanTransitionGateDecision,
    PlanTransitionGateOperation,
)
from sidera_control_plane.application.ports import WorkerItemPlacement
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ActivationGate,
    DependencyEnforcement,
    ExecutionRequestDependency,
    UpstreamActivationFact,
)
from sidera_control_plane.domain.plan_transitions import WorkerTransitionStatus
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.models import ExecutionModel, SchedulingBlockModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

WORKSPACE_ID = UUID("83000000-0000-0000-0000-000000000001")
GENERATION = 2
A_ID = UUID("84000000-0000-0000-0000-000000000001")
B_ID = UUID("84000000-0000-0000-0000-000000000002")
C_ID = UUID("84000000-0000-0000-0000-000000000003")
D_ID = UUID("84000000-0000-0000-0000-000000000004")


@dataclass
class BranchPlacements:
    generations: dict[UUID, int]

    def get_current_placement(self, worker_item_id, workspace_id, **_kwargs):
        assert workspace_id == WORKSPACE_ID
        generation = self.generations.get(worker_item_id)
        return (
            SimpleNamespace(plan_generation=generation)
            if generation is not None
            else None
        )


@dataclass
class BranchTransitions:
    active: dict[UUID, object]

    def get_active_for_worker_item(self, worker_item_id, workspace_id, **_kwargs):
        assert workspace_id == WORKSPACE_ID
        return self.active.get(worker_item_id)


class BranchUnitOfWork:
    def __init__(self) -> None:
        self.worker_items = BranchPlacements(
            {A_ID: GENERATION, B_ID: GENERATION, C_ID: GENERATION, D_ID: GENERATION}
        )
        self.worker_transitions = BranchTransitions(
            {
                B_ID: SimpleNamespace(
                    id=uuid4(), status=WorkerTransitionStatus.BLOCKED_UNKNOWN
                )
            }
        )


def _gate(uow: BranchUnitOfWork, worker_item_id: UUID):
    return PlanTransitionGate(uow).evaluate(
        worker_item_id=worker_item_id,
        workspace_id=WORKSPACE_ID,
        plan_generation=GENERATION,
        operation=PlanTransitionGateOperation.SCHEDULE,
        target_id=uuid4(),
    )


def test_transition_fences_isolate_parallel_branches_and_dependency_readiness() -> None:
    uow = BranchUnitOfWork()

    # A preserve has converged and D is unrelated; both remain runnable while B
    # is frozen by an unknown external outcome.
    assert _gate(uow, A_ID).decision is PlanTransitionGateDecision.ALLOW
    blocked_b = _gate(uow, B_ID)
    assert blocked_b.decision is PlanTransitionGateDecision.BLOCK
    assert blocked_b.reason == "worker_transition_unknown"
    assert _gate(uow, D_ID).decision is PlanTransitionGateDecision.ALLOW

    # C is not fenced itself, but its hard dependency keeps it blocked until B
    # has a successful, materialized predecessor fact.
    dependency = ExecutionRequestDependency(
        downstream_request_id=C_ID,
        upstream_request_id=B_ID,
        enforcement=DependencyEnforcement.HARD,
    )
    blocked_c = ActivationGate.evaluate(
        dependencies=(dependency,),
        upstream_facts=(UpstreamActivationFact(request_id=B_ID),),
    )
    assert not blocked_c.ready
    assert blocked_c.reason == "dependencies_blocked"
    assert blocked_c.blocked_upstream_request_ids == (B_ID,)

    ready_c = ActivationGate.evaluate(
        dependencies=(dependency,),
        upstream_facts=(
            UpstreamActivationFact(
                request_id=B_ID,
                execution_status=ExecutionStatus.SUCCEEDED,
                execution_lineage_id=uuid4(),
            ),
        ),
    )
    assert ready_c.ready


def test_old_generation_is_obsolete_without_freezing_the_new_head() -> None:
    uow = BranchUnitOfWork()
    old_generation = PlanTransitionGate(uow).evaluate(
        worker_item_id=B_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=GENERATION - 1,
        operation=PlanTransitionGateOperation.ACTIVATE,
    )
    new_generation = PlanTransitionGate(uow).evaluate(
        worker_item_id=A_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=GENERATION,
        operation=PlanTransitionGateOperation.ACTIVATE,
    )

    assert old_generation.decision is PlanTransitionGateDecision.OBSOLETE
    assert old_generation.reason == "plan_generation_stale"
    assert new_generation.decision is PlanTransitionGateDecision.ALLOW


def test_scheduler_keeps_an_unrelated_branch_running_while_unknown_branch_is_frozen(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    from fastapi.testclient import TestClient

    seed = _seed_revision_confirmation(database, workspace_id, snapshot=_base_plan(workspace_id))
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.post(
            "/api/v1/commands",
            json=_confirm_command(seed, idempotency_key="branch-isolation-confirm"),
        )
    assert response.status_code == 201, response.text

    run_at = datetime.now(UTC)
    independent_item_id = uuid4()
    target_snapshot_id = UUID(response.json()["data"]["plan_snapshot_id"])
    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.worker_transitions.get_active_for_worker_item(
            seed["worker_item"].id,
            workspace_id,
            for_update=True,
        )
        assert transition is not None
        previous_transition_version = transition.version
        transition.wait_for_safe_boundary(now=run_at)
        transition.begin_apply(now=run_at)
        transition.mark_unknown(failure_code="test_patch_unknown", now=run_at)
        uow.worker_transitions.save(transition, previous_transition_version)

        subtask = uow.tasks.get(seed["snapshot"].subtasks[0].task_id, workspace_id, for_update=True)
        assert subtask is not None
        previous_task_version = subtask.version
        subtask.mark_ready()
        uow.tasks.save(subtask, previous_task_version)

        independent_item = WorkerItem.create(
            item_id=independent_item_id,
            workspace_id=workspace_id,
            root_task_id=seed["root_task"].id,
            title="Independent worker",
            responsibility="Run an unrelated branch.",
            now=run_at,
        )
        uow.worker_items.add(independent_item)
        uow.worker_items.add_placement(
            WorkerItemPlacement(
                plan_snapshot_id=target_snapshot_id,
                plan_generation=2,
                workspace_id=workspace_id,
                root_task_id=seed["root_task"].id,
                worker_item_id=independent_item_id,
                worker_item_key="independent",
                subtask_key="implementation",
                subtask_task_id=subtask.id,
                role="independent",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                created_at=run_at,
            )
        )

        worker = Worker.register(
            worker_id=uuid4(),
            workspace_id=workspace_id,
            adapter_type="fake",
            display_name="Independent branch worker",
            capabilities=frozenset({"python"}),
            capacity=1,
            cost_per_hour_microunits=0,
            startup_latency_ms=0,
            now=run_at,
        )
        uow.workers.add(worker)
        uow.flush()
        register_runtime_modules_and_configure_worker(uow, worker)
        frozen_execution_id = uuid4()
        independent_execution_id = uuid4()
        uow.executions.add(
            Execution.create(
                execution_id=frozen_execution_id,
                workspace_id=workspace_id,
                task_id=subtask.id,
                worker_item_id=seed["worker_item"].id,
                lineage_id=frozen_execution_id,
                attempt=1,
                max_attempts=1,
                purpose="Frozen branch work",
                capability_requirements=frozenset({"python"}),
                pinned_worker_id=None,
                not_before=run_at,
            )
        )
        uow.executions.add(
            Execution.create(
                execution_id=independent_execution_id,
                workspace_id=workspace_id,
                task_id=subtask.id,
                worker_item_id=independent_item_id,
                lineage_id=independent_execution_id,
                attempt=1,
                max_attempts=1,
                purpose="Independent branch work",
                capability_requirements=frozenset({"python"}),
                pinned_worker_id=None,
                not_before=run_at,
            )
        )

    result = Scheduler(
        uow_factory=uow_factory,
        policy=LowestCostWorkerPolicy(),
        clock=lambda: run_at,
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
    ).run_once(limit=4)

    assert result.assigned == 1
    assert result.deferred >= 1
    with database.session() as session:
        frozen = session.get(ExecutionModel, frozen_execution_id)
        independent = session.get(ExecutionModel, independent_execution_id)
        assert frozen is not None and independent is not None
        assert frozen.status == ExecutionStatus.QUEUED.value
        assert independent.status == ExecutionStatus.LEASED.value
        block = session.query(SchedulingBlockModel).filter_by(
            execution_id=frozen_execution_id
        ).one()
        assert block.reason_code == "plan_transition_blocked"
