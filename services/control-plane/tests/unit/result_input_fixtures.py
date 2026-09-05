from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256


def _selection_hash_payload(value: dict[str, object]) -> dict[str, object]:
    """Build the fixture's independent semantic selection payload."""

    ignored = {
        "created_at",
        "decision_id",
        "id",
        "intent_id",
        "projection_hash",
        "projection_snapshot_id",
        "selection_hash",
    }

    def normalize(item: object) -> object:
        if isinstance(item, dict):
            return {
                key: normalize(item[key]) for key in sorted(item) if key not in ignored
            }
        if isinstance(item, list):
            return [normalize(entry) for entry in item]
        return item

    normalized = normalize(value)
    assert isinstance(normalized, dict)
    return normalized


def _build_selection_mapping() -> dict[str, object]:

    report_a = b"report-a\n"
    report_b = b"report-b\n"
    raw: dict[str, object] = {
        "schema_version": 1,
        "id": "61000000-0000-0000-0000-000000000001",
        "workspace_id": "61000000-0000-0000-0000-000000000002",
        "root_task_id": "61000000-0000-0000-0000-000000000003",
        "run_id": "61000000-0000-0000-0000-000000000004",
        "plan_snapshot_id": "61000000-0000-0000-0000-000000000005",
        "plan_generation": 4,
        "plan_content_hash": "a" * 64,
        "target_worker_item_id": "61000000-0000-0000-0000-000000000006",
        "target_execution_request_id": "61000000-0000-0000-0000-000000000007",
        "target_request_version": 3,
        "projection_snapshot_id": "61000000-0000-0000-0000-000000000008",
        "projection_hash": "b" * 64,
        "decision_id": "61000000-0000-0000-0000-000000000009",
        "intent_id": "61000000-0000-0000-0000-000000000010",
        "groups": [
            {
                "group_key": "research",
                "rule_hash": "c" * 64,
                "sources": [
                    {
                        "worker_item_key": "a",
                        "worker_item_id": "61000000-0000-0000-0000-000000000011",
                        "subtask_path": ["research"],
                        "effective_request_id": "61000000-0000-0000-0000-000000000012",
                        "allowed_lineage_ids": ["61000000-0000-0000-0000-000000000013"],
                        "effective_lineage_id": "61000000-0000-0000-0000-000000000013",
                        "effective_execution_id": "61000000-0000-0000-0000-000000000014",
                        "attempt": 1,
                        "inheritance_refs": [],
                        "priority": 1,
                        "source_basis_hash": "d" * 64,
                        "artifacts": [
                            {
                                "artifact_id": "61000000-0000-0000-0000-000000000015",
                                "artifact_type": "report",
                                "content_hash": sha256(report_a).hexdigest(),
                                "size_bytes": len(report_a),
                                "media_type": "text/plain",
                            }
                        ],
                        "evidence": [
                            {
                                "evidence_id": "61000000-0000-0000-0000-000000000016",
                                "acceptance_criterion_ref": "acceptance:research-complete",
                                "artifact_ids": [
                                    "61000000-0000-0000-0000-000000000015"
                                ],
                            }
                        ],
                    },
                    {
                        "worker_item_key": "b",
                        "worker_item_id": "61000000-0000-0000-0000-000000000017",
                        "subtask_path": ["research"],
                        "effective_request_id": "61000000-0000-0000-0000-000000000018",
                        "allowed_lineage_ids": ["61000000-0000-0000-0000-000000000019"],
                        "effective_lineage_id": "61000000-0000-0000-0000-000000000019",
                        "effective_execution_id": "61000000-0000-0000-0000-000000000020",
                        "attempt": 1,
                        "inheritance_refs": [],
                        "priority": 2,
                        "source_basis_hash": "e" * 64,
                        "artifacts": [
                            {
                                "artifact_id": "61000000-0000-0000-0000-000000000021",
                                "artifact_type": "report",
                                "content_hash": sha256(report_b).hexdigest(),
                                "size_bytes": len(report_b),
                                "media_type": "text/plain",
                            }
                        ],
                        "evidence": [
                            {
                                "evidence_id": "61000000-0000-0000-0000-000000000022",
                                "acceptance_criterion_ref": "acceptance:research-complete",
                                "artifact_ids": [
                                    "61000000-0000-0000-0000-000000000021"
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
        "hard_inputs": [],
        "created_at": "2026-08-25T12:00:00+00:00",
    }
    payload = _selection_hash_payload(raw)
    raw["selection_hash"] = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return raw


_SELECTION_MAPPING = _build_selection_mapping()


def selection_mapping() -> dict[str, object]:
    """Return an independent, valid immutable-selection fixture."""

    return deepcopy(_SELECTION_MAPPING)


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
                "candidates": [{"worker_item_key": "build-worker", "priority": 1}],
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


def revision_v5_mapping() -> dict[str, object]:
    """Return a complete target revision with an explicit result-input group."""

    from .test_planning_revision_proposals import proposal

    raw = deepcopy(proposal().to_mapping())
    raw["schema_version"] = 5

    workers = raw["worker_items"]
    intents = raw["execution_intents"]
    worker_decisions = raw["worker_item_identity_decisions"]
    assert isinstance(workers, list)
    assert isinstance(intents, list)
    assert isinstance(worker_decisions, list)

    workers[0]["key"] = "a"
    intents[0]["worker_item_key"] = "a"
    worker_decisions[0]["target_worker_item_key"] = "a"

    for key, title in (
        ("b", "Worker B"),
        ("c", "Worker C"),
        ("summary", "Summary worker"),
    ):
        worker = deepcopy(workers[0])
        worker["key"] = key
        worker["title"] = title
        worker["role"] = "summarizer" if key == "summary" else "builder"
        worker["responsibility"] = (
            "Summarize the selected implementation results."
            if key == "summary"
            else f"Produce implementation result {key}."
        )
        worker["pinned_worker_id"] = None
        workers.append(worker)

        intent = deepcopy(intents[0])
        intent["worker_item_key"] = key
        intent["purpose"] = worker["responsibility"]
        intents.append(intent)

        decision = deepcopy(worker_decisions[0])
        decision["target_worker_item_key"] = key
        decision["strategy"] = "create"
        decision["existing_worker_item_id"] = None
        decision["derived_from_worker_item_ids"] = []
        decision["context_disposition"] = "unchanged"
        decision["change_reasons"] = ["new target WorkerItem"]
        worker_decisions.append(decision)

    raw["collaboration_policy_request"] = {
        "schema_version": 2,
        "mode": "enabled",
        "default_policy": {"strategy_id": "all_required"},
        "subtask_overrides": [],
        "input_groups": [
            {
                "group_key": "research",
                "title": "Research results",
                "consumer_worker_item_key": "summary",
                "candidates": [
                    {"worker_item_key": "a", "priority": 1},
                    {"worker_item_key": "b", "priority": 2},
                    {"worker_item_key": "c", "priority": 3},
                ],
                "requirements": {
                    "execution_status": "succeeded",
                    "required_artifact_refs": ["artifact_type:build"],
                    "required_evidence_refs": ["acceptance:implementation"],
                    "evidence_status": "candidate",
                },
                "mode": "at_least",
                "minimum_count": 2,
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
