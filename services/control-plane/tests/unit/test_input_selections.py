from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionHardInputV1,
)
from sidera_control_plane.domain.result_inputs import ResultInputArtifact

from .result_input_fixtures import selection_mapping


def test_selection_round_trip_and_hash_excludes_record_identity_not_result_identity() -> (
    None
):
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    changed = replace(
        first, id=uuid4(), created_at=first.created_at + timedelta(seconds=1)
    )

    assert CollaborationInputSelectionV1.from_mapping(first.to_mapping()) == first
    assert first.compute_selection_hash() == changed.compute_selection_hash()
    assert first.selection_hash == first.compute_selection_hash()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_request_version", 4),
        ("plan_generation", 5),
        ("plan_content_hash", "f" * 64),
    ],
)
def test_selection_hash_changes_for_scope_or_target_changes(
    field: str, value: object
) -> None:
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    changed = replace(first, **{field: value})

    assert changed.compute_selection_hash() != first.compute_selection_hash()


@pytest.mark.parametrize(
    "field",
    ["projection_snapshot_id", "projection_hash", "decision_id", "intent_id"],
)
def test_selection_hash_excludes_audit_identity(field: str) -> None:
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    value: object = "f" * 64 if field == "projection_hash" else uuid4()

    assert replace(first, **{field: value}).compute_selection_hash() == (
        first.compute_selection_hash()
    )


@pytest.mark.parametrize("field", ["attempt", "priority", "source_basis_hash"])
def test_selection_hash_changes_for_source_changes(field: str) -> None:
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    source = first.groups[0].sources[0]
    value: object = {
        "attempt": 2,
        "priority": 3,
        "source_basis_hash": "f" * 64,
    }[field]
    changed_source = replace(source, **{field: value})
    changed_group = replace(
        first.groups[0], sources=(changed_source, first.groups[0].sources[1])
    )
    changed = replace(first, groups=(changed_group,))

    assert changed.compute_selection_hash() != first.compute_selection_hash()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effective_request_id", uuid4()),
        ("effective_lineage_id", uuid4()),
        ("effective_execution_id", uuid4()),
        ("inheritance_refs", ("handoff:61000000-0000-0000-0000-000000000099",)),
    ],
)
def test_selection_hash_changes_for_source_authorization(
    field: str, value: object
) -> None:
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    source = first.groups[0].sources[0]
    updates = {field: value}
    if field == "effective_lineage_id":
        updates["allowed_lineage_ids"] = (value,)
    changed_source = replace(source, **updates)
    changed = replace(
        first,
        groups=(
            replace(
                first.groups[0],
                sources=(changed_source, first.groups[0].sources[1]),
            ),
        ),
    )

    assert changed.compute_selection_hash() != first.compute_selection_hash()


def test_artifact_and_evidence_identity_changes_hash_and_relations_are_retained() -> (
    None
):
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    source = first.groups[0].sources[0]
    artifact = replace(source.artifacts[0], size_bytes=10)
    changed_source = replace(source, artifacts=(artifact,))
    changed = replace(
        first,
        groups=(
            replace(
                first.groups[0], sources=(changed_source, first.groups[0].sources[1])
            ),
        ),
    )

    assert changed.compute_selection_hash() != first.compute_selection_hash()
    assert changed.to_mapping()["groups"]
    assert changed.groups[0].sources[0].evidence[0].artifact_ids == (
        artifact.artifact_id,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content_hash", "f" * 64),
        ("size_bytes", 10),
        ("media_type", "application/json"),
    ],
)
def test_selection_hash_changes_for_artifact_descriptor(
    field: str, value: object
) -> None:
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    source = first.groups[0].sources[0]
    changed_source = replace(
        source,
        artifacts=(replace(source.artifacts[0], **{field: value}),),
    )
    changed = replace(
        first,
        groups=(
            replace(
                first.groups[0],
                sources=(changed_source, first.groups[0].sources[1]),
            ),
        ),
    )

    assert changed.compute_selection_hash() != first.compute_selection_hash()


def test_selection_hash_changes_for_evidence_artifact_closure() -> None:
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    source = first.groups[0].sources[0]
    extra_artifact = ResultInputArtifact(
        artifact_id=uuid4(),
        artifact_type=source.artifacts[0].artifact_type,
        content_hash="f" * 64,
        size_bytes=1,
        media_type="text/plain",
    )
    baseline_source = replace(
        source,
        artifacts=(source.artifacts[0], extra_artifact),
    )
    changed_source = replace(
        baseline_source,
        evidence=(
            replace(
                baseline_source.evidence[0],
                artifact_ids=(extra_artifact.artifact_id,),
            ),
        ),
    )
    baseline = replace(
        first,
        groups=(
            replace(
                first.groups[0],
                sources=(baseline_source, first.groups[0].sources[1]),
            ),
        ),
    )
    changed = replace(
        first,
        groups=(
            replace(
                first.groups[0],
                sources=(changed_source, first.groups[0].sources[1]),
            ),
        ),
    )

    assert changed.compute_selection_hash() != baseline.compute_selection_hash()


def test_rule_target_and_group_membership_remain_hash_bound() -> None:
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    second_group = replace(
        first.groups[0],
        group_key="verification",
        rule_hash="f" * 64,
        sources=(first.groups[0].sources[0],),
    )
    grouped = replace(first, groups=(first.groups[0], second_group))

    assert (
        replace(first, target_worker_item_id=uuid4()).compute_selection_hash()
        != first.compute_selection_hash()
    )
    assert (
        replace(first, target_execution_request_id=uuid4()).compute_selection_hash()
        != first.compute_selection_hash()
    )
    assert (
        replace(
            first,
            groups=(replace(first.groups[0], rule_hash="f" * 64),),
        ).compute_selection_hash()
        != first.compute_selection_hash()
    )
    assert grouped.compute_selection_hash() != first.compute_selection_hash()
    assert grouped.groups[0].sources[0].artifacts[0].artifact_id == (
        grouped.groups[1].sources[0].artifacts[0].artifact_id
    )


def test_selection_is_frozen_and_rejects_unknown_or_mismatched_wire_values() -> None:
    value = CollaborationInputSelectionV1.from_mapping(selection_mapping())

    with pytest.raises(FrozenInstanceError):
        value.target_request_version = 99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        value.groups[0].sources = ()  # type: ignore[misc]

    unknown = selection_mapping()
    unknown["unexpected"] = True
    with pytest.raises(ValidationError, match="unknown selection fields"):
        CollaborationInputSelectionV1.from_mapping(unknown)

    mismatched = selection_mapping()
    mismatched["selection_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="selection_hash"):
        CollaborationInputSelectionV1.from_mapping(mismatched)


def test_fixture_is_independent_and_uses_real_report_hash() -> None:
    first = selection_mapping()
    second = selection_mapping()
    assert first == second
    assert first is not second
    assert first["groups"] is not second["groups"]
    source = first["groups"][0]["sources"][0]  # type: ignore[index]
    assert source["artifacts"][0]["content_hash"] == (  # type: ignore[index]
        "f43ace628f0291889c2687565f61edaa87a78a512d3cded38a4aa0ce04a6b7ee"
    )
    assert first["selection_hash"] == (
        "51e158d5614e750802aaa43afec1274799c2fbad5b8fcf7e1317699dcd633b16"
    )


def test_wire_order_is_normalized_without_changing_selection_hash() -> None:
    raw = deepcopy(selection_mapping())
    raw["groups"][0]["sources"].reverse()  # type: ignore[index,union-attr]

    restored = CollaborationInputSelectionV1.from_mapping(raw)

    assert tuple(source.worker_item_key for source in restored.groups[0].sources) == (
        "a",
        "b",
    )
    assert restored.selection_hash == selection_mapping()["selection_hash"]


def test_hard_only_selection_round_trips_without_fabricating_optional_groups() -> None:
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    hard = InputSelectionHardInputV1(
        dependency_key="source-a-to-target",
        rule_hash="a" * 64,
        source=first.groups[0].sources[0],
    )
    draft = replace(first, groups=(), hard_inputs=(hard,))
    value = replace(draft, selection_hash=draft.compute_selection_hash())
    assert CollaborationInputSelectionV1.from_mapping(value.to_mapping()) == value
    assert value.to_mapping()["groups"] == []
    assert value.schema_version == 1 and value.hard_inputs == (hard,)
    assert (
        first.selection_hash
        == "51e158d5614e750802aaa43afec1274799c2fbad5b8fcf7e1317699dcd633b16"
    )


@pytest.mark.parametrize(
    "invalid",
    ("empty", "malformed", "duplicate", "target", "hash", "missing", "empty_sources"),
)
def test_hard_only_selection_keeps_content_source_and_hash_validation(invalid) -> None:
    first = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    hard = InputSelectionHardInputV1(
        "source-a-to-target", "a" * 64, first.groups[0].sources[0]
    )
    draft = replace(first, groups=(), hard_inputs=(hard,))
    value = replace(draft, selection_hash=draft.compute_selection_hash()).to_mapping()
    if invalid == "empty":
        value["hard_inputs"] = []
    elif invalid == "malformed":
        value["hard_inputs"] = {}
    elif invalid == "duplicate":
        value["hard_inputs"] = [hard.to_mapping(), hard.to_mapping()]
    elif invalid == "target":
        value["hard_inputs"][0]["source"]["worker_item_id"] = str(
            first.target_worker_item_id
        )
    elif invalid == "hash":
        value["hard_inputs"][0]["rule_hash"] = "b" * 64
    elif invalid == "missing":
        del value["hard_inputs"]
    else:
        value["groups"] = [{"group_key": "empty", "rule_hash": "a" * 64, "sources": []}]
    with pytest.raises(ValidationError):
        CollaborationInputSelectionV1.from_mapping(value)
