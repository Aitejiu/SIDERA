from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from sidera_control_plane.api.schemas import RevisionInputGroupChangeResponse
from sidera_control_plane.application.plan_revision_queries import (
    RevisionInputGroupChangeView,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyRequestV2,
    CollaborationPolicySnapshotV2,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV5,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.proposal_validation import validate_proposal
from sidera_control_plane.domain.revision_diff import (
    InputGroupChange,
    RevisionDiffNormalizer,
)

from .result_input_fixtures import revision_v5_mapping
from .test_collaboration_policy_v2 import runtime_binding
from .test_planning_proposals_v5 import v5_profile_snapshot
from .test_revision_diff import _v4_snapshot_with_two_workers, base_snapshot
from .test_revision_planning_context import _request


def _payload_for_base() -> PlanningRevisionProposalPayloadV5:
    return _payload_from_raw(revision_v5_mapping())


def _payload_from_raw(
    raw: dict[str, object], base: TaskPlanSnapshot | None = None
) -> PlanningRevisionProposalPayloadV5:
    base = base or base_snapshot()
    descriptor = raw["base_descriptor"]
    assert isinstance(descriptor, dict)
    descriptor["workspace_id"] = str(base.workspace_id)
    descriptor["root_task_id"] = str(base.root_task_id)
    descriptor["plan_snapshot_id"] = str(base.id)
    descriptor["plan_generation"] = base.generation
    descriptor["plan_content_hash"] = base_snapshot_hash(base)
    return PlanningRevisionProposalPayloadV5.from_mapping(raw)


def base_snapshot_hash(snapshot) -> str:
    from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash

    return plan_snapshot_content_hash(snapshot)


def _resolved_policy(payload: PlanningRevisionProposalPayloadV5) -> CollaborationPolicySnapshotV2:
    request = CollaborationPolicyRequestV2.from_mapping(
        payload.collaboration_policy_request.to_mapping()
    )
    return CollaborationPolicySnapshotV2.from_request(request, runtime_binding())


def _group_rule_raw() -> tuple[TaskPlanSnapshot, dict[str, object]]:
    base = _v4_snapshot_with_two_workers()
    raw = revision_v5_mapping()
    policy = raw["collaboration_policy_request"]
    assert isinstance(policy, dict)
    groups = policy["input_groups"]
    assert isinstance(groups, list)
    assert isinstance(groups[0], dict)
    group = groups[0]
    group["group_key"] = "source"
    group["consumer_worker_item_key"] = "a"
    group["candidates"] = [
        {"worker_item_key": "b", "priority": 1},
        {"worker_item_key": "c", "priority": 2},
    ]
    group["minimum_count"] = 1
    source_id = base.worker_item_plans[-1].worker_item_id
    assert source_id is not None
    retired = raw["retired_worker_item_ids"]
    assert isinstance(retired, list)
    retired.append(str(source_id))
    return base, raw


def test_revision_v5_round_trips_v2_input_groups_without_losing_threshold() -> None:
    raw = revision_v5_mapping()

    value = PlanningRevisionProposalPayloadV5.from_mapping(raw)

    assert value.schema_version == 5
    assert value.to_mapping() == raw
    assert value.collaboration_policy_request.input_groups[0].minimum_count == 2

    changed = deepcopy(raw)
    policy = changed["collaboration_policy_request"]
    assert isinstance(policy, dict)
    groups = policy["input_groups"]
    assert isinstance(groups, list)
    assert isinstance(groups[0], dict)
    groups[0]["minimum_count"] = 3
    changed_value = PlanningRevisionProposalPayloadV5.from_mapping(changed)

    assert changed_value.content_hash != value.content_hash
    assert changed_value.to_mapping()["collaboration_policy_request"]["input_groups"][0]["minimum_count"] == 3  # type: ignore[index]


def test_revision_v5_validates_complete_target_scope_and_wait_graph() -> None:
    payload = _payload_for_base()

    payload.validate_against_base(base_snapshot())


def test_revision_v5_rejects_input_group_unknown_target_worker() -> None:
    raw = revision_v5_mapping()
    policy = raw["collaboration_policy_request"]
    assert isinstance(policy, dict)
    groups = policy["input_groups"]
    assert isinstance(groups, list)
    assert isinstance(groups[0], dict)
    candidates = groups[0]["candidates"]
    assert isinstance(candidates, list)
    assert isinstance(candidates[0], dict)
    candidates[0]["worker_item_key"] = "missing"
    payload = PlanningRevisionProposalPayloadV5.from_mapping(raw)

    with pytest.raises(ValidationError, match="input group.*unknown WorkerItem"):
        payload.validate_against_base(base_snapshot())


def test_revision_v5_diff_exposes_added_group_before_after_and_reason() -> None:
    payload = _payload_for_base()
    resolved = _resolved_policy(payload)
    diff = RevisionDiffNormalizer().normalize(
        base_snapshot(),
        payload,
        collaboration_policy=resolved,
    )

    assert len(diff.input_group_changes) == 1
    change = diff.input_group_changes[0]
    assert change.consumer_worker_item_key == "summary"
    assert change.group_key == "research"
    assert change.before is None
    assert change.after["minimum_count"] == 2
    assert "added" in change.reason
    assert diff.diff_hash == RevisionDiffNormalizer().normalize(
        base_snapshot(),
        PlanningRevisionProposalPayloadV5.from_mapping(payload.to_mapping()),
        collaboration_policy=resolved,
    ).diff_hash


@pytest.mark.parametrize(
    "rule_field", ("title", "priority", "minimum_count", "requirements")
)
def test_revision_v5_group_rule_fields_change_diff_and_target_hash(
    rule_field: str,
) -> None:
    base, raw = _group_rule_raw()
    baseline_payload = _payload_from_raw(deepcopy(raw), base)
    baseline = RevisionDiffNormalizer().normalize(
        base,
        baseline_payload,
        collaboration_policy=_resolved_policy(baseline_payload),
    )
    policy = raw["collaboration_policy_request"]
    assert isinstance(policy, dict)
    groups = policy["input_groups"]
    assert isinstance(groups, list)
    assert isinstance(groups[0], dict)
    group = groups[0]
    if rule_field == "title":
        group["title"] = "Updated research results"
    elif rule_field == "priority":
        candidates = group["candidates"]
        assert isinstance(candidates, list)
        assert isinstance(candidates[0], dict)
        candidates[0]["priority"] = 2
        assert isinstance(candidates[1], dict)
        candidates[1]["priority"] = 1
    elif rule_field == "minimum_count":
        group["minimum_count"] = 2
    else:
        requirements = group["requirements"]
        assert isinstance(requirements, dict)
        requirements["required_evidence_refs"] = ["acceptance:research-updated"]

    payload = _payload_from_raw(raw, base)
    resolved = _resolved_policy(payload)
    diff = RevisionDiffNormalizer().normalize(
        base,
        payload,
        collaboration_policy=resolved,
    )

    assert diff.diff_hash != baseline.diff_hash
    assert diff.target_descriptor_preview.target_plan_content_hash != (
        baseline.target_descriptor_preview.target_plan_content_hash
    )
    assert len(diff.input_group_changes) == 1
    change = diff.input_group_changes[0]
    assert change.before is not None
    assert change.after is not None
    assert change.before != change.after
    assert "changed" in change.reason


def test_revision_v5_validation_entrypoint_checks_exact_base_and_v2_policy() -> None:
    base = base_snapshot()
    request = _request(base)
    profile = replace(
        v5_profile_snapshot(),
        workspace_id=base.workspace_id,
        allowed_capabilities=("code", "python"),
    )
    payload = _payload_for_base()

    validated = validate_proposal(
        payload,
        profile_snapshot=profile,
        brief_revision=1,
        request=request,
        capability_snapshot=profile.planner_capabilities,
        base_snapshot=base,
    )

    assert validated is payload


def test_input_group_change_response_preserves_nested_json_values() -> None:
    change = InputGroupChange(
        consumer_worker_item_key="summary",
        group_key="research",
        before=None,
        after={
            "minimum_count": 2,
            "candidates": [{"worker_item_key": "a", "priority": 1}],
        },
        reason="input_group_added",
    )
    view = RevisionInputGroupChangeView(
        consumer_worker_item_key=change.consumer_worker_item_key,
        group_key=change.group_key,
        before=change.before,
        after=change.after,
        reason=change.reason,
    )

    response = RevisionInputGroupChangeResponse.from_view(view)

    assert response.model_dump() == {
        "consumer_worker_item_key": "summary",
        "group_key": "research",
        "before": None,
        "after": {
            "minimum_count": 2,
            "candidates": [{"worker_item_key": "a", "priority": 1}],
        },
        "reason": "input_group_added",
    }
