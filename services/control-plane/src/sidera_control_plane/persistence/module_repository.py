from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import ProjectionChangePublisherPort
from sidera_control_plane.application.projection_invalidation import (
    projection_targets_for_module,
)
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.modules import (
    WORKER_ADAPTER_MODULE_TYPE,
    WORKSPACE_PROVIDER_MODULE_TYPE,
    CancellationCapability,
    CapabilityInput,
    ExecutionBinding,
    IdempotencyCapability,
    ModuleCapabilityProfile,
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
    canonical_module_type,
)
from sidera_control_plane.persistence.models import (
    ExecutionBindingModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
)


class ModuleRepository:
    def __init__(
        self,
        session: Session,
        projection_changes: ProjectionChangePublisherPort | None = None,
    ) -> None:
        self._session = session
        self._projection_changes = projection_changes

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

    def get_instances(
        self,
        module_ids: Collection[UUID],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ModuleInstance, ...]:
        ordered_ids = tuple(dict.fromkeys(module_ids))
        if not ordered_ids:
            return ()
        statement = (
            select(ModuleInstanceModel)
            .where(
                ModuleInstanceModel.id.in_(ordered_ids),
                ModuleInstanceModel.workspace_id == workspace_id,
            )
            .order_by(ModuleInstanceModel.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        rows = self._session.scalars(statement).all()
        by_id = {row.id: _to_domain_instance(row) for row in rows}
        return tuple(by_id[module_id] for module_id in ordered_ids if module_id in by_id)

    def get_definitions(
        self,
        module_ids: Collection[UUID],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ModuleDefinition, ...]:
        ordered_ids = tuple(dict.fromkeys(module_ids))
        if not ordered_ids:
            return ()
        statement = (
            select(ModuleDefinitionModel)
            .where(
                ModuleDefinitionModel.id.in_(ordered_ids),
                ModuleDefinitionModel.workspace_id == workspace_id,
            )
            .order_by(ModuleDefinitionModel.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        rows = self._session.scalars(statement).all()
        by_id = {row.id: _to_domain_definition(row) for row in rows}
        return tuple(by_id[module_id] for module_id in ordered_ids if module_id in by_id)

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
                resource_version=definition.resource_version,
            )
        )
        self._publish_module_change(
            workspace_id=definition.workspace_id,
            resource_version=definition.config_schema_version,
            change_type="module.definition_created",
            correlation_id=definition.id,
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
                resource_version=instance.resource_version,
            )
        )
        self._publish_module_change(
            workspace_id=instance.workspace_id,
            resource_version=instance.config_version,
            change_type="module.instance_created",
            correlation_id=instance.id,
        )

    def save_definition(
        self, definition: ModuleDefinition, previous_version: int
    ) -> None:
        result = self._session.execute(
            update(ModuleDefinitionModel)
            .where(
                ModuleDefinitionModel.id == definition.id,
                ModuleDefinitionModel.workspace_id == definition.workspace_id,
                ModuleDefinitionModel.resource_version == previous_version,
            )
            .values(
                lifecycle=definition.lifecycle.value,
                resource_version=definition.resource_version,
                updated_at=datetime.now(UTC),
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_definition_version(
                    definition.id, definition.workspace_id
                ),
            )
        self._publish_module_change(
            workspace_id=definition.workspace_id,
            resource_version=definition.resource_version,
            change_type="module.definition_lifecycle_changed",
            correlation_id=definition.id,
        )

    def save_instance(self, instance: ModuleInstance, previous_version: int) -> None:
        result = self._session.execute(
            update(ModuleInstanceModel)
            .where(
                ModuleInstanceModel.id == instance.id,
                ModuleInstanceModel.workspace_id == instance.workspace_id,
                ModuleInstanceModel.resource_version == previous_version,
            )
            .values(
                lifecycle=instance.lifecycle.value,
                resource_version=instance.resource_version,
                updated_at=datetime.now(UTC),
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_instance_version(instance.id, instance.workspace_id),
            )
        self._publish_module_change(
            workspace_id=instance.workspace_id,
            resource_version=instance.resource_version,
            change_type="module.instance_lifecycle_changed",
            correlation_id=instance.id,
        )

    def _current_definition_version(self, module_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(ModuleDefinitionModel.resource_version).where(
                ModuleDefinitionModel.id == module_id,
                ModuleDefinitionModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0

    def _current_instance_version(self, module_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(ModuleInstanceModel.resource_version).where(
                ModuleInstanceModel.id == module_id,
                ModuleInstanceModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0

    def _publish_module_change(
        self,
        *,
        workspace_id: UUID,
        resource_version: int,
        change_type: str,
        correlation_id: UUID,
    ) -> None:
        if self._projection_changes is None:
            return
        for target in projection_targets_for_module():
            self._projection_changes.changed(
                workspace_id=workspace_id,
                projection_type=target.projection_type,
                resource_id=target.resource_id,
                change_type=change_type,
                resource_version=resource_version,
                correlation_id=correlation_id,
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


def capability_profile_to_payload(
    profile: ModuleCapabilityProfile,
) -> dict[str, object]:
    if isinstance(profile, WorkerAdapterCapabilityProfile):
        return {
            "input": profile.input.value,
            "idempotency": profile.idempotency.value,
            "resume": profile.resume.value,
            "stop": profile.stop.value,
            "sandbox": profile.sandbox.value,
            "stability": profile.stability.value,
            "context_package_versions": list(profile.context_package_versions),
            "context_patch_versions": list(profile.context_patch_versions),
        }
    if isinstance(profile, WorkspaceProviderCapabilityProfile):
        return {
            "isolation": profile.isolation.value,
            "writable": profile.writable,
            "git_worktree": profile.git_worktree,
            "stability": profile.stability.value,
        }
    return {
        "structured_output": profile.structured_output.value,
        "clarification": profile.clarification,
        "idempotency": profile.idempotency.value,
        "cancellation": profile.cancellation.value,
        "usage_reporting": profile.usage_reporting.value,
        "max_context_bytes": profile.max_context_bytes,
        "stability": profile.stability.value,
        "interface_version": profile.interface_version,
        "proposal_schema_versions": list(profile.proposal_schema_versions),
        "context_package_versions": list(profile.context_package_versions),
    }


def capability_profile_from_payload(
    module_type: str,
    payload: Mapping[str, object],
) -> ModuleCapabilityProfile:
    canonical_type = canonical_module_type(module_type)
    if not isinstance(payload, Mapping):
        raise ValidationError("capability payload must be a mapping")

    if canonical_type == WORKER_ADAPTER_MODULE_TYPE:
        _reject_unknown_fields(payload, _WORKER_FIELDS)
        return WorkerAdapterCapabilityProfile(
            input=_enum_capability(payload, "input", CapabilityInput),
            idempotency=_enum_capability(
                payload, "idempotency", IdempotencyCapability
            ),
            resume=_enum_capability(payload, "resume", ResumeCapability),
            stop=_enum_capability(payload, "stop", StopCapability),
            sandbox=_enum_capability(payload, "sandbox", SandboxCapability),
            stability=_enum_capability(payload, "stability", StabilityLevel),
            context_package_versions=_version_tuple_capability(payload),
            context_patch_versions=_version_tuple_capability(
                payload, "context_patch_versions"
            ),
        )

    if canonical_type == WORKSPACE_PROVIDER_MODULE_TYPE:
        if (
            _LEGACY_WORKER_FIELDS.issubset(payload)
            and set(payload).issubset(_WORKER_FIELDS)
        ):
            legacy = capability_profile_from_payload(
                WORKER_ADAPTER_MODULE_TYPE, payload
            )
            assert isinstance(legacy, WorkerAdapterCapabilityProfile)
            return WorkspaceProviderCapabilityProfile(
                isolation=legacy.sandbox,
                writable=legacy.sandbox is not SandboxCapability.NONE,
                git_worktree=False,
                stability=legacy.stability,
            )
        _reject_unknown_fields(payload, _WORKSPACE_FIELDS)
        return WorkspaceProviderCapabilityProfile(
            isolation=_enum_capability(payload, "isolation", SandboxCapability),
            writable=_bool_capability(payload, "writable"),
            git_worktree=_bool_capability(payload, "git_worktree"),
            stability=_enum_capability(payload, "stability", StabilityLevel),
        )

    _reject_unknown_fields(payload, _PLANNER_FIELDS)
    proposal_versions = payload.get("proposal_schema_versions")
    if not isinstance(proposal_versions, (list, tuple)):
        raise ValidationError("capability field 'proposal_schema_versions' is required")
    return PlannerAdapterCapabilityProfile(
        structured_output=_enum_capability(
            payload, "structured_output", StructuredOutputCapability
        ),
        clarification=_bool_capability(payload, "clarification"),
        idempotency=_enum_capability(
            payload, "idempotency", IdempotencyCapability
        ),
        cancellation=_enum_capability(
            payload, "cancellation", CancellationCapability
        ),
        usage_reporting=_enum_capability(
            payload, "usage_reporting", UsageReportingCapability
        ),
        max_context_bytes=_positive_int_capability(payload, "max_context_bytes"),
        stability=_enum_capability(payload, "stability", StabilityLevel),
        interface_version=_required_string(payload, "interface_version"),
        proposal_schema_versions=tuple(proposal_versions),
        context_package_versions=_version_tuple_capability(payload),
    )


def _to_domain_definition(row: ModuleDefinitionModel) -> ModuleDefinition:
    return ModuleDefinition(
        id=row.id,
        workspace_id=row.workspace_id,
        module_type=row.module_type,
        provider=row.provider,
        interface_version=row.interface_version,
        config_schema_version=row.config_schema_version,
        capabilities=capability_profile_from_payload(
            row.module_type, row.capabilities
        ),
        lifecycle=ModuleLifecycle(row.lifecycle),
        resource_version=row.resource_version,
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
        resource_version=row.resource_version,
    )


def _to_domain_binding(row: ExecutionBindingModel) -> ExecutionBinding:
    capability_snapshot = capability_profile_from_payload(
        WORKER_ADAPTER_MODULE_TYPE, row.capability_snapshot
    )
    if not isinstance(capability_snapshot, WorkerAdapterCapabilityProfile):
        raise ValidationError("execution binding capability snapshot must be a worker")
    return ExecutionBinding(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        adapter_module_id=row.adapter_module_id,
        workspace_module_id=row.workspace_module_id,
        adapter_config_version=row.adapter_config_version,
        workspace_config_version=row.workspace_config_version,
        capability_snapshot=capability_snapshot,
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
        version=row.version,
    )


_WORKER_FIELDS = frozenset(
    {
        "input",
        "idempotency",
        "resume",
        "stop",
        "sandbox",
        "stability",
        "context_package_versions",
        "context_patch_versions",
    }
)
_LEGACY_WORKER_FIELDS = frozenset(
    {"input", "idempotency", "resume", "stop", "sandbox", "stability"}
)
_WORKSPACE_FIELDS = frozenset({"isolation", "writable", "git_worktree", "stability"})
_PLANNER_FIELDS = frozenset(
    {
        "structured_output",
        "clarification",
        "idempotency",
        "cancellation",
        "usage_reporting",
        "max_context_bytes",
        "stability",
        "interface_version",
        "proposal_schema_versions",
        "context_package_versions",
    }
)


def _reject_unknown_fields(
    payload: Mapping[str, object],
    allowed: frozenset[str],
) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ValidationError(f"unknown capability fields: {', '.join(unknown)}")


def _required_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ValidationError(f"capability field {name!r} must be a string")
    return value


def _enum_capability[EnumCapability: StrEnum](
    payload: Mapping[str, object],
    name: str,
    enum_type: type[EnumCapability],
) -> EnumCapability:
    value = _required_string(payload, name)
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported capability {name}: {value!r}") from exc


def _bool_capability(payload: Mapping[str, object], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise ValidationError(f"capability field {name!r} must be a boolean")
    return value


def _positive_int_capability(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"capability field {name!r} must be positive")
    return value


def _version_tuple_capability(
    payload: Mapping[str, object], field_name: str = "context_package_versions"
) -> tuple[int, ...]:
    return _version_tuple_capability_named(payload, field_name)


def _version_tuple_capability_named(
    payload: Mapping[str, object], field_name: str
) -> tuple[int, ...]:
    value = payload.get(field_name, ())
    if not isinstance(value, (list, tuple)):
        raise ValidationError(
            f"capability field {field_name!r} must be an array"
        )
    if any(
        not isinstance(version, int) or isinstance(version, bool) or version < 1
        for version in value
    ):
        raise ValidationError(
            f"capability field {field_name!r} must contain positive integers"
        )
    return tuple(sorted(set(value)))
