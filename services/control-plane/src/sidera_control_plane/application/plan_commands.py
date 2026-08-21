from typing import Any
from uuid import uuid4

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    PlanGenerationConflict,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.plans import PlanValidationError, TaskPlanSnapshot


class PublishPlanSnapshotHandler:
    command_types = frozenset({"PublishPlanSnapshot"})

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_type != "task":
            raise ValidationError("PublishPlanSnapshot requires target_type=task")
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError(
                "PublishPlanSnapshot requires target_id and expected_version"
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

        snapshot = _snapshot_from_payload(envelope.payload)
        if snapshot.workspace_id != envelope.workspace_id:
            raise PlanValidationError(
                "workspace_mismatch", "snapshot workspace does not match command"
            )
        if snapshot.root_task_id != root_task.id:
            raise PlanValidationError(
                "root_task_mismatch", "snapshot root task does not match command"
            )

        expected_generation = _required_non_negative_int(
            envelope.payload, "expected_current_generation"
        )
        current = uow.plans.get_current_for_root(
            root_task.id, envelope.workspace_id
        )
        actual_generation = current.generation if current is not None else 0
        if expected_generation != actual_generation:
            raise PlanGenerationConflict(expected_generation, actual_generation)
        if snapshot.generation != expected_generation + 1:
            raise PlanValidationError(
                "generation_gap",
                "snapshot generation must be expected_current_generation + 1",
            )
        if current is None:
            if snapshot.parent_snapshot_id is not None:
                raise PlanValidationError(
                    "parent_mismatch", "first generation cannot have a parent snapshot"
                )
        elif snapshot.parent_snapshot_id != current.id:
            raise PlanValidationError(
                "parent_mismatch", "snapshot parent must be the current generation"
            )

        materialized_ids = tuple(
            item.task_id for item in snapshot.work_items if item.task_id is not None
        )
        loaded_tasks = uow.tasks.get_many(materialized_ids, envelope.workspace_id)
        if len(loaded_tasks) != len(materialized_ids):
            raise PlanValidationError(
                "workspace_mismatch",
                "all materialized Tasks must belong to the command workspace",
            )

        uow.plans.add(snapshot)
        event = EventData(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            aggregate_type="task_plan",
            aggregate_id=snapshot.id,
            aggregate_version=snapshot.generation,
            event_type="task.plan_snapshot_published",
            payload={
                "snapshot_id": str(snapshot.id),
                "generation": snapshot.generation,
                "root_task_id": str(root_task.id),
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


def _snapshot_from_payload(payload: dict[str, Any]) -> TaskPlanSnapshot:
    value = payload.get("snapshot")
    if not isinstance(value, dict):
        raise ValidationError("snapshot must be an object")
    return TaskPlanSnapshot.from_payload(value)


def _required_non_negative_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer")
    return value
