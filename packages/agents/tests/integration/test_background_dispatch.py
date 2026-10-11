from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from sidera_agents.contracts import NativeOutcomeStatus, RunState, RunStop, StopStatus
from sidera_agents.postgres.store import PostgresAgentStore


def test_postgres_background_starts_two_admitted_runs_before_either_completes(
    owned_agents_pg, admission_lab_factory, exercise_background_parallel
):
    owned_agents_pg.migrate()
    lab = admission_lab_factory(
        PostgresAgentStore(owned_agents_pg.engine),
        runtime_max_active=2,
        runtime_max_sessions=2,
        profile_max_active=2,
    )
    run_ids = exercise_background_parallel(lab)

    reader = PostgresAgentStore(owned_agents_pg.engine)
    for run_id in run_ids:
        snapshot = reader.get_run_snapshot(lab.workspace_id, run_id)
        assert snapshot.state is RunState.COMPLETED
        events = reader.get_lifecycle_events(lab.workspace_id, run_id)
        assert [event.sequence for event in events] == list(
            range(1, snapshot.last_event_sequence + 1)
        )
    installation = reader.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 0
    assert installation.session_count == 0


def test_postgres_stop_before_dispatch_reclaims_without_native_open(
    owned_agents_pg, admission_lab_factory
):
    owned_agents_pg.migrate()
    lab = admission_lab_factory(PostgresAgentStore(owned_agents_pg.engine))
    try:
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
        lab.service.poll_once()
        assert lab.execution.stop(stop) == stopped
        assert lab.execution.start(request).run_id == start.run_id
        reader = PostgresAgentStore(owned_agents_pg.engine)
        assert (
            reader.get_run_snapshot(lab.workspace_id, start.run_id).state
            is RunState.STOPPED
        )
        installation = reader.get_runtime_entry(lab.runtime.ref).installation
        assert installation.active_count == 0
        assert installation.session_count == 0
        assert lab.native.open_calls == []
        assert lab.native.prompt_calls == []
    finally:
        lab.service.close(timeout=3)


def test_postgres_stop_during_open_preserves_closing_and_completes_all_operations(
    owned_agents_pg, admission_lab_factory, monkeypatch
):
    owned_agents_pg.migrate()
    lab = admission_lab_factory(PostgresAgentStore(owned_agents_pg.engine))
    start = lab.execution.start(lab.request())
    entered = Event()
    release = Event()
    closes = []
    original_open = lab.native.open
    original_close = lab.native.close

    def gated_open(request):
        handle = original_open(request)
        entered.set()
        if not release.wait(timeout=4):
            raise RuntimeError("native open was not released")
        return handle

    def tracked_close(handle):
        closes.append(handle.run_id)
        return original_close(handle)

    monkeypatch.setattr(lab.native, "open", gated_open)
    monkeypatch.setattr(lab.native, "close", tracked_close)
    stops = tuple(
        RunStop(
            workspace_id=lab.workspace_id,
            run_id=start.run_id,
            operation_id=uuid4(),
            grant_id=lab.grant_id,
        )
        for _ in range(2)
    )
    reader = PostgresAgentStore(owned_agents_pg.engine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            lab.service.start_background(interval_seconds=0.001)
            assert entered.wait(timeout=1)
            for stop in stops:
                assert (
                    pool.submit(lab.execution.stop, stop).result(timeout=1).status
                    is StopStatus.ACCEPTED
                )
                assert (
                    reader.get_lifecycle_event(
                        lab.workspace_id, start.run_id, stop.operation_id, "stop."
                    ).kind
                    == "stop.accepted"
                )
            snapshot = reader.get_run_snapshot(lab.workspace_id, start.run_id)
            assert snapshot.state is RunState.CLOSING
            assert not snapshot.input_open
            installation = reader.get_runtime_entry(lab.runtime.ref).installation
            assert installation.active_count == 1
            assert installation.session_count == 1
        finally:
            release.set()
            lab.service.close(timeout=3)
    for stop in stops:
        assert (
            reader.get_lifecycle_event(
                lab.workspace_id, start.run_id, stop.operation_id, "stop."
            ).kind
            == "stop.stopped"
        )
    assert (
        reader.get_run_snapshot(lab.workspace_id, start.run_id).state
        is RunState.STOPPED
    )
    installation = reader.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 0
    assert installation.session_count == 0
    assert closes == [start.run_id]
    assert len(lab.native.open_calls) == 1
    assert lab.native.prompt_calls == []


@pytest.mark.parametrize(
    "terminal", (RunState.COMPLETED, RunState.FAILED, RunState.STOPPED)
)
def test_postgres_terminal_stop_preserves_run_state(
    owned_agents_pg, admission_lab_factory, terminal
):
    owned_agents_pg.migrate()
    lab = admission_lab_factory(PostgresAgentStore(owned_agents_pg.engine))
    try:
        start = lab.execution.start(
            lab.request(
                policy="conversation" if terminal is RunState.STOPPED else "bounded"
            )
        )
        if terminal is RunState.FAILED:
            lab.native.outcomes[start.run_id] = NativeOutcomeStatus.FAILED
        lab.service.poll_once()
        if terminal is RunState.STOPPED:
            lab.execution.stop(
                RunStop(
                    workspace_id=lab.workspace_id,
                    run_id=start.run_id,
                    operation_id=uuid4(),
                    grant_id=lab.grant_id,
                )
            )
        reader = PostgresAgentStore(owned_agents_pg.engine)
        before = reader.get_run_snapshot(lab.workspace_id, start.run_id)
        assert before.state is terminal
        history = reader.get_lifecycle_events(lab.workspace_id, start.run_id)
        stop = RunStop(
            workspace_id=lab.workspace_id,
            run_id=start.run_id,
            operation_id=uuid4(),
            grant_id=lab.grant_id,
        )
        receipt = lab.execution.stop(stop)
        assert receipt.status is StopStatus.STOPPED
        assert lab.execution.stop(stop) == receipt
        after = reader.get_run_snapshot(lab.workspace_id, start.run_id)
        assert after.state is terminal
        assert after.reason_code == before.reason_code
        assert (
            reader.get_lifecycle_events(lab.workspace_id, start.run_id)[: len(history)]
            == history
        )
        installation = reader.get_runtime_entry(lab.runtime.ref).installation
        assert installation.active_count == 0
        assert installation.session_count == 0
    finally:
        lab.service.close(timeout=3)
