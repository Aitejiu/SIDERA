from pydantic import SecretStr

from sidera_agents.redaction import redact_secrets


def test_diagnostics_mask_full_overlapping_secret_values() -> None:
    short = SecretStr("fixture-token")
    long = SecretStr("fixture-token-with-suffix")
    diagnostic = "failed with fixture-token-with-suffix and fixture-token"

    redacted = redact_secrets(diagnostic, (short, long, SecretStr("")))

    assert redacted == "failed with [REDACTED] and [REDACTED]"
    assert "fixture-token" not in redacted
