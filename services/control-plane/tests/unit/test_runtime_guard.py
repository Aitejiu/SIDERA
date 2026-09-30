from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread
from uuid import uuid4

import pytest
from sidera_runtime_guard import (
    BudgetRequest,
    RuntimeBudgetGuard,
    RuntimeGuardError,
    RuntimeRouteIdentity,
    prepare_run,
    route_binding_from_env,
)


def _manifest(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    run_id = str(uuid4())
    workspace_id = str(uuid4())
    run_root = tmp_path / "run"
    approval = run_root / "approval.json"
    approval.parent.mkdir()
    approval.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "approved": True,
                "run_id": run_id,
                "workspace_id": workspace_id,
                "implementation_fingerprint": "a" * 64,
                "runtime_fingerprint": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "schema_version": 2,
        "run_id": run_id,
        "workspace_id": workspace_id,
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "b" * 64,
        "run_root": str(run_root),
        "approval_ref": str(approval),
        "batch_limits": {
            "main_agent_turns": 2,
            "planner_runs": 2,
            "worker_sessions": 2,
            "adapter_prompts": 3,
        },
        "scenarios": {
            "profile": {
                "max_seconds": 10,
                "limits": {
                    "main_agent_turns": 1,
                    "planner_runs": 1,
                    "worker_sessions": 1,
                    "adapter_prompts": 2,
                },
            }
        },
        "allowed_routes": [
            {
                "route_id": "worker-local",
                "role": "worker",
                "runtime": "fake-acp",
                "provider": "fake",
                "model_id": "fake-v1",
                "endpoint_host": "127.0.0.1:9999",
                "credential_ref": "none",
                "config_version": 1,
                "fallbacks": [],
                "costs": {"worker_sessions": 1, "adapter_prompts": 1},
            },
            {
                "route_id": "main-local",
                "role": "main_agent",
                "runtime": "fake-http",
                "provider": "fake",
                "model_id": "fake-main-v1",
                "endpoint_host": "127.0.0.1:9998",
                "credential_ref": "none",
                "config_version": 1,
                "fallbacks": [],
                "costs": {"main_agent_turns": 1},
            },
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path, payload


def _request(path: Path, *, operation_id: str = "op-1", **changes: object) -> BudgetRequest:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    values: dict[str, object] = {
        "run_id": str(manifest["run_id"]),
        "workspace_id": str(manifest["workspace_id"]),
        "implementation_fingerprint": str(manifest["implementation_fingerprint"]),
        "runtime_fingerprint": str(manifest["runtime_fingerprint"]),
        "scenario": "profile",
        "role": "worker",
        "operation_id": operation_id,
        "resource_key": "execution:1",
        "route_id": "worker-local",
        "payload_hash": "c" * 64,
        "costs": {"worker_sessions": 1, "adapter_prompts": 1},
    }
    values.update(changes)
    return BudgetRequest(
        **values,
    )


def test_prepare_is_explicit_and_does_not_recreate_a_missing_ledger(
    tmp_path: Path,
) -> None:
    path, payload = _manifest(tmp_path)
    first = prepare_run(path)
    assert first["status"] == "prepared"
    ledger = Path(str(payload["run_root"])) / "acceptance-state.json"
    marker = Path(str(payload["run_root"])) / "acceptance-state.initialized.json"
    assert ledger.exists()
    assert marker.exists()
    guard = RuntimeBudgetGuard(path)
    reserved = guard.reserve(_request(path))
    assert reserved.status == "reserved"
    second = prepare_run(path)
    assert second["status"] == "existing"
    assert json.loads(ledger.read_text(encoding="utf-8"))["operations"]
    ledger.unlink()
    with pytest.raises(RuntimeGuardError, match="ledger_missing"):
        RuntimeBudgetGuard(path)


def test_prepare_requires_an_explicit_approval_receipt(tmp_path: Path) -> None:
    path, payload = _manifest(tmp_path)
    approval = Path(str(payload["approval_ref"]))
    approval.unlink()
    with pytest.raises(RuntimeGuardError, match="approval_missing"):
        prepare_run(path)


def test_begin_same_operation_sends_once_and_observe_is_fenced(
    tmp_path: Path,
) -> None:
    path, _ = _manifest(tmp_path)
    prepare_run(path)
    guard = RuntimeBudgetGuard(path)
    first = guard.begin(_request(path))
    second = guard.begin(_request(path))
    assert first.status == "send"
    assert second.status == "reconcile"
    assert first.deadline is not None
    guard.observe("op-1", outcome="completed", resource_ids=("session:1",))
    third = guard.begin(_request(path))
    assert third.status == "reconcile"
    assert third.resource_ids == ("session:1",)


def test_route_cost_override_can_charge_a_follow_up_prompt_without_new_session(
    tmp_path: Path,
) -> None:
    path, _ = _manifest(tmp_path)
    prepare_run(path)
    guard = RuntimeBudgetGuard(path)

    request = guard.build_request(
        scenario="profile",
        role="worker",
        operation_id="repair-op",
        resource_key="execution:1",
        payload_hash="c" * 64,
        route_id="worker-local",
        costs={"adapter_prompts": 1},
    )

    assert request.costs == {"adapter_prompts": 1}
    assert guard.begin(request).status == "send"


def test_cost_reservation_is_atomic_when_one_batch_limit_is_exhausted(
    tmp_path: Path,
) -> None:
    path, payload = _manifest(tmp_path)
    limits = dict(payload["batch_limits"])
    limits["adapter_prompts"] = 0
    payload["batch_limits"] = limits
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    prepare_run(path)
    guard = RuntimeBudgetGuard(path)
    decision = guard.reserve(_request(path))
    assert decision.status == "blocked"
    assert decision.code == "budget_exhausted"
    state = json.loads(
        (Path(str(payload["run_root"])) / "acceptance-state.json").read_text()
    )
    assert state["batch_counts"] == {
        "adapter_prompts": 0,
        "main_agent_turns": 0,
        "planner_runs": 0,
        "worker_sessions": 0,
    }
    assert state["operations"] == {}


def test_scenario_limit_denial_preserves_shared_budget_without_recording_call(
    tmp_path: Path,
) -> None:
    path, payload = _manifest(tmp_path)
    scenarios = dict(payload["scenarios"])
    profile = dict(scenarios["profile"])
    profile_limits = dict(profile["limits"])
    profile_limits["adapter_prompts"] = 1
    profile["limits"] = profile_limits
    scenarios["profile"] = profile
    payload["scenarios"] = scenarios
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    prepare_run(path)
    guard = RuntimeBudgetGuard(path)

    first = guard.begin(_request(path, operation_id="profile-source"))
    denied = guard.begin(_request(path, operation_id="profile-integration"))

    assert first.status == "send"
    assert denied.status == "blocked"
    assert denied.code == "budget_exhausted"
    state = json.loads(
        (Path(str(payload["run_root"])) / "acceptance-state.json").read_text()
    )
    assert state["scenario_counts"]["profile"]["adapter_prompts"] == 1
    assert state["batch_counts"]["adapter_prompts"] == 1
    assert set(state["operations"]) == {"profile-source"}


def test_profile_then_revision_share_the_same_batch_counter(tmp_path: Path) -> None:
    path, payload = _manifest(tmp_path)
    scenarios = dict(payload["scenarios"])
    scenarios["revision"] = {
        "max_seconds": 10,
        "limits": {
            "main_agent_turns": 1,
            "planner_runs": 1,
            "worker_sessions": 1,
            "adapter_prompts": 2,
        },
    }
    payload["scenarios"] = scenarios
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    prepare_run(path)
    guard = RuntimeBudgetGuard(path)

    profile = guard.begin(_request(path, operation_id="profile", costs={"adapter_prompts": 1}))
    revision_one = guard.begin(
        _request(path, operation_id="revision-1", scenario="revision", costs={"adapter_prompts": 1})
    )
    revision_two = guard.begin(
        _request(path, operation_id="revision-2", scenario="revision", costs={"adapter_prompts": 1})
    )
    revision_overflow = guard.begin(
        _request(path, operation_id="revision-3", scenario="revision", costs={"adapter_prompts": 1})
    )

    assert [profile.status, revision_one.status, revision_two.status] == [
        "send",
        "send",
        "send",
    ]
    assert revision_overflow.status == "blocked"
    assert revision_overflow.code == "budget_exhausted"
    state = json.loads(
        (Path(str(payload["run_root"])) / "acceptance-state.json").read_text()
    )
    assert state["scenario_counts"]["profile"]["adapter_prompts"] == 1
    assert state["scenario_counts"]["revision"]["adapter_prompts"] == 2
    assert state["batch_counts"]["adapter_prompts"] == 3
    assert set(state["operations"]) == {"profile", "revision-1", "revision-2"}


def test_route_and_operation_identity_are_not_replaceable(tmp_path: Path) -> None:
    path, _ = _manifest(tmp_path)
    prepare_run(path)
    guard = RuntimeBudgetGuard(path)
    request = _request(path)
    assert guard.begin(request).status == "send"
    with pytest.raises(RuntimeGuardError, match="operation_identity_conflict"):
        guard.begin(_request(path, payload_hash="d" * 64))
    with pytest.raises(RuntimeGuardError, match="route_not_allowed"):
        guard.reserve(
            _request(
                path,
                operation_id="op-2",
                route_id="unknown",
            )
        )


def test_actual_route_is_checked_field_by_field_without_recording_secrets(
    tmp_path: Path,
) -> None:
    path, _ = _manifest(tmp_path)
    prepare_run(path)
    guard = RuntimeBudgetGuard(path)
    actual = RuntimeRouteIdentity(
        runtime="fake-acp",
        provider="fake",
        model_id="fake-v1",
        endpoint_host="127.0.0.1:9999",
        credential_ref="none",
        config_version=1,
        fallbacks=(),
    )
    request = guard.build_request(
        scenario="profile",
        role="worker",
        operation_id="route-fields",
        resource_key="execution:1",
        payload_hash="c" * 64,
        route_id="worker-local",
        route_identity=actual,
    )
    assert guard.begin(request).status == "send"
    assert request.identity()["route_identity"] == {
        "runtime": "fake-acp",
        "provider": "fake",
        "endpoint_host": "127.0.0.1:9999",
        "model_id": "fake-v1",
        "credential_ref": "none",
        "config_version": 1,
        "fallbacks": [],
    }

    for changed in (
        {"provider": "other-provider"},
        {"credential_ref": "env:OTHER_KEY"},
        {"endpoint_host": "127.0.0.1:9998"},
        {"config_version": 2},
        {"fallbacks": ("native-hidden-fallback",)},
    ):
        candidate = RuntimeRouteIdentity(
            runtime=changed.get("runtime", actual.runtime),
            provider=changed.get("provider", actual.provider),
            model_id=changed.get("model_id", actual.model_id),
            endpoint_host=changed.get("endpoint_host", actual.endpoint_host),
            credential_ref=changed.get("credential_ref", actual.credential_ref),
            config_version=changed.get("config_version", actual.config_version),
            fallbacks=changed.get("fallbacks", actual.fallbacks),
        )
        with pytest.raises(RuntimeGuardError, match="route_configuration_mismatch"):
            guard.begin(
                guard.build_request(
                    scenario="profile",
                    role="worker",
                    operation_id=f"route-fields-{len(changed)}",
                    resource_key="execution:1",
                    payload_hash="c" * 64,
                    route_id="worker-local",
                    route_identity=candidate,
                )
            )


def test_route_binding_parser_rejects_inline_secret_and_does_not_echo_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-do-not-print"
    monkeypatch.setenv(
        "SIDERA_ACCEPTANCE_ROUTE_FACTS",
        json.dumps(
            {
                "worker": {
                    "route_id": "worker-local",
                    "runtime": "fake-acp",
                    "provider": "fake",
                    "model_id": "fake-v1",
                    "endpoint_host": "127.0.0.1:9999",
                    "credential_ref": secret,
                    "config_version": 1,
                    "fallbacks": [],
                }
            }
        ),
    )

    with pytest.raises(RuntimeGuardError, match="route_credential_ref_invalid") as error:
        route_binding_from_env("worker", route_id="worker-local", required=True)

    assert secret not in str(error.value)


def test_deadline_is_fixed_at_first_reservation(tmp_path: Path) -> None:
    path, _payload = _manifest(tmp_path)
    prepare_run(path)
    current = datetime(2026, 9, 18, tzinfo=UTC)
    guard = RuntimeBudgetGuard(path, clock=lambda: current)
    first = guard.reserve(_request(path))
    assert first.deadline == (current + timedelta(seconds=10)).isoformat()
    guard.observe("op-1", outcome="unknown")
    later = current + timedelta(seconds=11)
    expired = RuntimeBudgetGuard(path, clock=lambda: later).reserve(
        _request(path, operation_id="op-2")
    )
    assert expired.status == "blocked"
    assert expired.code == "deadline_exceeded"


def test_bindings_cannot_cross_groups_or_be_rebound(tmp_path: Path) -> None:
    path, _payload = _manifest(tmp_path)
    prepare_run(path)
    guard = RuntimeBudgetGuard(path)
    guard.bind_thread("thread:1", group="A")
    guard.bind_resource("thread:1", "planning_request:1")
    with pytest.raises(RuntimeGuardError, match="resource_binding_conflict"):
        guard.bind_resource("thread:2", "planning_request:1")
    guard.bind_thread("thread:1", group="A")
    with pytest.raises(RuntimeGuardError, match="thread_binding_conflict"):
        guard.bind_thread("thread:1", group="B")


def test_persisted_binding_mode_rejects_unbound_resource_and_fences_chain(
    tmp_path: Path,
) -> None:
    path, payload = _manifest(tmp_path)
    payload["resource_binding_mode"] = "persisted"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    prepare_run(path)
    guard = RuntimeBudgetGuard(path)
    chain = ("workspace:1", "planning_request:1", "execution:1")
    request = guard.build_request(
        scenario="profile",
        role="worker",
        operation_id="bound-op",
        resource_key="execution:1",
        resource_chain=chain,
        payload_hash="c" * 64,
        route_id="worker-local",
    )

    with pytest.raises(RuntimeGuardError, match="resource_binding_missing"):
        guard.begin(request)

    guard.bind_resource("planning_request:1", "execution:1", resource_chain=chain)
    assert guard.begin(request).status == "send"
    assert RuntimeBudgetGuard(path).begin(request).status == "reconcile"
    with pytest.raises(RuntimeGuardError, match="resource_binding_conflict"):
        guard.begin(
            guard.build_request(
                scenario="profile",
                role="worker",
                operation_id="bound-op",
                resource_key="execution:1",
                resource_chain=("workspace:1", "planning_request:1", "execution:2"),
                payload_hash="c" * 64,
                route_id="worker-local",
            )
        )


def test_corrupt_state_is_never_treated_as_a_zero_ledger(tmp_path: Path) -> None:
    path, payload = _manifest(tmp_path)
    prepare_run(path)
    ledger = Path(str(payload["run_root"])) / "acceptance-state.json"
    ledger.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeGuardError, match="ledger_invalid"):
        RuntimeBudgetGuard(path)


def test_two_processes_begin_same_operation_have_one_sender(tmp_path: Path) -> None:
    path, _payload = _manifest(tmp_path)
    prepare_run(path)
    child = """
import json
import sys
from pathlib import Path
from sidera_runtime_guard import BudgetRequest, RuntimeBudgetGuard

manifest_path = sys.argv[1]
manifest = json.loads(open(manifest_path, encoding="utf-8").read())
request = BudgetRequest(
    run_id=manifest["run_id"],
    workspace_id=manifest["workspace_id"],
    implementation_fingerprint=manifest["implementation_fingerprint"],
    runtime_fingerprint=manifest["runtime_fingerprint"],
    scenario="profile",
    role="worker",
    operation_id="same-operation",
    resource_key="execution:1",
    route_id="worker-local",
    payload_hash="c" * 64,
    costs={"worker_sessions": 1, "adapter_prompts": 1},
)
print(RuntimeBudgetGuard(Path(manifest_path)).begin(request).status)
"""
    barrier = Barrier(2)
    results: list[str] = []

    def launch() -> None:
        barrier.wait()
        completed = subprocess.run(
            [sys.executable, "-c", child, str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            results.append(f"error:{completed.stderr.strip()}")
        else:
            results.append(completed.stdout.strip())

    threads = [Thread(target=launch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(results) == ["reconcile", "send"], results


def test_full_basic_acceptance_manifest_can_be_loaded_by_the_guard(
    tmp_path: Path,
) -> None:
    path, payload = _manifest(tmp_path)
    payload.update(
        {
            "local_gate_record": str(tmp_path / "gate.json"),
            "repository": str(tmp_path / "repository"),
            "base_commit": "a" * 40,
            "database_ref": "env:SIDERA_TEST_DB",
            "knowledge_database_ref": "env:SIDERA_TEST_KNOWLEDGE_DB",
            "control_plane_url": "http://127.0.0.1:18111",
            "knowledge_url": "http://127.0.0.1:18112",
            "main_agent_gateway_url": "http://127.0.0.1:18113",
            "web_url": "http://127.0.0.1:18114",
            "fingerprint_version": 2,
            "profiles": [
                {
                    "role": role,
                    "runtime": "fake",
                    "provider": "fake",
                    "model_id": f"fake-{role}",
                    "endpoint_host": "127.0.0.1:18101",
                    "credential_ref": "none",
                    "config_ref": f"module:test/{role}",
                    "config_version": 1,
                }
                for role in ("main_agent", "planner", "worker", "second_worker")
            ],
        }
    )
    payload["scenarios"] = {
        "profile": {
            "max_seconds": 10,
            "main_agent_turns": 1,
            "planner_runs": 1,
            "worker_sessions": 1,
            "adapter_prompts": 2,
        }
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    prepare_run(path)
    guard = RuntimeBudgetGuard(path)

    assert guard.run_id == payload["run_id"]
    assert guard.build_request(
        scenario="profile",
        role="worker",
        operation_id="full-manifest-operation",
        resource_key="execution:1",
        payload_hash="c" * 64,
        route_id="worker-local",
    ).route_id == "worker-local"
