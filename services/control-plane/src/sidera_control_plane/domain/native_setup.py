"""Pure contracts and stable identities for explicitly authorized native setup."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from sidera_agents.contracts import ProfileRef

from sidera_control_plane.domain.planning_profiles import PlanningBudgetPolicy
from sidera_control_plane.domain.result_integration import CheckSpecV1

NATIVE_SETUP_POLICY_KEY = "sidera.native.setup"


@dataclass(frozen=True, slots=True)
class PlanningPolicyRef:
    policy_id: UUID
    version: int


@dataclass(frozen=True, slots=True)
class WorkspaceSetupRequest:
    workspace_id: UUID
    repository_directory: Path
    expected_version: int
    planning_budget: PlanningBudgetPolicy
    planner_agent_ref: ProfileRef | None = None
    verification_checks: tuple[CheckSpecV1, ...] | None = field(
        default=None, kw_only=True
    )


@dataclass(frozen=True, slots=True)
class WorkspaceSetupReceipt:
    workspace_id: UUID
    version: int
    planning_policy_ref: PlanningPolicyRef
    repository_ref: str
    status: Literal["created", "updated", "unchanged"]


@dataclass(frozen=True, slots=True)
class WorkspaceSetupView:
    workspace_id: UUID
    version: int
    planning_policy_ref: PlanningPolicyRef
    repository_ref: str
    planner_agent_ref: ProfileRef


def native_local_slot_ids(
    workspace_module_id: UUID, verification_module_id: UUID
) -> tuple[UUID, UUID]:
    return (
        uuid5(workspace_module_id, "sidera.native.worker:agent-run"),
        uuid5(
            workspace_module_id,
            "sidera.native.worker:local-verification:" + verification_module_id.hex,
        ),
    )
