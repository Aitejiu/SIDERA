from dataclasses import replace
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.handoffs import HandoffTransferMode
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    CapabilityInput,
    HandoffCheckpointMode,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    StructuredOutputCapability,
    UsageReportingCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_from_payload,
    capability_profile_to_payload,
)


def worker_capabilities() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
    )


def planner_capabilities() -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.EXPERIMENTAL,
        interface_version="sidera.planner-adapter.v1",
        proposal_schema_versions=(2, 1, 2),
    )


def workspace_capabilities() -> WorkspaceProviderCapabilityProfile:
    return WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
    )


def definition(
    *, module_type: str, capabilities: object
) -> ModuleDefinition:
    return ModuleDefinition(
        id=uuid4(),
        workspace_id=uuid4(),
        module_type=module_type,
        provider="fake",
        interface_version="sidera.module.v1",
        config_schema_version=1,
        capabilities=capabilities,  # type: ignore[arg-type]
        lifecycle=ModuleLifecycle.ACTIVE,
    )


def test_planner_definition_requires_planner_capabilities() -> None:
    item = definition(
        module_type="planner_adapter",
        capabilities=planner_capabilities(),
    )

    assert isinstance(item.capabilities, PlannerAdapterCapabilityProfile)
    assert item.capabilities.structured_output is StructuredOutputCapability.JSON_SCHEMA
    assert item.capabilities.proposal_schema_versions == (1, 2)


def test_planner_definition_rejects_worker_capabilities() -> None:
    with pytest.raises(ValidationError, match="planner_adapter"):
        definition(module_type="planner_adapter", capabilities=worker_capabilities())


def test_worker_definition_rejects_planner_capabilities() -> None:
    with pytest.raises(ValidationError, match="worker_adapter"):
        definition(module_type="worker_adapter", capabilities=planner_capabilities())


def test_unknown_module_type_is_rejected() -> None:
    with pytest.raises(ValidationError, match="module_type"):
        definition(module_type="unknown_module", capabilities=worker_capabilities())


def test_legacy_worker_capability_payload_round_trips() -> None:
    profile = worker_capabilities()

    payload = capability_profile_to_payload(profile)
    restored = capability_profile_from_payload("worker_adapter", payload)

    assert restored == profile


def test_workspace_provider_profile_round_trips() -> None:
    profile = workspace_capabilities()

    payload = capability_profile_to_payload(profile)
    restored = capability_profile_from_payload("workspace_provider", payload)

    assert restored == profile
    assert payload == {
        "isolation": "isolated-runtime",
        "writable": True,
        "git_worktree": True,
        "stability": "stable",
    }


def test_unknown_capability_fields_are_rejected() -> None:
    payload = {
        **capability_profile_to_payload(worker_capabilities()),
        "future_flag": True,
    }

    with pytest.raises(ValidationError, match="future_flag"):
        capability_profile_from_payload("worker_adapter", payload)


def test_planner_profile_normalizes_schema_versions_and_requires_context() -> None:
    profile = planner_capabilities()

    assert profile.proposal_schema_versions == (1, 2)
    with pytest.raises(ValidationError, match="max_context_bytes"):
        PlannerAdapterCapabilityProfile(
            structured_output=profile.structured_output,
            clarification=profile.clarification,
            idempotency=profile.idempotency,
            cancellation=profile.cancellation,
            usage_reporting=profile.usage_reporting,
            max_context_bytes=0,
            stability=profile.stability,
            interface_version=profile.interface_version,
            proposal_schema_versions=profile.proposal_schema_versions,
        )


def test_context_package_versions_are_optional_and_normalized() -> None:
    worker = replace(worker_capabilities(), context_package_versions=(2, 1, 2))
    patch_capable_worker = replace(
        worker_capabilities(), context_patch_versions=(2, 1, 2)
    )
    planner = replace(planner_capabilities(), context_package_versions=(2, 1, 2))

    assert worker.context_package_versions == (1, 2)
    assert patch_capable_worker.context_patch_versions == (1, 2)
    assert planner.context_package_versions == (1, 2)
    assert worker_capabilities().context_package_versions == ()
    assert worker_capabilities().context_patch_versions == ()
    assert planner_capabilities().context_package_versions == ()

    with pytest.raises(ValidationError, match="context_package_versions"):
        replace(worker_capabilities(), context_package_versions=(0,))
    with pytest.raises(ValidationError, match="context_patch_versions"):
        replace(worker_capabilities(), context_patch_versions=(0,))


def test_context_patch_capability_round_trips_through_module_payload() -> None:
    profile = replace(worker_capabilities(), context_patch_versions=(1,))

    payload = capability_profile_to_payload(profile)
    restored = capability_profile_from_payload("worker_adapter", payload)

    assert payload["context_patch_versions"] == [1]
    assert restored == profile


def test_handoff_capability_profiles_round_trip_through_module_payload() -> None:
    worker = replace(
        worker_capabilities(),
        handoff_checkpoint_mode=HandoffCheckpointMode.CONTROL_EVENT,
        handoff_checkpoint_versions=(1,),
        handoff_session_context_versions=(1,),
        handoff_writer_gate_versions=(1,),
        handoff_inspection=True,
    )
    workspace = replace(
        workspace_capabilities(),
        workspace_handoff_versions=(1,),
        workspace_transfer_modes=(HandoffTransferMode.REUSE_EXACT,),
        handoff_inspection=True,
    )

    assert capability_profile_from_payload(
        "worker_adapter", capability_profile_to_payload(worker)
    ) == worker
    assert capability_profile_from_payload(
        "workspace_provider", capability_profile_to_payload(workspace)
    ) == workspace


def test_module_type_alias_is_stored_as_canonical_value() -> None:
    item = definition(
        module_type="worker-adapter",
        capabilities=worker_capabilities(),
    )

    assert item.module_type == "worker_adapter"


def test_module_lifecycle_uses_an_independent_resource_version() -> None:
    item = definition(
        module_type="worker_adapter",
        capabilities=worker_capabilities(),
    )

    draining = item.with_lifecycle(ModuleLifecycle.DRAINING)
    active_again = draining.with_lifecycle(ModuleLifecycle.ACTIVE)
    disabled = active_again.with_lifecycle(ModuleLifecycle.DISABLED)

    assert item.lifecycle is ModuleLifecycle.ACTIVE
    assert item.resource_version == 1
    assert draining.lifecycle is ModuleLifecycle.DRAINING
    assert draining.resource_version == 2
    assert active_again.lifecycle is ModuleLifecycle.ACTIVE
    assert active_again.resource_version == 3
    assert disabled.lifecycle is ModuleLifecycle.DISABLED
    assert disabled.resource_version == 4
    assert disabled.config_schema_version == item.config_schema_version

    with pytest.raises(InvalidTransition, match="disabled"):
        disabled.with_lifecycle(ModuleLifecycle.ACTIVE)


def test_module_instance_lifecycle_also_has_a_resource_version() -> None:
    definition_id = uuid4()
    item = ModuleInstance(
        id=uuid4(),
        workspace_id=uuid4(),
        definition_id=definition_id,
        instance_name="fake-worker",
        config_version=3,
        config_namespace="worker",
        config={"worker.command": "fake"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )

    draining = item.with_lifecycle(ModuleLifecycle.DRAINING)
    disabled = draining.with_lifecycle(ModuleLifecycle.DISABLED)

    assert item.resource_version == 1
    assert draining.resource_version == 2
    assert disabled.resource_version == 3
    assert disabled.config_version == 3
