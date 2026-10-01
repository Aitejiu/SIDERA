from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from ..admission import OperationRecord
from ..contracts import (
    AuthenticationState,
    CapabilityResourceDraft,
    NativeHandle,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    ProfileStatus,
    ResourceRef,
    ResourceRevision,
    ResourceStatus,
    RunSnapshot,
    RunState,
    RuntimeProbe,
    RuntimeRef,
    StartReceipt,
    StartStatus,
)
from ..errors import AgentError
from ..ports import (
    RuntimeEntry,
    RuntimeInstallation,
    RuntimeObservation,
    RuntimeRecord,
)
from .models import (
    AgentRunRow,
    ProfileHead,
    ProfileRevisionRow,
    ResourceHead,
    ResourceRevisionRow,
    RunOperationRow,
    RunReservationRow,
    RuntimeInstallationRow,
    RuntimeReferenceRow,
)

_OWNER_LOCK_CLASS = 1397310533
_OWNER_LOCK_ID = 3


def _resolve_alembic_config_path(package_root: Path, source_root: Path) -> Path:
    packaged_config = package_root / "alembic.ini"
    if packaged_config.is_file():
        return packaged_config
    return source_root / "alembic.ini"


def _not_found(kind: str) -> AgentError:
    return AgentError("not_found", f"{kind.capitalize()} not found.")


def _revision_conflict(message: str) -> AgentError:
    return AgentError("revision_conflict", message)


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Stored timestamp must be timezone-aware.")
    return value.astimezone(UTC)


def _profile_from_row(row: Mapping[str, Any]) -> ProfileRevision:
    return ProfileRevision(
        ref=ProfileRef(
            workspace_id=row["workspace_id"],
            profile_id=row["profile_id"],
            revision=row["revision"],
        ),
        draft=ProfileDraft.model_validate(row["draft"]),
        config_hash=row["config_hash"],
        created_at=_utc_timestamp(row["created_at"]),
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
        created_at=_utc_timestamp(row["created_at"]),
    )


def _runtime_entry_from_rows(
    runtime_row: Mapping[str, Any], installation_row: Mapping[str, Any]
) -> RuntimeEntry:
    probe = RuntimeProbe(
        actual_version=runtime_row["actual_version"],
        protocol_state=runtime_row["protocol_state"],
        capabilities=runtime_row["capabilities"],
        checked_at=_utc_timestamp(runtime_row["checked_at"]),
    )
    record = RuntimeRecord(
        ref=RuntimeRef(
            workspace_id=runtime_row["workspace_id"],
            runtime_id=runtime_row["runtime_id"],
        ),
        installation_key=runtime_row["installation_key"],
        harness_id=runtime_row["harness_id"],
        executable=runtime_row["executable"],
        observation=RuntimeObservation(
            probe=probe,
            installed=runtime_row["installed"],
            file_identity=runtime_row["file_identity"],
            auth_state=AuthenticationState(runtime_row["auth_state"]),
        ),
    )
    installation = RuntimeInstallation(
        installation_key=installation_row["installation_key"],
        harness_id=installation_row["harness_id"],
        executable=installation_row["executable"],
        max_active=installation_row["max_active"],
        max_sessions=installation_row["max_sessions"],
        enabled=installation_row["enabled"],
        version=installation_row["config_version"],
        active_count=installation_row["active_count"],
        session_count=installation_row["session_count"],
    )
    return RuntimeEntry(record=record, installation=installation)


def _start_receipt_from_row(row: Mapping[str, Any]) -> StartReceipt:
    return StartReceipt(
        operation_id=row["operation_id"],
        status=StartStatus(row["status"]),
        run_id=row["run_id"],
        reason_code=row["reason_code"],
    )


class PostgresAgentStore:
    """Synchronous catalog persistence over a caller-owned SQLAlchemy engine."""

    def __init__(self, engine: Engine, *, owns_engine: bool = False) -> None:
        self._engine = engine
        self._owns_engine = owns_engine
        self._owner_state_lock = Lock()
        self._owner_connection: Connection | None = None
        self._owner_id: UUID | None = None

    def migrate(self) -> None:
        from alembic import command
        from alembic.config import Config

        package_root = Path(str(files("sidera_agents")))
        source_root = Path(__file__).resolve().parents[3]
        config_path = _resolve_alembic_config_path(package_root, source_root)
        config = Config(str(config_path))
        with self._engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def readiness(self) -> bool:
        with self._engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one() == 1

    def close(self) -> None:
        with self._owner_state_lock:
            owner_id = self._owner_id
        if owner_id is not None:
            self.release_execution_owner(owner_id)
        if self._owns_engine:
            self._engine.dispose()

    def acquire_execution_owner(self, owner_id: UUID) -> bool:
        with self._owner_state_lock:
            if self._owner_connection is not None:
                return self._owner_id == owner_id and self._owner_is_held_locked()
            connection = self._engine.connect()
            try:
                acquired = connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_class, :lock_id)"),
                    {"lock_class": _OWNER_LOCK_CLASS, "lock_id": _OWNER_LOCK_ID},
                ).scalar_one()
                connection.commit()
            except Exception:  # noqa: BLE001 - owner acquisition fails closed on driver errors
                connection.close()
                return False
            if acquired is not True:
                connection.close()
                return False
            self._owner_connection = connection
            self._owner_id = owner_id
            return True

    def execution_owner_is_held(self, owner_id: UUID) -> bool:
        with self._owner_state_lock:
            return self._owner_id == owner_id and self._owner_is_held_locked()

    def release_execution_owner(self, owner_id: UUID) -> None:
        with self._owner_state_lock:
            if self._owner_id != owner_id:
                return
            connection = self._owner_connection
            if connection is not None:
                try:
                    released = connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_class, :lock_id)"),
                        {
                            "lock_class": _OWNER_LOCK_CLASS,
                            "lock_id": _OWNER_LOCK_ID,
                        },
                    ).scalar_one()
                    if released is not True:
                        raise RuntimeError("The execution owner lock was not released.")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                connection.close()
                self._owner_connection = None
                self._owner_id = None

    def _owner_is_held_locked(self) -> bool:
        connection = self._owner_connection
        if connection is None:
            return False
        try:
            backend_pid = connection.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            held = connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' AND pid = :pid "
                    "AND classid = CAST(:lock_class AS oid) "
                    "AND objid = CAST(:lock_id AS oid) "
                    "AND objsubid = 2 AND granted)"
                ),
                {
                    "pid": backend_pid,
                    "lock_class": _OWNER_LOCK_CLASS,
                    "lock_id": _OWNER_LOCK_ID,
                },
            ).scalar_one()
            connection.rollback()
            return held is True
        except Exception:  # noqa: BLE001 - an unverified owner lock is never treated as held
            connection.rollback()
            return False

    def _assert_owner(self, connection: Connection, owner_id: UUID) -> None:
        with self._owner_state_lock:
            if self._owner_id != owner_id or not self._owner_is_held_locked():
                raise AgentError(
                    "execution_owner_unavailable",
                    "The execution owner lock is not held.",
                )
            owner_connection = self._owner_connection
            if owner_connection is None:
                raise AgentError(
                    "execution_owner_unavailable",
                    "The execution owner lock is not held.",
                )
            owner_pid = owner_connection.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            owner_connection.rollback()
        held = connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = :pid "
                "AND classid = CAST(:lock_class AS oid) "
                "AND objid = CAST(:lock_id AS oid) "
                "AND objsubid = 2 AND granted)"
            ),
            {
                "pid": owner_pid,
                "lock_class": _OWNER_LOCK_CLASS,
                "lock_id": _OWNER_LOCK_ID,
            },
        ).scalar_one()
        if held is not True:
            raise AgentError(
                "execution_owner_unavailable",
                "The execution owner lock is not held.",
            )

    def find_runtime(
        self, workspace_id: UUID, harness_id: str, executable: str
    ) -> RuntimeEntry | None:
        references = RuntimeReferenceRow.__table__
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(references).where(
                        references.c.workspace_id == workspace_id,
                        references.c.harness_id == harness_id,
                        references.c.executable == executable,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return self._runtime_entry_for_row(connection, row)

    def register_runtime(
        self,
        workspace_id: UUID,
        runtime_id: UUID,
        harness_id: str,
        executable: str,
        installation_key: str,
        observation: RuntimeObservation,
        max_active: int,
        max_sessions: int,
    ) -> RuntimeEntry:
        installations = RuntimeInstallationRow.__table__
        references = RuntimeReferenceRow.__table__
        probe = observation.probe
        with self._engine.begin() as connection:
            connection.execute(
                pg_insert(installations)
                .values(
                    installation_key=installation_key,
                    harness_id=harness_id,
                    executable=executable,
                    max_active=max_active,
                    max_sessions=max_sessions,
                    enabled=True,
                    config_version=1,
                    active_count=0,
                    session_count=0,
                )
                .on_conflict_do_nothing(
                    index_elements=[installations.c.installation_key]
                )
            )
            installation = (
                connection.execute(
                    select(installations)
                    .where(installations.c.installation_key == installation_key)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if installation is None or (
                installation["harness_id"],
                installation["executable"],
            ) != (harness_id, executable):
                raise AgentError("catalog_corrupt", "Runtime installation is invalid.")

            connection.execute(
                pg_insert(references)
                .values(
                    workspace_id=workspace_id,
                    runtime_id=runtime_id,
                    installation_key=installation_key,
                    harness_id=harness_id,
                    executable=executable,
                    actual_version=probe.actual_version,
                    installed=observation.installed,
                    protocol_state=probe.protocol_state.value,
                    auth_state=observation.auth_state.value,
                    capabilities=probe.model_dump(mode="json")["capabilities"],
                    checked_at=probe.checked_at,
                    file_identity=observation.file_identity,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        references.c.workspace_id,
                        references.c.harness_id,
                        references.c.executable,
                    ]
                )
            )
            runtime_row = (
                connection.execute(
                    select(references).where(
                        references.c.workspace_id == workspace_id,
                        references.c.harness_id == harness_id,
                        references.c.executable == executable,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if runtime_row is None:
                raise AgentError("catalog_corrupt", "Runtime reference is missing.")
            return _runtime_entry_from_rows(runtime_row, installation)

    def get_runtime_entry(self, ref: RuntimeRef) -> RuntimeEntry:
        references = RuntimeReferenceRow.__table__
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(references).where(
                        references.c.workspace_id == ref.workspace_id,
                        references.c.runtime_id == ref.runtime_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise _not_found("runtime")
            return self._runtime_entry_for_row(connection, row)

    def list_runtime_entries(self, workspace_id: UUID) -> tuple[RuntimeEntry, ...]:
        references = RuntimeReferenceRow.__table__
        with self._engine.connect() as connection:
            rows = tuple(
                connection.execute(
                    select(references)
                    .where(references.c.workspace_id == workspace_id)
                    .order_by(references.c.harness_id, references.c.executable)
                ).mappings()
            )
            return tuple(self._runtime_entry_for_row(connection, row) for row in rows)

    def update_runtime_observation(
        self, ref: RuntimeRef, observation: RuntimeObservation
    ) -> RuntimeEntry:
        table = RuntimeReferenceRow.__table__
        probe = observation.probe
        with self._engine.begin() as connection:
            result = connection.execute(
                update(table)
                .where(
                    table.c.workspace_id == ref.workspace_id,
                    table.c.runtime_id == ref.runtime_id,
                )
                .values(
                    actual_version=probe.actual_version,
                    installed=observation.installed,
                    protocol_state=probe.protocol_state.value,
                    auth_state=observation.auth_state.value,
                    capabilities=probe.model_dump(mode="json")["capabilities"],
                    checked_at=probe.checked_at,
                    file_identity=observation.file_identity,
                )
            )
            if result.rowcount != 1:
                raise _not_found("runtime")
            row = (
                connection.execute(
                    select(table).where(
                        table.c.workspace_id == ref.workspace_id,
                        table.c.runtime_id == ref.runtime_id,
                    )
                )
                .mappings()
                .one()
            )
            return self._runtime_entry_for_row(connection, row)

    def set_runtime_configuration(
        self,
        ref: RuntimeRef,
        *,
        expected_version: int,
        max_active: int | None = None,
        max_sessions: int | None = None,
        enabled: bool | None = None,
    ) -> RuntimeEntry:
        references = RuntimeReferenceRow.__table__
        installations = RuntimeInstallationRow.__table__
        with self._engine.begin() as connection:
            runtime_row = (
                connection.execute(
                    select(references.c.installation_key).where(
                        references.c.workspace_id == ref.workspace_id,
                        references.c.runtime_id == ref.runtime_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if runtime_row is None:
                raise _not_found("runtime")
            values: dict[str, object] = {
                "config_version": installations.c.config_version + 1
            }
            if max_active is not None:
                values["max_active"] = max_active
            if max_sessions is not None:
                values["max_sessions"] = max_sessions
            if enabled is not None:
                values["enabled"] = enabled
            result = connection.execute(
                update(installations)
                .where(
                    installations.c.installation_key == runtime_row["installation_key"],
                    installations.c.config_version == expected_version,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                current = connection.execute(
                    select(installations.c.installation_key).where(
                        installations.c.installation_key
                        == runtime_row["installation_key"]
                    )
                ).first()
                if current is None:
                    raise AgentError(
                        "catalog_corrupt", "Runtime installation is missing."
                    )
                raise _revision_conflict("Runtime configuration version is stale.")
            row = (
                connection.execute(
                    select(references).where(
                        references.c.workspace_id == ref.workspace_id,
                        references.c.runtime_id == ref.runtime_id,
                    )
                )
                .mappings()
                .one()
            )
            return self._runtime_entry_for_row(connection, row)

    def get_profile_status(self, workspace_id: UUID, profile_id: UUID) -> ProfileStatus:
        table = ProfileHead.__table__
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(table.c.enabled, table.c.status_version).where(
                        table.c.workspace_id == workspace_id,
                        table.c.profile_id == profile_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise _not_found("profile")
        return ProfileStatus(
            profile_id=profile_id,
            enabled=row["enabled"],
            version=row["status_version"],
        )

    @staticmethod
    def _runtime_entry_for_row(connection: Connection, runtime_row) -> RuntimeEntry:
        installations = RuntimeInstallationRow.__table__
        installation_row = (
            connection.execute(
                select(installations).where(
                    installations.c.installation_key == runtime_row["installation_key"]
                )
            )
            .mappings()
            .one_or_none()
        )
        if installation_row is None:
            raise AgentError("catalog_corrupt", "Runtime installation is missing.")
        return _runtime_entry_from_rows(runtime_row, installation_row)

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

    def admit_start(
        self,
        *,
        owner_id: UUID,
        profile_ref: ProfileRef,
        runtime_ref: RuntimeRef,
        installation_key: str,
        operation_id: UUID,
        profile_config_hash: str,
        fingerprint: str,
        run_id: UUID,
        snapshot: RunSnapshot,
    ) -> tuple[StartReceipt, bool]:
        operations = RunOperationRow.__table__
        runs = AgentRunRow.__table__
        reservations = RunReservationRow.__table__
        installations = RuntimeInstallationRow.__table__
        references = RuntimeReferenceRow.__table__
        profile_heads = ProfileHead.__table__
        profile_revisions = ProfileRevisionRow.__table__

        with self._engine.begin() as connection:
            self._assert_owner(connection, owner_id)
            connection.execute(
                pg_insert(operations)
                .values(
                    workspace_id=profile_ref.workspace_id,
                    operation_id=operation_id,
                    profile_id=profile_ref.profile_id,
                    profile_revision=profile_ref.revision,
                    profile_config_hash=profile_config_hash,
                    fingerprint=fingerprint,
                    status=StartStatus.BUSY.value,
                    run_id=None,
                    reason_code=None,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        operations.c.workspace_id,
                        operations.c.operation_id,
                    ]
                )
            )
            operation = (
                connection.execute(
                    select(operations)
                    .where(
                        operations.c.workspace_id == profile_ref.workspace_id,
                        operations.c.operation_id == operation_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                operation["profile_id"] != profile_ref.profile_id
                or operation["profile_revision"] != profile_ref.revision
                or operation["profile_config_hash"] != profile_config_hash
                or operation["fingerprint"] != fingerprint
            ):
                raise AgentError(
                    "operation_conflict",
                    "Operation ID was used with a different request.",
                )
            if operation["status"] != StartStatus.BUSY.value:
                return _start_receipt_from_row(operation), False

            installation = (
                connection.execute(
                    select(installations)
                    .where(installations.c.installation_key == installation_key)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            profile_head = (
                connection.execute(
                    select(profile_heads)
                    .where(
                        profile_heads.c.workspace_id == profile_ref.workspace_id,
                        profile_heads.c.profile_id == profile_ref.profile_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            pinned_revision = connection.execute(
                select(profile_revisions.c.revision).where(
                    profile_revisions.c.workspace_id == profile_ref.workspace_id,
                    profile_revisions.c.profile_id == profile_ref.profile_id,
                    profile_revisions.c.revision == profile_ref.revision,
                )
            ).first()
            runtime_reference = connection.execute(
                select(references.c.installation_key).where(
                    references.c.workspace_id == runtime_ref.workspace_id,
                    references.c.runtime_id == runtime_ref.runtime_id,
                )
            ).scalar_one_or_none()
            if (
                installation is None
                or profile_head is None
                or pinned_revision is None
                or runtime_reference != installation_key
            ):
                raise AgentError(
                    "configuration_invalid", "Run configuration is unavailable."
                )
            if not profile_head["enabled"] or not installation["enabled"]:
                raise AgentError(
                    "configuration_invalid", "Run configuration is disabled."
                )
            current_revision = (
                connection.execute(
                    select(profile_revisions.c.draft).where(
                        profile_revisions.c.workspace_id == profile_ref.workspace_id,
                        profile_revisions.c.profile_id == profile_ref.profile_id,
                        profile_revisions.c.revision == profile_head["revision"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current_revision is None:
                raise AgentError("catalog_corrupt", "Profile head revision is missing.")
            profile_limit = ProfileDraft.model_validate(
                current_revision["draft"]
            ).max_active
            profile_active = connection.execute(
                select(func.count())
                .select_from(reservations)
                .where(
                    reservations.c.workspace_id == profile_ref.workspace_id,
                    reservations.c.profile_id == profile_ref.profile_id,
                    reservations.c.active.is_(True),
                )
            ).scalar_one()
            busy = (
                profile_active >= profile_limit
                or installation["active_count"] >= installation["max_active"]
                or installation["session_count"] >= installation["max_sessions"]
            )
            self._assert_owner(connection, owner_id)
            if busy:
                connection.execute(
                    update(operations)
                    .where(
                        operations.c.workspace_id == profile_ref.workspace_id,
                        operations.c.operation_id == operation_id,
                    )
                    .values(
                        status=StartStatus.BUSY.value,
                        run_id=None,
                        reason_code="resource_busy",
                    )
                )
                return (
                    StartReceipt(
                        operation_id=operation_id,
                        status=StartStatus.BUSY,
                        run_id=None,
                        reason_code="resource_busy",
                    ),
                    False,
                )

            connection.execute(
                runs.insert().values(
                    run_id=run_id,
                    workspace_id=profile_ref.workspace_id,
                    operation_id=operation_id,
                    profile_id=profile_ref.profile_id,
                    profile_revision=profile_ref.revision,
                    runtime_workspace_id=runtime_ref.workspace_id,
                    runtime_id=runtime_ref.runtime_id,
                    installation_key=installation_key,
                    state=snapshot.state.value,
                    snapshot=snapshot.model_dump(mode="json", warnings=False),
                    native_handle=None,
                    terminal_state=None,
                )
            )
            connection.execute(
                reservations.insert().values(
                    run_id=run_id,
                    workspace_id=profile_ref.workspace_id,
                    profile_id=profile_ref.profile_id,
                    installation_key=installation_key,
                    active=True,
                    session=True,
                )
            )
            connection.execute(
                update(installations)
                .where(installations.c.installation_key == installation_key)
                .values(
                    active_count=installation["active_count"] + 1,
                    session_count=installation["session_count"] + 1,
                )
            )
            connection.execute(
                update(operations)
                .where(
                    operations.c.workspace_id == profile_ref.workspace_id,
                    operations.c.operation_id == operation_id,
                )
                .values(
                    status=StartStatus.ACCEPTED.value,
                    run_id=run_id,
                    reason_code=None,
                )
            )
        return (
            StartReceipt(
                operation_id=operation_id,
                status=StartStatus.ACCEPTED,
                run_id=run_id,
                reason_code=None,
            ),
            True,
        )

    def get_start_receipt(
        self, workspace_id: UUID, operation_id: UUID
    ) -> StartReceipt | None:
        table = RunOperationRow.__table__
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(table).where(
                        table.c.workspace_id == workspace_id,
                        table.c.operation_id == operation_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _start_receipt_from_row(row)

    def get_start_operation(
        self, workspace_id: UUID, operation_id: UUID
    ) -> OperationRecord | None:
        table = RunOperationRow.__table__
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(table).where(
                        table.c.workspace_id == workspace_id,
                        table.c.operation_id == operation_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return OperationRecord(
            profile_ref=ProfileRef(
                workspace_id=workspace_id,
                profile_id=row["profile_id"],
                revision=row["profile_revision"],
            ),
            profile_config_hash=row["profile_config_hash"],
            fingerprint=row["fingerprint"],
            receipt=_start_receipt_from_row(row),
        )

    def get_run_snapshot(self, workspace_id: UUID, run_id: UUID) -> RunSnapshot:
        table = AgentRunRow.__table__
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(table.c.snapshot).where(
                        table.c.workspace_id == workspace_id,
                        table.c.run_id == run_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise _not_found("run")
        return RunSnapshot.model_validate(row["snapshot"])

    def list_starting_runs(self) -> tuple[RunSnapshot, ...]:
        table = AgentRunRow.__table__
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(table.c.snapshot)
                    .where(table.c.state == RunState.STARTING.value)
                    .order_by(table.c.run_id)
                )
                .mappings()
                .all()
            )
        return tuple(RunSnapshot.model_validate(row["snapshot"]) for row in rows)

    def list_unresolved_runs(
        self,
    ) -> tuple[tuple[RunSnapshot, NativeHandle, RunState | None, str | None], ...]:
        table = AgentRunRow.__table__
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        table.c.snapshot,
                        table.c.native_handle,
                        table.c.terminal_state,
                        table.c.terminal_reason_code,
                    )
                    .where(
                        table.c.state.in_(
                            (
                                RunState.RUNNING.value,
                                RunState.UNKNOWN.value,
                                RunState.CLOSING.value,
                            )
                        ),
                        table.c.native_handle.is_not(None),
                    )
                    .order_by(table.c.run_id)
                )
                .mappings()
                .all()
            )
        return tuple(
            (
                RunSnapshot.model_validate(row["snapshot"]),
                NativeHandle.model_validate(row["native_handle"]),
                None
                if row["terminal_state"] is None
                else RunState(row["terminal_state"]),
                row["terminal_reason_code"],
            )
            for row in rows
            if row["native_handle"] is not None
        )

    def transition_run(
        self,
        run_id: UUID,
        snapshot: RunSnapshot,
        *,
        owner_id: UUID,
        native_handle: NativeHandle | None = None,
        replace_native_handle: bool = False,
        receipt_status: StartStatus | None = None,
        active_reserved: bool | None = None,
        session_reserved: bool | None = None,
        terminal_state: RunState | None = None,
        terminal_reason_code: str | None = None,
    ) -> None:
        runs = AgentRunRow.__table__
        reservations = RunReservationRow.__table__
        installations = RuntimeInstallationRow.__table__
        operations = RunOperationRow.__table__
        with self._engine.begin() as connection:
            self._assert_owner(connection, owner_id)
            reservation_key = connection.execute(
                select(reservations.c.installation_key).where(
                    reservations.c.run_id == run_id
                )
            ).scalar_one_or_none()
            if reservation_key is None:
                raise _not_found("run")
            installation = (
                connection.execute(
                    select(installations)
                    .where(installations.c.installation_key == reservation_key)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            reservation = (
                connection.execute(
                    select(reservations)
                    .where(reservations.c.run_id == run_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            run = (
                connection.execute(
                    select(runs).where(runs.c.run_id == run_id).with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if installation is None or reservation is None or run is None:
                raise _not_found("run")
            if (
                snapshot.run_id != run_id
                or snapshot.workspace_id != run["workspace_id"]
            ):
                raise AgentError("catalog_corrupt", "Run identity is invalid.")
            active_delta = (
                int(active_reserved) - int(reservation["active"])
                if active_reserved is not None
                else 0
            )
            session_delta = (
                int(session_reserved) - int(reservation["session"])
                if session_reserved is not None
                else 0
            )
            if (
                installation["active_count"] + active_delta < 0
                or installation["session_count"] + session_delta < 0
            ):
                raise AgentError("catalog_corrupt", "Run reservation count is invalid.")
            connection.execute(
                update(installations)
                .where(installations.c.installation_key == reservation_key)
                .values(
                    active_count=installation["active_count"] + active_delta,
                    session_count=installation["session_count"] + session_delta,
                )
            )
            reservation_values: dict[str, object] = {}
            if active_reserved is not None:
                reservation_values["active"] = active_reserved
            if session_reserved is not None:
                reservation_values["session"] = session_reserved
            if reservation_values:
                connection.execute(
                    update(reservations)
                    .where(reservations.c.run_id == run_id)
                    .values(**reservation_values)
                )
            run_values: dict[str, object] = {
                "state": snapshot.state.value,
                "snapshot": snapshot.model_dump(mode="json", warnings=False),
            }
            if replace_native_handle:
                run_values["native_handle"] = (
                    None
                    if native_handle is None
                    else native_handle.model_dump(mode="json", warnings=False)
                )
            if terminal_state is not None:
                run_values["terminal_state"] = terminal_state.value
                run_values["terminal_reason_code"] = terminal_reason_code
            connection.execute(
                update(runs).where(runs.c.run_id == run_id).values(**run_values)
            )
            if receipt_status is not None:
                connection.execute(
                    update(operations)
                    .where(
                        operations.c.workspace_id == run["workspace_id"],
                        operations.c.operation_id == run["operation_id"],
                    )
                    .values(
                        status=receipt_status.value,
                        run_id=run_id,
                        reason_code=snapshot.reason_code,
                    )
                )

    def _require_enabled_resources(
        self, connection: Connection, workspace_id: UUID, refs: tuple[ResourceRef, ...]
    ) -> None:
        heads = ResourceHead.__table__
        revisions = ResourceRevisionRow.__table__
        if any(ref.workspace_id != workspace_id for ref in refs):
            raise AgentError(
                "configuration_invalid",
                "Resource reference belongs to another workspace.",
            )
        ordered_refs = sorted(
            refs, key=lambda ref: (ref.workspace_id.int, ref.resource_id.int)
        )
        for ref in ordered_refs:
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
