"""Remove the isolated integration workspace used by the real R4 browser path."""

from __future__ import annotations

import os
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, update

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
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    GatewayOperationModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    PlanSnapshotModel,
    ProcessDescriptorModel,
    ProjectionChangeModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerModel,
    WorkspaceModel,
    WorkspaceProjectionCursorModel,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE_NAME = "Integration Test Workspace"


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
            if workspace is not None and workspace.name != WORKSPACE_NAME:
                raise RuntimeError(
                    f"refusing to clean workspace {WORKSPACE_ID}: unexpected name {workspace.name!r}"
                )
            session.execute(
                update(ExecutionModel)
                .where(ExecutionModel.workspace_id == WORKSPACE_ID)
                .values(
                    active_lease_id=None,
                    source_execution_id=None,
                    source_session_id=None,
                )
            )
            for model in (
                AttentionAcknowledgementModel,
                ProjectionChangeModel,
                WorkspaceProjectionCursorModel,
                WorkerItemPlacementModel,
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
            session.commit()
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
