import json
from datetime import UTC, datetime
from hashlib import sha256
from threading import Event
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import func, select

from sidera_agents.bootstrap import create_agent_service
from sidera_agents.contracts import (
    ExecutionWorkspace,
    NativeEvent,
    NativeOutcome,
    NativeOutcomeStatus,
    RunState,
    ToolBinding,
    ToolConnection,
)
from sidera_agents.ports import DenyContextSourceReader, DenySecretReader
from sidera_agents.postgres.models import AgentRunRow, RunEventRow
from sidera_agents.postgres.store import PostgresAgentStore


def test_postgres_observations_persist_cursor_correlation_and_preclose_drain(
    owned_agents_pg, admission_lab_factory, observation_feed, monkeypatch
):
    owned_agents_pg.migrate()
    lab = admission_lab_factory(PostgresAgentStore(owned_agents_pg.engine))
    feed = observation_feed(lab)
    entered, release = Event(), Event()
    original_prompt = lab.native.prompt
    request = lab.request()
    start = lab.execution.start(request)
    response_id = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    ).active_response_id

    def prompt(handle, assembled, current_id):
        feed.emit(
            handle.run_id, cursor=40, native_id="same-native-id", text="live PG message"
        )
        feed.emit(
            handle.run_id, cursor=41, native_id="same-native-id", text="live PG message"
        )
        entered.set()
        assert release.wait(timeout=4)
        feed.emit(
            handle.run_id, cursor=42, native_id="final-id", text="final PG message"
        )
        return original_prompt(handle, assembled, current_id)

    monkeypatch.setattr(lab.native, "prompt", prompt)
    reader_store = PostgresAgentStore(owned_agents_pg.engine)
    reader = create_agent_service(
        reader_store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        lab.service.start_background(interval_seconds=0.001)
        assert entered.wait(timeout=1)
        # A manual tick uses the same live-ingestion path without waiting for prompt.
        lab.pump()
        first = reader.observations.read_trace(
            lab.workspace_id, start.run_id, 0, 500, grant_id=lab.grant_id
        )
        assert [e.data["text"] for e in first.events if e.kind == "message"] == [
            "live PG message"
        ]
        assert (
            reader.execution.get_run(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            ).state
            is RunState.RUNNING
        )
        release.set()
        lab.service.close(timeout=3)
        final = reader.observations.read_trace(
            lab.workspace_id, start.run_id, 0, 500, grant_id=lab.grant_id
        )
        assert [e.data["text"] for e in final.events if e.kind == "message"] == [
            "live PG message",
            "final PG message",
        ]
        assert all(
            e.response_id == response_id and e.operation_id == request.operation_id
            for e in final.events
            if e.kind == "message"
        )
        assert [e.sequence for e in final.events] == list(
            range(1, final.next_sequence + 1)
        )
        assert (
            reader.observations.read_trace(
                lab.workspace_id,
                start.run_id,
                final.next_sequence,
                grant_id=lab.grant_id,
            ).events
            == ()
        )
        with owned_agents_pg.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(RunEventRow.__table__)
                    .where(RunEventRow.run_id == start.run_id)
                    .order_by(RunEventRow.sequence)
                )
                .mappings()
                .all()
            )
            snapshot = connection.execute(
                select(AgentRunRow.snapshot).where(AgentRunRow.run_id == start.run_id)
            ).scalar_one()
        assert len(rows) == final.next_sequence == snapshot["last_event_sequence"]
        assert [r["native_cursor"] for r in rows if r["kind"] == "message"] == [41, 42]
        assert all(
            r["occurred_at"].astimezone(UTC) == datetime(2026, 10, 3, tzinfo=UTC)
            for r in rows
            if r["kind"] == "message"
        )
        assert (
            "native_cursor" not in final.model_dump_json()
            and "_native" not in final.model_dump_json()
        )
    finally:
        release.set()
        lab.service.close(timeout=3)
        reader.close()


def test_postgres_never_persists_resolved_secret_or_untrusted_usage(
    owned_agents_pg, admission_lab_factory, observation_feed, monkeypatch
):
    owned_agents_pg.migrate()
    lab = admission_lab_factory(PostgresAgentStore(owned_agents_pg.engine))
    feed = observation_feed(lab)
    secret = "N08_PG_SECRET_SENTINEL_NEVER_PERSIST"
    connection_id = uuid4()

    class Connections:
        def resolve(self, grant_id, requested_id):
            assert grant_id == lab.grant_id and requested_id == connection_id
            return ToolConnection(
                name="fixture",
                transport="http",
                endpoint_or_argv="https://fixture.invalid/mcp",
                headers={"X-Token": SecretStr(secret)},
                environment={"TOKEN": SecretStr(secret)},
            )

    lab.execution._tool_connections = Connections()
    start = lab.execution.start(
        lab.request(tools=(ToolBinding(connection_id=connection_id),))
    )
    original_prompt = lab.native.prompt

    def prompt(handle, assembled, response_id):
        feed.emit(handle.run_id, cursor=1, text="ordinary PG text " + secret)
        feed.emit(
            handle.run_id,
            cursor=2,
            kind="tool",
            data={"tool_name": "search", "text": secret, "auth": {"value": secret}},
        )
        feed.emit(
            handle.run_id, cursor=3, kind="stderr", data={"text": secret + "x" * 9000}
        )
        original_prompt(handle, assembled, response_id)
        return NativeOutcome(
            response_id=response_id,
            status=NativeOutcomeStatus.COMPLETED,
            reason_code=secret,
            observed_model="provider/" + secret,
            usage={"input_tokens": secret, "auth": {"token": secret}},
        )

    monkeypatch.setattr(lab.native, "prompt", prompt)
    lab.pump()
    assert lab.service.observations is not None, "Observation service is not wired."
    page = lab.service.observations.read_trace(
        lab.workspace_id, start.run_id, 0, grant_id=lab.grant_id
    )
    with owned_agents_pg.engine.connect() as connection:
        rows = (
            connection.execute(
                select(RunEventRow.__table__).where(RunEventRow.run_id == start.run_id)
            )
            .mappings()
            .all()
        )
        run_row = (
            connection.execute(
                select(AgentRunRow.__table__).where(AgentRunRow.run_id == start.run_id)
            )
            .mappings()
            .one()
        )
    assert secret not in page.model_dump_json()
    assert secret not in json.dumps([dict(row) for row in rows], default=str)
    assert secret not in json.dumps(dict(run_row), default=str)
    assert run_row["snapshot"]["effective_config"]["observed_model"] is None
    usage = next(e for e in page.events if e.kind == "usage")
    assert usage.data["usage"] is None
    assert (
        next(e for e in page.events if e.kind == "message").data["text"]
        == "ordinary PG text [REDACTED]"
    )
    assert (
        len(next(e for e in page.events if e.kind == "diagnostic").data["text"]) <= 4096
    )


def test_postgres_failed_transaction_retains_consumed_safe_page_for_next_tick(
    owned_agents_pg, admission_lab_factory, monkeypatch
):
    owned_agents_pg.migrate()
    lab = admission_lab_factory(PostgresAgentStore(owned_agents_pg.engine))
    request = lab.request(policy="conversation")
    start = lab.execution.start(request)
    lab.pump()
    response_id = lab.native.prompt_calls[0][2]
    queue = [
        NativeEvent(
            cursor=1,
            native_id="one-id",
            kind="message",
            response_id=response_id,
            operation_id=None,
            occurred_at=datetime(2026, 10, 3, tzinfo=UTC),
            data={
                "text": "transaction retry evidence",
                "cookie": "FAKE_COOKIE_MUST_BE_DROPPED",
            },
        )
    ]

    def consume(handle, after_cursor, limit=100):
        page = tuple(queue[:limit])
        del queue[:limit]
        return page

    monkeypatch.setattr(lab.native, "read_events", consume)
    original_write = lab.store._write_run
    failed = False

    def fail_after_insert(connection, row):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("one-shot rollback after event insertion")
        return original_write(connection, row)

    monkeypatch.setattr(lab.store, "_write_run", fail_after_insert)
    before = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    ).last_event_sequence
    lab.pump()
    with owned_agents_pg.engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(RunEventRow)
                .where(
                    RunEventRow.run_id == start.run_id, RunEventRow.kind == "message"
                )
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                select(AgentRunRow.snapshot).where(AgentRunRow.run_id == start.run_id)
            ).scalar_one()["last_event_sequence"]
            == before
        )
    assert queue == []
    lab.pump()
    lab.pump()
    reader = PostgresAgentStore(owned_agents_pg.engine)
    rows = reader.get_lifecycle_events(lab.workspace_id, start.run_id)
    messages = [e for e in rows if e.kind == "message"]
    assert [e.data["text"] for e in messages] == ["transaction retry evidence"]
    assert (
        messages[0].operation_id == request.operation_id
        and messages[0].response_id == response_id
    )
    assert (
        reader.get_run_snapshot(lab.workspace_id, start.run_id).last_event_sequence
        == before + 1
    )
    assert len(lab.native.prompt_calls) == 1
    assert "FAKE_COOKIE_MUST_BE_DROPPED" not in repr(rows)


def test_postgres_denied_artifact_authority_persists_only_safe_omission(
    owned_agents_pg,
    admission_lab_factory,
    observation_feed,
    artifact_authority,
    artifact_io,
    tmp_path,
):
    owned_agents_pg.migrate()
    lab = admission_lab_factory(PostgresAgentStore(owned_agents_pg.engine))
    feed = observation_feed(lab)
    root = tmp_path / "denied-pg-artifacts"
    root.mkdir()
    artifact = root / "private.txt"
    secret = "N08_PG_DENIED_ARTIFACT_CONTENT_SENTINEL"
    artifact.write_text(secret, encoding="utf-8")
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
            "content_hash": "sha256:" + sha256(secret.encode()).hexdigest(),
            "grant_id": str(lab.grant_id),
            "stderr": "FAKE_RAW_DRIVER_TEXT_MUST_NOT_PERSIST",
        },
    )
    opens = artifact_io()
    lab.pump()
    assert opens == [], (
        "PostgreSQL-backed verification opened an unauthorized artifact."
    )
    assert authority.calls == [(lab.grant_id, "artifact.read", workspace)]
    assert (
        lab.service.observations.list_outputs(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        )
        == ()
    )
    page = lab.service.observations.read_trace(
        lab.workspace_id, start.run_id, 0, grant_id=lab.grant_id
    )
    omitted = next(e for e in page.events if e.kind == "artifact")
    assert omitted.data == {"reason_code": "artifact_read_denied"}
    assert (
        omitted.response_id == response_id
        and omitted.operation_id == request.operation_id
    )
    with owned_agents_pg.engine.connect() as connection:
        rows = (
            connection.execute(
                select(RunEventRow.__table__).where(RunEventRow.run_id == start.run_id)
            )
            .mappings()
            .all()
        )
        snapshot = connection.execute(
            select(AgentRunRow.snapshot).where(AgentRunRow.run_id == start.run_id)
        ).scalar_one()
    serialized = json.dumps([dict(row) for row in rows], default=str)
    for value in (
        secret,
        artifact.as_uri(),
        str(lab.grant_id),
        "FAKE_RAW_DRIVER_TEXT_MUST_NOT_PERSIST",
        "FAKE_ARTIFACT_AUTHORITY_PRIVATE_REASON",
    ):
        assert (
            value not in serialized
            and value not in page.model_dump_json()
            and value not in json.dumps(snapshot)
        )
    artifact_rows = [row for row in rows if row["kind"] == "artifact"]
    assert len(artifact_rows) == 1
    assert artifact_rows[0]["data"]["reason_code"] == "artifact_read_denied"
    assert "output" not in artifact_rows[0]["data"]
    assert len(lab.native.prompt_calls) == 1 and snapshot["state"] == "completed"
