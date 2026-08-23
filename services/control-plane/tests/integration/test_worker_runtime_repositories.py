from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.domain.errors import VersionConflict
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
from sidera_control_plane.domain.scheduling import (
    SchedulingBlock,
    SchedulingBlockReason,
    SchedulingIssue,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    task: Task
    workers: tuple[Worker, ...]
    adapter_definition: ModuleDefinition
    workspace_definition: ModuleDefinition
    adapter_instance: ModuleInstance
    workspace_instance: ModuleInstance
    execution: Execution


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


def _seed_runtime_resources(
    database: Database, workspace_id: UUID
) -> RuntimeResources:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Runtime repository task",
        description="Verify runtime repository behavior",
        acceptance_criteria=("runtime facts round-trip",),
        priority=80,
        requested_by="integration",
    )
    task.mark_ready()
    workers = tuple(
        Worker.register(
            worker_id=uuid4(),
            workspace_id=workspace_id,
            adapter_type="opencode",
            display_name=f"Runtime worker {index}",
            capabilities=frozenset({"python"}),
            capacity=1,
            cost_per_hour_microunits=100,
            startup_latency_ms=250,
            now=NOW,
        )
        for index in range(2)
    )
    adapter_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="worker_adapter",
        provider="fake",
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=_worker_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="workspace_provider",
        provider="fake",
        interface_version="sidera.workspace-provider.v1",
        config_schema_version=1,
        capabilities=_workspace_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    adapter_instance = ModuleInstance(
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=adapter_definition.id,
        instance_name="runtime-adapter",
        config_version=1,
        config_namespace="adapter",
        config={"adapter.command": "fake"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_instance = ModuleInstance(
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=workspace_definition.id,
        instance_name="runtime-workspace",
        config_version=1,
        config_namespace="workspace",
        config={"workspace.root": "/tmp/sidera"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task.id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=2,
        purpose="run runtime repository test",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=workers[0].id,
        not_before=NOW,
        adapter_preferences=("opencode", "codex"),
        adapter_forbidden=("unsafe",),
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        for worker in workers:
            uow.workers.add(worker)
        uow.modules.add_definition(adapter_definition)
        uow.modules.add_definition(workspace_definition)
        uow.modules.add_instance(adapter_instance)
        uow.modules.add_instance(workspace_instance)

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.executions.add(execution)

    return RuntimeResources(
        task=task,
        workers=workers,
        adapter_definition=adapter_definition,
        workspace_definition=workspace_definition,
        adapter_instance=adapter_instance,
        workspace_instance=workspace_instance,
        execution=execution,
    )


def _runtime_configuration(resources: RuntimeResources, worker: Worker):
    from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration

    return WorkerRuntimeConfiguration.create(
        worker_id=worker.id,
        workspace_id=worker.workspace_id,
        adapter_module_instance_id=resources.adapter_instance.id,
        adapter_module_config_version=resources.adapter_instance.config_version,
        workspace_module_instance_id=resources.workspace_instance.id,
        workspace_module_config_version=resources.workspace_instance.config_version,
    )


def test_worker_runtime_repository_batches_reads_and_preserves_tenant_scope(
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    configurations = tuple(
        _runtime_configuration(resources, worker) for worker in resources.workers
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        for configuration in configurations:
            uow.worker_runtime_configurations.add(configuration)

    statements: list[str] = []

    def record_statement(*args: object, **kwargs: object) -> None:
        del kwargs
        if len(args) >= 3 and isinstance(args[2], str):
            statements.append(args[2])

    event.listen(database.engine, "before_cursor_execute", record_statement)
    try:
        with SqlAlchemyUnitOfWork(database) as uow:
            stored = uow.worker_runtime_configurations.get_for_workers(
                tuple(worker.id for worker in reversed(resources.workers)),
                workspace_id,
            )
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statement)

    assert tuple(item.worker_id for item in stored) == tuple(
        worker.id for worker in reversed(resources.workers)
    )
    assert sum("worker_runtime_configurations" in statement for statement in statements) == 1

    with SqlAlchemyUnitOfWork(database) as uow:
        assert (
            uow.worker_runtime_configurations.get_for_worker(
                resources.workers[0].id, uuid4()
            )
            is None
        )
        assert (
            uow.worker_runtime_configurations.get_for_workers(
                tuple(worker.id for worker in resources.workers), uuid4()
            )
            == ()
        )


def test_worker_runtime_repository_uses_occ_and_does_not_upsert(
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    configuration = _runtime_configuration(resources, resources.workers[0])
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.worker_runtime_configurations.add(configuration)

    with pytest.raises(IntegrityError), SqlAlchemyUnitOfWork(database) as uow:
        uow.worker_runtime_configurations.add(
            _runtime_configuration(resources, resources.workers[0])
        )
        uow.flush()

    with SqlAlchemyUnitOfWork(database) as uow:
        stale = uow.worker_runtime_configurations.get_for_worker(
            resources.workers[0].id, workspace_id
        )
    assert stale is not None

    with SqlAlchemyUnitOfWork(database) as uow:
        current = uow.worker_runtime_configurations.get_for_worker(
            resources.workers[0].id, workspace_id
        )
        assert current is not None
        previous_version = current.version
        current.reconfigure(
            adapter_module_instance_id=resources.adapter_instance.id,
            adapter_module_config_version=2,
            workspace_module_instance_id=resources.workspace_instance.id,
            workspace_module_config_version=1,
        )
        uow.worker_runtime_configurations.save(current, previous_version)

    stale.reconfigure(
        adapter_module_instance_id=resources.adapter_instance.id,
        adapter_module_config_version=3,
        workspace_module_instance_id=resources.workspace_instance.id,
        workspace_module_config_version=1,
    )
    with pytest.raises(VersionConflict), SqlAlchemyUnitOfWork(database) as uow:
        uow.worker_runtime_configurations.save(stale, 1)


def test_scheduling_block_repository_tracks_episodes_and_reopens_after_resolve(
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    issue = SchedulingIssue(
        reason_code=SchedulingBlockReason.NO_ELIGIBLE_WORKER,
        reason_details={"required": ["python"]},
        requires_operator=False,
    )
    block = SchedulingBlock.open(
        block_id=uuid4(),
        workspace_id=workspace_id,
        execution_id=resources.execution.id,
        issue=issue,
        now=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.scheduling_blocks.add(block)

    with SqlAlchemyUnitOfWork(database) as uow:
        stored = uow.scheduling_blocks.get_active_for_execution(
            resources.execution.id, workspace_id, for_update=True
        )
        assert stored is not None
        previous_version = stored.version
        stored.observe(issue=issue, now=NOW + timedelta(seconds=1))
        uow.scheduling_blocks.save(stored, previous_version)

    with pytest.raises(IntegrityError), SqlAlchemyUnitOfWork(database) as uow:
        assert (
            uow.scheduling_blocks.get_active_for_execution(
                resources.execution.id, workspace_id
            )
            is not None
        )
        duplicate = SchedulingBlock.open(
            block_id=uuid4(),
            workspace_id=workspace_id,
            execution_id=resources.execution.id,
            issue=issue,
            now=NOW + timedelta(seconds=2),
        )
        uow.scheduling_blocks.add(duplicate)
        uow.flush()

    with SqlAlchemyUnitOfWork(database) as uow:
        stored = uow.scheduling_blocks.get_active_for_execution(
            resources.execution.id, workspace_id
        )
        assert stored is not None
        previous_version = stored.version
        stored.resolve(now=NOW + timedelta(seconds=3))
        uow.scheduling_blocks.save(stored, previous_version)

    reopened = SchedulingBlock.open(
        block_id=uuid4(),
        workspace_id=workspace_id,
        execution_id=resources.execution.id,
        issue=issue,
        now=NOW + timedelta(seconds=4),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        assert (
            uow.scheduling_blocks.get_active_for_execution(
                resources.execution.id, workspace_id
            )
            is None
        )
        uow.scheduling_blocks.add(reopened)


def test_module_repository_saves_lifecycle_with_independent_resource_occ(
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    with SqlAlchemyUnitOfWork(database) as uow:
        stale_definition = uow.modules.get_definition(
            resources.adapter_definition.id, workspace_id
        )
        stale_instance = uow.modules.get_instance(
            resources.adapter_instance.id, workspace_id
        )
    assert stale_definition is not None
    assert stale_instance is not None

    with SqlAlchemyUnitOfWork(database) as uow:
        current_definition = uow.modules.get_definition(
            resources.adapter_definition.id, workspace_id
        )
        current_instance = uow.modules.get_instance(
            resources.adapter_instance.id, workspace_id
        )
        assert current_definition is not None
        assert current_instance is not None
        changed_definition = current_definition.with_lifecycle(
            ModuleLifecycle.DRAINING
        )
        changed_instance = current_instance.with_lifecycle(ModuleLifecycle.DRAINING)
        uow.modules.save_definition(changed_definition, current_definition.resource_version)
        uow.modules.save_instance(changed_instance, current_instance.resource_version)

    with SqlAlchemyUnitOfWork(database) as uow:
        stored_definition = uow.modules.get_definition(
            resources.adapter_definition.id, workspace_id
        )
        stored_instance = uow.modules.get_instance(
            resources.adapter_instance.id, workspace_id
        )
    assert stored_definition is not None
    assert stored_instance is not None
    assert stored_definition.resource_version == 2
    assert stored_definition.config_schema_version == 1
    assert stored_instance.resource_version == 2
    assert stored_instance.config_version == 1

    with pytest.raises(VersionConflict), SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.save_definition(
            stale_definition.with_lifecycle(ModuleLifecycle.DRAINING),
            stale_definition.resource_version,
        )
    with pytest.raises(VersionConflict), SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.save_instance(
            stale_instance.with_lifecycle(ModuleLifecycle.DRAINING),
            stale_instance.resource_version,
        )
