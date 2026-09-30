from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest

from tests.e2e.basic_acceptance.contracts import ExecutionWindow, UserFlowSnapshot
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.user_flow import (
    build_user_flow_evidence,
    collect_user_flow_observation,
    run_user_flow,
)
from tests.e2e.basic_acceptance.user_flow_restore import RestoredTreeObservation


def _builder() -> EvidenceBuilder:
    return EvidenceBuilder(
        run_id=uuid4(),
        scenario="user-flow",
        evidence_kind="real_user_flow",
        code_commit="a" * 40,
        runtime_fingerprint="b" * 64,
    )


def _snapshot(**overrides: object) -> UserFlowSnapshot:
    source_a = uuid4()
    source_b = uuid4()
    integration = uuid4()
    selection_hash = "c" * 64
    candidate_hash = "d" * 64
    accepted_tree = "e" * 40
    started = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    base: dict[str, object] = {
        "run_id": uuid4(),
        "workspace_id": uuid4(),
        "root_task_id": uuid4(),
        "source_execution_ids": (source_a, source_b),
        "source_session_ids": (uuid4(), uuid4()),
        "integration_execution_id": integration,
        "integration_session_id": uuid4(),
        "input_selection_id": uuid4(),
        "input_selection_hash": selection_hash,
        "candidate_id": uuid4(),
        "candidate_hash": candidate_hash,
        "candidate_selection_hash": selection_hash,
        "verification_id": uuid4(),
        "verification_candidate_id": None,
        "verification_candidate_hash": None,
        "verification_status": "passed",
        "acceptance_id": uuid4(),
        "accepted_candidate_id": None,
        "accepted_candidate_hash": None,
        "accepted": True,
        "accepted_tree_hash": accepted_tree,
        "restored_tree_hash": accepted_tree,
        "restore_check_passed": True,
        "dependency_satisfied": True,
        "source_windows": (
            {"execution_id": source_a, "started_at": started, "finished_at": started + timedelta(minutes=2)},
            {"execution_id": source_b, "started_at": started + timedelta(minutes=1), "finished_at": started + timedelta(minutes=3)},
        ),
    }
    base.update(overrides)
    if base["verification_candidate_id"] is None:
        base["verification_candidate_id"] = base["candidate_id"]
    if base["verification_candidate_hash"] is None:
        base["verification_candidate_hash"] = base["candidate_hash"]
    if base["accepted_candidate_id"] is None:
        base["accepted_candidate_id"] = base["candidate_id"]
    if base["accepted_candidate_hash"] is None:
        base["accepted_candidate_hash"] = base["candidate_hash"]
    return UserFlowSnapshot.model_validate(base)


def test_complete_user_flow_requires_and_records_all_milestones() -> None:
    report = build_user_flow_evidence(_builder(), _snapshot())

    assert report.status == "PASS"
    assert "parallel_overlap_observed" in report.check_codes
    assert "accepted_tree_restored" in report.check_codes


def test_missing_accepted_tree_restore_is_incomplete() -> None:
    snapshot = _snapshot(restored_tree_hash=None, restore_check_passed=False)

    report = build_user_flow_evidence(_builder(), snapshot)

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "accepted_tree_restore_not_observed"


def test_restored_tree_mismatch_is_a_confirmed_failure() -> None:
    snapshot = _snapshot(restored_tree_hash="0" * 40)

    report = build_user_flow_evidence(_builder(), snapshot)

    assert report.status == "FAIL"
    assert report.failure_code == "accepted_tree_restore_mismatch"


def test_user_flow_runner_rejects_snapshot_from_another_run() -> None:
    manifest = {
        "run_id": str(uuid4()),
        "code_commit": "a" * 40,
        "runtime_fingerprint": "b" * 64,
    }
    report = run_user_flow(manifest, snapshot=_snapshot())

    assert report.status == "FAIL"
    assert report.failure_code == "user_flow_snapshot_run_id_mismatch"


def test_verification_without_acceptance_is_incomplete() -> None:
    report = build_user_flow_evidence(
        _builder(), _snapshot(accepted=False, acceptance_id=None)
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "acceptance_not_observed"


def test_candidate_input_hash_mismatch_fails() -> None:
    report = build_user_flow_evidence(
        _builder(), _snapshot(candidate_selection_hash="f" * 64)
    )

    assert report.status == "FAIL"
    assert report.failure_code == "candidate_input_selection_mismatch"


def test_missing_dependency_fact_is_incomplete() -> None:
    report = build_user_flow_evidence(
        _builder(), _snapshot(dependency_satisfied=False)
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "dependency_fact_missing"


def test_no_observed_parallel_overlap_is_incomplete() -> None:
    started = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    snapshot = _snapshot()
    report = build_user_flow_evidence(
        _builder(),
        snapshot.model_copy(
            update={
                "source_windows": (
                    ExecutionWindow(
                        execution_id=snapshot.source_execution_ids[0],
                        started_at=started,
                        finished_at=started + timedelta(minutes=1),
                    ),
                    ExecutionWindow(
                        execution_id=snapshot.source_execution_ids[1],
                        started_at=started + timedelta(minutes=1),
                        finished_at=started + timedelta(minutes=2),
                    ),
                )
            }
        ),
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "parallel_overlap_not_observed"


def _public_user_flow_fixture(
    snapshot: UserFlowSnapshot,
    *,
    integration_started_at: datetime,
    source_b_finished_at: datetime | None = datetime(
        2026, 9, 8, 10, 3, tzinfo=UTC
    ),
    selection_embeds_refs: bool = True,
) -> tuple[dict[str, object], list[str], dict[str, object]]:
    artifact_a = uuid4()
    artifact_b = uuid4()
    package_id = uuid4()
    selection = {
        "selection_id": str(snapshot.input_selection_id),
        "workspace_id": str(snapshot.workspace_id),
        "root_task_id": str(snapshot.root_task_id),
        "run_id": str(snapshot.run_id),
        "selection_hash": snapshot.input_selection_hash,
        "groups": [
            {
                "sources": [
                    {
                        "effective_execution_id": str(snapshot.source_execution_ids[0]),
                        "artifacts": [{"artifact_id": str(artifact_a)}],
                    },
                    {
                        "effective_execution_id": str(snapshot.source_execution_ids[1]),
                        "artifacts": [{"artifact_id": str(artifact_b)}],
                    },
                ]
            }
        ],
        "hard_inputs": [],
        "consumers": [
            {
                "package_id": str(package_id),
                "package_hash": "f" * 64,
            }
        ],
    }
    if not selection_embeds_refs:
        for source in selection["groups"][0]["sources"]:
            source["artifacts"] = []
        selection["consumers"][0]["package_id"] = None
    source_a_started = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    source_b_started = datetime(2026, 9, 8, 10, 1, tzinfo=UTC)
    execution_bodies = {
        snapshot.source_execution_ids[0]: {
            "id": str(snapshot.source_execution_ids[0]),
            "workspace_id": str(snapshot.workspace_id),
            "status": "succeeded",
            "started_at": source_a_started.isoformat(),
            "finished_at": datetime(2026, 9, 8, 10, 2, tzinfo=UTC).isoformat(),
        },
        snapshot.source_execution_ids[1]: {
            "id": str(snapshot.source_execution_ids[1]),
            "workspace_id": str(snapshot.workspace_id),
            "status": "succeeded",
            "started_at": source_b_started.isoformat(),
            "finished_at": (
                source_b_finished_at.isoformat()
                if source_b_finished_at is not None
                else None
            ),
        },
        snapshot.integration_execution_id: {
            "id": str(snapshot.integration_execution_id),
            "workspace_id": str(snapshot.workspace_id),
            "status": "succeeded",
            "started_at": integration_started_at.isoformat(),
            "finished_at": datetime(2026, 9, 8, 10, 5, tzinfo=UTC).isoformat(),
        },
    }
    session_to_execution = {
        snapshot.source_session_ids[0]: snapshot.source_execution_ids[0],
        snapshot.source_session_ids[1]: snapshot.source_execution_ids[1],
        snapshot.integration_session_id: snapshot.integration_execution_id,
    }
    verification_ids = (uuid4(), uuid4())
    public_acceptance_id = snapshot.acceptance_id or uuid4()
    verification_rows: list[dict[str, object]] = [
        {
            "verification_id": str(verification_id),
            "candidate_id": str(snapshot.candidate_id),
            "execution_id": str(snapshot.integration_execution_id),
            "check_key": check_key,
            "check_version": 1,
            "actual_commit": "f" * 40,
            "actual_tree": "e" * 40,
            "status": "passed",
        }
        for verification_id, check_key in zip(
            verification_ids, ("check-a", "check-b"), strict=True
        )
    ]
    verification_pages: dict[str, dict[str, object]] = {
        "": {
            "candidate_id": str(snapshot.candidate_id),
            "items": [verification_rows[0]],
            "next_cursor": "verification-page-2",
            "last_change_cursor": 0,
        },
        "verification-page-2": {
            "candidate_id": str(snapshot.candidate_id),
            "items": [verification_rows[1]],
            "next_cursor": None,
            "last_change_cursor": 0,
        },
    }
    responses: dict[str, object] = {
        "selection": selection,
        "package": {
            "package_id": str(package_id),
            "workspace_id": str(snapshot.workspace_id),
            "package_hash": "f" * 64,
            "status": "active",
            "materialized_at": datetime(2026, 9, 8, 10, 3, tzinfo=UTC).isoformat(),
            "assembly": {"status": "ready"},
        },
        "results": {
            "workspace_id": str(snapshot.workspace_id),
            "root_task_id": str(snapshot.root_task_id),
            "current_candidate": {
                "candidate_id": str(snapshot.candidate_id),
                "candidate_hash": snapshot.candidate_hash,
                "result_commit": "f" * 40,
                "result_tree": "e" * 40,
                "is_current": True,
            },
            "acceptance": {
                "acceptance_id": str(public_acceptance_id),
                "candidate_id": str(snapshot.candidate_id),
                "candidate_hash": snapshot.candidate_hash,
            },
            "criteria": [
                {
                    "criterion_key": "automatic-checks",
                    "mode": "automatic",
                    "check_keys": ["check-a", "check-b"],
                    "verification_status": "passed",
                    "observed_status": "passed",
                    "verification_result_ids": [str(item) for item in verification_ids],
                },
                {
                    "criterion_key": "manual-review",
                    "mode": "manual",
                    "check_keys": [],
                    "verification_status": "not_run",
                    "observed_status": "pending",
                    "verification_result_ids": [],
                },
            ],
        },
        "verification_pages": verification_pages,
    }
    calls: list[str] = []
    bodies_by_execution = execution_bodies
    for execution_id, body in bodies_by_execution.items():
        responses[str(execution_id)] = body
    for session_id, execution_id in session_to_execution.items():
        responses[f"session:{session_id}"] = {
            "items": [
                {
                    "id": str(session_id),
                    "workspace_id": str(snapshot.workspace_id),
                    "execution_id": str(execution_id),
                }
            ]
        }
        responses[f"event:{session_id}"] = {
            "items": [
                {
                    "id": str(uuid4()),
                    "workspace_id": str(snapshot.workspace_id),
                    "execution_id": str(execution_id),
                    "session_id": str(session_id),
                    "sequence": 1,
                    "event_type": "session_started",
                    "occurred_at": datetime(2026, 9, 8, 10, 0, tzinfo=UTC).isoformat(),
                }
            ]
        }
    content = {artifact_a: b"source-a", artifact_b: b"source-b"}
    artifact_by_execution = {
        str(snapshot.source_execution_ids[0]): artifact_a,
        str(snapshot.source_execution_ids[1]): artifact_b,
    }

    def read_json(_manifest: dict[str, object], path: str) -> dict[str, object] | None:
        calls.append(path)
        if "/input-selections/" in path:
            return responses["selection"]
        if "/context-packages/" in path:
            return responses["package"]
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/results"):
            return responses["results"]  # type: ignore[return-value]
        if parsed.path.endswith("/verifications"):
            if responses.get("verification_pages") is None:
                return None
            pages = responses["verification_pages"]
            if not isinstance(pages, dict):
                return None
            page = pages.get(query.get("cursor", [""])[0])
            return page if isinstance(page, dict) else None
        if parsed.path.endswith("/context-package"):
            return responses["package"]
        if parsed.path.endswith("/artifacts"):
            execution_id = query["execution_id"][0]
            artifact_id = artifact_by_execution.get(execution_id)
            if artifact_id is None:
                return {"items": []}
            return {
                "items": [
                    {
                        "id": str(artifact_id),
                        "workspace_id": str(snapshot.workspace_id),
                        "execution_id": execution_id,
                    }
                ]
            }
        if "/executions/" in path:
            execution_id = path.split("/executions/", 1)[1].split("?", 1)[0]
            if execution_id == str(snapshot.integration_execution_id):
                source_paths = {
                    str(snapshot.source_execution_ids[0]),
                    str(snapshot.source_execution_ids[1]),
                }
                assert source_paths.issubset(
                    {
                        item.split("/executions/", 1)[1].split("?", 1)[0]
                        for item in calls
                        if "/executions/" in item
                    }
                )
            return responses.get(execution_id)
        session_id = query["session_id"][0]
        if parsed.path.endswith("/sessions"):
            return responses[f"session:{session_id}"]
        if parsed.path.endswith("/events"):
            return responses[f"event:{session_id}"]
        return None

    def read_content(_manifest: dict[str, object], artifact_id: object) -> bytes | None:
        return content.get(artifact_id)

    manifest = {
        "run_id": str(snapshot.run_id),
        "workspace_id": str(snapshot.workspace_id),
        "root_task_id": str(snapshot.root_task_id),
    }
    return manifest, calls, {
        "read_json": read_json,
        "read_content": read_content,
        "responses": responses,
        "verification_ids": verification_ids,
    }


def test_user_flow_collector_uses_public_times_and_proves_dependency_order() -> None:
    snapshot = _snapshot(
        source_windows=(),
        dependency_satisfied=False,
    )
    manifest, calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],
        content_fetcher=readers["read_content"],
    )

    assert observed.snapshot.source_windows == (
        ExecutionWindow(
            execution_id=snapshot.source_execution_ids[0],
            started_at=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
            finished_at=datetime(2026, 9, 8, 10, 2, tzinfo=UTC),
        ),
        ExecutionWindow(
            execution_id=snapshot.source_execution_ids[1],
            started_at=datetime(2026, 9, 8, 10, 1, tzinfo=UTC),
            finished_at=datetime(2026, 9, 8, 10, 3, tzinfo=UTC),
        ),
    )
    assert observed.snapshot.dependency_satisfied is True, observed.check_codes
    assert "selection_public_observed" in observed.check_codes
    assert "selected_artifacts_observed" in observed.check_codes
    assert "acceptance_public_observed" in observed.check_codes
    assert "verification_public_observed" in observed.check_codes
    assert observed.snapshot.verification_status == "passed"
    assert observed.snapshot.verification_id is not None
    assert observed.snapshot.verification_candidate_id == snapshot.candidate_id
    assert observed.snapshot.verification_candidate_hash == snapshot.candidate_hash
    assert observed.snapshot.accepted_tree_hash == "e" * 40
    assert any(path.endswith("/results") for path in calls)
    verification_paths = [path for path in calls if "/verifications?" in path]
    assert len(verification_paths) == 2
    assert "cursor=verification-page-2" in verification_paths[1]
    assert "integration_after_selected_inputs" in observed.check_codes
    integration_index = next(
        index for index, path in enumerate(calls) if "/executions/" in path and str(snapshot.integration_execution_id) in path
    )
    assert all(
        index < integration_index
        for index, path in enumerate(calls)
        if "/executions/" in path
        and any(
            str(source_execution_id) in path
            for source_execution_id in snapshot.source_execution_ids
        )
    )

    report = run_user_flow(
        manifest,
        snapshot=snapshot,
        evidence_kind="real_user_flow",
        public_json_fetcher=readers["read_json"],
        content_fetcher=readers["read_content"],
    )
    assert report.status == "INCOMPLETE"
    assert report.failure_code == "accepted_tree_restore_not_observed"


def test_user_flow_selection_run_id_is_independent_of_acceptance_batch_id() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    responses = readers["responses"]
    assert isinstance(responses, dict)
    selection = responses["selection"]
    assert isinstance(selection, dict)
    selection["run_id"] = str(uuid4())

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )

    assert "selection_identity_bound" in observed.check_codes
    assert "selection_identity_mismatch" not in observed.check_codes


def test_user_flow_collector_does_not_trust_snapshot_verification_or_acceptance() -> None:
    snapshot = _snapshot(
        verification_id=None,
        verification_candidate_id=None,
        verification_candidate_hash=None,
        verification_status="unknown",
        accepted=False,
        acceptance_id=None,
        accepted_candidate_id=None,
        accepted_candidate_hash=None,
        accepted_tree_hash="0" * 40,
        restored_tree_hash=None,
        restore_check_passed=False,
    )
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )

    assert observed.snapshot.verification_status == "passed"
    assert observed.snapshot.accepted is True
    assert observed.snapshot.acceptance_id is not None
    assert observed.snapshot.accepted_candidate_id == snapshot.candidate_id
    assert observed.snapshot.accepted_candidate_hash == snapshot.candidate_hash
    assert observed.snapshot.accepted_tree_hash == "e" * 40
    assert observed.snapshot.restored_tree_hash is None
    assert observed.snapshot.restore_check_passed is False


def test_verification_history_ignores_failure_not_selected_by_current_criteria() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    responses = readers["responses"]
    assert isinstance(responses, dict)
    pages = responses["verification_pages"]
    assert isinstance(pages, dict)
    first_page = pages[""]
    assert isinstance(first_page, dict)
    old_failure = dict(first_page["items"][0])
    old_failure["verification_id"] = str(uuid4())
    old_failure["status"] = "failed"
    second_page = pages["verification-page-2"]
    assert isinstance(second_page, dict)
    second_page["items"] = [*second_page["items"], old_failure]

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )

    assert observed.snapshot.verification_status == "passed"
    assert "verification_public_observed" in observed.check_codes


def test_missing_required_verification_uses_only_observed_passed_representative() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    responses = readers["responses"]
    assert isinstance(responses, dict)
    results = responses["results"]
    assert isinstance(results, dict)
    criteria = results["criteria"]
    assert isinstance(criteria, list)
    criterion = criteria[0]
    assert isinstance(criterion, dict)
    observed_id = UUID(int=2)
    missing_id = UUID(int=1)
    criterion["verification_result_ids"] = [str(observed_id), str(missing_id)]

    pages = responses["verification_pages"]
    assert isinstance(pages, dict)
    first_page = pages[""]
    second_page = pages["verification-page-2"]
    assert isinstance(first_page, dict)
    assert isinstance(second_page, dict)
    first_item = dict(first_page["items"][0])
    first_item["verification_id"] = str(observed_id)
    first_page["items"] = [first_item]
    second_page["items"] = []

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )

    assert observed.snapshot.verification_status == "unknown"
    assert observed.snapshot.verification_id == observed_id
    assert "verification_records_incomplete" in observed.check_codes


def test_missing_verification_api_is_incomplete_even_when_snapshot_claims_passed() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    responses = readers["responses"]
    assert isinstance(responses, dict)
    responses["verification_pages"] = None

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )

    assert "verification_public_missing" in observed.check_codes
    assert observed.snapshot.verification_status == "unknown"
    report = build_user_flow_evidence(
        _builder(), observed.snapshot, public_check_codes=observed.check_codes
    )
    assert report.status == "INCOMPLETE"


def test_current_failed_verification_is_not_hidden_by_a_historical_pass() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    responses = readers["responses"]
    assert isinstance(responses, dict)
    results = responses["results"]
    assert isinstance(results, dict)
    criterion = results["criteria"][0]
    criterion["verification_status"] = "failed"
    criterion["observed_status"] = "failed"
    pages = responses["verification_pages"]
    assert isinstance(pages, dict)
    current_page = pages["verification-page-2"]
    assert isinstance(current_page, dict)
    current_row = dict(current_page["items"][0])
    current_row["status"] = "failed"
    current_page["items"] = [current_row]

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )
    report = build_user_flow_evidence(
        _builder(), observed.snapshot, public_check_codes=observed.check_codes
    )

    assert observed.snapshot.verification_status == "failed"
    assert report.status == "FAIL"
    assert report.failure_code == "verification_failed"


@pytest.mark.parametrize(
    ("field", "bad_value", "expected_code"),
    (
        ("candidate_id", str(uuid4()), "verification_candidate_mismatch"),
        ("actual_commit", "c" * 40, "verification_commit_mismatch"),
        ("actual_tree", "c" * 40, "verification_tree_mismatch"),
    ),
)
def test_verification_record_must_match_public_current_candidate(
    field: str, bad_value: str, expected_code: str
) -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    responses = readers["responses"]
    assert isinstance(responses, dict)
    pages = responses["verification_pages"]
    assert isinstance(pages, dict)
    first_page = pages[""]
    assert isinstance(first_page, dict)
    row = dict(first_page["items"][0])
    row[field] = bad_value
    first_page["items"] = [row]

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )

    assert expected_code in observed.check_codes
    report = build_user_flow_evidence(
        _builder(), observed.snapshot, public_check_codes=observed.check_codes
    )
    assert report.status == "FAIL"
    assert report.failure_code == expected_code


def test_verification_pagination_repeat_cursor_is_incomplete() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    responses = readers["responses"]
    assert isinstance(responses, dict)
    pages = responses["verification_pages"]
    assert isinstance(pages, dict)
    second_page = pages["verification-page-2"]
    assert isinstance(second_page, dict)
    second_page["next_cursor"] = "verification-page-2"

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )

    assert "verification_pagination_invalid" in observed.check_codes
    assert observed.snapshot.verification_status == "unknown"


def test_empty_verification_pages_are_incomplete() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    responses = readers["responses"]
    assert isinstance(responses, dict)
    pages = responses["verification_pages"]
    assert isinstance(pages, dict)
    first_page = pages[""]
    second_page = pages["verification-page-2"]
    assert isinstance(first_page, dict) and isinstance(second_page, dict)
    first_page["items"] = []
    first_page["next_cursor"] = None
    second_page["items"] = []

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )

    assert "verification_public_observed" not in observed.check_codes
    assert observed.snapshot.verification_status == "unknown"


def test_every_current_automatic_check_key_must_have_a_verification() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    responses = readers["responses"]
    assert isinstance(responses, dict)
    results = responses["results"]
    assert isinstance(results, dict)
    criteria = results["criteria"]
    criteria[0]["check_keys"].append("check-c")

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
    )

    assert "verification_public_observed" not in observed.check_codes
    assert observed.snapshot.verification_status == "unknown"


def test_restore_checker_uses_public_candidate_and_can_produce_pass() -> None:
    snapshot = _snapshot(
        verification_id=None,
        verification_candidate_id=None,
        verification_candidate_hash=None,
        verification_status="unknown",
        accepted=False,
        acceptance_id=None,
        accepted_candidate_id=None,
        accepted_candidate_hash=None,
        restored_tree_hash="0" * 40,
        restore_check_passed=True,
    )
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    restore_calls: list[tuple[str, str]] = []

    def restore(
        _manifest: object, commit: str, tree: str
    ) -> RestoredTreeObservation:
        restore_calls.append((commit, tree))
        return RestoredTreeObservation("passed", tree)

    report = run_user_flow(
        manifest,
        snapshot=snapshot,
        evidence_kind="real_user_flow",
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
        restore_checker=restore,  # type: ignore[arg-type]
    )

    assert report.status == "PASS", report.check_codes
    assert "verification_bound_to_candidate" in report.check_codes
    assert "candidate_accepted" in report.check_codes
    assert "accepted_tree_restored" in report.check_codes
    assert restore_calls == [("f" * 40, "e" * 40)]


def test_snapshot_tree_conflict_blocks_restore_and_fails() -> None:
    snapshot = _snapshot(
        accepted_tree_hash="0" * 40,
        restored_tree_hash="0" * 40,
        restore_check_passed=True,
    )
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    restore_calls: list[str] = []

    def restore(
        _manifest: object, commit: str, tree: str
    ) -> RestoredTreeObservation:
        restore_calls.append(tree)
        return RestoredTreeObservation("passed", tree)

    report = run_user_flow(
        manifest,
        snapshot=snapshot,
        evidence_kind="real_user_flow",
        public_json_fetcher=readers["read_json"],  # type: ignore[arg-type]
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
        restore_checker=restore,  # type: ignore[arg-type]
    )

    assert report.status == "FAIL"
    assert report.failure_code == "accepted_tree_identity_mismatch"
    assert restore_calls == []


def test_public_result_identity_change_during_restore_fails() -> None:
    snapshot = _snapshot()
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
    )
    read_json = readers["read_json"]
    result_reads = 0

    def changed_after_restore(
        manifest_arg: dict[str, object], path: str
    ) -> dict[str, object] | None:
        nonlocal result_reads
        result = read_json(manifest_arg, path)  # type: ignore[operator]
        if path.endswith("/results"):
            result_reads += 1
            if result_reads == 2 and isinstance(result, dict):
                changed = dict(result)
                acceptance = result.get("acceptance")
                assert isinstance(acceptance, dict)
                changed["acceptance"] = {
                    **acceptance,
                    "acceptance_id": str(uuid4()),
                }
                return changed
        return result  # type: ignore[return-value]

    def restore(
        _manifest: object, _commit: str, tree: str
    ) -> RestoredTreeObservation:
        return RestoredTreeObservation("passed", tree)

    report = run_user_flow(
        manifest,
        snapshot=snapshot,
        evidence_kind="real_user_flow",
        public_json_fetcher=changed_after_restore,
        content_fetcher=readers["read_content"],  # type: ignore[arg-type]
        restore_checker=restore,  # type: ignore[arg-type]
    )

    assert report.status == "FAIL"
    assert report.failure_code == "public_result_identity_changed"


def test_user_flow_collector_does_not_use_snapshot_times_when_source_time_is_missing() -> None:
    snapshot = _snapshot(
        source_windows=(),
        dependency_satisfied=True,
    )
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
        source_b_finished_at=None,
    )

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],
        content_fetcher=readers["read_content"],
    )

    assert observed.snapshot.source_windows == (
        ExecutionWindow(
            execution_id=snapshot.source_execution_ids[0],
            started_at=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
            finished_at=datetime(2026, 9, 8, 10, 2, tzinfo=UTC),
        ),
    )
    assert observed.snapshot.dependency_satisfied is False
    assert "source_execution_facts_observed" not in observed.check_codes
    report = build_user_flow_evidence(
        _builder(),
        observed.snapshot,
        public_check_codes=observed.check_codes,
    )
    assert report.status == "INCOMPLETE"
    assert report.failure_code == "source_execution_facts_missing"


def test_user_flow_collector_rejects_downstream_start_before_inputs_finish() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=True)
    manifest, _calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 2, 30, tzinfo=UTC),
    )

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],
        content_fetcher=readers["read_content"],
    )

    assert observed.snapshot.dependency_satisfied is False
    assert "integration_after_selected_inputs" not in observed.check_codes
    report = build_user_flow_evidence(
        _builder(),
        observed.snapshot,
        public_check_codes=observed.check_codes,
    )
    assert report.status == "INCOMPLETE"
    assert report.failure_code == "dependency_fact_missing"


def test_user_flow_collector_uses_public_package_and_artifact_fallbacks() -> None:
    snapshot = _snapshot(source_windows=(), dependency_satisfied=False)
    manifest, calls, readers = _public_user_flow_fixture(
        snapshot,
        integration_started_at=datetime(2026, 9, 8, 10, 4, tzinfo=UTC),
        selection_embeds_refs=False,
    )

    observed = collect_user_flow_observation(
        manifest,
        snapshot,
        public_json_fetcher=readers["read_json"],
        content_fetcher=readers["read_content"],
    )

    assert observed.snapshot.dependency_satisfied is True, observed.check_codes
    assert "context_package_public_observed" in observed.check_codes
    assert "selected_artifacts_observed" in observed.check_codes
    assert any("/context-package?manifest_schema_version=3" in path for path in calls)
    assert sum("/gateway/artifacts?" in path for path in calls) == 2
