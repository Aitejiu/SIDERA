from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from test_context_package_repository import _seed_context

from sidera_control_plane.application.handoff_candidates import (
    runtime_snapshot_for_handoff,
)
from sidera_control_plane.application.handoff_target import (
    HandoffTargetError,
)
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    WorkerRuntimeResolver,
)
from sidera_control_plane.domain.context_packages import (
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.handoffs import (
    HandoffProposalPayload,
    HandoffTransferMode,
)
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    ContextAssemblyRequestModel,
    ContextPackageModel,
    ExecutionBindingModel,
    ExecutionModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    HandoffProposalModel,
    HandoffTransitionModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanSnapshotModel,
    TaskModel,
    WorkerItemModel,
    WorkerModel,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_to_payload,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class HandoffFixture:
    transition_id: UUID
    target_execution_id: UUID
    target_worker_id: UUID
    target_manifest_id: UUID
    target_package_id: UUID
    target_package_hash: str
    source_execution_id: UUID


class FixedRuntimeResolver(WorkerRuntimeResolver):
    def __init__(self, runtime: ResolvedWorkerRuntime) -> None:
        self.runtime = runtime

    def lock_and_resolve(self, uow, worker, execution):
        del uow, worker, execution
        return self.runtime


class FailingPolicy:
    def select(self, execution, candidates):
        del execution, candidates
        raise AssertionError("handoff exact assignment must not call WorkerPolicy")


def _capability_profile() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
    )


def _seed(database: Database, workspace_id: UUID) -> tuple[HandoffFixture, ResolvedWorkerRuntime]:
    _context_request_id, context_revision_id, _, _ = _seed_context(
        database, workspace_id
    )
    task_id = uuid4()
    worker_item_id = uuid4()
    source_worker_id = uuid4()
    target_worker_id = uuid4()
    source_execution_id = uuid4()
    source_session_id = uuid4()
    source_manifest_id = uuid4()
    target_execution_id = uuid4()
    target_manifest_id = uuid4()
    source_package_id = uuid4()
    target_package_id = uuid4()
    source_package_request_id = uuid4()
    target_package_request_id = uuid4()
    proposal_id = uuid4()
    transition_id = uuid4()
    plan_snapshot_id = uuid4()
    adapter_definition_id = uuid4()
    workspace_definition_id = uuid4()
    adapter_instance_id = uuid4()
    workspace_instance_id = uuid4()
    location_ref = f"git-worktree://handoff/{task_id}"
    target_runtime = ResolvedWorkerRuntime(
        worker_id=target_worker_id,
        adapter_type="fake",
        adapter_module_instance_id=adapter_instance_id,
        adapter_module_config_version=1,
        workspace_module_instance_id=workspace_instance_id,
        workspace_module_config_version=1,
        adapter_definition_id=adapter_definition_id,
        workspace_definition_id=workspace_definition_id,
        capability_snapshot=_capability_profile(),
        resolution_facts={
            "worker_id": str(target_worker_id),
            "workspace_trust_boundary_hash": "a" * 64,
            "workspace_repository_scope_hash": "b" * 64,
            "workspace_path_scope_hash": "c" * 64,
        },
    )
    target_runtime_snapshot = runtime_snapshot_for_handoff(target_runtime)
    budget = ContextBudgetV1(
        profile_id=uuid4(),
        profile_version=1,
        max_sections=8,
        max_chars=20_000,
        max_input_tokens=4_000,
    )
    source_package = ContextPackageManifestV1.create(
        package_id=source_package_id,
        workspace_id=workspace_id,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=source_execution_id,
        worker_item_id=worker_item_id,
        work_context_revision_id=context_revision_id,
        context_version=1,
        control_plane_fact_snapshot={},
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        budget=budget,
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=f"source-package-{source_package_id}",
        created_at=NOW,
    )
    target_package = ContextPackageManifestV1.create(
        package_id=target_package_id,
        workspace_id=workspace_id,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=target_execution_id,
        worker_item_id=worker_item_id,
        work_context_revision_id=context_revision_id,
        context_version=1,
        control_plane_fact_snapshot={},
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        budget=budget,
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=f"target-package-{target_package_id}",
        created_at=NOW,
    )
    proposal_payload = HandoffProposalPayload(
        proposal_schema_version=1,
        workspace_id=workspace_id,
        root_task_id=task_id,
        subtask_id=task_id,
        worker_item_id=worker_item_id,
        source_execution_id=source_execution_id,
        source_execution_version=1,
        source_session_id=source_session_id,
        source_session_version=1,
        source_session_last_sequence=3,
        source_worker_id=source_worker_id,
        source_worker_version=1,
        source_runtime_snapshot={"worker_id": str(source_worker_id)},
        source_workspace_manifest_id=source_manifest_id,
        source_workspace_manifest_version=1,
        source_context_package_id=source_package_id,
        source_context_package_hash=source_package.package_hash,
        source_worker_context_version=1,
        observed_plan_snapshot_id=plan_snapshot_id,
        observed_plan_generation=1,
        observed_plan_head_version=1,
        target_worker_id=target_worker_id,
        target_worker_version=1,
        target_runtime_snapshot=target_runtime_snapshot,
        target_capability_snapshot_hash="d" * 64,
        target_policy_reason={"policy": "fixed"},
        target_policy_facts={"selected": True},
        transfer_mode=HandoffTransferMode.REUSE_EXACT,
        handoff_reason="test exact assignment",
        handoff_instruction="continue from the source checkpoint",
        input_plan={},
        impact_summary={},
    )
    with database.session() as session:
        session.add_all(
            [
                TaskModel(
                    id=task_id,
                    workspace_id=workspace_id,
                    title="Handoff target assignment",
                    description="Verify exact target assignment",
                    acceptance_criteria=["lease uses transferred fence"],
                    status="ready",
                    priority=50,
                    requested_by="integration",
                    version=1,
                    created_at=NOW,
                ),
                WorkerItemModel(
                    id=worker_item_id,
                    workspace_id=workspace_id,
                    root_task_id=task_id,
                    stable_key=f"handoff-worker-{worker_item_id.hex[:8]}",
                    title="Handoff worker item",
                    responsibility="Run the exact target",
                    status="active",
                    context_version=1,
                    acknowledged_context_version=1,
                    derived_from_worker_item_ids=[],
                    version=1,
                    created_at=NOW,
                ),
                WorkerModel(
                    id=source_worker_id,
                    workspace_id=workspace_id,
                    adapter_type="fake",
                    display_name="Source worker",
                    status="idle",
                    desired_state="active",
                    capabilities=["python"],
                    capacity=1,
                    current_load=0,
                    cost_per_hour_microunits=1,
                    startup_latency_ms=1,
                    last_heartbeat_at=NOW,
                    version=1,
                    created_at=NOW,
                ),
                WorkerModel(
                    id=target_worker_id,
                    workspace_id=workspace_id,
                    adapter_type="fake",
                    display_name="Target worker",
                    status="idle",
                    desired_state="active",
                    capabilities=["python"],
                    capacity=1,
                    current_load=0,
                    cost_per_hour_microunits=100,
                    startup_latency_ms=10,
                    last_heartbeat_at=NOW,
                    version=1,
                    created_at=NOW,
                ),
                ModuleDefinitionModel(
                    id=adapter_definition_id,
                    workspace_id=workspace_id,
                    module_type="worker_adapter",
                    provider="fake",
                    interface_version="sidera.worker-adapter.v1",
                    config_schema_version=1,
                    capabilities={},
                    lifecycle="active",
                    resource_version=1,
                    created_at=NOW,
                ),
                ModuleDefinitionModel(
                    id=workspace_definition_id,
                    workspace_id=workspace_id,
                    module_type="workspace_provider",
                    provider="git-worktree",
                    interface_version="sidera.workspace-provider.v1",
                    config_schema_version=1,
                    capabilities={},
                    lifecycle="active",
                    resource_version=1,
                    created_at=NOW,
                ),
                ModuleInstanceModel(
                    id=adapter_instance_id,
                    workspace_id=workspace_id,
                    definition_id=adapter_definition_id,
                    instance_name="handoff-target-adapter",
                    config_version=1,
                    config_namespace="adapter",
                    config={},
                    secret_refs=[],
                    lifecycle="active",
                    resource_version=1,
                    created_at=NOW,
                ),
                ModuleInstanceModel(
                    id=workspace_instance_id,
                    workspace_id=workspace_id,
                    definition_id=workspace_definition_id,
                    instance_name="handoff-target-workspace",
                    config_version=1,
                    config_namespace="workspace",
                    config={},
                    secret_refs=[],
                    lifecycle="active",
                    resource_version=1,
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=plan_snapshot_id,
                schema_version=1,
                workspace_id=workspace_id,
                root_task_id=task_id,
                generation=1,
                parent_snapshot_id=None,
                source_type="integration",
                source_ref="test:handoff-target",
                title="Handoff target plan",
                goal_summary="Assign an exact target",
                payload={},
                published_by="integration",
                published_at=NOW,
                created_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            [
                ExecutionModel(
                    id=source_execution_id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    lineage_id=source_execution_id,
                    attempt=1,
                    max_attempts=3,
                    purpose="source execution",
                    status="canceled",
                    capability_requirements=["python"],
                    adapter_preferences=["fake"],
                    adapter_forbidden=[],
                    pinned_worker_id=source_worker_id,
                    assigned_worker_id=None,
                    active_lease_id=None,
                    worker_item_id=worker_item_id,
                    last_fencing_token=7,
                    not_before=NOW,
                    failure_code=None,
                    started_at=NOW,
                    finished_at=NOW,
                    version=1,
                    stop_requested=False,
                    source_execution_id=None,
                    source_session_id=None,
                    handoff_transition_id=None,
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add(
            ExecutionSessionModel(
                id=source_session_id,
                workspace_id=workspace_id,
                execution_id=source_execution_id,
                worker_id=source_worker_id,
                adapter_session_id=f"source-session-{source_session_id.hex[:8]}",
                status="closed",
                last_sequence=3,
                last_activity_at=NOW,
                input_mode="queued",
                adapter_module_id=None,
                adapter_config_version=None,
                workspace_module_id=None,
                workspace_config_version=None,
                capability_snapshot=capability_profile_to_payload(_capability_profile()),
                process_ref=None,
                workspace_ref=None,
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ExecutionModel(
                id=target_execution_id,
                workspace_id=workspace_id,
                task_id=task_id,
                lineage_id=target_execution_id,
                attempt=1,
                max_attempts=3,
                purpose="handoff",
                status="queued",
                capability_requirements=["python"],
                adapter_preferences=["fake"],
                adapter_forbidden=[],
                pinned_worker_id=target_worker_id,
                assigned_worker_id=None,
                active_lease_id=None,
                worker_item_id=worker_item_id,
                last_fencing_token=7,
                not_before=NOW,
                failure_code=None,
                started_at=None,
                finished_at=None,
                version=1,
                stop_requested=False,
                source_execution_id=source_execution_id,
                source_session_id=source_session_id,
                handoff_transition_id=transition_id,
                created_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            [
                ExecutionWorkspaceModel(
                    id=source_manifest_id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    execution_id=source_execution_id,
                    checkouts=[],
                    location_ref=location_ref,
                    lifecycle="frozen",
                    retention_seconds=3600,
                    retain_until=NOW + timedelta(hours=1),
                    fencing_token=7,
                    frozen_at=NOW,
                    released_at=None,
                    version=1,
                    created_at=NOW,
                ),
                ExecutionWorkspaceModel(
                    id=target_manifest_id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    execution_id=target_execution_id,
                    checkouts=[],
                    location_ref=location_ref,
                    lifecycle="preparing",
                    retention_seconds=3600,
                    retain_until=NOW + timedelta(hours=1),
                    fencing_token=8,
                    frozen_at=None,
                    released_at=None,
                    version=1,
                    created_at=NOW,
                ),
            ]
        )
        session.add_all(
            [
                ContextAssemblyRequestModel(
                    id=source_package_request_id,
                    workspace_id=workspace_id,
                    purpose="execution",
                    consumer_type="worker",
                    consumer_id=source_execution_id,
                    worker_item_id=worker_item_id,
                    work_context_revision_id=context_revision_id,
                    context_version=1,
                    knowledge_search_scope={},
                    knowledge_filter_refs=[],
                    fact_scope_refs=[],
                    budget=budget.to_mapping(),
                    expires_at=NOW + timedelta(hours=1),
                    status="ready",
                    lease_owner=None,
                    lease_expires_at=None,
                    attempt=1,
                    next_attempt_at=None,
                    error_code=None,
                    request_fingerprint="a" * 64,
                    idempotency_key=f"source-request-{source_package_request_id}",
                    version=1,
                    created_at=NOW,
                ),
                ContextAssemblyRequestModel(
                    id=target_package_request_id,
                    workspace_id=workspace_id,
                    purpose="execution",
                    consumer_type="worker",
                    consumer_id=target_execution_id,
                    worker_item_id=worker_item_id,
                    work_context_revision_id=context_revision_id,
                    context_version=1,
                    knowledge_search_scope={},
                    knowledge_filter_refs=[],
                    fact_scope_refs=[],
                    budget=budget.to_mapping(),
                    expires_at=NOW + timedelta(hours=1),
                    status="ready",
                    lease_owner=None,
                    lease_expires_at=None,
                    attempt=1,
                    next_attempt_at=None,
                    error_code=None,
                    request_fingerprint="b" * 64,
                    idempotency_key=f"target-request-{target_package_request_id}",
                    version=1,
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                _package_model(
                    source_package,
                    source_package_request_id,
                    status="active",
                ),
                _package_model(
                    target_package,
                    target_package_request_id,
                    status="revoked",
                ),
            ]
        )
        session.add(
            HandoffProposalModel(
                id=proposal_id,
                workspace_id=workspace_id,
                root_task_id=task_id,
                subtask_id=task_id,
                worker_item_id=worker_item_id,
                source_execution_id=source_execution_id,
                source_execution_version=1,
                source_session_id=source_session_id,
                source_session_version=1,
                source_session_last_sequence=3,
                source_worker_id=source_worker_id,
                source_worker_version=1,
                source_runtime_snapshot={"worker_id": str(source_worker_id)},
                source_workspace_manifest_id=source_manifest_id,
                source_workspace_manifest_version=1,
                source_context_package_id=source_package_id,
                source_context_package_hash=source_package.package_hash,
                source_worker_context_version=1,
                observed_plan_snapshot_id=plan_snapshot_id,
                observed_plan_generation=1,
                observed_plan_head_version=1,
                target_worker_id=target_worker_id,
                target_worker_version=1,
                target_runtime_snapshot=target_runtime_snapshot,
                target_capability_snapshot_hash="d" * 64,
                target_policy_reason={"policy": "fixed"},
                target_policy_facts={"selected": True},
                transfer_mode="reuse_exact",
                handoff_reason="test exact assignment",
                handoff_instruction="continue from the source checkpoint",
                input_plan={},
                impact_summary={},
                payload=proposal_payload.to_mapping(),
                proposal_schema_version=1,
                proposal_hash=proposal_payload.content_hash,
                status="confirmed",
                stale_reason=None,
                version=1,
                created_by_type="user",
                created_by_id="integration",
                source_channel="web",
                expires_at=NOW + timedelta(hours=1),
                confirmed_at=NOW,
                cancelled_at=None,
                created_at=NOW,
            )
        )
        session.add(
            HandoffTransitionModel(
                id=transition_id,
                workspace_id=workspace_id,
                worker_item_id=worker_item_id,
                proposal_id=proposal_id,
                source_execution_id=source_execution_id,
                source_session_id=source_session_id,
                source_workspace_manifest_id=source_manifest_id,
                target_worker_id=target_worker_id,
                target_runtime_snapshot=target_runtime_snapshot,
                target_execution_id=target_execution_id,
                target_session_id=None,
                target_workspace_manifest_id=target_manifest_id,
                handoff_context_bundle_id=None,
                target_context_version=1,
                target_context_package_id=target_package_id,
                target_context_package_hash=target_package.package_hash,
                status="running",
                phase="start_target_session",
                writer_state="target_starting",
                source_checkpoint_sequence=3,
                source_checkpoint_facts={"quiescent": True},
                source_stop_sequence=3,
                source_stop_facts={"stopped": True},
                source_workspace_freeze_operation_id=uuid4(),
                source_workspace_freeze_facts={"fencing_token": 7},
                source_input_cutoff_sequence=3,
                failure_code=None,
                blocked_operation_id=None,
                logical_attempt=1,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=None,
                version=1,
                confirmed_by_type="user",
                confirmed_by_id="integration",
                confirmed_source_channel="web",
                confirmed_at=NOW,
                started_at=NOW,
                completed_at=None,
                created_at=NOW,
            )
        )
        session.commit()
    return (
        HandoffFixture(
            transition_id=transition_id,
            target_execution_id=target_execution_id,
            target_worker_id=target_worker_id,
            target_manifest_id=target_manifest_id,
            target_package_id=target_package_id,
            target_package_hash=target_package.package_hash,
            source_execution_id=source_execution_id,
        ),
        target_runtime,
    )


def _package_model(manifest, request_id: UUID, *, status: str) -> ContextPackageModel:
    return ContextPackageModel(
        id=manifest.package_id,
        workspace_id=manifest.workspace_id,
        assembly_request_id=request_id,
        purpose=manifest.purpose.value,
        consumer_type=manifest.consumer_type.value,
        consumer_id=manifest.consumer_id,
        worker_item_id=manifest.worker_item_id,
        work_context_revision_id=manifest.work_context_revision_id,
        context_version=manifest.context_version,
        control_plane_fact_snapshot={},
        artifact_refs=[],
        evidence_refs=[],
        budget=manifest.budget.to_mapping(),
        expires_at=manifest.expires_at,
        package_hash=manifest.package_hash,
        status=status,
        revoked_at=NOW if status == "revoked" else None,
        revocation_reason="awaiting target transfer" if status == "revoked" else None,
        revocation_scope="new_starts" if status == "revoked" else None,
        idempotency_key=manifest.idempotency_key,
        materialized_at=NOW,
        created_at=NOW,
    )


def _scheduler(
    database: Database,
    runtime: ResolvedWorkerRuntime,
) -> Scheduler:
    return Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=FailingPolicy(),
        clock=lambda: NOW,
        runtime_resolver=FixedRuntimeResolver(runtime),
        monotonic_clock=lambda: 1.0,
    )


def test_exact_assignment_waits_for_ready_transfer_and_package(
    database: Database,
    workspace_id: UUID,
) -> None:
    fixture, runtime = _seed(database, workspace_id)
    scheduler = _scheduler(database, runtime)

    normal = scheduler.run_once(limit=1)
    assert normal.assigned == 0
    assert normal.deferred == 1

    with pytest.raises(HandoffTargetError, match="workspace manifest"):
        scheduler.assign_handoff_target(
            workspace_id=workspace_id,
            transition_id=fixture.transition_id,
            expected_transition_version=1,
            now=NOW,
        )

    with database.session() as session:
        session.execute(
            update(ExecutionWorkspaceModel)
            .where(ExecutionWorkspaceModel.id == fixture.target_manifest_id)
            .values(lifecycle="ready")
        )
        session.commit()

    with pytest.raises(HandoffTargetError, match="context package"):
        scheduler.assign_handoff_target(
            workspace_id=workspace_id,
            transition_id=fixture.transition_id,
            expected_transition_version=1,
            now=NOW,
        )

    with database.session() as session:
        session.execute(
            update(ContextPackageModel)
            .where(ContextPackageModel.id == fixture.target_package_id)
            .values(
                status="active",
                revoked_at=None,
                revocation_reason=None,
                revocation_scope=None,
            )
        )
        session.commit()

    assignment = scheduler.assign_handoff_target(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=1,
        now=NOW,
    )
    repeated = scheduler.assign_handoff_target(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=1,
        now=NOW,
    )

    assert assignment == repeated
    assert assignment.execution_id == fixture.target_execution_id
    assert assignment.worker_id == fixture.target_worker_id
    assert assignment.fencing_token == 8
    with SqlAlchemyUnitOfWork(database) as uow:
        execution = uow.executions.get(fixture.target_execution_id, workspace_id)
        lease = uow.leases.get(assignment.lease_id, workspace_id)
        binding = uow.bindings.get_for_execution(fixture.target_execution_id, workspace_id)
    assert execution is not None
    assert execution.last_fencing_token == 8
    assert execution.active_lease_id == assignment.lease_id
    assert lease is not None
    assert lease.fencing_token == 8
    assert binding is not None
    assert binding.fencing_token == 8
    assert binding.worker_id == fixture.target_worker_id


def test_concurrent_exact_assignment_creates_one_lease_and_binding(
    database: Database,
    workspace_id: UUID,
) -> None:
    fixture, runtime = _seed(database, workspace_id)
    with database.session() as session:
        session.execute(
            update(ExecutionWorkspaceModel)
            .where(ExecutionWorkspaceModel.id == fixture.target_manifest_id)
            .values(lifecycle="ready")
        )
        session.execute(
            update(ContextPackageModel)
            .where(ContextPackageModel.id == fixture.target_package_id)
            .values(
                status="active",
                revoked_at=None,
                revocation_reason=None,
                revocation_scope=None,
            )
        )
        session.commit()

    def assign() -> object:
        return _scheduler(database, runtime).assign_handoff_target(
            workspace_id=workspace_id,
            transition_id=fixture.transition_id,
            expected_transition_version=1,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assignments = tuple(pool.map(lambda _: assign(), range(2)))

    assert assignments[0] == assignments[1]
    with database.session() as session:
        lease_count = session.scalar(
            select(func.count())
            .select_from(AssignmentLeaseModel)
            .where(AssignmentLeaseModel.execution_id == fixture.target_execution_id)
        )
        binding_count = session.scalar(
            select(func.count())
            .select_from(ExecutionBindingModel)
            .where(ExecutionBindingModel.execution_id == fixture.target_execution_id)
        )
    assert lease_count == 1
    assert binding_count == 1
