from __future__ import annotations

import os
import signal
import textwrap
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sys import executable
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.dsh_acp import (
    DshACPAdapter,
    DshACPAdapterError,
)
from sidera_control_plane.domain.context_packages import context_package_reference
from sidera_control_plane.domain.gateway import DeliveryOutcome
from sidera_control_plane.domain.workspace import ExecutionSpec
from tests.unit.test_context_package_v2 import _materialization_v1
from tests.unit.test_opencode_acp import _spec, _workspace

_HOST = textwrap.dedent(
    """
    import json
    import os
    import sys
    from pathlib import Path

    methods = []
    session_id = "dsh-test-session"

    def write(message):
        sys.stdout.write(json.dumps(message) + "\\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        request = json.loads(raw)
        method = request.get("method")
        methods.append(method)
        capture = os.environ.get("SIDERA_DSH_CAPTURE")
        if method == "initialize":
            write({"id": request["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": False},
            }})
        elif method == "session/new":
            write({"id": request["id"], "result": {"sessionId": session_id}})
        elif method == "session/prompt":
            prompt = request["params"]["prompt"][0]["text"]
            if capture:
                Path(capture).write_text(json.dumps({"methods": methods, "prompt": prompt}))
            write({"method": "session/update", "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "agent_message", "content": {"type": "text", "text": "committed"}},
            }})
            write({"id": request["id"], "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            break
    """
).strip()


def _adapter(tmp_path: Path, *, capture: Path | None = None) -> DshACPAdapter:
    env = {"PATH": os.environ.get("PATH", "")}
    if capture is not None:
        env["SIDERA_DSH_CAPTURE"] = str(capture)
    return DshACPAdapter(
        argv=(executable, "-c", _HOST),
        process_env=env,
        workspace_root=tmp_path,
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
    )


def _context_for(execution_id):
    base = _materialization_v1()
    return replace(
        base,
        metadata=replace(
            base.metadata,
            consumer_id=execution_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )


def _context_spec(spec: ExecutionSpec, context) -> ExecutionSpec:
    return replace(
        spec,
        schema_version=2,
        context_package_ref=context_package_reference(
            context.metadata.package_id, context.metadata.package_hash
        ),
        context_package=context,
    )


def test_dsh_probe_context_prompt_and_committed_event(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    adapter = _adapter(tmp_path, capture=capture)
    profile = adapter.probe({})
    assert profile.input.value == "queued"
    assert profile.resume.value == "same-process"
    assert profile.stop.value == "exact-execution"
    assert profile.stability.value == "experimental"
    assert profile.context_package_versions == (1, 2)
    assert profile.handoff_checkpoint_mode.value == "unsupported"
    assert profile.handoff_session_context_versions == ()
    assert profile.handoff_writer_gate_versions == ()

    spec = _spec()
    context = _context_for(spec.execution_id)
    spec = _context_spec(spec, context)
    workspace = _workspace(spec)
    (tmp_path / str(spec.execution_id)).mkdir()
    acknowledgement = adapter.accept_context(context, operation_id=uuid4())
    assert acknowledgement.package_hash == context.metadata.package_hash

    handle = adapter.start_session(spec, workspace, operation_id=uuid4())
    result = adapter.send_input(handle, "do the bounded work", (), operation_id=uuid4())
    assert result.outcome is DeliveryOutcome.DELIVERED
    assert capture.exists()
    captured = capture.read_text()
    assert "<sidera-context-json>" in captured
    assert "do the bounded work" in captured
    assert "session/set_model" not in captured
    assert "session/set_mode" not in captured
    events = adapter.read_events(handle)
    assert any(event.event_type == "progress.updated" for event in events)
    progress = next(event for event in events if event.event_type == "progress.updated")
    assert progress.payload["committed"] is True
    assert progress.payload["trace_category"] == "assistant_message"
    assert any(event.event_type == "input.accepted" for event in events)
    assert adapter.stop_session(handle, operation_id=uuid4()).outcome is DeliveryOutcome.DELIVERED


def test_dsh_context_expiry_hash_and_attachments_are_explicit_failures(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    spec = _spec()
    expired = _context_for(spec.execution_id)
    expired = replace(
        expired,
        metadata=replace(expired.metadata, expires_at=datetime.now(UTC) - timedelta(seconds=1)),
    )
    with pytest.raises(DshACPAdapterError, match="expired"):
        adapter.accept_context(expired, operation_id=uuid4())

    context = _context_for(spec.execution_id)
    adapter.accept_context(context, operation_id=uuid4())
    (tmp_path / str(spec.execution_id)).mkdir()
    handle = adapter.start_session(_context_spec(spec, context), _workspace(spec), operation_id=uuid4())
    result = adapter.send_input(handle, "input", ("artifact://missing-body",), operation_id=uuid4())
    assert result.outcome is DeliveryOutcome.FAILED
    assert result.failure_code == "attachment_body_unavailable"
    adapter.stop_session(handle, operation_id=uuid4())


def test_dsh_two_sessions_are_independent_and_cross_handle_is_rejected(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    first_spec = _spec()
    second_spec = replace(first_spec, execution_id=uuid4())
    (tmp_path / str(first_spec.execution_id)).mkdir()
    (tmp_path / str(second_spec.execution_id)).mkdir()
    first = adapter.start_session(first_spec, _workspace(first_spec), operation_id=uuid4())
    second = adapter.start_session(second_spec, _workspace(second_spec), operation_id=uuid4())

    mismatch = replace(first, execution_session_id=second.execution_session_id)
    with pytest.raises(DshACPAdapterError, match="unknown_dsh_session"):
        adapter.send_input(mismatch, "wrong session", (), operation_id=uuid4())
    assert adapter.send_input(second, "right session", (), operation_id=uuid4()).outcome is DeliveryOutcome.DELIVERED
    assert adapter.stop_session(first, operation_id=uuid4()).outcome is DeliveryOutcome.DELIVERED
    assert adapter.send_input(second, "still alive", (), operation_id=uuid4()).outcome is DeliveryOutcome.DELIVERED
    assert adapter.stop_session(second, operation_id=uuid4()).outcome is DeliveryOutcome.DELIVERED


def test_dsh_operation_id_replay_does_not_prompt_twice(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    adapter = _adapter(tmp_path, capture=capture)
    spec = _spec()
    (tmp_path / str(spec.execution_id)).mkdir()
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())
    operation_id = uuid4()
    first = adapter.send_input(handle, "one", (), operation_id=operation_id)
    repeated = adapter.send_input(handle, "different text", (), operation_id=operation_id)
    assert repeated == first
    assert capture.read_text().count("one") == 1
    adapter.stop_session(handle, operation_id=uuid4())


def test_dsh_lost_process_is_unknown_and_not_automatically_retried(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    spec = _spec()
    (tmp_path / str(spec.execution_id)).mkdir()
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())
    process = next(iter(adapter.active_processes()))
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=2)
    result = adapter.send_input(handle, "do not retry", (), operation_id=uuid4())
    assert result.outcome is DeliveryOutcome.UNKNOWN
    assert result.failure_code == "process_identity_unknown"
    assert adapter.observe_terminal(handle) is not None


def test_dsh_v3_and_handoff_operations_are_explicitly_unsupported(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    assert adapter.probe({}).context_package_versions == (1, 2)
    spec = _spec()
    (tmp_path / str(spec.execution_id)).mkdir()
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())
    with pytest.raises(DshACPAdapterError, match="checkpoint"):
        adapter.capture_handoff_checkpoint(
            handle,
            transition_id=uuid4(),
            source_input_cutoff_sequence=0,
            operation_id=uuid4(),
        )
    inspection = adapter.inspect_handoff_operation(handle, operation_id=uuid4())
    assert inspection.outcome.value == "unknown"
    with pytest.raises(DshACPAdapterError, match="writer"):
        adapter.enable_handoff_writer(handle, fencing_token=1, operation_id=uuid4())
    adapter.stop_session(handle, operation_id=uuid4())
