from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from sidera_model_connections.contracts import (
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    CredentialWrite,
    CredentialWriteStatus,
    ModelConnectionRevision,
)
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.postgres.models import (
    ConnectionRevisionRow,
    ConnectionRow,
    CredentialEventRow,
    CredentialRow,
    CredentialWriteRow,
)

SessionFactory = sessionmaker[Session]


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _credential_values(credential: CredentialMetadata) -> dict[str, object]:
    return {
        "workspace_id": credential.ref.workspace_id,
        "id": credential.ref.credential_id,
        "alias": credential.alias,
        "source": credential.source,
        "status": credential.status,
        "env_name": credential.env_name,
        "replaces_id": (
            credential.replaces_ref.credential_id
            if credential.replaces_ref is not None
            else None
        ),
        "version": credential.version,
        "created_at": credential.created_at,
        "revoked_at": None,
    }


def _revision_values(connection: ModelConnectionRevision) -> dict[str, object]:
    return {
        "workspace_id": connection.ref.workspace_id,
        "connection_id": connection.ref.connection_id,
        "revision": connection.ref.revision,
        "name": connection.name,
        "provider_id": connection.provider_id,
        "protocol": connection.protocol,
        "base_url": connection.base_url,
        "network_scope": connection.network_scope,
        "allowed_roles": sorted(connection.allowed_roles),
        "credential_id": connection.credential_ref.credential_id,
        "created_at": connection.created_at,
    }


def _to_credential(row: CredentialRow) -> CredentialMetadata:
    replaces_ref = (
        CredentialRef(row.workspace_id, row.replaces_id)
        if row.replaces_id is not None
        else None
    )
    return CredentialMetadata(
        ref=CredentialRef(row.workspace_id, row.id),
        alias=row.alias,
        source=row.source,
        status=row.status,
        env_name=row.env_name,
        replaces_ref=replaces_ref,
        created_at=_as_utc(row.created_at),
        version=row.version,
    )


def _to_connection(row: ConnectionRevisionRow) -> ModelConnectionRevision:
    return ModelConnectionRevision(
        ref=ConnectionRef(row.workspace_id, row.connection_id, row.revision),
        name=row.name,
        provider_id=row.provider_id,
        protocol=row.protocol,
        base_url=row.base_url,
        network_scope=row.network_scope,
        allowed_roles=frozenset(row.allowed_roles),
        credential_ref=CredentialRef(row.workspace_id, row.credential_id),
        created_at=_as_utc(row.created_at),
    )


def _same_operation_metadata(
    stored: CredentialMetadata,
    requested: CredentialMetadata,
) -> bool:
    return (
        stored.alias == requested.alias
        and stored.source == requested.source
        and stored.env_name == requested.env_name
        and stored.replaces_ref == requested.replaces_ref
    )


def _insert_event(
    session: Session,
    *,
    workspace_id: UUID,
    credential_id: UUID,
    event_type: str,
    operation_id: UUID | None = None,
) -> None:
    session.execute(
        insert(CredentialEventRow).values(
            id=uuid4(),
            workspace_id=workspace_id,
            credential_id=credential_id,
            operation_id=operation_id,
            type=event_type,
            occurred_at=_now(),
        )
    )


class PostgresConnectionCatalog:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def get_connection(self, ref: ConnectionRef) -> ModelConnectionRevision:
        with self._session_factory() as session:
            row = session.scalar(
                select(ConnectionRevisionRow).where(
                    ConnectionRevisionRow.workspace_id == ref.workspace_id,
                    ConnectionRevisionRow.connection_id == ref.connection_id,
                    ConnectionRevisionRow.revision == ref.revision,
                )
            )
            if row is None:
                raise ConfigurationError("configuration_not_found")
            return _to_connection(row)

    def get_credential(self, ref: CredentialRef) -> CredentialMetadata:
        with self._session_factory() as session:
            row = session.scalar(
                select(CredentialRow).where(
                    CredentialRow.workspace_id == ref.workspace_id,
                    CredentialRow.id == ref.credential_id,
                )
            )
            if row is None:
                raise ConfigurationError("credential_not_found")
            return _to_credential(row)

    @contextmanager
    def credential_read_lease(self, ref: CredentialRef) -> Iterator[CredentialMetadata]:
        with self._session_factory() as session, session.begin():
            row = session.scalar(
                select(CredentialRow)
                .where(
                    CredentialRow.workspace_id == ref.workspace_id,
                    CredentialRow.id == ref.credential_id,
                )
                .with_for_update(read=True)
            )
            if row is None:
                raise ConfigurationError("credential_not_found")
            yield _to_credential(row)

    def list_connections(self, workspace_id: UUID) -> tuple[ModelConnectionRevision, ...]:
        statement = (
            select(ConnectionRevisionRow)
            .join(
                ConnectionRow,
                (ConnectionRow.workspace_id == ConnectionRevisionRow.workspace_id)
                & (ConnectionRow.id == ConnectionRevisionRow.connection_id)
                & (ConnectionRow.head_revision == ConnectionRevisionRow.revision),
            )
            .where(ConnectionRow.workspace_id == workspace_id)
            .order_by(ConnectionRow.created_at, ConnectionRow.id)
        )
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
            return tuple(_to_connection(row) for row in rows)

    def create_connection(self, connection: ModelConnectionRevision) -> None:
        if connection.ref.revision != 1:
            raise ConfigurationError("invalid_configuration")
        try:
            with self._session_factory.begin() as session:
                credential_exists = session.scalar(
                    select(CredentialRow.id).where(
                        CredentialRow.workspace_id == connection.ref.workspace_id,
                        CredentialRow.id == connection.credential_ref.credential_id,
                    )
                )
                if credential_exists is None:
                    raise ConfigurationError("credential_not_found")
                session.execute(
                    insert(ConnectionRow).values(
                        workspace_id=connection.ref.workspace_id,
                        id=connection.ref.connection_id,
                        name=connection.name,
                        head_revision=1,
                        version=1,
                        created_at=connection.created_at,
                    )
                )
                session.execute(insert(ConnectionRevisionRow).values(_revision_values(connection)))
        except IntegrityError:
            raise ConfigurationError("configuration_version_conflict") from None

    def append_connection(
        self,
        connection: ModelConnectionRevision,
        *,
        expected_revision: int,
    ) -> None:
        if (
            type(expected_revision) is not int
            or expected_revision < 1
            or connection.ref.revision != expected_revision + 1
        ):
            raise ConfigurationError("configuration_version_conflict")
        workspace_id = connection.ref.workspace_id
        connection_id = connection.ref.connection_id
        try:
            with self._session_factory.begin() as session:
                result = session.execute(
                    update(ConnectionRow)
                    .where(
                        ConnectionRow.workspace_id == workspace_id,
                        ConnectionRow.id == connection_id,
                        ConnectionRow.head_revision == expected_revision,
                    )
                    .values(
                        name=connection.name,
                        head_revision=connection.ref.revision,
                        version=ConnectionRow.version + 1,
                    )
                )
                if result.rowcount != 1:
                    exists = session.scalar(
                        select(ConnectionRow.id).where(
                            ConnectionRow.workspace_id == workspace_id,
                            ConnectionRow.id == connection_id,
                        )
                    )
                    if exists is None:
                        raise ConfigurationError("configuration_not_found")
                    raise ConfigurationError("configuration_version_conflict")
                credential_exists = session.scalar(
                    select(CredentialRow.id).where(
                        CredentialRow.workspace_id == workspace_id,
                        CredentialRow.id == connection.credential_ref.credential_id,
                    )
                )
                if credential_exists is None:
                    raise ConfigurationError("credential_not_found")
                session.execute(
                    insert(ConnectionRevisionRow).values(_revision_values(connection))
                )
        except IntegrityError:
            raise ConfigurationError("configuration_version_conflict") from None

    def reserve_credential_write(self, write: CredentialWrite) -> CredentialWrite:
        credential = write.credential
        if (
            write.status != "pending"
            or write.failure_code is not None
            or credential.source != "macos_keychain"
            or credential.status != "pending"
        ):
            raise ConfigurationError("invalid_configuration")
        try:
            with self._session_factory.begin() as session:
                lock_key = f"{write.workspace_id}:{write.operation_id}"
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": lock_key},
                )
                existing = session.scalar(
                    select(CredentialWriteRow).where(
                        CredentialWriteRow.workspace_id == write.workspace_id,
                        CredentialWriteRow.operation_id == write.operation_id,
                    )
                )
                if existing is not None:
                    stored_credential = session.scalar(
                        select(CredentialRow).where(
                            CredentialRow.workspace_id == write.workspace_id,
                            CredentialRow.id == existing.credential_id,
                        )
                    )
                    if stored_credential is None:
                        raise ConfigurationError("credential_write_unknown")
                    if not _same_operation_metadata(
                        _to_credential(stored_credential), credential
                    ):
                        raise ConfigurationError("credential_operation_conflict")
                    return CredentialWrite(
                        workspace_id=write.workspace_id,
                        operation_id=write.operation_id,
                        credential=_to_credential(stored_credential),
                        status=existing.status,
                        failure_code=existing.failure_code,
                    )

                if session.scalar(
                    select(CredentialRow.id).where(
                        CredentialRow.workspace_id == write.workspace_id,
                        CredentialRow.id == credential.ref.credential_id,
                    )
                ) is not None:
                    raise ConfigurationError("credential_already_exists")
                session.execute(insert(CredentialRow).values(_credential_values(credential)))
                session.execute(
                    insert(CredentialWriteRow).values(
                        workspace_id=write.workspace_id,
                        operation_id=write.operation_id,
                        credential_id=credential.ref.credential_id,
                        status="pending",
                        failure_code=None,
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
                return write
        except IntegrityError:
            raise ConfigurationError("credential_already_exists") from None

    def get_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
    ) -> CredentialWrite:
        with self._session_factory() as session:
            row = session.scalar(
                select(CredentialWriteRow).where(
                    CredentialWriteRow.workspace_id == workspace_id,
                    CredentialWriteRow.operation_id == operation_id,
                )
            )
            if row is None:
                raise ConfigurationError("credential_write_unknown")
            credential = session.scalar(
                select(CredentialRow).where(
                    CredentialRow.workspace_id == workspace_id,
                    CredentialRow.id == row.credential_id,
                )
            )
            if credential is None:
                raise ConfigurationError("credential_write_unknown")
            return CredentialWrite(
                workspace_id=workspace_id,
                operation_id=operation_id,
                credential=_to_credential(credential),
                status=row.status,
                failure_code=row.failure_code,
            )

    def finish_credential_write(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        *,
        status: CredentialWriteStatus,
    ) -> CredentialWrite:
        if status not in {"completed", "failed", "unknown"}:
            raise ConfigurationError("invalid_configuration")
        with self._session_factory.begin() as session:
            write_row = session.scalar(
                select(CredentialWriteRow)
                .where(
                    CredentialWriteRow.workspace_id == workspace_id,
                    CredentialWriteRow.operation_id == operation_id,
                )
                .with_for_update()
            )
            if write_row is None:
                raise ConfigurationError("credential_write_unknown")
            credential_row = session.scalar(
                select(CredentialRow)
                .where(
                    CredentialRow.workspace_id == workspace_id,
                    CredentialRow.id == write_row.credential_id,
                )
                .with_for_update()
            )
            if credential_row is None:
                raise ConfigurationError("credential_write_unknown")
            current = write_row.status
            credential = _to_credential(credential_row)
            if current == status:
                return CredentialWrite(
                    workspace_id=workspace_id,
                    operation_id=operation_id,
                    credential=credential,
                    status=current,
                    failure_code=write_row.failure_code,
                )
            if current not in {"pending", "unknown"} or (
                current == "unknown" and status == "unknown"
            ):
                raise ConfigurationError("credential_operation_conflict")

            changed = session.execute(
                update(CredentialWriteRow)
                .where(
                    CredentialWriteRow.workspace_id == workspace_id,
                    CredentialWriteRow.operation_id == operation_id,
                    CredentialWriteRow.status == current,
                )
                .values(status=status, updated_at=_now())
            )
            if changed.rowcount != 1:
                raise ConfigurationError("credential_operation_conflict")

            updated_credential = credential
            if status in {"completed", "failed"}:
                credential_status = "active" if status == "completed" else "failed"
                changed_credential = session.execute(
                    update(CredentialRow)
                    .where(
                        CredentialRow.workspace_id == workspace_id,
                        CredentialRow.id == credential.ref.credential_id,
                        CredentialRow.status == "pending",
                        CredentialRow.version == credential.version,
                    )
                    .values(
                        status=credential_status,
                        version=CredentialRow.version + 1,
                    )
                )
                if changed_credential.rowcount != 1:
                    raise ConfigurationError("credential_operation_conflict")
                updated_credential = replace(
                    credential,
                    status=credential_status,
                    version=credential.version + 1,
                )

            event_type = {
                "completed": "rotated" if credential.replaces_ref else "created",
                "failed": "write_failed",
                "unknown": "write_unknown",
            }[status]
            _insert_event(
                session,
                workspace_id=workspace_id,
                credential_id=credential.ref.credential_id,
                operation_id=operation_id,
                event_type=event_type,
            )
            return CredentialWrite(
                workspace_id=workspace_id,
                operation_id=operation_id,
                credential=updated_credential,
                status=status,
                failure_code=write_row.failure_code,
            )

    def create_env_credential(self, metadata: CredentialMetadata) -> None:
        if metadata.source != "env" or metadata.status != "active":
            raise ConfigurationError("invalid_configuration")
        try:
            with self._session_factory.begin() as session:
                session.execute(insert(CredentialRow).values(_credential_values(metadata)))
                _insert_event(
                    session,
                    workspace_id=metadata.ref.workspace_id,
                    credential_id=metadata.ref.credential_id,
                    event_type="created",
                )
        except IntegrityError:
            raise ConfigurationError("credential_already_exists") from None

    def revoke_credential(
        self,
        ref: CredentialRef,
        *,
        expected_version: int,
    ) -> CredentialMetadata:
        now = _now()
        with self._session_factory.begin() as session:
            row = session.scalar(
                select(CredentialRow)
                .where(
                    CredentialRow.workspace_id == ref.workspace_id,
                    CredentialRow.id == ref.credential_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConfigurationError("credential_not_found")
            if row.version != expected_version or row.status != "active":
                raise ConfigurationError("configuration_version_conflict")
            changed = session.execute(
                update(CredentialRow)
                .where(
                    CredentialRow.workspace_id == ref.workspace_id,
                    CredentialRow.id == ref.credential_id,
                    CredentialRow.version == expected_version,
                    CredentialRow.status == "active",
                )
                .values(
                    status="revoked",
                    version=CredentialRow.version + 1,
                    revoked_at=now,
                )
            )
            if changed.rowcount != 1:
                raise ConfigurationError("configuration_version_conflict")
            _insert_event(
                session,
                workspace_id=ref.workspace_id,
                credential_id=ref.credential_id,
                event_type="revoked",
            )
            return replace(
                _to_credential(row),
                status="revoked",
                version=expected_version + 1,
            )
