from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.fake_worker import (
    FakeAdapterError,
    FakeWorkerAdapter,
)
from sidera_control_plane.domain.artifacts import (
    ArtifactCandidate,
    ArtifactType,
)
from sidera_control_plane.domain.gateway import DeliveryOutcome
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _spec() -> ExecutionSpec:
    return ExecutionSpec(
        schema_version=1,
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        goal="Implement the adapter contract",
        acceptance_criteria=("the adapter emits standard events",),
        context_package_ref=None,
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="repo://sidera",
                base_revision="a" * 40,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={"shell": "allowed"},
        sandbox_policy={"mode": "filesystem"},
        budget={"max_seconds": 60},
        deadline=None,
        expected_outputs=("diff",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
    )


def _workspace(spec: ExecutionSpec) -> ExecutionWorkspaceManifest:
    return ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=spec.workspace_id,
        task_id=spec.task_id,
        execution_id=spec.execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision="a" * 40,
                branch=f"sidera/execution/{spec.execution_id}",
                current_head="a" * 40,
            ),
        ),
        location_ref=f"git-worktree://{spec.execution_id}",
        retention_seconds=3600,
        fencing_token=spec.fencing_token,
        created_at=NOW,
    )


def test_fake_adapter_respects_prompt_boundary_and_emits_events() -> None:
    spec = _spec()
    adapter = FakeWorkerAdapter(
        artifacts=(
            ArtifactCandidate(
                artifact_type=ArtifactType.DIFF,
                storage_ref="artifact://fake/diff",
                content_hash="a" * 64,
                size_bytes=4,
                media_type="text/plain",
                provenance={"producer": "fake"},
            ),
        )
    )

    profile = adapter.probe({})
    assert profile.input is CapabilityInput.QUEUED
    assert profile.idempotency is IdempotencyCapability.NONE
    assert profile.resume is ResumeCapability.SAME_PROCESS
    assert profile.stop is StopCapability.EXACT_EXECUTION
    assert profile.sandbox is SandboxCapability.FILESYSTEM
    assert profile.stability is StabilityLevel.STABLE

    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())
    started = adapter.read_events(handle)
    assert [event.event_type for event in started] == ["session.started"]

    first = adapter.send_input(
        handle,
        "first prompt",
        (),
        operation_id=uuid4(),
    )
    assert first.outcome is DeliveryOutcome.DELIVERED
    assert first.accepted_sequence is not None

    busy = adapter.send_input(
        handle,
        "queued prompt",
        (),
        operation_id=uuid4(),
    )
    assert busy.outcome is DeliveryOutcome.BLOCKED
    assert busy.failure_code == "prompt_busy"

    adapter.release_boundary(handle)
    second = adapter.send_input(
        handle,
        "queued prompt",
        (),
        operation_id=uuid4(),
    )
    assert second.outcome is DeliveryOutcome.DELIVERED

    candidates = adapter.collect_artifacts(handle, operation_id=uuid4())
    assert candidates[0].storage_ref == "artifact://fake/diff"
    stopped = adapter.stop_session(handle, operation_id=uuid4())
    assert stopped.outcome is DeliveryOutcome.DELIVERED
    assert any(event.event_type == "session.canceled" for event in adapter.read_events(handle))


def test_fake_adapter_failure_injection_is_explicit() -> None:
    spec = _spec()
    adapter = FakeWorkerAdapter(failures={"start_session": "process_start_failed"})

    with pytest.raises(FakeAdapterError, match="process_start_failed"):
        adapter.start_session(spec, _workspace(spec), operation_id=uuid4())
