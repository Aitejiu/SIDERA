"""Deterministic R6.2 fake handoff vertical contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select

from sidera_control_plane.application.handoff_candidates import HandoffCandidateService
from sidera_control_plane.application.handoff_commands import HandoffCommandError
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.domain.execution_requests import ExecutionRequestStatus
from sidera_control_plane.domain.handoffs import HandoffPhase
from sidera_control_plane.persistence.models import (
    ContextPackageAcknowledgementModel,
    ContextPackageModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionRequestModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    RootTaskPlanHeadModel,
    TaskModel,
    WorkerItemModel,
    WorkerModel,
    WorkerRuntimeConfigurationModel,
)
from tests.e2e.r6_2_fake_runtime import FakeHandoffRuntime, FaultPoint
from tests.e2e.seed_r6_2_handoff import G4Scenario, seed_g4_fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_g4_seed_has_exact_handoff_preconditions(database, uow_factory, workspace_id: UUID) -> None:
    fixture = seed_g4_fixture(database, workspace_id, scenario=G4Scenario.NORMAL, now=NOW, reset=True)

    with database.session() as session:
        root = session.get(TaskModel, fixture.root_task_id)
        assert root is not None
        assert root.status == "running"

        subtasks = session.scalars(
            select(TaskModel).where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.id.in_(tuple(fixture.subtask_ids.values())),
            )
        ).all()
        assert {task.title for task in subtasks} == {"Subtask A", "Subtask B", "Subtask C"}
        assert {task.status for task in subtasks} == {"running", "waiting"}

        items = session.scalars(
            select(WorkerItemModel).where(
                WorkerItemModel.workspace_id == workspace_id,
                WorkerItemModel.id.in_(tuple(fixture.worker_item_ids.values())),
            )
        ).all()
        assert {item.stable_key for item in items} == {"worker-a", "worker-b", "worker-c"}
        assert {item.status for item in items} == {"active"}

        head = session.get(RootTaskPlanHeadModel, (workspace_id, fixture.root_task_id))
        assert head is not None
        assert head.active_generation == fixture.plan_generation == 1
        assert head.active_snapshot_id == fixture.plan_snapshot_id
        assert head.active_snapshot_hash == fixture.plan_head_hash

        source_execution = session.get(ExecutionModel, fixture.source_execution_id)
        assert source_execution is not None
        assert source_execution.status == "running"
        assert source_execution.worker_item_id == fixture.worker_item_ids["b"]
        source_session = session.get(ExecutionSessionModel, fixture.source_session_id)
        assert source_session is not None
        assert source_session.status == "active"
        assert source_session.execution_id == fixture.source_execution_id
        assert source_session.last_sequence == 1
        assert source_session.input_mode == "queued"

        source_manifest = session.get(ExecutionWorkspaceModel, fixture.source_manifest_id)
        assert source_manifest is not None
        assert source_manifest.lifecycle == "active"
        assert source_manifest.fencing_token == fixture.source_fencing_token

        package = session.get(ContextPackageModel, fixture.source_package_id)
        assert package is not None
        assert package.package_hash == fixture.source_package_hash
        assert package.consumer_id == fixture.source_execution_id
        assert package.context_version == fixture.source_context_version
        acknowledgement = session.get(
            ContextPackageAcknowledgementModel,
            (fixture.source_package_id, "worker", fixture.source_execution_id),
        )
        assert acknowledgement is not None
        assert acknowledgement.acknowledged_package_hash == fixture.source_package_hash
        assert acknowledgement.context_version == fixture.source_context_version

        inputs = session.scalars(
            select(ExecutionInputModel).where(
                ExecutionInputModel.workspace_id == workspace_id,
                ExecutionInputModel.execution_id == fixture.source_execution_id,
            )
        ).all()
        assert {item.status for item in inputs} == {"delivered", "queued"}
        assert sum(item.status == "delivered" for item in inputs) == 1
        assert sum(item.status == "queued" for item in inputs) == 1

        targets = session.scalars(
            select(WorkerModel).where(
                WorkerModel.workspace_id == workspace_id,
                WorkerModel.id.in_(fixture.target_worker_ids),
            )
        ).all()
        assert len(targets) == 2
        assert {worker.status for worker in targets} == {"idle"}
        assert {worker.adapter_type for worker in targets} == {"fake"}
        assert session.scalar(
            select(WorkerRuntimeConfigurationModel.worker_id).where(
                WorkerRuntimeConfigurationModel.workspace_id == workspace_id,
                WorkerRuntimeConfigurationModel.worker_id.in_(fixture.target_worker_ids),
            )
        ) is not None

        requests = session.scalars(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.workspace_id == workspace_id,
            )
        ).all()
        assert {request.worker_item_key for request in requests} == {
            "worker-a",
            "worker-b",
            "worker-c",
        }
        source_request = next(
            request for request in requests if request.worker_item_key == "worker-b"
        )
        assert source_request.status == "materialized"
        assert source_request.execution_lineage_id == fixture.source_execution_id
        assert source_request.first_execution_id == fixture.source_execution_id

    candidates = HandoffCandidateService(
        uow_factory,
        policy=LowestCostWorkerPolicy(),
    ).inspect(
        workspace_id=workspace_id,
        worker_item_id=fixture.worker_item_ids["b"],
        now=NOW,
    )
    assert len(candidates.candidates) == 3
    assert sum(candidate.eligible for candidate in candidates.candidates) == 2
    assert sum(candidate.recommended for candidate in candidates.candidates) == 1
    assert fixture.adapter_contract == "sidera.worker-adapter.handoff.v1"
    assert fixture.workspace_contract == "sidera.workspace-provider.reuse-exact.v1"


def test_g4_fake_handoff_converges_without_changing_task_topology(
    database,
    uow_factory,
    workspace_id: UUID,
) -> None:
    fixture = seed_g4_fixture(database, workspace_id, scenario=G4Scenario.NORMAL, now=NOW, reset=True)
    runtime = FakeHandoffRuntime(database, workspace_id, fixture, now=NOW)
    proposal = runtime.prepare_handoff()
    transition = runtime.confirm_handoff(proposal)
    runtime.run_until_converged(transition.id)

    with database.session() as session:
        root = session.get(TaskModel, fixture.root_task_id)
        assert root is not None
        assert root.status == "running"
        assert session.scalar(
            select(ExecutionModel.id).where(
                ExecutionModel.worker_item_id == fixture.worker_item_ids["a"],
                ExecutionModel.status == "running",
            )
        ) == fixture.worker_a_execution_id
        assert session.scalar(
            select(ExecutionModel.id).where(
                ExecutionModel.worker_item_id == fixture.worker_item_ids["c"],
            )
        ) is None


def test_g4_revised_input_stales_proposal_and_downstream_waits_for_new_lineage(
    database,
    uow_factory,
    workspace_id: UUID,
) -> None:
    fixture = seed_g4_fixture(database, workspace_id, scenario=G4Scenario.NORMAL, now=NOW, reset=True)
    runtime = FakeHandoffRuntime(database, workspace_id, fixture, now=NOW)

    stale_proposal = runtime.prepare_handoff()
    runtime.update_pending_source_input()
    with pytest.raises(HandoffCommandError) as error:
        runtime.confirm_handoff(stale_proposal)
    assert error.value.code == "handoff_proposal_stale"
    with uow_factory() as uow:
        stale = uow.handoff_proposals.get(stale_proposal.id, workspace_id)
    assert stale is not None
    assert stale.status.value == "prepared"
    assert stale.stale_reason is None

    proposal = runtime.prepare_handoff()
    transition = runtime.confirm_handoff(proposal)
    runtime.run_until_converged(transition.id)
    runtime.attach_c_dependency_to_target(transition.id)

    runtime.run_activation()
    with uow_factory() as uow:
        downstream = uow.execution_requests.get(fixture.request_c_id, workspace_id)
        target = uow.execution_requests.get_by_handoff_transition(transition.id, workspace_id)
    assert downstream is not None
    assert downstream.status is ExecutionRequestStatus.BLOCKED
    assert downstream.gate_reason == "dependencies_blocked"
    assert target is not None
    with database.session() as session:
        assert session.scalar(
            select(ExecutionModel.id).where(
                ExecutionModel.worker_item_id == fixture.worker_item_ids["c"],
            )
        ) is None

    runtime.complete_target_execution(transition.id)
    runtime.run_activation()
    with uow_factory() as uow:
        downstream = uow.execution_requests.get(fixture.request_c_id, workspace_id)
    assert downstream is not None
    assert downstream.status is ExecutionRequestStatus.MATERIALIZED
    assert downstream.first_execution_id is not None


def test_g4_target_start_unknown_is_inspected_absent_and_retried_with_new_attempt(
    database,
    workspace_id: UUID,
) -> None:
    fixture = seed_g4_fixture(
        database,
        workspace_id,
        scenario=G4Scenario.TARGET_START_UNKNOWN,
        now=NOW,
        reset=True,
    )
    runtime = FakeHandoffRuntime(database, workspace_id, fixture, now=NOW)
    proposal = runtime.prepare_handoff()
    transition = runtime.confirm_handoff(proposal)
    blocked = runtime.run_until_unknown(transition.id)
    assert blocked.phase is HandoffPhase.START_TARGET_SESSION
    start_calls_before_recovery = [
        call for call in runtime.adapter.calls if call[0] == "start_session"
    ]
    assert len(start_calls_before_recovery) == 2
    first_attempt_target_request_id = None
    first_attempt_target_execution_id = blocked.target_execution_id
    with runtime.uow_factory() as uow:
        first_attempt_target = uow.execution_requests.get_by_handoff_transition(
            transition.id,
            workspace_id,
        )
    assert first_attempt_target is not None
    first_attempt_target_request_id = first_attempt_target.id

    recovered = runtime.recover_target_start_unknown(transition.id)
    assert recovered.phase is HandoffPhase.CREATE_TARGET_EXECUTION
    assert recovered.logical_attempt == 2
    with runtime.uow_factory() as uow:
        retired = uow.execution_requests.get(
            first_attempt_target_request_id,
            workspace_id,
        )
        assert retired is not None
        assert retired.status is ExecutionRequestStatus.OBSOLETE
        assert retired.handoff_transition_id == transition.id
        assert retired.execution_lineage_id == first_attempt_target_execution_id
        assert retired.first_execution_id == first_attempt_target_execution_id
        assert (
            retired.gate_details["retired_first_execution_id"]
            == str(first_attempt_target_execution_id)
        )
        assert uow.execution_requests.get_by_handoff_transition(
            transition.id,
            workspace_id,
        ) is None
    runtime.run_until_converged(transition.id)

    start_calls_after_recovery = [
        call for call in runtime.adapter.calls if call[0] == "start_session"
    ]
    assert len(start_calls_after_recovery) == 3


@pytest.mark.parametrize(
    "fault",
    (
        FaultPoint.CHECKPOINT_BEFORE,
        FaultPoint.CHECKPOINT_AFTER,
        FaultPoint.STOP_BEFORE,
        FaultPoint.STOP_AFTER,
        FaultPoint.FREEZE_BEFORE,
        FaultPoint.FREEZE_AFTER,
        FaultPoint.TRANSFER_BEFORE,
        FaultPoint.TRANSFER_AFTER,
        FaultPoint.TARGET_START_AFTER,
        FaultPoint.WRITER_BEFORE,
        FaultPoint.WRITER_AFTER,
    ),
)
def test_g4_unknown_boundary_does_not_replay_external_operation(
    database,
    workspace_id: UUID,
    fault: FaultPoint,
) -> None:
    fixture = seed_g4_fixture(database, workspace_id, scenario=G4Scenario.NORMAL, now=NOW, reset=True)
    runtime = FakeHandoffRuntime(database, workspace_id, fixture, now=NOW, fault=fault)
    transition = runtime.confirm_handoff(runtime.prepare_handoff())
    if fault is FaultPoint.FREEZE_AFTER:
        runtime.run_until_converged(transition.id)
        assert sum(call[0] == "freeze" for call in runtime.workspace.calls) == 1
        return
    blocked = runtime.run_until_unknown(transition.id)
    assert blocked.status.value == "blocked_unknown"
    calls_after_boundary = tuple(runtime.adapter.calls) + tuple(runtime.workspace.calls)

    with pytest.raises(AssertionError):
        runtime.run_until_converged(transition.id, limit=2)

    assert tuple(runtime.adapter.calls) + tuple(runtime.workspace.calls) == calls_after_boundary
    with database.session() as session:
        assert session.scalar(
            select(ExecutionModel.id).where(
                ExecutionModel.worker_item_id == fixture.worker_item_ids["a"],
                ExecutionModel.status == "running",
            )
        ) == fixture.worker_a_execution_id
        assert session.scalar(
            select(ExecutionModel.id).where(
                ExecutionModel.worker_item_id == fixture.worker_item_ids["c"],
            )
        ) is None


def test_g4_fake_handoff_accepts_distinct_adapter_definitions_without_core_branching(
    database,
    workspace_id: UUID,
) -> None:
    fixture = seed_g4_fixture(database, workspace_id, scenario=G4Scenario.NORMAL, now=NOW, reset=True)
    runtime = FakeHandoffRuntime(
        database,
        workspace_id,
        fixture,
        now=NOW,
        distinct_adapter_definitions=True,
    )
    assert runtime.prepare_handoff().id
