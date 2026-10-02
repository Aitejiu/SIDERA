from __future__ import annotations

import json
import os
import stat
import sys
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Thread
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sidera_agents.contracts import (
    AssembledInput,
    NativeCloseState,
    NativeOpenRequest,
    NativeOutcomeStatus,
    NativeProcessState,
    NativeSkill,
    ProtocolState,
    ResourceRef,
    RuntimeCandidate,
    RuntimeRef,
    RuntimeSnapshot,
    SupportStatus,
    ToolConnection,
)
from sidera_agents.errors import AgentError
from sidera_agents.harnesses.opencode import OpenCodeHarness
from sidera_agents.harnesses.registry import default_harness_registry

_PEER = Path(__file__).parents[1] / "fixtures" / "acp_peer.py"


def _launcher(
    tmp_path: Path,
    name: str,
    *,
    capture: Path | None = None,
    http: bool = False,
    mode: str | None = None,
) -> Path:
    launcher = tmp_path / name
    assignments = []
    if capture:
        assignments.append(
            f"os.environ['ACME_TEST_CAPTURE'] = {str(capture)!r}\n"
            f"open({str(capture)!r}, 'a', encoding='utf-8').write(json.dumps({{'kind': 'launcher', 'pid': os.getpid()}}) + '\\n')\n"
        )
    if http:
        assignments.append("os.environ['ACME_TEST_HTTP'] = '1'\n")
    if mode:
        assignments.append(f"os.environ['ACME_TEST_MODE'] = {mode!r}\n")
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, runpy, sys\n"
        + "".join(assignments)
        + f"sys.argv = [{str(_PEER)!r}, *sys.argv[1:]]\n"
        + f"runpy.run_path({str(_PEER)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher


def _runtime(executable: Path, *, http: bool = False) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        ref=RuntimeRef(workspace_id=uuid4(), runtime_id=uuid4()),
        harness_id="opencode",
        executable=str(executable),
        actual_version="9.8.7",
        installed=True,
        enabled=True,
        version=1,
        protocol_state=ProtocolState.READY,
        auth_state="unconfirmed",
        capabilities={
            "acp": {
                "status": "supported",
                "reason_code": "initialize_v1",
                "constraints": {},
            },
            "mcp": {
                "status": "supported",
                "reason_code": "acp_v1_stdio",
                "constraints": {},
            },
            "mcp_http": {
                "status": "supported" if http else "unsupported",
                "reason_code": "negotiated_http" if http else "http_not_advertised",
                "constraints": {},
            },
        },
        checked_at="2026-10-02T00:00:00Z",
        limits={"max_active": 2, "max_sessions": 2},
    )


def _request(
    executable: Path,
    workspace: Path,
    *,
    tools: tuple[ToolConnection, ...] = (),
    skills: tuple[NativeSkill, ...] = (),
    environment: dict[str, object] | None = None,
    http: bool = False,
) -> NativeOpenRequest:
    return NativeOpenRequest(
        run_id=uuid4(),
        runtime=_runtime(executable, http=http),
        projected_config={},
        workspace={
            "directory": str(workspace),
            "writable": True,
            "artifact_directory": None,
        },
        tools=tools,
        skills=skills,
        environment=environment or {},
    )


def _records(capture: Path) -> list[dict[str, object]]:
    if not capture.exists():
        return []
    return [json.loads(line) for line in capture.read_text().splitlines()]


def _request_record(capture: Path, method: str) -> dict[str, object]:
    return next(
        row
        for row in _records(capture)
        if row.get("kind") == "request" and row.get("method") == method
    )


def test_owned_stdio_acp_peer_receives_authorized_mcp_and_dispatches_tools(
    tmp_path, monkeypatch
):
    capture = tmp_path / "peer.jsonl"
    mcp_capture = tmp_path / "mcp.json"
    tool_executable = _launcher(tmp_path, "mcp-tool")
    tool_executable.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"sys.argv = [{str(_PEER)!r}, 'mcp', *sys.argv[1:]]\n"
        f"runpy.run_path({str(_PEER)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    tool_executable.chmod(tool_executable.stat().st_mode | stat.S_IXUSR)
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    config_dir = home / ".config" / "opencode"
    config_dir.mkdir(parents=True)
    global_config = config_dir / "opencode.json"
    global_auth = config_dir / "auth.json"
    global_config.write_bytes(b'{"mcp":{"unapproved":true}}\n')
    global_auth.write_bytes(b"synthetic-auth-bytes\n")
    original_files = (global_config.read_bytes(), global_auth.read_bytes())
    token = "N06_TOOL_SECRET_SENTINEL"
    tool = ToolConnection(
        name="task-bridge",
        transport="stdio",
        endpoint_or_argv=(str(tool_executable), str(mcp_capture), "arg with spaces"),
        headers={},
        environment={"TOOL_TOKEN": SecretStr(token)},
    )
    monkeypatch.setenv("SIDERA_PARENT_SECRET", "N06_PARENT_SECRET_SENTINEL")
    monkeypatch.setenv("SIDERA_SESSION_COOKIE", "N06_COOKIE_SENTINEL")
    monkeypatch.setenv("SIDERA_GRANT_TOKEN", "N06_GRANT_SENTINEL")
    monkeypatch.setenv("OPENAI_API_KEY", "N06_PARENT_PROVIDER_SENTINEL")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(global_config))
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(
        _request(
            opencode,
            workspace,
            tools=(tool,),
            environment={"PROFILE_MODE": "test"},
        )
    )

    try:
        new_session = _request_record(capture, "session/new")
        params = new_session["params"]
        assert isinstance(params, dict)
        assert params["cwd"] == str(workspace)
        assert params["mcpServers"] == [
            {
                "name": "task-bridge",
                "transport": "stdio",
                "command": str(tool_executable),
                "args": [str(mcp_capture), "arg with spaces"],
                "environment_names": ["TOOL_TOKEN"],
            }
        ]
        assert not any(
            "N06_" in json.dumps(row) or token in json.dumps(row)
            for row in _records(capture)
        )
        environment_record = next(
            row for row in _records(capture) if row.get("kind") == "environment"
        )
        assert environment_record["parent_sidera_present"] is False
        assert environment_record["parent_provider_present"] is False
        assert environment_record["parent_cookie_present"] is False
        assert environment_record["parent_grant_present"] is False
        serialized_records = json.dumps(_records(capture))
        assert "N06_COOKIE_SENTINEL" not in serialized_records
        assert "N06_GRANT_SENTINEL" not in serialized_records
        assert json.loads(mcp_capture.read_text()) == {
            "methods": [
                "initialize",
                "notifications/initialized",
                "tools/list",
                "tools/call",
            ],
            "token_present": True,
            "argv": ["arg with spaces"],
        }
        assert handle.session_id == "owned-acp-session"
    finally:
        closed = adapter.close(handle)

    assert closed.status is NativeCloseState.CLOSED
    assert (global_config.read_bytes(), global_auth.read_bytes()) == original_files


def test_prompt_events_permission_request_and_selected_observed_separation(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    request = _request(opencode, workspace)
    handle = adapter.open(request)

    try:
        outcome = adapter.prompt(
            handle,
            AssembledInput(
                instructions=("Do the requested work.",),
                text="Summarize this.",
                context=(),
            ),
            uuid4(),
        )
        events = adapter.read_events(handle, 0)
        assert outcome.status is NativeOutcomeStatus.COMPLETED
        assert outcome.observed_model is None
        assert [event.kind for event in events] == ["progress.updated"]
        assert "session/request_permission" in json.dumps(_records(capture))
        assert any(row.get("kind") == "server_reply" for row in _records(capture))
        assert adapter.inspect(handle).process_state is NativeProcessState.ALIVE
    finally:
        adapter.close(handle)


def test_prompt_event_secret_is_redacted_and_not_recorded(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode="secret_update")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "N06_EVENT_SECRET_SENTINEL"
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(
        _request(opencode, workspace, environment={"PROFILE_TOKEN": SecretStr(secret)})
    )

    try:
        adapter.prompt(
            handle,
            AssembledInput(instructions=(), text="Run.", context=()),
            uuid4(),
        )
        events = adapter.read_events(handle, 0)
        assert events[0].data["text"] == "[REDACTED]"
        assert secret not in json.dumps(
            [event.model_dump(mode="json") for event in events]
        )
    finally:
        adapter.close(handle)


def test_read_events_preserves_updates_beyond_limit(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode="multi_update")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))

    try:
        adapter.prompt(
            handle,
            AssembledInput(instructions=(), text="Run.", context=()),
            uuid4(),
        )
        first = adapter.read_events(handle, 0, limit=1)
        second = adapter.read_events(handle, first[-1].cursor, limit=1)

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].cursor == 1
        assert second[0].cursor == 2
        assert second[0].data["text"] == "second fixture update"
    finally:
        adapter.close(handle)


def test_skill_is_materialized_for_one_run_and_deleted_on_close(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    body = "Use the stable review procedure exactly.  "
    skill = NativeSkill(
        resource_ref=ResourceRef(workspace_id=uuid4(), resource_id=uuid4(), revision=3),
        name="review",
        content=body,
        content_hash="sha256:" + sha256(body.encode()).hexdigest(),
    )
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace, skills=(skill,)))
    env_record = next(
        row for row in _records(capture) if row.get("kind") == "environment"
    )
    skill_paths = adapter._sessions[
        handle.process_identity.owned_nonce
    ].prepared.skill_paths

    assert env_record["skill_hashes"] == [sha256(body.encode()).hexdigest()]
    assert len(skill_paths) == 1 and skill_paths[0].read_text() == body

    adapter.close(handle)
    assert not skill_paths[0].exists()


def test_concurrent_profiles_receive_independent_skill_projection(tmp_path):
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    capture_a = tmp_path / "profile-a.jsonl"
    capture_b = tmp_path / "profile-b.jsonl"
    executable_a = _launcher(tmp_path, "opencode-a", capture=capture_a)
    executable_b = _launcher(tmp_path, "opencode-b", capture=capture_b)
    content_a = "Profile A instructions only."
    content_b = "Profile B instructions only."
    skill_a = NativeSkill(
        resource_ref=ResourceRef(workspace_id=uuid4(), resource_id=uuid4(), revision=1),
        name="profile-a",
        content=content_a,
        content_hash="sha256:" + sha256(content_a.encode()).hexdigest(),
    )
    skill_b = NativeSkill(
        resource_ref=ResourceRef(workspace_id=uuid4(), resource_id=uuid4(), revision=2),
        name="profile-b",
        content=content_b,
        content_hash="sha256:" + sha256(content_b.encode()).hexdigest(),
    )
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle_a = adapter.open(_request(executable_a, workspace_a, skills=(skill_a,)))
    handle_b = adapter.open(_request(executable_b, workspace_b, skills=(skill_b,)))

    try:
        config_a = next(
            row for row in _records(capture_a) if row["kind"] == "environment"
        )
        config_b = next(
            row for row in _records(capture_b) if row["kind"] == "environment"
        )
        assert config_a["skill_hashes"] == [
            skill_a.content_hash.removeprefix("sha256:")
        ]
        assert config_b["skill_hashes"] == [
            skill_b.content_hash.removeprefix("sha256:")
        ]
        assert config_a["skill_hashes"] != config_b["skill_hashes"]
    finally:
        adapter.close(handle_a)
        adapter.close(handle_b)


def test_http_mcp_requires_negotiated_http_and_fails_before_session_creation(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ToolConnection(
        name="remote-bridge",
        transport="http",
        endpoint_or_argv="https://mcp.example.invalid/v1",
        headers={},
        environment={},
    )
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)

    with pytest.raises(AgentError) as error:
        adapter.open(_request(opencode, workspace, tools=(tool,)))

    assert error.value.code == "unsupported_capability"
    assert not any(row.get("method") == "session/new" for row in _records(capture))


def test_http_mcp_is_projected_only_after_owned_peer_advertises_http(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, http=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ToolConnection(
        name="loopback-bridge",
        transport="http",
        endpoint_or_argv="http://127.0.0.1:9912/mcp",
        headers={"X-Scoped": SecretStr("N06_HTTP_HEADER_SECRET")},
        environment={},
    )
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace, tools=(tool,), http=True))
    try:
        new_session = _request_record(capture, "session/new")
        assert new_session["params"]["mcpServers"] == [
            {
                "name": "loopback-bridge",
                "transport": "http",
                "url": "http://127.0.0.1:9912/mcp",
                "header_names": ["X-Scoped"],
            }
        ]
        assert "N06_HTTP_HEADER_SECRET" not in capture.read_text()
    finally:
        adapter.close(handle)


def test_native_error_diagnostic_is_not_exposed(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode="error_new")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "N06_START_DIAGNOSTIC_SECRET"
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)

    with pytest.raises(AgentError) as error:
        adapter.open(
            _request(
                opencode, workspace, environment={"PROFILE_TOKEN": SecretStr(secret)}
            )
        )

    assert error.value.code == "native_start_failed"
    assert secret not in str(error.value)
    assert secret not in capture.read_text()


def test_open_timeout_is_bounded_and_owned_process_is_reaped(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode="hang_new")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=3.0, stop_grace_seconds=0.1)
    started = time.monotonic()

    with pytest.raises(AgentError) as error:
        adapter.open(_request(opencode, workspace))

    assert error.value.code == "native_start_unknown"
    assert time.monotonic() - started < 5
    records = _records(capture)
    assert any(row.get("method") == "initialize" for row in records)
    assert any(row.get("method") == "session/new" for row in records)
    pid = next(row["pid"] for row in records if row.get("kind") == "launcher")
    exit_deadline = time.monotonic() + 1
    while time.monotonic() < exit_deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("owned ACP process remained alive after open timed out")


def test_inspect_reports_gone_when_owned_process_exits_after_open(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(
        tmp_path, "opencode", capture=capture, mode="exit_after_session"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))

    try:
        deadline = time.monotonic() + 1
        inspection = adapter.inspect(handle)
        while (
            inspection.process_state is not NativeProcessState.GONE
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            inspection = adapter.inspect(handle)

        assert inspection.process_state is NativeProcessState.GONE
        assert inspection.reason_code == "native_process_gone"
    finally:
        adapter.close(handle)


def test_cancel_notification_is_sent_while_prompt_request_waits(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode="block_prompt")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))
    response_id = uuid4()
    outcomes = []
    prompt_thread = Thread(
        target=lambda: outcomes.append(
            adapter.prompt(
                handle,
                AssembledInput(instructions=(), text="wait", context=()),
                response_id,
            )
        ),
        daemon=True,
    )
    prompt_thread.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not any(
        row.get("kind") == "server_reply" for row in _records(capture)
    ):
        time.sleep(0.01)

    cancelled = adapter.cancel(handle, response_id)
    prompt_thread.join(timeout=1)
    try:
        assert cancelled.status.value == "accepted"
        assert not prompt_thread.is_alive()
        assert outcomes[0].status is NativeOutcomeStatus.CANCELLED
        assert any(row.get("method") == "session/cancel" for row in _records(capture))
    finally:
        adapter.close(handle)


def test_profile_model_is_not_claimed_as_applied_or_observed(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _request(opencode, workspace).model_copy(
        update={"projected_config": {"model_id": "fixture/provider-model"}}
    )
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)

    with pytest.raises(AgentError) as error:
        adapter.open(request)

    assert error.value.code == "configuration_invalid"
    assert request.projected_config["model_id"] == "fixture/provider-model"
    assert not capture.exists()


def test_probe_negotiates_protocol_capabilities_without_prompting(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    harness = default_harness_registry().get("opencode")

    probe = harness.probe(
        RuntimeCandidate(harness_id="opencode", executable=str(opencode)),
        checked_at=datetime(2026, 10, 2, tzinfo=UTC),
    )

    assert probe.actual_version == "9.8.7"
    assert probe.protocol_state is ProtocolState.READY
    assert probe.capabilities["mcp"].status is SupportStatus.SUPPORTED
    assert probe.capabilities["mcp_http"].status is SupportStatus.UNSUPPORTED
    assert not any(
        row.get("method") in {"session/new", "session/prompt"}
        for row in _records(capture)
    )
