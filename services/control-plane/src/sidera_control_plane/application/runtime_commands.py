from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.plan_transition_gate import (
    PlanTransitionGate,
    PlanTransitionGateOperation,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    WorkerRuntimeResolver,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    InvalidTransition,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.modules import ModuleLifecycle
from sidera_control_plane.domain.scheduling import SchedulingIssue
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration


class RuntimeCommandHandler:
    command_types = frozenset(
        {
            "ConfigureWorkerRuntime",
            "SetModuleLifecycle",
            "PinExecutionWorker",
            "ClearExecutionWorkerPin",
        }
    )

    def __init__(
        self,
        clock: Callable[[], datetime],
        resolver: WorkerRuntimeResolver | None = None,
    ) -> None:
        self._clock = clock
        self._resolver = resolver or WorkerRuntimeResolver()

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "ConfigureWorkerRuntime":
            return self._configure_worker_runtime(uow, envelope)
        if envelope.command_type == "SetModuleLifecycle":
            return self._set_module_lifecycle(uow, envelope)
        if envelope.command_type == "PinExecutionWorker":
            return self._pin_execution_worker(uow, envelope)
        if envelope.command_type == "ClearExecutionWorkerPin":
            return self._clear_execution_worker_pin(uow, envelope)
        raise ValidationError(f"unsupported runtime command: {envelope.command_type}")

    def _pin_execution_worker(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        execution = _get_target_execution(uow, envelope)
        _require_execution_transition_gate(uow, execution)
        worker_id = _required_uuid(envelope.payload, "worker_id")
        worker = uow.workers.get(
            worker_id,
            execution.workspace_id,
            for_update=True,
        )
        if worker is None:
            raise NotFound("worker not found")
        missing_capabilities = execution.capability_requirements - worker.capabilities
        if missing_capabilities:
            raise ValidationError(
                "worker does not satisfy execution capabilities",
                details={
                    "worker_id": str(worker.id),
                    "missing_capabilities": sorted(missing_capabilities),
                },
            )
        if worker.adapter_type in execution.adapter_forbidden:
            raise ValidationError(
                "worker adapter is forbidden for execution",
                details={
                    "worker_id": str(worker.id),
                    "adapter_type": worker.adapter_type,
                },
            )
        configuration = uow.worker_runtime_configurations.get_for_worker(
            worker.id,
            execution.workspace_id,
        )
        previous_version = execution.version
        execution.pin_worker(worker.id)
        uow.executions.save(execution, previous_version)
        event = _runtime_event(
            envelope,
            workspace_id=execution.workspace_id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_type="ExecutionWorkerPinned",
            payload={
                "worker_id": str(worker.id),
                "adapter_type": worker.adapter_type,
                "runtime_configuration_present": configuration is not None,
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_ids=(event.id,),
        )

    def _clear_execution_worker_pin(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        execution = _get_target_execution(uow, envelope)
        _require_execution_transition_gate(uow, execution)
        previous_worker_id = execution.pinned_worker_id
        previous_version = execution.version
        execution.clear_worker_pin()
        uow.executions.save(execution, previous_version)
        event = _runtime_event(
            envelope,
            workspace_id=execution.workspace_id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_type="ExecutionWorkerPinCleared",
            payload={
                "previous_worker_id": str(previous_worker_id),
                "worker_id": None,
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_ids=(event.id,),
        )

    def _configure_worker_runtime(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_id is None:
            raise ValidationError("ConfigureWorkerRuntime requires a worker target")
        worker = uow.workers.get(
            envelope.target_id,
            envelope.workspace_id,
            for_update=True,
        )
        if worker is None:
            raise NotFound("worker not found")

        existing = uow.worker_runtime_configurations.get_for_worker(
            worker.id,
            envelope.workspace_id,
            for_update=True,
        )
        values = _configuration_values(envelope.payload)
        if existing is None:
            if envelope.expected_version is not None:
                raise ValidationError(
                    "new runtime configuration must not provide expected_version"
                )
            configuration = WorkerRuntimeConfiguration.create(
                worker_id=worker.id,
                workspace_id=envelope.workspace_id,
                **values,
            )
            previous_version: int | None = None
        else:
            if envelope.expected_version is None:
                raise ValidationError(
                    "existing runtime configuration requires expected_version"
                )
            if existing.version != envelope.expected_version:
                raise VersionConflict(envelope.expected_version, existing.version)
            previous_version = existing.version
            configuration = replace(existing)
            configuration.reconfigure(**values)

        instance_ids = tuple(
            sorted(
                {
                    configuration.adapter_module_instance_id,
                    configuration.workspace_module_instance_id,
                }
            )
        )
        instances = uow.modules.get_instances(
            instance_ids,
            envelope.workspace_id,
            for_update=True,
        )
        instance_by_id = {instance.id: instance for instance in instances}
        definition_ids = tuple(
            sorted(instance.definition_id for instance in instances)
        )
        definitions = uow.modules.get_definitions(
            definition_ids,
            envelope.workspace_id,
            for_update=True,
        )
        resolved = self._resolver.validate_configuration(
            worker,
            configuration,
            instance_by_id,
            {definition.id: definition for definition in definitions},
        )
        if not isinstance(resolved, ResolvedWorkerRuntime):
            _raise_runtime_issue(resolved)

        if previous_version is None:
            uow.worker_runtime_configurations.add(configuration)
        else:
            uow.worker_runtime_configurations.save(configuration, previous_version)
        event = EventData(
            id=uuid4(),
            workspace_id=worker.workspace_id,
            aggregate_type="worker_runtime_configuration",
            aggregate_id=worker.id,
            aggregate_version=configuration.version,
            event_type="WorkerRuntimeConfigured",
            payload={
                "adapter_module_instance_id": str(
                    configuration.adapter_module_instance_id
                ),
                "adapter_module_config_version": configuration.adapter_module_config_version,
                "workspace_module_instance_id": str(
                    configuration.workspace_module_instance_id
                ),
                "workspace_module_config_version": configuration.workspace_module_config_version,
                "configuration_version": configuration.version,
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=worker.id,
            aggregate_version=configuration.version,
            event_ids=(event.id,),
            data={
                "worker_id": str(worker.id),
                "configuration_version": configuration.version,
                "adapter_module_instance_id": str(
                    configuration.adapter_module_instance_id
                ),
                "workspace_module_instance_id": str(
                    configuration.workspace_module_instance_id
                ),
            },
        )

    def _set_module_lifecycle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError(
                "SetModuleLifecycle requires target_id and expected_version"
            )
        if envelope.expected_version < 1:
            raise ValidationError("expected_version must be positive")
        if envelope.target_type == "module_definition":
            definitions = uow.modules.get_definitions(
                (envelope.target_id,),
                envelope.workspace_id,
                for_update=True,
            )
            target = definitions[0] if definitions else None
            if target is None:
                raise NotFound("module definition not found")
            if target.resource_version != envelope.expected_version:
                raise VersionConflict(
                    envelope.expected_version, target.resource_version
                )
            previous_version = target.resource_version
            changed = target.with_lifecycle(
                _required_lifecycle(envelope.payload)
            )
            uow.modules.save_definition(changed, previous_version)
        elif envelope.target_type == "module_instance":
            instances = uow.modules.get_instances(
                (envelope.target_id,),
                envelope.workspace_id,
                for_update=True,
            )
            target = instances[0] if instances else None
            if target is None:
                raise NotFound("module instance not found")
            if target.resource_version != envelope.expected_version:
                raise VersionConflict(
                    envelope.expected_version, target.resource_version
                )
            previous_version = target.resource_version
            changed = target.with_lifecycle(
                _required_lifecycle(envelope.payload)
            )
            uow.modules.save_instance(changed, previous_version)
        else:
            raise ValidationError(
                "SetModuleLifecycle target_type must be module_definition or module_instance"
            )

        event = EventData(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            aggregate_type=envelope.target_type,
            aggregate_id=envelope.target_id,
            aggregate_version=changed.resource_version,
            event_type="ModuleLifecycleChanged",
            payload={
                "previous_lifecycle": target.lifecycle.value,
                "lifecycle": changed.lifecycle.value,
                "previous_resource_version": previous_version,
                "resource_version": changed.resource_version,
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=envelope.target_id,
            aggregate_version=changed.resource_version,
            event_ids=(event.id,),
            data={
                "lifecycle": changed.lifecycle.value,
                "resource_version": changed.resource_version,
            },
        )


def _configuration_values(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "adapter_module_instance_id": _required_uuid(
            payload, "adapter_module_instance_id"
        ),
        "adapter_module_config_version": _required_positive_int(
            payload, "adapter_module_config_version"
        ),
        "workspace_module_instance_id": _required_uuid(
            payload, "workspace_module_instance_id"
        ),
        "workspace_module_config_version": _required_positive_int(
            payload, "workspace_module_config_version"
        ),
    }


def _required_uuid(payload: Mapping[str, Any], name: str) -> UUID:
    value = payload.get(name)
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a UUID") from exc


def _required_positive_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _required_lifecycle(payload: dict[str, Any]) -> ModuleLifecycle:
    value = payload.get("lifecycle")
    if not isinstance(value, str):
        raise ValidationError("lifecycle must be a string")
    try:
        return ModuleLifecycle(value)
    except ValueError as exc:
        raise ValidationError(f"unsupported module lifecycle: {value!r}") from exc


def _raise_runtime_issue(issue: SchedulingIssue) -> None:
    raise ValidationError(
        "worker runtime configuration is invalid",
        details={"reason_code": issue.reason_code.value, **dict(issue.reason_details)},
    )


def _require_execution_transition_gate(
    uow: ControlPlaneUnitOfWork, execution: Execution
) -> None:
    if execution.worker_item_id is None:
        return
    worker_items = getattr(uow, "worker_items", None)
    if worker_items is None:
        return
    placement = worker_items.get_current_placement(
        execution.worker_item_id,
        execution.workspace_id,
    )
    if placement is None:
        return
    result = PlanTransitionGate(uow).evaluate(
        worker_item_id=execution.worker_item_id,
        workspace_id=execution.workspace_id,
        plan_generation=placement.plan_generation,
        operation=PlanTransitionGateOperation.MUTATE,
    )
    if not result.allowed:
        raise InvalidTransition(
            f"execution routing mutation is blocked by {result.reason}"
        )


def _get_target_execution(
    uow: ControlPlaneUnitOfWork,
    envelope: CommandEnvelope,
) -> Execution:
    if envelope.target_type != "execution":
        raise ValidationError("execution routing commands require target_type=execution")
    if envelope.target_id is None or envelope.expected_version is None:
        raise ValidationError(
            "execution routing commands require target_id and expected_version"
        )
    if envelope.expected_version < 1:
        raise ValidationError("expected_version must be positive")
    execution = uow.executions.get(
        envelope.target_id,
        envelope.workspace_id,
        for_update=True,
    )
    if execution is None:
        raise NotFound("execution not found")
    if execution.version != envelope.expected_version:
        raise VersionConflict(envelope.expected_version, execution.version)
    return execution


def _runtime_event(
    envelope: CommandEnvelope,
    *,
    workspace_id: UUID,
    aggregate_id: UUID,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, Any],
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=workspace_id,
        aggregate_type="execution",
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        payload=payload,
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )
