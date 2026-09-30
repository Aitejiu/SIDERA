import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.models import (
    CommandModel,
    ExecutionEventModel,
    ExecutionModel,
    ExecutionRequestModel,
    ExecutionSessionModel,
)
from tests.e2e.basic_acceptance.contracts import UserFlowSnapshot
from tests.e2e.basic_acceptance.user_flow import (
    collect_user_flow_observation,
    run_user_flow,
)
from tests.e2e.basic_acceptance.user_flow_restore import RestoredTreeObservation
from tests.integration.test_proposal_confirmation import command, seed_proposal

pytestmark = pytest.mark.integration


def _workspace_runtime_counts(database, workspace_id: UUID) -> dict[str, int]:
    models = {
        "commands": CommandModel,
        "executions": ExecutionModel,
        "execution_requests": ExecutionRequestModel,
        "sessions": ExecutionSessionModel,
        "events": ExecutionEventModel,
    }
    with database.session() as session:
        return {
            name: int(
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_id)
                )
                or 0
            )
            for name, model in models.items()
        }


def _git_output(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _result_event_state(result_scenario) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            event.id,
            event.event_type,
            event.aggregate_version,
            event.payload,
            event.created_at,
        )
        for event in result_scenario.events()
    )


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as test_client:
        yield test_client


def test_planning_public_confirmation_reaches_task_first_queries(
    client: TestClient,
    workspace_id: UUID,
    uow_factory,
) -> None:
    """The public confirmation result remains consumable by the task console."""

    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory,
        workspace_id,
    )

    confirmation = client.post(
        "/api/v1/commands",
        json=command(workspace_id, request_id, profile_id, content_hash),
    )

    assert confirmation.status_code == 201, confirmation.text
    confirmation_data = confirmation.json()["data"]
    root_task_id = UUID(confirmation_data["root_task_id"])

    overview = client.get(
        f"/api/v1/tasks/{root_task_id}/overview",
        params={"workspace_id": str(workspace_id)},
    )
    graph = client.get(
        f"/api/v1/tasks/{root_task_id}/graph",
        params={"workspace_id": str(workspace_id)},
    )

    assert overview.status_code == 200, overview.text
    overview_body = overview.json()
    assert overview_body["root_task_id"] == str(root_task_id)
    assert overview_body["subtask_count"] > 0

    assert graph.status_code == 200, graph.text
    graph_body = graph.json()
    assert graph_body["overview"]["root_task_id"] == str(root_task_id)
    assert graph_body["nodes"]


def test_real_shape_user_flow_report_uses_public_verification_and_independent_restore(
    database,
    database_url: str,
    result_scenario,
    tmp_path,
) -> None:
    """The result collector consumes only the public API, not scenario internals."""

    result_scenario.seed_plan()
    result_scenario.source(
        "a",
        files={"source-a.txt": b"source-a"},
        commit=True,
    )
    result_scenario.source(
        "b",
        files={"source-b.txt": b"source-b"},
        commit=True,
    )
    # Prepare the successful local check in the candidate tree before publishing it.
    # Delay verification until after context materialization so its request does not
    # compete with the integration request as the summary WorkerItem consumer.
    result_scenario._check_exit_code = 0
    result_scenario._run_integration_execution()
    integration_execution_id = result_scenario.integration_execution_id
    code_inputs = result_scenario.code_input_manifest(
        execution_id=integration_execution_id,
    )
    result_scenario._install_code_context(code_inputs)
    context_result = result_scenario._planning.run_context_worker("summary")
    assert context_result.ready >= 1, context_result
    assert context_result.failed == 0, context_result
    selection = result_scenario.selection(code_inputs.selection_id)
    source_execution_ids = tuple(result_scenario._source_execution_ids.values())
    session_ids: dict[UUID, UUID] = {}
    with database.session() as session:
        for execution_id in (*source_execution_ids, integration_execution_id):
            session_id = session.scalar(
                select(ExecutionSessionModel.id).where(
                    ExecutionSessionModel.execution_id == execution_id
                )
            )
            if session_id is None:
                raise AssertionError(f"execution {execution_id} has no session")
            session_ids[execution_id] = session_id
        now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == source_execution_ids[0])
            .values(
                started_at=now,
                finished_at=now.replace(minute=2),
                status="succeeded",
            )
        )
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == source_execution_ids[1])
            .values(
                started_at=now.replace(minute=1),
                finished_at=now.replace(minute=3),
                status="succeeded",
            )
        )
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == integration_execution_id)
            .values(
                started_at=now.replace(minute=4),
                finished_at=now.replace(minute=5),
                status="succeeded",
            )
        )
        for offset, execution_id in enumerate(
            (*source_execution_ids, integration_execution_id)
        ):
            session_id = session_ids[execution_id]
            occurred_at = now + timedelta(seconds=offset)
            session.add(
                ExecutionEventModel(
                    id=uuid4(),
                    workspace_id=result_scenario.workspace_id,
                    execution_id=execution_id,
                    session_id=session_id,
                    sequence=1,
                    event_type="session.completed",
                    payload={"source": "public-result-journey"},
                    occurred_at=occurred_at,
                    raw_ref=None,
                )
            )
            session.execute(
                update(ExecutionSessionModel)
                .where(ExecutionSessionModel.id == session_id)
                .values(last_sequence=1, last_activity_at=occurred_at)
            )
        session.commit()

    run_id = selection.run_id
    public_client = TestClient(
        create_app(
            Settings(
                database_url=database_url,
                local_actor_id="operator",
                artifact_root=tmp_path / "result-input-artifacts",
            )
        )
    )
    public_client.__enter__()
    package_response = public_client.get(
        f"/api/v1/workspaces/{result_scenario.workspace_id}"
        f"/executions/{integration_execution_id}/context-package"
        "?manifest_schema_version=3"
    )
    assert package_response.status_code == 200, package_response.text
    package_id = UUID(package_response.json()["package_id"])
    materialized = public_client.post(
        f"/api/v1/workspaces/{result_scenario.workspace_id}"
        f"/context-packages/{package_id}/materialize"
    )
    assert materialized.status_code == 200, materialized.text

    candidate = result_scenario.candidate(run_checks=True)

    acceptance_command = result_scenario.command(
        "CompleteTask", result_scenario.accept_payload(candidate)
    )
    assert acceptance_command.data["status"] == "accepted"
    result_scenario.drive()
    assert result_scenario.root().status == "succeeded"
    result_response = public_client.get(
        f"/api/v1/workspaces/{result_scenario.workspace_id}"
        f"/tasks/{result_scenario.root_task_id}/results"
    )
    assert result_response.status_code == 200, result_response.text
    result_overview = result_response.json()
    public_candidate = result_overview["current_candidate"]
    public_acceptance = result_overview["acceptance"]
    assert public_candidate["candidate_id"] == str(candidate.id)
    assert public_candidate["is_current"] is True
    assert public_acceptance["candidate_id"] == str(candidate.id)
    snapshot = UserFlowSnapshot(
        run_id=run_id,
        workspace_id=result_scenario.workspace_id,
        root_task_id=result_scenario.root_task_id,
        source_execution_ids=source_execution_ids,
        source_session_ids=tuple(session_ids[item] for item in source_execution_ids),
        integration_execution_id=integration_execution_id,
        integration_session_id=session_ids[integration_execution_id],
        input_selection_id=selection.id,
        input_selection_hash=selection.selection_hash,
        candidate_id=UUID(public_candidate["candidate_id"]),
        candidate_hash=public_candidate["candidate_hash"],
        candidate_selection_hash=selection.selection_hash,
        verification_id=None,
        verification_candidate_id=None,
        verification_candidate_hash=None,
        verification_status="unknown",
        acceptance_id=UUID(public_acceptance["acceptance_id"]),
        accepted_candidate_id=UUID(public_acceptance["candidate_id"]),
        accepted_candidate_hash=public_acceptance["candidate_hash"],
        accepted=True,
        accepted_tree_hash=public_candidate["result_tree"],
        restored_tree_hash=None,
        restore_check_passed=False,
        dependency_satisfied=False,
        source_windows=(),
    )

    read_methods: list[str] = []

    def read_json(_manifest: dict[str, object], path: str) -> dict[str, object] | None:
        read_methods.append("GET")
        response = public_client.get(path)
        if response.status_code != 200:
            return None
        value = response.json()
        return value if isinstance(value, dict) else None

    def read_content(_manifest: dict[str, object], artifact_id: UUID) -> bytes | None:
        prefix = (
            f"/api/v1/workspaces/{result_scenario.workspace_id}"
            f"/artifacts/{artifact_id}"
        )
        read_methods.append("GET")
        descriptor = public_client.get(f"{prefix}/descriptor")
        if descriptor.status_code != 200 or not descriptor.json().get("content_available"):
            return None
        read_methods.append("GET")
        content = public_client.get(f"{prefix}/content")
        return content.content if content.status_code == 200 else None

    manifest = {
        "run_id": str(run_id),
        "workspace_id": str(result_scenario.workspace_id),
        "root_task_id": str(result_scenario.root_task_id),
        "repository": str(result_scenario.repository_path),
    }
    before_report = _workspace_runtime_counts(database, result_scenario.workspace_id)
    before_result_state = (
        result_scenario.head(),
        result_scenario.verification_results(candidate.id),
        result_scenario.result_operations(),
        _result_event_state(result_scenario),
    )
    source_repository = result_scenario.repository_path
    before_git_state = (
        _git_output(source_repository, "rev-parse", "HEAD"),
        _git_output(source_repository, "for-each-ref", "--format=%(refname)=%(objectname)"),
        _git_output(source_repository, "status", "--porcelain", "--untracked-files=all"),
    )
    try:
        observed = collect_user_flow_observation(
            manifest,
            snapshot,
            public_json_fetcher=read_json,
            content_fetcher=read_content,
        )
        repeated = collect_user_flow_observation(
            manifest,
            snapshot,
            public_json_fetcher=read_json,
            content_fetcher=read_content,
        )
        report = run_user_flow(
            manifest,
            snapshot=snapshot,
            evidence_kind="real_user_flow",
            public_json_fetcher=read_json,
            content_fetcher=read_content,
        )

        def read_without_verifications(
            manifest_arg: dict[str, object], path: str
        ) -> dict[str, object] | None:
            if "/verifications?" in path:
                return None
            return read_json(manifest_arg, path)

        missing_verification = run_user_flow(
            manifest,
            snapshot=snapshot,
            evidence_kind="real_user_flow",
            public_json_fetcher=read_without_verifications,
            content_fetcher=read_content,
        )

        def read_wrong_tree(
            manifest_arg: dict[str, object], path: str
        ) -> dict[str, object] | None:
            body = read_json(manifest_arg, path)
            if "/verifications?" not in path or body is None:
                return body
            changed = dict(body)
            changed["items"] = [
                {**item, "actual_tree": "0" * 40}
                if isinstance(item, dict)
                else item
                for item in body.get("items", [])
            ]
            return changed

        wrong_verification_tree = run_user_flow(
            manifest,
            snapshot=snapshot,
            evidence_kind="real_user_flow",
            public_json_fetcher=read_wrong_tree,
            content_fetcher=read_content,
        )

        restore_unavailable = run_user_flow(
            manifest,
            snapshot=snapshot,
            evidence_kind="real_user_flow",
            public_json_fetcher=read_json,
            content_fetcher=read_content,
            restore_checker=lambda _manifest, _commit, _tree: RestoredTreeObservation(
                "incomplete", None, "restore_object_unavailable"
            ),
        )
    finally:
        public_client.__exit__(None, None, None)

    after_report = _workspace_runtime_counts(database, result_scenario.workspace_id)
    after_result_state = (
        result_scenario.head(),
        result_scenario.verification_results(candidate.id),
        result_scenario.result_operations(),
        _result_event_state(result_scenario),
    )
    after_git_state = (
        _git_output(source_repository, "rev-parse", "HEAD"),
        _git_output(source_repository, "for-each-ref", "--format=%(refname)=%(objectname)"),
        _git_output(source_repository, "status", "--porcelain", "--untracked-files=all"),
    )
    assert observed.snapshot.dependency_satisfied is True, observed.check_codes
    assert repeated.snapshot.model_dump(mode="json") == observed.snapshot.model_dump(mode="json")
    assert repeated.check_codes == observed.check_codes
    assert all(method == "GET" for method in read_methods)
    assert after_report == before_report
    assert after_result_state == before_result_state
    assert after_git_state == before_git_state
    assert report.status == "PASS", report.check_codes
    assert "verification_bound_to_candidate" in report.check_codes
    assert "candidate_accepted" in report.check_codes
    assert "accepted_tree_restored" in report.check_codes
    assert "parallel_overlap_observed" in report.check_codes
    assert missing_verification.status == "INCOMPLETE"
    assert "verification_public_missing" in missing_verification.check_codes
    assert wrong_verification_tree.status == "FAIL"
    assert wrong_verification_tree.failure_code == "verification_tree_mismatch"
    assert restore_unavailable.status == "INCOMPLETE"
    assert restore_unavailable.failure_code == "accepted_tree_restore_not_observed"
    assert "selection_public_observed" in observed.check_codes
    assert "context_package_public_observed" in observed.check_codes
    assert "source_events_observed" in observed.check_codes
    assert "integration_after_selected_inputs" in observed.check_codes
