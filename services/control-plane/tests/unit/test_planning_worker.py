from __future__ import annotations

import json

import pytest

from sidera_control_plane.application.planning_reconciler import (
    PlanningReconcileResult,
)
from sidera_control_plane.application.planning_runner import PlanningBatchResult
from sidera_control_plane.config import Settings
from sidera_control_plane.planning_worker import (
    PlanningWorker,
    PlanningWorkerSummary,
    summary_json,
)


class FakeReconciler:
    def run_once(self, limit: int) -> PlanningReconcileResult:
        assert limit == 10
        return PlanningReconcileResult(
            expired=1,
            retried=1,
            blocked=0,
            failed=0,
        )


class FakeRunner:
    def run_once(self, limit: int) -> PlanningBatchResult:
        assert limit == 10
        return PlanningBatchResult(
            claimed=2,
            succeeded=1,
            clarifications=1,
            failed=0,
            blocked=0,
            outcomes=(),
        )


def test_worker_runs_recovery_before_planning_batch() -> None:
    summary = PlanningWorker(
        reconciler=FakeReconciler(),
        runner=FakeRunner(),
    ).run_once(limit=10)

    assert isinstance(summary, PlanningWorkerSummary)
    assert summary.claimed == 2
    assert summary.succeeded == 1
    assert summary.clarifications == 1
    assert summary.retried == 1
    assert summary.blocked == 0
    assert summary.failed == 0


def test_worker_summary_is_one_line_json() -> None:
    encoded = summary_json(
        PlanningWorkerSummary(
            claimed=2,
            succeeded=1,
            clarifications=1,
            retried=1,
            blocked=0,
            failed=0,
        )
    )

    assert "\n" not in encoded
    assert json.loads(encoded)["component"] == "planning-worker"


def test_planning_settings_require_renewal_before_half_the_ttl() -> None:
    settings = Settings(database_url="postgresql+psycopg://test")
    assert settings.planning_batch_size == 10
    assert settings.planning_lease_ttl_seconds == 60
    assert settings.planning_lease_renew_interval_seconds == 10.0

    with pytest.raises(ValueError, match="less than half"):
        Settings(
            database_url="postgresql+psycopg://test",
            planning_lease_ttl_seconds=60,
            planning_lease_renew_interval_seconds=30,
        )
