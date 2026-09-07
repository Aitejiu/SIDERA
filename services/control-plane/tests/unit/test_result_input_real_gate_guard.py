from __future__ import annotations

from collections.abc import Mapping

import pytest

from sidera_control_plane.domain.modules import (
    CapabilityInput,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
)
from tests.e2e.test_r6_3_4_real_opencode_inputs import (
    RealResultInputGateError,
    load_real_result_gate_config,
    real_result_gate_requested,
    safe_real_result_gate_summary,
    validate_real_result_gate_profile,
)


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "SIDERA_RUN_REAL_RESULT_INPUTS": "1",
        "SIDERA_RESULT_INPUTS_REAL_APPROVED": "1",
        "SIDERA_OPENCODE_EXECUTABLE": "/usr/local/bin/opencode",
        "SIDERA_OPENCODE_MODEL": "opencode-go/MiMo-V2.5",
        "SIDERA_REAL_RESULT_INPUT_PROVIDER": "opencode-go",
        "SIDERA_REAL_RESULT_INPUT_ENDPOINT_HOST": "api.opencode.example",
        "SIDERA_REAL_RESULT_INPUT_ADAPTER_CONFIG_VERSION": "1",
        "SIDERA_REAL_RESULT_INPUT_FALLBACKS": "Ox-Alpha-Free",
        "SIDERA_REAL_RESULT_INPUT_APPROVED_FALLBACKS": "Ox-Alpha-Free",
        "SIDERA_REAL_RESULT_INPUT_BUDGET_MICROUNITS": "100000",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("run_flag", "approved_flag"),
    (
        ("0", "0"),
        ("1", "0"),
        ("0", "1"),
        ("yes", "1"),
        ("1", "yes"),
    ),
)
def test_real_gate_requires_both_exact_flags(
    run_flag: str, approved_flag: str
) -> None:
    environment = _environment(
        SIDERA_RUN_REAL_RESULT_INPUTS=run_flag,
        SIDERA_RESULT_INPUTS_REAL_APPROVED=approved_flag,
    )

    assert real_result_gate_requested(environment) is False


def test_real_gate_is_enabled_only_by_both_exact_flags() -> None:
    assert real_result_gate_requested(_environment()) is True


@pytest.mark.parametrize(
    "missing",
    (
        "SIDERA_OPENCODE_EXECUTABLE",
        "SIDERA_OPENCODE_MODEL",
        "SIDERA_REAL_RESULT_INPUT_PROVIDER",
        "SIDERA_REAL_RESULT_INPUT_ENDPOINT_HOST",
        "SIDERA_REAL_RESULT_INPUT_ADAPTER_CONFIG_VERSION",
    ),
)
def test_real_gate_rejects_missing_required_configuration(missing: str) -> None:
    environment = _environment()
    del environment[missing]

    with pytest.raises(RealResultInputGateError, match="required"):
        load_real_result_gate_config(environment)


@pytest.mark.parametrize(
    "field",
    (
        "SIDERA_REAL_RESULT_INPUT_PROVIDER",
        "SIDERA_OPENCODE_MODEL",
    ),
)
def test_real_gate_rejects_direct_deepseek(field: str) -> None:
    environment = _environment(**{field: "deepseek/deepseek-chat"})

    with pytest.raises(RealResultInputGateError, match="DeepSeek"):
        load_real_result_gate_config(environment)


def test_real_gate_rejects_unapproved_or_deepseek_fallback() -> None:
    with pytest.raises(RealResultInputGateError, match="approved"):
        load_real_result_gate_config(
            _environment(SIDERA_REAL_RESULT_INPUT_FALLBACKS="another-free-model")
        )

    with pytest.raises(RealResultInputGateError, match="DeepSeek"):
        load_real_result_gate_config(
            _environment(
                SIDERA_REAL_RESULT_INPUT_FALLBACKS="deepseek/deepseek-chat",
                SIDERA_REAL_RESULT_INPUT_APPROVED_FALLBACKS="deepseek/deepseek-chat",
            )
        )

    with pytest.raises(RealResultInputGateError, match="DeepSeek"):
        load_real_result_gate_config(
            _environment(
                SIDERA_REAL_RESULT_INPUT_FALLBACKS="",
                SIDERA_REAL_RESULT_INPUT_APPROVED_FALLBACKS="deepseek/deepseek-chat"
            )
        )


def test_real_gate_rejects_unsafe_endpoint_and_config_version() -> None:
    with pytest.raises(RealResultInputGateError, match="endpoint host"):
        load_real_result_gate_config(
            _environment(SIDERA_REAL_RESULT_INPUT_ENDPOINT_HOST="https://user:secret@example")
        )

    with pytest.raises(RealResultInputGateError, match="config version"):
        load_real_result_gate_config(
            _environment(SIDERA_REAL_RESULT_INPUT_ADAPTER_CONFIG_VERSION="2")
        )


def test_real_gate_rejects_context_v1_only_profile() -> None:
    profile = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.EXPERIMENTAL,
        context_package_versions=(1,),
    )

    with pytest.raises(RealResultInputGateError, match="context package v2"):
        validate_real_result_gate_profile(profile)


def test_valid_summary_is_bounded_and_contains_no_secret() -> None:
    environment = _environment(SIDERA_REAL_RESULT_INPUT_ENDPOINT_HOST="api.example")
    environment["SIDERA_REAL_RESULT_INPUT_TOKEN"] = "do-not-print-this"
    config = load_real_result_gate_config(environment)
    summary = safe_real_result_gate_summary(config)

    assert summary == {
        "provider": "opencode-go",
        "model": "opencode-go/MiMo-V2.5",
        "endpoint_host": "api.example",
        "adapter_config_version": 1,
        "max_attempts": 1,
        "concurrency": 1,
        "max_seconds": 600,
        "max_report_bytes": 4096,
        "budget_microunits": 100000,
    }
    assert "do-not-print-this" not in repr(summary)


def test_gate_helpers_do_not_mutate_the_supplied_environment() -> None:
    environment: Mapping[str, str] = _environment()
    before = dict(environment)

    load_real_result_gate_config(environment)

    assert dict(environment) == before
