from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from sidera_knowledge.application.command_service import KnowledgeCommandService
from sidera_knowledge.application.knowledge_commands import (
    knowledge_command_handlers,
    record_source_unavailable,
)
from sidera_knowledge.domain.commands import CommandEnvelope
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import (
    CommandModel,
    KnowledgeCandidateModel,
    KnowledgeCandidateReviewModel,
    KnowledgePageModel,
    KnowledgeRevisionModel,
    KnowledgeSourceModel,
    OutboxMessageModel,
)
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _payload(candidate_id, source_id):
    return {
        "candidate_schema_version": 1,
        "candidate_id": str(candidate_id),
        "operation": "create_page",
        "title": "Integration Guide",
        "slug": f"integration-{candidate_id.hex}",
        "summary": "A guide",
        "tags": ["integration"],
        "sections": [
            {
                "section_key": "overview",
                "heading": "Overview",
                "summary": "Summary",
                "body_markdown": "body",
                "ordinal": 0,
            }
        ],
        "relations": [],
        "sources": [
            {
                "source_id": str(source_id),
                "source_type": "manual",
                "label": "Integration source",
                "metadata": {},
            }
        ],
        "provenance": {"producer_type": "main_agent", "producer_id": "agent-1"},
    }


def _revise_payload(candidate_id, source_id, page_id, revision_id, revision_hash):
    payload = _payload(candidate_id, source_id)
    payload.update(
        {
            "operation": "revise_page",
            "target_page_id": str(page_id),
            "expected_revision_id": str(revision_id),
            "expected_revision_hash": revision_hash,
            "title": "Revised Integration Guide",
            "slug": "integration-guide",
        }
    )
    return payload


def _submit_command(workspace_id, candidate_id, payload):
    return CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="SubmitKnowledgeCandidate",
        actor_type="main_agent",
        actor_id="agent-1",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload=payload,
        expected_version=None,
        idempotency_key=f"submit-{candidate_id}",
        correlation_id=uuid4(),
    )


def _review_command(
    workspace_id,
    candidate_id,
    candidate_hash,
    candidate_version,
    *,
    decision="accept",
    target_revision_id=None,
    target_revision_hash=None,
):
    payload = {
        "candidate_id": str(candidate_id),
        "decision": decision,
        "reason": "Reviewed by owner",
        "observed_candidate_hash": candidate_hash,
        "expected_candidate_version": candidate_version,
    }
    if target_revision_id is not None:
        payload["observed_target_revision_id"] = str(target_revision_id)
        payload["observed_target_revision_hash"] = target_revision_hash
    return CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="ReviewKnowledgeCandidate",
        actor_type="local_owner",
        actor_id="aitejiu",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload=payload,
        expected_version=None,
        idempotency_key=f"review-{candidate_id}-{uuid4()}",
        correlation_id=uuid4(),
    )


def test_submit_candidate_is_idempotent_and_has_one_event(database: Database) -> None:
    workspace_id = uuid4()
    candidate_id = uuid4()
    source_id = uuid4()
    payload = _payload(candidate_id, source_id)
    command = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="SubmitKnowledgeCandidate",
        actor_type="main_agent",
        actor_id="agent-1",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload=payload,
        expected_version=None,
        idempotency_key=f"submit-{candidate_id}",
        correlation_id=uuid4(),
    )
    service = KnowledgeCommandService(
        lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
    )

    first = service.handle(command)
    second = service.handle(command)

    assert first.status == "applied"
    assert second == first
    with database.session() as session:
        candidates = session.scalar(
            select(func.count()).select_from(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == candidate_id
            )
        )
        commands = session.scalar(
            select(func.count()).select_from(CommandModel).where(
                CommandModel.id == command.command_id
            )
        )
        outbox = session.scalar(
            select(func.count())
            .select_from(OutboxMessageModel)
            .where(
                OutboxMessageModel.workspace_id == workspace_id,
                OutboxMessageModel.topic == "knowledge.candidate_submitted",
            )
        )
    assert candidates == 1
    assert commands == 1
    assert outbox == 1


def test_accept_create_candidate_publishes_page_revision_and_review(
    database: Database,
) -> None:
    workspace_id = uuid4()
    candidate_id = uuid4()
    source_id = uuid4()
    submit = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="SubmitKnowledgeCandidate",
        actor_type="main_agent",
        actor_id="agent-1",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload=_payload(candidate_id, source_id),
        expected_version=None,
        idempotency_key=f"submit-{candidate_id}",
        correlation_id=uuid4(),
    )
    service = KnowledgeCommandService(
        lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
    )
    service.handle(submit)

    with database.session() as session:
        candidate = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        assert candidate is not None
        candidate_hash = candidate.candidate_hash
        candidate_version = candidate.version

    review = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="ReviewKnowledgeCandidate",
        actor_type="local_owner",
        actor_id="aitejiu",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload={
            "candidate_id": str(candidate_id),
            "decision": "accept",
            "reason": "Reviewed",
            "observed_candidate_hash": candidate_hash,
            "expected_candidate_version": candidate_version,
        },
        expected_version=None,
        idempotency_key=f"review-{candidate_id}",
        correlation_id=uuid4(),
    )
    result = service.handle(review)

    assert result.status == "applied"
    assert result.result["status"] == "accepted"
    with database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(KnowledgePageModel)
                .where(KnowledgePageModel.workspace_id == workspace_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(KnowledgeRevisionModel)
                .where(KnowledgeRevisionModel.workspace_id == workspace_id)
            )
            == 1
        )
        stored_review = session.scalar(
            select(KnowledgeCandidateReviewModel).where(
                KnowledgeCandidateReviewModel.candidate_id == candidate_id,
                KnowledgeCandidateReviewModel.workspace_id == workspace_id,
            )
        )
        assert stored_review is not None
        assert stored_review.created_revision_id is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessageModel)
                .where(
                    OutboxMessageModel.workspace_id == workspace_id,
                    OutboxMessageModel.topic == "knowledge.candidate_reviewed",
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessageModel)
                .where(
                    OutboxMessageModel.workspace_id == workspace_id,
                    OutboxMessageModel.topic == "knowledge.revision_published",
                )
            )
            == 1
        )


def test_request_changes_keeps_candidate_immutable_and_does_not_publish(
    database: Database,
) -> None:
    workspace_id = uuid4()
    candidate_id = uuid4()
    submit = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="SubmitKnowledgeCandidate",
        actor_type="main_agent",
        actor_id="agent-1",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload=_payload(candidate_id, uuid4()),
        expected_version=None,
        idempotency_key=f"submit-{candidate_id}",
        correlation_id=uuid4(),
    )
    service = KnowledgeCommandService(
        lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
    )
    service.handle(submit)
    with database.session() as session:
        candidate = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        assert candidate is not None
        original_hash = candidate.candidate_hash
        original_snapshot = candidate.proposed_snapshot
        version = candidate.version

    review = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="ReviewKnowledgeCandidate",
        actor_type="local_owner",
        actor_id="aitejiu",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload={
            "candidate_id": str(candidate_id),
            "decision": "request_changes",
            "reason": "Need source detail",
            "observed_candidate_hash": original_hash,
            "expected_candidate_version": version,
        },
        expected_version=None,
        idempotency_key=f"changes-{candidate_id}",
        correlation_id=uuid4(),
    )
    result = service.handle(review)

    assert result.status == "applied"
    with database.session() as session:
        candidate = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        assert candidate is not None
        assert candidate.status == "changes_requested"
        assert candidate.candidate_hash == original_hash
        assert candidate.proposed_snapshot == original_snapshot
        assert (
            session.scalar(
                select(func.count())
                .select_from(KnowledgeRevisionModel)
                .where(KnowledgeRevisionModel.workspace_id == workspace_id)
            )
            == 0
        )


def test_revise_candidate_becomes_stale_when_target_revision_changes(
    database: Database,
) -> None:
    workspace_id = uuid4()
    create_id = uuid4()
    service = KnowledgeCommandService(
        lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
    )
    service.handle(_submit_command(workspace_id, create_id, _payload(create_id, uuid4())))
    with database.session() as session:
        create_row = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == create_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        assert create_row is not None
        create_review = _review_command(
            workspace_id,
            create_id,
            create_row.candidate_hash,
            create_row.version,
        )
    assert service.handle(create_review).result["status"] == "accepted"

    with database.session() as session:
        page = session.scalar(
            select(KnowledgePageModel).where(
                KnowledgePageModel.workspace_id == workspace_id
            )
        )
        assert page is not None
        revision = session.scalar(
            select(KnowledgeRevisionModel).where(
                KnowledgeRevisionModel.id == page.current_revision_id,
                KnowledgeRevisionModel.workspace_id == workspace_id,
            )
        )
        assert revision is not None
        page_id = page.id
        revision_id = revision.id
        revision_hash = revision.content_hash

    stale_id = uuid4()
    winner_id = uuid4()
    service.handle(
        _submit_command(
            workspace_id,
            stale_id,
            _revise_payload(
                stale_id, uuid4(), page_id, revision_id, revision_hash
            ),
        )
    )
    service.handle(
        _submit_command(
            workspace_id,
            winner_id,
            _revise_payload(
                winner_id, uuid4(), page_id, revision_id, revision_hash
            ),
        )
    )
    with database.session() as session:
        stale_row = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == stale_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        winner_row = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == winner_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        assert stale_row is not None
        assert winner_row is not None

    assert service.handle(
        _review_command(
            workspace_id,
            winner_id,
            winner_row.candidate_hash,
            winner_row.version,
            target_revision_id=revision_id,
            target_revision_hash=revision_hash,
        )
    ).result["status"] == "accepted"
    stale_result = service.handle(
        _review_command(
            workspace_id,
            stale_id,
            stale_row.candidate_hash,
            stale_row.version,
            target_revision_id=revision_id,
            target_revision_hash=revision_hash,
        )
    )

    assert stale_result.status == "applied"
    assert stale_result.result == {"candidate_id": str(stale_id), "status": "stale"}
    with database.session() as session:
        stored_candidate = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == stale_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        stored_review = session.scalar(
            select(KnowledgeCandidateReviewModel).where(
                KnowledgeCandidateReviewModel.candidate_id == stale_id,
                KnowledgeCandidateReviewModel.workspace_id == workspace_id,
            )
        )
        revision_count = session.scalar(
            select(func.count())
            .select_from(KnowledgeRevisionModel)
            .where(KnowledgeRevisionModel.workspace_id == workspace_id)
        )
        stale_events = session.scalar(
            select(func.count())
            .select_from(OutboxMessageModel)
            .where(
                OutboxMessageModel.workspace_id == workspace_id,
                OutboxMessageModel.topic == "knowledge.candidate_reviewed",
                OutboxMessageModel.payload["status"].as_string() == "stale",
            )
        )
    assert stored_candidate is not None
    assert stored_candidate.status == "stale"
    assert stored_review is not None
    assert stored_review.decision == "reject"
    assert stored_review.created_revision_id is None
    assert revision_count == 2
    assert stale_events == 1


def test_withdraw_requires_owner_and_archive_preserves_revision_history(
    database: Database,
) -> None:
    workspace_id = uuid4()
    candidate_id = uuid4()
    service = KnowledgeCommandService(
        lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
    )
    service.handle(_submit_command(workspace_id, candidate_id, _payload(candidate_id, uuid4())))
    with database.session() as session:
        candidate = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        assert candidate is not None
        candidate_hash = candidate.candidate_hash
        candidate_version = candidate.version

    non_owner = _review_command(
        workspace_id,
        candidate_id,
        candidate_hash,
        candidate_version,
    )
    non_owner = CommandEnvelope(
        command_id=non_owner.command_id,
        workspace_id=non_owner.workspace_id,
        command_type="WithdrawKnowledgeCandidate",
        actor_type="main_agent",
        actor_id="agent-1",
        target_type=non_owner.target_type,
        target_id=non_owner.target_id,
        payload={
            "candidate_id": str(candidate_id),
            "expected_candidate_version": candidate_version,
        },
        expected_version=None,
        idempotency_key=f"withdraw-denied-{candidate_id}",
        correlation_id=uuid4(),
    )
    denied = service.handle(non_owner)
    assert denied.status == "rejected"
    assert denied.error_code == "permission_denied"

    withdrawn = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="WithdrawKnowledgeCandidate",
        actor_type="local_owner",
        actor_id="aitejiu",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload={
            "candidate_id": str(candidate_id),
            "expected_candidate_version": candidate_version,
        },
        expected_version=None,
        idempotency_key=f"withdraw-{candidate_id}",
        correlation_id=uuid4(),
    )
    assert service.handle(withdrawn).result["status"] == "withdrawn"

    accepted_id = uuid4()
    service.handle(_submit_command(workspace_id, accepted_id, _payload(accepted_id, uuid4())))
    with database.session() as session:
        accepted = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == accepted_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        assert accepted is not None
    assert service.handle(
        _review_command(
            workspace_id,
            accepted_id,
            accepted.candidate_hash,
            accepted.version,
        )
    ).result["status"] == "accepted"
    with database.session() as session:
        page = session.scalar(
            select(KnowledgePageModel).where(
                KnowledgePageModel.workspace_id == workspace_id,
                KnowledgePageModel.slug == f"integration-{accepted_id.hex}",
            )
        )
        revision_count_before = session.scalar(
            select(func.count())
            .select_from(KnowledgeRevisionModel)
            .where(KnowledgeRevisionModel.workspace_id == workspace_id)
        )
        assert page is not None
        page_id = page.id
        page_version = page.version

    archived = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="ArchiveKnowledgePage",
        actor_type="local_owner",
        actor_id="aitejiu",
        target_type="knowledge_page",
        target_id=page_id,
        payload={"page_id": str(page_id), "expected_page_version": page_version},
        expected_version=None,
        idempotency_key=f"archive-{page_id}",
        correlation_id=uuid4(),
    )
    assert service.handle(archived).result["status"] == "archived"
    with database.session() as session:
        page_after = session.scalar(
            select(KnowledgePageModel).where(KnowledgePageModel.id == page_id)
        )
        revision_count_after = session.scalar(
            select(func.count())
            .select_from(KnowledgeRevisionModel)
            .where(KnowledgeRevisionModel.workspace_id == workspace_id)
        )
    assert page_after is not None
    assert page_after.status == "archived"
    assert revision_count_after == revision_count_before


def test_source_unavailable_is_idempotent_and_emits_one_event(
    database: Database,
) -> None:
    workspace_id = uuid4()
    candidate_id = uuid4()
    source_id = uuid4()
    service = KnowledgeCommandService(
        lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
    )
    service.handle(_submit_command(workspace_id, candidate_id, _payload(candidate_id, source_id)))
    checked_at = datetime.now(UTC)
    with SqlAlchemyUnitOfWork(database) as uow:
        first = record_source_unavailable(
            uow,
            workspace_id=workspace_id,
            source_id=source_id,
            expected_version=1,
            reason_code="file_missing",
            checked_at=checked_at,
            correlation_id=uuid4(),
        )
        uow.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        second = record_source_unavailable(
            uow,
            workspace_id=workspace_id,
            source_id=source_id,
            expected_version=1,
            reason_code="file_missing",
            checked_at=checked_at,
            correlation_id=uuid4(),
        )
        uow.commit()

    assert first == {"source_id": str(source_id), "status": "unavailable"}
    assert second == {
        "source_id": str(source_id),
        "status": "unavailable",
        "idempotent": True,
    }
    with database.session() as session:
        source = session.scalar(
            select(KnowledgeSourceModel).where(
                KnowledgeSourceModel.id == source_id,
                KnowledgeSourceModel.workspace_id == workspace_id,
            )
        )
        event_count = session.scalar(
            select(func.count())
            .select_from(OutboxMessageModel)
            .where(
                OutboxMessageModel.workspace_id == workspace_id,
                OutboxMessageModel.topic == "knowledge.source_unavailable",
            )
        )
    assert source is not None
    assert source.availability == "unavailable"
    assert source.version == 2
    assert event_count == 1
