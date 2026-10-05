from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import (
    CapabilityResourceDraft,
    ProfileRef,
    ProfileStatus,
    ResourceKind,
    ResourceRef,
    ResourceRevision,
    ResourceStatus,
    ToolBinding,
)
from sidera_agents.errors import AgentError

from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
    NativeAuthorityReader,
)
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.config import Settings
from tests.unit.test_execution_authority_provenance import (
    authority as execution_authority,
)
from tests.unit.test_native_request_authority import NoNativeCalls, runtime_module


def test_public_task_tool_contracts_are_pure_domain_types():
    public = runtime_module()
    setup = import_module("sidera_control_plane.domain.native_setup")
    assert getattr(public, "WorkspaceSetupView", None) is not None
    assert public.WorkspaceSetupView is setup.WorkspaceSetupView
    domain = import_module("sidera_control_plane.domain.agent_tools")
    for name in ("ControlPlaneToolScope", "WorkerProgressTarget"):
        assert getattr(public, name) is getattr(domain, name)


@pytest.mark.parametrize(
    "denied", ("missing", "grant", "principal", "revoked", "scope")
)
def test_public_setup_read_checks_current_authority_before_database(
    denied, monkeypatch
):
    public = runtime_module()
    workspace, grant = uuid4(), uuid4()
    identity = ControlPlaneIdentity("operator", grant, "user")
    if denied == "grant":
        identity = replace(identity, grant_id=uuid4())
    elif denied == "principal":
        identity = replace(identity, principal_id="other")
    elif denied == "revoked":
        identity = None
    queries, permissions = [], []

    def lookup(supplied):
        queries.append(supplied)
        return identity

    def require(supplied, action, target):
        permissions.append((supplied, action, target))
        raise AgentError("permission_denied", "PRIVATE host state")

    authority = (
        None
        if denied == "missing"
        else SimpleNamespace(identity_for_grant=lookup, require=require)
    )
    native = NoNativeCalls()
    runtime = public.create_control_plane_runtime(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
            local_actor_id="operator",
        ),
        native,
        native,
        native,
        native,
        authority,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Denied setup read reached persistence")

    monkeypatch.setattr(public, "SqlAlchemyUnitOfWork", forbidden)
    try:
        with pytest.raises(AgentError) as caught:
            runtime.get_workspace_setup(workspace, grant_id=grant)
        assert caught.value.code == "permission_denied"
        assert "PRIVATE" not in str(caught.value)
        assert queries == ([] if denied == "missing" else [grant])
        assert permissions == (
            [(grant, "workspace.setup.read", workspace)] if denied == "scope" else []
        )
        assert runtime._active_work == runtime._regular_work == 0
        assert native.calls == []
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "field,value",
    (
        ("fencing_token", True),
        ("binding_version", 0),
        ("execution_version", "1"),
        ("principal_id", ""),
        ("lease_id", "not-a-uuid"),
    ),
)
def test_worker_tool_scope_rejects_nonexact_fences(field, value):
    public = runtime_module()
    scope_type = getattr(public, "ControlPlaneToolScope", None)
    assert scope_type is not None, "Public native tool scope is missing"
    workspace = uuid4()
    fields = {
        "workspace_id": workspace,
        "principal_id": "operator",
        "role": "worker",
        "native_grant_id": uuid4(),
        "connection_id": uuid4(),
        "profile_ref": ProfileRef(
            workspace_id=workspace, profile_id=uuid4(), revision=1
        ),
        "start_operation_id": uuid4(),
        "run_id": None,
        "root_task_id": uuid4(),
        "execution_id": uuid4(),
        "worker_item_id": uuid4(),
        "execution_version": 1,
        "binding_version": 1,
        "planning_request_id": None,
        "planning_run_id": None,
        "lease_id": uuid4(),
        "fencing_token": 1,
    }
    fields[field] = value
    with pytest.raises(ValueError):
        scope_type(**fields)


def test_public_setup_read_rejects_closed_owner_without_lookup():
    public = runtime_module()
    native = NoNativeCalls()
    calls = []
    authority = SimpleNamespace(identity_for_grant=lambda grant: calls.append(grant))
    runtime = public.create_control_plane_runtime(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused"
        ),
        native,
        native,
        native,
        native,
        authority,
    )
    assert runtime.close(grace_seconds=0) is True
    with pytest.raises(AgentError) as caught:
        runtime.get_workspace_setup(uuid4(), grant_id=uuid4())
    assert caught.value.code == "control_plane_closed"
    assert calls == []


def worker_scope_fixture(*, case="resource"):
    now, connection, item, root, operation = (
        datetime.now(UTC),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    value = execution_authority()
    resource = ResourceRef(
        workspace_id=value.workspace_id, resource_id=uuid4(), revision=2
    )
    binding = (
        ToolBinding(connection_id=connection)
        if case == "direct"
        else ToolBinding(resource_ref=resource)
    )
    value = replace(
        value,
        target_type="worker_item",
        target_id=item,
        upstream_grant_id=uuid4(),
        tools=(binding,),
        resources=(resource,),
        actions=(*value.actions, "tool.execute", "resource.read", "profile.read"),
    )
    current_binding = SimpleNamespace(
        version=2 if case == "binding" else 1,
        profile_ref=value.profile_ref,
        runnable=True,
    )
    execution = SimpleNamespace(
        id=value.execution_id,
        workspace_id=value.workspace_id,
        worker_item_id=item,
        task_id=None,
        version=7,
        stop_requested=False,
        handoff_transition_id=None,
        status=SimpleNamespace(value="running"),
        active_lease_id=value.lease_id,
        last_fencing_token=1,
    )
    link = SimpleNamespace(
        authority_id=value.id,
        binding_version=1,
        profile_ref=value.profile_ref,
        start_operation_id=operation,
        run_id=None,
        status="starting",
    )
    lease = SimpleNamespace(
        id=value.lease_id,
        execution_id=value.execution_id,
        status=SimpleNamespace(value="active"),
        expires_at=now + timedelta(seconds=-1 if case == "expired" else 60),
        fencing_token=1,
    )
    manifest = SimpleNamespace(
        execution_id=value.execution_id,
        fencing_token=1,
        lifecycle=SimpleNamespace(value="ready"),
        location_ref="prepared://owned",
    )
    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(
            get=lambda supplied: value if supplied == value.id else None,
            get_link=lambda *args: link,
            prepared_location_ref=lambda *args: manifest.location_ref,
        ),
        planning_authorities=SimpleNamespace(get=lambda supplied: None),
        executions=SimpleNamespace(get=lambda *args: execution),
        worker_items=SimpleNamespace(
            get=lambda *args: SimpleNamespace(
                id=item,
                workspace_id=value.workspace_id,
                root_task_id=root,
                status=SimpleNamespace(value="active"),
            )
        ),
        leases=SimpleNamespace(get=lambda *args: lease),
        agent_bindings=SimpleNamespace(
            history=lambda *args: (replace_namespace(current_binding, version=1),),
            get=lambda *args: current_binding,
        ),
        workspaces=SimpleNamespace(get=lambda *args: manifest),
    )
    permissions, reads = [], []
    host = SimpleNamespace(
        require=lambda grant, action, target: permissions.append(
            (grant, action, target)
        )
    )
    factory = lambda: nullcontext(uow)
    reader = NativeAuthorityReader(
        factory,
        worker=ExecutionAuthorityReader(
            factory,
            principal=lambda: value.principal_id,
            authorizer=host,
            clock=lambda: now,
        ),
        planner=SimpleNamespace(
            require=lambda *args: pytest.fail("Worker became Planner")
        ),
    )
    native_permissions = []
    original_require = reader.require

    def checked_require(grant, action, target):
        native_permissions.append((grant, action, target))
        original_require(grant, action, target)

    reader.require = checked_require
    revision = ResourceRevision(
        ref=replace_resource(resource, revision=3)
        if case == "resource_revision"
        else resource,
        draft=CapabilityResourceDraft(
            name="Pinned task tools",
            kind=ResourceKind.SKILL if case == "kind" else ResourceKind.MCP,
            settings={
                "connection_id": str(uuid4() if case == "connection" else connection)
            },
            secret_refs=(),
        ),
        content_hash="owned-resource",
        created_at=now,
    )

    def read_resource(ref, *, grant_id):
        reads.append((ref, grant_id))
        reader.require(grant_id, "resource.read", ref)
        return revision

    catalog = SimpleNamespace(
        get_resource=read_resource,
        get_resource_status=lambda ref, **kwargs: ResourceStatus(
            resource_id=resource.resource_id,
            enabled=case != "resource_disabled",
            version=1,
        ),
        get_profile=lambda ref, **kwargs: SimpleNamespace(ref=value.profile_ref),
        get_profile_status=lambda ref, **kwargs: ProfileStatus(
            profile_id=value.profile_ref.profile_id,
            enabled=case != "profile_disabled",
            version=1,
        ),
    )
    return (
        factory,
        reader,
        catalog,
        value,
        connection,
        binding,
        (permissions, native_permissions),
        reads,
        root,
        operation,
    )


def replace_namespace(value, **changes):
    return SimpleNamespace(**{**vars(value), **changes})


def replace_resource(value, **changes):
    return value.model_copy(update=changes)


@pytest.mark.parametrize("case", ("resource", "direct"))
def test_scope_read_keeps_original_binding_and_uncommitted_run_identity(case):
    module = import_module("sidera_control_plane.application.task_tool_scope")
    (
        factory,
        reader,
        catalog,
        value,
        connection,
        binding,
        permissions,
        reads,
        root,
        operation,
    ) = worker_scope_fixture(case=case)
    scope = module.read_task_tool_scope(
        factory,
        catalog=catalog,
        authorizer=reader,
        grant_id=value.id,
        connection_id=connection,
    )
    assert scope.role == "worker" and scope.root_task_id == root
    assert scope.run_id is None and scope.start_operation_id == operation
    assert scope.native_grant_id == value.id and scope.execution_version == 7
    assert scope.lease_id == value.lease_id and scope.fencing_token == 1
    host_permissions, native_permissions = permissions
    original = [
        target for _, action, target in native_permissions if action == "tool.execute"
    ]
    assert original and all(target == binding for target in original)
    assert all(grant == value.upstream_grant_id for grant, _, _ in host_permissions)
    assert reads == ([] if case == "direct" else [(binding.resource_ref, value.id)])


@pytest.mark.parametrize(
    "case",
    (
        "connection",
        "kind",
        "resource_revision",
        "resource_disabled",
        "profile_disabled",
        "binding",
        "expired",
        "grant",
    ),
)
def test_scope_read_denies_changed_or_unowned_business_facts(case):
    module = import_module("sidera_control_plane.application.task_tool_scope")
    factory, reader, catalog, value, connection, _, _, reads, _, _ = (
        worker_scope_fixture(case=case)
    )
    with pytest.raises(AgentError) as caught:
        module.read_task_tool_scope(
            factory,
            catalog=catalog,
            authorizer=reader,
            grant_id=uuid4() if case == "grant" else value.id,
            connection_id=connection,
        )
    assert caught.value.code == "permission_denied"
    if case in {"grant", "binding", "expired"}:
        assert reads == []
