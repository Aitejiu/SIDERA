from __future__ import annotations

import json
import stat
import subprocess
import sys
import textwrap
import threading
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sidera_runtime_guard import (
    RuntimeBudgetGuard,
    RuntimeRouteIdentity,
    prepare_run,
)

from sidera_control_plane.acceptance_resource_binding import PersistedResourceFacts
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.adapters.opencode_planner import (
    OpenCodePlannerAdapter,
    OpenCodePlannerError,
)
from sidera_control_plane.application import worker_gateway as worker_gateway_module
from sidera_control_plane.application.worker_gateway import WorkerGateway
from sidera_control_plane.domain.gateway import DeliveryOutcome
from sidera_control_plane.domain.modules import LOCAL_VERIFICATION_PROVIDER
from tests.integration.test_opencode_handoff_contract import (
    _context_package as _handoff_context_package,
)
from tests.integration.test_opencode_handoff_contract import (
    _fake_handoff_acp,
)
from tests.integration.test_opencode_handoff_contract import (
    _spec as _handoff_spec,
)
from tests.integration.test_opencode_handoff_contract import (
    _workspace as _handoff_workspace,
)
from tests.unit.test_opencode_acp import _spec, _workspace
from tests.unit.test_opencode_planner import (
    _planner_input,
    _proposal_payload,
    _response,
    _run_control,
)

pytestmark = pytest.mark.integration


def _planner_manifest(tmp_path: Path, *, planner_runs: int) -> Path:
    run_id = str(uuid4())
    workspace_id = str(uuid4())
    run_root = tmp_path / "run"
    approval = run_root / "approval.json"
    run_root.mkdir()
    approval.write_text(
        json.dumps(
            {
                "approved": True,
                "run_id": run_id,
                "workspace_id": workspace_id,
                "implementation_fingerprint": "a" * 64,
                "runtime_fingerprint": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "workspace_id": workspace_id,
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "b" * 64,
        "run_root": str(run_root),
        "approval_ref": str(approval),
        "resource_binding_mode": "persisted",
        "batch_limits": {
            "main_agent_turns": 0,
            "planner_runs": planner_runs,
            "worker_sessions": 0,
            "adapter_prompts": planner_runs,
        },
        "scenarios": {
            "profile": {
                "max_seconds": 60,
                "limits": {
                    "main_agent_turns": 0,
                    "planner_runs": planner_runs,
                    "worker_sessions": 0,
                    "adapter_prompts": planner_runs,
                },
            }
        },
        "allowed_routes": [
            {
                "route_id": "planner-fake",
                "role": "planner",
                "runtime": "fake-acp",
                "provider": "fake",
                "model_id": "fake-planner-v1",
                "endpoint_host": "127.0.0.1:18102",
                "credential_ref": "none",
                "config_version": 1,
                "fallbacks": [],
                "costs": {"planner_runs": 1, "adapter_prompts": 1},
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _fake_planner_process(tmp_path: Path, capture: Path) -> Path:
    executable = tmp_path / "fake-planner"
    executable.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path

            capture = Path({str(capture)!r})
            methods = []
            prompt_count = 0

            def record(method):
                methods.append(method)
                capture.write_text(json.dumps(methods), encoding="utf-8")

            for raw in __import__("sys").stdin:
                request = json.loads(raw)
                method = request.get("method")
                record(method)
                if method == "initialize":
                    print(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": {{
                        "protocolVersion": 1,
                        "agentCapabilities": {{"loadSession": False}}
                    }}}}), flush=True)
                elif method == "session/new":
                    print(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": {{
                        "sessionId": "planner-session"
                    }}}}), flush=True)
                elif method == "session/prompt":
                    prompt_count += 1
                    response = os.environ.get("SIDERA_TEST_PLANNER_RESPONSE", "{{}}")
                    if prompt_count == 1 and os.environ.get("SIDERA_TEST_PLANNER_INVALID_FIRST") == "1":
                        response = "not-json"
                    print(json.dumps({{"jsonrpc": "2.0", "method": "session/update", "params": {{
                        "sessionId": "planner-session",
                        "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": response}}}}
                    }}}}), flush=True)
                    print(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": {{
                        "stopReason": "end_turn"
                    }}}}), flush=True)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _route_facts() -> str:
    return json.dumps(
        {
            "planner": {
                "route_id": "planner-fake",
                "runtime": "fake-acp",
                "provider": "fake",
                "model_id": "fake-planner-v1",
                "endpoint_host": "127.0.0.1:18102",
                "credential_ref": "none",
                "config_version": 1,
                "fallbacks": [],
            }
        },
        separators=(",", ":"),
    )


def _worker_manifest(
    tmp_path: Path,
    *,
    worker_sessions: int,
    adapter_prompts: int | None = None,
) -> Path:
    path = _planner_manifest(tmp_path, planner_runs=0)
    payload = json.loads(path.read_text(encoding="utf-8"))
    prompt_limit = worker_sessions if adapter_prompts is None else adapter_prompts
    payload["batch_limits"] = {
        "main_agent_turns": 0,
        "planner_runs": 0,
        "worker_sessions": worker_sessions,
        "adapter_prompts": prompt_limit,
    }
    payload["scenarios"]["profile"]["limits"] = dict(payload["batch_limits"])
    payload["allowed_routes"] = [
        {
            "route_id": "worker-fake",
            "role": "worker",
            "runtime": "fake-acp",
            "provider": "fake",
            "model_id": "fake-worker-v1",
            "endpoint_host": "127.0.0.1:18103",
            "credential_ref": "none",
            "config_version": 1,
            "fallbacks": [],
            "costs": {"worker_sessions": 1, "adapter_prompts": 1},
        },
        {
            "route_id": "second-worker-fake",
            "role": "second_worker",
            "runtime": "fake-acp",
            "provider": "fake",
            "model_id": "fake-second-worker-v1",
            "endpoint_host": "127.0.0.1:18103",
            "credential_ref": "none",
            "config_version": 1,
            "fallbacks": [],
            "costs": {"adapter_prompts": 1},
        },
    ]
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _worker_route_facts() -> str:
    return json.dumps(
        {
            "worker": {
                "route_id": "worker-fake",
                "runtime": "fake-acp",
                "provider": "fake",
                "model_id": "fake-worker-v1",
                "endpoint_host": "127.0.0.1:18103",
                "credential_ref": "none",
                "config_version": 1,
                "fallbacks": [],
            },
            "second_worker": {
                "route_id": "second-worker-fake",
                "runtime": "fake-acp",
                "provider": "fake",
                "model_id": "fake-second-worker-v1",
                "endpoint_host": "127.0.0.1:18103",
                "credential_ref": "none",
                "config_version": 1,
                "fallbacks": [],
            },
        },
        separators=(",", ":"),
    )


def _bind_planning_request(
    guard: RuntimeBudgetGuard,
    planner_input,
):
    chain = (
        f"workspace:{guard.workspace_id}",
        f"planning_request:{planner_input.planning_request_id}",
    )
    guard.bind_resource(chain[0], chain[1], resource_chain=chain)
    return replace(planner_input, acceptance_resource_chain=chain)


def test_planner_budget_zero_blocks_before_acp_process_receives_a_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _planner_manifest(tmp_path, planner_runs=0)
    prepare_run(manifest_path)
    guard = RuntimeBudgetGuard(manifest_path)
    planner_input, _ = _planner_input()
    planner_input = _bind_planning_request(guard, planner_input)
    capture = tmp_path / "methods.json"
    executable = _fake_planner_process(tmp_path, capture)
    monkeypatch.setenv("SIDERA_ACCEPTANCE_ROUTE_FACTS", _route_facts())
    monkeypatch.setenv("SIDERA_ACCEPTANCE_PLANNER_ROUTE_ID", "planner-fake")

    adapter = OpenCodePlannerAdapter(
        executable=str(executable),
        temp_root=tmp_path / "planner-runs",
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
        acceptance_guard=guard,
    )

    with pytest.raises(OpenCodePlannerError, match="budget_exhausted"):
        adapter.plan(
            planner_input,
            _run_control(planner_input.operation_id),
        )

    assert not capture.exists()


def test_expired_planner_deadline_blocks_before_acp_process_receives_a_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _planner_manifest(tmp_path, planner_runs=2)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["batch_limits"]["planner_runs"] = 1
    payload["batch_limits"]["adapter_prompts"] = 2
    payload["scenarios"]["profile"]["limits"]["planner_runs"] = 1
    payload["scenarios"]["profile"]["limits"]["adapter_prompts"] = 2
    manifest_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    prepare_run(manifest_path)
    base = datetime.now(UTC)
    seeded_guard = RuntimeBudgetGuard(manifest_path, clock=lambda: base)
    planner_input, _ = _planner_input()
    planner_input = _bind_planning_request(seeded_guard, planner_input)
    seed_request = seeded_guard.build_request(
        scenario="profile",
        role="planner",
        operation_id="deadline-seed",
        resource_key=f"planning_request:{planner_input.planning_request_id}",
        payload_hash="c" * 64,
        route_id="planner-fake",
        resource_chain=planner_input.acceptance_resource_chain,
        route_identity=RuntimeRouteIdentity(
            runtime="fake-acp",
            provider="fake",
            endpoint_host="127.0.0.1:18102",
            model_id="fake-planner-v1",
            credential_ref="none",
            config_version=1,
            fallbacks=(),
        ),
    )
    assert seeded_guard.reserve(seed_request).status == "reserved"

    capture = tmp_path / "methods.json"
    executable = _fake_planner_process(tmp_path, capture)
    monkeypatch.setenv("SIDERA_ACCEPTANCE_ROUTE_FACTS", _route_facts())
    monkeypatch.setenv("SIDERA_ACCEPTANCE_PLANNER_ROUTE_ID", "planner-fake")
    expired_guard = RuntimeBudgetGuard(
        manifest_path,
        clock=lambda: base + timedelta(seconds=61),
    )
    adapter = OpenCodePlannerAdapter(
        executable=str(executable),
        temp_root=tmp_path / "planner-runs",
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
        acceptance_guard=expired_guard,
    )

    with pytest.raises(OpenCodePlannerError, match="deadline_exceeded"):
        adapter.plan(planner_input, _run_control(planner_input.operation_id))

    assert not capture.exists()


def test_planner_initial_and_repair_prompts_are_recorded_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _planner_manifest(tmp_path, planner_runs=2)
    prepare_run(manifest_path)
    guard = RuntimeBudgetGuard(manifest_path)
    planner_input, _ = _planner_input()
    planner_input = _bind_planning_request(guard, planner_input)
    capture = tmp_path / "methods.json"
    executable = _fake_planner_process(tmp_path, capture)
    monkeypatch.setenv("SIDERA_ACCEPTANCE_ROUTE_FACTS", _route_facts())
    monkeypatch.setenv("SIDERA_ACCEPTANCE_PLANNER_ROUTE_ID", "planner-fake")
    monkeypatch.setenv("SIDERA_TEST_PLANNER_INVALID_FIRST", "1")
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", _proposal_payload().to_mapping()),
    )

    result = OpenCodePlannerAdapter(
        executable=str(executable),
        temp_root=tmp_path / "planner-runs",
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
        acceptance_guard=guard,
    ).plan(planner_input, _run_control(planner_input.operation_id))

    assert result.__class__.__name__ == "PlannerProposalResult"
    assert json.loads(capture.read_text(encoding="utf-8")) == [
        "initialize",
        "session/new",
        "session/prompt",
        "session/prompt",
    ]
    state = json.loads(
        (tmp_path / "run" / "acceptance-state.json").read_text(encoding="utf-8")
    )
    assert {
        key: value["state"]
        for key, value in state["operations"].items()
    } == {
        f"{planner_input.operation_id}:prompt:0": "completed",
        f"{planner_input.operation_id}:repair:1": "completed",
    }


def test_worker_session_and_prompt_reach_the_fake_acp_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _worker_manifest(tmp_path, worker_sessions=2)
    prepare_run(manifest_path)
    guard = RuntimeBudgetGuard(manifest_path)
    spec = replace(_spec(), workspace_id=UUID(guard.workspace_id))
    workspace = _workspace(spec)
    (tmp_path / str(spec.execution_id)).mkdir()
    capture = tmp_path / "worker-methods.json"
    executable = _fake_planner_process(tmp_path, capture)
    monkeypatch.setenv("SIDERA_ACCEPTANCE_ROUTE_FACTS", _worker_route_facts())
    monkeypatch.setenv("SIDERA_ACCEPTANCE_WORKER_ROUTE_ID", "worker-fake")
    monkeypatch.setenv(
        "SIDERA_ACCEPTANCE_SECOND_WORKER_ROUTE_ID", "second-worker-fake"
    )

    class Uow:
        session = object()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            del exc_type, exc_value, traceback

    gateway = WorkerGateway.__new__(WorkerGateway)
    gateway._acceptance_guard = guard
    gateway._uow_factory = lambda: Uow()
    facts = PersistedResourceFacts(
        workspace_id=spec.workspace_id,
        planning_request_id=uuid4(),
        planning_kind="create",
        root_task_id=spec.task_id,
        task_id=spec.task_id,
        execution_id=spec.execution_id,
        handoff_transition_id=None,
    )
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        workspace_root=tmp_path,
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
    )

    with patch.object(
        worker_gateway_module,
        "load_execution_resource_facts",
        return_value=facts,
    ):
        session_operation = uuid4()
        guarded_session = gateway._begin_acceptance_worker_call(
            operation_id=session_operation,
            resource_key=f"execution:{spec.execution_id}",
            payload="session",
            boundary="session",
        )
        handle = adapter.start_session(spec, workspace, operation_id=session_operation)
        gateway._observe_acceptance_worker_call(
            guarded_session,
            outcome="completed",
            resource_ids=(str(handle.execution_session_id),),
        )

        prompt_operation = uuid4()
        guarded_prompt = gateway._begin_acceptance_worker_call(
            operation_id=prompt_operation,
            resource_key=f"execution:{spec.execution_id}",
            payload="prompt",
            boundary="prompt",
        )
        delivery = adapter.send_input(
            handle,
            "bounded prompt",
            (),
            operation_id=prompt_operation,
        )
        assert delivery.outcome is DeliveryOutcome.DELIVERED
        gateway._observe_acceptance_worker_call(
            guarded_prompt,
            outcome="completed",
            resource_ids=(
                str(handle.execution_session_id),
                f"sequence:{delivery.accepted_sequence}",
            ),
        )
        adapter.stop_session(handle, operation_id=uuid4())

    assert json.loads(capture.read_text(encoding="utf-8"))[:3] == [
        "initialize",
        "session/new",
        "session/prompt",
    ]


def test_local_verification_session_does_not_consume_adapter_prompt_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _worker_manifest(
        tmp_path,
        worker_sessions=1,
        adapter_prompts=0,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["resource_binding_mode"] = "legacy"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    prepare_run(manifest_path)
    guard = RuntimeBudgetGuard(manifest_path)
    monkeypatch.setenv("SIDERA_ACCEPTANCE_WORKER_SESSION_ROUTE_ID", "worker-fake")

    gateway = WorkerGateway.__new__(WorkerGateway)
    gateway._acceptance_guard = guard
    operation_id = uuid4()

    guarded_operation = gateway._begin_acceptance_worker_call(
        operation_id=operation_id,
        resource_key=f"execution:{uuid4()}",
        payload="local verification session",
        boundary="session",
        provider=LOCAL_VERIFICATION_PROVIDER,
    )
    gateway._observe_acceptance_worker_call(guarded_operation, outcome="completed")

    state = json.loads(
        (tmp_path / "run" / "acceptance-state.json").read_text(encoding="utf-8")
    )
    assert state["batch_counts"]["worker_sessions"] == 1
    assert state["batch_counts"]["adapter_prompts"] == 0


def test_worker_intervention_and_handoff_prompts_reach_the_fake_acp_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _worker_manifest(
        tmp_path,
        worker_sessions=2,
        adapter_prompts=6,
    )
    prepare_run(manifest_path)
    guard = RuntimeBudgetGuard(manifest_path)
    monkeypatch.setenv("SIDERA_ACCEPTANCE_ROUTE_FACTS", _worker_route_facts())
    monkeypatch.setenv("SIDERA_ACCEPTANCE_WORKER_ROUTE_ID", "worker-fake")
    monkeypatch.setenv(
        "SIDERA_ACCEPTANCE_SECOND_WORKER_ROUTE_ID", "second-worker-fake"
    )
    executable, log_path, count_path = _fake_handoff_acp(tmp_path)
    monkeypatch.setenv("SIDERA_TEST_ACP_LOG", str(log_path))
    monkeypatch.setenv("SIDERA_TEST_ACP_COUNT", str(count_path))

    class Uow:
        session = object()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            del exc_type, exc_value, traceback

    gateway = WorkerGateway.__new__(WorkerGateway)
    gateway._acceptance_guard = guard
    gateway._uow_factory = lambda: Uow()
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        workspace_root=tmp_path,
        request_timeout_seconds=2,
        handoff_request_timeout_seconds=2,
        stop_grace_seconds=0.2,
    )

    source_execution_id = uuid4()
    source_spec = replace(
        _handoff_spec(
            source_execution_id,
            fencing_token=3,
            context_package=_handoff_context_package(source_execution_id),
        ),
        workspace_id=UUID(guard.workspace_id),
    )
    source_workspace = _handoff_workspace(source_spec, tmp_path)
    target_execution_id = uuid4()
    target_spec = replace(
        _handoff_spec(
            target_execution_id,
            fencing_token=8,
            context_package=_handoff_context_package(
                target_execution_id,
                handoff_instruction="continue from the transferred context",
            ),
        ),
        workspace_id=UUID(guard.workspace_id),
    )
    target_workspace = _handoff_workspace(target_spec, tmp_path)

    facts_by_execution = {
        source_execution_id: PersistedResourceFacts(
            workspace_id=source_spec.workspace_id,
            planning_request_id=uuid4(),
            planning_kind="create",
            root_task_id=source_spec.task_id,
            task_id=source_spec.task_id,
            execution_id=source_execution_id,
            handoff_transition_id=None,
        ),
        target_execution_id: PersistedResourceFacts(
            workspace_id=target_spec.workspace_id,
            planning_request_id=uuid4(),
            planning_kind="create",
            root_task_id=target_spec.task_id,
            task_id=target_spec.task_id,
            execution_id=target_execution_id,
            handoff_transition_id=None,
        ),
    }

    with patch.object(
        worker_gateway_module,
        "load_execution_resource_facts",
        side_effect=lambda _session, *, execution_id, workspace_id: facts_by_execution[
            execution_id
        ],
    ):
        source_session_operation = uuid4()
        guarded_source_session = gateway._begin_acceptance_worker_call(
            operation_id=source_session_operation,
            resource_key=f"execution:{source_execution_id}",
            payload="source-session",
            boundary="session",
        )
        source_handle = adapter.start_session(
            source_spec,
            source_workspace,
            operation_id=source_session_operation,
        )
        gateway._observe_acceptance_worker_call(
            guarded_source_session,
            outcome="completed",
            resource_ids=(str(source_handle.execution_session_id),),
        )

        initial_operation = uuid4()
        guarded_initial = gateway._begin_acceptance_worker_call(
            operation_id=initial_operation,
            resource_key=f"execution:{source_execution_id}",
            payload="initial-prompt",
            boundary="prompt",
        )
        initial = adapter.send_input(
            source_handle,
            "initial worker prompt",
            (),
            operation_id=initial_operation,
        )
        assert initial.outcome is DeliveryOutcome.DELIVERED
        gateway._observe_acceptance_worker_call(
            guarded_initial,
            outcome="completed",
            resource_ids=(
                str(source_handle.execution_session_id),
                f"sequence:{initial.accepted_sequence}",
            ),
        )

        checkpoint_operation = uuid4()
        guarded_checkpoint = gateway._begin_acceptance_worker_call(
            operation_id=checkpoint_operation,
            resource_key=f"execution:{source_execution_id}",
            payload="handoff-checkpoint",
            boundary="prompt",
        )
        checkpoint = adapter.capture_handoff_checkpoint(
            source_handle,
            transition_id=uuid4(),
            source_input_cutoff_sequence=initial.accepted_sequence or 0,
            operation_id=checkpoint_operation,
        )
        gateway._observe_acceptance_worker_call(
            guarded_checkpoint,
            outcome="completed",
            resource_ids=(
                str(source_handle.execution_session_id),
                f"sequence:{checkpoint.accepted_sequence}",
            ),
        )
        adapter.stop_session(source_handle, operation_id=uuid4())

        target_session_operation = uuid4()
        guarded_target_session = gateway._begin_acceptance_worker_call(
            operation_id=target_session_operation,
            resource_key=f"execution:{target_execution_id}",
            payload="target-session",
            boundary="session",
        )
        target_handle = adapter.start_session(
            target_spec,
            target_workspace,
            operation_id=target_session_operation,
        )
        gateway._observe_acceptance_worker_call(
            guarded_target_session,
            outcome="completed",
            resource_ids=(str(target_handle.execution_session_id),),
        )

        enable_operation = uuid4()
        guarded_enable = gateway._begin_acceptance_worker_call(
            operation_id=enable_operation,
            resource_key=f"execution:{target_execution_id}",
            payload="handoff-writer-enable",
            boundary="prompt",
        )
        enabled = adapter.enable_handoff_writer(
            target_handle,
            fencing_token=target_spec.fencing_token,
            operation_id=enable_operation,
        )
        assert enabled.outcome is DeliveryOutcome.DELIVERED
        gateway._observe_acceptance_worker_call(
            guarded_enable,
            outcome="completed",
            resource_ids=(
                str(target_handle.execution_session_id),
                f"sequence:{enabled.accepted_sequence}",
            ),
        )

        intervention_operation = uuid4()
        guarded_intervention = gateway._begin_acceptance_worker_call(
            operation_id=intervention_operation,
            resource_key=f"execution:{target_execution_id}",
            payload="intervention-prompt",
            boundary="prompt",
        )
        intervention = adapter.send_input(
            target_handle,
            "continue after intervention",
            (),
            operation_id=intervention_operation,
        )
        assert intervention.outcome is DeliveryOutcome.DELIVERED
        gateway._observe_acceptance_worker_call(
            guarded_intervention,
            outcome="completed",
            resource_ids=(
                str(target_handle.execution_session_id),
                f"sequence:{intervention.accepted_sequence}",
            ),
        )
        adapter.stop_session(target_handle, operation_id=uuid4())

    state = json.loads(
        (tmp_path / "run" / "acceptance-state.json").read_text(encoding="utf-8")
    )
    assert len(state["operations"]) == 6
    assert {value["state"] for value in state["operations"].values()} == {
        "completed"
    }
    messages = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    prompts = [
        message
        for message in messages
        if message.get("method") == "session/prompt"
    ]
    assert len(prompts) == 4
    assert any(
        "SIDERA_HANDOFF_CHECKPOINT_V1" in json.dumps(prompt)
        for prompt in prompts
    )
    assert any("SIDERA_HANDOFF_RESUME_V1" in json.dumps(prompt) for prompt in prompts)


def test_two_processes_share_one_fake_http_send_and_payload_conflict_is_fenced(
    tmp_path: Path,
) -> None:
    manifest_path = _planner_manifest(tmp_path, planner_runs=2)
    prepare_run(manifest_path)
    guard = RuntimeBudgetGuard(manifest_path)
    planning_request_id = uuid4()
    chain = (
        f"workspace:{guard.workspace_id}",
        f"planning_request:{planning_request_id}",
    )
    guard.bind_resource(chain[0], chain[1], resource_chain=chain)
    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            received.append(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        child = textwrap.dedent(
            """
            import sys
            import urllib.request
            from pathlib import Path
            from sidera_runtime_guard import RuntimeBudgetGuard, RuntimeRouteIdentity

            manifest = Path(sys.argv[1])
            endpoint = sys.argv[2]
            operation_id = sys.argv[3]
            payload_hash = sys.argv[4]
            guard = RuntimeBudgetGuard(manifest)
            request = guard.build_request(
                scenario="profile",
                role="planner",
                operation_id=operation_id,
                resource_key=sys.argv[5],
                payload_hash=payload_hash,
                route_id="planner-fake",
                resource_chain=tuple(sys.argv[6].split(",")),
                route_identity=RuntimeRouteIdentity(
                    runtime="fake-acp",
                    provider="fake",
                    endpoint_host="127.0.0.1:18102",
                    model_id="fake-planner-v1",
                    credential_ref="none",
                    config_version=1,
                    fallbacks=(),
                ),
            )
            try:
                decision = guard.begin(request)
                print(decision.status, flush=True)
                if decision.status == "send":
                    urllib.request.urlopen(
                        urllib.request.Request(endpoint, data=b"planner"),
                        timeout=5,
                    ).read()
            except Exception as exc:
                print(f"error:{getattr(exc, 'code', type(exc).__name__)}", flush=True)
            """
        )
        arguments = [
            sys.executable,
            "-c",
            child,
            str(manifest_path),
            f"http://127.0.0.1:{server.server_port}/call",
            "same-operation",
            "c" * 64,
            f"planning_request:{planning_request_id}",
            ",".join(chain),
        ]
        first = subprocess.Popen(arguments, stdout=subprocess.PIPE, text=True)
        second = subprocess.Popen(arguments, stdout=subprocess.PIPE, text=True)
        first_output = first.communicate(timeout=10)[0].strip()
        second_output = second.communicate(timeout=10)[0].strip()
        assert sorted((first_output, second_output)) == ["reconcile", "send"]
        assert len(received) == 1

        conflict = subprocess.run(
            [*arguments[:5], "same-operation", "d" * 64, arguments[7], arguments[8]],
            check=False,
            capture_output=True,
            text=True,
        )
        assert conflict.stdout.strip() == "error:operation_identity_conflict"
        assert len(received) == 1
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_unknown_outcome_is_not_refunded_or_replayed_to_fake_http(
    tmp_path: Path,
) -> None:
    manifest_path = _planner_manifest(tmp_path, planner_runs=2)
    prepare_run(manifest_path)
    guard = RuntimeBudgetGuard(manifest_path)
    planning_request_id = uuid4()
    chain = (
        f"workspace:{guard.workspace_id}",
        f"planning_request:{planning_request_id}",
    )
    guard.bind_resource(chain[0], chain[1], resource_chain=chain)
    identity = RuntimeRouteIdentity(
        runtime="fake-acp",
        provider="fake",
        endpoint_host="127.0.0.1:18102",
        model_id="fake-planner-v1",
        credential_ref="none",
        config_version=1,
        fallbacks=(),
    )
    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["content-length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = guard.build_request(
            scenario="profile",
            role="planner",
            operation_id="unknown-operation",
            resource_key=f"planning_request:{planning_request_id}",
            payload_hash="c" * 64,
            route_id="planner-fake",
            resource_chain=chain,
            route_identity=identity,
        )
        assert guard.begin(request).status == "send"
        urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/call",
                data=b"planner",
            ),
            timeout=5,
        ).read()
        guard.observe("unknown-operation", outcome="unknown")

        assert guard.begin(request).status == "reconcile"
        assert len(received) == 1
        state = json.loads(
            (tmp_path / "run" / "acceptance-state.json").read_text(
                encoding="utf-8"
            )
        )
        assert state["batch_counts"]["planner_runs"] == 1
        assert state["batch_counts"]["adapter_prompts"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
