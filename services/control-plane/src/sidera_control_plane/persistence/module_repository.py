from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from sidera_control_plane.domain.modules import (
    CapabilityInput,
    CapabilityProfile,
    ExecutionBinding,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
)
from sidera_control_plane.persistence.models import (
    ExecutionBindingModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
)


class ModuleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_definition(
        self, module_id: UUID, workspace_id: UUID
    ) -> ModuleDefinition | None:
        row = self._session.scalar(
            select(ModuleDefinitionModel).where(
                ModuleDefinitionModel.id == module_id,
                ModuleDefinitionModel.workspace_id == workspace_id,
            )
        )
        return _to_domain_definition(row) if row is not None else None

    def get_instance(
        self, module_id: UUID, workspace_id: UUID
    ) -> ModuleInstance | None:
        row = self._session.scalar(
            select(ModuleInstanceModel).where(
                ModuleInstanceModel.id == module_id,
                ModuleInstanceModel.workspace_id == workspace_id,
            )
        )
        return _to_domain_instance(row) if row is not None else None

    def add_definition(self, definition: ModuleDefinition) -> None:
        self._session.add(
            ModuleDefinitionModel(
                id=definition.id,
                workspace_id=definition.workspace_id,
                module_type=definition.module_type,
                provider=definition.provider,
                interface_version=definition.interface_version,
                config_schema_version=definition.config_schema_version,
                capabilities=capability_profile_to_payload(definition.capabilities),
                lifecycle=definition.lifecycle.value,
            )
        )

    def add_instance(self, instance: ModuleInstance) -> None:
        self._session.add(
            ModuleInstanceModel(
                id=instance.id,
                workspace_id=instance.workspace_id,
                definition_id=instance.definition_id,
                instance_name=instance.instance_name,
                config_version=instance.config_version,
                config_namespace=instance.config_namespace,
                config=dict(instance.config),
                secret_refs=list(instance.secret_refs),
                lifecycle=instance.lifecycle.value,
            )
        )


class ExecutionBindingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_for_execution(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionBinding | None:
        statement = select(ExecutionBindingModel).where(
            ExecutionBindingModel.execution_id == execution_id,
            ExecutionBindingModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_binding(row) if row is not None else None

    def add(self, binding: ExecutionBinding) -> None:
        self._session.add(
            ExecutionBindingModel(
                id=binding.id,
                workspace_id=binding.workspace_id,
                execution_id=binding.execution_id,
                worker_id=binding.worker_id,
                adapter_module_id=binding.adapter_module_id,
                workspace_module_id=binding.workspace_module_id,
                adapter_config_version=binding.adapter_config_version,
                workspace_config_version=binding.workspace_config_version,
                capability_snapshot=capability_profile_to_payload(
                    binding.capability_snapshot
                ),
                lease_id=binding.lease_id,
                fencing_token=binding.fencing_token,
                version=binding.version,
            )
        )


def capability_profile_to_payload(profile: CapabilityProfile) -> dict[str, str]:
    return {
        "input": profile.input.value,
        "idempotency": profile.idempotency.value,
        "resume": profile.resume.value,
        "stop": profile.stop.value,
        "sandbox": profile.sandbox.value,
        "stability": profile.stability.value,
    }


def capability_profile_from_payload(payload: Mapping[str, object]) -> CapabilityProfile:
    return CapabilityProfile(
        input=CapabilityInput(_required_capability(payload, "input")),
        idempotency=IdempotencyCapability(
            _required_capability(payload, "idempotency")
        ),
        resume=ResumeCapability(_required_capability(payload, "resume")),
        stop=StopCapability(_required_capability(payload, "stop")),
        sandbox=SandboxCapability(_required_capability(payload, "sandbox")),
        stability=StabilityLevel(_required_capability(payload, "stability")),
    )


def _to_domain_definition(row: ModuleDefinitionModel) -> ModuleDefinition:
    return ModuleDefinition(
        id=row.id,
        workspace_id=row.workspace_id,
        module_type=row.module_type,
        provider=row.provider,
        interface_version=row.interface_version,
        config_schema_version=row.config_schema_version,
        capabilities=capability_profile_from_payload(row.capabilities),
        lifecycle=ModuleLifecycle(row.lifecycle),
    )


def _to_domain_instance(row: ModuleInstanceModel) -> ModuleInstance:
    return ModuleInstance(
        id=row.id,
        workspace_id=row.workspace_id,
        definition_id=row.definition_id,
        instance_name=row.instance_name,
        config_version=row.config_version,
        config_namespace=row.config_namespace,
        config=dict(row.config),
        secret_refs=tuple(row.secret_refs),
        lifecycle=ModuleLifecycle(row.lifecycle),
    )


def _to_domain_binding(row: ExecutionBindingModel) -> ExecutionBinding:
    return ExecutionBinding(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        adapter_module_id=row.adapter_module_id,
        workspace_module_id=row.workspace_module_id,
        adapter_config_version=row.adapter_config_version,
        workspace_config_version=row.workspace_config_version,
        capability_snapshot=capability_profile_from_payload(row.capability_snapshot),
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
        version=row.version,
    )


def _required_capability(
    payload: Mapping[str, object],
    name: str,
) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise TypeError(f"capability payload field {name!r} is missing")
    return value
