from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from sidera_control_plane.application.collaboration_projection import (
    CollaborationArtifactFact,
    CollaborationEvidenceFact,
    CollaborationExecutionFact,
    CollaborationFacts,
    CollaborationPlanHeadFact,
    CollaborationRequestFact,
)
from sidera_control_plane.domain.collaboration import CollaborationRun, RuntimeRelease
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequestDependency,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    EvidenceClaimModel,
    ExecutionModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    RuntimeDecisionModel,
    RuntimeReleaseModel,
)


class SqlAlchemyCollaborationFactStore:
    """Load the bounded, root-scoped facts exposed to a collaboration runtime."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def load(
        self,
        workspace_id: UUID,
        run: CollaborationRun,
        *,
        fact_cursor: int,
    ) -> CollaborationFacts:
        self._validate_run_scope(workspace_id, run, fact_cursor)
        with self._database.session() as session:
            head = session.scalar(
                select(RootTaskPlanHeadModel).where(
                    RootTaskPlanHeadModel.workspace_id == workspace_id,
                    RootTaskPlanHeadModel.root_task_id == run.root_task_id,
                )
            )
            if head is None:
                raise ValidationError("collaboration plan head does not exist")
            if (
                head.active_snapshot_id != run.plan_snapshot_id
                or head.active_generation != run.plan_generation
                or head.active_snapshot_hash != run.plan_content_hash
            ):
                raise ValidationError(
                    "collaboration run is pinned to a stale plan head"
                )

            snapshot_row = session.scalar(
                select(PlanSnapshotModel).where(
                    PlanSnapshotModel.workspace_id == workspace_id,
                    PlanSnapshotModel.root_task_id == run.root_task_id,
                    PlanSnapshotModel.id == run.plan_snapshot_id,
                    PlanSnapshotModel.generation == run.plan_generation,
                )
            )
            if snapshot_row is None:
                raise ValidationError("collaboration plan snapshot does not exist")
            snapshot = TaskPlanSnapshot.from_payload(dict(snapshot_row.payload))
            self._validate_snapshot_identity(snapshot, run)

            requests = self._load_requests(session, run, snapshot)
            request_ids = tuple(item.id for item in requests)
            dependencies = self._load_dependencies(session, run, request_ids)
            lineage_ids = tuple(
                item.execution_lineage_id
                for item in requests
                if item.execution_lineage_id is not None
            )
            executions = self._load_executions(session, workspace_id, lineage_ids)
            execution_ids = tuple(item.id for item in executions)
            artifacts = self._load_artifacts(session, workspace_id, execution_ids)
            evidence = self._load_evidence(session, workspace_id, execution_ids)
            releases = self._load_releases(session, run, request_ids)
            previous_strategy_state = self._load_previous_strategy_state(session, run)

        return CollaborationFacts(
            plan_head=CollaborationPlanHeadFact(
                workspace_id=workspace_id,
                root_task_id=run.root_task_id,
                run_id=run.id,
                plan_snapshot_id=run.plan_snapshot_id,
                plan_generation=run.plan_generation,
                plan_content_hash=run.plan_content_hash,
                run_resource_version=run.resource_version,
                runtime_capability_snapshot=run.runtime_capability_snapshot,
                policy_snapshot=run.policy_snapshot,
            ),
            plan_snapshot=snapshot,
            requests=requests,
            dependencies=dependencies,
            executions=executions,
            artifacts=artifacts,
            evidence=evidence,
            releases=releases,
            previous_strategy_state=previous_strategy_state,
        )

    @staticmethod
    def _validate_run_scope(
        workspace_id: UUID, run: CollaborationRun, fact_cursor: int
    ) -> None:
        if run.workspace_id != workspace_id:
            raise ValidationError("collaboration run belongs to another workspace")
        if fact_cursor < 0:
            raise ValidationError("fact_cursor must be non-negative")
        if fact_cursor < run.last_evaluated_change_cursor:
            raise ValidationError("fact_cursor is behind the run watermark")

    @staticmethod
    def _validate_snapshot_identity(
        snapshot: TaskPlanSnapshot, run: CollaborationRun
    ) -> None:
        if (
            snapshot.id != run.plan_snapshot_id
            or snapshot.workspace_id != run.workspace_id
            or snapshot.root_task_id != run.root_task_id
            or snapshot.generation != run.plan_generation
        ):
            raise ValidationError("plan snapshot payload does not match its identity")

    @staticmethod
    def _load_requests(
        session: Session,
        run: CollaborationRun,
        snapshot: TaskPlanSnapshot,
    ) -> tuple[CollaborationRequestFact, ...]:
        subtask_keys = {
            item.task_id: item.key
            for item in snapshot.subtasks
            if item.task_id is not None
        }
        rows = session.execute(
            select(
                ExecutionRequestModel.id,
                ExecutionRequestModel.worker_item_key,
                ExecutionRequestModel.subtask_task_id,
                ExecutionRequestModel.worker_item_id,
                ExecutionRequestModel.version,
                ExecutionRequestModel.status,
                ExecutionRequestModel.collaboration_gate_mode,
                ExecutionRequestModel.gate_reason,
                ExecutionRequestModel.execution_lineage_id,
                ExecutionRequestModel.first_execution_id,
                ExecutionRequestModel.expected_artifact_refs,
                ExecutionRequestModel.expected_evidence_refs,
            )
            .where(
                ExecutionRequestModel.workspace_id == run.workspace_id,
                ExecutionRequestModel.root_task_id == run.root_task_id,
                ExecutionRequestModel.plan_snapshot_id == run.plan_snapshot_id,
                ExecutionRequestModel.plan_generation == run.plan_generation,
            )
            .order_by(ExecutionRequestModel.id)
        ).all()
        return tuple(
            CollaborationRequestFact(
                id=row.id,
                subtask_key=subtask_keys.get(
                    row.subtask_task_id, str(row.subtask_task_id)
                ),
                worker_item_key=row.worker_item_key,
                subtask_task_id=row.subtask_task_id,
                worker_item_id=row.worker_item_id,
                version=row.version,
                status=row.status,
                collaboration_gate_mode=row.collaboration_gate_mode,
                gate_reason=row.gate_reason,
                execution_lineage_id=row.execution_lineage_id,
                first_execution_id=row.first_execution_id,
                expected_artifact_refs=_string_tuple(row.expected_artifact_refs),
                expected_evidence_refs=_string_tuple(row.expected_evidence_refs),
            )
            for row in rows
        )

    @staticmethod
    def _load_dependencies(
        session: Session,
        run: CollaborationRun,
        request_ids: Sequence[UUID],
    ) -> tuple[ExecutionRequestDependency, ...]:
        if not request_ids:
            return ()
        downstream = aliased(ExecutionRequestModel)
        upstream = aliased(ExecutionRequestModel)
        rows = session.execute(
            select(
                ExecutionRequestDependencyModel.downstream_request_id,
                ExecutionRequestDependencyModel.upstream_request_id,
                ExecutionRequestDependencyModel.enforcement,
                ExecutionRequestDependencyModel.required_artifact_refs,
                ExecutionRequestDependencyModel.required_evidence_refs,
            )
            .select_from(ExecutionRequestDependencyModel)
            .join(
                downstream,
                downstream.id == ExecutionRequestDependencyModel.downstream_request_id,
            )
            .join(
                upstream,
                upstream.id == ExecutionRequestDependencyModel.upstream_request_id,
            )
            .where(
                ExecutionRequestDependencyModel.workspace_id == run.workspace_id,
                downstream.workspace_id == run.workspace_id,
                upstream.workspace_id == run.workspace_id,
                downstream.root_task_id == run.root_task_id,
                downstream.plan_snapshot_id == run.plan_snapshot_id,
                downstream.plan_generation == run.plan_generation,
                upstream.root_task_id == run.root_task_id,
                upstream.plan_snapshot_id == run.plan_snapshot_id,
                upstream.plan_generation == run.plan_generation,
                downstream.id.in_(request_ids),
                upstream.id.in_(request_ids),
            )
            .order_by(
                ExecutionRequestDependencyModel.downstream_request_id,
                ExecutionRequestDependencyModel.upstream_request_id,
            )
        ).all()
        return tuple(
            ExecutionRequestDependency(
                downstream_request_id=row.downstream_request_id,
                upstream_request_id=row.upstream_request_id,
                enforcement=DependencyEnforcement(row.enforcement),
                required_artifact_refs=_string_tuple(row.required_artifact_refs),
                required_evidence_refs=_string_tuple(row.required_evidence_refs),
            )
            for row in rows
        )

    @staticmethod
    def _load_executions(
        session: Session,
        workspace_id: UUID,
        lineage_ids: Sequence[UUID],
    ) -> tuple[CollaborationExecutionFact, ...]:
        if not lineage_ids:
            return ()
        rows = session.execute(
            select(
                ExecutionModel.id,
                ExecutionModel.lineage_id,
                ExecutionModel.task_id,
                ExecutionModel.worker_item_id,
                ExecutionModel.attempt,
                ExecutionModel.status,
                ExecutionModel.failure_code,
                ExecutionModel.started_at,
                ExecutionModel.finished_at,
                ExecutionModel.version,
            )
            .where(
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.lineage_id.in_(lineage_ids),
            )
            .order_by(
                ExecutionModel.lineage_id, ExecutionModel.attempt, ExecutionModel.id
            )
        ).all()
        return tuple(
            CollaborationExecutionFact(
                id=row.id,
                lineage_id=row.lineage_id,
                task_id=row.task_id,
                worker_item_id=row.worker_item_id,
                attempt=row.attempt,
                status=row.status,
                failure_code=row.failure_code,
                started_at=row.started_at,
                finished_at=row.finished_at,
                version=row.version,
            )
            for row in rows
        )

    @staticmethod
    def _load_artifacts(
        session: Session,
        workspace_id: UUID,
        execution_ids: Sequence[UUID],
    ) -> tuple[CollaborationArtifactFact, ...]:
        if not execution_ids:
            return ()
        rows = session.execute(
            select(
                ArtifactModel.id,
                ArtifactModel.execution_id,
                ArtifactModel.task_id,
                ArtifactModel.artifact_type,
                ArtifactModel.content_hash,
                ArtifactModel.size_bytes,
                ArtifactModel.media_type,
            )
            .where(
                ArtifactModel.workspace_id == workspace_id,
                ArtifactModel.execution_id.in_(execution_ids),
            )
            .order_by(ArtifactModel.execution_id, ArtifactModel.id)
        ).all()
        return tuple(
            CollaborationArtifactFact(
                id=row.id,
                execution_id=row.execution_id,
                task_id=row.task_id,
                artifact_type=row.artifact_type,
                content_hash=row.content_hash,
                size_bytes=row.size_bytes,
                media_type=row.media_type,
            )
            for row in rows
        )

    @staticmethod
    def _load_evidence(
        session: Session,
        workspace_id: UUID,
        execution_ids: Sequence[UUID],
    ) -> tuple[CollaborationEvidenceFact, ...]:
        if not execution_ids:
            return ()
        rows = session.execute(
            select(
                EvidenceClaimModel.id,
                EvidenceClaimModel.execution_id,
                EvidenceClaimModel.task_id,
                EvidenceClaimModel.acceptance_criterion_ref,
                EvidenceClaimModel.status,
                EvidenceClaimModel.artifact_ids,
            )
            .where(
                EvidenceClaimModel.workspace_id == workspace_id,
                EvidenceClaimModel.execution_id.in_(execution_ids),
            )
            .order_by(EvidenceClaimModel.execution_id, EvidenceClaimModel.id)
        ).all()
        return tuple(
            CollaborationEvidenceFact(
                id=row.id,
                execution_id=row.execution_id,
                task_id=row.task_id,
                acceptance_criterion_ref=row.acceptance_criterion_ref,
                status=row.status,
                artifact_ids=tuple(UUID(str(value)) for value in row.artifact_ids),
            )
            for row in rows
        )

    @staticmethod
    def _load_releases(
        session: Session,
        run: CollaborationRun,
        request_ids: Sequence[UUID],
    ) -> tuple[RuntimeRelease, ...]:
        if not request_ids:
            return ()
        rows = session.execute(
            select(
                RuntimeReleaseModel.id,
                RuntimeReleaseModel.workspace_id,
                RuntimeReleaseModel.run_id,
                RuntimeReleaseModel.plan_snapshot_id,
                RuntimeReleaseModel.plan_generation,
                RuntimeReleaseModel.plan_content_hash,
                RuntimeReleaseModel.decision_id,
                RuntimeReleaseModel.intent_id,
                RuntimeReleaseModel.execution_request_id,
                RuntimeReleaseModel.request_version_at_release,
                RuntimeReleaseModel.issued_at,
            )
            .where(
                RuntimeReleaseModel.workspace_id == run.workspace_id,
                RuntimeReleaseModel.run_id == run.id,
                RuntimeReleaseModel.plan_snapshot_id == run.plan_snapshot_id,
                RuntimeReleaseModel.plan_generation == run.plan_generation,
                RuntimeReleaseModel.plan_content_hash == run.plan_content_hash,
                RuntimeReleaseModel.execution_request_id.in_(request_ids),
            )
            .order_by(RuntimeReleaseModel.issued_at, RuntimeReleaseModel.id)
        ).all()
        return tuple(
            RuntimeRelease(
                id=row.id,
                workspace_id=row.workspace_id,
                run_id=row.run_id,
                plan_snapshot_id=row.plan_snapshot_id,
                plan_generation=row.plan_generation,
                plan_content_hash=row.plan_content_hash,
                decision_id=row.decision_id,
                intent_id=row.intent_id,
                execution_request_id=row.execution_request_id,
                request_version_at_release=row.request_version_at_release,
                issued_at=row.issued_at,
            )
            for row in rows
        )

    @staticmethod
    def _load_previous_strategy_state(
        session: Session, run: CollaborationRun
    ) -> dict[str, object]:
        if run.last_decision_id is None:
            return {}
        value = session.scalar(
            select(RuntimeDecisionModel.strategy_state_json).where(
                RuntimeDecisionModel.workspace_id == run.workspace_id,
                RuntimeDecisionModel.run_id == run.id,
                RuntimeDecisionModel.id == run.last_decision_id,
            )
        )
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValidationError("runtime strategy state must be an object")
        return dict(value)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("projection reference list must be an array")
    return tuple(str(item) for item in value)
