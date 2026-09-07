from __future__ import annotations

from copy import deepcopy

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.result_integration import (
    IntegrationSpecV1,
    validate_integration_spec,
)

from .result_integration_fixtures import integration_mapping


def test_default_policy_and_unknown_fields() -> None:
    value = integration_mapping()

    spec = IntegrationSpecV1.from_mapping(value)

    assert spec.rework_policy.max_auto_rework_rounds == 2
    assert spec.rework_policy.max_auto_rework_seconds == 1800
    assert spec.apply_policy.apply_on_accept is False

    value["silent_merge"] = True
    with pytest.raises(ValidationError, match="unknown integration spec fields"):
        IntegrationSpecV1.from_mapping(value)


def test_spec_round_trip_is_stable_and_validation_checks_plan_membership() -> None:
    spec = IntegrationSpecV1.from_mapping(integration_mapping())

    assert spec.to_mapping() == integration_mapping()
    validate_integration_spec(
        spec,
        worker_keys=frozenset({"login", "integrate"}),
        input_group_keys=frozenset({"implementation"}),
    )

    with pytest.raises(ValidationError, match="integration WorkerItem"):
        validate_integration_spec(
            spec,
            worker_keys=frozenset({"login"}),
            input_group_keys=frozenset({"implementation"}),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("rework_policy", {"max_auto_rework_rounds": True}),
        ("apply_policy", {"apply_on_accept": True, "mode": "fast_forward_only"}),
    ],
)
def test_contract_rejects_bool_as_integer_or_incomplete_nested_value(
    field: str, value: object
) -> None:
    raw = integration_mapping()
    if field == "rework_policy":
        raw[field] = {
            **raw[field],  # type: ignore[dict-item]
            **value,  # type: ignore[misc]
        }
    elif field == "apply_policy":
        raw[field] = value
    else:
        raw[field] = value

    with pytest.raises(ValidationError):
        IntegrationSpecV1.from_mapping(raw)


def test_automatic_criteria_require_a_known_check_and_apply_requires_target() -> None:
    raw = integration_mapping()
    criteria = deepcopy(raw["criteria"])
    criteria[0]["check_keys"] = ["missing"]  # type: ignore[index]
    raw["criteria"] = criteria
    with pytest.raises(ValidationError, match="unknown check"):
        IntegrationSpecV1.from_mapping(raw)

    raw = integration_mapping()
    raw["apply_policy"] = {
        "apply_on_accept": True,
        "mode": "fast_forward_only",
        "target_ref": None,
    }
    with pytest.raises(ValidationError, match="target ref"):
        IntegrationSpecV1.from_mapping(raw)


def test_check_contract_rejects_absolute_cwd_and_shell_metacharacters() -> None:
    raw = integration_mapping()
    checks = deepcopy(raw["checks"])
    checks[0]["cwd"] = "/tmp/project"  # type: ignore[index]
    raw["checks"] = checks
    with pytest.raises(ValidationError, match="relative"):
        IntegrationSpecV1.from_mapping(raw)

    raw = integration_mapping()
    checks = deepcopy(raw["checks"])
    checks[0]["argv"] = ["python3", "-c", "echo ok; rm -rf /"]  # type: ignore[index]
    raw["checks"] = checks
    with pytest.raises(ValidationError, match="shell"):
        IntegrationSpecV1.from_mapping(raw)
