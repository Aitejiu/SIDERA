from __future__ import annotations

import os

import pytest

if os.environ.get("SIDERA_INTERVIEW_DEMO_TEST_RUN") != "1":
    raise RuntimeError(
        "test_interview_notifications.py must run through the isolated demo test runner"
    )

from sidera_control_plane.demo.notification_sink import deliver_claimed_notification
from sidera_control_plane.demo.test_runner import validate_interview_test_context
from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


def test_notification_sink_persists_a_claimed_delivery_once(
    database: Database, clean_database: None
) -> None:
    del database, clean_database
    manifest = validate_interview_test_context(os.environ)
    from tests.unit.test_interview_notification_sink import _envelope

    envelope = _envelope()
    first = deliver_claimed_notification(manifest, envelope)
    second = deliver_claimed_notification(manifest, envelope)

    assert first == second == f"demo-message:{envelope.delivery_id}"
    files = tuple((manifest.run_root / "notifications").glob("message-*.json"))
    assert len(files) == 1
    assert files[0].is_file()
