from __future__ import annotations

import json
from datetime import UTC, datetime

from sidera_control_plane.result_worker import ResultWorkerRunResult, summary_json


def test_result_worker_summary_contains_safe_queue_observation() -> None:
    result = ResultWorkerRunResult(
        claimed=3,
        processed=3,
        completed=1,
        pending=1,
        blocked=0,
        unknown=1,
        failed=0,
        oldest_waiting_at=datetime(2026, 9, 8, 1, 2, 3, tzinfo=UTC),
        budget_exhausted=True,
        storage_missing=True,
    )

    payload = json.loads(summary_json(result))

    assert payload == {
        "blocked": 0,
        "budget_exhausted": True,
        "claimed": 3,
        "completed": 1,
        "component": "result-worker",
        "failed": 0,
        "oldest_waiting_at": "2026-09-08T01:02:03+00:00",
        "pending": 1,
        "processed": 3,
        "status": "ok",
        "storage_missing": True,
        "unknown": 1,
    }
    assert "source" not in summary_json(result)
    assert "key" not in summary_json(result)
