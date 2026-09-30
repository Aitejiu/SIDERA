from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import UUID, uuid4

import httpx
import pytest

from tests.e2e.basic_acceptance import runner as acceptance_runner
from tests.e2e.basic_acceptance.client import BasicAcceptanceClient
from tests.e2e.basic_acceptance.contracts import ConfirmedPlanEvidence, ScenarioEvidence
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.observations import record_group_run
from tests.e2e.basic_acceptance.run_state import (
    record_observation,
    reserve_action,
)
from tests.e2e.basic_acceptance.runner import run_scenario

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000101")


def _envelope(command_id: UUID) -> dict[str, object]:
    return {
        "command_id": str(command_id),
        "workspace_id": str(WORKSPACE_ID),
        "command_type": "CreateTask",
        "target_type": "task",
        "actor": {"type": "test", "id": "basic-acceptance"},
        "target_id": None,
        "expected_version": None,
        "idempotency_key": f"basic-{command_id}",
        "correlation_id": str(command_id),
        "payload": {
            "title": "fixture",
            "description": "fixture",
            "acceptance_criteria": ["fixture"],
            "priority": 0,
        },
    }


def test_disconnect_does_not_invent_a_second_command() -> None:
    command_id = uuid4()
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            payload = json.loads(request.content)
            sent.append(payload["command_id"])
            raise httpx.ReadError("connection closed after commit", request=request)
        return httpx.Response(
            200,
            json={
                "command_id": str(command_id),
                "status": "applied",
                "result": {
                    "command_id": str(command_id),
                    "aggregate_id": str(uuid4()),
                    "aggregate_version": 1,
                    "event_ids": [],
                    "data": {},
                },
            },
        )

    with BasicAcceptanceClient(
        "http://control-plane.test",
        workspace_id=WORKSPACE_ID,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.command(_envelope(command_id))

    assert sent == [str(command_id)]
    assert result.status == "observed"


def test_conflict_returns_current_version_without_error_body() -> None:
    secret = "provider-body-must-not-be-recorded"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"current_version": 7, "detail": secret},
        )

    with BasicAcceptanceClient(
        "http://control-plane.test",
        workspace_id=WORKSPACE_ID,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.command(_envelope(uuid4()))

    assert result.status == "conflict"
    assert result.current_version == 7
    assert secret not in result.diagnostic_code
    assert secret not in repr(result)


def test_skip_is_not_real_pass(tmp_path: Path) -> None:
    evidence = EvidenceBuilder(
        run_id=UUID("10000000-0000-0000-0000-000000000102"),
        scenario="user-flow",
        evidence_kind="real_user_flow",
        code_commit="a" * 40,
        runtime_fingerprint="b" * 64,
    ).finish(real_tests=0, skipped=1)

    assert isinstance(evidence, ScenarioEvidence)
    assert evidence.status == "NOT_RUN"
    assert evidence.failure_code == "real_tests_not_observed"


def test_timeout_is_bounded_and_contains_only_a_diagnostic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private provider response", request=request)

    with BasicAcceptanceClient(
        "http://control-plane.test",
        workspace_id=WORKSPACE_ID,
        transport=httpx.MockTransport(handler),
        max_retries=1,
    ) as client:
        result = client.get_json("/healthz")

    assert result.status == "incomplete"
    assert result.diagnostic_code == "transport_timeout"
    assert "private provider" not in repr(result)


def test_run_state_reservation_is_atomic_across_subprocesses(tmp_path: Path) -> None:
    manifest = {
        "run_id": "10000000-0000-0000-0000-000000000103",
        "run_root": str(tmp_path / "run"),
        "scenarios": {
            "user-flow": {
                "main_agent_turns": 1,
                "planner_runs": 1,
                "worker_sessions": 0,
                "adapter_prompts": 0,
                "max_seconds": 60,
            }
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    child = """
import json, sys
from tests.e2e.basic_acceptance.run_state import reserve_action
manifest = json.loads(open(sys.argv[1], encoding='utf-8').read())
result = reserve_action(manifest, 'user-flow', 'main_agent', sys.argv[2])
print(json.dumps({'status': result.status, 'code': result.diagnostic_code}))
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child, str(manifest_path), f"op-{index}"],
            cwd=Path(__file__).parents[2],
            stdout=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    outputs = [json.loads(process.communicate(timeout=5)[0]) for process in processes]

    assert sorted(item["status"] for item in outputs) == ["blocked", "reserved"]


def test_run_state_reopen_preserves_operation_and_deadline(tmp_path: Path) -> None:
    manifest = {
        "run_id": "10000000-0000-0000-0000-000000000104",
        "run_root": str(tmp_path / "run"),
        "scenarios": {
            "recovery": {
                "main_agent_turns": 1,
                "planner_runs": 0,
                "worker_sessions": 0,
                "adapter_prompts": 0,
                "max_seconds": 60,
            }
        },
    }
    first = reserve_action(manifest, "recovery", "main_agent", "same-op")
    record_observation("same-op", "unknown", ("resource-1",), manifest=manifest)
    replay = reserve_action(manifest, "recovery", "main_agent", "same-op")

    assert first.status == "reserved"
    assert replay.status == "replay"
    assert replay.deadline == first.deadline
    assert replay.resource_ids == ("resource-1",)


def test_check_scenario_does_not_create_a_session(tmp_path: Path) -> None:
    manifest = {
        "run_id": "10000000-0000-0000-0000-000000000105",
        "runtime_fingerprint": "c" * 64,
        "run_root": str(tmp_path / "run"),
        "scenarios": {"profile": {"max_seconds": 60}},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    evidence = run_scenario("profile", path, mode="check")

    assert evidence.status == "NOT_RUN"
    assert evidence.failure_code == "check_mode_no_business_session"
    assert not (tmp_path / "run" / "acceptance-state.json").exists()


def test_real_scenarios_without_observation_are_explicitly_incomplete(
    tmp_path: Path,
) -> None:
    manifest = {
        "run_id": "10000000-0000-0000-0000-000000000106",
        "runtime_fingerprint": "c" * 64,
        "run_root": str(tmp_path / "run"),
        "scenarios": {
            "profile": {"max_seconds": 60},
            "result-inputs": {"max_seconds": 60},
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    profile = run_scenario("profile", path, mode="real")
    result_inputs = run_scenario("result-inputs", path, mode="real")

    assert profile.status == "INCOMPLETE"
    assert profile.failure_code == "profile_observation_missing"
    assert result_inputs.status == "INCOMPLETE"
    assert result_inputs.failure_code == "result_inputs_real_evidence_not_supplied"


def test_real_profile_identity_only_observation_is_incomplete(
    tmp_path: Path,
) -> None:
    run_id = uuid4()
    workspace_id = uuid4()
    planning_request_id = uuid4()
    proposal_id = uuid4()
    command_id = uuid4()
    root_task_id = uuid4()
    manifest = {
        "run_id": str(run_id),
        "workspace_id": str(workspace_id),
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "c" * 64,
        "run_root": str(tmp_path / "run"),
        "scenarios": {"profile": {"max_seconds": 60}},
    }
    confirmed_plan = ConfirmedPlanEvidence(
        run_id=run_id,
        workspace_id=workspace_id,
        main_agent_thread_id="thread-profile",
        main_agent_turn_id="turn-profile",
        planning_request_id=planning_request_id,
        proposal_id=proposal_id,
        proposal_revision=1,
        proposal_content_hash="b" * 64,
        confirmation_command_id=command_id,
        root_task_id=root_task_id,
        confirmed_by="test-operator",
        pre_confirmation_status="proposal_ready",
        post_confirmation_status="materialized",
    )
    record_group_run(manifest, confirmed_plan, group="A")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    evidence = run_scenario("profile", path, mode="real")

    assert evidence.status == "INCOMPLETE"
    assert evidence.failure_code == "profile_execution_terminal_missing"
    assert evidence.root_task_id == root_task_id
    assert evidence.command_ids == (command_id,)
    assert "profile_group_observation_observed" in evidence.check_codes


def test_real_profile_passes_only_with_complete_group_a_observation(
    tmp_path: Path,
) -> None:
    run_id = uuid4()
    workspace_id = uuid4()
    root_task_id = uuid4()
    command_id = uuid4()
    source_execution_ids = (uuid4(), uuid4())
    integration_execution_id = uuid4()
    all_execution_ids = (*source_execution_ids, integration_execution_id)
    manifest = {
        "run_id": str(run_id),
        "workspace_id": str(workspace_id),
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "c" * 64,
        "run_root": str(tmp_path / "run"),
        "scenarios": {"profile": {"max_seconds": 60}},
    }
    observation_path = Path(str(manifest["run_root"])) / "observations"
    observation_path.mkdir(parents=True)
    (observation_path / "group-A.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": str(run_id),
                "workspace_id": str(workspace_id),
                "implementation_fingerprint": "a" * 64,
                "runtime_fingerprint": "c" * 64,
                "group": "A",
                "root_task_id": str(root_task_id),
                "command_ids": [str(command_id)],
                "source_execution_ids": [str(value) for value in source_execution_ids],
                "integration_execution_id": str(integration_execution_id),
                "terminal_execution_ids": [str(value) for value in all_execution_ids],
                "released_lease_execution_ids": [str(value) for value in all_execution_ids],
                "released_worker_ids": [str(uuid4()), str(uuid4())],
                "result_candidate_id": str(uuid4()),
                "result_accepted": True,
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    evidence = run_scenario("profile", path, mode="real")

    assert evidence.status == "PASS"
    assert evidence.failure_code is None
    assert evidence.execution_ids == all_execution_ids


def test_result_observation_reads_bytes_and_facts_from_public_readers(
    tmp_path: Path,
) -> None:
    run_id = uuid4()
    workspace_id = uuid4()
    selection_id = uuid4()
    context_package_id = uuid4()
    source_execution_id = uuid4()
    downstream_execution_id = uuid4()
    root_task_id = uuid4()
    candidate_id = uuid4()
    acceptance_id = uuid4()
    artifact_id = uuid4()
    downstream_artifact_id = uuid4()
    artifact_body = b"source artifact"
    downstream_body = b"summary: RESULT_MARKER"
    manifest = {
        "run_id": str(run_id),
        "workspace_id": str(workspace_id),
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "c" * 64,
        "run_root": str(tmp_path / "run"),
        "control_plane_url": "http://control-plane.test",
    }
    snapshot = {
        "root_task_id": str(root_task_id),
        "selection_id": str(selection_id),
        "selection_hash": "a" * 64,
        "expected_selection_hash": "forged-expected-hash",
        "bundle_id": str(uuid4()),
        "context_package_id": str(context_package_id),
        "context_package_hash": "c" * 64,
        "expected_context_package_hash": "forged-expected-context-hash",
        "selected_artifact_hashes": [
            [str(artifact_id), hashlib.sha256(artifact_body).hexdigest()]
        ],
        "artifact_bodies": [[str(artifact_id), "forged body must be ignored"]],
        "source_execution_ids": [str(source_execution_id)],
        "downstream_execution_id": str(downstream_execution_id),
        "expected_downstream_execution_id": str(downstream_execution_id),
        "downstream_artifact_id": str(downstream_artifact_id),
        "downstream_marker": "RESULT_MARKER",
        "real_session_ids": [str(uuid4())],
        "candidate_id": str(candidate_id),
        "candidate_hash": "d" * 64,
        "acceptance_id": str(acceptance_id),
        "accepted_candidate_id": str(candidate_id),
        "accepted_candidate_hash": "d" * 64,
        "budget_state": "completed",
        "session_state": "succeeded",
    }
    observation_path = Path(str(manifest["run_root"])) / "observations"
    observation_path.mkdir(parents=True)
    (observation_path / "result-inputs.json").write_text(
        json.dumps(
            {
                "run_id": str(run_id),
                "workspace_id": str(workspace_id),
                "implementation_fingerprint": "a" * 64,
                "runtime_fingerprint": "c" * 64,
                "scenario": "result-inputs",
                "snapshot": snapshot,
            }
        ),
        encoding="utf-8",
    )

    def read_json(_manifest: dict[str, object], path: str) -> dict[str, object] | None:
        if path.endswith(f"/collaboration/input-selections/{selection_id}"):
            return {"selection_id": str(selection_id), "selection_hash": "a" * 64}
        if path.endswith(f"/context-packages/{context_package_id}"):
            return {"package_id": str(context_package_id), "package_hash": "c" * 64}
        if path.endswith(f"/tasks/{root_task_id}/results"):
            return {
                "current_candidate": {
                    "candidate_id": str(candidate_id),
                    "candidate_hash": "d" * 64,
                },
                "acceptance": {
                    "acceptance_id": str(acceptance_id),
                    "candidate_id": str(candidate_id),
                    "candidate_hash": "d" * 64,
                },
            }
        if path.split("?", 1)[0].endswith(f"/executions/{source_execution_id}"):
            return {"id": str(source_execution_id), "status": "succeeded"}
        if path.split("?", 1)[0].endswith(
            f"/executions/{downstream_execution_id}"
        ):
            return {"id": str(downstream_execution_id), "status": "succeeded"}
        return None

    def read_bytes(_manifest: dict[str, object], artifact: UUID) -> bytes | None:
        return {
            artifact_id: artifact_body,
            downstream_artifact_id: downstream_body,
        }.get(artifact)

    observation = acceptance_runner._result_worker_observation(
        manifest,
        "result-inputs",
        public_json_fetcher=read_json,
        content_fetcher=read_bytes,
    )

    assert observation is not None
    assert observation.artifact_bodies == ((str(artifact_id), artifact_body),)
    assert observation.downstream_body == downstream_body
    assert observation.expected_selection_hash == "a" * 64
    assert observation.expected_context_package_hash == "c" * 64
    assert observation.expected_result_candidate_id == candidate_id
    assert observation.expected_acceptance_id == acceptance_id


def test_result_observation_does_not_trust_snapshot_bodies(tmp_path: Path) -> None:
    manifest = {
        "run_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "c" * 64,
        "run_root": str(tmp_path / "run"),
        "control_plane_url": "http://control-plane.test",
    }
    observation_path = Path(str(manifest["run_root"])) / "observations"
    observation_path.mkdir(parents=True)
    artifact_id = uuid4()
    body = b"not evidence"
    snapshot = {
        "selected_artifact_hashes": [[str(artifact_id), hashlib.sha256(body).hexdigest()]],
        "artifact_bodies": [[str(artifact_id), body.decode()]],
    }
    (observation_path / "result-inputs.json").write_text(
        json.dumps(
            {
                "run_id": manifest["run_id"],
                "workspace_id": manifest["workspace_id"],
                "implementation_fingerprint": "a" * 64,
                "runtime_fingerprint": "c" * 64,
                "scenario": "result-inputs",
                "snapshot": snapshot,
            }
        ),
        encoding="utf-8",
    )

    observation = acceptance_runner._result_worker_observation(
        manifest,
        "result-inputs",
        public_json_fetcher=lambda _manifest, _path: None,
        content_fetcher=lambda _manifest, _artifact: None,
    )

    assert observation is not None
    assert observation.artifact_bodies == ()
    assert observation.downstream_body is None


def test_result_observation_uses_artifact_id_not_a_snapshot_path(
    tmp_path: Path,
) -> None:
    run_id = uuid4()
    workspace_id = uuid4()
    artifact_id = uuid4()
    artifact_body = b"api artifact body"
    manifest = {
        "run_id": str(run_id),
        "workspace_id": str(workspace_id),
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "c" * 64,
        "run_root": str(tmp_path / "run"),
    }
    snapshot = {
        "selected_artifact_hashes": [
            [
                {
                    "artifact_id": str(artifact_id),
                    "path": "wrong/out/source-a.json",
                },
                hashlib.sha256(artifact_body).hexdigest(),
            ]
        ],
        "artifact_bodies": [["wrong/out/source-a.json", "forged"]],
    }
    observation_path = Path(str(manifest["run_root"])) / "observations"
    observation_path.mkdir(parents=True)
    (observation_path / "result-inputs.json").write_text(
        json.dumps(
            {
                "run_id": str(run_id),
                "workspace_id": str(workspace_id),
                "implementation_fingerprint": "a" * 64,
                "runtime_fingerprint": "c" * 64,
                "scenario": "result-inputs",
                "snapshot": snapshot,
            }
        ),
        encoding="utf-8",
    )
    seen_artifacts: list[UUID] = []

    def read_bytes(_manifest: dict[str, object], requested: UUID) -> bytes | None:
        seen_artifacts.append(requested)
        return artifact_body if requested == artifact_id else None

    observation = acceptance_runner._result_worker_observation(
        manifest,
        "result-inputs",
        public_json_fetcher=lambda _manifest, _path: None,
        content_fetcher=read_bytes,
    )

    assert observation is not None
    assert seen_artifacts == [artifact_id]
    assert observation.artifact_bodies == ((str(artifact_id), artifact_body),)


def test_result_observation_with_missing_public_artifact_is_unverified(
    tmp_path: Path,
) -> None:
    run_id = uuid4()
    workspace_id = uuid4()
    artifact_id = uuid4()
    manifest = {
        "run_id": str(run_id),
        "workspace_id": str(workspace_id),
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "c" * 64,
        "run_root": str(tmp_path / "run"),
    }
    observation_path = Path(str(manifest["run_root"])) / "observations"
    observation_path.mkdir(parents=True)
    (observation_path / "result-inputs.json").write_text(
        json.dumps(
            {
                "run_id": str(run_id),
                "workspace_id": str(workspace_id),
                "implementation_fingerprint": "a" * 64,
                "runtime_fingerprint": "c" * 64,
                "scenario": "result-inputs",
                "snapshot": {
                    "selected_artifact_hashes": [
                        [str(artifact_id), "d" * 64]
                    ],
                    "artifact_bodies": [[str(artifact_id), "forged"]],
                },
            }
        ),
        encoding="utf-8",
    )

    observation = acceptance_runner._result_worker_observation(
        manifest,
        "result-inputs",
        public_json_fetcher=lambda _manifest, _path: None,
        content_fetcher=lambda _manifest, _artifact: None,
    )

    assert observation is not None
    assert observation.selected_artifact_hashes == ((str(artifact_id), "d" * 64),)
    assert observation.artifact_bodies == ()


def test_result_observation_rejects_another_run_before_public_reads(
    tmp_path: Path,
) -> None:
    manifest = {
        "run_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "c" * 64,
        "run_root": str(tmp_path / "run"),
    }
    observation_path = Path(str(manifest["run_root"])) / "observations"
    observation_path.mkdir(parents=True)
    (observation_path / "result-inputs.json").write_text(
        json.dumps(
            {
                "run_id": str(uuid4()),
                "workspace_id": manifest["workspace_id"],
                "implementation_fingerprint": manifest["implementation_fingerprint"],
                "runtime_fingerprint": manifest["runtime_fingerprint"],
                "scenario": "result-inputs",
                "snapshot": {"selected_artifact_hashes": []},
            }
        ),
        encoding="utf-8",
    )

    def unexpected_read(*_args: object) -> None:
        raise AssertionError("cross-run read must stop first")

    assert (
        acceptance_runner._result_worker_observation(
            manifest,
            "result-inputs",
            public_json_fetcher=unexpected_read,
            content_fetcher=unexpected_read,
        )
        is None
    )


def test_acceptance_http_readers_bypass_system_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_body = b"verified local artifact"
    workspace_id = uuid4()
    artifact_id = uuid4()
    descriptor_path = (
        f"/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/descriptor"
    )
    content_path = (
        f"/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/content"
    )
    direct_requests: list[str] = []
    proxy_requests: list[str] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            direct_requests.append(self.path)
            if self.path == descriptor_path:
                body = json.dumps(
                    {
                        "content_available": True,
                        "content_hash": hashlib.sha256(artifact_body).hexdigest(),
                        "byte_size": len(artifact_body),
                    }
                ).encode()
                content_type = "application/json"
            elif self.path == content_path:
                body = artifact_body
                content_type = "application/octet-stream"
            else:
                body = b'{"status":"ready"}'
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            proxy_requests.append(self.path)
            body = b"local requests must not use the proxy"
            self.send_response(502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    servers = (target, proxy)
    threads = tuple(
        Thread(target=server.serve_forever, daemon=True) for server in servers
    )
    for thread in threads:
        thread.start()

    target_url = f"http://127.0.0.1:{target.server_port}"
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    for name in ("ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)

    try:
        manifest: dict[str, object] = {
            "control_plane_url": target_url,
            "workspace_id": str(workspace_id),
        }
        metadata = acceptance_runner._fetch_public_json(manifest, "/metadata")
        assert proxy_requests == []
        assert metadata == {
            "status": "ready"
        }
        assert acceptance_runner._fetch_artifact_content(manifest, artifact_id) == artifact_body
        with BasicAcceptanceClient(target_url) as client:
            assert client.get_json("/client").body == {"status": "ready"}
        assert proxy_requests == []
        assert direct_requests == [
            "/metadata",
            descriptor_path,
            content_path,
            "/client",
        ]
    finally:
        for server in servers:
            server.shutdown()
        for thread in threads:
            thread.join(timeout=2)
        for server in servers:
            server.server_close()
