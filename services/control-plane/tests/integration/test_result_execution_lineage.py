from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.context_requests import (
    execution_context_manifest_schema_version,
)
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.context_packages import (
    ContextPackageManifestV3,
    ContextPurpose,
)
from sidera_control_plane.domain.errors import InvalidTransition
from sidera_control_plane.domain.result_operations import ResultError
from tests.integration.result_input_scenario import _EmptyCatalog

pytestmark = pytest.mark.integration


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_rework_keeps_worker_identity_and_generation(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    generation = result_scenario.plan_generation()

    result = result_scenario.command(
        "RequestTaskRework",
        result_scenario.rework_payload(candidate),
    )

    request = result_scenario.request(UUID(result.data["request_ids"][0]))
    assert request.worker_item_id == result_scenario.worker_item_ids["a"]
    assert request.result_derivation_kind == "rework"
    assert request.source_candidate_id == candidate.id
    assert request.rework_record_id is not None
    assert result_scenario.plan_generation() == generation


def test_rework_materializes_a_fresh_v3_context_for_the_same_worker(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    source_request = result_scenario.request(result_scenario.request_ids["a"])

    result = result_scenario.command(
        "RequestTaskRework",
        result_scenario.rework_payload(candidate),
    )
    result_scenario.tick()

    rework_request = result_scenario.request(UUID(result.data["request_ids"][0]))
    assert rework_request.first_execution_id is not None
    assert rework_request.first_execution_id != source_request.first_execution_id
    with result_scenario.uow() as uow:
        rework_execution = uow.executions.get(
            rework_request.first_execution_id,
            result_scenario.workspace_id,
        )
        assert rework_execution is not None
        assert execution_context_manifest_schema_version(uow, rework_execution) == 3
    assembly = result_scenario.context_assembly_for_request(rework_request.id)
    assert assembly.manifest_schema_version == 3
    assert assembly.request_id != source_request.first_execution_id
    assert assembly.code_input_manifest is not None
    assert assembly.code_input_manifest.request_id == rework_request.id
    assert assembly.code_input_manifest.execution_id == rework_request.first_execution_id


def test_rework_context_package_resolves_candidate_inputs_and_acknowledges_once(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    result = result_scenario.command(
        "RequestTaskRework",
        result_scenario.rework_payload(candidate),
    )
    result_scenario.tick()
    rework_request = result_scenario.request(UUID(result.data["request_ids"][0]))
    assert rework_request.first_execution_id is not None

    outcome = ContextPackageWorker(
        uow_factory=result_scenario.uow,
        catalog=_EmptyCatalog(),
        owner_id="result-rework-context-test",
        lease_seconds=60,
        clock=lambda: NOW,
    ).run_once(limit=20)

    assert outcome.ready >= 1
    assert outcome.failed == 0
    with result_scenario.uow() as uow:
        package = uow.context_packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            rework_request.first_execution_id,
            result_scenario.workspace_id,
            manifest_schema_version=3,
        )
    assert isinstance(package, ContextPackageManifestV3)
    assert package.result_input_manifest is None
    assert package.code_input_manifest is not None
    assert package.code_input_manifest.request_id == rework_request.id
    assert package.code_input_manifest.execution_id == rework_request.first_execution_id


def test_rework_rejects_a_retired_worker_item(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    worker_id = result_scenario.worker_item_ids["a"]
    with result_scenario.uow() as uow:
        worker_item = uow.worker_items.get(worker_id, result_scenario.workspace_id, for_update=True)
        assert worker_item is not None
        previous_version = worker_item.version
        worker_item.retire(now=NOW + timedelta(minutes=1))
        uow.worker_items.save(worker_item, previous_version)

    with pytest.raises(ResultError, match="rework_worker_retired"):
        result_scenario.command(
            "RequestTaskRework",
            result_scenario.rework_payload(candidate),
        )

    assert result_scenario.rework_records() == ()


def test_ordinary_retry_still_rejects_a_succeeded_execution(result_scenario) -> None:
    result_scenario.seed_plan()
    result_scenario.source(
        "login",
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
        commit=True,
    )
    envelope = result_scenario._planning.retry_envelope(
        "a",
        idempotency_key="result-lineage-retry-succeeded",
    )

    with pytest.raises(InvalidTransition, match="only failed or lost executions"):
        CommandService(
            result_scenario.uow,
            (ExecutionCommandHandler(lambda: NOW),),
        ).handle(envelope)
