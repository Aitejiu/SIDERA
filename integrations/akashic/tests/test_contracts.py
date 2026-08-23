import json

import pytest
from pydantic import ValidationError

from sidera_task_bridge.contracts import ToolEnvelope


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
