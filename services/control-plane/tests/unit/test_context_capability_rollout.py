from __future__ import annotations

from dataclasses import replace
from typing import Literal
from uuid import uuid4

import pytest
from test_context_materialization import (
    FakeCatalog,
    _manifest,
    _revision,
    _run,
)

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.worker_gateway import (
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
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    RepositoryCheckoutSpec,
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
