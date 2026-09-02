from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import update

from sidera_control_plane.application.handoff_recovery import HandoffRecoveryService
from sidera_control_plane.domain.gateway import (
    GatewayOperationType,
    gateway_operation_id,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransitionStatus,
    HandoffWriterState,
)
from sidera_control_plane.persistence.models import ExecutionSessionModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_handoff_repository import _seed

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


def _repair_seed_session(database, workspace_id: UUID, session_id: UUID) -> None:
    with database.session() as session:
        session.execute(
            update(ExecutionSessionModel)
            .where(
                ExecutionSessionModel.id == session_id,
                ExecutionSessionModel.workspace_id == workspace_id,
            )
            .values(capability_snapshot=SESSION_CAPABILITIES)
        )
        session.commit()


def test_unknown_checkpoint_is_inspected_without_retry_or_state_mutation(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    _repair_seed_session(database, workspace_id, seed.source_session_id)
    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.handoff_transitions.get(
            seed.transition.id,
            workspace_id,
            for_update=True,
        )
        assert transition is not None
        operation_id = handoff_step_operation_id(
            transition.id,
            HandoffPhase.CHECKPOINT_SOURCE,
            logical_attempt=transition.logical_attempt,
            action="checkpoint",
        )
        transition.block(
            failure_code="checkpoint_result_unknown",
            now=NOW,
            status=HandoffTransitionStatus.BLOCKED_UNKNOWN,
            blocked_operation_id=operation_id,
        )
        uow.handoff_transitions.save(transition, previous_version=1)

    service = HandoffRecoveryService(lambda: SqlAlchemyUnitOfWork(database))
    with SqlAlchemyUnitOfWork(database) as uow:
        before = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        assert before is not None

    inspection = service.inspect_unknown(transition=before, now=NOW)

    assert inspection.blocked_operation_id == operation_id
    assert len(inspection.operation_inspections) == 1
    assert (
        inspection.operation_inspections[0].operation_type
        is GatewayOperationType.HANDOFF_CHECKPOINT
    )
    assert inspection.operation_inspections[0].outcome.value == "unknown"
    assert inspection.safe_conclusions == ()
    assert (
        service.available_strategies(
            transition=before,
            inspection=inspection,
        )
        == ()
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        after = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        assert after is not None
        assert after.status is HandoffTransitionStatus.BLOCKED_UNKNOWN
        assert after.version == before.version
        assert after.blocked_operation_id == operation_id
        assert (
            uow.handoff_recovery_decisions.list_for_transition(
                seed.transition.id,
                workspace_id,
            )
            == ()
        )


def test_unknown_source_stop_cannot_start_a_target(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    _repair_seed_session(database, workspace_id, seed.source_session_id)
    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.handoff_transitions.get(
            seed.transition.id,
            workspace_id,
            for_update=True,
        )
        assert transition is not None
        transition.phase = HandoffPhase.STOP_SOURCE
        transition.writer_state = HandoffWriterState.SOURCE_STOP_REQUESTED
        operation_id = gateway_operation_id(
            transition.source_execution_id,
            GatewayOperationType.STOP_SESSION,
        )
        transition.block(
            failure_code="stop_result_unknown",
            now=NOW,
            status=HandoffTransitionStatus.BLOCKED_UNKNOWN,
            blocked_operation_id=operation_id,
        )
        uow.handoff_transitions.save(transition, previous_version=1)

    service = HandoffRecoveryService(lambda: SqlAlchemyUnitOfWork(database))
    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        assert transition is not None

    inspection = service.inspect_unknown(transition=transition, now=NOW)

    assert (
        service.available_strategies(
            transition=transition,
            inspection=inspection,
        )
        == ()
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        current = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        assert current is not None
        assert current.target_execution_id is None
        assert current.status is HandoffTransitionStatus.BLOCKED_UNKNOWN
