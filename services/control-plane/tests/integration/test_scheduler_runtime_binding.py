from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    RuntimeCandidateInspection,
    WorkerRuntimeResolver,
)
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
    SchedulingBlockReason,
    SchedulingIssue,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionBindingModel,
    SchedulingBlockModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RuntimeFixture:
    task: Task
    workers: tuple[Worker, ...]
    adapter_instance: ModuleInstance
    workspace_instance: ModuleInstance
    execution: Execution


class RevalidationResolver(WorkerRuntimeResolver):
    def __init__(self, fixture: RuntimeFixture, failed_worker_id: UUID) -> None:
        self._fixture = fixture
        self._failed_worker_id = failed_worker_id
        self.calls: list[UUID] = []

    def inspect_candidates(
        self,
        uow: ControlPlaneUnitOfWork,
        workers: Collection[Worker],
        execution: Execution,
    ) -> tuple[RuntimeCandidateInspection, ...]:
        del uow, execution
        return tuple(
            RuntimeCandidateInspection(
                worker=worker,
                resolved=_resolved_runtime(self._fixture, worker),
                issue=None,
            )
            for worker in workers
        )

    def lock_and_resolve(
        self,
        uow: ControlPlaneUnitOfWork,
        worker: Worker,
        execution: Execution,
    ) -> ResolvedWorkerRuntime | SchedulingIssue:
        del uow, execution
        self.calls.append(worker.id)
        if worker.id == self._failed_worker_id:
            return SchedulingIssue(
                reason_code=SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
                reason_details={
                    "worker_id": str(worker.id),
                    "reason": "final_revalidation_failed",
                },
                requires_operator=True,
            )
        return _resolved_runtime(self._fixture, worker)


def _capabilities() -> WorkerAdapterCapabilityProfile:
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


def _resolved_runtime(fixture: RuntimeFixture, worker: Worker) -> ResolvedWorkerRuntime:
    return ResolvedWorkerRuntime(
        worker_id=worker.id,
        adapter_type=worker.adapter_type,
        adapter_module_instance_id=fixture.adapter_instance.id,
        adapter_module_config_version=fixture.adapter_instance.config_version,
        workspace_module_instance_id=fixture.workspace_instance.id,
        workspace_module_config_version=fixture.workspace_instance.config_version,
        adapter_definition_id=fixture.adapter_instance.definition_id,
        workspace_definition_id=fixture.workspace_instance.definition_id,
        capability_snapshot=_capabilities(),
        resolution_facts={"worker_id": str(worker.id)},
    )


def _seed(
    database: Database,
    workspace_id: UUID,
    *,
    configured_worker_indexes: tuple[int, ...],
    forbidden_adapters: tuple[str, ...] = (),
) -> RuntimeFixture:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Scheduler runtime binding",
        description="Verify the scheduler binds an exact runtime",
        acceptance_criteria=("the binding is complete and auditable",),
        priority=50,
        requested_by="integration",
    )
    task.mark_ready()
    workers = tuple(
        Worker.register(
            worker_id=UUID(f"00000000-0000-0000-0000-00000000000{index + 1}"),
            workspace_id=workspace_id,
            adapter_type="opencode",
            display_name=f"runtime worker {index}",
            capabilities=frozenset({"python"}),
            capacity=1,
            cost_per_hour_microunits=1 if index == 1 else 100,
            startup_latency_ms=10,
            now=NOW,
        )
        for index in range(2)
    )
    adapter_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="worker_adapter",
        provider="opencode",
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=_capabilities(),
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
        instance_name="scheduler-opencode",
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
        instance_name="scheduler-workspace",
        config_version=1,
        config_namespace="workspace",
        config={"workspace.root": "/tmp/sidera-scheduler"},
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
        purpose="bind runtime",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
        adapter_forbidden=forbidden_adapters,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        for worker in workers:
            uow.workers.add(worker)
        uow.modules.add_definition(adapter_definition)
        uow.modules.add_definition(workspace_definition)
        uow.modules.add_instance(adapter_instance)
        uow.modules.add_instance(workspace_instance)
        uow.flush()
        for index in configured_worker_indexes:
            worker = workers[index]
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
    return RuntimeFixture(
        task=task,
        workers=workers,
        adapter_instance=adapter_instance,
        workspace_instance=workspace_instance,
        execution=execution,
    )


def _scheduler(database: Database, *, monotonic_clock=None) -> Scheduler:
    return Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=LowestCostWorkerPolicy(),
        clock=lambda: NOW,
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
        monotonic_clock=monotonic_clock,
    )


def test_scheduler_records_finite_assignment_duration_and_safe_log(
    database: Database,
    workspace_id: UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = _seed(database, workspace_id, configured_worker_indexes=(1,))
    ticks = iter((10.0, 10.25))
    caplog.set_level(logging.INFO, logger="sidera_control_plane.application.scheduler")

    result = _scheduler(database, monotonic_clock=lambda: next(ticks)).run_once(limit=1)

    assert result.assigned == 1
    with database.session() as session:
        event = session.scalar(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type == "ExecutionRuntimeBound",
            )
        )
    assert event is not None
    assert event.payload["assignment_duration_seconds"] == pytest.approx(0.25)
    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "scheduler_assignment_committed"
    )
    record_fields = record.__dict__
    assert record_fields["workspace_id"] == str(workspace_id)
    assert record_fields["execution_id"] == str(fixture.execution.id)
    assert record_fields["duration_seconds"] == pytest.approx(0.25)
    assert not hasattr(record, "config")


def test_scheduler_binds_exact_runtime_and_emits_runtime_audit_event(
    database: Database,
    workspace_id: UUID,
) -> None:
    fixture = _seed(database, workspace_id, configured_worker_indexes=(1,))

    result = _scheduler(database).run_once(limit=1)

    assert result.examined == 1
    assert result.assigned == 1
    with database.session() as session:
        binding = session.scalar(
            select(ExecutionBindingModel).where(
                ExecutionBindingModel.execution_id == fixture.execution.id
            )
        )
        events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type.in_(
                    (
                        "ExecutionRuntimeBound",
                        "ExecutionLeased",
                        "LeaseGranted",
                        "WorkerCapacityReserved",
                    )
                ),
            )
        ).all()
        active_block = session.scalar(
            select(SchedulingBlockModel).where(
                SchedulingBlockModel.execution_id == fixture.execution.id,
                SchedulingBlockModel.status == "active",
            )
        )
    assert binding is not None
    assert binding.worker_id == fixture.workers[1].id
    assert binding.adapter_module_id == fixture.adapter_instance.id
    assert binding.workspace_module_id == fixture.workspace_instance.id
    assert binding.adapter_config_version == 1
    assert binding.workspace_config_version == 1
    assert binding.fencing_token == 1
    assert binding.capability_snapshot["resume"] == "durable"
    assert {event.event_type for event in events} == {
        "ExecutionRuntimeBound",
        "ExecutionLeased",
        "LeaseGranted",
        "WorkerCapacityReserved",
    }
    assert active_block is None


def test_scheduler_keeps_execution_queued_and_records_missing_runtime_block(
    database: Database,
    workspace_id: UUID,
) -> None:
    fixture = _seed(database, workspace_id, configured_worker_indexes=())

    result = _scheduler(database).run_once(limit=1)

    assert result.examined == 1
    assert result.assigned == 0
    assert result.deferred == 1
    with SqlAlchemyUnitOfWork(database) as uow:
        execution = uow.executions.get(fixture.execution.id, workspace_id)
        assert execution is not None
        assert execution.status is ExecutionStatus.QUEUED
    with database.session() as session:
        block = session.scalar(
            select(SchedulingBlockModel).where(
                SchedulingBlockModel.execution_id == fixture.execution.id,
                SchedulingBlockModel.status == "active",
            )
        )
        binding = session.scalar(
            select(ExecutionBindingModel).where(
                ExecutionBindingModel.execution_id == fixture.execution.id
            )
        )
    assert block is not None
    assert block.reason_code == "no_eligible_worker"
    assert block.reason_details["candidate_issue_counts"] == {
        "runtime_configuration_missing": 2
    }
    assert binding is None


def test_scheduler_excludes_missing_runtime_and_uses_configured_candidate(
    database: Database,
    workspace_id: UUID,
) -> None:
    fixture = _seed(database, workspace_id, configured_worker_indexes=(1,))

    result = _scheduler(database).run_once(limit=1)

    assert result.assigned == 1
    with SqlAlchemyUnitOfWork(database) as uow:
        execution = uow.executions.get(fixture.execution.id, workspace_id)
        assert execution is not None
        assert execution.assigned_worker_id == fixture.workers[1].id


def test_scheduler_records_adapter_policy_block_when_forbidden_clears_candidates(
    database: Database,
    workspace_id: UUID,
) -> None:
    fixture = _seed(
        database,
        workspace_id,
        configured_worker_indexes=(0, 1),
        forbidden_adapters=("opencode",),
    )

    result = _scheduler(database).run_once(limit=1)

    assert result.assigned == 0
    assert result.deferred == 1
    with database.session() as session:
        block = session.scalar(
            select(SchedulingBlockModel).where(
                SchedulingBlockModel.execution_id == fixture.execution.id,
                SchedulingBlockModel.status == "active",
            )
        )
    assert block is not None
    assert block.reason_code == "adapter_policy_rejected"


def test_scheduler_retries_after_final_runtime_revalidation_failure(
    database: Database,
    workspace_id: UUID,
) -> None:
    fixture = _seed(database, workspace_id, configured_worker_indexes=(0, 1))
    resolver = RevalidationResolver(fixture, fixture.workers[1].id)
    scheduler = Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=LowestCostWorkerPolicy(),
        clock=lambda: NOW,
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
        runtime_resolver=resolver,
    )

    result = scheduler.run_once(limit=1)

    assert result.assigned == 1
    assert resolver.calls == [fixture.workers[1].id, fixture.workers[0].id]
    with SqlAlchemyUnitOfWork(database) as uow:
        execution = uow.executions.get(fixture.execution.id, workspace_id)
        assert execution is not None
        assert execution.assigned_worker_id == fixture.workers[0].id
