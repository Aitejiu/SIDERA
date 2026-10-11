import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from sidera_agents.contracts import (
    CapabilitySupport,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    RuntimeRef,
    RuntimeSnapshot,
)
from sidera_agents.errors import AgentError
from sqlalchemy import func, select

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.application.worker_commands import WorkerCommandHandler
from sidera_control_plane.composition.native_runtime import create_control_plane_runtime
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.result_integration import CheckSpecV1
from sidera_control_plane.domain.worker import WorkerDesiredState, WorkerStatus
from sidera_control_plane.persistence.models import (
    CommandModel,
    EventModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningProfileModel,
    TaskModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.support.native_agents import (
    PublicAgentCatalog,
    PublicAgentRuntimes,
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)
from tests.unit.test_git_workspace import _repository
from tests.unit.test_native_request_authority import NoNativeCalls
from tests.unit.test_native_workspace_setup import setup_request

pytestmark = pytest.mark.integration


class SetupCatalog(PublicAgentCatalog):
    def create_profile(self, workspace_id, draft, *, grant_id):
        self.calls.append(("create_profile", workspace_id, grant_id))
        profile = ProfileRevision(
            ref=ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1),
            draft=draft,
            config_hash=sha256(draft.model_dump_json().encode()).hexdigest(),
            created_at=datetime.now(UTC),
        )
        self.profiles[profile.ref] = profile
        return profile


@pytest.fixture
def public_setup_case(database, tmp_path):
    workspace_id, grant = uuid4(), uuid4()
    repository, _ = _repository(tmp_path)
    identity = ControlPlaneIdentity("operator", grant, "user")
    current, calls = [identity], []

    def require(supplied, action, target):
        calls.append((supplied, action, target))
        if (
            supplied != grant
            or action != "workspace.configure"
            or target.workspace_id != workspace_id
        ):
            raise AgentError("permission_denied", "Denied setup")

    authority = SimpleNamespace(
        identity_for_grant=lambda supplied: current[0] if supplied == grant else None,
        require=require,
    )
    runtime_ref = RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4())
    catalog = SetupCatalog(())
    profile = catalog.create_profile(
        workspace_id,
        ProfileDraft(
            name="Controlled Planner",
            description="",
            runtime_ref=runtime_ref,
            instructions="Controlled planning",
            resource_refs=(),
            model_id=None,
            thinking=None,
            max_active=2,
            environment={},
            secret_refs=(),
            native_settings={},
        ),
        grant_id=grant,
    )
    runtimes = PublicAgentRuntimes(
        RuntimeSnapshot(
            ref=runtime_ref,
            harness_id="controlled",
            executable="/unused",
            actual_version="1",
            installed=True,
            enabled=True,
            version=1,
            protocol_state="ready",
            auth_state="unconfirmed",
            capabilities={
                key: CapabilitySupport(
                    status="supported", reason_code="controlled", constraints={}
                )
                for key in ("acp", "model", "native_tools")
            },
            checked_at=datetime.now(UTC),
            limits={},
        )
    )
    native = NoNativeCalls()
    runtime = create_control_plane_runtime(
        Settings(
            database_url=database.engine.url.render_as_string(hide_password=False),
            local_actor_id="operator",
            local_actor_type="user",
        ),
        native,
        catalog,
        native,
        native,
        authority,
        agent_runtimes=runtimes,
    )
    request = replace(
        setup_request(workspace_id, repository), planner_agent_ref=profile.ref
    )
    try:
        yield SimpleNamespace(
            runtime=runtime,
            request=request,
            repository=repository,
            database=database,
            workspace_id=workspace_id,
            grant=grant,
            current=current,
            calls=calls,
            catalog=catalog,
            runtimes=runtimes,
            native=native,
            profile=profile,
        )
    finally:
        assert runtime.close() is True


def repository_facts(repository):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    hashes = {
        str(path.relative_to(repository)): sha256(path.read_bytes()).hexdigest()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repository).parts
    }
    return head, status, hashes


def setup_counts(case):
    with case.database.session() as session:
        return tuple(
            session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.workspace_id == case.workspace_id)
            )
            for model in (
                ModuleDefinitionModel,
                ModuleInstanceModel,
                PlanningProfileModel,
                TaskModel,
            )
        )


def test_public_setup_creates_only_native_business_configuration_and_is_idempotent(
    public_setup_case,
):
    s = public_setup_case
    (s.repository / "existing-dirty.txt").write_text("user-owned dirty content\n")
    before = repository_facts(s.repository)
    receipt = s.runtime.configure_workspace(s.request, grant_id=s.grant)
    assert (receipt.workspace_id, receipt.version, receipt.status) == (
        s.workspace_id,
        1,
        "created",
    )
    assert (
        receipt.planning_policy_ref.version == 1
        and receipt.planning_policy_ref.policy_id != s.profile.ref.profile_id
    )
    assert (
        receipt.repository_ref.startswith("git-repository://")
        and str(s.repository) not in receipt.repository_ref
    )
    counts = setup_counts(s)
    assert counts[0] > 0 and counts[1] > 0 and counts[2:] == (1, 0)
    with SqlAlchemyUnitOfWork(s.database) as uow:
        policy = uow.planning_profiles.get(
            receipt.planning_policy_ref.policy_id, s.workspace_id, version=1
        )
        assert (
            policy.execution_mode == "native_profile"
            and policy.planner_agent_ref == s.profile.ref
        )
        assert (
            policy.planner_module_instance_id
            is policy.planner_module_config_version
            is None
        )
        assert policy.budget == s.request.planning_budget
        result = policy.system_policy["result_integration"]
        assert (
            tuple(
                CheckSpecV1.from_mapping(check)
                for check in result["verification_check_specs"]
            )
            == s.request.verification_checks
        )
        assert result["verification_environment_ref"] == "env://owned-test"
        definitions = tuple(
            uow.session.scalars(
                select(ModuleDefinitionModel).where(
                    ModuleDefinitionModel.workspace_id == s.workspace_id
                )
            )
        )
        assert sum(row.provider == "agent-run" for row in definitions) == 1
        assert all(row.module_type != "planner_adapter" for row in definitions)
    again = s.runtime.configure_workspace(
        replace(
            s.request,
            expected_version=1,
            planner_agent_ref=None,
            verification_checks=None,
        ),
        grant_id=s.grant,
    )
    assert again == replace(receipt, status="unchanged")
    assert setup_counts(s) == counts and repository_facts(s.repository) == before
    assert not s.native.calls


@pytest.mark.parametrize("missing", ("planner", "checks"))
def test_first_setup_missing_required_input_is_actionable_and_writes_nothing(
    public_setup_case, missing
):
    s = public_setup_case
    request = replace(
        s.request,
        **{
            "planner_agent_ref" if missing == "planner" else "verification_checks": None
        },
    )
    before = repository_facts(s.repository)
    with pytest.raises(AgentError) as caught:
        s.runtime.configure_workspace(request, grant_id=s.grant)
    assert caught.value.code == "setup_required"
    assert caught.value.field == (
        "planner_selection_required"
        if missing == "planner"
        else "verification_policy_required"
    )
    assert setup_counts(s) == (0, 0, 0, 0) and repository_facts(s.repository) == before
    assert not s.native.calls


def test_setup_update_preserves_original_immutable_policy_and_modules(
    public_setup_case,
):
    s = public_setup_case
    first = s.runtime.configure_workspace(s.request, grant_id=s.grant)
    with SqlAlchemyUnitOfWork(s.database) as uow:
        original = uow.planning_profiles.get(
            first.planning_policy_ref.policy_id, s.workspace_id, version=1
        )
        original_modules = tuple(
            (row.id, dict(row.config))
            for row in uow.session.scalars(
                select(ModuleInstanceModel).where(
                    ModuleInstanceModel.workspace_id == s.workspace_id
                )
            )
        )
    changed_check = replace(s.request.verification_checks[0], timeout_seconds=7)
    request = replace(
        s.request,
        expected_version=1,
        planning_budget=replace(s.request.planning_budget, max_tokens=11000),
        verification_checks=(changed_check,),
    )
    second = s.runtime.configure_workspace(request, grant_id=s.grant)
    assert (second.version, second.status) == (
        2,
        "updated",
    ) and second.planning_policy_ref.policy_id != first.planning_policy_ref.policy_id
    with SqlAlchemyUnitOfWork(s.database) as uow:
        assert (
            uow.planning_profiles.get(
                first.planning_policy_ref.policy_id, s.workspace_id, version=1
            )
            == original
        )
        for module_id, config in original_modules:
            assert (
                dict(uow.modules.get_instance(module_id, s.workspace_id).config)
                == config
            )
        policy = uow.planning_profiles.get(
            second.planning_policy_ref.policy_id, s.workspace_id, version=2
        )
        assert (
            policy.system_policy["result_integration"]["verification_check_specs"][0][
                "timeout_seconds"
            ]
            == 7
        )
        assert policy.budget.max_tokens == 11000
        assert uow.session.get(WorkspaceModel, s.workspace_id).version == 2
        assert (
            uow.session.scalar(
                select(func.count())
                .select_from(ModuleDefinitionModel)
                .where(
                    ModuleDefinitionModel.workspace_id == s.workspace_id,
                    ModuleDefinitionModel.provider == "agent-run",
                )
            )
            == 1
        )
    counts = setup_counts(s)
    with pytest.raises(AgentError) as stale:
        s.runtime.configure_workspace(request, grant_id=s.grant)
    assert stale.value.code == "revision_conflict" and setup_counts(s) == counts
    s.current[0] = None
    with pytest.raises(AgentError) as revoked:
        s.runtime.configure_workspace(
            replace(request, expected_version=2), grant_id=s.grant
        )
    assert revoked.value.code == "permission_denied" and setup_counts(s) == counts
    assert not s.native.calls


def test_setup_configuration_failure_rolls_back_workspace_and_all_resources(
    public_setup_case, monkeypatch
):
    from sidera_control_plane.persistence.module_repository import ModuleRepository

    s = public_setup_case
    original = ModuleRepository.add_instance

    def fail_after_insert(repository, instance):
        original(repository, instance)
        raise RuntimeError("controlled configuration write failure")

    monkeypatch.setattr(ModuleRepository, "add_instance", fail_after_insert)
    with pytest.raises(RuntimeError, match="controlled configuration write failure"):
        s.runtime.configure_workspace(s.request, grant_id=s.grant)
    assert setup_counts(s) == (0, 0, 0, 0)
    with s.database.session() as session:
        assert session.get(WorkspaceModel, s.workspace_id) is None
    assert not s.native.calls


def test_setup_persists_exact_module_bound_slots_without_reconfiguring_history(
    public_setup_case,
):
    s = public_setup_case
    first = s.runtime.configure_workspace(s.request, grant_id=s.grant)
    with SqlAlchemyUnitOfWork(s.database) as uow:
        policy = uow.planning_profiles.get(
            first.planning_policy_ref.policy_id, s.workspace_id, version=1
        )
        binding = policy.system_policy["result_integration"]["verification_binding"]
        workspace = UUID(binding["workspace_module_ref"])
        verification = UUID(binding["adapter_module_ref"])
        metadata = dict(policy.system_policy["local_execution_slots"])
        agent_id = uuid5(workspace, "sidera.native.worker:agent-run")
        verification_id = uuid5(
            workspace, "sidera.native.worker:local-verification:" + verification.hex
        )
        assert metadata == {
            "schema_version": 1,
            "agent_run_worker_id": str(agent_id),
            "verification_worker_id": str(verification_id),
        }
        original_config = uow.worker_runtime_configurations.get_for_worker(
            agent_id, s.workspace_id
        )
        agent = uow.workers.get(agent_id, s.workspace_id)
        checked = uow.workers.get(verification_id, s.workspace_id)
        checked_config = uow.worker_runtime_configurations.get_for_worker(
            verification_id, s.workspace_id
        )
        assert agent.adapter_type == "agent-run" and agent.capacity == 2
        assert {"code", "python"} <= agent.capabilities
        assert policy.allowed_capabilities == ("code", "python")
        assert checked.adapter_type == "local_verification" and checked.capacity == 1
        assert agent.current_load == checked.current_load == 0
        assert agent.status is checked.status is WorkerStatus.IDLE
        assert agent.desired_state is checked.desired_state is WorkerDesiredState.ACTIVE
        assert (
            original_config.workspace_module_instance_id
            == checked_config.workspace_module_instance_id
            == workspace
        )
        assert checked_config.adapter_module_instance_id == verification
    second = s.runtime.configure_workspace(
        replace(
            s.request,
            expected_version=1,
            verification_checks=(
                replace(s.request.verification_checks[0], timeout_seconds=9),
            ),
        ),
        grant_id=s.grant,
    )
    with SqlAlchemyUnitOfWork(s.database) as uow:
        updated = uow.planning_profiles.get(
            second.planning_policy_ref.policy_id, s.workspace_id, version=2
        )
        next_slots = updated.system_policy["local_execution_slots"]
        assert next_slots["agent_run_worker_id"] == str(agent_id)
        assert next_slots["verification_worker_id"] != str(verification_id)
        assert (
            uow.worker_runtime_configurations.get_for_worker(agent_id, s.workspace_id)
            == original_config
        )
        assert (
            uow.worker_runtime_configurations.get_for_worker(
                verification_id, s.workspace_id
            )
            == checked_config
        )
        assert len(uow.workers.list_for_workspace(s.workspace_id)) == 3
    assert not s.native.calls


def test_saved_native_repository_reader_discovers_new_setup_and_immutable_history(
    public_setup_case, tmp_path
):
    from importlib import import_module

    from sidera_control_plane.domain.result_operations import ResultError

    s = public_setup_case
    reader_type = import_module(
        "sidera_control_plane.persistence.native_repository_resolver"
    ).SavedNativeRepositories
    reader = reader_type(lambda: SqlAlchemyUnitOfWork(s.database))
    assert reader.repository_refs() == ()
    first = s.runtime.configure_workspace(s.request, grant_id=s.grant)
    assert reader.repository_refs() == (first.repository_ref,)
    assert reader(first.repository_ref) == s.repository.resolve()
    second_root = tmp_path / "second"
    second_root.mkdir()
    second_repository, _ = _repository(second_root)
    before = repository_facts(s.repository), repository_facts(second_repository)
    second = s.runtime.configure_workspace(
        replace(s.request, expected_version=1, repository_directory=second_repository),
        grant_id=s.grant,
    )
    fresh = reader_type(lambda: SqlAlchemyUnitOfWork(s.database))
    assert fresh.repository_refs() == tuple(
        sorted((first.repository_ref, second.repository_ref))
    )
    assert fresh(first.repository_ref) == s.repository.resolve()
    assert fresh(second.repository_ref) == second_repository.resolve()
    with pytest.raises(ResultError) as missing:
        fresh("git-repository://" + "f" * 64)
    assert missing.value.code == "result_repository_unconfigured"
    assert before == (
        repository_facts(s.repository),
        repository_facts(second_repository),
    )
    assert not s.native.calls


def test_public_startup_heartbeats_only_persisted_owned_slots_and_discovers_later_setup(
    public_setup_case, tmp_path
):
    s = public_setup_case
    first = s.runtime.configure_workspace(s.request, grant_id=s.grant)
    with SqlAlchemyUnitOfWork(s.database) as uow:
        policy = uow.planning_profiles.get(
            first.planning_policy_ref.policy_id, s.workspace_id, version=1
        )
        metadata = policy.system_policy["local_execution_slots"]
        agent_id, verification_id = (
            UUID(metadata["agent_run_worker_id"]),
            UUID(metadata["verification_worker_id"]),
        )
    commands = CommandService(
        lambda: SqlAlchemyUnitOfWork(s.database),
        (WorkerCommandHandler(lambda: datetime.now(UTC)),),
    )

    def command(kind, target=None, version=None, payload=None):
        command_id = uuid4()
        return commands.handle(
            CommandEnvelope(
                id=command_id,
                workspace_id=s.workspace_id,
                command_type=kind,
                target_type="worker",
                actor_type="user",
                actor_id="operator",
                target_id=target,
                expected_version=version,
                payload=payload or {},
                idempotency_key=str(command_id),
                correlation_id=command_id,
            )
        )

    foreign_id = command(
        "RegisterWorker",
        payload={
            "adapter_type": "agent-run",
            "display_name": "Unowned dispatcher",
            "capabilities": ["code", "python"],
            "capacity": 2,
            "cost_per_hour_microunits": 0,
            "startup_latency_ms": 0,
        },
    ).aggregate_id
    command("DrainWorker", verification_id, 1)
    with SqlAlchemyUnitOfWork(s.database) as uow:
        for worker_id in (agent_id, foreign_id):
            worker = uow.workers.get(worker_id, s.workspace_id)
            previous = worker.version
            worker.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=90)
            worker.mark_offline()
            uow.workers.save(worker, previous)
        foreign_before = uow.workers.get(foreign_id, s.workspace_id)
        draining_before = uow.workers.get(verification_id, s.workspace_id)
    assert s.runtime.close() is True
    background = ControlPlaneIdentity("operator", uuid4(), "system")
    human = s.current[0]
    allowed_workspaces = {s.workspace_id}
    authority = SimpleNamespace(
        identity_for_grant=lambda grant: (
            background
            if grant == background.grant_id
            else human
            if grant == human.grant_id
            else None
        ),
        require=lambda grant, action, target: (
            None
            if grant == human.grant_id
            and action == "workspace.configure"
            and target.workspace_id in allowed_workspaces
            else (_ for _ in ()).throw(AgentError("permission_denied", "Denied"))
        ),
    )
    (tmp_path / "owned-workspaces").mkdir()
    runtime = create_control_plane_runtime(
        Settings(
            database_url=s.database.engine.url.render_as_string(hide_password=False),
            local_actor_id="operator",
            local_actor_type="user",
            workspace_root=tmp_path / "owned-workspaces",
            planning_temp_root=tmp_path / "planning",
            result_store_root=tmp_path / "results",
            knowledge_candidate_spool_dir=tmp_path / "spool",
            assignment_lease_renew_interval_seconds=0.05,
        ),
        s.native,
        s.catalog,
        s.native,
        s.native,
        authority,
        agent_runtimes=s.runtimes,
    )
    try:
        runtime.start_background(background_identity=background)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with SqlAlchemyUnitOfWork(s.database) as uow:
                restored = uow.workers.get(agent_id, s.workspace_id)
            if (
                restored.status is WorkerStatus.IDLE
                and restored.last_heartbeat_at
                > datetime.now(UTC) - timedelta(seconds=45)
            ):
                break
            time.sleep(0.01)
        assert restored.status is WorkerStatus.IDLE and restored.version > 2
        with SqlAlchemyUnitOfWork(s.database) as uow:
            assert uow.workers.get(foreign_id, s.workspace_id) == foreign_before
            assert uow.workers.get(verification_id, s.workspace_id) == draining_before
            assert tuple(
                worker.id
                for worker in uow.workers.lock_candidates(
                    workspace_id=s.workspace_id,
                    required_capabilities=frozenset({"code", "python"}),
                    pinned_worker_id=None,
                    heartbeat_cutoff=datetime.now(UTC) - timedelta(seconds=45),
                )
            ) == (agent_id,)
            events = tuple(
                uow.session.scalars(
                    select(EventModel).where(
                        EventModel.aggregate_id == agent_id,
                        EventModel.event_type == "WorkerHeartbeatRecorded",
                    )
                )
            )
            assert events and all(
                (event.actor_type, event.actor_id) == ("system", "operator")
                for event in events
            )
            for event in events:
                assert (
                    uow.session.get(CommandModel, event.causation_id).status
                    == "applied"
                )
        later_id = uuid4()
        allowed_workspaces.add(later_id)
        later_runtime_ref = RuntimeRef(workspace_id=later_id, runtime_id=uuid4())
        later_profile = s.catalog.create_profile(
            later_id,
            s.profile.draft.model_copy(update={"runtime_ref": later_runtime_ref}),
            grant_id=human.grant_id,
        )
        original_runtime = s.runtimes.runtime
        s.runtimes.runtime = original_runtime.model_copy(
            update={"ref": later_runtime_ref}
        )
        later = runtime.configure_workspace(
            replace(
                s.request, workspace_id=later_id, planner_agent_ref=later_profile.ref
            ),
            grant_id=human.grant_id,
        )
        with SqlAlchemyUnitOfWork(s.database) as uow:
            policy = uow.planning_profiles.get(
                later.planning_policy_ref.policy_id, later_id, version=1
            )
            later_agent = UUID(
                policy.system_policy["local_execution_slots"]["agent_run_worker_id"]
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with SqlAlchemyUnitOfWork(s.database) as uow:
                discovered = uow.workers.get(later_agent, later_id)
            if discovered.version > 1:
                break
            time.sleep(0.01)
        assert discovered.version > 1 and discovered.status is WorkerStatus.IDLE
        assert not s.native.calls
    finally:
        assert runtime.close(grace_seconds=5) is True
        assert all(not thread.is_alive() for thread in runtime._threads)
