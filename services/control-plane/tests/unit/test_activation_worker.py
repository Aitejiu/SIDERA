from __future__ import annotations

from uuid import UUID

from sidera_control_plane.activation_worker import ActivationWorker, summary_json
from sidera_control_plane.application.activation_reconciler import (
    ActivationReconcileResult,
)


class FakeReconciler:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run_once(self, limit: int) -> ActivationReconcileResult:
        self.calls.append(limit)
        return ActivationReconcileResult(
            examined=2,
            readied=1,
            materialized=1,
            deferred=1,
            failed=0,
            execution_ids=(UUID("70000000-0000-0000-0000-000000000001"),),
        )


def test_activation_worker_delegates_and_reports_structured_summary() -> None:
    reconciler = FakeReconciler()
    worker = ActivationWorker(reconciler)

    result = worker.run_once(7)

    assert reconciler.calls == [7]
    assert result.materialized == 1
    assert summary_json(result) == (
        '{"component":"activation-worker","deferred":1,"examined":2,'
        '"execution_ids":["70000000-0000-0000-0000-000000000001"],"failed":0,'
        '"materialized":1,"readied":1,"status":"ok"}'
    )
