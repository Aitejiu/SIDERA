from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.runtime_commands import RuntimeCommandHandler
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    ExecutionBindingModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ConcurrencyFixture:
    worker: Worker
    execution: Execution
    old_adapter: ModuleInstance
    old_workspace: ModuleInstance
    new_adapter: ModuleInstance
    new_workspace: ModuleInstance


def _worker_capabilities() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
    )


def _workspace_capabilities() -> WorkspaceProviderCapabilityProfile:
    return WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
    )


def _module_instance(
    workspace_id: UUID,
    definition_id: UUID,
    name: str,
    namespace: str,
    value: str,
) -> ModuleInstance:
    return ModuleInstance(
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=definition_id,
        instance_name=name,
        config_version=1,
        config_namespace=namespace,
        config={f"{namespace}.value": value},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )


def _seed(database: Database, workspace_id: UUID) -> ConcurrencyFixture:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Scheduler configuration race",
        description="Switch runtime while scheduling",
        acceptance_criteria=("the binding is one complete runtime pair",),
        priority=50,
        requested_by="integration",
    )
    task.mark_ready()
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="opencode",
        display_name="concurrency worker",
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=100,
        startup_latency_ms=10,
        now=NOW,
    )
    adapter_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="worker_adapter",
        provider="opencode",
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=_worker_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="workspace_provider",
        provider="git-worktree",
        interface_version="sidera.workspace-provider.v1",
        config_schema_version=1,
        capabilities=_workspace_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    old_adapter = _module_instance(
        workspace_id, adapter_definition.id, "adapter-v1", "adapter", "v1"
    )
    old_workspace = _module_instance(
        workspace_id, workspace_definition.id, "workspace-v1", "workspace", "v1"
    )
    new_adapter = _module_instance(
        workspace_id, adapter_definition.id, "adapter-v2", "adapter", "v2"
    )
    new_workspace = _module_instance(
        workspace_id, workspace_definition.id, "workspace-v2", "workspace", "v2"
    )
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task.id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=2,
        purpose="configuration race",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        uow.workers.add(worker)
        uow.modules.add_definition(adapter_definition)
        uow.modules.add_definition(workspace_definition)
        uow.modules.add_instance(old_adapter)
        uow.modules.add_instance(old_workspace)
        uow.modules.add_instance(new_adapter)
        uow.modules.add_instance(new_workspace)
        uow.flush()
        uow.worker_runtime_configurations.add(
            WorkerRuntimeConfiguration.create(
                worker_id=worker.id,
                workspace_id=workspace_id,
                adapter_module_instance_id=old_adapter.id,
                adapter_module_config_version=1,
                workspace_module_instance_id=old_workspace.id,
                workspace_module_config_version=1,
            )
        )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.executions.add(execution)
    return ConcurrencyFixture(
        worker=worker,
        execution=execution,
        old_adapter=old_adapter,
        old_workspace=old_workspace,
        new_adapter=new_adapter,
        new_workspace=new_workspace,
    )


class BarrierResolver(WorkerRuntimeResolver):
    def __init__(self, barrier: Barrier) -> None:
        self._barrier = barrier
        self._delegate = WorkerRuntimeResolver()

    def inspect_candidates(self, uow, workers, execution):  # type: ignore[no-untyped-def]
        inspections = self._delegate.inspect_candidates(uow, workers, execution)
        self._barrier.wait(timeout=10)
        return inspections

    def lock_and_resolve(self, uow, worker, execution):  # type: ignore[no-untyped-def]
        return self._delegate.lock_and_resolve(uow, worker, execution)


def _scheduler(
    database: Database,
    *,
    resolver: WorkerRuntimeResolver | None = None,
) -> Scheduler:
    return Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=LowestCostWorkerPolicy(),
        clock=lambda: NOW,
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
        runtime_resolver=resolver,
    )


def _configure_command(fixture: ConcurrencyFixture, workspace_id: UUID) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type="ConfigureWorkerRuntime",
        target_type="worker_runtime_configuration",
        actor_type="user",
        actor_id="user:concurrency",
        target_id=fixture.worker.id,
        payload={
            "adapter_module_instance_id": str(fixture.new_adapter.id),
            "adapter_module_config_version": 1,
            "workspace_module_instance_id": str(fixture.new_workspace.id),
            "workspace_module_config_version": 1,
        },
        expected_version=1,
        idempotency_key=f"configure-race-{uuid4()}",
        correlation_id=uuid4(),
    )


def test_configuration_switch_binds_one_complete_runtime_pair(
    database: Database,
    workspace_id: UUID,
) -> None:
    fixture = _seed(database, workspace_id)
    barrier = Barrier(2)
    scheduler = _scheduler(database, resolver=BarrierResolver(barrier))
    command_service = CommandService(
        lambda: SqlAlchemyUnitOfWork(database),
        (RuntimeCommandHandler(clock=lambda: NOW),),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        schedule_future = pool.submit(scheduler.run_once, 1)
        barrier.wait(timeout=10)
        configure_future = pool.submit(
            command_service.handle, _configure_command(fixture, workspace_id)
        )
        schedule_result = schedule_future.result(timeout=10)
        configure_result = configure_future.result(timeout=10)

    assert schedule_result.assigned == 1
    assert configure_result.aggregate_version == 2
    with SqlAlchemyUnitOfWork(database) as uow:
        binding = uow.bindings.get_for_execution(fixture.execution.id, workspace_id)
        configuration = uow.worker_runtime_configurations.get_for_worker(
            fixture.worker.id, workspace_id
        )
    assert binding is not None
    assert configuration is not None
    assert (
        binding.adapter_module_id,
        binding.workspace_module_id,
    ) in {
        (fixture.old_adapter.id, fixture.old_workspace.id),
        (fixture.new_adapter.id, fixture.new_workspace.id),
    }


def test_two_schedulers_commit_one_lease_one_binding_and_one_worker_reservation(
    database: Database,
    workspace_id: UUID,
) -> None:
    fixture = _seed(database, workspace_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(pool.submit(_scheduler(database).run_once, 1) for _ in range(2))
        results = tuple(future.result(timeout=10) for future in futures)

    assert sum(result.assigned for result in results) == 1
    with database.session() as session:
        active_leases = session.scalar(
            select(func.count()).select_from(AssignmentLeaseModel).where(
                AssignmentLeaseModel.execution_id == fixture.execution.id,
                AssignmentLeaseModel.status == "active",
            )
        )
        binding_count = session.scalar(
            select(func.count()).select_from(ExecutionBindingModel).where(
                ExecutionBindingModel.execution_id == fixture.execution.id
            )
        )
        worker = session.scalar(
            select(WorkerModel).where(WorkerModel.id == fixture.worker.id)
        )
    assert active_leases == 1
    assert binding_count == 1
    assert worker is not None
    assert worker.current_load == 1
