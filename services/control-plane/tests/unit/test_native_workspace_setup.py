from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import ProfileRef
from sidera_agents.errors import AgentError

from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.planning_profiles import PlanningBudgetPolicy
from sidera_control_plane.domain.result_integration import CheckSpecV1
from tests.unit.test_native_request_authority import NoNativeCalls, runtime_module


def test_public_setup_contract_exports_are_the_pure_domain_types():
    public = import_module("sidera_control_plane.composition.native_runtime")
    domain = import_module("sidera_control_plane.domain.native_setup")
    for name in ("PlanningPolicyRef", "WorkspaceSetupRequest", "WorkspaceSetupReceipt"):
        assert getattr(public, name) is getattr(domain, name)


def setup_request(workspace_id, repository_directory, **kwargs):
    module = import_module("sidera_control_plane.composition.native_runtime")
    request_type = getattr(module, "WorkspaceSetupRequest", None)
    assert request_type is not None, "Public ordinary workspace setup is missing"
    budget = PlanningBudgetPolicy(64000, 10000, 1000000, 60, 3, 20, 2)
    check = CheckSpecV1.from_mapping(
        {
            "key": "owned-check",
            "version": 1,
            "argv": ["python3", "check.py"],
            "cwd": ".",
            "environment_ref": "env://owned-test",
            "prepare_argvs": [],
            "timeout_seconds": 5,
            "success_exit_codes": [0],
            "report_paths": [],
            "required": True,
        }
    )
    return request_type(
        workspace_id,
        repository_directory,
        0,
        budget,
        ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1),
        verification_checks=(check,),
        **kwargs,
    )


@pytest.mark.parametrize(
    "denied",
    (
        "missing",
        "shape",
        "grant",
        "agent",
        "system",
        "principal",
        "configured_actor",
        "revoked",
        "lookup_error",
        "scope",
    ),
)
def test_native_setup_denies_before_git_database_or_catalog(
    denied, monkeypatch, tmp_path
):
    module = runtime_module()
    workspace_id, grant = uuid4(), uuid4()
    request = setup_request(workspace_id, tmp_path / "not-inspected")
    identity = ControlPlaneIdentity("operator", grant, "user")
    if denied == "shape":
        identity = {"principal_id": "operator", "grant_id": grant, "actor_type": "user"}
    elif denied == "grant":
        identity = replace(identity, grant_id=uuid4())
    elif denied in {"agent", "system"}:
        identity = replace(identity, actor_type=denied)
    elif denied == "principal":
        identity = replace(identity, principal_id="other")
    elif denied == "revoked":
        identity = None
    lookups, scopes = [], []

    def lookup(supplied):
        lookups.append(supplied)
        if denied == "lookup_error":
            raise RuntimeError("PRIVATE host credential")
        return identity

    def require(supplied, action, target):
        scopes.append((supplied, action, target))
        if denied == "scope":
            raise AgentError("permission_denied", "PRIVATE directory")

    authority = (
        None
        if denied == "missing"
        else SimpleNamespace(
            identity_for_grant=lookup,
            require=require,
        )
    )
    native = NoNativeCalls()
    runtime = module.create_control_plane_runtime(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
            local_actor_id="operator",
            local_actor_type="system" if denied == "configured_actor" else "user",
        ),
        native,
        native,
        native,
        native,
        authority,
        agent_runtimes=native,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized setup reached Git or database")

    monkeypatch.setattr(module, "SqlAlchemyUnitOfWork", forbidden)
    import subprocess

    monkeypatch.setattr(subprocess, "run", forbidden)
    try:
        with pytest.raises(AgentError) as caught:
            runtime.configure_workspace(request, grant_id=grant)
        assert caught.value.code == "permission_denied"
        assert "PRIVATE" not in str(caught.value)
        assert not native.calls
        assert scopes == (
            [(grant, "workspace.configure", request)] if denied == "scope" else []
        )
        assert lookups == ([] if denied == "missing" else [grant])
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "invalid", ("empty", "duplicate", "environment", "argv", "cwd", "timeout")
)
def test_native_setup_revalidates_explicit_check_specs_before_configuration(
    invalid, monkeypatch, tmp_path
):
    module = runtime_module()
    workspace_id, grant = uuid4(), uuid4()
    request = setup_request(workspace_id, tmp_path)
    check = request.verification_checks[0]
    checks = {
        "empty": (),
        "duplicate": (check, check),
        "environment": (
            check,
            replace(check, key="other", environment_ref="env://other"),
        ),
        "argv": (replace(check, argv=("python3", "bad;command")),),
        "cwd": (replace(check, cwd="../escape"),),
        "timeout": (replace(check, timeout_seconds=0),),
    }[invalid]
    request = replace(request, verification_checks=checks)
    identity = ControlPlaneIdentity("operator", grant, "user")
    scopes = []
    authority = SimpleNamespace(
        identity_for_grant=lambda supplied: identity,
        require=lambda supplied, action, target: scopes.append(
            (supplied, action, target)
        ),
    )
    native = NoNativeCalls()
    runtime = module.create_control_plane_runtime(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
            local_actor_id="operator",
            local_actor_type="user",
        ),
        native,
        native,
        native,
        native,
        authority,
        agent_runtimes=native,
    )
    try:
        with pytest.raises(AgentError) as caught:
            runtime.configure_workspace(request, grant_id=grant)
        assert caught.value.code == "validation_error"
        assert scopes == [(grant, "workspace.configure", request)]
        assert not native.calls
    finally:
        runtime.close()
