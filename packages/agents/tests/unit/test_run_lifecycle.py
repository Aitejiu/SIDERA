from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Event
from uuid import uuid4

import pytest

from sidera_agents.contracts import (
    ContextItem,
    InputStatus,
    RunInput,
    RunPolicy,
    RunState,
    SourceRef,
    StartStatus,
)
from sidera_agents.errors import AgentError
from sidera_agents.input_delivery import InputDeliverySettings


def _start(lab, *, policy: RunPolicy = RunPolicy.CONVERSATION, text: str = "first"):
    receipt = lab.execution.start(
        lab.request(policy=policy.value, text=text, instructions=("Pinned run rule",))
    )
    assert receipt.run_id is not None
    return receipt


def _input(lab, run_id, text: str, *, operation_id=None, context=()):
    return RunInput(
        workspace_id=lab.workspace_id,
        run_id=run_id,
        operation_id=operation_id or uuid4(),
        grant_id=lab.grant_id,
        text=text,
        context=context,
    )


def _context(text: str) -> ContextItem:
    return ContextItem(
        item_id="new-context",
        origin="user",
        required=True,
        ordinal=0,
        version="v1",
        content_hash="sha256:" + sha256(text.encode()).hexdigest(),
        text=text,
        source_ref=None,
    )


def _block_next_prompt(lab, monkeypatch):
    entered = Event()
    release = Event()
    original = lab.native.prompt

    def prompt(handle, assembled, response_id):
        entered.set()
        if not release.wait(timeout=3):
            raise RuntimeError("controlled prompt was not released")
        return original(handle, assembled, response_id)

    monkeypatch.setattr(lab.native, "prompt", prompt)
    return entered, release


def test_conversation_reuses_one_session_and_pinned_projection_for_repeated_input(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    input_request = _input(
        lab, start.run_id, "second turn", context=(_context("fresh context"),)
    )

    accepted = lab.execution.send_input(input_request)
    assert accepted.status is InputStatus.ACCEPTED
    assert lab.execution.send_input(input_request) == accepted
    with pytest.raises(AgentError, match="different request") as conflict:
        lab.execution.send_input(input_request.model_copy(update={"text": "changed"}))
    assert conflict.value.code == "operation_conflict"

    lab.pump()
    delivered = lab.execution.get_input(
        lab.workspace_id,
        start.run_id,
        input_request.operation_id,
        grant_id=lab.grant_id,
    )
    assert delivered.status is InputStatus.DELIVERED
    assert delivered.evidence_sequence is not None
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 2
    first_handle, second_input, _ = lab.native.prompt_calls[-1]
    assert first_handle == lab.native.handles[start.run_id]
    assert second_input.text == "second turn"
    assert second_input.instructions == (
        "Follow the test request.",
        "Pinned run rule",
    )
    assert second_input.context[0].text == "fresh context"
    snapshot = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    assert snapshot.state is RunState.IDLE
    assert snapshot.input_open
    events = lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
    assert [event.sequence for event in events] == list(
        range(1, snapshot.last_event_sequence + 1)
    )
    assert all("payload_bytes" not in event.data for event in events)
    assert all(
        set(event.data) <= {"status", "reason_code", "queue_sequence"}
        for event in events
    )
    entry = lab.store.get_runtime_entry(lab.runtime.ref)
    assert (entry.installation.active_count, entry.installation.session_count) == (0, 1)

    later = _input(lab, start.run_id, "third turn")
    assert lab.execution.send_input(later).status is InputStatus.ACCEPTED
    lab.pump()
    assert (
        lab.execution.get_input(
            lab.workspace_id, start.run_id, later.operation_id, grant_id=lab.grant_id
        ).status
        is InputStatus.DELIVERED
    )
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 3


def test_input_during_native_open_handoff_is_delivered_once_on_same_handle(
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

    send_started = Event()
    original_send = lab.execution.send_input

    def tracked_send(request):
        send_started.set()
        return original_send(request)

    monkeypatch.setattr(lab.execution, "_transition_run", gated_transition)
    monkeypatch.setattr(lab.execution, "send_input", tracked_send)
    request = _input(lab, start.run_id, "arrives during native-open handoff")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pumping = pool.submit(lab.pump)
            assert persisted.wait(timeout=2)
            sending = pool.submit(lab.execution.send_input, request)
            assert send_started.wait(timeout=2)
            release_handoff.set()
            receipt = sending.result(timeout=3)
            pumping.result(timeout=3)

        assert receipt.status is InputStatus.ACCEPTED
        lab.pump()
        delivered = lab.execution.get_input(
            lab.workspace_id,
            start.run_id,
            request.operation_id,
            grant_id=lab.grant_id,
        )
        assert delivered.status is InputStatus.DELIVERED
        assert [call[1].text for call in lab.native.prompt_calls] == [
            "first",
            request.text,
        ]
        assert len(lab.native.open_calls) == 1
        assert all(
            call[0] == lab.native.handles[start.run_id]
            for call in lab.native.prompt_calls
        )
    finally:
        release_handoff.set()


def test_busy_inputs_are_delivered_in_acceptance_order_before_bounded_close(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab, policy=RunPolicy.BOUNDED)
    entered, release = _block_next_prompt(lab, monkeypatch)
    first_input = _input(lab, start.run_id, "queued one")
    second_input = _input(lab, start.run_id, "queued two")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        first_receipt = lab.execution.send_input(first_input)
        second_receipt = lab.execution.send_input(second_input)
        assert first_receipt.status is InputStatus.ACCEPTED
        assert second_receipt.status is InputStatus.ACCEPTED
        release.set()
        pumping.result(timeout=3)

    assert [call[1].text for call in lab.native.prompt_calls] == [
        "first",
        "queued one",
        "queued two",
    ]
    assert (
        lab.execution.get_input(
            lab.workspace_id,
            start.run_id,
            first_input.operation_id,
            grant_id=lab.grant_id,
        ).status
        is InputStatus.DELIVERED
    )
    assert (
        lab.execution.get_input(
            lab.workspace_id,
            start.run_id,
            second_input.operation_id,
            grant_id=lab.grant_id,
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


def test_unknown_input_ack_is_never_resent(admission_lab, monkeypatch):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    request = _input(lab, start.run_id, "possibly applied")
    original = lab.native.prompt

    def lose_ack(handle, assembled, response_id):
        result = original(handle, assembled, response_id)
        if assembled.text == request.text:
            raise OSError("lost controlled response")
        return result

    monkeypatch.setattr(lab.native, "prompt", lose_ack)
    assert lab.execution.send_input(request).status is InputStatus.ACCEPTED
    lab.pump()
    receipt = lab.execution.get_input(
        lab.workspace_id, start.run_id, request.operation_id, grant_id=lab.grant_id
    )
    assert receipt.status is InputStatus.UNKNOWN
    assert lab.pump() == 0
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 2


def test_idle_input_retries_capacity_rejection_after_active_slot_releases(
    admission_lab, monkeypatch
):
    lab = admission_lab
    idle_run = _start(lab)
    lab.pump()
    busy_run = _start(lab, policy=RunPolicy.BOUNDED, text="occupy active slot")
    entered, release = _block_next_prompt(lab, monkeypatch)
    request = _input(lab, idle_run.run_id, "retry after capacity")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        rejected = lab.execution.send_input(request)
        assert rejected.status is InputStatus.REJECTED
        assert rejected.reason_code == "resource_busy"
        release.set()
        pumping.result(timeout=3)

    assert (
        lab.execution.get_run(
            lab.workspace_id, busy_run.run_id, grant_id=lab.grant_id
        ).state
        is RunState.COMPLETED
    )
    retried = lab.execution.send_input(request)
    assert retried.status is InputStatus.ACCEPTED
    lab.pump()
    assert (
        lab.execution.get_input(
            lab.workspace_id,
            idle_run.run_id,
            request.operation_id,
            grant_id=lab.grant_id,
        ).status
        is InputStatus.DELIVERED
    )
    assert len(lab.native.open_calls) == 2


def test_queue_and_byte_limits_reject_only_new_inputs(admission_lab, monkeypatch):
    lab = admission_lab
    start = _start(lab, policy=RunPolicy.BOUNDED)
    monkeypatch.setattr(
        lab.execution,
        "_input_settings",
        InputDeliverySettings(
            max_pending=1, max_input_bytes=512, drain_deadline_seconds=5
        ),
    )
    entered, release = _block_next_prompt(lab, monkeypatch)
    too_large = _input(lab, start.run_id, "x" * 513)
    accepted = _input(lab, start.run_id, "one")
    over_queue = _input(lab, start.run_id, "two")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        large_receipt = lab.execution.send_input(too_large)
        accepted_receipt = lab.execution.send_input(accepted)
        queue_receipt = lab.execution.send_input(over_queue)
        assert large_receipt.status is InputStatus.REJECTED
        assert large_receipt.reason_code == "input_too_large"
        assert accepted_receipt.status is InputStatus.ACCEPTED
        assert queue_receipt.status is InputStatus.REJECTED
        assert queue_receipt.reason_code == "input_queue_full"
        release.set()
        pumping.result(timeout=3)

    assert (
        lab.execution.get_input(
            lab.workspace_id, start.run_id, accepted.operation_id, grant_id=lab.grant_id
        ).status
        is InputStatus.DELIVERED
    )
    assert (
        lab.execution.get_input(
            lab.workspace_id,
            start.run_id,
            too_large.operation_id,
            grant_id=lab.grant_id,
        ).reason_code
        == "input_too_large"
    )
    assert [call[1].text for call in lab.native.prompt_calls] == ["first", "one"]


def test_source_reference_metadata_counts_toward_input_byte_limit(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    monkeypatch.setattr(
        lab.execution,
        "_input_settings",
        InputDeliverySettings(
            max_pending=4, max_input_bytes=256, drain_deadline_seconds=5
        ),
    )
    source_context = ContextItem(
        item_id="large-source",
        origin="user",
        required=True,
        ordinal=0,
        version="v1",
        content_hash="sha256:" + "0" * 64,
        source_ref=SourceRef(
            provider="provider-" + "p" * 256,
            key="key-" + "k" * 256,
            version="v1",
        ),
    )
    request = _input(lab, start.run_id, "x", context=(source_context,))

    receipt = lab.execution.send_input(request)

    assert receipt.status is InputStatus.REJECTED
    assert receipt.reason_code == "input_too_large"
    assert not any(
        event.kind == "input.accepted" and event.operation_id == request.operation_id
        for event in lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
    )
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.IDLE
    )


def test_many_metadata_only_context_items_count_toward_input_byte_limit(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    monkeypatch.setattr(
        lab.execution,
        "_input_settings",
        InputDeliverySettings(
            max_pending=4, max_input_bytes=512, drain_deadline_seconds=5
        ),
    )
    context = tuple(
        ContextItem(
            item_id=f"source-{index:02d}",
            origin="user",
            required=True,
            ordinal=index,
            version="v1",
            content_hash="sha256:" + "0" * 64,
            source_ref=SourceRef(
                provider="files", key=f"document-{index:02d}", version="v1"
            ),
        )
        for index in range(6)
    )
    request = _input(lab, start.run_id, "x", context=context)

    receipt = lab.execution.send_input(request)

    assert receipt.status is InputStatus.REJECTED
    assert receipt.reason_code == "input_too_large"
    assert not any(
        event.kind == "input.accepted" and event.operation_id == request.operation_id
        for event in lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
    )
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.IDLE
    )


def test_expired_drain_fails_accepted_input_without_sending_it(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab, policy=RunPolicy.BOUNDED)
    now = [10.0]
    monkeypatch.setattr(lab.execution, "_monotonic", lambda: now[0])
    monkeypatch.setattr(
        lab.execution,
        "_input_settings",
        InputDeliverySettings(
            max_pending=2, max_input_bytes=1024, drain_deadline_seconds=5
        ),
    )
    entered, release = _block_next_prompt(lab, monkeypatch)
    request = _input(lab, start.run_id, "expires before next turn")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pumping = pool.submit(lab.pump)
        assert entered.wait(timeout=2)
        assert lab.execution.send_input(request).status is InputStatus.ACCEPTED
        now[0] = 16.0
        release.set()
        pumping.result(timeout=3)

    receipt = lab.execution.get_input(
        lab.workspace_id, start.run_id, request.operation_id, grant_id=lab.grant_id
    )
    assert receipt.status is InputStatus.FAILED
    assert receipt.reason_code == "input_drain_deadline"
    assert [call[1].text for call in lab.native.prompt_calls] == ["first"]
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.COMPLETED
    )


def test_expired_idle_input_fails_while_active_capacity_remains_unavailable(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    now = [10.0]
    monkeypatch.setattr(lab.execution, "_monotonic", lambda: now[0])
    monkeypatch.setattr(
        lab.execution,
        "_input_settings",
        InputDeliverySettings(
            max_pending=2, max_input_bytes=1024, drain_deadline_seconds=5
        ),
    )

    first_input = _input(lab, start.run_id, "active response")
    expired_input = _input(lab, start.run_id, "expired while busy")
    assert lab.execution.send_input(first_input).status is InputStatus.ACCEPTED

    active_prompt_entered = Event()
    release_active_prompt = Event()
    finish_entered = Event()
    release_finish = Event()
    original_prompt = lab.native.prompt
    original_finish = lab.store.finish_conversation_response

    def gated_prompt(handle, assembled, response_id):
        if assembled.text == first_input.text:
            active_prompt_entered.set()
            assert release_active_prompt.wait(timeout=3)
        return original_prompt(handle, assembled, response_id)

    finish_blocked = False

    def gated_finish(run_id, response_id, **kwargs):
        nonlocal finish_blocked
        result = original_finish(run_id, response_id, **kwargs)
        if run_id == start.run_id and not finish_blocked:
            finish_blocked = True
            finish_entered.set()
            assert release_finish.wait(timeout=3)
        return result

    monkeypatch.setattr(lab.native, "prompt", gated_prompt)
    monkeypatch.setattr(lab.store, "finish_conversation_response", gated_finish)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            target_poll = pool.submit(lab.pump)
            assert active_prompt_entered.wait(timeout=2)
            assert (
                lab.execution.send_input(expired_input).status is InputStatus.ACCEPTED
            )
            release_active_prompt.set()
            assert finish_entered.wait(timeout=2)

            competing_run = _start(
                lab, policy=RunPolicy.BOUNDED, text="occupy active capacity"
            )
            assert competing_run.status is StartStatus.ACCEPTED
            now[0] = 16.0
            release_finish.set()
            target_poll.result(timeout=3)

            assert (
                lab.execution.get_run(
                    lab.workspace_id, competing_run.run_id, grant_id=lab.grant_id
                ).state
                is RunState.STARTING
            )
            assert (
                lab.store.get_runtime_entry(lab.runtime.ref).installation.active_count
                == 1
            )
            expired_receipt = lab.execution.get_input(
                lab.workspace_id,
                start.run_id,
                expired_input.operation_id,
                grant_id=lab.grant_id,
            )
            assert expired_receipt.status is InputStatus.FAILED
            assert expired_receipt.reason_code == "input_drain_deadline"
            assert [call[1].text for call in lab.native.prompt_calls] == [
                "first",
                first_input.text,
            ]
    finally:
        release_active_prompt.set()
        release_finish.set()


def test_conversation_starts_a_fresh_deadline_for_a_later_input_queue(
    admission_lab, monkeypatch
):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    now = [10.0]
    monkeypatch.setattr(lab.execution, "_monotonic", lambda: now[0])
    monkeypatch.setattr(
        lab.execution,
        "_input_settings",
        InputDeliverySettings(
            max_pending=2, max_input_bytes=1024, drain_deadline_seconds=5
        ),
    )
    expired = _input(lab, start.run_id, "expired queue")
    assert lab.execution.send_input(expired).status is InputStatus.ACCEPTED
    now[0] = 16.0
    lab.pump()
    assert (
        lab.execution.get_input(
            lab.workspace_id, start.run_id, expired.operation_id, grant_id=lab.grant_id
        ).status
        is InputStatus.FAILED
    )

    later = _input(lab, start.run_id, "later queue")
    assert lab.execution.send_input(later).status is InputStatus.ACCEPTED
    lab.pump()
    assert (
        lab.execution.get_input(
            lab.workspace_id, start.run_id, later.operation_id, grant_id=lab.grant_id
        ).status
        is InputStatus.DELIVERED
    )
    assert [call[1].text for call in lab.native.prompt_calls] == [
        "first",
        "later queue",
    ]


def test_input_cannot_replace_the_pinned_grant_or_workspace(admission_lab):
    lab = admission_lab
    start = _start(lab)
    lab.pump()
    other_grant = uuid4()
    lab.authorizer.grant_workspace(lab.workspace_id, other_grant)
    request = _input(lab, start.run_id, "wrong grant")
    with pytest.raises(AgentError) as denied:
        lab.execution.send_input(request.model_copy(update={"grant_id": other_grant}))
    assert denied.value.code == "permission_denied"

    with pytest.raises(AgentError) as denied_workspace:
        lab.execution.send_input(request.model_copy(update={"workspace_id": uuid4()}))
    assert denied_workspace.value.code == "permission_denied"
