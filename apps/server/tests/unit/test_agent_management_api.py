import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import CatalogStatusTarget, ProfileDraft, RuntimeRef
from sidera_agents.errors import AgentError
from sidera_agents.memory import MemoryAgentStore
from sidera_control_plane.composition.native_runtime import (
    ProfileWorkRunTarget,
    ProfileWorkTarget,
)

from sidera_product.composition import build_product_runtime
from sidera_product.config import ProductSettings
from tests.unit.test_configuration_requests import ConfigurationSQLBoundary
from tests.unit.test_defaults import DefaultsSQLBoundary
from tests.unit.test_lifecycle import (
    controlled_owner_runtime as controlled_owner_runtime,
)
from tests.unit.test_local_session import MemorySessionStore


@pytest.fixture
def management_runtime(settings_values, no_database_connections, monkeypatch):
    settings = ProductSettings.model_validate(settings_values)
    settings.workspace_root.mkdir()
    settings.artifact_root.mkdir()
    runtime = build_product_runtime(settings)
    sessions = MemorySessionStore()
    monkeypatch.setattr(runtime.local_sessions, "store", sessions)
    monkeypatch.setattr(runtime.authority_store, "find_session", sessions.find_session)
    monkeypatch.setattr(runtime.authority_store, "find_grant", lambda **kwargs: None)
    issue = runtime.local_sessions.exchange(
        runtime.local_sessions.create_bootstrap().token
    )
    memory = MemoryAgentStore()
    for method in (
        "create_profile",
        "get_profile",
        "set_profile_enabled",
        "get_profile_status",
    ):
        monkeypatch.setattr(
            runtime.agents.catalog._store, method, getattr(memory, method)
        )
    yield runtime, sessions, issue
    assert runtime.close(grace_seconds=0) is True


def profile_draft(runtime):
    return ProfileDraft(
        name="Reusable reviewer",
        description="",
        instructions="Review safely.",
        runtime_ref=RuntimeRef(
            workspace_id=runtime.settings.workspace_id, runtime_id=uuid4()
        ),
        resource_refs=(),
        model_id=None,
        thinking=None,
        max_active=1,
        environment={},
        secret_refs=(),
        native_settings={},
    )


def test_runtime_availability_refresh_uses_authoritative_metadata_without_probe(
    controlled_owner_runtime, monkeypatch
):
    runtime, calls = controlled_owner_runtime
    original = runtime.agents.runtimes.list_runtimes
    installed = original(
        runtime.settings.workspace_id, grant_id=runtime.background_identity.grant_id
    )
    selected = []
    reads = []

    def metadata(workspace_id, *, grant_id):
        assert workspace_id == runtime.settings.workspace_id
        assert grant_id == runtime.background_identity.grant_id
        reads.append((workspace_id, grant_id))
        return tuple(selected)

    monkeypatch.setattr(runtime.agents.runtimes, "list_runtimes", metadata)
    runtime.start()
    assert runtime.status()["execution"] == {"ready": False}
    selected.extend(installed)
    assert runtime.refresh_execution_availability() is True
    assert runtime.status()["main_agent"] == {"available": True, "code": None}
    selected[:] = [installed[0].model_copy(update={"enabled": False})]
    assert runtime.refresh_execution_availability() is False
    assert runtime.status()["execution"] == {"ready": False}
    assert len(reads) == 3 and calls == ["agents.start", "cp.start"]


def test_cold_runtime_refresh_is_inert_and_metadata_failure_is_unavailable(
    controlled_owner_runtime, monkeypatch
):
    runtime, _ = controlled_owner_runtime
    original = runtime.agents.runtimes.list_runtimes
    monkeypatch.setattr(
        runtime.agents.runtimes,
        "list_runtimes",
        lambda *args, **kwargs: pytest.fail("Cold metadata refresh connected"),
    )
    assert runtime.refresh_execution_availability() is False
    monkeypatch.setattr(runtime.agents.runtimes, "list_runtimes", original)
    runtime.start()

    def unavailable(*args, **kwargs):
        raise RuntimeError("PRIVATE_DRIVER_DETAILS")

    monkeypatch.setattr(runtime.agents.runtimes, "list_runtimes", unavailable)
    assert runtime.refresh_execution_availability() is False
    assert runtime.status()["execution"] == {"ready": False}
    assert "PRIVATE_DRIVER_DETAILS" not in repr(runtime.status())


def test_shutdown_fence_wins_over_held_runtime_metadata_refresh(
    controlled_owner_runtime, monkeypatch
):
    from threading import Event, Thread

    runtime, _ = controlled_owner_runtime
    runtime.start()
    original = runtime.agents.runtimes.list_runtimes
    entered, release = Event(), Event()
    results = []

    def held(*args, **kwargs):
        snapshots = original(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return snapshots

    monkeypatch.setattr(runtime.agents.runtimes, "list_runtimes", held)
    worker = Thread(
        target=lambda: results.append(runtime.refresh_execution_availability())
    )
    try:
        worker.start()
        assert entered.wait(3)
        assert runtime.close(grace_seconds=0) is True
        release.set()
        worker.join(3)
        assert not worker.is_alive() and results == [False]
        assert runtime.status()["lifecycle"] == "closed"
        assert runtime.status()["execution"] == {"ready": False}
    finally:
        release.set()
        worker.join(3)


@pytest.mark.parametrize("older,newer", [(True, False), (False, True), ("error", True)])
def test_runtime_availability_latest_observation_wins(
    controlled_owner_runtime, monkeypatch, older, newer
):
    from threading import Event, Thread

    runtime, calls = controlled_owner_runtime
    runtime.start()
    snapshot = runtime.agents.runtimes.list_runtimes(
        runtime.settings.workspace_id, grant_id=runtime.background_identity.grant_id
    )[0]
    entered, release = Event(), Event()
    reads, results, errors = [], [], []

    def ordered(workspace_id, *, grant_id):
        assert workspace_id == runtime.settings.workspace_id
        assert grant_id == runtime.background_identity.grant_id
        reads.append(len(reads) + 1)
        if len(reads) == 1:
            entered.set()
            assert release.wait(3)
            if older == "error":
                raise RuntimeError("CONTROLLED_OLDER_READ_FAILURE")
            return (snapshot.model_copy(update={"enabled": older}),)
        return (snapshot.model_copy(update={"enabled": newer}),)

    def refresh():
        try:
            results.append(runtime.refresh_execution_availability())
        except Exception as error:  # noqa: BLE001 - thread failures must reach the final assertion
            errors.append(type(error))

    monkeypatch.setattr(runtime.agents.runtimes, "list_runtimes", ordered)
    worker = Thread(target=refresh)
    try:
        worker.start()
        assert entered.wait(3)
        assert runtime.refresh_execution_availability() is newer
        assert runtime.status()["execution"] == {"ready": newer}
        release.set()
        worker.join(3)
        assert not worker.is_alive() and errors == []
        assert results == [newer]
        assert runtime.status()["execution"] == {"ready": newer}
        assert reads == [1, 2] and calls == ["agents.start", "cp.start"]
    finally:
        release.set()
        worker.join(3)


@pytest.fixture
def management_http_runtime(settings_values, no_database_connections, monkeypatch):
    from fastapi.testclient import TestClient

    from sidera_product.agents import configuration_store
    from sidera_product.app import create_app
    from sidera_product.postgres import chat_store
    from sidera_product.postgres import store as store_module
    from tests.support.controlled_native import ControlledHarness, ControlledSecretStore
    from tests.unit.test_chat_store import JournalBoundary

    settings = ProductSettings.model_validate(settings_values)
    settings.workspace_root.mkdir(mode=0o700)
    settings.artifact_root.mkdir(mode=0o700)
    native = ControlledHarness()
    runtime = build_product_runtime(
        settings, harnesses=[native], secret_store=ControlledSecretStore()
    )
    sessions = MemorySessionStore()
    monkeypatch.setattr(runtime.local_sessions, "store", sessions)
    monkeypatch.setattr(runtime.authority_store, "find_session", sessions.find_session)
    monkeypatch.setattr(runtime.authority_store, "find_grant", lambda **kwargs: None)
    memory = MemoryAgentStore()
    for method in (
        "create_profile",
        "get_profile",
        "get_profile_head",
        "list_profiles",
        "append_profile_revision",
        "get_profile_status",
        "set_profile_enabled",
        "create_resource",
        "get_resource",
        "get_resource_head",
        "list_resources",
        "append_resource_revision",
        "get_resource_status",
        "set_resource_enabled",
        "find_runtime",
        "register_runtime",
        "get_runtime_entry",
        "list_runtime_entries",
        "update_runtime_observation",
        "set_runtime_configuration",
    ):
        monkeypatch.setattr(
            runtime.agents.catalog._store, method, getattr(memory, method)
        )
    journal = ConfigurationSQLBoundary(runtime.product_store.engine)
    monkeypatch.setattr(configuration_store, "Session", journal.session)
    defaults = DefaultsSQLBoundary(runtime.product_store.engine)
    monkeypatch.setattr(store_module, "Session", defaults.session)
    chats = JournalBoundary(runtime.product_store.engine)
    monkeypatch.setattr(chat_store, "Session", chats.session)
    client = TestClient(create_app(runtime=runtime), base_url=settings.local_url)
    bootstrap = runtime.local_sessions.create_bootstrap()
    exchanged = client.post(
        "/product/v1/session/bootstrap",
        json={"token": bootstrap.token.get_secret_value()},
        headers={"Origin": settings.web_origin},
    )
    assert exchanged.status_code == 200
    headers = {
        "Origin": settings.web_origin,
        "X-Sidera-CSRF": client.cookies.get("sidera_csrf"),
    }
    yield SimpleNamespace(
        runtime=runtime,
        client=client,
        prefix=f"/agents/v1/workspaces/{settings.workspace_id}",
        headers=headers,
        native=native,
        journal=journal,
        defaults=defaults,
        memory=memory,
        chats=chats,
    )
    client.close()
    assert runtime.close(grace_seconds=0) is True


def test_mounted_profile_configuration_journal_routes_keep_original_identity_and_cas(
    management_http_runtime,
):
    lab = management_http_runtime
    body = {
        "operationId": "save/original 1",
        "draft": profile_draft(lab.runtime).model_dump(mode="json"),
    }
    created = lab.client.post(lab.prefix + "/profiles", json=body, headers=lab.headers)
    assert created.status_code == 201
    original = created.json()
    assert set(original) == {
        "operationId",
        "state",
        "target_ref",
        "result_ref",
        "reason_code",
    }
    assert (
        original["state"] == "succeeded"
        and original["operationId"] == body["operationId"]
    )
    duplicate = lab.client.post(
        lab.prefix + "/profiles", json=body, headers=lab.headers
    )
    assert duplicate.status_code == 201 and duplicate.json() == original
    query = lab.client.get(
        f"/product/v1/workspaces/{lab.runtime.settings.workspace_id}/configuration-requests",
        params={"operationId": body["operationId"]},
    )
    assert query.status_code == 200 and query.json() == original
    profile_id = original["target_ref"]["profile_id"]
    saved = lab.client.get(lab.prefix + "/profiles/" + profile_id)
    assert (
        saved.status_code == 200
        and saved.json()["profile"]["ref"] == original["result_ref"]
    )
    draft = {**body["draft"], "instructions": "Next reviewed instructions"}
    revised = lab.client.post(
        lab.prefix + f"/profiles/{profile_id}/revisions",
        json={
            "operationId": "revision",
            "expected_ref": original["target_ref"],
            "draft": draft,
        },
        headers=lab.headers,
    )
    assert revised.status_code == 200 and revised.json()["result_ref"]["revision"] == 2
    stale = lab.client.post(
        lab.prefix + f"/profiles/{profile_id}/revisions",
        json={
            "operationId": "stale",
            "expected_ref": original["target_ref"],
            "draft": draft,
        },
        headers=lab.headers,
    )
    assert (
        stale.status_code == 409 and stale.json()["reason_code"] == "revision_conflict"
    )
    pinned = lab.client.get(
        lab.prefix + f"/profiles/{profile_id}", params={"revision": 1}
    )
    assert (
        pinned.json()["profile"]["draft"]["instructions"]
        == body["draft"]["instructions"]
    )
    assert len(lab.journal.rows) == 3 and lab.native.calls == []


@pytest.mark.parametrize(
    "kind,settings",
    [
        ("skill", {"body": "Inline guidance"}),
        (
            "skill",
            {
                "source_ref": {
                    "provider": "owned",
                    "key": "skill/original",
                    "version": "1",
                },
                "content_hash": "sha256:" + "a" * 64,
            },
        ),
        ("mcp", {"connection_id": str(uuid4())}),
        (
            "mcp",
            {
                "transport": "stdio",
                "command": ["/usr/bin/true"],
                "environment": {"SAFE_MODE": "read"},
            },
        ),
        (
            "mcp",
            {
                "transport": "http",
                "url": "https://example.test/mcp",
                "headers": {"X-Mode": "read"},
            },
        ),
        ("integration", {"connection_id": str(uuid4())}),
    ],
)
def test_mounted_resource_modes_use_actual_catalog_validation_and_independent_revision(
    management_http_runtime, kind, settings
):
    lab = management_http_runtime
    body = {
        "operationId": "resource",
        "draft": {
            "name": "Owned resource",
            "kind": kind,
            "settings": settings,
            "secret_refs": [],
        },
    }
    created = lab.client.post(lab.prefix + "/resources", json=body, headers=lab.headers)
    assert created.status_code == 201, created.text
    ref = created.json()["result_ref"]
    fetched = lab.client.get(lab.prefix + f"/resources/{ref['resource_id']}")
    assert (
        fetched.status_code == 200
        and fetched.json()["resource"]["draft"] == body["draft"]
    )
    changed = {**body["draft"], "name": "Independent next revision"}
    revised = lab.client.post(
        lab.prefix + f"/resources/{ref['resource_id']}/revisions",
        json={"operationId": "next", "expected_ref": ref, "draft": changed},
        headers=lab.headers,
    )
    assert revised.status_code == 200 and revised.json()["result_ref"]["revision"] == 2
    disabled = lab.client.put(
        lab.prefix + f"/resources/{ref['resource_id']}/status",
        json={"enabled": False, "expected_version": 1},
        headers=lab.headers,
    )
    assert disabled.status_code == 200 and disabled.json()["version"] == 2
    old = lab.client.get(
        lab.prefix + f"/resources/{ref['resource_id']}", params={"revision": 1}
    )
    assert old.json()["resource"]["draft"]["name"] == "Owned resource"
    assert old.json()["status"]["enabled"] is False and lab.native.calls == []


@pytest.mark.parametrize(
    "kind,settings",
    [
        pytest.param(
            "mcp",
            {"transport": "stdio", "command": ["relative-tool"]},
            id="relative-stdio",
        ),
        pytest.param(
            "skill",
            {
                "source_ref": {"provider": "owned", "key": "guidance", "version": "1"},
                "content_hash": "sha256:bad",
            },
            id="invalid-skill-hash",
        ),
        pytest.param(
            "skill",
            {
                "body": "Inline guidance",
                "source_ref": {"provider": "owned", "key": "guidance", "version": "1"},
                "content_hash": "sha256:" + "a" * 64,
            },
            id="mixed-skill-modes",
        ),
        pytest.param(
            "mcp",
            {"transport": "http", "url": "https://user@example.test/mcp"},
            id="http-userinfo",
        ),
        pytest.param(
            "mcp",
            {"transport": "http", "url": "https://example.test/mcp?mode=read"},
            id="http-query",
        ),
        pytest.param(
            "mcp",
            {"transport": "http", "url": "https://example.test/mcp#fragment"},
            id="http-fragment",
        ),
        pytest.param(
            "mcp",
            {"transport": "http", "url": "http://example.test/mcp"},
            id="remote-plain-http",
        ),
        pytest.param(
            "mcp",
            {"transport": "http", "url": "https://example.test:99999/mcp"},
            id="http-invalid-port",
        ),
        pytest.param(
            "mcp",
            {
                "transport": "stdio",
                "command": ["/usr/bin/true"],
                "environment": {"API_TOKEN": "CONTROLLED_NON_SECRET_VALUE"},
            },
            id="ordinary-secret-environment",
        ),
        pytest.param(
            "mcp",
            {
                "transport": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "CONTROLLED_NON_SECRET_VALUE"},
            },
            id="ordinary-secret-header",
        ),
    ],
)
def test_mounted_resource_invalid_modes_are_rejected_by_actual_catalog(
    management_http_runtime, kind, settings
):
    lab = management_http_runtime
    response = lab.client.post(
        lab.prefix + "/resources",
        json={
            "operationId": "invalid-resource",
            "draft": {
                "name": "Controlled invalid resource",
                "kind": kind,
                "settings": settings,
                "secret_refs": [],
            },
        },
        headers=lab.headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["state"] == "rejected"
    assert response.json()["reason_code"] == "configuration_invalid"
    assert "CONTROLLED_NON_SECRET_VALUE" not in response.text
    assert lab.client.get(lab.prefix + "/resources").json()["items"] == []
    assert len(lab.journal.rows) == 1 and lab.native.calls == []


@pytest.mark.parametrize("kind", ["resource", "credential"])
def test_mounted_resource_foreign_refs_deny_before_journal_or_native(
    management_http_runtime, kind
):
    lab = management_http_runtime
    if kind == "resource":
        draft = profile_draft(lab.runtime).model_dump(mode="json")
        draft["resource_refs"] = [
            {"workspace_id": str(uuid4()), "resource_id": str(uuid4()), "revision": 1}
        ]
        path = "/profiles"
    else:
        draft = {
            "name": "Owned MCP",
            "kind": "mcp",
            "settings": {"connection_id": str(uuid4())},
            "secret_refs": [
                {
                    "credential_ref": {
                        "workspace_id": str(uuid4()),
                        "credential_id": str(uuid4()),
                    },
                    "destination": "API_TOKEN",
                }
            ],
        }
        path = "/resources"
    response = lab.client.post(
        lab.prefix + path,
        json={"operationId": "foreign-ref", "draft": draft},
        headers=lab.headers,
    )
    assert response.status_code == 403, response.text
    assert lab.journal.rows == {} and lab.native.calls == []
    assert lab.client.get(lab.prefix + path).json()["items"] == []


def test_mounted_disabled_resource_cannot_be_newly_bound_and_original_pin_survives(
    management_http_runtime,
):
    lab = management_http_runtime
    resource = lab.client.post(
        lab.prefix + "/resources",
        json={
            "operationId": "owned-skill",
            "draft": {
                "name": "Owned skill",
                "kind": "skill",
                "settings": {"body": "Original guidance"},
                "secret_refs": [],
            },
        },
        headers=lab.headers,
    )
    assert resource.status_code == 201, resource.text
    resource_ref = resource.json()["result_ref"]
    draft = profile_draft(lab.runtime).model_dump(mode="json")
    draft["resource_refs"] = [resource_ref]
    created = lab.client.post(
        lab.prefix + "/profiles",
        json={"operationId": "owned-profile", "draft": draft},
        headers=lab.headers,
    )
    assert created.status_code == 201, created.text
    profile_ref = created.json()["result_ref"]
    disabled = lab.client.put(
        lab.prefix + f"/resources/{resource_ref['resource_id']}/status",
        json={"enabled": False, "expected_version": 1},
        headers=lab.headers,
    )
    assert disabled.status_code == 200
    revised = lab.client.post(
        lab.prefix + f"/profiles/{profile_ref['profile_id']}/revisions",
        json={
            "operationId": "new-binding-denied",
            "expected_ref": profile_ref,
            "draft": {**draft, "instructions": "A separate new arrangement"},
        },
        headers=lab.headers,
    )
    assert (
        revised.status_code == 422
        and revised.json()["reason_code"] == "configuration_invalid"
    )
    original = lab.client.get(
        lab.prefix + f"/profiles/{profile_ref['profile_id']}", params={"revision": 1}
    )
    assert original.status_code == 200
    assert original.json()["profile"]["ref"] == profile_ref
    assert original.json()["profile"]["draft"]["resource_refs"] == [resource_ref]
    assert lab.native.calls == []


def test_mounted_runtime_metadata_and_explicit_actions_do_not_open_or_prompt(
    management_http_runtime,
):
    lab = management_http_runtime
    descriptors = lab.client.get(lab.prefix + "/runtime-descriptors")
    assert descriptors.status_code == 200 and len(descriptors.json()) == 1
    assert lab.client.get(lab.prefix + "/runtimes").json() == {"items": []}
    assert lab.native.calls == []
    registered = lab.client.post(
        lab.prefix + "/runtimes",
        json={
            "harness_id": lab.native.harness_id,
            "executable": sys.executable,
            "max_active": 1,
            "max_sessions": 2,
        },
        headers=lab.headers,
    )
    assert registered.status_code == 201
    snapshot = registered.json()
    ref = snapshot["ref"]
    runtime_id = ref["runtime_id"]
    assert [call[0] for call in lab.native.calls] == ["probe"]
    got = lab.client.get(lab.prefix + f"/runtimes/{runtime_id}")
    assert got.status_code == 200 and got.json() == snapshot
    limited = lab.client.put(
        lab.prefix + f"/runtimes/{runtime_id}/limits",
        json={"ref": ref, "max_active": 2, "max_sessions": 3, "expected_version": 1},
        headers=lab.headers,
    )
    assert limited.status_code == 200 and limited.json()["version"] == 2
    stale = lab.client.put(
        lab.prefix + f"/runtimes/{runtime_id}/status",
        json={"ref": ref, "enabled": False, "expected_version": 1},
        headers=lab.headers,
    )
    assert stale.status_code == 409
    disabled = lab.client.put(
        lab.prefix + f"/runtimes/{runtime_id}/status",
        json={"ref": ref, "enabled": False, "expected_version": 2},
        headers=lab.headers,
    )
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    assert [call[0] for call in lab.native.calls] == ["probe"]
    assert lab.runtime.status()["lifecycle"] == "constructed"


@pytest.mark.parametrize(
    "variant", ["anonymous", "revoked", "foreign", "csrf", "unsafe_body"]
)
def test_mounted_management_denies_before_package_io_and_never_echoes_secret_inputs(
    management_http_runtime, monkeypatch, variant
):
    lab = management_http_runtime
    prefix, headers = lab.prefix, lab.headers
    sentinel = "PRIVATE_SUBMITTED_SECRET_SENTINEL"
    body = {
        "operationId": "original",
        "draft": profile_draft(lab.runtime).model_dump(mode="json"),
    }
    if variant == "anonymous":
        lab.client.cookies.clear()
    elif variant == "revoked":
        row = next(iter(lab.runtime.local_sessions.store.sessions.values()))
        lab.runtime.local_sessions.revoke(row.session_id)
    elif variant == "foreign":
        prefix = f"/agents/v1/workspaces/{uuid4()}"
    elif variant == "csrf":
        headers = {"Origin": lab.runtime.settings.web_origin}
    else:
        body["draft"]["max_active"] = sentinel
    monkeypatch.setattr(
        lab.runtime.agents.catalog,
        "create_profile",
        lambda *args, **kwargs: pytest.fail("Denied catalog write"),
    )
    reply = lab.client.post(prefix + "/profiles", json=body, headers=headers)
    assert reply.status_code == (
        422
        if variant == "unsafe_body"
        else 403
        if variant in {"foreign", "csrf"}
        else 401
    )
    assert (
        sentinel not in reply.text and lab.journal.rows == {} and lab.native.calls == []
    )


def test_actual_product_openapi_exposes_finite_session_and_management_contracts_without_startup(
    management_http_runtime,
):
    lab = management_http_runtime
    document = lab.client.app.openapi()
    base = "/agents/v1/workspaces/{workspace_id}"
    expected = [
        "/profiles",
        "/profiles/{profile_id}",
        "/profiles/{profile_id}/revisions",
        "/profiles/{profile_id}/status",
        "/profiles/{profile_id}/validation",
        "/profiles/{profile_id}/work",
        "/resources",
        "/resources/{resource_id}/revisions",
        "/resources/{resource_id}/status",
        "/runtime-descriptors",
        "/runtimes",
        "/runtimes/discovery",
        "/runtimes/{runtime_id}/refresh",
        "/runtimes/{runtime_id}/limits",
        "/runtimes/{runtime_id}/status",
    ]
    assert all(base + route in document["paths"] for route in expected)
    assert "/agents/v1/{path}" not in document["paths"]
    schema = document["paths"]["/product/v1/session"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/LocalSessionView"}
    assert set(document["components"]["schemas"]["LocalSessionView"]["properties"]) == {
        "session_id",
        "workspace_id",
        "principal_id",
        "expires_at",
    }
    assert (
        "key"
        not in document["components"]["schemas"]["ConfigurationResult"]["properties"]
    )
    assert lab.runtime.status()["lifecycle"] == "constructed" and lab.native.calls == []


@pytest.mark.parametrize(
    "operation_id",
    [
        "createAgentProfile",
        "reviseAgentProfile",
        "createAgentResource",
        "reviseAgentResource",
        "readConfigurationRequest",
    ],
)
def test_configuration_openapi_declares_original_pending_result(
    management_http_runtime, operation_id
):
    lab = management_http_runtime
    document = lab.client.app.openapi()
    operations = [
        operation
        for path in document["paths"].values()
        for operation in path.values()
        if operation.get("operationId") == operation_id
    ]
    assert len(operations) == 1
    responses = operations[0]["responses"]
    assert responses["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ConfigurationResult"
    }
    assert ("201" if operation_id.startswith("create") else "200") in responses
    assert lab.runtime.status()["lifecycle"] == "constructed" and lab.native.calls == []


@pytest.mark.parametrize("kind", ["profile", "resource"])
def test_mounted_configuration_commit_lost_reply_queries_only_original_ref(
    management_http_runtime, monkeypatch, kind
):
    lab = management_http_runtime
    writes = []
    original = getattr(lab.memory, f"create_{kind}")

    def commit_then_lose_reply(*args, **kwargs):
        saved = original(*args, **kwargs)
        writes.append(saved.ref)
        raise OSError("Controlled catalog reply loss")

    monkeypatch.setattr(
        lab.runtime.agents.catalog._store, f"create_{kind}", commit_then_lose_reply
    )
    draft = (
        profile_draft(lab.runtime).model_dump(mode="json")
        if kind == "profile"
        else {
            "name": "Original skill",
            "kind": "skill",
            "settings": {"body": "Reviewed instructions"},
            "secret_refs": [],
        }
    )
    operation_id = f"original/{kind} save"
    pending = lab.client.post(
        lab.prefix + f"/{kind}s",
        json={"operationId": operation_id, "draft": draft},
        headers=lab.headers,
    )
    assert pending.status_code == 202
    result = pending.json()
    assert result["operationId"] == operation_id and result["state"] == "unknown"
    assert result["result_ref"] is None
    assert result["target_ref"] == writes[0].model_dump(mode="json")
    recovered = lab.client.get(
        f"/product/v1/workspaces/{lab.runtime.settings.workspace_id}/configuration-requests",
        params={"operationId": operation_id},
    )
    assert recovered.status_code == 200
    assert recovered.json()["state"] == "reconciled"
    assert recovered.json()["operationId"] == operation_id
    assert (
        recovered.json()["target_ref"]
        == recovered.json()["result_ref"]
        == result["target_ref"]
    )
    assert len(writes) == len(lab.journal.rows) == 1 and lab.native.calls == []


def test_product_profile_work_route_consumes_actual_public_cp_port_and_own_conversation_refs(
    management_http_runtime, monkeypatch
):
    from sidera_control_plane.persistence.assignment_read_store import (
        PostgresAssignmentReadStore,
    )

    from sidera_product.chat.contracts import CreateThreadBody

    lab = management_http_runtime
    created = lab.client.post(
        lab.prefix + "/profiles",
        json={
            "operationId": "profile",
            "draft": profile_draft(lab.runtime).model_dump(mode="json"),
        },
        headers=lab.headers,
    )
    ref = created.json()["result_ref"]
    reads = []

    def read(self, workspace_id, profile_id, limit, after):
        assert workspace_id == lab.runtime.settings.workspace_id
        assert str(profile_id) == ref["profile_id"]
        assert limit == 6 and after is None
        reads.append((workspace_id, profile_id))
        return ()

    monkeypatch.setattr(PostgresAssignmentReadStore, "list_profile_work", read)
    session_id = next(iter(lab.runtime.local_sessions.store.sessions))
    identity = lab.runtime.local_sessions.identity_for_grant(session_id)
    thread = lab.runtime.chat.create_thread(
        identity, CreateThreadBody(title="Actual product chat", profile_ref=ref)
    )
    reply = lab.client.get(
        lab.prefix + f"/profiles/{ref['profile_id']}/work", params={"limit": 5}
    )
    assert reply.status_code == 200
    assert reply.json()["task_work"] == {"items": [], "next_cursor": None}
    assert reply.json()["conversations"][0]["conversation_id"] == str(thread.id)
    assert reply.json()["conversations"][0]["profile_ref"] == ref
    assert reply.json()["conversations"][0]["run_id"] is None
    assert len(reads) == 1 and lab.native.calls == []


def test_product_profile_work_cursor_reaches_actual_public_cp_second_page(
    management_http_runtime, monkeypatch
):
    from sidera_control_plane.domain.assignment_views import ProfileWorkRow
    from sidera_control_plane.persistence.assignment_read_store import (
        PostgresAssignmentReadStore,
    )

    lab = management_http_runtime
    created = lab.client.post(
        lab.prefix + "/profiles",
        json={
            "operationId": "paged-profile",
            "draft": profile_draft(lab.runtime).model_dump(mode="json"),
        },
        headers=lab.headers,
    )
    ref = created.json()["result_ref"]
    first, second = uuid4(), uuid4()
    when = datetime(2026, 10, 5, tzinfo=UTC)
    rows = tuple(
        ProfileWorkRow(
            workspace_id=lab.runtime.settings.workspace_id,
            work_id=work_id,
            kind="planning",
            title=title,
            status="queued",
            updated_at=when - timedelta(seconds=index),
            profile_ref=ref,
        )
        for index, (work_id, title) in enumerate(
            ((first, "First original"), (second, "Second original"))
        )
    )
    reads = []

    def read(self, workspace_id, profile_id, limit, after):
        assert workspace_id == lab.runtime.settings.workspace_id
        assert str(profile_id) == ref["profile_id"] and limit == 2
        reads.append(after)
        if after is None:
            return rows
        assert after.workspace_id == workspace_id and after.profile_id == profile_id
        assert (
            after.work_id == first
            and after.kind == "planning"
            and after.target_type == ""
        )
        return rows[1:]

    monkeypatch.setattr(PostgresAssignmentReadStore, "list_profile_work", read)
    endpoint = lab.prefix + f"/profiles/{ref['profile_id']}/work"
    page = lab.client.get(endpoint, params={"limit": 1})
    assert page.status_code == 200
    assert [item["work_id"] for item in page.json()["task_work"]["items"]] == [
        str(first)
    ]
    cursor = page.json()["task_work"]["next_cursor"]
    assert isinstance(cursor, str)
    next_page = lab.client.get(endpoint, params={"limit": 1, "cursor": cursor})
    assert next_page.status_code == 200
    assert [item["work_id"] for item in next_page.json()["task_work"]["items"]] == [
        str(second)
    ]
    assert next_page.json()["task_work"]["next_cursor"] is None
    assert len(reads) == 2 and lab.native.calls == []


def test_mounted_defaults_precede_catchall_and_keep_exact_expected_version(
    management_http_runtime,
):
    lab = management_http_runtime
    prefix = f"/product/v1/workspaces/{lab.runtime.settings.workspace_id}/defaults"
    initial = lab.client.get(prefix)
    assert initial.status_code == 200 and initial.json()["version"] == 0
    created = lab.client.post(
        lab.prefix + "/profiles",
        json={
            "operationId": "profile",
            "draft": profile_draft(lab.runtime).model_dump(mode="json"),
        },
        headers=lab.headers,
    )
    profile_id = created.json()["result_ref"]["profile_id"]
    body = {
        "expected_version": 0,
        "main_profile_id": profile_id,
        "planner_profile_id": profile_id,
        "worker_profile_id": profile_id,
    }
    saved = lab.client.put(prefix, json=body, headers=lab.headers)
    assert saved.status_code == 200 and saved.json()["version"] == 1
    stale = lab.client.put(prefix, json=body, headers=lab.headers)
    assert (
        stale.status_code == 409
        and stale.json()["detail"]["code"] == "version_conflict"
    )
    assert lab.client.get(prefix).json() == saved.json()
    assert lab.defaults.writes == 1 and lab.native.calls == []


@pytest.mark.parametrize("variant", ["current", "anonymous", "foreign", "revoked"])
def test_mounted_credentials_use_existing_scoped_management_owner_without_secret_read(
    management_http_runtime, monkeypatch, variant
):
    from sidera_model_connections.contracts import CredentialMetadata, CredentialRef

    lab = management_http_runtime
    reads = []
    metadata = CredentialMetadata(
        ref=CredentialRef(lab.runtime.settings.workspace_id, uuid4()),
        alias="Safe alias",
        source="env",
        status="active",
        env_name="PRIVATE_ENV_NAME",
        replaces_ref=None,
        created_at=datetime.now(UTC),
        version=1,
    )

    def list_credentials(workspace_id):
        assert workspace_id == metadata.ref.workspace_id
        reads.append(workspace_id)
        return (metadata,)

    monkeypatch.setattr(
        lab.runtime.credential_catalog, "list_credentials", list_credentials
    )
    monkeypatch.setattr(
        lab.runtime.secret_store,
        "read",
        lambda *args: pytest.fail("Metadata resolved a secret"),
    )
    workspace_id = lab.runtime.settings.workspace_id
    if variant == "anonymous":
        lab.client.cookies.clear()
    elif variant == "foreign":
        workspace_id = uuid4()
    elif variant == "revoked":
        session_id = next(iter(lab.runtime.local_sessions.store.sessions))
        lab.runtime.local_sessions.revoke(session_id)
    reply = lab.client.get(f"/model-config/v1/workspaces/{workspace_id}/credentials")
    assert reply.status_code == (
        200 if variant == "current" else 403 if variant == "foreign" else 401
    )
    assert reply.headers["cache-control"] == "no-store"
    assert "PRIVATE_ENV_NAME" not in reply.text
    if variant == "current":
        assert set(reply.json()[0]) == {
            "workspace_id",
            "credential_id",
            "alias",
            "source",
            "status",
            "version",
            "replaces_id",
        }
        assert reads == [workspace_id]
    else:
        assert reads == []
    assert lab.native.calls == []


def test_composed_current_human_can_change_public_catalog_status(management_runtime):
    runtime, _, issue = management_runtime
    profile = runtime.agents.catalog.create_profile(
        runtime.settings.workspace_id,
        profile_draft(runtime),
        grant_id=issue.session_id,
    )
    disabled = runtime.agents.catalog.set_profile_enabled(
        profile.ref.workspace_id,
        profile.ref.profile_id,
        False,
        1,
        grant_id=issue.session_id,
    )
    assert disabled.enabled is False and disabled.version == 2
    assert (
        runtime.agents.catalog.get_profile(profile.ref, grant_id=issue.session_id)
        == profile
    )


def test_composed_descriptor_reads_use_captured_public_ids_without_probe(
    management_runtime, monkeypatch
):
    runtime, _, issue = management_runtime
    known = runtime.agents.runtimes.registered_harness_ids()
    assert known
    for harness in runtime.agents.runtimes._registry.all():
        monkeypatch.setattr(
            harness,
            "probe",
            lambda *args: pytest.fail("Metadata probed native runtime"),
        )
    descriptors = runtime.agents.runtimes.list_descriptors(
        runtime.settings.workspace_id,
        grant_id=issue.session_id,
    )
    assert tuple(item.harness_id for item in descriptors) == known
    for harness_id in known:
        assert (
            runtime.agents.runtimes.describe(
                harness_id, grant_id=issue.session_id
            ).harness_id
            == harness_id
        )


@pytest.mark.parametrize(
    "variant",
    [
        "foreign",
        "wrong_action",
        "wrong_entity",
        "unknown_harness",
        "unknown_grant",
        "background",
        "expired",
        "revoked",
    ],
)
def test_composed_directory_denial_precedes_store_or_registry(
    management_runtime, monkeypatch, variant
):
    runtime, sessions, issue = management_runtime
    grant_id = issue.session_id
    action = "profile.disable"
    target = CatalogStatusTarget(
        workspace_id=runtime.settings.workspace_id, entity="profile", entity_id=uuid4()
    )
    if variant == "foreign":
        target = target.model_copy(update={"workspace_id": uuid4()})
    elif variant == "wrong_action":
        action = "run.start"
    elif variant == "wrong_entity":
        target = target.model_copy(update={"entity": "resource"})
    elif variant == "unknown_harness":
        action, target = "runtime.describe", "unregistered"
    elif variant == "unknown_grant":
        grant_id = uuid4()
    elif variant == "background":
        grant_id = runtime.background_identity.grant_id
    elif variant in {"expired", "revoked"}:
        row = sessions.sessions[grant_id]
        sessions.sessions[grant_id] = replace(
            row,
            **{
                "expires_at" if variant == "expired" else "revoked_at": datetime.now(
                    UTC
                )
                - timedelta(seconds=1),
            },
        )
    monkeypatch.setattr(
        runtime.agents.runtimes._registry,
        "get",
        lambda *args: pytest.fail("Denied registry read"),
    )
    monkeypatch.setattr(
        runtime.agents.catalog._store,
        "set_profile_enabled",
        lambda *args: pytest.fail("Denied status write"),
    )
    with pytest.raises(AgentError) as error:
        runtime.authority.require(grant_id, action, target)
    assert error.value.code == "permission_denied"


@pytest.mark.parametrize(
    "fields,action",
    [
        ({"entity": "run"}, "run.enable"),
        ({"entity_id": "not-a-uuid"}, "profile.disable"),
        ({"workspace_id": "not-a-uuid"}, "profile.disable"),
    ],
)
def test_composed_catalog_target_rejects_forged_contract_fields(
    management_runtime, fields, action
):
    runtime, _, issue = management_runtime
    target = CatalogStatusTarget(
        workspace_id=runtime.settings.workspace_id, entity="profile", entity_id=uuid4()
    ).model_copy(update=fields)
    with pytest.raises(AgentError) as error:
        runtime.authority.require(issue.session_id, action, target)
    assert error.value.code == "permission_denied"


@pytest.mark.parametrize(
    "kind,target_type",
    [
        ("assignment", "subtask"),
        ("assignment", "worker_item"),
        ("execution", None),
        ("planning", None),
        ("profile", None),
    ],
)
def test_composed_human_work_read_targets_reach_both_finite_mappings(
    management_runtime, kind, target_type
):
    runtime, _, issue = management_runtime
    workspace = runtime.settings.workspace_id
    target = (
        ProfileWorkTarget(workspace, uuid4(), runtime.settings.local_principal_id)
        if kind == "profile"
        else ProfileWorkRunTarget(
            workspace, kind, uuid4(), runtime.settings.local_principal_id, target_type
        )
    )
    runtime.request_authorizer.require(issue.session_id, "assignment.read", target)
    runtime.authority.require(issue.session_id, "assignment.read", target)


@pytest.mark.parametrize(
    "variant",
    [
        "foreign",
        "principal",
        "wrong_action",
        "background",
        "unknown",
        "revoked",
        "expired",
        "forged_id",
        "forged_kind",
        "forged_discriminator",
    ],
)
def test_composed_work_read_denies_before_cp_or_native(
    management_runtime, monkeypatch, variant
):
    runtime, sessions, issue = management_runtime
    target = ProfileWorkRunTarget(
        runtime.settings.workspace_id,
        "planning",
        uuid4(),
        runtime.settings.local_principal_id,
    )
    grant, action = issue.session_id, "assignment.read"
    if variant == "foreign":
        target = replace(target, workspace_id=uuid4())
    elif variant == "principal":
        target = replace(target, principal_id="foreign")
    elif variant == "wrong_action":
        action = "run.start"
    elif variant in {"background", "unknown"}:
        grant = (
            runtime.background_identity.grant_id if variant == "background" else uuid4()
        )
    elif variant in {"revoked", "expired"}:
        row = sessions.sessions[grant]
        sessions.sessions[grant] = replace(
            row,
            **{
                "revoked_at" if variant == "revoked" else "expires_at": datetime.now(
                    UTC
                )
                - timedelta(seconds=1)
            },
        )
    else:
        # A subclass can bypass dataclass construction; the host still checks every field.
        class ForgedReadTarget(ProfileWorkRunTarget):
            def __post_init__(self):
                pass

        fields = dict(
            workspace_id=target.workspace_id,
            kind=target.kind,
            work_id=target.work_id,
            principal_id=target.principal_id,
        )
        fields.update(
            {"work_id": "invalid"}
            if variant == "forged_id"
            else {"kind": "other"}
            if variant == "forged_kind"
            else {"target_type": "subtask"}
        )
        target = ForgedReadTarget(**fields)
    monkeypatch.setattr(
        runtime.control_plane.execution_authorizer,
        "require",
        lambda *args: pytest.fail("Nonhuman read reached CP execution authority"),
    )
    monkeypatch.setattr(
        runtime.agents.execution,
        "get_run",
        lambda *args, **kwargs: pytest.fail("Denied native read"),
    )
    with pytest.raises(AgentError) as error:
        runtime.authority.require(grant, action, target)
    assert error.value.code == "permission_denied"


@pytest.mark.parametrize(
    "method",
    [
        "read_assignment",
        "read_agent_execution",
        "list_profile_work",
        "read_profile_work_run",
    ],
)
def test_public_cp_work_methods_deny_nonhuman_before_reader(management_runtime, method):
    runtime, _, _ = management_runtime
    workspace, work_id = runtime.settings.workspace_id, uuid4()
    args = (
        (workspace, "subtask", work_id)
        if method == "read_assignment"
        else (workspace, "planning", work_id)
        if method == "read_profile_work_run"
        else (workspace, work_id)
    )
    with pytest.raises(AgentError) as error:
        getattr(runtime.control_plane, method)(
            *args, grant_id=runtime.background_identity.grant_id
        )
    assert error.value.code == "permission_denied"


@pytest.mark.parametrize("kind", ["assignment", "execution", "work", "summary"])
def test_real_cp_work_routes_use_current_cookie_and_safe_schemas(
    management_runtime, monkeypatch, kind
):
    from fastapi.testclient import TestClient
    from sidera_control_plane.domain.assignment_views import AgentExecutionView
    from sidera_control_plane.persistence.assignment_read_store import (
        PostgresAssignmentReadStore,
    )

    runtime, _, issue = management_runtime
    workspace, work_id = runtime.settings.workspace_id, uuid4()
    calls = []
    monkeypatch.setattr(
        PostgresAssignmentReadStore,
        "read_assignment",
        lambda *args: calls.append("assignment") or None,
    )
    monkeypatch.setattr(
        PostgresAssignmentReadStore,
        "list_profile_work",
        lambda *args: calls.append("work") or (),
    )
    monkeypatch.setattr(
        PostgresAssignmentReadStore,
        "read_original_run",
        lambda *args, **kwargs: calls.append("summary") or None,
    )
    monkeypatch.setattr(
        PostgresAssignmentReadStore,
        "read_execution",
        lambda *args: (
            calls.append("execution")
            or AgentExecutionView(
                workspace_id=workspace,
                execution_id=work_id,
                root_task_id=None,
                subtask_id=uuid4(),
                worker_item_id=None,
                status="running",
                updated_at=datetime.now(UTC),
                profile_ref=None,
                binding_version=None,
                run_id=None,
                link_status=None,
                event_cursor=None,
            )
        ),
    )
    paths = {
        "assignment": f"assignments/subtask/{work_id}",
        "execution": f"agent-executions/{work_id}",
        "work": f"profiles/{work_id}/work",
        "summary": f"profile-work/planning/{work_id}/run",
    }
    client = TestClient(runtime.control_plane.app, base_url=runtime.settings.local_url)
    try:
        client.cookies.set("sidera_local_session", issue.token.get_secret_value())
        response = client.get(f"/api/v1/workspaces/{workspace}/{paths[kind]}")
        assert response.status_code == 200, response.text
        assert calls == [kind]
        assert not any(
            key in response.text
            for key in (
                "grant_id",
                "authority_id",
                "start_request",
                "preparation",
                "fencing_token",
            )
        )
        document = runtime.control_plane.app.openapi()
        assert all(
            operation in str(document["paths"])
            for operation in (
                "readAgentAssignment",
                "readAgentExecution",
                "listAgentProfileWork",
                "readAgentProfileWorkRun",
            )
        )
    finally:
        client.close()


@pytest.mark.parametrize(
    "path",
    [
        "assignments/planning",
        "profile-work/assignment",
        "profile-work/execution?target_type=subtask",
        "profile-work/planning?target_type=worker_item",
    ],
)
def test_real_cp_work_routes_reject_wrong_discriminators_before_store(
    management_runtime, monkeypatch, path
):
    from fastapi.testclient import TestClient
    from sidera_control_plane.persistence.assignment_read_store import (
        PostgresAssignmentReadStore,
    )

    runtime, _, issue = management_runtime
    monkeypatch.setattr(
        PostgresAssignmentReadStore,
        "read_assignment",
        lambda *args: pytest.fail("Invalid assignment lookup"),
    )
    monkeypatch.setattr(
        PostgresAssignmentReadStore,
        "read_original_run",
        lambda *args, **kwargs: pytest.fail("Invalid original lookup"),
    )
    base, _, query = path.partition("?")
    suffix = (
        f"{base}/{uuid4()}"
        if base.startswith("assignments")
        else f"{base}/{uuid4()}/run"
    )
    client = TestClient(runtime.control_plane.app, base_url=runtime.settings.local_url)
    try:
        client.cookies.set("sidera_local_session", issue.token.get_secret_value())
        response = client.get(
            f"/api/v1/workspaces/{runtime.settings.workspace_id}/{suffix}"
            + (f"?{query}" if query else "")
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    finally:
        client.close()
