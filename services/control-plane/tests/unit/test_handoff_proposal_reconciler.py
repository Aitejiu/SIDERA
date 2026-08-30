from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.handoff_proposal_reconciler import (
    HandoffProposalReconciler,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    RuntimeCandidateInspection,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPackageStatus,
    ContextPurpose,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.handoffs import (
    HandoffCandidateInspection,
    HandoffCandidateList,
    HandoffProposal,
    HandoffProposalPayload,
    HandoffProposalStatus,
    HandoffSourceChannel,
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
from sidera_control_plane.domain.worker import Worker, WorkerStatus

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("11000000-0000-0000-0000-000000000001")
HASH = "a" * 64


def _profile() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(1,),
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


def _worker(name: str, *, status: WorkerStatus, version: int = 1) -> Worker:
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        adapter_type="opencode",
        display_name=name,
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        now=NOW,
    )
    return replace(worker, status=status, version=version)


def _runtime(worker: Worker, *, marker: str = "v1") -> ResolvedWorkerRuntime:
    return ResolvedWorkerRuntime(
        worker_id=worker.id,
        adapter_type=worker.adapter_type,
        adapter_module_instance_id=UUID("31000000-0000-0000-0000-000000000001"),
        adapter_module_config_version=1,
        workspace_module_instance_id=UUID("41000000-0000-0000-0000-000000000001"),
        workspace_module_config_version=1,
        adapter_definition_id=UUID("51000000-0000-0000-0000-000000000001"),
        workspace_definition_id=UUID("61000000-0000-0000-0000-000000000001"),
        capability_snapshot=_profile(),
        workspace_capability_snapshot=_workspace_profile(),
        resolution_facts={
            "workspace_trust_boundary_hash": "trust",
            "workspace_repository_scope_hash": "repository",
            "workspace_path_scope_hash": "paths",
            "marker": marker,
        },
    )


class StaticCandidateService:
    def __init__(self, result: HandoffCandidateList) -> None:
        self.result = result

    def inspect_in_uow(self, _uow: ControlPlaneUnitOfWork, **_kwargs: Any) -> HandoffCandidateList:
        return self.result


class StaticRuntimeResolver:
    def __init__(self, runtimes: dict[UUID, ResolvedWorkerRuntime]) -> None:
        self.runtimes = runtimes

    def inspect_candidates(
        self,
        _uow: ControlPlaneUnitOfWork,
        workers: Collection[Worker],
        _execution: object,
    ) -> tuple[RuntimeCandidateInspection, ...]:
        return tuple(
            RuntimeCandidateInspection(worker=worker, resolved=self.runtimes.get(worker.id), issue=None)
            for worker in workers
        )


class MemoryProposalRepository:
    def __init__(self, proposal: HandoffProposal) -> None:
        self.proposal = proposal

    def get(self, proposal_id: UUID, workspace_id: UUID, *, for_update: bool = False) -> HandoffProposal | None:
        del for_update
        if self.proposal.id == proposal_id and self.proposal.payload.workspace_id == workspace_id:
            return replace(self.proposal)
        return None

    def save_status(self, proposal: HandoffProposal, previous_version: int) -> None:
        assert self.proposal.version == previous_version
        self.proposal = proposal


class MemoryEvents:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class ReconcileUnitOfWork:
    def __init__(self, proposal: HandoffProposal) -> None:
        payload = proposal.payload
        self.worker_item = SimpleNamespace(
            id=payload.worker_item_id,
            workspace_id=WORKSPACE_ID,
            root_task_id=payload.root_task_id,
            version=payload.impact_summary["worker_item_version"],
            context_version=payload.source_worker_context_version,
            status="active",
        )
        self.execution = SimpleNamespace(
            id=payload.source_execution_id,
            workspace_id=WORKSPACE_ID,
            task_id=payload.subtask_id,
            worker_item_id=payload.worker_item_id,
            version=payload.source_execution_version,
            status=ExecutionStatus.RUNNING,
        )
        self.session = SimpleNamespace(
            id=payload.source_session_id,
            workspace_id=WORKSPACE_ID,
            execution_id=payload.source_execution_id,
            worker_id=payload.source_worker_id,
            version=payload.source_session_version,
            last_sequence=payload.source_session_last_sequence,
            status=ExecutionSessionStatus.ACTIVE,
        )
        self.source = _worker("source", status=WorkerStatus.BUSY, version=payload.source_worker_version)
        self.source = replace(self.source, id=payload.source_worker_id)
        self.target = _worker("target", status=WorkerStatus.IDLE, version=payload.target_worker_version)
        self.target = replace(self.target, id=payload.target_worker_id)
        self.manifest = SimpleNamespace(
            id=payload.source_workspace_manifest_id,
            workspace_id=WORKSPACE_ID,
            execution_id=payload.source_execution_id,
            version=payload.source_workspace_manifest_version,
            lifecycle="active",
        )
        self.package = SimpleNamespace(
            package_id=payload.source_context_package_id,
            package_hash=payload.source_context_package_hash,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=payload.source_execution_id,
            worker_item_id=payload.worker_item_id,
            context_version=payload.source_worker_context_version,
            status=ContextPackageStatus.ACTIVE,
        )
        self.plan_head = SimpleNamespace(
            active_snapshot_id=payload.observed_plan_snapshot_id,
            active_generation=payload.observed_plan_generation,
            version=payload.observed_plan_head_version,
        )
        self.pending_input: object | None = None
        self.active_handoff: object | None = None
        self.plan_transition: object | None = None
        self.runtime_resolver = StaticRuntimeResolver({
            self.source.id: _runtime(self.source),
            self.target.id: _runtime(self.target),
        })
        self.worker_items = SimpleNamespace(get=lambda *_args, **_kwargs: self.worker_item)
        self.executions = SimpleNamespace(get=lambda *_args, **_kwargs: self.execution)
        self.sessions = SimpleNamespace(get=lambda *_args, **_kwargs: self.session)
        self.workers = SimpleNamespace(get=self._worker_get)
        self.workspaces = SimpleNamespace(get_for_execution=lambda *_args, **_kwargs: self.manifest)
        self.context_packages = SimpleNamespace(get=lambda *_args, **_kwargs: self.package, get_for_consumer=lambda *_args, **_kwargs: self.package)
        self.plans = SimpleNamespace(get_head=lambda *_args, **_kwargs: self.plan_head)
        self.plan_transitions = SimpleNamespace(list_for_root=lambda *_args, **_kwargs: (self.plan_transition,) if self.plan_transition else ())
        self.handoff_transitions = SimpleNamespace(get_active_for_worker_item=lambda *_args, **_kwargs: self.active_handoff)
        self.execution_inputs = SimpleNamespace(get_pending_for_session=lambda *_args, **_kwargs: self.pending_input)
        self.gateway_operations = SimpleNamespace(list_for_execution=lambda *_args, **_kwargs: ())
        self.handoff_proposals = MemoryProposalRepository(proposal)
        self.events = MemoryEvents()

    def _worker_get(self, worker_id: UUID, _workspace_id: UUID, **_kwargs: Any) -> Worker | None:
        return next((worker for worker in (self.source, self.target) if worker.id == worker_id), None)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _proposal() -> HandoffProposal:
    source_worker_id = UUID("71000000-0000-0000-0000-000000000001")
    target_worker_id = UUID("81000000-0000-0000-0000-000000000001")
    source_execution_id = UUID("91000000-0000-0000-0000-000000000001")
    source_session_id = UUID("92000000-0000-0000-0000-000000000001")
    source_manifest_id = UUID("93000000-0000-0000-0000-000000000001")
    package_id = UUID("94000000-0000-0000-0000-000000000001")
    root_task_id = UUID("95000000-0000-0000-0000-000000000001")
    subtask_id = UUID("96000000-0000-0000-0000-000000000001")
    item_id = UUID("97000000-0000-0000-0000-000000000001")
    payload = HandoffProposalPayload(
        proposal_schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=root_task_id,
        subtask_id=subtask_id,
        worker_item_id=item_id,
        source_execution_id=source_execution_id,
        source_execution_version=1,
        source_session_id=source_session_id,
        source_session_version=1,
        source_session_last_sequence=0,
        source_worker_id=source_worker_id,
        source_worker_version=1,
        source_runtime_snapshot={"worker_id": str(source_worker_id), "runtime": "v1"},
        source_workspace_manifest_id=source_manifest_id,
        source_workspace_manifest_version=1,
        source_context_package_id=package_id,
        source_context_package_hash=HASH,
        source_worker_context_version=1,
        observed_plan_snapshot_id=UUID("98000000-0000-0000-0000-000000000001"),
        observed_plan_generation=1,
        observed_plan_head_version=1,
        target_worker_id=target_worker_id,
        target_worker_version=1,
        target_runtime_snapshot={"worker_id": str(target_worker_id), "runtime": "v1"},
        target_capability_snapshot_hash=HASH,
        target_policy_reason={"policy": "lowest-cost-v1"},
        target_policy_facts={"eligible": True},
        transfer_mode=HandoffTransferMode.REUSE_EXACT,
        handoff_reason="source needs a replacement",
        handoff_instruction="continue from the accepted checkpoint",
        input_plan={"preserve": ["accepted-inputs"]},
        impact_summary={"worker_item_version": 1, "input_state_hash": "none"},
    )
    return HandoffProposal.create(
        id=UUID("99000000-0000-0000-0000-000000000001"),
        payload=payload,
        created_by_type="user",
        created_by_id="user:test",
        source_channel=HandoffSourceChannel.WEB,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def _reconciler(uow: ReconcileUnitOfWork) -> HandoffProposalReconciler:
    candidate = HandoffCandidateInspection(
        worker_id=uow.target.id,
        display_name=uow.target.display_name,
        adapter_type=uow.target.adapter_type,
        eligible=True,
        recommended=True,
        ineligibility_code=None,
        runtime_snapshot=dict(uow.handoff_proposals.proposal.payload.target_runtime_snapshot),
        capability_snapshot_hash=HASH,
        policy_reason={"policy": "lowest-cost-v1"},
        workspace_compatibility={"transfer_mode": "reuse_exact"},
    )
    candidates = HandoffCandidateList(
        workspace_id=WORKSPACE_ID,
        worker_item_id=uow.worker_item.id,
        source_execution_id=uow.execution.id,
        source_session_id=uow.session.id,
        source_worker_id=uow.source.id,
        source_worker_item_version=uow.worker_item.version,
        candidates=(candidate,),
        recommended_worker_id=uow.target.id,
        blocking_codes=(),
        inspected_at=NOW,
    )
    return HandoffProposalReconciler(
        lambda: cast(ControlPlaneUnitOfWork, uow),
        candidate_service=StaticCandidateService(candidates),
        runtime_resolver=cast(Any, uow.runtime_resolver),
    )


def test_unchanged_prepared_proposal_remains_prepared_without_event() -> None:
    proposal = _proposal()
    uow = ReconcileUnitOfWork(proposal)

    result = _reconciler(uow).reconcile(proposal.id, WORKSPACE_ID, NOW)

    assert result.status is HandoffProposalStatus.PREPARED
    assert not uow.events.events


def test_execution_change_is_staled_once_and_is_idempotent() -> None:
    proposal = _proposal()
    uow = ReconcileUnitOfWork(proposal)
    uow.execution.version += 1
    reconciler = _reconciler(uow)

    first = reconciler.reconcile(proposal.id, WORKSPACE_ID, NOW)
    second = reconciler.reconcile(proposal.id, WORKSPACE_ID, NOW)

    assert first.status is HandoffProposalStatus.STALE
    assert first.stale_reason == "source_execution_changed"
    assert second == first
    assert [event.event_type for event in uow.events.events] == ["HandoffProposalStale"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("sequence", "source_sequence_changed"),
        ("worker_item_context", "worker_item_context_changed"),
        ("source_worker", "source_worker_changed"),
        ("workspace", "source_workspace_changed"),
        ("plan", "plan_head_changed"),
        ("input", "input_changed"),
        ("target_worker", "target_worker_changed"),
        ("active_handoff", "handoff_already_active"),
    ],
)
def test_each_authoritative_base_change_stales_the_proposal_once(
    mutation: str, reason: str
) -> None:
    proposal = _proposal()
    uow = ReconcileUnitOfWork(proposal)
    if mutation == "sequence":
        uow.session.last_sequence += 1
    elif mutation == "worker_item_context":
        uow.worker_item.context_version += 1
    elif mutation == "source_worker":
        uow.source.version += 1
    elif mutation == "workspace":
        uow.manifest.version += 1
    elif mutation == "plan":
        uow.plan_head.version += 1
    elif mutation == "input":
        uow.pending_input = SimpleNamespace(id=uuid4(), version=1, status="accepted")
    elif mutation == "target_worker":
        uow.target.version += 1
    else:
        uow.active_handoff = object()

    result = _reconciler(uow).reconcile(proposal.id, WORKSPACE_ID, NOW)

    assert result.status is HandoffProposalStatus.STALE
    assert result.stale_reason == reason
    assert len(uow.events.events) == 1


def test_ttl_expiry_becomes_expired_once() -> None:
    proposal = _proposal()
    uow = ReconcileUnitOfWork(proposal)
    reconciler = _reconciler(uow)
    now = proposal.expires_at

    first = reconciler.reconcile(proposal.id, WORKSPACE_ID, now)
    second = reconciler.reconcile(proposal.id, WORKSPACE_ID, now + timedelta(minutes=1))

    assert first.status is HandoffProposalStatus.EXPIRED
    assert second == first
    assert [event.event_type for event in uow.events.events] == ["HandoffProposalExpired"]
