from datetime import UTC, datetime

import pytest

from sidera_control_plane.application.agent_worker_adapter import project_run_event
from sidera_control_plane.application.event_ingestor import (
    _redact_payload,
    _terminal_observation,
)
from sidera_control_plane.domain.gateway import AdapterEvent
from tests.unit import test_native_terminal_stop

terminal_stop_factory = test_native_terminal_stop.terminal_stop_factory


def test_event_payload_redacts_secrets_and_bounds_large_values() -> None:
    payload = _redact_payload(
        {
            "message": "x" * 20_000,
            "api_key": "do-not-store",
            "nested": {"authorization": "Bearer private"},
        }
    )

    assert payload["api_key"] == "[redacted]"
    assert payload["nested"] == {"authorization": "[redacted]"}
    message = payload["message"]
    assert isinstance(message, dict)
    assert message["truncated"] is True
    assert message["size_bytes"] == 20_000


def test_terminal_observation_only_accepts_control_plane_terminal_events() -> None:
    completed = _terminal_observation(
        AdapterEvent(
            event_type="execution.succeeded",
            sequence=4,
            payload={"api_key": "private", "summary": "ready"},
            occurred_at=datetime.now(UTC),
        )
    )
    progress = _terminal_observation(
        AdapterEvent(
            event_type="progress.updated",
            sequence=5,
            payload={"progress": 1},
            occurred_at=datetime.now(UTC),
        )
    )

    assert completed is not None
    assert completed.outcome == "succeeded"
    assert completed.sequence == 4
    assert completed.payload["api_key"] == "[redacted]"
    assert progress is None


def test_turn_completion_is_persisted_without_becoming_terminal_observation() -> None:
    turn = _terminal_observation(
        AdapterEvent(
            event_type="turn.completed",
            sequence=6,
            payload={
                "schema_version": 1,
                "operation_id": "00000000-0000-0000-0000-000000000001",
                "stop_reason": "end_turn",
            },
            occurred_at=datetime.now(UTC),
        )
    )

    assert turn is None


@pytest.mark.parametrize("terminal", ("succeeded", "failed"))
def test_original_native_stop_preserves_terminal_business_history(
    terminal_stop_factory,
    terminal,
):
    s = terminal_stop_factory(terminal=terminal, closed=False, ingest=True)
    before = (s.execution.status, s.task.status, s.task.version)
    event = project_run_event(s.native_events[0])

    report = s.gateway._event_ingestor.ingest_original_page_in_uow(
        s.uow,
        expected=s.link,
        principal_id=s.authority.principal_id,
        events=(event,),
    )

    assert s.link.status == terminal and s.link.event_cursor == 2
    assert s.session.last_sequence == 2
    assert report.events[0].event_type == "native.stop.stopped"
    assert not report.terminal_observations
    assert _terminal_observation(event) is None
    assert (s.execution.status, s.task.status, s.task.version) == before


@pytest.mark.parametrize("kind", ("stop.accepted", "stop.unknown", "stop.failed"))
def test_unconfirmed_original_native_stop_does_not_close_running_link(
    terminal_stop_factory,
    kind,
):
    s = terminal_stop_factory(terminal="running", closed=False, ingest=True)
    event = project_run_event(s.native_events[0].model_copy(update={"kind": kind}))

    report = s.gateway._event_ingestor.ingest_original_page_in_uow(
        s.uow,
        expected=s.link,
        principal_id=s.authority.principal_id,
        events=(event,),
    )

    assert s.link.status == "running" and s.link.event_cursor == 2
    assert s.session.status.value == "active" and s.session.last_sequence == 2
    assert report.events[0].event_type == f"native.{kind}"
    assert not report.terminal_observations
