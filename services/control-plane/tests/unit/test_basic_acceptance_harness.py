from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from tests.e2e.basic_acceptance.client import BasicAcceptanceClient
from tests.e2e.basic_acceptance.contracts import ScenarioEvidence
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
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
    assert profile.failure_code == "profile_real_evidence_not_supplied"
    assert result_inputs.status == "INCOMPLETE"
    assert result_inputs.failure_code == "result_inputs_real_evidence_not_supplied"
