import re
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select

from sidera_agents.contracts import (
    ContextItem,
    InputStatus,
    InterruptStatus,
    NativeCancelOutcome,
    NativeCancelState,
    RecoveryStatus,
    RunInput,
    RunInterrupt,
    RunPolicy,
    RunRecover,
    RunState,
    RunStop,
    StopStatus,
)
from sidera_agents.errors import AgentError


def _postgres_lab(owned_agents_pg, admission_lab_factory):
    from sidera_agents.postgres.store import PostgresAgentStore

    owned_agents_pg.migrate()
    store = PostgresAgentStore(owned_agents_pg.engine)
    return admission_lab_factory(store), store


def _context(text: str) -> ContextItem:
    return ContextItem(
        item_id="initial-context",
        origin="user",
        required=True,
        ordinal=0,
        version="v1",
        content_hash="sha256:" + sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )


@pytest.mark.parametrize("missing", ["reservation", "installation"])
@pytest.mark.parametrize(
    "action", ["finish", "close_input", "stop", "admit", "activate"]
)
def test_postgres_incomplete_capacity_never_returns_success_or_changes_evidence(
    owned_agents_pg, admission_lab_factory, monkeypatch, missing, action
):
    lab, store = _postgres_lab(owned_agents_pg, admission_lab_factory)
    start = lab.execution.start(lab.request(policy=RunPolicy.CONVERSATION.value))
    lab.pump()
    response_id = lab.native.prompt_calls[0][2]
    snapshot = store.get_run_snapshot(lab.workspace_id, start.run_id)
    handle = store.get_native_handle(lab.workspace_id, start.run_id)
    events = store.get_lifecycle_events(lab.workspace_id, start.run_id)
    installation = store.get_runtime_entry(lab.runtime.ref).installation
    locked_capacity = store._locked_run_capacity

    def incomplete_capacity(connection, workspace_id, run_id):
        run, reservation, runtime = locked_capacity(connection, workspace_id, run_id)
        return (
            run,
            None if missing == "reservation" else reservation,
            None if missing == "installation" else runtime,
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "_locked_run_capacity", incomplete_capacity)
        with pytest.raises(AgentError) as error:
            if action == "finish":
                store.finish_conversation_response(
                    start.run_id, response_id, owner_id=lab.service._owner_id
                )
            elif action == "close_input":
                store.close_input_admission(
                    start.run_id,
                    response_id,
                    owner_id=lab.service._owner_id,
                    terminal_state=RunState.COMPLETED,
                    reason_code=None,
                )
            elif action == "stop":
                store.complete_stop(
                    start.run_id,
                    uuid4(),
                    owner_id=lab.service._owner_id,
                    stopped=True,
                    reason_code=None,
                )
            elif action == "admit":
                lab.execution.send_input(
                    RunInput(
                        workspace_id=lab.workspace_id,
                        run_id=start.run_id,
                        operation_id=uuid4(),
                        grant_id=lab.grant_id,
                        text="must not reserve incomplete capacity",
                        context=(),
                    )
                )
            else:
                store.activate_pending_input(
                    start.run_id, uuid4(), owner_id=lab.service._owner_id
                )

    assert error.value.code == "not_found"
    assert store.get_run_snapshot(lab.workspace_id, start.run_id) == snapshot
    assert store.get_native_handle(lab.workspace_id, start.run_id) == handle
    assert store.get_lifecycle_events(lab.workspace_id, start.run_id) == events
    assert store.get_runtime_entry(lab.runtime.ref).installation == installation
    assert len(lab.native.open_calls) == 1 and len(lab.native.prompt_calls) == 1


def test_postgres_missing_run_workspace_rejects_before_capacity_lookup(
    owned_agents_pg, admission_lab_factory, monkeypatch
):
    lab, store = _postgres_lab(owned_agents_pg, admission_lab_factory)
    locked_capacity = store._locked_run_capacity

    def owned_capacity(connection, workspace_id, run_id):
        assert workspace_id is not None, "Capacity requires a verified workspace"
        return locked_capacity(connection, workspace_id, run_id)

    monkeypatch.setattr(store, "_locked_run_capacity", owned_capacity)
    with pytest.raises(AgentError) as error:
        store.finish_conversation_response(
            uuid4(), uuid4(), owner_id=lab.service._owner_id
        )
    assert error.value.code == "not_found"
    assert lab.native.open_calls == [] and lab.native.prompt_calls == []


def test_postgres_persists_only_live_input_payload_and_safe_events(
    owned_agents_pg, admission_lab_factory, monkeypatch
):
    from sidera_agents.postgres.models import (
        AgentRunRow,
        RunEventRow,
        RunInputRow,
        RunOperationRow,
    )

    lab, _ = _postgres_lab(owned_agents_pg, admission_lab_factory)
    initial_text = "INITIAL_USER_TEXT_MUST_NOT_PERSIST"
    initial_context = "INITIAL_CONTEXT_MUST_NOT_PERSIST"
    start_request = lab.request(
        policy=RunPolicy.CONVERSATION.value,
        text=initial_text,
        context=(_context(initial_context),),
        instructions=("INITIAL_INSTRUCTION_MUST_NOT_PERSIST",),
    )
    started = lab.execution.start(start_request)
    assert started.run_id is not None

    entered = Event()
    release = Event()
    original_prompt = lab.native.prompt

    def blocked_prompt(handle, assembled, response_id):
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("controlled prompt was not released")
        return original_prompt(handle, assembled, response_id)

    monkeypatch.setattr(lab.native, "prompt", blocked_prompt)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pumping = pool.submit(lab.pump)
            assert entered.wait(timeout=2)
            live_text = "LIVE_ACCEPTED_INPUT_MAY_PERSIST_TEMPORARILY"
            live_context = "LIVE_ACCEPTED_CONTEXT_MAY_PERSIST_TEMPORARILY"
            request = RunInput(
                workspace_id=lab.workspace_id,
                run_id=started.run_id,
                operation_id=uuid4(),
                grant_id=lab.grant_id,
                text=live_text,
                context=(_context(live_context),),
            )
            accepted = lab.execution.send_input(request)
            assert accepted.status is InputStatus.ACCEPTED

            run_table = AgentRunRow.__table__
            input_table = RunInputRow.__table__
            operation_table = RunOperationRow.__table__
            event_table = RunEventRow.__table__
            with owned_agents_pg.engine.connect() as connection:
                run_row = (
                    connection.execute(
                        select(run_table).where(run_table.c.run_id == started.run_id)
                    )
                    .mappings()
                    .one()
                )
                input_row = (
                    connection.execute(
                        select(input_table).where(
                            input_table.c.workspace_id == lab.workspace_id,
                            input_table.c.run_id == started.run_id,
                            input_table.c.operation_id == request.operation_id,
                        )
                    )
                    .mappings()
                    .one()
                )
                operation_row = (
                    connection.execute(
                        select(operation_table).where(
                            operation_table.c.workspace_id == lab.workspace_id,
                            operation_table.c.operation_id
                            == start_request.operation_id,
                        )
                    )
                    .mappings()
                    .one()
                )
                events = (
                    connection.execute(
                        select(event_table)
                        .where(event_table.c.run_id == started.run_id)
                        .order_by(event_table.c.sequence)
                    )
                    .mappings()
                    .all()
                )

            assert run_row["original_grant_id"] == lab.grant_id
            assert input_row["status"] == InputStatus.ACCEPTED.value
            assert input_row["text"] == live_text
            assert input_row["context"][0]["text"] == live_context
            persisted_before_outcome = repr(dict(run_row)) + repr(dict(operation_row))
            assert initial_text not in persisted_before_outcome
            assert initial_context not in persisted_before_outcome
            assert (
                "INITIAL_INSTRUCTION_MUST_NOT_PERSIST" not in persisted_before_outcome
            )
            assert [event["sequence"] for event in events] == list(
                range(1, run_row["snapshot"]["last_event_sequence"] + 1)
            )
            assert all(
                set(event["data"]) <= {"status", "reason_code", "queue_sequence"}
                for event in events
            )
            assert "payload_bytes" not in repr(events)
            assert live_text not in repr(events)
            assert live_context not in repr(events)

            release.set()
            pumping.result(timeout=5)

        with owned_agents_pg.engine.connect() as connection:
            completed_input = (
                connection.execute(
                    select(input_table).where(
                        input_table.c.workspace_id == lab.workspace_id,
                        input_table.c.run_id == started.run_id,
                        input_table.c.operation_id == request.operation_id,
                    )
                )
                .mappings()
                .one()
            )
            completed_events = (
                connection.execute(
                    select(event_table).where(event_table.c.run_id == started.run_id)
                )
                .mappings()
                .all()
            )
        assert completed_input["status"] == InputStatus.DELIVERED.value
        assert completed_input["text"] is None
        assert completed_input["context"] is None
        assert live_text not in repr(completed_events)
        assert live_context not in repr(completed_events)
    finally:
        release.set()


def test_postgres_restart_fences_accepted_input_without_replay(
    owned_agents_pg, admission_lab_factory
):
    from sidera_agents.bootstrap import create_agent_service
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader
    from sidera_agents.postgres.models import RunEventRow, RunInputRow

    lab, store = _postgres_lab(owned_agents_pg, admission_lab_factory)
    started = lab.execution.start(lab.request(policy=RunPolicy.CONVERSATION.value))
    assert started.run_id is not None
    lab.pump()
    initial_response_id = lab.native.prompt_calls[0][2]
    request = RunInput(
        workspace_id=lab.workspace_id,
        run_id=started.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
        text="QUEUED_TEXT_MUST_NOT_REPLAY_AFTER_RESTART",
        context=(),
    )
    accepted = lab.execution.send_input(request)
    assert accepted.status is InputStatus.ACCEPTED
    queued = store.get_input_operation(
        lab.workspace_id, started.run_id, request.operation_id
    )
    assert queued is not None
    queued_response_id = queued.response_id
    native_counts = (len(lab.native.open_calls), len(lab.native.prompt_calls))
    assert native_counts == (1, 1)
    lab.service.close()

    from sidera_agents.postgres.store import PostgresAgentStore

    replacement = create_agent_service(
        PostgresAgentStore(owned_agents_pg.engine),
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        replacement.poll_once()
        receipt = replacement.execution.get_input(
            lab.workspace_id,
            started.run_id,
            request.operation_id,
            grant_id=lab.grant_id,
        )
        assert receipt.status is InputStatus.FAILED
        assert receipt.reason_code == "owner_restarted"
        assert receipt.operation_id == accepted.operation_id == request.operation_id
        assert (
            replacement.execution.get_run(
                lab.workspace_id, started.run_id, grant_id=lab.grant_id
            ).state
            is RunState.UNKNOWN
        )
        recovery = replacement.execution.recover(
            RunRecover(
                workspace_id=lab.workspace_id,
                run_id=started.run_id,
                operation_id=uuid4(),
                grant_id=lab.grant_id,
            )
        )
        assert recovery.status is RecoveryStatus.UNSUPPORTED
        assert (
            len(lab.native.open_calls),
            len(lab.native.prompt_calls),
        ) == native_counts

        with owned_agents_pg.engine.connect() as connection:
            input_row = (
                connection.execute(
                    select(RunInputRow.__table__).where(
                        RunInputRow.__table__.c.workspace_id == lab.workspace_id,
                        RunInputRow.__table__.c.run_id == started.run_id,
                        RunInputRow.__table__.c.operation_id == request.operation_id,
                    )
                )
                .mappings()
                .one()
            )
            events = (
                connection.execute(
                    select(RunEventRow.__table__)
                    .where(RunEventRow.__table__.c.run_id == started.run_id)
                    .order_by(RunEventRow.__table__.c.sequence)
                )
                .mappings()
                .all()
            )
        assert input_row["text"] is None
        assert input_row["context"] is None
        assert input_row["operation_id"] == request.operation_id
        assert input_row["response_id"] == queued_response_id
        assert "QUEUED_TEXT_MUST_NOT_REPLAY_AFTER_RESTART" not in repr(events)
        expected_kinds = (
            "run.started",
            "response.started",
            "usage",
            "response.completed",
            "run.idle",
            "input.accepted",
            "input.failed",
            "run.owner_restarted",
            "recovery.unsupported",
        )
        assert tuple(event["kind"] for event in events) == expected_kinds
        for event in events:
            data = event["data"]
            if event["kind"] == "usage":
                assert set(data) == {
                    "usage",
                    "observed_model",
                    "reason_code",
                    "_native",
                }
                assert data["usage"] is None
                assert data["observed_model"] is None
                assert data["reason_code"] == "usage_unavailable"
                native = data["_native"]
                assert isinstance(native, dict)
                assert set(native) == {"source", "identity"}
                assert isinstance(native["source"], str)
                assert re.fullmatch(r"[0-9a-f]{64}", native["source"]) is not None
                assert native["identity"] == f"outcome:{initial_response_id}"
                assert event["native_cursor"] is None
                assert event["response_id"] == initial_response_id
                assert event["operation_id"] == started.operation_id
            else:
                assert set(data) <= {"status", "reason_code", "queue_sequence"}
            if event["kind"] in {"input.accepted", "input.failed"}:
                assert event["operation_id"] == request.operation_id
                assert event["response_id"] == queued_response_id

        trace = replacement.observations.read_trace(
            lab.workspace_id, started.run_id, 0, grant_id=lab.grant_id
        )
        page = replacement.execution.list_events(
            lab.workspace_id, started.run_id, 0, grant_id=lab.grant_id
        )
        assert trace == page
        assert tuple(event.kind for event in page.events) == expected_kinds
        assert page.next_sequence == events[-1]["sequence"] and not page.has_more
        assert all("_native" not in event.data for event in page.events)
        assert "_native" not in page.model_dump_json()
        assert "QUEUED_TEXT_MUST_NOT_REPLAY_AFTER_RESTART" not in page.model_dump_json()
        public_usage = next(event for event in page.events if event.kind == "usage")
        assert public_usage.data == {
            "usage": None,
            "observed_model": None,
            "reason_code": "usage_unavailable",
        }
        assert public_usage.response_id == initial_response_id
        assert public_usage.operation_id == started.operation_id
    finally:
        replacement.close()


def test_postgres_late_input_response_cannot_reopen_unknown_run(
    owned_agents_pg, admission_lab_factory, monkeypatch
):
    lab, _ = _postgres_lab(owned_agents_pg, admission_lab_factory)
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
    started = lab.execution.start(lab.request(policy=RunPolicy.CONVERSATION.value))
    assert started.run_id is not None
    lab.pump()
    request = RunInput(
        workspace_id=lab.workspace_id,
        run_id=started.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
        text="active input response",
        context=(),
    )
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
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pumping = pool.submit(lab.pump)
            assert entered.wait(timeout=2)
            response_id = lab.execution.get_run(
                lab.workspace_id, started.run_id, grant_id=lab.grant_id
            ).active_response_id
            assert response_id is not None
            interrupted = lab.execution.interrupt(
                RunInterrupt(
                    workspace_id=lab.workspace_id,
                    run_id=started.run_id,
                    operation_id=uuid4(),
                    response_id=response_id,
                    grant_id=lab.grant_id,
                )
            )
            assert interrupted.status is InterruptStatus.UNKNOWN
            assert (
                lab.execution.get_input(
                    lab.workspace_id,
                    started.run_id,
                    request.operation_id,
                    grant_id=lab.grant_id,
                ).status
                is InputStatus.UNKNOWN
            )
            release.set()
            pumping.result(timeout=3)

        snapshot = lab.execution.get_run(
            lab.workspace_id, started.run_id, grant_id=lab.grant_id
        )
        assert snapshot.state is RunState.UNKNOWN
        assert not snapshot.input_open
        assert len(lab.native.open_calls) == 1
        assert len(lab.native.prompt_calls) == 2
    finally:
        release.set()


def test_postgres_stop_closing_is_not_reopened_by_late_input_response(
    owned_agents_pg, admission_lab_factory, monkeypatch
):
    lab, _ = _postgres_lab(owned_agents_pg, admission_lab_factory)
    started = lab.execution.start(lab.request(policy=RunPolicy.CONVERSATION.value))
    assert started.run_id is not None
    lab.pump()

    request = RunInput(
        workspace_id=lab.workspace_id,
        run_id=started.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
        text="input completing during stop",
        context=(),
    )
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
        return original_finish(run_id, response_id, **kwargs)

    monkeypatch.setattr(lab.native, "prompt", blocked_prompt)
    monkeypatch.setattr(lab.native, "close", blocked_close)
    monkeypatch.setattr(lab.store, "finish_conversation_response", tracked_finish)
    assert lab.execution.send_input(request).status is InputStatus.ACCEPTED
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=started.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pumping = pool.submit(lab.pump)
            assert prompt_entered.wait(timeout=2)
            stopping = pool.submit(lab.execution.stop, stop)
            assert close_entered.wait(timeout=2)
            closing = lab.execution.get_run(
                lab.workspace_id, started.run_id, grant_id=lab.grant_id
            )
            assert closing.state is RunState.CLOSING
            assert not closing.input_open

            release_prompt.set()
            pumping.result(timeout=3)

            rejected = lab.execution.send_input(
                RunInput(
                    workspace_id=lab.workspace_id,
                    run_id=started.run_id,
                    operation_id=uuid4(),
                    grant_id=lab.grant_id,
                    text="must remain closed",
                    context=(),
                )
            )
            assert rejected.status is InputStatus.REJECTED
            assert rejected.reason_code == "run_closed"
            assert RunState.CLOSING in completion_states
            snapshot = lab.execution.get_run(
                lab.workspace_id, started.run_id, grant_id=lab.grant_id
            )
            assert snapshot.state is RunState.CLOSING
            assert not snapshot.input_open
            installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
            assert installation.active_count == 0
            assert installation.session_count == 1
            assert (
                lab.execution.get_input(
                    lab.workspace_id,
                    started.run_id,
                    request.operation_id,
                    grant_id=lab.grant_id,
                ).status
                is InputStatus.UNKNOWN
            )
            events = lab.store.get_lifecycle_events(lab.workspace_id, started.run_id)
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
                started.run_id,
                stop.operation_id,
                grant_id=lab.grant_id,
            ).status
            is StopStatus.STOPPED
        )
        installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
        assert installation.active_count == 0
        assert installation.session_count == 0
    finally:
        release_prompt.set()
        release_close.set()
