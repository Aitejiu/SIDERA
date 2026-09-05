from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import (
    InputSelectionConflict,
    ValidationError,
)
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
)
from sidera_control_plane.persistence.models import (
    CollaborationInputSelectionModel,
    CollaborationRunModel,
    ExecutionRequestModel,
    PlanSnapshotModel,
    RuntimeDecisionModel,
    RuntimeIntentModel,
    RuntimeProjectionSnapshotModel,
)


class InputSelectionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self, selection: CollaborationInputSelectionV1
    ) -> CollaborationInputSelectionV1:
        if selection.selection_hash != selection.compute_selection_hash():
            raise ValidationError(
                "input selection hash does not match its immutable contents"
            )
        self._validate_scope(selection)
        self._session.execute(
            insert(CollaborationInputSelectionModel)
            .values(**_selection_values(selection))
            .on_conflict_do_nothing(
                constraint="uq_collaboration_input_selections_intent"
            )
        )
        row = self._session.scalar(
            select(CollaborationInputSelectionModel).where(
                CollaborationInputSelectionModel.workspace_id
                == selection.workspace_id,
                CollaborationInputSelectionModel.intent_id == selection.intent_id,
            )
        )
        if row is None:
            raise RuntimeError("input selection insert returned no row")
        stored = _to_selection(row)
        if stored.selection_hash != selection.selection_hash:
            raise InputSelectionConflict()
        return stored

    def get(
        self, workspace_id: UUID, selection_id: UUID
    ) -> CollaborationInputSelectionV1 | None:
        row = self._session.scalar(
            select(CollaborationInputSelectionModel).where(
                CollaborationInputSelectionModel.workspace_id == workspace_id,
                CollaborationInputSelectionModel.id == selection_id,
            )
        )
        return _to_selection(row) if row is not None else None

    def get_by_intent(
        self, workspace_id: UUID, intent_id: UUID
    ) -> CollaborationInputSelectionV1 | None:
        row = self._session.scalar(
            select(CollaborationInputSelectionModel).where(
                CollaborationInputSelectionModel.workspace_id == workspace_id,
                CollaborationInputSelectionModel.intent_id == intent_id,
            )
        )
        return _to_selection(row) if row is not None else None

    def _validate_scope(self, value: CollaborationInputSelectionV1) -> None:
        plan = self._session.scalar(
            select(PlanSnapshotModel.id).where(
                PlanSnapshotModel.id == value.plan_snapshot_id,
                PlanSnapshotModel.workspace_id == value.workspace_id,
                PlanSnapshotModel.root_task_id == value.root_task_id,
                PlanSnapshotModel.generation == value.plan_generation,
            )
        )
        request = self._session.scalar(
            select(ExecutionRequestModel.id).where(
                ExecutionRequestModel.id == value.target_execution_request_id,
                ExecutionRequestModel.workspace_id == value.workspace_id,
                ExecutionRequestModel.plan_snapshot_id == value.plan_snapshot_id,
                ExecutionRequestModel.plan_generation == value.plan_generation,
                ExecutionRequestModel.root_task_id == value.root_task_id,
                ExecutionRequestModel.worker_item_id == value.target_worker_item_id,
                ExecutionRequestModel.version == value.target_request_version,
            )
        )
        run = self._session.scalar(
            select(CollaborationRunModel.id).where(
                CollaborationRunModel.id == value.run_id,
                CollaborationRunModel.workspace_id == value.workspace_id,
                CollaborationRunModel.root_task_id == value.root_task_id,
                CollaborationRunModel.plan_snapshot_id == value.plan_snapshot_id,
                CollaborationRunModel.plan_generation == value.plan_generation,
                CollaborationRunModel.plan_content_hash == value.plan_content_hash,
            )
        )
        projection = self._session.scalar(
            select(RuntimeProjectionSnapshotModel.id).where(
                RuntimeProjectionSnapshotModel.id == value.projection_snapshot_id,
                RuntimeProjectionSnapshotModel.workspace_id == value.workspace_id,
                RuntimeProjectionSnapshotModel.run_id == value.run_id,
                RuntimeProjectionSnapshotModel.plan_snapshot_id
                == value.plan_snapshot_id,
                RuntimeProjectionSnapshotModel.plan_generation
                == value.plan_generation,
                RuntimeProjectionSnapshotModel.content_hash == value.projection_hash,
            )
        )
        decision = self._session.scalar(
            select(RuntimeDecisionModel.id).where(
                RuntimeDecisionModel.id == value.decision_id,
                RuntimeDecisionModel.workspace_id == value.workspace_id,
                RuntimeDecisionModel.run_id == value.run_id,
                RuntimeDecisionModel.projection_snapshot_id
                == value.projection_snapshot_id,
                RuntimeDecisionModel.projection_hash == value.projection_hash,
            )
        )
        intent = self._session.scalar(
            select(RuntimeIntentModel.id).where(
                RuntimeIntentModel.id == value.intent_id,
                RuntimeIntentModel.workspace_id == value.workspace_id,
                RuntimeIntentModel.run_id == value.run_id,
                RuntimeIntentModel.decision_id == value.decision_id,
                RuntimeIntentModel.target_execution_request_id
                == value.target_execution_request_id,
                RuntimeIntentModel.target_request_version
                == value.target_request_version,
            )
        )
        if None in (plan, request, run, projection, decision, intent):
            raise ValidationError(
                "input selection references facts outside its authorized scope"
            )


def _selection_values(value: CollaborationInputSelectionV1) -> dict[str, object]:
    return {
        "id": value.id,
        "workspace_id": value.workspace_id,
        "schema_version": value.schema_version,
        "root_task_id": value.root_task_id,
        "run_id": value.run_id,
        "plan_snapshot_id": value.plan_snapshot_id,
        "plan_generation": value.plan_generation,
        "plan_content_hash": value.plan_content_hash,
        "target_worker_item_id": value.target_worker_item_id,
        "target_execution_request_id": value.target_execution_request_id,
        "target_request_version": value.target_request_version,
        "projection_snapshot_id": value.projection_snapshot_id,
        "projection_hash": value.projection_hash,
        "decision_id": value.decision_id,
        "intent_id": value.intent_id,
        "groups_json": [item.to_mapping() for item in value.groups],
        "hard_inputs_json": [item.to_mapping() for item in value.hard_inputs],
        "selection_hash": value.selection_hash,
        "created_at": value.created_at,
    }


def _to_selection(
    row: CollaborationInputSelectionModel,
) -> CollaborationInputSelectionV1:
    return CollaborationInputSelectionV1.from_mapping(
        {
            "id": str(row.id),
            "workspace_id": str(row.workspace_id),
            "schema_version": row.schema_version,
            "root_task_id": str(row.root_task_id),
            "run_id": str(row.run_id),
            "plan_snapshot_id": str(row.plan_snapshot_id),
            "plan_generation": row.plan_generation,
            "plan_content_hash": row.plan_content_hash,
            "target_worker_item_id": str(row.target_worker_item_id),
            "target_execution_request_id": str(row.target_execution_request_id),
            "target_request_version": row.target_request_version,
            "projection_snapshot_id": str(row.projection_snapshot_id),
            "projection_hash": row.projection_hash,
            "decision_id": str(row.decision_id),
            "intent_id": str(row.intent_id),
            "groups": row.groups_json,
            "hard_inputs": row.hard_inputs_json,
            "selection_hash": row.selection_hash,
            "created_at": row.created_at.isoformat(),
        }
    )
