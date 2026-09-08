"""Fixed real-notification entry; it never accepts an arbitrary shell command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ACCEPTANCE_SCRIPT = ROOT / "scripts" / "r6-basic-acceptance.py"


def test_basic_real_notification_entry() -> None:
    """Consume evidence from the normal gateway run when the gate is opted in."""

    if os.environ.get("SIDERA_RUN_REAL_NOTIFICATION_GATE") != "1":
        pytest.skip("real notification gate is opt-in")

    manifest_value = os.environ.get("SIDERA_BASIC_NOTIFICATION_MANIFEST", "")
    if not manifest_value:
        pytest.fail("SIDERA_BASIC_NOTIFICATION_MANIFEST is required")
    manifest_path = Path(manifest_value).expanduser()
    if not manifest_path.is_file():
        pytest.fail("basic notification manifest is unavailable")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        pytest.fail(f"basic notification manifest is invalid: {type(exc).__name__}")
    route_reference = manifest.get("notification_route_ref")
    if not isinstance(route_reference, str) or not route_reference.startswith(
        ("env:", "module:")
    ):
        pytest.fail("manifest must select an explicit notification route reference")

    result = subprocess.run(
        [
            sys.executable,
            str(ACCEPTANCE_SCRIPT),
            "--manifest",
            str(manifest_path),
            "--scenario",
            "notification",
            "--mode",
            "real",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "ANTHROPIC_API_KEY",
                "DEEPSEEK_API_KEY",
                "OPENAI_API_KEY",
                "OPENCODE_API_KEY",
            }
        },
    )
    if result.returncode != 0:
        pytest.fail(
            "notification evidence was not PASS: "
            f"{_safe_result_summary(result.stdout)}"
        )
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "PASS"
    assert evidence["scenario"] == "notification"


def _safe_result_summary(output: str) -> str:
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return "invalid evidence output"
    if not isinstance(value, dict):
        return "invalid evidence shape"
    return json.dumps(
        {
            "status": value.get("status"),
            "failure_code": value.get("failure_code"),
            "check_codes": value.get("check_codes", []),
        },
        sort_keys=True,
    )
