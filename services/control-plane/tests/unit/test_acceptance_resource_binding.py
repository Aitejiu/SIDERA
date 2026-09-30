from __future__ import annotations

from uuid import uuid4

import pytest
from sidera_runtime_guard import RuntimeGuardError

from sidera_control_plane.acceptance_resource_binding import (
    PersistedResourceFacts,
    bind_runtime_resource_chain,
    derive_runtime_resource_chain,
    load_execution_resource_facts,
)


def _facts(**overrides: object) -> PersistedResourceFacts:
    values: dict[str, object] = {
        "workspace_id": uuid4(),
        "planning_request_id": uuid4(),
        "planning_kind": "create",
        "root_task_id": uuid4(),
        "task_id": uuid4(),
        "execution_id": uuid4(),
        "handoff_transition_id": None,
    }
    values.update(overrides)
    return PersistedResourceFacts(**values)


def test_resource_chain_comes_from_persisted_facts_not_requested_scenario() -> None:
    facts = _facts(planning_kind="create")

    chain = derive_runtime_resource_chain(
        facts,
        requested_scenario="profile",
        requested_group="A",
    )

    assert chain.scenario == "profile"
    assert chain.group == "A"
    assert chain.resource_key == f"execution:{facts.execution_id}"
    assert chain.keys == (
        f"workspace:{facts.workspace_id}",
        f"planning_request:{facts.planning_request_id}",
        f"root_task:{facts.root_task_id}",
        f"task:{facts.task_id}",
        f"execution:{facts.execution_id}",
    )


@pytest.mark.parametrize(
    ("scenario", "group"),
    (("revision", "B"), ("second-adapter", "C")),
)
def test_new_task_planning_uses_the_active_acceptance_stage(
    scenario: str,
    group: str,
) -> None:
    facts = _facts(
        planning_kind="create",
        root_task_id=None,
        task_id=None,
        execution_id=None,
    )

    chain = derive_runtime_resource_chain(
        facts,
        requested_scenario=scenario,
        requested_group=group,
    )

    assert chain.scenario == scenario
    assert chain.group == group
    assert chain.resource_key == f"planning_request:{facts.planning_request_id}"


@pytest.mark.parametrize(
    ("facts", "scenario", "group"),
    (
        (_facts(planning_kind="revision"), "profile", "A"),
        (_facts(planning_kind="create"), "revision", "A"),
        (_facts(handoff_transition_id=uuid4()), "profile", "A"),
        (_facts(planning_kind="create"), "profile", "B"),
    ),
)
def test_requested_stage_must_match_request_constraints_and_group(
    facts: PersistedResourceFacts,
    scenario: str,
    group: str,
) -> None:
    with pytest.raises(
        RuntimeGuardError,
        match="resource_(scenario|group)_mismatch",
    ):
        derive_runtime_resource_chain(
            facts,
            requested_scenario=scenario,
            requested_group=group,
        )


def test_revision_without_root_task_is_unbound() -> None:
    with pytest.raises(RuntimeGuardError, match="resource_chain_invalid"):
        derive_runtime_resource_chain(
            _facts(planning_kind="revision", root_task_id=None),
            requested_scenario="revision",
            requested_group="B",
        )


def test_execution_without_a_persisted_execution_id_is_unbound() -> None:
    with pytest.raises(RuntimeGuardError, match="resource_unbound"):
        derive_runtime_resource_chain(
            _facts(execution_id=None),
            requested_scenario="profile",
            requested_group="A",
            require_execution=True,
        )


class _RecordingGuard:
    def __init__(self) -> None:
        self.bindings: list[tuple[str, str, tuple[str, ...]]] = []

    def bind_resource(
        self,
        parent_key: str,
        resource_key: str,
        *,
        resource_chain: tuple[str, ...],
    ) -> None:
        self.bindings.append((parent_key, resource_key, resource_chain))


def test_runtime_resource_chain_binds_the_charged_resource() -> None:
    facts = _facts()
    chain = derive_runtime_resource_chain(facts)
    guard = _RecordingGuard()

    bind_runtime_resource_chain(guard, chain)

    assert guard.bindings == [
        (chain.keys[-2], chain.resource_key, chain.keys),
    ]


def test_revision_planning_chain_binds_the_planning_request() -> None:
    root_task_id = uuid4()
    chain = derive_runtime_resource_chain(
        _facts(
            planning_kind="revision",
            root_task_id=root_task_id,
            task_id=root_task_id,
            execution_id=None,
        ),
        requested_scenario="revision",
        requested_group="B",
    )
    guard = _RecordingGuard()

    bind_runtime_resource_chain(guard, chain)

    assert guard.bindings == [
        (chain.keys[0], chain.resource_key, chain.keys),
    ]


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        if len(self._rows) > 1:
            raise AssertionError("test result unexpectedly contained multiple rows")
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _Session:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def execute(self, _statement: object) -> _Result:
        return _Result(self.rows)


def test_execution_facts_are_scoped_to_the_persisted_workspace() -> None:
    workspace_id = uuid4()
    facts = {
        "workspace_id": workspace_id,
        "execution_id": uuid4(),
        "task_id": uuid4(),
        "planning_request_id": uuid4(),
        "planning_kind": "create",
        "root_task_id": uuid4(),
        "handoff_transition_id": None,
    }

    resolved = load_execution_resource_facts(
        _Session([facts]),
        execution_id=facts["execution_id"],  # type: ignore[arg-type]
        workspace_id=workspace_id,
    )

    assert resolved == PersistedResourceFacts(**facts)


def test_unbound_execution_has_no_fallback_resource() -> None:
    with pytest.raises(RuntimeGuardError, match="resource_unbound"):
        load_execution_resource_facts(
            _Session([]), execution_id=uuid4(), workspace_id=uuid4()
        )


def test_cross_workspace_execution_facts_are_rejected() -> None:
    requested_workspace = uuid4()
    facts = {
        "workspace_id": uuid4(),
        "execution_id": uuid4(),
        "task_id": uuid4(),
        "planning_request_id": uuid4(),
        "planning_kind": "create",
        "root_task_id": uuid4(),
        "handoff_transition_id": None,
    }

    with pytest.raises(RuntimeGuardError, match="resource_workspace_mismatch"):
        load_execution_resource_facts(
            _Session([facts]),
            execution_id=facts["execution_id"],  # type: ignore[arg-type]
            workspace_id=requested_workspace,
        )
