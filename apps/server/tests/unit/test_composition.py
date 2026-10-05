import importlib.util
import sys
from uuid import UUID

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

    def missing_session(*, session_id=None, token_digest=None):
        assert session_id == UUID(int=789) and token_digest is None
        queried.append(session_id)
        return None

    monkeypatch.setattr(first.authority_store, "find_session", missing_session)
    try:
        assert first.background_identity == second.background_identity
        identity = first.background_identity
        assert identity.actor_type == "system"
        assert identity.principal_id == "n17-test-user"
        assert (
            first.request_authorizer.identity_for_grant(identity.grant_id) is identity
        )
        assert queried == []
        assert first.request_authorizer.identity_for_grant(UUID(int=789)) is None
        assert queried == [UUID(int=789)]
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
        assert (
            await client.get("/agents/v1/workspaces/unknown/profiles")
        ).status_code == 401
    assert app.state.product_runtime.close(grace_seconds=0) is True
