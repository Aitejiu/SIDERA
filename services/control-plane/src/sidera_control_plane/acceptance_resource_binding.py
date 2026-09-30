"""Resolve acceptance resources from control-plane persistence facts.

The acceptance ledger is intentionally provider-neutral, but it must not trust
the browser or an adapter to invent the resource lineage it is charging. This
module keeps that trust boundary in the control plane: callers provide facts
read from persisted rows, while the guard receives only the derived chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from sidera_runtime_guard import RuntimeGuardError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionRequestModel,
    PlanningRequestModel,
    TaskModel,
)

AcceptanceScenario = Literal["profile", "revision", "second-adapter"]
AcceptanceGroup = Literal["A", "B", "C"]

_GROUP_BY_SCENARIO: dict[AcceptanceScenario, AcceptanceGroup] = {
    "profile": "A",
    "revision": "B",
    "second-adapter": "C",
}


@dataclass(frozen=True, slots=True)
class PersistedResourceFacts:
    """The relationship facts read by the service from durable rows."""

    workspace_id: UUID
    planning_request_id: UUID
    planning_kind: str
    root_task_id: UUID | None
    task_id: UUID | None
    execution_id: UUID | None
    handoff_transition_id: UUID | None


@dataclass(frozen=True, slots=True)
class RuntimeResourceChain:
    """A server-derived resource lineage used by the acceptance guard."""

    workspace_id: UUID
    planning_request_id: UUID
    root_task_id: UUID | None
    task_id: UUID | None
    execution_id: UUID | None
    scenario: AcceptanceScenario
    group: AcceptanceGroup
    keys: tuple[str, ...]

    @property
    def resource_key(self) -> str:
        if self.execution_id is not None:
            return f"execution:{self.execution_id}"
        return f"planning_request:{self.planning_request_id}"


def derive_runtime_resource_chain(
    facts: PersistedResourceFacts,
    *,
    requested_scenario: str | None = None,
    requested_group: str | None = None,
    require_execution: bool = False,
) -> RuntimeResourceChain:
    """Derive persisted identity and apply the server-selected acceptance stage."""

    _require_uuid(facts.workspace_id, "workspace_id")
    _require_uuid(facts.planning_request_id, "planning_request_id")
    if facts.planning_kind not in {"create", "revision"}:
        raise RuntimeGuardError("resource_chain_invalid")
    if facts.planning_kind == "revision" and facts.root_task_id is None:
        raise RuntimeGuardError("resource_chain_invalid")
    if require_execution and facts.execution_id is None:
        raise RuntimeGuardError("resource_unbound")
    if facts.execution_id is not None and (
        facts.root_task_id is None or facts.task_id is None
    ):
        raise RuntimeGuardError("resource_chain_invalid")

    if requested_scenario is not None and requested_scenario not in _GROUP_BY_SCENARIO:
        raise RuntimeGuardError("resource_scenario_mismatch")

    if facts.handoff_transition_id is not None:
        scenario: AcceptanceScenario = "second-adapter"
    elif facts.planning_kind == "revision":
        scenario = "revision"
    elif requested_scenario is None:
        scenario = "profile"
    else:
        scenario = cast(AcceptanceScenario, requested_scenario)
    group = _GROUP_BY_SCENARIO[scenario]

    if requested_scenario is not None and requested_scenario != scenario:
        raise RuntimeGuardError("resource_scenario_mismatch")
    if requested_group is not None and requested_group != group:
        raise RuntimeGuardError("resource_group_mismatch")

    keys = [
        f"workspace:{facts.workspace_id}",
        f"planning_request:{facts.planning_request_id}",
    ]
    if facts.root_task_id is not None:
        keys.append(f"root_task:{facts.root_task_id}")
    if facts.task_id is not None and facts.task_id != facts.root_task_id:
        keys.append(f"task:{facts.task_id}")
    if facts.execution_id is not None:
        keys.append(f"execution:{facts.execution_id}")
    if len(keys) != len(set(keys)):
        raise RuntimeGuardError("resource_chain_invalid")
    if facts.execution_id is None and facts.planning_kind != "create" and facts.root_task_id is None:
        raise RuntimeGuardError("resource_unbound")

    return RuntimeResourceChain(
        workspace_id=facts.workspace_id,
        planning_request_id=facts.planning_request_id,
        root_task_id=facts.root_task_id,
        task_id=facts.task_id,
        execution_id=facts.execution_id,
        scenario=scenario,
        group=group,
        keys=tuple(keys),
    )


def load_planning_resource_facts(
    session: Session,
    *,
    planning_request_id: UUID,
    workspace_id: UUID,
) -> PersistedResourceFacts:
    """Load a planning request's authoritative resource facts."""

    row = (
        session.execute(
            select(
                PlanningRequestModel.workspace_id.label("workspace_id"),
                PlanningRequestModel.id.label("planning_request_id"),
                PlanningRequestModel.kind.label("planning_kind"),
                PlanningRequestModel.root_task_id.label("root_task_id"),
            ).where(
                PlanningRequestModel.id == planning_request_id,
                PlanningRequestModel.workspace_id == workspace_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeGuardError("resource_unbound")
    _assert_workspace(row, workspace_id)
    return PersistedResourceFacts(
        workspace_id=row["workspace_id"],
        planning_request_id=row["planning_request_id"],
        planning_kind=row["planning_kind"],
        root_task_id=row["root_task_id"],
        task_id=row["root_task_id"],
        execution_id=None,
        handoff_transition_id=None,
    )


def load_execution_resource_facts(
    session: Session,
    *,
    execution_id: UUID,
    workspace_id: UUID,
) -> PersistedResourceFacts:
    """Resolve execution -> request -> task lineage from durable rows."""

    statement = (
        select(
            ExecutionModel.workspace_id.label("workspace_id"),
            ExecutionModel.id.label("execution_id"),
            TaskModel.id.label("task_id"),
            ExecutionRequestModel.planning_request_id.label("planning_request_id"),
            ExecutionRequestModel.root_task_id.label("root_task_id"),
            PlanningRequestModel.kind.label("planning_kind"),
            ExecutionRequestModel.handoff_transition_id.label(
                "handoff_transition_id"
            ),
        )
        .join(
            TaskModel,
            and_(
                TaskModel.id == ExecutionModel.task_id,
                TaskModel.workspace_id == ExecutionModel.workspace_id,
            ),
        )
        .join(
            ExecutionRequestModel,
            and_(
                ExecutionRequestModel.workspace_id == ExecutionModel.workspace_id,
                or_(
                    ExecutionRequestModel.first_execution_id == ExecutionModel.id,
                    ExecutionRequestModel.execution_lineage_id
                    == ExecutionModel.lineage_id,
                ),
            ),
        )
        .join(
            PlanningRequestModel,
            and_(
                PlanningRequestModel.id
                == ExecutionRequestModel.planning_request_id,
                PlanningRequestModel.workspace_id == ExecutionModel.workspace_id,
            ),
        )
        .where(
            ExecutionModel.id == execution_id,
            ExecutionModel.workspace_id == workspace_id,
        )
        .distinct()
    )
    rows = session.execute(statement).mappings().all()
    if not rows:
        raise RuntimeGuardError("resource_unbound")
    facts = [_facts_from_mapping(row) for row in rows]
    _assert_workspace(rows[0], workspace_id)
    identities = {
        (
            item.workspace_id,
            item.planning_request_id,
            item.root_task_id,
            item.task_id,
            item.execution_id,
            item.handoff_transition_id,
        )
        for item in facts
    }
    if len(identities) != 1:
        raise RuntimeGuardError("resource_chain_ambiguous")
    return facts[0]


def bind_runtime_resource_chain(
    guard: object,
    chain: RuntimeResourceChain,
    *,
    thread_id: str | None = None,
) -> None:
    """Persist a server-derived chain before the first guarded call."""

    bind_thread = getattr(guard, "bind_thread", None)
    bind_resource = getattr(guard, "bind_resource", None)
    if not callable(bind_resource):
        raise RuntimeGuardError("resource_binding_unavailable")
    if thread_id is not None:
        if not callable(bind_thread):
            raise RuntimeGuardError("thread_binding_unavailable")
        bind_thread(
            thread_id,
            group=chain.group,
            resource_chain=chain.keys,
        )
    # A resource binding stores the authoritative chain for the resource being
    # charged. The charged resource is not always the final node: planners
    # charge the planning request while workers charge the execution.
    try:
        resource_index = chain.keys.index(chain.resource_key)
    except ValueError as exc:
        raise RuntimeGuardError("resource_chain_invalid") from exc
    if resource_index < 1:
        raise RuntimeGuardError("resource_chain_invalid")
    if len(chain.keys) >= 2:
        bind_resource(
            chain.keys[resource_index - 1],
            chain.resource_key,
            resource_chain=chain.keys,
        )


def _facts_from_mapping(row: object) -> PersistedResourceFacts:
    if not hasattr(row, "__getitem__"):
        raise RuntimeGuardError("resource_chain_invalid")
    return PersistedResourceFacts(
        workspace_id=row["workspace_id"],  # type: ignore[index]
        planning_request_id=row["planning_request_id"],  # type: ignore[index]
        planning_kind=row["planning_kind"],  # type: ignore[index]
        root_task_id=row["root_task_id"],  # type: ignore[index]
        task_id=row["task_id"],  # type: ignore[index]
        execution_id=row["execution_id"],  # type: ignore[index]
        handoff_transition_id=row["handoff_transition_id"],  # type: ignore[index]
    )


def _assert_workspace(row: object, workspace_id: UUID) -> None:
    try:
        actual = row["workspace_id"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise RuntimeGuardError("resource_chain_invalid") from exc
    if actual != workspace_id:
        raise RuntimeGuardError("resource_workspace_mismatch")


def _require_uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise RuntimeGuardError(f"{name}_invalid")


__all__ = [
    "AcceptanceGroup",
    "AcceptanceScenario",
    "PersistedResourceFacts",
    "RuntimeResourceChain",
    "bind_runtime_resource_chain",
    "derive_runtime_resource_chain",
    "load_execution_resource_facts",
    "load_planning_resource_facts",
]
