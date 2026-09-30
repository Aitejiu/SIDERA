from __future__ import annotations

from uuid import UUID

import pytest
from sidera_runtime_guard import RuntimeGuardError

from sidera_control_plane.acceptance_resource_binding import (
    derive_runtime_resource_chain,
    load_execution_resource_facts,
)
from tests.integration.test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration


def _materialize_execution_request(database, ids: dict[str, UUID]) -> None:
    request = request_row(ids)
    request.status = "materialized"
    request.gate_reason = "ready"
    request.gate_details = {}
    request.execution_lineage_id = ids["execution"]
    request.first_execution_id = ids["execution"]
    with database.session() as session:
        session.add(request)
        session.commit()


def test_execution_binding_uses_the_persisted_request_and_task_chain(
    database,
    workspace_id: UUID,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    _materialize_execution_request(database, ids)

    with database.session() as session:
        facts = load_execution_resource_facts(
            session,
            execution_id=ids["execution"],
            workspace_id=workspace_id,
        )

    chain = derive_runtime_resource_chain(
        facts,
        requested_scenario="profile",
        requested_group="A",
        require_execution=True,
    )

    assert chain.resource_key == f"execution:{ids['execution']}"
    assert chain.keys == (
        f"workspace:{workspace_id}",
        f"planning_request:{ids['request']}",
        f"root_task:{ids['root_task']}",
        f"task:{ids['subtask']}",
        f"execution:{ids['execution']}",
    )


def test_execution_binding_does_not_fallback_across_workspaces(
    database,
    workspace_id: UUID,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    _materialize_execution_request(database, ids)

    with database.session() as session, pytest.raises(
        RuntimeGuardError, match="resource_unbound"
    ):
        load_execution_resource_facts(
            session,
            execution_id=ids["execution"],
            workspace_id=UUID("00000000-0000-0000-0000-000000000002"),
        )
