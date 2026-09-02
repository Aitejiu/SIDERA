from __future__ import annotations

import pytest

from sidera_control_plane.persistence.runtime_metrics_store import (
    SqlAlchemyRuntimeMetricsStore,
)

pytestmark = pytest.mark.integration


def test_collaboration_metrics_are_bounded_and_use_only_allowlisted_labels(
    database, workspace_id
) -> None:
    view = SqlAlchemyRuntimeMetricsStore(database).get_collaboration_metrics(
        workspace_id
    )

    assert view.workspace_id == workspace_id
    assert view.samples
    assert all(sample.labels == tuple(sorted(sample.labels)) for sample in view.samples)
    assert all(
        set(dict(sample.labels)) <= {"outcome", "status", "reason"}
        for sample in view.samples
    )
    assert all(
        workspace_id.__str__() not in str(dict(sample.labels))
        for sample in view.samples
    )
    assert {
        sample.name for sample in view.samples
    } >= {
        "sidera_collaboration_evaluations_total",
        "sidera_collaboration_runs",
        "sidera_collaboration_decisions_total",
        "sidera_collaboration_releases_total",
    }
