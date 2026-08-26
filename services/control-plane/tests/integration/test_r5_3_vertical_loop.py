from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "r5-3-vertical-loop.py"


def test_r5_3_deterministic_vertical_loop() -> None:
    if os.environ.get("SIDERA_RUN_R5_3_DETERMINISTIC") != "1":
        pytest.skip(
            "set SIDERA_RUN_R5_3_DETERMINISTIC=1 to run the local vertical gate"
        )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "deterministic"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "passed"
    assert summary["mode"] == "deterministic"
    assert summary["knowledge"]["initial_revision_number"] == 1
    assert summary["knowledge"]["worker_revision_number"] == 2
    assert summary["context"]["planning_revision_number"] == 2
    assert summary["context"]["proposal_confirmed_revision_number"] == 3
    assert summary["context"]["post_review_revision_number"] == 3
    assert summary["context"]["replan_revision_number"] == 2
    assert summary["execution"]["status"] == "succeeded"
    assert summary["candidate_submission"]["status"] == "submitted"
