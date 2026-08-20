from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Row

from sidera_control_plane.application.resource_queries import (
    ArtifactView,
    EvidenceClaimView,
    ExecutionBindingView,
    ExecutionEventView,
    ExecutionInputView,
    ExecutionSessionView,
    ExecutionView,
    ExecutionWorkspaceView,
    LeaseView,
    QueryPage,
    WorkerView,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    WorkerModel,
)


class SqlAlchemyResourceQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_workers(self, workspace_id: UUID) -> tuple[WorkerView, ...]:
        with self._database.session() as session:
            rows = session.execute(
                _worker_columns()
                .where(WorkerModel.workspace_id == workspace_id)
                .order_by(WorkerModel.display_name.asc(), WorkerModel.id.asc())
            ).all()
        return tuple(_worker_view(row) for row in rows)

    def get_worker(self, workspace_id: UUID, worker_id: UUID) -> WorkerView | None:
        with self._database.session() as session:
            row = session.execute(
                _worker_columns().where(
                    WorkerModel.workspace_id == workspace_id,
                    WorkerModel.id == worker_id,
                )
            ).one_or_none()
        return _worker_view(row) if row is not None else None

    def list_executions(
        self, workspace_id: UUID, task_id: UUID | None, status: str | None = None
    ) -> tuple[ExecutionView, ...]:
        statement = _execution_columns().where(
            ExecutionModel.workspace_id == workspace_id
        )
        if task_id is not None:
            statement = statement.where(ExecutionModel.task_id == task_id)
        if status is not None:
            statement = statement.where(ExecutionModel.status == status)
        with self._database.session() as session:
            rows = session.execute(
                statement.order_by(ExecutionModel.created_at.asc(), ExecutionModel.id.asc())
            ).all()
        return tuple(_execution_view(row) for row in rows)

    def get_execution(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionView | None:
        with self._database.session() as session:
            row = session.execute(
                _execution_columns().where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.id == execution_id,
                )
            ).one_or_none()
        return _execution_view(row) if row is not None else None

    def list_leases(
        self, workspace_id: UUID, execution_id: UUID
    ) -> tuple[LeaseView, ...]:
        with self._database.session() as session:
            rows = session.execute(
                _lease_columns()
                .where(
                    AssignmentLeaseModel.workspace_id == workspace_id,
                    AssignmentLeaseModel.execution_id == execution_id,
                )
                .order_by(AssignmentLeaseModel.fencing_token.asc())
            ).all()
        return tuple(_lease_view(row) for row in rows)

    def get_execution_binding(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionBindingView | None:
        with self._database.session() as session:
            row = session.execute(
                _binding_columns().where(
                    ExecutionBindingModel.workspace_id == workspace_id,
                    ExecutionBindingModel.execution_id == execution_id,
                )
            ).one_or_none()
        return _binding_view(row) if row is not None else None

    def get_execution_workspace(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionWorkspaceView | None:
        with self._database.session() as session:
            row = session.execute(
                _workspace_columns().where(
                    ExecutionWorkspaceModel.workspace_id == workspace_id,
                    ExecutionWorkspaceModel.execution_id == execution_id,
                )
            ).one_or_none()
        return _workspace_view(row) if row is not None else None

    def list_execution_sessions(
        self,
        workspace_id: UUID,
        *,
        execution_id: UUID | None,
        session_id: UUID | None,
        status: str | None,
        cursor: int,
        limit: int,
    ) -> QueryPage[ExecutionSessionView]:
        statement = _session_columns().where(
            ExecutionSessionModel.workspace_id == workspace_id
        )
        if execution_id is not None:
            statement = statement.where(ExecutionSessionModel.execution_id == execution_id)
        if session_id is not None:
            statement = statement.where(ExecutionSessionModel.id == session_id)
        if status is not None:
            statement = statement.where(ExecutionSessionModel.status == status)
        with self._database.session() as session:
            rows = session.execute(
                statement.order_by(
                    ExecutionSessionModel.created_at.asc(),
                    ExecutionSessionModel.id.asc(),
                )
                .offset(cursor)
                .limit(limit + 1)
            ).all()
        return _page(tuple(_session_view(row) for row in rows), cursor, limit)

    def list_execution_events(
        self,
        workspace_id: UUID,
        *,
        execution_id: UUID | None,
        session_id: UUID | None,
        after_sequence: int,
        cursor: int,
        limit: int,
    ) -> QueryPage[ExecutionEventView]:
        statement = _event_columns().where(
            ExecutionEventModel.workspace_id == workspace_id,
            ExecutionEventModel.sequence > after_sequence,
        )
        if execution_id is not None:
            statement = statement.where(ExecutionEventModel.execution_id == execution_id)
        if session_id is not None:
            statement = statement.where(ExecutionEventModel.session_id == session_id)
        with self._database.session() as session:
            rows = session.execute(
                statement.order_by(
                    ExecutionEventModel.sequence.asc(), ExecutionEventModel.id.asc()
                )
                .offset(cursor)
                .limit(limit + 1)
            ).all()
        return _page(tuple(_event_view(row) for row in rows), cursor, limit)

    def list_artifacts(
        self,
        workspace_id: UUID,
        *,
        task_id: UUID | None,
        execution_id: UUID | None,
        session_id: UUID | None,
        cursor: int,
        limit: int,
    ) -> QueryPage[ArtifactView]:
        statement = _artifact_columns().where(
            ArtifactModel.workspace_id == workspace_id
        )
        if task_id is not None:
            statement = statement.where(ArtifactModel.task_id == task_id)
        if execution_id is not None:
            statement = statement.where(ArtifactModel.execution_id == execution_id)
        if session_id is not None:
            statement = statement.where(ArtifactModel.session_id == session_id)
        with self._database.session() as session:
            rows = session.execute(
                statement.order_by(ArtifactModel.created_at.asc(), ArtifactModel.id.asc())
                .offset(cursor)
                .limit(limit + 1)
            ).all()
        return _page(tuple(_artifact_view(row) for row in rows), cursor, limit)

    def list_evidence_claims(
        self,
        workspace_id: UUID,
        *,
        task_id: UUID | None,
        execution_id: UUID | None,
        cursor: int,
        limit: int,
    ) -> QueryPage[EvidenceClaimView]:
        statement = _evidence_columns().where(
            EvidenceClaimModel.workspace_id == workspace_id
        )
        if task_id is not None:
            statement = statement.where(EvidenceClaimModel.task_id == task_id)
        if execution_id is not None:
            statement = statement.where(EvidenceClaimModel.execution_id == execution_id)
        with self._database.session() as session:
            rows = session.execute(
                statement.order_by(
                    EvidenceClaimModel.created_at.asc(), EvidenceClaimModel.id.asc()
                )
                .offset(cursor)
                .limit(limit + 1)
            ).all()
        return _page(tuple(_evidence_view(row) for row in rows), cursor, limit)

    def list_execution_inputs(
        self,
        workspace_id: UUID,
        *,
        execution_id: UUID | None,
        session_id: UUID | None,
        status: str | None,
        cursor: int,
        limit: int,
    ) -> QueryPage[ExecutionInputView]:
        statement = _input_columns().where(
            ExecutionInputModel.workspace_id == workspace_id
        )
        if execution_id is not None:
            statement = statement.where(ExecutionInputModel.execution_id == execution_id)
        if session_id is not None:
            statement = statement.where(ExecutionInputModel.session_id == session_id)
        if status is not None:
            statement = statement.where(ExecutionInputModel.status == status)
        with self._database.session() as session:
            rows = session.execute(
                statement.order_by(
                    ExecutionInputModel.created_at.asc(), ExecutionInputModel.id.asc()
                )
                .offset(cursor)
                .limit(limit + 1)
            ).all()
        return _page(tuple(_input_view(row) for row in rows), cursor, limit)


def _worker_columns():
    return select(
        WorkerModel.id,
        WorkerModel.workspace_id,
        WorkerModel.adapter_type,
        WorkerModel.display_name,
        WorkerModel.status,
        WorkerModel.desired_state,
        WorkerModel.capabilities,
        WorkerModel.capacity,
        WorkerModel.current_load,
        WorkerModel.cost_per_hour_microunits,
        WorkerModel.startup_latency_ms,
        WorkerModel.last_heartbeat_at,
        WorkerModel.version,
        WorkerModel.created_at,
        WorkerModel.updated_at,
    )


def _worker_view(row: Row[Any]) -> WorkerView:
    return WorkerView(
        id=row.id,
        workspace_id=row.workspace_id,
        adapter_type=row.adapter_type,
        display_name=row.display_name,
        status=row.status,
        desired_state=row.desired_state,
        capabilities=tuple(sorted(row.capabilities)),
        capacity=row.capacity,
        current_load=row.current_load,
        cost_per_hour_microunits=row.cost_per_hour_microunits,
        startup_latency_ms=row.startup_latency_ms,
        last_heartbeat_at=_utc(row.last_heartbeat_at),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _execution_columns():
    return select(
        ExecutionModel.id,
        ExecutionModel.workspace_id,
        ExecutionModel.task_id,
        ExecutionModel.lineage_id,
        ExecutionModel.attempt,
        ExecutionModel.max_attempts,
        ExecutionModel.purpose,
        ExecutionModel.status,
        ExecutionModel.capability_requirements,
        ExecutionModel.pinned_worker_id,
        ExecutionModel.assigned_worker_id,
        ExecutionModel.active_lease_id,
        ExecutionModel.last_fencing_token,
        ExecutionModel.not_before,
        ExecutionModel.failure_code,
        ExecutionModel.started_at,
        ExecutionModel.finished_at,
        ExecutionModel.version,
        ExecutionModel.stop_requested,
        ExecutionModel.source_execution_id,
        ExecutionModel.source_session_id,
        ExecutionModel.created_at,
        ExecutionModel.updated_at,
    )


def _execution_view(row: Row[Any]) -> ExecutionView:
    return ExecutionView(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        lineage_id=row.lineage_id,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        purpose=row.purpose,
        status=row.status,
        capability_requirements=tuple(sorted(row.capability_requirements)),
        pinned_worker_id=row.pinned_worker_id,
        assigned_worker_id=row.assigned_worker_id,
        active_lease_id=row.active_lease_id,
        last_fencing_token=row.last_fencing_token,
        not_before=_utc(row.not_before),
        failure_code=row.failure_code,
        started_at=_utc_optional(row.started_at),
        finished_at=_utc_optional(row.finished_at),
        version=row.version,
        stop_requested=row.stop_requested,
        source_execution_id=row.source_execution_id,
        source_session_id=row.source_session_id,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _lease_columns():
    return select(
        AssignmentLeaseModel.id,
        AssignmentLeaseModel.execution_id,
        AssignmentLeaseModel.worker_id,
        AssignmentLeaseModel.status,
        AssignmentLeaseModel.issued_at,
        AssignmentLeaseModel.expires_at,
        AssignmentLeaseModel.renewed_at,
        AssignmentLeaseModel.ended_at,
        AssignmentLeaseModel.fencing_token,
        AssignmentLeaseModel.assignment_reason,
        AssignmentLeaseModel.version,
    )


def _lease_view(row: Row[Any]) -> LeaseView:
    return LeaseView(
        id=row.id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        status=row.status,
        issued_at=_utc(row.issued_at),
        expires_at=_utc(row.expires_at),
        renewed_at=_utc_optional(row.renewed_at),
        ended_at=_utc_optional(row.ended_at),
        fencing_token=row.fencing_token,
        assignment_reason=dict(row.assignment_reason),
        version=row.version,
    )


def _binding_columns():
    return select(
        ExecutionBindingModel.id,
        ExecutionBindingModel.workspace_id,
        ExecutionBindingModel.execution_id,
        ExecutionBindingModel.worker_id,
        ExecutionBindingModel.adapter_module_id,
        ExecutionBindingModel.workspace_module_id,
        ExecutionBindingModel.adapter_config_version,
        ExecutionBindingModel.workspace_config_version,
        ExecutionBindingModel.capability_snapshot,
        ExecutionBindingModel.lease_id,
        ExecutionBindingModel.fencing_token,
        ExecutionBindingModel.version,
        ExecutionBindingModel.created_at,
        ExecutionBindingModel.updated_at,
    )


def _binding_view(row: Row[Any]) -> ExecutionBindingView:
    return ExecutionBindingView(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        adapter_module_id=row.adapter_module_id,
        workspace_module_id=row.workspace_module_id,
        adapter_config_version=row.adapter_config_version,
        workspace_config_version=row.workspace_config_version,
        capability_snapshot=dict(row.capability_snapshot),
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
        version=row.version,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _session_columns():
    return select(
        ExecutionSessionModel.id,
        ExecutionSessionModel.workspace_id,
        ExecutionSessionModel.execution_id,
        ExecutionSessionModel.worker_id,
        ExecutionSessionModel.adapter_session_id,
        ExecutionSessionModel.status,
        ExecutionSessionModel.last_sequence,
        ExecutionSessionModel.last_activity_at,
        ExecutionSessionModel.input_mode,
        ExecutionSessionModel.adapter_module_id,
        ExecutionSessionModel.adapter_config_version,
        ExecutionSessionModel.workspace_module_id,
        ExecutionSessionModel.workspace_config_version,
        ExecutionSessionModel.capability_snapshot,
        ExecutionSessionModel.workspace_ref,
        ExecutionSessionModel.version,
        ExecutionSessionModel.created_at,
        ExecutionSessionModel.updated_at,
    )


def _session_view(row: Row[Any]) -> ExecutionSessionView:
    return ExecutionSessionView(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        adapter_session_id=row.adapter_session_id,
        status=row.status,
        last_sequence=row.last_sequence,
        last_activity_at=_utc(row.last_activity_at),
        input_mode=row.input_mode,
        adapter_module_id=row.adapter_module_id,
        adapter_config_version=row.adapter_config_version,
        workspace_module_id=row.workspace_module_id,
        workspace_config_version=row.workspace_config_version,
        capability_snapshot=(
            dict(row.capability_snapshot)
            if row.capability_snapshot is not None
            else None
        ),
        workspace_ref=row.workspace_ref,
        version=row.version,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _workspace_columns():
    return select(
        ExecutionWorkspaceModel.id,
        ExecutionWorkspaceModel.workspace_id,
        ExecutionWorkspaceModel.task_id,
        ExecutionWorkspaceModel.execution_id,
        ExecutionWorkspaceModel.checkouts,
        ExecutionWorkspaceModel.location_ref,
        ExecutionWorkspaceModel.lifecycle,
        ExecutionWorkspaceModel.retention_seconds,
        ExecutionWorkspaceModel.retain_until,
        ExecutionWorkspaceModel.fencing_token,
        ExecutionWorkspaceModel.frozen_at,
        ExecutionWorkspaceModel.released_at,
        ExecutionWorkspaceModel.version,
        ExecutionWorkspaceModel.created_at,
        ExecutionWorkspaceModel.updated_at,
    )


def _workspace_view(row: Row[Any]) -> ExecutionWorkspaceView:
    return ExecutionWorkspaceView(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        checkouts=tuple(dict(item) for item in row.checkouts),
        location_ref=row.location_ref,
        lifecycle=row.lifecycle,
        retention_seconds=row.retention_seconds,
        retain_until=_utc_optional(row.retain_until),
        fencing_token=row.fencing_token,
        frozen_at=_utc_optional(row.frozen_at),
        released_at=_utc_optional(row.released_at),
        version=row.version,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _event_columns():
    return select(
        ExecutionEventModel.id,
        ExecutionEventModel.workspace_id,
        ExecutionEventModel.execution_id,
        ExecutionEventModel.session_id,
        ExecutionEventModel.sequence,
        ExecutionEventModel.event_type,
        ExecutionEventModel.payload,
        ExecutionEventModel.occurred_at,
        ExecutionEventModel.raw_ref,
    )


def _event_view(row: Row[Any]) -> ExecutionEventView:
    return ExecutionEventView(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        session_id=row.session_id,
        sequence=row.sequence,
        event_type=row.event_type,
        payload=dict(row.payload),
        occurred_at=_utc(row.occurred_at),
        raw_ref=row.raw_ref,
    )


def _artifact_columns():
    return select(
        ArtifactModel.id,
        ArtifactModel.workspace_id,
        ArtifactModel.task_id,
        ArtifactModel.execution_id,
        ArtifactModel.session_id,
        ArtifactModel.artifact_type,
        ArtifactModel.storage_ref,
        ArtifactModel.content_hash,
        ArtifactModel.size_bytes,
        ArtifactModel.media_type,
        ArtifactModel.provenance,
        ArtifactModel.created_at,
    )


def _artifact_view(row: Row[Any]) -> ArtifactView:
    return ArtifactView(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        session_id=row.session_id,
        artifact_type=row.artifact_type,
        storage_ref=row.storage_ref,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        media_type=row.media_type,
        provenance=dict(row.provenance),
        created_at=_utc(row.created_at),
    )


def _evidence_columns():
    return select(
        EvidenceClaimModel.id,
        EvidenceClaimModel.workspace_id,
        EvidenceClaimModel.task_id,
        EvidenceClaimModel.execution_id,
        EvidenceClaimModel.acceptance_criterion_ref,
        EvidenceClaimModel.conclusion,
        EvidenceClaimModel.environment_ref,
        EvidenceClaimModel.artifact_ids,
        EvidenceClaimModel.producer,
        EvidenceClaimModel.status,
        EvidenceClaimModel.created_at,
    )


def _evidence_view(row: Row[Any]) -> EvidenceClaimView:
    return EvidenceClaimView(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        acceptance_criterion_ref=row.acceptance_criterion_ref,
        conclusion=row.conclusion,
        environment_ref=row.environment_ref,
        artifact_ids=tuple(UUID(value) for value in row.artifact_ids),
        producer=row.producer,
        status=row.status,
        created_at=_utc(row.created_at),
    )


def _input_columns():
    return select(
        ExecutionInputModel.id,
        ExecutionInputModel.workspace_id,
        ExecutionInputModel.execution_id,
        ExecutionInputModel.session_id,
        ExecutionInputModel.client_idempotency_key,
        ExecutionInputModel.status,
        ExecutionInputModel.operation_id,
        ExecutionInputModel.failure_code,
        ExecutionInputModel.version,
        ExecutionInputModel.text,
        ExecutionInputModel.attachment_refs,
        ExecutionInputModel.created_at,
        ExecutionInputModel.updated_at,
    )


def _input_view(row: Row[Any]) -> ExecutionInputView:
    return ExecutionInputView(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        session_id=row.session_id,
        client_idempotency_key=row.client_idempotency_key,
        status=row.status,
        operation_id=row.operation_id,
        failure_code=row.failure_code,
        version=row.version,
        text_length=len(row.text),
        attachment_count=len(row.attachment_refs),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _page[ViewT](
    items: tuple[ViewT, ...], cursor: int, limit: int
) -> QueryPage[ViewT]:
    has_more = len(items) > limit
    visible = items[:limit]
    return QueryPage(
        items=visible,
        next_cursor=str(cursor + len(visible)) if has_more else None,
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _utc_optional(value: datetime | None) -> datetime | None:
    return _utc(value) if value is not None else None
