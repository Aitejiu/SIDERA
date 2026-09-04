from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.notification_metrics import (
    NOTIFICATION_METRIC_LABELS,
    NOTIFICATION_METRIC_NAMES,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.notification_metrics_store import (
    SqlAlchemyNotificationMetricsStore,
)

pytestmark = pytest.mark.integration


EXPECTED_METRICS = {
    "sidera_notification_reconcile_total",
    "sidera_notification_reconcile_lag",
    "sidera_notification_reconcile_oldest_seconds",
    "sidera_notification_fact_total",
    "sidera_notification_thread_count",
    "sidera_notification_delivery_count",
    "sidera_notification_delivery_attempt_total",
    "sidera_notification_delivery_oldest_seconds",
    "sidera_notification_resync_total",
}


def test_notification_metrics_are_typed_bounded_and_workspace_scoped(
    database: Database,
    database_url: str,
    workspace_id: UUID,
) -> None:
    view = SqlAlchemyNotificationMetricsStore(database).get_metrics(workspace_id)

    assert view.workspace_id == workspace_id
    assert NOTIFICATION_METRIC_NAMES == EXPECTED_METRICS
    assert {sample.name for sample in view.samples} >= EXPECTED_METRICS
    assert all(sample.labels == tuple(sorted(sample.labels)) for sample in view.samples)
    assert all(
        set(dict(sample.labels)) == NOTIFICATION_METRIC_LABELS[sample.name]
        for sample in view.samples
    )
    assert all(workspace_id.__str__() not in str(dict(sample.labels)) for sample in view.samples)
    assert all("secret" not in str(sample).lower() for sample in view.samples)
    assert all("token" not in str(sample).lower() for sample in view.samples)

    with TestClient(create_app(Settings(database_url=database_url))) as client:
        metrics_response = client.get(
            f"/api/v1/workspaces/{workspace_id}/notification-metrics"
        )
        readiness_response = client.get(
            f"/api/v1/workspaces/{workspace_id}/notification-readiness"
        )

    assert metrics_response.status_code == 200, metrics_response.text
    assert readiness_response.status_code == 200, readiness_response.text
    assert {item["name"] for item in metrics_response.json()["samples"]} >= EXPECTED_METRICS
    assert readiness_response.json()["component"] == "sidera-notification-worker"
    assert "secret" not in readiness_response.text.lower()
    assert "token" not in readiness_response.text.lower()
