from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.handoff_candidates import HandoffCandidateService
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    RuntimeCandidateInspection,
)
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionStatus,
)
from sidera_control_plane.domain.handoffs import HandoffTransferMode
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    HandoffCheckpointMode,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker, WorkerStatus
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.workspace import ExecutionWorkspaceManifest
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    TaskModel,
    WorkerItemModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _worker_profile() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        handoff_checkpoint_mode=HandoffCheckpointMode.NATIVE,
        handoff_checkpoint_versions=(1,),
        handoff_session_context_versions=(1,),
        handoff_writer_gate_versions=(1,),
        handoff_inspection=True,
    )


def _workspace_profile() -> WorkspaceProviderCapabilityProfile:
    return WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
        workspace_handoff_versions=(1,),
        workspace_transfer_modes=(HandoffTransferMode.REUSE_EXACT,),
        handoff_inspection=True,
    )


class StaticRuntimeInspector:
    def __init__(self, runtimes: dict[UUID, ResolvedWorkerRuntime]) -> None:
        self._runtimes = runtimes

    def inspect_candidates(
        self,
        uow: object,
        workers: Collection[Worker],
        execution: Execution,
    ) -> tuple[RuntimeCandidateInspection, ...]:
        del uow, execution
        return tuple(
            RuntimeCandidateInspection(
                worker=worker,
                resolved=self._runtimes.get(worker.id),
                issue=None,
            )
            for worker in workers
        )


def _runtime(worker: Worker) -> ResolvedWorkerRuntime:
    return ResolvedWorkerRuntime(
        worker_id=worker.id,
        adapter_type=worker.adapter_type,
        adapter_module_instance_id=UUID("30000000-0000-0000-0000-000000000001"),
        adapter_module_config_version=1,
        workspace_module_instance_id=UUID("40000000-0000-0000-0000-000000000001"),
        workspace_module_config_version=1,
        adapter_definition_id=UUID("50000000-0000-0000-0000-000000000001"),
        workspace_definition_id=UUID("60000000-0000-0000-0000-000000000001"),
        capability_snapshot=_worker_profile(),
        resolution_facts={
            "workspace_trust_boundary_hash": "trust",
            "workspace_repository_scope_hash": "repository",
            "workspace_path_scope_hash": "paths",
        },
        workspace_capability_snapshot=_workspace_profile(),
    )


def _seed(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
) -> tuple[Task, WorkerItem, Execution, Worker, Worker, ExecutionWorkspaceManifest]:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Candidate query task",
        description="Inspect handoff targets",
        acceptance_criteria=("candidate facts are deterministic",),
        priority=50,
        requested_by="integration",
    )
    item = WorkerItem.create(
        item_id=uuid4(),
        workspace_id=workspace_id,
        root_task_id=task.id,
        title="Candidate WorkerItem",
        responsibility="Keep the task moving",
        now=NOW,
    )
    source = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="opencode",
        display_name="source",
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=10,
        startup_latency_ms=10,
        now=NOW,
    )
    source = replace(source, status=WorkerStatus.BUSY, current_load=1)
    target = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="opencode",
        display_name="target",
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        now=NOW,
    )
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task.id,
        worker_item_id=item.id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=2,
        purpose="candidate query",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
    )
    execution = replace(
        execution,
        status=ExecutionStatus.RUNNING,
        assigned_worker_id=source.id,
        started_at=NOW,
    )
    session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=workspace_id,
        execution_id=execution.id,
        worker_id=source.id,
        adapter_session_id="candidate-source-session",
        now=NOW,
        capability_snapshot=_worker_profile(),
    )
    manifest = ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=workspace_id,
        task_id=task.id,
        execution_id=execution.id,
        checkouts=(),
        location_ref="candidate-location",
        retention_seconds=3600,
        fencing_token=1,
        created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()

    with uow_factory() as uow:
        uow.tasks.add(task)
        uow.workers.add(source)
        uow.workers.add(target)
        uow.worker_items.add(item)
        # The execution/task/work-item graph contains a cycle elsewhere in
        # the metadata; persist the task-side parents before the execution.
        uow.flush()
        uow.executions.add(execution)
        uow.flush()
        uow.sessions.add(session)
        uow.workspaces.add(manifest)
    return task, item, execution, source, target, manifest


def _counts(database: object, models: tuple[type[object], ...]) -> tuple[int, ...]:
    counts: list[int] = []
    for model in models:
        with database.session() as session:  # type: ignore[attr-defined]
            counts.append(session.scalar(select(func.count()).select_from(model)) or 0)
    return tuple(counts)


def test_handoff_candidate_query_is_read_only_and_workspace_scoped(
    database: object,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
) -> None:
    _, item, execution, source, target, _ = _seed(uow_factory, workspace_id)
    runtime_inspector = StaticRuntimeInspector(
        {source.id: _runtime(source), target.id: _runtime(target)}
    )
    service = HandoffCandidateService(
        uow_factory,
        policy=LowestCostWorkerPolicy(),
        runtime_resolver=runtime_inspector,  # type: ignore[arg-type]
    )
    models = (
        TaskModel,
        WorkerModel,
        WorkerItemModel,
        ExecutionModel,
        ExecutionSessionModel,
        ExecutionWorkspaceModel,
    )
    before = _counts(database, models)
    first = service.inspect(
        workspace_id=workspace_id,
        worker_item_id=item.id,
        now=NOW,
    )
    second = service.inspect(
        workspace_id=workspace_id,
        worker_item_id=item.id,
        now=NOW,
    )
    after = _counts(database, models)

    assert first == second
    assert first.source_execution_id == execution.id
    assert first.source_worker_id == source.id
    assert first.recommended_worker_id is None
    assert [candidate.worker_id for candidate in first.candidates] == [target.id]
    assert first.candidates[0].eligible is True
    assert before == after

    with pytest.raises(NotFound):
        service.inspect(
            workspace_id=workspace_id,
            worker_item_id=UUID("99999999-9999-9999-9999-999999999999"),
            now=NOW,
        )
