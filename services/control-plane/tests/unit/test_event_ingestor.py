from sidera_control_plane.application.event_ingestor import _redact_payload


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
