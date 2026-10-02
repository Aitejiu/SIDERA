from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sidera_agents.contracts import InputStatus, RunInput, RunPolicy, RunState


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
