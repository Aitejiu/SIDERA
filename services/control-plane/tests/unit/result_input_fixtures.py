from __future__ import annotations

from copy import deepcopy


def proposal_v5_mapping() -> dict[str, object]:
    """Return a complete initial proposal using the input-group policy contract."""

    from .test_planning_proposals_v4 import proposal_v4

    raw = deepcopy(proposal_v4().to_mapping())
    raw["schema_version"] = 5
    subtasks = raw["subtasks"]
    worker_items = raw["worker_items"]
    milestones = raw["milestones"]
    intents = raw["execution_intents"]
    assert isinstance(subtasks, list)
    assert isinstance(worker_items, list)
    assert isinstance(milestones, list)
    assert isinstance(intents, list)
    subtasks.append(
        {
            "key": "review",
            "kind": "child",
            "title": "Review",
            "goal_summary": "Review the implementation.",
            "acceptance_refs": ["acceptance:review"],
            "expected_outputs": ["artifact_type:report"],
            "milestone_key": "m2",
            "source_refs": ["brief:1"],
        }
    )
    worker_items.append(
        {
            "key": "review-worker",
            "subtask_key": "review",
            "title": "Review worker",
            "role": "reviewer",
            "responsibility": "Review the implementation.",
            "capability_requirements": ["code"],
            "worker_policy_ref": None,
            "configuration_ref": None,
            "concurrency_limit": 1,
            "source_refs": ["brief:1"],
            "pinned_worker_id": None,
        }
    )
    milestones.append(
        {
            "key": "m2",
            "title": "Review",
            "subtask_keys": ["review"],
            "acceptance_refs": ["acceptance:review"],
            "order_hint": 2,
        }
    )
    intents.append(
        {
            "worker_item_key": "review-worker",
            "purpose": "Review the implementation.",
            "context_refs": ["context:brief"],
            "expected_artifact_refs": ["artifact_type:report"],
            "required_evidence_refs": ["acceptance:review"],
            "capability_requirements": ["code"],
            "adapter_preferences": ["opencode"],
            "budget_class": "standard",
            "deadline_seconds": 60,
            "not_before_seconds": None,
            "max_attempts": 1,
            "activation_dependencies": [],
        }
    )
    raw["collaboration_policy_request"] = {
        "schema_version": 2,
        "mode": "enabled",
        "default_policy": {"strategy_id": "all_required"},
        "subtask_overrides": [],
        "input_groups": [
            {
                "group_key": "implementation",
                "title": "Implementation result",
                "consumer_worker_item_key": "review-worker",
                "candidates": [
                    {"worker_item_key": "build-worker", "priority": 1}
                ],
                "requirements": {
                    "execution_status": "succeeded",
                    "required_artifact_refs": ["artifact_type:build"],
                    "required_evidence_refs": ["acceptance:build"],
                    "evidence_status": "candidate",
                },
                "mode": "all",
                "minimum_count": None,
                "selection_strategy": "plan_order",
            }
        ],
    }
    return raw


def group_mapping() -> dict[str, object]:
    """Return a fresh, valid at-least-K input group mapping for unit tests."""

    return deepcopy(
        {
            "group_key": "research",
            "title": "Research reports",
            "consumer_worker_item_key": "summary",
            "candidates": [
                {"worker_item_key": "a", "priority": 1},
                {"worker_item_key": "b", "priority": 2},
                {"worker_item_key": "c", "priority": 3},
            ],
            "requirements": {
                "execution_status": "succeeded",
                "required_artifact_refs": ["artifact_type:report"],
                "required_evidence_refs": ["acceptance:research-complete"],
                "evidence_status": "candidate",
            },
            "mode": "at_least",
            "minimum_count": 2,
            "selection_strategy": "plan_order",
        }
    )
