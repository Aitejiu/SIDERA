from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sidera_agents.contracts import (
    CapabilitySupport,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    RuntimeRef,
    RuntimeSnapshot,
)
from sqlalchemy import func, select, update

from sidera_control_plane.application.command_service import (
    CommandService,
    hash_request,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import DomainError, VersionConflict
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.models import (
    CommandModel,
    EventModel,
    ExecutionModel,
)
from tests.support.native_agents import (
    AssignmentScope,
    PublicAgentCatalog,
    PublicAgentRuntimes,
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(workspace_id, uow_factory):
    from sidera_control_plane.application.assignments import (
        AssignmentCommandHandler,
        TrustedAssignmentIdentity,
    )

    now = datetime.now(UTC)
    principal, grant, profile_id, task_id, item_id = (uuid4() for _ in range(5))
    runtime_ref = RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4())
    runtime = RuntimeSnapshot(
        ref=runtime_ref,
        harness_id="test",
        executable="/unused",
        actual_version="1",
        installed=True,
        enabled=True,
        version=1,
        protocol_state="ready",
        auth_state="ready",
        capabilities={
            "model": CapabilitySupport(
                status="supported", reason_code="verified", constraints={}
            )
        },
        checked_at=now,
        limits={},
    )
    draft = ProfileDraft(
        name="Agent",
        description="",
        runtime_ref=runtime_ref,
        instructions="",
        resource_refs=(),
        model_id=None,
        thinking=None,
        max_active=1,
        environment={},
        secret_refs=(),
        native_settings={},
    )
    profile = ProfileRevision(
        ref=ProfileRef(workspace_id=workspace_id, profile_id=profile_id, revision=1),
        draft=draft,
        config_hash="abc",
        created_at=now,
    )
    catalog, runtimes = PublicAgentCatalog((profile,)), PublicAgentRuntimes(runtime)
    identity = TrustedAssignmentIdentity(principal_id=str(principal), grant_id=grant)
    scope = AssignmentScope(workspace_id, str(principal), grant)
    handler = AssignmentCommandHandler(
        catalog=catalog, runtimes=runtimes, identity=identity, authorizer=scope
    )
    service = CommandService(uow_factory, (handler,), preflight=handler.preflight)
    with uow_factory() as uow:
        uow.tasks.add(
            Task.create(
                task_id=task_id,
                workspace_id=workspace_id,
                title="Task",
                description="",
                acceptance_criteria=("done",),
                priority=50,
                requested_by=str(principal),
            )
        )
        uow.flush()
        uow.worker_items.add(
            WorkerItem.create(
                item_id=item_id,
                workspace_id=workspace_id,
                root_task_id=task_id,
                title="Code",
                responsibility="Code",
                now=now,
            )
        )
    return {
        "workspace": workspace_id,
        "principal": str(principal),
        "grant": grant,
        "task": task_id,
        "item": item_id,
        "profile": profile,
        "catalog": catalog,
        "runtimes": runtimes,
        "scope": scope,
        "handler": handler,
        "service": service,
        "uow": uow_factory,
        "now": now,
    }


def assign(s, *, human=False, item=False, version=1, **changes):
    request = CommandEnvelope(
        id=uuid4(),
        workspace_id=s["workspace"],
        command_type=("SetWorkerItemAssignee" if item else "SetSubtaskAssignee"),
        target_type="worker_item" if item else "task",
        target_id=s["item"] if item else s["task"],
        actor_type="user",
        actor_id=s["principal"],
        expected_version=version,
        idempotency_key=str(uuid4()),
        correlation_id=uuid4(),
        payload={
            "assignee_kind": "human" if human else "profile",
            "assignee_id": s["principal"]
            if human
            else str(s["profile"].ref.profile_id),
            "profile_revision": None,
        },
    )
    return replace(request, **changes)


def execution(s, *, item=False, status="queued"):
    execution_id = uuid4()
    result = Execution.create(
        execution_id=execution_id,
        workspace_id=s["workspace"],
        task_id=s["task"],
        worker_item_id=s["item"] if item else None,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=1,
        purpose="Execute",
        capability_requirements=frozenset({"model"}),
        pinned_worker_id=None,
        not_before=s["now"],
    )
    with s["uow"]() as uow:
        uow.executions.add(result)
        uow.flush()
        uow.session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == result.id)
            .values(status=status)
        )
    return result.id


def test_fresh_active_worker_assignment_and_parent_human_are_independent(scenario):
    s = scenario
    s["service"].handle(assign(s, item=True))
    s["catalog"].calls.clear()
    s["runtimes"].calls.clear()
    s["service"].handle(assign(s, human=True))
    with s["uow"]() as uow:
        child = s["handler"].get_assignee(uow, s["workspace"], "worker_item", s["item"])
        parent = s["handler"].get_assignee(uow, s["workspace"], "subtask", s["task"])
    assert child.profile_ref == s["profile"].ref
    assert parent.assignee.id == s["principal"]
    assert parent.profile_ref is None
    assert s["catalog"].calls == s["runtimes"].calls == []


def test_pin_receipt_history_and_exact_aggregate_version(scenario):
    s = scenario
    request = assign(s)
    original = s["service"].handle(request)
    assert original.aggregate_version == 2
    assert original.data["binding"]["profile_ref"]["revision"] == 1
    new = s["profile"].model_copy(
        update={"ref": s["profile"].ref.model_copy(update={"revision": 2})}
    )
    s["catalog"].profiles[new.ref] = new
    s["catalog"].calls.clear()
    assert s["service"].handle(request) == original
    assert s["catalog"].calls == []
    with pytest.raises(VersionConflict):
        s["service"].handle(assign(s, version=1))
    changed = s["service"].handle(assign(s, version=2))
    assert changed.aggregate_version == 3
    with s["uow"]() as uow:
        history = uow.agent_bindings.history(s["workspace"], "subtask", s["task"])
        assert [b.profile_ref.revision for b in history] == [1, 2]
        assert uow.tasks.get(s["task"], s["workspace"]).version == 3
        assert uow.session.scalar(select(func.count()).select_from(EventModel)) == 2


@pytest.mark.parametrize("path", ["replay", "conflict", "concurrent"])
def test_revocation_precedes_real_receipt_read(scenario, monkeypatch, path):
    from sidera_control_plane.persistence.repositories import CommandRepository

    s = scenario
    request = assign(s)
    s["service"].handle(request)
    s["scope"].revoked = True

    def no_read(*_):
        pytest.fail("revoked scope read a receipt")

    monkeypatch.setattr(CommandRepository, "find_by_id", no_read)
    if path == "conflict":
        request = replace(request, payload={"forged": True})
    with pytest.raises(DomainError, match="denied"):
        if path == "concurrent":
            s["service"]._recover_concurrent(request, hash_request(request))
        else:
            s["service"].handle(request)
    with s["uow"]() as uow:
        assert (
            len(uow.agent_bindings.history(s["workspace"], "subtask", s["task"])) == 1
        )
        assert uow.session.scalar(select(func.count()).select_from(CommandModel)) == 1


@pytest.mark.parametrize("case", ["actor", "human", "cross_profile", "unknown_target"])
def test_invalid_assignment_cannot_write_or_reveal_profile(scenario, case):
    s = scenario
    request = assign(s, human=case == "human")
    if case == "actor":
        request = replace(request, actor_id="forged")
    elif case == "human":
        request = replace(
            request,
            payload={
                "assignee_kind": "human",
                "assignee_id": "other",
                "profile_revision": None,
            },
        )
    elif case == "cross_profile":
        request = replace(
            request,
            payload={
                "assignee_kind": "profile",
                "assignee_id": str(uuid4()),
                "profile_revision": None,
                "workspace_id": str(uuid4()),
            },
        )
    else:
        request = replace(request, target_id=uuid4())
    with pytest.raises(DomainError):
        s["service"].handle(request)
    with s["uow"]() as uow:
        assert uow.agent_bindings.history(s["workspace"], "subtask", s["task"]) == ()
    assert s["catalog"].calls == s["runtimes"].calls == []


@pytest.mark.parametrize("status", ["leased", "running"])
@pytest.mark.parametrize("human", [False, True])
def test_leased_or_running_owner_change_requires_handoff(scenario, status, human):
    s = scenario
    s["service"].handle(assign(s, item=True))
    execution(s, item=True, status=status)
    with pytest.raises(DomainError) as denied:
        s["service"].handle(assign(s, item=True, human=human, version=2))
    assert denied.value.code == "handoff_required"


def test_mandatory_unconfirmed_capability_cannot_become_runnable(scenario):
    s = scenario
    execution(s)
    s["runtimes"].runtime = s["runtimes"].runtime.model_copy(
        update={
            "capabilities": {
                "model": CapabilitySupport(
                    status="unconfirmed",
                    reason_code="model_response_unconfirmed",
                    constraints={},
                )
            }
        }
    )
    result = s["service"].handle(assign(s))
    assert result.data["binding"]["runnable"] is False
    assert (
        "model_response_unconfirmed" in result.data["binding"]["not_runnable_reasons"]
    )


def test_failed_authentication_blocks_without_mandatory_capabilities(scenario):
    s = scenario
    runtime = s["runtimes"].runtime
    s["runtimes"].runtime = runtime.model_copy(
        update={"auth_state": type(runtime.auth_state).FAILED}
    )
    result = s["service"].handle(assign(s))
    assert result.data["binding"]["runnable"] is False
    assert result.data["binding"]["not_runnable_reasons"] == ["authentication_failed"]


def test_disabled_profile_is_not_saved(scenario):
    s = scenario
    s["catalog"].enabled = False
    with pytest.raises(DomainError):
        s["service"].handle(assign(s))
    with s["uow"]() as uow:
        assert uow.agent_bindings.history(s["workspace"], "subtask", s["task"]) == ()


@pytest.mark.parametrize("mandatory", [True, False])
def test_actual_public_agent_service_pins_without_native_execution(
    scenario, tmp_path, mandatory
):
    from sidera_agents.bootstrap import (
        create_agent_service,
        create_harness_registry,
        open_memory_store,
    )
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader

    from sidera_control_plane.application.assignments import (
        AssignmentCommandHandler,
        TrustedAssignmentIdentity,
    )
    from tests.support.native_agents import (
        ControlledAssignmentHarness,
        PublicWorkspaceScope,
    )

    s = scenario
    harness = ControlledAssignmentHarness()
    executable = tmp_path / harness.executable_name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    executable.chmod(0o700)
    store = open_memory_store()
    registry = create_harness_registry((harness,))
    agents = create_agent_service(
        store,
        registry,
        PublicWorkspaceScope(s["workspace"], s["grant"]),
        DenyContextSourceReader(),
        DenySecretReader(),
        lambda: s["now"],
    )
    try:
        runtime = agents.runtimes.register(
            s["workspace"],
            harness.harness_id,
            str(executable),
            max_active=1,
            max_sessions=1,
            grant_id=s["grant"],
        )
        profile = agents.catalog.create_profile(
            s["workspace"],
            s["profile"].draft.model_copy(update={"runtime_ref": runtime.ref}),
            grant_id=s["grant"],
        )
        if mandatory:
            execution(s)
        handler = AssignmentCommandHandler(
            catalog=agents.catalog,
            runtimes=agents.runtimes,
            identity=TrustedAssignmentIdentity(s["principal"], s["grant"]),
            authorizer=s["scope"],
        )
        service = CommandService(s["uow"], (handler,), preflight=handler.preflight)
        request = replace(
            assign(s),
            payload={
                "assignee_kind": "profile",
                "assignee_id": str(profile.ref.profile_id),
                "profile_revision": None,
            },
        )
        result = service.handle(request)
        assert result.data["binding"]["profile_ref"] == profile.ref.model_dump(
            mode="json"
        )
        assert result.data["binding"]["runnable"] is (not mandatory)
        assert (
            "authentication_unconfirmed"
            not in result.data["binding"]["not_runnable_reasons"]
        )
        assert result.data["binding"]["not_runnable_reasons"] == (
            ["model_response_unconfirmed"] if mandatory else []
        )
        assert service.handle(request) == result
        with s["uow"]() as uow:
            assert (
                handler.get_assignee(
                    uow, s["workspace"], "subtask", s["task"]
                ).profile_ref
                == profile.ref
            )
        from sidera_control_plane.application.execution_authority import (
            ExecutionAuthorityReader,
        )

        if mandatory:
            value = _prepare_authority(s, runtime_ref=runtime.ref)
            reader = ExecutionAuthorityReader(
                s["uow"],
                principal=lambda: s["principal"],
                authorizer=s["execution_scope"],
                clock=lambda: s["now"],
            )
            consumer = create_agent_service(
                store,
                registry,
                reader,
                DenyContextSourceReader(),
                DenySecretReader(),
                lambda: s["now"],
            )
            try:
                assert (
                    consumer.runtimes.get_runtime(runtime.ref, grant_id=value.id)
                    == runtime
                )
                with s["uow"]() as uow:
                    original = uow.execution_authorities.get_link(
                        s["workspace"], value.execution_id
                    )
                lookup = consumer.execution.get_start(
                    s["workspace"], original.start_operation_id, grant_id=value.id
                )
                assert lookup.found is False
                assert lookup.receipt is None
            finally:
                consumer.close()
        assert (harness.probes, harness.opens, harness.prompts) == (1, 0, 0)
        observed = agents.runtimes.get_runtime(runtime.ref, grant_id=s["grant"])
        assert observed.auth_state.value == "unconfirmed"
        assert observed.capabilities["model"].status.value == "unconfirmed"
        assert (
            agents.runtimes.validate_profile(profile.ref, grant_id=s["grant"]).valid
            is True
        )
    finally:
        agents.close()


def test_exact_revision_does_not_resolve_latest(scenario):
    s = scenario
    new = s["profile"].model_copy(
        update={"ref": s["profile"].ref.model_copy(update={"revision": 2})}
    )
    s["catalog"].profiles[new.ref] = new
    request = assign(s)
    result = s["service"].handle(
        replace(request, payload={**request.payload, "profile_revision": 1})
    )
    assert result.data["binding"]["profile_ref"]["revision"] == 1
    assert not any(call[0] == "list_profiles" for call in s["catalog"].calls)


def test_bulk_update_is_explicit_and_atomic(scenario):
    s = scenario
    s["service"].handle(assign(s))
    s["service"].handle(assign(s, item=True))
    request = assign(s, human=True, version=2)
    request = replace(
        request,
        command_type="UpdateUnstartedAgentBinding",
        payload={
            **request.payload,
            "targets": [
                {
                    "target_type": "subtask",
                    "target_id": str(s["task"]),
                    "expected_version": 2,
                }
            ],
        },
    )
    s["service"].handle(request)
    with s["uow"]() as uow:
        assert (
            uow.agent_bindings.get(s["workspace"], "subtask", s["task"]).assignee.kind
            == "human"
        )
        assert (
            uow.agent_bindings.get(s["workspace"], "worker_item", s["item"]).version
            == 1
        )
    invalid = assign(s, human=True, version=3)
    invalid = replace(
        invalid,
        command_type="UpdateUnstartedAgentBinding",
        payload={
            **invalid.payload,
            "targets": [
                {
                    "target_type": "subtask",
                    "target_id": str(s["task"]),
                    "expected_version": 3,
                },
                {
                    "target_type": "worker_item",
                    "target_id": str(s["item"]),
                    "expected_version": 1,
                },
            ],
        },
    )
    with pytest.raises(VersionConflict):
        s["service"].handle(invalid)
    with s["uow"]() as uow:
        assert uow.tasks.get(s["task"], s["workspace"]).version == 3
        assert uow.agent_bindings.get(s["workspace"], "subtask", s["task"]).version == 2


@pytest.fixture
def authority(scenario):
    scenario["service"].handle(assign(scenario))
    return _prepare_authority(scenario)


def _prepare_authority(s, *, runtime_ref=None):
    from datetime import timedelta

    from sidera_agents.contracts import ExecutionWorkspace

    from sidera_control_plane.domain.agent_bindings import (
        AgentExecutionLink,
        ExecutionAuthority,
    )
    from sidera_control_plane.domain.lease import AssignmentLease
    from sidera_control_plane.domain.workspace import ExecutionWorkspaceManifest
    from sidera_control_plane.persistence.models import WorkerModel

    execution_id = execution(s)
    worker, lease_id, workspace_ref = uuid4(), uuid4(), uuid4()
    with s["uow"]() as uow:
        binding = uow.agent_bindings.get(s["workspace"], "subtask", s["task"])
        uow.session.add(
            WorkerModel(
                id=worker,
                workspace_id=s["workspace"],
                adapter_type="fixture",
                display_name="Fixture",
                status="online",
                capabilities=["model"],
                capacity=1,
                current_load=0,
                cost_per_hour_microunits=0,
                startup_latency_ms=0,
                last_heartbeat_at=s["now"],
                version=1,
            )
        )
        uow.flush()
        lease = AssignmentLease.issue(
            lease_id=lease_id,
            workspace_id=s["workspace"],
            execution_id=execution_id,
            worker_id=worker,
            issued_at=s["now"],
            expires_at=s["now"] + timedelta(seconds=300),
            fencing_token=1,
            assignment_reason={"purpose": "test"},
        )
        uow.leases.add(lease)
        manifest = ExecutionWorkspaceManifest.create(
            id=workspace_ref,
            workspace_id=s["workspace"],
            task_id=s["task"],
            execution_id=execution_id,
            checkouts=(),
            location_ref="prepared://cp-work",
            retention_seconds=300,
            fencing_token=1,
            created_at=s["now"],
        )
        manifest.mark_preparing()
        manifest.mark_ready()
        uow.workspaces.add(manifest)
        uow.flush()
        uow.session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(active_lease_id=lease_id, last_fencing_token=1, status="leased")
        )
        value = ExecutionAuthority(
            id=s["grant"],
            workspace_id=s["workspace"],
            principal_id=s["principal"],
            execution_id=execution_id,
            target_type="subtask",
            target_id=s["task"],
            binding_version=1,
            profile_ref=binding.profile_ref,
            runtime_ref=runtime_ref or s["profile"].draft.runtime_ref,
            lease_id=lease_id,
            fencing_token=1,
            workspace_ref=workspace_ref,
            execution_workspace=ExecutionWorkspace(
                directory="/prepared/cp-work",
                writable=True,
                artifact_directory="/prepared/cp-work/artifacts",
            ),
            actions=(
                "run.start",
                "run.input",
                "run.interrupt",
                "run.recover",
                "workspace.execute",
                "artifact.read",
                "tool.execute",
                "secret.read",
                "profile.read",
                "runtime.read",
                "run.read",
            ),
        )
        uow.execution_authorities.add(value)
        uow.flush()
        link = AgentExecutionLink(
            s["workspace"],
            execution_id,
            uuid4(),
            binding.profile_ref,
            None,
            0,
            "pending",
            value.id,
            1,
        )
        uow.execution_authorities.add_link(link)
    s["execution_scope"] = AssignmentScope(
        s["workspace"], s["principal"], s["grant"], actions=value.actions
    )
    return value


@pytest.mark.parametrize("human", [True, False])
@pytest.mark.parametrize("status", ["pending", "unknown"])
def test_missing_run_id_is_not_unstarted_proof(scenario, authority, human, status):
    from sidera_control_plane.persistence.models import (
        AgentExecutionLinkModel,
        AssignmentLeaseModel,
    )

    s = scenario
    with s["uow"]() as uow:
        uow.session.execute(update(AssignmentLeaseModel).values(status="released"))
        uow.session.execute(
            update(ExecutionModel).values(status="queued", active_lease_id=None)
        )
        uow.session.execute(update(AgentExecutionLinkModel).values(status=status))
    with pytest.raises(DomainError) as denied:
        s["service"].handle(assign(s, human=human, version=2))
    assert denied.value.code == "handoff_required"
    with s["uow"]() as uow:
        assert (
            uow.execution_authorities.get_link(
                s["workspace"], authority.execution_id
            ).run_id
            is None
        )


@pytest.mark.parametrize("human", [True, False])
@pytest.mark.parametrize("fact", ["lease", "session"])
def test_live_lease_or_session_blocks_owner_change_on_queued_execution(
    scenario, authority, human, fact
):
    from sidera_control_plane.domain.execution import ExecutionSession
    from sidera_control_plane.persistence.models import (
        AgentExecutionLinkModel,
        AssignmentLeaseModel,
    )

    s = scenario
    session_id = uuid4()
    with s["uow"]() as uow:
        lease = uow.leases.get(authority.lease_id, s["workspace"])
        uow.session.execute(
            update(ExecutionModel).values(status="queued", active_lease_id=None)
        )
        uow.session.execute(update(AgentExecutionLinkModel).values(status="stopped"))
        if fact == "session":
            uow.session.execute(update(AssignmentLeaseModel).values(status="released"))
            uow.sessions.add(
                ExecutionSession.start(
                    session_id=session_id,
                    workspace_id=s["workspace"],
                    execution_id=authority.execution_id,
                    worker_id=lease.worker_id,
                    adapter_session_id="retained-legacy-session",
                    now=s["now"],
                )
            )
    with pytest.raises(DomainError) as denied:
        s["service"].handle(assign(s, human=human, version=2))
    assert denied.value.code == "handoff_required"
    with s["uow"]() as uow:
        assert uow.tasks.get(s["task"], s["workspace"]).version == 2
        assert uow.tasks.get(s["task"], uuid4()) is None
        if fact == "session":
            restored = uow.sessions.get(session_id, s["workspace"])
            assert restored.adapter_session_id == "retained-legacy-session"
            assert restored.status.value == "active"
            assert uow.sessions.get(session_id, uuid4()) is None


def test_native_tables_have_only_owned_cp_foreign_keys(scenario):
    from sqlalchemy import inspect

    expected = {
        "agent_bindings": {"workspaces"},
        "execution_authorities": {"executions", "agent_bindings"},
        "agent_execution_links": {"executions", "execution_authorities"},
    }
    with scenario["uow"]() as uow:
        inspector = inspect(uow.session.connection())
        for table, targets in expected.items():
            foreign_keys = inspector.get_foreign_keys(table)
            assert {key["referred_table"] for key in foreign_keys} == targets
            assert all(
                key["referred_schema"] in {None, "public"} for key in foreign_keys
            )


@pytest.mark.parametrize("action", ["workspace.execute", "artifact.read"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("directory", "/elsewhere"),
        ("artifact_directory", "/elsewhere"),
        ("writable", False),
    ],
)
def test_workspace_authority_requires_exact_prepared_target(
    scenario, authority, action, field, value
):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )

    s = scenario
    reader = ExecutionAuthorityReader(
        s["uow"],
        principal=lambda: s["principal"],
        authorizer=s["execution_scope"],
        clock=lambda: s["now"],
    )
    reader.require(authority.id, action, authority.execution_workspace)
    with pytest.raises(AgentError):
        reader.require(
            authority.id,
            action,
            authority.execution_workspace.model_copy(update={field: value}),
        )
    with pytest.raises(AgentError):
        reader.require(authority.id, action, object())


def test_revoked_or_stale_authority_denies_later_input_but_allows_owner_cleanup(
    scenario, authority
):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )

    s = scenario
    reader = ExecutionAuthorityReader(
        s["uow"],
        principal=lambda: s["principal"],
        authorizer=s["execution_scope"],
        clock=lambda: s["now"],
    )
    reader.require(authority.id, "run.start", authority.profile_ref)
    with s["uow"]() as uow:
        uow.execution_authorities.advance_link(
            workspace_id=s["workspace"],
            execution_id=authority.execution_id,
            authority_id=authority.id,
            run_id=uuid4(),
            event_cursor=1,
            status="running",
        )
        uow.execution_authorities.revoke(authority.id, s["workspace"])
    for action, target in [
        ("run.input", s["workspace"]),
        ("run.recover", s["workspace"]),
        ("artifact.read", authority.execution_workspace),
    ]:
        with pytest.raises(AgentError):
            reader.require(authority.id, action, target)
    reader.require(authority.id, "run.stop", s["workspace"])
    s["execution_scope"].revoked = True
    reader.require(authority.id, "run.stop", s["workspace"])


def test_tool_denial_precedes_secret_resolution_and_stale_fencing_denies(
    scenario, authority
):
    from sidera_agents.contracts import ToolBinding
    from sidera_agents.errors import AgentError

    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )

    s = scenario
    reader = ExecutionAuthorityReader(
        s["uow"],
        principal=lambda: s["principal"],
        authorizer=s["execution_scope"],
        clock=lambda: s["now"],
    )
    with pytest.raises(AgentError):
        reader.require(
            authority.id,
            "tool.execute",
            ToolBinding(connection_id=uuid4(), resource_ref=None),
        )
        pytest.fail("secret reader reached after denied tool")
    with s["uow"]() as uow:
        uow.session.execute(update(ExecutionModel).values(last_fencing_token=2))
    with pytest.raises(AgentError):
        reader.require(authority.id, "workspace.execute", authority.execution_workspace)


def test_native_and_legacy_policy_repository_and_query_round_trip(scenario, database):
    from sidera_control_plane.api.schemas import PlanningProfileResponse
    from sidera_control_plane.persistence.models import ModuleDefinitionModel
    from sidera_control_plane.persistence.planning_query_store import (
        SqlAlchemyPlanningQueryStore,
    )
    from sidera_control_plane.persistence.planning_repository import (
        _capability_to_mapping,
        _snapshot_from_mapping,
        _snapshot_to_mapping,
    )
    from tests.integration.test_planning_repository import add_module_instance
    from tests.unit.test_planning_profiles import planner_capabilities, planning_profile

    s = scenario
    module_id = add_module_instance(database, s["workspace"])
    legacy = planning_profile(
        workspace_id=s["workspace"], planner_module_instance_id=module_id
    )
    pin, _ = s["handler"].resolve_profile(
        s["workspace"], s["profile"].ref.profile_id, 1
    )
    native = planning_profile(
        workspace_id=s["workspace"],
        profile_key="native",
        execution_mode="native_profile",
        planner_agent_ref=pin,
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_secret_refs=(),
    )
    with s["uow"]() as uow:
        uow.session.execute(
            update(ModuleDefinitionModel).values(
                capabilities=_capability_to_mapping(planner_capabilities())
            )
        )
        uow.planning_profiles.add(legacy)
        uow.planning_profiles.add(native)
    with s["uow"]() as uow:
        restored = uow.planning_profiles.get(native.id, s["workspace"])
        assert restored.planner_agent_ref == pin
        assert restored.planner_module_instance_id is None
        assert (
            _snapshot_from_mapping(
                _snapshot_to_mapping(restored.snapshot())
            ).planner_agent_ref
            == pin
        )
        assert (
            uow.planning_profiles.get(
                legacy.id, s["workspace"]
            ).planner_module_instance_id
            == module_id
        )
    page = SqlAlchemyPlanningQueryStore(database).list_profiles(
        s["workspace"], limit=10, cursor=None
    )
    assert {row.id for row in page.items} == {legacy.id, native.id}
    native_view = next(row for row in page.items if row.id == native.id)
    response = PlanningProfileResponse.from_view(native_view)
    assert response.execution_mode == "native_profile"
    assert response.planner_agent_ref == pin
    assert response.capabilities is None
    legacy_view = next(row for row in page.items if row.id == legacy.id)
    assert legacy_view.planner_provider == "fake"
    assert legacy_view.capabilities.structured_output == "json_schema"


def test_exact_runtime_and_action_scope_do_not_require_assignment_permission(
    scenario, authority
):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )

    s = scenario
    reader = ExecutionAuthorityReader(
        s["uow"],
        principal=lambda: s["principal"],
        authorizer=s["execution_scope"],
        clock=lambda: s["now"],
    )
    reader.require(authority.id, "runtime.read", authority.runtime_ref)
    reader.require(authority.id, "run.start", authority.profile_ref)
    with pytest.raises(DomainError):
        s["execution_scope"].require(
            authority.id,
            "assignment.write",
            type(
                "Target",
                (),
                {"workspace_id": s["workspace"], "principal_id": s["principal"]},
            )(),
        )
    for target in [
        object(),
        authority.runtime_ref.model_copy(update={"runtime_id": uuid4()}),
        authority.runtime_ref.model_copy(update={"workspace_id": uuid4()}),
    ]:
        with pytest.raises(AgentError):
            reader.require(authority.id, "runtime.read", target)


def test_pending_original_reads_survive_revocation_and_expiry_without_execution_rights(
    scenario, authority
):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )
    from sidera_control_plane.persistence.models import AssignmentLeaseModel

    s = scenario
    reader = ExecutionAuthorityReader(
        s["uow"],
        principal=lambda: s["principal"],
        authorizer=s["execution_scope"],
        clock=lambda: s["now"],
    )
    reader.require(authority.id, "run.read", s["workspace"])
    with s["uow"]() as uow:
        uow.execution_authorities.revoke(authority.id, s["workspace"])
        uow.session.execute(update(AssignmentLeaseModel).values(expires_at=s["now"]))
    s["execution_scope"].revoked = True
    reader.require(authority.id, "run.read", s["workspace"])
    reader.require(authority.id, "run.stop", s["workspace"])
    for action, target in [
        ("run.start", authority.profile_ref),
        ("run.input", s["workspace"]),
        ("run.recover", s["workspace"]),
        ("workspace.execute", authority.execution_workspace),
        ("artifact.read", authority.execution_workspace),
        ("secret.read", object()),
        ("tool.execute", object()),
    ]:
        with pytest.raises(AgentError):
            reader.require(authority.id, action, target)
    with pytest.raises(AgentError):
        reader.require(authority.id, "run.read", uuid4())
    stranger = ExecutionAuthorityReader(
        s["uow"], principal=lambda: "stranger", authorizer=s["execution_scope"]
    )
    with pytest.raises(AgentError):
        stranger.require(authority.id, "run.read", s["workspace"])


def test_interrupt_requires_explicit_action_and_current_mapped_run(scenario, authority):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )

    s = scenario
    reader = ExecutionAuthorityReader(
        s["uow"],
        principal=lambda: s["principal"],
        authorizer=s["execution_scope"],
        clock=lambda: s["now"],
    )
    with pytest.raises(AgentError):
        reader.require(authority.id, "run.interrupt", s["workspace"])
    with s["uow"]() as uow:
        uow.execution_authorities.advance_link(
            workspace_id=s["workspace"],
            execution_id=authority.execution_id,
            authority_id=authority.id,
            run_id=uuid4(),
            event_cursor=1,
            status="running",
        )
    reader.require(authority.id, "run.interrupt", s["workspace"])
    s["execution_scope"].actions = frozenset({"run.read"})
    with pytest.raises(AgentError):
        reader.require(authority.id, "run.interrupt", s["workspace"])


def test_link_query_authorizes_execution_then_canonical_target(
    scenario, authority, monkeypatch
):
    from sidera_control_plane.application.assignments import (
        AssignmentCommandHandler,
        TrustedAssignmentIdentity,
    )

    s = scenario
    permitted_targets = {("execution", authority.execution_id), ("subtask", s["task"])}

    class TargetScope:
        def require(self, grant_id, action, target):
            if (
                grant_id != s["grant"]
                or action != "assignment.read"
                or target.workspace_id != s["workspace"]
                or (target.target_type, target.target_id) not in permitted_targets
            ):
                raise DomainError("target scope denied")

    handler = AssignmentCommandHandler(
        identity=TrustedAssignmentIdentity(s["principal"], s["grant"]),
        authorizer=TargetScope(),
    )
    with s["uow"]() as uow:
        link = handler.get_agent_execution_link(
            uow, s["workspace"], authority.execution_id
        )
        assert link.execution_id == authority.execution_id
        from sidera_control_plane.persistence.execution_authority_repository import (
            ExecutionAuthorityRepository,
        )
        from sidera_control_plane.persistence.execution_repository import (
            ExecutionRepository,
        )

        def unreadable(*_):
            pytest.fail("forbidden execution lookup")

        permitted_targets.remove(("subtask", s["task"]))
        monkeypatch.setattr(ExecutionAuthorityRepository, "get_link", unreadable)
        with pytest.raises(DomainError):
            handler.get_agent_execution_link(
                uow, s["workspace"], authority.execution_id
            )
        monkeypatch.setattr(ExecutionRepository, "get", unreadable)
        with pytest.raises(DomainError):
            handler.get_agent_execution_link(uow, uuid4(), authority.execution_id)
        with pytest.raises(DomainError):
            handler.get_agent_execution_link(uow, s["workspace"], uuid4())


def test_historical_run_cannot_advance_a_replaced_binding(scenario, authority):
    from sidera_control_plane.persistence.models import AssignmentLeaseModel

    s = scenario
    run_id = uuid4()
    with s["uow"]() as uow:
        uow.execution_authorities.advance_link(
            workspace_id=s["workspace"],
            execution_id=authority.execution_id,
            authority_id=authority.id,
            run_id=run_id,
            event_cursor=1,
            status="stopped",
        )
        uow.session.execute(update(AssignmentLeaseModel).values(status="released"))
        uow.session.execute(
            update(ExecutionModel).values(status="succeeded", active_lease_id=None)
        )
    s["service"].handle(assign(s, human=True, version=2))
    with s["uow"]() as uow:
        with pytest.raises(DomainError):
            uow.execution_authorities.advance_link(
                workspace_id=s["workspace"],
                execution_id=authority.execution_id,
                authority_id=authority.id,
                run_id=run_id,
                event_cursor=2,
                status="running",
            )
        stored = uow.execution_authorities.get_link(
            s["workspace"], authority.execution_id
        )
        assert (stored.status, stored.event_cursor, stored.profile_ref) == (
            "stopped",
            1,
            authority.profile_ref,
        )
        assert (
            uow.agent_bindings.get(s["workspace"], "subtask", s["task"]).assignee.kind
            == "human"
        )


def test_concurrent_recovery_rechecks_revoked_scope_through_handle(
    scenario, monkeypatch
):
    from sidera_control_plane.persistence.repositories import (
        CommandRepository,
        ConcurrentCommandCommit,
    )

    s = scenario
    request = assign(s)
    s["service"].handle(request)
    s["catalog"].calls.clear()
    calls = []

    def concurrent(*_):
        calls.append("first receipt race")
        if len(calls) != 1:
            pytest.fail("recovery read a receipt after scope revocation")
        s["scope"].revoked = True
        raise ConcurrentCommandCommit("uq_command_idempotency")

    monkeypatch.setattr(CommandRepository, "find_by_id", concurrent)
    with pytest.raises(DomainError, match="denied"):
        s["service"].handle(request)
    assert calls == ["first receipt race"]
    assert s["catalog"].calls == []
