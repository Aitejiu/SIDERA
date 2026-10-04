from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_control_plane import gateway_worker
from sidera_control_plane.adapters.local_verification import LocalVerificationAdapter
from sidera_control_plane.domain.modules import (
    LOCAL_VERIFICATION_PROVIDER,
    ModuleInstance,
    ModuleLifecycle,
)
from sidera_control_plane.domain.result_integration import CheckSpecV1


def _instance(*, check_specs: object = None) -> ModuleInstance:
    return ModuleInstance(
        id=uuid4(),
        workspace_id=uuid4(),
        definition_id=uuid4(),
        instance_name="verification",
        config_version=1,
        config_namespace="verification",
        config={
            "verification.check_specs": [] if check_specs is None else check_specs
        },
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )


def test_gateway_factory_routes_local_verification_without_constructing_opencode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters import opencode_acp

    def fail_if_constructed(*args: object, **kwargs: object) -> None:
        raise AssertionError("OpenCode must not be constructed for verification")

    monkeypatch.setattr(
        opencode_acp, "OpenCodeACPAdapter", fail_if_constructed
    )
    settings = SimpleNamespace(
        workspace_root=tmp_path,
        git_executable="git",
        opencode_executable="opencode",
        opencode_request_timeout_seconds=30.0,
        opencode_handoff_request_timeout_seconds=30.0,
        opencode_stop_grace_seconds=1.0,
    )

    factories = gateway_worker.build_worker_adapter_factories(
        settings,
        workspace_manager=SimpleNamespace(
            physical_location=lambda _manifest: tmp_path,
        ),
    )
    adapter = factories[LOCAL_VERIFICATION_PROVIDER](_instance())

    assert isinstance(adapter, LocalVerificationAdapter)


def test_gateway_factory_binds_configured_verification_checks(
    tmp_path: Path,
) -> None:
    check = CheckSpecV1(
        key="unit",
        version=1,
        argv=("python3", "-m", "unittest"),
        cwd=".",
        environment_ref="env://python-test",
        prepare_argvs=(),
        timeout_seconds=60,
        success_exit_codes=(0,),
        report_paths=(),
        required=True,
    )
    settings = SimpleNamespace(
        workspace_root=tmp_path,
        git_executable="git",
        opencode_executable="opencode",
        opencode_request_timeout_seconds=30.0,
        opencode_handoff_request_timeout_seconds=30.0,
        opencode_stop_grace_seconds=1.0,
    )

    factories = gateway_worker.build_worker_adapter_factories(
        settings,
        workspace_manager=SimpleNamespace(),
    )
    adapter = factories[LOCAL_VERIFICATION_PROVIDER](
        _instance(check_specs=[check.to_mapping()])
    )

    assert adapter._check_specs["unit"] == check


def test_fallback_workspace_locator_rejects_symlink(
    tmp_path: Path,
) -> None:
    execution_id = uuid4()
    target = tmp_path / "real-worktree"
    target.mkdir()
    (tmp_path / str(execution_id)).symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="verification workspace is unavailable"):
        gateway_worker._workspace_location(
            tmp_path,
            SimpleNamespace(execution_id=execution_id),
        )


def test_runtime_binding_rejects_verification_on_normal_provider() -> None:
    from sidera_control_plane.application.worker_runtime import (
        RuntimeBindingError,
        require_verification_runtime,
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

    runtime = SimpleNamespace(
        adapter_type="opencode",
        capability_snapshot=WorkerAdapterCapabilityProfile(
            input=CapabilityInput.QUEUED,
            idempotency=IdempotencyCapability.NONE,
            resume=ResumeCapability.SAME_PROCESS,
            stop=StopCapability.EXACT_EXECUTION,
            sandbox=SandboxCapability.FILESYSTEM,
            stability=StabilityLevel.EXPERIMENTAL,
        ),
    )

    with pytest.raises(RuntimeBindingError, match="verification_provider_required"):
        require_verification_runtime(runtime)


def test_runtime_binding_rejects_verification_without_git_worktree() -> None:
    from sidera_control_plane.application.worker_runtime import (
        ResolvedWorkerRuntime,
        RuntimeBindingError,
        require_verification_runtime,
    )
    from sidera_control_plane.domain.modules import (
        CapabilityInput,
        IdempotencyCapability,
        ResumeCapability,
        SandboxCapability,
        StabilityLevel,
        StopCapability,
        WorkerAdapterCapabilityProfile,
        WorkspaceProviderCapabilityProfile,
    )

    runtime = ResolvedWorkerRuntime(
        worker_id=uuid4(),
        adapter_type=LOCAL_VERIFICATION_PROVIDER,
        adapter_module_instance_id=uuid4(),
        adapter_module_config_version=1,
        workspace_module_instance_id=uuid4(),
        workspace_module_config_version=1,
        adapter_definition_id=uuid4(),
        workspace_definition_id=uuid4(),
        capability_snapshot=WorkerAdapterCapabilityProfile(
            input=CapabilityInput.QUEUED,
            idempotency=IdempotencyCapability.NONE,
            resume=ResumeCapability.SAME_PROCESS,
            stop=StopCapability.EXACT_EXECUTION,
            sandbox=SandboxCapability.FILESYSTEM,
            stability=StabilityLevel.STABLE,
            verification_versions=(1,),
        ),
        resolution_facts={},
        workspace_capability_snapshot=WorkspaceProviderCapabilityProfile(
            isolation=SandboxCapability.ISOLATED_RUNTIME,
            writable=True,
            git_worktree=False,
            stability=StabilityLevel.STABLE,
        ),
    )

    with pytest.raises(RuntimeBindingError, match="verification_workspace_unsupported"):
        require_verification_runtime(runtime)
