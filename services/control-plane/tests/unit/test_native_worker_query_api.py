from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module, util
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sidera_agents.contracts import ProfileRef, RunEvent
from sidera_agents.errors import AgentError

from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.dependencies import get_trusted_assignment_identity
from sidera_control_plane.api.errors import (
    install_error_handlers,
    install_native_http_error_handler,
)
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.assignment_views import AgentExecutionView
from sidera_control_plane.domain.native_worker_views import (
    NativeWorkerContextAddress,
    NativeWorkerContextView,
    NativeWorkerTraceView,
)

WORKSPACE_ID = UUID("22000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("42000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("52000000-0000-0000-0000-000000000001")
GRANT_ID = UUID("92000000-0000-0000-0000-000000000001")
PROFILE_REF = ProfileRef(
    workspace_id=WORKSPACE_ID,
    profile_id=UUID("62000000-0000-0000-0000-000000000001"),
    revision=2,
)
SESSION_ID = UUID("d2000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("72000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _api_module():
    name = "sidera_control_plane.api.native_worker_queries"
    try:
        spec = util.find_spec(name)
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, f"Required native worker API module is missing: {name}"
    return import_module(name)


class _Reads:
    def __init__(self, *, error: Exception | None = None, context=None, trace=None):
        self.error = error
        self.context = context
        self.trace = trace
        self.calls = []

    def read_native_worker_context(self, workspace_id, worker_item_id, *, grant_id):
        self.calls.append(("context", workspace_id, worker_item_id, grant_id))
        if self.error is not None:
            raise self.error
        return self.context

    def read_native_worker_trace(
        self,
        workspace_id,
        worker_item_id,
        execution_id,
        *,
        grant_id,
        after_sequence=0,
        limit=100,
    ):
        self.calls.append(
            (
                "trace",
                workspace_id,
                worker_item_id,
                execution_id,
                grant_id,
                after_sequence,
                limit,
            )
        )
        if self.error is not None:
            raise self.error
        return self.trace


def _client(
    identity: ControlPlaneIdentity | None,
    reads: _Reads | None,
    *,
    native: bool = False,
):
    module = _api_module()
    app = FastAPI()
    app.include_router(module.router)
    install_error_handlers(app)
    if native:
        install_native_http_error_handler(app)
    app.dependency_overrides[get_trusted_assignment_identity] = lambda: identity
    dependency_calls = []

    def provide_reads():
        dependency_calls.append("reads")
        return reads

    app.dependency_overrides[module.get_native_worker_reads] = provide_reads
    return TestClient(app), dependency_calls


def _context_view() -> NativeWorkerContextView:
    execution = AgentExecutionView(
        workspace_id=WORKSPACE_ID,
        execution_id=EXECUTION_ID,
        root_task_id=UUID("12000000-0000-0000-0000-000000000001"),
        subtask_id=UUID("32000000-0000-0000-0000-000000000001"),
        worker_item_id=WORKER_ITEM_ID,
        status="running",
        updated_at=NOW,
        profile_ref=PROFILE_REF,
        binding_version=2,
        run_id=UUID("82000000-0000-0000-0000-000000000001"),
        link_status="running",
        event_cursor=3,
    )
    return NativeWorkerContextView(
        workspace_id=WORKSPACE_ID,
        worker_item_id=WORKER_ITEM_ID,
        execution_mode="native_profile",
        worker_item_version=11,
        binding=None,
        execution=execution,
        execution_version=7,
        context=NativeWorkerContextAddress(
            worker_item_id=WORKER_ITEM_ID,
            plan_generation=6,
            context_version=4,
            execution_id=EXECUTION_ID,
            session_id=SESSION_ID,
            session_version=5,
            worker_id=WORKER_ID,
            execution_mode="native_profile",
            binding_version=2,
            run_id=execution.run_id,
        ),
        input_enabled=False,
        read_only_reason="native_input_closed",
        run=None,
        as_of=NOW,
    )


def _trace_view() -> NativeWorkerTraceView:
    run_id = UUID("82000000-0000-0000-0000-000000000001")
    return NativeWorkerTraceView(
        workspace_id=WORKSPACE_ID,
        worker_item_id=WORKER_ITEM_ID,
        execution_id=EXECUTION_ID,
        run_id=run_id,
        profile_ref=PROFILE_REF,
        events=(
            RunEvent(
                run_id=run_id,
                sequence=2,
                kind="agent_message",
                occurred_at=NOW,
                operation_id=None,
                response_id=None,
                data={"text": "safe"},
            ),
        ),
        next_sequence=2,
        has_more=False,
    )


def test_native_read_routes_are_mounted_with_frozen_operation_ids():
    app = create_app(Settings(database_url="postgresql+psycopg://unused/unused"))
    operations = {
        (path, operation["operationId"])
        for path, methods in app.openapi()["paths"].items()
        for operation in methods.values()
        if "operationId" in operation
    }
    assert (
        "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/native-context",
        "readNativeWorkerContext",
    ) in operations
    assert (
        "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/executions/{execution_id}/native-trace",
        "readNativeWorkerTrace",
    ) in operations


def test_agent_identity_is_denied_before_native_read_dependency_or_store_access():
    identity = ControlPlaneIdentity("agent", GRANT_ID, "agent")
    reads = _Reads()
    client, dependency_calls = _client(identity, reads)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/native-context"
    )
    assert response.status_code == 403
    assert reads.calls == []
    assert dependency_calls == []


def test_missing_native_read_dependency_returns_safe_503():
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    client, _ = _client(identity, None)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/native-context"
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_context_route_serializes_only_the_native_context_view():
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    client, _ = _client(identity, _Reads(context=_context_view()))
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/native-context"
    )
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "workspace_id",
        "worker_item_id",
        "execution_mode",
        "worker_item_version",
        "binding",
        "execution",
        "execution_version",
        "context",
        "input_enabled",
        "read_only_reason",
        "run",
        "as_of",
    }
    assert set(payload["context"]) == set(NativeWorkerContextAddress.model_fields)
    assert "grant_id" not in response.text
    assert "native_handle" not in response.text


def test_trace_route_serializes_public_events_and_forwards_bounded_cursor():
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    reads = _Reads(trace=_trace_view())
    client, _ = _client(identity, reads)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/executions/{EXECUTION_ID}/native-trace",
        params={"after_sequence": 1, "limit": 2},
    )
    assert response.status_code == 200
    assert reads.calls == [
        ("trace", WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID, GRANT_ID, 1, 2)
    ]
    assert response.json()["events"][0]["data"] == {"text": "safe"}
    assert "grant_id" not in response.text


@pytest.mark.parametrize("native", [False, True], ids=["ordinary", "native"])
def test_trace_unsupported_has_safe_fixed_http_code(native):
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    reads = _Reads(
        error=AgentError("native_trace_unsupported", "private-native-handle-secret")
    )
    client, _ = _client(identity, reads, native=native)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/executions/{EXECUTION_ID}/native-trace"
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "native_trace_unsupported"
    assert "private-native-handle-secret" not in response.text


@pytest.mark.parametrize("native", [False, True], ids=["ordinary", "native"])
def test_trace_ordinary_read_error_stays_generic_unavailable(native):
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    reads = _Reads(error=AgentError("query_unavailable", "private-store-secret"))
    client, _ = _client(identity, reads, native=native)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/executions/{EXECUTION_ID}/native-trace"
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "native_trace_unsupported" not in response.text
    assert "private-store-secret" not in response.text


@pytest.mark.parametrize("native", [False, True], ids=["ordinary", "native"])
def test_context_unsupported_error_stays_generic_unavailable(native):
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    reads = _Reads(
        error=AgentError("native_trace_unsupported", "private-native-handle-secret")
    )
    client, _ = _client(identity, reads, native=native)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/native-context"
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "native_trace_unsupported" not in response.text
    assert "private-native-handle-secret" not in response.text


@pytest.mark.parametrize("native", [False, True], ids=["ordinary", "native"])
def test_agent_identity_is_denied_before_native_trace_read_dependency_or_store(native):
    identity = ControlPlaneIdentity("agent", GRANT_ID, "agent")
    reads = _Reads()
    client, dependency_calls = _client(identity, reads, native=native)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/executions/{EXECUTION_ID}/native-trace"
    )
    assert response.status_code == 403
    assert reads.calls == []
    assert dependency_calls == []


def test_workspace_denial_is_safe_and_does_not_expose_reader_details():
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    reads = _Reads(error=AgentError("permission_denied", "private-workspace-secret"))
    client, _ = _client(identity, reads)
    other_workspace = UUID("f2000000-0000-0000-0000-000000000010")
    response = client.get(
        f"/api/v1/workspaces/{other_workspace}/worker-items/{WORKER_ITEM_ID}/native-context"
    )
    assert response.status_code == 403
    assert "private-workspace-secret" not in response.text


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("native-context", {"grant_id": str(GRANT_ID)}),
        ("native-context", {"run_id": "f2000000-0000-0000-0000-000000000001"}),
        (
            "executions/{execution_id}/native-trace",
            {"run_id": "f2000000-0000-0000-0000-000000000001"},
        ),
        ("executions/{execution_id}/native-trace", {"native_handle": "private"}),
    ],
)
def test_unknown_or_private_query_fields_are_rejected_before_read(path, query):
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    reads = _Reads()
    client, _ = _client(identity, reads)
    suffix = path.format(execution_id=EXECUTION_ID)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/{suffix}",
        params=query,
    )
    assert response.status_code == 422
    assert reads.calls == []


@pytest.mark.parametrize(
    "query",
    [{"after_sequence": -1}, {"limit": 0}, {"limit": 501}],
)
def test_trace_cursor_bounds_are_checked_before_read(query):
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    reads = _Reads()
    client, _ = _client(identity, reads)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/executions/{EXECUTION_ID}/native-trace",
        params=query,
    )
    assert response.status_code == 422
    assert reads.calls == []


def test_reader_errors_are_safe_and_do_not_expose_exception_text():
    identity = ControlPlaneIdentity("operator", GRANT_ID, "user")
    reads = _Reads(error=AgentError("query_unavailable", "private-authority-secret"))
    client, _ = _client(identity, reads)
    response = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}/native-context"
    )
    assert response.status_code == 503
    assert "private-authority-secret" not in response.text
    assert "grant_id" not in response.text


def test_app_construction_does_not_start_native_or_provider_work():
    app = create_app(Settings(database_url="postgresql+psycopg://unused/unused"))
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []
    assert any(path.endswith("/native-context") for path in app.openapi()["paths"])
