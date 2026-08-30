from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.modules import (
    WORKER_ADAPTER_MODULE_TYPE,
    WORKSPACE_PROVIDER_MODULE_TYPE,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.scheduling import (
    SchedulingBlockReason,
    SchedulingIssue,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ResolvedWorkerRuntime:
    worker_id: UUID
    adapter_type: str
    adapter_module_instance_id: UUID
    adapter_module_config_version: int
    workspace_module_instance_id: UUID
    workspace_module_config_version: int
    adapter_definition_id: UUID
    workspace_definition_id: UUID
    capability_snapshot: WorkerAdapterCapabilityProfile
    resolution_facts: Mapping[str, JsonValue]
    workspace_capability_snapshot: WorkspaceProviderCapabilityProfile | None = None


@dataclass(frozen=True, slots=True)
class RuntimeCandidateInspection:
    worker: Worker
    resolved: ResolvedWorkerRuntime | None
    issue: SchedulingIssue | None


class WorkerRuntimeInspectionPort(Protocol):
    def inspect_candidates(
        self,
        uow: ControlPlaneUnitOfWork,
        workers: Collection[Worker],
        execution: Execution,
    ) -> tuple[RuntimeCandidateInspection, ...]:
        raise NotImplementedError


RuntimeIssue = SchedulingIssue


class WorkerRuntimeResolver:
    """Resolve one exact Worker runtime from control-plane facts only."""

    def inspect_candidates(
        self,
        uow: ControlPlaneUnitOfWork,
        workers: Collection[Worker],
        execution: Execution,
    ) -> tuple[RuntimeCandidateInspection, ...]:
        candidate_workers = tuple(workers)
        if not candidate_workers:
            return ()

        worker_ids = tuple(worker.id for worker in candidate_workers)
        configurations = uow.worker_runtime_configurations.get_for_workers(
            worker_ids,
            execution.workspace_id,
        )
        configuration_by_worker = {
            configuration.worker_id: configuration
            for configuration in configurations
        }
        instance_ids = _unique_ids(
            module_id
            for worker in candidate_workers
            for module_id in _configuration_module_ids(
                configuration_by_worker.get(worker.id)
            )
        )
        instances = uow.modules.get_instances(instance_ids, execution.workspace_id)
        instance_by_id = {instance.id: instance for instance in instances}
        definition_ids = _unique_ids(
            instance.definition_id for instance in instances
        )
        definitions = uow.modules.get_definitions(
            definition_ids,
            execution.workspace_id,
        )
        definition_by_id = {definition.id: definition for definition in definitions}

        return tuple(
            _inspection_for(
                worker=worker,
                execution=execution,
                configuration=configuration_by_worker.get(worker.id),
                instances=instance_by_id,
                definitions=definition_by_id,
            )
            for worker in candidate_workers
        )

    def lock_and_resolve(
        self,
        uow: ControlPlaneUnitOfWork,
        worker: Worker,
        execution: Execution,
    ) -> ResolvedWorkerRuntime | RuntimeIssue:
        if worker.workspace_id != execution.workspace_id:
            return _issue(
                SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
                worker,
                reason="worker_workspace_mismatch",
            )
        if worker.adapter_type in execution.adapter_forbidden:
            return _issue(
                SchedulingBlockReason.ADAPTER_POLICY_REJECTED,
                worker,
                reason="adapter_forbidden",
                adapter_type=worker.adapter_type,
            )

        # Keep this lock order identical for every assignment and configuration
        # mutation: the configuration row, module instances, then definitions.
        configuration = uow.worker_runtime_configurations.get_for_worker(
            worker.id,
            execution.workspace_id,
            for_update=True,
        )
        if configuration is None:
            return _issue(
                SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING,
                worker,
            )
        instance_ids = tuple(
            sorted(_configuration_module_ids(configuration))
        )
        instances = uow.modules.get_instances(
            instance_ids,
            execution.workspace_id,
            for_update=True,
        )
        instance_by_id = {instance.id: instance for instance in instances}
        definition_ids = tuple(
            sorted(instance.definition_id for instance in instances)
        )
        definitions = uow.modules.get_definitions(
            definition_ids,
            execution.workspace_id,
            for_update=True,
        )
        return _resolve(
            worker=worker,
            execution=execution,
            configuration=configuration,
            instances=instance_by_id,
            definitions={definition.id: definition for definition in definitions},
        )

    def validate_configuration(
        self,
        worker: Worker,
        configuration: WorkerRuntimeConfiguration,
        instances: Mapping[UUID, ModuleInstance],
        definitions: Mapping[UUID, ModuleDefinition],
    ) -> ResolvedWorkerRuntime | RuntimeIssue:
        """Validate a proposed runtime without requiring an Execution route."""
        return _resolve(
            worker=worker,
            execution=None,
            configuration=configuration,
            instances=instances,
            definitions=definitions,
        )


def _inspection_for(
    *,
    worker: Worker,
    execution: Execution,
    configuration: WorkerRuntimeConfiguration | None,
    instances: Mapping[UUID, ModuleInstance],
    definitions: Mapping[UUID, ModuleDefinition],
) -> RuntimeCandidateInspection:
    result = _resolve(
        worker=worker,
        execution=execution,
        configuration=configuration,
        instances=instances,
        definitions=definitions,
    )
    if isinstance(result, SchedulingIssue):
        return RuntimeCandidateInspection(worker=worker, resolved=None, issue=result)
    return RuntimeCandidateInspection(worker=worker, resolved=result, issue=None)


def _resolve(
    *,
    worker: Worker,
    execution: Execution | None,
    configuration: WorkerRuntimeConfiguration | None,
    instances: Mapping[UUID, ModuleInstance],
    definitions: Mapping[UUID, ModuleDefinition],
) -> ResolvedWorkerRuntime | RuntimeIssue:
    if execution is not None and worker.workspace_id != execution.workspace_id:
        return _issue(
            SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
            worker,
            reason="worker_workspace_mismatch",
        )
    if execution is not None and worker.adapter_type in execution.adapter_forbidden:
        return _issue(
            SchedulingBlockReason.ADAPTER_POLICY_REJECTED,
            worker,
            reason="adapter_forbidden",
            adapter_type=worker.adapter_type,
        )
    if configuration is None:
        return _issue(
            SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING,
            worker,
        )
    if (
        configuration.worker_id != worker.id
        or configuration.workspace_id != worker.workspace_id
    ):
        return _issue(
            SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
            worker,
            reason="configuration_identity_mismatch",
        )

    adapter_instance = instances.get(configuration.adapter_module_instance_id)
    if adapter_instance is None:
        return _issue(
            SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE,
            worker,
            reason="adapter_instance_missing",
        )
    workspace_instance = instances.get(configuration.workspace_module_instance_id)
    if workspace_instance is None:
        return _issue(
            SchedulingBlockReason.WORKSPACE_MODULE_UNAVAILABLE,
            worker,
            reason="workspace_instance_missing",
        )
    identity_issue = _validate_instance_identity(
        worker,
        adapter_instance,
        resource="adapter",
    )
    if identity_issue is not None:
        return identity_issue
    identity_issue = _validate_instance_identity(
        worker,
        workspace_instance,
        resource="workspace",
    )
    if identity_issue is not None:
        return identity_issue

    adapter_definition = definitions.get(adapter_instance.definition_id)
    if adapter_definition is None:
        return _issue(
            SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE,
            worker,
            reason="adapter_definition_missing",
        )
    workspace_definition = definitions.get(workspace_instance.definition_id)
    if workspace_definition is None:
        return _issue(
            SchedulingBlockReason.WORKSPACE_MODULE_UNAVAILABLE,
            worker,
            reason="workspace_definition_missing",
        )
    definition_identity_issue = _validate_definition_identity(
        worker,
        adapter_definition,
        resource="adapter",
    )
    if definition_identity_issue is not None:
        return definition_identity_issue
    definition_identity_issue = _validate_definition_identity(
        worker,
        workspace_definition,
        resource="workspace",
    )
    if definition_identity_issue is not None:
        return definition_identity_issue

    if configuration.adapter_module_config_version != adapter_instance.config_version:
        return _issue(
            SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
            worker,
            reason="module_config_version_mismatch",
            field="adapter_config_version",
            expected=configuration.adapter_module_config_version,
            actual=adapter_instance.config_version,
        )
    if (
        configuration.workspace_module_config_version
        != workspace_instance.config_version
    ):
        return _issue(
            SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
            worker,
            reason="module_config_version_mismatch",
            field="workspace_config_version",
            expected=configuration.workspace_module_config_version,
            actual=workspace_instance.config_version,
        )

    lifecycle_issue = _validate_lifecycle(
        worker,
        adapter_definition,
        adapter_instance,
        resource="adapter",
    )
    if lifecycle_issue is not None:
        return lifecycle_issue
    lifecycle_issue = _validate_lifecycle(
        worker,
        workspace_definition,
        workspace_instance,
        resource="workspace",
    )
    if lifecycle_issue is not None:
        return lifecycle_issue

    capability_snapshot = adapter_definition.capabilities
    if not isinstance(capability_snapshot, WorkerAdapterCapabilityProfile):
        return _issue(
            SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE,
            worker,
            reason="adapter_capability_profile_invalid",
        )
    if not isinstance(
        workspace_definition.capabilities,
        WorkspaceProviderCapabilityProfile,
    ):
        return _issue(
            SchedulingBlockReason.WORKSPACE_MODULE_UNAVAILABLE,
            worker,
            reason="workspace_capability_profile_invalid",
        )

    facts: dict[str, JsonValue] = {
        "worker_id": str(worker.id),
        "workspace_id": str(worker.workspace_id),
        "adapter_type": worker.adapter_type,
        "adapter_provider": adapter_definition.provider,
        "adapter_definition_id": str(adapter_definition.id),
        "adapter_definition_resource_version": adapter_definition.resource_version,
        "adapter_instance_id": str(adapter_instance.id),
        "adapter_instance_resource_version": adapter_instance.resource_version,
        "adapter_config_version": configuration.adapter_module_config_version,
        "workspace_provider": workspace_definition.provider,
        "workspace_definition_id": str(workspace_definition.id),
        "workspace_definition_resource_version": workspace_definition.resource_version,
        "workspace_instance_id": str(workspace_instance.id),
        "workspace_instance_resource_version": workspace_instance.resource_version,
        "workspace_config_version": configuration.workspace_module_config_version,
    }
    facts.update(_workspace_scope_facts(worker, workspace_instance))
    return ResolvedWorkerRuntime(
        worker_id=worker.id,
        adapter_type=worker.adapter_type,
        adapter_module_instance_id=adapter_instance.id,
        adapter_module_config_version=configuration.adapter_module_config_version,
        workspace_module_instance_id=workspace_instance.id,
        workspace_module_config_version=configuration.workspace_module_config_version,
        adapter_definition_id=adapter_definition.id,
        workspace_definition_id=workspace_definition.id,
        capability_snapshot=capability_snapshot,
        resolution_facts=MappingProxyType(facts),
        workspace_capability_snapshot=workspace_definition.capabilities,
    )


def _workspace_scope_facts(
    worker: Worker, workspace_instance: ModuleInstance
) -> dict[str, JsonValue]:
    """Expose only digests for the scopes needed by handoff inspection."""
    trust_boundary = _config_text(
        workspace_instance,
        ("trust_boundary", "trust_boundary_ref"),
        str(worker.workspace_id),
    )
    repository_scope = _config_text(
        workspace_instance,
        ("repository_scope", "repository_ref"),
        "workspace-default",
    )
    writable_paths = _config_paths(
        workspace_instance,
        "writable_paths",
        (".",),
    )
    readable_paths = _config_paths(
        workspace_instance,
        "readable_paths",
        writable_paths,
    )
    return {
        "workspace_trust_boundary_hash": _scope_hash(trust_boundary),
        "workspace_repository_scope_hash": _scope_hash(repository_scope),
        "workspace_path_scope_hash": _scope_hash(
            {
                "readable": sorted(readable_paths),
                "writable": sorted(writable_paths),
            }
        ),
    }


def _config_text(
    instance: ModuleInstance,
    names: tuple[str, ...],
    default: str,
) -> str:
    for name in names:
        value = _config_value(instance, name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _config_paths(
    instance: ModuleInstance,
    name: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    value = _config_value(instance, name)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return default
    paths = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return paths or default


def _config_value(instance: ModuleInstance, name: str) -> object | None:
    for key in (f"{instance.config_namespace}.{name}", name):
        if key in instance.config:
            return instance.config[key]
    return None


def _scope_hash(value: object) -> str:
    encoded = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _validate_instance_identity(
    worker: Worker,
    instance: ModuleInstance,
    *,
    resource: str,
) -> SchedulingIssue | None:
    if instance.workspace_id != worker.workspace_id:
        reason = f"{resource}_instance_workspace_mismatch"
        reason_code = _module_reason(resource)
        return _issue(reason_code, worker, reason=reason)
    return None


def _validate_definition_identity(
    worker: Worker,
    definition: ModuleDefinition,
    *,
    resource: str,
) -> SchedulingIssue | None:
    if definition.workspace_id != worker.workspace_id:
        return _issue(
            SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
            worker,
            reason=f"{resource}_definition_workspace_mismatch",
        )
    expected_type = (
        WORKER_ADAPTER_MODULE_TYPE
        if resource == "adapter"
        else WORKSPACE_PROVIDER_MODULE_TYPE
    )
    if definition.module_type != expected_type:
        return _issue(
            _module_reason(resource),
            worker,
            reason=f"{resource}_definition_type_mismatch",
            expected_type=expected_type,
            actual_type=definition.module_type,
        )
    if resource == "adapter" and definition.provider != worker.adapter_type:
        return _issue(
            SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE,
            worker,
            reason="adapter_provider_mismatch",
            expected_provider=worker.adapter_type,
            provider=definition.provider,
        )
    return None


def _validate_lifecycle(
    worker: Worker,
    definition: ModuleDefinition,
    instance: ModuleInstance,
    *,
    resource: str,
) -> SchedulingIssue | None:
    if definition.lifecycle is not ModuleLifecycle.ACTIVE:
        return _issue(
            _module_reason(resource),
            worker,
            reason="module_definition_not_active",
            lifecycle=definition.lifecycle.value,
        )
    if instance.lifecycle is not ModuleLifecycle.ACTIVE:
        return _issue(
            _module_reason(resource),
            worker,
            reason="module_instance_not_active",
            lifecycle=instance.lifecycle.value,
        )
    return None


def _module_reason(resource: str) -> SchedulingBlockReason:
    return (
        SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE
        if resource == "adapter"
        else SchedulingBlockReason.WORKSPACE_MODULE_UNAVAILABLE
    )


def _configuration_module_ids(
    configuration: WorkerRuntimeConfiguration | None,
) -> tuple[UUID, ...]:
    if configuration is None:
        return ()
    return (
        configuration.adapter_module_instance_id,
        configuration.workspace_module_instance_id,
    )


def _unique_ids(values: Iterable[UUID]) -> tuple[UUID, ...]:
    return tuple(dict.fromkeys(values))


def _issue(
    reason_code: SchedulingBlockReason,
    worker: Worker,
    **details: JsonValue,
) -> SchedulingIssue:
    return SchedulingIssue(
        reason_code=reason_code,
        reason_details={"worker_id": str(worker.id), **details},
        requires_operator=True,
    )
