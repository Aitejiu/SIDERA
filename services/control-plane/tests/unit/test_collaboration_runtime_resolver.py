from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    CollaborationRuntimeResolver,
    RegistryCollaborationRuntimeFactory,
    RuntimeResolutionError,
)
from sidera_control_plane.domain.collaboration import CollaborationRun
from sidera_control_plane.domain.modules import (
    COLLABORATION_RUNTIME_MODULE_TYPE,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    StabilityLevel,
)

WORKSPACE_ID = UUID("21000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
PLAN_SNAPSHOT_ID = UUID("32000000-0000-0000-0000-000000000001")
RUN_ID = UUID("33000000-0000-0000-0000-000000000001")
DEFINITION_ID = UUID("34000000-0000-0000-0000-000000000001")
INSTANCE_ID = UUID("35000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _capability() -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime.v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.STABLE,
    )


def _definition(
    *,
    capability: CollaborationRuntimeCapabilityProfile | None = None,
    lifecycle: ModuleLifecycle = ModuleLifecycle.ACTIVE,
    provider: str = "reference",
) -> ModuleDefinition:
    return ModuleDefinition(
        id=DEFINITION_ID,
        workspace_id=WORKSPACE_ID,
        module_type=COLLABORATION_RUNTIME_MODULE_TYPE,
        provider=provider,
        interface_version="collaboration-runtime.v1",
        config_schema_version=1,
        capabilities=capability or _capability(),
        lifecycle=lifecycle,
    )


def _instance(
    *, config_version: int = 3, lifecycle: ModuleLifecycle = ModuleLifecycle.ACTIVE
) -> ModuleInstance:
    return ModuleInstance(
        id=INSTANCE_ID,
        workspace_id=WORKSPACE_ID,
        definition_id=DEFINITION_ID,
        instance_name="reference-runtime",
        config_version=config_version,
        config_namespace="runtime",
        config={"runtime.strategy": "all_required"},
        secret_refs=(),
        lifecycle=lifecycle,
    )


def _run(
    *,
    runtime_config_version: int = 3,
    capability: CollaborationRuntimeCapabilityProfile | None = None,
) -> CollaborationRun:
    return CollaborationRun.create(
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=7,
        plan_content_hash="a" * 64,
        runtime_definition_id=DEFINITION_ID,
        runtime_instance_id=INSTANCE_ID,
        runtime_interface_version="collaboration-runtime.v1",
        runtime_config_version=runtime_config_version,
        runtime_capability_snapshot=capability or _capability(),
        policy_snapshot={
            "schema_version": 1,
            "default_policy": {"enabled": True, "strategy_id": "all_required"},
            "subtask_overrides": [],
        },
        now=NOW,
    )


class InMemoryModuleRepository:
    def __init__(
        self,
        definition: ModuleDefinition | None,
        instance: ModuleInstance | None,
    ) -> None:
        self.definition = definition
        self.instance = instance
        self.definition_requests: list[tuple[UUID, UUID]] = []
        self.instance_requests: list[tuple[UUID, UUID]] = []

    def get_definition(
        self, module_id: UUID, workspace_id: UUID
    ) -> ModuleDefinition | None:
        self.definition_requests.append((module_id, workspace_id))
        if self.definition is not None and self.definition.id == module_id:
            return self.definition
        return None

    def get_instance(
        self, module_id: UUID, workspace_id: UUID
    ) -> ModuleInstance | None:
        self.instance_requests.append((module_id, workspace_id))
        if self.instance is not None and self.instance.id == module_id:
            return self.instance
        return None


def _resolver(
    *,
    definition: ModuleDefinition | None = None,
    instance: ModuleInstance | None = None,
    builders: dict[str, Any] | None = None,
) -> CollaborationRuntimeResolver:
    repository = InMemoryModuleRepository(
        definition or _definition(), instance or _instance()
    )
    factory = RegistryCollaborationRuntimeFactory(
        builders
        or {"reference": lambda _: ReferenceCollaborationRuntime()}
    )
    return CollaborationRuntimeResolver(repository, factory)


def test_resolver_returns_the_exact_pinned_runtime() -> None:
    resolved = _resolver().resolve(_run())

    assert isinstance(resolved.runtime, ReferenceCollaborationRuntime)
    assert resolved.provider == "reference"
    assert resolved.interface_version == "collaboration-runtime.v1"
    assert resolved.config_version == 3


def test_resolver_fails_closed_when_pinned_config_changed() -> None:
    with pytest.raises(RuntimeResolutionError) as error:
        _resolver(instance=_instance(config_version=4)).resolve(
            _run(runtime_config_version=3)
        )

    assert error.value.code == "runtime_config_mismatch"


def test_resolver_rejects_disabled_definition_and_instance() -> None:
    with pytest.raises(RuntimeResolutionError) as definition_error:
        _resolver(definition=_definition(lifecycle=ModuleLifecycle.DISABLED)).resolve(
            _run()
        )
    assert definition_error.value.code == "runtime_module_disabled"

    with pytest.raises(RuntimeResolutionError) as instance_error:
        _resolver(instance=_instance(lifecycle=ModuleLifecycle.DISABLED)).resolve(
            _run()
        )
    assert instance_error.value.code == "runtime_module_disabled"


def test_resolver_rejects_a_changed_capability_snapshot() -> None:
    current = replace(_capability(), max_intents_per_decision=64)

    with pytest.raises(RuntimeResolutionError) as error:
        _resolver(definition=_definition(capability=current)).resolve(_run())

    assert error.value.code == "runtime_capability_mismatch"


def test_resolver_rejects_capabilities_outside_service_limits() -> None:
    oversized = replace(
        _capability(),
        max_projection_bytes=1_048_577,
    )

    with pytest.raises(RuntimeResolutionError) as error:
        _resolver(definition=_definition(capability=oversized)).resolve(
            _run(capability=oversized)
        )

    assert error.value.code == "runtime_capability_limit_exceeded"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("projection_schema_versions", (2,)),
        ("decision_schema_versions", (2,)),
        ("strategy_ids", ("other",)),
        ("idempotency", IdempotencyCapability.NONE),
    ),
)
def test_resolver_rejects_unsupported_pinned_capability(field: str, value: Any) -> None:
    capability = replace(_capability(), **{field: value})

    with pytest.raises(RuntimeResolutionError) as error:
        _resolver(definition=_definition(capability=capability)).resolve(
            _run(capability=capability)
        )

    assert error.value.code == "runtime_capability_unsupported"


def test_resolver_does_not_fallback_to_another_provider() -> None:
    alternative_called = False

    def alternative(_: ModuleInstance) -> ReferenceCollaborationRuntime:
        nonlocal alternative_called
        alternative_called = True
        return ReferenceCollaborationRuntime()

    with pytest.raises(RuntimeResolutionError) as error:
        _resolver(builders={"alternative": alternative}).resolve(_run())

    assert error.value.code == "runtime_provider_unsupported"
    assert alternative_called is False
