from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.gateway import AdapterSessionHandle, DeliveryResult
from sidera_control_plane.domain.handoff_evidence import SessionContextBinding
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

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
HASH = "a" * 64


def worker_profile(**overrides: object) -> WorkerAdapterCapabilityProfile:
    values: dict[str, object] = {
        "input": CapabilityInput.QUEUED,
        "idempotency": IdempotencyCapability.OPERATION_ID,
        "resume": ResumeCapability.SAME_PROCESS,
        "stop": StopCapability.EXACT_EXECUTION,
        "sandbox": SandboxCapability.FILESYSTEM,
        "stability": StabilityLevel.STABLE,
        "context_package_versions": (1,),
        "context_patch_versions": (),
    }
    values.update(overrides)
    return WorkerAdapterCapabilityProfile(**values)  # type: ignore[arg-type]


def test_handoff_capability_profile_defaults_are_unsupported() -> None:
    profile = worker_profile()

    assert profile.handoff_checkpoint_mode is HandoffCheckpointMode.UNSUPPORTED
    assert profile.handoff_checkpoint_versions == ()
    assert profile.handoff_session_context_versions == ()
    assert profile.handoff_writer_gate_versions == ()
    assert profile.handoff_inspection is False


def test_handoff_capability_profile_accepts_explicit_supported_contract() -> None:
    profile = worker_profile(
        handoff_checkpoint_mode=HandoffCheckpointMode.NATIVE,
        handoff_checkpoint_versions=(1,),
        handoff_session_context_versions=(1,),
        handoff_writer_gate_versions=(1,),
        handoff_inspection=True,
    )

    assert profile.handoff_checkpoint_mode is HandoffCheckpointMode.NATIVE
    assert profile.handoff_checkpoint_versions == (1,)
    assert profile.handoff_inspection is True


def test_handoff_capability_versions_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        worker_profile(handoff_checkpoint_versions=(0,))
    with pytest.raises(ValidationError):
        worker_profile(handoff_session_context_versions=(-1,))
    with pytest.raises(ValidationError):
        worker_profile(handoff_writer_gate_versions=(True,))


def test_workspace_handoff_capability_defaults_and_modes() -> None:
    profile = WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.FILESYSTEM,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
    )

    assert profile.workspace_handoff_versions == ()
    assert profile.workspace_transfer_modes == ()
    assert profile.handoff_inspection is False


def test_session_handle_and_delivery_result_carry_handoff_identity() -> None:
    binding = SessionContextBinding(
        operation_id=uuid4(),
        execution_session_id=UUID("20000000-0000-0000-0000-000000000002"),
        consumer_execution_id=UUID("30000000-0000-0000-0000-000000000002"),
        package_id=UUID("40000000-0000-0000-0000-000000000002"),
        package_hash=HASH,
        context_version=1,
        acknowledged_at=NOW,
    )
    assert binding.operation_id
    assert AdapterSessionHandle.__dataclass_fields__["context_binding"]
    assert DeliveryResult.__dataclass_fields__["execution_session_id"]
