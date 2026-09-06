import json

import pytest
from pydantic import ValidationError

from sidera_task_bridge.contracts import GovernanceProposalView, ToolEnvelope


@pytest.mark.parametrize(
    "outcome",
    ["ok", "needs_user", "conflict", "unavailable", "result_unknown", "failed"],
)
def test_tool_envelope_accepts_each_stable_outcome(outcome: str) -> None:
    envelope = ToolEnvelope.model_validate(
        {
            "outcome": outcome,
            "summary": "The operation has a stable result.",
            "resource_ref": "planning:00000000-0000-0000-0000-000000000001",
            "data": {"count": 2, "items": ["backend", {"ready": True}]},
        }
    )

    assert envelope.outcome == outcome
    assert json.loads(envelope.model_dump_json())[
        "summary"
    ] == "The operation has a stable result."


def test_tool_envelope_rejects_empty_summary_invalid_outcome_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ToolEnvelope(outcome="ok", summary="   ")
    with pytest.raises(ValidationError):
        ToolEnvelope.model_validate({"outcome": "unknown", "summary": "not allowed"})
    with pytest.raises(ValidationError):
        ToolEnvelope.model_validate(
            {"outcome": "ok", "summary": "not allowed", "extra": "forbidden"}
        )


def test_tool_envelope_rejects_non_json_data() -> None:
    with pytest.raises(ValidationError):
        ToolEnvelope.model_validate(
            {"outcome": "failed", "summary": "failed", "data": {"error": object()}}
        )


def test_tool_envelope_json_does_not_include_secret_or_exception_repr() -> None:
    envelope = ToolEnvelope(
        outcome="failed",
        summary="The request failed.",
        error_code="invalid_control_plane_response",
        data={"safe": "value"},
    )

    encoded = envelope.model_dump_json()
    assert "secret" not in encoded.lower()
    assert "RuntimeError" not in encoded
    assert "Traceback" not in encoded


def test_governance_proposal_preserves_the_fixed_input_selection_identity() -> None:
    proposal = GovernanceProposalView.model_validate(
        {
            "workspace_id": "00000000-0000-0000-0000-000000000001",
            "root_task_id": "00000000-0000-0000-0000-000000000020",
            "subtask_id": None,
            "subtask_key": "summary",
            "subtask_title": "Summary",
            "worker_item_id": "00000000-0000-0000-0000-000000000060",
            "worker_item_key": "summary",
            "worker_item_title": "Summarize",
            "proposal_id": "00000000-0000-0000-0000-000000000061",
            "status": "pending",
            "resource_version": 3,
            "target_request_version": 8,
            "reason_code": "waiting_release",
            "summary": "The selected results are ready.",
            "hold_id": "00000000-0000-0000-0000-000000000062",
            "hold_resource_version": 4,
            "governance_mode": "paused",
            "available_actions": ["approve"],
            "deep_link": "/workspaces/00000000-0000-0000-0000-000000000001/worker-items/00000000-0000-0000-0000-000000000060",
            "input_selection_id": "61000000-0000-0000-0000-000000000001",
            "input_selection_hash": "a" * 64,
        }
    )

    assert str(proposal.input_selection_id) == (
        "61000000-0000-0000-0000-000000000001"
    )
    assert proposal.input_selection_hash == "a" * 64


def test_governance_proposal_rejects_a_partial_input_selection_identity() -> None:
    with pytest.raises(ValidationError):
        GovernanceProposalView.model_validate(
            {
                "workspace_id": "00000000-0000-0000-0000-000000000001",
                "root_task_id": "00000000-0000-0000-0000-000000000020",
                "subtask_id": None,
                "subtask_key": "summary",
                "subtask_title": "Summary",
                "worker_item_id": "00000000-0000-0000-0000-000000000060",
                "worker_item_key": "summary",
                "worker_item_title": "Summarize",
                "proposal_id": "00000000-0000-0000-0000-000000000061",
                "status": "pending",
                "resource_version": 3,
                "target_request_version": 8,
                "reason_code": "waiting_release",
                "summary": "The selected results are ready.",
                "hold_id": "00000000-0000-0000-0000-000000000062",
                "hold_resource_version": 4,
                "governance_mode": "paused",
                "available_actions": ["approve"],
                "deep_link": "/worker-items/00000000-0000-0000-0000-000000000060",
                "input_selection_id": "61000000-0000-0000-0000-000000000001",
            }
        )
