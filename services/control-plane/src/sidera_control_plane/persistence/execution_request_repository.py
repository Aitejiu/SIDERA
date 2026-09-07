from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.collaboration import CollaborationGateMode
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequest,
    ExecutionRequestDependency,
    ExecutionRequestStatus,
)
from sidera_control_plane.persistence.models import (
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
)


class ExecutionRequestRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, request: ExecutionRequest) -> None:
        self._session.add(_request_model(request))

    def get(
        self,
        request_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        statement = select(ExecutionRequestModel).where(
            ExecutionRequestModel.id == request_id,
            ExecutionRequestModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_request(row) if row is not None else None

    def get_by_proposal_worker_item(
        self,
        proposal_revision_id: UUID,
        worker_item_key: str,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        statement = select(ExecutionRequestModel).where(
            ExecutionRequestModel.proposal_revision_id == proposal_revision_id,
            ExecutionRequestModel.worker_item_key == worker_item_key,
            ExecutionRequestModel.workspace_id == workspace_id,
            ExecutionRequestModel.handoff_transition_id.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_request(row) if row is not None else None

    def get_by_handoff_transition(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        statement = select(ExecutionRequestModel).where(
            ExecutionRequestModel.handoff_transition_id == transition_id,
            ExecutionRequestModel.workspace_id == workspace_id,
            ExecutionRequestModel.status == ExecutionRequestStatus.MATERIALIZED.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_request(row) if row is not None else None

    def get_by_execution_lineage(
        self,
        execution_lineage_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        statement = (
            select(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.execution_lineage_id == execution_lineage_id,
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.handoff_transition_id.is_(None),
            )
            .order_by(
                ExecutionRequestModel.created_at.asc(),
                ExecutionRequestModel.id.asc(),
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_request(row) if row is not None else None

    def get_effective_by_execution_lineage(
        self,
        execution_lineage_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        statement = (
            select(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.execution_lineage_id == execution_lineage_id,
                ExecutionRequestModel.workspace_id == workspace_id,
            )
            .order_by(
                ExecutionRequestModel.created_at.asc(),
                ExecutionRequestModel.id.asc(),
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_request(row) if row is not None else None

    def get_many(
        self,
        request_ids: Collection[UUID],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ExecutionRequest, ...]:
        ordered_ids = tuple(sorted(set(request_ids), key=str))
        if not ordered_ids:
            return ()
        statement = (
            select(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.id.in_(ordered_ids),
                ExecutionRequestModel.workspace_id == workspace_id,
            )
            .order_by(ExecutionRequestModel.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        rows = self._session.scalars(statement).all()
        return tuple(_to_request(row) for row in rows)

    def save(self, request: ExecutionRequest, previous_version: int) -> None:
        result = self._session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.id == request.id,
                ExecutionRequestModel.workspace_id == request.workspace_id,
                ExecutionRequestModel.version == previous_version,
            )
            .values(
                worker_item_key=request.worker_item_key,
                intent_key=request.intent_key,
                purpose=request.purpose,
                capability_requirements=list(request.capability_requirements),
                adapter_preferences=list(request.adapter_preferences),
                adapter_forbidden=list(request.adapter_forbidden),
                budget_class=request.budget_class,
                not_before=request.not_before,
                max_attempts=request.max_attempts,
                expected_outputs=list(request.expected_outputs),
                expected_artifact_refs=list(request.expected_artifact_refs),
                expected_evidence_refs=list(request.expected_evidence_refs),
                status=request.status.value,
                gate_reason=request.gate_reason,
                gate_details=_json_value(request.gate_details),
                gate_evaluated_at=request.gate_evaluated_at,
                status_changed_at=request.status_changed_at,
                execution_lineage_id=request.execution_lineage_id,
                first_execution_id=request.first_execution_id,
                source_execution_id=request.source_execution_id,
                source_session_id=request.source_session_id,
                handoff_transition_id=request.handoff_transition_id,
                input_selection_id=request.input_selection_id,
                input_selection_hash=request.input_selection_hash,
                result_derivation_kind=request.result_derivation_kind,
                source_candidate_id=request.source_candidate_id,
                rework_record_id=request.rework_record_id,
                version=request.version,
                updated_at=datetime.now(UTC),
            )
        )
        actual = self._session.scalar(
            select(ExecutionRequestModel.version).where(
                ExecutionRequestModel.id == request.id,
                ExecutionRequestModel.workspace_id == request.workspace_id,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(previous_version, actual or 0)

    def claim_ready(self, *, now: datetime, limit: int) -> tuple[ExecutionRequest, ...]:
        rows = self._claim_status(
            status=ExecutionRequestStatus.READY, now=now, limit=limit
        )
        return tuple(_to_request(row) for row in rows)

    def claim_blocked(
        self, *, now: datetime, limit: int
    ) -> tuple[ExecutionRequest, ...]:
        rows = self._claim_status(
            status=ExecutionRequestStatus.BLOCKED, now=now, limit=limit
        )
        return tuple(_to_request(row) for row in rows)

    def _claim_status(
        self, *, status: ExecutionRequestStatus, now: datetime, limit: int
    ) -> Sequence[ExecutionRequestModel]:
        return self._session.scalars(
            select(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.status == status.value,
                ExecutionRequestModel.not_before <= now,
            )
            .order_by(
                ExecutionRequestModel.not_before.asc(),
                ExecutionRequestModel.created_at.asc(),
                ExecutionRequestModel.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()

    def add_dependency(self, dependency: ExecutionRequestDependency) -> None:
        workspace_id = self._request_workspace(dependency.downstream_request_id)
        self._session.add(
            ExecutionRequestDependencyModel(
                workspace_id=workspace_id,
                downstream_request_id=dependency.downstream_request_id,
                upstream_request_id=dependency.upstream_request_id,
                enforcement=dependency.enforcement.value,
                required_artifact_refs=list(dependency.required_artifact_refs),
                required_evidence_refs=list(dependency.required_evidence_refs),
            )
        )

    def list_dependencies(
        self, downstream_request_id: UUID, workspace_id: UUID
    ) -> tuple[ExecutionRequestDependency, ...]:
        rows = self._session.scalars(
            select(ExecutionRequestDependencyModel)
            .where(
                ExecutionRequestDependencyModel.downstream_request_id
                == downstream_request_id,
                ExecutionRequestDependencyModel.workspace_id == workspace_id,
            )
            .order_by(ExecutionRequestDependencyModel.upstream_request_id.asc())
        ).all()
        return tuple(_to_dependency(row) for row in rows)

    def list_downstream_for_upstream(
        self, upstream_request_id: UUID, workspace_id: UUID
    ) -> tuple[ExecutionRequest, ...]:
        rows = self._session.scalars(
            select(ExecutionRequestModel)
            .join(
                ExecutionRequestDependencyModel,
                (
                    ExecutionRequestDependencyModel.downstream_request_id
                    == ExecutionRequestModel.id
                )
                & (
                    ExecutionRequestDependencyModel.workspace_id
                    == ExecutionRequestModel.workspace_id
                ),
            )
            .where(
                ExecutionRequestDependencyModel.upstream_request_id
                == upstream_request_id,
                ExecutionRequestDependencyModel.workspace_id == workspace_id,
            )
            .order_by(
                ExecutionRequestModel.created_at.asc(), ExecutionRequestModel.id.asc()
            )
        ).all()
        return tuple(_to_request(row) for row in rows)

    def _request_workspace(self, request_id: UUID) -> UUID:
        workspace_id = self._session.scalar(
            select(ExecutionRequestModel.workspace_id).where(
                ExecutionRequestModel.id == request_id
            )
        )
        if workspace_id is None:
            raise ValueError("execution request does not exist")
        return workspace_id


def _request_model(request: ExecutionRequest) -> ExecutionRequestModel:
    return ExecutionRequestModel(
        id=request.id,
        workspace_id=request.workspace_id,
        planning_request_id=request.planning_request_id,
        proposal_revision_id=request.proposal_revision_id,
        plan_snapshot_id=request.plan_snapshot_id,
        plan_generation=request.plan_generation,
        root_task_id=request.root_task_id,
        subtask_task_id=request.subtask_id,
        worker_item_id=request.worker_item_id,
        worker_item_key=request.worker_item_key,
        intent_key=request.intent_key,
        purpose=request.purpose,
        capability_requirements=list(request.capability_requirements),
        adapter_preferences=list(request.adapter_preferences),
        adapter_forbidden=list(request.adapter_forbidden),
        budget_class=request.budget_class,
        not_before=request.not_before,
        max_attempts=request.max_attempts,
        expected_outputs=list(request.expected_outputs),
        expected_artifact_refs=list(request.expected_artifact_refs),
        expected_evidence_refs=list(request.expected_evidence_refs),
        status=request.status.value,
        collaboration_gate_mode=request.collaboration_gate_mode.value,
        gate_reason=request.gate_reason,
        gate_details=_json_value(request.gate_details),
        gate_evaluated_at=request.gate_evaluated_at,
        status_changed_at=request.status_changed_at,
        execution_lineage_id=request.execution_lineage_id,
        first_execution_id=request.first_execution_id,
        source_execution_id=request.source_execution_id,
        source_session_id=request.source_session_id,
        handoff_transition_id=request.handoff_transition_id,
        input_selection_id=request.input_selection_id,
        input_selection_hash=request.input_selection_hash,
        result_derivation_kind=request.result_derivation_kind,
        source_candidate_id=request.source_candidate_id,
        rework_record_id=request.rework_record_id,
        version=request.version,
    )


def _to_request(row: ExecutionRequestModel) -> ExecutionRequest:
    return ExecutionRequest(
        id=row.id,
        workspace_id=row.workspace_id,
        planning_request_id=row.planning_request_id,
        proposal_revision_id=row.proposal_revision_id,
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        root_task_id=row.root_task_id,
        subtask_id=row.subtask_task_id,
        worker_item_id=row.worker_item_id,
        worker_item_key=row.worker_item_key,
        intent_key=row.intent_key,
        purpose=row.purpose,
        capability_requirements=frozenset(row.capability_requirements),
        adapter_preferences=tuple(row.adapter_preferences),
        adapter_forbidden=frozenset(row.adapter_forbidden),
        budget_class=row.budget_class,
        not_before=row.not_before,
        max_attempts=row.max_attempts,
        expected_artifact_refs=tuple(row.expected_artifact_refs),
        expected_evidence_refs=tuple(row.expected_evidence_refs),
        status=ExecutionRequestStatus(row.status),
        collaboration_gate_mode=CollaborationGateMode(row.collaboration_gate_mode),
        gate_reason=row.gate_reason,
        gate_details=MappingProxyType(dict(row.gate_details)),
        gate_evaluated_at=row.gate_evaluated_at,
        status_changed_at=row.status_changed_at,
        execution_lineage_id=row.execution_lineage_id,
        first_execution_id=row.first_execution_id,
        source_execution_id=row.source_execution_id,
        source_session_id=row.source_session_id,
        handoff_transition_id=row.handoff_transition_id,
        input_selection_id=row.input_selection_id,
        input_selection_hash=row.input_selection_hash,
        result_derivation_kind=row.result_derivation_kind,
        source_candidate_id=row.source_candidate_id,
        rework_record_id=row.rework_record_id,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expected_outputs=tuple(row.expected_outputs),
    )


def _to_dependency(
    row: ExecutionRequestDependencyModel,
) -> ExecutionRequestDependency:
    return ExecutionRequestDependency(
        downstream_request_id=row.downstream_request_id,
        upstream_request_id=row.upstream_request_id,
        enforcement=DependencyEnforcement(row.enforcement),
        required_artifact_refs=tuple(row.required_artifact_refs),
        required_evidence_refs=tuple(row.required_evidence_refs),
    )


def _json_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return value
