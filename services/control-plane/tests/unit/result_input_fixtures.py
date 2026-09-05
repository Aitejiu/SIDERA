from __future__ import annotations

from copy import deepcopy


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
