"""Remove the isolated integration workspace used by the real R4 browser path."""

from __future__ import annotations

import os
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, text, update

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    AttentionAcknowledgementModel,
    CommandModel,
    ContextAssemblyRequestModel,
    ContextPackageAcknowledgementModel,
    ContextPackageKnowledgeRefModel,
    ContextPackageMaterializationAuditModel,
    ContextPackageModel,
    ContextPatchAcknowledgementModel,
    ContextPatchModel,
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
    KnowledgeCandidateSubmissionModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    PlanEntityLineageModel,
    PlanningBriefRevisionModel,
    PlanningClarificationModel,
    PlanningProfileModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunLeaseModel,
    PlanningRunModel,
    PlanSnapshotModel,
    PlanTransitionModel,
    ProcessDescriptorModel,
    ProjectionChangeModel,
    ProposalMaterializationModel,
    RootTaskPlanHeadModel,
    SchedulingBlockModel,
    TaskModel,
    WorkContextModel,
    WorkContextRevisionModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerModel,
    WorkerRuntimeConfigurationModel,
    WorkerTransitionModel,
    WorkspaceModel,
    WorkspaceProjectionCursorModel,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE_NAME = "Integration Test Workspace"
R6_WORKSPACE_NAME = "SIDERA R6.1 revision vertical workspace"
ALLOWED_WORKSPACE_NAMES = frozenset({WORKSPACE_NAME, R6_WORKSPACE_NAME})


def main() -> int:
    if os.environ.get("SIDERA_ALLOW_E2E_SEED") != "1":
        raise SystemExit("SIDERA_ALLOW_E2E_SEED=1 is required for cleanup")
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_DATABASE_URL is required for cleanup")
    database = Database(database_url)
    try:
        with database.session() as session:
            workspace = session.get(WorkspaceModel, WORKSPACE_ID)
            if workspace is not None and workspace.name not in ALLOWED_WORKSPACE_NAMES:
                raise RuntimeError(
                    f"refusing to clean workspace {WORKSPACE_ID}: unexpected name {workspace.name!r}"
                )
            session.execute(
                update(PlanningRequestModel)
                .where(PlanningRequestModel.workspace_id == WORKSPACE_ID)
                .values(
                    current_brief_revision=None,
                    current_proposal_revision=None,
                    current_proposal_content_hash=None,
                )
            )
            session.execute(
                delete(ContextPackageKnowledgeRefModel).where(
                    ContextPackageKnowledgeRefModel.package_id.in_(
                        select(ContextPackageModel.id).where(
                            ContextPackageModel.workspace_id == WORKSPACE_ID
                        )
                    )
                )
            )
            session.execute(
                text(
                    "ALTER TABLE work_contexts "
                    "DROP CONSTRAINT IF EXISTS fk_work_contexts_current_revision"
                )
            )
            session.execute(
                update(ExecutionModel)
                .where(ExecutionModel.workspace_id == WORKSPACE_ID)
                .values(
                    active_lease_id=None,
                    source_execution_id=None,
                    source_session_id=None,
                    worker_item_id=None,
                )
            )
            for model in (
                AttentionAcknowledgementModel,
                SchedulingBlockModel,
                ProjectionChangeModel,
                WorkspaceProjectionCursorModel,
                ContextPackageAcknowledgementModel,
                ContextPackageMaterializationAuditModel,
                ContextPackageModel,
                ContextAssemblyRequestModel,
                ContextPatchAcknowledgementModel,
                ContextPatchModel,
                PlanEntityLineageModel,
                WorkerTransitionModel,
                KnowledgeCandidateSubmissionModel,
                WorkContextRevisionModel,
                WorkContextModel,
                ExecutionRequestDependencyModel,
                ExecutionRequestModel,
                ProposalMaterializationModel,
                PlanTransitionModel,
                PlanningProposalRevisionModel,
                PlanningClarificationModel,
                PlanningRunLeaseModel,
                PlanningRunModel,
                PlanningBriefRevisionModel,
                PlanningRequestModel,
                PlanningProfileModel,
                WorkerItemPlacementModel,
                RootTaskPlanHeadModel,
                PlanSnapshotModel,
                ExecutionInputModel,
                EvidenceClaimModel,
                ArtifactModel,
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
                WorkerItemModel,
                WorkerModel,
                OutboxMessageModel,
                EventModel,
                CommandModel,
                TaskModel,
            ):
                if model is OutboxMessageModel:
                    session.execute(
                        delete(model).where(
                            model.event_id.in_(
                                select(EventModel.id).where(
                                    EventModel.workspace_id == WORKSPACE_ID
                                )
                            )
                        )
                    )
                else:
                    session.execute(
                        delete(model).where(
                            cast(Any, model).workspace_id == WORKSPACE_ID
                        )
                    )
            if workspace is not None:
                session.delete(workspace)
            session.execute(
                text(
                    "ALTER TABLE work_contexts "
                    "ADD CONSTRAINT fk_work_contexts_current_revision "
                    "FOREIGN KEY (current_revision_id) REFERENCES work_context_revisions (id) "
                    "ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED"
                )
            )
            session.commit()
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
