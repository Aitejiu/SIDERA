from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SESSION_ID = "owned-acp-session"


def _record(value: dict[str, Any]) -> None:
    target = os.environ.get("ACME_TEST_CAPTURE")
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _safe_server(server: dict[str, Any]) -> dict[str, Any]:
    if server.get("type") == "http":
        return {
            "name": server.get("name"),
            "transport": "http",
            "url": server.get("url"),
            "header_names": sorted(
                value.get("name")
                for value in server.get("headers", [])
                if isinstance(value, dict) and isinstance(value.get("name"), str)
            ),
        }
    return {
        "name": server.get("name"),
        "transport": "stdio",
        "command": server.get("command"),
        "args": server.get("args"),
        "environment_names": sorted(
            value.get("name")
            for value in server.get("env", [])
            if isinstance(value, dict) and isinstance(value.get("name"), str)
        ),
    }


def _mcp_roundtrip(server: dict[str, Any]) -> None:
    if server.get("type") == "http":
        return
    command = server.get("command")
    args = server.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return
    environment = os.environ.copy()
    for entry in server.get("env", []):
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            environment[entry["name"]] = str(entry.get("value", ""))
    process = subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    calls = (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "owned_fixture_tool", "arguments": {"value": "ok"}},
        },
    )
    try:
        for request in calls:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            if "id" in request:
                response = json.loads(process.stdout.readline())
                assert response.get("id") == request["id"]
    finally:
        process.stdin.close()
        process.wait(timeout=2)


def run_peer() -> None:
    if "--version" in sys.argv:
        print("OpenCode v9.8.7")
        return
    if len(sys.argv) < 2 or sys.argv[1] != "acp":
        raise SystemExit(2)

    content = os.environ.get("OPENCODE_CONFIG_CONTENT", "{}")
    try:
        config = json.loads(content)
    except json.JSONDecodeError:
        config = {}
    instructions = config.get("instructions", [])
    skill_hashes = []
    for instruction in instructions:
        try:
            skill_hashes.append(
                hashlib.sha256(Path(instruction).read_bytes()).hexdigest()
            )
        except (OSError, TypeError):
            skill_hashes.append("unavailable")
    _record(
        {
            "kind": "environment",
            "config_keys": sorted(config) if isinstance(config, dict) else [],
            "skill_hashes": skill_hashes,
            "parent_sidera_present": "SIDERA_PARENT_SECRET" in os.environ,
            "parent_provider_present": "OPENAI_API_KEY" in os.environ,
            "parent_cookie_present": "SIDERA_SESSION_COOKIE" in os.environ,
            "parent_grant_present": "SIDERA_GRANT_TOKEN" in os.environ,
            "pid": os.getpid(),
            "config_dir_isolated": os.environ.get("XDG_CONFIG_HOME")
            != str(Path.home() / ".config"),
        }
    )

    for raw in sys.stdin:
        request = json.loads(raw)
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")
        if method == "initialize":
            _record({"kind": "request", "method": method, "params": params})
            mode = os.environ.get("ACME_TEST_MODE")
            result: dict[str, Any] = {
                "agentCapabilities": {
                    "loadSession": False,
                    "mcpCapabilities": {
                        "http": os.environ.get("ACME_TEST_HTTP") == "1"
                    },
                    "sessionCapabilities": {"close": {}},
                },
                "agentInfo": {"name": "owned-peer", "version": "1"},
            }
            if mode != "missing_protocol_version":
                result["protocolVersion"] = (
                    0
                    if mode == "protocol_version_zero"
                    else 2
                    if mode == "protocol_version_two"
                    else True
                    if mode == "protocol_version_boolean"
                    else 1
                )
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": result,
                }
            )
        elif method == "session/new":
            servers = params.get("mcpServers", [])
            _record(
                {
                    "kind": "request",
                    "method": method,
                    "params": {
                        "cwd": params.get("cwd"),
                        "mcpServers": [_safe_server(server) for server in servers],
                    },
                }
            )
            mode = os.environ.get("ACME_TEST_MODE")
            if mode == "hang_new":
                while True:
                    time.sleep(1)
            if mode == "error_new":
                diagnostic = os.environ.get(
                    "PROFILE_TOKEN", "N06_SYNTHETIC_DIAGNOSTIC_SECRET"
                )
                sys.stderr.write(diagnostic + "\n")
                sys.stderr.flush()
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": diagnostic},
                    }
                )
                continue
            for server in servers:
                _mcp_roundtrip(server)
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"sessionId": SESSION_ID},
                }
            )
            if mode == "exit_after_session":
                return
        elif method == "session/prompt":
            _record(
                {
                    "kind": "request",
                    "method": method,
                    "params": {
                        "sessionId": params.get("sessionId"),
                        "block_types": [
                            block.get("type")
                            for block in params.get("prompt", [])
                            if isinstance(block, dict)
                        ],
                    },
                }
            )
            mode = os.environ.get("ACME_TEST_MODE")
            permission_params: dict[str, Any] = {}
            if mode == "permission_options":
                permission_params["options"] = [
                    {
                        "optionId": "allow-once",
                        "name": "Allow once",
                        "kind": "allow_once",
                    },
                    {
                        "optionId": "reject-once",
                        "name": "Reject once",
                        "kind": "reject_once",
                    },
                ]
            elif mode == "permission_no_reject":
                permission_params["options"] = [
                    {
                        "optionId": "allow-once",
                        "name": "Allow once",
                        "kind": "allow_once",
                    }
                ]
            _write(
                {
                    "jsonrpc": "2.0",
                    "method": "session/request_permission",
                    "id": "permission-request",
                    "params": permission_params,
                }
            )
            reply = json.loads(sys.stdin.readline())
            _record({"kind": "server_reply", "reply": reply})
            cancelled = False
            if mode == "withhold_prompt":
                while True:
                    time.sleep(1)
            if mode == "late_terminal_response":
                time.sleep(0.2)
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
                _record({"kind": "terminal_reply", "method": "session/prompt"})
                continue
            if os.environ.get("ACME_TEST_MODE") == "block_prompt":
                while True:
                    control = json.loads(sys.stdin.readline())
                    if control.get("method") == "session/cancel":
                        cancelled = True
                        _record({"kind": "request", "method": "session/cancel"})
                        break
            update_texts = [os.environ.get("PROFILE_TOKEN", "fixture response")]
            if os.environ.get("ACME_TEST_MODE") == "multi_update":
                update_texts.append("second fixture update")
            for update_text in update_texts:
                update_type = (
                    "credential_N06_UNKNOWN_UPDATE_SENTINEL"
                    if mode == "unknown_update_type"
                    else "agent_message_chunk"
                )
                _write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": SESSION_ID,
                            "update": {
                                "sessionUpdate": update_type,
                                "content": {"type": "text", "text": update_text},
                            },
                        },
                    }
                )
            _record(
                {
                    "kind": "server_request",
                    "method": "session/request_permission",
                }
            )
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"stopReason": "cancelled" if cancelled else "end_turn"},
                }
            )
        elif method == "session/cancel":
            _record({"kind": "request", "method": method})
        elif method == "session/close":
            _record({"kind": "request", "method": method})
            _write({"jsonrpc": "2.0", "id": request_id, "result": {}})


def run_mcp_stub() -> None:
    capture = Path(sys.argv[2])
    methods = []
    for raw in sys.stdin:
        request = json.loads(raw)
        method = request.get("method")
        methods.append(method)
        request_id = request.get("id")
        if method == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2025-03-26"}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": "owned_fixture_tool", "inputSchema": {"type": "object"}}
                ]
            }
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        else:
            result = {}
        if request_id is not None:
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
                + "\n"
            )
            sys.stdout.flush()
    capture.write_text(
        json.dumps(
            {
                "methods": methods,
                "token_present": bool(os.environ.get("TOOL_TOKEN")),
                "argv": sys.argv[3:],
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        run_mcp_stub()
    else:
        run_peer()
