from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
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
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.execution_repository import ExecutionRepository
from sidera_control_plane.persistence.lease_store import LeaseStore
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    EventModel,
    ExecutionBindingModel,
    OutboxMessageModel,
    SchedulingBlockModel,
)
from sidera_control_plane.persistence.module_repository import (
    ExecutionBindingRepository,
)
from sidera_control_plane.persistence.repositories import (
    CommandRepository,
    EventRepository,
)
from sidera_control_plane.persistence.scheduling_block_repository import (
    SqlAlchemySchedulingBlockRepository,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from sidera_control_plane.persistence.worker_repository import WorkerRepository

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class AtomicityFixture:
    worker: Worker
    execution: Execution
    adapter_instance: ModuleInstance
    workspace_instance: ModuleInstance


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


def _seed(
    database: Database,
    workspace_id: UUID,
    *,
    active_block: bool = False,
) -> AtomicityFixture:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Scheduler atomicity",
        description="Force persistence failures during assignment",
        acceptance_criteria=("all assignment facts roll back together",),
        priority=50,
        requested_by="integration",
    )
    task.mark_ready()
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="opencode",
        display_name="atomicity worker",
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
    adapter_instance = ModuleInstance(
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=adapter_definition.id,
        instance_name="atomicity-adapter",
        config_version=1,
        config_namespace="adapter",
        config={"adapter.command": "opencode"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_instance = ModuleInstance(
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=workspace_definition.id,
        instance_name="atomicity-workspace",
        config_version=1,
        config_namespace="workspace",
        config={"workspace.root": "/tmp/sidera-atomicity"},
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
        purpose="atomic scheduler assignment",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        uow.workers.add(worker)
        uow.modules.add_definition(adapter_definition)
        uow.modules.add_definition(workspace_definition)
        uow.modules.add_instance(adapter_instance)
        uow.modules.add_instance(workspace_instance)
        uow.flush()
        uow.worker_runtime_configurations.add(
            WorkerRuntimeConfiguration.create(
                worker_id=worker.id,
                workspace_id=workspace_id,
                adapter_module_instance_id=adapter_instance.id,
                adapter_module_config_version=1,
                workspace_module_instance_id=workspace_instance.id,
                workspace_module_config_version=1,
            )
        )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.executions.add(execution)
    if active_block:
        issue = SchedulingIssue(
            reason_code=SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING,
            reason_details={"worker_count": 1},
            requires_operator=True,
        )
        block = SchedulingBlock.open(
            block_id=uuid4(),
            workspace_id=workspace_id,
            execution_id=execution.id,
            issue=issue,
            now=NOW,
        )
        with SqlAlchemyUnitOfWork(database) as uow:
            uow.scheduling_blocks.add(block)
    return AtomicityFixture(
        worker=worker,
        execution=execution,
        adapter_instance=adapter_instance,
        workspace_instance=workspace_instance,
    )


def _scheduler(database: Database) -> Scheduler:
    return Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=LowestCostWorkerPolicy(),
        clock=lambda: NOW,
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
    )


@pytest.mark.parametrize(
    "failure_point",
    (
        "lease_add",
        "worker_save",
        "execution_save",
        "binding_add",
        "block_save",
        "command_record",
        "event_0",
        "event_1",
        "event_2",
        "event_3",
    ),
)
def test_scheduler_assignment_rolls_back_every_persistence_failure(
    database: Database,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    fixture = _seed(database, workspace_id, active_block=failure_point == "block_save")

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(f"injected failure: {failure_point}")

    if failure_point == "lease_add":
        monkeypatch.setattr(LeaseStore, "add", fail)
    elif failure_point == "worker_save":
        monkeypatch.setattr(WorkerRepository, "save", fail)
    elif failure_point == "execution_save":
        monkeypatch.setattr(ExecutionRepository, "save", fail)
    elif failure_point == "binding_add":
        monkeypatch.setattr(ExecutionBindingRepository, "add", fail)
    elif failure_point == "block_save":
        monkeypatch.setattr(SqlAlchemySchedulingBlockRepository, "save", fail)
    elif failure_point == "command_record":
        monkeypatch.setattr(CommandRepository, "record_applied", fail)
    else:
        event_index = int(failure_point.split("_", 1)[1])
        original_append = EventRepository.append_with_outbox
        calls = 0

        def append_or_fail(self: EventRepository, event: EventData) -> None:
            nonlocal calls
            if calls == event_index:
                fail()
            calls += 1
            original_append(self, event)

        monkeypatch.setattr(EventRepository, "append_with_outbox", append_or_fail)

    with pytest.raises(RuntimeError, match="injected failure"):
        _scheduler(database).run_once(limit=1)

    with SqlAlchemyUnitOfWork(database) as uow:
        execution = uow.executions.get(fixture.execution.id, workspace_id)
        worker = uow.workers.get(fixture.worker.id, workspace_id)
    assert execution is not None
    assert execution.status is ExecutionStatus.QUEUED
    assert worker is not None
    assert worker.current_load == 0
    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(AssignmentLeaseModel).where(
                AssignmentLeaseModel.execution_id == fixture.execution.id,
                AssignmentLeaseModel.status == "active",
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ExecutionBindingModel).where(
                ExecutionBindingModel.execution_id == fixture.execution.id
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(CommandModel)
        ) == 0
        assert session.scalar(select(func.count()).select_from(EventModel)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxMessageModel)) == 0
        blocks = session.scalars(
            select(SchedulingBlockModel).where(
                SchedulingBlockModel.execution_id == fixture.execution.id
            )
        ).all()
    if failure_point == "block_save":
        assert len(blocks) == 1
        assert blocks[0].status == "active"
        assert blocks[0].version == 1
    else:
        assert blocks == []
