from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.handoff_candidates import HandoffCandidateService
from sidera_control_plane.application.handoff_commands import (
    HandoffCommandError,
    HandoffCommandHandler,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _service(
    database: Database,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
) -> CommandService:
    factory: Callable[[], ControlPlaneUnitOfWork] = lambda: SqlAlchemyUnitOfWork(database)
    runtime_resolver = WorkerRuntimeResolver()
    candidates = HandoffCandidateService(factory, runtime_resolver=runtime_resolver)
    return CommandService(
        factory,
        (
            HandoffCommandHandler(
                clock,
                candidate_service=candidates,
                runtime_resolver=runtime_resolver,
            ),
        ),
    )


def test_prepare_requires_a_real_seeded_running_worker_item(
    database: Database,
    workspace_id: UUID,
) -> None:
    service = _service(database)
    envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type="PrepareWorkerHandoff",
        target_type="worker_item",
        actor_type="user",
        actor_id="user:integration",
        target_id=uuid4(),
        expected_version=1,
        payload={
            "target_worker_id": str(uuid4()),
            "handoff_reason": "integration seed is intentionally incomplete",
            "handoff_instruction": "do not execute",
            "input_plan": {},
            "proposal_ttl_seconds": 900,
        },
        idempotency_key="handoff-prepare-missing-source",
        correlation_id=uuid4(),
    )

    with pytest.raises(HandoffCommandError) as error:
        service.handle(envelope)

    assert error.value.code in {
        "not_found",
        "handoff_source_not_active",
        "handoff_candidate_blocked",
    }
