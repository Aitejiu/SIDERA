from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.lease import AssignmentLease, LeaseStatus
from sidera_control_plane.persistence.models import AssignmentLeaseModel


class LeaseStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self, lease_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> AssignmentLease | None:
        statement = select(AssignmentLeaseModel).where(
            AssignmentLeaseModel.id == lease_id,
            AssignmentLeaseModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_lease(row) if row is not None else None

    def get_active_for_execution(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> AssignmentLease | None:
        statement = select(AssignmentLeaseModel).where(
            AssignmentLeaseModel.execution_id == execution_id,
            AssignmentLeaseModel.workspace_id == workspace_id,
            AssignmentLeaseModel.status == LeaseStatus.ACTIVE.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_lease(row) if row is not None else None

    def add(self, lease: AssignmentLease) -> None:
        self._session.add(
            AssignmentLeaseModel(
                id=lease.id,
                workspace_id=lease.workspace_id,
                execution_id=lease.execution_id,
                worker_id=lease.worker_id,
                status=lease.status.value,
                issued_at=lease.issued_at,
                expires_at=lease.expires_at,
                renewed_at=lease.renewed_at,
                ended_at=lease.ended_at,
                fencing_token=lease.fencing_token,
                assignment_reason=lease.assignment_reason,
                version=lease.version,
            )
        )

    def save(self, lease: AssignmentLease, previous_version: int) -> None:
        statement = (
            update(AssignmentLeaseModel)
            .where(
                AssignmentLeaseModel.id == lease.id,
                AssignmentLeaseModel.workspace_id == lease.workspace_id,
                AssignmentLeaseModel.version == previous_version,
            )
            .values(
                execution_id=lease.execution_id,
                worker_id=lease.worker_id,
                status=lease.status.value,
                issued_at=lease.issued_at,
                expires_at=lease.expires_at,
                renewed_at=lease.renewed_at,
                ended_at=lease.ended_at,
                fencing_token=lease.fencing_token,
                assignment_reason=lease.assignment_reason,
                version=lease.version,
                updated_at=datetime.now(UTC),
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._current_version(lease.id, lease.workspace_id)
            raise VersionConflict(previous_version, actual)

    def claim_expired(
        self, *, now: datetime, limit: int
    ) -> tuple[AssignmentLease, ...]:
        statement = (
            select(AssignmentLeaseModel)
            .where(
                AssignmentLeaseModel.status == LeaseStatus.ACTIVE.value,
                AssignmentLeaseModel.expires_at <= now,
            )
            .order_by(
                AssignmentLeaseModel.expires_at.asc(), AssignmentLeaseModel.id.asc()
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = self._session.scalars(statement).all()
        return tuple(_to_domain_lease(row) for row in rows)

    def _current_version(self, lease_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(AssignmentLeaseModel.version).where(
                AssignmentLeaseModel.id == lease_id,
                AssignmentLeaseModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


def _to_domain_lease(row: AssignmentLeaseModel) -> AssignmentLease:
    return AssignmentLease(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        status=LeaseStatus(row.status),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        renewed_at=row.renewed_at,
        ended_at=row.ended_at,
        fencing_token=row.fencing_token,
        assignment_reason=dict(row.assignment_reason),
        version=row.version,
    )
