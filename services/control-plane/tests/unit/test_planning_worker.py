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


def test_owned_planning_worker_observes_originals_before_new_planning_batch():
    from types import SimpleNamespace
    from uuid import uuid4

    calls = []
    workspace = uuid4()
    worker = PlanningWorker(
        reconciler=SimpleNamespace(
            run_once=lambda limit: (
                calls.append("expiry") or FakeReconciler().run_once(limit)
            )
        ),
        runner=SimpleNamespace(
            run_once=lambda limit: (
                calls.append("runner") or FakeRunner().run_once(limit)
            )
        ),
        native_reconciler=SimpleNamespace(
            run_once=lambda workspace_id, limit: calls.append(
                ("native", workspace_id, limit)
            )
        ),
        native_workspace_id=workspace,
        owner_check=lambda: True,
    )
    worker.run_once(10)
    assert calls == ["expiry", ("native", workspace, 10), "runner"]


def test_native_planning_tick_adapts_batch_201_only_for_observer():
    from types import SimpleNamespace
    from uuid import uuid4

    calls = []
    workspace = uuid4()

    def observe(workspace_id, limit):
        assert workspace_id == workspace and limit == 200
        calls.append(("native", limit))

    worker = PlanningWorker(
        reconciler=SimpleNamespace(
            run_once=lambda limit: (
                calls.append(("expiry", limit)) or FakeReconciler().run_once(10)
            )
        ),
        runner=SimpleNamespace(
            run_once=lambda limit: (
                calls.append(("runner", limit)) or FakeRunner().run_once(10)
            )
        ),
        native_reconciler=SimpleNamespace(run_once=observe),
        native_workspace_id=workspace,
    )
    worker.run_once(201)
    assert calls == [("expiry", 201), ("native", 200), ("runner", 201)]


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
