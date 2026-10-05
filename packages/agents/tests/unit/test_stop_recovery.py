from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sidera_agents.contracts import (
    InputStatus,
    InterruptStatus,
    NativeCancelOutcome,
    NativeCancelState,
    NativeCloseOutcome,
    NativeCloseState,
    NativeHandle,
    NativeInspection,
    NativeOutcomeStatus,
    NativeProcessState,
    NativeResponseState,
    RecoveryStatus,
    RunInput,
    RunInterrupt,
    RunPolicy,
    RunRecover,
    RunState,
    RunStop,
    StartStatus,
    StopStatus,
)
from sidera_agents.errors import AgentError


def _start(lab, *, policy=RunPolicy.CONVERSATION):
    receipt = lab.execution.start(lab.request(policy=policy.value))
    assert receipt.status is StartStatus.ACCEPTED
    assert receipt.run_id is not None
    return receipt


def _input(lab, run_id, text):
    return RunInput(
        workspace_id=lab.workspace_id,
        run_id=run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
        text=text,
        context=(),
    )


def _interrupt(lab, run_id, response_id, *, operation_id=None):
    return RunInterrupt(
        workspace_id=lab.workspace_id,
        run_id=run_id,
        operation_id=operation_id or uuid4(),
        grant_id=lab.grant_id,
        response_id=response_id,
    )


def test_stop_before_native_dispatch_finishes_without_opening(admission_lab):
    lab = admission_lab
    request = lab.request()
    start = lab.execution.start(request)
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    assert lab.execution.stop(stop).status is StopStatus.ACCEPTED
    stopped = lab.execution.get_stop(
        lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
    )
    assert stopped.status is StopStatus.STOPPED
    lab.pump()
    assert lab.execution.stop(stop) == stopped
    assert lab.execution.start(request).run_id == start.run_id
    assert lab.native.open_calls == []
    assert lab.native.prompt_calls == []
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.STOPPED
    )
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 0
    assert installation.session_count == 0
    independent = lab.execution.start(lab.request(text="use reclaimed capacity"))
    assert independent.status is StartStatus.ACCEPTED
    assert independent.run_id != start.run_id
    lab.pump()


def test_stop_during_start_publication_uses_local_no_dispatch_proof(
    admission_lab, monkeypatch
):
    lab = admission_lab
    request = lab.request()
    entered = Event()
    release = Event()
    original_admit = lab.store.admit_start

    def gated_admit(**kwargs):
        result = original_admit(**kwargs)
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("start admission was not released")
        return result

    monkeypatch.setattr(lab.store, "admit_start", gated_admit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        starting = pool.submit(lab.execution.start, request)
        try:
            assert entered.wait(timeout=1)
            accepted = lab.execution.get_start(
                lab.workspace_id, request.operation_id, grant_id=lab.grant_id
            ).receipt
            assert accepted.status is StartStatus.ACCEPTED
            stop = RunStop(
                workspace_id=lab.workspace_id,
                run_id=accepted.run_id,
                operation_id=uuid4(),
                grant_id=lab.grant_id,
            )
            assert lab.execution.stop(stop).status is StopStatus.ACCEPTED
            assert (
                lab.execution.get_stop(
                    lab.workspace_id,
                    accepted.run_id,
                    stop.operation_id,
                    grant_id=lab.grant_id,
                ).status
                is StopStatus.STOPPED
            )
        finally:
            release.set()
        assert starting.result(timeout=2).run_id == accepted.run_id
    lab.pump()
    assert lab.native.open_calls == []
    assert lab.native.prompt_calls == []
    assert (
        lab.execution.get_run(
            lab.workspace_id, accepted.run_id, grant_id=lab.grant_id
        ).state
        is RunState.STOPPED
    )
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 0
    assert installation.session_count == 0


@pytest.mark.parametrize(
    "terminal", (RunState.COMPLETED, RunState.FAILED, RunState.STOPPED)
)
def test_stop_of_terminal_run_preserves_state_and_native_history(
    admission_lab, monkeypatch, terminal
):
    lab = admission_lab
    start = _start(
        lab,
        policy=RunPolicy.CONVERSATION
        if terminal is RunState.STOPPED
        else RunPolicy.BOUNDED,
    )
    if terminal is RunState.FAILED:
        lab.native.outcomes[start.run_id] = NativeOutcomeStatus.FAILED
    lab.pump()
    if terminal is RunState.STOPPED:
        lab.execution.stop(
            RunStop(
                workspace_id=lab.workspace_id,
                run_id=start.run_id,
                operation_id=uuid4(),
                grant_id=lab.grant_id,
            )
        )
    before = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    assert before.state is terminal
    history = lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
    closes = []
    original_close = lab.native.close

    def tracked_close(handle):
        closes.append(handle.run_id)
        return original_close(handle)

    monkeypatch.setattr(lab.native, "close", tracked_close)
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    receipt = lab.execution.stop(stop)
    assert receipt.status is StopStatus.STOPPED
    assert lab.execution.stop(stop) == receipt
    assert (
        lab.execution.get_stop(
            lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
        )
        == receipt
    )
    after = lab.execution.get_run(lab.workspace_id, start.run_id, grant_id=lab.grant_id)
    assert after.state is terminal
    assert after.run_id == before.run_id
    assert after.reason_code == before.reason_code
    assert (
        lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)[: len(history)]
        == history
    )
    assert closes == []
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 1
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 0
    assert installation.session_count == 0


def test_restarted_start_without_local_no_dispatch_proof_keeps_unknown_capacity(
    admission_lab,
):
    from sidera_agents.bootstrap import create_agent_service
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader

    lab = admission_lab
    start = _start(lab, policy=RunPolicy.BOUNDED)
    lab.service.close()
    replacement = create_agent_service(
        lab.store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        replacement.poll_once()
        stop = RunStop(
            workspace_id=lab.workspace_id,
            run_id=start.run_id,
            operation_id=uuid4(),
            grant_id=lab.grant_id,
        )
        assert replacement.execution.stop(stop).status is StopStatus.ACCEPTED
        assert (
            replacement.execution.get_stop(
                lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
            ).status
            is StopStatus.UNKNOWN
        )
        assert lab.native.open_calls == []
        installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
        assert installation.active_count == 1
        assert installation.session_count == 1
    finally:
        replacement.close()


def _run_prompt_blocked(lab, monkeypatch, *, with_cancel=False):
    start = _start(lab)
    entered = Event()
    release = Event()
    original_prompt = lab.native.prompt
    original_open = lab.native.open

    if with_cancel:

        def open_with_cancel(request):
            handle = original_open(request).model_copy(
                update={
                    "capabilities": (
                        *lab.native.handles[request.run_id].capabilities,
                        "cancel_current_response",
                    )
                }
            )
            lab.native.handles[request.run_id] = handle
            return handle

        monkeypatch.setattr(lab.native, "open", open_with_cancel)

    def prompt(handle, assembled, response_id):
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("controlled prompt was not released")
        return original_prompt(handle, assembled, response_id)

    monkeypatch.setattr(lab.native, "prompt", prompt)
    return start, entered, release


def test_confirmed_interrupt_targets_one_response_and_same_run_continues(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start, entered, release = _run_prompt_blocked(lab, monkeypatch, with_cancel=True)
    lab.native.outcomes[start.run_id] = NativeOutcomeStatus.CANCELLED
    cancel_calls = []

    def confirmed_cancel(handle, response_id):
        cancel_calls.append((handle, response_id))
        return NativeCancelOutcome(
            response_id=response_id,
            status=NativeCancelState.CONFIRMED,
            reason_code=None,
        )

    monkeypatch.setattr(lab.native, "cancel", confirmed_cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        active = lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).active_response_id
        assert active is not None
        request = _interrupt(lab, start.run_id, active)
        receipt = lab.execution.interrupt(request)
        assert receipt.status is InterruptStatus.INTERRUPTED
        assert cancel_calls == [(lab.native.handles[start.run_id], active)]
        queued = _input(lab, start.run_id, "continue in same session")
        assert lab.execution.send_input(queued).status is InputStatus.ACCEPTED
        release.set()
        pumping.result(timeout=3)

    assert [call[1].text for call in lab.native.prompt_calls] == [
        "Review the synthetic change.",
        "continue in same session",
    ]
    assert len(lab.native.open_calls) == 1
    assert (
        lab.execution.get_input(
            lab.workspace_id, start.run_id, queued.operation_id, grant_id=lab.grant_id
        ).status
        is InputStatus.DELIVERED
    )
    assert (
        lab.execution.get_interrupt(
            lab.workspace_id,
            start.run_id,
            request.operation_id,
            grant_id=lab.grant_id,
        ).status
        is InterruptStatus.INTERRUPTED
    )
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.IDLE
    )


def test_interrupt_without_negotiated_capability_is_unsupported(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start, entered, release = _run_prompt_blocked(lab, monkeypatch)
    cancel_calls = []
    monkeypatch.setattr(lab.native, "cancel", lambda *args: cancel_calls.append(args))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        active = lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).active_response_id
        assert active is not None
        request = _interrupt(lab, start.run_id, active)
        receipt = lab.execution.interrupt(request)
        assert receipt.status is InterruptStatus.UNSUPPORTED
        assert cancel_calls == []
        release.set()
        pumping.result(timeout=3)

    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.IDLE
    )


def test_input_response_return_after_unknown_interrupt_cannot_reopen_run(
    admission_lab, monkeypatch
):
    lab = admission_lab
    original_open = lab.native.open

    def open_with_cancel(request):
        handle = original_open(request).model_copy(
            update={
                "capabilities": (
                    *lab.native.handles[request.run_id].capabilities,
                    "cancel_current_response",
                )
            }
        )
        lab.native.handles[request.run_id] = handle
        return handle

    monkeypatch.setattr(lab.native, "open", open_with_cancel)
    start = _start(lab)
    lab.pump()
    request = _input(lab, start.run_id, "active input response")
    assert lab.execution.send_input(request).status is InputStatus.ACCEPTED

    entered = Event()
    release = Event()
    original_prompt = lab.native.prompt

    def blocked_prompt(handle, assembled, response_id):
        if assembled.text == request.text:
            entered.set()
            if not release.wait(timeout=4):
                raise RuntimeError("controlled input prompt was not released")
        return original_prompt(handle, assembled, response_id)

    monkeypatch.setattr(lab.native, "prompt", blocked_prompt)
    monkeypatch.setattr(
        lab.native,
        "cancel",
        lambda handle, response_id: NativeCancelOutcome(
            response_id=response_id,
            status=NativeCancelState.UNKNOWN,
            reason_code="native_response_unknown",
        ),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        response_id = lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).active_response_id
        assert response_id is not None
        interrupted = lab.execution.interrupt(
            _interrupt(lab, start.run_id, response_id)
        )
        assert interrupted.status is InterruptStatus.UNKNOWN
        assert (
            lab.execution.get_input(
                lab.workspace_id,
                start.run_id,
                request.operation_id,
                grant_id=lab.grant_id,
            ).status
            is InputStatus.UNKNOWN
        )
        release.set()
        pumping.result(timeout=3)

    snapshot = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    assert snapshot.state is RunState.UNKNOWN
    assert not snapshot.input_open
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 2


def test_unknown_interrupt_fences_run_and_does_not_resume(admission_lab, monkeypatch):
    lab = admission_lab
    start, entered, release = _run_prompt_blocked(lab, monkeypatch, with_cancel=True)
    queued = _input(lab, start.run_id, "must be cleared when response becomes unknown")

    def unknown_cancel(handle, response_id):
        return NativeCancelOutcome(
            response_id=response_id,
            status=NativeCancelState.UNKNOWN,
            reason_code="native_response_unknown",
        )

    monkeypatch.setattr(lab.native, "cancel", unknown_cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        active = lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).active_response_id
        assert active is not None
        assert lab.execution.send_input(queued).status is InputStatus.ACCEPTED
        request = _interrupt(lab, start.run_id, active)
        assert lab.execution.interrupt(request).status is InterruptStatus.UNKNOWN
        queued_receipt = lab.execution.get_input(
            lab.workspace_id,
            start.run_id,
            queued.operation_id,
            grant_id=lab.grant_id,
        )
        assert queued_receipt.status is InputStatus.FAILED
        assert queued_receipt.reason_code == "run_fenced"
        assert (
            lab.execution.get_run(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            ).state
            is RunState.UNKNOWN
        )
        release.set()
        pumping.result(timeout=3)

    assert (
        lab.execution.get_interrupt(
            lab.workspace_id,
            start.run_id,
            request.operation_id,
            grant_id=lab.grant_id,
        ).status
        is InterruptStatus.UNKNOWN
    )
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 1


def test_late_interrupt_for_old_response_cannot_cancel_new_response(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    old_response_id = lab.native.prompt_calls[0][2]
    queued = _input(lab, start.run_id, "new active response")
    assert lab.execution.send_input(queued).status is InputStatus.ACCEPTED
    entered = Event()
    release = Event()
    original_prompt = lab.native.prompt

    def prompt(handle, assembled, response_id):
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("controlled prompt was not released")
        return original_prompt(handle, assembled, response_id)

    monkeypatch.setattr(lab.native, "prompt", prompt)
    cancel_calls = []
    monkeypatch.setattr(lab.native, "cancel", lambda *args: cancel_calls.append(args))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        receipt = lab.execution.interrupt(
            _interrupt(lab, start.run_id, old_response_id)
        )
        assert receipt.status is InterruptStatus.FAILED
        assert receipt.reason_code == "response_not_current"
        assert cancel_calls == []
        release.set()
        pumping.result(timeout=3)

    assert (
        lab.execution.get_input(
            lab.workspace_id, start.run_id, queued.operation_id, grant_id=lab.grant_id
        ).status
        is InputStatus.DELIVERED
    )
    assert len(lab.native.open_calls) == 1


def test_cancel_delayed_after_admission_cannot_reach_the_next_response(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab)
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    release_second = Event()
    cancel_entered = Event()
    allow_cancel = Event()
    original_open = lab.native.open
    original_cancel = lab.native.cancel
    response_count = 0

    def open_with_cancel(request):
        handle = original_open(request).model_copy(
            update={
                "capabilities": (
                    *lab.native.handles[request.run_id].capabilities,
                    "cancel_current_response",
                )
            }
        )
        lab.native.handles[request.run_id] = handle
        return handle

    def prompt(handle, assembled, response_id):
        nonlocal response_count
        response_count += 1
        lab.native.begin_prompt(handle, assembled, response_id)
        entered, release = (
            (first_entered, release_first)
            if response_count == 1
            else (second_entered, release_second)
        )
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("controlled prompt was not released")
        return lab.native.finish_prompt(
            handle, response_id, NativeOutcomeStatus.COMPLETED
        )

    def delayed_cancel(handle, response_id):
        cancel_entered.set()
        if not allow_cancel.wait(timeout=4):
            raise RuntimeError("controlled cancel was not released")
        return original_cancel(handle, response_id)

    monkeypatch.setattr(lab.native, "open", open_with_cancel)
    monkeypatch.setattr(lab.native, "prompt", prompt)
    monkeypatch.setattr(lab.native, "cancel", delayed_cancel)
    queued = _input(lab, start.run_id, "next response")

    with ThreadPoolExecutor(max_workers=2) as pool:
        pumping = pool.submit(lab.pump)
        assert first_entered.wait(timeout=2)
        first_response_id = lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).active_response_id
        assert first_response_id is not None
        assert lab.execution.send_input(queued).status is InputStatus.ACCEPTED
        interrupting = pool.submit(
            lab.execution.interrupt,
            _interrupt(lab, start.run_id, first_response_id),
        )
        assert cancel_entered.wait(timeout=2)

        release_first.set()
        assert second_entered.wait(timeout=2)
        second_response_id = lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).active_response_id
        assert second_response_id is not None
        assert second_response_id != first_response_id

        allow_cancel.set()
        receipt = interrupting.result(timeout=2)
        assert receipt.status is InterruptStatus.FAILED
        assert receipt.reason_code == "response_not_current"
        assert lab.native.cancel_side_effects == []

        release_second.set()
        pumping.result(timeout=3)

    assert (
        lab.execution.get_input(
            lab.workspace_id, start.run_id, queued.operation_id, grant_id=lab.grant_id
        ).status
        is InputStatus.DELIVERED
    )
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.IDLE
    )


def test_stop_closes_input_gate_and_fails_queued_inputs(admission_lab, monkeypatch):
    lab = admission_lab
    start, entered, release = _run_prompt_blocked(lab, monkeypatch, with_cancel=True)
    native_order = []
    post_close_receipts = []
    original_close = lab.native.close

    def confirmed_cancel(handle, response_id):
        native_order.append(("cancel", response_id))
        post_close_receipts.append(
            lab.execution.send_input(_input(lab, start.run_id, "during stop"))
        )
        release.set()
        return NativeCancelOutcome(
            response_id=response_id,
            status=NativeCancelState.CONFIRMED,
            reason_code=None,
        )

    def tracked_close(handle):
        native_order.append(("close", handle.run_id))
        return original_close(handle)

    monkeypatch.setattr(lab.native, "cancel", confirmed_cancel)
    monkeypatch.setattr(lab.native, "close", tracked_close)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        queued = _input(lab, start.run_id, "accepted before stop")
        assert lab.execution.send_input(queued).status is InputStatus.ACCEPTED
        stop = RunStop(
            workspace_id=lab.workspace_id,
            run_id=start.run_id,
            operation_id=uuid4(),
            grant_id=lab.grant_id,
        )
        assert lab.execution.stop(stop).status is StopStatus.ACCEPTED
        assert (
            lab.execution.get_input(
                lab.workspace_id,
                start.run_id,
                queued.operation_id,
                grant_id=lab.grant_id,
            ).status
            is InputStatus.FAILED
        )
        assert (
            lab.execution.get_input(
                lab.workspace_id,
                start.run_id,
                queued.operation_id,
                grant_id=lab.grant_id,
            ).reason_code
            == "input_cancelled"
        )
        assert [entry[0] for entry in native_order] == ["cancel", "close"]
        assert post_close_receipts[0].status is InputStatus.REJECTED
        assert post_close_receipts[0].reason_code == "run_closed"
        rejected = lab.execution.send_input(
            _input(lab, start.run_id, "after stop gate")
        )
        assert rejected.status is InputStatus.REJECTED
        assert rejected.reason_code == "run_closed"
        release.set()
        pumping.result(timeout=3)

    assert (
        lab.execution.get_stop(
            lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
        ).status
        is StopStatus.STOPPED
    )
    assert len(lab.native.open_calls) == 1


def test_stop_winning_open_handoff_prevents_initial_prompt_dispatch(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab)
    persisted = Event()
    release_handoff = Event()
    original_transition = lab.execution._transition_run

    def gated_transition(run_id, snapshot, **kwargs):
        result = original_transition(run_id, snapshot, **kwargs)
        if snapshot.state is RunState.RUNNING and kwargs.get("replace_native_handle"):
            persisted.set()
            if not release_handoff.wait(timeout=4):
                raise RuntimeError("native-open handoff was not released")
        return result

    monkeypatch.setattr(lab.execution, "_transition_run", gated_transition)
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pumping = pool.submit(lab.pump)
            assert persisted.wait(timeout=2)
            assert lab.execution.stop(stop).status is StopStatus.ACCEPTED
            assert (
                lab.execution.get_stop(
                    lab.workspace_id,
                    start.run_id,
                    stop.operation_id,
                    grant_id=lab.grant_id,
                ).status
                is StopStatus.STOPPED
            )
            release_handoff.set()
            pumping.result(timeout=3)

        snapshot = lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        )
        assert snapshot.state is RunState.STOPPED
        assert not snapshot.input_open
        assert lab.native.prompt_calls == []
        assert len(lab.native.open_calls) == 1
    finally:
        release_handoff.set()


def test_unknown_stop_retains_session_reservation(admission_lab, monkeypatch):
    lab = admission_lab
    start = _start(lab)
    lab.pump()

    def unknown_close(handle):
        lab.native.inspections[start.run_id] = NativeInspection(
            process_state=NativeProcessState.UNKNOWN,
            response_state=NativeResponseState.UNKNOWN,
            reason_code="native_identity_unverified",
        )
        return NativeCloseOutcome(
            status=NativeCloseState.UNKNOWN, reason_code="native_close_unknown"
        )

    monkeypatch.setattr(lab.native, "close", unknown_close)
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    assert lab.execution.stop(stop).status is StopStatus.ACCEPTED
    lab.pump()
    assert (
        lab.execution.get_stop(
            lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
        ).status
        is StopStatus.UNKNOWN
    )
    entry = lab.store.get_runtime_entry(lab.runtime.ref)
    assert entry.installation.session_count == 1
    assert len(lab.native.open_calls) == 1


def _unknown_original_stop(lab, monkeypatch):
    start = _start(lab)
    lab.pump()
    close_calls = []

    def unknown_close(handle):
        close_calls.append(handle.run_id)
        assert lab.native.inspect(handle).process_state is NativeProcessState.ALIVE
        return NativeCloseOutcome(
            status=NativeCloseState.UNKNOWN, reason_code="native_close_unknown"
        )

    monkeypatch.setattr(lab.native, "close", unknown_close)
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    assert lab.execution.stop(stop).status is StopStatus.ACCEPTED
    assert (
        lab.execution.get_stop(
            lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
        ).status
        is StopStatus.UNKNOWN
    )
    assert close_calls == [start.run_id]
    return start, stop, close_calls


def test_unknown_stop_is_confirmed_after_original_process_exit(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start, stop, close_calls = _unknown_original_stop(lab, monkeypatch)
    saved_handle = lab.store.get_native_handle(lab.workspace_id, start.run_id)
    original = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    for _ in range(3):
        lab.pump()
        assert (
            lab.execution.get_stop(
                lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
            ).status
            is StopStatus.UNKNOWN
        )
    assert close_calls == [start.run_id]
    lab.native.inspections[start.run_id] = NativeInspection(
        process_state=NativeProcessState.GONE,
        response_state=NativeResponseState.IDLE,
        reason_code=None,
    )
    lab.pump()
    receipt = lab.execution.get_stop(
        lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
    )
    assert receipt.status is StopStatus.STOPPED
    assert receipt.operation_id == stop.operation_id and receipt.run_id == start.run_id
    snapshot = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    assert snapshot.state is RunState.STOPPED and not snapshot.input_open
    assert snapshot.profile_ref == original.profile_ref
    assert snapshot.effective_config == original.effective_config
    assert lab.store.get_native_handle(lab.workspace_id, start.run_id) == saved_handle
    events = lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
    assert [
        event.kind for event in events if event.operation_id == stop.operation_id
    ] == ["stop.accepted", "stop.unknown", "stop.stopped"]
    for _ in range(3):
        lab.pump()
        assert (
            lab.execution.get_stop(
                lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
            )
            == receipt
        )
    assert lab.store.get_lifecycle_events(lab.workspace_id, start.run_id) == events
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    reservation = lab.store._run_reservations[start.run_id]
    assert installation.active_count == installation.session_count == 0
    assert not reservation.active and not reservation.session
    assert close_calls == [start.run_id]
    assert len(lab.native.open_calls) == len(lab.native.prompt_calls) == 1


@pytest.mark.parametrize("observation", ("alive", "unknown", "inspector_error"))
def test_unknown_stop_requires_authoritative_original_exit(
    admission_lab, monkeypatch, observation
):
    lab = admission_lab
    start, stop, close_calls = _unknown_original_stop(lab, monkeypatch)
    if observation == "inspector_error":

        def unavailable_inspection(handle):
            raise RuntimeError("controlled inspection unavailable")

        monkeypatch.setattr(lab.native, "inspect", unavailable_inspection)
    else:
        lab.native.inspections[start.run_id] = NativeInspection(
            process_state=NativeProcessState(observation),
            response_state=NativeResponseState.UNKNOWN,
            reason_code=None,
        )
    for _ in range(3):
        lab.pump()
        assert (
            lab.execution.get_stop(
                lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
            ).status
            is StopStatus.UNKNOWN
        )
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.UNKNOWN
    )
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.session_count == 1
    assert lab.store._run_reservations[start.run_id].session
    assert close_calls == [start.run_id]
    assert [
        event.kind
        for event in lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
        if event.operation_id == stop.operation_id
    ] == ["stop.accepted", "stop.unknown"]


def test_gone_without_original_stop_does_not_invent_stopped(admission_lab, monkeypatch):
    lab = admission_lab
    start = _start(lab)
    lab.native.outcomes[start.run_id] = NativeOutcomeStatus.UNKNOWN
    lab.pump()
    close_calls = []
    monkeypatch.setattr(lab.native, "close", lambda handle: close_calls.append(handle))
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.UNKNOWN
    )
    lab.native.inspections[start.run_id] = NativeInspection(
        process_state=NativeProcessState.GONE,
        response_state=NativeResponseState.IDLE,
        reason_code=None,
    )
    lab.pump()
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.UNKNOWN
    )
    assert not any(
        event.kind.startswith("stop.")
        for event in lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
    )
    assert close_calls == []
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == installation.session_count == 0


def test_foreign_handle_cannot_confirm_or_release_original_unknown_stop(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start, stop, close_calls = _unknown_original_stop(lab, monkeypatch)
    original = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    saved_handle = lab.store.get_native_handle(lab.workspace_id, start.run_id)
    foreign = NativeHandle(
        run_id=uuid4(),
        session_id="foreign-controlled-session",
        process_identity=saved_handle.process_identity,
        capabilities=saved_handle.capabilities,
    )
    unresolved = lab.store.list_unresolved_runs

    def foreign_unresolved():
        return tuple(
            (snapshot, foreign, terminal, reason)
            for snapshot, _, terminal, reason in unresolved()
        )

    inspected = []

    def foreign_gone(handle):
        inspected.append(handle)
        return NativeInspection(
            process_state=NativeProcessState.GONE,
            response_state=NativeResponseState.IDLE,
            reason_code=None,
        )

    monkeypatch.setattr(lab.store, "list_unresolved_runs", foreign_unresolved)
    monkeypatch.setattr(lab.native, "inspect", foreign_gone)
    lab.pump()
    assert inspected == []
    assert (
        lab.execution.get_run(lab.workspace_id, start.run_id, grant_id=lab.grant_id)
        == original
    )
    assert (
        lab.execution.get_stop(
            lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
        ).status
        is StopStatus.UNKNOWN
    )
    assert lab.store.get_runtime_entry(lab.runtime.ref).installation.session_count == 1
    assert lab.store._run_reservations[start.run_id].session
    assert close_calls == [start.run_id]


def test_unauthorized_original_stop_query_does_not_inspect_native(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start, stop, close_calls = _unknown_original_stop(lab, monkeypatch)
    inspected = []
    original_inspect = lab.native.inspect

    def tracked_inspection(handle):
        inspected.append(handle)
        return original_inspect(handle)

    monkeypatch.setattr(lab.native, "inspect", tracked_inspection)
    history = lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
    with pytest.raises(AgentError) as denied:
        lab.execution.get_stop(
            lab.workspace_id, start.run_id, stop.operation_id, grant_id=uuid4()
        )
    assert denied.value.code == "permission_denied"
    assert inspected == [] and close_calls == [start.run_id]
    assert lab.store.get_lifecycle_events(lab.workspace_id, start.run_id) == history
    assert lab.store._run_reservations[start.run_id].session


def test_revoked_grant_cannot_resume_but_trusted_owner_can_stop(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    lab.authorizer.workspace_grants.pop(lab.grant_id)
    with pytest.raises(AgentError) as denied:
        lab.execution.send_input(_input(lab, start.run_id, "must not resume"))
    assert denied.value.code == "permission_denied"

    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    assert lab.execution.stop(stop).status is StopStatus.ACCEPTED
    lab.pump()
    lab.authorizer.grant_workspace(lab.workspace_id, lab.grant_id)
    assert (
        lab.execution.get_stop(
            lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
        ).status
        is StopStatus.STOPPED
    )
    assert len(lab.native.open_calls) == 1


@pytest.mark.parametrize(
    ("negotiated_load", "expected_reason"),
    (
        (False, "session_load_unsupported"),
        (True, "session_load_unverified"),
    ),
)
def test_owner_restart_fences_queued_input_and_recovery_does_not_reload(
    admission_lab, monkeypatch, negotiated_load, expected_reason
):
    from sidera_agents.bootstrap import create_agent_service
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader

    lab = admission_lab
    if negotiated_load:
        original_open = lab.native.open

        def open_with_session_load(request):
            handle = original_open(request).model_copy(
                update={
                    "capabilities": (
                        *lab.native.handles[request.run_id].capabilities,
                        "session_load",
                    )
                }
            )
            lab.native.handles[request.run_id] = handle
            return handle

        monkeypatch.setattr(lab.native, "open", open_with_session_load)
    start = _start(lab)
    lab.pump()
    queued = _input(lab, start.run_id, "must not replay after restart")
    assert lab.execution.send_input(queued).status is InputStatus.ACCEPTED
    original_open_count = len(lab.native.open_calls)
    original_prompt_count = len(lab.native.prompt_calls)
    lab.service.close()
    replacement = create_agent_service(
        lab.store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        replacement.poll_once()
        assert (
            replacement.execution.get_input(
                lab.workspace_id,
                start.run_id,
                queued.operation_id,
                grant_id=lab.grant_id,
            ).status
            is InputStatus.FAILED
        )
        assert (
            replacement.execution.get_run(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            ).state
            is RunState.UNKNOWN
        )
        request = RunRecover(
            workspace_id=lab.workspace_id,
            run_id=start.run_id,
            operation_id=uuid4(),
            grant_id=lab.grant_id,
        )
        recovery = replacement.execution.recover(request)
        assert recovery.status is RecoveryStatus.UNSUPPORTED
        assert recovery.reason_code == expected_reason
        assert len(lab.native.open_calls) == original_open_count
        assert len(lab.native.prompt_calls) == original_prompt_count
        entry = lab.store.get_runtime_entry(lab.runtime.ref)
        assert entry.installation.session_count == 1
    finally:
        replacement.close()


def test_recovery_requires_original_grant_scope(admission_lab):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    replacement_grant = uuid4()
    lab.authorizer.grant_workspace(lab.workspace_id, replacement_grant)
    request = RunRecover(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=replacement_grant,
    )
    with pytest.raises(AgentError) as denied:
        lab.execution.recover(request)
    assert denied.value.code == "permission_denied"


def test_live_recovery_inspects_the_existing_run_handle(admission_lab, monkeypatch):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    handle = lab.store.get_native_handle(lab.workspace_id, start.run_id)
    assert handle is not None
    inspections = []
    original_inspect = lab.native.inspect

    def tracked_inspect(candidate):
        inspections.append(candidate)
        return original_inspect(candidate)

    monkeypatch.setattr(lab.native, "inspect", tracked_inspect)
    request = RunRecover(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    receipt = lab.execution.recover(request)
    repeated = lab.execution.recover(request)

    assert receipt.status is RecoveryStatus.RECOVERED
    assert repeated == receipt
    assert inspections == [handle]
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 1
