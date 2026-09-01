from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
    WorkspaceLifecycle,
)
from sidera_control_plane.persistence.models import ExecutionWorkspaceModel


class WorkspaceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        manifest_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionWorkspaceManifest | None:
        statement = select(ExecutionWorkspaceModel).where(
            ExecutionWorkspaceModel.id == manifest_id,
            ExecutionWorkspaceModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_manifest(row) if row is not None else None

    def get_for_execution(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionWorkspaceManifest | None:
        statement = select(ExecutionWorkspaceModel).where(
            ExecutionWorkspaceModel.execution_id == execution_id,
            ExecutionWorkspaceModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_manifest(row) if row is not None else None

    def add(self, manifest: ExecutionWorkspaceManifest) -> None:
        self._session.add(
            ExecutionWorkspaceModel(
                id=manifest.id,
                workspace_id=manifest.workspace_id,
                task_id=manifest.task_id,
                execution_id=manifest.execution_id,
                checkouts=[_checkout_payload(checkout) for checkout in manifest.checkouts],
                location_ref=manifest.location_ref,
                lifecycle=manifest.lifecycle.value,
                created_at=manifest.created_at,
                retention_seconds=manifest.retention_seconds,
                retain_until=manifest.retain_until,
                fencing_token=manifest.fencing_token,
                frozen_at=manifest.frozen_at,
                released_at=manifest.released_at,
                version=manifest.version,
            )
        )

    def save(
        self, manifest: ExecutionWorkspaceManifest, previous_version: int
    ) -> None:
        statement = (
            update(ExecutionWorkspaceModel)
            .where(
                ExecutionWorkspaceModel.id == manifest.id,
                ExecutionWorkspaceModel.workspace_id == manifest.workspace_id,
                ExecutionWorkspaceModel.version == previous_version,
            )
            .values(
                checkouts=[
                    _checkout_payload(checkout) for checkout in manifest.checkouts
                ],
                location_ref=manifest.location_ref,
                lifecycle=manifest.lifecycle.value,
                retention_seconds=manifest.retention_seconds,
                retain_until=manifest.retain_until,
                fencing_token=manifest.fencing_token,
                frozen_at=manifest.frozen_at,
                released_at=manifest.released_at,
                version=manifest.version,
                updated_at=datetime.now(UTC),
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._current_version(manifest.id, manifest.workspace_id)
            raise VersionConflict(previous_version, actual)

    def _current_version(self, manifest_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(ExecutionWorkspaceModel.version).where(
                ExecutionWorkspaceModel.id == manifest_id,
                ExecutionWorkspaceModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


def _checkout_payload(checkout: WorkspaceCheckout) -> dict[str, str]:
    return {
        "repository_ref": checkout.repository_ref,
        "base_revision": checkout.base_revision,
        "branch": checkout.branch,
        "current_head": checkout.current_head,
    }


def _to_domain_manifest(row: ExecutionWorkspaceModel) -> ExecutionWorkspaceManifest:
    return ExecutionWorkspaceManifest(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        checkouts=tuple(_to_domain_checkout(item) for item in row.checkouts),
        location_ref=row.location_ref,
        lifecycle=WorkspaceLifecycle(row.lifecycle),
        retention_seconds=row.retention_seconds,
        retain_until=row.retain_until,
        fencing_token=row.fencing_token,
        created_at=row.created_at,
        frozen_at=row.frozen_at,
        released_at=row.released_at,
        version=row.version,
    )


def _to_domain_checkout(payload: dict[str, object]) -> WorkspaceCheckout:
    return WorkspaceCheckout(
        repository_ref=_required_checkout_text(payload, "repository_ref"),
        base_revision=_required_checkout_text(payload, "base_revision"),
        branch=_required_checkout_text(payload, "branch"),
        current_head=_required_checkout_text(payload, "current_head"),
    )


def _required_checkout_text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise TypeError(f"workspace checkout field {name!r} is missing")
    return value
