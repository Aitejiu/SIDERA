from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import ProfileRef

from sidera_control_plane.api.schemas import SubmitPlanRevisionPayload
from sidera_control_plane.application.assignments import TrustedAssignmentIdentity
from sidera_control_plane.application.planning_commands import PlanningCommandHandler
from sidera_control_plane.domain.agent_bindings import AssignmentDenied
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from tests.unit.test_planning_commands import WORKSPACE_ID, profile


def selected_fixture(*, denied=False, foreign=False, actor="agent"):
    original = ProfileRef(workspace_id=WORKSPACE_ID, profile_id=uuid4(), revision=1)
    selected = ProfileRef(
        workspace_id=uuid4() if foreign else WORKSPACE_ID,
        profile_id=uuid4(),
        revision=2,
    )
    policy = replace(
        profile(uuid4()),
        planner_module_instance_id=None,
        planner_module_config_version=None,
        execution_mode="native_profile",
        planner_agent_ref=original,
    )
    identity = TrustedAssignmentIdentity("operator", uuid4(), actor)
    resolutions, permissions = [], []

    def resolve(workspace, profile_id, revision):
        resolutions.append((workspace, profile_id, revision))
        if denied:
            raise AssignmentDenied("Selected Profile is unavailable")

    def require(grant, action, target):
        permissions.append((grant, action, target))

    handler = PlanningCommandHandler(
        local_actor_type="user",
        local_actor_id="operator",
        identity=identity,
        authorizer=SimpleNamespace(require=require),
        assignment_handler=SimpleNamespace(resolve_profile=resolve),
    )
    envelope = CommandEnvelope(
        uuid4(),
        WORKSPACE_ID,
        "SubmitTaskGoal",
        "workspace",
        actor,
        "operator",
        None,
        {
            "title": "Selected Planner",
            "objective": "Create a real immutable request.",
            "constraints": [],
            "acceptance_criteria": ["Exact selection survives."],
            "requested_outputs": ["A plan"],
            "context_refs": [],
            "planning_profile_id": str(policy.id),
            "planning_profile_version": policy.version,
            "planner_agent_ref": selected.model_dump(mode="json"),
        },
        None,
        str(uuid4()),
        uuid4(),
    )
    uow = SimpleNamespace(
        planning_profiles=SimpleNamespace(get=lambda *args, **kwargs: policy)
    )
    return handler, uow, envelope, policy, selected, resolutions, permissions


def test_new_goal_authorizes_selected_planner_without_changing_policy():
    handler, uow, envelope, policy, selected, resolutions, permissions = (
        selected_fixture()
    )
    original = policy.snapshot()
    handler.preflight(uow, envelope)
    assert permissions[0][1] == "planning.submit"
    assert permissions[0][2].planner_agent_ref == selected
    assert resolutions == [(WORKSPACE_ID, selected.profile_id, selected.revision)]
    assert policy.snapshot() == original


def test_new_goal_creation_uses_same_selected_snapshot_and_initial_run(monkeypatch):
    handler, uow, envelope, policy, selected, _, _ = selected_fixture()
    captured = []

    def save(unit, command, stored_policy, request, root):
        captured.append(request)
        return CommandResult(command.id, request.id, request.version, ())

    monkeypatch.setattr(handler, "_save_initial_request", save)
    normalized = replace(
        envelope,
        payload={
            **envelope.payload,
            "profile_id": str(policy.id),
            "profile_version": policy.version,
            "source_type": "task_goal",
        },
    )
    handler._submit_request(uow, normalized)
    request = captured[0]
    assert request.profile_snapshot == replace(
        policy.snapshot(), planner_agent_ref=selected
    )
    assert request.runs[0].profile_snapshot == request.profile_snapshot
    assert policy.planner_agent_ref != selected


@pytest.mark.parametrize("case", ("foreign", "disabled", "forged"))
def test_selected_planner_denial_precedes_new_request(case):
    handler, uow, envelope, _, _, resolutions, permissions = selected_fixture(
        foreign=case == "foreign", denied=case == "disabled"
    )
    if case == "forged":
        envelope = replace(envelope, actor_id="forged")
    with pytest.raises(AssignmentDenied):
        handler.preflight(uow, envelope)
    if case in {"foreign", "forged"}:
        assert resolutions == [] and permissions == []
    else:
        assert len(resolutions) == len(permissions) == 1


def test_revision_payload_accepts_exact_optional_planner_and_preserves_omission():
    _, _, _, policy, selected, _, _ = selected_fixture()
    body = {
        "root_task_id": uuid4(),
        "expected_base_snapshot_id": uuid4(),
        "expected_base_generation": 1,
        "expected_base_content_hash": "a" * 64,
        "objective": "Revise only new work.",
        "acceptance_criteria": ["Preserve the old head."],
        "planning_profile_id": policy.id,
        "planning_profile_version": policy.version,
    }
    assert SubmitPlanRevisionPayload.model_validate(body).planner_agent_ref is None
    assert (
        SubmitPlanRevisionPayload.model_validate(
            {**body, "planner_agent_ref": selected}
        ).planner_agent_ref
        == selected
    )


def test_native_revision_snapshot_uses_override_and_omission_retains_policy():
    handler, uow, envelope, policy, selected, _, _ = selected_fixture()
    revision = replace(envelope, command_type="SubmitPlanRevision")
    assert (
        handler._submission_snapshot(uow, policy, revision).planner_agent_ref
        == selected
    )
    omitted = replace(
        revision,
        payload={
            key: value
            for key, value in revision.payload.items()
            if key != "planner_agent_ref"
        },
    )
    assert handler._submission_snapshot(uow, policy, omitted) == policy.snapshot()


def test_legacy_policy_cannot_be_activated_by_planner_override():
    handler, uow, envelope, _, _, _, _ = selected_fixture()
    legacy = profile(uuid4())
    with pytest.raises(AssignmentDenied):
        handler._submission_snapshot(
            uow, legacy, replace(envelope, command_type="SubmitPlanRevision")
        )
