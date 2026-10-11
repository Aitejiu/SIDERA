from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from sidera_agents.contracts import (
    InputStatus,
    RunInput,
    RunPolicy,
    RunState,
    RunStop,
    StopStatus,
)


def _input(lab, run_id, text):
    from uuid import uuid4

    return RunInput(
        workspace_id=lab.workspace_id,
        run_id=run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
        text=text,
        context=(),
    )


def _blocked_bounded_pump(lab, monkeypatch):
    from threading import Event

    start = lab.execution.start(lab.request(policy=RunPolicy.BOUNDED.value))
    assert start.run_id is not None
    entered = Event()
    release = Event()
    response_returned = Event()
    original = lab.native.prompt

    def prompt(handle, assembled, response_id):
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("controlled prompt was not released")
        outcome = original(handle, assembled, response_id)
        response_returned.set()
        return outcome

    monkeypatch.setattr(lab.native, "prompt", prompt)
    return start, entered, release, response_returned


def test_input_commit_wins_response_close_race_and_is_drained(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start, entered, release_prompt, response_returned = _blocked_bounded_pump(
        lab, monkeypatch
    )
    request = _input(lab, start.run_id, "accepted at close boundary")
    input_in_store = Event()
    allow_input_commit = Event()
    original_admit = lab.store.admit_input

    def gated_admit(*args, **kwargs):
        input_in_store.set()
        if not allow_input_commit.wait(timeout=4):
            raise RuntimeError("controlled input admission was not released")
        return original_admit(*args, **kwargs)

    monkeypatch.setattr(lab.store, "admit_input", gated_admit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        sending = pool.submit(lab.execution.send_input, request)
        assert input_in_store.wait(timeout=2)
        release_prompt.set()
        assert response_returned.wait(timeout=2)
        allow_input_commit.set()
        assert sending.result(timeout=3).status is InputStatus.ACCEPTED
        pumping.result(timeout=3)

    assert [call[1].text for call in lab.native.prompt_calls] == [
        "Review the synthetic change.",
        "accepted at close boundary",
    ]
    assert (
        lab.execution.get_input(
            lab.workspace_id, start.run_id, request.operation_id, grant_id=lab.grant_id
        ).status
        is InputStatus.DELIVERED
    )
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.COMPLETED
    )
    assert len(lab.native.open_calls) == 1


def test_close_commit_wins_input_race_and_later_input_is_run_closed(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start, entered, release_prompt, _ = _blocked_bounded_pump(lab, monkeypatch)
    close_in_store = Event()
    allow_close_commit = Event()
    original_close = lab.store.close_input_admission

    def gated_close(*args, **kwargs):
        close_in_store.set()
        if not allow_close_commit.wait(timeout=4):
            raise RuntimeError("controlled close was not released")
        return original_close(*args, **kwargs)

    monkeypatch.setattr(lab.store, "close_input_admission", gated_close)
    input_started = Event()
    original_send = lab.execution.send_input

    def tracked_send(request):
        input_started.set()
        return original_send(request)

    monkeypatch.setattr(lab.execution, "send_input", tracked_send)
    request = _input(lab, start.run_id, "after closing wins")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        release_prompt.set()
        assert close_in_store.wait(timeout=2)
        sending = pool.submit(lab.execution.send_input, request)
        assert input_started.wait(timeout=2)
        allow_close_commit.set()
        pumping.result(timeout=3)
        receipt = sending.result(timeout=3)

    assert receipt.status is InputStatus.REJECTED
    assert receipt.reason_code == "run_closed"
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.COMPLETED
    )
    assert len(lab.native.open_calls) == 1


def test_stop_keeps_late_input_response_closed_until_native_close_finishes(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = lab.execution.start(lab.request(policy=RunPolicy.CONVERSATION.value))
    assert start.run_id is not None
    lab.pump()

    request = _input(lab, start.run_id, "input completing during stop")
    prompt_entered = Event()
    release_prompt = Event()
    original_prompt = lab.native.prompt

    def blocked_prompt(handle, assembled, response_id):
        if assembled.text == request.text:
            prompt_entered.set()
            if not release_prompt.wait(timeout=4):
                raise RuntimeError("controlled input response was not released")
        return original_prompt(handle, assembled, response_id)

    close_entered = Event()
    release_close = Event()
    original_close = lab.native.close

    def blocked_close(handle):
        close_entered.set()
        if not release_close.wait(timeout=4):
            raise RuntimeError("controlled native close was not released")
        return original_close(handle)

    completion_states = []
    original_finish = lab.store.finish_conversation_response

    def tracked_finish(run_id, response_id, **kwargs):
        completion_states.append(
            lab.store.get_run_snapshot(lab.workspace_id, run_id).state
        )
        result = original_finish(run_id, response_id, **kwargs)
        return result

    monkeypatch.setattr(lab.native, "prompt", blocked_prompt)
    monkeypatch.setattr(lab.native, "close", blocked_close)
    monkeypatch.setattr(lab.store, "finish_conversation_response", tracked_finish)
    assert lab.execution.send_input(request).status is InputStatus.ACCEPTED
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pumping = pool.submit(lab.pump)
            assert prompt_entered.wait(timeout=2)
            stopping = pool.submit(lab.execution.stop, stop)
            assert close_entered.wait(timeout=2)
            assert (
                lab.execution.get_run(
                    lab.workspace_id, start.run_id, grant_id=lab.grant_id
                ).state
                is RunState.CLOSING
            )
            release_prompt.set()
            pumping.result(timeout=3)

            rejected = lab.execution.send_input(
                _input(lab, start.run_id, "must remain closed")
            )
            assert rejected.status is InputStatus.REJECTED
            assert rejected.reason_code == "run_closed"
            assert RunState.CLOSING in completion_states
            snapshot = lab.execution.get_run(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            )
            assert snapshot.state is RunState.CLOSING
            assert not snapshot.input_open
            reservations_after_response = lab.store.get_runtime_entry(
                lab.runtime.ref
            ).installation
            assert reservations_after_response.active_count == 0
            assert reservations_after_response.session_count == 1
            assert (
                lab.execution.get_input(
                    lab.workspace_id,
                    start.run_id,
                    request.operation_id,
                    grant_id=lab.grant_id,
                ).status
                is InputStatus.UNKNOWN
            )
            events = lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
            stop_sequence = next(
                event.sequence for event in events if event.kind == "stop.accepted"
            )
            assert not any(
                event.kind == "run.idle" and event.sequence > stop_sequence
                for event in events
            )

            release_close.set()
            assert stopping.result(timeout=3).status is StopStatus.ACCEPTED

        assert (
            lab.execution.get_stop(
                lab.workspace_id,
                start.run_id,
                stop.operation_id,
                grant_id=lab.grant_id,
            ).status
            is StopStatus.STOPPED
        )
        assert (
            lab.store.get_runtime_entry(lab.runtime.ref).installation.active_count == 0
        )
        assert (
            lab.store.get_runtime_entry(lab.runtime.ref).installation.session_count == 0
        )
    finally:
        release_prompt.set()
        release_close.set()
