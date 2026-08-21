from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import ProjectionChangePublisherPort
from sidera_control_plane.domain.artifacts import (
    Artifact,
    ArtifactType,
    EvidenceClaim,
    EvidenceClaimStatus,
)
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.gateway import (
    ExecutionEvent,
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
)
from sidera_control_plane.domain.inputs import ExecutionInput, ExecutionInputStatus
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    EvidenceClaimModel,
    ExecutionEventModel,
    ExecutionInputModel,
    GatewayOperationModel,
)


class GatewayOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_operation_id(
        self,
        operation_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> GatewayOperation | None:
        statement = select(GatewayOperationModel).where(
            GatewayOperationModel.operation_id == operation_id,
            GatewayOperationModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_operation(row) if row is not None else None

    def add(self, operation: GatewayOperation) -> None:
        self._session.add(
            GatewayOperationModel(
                id=operation.id,
                workspace_id=operation.workspace_id,
                operation_id=operation.operation_id,
                task_id=operation.task_id,
                execution_id=operation.execution_id,
                session_id=operation.session_id,
                operation_type=operation.operation_type.value,
                module_instance_id=operation.module_instance_id,
                config_version=operation.config_version,
                lease_id=operation.lease_id,
                fencing_token=operation.fencing_token,
                status=operation.status.value,
                attempt_count=operation.attempt_count,
                next_attempt_at=operation.next_attempt_at,
                result_ref=operation.result_ref,
                failure_code=operation.failure_code,
                version=operation.version,
            )
        )

    def save(self, operation: GatewayOperation, previous_version: int) -> None:
        statement = (
            update(GatewayOperationModel)
            .where(
                GatewayOperationModel.id == operation.id,
                GatewayOperationModel.workspace_id == operation.workspace_id,
                GatewayOperationModel.version == previous_version,
            )
            .values(
                session_id=operation.session_id,
                status=operation.status.value,
                attempt_count=operation.attempt_count,
                next_attempt_at=operation.next_attempt_at,
                result_ref=operation.result_ref,
                failure_code=operation.failure_code,
                version=operation.version,
                updated_at=datetime.now(UTC),
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._current_version(operation.id, operation.workspace_id)
            raise VersionConflict(previous_version, actual)

    def _current_version(self, operation_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(GatewayOperationModel.version).where(
                GatewayOperationModel.id == operation_id,
                GatewayOperationModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


class ExecutionEventRepository:
    def __init__(
        self,
        session: Session,
        projection_changes: ProjectionChangePublisherPort | None = None,
    ) -> None:
        self._session = session
        self._projection_changes = projection_changes

    def add(self, event: ExecutionEvent) -> None:
        self._session.add(
            ExecutionEventModel(
                id=event.id,
                workspace_id=event.workspace_id,
                execution_id=event.execution_id,
                session_id=event.session_id,
                sequence=event.sequence,
                event_type=event.event_type,
                payload=dict(event.payload),
                occurred_at=event.occurred_at,
                raw_ref=event.raw_ref,
            )
        )
        if self._projection_changes is not None:
            self._projection_changes.changed(
                workspace_id=event.workspace_id,
                projection_type="worker_trace",
                resource_id=event.execution_id,
                change_type=event.event_type,
                resource_version=event.sequence,
                correlation_id=event.id,
            )

    def get(
        self, session_id: UUID, sequence: int, workspace_id: UUID
    ) -> ExecutionEvent | None:
        row = self._session.scalar(
            select(ExecutionEventModel).where(
                ExecutionEventModel.session_id == session_id,
                ExecutionEventModel.sequence == sequence,
                ExecutionEventModel.workspace_id == workspace_id,
            )
        )
        return _to_domain_event(row) if row is not None else None


class ArtifactRepository:
    def __init__(
        self,
        session: Session,
        projection_changes: ProjectionChangePublisherPort | None = None,
    ) -> None:
        self._session = session
        self._projection_changes = projection_changes

    def add(self, artifact: Artifact) -> None:
        self._session.add(
            ArtifactModel(
                id=artifact.id,
                workspace_id=artifact.workspace_id,
                task_id=artifact.task_id,
                execution_id=artifact.execution_id,
                session_id=artifact.session_id,
                artifact_type=artifact.artifact_type.value,
                storage_ref=artifact.storage_ref,
                content_hash=artifact.content_hash,
                size_bytes=artifact.size_bytes,
                media_type=artifact.media_type,
                provenance=dict(artifact.provenance),
            )
        )
        if self._projection_changes is not None:
            self._projection_changes.changed(
                workspace_id=artifact.workspace_id,
                projection_type="worker_trace",
                resource_id=artifact.execution_id,
                change_type="artifact",
                resource_version=None,
                correlation_id=artifact.id,
            )

    def list_for_execution(
        self, execution_id: UUID, workspace_id: UUID
    ) -> tuple[Artifact, ...]:
        rows = self._session.scalars(
            select(ArtifactModel)
            .where(
                ArtifactModel.execution_id == execution_id,
                ArtifactModel.workspace_id == workspace_id,
            )
            .order_by(ArtifactModel.created_at.asc(), ArtifactModel.id.asc())
        ).all()
        return tuple(_to_domain_artifact(row) for row in rows)


class EvidenceClaimRepository:
    def __init__(
        self,
        session: Session,
        projection_changes: ProjectionChangePublisherPort | None = None,
    ) -> None:
        self._session = session
        self._projection_changes = projection_changes

    def add(self, claim: EvidenceClaim) -> None:
        self._session.add(
            EvidenceClaimModel(
                id=claim.id,
                workspace_id=claim.workspace_id,
                task_id=claim.task_id,
                execution_id=claim.execution_id,
                acceptance_criterion_ref=claim.acceptance_criterion_ref,
                conclusion=claim.conclusion,
                environment_ref=claim.environment_ref,
                artifact_ids=[str(artifact_id) for artifact_id in claim.artifact_ids],
                producer=claim.producer,
                status=claim.status.value,
                created_at=claim.created_at,
            )
        )
        if self._projection_changes is not None:
            self._projection_changes.changed(
                workspace_id=claim.workspace_id,
                projection_type="task_first",
                resource_id=claim.task_id,
                change_type="evidence_claim",
                resource_version=None,
                correlation_id=claim.id,
            )

    def list_for_task(
        self, task_id: UUID, workspace_id: UUID
    ) -> tuple[EvidenceClaim, ...]:
        rows = self._session.scalars(
            select(EvidenceClaimModel)
            .where(
                EvidenceClaimModel.task_id == task_id,
                EvidenceClaimModel.workspace_id == workspace_id,
            )
            .order_by(EvidenceClaimModel.created_at.asc(), EvidenceClaimModel.id.asc())
        ).all()
        return tuple(_to_domain_claim(row) for row in rows)


class ExecutionInputRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        input_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionInput | None:
        statement = select(ExecutionInputModel).where(
            ExecutionInputModel.id == input_id,
            ExecutionInputModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_input(row) if row is not None else None

    def get_pending_for_session(
        self,
        session_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionInput | None:
        statement = select(ExecutionInputModel).where(
            ExecutionInputModel.session_id == session_id,
            ExecutionInputModel.workspace_id == workspace_id,
            ExecutionInputModel.status.in_(_PENDING_INPUT_STATUSES),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_input(row) if row is not None else None

    def find_by_client_idempotency_key(
        self,
        session_id: UUID,
        workspace_id: UUID,
        client_idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> ExecutionInput | None:
        statement = select(ExecutionInputModel).where(
            ExecutionInputModel.session_id == session_id,
            ExecutionInputModel.workspace_id == workspace_id,
            ExecutionInputModel.client_idempotency_key == client_idempotency_key,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_input(row) if row is not None else None

    def claim_queued(self, limit: int) -> tuple[ExecutionInput, ...]:
        rows = self._session.scalars(
            select(ExecutionInputModel)
            .where(ExecutionInputModel.status == ExecutionInputStatus.QUEUED.value)
            .order_by(ExecutionInputModel.created_at, ExecutionInputModel.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        ).all()
        claimed: list[ExecutionInput] = []
        for row in rows:
            item = _to_domain_input(row)
            previous_version = item.version
            item.begin_dispatch(
                operation_id=uuid5(NAMESPACE_URL, f"sidera.input:{item.id}")
            )
            self.save(item, previous_version)
            claimed.append(item)
        return tuple(claimed)

    def add(self, item: ExecutionInput) -> None:
        self._session.add(
            ExecutionInputModel(
                id=item.id,
                workspace_id=item.workspace_id,
                execution_id=item.execution_id,
                session_id=item.session_id,
                client_idempotency_key=item.client_idempotency_key,
                text=item.text,
                attachment_refs=list(item.attachment_refs),
                status=item.status.value,
                operation_id=item.operation_id,
                failure_code=item.failure_code,
                version=item.version,
                created_at=item.created_at,
                updated_at=item.updated_at,
                source_command_id=item.source_command_id,
            )
        )

    def save(self, item: ExecutionInput, previous_version: int) -> None:
        statement = (
            update(ExecutionInputModel)
            .where(
                ExecutionInputModel.id == item.id,
                ExecutionInputModel.workspace_id == item.workspace_id,
                ExecutionInputModel.version == previous_version,
            )
            .values(
                text=item.text,
                attachment_refs=list(item.attachment_refs),
                status=item.status.value,
                operation_id=item.operation_id,
                failure_code=item.failure_code,
                version=item.version,
                updated_at=datetime.now(UTC),
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._current_version(item.id, item.workspace_id)
            raise VersionConflict(previous_version, actual)

    def _current_version(self, item_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(ExecutionInputModel.version).where(
                ExecutionInputModel.id == item_id,
                ExecutionInputModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


_PENDING_INPUT_STATUSES = tuple(
    status.value
    for status in (
        ExecutionInputStatus.ACCEPTED,
        ExecutionInputStatus.QUEUED,
        ExecutionInputStatus.DISPATCHING,
        ExecutionInputStatus.BLOCKED,
    )
)


def _to_domain_operation(row: GatewayOperationModel) -> GatewayOperation:
    return GatewayOperation(
        id=row.id,
        operation_id=row.operation_id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        session_id=row.session_id,
        operation_type=GatewayOperationType(row.operation_type),
        module_instance_id=row.module_instance_id,
        config_version=row.config_version,
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
        status=GatewayOperationStatus(row.status),
        attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at,
        result_ref=row.result_ref,
        failure_code=row.failure_code,
        version=row.version,
    )


def _to_domain_event(row: ExecutionEventModel) -> ExecutionEvent:
    return ExecutionEvent(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        session_id=row.session_id,
        sequence=row.sequence,
        event_type=row.event_type,
        payload=dict(row.payload),
        occurred_at=row.occurred_at,
        raw_ref=row.raw_ref,
    )


def _to_domain_artifact(row: ArtifactModel) -> Artifact:
    return Artifact(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        session_id=row.session_id,
        artifact_type=ArtifactType(row.artifact_type),
        storage_ref=row.storage_ref,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        media_type=row.media_type,
        provenance=dict(row.provenance),
    )


def _to_domain_claim(row: EvidenceClaimModel) -> EvidenceClaim:
    return EvidenceClaim(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        acceptance_criterion_ref=row.acceptance_criterion_ref,
        conclusion=row.conclusion,
        environment_ref=row.environment_ref,
        artifact_ids=tuple(UUID(value) for value in row.artifact_ids),
        producer=row.producer,
        status=EvidenceClaimStatus(row.status),
        created_at=row.created_at,
    )


def _to_domain_input(row: ExecutionInputModel) -> ExecutionInput:
    return ExecutionInput(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        session_id=row.session_id,
        client_idempotency_key=row.client_idempotency_key,
        text=row.text,
        attachment_refs=tuple(row.attachment_refs),
        status=ExecutionInputStatus(row.status),
        operation_id=row.operation_id,
        failure_code=row.failure_code,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        source_command_id=row.source_command_id,
    )
