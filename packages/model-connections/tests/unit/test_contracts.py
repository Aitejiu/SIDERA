import json
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import SecretStr

from sidera_model_connections.contracts import (
    BindingPermission,
    ConnectionRef,
    ConnectionSummary,
    CredentialMetadata,
    CredentialRef,
    CredentialSummary,
    CredentialWrite,
    ModelConnectionRevision,
    ResolvedConnection,
    RuntimeAccessRequest,
)
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.ports import (
    BindingPermissionReader,
    ConnectionCatalog,
    ConnectionResolver,
    DenyAllBindingPermissionReader,
    SecretStore,
)
from tests.fakes import FakeBindingPermissionReader, FakeSecretStore, MemoryCatalog

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID(int=1)
OTHER_WORKSPACE_ID = UUID(int=2)
CONNECTION_ID = UUID(int=3)
CREDENTIAL_ID = UUID(int=4)


def make_connection(**overrides: object) -> ModelConnectionRevision:
    values: dict[str, object] = {
        "ref": ConnectionRef(WORKSPACE_ID, CONNECTION_ID, 1),
        "name": "Primary",
        "provider_id": "example_provider-1",
        "protocol": "openai_compatible",
        "base_url": "https://api.example.test/v1",
        "network_scope": "public",
        "allowed_roles": frozenset({"main_agent", "worker"}),
        "credential_ref": CredentialRef(WORKSPACE_ID, CREDENTIAL_ID),
        "created_at": NOW,
    }
    values.update(overrides)
    return ModelConnectionRevision(**values)  # type: ignore[arg-type]


def make_credential(**overrides: object) -> CredentialMetadata:
    values: dict[str, object] = {
        "ref": CredentialRef(WORKSPACE_ID, CREDENTIAL_ID),
        "alias": "Primary key",
        "source": "env",
        "status": "active",
        "env_name": "SIDERA_PRIMARY_KEY",
        "replaces_ref": None,
        "created_at": NOW,
        "version": 1,
    }
    values.update(overrides)
    return CredentialMetadata(**values)  # type: ignore[arg-type]


def test_connection_revision_is_immutable() -> None:
    connection = make_connection()

    with pytest.raises(FrozenInstanceError):
        connection.name = "Changed"  # type: ignore[misc]


@pytest.mark.parametrize("revision", [0, -1])
def test_connection_reference_requires_positive_revision(revision: int) -> None:
    with pytest.raises(ConfigurationError) as rejected:
        ConnectionRef(WORKSPACE_ID, CONNECTION_ID, revision)

    assert rejected.value.code == "invalid_configuration"


def test_connection_revision_requires_at_least_one_fixed_role() -> None:
    with pytest.raises(ConfigurationError):
        make_connection(allowed_roles=frozenset())


def test_connection_revision_rejects_unknown_role() -> None:
    with pytest.raises(ConfigurationError):
        make_connection(allowed_roles=frozenset({"operator"}))


def test_connection_revision_rejects_mutable_role_set() -> None:
    with pytest.raises(ConfigurationError):
        make_connection(allowed_roles={"worker"})


@pytest.mark.parametrize(
    "created_at",
    [
        datetime.fromisoformat("2026-09-30T12:00:00"),
        datetime(2026, 9, 30, 13, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_connection_revision_requires_aware_utc_time(created_at: datetime) -> None:
    with pytest.raises(ConfigurationError):
        make_connection(created_at=created_at)


@pytest.mark.parametrize("name", ["", "x" * 101, "bad\nname"])
def test_connection_revision_rejects_invalid_name(name: str) -> None:
    with pytest.raises(ConfigurationError):
        make_connection(name=name)


def test_connection_revision_accepts_name_at_maximum_length() -> None:
    assert len(make_connection(name="n" * 100).name) == 100


@pytest.mark.parametrize("provider_id", ["Upper", "1provider", "a" * 65, "bad.id"])
def test_connection_revision_rejects_invalid_provider_id(provider_id: str) -> None:
    with pytest.raises(ConfigurationError):
        make_connection(provider_id=provider_id)


def test_connection_revision_rejects_cross_workspace_credential(
    workspace_ids: tuple[UUID, UUID],
) -> None:
    workspace_id, other_workspace_id = workspace_ids
    ref = ConnectionRef(workspace_id, CONNECTION_ID, 1)

    with pytest.raises(ConfigurationError):
        make_connection(
            ref=ref,
            credential_ref=CredentialRef(other_workspace_id, CREDENTIAL_ID),
        )


def test_connection_revision_rejects_unknown_protocol_and_network_scope() -> None:
    with pytest.raises(ConfigurationError):
        make_connection(protocol="custom")
    with pytest.raises(ConfigurationError):
        make_connection(network_scope="private")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: make_connection(protocol=[]),
        lambda: make_connection(network_scope=[]),
        lambda: RuntimeAccessRequest(WORKSPACE_ID, [], UUID(int=17)),
        lambda: make_credential(source=[]),
        lambda: make_credential(status=[]),
    ],
)
def test_non_string_fixed_literals_raise_configuration_error(factory) -> None:
    with pytest.raises(ConfigurationError):
        factory()


def test_credential_metadata_requires_env_name_for_env_source() -> None:
    with pytest.raises(ConfigurationError):
        make_credential(env_name=None)


def test_credential_metadata_rejects_env_name_for_keychain_source() -> None:
    with pytest.raises(ConfigurationError):
        make_credential(source="macos_keychain")


@pytest.mark.parametrize("env_name", ["lowercase", "1INVALID", "HAS-DASH", "x" * 129])
def test_credential_metadata_rejects_invalid_env_name(env_name: str) -> None:
    with pytest.raises(ConfigurationError):
        make_credential(env_name=env_name)


def test_credential_metadata_requires_positive_version() -> None:
    with pytest.raises(ConfigurationError):
        make_credential(version=0)


def test_credential_metadata_rejects_invalid_alias() -> None:
    with pytest.raises(ConfigurationError):
        make_credential(alias="alias\x00")
    with pytest.raises(ConfigurationError):
        make_credential(alias="a" * 101)


def test_credential_rotation_must_stay_in_workspace_and_change_reference() -> None:
    with pytest.raises(ConfigurationError):
        make_credential(
            replaces_ref=CredentialRef(OTHER_WORKSPACE_ID, UUID(int=5))
        )
    with pytest.raises(ConfigurationError):
        make_credential(replaces_ref=CredentialRef(WORKSPACE_ID, CREDENTIAL_ID))


def test_credential_metadata_rejects_unknown_fixed_literals() -> None:
    with pytest.raises(ConfigurationError):
        make_credential(source="vault")
    with pytest.raises(ConfigurationError):
        make_credential(status="ready")


def test_credential_write_has_no_secret_field_and_fixed_status() -> None:
    credential = make_credential()
    write = CredentialWrite(
        workspace_id=WORKSPACE_ID,
        operation_id=UUID(int=5),
        credential=credential,
        status="pending",
        failure_code=None,
    )

    assert {item.name for item in fields(write)} == {
        "workspace_id",
        "operation_id",
        "credential",
        "status",
        "failure_code",
    }
    with_code = CredentialWrite(
        WORKSPACE_ID,
        UUID(int=18),
        credential,
        "failed",
        "credential_store_unavailable",
    )
    assert with_code.failure_code == "credential_store_unavailable"
    with pytest.raises(ConfigurationError):
        CredentialWrite(WORKSPACE_ID, UUID(int=5), credential, "done", None)


@pytest.mark.parametrize(
    "failure_code",
    ["unknown_failure", "cfg2-fake-main-key", "invalid_configuration\n"],
)
def test_credential_write_rejects_non_static_failure_code_without_echo(
    failure_code: str,
) -> None:
    with pytest.raises(ConfigurationError) as rejected:
        CredentialWrite(
            WORKSPACE_ID,
            UUID(int=19),
            make_credential(),
            "failed",
            failure_code,
        )

    assert rejected.value.code == "invalid_configuration"
    assert failure_code not in str(rejected.value)
    assert failure_code not in repr(rejected.value)


def test_binding_permission_uses_immutable_fixed_protocol_set() -> None:
    request = RuntimeAccessRequest(WORKSPACE_ID, "worker", UUID(int=6))
    permission = BindingPermission(
        request=request,
        connection_ref=ConnectionRef(WORKSPACE_ID, CONNECTION_ID, 1),
        allowed_protocols=frozenset({"openai_compatible"}),
        allowed_origins=frozenset({"https://api.example.test"}),
    )

    with pytest.raises(FrozenInstanceError):
        permission.request = request  # type: ignore[misc]
    with pytest.raises(ConfigurationError):
        BindingPermission(
            request,
            ConnectionRef(WORKSPACE_ID, CONNECTION_ID, 1),
            {"openai_compatible"},  # type: ignore[arg-type]
            frozenset(),
        )


def test_resolved_connection_repr_and_json_do_not_expose_secret() -> None:
    secret_value = "fake-secret-never-print-this"
    connection = make_connection()
    resolved = ResolvedConnection(
        connection,
        make_credential(),
        SecretStr(secret_value),
    )

    assert secret_value not in repr(resolved)
    assert "secret" not in repr(resolved).lower()
    with pytest.raises(TypeError):
        json.dumps(resolved)


def test_resolved_connection_requires_matching_credential_reference() -> None:
    with pytest.raises(ConfigurationError):
        ResolvedConnection(
            make_connection(),
            make_credential(ref=CredentialRef(WORKSPACE_ID, UUID(int=8))),
            SecretStr("fake-secret-value"),
        )


def test_public_summaries_are_distinct_secret_free_contracts() -> None:
    assert {item.name for item in fields(ConnectionSummary)} == {
        "workspace_id",
        "connection_id",
        "revision",
        "name",
        "provider_id",
        "protocol",
        "base_url",
        "network_scope",
        "allowed_roles",
        "credential_id",
        "availability",
    }
    assert {item.name for item in fields(CredentialSummary)} == {
        "workspace_id",
        "credential_id",
        "alias",
        "source",
        "status",
        "version",
        "replaces_id",
    }
    assert "secret" not in {item.name for item in fields(ConnectionSummary)}
    assert "secret" not in {item.name for item in fields(CredentialSummary)}
    assert ConnectionSummary is not ResolvedConnection
    assert CredentialSummary is not CredentialMetadata


def test_credential_summary_cannot_replace_itself() -> None:
    with pytest.raises(ConfigurationError):
        CredentialSummary(
            workspace_id=WORKSPACE_ID,
            credential_id=CREDENTIAL_ID,
            alias="Primary key",
            source="env",
            status="active",
            version=1,
            replaces_id=CREDENTIAL_ID,
        )


def test_connection_contract_does_not_contain_runtime_state_fields() -> None:
    assert tuple(item.name for item in fields(ModelConnectionRevision)) == (
        "ref",
        "name",
        "provider_id",
        "protocol",
        "base_url",
        "network_scope",
        "allowed_roles",
        "credential_ref",
        "created_at",
    )


def test_memory_catalog_uses_exact_reference_and_operation_keys() -> None:
    catalog = MemoryCatalog()
    connection = make_connection()
    credential = make_credential()
    write = CredentialWrite(
        WORKSPACE_ID,
        UUID(int=9),
        credential,
        "pending",
        None,
    )
    catalog.create_connection(connection)
    catalog.create_env_credential(credential)
    catalog.reserve_credential_write(write)

    assert catalog.get_connection(connection.ref) is connection
    assert catalog.get_credential(credential.ref) is credential
    assert catalog.get_credential_write(WORKSPACE_ID, write.operation_id) is write
    assert catalog.list_connections(WORKSPACE_ID) == (connection,)
    with pytest.raises(ConfigurationError):
        catalog.get_connection(ConnectionRef(WORKSPACE_ID, CONNECTION_ID, 2))
    with pytest.raises(ConfigurationError):
        catalog.get_credential_write(WORKSPACE_ID, UUID(int=10))


def test_port_interfaces_are_protocols() -> None:
    assert all(
        getattr(port, "_is_protocol", False)
        for port in (
            BindingPermissionReader,
            ConnectionCatalog,
            ConnectionResolver,
            SecretStore,
        )
    )


def test_deny_all_permission_reader_rejects_every_request() -> None:
    reader = DenyAllBindingPermissionReader()
    request = RuntimeAccessRequest(WORKSPACE_ID, "worker", UUID(int=16))

    with pytest.raises(ConfigurationError) as rejected:
        reader.authorize(request)

    assert rejected.value.code == "runtime_binding_denied"


def test_fake_secret_store_uses_exact_refs_and_secret_free_calls() -> None:
    store = FakeSecretStore()
    first_ref = CredentialRef(WORKSPACE_ID, UUID(int=11))
    second_ref = CredentialRef(WORKSPACE_ID, UUID(int=12))
    missing_ref = CredentialRef(WORKSPACE_ID, UUID(int=13))
    shared = SecretStr("cfg2-fake-main-key")
    store.put_new(first_ref, shared)
    store.put_new(second_ref, shared)

    with pytest.raises(ConfigurationError) as duplicate:
        store.put_new(first_ref, SecretStr("cfg2-fake-replacement-key"))

    assert duplicate.value.code == "credential_already_exists"
    assert store.read(first_ref).get_secret_value() == "cfg2-fake-main-key"
    assert store.read(second_ref).get_secret_value() == "cfg2-fake-main-key"
    with pytest.raises(ConfigurationError) as missing:
        store.read(missing_ref)
    assert missing.value.code == "credential_unresolved"
    store.delete_owned(second_ref)
    store.delete_owned(missing_ref)
    assert store.read(first_ref).get_secret_value() == "cfg2-fake-main-key"
    with pytest.raises(ConfigurationError):
        store.read(second_ref)
    assert store.calls == [
        ("put_new", first_ref),
        ("put_new", second_ref),
        ("put_new", first_ref),
        ("read", first_ref),
        ("read", second_ref),
        ("read", missing_ref),
        ("delete_owned", second_ref),
        ("delete_owned", missing_ref),
        ("read", first_ref),
        ("read", second_ref),
    ]
    assert "cfg2-fake-main-key" not in repr(store.calls)
    assert "cfg2-fake-replacement-key" not in repr(store.calls)


def test_fake_binding_permission_reader_uses_exact_request_key() -> None:
    request = RuntimeAccessRequest(WORKSPACE_ID, "worker", UUID(int=14))
    permission = BindingPermission(
        request,
        ConnectionRef(WORKSPACE_ID, CONNECTION_ID, 1),
        frozenset({"openai_compatible"}),
        frozenset({"https://api.example.test"}),
    )
    reader = FakeBindingPermissionReader({request: permission})

    assert reader.authorize(request) is permission
    assert reader.calls == [request]
    with pytest.raises(ConfigurationError):
        reader.authorize(RuntimeAccessRequest(WORKSPACE_ID, "worker", UUID(int=15)))
