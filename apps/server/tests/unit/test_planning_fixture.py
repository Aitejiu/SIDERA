from dataclasses import replace
from uuid import uuid4

import pytest
from sidera_agents.contracts import ProfileRef
from sidera_agents.errors import AgentError
from sidera_control_plane.application.planning_commands import PlanningCommandTarget

from tests.support.product_lab import ControlledPrincipal


def case(tmp_path):
    workspace = uuid4()
    principal = ControlledPrincipal(workspace, "owned-human", tmp_path)
    profile = ProfileRef(workspace_id=workspace, profile_id=uuid4(), revision=1)
    policy = uuid4()
    principal.planning_selection = (policy, 1, profile)
    target = PlanningCommandTarget(
        workspace, "owned-human", None, policy, 1, profile, None, ()
    )
    return principal, target


def test_fixture_human_admission_allows_only_the_saved_exact_goal_selection(tmp_path):
    principal, target = case(tmp_path)
    principal.require(principal.identity.grant_id, "planning.submit", target)
    with pytest.raises(AgentError):
        principal.require(uuid4(), "planning.submit", target)
    with pytest.raises(AgentError):
        principal.require(principal.identity.grant_id, "planning.confirm", target)


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": uuid4()},
        {"principal_id": "wrong-human"},
        {"planning_profile_id": uuid4()},
        {"planning_profile_version": 2},
        {
            "planner_agent_ref": ProfileRef(
                workspace_id=uuid4(), profile_id=uuid4(), revision=1
            )
        },
        {"planning_request_id": uuid4()},
        {"root_task_id": uuid4()},
        {"context_refs": ("foreign-source",)},
        {"proposal_content_hash": "foreign-proposal"},
    ],
)
def test_fixture_human_admission_denies_mismatched_or_confirmation_scope(
    tmp_path, changes
):
    principal, target = case(tmp_path)
    with pytest.raises(AgentError):
        principal.require(
            principal.identity.grant_id, "planning.submit", replace(target, **changes)
        )
