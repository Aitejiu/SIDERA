from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sidera_agents.contracts import (
    CredentialRef,
    ExecutionWorkspace,
    ProfileRef,
    ResourceRef,
    SecretBinding,
    SourceRef,
    ToolBinding,
)
from sidera_agents.errors import AgentError
from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.application.execution_authority import PlanningActionTarget
from sidera_control_plane.domain.context_packages import (
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)

from sidera_product.composition import ProductRequestAuthorizer
from sidera_product.config import ProductSettings


@pytest.fixture
def host_case(settings_values):
    settings = ProductSettings.model_validate(settings_values)
    host = ProductRequestAuthorizer(settings)
    profile = ProfileRef(
        workspace_id=settings.workspace_id, profile_id=uuid4(), revision=1
    )
    planner = PlanningActionTarget(
        settings.workspace_id,
        settings.local_principal_id,
        uuid4(),
        uuid4(),
        uuid4(),
        1,
        profile,
        uuid4(),
        profile,
    )
    assignment = AssignmentTarget(
        settings.workspace_id, "worker_item", uuid4(), settings.local_principal_id
    )
    return host, planner, assignment


@pytest.fixture
def prepared_manifest(host_case):
    _, planner, _ = host_case
    now = datetime(2026, 10, 5, tzinfo=UTC)
    return ContextPackageManifestV1.create(
        package_id=uuid4(),
        workspace_id=planner.workspace_id,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=planner.planning_run_id,
        worker_item_id=None,
        work_context_revision_id=uuid4(),
        context_version=None,
        control_plane_fact_snapshot={},
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        budget=ContextBudgetV1(
            profile_id=planner.planning_profile_id,
            profile_version=planner.planning_profile_version,
            max_sections=64,
            max_chars=65536,
            max_input_tokens=16384,
        ),
        expires_at=now + timedelta(hours=1),
        idempotency_key="controlled-prepared-manifest",
        created_at=now,
    )


def test_planning_prepared_manifest_is_authorized(host_case, prepared_manifest):
    host, planner, _ = host_case
    host.require(
        host.background_identity.grant_id,
        "source.read",
        replace(planner, target=prepared_manifest),
    )


@pytest.mark.parametrize(
    "variant", ["workspace", "consumer_run", "policy_id", "policy_version", "execution"]
)
def test_prepared_manifest_relation_mismatches_are_denied(
    host_case, prepared_manifest, variant
):
    host, planner, _ = host_case
    changes = {
        "workspace": {"workspace_id": uuid4()},
        "consumer_run": {"consumer_id": uuid4()},
        "policy_id": {"budget": replace(prepared_manifest.budget, profile_id=uuid4())},
        "policy_version": {
            "budget": replace(prepared_manifest.budget, profile_version=2)
        },
        "execution": {
            "purpose": ContextPurpose.EXECUTION,
            "consumer_type": ContextConsumerType.WORKER,
            "worker_item_id": uuid4(),
            "context_version": 1,
        },
    }
    manifest = replace(prepared_manifest, **changes[variant])
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(
            host.background_identity.grant_id,
            "source.read",
            replace(planner, target=manifest),
        )


@pytest.mark.parametrize("variant", ["grant", "principal", "workspace"])
def test_prepared_manifest_wrapped_scope_is_denied(
    host_case, prepared_manifest, variant
):
    host, planner, _ = host_case
    target = replace(planner, target=prepared_manifest)
    grant = host.background_identity.grant_id
    if variant == "grant":
        grant = uuid4()
    elif variant == "principal":
        target = replace(target, principal_id="wrong-user")
    else:
        target = replace(target, workspace_id=uuid4())
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(grant, "source.read", target)


def test_later_control_plane_source_reference_remains_authorized(host_case):
    host, planner, _ = host_case
    source = SourceRef(provider="control-plane", key="context-package", version="1")
    host.require(
        host.background_identity.grant_id,
        "source.read",
        replace(planner, target=source),
    )
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(host.background_identity.grant_id, "source.read", source)


def test_other_planning_source_provider_is_denied(host_case):
    host, planner, _ = host_case
    source = SourceRef(provider="knowledge", key="external-reference", version="1")
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(
            host.background_identity.grant_id,
            "source.read",
            replace(planner, target=source),
        )


def test_raw_prepared_manifest_is_denied(host_case, prepared_manifest):
    host, _, _ = host_case
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(
            host.background_identity.grant_id, "source.read", prepared_manifest
        )


def test_arbitrary_planning_source_is_denied(host_case):
    host, planner, _ = host_case
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(
            host.background_identity.grant_id,
            "source.read",
            replace(planner, target=object()),
        )


@pytest.mark.parametrize(
    "action",
    [
        "profile.read",
        "runtime.read",
        "resource.read",
        "run.start",
        "workspace.execute",
        "artifact.read",
        "source.read",
        "secret.read",
        "tool.execute",
        "run.input",
        "run.recover",
        "run.interrupt",
    ],
)
@pytest.mark.parametrize("kind", ["subtask", "worker_item"])
def test_finite_assignment_upstream_actions_allow_only_wrapped_internal_scope(
    host_case, action, kind
):
    host, _, target = host_case
    host.require(
        host.background_identity.grant_id, action, replace(target, target_type=kind)
    )
    with pytest.raises(AgentError):
        host.require(host.background_identity.grant_id, action, target.workspace_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": UUID(int=998)},
        {"principal_id": "wrong-user"},
        {"target_type": "task"},
        {"target_id": "not-a-uuid"},
    ],
)
def test_invalid_assignment_scope_is_denied(host_case, changes):
    host, _, target = host_case
    with pytest.raises(AgentError):
        host.require(
            host.background_identity.grant_id, "run.start", replace(target, **changes)
        )


@pytest.mark.parametrize(
    "variant", ["credential", "secret_binding", "resource_tool", "connection_tool"]
)
def test_typed_planning_secret_and_tool_upstream_targets(host_case, variant):
    host, planner, _ = host_case
    credential = CredentialRef(workspace_id=planner.workspace_id, credential_id=uuid4())
    targets = {
        "credential": ("secret.read", credential),
        "secret_binding": (
            "secret.read",
            SecretBinding(credential_ref=credential, destination="CONTROLLED_API_KEY"),
        ),
        "resource_tool": (
            "tool.execute",
            ToolBinding(
                resource_ref=ResourceRef(
                    workspace_id=planner.workspace_id, resource_id=uuid4(), revision=1
                )
            ),
        ),
        "connection_tool": ("tool.execute", ToolBinding(connection_id=uuid4())),
    }
    action, target = targets[variant]
    host.require(
        host.background_identity.grant_id, action, replace(planner, target=target)
    )
    with pytest.raises(AgentError):
        host.require(host.background_identity.grant_id, action, target)


@pytest.mark.parametrize("variant", ["credential", "secret_binding", "resource_tool"])
def test_cross_workspace_planning_credentials_and_tools_are_denied(host_case, variant):
    host, planner, _ = host_case
    credential = CredentialRef(workspace_id=UUID(int=887), credential_id=uuid4())
    targets = {
        "credential": ("secret.read", credential),
        "secret_binding": (
            "secret.read",
            SecretBinding(credential_ref=credential, destination="CONTROLLED_API_KEY"),
        ),
        "resource_tool": (
            "tool.execute",
            ToolBinding(
                resource_ref=ResourceRef(
                    workspace_id=credential.workspace_id,
                    resource_id=uuid4(),
                    revision=1,
                )
            ),
        ),
    }
    action, target = targets[variant]
    with pytest.raises(AgentError):
        host.require(
            host.background_identity.grant_id, action, replace(planner, target=target)
        )


@pytest.mark.parametrize(
    "action",
    [
        "run.input",
        "run.recover",
        "run.interrupt",
        "run.read",
        "run.stop",
        "profile.create",
    ],
)
def test_unsupported_planner_actions_and_service_run_shortcuts_are_denied(
    host_case, action
):
    host, planner, assignment = host_case
    with pytest.raises(AgentError):
        host.require(host.background_identity.grant_id, action, planner)
    if action in {"run.read", "run.stop", "profile.create"}:
        with pytest.raises(AgentError):
            host.require(host.background_identity.grant_id, action, assignment)


def test_planning_workspace_uses_canonical_root_not_cp_directory_algorithm(host_case):
    host, planner, _ = host_case
    root = host.settings.workspace_root
    valid = ExecutionWorkspace(
        directory=str(root / "another-owned-layout"),
        writable=True,
        artifact_directory=None,
    )
    host.require(
        host.background_identity.grant_id,
        "workspace.execute",
        replace(planner, target=valid),
    )
    for target in (
        ExecutionWorkspace(
            directory="relative", writable=True, artifact_directory=None
        ),
        ExecutionWorkspace(
            directory=str(root.parent / (root.name + "-other") / "path"),
            writable=True,
            artifact_directory=None,
        ),
        ExecutionWorkspace(
            directory=str(root / ".." / "escaped"),
            writable=True,
            artifact_directory=None,
        ),
        ExecutionWorkspace(
            directory=str(root / "path"), writable=False, artifact_directory=None
        ),
        ExecutionWorkspace(
            directory=str(root / "path"),
            writable=True,
            artifact_directory=str(host.settings.artifact_root),
        ),
    ):
        with pytest.raises(AgentError):
            host.require(
                host.background_identity.grant_id,
                "workspace.execute",
                replace(planner, target=target),
            )


def test_symlink_escape_and_unknown_grant_are_denied(host_case, tmp_path):
    host, planner, assignment = host_case
    root = host.settings.workspace_root
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    target = ExecutionWorkspace(
        directory=str(root / "escape" / "work"), writable=True, artifact_directory=None
    )
    with pytest.raises(AgentError):
        host.require(
            host.background_identity.grant_id,
            "workspace.execute",
            replace(planner, target=target),
        )
    with pytest.raises(AgentError):
        host.require(uuid4(), "run.start", assignment)


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": UUID(int=765)},
        {"principal_id": "wrong-user"},
        {"planning_profile_version": 0},
        {
            "profile_ref": ProfileRef(
                workspace_id=UUID(int=654), profile_id=UUID(int=653), revision=1
            )
        },
    ],
)
def test_invalid_planning_scope_is_denied(host_case, changes):
    host, planner, _ = host_case
    with pytest.raises(AgentError):
        host.require(
            host.background_identity.grant_id,
            "planning.prepare",
            replace(planner, **changes),
        )
