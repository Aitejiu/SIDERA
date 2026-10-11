from copy import copy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sidera_agents.contracts import (
    CredentialRef,
    EventPage,
    ResourceRef,
    RunEvent,
    RunState,
    SecretBinding,
    StartReceipt,
    StartStatus,
    ToolBinding,
)
from sidera_agents.errors import AgentError

from sidera_control_plane.application.agent_worker_adapter import (
    NativeDeferredResult,
    NativeSourceValidation,
    NativeWorkerServices,
    native_capabilities,
    native_preparation_command,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
)
from sidera_control_plane.application.handoff_coordinator import HandoffCoordinator
from sidera_control_plane.application.profile_handoff import (
    validate_native_handoff_confirmation,
)
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    GatewayRecoveryRequired,
    WorkerGateway,
)
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.application.worker_runtime import ResolvedWorkerRuntime
from sidera_control_plane.application.workspace_handoff import (
    WorkspaceHandoffError,
    WorkspaceHandoffService,
)
from sidera_control_plane.domain.agent_bindings import AgentBinding, AgentExecutionLink
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffWorkspaceTransfer,
    HandoffWorkspaceTransferStatus,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransitionStatus,
    HandoffWriterState,
)
from sidera_control_plane.domain.lease import AssignmentLease, LeaseStatus
from sidera_control_plane.domain.modules import (
    ExecutionBinding,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    SandboxCapability,
    StabilityLevel,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.worker_item_projections import WorkerItemPlacementFact
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceLifecycle,
    workspace_location_ref_hash,
    workspace_manifest_digest,
)
from tests.unit.test_native_handoff_drain import (
    NOW,
    DrainRecords,
    known_stop,
    make_drain_case,
)
from tests.unit.test_native_terminal_stop import preparation


class StartAuthorities:
    def __init__(self, case, source):
        self.case, self.source = case, source

    def get(self, identifier, **kwargs):
        del kwargs
        return next(
            (a for a in (self.case.authority, self.source.authority) if a.id == identifier),
            None,
        )

    def get_link(self, workspace_id, execution_id, **kwargs):
        del kwargs
        return next(
            (
                link
                for link in (self.case.link, self.source.link)
                if (link.workspace_id, link.execution_id) == (workspace_id, execution_id)
            ),
            None,
        )

    def prepared_location_ref(self, grant_id, workspace_id):
        assert (grant_id, workspace_id) == (
            self.case.authority.id,
            self.case.authority.workspace_id,
        )
        return "controlled://target"

    def lock_intention(self, expected, *, now):
        s = self.case
        assert expected == s.link and not s.authority.revoked
        execution = s.uow.executions.get(expected.execution_id, expected.workspace_id)
        lease = s.uow.leases.get(s.authority.lease_id, expected.workspace_id)
        execution.assert_fence(lease_id=lease.id, fencing_token=s.authority.fencing_token)
        lease.assert_owner(execution.assigned_worker_id, s.authority.fencing_token, now)
        return execution, lease, expected, s.authority

    def admission_history(self, workspace_id, execution_id):
        assert (workspace_id, execution_id) == (
            self.case.link.workspace_id, self.case.link.execution_id
        )
        return ()

    def lock_original_observation(self, expected, *, principal_id):
        s = self.case
        assert expected == s.link and principal_id == s.authority.principal_id
        execution = s.uow.executions.get(expected.execution_id, expected.workspace_id)
        session = s.uow.sessions.get_any(s.session_id, expected.workspace_id)
        assert session.execution_id == execution.id
        assert session.adapter_session_id == f"agent-run:{expected.run_id}"
        return execution, session, expected, s.authority

    def save_original_observation(self, expected, *, principal_id, event_cursor, status):
        self.lock_original_observation(expected, principal_id=principal_id)
        assert event_cursor == expected.event_cursor + 1
        self.case.link = replace(expected, event_cursor=event_cursor, status=status)
        return self.case.link


class StartEvents(DrainRecords):
    def get(self, session_id, sequence, workspace_id):
        return next(
            (
                row
                for row in self.values.values()
                if (row.session_id, row.sequence, row.workspace_id)
                == (session_id, sequence, workspace_id)
            ),
            None,
        )

    def list_for_session(self, session_id, workspace_id):
        return tuple(
            row
            for row in self.values.values()
            if (row.session_id, row.workspace_id) == (session_id, workspace_id)
        )


class StartRequests(DrainRecords):
    def get_effective_by_execution_lineage(self, lineage_id, workspace_id):
        return next(
            (
                copy(row)
                for row in self.values.values()
                if (row.execution_lineage_id, row.workspace_id) == (lineage_id, workspace_id)
            ),
            None,
        )

    def get_by_handoff_transition(self, transition_id, workspace_id):
        return next(
            (
                copy(row)
                for row in self.values.values()
                if (row.handoff_transition_id, row.workspace_id)
                == (transition_id, workspace_id)
            ),
            None,
        )

    def get_many(self, identifiers, workspace_id, **kwargs):
        return tuple(self.get(identifier, workspace_id, **kwargs) for identifier in identifiers)


class TargetNative:
    def __init__(self, case):
        self.case = case
        self.starts = []
        self.reads = []
        self.read_error = None
        self.after_read = lambda: None

    def admit(self):
        s = self.case
        self.admitted_identity = (s.link.workspace_id, s.link.run_id, s.link.authority_id)
        self.starts.append((s.link.start_operation_id, s.link.run_id))
        return StartReceipt(
            operation_id=s.link.start_operation_id,
            run_id=s.link.run_id,
            status="accepted",
            reason_code=None,
        )

    def get_run(self, workspace_id, run_id, *, grant_id):
        s = self.case
        assert s.transactions == 0, "native I/O must stay outside the unit of work"
        assert (workspace_id, run_id, grant_id) == self.admitted_identity
        s.reader.require(grant_id, "run.read", workspace_id)
        self.reads.append(run_id)
        if self.read_error is not None:
            raise self.read_error
        result = s.snapshot
        self.after_read()
        return result

    def list_events(self, workspace_id, run_id, after_sequence, *, grant_id, limit=100):
        s = self.case
        assert s.transactions == 0
        assert (workspace_id, run_id, grant_id) == self.admitted_identity
        assert limit == 100
        return EventPage(
            events=tuple(event for event in s.native_events if event.sequence > after_sequence),
            next_sequence=1,
            has_more=False,
        )

    def validated_facts(self, grant_id, source):
        s = self.case
        s.reader.require(grant_id, "source.read", source)
        assert source == s.prepared.sources[0].ref
        s.source_checks.append(source)
        return s.prepared.sources[0]

    def require_execution(self, grant_id, workspace_id, execution_id, operation_id):
        s = self.case
        assert (grant_id, workspace_id, execution_id, operation_id) == (
            s.link.authority_id, s.link.workspace_id, s.link.execution_id,
            s.link.start_operation_id,
        )
        s.reader.require(grant_id, "source.read", s.prepared.sources[0].ref)

    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            pytest.fail(f"Unexpected native action: {name}")

        return forbidden


def make_start_case(*, session=True, claim="released", target_fence=1, real_scheduler=False):
    from contextlib import contextmanager

    source = make_drain_case()
    known_stop(source)
    assert source.coordinator.run_once(limit=1).progressed == 1
    uow, transition = source.uow, source.uow.handoff_transitions.item
    source_execution = uow.executions.values[source.link.execution_id]
    source_manifest = uow.workspaces.values[source.authority.workspace_ref]
    source_manifest.freeze(now=NOW)
    freeze_id = uuid4()
    transition.record_workspace_freeze(
        operation_id=freeze_id,
        facts={
            "operation_id": str(freeze_id),
            "transition_id": str(transition.id),
            "manifest_id": str(source_manifest.id),
            "workspace_id": str(source_manifest.workspace_id),
            "execution_id": str(source_manifest.execution_id),
            "fencing_token": 1,
            "workspace_digest": workspace_manifest_digest(source_manifest),
            "location_ref": source_manifest.location_ref,
            "lifecycle": "frozen",
            "stop_sequence": transition.source_stop_sequence,
            "frozen_at": NOW.isoformat(),
        },
    )
    authority = replace(
        source.authority,
        id=uuid4(),
        execution_id=uuid4(),
        lease_id=uuid4(),
        workspace_ref=uuid4(),
        fencing_token=target_fence,
        profile_ref=transition.native_payload.target_profile_ref,
        upstream_grant_id=uuid4(),
        actions=(
            "run.start", "run.input", "run.interrupt", "run.recover", "run.read",
            "run.stop", "profile.read", "runtime.read", "workspace.execute",
            "source.read", "tool.execute", "secret.read", "resource.read",
        ),
        credentials=(CredentialRef(workspace_id=transition.workspace_id, credential_id=uuid4()),),
        resources=(ResourceRef(workspace_id=transition.workspace_id, resource_id=uuid4(), revision=1),),
        tools=(ToolBinding(connection_id=uuid4()),),
    )
    worker = Worker.register(
        worker_id=uuid4(), workspace_id=authority.workspace_id,
        adapter_type="controlled-native-start", display_name="Handoff target",
        capabilities=frozenset({"code"}), capacity=1, cost_per_hour_microunits=0,
        startup_latency_ms=0, now=NOW,
    )
    worker.reserve()
    execution = Execution.create(
        execution_id=authority.execution_id, workspace_id=authority.workspace_id,
        task_id=authority.target_id, worker_item_id=transition.worker_item_id,
        lineage_id=uuid4(), attempt=1, max_attempts=1, purpose="code",
        capability_requirements=frozenset({"code"}), pinned_worker_id=None,
        not_before=NOW, source_execution_id=source_execution.id,
        source_session_id=transition.source_session_id, handoff_transition_id=transition.id,
    )
    execution.assign(lease_id=authority.lease_id, worker_id=worker.id, fencing_token=target_fence)
    lease = AssignmentLease.issue(
        lease_id=authority.lease_id, workspace_id=authority.workspace_id,
        execution_id=execution.id, worker_id=worker.id, issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5), fencing_token=target_fence,
        assignment_reason={
            "policy": "handoff-native-v2", "handoff_transition_id": str(transition.id),
        },
    )
    manifest = ExecutionWorkspaceManifest.create(
        id=authority.workspace_ref, workspace_id=authority.workspace_id,
        task_id=execution.task_id, execution_id=execution.id, checkouts=(),
        location_ref="controlled://target", retention_seconds=3600,
        fencing_token=target_fence, created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    capabilities = native_capabilities()
    adapter_definition = ModuleDefinition(
        uuid4(), authority.workspace_id, "worker_adapter", worker.adapter_type,
        "1", 1, capabilities, ModuleLifecycle.ACTIVE,
    )
    workspace_definition = ModuleDefinition(
        uuid4(), authority.workspace_id, "workspace_provider", "controlled-workspace",
        "1", 1, WorkspaceProviderCapabilityProfile(
            SandboxCapability.FILESYSTEM, True, False, StabilityLevel.STABLE
        ), ModuleLifecycle.ACTIVE,
    )
    adapter_instance = ModuleInstance(
        uuid4(), authority.workspace_id, adapter_definition.id, "Target adapter",
        1, "adapter", {}, (), ModuleLifecycle.ACTIVE,
    )
    workspace_instance = ModuleInstance(
        uuid4(), authority.workspace_id, workspace_definition.id, "Target workspace",
        1, "workspace", {}, (), ModuleLifecycle.ACTIVE,
    )
    binding = ExecutionBinding(
        uuid4(), authority.workspace_id, execution.id, worker.id,
        adapter_instance.id, workspace_instance.id, 1, 1, capabilities,
        lease.id, target_fence, 1,
    )
    selected = AgentBinding(
        authority.workspace_id, authority.target_type, authority.target_id,
        transition.native_payload.target_assignee, authority.profile_ref,
        1, NOW, NOW, runnable=True,
    )
    transition.target_execution_id = execution.id
    transition.target_worker_id = worker.id
    transition.target_binding_version = 1
    transition.target_workspace_manifest_id = manifest.id
    transition.phase = HandoffPhase.START_TARGET_SESSION
    transition.writer_state = HandoffWriterState.TARGET_STARTING
    transition.failure_code = None
    transition.lease_owner = "native-start" if claim != "released" else None
    transition.lease_expires_at = (
        NOW + timedelta(seconds=-1 if claim == "expired" else 30)
        if claim != "released" else None
    )
    bundle = SimpleNamespace(
        id=uuid4(), workspace_id=authority.workspace_id,
        target_execution_id=execution.id, worker_item_id=transition.worker_item_id,
        target_context_version=1, bundle_hash="e" * 64,
    )
    transition.handoff_context_bundle_id = bundle.id
    package = ContextPackageManifestV1.create(
        package_id=uuid4(), workspace_id=authority.workspace_id,
        purpose=ContextPurpose.EXECUTION, consumer_type=ContextConsumerType.WORKER,
        consumer_id=execution.id, worker_item_id=transition.worker_item_id,
        work_context_revision_id=transition.native_payload.work_context_revision_id,
        context_version=1, control_plane_fact_snapshot={"handoff": {
            "bundle_id": str(bundle.id), "bundle_hash": bundle.bundle_hash,
            "transition_id": str(transition.id), "logical_attempt": 1,
            "instruction": "Continue under the exact admitted authority",
        }}, knowledge_refs=(), artifact_refs=(), evidence_refs=(),
        budget=ContextBudgetV1(uuid4(), 1, 16, 65536, 16384),
        expires_at=NOW + timedelta(minutes=5), idempotency_key="native-start",
        created_at=NOW,
    )
    transition.target_context_package_id = package.package_id
    transition.target_context_package_hash = package.package_hash
    assembly = ContextAssemblyRequest.create(
        request_id=uuid4(), workspace_id=authority.workspace_id,
        purpose=ContextPurpose.EXECUTION, consumer_type=ContextConsumerType.WORKER,
        consumer_id=execution.id, worker_item_id=transition.worker_item_id,
        work_context_revision_id=package.work_context_revision_id, context_version=1,
        knowledge_search_scope={}, knowledge_filter_refs=(), fact_scope_refs=(),
        budget={}, expires_at=package.expires_at, request_fingerprint="f" * 64,
        idempotency_key="native-start", created_at=NOW,
    )
    assembly = replace(assembly, status=ContextAssemblyStatus.READY)
    prepared, _ = preparation(authority)
    prepared = prepared.model_copy(update={
        "command_id": uuid5(NAMESPACE_URL, f"sidera.native.prepare-context:{execution.id}:1:{package.package_id}:{package.package_hash}:{prepared.revision_hash}"),
        "package_id": package.package_id, "package_hash": package.package_hash,
        "revision_id": package.work_context_revision_id,
        "fencing_token": target_fence,
    })
    prepared_command = native_preparation_command(prepared)
    saved_preparation = SimpleNamespace(
        id=prepared.command_id, workspace_id=authority.workspace_id, status="applied",
        request_hash=hash_request(prepared_command),
        result={"data": {"preparation": prepared.model_dump(mode="json")}},
    )
    run_id = uuid4()
    link = AgentExecutionLink(
        authority.workspace_id, execution.id,
        gateway_operation_id(execution.id, GatewayOperationType.START_SESSION),
        authority.profile_ref, run_id, 0, "starting", authority.id, 1,
        request_sha256="a" * 64, preparation=prepared.model_dump(mode="json"),
    )
    s = SimpleNamespace(
        source=source, uow=uow, authority=authority, selected=selected,
        original_selected=selected, link=link, session_id=uuid5(NAMESPACE_URL, f"sidera.native.session:{run_id}"),
        transactions=0, host_allowed=True, host_calls=[], outputs=[],
        prepared=prepared, saved_preparation=saved_preparation, source_checks=[],
    )
    s.native = TargetNative(s)
    s.link = replace(link, start_receipt=s.native.admit())
    s.snapshot = source.snapshot.model_copy(update={
        "run_id": run_id, "profile_ref": authority.profile_ref, "state": RunState.STARTING,
        "effective_config": source.snapshot.effective_config.model_copy(update={"profile_ref": authority.profile_ref}),
        "last_event_sequence": 1,
    })
    s.native_events = (RunEvent(
        run_id=run_id, sequence=1, occurred_at=NOW, kind="run.started",
        operation_id=link.start_operation_id, response_id=None, data={"status": "starting"},
    ),)
    if session:
        execution.start(lease_id=lease.id, fencing_token=target_fence, now=NOW)
        worker.mark_running()
        manifest.mark_active()
        uow.sessions.add(ExecutionSession.start(
            session_id=s.session_id, workspace_id=authority.workspace_id,
            execution_id=execution.id, worker_id=worker.id,
            adapter_session_id=f"agent-run:{run_id}", now=NOW,
            adapter_module_id=adapter_instance.id, adapter_config_version=1,
            workspace_module_id=workspace_instance.id, workspace_config_version=1,
            capability_snapshot=capabilities,
            process_ref=f"agent-run-reference:{run_id}",
            workspace_ref=workspace_instance.instance_name,
        ))
    for repository, row in (
        (uow.executions, execution), (uow.workers, worker), (uow.leases, lease),
        (uow.workspaces, manifest), (uow.bindings, binding),
    ):
        repository.add(row)
    task = Task.create(
        task_id=execution.task_id, workspace_id=authority.workspace_id,
        title="Native handoff", description="Exact target", acceptance_criteria=("start",),
        priority=1, requested_by=authority.principal_id,
    )
    uow.tasks = DrainRecords((task,))
    uow.worker_items = DrainRecords((WorkerItem.create(
        item_id=transition.worker_item_id, workspace_id=authority.workspace_id,
        root_task_id=transition.native_payload.root_task_id,
        title="Native handoff", responsibility="Continue the admitted Run", now=NOW,
    ),))
    placement = WorkerItemPlacementFact(
        transition.native_payload.observed_plan_snapshot_id,
        transition.native_payload.observed_plan_generation,
        transition.worker_item_id, "native", "native", execution.task_id,
        "implementation", ("code",),
    )
    uow.worker_items.get_current_placement = lambda *a: placement
    uow.plans = SimpleNamespace(get_head=lambda *a, **k: None)
    uow.results = SimpleNamespace(get_head=lambda *a, **k: None)
    uow.agent_bindings = SimpleNamespace(
        get=lambda *a, **k: s.selected, history=lambda *a, **k: (s.original_selected,),
    )
    uow.execution_authorities = StartAuthorities(s, source)
    uow.modules = SimpleNamespace(
        get_instance=lambda identifier, workspace: {adapter_instance.id: adapter_instance, workspace_instance.id: workspace_instance}.get(identifier),
        get_definition=lambda identifier, workspace: {adapter_definition.id: adapter_definition, workspace_definition.id: workspace_definition}.get(identifier),
    )
    uow.context_packages = SimpleNamespace(
        get=lambda identifier, workspace, **k: package if identifier == package.package_id else None,
        get_for_consumer=lambda purpose, consumer, workspace, **k: package
        if (purpose, consumer, workspace) == (ContextPurpose.EXECUTION, execution.id, authority.workspace_id)
        else None,
        get_ack=lambda *a, **k: None,
    )
    uow.context_assemblies = SimpleNamespace(get_for_consumer=lambda *a, **k: assembly)
    uow.handoff_context_bundles = DrainRecords((bundle,))
    uow.commands.find_by_id = lambda workspace, identifier: (
        saved_preparation if identifier == prepared.command_id else None
    )
    uow.scheduling_blocks = SimpleNamespace(get_active_for_execution=lambda *a, **k: None)
    uow.execution_events = StartEvents()
    uow.artifacts = SimpleNamespace(list_for_execution=lambda *a: ())
    uow.evidence_claims = SimpleNamespace(list_for_task=lambda *a: ())
    requests = []
    for current in (source_execution, execution):
        request = ExecutionRequest.create(
            request_id=uuid4(), workspace_id=authority.workspace_id,
            planning_request_id=uuid4(), proposal_revision_id=uuid4(),
            plan_snapshot_id=transition.native_payload.observed_plan_snapshot_id,
            plan_generation=1, root_task_id=transition.native_payload.root_task_id,
            subtask_id=execution.task_id, worker_item_id=transition.worker_item_id,
            worker_item_key="native", intent_key="start", purpose="code",
            capability_requirements=frozenset({"code"}), adapter_preferences=(),
            budget_class="default", not_before=NOW, max_attempts=1,
            expected_artifact_refs=(), expected_evidence_refs=(), now=NOW,
            source_execution_id=current.source_execution_id,
            source_session_id=current.source_session_id,
            handoff_transition_id=current.handoff_transition_id,
        )
        requests.append(replace(
            request, status=ExecutionRequestStatus.MATERIALIZED,
            execution_lineage_id=current.lineage_id, first_execution_id=current.id,
        ))
    uow.execution_requests = StartRequests(tuple(requests))

    @contextmanager
    def uow_factory():
        s.transactions += 1
        try:
            with source.uow_factory() as current:
                yield current
        finally:
            s.transactions -= 1

    def host_require(grant_id, action, target):
        s.host_calls.append((grant_id, action, target))
        if not s.host_allowed or grant_id != authority.upstream_grant_id:
            raise AgentError("permission_denied", "Host grant denied")
        assert (target.workspace_id, target.target_type, target.target_id, target.principal_id) == (
            authority.workspace_id, authority.target_type, authority.target_id, authority.principal_id
        )

    s.uow_factory = uow_factory
    s.reader = ExecutionAuthorityReader(
        uow_factory, principal=lambda: authority.principal_id,
        authorizer=SimpleNamespace(require=host_require), clock=lambda: NOW,
    )
    services = NativeWorkerServices(
        s.native, s.native, s.native, SimpleNamespace(require=host_require),
        authority.principal_id, uuid4(),
    )
    s.gateway = WorkerGateway(
        uow_factory, adapter_factories={}, workspace_factories={}, native_services=services,
        native_source_reader=s.native, clock=lambda: NOW,
        spec_factory=SimpleNamespace(create=lambda *a, **k: SimpleNamespace(
            workspace_id=authority.workspace_id, execution_id=execution.id
        )),
    )
    scheduler = SimpleNamespace(assign_handoff_target=lambda **k: binding)
    if real_scheduler:
        runtime = ResolvedWorkerRuntime(
            worker.id, worker.adapter_type, adapter_instance.id, 1,
            workspace_instance.id, 1, adapter_definition.id, workspace_definition.id,
            capabilities, {"execution_mode": "native"}, workspace_definition.capabilities,
        )
        scheduler = Scheduler(
            uow_factory, LowestCostWorkerPolicy(), clock=lambda: NOW,
            runtime_resolver=SimpleNamespace(lock_and_resolve=lambda *a: runtime),
        )
    s.coordinator = HandoffCoordinator(
        uow_factory=uow_factory, gateway=s.gateway,
        scheduler=scheduler,
        clock=lambda: NOW, owner_id="native-start", lease_ttl_seconds=30,
    )
    return s


def make_ack_case():
    s = make_start_case(session=False, claim="held", target_fence=2)
    s.snapshot = s.snapshot.model_copy(update={"state": RunState.RUNNING})
    assert start_target(s).session_id == s.session_id
    transition = s.uow.handoff_transitions.item
    transition.phase = HandoffPhase.ACKNOWLEDGE_CONTEXT
    manifest = s.uow.workspaces.values[s.authority.workspace_ref]
    source = NativeSourceValidation(
        ref=s.authority.sources[0], content_hash="sha256:" + "e" * 64,
        validated_bytes=7,
    )
    s.prepared = s.prepared.model_copy(update={
        "sources": (source,), "validated_sections": 1, "validated_bytes": 7,
        "workspace_manifest_digest": workspace_manifest_digest(manifest),
        "workspace_location_hash": workspace_location_ref_hash(manifest.location_ref),
    })
    s.link = replace(s.link, preparation=s.prepared.model_dump(mode="json"))
    s.saved_preparation.request_hash = hash_request(native_preparation_command(s.prepared))
    s.saved_preparation.result = {"data": {"preparation": s.link.preparation}}
    s.uow.work_contexts = SimpleNamespace(
        get_revision=lambda identifier, workspace: SimpleNamespace(
            id=s.prepared.revision_id, workspace_id=s.authority.workspace_id,
            content_hash=s.prepared.revision_hash,
        ) if (identifier, workspace) == (s.prepared.revision_id, s.authority.workspace_id) else None,
        get_by_root_task=lambda *a: SimpleNamespace(current_revision_id=s.prepared.revision_id),
    )
    transfer = HandoffWorkspaceTransfer(
        transition_id=transition.id, logical_attempt=transition.logical_attempt,
        operation_id=uuid4(), source_manifest_id=transition.source_workspace_manifest_id,
        target_manifest_id=manifest.id, source_execution_id=transition.source_execution_id,
        target_execution_id=s.authority.execution_id, source_fencing_token=1,
        target_fencing_token=2, location_ref_hash=s.prepared.workspace_location_hash,
        repository_state_digest="e" * 64, status=HandoffWorkspaceTransferStatus.SUCCEEDED,
        failure_code=None, created_at=NOW, updated_at=NOW,
    )
    s.uow.workspace_transfers = SimpleNamespace(
        get_by_attempt=lambda transition_id, attempt, workspace: transfer
        if (transition_id, attempt, workspace) == (transition.id, 1, transition.workspace_id)
        else None,
    )
    return s


def confirm_target(s):
    transition = s.uow.handoff_transitions.item
    return s.gateway.enable_handoff_target_writer(
        workspace_id=transition.workspace_id, transition_id=transition.id,
        expected_transition_version=transition.version,
    )


def advance_observation(s, *, status):
    session = s.uow.sessions.get_any(s.session_id, s.authority.workspace_id)
    previous = session.version
    session.record_activity(sequence=session.last_sequence + 1, now=NOW)
    s.uow.sessions.save(session, previous)
    s.uow.execution_authorities.save_original_observation(
        s.link, principal_id=s.authority.principal_id,
        event_cursor=s.link.event_cursor + 1, status=status,
    )


def start_target(s):
    transition = s.uow.handoff_transitions.item
    return s.gateway.start_handoff_target(
        workspace_id=transition.workspace_id, transition_id=transition.id,
        expected_transition_version=transition.version,
    )


def projection_targets(s):
    authority = s.authority
    return (
        ("profile.read", authority.profile_ref),
        ("workspace.execute", authority.execution_workspace),
        ("source.read", authority.sources[0]),
        ("tool.execute", authority.tools[0]),
        ("secret.read", SecretBinding(credential_ref=authority.credentials[0], destination="TOKEN")),
        ("resource.read", authority.resources[0]),
    )


def project_async(s):
    for action, target in projection_targets(s):
        s.reader.require(s.authority.id, action, target)
        if action == "secret.read":
            s.outputs.append("secret")
    s.outputs.append("native_open")


@pytest.mark.parametrize("claim", ("released", "expired"))
@pytest.mark.parametrize("link_status", ("starting", "running", "waiting"))
def test_admitted_projection_uses_durable_lease_between_claims(claim, link_status):
    s = make_start_case(claim=claim)
    s.link = replace(s.link, status=link_status)
    project_async(s)
    assert len(s.host_calls) == 6 and s.outputs == ["secret", "native_open"]


@pytest.mark.parametrize("claim", ("released", "expired"))
@pytest.mark.parametrize("bad", (
    "no_run", "receipt_missing", "receipt_unknown", "receipt_busy", "receipt_rejected", "receipt_operation",
    "receipt_run", "session_missing", "session_id", "session_execution", "session_adapter",
    "session_closed", "transition_session", "link_terminal", "link_unknown", "phase", "blocked",
    "blocked_unknown", "failed",
))
def test_between_claims_projection_requires_exact_admission(claim, bad):
    s = make_start_case(claim=claim)
    transition = s.uow.handoff_transitions.item
    session = s.uow.sessions.values[s.session_id]
    if bad == "no_run":
        s.link = replace(s.link, run_id=None)
    elif bad == "receipt_missing":
        s.link = replace(s.link, start_receipt=None)
    elif bad.startswith("receipt_"):
        receipt = s.link.start_receipt.model_copy(update={
            {"receipt_unknown": "status", "receipt_busy": "status", "receipt_rejected": "status", "receipt_operation": "operation_id", "receipt_run": "run_id"}[bad]:
            {"receipt_unknown": StartStatus.UNKNOWN, "receipt_busy": StartStatus.BUSY, "receipt_rejected": StartStatus.REJECTED, "receipt_operation": uuid4(), "receipt_run": uuid4()}[bad]
        })
        # Deliberately corrupt storage; normal constructors reject foreign receipts.
        object.__setattr__(s.link, "start_receipt", receipt)
    elif bad == "session_missing":
        del s.uow.sessions.values[s.session_id]
    elif bad == "session_id":
        del s.uow.sessions.values[s.session_id]
        session.id = uuid4()
        s.uow.sessions.values[session.id] = session
    elif bad == "session_execution":
        session.execution_id = uuid4()
    elif bad == "session_adapter":
        session.adapter_session_id = f"agent-run:{uuid4()}"
    elif bad == "session_closed":
        session.close(lost=False, now=NOW)
    elif bad == "transition_session":
        transition.target_session_id = uuid4()
    elif bad == "link_terminal":
        s.link = replace(s.link, status="failed")
    elif bad == "link_unknown":
        s.link = replace(s.link, status="unknown")
    elif bad == "phase":
        transition.phase = HandoffPhase.ACKNOWLEDGE_CONTEXT
    else:
        transition.status = HandoffTransitionStatus(bad)
    with pytest.raises(AgentError, match="Execution authority denied"):
        project_async(s)
    assert not s.native.reads and not s.outputs


@pytest.mark.parametrize("claim", ("released", "expired"))
@pytest.mark.parametrize("action", ("run.start", "run.input", "run.interrupt", "run.recover", "runtime.read"))
def test_admitted_projection_exception_cannot_issue_control_actions(claim, action):
    s = make_start_case(claim=claim)
    target = {"run.start": s.authority.profile_ref, "runtime.read": s.authority.runtime_ref}.get(action, s.authority.workspace_id)
    with pytest.raises(AgentError):
        s.reader.require(s.authority.id, action, target)


def test_original_dispatch_under_held_claim_keeps_preadmission_resolution():
    s = make_start_case(session=False, claim="held")
    s.link = replace(s.link, status="pending", run_id=None, start_receipt=None)
    for action, target in (
        ("run.start", s.authority.profile_ref), ("profile.read", s.authority.profile_ref),
        ("runtime.read", s.authority.runtime_ref),
        ("source.read", s.authority.sources[0]),
    ):
        s.reader.require(s.authority.id, action, target)


@pytest.mark.parametrize("claim", ("held", "released", "expired"))
@pytest.mark.parametrize("action_index", range(6))
def test_ack_scope_still_allows_only_source_read_under_a_held_claim(claim, action_index):
    s = make_start_case(claim=claim)
    transition = s.uow.handoff_transitions.item
    transition.phase = HandoffPhase.ACKNOWLEDGE_CONTEXT
    transition.target_session_id = s.session_id
    action, target = projection_targets(s)[action_index]
    if claim == "held" and action == "source.read":
        s.reader.require(s.authority.id, action, target)
    else:
        with pytest.raises(AgentError):
            s.reader.require(s.authority.id, action, target)


@pytest.mark.parametrize("bad", (
    "upstream", "host_revoked", "authority_revoked", "binding_profile", "binding_version",
    "binding_unrunnable", "link_profile", "link_version", "lease_missing", "lease_released",
    "lease_expired", "lease_fence", "execution_lease", "execution_fence", "workspace_path",
    "workspace_fence", "workspace_lifecycle", "source_stop", "source_freeze", "source_execution",
    "source_lease", "source_session", "source_operation", "source_fence",
))
def test_async_projection_preserves_authority_fences_and_real_source_cleanup(bad):
    s = make_start_case()
    authority, uow = s.authority, s.uow
    transition = uow.handoff_transitions.item
    execution = uow.executions.values[authority.execution_id]
    lease = uow.leases.values[authority.lease_id]
    manifest = uow.workspaces.values[authority.workspace_ref]
    if bad == "upstream":
        s.authority = replace(authority, upstream_grant_id=uuid4())
    elif bad == "host_revoked":
        s.host_allowed = False
    elif bad == "authority_revoked":
        s.authority = replace(authority, revoked=True)
    elif bad == "binding_profile":
        s.selected = replace(s.selected, profile_ref=authority.profile_ref.model_copy(update={"revision": 2}))
    elif bad == "binding_version":
        s.selected = replace(s.selected, version=2)
    elif bad == "binding_unrunnable":
        s.selected = replace(s.selected, runnable=False)
    elif bad == "link_profile":
        s.link = replace(s.link, profile_ref=authority.profile_ref.model_copy(update={"revision": 2}))
    elif bad == "link_version":
        s.link = replace(s.link, binding_version=2)
    elif bad == "lease_missing":
        del uow.leases.values[lease.id]
    elif bad == "lease_released":
        lease.status = LeaseStatus.RELEASED
    elif bad == "lease_expired":
        lease.expires_at = NOW
    elif bad == "lease_fence":
        lease.fencing_token = 2
    elif bad == "execution_lease":
        execution.active_lease_id = uuid4()
    elif bad == "execution_fence":
        execution.last_fencing_token = 2
    elif bad == "workspace_path":
        manifest.location_ref = "controlled://foreign"
    elif bad == "workspace_fence":
        manifest.fencing_token = 2
    elif bad == "workspace_lifecycle":
        manifest.lifecycle = WorkspaceLifecycle.FROZEN
    elif bad == "source_stop":
        transition.source_stop_facts = None
    elif bad == "source_freeze":
        transition.source_workspace_freeze_facts = None
    elif bad == "source_execution":
        uow.executions.values[transition.source_execution_id].status = ExecutionStatus.RUNNING
    elif bad == "source_lease":
        uow.leases.values[s.source.authority.lease_id].status = LeaseStatus.ACTIVE
    elif bad == "source_session":
        uow.sessions.values[transition.source_session_id].status = ExecutionSessionStatus.ACTIVE
    elif bad == "source_operation":
        uow.gateway_operations.values.clear()
    else:
        uow.workspaces.values[transition.source_workspace_manifest_id].fencing_token = 2
    with pytest.raises((AgentError, WorkspaceHandoffError)):
        project_async(s)
    assert not s.native.reads and not s.outputs


@pytest.mark.parametrize("action_index", range(6))
def test_between_claims_projection_retains_exact_action_target_allowlists(action_index):
    s = make_start_case()
    action, target = projection_targets(s)[action_index]
    changes = {
        "profile.read": {"revision": 2},
        "workspace.execute": {"directory": "/foreign"},
        "source.read": {"key": "foreign"},
        "tool.execute": {"connection_id": uuid4()},
        "secret.read": {"credential_ref": CredentialRef(workspace_id=s.authority.workspace_id, credential_id=uuid4())},
        "resource.read": {"revision": 2},
    }
    with pytest.raises(AgentError):
        s.reader.require(s.authority.id, action, target.model_copy(update=changes[action]))


@pytest.mark.parametrize("state", (RunState.STARTING, RunState.UNKNOWN, RunState.CLOSING))
def test_gateway_waits_for_same_original_run_to_open(state):
    s = make_start_case(session=False, claim="held")
    s.snapshot = s.snapshot.model_copy(update={"state": state})
    first = start_target(s)
    assert isinstance(first, NativeDeferredResult)
    assert (first.workspace_id, first.execution_id, first.start_operation_id, first.status) == (
        s.authority.workspace_id, s.authority.execution_id, s.link.start_operation_id, state.value
    )
    assert s.uow.handoff_transitions.item.target_session_id is None
    assert s.uow.sessions.values[s.session_id].status is ExecutionSessionStatus.ACTIVE
    s.snapshot = s.snapshot.model_copy(update={"state": RunState.RUNNING})
    second = start_target(s)
    assert second.session_id == s.session_id
    assert s.uow.handoff_transitions.item.target_session_id == s.session_id
    assert len(s.native.starts) == 1


@pytest.mark.parametrize("state", (RunState.RUNNING, RunState.IDLE))
def test_gateway_accepts_actual_open_proof_without_an_extra_event(state):
    s = make_start_case(session=False, claim="held")
    s.snapshot = s.snapshot.model_copy(update={"state": state})
    assert start_target(s).session_id == s.session_id
    assert len(s.native_events) == len(s.native.starts) == 1


@pytest.mark.parametrize("state", (RunState.FAILED, RunState.STOPPED, RunState.COMPLETED))
def test_gateway_classifies_exact_terminal_snapshot_as_known_negative(state):
    s = make_start_case(claim="held")
    s.snapshot = s.snapshot.model_copy(update={"state": state})
    with pytest.raises(GatewayMessageError) as error:
        start_target(s)
    assert error.value.code == "native_handoff_target_terminal"
    assert s.uow.handoff_transitions.item.target_session_id is None


@pytest.mark.parametrize("bad", ("workspace", "run", "profile", "effective_profile", "read_failure"))
def test_gateway_unconfirmed_snapshot_is_query_only_recovery(bad):
    s = make_start_case(claim="held")
    s.snapshot = s.snapshot.model_copy(update={"state": RunState.RUNNING})
    # The adapter's sync read is valid; corrupt only the subsequent open-proof read.
    original = s.native.get_run
    def get_run(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(s.native.reads) == 1:
            return result
        if bad == "read_failure":
            raise AgentError("unavailable", "Read unavailable")
        field = {"workspace": "workspace_id", "run": "run_id", "profile": "profile_ref", "effective_profile": "effective_config"}[bad]
        value = uuid4() if bad in {"workspace", "run"} else s.authority.profile_ref.model_copy(update={"revision": 2})
        if bad == "effective_profile":
            value = result.effective_config.model_copy(update={"profile_ref": value})
        return result.model_copy(update={field: value})
    s.native.get_run = get_run
    with pytest.raises(GatewayRecoveryRequired):
        start_target(s)
    assert s.uow.handoff_transitions.item.target_session_id is None
    assert len(s.native.starts) == 1


@pytest.mark.parametrize("bad", (
    "claim_owner", "claim_expiry", "link_run", "link_operation",
    "session_closed", "session_execution", "source_cleanup",
))
def test_gateway_revalidates_current_pins_after_public_snapshot_read(bad):
    s = make_start_case(claim="held")
    s.snapshot = s.snapshot.model_copy(update={"state": RunState.RUNNING})

    def change_after_proof():
        if len(s.native.reads) != 2:
            return
        transition = s.uow.handoff_transitions.item
        session = s.uow.sessions.values[s.session_id]
        if bad == "claim_owner":
            transition.lease_owner = "another-dispatch"
        elif bad == "claim_expiry":
            transition.lease_expires_at = NOW
        elif bad == "link_run":
            s.link = replace(s.link, run_id=uuid4())
        elif bad == "link_operation":
            receipt = s.link.start_receipt.model_copy(update={"operation_id": uuid4()})
            s.link = replace(s.link, start_operation_id=receipt.operation_id, start_receipt=receipt)
        elif bad == "session_closed":
            session.close(lost=False, now=NOW)
        elif bad == "session_execution":
            session.execution_id = uuid4()
        else:
            s.uow.leases.values[s.source.authority.lease_id].status = LeaseStatus.ACTIVE

    s.native.after_read = change_after_proof
    with pytest.raises((GatewayRecoveryRequired, WorkspaceHandoffError)):
        start_target(s)
    assert s.uow.handoff_transitions.item.target_session_id is None
    assert len(s.native.starts) == 1


@pytest.mark.parametrize("status", (ExecutionStatus.FAILED, ExecutionStatus.CANCELED, ExecutionStatus.SUCCEEDED))
@pytest.mark.parametrize("phase", (HandoffPhase.START_TARGET_SESSION, HandoffPhase.ACKNOWLEDGE_CONTEXT))
def test_known_cp_terminal_target_is_not_masked_by_missing_lease(status, phase):
    s = make_start_case(claim="held")
    transition = s.uow.handoff_transitions.item
    transition.phase = phase
    execution = s.uow.executions.values[s.authority.execution_id]
    execution.status, execution.active_lease_id = status, None
    with pytest.raises(GatewayMessageError) as error:
        s.gateway._load_handoff_target_context_in_uow(
            s.uow, transition=transition, workspace_id=transition.workspace_id,
            require_session=phase is HandoffPhase.ACKNOWLEDGE_CONTEXT,
            require_acknowledgement=False, allow_active_manifest=True,
        )
    assert error.value.code == "native_handoff_target_terminal"


@pytest.mark.parametrize("bad", ("legacy", "missing", "worker_item", "transition", "worker", "lost"))
def test_terminal_classification_requires_exact_schema2_target(bad):
    s = make_start_case(claim="held")
    transition = s.uow.handoff_transitions.item
    execution = s.uow.executions.values[s.authority.execution_id]
    execution.status, execution.active_lease_id = ExecutionStatus.FAILED, None
    if bad == "legacy":
        transition.handoff_schema_version = 1
    elif bad == "missing":
        del s.uow.executions.values[execution.id]
    elif bad == "worker_item":
        execution.worker_item_id = uuid4()
    elif bad == "transition":
        execution.handoff_transition_id = uuid4()
    elif bad == "worker":
        execution.assigned_worker_id = uuid4()
    else:
        execution.status = ExecutionStatus.LOST
    with pytest.raises(GatewayMessageError) as error:
        s.gateway._load_handoff_target_context_in_uow(
            s.uow, transition=transition, workspace_id=transition.workspace_id,
            require_session=False, require_acknowledgement=False, allow_active_manifest=True,
        )
    assert error.value.code != "native_handoff_target_terminal"


def test_gateway_classifies_target_closed_immediately_after_real_sync(monkeypatch):
    s = make_start_case(session=False, claim="held")
    original_sync = s.gateway.sync_session
    def sync_then_close(**kwargs):
        result = original_sync(**kwargs)
        execution = s.uow.executions.values[s.authority.execution_id]
        execution.status, execution.active_lease_id = ExecutionStatus.FAILED, None
        s.uow.sessions.values[s.session_id].close(lost=False, now=NOW)
        return result
    monkeypatch.setattr(s.gateway, "sync_session", sync_then_close)
    with pytest.raises(GatewayMessageError) as error:
        start_target(s)
    assert error.value.code == "native_handoff_target_terminal"


@pytest.mark.parametrize("phase", (HandoffPhase.START_TARGET_SESSION, HandoffPhase.ACKNOWLEDGE_CONTEXT))
@pytest.mark.parametrize("status", (ExecutionStatus.FAILED, ExecutionStatus.CANCELED, ExecutionStatus.SUCCEEDED))
def test_real_coordinator_fails_terminal_target_once_without_retry(phase, status):
    s = make_start_case(real_scheduler=True)
    transition = s.uow.handoff_transitions.item
    transition.phase = phase
    transition.target_session_id = s.session_id if phase is HandoffPhase.ACKNOWLEDGE_CONTEXT else None
    execution = s.uow.executions.values[s.authority.execution_id]
    execution.status, execution.active_lease_id = status, None
    s.uow.leases.values[s.authority.lease_id].status = LeaseStatus.RELEASED
    s.uow.sessions.values[s.session_id].close(lost=False, now=NOW)
    original_link, original_attempt = s.link, transition.logical_attempt
    original_sessions = set(s.uow.sessions.values)
    original_operations = set(s.uow.gateway_operations.values)
    first = s.coordinator.run_once(limit=1)
    stored = s.uow.handoff_transitions.item
    assert first.failed == 1 and first.blocked == 0
    assert stored.status is HandoffTransitionStatus.FAILED
    assert stored.failure_code == "native_handoff_target_terminal"
    assert stored.writer_state is HandoffWriterState.WRITER_NONE
    assert stored.next_attempt_at is None and stored.lease_owner is None
    assert stored.lease_expires_at is None and stored.logical_attempt == original_attempt
    assert s.coordinator.run_once(limit=1).claimed == 0
    assert s.link == original_link and len(s.native.starts) == 1
    assert len(s.uow.executions.values) == 2
    assert set(s.uow.sessions.values) == original_sessions
    assert set(s.uow.gateway_operations.values) == original_operations
    assert not s.native.reads
    failures = [event for event in s.source.events if event.event_type == "HandoffBlocked"]
    assert len(failures) == 1
    assert failures[0].payload["failure_code"] == "native_handoff_target_terminal"
    assert failures[0].payload["status"] == "failed"


@pytest.mark.parametrize("bad", ("worker_item", "transition", "worker", "missing"))
def test_coordinator_terminal_route_keeps_exact_target_identity_checks(bad):
    s = make_start_case(real_scheduler=True)
    execution = s.uow.executions.values[s.authority.execution_id]
    execution.status, execution.active_lease_id = ExecutionStatus.FAILED, None
    s.uow.leases.values[s.authority.lease_id].status = LeaseStatus.RELEASED
    if bad == "missing":
        del s.uow.executions.values[execution.id]
    else:
        field = {"worker_item": "worker_item_id", "transition": "handoff_transition_id", "worker": "assigned_worker_id"}[bad]
        setattr(execution, field, uuid4())
    result = s.coordinator.run_once(limit=1)
    stored = s.uow.handoff_transitions.item
    assert result.blocked == 1 and result.failed == 0
    assert stored.status is HandoffTransitionStatus.BLOCKED
    assert stored.failure_code == (
        "handoff_target_stale" if bad == "missing"
        else "handoff_target_worker_mismatch" if bad == "worker"
        else "handoff_target_execution_mismatch"
    )
    assert len(s.native.starts) == 1 and not s.native.reads


@pytest.mark.parametrize("bad", ("policy", "handoff_transition_id"))
def test_nonterminal_start_keeps_real_scheduler_reservation_validation(bad):
    s = make_start_case(real_scheduler=True)
    lease = s.uow.leases.values[s.authority.lease_id]
    lease.assignment_reason = {**lease.assignment_reason, bad: "foreign"}
    result = s.coordinator.run_once(limit=1)
    stored = s.uow.handoff_transitions.item
    assert result.blocked == 1 and result.progressed == 0
    assert stored.status is HandoffTransitionStatus.BLOCKED
    assert stored.failure_code == "handoff_target_stale"
    assert len(s.native.starts) == 1 and not s.native.reads


@pytest.mark.parametrize("kind", ("known", "unknown"))
def test_other_gateway_failures_keep_existing_coordinator_classification(kind):
    s = make_start_case()
    original_operation = s.link.start_operation_id
    if kind == "known":
        s.selected = replace(s.selected, runnable=False)
    else:
        s.native.read_error = GatewayRecoveryRequired("Read is unconfirmed")
    result = s.coordinator.run_once(limit=1)
    stored = s.uow.handoff_transitions.item
    if kind == "known":
        assert result.blocked == 1 and stored.status is HandoffTransitionStatus.BLOCKED
        assert stored.next_attempt_at == NOW + timedelta(seconds=1)
    else:
        assert result.unknown == 1 and stored.status is HandoffTransitionStatus.BLOCKED_UNKNOWN
        assert stored.blocked_operation_id == original_operation
        assert stored.next_attempt_at is None
    assert len(s.native.starts) == 1 and len(s.uow.executions.values) == 2


@pytest.mark.parametrize("case", ("legacy_start", "native_checkpoint"))
def test_terminal_error_code_does_not_fail_other_schema_or_phase(case):
    s = make_start_case()
    transition = s.uow.handoff_transitions.item

    def terminal(**kwargs):
        raise GatewayMessageError("Controlled terminal result", code="native_handoff_target_terminal")

    if case == "legacy_start":
        transition.handoff_schema_version = 1
        s.coordinator._gateway = SimpleNamespace(start_handoff_target=terminal)
    else:
        transition.phase = HandoffPhase.CHECKPOINT_SOURCE
        s.coordinator._native_handoff = SimpleNamespace(checkpoint=lambda current: terminal())
    result = s.coordinator.run_once(limit=1)
    assert result.blocked == 1 and result.failed == 0
    assert s.uow.handoff_transitions.item.status is HandoffTransitionStatus.BLOCKED


def test_ordered_async_projection_between_start_claims_advances_only_after_original_open():
    s = make_start_case(session=False)
    original_operation, original_run = s.link.start_operation_id, s.link.run_id
    assert s.session_id not in s.uow.sessions.values
    assert s.coordinator.run_once(limit=1).waiting == 1
    stored = s.uow.handoff_transitions.item
    assert stored.phase is HandoffPhase.START_TARGET_SESSION
    assert stored.target_session_id is None and stored.lease_owner is None
    assert s.uow.sessions.values[s.session_id].status is ExecutionSessionStatus.ACTIVE
    WorkspaceHandoffService._validate_native_cleanup(s.uow, stored)
    project_async(s)
    assert s.outputs == ["secret", "native_open"]
    s.snapshot = s.snapshot.model_copy(update={"state": RunState.RUNNING})
    assert s.coordinator.run_once(limit=1).progressed == 1
    stored = s.uow.handoff_transitions.item
    assert stored.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT
    assert stored.target_session_id == s.session_id
    assert s.link.start_operation_id == original_operation and s.link.run_id == original_run
    assert len(s.native.starts) == len(s.native_events) == 1
    assert len(s.uow.executions.values) == 2


@pytest.mark.parametrize("status", ("running", "waiting"))
def test_start_accepts_same_run_observation_progress_during_public_proof(status):
    s = make_start_case(session=False, claim="held")
    s.snapshot = s.snapshot.model_copy(update={"state": RunState.RUNNING})
    original_run, original_operation = s.link.run_id, s.link.start_operation_id

    def observe_during_proof():
        if len(s.native.reads) == 2:
            advance_observation(s, status=status)

    s.native.after_read = observe_during_proof
    assert start_target(s).session_id == s.session_id
    assert s.link.event_cursor == s.uow.sessions.values[s.session_id].last_sequence == 2
    assert s.link.status == status and s.link.run_id == original_run
    assert s.link.start_operation_id == original_operation and len(s.native.starts) == 1
    assert s.uow.handoff_transitions.item.target_session_id == s.session_id


@pytest.mark.parametrize("status", ("running", "waiting"))
def test_ack_records_real_prepared_confirmation_during_same_run_observation(status):
    s = make_ack_case()
    original_run, original_operation = s.link.run_id, s.link.start_operation_id
    before = len(s.native.reads)
    s.native.after_read = lambda: advance_observation(s, status=status)
    result = confirm_target(s)
    assert result.outcome is DeliveryOutcome.DELIVERED
    assert result.execution_session_id == s.session_id
    assert len(s.native.reads) == before + 1
    assert s.link.event_cursor == s.uow.sessions.values[s.session_id].last_sequence == 2
    assert len(s.source_checks) == 2
    proof = validate_native_handoff_confirmation(
        s.uow, s.uow.handoff_transitions.item, now=NOW,
    )
    assert proof["run_id"] == str(original_run)
    assert proof["start_operation_id"] == str(original_operation)
    assert proof["started_sequence"] == 1
    assert len(s.native.starts) == 1


@pytest.mark.parametrize("site", ("start", "ack"))
@pytest.mark.parametrize("bad", (
    "cursor_regression", "preparation", "request_fingerprint", "run", "operation",
    "receipt", "authority", "terminal", "unknown", "closing", "session_closed",
    "session_execution", "claim_expired", "claim_replaced", "source_cleanup",
))
def test_observation_progress_keeps_immutable_pins_and_live_fences_strict(site, bad):
    s = make_ack_case() if site == "ack" else make_start_case(session=False, claim="held")
    s.snapshot = s.snapshot.model_copy(update={"state": RunState.RUNNING})
    proof_read = len(s.native.reads) + (1 if site == "ack" else 2)

    def mutate_during_proof():
        if len(s.native.reads) != proof_read:
            return
        transition = s.uow.handoff_transitions.item
        if bad == "cursor_regression":
            s.link = replace(s.link, event_cursor=s.link.event_cursor - 1)
        elif bad == "preparation":
            s.link = replace(s.link, preparation={**s.link.preparation, "validated_bytes": 8})
        elif bad == "request_fingerprint":
            s.link = replace(s.link, request_sha256="b" * 64)
        elif bad == "run":
            s.link = replace(s.link, run_id=uuid4())
        elif bad == "operation":
            receipt = s.link.start_receipt.model_copy(update={"operation_id": uuid4()})
            s.link = replace(s.link, start_operation_id=receipt.operation_id, start_receipt=receipt)
        elif bad == "receipt":
            s.link = replace(s.link, start_receipt=s.link.start_receipt.model_copy(update={"reason_code": "changed"}))
        elif bad == "authority":
            s.link = replace(s.link, authority_id=uuid4())
        elif bad in {"terminal", "unknown", "closing"}:
            s.link = replace(s.link, status="failed" if bad == "terminal" else bad)
        elif bad == "session_closed":
            s.uow.sessions.values[s.session_id].close(lost=False, now=NOW)
        elif bad == "session_execution":
            s.uow.sessions.values[s.session_id].execution_id = uuid4()
        elif bad == "claim_expired":
            transition.lease_expires_at = NOW
        elif bad == "claim_replaced":
            transition.lease_owner = "replaced-claim"
        else:
            s.uow.leases.values[s.source.authority.lease_id].status = LeaseStatus.ACTIVE

    s.native.after_read = mutate_during_proof
    with pytest.raises((GatewayRecoveryRequired, GatewayMessageError, WorkspaceHandoffError)):
        (confirm_target if site == "ack" else start_target)(s)
    assert len(s.native.starts) == 1
    if site == "start":
        assert s.uow.handoff_transitions.item.target_session_id is None
    else:
        assert not any(event.event_type == "NativeHandoffContextConfirmed" for event in s.source.events)
