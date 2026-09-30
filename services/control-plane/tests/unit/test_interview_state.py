from __future__ import annotations

import multiprocessing
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.demo.state import (
    DemoStateStore,
    StateCorrupt,
    StateIdentityConflict,
)


def _reserve_in_process(
    root: str, run_id: str, operation_id: str, value: str, output: object
) -> None:
    store = DemoStateStore(Path(root), UUID(run_id))
    try:
        store.reserve("mutex/key", UUID(operation_id), {"value": value})
    except StateIdentityConflict:
        output.put("conflict")  # type: ignore[attr-defined]
    else:
        output.put("applied")  # type: ignore[attr-defined]


def test_reserve_and_settle_replay_survives_a_new_store_instance(tmp_path: Path) -> None:
    run_id, operation_id = uuid4(), uuid4()
    first = DemoStateStore(tmp_path, run_id)

    pending = first.reserve("step/validate", operation_id, {"phone": "optional"})
    settled = first.settle(
        "step/validate", operation_id, "applied", {"rows": 3}
    )
    second = DemoStateStore(tmp_path, run_id)

    replay = second.reserve("step/validate", operation_id, {"phone": "optional"})

    assert pending.status == "pending"
    assert settled.status == "applied"
    assert replay.status == "applied"
    assert replay.result == {"rows": 3}
    assert second.read("step/validate") == {
        "operation_id": str(operation_id),
        "request_hash": pending.request_hash,
        "status": "applied",
        "result": {"rows": 3},
    }


def test_same_operation_id_with_a_different_request_is_rejected(tmp_path: Path) -> None:
    store = DemoStateStore(tmp_path, uuid4())
    operation_id = uuid4()
    store.reserve("step/validate", operation_id, {"phone": "optional"})

    with pytest.raises(StateIdentityConflict, match="request_hash"):
        store.reserve("step/validate", operation_id, {"phone": "required"})


def test_same_operation_id_cannot_be_reused_for_another_key(tmp_path: Path) -> None:
    store = DemoStateStore(tmp_path, uuid4())
    operation_id = uuid4()
    store.reserve("step/validate", operation_id, {"phone": "optional"})

    with pytest.raises(StateIdentityConflict, match="operation_id"):
        store.reserve("step/report", operation_id, {"phone": "optional"})


def test_cross_process_reservations_have_one_winner(tmp_path: Path) -> None:
    run_id = uuid4()
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(
            target=_reserve_in_process,
            args=(str(tmp_path), str(run_id), str(uuid4()), str(index), output),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(output.get() for _ in processes) == ["applied", "conflict"]


def test_corrupt_json_is_not_treated_as_empty_state(tmp_path: Path) -> None:
    store = DemoStateStore(tmp_path, uuid4())
    store.reserve("step/validate", uuid4(), {"phone": "optional"})
    state_path = tmp_path / "operations.json"
    state_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(StateCorrupt, match="invalid_json"):
        store.read("step/validate")


def test_events_have_continuous_sequences_and_are_recoverable(tmp_path: Path) -> None:
    run_id, session_id = uuid4(), uuid4()
    first = DemoStateStore(tmp_path, run_id)
    first_event = first.append_event(session_id, uuid4(), "step_started", {"key": "validate"})
    second_event = first.append_event(session_id, uuid4(), "step_finished", {"rows": 3})
    restarted = DemoStateStore(tmp_path, run_id)

    recovered = restarted.events_after(session_id, first_event.sequence)

    assert first_event.sequence == 1
    assert second_event.sequence == 2
    assert [event.sequence for event in recovered] == [2]
    assert recovered[0].payload == {"rows": 3}


def test_duplicate_event_replay_does_not_advance_sequence(tmp_path: Path) -> None:
    store = DemoStateStore(tmp_path, uuid4())
    session_id, operation_id = uuid4(), uuid4()
    first = store.append_event(session_id, operation_id, "step_started", {"x": 1})
    replay = store.append_event(session_id, operation_id, "step_started", {"x": 1})

    assert replay == first
    assert store.events_after(session_id, 0) == (first,)


def test_event_replay_with_different_payload_is_rejected(tmp_path: Path) -> None:
    store = DemoStateStore(tmp_path, uuid4())
    session_id, operation_id = uuid4(), uuid4()
    store.append_event(session_id, operation_id, "step_started", {"x": 1})

    with pytest.raises(StateIdentityConflict, match="event"):
        store.append_event(session_id, operation_id, "step_started", {"x": 2})


def test_gates_are_local_and_persisted(tmp_path: Path) -> None:
    store = DemoStateStore(tmp_path, uuid4())
    store.set_gate("parallel", True)
    store.set_gate("governance", True)
    restarted = DemoStateStore(tmp_path, store.run_id)

    assert restarted.read_gate("parallel") is True
    assert restarted.read_gate("before-artifact") is False
    assert restarted.read_gate("governance") is True


def test_state_files_are_private_and_payload_is_canonical(tmp_path: Path) -> None:
    store = DemoStateStore(tmp_path, uuid4())
    record = store.reserve("step/validate", uuid4(), {"b": 2, "a": "中文"})

    assert record.request_hash == store.request_hash({"a": "中文", "b": 2})
    assert (tmp_path / "operations.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "events.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "gates.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "lock").stat().st_mode & 0o777 == 0o600
