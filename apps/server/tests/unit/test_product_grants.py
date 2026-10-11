import importlib
from dataclasses import replace
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from pydantic import SecretStr
from sidera_agents.contracts import (
    CredentialRef,
    EffectiveConfig,
    ExecutionWorkspace,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    ProfileStatus,
    ProtocolState,
    ResourceRef,
    RunPolicy,
    RunSnapshot,
    RunState,
    RuntimeRef,
    SecretBinding,
    SourceRef,
    StartLookup,
    StartReceipt,
    StartStatus,
    ToolBinding,
)
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import ControlPlaneToolScope

from tests.unit.test_local_session import service


class MemoryGrantStore:
    """Only the product persistence I/O boundary is controlled in units."""

    def __init__(self):
        self.rows = {}
        self.lock = Lock()

    def save_grant(self, row):
        with self.lock:
            existing = next(
                (
                    value
                    for value in self.rows.values()
                    if (
                        value.workspace_id,
                        value.start_operation_id,
                        value.connection_id,
                    )
                    == (row.workspace_id, row.start_operation_id, row.connection_id)
                ),
                None,
            )
            if existing is not None:
                return existing, False
            self.rows[row.grant_id] = row
            return row, True

    def find_grant(self, *, grant_id=None, bearer_digest=None):
        return (
            self.rows.get(grant_id)
            if grant_id is not None
            else next(
                (
                    row
                    for row in self.rows.values()
                    if row.bearer_digest == bearer_digest
                ),
                None,
            )
        )

    def bind_grant(self, grant_id, *, run_id, bearer_digest):
        with self.lock:
            row = self.rows[grant_id]
            if row.run_id is not None and row.run_id != run_id:
                raise AgentError("permission_denied", "Original Run changed")
            winner = row.bearer_digest is None
            row = replace(
                row,
                run_id=run_id,
                bearer_digest=bearer_digest if winner else row.bearer_digest,
            )
            self.rows[grant_id] = row
            return row, winner

    def revoke_grant(self, grant_id, *, now):
        self.rows[grant_id] = replace(self.rows[grant_id], revoked_at=now)


def grant_fixture(
    settings_values, tmp_path, *, role="main", state=RunState.STARTING, catalogue=False
):
    module = importlib.import_module("sidera_product.auth.grants")
    from sidera_product.composition import ProductRequestAuthorizer

    _, sessions, _, clock = service(settings_values)
    host = ProductRequestAuthorizer(sessions.settings)
    host.local_sessions = sessions
    parent = sessions.exchange(sessions.create_bootstrap().token)
    root = sessions.settings.workspace_root
    directory = root / "explicit-main"
    directory.mkdir(parents=True, mode=0o700)
    profile_ref = ProfileRef(
        workspace_id=sessions.settings.workspace_id, profile_id=uuid4(), revision=2
    )
    runtime_ref = RuntimeRef(workspace_id=profile_ref.workspace_id, runtime_id=uuid4())
    profile = ProfileRevision(
        ref=profile_ref,
        draft=ProfileDraft(
            name="Same Profile",
            description="Role comes from original Run scope.",
            runtime_ref=runtime_ref,
            instructions="No identity comes from these instructions.",
            resource_refs=(),
            model_id=None,
            thinking=None,
            max_active=1,
            environment={},
            secret_refs=(),
            native_settings={},
        ),
        config_hash="owned-config",
        created_at=clock[0],
    )
    calls = []
    available = [profile]
    if catalogue:
        available.append(
            profile.model_copy(
                update={
                    "ref": ProfileRef(
                        workspace_id=profile_ref.workspace_id,
                        profile_id=uuid4(),
                        revision=1,
                    )
                }
            )
        )
    catalog = SimpleNamespace(
        get_profile=lambda ref, **kwargs: profile,
        get_profile_status=lambda ref, **kwargs: ProfileStatus(
            profile_id=profile_ref.profile_id, enabled=True, version=1
        ),
        list_profiles=lambda workspace, **kwargs: tuple(available),
    )
    runtimes = SimpleNamespace(
        get_runtime=lambda ref, **kwargs: SimpleNamespace(
            ref=runtime_ref,
            enabled=True,
            installed=True,
            protocol_state=ProtocolState.READY,
        )
    )
    operation, run_id, native_grant = uuid4(), uuid4(), uuid4()
    run = [
        RunSnapshot(
            run_id=run_id,
            workspace_id=profile_ref.workspace_id,
            profile_ref=profile_ref,
            policy=RunPolicy.CONVERSATION,
            state=state,
            input_open=False,
            active_response_id=uuid4() if state is RunState.RUNNING else None,
            effective_config=EffectiveConfig(
                profile_ref=profile_ref,
                config_hash="owned-config",
                runtime_ref=runtime_ref,
                actual_runtime_version="owned",
                selected_model=None,
                observed_model=None,
                inherited_fields=(),
                applied_fields=(),
                resource_refs=(),
                context_receipts=(),
            ),
            last_event_sequence=0,
            capabilities={},
            reason_code=None,
        )
    ]
    lookup = [
        StartLookup(
            found=True,
            receipt=StartReceipt(
                operation_id=operation,
                status=StartStatus.ACCEPTED,
                run_id=run_id,
                reason_code=None,
            ),
        )
    ]

    def get_start(workspace, supplied, *, grant_id):
        calls.append(("get_start", workspace, supplied, grant_id))
        return lookup[0]

    def get_run(workspace, supplied, *, grant_id):
        calls.append(("get_run", workspace, supplied, grant_id))
        return run[0]

    def forbidden(*args, **kwargs):
        pytest.fail("Grant preparation/projection started another native Run")

    execution = SimpleNamespace(get_start=get_start, get_run=get_run, start=forbidden)
    scope = [
        ControlPlaneToolScope(
            profile_ref.workspace_id,
            sessions.settings.local_principal_id,
            "worker" if role == "main" else role,
            native_grant,
            uuid5(sessions.settings.deployment_namespace, "sidera.product.task-tools"),
            profile_ref,
            operation,
            None,
            uuid4(),
            uuid4() if role != "planner" else None,
            uuid4() if role != "planner" else None,
            3 if role != "planner" else None,
            2 if role != "planner" else None,
            uuid4() if role == "planner" else None,
            uuid4() if role == "planner" else None,
            uuid4(),
            7,
        )
    ]
    store = MemoryGrantStore()

    def build():
        return module.ProductGrants(
            sessions.settings,
            store,
            sessions,
            catalog=catalog,
            runtimes=runtimes,
            execution=execution,
            read_cp_scope=lambda **kwargs: scope[0],
            parent_authorizer=host,
            clock=lambda: clock[0],
        )

    grants = build()
    workspace = ExecutionWorkspace(
        directory=str(directory), writable=True, artifact_directory=None
    )
    owner = (
        grants.prepare_main_scope(
            session_grant_id=parent.session_id,
            operation_id=operation,
            profile_ref=profile_ref,
            workspace=workspace,
        )
        if role == "main"
        else native_grant
    )
    return SimpleNamespace(
        module=module,
        grants=grants,
        store=store,
        sessions=sessions,
        parent=parent,
        clock=clock,
        profile=profile,
        catalog=catalog,
        run=run,
        lookup=lookup,
        scope=scope,
        operation=operation,
        owner=owner,
        workspace=workspace,
        calls=calls,
        build=build,
        host=host,
        available=available,
    )


def test_main_preparation_is_immutable_before_start(settings_values, tmp_path):
    lab = grant_fixture(settings_values, tmp_path)
    assert lab.calls == []
    assert (
        lab.grants.prepare_main_scope(
            session_grant_id=lab.parent.session_id,
            operation_id=lab.operation,
            profile_ref=lab.profile.ref,
            workspace=lab.workspace,
        )
        == lab.owner
    )
    assert len(lab.store.rows) == 1
    changed = lab.workspace.model_copy(update={"writable": False})
    with pytest.raises(AgentError):
        lab.grants.prepare_main_scope(
            session_grant_id=lab.parent.session_id,
            operation_id=lab.operation,
            profile_ref=lab.profile.ref,
            workspace=changed,
        )
    assert lab.calls == [] and len(lab.store.rows) == 1


@pytest.mark.parametrize(
    "shape", ("profile", "sources_list", "source_element", "workspace", "cp_reader")
)
def test_grant_validation_boundaries_deny_wrong_shapes_before_catalog_or_native_work(
    settings_values,
    tmp_path,
    no_database_connections,
    monkeypatch,
    shape,
):
    lab = grant_fixture(settings_values, tmp_path, role="worker")

    def forbidden(*args, **kwargs):
        pytest.fail("Wrong-shaped authority reached catalogue/native validation")

    for name in ("get_profile", "get_profile_status", "list_profiles"):
        monkeypatch.setattr(lab.catalog, name, forbidden)
    monkeypatch.setattr(lab.grants.runtimes, "get_runtime", forbidden)
    if shape == "cp_reader":
        monkeypatch.setattr(lab.grants, "read_cp_scope", lambda **kwargs: object())
        with pytest.raises(AgentError) as caught:
            lab.grants.resolve(lab.owner, lab.grants.connection_id)
    else:
        with pytest.raises(AgentError) as caught:
            lab.grants.prepare_main_scope(
                session_grant_id=lab.parent.session_id,
                operation_id=lab.operation,
                profile_ref=lab.profile.ref.model_dump(mode="json")
                if shape == "profile"
                else lab.profile.ref,
                workspace=lab.workspace.model_dump(mode="json")
                if shape == "workspace"
                else lab.workspace,
                sources=[]
                if shape == "sources_list"
                else ((object(),) if shape == "source_element" else ()),
            )
    assert caught.value.code == "permission_denied"
    assert lab.store.rows == {} and lab.calls == []


def test_main_source_tuple_validation_preserves_original_reference_identity_and_order(
    no_database_connections,
):
    from sidera_product.auth.grants import _validate_main_refs

    profile = ProfileRef(workspace_id=uuid4(), profile_id=uuid4(), revision=1)
    first = SourceRef(provider="knowledge", key="first", version="1")
    second = SourceRef(provider="knowledge", key="second", version="1")
    validated_profile, validated_sources = _validate_main_refs(
        profile, (first, second, first)
    )
    assert validated_profile is profile and len(validated_sources) == 3
    assert validated_sources[0] is first
    assert validated_sources[1] is second
    assert validated_sources[2] is first


@pytest.mark.parametrize("role", ("main", "worker", "planner"))
def test_same_profile_scope_mints_role_on_original_accepted_run(
    settings_values, tmp_path, role
):
    lab = grant_fixture(settings_values, tmp_path, role=role)
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    assert connection.name == "SIDERA Task Tools" and connection.transport == "http"
    assert (
        connection.endpoint_or_argv
        == lab.sessions.settings.local_url + "/product/v1/task-tools/mcp"
    )
    assert isinstance(connection.headers["Authorization"], SecretStr)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    verified = lab.grants.verify(token)
    assert verified.record.role == role and verified.record.run_id == lab.run[0].run_id
    assert (
        verified.identity.actor_type == "agent"
        and verified.identity.grant_id != lab.parent.session_id
    )
    assert lab.calls[0] == (
        "get_start",
        lab.profile.ref.workspace_id,
        lab.operation,
        lab.owner,
    )
    assert len(lab.store.rows) == 1
    assert token.get_secret_value() not in repr(next(iter(lab.store.rows.values())))


@pytest.mark.parametrize(
    "status", (StartStatus.REJECTED, StartStatus.UNKNOWN, StartStatus.BUSY)
)
def test_unaccepted_original_never_mints_or_restarts(settings_values, tmp_path, status):
    lab = grant_fixture(settings_values, tmp_path, role="worker")
    lab.lookup[0] = StartLookup(
        found=True,
        receipt=StartReceipt(
            operation_id=lab.operation,
            status=status,
            run_id=None,
            reason_code="owned-original",
        ),
    )
    with pytest.raises(AgentError):
        lab.grants.resolve(lab.owner, lab.grants.connection_id)
    assert len(lab.store.rows) == 0
    assert [call[0] for call in lab.calls] == ["get_start"]


def test_starting_can_enumerate_but_cannot_call_and_running_needs_response(
    settings_values, tmp_path
):
    lab = grant_fixture(settings_values, tmp_path)
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    assert lab.grants.verify(token).run.state is RunState.STARTING
    with pytest.raises(AgentError):
        lab.grants.verify(token, require_running=True)
    lab.run[0] = lab.run[0].model_copy(
        update={"state": RunState.RUNNING, "active_response_id": None}
    )
    with pytest.raises(AgentError):
        lab.grants.verify(token, require_running=True)
    response = uuid4()
    lab.run[0] = lab.run[0].model_copy(update={"active_response_id": response})
    assert (
        lab.grants.verify(token, require_running=True).run.active_response_id
        == response
    )


def test_reopened_digest_verifies_retained_client_but_cannot_reproject_raw(
    settings_values, tmp_path
):
    lab = grant_fixture(settings_values, tmp_path)
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    reopened = lab.build()
    assert reopened.verify(token).record.grant_id == lab.owner
    before = dict(lab.store.rows)
    with pytest.raises(AgentError) as caught:
        reopened.resolve(lab.owner, reopened.connection_id)
    assert caught.value.code == "tool_connection_unavailable"
    assert lab.store.rows == before


def test_changed_cp_fence_revocation_and_terminal_run_deny_current_token(
    settings_values, tmp_path
):
    lab = grant_fixture(
        settings_values, tmp_path, role="worker", state=RunState.RUNNING
    )
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    lab.scope[0] = replace(lab.scope[0], fencing_token=8)
    with pytest.raises(AgentError):
        lab.grants.verify(token, require_running=True)
    lab.scope[0] = replace(lab.scope[0], fencing_token=7)
    lab.run[0] = lab.run[0].model_copy(update={"state": RunState.UNKNOWN})
    with pytest.raises(AgentError):
        lab.grants.verify(token)
    lab.store.revoke_grant(next(iter(lab.store.rows)), now=lab.clock[0])
    with pytest.raises(AgentError):
        lab.grants.verify(token)


def test_main_parent_revocation_denies_tools_but_preserves_original_cleanup_permission(
    settings_values, tmp_path
):
    lab = grant_fixture(settings_values, tmp_path, state=RunState.RUNNING)
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    lab.sessions.revoke(lab.parent.session_id)
    with pytest.raises(AgentError):
        lab.grants.verify(token)
    lab.grants.require(lab.owner, "run.stop", lab.profile.ref.workspace_id)
    with pytest.raises(AgentError):
        lab.grants.require(lab.owner, "run.start", lab.profile.ref)


def test_actual_composition_registers_cold_grants_and_generic_connection_reader(
    settings_values, no_database_connections, monkeypatch
):
    from sidera_product import composition
    from sidera_product.config import ProductSettings

    captured = []
    original = composition.bootstrap.create_agent_service

    def build(**kwargs):
        captured.append(kwargs["tool_connections"])
        return original(**kwargs)

    monkeypatch.setattr(composition.bootstrap, "create_agent_service", build)
    runtime = composition.build_product_runtime(
        ProductSettings.model_validate(settings_values)
    )
    try:
        assert runtime.grants.store is runtime.authority_store
        assert runtime.grants.catalog is runtime.agents.catalog
        assert runtime.grants.execution is runtime.agents.execution
        assert (
            runtime.authority.grants
            is runtime.request_authorizer.grants
            is runtime.grants
        )
        assert captured[0] is runtime.task_tool_connections
        assert captured[0].grants is runtime.grants
        assert runtime.grants.connection_id == uuid5(
            runtime.settings.deployment_namespace, "sidera.product.task-tools"
        )
    finally:
        assert runtime.close(grace_seconds=0) is True


def test_main_catalogue_parent_reads_new_captured_revision_without_execution_rebind(
    settings_values, tmp_path
):
    lab = grant_fixture(settings_values, tmp_path, catalogue=True)
    b2 = lab.available[1].ref.model_copy(update={"revision": 2})
    runtime = RuntimeRef(workspace_id=b2.workspace_id, runtime_id=uuid4())
    resource = ResourceRef(
        workspace_id=b2.workspace_id, resource_id=uuid4(), revision=2
    )
    before = dict(lab.store.rows)
    for action, target in (
        ("profile.read", b2),
        ("runtime.read", runtime),
        ("resource.read", resource),
        ("profile.list", b2.workspace_id),
    ):
        lab.grants.require(lab.owner, action, target)
    lab.grants.require(lab.owner, "run.start", lab.profile.ref)
    lab.grants.require(
        lab.owner, "tool.execute", ToolBinding(connection_id=lab.grants.connection_id)
    )
    lab.grants.require(lab.owner, "workspace.execute", lab.workspace)
    for action, target in (
        ("run.start", lab.profile.ref.model_copy(update={"revision": 3})),
        ("tool.execute", ToolBinding(resource_ref=resource)),
        (
            "secret.read",
            SecretBinding(
                credential_ref=CredentialRef(
                    workspace_id=b2.workspace_id, credential_id=uuid4()
                ),
                destination="NEW_KEY",
            ),
        ),
        ("source.read", SourceRef(provider="knowledge", key="new-source", version="1")),
    ):
        with pytest.raises(AgentError):
            lab.grants.require(lab.owner, action, target)
    assert lab.store.rows == before and lab.calls == []


def test_main_catalogue_repeat_preparation_keeps_original_captured_set_and_bearer(
    settings_values, tmp_path
):
    lab = grant_fixture(settings_values, tmp_path, catalogue=True)
    lab.grants.resolve(lab.owner, lab.grants.connection_id)
    before, native_reads = dict(lab.store.rows), list(lab.calls)
    lab.available[1] = lab.available[1].model_copy(
        update={"ref": lab.available[1].ref.model_copy(update={"revision": 2})}
    )
    new_profile = lab.profile.model_copy(
        update={"ref": lab.profile.ref.model_copy(update={"profile_id": uuid4()})}
    )
    lab.available.append(new_profile)
    assert (
        lab.grants.prepare_main_scope(
            session_grant_id=lab.parent.session_id,
            operation_id=lab.operation,
            profile_ref=lab.profile.ref,
            workspace=lab.workspace,
        )
        == lab.owner
    )
    assert lab.store.rows == before and lab.calls == native_reads
    with pytest.raises(AgentError):
        lab.grants.require(lab.owner, "profile.read", new_profile.ref)


@pytest.mark.parametrize(
    "variant",
    [
        "new_id",
        "foreign_profile",
        "foreign_runtime",
        "foreign_resource",
        "expired",
        "revoked",
        "denied_profile",
        "denied_runtime",
        "denied_resource",
    ],
)
def test_main_catalogue_current_parent_and_closed_identity_set_fail_closed(
    settings_values, tmp_path, monkeypatch, variant
):
    lab = grant_fixture(settings_values, tmp_path, catalogue=True)
    ref = lab.available[1].ref.model_copy(update={"revision": 2})
    action, target = "profile.read", ref
    if variant == "new_id":
        target = ref.model_copy(update={"profile_id": uuid4()})
    elif variant == "foreign_profile":
        target = ref.model_copy(update={"workspace_id": uuid4()})
    elif variant == "foreign_runtime":
        action, target = (
            "runtime.read",
            RuntimeRef(workspace_id=uuid4(), runtime_id=uuid4()),
        )
    elif variant == "foreign_resource":
        action, target = (
            "resource.read",
            ResourceRef(workspace_id=uuid4(), resource_id=uuid4(), revision=1),
        )
    elif variant == "expired":
        lab.clock[0] = lab.parent.expires_at
    elif variant == "revoked":
        lab.sessions.revoke(lab.parent.session_id)
    else:
        if variant == "denied_runtime":
            action, target = (
                "runtime.read",
                RuntimeRef(workspace_id=ref.workspace_id, runtime_id=uuid4()),
            )
        elif variant == "denied_resource":
            action, target = (
                "resource.read",
                ResourceRef(
                    workspace_id=ref.workspace_id, resource_id=uuid4(), revision=1
                ),
            )
        original = lab.host.require

        def denied(grant, requested, value):
            if requested == action and value == target:
                raise AgentError(
                    "permission_denied", "Exact current parent metadata denied"
                )
            original(grant, requested, value)

        monkeypatch.setattr(lab.host, "require", denied)
    with pytest.raises(AgentError):
        lab.grants.require(lab.owner, action, target)
    assert lab.calls == []


def test_cp_execution_version_advance_keeps_original_bearer_and_exposes_fresh_scope(
    settings_values, tmp_path
):
    lab = grant_fixture(settings_values, tmp_path, role="worker")
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    grant_id = lab.grants.verify(token).record.grant_id
    before = lab.store.rows[grant_id]
    assert before.execution_version == 3
    response = uuid4()
    lab.scope[0] = replace(lab.scope[0], execution_version=4, run_id=lab.run[0].run_id)
    lab.run[0] = lab.run[0].model_copy(
        update={"state": RunState.RUNNING, "active_response_id": response}
    )
    verified = lab.grants.verify(token, require_running=True)
    assert verified.scope.execution_version == 4
    assert verified.run.active_response_id == response
    assert lab.store.rows[grant_id] == before
    assert lab.grants.resolve(lab.owner, lab.grants.connection_id) == connection
    assert lab.store.rows[grant_id] == before
    assert all(call[0] in {"get_start", "get_run"} for call in lab.calls)


def test_different_committed_cp_run_denies_original_token_and_projection(
    settings_values, tmp_path
):
    lab = grant_fixture(settings_values, tmp_path, role="worker")
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    before = dict(lab.store.rows)
    lab.scope[0] = replace(lab.scope[0], run_id=uuid4())
    with pytest.raises(AgentError):
        lab.grants.verify(token)
    with pytest.raises(AgentError):
        lab.grants.resolve(lab.owner, lab.grants.connection_id)
    assert lab.store.rows == before


@pytest.mark.parametrize(
    "field",
    (
        "execution_id",
        "worker_item_id",
        "start_operation_id",
        "profile_ref",
        "binding_version",
        "lease_id",
        "fencing_token",
        "root_task_id",
        "native_grant_id",
        "execution_version",
    ),
)
def test_cp_owned_identity_fences_and_version_regression_remain_denied(
    settings_values, tmp_path, field
):
    lab = grant_fixture(settings_values, tmp_path, role="worker")
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    previous = lab.scope[0]
    if field == "profile_ref":
        value = previous.profile_ref.model_copy(
            update={"revision": previous.profile_ref.revision + 1}
        )
    elif field in {"binding_version", "fencing_token"}:
        value = getattr(previous, field) + 1
    elif field == "execution_version":
        value = 2
    else:
        value = uuid4()
    lab.scope[0] = replace(previous, **{field: value})
    with pytest.raises(AgentError):
        lab.grants.verify(token)
