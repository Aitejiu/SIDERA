import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from traceback import format_exception
from uuid import uuid4

import pytest

from sidera_agents.bootstrap import create_agent_service
from sidera_agents.contracts import (
    InputStatus,
    InterruptStatus,
    NativeCancelOutcome,
    NativeCancelState,
    NativeOutcomeStatus,
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
from sidera_agents.memory import MemoryAgentStore
from sidera_agents.ports import DenyContextSourceReader, DenySecretReader
from sidera_agents.runs import VerifiedNativeOpenFailure


@pytest.mark.parametrize("stop_count", (1, 2))
@pytest.mark.parametrize("gate_phase", ("open", "publication"))
def test_stop_during_native_open_returns_before_open_resolves(
    admission_lab, monkeypatch, stop_count, gate_phase
):
    lab = admission_lab
    start = lab.execution.start(lab.request())
    entered = Event()
    release = Event()
    closes = []
    original_open = lab.native.open
    original_close = lab.native.close
    original_transition = lab.execution._transition_run

    def gated_open(request):
        handle = original_open(request)
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("native open was not released")
        return handle

    def tracked_close(handle):
        closes.append(handle.run_id)
        return original_close(handle)

    def gated_transition(run_id, snapshot, **kwargs):
        if kwargs.get("replace_native_handle") and snapshot.state is RunState.RUNNING:
            entered.set()
            if not release.wait(timeout=4):
                raise RuntimeError("native publication was not released")
        return original_transition(run_id, snapshot, **kwargs)

    if gate_phase == "open":
        monkeypatch.setattr(lab.native, "open", gated_open)
    else:
        monkeypatch.setattr(lab.execution, "_transition_run", gated_transition)
    monkeypatch.setattr(lab.native, "close", tracked_close)
    stops = tuple(
        RunStop(
            workspace_id=lab.workspace_id,
            run_id=start.run_id,
            operation_id=uuid4(),
            grant_id=lab.grant_id,
        )
        for _ in range(stop_count)
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            lab.service.start_background(interval_seconds=0.001)
            assert entered.wait(timeout=1)
            for stop in stops:
                receipt = pool.submit(lab.execution.stop, stop).result(timeout=1)
                assert receipt.status is StopStatus.ACCEPTED
                assert lab.execution.stop(stop) == receipt
                assert (
                    lab.execution.get_stop(
                        lab.workspace_id,
                        start.run_id,
                        stop.operation_id,
                        grant_id=lab.grant_id,
                    )
                    == receipt
                )
            snapshot = lab.execution.get_run(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            )
            assert snapshot.state is RunState.CLOSING
            assert not snapshot.input_open
            rejected = pool.submit(
                lab.execution.send_input, _input(lab, start.run_id, "reject after Stop")
            ).result(timeout=1)
            assert rejected.status is InputStatus.REJECTED
            assert rejected.reason_code == "run_closed"
            installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
            assert installation.active_count == 1
            assert installation.session_count == 1
        finally:
            release.set()
            lab.service.close(timeout=3)
    for stop in stops:
        assert (
            lab.execution.get_stop(
                lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
            ).status
            is StopStatus.STOPPED
        )
    assert closes == [start.run_id]
    assert len(lab.native.open_calls) == 1
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


def test_stop_before_scheduled_worker_claim_prevents_native_open(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = lab.execution.start(lab.request())
    entered = Event()
    release = Event()
    original_build = lab.execution._build_open_request

    def gated_projection(item):
        request = original_build(item)
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("native projection was not released")
        return request

    monkeypatch.setattr(lab.execution, "_build_open_request", gated_projection)
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            lab.service.start_background(interval_seconds=0.001)
            assert entered.wait(timeout=1)
            assert (
                pool.submit(lab.execution.stop, stop).result(timeout=1).status
                is StopStatus.ACCEPTED
            )
            assert (
                lab.execution.get_stop(
                    lab.workspace_id,
                    start.run_id,
                    stop.operation_id,
                    grant_id=lab.grant_id,
                ).status
                is StopStatus.STOPPED
            )
            installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
            assert installation.active_count == 0
            assert installation.session_count == 0
        finally:
            release.set()
            lab.service.close(timeout=3)
    assert lab.native.open_calls == []
    assert lab.native.prompt_calls == []
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.STOPPED
    )


@pytest.mark.parametrize("operation", ("stop", "recover"))
def test_close_retains_owner_while_public_native_lifecycle_call_is_blocked(
    admission_lab, monkeypatch, operation
):
    lab = admission_lab
    start = lab.execution.start(lab.request(policy="conversation"))
    lab.pump()
    entered = Event()
    release = Event()
    boundary = "close" if operation == "stop" else "inspect"
    original_call = getattr(lab.native, boundary)

    def gated_call(handle):
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("native lifecycle call was not released")
        return original_call(handle)

    monkeypatch.setattr(lab.native, boundary, gated_call)
    request_type = RunStop if operation == "stop" else RunRecover
    request = request_type(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        calling = pool.submit(getattr(lab.execution, operation), request)
        try:
            assert entered.wait(timeout=1)
            with pytest.raises(AgentError) as caught:
                lab.service.close(timeout=0.01)
            assert caught.value.code == "shutdown_pending"
            assert lab.store.execution_owner_is_held(lab.service._owner_id)
            other = create_agent_service(
                lab.store,
                lab.registry,
                lab.authorizer,
                DenyContextSourceReader(),
                DenySecretReader(),
                lab.clock,
            )
            try:
                with pytest.raises(AgentError) as second:
                    other.poll_once()
                assert second.value.code == "execution_owner_unavailable"
            finally:
                other.close(timeout=1)
        finally:
            release.set()
        receipt = calling.result(timeout=2)
    if operation == "stop":
        assert receipt.status is StopStatus.ACCEPTED
        assert (
            lab.execution.get_stop(
                lab.workspace_id,
                start.run_id,
                request.operation_id,
                grant_id=lab.grant_id,
            ).status
            is StopStatus.STOPPED
        )
    else:
        assert receipt.status is RecoveryStatus.RECOVERED
        assert (
            lab.store.get_lifecycle_event(
                lab.workspace_id, start.run_id, request.operation_id, "recovery."
            ).kind
            == "recovery.recovered"
        )
        assert (
            lab.execution.get_run(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            ).state
            is RunState.IDLE
        )
    lab.service.close(timeout=2)
    assert not lab.store.execution_owner_is_held(lab.service._owner_id)
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 0
    assert installation.session_count == (0 if operation == "stop" else 1)


def test_close_retains_owner_after_prompt_finishes_while_cancel_is_blocked(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = lab.execution.start(lab.request(policy="conversation"))
    prompt_entered = Event()
    prompt_release = Event()
    cancel_entered = Event()
    cancel_release = Event()
    cancel_calls = []
    original_open = lab.native.open

    def open_with_cancel(request):
        handle = original_open(request).model_copy(
            update={"capabilities": ("prompt", "cancel_current_response")}
        )
        lab.native.handles[request.run_id] = handle
        return handle

    def gated_prompt(handle, assembled, response_id):
        lab.native.begin_prompt(handle, assembled, response_id)
        prompt_entered.set()
        if not prompt_release.wait(timeout=4):
            raise RuntimeError("native prompt was not released")
        return lab.native.finish_prompt(
            handle, response_id, NativeOutcomeStatus.COMPLETED
        )

    def gated_cancel(handle, response_id):
        cancel_calls.append((handle.run_id, response_id))
        cancel_entered.set()
        if not cancel_release.wait(timeout=4):
            raise RuntimeError("native cancellation was not released")
        return NativeCancelOutcome(
            response_id=response_id,
            status=NativeCancelState.CONFIRMED,
            reason_code=None,
        )

    monkeypatch.setattr(lab.native, "open", open_with_cancel)
    monkeypatch.setattr(lab.native, "prompt", gated_prompt)
    monkeypatch.setattr(lab.native, "cancel", gated_cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            lab.service.start_background(interval_seconds=0.001)
            assert prompt_entered.wait(timeout=1)
            response_id = lab.execution.get_run(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            ).active_response_id
            assert response_id is not None
            request = RunInterrupt(
                workspace_id=lab.workspace_id,
                run_id=start.run_id,
                operation_id=uuid4(),
                grant_id=lab.grant_id,
                response_id=response_id,
            )
            calling = pool.submit(lab.execution.interrupt, request)
            assert cancel_entered.wait(timeout=1)
            prompt_release.set()
            assert lab.execution._native_workers.wait_until(monotonic() + 1)
            assert (
                lab.execution.get_run(
                    lab.workspace_id, start.run_id, grant_id=lab.grant_id
                ).state
                is RunState.IDLE
            )
            with pytest.raises(AgentError) as caught:
                lab.service.close(timeout=0.01)
            assert caught.value.code == "shutdown_pending"
            assert lab.store.execution_owner_is_held(lab.service._owner_id)
        finally:
            prompt_release.set()
            cancel_release.set()
        receipt = calling.result(timeout=2)
    assert receipt.status is InterruptStatus.INTERRUPTED
    assert receipt.response_id == response_id
    assert (
        lab.execution.get_interrupt(
            lab.workspace_id, start.run_id, request.operation_id, grant_id=lab.grant_id
        )
        == receipt
    )
    assert cancel_calls == [(start.run_id, response_id)]
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 1
    lab.service.close(timeout=2)
    assert not lab.store.execution_owner_is_held(lab.service._owner_id)


@pytest.mark.parametrize("operation", ("stop", "interrupt", "recover"))
def test_new_native_lifecycle_call_is_rejected_after_closing(admission_lab, operation):
    lab = admission_lab
    start = lab.execution.start(lab.request(policy="conversation"))
    lab.pump()
    lab.service.close()
    fields = {
        "workspace_id": lab.workspace_id,
        "run_id": start.run_id,
        "operation_id": uuid4(),
        "grant_id": lab.grant_id,
    }
    if operation == "interrupt":
        fields["response_id"] = uuid4()
    request = {"stop": RunStop, "interrupt": RunInterrupt, "recover": RunRecover}[
        operation
    ](**fields)
    before = lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
    with pytest.raises(AgentError) as caught:
        getattr(lab.execution, operation)(request)
    assert caught.value.code == "shutdown_pending"
    assert lab.store.get_lifecycle_events(lab.workspace_id, start.run_id) == before
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.IDLE
    )


@pytest.mark.parametrize("operation", ("stop", "interrupt", "recover"))
@pytest.mark.parametrize("failure", ("denied", "store_error"))
def test_native_lifecycle_error_drains_owner_lifetime_boundary(
    admission_lab, monkeypatch, operation, failure
):
    lab = admission_lab
    start = lab.execution.start(lab.request(policy="conversation"))
    lab.pump()
    fields = {
        "workspace_id": lab.workspace_id,
        "run_id": start.run_id,
        "operation_id": uuid4(),
        "grant_id": uuid4() if failure == "denied" else lab.grant_id,
    }
    if operation == "interrupt":
        fields["response_id"] = uuid4()
    request = {"stop": RunStop, "interrupt": RunInterrupt, "recover": RunRecover}[
        operation
    ](**fields)

    def fail_read(*args, **kwargs):
        raise RuntimeError("controlled state read failure")

    if failure == "store_error":
        monkeypatch.setattr(lab.store, "get_run_snapshot", fail_read)
    with pytest.raises(AgentError if failure == "denied" else RuntimeError):
        getattr(lab.execution, operation)(request)
    lab.service.close(timeout=0.1)
    assert not lab.store.execution_owner_is_held(lab.service._owner_id)


@pytest.mark.parametrize("verified", (True, False))
def test_stop_during_open_failure_resolves_original_operation(
    admission_lab, monkeypatch, verified
):
    lab = admission_lab
    start = lab.execution.start(lab.request())
    entered = Event()
    release = Event()
    closes = []

    def gated_failure(request):
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("native failure was not released")
        if verified:
            raise VerifiedNativeOpenFailure("native_spawn_failed")
        raise OSError("ambiguous native open")

    monkeypatch.setattr(lab.native, "open", gated_failure)
    monkeypatch.setattr(
        lab.native, "close", lambda handle: closes.append(handle.run_id)
    )
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            lab.service.start_background(interval_seconds=0.001)
            assert entered.wait(timeout=1)
            receipt = pool.submit(lab.execution.stop, stop).result(timeout=1)
            assert receipt.status is StopStatus.ACCEPTED
            assert (
                lab.execution.get_stop(
                    lab.workspace_id,
                    start.run_id,
                    stop.operation_id,
                    grant_id=lab.grant_id,
                ).status
                is StopStatus.ACCEPTED
            )
        finally:
            release.set()
            lab.service.close(timeout=3)
    receipt = lab.execution.get_stop(
        lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
    )
    assert receipt.status is (StopStatus.STOPPED if verified else StopStatus.UNKNOWN)
    assert lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    ).state is (RunState.STOPPED if verified else RunState.UNKNOWN)
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == (0 if verified else 1)
    assert installation.session_count == (0 if verified else 1)
    assert closes == []
    assert lab.native.prompt_calls == []


def _input(lab, run_id, text):
    return RunInput(
        workspace_id=lab.workspace_id,
        run_id=run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
        text=text,
        context=(),
    )


def test_background_starts_two_admitted_runs_before_either_completes(
    admission_lab_factory, exercise_background_parallel
):
    lab = admission_lab_factory(
        MemoryAgentStore(),
        runtime_max_active=2,
        runtime_max_sessions=2,
        profile_max_active=2,
    )
    exercise_background_parallel(lab)


def test_pending_background_start_is_not_orphan_fenced_or_opened_twice(
    admission_lab, monkeypatch
):
    lab = admission_lab
    request = lab.request()
    started = lab.execution.start(request)
    entered = Event()
    release = Event()
    original_open = lab.native.open

    def gated_open(request):
        handle = original_open(request)
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("native open was not released")
        return handle

    monkeypatch.setattr(lab.native, "open", gated_open)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            lab.service.start_background(interval_seconds=0.001)
            assert entered.wait(timeout=1)
            pool.submit(lab.service.poll_once).result(timeout=1)
            assert lab.execution.start(request) == started
            assert (
                lab.execution.get_start(
                    lab.workspace_id, request.operation_id, grant_id=lab.grant_id
                ).receipt
                == started
            )
            assert (
                lab.execution.get_run(
                    lab.workspace_id, started.run_id, grant_id=lab.grant_id
                ).state
                is RunState.STARTING
            )
            assert len(lab.native.open_calls) == 1
            assert not any(
                event.kind in {"run.unknown", "run.owner_restarted"}
                for event in lab.store.get_lifecycle_events(
                    lab.workspace_id, started.run_id
                )
            )
        finally:
            release.set()
            lab.service.close(timeout=3)

    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 1
    assert (
        lab.execution.get_run(
            lab.workspace_id, started.run_id, grant_id=lab.grant_id
        ).state
        is RunState.COMPLETED
    )


def test_native_worker_callback_error_is_reported_and_registry_drains(monkeypatch):
    from sidera_agents.native_workers import NativeWorkers

    workers = NativeWorkers()
    run_id = uuid4()
    entered = Event()
    release = Event()
    reported = Event()
    errors = []

    def report(args):
        errors.append(
            (
                args.exc_type,
                str(args.exc_value),
                args.exc_value.__cause__,
                args.exc_value.__context__,
            )
        )
        reported.set()

    def fail():
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("worker was not released")
        raise OSError("controlled callback failure")

    monkeypatch.setattr(threading, "excepthook", report)
    try:
        assert workers.dispatch(run_id, fail)
        assert entered.wait(timeout=1)
        assert not workers.dispatch(run_id, fail)
        workers.begin_close()
        assert not workers.dispatch(uuid4(), fail)
        assert not workers.wait_until(monotonic() + 0.01)
        assert workers.busy(run_id)
    finally:
        release.set()
        assert workers.wait_until(monotonic() + 2)
    assert reported.wait(timeout=1)
    assert errors == [(RuntimeError, "native_worker_failed", None, None)]
    assert workers.run_ids() == ()


def test_unexpected_background_callback_reports_safe_error_after_fencing(
    admission_lab, monkeypatch
):
    lab = admission_lab
    request = lab.request()
    started = lab.execution.start(request)
    marker = "PRIVATE_UNEXPECTED_WORKER_CALLBACK"
    reported = Event()
    errors = []

    def fail_dispatch(live, response_id):
        raise RuntimeError(marker)

    def report(args):
        errors.append(
            (
                "".join(
                    format_exception(args.exc_type, args.exc_value, args.exc_traceback)
                ),
                args.exc_value,
            )
        )
        reported.set()

    monkeypatch.setattr(lab.execution, "_begin_initial_prompt", fail_dispatch)
    monkeypatch.setattr(threading, "excepthook", report)
    try:
        lab.service.start_background(interval_seconds=0.001)
        assert reported.wait(timeout=1)
        assert lab.execution._native_workers.wait_until(monotonic() + 2)
        snapshot = lab.execution.get_run(
            lab.workspace_id, started.run_id, grant_id=lab.grant_id
        )
        assert snapshot.state is RunState.UNKNOWN
        assert snapshot.reason_code == "native_response_unknown"
        assert snapshot.run_id == started.run_id
        assert (
            lab.execution.get_start(
                lab.workspace_id, request.operation_id, grant_id=lab.grant_id
            ).receipt.run_id
            == started.run_id
        )
        installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
        assert installation.active_count == 1
        assert installation.session_count == 1
        assert lab.execution._native_workers.run_ids() == ()
        assert len(errors) == 1
        traceback_text, error = errors[0]
        assert marker not in traceback_text
        assert str(error) == "native_worker_failed"
        assert error.__cause__ is None
        assert error.__context__ is None
        assert marker not in repr(snapshot)
        assert marker not in repr(
            lab.store.get_lifecycle_events(lab.workspace_id, started.run_id)
        )
        lab.service.poll_once()
        assert len(lab.native.open_calls) == 1
        assert lab.native.prompt_calls == []
    finally:
        lab.service.close(timeout=2)


def test_background_native_worker_cannot_release_its_own_owner(
    admission_lab, monkeypatch
):
    lab = admission_lab
    started = lab.execution.start(lab.request())
    attempted_close = Event()
    outcomes = []
    original_prompt = lab.native.prompt

    def self_closing_prompt(handle, assembled, response_id):
        try:
            lab.service.close(timeout=0.1)
        except AgentError as error:
            outcomes.append(
                (error.code, lab.store.execution_owner_is_held(lab.service._owner_id))
            )
        finally:
            attempted_close.set()
        return original_prompt(handle, assembled, response_id)

    monkeypatch.setattr(lab.native, "prompt", self_closing_prompt)
    try:
        lab.service.start_background(interval_seconds=0.001)
        assert attempted_close.wait(timeout=1)
    finally:
        lab.service.close(timeout=2)
    assert outcomes == [("shutdown_pending", True)]
    assert not lab.store.execution_owner_is_held(lab.service._owner_id)
    assert (
        lab.execution.get_run(
            lab.workspace_id, started.run_id, grant_id=lab.grant_id
        ).state
        is RunState.COMPLETED
    )


@pytest.mark.parametrize("boundary", ("open", "prompt"))
def test_background_native_failure_is_unknown_and_never_replayed(
    admission_lab, monkeypatch, boundary
):
    lab = admission_lab
    request = lab.request()
    started = lab.execution.start(request)
    failed = Event()
    original_open = lab.native.open
    marker = "PRIVATE_NATIVE_CALLBACK_FAILURE"

    def fail_open(request):
        original_open(request)
        failed.set()
        raise OSError(marker)

    def fail_prompt(handle, assembled, response_id):
        lab.native.begin_prompt(handle, assembled, response_id)
        failed.set()
        raise OSError(marker)

    monkeypatch.setattr(
        lab.native, boundary, fail_open if boundary == "open" else fail_prompt
    )
    try:
        lab.service.start_background(interval_seconds=0.001)
        assert failed.wait(timeout=1)
        assert lab.execution._native_workers.wait_until(monotonic() + 2)
        repeated = lab.execution.start(request)
        assert repeated.run_id == started.run_id
        assert repeated.operation_id == request.operation_id
        lab.service.poll_once()
    finally:
        lab.service.close(timeout=2)

    snapshot = lab.execution.get_run(
        lab.workspace_id, started.run_id, grant_id=lab.grant_id
    )
    assert snapshot.state is RunState.UNKNOWN
    assert snapshot.reason_code == (
        "native_start_unknown" if boundary == "open" else "native_response_unknown"
    )
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == (0 if boundary == "open" else 1)
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 1
    assert installation.session_count == 1
    assert marker not in repr(snapshot)
    assert marker not in repr(
        lab.store.get_lifecycle_events(lab.workspace_id, started.run_id)
    )


def test_background_thread_start_failure_records_verified_no_dispatch(
    admission_lab, monkeypatch
):
    from sidera_agents import native_workers

    lab = admission_lab
    request = lab.request()
    started = lab.execution.start(request)
    resolved = Event()
    original_transition = lab.store.transition_run

    class UnstartableThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("controlled thread start failure")

    def track_transition(run_id, snapshot, **kwargs):
        result = original_transition(run_id, snapshot, **kwargs)
        if snapshot.state is RunState.FAILED:
            resolved.set()
        return result

    monkeypatch.setattr(native_workers, "Thread", UnstartableThread)
    monkeypatch.setattr(lab.store, "transition_run", track_transition)
    try:
        lab.service.start_background(interval_seconds=0.001)
        assert resolved.wait(timeout=1)
    finally:
        lab.service.close(timeout=2)
    lookup = lab.execution.get_start(
        lab.workspace_id, request.operation_id, grant_id=lab.grant_id
    )
    assert lookup.receipt.run_id == started.run_id
    assert lookup.receipt.status is StartStatus.REJECTED
    assert lookup.receipt.reason_code == "native_start_failed"
    assert lab.native.open_calls == []
    assert lab.execution._native_workers.run_ids() == ()
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 0
    assert installation.session_count == 0


def test_background_input_thread_start_failure_fails_without_native_delivery(
    admission_lab, monkeypatch
):
    from sidera_agents import native_workers

    lab = admission_lab
    started = lab.execution.start(lab.request(policy=RunPolicy.CONVERSATION.value))
    lab.pump()
    request = _input(lab, started.run_id, "cannot dispatch this input")
    assert lab.execution.send_input(request).status is InputStatus.ACCEPTED
    attempted = Event()
    errors = []

    class UnstartableThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            attempted.set()
            raise RuntimeError("controlled thread start failure")

    monkeypatch.setattr(native_workers, "Thread", UnstartableThread)
    monkeypatch.setattr(
        threading, "excepthook", lambda args: errors.append(args.exc_type)
    )
    try:
        lab.service.start_background(interval_seconds=0.001)
        assert attempted.wait(timeout=1)
    finally:
        lab.service.close(timeout=2)

    receipt = lab.execution.get_input(
        lab.workspace_id, started.run_id, request.operation_id, grant_id=lab.grant_id
    )
    assert receipt.status is InputStatus.FAILED
    assert receipt.reason_code == "native_response_failed"
    assert errors == []
    assert (
        lab.store.get_input_operation(
            lab.workspace_id, started.run_id, request.operation_id
        ).payload
        is None
    )
    assert len(lab.native.prompt_calls) == 1
    assert lab.execution._native_workers.run_ids() == ()
    snapshot = lab.execution.get_run(
        lab.workspace_id, started.run_id, grant_id=lab.grant_id
    )
    assert snapshot.state is RunState.IDLE
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 0
    assert installation.session_count == 1


def test_close_retains_owner_while_background_native_worker_is_blocked(
    admission_lab, monkeypatch
):
    lab = admission_lab
    started = lab.execution.start(lab.request())
    entered = Event()
    release = Event()
    original_prompt = lab.native.prompt

    def gated_prompt(handle, assembled, response_id):
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("background response was not released")
        return original_prompt(handle, assembled, response_id)

    monkeypatch.setattr(lab.native, "prompt", gated_prompt)
    other = create_agent_service(
        lab.store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        lab.service.start_background(interval_seconds=0.001)
        assert entered.wait(timeout=1)
        with pytest.raises(AgentError) as closing:
            lab.service.close(timeout=0.01)
        assert closing.value.code == "shutdown_pending"
        assert lab.store.execution_owner_is_held(lab.service._owner_id)
        with pytest.raises(AgentError) as owner:
            other.poll_once()
        assert owner.value.code == "execution_owner_unavailable"
        with pytest.raises(AgentError) as starting:
            lab.execution.start(lab.request(text="reject during shutdown"))
        assert starting.value.code == "shutdown_pending"
        with pytest.raises(AgentError) as sending:
            lab.execution.send_input(_input(lab, started.run_id, "also reject"))
        assert sending.value.code == "shutdown_pending"
        assert (
            lab.execution.get_run(
                lab.workspace_id, started.run_id, grant_id=lab.grant_id
            ).state
            is RunState.RUNNING
        )
    finally:
        release.set()
        lab.service.close(timeout=3)
        other.close(timeout=1)
    assert not lab.store.execution_owner_is_held(lab.service._owner_id)
    assert (
        lab.execution.get_run(
            lab.workspace_id, started.run_id, grant_id=lab.grant_id
        ).state
        is RunState.COMPLETED
    )


@pytest.mark.parametrize("admission", ("start", "input"))
def test_close_waits_for_in_progress_admission_before_releasing_owner(
    admission_lab, monkeypatch, admission
):
    lab = admission_lab
    if admission == "input":
        started = lab.execution.start(lab.request(policy=RunPolicy.CONVERSATION.value))
        lab.pump()
        request = _input(lab, started.run_id, "already entering admission")
        submit = lab.execution.send_input
    else:
        request = lab.request()
        submit = lab.execution.start
    original_admit = getattr(lab.store, f"admit_{admission}")
    entered = Event()
    release = Event()

    def gated_admit(*args, **kwargs):
        receipt = original_admit(*args, **kwargs)
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("admission publication was not released")
        return receipt

    monkeypatch.setattr(lab.store, f"admit_{admission}", gated_admit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            submitting = pool.submit(submit, request)
            assert entered.wait(timeout=1)
            with pytest.raises(AgentError) as closing:
                lab.service.close(timeout=0.01)
            assert closing.value.code == "shutdown_pending"
            assert lab.store.execution_owner_is_held(lab.service._owner_id)
        finally:
            release.set()
            receipt = submitting.result(timeout=2)
            lab.service.close(timeout=2)

    assert receipt.operation_id == request.operation_id
    assert receipt.status.value == "accepted"
    assert not lab.store.execution_owner_is_held(lab.service._owner_id)


def test_background_input_queue_keeps_one_worker_and_session(
    admission_lab, monkeypatch
):
    lab = admission_lab
    started = lab.execution.start(lab.request(policy=RunPolicy.CONVERSATION.value))
    entered = Event()
    release = Event()
    prompts_active = 0
    original_prompt = lab.native.prompt

    def gated_prompt(handle, assembled, response_id):
        nonlocal prompts_active
        prompts_active += 1
        assert prompts_active == 1
        try:
            if not entered.is_set():
                lab.native.begin_prompt(handle, assembled, response_id)
                entered.set()
                if not release.wait(timeout=4):
                    raise RuntimeError("initial response was not released")
                return lab.native.finish_prompt(
                    handle, response_id, NativeOutcomeStatus.COMPLETED
                )
            return original_prompt(handle, assembled, response_id)
        finally:
            prompts_active -= 1

    monkeypatch.setattr(lab.native, "prompt", gated_prompt)
    first = _input(lab, started.run_id, "first queued input")
    second = _input(lab, started.run_id, "second queued input")
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            lab.service.start_background(interval_seconds=0.001)
            assert entered.wait(timeout=1)
            pinned = lab.execution.get_run(
                lab.workspace_id, started.run_id, grant_id=lab.grant_id
            ).effective_config
            for request in (first, second):
                assert lab.execution.send_input(request).status is InputStatus.ACCEPTED
            for _ in range(3):
                pool.submit(lab.service.poll_once).result(timeout=1)
            assert len(lab.native.prompt_calls) == 1
        finally:
            release.set()
            lab.service.close(timeout=3)

    for request in (first, second):
        assert (
            lab.execution.get_input(
                lab.workspace_id,
                started.run_id,
                request.operation_id,
                grant_id=lab.grant_id,
            ).status
            is InputStatus.DELIVERED
        )
    assert [call[1].text for call in lab.native.prompt_calls] == [
        "Review the synthetic change.",
        first.text,
        second.text,
    ]
    assert len(lab.native.open_calls) == 1
    assert len({call[0].session_id for call in lab.native.prompt_calls}) == 1
    snapshot = lab.execution.get_run(
        lab.workspace_id, started.run_id, grant_id=lab.grant_id
    )
    assert snapshot.state is RunState.IDLE
    assert snapshot.effective_config == pinned


def test_starting_run_input_rejects_without_waiting_for_native_open(
    admission_lab, monkeypatch
):
    lab = admission_lab
    started = lab.execution.start(lab.request())
    entered = Event()
    release = Event()
    returned = Event()
    original_open = lab.native.open

    def gated_open(request):
        handle = original_open(request)
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("native open was not released")
        return handle

    monkeypatch.setattr(lab.native, "open", gated_open)
    request = _input(lab, started.run_id, "reject during native open")

    def send():
        try:
            return lab.execution.send_input(request)
        finally:
            returned.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            lab.service.start_background(interval_seconds=0.001)
            assert entered.wait(timeout=1)
            assert (
                lab.execution.get_run(
                    lab.workspace_id, started.run_id, grant_id=lab.grant_id
                ).state
                is RunState.STARTING
            )
            sending = pool.submit(send)
            assert returned.wait(timeout=1)
            rejected = sending.result(timeout=1)
            assert rejected.status is InputStatus.REJECTED
            assert rejected.reason_code == "run_closed"
            assert (
                lab.execution.get_input(
                    lab.workspace_id,
                    started.run_id,
                    request.operation_id,
                    grant_id=lab.grant_id,
                )
                == rejected
            )
            operation = lab.store.get_input_operation(
                lab.workspace_id, started.run_id, request.operation_id
            )
            assert operation.payload is None
            assert not any(
                event.kind in {"input.accepted", "run.unknown", "run.owner_restarted"}
                for event in lab.store.get_lifecycle_events(
                    lab.workspace_id, started.run_id
                )
            )
        finally:
            release.set()
            lab.service.close(timeout=3)

    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 1
