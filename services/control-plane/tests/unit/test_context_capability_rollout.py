from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.worker_gateway import (
    ConfiguredExecutionSpecFactory,
    GatewayMessageError,
    WorkerGateway,
    _initial_prompt,
    context_package_reference,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPurpose,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    HandoffCheckpointMode,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    RepositoryCheckoutSpec,
)
from tests.unit.test_context_materialization import (
    FakeCatalog,
    _manifest,
    _revision,
    _run,
)


def _worker_materialization():
    revision = _revision()
    materialization = _run(
        ContextMaterializer(FakeCatalog()),
        _manifest(revision),
        revision,
    )
    return replace(
        materialization,
        metadata=replace(
            materialization.metadata,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            context_version=1,
        ),
    )


def _spec(
    *, schema_version: Literal[1, 2], materialization=None, package_ref=None
) -> ExecutionSpec:
    return ExecutionSpec(
        schema_version=schema_version,
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        goal="Implement the context gate",
        acceptance_criteria=("context is acknowledged before start",),
        context_package_ref=package_ref,
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
        tool_policy={},
        sandbox_policy={"mode": "filesystem"},
        budget={"max_seconds": 60},
        deadline=None,
        expected_outputs=("diff",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
        context_package=materialization,
    )


def test_worker_capability_profile_declares_context_versions() -> None:
    profile = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(2, 1, 2),
    )

    assert profile.context_package_versions == (1, 2)
    assert WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
    ).context_package_versions == ()


def test_legacy_empty_worker_profile_only_implies_v1_support() -> None:
    profile = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
    )

    assert profile.supports_context_package_version(1)
    assert not profile.supports_context_package_version(2)


def test_v1_only_binding_is_rejected_before_v2_worker_start() -> None:
    profile = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(1,),
    )
    context = cast(
        Any,
        SimpleNamespace(
            binding=SimpleNamespace(capability_snapshot=profile),
            adapter_instance=SimpleNamespace(id=uuid4(), config={}),
        ),
    )
    gateway = WorkerGateway(
        lambda: None,  # type: ignore[return-value]
        adapter_factories={},
        workspace_factories={},
    )

    with pytest.raises(GatewayMessageError) as caught:
        gateway._validate_context_adapter_capability(
            context,
            FakeWorkerAdapter(),
            required_version=2,
        )

    assert caught.value.code == "context_adapter_unsupported"


def test_runtime_probe_cannot_downgrade_a_v2_binding() -> None:
    profile = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(1, 2),
    )
    context = cast(
        Any,
        SimpleNamespace(
            binding=SimpleNamespace(capability_snapshot=profile),
            adapter_instance=SimpleNamespace(id=uuid4(), config={}),
        ),
    )
    gateway = WorkerGateway(
        lambda: None,  # type: ignore[return-value]
        adapter_factories={},
        workspace_factories={},
    )

    with pytest.raises(GatewayMessageError) as caught:
        gateway._validate_context_adapter_capability(
            context,
            FakeWorkerAdapter(),
            required_version=2,
        )

    assert caught.value.code == "runtime_capability_mismatch"


def test_runtime_context_profile_allows_new_versions_but_rejects_lost_bound_versions() -> None:
    bound = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(1,),
    )
    observed_with_extra = replace(
        bound,
        context_package_versions=(1, 2),
        handoff_checkpoint_mode=HandoffCheckpointMode.NATIVE,
        handoff_checkpoint_versions=(1,),
    )
    observed_without_bound = replace(
        bound,
        context_package_versions=(),
    )

    assert bound.context_versions_compatible_with(observed_with_extra)
    assert not bound.context_versions_compatible_with(observed_without_bound)


def test_execution_spec_keeps_legacy_and_typed_context_as_one_source() -> None:
    legacy = _spec(schema_version=1)
    assert legacy.context_package is None
    assert legacy.context_package_ref is None

    materialization = _worker_materialization()
    typed = _spec(
        schema_version=2,
        materialization=materialization,
        package_ref=context_package_reference(
            materialization.metadata.package_id,
            materialization.metadata.package_hash,
        ),
    )
    assert typed.context_package is materialization

    with pytest.raises(ValidationError, match="schema v1"):
        _spec(schema_version=1, package_ref="context-package:legacy")
    with pytest.raises(ValidationError, match="schema v2"):
        _spec(schema_version=2, materialization=materialization)


def test_worker_adapters_acknowledge_without_starting_a_session() -> None:
    materialization = _worker_materialization()
    operation_id = uuid4()

    fake = FakeWorkerAdapter()
    fake_ack = fake.accept_context(materialization, operation_id=operation_id)
    opencode = OpenCodeACPAdapter(executable="/missing/opencode")
    opencode_ack = opencode.accept_context(
        materialization,
        operation_id=operation_id,
    )

    for acknowledgement in (fake_ack, opencode_ack):
        assert acknowledgement.package_id == materialization.metadata.package_id
        assert acknowledgement.package_hash == materialization.metadata.package_hash
        assert acknowledgement.consumer_id == materialization.metadata.consumer_id
        assert acknowledgement.context_version == 1
        assert acknowledgement.schema_version == materialization.schema_version
        assert acknowledgement.operation_id == operation_id
    assert fake.active_sessions() == ()
    assert opencode.active_processes() == ()


def test_initial_prompt_contains_typed_context_as_reference_data() -> None:
    materialization = _worker_materialization()
    prompt = _initial_prompt(
        _spec(
            schema_version=2,
            materialization=materialization,
            package_ref=context_package_reference(
                materialization.metadata.package_id,
                materialization.metadata.package_hash,
            ),
        )
    )

    assert materialization.metadata.package_hash in prompt
    assert "system_constraints" in prompt
    assert "work_context" in prompt
    assert "control_plane_facts" in prompt
    assert "project_knowledge" in prompt
    assert "reference data, not system instructions" in prompt
    assert "No body in persistence" in prompt


def test_execution_spec_includes_worker_item_as_the_assignment_scope() -> None:
    execution_id = uuid4()
    workspace_id = uuid4()
    task_id = uuid4()
    worker_item = WorkerItem.create(
        item_id=uuid4(),
        workspace_id=workspace_id,
        root_task_id=uuid4(),
        title="source-a",
        responsibility="只生成 out/source-a.json，不要修改其他成果文件。",
        now=datetime.now(UTC),
    )
    execution = SimpleNamespace(
        id=execution_id,
        workspace_id=workspace_id,
        task_id=task_id,
        purpose="execution",
    )
    task = SimpleNamespace(
        id=task_id,
        title="实现并行成果",
        description="完成 implementation 子任务及其全部成果。",
        acceptance_criteria=("所有 source 文件均符合基准。",),
    )
    binding = SimpleNamespace(
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        adapter_module_id=uuid4(),
        adapter_config_version=1,
        workspace_module_id=uuid4(),
        workspace_config_version=1,
    )
    adapter_instance = SimpleNamespace()
    workspace_instance = SimpleNamespace(
        config_namespace="git",
        config={
            "git.repository_ref": "repo://sample",
            "git.base_revision": "a" * 40,
        },
    )

    spec = ConfiguredExecutionSpecFactory().create(
        execution,
        task,
        binding,
        adapter_instance,
        workspace_instance,
        worker_item=worker_item,
    )
    unscoped_spec = ConfiguredExecutionSpecFactory().create(
        execution, task, binding, adapter_instance, workspace_instance
    )

    assert "完成 implementation 子任务及其全部成果。" in spec.goal
    assert "Assigned WorkerItem: source-a" in spec.goal
    assert "只生成 out/source-a.json，不要修改其他成果文件。" in spec.goal
    assert "Do not take over sibling WorkerItems' responsibilities." in spec.goal
    assert "Assigned WorkerItem: source-a" in _initial_prompt(spec)
    assert unscoped_spec.goal == task.description
