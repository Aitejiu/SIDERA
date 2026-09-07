from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.application.result_input_sources import (
    resolve_result_input_source,
)
from sidera_control_plane.domain.execution_requests import ExecutionRequestStatus
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.models import ExecutionModel

pytestmark = pytest.mark.integration


def test_verification_requires_a_published_candidate(result_scenario) -> None:
    result_scenario.seed_plan()

    with pytest.raises(ResultError, match="verification_candidate_not_found"):
        result_scenario.verification_service.schedule(
            workspace_id=result_scenario.workspace_id,
            candidate_id=uuid4(),
            command_id=uuid4(),
        )


def test_verification_schedule_is_idempotent_for_one_candidate(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(run_checks=False)

    first = result_scenario.verification_service.schedule(
        workspace_id=result_scenario.workspace_id,
        candidate_id=candidate.id,
        command_id=uuid4(),
    )
    second = result_scenario.verification_service.schedule(
        workspace_id=result_scenario.workspace_id,
        candidate_id=candidate.id,
        command_id=uuid4(),
    )

    assert second == first
    request = result_scenario.request(first)
    assert request.result_derivation_kind == "verification"
    assert request.source_candidate_id == candidate.id
    assert request.input_selection_id is None
    assert request.status is ExecutionRequestStatus.READY
    assert request.worker_item_id == result_scenario.worker_item_ids["summary"]


def test_verification_schedule_blocks_while_integration_is_writing(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(run_checks=False)
    with result_scenario.database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.workspace_id == result_scenario.workspace_id,
                ExecutionModel.id == candidate.integration_execution_id,
            )
            .values(status="running")
        )
        session.commit()

    with pytest.raises(ResultError, match="verification_source_not_terminal"):
        result_scenario.verification_service.schedule(
            workspace_id=result_scenario.workspace_id,
            candidate_id=candidate.id,
            command_id=uuid4(),
        )


def test_verification_uses_original_workeritem_without_reconfiguring_it(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(run_checks=False)
    original = result_scenario.worker_configuration("integrate")

    request_id = result_scenario.verification_service.schedule(
        workspace_id=candidate.workspace_id,
        candidate_id=candidate.id,
        command_id=uuid4(),
    )

    request = result_scenario.request(request_id)
    assert request.result_derivation_kind == "verification"
    assert request.source_candidate_id == candidate.id
    assert result_scenario.worker_configuration("integrate") == original


def test_verification_context_binds_exact_candidate_without_code_selection(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(run_checks=False)
    request_id = result_scenario.verification_service.schedule(
        workspace_id=candidate.workspace_id,
        candidate_id=candidate.id,
        command_id=uuid4(),
    )

    result_scenario.tick()
    request = result_scenario.request(request_id)
    assert request.status is ExecutionRequestStatus.MATERIALIZED
    assembly = result_scenario.context_assembly_for_request(request_id)
    assert assembly.manifest_schema_version == 3
    assert assembly.input_selection_id is None
    assert assembly.verification_manifest is not None
    assert assembly.verification_manifest.candidate_id == candidate.id
    assert assembly.verification_manifest.candidate_hash == candidate.content_hash
    assert "execution_request_id" not in assembly.budget


def test_verification_request_is_not_a_code_result_source(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(run_checks=False)
    result_scenario.verification_service.schedule(
        workspace_id=candidate.workspace_id,
        candidate_id=candidate.id,
        command_id=uuid4(),
    )

    with result_scenario.uow() as uow:
        run = uow.collaboration_runs.get_current(
            result_scenario.workspace_id,
            result_scenario.root_task_id,
        )
        assert run is not None
        facts = SqlAlchemyCollaborationFactStore(session=uow.session).load(
            result_scenario.workspace_id,
            run,
            fact_cursor=run.last_evaluated_change_cursor,
            allow_stale_head=True,
        )
        source = resolve_result_input_source(
            worker_item_key="summary",
            facts=facts,
        )

    assert source.effective_request_id == result_scenario.request_ids["summary"]
