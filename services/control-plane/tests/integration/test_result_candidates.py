from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.application.result_integration import ResultIntegrationService
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.persistence.models import ProjectionChangeModel


def test_candidate_publish_does_not_complete_root(result_scenario) -> None:
    result_scenario.seed_plan()

    candidate = result_scenario.candidate(run_checks=False)

    assert result_scenario.head().current_candidate_id == candidate.id
    assert result_scenario.head().result_version == 2
    assert result_scenario.root().status != "succeeded"
    assert len(result_scenario.events("IntegrationCandidatePrepared")) == 1
    assert result_scenario.events("TaskCompleted") == ()
    with result_scenario.database.session() as session:
        changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == result_scenario.workspace_id,
                ProjectionChangeModel.change_type == "integration_candidate_prepared",
            )
        ).all()
    assert {change.projection_type for change in changes} == {
        "executions",
        "task_overview",
        "task_graph",
        "subtask_workspace",
        "worker_item_workspace",
        "worker_item_activity",
    }
    assert all(
        change.resource_id
        == (
            result_scenario.integration_execution_id
            if change.projection_type == "executions"
            else result_scenario.root_task_id
            if change.projection_type
            in {"task_overview", "task_graph", "subtask_workspace"}
            else result_scenario.worker_item_ids["summary"]
        )
        for change in changes
    )


def test_candidate_publish_is_idempotent_for_same_operation(result_scenario) -> None:
    result_scenario.seed_plan()
    first = result_scenario.candidate(run_checks=False)
    first_mapping = first.to_mapping()

    second = ResultIntegrationService(result_scenario.uow).publish_candidate(
        workspace_id=result_scenario.workspace_id,
        execution_id=result_scenario.integration_execution_id,
        operation_id=result_scenario.integration_operation_id,
    )

    assert second.to_mapping() == first_mapping
    assert result_scenario.head().current_candidate_id == first.id
    assert result_scenario.head().result_version == 2
    assert len(result_scenario.events("IntegrationCandidatePrepared")) == 1


def test_ordinary_execution_cannot_publish_an_integration_candidate(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    source = result_scenario.source(
        "login",
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
        commit=True,
    )
    assert source is not None

    with pytest.raises(ResultError, match="integration_worker_required"):
        ResultIntegrationService(result_scenario.uow).publish_candidate(
            workspace_id=result_scenario.workspace_id,
            execution_id=source.execution_id,
            operation_id=uuid4(),
        )


def test_changed_plan_rejects_old_integration_execution(result_scenario) -> None:
    result_scenario.seed_plan()
    first = result_scenario.candidate(run_checks=False)
    result_scenario.revise_plan(minimum_count=1)

    with pytest.raises(ResultError, match="integration_plan_changed"):
        ResultIntegrationService(result_scenario.uow).publish_candidate(
            workspace_id=result_scenario.workspace_id,
            execution_id=result_scenario.integration_execution_id,
            operation_id=uuid4(),
        )

    with result_scenario.uow() as uow:
        stored = uow.results.get_record("candidate", first.id, result_scenario.workspace_id)
    assert stored is not None
    assert stored.to_mapping() == first.to_mapping()


def test_changed_selection_rejects_old_integration_execution(result_scenario) -> None:
    result_scenario.seed_plan()
    first = result_scenario.candidate(run_checks=False)
    result_scenario.corrupt_current_selection_hash()

    with pytest.raises(ResultError, match="integration_selection_changed"):
        ResultIntegrationService(result_scenario.uow).publish_candidate(
            workspace_id=result_scenario.workspace_id,
            execution_id=result_scenario.integration_execution_id,
            operation_id=uuid4(),
        )

    with result_scenario.uow() as uow:
        stored = uow.results.get_record("candidate", first.id, result_scenario.workspace_id)
    assert stored is not None
    assert stored.to_mapping() == first.to_mapping()
