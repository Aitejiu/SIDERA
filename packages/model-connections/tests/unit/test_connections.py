from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_model_connections.connections import ConnectionService
from sidera_model_connections.contracts import (
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    ModelConnectionRevision,
)
from sidera_model_connections.errors import ConfigurationError
from tests.fakes import MemoryCatalog

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
CONNECTION_ID = UUID("20000000-0000-0000-0000-000000000001")
CREDENTIAL_ID = UUID("30000000-0000-0000-0000-000000000001")


def _service(catalog: MemoryCatalog):
    return ConnectionService(catalog)


def _credential(*, status: str = "active") -> CredentialMetadata:
    return CredentialMetadata(
        ref=CredentialRef(WORKSPACE_ID, CREDENTIAL_ID),
        alias="deployment key",
        source="env",
        status=status,
        env_name="SIDERA_MODEL_KEY",
        replaces_ref=None,
        created_at=datetime.now(UTC),
        version=1,
    )


def _connection(
    *,
    revision: int = 1,
    base_url: str = "HTTPS://API.EXAMPLE.TEST:443/v1",
    allowed_roles: frozenset[str] = frozenset({"main_agent"}),
) -> ModelConnectionRevision:
    return ModelConnectionRevision(
        ref=ConnectionRef(WORKSPACE_ID, CONNECTION_ID, revision),
        name="primary model",
        provider_id="example",
        protocol="openai_compatible",
        base_url=base_url,
        network_scope="public",
        allowed_roles=allowed_roles,
        credential_ref=CredentialRef(WORKSPACE_ID, CREDENTIAL_ID),
        created_at=datetime.now(UTC),
    )


def test_create_canonicalizes_and_stores_valid_revision() -> None:
    catalog = MemoryCatalog()
    catalog.credentials[_credential().ref] = _credential()
    candidate = _connection()

    saved = _service(catalog).create(candidate)

    assert saved == replace(candidate, base_url="https://api.example.test/v1")
    assert catalog.get_connection(candidate.ref) == saved


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("pending", "credential_unresolved"),
        ("failed", "credential_unresolved"),
        ("revoked", "credential_revoked"),
    ],
)
def test_create_rejects_non_active_credential_before_catalog_write(
    status: str,
    code: str,
) -> None:
    catalog = MemoryCatalog()
    credential = _credential(status=status)
    catalog.credentials[credential.ref] = credential

    with pytest.raises(ConfigurationError) as rejected:
        _service(catalog).create(_connection())

    assert rejected.value.code == code
    assert catalog.connections == {}


def test_create_rejects_invalid_endpoint_without_persisting() -> None:
    catalog = MemoryCatalog()
    credential = _credential()
    catalog.credentials[credential.ref] = credential

    with pytest.raises(ConfigurationError) as rejected:
        _service(catalog).create(
            _connection(base_url="https://user:fake-pass@api.example.test/v1")
        )

    assert rejected.value.code == "invalid_configuration"
    assert catalog.connections == {}


def test_revise_appends_new_revision_and_preserves_old_binding_target() -> None:
    catalog = MemoryCatalog()
    credential = _credential()
    catalog.credentials[credential.ref] = credential
    service = _service(catalog)
    original = service.create(_connection())
    revised_candidate = replace(
        original,
        ref=ConnectionRef(WORKSPACE_ID, CONNECTION_ID, 2),
        name="shared model",
        allowed_roles=frozenset({"main_agent", "worker"}),
    )

    revised = service.revise(revised_candidate, expected_revision=1)

    assert revised == revised_candidate
    assert catalog.get_connection(original.ref) == original
    assert catalog.get_connection(revised.ref) == revised


def test_revise_rejects_stale_expected_revision_without_appending() -> None:
    catalog = MemoryCatalog()
    credential = _credential()
    catalog.credentials[credential.ref] = credential
    service = _service(catalog)
    original = service.create(_connection())
    candidate = replace(original, ref=ConnectionRef(WORKSPACE_ID, CONNECTION_ID, 2))

    with pytest.raises(ConfigurationError) as rejected:
        service.revise(candidate, expected_revision=9)

    assert rejected.value.code == "configuration_version_conflict"
    assert catalog.connections == {original.ref: original}
