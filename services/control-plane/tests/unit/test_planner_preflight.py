import stat
import textwrap
from pathlib import Path

from sidera_control_plane.planner_preflight import run_planner_preflight


def _fake_opencode(tmp_path: Path) -> Path:
    script = tmp_path / "fake-opencode-preflight"
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import sys

            if len(sys.argv) > 1 and sys.argv[1] == "--version":
                print("opencode 0.0.0-planner")
                raise SystemExit(0)

            def write(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()

            for raw in sys.stdin:
                request = json.loads(raw)
                if request.get("method") == "initialize":
                    write({"jsonrpc": "2.0", "id": request["id"], "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {"loadSession": False},
                    }})
                elif request.get("method") == "session/new":
                    write({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": "preflight"}})
            """
        ).strip()
        + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_planner_preflight_is_independent_and_reports_capability(tmp_path: Path) -> None:
    root = tmp_path / "planner-preflight"
    report = run_planner_preflight(
        executable=str(_fake_opencode(tmp_path)),
        expected_version="0.0.0-planner",
        temp_root=root,
        timeout_seconds=2,
    )

    assert report.status == "ok"
    assert report.version == "opencode 0.0.0-planner"
    assert report.capabilities["structured_output"] == "json"
    assert report.capabilities["schema_round_trip"] == "ok"
    assert not root.exists()


def test_planner_preflight_does_not_fallback_for_missing_executable() -> None:
    report = run_planner_preflight(executable="/missing/opencode")

    assert report.status == "failed"
    assert report.capabilities["status"] == "unavailable"
