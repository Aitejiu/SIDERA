from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Event
from uuid import uuid4

import pytest

from sidera_agents.bootstrap import create_agent_service
from sidera_agents.contracts import (
    ExecutionWorkspace,
    InputStatus,
    NativeOutcomeStatus,
    RunInput,
    RunState,
    RunStop,
)
from sidera_agents.errors import AgentError
from sidera_agents.ports import DenyContextSourceReader, DenySecretReader


def _trace(lab, run_id, after=0, limit=100):
    assert lab.service.observations is not None, "Observation service is not wired."
    return lab.service.observations.read_trace(
        lab.workspace_id, run_id, after, limit, grant_id=lab.grant_id
    )


def test_final_messages_survive_bounded_native_close(admission_lab, observation_feed):
    lab = admission_lab
    feed = observation_feed(lab)
    request = lab.request()
    start = lab.execution.start(request)
    response_id = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    ).active_response_id
    for cursor in range(1, 206):
        feed.emit(start.run_id, cursor=cursor, text=f"chunk {cursor}")
    lab.pump()
    page = _trace(lab, start.run_id, limit=500)
    messages = [event for event in page.events if event.kind == "message"]
    assert [event.data["text"] for event in messages] == [
        f"chunk {cursor}" for cursor in range(1, 206)
    ]
    assert all(event.response_id == response_id for event in messages)
    assert all(event.operation_id == request.operation_id for event in messages)
    assert start.run_id in feed.closed
    snapshot = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    assert snapshot.state is RunState.COMPLETED
    assert page.next_sequence == snapshot.last_event_sequence
    assert page.events[-1].kind == "run.closed"
    assert page.events[-1].data["status"] == "completed"


def test_live_message_is_readable_during_unrelated_slow_admission(
    admission_lab_factory, observation_feed, monkeypatch
):
    from sidera_agents.contracts import ContextItem, SourceContent, SourceRef
    from sidera_agents.memory import MemoryAgentStore

    lab = admission_lab_factory(
        MemoryAgentStore(), runtime_max_active=2, profile_max_active=2
    )
    feed = observation_feed(lab)
    entered = Event()
    release = Event()
    source_entered = Event()
    source_release = Event()
    ingested = Event()
    original_prompt = lab.native.prompt
    original_append = lab.store._append_event

    def gated_prompt(handle, assembled, response_id):
        entered.set()
        assert release.wait(timeout=4)
        return original_prompt(handle, assembled, response_id)

    def observe_append(*args, **kwargs):
        event = original_append(*args, **kwargs)
        if event.kind == "message":
            ingested.set()
        return event

    text = "slow authorized source"
    digest = "sha256:" + sha256(text.encode()).hexdigest()
    ref = SourceRef(provider="fixture", key="slow", version="1")

    class SlowSource:
        def read(self, grant_id, source_ref):
            assert grant_id == lab.grant_id and source_ref == ref
            source_entered.set()
            assert source_release.wait(timeout=4)
            return SourceContent(text=text, actual_version="1", content_hash=digest)

    monkeypatch.setattr(lab.native, "prompt", gated_prompt)
    monkeypatch.setattr(lab.store, "_append_event", observe_append)
    lab.execution._sources = SlowSource()
    start = lab.execution.start(lab.request())
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            lab.service.start_background(interval_seconds=0.001)
            assert entered.wait(timeout=1)
            # Hold admission before the next owner tick can ingest this message.
            admitted = pool.submit(
                lab.execution.start,
                lab.request(
                    context=(
                        ContextItem(
                            item_id="slow",
                            origin="fixture",
                            ordinal=0,
                            required=True,
                            version="1",
                            content_hash=digest,
                            source_ref=ref,
                            text=None,
                        ),
                    )
                ),
            )
            assert source_entered.wait(timeout=1)
            feed.emit(start.run_id, cursor=1, text="visible before response returns")
            assert ingested.wait(timeout=1), "Live ingestion stopped behind admission."
            page = _trace(lab, start.run_id)
            assert [e.data["text"] for e in page.events if e.kind == "message"] == [
                "visible before response returns"
            ]
            assert not release.is_set()
            assert (
                lab.execution.get_run(
                    lab.workspace_id, start.run_id, grant_id=lab.grant_id
                ).state
                is RunState.RUNNING
            )
        finally:
            source_release.set()
            release.set()
            lab.service.close(timeout=3)
        assert admitted.result(timeout=1).run_id is not None


def test_native_identity_dedup_and_pages_survive_fresh_service(
    admission_lab, observation_feed
):
    lab = admission_lab
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request(policy="conversation"))
    feed.emit(start.run_id, cursor=10, native_id="n1", text="one")
    feed.emit(start.run_id, cursor=11, native_id="n1", text="one")
    feed.emit(start.run_id, cursor=12, native_id="n2", text="two")
    lab.pump()
    page = _trace(lab, start.run_id, limit=500)
    assert [e.data["text"] for e in page.events if e.kind == "message"] == [
        "one",
        "two",
    ]
    assert [e.sequence for e in page.events] == list(range(1, page.next_sequence + 1))
    lab.pump()
    assert _trace(lab, start.run_id) == page
    reader = create_agent_service(
        lab.store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        cursor = 0
        collected = []
        while True:
            current = reader.observations.read_trace(
                lab.workspace_id, start.run_id, cursor, 2, grant_id=lab.grant_id
            )
            collected.extend(current.events)
            cursor = current.next_sequence
            if not current.has_more:
                break
        assert tuple(collected) == page.events
        assert (
            reader.observations.read_trace(
                lab.workspace_id, start.run_id, cursor, grant_id=lab.grant_id
            ).events
            == ()
        )
        assert (
            lab.execution.list_events(
                lab.workspace_id, start.run_id, 0, 500, grant_id=lab.grant_id
            )
            == page
        )
        assert len(lab.native.prompt_calls) == 1
    finally:
        reader.close()


def test_small_memory_trace_page_copies_only_the_bounded_lookahead(
    admission_lab, observation_feed, monkeypatch
):
    from sidera_agents.contracts import RunEvent

    lab = admission_lab
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request(policy="conversation"))
    for cursor in range(1, 206):
        feed.emit(start.run_id, cursor=cursor, text=f"long trace chunk {cursor}")
    lab.pump()
    head = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    ).last_event_sequence
    copied = []
    original_dump = RunEvent.model_dump

    def dump(event, *args, **kwargs):
        copied.append(event.sequence)
        return original_dump(event, *args, **kwargs)

    monkeypatch.setattr(RunEvent, "model_dump", dump)
    page = _trace(lab, start.run_id, limit=1)
    assert copied == [1, 2], "A small page cloned the entire remaining Trace."
    assert len(page.events) == 1 and page.next_sequence == 1 and page.has_more
    copied.clear()
    tail = _trace(lab, start.run_id, after=head - 1, limit=1)
    assert copied == [head]
    assert tail.next_sequence == head and not tail.has_more
    copied.clear()
    empty = _trace(lab, start.run_id, after=head, limit=1)
    assert copied == [] and empty.events == ()
    assert empty.next_sequence == head and not empty.has_more


def test_queued_turns_keep_their_own_correlation(
    admission_lab, observation_feed, monkeypatch
):
    lab = admission_lab
    feed = observation_feed(lab)
    request = lab.request(policy="conversation")
    start = lab.execution.start(request)
    initial_id = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    ).active_response_id
    entered = Event()
    release = Event()
    original_prompt = lab.native.prompt
    turns = []

    def prompt(handle, assembled, response_id):
        turns.append(response_id)
        feed.emit(
            handle.run_id,
            cursor=len(turns),
            native_id="request-local-id",
            text=assembled.text,
        )
        if len(turns) == 1:
            entered.set()
            assert release.wait(timeout=4)
        return original_prompt(handle, assembled, response_id)

    monkeypatch.setattr(lab.native, "prompt", prompt)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pumping = pool.submit(lab.pump)
            assert entered.wait(timeout=1)
            inputs = tuple(
                RunInput(
                    workspace_id=lab.workspace_id,
                    run_id=start.run_id,
                    operation_id=uuid4(),
                    grant_id=lab.grant_id,
                    text=f"turn {index}",
                    context=(),
                )
                for index in (2, 3)
            )
            for item in inputs:
                assert lab.execution.send_input(item).status is InputStatus.ACCEPTED
            release.set()
            pumping.result(timeout=3)
        finally:
            release.set()
    page = _trace(lab, start.run_id)
    messages = [e for e in page.events if e.kind == "message"]
    assert [e.data["text"] for e in messages] == [request.text, "turn 2", "turn 3"]
    assert [e.response_id for e in messages] == turns
    assert turns[0] == initial_id and len(set(turns)) == 3
    assert [e.operation_id for e in messages] == [
        request.operation_id,
        *(i.operation_id for i in inputs),
    ]
    assert all(e.data["update_type"] == "agent_message_chunk" for e in messages)
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.IDLE
    )
    assert not any(e.kind == "run.closed" for e in page.events)


@pytest.mark.parametrize("reader", ("run", "events", "trace", "outputs"))
def test_observation_authorization_precedes_metadata_and_pagination(
    admission_lab, reader
):
    lab = admission_lab
    start = lab.execution.start(lab.request())
    assert lab.service.observations is not None, "Observation service is not wired."

    def read(workspace_id, run_id, grant_id):
        if reader == "run":
            return lab.execution.get_run(workspace_id, run_id, grant_id=grant_id)
        if reader == "outputs":
            return lab.service.observations.list_outputs(
                workspace_id, run_id, grant_id=grant_id
            )
        method = (
            lab.execution.list_events
            if reader == "events"
            else lab.service.observations.read_trace
        )
        return method(workspace_id, run_id, -1, 0, grant_id=grant_id)

    for run_id in (start.run_id, uuid4()):
        with pytest.raises(AgentError) as error:
            read(lab.workspace_id, run_id, uuid4())
        assert error.value.code == "permission_denied"
    other_workspace, other_grant = uuid4(), uuid4()
    lab.authorizer.grant_workspace(other_workspace, other_grant)
    for run_id in (start.run_id, uuid4()):
        with pytest.raises(AgentError) as error:
            read(other_workspace, run_id, other_grant)
        assert error.value.code == "not_found"


@pytest.mark.parametrize(
    "cursor,limit", ((-1, 1), (True, 1), (999, 1), (0, 0), (0, 501), (0, True))
)
def test_invalid_public_page_is_rejected_after_run_authorization(
    admission_lab, cursor, limit
):
    lab = admission_lab
    start = lab.execution.start(lab.request())
    with pytest.raises(AgentError) as error:
        _trace(lab, start.run_id, cursor, limit)
    assert error.value.code == "configuration_invalid"


def test_outputs_require_owned_artifact_and_verified_hash(
    admission_lab, observation_feed, artifact_authority, artifact_io, tmp_path
):
    lab = admission_lab
    feed = observation_feed(lab)
    root = tmp_path / "owned-artifacts"
    root.mkdir()
    artifact = root / "result.txt"
    artifact.write_text("verified evidence", encoding="utf-8")
    digest = "sha256:" + sha256(b"verified evidence").hexdigest()
    workspace = ExecutionWorkspace(
        directory=str(tmp_path), writable=True, artifact_directory=str(root)
    )
    authority = artifact_authority(lab, workspace)
    request = lab.request(workspace=workspace)
    start = lab.execution.start(request)
    opens = artifact_io()
    feed.emit(
        start.run_id,
        cursor=1,
        kind="artifact",
        data={
            "ref_id": "result",
            "media_type": "text/plain",
            "uri": artifact.as_uri(),
            "content_hash": digest,
            "workspace": {
                "directory": "/FAKE_REPLACEMENT",
                "artifact_directory": "/FAKE_OTHER_ROOT",
            },
        },
    )
    lab.pump()
    _trace(lab, start.run_id)
    outputs = lab.service.observations.list_outputs(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    assert len(outputs) == 1
    assert outputs[0].ref_id == "result" and outputs[0].content_hash == digest
    assert outputs[0].uri == artifact.as_uri()
    assert authority.calls == [(lab.grant_id, "artifact.read", request.workspace)]
    assert opens
    opens.clear()
    authority.enabled = False
    assert (
        lab.service.observations.list_outputs(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        )
        == outputs
    )
    assert opens == [] and len(authority.calls) == 1


def test_denied_artifact_authority_prevents_local_io_and_optional_evidence(
    admission_lab, observation_feed, artifact_authority, artifact_io, tmp_path
):
    lab = admission_lab
    feed = observation_feed(lab)
    root = tmp_path / "denied-artifacts"
    root.mkdir()
    artifact = root / "private.txt"
    artifact.write_text("owned but not authorized", encoding="utf-8")
    workspace = ExecutionWorkspace(
        directory=str(tmp_path), writable=True, artifact_directory=str(root)
    )
    authority = artifact_authority(lab, None)
    request = lab.request(workspace=workspace)
    start = lab.execution.start(request)
    response_id = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    ).active_response_id
    feed.emit(
        start.run_id,
        cursor=1,
        kind="artifact",
        data={
            "ref_id": "denied",
            "media_type": "text/plain",
            "uri": artifact.as_uri(),
            "content_hash": "sha256:" + sha256(b"owned but not authorized").hexdigest(),
        },
    )
    opens = artifact_io()
    lab.pump()
    assert opens == [], "Artifact verification performed unauthorized local I/O."
    assert (
        lab.service.observations.list_outputs(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        )
        == ()
    )
    event = next(e for e in _trace(lab, start.run_id).events if e.kind == "artifact")
    assert event.data == {"reason_code": "artifact_read_denied"}
    assert (
        event.response_id == response_id and event.operation_id == request.operation_id
    )
    assert artifact.as_uri() not in _trace(lab, start.run_id).model_dump_json()
    assert authority.calls == [(lab.grant_id, "artifact.read", workspace)]
    assert len(lab.native.prompt_calls) == 1
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.COMPLETED
    )


def test_artifact_authority_is_rechecked_after_revocation_without_prompt_replay(
    admission_lab, observation_feed, artifact_authority, artifact_io, tmp_path
):
    lab = admission_lab
    feed = observation_feed(lab)
    root = tmp_path / "leased-artifacts"
    root.mkdir()
    artifact = root / "result.txt"
    artifact.write_text("leased evidence", encoding="utf-8")
    workspace = ExecutionWorkspace(
        directory=str(tmp_path), writable=True, artifact_directory=str(root)
    )
    authority = artifact_authority(lab, workspace)
    request = lab.request(policy="conversation", workspace=workspace)
    start = lab.execution.start(request)
    data = {
        "ref_id": "first",
        "media_type": "text/plain",
        "uri": artifact.as_uri(),
        "content_hash": "sha256:" + sha256(b"leased evidence").hexdigest(),
    }
    feed.emit(start.run_id, cursor=1, kind="artifact", data=data)
    lab.pump()
    assert (
        len(
            lab.service.observations.list_outputs(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            )
        )
        == 1
    )
    response_id = lab.native.prompt_calls[0][2]
    authority.enabled = False
    feed.emit(
        start.run_id,
        cursor=2,
        native_id="later-evidence",
        kind="artifact",
        response_id=response_id,
        data={**data, "ref_id": "after-revocation"},
    )
    opens = artifact_io()
    lab.pump()
    assert opens == [], "Revoked artifact authority still opened the file."
    outputs = lab.service.observations.list_outputs(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    assert [output.ref_id for output in outputs] == ["first"]
    events = [e for e in _trace(lab, start.run_id).events if e.kind == "artifact"]
    assert events[-1].data == {"reason_code": "artifact_read_denied"}
    assert (
        events[-1].response_id == response_id
        and events[-1].operation_id == request.operation_id
    )
    assert authority.calls == [(lab.grant_id, "artifact.read", workspace)] * 2
    assert len(lab.native.prompt_calls) == 1
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.IDLE
    )


@pytest.mark.parametrize("different", ("root", "directory"))
def test_artifact_authority_cannot_cross_the_granted_workspace_or_root(
    admission_lab,
    observation_feed,
    artifact_authority,
    artifact_io,
    tmp_path,
    different,
):
    lab = admission_lab
    feed = observation_feed(lab)
    root = tmp_path / "requested-root"
    root.mkdir()
    artifact = root / "result.txt"
    artifact.write_text("contained but unauthorized", encoding="utf-8")
    workspace = ExecutionWorkspace(
        directory=str(tmp_path), writable=True, artifact_directory=str(root)
    )
    approved = workspace.model_copy(
        update={
            "artifact_directory" if different == "root" else "directory": str(
                tmp_path / "different-authorized-location"
            )
        }
    )
    authority = artifact_authority(lab, approved)
    request = lab.request(workspace=workspace)
    start = lab.execution.start(request)
    feed.emit(
        start.run_id,
        cursor=1,
        kind="artifact",
        data={
            "ref_id": "denied",
            "media_type": "text/plain",
            "uri": artifact.as_uri(),
            "content_hash": "sha256:"
            + sha256(b"contained but unauthorized").hexdigest(),
        },
    )
    opens = artifact_io()
    lab.pump()
    assert opens == []
    assert (
        lab.service.observations.list_outputs(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        )
        == ()
    )
    assert authority.calls == [(lab.grant_id, "artifact.read", workspace)]
    assert len(lab.native.prompt_calls) == 1
    event = next(e for e in _trace(lab, start.run_id).events if e.kind == "artifact")
    assert event.operation_id == request.operation_id
    assert event.response_id == lab.native.prompt_calls[0][2]


@pytest.mark.parametrize(
    "invalid", ("outside", "escape", "symlink", "credentials", "hash")
)
def test_untrusted_artifact_uri_cannot_authorize_local_read(
    admission_lab, observation_feed, artifact_authority, artifact_io, tmp_path, invalid
):
    lab = admission_lab
    feed = observation_feed(lab)
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private outside artifact scope", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    inside = root / "valid.txt"
    inside.write_text("owned", encoding="utf-8")
    uri = {
        "outside": outside.as_uri(),
        "escape": (root / ".." / "private.txt").as_uri(),
        "symlink": (root / "link.txt").as_uri(),
        "credentials": "file://token@localhost/private.txt",
        "hash": inside.as_uri(),
    }[invalid]
    workspace = ExecutionWorkspace(
        directory=str(tmp_path), writable=True, artifact_directory=str(root)
    )
    authority = artifact_authority(lab, workspace)
    start = lab.execution.start(lab.request(workspace=workspace))
    feed.emit(
        start.run_id,
        cursor=1,
        kind="artifact",
        data={
            "ref_id": "bad",
            "media_type": "text/plain",
            "uri": uri,
            "content_hash": "sha256:"
            + sha256(b"private outside artifact scope").hexdigest(),
        },
    )
    opens = artifact_io()
    lab.pump()
    _trace(lab, start.run_id)
    assert (
        lab.service.observations.list_outputs(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        )
        == ()
    )
    omitted = [e for e in _trace(lab, start.run_id).events if e.kind == "artifact"]
    assert omitted[0].data == {"reason_code": "output_ref_unverified"}
    assert authority.calls == [(lab.grant_id, "artifact.read", workspace)]
    if invalid in {"outside", "escape", "credentials"}:
        assert opens == []


def test_missing_artifact_directory_does_not_authorize_or_open_local_files(
    admission_lab, observation_feed, artifact_authority, artifact_io, tmp_path
):
    lab = admission_lab
    feed = observation_feed(lab)
    authority = artifact_authority(lab, None)
    start = lab.execution.start(lab.request())
    artifact = tmp_path / "not-an-artifact.txt"
    artifact.write_text("not delegated", encoding="utf-8")
    feed.emit(
        start.run_id,
        cursor=1,
        kind="artifact",
        data={
            "ref_id": "outside",
            "media_type": "text/plain",
            "uri": artifact.as_uri(),
            "content_hash": "sha256:" + sha256(b"not delegated").hexdigest(),
        },
    )
    opens = artifact_io()
    lab.pump()
    assert opens == [] and authority.calls == []
    assert (
        lab.service.observations.list_outputs(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        )
        == ()
    )


def test_stop_drains_available_evidence_before_removing_session(
    admission_lab, observation_feed, monkeypatch
):
    lab = admission_lab
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request(policy="conversation"))
    lab.pump()
    # Late, explicitly correlated native evidence still belongs to the completed turn.
    response_id = lab.native.prompt_calls[0][2]
    feed.emit(
        start.run_id, cursor=1, text="last message before Stop", response_id=response_id
    )
    lab.execution.stop(
        RunStop(
            workspace_id=lab.workspace_id,
            run_id=start.run_id,
            operation_id=uuid4(),
            grant_id=lab.grant_id,
        )
    )
    messages = [e for e in _trace(lab, start.run_id).events if e.kind == "message"]
    assert [e.data["text"] for e in messages] == ["last message before Stop"]
    assert messages[0].response_id == response_id
    assert start.run_id in feed.closed


@pytest.mark.parametrize(
    "status", (NativeOutcomeStatus.FAILED, NativeOutcomeStatus.UNKNOWN, "exception")
)
def test_failed_response_keeps_final_safe_evidence(
    admission_lab, observation_feed, monkeypatch, status
):
    lab = admission_lab
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request())

    def prompt(handle, assembled, response_id):
        feed.emit(handle.run_id, cursor=1, text="safe failure evidence")
        if status == "exception":
            raise RuntimeError("untrusted raw diagnostics")
        return lab.native.finish_prompt(handle, response_id, status)

    monkeypatch.setattr(lab.native, "prompt", prompt)
    lab.pump()
    assert [
        e.data["text"] for e in _trace(lab, start.run_id).events if e.kind == "message"
    ] == ["safe failure evidence"]


def test_consumed_safe_page_retries_store_failure_without_prompt_replay(
    admission_lab, monkeypatch
):
    from datetime import UTC, datetime

    from sidera_agents.contracts import NativeEvent

    lab = admission_lab
    start = lab.execution.start(lab.request(policy="conversation"))
    # A completed turn is still a retained native session, with one late correlated update.
    lab.pump()
    response_id = lab.native.prompt_calls[0][2]
    queue = [
        NativeEvent(
            cursor=1,
            native_id="retained-message",
            kind="message",
            response_id=response_id,
            operation_id=None,
            occurred_at=datetime(2026, 10, 3, tzinfo=UTC),
            data={
                "text": "original consumed notification",
                "auth": {"secret": "FAKE_DROP_THIS"},
            },
        )
    ]
    reads = []

    def consume(handle, after_cursor, limit=100):
        reads.append(after_cursor)
        page = tuple(queue[:limit])
        del queue[:limit]
        return page

    monkeypatch.setattr(lab.native, "read_events", consume)
    assert hasattr(lab.store, "append_observation"), (
        "Durable observation ingestion is absent."
    )
    original_append = lab.store.append_observation
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        record = args[2]
        assert "FAKE_DROP_THIS" not in repr(record)
        if not failed:
            failed = True
            raise RuntimeError("one-shot transaction failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(lab.store, "append_observation", fail_once)
    lab.pump()
    assert not any(e.kind == "message" for e in _trace(lab, start.run_id).events)
    assert queue == []
    ingestor = lab.execution._observations[start.run_id]
    assert len(ingestor._pending) == 1
    assert (
        lab.store.get_native_event_cursor(
            lab.workspace_id, start.run_id, ingestor.source
        )
        == 0
    )
    lab.pump()
    lab.pump()
    messages = [e for e in _trace(lab, start.run_id).events if e.kind == "message"]
    assert [e.data["text"] for e in messages] == ["original consumed notification"]
    assert messages[0].response_id == response_id
    assert len(lab.native.prompt_calls) == 1
    assert reads[0] == 0 and reads[1] == 1
    assert (
        lab.store.get_native_event_cursor(
            lab.workspace_id, start.run_id, ingestor.source
        )
        == 1
    )


def test_failed_final_drain_defers_close_until_all_notifications_commit(
    admission_lab, observation_feed, monkeypatch
):
    lab = admission_lab
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request())
    original_append = lab.store.append_observation
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed and args[2].kind == "message":
            failed = True
            raise RuntimeError("final page write failed")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(lab.store, "append_observation", fail_once)
    for cursor in range(1, 206):
        feed.emit(start.run_id, cursor=cursor, text=f"final chunk {cursor}")
    lab.pump()
    assert start.run_id not in feed.closed, "Close discarded unread native pages."
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.CLOSING
    )
    lab.pump()
    messages = [
        e for e in _trace(lab, start.run_id, limit=500).events if e.kind == "message"
    ]
    assert [e.data["text"] for e in messages] == [
        f"final chunk {cursor}" for cursor in range(1, 206)
    ]
    assert start.run_id in feed.closed
    assert len(lab.native.prompt_calls) == 1


def test_stop_with_failed_drain_keeps_accepted_cleanup_until_next_tick(
    admission_lab, observation_feed, monkeypatch
):
    lab = admission_lab
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request(policy="conversation"))
    lab.pump()
    response_id = lab.native.prompt_calls[0][2]
    feed.emit(
        start.run_id, cursor=1, text="Stop retry evidence", response_id=response_id
    )
    original_append = lab.store.append_observation
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("Stop drain write failed")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(lab.store, "append_observation", fail_once)
    stop = RunStop(
        workspace_id=lab.workspace_id,
        run_id=start.run_id,
        operation_id=uuid4(),
        grant_id=lab.grant_id,
    )
    lab.execution.stop(stop)
    assert (
        lab.execution.get_stop(
            lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
        ).status.value
        == "accepted"
    )
    assert start.run_id not in feed.closed
    lab.pump()
    assert (
        lab.execution.get_stop(
            lab.workspace_id, start.run_id, stop.operation_id, grant_id=lab.grant_id
        ).status.value
        == "stopped"
    )
    assert [
        e.data["text"] for e in _trace(lab, start.run_id).events if e.kind == "message"
    ] == ["Stop retry evidence"]


def test_response_completion_does_not_claim_unknown_native_close_is_terminal(
    admission_lab, observation_feed, monkeypatch
):
    from sidera_agents.contracts import NativeCloseOutcome, NativeCloseState

    lab = admission_lab
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request())
    feed.emit(start.run_id, cursor=1, text="completed response evidence")
    monkeypatch.setattr(
        lab.native,
        "close",
        lambda handle: NativeCloseOutcome(
            status=NativeCloseState.UNKNOWN, reason_code="native_close_unknown"
        ),
    )
    lab.pump()
    assert (
        lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).state
        is RunState.CLOSING
    )
    page = _trace(lab, start.run_id)
    assert any(e.kind == "response.completed" for e in page.events)
    assert not any(e.kind == "run.closed" for e in page.events)
