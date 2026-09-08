"""Explicit entry point for the bounded real R6 result-integration run.

The default path is intentionally collectable but skipped. A real provider run
requires a local gate record and both authorization switches; this test does
not infer approval from a configured credential.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sidera_control_plane.config import Settings
from sidera_control_plane.result_readiness import authorize_real_result_run


def test_real_opencode_result_integration_entry() -> None:
    if not _real_run_requested():
        pytest.skip(
            "set both real result-integration authorization switches to collect "
            "the bounded OpenCode entry"
        )

    gate_path = os.environ.get("SIDERA_REAL_RESULT_LOCAL_GATE_FILE")
    if gate_path is None or not gate_path.strip():
        pytest.fail("SIDERA_REAL_RESULT_LOCAL_GATE_FILE is required for a real run")
    try:
        record = json.loads(Path(gate_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError("the local real-result gate record is invalid") from exc

    authorization = authorize_real_result_run(Settings(), record)
    assert authorization.reserve_execution(0) == 1

    pytest.skip(
        "real OpenCode result integration is opt-in and was not executed in this "
        "provider-free verification run"
    )


def _real_run_requested() -> bool:
    return (
        os.environ.get("SIDERA_RUN_REAL_RESULT_INTEGRATION") == "1"
        and os.environ.get("SIDERA_REAL_RESULT_INTEGRATION_AUTHORIZED") == "1"
    )
