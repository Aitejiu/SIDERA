from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.worker import Worker


class WorkerCommandHandler:
    command_types = frozenset({"RegisterWorker", "HeartbeatWorker", "DrainWorker"})

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "RegisterWorker":
            return self._register(uow, envelope)
        return self._mutate(uow, envelope)

    def _register(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if not uow.tasks.workspace_exists(envelope.workspace_id):
            raise NotFound("workspace not found")

        worker = Worker.register(
            worker_id=uuid4(),
            workspace_id=envelope.workspace_id,
            adapter_type=_required_str(envelope.payload, "adapter_type"),
            display_name=_required_str(envelope.payload, "display_name"),
            capabilities=frozenset(
                _required_string_tuple(envelope.payload, "capabilities")
            ),
            capacity=_required_int(envelope.payload, "capacity"),
            cost_per_hour_microunits=_required_int(
                envelope.payload, "cost_per_hour_microunits"
            ),
            startup_latency_ms=_required_int(envelope.payload, "startup_latency_ms"),
            now=self._clock(),
        )
        event = EventData(
            id=uuid4(),
            workspace_id=worker.workspace_id,
            aggregate_type="worker",
            aggregate_id=worker.id,
            aggregate_version=worker.version,
            event_type="WorkerRegistered",
            payload={
                "adapter_type": worker.adapter_type,
                "display_name": worker.display_name,
                "status": worker.status.value,
                "capabilities": sorted(worker.capabilities),
                "capacity": worker.capacity,
                "current_load": worker.current_load,
                "cost_per_hour_microunits": worker.cost_per_hour_microunits,
                "startup_latency_ms": worker.startup_latency_ms,
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        uow.workers.add(worker)
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=worker.id,
            aggregate_version=worker.version,
            event_ids=(event.id,),
        )

    def _mutate(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError(
                "mutation commands require target_id and expected_version"
            )
        if envelope.expected_version < 1:
            raise ValidationError("expected_version must be positive")

        worker = uow.workers.get(envelope.target_id, envelope.workspace_id)
        if worker is None:
            raise NotFound("worker not found")
        if worker.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, worker.version)

        previous_version = worker.version
        if envelope.command_type == "HeartbeatWorker":
            worker.heartbeat(
                healthy=_required_bool(envelope.payload, "healthy"),
                now=self._clock(),
            )
            event_type = "WorkerHeartbeatRecorded"
        elif envelope.command_type == "DrainWorker":
            worker.drain()
            event_type = "WorkerDraining"
        else:
            raise ValidationError(f"unsupported worker command: {envelope.command_type}")

        uow.workers.save(worker, previous_version)
        event = EventData(
            id=uuid4(),
            workspace_id=worker.workspace_id,
            aggregate_type="worker",
            aggregate_id=worker.id,
            aggregate_version=worker.version,
            event_type=event_type,
            payload={
                "status": worker.status.value,
                "current_load": worker.current_load,
                "capacity": worker.capacity,
                "last_heartbeat_at": worker.last_heartbeat_at.isoformat(),
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
            aggregate_version=worker.version,
            event_ids=(event.id,),
        )


def _required_str(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    return value


def _required_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    return value


def _required_string_tuple(payload: dict[str, Any], name: str) -> tuple[str, ...]:
    value = payload.get(name)
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValidationError(f"{name} must be a list of strings")
    return tuple(value)


def _required_bool(payload: dict[str, Any], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value
