from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sidera_control_plane.application.collaboration_projection import (
    CollaborationFacts,
    CollaborationRequestFact,
)
from sidera_control_plane.application.result_input_queries import (
    MAX_RESULT_INPUT_CONSUMERS,
    ResultInputDeliveryFact,
    ResultInputQuerySnapshot,
    ResultInputSelectionQuerySnapshot,
    ResultInputWorkerItemFact,
)
from sidera_control_plane.domain.collaboration_projections import (
    CollaborationProjectionFacts,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
)
from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.collaboration_query_store import (
    SqlAlchemyCollaborationQueryStore,
)
from sidera_control_plane.persistence.collaboration_repository import (
    CollaborationRunRepository,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.input_selection_repository import (
    InputSelectionRepository,
)
from sidera_control_plane.persistence.models import (
    CollaborationInputSelectionModel,
    CollaborationRunModel,
    ContextAssemblyRequestModel,
    ContextPackageAcknowledgementModel,
    ContextPackageMaterializationAuditModel,
    ContextPackageModel,
    ExecutionModel,
    ExecutionRequestModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    RuntimeDecisionModel,
    RuntimeIntentModel,
    RuntimeProjectionSnapshotModel,
    WorkerItemModel,
)


class SqlAlchemyResultInputQueryStore:
    """Load bounded, scope-checked result-input views from one read transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._normalizer = PlanNormalizer()
        self._projection_store = SqlAlchemyCollaborationQueryStore(database)

    def load_worker_result_inputs(
        self,
        workspace_id: UUID,
        worker_item_id: UUID,
        execution_request_id: UUID | None,
    ) -> ResultInputQuerySnapshot | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            worker_row = session.scalar(
                select(WorkerItemModel).where(
                    WorkerItemModel.workspace_id == workspace_id,
                    WorkerItemModel.id == worker_item_id,
                )
            )
            if worker_row is None:
                return None

            is_historical = execution_request_id is not None
            if execution_request_id is not None:
                request_row = session.scalar(
                    select(ExecutionRequestModel).where(
                        ExecutionRequestModel.workspace_id == workspace_id,
                        ExecutionRequestModel.id == execution_request_id,
                    )
                )
                if request_row is None or request_row.worker_item_id != worker_item_id:
                    return None
                plan_row = _plan_row_for_request(
                    session, workspace_id=workspace_id, request=request_row
                )
            else:
                plan_row = _current_plan_row(
                    session, workspace_id=workspace_id, root_task_id=worker_row.root_task_id
                )
                request_row = (
                    _current_request_for_worker(
                        session,
                        workspace_id=workspace_id,
                        worker_item_id=worker_item_id,
                        root_task_id=worker_row.root_task_id,
                        plan_row=plan_row,
                    )
                    if plan_row is not None
                    else None
                )

            worker = _worker_fact(worker_row)
            if plan_row is None:
                return ResultInputQuerySnapshot(
                    worker_item=worker,
                    request=None,
                    facts=None,
                    projection_facts=None,
                    selection=None,
                    delivery=None,
                    is_historical=is_historical,
                )

            plan = _plan(plan_row)
            facts, projection_facts, request = self._load_collaboration_facts(
                session,
                workspace_id=workspace_id,
                plan=plan,
                request_row=request_row,
                is_historical=is_historical,
            )
            selection = (
                _selection_for_request(
                    session,
                    workspace_id=workspace_id,
                    plan=plan,
                    request_row=request_row,
                )
                if request_row is not None
                else None
            )
            execution = (
                _latest_execution_for_request(
                    session, workspace_id=workspace_id, request_row=request_row
                )
                if request_row is not None
                else None
            )
            delivery = (
                _delivery_for_execution(
                    session,
                    workspace_id=workspace_id,
                    execution=execution,
                    request_row=request_row,
                )
                if request_row is not None and execution is not None
                else None
            )
            return ResultInputQuerySnapshot(
                worker_item=worker,
                request=request,
                facts=facts,
                projection_facts=projection_facts,
                selection=selection,
                delivery=delivery,
                is_historical=is_historical,
            )

    def load_input_selection(
        self, workspace_id: UUID, selection_id: UUID
    ) -> ResultInputSelectionQuerySnapshot | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            selection = _authorized_selection(
                session, workspace_id=workspace_id, selection_id=selection_id
            )
            if selection is None:
                return None
            plan_row = session.scalar(
                select(PlanSnapshotModel).where(
                    PlanSnapshotModel.workspace_id == workspace_id,
                    PlanSnapshotModel.root_task_id == selection.root_task_id,
                    PlanSnapshotModel.id == selection.plan_snapshot_id,
                    PlanSnapshotModel.generation == selection.plan_generation,
                )
            )
            if plan_row is None:
                return None
            plan = _plan(plan_row)
            assemblies = session.scalars(
                select(ContextAssemblyRequestModel)
                .where(
                    ContextAssemblyRequestModel.workspace_id == workspace_id,
                    ContextAssemblyRequestModel.purpose == "execution",
                    ContextAssemblyRequestModel.consumer_type == "worker",
                    ContextAssemblyRequestModel.worker_item_id
                    == selection.target_worker_item_id,
                    ContextAssemblyRequestModel.manifest_schema_version == 2,
                    ContextAssemblyRequestModel.input_selection_id == selection.id,
                    ContextAssemblyRequestModel.input_selection_hash
                    == selection.selection_hash,
                )
                .order_by(
                    ContextAssemblyRequestModel.updated_at.desc(),
                    ContextAssemblyRequestModel.id.desc(),
                )
                .limit(MAX_RESULT_INPUT_CONSUMERS + 1)
            ).all()
            consumers: list[ResultInputDeliveryFact] = []
            for assembly in assemblies:
                execution = session.scalar(
                    select(ExecutionModel).where(
                        ExecutionModel.workspace_id == workspace_id,
                        ExecutionModel.id == assembly.consumer_id,
                        ExecutionModel.worker_item_id
                        == selection.target_worker_item_id,
                    )
                )
                if execution is None:
                    continue
                request_row = session.scalar(
                    select(ExecutionRequestModel)
                    .where(
                        ExecutionRequestModel.workspace_id == workspace_id,
                        ExecutionRequestModel.root_task_id == selection.root_task_id,
                        ExecutionRequestModel.plan_snapshot_id
                        == selection.plan_snapshot_id,
                        ExecutionRequestModel.plan_generation
                        == selection.plan_generation,
                        ExecutionRequestModel.worker_item_id
                        == selection.target_worker_item_id,
                        ExecutionRequestModel.execution_lineage_id
                        == execution.lineage_id,
                    )
                    .order_by(
                        ExecutionRequestModel.updated_at.desc(),
                        ExecutionRequestModel.id.desc(),
                    )
                )
                if request_row is None:
                    continue
                delivery = _delivery_for_execution(
                    session,
                    workspace_id=workspace_id,
                    execution=execution,
                    request_row=request_row,
                )
                if delivery is None or delivery.assembly_request_id != assembly.id:
                    continue
                consumers.append(delivery)
            return ResultInputSelectionQuerySnapshot(
                selection=selection,
                plan=plan,
                consumers=tuple(consumers),
            )

    def _load_collaboration_facts(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        plan: TaskPlanSnapshot,
        request_row: ExecutionRequestModel | None,
        is_historical: bool,
    ) -> tuple[
        CollaborationFacts | None,
        CollaborationProjectionFacts | None,
        CollaborationRequestFact | None,
    ]:
        run = CollaborationRunRepository(session).get_for_plan(
            workspace_id,
            plan_snapshot_id=plan.id,
            plan_generation=plan.generation,
        )
        if run is None:
            return None, None, _request_fact(request_row, plan) if request_row else None
        facts = SqlAlchemyCollaborationFactStore(session=session).load_in_session(
            session,
            workspace_id,
            run,
            fact_cursor=run.last_evaluated_change_cursor,
            allow_stale_head=is_historical,
        )
        projection_facts = self._projection_store.load_in_session(
            session,
            workspace_id=workspace_id,
            root_task_id=plan.root_task_id,
            plan=self._normalizer.normalize(plan),
            include_superseded=is_historical,
        )
        request = next(
            (item for item in facts.requests if item.id == request_row.id),
            None,
        ) if request_row is not None else None
        return facts, projection_facts, request or _request_fact(request_row, plan)


def _worker_fact(row: WorkerItemModel) -> ResultInputWorkerItemFact:
    return ResultInputWorkerItemFact(
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        worker_item_id=row.id,
        worker_item_key=row.stable_key,
        title=row.title,
        responsibility=row.responsibility,
        status=row.status,
        version=row.version,
        updated_at=_as_utc(row.updated_at),
    )


def _current_plan_row(
    session: Session, *, workspace_id: UUID, root_task_id: UUID
) -> PlanSnapshotModel | None:
    return session.scalar(
        select(PlanSnapshotModel)
        .join(
            RootTaskPlanHeadModel,
            (RootTaskPlanHeadModel.workspace_id == PlanSnapshotModel.workspace_id)
            & (RootTaskPlanHeadModel.root_task_id == PlanSnapshotModel.root_task_id)
            & (RootTaskPlanHeadModel.active_snapshot_id == PlanSnapshotModel.id)
            & (RootTaskPlanHeadModel.active_generation == PlanSnapshotModel.generation),
        )
        .where(
            PlanSnapshotModel.workspace_id == workspace_id,
            PlanSnapshotModel.root_task_id == root_task_id,
        )
    )


def _plan_row_for_request(
    session: Session,
    *,
    workspace_id: UUID,
    request: ExecutionRequestModel,
) -> PlanSnapshotModel | None:
    return session.scalar(
        select(PlanSnapshotModel).where(
            PlanSnapshotModel.workspace_id == workspace_id,
            PlanSnapshotModel.id == request.plan_snapshot_id,
            PlanSnapshotModel.root_task_id == request.root_task_id,
            PlanSnapshotModel.generation == request.plan_generation,
        )
    )


def _current_request_for_worker(
    session: Session,
    *,
    workspace_id: UUID,
    worker_item_id: UUID,
    root_task_id: UUID,
    plan_row: PlanSnapshotModel,
) -> ExecutionRequestModel | None:
    return session.scalar(
        select(ExecutionRequestModel)
        .where(
            ExecutionRequestModel.workspace_id == workspace_id,
            ExecutionRequestModel.root_task_id == root_task_id,
            ExecutionRequestModel.plan_snapshot_id == plan_row.id,
            ExecutionRequestModel.plan_generation == plan_row.generation,
            ExecutionRequestModel.worker_item_id == worker_item_id,
            ExecutionRequestModel.handoff_transition_id.is_(None),
        )
        .order_by(
            ExecutionRequestModel.updated_at.desc(),
            ExecutionRequestModel.id.desc(),
        )
    )


def _plan(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if (
        snapshot.id != row.id
        or snapshot.workspace_id != row.workspace_id
        or snapshot.root_task_id != row.root_task_id
        or snapshot.generation != row.generation
    ):
        raise ValidationError("task plan snapshot payload does not match identity columns")
    return snapshot


def _request_fact(
    row: ExecutionRequestModel | None, plan: TaskPlanSnapshot
) -> CollaborationRequestFact | None:
    if row is None:
        return None
    subtask_key = next(
        (
            item.key
            for item in plan.subtasks
            if item.task_id == row.subtask_task_id
        ),
        row.worker_item_key,
    )
    return CollaborationRequestFact(
        id=row.id,
        subtask_key=subtask_key,
        worker_item_key=row.worker_item_key,
        subtask_task_id=row.subtask_task_id,
        worker_item_id=row.worker_item_id,
        version=row.version,
        status=row.status,
        collaboration_gate_mode=row.collaboration_gate_mode,
        gate_reason=row.gate_reason,
        execution_lineage_id=row.execution_lineage_id,
        first_execution_id=row.first_execution_id,
        expected_artifact_refs=tuple(str(item) for item in row.expected_artifact_refs),
        expected_evidence_refs=tuple(str(item) for item in row.expected_evidence_refs),
        max_attempts=row.max_attempts,
        source_execution_id=row.source_execution_id,
        source_session_id=row.source_session_id,
        handoff_transition_id=row.handoff_transition_id,
    )


def _selection_for_request(
    session: Session,
    *,
    workspace_id: UUID,
    plan: TaskPlanSnapshot,
    request_row: ExecutionRequestModel,
) -> CollaborationInputSelectionV1 | None:
    if request_row.input_selection_id is None:
        return None
    selection = InputSelectionRepository(session).get(
        workspace_id, request_row.input_selection_id
    )
    if selection is None:
        return None
    if (
        selection.workspace_id != workspace_id
        or selection.root_task_id != request_row.root_task_id
        or selection.plan_snapshot_id != plan.id
        or selection.plan_generation != plan.generation
        or selection.target_worker_item_id != request_row.worker_item_id
        or selection.target_execution_request_id != request_row.id
        or selection.selection_hash != request_row.input_selection_hash
    ):
        return None
    return selection


def _latest_execution_for_request(
    session: Session,
    *,
    workspace_id: UUID,
    request_row: ExecutionRequestModel,
) -> ExecutionModel | None:
    if request_row.execution_lineage_id is None:
        return None
    return session.scalar(
        select(ExecutionModel)
        .where(
            ExecutionModel.workspace_id == workspace_id,
            ExecutionModel.lineage_id == request_row.execution_lineage_id,
            ExecutionModel.worker_item_id == request_row.worker_item_id,
        )
        .order_by(ExecutionModel.attempt.desc(), ExecutionModel.id.desc())
    )


def _delivery_for_execution(
    session: Session,
    *,
    workspace_id: UUID,
    execution: ExecutionModel,
    request_row: ExecutionRequestModel,
) -> ResultInputDeliveryFact | None:
    assembly = session.scalar(
        select(ContextAssemblyRequestModel)
        .where(
            ContextAssemblyRequestModel.workspace_id == workspace_id,
            ContextAssemblyRequestModel.purpose == "execution",
            ContextAssemblyRequestModel.consumer_type == "worker",
            ContextAssemblyRequestModel.consumer_id == execution.id,
            ContextAssemblyRequestModel.worker_item_id == request_row.worker_item_id,
            ContextAssemblyRequestModel.manifest_schema_version == 2,
        )
        .order_by(
            ContextAssemblyRequestModel.updated_at.desc(),
            ContextAssemblyRequestModel.id.desc(),
        )
    )
    if assembly is None:
        return None
    package = session.scalar(
        select(ContextPackageModel).where(
            ContextPackageModel.workspace_id == workspace_id,
            ContextPackageModel.assembly_request_id == assembly.id,
            ContextPackageModel.purpose == "execution",
            ContextPackageModel.consumer_type == "worker",
            ContextPackageModel.consumer_id == execution.id,
            ContextPackageModel.manifest_schema_version == 2,
        )
    )
    acknowledgement = (
        session.scalar(
            select(ContextPackageAcknowledgementModel).where(
                ContextPackageAcknowledgementModel.workspace_id == workspace_id,
                ContextPackageAcknowledgementModel.package_id == package.id,
                ContextPackageAcknowledgementModel.consumer_type == "worker",
                ContextPackageAcknowledgementModel.consumer_id == execution.id,
            )
        )
        if package is not None
        else None
    )
    audit = (
        session.scalar(
            select(ContextPackageMaterializationAuditModel)
            .where(
                ContextPackageMaterializationAuditModel.workspace_id == workspace_id,
                ContextPackageMaterializationAuditModel.package_id == package.id,
                ContextPackageMaterializationAuditModel.consumer_type == "worker",
                ContextPackageMaterializationAuditModel.consumer_id == execution.id,
            )
            .order_by(
                ContextPackageMaterializationAuditModel.attempt.desc(),
                ContextPackageMaterializationAuditModel.occurred_at.desc(),
                ContextPackageMaterializationAuditModel.id.desc(),
            )
        )
        if package is not None
        else None
    )
    acknowledgement_status = "not_acknowledged"
    if acknowledgement is not None:
        acknowledgement_status = (
            "acknowledged"
            if package is not None
            and acknowledgement.acknowledged_package_hash == package.package_hash
            else "hash_mismatch"
        )
    return ResultInputDeliveryFact(
        consumer_type=assembly.consumer_type,
        consumer_id=assembly.consumer_id,
        execution_request_id=request_row.id,
        worker_item_id=assembly.worker_item_id,
        package_id=package.id if package is not None else None,
        context_version=(
            package.context_version
            if package is not None
            else assembly.context_version
        ),
        package_hash=package.package_hash if package is not None else None,
        package_status=package.status if package is not None else None,
        assembly_request_id=assembly.id,
        assembly_status=assembly.status,
        assembly_attempt=assembly.attempt,
        assembly_error_code=assembly.error_code,
        assembly_version=assembly.version,
        assembly_request_fingerprint=assembly.request_fingerprint,
        materialization_attempt=audit.attempt if audit is not None else None,
        materialization_outcome=audit.outcome if audit is not None else None,
        materialization_failure_code=audit.failure_code if audit is not None else None,
        materialization_occurred_at=(
            _as_utc(audit.occurred_at) if audit is not None else None
        ),
        acknowledgement_status=acknowledgement_status,
        acknowledged_package_hash=(
            acknowledgement.acknowledged_package_hash
            if acknowledgement is not None
            else None
        ),
        acknowledged_at=(
            _as_utc(acknowledgement.acknowledged_at)
            if acknowledgement is not None
            else None
        ),
    )


def _authorized_selection(
    session: Session, *, workspace_id: UUID, selection_id: UUID
) -> CollaborationInputSelectionV1 | None:
    selection_row = session.scalar(
        select(CollaborationInputSelectionModel).where(
            CollaborationInputSelectionModel.workspace_id == workspace_id,
            CollaborationInputSelectionModel.id == selection_id,
        )
    )
    if selection_row is None:
        return None
    selection = InputSelectionRepository(session).get(workspace_id, selection_id)
    if selection is None:
        return None

    checks = (
        session.scalar(
            select(PlanSnapshotModel.id).where(
                PlanSnapshotModel.workspace_id == workspace_id,
                PlanSnapshotModel.id == selection.plan_snapshot_id,
                PlanSnapshotModel.root_task_id == selection.root_task_id,
                PlanSnapshotModel.generation == selection.plan_generation,
            )
        ),
        session.scalar(
            select(WorkerItemModel.id).where(
                WorkerItemModel.workspace_id == workspace_id,
                WorkerItemModel.id == selection.target_worker_item_id,
                WorkerItemModel.root_task_id == selection.root_task_id,
            )
        ),
        session.scalar(
            select(ExecutionRequestModel.id).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == selection.target_execution_request_id,
                ExecutionRequestModel.root_task_id == selection.root_task_id,
                ExecutionRequestModel.plan_snapshot_id == selection.plan_snapshot_id,
                ExecutionRequestModel.plan_generation == selection.plan_generation,
                ExecutionRequestModel.worker_item_id == selection.target_worker_item_id,
                ExecutionRequestModel.input_selection_id == selection.id,
                ExecutionRequestModel.input_selection_hash == selection.selection_hash,
            )
        ),
        session.scalar(
            select(CollaborationRunModel.id).where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.id == selection.run_id,
                CollaborationRunModel.root_task_id == selection.root_task_id,
                CollaborationRunModel.plan_snapshot_id == selection.plan_snapshot_id,
                CollaborationRunModel.plan_generation == selection.plan_generation,
                CollaborationRunModel.plan_content_hash == selection.plan_content_hash,
            )
        ),
        session.scalar(
            select(RuntimeProjectionSnapshotModel.id).where(
                RuntimeProjectionSnapshotModel.workspace_id == workspace_id,
                RuntimeProjectionSnapshotModel.id == selection.projection_snapshot_id,
                RuntimeProjectionSnapshotModel.run_id == selection.run_id,
                RuntimeProjectionSnapshotModel.plan_snapshot_id == selection.plan_snapshot_id,
                RuntimeProjectionSnapshotModel.plan_generation == selection.plan_generation,
                RuntimeProjectionSnapshotModel.content_hash == selection.projection_hash,
            )
        ),
        session.scalar(
            select(RuntimeDecisionModel.id).where(
                RuntimeDecisionModel.workspace_id == workspace_id,
                RuntimeDecisionModel.id == selection.decision_id,
                RuntimeDecisionModel.run_id == selection.run_id,
                RuntimeDecisionModel.projection_snapshot_id
                == selection.projection_snapshot_id,
                RuntimeDecisionModel.projection_hash == selection.projection_hash,
            )
        ),
        session.scalar(
            select(RuntimeIntentModel.id).where(
                RuntimeIntentModel.workspace_id == workspace_id,
                RuntimeIntentModel.id == selection.intent_id,
                RuntimeIntentModel.run_id == selection.run_id,
                RuntimeIntentModel.decision_id == selection.decision_id,
                RuntimeIntentModel.target_execution_request_id
                == selection.target_execution_request_id,
                RuntimeIntentModel.target_request_version
                == selection.target_request_version,
            )
        ),
    )
    return selection if all(check is not None for check in checks) else None


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
