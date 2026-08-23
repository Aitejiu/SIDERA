import stat
import textwrap
from pathlib import Path

import pytest

from sidera_control_plane.opencode_preflight import main, run_preflight


def _fake_opencode(tmp_path: Path) -> Path:
    script = tmp_path / "fake-opencode"
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import sys

            if len(sys.argv) > 1 and sys.argv[1] == "--version":
                print("opencode 0.0.0-preflight")
                raise SystemExit(0)

            def write(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()

            for raw in sys.stdin:
                request = json.loads(raw)
                if request.get("method") == "initialize":
                    write({
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "protocolVersion": 1,
                            "agentCapabilities": {"loadSession": False},
                        },
                    })
                elif request.get("method") == "session/new":
                    write({
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"sessionId": "preflight-session"},
                    })
            """
        ).strip()
        + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_preflight_reports_version_and_acp_capabilities(tmp_path: Path) -> None:
    report = run_preflight(
        executable=str(_fake_opencode(tmp_path)),
        expected_version="0.0.0-preflight",
        timeout_seconds=2,
    )

    assert report.status == "ok"
    assert report.version == "opencode 0.0.0-preflight"
    assert report.capability_profile is not None
    assert report.as_dict()["capabilities"] == {
        "status": "available",
        "input": "queued",
        "idempotency": "none",
        "resume": "same-process",
        "stop": "exact-execution",
        "sandbox": "filesystem",
        "stability": "experimental",
    }


def test_preflight_does_not_fallback_when_executable_is_missing() -> None:
    report = run_preflight(executable="/missing/opencode")

    assert report.status == "failed"
    assert report.resolved_executable is None
    assert report.capability_profile is None
    assert report.as_dict()["capabilities"] == {
        "status": "unavailable",
        "reason": "OpenCode executable was not found: /missing/opencode",
    }


def test_preflight_command_does_not_require_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SIDERA_DATABASE_URL", raising=False)
    monkeypatch.setenv("SIDERA_OPENCODE_EXECUTABLE", str(_fake_opencode(tmp_path)))
    monkeypatch.setenv("SIDERA_OPENCODE_EXPECTED_VERSION", "0.0.0-preflight")
    monkeypatch.setenv("SIDERA_OPENCODE_PROBE_TIMEOUT_SECONDS", "2")

    assert main([]) == 0
