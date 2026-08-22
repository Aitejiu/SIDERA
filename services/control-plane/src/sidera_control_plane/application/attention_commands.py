from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.attention import AttentionAcknowledgement
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.events import EventData


class AcknowledgeAttentionItemHandler:
    command_types = frozenset({"AcknowledgeAttentionItem"})

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_type != "task":
            raise ValidationError("AcknowledgeAttentionItem requires target_type=task")
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError(
                "AcknowledgeAttentionItem requires target_id and expected_version"
            )
        if envelope.expected_version < 1:
            raise ValidationError("expected_version must be positive")

        root_task = uow.tasks.get(
            envelope.target_id, envelope.workspace_id, for_update=True
        )
        if root_task is None:
            raise NotFound("root task not found")
        if root_task.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, root_task.version)

        attention_key = _required_str(envelope.payload, "attention_key")
        source_correlation_id = _required_uuid(
            envelope.payload, "source_correlation_id"
        )
        source_resource_version = _required_positive_int(
            envelope.payload, "source_resource_version"
        )
        source = uow.attention.get_current_source(
            root_task.id, envelope.workspace_id, attention_key
        )
        if source is None:
            raise ValidationError(
                "attention item is no longer current",
                details={"attention_key": attention_key},
            )
        if (
            source.correlation_id != source_correlation_id
            or source.resource_version != source_resource_version
        ):
            raise ValidationError(
                "attention item source is stale",
                details={
                    "attention_key": attention_key,
                    "current_source_correlation_id": str(source.correlation_id),
                    "current_resource_version": source.resource_version,
                },
            )

        previous = uow.attention.get(
            envelope.workspace_id,
            envelope.actor_type,
            envelope.actor_id,
            attention_key,
        )
        if previous is not None and previous.applies_to(source):
            return CommandResult(
                command_id=envelope.id,
                aggregate_id=root_task.id,
                aggregate_version=root_task.version,
                event_ids=(),
            )

        acknowledgement = AttentionAcknowledgement(
            workspace_id=envelope.workspace_id,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            attention_key=attention_key,
            root_task_id=root_task.id,
            source_correlation_id=source_correlation_id,
            source_resource_version=source_resource_version,
            acknowledged_command_id=envelope.id,
            acknowledged_at=self._clock(),
        )
        uow.attention.upsert(acknowledgement)
        event = EventData(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            aggregate_type="attention",
            aggregate_id=uuid5(
                NAMESPACE_URL,
                f"{envelope.workspace_id}:{envelope.actor_type}:{envelope.actor_id}:{attention_key}",
            ),
            aggregate_version=(
                previous.source_resource_version + 1 if previous is not None else 1
            ),
            event_type="attention.item_acknowledged",
            payload={
                "attention_key": attention_key,
                "root_task_id": str(root_task.id),
                "source_correlation_id": str(source_correlation_id),
                "source_resource_version": source_resource_version,
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=root_task.id,
            aggregate_version=root_task.version,
            event_ids=(event.id,),
        )


def _required_str(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _required_uuid(payload: Mapping[str, Any], name: str) -> UUID:
    value = payload.get(name)
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a UUID") from exc


def _required_positive_int(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value
