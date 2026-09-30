from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.handoff_gate import (
    HandoffGateOperation,
    HandoffGateResult,
)
from sidera_control_plane.application.handoff_target import (
    HandoffActivationGate,
    HandoffTargetError,
    HandoffTargetService,
)
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffProposal,
    HandoffProposalPayload,
    HandoffSourceChannel,
    HandoffTransferMode,
    HandoffTransition,
    HandoffTransitionStatus,
    HandoffWriterState,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
    WorkspaceLifecycle,
    workspace_manifest_digest,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")


class MemoryRepository:
    def __init__(self, values: tuple[Any, ...] = ()) -> None:
        self.values = {value.id: value for value in values}
        self.added: list[object] = []
        self.saved: list[tuple[object, int]] = []

    def get(self, value_id: UUID, workspace_id: UUID, **_kwargs: object) -> Any | None:
        value = self.values.get(value_id)
        return value if value is not None and value.workspace_id == workspace_id else None

    def add(self, value: Any) -> None:
        value_id = value.id
        if value_id in self.values:
            raise AssertionError(f"duplicate value: {value_id}")
        self.values[value_id] = value
        self.added.append(value)

    def save(self, value: Any, previous_version: int) -> None:
        self.values[value.id] = value
        self.saved.append((value, previous_version))


class MemoryTransitionRepository(MemoryRepository):
    def get(self, transition_id: UUID, workspace_id: UUID, **_kwargs: object) -> object | None:
        return super().get(transition_id, workspace_id)


class MemoryUnitOfWork:
    def __init__(
        self,
        transition: HandoffTransition,
        source_request: ExecutionRequest,
        source_execution: Execution,
        target_worker: Worker,
    ) -> None:
        self.handoff_transitions = MemoryTransitionRepository((transition,))
        self.execution_requests = MemoryRepository((source_request,))
        self.executions = MemoryRepository((source_execution,))
        self.workers = MemoryRepository((target_worker,))
        self.flush_count = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def flush(self) -> None:
        self.flush_count += 1


def _worker(*, worker_id: UUID | None = None, name: str = "target") -> Worker:
    return Worker.register(
        worker_id=worker_id or uuid4(),
        workspace_id=WORKSPACE_ID,
        adapter_type="fake",
        display_name=name,
        capabilities=frozenset({"python", "handoff"}),
        capacity=1,
        cost_per_hour_microunits=100,
        startup_latency_ms=10,
        now=NOW,
    )


def _target_runtime(worker: Worker) -> dict[str, object]:
    return {
        "worker_id": str(worker.id),
        "adapter_type": worker.adapter_type,
        "adapter_module_instance_id": str(uuid4()),
        "adapter_module_config_version": 2,
        "workspace_module_instance_id": str(uuid4()),
        "workspace_module_config_version": 3,
        "adapter_definition_id": str(uuid4()),
        "workspace_definition_id": str(uuid4()),
        "capability_snapshot_hash": "b" * 64,
        "resolution_facts_hash": "c" * 64,
    }


def _source_manifest(
    *,
    execution: Execution,
    manifest_id: UUID,
    fencing_token: int = 7,
) -> ExecutionWorkspaceManifest:
    manifest = ExecutionWorkspaceManifest.create(
        id=manifest_id,
        workspace_id=WORKSPACE_ID,
        task_id=execution.task_id,
        execution_id=execution.id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision="a" * 40,
                branch="sidera/source",
                current_head="a" * 40,
            ),
        ),
        location_ref="fake-workspace://handoff/source",
        retention_seconds=3600,
        fencing_token=fencing_token,
        created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    manifest.freeze(now=NOW)
    return manifest


def _proposal_and_transition(
    *,
    source_execution: Execution,
    source_manifest: ExecutionWorkspaceManifest,
    source_session_id: UUID,
    target_worker: Worker,
) -> tuple[HandoffProposal, HandoffTransition]:
    target_runtime = _target_runtime(target_worker)
    payload = HandoffProposalPayload(
        proposal_schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=source_execution.task_id,
        subtask_id=source_execution.task_id,
        worker_item_id=source_execution.worker_item_id or uuid4(),
        source_execution_id=source_execution.id,
        source_execution_version=source_execution.version,
        source_session_id=source_session_id,
        source_session_version=3,
        source_session_last_sequence=3,
        source_worker_id=uuid4(),
        source_worker_version=2,
        source_runtime_snapshot={"adapter_type": "fake"},
        source_workspace_manifest_id=source_manifest.id,
        source_workspace_manifest_version=source_manifest.version,
        source_context_package_id=uuid4(),
        source_context_package_hash="d" * 64,
        source_worker_context_version=1,
        observed_plan_snapshot_id=uuid4(),
        observed_plan_generation=4,
        observed_plan_head_version=5,
        target_worker_id=target_worker.id,
        target_worker_version=target_worker.version,
        target_runtime_snapshot=target_runtime,
        target_capability_snapshot_hash="b" * 64,
        target_policy_reason={"policy": "lowest-cost-v1"},
        target_policy_facts={"eligible": True},
        transfer_mode=HandoffTransferMode.REUSE_EXACT,
        handoff_reason="move the active worker",
        handoff_instruction="continue from the frozen workspace",
        input_plan={"mode": "safe"},
        impact_summary={"scope": "worker_item"},
    )
    proposal = HandoffProposal.create(
        id=uuid4(),
        payload=payload,
        created_by_type="user",
        created_by_id="unit",
        source_channel=HandoffSourceChannel.WEB,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    proposal.mark_confirmed(now=NOW)
    transition = HandoffTransition.create(
        id=uuid4(),
        proposal=proposal,
        confirmed_by_type="user",
        confirmed_by_id="unit",
        confirmed_at=NOW,
    )
    transition.start(now=NOW)
    transition.advance(HandoffPhase.STOP_SOURCE, now=NOW)
    transition.record_checkpoint(
        sequence=3,
        facts={
            "transition_id": str(transition.id),
            "source_session_id": str(source_session_id),
            "accepted_sequence": 3,
            "quiescent": True,
            "workspace_digest": workspace_manifest_digest(source_manifest),
        },
    )
    transition.advance(HandoffPhase.FREEZE_SOURCE_WORKSPACE, now=NOW)
    transition.record_stop(
        sequence=3,
        facts={
            "source_session_id": str(source_session_id),
            "accepted_sequence": 3,
            "stopped": True,
        },
    )
    transition.record_workspace_freeze(
        operation_id=uuid4(),
        facts={"manifest_id": str(source_manifest.id)},
    )
    transition.advance(HandoffPhase.CREATE_TARGET_EXECUTION, now=NOW)
    return proposal, transition


def _fixture() -> tuple[
    MemoryUnitOfWork,
    HandoffProposal,
    HandoffTransition,
    ExecutionRequest,
    Execution,
    ExecutionWorkspaceManifest,
    Worker,
]:
    worker_item_id = uuid4()
    source_execution = Execution.create(
        execution_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=uuid4(),
        worker_item_id=worker_item_id,
        lineage_id=uuid4(),
        attempt=1,
        max_attempts=3,
        purpose="implement the task",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=uuid4(),
        not_before=NOW,
        adapter_preferences=("fake",),
        adapter_forbidden=("unsafe",),
    )
    source_execution = replace(
        source_execution,
        status=ExecutionStatus.CANCELED,
        last_fencing_token=7,
        finished_at=NOW,
    )
    source_manifest = _source_manifest(
        execution=source_execution,
        manifest_id=uuid4(),
    )
    source_request = ExecutionRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        planning_request_id=uuid4(),
        proposal_revision_id=uuid4(),
        plan_snapshot_id=uuid4(),
        plan_generation=4,
        root_task_id=source_execution.task_id,
        subtask_id=source_execution.task_id,
        worker_item_id=worker_item_id,
        worker_item_key="worker-a",
        intent_key="worker-a",
        purpose=source_execution.purpose,
        capability_requirements=source_execution.capability_requirements,
        adapter_preferences=source_execution.adapter_preferences,
        adapter_forbidden=source_execution.adapter_forbidden,
        budget_class="standard",
        not_before=NOW,
        max_attempts=3,
        expected_artifact_refs=("artifact_type:diff",),
        expected_evidence_refs=("acceptance:done",),
        expected_outputs=("diff",),
        now=NOW,
    )
    source_request.mark_ready(now=NOW)
    source_request.materialize(
        execution_lineage_id=source_execution.lineage_id,
        first_execution_id=source_execution.id,
        now=NOW,
    )
    target_worker = _worker()
    proposal, transition = _proposal_and_transition(
        source_execution=source_execution,
        source_manifest=source_manifest,
        source_session_id=uuid4(),
        target_worker=target_worker,
    )
    uow = MemoryUnitOfWork(
        transition,
        source_request,
        source_execution,
        target_worker,
    )
    return (
        uow,
        proposal,
        transition,
        source_request,
        source_execution,
        source_manifest,
        target_worker,
    )


def test_create_target_is_new_held_lineage_and_idempotent() -> None:
    (
        uow,
        proposal,
        transition,
        source_request,
        source_execution,
        source_manifest,
        target_worker,
    ) = _fixture()
    service = HandoffTargetService(lambda: uow)

    identity = service.create_target(
        transition=transition,
        proposal=proposal,
        source_request=source_request,
        source_execution=source_execution,
        source_manifest=source_manifest,
        now=NOW,
    )
    repeated = service.create_target(
        transition=transition,
        proposal=proposal,
        source_request=source_request,
        source_execution=source_execution,
        source_manifest=source_manifest,
        now=NOW,
    )

    assert repeated == identity
    assert identity.execution_request_id != source_request.id
    assert identity.execution_id != source_execution.id
    assert identity.lineage_id == identity.execution_id
    assert identity.anticipated_fencing_token == source_manifest.fencing_token + 1
    assert len(uow.execution_requests.added) == 1
    assert len(uow.executions.added) == 1
    assert len(uow.handoff_transitions.saved) == 1

    target_request = uow.execution_requests.values[identity.execution_request_id]
    target_execution = uow.executions.values[identity.execution_id]
    assert isinstance(target_request, ExecutionRequest)
    assert isinstance(target_execution, Execution)
    assert target_request.status is ExecutionRequestStatus.MATERIALIZED
    assert target_request.purpose == "handoff"
    assert target_request.plan_generation == source_request.plan_generation
    assert target_request.worker_item_id == source_request.worker_item_id
    assert target_request.handoff_transition_id == transition.id
    assert target_request.source_execution_id == source_execution.id
    assert target_request.source_session_id == transition.source_session_id
    assert target_request.execution_lineage_id == identity.lineage_id
    assert target_request.first_execution_id == identity.execution_id
    assert target_execution.status is ExecutionStatus.QUEUED
    assert target_execution.pinned_worker_id == target_worker.id
    assert target_execution.last_fencing_token == source_manifest.fencing_token
    assert target_execution.handoff_transition_id == transition.id
    assert target_execution.source_execution_id == source_execution.id
    assert target_execution.source_session_id == transition.source_session_id
    assert target_execution.active_lease_id is None
    assert transition.target_execution_id == identity.execution_id
    assert source_execution.handoff_transition_id is None
    assert source_execution.version == 1


def test_create_target_allows_target_heartbeat_version_to_advance() -> None:
    (
        uow,
        proposal,
        transition,
        source_request,
        source_execution,
        source_manifest,
        target_worker,
    ) = _fixture()
    uow.workers.values[target_worker.id] = replace(
        target_worker,
        version=target_worker.version + 1,
    )

    identity = HandoffTargetService(lambda: uow).create_target(
        transition=transition,
        proposal=proposal,
        source_request=source_request,
        source_execution=source_execution,
        source_manifest=source_manifest,
        now=NOW,
    )

    assert identity.execution_id != source_execution.id


def test_target_creation_rejects_unfrozen_source() -> None:
    (
        uow,
        proposal,
        transition,
        source_request,
        source_execution,
        source_manifest,
        _target_worker,
    ) = _fixture()
    unfrozen = replace(source_manifest, lifecycle=WorkspaceLifecycle.ACTIVE)

    with pytest.raises(HandoffTargetError, match="frozen"):
        HandoffTargetService(lambda: uow).create_target(
            transition=transition,
            proposal=proposal,
            source_request=source_request,
            source_execution=source_execution,
            source_manifest=unfrozen,
            now=NOW,
        )


def test_activation_gate_preserves_authoritative_gate_failure() -> None:
    (
        _uow,
        _proposal,
        transition,
        _source_request,
        _source_execution,
        _source_manifest,
        target_worker,
    ) = _fixture()
    target_execution = Execution.create(
        execution_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=transition.worker_item_id,
        worker_item_id=transition.worker_item_id,
        lineage_id=uuid4(),
        attempt=1,
        max_attempts=1,
        purpose="handoff",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=target_worker.id,
        not_before=NOW,
        source_execution_id=transition.source_execution_id,
        source_session_id=transition.source_session_id,
        handoff_transition_id=transition.id,
        initial_fencing_token=7,
    )
    blocked = HandoffGateResult(
        allowed=False,
        blocking_code="handoff_transition_mismatch",
        transition_id=transition.id,
        status=HandoffTransitionStatus.RUNNING,
        phase=HandoffPhase.CREATE_TARGET_EXECUTION,
        writer_state=HandoffWriterState.WRITER_NONE,
        target_execution_id=target_execution.id,
    )

    result = HandoffActivationGate().evaluate(
        execution=target_execution,
        transition=transition,
        operation=HandoffGateOperation.COORDINATOR_ASSIGN_TARGET,
        handoff_gate=blocked,
    )

    assert result == blocked


def test_activation_gate_rejects_normal_schedule_and_mismatched_target() -> None:
    (
        _uow,
        _proposal,
        transition,
        _source_request,
        _source_execution,
        _source_manifest,
        target_worker,
    ) = _fixture()
    target_execution = Execution.create(
        execution_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=transition.worker_item_id,
        worker_item_id=transition.worker_item_id,
        lineage_id=uuid4(),
        attempt=1,
        max_attempts=1,
        purpose="handoff",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=target_worker.id,
        not_before=NOW,
        source_execution_id=transition.source_execution_id,
        source_session_id=transition.source_session_id,
        handoff_transition_id=transition.id,
        initial_fencing_token=7,
    )
    gate = HandoffActivationGate()

    normal = gate.evaluate(
        execution=target_execution,
        transition=transition,
        operation=HandoffGateOperation.NORMAL_SCHEDULE,
    )
    stale = gate.evaluate(
        execution=target_execution,
        transition=transition,
        operation=HandoffGateOperation.COORDINATOR_ASSIGN_TARGET,
        handoff_gate=HandoffGateResult(
            allowed=True,
            blocking_code=None,
            transition_id=transition.id,
            status=HandoffTransitionStatus.RUNNING,
            phase=HandoffPhase.START_TARGET_SESSION,
            writer_state=HandoffWriterState.TARGET_STARTING,
            target_execution_id=uuid4(),
        ),
    )

    assert normal.allowed is False
    assert normal.blocking_code == "handoff_in_progress"
    assert stale.allowed is False
    assert stale.blocking_code == "handoff_target_stale"
