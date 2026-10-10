from __future__ import annotations

import json
import os
import stat
import sys
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from typing import get_type_hints
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sidera_agents.contracts import (
    AssembledInput,
    ConfigValidation,
    CredentialRef,
    NativeCancelState,
    NativeCloseState,
    NativeOpenRequest,
    NativeOutcomeStatus,
    NativeProcessState,
    NativeResponseState,
    NativeSkill,
    ProtocolState,
    ResourceRef,
    RuntimeCandidate,
    RuntimeRef,
    RuntimeSnapshot,
    SecretBinding,
    SupportStatus,
    ToolConnection,
)
from sidera_agents.errors import AgentError
from sidera_agents.harnesses import acp_transport, opencode_config
from sidera_agents.harnesses import opencode as opencode_module
from sidera_agents.harnesses.acp_transport import ACPProcessTransport
from sidera_agents.harnesses.opencode import OpenCodeHarness
from sidera_agents.harnesses.registry import (
    HarnessRegistry,
    TrustedHarness,
    default_harness_registry,
)

_PEER = Path(__file__).parents[1] / "fixtures" / "acp_peer.py"


@pytest.mark.parametrize(
    "field",
    [
        "request_timeout_seconds",
        "stop_grace_seconds",
        "prompt_timeout_seconds",
        "cancel_timeout_seconds",
        "close_timeout_seconds",
    ],
)
@pytest.mark.parametrize(
    "value", [0, -1, True, False, float("nan"), float("inf"), float("-inf")]
)
def test_invalid_deadline_rejects_before_native_spawn(field, value, monkeypatch):
    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("Invalid deadlines must not spawn native processes")

    monkeypatch.setattr(ACPProcessTransport, "spawn_argv", forbidden_spawn)
    with pytest.raises(ValueError):
        OpenCodeHarness(**{field: value})


@pytest.mark.parametrize(
    "configuration,prompt_timeout,close_timeout",
    [
        ("builtin", 900, 1),
        ("explicit_none", 2, 2),
        ("overrides", 17, 0.05),
    ],
)
def test_setup_prompt_and_close_use_independent_transport_deadlines(
    tmp_path, monkeypatch, configuration, prompt_timeout, close_timeout
):
    calls = []
    request = ACPProcessTransport.request

    def capture_request(self, method, params, *, timeout):
        calls.append((method, timeout))
        return request(self, method, params, timeout=timeout)

    monkeypatch.setattr(ACPProcessTransport, "request", capture_request)
    if configuration == "builtin":
        adapter = default_harness_registry().get("opencode")
        setup_timeout = 30
    elif configuration == "explicit_none":
        adapter = OpenCodeHarness(
            request_timeout_seconds=2,
            prompt_timeout_seconds=None,
            cancel_timeout_seconds=None,
            close_timeout_seconds=None,
            stop_grace_seconds=0.2,
        )
        setup_timeout = 2
    else:
        adapter = OpenCodeHarness(
            request_timeout_seconds=2,
            prompt_timeout_seconds=17,
            cancel_timeout_seconds=0.03,
            close_timeout_seconds=0.05,
            stop_grace_seconds=0.2,
        )
        setup_timeout = 2
    executable = _launcher(tmp_path, "opencode")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handle = adapter.open(_request(executable, workspace))
    try:
        assert (
            adapter.prompt(
                handle,
                AssembledInput(instructions=(), text="fixture", context=()),
                uuid4(),
            ).status
            is NativeOutcomeStatus.COMPLETED
        )
    finally:
        assert adapter.close(handle).status is NativeCloseState.CLOSED
    assert calls == [
        ("initialize", setup_timeout),
        ("session/new", setup_timeout),
        ("session/prompt", prompt_timeout),
        ("session/close", close_timeout),
    ]


@pytest.mark.parametrize(
    "configuration,expected_cancel",
    [("builtin", 1), ("explicit_none", 2), ("override", 0.05)],
)
def test_cancel_confirmation_budget_is_independent_of_long_prompt(
    tmp_path, monkeypatch, configuration, expected_cancel
):
    if configuration == "builtin":
        adapter = default_harness_registry().get("opencode")
    else:
        adapter = OpenCodeHarness(
            request_timeout_seconds=2,
            prompt_timeout_seconds=900,
            cancel_timeout_seconds=None if configuration == "explicit_none" else 0.05,
            close_timeout_seconds=0.05,
            stop_grace_seconds=0.2,
        )
    capture = tmp_path / "peer.jsonl"
    executable = _launcher(
        tmp_path, "opencode", capture=capture, mode="withhold_prompt"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handle = adapter.open(_request(executable, workspace))
    session = adapter._sessions[handle.process_identity.owned_nonce]
    wait = session.condition.wait
    budgets = []

    def bounded_wait(timeout):
        budgets.append(timeout)
        return wait(min(timeout, 0.01))

    monkeypatch.setattr(session.condition, "wait", bounded_wait)
    _shorten_prompt_timeout(adapter, handle, monkeypatch, seconds=0.3)
    response_id = uuid4()
    outcomes = []
    prompt_thread = Thread(
        target=lambda: outcomes.append(
            adapter.prompt(
                handle,
                AssembledInput(instructions=(), text="fixture", context=()),
                response_id,
            )
        ),
        daemon=True,
    )
    try:
        prompt_thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not any(
            row.get("kind") == "server_reply" for row in _records(capture)
        ):
            time.sleep(0.005)
        assert any(row.get("kind") == "server_reply" for row in _records(capture))
        outcome = adapter.cancel(handle, response_id)
        assert outcome.status is NativeCancelState.UNKNOWN
        assert outcome.response_id == response_id
        assert budgets[0] == pytest.approx(expected_cancel, rel=0.05, abs=0.01)
        if configuration == "override":
            assert prompt_thread.is_alive()
            assert adapter.inspect(handle).response_state is NativeResponseState.ACTIVE
    finally:
        adapter.close(handle)
        prompt_thread.join(timeout=1)
    assert not prompt_thread.is_alive()


def test_independent_prompt_timeout_retains_original_uncertain_response(tmp_path):
    capture = tmp_path / "peer.jsonl"
    executable = _launcher(
        tmp_path, "opencode", capture=capture, mode="withhold_prompt"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(
        request_timeout_seconds=2,
        prompt_timeout_seconds=0.05,
        cancel_timeout_seconds=0.01,
        close_timeout_seconds=0.01,
        stop_grace_seconds=0.2,
    )
    handle = adapter.open(_request(executable, workspace))
    prepared = adapter._sessions[handle.process_identity.owned_nonce].prepared
    response_id = uuid4()
    try:
        outcome = adapter.prompt(
            handle,
            AssembledInput(instructions=(), text="fixture", context=()),
            response_id,
        )
        assert outcome.status is NativeOutcomeStatus.UNKNOWN
        assert outcome.reason_code == "native_response_unknown"
        assert outcome.response_id == response_id
        assert prepared.directory.exists()
        assert adapter.inspect(handle).response_state is NativeResponseState.UNKNOWN
        with pytest.raises(AgentError) as error:
            adapter.prompt(
                handle,
                AssembledInput(instructions=(), text="retry", context=()),
                uuid4(),
            )
        assert error.value.code == "native_response_unknown"
        assert (
            sum(row.get("method") == "session/prompt" for row in _records(capture)) == 1
        )
    finally:
        assert adapter.close(handle).status is NativeCloseState.CLOSED


def test_trusted_harness_validate_returns_config_validation_contract():
    assert get_type_hints(TrustedHarness.validate)["return"] is ConfigValidation


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


def _product_http_tool(name: str = "SIDERA Task Tools") -> ToolConnection:
    return ToolConnection(
        name=name,
        transport="http",
        endpoint_or_argv="http://127.0.0.1:9912/mcp",
        headers={"Authorization": SecretStr("N23_FAKE_HTTP_SECRET")},
        environment={},
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


def _shorten_prompt_timeout(adapter, handle, monkeypatch, seconds=0.05):
    transport = adapter._sessions[handle.process_identity.owned_nonce].transport
    request = transport.request

    def bounded_request(method, params, *, timeout):
        if method in {"session/prompt", "session/close"}:
            timeout = min(timeout, seconds)
        return request(method, params, timeout=timeout)

    monkeypatch.setattr(transport, "request", bounded_request)


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


@pytest.mark.parametrize(
    ("source_path", "is_directory"),
    [
        ("opencode.json", False),
        (".opencode/opencode.jsonc", False),
        (".opencode/plugins", True),
    ],
)
def test_open_rejects_unapproved_workspace_config_before_session_new(
    tmp_path, monkeypatch, source_path, is_directory
):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    repository = tmp_path / "repository"
    workspace = repository / "nested"
    workspace.mkdir(parents=True)
    source = repository / source_path
    if is_directory:
        source.mkdir(parents=True)
        (source / "untrusted.js").write_text("export default () => {};\n")
    else:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"mcp":{"unapproved":{"enabled":true}}}\n')
    monkeypatch.setattr(
        opencode_config, "managed_opencode_config_sources", lambda: (), raising=False
    )
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)

    try:
        handle = adapter.open(_request(opencode, workspace))
    except AgentError as error:
        assert error.code in {"configuration_invalid", "unsupported_capability"}
    else:
        adapter.close(handle)
        pytest.fail("unapproved workspace config was accepted")

    assert not any(row.get("method") == "session/new" for row in _records(capture))


@pytest.mark.parametrize(
    "managed_relative_path",
    [
        "Library/Application Support/opencode/opencode.json",
        "etc/opencode/opencode.jsonc",
        "Library/Managed Preferences/test-user/ai.opencode.managed.plist",
        "Library/Managed Preferences/ai.opencode.managed.plist",
    ],
)
def test_open_rejects_managed_opencode_policy_before_session_new(
    tmp_path, monkeypatch, managed_relative_path
):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    managed_source = tmp_path / "system" / managed_relative_path
    managed_source.parent.mkdir(parents=True)
    original = b"synthetic managed policy\n"
    managed_source.write_bytes(original)
    monkeypatch.setattr(
        opencode_config,
        "managed_opencode_config_sources",
        lambda: (managed_source,),
        raising=False,
    )
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)

    try:
        handle = adapter.open(_request(opencode, workspace))
    except AgentError as error:
        assert error.code in {"configuration_invalid", "unsupported_capability"}
    else:
        adapter.close(handle)
        pytest.fail("managed OpenCode policy was accepted")

    assert managed_source.read_bytes() == original
    assert not any(row.get("method") == "session/new" for row in _records(capture))


@pytest.mark.parametrize(
    "mode",
    [
        "missing_protocol_version",
        "protocol_version_zero",
        "protocol_version_two",
        "protocol_version_boolean",
    ],
)
def test_open_rejects_wrong_acp_protocol_version_before_session_new(tmp_path, mode):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode=mode)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)

    try:
        handle = adapter.open(_request(opencode, workspace))
    except AgentError as error:
        assert error.code in {"native_start_failed", "unsupported_capability"}
    else:
        adapter.close(handle)
        pytest.fail("an unsupported ACP protocol version was accepted")

    records = _records(capture)
    assert any(row.get("method") == "initialize" for row in records)
    assert not any(row.get("method") == "session/new" for row in records)
    assert adapter._sessions == {}


@pytest.mark.parametrize(
    ("mode", "expected_outcome"),
    [
        (
            "permission_options",
            {"outcome": "selected", "optionId": "reject-once"},
        ),
        ("permission_no_reject", {"outcome": "cancelled"}),
    ],
)
@pytest.mark.parametrize("native_tools", ["none", "read", "write"])
def test_permission_request_uses_valid_reject_outcome(
    tmp_path, mode, expected_outcome, native_tools
):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode=mode)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(
        _request(opencode, workspace).model_copy(
            update={"projected_config": {"native_tools": native_tools}}
        )
    )

    try:
        adapter.prompt(
            handle,
            AssembledInput(instructions=(), text="Run.", context=()),
            uuid4(),
        )
        reply = next(
            row["reply"]
            for row in _records(capture)
            if row.get("kind") == "server_reply"
        )
        assert reply["result"]["outcome"] == expected_outcome
        environment = next(
            row for row in _records(capture) if row["kind"] == "environment"
        )
        allowed = (
            {}
            if native_tools == "none"
            else {"read": "allow", "glob": "allow", "grep": "allow"}
        )
        if native_tools == "write":
            allowed.update(edit="allow", bash="allow")
        assert environment["permission"] == {"*": "deny", **allowed}
    finally:
        adapter.close(handle)


def test_unknown_update_type_is_not_copied_to_public_event(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(
        tmp_path, "opencode", capture=capture, mode="unknown_update_type"
    )
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
        events = adapter.read_events(handle, 0)
        serialized = json.dumps([event.model_dump(mode="json") for event in events])
        assert len(events) == 1
        assert events[0].data["update_type"] == "unknown"
        assert "credential_N06_UNKNOWN_UPDATE_SENTINEL" not in serialized
    finally:
        adapter.close(handle)


def test_prompt_timeout_keeps_response_unknown_until_terminal_evidence(
    tmp_path, monkeypatch
):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode="withhold_prompt")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=5, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))
    _shorten_prompt_timeout(adapter, handle, monkeypatch)

    try:
        outcome = adapter.prompt(
            handle,
            AssembledInput(instructions=(), text="Run.", context=()),
            uuid4(),
        )

        assert outcome.status is NativeOutcomeStatus.UNKNOWN
        assert adapter.inspect(handle).response_state is NativeResponseState.UNKNOWN
    finally:
        adapter.close(handle)


def test_late_prompt_terminal_response_clears_unknown_response_state(
    tmp_path, monkeypatch
):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(
        tmp_path, "opencode", capture=capture, mode="late_terminal_response"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=5, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))
    _shorten_prompt_timeout(adapter, handle, monkeypatch)

    try:
        outcome = adapter.prompt(
            handle,
            AssembledInput(instructions=(), text="Run.", context=()),
            uuid4(),
        )
        assert outcome.status is NativeOutcomeStatus.UNKNOWN
        assert adapter.inspect(handle).response_state is NativeResponseState.UNKNOWN

        time.sleep(0.25)
        transport = adapter._sessions[handle.process_identity.owned_nonce].transport
        transport.request("session/close", {"sessionId": handle.session_id}, timeout=2)
        adapter.read_events(handle, 0)

        assert adapter.inspect(handle).response_state is NativeResponseState.IDLE
    finally:
        adapter.close(handle)


@pytest.mark.parametrize("probe_failure", ["permission", "start_identity"])
def test_identity_probe_error_is_unknown_not_gone(tmp_path, monkeypatch, probe_failure):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))

    try:
        with monkeypatch.context() as scoped:
            if probe_failure == "permission":

                def denied(_pid):
                    raise PermissionError

                scoped.setattr(acp_transport.os, "getpgid", denied)
            else:

                def unavailable(_pid):
                    raise acp_transport.ACPTransportError("acp_identity_unavailable")

                scoped.setattr(acp_transport, "_process_start_identity", unavailable)
            inspection = adapter.inspect(handle)

        assert inspection.process_state is NativeProcessState.UNKNOWN
    finally:
        adapter.close(handle)


def test_replaced_process_identity_is_gone(tmp_path, monkeypatch):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                acp_transport, "_process_start_identity", lambda _pid: "replacement"
            )
            inspection = adapter.inspect(handle)
        assert inspection.process_state is NativeProcessState.GONE
    finally:
        adapter.close(handle)


def test_close_unknown_retains_session_and_private_config(tmp_path, monkeypatch):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=5, stop_grace_seconds=1.0)
    handle = adapter.open(_request(opencode, workspace))
    session = adapter._sessions[handle.process_identity.owned_nonce]
    prepared_directory = session.prepared.directory
    transport = session.transport
    terminate = transport.terminate
    close_transport = transport.close
    attempts = 0
    close_calls = []

    def uncertain_then_real_terminate(*, grace_seconds):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return terminate(grace_seconds=grace_seconds)

    def track_close():
        close_calls.append(True)
        close_transport()

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(transport, "terminate", uncertain_then_real_terminate)
            scoped.setattr(transport, "close", track_close)

            first = adapter.close(handle)

            assert first.status.value == "unknown"
            assert adapter._sessions[handle.process_identity.owned_nonce] is session
            assert prepared_directory.exists()
            assert close_calls == []

            confirmed = adapter.close(handle)
            if confirmed.status is NativeCloseState.UNKNOWN:
                assert adapter._sessions[handle.process_identity.owned_nonce] is session
                assert prepared_directory.exists()
                assert close_calls == []
                time.sleep(0.05)
                confirmed = adapter.close(handle)

            assert confirmed.status is NativeCloseState.CLOSED
            assert handle.process_identity.owned_nonce not in adapter._sessions
            assert not prepared_directory.exists()
            assert close_calls == [True]
    finally:
        terminate(grace_seconds=0.2)
        close_transport()
        session.prepared.close()
        adapter._sessions.pop(handle.process_identity.owned_nonce, None)


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


def test_known_secret_collection_does_not_call_getter_on_plain_text(tmp_path):
    class PlainText(str):
        def get_secret_value(self):
            raise AssertionError("Plain text must not be treated as a credential")

    request = _request(_launcher(tmp_path, "opencode"), tmp_path).model_copy(
        update={
            "environment": {
                "PLAIN_TEXT": PlainText("ordinary value"),
                "BOUND_SECRET": SecretStr("fixture-bound-secret"),
            }
        }
    )

    assert opencode_module._secret_values(request) == ("fixture-bound-secret",)


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


def test_pending_updates_keep_original_response_across_two_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_config, "managed_opencode_config_sources", lambda: ())
    opencode = _launcher(tmp_path, "opencode", mode="multi_update")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))
    first_id, second_id = uuid4(), uuid4()
    try:
        for response_id in (first_id, second_id):
            outcome = adapter.prompt(
                handle,
                AssembledInput(instructions=(), text="Run.", context=()),
                response_id,
            )
            assert outcome.status is NativeOutcomeStatus.COMPLETED
        events = adapter.read_events(handle, 0)
        assert [event.response_id for event in events] == [
            first_id,
            first_id,
            second_id,
            second_id,
        ]
        assert [event.cursor for event in events] == [1, 2, 3, 4]
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


def test_product_http_duplicate_uses_one_owned_acp_alias_and_permission(
    tmp_path, monkeypatch
):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, http=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    monkeypatch.setattr(opencode_config.tempfile, "tempdir", str(private_root))
    tools = (_product_http_tool(), _product_http_tool())
    originals = tuple(tool.model_copy(deep=True) for tool in tools)
    adapter = OpenCodeHarness(
        request_timeout_seconds=2, stop_grace_seconds=0.2, parent_environment={}
    )
    handle = adapter.open(_request(opencode, workspace, tools=tools, http=True))
    prepared = adapter._sessions[handle.process_identity.owned_nonce].prepared

    try:
        new_session = _request_record(capture, "session/new")
        assert new_session["params"]["mcpServers"] == [
            {
                "name": "SIDERA_Task_Tools",
                "transport": "http",
                "url": "http://127.0.0.1:9912/mcp",
                "header_names": ["Authorization"],
            }
        ]
        environment = next(
            row for row in _records(capture) if row.get("kind") == "environment"
        )
        assert environment["permission"] == {
            "*": "deny",
            "SIDERA_Task_Tools_*": "allow",
        }
        assert json.loads(prepared.content)["permission"] == {
            "*": "deny",
            "SIDERA_Task_Tools_*": "allow",
        }
        assert prepared.directory.parent == private_root
        assert prepared.directory.exists()
        assert "N23_" not in prepared.content
        assert "N23_" not in capture.read_text(encoding="utf-8")
        assert tools == originals
        assert (
            len([row for row in _records(capture) if row.get("kind") == "launcher"])
            == 1
        )
    finally:
        closed = adapter.close(handle)

    assert closed.status is NativeCloseState.CLOSED
    assert not prepared.directory.exists()
    assert list(private_root.iterdir()) == []
    assert adapter._sessions == {}
    assert adapter._orphaned == {}


@pytest.mark.parametrize("case", ["collision", "endpoint", "header", "environment"])
def test_open_invalid_tool_identity_forbids_allocation_and_peer_spawn(
    tmp_path, monkeypatch, case
):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, http=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    monkeypatch.setattr(opencode_config.tempfile, "tempdir", str(private_root))
    tool = _product_http_tool()
    if case == "collision":
        changed = _product_http_tool("SIDERA_Task_Tools")
    elif case == "endpoint":
        changed = tool.model_copy(
            update={"endpoint_or_argv": "http://127.0.0.1:9913/mcp"}
        )
    elif case == "header":
        changed = tool.model_copy(
            update={"headers": {"Authorization": SecretStr("N23_FAKE_OTHER_HEADER")}}
        )
    else:
        changed = tool.model_copy(
            update={"environment": {"TOOL_TOKEN": SecretStr("N23_FAKE_OTHER_ENV")}}
        )

    def forbidden_allocation(*args, **kwargs):
        raise AssertionError("Invalid tool identity attempted private allocation.")

    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("Invalid tool identity attempted ACP peer spawn.")

    monkeypatch.setattr(opencode_config.tempfile, "mkdtemp", forbidden_allocation)
    monkeypatch.setattr(ACPProcessTransport, "spawn_argv", forbidden_spawn)
    adapter = OpenCodeHarness(
        request_timeout_seconds=2, stop_grace_seconds=0.2, parent_environment={}
    )

    with pytest.raises(AgentError) as error:
        adapter.open(_request(opencode, workspace, tools=(tool, changed), http=True))

    assert error.value.code == "configuration_invalid"
    assert error.value.field == "tools"
    assert "N23_" not in str(error.value)
    assert not capture.exists()
    assert list(private_root.iterdir()) == []
    assert adapter._sessions == {}
    assert adapter._orphaned == {}


@pytest.mark.parametrize(
    "snapshot_http,peer_http", [(False, False), (False, True), (True, False)]
)
def test_product_http_requires_both_snapshot_and_negotiated_capability(
    tmp_path, monkeypatch, snapshot_http, peer_http
):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, http=peer_http)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    monkeypatch.setattr(opencode_config.tempfile, "tempdir", str(private_root))
    adapter = OpenCodeHarness(
        request_timeout_seconds=2, stop_grace_seconds=0.2, parent_environment={}
    )

    with pytest.raises(AgentError) as error:
        adapter.open(
            _request(
                opencode,
                workspace,
                tools=(_product_http_tool(), _product_http_tool()),
                http=snapshot_http,
            )
        )

    assert error.value.code == "unsupported_capability"
    assert error.value.field == "tools"
    records = _records(capture)
    assert any(row.get("method") == "initialize" for row in records)
    assert not any(row.get("method") == "session/new" for row in records)
    pid = next(row["pid"] for row in records if row.get("kind") == "launcher")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert list(private_root.iterdir()) == []
    assert adapter._sessions == {}
    assert adapter._orphaned == {}


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


def test_failed_open_retains_recovery_state_when_termination_is_unknown(
    tmp_path, monkeypatch
):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode="error_new")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    prepared_configs = []
    transports = []
    prepare = opencode_module.prepare_opencode_config
    terminate = ACPProcessTransport.terminate

    def capture_prepared(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        prepared_configs.append(prepared)
        return prepared

    def uncertain_termination(self, *, grace_seconds):
        transports.append(self)
        return False

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(opencode_module, "prepare_opencode_config", capture_prepared)
            scoped.setattr(ACPProcessTransport, "terminate", uncertain_termination)
            with pytest.raises(AgentError):
                adapter.open(_request(opencode, workspace))

            assert len(transports) == 1
            assert len(prepared_configs) == 1
            transport = transports[0]
            prepared = prepared_configs[0]
            assert not transport._closed
            assert prepared.directory.exists()
            orphaned = getattr(adapter, "_orphaned", {})
            assert len(orphaned) == 1
            retained = next(iter(orphaned.values()))
            assert retained.transport is transport
            assert retained.prepared is prepared

        adapter._reap_orphaned()
        assert adapter._orphaned == {}
        assert transport._closed
        assert not prepared.directory.exists()
    finally:
        for transport in transports:
            for _ in range(10):
                if terminate(transport, grace_seconds=0.2):
                    transport.close()
                    for prepared in prepared_configs:
                        prepared.close()
                    break
                time.sleep(0.05)


def test_probe_retains_recovery_state_when_termination_is_unknown(
    tmp_path, monkeypatch
):
    capture = tmp_path / "peer.jsonl"
    executable = _launcher(tmp_path, "opencode", capture=capture)
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    prepared_configs = []
    temporary_directories = []
    transports = []
    prepare = opencode_module.prepare_opencode_config
    terminate = ACPProcessTransport.terminate
    temporary_directory_type = opencode_module.tempfile.TemporaryDirectory

    def capture_prepared(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        prepared_configs.append(prepared)
        return prepared

    def capture_temporary_directory(*args, **kwargs):
        temporary = temporary_directory_type(*args, **kwargs)
        temporary_directories.append(temporary)
        return temporary

    def uncertain_termination(self, *, grace_seconds):
        transports.append(self)
        return False

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(opencode_module, "prepare_opencode_config", capture_prepared)
            scoped.setattr(
                opencode_module.tempfile,
                "TemporaryDirectory",
                capture_temporary_directory,
            )
            scoped.setattr(ACPProcessTransport, "terminate", uncertain_termination)
            probe = adapter.probe(
                RuntimeCandidate(harness_id="opencode", executable=str(executable))
            )

            assert probe.protocol_state is ProtocolState.READY
            assert len(transports) == 1
            assert len(prepared_configs) == 1
            assert len(temporary_directories) == 1
            transport = transports[0]
            prepared = prepared_configs[0]
            temporary = temporary_directories[0]
            assert not transport._closed
            assert prepared.directory.exists()
            assert Path(temporary.name).exists()
            orphaned = getattr(adapter, "_orphaned", {})
            assert len(orphaned) == 1
            retained = next(iter(orphaned.values()))
            assert retained.transport is transport
            assert retained.prepared is prepared
            assert retained.temporary_directory is temporary

        adapter._reap_orphaned()
        assert adapter._orphaned == {}
        assert transport._closed
        assert not prepared.directory.exists()
        assert not Path(temporary.name).exists()
    finally:
        for transport in transports:
            for _ in range(10):
                if terminate(transport, grace_seconds=0.2):
                    transport.close()
                    for prepared in prepared_configs:
                        prepared.close()
                    for temporary in temporary_directories:
                        temporary.cleanup()
                    break
                time.sleep(0.05)


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
        assert cancelled.status is NativeCancelState.CONFIRMED
        assert cancelled.response_id == response_id
        assert not prompt_thread.is_alive()
        assert outcomes[0].status is NativeOutcomeStatus.CANCELLED
        assert any(row.get("method") == "session/cancel" for row in _records(capture))
    finally:
        adapter.close(handle)


def test_stale_cancel_is_fenced_while_a_new_response_is_active(tmp_path, monkeypatch):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(
        tmp_path, "opencode", capture=capture, mode="block_second_prompt"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))
    old_response_id = uuid4()
    new_response_id = uuid4()
    outcomes = []
    prompt_sent = Event()
    permission_replied = Event()
    session = adapter._session(handle)
    original_write = session.transport._write

    def observe_prompt_write(message, **kwargs):
        result = original_write(message, **kwargs)
        if message.get("method") == "session/prompt":
            prompt_sent.set()
        if message.get("id") == "permission-request":
            permission_replied.set()
        return result

    monkeypatch.setattr(session.transport, "_write", observe_prompt_write)

    try:
        first = adapter.prompt(
            handle,
            AssembledInput(instructions=(), text="first", context=()),
            old_response_id,
        )
        prompt_sent.clear()
        permission_replied.clear()
        prompt_thread = Thread(
            target=lambda: outcomes.append(
                adapter.prompt(
                    handle,
                    AssembledInput(instructions=(), text="second", context=()),
                    new_response_id,
                )
            ),
            daemon=True,
        )
        prompt_thread.start()
        assert prompt_sent.wait(timeout=2)
        assert permission_replied.wait(timeout=2)

        stale = adapter.cancel(handle, old_response_id)
        assert first.status is NativeOutcomeStatus.COMPLETED
        assert stale.status is NativeCancelState.FAILED
        assert stale.reason_code == "response_not_current"
        assert not any(
            row.get("method") == "session/cancel" for row in _records(capture)
        )

        cancelled = adapter.cancel(handle, new_response_id)
        prompt_thread.join(timeout=1)
        assert cancelled.status is NativeCancelState.CONFIRMED
        assert cancelled.response_id == new_response_id
        assert not prompt_thread.is_alive()
        assert outcomes[0].status is NativeOutcomeStatus.CANCELLED
        assert (
            sum(row.get("method") == "session/cancel" for row in _records(capture)) == 1
        )
    finally:
        adapter.close(handle)


def test_cancel_without_correlated_prompt_result_remains_unknown(tmp_path, monkeypatch):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture, mode="withhold_prompt")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))
    response_id = uuid4()
    outcomes = []
    _shorten_prompt_timeout(adapter, handle, monkeypatch, seconds=0.2)
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

    try:
        prompt_thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not any(
            row.get("kind") == "server_reply" for row in _records(capture)
        ):
            time.sleep(0.01)
        cancelled = adapter.cancel(handle, response_id)
        prompt_thread.join(timeout=1)

        assert cancelled.status is NativeCancelState.UNKNOWN
        assert cancelled.response_id == response_id
        assert not prompt_thread.is_alive()
        assert outcomes[0].status is NativeOutcomeStatus.UNKNOWN
    finally:
        adapter.close(handle)


def test_cancel_without_negotiated_capability_is_unsupported(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)
    handle = adapter.open(_request(opencode, workspace))

    try:
        outcome = adapter.cancel(handle, uuid4())
        assert outcome.status is NativeCancelState.UNSUPPORTED
        assert outcome.reason_code == "cancel_not_negotiated"
        assert "cancel_current_response" not in handle.capabilities
        assert not any(
            row.get("method") == "session/cancel" for row in _records(capture)
        )
    finally:
        adapter.close(handle)


def test_profile_model_is_projected_without_fabricated_observed_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(opencode_config, "managed_opencode_config_sources", lambda: ())
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(tmp_path, "opencode", capture=capture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _request(opencode, workspace).model_copy(
        update={"projected_config": {"model_id": "opencode-go/deepseek-v4.1-flash"}}
    )
    adapter = OpenCodeHarness(request_timeout_seconds=2, stop_grace_seconds=0.2)

    handle = adapter.open(request)
    try:
        environment = next(
            row for row in _records(capture) if row["kind"] == "environment"
        )
        assert environment["model"] == "opencode-go/deepseek-v4.1-flash"
        assert environment["small_model"] == "opencode-go/deepseek-v4.1-flash"
        assert environment["enabled_providers"] == ["opencode-go"]
        assert environment["pid"] == handle.process_identity.pid
        assert handle.session_id == "owned-acp-session"
        assert _request_record(capture, "session/new")["params"]["cwd"] == str(
            workspace
        )
        outcome = adapter.prompt(
            handle, AssembledInput(instructions=(), text="fixture", context=()), uuid4()
        )
        assert outcome.status is NativeOutcomeStatus.COMPLETED
        assert outcome.observed_model is None
        assert request.runtime.auth_state == "unconfirmed"
    finally:
        assert adapter.close(handle).status is NativeCloseState.CLOSED


def test_two_profiles_deliver_independent_child_credentials_without_global_inheritance(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(opencode_config, "managed_opencode_config_sources", lambda: ())
    adapter = OpenCodeHarness(
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
        parent_environment={
            "PATH": os.defpath,
            "OPENCODE_API_KEY": "N09_FAKE_PARENT_PROVIDER_KEY",
        },
    )
    delivered = []
    spawn = ACPProcessTransport.spawn_argv

    def capture_spawn(argv, *, cwd, environment):
        delivered.append(environment["OPENCODE_API_KEY"])
        return spawn(argv, cwd=cwd, environment=environment)

    monkeypatch.setattr(ACPProcessTransport, "spawn_argv", capture_spawn)
    handles = []
    captures = []
    roots = []
    try:
        for index in range(2):
            capture = tmp_path / f"peer-{index}.jsonl"
            captures.append(capture)
            executable = _launcher(tmp_path, f"opencode-{index}", capture=capture)
            workspace = tmp_path / f"workspace-{index}"
            workspace.mkdir()
            request = _request(
                executable,
                workspace,
                environment={
                    "OPENCODE_API_KEY": SecretStr(f"N09_FAKE_PROFILE_KEY_{index}")
                },
            )
            request = request.model_copy(
                update={
                    "projected_config": {"model_id": "opencode-go/deepseek-v4.1-flash"}
                }
            )
            handle = adapter.open(request)
            handles.append(handle)
            root = adapter._sessions[
                handle.process_identity.owned_nonce
            ].prepared.directory
            roots.append(root)
            environment = next(
                row for row in _records(capture) if row["kind"] == "environment"
            )
            assert environment["provider_placeholders"] == {
                "opencode-go": "{env:OPENCODE_API_KEY}"
            }
            assert environment["provider_credential_present"] is True
            assert environment["home"] == str(root)
            assert "N09_FAKE_" not in capture.read_text()
        assert delivered == ["N09_FAKE_PROFILE_KEY_0", "N09_FAKE_PROFILE_KEY_1"]
        assert roots[0] != roots[1]
    finally:
        for handle in handles:
            assert adapter.close(handle).status is NativeCloseState.CLOSED
    assert not adapter._sessions
    assert all(not root.exists() for root in roots)


def test_authorized_profile_credentials_reach_owned_peer_and_redacted_run_trace(
    admission_lab, tmp_path, monkeypatch
):
    from sidera_agents.bootstrap import create_agent_service
    from sidera_agents.memory import MemoryAgentStore
    from sidera_agents.ports import DenyContextSourceReader

    monkeypatch.setattr(opencode_config, "managed_opencode_config_sources", lambda: ())
    lab = admission_lab
    capture = tmp_path / "projected-peer.jsonl"
    executable = _launcher(
        tmp_path, "opencode", capture=capture, mode="provider_update"
    )
    adapter = OpenCodeHarness(
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
        parent_environment={
            "PATH": os.defpath,
            "OPENCODE_API_KEY": "N09_FAKE_GLOBAL_KEY",
            "HTTP_PROXY": "http://127.0.0.1:9001",
            "HTTPS_PROXY": "http://127.0.0.1:9002",
            "NO_PROXY": "parent-only.invalid",
            "ALL_PROXY": "socks5://127.0.0.1:9003",
        },
    )
    values = {}
    reads = []

    class Secrets:
        def read(self, grant_id, binding):
            assert grant_id == lab.grant_id
            reads.append(binding)
            return SecretStr(values[binding.credential_ref.credential_id])

    service = create_agent_service(
        MemoryAgentStore(),
        HarnessRegistry((adapter,)),
        lab.authorizer,
        DenyContextSourceReader(),
        Secrets(),
        lab.clock,
    )
    try:
        runtime = service.runtimes.register(
            lab.workspace_id,
            "opencode",
            str(executable),
            max_active=2,
            max_sessions=2,
            grant_id=lab.grant_id,
        )
        service.poll_once()
        starts = []
        bindings = []
        for index in range(2):
            binding = SecretBinding(
                credential_ref=CredentialRef(
                    workspace_id=lab.workspace_id, credential_id=uuid4()
                ),
                destination="OPENCODE_API_KEY",
            )
            bindings.append(binding)
            values[binding.credential_ref.credential_id] = (
                f"N09_FAKE_AUTHORIZED_PROFILE_{index}"
            )
            profile = service.catalog.create_profile(
                lab.workspace_id,
                lab.profile.draft.model_copy(
                    update={
                        "name": f"explicit-native-profile-{index}",
                        "runtime_ref": runtime.ref,
                        "model_id": "opencode-go/deepseek-v4.1-flash",
                        "environment": {
                            "HTTP_PROXY": "http://127.0.0.1:7897",
                            "HTTPS_PROXY": "http://127.0.0.1:7897",
                            "NO_PROXY": "localhost,127.0.0.1,::1,[::1]",
                        },
                        "secret_refs": (binding,),
                        "native_settings": {"provider_env": "OPENCODE_API_KEY"},
                    }
                ),
                grant_id=lab.grant_id,
            )
            start = service.execution.start(
                lab.request(profile_ref=profile.ref, required_capabilities=("acp",))
            )
            starts.append(start)
            service.poll_once()
            snapshot = service.execution.get_run(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            )
            trace = service.observations.read_trace(
                lab.workspace_id, start.run_id, 0, grant_id=lab.grant_id
            )
            assert snapshot.state.value == "completed"
            assert (
                snapshot.effective_config.selected_model
                == "opencode-go/deepseek-v4.1-flash"
            )
            assert snapshot.effective_config.observed_model is None
            assert [
                event.data["text"] for event in trace.events if event.kind == "message"
            ] == ["[REDACTED]"]
            assert all(
                value not in snapshot.model_dump_json() + trace.model_dump_json()
                for value in values.values()
            )
        assert reads == bindings
        assert starts[0].run_id != starts[1].run_id
        environments = [
            row
            for row in _records(capture)
            if row.get("kind") == "environment" and row.get("provider_placeholders")
        ]
        assert len(environments) == 2
        assert all(
            row["network_environment"]
            == {
                "HTTP_PROXY": "http://127.0.0.1:7897",
                "HTTPS_PROXY": "http://127.0.0.1:7897",
                "NO_PROXY": "localhost,127.0.0.1,::1,[::1]",
                "ALL_PROXY": None,
            }
            for row in environments
        )
        assert all(row["provider_credential_present"] for row in environments)
        assert environments[0]["home"] != environments[1]["home"]
        assert all(not Path(row["home"]).exists() for row in environments)
        assert "N09_FAKE_" not in capture.read_text()
        assert not adapter._sessions
        assert runtime.auth_state.value == "unconfirmed"
    finally:
        service.close()


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
    assert probe.capabilities["native_login"].status is SupportStatus.UNSUPPORTED
    assert (
        probe.capabilities["native_login"].reason_code
        == "global_auth_inheritance_not_supported"
    )
    assert probe.capabilities["model"].status is SupportStatus.UNCONFIRMED
    assert probe.capabilities["native_tools"].status is SupportStatus.UNCONFIRMED
    assert (
        probe.capabilities["native_tools"].reason_code
        == "native_tool_execution_unconfirmed"
    )
    assert not any(
        row.get("method") in {"session/new", "session/prompt"}
        for row in _records(capture)
    )


def test_probe_rejects_boolean_acp_protocol_version(tmp_path):
    capture = tmp_path / "peer.jsonl"
    opencode = _launcher(
        tmp_path, "opencode", capture=capture, mode="protocol_version_boolean"
    )
    harness = default_harness_registry().get("opencode")

    probe = harness.probe(
        RuntimeCandidate(harness_id="opencode", executable=str(opencode)),
        checked_at=datetime(2026, 10, 2, tzinfo=UTC),
    )

    assert probe.protocol_state is ProtocolState.UNAVAILABLE
    assert probe.capabilities["acp"].status is SupportStatus.UNSUPPORTED
    assert probe.capabilities["acp"].reason_code != "acp_v1_initialized"
