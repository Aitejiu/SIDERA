import stat
import textwrap
import time
from pathlib import Path

import pytest

from sidera_control_plane.adapters.acp_process import (
    ACPProcessProtocolError,
    ACPProcessTimeoutError,
    ACPProcessTransport,
)


def _fake_acp_process(tmp_path: Path) -> Path:
    script = tmp_path / "fake-acp-process"
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import sys
            import time

            def write(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()

            for raw in sys.stdin:
                request = json.loads(raw)
                method = request.get("method")
                request_id = request.get("id")
                if method == "echo":
                    write({"jsonrpc": "2.0", "method": "server/note", "params": {"ok": True}})
                    write({"jsonrpc": "2.0", "id": request_id, "result": request.get("params", {})})
                elif method == "hang":
                    time.sleep(30)
                elif method == "malformed":
                    sys.stdout.write("not-json\\n")
                    sys.stdout.flush()
                elif method == "stderr":
                    sys.stderr.write("token=secret-value " + "x" * 10000)
                    sys.stderr.flush()
                    write({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
            """
        ).strip()
        + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_transport_matches_request_ids_and_preserves_notifications(tmp_path: Path) -> None:
    transport = ACPProcessTransport.spawn(
        str(_fake_acp_process(tmp_path)),
        cwd=tmp_path,
        stderr_limit_bytes=128,
    )
    try:
        result = transport.request("echo", {"value": 7}, timeout=1)

        assert result == {"value": 7}
        notifications = transport.drain_notifications()
        assert notifications == (
            {"jsonrpc": "2.0", "method": "server/note", "params": {"ok": True}},
        )
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_transport_rejects_malformed_json_and_times_out(tmp_path: Path) -> None:
    transport = ACPProcessTransport.spawn(
        str(_fake_acp_process(tmp_path)),
        cwd=tmp_path,
    )
    try:
        with pytest.raises(ACPProcessProtocolError, match="invalid JSON"):
            transport.request("malformed", {}, timeout=1)
        with pytest.raises(ACPProcessTimeoutError):
            transport.request("hang", {}, timeout=0.05)
    finally:
        assert transport.terminate(grace_seconds=0.2)
        transport.close()


def test_transport_bounds_stderr_and_terminates_owned_process(tmp_path: Path) -> None:
    transport = ACPProcessTransport.spawn(
        str(_fake_acp_process(tmp_path)),
        cwd=tmp_path,
        stderr_limit_bytes=64,
    )
    try:
        assert transport.request("stderr", {}, timeout=1) == {"ok": True}
        deadline = time.monotonic() + 1
        while len(transport.stderr_text) == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(transport.stderr_text.encode()) <= 64
        assert "secret-value" not in transport.stderr_text
        assert transport.terminate(grace_seconds=0.2)
        assert transport.process.poll() is not None
    finally:
        transport.close()
