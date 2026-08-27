from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from sidera_control_plane.domain.context_patches import (
    ContextPatchAcknowledgement,
    ContextPatchV1,
)
from sidera_control_plane.persistence.models import (
    ContextPatchAcknowledgementModel,
    ContextPatchModel,
)


class ContextPatchRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, patch: ContextPatchV1) -> None:
        self._session.add(
            ContextPatchModel(
                id=patch.id,
                workspace_id=patch.workspace_id,
                root_task_id=patch.root_task_id,
                worker_item_id=patch.worker_item_id,
                plan_transition_id=patch.plan_transition_id,
                worker_transition_id=patch.worker_transition_id,
                base_plan_snapshot_id=patch.base_plan_snapshot_id,
                base_generation=patch.base_generation,
                target_plan_snapshot_id=patch.target_plan_snapshot_id,
                target_generation=patch.target_generation,
                base_context_package_id=patch.base_context_package_id,
                base_package_hash=patch.base_package_hash,
                base_work_context_revision_id=patch.base_work_context_revision_id,
                base_context_hash=patch.base_context_hash,
                target_context_version=patch.target_context_version,
                scope=patch.scope,
                changed_requirements=list(patch.changed_requirements),
                changed_acceptance_refs=list(patch.changed_acceptance_refs),
                changed_dependency_refs=list(patch.changed_dependency_refs),
                decision_refs=list(patch.decision_refs),
                artifact_refs=list(patch.artifact_refs),
                evidence_refs=list(patch.evidence_refs),
                intervention_refs=list(patch.intervention_refs),
                unfinished_work=patch.unfinished_work,
                checkpoint_ref=patch.checkpoint_ref,
                patch_hash=patch.patch_hash,
                proposer=patch.proposer,
                expires_at=patch.expires_at,
                created_at=patch.created_at,
            )
        )

    def get(
        self,
        patch_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ContextPatchV1 | None:
        statement = select(ContextPatchModel).where(
            ContextPatchModel.id == patch_id,
            ContextPatchModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_patch(row) if row is not None else None

    def get_by_hash(
        self, patch_hash: str, workspace_id: UUID
    ) -> ContextPatchV1 | None:
        row = self._session.scalar(
            select(ContextPatchModel).where(
                ContextPatchModel.patch_hash == patch_hash,
                ContextPatchModel.workspace_id == workspace_id,
            )
        )
        return _to_patch(row) if row is not None else None

    def list_for_worker_item(
        self, worker_item_id: UUID, workspace_id: UUID
    ) -> tuple[ContextPatchV1, ...]:
        rows = self._session.scalars(
            select(ContextPatchModel)
            .where(
                ContextPatchModel.worker_item_id == worker_item_id,
                ContextPatchModel.workspace_id == workspace_id,
            )
            .order_by(ContextPatchModel.created_at.asc(), ContextPatchModel.id.asc())
        ).all()
        return tuple(_to_patch(row) for row in rows)

    def add_acknowledgement(
        self,
        acknowledgement: ContextPatchAcknowledgement,
        workspace_id: UUID,
    ) -> None:
        self._session.add(
            ContextPatchAcknowledgementModel(
                patch_id=acknowledgement.patch_id,
                workspace_id=workspace_id,
                patch_hash=acknowledgement.patch_hash,
                execution_id=acknowledgement.execution_id,
                session_id=acknowledgement.session_id,
                operation_id=acknowledgement.operation_id,
                accepted_sequence=acknowledgement.accepted_sequence,
                adapter_ref=acknowledgement.adapter_ref,
                acknowledged_at=acknowledgement.acknowledged_at,
            )
        )

    def get_acknowledgement(
        self, patch_id: UUID, workspace_id: UUID
    ) -> ContextPatchAcknowledgement | None:
        row = self._session.scalar(
            select(ContextPatchAcknowledgementModel).where(
                ContextPatchAcknowledgementModel.patch_id == patch_id,
                ContextPatchAcknowledgementModel.workspace_id == workspace_id,
            )
        )
        return _to_acknowledgement(row) if row is not None else None


def _to_patch(row: ContextPatchModel) -> ContextPatchV1:
    return ContextPatchV1(
        id=row.id,
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        worker_item_id=row.worker_item_id,
        plan_transition_id=row.plan_transition_id,
        worker_transition_id=row.worker_transition_id,
        base_plan_snapshot_id=row.base_plan_snapshot_id,
        base_generation=row.base_generation,
        target_plan_snapshot_id=row.target_plan_snapshot_id,
        target_generation=row.target_generation,
        base_context_package_id=row.base_context_package_id,
        base_package_hash=row.base_package_hash,
        base_work_context_revision_id=row.base_work_context_revision_id,
        base_context_hash=row.base_context_hash,
        target_context_version=row.target_context_version,
        scope=cast(Literal["worker_item"], row.scope),
        changed_requirements=tuple(row.changed_requirements),
        changed_acceptance_refs=tuple(row.changed_acceptance_refs),
        changed_dependency_refs=tuple(row.changed_dependency_refs),
        decision_refs=tuple(row.decision_refs),
        artifact_refs=tuple(row.artifact_refs),
        evidence_refs=tuple(row.evidence_refs),
        intervention_refs=tuple(row.intervention_refs),
        unfinished_work=row.unfinished_work,
        checkpoint_ref=row.checkpoint_ref,
        patch_hash=row.patch_hash,
        proposer=row.proposer,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


def _to_acknowledgement(
    row: ContextPatchAcknowledgementModel,
) -> ContextPatchAcknowledgement:
    return ContextPatchAcknowledgement(
        patch_id=row.patch_id,
        patch_hash=row.patch_hash,
        execution_id=row.execution_id,
        session_id=row.session_id,
        operation_id=row.operation_id,
        accepted_sequence=row.accepted_sequence,
        adapter_ref=row.adapter_ref,
        acknowledged_at=row.acknowledged_at,
    )
