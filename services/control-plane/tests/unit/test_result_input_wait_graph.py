from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import cast

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning_proposals import PlanningProposalPayload
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
)
from sidera_control_plane.domain.proposal_validation import _validate_cycles
from sidera_control_plane.domain.result_inputs import (
    InputGroupV1,
    validate_input_wait_graph,
)

from .result_input_fixtures import group_mapping


def test_candidate_edge_and_hard_edge_can_form_a_cycle() -> None:
    group = InputGroupV1.from_mapping(group_mapping())

    with pytest.raises(ValidationError, match="input_wait_cycle"):
        validate_input_wait_graph(
            worker_keys=frozenset({"a", "b", "c", "summary"}),
            hard_edges=(("summary", "a"),),
            input_groups=(group,),
        )


def test_candidate_edges_are_added_without_removing_an_existing_hard_edge() -> None:
    group = InputGroupV1.from_mapping(group_mapping())

    validate_input_wait_graph(
        worker_keys=frozenset({"a", "b", "c", "summary"}),
        hard_edges=(("a", "summary"),),
        input_groups=(group,),
    )


def test_multiple_input_groups_are_checked_as_one_wait_graph() -> None:
    first = group_mapping()
    second = deepcopy(first)
    first["group_key"] = "research"
    first["consumer_worker_item_key"] = "summary"
    first["candidates"] = [{"worker_item_key": "a", "priority": 1}]
    first["minimum_count"] = 1
    second["group_key"] = "review"
    second["consumer_worker_item_key"] = "a"
    second["candidates"] = [{"worker_item_key": "summary", "priority": 1}]
    second["minimum_count"] = 1

    with pytest.raises(ValidationError, match="input_wait_cycle"):
        validate_input_wait_graph(
            worker_keys=frozenset({"a", "summary"}),
            hard_edges=(),
            input_groups=(
                InputGroupV1.from_mapping(first),
                InputGroupV1.from_mapping(second),
            ),
        )


@pytest.mark.parametrize(
    ("worker_keys", "hard_edges", "group_key"),
    [
        (frozenset({"a", "b", "summary"}), (), "unknown-candidate"),
        (frozenset({"a", "b", "c"}), (), "unknown-consumer"),
    ],
)
def test_wait_graph_rejects_references_outside_the_plan(
    worker_keys: frozenset[str],
    hard_edges: tuple[tuple[str, str], ...],
    group_key: str,
) -> None:
    raw = group_mapping()
    if group_key == "unknown-candidate":
        raw["candidates"] = [
            {"worker_item_key": "missing", "priority": 1},
            {"worker_item_key": "b", "priority": 2},
        ]
        raw["minimum_count"] = 1
    else:
        raw["consumer_worker_item_key"] = "missing"
    raw["group_key"] = group_key

    with pytest.raises(ValidationError, match="input_group_invalid"):
        validate_input_wait_graph(
            worker_keys=worker_keys,
            hard_edges=hard_edges,
            input_groups=(InputGroupV1.from_mapping(raw),),
        )


def test_wait_graph_rejects_duplicate_group_keys() -> None:
    first = InputGroupV1.from_mapping(group_mapping())
    second_raw = group_mapping()
    second_raw["consumer_worker_item_key"] = "other-summary"
    second = InputGroupV1.from_mapping(second_raw)

    with pytest.raises(ValidationError, match="input_group_invalid"):
        validate_input_wait_graph(
            worker_keys=frozenset({"a", "b", "c", "summary", "other-summary"}),
            hard_edges=(),
            input_groups=(first, second),
        )


def test_wait_graph_rejects_self_or_unknown_hard_edges() -> None:
    group = InputGroupV1.from_mapping(group_mapping())

    with pytest.raises(ValidationError, match="input_wait_cycle"):
        validate_input_wait_graph(
            worker_keys=frozenset({"a", "b", "c", "summary"}),
            hard_edges=(("summary", "summary"),),
            input_groups=(group,),
        )

    with pytest.raises(ValidationError, match="input_group_invalid"):
        validate_input_wait_graph(
            worker_keys=frozenset({"a", "b", "c", "summary"}),
            hard_edges=(("missing", "summary"),),
            input_groups=(group,),
        )


def test_proposal_cycle_validation_includes_policy_input_group_edges() -> None:
    group = InputGroupV1.from_mapping(group_mapping())
    payload = SimpleNamespace(
        subtasks=(),
        worker_items=tuple(
            SimpleNamespace(key=key, subtask_key="research")
            for key in ("a", "b", "c", "summary")
        ),
        subtask_dependencies=(),
        worker_item_dependencies=(
            SimpleNamespace(
                upstream_worker_item_key="summary",
                downstream_worker_item_key="a",
                kind="hard",
            ),
        ),
        execution_intents=(),
        collaboration_policy_request=SimpleNamespace(input_groups=(group,)),
    )

    with pytest.raises(ValidationError, match="input_wait_cycle"):
        _validate_cycles(cast(PlanningProposalPayload, payload))


def test_proposal_cycle_validation_expands_hard_subtask_edges() -> None:
    group = InputGroupV1.from_mapping(group_mapping())
    payload = SimpleNamespace(
        subtasks=tuple(
            SimpleNamespace(key=key)
            for key in ("prepare", "summarize")
        ),
        worker_items=tuple(
            SimpleNamespace(
                key=key,
                subtask_key=subtask_key,
            )
            for key, subtask_key in (
                ("a", "prepare"),
                ("b", "prepare"),
                ("c", "prepare"),
                ("summary", "summarize"),
            )
        ),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="summarize",
                downstream_subtask_key="prepare",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(),
        execution_intents=(),
        collaboration_policy_request=SimpleNamespace(input_groups=(group,)),
    )

    with pytest.raises(ValidationError, match="input_wait_cycle"):
        _validate_cycles(cast(PlanningProposalPayload, payload))
