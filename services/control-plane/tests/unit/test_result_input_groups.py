from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.result_inputs import InputGroupV1

from .result_input_fixtures import group_mapping


def test_group_round_trip_and_bool_threshold_rejected() -> None:
    raw = group_mapping()

    value = InputGroupV1.from_mapping(raw)

    assert value.to_mapping() == raw
    assert value.candidates[1].priority == 2
    assert value.requirements.required_artifact_refs == ("artifact_type:report",)

    raw["minimum_count"] = True
    with pytest.raises(ValidationError):
        InputGroupV1.from_mapping(raw)


def test_group_accepts_evidence_only_but_rejects_empty_requirements() -> None:
    raw = group_mapping()
    requirements = cast(dict[str, object], raw["requirements"])
    requirements["required_artifact_refs"] = []

    assert InputGroupV1.from_mapping(raw).to_mapping() == raw

    requirements["required_evidence_refs"] = []
    with pytest.raises(ValidationError):
        InputGroupV1.from_mapping(raw)


@pytest.mark.parametrize("minimum_count", [0, 4, 1.5])
def test_at_least_group_rejects_invalid_threshold(minimum_count: object) -> None:
    raw = group_mapping()
    raw["minimum_count"] = minimum_count

    with pytest.raises(ValidationError):
        InputGroupV1.from_mapping(raw)


def test_all_group_requires_null_threshold() -> None:
    raw = group_mapping()
    raw["mode"] = "all"
    raw["minimum_count"] = None

    value = InputGroupV1.from_mapping(raw)

    assert value.mode.value == "all"
    assert value.minimum_count is None

    raw["minimum_count"] = 3
    with pytest.raises(ValidationError):
        InputGroupV1.from_mapping(raw)


def test_group_normalizes_candidate_array_by_explicit_priority() -> None:
    raw = group_mapping()
    raw["candidates"] = [
        {"worker_item_key": "c", "priority": 3},
        {"worker_item_key": "a", "priority": 1},
        {"worker_item_key": "b", "priority": 2},
    ]

    value = InputGroupV1.from_mapping(raw)

    assert tuple(item.worker_item_key for item in value.candidates) == ("a", "b", "c")
    candidates = cast(list[dict[str, object]], value.to_mapping()["candidates"])
    assert [item["priority"] for item in candidates] == [1, 2, 3]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidates", []),
        (
            "candidates",
            [
                {"worker_item_key": "a", "priority": 1},
                {"worker_item_key": "a", "priority": 2},
                {"worker_item_key": "c", "priority": 3},
            ],
        ),
        (
            "candidates",
            [
                {"worker_item_key": "a", "priority": 1},
                {"worker_item_key": "b", "priority": 2},
                {"worker_item_key": "c", "priority": 2},
            ],
        ),
        (
            "candidates",
            [
                {"worker_item_key": "a", "priority": 1},
                {"worker_item_key": "b", "priority": 4},
                {"worker_item_key": "c", "priority": 3},
            ],
        ),
        (
            "candidates",
            [
                {"worker_item_key": "summary", "priority": 1},
                {"worker_item_key": "b", "priority": 2},
                {"worker_item_key": "c", "priority": 3},
            ],
        ),
    ],
)
def test_group_rejects_invalid_candidate_collection(
    field: str, value: object
) -> None:
    raw = group_mapping()
    raw[field] = value

    with pytest.raises(ValidationError):
        InputGroupV1.from_mapping(raw)


@pytest.mark.parametrize(
    "raw_requirement",
    [
        {"execution_status": "running"},
        {"execution_status": "succeeded", "required_artifact_refs": []},
        {
            "execution_status": "succeeded",
            "required_artifact_refs": ["artifact_type:unknown"],
        },
        {
            "execution_status": "succeeded",
            "required_artifact_refs": ["artifact_type:report", "artifact_type:report"],
        },
        {
            "execution_status": "succeeded",
            "required_artifact_refs": ["artifact_type:report"],
            "required_evidence_refs": ["acceptance:tests", "acceptance:tests"],
            "evidence_status": "accepted",
        },
        {
            "execution_status": "succeeded",
            "required_artifact_refs": ["artifact_type:report"],
            "required_evidence_refs": ["not-an-acceptance-ref"],
            "evidence_status": "candidate",
        },
    ],
)
def test_group_rejects_invalid_requirements(raw_requirement: dict[str, object]) -> None:
    raw = group_mapping()
    raw["requirements"] = raw_requirement

    with pytest.raises(ValidationError):
        InputGroupV1.from_mapping(raw)


def test_group_rejects_unknown_fields_at_every_contract_level() -> None:
    raw = group_mapping()
    raw["unexpected"] = "not allowed"
    with pytest.raises(ValidationError, match="unknown input group fields"):
        InputGroupV1.from_mapping(raw)

    raw = group_mapping()
    requirements = cast(dict[str, object], raw["requirements"])
    raw["requirements"] = {
        **requirements,
        "unexpected": "not allowed",
    }
    with pytest.raises(ValidationError, match="unknown input requirements fields"):
        InputGroupV1.from_mapping(raw)

    raw = group_mapping()
    raw["candidates"] = [
        {"worker_item_key": "a", "priority": 1, "unexpected": True},
        {"worker_item_key": "b", "priority": 2},
        {"worker_item_key": "c", "priority": 3},
    ]
    with pytest.raises(ValidationError, match="unknown input candidate fields"):
        InputGroupV1.from_mapping(raw)


def test_group_rejects_non_empty_selector_budget_overflow() -> None:
    raw = group_mapping()
    requirements = cast(dict[str, object], raw["requirements"])
    raw["requirements"] = {
        **requirements,
        "required_evidence_refs": [
            f"acceptance:research-{index}" for index in range(9)
        ],
    }
    with pytest.raises(ValidationError):
        InputGroupV1.from_mapping(raw)

    raw = group_mapping()
    raw["candidates"] = [
        {"worker_item_key": f"worker-{index}", "priority": index + 1}
        for index in range(33)
    ]
    raw["minimum_count"] = 2
    with pytest.raises(ValidationError):
        InputGroupV1.from_mapping(raw)


def test_group_mapping_is_not_mutated_and_value_is_immutable() -> None:
    raw = group_mapping()
    original = deepcopy(raw)

    value = InputGroupV1.from_mapping(raw)

    assert raw == original
    with pytest.raises(AttributeError):
        value.group_key = "changed"  # type: ignore[misc]
