from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from sidera_control_plane.application.ports import ModuleRepositoryPort
from sidera_control_plane.domain.collaboration import (
    CollaborationRun,
    CollaborationStrategy,
)
from sidera_control_plane.domain.collaboration_runtime import CollaborationRuntime
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.modules import (
    COLLABORATION_RUNTIME_MODULE_TYPE,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
)
from sidera_control_plane.domain.planning_profiles import (
    COLLABORATION_MAX_DECISION_BYTES,
    COLLABORATION_MAX_INTENTS_PER_DECISION,
    COLLABORATION_MAX_PROJECTION_BYTES,
    COLLABORATION_MAX_STRATEGY_STATE_BYTES,
)


class RuntimeResolutionError(DomainError):
    """A pinned collaboration runtime cannot be used for this Run."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class CollaborationRuntimeFactory(Protocol):
    def create(
        self,
        definition: ModuleDefinition,
        instance: ModuleInstance,
    ) -> CollaborationRuntime:
        raise NotImplementedError


CollaborationRuntimeBuilder = Callable[[ModuleInstance], CollaborationRuntime]


class RegistryCollaborationRuntimeFactory:
    """Resolve providers by explicit name without candidate fallback."""

    def __init__(self, builders: Mapping[str, CollaborationRuntimeBuilder]) -> None:
        self._builders = dict(builders)

    def create(
        self,
        definition: ModuleDefinition,
        instance: ModuleInstance,
    ) -> CollaborationRuntime:
        builder = self._builders.get(definition.provider)
        if builder is None:
            raise RuntimeResolutionError(
                "runtime_provider_unsupported",
                "the pinned collaboration runtime provider is unsupported",
            )
        try:
            return builder(instance)
        except RuntimeResolutionError:
            raise
        except Exception as exc:
            raise RuntimeResolutionError(
                "runtime_provider_unavailable",
                "the pinned collaboration runtime provider is unavailable",
            ) from exc


@dataclass(frozen=True, slots=True)
class ResolvedCollaborationRuntime:
    runtime: CollaborationRuntime
    provider: str
    interface_version: str
    config_version: int


class CollaborationRuntimeResolver:
    """Resolve exactly the runtime binding captured on a CollaborationRun."""

    def __init__(
        self,
        module_repository: ModuleRepositoryPort,
        factories: CollaborationRuntimeFactory,
    ) -> None:
        self._modules = module_repository
        self._factories = factories

    def resolve(self, run: CollaborationRun) -> ResolvedCollaborationRuntime:
        definition, instance = self._load_exact_binding(run)
        self._verify_pinned_contract(run, definition, instance)
        try:
            runtime = self._factories.create(definition, instance)
        except RuntimeResolutionError:
            raise
        except Exception as exc:
            raise RuntimeResolutionError(
                "runtime_provider_unavailable",
                "the pinned collaboration runtime provider is unavailable",
            ) from exc
        return ResolvedCollaborationRuntime(
            runtime=runtime,
            provider=definition.provider,
            interface_version=run.runtime_interface_version,
            config_version=run.runtime_config_version,
        )

    def _load_exact_binding(
        self, run: CollaborationRun
    ) -> tuple[ModuleDefinition, ModuleInstance]:
        definition = self._modules.get_definition(
            run.runtime_definition_id, run.workspace_id
        )
        instance = self._modules.get_instance(run.runtime_instance_id, run.workspace_id)
        if definition is None or instance is None:
            raise RuntimeResolutionError(
                "runtime_binding_missing",
                "the pinned collaboration runtime binding does not exist",
            )
        return definition, instance

    @staticmethod
    def _verify_pinned_contract(
        run: CollaborationRun,
        definition: ModuleDefinition,
        instance: ModuleInstance,
    ) -> None:
        if (
            definition.workspace_id != run.workspace_id
            or instance.workspace_id != run.workspace_id
        ):
            raise RuntimeResolutionError(
                "runtime_binding_scope_mismatch",
                "the pinned collaboration runtime belongs to another workspace",
            )
        if definition.id != run.runtime_definition_id:
            raise RuntimeResolutionError(
                "runtime_definition_mismatch",
                "the resolved definition is not the definition pinned by the Run",
            )
        if instance.id != run.runtime_instance_id:
            raise RuntimeResolutionError(
                "runtime_instance_mismatch",
                "the resolved instance is not the instance pinned by the Run",
            )
        if instance.definition_id != definition.id:
            raise RuntimeResolutionError(
                "runtime_binding_mismatch",
                "the runtime instance does not belong to the pinned definition",
            )
        if definition.module_type != COLLABORATION_RUNTIME_MODULE_TYPE:
            raise RuntimeResolutionError(
                "runtime_module_type_mismatch",
                "the pinned definition is not a collaboration runtime",
            )
        if (
            definition.lifecycle is not ModuleLifecycle.ACTIVE
            or instance.lifecycle is not ModuleLifecycle.ACTIVE
        ):
            raise RuntimeResolutionError(
                "runtime_module_disabled",
                "the pinned collaboration runtime module is not active",
            )
        if instance.config_version != run.runtime_config_version:
            raise RuntimeResolutionError(
                "runtime_config_mismatch",
                "the pinned collaboration runtime config version changed",
                details={
                    "expected": run.runtime_config_version,
                    "observed": instance.config_version,
                },
            )
        if definition.interface_version != run.runtime_interface_version:
            raise RuntimeResolutionError(
                "runtime_interface_mismatch",
                "the pinned collaboration runtime interface version changed",
            )
        capability = definition.capabilities
        if not isinstance(capability, CollaborationRuntimeCapabilityProfile):
            raise RuntimeResolutionError(
                "runtime_capability_invalid",
                "the pinned definition does not expose collaboration runtime capabilities",
            )
        if capability != run.runtime_capability_snapshot:
            raise RuntimeResolutionError(
                "runtime_capability_mismatch",
                "the pinned collaboration runtime capability snapshot changed",
            )
        if capability.interface_version != run.runtime_interface_version:
            raise RuntimeResolutionError(
                "runtime_capability_mismatch",
                "the runtime capability interface does not match the Run",
            )
        _verify_service_limits(capability)
        policy_schema_version = _policy_schema_version(run.policy_snapshot)
        if (
            policy_schema_version == 1
            and 1 not in capability.projection_schema_versions
        ):
            raise RuntimeResolutionError(
                "runtime_capability_unsupported",
                "the runtime does not support Projection v1",
            )
        if policy_schema_version == 1 and 1 not in capability.decision_schema_versions:
            raise RuntimeResolutionError(
                "runtime_capability_unsupported",
                "the runtime does not support Decision v1",
            )
        if policy_schema_version == 2 and not capability.supports_input_groups_v1():
            raise RuntimeResolutionError(
                "runtime_capability_unsupported",
                "the runtime does not support the pinned V2 input-group contract",
            )
        if CollaborationStrategy.ALL_REQUIRED.value not in capability.strategy_ids:
            raise RuntimeResolutionError(
                "runtime_capability_unsupported",
                "the runtime does not support all_required",
            )
        if capability.idempotency is not IdempotencyCapability.OPERATION_ID:
            raise RuntimeResolutionError(
                "runtime_capability_unsupported",
                "the runtime does not support operation-id idempotency",
            )


def _policy_schema_version(policy: Mapping[str, object]) -> int:
    value = policy.get("schema_version")
    if type(value) is not int or value not in {1, 2}:
        raise RuntimeResolutionError(
            "runtime_capability_unsupported",
            "the Run pins an unsupported collaboration policy schema",
        )
    return value


def _verify_service_limits(capability: CollaborationRuntimeCapabilityProfile) -> None:
    limits = {
        "max_projection_bytes": COLLABORATION_MAX_PROJECTION_BYTES,
        "max_decision_bytes": COLLABORATION_MAX_DECISION_BYTES,
        "max_intents_per_decision": COLLABORATION_MAX_INTENTS_PER_DECISION,
        "max_strategy_state_bytes": COLLABORATION_MAX_STRATEGY_STATE_BYTES,
    }
    exceeded = {
        name: {"declared": getattr(capability, name), "limit": limit}
        for name, limit in limits.items()
        if getattr(capability, name) > limit
    }
    if exceeded:
        raise RuntimeResolutionError(
            "runtime_capability_limit_exceeded",
            "the runtime capability exceeds a control-plane hard limit",
            details={"limits": exceeded},
        )
