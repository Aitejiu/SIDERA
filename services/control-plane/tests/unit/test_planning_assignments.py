from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import ValidationError


def choice(**changes):
    return {
        "target_type": "worker_item",
        "target_key": "implementation",
        "assignee_kind": "profile",
        "assignee_id": str(uuid4()),
        "profile_revision": 2,
        "expected_binding_version": None,
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"profile_revision": None},
        {"profile_revision": True},
        {"profile_revision": "2"},
        {"expected_binding_version": True},
        {"expected_binding_version": 0},
        {"target_key": " "},
        {"target_type": "root"},
        {"extra": "rejected"},
        {"assignee_id": "unknown-profile"},
        {"assignee_kind": "human", "profile_revision": 2},
    ],
)
def test_confirmation_choices_reject_invalid_explicit_shape(changes):
    from sidera_control_plane.application.planning_assignments import (
        parse_assignee_bindings,
    )

    with pytest.raises(ValidationError):
        parse_assignee_bindings([choice(**changes)])


def test_confirmation_choices_reject_duplicate_actual_proposal_key():
    from sidera_control_plane.application.planning_assignments import (
        parse_assignee_bindings,
    )

    first = choice()
    with pytest.raises(ValidationError):
        parse_assignee_bindings([first, {**first, "assignee_id": str(uuid4())}])


def test_api_omitted_legacy_confirmation_retains_exact_payload_shape():
    from sidera_control_plane.api.schemas import CommandRequest

    payload = {
        "proposal_revision": 1,
        "proposal_content_hash": "a" * 64,
        "profile_id": str(uuid4()),
        "profile_version": 1,
    }
    request = CommandRequest.model_validate(
        {
            "command_id": str(uuid4()),
            "workspace_id": str(uuid4()),
            "actor": {"type": "user", "id": "human"},
            "command_type": "ConfirmPlanningProposal",
            "target_type": "planning_request",
            "target_id": str(uuid4()),
            "expected_version": 3,
            "idempotency_key": str(uuid4()),
            "correlation_id": str(uuid4()),
            "payload": payload,
        }
    )
    assert request.payload == payload


def test_api_confirmation_accepts_explicit_exact_profile_choice():
    from sidera_control_plane.api.schemas import ConfirmPlanningProposalPayload

    payload = {
        "proposal_revision": 1,
        "proposal_content_hash": "a" * 64,
        "profile_id": str(uuid4()),
        "profile_version": 1,
        "assignee_bindings": [choice()],
    }
    parsed = ConfirmPlanningProposalPayload.model_validate(payload)
    assert parsed.model_dump(mode="json") == payload
