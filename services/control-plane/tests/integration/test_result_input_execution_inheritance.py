from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.domain.context_packages import ContextConsumerType
from sidera_control_plane.persistence.database import Database

from .result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_retry_keeps_selection_but_requires_new_consumer_ack(scenario):
    scenario.run_to_started_summary()
    first_selection = scenario.selection("summary").selection_hash
    first_generation = scenario.selection("summary").plan_generation
    first_request_version = scenario.request_version("summary")
    first_consumer = scenario.latest_execution_id("summary")
    first_packages = scenario.package_ids("summary")

    scenario.fail_worker("summary")
    scenario.retry_worker("summary")

    assert scenario.selection("summary").selection_hash == first_selection
    assert scenario.selection("summary").plan_generation == first_generation
    assert scenario.request_version("summary") == first_request_version
    assert scenario.latest_execution_id("summary") != first_consumer
    assert scenario.start_call_count("summary") == 1
    assert scenario.package_ids("summary") == first_packages

    scenario.assemble_context("summary")
    scenario.start_worker("summary")

    assert scenario.start_call_count("summary") == 2
    assert len(scenario.package_ids("summary")) == len(first_packages) + 1
    value = scenario.captured_context("summary")
    assert value.metadata.consumer_id == scenario.latest_execution_id("summary")
    assert value.metadata.package_id != first_packages[0]
    with scenario.uow_factory() as uow:
        old_ack = uow.context_packages.get_ack(
            first_packages[0],
            ContextConsumerType.WORKER,
            first_consumer,
            scenario.workspace_id,
        )
        new_ack = uow.context_packages.get_ack(
            value.metadata.package_id,
            ContextConsumerType.WORKER,
            value.metadata.consumer_id,
            scenario.workspace_id,
        )
    assert old_ack is not None
    assert new_ack is not None
    assert new_ack.package_id != old_ack.package_id


def test_continuation_inherits_selection_through_source_execution_and_session(
    scenario,
):
    scenario.run_to_started_summary()
    first_selection = scenario.selection("summary").selection_hash
    first_generation = scenario.selection("summary").plan_generation
    first_request_version = scenario.request_version("summary")
    first_consumer = scenario.latest_execution_id("summary")
    first_packages = scenario.package_ids("summary")

    scenario.fail_worker("summary")
    scenario.continue_worker("summary")

    assert scenario.selection("summary").selection_hash == first_selection
    assert scenario.selection("summary").plan_generation == first_generation
    assert scenario.request_version("summary") == first_request_version
    assert scenario.latest_execution_id("summary") != first_consumer
    assert scenario.start_call_count("summary") == 1
    assert scenario.package_ids("summary") == first_packages

    scenario.assemble_context("summary")
    scenario.start_worker("summary")

    assert scenario.start_call_count("summary") == 2
    assert len(scenario.package_ids("summary")) == len(first_packages) + 1


def test_continuation_rejects_a_source_session_from_another_boundary(scenario):
    scenario.run_to_started_summary()
    first_packages = scenario.package_ids("summary")
    scenario.fail_worker("summary")
    scenario.continue_worker("summary")

    scenario.mutate_context_assembly_budget(
        "summary",
        lambda budget: budget["execution_context_inheritance"].update(
            {"source_session_id": str(UUID(int=0))}
        ),
    )

    result = scenario.run_context_worker("summary")

    assert result.blocked == 1
    assert result.ready == 0
    assert scenario.package_ids("summary") == first_packages
    assert scenario.start_call_count("summary") == 1


def test_continuation_does_not_fall_back_to_same_worker_item(scenario):
    scenario.run_to_started_summary()
    first_packages = scenario.package_ids("summary")
    scenario.fail_worker("summary")
    scenario.continue_worker("summary")
    scenario.remove_execution_source_link("summary")

    result = scenario.run_context_worker("summary")

    assert result.blocked == 1
    assert result.ready == 0
    assert scenario.package_ids("summary") == first_packages
    assert scenario.start_call_count("summary") == 1


def test_retry_after_continuation_keeps_the_explicit_source_chain(scenario):
    scenario.run_to_started_summary()
    selection = scenario.selection("summary")
    request_version = scenario.request_version("summary")

    scenario.fail_worker("summary")
    scenario.continue_worker("summary")
    scenario.assemble_context("summary")
    scenario.start_worker("summary")
    continuation_consumer = scenario.latest_execution_id("summary")

    scenario.fail_worker("summary")
    scenario.retry_worker("summary")
    retry_consumer = scenario.latest_execution_id("summary")

    assert retry_consumer != continuation_consumer
    assert scenario.selection("summary").selection_hash == selection.selection_hash
    assert scenario.request_version("summary") == request_version
    scenario.assemble_context("summary")
    scenario.start_worker("summary")

    assert scenario.start_call_count("summary") == 3
    assert len(scenario.package_ids("summary")) == 3


def test_revoked_previous_package_is_not_reused_by_retry(scenario):
    scenario.run_to_started_summary()
    first_package_id = scenario.package_ids("summary")[0]
    first_request_version = scenario.request_version("summary")

    scenario.fail_worker("summary")
    scenario.retry_worker("summary")
    scenario.revoke_package(first_package_id)
    scenario.assemble_context("summary")
    scenario.start_worker("summary")

    package_ids = scenario.package_ids("summary")
    assert len(package_ids) == 2
    assert first_package_id in package_ids
    assert scenario.request_version("summary") == first_request_version
    assert scenario.start_call_count("summary") == 2
    assert scenario.captured_context("summary").metadata.package_id != first_package_id


def test_concurrent_retry_with_same_key_replays_one_new_consumer(scenario):
    scenario.run_to_started_summary()
    scenario.fail_worker("summary")
    envelope = scenario.retry_envelope(
        "summary", idempotency_key="result-input-concurrent-retry"
    )
    service = CommandService(
        scenario.uow_factory,
        (ExecutionCommandHandler(scenario.clock),),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(service.handle, (envelope, envelope)))

    assert results[0].aggregate_id == results[1].aggregate_id
    assert scenario.execution_count("summary") == 2
    scenario.assemble_context("summary")
    scenario.start_worker("summary")
    assert scenario.start_call_count("summary") == 2
