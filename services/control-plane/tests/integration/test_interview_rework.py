from __future__ import annotations

from uuid import UUID

from sidera_control_plane.application.result_integration_pipeline import (
    enqueue_integration_candidate_publication,
    integration_candidate_publication_operation_id,
)
from tests.integration.result_integration_scenario import NOW


def test_rework_execution_is_a_replacement_result_until_reintegration(
    result_scenario,
) -> None:
    result_scenario.seed_repairable_candidate()
    candidate = result_scenario.latest_candidate()
    rework = result_scenario.command(
        "RequestTaskRework",
        result_scenario.rework_payload(
            candidate,
            target_worker_item_keys=("integrate",),
        ),
    )
    request_id = UUID(str(rework.data["request_ids"][0]))
    repaired = result_scenario.rework_source(
        request_id,
        files={"integration.txt": b"repaired result\n"},
    )

    request = result_scenario.request(request_id)
    execution_id = request.first_execution_id
    assert execution_id is not None
    with result_scenario.uow() as uow:
        execution = uow.executions.get(execution_id, result_scenario.workspace_id)
        assert execution is not None
        assert enqueue_integration_candidate_publication(
            uow,
            execution,
            now=NOW,
        ) is None
        assert uow.results.get_operation(
            integration_candidate_publication_operation_id(execution.id),
            result_scenario.workspace_id,
        ) is None

    assert repaired.execution_id == execution_id
    assert result_scenario.latest_candidate().id == candidate.id

    reintegrated = result_scenario.command(
        "ReintegrateTaskResult",
        result_scenario.reintegration_payload(
            candidate,
            repaired,
            reason="reintegrate the repaired integration WorkerItem result",
        ),
    )
    result_scenario.drive()

    replacement_candidate = result_scenario.latest_candidate()
    assert reintegrated.data["execution_request_id"]
    assert replacement_candidate.id != candidate.id
    assert replacement_candidate.previous_candidate_id == candidate.id
    assert replacement_candidate.code_result_id != candidate.code_result_id
    assert replacement_candidate.integration_execution_id != candidate.integration_execution_id


def test_duplicate_rework_command_does_not_create_a_second_operation(
    result_scenario,
) -> None:
    result_scenario.seed_repairable_candidate()
    candidate = result_scenario.latest_candidate()
    payload = result_scenario.rework_payload(candidate)

    first = result_scenario.command(
        "RequestTaskRework",
        payload,
        idempotency_key="interview-demo-rework-once",
    )
    second = result_scenario.command(
        "RequestTaskRework",
        payload,
        idempotency_key="interview-demo-rework-once",
    )

    assert second.data == first.data
    assert len(result_scenario.rework_records()) == 1
