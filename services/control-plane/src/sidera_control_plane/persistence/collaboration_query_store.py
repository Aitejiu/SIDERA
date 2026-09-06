from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sidera_control_plane.domain.collaboration import (
    CollaborationPolicySnapshotV1,
    CollaborationPolicySnapshotV2,
)
from sidera_control_plane.domain.collaboration_projections import (
    CollaborationDecisionProjectionFact,
    CollaborationExecutionProjectionFact,
    CollaborationGovernanceHoldProjectionFact,
    CollaborationGovernanceResolutionProjectionFact,
    CollaborationIntentApplicationProjectionFact,
    CollaborationIntentProjectionFact,
    CollaborationProjectionFacts,
    CollaborationReleaseProposalProjectionFact,
    CollaborationRequestDependencyProjectionFact,
    CollaborationRequestProjectionFact,
    CollaborationRunProjectionFact,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.plan_normalization import (
    CanonicalTaskPlan,
    PlanNormalizer,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CollaborationGovernanceHoldModel,
    CollaborationGovernanceResolutionModel,
    CollaborationReleaseProposalModel,
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
    WorkerItemModel,
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

    def load_for_worker_item(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> CollaborationProjectionFacts | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            root_task_id = session.scalar(
                select(WorkerItemModel.root_task_id).where(
                    WorkerItemModel.workspace_id == workspace_id,
                    WorkerItemModel.id == worker_item_id,
                )
            )
            if root_task_id is None:
                return None
            plan_row = _current_plan_row(session, workspace_id, root_task_id)
            if plan_row is None:
                return None
            return self.load_in_session(
                session,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                plan=self._normalizer.normalize(_plan(plan_row)),
            )

    def load_for_proposal(
        self, workspace_id: UUID, proposal_id: UUID
    ) -> CollaborationProjectionFacts | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            root_task_id = session.scalar(
                select(CollaborationGovernanceHoldModel.root_task_id)
                .join(
                    CollaborationReleaseProposalModel,
                    (
                        CollaborationReleaseProposalModel.hold_id
                        == CollaborationGovernanceHoldModel.id
                    )
                    & (
                        CollaborationReleaseProposalModel.workspace_id
                        == CollaborationGovernanceHoldModel.workspace_id
                    ),
                )
                .where(
                    CollaborationReleaseProposalModel.workspace_id == workspace_id,
                    CollaborationReleaseProposalModel.id == proposal_id,
                    CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                )
            )
            if root_task_id is None:
                return None
            plan_row = _current_plan_row(session, workspace_id, root_task_id)
            if plan_row is None:
                return None
            return self.load_in_session(
                session,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                plan=self._normalizer.normalize(_plan(plan_row)),
            )

    def load_in_session(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        root_task_id: UUID,
        plan: CanonicalTaskPlan,
        include_superseded: bool = False,
    ) -> CollaborationProjectionFacts | None:
        run_conditions = [
            CollaborationRunModel.workspace_id == workspace_id,
            CollaborationRunModel.root_task_id == root_task_id,
            CollaborationRunModel.plan_snapshot_id == plan.snapshot_id,
            CollaborationRunModel.plan_generation == plan.generation,
        ]
        if not include_superseded:
            run_conditions.append(CollaborationRunModel.status != "superseded")
        run = session.scalar(
            select(CollaborationRunModel)
            .where(*run_conditions)
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
            .order_by(
                RuntimeIntentModel.decision_id.asc(), RuntimeIntentModel.ordinal.asc()
            )
        ).all()
        intents = tuple(_intent(row) for row in intent_rows)
        intent_ids = {intent.id for intent in intents}
        application_rows = session.scalars(
            select(RuntimeIntentApplicationModel)
            .where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
                RuntimeIntentApplicationModel.intent_id.in_(
                    intent_ids or {UUID(int=0)}
                ),
            )
            .order_by(RuntimeIntentApplicationModel.intent_id.asc())
        ).all()
        applications = tuple(_application(row) for row in application_rows)

        current_worker_item_ids = {request.worker_item_id for request in requests}
        hold_rows = session.scalars(
            select(CollaborationGovernanceHoldModel)
            .where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.root_task_id == root_task_id,
                CollaborationGovernanceHoldModel.run_id == run.id,
                CollaborationGovernanceHoldModel.plan_snapshot_id == plan.snapshot_id,
                CollaborationGovernanceHoldModel.plan_generation == plan.generation,
                CollaborationGovernanceHoldModel.worker_item_id.in_(
                    current_worker_item_ids or {UUID(int=0)}
                ),
            )
            .order_by(
                CollaborationGovernanceHoldModel.worker_item_id.asc(),
                CollaborationGovernanceHoldModel.created_at.asc(),
                CollaborationGovernanceHoldModel.id.asc(),
            )
        ).all()
        holds = tuple(_hold(row) for row in hold_rows)
        proposal_rows = session.scalars(
            select(CollaborationReleaseProposalModel)
            .where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.run_id == run.id,
                CollaborationReleaseProposalModel.execution_request_id.in_(
                    request_ids or {UUID(int=0)}
                ),
            )
            .order_by(
                CollaborationReleaseProposalModel.execution_request_id.asc(),
                CollaborationReleaseProposalModel.created_at.asc(),
                CollaborationReleaseProposalModel.id.asc(),
            )
        ).all()
        request_by_id = {request.id: request for request in requests}
        hold_by_id = {hold.id: hold for hold in holds}
        proposals = tuple(
            _proposal(
                row,
                worker_item_id=(
                    request_by_id[row.execution_request_id].worker_item_id
                    if row.execution_request_id in request_by_id
                    else hold_by_id[row.hold_id].worker_item_id
                ),
            )
            for row in proposal_rows
            if row.execution_request_id in request_by_id or row.hold_id in hold_by_id
        )
        proposal_ids = {proposal.id for proposal in proposals}
        resolution_rows = session.scalars(
            select(CollaborationGovernanceResolutionModel)
            .where(
                CollaborationGovernanceResolutionModel.workspace_id == workspace_id,
                CollaborationGovernanceResolutionModel.proposal_id.in_(
                    proposal_ids or {UUID(int=0)}
                ),
            )
            .order_by(
                CollaborationGovernanceResolutionModel.proposal_id.asc(),
                CollaborationGovernanceResolutionModel.created_at.asc(),
                CollaborationGovernanceResolutionModel.id.asc(),
            )
        ).all()
        resolutions = tuple(_resolution(row) for row in resolution_rows)
        applications = _attach_governance_lineage(
            applications,
            proposals=proposals,
            resolutions=resolutions,
        )

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
                latest_decision_summary=(decisions[-1].summary if decisions else None),
                latest_decision_at=(decisions[-1].created_at if decisions else None),
            ),
            requests=requests,
            dependencies=dependencies,
            executions=executions,
            released_request_ids=released_request_ids,
            decisions=decisions,
            intents=intents,
            applications=applications,
            holds=holds,
            proposals=proposals,
            resolutions=resolutions,
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
        lineage_by_execution = {
            item.execution_id: item.lineage_id for item in executions
        }
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
                artifact_by_lineage[lineage_id].add(
                    f"artifact_type:{row.artifact_type}"
                )

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


def _policy_snapshot(
    value: Mapping[str, object],
) -> CollaborationPolicySnapshotV1 | CollaborationPolicySnapshotV2 | None:
    try:
        schema_version = value.get("schema_version")
        if schema_version == 1:
            return CollaborationPolicySnapshotV1.from_mapping(value)
        if schema_version == 2:
            return CollaborationPolicySnapshotV2.from_mapping(value)
        return None
    except (ValidationError, KeyError, TypeError, ValueError):
        return None


def _request(
    row: ExecutionRequestModel, plan: CanonicalTaskPlan
) -> CollaborationRequestProjectionFact:
    worker_plan = next(
        (
            item
            for item in plan.worker_items
            if item.worker_item_id == row.worker_item_id
            or item.key == row.worker_item_key
        ),
        None,
    )
    return CollaborationRequestProjectionFact(
        id=row.id,
        subtask_key=(
            worker_plan.subtask_key if worker_plan is not None else row.worker_item_key
        ),
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
        summary=_bounded(row.summary, 2000),
        reason_code=_bounded(row.reason_code, 255),
        created_at=_as_utc(row.created_at),
    )


def _application(
    row: RuntimeIntentApplicationModel,
) -> CollaborationIntentApplicationProjectionFact:
    return CollaborationIntentApplicationProjectionFact(
        intent_id=row.intent_id,
        status=row.status,
        result_code=(
            "governance_proposal_duplicate"
            if row.status == "duplicate"
            else _bounded(row.result_code, 255)
        ),
        result_summary=_bounded(row.result_summary, 2000),
        release_id=row.release_id,
        error_code=(row.result_code if row.status in {"rejected", "stale"} else None),
        created_at=_as_utc(row.created_at),
        applied_at=_optional_utc(row.applied_at),
    )


def _hold(
    row: CollaborationGovernanceHoldModel,
) -> CollaborationGovernanceHoldProjectionFact:
    return CollaborationGovernanceHoldProjectionFact(
        id=row.id,
        run_id=row.run_id,
        worker_item_id=row.worker_item_id,
        status=row.status,
        resource_version=row.resource_version,
        reason=_bounded_optional(row.reason, 2000),
        created_at=_as_utc(row.created_at),
        resolved_at=_optional_utc(row.resolved_at),
        created_by_actor_id=_bounded(row.created_by_actor_id, 255),
        created_via=_bounded(row.created_via, 32),
    )


def _proposal(
    row: CollaborationReleaseProposalModel,
    *,
    worker_item_id: UUID,
) -> CollaborationReleaseProposalProjectionFact:
    return CollaborationReleaseProposalProjectionFact(
        id=row.id,
        hold_id=row.hold_id,
        run_id=row.run_id,
        decision_id=row.decision_id,
        intent_id=row.intent_id,
        execution_request_id=row.execution_request_id,
        worker_item_id=worker_item_id,
        target_request_version=row.target_request_version,
        status=row.status,
        resource_version=row.resource_version,
        reason_code=_bounded(row.reason_code, 255),
        summary=_bounded(row.summary, 2000),
        created_at=_as_utc(row.created_at),
        resolved_at=_optional_utc(row.resolved_at),
        input_selection_id=row.input_selection_id,
        input_selection_hash=row.input_selection_hash,
    )


def _resolution(
    row: CollaborationGovernanceResolutionModel,
) -> CollaborationGovernanceResolutionProjectionFact:
    return CollaborationGovernanceResolutionProjectionFact(
        id=row.id,
        proposal_id=row.proposal_id,
        action=_bounded(row.action, 32),
        result=_bounded(row.result, 32),
        actor_id=_bounded(row.actor_id, 255),
        transport=_bounded(row.transport, 32),
        created_at=_as_utc(row.created_at),
    )


def _attach_governance_lineage(
    applications: tuple[CollaborationIntentApplicationProjectionFact, ...],
    *,
    proposals: tuple[CollaborationReleaseProposalProjectionFact, ...],
    resolutions: tuple[CollaborationGovernanceResolutionProjectionFact, ...],
) -> tuple[CollaborationIntentApplicationProjectionFact, ...]:
    proposal_by_intent = {proposal.intent_id: proposal for proposal in proposals}
    resolution_by_proposal = {
        resolution.proposal_id: resolution for resolution in resolutions
    }
    result: list[CollaborationIntentApplicationProjectionFact] = []
    for application in applications:
        if application.status == "duplicate":
            result.append(
                replace(
                    application,
                    result_code="governance_proposal_duplicate",
                    proposal_id=None,
                    proposal_status=None,
                    resolution_action=None,
                    resolution_result=None,
                    resolution_actor=None,
                    resolution_transport=None,
                    proposal_created_at=None,
                    proposal_resolved_at=None,
                    resolution_created_at=None,
                )
            )
            continue
        proposal = proposal_by_intent.get(application.intent_id)
        resolution = (
            resolution_by_proposal.get(proposal.id) if proposal is not None else None
        )
        result.append(
            replace(
                application,
                proposal_id=proposal.id if proposal is not None else None,
                proposal_status=proposal.status if proposal is not None else None,
                resolution_action=resolution.action if resolution is not None else None,
                resolution_result=resolution.result if resolution is not None else None,
                resolution_actor=resolution.actor_id
                if resolution is not None
                else None,
                resolution_transport=(
                    resolution.transport if resolution is not None else None
                ),
                proposal_created_at=(
                    proposal.created_at if proposal is not None else None
                ),
                proposal_resolved_at=(
                    proposal.resolved_at if proposal is not None else None
                ),
                resolution_created_at=(
                    resolution.created_at if resolution is not None else None
                ),
            )
        )
    return tuple(result)


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
    return (
        value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    )


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


def _bounded(value: str, limit: int) -> str:
    return value[:limit]


def _bounded_optional(value: str | None, limit: int) -> str | None:
    return _bounded(value, limit) if value is not None else None
