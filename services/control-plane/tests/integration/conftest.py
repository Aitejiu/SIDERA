import os
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text, update

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    AttentionAcknowledgementModel,
    CommandModel,
    EventModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    GatewayOperationModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    PlanningBriefRevisionModel,
    PlanningClarificationModel,
    PlanningProfileModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunLeaseModel,
    PlanningRunModel,
    PlanSnapshotModel,
    ProcessDescriptorModel,
    ProjectionChangeModel,
    ProposalMaterializationModel,
    SchedulingBlockModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerModel,
    WorkerRuntimeConfigurationModel,
    WorkspaceModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[2]

PLANNING_CLEANUP_MODELS = (
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    ProposalMaterializationModel,
    PlanningProposalRevisionModel,
    PlanningClarificationModel,
    PlanningRunLeaseModel,
    PlanningRunModel,
    PlanningBriefRevisionModel,
    PlanningRequestModel,
    PlanningProfileModel,
)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("SIDERA_DATABASE_URL")
    if url is None:
        pytest.fail("SIDERA_DATABASE_URL is required for integration tests")
    return url


@pytest.fixture(scope="session")
def database(database_url: str) -> Iterator[Database]:
    config = Config(str(CONTROL_PLANE_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    instance = Database(database_url)
    yield instance
    instance.dispose()


@pytest.fixture(autouse=True)
def clean_database(database: Database) -> Iterator[None]:
    _clear_database(database)
    yield
    if os.environ.get("SIDERA_KEEP_OPENCODE_E2E") == "1":
        return
    _clear_database(database)


def _clear_database(database: Database) -> None:
    with database.session() as session:
        session.execute(
            text(
                "TRUNCATE TABLE "
                "context_package_materialization_audits, "
                "context_package_acknowledgements, "
                "context_package_knowledge_refs, "
                "knowledge_candidate_submissions, "
                "context_packages, "
                "context_assembly_requests, "
                "work_context_revisions, "
                "work_contexts "
                "CASCADE"
            )
        )
        session.execute(
            update(PlanningRequestModel).values(
                current_brief_revision=None,
                current_proposal_revision=None,
                current_proposal_content_hash=None,
            )
        )
        session.execute(
            update(ExecutionModel).values(
                active_lease_id=None,
                source_execution_id=None,
                source_session_id=None,
                worker_item_id=None,
            )
        )
        for model in (
            ProjectionChangeModel,
            AttentionAcknowledgementModel,
            WorkspaceProjectionCursorModel,
            *PLANNING_CLEANUP_MODELS,
            WorkerItemPlacementModel,
            PlanSnapshotModel,
            ExecutionInputModel,
            EvidenceClaimModel,
            ArtifactModel,
            SchedulingBlockModel,
            WorkerItemModel,
            ExecutionEventModel,
            GatewayOperationModel,
            ProcessDescriptorModel,
            ExecutionWorkspaceModel,
            ExecutionBindingModel,
            ExecutionSessionModel,
            AssignmentLeaseModel,
            ExecutionModel,
            WorkerRuntimeConfigurationModel,
            ModuleInstanceModel,
            ModuleDefinitionModel,
            WorkerModel,
            OutboxMessageModel,
            EventModel,
            CommandModel,
            TaskModel,
            WorkspaceModel,
        ):
            session.execute(delete(model))
        session.commit()


@pytest.fixture
def workspace_id(database: Database) -> UUID:
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=WORKSPACE_ID,
                name="Integration Test Workspace",
                status="active",
                version=1,
            )
        )
        session.commit()
    return WORKSPACE_ID


@pytest.fixture
def uow_factory(database: Database) -> Callable[[], SqlAlchemyUnitOfWork]:
    return lambda: SqlAlchemyUnitOfWork(database)


@pytest.fixture
def row_counts(database: Database) -> Callable[[], dict[str, int]]:
    def count_rows() -> dict[str, int]:
        with database.session() as session:
            return {
                "tasks": session.scalar(
                    select(func.count()).select_from(TaskModel)
                )
                or 0,
                "commands": session.scalar(
                    select(func.count()).select_from(CommandModel)
                )
                or 0,
                "events": session.scalar(
                    select(func.count()).select_from(EventModel)
                )
                or 0,
                "outbox_messages": session.scalar(
                    select(func.count()).select_from(OutboxMessageModel)
                )
                or 0,
            }

    return count_rows
