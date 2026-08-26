"""Seed deterministic Knowledge Service data for the R5.3 browser workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, update

from sidera_knowledge.domain.candidates import KnowledgeCandidate
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
    KnowledgeSource,
)
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import (
    EventModel,
    KnowledgeCandidateModel,
    KnowledgeCandidateReviewModel,
    KnowledgeCandidateSourceModel,
    KnowledgePageModel,
    KnowledgeRelationSnapshotModel,
    KnowledgeRevisionModel,
    KnowledgeRevisionSourceModel,
    KnowledgeSectionSnapshotModel,
    KnowledgeSourceModel,
    OutboxMessageModel,
)
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
DEFAULT_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
SUBTASK_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
WORKER_ITEM_ID = UUID("a1000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000001")
SOURCE_ID = UUID("c0000000-0000-0000-0000-000000000001")
PAGE_ID = UUID("c0000000-0000-0000-0000-000000000002")
REVISION_ONE_ID = UUID("c0000000-0000-0000-0000-000000000003")
REVISION_TWO_ID = UUID("c0000000-0000-0000-0000-000000000004")
PENDING_CANDIDATE_ID = UUID("c0000000-0000-0000-0000-000000000005")
STALE_CANDIDATE_ID = UUID("c0000000-0000-0000-0000-000000000006")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", default=str(DEFAULT_WORKSPACE_ID))
    args = parser.parse_args(argv)

    if os.environ.get("SIDERA_ALLOW_E2E_SEED") != "1":
        raise SystemExit("SIDERA_ALLOW_E2E_SEED=1 is required for the E2E seed")
    database_url = os.environ.get("SIDERA_KNOWLEDGE_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_KNOWLEDGE_DATABASE_URL is required for the E2E seed")
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError as exc:
        raise SystemExit("--workspace-id must be a UUID") from exc

    _upgrade_schema()
    database = Database(database_url)
    try:
        _seed(database, workspace_id)
    finally:
        database.dispose()

    revision_one = _revision_one()
    revision_two = _revision_two()
    print(
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "source_id": str(SOURCE_ID),
                "page_id": str(PAGE_ID),
                "revision_one_id": str(REVISION_ONE_ID),
                "revision_one_hash": revision_one.content_hash,
                "revision_two_id": str(REVISION_TWO_ID),
                "revision_two_hash": revision_two.content_hash,
                "revision_two_section_hash": revision_two.sections[0].section_hash,
                "pending_candidate_id": str(PENDING_CANDIDATE_ID),
                "stale_candidate_id": str(STALE_CANDIDATE_ID),
            },
            sort_keys=True,
        )
    )
    return 0


def _upgrade_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "head")


def _seed(database: Database, workspace_id: UUID) -> None:
    with database.session() as session:
        session.execute(
            update(KnowledgePageModel)
            .where(KnowledgePageModel.workspace_id == workspace_id)
            .values(current_revision_id=None)
        )
        for model in (
            KnowledgeCandidateReviewModel,
            KnowledgeCandidateSourceModel,
            KnowledgeCandidateModel,
            KnowledgeRevisionSourceModel,
            KnowledgeRelationSnapshotModel,
            KnowledgeSectionSnapshotModel,
            KnowledgeRevisionModel,
            KnowledgePageModel,
            KnowledgeSourceModel,
            OutboxMessageModel,
            EventModel,
        ):
            session.execute(delete(model).where(model.workspace_id == workspace_id))
        session.commit()

    source = KnowledgeSource(
        source_id=SOURCE_ID,
        workspace_id=workspace_id,
        source_type="markdown_file",
        label="context-package.md",
        workspace_relative_path="docs/context-package.md",
        digest=_sha256_text("context-package.md"),
        metadata={"origin": "r5.3-e2e"},
    )
    page = KnowledgePage(
        page_id=PAGE_ID,
        workspace_id=workspace_id,
        canonical_title="Worker context reference",
        slug="worker-context-reference",
    )
    revision_one = _revision_one(workspace_id=workspace_id)
    revision_two = _revision_two(workspace_id=workspace_id)
    pending = _pending_candidate(workspace_id, revision_two)
    stale = _stale_candidate(workspace_id, revision_one)

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.sources.add_source(source)
        uow.pages.add_page(page)
        uow.flush()
        uow.pages.add_revision_and_advance_page(revision_one, expected_page_version=1)
        uow.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(revision_two, expected_page_version=2)
        uow.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.candidates.add_candidate(pending)
        uow.candidates.add_candidate(stale)
        uow.commit()


def _revision_one(workspace_id: UUID = DEFAULT_WORKSPACE_ID) -> KnowledgeRevision:
    return KnowledgeRevision(
        revision_id=REVISION_ONE_ID,
        page_id=PAGE_ID,
        workspace_id=workspace_id,
        revision_number=1,
        title="Worker context reference",
        summary="Initial context reference.",
        tags=("context", "r5"),
        sections=(
            KnowledgeSectionSnapshot(
                section_key="execution",
                heading="Execution handoff",
                summary="",
                body_markdown="The worker receives an exact immutable context package.\n",
                ordinal=0,
            ),
        ),
        relations=(),
        source_refs=(SOURCE_ID,),
        change_reason="initial_import",
        created_by="local:e2e",
        created_at=NOW,
    )


def _revision_two(workspace_id: UUID = DEFAULT_WORKSPACE_ID) -> KnowledgeRevision:
    return KnowledgeRevision(
        revision_id=REVISION_TWO_ID,
        page_id=PAGE_ID,
        workspace_id=workspace_id,
        revision_number=2,
        title="Worker context reference",
        summary="Exact knowledge used by the execution context package.",
        tags=("context", "r5"),
        sections=(
            KnowledgeSectionSnapshot(
                section_key="execution",
                heading="Execution handoff",
                summary="",
                body_markdown=(
                    "The worker receives an exact immutable context package and keeps "
                    "the selected revision hash.\n"
                ),
                ordinal=0,
            ),
        ),
        relations=(),
        source_refs=(SOURCE_ID,),
        change_reason="context_package_revision",
        created_by="local:e2e",
        created_at=NOW,
    )


def _pending_candidate(
    workspace_id: UUID, revision: KnowledgeRevision
) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        candidate_id=PENDING_CANDIDATE_ID,
        workspace_id=workspace_id,
        operation="revise_page",
        target_page_id=PAGE_ID,
        expected_revision_id=revision.revision_id,
        expected_revision_hash=revision.content_hash,
        proposed_title="Worker context reference",
        proposed_slug="worker-context-reference",
        proposed_summary="Updated context reference after worker review.",
        proposed_tags=("context", "handoff", "r5"),
        proposed_sections=(
            KnowledgeSectionSnapshot(
                section_key="execution",
                heading="Execution handoff",
                summary="",
                body_markdown=(
                    "The worker receives an exact immutable context package and records "
                    "a safe handoff.\n"
                ),
                ordinal=0,
            ),
        ),
        source_refs=(SOURCE_ID,),
        producer_type="worker",
        producer_id="OpenCode E2E Worker",
        task_id=ROOT_TASK_ID,
        subtask_id=SUBTASK_TASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        execution_id=EXECUTION_ID,
    )


def _stale_candidate(
    workspace_id: UUID, revision: KnowledgeRevision
) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        candidate_id=STALE_CANDIDATE_ID,
        workspace_id=workspace_id,
        operation="revise_page",
        target_page_id=PAGE_ID,
        expected_revision_id=revision.revision_id,
        expected_revision_hash=revision.content_hash,
        proposed_title="Stale worker notes",
        proposed_slug="stale-worker-notes",
        proposed_summary="This proposal intentionally points at an old revision.",
        proposed_tags=("stale",),
        proposed_sections=(
            KnowledgeSectionSnapshot(
                section_key="execution",
                heading="Old execution handoff",
                summary="",
                body_markdown="This candidate must not be auto-merged.\n",
                ordinal=0,
            ),
        ),
        source_refs=(SOURCE_ID,),
        producer_type="worker",
        producer_id="OpenCode E2E Worker",
        task_id=ROOT_TASK_ID,
        subtask_id=SUBTASK_TASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        execution_id=EXECUTION_ID,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
