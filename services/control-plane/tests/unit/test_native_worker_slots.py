from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from sidera_control_plane.composition.native_setup import _resources
from sidera_control_plane.domain.modules import ModuleLifecycle
from sidera_control_plane.domain.native_setup import NATIVE_SETUP_POLICY_KEY
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from tests.unit.test_native_workspace_setup import setup_request


def slot_fixture():
    workspace_id = uuid4()
    checks = setup_request(workspace_id, Path("/unused")).verification_checks
    definitions, instances = _resources(workspace_id, Path("/unused"), "a" * 40, checks)
    agent, workspace, verification, _ = instances
    agent_id = uuid5(workspace.id, "sidera.native.worker:agent-run")
    verification_id = uuid5(
        workspace.id, "sidera.native.worker:local-verification:" + verification.id.hex
    )
    binding = {
        "adapter_module_ref": str(verification.id),
        "adapter_config_version": 1,
        "workspace_module_ref": str(workspace.id),
        "workspace_config_version": 1,
    }
    policy = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        version=1,
        profile_key=NATIVE_SETUP_POLICY_KEY,
        execution_mode="native_profile",
        system_policy={
            "result_integration": {"verification_binding": binding},
            "local_execution_slots": {
                "schema_version": 1,
                "agent_run_worker_id": str(agent_id),
                "verification_worker_id": str(verification_id),
            },
        },
    )
    workers = {
        worker_id: Worker.register(
            worker_id=worker_id,
            workspace_id=workspace_id,
            adapter_type=provider,
            display_name="Owned fixture",
            capabilities=frozenset({"code", "python"}),
            capacity=2 if provider == "agent-run" else 1,
            cost_per_hour_microunits=0,
            startup_latency_ms=0,
            now=datetime.now(UTC),
        )
        for worker_id, adapter, provider in (
            (agent_id, agent, "agent-run"),
            (verification_id, verification, "local_verification"),
        )
    }
    configurations = {
        worker_id: WorkerRuntimeConfiguration.create(
            worker_id=worker_id,
            workspace_id=workspace_id,
            adapter_module_instance_id=adapter.id,
            adapter_module_config_version=1,
            workspace_module_instance_id=workspace.id,
            workspace_module_config_version=1,
        )
        for worker_id, adapter in ((agent_id, agent), (verification_id, verification))
    }
    modules = {item.id: item for item in instances}
    module_definitions = {item.id: item for item in definitions}
    policies, queries = [policy], []

    def rows(statement):
        queries.append(statement)
        return tuple((item.id, item.workspace_id, item.version) for item in policies)

    uow = SimpleNamespace(
        session=SimpleNamespace(execute=rows),
        planning_profiles=SimpleNamespace(
            get=lambda policy_id, workspace_id, *, version: next(
                (
                    item
                    for item in policies
                    if (item.id, item.workspace_id, item.version)
                    == (policy_id, workspace_id, version)
                ),
                None,
            )
        ),
        workers=SimpleNamespace(
            get=lambda worker_id, workspace_id: workers.get(worker_id)
        ),
        worker_runtime_configurations=SimpleNamespace(
            get_for_worker=lambda worker_id, workspace_id: configurations.get(worker_id)
        ),
        modules=SimpleNamespace(
            get_instance=lambda module_id, workspace_id: modules.get(module_id),
            get_definition=lambda module_id, workspace_id: module_definitions.get(
                module_id
            ),
        ),
    )
    return SimpleNamespace(
        uow=uow,
        workers=workers,
        configurations=configurations,
        modules=modules,
        definitions=module_definitions,
        policies=policies,
        queries=queries,
        agent_id=agent_id,
        verification_id=verification_id,
        workspace=workspace,
    )


def test_slot_membership_is_inert_fresh_and_deduplicates_original_policy_versions():
    s = slot_fixture()
    reader_type = import_module(
        "sidera_control_plane.persistence.native_worker_slots"
    ).SavedNativeWorkerSlots
    reader = reader_type(lambda: nullcontext(s.uow))
    assert not s.queries
    assert {worker.id for worker in reader.owned_slots()} == {
        s.agent_id,
        s.verification_id,
    }
    s.policies.append(
        SimpleNamespace(**{**vars(s.policies[0]), "id": uuid4(), "version": 2})
    )
    assert {worker.id for worker in reader.owned_slots()} == {
        s.agent_id,
        s.verification_id,
    }
    s.policies.clear()
    assert reader.owned_slots() == ()


@pytest.mark.parametrize(
    "invalid",
    (
        "metadata",
        "result_metadata",
        "schema_bool",
        "ids",
        "legacy",
        "worker",
        "foreign_worker",
        "configuration",
        "workspace_version",
        "module",
        "foreign_module",
        "disabled",
        "provider",
        "draining",
    ),
)
def test_slot_membership_does_not_adopt_invalid_or_unowned_dispatchers(invalid):
    s = slot_fixture()
    policy = s.policies[0]
    if invalid == "metadata":
        del policy.system_policy["local_execution_slots"]
    elif invalid == "result_metadata":
        policy.system_policy["result_integration"] = None
    elif invalid == "schema_bool":
        policy.system_policy["local_execution_slots"]["schema_version"] = True
    elif invalid == "ids":
        policy.system_policy["local_execution_slots"]["agent_run_worker_id"] = str(
            uuid4()
        )
    elif invalid == "legacy":
        policy.profile_key = "external"
    elif invalid == "worker":
        del s.workers[s.agent_id]
    elif invalid == "foreign_worker":
        s.workers[s.agent_id] = replace(s.workers[s.agent_id], workspace_id=uuid4())
    elif invalid == "configuration":
        s.configurations[s.agent_id] = replace(
            s.configurations[s.agent_id], adapter_module_config_version=2
        )
    elif invalid == "workspace_version":
        s.configurations[s.agent_id] = replace(
            s.configurations[s.agent_id], workspace_module_config_version=2
        )
    elif invalid == "module":
        del s.modules[s.workspace.id]
    elif invalid == "foreign_module":
        s.modules[s.workspace.id] = replace(s.workspace, workspace_id=uuid4())
    elif invalid == "disabled":
        s.modules[s.workspace.id] = replace(
            s.workspace, lifecycle=ModuleLifecycle.DISABLED
        )
    elif invalid == "provider":
        definition = s.definitions[s.workspace.definition_id]
        s.definitions[definition.id] = replace(definition, provider="foreign")
    else:
        s.workers[s.agent_id].drain()
    before = {worker_id: replace(worker) for worker_id, worker in s.workers.items()}
    reader = import_module(
        "sidera_control_plane.persistence.native_worker_slots"
    ).SavedNativeWorkerSlots(lambda: nullcontext(s.uow))
    owned = {worker.id for worker in reader.owned_slots()}
    assert s.agent_id not in owned
    assert s.workers == before


@pytest.mark.parametrize("value", ([], {}, True, 7))
def test_slot_membership_rejects_non_text_saved_ids_without_coercion(value):
    s = slot_fixture()
    s.policies[0].system_policy["local_execution_slots"]["agent_run_worker_id"] = value
    reader = import_module(
        "sidera_control_plane.persistence.native_worker_slots"
    ).SavedNativeWorkerSlots(lambda: nullcontext(s.uow))
    assert reader.owned_slots() == ()


@pytest.mark.parametrize(
    "loss",
    (None, "initial_owner", "initial_grant", "final_owner", "final_grant", "version"),
)
def test_slot_heartbeat_uses_real_commands_and_fences_transaction(loss, monkeypatch):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.application.request_authority import ControlPlaneIdentity
    from sidera_control_plane.config import Settings
    from sidera_control_plane.domain.worker import WorkerStatus
    from tests.unit.test_command_service import FakeCommandRepository
    from tests.unit.test_native_request_authority import NoNativeCalls, runtime_module

    module = runtime_module()
    s = slot_fixture()
    s.workers[s.agent_id].reserve()
    s.workers[s.agent_id].mark_offline()
    s.workers[s.verification_id].drain()
    identity = ControlPlaneIdentity("operator", uuid4(), "system")
    current, alive = [identity], [True]
    native, commands, events, transactions = (
        NoNativeCalls(),
        FakeCommandRepository(),
        [],
        [],
    )
    runtime = module.create_control_plane_runtime(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
            local_actor_id="operator",
        ),
        native,
        native,
        native,
        native,
        SimpleNamespace(
            identity_for_grant=lambda grant: (
                current[0] if grant == identity.grant_id else None
            )
        ),
        agent_runtimes=native,
    )

    def require_owner():
        if not alive[0]:
            raise AgentError("gateway_owner_lost", "Owner lost")

    runtime._owner = SimpleNamespace(require_owner=require_owner)
    runtime._background_identity = identity
    s.uow.commands = commands
    s.uow.events = SimpleNamespace(append_with_outbox=events.append)
    s.uow.savepoint = nullcontext
    s.uow.workers.get = lambda worker_id, workspace_id: (
        replace(s.workers[worker_id]) if worker_id in s.workers else None
    )

    def save(worker, previous_version):
        assert s.workers[worker.id].version == previous_version
        s.workers[worker.id] = worker
        if loss == "final_owner":
            alive[0] = False
        elif loss == "final_grant":
            current[0] = None

    s.uow.workers.save = save

    @contextmanager
    def transaction(database, **kwargs):
        transactions.append(kwargs)
        before = deepcopy(s.workers), deepcopy(commands.records), list(events)
        try:
            yield s.uow
            if kwargs.get("before_commit") is not None:
                kwargs["before_commit"]()
        except BaseException:
            s.workers.clear()
            s.workers.update(before[0])
            commands.records = before[1]
            events[:] = before[2]
            raise

    monkeypatch.setattr(module, "SqlAlchemyUnitOfWork", transaction)
    before = deepcopy(s.workers)
    if loss == "initial_owner":
        alive[0] = False
    elif loss == "initial_grant":
        current[0] = None
    elif loss == "version":
        original_get = s.uow.workers.get
        reads = [0]

        def concurrent_get(worker_id, workspace_id):
            if worker_id == s.agent_id:
                reads[0] += 1
                if reads[0] == 3:
                    s.workers[worker_id].release()
            return original_get(worker_id, workspace_id)

        s.uow.workers.get = concurrent_get
    try:
        callback = getattr(runtime, "_heartbeat_owned_slots", None)
        assert callable(callback), (
            "Ordinary runtime has no owned slot heartbeat callback"
        )
        if loss in {"initial_owner", "initial_grant", "final_owner", "final_grant"}:
            with pytest.raises(AgentError) as denied:
                callback()
            assert denied.value.code == (
                "gateway_owner_lost" if "owner" in loss else "permission_denied"
            )
            assert s.workers == before and not commands.records and not events
            if loss.startswith("initial"):
                assert transactions == []
        else:
            callback()
            if loss == "version":
                assert [record.status for record in commands.records.values()] == [
                    "rejected"
                ]
                callback()
            worker = s.workers[s.agent_id]
            assert worker.last_heartbeat_at >= before[s.agent_id].last_heartbeat_at
            assert worker.status is (
                WorkerStatus.IDLE if loss == "version" else WorkerStatus.BUSY
            )
            assert worker.current_load == (0 if loss == "version" else 1)
            assert (
                worker.capacity == 2
                and s.workers[s.verification_id] == before[s.verification_id]
            )
            applied = [
                record
                for record in commands.records.values()
                if record.status == "applied"
            ]
            assert len(applied) == len(events) == 1
            assert (
                applied[0].actor_type,
                applied[0].actor_id,
                applied[0].target_type,
            ) == ("system", "operator", "worker")
            assert events[0].event_type == "WorkerHeartbeatRecorded"
        assert not native.calls
    finally:
        runtime._owner = None
        runtime.close()
