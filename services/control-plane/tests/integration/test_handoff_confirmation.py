from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.handoff_candidates import HandoffCandidateService
from sidera_control_plane.application.handoff_commands import (
    HandoffCommandError,
    HandoffCommandHandler,
)
from sidera_control_plane.application.handoff_gate import (
    HandoffGateOperation,
    HandoffOperationGate,
)
from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CommandModel,
    HandoffTransitionModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _service(database: Database) -> CommandService:
    factory = lambda: SqlAlchemyUnitOfWork(database)
    resolver = WorkerRuntimeResolver()
    candidates = HandoffCandidateService(factory, runtime_resolver=resolver)
    return CommandService(
        factory,
        (
            HandoffCommandHandler(
                lambda: datetime.now(UTC),
                candidate_service=candidates,
                runtime_resolver=resolver,
            ),
        ),
    )


def _confirm_envelope(workspace_id: UUID, *, proposal_id: UUID | None = None) -> CommandEnvelope:
    proposal_id = proposal_id or uuid4()
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type="ConfirmWorkerHandoff",
        target_type="handoff_proposal",
        actor_type="user",
        actor_id="user:integration",
        target_id=proposal_id,
        expected_version=1,
        payload={
            "proposal_id": str(proposal_id),
            "expected_proposal_version": 1,
            "expected_proposal_hash": "0" * 64,
            "expected_worker_item_version": 1,
        },
        idempotency_key=f"handoff-confirm-{uuid4()}",
        correlation_id=uuid4(),
    )


def test_confirm_rejects_unknown_proposal_without_creating_a_transition(
    database: Database,
    workspace_id: UUID,
) -> None:
    service = _service(database)

    with pytest.raises(HandoffCommandError) as error:
        service.handle(_confirm_envelope(workspace_id))

    assert getattr(error.value, "code", None) == "not_found"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(HandoffTransitionModel)) == 0
        assert session.scalar(select(func.count()).select_from(CommandModel)) == 1


def test_handoff_gate_reads_the_real_transition_repository(
    database: Database,
    workspace_id: UUID,
) -> None:
    worker_item_id = uuid4()
    with SqlAlchemyUnitOfWork(database) as uow:
        result = HandoffOperationGate(uow).evaluate(
            worker_item_id=worker_item_id,
            workspace_id=workspace_id,
            operation=HandoffGateOperation.USER_INPUT,
        )

    assert result.allowed is True
    assert result.transition_id is None
