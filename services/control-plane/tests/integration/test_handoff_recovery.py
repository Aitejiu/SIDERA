from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from tests.integration.test_handoff_repository import _seed

from sidera_control_plane.application.handoff_recovery import HandoffRecoveryService
from sidera_control_plane.domain.execution import ExecutionSession
from sidera_control_plane.domain.gateway import (
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
    gateway_operation_id,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoff_recovery import (
    HandoffRecoveryDecision,
    HandoffRecoveryStrategy,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransitionStatus,
    HandoffWriterState,
)
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.persistence.handoff_repository import (
    HandoffTransitionRepository,
)
from sidera_control_plane.persistence.models import (
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
SESSION_CAPABILITIES = {
    "input": "queued",
    "idempotency": "operation-id",
    "resume": "durable",
    "stop": "exact-execution",
    "sandbox": "filesystem",
    "stability": "stable",
    "context_package_versions": [1],
    "context_patch_versions": [],
    "handoff_checkpoint_mode": "native",
    "handoff_checkpoint_versions": [1],
    "handoff_session_context_versions": [1],
    "handoff_writer_gate_versions": [1],
    "handoff_inspection": True,
}


def test_failed_derived_operation_is_recovered_with_a_new_logical_attempt(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    with database.session() as session:
        session.execute(
            update(ExecutionSessionModel)
            .where(
                ExecutionSessionModel.id == seed.source_session_id,
                ExecutionSessionModel.workspace_id == workspace_id,
            )
            .values(capability_snapshot=SESSION_CAPABILITIES)
        )
        session.execute(
            update(WorkerModel)
            .where(
                WorkerModel.id == seed.target_worker_id,
                WorkerModel.workspace_id == workspace_id,
            )
            .values(status="idle")
        )
        session.execute(
            update(ExecutionWorkspaceModel)
            .where(
                ExecutionWorkspaceModel.id == seed.target_manifest_id,
                ExecutionWorkspaceModel.workspace_id == workspace_id,
            )
            .values(
                checkouts=[
                    {
                        "repository_ref": "repo://sidera",
                        "base_revision": "a" * 40,
                        "branch": "main",
                        "current_head": "a" * 40,
                    }
                ]
            )
        )
        session.commit()
    operation_id = handoff_step_operation_id(
        seed.transition.id,
        seed.transition.phase,
        logical_attempt=1,
        action="checkpoint",
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.handoff_transitions.get(
            seed.transition.id,
            workspace_id,
            for_update=True,
        )
        source_execution = uow.executions.get(seed.source_execution_id, workspace_id)
        assert transition is not None
        assert source_execution is not None

        lease = AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=workspace_id,
            execution_id=source_execution.id,
            worker_id=seed.source_worker_id,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            fencing_token=1,
            assignment_reason={"source": "recovery-test"},
        )
        uow.leases.add(lease)
        operation = GatewayOperation.create(
            id=uuid4(),
            operation_id=operation_id,
            workspace_id=workspace_id,
            task_id=source_execution.task_id,
            execution_id=source_execution.id,
            session_id=seed.source_session_id,
            operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
            module_instance_id=None,
            config_version=None,
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        operation.start(now=NOW)
        operation.fail(failure_code="adapter_rejected", now=NOW)
        uow.gateway_operations.add(operation)
        transition.block(
            failure_code="adapter_rejected",
            now=NOW,
            status=HandoffTransitionStatus.BLOCKED,
            blocked_operation_id=operation_id,
        )
        uow.handoff_transitions.save(transition, previous_version=1)

    service = HandoffRecoveryService(lambda: SqlAlchemyUnitOfWork(database))
    with SqlAlchemyUnitOfWork(database) as uow:
        before = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        assert before is not None
    inspection = service.inspect_unknown(transition=before, now=NOW)
    assert inspection.safe_conclusions
    assert service.available_strategies(
        transition=before,
        inspection=inspection,
    ) == (HandoffRecoveryStrategy.RETRY_CURRENT_PHASE,)

    decision = HandoffRecoveryDecision(
        id=uuid4(),
        handoff_transition_id=before.id,
        expected_transition_version=before.version,
        strategy=HandoffRecoveryStrategy.RETRY_CURRENT_PHASE,
        actor_type="user",
        actor_id="integration",
        source_channel="web",
        reason="retry after the adapter reported a bounded failure",
        evidence_refs=inspection.evidence_refs,
        replacement_target_snapshot=None,
        resulting_operation_refs=(),
        created_at=NOW,
    )
    result = service.apply_decision(
        transition=before,
        decision=decision,
        now=NOW,
    )

    expected_new_operation_id = handoff_step_operation_id(
        before.id,
        before.phase,
        logical_attempt=2,
        action="checkpoint",
    )
    assert result.logical_attempt == 2
    assert result.resulting_operation_refs == (expected_new_operation_id,)
    with SqlAlchemyUnitOfWork(database) as uow:
        current = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        old_operation = uow.gateway_operations.get_by_operation_id(
            operation_id,
            workspace_id,
        )
        new_operation = uow.gateway_operations.get_by_operation_id(
            expected_new_operation_id,
            workspace_id,
        )
        decisions = uow.handoff_recovery_decisions.list_for_transition(
            seed.transition.id,
            workspace_id,
        )
        assert current is not None
        assert current.logical_attempt == 2
        assert current.status is HandoffTransitionStatus.BLOCKED
        assert old_operation is not None
        assert old_operation.status is GatewayOperationStatus.FAILED
        assert new_operation is None
        assert len(decisions) == 1
        assert decisions[0].resulting_operation_refs == (expected_new_operation_id,)


def test_recovery_save_persists_replacement_target_facts(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    replacement_snapshot = {
        "worker_id": str(seed.source_worker_id),
        "version": 2,
    }

    with database.session() as session:
        repository = HandoffTransitionRepository(session)
        transition = repository.get(seed.transition.id, workspace_id, for_update=True)
        assert transition is not None
        replaced = replace(
            transition,
            target_worker_id=seed.source_worker_id,
            target_runtime_snapshot=replacement_snapshot,
            version=transition.version + 1,
        )
        repository.save_recovery(replaced, previous_version=transition.version)
        session.commit()

    with database.session() as session:
        loaded = HandoffTransitionRepository(session).get(
            seed.transition.id,
            workspace_id,
        )
        assert loaded is not None
        assert loaded.target_worker_id == seed.source_worker_id
        assert dict(loaded.target_runtime_snapshot) == replacement_snapshot


def test_failed_target_start_with_a_lease_is_still_target_absent(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    with database.session() as session:
        session.execute(
            update(ExecutionSessionModel)
            .where(
                ExecutionSessionModel.id == seed.source_session_id,
                ExecutionSessionModel.workspace_id == workspace_id,
            )
            .values(capability_snapshot=SESSION_CAPABILITIES)
        )
        session.execute(
            update(WorkerModel)
            .where(
                WorkerModel.id == seed.target_worker_id,
                WorkerModel.workspace_id == workspace_id,
            )
            .values(status="idle")
        )
        session.execute(
            update(ExecutionWorkspaceModel)
            .where(
                ExecutionWorkspaceModel.id == seed.target_manifest_id,
                ExecutionWorkspaceModel.workspace_id == workspace_id,
            )
            .values(
                checkouts=[
                    {
                        "repository_ref": "repo://sidera",
                        "base_revision": "a" * 40,
                        "branch": "main",
                        "current_head": "a" * 40,
                    }
                ]
            )
        )
        session.commit()

    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.handoff_transitions.get(
            seed.transition.id,
            workspace_id,
            for_update=True,
        )
        target_execution = uow.executions.get(
            seed.target_execution_id,
            workspace_id,
            for_update=True,
        )
        target_worker = uow.workers.get(
            seed.target_worker_id,
            workspace_id,
            for_update=True,
        )
        assert transition is not None
        assert target_execution is not None
        assert target_worker is not None

        lease = AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=workspace_id,
            execution_id=target_execution.id,
            worker_id=target_worker.id,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            fencing_token=2,
            assignment_reason={"source": "target-start-recovery-test"},
        )
        uow.leases.add(lease)
        target_worker.reserve()
        uow.workers.save(target_worker, previous_version=1)
        target_execution.assign(
            lease_id=lease.id,
            worker_id=target_worker.id,
            fencing_token=lease.fencing_token,
        )
        uow.executions.save(target_execution, previous_version=1)

        operation_id = gateway_operation_id(
            target_execution.id,
            GatewayOperationType.START_SESSION,
        )
        operation = GatewayOperation.create(
            id=uuid4(),
            operation_id=operation_id,
            workspace_id=workspace_id,
            task_id=target_execution.task_id,
            execution_id=target_execution.id,
            session_id=None,
            operation_type=GatewayOperationType.START_SESSION,
            module_instance_id=None,
            config_version=None,
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        operation.start(now=NOW)
        operation.fail(failure_code="adapter_rejected", now=NOW)
        uow.gateway_operations.add(operation)

        transition.phase = HandoffPhase.START_TARGET_SESSION
        transition.writer_state = HandoffWriterState.TARGET_STARTING
        transition.target_execution_id = target_execution.id
        transition.target_workspace_manifest_id = seed.target_manifest_id
        transition.block(
            failure_code="adapter_rejected",
            now=NOW,
            status=HandoffTransitionStatus.BLOCKED,
            blocked_operation_id=operation_id,
        )
        uow.handoff_transitions.save(transition, previous_version=1)

    service = HandoffRecoveryService(lambda: SqlAlchemyUnitOfWork(database))
    with SqlAlchemyUnitOfWork(database) as uow:
        current = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        assert current is not None

    inspection = service.inspect_unknown(transition=current, now=NOW)

    assert "target_absent" in {item.value for item in inspection.safe_conclusions}
    assert set(
        service.available_strategies(
            transition=current,
            inspection=inspection,
        )
    ) >= {
        HandoffRecoveryStrategy.RETRY_SAME_TARGET,
        HandoffRecoveryStrategy.SELECT_NEW_TARGET,
        HandoffRecoveryStrategy.CONTINUE_WITH_SOURCE_AGENT,
    }


def test_successful_target_start_requires_matching_active_session_and_ready_manifest(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    with database.session() as session:
        session.execute(
            update(ExecutionSessionModel)
            .where(
                ExecutionSessionModel.id == seed.source_session_id,
                ExecutionSessionModel.workspace_id == workspace_id,
            )
            .values(capability_snapshot=SESSION_CAPABILITIES)
        )
        session.execute(
            update(WorkerModel)
            .where(
                WorkerModel.id == seed.target_worker_id,
                WorkerModel.workspace_id == workspace_id,
            )
            .values(status="idle")
        )
        session.execute(
            update(ExecutionWorkspaceModel)
            .where(
                ExecutionWorkspaceModel.id == seed.target_manifest_id,
                ExecutionWorkspaceModel.workspace_id == workspace_id,
            )
            .values(
                checkouts=[
                    {
                        "repository_ref": "repo://sidera",
                        "base_revision": "a" * 40,
                        "branch": "main",
                        "current_head": "a" * 40,
                    }
                ]
            )
        )
        session.commit()

    target_session_id = uuid4()
    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.handoff_transitions.get(
            seed.transition.id,
            workspace_id,
            for_update=True,
        )
        target_execution = uow.executions.get(
            seed.target_execution_id,
            workspace_id,
            for_update=True,
        )
        target_worker = uow.workers.get(
            seed.target_worker_id,
            workspace_id,
            for_update=True,
        )
        assert transition is not None
        assert target_execution is not None
        assert target_worker is not None

        lease = AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=workspace_id,
            execution_id=target_execution.id,
            worker_id=target_worker.id,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            fencing_token=2,
            assignment_reason={"source": "target-active-recovery-test"},
        )
        uow.leases.add(lease)
        target_worker.reserve()
        target_worker.mark_running()
        uow.workers.save(target_worker, previous_version=1)
        target_execution.assign(
            lease_id=lease.id,
            worker_id=target_worker.id,
            fencing_token=lease.fencing_token,
        )
        target_execution.start(
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        uow.executions.save(target_execution, previous_version=1)
        uow.sessions.add(
            ExecutionSession.start(
                session_id=target_session_id,
                workspace_id=workspace_id,
                execution_id=target_execution.id,
                worker_id=target_worker.id,
                adapter_session_id=f"target-session-{target_session_id.hex[:8]}",
                now=NOW,
            )
        )

        operation_id = gateway_operation_id(
            target_execution.id,
            GatewayOperationType.START_SESSION,
        )
        operation = GatewayOperation.create(
            id=uuid4(),
            operation_id=operation_id,
            workspace_id=workspace_id,
            task_id=target_execution.task_id,
            execution_id=target_execution.id,
            session_id=target_session_id,
            operation_type=GatewayOperationType.START_SESSION,
            module_instance_id=None,
            config_version=None,
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        operation.start(now=NOW)
        operation.succeed(result_ref="started:target-session", now=NOW)
        uow.gateway_operations.add(operation)

        transition.phase = HandoffPhase.START_TARGET_SESSION
        transition.writer_state = HandoffWriterState.TARGET_STARTING
        transition.target_execution_id = target_execution.id
        transition.target_session_id = target_session_id
        transition.target_workspace_manifest_id = seed.target_manifest_id
        transition.block(
            failure_code="start_result_unknown",
            now=NOW,
            status=HandoffTransitionStatus.BLOCKED,
            blocked_operation_id=operation_id,
        )
        uow.handoff_transitions.save(transition, previous_version=1)

    service = HandoffRecoveryService(lambda: SqlAlchemyUnitOfWork(database))
    with SqlAlchemyUnitOfWork(database) as uow:
        current = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        assert current is not None

    inspection = service.inspect_unknown(transition=current, now=NOW)

    assert "target_active" in {item.value for item in inspection.safe_conclusions}
    assert service.available_strategies(
        transition=current,
        inspection=inspection,
    ) == (HandoffRecoveryStrategy.CONFIRM_TARGET_ACTIVE,)

    decision = HandoffRecoveryDecision(
        id=uuid4(),
        handoff_transition_id=current.id,
        expected_transition_version=current.version,
        strategy=HandoffRecoveryStrategy.CONFIRM_TARGET_ACTIVE,
        actor_type="user",
        actor_id="integration",
        source_channel="web",
        reason="confirm the target that was started exactly once",
        evidence_refs=inspection.evidence_refs,
        replacement_target_snapshot=None,
        resulting_operation_refs=(),
        created_at=NOW,
    )
    result = service.apply_decision(
        transition=current,
        decision=decision,
        now=NOW,
    )

    assert result.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT
    assert result.status is HandoffTransitionStatus.BLOCKED
