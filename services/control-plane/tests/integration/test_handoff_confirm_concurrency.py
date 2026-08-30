from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.handoff_candidates import HandoffCandidateService
from sidera_control_plane.application.handoff_commands import HandoffCommandHandler
from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import CommandModel, HandoffTransitionModel
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


def _envelope(workspace_id: UUID) -> CommandEnvelope:
    proposal_id = uuid4()
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
        idempotency_key="handoff-confirm-concurrent",
        correlation_id=uuid4(),
    )


def test_concurrent_confirm_requests_share_one_rejection_record(
    database: Database,
    workspace_id: UUID,
) -> None:
    service = _service(database)
    envelope = _envelope(workspace_id)

    def invoke() -> str:
        try:
            service.handle(envelope)
        except DomainError as error:
            return str(getattr(error, "code", "unknown"))
        raise AssertionError("unknown proposal unexpectedly confirmed")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: invoke(), range(2)))

    assert set(results) == {"not_found"}
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(CommandModel)) == 1
        assert session.scalar(select(func.count()).select_from(HandoffTransitionModel)) == 0
