from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from sidera_knowledge.application.command_service import KnowledgeCommandService
from sidera_knowledge.application.knowledge_commands import knowledge_command_handlers
from sidera_knowledge.domain.commands import CommandEnvelope
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import (
    CommandModel,
    KnowledgeCandidateModel,
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
