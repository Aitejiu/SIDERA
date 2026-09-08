"""Reusable Adapter v1 handoff conformance checks.

The checks deliberately exercise only the public WorkerAdapter protocol. A
future adapter can provide a small factory that returns the same fixture and
reuse these source and target role checks without changing the handoff core.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import WorkerAdapter
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextMaterializationV1,
    ContextMetadata,
    ContextPurpose,
    ControlPlaneFactSection,
    SystemConstraintSection,
    WorkContextSection,
    context_package_reference,
)
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import HandoffInspectionOutcome
from sidera_control_plane.domain.modules import (
    HandoffCheckpointMode,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
    workspace_manifest_digest,
)

HASH = "c" * 64
BASE_REVISION = "b" * 40
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class HandoffAdapterFixture:
    """Adapter instance and two v2 execution records for one contract run."""

    adapter: WorkerAdapter
    probe_config: Mapping[str, object]
    source_spec: ExecutionSpec
    source_workspace: ExecutionWorkspaceManifest
    target_spec: ExecutionSpec
    target_workspace: ExecutionWorkspaceManifest


class HandoffAdapterFactory(Protocol):
    """Build one adapter and its source/target execution fixture."""

    name: str

    def create(self, root: Path) -> HandoffAdapterFixture:
        raise NotImplementedError


def build_handoff_fixture(
    adapter: WorkerAdapter | None,
    root: Path,
    *,
    probe_config: Mapping[str, object] | None = None,
    adapter_factory: Callable[[ContextMaterializationV1], WorkerAdapter] | None = None,
) -> HandoffAdapterFixture:
    """Create stable, provider-neutral source and held-target inputs."""

    workspace_id = uuid4()
    task_id = uuid4()
    source_execution_id = uuid4()
    target_execution_id = uuid4()
    source_context = _context_package(source_execution_id)
    target_context = _context_package(
        target_execution_id,
        handoff_instruction=(
            "Resume the transferred WorkerItem from the captured checkpoint. "
            "Preserve the existing task topology."
        ),
    )
    source_spec = _execution_spec(
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=source_execution_id,
        worker_id=uuid4(),
        fencing_token=7,
        context_package=source_context,
    )
    target_spec = _execution_spec(
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=target_execution_id,
        worker_id=uuid4(),
        fencing_token=8,
        context_package=target_context,
    )
    source_workspace = _workspace_manifest(source_spec)
    target_workspace = _workspace_manifest(target_spec)
    if adapter is None:
        if adapter_factory is None:
            raise ValueError("adapter or adapter_factory is required")
        adapter = adapter_factory(source_context)

    # OpenCode resolves opaque git-worktree references below its configured
    # root. Creating both identities keeps the fixture valid for process-backed
    # adapters while remaining harmless for in-memory adapters.
    (root / str(source_execution_id)).mkdir(parents=True, exist_ok=True)
    (root / str(target_execution_id)).mkdir(parents=True, exist_ok=True)
    return HandoffAdapterFixture(
        adapter=adapter,
        probe_config=dict(probe_config or {}),
        source_spec=source_spec,
        source_workspace=source_workspace,
        target_spec=target_spec,
        target_workspace=target_workspace,
    )


def run_source_role(factory: HandoffAdapterFactory, root: Path) -> None:
    """Assert checkpoint, quiescence, exact stop and inspection semantics."""

    fixture = factory.create(root)
    adapter = fixture.adapter
    profile = adapter.probe(fixture.probe_config)
    _assert_source_handoff_profile(profile)
    source_handle = None
    stop_operation_id = gateway_operation_id(
        fixture.source_spec.execution_id, GatewayOperationType.STOP_SESSION
    )
    try:
        source_handle = adapter.start_session(
            fixture.source_spec,
            fixture.source_workspace,
            operation_id=uuid4(),
        )
        checkpoint = adapter.capture_handoff_checkpoint(
            source_handle,
            transition_id=uuid4(),
            source_input_cutoff_sequence=0,
            operation_id=uuid4(),
        )
        assert checkpoint.source_session_id == source_handle.execution_session_id
        assert checkpoint.accepted_sequence >= 0
        assert checkpoint.workspace_digest == workspace_manifest_digest(
            fixture.source_workspace
        )
        assert fixture.source_spec.context_package is not None
        assert checkpoint.context_package_id == (
            fixture.source_spec.context_package.metadata.package_id
        )
        assert checkpoint.context_package_hash == (
            fixture.source_spec.context_package.metadata.package_hash
        )
        assert checkpoint.context_version == 1
        assert checkpoint.quiescent is True

        blocked = adapter.send_input(
            source_handle,
            "a post-checkpoint input must be rejected",
            (),
            operation_id=uuid4(),
        )
        assert blocked.outcome is DeliveryOutcome.BLOCKED
        assert blocked.failure_code == "handoff_checkpointed"

        checkpoint_inspection = adapter.inspect_handoff_operation(
            source_handle,
            operation_id=checkpoint.operation_id,
        )
        assert checkpoint_inspection.outcome is HandoffInspectionOutcome.SUCCEEDED
        assert checkpoint_inspection.session_id == source_handle.execution_session_id
        absent = adapter.inspect_handoff_operation(
            source_handle,
            operation_id=uuid4(),
        )
        assert absent.outcome is HandoffInspectionOutcome.ABSENT

        stopped = adapter.stop_session(
            source_handle,
            operation_id=stop_operation_id,
        )
        repeated = adapter.stop_session(
            source_handle,
            operation_id=stop_operation_id,
        )
        assert stopped.outcome is DeliveryOutcome.DELIVERED
        assert stopped.execution_session_id == source_handle.execution_session_id
        assert stopped.accepted_sequence == checkpoint.accepted_sequence
        assert repeated == stopped
        stop_inspection = adapter.inspect_handoff_operation(
            source_handle,
            operation_id=stop_operation_id,
        )
        assert stop_inspection.outcome is HandoffInspectionOutcome.SUCCEEDED
        assert stop_inspection.operation_type is GatewayOperationType.STOP_SESSION
    finally:
        _best_effort_stop(adapter, source_handle, stop_operation_id)


def run_target_role(factory: HandoffAdapterFactory, root: Path) -> None:
    """Assert context binding, read-only hold, fencing and writer inspection."""

    fixture = factory.create(root)
    adapter = fixture.adapter
    profile = adapter.probe(fixture.probe_config)
    _assert_target_handoff_profile(profile)
    target_handle = None
    target_stop_operation_id = uuid4()
    try:
        target_handle = adapter.start_session(
            fixture.target_spec,
            fixture.target_workspace,
            operation_id=uuid4(),
        )
        assert fixture.target_spec.context_package is not None
        binding = target_handle.context_binding
        assert binding is not None
        assert binding.consumer_execution_id == fixture.target_spec.execution_id
        assert binding.package_id == fixture.target_spec.context_package.metadata.package_id
        assert binding.package_hash == (
            fixture.target_spec.context_package.metadata.package_hash
        )
        assert binding.context_version == 1

        before_enable = adapter.send_input(
            target_handle,
            "a write before the writer gate must be rejected",
            (),
            operation_id=uuid4(),
        )
        assert before_enable.outcome is DeliveryOutcome.BLOCKED
        assert before_enable.failure_code == "handoff_writer_disabled"

        stale_fence_rejected = False
        try:
            stale_result = adapter.enable_handoff_writer(
                target_handle,
                fencing_token=fixture.target_spec.fencing_token - 1,
                operation_id=uuid4(),
            )
        except Exception:  # noqa: BLE001 - adapters may reject by exception or result
            stale_fence_rejected = True
        else:
            stale_fence_rejected = stale_result.outcome is not DeliveryOutcome.DELIVERED
        assert stale_fence_rejected is True

        enable_operation_id = uuid4()
        enabled = adapter.enable_handoff_writer(
            target_handle,
            fencing_token=fixture.target_spec.fencing_token,
            operation_id=enable_operation_id,
        )
        repeated = adapter.enable_handoff_writer(
            target_handle,
            fencing_token=fixture.target_spec.fencing_token,
            operation_id=enable_operation_id,
        )
        assert enabled.outcome is DeliveryOutcome.DELIVERED
        assert enabled.execution_session_id == target_handle.execution_session_id
        assert repeated == enabled

        after_enable = adapter.send_input(
            target_handle,
            "continue after the explicit writer gate",
            (),
            operation_id=uuid4(),
        )
        assert after_enable.outcome is DeliveryOutcome.DELIVERED
        assert after_enable.accepted_sequence is not None

        inspection = adapter.inspect_handoff_operation(
            target_handle,
            operation_id=enable_operation_id,
        )
        assert inspection.outcome is HandoffInspectionOutcome.SUCCEEDED
        assert inspection.operation_type is GatewayOperationType.HANDOFF_ENABLE_WRITER
        assert inspection.context_binding == binding
    finally:
        _best_effort_stop(adapter, target_handle, target_stop_operation_id)


def _assert_source_handoff_profile(profile: WorkerAdapterCapabilityProfile) -> None:
    assert profile.handoff_checkpoint_mode in {
        HandoffCheckpointMode.NATIVE,
        HandoffCheckpointMode.CONTROL_EVENT,
    }
    assert 1 in profile.handoff_checkpoint_versions
    assert 1 in profile.handoff_session_context_versions
    assert 1 in profile.handoff_writer_gate_versions
    assert profile.handoff_inspection is True


def _assert_target_handoff_profile(profile: WorkerAdapterCapabilityProfile) -> None:
    """Target eligibility does not require source checkpoint capabilities."""

    assert profile.stop.value == "exact-execution"
    assert 1 in profile.handoff_session_context_versions
    assert 1 in profile.handoff_writer_gate_versions
    assert profile.handoff_inspection is True


def _best_effort_stop(
    adapter: WorkerAdapter,
    handle: object | None,
    operation_id: UUID,
) -> None:
    if handle is None:
        return
    try:
        adapter.stop_session(handle, operation_id=operation_id)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - cleanup must not hide the contract failure
        return


def _context_package(
    execution_id: UUID,
    *,
    handoff_instruction: str | None = None,
) -> ContextMaterializationV1:
    facts: dict[str, object] = {"workspace": "opaque-contract-workspace"}
    if handoff_instruction is not None:
        facts["handoff"] = {"instruction": handoff_instruction}
    return ContextMaterializationV1(
        metadata=ContextMetadata(
            package_id=uuid4(),
            package_hash=HASH,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=execution_id,
            context_version=1,
            expires_at=NOW + timedelta(hours=1),
        ),
        system_constraints=SystemConstraintSection(
            objective="Continue the exact execution context",
            constraints=(),
            acceptance_criteria=(),
        ),
        work_context=WorkContextSection(
            revision_id=uuid4(),
            revision_number=1,
            content_hash=HASH,
            snapshot_schema_version=1,
            requested_outputs=(),
            clarification_decisions=(),
            accepted_decisions=(),
            unresolved_questions=(),
            checkpoint_refs=(),
            missing_fact_codes=(),
        ),
        control_plane_facts=ControlPlaneFactSection(facts=facts),
        project_knowledge=(),
        artifact_and_evidence_refs=(),
    )


def _execution_spec(
    *,
    workspace_id: UUID,
    task_id: UUID,
    execution_id: UUID,
    worker_id: UUID,
    fencing_token: int,
    context_package: ContextMaterializationV1,
) -> ExecutionSpec:
    return ExecutionSpec(
        schema_version=2,
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        worker_id=worker_id,
        lease_id=uuid4(),
        fencing_token=fencing_token,
        goal="Continue the transferred WorkerItem",
        acceptance_criteria=("the Adapter v1 handoff boundary is exact",),
        context_package_ref=context_package_reference(
            context_package.metadata.package_id,
            context_package.metadata.package_hash,
        ),
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="repo://sidera-contract",
                base_revision=BASE_REVISION,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={},
        sandbox_policy={"mode": "filesystem"},
        budget={"max_seconds": 60},
        deadline=None,
        expected_outputs=("diff",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
        context_package=context_package,
    )


def _workspace_manifest(spec: ExecutionSpec) -> ExecutionWorkspaceManifest:
    return ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=spec.workspace_id,
        task_id=spec.task_id,
        execution_id=spec.execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref=spec.repositories[0].repository_ref,
                base_revision=BASE_REVISION,
                branch=f"sidera/contract/{spec.execution_id}",
                current_head=BASE_REVISION,
            ),
        ),
        location_ref=f"git-worktree://{spec.execution_id}",
        retention_seconds=3600,
        fencing_token=spec.fencing_token,
        created_at=NOW,
    )


__all__ = [
    "HandoffAdapterFactory",
    "HandoffAdapterFixture",
    "build_handoff_fixture",
    "run_source_role",
    "run_target_role",
]
