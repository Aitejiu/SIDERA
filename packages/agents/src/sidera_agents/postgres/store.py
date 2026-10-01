from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from ..contracts import (
    CapabilityResourceDraft,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    ProfileStatus,
    ResourceRef,
    ResourceRevision,
    ResourceStatus,
)
from ..errors import AgentError
from .models import ProfileHead, ProfileRevisionRow, ResourceHead, ResourceRevisionRow


def _not_found(kind: str) -> AgentError:
    return AgentError("not_found", f"{kind.capitalize()} not found.")


def _revision_conflict(message: str) -> AgentError:
    return AgentError("revision_conflict", message)


def _profile_from_row(row: Mapping[str, Any]) -> ProfileRevision:
    return ProfileRevision(
        ref=ProfileRef(
            workspace_id=row["workspace_id"],
            profile_id=row["profile_id"],
            revision=row["revision"],
        ),
        draft=ProfileDraft.model_validate(row["draft"]),
        config_hash=row["config_hash"],
        created_at=row["created_at"],
    )


def _resource_from_row(row: Mapping[str, Any]) -> ResourceRevision:
    return ResourceRevision(
        ref=ResourceRef(
            workspace_id=row["workspace_id"],
            resource_id=row["resource_id"],
            revision=row["revision"],
        ),
        draft=CapabilityResourceDraft.model_validate(row["draft"]),
        content_hash=row["content_hash"],
        created_at=row["created_at"],
    )


class PostgresAgentStore:
    """Synchronous catalog persistence over a caller-owned SQLAlchemy engine."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_profile(
        self,
        workspace_id: UUID,
        profile_id: UUID,
        draft: ProfileDraft,
        config_hash: str,
        created_at: datetime,
    ) -> ProfileRevision:
        heads = ProfileHead.__table__
        revisions = ProfileRevisionRow.__table__
        ref = ProfileRef(workspace_id=workspace_id, profile_id=profile_id, revision=1)
        revision = ProfileRevision(
            ref=ref, draft=draft, config_hash=config_hash, created_at=created_at
        )
        try:
            with self._engine.begin() as connection:
                self._require_enabled_resources(
                    connection, workspace_id, draft.resource_refs
                )
                connection.execute(
                    heads.insert().values(
                        workspace_id=workspace_id,
                        profile_id=profile_id,
                        revision=1,
                        enabled=True,
                        status_version=1,
                    )
                )
                connection.execute(
                    revisions.insert().values(
                        workspace_id=workspace_id,
                        profile_id=profile_id,
                        revision=1,
                        draft=draft.model_dump(mode="json"),
                        config_hash=config_hash,
                        created_at=created_at,
                    )
                )
        except IntegrityError:
            raise AgentError("operation_conflict", "Profile already exists.") from None
        return ProfileRevision.model_validate(revision.model_dump(mode="python"))

    def get_profile(self, ref: ProfileRef) -> ProfileRevision:
        table = ProfileRevisionRow.__table__
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(table).where(
                        table.c.workspace_id == ref.workspace_id,
                        table.c.profile_id == ref.profile_id,
                        table.c.revision == ref.revision,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise _not_found("profile")
        return _profile_from_row(row)

    def get_profile_head(self, workspace_id: UUID, profile_id: UUID) -> ProfileRevision:
        heads = ProfileHead.__table__
        with self._engine.connect() as connection:
            head = (
                connection.execute(
                    select(heads.c.revision).where(
                        heads.c.workspace_id == workspace_id,
                        heads.c.profile_id == profile_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if head is None:
                raise _not_found("profile")
            row = self._profile_row(
                connection, workspace_id, profile_id, head["revision"]
            )
        if row is None:
            raise AgentError(
                "catalog_corrupt", "Profile head has no matching revision."
            )
        return _profile_from_row(row)

    def list_profiles(self, workspace_id: UUID) -> tuple[ProfileRevision, ...]:
        heads = ProfileHead.__table__
        with self._engine.connect() as connection:
            head_rows = tuple(
                connection.execute(
                    select(heads.c.profile_id, heads.c.revision)
                    .where(heads.c.workspace_id == workspace_id)
                    .order_by(heads.c.profile_id)
                ).mappings()
            )
            revisions = tuple(
                row
                for head in head_rows
                if (
                    row := self._profile_row(
                        connection, workspace_id, head["profile_id"], head["revision"]
                    )
                )
                is not None
            )
        if len(revisions) != len(head_rows):
            raise AgentError(
                "catalog_corrupt", "Profile head has no matching revision."
            )
        return tuple(_profile_from_row(row) for row in revisions)

    def append_profile_revision(
        self,
        expected_ref: ProfileRef,
        draft: ProfileDraft,
        config_hash: str,
        created_at: datetime,
    ) -> ProfileRevision:
        heads = ProfileHead.__table__
        revisions = ProfileRevisionRow.__table__
        next_revision = expected_ref.revision + 1
        ref = ProfileRef(
            workspace_id=expected_ref.workspace_id,
            profile_id=expected_ref.profile_id,
            revision=next_revision,
        )
        result = ProfileRevision(
            ref=ref, draft=draft, config_hash=config_hash, created_at=created_at
        )
        with self._engine.begin() as connection:
            self._require_enabled_resources(
                connection, expected_ref.workspace_id, draft.resource_refs
            )
            updated = connection.execute(
                update(heads)
                .where(
                    heads.c.workspace_id == expected_ref.workspace_id,
                    heads.c.profile_id == expected_ref.profile_id,
                    heads.c.revision == expected_ref.revision,
                )
                .values(revision=next_revision)
            )
            if updated.rowcount != 1:
                self._raise_missing_or_conflict(
                    connection,
                    heads,
                    expected_ref.workspace_id,
                    expected_ref.profile_id,
                    "Profile",
                )
            connection.execute(
                revisions.insert().values(
                    workspace_id=ref.workspace_id,
                    profile_id=ref.profile_id,
                    revision=ref.revision,
                    draft=draft.model_dump(mode="json"),
                    config_hash=config_hash,
                    created_at=created_at,
                )
            )
        return ProfileRevision.model_validate(result.model_dump(mode="python"))

    def set_profile_enabled(
        self, workspace_id: UUID, profile_id: UUID, enabled: bool, expected_version: int
    ) -> ProfileStatus:
        table = ProfileHead.__table__
        with self._engine.begin() as connection:
            result = connection.execute(
                update(table)
                .where(
                    table.c.workspace_id == workspace_id,
                    table.c.profile_id == profile_id,
                    table.c.status_version == expected_version,
                )
                .values(enabled=enabled, status_version=table.c.status_version + 1)
            )
            if result.rowcount != 1:
                self._raise_missing_or_conflict(
                    connection, table, workspace_id, profile_id, "Profile"
                )
        return ProfileStatus(
            profile_id=profile_id, enabled=enabled, version=expected_version + 1
        )

    def create_resource(
        self,
        workspace_id: UUID,
        resource_id: UUID,
        draft: CapabilityResourceDraft,
        content_hash: str,
        created_at: datetime,
    ) -> ResourceRevision:
        heads = ResourceHead.__table__
        revisions = ResourceRevisionRow.__table__
        ref = ResourceRef(
            workspace_id=workspace_id, resource_id=resource_id, revision=1
        )
        revision = ResourceRevision(
            ref=ref, draft=draft, content_hash=content_hash, created_at=created_at
        )
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    heads.insert().values(
                        workspace_id=workspace_id,
                        resource_id=resource_id,
                        revision=1,
                        enabled=True,
                        status_version=1,
                    )
                )
                connection.execute(
                    revisions.insert().values(
                        workspace_id=workspace_id,
                        resource_id=resource_id,
                        revision=1,
                        draft=draft.model_dump(mode="json"),
                        content_hash=content_hash,
                        created_at=created_at,
                    )
                )
        except IntegrityError:
            raise AgentError("operation_conflict", "Resource already exists.") from None
        return ResourceRevision.model_validate(revision.model_dump(mode="python"))

    def get_resource(self, ref: ResourceRef) -> ResourceRevision:
        table = ResourceRevisionRow.__table__
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(table).where(
                        table.c.workspace_id == ref.workspace_id,
                        table.c.resource_id == ref.resource_id,
                        table.c.revision == ref.revision,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise _not_found("resource")
        return _resource_from_row(row)

    def get_resource_head(
        self, workspace_id: UUID, resource_id: UUID
    ) -> ResourceRevision:
        heads = ResourceHead.__table__
        with self._engine.connect() as connection:
            head = (
                connection.execute(
                    select(heads.c.revision).where(
                        heads.c.workspace_id == workspace_id,
                        heads.c.resource_id == resource_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if head is None:
                raise _not_found("resource")
            row = self._resource_row(
                connection, workspace_id, resource_id, head["revision"]
            )
        if row is None:
            raise AgentError(
                "catalog_corrupt", "Resource head has no matching revision."
            )
        return _resource_from_row(row)

    def list_resources(self, workspace_id: UUID) -> tuple[ResourceRevision, ...]:
        heads = ResourceHead.__table__
        with self._engine.connect() as connection:
            head_rows = tuple(
                connection.execute(
                    select(heads.c.resource_id, heads.c.revision)
                    .where(heads.c.workspace_id == workspace_id)
                    .order_by(heads.c.resource_id)
                ).mappings()
            )
            revisions = tuple(
                row
                for head in head_rows
                if (
                    row := self._resource_row(
                        connection, workspace_id, head["resource_id"], head["revision"]
                    )
                )
                is not None
            )
        if len(revisions) != len(head_rows):
            raise AgentError(
                "catalog_corrupt", "Resource head has no matching revision."
            )
        return tuple(_resource_from_row(row) for row in revisions)

    def append_resource_revision(
        self,
        expected_ref: ResourceRef,
        draft: CapabilityResourceDraft,
        content_hash: str,
        created_at: datetime,
    ) -> ResourceRevision:
        heads = ResourceHead.__table__
        revisions = ResourceRevisionRow.__table__
        next_revision = expected_ref.revision + 1
        ref = ResourceRef(
            workspace_id=expected_ref.workspace_id,
            resource_id=expected_ref.resource_id,
            revision=next_revision,
        )
        result = ResourceRevision(
            ref=ref, draft=draft, content_hash=content_hash, created_at=created_at
        )
        with self._engine.begin() as connection:
            updated = connection.execute(
                update(heads)
                .where(
                    heads.c.workspace_id == expected_ref.workspace_id,
                    heads.c.resource_id == expected_ref.resource_id,
                    heads.c.revision == expected_ref.revision,
                )
                .values(revision=next_revision)
            )
            if updated.rowcount != 1:
                self._raise_missing_or_conflict(
                    connection,
                    heads,
                    expected_ref.workspace_id,
                    expected_ref.resource_id,
                    "Resource",
                )
            connection.execute(
                revisions.insert().values(
                    workspace_id=ref.workspace_id,
                    resource_id=ref.resource_id,
                    revision=ref.revision,
                    draft=draft.model_dump(mode="json"),
                    content_hash=content_hash,
                    created_at=created_at,
                )
            )
        return ResourceRevision.model_validate(result.model_dump(mode="python"))

    def get_resource_status(
        self, workspace_id: UUID, resource_id: UUID
    ) -> ResourceStatus:
        table = ResourceHead.__table__
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(table.c.enabled, table.c.status_version).where(
                        table.c.workspace_id == workspace_id,
                        table.c.resource_id == resource_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise _not_found("resource")
        return ResourceStatus(
            resource_id=resource_id,
            enabled=row["enabled"],
            version=row["status_version"],
        )

    def set_resource_enabled(
        self,
        workspace_id: UUID,
        resource_id: UUID,
        enabled: bool,
        expected_version: int,
    ) -> ResourceStatus:
        table = ResourceHead.__table__
        with self._engine.begin() as connection:
            result = connection.execute(
                update(table)
                .where(
                    table.c.workspace_id == workspace_id,
                    table.c.resource_id == resource_id,
                    table.c.status_version == expected_version,
                )
                .values(enabled=enabled, status_version=table.c.status_version + 1)
            )
            if result.rowcount != 1:
                self._raise_missing_or_conflict(
                    connection, table, workspace_id, resource_id, "Resource"
                )
        return ResourceStatus(
            resource_id=resource_id, enabled=enabled, version=expected_version + 1
        )

    def _require_enabled_resources(
        self, connection: Connection, workspace_id: UUID, refs: tuple[ResourceRef, ...]
    ) -> None:
        heads = ResourceHead.__table__
        revisions = ResourceRevisionRow.__table__
        for ref in refs:
            if ref.workspace_id != workspace_id:
                raise AgentError(
                    "configuration_invalid",
                    "Resource reference belongs to another workspace.",
                )
            head = (
                connection.execute(
                    select(heads.c.enabled)
                    .where(
                        heads.c.workspace_id == workspace_id,
                        heads.c.resource_id == ref.resource_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if head is None:
                raise _not_found("resource")
            exact_revision = connection.execute(
                select(revisions.c.revision).where(
                    revisions.c.workspace_id == workspace_id,
                    revisions.c.resource_id == ref.resource_id,
                    revisions.c.revision == ref.revision,
                )
            ).first()
            if exact_revision is None:
                raise _not_found("resource")
            if not head["enabled"]:
                raise AgentError(
                    "configuration_invalid", "Disabled resources cannot be newly bound."
                )

    @staticmethod
    def _raise_missing_or_conflict(
        connection: Connection,
        table,
        workspace_id: UUID,
        entity_id: UUID,
        kind: str,
    ) -> None:
        id_column = table.c.profile_id if kind == "Profile" else table.c.resource_id
        exists = connection.execute(
            select(id_column).where(
                table.c.workspace_id == workspace_id, id_column == entity_id
            )
        ).first()
        if exists is None:
            raise _not_found(kind)
        raise _revision_conflict(f"{kind} revision or status version is stale.")

    @staticmethod
    def _profile_row(
        connection: Connection, workspace_id: UUID, profile_id: UUID, revision: int
    ):
        table = ProfileRevisionRow.__table__
        return (
            connection.execute(
                select(table).where(
                    table.c.workspace_id == workspace_id,
                    table.c.profile_id == profile_id,
                    table.c.revision == revision,
                )
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _resource_row(
        connection: Connection, workspace_id: UUID, resource_id: UUID, revision: int
    ):
        table = ResourceRevisionRow.__table__
        return (
            connection.execute(
                select(table).where(
                    table.c.workspace_id == workspace_id,
                    table.c.resource_id == resource_id,
                    table.c.revision == revision,
                )
            )
            .mappings()
            .one_or_none()
        )
