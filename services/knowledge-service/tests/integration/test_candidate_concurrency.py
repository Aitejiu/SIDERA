from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from sidera_knowledge.application.command_service import KnowledgeCommandService
from sidera_knowledge.application.knowledge_commands import knowledge_command_handlers
from sidera_knowledge.domain.commands import CommandEnvelope
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import (
    KnowledgeCandidateModel,
    KnowledgePageModel,
    KnowledgeRevisionModel,
)
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _payload(candidate_id, source_id):
    return {
        "candidate_schema_version": 1,
        "candidate_id": str(candidate_id),
        "operation": "create_page",
        "title": "Concurrent guide",
        "slug": f"concurrent-{candidate_id.hex}",
        "summary": "A guide",
        "tags": ["concurrency"],
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
                "label": "Source",
                "metadata": {},
            }
        ],
        "provenance": {"producer_type": "main_agent", "producer_id": "agent"},
    }


def _submit(database: Database, workspace_id, candidate_id):
    command = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="SubmitKnowledgeCandidate",
        actor_type="main_agent",
        actor_id="agent",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload=_payload(candidate_id, uuid4()),
        expected_version=None,
        idempotency_key=f"submit-{candidate_id}",
        correlation_id=uuid4(),
    )
    return KnowledgeCommandService(
        lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
    ).handle(command)


def _accept_command(workspace_id, candidate_id, candidate_hash, version):
    return CommandEnvelope(
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
            "reason": "Concurrent review",
            "observed_candidate_hash": candidate_hash,
            "expected_candidate_version": version,
        },
        expected_version=None,
        idempotency_key=f"accept-{uuid4()}",
        correlation_id=uuid4(),
    )


def test_two_sessions_accept_same_candidate_publish_one_revision(
    database: Database, database_url: str
) -> None:
    workspace_id = uuid4()
    candidate_id = uuid4()
    _submit(database, workspace_id, candidate_id)
    with database.session() as session:
        candidate = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        assert candidate is not None
        candidate_hash = candidate.candidate_hash
        version = candidate.version

    commands = [
        _accept_command(workspace_id, candidate_id, candidate_hash, version),
        _accept_command(workspace_id, candidate_id, candidate_hash, version),
    ]

    barrier = Barrier(2)

    def run(command: CommandEnvelope):
        barrier.wait()
        local_database = Database(database_url)
        try:
            return KnowledgeCommandService(
                lambda: SqlAlchemyUnitOfWork(local_database),
                knowledge_command_handlers(),
            ).handle(command)
        finally:
            local_database.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(run, commands))

    assert sum(outcome.status == "applied" for outcome in outcomes) == 1
    assert sum(outcome.status == "rejected" for outcome in outcomes) == 1
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
