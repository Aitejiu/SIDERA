from __future__ import annotations

from copy import deepcopy

import pytest

from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.errors import ValidationError

from .result_integration_fixtures import (
    candidate_mapping,
    code_input_manifest_mapping,
    snapshot_mapping,
)


def test_snapshot_cannot_claim_a_changed_tree_with_old_hash() -> None:
    value = snapshot_mapping()
    value["result_tree"] = "e" * 40

    with pytest.raises(ValidationError, match="content hash"):
        CodeResultSnapshotV1.from_mapping(value)


def test_code_result_contracts_round_trip_and_keep_oid_hash_distinct() -> None:
    snapshot = CodeResultSnapshotV1.from_mapping(snapshot_mapping())
    candidate = IntegrationCandidateV1.from_mapping(candidate_mapping())
    manifest = CodeInputManifestV1.from_mapping(code_input_manifest_mapping())

    assert snapshot.to_mapping()["input_commit"] == "a" * 40
    assert len(snapshot.content_hash) == 64
    assert candidate.to_mapping()["source_result_ids"] == [
        "10000000-0000-0000-0000-000000000001"
    ]
    assert manifest.to_mapping()["selection_hash"] == "3" * 64
    assert snapshot.to_mapping() == snapshot_mapping()
    assert candidate.to_mapping() == candidate_mapping()
    assert manifest.to_mapping() == code_input_manifest_mapping()


def test_source_order_is_part_of_manifest_hash() -> None:
    raw = code_input_manifest_mapping()
    manifest = CodeInputManifestV1.from_mapping(raw)

    reversed_raw = deepcopy(raw)
    reversed_raw["sources"] = [
        {
            "result_id": "10000000-0000-0000-0000-000000000002",
            "result_hash": "5" * 64,
            "input_commit": "a" * 40,
            "input_tree": "b" * 40,
            "result_commit": "c" * 40,
            "result_tree": "d" * 40,
        },
        *reversed_raw["sources"],  # type: ignore[arg-type]
    ]
    reversed_raw["input_hash"] = manifest.compute_content_hash()

    with pytest.raises(ValidationError, match="content hash"):
        CodeInputManifestV1.from_mapping(reversed_raw)


@pytest.mark.parametrize("field", ["id", "created_at", "content_hash"])
def test_snapshot_hash_does_not_depend_on_identity_or_timestamp(field: str) -> None:
    raw = snapshot_mapping()
    original = CodeResultSnapshotV1.from_mapping(raw)
    changed = deepcopy(raw)
    changed[field] = (
        "ffffffff-ffff-ffff-ffff-ffffffffffff"
        if field == "id"
        else "2026-09-07T13:00:00+00:00"
        if field == "created_at"
        else original.content_hash
    )
    changed["content_hash"] = original.content_hash

    assert CodeResultSnapshotV1.from_mapping(changed).content_hash == original.content_hash
