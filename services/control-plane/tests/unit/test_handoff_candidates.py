from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.handoff_candidates import (
    HandoffCandidateBlocked,
    HandoffCandidateService,
)
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    RuntimeCandidateInspection,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPackageStatus,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperationStatus,
    GatewayOperationType,
)
from sidera_control_plane.domain.handoffs import (
    HandoffCandidateList,
    HandoffTransferMode,
)
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
from sidera_control_plane.domain.plan_transitions import PlanTransitionStatus
from sidera_control_plane.domain.worker import (
    Worker,
    WorkerDesiredState,
    WorkerStatus,
)
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.workspace import ExecutionWorkspaceManifest

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
FOREIGN_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
REVISION = "a" * 40
HASH = "b" * 64


def worker_profile(**overrides: object) -> WorkerAdapterCapabilityProfile:
    values: dict[str, object] = {
        "input": CapabilityInput.QUEUED,
        "idempotency": IdempotencyCapability.OPERATION_ID,
        "resume": ResumeCapability.DURABLE,
        "stop": StopCapability.EXACT_EXECUTION,
        "sandbox": SandboxCapability.FILESYSTEM,
        "stability": StabilityLevel.STABLE,
        "context_package_versions": (1,),
        "context_patch_versions": (),
        "handoff_checkpoint_mode": HandoffCheckpointMode.NATIVE,
        "handoff_checkpoint_versions": (1,),
        "handoff_session_context_versions": (1,),
        "handoff_writer_gate_versions": (1,),
        "handoff_inspection": True,
    }
    values.update(overrides)
    return WorkerAdapterCapabilityProfile(**values)  # type: ignore[arg-type]


def workspace_profile(**overrides: object) -> WorkspaceProviderCapabilityProfile:
    values: dict[str, object] = {
        "isolation": SandboxCapability.ISOLATED_RUNTIME,
        "writable": True,
        "git_worktree": True,
        "stability": StabilityLevel.STABLE,
        "workspace_handoff_versions": (1,),
        "workspace_transfer_modes": (HandoffTransferMode.REUSE_EXACT,),
        "handoff_inspection": True,
    }
    values.update(overrides)
    return WorkspaceProviderCapabilityProfile(**values)  # type: ignore[arg-type]


def make_worker(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    name: str,
    status: WorkerStatus = WorkerStatus.IDLE,
    desired_state: WorkerDesiredState = WorkerDesiredState.ACTIVE,
    current_load: int = 0,
    capacity: int = 1,
    heartbeat: datetime = NOW,
    adapter_type: str = "opencode",
) -> Worker:
    item = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type=adapter_type,
        display_name=name,
        capabilities=frozenset({"python"}),
        capacity=capacity,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        now=heartbeat,
    )
    return replace(
        item,
        status=status,
        desired_state=desired_state,
        current_load=current_load,
        last_heartbeat_at=heartbeat,
    )


def make_execution(worker_item_id: UUID) -> Execution:
    execution = Execution.create(
        execution_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=uuid4(),
        worker_item_id=worker_item_id,
        lineage_id=uuid4(),
        attempt=1,
        max_attempts=2,
        purpose="handoff candidate inspection",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
    )
    return replace(
        execution,
        status=ExecutionStatus.RUNNING,
        assigned_worker_id=uuid4(),
        started_at=NOW,
    )


def make_manifest(execution: Execution) -> ExecutionWorkspaceManifest:
    manifest = ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=execution.task_id,
        execution_id=execution.id,
        checkouts=(),
        location_ref="workspace-location-1",
        retention_seconds=3600,
        fencing_token=7,
        created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    return manifest


def make_runtime(
    worker: Worker,
    *,
    adapter: WorkerAdapterCapabilityProfile | None = None,
    workspace: WorkspaceProviderCapabilityProfile | None = None,
    trust_boundary: str = "workspace-boundary",
    repository_scope: str = "repo-scope",
    path_scope: str = "path-scope",
) -> ResolvedWorkerRuntime:
    return ResolvedWorkerRuntime(
        worker_id=worker.id,
        adapter_type=worker.adapter_type,
        adapter_module_instance_id=UUID("30000000-0000-0000-0000-000000000001"),
        adapter_module_config_version=1,
        workspace_module_instance_id=UUID("40000000-0000-0000-0000-000000000001"),
        workspace_module_config_version=1,
        adapter_definition_id=UUID("50000000-0000-0000-0000-000000000001"),
        workspace_definition_id=UUID("60000000-0000-0000-0000-000000000001"),
        capability_snapshot=adapter or worker_profile(),
        workspace_capability_snapshot=workspace or workspace_profile(),
        resolution_facts={
            "workspace_trust_boundary_hash": trust_boundary,
            "workspace_repository_scope_hash": repository_scope,
            "workspace_path_scope_hash": path_scope,
            "config_body": {"api_key": "must-not-leak"},
            "absolute_path": "/private/provider/path",
        },
    )


class FakeWorkers:
    def __init__(self, workers: tuple[Worker, ...]) -> None:
        self.workers = workers

    def get(self, worker_id: UUID, workspace_id: UUID, *, for_update: bool = False) -> Worker | None:
        return next(
            (
                worker
                for worker in self.workers
                if worker.id == worker_id and worker.workspace_id == workspace_id
            ),
            None,
        )

    def list_for_workspace(self, workspace_id: UUID) -> tuple[Worker, ...]:
        return self.workers


class FakeExecutions:
    def __init__(self, executions: tuple[Execution, ...]) -> None:
        self.executions = executions

    def list_for_worker_item(self, worker_item_id: UUID, workspace_id: UUID) -> tuple[Execution, ...]:
        return tuple(
            execution
            for execution in self.executions
            if execution.worker_item_id == worker_item_id
        )


class FakeSessions:
    def __init__(self, sessions: dict[UUID, tuple[ExecutionSession, ...]]) -> None:
        self.sessions = sessions

    def list_active_for_execution(
        self, execution_id: UUID, workspace_id: UUID
    ) -> tuple[ExecutionSession, ...]:
        return tuple(
            session
            for session in self.sessions.get(execution_id, ())
            if session.workspace_id == workspace_id
            and session.status is ExecutionSessionStatus.ACTIVE
        )

    def get_active_for_execution(
        self, execution_id: UUID, *, for_update: bool = False
    ) -> ExecutionSession | None:
        return next(iter(self.sessions.get(execution_id, ())), None)


class FakeRuntimeResolver:
    def __init__(self, runtimes: dict[UUID, ResolvedWorkerRuntime | None]) -> None:
        self.runtimes = runtimes
        self.calls: list[tuple[UUID, ...]] = []

    def inspect_candidates(
        self, uow: object, workers: Collection[Worker], execution: Execution
    ) -> tuple[RuntimeCandidateInspection, ...]:
        self.calls.append(tuple(worker.id for worker in workers))
        return tuple(
            RuntimeCandidateInspection(
                worker=worker,
                resolved=self.runtimes.get(worker.id),
                issue=None,
            )
            for worker in workers
        )


class FakeUow:
    def __init__(
        self,
        *,
        worker_item: WorkerItem,
        executions: tuple[Execution, ...],
        sessions: dict[UUID, tuple[ExecutionSession, ...]],
        workers: tuple[Worker, ...],
        runtimes: dict[UUID, ResolvedWorkerRuntime | None],
        manifest: object | None,
        package: object | None,
        plan_transitions: tuple[object, ...] = (),
        active_handoff: object | None = None,
        pending_input: object | None = None,
        gateway_operations: tuple[object, ...] = (),
    ) -> None:
        self.worker_items = SimpleNamespace(get=lambda item_id, workspace_id: worker_item)
        self.executions = FakeExecutions(executions)
        self.execution_requests = SimpleNamespace(
            get_effective_by_execution_lineage=lambda *_args, **_kwargs: None
        )
        self.sessions = FakeSessions(sessions)
        self.workers = FakeWorkers(workers)
        self.worker_runtime_configurations = SimpleNamespace()
        self.modules = SimpleNamespace()
        self.workspaces = SimpleNamespace(
            get_for_execution=lambda execution_id, workspace_id: manifest
        )
        self.context_packages = SimpleNamespace(
            get_for_consumer=lambda purpose, consumer_id, workspace_id, **_kwargs: package
        )
        self.plans = SimpleNamespace(
            get_head=lambda root_task_id, workspace_id: SimpleNamespace(
                active_snapshot_id=uuid4(), active_generation=1, version=1
            )
        )
        self.plan_transitions = SimpleNamespace(
            list_for_root=lambda root_task_id, workspace_id: plan_transitions
        )
        self.handoff_transitions = SimpleNamespace(
            get_active_for_worker_item=lambda item_id, workspace_id: active_handoff
        )
        self.execution_inputs = SimpleNamespace(
            get_pending_for_session=lambda session_id, workspace_id: pending_input
        )
        self.gateway_operations = SimpleNamespace(
            list_for_execution=lambda execution_id, workspace_id: gateway_operations
        )
        self.adapter_calls = 0
        self.workspace_provider_calls = 0
        self.runtime_resolver = FakeRuntimeResolver(runtimes)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


def make_fixture(
    *,
    targets: tuple[Worker, ...] = (),
    runtimes: dict[UUID, ResolvedWorkerRuntime | None] | None = None,
    sessions: tuple[ExecutionSession, ...] | None = None,
    plan_transitions: tuple[object, ...] = (),
    active_handoff: object | None = None,
    pending_input: object | None = None,
    gateway_operations: tuple[object, ...] = (),
) -> tuple[FakeUow, Worker, Execution, WorkerItem]:
    source = make_worker(name="Source")
    item = WorkerItem.create(
        item_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=uuid4(),
        title="Implement handoff",
        responsibility="Keep the execution moving",
        now=NOW,
    )
    execution = make_execution(item.id)
    execution = replace(execution, assigned_worker_id=source.id)
    session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        execution_id=execution.id,
        worker_id=source.id,
        adapter_session_id="source-session",
        now=NOW,
    )
    source_runtime = make_runtime(source)
    runtime_by_worker: dict[UUID, ResolvedWorkerRuntime | None] = {
        source.id: source_runtime
    }
    if runtimes is not None:
        runtime_by_worker.update(runtimes)
    all_workers = (source, *targets)
    uow = FakeUow(
        worker_item=item,
        executions=(execution,),
        sessions={execution.id: sessions if sessions is not None else (session,)},
        workers=all_workers,
        runtimes=runtime_by_worker,
        manifest=make_manifest(execution),
        package=SimpleNamespace(
            package_id=uuid4(),
            package_hash=HASH,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=execution.id,
            worker_item_id=item.id,
            context_version=item.context_version,
            status=ContextPackageStatus.ACTIVE,
        ),
        plan_transitions=plan_transitions,
        active_handoff=active_handoff,
        pending_input=pending_input,
        gateway_operations=gateway_operations,
    )
    return uow, source, execution, item


def inspect_fixture(uow: FakeUow) -> HandoffCandidateList:
    service = HandoffCandidateService(
        cast(Any, lambda: uow),
        policy=LowestCostWorkerPolicy(),
        runtime_resolver=uow.runtime_resolver,
        heartbeat_timeout=timedelta(seconds=45),
    )
    return service.inspect(
        workspace_id=WORKSPACE_ID,
        worker_item_id=cast(UUID, next(iter(uow.executions.executions)).worker_item_id),
        now=NOW,
    )


def test_no_or_ambiguous_active_source_session_blocks_list() -> None:
    uow, _, _, _ = make_fixture(sessions=())
    with pytest.raises(HandoffCandidateBlocked) as no_source:
        inspect_fixture(uow)
    assert no_source.value.reason_code == "handoff_source_not_active"

    uow, _, execution, _ = make_fixture(sessions=())
    uow.sessions.sessions[execution.id] = (
        ExecutionSession.start(
            session_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            execution_id=execution.id,
            worker_id=uuid4(),
            adapter_session_id="ambiguous-1",
            now=NOW,
        ),
        ExecutionSession.start(
            session_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            execution_id=execution.id,
            worker_id=uuid4(),
            adapter_session_id="ambiguous-2",
            now=NOW,
        ),
    )
    with pytest.raises(HandoffCandidateBlocked) as ambiguous:
        inspect_fixture(uow)
    assert ambiguous.value.reason_code == "handoff_source_ambiguous"


def test_source_is_excluded_and_recommended_candidate_is_first_deterministically() -> None:
    alpha = replace(make_worker(name="Alpha"), cost_per_hour_microunits=0)
    zulu = make_worker(name="Zulu")
    uow, source, _, _ = make_fixture(
        targets=(zulu, alpha),
        runtimes={alpha.id: make_runtime(alpha), zulu.id: make_runtime(zulu)},
    )

    first = inspect_fixture(uow)
    second = inspect_fixture(uow)

    assert first == second
    assert first.source_worker_id == source.id
    assert source.id not in {candidate.worker_id for candidate in first.candidates}
    assert first.recommended_worker_id == alpha.id
    assert first.candidates[0].worker_id == alpha.id
    assert first.candidates[0].policy_reason["policy"] == "lowest-cost-v1"
    assert first.candidates[0].policy_reason["selected"] is True
    assert "selected_score" in first.candidates[0].policy_reason
    assert all(candidate.eligible for candidate in first.candidates)


@pytest.mark.parametrize(
    ("name", "status", "desired_state", "current_load", "heartbeat", "code"),
    [
        ("Offline", WorkerStatus.OFFLINE, WorkerDesiredState.ACTIVE, 0, NOW, "handoff_target_unhealthy"),
        ("Draining", WorkerStatus.DRAINING, WorkerDesiredState.DRAINING, 0, NOW, "handoff_target_unhealthy"),
        ("Stale", WorkerStatus.IDLE, WorkerDesiredState.ACTIVE, 0, NOW - timedelta(minutes=2), "handoff_target_unhealthy"),
        ("Full", WorkerStatus.BUSY, WorkerDesiredState.ACTIVE, 1, NOW, "handoff_target_capacity"),
    ],
)
def test_worker_health_and_capacity_have_stable_ineligibility_codes(
    name: str,
    status: WorkerStatus,
    desired_state: WorkerDesiredState,
    current_load: int,
    heartbeat: datetime,
    code: str,
) -> None:
    target = make_worker(
        name=name,
        status=status,
        desired_state=desired_state,
        current_load=current_load,
        heartbeat=heartbeat,
    )
    uow, _, _, _ = make_fixture(
        targets=(target,), runtimes={target.id: make_runtime(target)}
    )
    result = cast(Any, inspect_fixture(uow))
    candidate = result.candidates[0]
    assert candidate.eligible is False
    assert candidate.ineligibility_code == code


def test_missing_and_changed_runtime_configurations_are_ineligible() -> None:
    missing = make_worker(name="Missing runtime")
    changed = make_worker(name="Changed runtime")
    uow, _, _, _ = make_fixture(
        targets=(missing, changed),
        runtimes={missing.id: None, changed.id: None},
    )
    result = cast(Any, inspect_fixture(uow))
    by_id = {candidate.worker_id: candidate for candidate in result.candidates}
    assert by_id[missing.id].ineligibility_code == "handoff_runtime_incompatible"
    assert by_id[changed.id].ineligibility_code == "handoff_runtime_incompatible"


@pytest.mark.parametrize(
    ("profile_change", "expected"),
    [
        (
            {"handoff_session_context_versions": ()},
            "handoff_context_binding_unsupported",
        ),
        (
            {"handoff_writer_gate_versions": ()},
            "handoff_writer_gate_unsupported",
        ),
        ({"handoff_inspection": False}, "handoff_runtime_incompatible"),
    ],
)
def test_target_adapter_capability_matrix(
    profile_change: dict[str, object], expected: str
) -> None:
    target = make_worker(name="Target")
    profile = worker_profile(**profile_change)
    uow, _, _, _ = make_fixture(
        targets=(target,), runtimes={target.id: make_runtime(target, adapter=profile)}
    )
    result = cast(Any, inspect_fixture(uow))
    assert result.candidates[0].ineligibility_code == expected


@pytest.mark.parametrize(
    ("profile_change", "expected"),
    [
        ({"handoff_checkpoint_mode": HandoffCheckpointMode.UNSUPPORTED}, "handoff_checkpoint_unsupported"),
        ({"handoff_checkpoint_versions": ()}, "handoff_checkpoint_unsupported"),
        ({"stop": StopCapability.SESSION}, "handoff_checkpoint_unsupported"),
        ({"handoff_inspection": False}, "handoff_checkpoint_unsupported"),
    ],
)
def test_source_adapter_capability_matrix_is_a_global_blocker(
    profile_change: dict[str, object], expected: str
) -> None:
    target = make_worker(name="Target")
    uow, source, _, _ = make_fixture(
        targets=(target,), runtimes={target.id: make_runtime(target)}
    )
    uow.runtime_resolver.runtimes[source.id] = make_runtime(
        source, adapter=worker_profile(**profile_change)
    )
    result = cast(Any, inspect_fixture(uow))
    assert expected in result.blocking_codes


@pytest.mark.parametrize(
    ("workspace_change", "expected"),
    [
        ({"workspace_handoff_versions": ()}, "handoff_workspace_incompatible"),
        ({"workspace_transfer_modes": ()}, "handoff_workspace_incompatible"),
        ({"handoff_inspection": False}, "handoff_workspace_incompatible"),
    ],
)
def test_workspace_provider_capability_matrix(
    workspace_change: dict[str, object], expected: str
) -> None:
    target = make_worker(name="Target")
    uow, _, _, _ = make_fixture(
        targets=(
            target,
        ),
        runtimes={
            target.id: make_runtime(
                target, workspace=workspace_profile(**workspace_change)
            )
        },
    )
    result = cast(Any, inspect_fixture(uow))
    assert result.candidates[0].ineligibility_code == expected


def test_workspace_scopes_must_match() -> None:
    trust_target = make_worker(name="Trust mismatch")
    repo_target = make_worker(name="Repository mismatch")
    path_target = make_worker(name="Path mismatch")
    uow, _, _, _ = make_fixture(
        targets=(trust_target, repo_target, path_target),
        runtimes={
            trust_target.id: make_runtime(trust_target, trust_boundary="different"),
            repo_target.id: make_runtime(repo_target, repository_scope="different"),
            path_target.id: make_runtime(path_target, path_scope="different"),
        },
    )
    result = cast(Any, inspect_fixture(uow))
    by_id = {candidate.worker_id: candidate for candidate in result.candidates}
    assert by_id[trust_target.id].ineligibility_code == "handoff_trust_boundary_mismatch"
    assert by_id[repo_target.id].ineligibility_code == "handoff_workspace_incompatible"
    assert by_id[path_target.id].ineligibility_code == "handoff_workspace_incompatible"


def test_cross_workspace_target_is_visible_but_ineligible() -> None:
    target = make_worker(name="Foreign", workspace_id=FOREIGN_WORKSPACE_ID)
    uow, _, _, _ = make_fixture(
        targets=(target,), runtimes={target.id: make_runtime(target)}
    )
    result = cast(Any, inspect_fixture(uow))
    assert result.candidates[0].ineligibility_code == "handoff_workspace_incompatible"


def test_active_transition_input_and_unknown_operation_are_reported() -> None:
    target = make_worker(name="Target")
    transition = SimpleNamespace()
    pending_input = SimpleNamespace(status="dispatching")
    operation = SimpleNamespace(
        operation_type=GatewayOperationType.STOP_SESSION,
        status=GatewayOperationStatus.UNKNOWN,
    )
    uow, _, _, _ = make_fixture(
        targets=(target,),
        runtimes={target.id: make_runtime(target)},
        plan_transitions=(SimpleNamespace(status=PlanTransitionStatus.CONVERGING),),
        active_handoff=transition,
        pending_input=pending_input,
        gateway_operations=(operation,),
    )
    result = cast(Any, inspect_fixture(uow))
    assert result.blocking_codes == (
        "handoff_already_active",
        "handoff_plan_transition_active",
        "handoff_input_in_flight",
        "handoff_operation_unknown",
    )


def test_runtime_snapshot_is_safe_and_inspection_has_no_side_effects() -> None:
    target = make_worker(name="Target")
    uow, _, _, _ = make_fixture(
        targets=(target,), runtimes={target.id: make_runtime(target)}
    )
    first = cast(Any, inspect_fixture(uow))
    second = cast(Any, inspect_fixture(uow))
    snapshot = first.candidates[0].runtime_snapshot
    assert first == second
    assert uow.adapter_calls == 0
    assert uow.workspace_provider_calls == 0
    assert snapshot is not None
    serialized = repr(snapshot)
    assert "must-not-leak" not in serialized
    assert "/private/provider/path" not in serialized
    assert "config_body" not in serialized
    assert "capability_snapshot_hash" in snapshot
    assert "workspace_module_instance_id" in snapshot
    assert "workspace_module_config_version" in snapshot
