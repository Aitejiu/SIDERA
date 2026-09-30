from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sidera_model_connections.contracts import (
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    CredentialWrite,
    ModelConnectionRevision,
)
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.postgres import PostgresConnectionCatalog
from sidera_model_connections.postgres.models import CredentialEventRow


def test_append_preserves_old_revision(
    catalog: PostgresConnectionCatalog,
    connection_v1: ModelConnectionRevision,
    connection_v2: ModelConnectionRevision,
) -> None:
    catalog.create_connection(connection_v1)
    catalog.append_connection(connection_v2, expected_revision=1)
    assert catalog.get_connection(connection_v1.ref) == connection_v1
    assert catalog.get_connection(connection_v2.ref) == connection_v2
    with pytest.raises(ConfigurationError) as conflict:
        catalog.append_connection(connection_v2, expected_revision=1)
    assert conflict.value.code == "configuration_version_conflict"
    assert catalog.get_connection(connection_v2.ref) == connection_v2


def test_catalog_reads_and_lists_are_workspace_scoped(
    catalog: PostgresConnectionCatalog,
    connection_v1: ModelConnectionRevision,
    credential_metadata: CredentialMetadata,
) -> None:
    from dataclasses import replace

    catalog.create_connection(connection_v1)
    other_workspace = uuid4()
    other_credential = replace(
        credential_metadata,
        ref=CredentialRef(other_workspace, uuid4()),
    )
    other_connection = replace(
        connection_v1,
        ref=ConnectionRef(other_workspace, connection_v1.ref.connection_id, 1),
        credential_ref=other_credential.ref,
    )
    catalog.create_env_credential(other_credential)
    catalog.create_connection(other_connection)

    assert catalog.get_connection(connection_v1.ref) == connection_v1
    assert catalog.get_connection(other_connection.ref) == other_connection
    assert catalog.list_connections(connection_v1.ref.workspace_id) == (connection_v1,)
    assert catalog.list_connections(other_workspace) == (other_connection,)
    with pytest.raises(ConfigurationError) as missing:
        catalog.get_connection(
            ConnectionRef(other_workspace, connection_v1.ref.connection_id, 2)
        )
    assert missing.value.code == "configuration_not_found"


def test_parallel_edits_have_one_cas_winner_and_keep_the_old_revision(
    catalog: PostgresConnectionCatalog,
    connection_v1: ModelConnectionRevision,
    connection_v2: ModelConnectionRevision,
) -> None:
    from dataclasses import replace

    catalog.create_connection(connection_v1)
    candidates = (
        connection_v2,
        replace(connection_v2, name="parallel editor"),
    )
    barrier = Barrier(2)

    def append(candidate: ModelConnectionRevision) -> str:
        barrier.wait(timeout=10)
        try:
            catalog.append_connection(candidate, expected_revision=1)
        except ConfigurationError as error:
            return error.code
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(append, candidates))

    assert outcomes.count("success") == 1
    assert outcomes.count("configuration_version_conflict") == 1
    assert catalog.get_connection(connection_v1.ref) == connection_v1
    head = catalog.list_connections(connection_v1.ref.workspace_id)
    assert len(head) == 1
    assert head[0].ref.revision == 2
    assert head[0] in candidates


def test_reserve_finish_and_repeats_emit_only_the_first_transition_event(
    catalog: PostgresConnectionCatalog,
    credential_metadata: CredentialMetadata,
    session_factory: sessionmaker[Session],
) -> None:
    operation_id = uuid4()
    pending_credential = CredentialMetadata(
        ref=CredentialRef(credential_metadata.ref.workspace_id, uuid4()),
        alias="managed integration credential",
        source="macos_keychain",
        status="pending",
        env_name=None,
        replaces_ref=None,
        created_at=datetime.now(UTC),
        version=1,
    )
    write = CredentialWrite(
        workspace_id=credential_metadata.ref.workspace_id,
        operation_id=operation_id,
        credential=pending_credential,
        status="pending",
        failure_code=None,
    )
    reserved = catalog.reserve_credential_write(write)
    assert reserved.status == "pending"

    retry = CredentialWrite(
        workspace_id=write.workspace_id,
        operation_id=operation_id,
        credential=CredentialMetadata(
            ref=CredentialRef(write.workspace_id, uuid4()),
            alias=pending_credential.alias,
            source=pending_credential.source,
            status="pending",
            env_name=None,
            replaces_ref=None,
            created_at=datetime.now(UTC),
            version=1,
        ),
        status="pending",
        failure_code=None,
    )
    assert catalog.reserve_credential_write(retry) == reserved
    completed = catalog.finish_credential_write(
        write.workspace_id, operation_id, status="completed"
    )
    assert completed.status == "completed"
    assert completed.credential == catalog.get_credential(reserved.credential.ref)
    assert catalog.finish_credential_write(
        write.workspace_id, operation_id, status="completed"
    ).status == "completed"
    assert catalog.get_credential(reserved.credential.ref).status == "active"

    with session_factory() as session:
        events = session.scalars(
            select(CredentialEventRow.type).where(
                CredentialEventRow.workspace_id == write.workspace_id,
                CredentialEventRow.credential_id == reserved.credential.ref.credential_id,
            )
        ).all()
    assert events == ["created"]


def test_unknown_and_failed_transitions_are_idempotent_and_secret_free(
    catalog: PostgresConnectionCatalog,
    credential_metadata: CredentialMetadata,
    session_factory: sessionmaker[Session],
) -> None:
    operation_id = uuid4()
    pending = CredentialMetadata(
        ref=CredentialRef(credential_metadata.ref.workspace_id, uuid4()),
        alias="unknown integration credential",
        source="macos_keychain",
        status="pending",
        env_name=None,
        replaces_ref=None,
        created_at=datetime.now(UTC),
        version=1,
    )
    write = CredentialWrite(
        workspace_id=pending.ref.workspace_id,
        operation_id=operation_id,
        credential=pending,
        status="pending",
        failure_code=None,
    )
    catalog.reserve_credential_write(write)
    with session_factory() as session:
        assert session.scalar(
            select(CredentialEventRow.id).where(
                CredentialEventRow.credential_id == pending.ref.credential_id
            )
        ) is None

    for status in ("unknown", "unknown", "failed", "failed"):
        catalog.finish_credential_write(
            write.workspace_id,
            operation_id,
            status=status,
        )

    assert catalog.get_credential(pending.ref).status == "failed"
    with session_factory() as session:
        events = session.scalars(
            select(CredentialEventRow.type).where(
                CredentialEventRow.workspace_id == write.workspace_id,
                CredentialEventRow.credential_id == pending.ref.credential_id,
            )
        ).all()
    assert sorted(events) == ["write_failed", "write_unknown"]


def test_env_registration_and_revoke_emit_single_events(
    catalog: PostgresConnectionCatalog,
    credential_metadata: CredentialMetadata,
    session_factory: sessionmaker[Session],
) -> None:
    revoked = catalog.revoke_credential(
        credential_metadata.ref,
        expected_version=credential_metadata.version,
    )
    assert revoked.status == "revoked"
    assert revoked.version == credential_metadata.version + 1
    assert revoked == catalog.get_credential(credential_metadata.ref)
    with pytest.raises(ConfigurationError) as conflict:
        catalog.revoke_credential(
            credential_metadata.ref,
            expected_version=revoked.version,
        )
    assert conflict.value.code == "configuration_version_conflict"
    with session_factory() as session:
        events = session.scalars(
            select(CredentialEventRow.type).where(
                CredentialEventRow.workspace_id == credential_metadata.ref.workspace_id,
                CredentialEventRow.credential_id == credential_metadata.ref.credential_id,
            )
        ).all()
    assert events == ["created", "revoked"]
