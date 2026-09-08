from __future__ import annotations

import os
import stat
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest

from sidera_control_plane.adapters.acp_process import (
    ACPProcessError,
    ACPProcessProtocolError,
    ACPProcessTimeoutError,
    ACPProcessTransport,
)

_ECHO_SCRIPT = textwrap.dedent(
    """
    import json
    import os
    import sys

    def write(message):
        sys.stdout.write(json.dumps(message) + "\\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        request = json.loads(raw)
        write({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "cwd": os.getcwd(),
                "explicit": os.environ.get("SIDERA_TEST_TOKEN"),
                "ambient": os.environ.get("DEEPSEEK_API_KEY"),
            },
        })
    """
).strip()


def _spawn_script(tmp_path: Path, body: str, *args: str) -> ACPProcessTransport:
    return ACPProcessTransport.spawn_argv(
        (sys.executable, "-c", body, *args),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "SIDERA_TEST_TOKEN": "explicit"},
    )


def test_spawn_argv_preserves_spaces_and_replaces_ambient_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-cross-process-boundary")

    transport = _spawn_script(workspace, _ECHO_SCRIPT)
    try:
        result = transport.request("echo", {}, timeout=2)
        assert result == {
            "cwd": str(workspace),
            "explicit": "explicit",
            "ambient": None,
        }
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_legacy_spawn_still_appends_acp_argument(tmp_path: Path) -> None:
    executable = tmp_path / "legacy executable"
    executable.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import sys

            assert sys.argv[1:] == ["acp"]
            for raw in sys.stdin:
                request = json.loads(raw)
                sys.stdout.write(json.dumps({"id": request["id"], "result": {"ok": True}}) + "\\n")
                sys.stdout.flush()
            """
        ).strip()
        + "\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    transport = ACPProcessTransport.spawn(str(executable), cwd=tmp_path)
    try:
        assert transport.request("echo", {}, timeout=2) == {"ok": True}
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


@pytest.mark.parametrize(
    ("body", "error_type"),
    [
        ("import sys; sys.stdout.write('not-json\\n'); sys.stdout.flush()", ACPProcessProtocolError),
        ("import sys; sys.exit(0)", ACPProcessError),
        ("import time; time.sleep(2)", ACPProcessTimeoutError),
    ],
)
def test_transport_classifies_protocol_eof_and_timeout(
    tmp_path: Path, body: str, error_type: type[Exception]
) -> None:
    transport = _spawn_script(tmp_path, body)
    try:
        with pytest.raises(error_type):
            transport.request("test", {}, timeout=0.05)
    finally:
        transport.terminate(grace_seconds=0.1)
        transport.close()


def test_server_request_with_same_id_is_handled_before_response() -> None:
    body = textwrap.dedent(
        """
        import json
        import sys

        def write(message):
            sys.stdout.write(json.dumps(message) + "\\n")
            sys.stdout.flush()

        request = json.loads(sys.stdin.readline())
        write({
            "jsonrpc": "2.0",
            "id": request["id"],
            "method": "session/request_permission",
            "params": {"permission": "workspace"},
        })
        permission = json.loads(sys.stdin.readline())
        write({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"permission": permission["result"]},
        })
        """
    ).strip()
    transport = _spawn_script(Path.cwd(), body)
    try:
        result = transport.request("roundtrip", {}, timeout=2)
        assert result == {"permission": {"outcome": "reject"}}
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_server_request_handler_is_bounded_and_can_reply() -> None:
    body = textwrap.dedent(
        """
        import json
        import sys

        def write(message):
            sys.stdout.write(json.dumps(message) + "\\n")
            sys.stdout.flush()

        request = json.loads(sys.stdin.readline())
        write({"id": request["id"], "method": "workspace/read", "params": {}})
        response = json.loads(sys.stdin.readline())
        write({"id": request["id"], "result": {"reply": response["result"]}})
        """
    ).strip()
    seen: list[str] = []

    def handler(message: Mapping[str, object]) -> dict[str, object]:
        seen.append(str(message["method"]))
        return {"allowed": True}

    transport = ACPProcessTransport.spawn_argv(
        (sys.executable, "-c", body),
        cwd=Path.cwd(),
        env={"PATH": os.environ.get("PATH", "")},
        server_request_handler=handler,
        server_request_timeout_seconds=0.2,
    )
    try:
        assert transport.request("roundtrip", {}, timeout=2) == {
            "reply": {"allowed": True}
        }
        assert seen == ["workspace/read"]
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_server_request_handler_timeout_returns_protocol_error() -> None:
    body = textwrap.dedent(
        """
        import json
        import sys

        def write(message):
            sys.stdout.write(json.dumps(message) + "\\n")
            sys.stdout.flush()

        request = json.loads(sys.stdin.readline())
        write({"id": request["id"], "method": "workspace/read", "params": {}})
        response = json.loads(sys.stdin.readline())
        write({"id": request["id"], "result": response})
        """
    ).strip()

    def handler(_message: Mapping[str, object]) -> dict[str, object]:
        import time

        time.sleep(1)
        return {"late": True}

    transport = ACPProcessTransport.spawn_argv(
        (sys.executable, "-c", body),
        cwd=Path.cwd(),
        env={"PATH": os.environ.get("PATH", "")},
        server_request_handler=handler,
        server_request_timeout_seconds=0.05,
    )
    try:
        assert transport.request("roundtrip", {}, timeout=1) == {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32001,
                "message": "ACP server request timed out",
            },
        }
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_owned_process_group_and_stderr_are_bounded(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        import json
        import sys
        import time

        sys.stderr.write("token=super-secret " * 1000)
        sys.stderr.flush()
        for raw in sys.stdin:
            request = json.loads(raw)
            sys.stdout.write(json.dumps({"id": request["id"], "result": {}}) + "\\n")
            sys.stdout.flush()
            time.sleep(0.2)
        """
    ).strip()
    transport = ACPProcessTransport.spawn_argv(
        (sys.executable, "-c", body),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        stderr_limit_bytes=128,
    )
    try:
        assert transport.request("test", {}, timeout=2) == {}
        assert transport.terminate(grace_seconds=0.2) is True
        assert len(transport.stderr_text.encode()) <= 128
        assert "super-secret" not in transport.stderr_text
    finally:
        transport.close()
