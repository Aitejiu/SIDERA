from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.fake_worker import (
    FakeAdapterError,
    FakeWorkerAdapter,
)
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    GatewayRecoveryRequired,
    WorkerGateway,
    _operation_id,
)
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextConsumerType,
    ContextPackageStatus,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.gateway import (
    AdapterSessionHandle,
    DeliveryOutcome,
    GatewayOperationStatus,
    GatewayOperationType,
    RuntimeModuleOperation,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffSourceChannel,
    HandoffTransition,
    HandoffTransitionStatus,
    HandoffWriterState,
)
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    CapabilityInput,
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
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
HASH = "a" * 64


class _Repository:
    def __init__(self, values=()):
        self.values = {getattr(value, "id", uuid4()): value for value in values}
        self.saved: list[tuple[object, int]] = []

    def get(self, value_id, workspace_id, *, for_update=False):
        del workspace_id, for_update
        return self.values.get(value_id)

    def get_any(self, value_id, workspace_id, *, for_update=False):
        return self.get(value_id, workspace_id, for_update=for_update)

    def save(self, value, previous_version):
        self.values[value.id] = value
        self.saved.append((value, previous_version))

    def add(self, value):
        self.values[value.id] = value

    def get_for_execution(self, execution_id, workspace_id, *, for_update=False):
        del workspace_id, for_update
        return next(
            (
                value
                for value in self.values.values()
                if getattr(value, "execution_id", None) == execution_id
            ),
            None,
        )


class _ExecutionRepository(_Repository):
    def get(self, execution_id, workspace_id, *, for_update=False):
        return super().get(execution_id, workspace_id, for_update=for_update)


class _SessionRepository(_Repository):
    def get_active_for_execution(self, execution_id, *, for_update=False):
        del for_update
        return next(
            (
                item
                for item in self.values.values()
                if item.execution_id == execution_id and item.status.value == "active"
            ),
            None,
        )


class _ModuleRepository:
    def __init__(self, definitions, instances):
        self.definitions = {item.id: item for item in definitions}
        self.instances = {item.id: item for item in instances}

    def get_definition(self, module_id, workspace_id):
        del workspace_id
        return self.definitions.get(module_id)

    def get_instance(self, module_id, workspace_id):
        del workspace_id
        return self.instances.get(module_id)


class _TransitionRepository:
    def __init__(self, transition):
        self.transition = transition

    def get(self, transition_id, workspace_id, *, for_update=False):
        del workspace_id, for_update
        return self.transition if self.transition.id == transition_id else None

    def get_active_for_worker_item(self, worker_item_id, workspace_id, *, for_update=False):
        del workspace_id, for_update
        if (
            self.transition.worker_item_id == worker_item_id
            and self.transition.status is not HandoffTransitionStatus.CONVERGED
        ):
            return self.transition
        return None

    def save(self, transition, previous_version):
        assert previous_version < transition.version
        self.transition = transition


class _OperationRepository:
    def __init__(self):
        self.operations = {}

    def get_by_operation_id(self, operation_id, workspace_id, *, for_update=False):
        del workspace_id, for_update
        return self.operations.get(operation_id)

    def add(self, operation):
        self.operations[operation.operation_id] = operation

    def save(self, operation, previous_version):
        assert previous_version < operation.version
        self.operations[operation.operation_id] = operation


class _CommandRepository:
    def __init__(self):
        self.commands = {}

    def find_by_id(self, workspace_id, command_id):
        return self.commands.get((workspace_id, command_id))

    def record_applied(self, command, request_hash, result):
        del request_hash
        self.commands[(command.workspace_id, command.id)] = result


class _ContextPackageRepository:
    def __init__(self, package, acknowledgement):
        self.package = package
        self.acknowledgement = acknowledgement

    def get_for_consumer(
        self,
        purpose,
        consumer_id,
        workspace_id,
        *,
        for_update=False,
        manifest_schema_version=None,
    ):
        del workspace_id, for_update, manifest_schema_version
        if purpose is ContextPurpose.EXECUTION and consumer_id == self.package.consumer_id:
            return self.package
        return None

    def get_ack(
        self,
        package_id,
        consumer_type,
        consumer_id,
        workspace_id,
        *,
        for_update=False,
    ):
        del workspace_id, for_update
        if (
            package_id == self.acknowledgement.package_id
            and consumer_type is ContextConsumerType.WORKER
            and consumer_id == self.acknowledgement.consumer_id
        ):
            return self.acknowledgement
        return None


class _Uow:
    def __init__(self, fixture):
        self.handoff_transitions = _TransitionRepository(fixture.transition)
        self.executions = _ExecutionRepository((fixture.execution,))
        self.worker_items = _Repository((fixture.worker_item,))
        self.execution_requests = SimpleNamespace(
            get_effective_by_execution_lineage=lambda *_args, **_kwargs: None
        )
        self.sessions = _SessionRepository((fixture.session,))
        self.leases = _Repository((fixture.lease,))
        self.workers = _Repository((fixture.worker,))
        self.tasks = _Repository((fixture.task,))
        self.bindings = _Repository((fixture.binding,))
        self.modules = _ModuleRepository(
            (fixture.adapter_definition, fixture.workspace_definition),
            (fixture.adapter_instance, fixture.workspace_instance),
        )
        self.workspaces = _Repository((fixture.manifest,))
        self.gateway_operations = _OperationRepository()
        self.context_packages = _ContextPackageRepository(
            fixture.package, fixture.acknowledgement
        )
        self.events = SimpleNamespace(append_with_outbox=lambda event: None)
        self.commands = _CommandRepository()
        self.plans = SimpleNamespace(get_head=lambda *_args, **_kwargs: None)
        self.results = SimpleNamespace(get_head=lambda *_args, **_kwargs: None)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback


class _Fixture:
    def __init__(self, *, adapter=None):
        self.workspace_id = uuid4()
        self.task = Task.create(
            task_id=uuid4(),
            workspace_id=self.workspace_id,
            title="Handoff source",
            description="Capture source state",
            acceptance_criteria=("source is exact",),
            priority=50,
            requested_by="unit",
        )
        self.worker = Worker.register(
            worker_id=uuid4(),
            workspace_id=self.workspace_id,
            adapter_type="fake",
            display_name="Source worker",
            capabilities=frozenset({"python"}),
            capacity=1,
            cost_per_hour_microunits=1,
            startup_latency_ms=1,
            now=NOW,
        )
        self.worker.reserve()
        self.execution_id = uuid4()
        worker_item_id = uuid4()
        self.worker_item = WorkerItem.create(
            item_id=worker_item_id,
            workspace_id=self.workspace_id,
            root_task_id=self.task.id,
            title="Source WorkerItem",
            responsibility="Execute the source task.",
            now=NOW,
        )
        self.lease = AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=self.workspace_id,
            execution_id=self.execution_id,
            worker_id=self.worker.id,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=30),
            fencing_token=4,
            assignment_reason={"source": "unit"},
        )
        self.execution = Execution.create(
            execution_id=self.execution_id,
            workspace_id=self.workspace_id,
            task_id=self.task.id,
            worker_item_id=worker_item_id,
            lineage_id=uuid4(),
            attempt=1,
            max_attempts=3,
            purpose="worker-item",
            capability_requirements=frozenset({"python"}),
            pinned_worker_id=self.worker.id,
            not_before=NOW - timedelta(minutes=1),
        )
        self.execution.assign(
            lease_id=self.lease.id,
            worker_id=self.worker.id,
            fencing_token=self.lease.fencing_token,
        )
        self.execution.start(
            lease_id=self.lease.id,
            fencing_token=self.lease.fencing_token,
            now=NOW - timedelta(seconds=30),
        )
        self.adapter_definition_id = uuid4()
        self.workspace_definition_id = uuid4()
        self.adapter_instance_id = uuid4()
        self.workspace_instance_id = uuid4()
        self.adapter_definition = ModuleDefinition(
            id=self.adapter_definition_id,
            workspace_id=self.workspace_id,
            module_type="worker_adapter",
            provider="fake",
            interface_version="sidera.worker-adapter.v1",
            config_schema_version=1,
            capabilities=FakeWorkerAdapter.capabilities(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        self.workspace_definition = ModuleDefinition(
            id=self.workspace_definition_id,
            workspace_id=self.workspace_id,
            module_type="workspace_provider",
            provider="fake-workspace",
            interface_version="sidera.workspace-provider.v1",
            config_schema_version=1,
            capabilities=WorkspaceProviderCapabilityProfile(
                isolation=SandboxCapability.FILESYSTEM,
                writable=True,
                git_worktree=True,
                stability=StabilityLevel.STABLE,
            ),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        self.adapter_instance = ModuleInstance(
            id=self.adapter_instance_id,
            workspace_id=self.workspace_id,
            definition_id=self.adapter_definition.id,
            instance_name="fake-source",
            config_version=1,
            config_namespace="fake",
            config={},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        self.workspace_instance = ModuleInstance(
            id=self.workspace_instance_id,
            workspace_id=self.workspace_id,
            definition_id=self.workspace_definition.id,
            instance_name="fake-workspace",
            config_version=1,
            config_namespace="workspace",
            config={
                "workspace.repository_ref": "repo://sidera",
                "workspace.base_revision": "a" * 40,
            },
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        self.binding = ExecutionBinding(
            id=uuid4(),
            workspace_id=self.workspace_id,
            execution_id=self.execution.id,
            worker_id=self.worker.id,
            adapter_module_id=self.adapter_instance.id,
            workspace_module_id=self.workspace_instance.id,
            adapter_config_version=1,
            workspace_config_version=1,
            capability_snapshot=self.adapter_definition.capabilities,
            lease_id=self.lease.id,
            fencing_token=self.lease.fencing_token,
            version=1,
        )
        self.manifest = ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=self.workspace_id,
            task_id=self.task.id,
            execution_id=self.execution.id,
            checkouts=(
                WorkspaceCheckout(
                    repository_ref="repo://sidera",
                    base_revision="a" * 40,
                    branch=f"sidera/execution/{self.execution.id}",
                    current_head="a" * 40,
                ),
            ),
            location_ref=f"fake-workspace://{self.execution.id}",
            retention_seconds=3600,
            fencing_token=self.lease.fencing_token,
            created_at=NOW,
        )
        self.manifest.mark_preparing()
        self.manifest.mark_ready()
        self.manifest.mark_active()
        self.package_id = uuid4()
        self.package = SimpleNamespace(
            package_id=self.package_id,
            package_hash=HASH,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=self.execution.id,
            worker_item_id=self.execution.worker_item_id,
            workspace_id=self.workspace_id,
            context_version=1,
            status=ContextPackageStatus.ACTIVE,
            expires_at=NOW + timedelta(hours=1),
        )
        self.acknowledgement = ContextAcknowledgement(
            package_id=self.package_id,
            package_hash=HASH,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=self.execution.id,
            context_version=1,
            adapter_module_id=self.adapter_instance.id,
            adapter_config_version=1,
            acknowledged_at=NOW,
        )
        self.adapter = adapter or FakeWorkerAdapter(
            handoff_context=(self.package_id, HASH, 1)
        )
        handle = self.adapter.start_session(
            SimpleNamespace(execution_id=self.execution.id),
            self.manifest,
            operation_id=uuid4(),
        )
        self.session = ExecutionSession.start(
            session_id=handle.execution_session_id,
            workspace_id=self.workspace_id,
            execution_id=self.execution.id,
            worker_id=self.worker.id,
            adapter_session_id=handle.adapter_session_id,
            now=NOW - timedelta(seconds=30),
            adapter_module_id=self.adapter_instance.id,
            adapter_config_version=1,
            workspace_module_id=self.workspace_instance.id,
            workspace_config_version=1,
            capability_snapshot=handle.capability_snapshot,
            process_ref=handle.process_ref,
            workspace_ref=self.workspace_instance.instance_name,
        )
        self.session.input_mode = CapabilityInput.QUEUED.value
        self.transition = HandoffTransition(
            id=uuid4(),
            workspace_id=self.workspace_id,
            worker_item_id=self.execution.worker_item_id,
            proposal_id=uuid4(),
            source_execution_id=self.execution.id,
            source_session_id=self.session.id,
            source_workspace_manifest_id=self.manifest.id,
            target_worker_id=uuid4(),
            target_runtime_snapshot={},
            target_execution_id=None,
            target_session_id=None,
            target_workspace_manifest_id=None,
            handoff_context_bundle_id=None,
            target_context_version=1,
            target_context_package_id=None,
            target_context_package_hash=None,
            status=HandoffTransitionStatus.RUNNING,
            phase=HandoffPhase.CHECKPOINT_SOURCE,
            writer_state=HandoffWriterState.SOURCE_ACTIVE,
            source_checkpoint_sequence=None,
            source_checkpoint_facts=None,
            source_stop_sequence=None,
            source_stop_facts=None,
            source_workspace_freeze_operation_id=None,
            source_workspace_freeze_facts=None,
            source_input_cutoff_sequence=0,
            failure_code=None,
            blocked_operation_id=None,
            logical_attempt=1,
            lease_owner="coordinator",
            lease_expires_at=NOW + timedelta(minutes=5),
            next_attempt_at=None,
            version=2,
            confirmed_by_type="user",
            confirmed_by_id="unit",
            confirmed_source_channel=HandoffSourceChannel.WEB,
            confirmed_at=NOW - timedelta(minutes=1),
            started_at=NOW - timedelta(minutes=1),
            completed_at=None,
        )
        self.uow = _Uow(self)


def _gateway(fixture: _Fixture) -> WorkerGateway:
    return WorkerGateway(
        lambda: fixture.uow,
        adapter_factories={"fake": lambda _instance: fixture.adapter},
        workspace_factories={},
        clock=lambda: NOW,
    )


def test_fake_source_checkpoint_drains_events_and_fences_normal_activity() -> None:
    fixture = _Fixture()
    handle = AdapterSessionHandle(
        execution_session_id=fixture.session.id,
        adapter_session_id=f"fake-session-{fixture.execution.id}",
        process_ref=f"fake://{fixture.execution.id}",
        capability_snapshot=fixture.adapter.capabilities(),
        last_accepted_sequence=0,
        started_at=NOW,
    )
    checkpoint = fixture.adapter.capture_handoff_checkpoint(
        handle,
        transition_id=fixture.transition.id,
        source_input_cutoff_sequence=0,
        operation_id=uuid4(),
    )

    assert checkpoint.quiescent is True
    assert checkpoint.accepted_sequence == 0
    assert fixture.adapter.pending_events(handle) == ()
    blocked = fixture.adapter.send_input(
        handle, "after checkpoint", (), operation_id=uuid4()
    )
    assert blocked.outcome is DeliveryOutcome.BLOCKED
    assert blocked.failure_code == "handoff_checkpointed"
    with pytest.raises(FakeAdapterError, match="handoff_checkpointed"):
        fixture.adapter.release_boundary(handle)


def test_fake_source_checkpoint_requires_an_observed_cutoff() -> None:
    fixture = _Fixture()
    handle = fixture.adapter.active_sessions()[0]

    with pytest.raises(FakeAdapterError, match="handoff_checkpoint_sequence_stale"):
        fixture.adapter.capture_handoff_checkpoint(
            handle,
            transition_id=fixture.transition.id,
            source_input_cutoff_sequence=1,
            operation_id=uuid4(),
        )

    accepted = fixture.adapter.send_input(
        handle, "progress", (), operation_id=uuid4()
    )
    assert accepted.accepted_sequence == 2
    fixture.adapter.release_boundary(handle)
    checkpoint = fixture.adapter.capture_handoff_checkpoint(
        handle,
        transition_id=fixture.transition.id,
        source_input_cutoff_sequence=2,
        operation_id=uuid4(),
    )
    assert checkpoint.accepted_sequence == 2


def test_gateway_rejects_workspace_and_context_identity_mismatches() -> None:
    workspace_fixture = _Fixture(
        adapter=FakeWorkerAdapter(handoff_workspace_digest="b" * 64)
    )
    with pytest.raises(GatewayMessageError) as workspace_error:
        _gateway(workspace_fixture).capture_handoff_source(
            workspace_id=workspace_fixture.workspace_id,
            transition_id=workspace_fixture.transition.id,
            expected_transition_version=workspace_fixture.transition.version,
        )
    assert workspace_error.value.code == "handoff_checkpoint_workspace_mismatch"

    context_fixture = _Fixture(
        adapter=FakeWorkerAdapter(
            handoff_context=(uuid4(), HASH, 1),
        )
    )
    with pytest.raises(GatewayMessageError) as context_error:
        _gateway(context_fixture).capture_handoff_source(
            workspace_id=context_fixture.workspace_id,
            transition_id=context_fixture.transition.id,
            expected_transition_version=context_fixture.transition.version,
        )
    assert context_error.value.code == "handoff_checkpoint_context_mismatch"


def test_gateway_captures_exact_checkpoint_and_reuses_it_after_restart() -> None:
    fixture = _Fixture()
    gateway = _gateway(fixture)
    first = gateway.capture_handoff_source(
        workspace_id=fixture.workspace_id,
        transition_id=fixture.transition.id,
        expected_transition_version=fixture.transition.version,
    )
    calls_after_first = fixture.adapter.checkpoint_call_count

    restarted = _gateway(fixture)
    second = restarted.capture_handoff_source(
        workspace_id=fixture.workspace_id,
        transition_id=fixture.transition.id,
        expected_transition_version=fixture.transition.version,
    )

    assert second == first
    assert fixture.adapter.checkpoint_call_count == calls_after_first
    operation = fixture.uow.gateway_operations.operations[first.operation_id]
    assert operation.status is GatewayOperationStatus.SUCCEEDED
    assert fixture.transition.source_checkpoint_sequence == first.accepted_sequence


def test_gateway_rejects_non_quiescent_checkpoint_and_workspace_mismatch() -> None:
    fixture = _Fixture(
        adapter=FakeWorkerAdapter(
            handoff_context=(fixture_package_id := uuid4(), HASH, 1),
            handoff_workspace_digest="b" * 64,
        )
    )
    fixture.package.package_id = fixture_package_id
    fixture.acknowledgement = ContextAcknowledgement(
        package_id=fixture_package_id,
        package_hash=HASH,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=fixture.execution.id,
        context_version=1,
        adapter_module_id=fixture.adapter_instance.id,
        adapter_config_version=1,
        acknowledged_at=NOW,
    )
    fixture.uow.context_packages = _ContextPackageRepository(
        fixture.package, fixture.acknowledgement
    )
    fixture.adapter.inject_handoff_checkpoint(
        quiescent=False, workspace_digest="b" * 64
    )

    with pytest.raises(GatewayMessageError) as caught:
        _gateway(fixture).capture_handoff_source(
            workspace_id=fixture.workspace_id,
            transition_id=fixture.transition.id,
            expected_transition_version=fixture.transition.version,
        )
    assert caught.value.code == "handoff_checkpoint_not_quiescent"


def test_gateway_stop_requires_checkpoint_session_and_sequence_and_closes_source() -> None:
    fixture = _Fixture()
    gateway = _gateway(fixture)
    checkpoint = gateway.capture_handoff_source(
        workspace_id=fixture.workspace_id,
        transition_id=fixture.transition.id,
        expected_transition_version=fixture.transition.version,
    )
    fixture.transition.advance(HandoffPhase.STOP_SOURCE, now=NOW)
    result = gateway.stop_handoff_source(
        workspace_id=fixture.workspace_id,
        transition_id=fixture.transition.id,
        expected_transition_version=fixture.transition.version,
    )

    assert result.source_session_id == checkpoint.source_session_id
    assert result.accepted_sequence == checkpoint.accepted_sequence
    assert fixture.session.status.value == "closed"
    assert fixture.execution.status.value == "canceled"
    assert fixture.transition.source_stop_sequence == checkpoint.accepted_sequence


def test_gateway_stop_timeout_is_unknown_and_keeps_source_runtime() -> None:
    fixture = _Fixture()
    fixture.adapter.inject_failure("stop_session", "timeout")
    gateway = _gateway(fixture)
    gateway.capture_handoff_source(
        workspace_id=fixture.workspace_id,
        transition_id=fixture.transition.id,
        expected_transition_version=fixture.transition.version,
    )
    fixture.transition.advance(HandoffPhase.STOP_SOURCE, now=NOW)

    with pytest.raises(GatewayRecoveryRequired):
        gateway.stop_handoff_source(
            workspace_id=fixture.workspace_id,
            transition_id=fixture.transition.id,
            expected_transition_version=fixture.transition.version,
        )

    operation_id = _operation_id(
        fixture.execution.id, GatewayOperationType.STOP_SESSION
    )
    assert (
        fixture.uow.gateway_operations.operations[operation_id].status
        is GatewayOperationStatus.UNKNOWN
    )
    assert fixture.session.status.value == "active"
    assert fixture.execution.status.value == "running"


def test_fake_stop_accepts_only_exact_session_and_sequence_and_reuses_operation() -> None:
    fixture = _Fixture()
    handle = AdapterSessionHandle(
        execution_session_id=fixture.session.id,
        adapter_session_id=f"fake-session-{fixture.execution.id}",
        process_ref=f"fake://{fixture.execution.id}",
        capability_snapshot=fixture.adapter.capabilities(),
        last_accepted_sequence=0,
        started_at=NOW,
    )
    checkpoint = fixture.adapter.capture_handoff_checkpoint(
        handle,
        transition_id=fixture.transition.id,
        source_input_cutoff_sequence=0,
        operation_id=uuid4(),
    )
    operation_id = _operation_id(fixture.execution.id, GatewayOperationType.STOP_SESSION)
    first = fixture.adapter.stop_session(handle, operation_id=operation_id)
    second = fixture.adapter.stop_session(handle, operation_id=operation_id)

    assert first.outcome is DeliveryOutcome.DELIVERED
    assert first.accepted_sequence == checkpoint.accepted_sequence
    assert first.execution_session_id == fixture.session.id
    assert second == first
    with pytest.raises(FakeAdapterError, match="handoff_stop_identity_mismatch"):
        fixture.adapter.stop_session(handle, operation_id=uuid4())


def test_unknown_checkpoint_is_inspected_and_never_replayed() -> None:
    fixture = _Fixture()
    gateway = _gateway(fixture)
    operation_id = handoff_step_operation_id(
        fixture.transition.id,
        HandoffPhase.CHECKPOINT_SOURCE,
        logical_attempt=fixture.transition.logical_attempt,
        action="checkpoint",
    )
    with fixture.uow:
        context = gateway._load_context_by_ids_in_uow(  # type: ignore[attr-defined]
            fixture.uow,
            workspace_id=fixture.workspace_id,
            execution_id=fixture.execution.id,
            lease_id=fixture.lease.id,
            fencing_token=fixture.lease.fencing_token,
            operation=RuntimeModuleOperation.OBSERVE,
        )
        gateway._begin_operation(  # type: ignore[attr-defined]
            fixture.uow,
            context,
            operation_id=operation_id,
            operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
            session_id=fixture.session.id,
        )
        operation_row = fixture.uow.gateway_operations.operations[operation_id]
        operation_row.mark_unknown(failure_code="process_uncertain", now=NOW)

    with pytest.raises(GatewayRecoveryRequired):
        gateway.capture_handoff_source(
            workspace_id=fixture.workspace_id,
            transition_id=fixture.transition.id,
            expected_transition_version=fixture.transition.version,
        )
    assert fixture.adapter.inspect_call_count == 1
    assert fixture.adapter.checkpoint_call_count == 0
