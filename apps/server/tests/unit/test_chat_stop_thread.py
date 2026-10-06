from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import uuid4

import httpx
import pytest
from sidera_agents.contracts import (
    RunEvent,
    RunState,
    StartReceipt,
    StartStatus,
    StopStatus,
)

from sidera_product.chat.contracts import CreateThreadBody, StartTurnBody
from tests.unit.test_chat_service import (
    completed_response,
    http_chat_lab,
    stopping_lab,
    threaded_turn,
)
from tests.unit.test_chat_service import (
    controlled_owner_runtime as controlled_owner_runtime,
)


def test_individual_empty_stop_is_inert_and_does_not_close_owner(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    try:
        result = lab.chat.stop_thread(lab.identity, lab.thread.id)
        assert result.conversation_id == lab.thread.id
        assert result.cleanup_confirmed and result.stop_state == "stopped"
        assert (
            result.reason_code is None and lab.starts == lab.stops == lab.queries == []
        )
        other = lab.chat.create_thread(
            lab.identity, CreateThreadBody(profile_ref=lab.thread.profile_ref)
        )
        assert other.id != lab.thread.id and other.stop_state is None
    finally:
        lab.product.close()


def test_individual_unknown_stop_retains_original_and_repeated_call_queries_only(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    try:
        turn = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="original", operation_id="individual:original"),
        )
        completed_response(lab, turn)
        assert (
            lab.chat.read_turn(lab.identity, lab.thread.id, turn.id).status
            == "completed"
        )
        assert lab.chat.read_thread(lab.identity, lab.thread.id).active_turn_id is None
        result = lab.chat.stop_thread(lab.identity, lab.thread.id)
        assert not result.cleanup_confirmed and result.stop_state == "unknown"
        assert result.reason_code == "cleanup_pending"
        request = lab.stops[0]
        assert (
            lab.chat.stop_thread(lab.identity, lab.thread.id).cleanup_confirmed is False
        )
        assert len(lab.stops) == 1
        assert lab.stop_reads[-1] == (
            lab.thread.workspace_id,
            request.run_id,
            request.operation_id,
            request.grant_id,
        )
        lab.stop_state[0] = StopStatus.STOPPED
        confirmed = lab.chat.stop_thread(lab.identity, lab.thread.id)
        assert confirmed.cleanup_confirmed and confirmed.stop_state == "stopped"
        assert lab.chat.stop_thread(lab.identity, lab.thread.id) == confirmed
        assert len(lab.stops) == 1
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(
                lab.identity,
                lab.thread.id,
                StartTurnBody(input="must not reopen", operation_id="later"),
            )
        assert (
            failure.value.code == "run_closed"
            and lab.inputs == []
            and len(lab.starts) == 1
        )
    finally:
        lab.product.close()


def test_individual_stop_during_held_original_admission_does_not_infer_no_run(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    entered, release = Event(), Event()
    results, errors = [], []
    original = lab.chat.execution.start

    def held(request):
        entered.set()
        assert release.wait(5)
        return original(request)

    monkeypatch.setattr(lab.chat.execution, "start", held)
    worker = threaded_turn(
        lab,
        StartTurnBody(input="held", operation_id="individual:held"),
        results,
        errors,
    )
    try:
        assert entered.wait(5)
        result = lab.chat.stop_thread(lab.identity, lab.thread.id)
        assert not result.cleanup_confirmed and result.stop_state == "prepared"
        assert lab.stops == []
        saved = lab.chat.read_thread(lab.identity, lab.thread.id)
        operation = saved.stop_operation_id
        assert operation is not None
        release.set()
        worker.join(5)
        assert (
            not worker.is_alive()
            and errors == []
            and results[0].request_state == "accepted"
        )
        lab.stop_state[0] = StopStatus.STOPPED
        assert lab.chat.stop_thread(lab.identity, lab.thread.id).cleanup_confirmed
        assert (
            len(lab.stops) == len(lab.starts) == 1
            and lab.stops[0].operation_id == operation
        )
    finally:
        release.set()
        worker.join(5)
        lab.product.close()


def test_individual_stop_leaves_other_same_profile_original_run_input_open(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    try:
        first = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="first", operation_id="individual:first"),
        )
        completed_response(lab, first)
        runs = {lab.lab.run[0].run_id: lab.lab.run[0]}
        other = lab.chat.create_thread(
            lab.identity, CreateThreadBody(profile_ref=lab.thread.profile_ref)
        )

        def second_start(request):
            assert lab.boundary.open_sessions == 0
            saved = lab.chat.read_thread(lab.identity, other.id)
            original_turn = lab.chat.store.list_turns(saved)[0]
            assert original_turn.request_state == "dispatching"
            assert original_turn.native_request == request.model_dump(mode="json")
            run = lab.lab.run[0].model_copy(
                update={
                    "run_id": uuid4(),
                    "state": RunState.IDLE,
                    "input_open": True,
                    "active_response_id": None,
                }
            )
            runs[run.run_id] = run
            receipt = StartReceipt(
                operation_id=request.operation_id,
                status=StartStatus.ACCEPTED,
                run_id=run.run_id,
                reason_code=None,
            )
            lab.receipts[request.operation_id] = receipt
            lab.starts.append(request)
            return receipt

        def get_run(workspace, run_id, *, grant_id):
            assert lab.boundary.open_sessions == 0 and workspace == other.workspace_id
            return runs[run_id]

        monkeypatch.setattr(lab.chat.execution, "start", second_start)
        monkeypatch.setattr(lab.chat.execution, "get_run", get_run)
        admitted = lab.chat.start_turn(
            lab.identity,
            other.id,
            StartTurnBody(input="second", operation_id="individual:second"),
        )
        assert admitted.request_state == "accepted"
        saved_other = lab.chat.read_thread(lab.identity, other.id)
        lab.lab.run[0] = runs[saved_other.run_id]
        lab.events.clear()
        completed_response(lab, admitted, text="second original reply")
        runs[lab.lab.run[0].run_id] = lab.lab.run[0]
        assert (
            lab.chat.read_turn(lab.identity, other.id, admitted.id).status
            == "completed"
        )
        lab.stop_state[0] = StopStatus.STOPPED
        assert lab.chat.stop_thread(lab.identity, lab.thread.id).cleanup_confirmed
        saved_other = lab.chat.read_thread(lab.identity, other.id)
        assert saved_other.stop_operation_id is None and saved_other.run_id in runs
        assert runs[saved_other.run_id].input_open
        continuation = lab.chat.start_turn(
            lab.identity,
            other.id,
            StartTurnBody(input="still open", operation_id="individual:continuation"),
        )
        assert continuation.request_state == "accepted" and len(lab.inputs) == 1
        assert lab.inputs[0].run_id == saved_other.run_id != lab.stops[0].run_id
        assert len(lab.stops) == 1 and len(lab.starts) == 2
    finally:
        lab.product.close()


@pytest.mark.parametrize("kind", ["foreign_thread", "noncurrent_human", "agent"])
def test_individual_stop_denies_before_original_native_io(
    settings_values, tmp_path, monkeypatch, no_database_connections, kind
):
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    try:
        identity, conversation = lab.identity, lab.thread.id
        if kind == "foreign_thread":
            conversation = uuid4()
        elif kind == "noncurrent_human":
            identity = replace(identity, grant_id=uuid4())
        else:
            identity = replace(identity, actor_type="agent")
        with pytest.raises(lab.error_type) as failure:
            lab.chat.stop_thread(identity, conversation)
        assert failure.value.code == (
            "thread_not_found" if kind == "foreign_thread" else "local_session_required"
        )
        assert lab.starts == lab.stops == lab.stop_reads == lab.queries == []
        assert (
            lab.chat.read_thread(lab.identity, lab.thread.id).stop_operation_id is None
        )
    finally:
        lab.product.close()


@pytest.mark.anyio
async def test_individual_stop_http_requires_csrf_and_returns_finite_original_cleanup(
    request, settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = http_chat_lab(request, settings_values, tmp_path, monkeypatch)
    path = f"/main-agent/v1/threads/{lab.thread.id}/stop"
    try:
        lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="original", operation_id="individual:http"),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lab.app),
            base_url=lab.chat.settings.local_url,
            cookies=lab.cookie,
        ) as client:
            denied = await client.post(
                path, headers={"Origin": lab.chat.settings.web_origin}
            )
            assert (
                denied.status_code == 403
                and denied.json()["error"]["code"] == "csrf_required"
            )
            assert lab.stops == []
            uncertain = await client.post(path, headers=lab.headers)
            assert uncertain.status_code == 202
            assert uncertain.json() == {
                "conversation_id": str(lab.thread.id),
                "cleanup_confirmed": False,
                "stop_state": "unknown",
                "reason_code": "cleanup_pending",
            }
            lab.stop_state[0] = StopStatus.STOPPED
            confirmed = await client.post(path, headers=lab.headers)
            assert confirmed.status_code == 200
            assert confirmed.json() == {
                "conversation_id": str(lab.thread.id),
                "cleanup_confirmed": True,
                "stop_state": "stopped",
                "reason_code": None,
            }
            assert len(lab.stops) == 1 and len(lab.stop_reads) == 1
    finally:
        lab.stop_state[0] = StopStatus.STOPPED
        assert lab.runtime.close(grace_seconds=0) is True
        lab.product.close()


def test_confirmed_stop_read_caches_cancelled_and_clears_active_turn(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    operation_id = "confirmed-stop:original"
    stop_operation_id = uuid4()
    response_id = uuid4()
    try:
        turn = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="original unfinished input", operation_id=operation_id),
        )
        assert turn.request_state == "accepted"
        original_request = turn.native_request
        started_at = lab.chat.clock()
        lab.events.append(
            RunEvent(
                run_id=lab.lab.run[0].run_id,
                sequence=1,
                kind="response.started",
                occurred_at=started_at,
                operation_id=turn.native_operation_id,
                response_id=response_id,
                data={},
            )
        )
        lab.lab.run[0] = lab.lab.run[0].model_copy(
            update={
                "state": RunState.RUNNING,
                "input_open": True,
                "active_response_id": response_id,
                "last_event_sequence": 1,
            }
        )

        first_thread_view = lab.chat.read_thread_view(lab.identity, lab.thread.id)

        assert len(first_thread_view.turns) == 1
        assert first_thread_view.turns[0].status == "in_progress"
        assert first_thread_view.turns[0].response_id == response_id
        before_thread = lab.chat.read_thread(lab.identity, lab.thread.id)
        before_turn = lab.chat.store.get_turn(before_thread, turn.id)
        original_run_id = before_thread.run_id
        assert before_turn is not None and before_turn.cached_view is not None
        assert original_run_id == lab.lab.run[0].run_id
        assert before_turn.cached_view["status"] == "in_progress"
        assert before_thread.active_turn_id == turn.id

        stopped_at = started_at + timedelta(seconds=5)
        lab.events.extend(
            (
                RunEvent(
                    run_id=lab.lab.run[0].run_id,
                    sequence=2,
                    kind="stop.accepted",
                    occurred_at=started_at + timedelta(seconds=1),
                    operation_id=stop_operation_id,
                    response_id=None,
                    data={},
                ),
                RunEvent(
                    run_id=lab.lab.run[0].run_id,
                    sequence=3,
                    kind="stop.stopped",
                    occurred_at=stopped_at,
                    operation_id=stop_operation_id,
                    response_id=None,
                    data={},
                ),
            )
        )
        lab.lab.run[0] = lab.lab.run[0].model_copy(
            update={
                "state": RunState.STOPPED,
                "input_open": False,
                "active_response_id": None,
                "last_event_sequence": 3,
            }
        )

        stopped_thread_view = lab.chat.read_thread_view(lab.identity, lab.thread.id)
        cancelled = stopped_thread_view.turns[0]

        assert cancelled.status == "cancelled"
        assert cancelled.id == str(turn.id) and cancelled.thread_id == str(
            lab.thread.id
        )
        assert cancelled.run_id == original_run_id
        assert cancelled.response_id == response_id
        assert cancelled.operation_id == operation_id
        assert cancelled.completed_at == stopped_at
        assert cancelled.stream_available is False and cancelled.recovery_mode is None
        assert lab.chat.read_turn(lab.identity, lab.thread.id, turn.id) == cancelled

        saved_thread = lab.chat.read_thread(lab.identity, lab.thread.id)
        saved_turn = lab.chat.store.get_turn(saved_thread, turn.id)
        assert saved_turn is not None and saved_turn.cached_view is not None
        assert saved_turn.cached_view["status"] == "cancelled"
        assert saved_thread.active_turn_id is None
        assert saved_turn.request_state == "accepted"
        assert saved_turn.native_operation_id == turn.native_operation_id
        assert saved_turn.operation_id == operation_id
        assert saved_turn.response_id == response_id
        assert saved_turn.native_request == original_request
        assert saved_thread.run_id == original_run_id

        rejected = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="must not reopen", operation_id="after-confirmed-stop"),
        )
        assert (
            rejected.request_state == "rejected"
            and rejected.reason_code == "run_closed"
        )
        assert rejected.native_request is None
        assert len(lab.starts) == 1 and lab.inputs == lab.stops == []
        assert lab.boundary.open_sessions == 0
    finally:
        lab.product.close()
