from __future__ import annotations

import os
import subprocess
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

if os.environ.get("SIDERA_INTERVIEW_DEMO_TEST_RUN") != "1":
    raise RuntimeError(
        "test_interview_results.py must run through the isolated demo test runner"
    )

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.result_integration_pipeline import (
    INTEGRATION_CANDIDATE_PUBLICATION_KIND,
    integration_candidate_publication_operation_id,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.demo.results import DemoResultError, restore_demo_candidate
from sidera_control_plane.demo.sample_repository import (
    create_sample_repository,
    materialize_sample_stage,
)
from sidera_control_plane.domain.result_verification import VerificationStatus
from sidera_control_plane.result_worker import build_result_worker
from tests.integration.result_integration_scenario import ResultIntegrationScenario
from tests.integration.test_r5_1_execution_request_schema import (
    seed_execution_request_facts,
)
from tests.unit.interview_demo_support import manifest_for

pytestmark = pytest.mark.integration


def test_verified_result_acceptance_restores_the_same_tree_without_advancing_branch(
    database, database_url: str, workspace_id, tmp_path, clean_database: None
) -> None:
    repository, base_commit = create_sample_repository(tmp_path)
    for stage in ("validation", "report", "integration"):
        materialize_sample_stage(repository, stage, True)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=repository,
        capture_output=True,
        check=True,
    )
    manifest = manifest_for(tmp_path).model_copy(
        update={
            "workspace_id": workspace_id,
            "run_root": tmp_path / "interview-run",
            "repository": repository,
            "base_commit": base_commit,
            "code_commit": base_commit,
        }
    )
    manifest.run_root.mkdir(parents=True)
    scenario = ResultIntegrationScenario(
        database,
        workspace_id,
        tmp_path,
        seed_execution_request_facts(database, workspace_id),
        uuid4(),
        base_commit=base_commit,
        repository_path=repository,
    )
    scenario.seed_plan(integration_base_commit=base_commit)
    candidate = scenario.candidate(run_checks=False)
    assert candidate.base_commit == manifest.base_commit
    with scenario.uow() as uow:
        plan = uow.plans.get(candidate.plan_snapshot_id, workspace_id)
    assert plan is not None and plan.integration_spec is not None
    criteria = [
        {
            "criterion_key": item.key,
            "decision": "accept_passed",
            "reason": None,
        }
        for item in plan.integration_spec.criteria
    ]

    settings = Settings(
        database_url=database_url,
        local_actor_type="local",
        local_actor_id="operator",
        result_integration_enabled=True,
        result_repository_ref="repo://integration-test",
        result_repository_path=repository,
        result_base_commit=base_commit,
        workspace_root=manifest.run_root / "workspaces",
        artifact_root=manifest.run_root / "artifacts",
        planning_temp_root=manifest.run_root / "planning",
        knowledge_candidate_spool_dir=manifest.run_root / "candidate-spool",
    )
    workspace_id_text = str(workspace_id)
    root_task_id_text = str(scenario.root_task_id)
    candidate_id_text = str(candidate.id)
    overview_path = (
        f"/api/v1/workspaces/{workspace_id_text}/tasks/{root_task_id_text}/results"
    )
    basis_path = f"{overview_path}/{candidate_id_text}/acceptance-basis"

    with TestClient(create_app(settings)) as client:
        overview_response = client.get(overview_path)
        assert overview_response.status_code == 200, overview_response.text
        action = next(
            item
            for item in overview_response.json()["available_actions"]
            if item["command_type"] == "CompleteTask"
        )
        unverified_basis = client.post(basis_path, json={"criteria": criteria})
        assert unverified_basis.status_code >= 400, unverified_basis.text

        unaccepted_destination = manifest.run_root / "restored" / "unaccepted"
        unaccepted_destination.parent.mkdir(parents=True)
        unaccepted_destination.mkdir()
        with pytest.raises(DemoResultError, match="candidate_not_accepted"):
            restore_demo_candidate(manifest, candidate.id, unaccepted_destination)

        scenario._run_verification_checks(candidate)
        verification_results = scenario.verification_results(candidate.id)
        assert len(verification_results) == 1, [
            result.to_mapping() for result in verification_results
        ]
        assert verification_results[0].status is VerificationStatus.PASSED, (
            verification_results[0].to_mapping()
        )
        basis_response = client.post(basis_path, json={"criteria": criteria})
        assert basis_response.status_code == 200, basis_response.text
        basis = basis_response.json()
        payload = {
            "candidate_id": basis["candidate_id"],
            "candidate_hash": basis["candidate_hash"],
            "acceptance_basis_hash": basis["acceptance_basis_hash"],
            "expected_result_version": basis["expected_result_version"],
            "criteria": criteria,
        }
        command = {
            "command_id": str(uuid4()),
            "workspace_id": workspace_id_text,
            "command_type": "CompleteTask",
            "target_type": action["target_type"],
            "actor": {"type": "local", "id": "operator"},
            "target_id": action["target_id"],
            "expected_version": action["expected_version"],
            "idempotency_key": f"interview-result-accept:{candidate.id}",
            "correlation_id": root_task_id_text,
            "payload": payload,
        }
        accepted = client.post("/api/v1/commands", json=command)
        assert accepted.status_code in {200, 201}, accepted.text
        repeated = client.post("/api/v1/commands", json=command)
        assert repeated.status_code == 200, repeated.text

    assert len(scenario.events("TaskAcceptanceRecorded")) == 1
    assert scenario.head().result_version == basis["expected_result_version"] + 1
    wrong_identity_destination = manifest.run_root / "restored" / "wrong-identity"
    wrong_identity_destination.mkdir()
    wrong_manifest = manifest.model_copy(update={"workspace_id": uuid4()})
    with pytest.raises(DemoResultError, match="candidate_not_found"):
        restore_demo_candidate(wrong_manifest, candidate.id, wrong_identity_destination)

    target_before = scenario.target_commit()
    restore_destination = manifest.run_root / "restored" / candidate.id.hex
    restore_destination.mkdir()
    restored_tree = restore_demo_candidate(
        manifest,
        candidate.id,
        restore_destination,
    )

    assert restored_tree == scenario.candidate_tree(candidate)
    assert scenario.target_commit() == target_before


def test_integration_completion_durably_publishes_candidate_and_schedules_check(
    database, database_url: str, workspace_id, tmp_path, clean_database: None
) -> None:
    repository, base_commit = create_sample_repository(tmp_path)
    for stage in ("validation", "report", "integration"):
        materialize_sample_stage(repository, stage, True)
    scenario = ResultIntegrationScenario(
        database,
        workspace_id,
        tmp_path,
        seed_execution_request_facts(database, workspace_id),
        uuid4(),
        base_commit=base_commit,
        repository_path=repository,
        result_integration_enabled=True,
    )
    scenario.seed_plan(integration_base_commit=base_commit)
    scenario.source(
        "a", files={"login.py": b"ALLOW_EXPIRED = False\n"}, commit=True
    )
    scenario.source(
        "b", files={"profile.py": b"PROFILE_ENABLED = True\n"}, commit=True
    )

    scenario._run_integration_execution()

    operation_id = integration_candidate_publication_operation_id(
        scenario.integration_execution_id
    )
    with scenario.uow() as uow:
        operation = uow.results.get_operation(operation_id, workspace_id)
    assert operation is not None
    assert operation.operation_kind == INTEGRATION_CANDIDATE_PUBLICATION_KIND
    assert operation.status.value == "pending"

    result_worker_runtime = build_result_worker(
        Settings(
            database_url=database_url,
            result_integration_enabled=True,
            result_repository_ref=scenario.repository_ref,
            result_repository_path=repository,
            result_base_commit=base_commit,
        ),
        repository_resolver={scenario.repository_ref: repository},
    )
    try:
        first_batch = result_worker_runtime.worker.run_once(limit=1)
        second_batch = result_worker_runtime.worker.run_once(limit=1)
    finally:
        result_worker_runtime.database.dispose()
    assert first_batch.processed == 1
    assert second_batch.processed == 0

    with scenario.uow() as uow:
        completed = uow.results.get_operation(operation_id, workspace_id)
        head = uow.results.get_head(workspace_id, scenario.root_task_id)
        candidate = (
            uow.results.get_record("candidate", head.current_candidate_id, workspace_id)
            if head is not None and head.current_candidate_id is not None
            else None
        )
        request = (
            uow.execution_requests.get(
                UUID(str(completed.payload["verification_request_id"])), workspace_id
            )
            if completed is not None
            else None
        )
    assert completed is not None and completed.status.value == "succeeded"
    assert candidate is not None
    assert request is not None and request.status.value == "ready"
    assert len(scenario.events("IntegrationCandidatePrepared")) == 1
    assert len(scenario.events("VerificationExecutionScheduled")) == 1
