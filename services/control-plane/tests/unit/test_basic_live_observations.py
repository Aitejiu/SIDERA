from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from tests.e2e.basic_acceptance import runner as acceptance_runner
from tests.e2e.basic_acceptance.contracts import ConfirmedPlanEvidence
from tests.e2e.basic_acceptance.live_scenarios import run_closure_sample_check
from tests.e2e.basic_acceptance.observations import (
    LiveObservationError,
    ScenarioObservation,
    ensure_group_run,
    load_observation,
    record_group_run,
)
from tests.e2e.basic_acceptance.sample_case import prepare_sample_repository


def _manifest(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "b" * 64,
        "run_root": str(tmp_path / "run"),
    }


def _confirmed_plan(manifest: dict[str, object]) -> ConfirmedPlanEvidence:
    return ConfirmedPlanEvidence(
        run_id=UUID(str(manifest["run_id"])),
        workspace_id=UUID(str(manifest["workspace_id"])),
        main_agent_thread_id="thread-1",
        main_agent_turn_id="turn-1",
        planning_request_id=uuid4(),
        proposal_id=uuid4(),
        proposal_revision=1,
        proposal_content_hash="c" * 64,
        confirmation_command_id=uuid4(),
        root_task_id=uuid4(),
        confirmed_by="api-operator",
        pre_confirmation_status="proposal_ready",
        post_confirmation_status="materialized",
    )


@pytest.mark.parametrize("scenario", ("intervention", "revision"))
def test_missing_b_scenario_observation_is_not_treated_as_evidence(
    tmp_path: Path, scenario: str
) -> None:
    manifest = _manifest(tmp_path)

    with pytest.raises(LiveObservationError, match="observation_missing"):
        load_observation(manifest, scenario)

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    report = acceptance_runner.run_scenario(scenario, path, mode="real")
    assert report.status == "INCOMPLETE"
    assert report.check_codes == (f"{scenario}_snapshot_not_supplied",)


def test_observation_must_match_manifest_identity(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    path = Path(str(manifest["run_root"])) / "observations"
    path.mkdir(parents=True)
    payload = {
        "run_id": manifest["run_id"],
        "workspace_id": manifest["workspace_id"],
        "implementation_fingerprint": manifest["implementation_fingerprint"],
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "group": "A",
        "root_task_id": str(uuid4()),
        "status": "completed",
    }
    (path / "profile.json").write_text(json.dumps(payload), encoding="utf-8")
    assert load_observation(manifest, "profile")["group"] == "A"
    payload["runtime_fingerprint"] = "c" * 64
    (path / "profile.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LiveObservationError, match="observation_identity_mismatch"):
        load_observation(manifest, "profile")


@pytest.mark.parametrize(
    ("requested", "stored"),
    (
        ("result-inputs", "intervention"),
        ("intervention", "revision"),
        ("revision", "intervention"),
    ),
)
def test_scenario_observation_must_match_requested_scenario(
    tmp_path: Path, requested: str, stored: str
) -> None:
    manifest = _manifest(tmp_path)
    path = Path(str(manifest["run_root"])) / "observations"
    path.mkdir(parents=True)
    payload = {
        "run_id": manifest["run_id"],
        "workspace_id": manifest["workspace_id"],
        "implementation_fingerprint": manifest["implementation_fingerprint"],
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "scenario": stored,
        "snapshot": {},
    }
    (path / f"{requested}.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LiveObservationError, match="observation_scenario_mismatch"):
        load_observation(manifest, requested)


@pytest.mark.parametrize(
    "identity_field",
    ("run_id", "workspace_id", "implementation_fingerprint", "runtime_fingerprint"),
)
@pytest.mark.parametrize(
    "scenario",
    (
        "result-inputs",
        "result-integration",
        "user-flow",
        "intervention",
        "revision",
    ),
)
def test_every_scenario_observation_rejects_identity_drift(
    tmp_path: Path,
    identity_field: str,
    scenario: str,
) -> None:
    manifest = _manifest(tmp_path)
    path = Path(str(manifest["run_root"])) / "observations"
    path.mkdir(parents=True)
    payload = {
        "run_id": manifest["run_id"],
        "workspace_id": manifest["workspace_id"],
        "implementation_fingerprint": manifest["implementation_fingerprint"],
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "scenario": scenario,
        "snapshot": {"source": "public-api"},
    }
    (path / f"{scenario}.json").write_text(json.dumps(payload), encoding="utf-8")
    assert load_observation(manifest, scenario)["scenario"] == scenario

    payload[identity_field] = (
        str(uuid4()) if identity_field in {"run_id", "workspace_id"} else "d" * 64
    )
    (path / f"{scenario}.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LiveObservationError, match="observation_identity_mismatch"):
        load_observation(manifest, scenario)


def test_group_observation_is_api_bound_idempotent_and_private(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["implementation_fingerprint"] = "a" * 64
    manifest["runtime_fingerprint"] = "b" * 64
    confirmed = _confirmed_plan(manifest)

    first = record_group_run(manifest, confirmed, group="A")
    second = record_group_run(manifest, confirmed, group="A")

    assert first == second
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    loaded = ensure_group_run("A", manifest)
    assert loaded.root_task_id == confirmed.root_task_id
    assert loaded.command_ids == (confirmed.confirmation_command_id,)
    assert loaded.resource_ids == (
        f"planning_request:{confirmed.planning_request_id}",
        f"proposal:{confirmed.proposal_id}",
        f"root_task:{confirmed.root_task_id}",
    )


def test_group_observation_conflict_does_not_overwrite(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["implementation_fingerprint"] = "a" * 64
    manifest["runtime_fingerprint"] = "b" * 64
    first = _confirmed_plan(manifest)
    record_group_run(manifest, first, group="A")
    second = first.model_copy(update={"root_task_id": uuid4()})

    with pytest.raises(LiveObservationError, match="observation_conflict"):
        record_group_run(manifest, second, group="A")
    assert ensure_group_run("A", manifest).root_task_id == first.root_task_id


def test_group_observation_rejects_cross_group_file(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["implementation_fingerprint"] = "a" * 64
    manifest["runtime_fingerprint"] = "b" * 64
    path = record_group_run(manifest, _confirmed_plan(manifest), group="A")
    path.rename(path.with_name("group-B.json"))

    with pytest.raises(LiveObservationError, match="group_observation_group_mismatch"):
        ensure_group_run("B", manifest)


def test_fixed_checker_observation_records_hashes(tmp_path: Path) -> None:
    repository = tmp_path / "sample"
    prepare_sample_repository(repository)
    output = repository / "out"
    output.mkdir()
    (output / "source-a.json").write_text(
        '{"marker":"SIDERA_CLOSURE_SOURCE_A_V1","items":[{"id":"api","status":"done"},{"id":"ui","status":"queued"}]}',
        encoding="utf-8",
    )
    (output / "source-b.json").write_text(
        '{"marker":"SIDERA_CLOSURE_SOURCE_B_V1","items":[{"id":"tests","status":"running"},{"id":"docs","status":"blocked"}]}',
        encoding="utf-8",
    )
    (output / "summary.json").write_text(
        '{"total":4,"counts":{"done":1,"queued":1,"running":1,"blocked":1},"source_markers":["SIDERA_CLOSURE_SOURCE_A_V1","SIDERA_CLOSURE_SOURCE_B_V1"]}',
        encoding="utf-8",
    )
    result = run_closure_sample_check(repository, sidera_root=Path(__file__).parents[4])
    assert result["status"] == "PASS"
    assert len(str(result["checker_sha256"])) == 64
    assert len(str(result["command_sha256"])) == 64
    assert result["candidate_tree_hash_before"] == result["candidate_tree_hash_after"]
    assert result["candidate_tree_unchanged"] is True
    assert result["checker_exit_code"] == 0


def test_real_user_flow_runner_rehydrates_empty_fields_from_public_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    workspace_id = uuid4()
    root_task_id = uuid4()
    source_execution_ids = (uuid4(), uuid4())
    integration_execution_id = uuid4()
    source_session_ids = (uuid4(), uuid4())
    integration_session_id = uuid4()
    selection_id = uuid4()
    package_id = uuid4()
    candidate_id = uuid4()
    acceptance_id = uuid4()
    artifact_ids = (uuid4(), uuid4())
    verification_id = uuid4()
    repository = tmp_path / "restore-source"
    commit = prepare_sample_repository(repository)
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    candidate_hash = "c" * 64
    selection_hash = "d" * 64
    source_a, source_b = source_execution_ids
    source_session_a, source_session_b = source_session_ids
    artifact_bodies = {
        artifact_ids[0]: b"first selected source\n",
        artifact_ids[1]: b"second selected source\n",
    }
    selection = {
        "selection_id": str(selection_id),
        "workspace_id": str(workspace_id),
        "root_task_id": str(root_task_id),
        "run_id": str(run_id),
        "selection_hash": selection_hash,
        "groups": [
            {
                "sources": [
                    {
                        "effective_execution_id": str(source_a),
                        "artifacts": [{"artifact_id": str(artifact_ids[0])}],
                    },
                    {
                        "effective_execution_id": str(source_b),
                        "artifacts": [{"artifact_id": str(artifact_ids[1])}],
                    },
                ]
            }
        ],
        "hard_inputs": [],
        "consumers": [{"package_id": str(package_id)}],
    }
    package = {
        "package_id": str(package_id),
        "workspace_id": str(workspace_id),
        "status": "active",
        "materialized_at": "2026-09-23T10:03:00+00:00",
        "assembly": {"status": "ready"},
    }
    candidate = {
        "candidate_id": str(candidate_id),
        "candidate_hash": candidate_hash,
        "result_commit": commit,
        "result_tree": tree,
        "is_current": True,
    }
    acceptance = {
        "acceptance_id": str(acceptance_id),
        "candidate_id": str(candidate_id),
        "candidate_hash": candidate_hash,
    }
    result_overview = {
        "workspace_id": str(workspace_id),
        "root_task_id": str(root_task_id),
        "current_candidate": candidate,
        "acceptance": acceptance,
        "criteria": [
            {
                "criterion_key": "required-check",
                "mode": "automatic",
                "check_keys": ["check-a"],
                "verification_status": "passed",
                "observed_status": "passed",
                "verification_result_ids": [str(verification_id)],
            }
        ],
    }
    executions = {
        source_a: {
            "id": str(source_a),
            "workspace_id": str(workspace_id),
            "status": "succeeded",
            "started_at": "2026-09-23T10:00:00+00:00",
            "finished_at": "2026-09-23T10:02:00+00:00",
        },
        source_b: {
            "id": str(source_b),
            "workspace_id": str(workspace_id),
            "status": "succeeded",
            "started_at": "2026-09-23T10:01:00+00:00",
            "finished_at": "2026-09-23T10:03:00+00:00",
        },
        integration_execution_id: {
            "id": str(integration_execution_id),
            "workspace_id": str(workspace_id),
            "status": "succeeded",
            "started_at": "2026-09-23T10:04:00+00:00",
            "finished_at": "2026-09-23T10:05:00+00:00",
        },
    }
    session_rows = {
        source_session_a: (source_a, source_session_a),
        source_session_b: (source_b, source_session_b),
        integration_session_id: (integration_execution_id, integration_session_id),
    }
    verification_page = {
        "candidate_id": str(candidate_id),
        "items": [
            {
                "verification_id": str(verification_id),
                "candidate_id": str(candidate_id),
                "execution_id": str(integration_execution_id),
                "check_key": "check-a",
                "actual_commit": commit,
                "actual_tree": tree,
                "status": "passed",
            }
        ],
        "next_cursor": None,
        "last_change_cursor": 0,
    }
    api_calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        api_calls.append((request.method, request.url.path))
        path = request.url.path
        if "/collaboration/input-selections/" in path:
            return httpx.Response(200, json=selection)
        if "/context-packages/" in path:
            return httpx.Response(200, json=package)
        if path.endswith("/results"):
            return httpx.Response(200, json=result_overview)
        if path.endswith("/verifications"):
            return httpx.Response(200, json=verification_page)
        if path.startswith("/api/v1/executions/"):
            execution_id = UUID(path.rsplit("/", maxsplit=1)[-1])
            return httpx.Response(200, json=executions[execution_id])
        if path.endswith("/sessions"):
            session_id = UUID(str(request.url.params["session_id"]))
            execution_id, _ = session_rows[session_id]
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": str(session_id),
                            "workspace_id": str(workspace_id),
                            "execution_id": str(execution_id),
                        }
                    ]
                },
            )
        if path.endswith("/events"):
            session_id = UUID(str(request.url.params["session_id"]))
            execution_id, _ = session_rows[session_id]
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": str(uuid4()),
                            "workspace_id": str(workspace_id),
                            "execution_id": str(execution_id),
                            "session_id": str(session_id),
                        }
                    ]
                },
            )
        if "/artifacts/" in path and path.endswith("/descriptor"):
            artifact_id = UUID(path.split("/artifacts/", maxsplit=1)[1].split("/", maxsplit=1)[0])
            body = artifact_bodies[artifact_id]
            return httpx.Response(
                200,
                json={
                    "content_available": True,
                    "content_hash": hashlib.sha256(body).hexdigest(),
                    "byte_size": len(body),
                },
            )
        if "/artifacts/" in path and path.endswith("/content"):
            artifact_id = UUID(path.split("/artifacts/", maxsplit=1)[1].split("/", maxsplit=1)[0])
            return httpx.Response(200, content=artifact_bodies[artifact_id])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original_client = httpx.Client

    def local_client(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(acceptance_runner.httpx, "Client", local_client)
    manifest = {
        "run_id": str(run_id),
        "workspace_id": str(workspace_id),
        "root_task_id": str(root_task_id),
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "b" * 64,
        "code_commit": "f" * 40,
        "run_root": str(tmp_path / "run"),
        "repository": str(repository),
        "control_plane_url": "http://sidera.invalid",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    snapshot = {
        "run_id": str(run_id),
        "workspace_id": str(workspace_id),
        "root_task_id": str(root_task_id),
        "source_execution_ids": [str(source_a), str(source_b)],
        "source_session_ids": [str(source_session_a), str(source_session_b)],
        "integration_execution_id": str(integration_execution_id),
        "integration_session_id": str(integration_session_id),
        "input_selection_id": str(selection_id),
        "input_selection_hash": selection_hash,
        "candidate_id": str(candidate_id),
        "candidate_hash": candidate_hash,
        "candidate_selection_hash": selection_hash,
        "verification_id": None,
        "verification_candidate_id": None,
        "verification_candidate_hash": None,
        "verification_status": "unknown",
        "acceptance_id": str(acceptance_id),
        "accepted_candidate_id": str(candidate_id),
        "accepted_candidate_hash": candidate_hash,
        "accepted": True,
        "accepted_tree_hash": tree,
        "restored_tree_hash": None,
        "restore_check_passed": False,
        "dependency_satisfied": False,
        "source_windows": [],
    }
    observation = ScenarioObservation(
        run_id=run_id,
        workspace_id=workspace_id,
        implementation_fingerprint="a" * 64,
        runtime_fingerprint="b" * 64,
        scenario="user-flow",
        snapshot=snapshot,
    )
    observation_path = Path(str(manifest["run_root"])) / "observations"
    observation_path.mkdir(parents=True)
    (observation_path / "user-flow.json").write_text(
        json.dumps(observation.model_dump(mode="json")), encoding="utf-8"
    )

    report = acceptance_runner.run_scenario("user-flow", manifest_path, mode="real")

    assert report.status == "PASS", report.check_codes
    assert "verification_bound_to_candidate" in report.check_codes
    assert "accepted_tree_restored" in report.check_codes
    assert "integration_dependency_satisfied" in report.check_codes
    assert len(api_calls) > 0
    assert all(method == "GET" for method, _path in api_calls)
    assert any(path.endswith("/verifications") for _method, path in api_calls)
