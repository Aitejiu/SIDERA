from datetime import UTC, datetime

from sidera_control_plane.application.event_ingestor import (
    _redact_payload,
    _terminal_observation,
)
from sidera_control_plane.domain.gateway import AdapterEvent


def test_event_payload_redacts_secrets_and_bounds_large_values() -> None:
    payload = _redact_payload(
        {
            "message": "x" * 20_000,
            "api_key": "do-not-store",
            "nested": {"authorization": "Bearer private"},
        }
    )

    assert payload["api_key"] == "[redacted]"
    assert payload["nested"] == {"authorization": "[redacted]"}
    message = payload["message"]
    assert isinstance(message, dict)
    assert message["truncated"] is True
    assert message["size_bytes"] == 20_000


def test_terminal_observation_only_accepts_control_plane_terminal_events() -> None:
    completed = _terminal_observation(
        AdapterEvent(
            event_type="execution.succeeded",
            sequence=4,
            payload={"api_key": "private", "summary": "ready"},
            occurred_at=datetime.now(UTC),
        )
    )
    progress = _terminal_observation(
        AdapterEvent(
            event_type="progress.updated",
            sequence=5,
            payload={"progress": 1},
            occurred_at=datetime.now(UTC),
        )
    )

    assert completed is not None
    assert completed.outcome == "succeeded"
    assert completed.sequence == 4
    assert completed.payload["api_key"] == "[redacted]"
    assert progress is None
