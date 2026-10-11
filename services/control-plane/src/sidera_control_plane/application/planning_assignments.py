"""Explicit human-confirmed responsibilities for canonical planning targets."""

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as ShapeError

from sidera_control_plane.domain.agent_bindings import (
    AgentBinding,
    AssigneeRef,
    AssignmentDenied,
    HandoffRequired,
)
from sidera_control_plane.domain.errors import (
    DomainError,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.plan_revisions import IdentityStrategy
from sidera_control_plane.domain.planning import PlanningRequestKind


class AssignmentRequired(DomainError):
    code = "assignment_required"


class AssignmentChangeRequired(DomainError):
    code = "assignment_change_required"


class PlanningAssigneeChoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_type: Literal["subtask", "worker_item"]
    target_key: str
    assignee_kind: Literal["human", "profile"]
    assignee_id: str
    profile_revision: Annotated[StrictInt, Field(gt=0)] | None
    expected_binding_version: Annotated[StrictInt, Field(gt=0)] | None

    @field_validator("target_key", "assignee_id")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("nonempty responsibility identity required")
        return value.strip()

    @model_validator(mode="after")
    def validate_pin(self):
        if self.assignee_kind == "human":
            if self.profile_revision is not None:
                raise ValueError("human has no Profile revision")
        elif self.profile_revision is None:
            raise ValueError("exact Profile revision required")
        else:
            UUID(self.assignee_id)
        return self


def parse_assignee_bindings(raw) -> tuple[PlanningAssigneeChoice, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("assignee_bindings must be an array")
    try:
        choices = tuple(PlanningAssigneeChoice.model_validate(item) for item in raw)
    except (ShapeError, TypeError, ValueError):
        raise ValidationError("invalid explicit assignee_bindings") from None
    if len({(item.target_type, item.target_key) for item in choices}) != len(choices):
        raise ValidationError("duplicate responsibility target")
    return choices


@dataclass(frozen=True)
class _ResolvedChoice:
    choice: PlanningAssigneeChoice
    assignee: AssigneeRef
    profile_ref: object
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlanningAssignments:
    handler: object
    resolved: dict
    reused: dict
    preserved: dict

    def apply(
        self, uow, *, workspace_id, subtask_mapping, worker_mapping, envelope, now
    ):
        bindings, events = [], []
        mappings = {"subtask": subtask_mapping, "worker_item": worker_mapping}
        actual = {(kind, key) for kind, mapping in mappings.items() for key in mapping}
        if actual != set(self.resolved) | set(self.reused):
            raise ValidationError(
                "materialized responsibility mappings differ from proposal"
            )
        for kind, key in sorted(actual):
            target_id = mappings[kind][key]
            self.handler.authorize_target(workspace_id, kind, target_id)
            current = uow.agent_bindings.get(
                workspace_id, kind, target_id, for_update=True
            )
            if (kind, key) in self.reused:
                if (
                    target_id != self.reused[(kind, key)]
                    or current != self.preserved[(kind, key)]
                ):
                    raise ValidationError(
                        "reused responsibility changed during confirmation"
                    )
                bindings.append(current)
                continue
            if current is not None:
                raise ValidationError("new target already has a responsibility")
            resolved = self.resolved[(kind, key)]
            binding = AgentBinding(
                workspace_id,
                kind,
                target_id,
                resolved.assignee,
                resolved.profile_ref,
                1,
                now,
                now,
                runnable=resolved.profile_ref is not None and not resolved.reasons,
                not_runnable_reasons=resolved.reasons,
            )
            uow.agent_bindings.append(binding)
            repository = uow.tasks if kind == "subtask" else uow.worker_items
            aggregate = repository.get(target_id, workspace_id, for_update=True)
            if aggregate is None:
                raise ValidationError("materialized assignment target unavailable")
            previous_version = aggregate.version
            aggregate.version += 1
            if kind == "worker_item":
                aggregate.updated_at = now
            repository.save(aggregate, previous_version)
            events.append(
                EventData(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    aggregate_type="task" if kind == "subtask" else "worker_item",
                    aggregate_id=target_id,
                    aggregate_version=aggregate.version,
                    event_type="AgentBindingChanged",
                    payload=binding.to_mapping(),
                    actor_type=envelope.actor_type,
                    actor_id=envelope.actor_id,
                    causation_id=envelope.id,
                    correlation_id=envelope.correlation_id,
                )
            )
            bindings.append(binding)
        return tuple(bindings), tuple(events)


def prepare_planning_assignments(
    uow, *, request, proposal, envelope, handler, identity
):
    if request.profile_snapshot.execution_mode != "native_profile":
        if "assignee_bindings" in envelope.payload:
            raise ValidationError(
                "legacy confirmation cannot supply native assignee_bindings"
            )
        return None
    if handler is None or identity is None or identity.actor_type != "user":
        raise AssignmentDenied("verified human responsibility confirmation required")
    choices = parse_assignee_bindings(envelope.payload.get("assignee_bindings", ()))
    by_key = {(item.target_type, item.target_key): item for item in choices}
    targets = {("subtask", item.key): item for item in proposal.payload.subtasks}
    targets.update(
        {("worker_item", item.key): item for item in proposal.payload.worker_items}
    )
    if set(by_key) - set(targets):
        raise ValidationError("responsibility target is absent from the exact proposal")
    reused = {}
    if request.kind is PlanningRequestKind.REVISION:
        reused.update(
            {
                ("subtask", item.target_subtask_key): item.existing_subtask_task_id
                for item in proposal.payload.subtask_identity_decisions
                if item.strategy is IdentityStrategy.REUSE
            }
        )
        reused.update(
            {
                (
                    "worker_item",
                    item.target_worker_item_key,
                ): item.existing_worker_item_id
                for item in proposal.payload.worker_item_identity_decisions
                if item.strategy is IdentityStrategy.REUSE
            }
        )
    if set(targets) - set(reused) - set(by_key):
        raise AssignmentRequired("every new target requires an explicit responsibility")
    resolved, preserved = {}, {}
    for target, choice in by_key.items():
        assignee = AssigneeRef(choice.assignee_kind, choice.assignee_id)
        pin, reasons = None, ()
        if assignee.kind == "human":
            if assignee.id != identity.principal_id:
                raise AssignmentDenied("human must be the trusted current local user")
        else:
            requirements = (
                frozenset()
                if target[0] == "subtask"
                else frozenset(targets[target].capability_requirements)
            )
            pin, reasons = handler.resolve_profile(
                request.workspace_id, assignee.id, choice.profile_revision, requirements
            )
        if target not in reused and choice.expected_binding_version is not None:
            raise ValidationError("new responsibility cannot name a binding version")
        resolved[target] = _ResolvedChoice(choice, assignee, pin, reasons)
    for target, target_id in reused.items():
        handler.authorize_target(request.workspace_id, target[0], target_id)
        current = uow.agent_bindings.get(
            request.workspace_id, target[0], target_id, for_update=True
        )
        if current is None:
            raise AssignmentRequired("reused target has no canonical responsibility")
        supplied = resolved.get(target)
        if supplied is not None:
            expected = supplied.choice.expected_binding_version
            if expected is None:
                raise ValidationError(
                    "reused responsibility requires its exact binding version"
                )
            if expected != current.version:
                raise VersionConflict(expected, current.version)
            if (
                supplied.assignee != current.assignee
                or supplied.profile_ref != current.profile_ref
            ):
                active, _requirements = uow.agent_bindings.execution_facts(
                    request.workspace_id, target[0], target_id
                )
                if active:
                    raise HandoffRequired(
                        "active or uncertain owner changes require handoff"
                    )
                raise AssignmentChangeRequired(
                    "owner change requires explicit assignment"
                )
        preserved[target] = current
    return PlanningAssignments(handler, resolved, reused, preserved)
