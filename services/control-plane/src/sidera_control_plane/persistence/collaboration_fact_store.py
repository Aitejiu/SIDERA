from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, aliased

from sidera_control_plane.application.collaboration_projection import (
    CollaborationArtifactFact,
    CollaborationEvidenceFact,
    CollaborationExecutionBindingFact,
    CollaborationExecutionFact,
    CollaborationFacts,
    CollaborationHandoffTransitionFact,
    CollaborationPlanHeadFact,
    CollaborationRequestFact,
    CollaborationWorkerPlacementFact,
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
    ExecutionBindingModel,
    ExecutionModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    HandoffTransitionModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    RuntimeDecisionModel,
    RuntimeReleaseModel,
    WorkerItemPlacementModel,
)

_MAX_RESULT_SOURCE_FACTS = 4096


class SqlAlchemyCollaborationFactStore:
    """Load the bounded, root-scoped facts exposed to a collaboration runtime."""

    def __init__(
        self,
        database: Database | None = None,
        *,
        session: Session | None = None,
    ) -> None:
        if database is None and session is None:
            raise ValueError("database or session is required")
        if database is not None and session is not None:
            raise ValueError("database and session are mutually exclusive")
        self._database = database
        self._session = session

    def load(
        self,
        workspace_id: UUID,
        run: CollaborationRun,
        *,
        fact_cursor: int,
        allow_stale_head: bool = False,
    ) -> CollaborationFacts:
        self._validate_run_scope(workspace_id, run, fact_cursor)
        if self._session is not None:
            return self._load_from_session(
                self._session,
                workspace_id,
                run,
                fact_cursor=fact_cursor,
                allow_stale_head=allow_stale_head,
            )
        if self._database is None:
            raise RuntimeError("fact store has no database or session")
        with self._database.session() as session:
            return self._load_from_session(
                session,
                workspace_id,
                run,
                fact_cursor=fact_cursor,
                allow_stale_head=allow_stale_head,
            )

    def load_in_session(
        self,
        session: Session,
        workspace_id: UUID,
        run: CollaborationRun,
        *,
        fact_cursor: int,
        allow_stale_head: bool = False,
    ) -> CollaborationFacts:
        """Load facts without opening a second transaction."""
        self._validate_run_scope(workspace_id, run, fact_cursor)
        return self._load_from_session(
            session,
            workspace_id,
            run,
            fact_cursor=fact_cursor,
            allow_stale_head=allow_stale_head,
        )

    def _load_from_session(
        self,
        session: Session,
        workspace_id: UUID,
        run: CollaborationRun,
        *,
        fact_cursor: int,
        allow_stale_head: bool,
    ) -> CollaborationFacts:
        head = session.scalar(
            select(RootTaskPlanHeadModel).where(
                RootTaskPlanHeadModel.workspace_id == workspace_id,
                RootTaskPlanHeadModel.root_task_id == run.root_task_id,
            )
        )
        if head is None:
            raise ValidationError("collaboration plan head does not exist")
        if not allow_stale_head and (
            head.active_snapshot_id != run.plan_snapshot_id
            or head.active_generation != run.plan_generation
            or head.active_snapshot_hash != run.plan_content_hash
        ):
            raise ValidationError("collaboration run is pinned to a stale plan head")

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

        worker_placements = self._load_worker_placements(session, run)
        requests = self._load_requests(session, run, snapshot)
        result_source_requests = self._load_result_source_requests(
            session,
            run,
            snapshot,
            worker_placements=worker_placements,
            current_request_ids=tuple(item.id for item in requests),
        )
        request_ids = tuple(item.id for item in requests)
        dependencies = self._load_dependencies(session, run, request_ids)
        lineage_ids = tuple(
            item.execution_lineage_id
            for item in requests
            if item.execution_lineage_id is not None
        )
        executions = self._load_executions(
            session,
            workspace_id,
            worker_item_ids=tuple(
                item.worker_item_id for item in worker_placements
            ),
            subtask_task_ids=tuple(
                item.subtask_task_id for item in worker_placements
            ),
            legacy_lineage_ids=lineage_ids,
        )
        execution_ids = tuple(item.id for item in executions)
        artifacts = self._load_artifacts(session, workspace_id, execution_ids)
        evidence = self._load_evidence(session, workspace_id, execution_ids)
        execution_bindings = self._load_execution_bindings(
            session, workspace_id, execution_ids
        )
        handoff_transitions = self._load_handoff_transitions(
            session,
            workspace_id,
            tuple(item.worker_item_id for item in worker_placements),
            execution_ids,
        )
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
                run_status=run.status.value,
            ),
            plan_snapshot=snapshot,
            requests=requests,
            dependencies=dependencies,
            executions=executions,
            artifacts=artifacts,
            evidence=evidence,
            releases=releases,
            previous_strategy_state=previous_strategy_state,
            worker_placements=worker_placements,
            execution_bindings=execution_bindings,
            handoff_transitions=handoff_transitions,
            result_source_requests=result_source_requests,
        )

    @staticmethod
    def _load_worker_placements(
        session: Session, run: CollaborationRun
    ) -> tuple[CollaborationWorkerPlacementFact, ...]:
        rows = session.execute(
            select(
                WorkerItemPlacementModel.worker_item_key,
                WorkerItemPlacementModel.worker_item_id,
                WorkerItemPlacementModel.subtask_key,
                WorkerItemPlacementModel.subtask_task_id,
            ).where(
                WorkerItemPlacementModel.workspace_id == run.workspace_id,
                WorkerItemPlacementModel.root_task_id == run.root_task_id,
                WorkerItemPlacementModel.plan_snapshot_id == run.plan_snapshot_id,
                WorkerItemPlacementModel.plan_generation == run.plan_generation,
                WorkerItemPlacementModel.subtask_task_id.is_not(None),
            )
            .order_by(WorkerItemPlacementModel.worker_item_key)
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "worker placements")
        return tuple(
            CollaborationWorkerPlacementFact(
                worker_item_key=row.worker_item_key,
                worker_item_id=row.worker_item_id,
                subtask_key=row.subtask_key,
                subtask_task_id=row.subtask_task_id,
                subtask_path=(row.subtask_key,),
            )
            for row in rows
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
                ExecutionRequestModel.purpose,
                ExecutionRequestModel.subtask_task_id,
                ExecutionRequestModel.worker_item_id,
                ExecutionRequestModel.version,
                ExecutionRequestModel.status,
                ExecutionRequestModel.collaboration_gate_mode,
                ExecutionRequestModel.gate_reason,
                ExecutionRequestModel.execution_lineage_id,
                ExecutionRequestModel.first_execution_id,
                ExecutionRequestModel.max_attempts,
                ExecutionRequestModel.source_execution_id,
                ExecutionRequestModel.source_session_id,
                ExecutionRequestModel.handoff_transition_id,
                ExecutionRequestModel.result_derivation_kind,
                ExecutionRequestModel.source_candidate_id,
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
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "requests")
        return tuple(
            SqlAlchemyCollaborationFactStore._request_fact(
                row, subtask_keys=subtask_keys
            )
            for row in rows
        )

    @classmethod
    def _load_result_source_requests(
        cls,
        session: Session,
        run: CollaborationRun,
        snapshot: TaskPlanSnapshot,
        *,
        worker_placements: Sequence[CollaborationWorkerPlacementFact],
        current_request_ids: Sequence[UUID],
    ) -> tuple[CollaborationRequestFact, ...]:
        """Load only historical requests usable as result-source lineage facts.

        These rows are deliberately kept out of ``facts.requests`` so an old
        request cannot become a new runtime release target after a Revision.
        The stable WorkerItem and subtask identities define the source scope.
        """
        if not worker_placements:
            return ()
        worker_item_ids = tuple({item.worker_item_id for item in worker_placements})
        subtask_task_ids = tuple({item.subtask_task_id for item in worker_placements})
        if not worker_item_ids or not subtask_task_ids:
            return ()
        subtask_keys = {
            item.task_id: item.key
            for item in snapshot.subtasks
            if item.task_id is not None
        }
        statement = select(
            ExecutionRequestModel.id,
                ExecutionRequestModel.worker_item_key,
                ExecutionRequestModel.purpose,
                ExecutionRequestModel.subtask_task_id,
            ExecutionRequestModel.worker_item_id,
            ExecutionRequestModel.version,
            ExecutionRequestModel.status,
            ExecutionRequestModel.collaboration_gate_mode,
            ExecutionRequestModel.gate_reason,
            ExecutionRequestModel.execution_lineage_id,
            ExecutionRequestModel.first_execution_id,
            ExecutionRequestModel.max_attempts,
            ExecutionRequestModel.source_execution_id,
            ExecutionRequestModel.source_session_id,
            ExecutionRequestModel.handoff_transition_id,
            ExecutionRequestModel.result_derivation_kind,
            ExecutionRequestModel.source_candidate_id,
            ExecutionRequestModel.expected_artifact_refs,
            ExecutionRequestModel.expected_evidence_refs,
            ExecutionRequestModel.plan_generation,
            ExecutionRequestModel.created_at,
        ).where(
            ExecutionRequestModel.workspace_id == run.workspace_id,
            ExecutionRequestModel.root_task_id == run.root_task_id,
            ExecutionRequestModel.plan_generation < run.plan_generation,
            ExecutionRequestModel.worker_item_id.in_(worker_item_ids),
            ExecutionRequestModel.subtask_task_id.in_(subtask_task_ids),
            ExecutionRequestModel.handoff_transition_id.is_(None),
            or_(
                ExecutionRequestModel.result_derivation_kind.is_(None),
                ExecutionRequestModel.result_derivation_kind != "verification",
            ),
            ExecutionRequestModel.status.not_in({"cancelled", "obsolete"}),
        )
        if current_request_ids:
            statement = statement.where(
                ~ExecutionRequestModel.id.in_(tuple(current_request_ids))
            )
        rows = session.execute(
            statement
            .order_by(
                ExecutionRequestModel.worker_item_id,
                ExecutionRequestModel.subtask_task_id,
                ExecutionRequestModel.plan_generation.desc(),
                ExecutionRequestModel.created_at.desc(),
                ExecutionRequestModel.id.desc(),
            )
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "result source requests")
        placement_keys = {
            (item.worker_item_id, item.subtask_task_id): item.worker_item_key
            for item in worker_placements
        }
        selected: list[CollaborationRequestFact] = []
        seen: set[tuple[UUID, UUID]] = set()
        for row in rows:
            identity = (row.worker_item_id, row.subtask_task_id)
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(
                cls._request_fact(
                    row,
                    subtask_keys=subtask_keys,
                    worker_item_key=placement_keys[identity],
                )
            )
        return tuple(selected)

    @staticmethod
    def _request_fact(
        row: Any,
        *,
        subtask_keys: Mapping[UUID, str],
        worker_item_key: str | None = None,
    ) -> CollaborationRequestFact:
        return CollaborationRequestFact(
            id=row.id,
            subtask_key=subtask_keys.get(row.subtask_task_id, str(row.subtask_task_id)),
            worker_item_key=worker_item_key or row.worker_item_key,
            purpose=row.purpose,
            subtask_task_id=row.subtask_task_id,
            worker_item_id=row.worker_item_id,
            version=row.version,
            status=row.status,
            collaboration_gate_mode=row.collaboration_gate_mode,
            gate_reason=row.gate_reason,
            execution_lineage_id=row.execution_lineage_id,
            first_execution_id=row.first_execution_id,
            max_attempts=row.max_attempts,
            source_execution_id=row.source_execution_id,
            source_session_id=row.source_session_id,
            handoff_transition_id=row.handoff_transition_id,
            result_derivation_kind=row.result_derivation_kind,
            source_candidate_id=row.source_candidate_id,
            expected_artifact_refs=_string_tuple(row.expected_artifact_refs),
            expected_evidence_refs=_string_tuple(row.expected_evidence_refs),
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
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "dependencies")
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
        *,
        worker_item_ids: Sequence[UUID],
        subtask_task_ids: Sequence[UUID],
        legacy_lineage_ids: Sequence[UUID],
    ) -> tuple[CollaborationExecutionFact, ...]:
        has_placement_scope = bool(worker_item_ids or subtask_task_ids)
        if has_placement_scope and (not worker_item_ids or not subtask_task_ids):
            raise ValidationError("collaboration worker placement scope is incomplete")
        if not has_placement_scope and not legacy_lineage_ids:
            return ()

        statement = select(
            ExecutionModel.id,
            ExecutionModel.lineage_id,
            ExecutionModel.task_id,
            ExecutionModel.worker_item_id,
            ExecutionModel.attempt,
            ExecutionModel.max_attempts,
            ExecutionModel.status,
            ExecutionModel.failure_code,
            ExecutionModel.started_at,
            ExecutionModel.finished_at,
            ExecutionModel.version,
            ExecutionModel.source_execution_id,
            ExecutionModel.source_session_id,
            ExecutionModel.handoff_transition_id,
        ).where(ExecutionModel.workspace_id == workspace_id)
        if has_placement_scope:
            statement = statement.where(
                ExecutionModel.worker_item_id.in_(worker_item_ids),
                ExecutionModel.task_id.in_(subtask_task_ids),
            )
        else:
            # Schema-v1 plan snapshots predate placement rows. Preserve their
            # established projection scope without treating it as v2 authority.
            statement = statement.where(
                ExecutionModel.lineage_id.in_(legacy_lineage_ids)
            )

        rows = session.execute(
            statement
            .order_by(
                ExecutionModel.lineage_id, ExecutionModel.attempt, ExecutionModel.id
            )
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "executions")
        return tuple(
            CollaborationExecutionFact(
                id=row.id,
                lineage_id=row.lineage_id,
                task_id=row.task_id,
                worker_item_id=row.worker_item_id,
                attempt=row.attempt,
                max_attempts=row.max_attempts,
                status=row.status,
                failure_code=row.failure_code,
                started_at=row.started_at,
                finished_at=row.finished_at,
                version=row.version,
                source_execution_id=row.source_execution_id,
                source_session_id=row.source_session_id,
                handoff_transition_id=row.handoff_transition_id,
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
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "artifacts")
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
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "evidence")
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
    def _load_execution_bindings(
        session: Session,
        workspace_id: UUID,
        execution_ids: Sequence[UUID],
    ) -> tuple[CollaborationExecutionBindingFact, ...]:
        if not execution_ids:
            return ()
        rows = session.execute(
            select(
                ExecutionBindingModel.id,
                ExecutionBindingModel.execution_id,
                ExecutionBindingModel.worker_id,
                ExecutionBindingModel.version,
            )
            .where(
                ExecutionBindingModel.workspace_id == workspace_id,
                ExecutionBindingModel.execution_id.in_(execution_ids),
            )
            .order_by(ExecutionBindingModel.execution_id, ExecutionBindingModel.id)
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "execution bindings")
        return tuple(
            CollaborationExecutionBindingFact(
                id=row.id,
                execution_id=row.execution_id,
                worker_id=row.worker_id,
                version=row.version,
            )
            for row in rows
        )

    @staticmethod
    def _load_handoff_transitions(
        session: Session,
        workspace_id: UUID,
        worker_item_ids: Sequence[UUID],
        execution_ids: Sequence[UUID],
    ) -> tuple[CollaborationHandoffTransitionFact, ...]:
        if not worker_item_ids or not execution_ids:
            return ()
        rows = session.execute(
            select(
                HandoffTransitionModel.id,
                HandoffTransitionModel.worker_item_id,
                HandoffTransitionModel.source_execution_id,
                HandoffTransitionModel.target_execution_id,
                HandoffTransitionModel.status,
                HandoffTransitionModel.phase,
                HandoffTransitionModel.writer_state,
            )
            .where(
                HandoffTransitionModel.workspace_id == workspace_id,
                HandoffTransitionModel.worker_item_id.in_(worker_item_ids),
                or_(
                    HandoffTransitionModel.source_execution_id.in_(execution_ids),
                    HandoffTransitionModel.target_execution_id.in_(execution_ids),
                ),
            )
            .order_by(HandoffTransitionModel.id)
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "handoff transitions")
        return tuple(
            CollaborationHandoffTransitionFact(
                id=row.id,
                worker_item_id=row.worker_item_id,
                source_execution_id=row.source_execution_id,
                target_execution_id=row.target_execution_id,
                status=row.status,
                phase=row.phase,
                writer_state=row.writer_state,
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
            .limit(_MAX_RESULT_SOURCE_FACTS + 1)
        ).all()
        _ensure_bounded(rows, "releases")
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


def _ensure_bounded(rows: Sequence[object], name: str) -> None:
    if len(rows) > _MAX_RESULT_SOURCE_FACTS:
        raise ValidationError(f"collaboration {name} exceed the bounded fact limit")
