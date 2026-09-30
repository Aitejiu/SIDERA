from __future__ import annotations

import importlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sidera_model_connections.connections import ConnectionService
from sidera_model_connections.contracts import (
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    CredentialWrite,
    ModelConnectionRevision,
)
from sidera_model_connections.credentials import CredentialService
from sidera_model_connections.errors import ConfigurationError
from tests.fakes import FakeSecretStore, MemoryCatalog

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000002")
CONNECTION_ID = UUID("20000000-0000-0000-0000-000000000001")
OPERATION_ID = UUID("30000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BASE_URL = "http://127.0.0.1"
ALLOWED_ORIGIN = "http://127.0.0.1:3000"
MANAGEMENT_TOKEN = "cfg2-fake-management-token"
SYNTHETIC_SECRET = "cfg2-fake-provider-key-never-return-this"
AUTH_HEADERS = {
    "Authorization": f"Bearer {MANAGEMENT_TOKEN}",
    "Origin": ALLOWED_ORIGIN,
}


@pytest.fixture(scope="module")
def http_modules() -> Any:
    importlib.import_module("sidera_model_connections.http")
    return type(
        "HttpModules",
        (),
        {
            "auth": importlib.import_module("sidera_model_connections.http.auth"),
            "router": importlib.import_module("sidera_model_connections.http.router"),
        },
    )


class RecordingCatalog:
    _METHODS = frozenset(
        {
            "get_connection",
            "get_credential",
            "list_connections",
            "create_connection",
            "append_connection",
            "reserve_credential_write",
            "get_credential_write",
            "finish_credential_write",
            "create_env_credential",
            "revoke_credential",
        }
    )

    def __init__(self, catalog: MemoryCatalog) -> None:
        self.catalog = catalog
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.catalog, name)
        if name not in self._METHODS or not callable(value):
            return value

        def record(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return value(*args, **kwargs)

        return record


class ConnectionServiceSpy:
    def __init__(self, service: ConnectionService) -> None:
        self.service = service
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def create(self, connection: ModelConnectionRevision) -> ModelConnectionRevision:
        self.calls.append(("create", (connection.ref,)))
        return self.service.create(connection)

    def revise(
        self,
        connection: ModelConnectionRevision,
        *,
        expected_revision: int,
    ) -> ModelConnectionRevision:
        self.calls.append(("revise", (connection.ref, expected_revision)))
        return self.service.revise(connection, expected_revision=expected_revision)


class CredentialServiceSpy:
    def __init__(self, service: CredentialService) -> None:
        self.service = service
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def save_managed(
        self,
        workspace_id: UUID,
        operation_id: UUID,
        alias: str,
        value: SecretStr,
        *,
        replaces_ref: CredentialRef | None = None,
    ) -> Any:
        self.calls.append(
            ("save_managed", (workspace_id, operation_id, alias, replaces_ref))
        )
        return self.service.save_managed(
            workspace_id,
            operation_id,
            alias,
            value,
            replaces_ref=replaces_ref,
        )

    def recover_write(self, workspace_id: UUID, operation_id: UUID) -> Any:
        self.calls.append(("recover_write", (workspace_id, operation_id)))
        return self.service.recover_write(workspace_id, operation_id)

    def register_env(self, workspace_id: UUID, alias: str, env_name: str) -> Any:
        self.calls.append(("register_env", (workspace_id, alias, env_name)))
        return self.service.register_env(workspace_id, alias, env_name)

    def revoke(
        self,
        ref: CredentialRef,
        *,
        expected_version: int,
    ) -> Any:
        self.calls.append(("revoke", (ref, expected_version)))
        return self.service.revoke(ref, expected_version=expected_version)


@dataclass
class Harness:
    client: TestClient
    catalog: RecordingCatalog
    memory_catalog: MemoryCatalog
    store: FakeSecretStore
    connection_service: ConnectionServiceSpy
    credential_service: CredentialServiceSpy
    real_connection_service: ConnectionService
    real_credential_service: CredentialService

    def reset_calls(self) -> None:
        self.catalog.calls.clear()
        self.store.calls.clear()
        self.connection_service.calls.clear()
        self.credential_service.calls.clear()


@contextmanager
def _make_harness(
    http_modules: Any,
    *,
    store: FakeSecretStore | None = None,
    workspace_ids: frozenset[UUID] | None = None,
) -> Iterator[Harness]:
    memory_catalog = MemoryCatalog()
    catalog = RecordingCatalog(memory_catalog)
    secret_store = store or FakeSecretStore()
    real_connection_service = ConnectionService(catalog)
    real_credential_service = CredentialService(catalog, secret_store)
    connection_service = ConnectionServiceSpy(real_connection_service)
    credential_service = CredentialServiceSpy(real_credential_service)
    principal = http_modules.auth.ManagementPrincipal(
        "task-6-test-principal",
        frozenset({WORKSPACE_ID}) if workspace_ids is None else workspace_ids,
    )
    authorizer = http_modules.auth.LoopbackManagementAuthorizer(
        SecretStr(MANAGEMENT_TOKEN), ALLOWED_ORIGIN, principal
    )
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=False,
    )
    app.include_router(
        http_modules.router.create_management_router(
            connection_service,
            credential_service,
            catalog,
            authorize_management=authorizer,
        )
    )
    with TestClient(app, base_url=BASE_URL) as client:
        yield Harness(
            client,
            catalog,
            memory_catalog,
            secret_store,
            connection_service,
            credential_service,
            real_connection_service,
            real_credential_service,
        )


def _connection_body(credential_id: UUID, *, name: str = "Main") -> dict[str, Any]:
    return {
        "name": name,
        "provider_id": "example_provider",
        "protocol": "openai_compatible",
        "base_url": "https://api.example.test/v1",
        "network_scope": "public",
        "allowed_roles": ["main_agent", "worker"],
        "credential_id": str(credential_id),
    }


def _make_credential(
    credential_id: UUID,
    *,
    status: str = "active",
    workspace_id: UUID = WORKSPACE_ID,
    source: str = "macos_keychain",
    version: int = 1,
) -> CredentialMetadata:
    return CredentialMetadata(
        CredentialRef(workspace_id, credential_id),
        "Test credential",
        source,  # type: ignore[arg-type]
        status,  # type: ignore[arg-type]
        "SIDERA_TEST_KEY" if source == "env" else None,
        None,
        NOW,
        version,
    )


def _make_connection(
    connection_id: UUID,
    credential: CredentialMetadata,
    *,
    revision: int = 1,
    name: str = "Main",
) -> ModelConnectionRevision:
    return ModelConnectionRevision(
        ConnectionRef(credential.ref.workspace_id, connection_id, revision),
        name,
        "example_provider",
        "openai_compatible",
        "https://api.example.test/v1",
        "public",
        frozenset({"main_agent", "worker"}),
        credential.ref,
        NOW,
    )


def _seed_active_credential(harness: Harness) -> CredentialMetadata:
    write = harness.real_credential_service.save_managed(
        WORKSPACE_ID,
        uuid4(),
        "primary",
        SecretStr(SYNTHETIC_SECRET),
    )
    harness.reset_calls()
    return write.credential


def _assert_no_store_calls(harness: Harness) -> None:
    assert harness.store.calls == []


def test_management_principal_is_frozen_and_router_requires_authorizer(
    http_modules: Any,
) -> None:
    principal = http_modules.auth.ManagementPrincipal(
        "operator", frozenset({WORKSPACE_ID})
    )

    with pytest.raises(FrozenInstanceError):
        principal.id = "changed"
    with pytest.raises(TypeError):
        http_modules.router.create_management_router(None, None, None)
    with pytest.raises(TypeError):
        http_modules.router.create_management_router(
            None, None, None, authorize_management=None
        )


def test_router_contains_only_the_nine_fixed_management_routes(
    http_modules: Any,
) -> None:
    principal = http_modules.auth.ManagementPrincipal(
        "operator", frozenset({WORKSPACE_ID})
    )
    authorizer = http_modules.auth.LoopbackManagementAuthorizer(
        SecretStr(MANAGEMENT_TOKEN), ALLOWED_ORIGIN, principal
    )
    router = http_modules.router.create_management_router(
        None,
        None,
        MemoryCatalog(),
        authorize_management=authorizer,
    )
    actual = {
        (method, route.path)
        for route in router.routes
        for method in route.methods or ()
        if method in {"GET", "POST"}
    }

    assert actual == {
        ("GET", "/model-config/v1/workspaces/{workspace_id}/connections"),
        (
            "GET",
            "/model-config/v1/workspaces/{workspace_id}/connections/{id}/revisions/{revision}",
        ),
        ("POST", "/model-config/v1/workspaces/{workspace_id}/connections"),
        (
            "POST",
            "/model-config/v1/workspaces/{workspace_id}/connections/{id}/revisions",
        ),
        ("POST", "/model-config/v1/workspaces/{workspace_id}/credential-writes"),
        (
            "GET",
            "/model-config/v1/workspaces/{workspace_id}/credential-writes/{operation_id}",
        ),
        (
            "POST",
            "/model-config/v1/workspaces/{workspace_id}/credential-writes/{operation_id}/recover",
        ),
        ("POST", "/model-config/v1/workspaces/{workspace_id}/env-credentials"),
        (
            "POST",
            "/model-config/v1/workspaces/{workspace_id}/credentials/{id}/revoke",
        ),
    }
    assert MANAGEMENT_TOKEN not in repr(authorizer)


@pytest.mark.parametrize("method", ["get", "post"])
def test_unauthorized_read_and_write_do_not_call_services_or_catalog(
    http_modules: Any,
    method: str,
) -> None:
    with _make_harness(http_modules) as harness:
        if method == "get":
            response = harness.client.get(
                f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections",
                headers={"Origin": ALLOWED_ORIGIN},
            )
        else:
            response = harness.client.post(
                f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections",
                json=_connection_body(uuid4()),
                headers=AUTH_HEADERS | {"Authorization": "Bearer wrong-token"},
            )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "management_unauthorized"
        assert response.headers["cache-control"] == "no-store"
        assert harness.catalog.calls == []
        assert harness.connection_service.calls == []
        assert harness.credential_service.calls == []


def test_workspace_scope_denial_precedes_catalog_and_service_calls(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        response = harness.client.post(
            f"/model-config/v1/workspaces/{OTHER_WORKSPACE_ID}/connections",
            json=_connection_body(uuid4()),
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "management_scope_denied"
        assert harness.catalog.calls == []
        assert harness.connection_service.calls == []
        assert harness.credential_service.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        AUTH_HEADERS | {"Origin": "null"},
        AUTH_HEADERS | {"Origin": "https://attacker.example"},
        AUTH_HEADERS | {"Host": "attacker.example"},
        AUTH_HEADERS | {"Host": "localhost"},
    ],
    ids=["null-origin", "foreign-origin", "foreign-host", "hostname-not-ip"],
)
def test_loopback_authorizer_rejects_untrusted_origin_and_host(
    http_modules: Any,
    headers: dict[str, str],
) -> None:
    with _make_harness(http_modules) as harness:
        response = harness.client.get(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections",
            headers=headers,
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "management_unauthorized"
        assert harness.catalog.calls == []


def test_valid_bearer_allows_cli_request_without_origin(http_modules: Any) -> None:
    with _make_harness(http_modules) as harness:
        response = harness.client.get(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections",
            headers={"Authorization": f"Bearer {MANAGEMENT_TOKEN}"},
        )

        assert response.status_code == 200
        assert response.json() == []
        assert response.headers["cache-control"] == "no-store"
        assert [name for name, _args, _kwargs in harness.catalog.calls] == [
            "list_connections"
        ]


def test_list_projects_metadata_without_reading_secrets(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        credential = _seed_active_credential(harness)
        harness.memory_catalog.create_connection(
            _make_connection(CONNECTION_ID, credential)
        )
        harness.reset_calls()

        response = harness.client.get(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections",
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 200
        assert response.json()[0]["availability"] == "configured"
        assert response.json()[0]["credential_id"] == str(credential.ref.credential_id)
        assert response.headers["cache-control"] == "no-store"
        _assert_no_store_calls(harness)


def test_list_uses_fixed_availability_mapping_for_latest_metadata(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        expected = {
            "active": "configured",
            "pending": "credential_unresolved",
            "failed": "credential_unresolved",
            "revoked": "credential_revoked",
        }
        expected_by_credential: dict[str, str] = {}
        for index, (status, availability) in enumerate(expected.items(), start=1):
            credential = _make_credential(UUID(int=100 + index), status=status)
            expected_by_credential[str(credential.ref.credential_id)] = availability
            harness.memory_catalog.credentials[credential.ref] = credential
            connection = _make_connection(UUID(int=200 + index), credential)
            harness.memory_catalog.create_connection(connection)

        response = harness.client.get(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections",
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 200
        assert {
            item["credential_id"]: item["availability"] for item in response.json()
        } == expected_by_credential
        _assert_no_store_calls(harness)


def test_detail_returns_the_requested_revision_without_secret_reads(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        credential = _seed_active_credential(harness)
        old = _make_connection(CONNECTION_ID, credential, name="Old revision")
        latest = _make_connection(
            CONNECTION_ID, credential, revision=2, name="Current revision"
        )
        harness.memory_catalog.create_connection(old)
        harness.memory_catalog.append_connection(latest, expected_revision=1)
        harness.reset_calls()

        response = harness.client.get(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections/{CONNECTION_ID}/revisions/1",
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Old revision"
        assert response.json()["revision"] == 1
        assert harness.catalog.calls[0][0] == "get_connection"
        assert harness.catalog.calls[0][1][0] == old.ref
        _assert_no_store_calls(harness)


def test_connection_create_and_revision_use_services_and_cas(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        credential = _seed_active_credential(harness)
        created = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections",
            json=_connection_body(credential.ref.credential_id),
            headers=AUTH_HEADERS,
        )
        connection_id = created.json()["connection_id"]
        revision_body = _connection_body(
            credential.ref.credential_id, name="Revised"
        ) | {"expected_revision": 1}
        revised = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections/{connection_id}/revisions",
            json=revision_body,
            headers=AUTH_HEADERS,
        )

        assert created.status_code == 201
        assert revised.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        assert revised.headers["cache-control"] == "no-store"
        assert revised.json()["revision"] == 2
        assert revised.json()["name"] == "Revised"
        assert [name for name, _args in harness.connection_service.calls] == [
            "create",
            "revise",
        ]


def test_stale_connection_revision_maps_to_cas_conflict(http_modules: Any) -> None:
    with _make_harness(http_modules) as harness:
        credential = _seed_active_credential(harness)
        harness.memory_catalog.create_connection(
            _make_connection(CONNECTION_ID, credential)
        )
        body = _connection_body(credential.ref.credential_id) | {
            "expected_revision": 2
        }

        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections/{CONNECTION_ID}/revisions",
            json=body,
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "configuration_version_conflict"
        assert [name for name, _args in harness.connection_service.calls] == [
            "revise"
        ]


def test_invalid_body_uses_safe_validation_error_without_echo_or_log(
    http_modules: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _make_harness(http_modules) as harness:
        body = _connection_body(uuid4()) | {
            "provider_key": SYNTHETIC_SECRET,
        }

        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections",
            json=body,
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_configuration"
        assert SYNTHETIC_SECRET not in response.text
        assert "input" not in response.text.lower()
        assert SYNTHETIC_SECRET not in caplog.text
        assert response.headers["cache-control"] == "no-store"
        assert harness.connection_service.calls == []
        assert harness.catalog.calls == []


def test_credential_input_rejects_actor_and_raw_configuration_fields(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/credential-writes",
            json={
                "operation_id": str(OPERATION_ID),
                "alias": "primary",
                "secret": SYNTHETIC_SECRET,
                "actor": "body supplied authority",
                "config": {"api_key": SYNTHETIC_SECRET},
            },
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_configuration"
        assert SYNTHETIC_SECRET not in response.text
        assert "body supplied authority" not in response.text
        assert harness.store.calls == []
        assert harness.credential_service.calls == []


def test_managed_credential_write_returns_only_safe_completed_summary(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/credential-writes",
            json={
                "operation_id": str(OPERATION_ID),
                "alias": "primary",
                "secret": SYNTHETIC_SECRET,
            },
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 201
        assert response.json()["status"] == "completed"
        assert response.json()["credential"]["status"] == "active"
        assert "secret" not in response.json()
        assert SYNTHETIC_SECRET not in response.text
        assert harness.store.calls[0][0] == "put_new"
        assert [name for name, _args in harness.credential_service.calls] == [
            "save_managed"
        ]
        assert response.headers["cache-control"] == "no-store"


def test_rotation_records_the_replacement_reference_without_replacing_old_secret(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        old_credential = _seed_active_credential(harness)
        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/credential-writes",
            json={
                "operation_id": str(uuid4()),
                "alias": "rotated",
                "secret": "cfg2-fake-rotated-key",
                "replaces_id": str(old_credential.ref.credential_id),
            },
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 201
        assert response.json()["credential"]["replaces_id"] == str(
            old_credential.ref.credential_id
        )
        assert response.json()["credential"]["credential_id"] != str(
            old_credential.ref.credential_id
        )
        assert harness.memory_catalog.get_credential(old_credential.ref) == old_credential
        assert sum(action == "put_new" for action, _ref in harness.store.calls) == 1
        assert [name for name, _args in harness.credential_service.calls] == [
            "save_managed"
        ]


@pytest.mark.parametrize("status", ["pending", "unknown"])
def test_persisted_pending_or_unknown_write_returns_202_without_put(
    http_modules: Any,
    status: str,
) -> None:
    with _make_harness(http_modules) as harness:
        operation_id = uuid4()
        credential = _make_credential(uuid4(), status="pending")
        write = CredentialWrite(
            WORKSPACE_ID, operation_id, credential, "pending", None
        )
        harness.memory_catalog.reserve_credential_write(write)
        if status == "unknown":
            harness.memory_catalog.finish_credential_write(
                WORKSPACE_ID, operation_id, status="unknown"
            )
        harness.reset_calls()

        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/credential-writes",
            json={
                "operation_id": str(operation_id),
                "alias": credential.alias,
                "secret": SYNTHETIC_SECRET,
            },
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 202
        assert response.json()["status"] == status
        assert response.json()["credential"]["credential_id"] == str(
            credential.ref.credential_id
        )
        _assert_no_store_calls(harness)


class LockedPutStore(FakeSecretStore):
    def put_new(self, ref: CredentialRef, value: SecretStr) -> None:
        self.calls.append(("put_new", ref))
        raise ConfigurationError("credential_store_locked")


class FailedDeleteStore(FakeSecretStore):
    def delete_owned(self, ref: CredentialRef) -> None:
        self.calls.append(("delete_owned", ref))
        raise RuntimeError("synthetic delete failure")


def test_failed_write_is_fixed_503_on_repeated_post_without_replay(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules, store=LockedPutStore()) as harness:
        body = {
            "operation_id": str(OPERATION_ID),
            "alias": "primary",
            "secret": SYNTHETIC_SECRET,
        }
        path = f"/model-config/v1/workspaces/{WORKSPACE_ID}/credential-writes"
        first = harness.client.post(path, json=body, headers=AUTH_HEADERS)
        second = harness.client.post(path, json=body, headers=AUTH_HEADERS)
        queried = harness.client.get(
            f"{path}/{OPERATION_ID}", headers=AUTH_HEADERS
        )

        assert first.status_code == second.status_code == 503
        assert first.json()["error"]["code"] == "credential_unresolved"
        assert second.json()["error"]["code"] == "credential_unresolved"
        assert queried.status_code == 200
        assert queried.json()["status"] == "failed"
        assert queried.json()["credential"]["status"] == "failed"
        assert sum(action == "put_new" for action, _ref in harness.store.calls) == 1
        assert SYNTHETIC_SECRET not in first.text + second.text + queried.text
        assert queried.headers["cache-control"] == "no-store"


def test_missing_write_status_is_fixed_unknown_error(http_modules: Any) -> None:
    with _make_harness(http_modules) as harness:
        response = harness.client.get(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/credential-writes/{OPERATION_ID}",
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "credential_write_unknown"
        assert response.headers["cache-control"] == "no-store"


def test_recovery_uses_original_operation_without_accepting_a_secret(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        operation_id = uuid4()
        credential = _make_credential(uuid4(), status="pending")
        write = CredentialWrite(
            WORKSPACE_ID, operation_id, credential, "pending", None
        )
        harness.memory_catalog.reserve_credential_write(write)
        harness.store.put_new(credential.ref, SecretStr(SYNTHETIC_SECRET))
        harness.memory_catalog.finish_credential_write(
            WORKSPACE_ID, operation_id, status="unknown"
        )
        harness.reset_calls()

        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/credential-writes/{operation_id}/recover",
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert response.json()["credential"]["status"] == "active"
        assert harness.store.calls == [("read", credential.ref)]
        assert [name for name, _args in harness.credential_service.calls] == [
            "recover_write"
        ]
        assert SYNTHETIC_SECRET not in response.text


def test_recovery_rejects_body_instead_of_ignoring_replacement_secret(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/credential-writes/{OPERATION_ID}/recover",
            json={"secret": SYNTHETIC_SECRET},
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_configuration"
        assert SYNTHETIC_SECRET not in response.text
        assert harness.credential_service.calls == []
        assert harness.catalog.calls == []


class BlockingReadStore(FakeSecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_started = Event()
        self.release_read = Event()

    def read(self, ref: CredentialRef) -> SecretStr:
        self.read_started.set()
        if not self.release_read.wait(timeout=5):
            raise RuntimeError("test read release timed out")
        return super().read(ref)


def test_recovery_offloads_blocking_secret_read_and_keeps_original_operation(
    http_modules: Any,
) -> None:
    store = BlockingReadStore()
    with _make_harness(http_modules, store=store) as harness:
        operation_id = uuid4()
        credential = _make_credential(uuid4(), status="pending")
        write = CredentialWrite(
            WORKSPACE_ID, operation_id, credential, "pending", None
        )
        harness.memory_catalog.reserve_credential_write(write)
        store.put_new(credential.ref, SecretStr(SYNTHETIC_SECRET))
        harness.memory_catalog.finish_credential_write(
            WORKSPACE_ID, operation_id, status="unknown"
        )
        harness.reset_calls()
        recover_path = (
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/credential-writes/"
            f"{operation_id}/recover"
        )
        list_path = f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections"

        with ThreadPoolExecutor(max_workers=2) as executor:
            recovery_future = executor.submit(
                harness.client.post,
                recover_path,
                headers=AUTH_HEADERS,
            )
            list_future = None
            blocked = False
            try:
                assert store.read_started.wait(timeout=2)
                list_future = executor.submit(
                    harness.client.get,
                    list_path,
                    headers=AUTH_HEADERS,
                )
                try:
                    list_response = list_future.result(timeout=0.75)
                except FutureTimeoutError:
                    blocked = True
                    list_response = None
            finally:
                store.release_read.set()
            recovery_response = recovery_future.result(timeout=3)
            if list_future is not None and list_response is None:
                list_response = list_future.result(timeout=3)

        assert not blocked
        assert list_response is not None and list_response.status_code == 200
        assert recovery_response.status_code == 200
        assert recovery_response.json()["operation_id"] == str(operation_id)
        assert recovery_response.json()["status"] == "completed"
        assert store.calls == [("read", credential.ref)]
        assert harness.credential_service.calls == [
            ("recover_write", (WORKSPACE_ID, operation_id))
        ]


def test_environment_credential_registration_does_not_read_or_return_value(
    http_modules: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIDERA_TASK6_TEST_KEY", SYNTHETIC_SECRET)
    with _make_harness(http_modules) as harness:
        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/env-credentials",
            json={"alias": "deployment", "env_name": "SIDERA_TASK6_TEST_KEY"},
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 201
        assert response.json()["source"] == "env"
        assert response.json()["status"] == "active"
        assert "env_name" not in response.json()
        assert SYNTHETIC_SECRET not in response.text
        _assert_no_store_calls(harness)
        assert [name for name, _args in harness.credential_service.calls] == [
            "register_env"
        ]


def test_revoke_returns_service_cleanup_pending_flag_and_safe_credential(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules, store=FailedDeleteStore()) as harness:
        credential = _seed_active_credential(harness)
        response = harness.client.post(
            f"/model-config/v1/workspaces/{WORKSPACE_ID}/credentials/{credential.ref.credential_id}/revoke",
            json={"expected_version": credential.version},
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["credential"]["status"] == "revoked"
        assert response.json()["credential"]["version"] == credential.version + 1
        assert response.json()["cleanup_pending"] is True
        assert harness.store.calls == [("delete_owned", credential.ref)]
        assert SYNTHETIC_SECRET not in response.text
        assert [name for name, _args in harness.credential_service.calls] == [
            "revoke"
        ]


def test_cors_preflight_requires_exact_origin_and_never_calls_services(
    http_modules: Any,
) -> None:
    with _make_harness(http_modules) as harness:
        path = f"/model-config/v1/workspaces/{WORKSPACE_ID}/connections"
        allowed = harness.client.options(
            path,
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        rejected = harness.client.options(
            path,
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert "POST" in allowed.headers["access-control-allow-methods"]
        assert allowed.headers.get("access-control-allow-credentials") is None
        assert rejected.status_code == 400
        assert rejected.headers.get("access-control-allow-origin") is None
        assert harness.catalog.calls == []
        assert harness.connection_service.calls == []
        assert harness.credential_service.calls == []


def test_configuration_error_status_map_is_complete_and_fixed(
    http_modules: Any,
) -> None:
    assert http_modules.router.CONFIGURATION_ERROR_STATUS == {
        "invalid_configuration": 422,
        "configuration_not_found": 404,
        "configuration_version_conflict": 409,
        "credential_not_found": 404,
        "credential_revoked": 409,
        "credential_unresolved": 503,
        "credential_store_unavailable": 503,
        "credential_store_locked": 503,
        "credential_write_unknown": 503,
        "credential_operation_conflict": 409,
        "credential_already_exists": 409,
        "runtime_binding_denied": 403,
        "connection_role_denied": 403,
        "connection_target_denied": 403,
        "management_unauthorized": 401,
        "management_scope_denied": 403,
    }
