import os

import pytest
from pydantic import SecretStr

from sidera_control_plane.application.secret_resolution import (
    EnvironmentSecretResolver,
    SecretResolutionError,
)


def test_environment_secret_resolver_returns_ephemeral_secret(monkeypatch) -> None:
    monkeypatch.setenv("SIDERA_PLANNER_TEST_SECRET", "do-not-persist")

    resolved = EnvironmentSecretResolver().resolve("env:SIDERA_PLANNER_TEST_SECRET")

    assert isinstance(resolved, SecretStr)
    assert resolved.get_secret_value() == "do-not-persist"
    assert "do-not-persist" not in repr(resolved)
    assert os.environ["SIDERA_PLANNER_TEST_SECRET"] == "do-not-persist"


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        ("", "invalid_reference"),
        ("secret://planner/key", "unsupported_scheme"),
        ("env:", "invalid_reference"),
        ("env:planner-key", "invalid_reference"),
        ("env:SIDERA_MISSING_PLANNER_SECRET", "missing_secret"),
    ],
)
def test_environment_secret_resolver_reports_structured_failures(
    reference: str, code: str
) -> None:
    with pytest.raises(SecretResolutionError) as error:
        EnvironmentSecretResolver().resolve(reference)

    assert error.value.code == code
    assert "do-not-persist" not in str(error.value)


def test_environment_secret_resolver_never_accepts_raw_values() -> None:
    with pytest.raises(SecretResolutionError, match="unsupported_scheme"):
        EnvironmentSecretResolver().resolve("raw-secret-value")


def test_environment_secret_resolver_accepts_declared_local_credential_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIDERA_CREDENTIAL_STORE_READY", "1")

    resolved = EnvironmentSecretResolver().resolve("store:opencode-go")

    assert isinstance(resolved, SecretStr)
    assert resolved.get_secret_value() == "credential-store:opencode-go"
    assert "opencode-go" not in repr(resolved)


def test_environment_secret_resolver_rejects_unavailable_local_credential_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIDERA_CREDENTIAL_STORE_READY", raising=False)

    with pytest.raises(SecretResolutionError) as error:
        EnvironmentSecretResolver().resolve("store:opencode-go")

    assert error.value.code == "credential_store_unavailable"
    assert "opencode-go" not in str(error.value)
