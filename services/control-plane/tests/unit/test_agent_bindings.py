from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import Response

from sidera_control_plane.api.schemas import CommandRequest
from sidera_control_plane.api.task_commands import _execute_command
from sidera_control_plane.application.command_service import (
    CommandService,
    hash_request,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import DomainError, ValidationError
from sidera_control_plane.persistence.planning_repository import (
    _snapshot_from_mapping,
    _snapshot_to_mapping,
)
from tests.unit.test_command_service import FakeCommandRepository, FakeUnitOfWork
from tests.unit.test_planning_profiles import planner_capabilities, planning_profile


def envelope():
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=uuid4(),
        command_type="SetSubtaskAssignee",
        target_type="task",
        target_id=uuid4(),
        actor_type="user",
        actor_id=str(uuid4()),
        expected_version=1,
        idempotency_key=str(uuid4()),
        correlation_id=None,
        payload={
            "assignee_kind": "human",
            "assignee_id": str(uuid4()),
            "profile_revision": None,
        },
    )


class NoDispatch:
    command_types = frozenset({"SetSubtaskAssignee"})

    def handle(self, *_):
        pytest.fail("denied request dispatched")


@pytest.mark.parametrize("path", ["first", "replay", "concurrent", "conflict"])
def test_preflight_denies_before_receipt_reads_or_dispatch(path):
    request = envelope()
    commands = FakeCommandRepository()
    if path != "first":
        commands.record_applied(
            request,
            hash_request(request),
            CommandResult(
                command_id=request.id,
                aggregate_id=request.target_id,
                aggregate_version=2,
                event_ids=(),
                data={"revision": 1},
            ),
        )
    if path == "conflict":
        request = replace(request, payload={"forged": True})

    def forbidden(*_):
        raise ValidationError("scope revoked")

    def unreadable(*_):
        pytest.fail("forbidden receipt read")

    commands.find_by_id = unreadable
    service = CommandService(
        lambda: FakeUnitOfWork(commands), (NoDispatch(),), preflight=forbidden
    )
    with pytest.raises(ValidationError, match="scope revoked"):
        if path == "concurrent":
            service._recover_concurrent(request, hash_request(request))
        else:
            service.handle(request)


def test_assignment_http_matching_configured_actor_is_not_authentication():
    settings = Settings(database_url="postgresql+psycopg://unit.invalid/unused")
    request = CommandRequest(
        command_id=uuid4(),
        workspace_id=uuid4(),
        command_type="SetSubtaskAssignee",
        target_type="task",
        target_id=uuid4(),
        expected_version=1,
        actor={"type": settings.local_actor_type, "id": settings.local_actor_id},
        idempotency_key="assignment",
        correlation_id=uuid4(),
        payload={
            "assignee_kind": "human",
            "assignee_id": settings.local_actor_id,
            "profile_revision": None,
        },
    )
    with pytest.raises(DomainError, match="trusted"):
        _execute_command(request, Response(), NoDispatch(), settings)


def test_native_policy_snapshot_round_trip_without_a_legacy_module():
    from sidera_agents.contracts import ProfileRef

    workspace = uuid4()
    pin = ProfileRef(workspace_id=workspace, profile_id=uuid4(), revision=2)
    policy = planning_profile(
        workspace_id=workspace,
        execution_mode="native_profile",
        planner_agent_ref=pin,
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_secret_refs=(),
    )
    stored = _snapshot_to_mapping(policy.snapshot())
    assert stored["execution_mode"] == "native_profile"
    assert stored["planner_module_instance_id"] is None
    assert stored["planner_capabilities"] is None
    assert stored["planner_agent_ref"] == pin.model_dump(mode="json")
    restored = _snapshot_from_mapping(stored)
    assert restored.planner_agent_ref == pin
    assert restored.planner_module_instance_id is None


def test_old_snapshot_without_discriminator_retains_strict_legacy_contract():
    stored = _snapshot_to_mapping(planning_profile().snapshot(planner_capabilities()))
    stored.pop("execution_mode", None)
    stored.pop("planner_agent_ref", None)
    assert _snapshot_from_mapping(stored).execution_mode == "legacy"
    stored["planner_module_instance_id"] = None
    with pytest.raises(ValidationError):
        _snapshot_from_mapping(stored)


@pytest.mark.parametrize(
    "overrides",
    [
        {"planner_module_instance_id": None},
        {"planner_module_config_version": None},
        {"planner_module_config_version": 0},
        {"execution_mode": "unknown"},
        {"execution_mode": "native_profile"},
    ],
)
def test_invalid_or_dual_planning_backend_is_rejected(overrides):
    with pytest.raises(ValidationError):
        planning_profile(**overrides)


@pytest.mark.parametrize(
    "change",
    [
        {"assignee": {"kind": "human", "id": "user"}},
        {"workspace_id": str(uuid4())},
        {"grant_id": str(uuid4())},
        {"profile_revision": True},
        {"profile_revision": 0},
        {"profile_revision": -1},
        {"profile_revision": 1},
    ],
)
def test_noncanonical_assignment_rejected_before_target_or_agent_reads(change):
    from sidera_control_plane.application.assignments import (
        AssignmentCommandHandler,
        TrustedAssignmentIdentity,
    )
    from tests.support.native_agents import AssignmentScope

    request = envelope()
    identity = TrustedAssignmentIdentity(request.actor_id, uuid4())
    handler = AssignmentCommandHandler(
        identity=identity,
        authorizer=AssignmentScope(
            request.workspace_id, identity.principal_id, identity.grant_id
        ),
    )
    payload = {
        "assignee_kind": "human",
        "assignee_id": identity.principal_id,
        "profile_revision": None,
    }
    if "assignee" in change:
        payload = {"assignee": {"kind": "human", "id": identity.principal_id}}
    else:
        payload.update(change)
    with pytest.raises(DomainError):
        handler.handle(object(), replace(request, payload=payload))


def test_public_catalog_keyword_only_grant_and_missing_profile_denial():
    from sidera_control_plane.application.assignments import (
        AssignmentCommandHandler,
        TrustedAssignmentIdentity,
    )
    from sidera_control_plane.domain.agent_bindings import AssignmentDenied
    from tests.support.native_agents import PublicAgentCatalog, PublicAgentRuntimes

    handler = AssignmentCommandHandler(
        catalog=PublicAgentCatalog(()),
        runtimes=PublicAgentRuntimes(None),
        identity=TrustedAssignmentIdentity("local", uuid4()),
    )
    with pytest.raises(AssignmentDenied, match="unavailable"):
        handler.resolve_profile(uuid4(), uuid4(), None)
