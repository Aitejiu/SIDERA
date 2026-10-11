from datetime import timedelta
from uuid import uuid4

import pytest
from sidera_agents.contracts import StartReceipt

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.domain.errors import DomainError
from tests.integration.test_agent_context_api import SourceScenario
from tests.support.native_agents import (
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)

pytestmark = pytest.mark.integration


@pytest.fixture
def dispatch_source(database, workspace_id, tmp_path):
    return SourceScenario(database, workspace_id, tmp_path)


def start_intention(s, prepared):
    with s.uow() as uow:
        begin = getattr(uow.execution_authorities, "begin_start", None)
        assert callable(begin), (
            "N12 requires a durable request fingerprint and start CAS"
        )
        return begin(prepared["link"], request_sha256="a" * 64, now=s.cp.now)


def busy_intention(s, prepared):
    starting = start_intention(s, prepared)
    with s.uow() as uow:
        return uow.execution_authorities.record_start(
            starting,
            StartReceipt(
                operation_id=starting.start_operation_id,
                status="busy",
                run_id=None,
                reason_code="runtime_capacity",
            ),
            now=s.cp.now,
        )


def change_owner(s, prepared, *, human=True):
    item_id = prepared["execution"].worker_item_id
    with s.uow() as uow:
        item = uow.worker_items.get(item_id, s.workspace_id)
    return s.commands.handle(
        s.command(
            "SetWorkerItemAssignee",
            "worker_item",
            item_id,
            item.version,
            {
                "assignee_kind": "human" if human else "profile",
                "assignee_id": s.principal if human else str(s.profile.ref.profile_id),
                "profile_revision": None if human else 1,
            },
        )
    )


def test_busy_owner_change_archives_pin_and_requeues_same_execution(native_scenario):
    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    s.start()
    busy = s.link()
    with s.uow() as uow:
        original = uow.executions.get(busy.execution_id, s.workspace_id)
        authority = uow.execution_authorities.get(busy.authority_id)
    changed = s.assign(human=True)
    with s.uow() as uow:
        execution = uow.executions.get(busy.execution_id, s.workspace_id)
        history = uow.execution_authorities.admission_history(
            s.workspace_id, execution.id
        )
        assert len(history) == 1
        assert history[0].link == busy
        assert history[0].cause_command_id == changed.command_id
        assert uow.execution_authorities.get_link(s.workspace_id, execution.id) is None
        assert uow.execution_authorities.get(busy.authority_id).revoked
        assert (
            uow.leases.get(authority.lease_id, s.workspace_id).status.value
            == "released"
        )
        assert execution.status.value == "queued"
        assert execution.active_lease_id is execution.assigned_worker_id is None
        assert (
            execution.id,
            execution.attempt,
            execution.lineage_id,
            execution.last_fencing_token,
        ) == (
            original.id,
            1,
            original.lineage_id,
            1,
        )


def test_busy_retry_preserves_sha_operation_and_blocks_owner_change_in_flight(
    dispatch_source,
):
    s = dispatch_source
    prepared = s.prepare("a")
    busy = busy_intention(s, prepared)
    with s.uow() as uow:
        starting = uow.execution_authorities.begin_start(
            busy, request_sha256="a" * 64, now=s.cp.now
        )
    assert (
        starting.start_operation_id,
        starting.authority_id,
        starting.request_sha256,
    ) == (
        busy.start_operation_id,
        busy.authority_id,
        "a" * 64,
    )
    with pytest.raises(DomainError) as denied:
        change_owner(s, prepared)
    assert denied.value.code == "handoff_required"


def test_owner_change_wins_before_retry_and_old_pin_cannot_start(native_scenario):
    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    s.start()
    busy = s.link()
    s.assign(human=True)
    with s.uow() as uow, pytest.raises(DomainError):
        uow.execution_authorities.begin_start(
            busy, request_sha256=busy.request_sha256, now=s.cp.now
        )


@pytest.mark.parametrize("case", ["changed_request", "expired", "unrelated_receipt"])
def test_start_identity_and_live_lease_are_not_waived(dispatch_source, case):
    s = dispatch_source
    prepared = s.prepare("a")
    busy = busy_intention(s, prepared)
    with s.uow() as uow, pytest.raises(DomainError):
        if case == "unrelated_receipt":
            uow.execution_authorities.record_start(
                busy,
                StartReceipt(
                    operation_id=uuid4(),
                    status="busy",
                    run_id=None,
                    reason_code="runtime_capacity",
                ),
                now=s.cp.now,
            )
        else:
            uow.execution_authorities.begin_start(
                busy,
                request_sha256=("b" if case == "changed_request" else "a") * 64,
                now=s.cp.now
                + (timedelta(hours=2) if case == "expired" else timedelta()),
            )


def test_assignment_failure_rolls_back_retirement(native_scenario, monkeypatch):
    from sidera_control_plane.persistence.agent_binding_repository import (
        AgentBindingRepository,
    )

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    s.start()
    busy = s.link()

    def fail_write(*args):
        raise RuntimeError("bounded injected binding failure")

    monkeypatch.setattr(AgentBindingRepository, "append", fail_write)
    with pytest.raises(RuntimeError, match="bounded injected"):
        s.assign(human=True)
    with s.uow() as uow:
        assert (
            uow.execution_authorities.get_link(s.workspace_id, busy.execution_id)
            == busy
        )
        assert (
            uow.execution_authorities.admission_history(
                s.workspace_id, busy.execution_id
            )
            == ()
        )
        assert not uow.execution_authorities.get(busy.authority_id).revoked
        assert (
            uow.executions.get(busy.execution_id, s.workspace_id).status.value
            == "leased"
        )


@pytest.fixture
def native_scenario(database, workspace_id, tmp_path):
    return NativeScenario(database, workspace_id, tmp_path)


class NativeScenario:
    def __init__(
        self,
        database,
        workspace_id,
        tmp_path,
        *,
        pipeline=False,
        principal_id="operator",
    ):
        import inspect

        from sidera_control_plane.adapters.git_results import GitResultStore
        from sidera_control_plane.adapters.git_workspace import (
            GitWorktreeWorkspaceManager,
            opaque_repository_ref,
        )
        from sidera_control_plane.application.agent_sources import (
            TaskContextSourceReader,
        )
        from sidera_control_plane.application.agent_worker_adapter import (
            NativeWorkerServices,
            native_capabilities,
        )
        from sidera_control_plane.application.artifact_content import (
            ArtifactContentService,
        )
        from sidera_control_plane.application.assignments import (
            AssignmentCommandHandler,
            TrustedAssignmentIdentity,
        )
        from sidera_control_plane.application.code_result_capture import (
            CodeResultCaptureService,
        )
        from sidera_control_plane.application.context_materialization import (
            ContextMaterializer,
        )
        from sidera_control_plane.application.execution_authority import (
            ExecutionAuthorityReader,
        )
        from sidera_control_plane.application.scheduler import Scheduler
        from sidera_control_plane.application.worker_gateway import WorkerGateway
        from sidera_control_plane.application.worker_policy import (
            LowestCostWorkerPolicy,
        )
        from sidera_control_plane.application.worker_runtime import (
            WorkerRuntimeResolver,
        )
        from sidera_control_plane.domain.modules import (
            WorkspaceProviderCapabilityProfile,
        )
        from tests.integration.result_input_scenario import (
            ResultInputScenario,
            ResultInputWorkerRuntime,
            _EmptyCatalog,
        )
        from tests.integration.test_agent_context_api import SourceScenario
        from tests.support.native_agents import AssignmentScope
        from tests.unit.test_git_workspace import _repository

        self.repository, self.revision = _repository(tmp_path)
        root = tmp_path / "native-worktrees"
        root.mkdir()
        self.manager = GitWorktreeWorkspaceManager(root)
        runtime = ResultInputWorkerRuntime(
            adapter_provider="agent-run",
            adapter_interface_version="sidera.worker-adapter.v2",
            adapter_capabilities=native_capabilities(),
            adapter_factory=lambda instance: None,
            adapter_config_namespace="adapter",
            adapter_config={},
            workspace_provider="git-worktree",
            workspace_interface_version="sidera.workspace-provider.v1",
            workspace_capabilities=WorkspaceProviderCapabilityProfile(
                isolation="filesystem",
                writable=True,
                git_worktree=True,
                stability="stable",
                code_input_versions=(1,),
            ),
            workspace_factory=lambda instance: self.manager,
            workspace_config_namespace="workspace",
            workspace_config={
                "workspace.repository_ref": str(self.repository),
                "workspace.base_revision": self.revision,
            },
        )
        self.cp = ResultInputScenario(
            database, workspace_id, tmp_path, worker_runtime=runtime
        )
        self.workspace_id, self.uow, self.principal = (
            workspace_id,
            self.cp.uow_factory,
            principal_id,
        )
        self.verification_adapter = None
        if pipeline:
            self._confirm_pipeline(tmp_path, runtime)
        else:
            self.cp.confirm_plan()
            self.cp._ensure_source_executions()
        # Reuse only N11's complete public catalog fixtures, not its leased setup.
        self.setup_grant = uuid4()
        from sidera_agents.contracts import (
            CapabilitySupport,
            ProfileDraft,
            ProfileRef,
            ProfileRevision,
            RuntimeRef,
            RuntimeSnapshot,
        )

        from tests.support.native_agents import PublicAgentCatalog, PublicAgentRuntimes

        ref = RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4())
        self.profile = ProfileRevision(
            ref=ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1),
            draft=ProfileDraft(
                name="Fixture",
                description="",
                runtime_ref=ref,
                instructions="PRIVATE PROFILE PROMPT",
                resource_refs=(),
                model_id=None,
                thinking=None,
                max_active=4,
                environment={},
                secret_refs=(),
                native_settings={},
            ),
            config_hash="fixture",
            created_at=self.cp.now,
        )
        self.catalog = PublicAgentCatalog((self.profile,))
        self.runtimes = PublicAgentRuntimes(
            RuntimeSnapshot(
                ref=ref,
                harness_id="fixture",
                executable="/unused",
                actual_version="1",
                installed=True,
                enabled=True,
                version=1,
                protocol_state="ready",
                auth_state="unconfirmed",
                capabilities={
                    "acp": CapabilitySupport(
                        status="supported", reason_code="fixture_only", constraints={}
                    )
                },
                checked_at=self.cp.now,
                limits={},
            )
        )
        self.assignment = AssignmentCommandHandler(
            catalog=self.catalog,
            runtimes=self.runtimes,
            identity=TrustedAssignmentIdentity(self.principal, self.setup_grant),
            authorizer=AssignmentScope(workspace_id, self.principal, self.setup_grant),
            clock=self.cp.clock,
        )
        self.commands = CommandService(
            self.uow, (self.assignment,), preflight=self.assignment.preflight
        )
        self.command = lambda *args: SourceScenario.command(self, *args)
        self.execution = PublicNativeExecution(self)
        self.denied_worker_actions = set()

        self.worker_scope_calls = []
        class WorkerScope:
            def require(scope, grant_id, action, target):
                assert action != "assignment.write"
                self.worker_scope_calls.append((grant_id, action, target))
                if (
                    grant_id != self.setup_grant
                    or action in self.denied_worker_actions
                    or target.workspace_id != workspace_id
                    or target.principal_id != self.principal
                ):
                    raise DomainError("worker scope denied")

        self.services = NativeWorkerServices(
            self.catalog,
            self.runtimes,
            self.execution,
            WorkerScope(),
            self.principal,
            self.setup_grant,
        )
        assert "native_services" in inspect.signature(WorkerGateway).parameters, (
            "N12 Gateway must select the canonical native binding"
        )
        assert (
            "native_services" in inspect.signature(WorkerRuntimeResolver).parameters
        ), "N12 Scheduler must resolve a pinned public Profile"
        knowledge = _EmptyCatalog()
        authority_reader = ExecutionAuthorityReader(
            self.uow,
            principal=lambda: self.principal,
            authorizer=self.services.authorizer,
            clock=self.cp.clock,
        )
        source_reader = TaskContextSourceReader(
            self.uow,
            authorizer=authority_reader,
            catalog=knowledge,
            result_input_content=ArtifactContentService(
                database, self.cp.artifact_store
            ),
            clock=self.cp.clock,
        )
        self.capture = CodeResultCaptureService(
            self.uow,
            GitResultStore({opaque_repository_ref(self.repository): self.repository}),
            artifact_content_store=self.cp.artifact_store,
            clock=self.cp.clock,
        )
        self.gateway = WorkerGateway(
            uow_factory=self.uow,
            adapter_factories={}
            if not pipeline
            else {"local_verification": lambda instance: self.verification_adapter},
            workspace_factories={"git-worktree": lambda instance: self.manager},
            context_materializer=ContextMaterializer(
                knowledge,
                result_input_content=ArtifactContentService(
                    database, self.cp.artifact_store
                ),
            ),
            native_services=self.services,
            code_result_capture_service=self.capture,
            artifact_content_store=self.cp.artifact_store,
            native_source_reader=source_reader,
            clock=self.cp.clock,
            assignment_lease_ttl_seconds=300,
            result_integration_enabled=pipeline,
        )
        self.scheduler = Scheduler(
            self.uow,
            LowestCostWorkerPolicy(),
            self.cp.clock,
            context_enforcement=True,
            assignment_lease_seconds=300,
            runtime_resolver=WorkerRuntimeResolver(native_services=self.services),
        )
        self.worker_id, _, _ = self.cp._install_capture_runtime(
            self.cp._execution_id("a"), None
        )

    def _confirm_pipeline(self, tmp_path, runtime):
        import sys

        from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
        from sidera_control_plane.adapters.local_verification import (
            LocalCheckRunner,
            LocalVerificationAdapter,
        )
        from sidera_control_plane.domain.modules import (
            ModuleDefinition,
            ModuleInstance,
            ModuleLifecycle,
        )
        from sidera_control_plane.domain.worker import Worker
        from sidera_control_plane.domain.worker_runtime import (
            WorkerRuntimeConfiguration,
        )
        from tests.integration import result_input_scenario

        instances = []
        with self.uow() as uow:
            for module_type, provider, capabilities, config in (
                (
                    "worker_adapter",
                    "local_verification",
                    LocalVerificationAdapter.capabilities(),
                    {},
                ),
                (
                    "workspace_provider",
                    "git-worktree",
                    runtime.workspace_capabilities,
                    dict(runtime.workspace_config),
                ),
            ):
                definition = ModuleDefinition(
                    uuid4(),
                    self.workspace_id,
                    module_type,
                    provider,
                    "sidera.worker-adapter.v2"
                    if module_type == "worker_adapter"
                    else "sidera.workspace-provider.v1",
                    1,
                    capabilities,
                    ModuleLifecycle.ACTIVE,
                )
                instance = ModuleInstance(
                    uuid4(),
                    self.workspace_id,
                    definition.id,
                    f"native-pipeline-{provider}",
                    1,
                    "workspace" if module_type == "workspace_provider" else "adapter",
                    config,
                    (),
                    ModuleLifecycle.ACTIVE,
                )
                uow.modules.add_definition(definition)
                uow.modules.add_instance(instance)
                instances.append(instance)
            worker = Worker.register(
                worker_id=uuid4(),
                workspace_id=self.workspace_id,
                adapter_type="local_verification",
                display_name="Deterministic native result verification",
                capabilities=frozenset({"verification"}),
                capacity=1,
                cost_per_hour_microunits=0,
                startup_latency_ms=0,
                now=self.cp.now,
            )
            uow.workers.add(worker)
            uow.worker_runtime_configurations.add(
                WorkerRuntimeConfiguration.create(
                    worker_id=worker.id,
                    workspace_id=self.workspace_id,
                    adapter_module_instance_id=instances[0].id,
                    adapter_module_config_version=1,
                    workspace_module_instance_id=instances[1].id,
                    workspace_module_config_version=1,
                )
            )
        original = result_input_scenario._proposal_mapping

        def pipeline_proposal(**kwargs):
            raw = original(**kwargs)
            subtask = raw["worker_items"][0]["subtask_key"]
            for item in raw["worker_items"]:
                item["subtask_key"] = subtask
            raw["subtasks"] = [
                item for item in raw["subtasks"] if item["key"] == subtask
            ]
            for milestone in raw["milestones"]:
                milestone["subtask_keys"] = [
                    key for key in milestone["subtask_keys"] if key == subtask
                ]
            raw["milestones"] = [
                item for item in raw["milestones"] if item["subtask_keys"]
            ]
            for intent in raw["execution_intents"]:
                key = intent["worker_item_key"]
                upstream = (
                    ("a",)
                    if key in {"b", "c"}
                    else ("b", "c")
                    if key == "summary"
                    else ()
                )
                intent["activation_dependencies"] = [
                    {
                        "upstream_worker_item_key": source,
                        "downstream_worker_item_key": key,
                        "kind": "hard",
                        "required_artifact_refs": ["artifact_type:build"],
                    }
                    for source in upstream
                ]
            spec = raw["integration_spec"]
            spec["repository_ref"] = opaque_repository_ref(self.repository)
            spec["input_group_keys"] = ["research"]
            spec["criteria"][0]["text"] = (
                "The physically integrated function returns the contract value."
            )
            spec["checks"][0]["argv"] = [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                "test_native_value",
                "-v",
            ]
            return raw

        groups = tuple(
            {
                "group_key": f"contracts-for-{consumer}",
                "title": "Contracts",
                "consumer_worker_item_key": consumer,
                "candidates": [{"worker_item_key": "a", "priority": 1}],
                "requirements": {
                    "execution_status": "succeeded",
                    "required_artifact_refs": ["artifact_type:build"],
                    "required_evidence_refs": ["acceptance:build"],
                    "evidence_status": "candidate",
                },
                "mode": "at_least",
                "minimum_count": 1,
                "selection_strategy": "plan_order",
            }
            for consumer in ("b", "c")
        ) + (
            {
                "group_key": "research",
                "title": "Code and tests",
                "consumer_worker_item_key": "summary",
                "candidates": [
                    {"worker_item_key": "b", "priority": 1},
                    {"worker_item_key": "c", "priority": 2},
                ],
                "requirements": {
                    "execution_status": "succeeded",
                    "required_artifact_refs": ["artifact_type:build"],
                    "required_evidence_refs": ["acceptance:build"],
                    "evidence_status": "candidate",
                },
                "mode": "at_least",
                "minimum_count": 2,
                "selection_strategy": "plan_order",
            },
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(result_input_scenario, "_proposal_mapping", pipeline_proposal)
            self.cp.confirm_plan(
                proposal_schema_version=6,
                groups=groups,
                source_worker_keys=("a", "b", "c", "manual"),
                verification_module_refs=(instances[0].id, instances[1].id),
                integration_base_commit=self.revision,
            )
        self.cp.reconcile()
        self.cp.activate()
        with self.uow() as uow:
            plan = uow.plans.get_current_for_root(
                self.cp.ids["root_task"], self.workspace_id
            )
        checks = {check.key: check for check in plan.integration_spec.checks}
        self.verification_runner = LocalCheckRunner(
            state_root=tmp_path / "verification-state",
            approved_environment={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "en_US.UTF-8",
            },
            clock=self.cp.clock,
        )
        self.verification_adapter = LocalVerificationAdapter(
            runner=self.verification_runner,
            check_specs=checks,
            workspace_locator=self.manager.physical_directory,
            adapter_module_id=instances[0].id,
            clock=self.cp.clock,
        )

    def register_physical_build(self, key, filename, *, evidence):
        import hashlib
        from pathlib import Path

        from sidera_control_plane.application.event_ingestor import ArtifactRegistry
        from sidera_control_plane.domain.artifacts import (
            ArtifactCandidate,
            ArtifactType,
        )

        link = self.link(key)
        request = next(
            item
            for item in self.execution.starts
            if item.operation_id == link.start_operation_id
        )
        body = (Path(request.workspace.directory) / filename).read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        storage = self.cp.artifact_store.put(
            self.workspace_id, link.execution_id, digest, body
        )
        provenance = {
            "producer": "native-physical-fixture",
            "environment_ref": "env://native-physical-fixture",
        }
        if evidence:
            provenance.update(
                acceptance_criterion_ref="build",
                conclusion="Physical source captured by the CP Git boundary.",
            )
        with self.uow() as uow:
            execution = uow.executions.get(link.execution_id, self.workspace_id)
            session = uow.sessions.get_active_for_execution(execution.id)
            assert execution.status.value == "succeeded" and session is None
        return ArtifactRegistry(self.uow, clock=self.cp.clock).register(
            workspace_id=self.workspace_id,
            task_id=execution.task_id,
            execution_id=execution.id,
            session_id=None,
            candidates=(
                ArtifactCandidate(
                    ArtifactType.BUILD,
                    storage,
                    digest,
                    len(body),
                    "text/plain",
                    provenance,
                ),
            ),
            acceptance_criteria=("build",),
            producer="native-physical-fixture",
            environment_ref="env://native-physical-fixture",
        )

    def prepare_integration_inputs(self):
        from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
        from sidera_control_plane.application.context_requests import (
            build_execution_context_assembly_request,
        )
        from sidera_control_plane.application.result_input_evaluation import (
            build_code_input_manifest,
        )
        from sidera_control_plane.domain.context_packages import ResultInputBudgetV1

        with self.uow() as uow:
            execution = uow.executions.get(
                self.cp._execution_id("summary"), self.workspace_id
            )
            request = uow.execution_requests.get_by_execution_lineage(
                execution.lineage_id, self.workspace_id
            )
            selection = uow.input_selections.get(
                self.workspace_id, request.input_selection_id
            )
            item = uow.worker_items.get(execution.worker_item_id, self.workspace_id)
            context = uow.work_contexts.get_by_planning_request(
                request.planning_request_id, self.workspace_id
            )
            revision = uow.work_contexts.get_revision(
                context.current_revision_id, self.workspace_id
            )
            snapshots = tuple(
                snapshot
                for snapshot in uow.results.retained_code_results(self.workspace_id)
                if snapshot.execution_id
                in {self.cp._execution_id("b"), self.cp._execution_id("c")}
            )
            inputs = build_code_input_manifest(
                selection=selection,
                snapshots=snapshots,
                request_id=request.id,
                execution_id=execution.id,
                repository_ref=opaque_repository_ref(self.repository),
                base_commit=self.revision,
            )
            assembly = build_execution_context_assembly_request(
                execution=execution,
                execution_request=request,
                worker_item=item,
                work_context_revision=revision,
                ttl_seconds=3600,
                created_at=self.cp.now,
                manifest_schema_version=3,
                input_selection=selection,
                result_input_budget=ResultInputBudgetV1(),
                code_input_manifest=inputs,
            )
            uow.context_assemblies.add(assembly)
        return inputs

    def assign(self, key="a", *, human=False, profile=None):
        with self.uow() as uow:
            item = uow.worker_items.get(self.cp.worker_item_ids[key], self.workspace_id)
        self.last_assignment_command = self.command(
            "SetWorkerItemAssignee",
            "worker_item",
            item.id,
            item.version,
            {
                "assignee_kind": "human" if human else "profile",
                "assignee_id": self.principal
                if human
                else str((profile or self.profile).ref.profile_id),
                "profile_revision": None if human else 1,
            },
        )
        return self.commands.handle(self.last_assignment_command)

    def schedule(self, key="a"):
        self.cp.run_context_worker(key)
        return self.scheduler.run_once(
            1,
            workspace_id=self.workspace_id,
            target_execution_id=self.cp._execution_id(key),
        )

    def start(self, key="a", *, execution_id=None):
        from sqlalchemy import select

        from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
        from sidera_control_plane.application.worker_gateway import ASSIGNMENT_TOPIC
        from sidera_control_plane.persistence.models import OutboxMessageModel

        execution_id = execution_id or self.cp._execution_id(key)
        with self.uow() as uow:
            execution = uow.executions.get(execution_id, self.workspace_id)
            rows = tuple(
                uow.session.scalars(
                    select(OutboxMessageModel).where(
                        OutboxMessageModel.topic == ASSIGNMENT_TOPIC,
                    )
                )
            )
            row = next(
                row
                for row in rows
                if row.payload["workspace_id"] == str(self.workspace_id)
                and row.payload["aggregate_id"] == str(execution_id)
                and row.payload["data"]["lease_id"] == str(execution.active_lease_id)
            )
            message = OutboxMessage(
                row.id,
                row.event_id,
                row.topic,
                dict(row.payload),
                max(row.attempts, 1),
                uuid4(),
            )
        return self.gateway.handle(message)

    def link(self, key="a"):
        with self.uow() as uow:
            return uow.execution_authorities.get_link(
                self.workspace_id, self.cp._execution_id(key)
            )


class PublicNativeExecution:
    def __init__(self, scenario):
        self.s = scenario
        self.starts, self.reads, self.receipts, self.events = [], [], {}, {}
        self.next_status = "accepted"
        self.states = {}
        self.before_start = None

    def start(self, request):
        from sidera_agents.contracts import RunEvent

        if self.before_start is not None:
            self.before_start(request)
        self.starts.append(request)
        status = self.next_status
        run_id = uuid4() if status == "accepted" else None
        receipt = StartReceipt(
            operation_id=request.operation_id,
            status=status,
            run_id=run_id,
            reason_code="runtime_capacity" if status == "busy" else None,
        )
        self.receipts[request.operation_id] = receipt
        if run_id is not None:
            self.events[run_id] = [
                RunEvent(
                    run_id=run_id,
                    sequence=1,
                    kind="run.started",
                    occurred_at=self.s.cp.now,
                    operation_id=request.operation_id,
                    response_id=None,
                    data={},
                )
            ]
            self.states[run_id] = "running"
        return receipt

    def get_start(self, workspace_id, operation_id, *, grant_id):
        from sidera_agents.contracts import StartLookup

        self.reads.append(("start", workspace_id, operation_id, grant_id))
        receipt = self.receipts.get(operation_id)
        return StartLookup(found=receipt is not None, receipt=receipt)

    def list_events(self, workspace_id, run_id, after_sequence, limit=100, *, grant_id):
        from sidera_agents.contracts import EventPage

        self.reads.append(("events", workspace_id, run_id, grant_id))
        events = tuple(
            event for event in self.events[run_id] if event.sequence > after_sequence
        )[:limit]
        return EventPage(
            events=events,
            next_sequence=events[-1].sequence if events else after_sequence,
            has_more=False,
        )

    def get_run(self, workspace_id, run_id, *, grant_id):
        from sidera_agents.contracts import EffectiveConfig, RunSnapshot

        self.reads.append(("run", workspace_id, run_id, grant_id))
        request = next(
            request
            for request in self.starts
            if self.receipts[request.operation_id].run_id == run_id
        )
        return RunSnapshot(
            run_id=run_id,
            workspace_id=workspace_id,
            profile_ref=request.profile_ref,
            policy=request.policy,
            state=self.states[run_id],
            input_open=self.states[run_id] == "running",
            active_response_id=None,
            effective_config=EffectiveConfig(
                profile_ref=request.profile_ref,
                config_hash="fixture",
                runtime_ref=self.s.profile.draft.runtime_ref,
                actual_runtime_version="1",
                selected_model=None,
                observed_model=None,
                inherited_fields=(),
                applied_fields=(),
                resource_refs=(),
                context_receipts=(),
            ),
            last_event_sequence=len(self.events[run_id]),
            capabilities=self.s.runtimes.runtime.capabilities,
            reason_code=None,
        )

    def send_input(self, request):
        pytest.fail("native initial objective was sent twice")

    def emit(self, run_id, kind, *, state=None):
        from sidera_agents.contracts import RunEvent

        if state is not None:
            self.states[run_id] = state
        self.events[run_id].append(
            RunEvent(
                run_id=run_id,
                sequence=len(self.events[run_id]) + 1,
                kind=kind,
                occurred_at=self.s.cp.now,
                operation_id=None,
                response_id=uuid4() if kind.startswith("response.") else None,
                data={},
            )
        )


def test_actual_scheduler_gateway_busy_retry_has_one_initial_native_input(
    native_scenario,
):
    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    waiting = s.start()
    busy = s.link()
    assert waiting.status == "waiting_capacity"
    assert busy.not_admitted
    with s.uow() as uow:
        execution = uow.executions.get(busy.execution_id, s.workspace_id)
        assert execution.status.value == "leased"
        assert uow.sessions.get_active_for_execution(execution.id) is None
        assert uow.workers.get(s.worker_id, s.workspace_id).current_load == 1
    s.execution.next_status = "accepted"
    s.cp.now += timedelta(seconds=20)
    assert s.gateway.sync_active_sessions(s.workspace_id) == 1
    admitted = s.link()
    assert admitted.run_id is not None
    assert admitted.start_operation_id == busy.start_operation_id
    assert admitted.authority_id == busy.authority_id
    assert admitted.request_sha256 == busy.request_sha256
    assert s.execution.starts[0] == s.execution.starts[1]
    assert "PRIVATE PROFILE PROMPT" not in str(s.execution.starts[1])
    assert_native_event_causes(s, admitted)
    s.gateway.sync_active_sessions(s.workspace_id)
    assert_native_event_causes(s, admitted)


def test_worker_gateway_persists_original_host_grant_without_recursive_cp_permission(
    native_scenario,
):
    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "accepted"
    s.start()
    link = s.link()
    with s.uow() as uow:
        original = uow.execution_authorities.get(link.authority_id)
    assert original.upstream_grant_id == s.setup_grant != original.id
    assert s.execution.starts[0].grant_id == original.id
    assert s.worker_scope_calls and {grant for grant, _, _ in s.worker_scope_calls} == {
        s.setup_grant
    }
    reader = ExecutionAuthorityReader(
        s.uow,
        principal=lambda: s.principal,
        authorizer=s.services.authorizer,
        clock=s.cp.clock,
    )
    reader.require(original.id, "workspace.execute", original.execution_workspace)
    reader.require(original.id, "source.read", original.sources[0])
    with s.uow() as uow:
        assert uow.execution_authorities.get(original.id) == original
        assert (
            uow.execution_authorities.get_link(s.workspace_id, link.execution_id)
            == link
        )


def test_real_vocabulary_runtime_reaches_actual_assignment_scheduler_and_native_start(
    native_scenario,
):
    from hashlib import sha256

    from sidera_control_plane.application.agent_sources import canonical_source_text
    from sidera_control_plane.application.context_requests import (
        resolve_execution_context_authorization,
    )

    s = native_scenario
    assert "code" not in s.runtimes.runtime.capabilities
    assigned = s.assign()
    assert assigned.data["binding"]["runnable"] is True
    assert s.schedule().assigned == 1
    s.start()
    (request,) = s.execution.starts
    assert request.required_capabilities == ()
    with s.uow() as uow:
        execution = uow.executions.get(s.link().execution_id, s.workspace_id)
        authorization = resolve_execution_context_authorization(uow, execution)
        plan = uow.plans.get(authorization.request.plan_snapshot_id, s.workspace_id)
        role = next(
            item
            for item in plan.worker_item_plans
            if item.worker_item_id == execution.worker_item_id
        )
        assert execution.capability_requirements == frozenset({"code"})
        assert role.capability_requirements == ("code",)
    facts = next(item for item in request.context if item.item_id == "facts")
    body = s.gateway._native_source_reader.read(request.grant_id, facts.source_ref)
    manifest = s.cp.context_package_for_execution(execution.id)
    assert body.text == canonical_source_text(manifest.control_plane_fact_snapshot)
    assert (
        body.content_hash
        == facts.content_hash
        == "sha256:" + sha256(body.text.encode()).hexdigest()
    )


def test_busy_new_profile_reuses_directory_under_new_fence(native_scenario):
    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    s.start()
    old = s.link()
    old_message = s.cp._assignment_message(old.execution_id)
    with s.uow() as uow:
        manifest = uow.workspaces.get_for_execution(old.execution_id, s.workspace_id)
    directory = s.manager.physical_directory(manifest)
    profile = s.profile.model_copy(
        update={"ref": s.profile.ref.model_copy(update={"profile_id": uuid4()})}
    )
    s.catalog.profiles[profile.ref] = profile
    s.assign(profile=profile)
    with s.uow() as uow:
        assert uow.workers.get(s.worker_id, s.workspace_id).current_load == 0
        assert uow.bindings.get_for_execution(old.execution_id, s.workspace_id) is None
        assert (
            uow.executions.get(old.execution_id, s.workspace_id).not_before == s.cp.now
        )
        assert (
            uow.scheduling_blocks.get_active_for_execution(
                old.execution_id, s.workspace_id
            )
            is None
        )
    assert s.schedule().assigned == 1
    s.execution.next_status = "accepted"
    s.start()
    new = s.link()
    assert new.profile_ref == profile.ref
    assert new.start_operation_id != old.start_operation_id
    with s.uow() as uow:
        execution = uow.executions.get(old.execution_id, s.workspace_id)
        current = uow.workspaces.get_for_execution(old.execution_id, s.workspace_id)
        assert execution.attempt == 1 and execution.last_fencing_token == 2
        assert uow.workers.get(s.worker_id, s.workspace_id).current_load == 1
        assert current.fencing_token == 2
    assert s.manager.physical_directory(current) == directory
    starts = len(s.execution.starts)
    from sidera_control_plane.application.worker_gateway import (
        GatewayObsoleteAssignmentError,
    )

    with pytest.raises(GatewayObsoleteAssignmentError):
        s.gateway.handle(old_message)
    assert len(s.execution.starts) == starts
    assert_native_event_causes(s, old)
    assert_native_event_causes(s, new)


def test_native_preparation_uses_one_command_event_not_legacy_materialization(
    native_scenario,
):
    from sqlalchemy import func, select

    from sidera_control_plane.application.agent_worker_adapter import (
        NativeContextPreparationReceipt,
    )
    from sidera_control_plane.persistence.models import (
        ContextPackageMaterializationAuditModel,
        EventModel,
    )

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    execution_id = s.cp._execution_id("a")
    with s.uow() as uow:
        assigned = uow.session.scalar(
            select(EventModel).where(
                EventModel.aggregate_type == "execution",
                EventModel.aggregate_id == execution_id,
                EventModel.event_type == "ExecutionLeased",
            )
        )
        assigned_snapshot = (
            assigned.id,
            assigned.aggregate_version,
            dict(assigned.payload),
            assigned.causation_id,
        )
    s.execution.next_status = "busy"
    s.start()
    link = s.link()
    receipt = NativeContextPreparationReceipt.model_validate(link.preparation)
    with s.uow() as uow:
        command = uow.commands.find_by_id(s.workspace_id, receipt.command_id)
        assert command.result["data"]["preparation"] == link.preparation
        package = uow.context_packages.get(receipt.package_id, s.workspace_id)
        assert package.materialized_at is None
        assert (
            uow.session.scalar(
                select(func.count())
                .select_from(ContextPackageMaterializationAuditModel)
                .where(
                    ContextPackageMaterializationAuditModel.package_id
                    == package.package_id
                )
            )
            == 0
        )
        assert (
            uow.session.scalar(
                select(func.count())
                .select_from(EventModel)
                .where(
                    EventModel.workspace_id == s.workspace_id,
                    EventModel.event_type == "ContextSourcesPrepared",
                )
            )
            == 1
        )
        event = uow.session.scalar(
            select(EventModel).where(
                EventModel.workspace_id == s.workspace_id,
                EventModel.event_type == "ContextSourcesPrepared",
            )
        )
        assert (event.aggregate_type, event.aggregate_id, event.aggregate_version) == (
            "context_sources_preparation",
            receipt.command_id,
            1,
        )
        assert event.payload["preparation"]["execution_id"] == str(execution_id)
        assert event.causation_id == receipt.command_id
        assigned = uow.session.get(EventModel, assigned_snapshot[0])
        assert (
            assigned.id,
            assigned.aggregate_version,
            dict(assigned.payload),
            assigned.causation_id,
        ) == assigned_snapshot
        assert (
            uow.worker_items.get(
                s.cp.worker_item_ids["a"], s.workspace_id
            ).acknowledged_context_version
            == receipt.context_version
        )
    s.cp.now += timedelta(seconds=20)
    s.gateway.sync_active_sessions(s.workspace_id)
    assert s.link().preparation == link.preparation
    with s.uow() as uow:
        events = tuple(
            uow.session.scalars(
                select(EventModel).where(
                    EventModel.workspace_id == s.workspace_id,
                    EventModel.event_type == "ContextSourcesPrepared",
                )
            )
        )
        assert len(events) == 1 and events[0].id == event.id
    assert_native_event_causes(s, s.link())


def assert_native_event_causes(s, link):
    from uuid import NAMESPACE_URL, uuid5

    from sqlalchemy import select

    from sidera_control_plane.persistence.models import CommandModel, EventModel

    with s.uow() as uow:
        events = tuple(
            uow.session.scalars(
                select(EventModel).where(
                    EventModel.workspace_id == s.workspace_id,
                    EventModel.correlation_id == link.start_operation_id,
                    EventModel.event_type.in_(
                        (
                            "ExecutionSchedulingBlocked",
                            "ExecutionSchedulingUnblocked",
                            "ExecutionStarted",
                            "ExecutionSessionStarted",
                        )
                    ),
                )
            )
        )
        assert events
        for event in events:
            command = uow.session.get(CommandModel, event.causation_id)
            assert command is not None and command.workspace_id == s.workspace_id
            assert command.command_type in (
                "ObserveNativeCapacity",
                "ObserveNativeStarted",
            )
            assert command.payload["execution_id"] == str(link.execution_id)
            assert command.payload["start_operation_id"] == str(link.start_operation_id)
            assert command.result["data"]["start_operation_id"] == str(
                link.start_operation_id
            )
            assert str(event.id) in command.result["event_ids"]
            if command.command_type == "ObserveNativeStarted":
                assert command.id == uuid5(
                    NAMESPACE_URL,
                    f"sidera.native.observe-started:{link.start_operation_id}",
                )
                assert command.payload["run_id"] == str(link.run_id)
                assert command.payload["lease_id"] == str(
                    uow.execution_authorities.get(link.authority_id).lease_id
                )
        started = tuple(
            uow.session.scalars(
                select(CommandModel).where(
                    CommandModel.workspace_id == s.workspace_id,
                    CommandModel.command_type == "ObserveNativeStarted",
                    CommandModel.correlation_id == link.start_operation_id,
                )
            )
        )
        assert len(started) == (1 if link.run_id is not None else 0)


def test_capacity_command_event_failure_rolls_back_backoff_not_busy_fact(
    native_scenario, monkeypatch
):
    from sqlalchemy import func, select

    from sidera_control_plane.persistence.models import CommandModel, EventModel
    from sidera_control_plane.persistence.repositories import EventRepository

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    with s.uow() as uow:
        before = uow.executions.get(s.cp._execution_id("a"), s.workspace_id).not_before
    append = EventRepository.append_with_outbox

    def fail_capacity_event(repository, event):
        if event.event_type == "ExecutionSchedulingBlocked":
            raise RuntimeError("capacity event transaction failure")
        append(repository, event)

    s.execution.next_status = "busy"
    with monkeypatch.context() as patch:
        patch.setattr(EventRepository, "append_with_outbox", fail_capacity_event)
        with pytest.raises(RuntimeError, match="capacity event transaction failure"):
            s.start()
    busy = s.link()
    assert busy.not_admitted
    with s.uow() as uow:
        execution = uow.executions.get(busy.execution_id, s.workspace_id)
        assert execution.not_before == before
        assert (
            uow.scheduling_blocks.get_active_for_execution(execution.id, s.workspace_id)
            is None
        )
        for model, predicate in (
            (CommandModel, CommandModel.command_type == "ObserveNativeCapacity"),
            (EventModel, EventModel.event_type == "ExecutionSchedulingBlocked"),
        ):
            assert (
                uow.session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == s.workspace_id, predicate)
                )
                == 0
            )
    assert s.gateway.sync_active_sessions(s.workspace_id) == 1
    assert s.link().start_operation_id == busy.start_operation_id
    assert s.execution.starts[0] == s.execution.starts[1]
    assert_native_event_causes(s, s.link())


def test_unknown_start_is_query_only_without_a_fake_session(native_scenario):
    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "unknown"
    assert s.start().status == "unknown"
    original = s.link()
    s.cp.now += timedelta(seconds=20)
    s.gateway.sync_active_sessions(s.workspace_id)
    assert len(s.execution.starts) == 1
    assert any(
        read[0] == "start" and read[2] == original.start_operation_id
        for read in s.execution.reads
    )
    with s.uow() as uow:
        assert uow.sessions.get_active_for_execution(original.execution_id) is None
        assert (
            uow.executions.get(original.execution_id, s.workspace_id).status.value
            == "leased"
        )


def test_no_confirmation_and_human_assignment_make_zero_native_calls(native_scenario):
    s = native_scenario
    assert s.schedule().assigned == 0
    s.assign(human=True)
    assert s.schedule().assigned == 0
    assert s.execution.starts == []
    assert s.catalog.calls == []
    assert s.runtimes.calls == []


def test_required_context_revocation_denies_before_native_start(native_scenario):
    from sidera_control_plane.application.worker_gateway import GatewayMessageError

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    package = s.cp.context_package_for_execution(s.cp._execution_id("a"))
    s.cp.revoke_package(package.package_id)
    with pytest.raises(GatewayMessageError) as denied:
        s.start()
    assert denied.value.code == "context_package_revoked"
    assert s.execution.starts == []


def test_accepted_run_with_lost_cp_receipt_queries_original_without_restart(
    native_scenario, monkeypatch
):
    from sidera_control_plane.persistence.execution_authority_repository import (
        ExecutionAuthorityRepository,
    )

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    with monkeypatch.context() as patch:
        patch.setattr(
            ExecutionAuthorityRepository,
            "record_start",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("lost CP receipt")
            ),
        )
        assert s.start().status == "starting"
    original = s.link()
    assert original.run_id is None
    assert len(s.execution.starts) == 1
    s.gateway.sync_active_sessions(s.workspace_id)
    assert len(s.execution.starts) == 1
    assert s.link().run_id == s.execution.receipts[original.start_operation_id].run_id
    assert any(
        read[0] == "start" and read[2] == original.start_operation_id
        for read in s.execution.reads
    )


def test_busy_to_human_replay_releases_one_real_worker_slot(native_scenario):
    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    s.start()
    old = s.link()
    first = s.assign(human=True)
    replay = s.commands.handle(s.last_assignment_command)
    assert replay == first
    with s.uow() as uow:
        assert uow.workers.get(s.worker_id, s.workspace_id).current_load == 0
        assert uow.bindings.get_for_execution(old.execution_id, s.workspace_id) is None
        assert (
            len(
                uow.execution_authorities.admission_history(
                    s.workspace_id, old.execution_id
                )
            )
            == 1
        )
        assert (
            uow.executions.get(old.execution_id, s.workspace_id).not_before == s.cp.now
        )
        assert (
            uow.scheduling_blocks.get_active_for_execution(
                old.execution_id, s.workspace_id
            )
            is None
        )
    assert s.schedule().assigned == 0
    assert len(s.execution.starts) == 1


def test_busy_retirement_rollback_preserves_slot_and_current_binding(
    native_scenario, monkeypatch
):
    from sidera_control_plane.persistence.agent_binding_repository import (
        AgentBindingRepository,
    )

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    s.start()
    old = s.link()
    with s.uow() as uow:
        old_binding = uow.bindings.get_for_execution(old.execution_id, s.workspace_id)
        old_delay = uow.executions.get(old.execution_id, s.workspace_id).not_before
        old_block = uow.scheduling_blocks.get_active_for_execution(
            old.execution_id, s.workspace_id
        )
    with monkeypatch.context() as patch:
        patch.setattr(
            AgentBindingRepository,
            "append",
            lambda *args: (_ for _ in ()).throw(RuntimeError("assignment rollback")),
        )
        with pytest.raises(RuntimeError, match="assignment rollback"):
            s.assign(human=True)
    with s.uow() as uow:
        assert uow.workers.get(s.worker_id, s.workspace_id).current_load == 1
        assert (
            uow.bindings.get_for_execution(old.execution_id, s.workspace_id)
            == old_binding
        )
        assert (
            uow.execution_authorities.get_link(s.workspace_id, old.execution_id) == old
        )
        assert (
            uow.execution_authorities.admission_history(
                s.workspace_id, old.execution_id
            )
            == ()
        )
        assert (
            uow.executions.get(old.execution_id, s.workspace_id).not_before == old_delay
        )
        assert (
            uow.scheduling_blocks.get_active_for_execution(
                old.execution_id, s.workspace_id
            )
            == old_block
        )


def test_revoked_busy_authority_cannot_issue_another_start(native_scenario):
    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    s.start()
    old = s.link()
    with s.uow() as uow:
        uow.execution_authorities.revoke(old.authority_id, s.workspace_id)
    s.cp.now += timedelta(seconds=20)
    assert s.gateway.sync_active_sessions(s.workspace_id) == 0
    assert len(s.execution.starts) == 1


def test_response_completion_and_unconfirmed_close_retain_resources(native_scenario):
    from sidera_control_plane.application.agent_worker_adapter import (
        native_operation_id,
    )

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.start()
    link = s.link()
    s.execution.emit(link.run_id, "response.completed")
    s.execution.emit(link.run_id, "run.closing", state="closing")
    s.gateway.sync_active_sessions(s.workspace_id)
    s.gateway.sync_active_sessions(s.workspace_id)
    with s.uow() as uow:
        execution = uow.executions.get(link.execution_id, s.workspace_id)
        session = uow.sessions.get_active_for_execution(execution.id)
        assert execution.status.value == "running"
        assert session.last_sequence == 3
        assert uow.workers.get(s.worker_id, s.workspace_id).current_load == 1
        assert (
            uow.leases.get(execution.active_lease_id, s.workspace_id).status.value
            == "active"
        )
        assert (
            uow.results.get_code_result_by_capture_operation(
                native_operation_id(execution.id, link.binding_version, "code-capture"),
                s.workspace_id,
            )
            is None
        )
        assert (
            len(uow.execution_events.list_for_session(session.id, s.workspace_id)) == 3
        )


def test_profile_reference_authority_is_exact_scoped_and_revision_pinned(
    native_scenario,
):
    from sidera_agents.contracts import (
        CapabilityResourceDraft,
        CredentialRef,
        ResourceRef,
        ResourceRevision,
        SecretBinding,
        ToolBinding,
    )
    from sidera_agents.errors import AgentError

    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )

    s = native_scenario
    credentials = tuple(
        CredentialRef(workspace_id=s.workspace_id, credential_id=uuid4())
        for _ in range(2)
    )
    refs = tuple(
        ResourceRef(workspace_id=s.workspace_id, resource_id=uuid4(), revision=1)
        for _ in range(2)
    )
    declared = s.profile.model_copy(
        update={
            "draft": s.profile.draft.model_copy(
                update={
                    "resource_refs": refs,
                    "secret_refs": (
                        SecretBinding(
                            credential_ref=credentials[0], destination="provider"
                        ),
                    ),
                }
            )
        }
    )
    s.catalog.profiles[declared.ref] = declared
    for ref, kind in zip(refs, ("skill", "mcp"), strict=True):
        s.catalog.resources[ref] = ResourceRevision(
            ref=ref,
            draft=CapabilityResourceDraft(
                name=kind,
                kind=kind,
                settings={"private_fixture_settings": "must not copy"},
                secret_refs=(
                    SecretBinding(credential_ref=credentials[1], destination="mcp"),
                )
                if kind == "mcp"
                else (),
            ),
            content_hash="fixture",
            created_at=s.cp.now,
        )
    s.assign(profile=declared)
    # A later public head cannot mutate the confirmed revision.
    edited = declared.model_copy(
        update={
            "ref": declared.ref.model_copy(update={"revision": 2}),
            "draft": s.profile.draft,
        }
    )
    s.catalog.profiles[edited.ref] = edited
    assert s.schedule().assigned == 1
    reader = ExecutionAuthorityReader(
        s.uow,
        principal=lambda: s.principal,
        authorizer=s.services.authorizer,
        clock=s.cp.clock,
    )
    checks = []

    def require_declared_references(request):
        for credential in credentials:
            reader.require(request.grant_id, "secret.read", credential)
            checks.append(credential)
        for resource in refs:
            reader.require(request.grant_id, "resource.read", resource)
            checks.append(resource)
        tool = ToolBinding(resource_ref=refs[1])
        reader.require(request.grant_id, "tool.execute", tool)
        checks.append(tool)
        for action, target in (
            (
                "secret.read",
                CredentialRef(workspace_id=s.workspace_id, credential_id=uuid4()),
            ),
            ("resource.read", refs[0].model_copy(update={"revision": 2})),
            ("resource.read", refs[0].model_copy(update={"workspace_id": uuid4()})),
            ("tool.execute", ToolBinding(resource_ref=refs[0])),
            ("tool.execute", ToolBinding(connection_id=uuid4())),
        ):
            with pytest.raises(AgentError):
                reader.require(request.grant_id, action, target)

    s.execution.before_start = require_declared_references
    s.execution.next_status = "busy"
    s.start()
    link = s.link()
    assert checks == list(credentials) + list(refs) + [
        ToolBinding(resource_ref=refs[1])
    ]
    assert s.execution.starts[0].tools == (ToolBinding(resource_ref=refs[1]),)
    with s.uow() as uow:
        authority = uow.execution_authorities.get(link.authority_id)
        assert authority.credentials == credentials and authority.resources == refs
        assert {"secret.read", "resource.read", "tool.execute"} <= set(
            authority.actions
        )
        assert "private_fixture_settings" not in repr(authority)
    s.denied_worker_actions.add("tool.execute")
    with pytest.raises(AgentError):
        reader.require(link.authority_id, "tool.execute", authority.tools[0])
    s.denied_worker_actions.clear()
    s.cp.now += timedelta(seconds=301)
    with pytest.raises(AgentError):
        reader.require(link.authority_id, "secret.read", credentials[0])
    with s.uow() as uow:
        uow.execution_authorities.revoke(link.authority_id, s.workspace_id)
    with pytest.raises(AgentError):
        reader.require(link.authority_id, "resource.read", refs[0])
    assert len(s.execution.starts) == 1


def test_native_preparation_command_hash_is_verified_before_busy_retry(native_scenario):
    from sidera_control_plane.persistence.models import CommandModel

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    s.start()
    link = s.link()
    with s.uow() as uow:
        uow.session.get(CommandModel, link.preparation["command_id"]).request_hash = (
            "f" * 64
        )
    s.cp.now += timedelta(seconds=20)
    assert s.gateway.sync_active_sessions(s.workspace_id) == 0
    assert len(s.execution.starts) == 1
    assert s.link().not_admitted


def test_native_preparation_event_failure_has_no_half_ack_or_receipt(
    native_scenario, monkeypatch
):
    from sqlalchemy import func, select

    from sidera_control_plane.persistence.models import CommandModel, EventModel
    from sidera_control_plane.persistence.repositories import EventRepository

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    with s.uow() as uow:
        item = uow.worker_items.get(s.cp.worker_item_ids["a"], s.workspace_id)
        before = (item.version, item.acknowledged_context_version)
    append = EventRepository.append_with_outbox

    def fail_preparation_event(repository, event):
        if event.event_type == "ContextSourcesPrepared":
            raise RuntimeError("preparation event transaction failure")
        append(repository, event)

    with monkeypatch.context() as patch:
        patch.setattr(EventRepository, "append_with_outbox", fail_preparation_event)
        with pytest.raises(RuntimeError, match="preparation event transaction failure"):
            s.start()
    assert s.execution.starts == []
    assert s.link().status == "pending" and s.link().preparation is None
    with s.uow() as uow:
        item = uow.worker_items.get(s.cp.worker_item_ids["a"], s.workspace_id)
        assert (item.version, item.acknowledged_context_version) == before
        for model, predicate in (
            (CommandModel, CommandModel.command_type == "PrepareNativeContextSources"),
            (EventModel, EventModel.event_type == "ContextSourcesPrepared"),
        ):
            assert (
                uow.session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == s.workspace_id, predicate)
                )
                == 0
            )
    s.start()
    assert len(s.execution.starts) == 1
    assert s.link().preparation is not None


def test_independent_same_profile_runs_keep_separate_cp_pins_and_slots(native_scenario):
    s = native_scenario
    with s.uow() as uow:
        worker = uow.workers.get(s.worker_id, s.workspace_id)
        previous = worker.version
        worker.capacity = 2
        worker.version += 1
        uow.workers.save(worker, previous)
    for key in ("b", "c"):
        s.assign(key)
        assert s.schedule(key).assigned == 1
        s.start(key)
    code, tests = s.link("b"), s.link("c")
    assert code.profile_ref == tests.profile_ref == s.profile.ref
    assert code.run_id != tests.run_id
    assert code.authority_id != tests.authority_id
    assert code.start_operation_id != tests.start_operation_id
    assert code.preparation["package_id"] != tests.preparation["package_id"]
    assert len(s.execution.starts) == 2
    assert (
        s.execution.starts[0].workspace.directory
        != s.execution.starts[1].workspace.directory
    )
    with s.uow() as uow:
        worker = uow.workers.get(s.worker_id, s.workspace_id)
        assert worker.capacity == worker.current_load == 2
    s.assign("a")
    assert s.schedule("a").assigned == 0
    assert len(s.execution.starts) == 2


def test_confirmed_run_close_captures_physical_code_not_a_completion_message(
    native_scenario,
):
    from pathlib import Path
    from uuid import UUID

    from sidera_control_plane.application.code_result_capture import (
        code_capture_operation_id,
    )

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.start()
    link = s.link()
    directory = Path(s.execution.starts[0].workspace.directory)
    (directory / "contract.py").write_text(
        "def native_contract():\n    return 12\n", encoding="utf-8"
    )
    s.execution.emit(link.run_id, "response.completed")
    s.gateway.sync_active_sessions(s.workspace_id)
    capture_id = UUID(link.preparation["capture_operation_id"])
    with s.uow() as uow:
        assert (
            uow.executions.get(link.execution_id, s.workspace_id).status.value
            == "running"
        )
        assert (
            uow.results.get_code_result_by_capture_operation(capture_id, s.workspace_id)
            is None
        )
    s.execution.emit(link.run_id, "run.completed", state="completed")
    s.gateway.sync_active_sessions(s.workspace_id)
    with s.uow() as uow:
        snapshot = uow.results.get_code_result_by_capture_operation(
            capture_id, s.workspace_id
        )
        assert snapshot is not None
        assert (
            snapshot.execution_id == link.execution_id
            and snapshot.capture_operation_id == capture_id
        )
        assert snapshot.input_commit == s.revision
        assert snapshot.result_tree != snapshot.input_tree
        assert (
            uow.results.get_code_result_by_capture_operation(
                code_capture_operation_id(link.execution_id), s.workspace_id
            )
            is None
        )
        execution = uow.executions.get(link.execution_id, s.workspace_id)
        assert (
            execution.status.value == "succeeded" and execution.active_lease_id is None
        )
        assert uow.workers.get(s.worker_id, s.workspace_id).current_load == 0
        head = uow.results.get_head(s.workspace_id, s.cp.ids["root_task"])
        assert head is None or head.current_acceptance_id is None


def test_actual_native_completion_receipt_replays_saved_version_without_duplicate_release(
    native_scenario, monkeypatch
):
    from pathlib import Path
    from uuid import NAMESPACE_URL, uuid5

    from sqlalchemy import select

    from sidera_control_plane.domain.gateway import RuntimeModuleOperation
    from sidera_control_plane.persistence.models import EventModel

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.start()
    link = s.link()
    with s.uow() as uow:
        authority = uow.execution_authorities.get(link.authority_id)
        session = uow.sessions.get_active_for_execution(link.execution_id)
    context = s.gateway._load_context_by_ids(
        workspace_id=s.workspace_id,
        execution_id=link.execution_id,
        lease_id=authority.lease_id,
        fencing_token=authority.fencing_token,
        operation=RuntimeModuleOperation.OBSERVE,
    )
    directory = Path(s.execution.starts[0].workspace.directory)
    (directory / "completion.py").write_text("VALUE = 16\n", encoding="utf-8")
    completion_envelopes = []
    handle = CommandService.handle

    def observe(service, envelope):
        if (
            envelope.command_type == "ReportExecutionSucceeded"
            and envelope.target_id == link.execution_id
        ):
            completion_envelopes.append(envelope)
        return handle(service, envelope)

    monkeypatch.setattr(CommandService, "handle", observe)
    s.execution.emit(link.run_id, "run.completed", state="completed")
    s.gateway.sync_active_sessions(s.workspace_id)
    decision = s.gateway._load_completion_decision(context, session_id=session.id)
    assert decision is not None and decision.outcome == "succeeded"
    command_id = uuid5(
        NAMESPACE_URL,
        f"sidera.execution-completion:{link.execution_id}:{session.id}:{decision.sequence}:{decision.policy_version}:{decision.outcome}",
    )
    with s.uow() as uow:
        saved = uow.commands.find_by_id(s.workspace_id, command_id)
        assert saved is not None and saved.status == "applied"
        assert len(completion_envelopes) == 1
        assert saved.expected_version == completion_envelopes[0].expected_version == 4
        completed = uow.executions.get(link.execution_id, s.workspace_id)
        assert (
            completed.status.value == "succeeded"
            and completed.version > saved.expected_version
        )
        rows = tuple(
            uow.session.scalars(
                select(EventModel).where(EventModel.workspace_id == s.workspace_id)
            )
        )
        event_ids = {event.id for event in rows}
        counts = {
            kind: sum(event.event_type == kind for event in rows)
            for kind in (
                "ExecutionSucceeded",
                "LeaseReleased",
                "WorkerCapacityReleased",
                "ExecutionSessionClosed",
            )
        }
        assert counts == {
            "ExecutionSucceeded": 1,
            "LeaseReleased": 1,
            "WorkerCapacityReleased": 1,
            "ExecutionSessionClosed": 1,
        }
    for _ in range(2):
        assert (
            s.gateway._report_execution_completion(
                context, session_id=session.id, decision=decision
            )
            is True
        )
    assert len(completion_envelopes) == 3
    assert all(envelope == completion_envelopes[0] for envelope in completion_envelopes)
    with s.uow() as uow:
        assert uow.commands.find_by_id(s.workspace_id, command_id) == saved
        assert uow.executions.get(link.execution_id, s.workspace_id) == completed
        assert {
            event.id
            for event in uow.session.scalars(
                select(EventModel).where(EventModel.workspace_id == s.workspace_id)
            )
        } == event_ids
        assert uow.workers.get(s.worker_id, s.workspace_id).current_load == 0
        assert (
            uow.leases.get(authority.lease_id, s.workspace_id).status.value
            == "released"
        )
        assert uow.sessions.get_any(session.id, s.workspace_id).status.value == "closed"
    assert len(s.execution.starts) == 1


def test_multi_target_busy_retirement_defers_all_events_until_command_save(
    native_scenario,
):
    from sqlalchemy import select

    from sidera_control_plane.persistence.models import EventModel

    s = native_scenario
    with s.uow() as uow:
        worker = uow.workers.get(s.worker_id, s.workspace_id)
        previous = worker.version
        worker.capacity = 2
        worker.version += 1
        uow.workers.save(worker, previous)
    s.execution.next_status = "busy"
    for key in ("a", "b"):
        s.assign(key)
        assert s.schedule(key).assigned == 1
        s.start(key)
    with s.uow() as uow:
        items = tuple(
            uow.worker_items.get(s.cp.worker_item_ids[key], s.workspace_id)
            for key in ("a", "b")
        )
    command = s.command(
        "UpdateUnstartedAgentBinding",
        "worker_item",
        items[0].id,
        items[0].version,
        {
            "assignee_kind": "human",
            "assignee_id": s.principal,
            "profile_revision": None,
            "targets": [
                {
                    "target_type": "worker_item",
                    "target_id": str(item.id),
                    "expected_version": item.version,
                }
                for item in items
            ],
        },
    )
    result = s.commands.handle(command)
    assert s.commands.handle(command) == result
    assert len(result.event_ids) == 4
    with s.uow() as uow:
        assert uow.workers.get(s.worker_id, s.workspace_id).current_load == 0
        for key in ("a", "b"):
            execution_id = s.cp._execution_id(key)
            assert (
                uow.execution_authorities.get_link(s.workspace_id, execution_id) is None
            )
            assert (
                len(
                    uow.execution_authorities.admission_history(
                        s.workspace_id, execution_id
                    )
                )
                == 1
            )
            assert (
                uow.scheduling_blocks.get_active_for_execution(
                    execution_id, s.workspace_id
                )
                is None
            )
        events = tuple(
            uow.session.scalars(
                select(EventModel).where(EventModel.id.in_(result.event_ids))
            )
        )
        assert len(events) == 4
        assert all(event.causation_id == command.id for event in events)
        assert uow.commands.find_by_id(s.workspace_id, command.id).result[
            "event_ids"
        ] == [str(event_id) for event_id in result.event_ids]
    assert s.schedule("a").assigned == s.schedule("b").assigned == 0
    assert len(s.execution.starts) == 2


def _native_dag_ready_for_integration(database, workspace_id, tmp_path):
    from pathlib import Path
    from uuid import UUID

    s = NativeScenario(database, workspace_id, tmp_path, pipeline=True)
    with s.uow() as uow:
        worker = uow.workers.get(s.worker_id, s.workspace_id)
        previous = worker.version
        worker.capacity = 2
        worker.version += 1
        uow.workers.save(worker, previous)
    assert s.schedule().assigned == 0 and s.execution.starts == []
    for key in ("a", "b", "c", "summary"):
        s.assign(key)
    s.assign("manual", human=True)
    assert s.schedule("manual").assigned == 0 and s.execution.starts == []
    s.execution.next_status = "busy"
    assert s.schedule().assigned == 1
    s.start()
    original = s.link()
    assert original.not_admitted
    s.cp.reconcile()
    s.cp.activate()
    assert all(s.cp._execution_id(key) is None for key in ("b", "c", "summary"))
    s.execution.next_status = "accepted"
    s.cp.now += timedelta(seconds=3)
    assert s.gateway.sync_active_sessions(s.workspace_id) == 1
    contracts = s.link()
    assert contracts.start_operation_id == original.start_operation_id
    assert len(s.execution.starts) == 2 and contracts.run_id is not None
    (Path(s.execution.starts[-1].workspace.directory) / "contract.txt").write_text(
        "value = 12\n", encoding="utf-8"
    )
    s.execution.emit(contracts.run_id, "run.completed", state="completed")
    s.gateway.sync_active_sessions(s.workspace_id)
    s.register_physical_build("a", "contract.txt", evidence=True)
    s.cp.reconcile()
    s.cp.activate()
    assert s.cp._execution_id("b") is not None and s.cp._execution_id("c") is not None
    assert s.cp._execution_id("summary") is None
    for key in ("b", "c"):
        assert s.schedule(key).assigned == 1
        s.start(key)
    code, tests = s.link("b"), s.link("c")
    code_request, tests_request = s.execution.starts[-2:]
    assert code.profile_ref == tests.profile_ref
    assert code.run_id != tests.run_id and code.authority_id != tests.authority_id
    assert code.start_operation_id != tests.start_operation_id
    assert code_request.workspace.directory != tests_request.workspace.directory
    assert code_request.context != tests_request.context
    (Path(code_request.workspace.directory) / "native_value.py").write_text(
        "def value():\n    return 12\n", encoding="utf-8"
    )
    (Path(tests_request.workspace.directory) / "test_native_value.py").write_text(
        "import unittest\nfrom native_value import value\n\nclass NativeValue(unittest.TestCase):\n    def test_contract(self):\n        self.assertEqual(value(), 12)\n",
        encoding="utf-8",
    )
    for link in (code, tests):
        s.execution.emit(link.run_id, "response.completed")
    s.gateway.sync_active_sessions(s.workspace_id)
    with s.uow() as uow:
        for link in (code, tests):
            assert (
                uow.executions.get(link.execution_id, s.workspace_id).status.value
                == "running"
            )
            assert (
                uow.results.get_code_result_by_capture_operation(
                    UUID(link.preparation["capture_operation_id"]), s.workspace_id
                )
                is None
            )
    for link in (code, tests):
        s.execution.emit(link.run_id, "run.completed", state="completed")
    s.gateway.sync_active_sessions(s.workspace_id)
    s.register_physical_build("b", "native_value.py", evidence=True)
    s.register_physical_build("c", "test_native_value.py", evidence=False)
    s.cp.reconcile()
    s.cp.activate()
    assert s.cp._execution_id("summary") is None
    s.register_physical_build("c", "test_native_value.py", evidence=True)
    s.cp.reconcile()
    s.cp.activate()
    assert s.cp._execution_id("summary") is not None
    inputs = s.prepare_integration_inputs()
    assert s.schedule("summary").assigned == 1
    return s, inputs


def _busy_prepared_native_integration(database, workspace_id, tmp_path):
    from pathlib import Path

    s, inputs = _native_dag_ready_for_integration(database, workspace_id, tmp_path)
    s.execution.next_status = "busy"
    s.start("summary")
    link = s.link("summary")
    directory = Path(s.execution.starts[-1].workspace.directory)
    assert (directory / "native_value.py").read_text(
        encoding="utf-8"
    ) == "def value():\n    return 12\n"
    assert "self.assertEqual(value(), 12)" in (
        directory / "test_native_value.py"
    ).read_text(encoding="utf-8")
    assert (
        link.not_admitted
        and link.preparation["physical_input"]["input_hash"] == inputs.input_hash
    )
    with s.uow() as uow:
        manifest = uow.workspaces.get_for_execution(link.execution_id, s.workspace_id)
    return s, inputs, link, manifest, directory


def test_native_dag_requires_physical_results_and_runs_deterministic_verification(
    database, workspace_id, tmp_path
):
    import time
    from uuid import UUID

    from sidera_control_plane.adapters.git_results import GitResultStore
    from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
    from sidera_control_plane.application.code_result_capture import (
        capture_operation_for_execution,
        code_capture_operation_id,
    )
    from sidera_control_plane.application.result_apply import ResultApplyService
    from sidera_control_plane.application.result_integration import (
        ResultIntegrationService,
    )
    from sidera_control_plane.application.result_integration_pipeline import (
        INTEGRATION_CANDIDATE_PUBLICATION_KIND,
        IntegrationCandidatePublicationHandler,
        integration_candidate_publication_operation_id,
    )
    from sidera_control_plane.result_worker import ResultWorker

    s, inputs, old, old_manifest, directory = _busy_prepared_native_integration(
        database, workspace_id, tmp_path
    )
    old_capture_id = UUID(old.preparation["capture_operation_id"])
    profile = s.profile.model_copy(
        update={"ref": s.profile.ref.model_copy(update={"profile_id": uuid4()})}
    )
    s.catalog.profiles[profile.ref] = profile
    s.assign("summary", profile=profile)
    assert s.schedule("summary").assigned == 1
    s.execution.next_status = "accepted"
    s.start("summary")
    integration = s.link("summary")
    assert (
        integration.profile_ref == profile.ref
        and integration.start_operation_id != old.start_operation_id
    )
    assert (
        integration.preparation["fencing_token"] == old.preparation["fencing_token"] + 1
    )
    assert (
        integration.preparation["physical_input"] == old.preparation["physical_input"]
    )
    with s.uow() as uow:
        current = uow.workspaces.get_for_execution(
            integration.execution_id, s.workspace_id
        )
        assert (
            current.id == old_manifest.id
            and s.manager.physical_directory(current) == directory
        )
        assert (
            capture_operation_for_execution(
                uow, workspace_id=s.workspace_id, execution_id=integration.execution_id
            )
            != old_capture_id
        )
        assert (
            uow.results.get_operation(old_capture_id, s.workspace_id).payload[
                "fencing_token"
            ]
            == old_manifest.fencing_token
        )
        assert (
            uow.results.get_code_result_by_capture_operation(
                old_capture_id, s.workspace_id
            )
            is None
        )
        history = uow.execution_authorities.admission_history(
            s.workspace_id, integration.execution_id
        )
        assert len(history) == 1 and history[0].link == old
    (directory / "integration.txt").write_text(
        "Code and test physically integrated.\n", encoding="utf-8"
    )
    s.execution.emit(integration.run_id, "response.completed")
    s.gateway.sync_active_sessions(s.workspace_id)
    publication_id = integration_candidate_publication_operation_id(
        integration.execution_id
    )
    with s.uow() as uow:
        assert uow.results.get_operation(publication_id, s.workspace_id) is None
        assert (
            uow.results.get_head(
                s.workspace_id, s.cp.ids["root_task"]
            ).current_acceptance_id
            is None
        )
    s.execution.emit(integration.run_id, "run.completed", state="completed")
    s.gateway.sync_active_sessions(s.workspace_id)
    with s.uow() as uow:
        snapshot = uow.results.get_code_result_by_capture_operation(
            capture_operation_for_execution(
                uow, workspace_id=s.workspace_id, execution_id=integration.execution_id
            ),
            s.workspace_id,
        )
        assert snapshot is not None and set(snapshot.consumed_result_ids) == {
            source.result_id for source in inputs.sources
        }
        assert snapshot.capture_operation_id != old_capture_id
        assert (
            uow.results.get_code_result_by_capture_operation(
                old_capture_id, s.workspace_id
            )
            is None
        )
        assert (
            uow.results.get_code_result_by_capture_operation(
                code_capture_operation_id(integration.execution_id), s.workspace_id
            )
            is None
        )
        assert (
            uow.executions.get(integration.execution_id, s.workspace_id).status.value
            == "succeeded"
        )
        operation = uow.results.get_operation(publication_id, s.workspace_id)
        assert operation.payload["code_result_id"] == str(snapshot.id)
    result_worker = ResultWorker(
        s.uow,
        ResultApplyService(
            s.uow,
            GitResultStore({opaque_repository_ref(s.repository): s.repository}),
            clock=s.cp.clock,
        ),
        clock=s.cp.clock,
        operation_handlers={
            INTEGRATION_CANDIDATE_PUBLICATION_KIND: IntegrationCandidatePublicationHandler(
                s.uow, clock=s.cp.clock
            )
        },
    )
    assert result_worker.run_once().processed == 1
    assert result_worker.run_once().processed == 0
    with s.uow() as uow:
        operation = uow.results.get_operation(publication_id, s.workspace_id)
        head = uow.results.get_head(s.workspace_id, s.cp.ids["root_task"])
        candidate = uow.results.get_record(
            "candidate", head.current_candidate_id, s.workspace_id
        )
        verification_request_id = UUID(operation.payload["verification_request_id"])
        assert candidate.code_result_id == snapshot.id and set(
            candidate.source_result_ids
        ) == {source.result_id for source in inputs.sources}
        assert head.current_acceptance_id is None
    assert (
        ResultIntegrationService(s.uow, clock=s.cp.clock).publish_candidate(
            workspace_id=s.workspace_id,
            execution_id=integration.execution_id,
            operation_id=publication_id,
        )
        == candidate
    )
    s.cp.reconcile()
    s.cp.activate()
    with s.uow() as uow:
        request = uow.execution_requests.get(verification_request_id, s.workspace_id)
        verification_id = request.first_execution_id
        assert verification_id is not None and request.purpose == "verification"
        assert (
            uow.agent_bindings.get(
                s.workspace_id, "worker_item", request.worker_item_id
            ).profile_ref
            == integration.profile_ref
        )
    s.cp.run_context_worker("summary")
    assert (
        s.scheduler.run_once(
            1, workspace_id=s.workspace_id, target_execution_id=verification_id
        ).assigned
        == 1
    )
    native_calls = len(s.execution.starts)
    verification_session = s.start(execution_id=verification_id)
    deadline = time.monotonic() + 10
    results = ()
    while time.monotonic() < deadline:
        s.gateway.sync_active_sessions(s.workspace_id)
        with s.uow() as uow:
            results = uow.results.list_verification_results(
                candidate.id, s.workspace_id
            )
        if results:
            break
        time.sleep(0.01)
    assert len(s.execution.starts) == native_calls
    assert (
        len(results) == 1
        and results[0].status.value == "passed"
        and results[0].exit_code == 0
    )
    with s.uow() as uow:
        session = uow.sessions.get_any(verification_session.session_id, s.workspace_id)
        assert session is not None and session.execution_id == verification_id
    assert session.process_ref.startswith("local-check://")
    observation = s.verification_runner.inspect(
        UUID(session.process_ref.removeprefix("local-check://"))
    )
    assert observation is not None and observation.status.value == "passed"
    assert observation.exit_code == 0 and observation.source_dirty is False
    with s.uow() as uow:
        verification_workspace = uow.workspaces.get_for_execution(
            verification_id, s.workspace_id
        )
        assert not tuple(
            s.manager.physical_directory(verification_workspace).rglob("__pycache__")
        )
        assert (
            uow.executions.get(verification_id, s.workspace_id).status.value
            == "succeeded"
        )
        assert (
            uow.results.get_head(
                s.workspace_id, s.cp.ids["root_task"]
            ).current_acceptance_id
            is None
        )


@pytest.mark.parametrize("fault", ("dirty", "corrupt_tree"))
def test_native_v3_prepared_input_changes_deny_refence_before_start(
    database, workspace_id, tmp_path, fault
):
    import subprocess

    from sidera_control_plane.adapters.git_workspace import WorkspaceProviderError

    s, _, old, manifest, directory = _busy_prepared_native_integration(
        database, workspace_id, tmp_path
    )
    (directory / "native_value.py").write_text(
        "def value():\n    return 13\n", encoding="utf-8"
    )
    if fault == "corrupt_tree":
        subprocess.run(
            ("git", "-C", str(directory), "add", "--", "native_value.py"),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                str(directory),
                "commit",
                "-m",
                "Owned fixture corrupts prepared input",
            ),
            check=True,
            capture_output=True,
        )
    profile = s.profile.model_copy(
        update={"ref": s.profile.ref.model_copy(update={"profile_id": uuid4()})}
    )
    s.catalog.profiles[profile.ref] = profile
    s.assign("summary", profile=profile)
    assert s.schedule("summary").assigned == 1
    calls = len(s.execution.starts)
    s.execution.next_status = "accepted"
    with pytest.raises(WorkspaceProviderError):
        s.start("summary")
    assert len(s.execution.starts) == calls
    with s.uow() as uow:
        assert uow.workspaces.get(manifest.id, s.workspace_id) == manifest
        assert (
            uow.execution_authorities.get_link(s.workspace_id, old.execution_id) is None
        )
        assert (
            uow.execution_authorities.admission_history(
                s.workspace_id, old.execution_id
            )[0].link
            == old
        )
    assert s.manager.physical_directory(manifest) == directory


def test_native_v3_refence_save_failure_keeps_canonical_cache_truthful_then_retries(
    database, workspace_id, tmp_path
):
    from sidera_control_plane.persistence.workspace_repository import (
        WorkspaceRepository,
    )

    s, _, old, manifest, directory = _busy_prepared_native_integration(
        database, workspace_id, tmp_path
    )
    profile = s.profile.model_copy(
        update={"ref": s.profile.ref.model_copy(update={"profile_id": uuid4()})}
    )
    s.catalog.profiles[profile.ref] = profile
    s.assign("summary", profile=profile)
    assert s.schedule("summary").assigned == 1
    calls = len(s.execution.starts)
    s.execution.next_status = "accepted"
    original = WorkspaceRepository.save

    def fail_refence(repository, candidate, previous_version):
        if (
            candidate.id == manifest.id
            and candidate.fencing_token == manifest.fencing_token + 1
        ):
            raise RuntimeError("bounded native refence persistence failure")
        return original(repository, candidate, previous_version)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(WorkspaceRepository, "save", fail_refence)
        with pytest.raises(RuntimeError, match="bounded native refence"):
            s.start("summary")
    with s.uow() as uow:
        assert uow.workspaces.get(manifest.id, s.workspace_id) == manifest
        assert (
            uow.execution_authorities.get_link(s.workspace_id, old.execution_id) is None
        )
        assert (
            uow.execution_authorities.admission_history(
                s.workspace_id, old.execution_id
            )[0].link
            == old
        )
    assert (
        len(s.execution.starts) == calls
        and s.manager.physical_directory(manifest) == directory
    )
    s.start("summary")
    admitted = s.link("summary")
    with s.uow() as uow:
        current = uow.workspaces.get(manifest.id, s.workspace_id)
        assert (
            current.fencing_token == manifest.fencing_token + 1
            and current.version > manifest.version
        )
    assert s.manager.physical_directory(current) == directory
    assert len(s.execution.starts) == calls + 1 and admitted.run_id is not None
    assert admitted.preparation["physical_input"] == old.preparation["physical_input"]


def test_native_terminal_capture_pin_blocks_input_before_cp_completion(native_scenario):
    from sidera_control_plane.application.assignments import TrustedAssignmentIdentity
    from sidera_control_plane.application.execution_commands import (
        ExecutionInputCommandHandler,
    )
    from sidera_control_plane.domain.errors import ExecutionContextChanged

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    result = s.start()
    link = s.link()
    snapshot = s.execution.get_run(
        s.workspace_id, link.run_id, grant_id=link.authority_id
    )
    assert snapshot.state == "running" and snapshot.input_open
    with s.uow() as uow:
        execution = uow.executions.get(link.execution_id, s.workspace_id)
        session = uow.sessions.get(result.session_id, s.workspace_id)
        placement = uow.worker_items.get_current_placement(
            execution.worker_item_id, s.workspace_id
        )
        item = uow.worker_items.get(execution.worker_item_id, s.workspace_id)
        authority = uow.execution_authorities.get(link.authority_id)
        assert execution.status.value == "running" and session.status.value == "active"
    s.capture.observe_terminal(
        workspace_id=s.workspace_id,
        execution_id=execution.id,
        operation_id=s.gateway._capture_operation_id(s.workspace_id, execution.id),
        outcome="succeeded",
        fencing_token=authority.fencing_token,
        details={"run_id": str(link.run_id), "event_type": "run.completed"},
    )
    command = s.command(
        "SendExecutionInput",
        "execution_session",
        session.id,
        session.version,
        {
            "client_idempotency_key": "native-finalizing",
            "text": "Do not deliver after the terminal boundary.",
            "attachment_refs": [],
            "context": {
                "worker_item_id": str(item.id),
                "plan_generation": placement.plan_generation,
                "context_version": item.context_version,
                "execution_id": str(execution.id),
                "session_id": str(session.id),
                "session_version": session.version,
                "worker_id": str(session.worker_id),
                "execution_mode": "native_profile",
                "binding_version": link.binding_version,
                "run_id": str(link.run_id),
            },
        },
    )
    handler = ExecutionInputCommandHandler(
        s.cp.clock,
        identity=TrustedAssignmentIdentity(s.principal, s.setup_grant),
        native_services=s.services,
    )
    with pytest.raises(ExecutionContextChanged) as denied:
        CommandService(s.uow, (handler,), preflight=handler.preflight).handle(command)
    assert "execution_finalizing" in denied.value.details["changed_fields"]
    assert len(s.execution.starts) == 1
    with s.uow() as uow:
        assert (
            uow.execution_inputs.get_pending_for_session(session.id, s.workspace_id)
            is None
        )


@pytest.mark.parametrize(
    "fault",
    (
        "missing_binding",
        "wrong_binding_lease",
        "wrong_binding_fence",
        "missing_workspace",
        "wrong_workspace_fence",
    ),
)
def test_busy_retirement_requires_exact_reserved_runtime_evidence(
    native_scenario, fault
):
    from dataclasses import replace

    from sidera_control_plane.persistence.module_repository import (
        ExecutionBindingRepository,
    )
    from sidera_control_plane.persistence.workspace_repository import (
        WorkspaceRepository,
    )

    s = native_scenario
    s.assign()
    assert s.schedule().assigned == 1
    s.execution.next_status = "busy"
    s.start()
    link = s.link()
    with s.uow() as uow:
        binding = uow.bindings.get_for_execution(link.execution_id, s.workspace_id)
        authority = uow.execution_authorities.get(link.authority_id)
        workspace = uow.workspaces.get(authority.workspace_ref, s.workspace_id)
        worker = uow.workers.get(binding.worker_id, s.workspace_id)
        lease = uow.leases.get(binding.lease_id, s.workspace_id)
        execution = uow.executions.get(link.execution_id, s.workspace_id)
        block = uow.scheduling_blocks.get_active_for_execution(
            execution.id, s.workspace_id
        )
        assert link.not_admitted and worker.current_load == 1
    if fault.startswith(("missing_binding", "wrong_binding")):
        original = ExecutionBindingRepository.get_for_execution

        def unavailable(repository, execution_id, workspace_id, **kwargs):
            actual = original(repository, execution_id, workspace_id, **kwargs)
            if (execution_id, workspace_id) != (link.execution_id, s.workspace_id):
                return actual
            if fault == "missing_binding":
                return None
            return (
                replace(actual, lease_id=uuid4())
                if fault == "wrong_binding_lease"
                else replace(actual, fencing_token=actual.fencing_token + 1)
            )

        owner, method = ExecutionBindingRepository, "get_for_execution"
    else:
        original = WorkspaceRepository.get

        def unavailable(repository, workspace_id, scope_id, **kwargs):
            actual = original(repository, workspace_id, scope_id, **kwargs)
            if (workspace_id, scope_id) != (workspace.id, s.workspace_id):
                return actual
            return (
                None
                if fault == "missing_workspace"
                else replace(actual, fencing_token=actual.fencing_token + 1)
            )

        owner, method = WorkspaceRepository, "get"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(owner, method, unavailable)
        with pytest.raises(DomainError) as denied:
            s.assign(human=True)
        assert denied.value.code == "handoff_required"
    with s.uow() as uow:
        assert uow.execution_authorities.get_link(s.workspace_id, execution.id) == link
        assert uow.execution_authorities.get(authority.id) == authority
        assert uow.bindings.get_for_execution(execution.id, s.workspace_id) == binding
        assert uow.workspaces.get(workspace.id, s.workspace_id) == workspace
        assert uow.workers.get(worker.id, s.workspace_id) == worker
        assert uow.leases.get(lease.id, s.workspace_id) == lease
        assert uow.executions.get(execution.id, s.workspace_id) == execution
        assert (
            uow.scheduling_blocks.get_active_for_execution(execution.id, s.workspace_id)
            == block
        )
        assert (
            uow.execution_authorities.admission_history(s.workspace_id, execution.id)
            == ()
        )
