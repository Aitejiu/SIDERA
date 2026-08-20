from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.event_ingestor import (
    ArtifactRegistrationReport,
    ArtifactRegistry,
    EventIngestionReport,
    SessionEventIngestor,
)
from sidera_control_plane.application.outbox_dispatcher import (
    OutboxMessage,
    OutboxStore,
)
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkerAdapter,
    WorkspaceProvider,
)
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
)
from sidera_control_plane.domain.gateway import (
    AdapterSessionHandle,
    DeliveryOutcome,
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
)
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    ExecutionBinding,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceLifecycle,
)

ASSIGNMENT_TOPIC = "execution.ExecutionLeased"


class GatewayError(RuntimeError):
    """The Gateway cannot safely process an assignment message."""


class GatewayMessageError(GatewayError):
    """The assignment message is malformed or targets an unavailable resource."""


class GatewayRecoveryRequired(GatewayError):
    """A prior external side effect has no safe in-process retry path."""


class ExecutionSpecFactory(Protocol):
    def create(
        self,
        execution: Execution,
        task: Task,
        binding: ExecutionBinding,
        adapter_instance: ModuleInstance,
        workspace_instance: ModuleInstance,
    ) -> ExecutionSpec:
        raise NotImplementedError


class GatewayHandler(Protocol):
    def handle(self, message: OutboxMessage) -> object:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class GatewayExecutionResult:
    execution_id: UUID
    workspace_id: UUID
    session_id: UUID
    adapter_session_id: str


@dataclass(frozen=True, slots=True)
class GatewaySessionSyncResult:
    event_report: EventIngestionReport
    artifact_report: ArtifactRegistrationReport


@dataclass(frozen=True, slots=True)
class _AssignmentContext:
    execution: Execution
    task: Task
    lease: AssignmentLease
    binding: ExecutionBinding
    adapter_definition: ModuleDefinition
    adapter_instance: ModuleInstance
    workspace_definition: ModuleDefinition
    workspace_instance: ModuleInstance


@dataclass(frozen=True, slots=True)
class _OperationState:
    operation_id: UUID
    completed: bool
    recovery_required: bool = False


class ConfiguredExecutionSpecFactory:
    """Build the R3 single-repository spec from a workspace module snapshot."""

    def create(
        self,
        execution: Execution,
        task: Task,
        binding: ExecutionBinding,
        adapter_instance: ModuleInstance,
        workspace_instance: ModuleInstance,
    ) -> ExecutionSpec:
        config = workspace_instance.config
        repository_ref = _required_config(config, workspace_instance.config_namespace, "repository_ref")
        base_revision = _required_config(config, workspace_instance.config_namespace, "base_revision")
        writable_paths = _config_paths(
            config,
            workspace_instance.config_namespace,
            "writable_paths",
            default=(".",),
        )
        readable_paths = _config_paths(
            config,
            workspace_instance.config_namespace,
            "readable_paths",
            default=writable_paths,
        )
        checkout_mode = CheckoutMode(
            _config_text(
                config,
                workspace_instance.config_namespace,
                "checkout_mode",
                CheckoutMode.WORKTREE.value,
            )
        )
        branch_policy = BranchPolicy(
            _config_text(
                config,
                workspace_instance.config_namespace,
                "branch_policy",
                BranchPolicy.EXECUTION.value,
            )
        )
        expected_outputs = _config_paths(
            config,
            workspace_instance.config_namespace,
            "expected_outputs",
            default=("diff",),
        )
        return ExecutionSpec(
            schema_version=1,
            workspace_id=execution.workspace_id,
            task_id=execution.task_id,
            execution_id=execution.id,
            worker_id=binding.worker_id,
            lease_id=binding.lease_id,
            fencing_token=binding.fencing_token,
            goal=task.description or task.title,
            acceptance_criteria=task.acceptance_criteria,
            context_package_ref=_config_optional_text(
                config, workspace_instance.config_namespace, "context_package_ref"
            ),
            repositories=(
                RepositoryCheckoutSpec(
                    repository_ref=repository_ref,
                    base_revision=base_revision,
                    checkout_mode=checkout_mode,
                    writable_paths=writable_paths,
                    readable_paths=readable_paths,
                    branch_policy=branch_policy,
                ),
            ),
            tool_policy=_config_mapping(
                config, workspace_instance.config_namespace, "tool_policy", {}
            ),
            sandbox_policy=_config_mapping(
                config,
                workspace_instance.config_namespace,
                "sandbox_policy",
                {"mode": "filesystem"},
            ),
            budget=_config_mapping(
                config,
                workspace_instance.config_namespace,
                "budget",
                {"max_seconds": 3_600},
            ),
            deadline=None,
            expected_outputs=expected_outputs,
            adapter_module_ref=binding.adapter_module_id,
            adapter_config_version=binding.adapter_config_version,
            workspace_module_ref=binding.workspace_module_id,
            workspace_config_version=binding.workspace_config_version,
        )


class WorkerGateway:
    """Durable orchestration boundary for one Scheduler assignment."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        adapter_factories: Mapping[str, Callable[[ModuleInstance], WorkerAdapter]],
        workspace_factories: Mapping[
            str, Callable[[ModuleInstance], WorkspaceProvider]
        ],
        spec_factory: ExecutionSpecFactory | None = None,
        event_ingestor: SessionEventIngestor | None = None,
        artifact_registry: ArtifactRegistry | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._adapter_factories = dict(adapter_factories)
        self._workspace_factories = dict(workspace_factories)
        self._spec_factory = spec_factory or ConfiguredExecutionSpecFactory()
        self._clock = clock
        self._adapter_instances: dict[UUID, WorkerAdapter] = {}
        self._event_ingestor = event_ingestor or SessionEventIngestor(
            uow_factory, clock=clock
        )
        self._artifact_registry = artifact_registry or ArtifactRegistry(
            uow_factory, clock=clock
        )

    def handle(self, message: OutboxMessage) -> GatewayExecutionResult:
        context = self._load_context(message)
        spec = self._spec_factory.create(
            context.execution,
            context.task,
            context.binding,
            context.adapter_instance,
            context.workspace_instance,
        )
        workspace = self._ensure_workspace(context, spec)
        result = self._ensure_session(context, spec, workspace)
        self._ensure_initial_prompt(context, spec, result)
        self.sync_session(
            workspace_id=context.execution.workspace_id,
            execution_id=context.execution.id,
            session_id=result.session_id,
            lease_id=context.lease.id,
            fencing_token=context.binding.fencing_token,
        )
        return result

    def _load_context(self, message: OutboxMessage) -> _AssignmentContext:
        if message.topic != ASSIGNMENT_TOPIC:
            raise GatewayMessageError(f"unsupported Gateway topic: {message.topic}")
        workspace_id, execution_id, lease_id, fencing_token = _assignment_ids(message)
        return self._load_context_by_ids(
            workspace_id=workspace_id,
            execution_id=execution_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
        )

    def _load_context_by_ids(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        lease_id: UUID,
        fencing_token: int,
    ) -> _AssignmentContext:
        with self._uow_factory() as uow:
            execution = uow.executions.get(execution_id, workspace_id, for_update=True)
            if execution is None:
                raise GatewayMessageError("assigned execution was not found")
            binding = uow.bindings.get_for_execution(
                execution_id, workspace_id, for_update=True
            )
            if binding is None:
                raise GatewayMessageError("execution binding was not found")
            lease = uow.leases.get(lease_id, workspace_id, for_update=True)
            if lease is None:
                raise GatewayMessageError("assignment lease was not found")
            task = uow.tasks.get(execution.task_id, workspace_id)
            if task is None:
                raise GatewayMessageError("assigned task was not found")
            if binding.lease_id != lease_id or binding.fencing_token != fencing_token:
                raise GatewayMessageError("assignment binding fence does not match event")
            if binding.execution_id != execution_id:
                raise GatewayMessageError("assignment binding targets another execution")
            if execution.assigned_worker_id != binding.worker_id:
                raise GatewayMessageError("execution worker does not match binding")
            if execution.active_lease_id != lease_id:
                raise GatewayMessageError("execution lease does not match event")
            execution.assert_fence(lease_id=lease_id, fencing_token=fencing_token)
            lease.assert_owner(binding.worker_id, fencing_token, self._clock())
            adapter_instance, adapter_definition = _load_module(
                uow,
                binding.adapter_module_id,
                workspace_id,
                expected_type="worker_adapter",
            )
            workspace_instance, workspace_definition = _load_module(
                uow,
                binding.workspace_module_id,
                workspace_id,
                expected_type="workspace_provider",
            )
            if binding.capability_snapshot != adapter_definition.capabilities:
                raise GatewayMessageError("binding capability snapshot is stale")
            return _AssignmentContext(
                execution=execution,
                task=task,
                lease=lease,
                binding=binding,
                adapter_definition=adapter_definition,
                adapter_instance=adapter_instance,
                workspace_definition=workspace_definition,
                workspace_instance=workspace_instance,
            )

    def sync_session(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        session_id: UUID,
        lease_id: UUID,
        fencing_token: int,
    ) -> GatewaySessionSyncResult:
        context = self._load_context_by_ids(
            workspace_id=workspace_id,
            execution_id=execution_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
        )
        with self._uow_factory() as uow:
            session = uow.sessions.get_active_for_execution(
                execution_id, for_update=True
            )
            if session is None or session.id != session_id:
                raise GatewayMessageError("active session does not match sync request")
            handle = _session_handle(session)
        adapter = self._adapter(context.adapter_definition, context.adapter_instance)
        adapter_events = tuple(adapter.read_events(handle))
        event_report = self._event_ingestor.ingest(
            workspace_id=workspace_id,
            execution_id=execution_id,
            session_id=session_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            events=adapter_events,
        )
        candidates = adapter.collect_artifacts(
            handle,
            operation_id=uuid5(
                NAMESPACE_URL, f"sidera.gateway.collect_artifacts:{session_id}"
            ),
        )
        artifact_report = self._artifact_registry.register(
            workspace_id=workspace_id,
            task_id=context.task.id,
            execution_id=execution_id,
            session_id=session_id,
            candidates=candidates,
            acceptance_criteria=context.task.acceptance_criteria,
            producer=context.adapter_instance.instance_name,
            environment_ref=context.workspace_instance.instance_name,
        )
        return GatewaySessionSyncResult(event_report, artifact_report)

    def _ensure_workspace(
        self,
        context: _AssignmentContext,
        spec: ExecutionSpec,
    ) -> ExecutionWorkspaceManifest:
        provider = self._workspace_provider(
            context.workspace_definition, context.workspace_instance
        )
        operation_id = _operation_id(
            context.execution.id, GatewayOperationType.PREPARE_WORKSPACE
        )
        with self._uow_factory() as uow:
            manifest = uow.workspaces.get_for_execution(
                context.execution.id, context.execution.workspace_id, for_update=True
            )
            operation_state = self._begin_operation(
                uow,
                context,
                operation_id=operation_id,
                operation_type=GatewayOperationType.PREPARE_WORKSPACE,
                session_id=None,
            )
            if operation_state.completed:
                if manifest is None:
                    raise GatewayRecoveryRequired(
                        "workspace operation succeeded without a persisted manifest"
                    )
                return manifest
            if manifest is not None and manifest.lifecycle not in {
                WorkspaceLifecycle.REQUESTED,
                WorkspaceLifecycle.PREPARING,
            }:
                self._succeed_operation(
                    uow, operation_id, context, manifest.location_ref
                )
                return manifest

        if operation_state.recovery_required:
            raise GatewayRecoveryRequired(
                f"Gateway operation {operation_id} requires reconciliation"
            )
        try:
            manifest = provider.prepare(spec, operation_id=operation_id)
            _validate_manifest(context, manifest)
            if manifest.lifecycle is WorkspaceLifecycle.REQUESTED:
                manifest.mark_preparing()
                manifest.mark_ready()
                manifest.mark_active()
            self._persist_workspace_success(context, manifest, operation_id)
            return manifest
        except Exception as exc:
            self._fail_operation(operation_id, context, _failure_code(exc))
            raise GatewayError("workspace preparation failed") from exc

    def _ensure_session(
        self,
        context: _AssignmentContext,
        spec: ExecutionSpec,
        workspace: ExecutionWorkspaceManifest,
    ) -> GatewayExecutionResult:
        adapter = self._adapter(context.adapter_definition, context.adapter_instance)
        operation_id = _operation_id(
            context.execution.id, GatewayOperationType.START_SESSION
        )
        with self._uow_factory() as uow:
            existing_session = uow.sessions.get_active_for_execution(
                context.execution.id, for_update=True
            )
            operation_state = self._begin_operation(
                uow,
                context,
                operation_id=operation_id,
                operation_type=GatewayOperationType.START_SESSION,
                session_id=existing_session.id if existing_session else None,
            )
            if operation_state.completed:
                if existing_session is None:
                    raise GatewayRecoveryRequired(
                        "session operation succeeded without a persisted session"
                    )
                return GatewayExecutionResult(
                    execution_id=context.execution.id,
                    workspace_id=context.execution.workspace_id,
                    session_id=existing_session.id,
                    adapter_session_id=existing_session.adapter_session_id,
                )
            if existing_session is not None:
                self._succeed_operation(
                    uow,
                    operation_id,
                    context,
                    existing_session.adapter_session_id,
                    session_id=existing_session.id,
                )
                return GatewayExecutionResult(
                    execution_id=context.execution.id,
                    workspace_id=context.execution.workspace_id,
                    session_id=existing_session.id,
                    adapter_session_id=existing_session.adapter_session_id,
                )

        if operation_state.recovery_required:
            self._mark_operation_unknown(
                operation_id,
                context,
                "gateway_restart_requires_reconciliation",
            )
            raise GatewayRecoveryRequired(
                f"Gateway operation {operation_id} requires reconciliation"
            )

        try:
            handle = adapter.start_session(spec, workspace, operation_id=operation_id)
            result = self._persist_session_success(context, handle, operation_id)
            return result
        except Exception as exc:
            self._fail_operation(operation_id, context, _failure_code(exc))
            raise GatewayError("session startup failed") from exc

    def _ensure_initial_prompt(
        self,
        context: _AssignmentContext,
        spec: ExecutionSpec,
        session_result: GatewayExecutionResult,
    ) -> None:
        adapter = self._adapter(context.adapter_definition, context.adapter_instance)
        operation_id = _operation_id(
            context.execution.id, GatewayOperationType.SEND_INPUT
        )
        with self._uow_factory() as uow:
            session = uow.sessions.get_active_for_execution(
                context.execution.id, for_update=True
            )
            if session is None or session.id != session_result.session_id:
                raise GatewayRecoveryRequired(
                    "session operation succeeded without an active session"
                )
            operation_state = self._begin_operation(
                uow,
                context,
                operation_id=operation_id,
                operation_type=GatewayOperationType.SEND_INPUT,
                session_id=session.id,
            )
            if operation_state.completed:
                return
            if operation_state.recovery_required:
                handle = None
            else:
                handle = _session_handle(session)

        if operation_state.recovery_required:
            self._mark_operation_unknown(
                operation_id,
                context,
                "gateway_restart_requires_reconciliation",
            )
            raise GatewayRecoveryRequired(
                f"Gateway operation {operation_id} requires reconciliation"
            )
        if handle is None:
            raise GatewayRecoveryRequired("active session handle is unavailable")
        try:
            delivery = adapter.send_input(
                handle,
                _initial_prompt(spec),
                (),
                operation_id=operation_id,
            )
            if delivery.operation_id != operation_id:
                raise GatewayError("adapter returned a mismatched input operation id")
            if delivery.outcome is DeliveryOutcome.DELIVERED:
                with self._uow_factory() as uow:
                    self._succeed_operation(
                        uow,
                        operation_id,
                        context,
                        f"sequence:{delivery.accepted_sequence}",
                        session_id=session_result.session_id,
                    )
                return
            failure_code = delivery.failure_code or delivery.outcome.value
            if delivery.outcome is DeliveryOutcome.UNKNOWN:
                self._mark_operation_unknown(operation_id, context, failure_code)
                raise GatewayRecoveryRequired(
                    f"initial prompt delivery is unknown: {failure_code}"
                )
            self._fail_operation(operation_id, context, failure_code)
            raise GatewayError(f"initial prompt delivery failed: {failure_code}")
        except GatewayRecoveryRequired:
            raise
        except Exception as exc:
            self._fail_operation(operation_id, context, _failure_code(exc))
            raise GatewayError("initial prompt delivery failed") from exc

    def _begin_operation(
        self,
        uow: ControlPlaneUnitOfWork,
        context: _AssignmentContext,
        *,
        operation_id: UUID,
        operation_type: GatewayOperationType,
        session_id: UUID | None,
    ) -> _OperationState:
        now = self._clock()
        operation = uow.gateway_operations.get_by_operation_id(
            operation_id, context.execution.workspace_id, for_update=True
        )
        if operation is None:
            operation = GatewayOperation.create(
                id=uuid5(NAMESPACE_URL, f"sidera.gateway.row:{operation_id}"),
                operation_id=operation_id,
                workspace_id=context.execution.workspace_id,
                task_id=context.execution.task_id,
                execution_id=context.execution.id,
                session_id=session_id,
                operation_type=operation_type,
                module_instance_id=(
                    context.workspace_instance.id
                    if operation_type is GatewayOperationType.PREPARE_WORKSPACE
                    else context.adapter_instance.id
                ),
                config_version=(
                    context.binding.workspace_config_version
                    if operation_type is GatewayOperationType.PREPARE_WORKSPACE
                    else context.binding.adapter_config_version
                ),
                lease_id=context.lease.id,
                fencing_token=context.binding.fencing_token,
                now=now,
            )
            operation.start(now=now)
            uow.gateway_operations.add(operation)
            return _OperationState(operation_id=operation_id, completed=False)
        if operation.status is GatewayOperationStatus.SUCCEEDED:
            return _OperationState(operation_id=operation_id, completed=True)
        if operation.status is GatewayOperationStatus.RUNNING:
            return _OperationState(operation_id=operation_id, completed=False, recovery_required=True)
        if operation.status is GatewayOperationStatus.UNKNOWN:
            return _OperationState(operation_id=operation_id, completed=False, recovery_required=True)
        previous_version = operation.version
        if operation.status is GatewayOperationStatus.FAILED:
            operation.retry(now=now)
        operation.start(now=now)
        uow.gateway_operations.save(operation, previous_version)
        return _OperationState(operation_id=operation_id, completed=False)

    def _persist_workspace_success(
        self,
        context: _AssignmentContext,
        manifest: ExecutionWorkspaceManifest,
        operation_id: UUID,
    ) -> None:
        with self._uow_factory() as uow:
            existing = uow.workspaces.get_for_execution(
                context.execution.id, context.execution.workspace_id, for_update=True
            )
            if existing is None:
                uow.workspaces.add(manifest)
            else:
                previous_version = existing.version
                uow.workspaces.save(manifest, previous_version)
            self._succeed_operation(uow, operation_id, context, str(manifest.location_ref))

    def _persist_session_success(
        self,
        context: _AssignmentContext,
        handle: AdapterSessionHandle,
        operation_id: UUID,
    ) -> GatewayExecutionResult:
        with self._uow_factory() as uow:
            execution = uow.executions.get(
                context.execution.id, context.execution.workspace_id, for_update=True
            )
            lease = uow.leases.get(
                context.lease.id, context.execution.workspace_id, for_update=True
            )
            worker = uow.workers.get(
                context.binding.worker_id, context.execution.workspace_id, for_update=True
            )
            if execution is None or lease is None or worker is None:
                raise GatewayMessageError("Gateway runtime facts disappeared during startup")
            execution.assert_fence(
                lease_id=context.binding.lease_id,
                fencing_token=context.binding.fencing_token,
            )
            lease.assert_owner(
                context.binding.worker_id, context.binding.fencing_token, self._clock()
            )
            previous_execution_version = execution.version
            previous_worker_version = worker.version
            execution.start(
                lease_id=context.binding.lease_id,
                fencing_token=context.binding.fencing_token,
                now=self._clock(),
            )
            worker.mark_running()
            session = ExecutionSession.start(
                session_id=handle.execution_session_id,
                workspace_id=execution.workspace_id,
                execution_id=execution.id,
                worker_id=worker.id,
                adapter_session_id=handle.adapter_session_id,
                now=handle.started_at,
                adapter_module_id=context.binding.adapter_module_id,
                adapter_config_version=context.binding.adapter_config_version,
                workspace_module_id=context.binding.workspace_module_id,
                workspace_config_version=context.binding.workspace_config_version,
                capability_snapshot=handle.capability_snapshot,
                process_ref=handle.process_ref,
                workspace_ref=context.workspace_instance.instance_name,
            )
            session.input_mode = handle.capability_snapshot.input.value
            uow.executions.save(execution, previous_execution_version)
            uow.workers.save(worker, previous_worker_version)
            uow.sessions.add(session)
            self._succeed_operation(
                uow,
                operation_id,
                context,
                handle.adapter_session_id,
                session_id=session.id,
            )
            return GatewayExecutionResult(
                execution_id=execution.id,
                workspace_id=execution.workspace_id,
                session_id=session.id,
                adapter_session_id=session.adapter_session_id,
            )

    def _succeed_operation(
        self,
        uow: ControlPlaneUnitOfWork,
        operation_id: UUID,
        context: _AssignmentContext,
        result_ref: str,
        session_id: UUID | None = None,
    ) -> None:
        operation = uow.gateway_operations.get_by_operation_id(
            operation_id, context.execution.workspace_id, for_update=True
        )
        if operation is None:
            raise GatewayRecoveryRequired("Gateway operation disappeared before success")
        previous_version = operation.version
        if operation.status is GatewayOperationStatus.RUNNING:
            if session_id is not None:
                operation.session_id = session_id
            operation.succeed(result_ref=result_ref, now=self._clock())
            uow.gateway_operations.save(operation, previous_version)

    def _fail_operation(
        self,
        operation_id: UUID,
        context: _AssignmentContext,
        failure_code: str,
    ) -> None:
        try:
            with self._uow_factory() as uow:
                operation = uow.gateway_operations.get_by_operation_id(
                    operation_id, context.execution.workspace_id, for_update=True
                )
                if operation is None or operation.status is not GatewayOperationStatus.RUNNING:
                    return
                previous_version = operation.version
                operation.fail(failure_code=failure_code, now=self._clock())
                uow.gateway_operations.save(operation, previous_version)
        except Exception:  # noqa: BLE001
            return

    def _mark_operation_unknown(
        self,
        operation_id: UUID,
        context: _AssignmentContext,
        failure_code: str,
    ) -> None:
        try:
            with self._uow_factory() as uow:
                operation = uow.gateway_operations.get_by_operation_id(
                    operation_id, context.execution.workspace_id, for_update=True
                )
                if operation is None or operation.status is not GatewayOperationStatus.RUNNING:
                    return
                previous_version = operation.version
                operation.mark_unknown(failure_code=failure_code, now=self._clock())
                uow.gateway_operations.save(operation, previous_version)
        except Exception:  # noqa: BLE001
            return

    def _adapter(
        self, definition: ModuleDefinition, instance: ModuleInstance
    ) -> WorkerAdapter:
        factory = self._adapter_factories.get(definition.provider)
        if factory is None:
            raise GatewayMessageError(
                f"no WorkerAdapter registered for provider {definition.provider}"
            )
        if definition.lifecycle is not ModuleLifecycle.ACTIVE:
            raise GatewayMessageError("adapter module is not active")
        adapter = self._adapter_instances.get(instance.id)
        if adapter is None:
            adapter = factory(instance)
            self._adapter_instances[instance.id] = adapter
        return adapter

    def _workspace_provider(
        self, definition: ModuleDefinition, instance: ModuleInstance
    ) -> WorkspaceProvider:
        factory = self._workspace_factories.get(definition.provider)
        if factory is None:
            raise GatewayMessageError(
                f"no WorkspaceProvider registered for provider {definition.provider}"
            )
        if definition.lifecycle is not ModuleLifecycle.ACTIVE:
            raise GatewayMessageError("workspace module is not active")
        return factory(instance)


class GatewayAssignmentRunner:
    """Claim only assignment messages and route failures back to the Outbox."""

    def __init__(
        self,
        store: OutboxStore,
        gateway: GatewayHandler,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._clock = clock

    def run_once(self, batch_size: int = 20) -> tuple[int, int, int]:
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        messages = self._store.claim(batch_size, topics={ASSIGNMENT_TOPIC})
        processed = 0
        failed = 0
        for message in messages:
            try:
                self._gateway.handle(message)
            except Exception as exc:  # noqa: BLE001
                retry_at = self._clock().replace(microsecond=0)
                delay_seconds = min(300, 2 ** min(message.attempts, 8))
                retry_at += timedelta(seconds=delay_seconds)
                if self._store.reschedule(
                    message.id,
                    message.claim_token,
                    retry_at,
                    _failure_code(exc),
                ):
                    failed += 1
            else:
                if self._store.acknowledge(message.id, message.claim_token):
                    processed += 1
        return len(messages), processed, failed


def _load_module(
    uow: ControlPlaneUnitOfWork,
    module_id: UUID,
    workspace_id: UUID,
    *,
    expected_type: str,
) -> tuple[ModuleInstance, ModuleDefinition]:
    instance = uow.modules.get_instance(module_id, workspace_id)
    if instance is None:
        raise GatewayMessageError(f"{expected_type} module is unavailable")
    definition = uow.modules.get_definition(instance.definition_id, workspace_id)
    if definition is None:
        raise GatewayMessageError(f"{expected_type} module definition is unavailable")
    if definition.module_type != expected_type:
        raise GatewayMessageError(f"module is not a {expected_type}")
    if instance.definition_id != definition.id:
        raise GatewayMessageError("module instance definition does not match")
    if instance.lifecycle is not ModuleLifecycle.ACTIVE:
        raise GatewayMessageError("module instance is not active")
    return instance, definition


def _validate_manifest(
    context: _AssignmentContext, manifest: ExecutionWorkspaceManifest
) -> None:
    if manifest.workspace_id != context.execution.workspace_id:
        raise GatewayMessageError("workspace manifest belongs to another workspace")
    if manifest.execution_id != context.execution.id:
        raise GatewayMessageError("workspace manifest belongs to another execution")
    if manifest.fencing_token != context.binding.fencing_token:
        raise GatewayMessageError("workspace manifest fencing token does not match")


def _session_handle(session: ExecutionSession) -> AdapterSessionHandle:
    if session.capability_snapshot is None or session.process_ref is None:
        raise GatewayRecoveryRequired("persisted session runtime facts are incomplete")
    return AdapterSessionHandle(
        execution_session_id=session.id,
        adapter_session_id=session.adapter_session_id,
        process_ref=session.process_ref,
        capability_snapshot=session.capability_snapshot,
        last_accepted_sequence=session.last_sequence,
        started_at=session.last_activity_at,
    )


def _initial_prompt(spec: ExecutionSpec) -> str:
    criteria = "\n".join(f"- {item}" for item in spec.acceptance_criteria)
    if not criteria:
        return spec.goal
    return f"{spec.goal}\n\nAcceptance criteria:\n{criteria}"


def _assignment_ids(message: OutboxMessage) -> tuple[UUID, UUID, UUID, int]:
    payload = message.payload
    data = payload.get("data")
    data_mapping: Mapping[str, object] = data if isinstance(data, Mapping) else payload
    try:
        workspace_id = UUID(str(_required_payload(payload, "workspace_id")))
        execution_id = UUID(str(_required_payload(payload, "aggregate_id")))
        lease_id = UUID(str(_required_payload(data_mapping, "lease_id")))
        raw_fencing_token = _required_payload(data_mapping, "fencing_token")
        if isinstance(raw_fencing_token, bool) or not isinstance(
            raw_fencing_token, (int, str)
        ):
            raise TypeError("fencing_token must be an integer")
        fencing_token = int(raw_fencing_token)
    except (KeyError, TypeError, ValueError) as exc:
        raise GatewayMessageError("ExecutionLeased payload is malformed") from exc
    return workspace_id, execution_id, lease_id, fencing_token


def _required_payload(payload: Mapping[str, object], name: str) -> object:
    if name not in payload:
        raise KeyError(name)
    return payload[name]


def _operation_id(execution_id: UUID, operation_type: GatewayOperationType) -> UUID:
    return uuid5(NAMESPACE_URL, f"sidera.gateway:{operation_type.value}:{execution_id}")


def _required_config(config: Mapping[str, object], namespace: str, name: str) -> str:
    value = _config_value(config, namespace, name)
    if not isinstance(value, str) or not value.strip():
        raise GatewayMessageError(f"workspace config requires {name}")
    return value.strip()


def _config_text(
    config: Mapping[str, object], namespace: str, name: str, default: str
) -> str:
    value = _config_value(config, namespace, name)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _config_optional_text(
    config: Mapping[str, object], namespace: str, name: str
) -> str | None:
    value = _config_value(config, namespace, name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _config_paths(
    config: Mapping[str, object],
    namespace: str,
    name: str,
    *,
    default: Sequence[str],
) -> tuple[str, ...]:
    value = _config_value(config, namespace, name)
    if value is None:
        return tuple(default)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GatewayMessageError(f"workspace config {name} must be a list")
    return tuple(str(item) for item in value)


def _config_mapping(
    config: Mapping[str, object],
    namespace: str,
    name: str,
    default: Mapping[str, object],
) -> Mapping[str, object]:
    value = _config_value(config, namespace, name)
    if value is None:
        return dict(default)
    if not isinstance(value, Mapping):
        raise GatewayMessageError(f"workspace config {name} must be an object")
    return dict(value)


def _config_value(config: Mapping[str, object], namespace: str, name: str) -> object:
    return config.get(f"{namespace}.{name}", config.get(name))


def _failure_code(exc: Exception) -> str:
    value = str(exc).lower().replace(" ", "_")
    clean = "".join(char if char.isalnum() or char == "_" else "_" for char in value)
    return clean.strip("_")[:100] or type(exc).__name__.lower()
