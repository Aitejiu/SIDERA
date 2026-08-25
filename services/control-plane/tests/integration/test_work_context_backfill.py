from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update
from test_proposal_confirmation import command, seed_proposal
from test_work_context_queries import _create_context

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    PlanningBriefRevisionModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunModel,
    ProposalMaterializationModel,
    TaskModel,
    WorkContextModel,
    WorkContextRevisionModel,
)
from sidera_control_plane.work_context_backfill import WorkContextBackfillService

pytestmark = pytest.mark.integration


def _clear_context_tables(database: Database) -> None:
    with database.session() as session:
        session.execute(
            text(
                "TRUNCATE TABLE work_context_revisions, work_contexts "
                "CASCADE"
            )
        )
        session.commit()


def _context_rows(
    database: Database, planning_request_id: UUID
) -> tuple[WorkContextModel, ...]:
    with database.session() as session:
        return tuple(
            session.scalars(
                select(WorkContextModel).where(
                    WorkContextModel.planning_request_id == planning_request_id
                )
            ).all()
        )


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_backfill_rebuilds_submitted_request_and_is_idempotent(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    data = _create_context(client, workspace_id, "backfill-submitted")
    request_id = UUID(str(data["planning_request_id"]))
    _clear_context_tables(database)

    service = WorkContextBackfillService()
    first = service.run(database, workspace_id)

    assert first.examined == 1
    assert first.created == 1
    assert first.incomplete == 0
    assert first.skipped == 0
    assert first.failed == 0
    assert first.last_planning_request_id == request_id

    with database.session() as session:
        context = session.scalar(
            select(WorkContextModel).where(
                WorkContextModel.planning_request_id == request_id
            )
        )
        assert context is not None
        assert context.status == "active"
        revision = session.scalar(
            select(WorkContextRevisionModel).where(
                WorkContextRevisionModel.work_context_id == context.id
            )
        )
        assert revision is not None
        assert revision.reason == "legacy_backfill"
        first_hash = revision.content_hash

    second = service.run(database, workspace_id)
    assert second.examined == 1
    assert second.created == 0
    assert second.skipped == 1
    assert second.last_planning_request_id == request_id
    assert len(_context_rows(database, request_id)) == 1

    with database.session() as session:
        revision = session.scalar(
            select(WorkContextRevisionModel).where(
                WorkContextRevisionModel.work_context_id == context.id
            )
        )
        assert revision is not None
        assert revision.content_hash == first_hash


def test_backfill_rebuilds_proposal_ready_request_with_proposal_reference(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    _clear_context_tables(database)

    summary = WorkContextBackfillService().run(database, workspace_id)

    assert summary.created == 1
    with database.session() as session:
        context = session.scalar(
            select(WorkContextModel).where(
                WorkContextModel.planning_request_id == request_id
            )
        )
        assert context is not None
        assert context.status == "active"
        revision = session.scalar(
            select(WorkContextRevisionModel).where(
                WorkContextRevisionModel.work_context_id == context.id
            )
        )
        assert revision is not None
        assert revision.proposal_revision_id == proposal_id
        assert revision.plan_snapshot_id is None
        assert revision.snapshot["accepted_decisions"]

    assert profile_id
    assert content_hash


def test_backfill_materialized_terminal_request_is_archived(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    response = client.post(
        "/api/v1/commands",
        json=command(workspace_id, request_id, profile_id, content_hash),
    )
    assert response.status_code == 201, response.text
    root_task_id = UUID(response.json()["data"]["root_task_id"])
    _clear_context_tables(database)

    with database.session() as session:
        session.execute(
            update(TaskModel)
            .where(TaskModel.id == root_task_id)
            .values(status="succeeded")
        )
        session.commit()

    summary = WorkContextBackfillService().run(database, workspace_id)

    assert summary.created == 1
    with database.session() as session:
        context = session.scalar(
            select(WorkContextModel).where(
                WorkContextModel.planning_request_id == request_id
            )
        )
        assert context is not None
        assert context.root_task_id == root_task_id
        assert context.status == "archived"
        assert session.scalar(
            select(func.count())
            .select_from(ProposalMaterializationModel)
            .where(ProposalMaterializationModel.planning_request_id == request_id)
        ) == 1


def test_backfill_marks_missing_proposal_without_inventing_content(
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, _profile_id, _content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    _clear_context_tables(database)
    with database.session() as session:
        session.execute(
            update(PlanningRequestModel)
            .where(PlanningRequestModel.id == request_id)
            .values(
                current_proposal_revision=None,
                current_proposal_content_hash=None,
            )
        )
        session.commit()

    summary = WorkContextBackfillService().run(database, workspace_id)

    assert summary.created == 1
    assert summary.incomplete == 1
    with database.session() as session:
        context = session.scalar(
            select(WorkContextModel).where(
                WorkContextModel.planning_request_id == request_id
            )
        )
        assert context is not None
        assert context.status == "legacy_incomplete"
        revision = session.scalar(
            select(WorkContextRevisionModel).where(
                WorkContextRevisionModel.work_context_id == context.id
            )
        )
        assert revision is not None
        assert revision.snapshot["missing_fact_codes"] == ["proposal_missing"]
        assert revision.proposal_revision_id is None


def test_backfill_skips_write_when_brief_pointer_is_missing(
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, _profile_id, _content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    _clear_context_tables(database)
    with database.session() as session:
        session.execute(
            update(PlanningRequestModel)
            .where(PlanningRequestModel.id == request_id)
            .values(
                current_brief_revision=None,
                current_proposal_revision=None,
                current_proposal_content_hash=None,
            )
        )
        session.execute(
            delete(PlanningProposalRevisionModel).where(
                PlanningProposalRevisionModel.planning_request_id == request_id
            )
        )
        session.execute(
            delete(PlanningRunModel).where(
                PlanningRunModel.planning_request_id == request_id
            )
        )
        session.execute(
            delete(PlanningBriefRevisionModel).where(
                PlanningBriefRevisionModel.planning_request_id == request_id
            )
        )
        session.commit()

    summary = WorkContextBackfillService().run(database, workspace_id)

    assert summary.created == 0
    assert summary.incomplete == 1
    assert _context_rows(database, request_id) == ()


def test_backfill_dry_run_and_uuid_resume_do_not_duplicate_rows(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    first_data = _create_context(client, workspace_id, "backfill-resume-a")
    second_data = _create_context(client, workspace_id, "backfill-resume-b")
    first_id = UUID(str(first_data["planning_request_id"]))
    second_id = UUID(str(second_data["planning_request_id"]))
    _clear_context_tables(database)

    service = WorkContextBackfillService()
    dry_run = service.run(database, workspace_id, batch_size=1, dry_run=True)
    assert dry_run.created == 2
    assert _context_rows(database, first_id) == ()
    assert _context_rows(database, second_id) == ()

    first_batch = service.run(
        database,
        workspace_id,
        batch_size=1,
        after_planning_request_id=min(first_id, second_id),
    )
    assert first_batch.created == 1
    assert first_batch.last_planning_request_id == max(first_id, second_id)
    resumed = service.run(
        database,
        workspace_id,
        batch_size=1,
    )
    assert resumed.created == 1
    assert resumed.last_planning_request_id is not None
    assert {row.planning_request_id for row in _context_rows(database, first_id)} == {
        first_id
    }
    assert {row.planning_request_id for row in _context_rows(database, second_id)} == {
        second_id
    }


def test_backfill_rolls_back_only_the_failed_batch(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    request_ids = [
        UUID(
            str(
                _create_context(
                    client, workspace_id, f"backfill-failure-{index}"
                )["planning_request_id"]
            )
        )
        for index in range(3)
    ]
    request_ids.sort()
    _clear_context_tables(database)
    with database.session() as session:
        session.execute(
            update(PlanningBriefRevisionModel)
            .where(
                PlanningBriefRevisionModel.planning_request_id == request_ids[1]
            )
            .values(payload={"objective": "corrupted legacy brief"})
        )
        session.commit()

    summary = WorkContextBackfillService().run(
        database, workspace_id, batch_size=2
    )

    assert summary.examined == 3
    assert summary.failed == 2
    assert summary.created == 1
    assert _context_rows(database, request_ids[0]) == ()
    assert _context_rows(database, request_ids[1]) == ()
    assert len(_context_rows(database, request_ids[2])) == 1


def test_backfill_cli_requires_explicit_write_confirmation() -> None:
    from sidera_control_plane.work_context_backfill import main

    with pytest.raises(SystemExit):
        main(
            [
                "--workspace-id",
                "00000000-0000-0000-0000-000000000001",
            ]
        )
