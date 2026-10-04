import importlib
import importlib.util
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sidera_agents.errors import AgentError

from sidera_control_plane.api.dependencies import get_database
from sidera_control_plane.application.assignments import TrustedAssignmentIdentity
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.commands import CommandEnvelope
from tests.unit.test_command_service import (
    FakeCommandRepository,
    FakeUnitOfWork,
    RecordingHandler,
)


def runtime_module():
    name = "sidera_control_plane.composition.native_runtime"
    assert importlib.util.find_spec(name) is not None, "Public CP runtime is missing"
    return importlib.import_module(name)


def test_cancelled_public_http_request_releases_lifetime_counter():
    import asyncio

    import httpx

    workspace = uuid4()
    authority, native = RequestAuthority(workspace), NoNativeCalls()
    runtime = runtime_module().create_control_plane_runtime(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused"
        ),
        native,
        native,
        native,
        native,
        authority,
    )

    async def exercise():
        entered = asyncio.Event()
        cleaned = []

        @runtime.app.get("/api/v1/workspaces/{workspace_id}/cancelled-request")
        async def handler():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runtime.app), base_url="http://controlled"
        ) as client:
            request = asyncio.create_task(
                client.get(
                    f"/api/v1/workspaces/{workspace}/cancelled-request",
                    headers={"X-Trusted-Test": "human"},
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert runtime._active_work == runtime._regular_work == 1
            finally:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            assert cleaned == [True]
            assert runtime._active_work == runtime._regular_work == 0

    try:
        asyncio.run(exercise())
        assert (
            runtime.close(grace_seconds=0) is True
            and runtime.close(grace_seconds=0) is True
        )
        assert not native.calls
    finally:
        runtime.close()


class NoNativeCalls:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            self.calls.append(name)
            pytest.fail("HTTP boundary invoked native " + name)

        return fail


class RequestAuthority:
    def __init__(self, workspace_id):
        self.workspace_id = workspace_id
        self.human = TrustedAssignmentIdentity("operator", uuid4(), "user")
        self.agent = TrustedAssignmentIdentity("assistant", uuid4(), "agent")
        self.identities = {"human": self.human, "agent": self.agent}
        self.calls = []
        self.revoked = False
        self.denied_resource = None

    def authenticate_request(self, request):
        return self.identities.get(request.headers.get("X-Trusted-Test"))

    def identity_for_grant(self, grant_id):
        return next(
            (
                identity
                for identity in self.identities.values()
                if identity.grant_id == grant_id
            ),
            None,
        )

    def require(self, grant_id, action, target):
        self.calls.append((grant_id, action, target))
        if (
            self.revoked
            or target.workspace_id != self.workspace_id
            or any(
                value == str(self.denied_resource)
                for _, value in target.path_parameters
            )
        ):
            raise AgentError(
                "permission_denied", "PRIVATE credential/DSN must not escape"
            )


@pytest.fixture
def native_http():
    workspace_id = uuid4()
    authority = RequestAuthority(workspace_id)
    native = NoNativeCalls()
    settings = Settings(
        database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
        local_actor_type="user",
        local_actor_id="operator",
    )
    build = runtime_module().create_control_plane_runtime
    runtime = build(settings, native, native, native, native, authority)
    business_calls = []

    def no_database():
        business_calls.append("database")
        pytest.fail("Denied request reached the database dependency")

    runtime.app.dependency_overrides[get_database] = no_database
    try:
        with TestClient(runtime.app) as client:
            yield SimpleNamespace(
                runtime=runtime,
                client=client,
                workspace_id=workspace_id,
                authority=authority,
                native=native,
                business_calls=business_calls,
                settings=settings,
            )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("method", "route"),
    (
        ("GET", "/api/v1/tasks?workspace_id={workspace}"),
        ("GET", "/api/v1/modules?workspace_id={workspace}"),
        ("GET", "/api/v1/workspaces/{workspace}/commands/{resource}"),
        ("GET", "/api/v1/workspaces/{workspace}/artifacts/{resource}/content"),
        ("GET", "/api/v1/workspaces/{workspace}/context-packages/{resource}"),
        (
            "GET",
            "/api/v1/tasks/{resource}/executions/{resource}/sessions/{resource}/trace?workspace_id={workspace}",
        ),
        ("GET", "/api/v1/workspaces/{workspace}/projection-changes"),
        ("POST", "/api/v1/commands"),
        (
            "POST",
            "/api/v1/workspaces/{workspace}/worker-transitions/{resource}/reconcile",
        ),
        (
            "POST",
            "/api/v1/workspaces/{workspace}/worker-transitions/{resource}/confirm-stopped",
        ),
        (
            "POST",
            "/api/v1/workspaces/{workspace}/worker-transitions/{resource}/recover",
        ),
        (
            "POST",
            "/api/v1/workspaces/{workspace}/context-packages/{resource}/materialize",
        ),
        ("POST", "/api/v1/workspaces/{workspace}/notification-deliveries/claims"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
    ),
)
def test_native_http_without_verified_identity_denies_before_business(
    native_http, method, route
):
    s = native_http
    response = s.client.request(
        method, route.format(workspace=s.workspace_id, resource=uuid4()), json={}
    )
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "authentication_required",
            "message": "authentication required",
            "details": {},
        }
    }
    assert not s.business_calls and not s.native.calls and not s.authority.calls
    assert "PRIVATE" not in response.text
    assert s.client.get("/healthz").json() == {
        "status": "ok",
        "service": "sidera-control-plane",
    }


def test_native_request_target_uses_matched_route_and_preserves_query_values(
    native_http,
):
    s = native_http
    resource_id = uuid4()

    @s.runtime.app.get(
        "/api/v1/workspaces/{workspace_id}/authority-probe/{resource_id}"
    )
    def probe(request: Request):
        s.business_calls.append("probe")
        return {"principal": request.state.control_plane_identity.principal_id}

    response = s.client.get(
        f"/api/v1/workspaces/{s.workspace_id}/authority-probe/{resource_id}?tag=a&tag=b",
        headers={"X-Trusted-Test": "human", "X-Route": "/forged"},
    )
    assert response.json() == {"principal": "operator"}
    grant, action, target = s.authority.calls[-1]
    assert grant == s.authority.human.grant_id and action == "control_plane.query"
    assert (
        target.route
        == "/api/v1/workspaces/{workspace_id}/authority-probe/{resource_id}"
    )
    assert target.path_parameters == (
        ("workspace_id", str(s.workspace_id)),
        ("resource_id", str(resource_id)),
    )
    assert target.query_parameters == (("tag", "a"), ("tag", "b"))
    assert target.command is None and target.principal_id == "operator"
    assert s.business_calls == ["probe"] and not s.native.calls
    s.authority.denied_resource = resource_id
    denied = s.client.get(
        f"/api/v1/workspaces/{s.workspace_id}/authority-probe/{resource_id}",
        headers={"X-Trusted-Test": "human"},
    )
    assert denied.status_code == 403 and "PRIVATE" not in denied.text
    assert denied.json() == {
        "error": {
            "code": "permission_denied",
            "message": "permission denied",
            "details": {},
        }
    }
    assert s.business_calls == ["probe"]


def test_native_dependency_and_outer_auth_use_the_same_safe_envelope(native_http):
    s = native_http
    s.authority.revoked = True
    responses = (
        s.client.get(
            f"/api/v1/workspaces/{s.workspace_id}/commands/{uuid4()}",
            headers={"X-Trusted-Test": "human"},
        ),
        s.client.get(
            f"/openapi.json?workspace_id={s.workspace_id}",
            headers={"X-Trusted-Test": "human"},
        ),
    )
    for response in responses:
        assert response.status_code == 403
        assert response.json() == {
            "error": {
                "code": "permission_denied",
                "message": "permission denied",
                "details": {},
            }
        }
        assert "PRIVATE" not in response.text
    assert not s.business_calls and not s.native.calls


def test_native_validation_and_closed_admission_errors_remain_safe(native_http):
    s = native_http
    invalid = s.client.post(
        "/api/v1/commands", json={}, headers={"X-Trusted-Test": "human"}
    )
    assert invalid.status_code == 422
    assert invalid.json() == {
        "error": {
            "code": "validation_error",
            "message": "request validation failed",
            "details": {},
        }
    }
    s.runtime.close()
    closed = s.client.get(
        f"/api/v1/tasks?workspace_id={s.workspace_id}",
        headers={"X-Trusted-Test": "human"},
    )
    assert closed.status_code == 503
    assert closed.json() == {
        "error": {
            "code": "control_plane_closed",
            "message": "control plane is closed",
            "details": {},
        }
    }
    assert not s.business_calls and not s.native.calls


@pytest.mark.parametrize(
    ("status", "code", "message"),
    (
        (401, "authentication_required", "authentication required"),
        (403, "permission_denied", "permission denied"),
        (422, "validation_error", "request validation failed"),
        (503, "service_unavailable", "service unavailable"),
    ),
)
def test_native_unknown_http_error_detail_and_headers_are_not_disclosed(
    native_http, status, code, message
):
    s = native_http

    @s.runtime.app.get("/api/v1/workspaces/{workspace_id}/private-error")
    def private_error():
        raise HTTPException(
            status,
            detail={"code": [], "message": "PRIVATE credential/DSN"},
            headers={"X-Private": "PRIVATE"},
        )

    response = s.client.get(
        f"/api/v1/workspaces/{s.workspace_id}/private-error",
        headers={"X-Trusted-Test": "human"},
    )
    assert response.status_code == status
    assert response.json() == {
        "error": {"code": code, "message": message, "details": {}}
    }
    assert "X-Private" not in response.headers and "PRIVATE" not in response.text


def test_explicit_legacy_app_keeps_its_http_exception_shape():
    from sidera_control_plane.api.app import create_app

    app = create_app(Settings(database_url="postgresql+psycopg://unused"))

    @app.get("/legacy-error")
    def legacy_error():
        raise HTTPException(403, detail={"code": "legacy_denied"})

    with TestClient(app) as client:
        response = client.get("/legacy-error")
    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "legacy_denied"}}


@pytest.mark.parametrize(
    "mismatch",
    ("actor_id", "actor_type", "workspace", "agent_human", "configured_actor_type"),
)
def test_native_commands_cannot_forge_identity_or_human_approval(native_http, mismatch):
    s = native_http
    body = {
        "command_id": str(uuid4()),
        "workspace_id": str(s.workspace_id),
        "command_type": "CompleteTask",
        "target_type": "task",
        "actor": {"type": "user", "id": "operator"},
        "target_id": str(uuid4()),
        "expected_version": 1,
        "idempotency_key": "complete-exact-task",
        "correlation_id": str(uuid4()),
        "payload": {},
    }
    identity = "human"
    if mismatch == "actor_id":
        body["actor"]["id"] = "untrusted"
    elif mismatch == "actor_type":
        body["actor"]["type"] = "agent"
    elif mismatch == "workspace":
        body["workspace_id"] = str(uuid4())
    elif mismatch == "configured_actor_type":
        s.settings.local_actor_type = "local"
    else:
        identity = "agent"
        body["actor"] = {"type": "agent", "id": "assistant"}
    response = s.client.post(
        "/api/v1/commands", json=body, headers={"X-Trusted-Test": identity}
    )
    assert response.status_code == 403
    assert not s.business_calls and not s.native.calls


def test_native_command_replay_rechecks_authority_before_receipt_lookup(native_http):
    s = native_http
    module = importlib.import_module(
        "sidera_control_plane.application.request_authority"
    )
    reads = []

    class Commands(FakeCommandRepository):
        def find_by_id(self, *args):
            reads.append("by_id")
            return super().find_by_id(*args)

        def find_duplicate(self, *args):
            reads.append("by_key")
            return super().find_duplicate(*args)

    commands = Commands()
    handler = RecordingHandler()
    envelope = CommandEnvelope(
        uuid4(),
        s.workspace_id,
        "RecordThing",
        "task",
        "user",
        "operator",
        None,
        {},
        None,
        "original-command",
        uuid4(),
    )
    target = module.ControlPlaneRequestTarget(
        s.workspace_id, "operator", "POST", "/api/v1/commands", (), (), envelope
    )
    service = CommandService(
        lambda: FakeUnitOfWork(commands),
        (handler,),
        preflight=lambda uow, command: module.require_command_authority(
            s.authority, s.authority.human, target, s.settings, command
        ),
    )
    result = service.handle(envelope)
    assert handler.calls == 1 and reads == ["by_id", "by_key"]
    assert service.handle(replace(envelope, correlation_id=uuid4())) == result
    before = list(reads)
    s.authority.revoked = True
    with pytest.raises(HTTPException) as denied:
        service.handle(envelope)
    assert denied.value.status_code == 403
    assert reads == before and handler.calls == 1


@pytest.mark.parametrize("suffix", ("reconcile", "confirm-stopped", "recover"))
@pytest.mark.parametrize(
    "denial", ("actor", "workspace", "target", "command_type", "resource")
)
def test_actual_transition_routes_authorize_exact_command_before_services(
    native_http, suffix, denial
):
    s = native_http
    transition_id = uuid4()
    types = {
        "reconcile": "ReconcileWorkerTransition",
        "confirm-stopped": "ConfirmWorkerTransitionStopped",
        "recover": "RecoverWorkerTransition",
    }
    payload = {
        "reason": "original operation evidence",
        "evidence_refs": ["evidence://original"],
    }
    if suffix == "recover":
        payload["strategy"] = "reconcile_adapter"
    body = {
        "command_id": str(uuid4()),
        "workspace_id": str(s.workspace_id),
        "command_type": types[suffix],
        "target_type": "worker_transition",
        "actor": {"type": "user", "id": "operator"},
        "target_id": str(transition_id),
        "expected_version": 1,
        "idempotency_key": "exact-transition",
        "correlation_id": str(uuid4()),
        "payload": payload,
    }
    if denial == "actor":
        body["actor"]["id"] = "forged"
    elif denial == "workspace":
        body["workspace_id"] = str(uuid4())
    elif denial == "target":
        body["target_id"] = str(uuid4())
    elif denial == "command_type":
        body["command_type"] = (
            "ConfirmWorkerTransitionStopped"
            if suffix == "reconcile"
            else "ReconcileWorkerTransition"
        )
        payload.pop("strategy", None)
    else:
        s.authority.denied_resource = transition_id
    response = s.client.post(
        f"/api/v1/workspaces/{s.workspace_id}/worker-transitions/{transition_id}/{suffix}",
        json=body,
        headers={"X-Trusted-Test": "human"},
    )
    assert response.status_code == 403
    assert not s.business_calls and not s.native.calls
    if denial == "resource":
        _, action, target = s.authority.calls[-1]
        assert action == "control_plane.command:" + types[suffix]
        assert (
            target.route
            == "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/"
            + suffix
        )
        assert target.command.id.hex == body["command_id"].replace("-", "")
        assert target.path_parameters == (
            ("workspace_id", str(s.workspace_id)),
            ("worker_transition_id", str(transition_id)),
        )


@pytest.mark.parametrize(
    "route",
    (
        "/api/v1/workspaces/{workspace}/context-packages/{resource}/materialize",
        "/api/v1/workspaces/{workspace}/notification-deliveries/claims",
    ),
)
def test_native_noncommand_writes_recheck_mutation_scope_before_dependencies(
    native_http, route
):
    s = native_http
    s.authority.revoked = True
    response = s.client.post(
        route.format(workspace=s.workspace_id, resource=uuid4()),
        json={},
        headers={"X-Trusted-Test": "human"},
    )
    assert response.status_code == 403
    assert s.authority.calls[-1][1] == "control_plane.mutate"
    assert not s.business_calls and not s.native.calls


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("principal_id", ""),
        ("grant_id", "untrusted-uuid"),
        ("actor_type", "worker"),
        ("actor_type", []),
        ("actor_type", {}),
    ),
)
def test_malformed_host_identity_is_unauthenticated(native_http, field, value):
    s = native_http
    s.authority.identities["human"] = replace(s.authority.human, **{field: value})
    response = s.client.get(
        f"/api/v1/tasks?workspace_id={s.workspace_id}",
        headers={"X-Trusted-Test": "human"},
    )
    assert response.status_code == 401
    assert not s.business_calls and not s.native.calls and not s.authority.calls


def test_restore_mode_does_not_disclose_write_mode_before_authentication():
    native = NoNativeCalls()
    settings = Settings(
        database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
        restore_read_only=True,
        restore_schema_validation=False,
    )
    runtime = runtime_module().create_control_plane_runtime(
        settings, native, native, native, native
    )
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/api/v1/commands", json={})
            assert response.status_code == 401
            assert not native.calls
    finally:
        runtime.close()
