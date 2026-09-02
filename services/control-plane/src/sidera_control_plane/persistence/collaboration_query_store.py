from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sidera_control_plane.domain.collaboration import CollaborationPolicySnapshotV1
from sidera_control_plane.domain.collaboration_projections import (
    CollaborationDecisionProjectionFact,
    CollaborationExecutionProjectionFact,
    CollaborationIntentApplicationProjectionFact,
    CollaborationIntentProjectionFact,
    CollaborationProjectionFacts,
    CollaborationRequestDependencyProjectionFact,
    CollaborationRequestProjectionFact,
    CollaborationRunProjectionFact,
)
from sidera_control_plane.domain.plan_normalization import (
    CanonicalTaskPlan,
    PlanNormalizer,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CollaborationRunModel,
    EvidenceClaimModel,
    ExecutionModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    ModuleDefinitionModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    RuntimeDecisionModel,
    RuntimeIntentApplicationModel,
    RuntimeIntentModel,
    RuntimeReleaseModel,
)


class SqlAlchemyCollaborationQueryStore:
    """Read the current Plan Head's bounded collaboration facts."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._normalizer = PlanNormalizer()

    def load(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> CollaborationProjectionFacts | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            plan_row = _current_plan_row(session, workspace_id, root_task_id)
            if plan_row is None:
                return None
            plan = self._normalizer.normalize(_plan(plan_row))
            return self.load_in_session(
                session,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                plan=plan,
            )

    def load_in_session(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        root_task_id: UUID,
        plan: CanonicalTaskPlan,
    ) -> CollaborationProjectionFacts | None:
        run = session.scalar(
            select(CollaborationRunModel)
            .where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.root_task_id == root_task_id,
                CollaborationRunModel.plan_snapshot_id == plan.snapshot_id,
                CollaborationRunModel.plan_generation == plan.generation,
                CollaborationRunModel.status != "superseded",
            )
            .order_by(CollaborationRunModel.updated_at.desc())
            .limit(1)
        )
        if run is None:
            return None
        policy = _policy_snapshot(run.policy_snapshot_json)
        if policy is None or not policy.default_policy.enabled:
            return None

        request_rows = session.scalars(
            select(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.root_task_id == root_task_id,
                ExecutionRequestModel.plan_snapshot_id == plan.snapshot_id,
                ExecutionRequestModel.plan_generation == plan.generation,
            )
            .order_by(ExecutionRequestModel.worker_item_key.asc())
        ).all()
        requests = tuple(_request(row, plan) for row in request_rows)
        request_ids = {request.id for request in requests}

        dependency_rows = session.scalars(
            select(ExecutionRequestDependencyModel)
            .where(
                ExecutionRequestDependencyModel.workspace_id == workspace_id,
                ExecutionRequestDependencyModel.downstream_request_id.in_(
                    request_ids or {UUID(int=0)}
                ),
                ExecutionRequestDependencyModel.upstream_request_id.in_(
                    request_ids or {UUID(int=0)}
                ),
            )
            .order_by(
                ExecutionRequestDependencyModel.downstream_request_id.asc(),
                ExecutionRequestDependencyModel.upstream_request_id.asc(),
            )
        ).all()
        dependencies = tuple(_dependency(row) for row in dependency_rows)

        release_rows = session.scalars(
            select(RuntimeReleaseModel)
            .where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.run_id == run.id,
                RuntimeReleaseModel.plan_snapshot_id == plan.snapshot_id,
                RuntimeReleaseModel.plan_generation == plan.generation,
            )
            .order_by(RuntimeReleaseModel.issued_at.asc(), RuntimeReleaseModel.id.asc())
        ).all()
        released_request_ids = tuple(row.execution_request_id for row in release_rows)

        executions, execution_ids = self._executions(
            session,
            workspace_id=workspace_id,
            requests=requests,
        )
        artifact_refs, evidence_refs = self._outputs(
            session,
            workspace_id=workspace_id,
            execution_ids=execution_ids,
            executions=executions,
        )
        executions = tuple(
            _with_outputs(execution, artifact_refs, evidence_refs)
            for execution in executions
        )

        decisions = tuple(
            _decision(row)
            for row in session.scalars(
                select(RuntimeDecisionModel)
                .where(
                    RuntimeDecisionModel.workspace_id == workspace_id,
                    RuntimeDecisionModel.run_id == run.id,
                )
                .order_by(
                    RuntimeDecisionModel.created_at.asc(),
                    RuntimeDecisionModel.id.asc(),
                )
            ).all()
        )
        decision_ids = {decision.id for decision in decisions}
        intent_rows = session.scalars(
            select(RuntimeIntentModel)
            .where(
                RuntimeIntentModel.workspace_id == workspace_id,
                RuntimeIntentModel.run_id == run.id,
                RuntimeIntentModel.decision_id.in_(decision_ids or {UUID(int=0)}),
            )
            .order_by(RuntimeIntentModel.decision_id.asc(), RuntimeIntentModel.ordinal.asc())
        ).all()
        intents = tuple(_intent(row) for row in intent_rows)
        intent_ids = {intent.id for intent in intents}
        application_rows = session.scalars(
            select(RuntimeIntentApplicationModel)
            .where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
                RuntimeIntentApplicationModel.intent_id.in_(intent_ids or {UUID(int=0)}),
            )
            .order_by(RuntimeIntentApplicationModel.intent_id.asc())
        ).all()
        applications = tuple(_application(row) for row in application_rows)

        provider = session.scalar(
            select(ModuleDefinitionModel.provider).where(
                ModuleDefinitionModel.workspace_id == workspace_id,
                ModuleDefinitionModel.id == run.runtime_definition_id,
            )
        )
        return CollaborationProjectionFacts(
            plan=plan,
            run=CollaborationRunProjectionFact(
                id=run.id,
                workspace_id=run.workspace_id,
                root_task_id=run.root_task_id,
                plan_snapshot_id=run.plan_snapshot_id,
                plan_generation=run.plan_generation,
                status=run.status,
                provider=str(provider or "unknown"),
                interface_version=run.runtime_interface_version,
                config_version=run.runtime_config_version,
                strategy_label=policy.default_policy.strategy_id.value,
                policy_enabled=policy.default_policy.enabled,
                blocked_reason=run.blocked_reason,
                updated_at=_as_utc(run.updated_at),
                strategy_by_subtask={
                    item.subtask_key: item.strategy_id.value
                    for item in policy.subtask_overrides
                },
                latest_decision_summary=(
                    decisions[-1].summary if decisions else None
                ),
                latest_decision_at=(
                    decisions[-1].created_at if decisions else None
                ),
            ),
            requests=requests,
            dependencies=dependencies,
            executions=executions,
            released_request_ids=released_request_ids,
            decisions=decisions,
            intents=intents,
            applications=applications,
        )

    @staticmethod
    def _executions(
        session: Session,
        *,
        workspace_id: UUID,
        requests: tuple[CollaborationRequestProjectionFact, ...],
    ) -> tuple[tuple[CollaborationExecutionProjectionFact, ...], set[UUID]]:
        lineage_to_request = {
            row.execution_lineage_id: row.id
            for row in requests
            if row.execution_lineage_id is not None
        }
        if not lineage_to_request:
            return (), set()
        rows = session.scalars(
            select(ExecutionModel)
            .where(
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.lineage_id.in_(set(lineage_to_request)),
            )
            .order_by(ExecutionModel.attempt.asc(), ExecutionModel.id.asc())
        ).all()
        facts = tuple(
            CollaborationExecutionProjectionFact(
                request_id=lineage_to_request[row.lineage_id],
                execution_id=row.id,
                lineage_id=row.lineage_id,
                status=row.status,
                attempt=row.attempt,
                failure_code=row.failure_code,
                started_at=_optional_utc(row.started_at),
                finished_at=_optional_utc(row.finished_at),
                updated_at=_as_utc(row.updated_at),
            )
            for row in rows
            if row.lineage_id in lineage_to_request
        )
        return facts, {row.id for row in rows}

    @staticmethod
    def _outputs(
        session: Session,
        *,
        workspace_id: UUID,
        execution_ids: set[UUID],
        executions: tuple[CollaborationExecutionProjectionFact, ...],
    ) -> tuple[dict[UUID, tuple[str, ...]], dict[UUID, tuple[str, ...]]]:
        if not execution_ids:
            return {}, {}
        lineage_by_execution = {item.execution_id: item.lineage_id for item in executions}
        artifact_by_lineage: dict[UUID, set[str]] = defaultdict(set)
        artifact_rows = session.scalars(
            select(ArtifactModel).where(
                ArtifactModel.workspace_id == workspace_id,
                ArtifactModel.execution_id.in_(execution_ids),
            )
        ).all()
        for row in artifact_rows:
            lineage_id = lineage_by_execution.get(row.execution_id)
            if lineage_id is not None:
                artifact_by_lineage[lineage_id].add(f"artifact_type:{row.artifact_type}")

        evidence_by_lineage: dict[UUID, set[str]] = defaultdict(set)
        evidence_rows = session.scalars(
            select(EvidenceClaimModel).where(
                EvidenceClaimModel.workspace_id == workspace_id,
                EvidenceClaimModel.execution_id.in_(execution_ids),
            )
        ).all()
        for row in evidence_rows:
            lineage_id = lineage_by_execution.get(row.execution_id)
            if lineage_id is not None and row.status == "candidate":
                evidence_by_lineage[lineage_id].add(row.acceptance_criterion_ref)
        return (
            {
                lineage: tuple(sorted(values))
                for lineage, values in artifact_by_lineage.items()
            },
            {
                lineage: tuple(sorted(values))
                for lineage, values in evidence_by_lineage.items()
            },
        )


def _current_plan_row(
    session: Session, workspace_id: UUID, root_task_id: UUID
) -> PlanSnapshotModel | None:
    return session.scalar(
        select(PlanSnapshotModel)
        .join(
            RootTaskPlanHeadModel,
            (RootTaskPlanHeadModel.active_snapshot_id == PlanSnapshotModel.id)
            & (RootTaskPlanHeadModel.workspace_id == PlanSnapshotModel.workspace_id)
            & (RootTaskPlanHeadModel.root_task_id == PlanSnapshotModel.root_task_id),
        )
        .where(
            PlanSnapshotModel.workspace_id == workspace_id,
            PlanSnapshotModel.root_task_id == root_task_id,
        )
    )


def _plan(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if snapshot.id != row.id or snapshot.generation != row.generation:
        raise ValueError("task plan snapshot payload does not match identity columns")
    return snapshot


def _policy_snapshot(value: Mapping[str, object]) -> CollaborationPolicySnapshotV1 | None:
    try:
        return CollaborationPolicySnapshotV1.from_mapping(value)
    except (KeyError, TypeError, ValueError):
        return None


def _request(
    row: ExecutionRequestModel, plan: CanonicalTaskPlan
) -> CollaborationRequestProjectionFact:
    worker_plan = next(
        (
            item
            for item in plan.worker_items
            if item.worker_item_id == row.worker_item_id or item.key == row.worker_item_key
        ),
        None,
    )
    return CollaborationRequestProjectionFact(
        id=row.id,
        subtask_key=(worker_plan.subtask_key if worker_plan is not None else row.worker_item_key),
        worker_item_key=row.worker_item_key,
        worker_item_id=row.worker_item_id,
        status=row.status,
        collaboration_gate_mode=row.collaboration_gate_mode,
        gate_reason=row.gate_reason,
        version=row.version,
        expected_artifact_refs=tuple(str(item) for item in row.expected_artifact_refs),
        expected_evidence_refs=tuple(str(item) for item in row.expected_evidence_refs),
        updated_at=_as_utc(row.updated_at),
        execution_lineage_id=row.execution_lineage_id,
        first_execution_id=row.first_execution_id,
    )


def _dependency(
    row: ExecutionRequestDependencyModel,
) -> CollaborationRequestDependencyProjectionFact:
    return CollaborationRequestDependencyProjectionFact(
        downstream_request_id=row.downstream_request_id,
        upstream_request_id=row.upstream_request_id,
        enforcement=row.enforcement,
        required_artifact_refs=tuple(str(item) for item in row.required_artifact_refs),
        required_evidence_refs=tuple(str(item) for item in row.required_evidence_refs),
    )


def _decision(row: RuntimeDecisionModel) -> CollaborationDecisionProjectionFact:
    return CollaborationDecisionProjectionFact(
        id=row.id,
        sequence=int(row.sequence),
        outcome=row.outcome,
        reason_code=row.reason_code,
        summary=row.summary,
        projection_id=row.projection_snapshot_id,
        projection_hash=row.projection_hash,
        created_at=_as_utc(row.created_at),
    )


def _intent(row: RuntimeIntentModel) -> CollaborationIntentProjectionFact:
    return CollaborationIntentProjectionFact(
        id=row.id,
        decision_id=row.decision_id,
        target_request_id=row.target_execution_request_id,
        ordinal=row.ordinal,
        summary=row.summary,
    )


def _application(
    row: RuntimeIntentApplicationModel,
) -> CollaborationIntentApplicationProjectionFact:
    return CollaborationIntentApplicationProjectionFact(
        intent_id=row.intent_id,
        status=row.status,
        result_code=row.result_code,
        result_summary=row.result_summary,
        release_id=row.release_id,
        error_code=(
            row.result_code if row.status in {"rejected", "stale"} else None
        ),
    )


def _with_outputs(
    execution: CollaborationExecutionProjectionFact,
    artifact_refs: Mapping[UUID, tuple[str, ...]],
    evidence_refs: Mapping[UUID, tuple[str, ...]],
) -> CollaborationExecutionProjectionFact:
    return CollaborationExecutionProjectionFact(
        request_id=execution.request_id,
        execution_id=execution.execution_id,
        lineage_id=execution.lineage_id,
        status=execution.status,
        attempt=execution.attempt,
        failure_code=execution.failure_code,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        updated_at=execution.updated_at,
        artifact_refs=artifact_refs.get(execution.lineage_id, ()),
        evidence_refs=evidence_refs.get(execution.lineage_id, ()),
    )


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None
