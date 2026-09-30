from __future__ import annotations

import hmac
import io
import json
import subprocess
import sys
import traceback
from dataclasses import fields
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from sidera_model_connections.contracts import (
    BindingPermission,
    ConnectionRef,
    ConnectionSummary,
    CredentialMetadata,
    CredentialRef,
    CredentialSummary,
    ModelConnectionRevision,
    ResolvedConnection,
    RuntimeAccessRequest,
)
from sidera_model_connections.credentials import CredentialService
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.macos_keychain import MacOSKeychainSecretStore
from sidera_model_connections.resolution import ConnectionResolver
from tests.fakes import FakeBindingPermissionReader, FakeSecretStore, MemoryCatalog

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
ROLE_KEYS = (
    "cfg2-fake-main-key",
    "cfg2-fake-worker-key",
    "cfg2-fake-planner-key",
)


def _assert_keys_absent(keys: tuple[str, ...], rendered: str, surface: str) -> None:
    if any(key in rendered for key in keys):
        pytest.fail(f"{surface} exposed a synthetic credential", pytrace=False)


def _runtime_objects(role: str, index: int):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    credential = CredentialMetadata(
        ref=CredentialRef(WORKSPACE_ID, UUID(int=500 + index)),
        alias=f"{role} credential",
        source="macos_keychain",
        status="active",
        env_name=None,
        replaces_ref=None,
        created_at=now,
        version=1,
    )
    connection = ModelConnectionRevision(
        ref=ConnectionRef(WORKSPACE_ID, UUID(int=600 + index), 1),
        name=f"{role} connection",
        provider_id="example",
        protocol="openai_compatible",
        base_url="https://api.example.test/v1",
        network_scope="public",
        allowed_roles=frozenset({role}),
        credential_ref=credential.ref,
        created_at=now,
    )
    return connection, credential


def test_role_secrets_are_memory_only_and_public_summaries_are_secret_free() -> None:
    rendered: list[str] = []
    summary_fields = {field.name for field in fields(ConnectionSummary)}
    credential_fields = {field.name for field in fields(CredentialSummary)}
    assert "secret" not in summary_fields
    assert "secret" not in credential_fields

    for index, (role, key) in enumerate(
        zip(("main_agent", "worker", "planner"), ROLE_KEYS, strict=True),
        start=1,
    ):
        connection, credential = _runtime_objects(role, index)
        resolved = ResolvedConnection(connection, credential, SecretStr(key))
        connection_summary = ConnectionSummary(
            workspace_id=connection.ref.workspace_id,
            connection_id=connection.ref.connection_id,
            revision=connection.ref.revision,
            name=connection.name,
            provider_id=connection.provider_id,
            protocol=connection.protocol,
            base_url=connection.base_url,
            network_scope=connection.network_scope,
            allowed_roles=connection.allowed_roles,
            credential_id=credential.ref.credential_id,
            availability="configured",
        )
        credential_summary = CredentialSummary(
            workspace_id=credential.ref.workspace_id,
            credential_id=credential.ref.credential_id,
            alias=credential.alias,
            source=credential.source,
            status=credential.status,
            version=credential.version,
            replaces_id=None,
        )
        rendered.extend(
            (
                repr(resolved),
                repr(connection_summary),
                repr(credential_summary),
            )
        )
        with pytest.raises(TypeError):
            json.dumps(resolved)

    _assert_keys_absent(ROLE_KEYS, "\n".join(rendered), "runtime repr or DTO")


def test_credential_results_and_memory_events_contain_metadata_only() -> None:
    catalog = MemoryCatalog()
    store = FakeSecretStore()
    service = CredentialService(catalog, store)
    writes = [
        service.save_managed(
            WORKSPACE_ID,
            uuid4(),
            alias,
            SecretStr(key),
        )
        for alias, key in zip(
            ("main_agent", "worker", "planner"), ROLE_KEYS, strict=True
        )
    ]
    surfaces = "\n".join((repr(writes), repr(catalog.events), repr(store.calls)))

    assert [event[0] for event in catalog.events] == ["created"] * 3
    _assert_keys_absent(ROLE_KEYS, surfaces, "credential result, event, or fake call")


def test_untrusted_store_error_is_fixed_in_exception_and_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection, credential = _runtime_objects("main_agent", 1)
    request = RuntimeAccessRequest(WORKSPACE_ID, "main_agent", uuid4())
    permission = BindingPermission(
        request,
        connection.ref,
        frozenset({"openai_compatible"}),
        frozenset({"https://api.example.test"}),
    )
    catalog = MemoryCatalog()
    catalog.credentials[credential.ref] = credential
    catalog.create_connection(connection)

    class MaliciousStore(FakeSecretStore):
        def read(self, ref: CredentialRef) -> SecretStr:
            self.calls.append(("read", ref))
            raise RuntimeError(f"untrusted SDK/OS detail: {'|'.join(ROLE_KEYS)}")

    store = MaliciousStore()
    resolver = ConnectionResolver(
        catalog,
        FakeBindingPermissionReader({request: permission}),
        store,
        {},
    )

    with pytest.raises(ConfigurationError) as rejected:
        resolver.resolve(request)

    error = rejected.value
    rendered_traceback = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert error.code == "credential_store_unavailable"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert store.calls == [("read", credential.ref)]
    _assert_keys_absent(
        ROLE_KEYS,
        "\n".join((str(error), repr(error), rendered_traceback, caplog.text)),
        "exception, traceback, or log",
    )


def test_fake_security_error_does_not_reach_helper_stdout_or_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sidera_model_connections import keychain_helper

    class MaliciousSecurity:
        kSecUseAuthenticationUI = "authentication-ui"
        kSecUseAuthenticationUIFail = "fail"
        kSecClass = "class"
        kSecClassGenericPassword = "generic-password"
        kSecAttrService = "service"
        kSecAttrAccount = "account"
        kSecValueData = "value-data"
        errSecSuccess = 0

        def __init__(self) -> None:
            self.item_recorded = False

        def SecItemAdd(self, query: dict, result: object) -> int:
            self.item_recorded = query[self.kSecValueData] == ROLE_KEYS[0].encode()
            raise RuntimeError(f"private Security detail: {'|'.join(ROLE_KEYS)}")

    security = MaliciousSecurity()
    payload = {
        "operation": "put_new",
        "namespace": str(uuid4()),
        "workspace_id": str(uuid4()),
        "credential_id": str(uuid4()),
        "secret": ROLE_KEYS[0],
    }
    monkeypatch.setattr(keychain_helper, "_load_security", lambda: security)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = keychain_helper.main()
    captured = capsys.readouterr()
    try:
        response = json.loads(captured.out)
    except (TypeError, ValueError):
        pytest.fail("helper returned invalid protocol JSON", pytrace=False)

    assert security.item_recorded is True
    assert exit_code == 1
    assert response == {"ok": False, "code": "credential_write_unknown"}
    assert captured.err == ""
    _assert_keys_absent(ROLE_KEYS, captured.out + captured.err, "helper output")


def test_keychain_child_argv_and_environment_never_contain_role_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    requests: list[str] = []

    class SuccessfulChild:
        returncode = 0

        def communicate(self, input: str | None = None, timeout: float | None = None):
            requests.append(input or "")
            return '{"ok":true}', None

    def fake_popen(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return SuccessfulChild()

    from sidera_model_connections import macos_keychain

    monkeypatch.setattr(macos_keychain, "Popen", fake_popen)
    store = MacOSKeychainSecretStore(uuid4())
    for key in ROLE_KEYS:
        store.put_new(CredentialRef(WORKSPACE_ID, uuid4()), SecretStr(key))

    try:
        payloads = [json.loads(request) for request in requests]
    except (TypeError, ValueError):
        pytest.fail("child request was not valid JSON", pytrace=False)
    assert len(calls) == len(ROLE_KEYS)
    assert len(payloads) == len(ROLE_KEYS)
    for key, (argv, kwargs), payload in zip(ROLE_KEYS, calls, payloads, strict=True):
        _assert_keys_absent(ROLE_KEYS, repr(argv), "helper argv")
        _assert_keys_absent(ROLE_KEYS, repr(kwargs.get("env")), "helper environment")
        assert kwargs.get("stderr") is subprocess.DEVNULL
        assert hmac.compare_digest(payload.get("secret", ""), key) is True


def test_changed_namespace_missing_item_stays_an_exact_unresolved_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class MissingItemChild:
        returncode = 1

        def communicate(self, input: str | None = None, timeout: float | None = None):
            calls.append(((), {"request": input or ""}))
            return '{"ok":false,"code":"credential_unresolved"}', None

    from sidera_model_connections import macos_keychain

    def fake_popen(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return MissingItemChild()

    monkeypatch.setattr(macos_keychain, "Popen", fake_popen)
    namespace = uuid4()
    ref = CredentialRef(WORKSPACE_ID, uuid4())
    store = MacOSKeychainSecretStore(namespace)

    with pytest.raises(ConfigurationError) as unresolved:
        store.read(ref)

    assert unresolved.value.code == "credential_unresolved"
    assert len(calls) == 2
    request = calls[1][1]["request"]
    try:
        payload = json.loads(request)
    except (TypeError, ValueError):
        pytest.fail("child request was not valid JSON", pytrace=False)
    assert payload == {
        "operation": "read",
        "namespace": str(namespace),
        "workspace_id": str(ref.workspace_id),
        "credential_id": str(ref.credential_id),
    }
