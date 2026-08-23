from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.lease import LeaseStatus
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
    CommandModel,
    EventModel,
    ExecutionBindingModel,
    ExecutionModel,
    SchedulingBlockModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    name: str
    capabilities: frozenset[str]
    startup_latency_ms: int = 25
    cost_per_hour_microunits: int = 500_000
    capacity: int = 1
    current_load: int = 0
    heartbeat_at: datetime = NOW
    status: str = "idle"


def add_workspace(database: Database, workspace_id: UUID, name: str) -> None:
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=workspace_id,
                name=name,
                status="active",
                version=1,
            )
        )
        session.commit()


def seed_scheduler_case(
    database: Database,
    workspace_id: UUID,
    *,
    worker_specs: tuple[WorkerSpec, ...],
    priority: int = 50,
    required_capabilities: frozenset[str] = frozenset({"code", "python"}),
    pinned_worker_id: UUID | None = None,
    execution_workspace_id: UUID | None = None,
) -> tuple[Task, tuple[Worker, ...], Execution]:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=execution_workspace_id or workspace_id,
        title="Scheduler task",
        description="Deterministic scheduling",
        acceptance_criteria=("one worker is assigned",),
        priority=priority,
        requested_by="system:test",
    )
    task.mark_ready()
    workers = tuple(
        _make_worker(workspace_id, spec) for spec in worker_specs
    )
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=3,
        purpose="schedule execution",
        capability_requirements=required_capabilities,
        pinned_worker_id=pinned_worker_id,
        not_before=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        for worker in workers:
            uow.workers.add(worker)
        for worker in workers:
            register_runtime_modules_and_configure_worker(uow, worker)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.executions.add(execution)
    return task, workers, execution


def register_runtime_modules_and_configure_worker(
    uow: SqlAlchemyUnitOfWork, worker: Worker
) -> None:
    adapter_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=worker.workspace_id,
        module_type="worker_adapter",
        provider=worker.adapter_type,
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=WorkerAdapterCapabilityProfile(
            input=CapabilityInput.QUEUED,
            idempotency=IdempotencyCapability.OPERATION_ID,
            resume=ResumeCapability.DURABLE,
            stop=StopCapability.EXACT_EXECUTION,
            sandbox=SandboxCapability.FILESYSTEM,
            stability=StabilityLevel.STABLE,
        ),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=worker.workspace_id,
        module_type="workspace_provider",
        provider="fake-workspace",
        interface_version="sidera.workspace-provider.v1",
        config_schema_version=1,
        capabilities=WorkspaceProviderCapabilityProfile(
            isolation=SandboxCapability.ISOLATED_RUNTIME,
            writable=True,
            git_worktree=True,
            stability=StabilityLevel.STABLE,
        ),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    adapter_instance = ModuleInstance(
        id=uuid4(),
        workspace_id=worker.workspace_id,
        definition_id=adapter_definition.id,
        instance_name=f"adapter-{worker.id}",
        config_version=1,
        config_namespace="adapter",
        config={"adapter.command": worker.adapter_type},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_instance = ModuleInstance(
        id=uuid4(),
        workspace_id=worker.workspace_id,
        definition_id=workspace_definition.id,
        instance_name=f"workspace-{worker.id}",
        config_version=1,
        config_namespace="workspace",
        config={"workspace.root": f"/tmp/sidera/{worker.id}"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    uow.modules.add_definition(adapter_definition)
    uow.modules.add_definition(workspace_definition)
    uow.modules.add_instance(adapter_instance)
    uow.modules.add_instance(workspace_instance)
    uow.flush()
    uow.worker_runtime_configurations.add(
        WorkerRuntimeConfiguration.create(
            worker_id=worker.id,
            workspace_id=worker.workspace_id,
            adapter_module_instance_id=adapter_instance.id,
            adapter_module_config_version=adapter_instance.config_version,
            workspace_module_instance_id=workspace_instance.id,
            workspace_module_config_version=workspace_instance.config_version,
        )
    )


def _make_worker(workspace_id: UUID, spec: WorkerSpec) -> Worker:
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name=spec.name,
        capabilities=spec.capabilities,
        capacity=spec.capacity,
        cost_per_hour_microunits=spec.cost_per_hour_microunits,
        startup_latency_ms=spec.startup_latency_ms,
        now=spec.heartbeat_at,
    )
    if spec.current_load:
        for _ in range(spec.current_load):
            worker.reserve()
    if spec.status == "unhealthy":
        worker.heartbeat(healthy=False, now=spec.heartbeat_at)
    elif spec.status == "draining":
        worker.drain()
    return worker


def make_scheduler(
    database: Database,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
) -> Scheduler:
    return Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=LowestCostWorkerPolicy(),
        clock=clock,
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
    )


def test_scheduler_assigns_atomically_and_records_auditable_facts(
    database: Database,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    _task, workers, execution = seed_scheduler_case(
        database,
        workspace_id,
        worker_specs=(
            WorkerSpec(
                name="Extra but cheap",
                capabilities=frozenset({"code", "python", "review"}),
                startup_latency_ms=1,
                cost_per_hour_microunits=1,
            ),
            WorkerSpec(
                name="Exact candidate",
                capabilities=frozenset({"code", "python"}),
                startup_latency_ms=50,
                cost_per_hour_microunits=900,
            ),
        ),
    )
    result = make_scheduler(database).run_once(limit=10)

    assert result.examined == 1
    assert result.assigned == 1
    assert result.deferred == 0
    with SqlAlchemyUnitOfWork(database) as uow:
        stored_execution = uow.executions.get(execution.id, workspace_id)
        stored_worker = uow.workers.get(workers[1].id, workspace_id)
        leases = uow.leases.claim_expired(now=NOW - timedelta(seconds=1), limit=10)
    assert stored_execution is not None
    assert stored_execution.status is ExecutionStatus.LEASED
    assert stored_execution.last_fencing_token == 1
    assert stored_worker is not None
    assert stored_worker.current_load == 1
    assert stored_worker.id == workers[1].id
    assert leases == ()

    with database.session() as session:
        command = session.scalar(select(CommandModel))
        assert command is not None
        assert command.result is not None
        event_ids = tuple(UUID(item) for item in command.result["event_ids"])
        events = session.scalars(
            select(EventModel).where(EventModel.id.in_(event_ids))
        ).all()
        lease = session.scalar(
            select(AssignmentLeaseModel).where(
                AssignmentLeaseModel.execution_id == execution.id
            )
        )
        binding = session.scalar(
            select(ExecutionBindingModel).where(
                ExecutionBindingModel.execution_id == execution.id
            )
        )
        active_block = session.scalar(
            select(SchedulingBlockModel).where(
                SchedulingBlockModel.execution_id == execution.id,
                SchedulingBlockModel.status == "active",
            )
        )
    events_by_id = {event.id: event for event in events}
    assert [events_by_id[event_id].event_type for event_id in event_ids] == [
        "ExecutionRuntimeBound",
        "ExecutionLeased",
        "LeaseGranted",
        "WorkerCapacityReserved",
    ]
    assert command.command_type == "ScheduleExecution"
    assert command.actor_type == "system"
    assert command.actor_id == "scheduler"
    assert lease is not None
    assert lease.status == LeaseStatus.ACTIVE.value
    assert lease.fencing_token == 1
    assert lease.assignment_reason["policy"] == "lowest-cost-v1"
    assert binding is not None
    assert binding.worker_id == workers[1].id
    assert binding.adapter_config_version == 1
    assert binding.workspace_config_version == 1
    assert binding.adapter_module_id is not None
    assert binding.workspace_module_id is not None
    assert binding.fencing_token == lease.fencing_token
    assert active_block is None
    assert row_counts() == {
        "tasks": 1,
        "commands": 1,
        "events": 4,
        "outbox_messages": 4,
    }


@pytest.mark.parametrize(
    "worker_spec",
    [
        WorkerSpec("Missing capability", frozenset({"python"})),
        WorkerSpec(
            "Stale worker",
            frozenset({"code", "python"}),
            heartbeat_at=NOW - timedelta(minutes=2),
        ),
        WorkerSpec("Unhealthy worker", frozenset({"code", "python"}), status="unhealthy"),
        WorkerSpec("Draining worker", frozenset({"code", "python"}), status="draining"),
        WorkerSpec(
            "Full worker",
            frozenset({"code", "python"}),
            capacity=1,
            current_load=1,
        ),
    ],
)
def test_scheduler_defers_without_hard_eligible_worker(
    database: Database,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
    worker_spec: WorkerSpec,
) -> None:
    _, _, execution = seed_scheduler_case(
        database,
        workspace_id,
        worker_specs=(worker_spec,),
    )

    result = make_scheduler(database).run_once(limit=10)

    assert result == type(result)(examined=1, assigned=0, deferred=1)
    with SqlAlchemyUnitOfWork(database) as uow:
        stored = uow.executions.get(execution.id, workspace_id)
    assert stored is not None
    assert stored.status is ExecutionStatus.QUEUED
    assert row_counts() == {
        "tasks": 1,
        "commands": 1,
        "events": 1,
        "outbox_messages": 1,
    }


def test_scheduler_skips_deferred_head_and_assigns_later_candidate(
    database: Database,
    workspace_id: UUID,
) -> None:
    _first_task, _, first_execution = seed_scheduler_case(
        database,
        workspace_id,
        worker_specs=(),
        priority=100,
        required_capabilities=frozenset({"special-first"}),
    )
    _second_task, second_workers, second_execution = seed_scheduler_case(
        database,
        workspace_id,
        worker_specs=(WorkerSpec("Available worker", frozenset({"code", "python"})),),
        priority=50,
    )

    result = make_scheduler(database).run_once(limit=2)

    assert result.examined == 2
    assert result.assigned == 1
    assert result.deferred == 1
    with SqlAlchemyUnitOfWork(database) as uow:
        stored_first = uow.executions.get(first_execution.id, workspace_id)
        stored_second = uow.executions.get(second_execution.id, workspace_id)
        stored_worker = uow.workers.get(second_workers[0].id, workspace_id)
    assert stored_first is not None
    assert stored_first.status is ExecutionStatus.QUEUED
    assert stored_second is not None
    assert stored_second.status is ExecutionStatus.LEASED
    assert stored_worker is not None
    assert stored_worker.current_load == 1


def test_scheduler_respects_workspace_and_unavailable_pin(
    database: Database,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    workspace_b_id = uuid4()
    add_workspace(database, workspace_b_id, "Workspace B")
    workspace_b_worker = _make_worker(
        workspace_b_id,
        WorkerSpec("Workspace B worker", frozenset({"code", "python"})),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.workers.add(workspace_b_worker)
    _, _, _wrong_workspace_execution = seed_scheduler_case(
        database,
        workspace_id,
        worker_specs=(),
        execution_workspace_id=workspace_id,
    )
    _, workspace_a_workers, pinned_execution = seed_scheduler_case(
        database,
        workspace_id,
        worker_specs=(
            WorkerSpec(
                "Unavailable pinned worker",
                frozenset({"code", "python"}),
                status="draining",
            ),
        ),
    )
    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == pinned_execution.id)
            .values(pinned_worker_id=workspace_a_workers[0].id)
        )
        session.commit()

    scheduler = make_scheduler(database)
    result = scheduler.run_once(limit=1)
    pinned_result = scheduler.run_once(limit=1)

    assert result.assigned == 0
    assert result.deferred == 1
    assert pinned_result.assigned == 0
    assert pinned_result.deferred == 1
    assert row_counts()["events"] == 1


def test_two_schedulers_reserve_one_execution_once(
    database: Database,
    workspace_id: UUID,
) -> None:
    _, workers, execution = seed_scheduler_case(
        database,
        workspace_id,
        worker_specs=(WorkerSpec("One-slot worker", frozenset({"code", "python"})),),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda _index: make_scheduler(database).run_once(limit=1), range(2)
            )
        )

    assert sorted(result.assigned for result in results) == [0, 1]
    with database.session() as session:
        active_leases = session.scalar(
            select(func.count())
            .select_from(AssignmentLeaseModel)
            .where(
                AssignmentLeaseModel.execution_id == execution.id,
                AssignmentLeaseModel.status == LeaseStatus.ACTIVE.value,
            )
        )
    assert active_leases == 1
    with SqlAlchemyUnitOfWork(database) as uow:
        stored_worker = uow.workers.get(workers[0].id, workspace_id)
    assert stored_worker is not None
    assert stored_worker.current_load == 1
