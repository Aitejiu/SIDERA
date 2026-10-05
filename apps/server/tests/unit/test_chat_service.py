import importlib.util
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from sidera_agents.contracts import (
    CapabilitySupport,
    EventPage,
    InputReceipt,
    InputStatus,
    ProfileStatus,
    RunEvent,
    RunPolicy,
    RunState,
    StartLookup,
    StartReceipt,
    StartStatus,
    StopReceipt,
    StopStatus,
    SupportStatus,
)
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import ControlPlaneIdentity

from sidera_product.chat.contracts import CreateThreadBody, StartTurnBody
from sidera_product.postgres.store import PostgresProductStore
from tests.unit.test_chat_store import JournalBoundary
from tests.unit.test_lifecycle import (
    controlled_owner_runtime as controlled_owner_runtime,
)
from tests.unit.test_product_grants import grant_fixture


def chat_lab(settings_values, tmp_path, monkeypatch):
    assert importlib.util.find_spec("sidera_product.chat.service") is not None, (
        "N19 needs actual persistent empty conversation creation"
    )
    from sidera_product.chat.service import ChatError, ChatService
    from sidera_product.postgres import authority_store, chat_store
    from sidera_product.postgres import store as product_module

    lab = grant_fixture(settings_values, tmp_path)
    product = PostgresProductStore(str(settings_values["product_database_url"]))
    boundary = JournalBoundary(product.engine)
    monkeypatch.setattr(chat_store, "Session", boundary.session)
    monkeypatch.setattr(product_module, "Session", boundary.session)
    monkeypatch.setattr(authority_store, "Session", boundary.session)
    journal = chat_store.PostgresChatStore(product.engine)
    receipt_store = authority_store.PostgresAuthorityStore(product.engine)
    identity = lab.sessions.identity_for_grant(lab.parent.session_id)
    reads = []
    profiles = {lab.profile.ref: lab.profile}

    def get_profile(ref, *, grant_id):
        lab.host.require(grant_id, "profile.read", ref)
        reads.append((grant_id, ref))
        return profiles[ref]

    monkeypatch.setattr(lab.catalog, "get_profile", get_profile)
    monkeypatch.setattr(
        lab.catalog,
        "get_profile_status",
        lambda ref, **options: ProfileStatus(
            profile_id=ref.profile_id, enabled=True, version=1
        ),
    )
    chat = ChatService(
        settings=lab.sessions.settings,
        product_store=product,
        store=journal,
        sessions=lab.sessions,
        catalog=lab.catalog,
        runtimes=lab.grants.runtimes,
        execution=lab.grants.execution,
        grants=lab.grants,
        authority_store=lab.store,
        receipt_store=receipt_store,
        clock=lambda: lab.clock[0],
    )
    return SimpleNamespace(
        chat=chat,
        lab=lab,
        product=product,
        boundary=boundary,
        identity=identity,
        reads=reads,
        error_type=ChatError,
        profiles=profiles,
    )


def test_empty_conversation_pins_authorized_profile_and_starts_nothing(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = chat_lab(settings_values, tmp_path, monkeypatch)
    try:
        conversation = lab.chat.create_thread(
            lab.identity,
            CreateThreadBody(
                title="Pinned",
                context_refs=("task:opaque",),
                profile_ref=lab.lab.profile.ref,
            ),
        )
        assert conversation.profile_ref == lab.lab.profile.ref
        assert conversation.title == "Pinned" and conversation.context_refs == (
            "task:opaque",
        )
        assert (
            conversation.grant_id
            is conversation.run_id
            is conversation.start_operation_id
            is None
        )
        assert lab.lab.calls == []
        assert lab.boundary.open_sessions == 0
        assert lab.reads == [(lab.identity.grant_id, lab.lab.profile.ref)]
        assert lab.chat.read_thread(lab.identity, conversation.id) == conversation
    finally:
        lab.product.close()


def test_missing_default_does_not_infer_global_profile_or_start(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = chat_lab(settings_values, tmp_path, monkeypatch)
    try:
        with pytest.raises(lab.error_type) as error:
            lab.chat.create_thread(lab.identity, CreateThreadBody())
        assert error.value.code == "configuration_unavailable"
        assert lab.boundary.rows == {} and lab.reads == [] and lab.lab.calls == []
    finally:
        lab.product.close()


@pytest.mark.parametrize(
    "variant",
    ["foreign", "principal", "agent", "revoked", "expired", "profile_mismatch"],
)
def test_empty_conversation_denies_foreign_or_noncurrent_authority_before_native_io(
    settings_values, tmp_path, monkeypatch, no_database_connections, variant
):
    lab = chat_lab(settings_values, tmp_path, monkeypatch)
    identity, ref = lab.identity, lab.lab.profile.ref
    if variant == "foreign":
        ref = ref.model_copy(update={"workspace_id": uuid4()})
    elif variant == "principal":
        identity = ControlPlaneIdentity("foreign-owner", identity.grant_id, "user")
    elif variant == "agent":
        identity = ControlPlaneIdentity(
            identity.principal_id, identity.grant_id, "agent"
        )
    elif variant == "revoked":
        lab.lab.sessions.revoke(identity.grant_id)
    elif variant == "expired":
        lab.lab.clock[0] = lab.lab.parent.expires_at
    else:
        lab.profiles[ref] = lab.lab.profile.model_copy(
            update={"ref": ref.model_copy(update={"revision": ref.revision + 1})}
        )
    try:
        with pytest.raises((lab.error_type, AgentError)):
            lab.chat.create_thread(identity, CreateThreadBody(profile_ref=ref))
        assert lab.boundary.rows == {} and lab.lab.calls == []
        assert lab.boundary.open_sessions == 0
    finally:
        lab.product.close()


def test_default_and_head_changes_apply_only_to_new_empty_threads(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = chat_lab(settings_values, tmp_path, monkeypatch)
    workspace = lab.lab.profile.ref.workspace_id
    try:
        lab.product.write_defaults(
            workspace,
            expected_version=0,
            main_profile_id=lab.lab.profile.ref.profile_id,
            planner_profile_id=None,
            worker_profile_id=None,
        )
        first = lab.chat.create_thread(lab.identity, CreateThreadBody())
        head = lab.lab.profile.model_copy(
            update={"ref": lab.lab.profile.ref.model_copy(update={"revision": 3})}
        )
        lab.profiles[head.ref] = head
        lab.lab.available[:] = [head]
        second = lab.chat.create_thread(lab.identity, CreateThreadBody())
        new_default = head.model_copy(
            update={
                "ref": head.ref.model_copy(
                    update={"profile_id": uuid4(), "revision": 1}
                )
            }
        )
        lab.profiles[new_default.ref] = new_default
        lab.lab.available[:] = [head, new_default]
        lab.product.write_defaults(
            workspace,
            expected_version=1,
            main_profile_id=new_default.ref.profile_id,
            planner_profile_id=None,
            worker_profile_id=None,
        )
        third = lab.chat.create_thread(lab.identity, CreateThreadBody())
        assert first.profile_ref.revision == 2
        assert second.profile_ref == head.ref
        assert third.profile_ref == new_default.ref
        assert (
            lab.chat.read_thread(lab.identity, first.id).profile_ref
            == first.profile_ref
        )
        assert (
            lab.chat.read_thread(lab.identity, second.id).profile_ref
            == second.profile_ref
        )
        assert lab.lab.calls == [] and lab.boundary.open_sessions == 0
    finally:
        lab.product.close()


def test_same_owner_thread_requires_current_cookie_and_foreign_thread_is_not_disclosed(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = chat_lab(settings_values, tmp_path, monkeypatch)
    try:
        thread = lab.chat.create_thread(
            lab.identity, CreateThreadBody(profile_ref=lab.lab.profile.ref)
        )
        with pytest.raises(lab.error_type) as error:
            lab.chat.read_thread(lab.identity, uuid4())
        assert error.value.code == "thread_not_found"
        lab.lab.sessions.revoke(lab.identity.grant_id)
        with pytest.raises(lab.error_type):
            lab.chat.read_thread(lab.identity, thread.id)
        assert lab.lab.calls == []
    finally:
        lab.product.close()


def admission_lab(settings_values, tmp_path, monkeypatch):
    lab = chat_lab(settings_values, tmp_path, monkeypatch)
    lab.chat.settings.artifact_root.mkdir(parents=True, mode=0o700)
    thread = lab.chat.create_thread(
        lab.identity, CreateThreadBody(profile_ref=lab.lab.profile.ref)
    )
    starts, inputs, queries = [], [], []
    receipts = {}
    input_receipts, events = {}, []
    outcome = [StartStatus.ACCEPTED]
    lost = [False]

    def start(request):
        assert lab.boundary.open_sessions == 0
        stored = lab.chat.store.get_conversation(
            conversation_id=thread.id,
            workspace_id=thread.workspace_id,
            principal_id=thread.principal_id,
        )
        turns = lab.chat.store.list_turns(stored)
        assert len(turns) == 1 and turns[0].request_state in ("dispatching", "unknown")
        assert turns[0].native_request == request.model_dump(mode="json")
        starts.append(request)
        receipt = StartReceipt(
            operation_id=request.operation_id,
            status=outcome[0],
            run_id=lab.lab.run[0].run_id
            if outcome[0] is StartStatus.ACCEPTED
            else None,
            reason_code="resource_busy" if outcome[0] is StartStatus.BUSY else None,
        )
        receipts[request.operation_id] = receipt
        lab.lab.lookup[0] = StartLookup(found=True, receipt=receipt)
        if lost[0]:
            raise OSError("controlled lost native reply")
        return receipt

    def get_start(workspace, operation, *, grant_id):
        assert lab.boundary.open_sessions == 0
        queries.append(("get_start", workspace, operation, grant_id))
        receipt = receipts.get(operation)
        return StartLookup(found=receipt is not None, receipt=receipt)

    def send_input(request):
        assert lab.boundary.open_sessions == 0
        inputs.append(request)
        receipt = InputReceipt(
            operation_id=request.operation_id,
            run_id=request.run_id,
            status=InputStatus.ACCEPTED,
            reason_code=None,
            evidence_sequence=3,
        )
        input_receipts[request.operation_id] = receipt
        return receipt

    def get_input(workspace, run_id, operation, *, grant_id):
        assert lab.boundary.open_sessions == 0
        queries.append(("get_input", workspace, operation, grant_id))
        return input_receipts[operation]

    def get_run(workspace, run_id, *, grant_id):
        assert lab.boundary.open_sessions == 0
        queries.append(("get_run", workspace, run_id, grant_id))
        return lab.lab.run[0]

    def list_events(workspace, run_id, after_sequence, limit=100, *, grant_id):
        assert lab.boundary.open_sessions == 0
        queries.append(("list_events", workspace, run_id, grant_id))
        selected = tuple(event for event in events if event.sequence > after_sequence)[
            :limit
        ]
        return EventPage(
            events=selected,
            next_sequence=selected[-1].sequence if selected else after_sequence,
            has_more=False,
        )

    monkeypatch.setattr(lab.chat.execution, "start", start)
    monkeypatch.setattr(lab.chat.execution, "get_start", get_start)
    monkeypatch.setattr(lab.chat.execution, "get_run", get_run)
    monkeypatch.setattr(lab.chat.execution, "send_input", send_input, raising=False)
    monkeypatch.setattr(lab.chat.execution, "get_input", get_input, raising=False)
    monkeypatch.setattr(lab.chat.execution, "list_events", list_events, raising=False)
    lab.thread, lab.starts, lab.inputs, lab.queries = thread, starts, inputs, queries
    lab.receipts, lab.outcome, lab.lost = receipts, outcome, lost
    lab.input_receipts, lab.events = input_receipts, events
    return lab


@pytest.mark.parametrize(
    "external", ["fallback:exact", "7974b8a4-f536-437f-a2bd-15b7252dd649", " x "]
)
def test_first_input_freezes_original_request_before_one_native_start(
    settings_values, tmp_path, monkeypatch, no_database_connections, external
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        turn = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="hello", operation_id=external),
        )
        request = lab.starts[0]
        assert turn.operation_id == external
        assert turn.native_operation_id == uuid5(
            lab.thread.id, "sidera.chat.input:" + external
        )
        assert request.operation_id == turn.native_operation_id
        assert request.profile_ref == lab.thread.profile_ref
        assert request.policy is RunPolicy.CONVERSATION
        assert request.text == "hello" and request.instructions == request.context == ()
        assert request.required_capabilities == ("mcp",)
        assert request.tools[0].connection_id == lab.chat.grants.connection_id
        assert turn.request_state == "accepted" and len(lab.starts) == 1
        duplicate = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="hello", operation_id=external),
        )
        assert (
            duplicate.id == turn.id
            and duplicate.native_operation_id == turn.native_operation_id
        )
        assert len(lab.starts) == 1 and lab.inputs == []
        assert next(call for call in reversed(lab.queries) if call[0] == "get_start")[
            2:
        ] == (turn.native_operation_id, request.grant_id)
        assert lab.queries[-1][0] == "get_run" and lab.queries[-1][2:] == (
            lab.lab.run[0].run_id,
            request.grant_id,
        )
    finally:
        lab.product.close()


def test_missing_operation_is_generated_once_and_changed_payload_denied_before_native_io(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        turn = lab.chat.start_turn(
            lab.identity, lab.thread.id, StartTurnBody(input="original")
        )
        UUID(turn.operation_id)
        counts = len(lab.starts), len(lab.queries)
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(
                lab.identity,
                lab.thread.id,
                StartTurnBody(input="changed", operation_id=turn.operation_id),
            )
        assert failure.value.code == "operation_conflict"
        assert (len(lab.starts), len(lab.queries)) == counts
    finally:
        lab.product.close()


@pytest.mark.parametrize("found", [True, False])
def test_unknown_or_lost_reply_restart_queries_only_original(
    settings_values, tmp_path, monkeypatch, no_database_connections, found
):
    from sidera_product.chat.service import ChatService

    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    lab.lost[0] = True
    try:
        body = StartTurnBody(input="original", operation_id="lost:original")
        turn = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert turn.request_state == "unknown" and len(lab.starts) == 1
        if not found:
            lab.receipts.clear()
        restarted = ChatService(
            settings=lab.chat.settings,
            product_store=lab.product,
            store=lab.chat.store,
            sessions=lab.chat.sessions,
            catalog=lab.chat.catalog,
            runtimes=lab.chat.runtimes,
            execution=lab.chat.execution,
            grants=lab.chat.grants,
            authority_store=lab.chat.authority_store,
            clock=lab.chat.clock,
            receipt_store=lab.chat.receipt_store,
        )
        recovered = restarted.start_turn(lab.identity, lab.thread.id, body)
        assert recovered.id == turn.id and len(lab.starts) == 1
        assert recovered.request_state == ("accepted" if found else "unknown")
        assert next(call for call in reversed(lab.queries) if call[0] == "get_start")[
            2:
        ] == (turn.native_operation_id, lab.starts[0].grant_id)
        with pytest.raises(lab.error_type) as failure:
            restarted.start_turn(
                lab.identity,
                lab.thread.id,
                StartTurnBody(input="another", operation_id="new"),
            )
        assert failure.value.code == "thread_busy" and len(lab.starts) == 1
    finally:
        lab.product.close()


def test_explicit_busy_re_admits_only_same_frozen_intent(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    lab.outcome[0] = StartStatus.BUSY
    try:
        body = StartTurnBody(input="original", operation_id="busy:original")
        refused = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert (
            refused.request_state == "busy" and refused.reason_code == "resource_busy"
        )
        original = lab.starts[0]
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(
                lab.identity,
                lab.thread.id,
                StartTurnBody(input="changed", operation_id=body.operation_id),
            )
        assert failure.value.code == "operation_conflict" and len(lab.starts) == 1
        lab.outcome[0] = StartStatus.ACCEPTED
        admitted = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert admitted.id == refused.id and admitted.request_state == "accepted"
        assert lab.starts == [original, original]
    finally:
        lab.product.close()


def test_parallel_identical_request_during_original_dispatch_does_not_resend(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    entered, release = Event(), Event()
    results, errors = [], []
    original = lab.chat.execution.start

    def held_start(request):
        saved = lab.chat.store.list_turns(
            lab.chat.read_thread(lab.identity, lab.thread.id)
        )
        assert len(saved) == 1 and saved[0].request_state == "dispatching"
        entered.set()
        assert release.wait(5)
        return original(request)

    monkeypatch.setattr(lab.chat.execution, "start", held_start)
    body = StartTurnBody(input="original", operation_id="parallel:original")

    def caller():
        try:
            results.append(lab.chat.start_turn(lab.identity, lab.thread.id, body))
        except Exception as error:  # noqa: BLE001 - assert collected thread failures below
            errors.append(error)

    thread = Thread(target=caller)
    try:
        thread.start()
        assert entered.wait(5)
        duplicate = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert duplicate.request_state == "unknown"
        assert lab.starts == [] and len(lab.queries) == 1
        release.set()
        thread.join(5)
        assert not thread.is_alive() and errors == []
        assert len(lab.starts) == 1 and results[0].id == duplicate.id
        assert (
            len(
                lab.chat.store.list_turns(
                    lab.chat.read_thread(lab.identity, lab.thread.id)
                )
            )
            == 1
        )
    finally:
        release.set()
        thread.join(5)
        lab.product.close()


def threaded_turn(lab, body, results, errors):
    def call():
        try:
            results.append(lab.chat.start_turn(lab.identity, lab.thread.id, body))
        except Exception as error:  # noqa: BLE001 - collected errors are asserted by the caller
            errors.append(error)

    thread = Thread(target=call)
    thread.start()
    return thread


def test_pre_dispatch_duplicate_preserves_prepared_winner_without_native_query(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    entered, release = Event(), Event()
    results, errors = [], []
    original = lab.chat.store.mark_dispatching

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(lab.chat.store, "mark_dispatching", held)
    body = StartTurnBody(input="original", operation_id="prepared:original")
    thread = threaded_turn(lab, body, results, errors)
    try:
        assert entered.wait(5)
        duplicate = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert duplicate.request_state == "prepared"
        assert lab.starts == lab.inputs == lab.queries == []
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(
                lab.identity,
                lab.thread.id,
                StartTurnBody(input="other", operation_id="other"),
            )
        assert failure.value.code == "thread_busy"
        release.set()
        thread.join(5)
        assert not thread.is_alive() and errors == []
        assert results[0].id == duplicate.id and results[0].request_state == "accepted"
        assert len(lab.starts) == 1
    finally:
        release.set()
        thread.join(5)
        lab.product.close()


def test_old_busy_observation_cannot_release_current_explicit_redispatch(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    entered, release = Event(), Event()
    results, errors = [], []
    body = StartTurnBody(input="original", operation_id="redispatch:original")
    lab.outcome[0] = StartStatus.BUSY
    refused = lab.chat.start_turn(lab.identity, lab.thread.id, body)
    original_start = lab.chat.execution.start

    def held(request):
        entered.set()
        assert release.wait(5)
        return original_start(request)

    monkeypatch.setattr(lab.chat.execution, "start", held)
    lab.outcome[0] = StartStatus.ACCEPTED
    thread = threaded_turn(lab, body, results, errors)
    try:
        assert entered.wait(5)
        duplicate = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert duplicate.id == refused.id and duplicate.request_state in (
            "dispatching",
            "unknown",
        )
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(
                lab.identity,
                lab.thread.id,
                StartTurnBody(input="other", operation_id="other"),
            )
        assert failure.value.code == "thread_busy" and len(lab.starts) == 1
        release.set()
        thread.join(5)
        assert not thread.is_alive() and errors == []
        assert results[0].request_state == "accepted" and len(lab.starts) == 2
        assert lab.starts[0] == lab.starts[1]
    finally:
        release.set()
        thread.join(5)
        lab.product.close()


@pytest.mark.parametrize("stale_status", ["not_found", "busy"])
def test_late_original_observation_cannot_demote_settled_accepted_or_release_guard(
    settings_values, tmp_path, monkeypatch, no_database_connections, stale_status
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    settled, release_settle, queried, release_query = Event(), Event(), Event(), Event()
    results, errors, query_results, query_errors = [], [], [], []
    body = StartTurnBody(input="original", operation_id="late:original")
    lab.outcome[0] = StartStatus.BUSY
    old = lab.chat.start_turn(lab.identity, lab.thread.id, body)
    old_receipt = lab.receipts[old.native_operation_id]
    lab.outcome[0] = StartStatus.ACCEPTED
    original_settle = lab.chat.store.settle_turn
    original_query = lab.chat.execution.get_start

    def held_settle(*args, **kwargs):
        if kwargs["state"] == "accepted":
            settled.set()
            assert release_settle.wait(5)
        return original_settle(*args, **kwargs)

    def held_query(workspace, operation, *, grant_id):
        original_query(workspace, operation, grant_id=grant_id)
        queried.set()
        assert release_query.wait(5)
        return StartLookup(
            found=stale_status == "busy",
            receipt=old_receipt if stale_status == "busy" else None,
        )

    monkeypatch.setattr(lab.chat.store, "settle_turn", held_settle)
    monkeypatch.setattr(lab.chat.execution, "get_start", held_query)
    winner = threaded_turn(lab, body, results, errors)
    observer = None
    try:
        assert settled.wait(5)
        observer = threaded_turn(lab, body, query_results, query_errors)
        assert queried.wait(5)
        release_settle.set()
        winner.join(5)
        assert (
            not winner.is_alive()
            and errors == []
            and results[0].request_state == "accepted"
        )
        release_query.set()
        observer.join(5)
        assert not observer.is_alive() and query_errors == []
        current = lab.chat.read_thread(lab.identity, lab.thread.id)
        saved = lab.chat.store.get_turn(current, old.id)
        assert saved.request_state == "accepted" and saved.id == results[0].id
        assert current.run_id == lab.lab.run[0].run_id
        assert current.active_turn_id == saved.id and len(lab.starts) == 2
    finally:
        release_settle.set()
        release_query.set()
        winner.join(5)
        if observer is not None:
            observer.join(5)
        lab.product.close()


def test_persisted_stop_fence_denies_same_busy_intent_without_reopening_guard(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    lab.outcome[0] = StartStatus.BUSY
    try:
        body = StartTurnBody(input="original", operation_id="closed:busy")
        lab.chat.start_turn(lab.identity, lab.thread.id, body)
        current = lab.chat.read_thread(lab.identity, lab.thread.id)
        stopped = lab.chat.store.prepare_stop(current, now=lab.chat.clock())
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert (
            failure.value.code == "run_closed"
            and len(lab.starts) == 1
            and lab.inputs == []
        )
        saved = lab.chat.read_thread(lab.identity, lab.thread.id)
        assert (
            saved.stop_operation_id == stopped.stop_operation_id
            and saved.active_turn_id is None
        )
    finally:
        lab.product.close()


def completed_response(lab, turn, *, text="first reply"):
    response = uuid4()
    after = lab.events[-1].sequence if lab.events else 0
    lab.events.extend(
        [
            RunEvent(
                run_id=lab.lab.run[0].run_id,
                sequence=after + offset,
                kind=kind,
                occurred_at=lab.chat.clock(),
                operation_id=turn.native_operation_id,
                response_id=response,
                data=data,
            )
            for offset, kind, data in (
                (2, "response.started", {}),
                (5, "message", {"text": text, "update_type": "agent_message_chunk"}),
                (9, "response.completed", {}),
            )
        ]
    )
    lab.lab.run[0] = lab.lab.run[0].model_copy(
        update={
            "state": RunState.IDLE,
            "input_open": True,
            "active_response_id": None,
            "last_event_sequence": after + 9,
        }
    )
    return response


def test_saved_start_busy_view_keeps_original_intent_for_explicit_retry_only(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    lab.outcome[0] = StartStatus.BUSY
    try:
        body = StartTurnBody(
            input="original busy input", operation_id="busy:wire:original"
        )
        turn = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        original = lab.starts[0]
        counts = len(lab.starts), len(lab.inputs), len(lab.queries)
        view = lab.chat.read_turn(lab.identity, lab.thread.id, turn.id)
        assert view.status == "failed" and view.operation_state == "busy"
        assert view.operation_id == body.operation_id and view.items[0].data == {
            "text": body.input
        }
        assert view.error["retryable"] is True and view.error["code"] == "resource_busy"
        assert (
            view.recovery_mode is None
            and view.stream_available is view.interrupt_available is False
        )
        assert (
            view.started_at
            is view.completed_at
            is view.response_id
            is view.final_response
            is None
        )
        assert (len(lab.starts), len(lab.inputs), len(lab.queries)) == counts
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(
                lab.identity,
                lab.thread.id,
                body.model_copy(update={"input": "changed input"}),
            )
        assert failure.value.code == "operation_conflict"
        assert (len(lab.starts), len(lab.inputs), len(lab.queries)) == counts
        lab.outcome[0] = StartStatus.ACCEPTED
        retried = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert (
            retried.id == turn.id
            and retried.native_operation_id == turn.native_operation_id
        )
        assert retried.request_state == "accepted" and lab.starts == [
            original,
            original,
        ]
    finally:
        lab.product.close()


def test_saved_input_busy_view_retries_same_original_input_receipt_identity(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        first = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="first", operation_id="first"),
        )
        completed_response(lab, first)
        assert (
            lab.chat.read_turn(lab.identity, lab.thread.id, first.id).status
            == "completed"
        )
        accepted_input = lab.chat.execution.send_input

        def busy_input(request):
            assert lab.boundary.open_sessions == 0
            lab.inputs.append(request)
            receipt = InputReceipt(
                operation_id=request.operation_id,
                run_id=request.run_id,
                status=InputStatus.REJECTED,
                reason_code="resource_busy",
                evidence_sequence=None,
            )
            lab.input_receipts[request.operation_id] = receipt
            return receipt

        monkeypatch.setattr(lab.chat.execution, "send_input", busy_input)
        body = StartTurnBody(
            input="same continuation", operation_id="input:busy:original"
        )
        turn = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        original = lab.inputs[0]
        counts = len(lab.starts), len(lab.inputs), len(lab.queries)
        view = lab.chat.read_turn(lab.identity, lab.thread.id, turn.id)
        assert view.status == "failed" and view.operation_state == "busy"
        assert (
            view.operation_id == body.operation_id and view.error["retryable"] is True
        )
        assert (
            view.recovery_mode is None
            and view.stream_available is view.interrupt_available is False
        )
        assert (
            view.started_at
            is view.completed_at
            is view.response_id
            is view.final_response
            is None
        )
        assert (len(lab.starts), len(lab.inputs), len(lab.queries)) == counts
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(
                lab.identity,
                lab.thread.id,
                body.model_copy(update={"input": "different"}),
            )
        assert failure.value.code == "operation_conflict"
        assert (len(lab.starts), len(lab.inputs), len(lab.queries)) == counts
        monkeypatch.setattr(lab.chat.execution, "send_input", accepted_input)
        retried = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert (
            retried.id == turn.id
            and retried.native_operation_id == turn.native_operation_id
        )
        assert retried.request_state == "accepted" and lab.inputs == [
            original,
            original,
        ]
        assert len(lab.starts) == 1
    finally:
        lab.product.close()


@pytest.mark.parametrize("admitted", [InputStatus.ACCEPTED, InputStatus.DELIVERED])
def test_unknown_continuation_read_preserves_saved_receipt_and_exact_active_cache_guard(
    settings_values, tmp_path, monkeypatch, no_database_connections, admitted
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        first = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="first", operation_id="first"),
        )
        completed_response(lab, first)
        assert (
            lab.chat.read_turn(lab.identity, lab.thread.id, first.id).status
            == "completed"
        )

        def admitted_input(request):
            assert lab.boundary.open_sessions == 0
            lab.inputs.append(request)
            receipt = InputReceipt(
                operation_id=request.operation_id,
                run_id=request.run_id,
                status=admitted,
                reason_code=None,
                evidence_sequence=10,
            )
            lab.input_receipts[request.operation_id] = receipt
            return receipt

        monkeypatch.setattr(lab.chat.execution, "send_input", admitted_input)
        body = StartTurnBody(
            input="uncertain continuation", operation_id="input:unknown:original"
        )
        turn = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert turn.request_state == admitted.value
        response_id = uuid4()
        lab.events.extend(
            [
                RunEvent(
                    run_id=lab.lab.run[0].run_id,
                    sequence=sequence,
                    kind=kind,
                    occurred_at=lab.chat.clock(),
                    operation_id=turn.native_operation_id,
                    response_id=response_id,
                    data=data,
                )
                for sequence, kind, data in (
                    (10, "input.accepted", {}),
                    (11, "response.started", {}),
                    (12, "input.unknown", {"reason_code": "prompt_outcome_unknown"}),
                )
            ]
        )
        lab.input_receipts[turn.native_operation_id] = InputReceipt(
            operation_id=turn.native_operation_id,
            run_id=lab.lab.run[0].run_id,
            status=InputStatus.UNKNOWN,
            reason_code="prompt_outcome_unknown",
            evidence_sequence=12,
        )
        lab.lab.run[0] = lab.lab.run[0].model_copy(
            update={
                "state": RunState.UNKNOWN,
                "input_open": False,
                "active_response_id": None,
                "last_event_sequence": 12,
                "reason_code": "native_run_unknown",
            }
        )
        view = lab.chat.read_turn(lab.identity, lab.thread.id, turn.id)
        assert (
            view.status == "queued" and view.completed_at is view.final_response is None
        )
        assert (
            view.operation_id == body.operation_id
            and view.operation_state == admitted.value
        )
        assert view.response_id == response_id and view.run_id == lab.lab.run[0].run_id
        assert view.error["retryable"] is False and view.interrupt_available is False
        current = lab.chat.read_thread(lab.identity, lab.thread.id)
        saved = lab.chat.store.get_turn(current, turn.id)
        assert (
            current.active_turn_id == turn.id and saved.request_state == admitted.value
        )
        assert (
            saved.response_id == response_id and saved.cached_view["status"] == "queued"
        )
        counts = len(lab.starts), len(lab.inputs), len(lab.queries)
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(
                lab.identity,
                lab.thread.id,
                StartTurnBody(input="new", operation_id="new"),
            )
        assert failure.value.code == "thread_busy"
        assert (len(lab.starts), len(lab.inputs), len(lab.queries)) == counts
        original = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert original.id == turn.id and original.request_state == admitted.value
        assert len(lab.starts) == len(lab.inputs) == 1 and len(lab.queries) > counts[2]
        assert (
            lab.chat.read_thread(lab.identity, lab.thread.id).active_turn_id == turn.id
        )
    finally:
        lab.product.close()


def test_two_replies_use_one_run_and_original_grant_without_reassembling_transcript(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        first = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="first", operation_id="first:opaque"),
        )
        first_response = completed_response(lab, first)
        first_view = lab.chat.read_turn(lab.identity, lab.thread.id, first.id)
        assert (
            first_view.status == "completed"
            and first_view.response_id == first_response
        )
        assert first_view.final_response == "first reply"
        second = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="second", operation_id="second:opaque"),
        )
        request = lab.inputs[0]
        assert request.run_id == lab.lab.run[0].run_id
        assert request.grant_id == lab.starts[0].grant_id
        assert request.text == "second" and request.context == ()
        second_response = completed_response(lab, second, text="second reply")
        second_view = lab.chat.read_turn(lab.identity, lab.thread.id, second.id)
        assert (
            second_view.response_id == second_response
            and second_response != first_response
        )
        assert (
            second_view.final_response == "second reply"
            and len(lab.starts) == len(lab.inputs) == 1
        )
        assert (
            lab.chat.read_turn(lab.identity, lab.thread.id, first.id).final_response
            == "first reply"
        )
        lab.lab.run[0] = lab.lab.run[0].model_copy(
            update={"state": RunState.STOPPED, "input_open": False}
        )
        assert (
            lab.chat.read_turn(lab.identity, lab.thread.id, first.id).status
            == "completed"
        )
        assert all(call[3] == request.grant_id for call in lab.queries)
    finally:
        lab.product.close()


@pytest.mark.parametrize(
    "variant",
    [
        "revoked",
        "expired",
        "parent",
        "grant_workspace",
        "grant_owner",
        "grant_profile",
        "grant_start",
        "grant_run",
        "missing",
    ],
)
def test_history_uses_live_original_structural_read_before_native_or_receipt_io(
    settings_values, tmp_path, monkeypatch, no_database_connections, variant
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        turn = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="owned", operation_id="history"),
        )
        grant = lab.lab.store.rows[lab.starts[0].grant_id]
        if variant == "revoked":
            grant = replace(grant, revoked_at=lab.chat.clock())
        elif variant == "expired":
            grant = replace(grant, expires_at=lab.chat.clock())
        elif variant == "parent":
            grant = replace(grant, parent_session_id=uuid4())
        elif variant == "grant_workspace":
            grant = replace(grant, workspace_id=uuid4())
        elif variant == "grant_owner":
            grant = replace(grant, principal_id="foreign")
        elif variant == "grant_profile":
            grant = replace(
                grant, profile_ref=grant.profile_ref.model_copy(update={"revision": 17})
            )
        elif variant == "grant_start":
            grant = replace(grant, start_operation_id=uuid4())
        elif variant == "grant_run":
            grant = replace(grant, run_id=uuid4())
        lab.lab.store.rows[grant.grant_id] = grant
        if variant == "missing":
            del lab.lab.store.rows[grant.grant_id]
        lab.queries.clear()
        view = lab.chat.read_turn(lab.identity, lab.thread.id, turn.id)
        assert lab.queries == [] and len(lab.starts) == 1 and lab.inputs == []
        assert view.stream_available is view.interrupt_available is False
        assert len(view.items) == 1 and view.items[0].data == {"text": "owned"}
        assert view.error is not None and view.final_response is None
    finally:
        lab.product.close()


def test_foreign_turn_or_cookie_denies_before_any_original_lookup(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        turn = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="owned", operation_id="history"),
        )
        other = lab.chat.create_thread(
            lab.identity, CreateThreadBody(profile_ref=lab.thread.profile_ref)
        )
        lab.queries.clear()
        with pytest.raises(lab.error_type) as failure:
            lab.chat.read_turn(lab.identity, other.id, turn.id)
        assert failure.value.code == "turn_not_found"
        lab.chat.sessions.revoke(lab.identity.grant_id)
        with pytest.raises(lab.error_type) as failure:
            lab.chat.read_turn(lab.identity, lab.thread.id, turn.id)
        assert failure.value.code == "local_session_required" and lab.queries == []
    finally:
        lab.product.close()


def test_native_foreign_same_workspace_run_is_not_disclosed_or_rebound(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        turn = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="owned", operation_id="history"),
        )
        lab.lab.run[0] = lab.lab.run[0].model_copy(update={"run_id": uuid4()})
        lab.queries.clear()
        view = lab.chat.read_turn(lab.identity, lab.thread.id, turn.id)
        assert view.error["code"] == "original_run_mismatch" and len(view.items) == 1
        assert all(call[0] != "list_events" for call in lab.queries)
    finally:
        lab.product.close()


def test_native_bound_grant_before_lost_start_journal_can_query_only_exact_original(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    lab.lost[0] = True
    try:
        body = StartTurnBody(input="original", operation_id="bound:lost")
        turn = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        request = lab.starts[0]
        lab.lab.store.bind_grant(
            request.grant_id,
            run_id=lab.lab.run[0].run_id,
            bearer_digest="controlled-fixture-digest",
        )
        recovered = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        assert recovered.id == turn.id and recovered.request_state == "accepted"
        assert len(lab.starts) == 1 and lab.inputs == []
    finally:
        lab.product.close()


@pytest.mark.parametrize(
    "variant",
    ["not_found", "receipt_run", "profile", "effective_profile", "workspace", "policy"],
)
def test_unbound_journal_hint_never_substitutes_original_lookup_or_invalid_run(
    settings_values, tmp_path, monkeypatch, no_database_connections, variant
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    lab.lost[0] = True
    try:
        body = StartTurnBody(input="original", operation_id="hint:original")
        turn = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        request = lab.starts[0]
        run = lab.lab.run[0]
        lab.lab.store.bind_grant(
            request.grant_id,
            run_id=run.run_id,
            bearer_digest="controlled-fixture-digest",
        )
        if variant == "not_found":
            lab.receipts.clear()
        elif variant == "receipt_run":
            lab.receipts[turn.native_operation_id] = StartReceipt(
                operation_id=turn.native_operation_id,
                status=StartStatus.ACCEPTED,
                run_id=uuid4(),
                reason_code=None,
            )
        else:
            changes = {
                "profile": {
                    "profile_ref": run.profile_ref.model_copy(update={"revision": 19})
                },
                "effective_profile": {
                    "effective_config": run.effective_config.model_copy(
                        update={
                            "profile_ref": run.profile_ref.model_copy(
                                update={"revision": 19}
                            )
                        }
                    )
                },
                "workspace": {"workspace_id": uuid4()},
                "policy": {"policy": RunPolicy.BOUNDED},
            }
            lab.lab.run[0] = run.model_copy(update=changes[variant])
        lab.queries.clear()
        if variant == "not_found":
            pending = lab.chat.start_turn(lab.identity, lab.thread.id, body)
            assert pending.request_state == "unknown"
        else:
            with pytest.raises(lab.error_type) as failure:
                lab.chat.start_turn(lab.identity, lab.thread.id, body)
            assert failure.value.code == "original_run_mismatch"
        assert [call[0] for call in lab.queries] == (
            ["get_start"]
            if variant in ("not_found", "receipt_run")
            else ["get_start", "get_run"]
        )
        assert all(call[3] == request.grant_id for call in lab.queries)
        saved = lab.chat.read_thread(lab.identity, lab.thread.id)
        assert saved.run_id is None and saved.active_turn_id == turn.id
        assert len(lab.starts) == 1 and lab.inputs == []
    finally:
        lab.product.close()


def test_fresh_current_cookie_does_not_replace_revoked_original_parent_in_recovery(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    lab.lost[0] = True
    try:
        body = StartTurnBody(input="original", operation_id="revoked:original")
        lab.chat.start_turn(lab.identity, lab.thread.id, body)
        lab.chat.sessions.revoke(lab.identity.grant_id)
        current = lab.chat.sessions.exchange(lab.chat.sessions.create_bootstrap().token)
        identity = lab.chat.sessions.identity_for_grant(current.session_id)
        lab.queries.clear()
        with pytest.raises(AgentError):
            lab.chat.start_turn(identity, lab.thread.id, body)
        assert lab.queries == [] and len(lab.starts) == 1 and lab.inputs == []
    finally:
        lab.product.close()


def test_stale_attempt_cannot_release_newer_original_dispatch_guard(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        lab.outcome[0] = StartStatus.BUSY
        body = StartTurnBody(input="original", operation_id="stale:attempt")
        old = lab.chat.start_turn(lab.identity, lab.thread.id, body)
        current = lab.chat.read_thread(lab.identity, lab.thread.id)
        claimed_thread, claimed, winner = lab.chat.store.claim_turn(
            current,
            operation_id=old.operation_id,
            native_operation_id=old.native_operation_id,
            fingerprint=old.request_fingerprint,
            input_text=old.input_text,
            now=lab.chat.clock(),
        )
        assert winner
        dispatched = lab.chat.store.mark_dispatching(
            claimed_thread, claimed, now=lab.chat.clock()
        )
        assert dispatched.dispatch_attempt == old.dispatch_attempt + 1
        late = lab.chat.store.settle_turn(
            claimed_thread,
            old.id,
            state="busy",
            reason_code="resource_busy",
            now=lab.chat.clock(),
            expected=old,
            observation=True,
        )
        assert (
            late.request_state == "dispatching"
            and late.dispatch_attempt == dispatched.dispatch_attempt
        )
        assert (
            lab.chat.read_thread(lab.identity, lab.thread.id).active_turn_id == old.id
        )
        assert len(lab.starts) == 1
    finally:
        lab.product.close()


@pytest.mark.parametrize("mcp_status", list(SupportStatus))
def test_product_start_uses_actual_declared_opencode_capabilities_and_requires_mcp(
    settings_values, tmp_path, monkeypatch, no_database_connections, mcp_status
):
    from sidera_agents.harnesses.opencode import OpenCodeHarness
    from sidera_agents.runs import RunExecution

    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="original", operation_id="capability:original"),
        )
        request = lab.starts[0]
        capabilities = OpenCodeHarness._negotiated_capabilities(
            {"protocolVersion": 1, "agentCapabilities": {}}
        )
        assert "prompt" not in capabilities
        assert capabilities["model"].status is SupportStatus.UNCONFIRMED
        if mcp_status is SupportStatus.SUPPORTED:
            RunExecution._check_required_capabilities(request, capabilities)
        else:
            capabilities["mcp"] = CapabilitySupport(
                status=mcp_status, reason_code="controlled-mcp-denial", constraints={}
            )
            with pytest.raises(AgentError) as failure:
                RunExecution._check_required_capabilities(request, capabilities)
            assert (
                failure.value.code == "configuration_invalid"
                and failure.value.field == "mcp"
            )
        assert len(lab.starts) == 1 and lab.inputs == []
    finally:
        lab.product.close()


def test_delivered_original_input_cannot_regress_to_stale_accepted_observation(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    try:
        first = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="first", operation_id="first"),
        )
        completed_response(lab, first)
        lab.chat.read_turn(lab.identity, lab.thread.id, first.id)
        second = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="second", operation_id="second"),
        )
        current = lab.chat.read_thread(lab.identity, lab.thread.id)
        delivered = lab.chat.store.settle_turn(
            current,
            second.id,
            expected=second,
            observation=True,
            state="delivered",
            reason_code=None,
            run_id=current.run_id,
            now=lab.chat.clock(),
        )
        assert delivered.request_state == "delivered"
        late = lab.chat.store.settle_turn(
            current,
            second.id,
            expected=second,
            observation=True,
            state="accepted",
            reason_code=None,
            run_id=current.run_id,
            now=lab.chat.clock(),
        )
        assert late.request_state == "delivered" and late.version == delivered.version
        assert (
            lab.chat.read_thread(lab.identity, lab.thread.id).active_turn_id
            == second.id
        )
        assert len(lab.starts) == len(lab.inputs) == 1
    finally:
        lab.product.close()


def stopping_lab(settings_values, tmp_path, monkeypatch):
    lab = admission_lab(settings_values, tmp_path, monkeypatch)
    stops, reads = [], []
    state = [StopStatus.UNKNOWN]

    def stop(request):
        assert lab.boundary.open_sessions == 0
        saved = lab.chat.read_thread(lab.identity, lab.thread.id)
        assert (
            saved.stop_operation_id == request.operation_id
            and saved.stop_state == "dispatching"
        )
        stops.append(request)
        return StopReceipt(
            operation_id=request.operation_id,
            run_id=request.run_id,
            status=state[0],
            reason_code=None,
            evidence_sequence=10 if state[0] is StopStatus.STOPPED else None,
        )

    def get_stop(workspace, run, operation, *, grant_id):
        assert lab.boundary.open_sessions == 0
        reads.append((workspace, run, operation, grant_id))
        return StopReceipt(
            operation_id=operation,
            run_id=run,
            status=state[0],
            reason_code=None,
            evidence_sequence=10 if state[0] is StopStatus.STOPPED else None,
        )

    monkeypatch.setattr(lab.chat.execution, "stop", stop, raising=False)
    monkeypatch.setattr(lab.chat.execution, "get_stop", get_stop, raising=False)
    lab.stops, lab.stop_reads, lab.stop_state = stops, reads, state
    return lab


def test_conversation_close_persists_one_original_stop_unknown_then_query_only_and_preserves_completed_history(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    try:
        turn = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="hello", operation_id="close:original"),
        )
        completed_response(lab, turn)
        assert (
            lab.chat.read_turn(lab.identity, lab.thread.id, turn.id).status
            == "completed"
        )
        assert lab.chat.close(grace_seconds=0) is False
        request = lab.stops[0]
        assert (
            request.grant_id == lab.starts[0].grant_id
            and request.run_id == lab.lab.run[0].run_id
        )
        assert lab.chat.close(grace_seconds=0) is False and len(lab.stops) == 1
        assert lab.stop_reads[-1] == (
            lab.thread.workspace_id,
            request.run_id,
            request.operation_id,
            request.grant_id,
        )
        with pytest.raises(lab.error_type) as failure:
            lab.chat.start_turn(
                lab.identity,
                lab.thread.id,
                StartTurnBody(input="later", operation_id="later"),
            )
        assert (
            failure.value.code == "shutdown_pending"
            and len(lab.starts) == 1
            and lab.inputs == []
        )
        lab.stop_state[0] = StopStatus.STOPPED
        lab.lab.run[0] = lab.lab.run[0].model_copy(
            update={"state": RunState.STOPPED, "input_open": False}
        )
        assert (
            lab.chat.close(grace_seconds=0) is True
            and lab.chat.close(grace_seconds=0) is True
        )
        assert len(lab.stops) == 1
        assert (
            lab.chat.read_turn(lab.identity, lab.thread.id, turn.id).status
            == "completed"
        )
    finally:
        lab.product.close()


def test_close_during_held_original_start_retains_service_until_original_binding_can_stop(
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
    thread = threaded_turn(
        lab, StartTurnBody(input="hello", operation_id="held:close"), results, errors
    )
    try:
        assert entered.wait(5)
        assert lab.chat.close(grace_seconds=0) is False and lab.stops == []
        release.set()
        thread.join(5)
        assert (
            not thread.is_alive()
            and errors == []
            and results[0].request_state == "accepted"
        )
        lab.stop_state[0] = StopStatus.STOPPED
        assert (
            lab.chat.close(grace_seconds=0) is True
            and len(lab.stops) == len(lab.starts) == 1
        )
    finally:
        release.set()
        thread.join(5)
        lab.product.close()


def test_unresolved_original_start_not_found_keeps_close_incomplete_without_stop_or_resend(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    lab.lost[0] = True
    try:
        lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="original", operation_id="unknown:close"),
        )
        lab.receipts.clear()
        assert lab.chat.close(grace_seconds=0) is False
        assert lab.chat.close(grace_seconds=0) is False
        assert len(lab.starts) == 1 and lab.stops == lab.inputs == []
    finally:
        lab.product.close()


def test_revoked_original_grant_has_cleanup_only_stop_without_native_history_disclosure(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    try:
        turn = lab.chat.start_turn(
            lab.identity,
            lab.thread.id,
            StartTurnBody(input="owned", operation_id="revoked:close"),
        )
        original = lab.starts[0].grant_id
        lab.lab.store.revoke_grant(original, now=lab.chat.clock())
        lab.queries.clear()
        view = lab.chat.read_turn(lab.identity, lab.thread.id, turn.id)
        assert len(view.items) == 1 and lab.queries == []
        lab.stop_state[0] = StopStatus.STOPPED
        assert lab.chat.close(grace_seconds=0) is True
        assert len(lab.stops) == 1 and lab.stops[0].grant_id == original
        assert all(call[0] != "list_events" for call in lab.queries)
    finally:
        lab.product.close()


def test_cold_close_is_inert_and_fences_new_empty_threads(
    settings_values, tmp_path, monkeypatch, no_database_connections
):
    lab = chat_lab(settings_values, tmp_path, monkeypatch)
    try:
        assert lab.chat.close(grace_seconds=0) is True and lab.boundary.queries == []
        with pytest.raises(lab.error_type) as failure:
            lab.chat.create_thread(
                lab.identity, CreateThreadBody(profile_ref=lab.lab.profile.ref)
            )
        assert failure.value.code == "shutdown_pending" and lab.lab.calls == []
    finally:
        lab.product.close()


def test_public_composition_owns_one_inert_chat_service_and_shared_readers(
    settings_values, no_database_connections
):
    from sidera_product.chat.service import ChatService
    from sidera_product.composition import build_product_runtime
    from sidera_product.config import ProductSettings

    runtime = build_product_runtime(ProductSettings.model_validate(settings_values))
    try:
        assert isinstance(runtime.chat, ChatService)
        assert runtime.chat.store.engine is runtime.product_store.engine
        assert runtime.chat.receipt_store is runtime.authority_store
        assert runtime.chat.grants is runtime.grants
        assert runtime.chat.execution is runtime.agents.execution
        assert runtime.chat.catalog is runtime.agents.catalog
        assert runtime.chat.runtimes is runtime.agents.runtimes
        assert runtime.chat.sessions is runtime.local_sessions
        assert runtime.close(grace_seconds=0) is True
    finally:
        assert runtime.close(grace_seconds=0) is True


@pytest.mark.parametrize("activation", ["ready", "partial_start"])
def test_activated_chat_reader_failure_retains_shared_owners_until_same_close_recovers(
    request, monkeypatch, activation
):
    from sidera_product.postgres.store import ProductStoreError

    runtime, calls = request.getfixturevalue("controlled_owner_runtime")
    original_reader = runtime.chat.store.list_conversations
    reads = []

    def unavailable_reader(*, workspace_id, principal_id):
        assert workspace_id == runtime.settings.workspace_id
        assert principal_id == runtime.settings.local_principal_id
        reads.append((workspace_id, principal_id))
        raise ProductStoreError("product_read_failed")

    original_start = runtime.agents.start_background

    def partially_started(**kwargs):
        original_start(**kwargs)
        raise RuntimeError("controlled partial external start")

    if activation == "ready":
        runtime.start()
    else:
        monkeypatch.setattr(runtime.agents, "start_background", partially_started)
    monkeypatch.setattr(runtime.chat.store, "list_conversations", unavailable_reader)
    try:
        if activation == "partial_start":
            with pytest.raises(RuntimeError, match="product_startup_failed"):
                runtime.start()
        else:
            assert runtime.close(grace_seconds=0) is False
        assert reads == [
            (runtime.settings.workspace_id, runtime.settings.local_principal_id)
        ]
        assert runtime.status()["lifecycle"] == "cleanup_incomplete"
        assert runtime.status()["cleanup"]["code"] == "conversation_cleanup_pending"
        with pytest.raises(AgentError) as denied:
            runtime.require_write_admission()
        assert denied.value.code == "product_unavailable"
        starts = ["agents.start"]
        if activation == "ready":
            starts.append("cp.start")
        assert calls == starts + ["cp.close", "chat.close"]
    finally:
        monkeypatch.setattr(runtime.chat.store, "list_conversations", original_reader)
        assert runtime.close(grace_seconds=0) is True
    assert calls == starts + [
        "cp.close",
        "chat.close",
        "chat.close",
        "agents.close",
        "agents.store.close",
        "credentials.dispose",
        "product.store.close",
    ]
    with pytest.raises(RuntimeError, match="product_closed"):
        runtime.start()


def http_chat_lab(request, settings_values, tmp_path, monkeypatch):
    from sidera_product.app import create_app

    runtime, _ = request.getfixturevalue("controlled_owner_runtime")
    lab = stopping_lab(settings_values, tmp_path, monkeypatch)
    runtime.chat = lab.chat
    runtime.local_sessions = lab.chat.sessions
    runtime.request_authorizer.local_sessions = lab.chat.sessions
    runtime.start()
    lab.runtime, lab.app = runtime, create_app(runtime=runtime)
    lab.cookie = {
        "sidera_local_session": lab.lab.parent.token.get_secret_value(),
        "sidera_csrf": lab.lab.parent.csrf.get_secret_value(),
    }
    lab.headers = {
        "Origin": runtime.settings.web_origin,
        "X-Sidera-CSRF": lab.lab.parent.csrf.get_secret_value(),
    }
    return lab


@pytest.mark.anyio
async def test_native_chat_http_empty_thread_health_and_capabilities_use_real_owner_without_native_io(
    request, settings_values, tmp_path, monkeypatch, no_database_connections
):
    import httpx

    lab = http_chat_lab(request, settings_values, tmp_path, monkeypatch)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lab.app),
            base_url=lab.chat.settings.local_url,
            cookies=lab.cookie,
            headers=lab.headers,
        ) as client:
            health = await client.get("/main-agent/v1/health")
            assert (
                health.status_code == 200 and health.json()["provider"] == "available"
            )
            caps = (await client.get("/main-agent/v1/capabilities")).json()
            assert caps == {
                "thread_list": True,
                "thread_create": True,
                "thread_read": True,
                "thread_rename": False,
                "turn_stream": True,
                "turn_interrupt": False,
                "stream_resume_after_gateway_restart": True,
            }
            created = await client.post(
                "/main-agent/v1/threads",
                json={
                    "title": "Empty authorized chat",
                    "context_refs": ["task:opaque"],
                    "profileRef": lab.lab.profile.ref.model_dump(mode="json"),
                },
            )
            assert created.status_code == 201
            wire = created.json()
            assert wire["profileRef"] == lab.lab.profile.ref.model_dump(mode="json")
            assert wire["contextRefs"] == ["task:opaque"] and wire["turns"] == []
            assert wire["runId"] is None
            read = await client.get("/main-agent/v1/threads/" + wire["id"])
            assert read.status_code == 200 and read.json() == wire
            page = await client.get("/main-agent/v1/threads?limit=1")
            assert page.status_code == 200 and len(page.json()["data"]) == 1
            next_page = await client.get(
                "/main-agent/v1/threads", params={"cursor": page.json()["nextCursor"]}
            )
            assert (
                len(next_page.json()["data"]) == 1
                and next_page.json()["nextCursor"] is None
            )
        assert lab.starts == lab.inputs == lab.queries == []
    finally:
        assert lab.runtime.close(grace_seconds=0) is True
        lab.product.close()


@pytest.mark.anyio
async def test_native_chat_http_original_turn_and_native_sequence_replay_have_compatible_wire(
    request, settings_values, tmp_path, monkeypatch, no_database_connections
):
    import httpx

    lab = http_chat_lab(request, settings_values, tmp_path, monkeypatch)
    prefix = f"/main-agent/v1/threads/{lab.thread.id}"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lab.app),
            base_url=lab.chat.settings.local_url,
            cookies=lab.cookie,
            headers=lab.headers,
        ) as client:
            accepted = await client.post(
                prefix + "/turns",
                json={"input": "hello", "operationId": "fallback:wire"},
            )
            assert accepted.status_code == 202
            first = accepted.json()
            assert (
                first["operationId"] == "fallback:wire"
                and first["operationState"] == "accepted"
            )
            assert first["threadId"] == str(lab.thread.id) and first["runId"] == str(
                lab.lab.run[0].run_id
            )
            assert first["interruptAvailable"] is False
            turn = lab.chat.store.get_turn(
                lab.chat.read_thread(lab.identity, lab.thread.id), UUID(first["id"])
            )
            response_id = completed_response(lab, turn, text="safe original reply")
            path = prefix + "/turns/" + first["id"]
            completed = await client.get(path)
            view = completed.json()
            assert (
                view["status"] == "completed"
                and view["finalResponse"] == "safe original reply"
            )
            assert view["responseId"] == str(response_id) and view["usage"] is None
            replay = await client.get(
                path + "/events?after=2", headers={"Last-Event-ID": "5"}
            )
            assert replay.status_code == 200 and replay.headers[
                "content-type"
            ].startswith("text/event-stream")
            assert (
                "id: 9\n" in replay.text
                and "id: 2\n" not in replay.text
                and "id: 5\n" not in replay.text
            )
            assert "event: completed\n" in replay.text
            future = await client.get(path + "/events?after=999")
            assert (
                "event: resync_required\n" in future.text and "id:" not in future.text
            )
            interrupted = await client.post(path + "/interrupt")
            assert (
                interrupted.status_code == 409
                and interrupted.json()["error"]["code"] == "response_not_current"
            )
            assert (await client.get(prefix)).json()["turns"] == [view]
        assert len(lab.starts) == 1 and lab.inputs == []
    finally:
        lab.stop_state[0] = StopStatus.STOPPED
        assert lab.runtime.close(grace_seconds=0) is True
        lab.product.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "boundary", ["missing", "csrf", "origin", "host", "dual", "bearer", "revoked"]
)
async def test_native_chat_http_denials_keep_safe_envelope_and_no_native_disclosure(
    request, settings_values, tmp_path, monkeypatch, no_database_connections, boundary
):
    import httpx

    lab = http_chat_lab(request, settings_values, tmp_path, monkeypatch)
    cookies, headers = dict(lab.cookie), dict(lab.headers)
    codes = {
        "missing": "local_session_required",
        "csrf": "csrf_required",
        "origin": "origin_denied",
        "host": "host_denied",
        "dual": "ambiguous_authentication",
        "bearer": "local_session_required",
        "revoked": "local_session_required",
    }
    if boundary == "missing":
        cookies.clear()
    elif boundary == "csrf":
        headers.pop("X-Sidera-CSRF")
    elif boundary == "origin":
        headers["Origin"] = "http://attacker.example"
    elif boundary == "host":
        headers["Host"] = "attacker.example"
    elif boundary in ("dual", "bearer"):
        headers["Authorization"] = "Bearer N19_UNTRUSTED_SECRET_SENTINEL"
        if boundary == "bearer":
            cookies.clear()
    else:
        lab.chat.sessions.revoke(lab.identity.grant_id)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lab.app),
            base_url=lab.chat.settings.local_url,
            cookies=cookies,
            headers=headers,
        ) as client:
            reply = await client.post(
                f"/main-agent/v1/threads/{lab.thread.id}/turns",
                json={"input": "private"},
            )
            assert reply.status_code == (
                401 if boundary in ("missing", "bearer", "revoked") else 403
            )
            assert reply.json() == {
                "error": {
                    "code": codes[boundary],
                    "message": codes[boundary],
                    "retryable": False,
                }
            }
            assert "SECRET_SENTINEL" not in reply.text and "private" not in reply.text
            if boundary == "csrf":
                cp = await client.post("/api/v1/commands", json={})
                assert cp.json() == {"detail": {"code": "csrf_required"}}
        assert lab.starts == lab.inputs == lab.queries == []
    finally:
        assert lab.runtime.close(grace_seconds=0) is True
        lab.product.close()


@pytest.mark.anyio
async def test_native_chat_http_validation_never_echoes_request_or_crosses_native_boundary(
    request, settings_values, tmp_path, monkeypatch, no_database_connections
):
    import httpx

    lab = http_chat_lab(request, settings_values, tmp_path, monkeypatch)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lab.app),
            base_url=lab.chat.settings.local_url,
            cookies=lab.cookie,
            headers=lab.headers,
        ) as client:
            reply = await client.post(
                f"/main-agent/v1/threads/{lab.thread.id}/turns",
                json={"input": {"private": "N19_BODY_SECRET_SENTINEL"}},
            )
            assert (
                reply.status_code == 422
                and reply.json()["error"]["code"] == "validation_error"
            )
            assert "SECRET_SENTINEL" not in reply.text
        assert lab.starts == lab.inputs == lab.queries == []
    finally:
        assert lab.runtime.close(grace_seconds=0) is True
        lab.product.close()
