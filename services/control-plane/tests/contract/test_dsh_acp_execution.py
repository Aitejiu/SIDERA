from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sidera_control_plane.domain.gateway import DeliveryOutcome
from tests.unit.test_dsh_acp import _adapter, _spec, _workspace


def test_dsh_execution_contract_uses_one_process_per_execution(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    profile = adapter.probe({})
    assert profile.context_package_versions == (1, 2)
    assert profile.handoff_checkpoint_versions == ()
    assert profile.handoff_session_context_versions == ()
    assert profile.handoff_writer_gate_versions == ()
    assert profile.handoff_inspection is False

    spec = _spec()
    (tmp_path / str(spec.execution_id)).mkdir()
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())
    delivered = adapter.send_input(
        handle,
        "execute one bounded prompt",
        (),
        operation_id=uuid4(),
    )
    assert delivered.outcome is DeliveryOutcome.DELIVERED
    assert delivered.execution_session_id == handle.execution_session_id
    assert any(
        event.event_type == "progress.updated"
        for event in adapter.read_events(handle)
    )

    attachment = adapter.send_input(
        handle,
        "cannot resolve an opaque attachment",
        ("artifact://body-not-materialized",),
        operation_id=uuid4(),
    )
    assert attachment.outcome is DeliveryOutcome.FAILED
    assert attachment.failure_code == "attachment_body_unavailable"
    stopped = adapter.stop_session(handle, operation_id=uuid4())
    assert stopped.outcome is DeliveryOutcome.DELIVERED
    assert adapter.active_processes() == ()
