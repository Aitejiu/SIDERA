from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from sidera_control_plane.domain.collaboration import CollaborationPolicySnapshotV2
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import TaskPlanSnapshot

from .result_input_fixtures import group_mapping
from .test_plan_contracts import collaboration_policy_payload, plan_v2_payload

LEGACY_V3_HASH = "0f6d2298325de0f2edfae0ec604b3a805bdf59a8cc371da4ffd490c57ff0a835"


def legacy_v3_payload() -> dict[str, object]:
    raw = deepcopy(plan_v2_payload())
    raw["schema_version"] = 3
    raw["collaboration_policy"] = collaboration_policy_payload()
    return raw


def policy_v2_payload() -> dict[str, object]:
    policy = collaboration_policy_payload()
    policy["schema_version"] = 2
    policy["runtime_interface_version"] = "collaboration-runtime-v2"
    capability = deepcopy(policy["runtime_capability_snapshot"])
    assert isinstance(capability, dict)
    capability["interface_version"] = "collaboration-runtime-v2"
    capability["projection_schema_versions"] = [2]
    capability["decision_schema_versions"] = [2]
    capability["strategy_ids"] = ["all_required", "input_groups_v1"]
    policy["runtime_capability_snapshot"] = capability
    group = group_mapping()
    group["consumer_worker_item_key"] = "integration-checker"
    group["candidates"] = [
        {"worker_item_key": "callback-implementer", "priority": 1},
        {"worker_item_key": "callback-reviewer", "priority": 2},
    ]
    group["minimum_count"] = 2
    policy["input_groups"] = [group]
    return policy


def plan_v4_payload() -> dict[str, object]:
    raw = legacy_v3_payload()
    raw["schema_version"] = 4
    raw["collaboration_policy"] = policy_v2_payload()
    return raw


def policy_mapping(raw: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], raw["collaboration_policy"])


def first_group_mapping(raw: dict[str, object]) -> dict[str, object]:
    groups = cast(list[dict[str, object]], policy_mapping(raw)["input_groups"])
    return groups[0]


def test_legacy_v3_payload_and_hash_remain_literal_compatible() -> None:
    raw = legacy_v3_payload()
    snapshot = TaskPlanSnapshot.from_payload(raw)

    assert snapshot.to_payload() == raw
    assert plan_snapshot_content_hash(snapshot) == LEGACY_V3_HASH


def test_schema_v4_round_trips_policy_v2_and_input_groups() -> None:
    raw = plan_v4_payload()

    snapshot = TaskPlanSnapshot.from_payload(raw)

    assert snapshot.schema_version == 4
    assert isinstance(snapshot.collaboration_policy, CollaborationPolicySnapshotV2)
    assert snapshot.collaboration_policy.schema_version == 2
    assert snapshot.collaboration_policy.input_groups[0].consumer_worker_item_key == (
        "integration-checker"
    )
    assert snapshot.to_payload() == raw


def test_schema_v4_normalizes_the_existing_three_layer_plan_shape() -> None:
    snapshot = TaskPlanSnapshot.from_payload(plan_v4_payload())

    canonical = PlanNormalizer().normalize(snapshot)

    assert canonical.schema_version == 4
    assert canonical.worker_items[0].key == "callback-implementer"
    assert canonical.subtask_dependencies[0].upstream_subtask_key == (
        "callback-idempotency"
    )


def test_schema_v4_requires_a_v2_policy_snapshot() -> None:
    missing = plan_v4_payload()
    del missing["collaboration_policy"]
    with pytest.raises(ValidationError, match="collaboration_policy"):
        TaskPlanSnapshot.from_payload(missing)

    legacy_policy = plan_v4_payload()
    legacy_policy["collaboration_policy"] = collaboration_policy_payload()
    with pytest.raises(ValidationError, match="schema version"):
        TaskPlanSnapshot.from_payload(legacy_policy)


def test_schema_v4_rejects_unknown_policy_fields() -> None:
    raw = plan_v4_payload()
    policy = deepcopy(raw["collaboration_policy"])
    assert isinstance(policy, dict)
    policy["provider"] = "forbidden"
    raw["collaboration_policy"] = policy

    with pytest.raises(ValidationError, match="unknown collaboration policy snapshot"):
        TaskPlanSnapshot.from_payload(raw)


def test_v4_hash_covers_input_group_rules() -> None:
    baseline = TaskPlanSnapshot.from_payload(plan_v4_payload())
    baseline_hash = plan_snapshot_content_hash(baseline)

    mutations = []
    changed_title = plan_v4_payload()
    first_group_mapping(changed_title)["title"] = "Changed"
    mutations.append(changed_title)

    changed_priority = plan_v4_payload()
    candidates = cast(list[dict[str, object]], first_group_mapping(changed_priority)["candidates"])
    candidates[0]["priority"], candidates[1]["priority"] = 2, 1
    mutations.append(changed_priority)

    changed_threshold = plan_v4_payload()
    first_group_mapping(changed_threshold)["minimum_count"] = 1
    mutations.append(changed_threshold)

    changed_requirement = plan_v4_payload()
    requirements = cast(
        dict[str, object], first_group_mapping(changed_requirement)["requirements"]
    )
    requirements["required_artifact_refs"] = ["artifact_type:log"]
    mutations.append(changed_requirement)

    for raw in mutations:
        assert plan_snapshot_content_hash(TaskPlanSnapshot.from_payload(raw)) != baseline_hash


def test_v4_hash_is_stable_for_mapping_key_order_and_candidate_priority_order() -> None:
    first = plan_v4_payload()
    second = plan_v4_payload()
    group = first_group_mapping(second)
    requirements = cast(dict[str, object], group["requirements"])
    candidates = cast(list[dict[str, object]], group["candidates"])
    group["requirements"] = {
        "required_evidence_refs": requirements["required_evidence_refs"],
        "evidence_status": requirements["evidence_status"],
        "required_artifact_refs": requirements["required_artifact_refs"],
        "execution_status": requirements["execution_status"],
    }
    group["candidates"] = list(reversed(candidates))

    assert plan_snapshot_content_hash(TaskPlanSnapshot.from_payload(first)) == (
        plan_snapshot_content_hash(TaskPlanSnapshot.from_payload(second))
    )
