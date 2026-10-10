import importlib.util
import sys
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sidera_agents.errors import AgentError


def product_module():
    assert importlib.util.find_spec("sidera_product") is not None, (
        "N17 must compose the actual public owners"
    )
    from sidera_product import composition

    return composition


def test_product_shares_actual_agent_public_objects_and_is_inert(
    settings_values, monkeypatch, no_database_connections
):
    from sidera_agents import bootstrap
    from sidera_control_plane.composition import native_runtime

    composition = product_module()
    from sidera_product.config import ProductSettings

    captures = []
    real_factory = native_runtime.create_control_plane_runtime

    def capture(*args, **kwargs):
        captures.append((args, kwargs))
        return real_factory(*args, **kwargs)

    monkeypatch.setattr(native_runtime, "create_control_plane_runtime", capture)
    native_calls = []

    def forbidden(*args, **kwargs):
        native_calls.append((args, kwargs))
        raise AssertionError("Construction cannot start/probe native work")

    monkeypatch.setattr(bootstrap.AgentService, "start_background", forbidden)
    runtime = composition.build_product_runtime(
        ProductSettings.model_validate(settings_values)
    )
    try:
        assert len(captures) == 1
        args, kwargs = captures[0]
        passed = dict(
            zip(
                (
                    "settings",
                    "agent_execution",
                    "agent_catalog",
                    "agent_observations",
                    "source_reader",
                    "request_authorizer",
                ),
                args,
                strict=False,
            )
        )
        passed.update(kwargs)
        assert passed["agent_execution"] is runtime.agents.execution
        assert passed["agent_catalog"] is runtime.agents.catalog
        assert passed["agent_observations"] is runtime.agents.observations
        assert passed["agent_runtimes"] is runtime.agents.runtimes
        assert passed["source_reader"] is runtime.sources
        assert passed["request_authorizer"] is runtime.request_authorizer
        assert runtime.execution is runtime.agents.execution
        assert (
            runtime.authority.control_plane
            is runtime.control_plane.execution_authorizer
        )
        assert (
            runtime.sources.control_plane is runtime.control_plane.context_source_reader
        )
        assert native_calls == []
        assert not any(
            "akashic" in name or "main_agent_gateway" in name for name in sys.modules
        )
    finally:
        assert runtime.close(grace_seconds=0) is True


def test_internal_service_identity_is_stable_and_not_http_auth(
    settings_values, no_database_connections, monkeypatch
):
    composition = product_module()
    from sidera_product.config import ProductSettings

    first = composition.build_product_runtime(
        ProductSettings.model_validate(settings_values)
    )
    second = composition.build_product_runtime(
        ProductSettings.model_validate(settings_values)
    )
    queried = []
    grant_queries = []

    def missing_session(*, session_id=None, token_digest=None):
        assert session_id == UUID(int=789) and token_digest is None
        queried.append(session_id)
        return None

    def missing_grant(*, grant_id=None, bearer_digest=None):
        assert grant_id == UUID(int=789) and bearer_digest is None
        grant_queries.append(grant_id)
        return None

    monkeypatch.setattr(first.authority_store, "find_session", missing_session)
    monkeypatch.setattr(first.authority_store, "find_grant", missing_grant)
    try:
        assert first.background_identity == second.background_identity
        identity = first.background_identity
        assert identity.actor_type == "system"
        assert identity.principal_id == "n17-test-user"
        assert (
            first.request_authorizer.identity_for_grant(identity.grant_id) is identity
        )
        assert queried == []
        assert grant_queries == []
        assert first.request_authorizer.identity_for_grant(UUID(int=789)) is None
        assert queried == [UUID(int=789)]
        assert grant_queries == [UUID(int=789)]
        first.request_authorizer.require(
            identity.grant_id, "profile.list", identity_workspace(first)
        )
        with pytest.raises(AgentError, match="permission_denied"):
            first.request_authorizer.require(
                identity.grant_id, "profile.create", identity_workspace(first)
            )
        with pytest.raises(AgentError, match="permission_denied"):
            first.request_authorizer.require(
                identity.grant_id, "profile.list", UUID(int=321)
            )
        assert queried == [UUID(int=789)]
        assert grant_queries == [UUID(int=789)]
    finally:
        assert first.close(grace_seconds=0) is True
        assert second.close(grace_seconds=0) is True


def identity_workspace(runtime):
    return runtime.settings.workspace_id


def test_unbound_and_unknown_execution_or_sources_are_denied(settings_values):
    composition = product_module()
    from sidera_agents.contracts import ExecutionWorkspace, SourceRef

    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    host = composition.ProductRequestAuthorizer(settings)
    authority = composition.AuthorityRouter(host)
    sources = composition.SourceRouter()
    for target in (
        object(),
        ExecutionWorkspace(
            directory="/untrusted", writable=True, artifact_directory=None
        ),
    ):
        with pytest.raises(AgentError, match="permission_denied"):
            authority.require(UUID(int=678), "workspace.execute", target)
    with pytest.raises(AgentError, match="permission_denied"):
        sources.read(
            UUID(int=678), SourceRef(provider="unknown", key="data", version="1")
        )


def test_system_registration_never_authenticates_http_or_human_setup(
    settings_values, no_database_connections
):
    composition = product_module()
    from fastapi import Request
    from sidera_control_plane.composition.native_runtime import (
        ControlPlaneRequestTarget,
    )

    from sidera_product.config import ProductSettings

    runtime = composition.build_product_runtime(
        ProductSettings.model_validate(settings_values)
    )
    try:
        identity = runtime.background_identity
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/tasks",
                "headers": [
                    (b"authorization", ("Bearer " + str(identity.grant_id)).encode()),
                    (b"x-actor-id", identity.principal_id.encode()),
                ],
            }
        )
        assert runtime.request_authorizer.authenticate_request(request) is None
        target = ControlPlaneRequestTarget(
            runtime.settings.workspace_id,
            identity.principal_id,
            "POST",
            "/api/v1/commands",
            (),
            (),
        )
        for action in (
            "workspace.configure",
            "control_plane.query",
            "control_plane.mutate",
            "control_plane.command:ConfirmPlanningProposal",
        ):
            with pytest.raises(AgentError, match="permission_denied"):
                runtime.request_authorizer.require(identity.grant_id, action, target)
    finally:
        assert runtime.close(grace_seconds=0) is True


@pytest.mark.anyio
async def test_mount_keeps_cp_api_path_and_inert_health_denies_anonymous(
    settings_values,
    no_database_connections,
):
    product_module()
    from sidera_product.app import create_app
    from sidera_product.config import ProductSettings

    app = create_app(ProductSettings.model_validate(settings_values))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18080"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/api/v1/tasks")).status_code == 401
        response = await client.get("/product/v1/status")
        assert response.status_code == 200
        assert response.json()["execution"]["ready"] is False
        assert response.json()["knowledge"]["available"] is False
        assert response.json()["main_agent"]["available"] is False
        anonymous = await client.get(
            f"/agents/v1/workspaces/{app.state.product_runtime.settings.workspace_id}/profiles"
        )
        assert anonymous.status_code == 401
        assert anonymous.json()["detail"]["code"] == "local_session_required"
        invalid = await client.get("/agents/v1/workspaces/unknown/profiles")
        assert invalid.status_code == 422
    assert app.state.product_runtime.close(grace_seconds=0) is True


def _repair_request_target(settings):
    from sidera_control_plane.application.request_authority import (
        ControlPlaneRequestTarget,
    )
    from sidera_control_plane.domain.commands import CommandEnvelope

    command = CommandEnvelope(
        id=uuid4(),
        workspace_id=settings.workspace_id,
        command_type="RepairPlanningContext",
        target_type="planning_run",
        target_id=uuid4(),
        expected_version=4,
        actor_type="user",
        actor_id=settings.local_principal_id,
        idempotency_key=uuid4().hex,
        correlation_id=uuid4(),
        payload={
            "expected_planning_request_version": 7,
            "expected_work_context_version": 3,
            "work_context_revision_id": str(uuid4()),
            "work_context_content_hash": "a" * 64,
        },
    )
    return ControlPlaneRequestTarget(
        settings.workspace_id,
        settings.local_principal_id,
        "POST",
        "/api/v1/commands",
        (),
        (),
        command,
    )


def test_human_authorizer_admits_exact_repair_planning_context_command(settings_values):
    composition = product_module()
    from sidera_control_plane.application.assignments import TrustedAssignmentIdentity

    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    identity = TrustedAssignmentIdentity(
        settings.local_principal_id, uuid4(), actor_type="user"
    )
    host = composition.ProductRequestAuthorizer(settings)
    host.local_sessions = SimpleNamespace(
        identity_for_grant=lambda supplied: (
            identity if supplied == identity.grant_id else None
        )
    )
    host.require(
        identity.grant_id,
        "control_plane.command:RepairPlanningContext",
        _repair_request_target(settings),
    )


@pytest.mark.parametrize(
    "damage",
    [
        "target_workspace",
        "target_principal",
        "command_workspace",
        "command_principal",
        "actor_type",
        "action",
        "identity_type",
        "identity_principal",
    ],
)
def test_human_authorizer_refuses_foreign_repair_scope(settings_values, damage):
    composition = product_module()
    from sidera_control_plane.application.assignments import TrustedAssignmentIdentity

    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    identity = TrustedAssignmentIdentity(
        settings.local_principal_id, uuid4(), actor_type="user"
    )
    host = composition.ProductRequestAuthorizer(settings)
    target = _repair_request_target(settings)
    action = "control_plane.command:RepairPlanningContext"
    if damage.startswith("target_"):
        target = replace(
            target,
            **(
                {"workspace_id": uuid4()}
                if damage == "target_workspace"
                else {"principal_id": "foreign"}
            ),
        )
    elif damage in {"command_workspace", "command_principal", "actor_type"}:
        target = replace(
            target,
            command=replace(
                target.command,
                **{
                    "command_workspace": {"workspace_id": uuid4()},
                    "command_principal": {"actor_id": "foreign"},
                    "actor_type": {"actor_type": "agent"},
                }[damage],
            ),
        )
    elif damage == "action":
        action = "control_plane.command:RetryBlockedPlanningRun"
    else:
        identity = replace(
            identity,
            **(
                {"actor_type": "system"}
                if damage == "identity_type"
                else {"principal_id": "foreign"}
            ),
        )
    host.local_sessions = SimpleNamespace(identity_for_grant=lambda supplied: identity)
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(identity.grant_id, action, target)


@pytest.mark.parametrize("role", ["main", "worker", "planner"])
def test_task_tool_authorizer_never_admits_repair_planning_context(
    settings_values, tmp_path, role
):
    from tests.unit.test_task_tools import host_tool_fixture

    lab, _token, verified = host_tool_fixture(settings_values, tmp_path, role=role)
    target = _repair_request_target(lab.sessions.settings)
    target = replace(target, command=replace(target.command, actor_type="agent"))
    with pytest.raises(AgentError, match="permission_denied"):
        lab.host.require(
            verified.record.grant_id,
            "control_plane.command:RepairPlanningContext",
            target,
        )


def test_background_authorizer_never_admits_repair_planning_context(settings_values):
    composition = product_module()
    from sidera_product.config import ProductSettings

    host = composition.ProductRequestAuthorizer(
        ProductSettings.model_validate(settings_values)
    )
    target = _repair_request_target(host.settings)
    target = replace(target, command=replace(target.command, actor_type="system"))
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(
            host.background_identity.grant_id,
            "control_plane.command:RepairPlanningContext",
            target,
        )


def _failed_feedback_request_target(settings):
    target = _repair_request_target(settings)
    return replace(
        target,
        command=replace(
            target.command,
            command_type="ReviseFailedPlanningRequest",
            target_type="planning_request",
            payload={
                "failed_planning_run_id": str(uuid4()),
                "expected_failed_planning_run_version": 4,
                "expected_work_context_version": 3,
                "work_context_revision_id": str(uuid4()),
                "work_context_content_hash": "a" * 64,
                "feedback": "Keep four workers and explicit verification.",
            },
        ),
    )


def test_human_authorizer_admits_exact_failed_planning_feedback(settings_values):
    composition = product_module()
    from sidera_control_plane.application.assignments import TrustedAssignmentIdentity

    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    identity = TrustedAssignmentIdentity(
        settings.local_principal_id, uuid4(), actor_type="user"
    )
    host = composition.ProductRequestAuthorizer(settings)
    host.local_sessions = SimpleNamespace(
        identity_for_grant=lambda supplied: (
            identity if supplied == identity.grant_id else None
        )
    )
    host.require(
        identity.grant_id,
        "control_plane.command:ReviseFailedPlanningRequest",
        _failed_feedback_request_target(settings),
    )


@pytest.mark.parametrize(
    "damage",
    [
        "target_workspace",
        "target_principal",
        "command_workspace",
        "command_principal",
        "actor_type",
        "action",
        "identity_type",
        "identity_principal",
        "closed_session",
        "missing_session",
    ],
)
def test_human_authorizer_refuses_foreign_failed_planning_feedback(
    settings_values, damage
):
    composition = product_module()
    from sidera_control_plane.application.assignments import TrustedAssignmentIdentity

    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    identity = TrustedAssignmentIdentity(
        settings.local_principal_id, uuid4(), actor_type="user"
    )
    host = composition.ProductRequestAuthorizer(settings)
    target = _failed_feedback_request_target(settings)
    action = "control_plane.command:ReviseFailedPlanningRequest"
    grant_id = identity.grant_id
    if damage in {"target_workspace", "target_principal"}:
        target = replace(
            target,
            **(
                {"workspace_id": uuid4()}
                if damage == "target_workspace"
                else {"principal_id": "foreign"}
            ),
        )
    elif damage in {"command_workspace", "command_principal", "actor_type"}:
        target = replace(
            target,
            command=replace(
                target.command,
                **{
                    "command_workspace": {"workspace_id": uuid4()},
                    "command_principal": {"actor_id": "foreign"},
                    "actor_type": {"actor_type": "agent"},
                }[damage],
            ),
        )
    elif damage == "action":
        action = "control_plane.command:RequestProposalRevision"
    elif damage in {"identity_type", "identity_principal"}:
        identity = replace(
            identity,
            **(
                {"actor_type": "system"}
                if damage == "identity_type"
                else {"principal_id": "foreign"}
            ),
        )
    elif damage == "missing_session":
        grant_id = uuid4()
    host.local_sessions = SimpleNamespace(
        identity_for_grant=lambda supplied: (
            identity
            if supplied == identity.grant_id and damage != "closed_session"
            else None
        )
    )
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(grant_id, action, target)


@pytest.mark.parametrize("role", ["main", "worker", "planner"])
def test_task_tool_authorizer_never_admits_failed_planning_feedback(
    settings_values, tmp_path, role
):
    from tests.unit.test_task_tools import host_tool_fixture

    lab, _token, verified = host_tool_fixture(settings_values, tmp_path, role=role)
    target = _failed_feedback_request_target(lab.sessions.settings)
    target = replace(target, command=replace(target.command, actor_type="agent"))
    with pytest.raises(AgentError, match="permission_denied"):
        lab.host.require(
            verified.record.grant_id,
            "control_plane.command:ReviseFailedPlanningRequest",
            target,
        )


def test_background_authorizer_never_admits_failed_planning_feedback(settings_values):
    composition = product_module()
    from sidera_product.config import ProductSettings

    host = composition.ProductRequestAuthorizer(
        ProductSettings.model_validate(settings_values)
    )
    target = _failed_feedback_request_target(host.settings)
    target = replace(target, command=replace(target.command, actor_type="system"))
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(
            host.background_identity.grant_id,
            "control_plane.command:ReviseFailedPlanningRequest",
            target,
        )


def _stop_request_target(settings):
    target = _repair_request_target(settings)
    return replace(
        target,
        command=replace(
            target.command,
            command_type="StopExecution",
            target_type="execution",
            payload={},
        ),
    )


def test_human_authorizer_admits_exact_stop_execution_command(settings_values):
    composition = product_module()
    from sidera_control_plane.application.assignments import TrustedAssignmentIdentity

    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    identity = TrustedAssignmentIdentity(
        settings.local_principal_id, uuid4(), actor_type="user"
    )
    host = composition.ProductRequestAuthorizer(settings)
    host.local_sessions = SimpleNamespace(
        identity_for_grant=lambda supplied: (
            identity if supplied == identity.grant_id else None
        )
    )
    host.require(
        identity.grant_id,
        "control_plane.command:StopExecution",
        _stop_request_target(settings),
    )


@pytest.mark.parametrize(
    "damage",
    [
        "target_workspace",
        "target_principal",
        "command_workspace",
        "command_principal",
        "agent_actor",
        "system_actor",
        "action",
        "agent_identity",
        "system_identity",
        "identity_principal",
    ],
)
def test_human_authorizer_refuses_foreign_stop_scope(settings_values, damage):
    composition = product_module()
    from sidera_control_plane.application.assignments import TrustedAssignmentIdentity

    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    identity = TrustedAssignmentIdentity(
        settings.local_principal_id, uuid4(), actor_type="user"
    )
    host = composition.ProductRequestAuthorizer(settings)
    target = _stop_request_target(settings)
    action = "control_plane.command:StopExecution"
    if damage in {"target_workspace", "target_principal"}:
        target = replace(
            target,
            **(
                {"workspace_id": uuid4()}
                if damage == "target_workspace"
                else {"principal_id": "foreign"}
            ),
        )
    elif damage in {
        "command_workspace",
        "command_principal",
        "agent_actor",
        "system_actor",
    }:
        target = replace(
            target,
            command=replace(
                target.command,
                **{
                    "command_workspace": {"workspace_id": uuid4()},
                    "command_principal": {"actor_id": "foreign"},
                    "agent_actor": {"actor_type": "agent"},
                    "system_actor": {"actor_type": "system"},
                }[damage],
            ),
        )
    elif damage == "action":
        action = "control_plane.command:SendExecutionInput"
    else:
        identity = replace(
            identity,
            **{
                "agent_identity": {"actor_type": "agent"},
                "system_identity": {"actor_type": "system"},
                "identity_principal": {"principal_id": "foreign"},
            }[damage],
        )
    host.local_sessions = SimpleNamespace(
        identity_for_grant=lambda supplied: (
            identity if supplied == identity.grant_id else None
        )
    )
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(identity.grant_id, action, target)


@pytest.mark.parametrize(
    "damage",
    ["missing", "expired", "revoked", "foreign_workspace", "foreign_principal"],
)
def test_human_authorizer_refuses_noncurrent_stop_grant(settings_values, damage):
    from tests.unit.test_local_session import service

    _, sessions, store, clock = service(settings_values)
    issued = sessions.exchange(sessions.create_bootstrap().token)
    host = product_module().ProductRequestAuthorizer(sessions.settings)
    host.local_sessions = sessions
    target = _stop_request_target(sessions.settings)
    action = "control_plane.command:StopExecution"
    host.require(issued.session_id, action, target)

    grant_id = issued.session_id
    row = store.sessions[issued.session_id]
    if damage == "missing":
        grant_id = uuid4()
    elif damage == "expired":
        clock[0] = issued.expires_at
    elif damage == "revoked":
        sessions.revoke(issued.session_id)
    else:
        store.sessions[issued.session_id] = replace(
            row,
            **(
                {"workspace_id": uuid4()}
                if damage == "foreign_workspace"
                else {"principal_id": "foreign"}
            ),
        )
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(grant_id, action, target)


@pytest.mark.parametrize("role", ["main", "worker", "planner"])
def test_task_tool_authorizer_never_admits_stop_execution(
    settings_values, tmp_path, role
):
    from tests.unit.test_task_tools import host_tool_fixture

    lab, _token, verified = host_tool_fixture(settings_values, tmp_path, role=role)
    target = _stop_request_target(lab.sessions.settings)
    target = replace(target, command=replace(target.command, actor_type="agent"))
    with pytest.raises(AgentError, match="permission_denied"):
        lab.host.require(
            verified.record.grant_id, "control_plane.command:StopExecution", target
        )


def test_background_authorizer_never_admits_stop_execution(settings_values):
    composition = product_module()
    from sidera_product.config import ProductSettings

    host = composition.ProductRequestAuthorizer(
        ProductSettings.model_validate(settings_values)
    )
    target = _stop_request_target(host.settings)
    target = replace(target, command=replace(target.command, actor_type="system"))
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(
            host.background_identity.grant_id,
            "control_plane.command:StopExecution",
            target,
        )


@pytest.mark.parametrize(
    "command_type", ["ContinueExecutionSession", "RequestExecutionStop"]
)
def test_human_authorizer_keeps_other_execution_commands_denied(
    settings_values, command_type
):
    composition = product_module()
    from sidera_control_plane.application.assignments import TrustedAssignmentIdentity

    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    identity = TrustedAssignmentIdentity(
        settings.local_principal_id, uuid4(), actor_type="user"
    )
    host = composition.ProductRequestAuthorizer(settings)
    host.local_sessions = SimpleNamespace(
        identity_for_grant=lambda supplied: (
            identity if supplied == identity.grant_id else None
        )
    )
    target = _stop_request_target(settings)
    target = replace(
        target,
        command=replace(
            target.command,
            command_type=command_type,
            target_type=(
                "execution_session"
                if command_type == "ContinueExecutionSession"
                else "execution"
            ),
            payload=(
                {"not_before": "2026-10-11T00:00:00Z"}
                if command_type == "ContinueExecutionSession"
                else {}
            ),
        ),
    )
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(identity.grant_id, "control_plane.command:" + command_type, target)
