from __future__ import annotations

from pathlib import Path

import pytest

from sidera_control_plane.config import Settings
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.result_readiness import authorize_real_result_run
from tests.e2e.test_real_opencode_result_integration import (
    REAL_RESULT_EXECUTION_STEPS,
)


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database_url": "postgresql+psycopg://sidera@127.0.0.1:55432/sidera_test",
            "result_integration_enabled": True,
        }
    )


def _environment(repository: Path, **overrides: str) -> dict[str, str]:
    values = {
        "SIDERA_RUN_REAL_RESULT_INTEGRATION": "1",
        "SIDERA_REAL_RESULT_INTEGRATION_AUTHORIZED": "1",
        "SIDERA_REAL_RESULT_RUN_ID": "11111111-1111-4111-8111-111111111111",
        "SIDERA_REAL_RESULT_PROVIDER": "opencode-go",
        "SIDERA_REAL_RESULT_MODEL": "opencode-go/MiMo-V2.5",
        "SIDERA_REAL_RESULT_ENDPOINT_HOST": "api.opencode.example",
        "SIDERA_REAL_RESULT_CREDENTIAL_SOURCE": "opencode-credential-store",
        "SIDERA_REAL_RESULT_REPOSITORY": str(repository),
        "SIDERA_REAL_RESULT_FALLBACKS": "Ox-Alpha-Free",
        "SIDERA_REAL_RESULT_APPROVED_FALLBACKS": "Ox-Alpha-Free",
    }
    values.update(overrides)
    return values


def _local_gate(repository: Path) -> dict[str, object]:
    return {
        "status": "PASS",
        "commit": "a" * 40,
        "provider_free": True,
        "database": {
            "scheme": "postgresql+psycopg",
            "host": "127.0.0.1",
            "name": "sidera_test",
        },
        "repository": {"path": str(repository), "isolated": True},
        "approved": {
            "provider": "opencode-go",
            "model": "opencode-go/MiMo-V2.5",
            "endpoint_host": "api.opencode.example",
            "credential_source": "opencode-credential-store",
            "fallbacks": ["Ox-Alpha-Free"],
        },
    }


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "isolated-repository"
    (repository / ".git").mkdir(parents=True)
    return repository


@pytest.mark.parametrize(
    "missing_flag",
    (
        "SIDERA_RUN_REAL_RESULT_INTEGRATION",
        "SIDERA_REAL_RESULT_INTEGRATION_AUTHORIZED",
    ),
)
def test_real_gate_requires_both_exact_flags_before_session_creation(
    tmp_path: Path, missing_flag: str
) -> None:
    repository = _repository(tmp_path)
    environment = _environment(repository)
    del environment[missing_flag]
    started_sessions = 0

    with pytest.raises(ResultError, match="real_run_not_authorized"):
        authorize_real_result_run(_settings(), _local_gate(repository), environ=environment)

    assert started_sessions == 0


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    (
        ("SIDERA_REAL_RESULT_PROVIDER", "deepseek", "real_provider_not_approved"),
        ("SIDERA_REAL_RESULT_MODEL", "deepseek/deepseek-chat", "real_model_not_approved"),
        ("SIDERA_REAL_RESULT_ENDPOINT_HOST", "https://user:secret@example", "real_endpoint_invalid"),
        ("SIDERA_REAL_RESULT_CREDENTIAL_SOURCE", "sk-secret", "real_credential_source_invalid"),
        ("SIDERA_REAL_RESULT_FALLBACKS", "DeepSeek-Free", "real_fallback_not_approved"),
    ),
)
def test_real_gate_rejects_unapproved_runtime_configuration(
    tmp_path: Path, field: str, value: str, error_code: str
) -> None:
    repository = _repository(tmp_path)
    environment = _environment(repository, **{field: value})

    with pytest.raises(ResultError) as raised:
        authorize_real_result_run(_settings(), _local_gate(repository), environ=environment)

    assert raised.value.code == error_code
    assert "secret" not in str(raised.value).casefold()


def test_real_gate_requires_a_passing_provider_free_local_gate(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    gate = _local_gate(repository)
    gate["status"] = "FAIL"

    with pytest.raises(ResultError, match="real_local_gate_invalid"):
        authorize_real_result_run(
            _settings(), gate, environ=_environment(repository)
        )


def test_real_gate_binds_approved_identity_and_isolated_repository(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    other_repository = tmp_path / "other"
    (other_repository / ".git").mkdir(parents=True)
    gate = _local_gate(repository)
    gate["repository"] = {"path": str(other_repository), "isolated": True}

    with pytest.raises(ResultError, match="real_repository_not_approved"):
        authorize_real_result_run(
            _settings(), gate, environ=_environment(repository)
        )


def test_valid_real_gate_is_bounded_and_has_no_secret_summary(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    authorization = authorize_real_result_run(
        _settings(), _local_gate(repository), environ=_environment(repository)
    )

    assert authorization.provider == "opencode-go"
    assert str(authorization.run_id) == "11111111-1111-4111-8111-111111111111"
    assert authorization.model == "opencode-go/MiMo-V2.5"
    assert authorization.max_executions == 5
    assert authorization.wall_clock_seconds == 1800
    assert authorization.reserve_execution(0) == 1
    assert authorization.reserve_execution(4) == 5
    with pytest.raises(ResultError, match="real_run_budget_exhausted"):
        authorization.reserve_execution(5)
    assert "credential" not in repr(authorization.safe_summary()).casefold()
    assert "secret" not in repr(authorization.safe_summary()).casefold()


def test_real_gate_requires_a_valid_durable_run_id(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ResultError, match="real_run_id_invalid"):
        authorize_real_result_run(
            _settings(),
            _local_gate(repository),
            environ=_environment(repository, SIDERA_REAL_RESULT_RUN_ID="not-a-uuid"),
        )


def test_real_result_flow_budget_names_the_full_acceptance_path() -> None:
    assert REAL_RESULT_EXECUTION_STEPS == (
        "source-a",
        "source-b",
        "integration",
        "repair-source-a",
        "reintegration",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("SIDERA_REAL_RESULT_MAX_EXECUTIONS", "6"),
        ("SIDERA_REAL_RESULT_WALL_SECONDS", "1801"),
        ("SIDERA_REAL_RESULT_MAX_EXECUTIONS", "0"),
        ("SIDERA_REAL_RESULT_WALL_SECONDS", "0"),
    ),
)
def test_real_gate_rejects_budget_overrides_outside_hard_limits(
    tmp_path: Path, field: str, value: str
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ResultError, match="real_budget_invalid"):
        authorize_real_result_run(
            _settings(),
            _local_gate(repository),
            environ=_environment(repository, **{field: value}),
        )
