from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier, Lock, local
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sidera_agents.contracts import ToolConnection
from sidera_agents.errors import AgentError


def _postgres_lab(owned_agents_pg, admission_lab_factory, **options):
    from sidera_agents.postgres.store import PostgresAgentStore

    owned_agents_pg.migrate()
    store = PostgresAgentStore(owned_agents_pg.engine)
    return admission_lab_factory(store, **options)


class _ToggleSourceReader:
    def __init__(self, content):
        self.content = content
        self.available = True
        self.calls = []

    def read(self, grant_id, ref):
        self.calls.append((grant_id, ref))
        if not self.available:
            raise RuntimeError("synthetic source unavailable")
        return self.content


class _CountingSecretReader:
    def __init__(self):
        self.calls = []

    def read(self, grant_id, binding):
        self.calls.append((grant_id, binding))
        return SecretStr("SYNTHETIC_SECRET_VALUE")


class _CountingToolConnectionReader:
    def __init__(self):
        self.calls = []

    def resolve(self, grant_id, connection_id):
        self.calls.append((grant_id, connection_id))
        return ToolConnection(
            name="synthetic tool",
            transport="stdio",
            endpoint_or_argv=("/tmp/synthetic-tool",),
            headers={},
            environment={},
        )


@pytest.mark.parametrize(
    ("reason_path", "expected_state", "expected_reason"),
    (
        ("open_failure", "failed", "native_start_failed"),
        ("response_failure", "failed", "native_response_failed"),
        ("response_unknown", "unknown", "native_response_unknown"),
        ("close_unknown", "closing", "native_close_unknown"),
        ("inspection_gone", "unknown", "native_process_gone"),
    ),
)
def test_postgres_native_reason_codes_do_not_persist_resolved_secret(
    owned_agents_pg,
    admission_lab_factory,
    reason_path,
    expected_state,
    expected_reason,
):
    from sqlalchemy import select

    from sidera_agents.contracts import (
        CredentialRef,
        NativeCloseOutcome,
        NativeCloseState,
        NativeInspection,
        NativeOutcomeStatus,
        NativeProcessState,
        NativeResponseState,
        SecretBinding,
    )
    from sidera_agents.postgres.models import AgentRunRow, RunOperationRow
    from sidera_agents.runs import VerifiedNativeOpenFailure

    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)
    profile = lab.catalog.revise_profile(
        lab.profile.ref,
        lab.profile.draft.model_copy(
            update={
                "secret_refs": (
                    SecretBinding(
                        credential_ref=CredentialRef(
                            workspace_id=lab.workspace_id,
                            credential_id=uuid4(),
                        ),
                        destination="SYNTHETIC_TOKEN",
                    ),
                )
            }
        ),
        grant_id=lab.grant_id,
    )
    marker = "SYNTHETIC_SECRET_MARKER_DO_NOT_PERSIST"
    secret_reads = []

    class MarkerSecretReader:
        def read(self, grant_id, binding):
            del grant_id
            secret_reads.append(binding.destination)
            return SecretStr(marker)

    lab.execution._secrets = MarkerSecretReader()
    accepted = lab.execution.start(lab.request(profile_ref=profile.ref))

    if reason_path == "open_failure":

        def fail_open(open_request):
            assert (
                open_request.environment["SYNTHETIC_TOKEN"].get_secret_value() == marker
            )
            raise VerifiedNativeOpenFailure(marker)

        lab.native.open = fail_open
    else:
        original_prompt = lab.native.prompt

        def prompt_with_marker(handle, assembled_input, response_id):
            outcome = original_prompt(handle, assembled_input, response_id)
            if reason_path == "response_failure":
                return outcome.model_copy(
                    update={
                        "status": NativeOutcomeStatus.FAILED,
                        "reason_code": marker,
                    }
                )
            if reason_path == "response_unknown":
                return outcome.model_copy(
                    update={
                        "status": NativeOutcomeStatus.UNKNOWN,
                        "reason_code": marker,
                    }
                )
            if reason_path == "close_unknown":
                return outcome.model_copy(
                    update={
                        "status": NativeOutcomeStatus.FAILED,
                        "reason_code": "native_response_failed",
                    }
                )
            return outcome.model_copy(
                update={
                    "status": NativeOutcomeStatus.UNKNOWN,
                    "reason_code": "native_response_unknown",
                }
            )

        lab.native.prompt = prompt_with_marker

        if reason_path == "close_unknown":

            def close_with_marker(handle):
                lab.native.inspections[handle.run_id] = NativeInspection(
                    process_state=NativeProcessState.ALIVE,
                    response_state=NativeResponseState.ACTIVE,
                    reason_code=None,
                )
                return NativeCloseOutcome(
                    status=NativeCloseState.UNKNOWN,
                    reason_code=marker,
                )

            lab.native.close = close_with_marker
        elif reason_path == "inspection_gone":
            lab.native.inspect = lambda handle: NativeInspection(
                process_state=NativeProcessState.GONE,
                response_state=NativeResponseState.IDLE,
                reason_code=marker,
            )

    lab.pump()
    snapshot = lab.execution.get_run(
        lab.workspace_id, accepted.run_id, grant_id=lab.grant_id
    )
    receipt = lab.execution.get_start(
        lab.workspace_id, accepted.operation_id, grant_id=lab.grant_id
    ).receipt
    assert snapshot.state.value == expected_state
    assert snapshot.reason_code == expected_reason
    assert receipt.reason_code == (
        expected_reason if reason_path == "open_failure" else None
    )
    assert secret_reads == ["SYNTHETIC_TOKEN"]
    if reason_path != "open_failure":
        assert (
            lab.native.open_calls[0].environment["SYNTHETIC_TOKEN"].get_secret_value()
            == marker
        )
    assert marker not in repr(snapshot)
    assert marker not in repr(receipt)

    run_table = AgentRunRow.__table__
    operation_table = RunOperationRow.__table__
    with owned_agents_pg.engine.connect() as connection:
        run_row = (
            connection.execute(
                select(run_table).where(run_table.c.run_id == accepted.run_id)
            )
            .mappings()
            .one()
        )
        operation_row = (
            connection.execute(
                select(operation_table).where(
                    operation_table.c.workspace_id == lab.workspace_id,
                    operation_table.c.operation_id == accepted.operation_id,
                )
            )
            .mappings()
            .one()
        )
    persisted = repr(dict(run_row)) + repr(dict(operation_row))
    assert marker not in persisted


def test_postgres_accepted_replay_survives_profile_disable_and_source_loss(
    owned_agents_pg, admission_lab_factory
):
    from sqlalchemy import select

    from sidera_agents.bootstrap import create_agent_service
    from sidera_agents.contracts import (
        ContextItem,
        CredentialRef,
        SecretBinding,
        SourceContent,
        SourceRef,
        ToolBinding,
    )
    from sidera_agents.postgres.models import AgentRunRow, RunOperationRow
    from sidera_agents.postgres.store import PostgresAgentStore

    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=lab.workspace_id, credential_id=uuid4()
        ),
        destination="SYNTHETIC_TOKEN",
    )
    profile = lab.catalog.revise_profile(
        lab.profile.ref,
        lab.profile.draft.model_copy(update={"secret_refs": (binding,)}),
        grant_id=lab.grant_id,
    )
    source_text = "SYNTHETIC_SOURCE_TEXT"
    source_ref = SourceRef(provider="fixture", key="notes/one", version="v1")
    source_content = SourceContent(
        text=source_text,
        actual_version="v1",
        content_hash="sha256:" + sha256(source_text.encode("utf-8")).hexdigest(),
    )
    sources = _ToggleSourceReader(source_content)
    secrets = _CountingSecretReader()
    tools = _CountingToolConnectionReader()
    lab.execution._sources = sources
    lab.execution._secrets = secrets
    lab.execution._tool_connections = tools
    prompt_marker = "SYNTHETIC_PROMPT_MARKER"
    request = lab.request(
        profile_ref=profile.ref,
        text=prompt_marker,
        context=(
            ContextItem(
                item_id="note-1",
                origin="project",
                required=True,
                ordinal=0,
                version="v1",
                content_hash=source_content.content_hash,
                source_ref=source_ref,
            ),
        ),
        tools=(ToolBinding(connection_id=uuid4()),),
    )

    accepted = lab.execution.start(request)
    assert accepted.status.value == "accepted"
    lab.pump()
    assert len(lab.native.open_calls) == 1
    assert len(sources.calls) == 1
    assert len(secrets.calls) == 1
    assert len(tools.calls) == 1

    sources.available = False
    profile_status = lab.store.get_profile_status(
        lab.workspace_id, profile.ref.profile_id
    )
    lab.catalog.set_profile_enabled(
        lab.workspace_id,
        profile.ref.profile_id,
        False,
        profile_status.version,
        grant_id=lab.grant_id,
    )
    lab.service.close()

    fresh_store = PostgresAgentStore(owned_agents_pg.engine)
    fresh_service = create_agent_service(
        fresh_store,
        lab.registry,
        lab.authorizer,
        sources,
        secrets,
        lab.clock,
        tool_connections=tools,
    )
    try:
        fresh_service.poll_once()
        replayed = fresh_service.execution.start(request)
        fresh_service.poll_once()

        assert replayed == accepted
        assert len(lab.native.open_calls) == 1
        assert len(sources.calls) == 1
        assert len(secrets.calls) == 1
        assert len(tools.calls) == 1
        record = fresh_store.get_start_operation(lab.workspace_id, request.operation_id)
        assert record is not None
        assert record.profile_ref == profile.ref
        assert record.profile_config_hash == profile.config_hash
        assert record.receipt == accepted

        operation_table = RunOperationRow.__table__
        run_table = AgentRunRow.__table__
        with owned_agents_pg.engine.connect() as connection:
            operation_row = (
                connection.execute(
                    select(operation_table).where(
                        operation_table.c.workspace_id == lab.workspace_id,
                        operation_table.c.operation_id == request.operation_id,
                    )
                )
                .mappings()
                .one()
            )
            run_row = (
                connection.execute(
                    select(run_table).where(
                        run_table.c.workspace_id == lab.workspace_id,
                        run_table.c.operation_id == request.operation_id,
                    )
                )
                .mappings()
                .one()
            )
        persisted = repr(dict(operation_row)) + repr(dict(run_row))
        assert prompt_marker not in persisted
        assert source_text not in persisted
        assert "SYNTHETIC_SECRET_VALUE" not in persisted
        assert operation_row["profile_id"] == profile.ref.profile_id
        assert operation_row["profile_revision"] == profile.ref.revision
        assert operation_row["profile_config_hash"] == profile.config_hash
        assert operation_row["fingerprint"].startswith("sha256:")
        assert len(operation_row["fingerprint"]) == 71
    finally:
        fresh_service.close()


def test_postgres_changed_payload_conflicts_after_profile_disable(
    owned_agents_pg, admission_lab_factory
):
    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)
    request = lab.request()
    accepted = lab.execution.start(request)
    lab.pump()
    profile_status = lab.store.get_profile_status(
        lab.workspace_id, lab.profile.ref.profile_id
    )
    lab.catalog.set_profile_enabled(
        lab.workspace_id,
        lab.profile.ref.profile_id,
        False,
        profile_status.version,
        grant_id=lab.grant_id,
    )
    altered = request.model_copy(update={"text": "changed synthetic payload"})

    with pytest.raises(AgentError) as caught:
        lab.execution.start(altered)

    assert caught.value.code == "operation_conflict"
    lab.pump()
    assert accepted.status.value == "accepted"
    assert len(lab.native.open_calls) == 1
    lab.service.close()


def test_postgres_profile_mismatch_requires_stored_target_authority(
    owned_agents_pg, admission_lab_factory
):
    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)
    stored_profile = lab.profile
    accepted = lab.execution.start(lab.request(profile_ref=stored_profile.ref))
    lab.pump()
    submitted_profile = lab.catalog.revise_profile(
        stored_profile.ref,
        stored_profile.draft.model_copy(
            update={"instructions": "A revised synthetic instruction."}
        ),
        grant_id=lab.grant_id,
    )

    class StoredProfileDenyingAuthorizer:
        def __init__(self):
            self.targets = []

        def require(self, grant_id, action, target):
            del grant_id
            self.targets.append((action, target))
            if action == "run.start" and target == stored_profile.ref:
                raise AgentError("permission_denied", "Access denied.")

    authorizer = StoredProfileDenyingAuthorizer()
    lab.execution._authorizer = authorizer
    request = lab.request(
        operation_id=accepted.operation_id,
        profile_ref=submitted_profile.ref,
    )

    with pytest.raises(AgentError) as caught:
        lab.execution.start(request)

    assert caught.value.code == "permission_denied"
    assert ("run.start", submitted_profile.ref) in authorizer.targets
    assert ("run.start", stored_profile.ref) in authorizer.targets
    assert str(accepted.run_id) not in repr(caught.value)
    lab.pump()
    assert len(lab.native.open_calls) == 1
    lab.service.close()


def test_postgres_last_slot_is_atomic_across_independent_sessions(
    owned_agents_pg, admission_lab_factory
):
    from sqlalchemy import text

    lab = _postgres_lab(
        owned_agents_pg,
        admission_lab_factory,
        runtime_max_active=1,
        runtime_max_sessions=2,
        profile_max_active=2,
    )
    admission_barrier = Barrier(2)
    pid_lock = Lock()
    backend_pids = []
    thread_state = local()
    assert_owner = lab.store._assert_owner

    def wait_in_transaction(connection, owner_id):
        assert_owner(connection, owner_id)
        if getattr(thread_state, "waited", False):
            return
        thread_state.waited = True
        backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        with pid_lock:
            backend_pids.append(backend_pid)
        admission_barrier.wait(timeout=10)

    lab.store._assert_owner = wait_in_transaction
    requests = (lab.request(), lab.request())

    def start(request):
        try:
            return lab.execution.start(request)
        except AgentError as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = tuple(workers.submit(start, request) for request in requests)
            outcomes = tuple(future.result(timeout=20) for future in futures)
    finally:
        lab.store._assert_owner = assert_owner
        lab.service.close()

    statuses = [
        outcome.status.value
        for outcome in outcomes
        if not isinstance(outcome, AgentError)
    ]
    assert statuses.count("accepted") == 1
    assert statuses.count("busy") == 1
    assert not any(isinstance(outcome, AgentError) for outcome in outcomes)
    assert len(backend_pids) == 2
    assert len(set(backend_pids)) == 2
    assert lab.native.open_calls == []
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert installation.active_count == 1
    assert installation.session_count == 1


def test_postgres_profile_head_limit_applies_across_revisions(
    owned_agents_pg, admission_lab_factory
):
    lab = _postgres_lab(
        owned_agents_pg,
        admission_lab_factory,
        runtime_max_active=2,
        runtime_max_sessions=3,
        profile_max_active=2,
    )
    old_revision = lab.profile
    first = lab.execution.start(lab.request(profile_ref=old_revision.ref))
    lab.catalog.revise_profile(
        old_revision.ref,
        old_revision.draft.model_copy(update={"max_active": 1}),
        grant_id=lab.grant_id,
    )
    retry_request = lab.request(profile_ref=old_revision.ref)

    busy = lab.execution.start(retry_request)
    assert first.status.value == "accepted"
    assert busy.status.value == "busy"
    lab.pump()
    retried = lab.execution.start(retry_request)

    assert retried.status.value == "accepted"
    lab.service.close()


def test_postgres_canonical_installation_capacity_crosses_workspace_refs(
    owned_agents_pg, admission_lab_factory
):
    from sidera_agents.contracts import ProfileDraft

    lab = _postgres_lab(
        owned_agents_pg,
        admission_lab_factory,
        runtime_max_active=1,
        runtime_max_sessions=3,
        profile_max_active=2,
    )
    workspace_id, grant_id = uuid4(), uuid4()
    lab.authorizer.grant_workspace(workspace_id, grant_id)
    other_runtime = lab.runtimes.register(
        workspace_id,
        lab.native.harness_id,
        str(lab.executable),
        max_active=1,
        max_sessions=3,
        grant_id=grant_id,
    )
    other_profile = lab.catalog.create_profile(
        workspace_id,
        ProfileDraft(
            name="other workspace",
            description="Canonical installation sharing test",
            runtime_ref=other_runtime.ref,
            instructions="Use only synthetic test inputs.",
            resource_refs=(),
            model_id=None,
            thinking=None,
            max_active=2,
            environment={},
            secret_refs=(),
            native_settings={},
        ),
        grant_id=grant_id,
    )
    first = lab.execution.start(lab.request())
    busy = lab.execution.start(
        lab.request(profile_ref=other_profile.ref, grant_id=grant_id)
    )

    assert first.status.value == "accepted"
    assert busy.status.value == "busy"
    assert (
        lab.store.get_runtime_entry(other_runtime.ref).installation.installation_key
        == lab.store.get_runtime_entry(lab.runtime.ref).record.installation_key
    )
    assert lab.store.get_runtime_entry(lab.runtime.ref).installation.active_count == 1
    lab.service.close()


def test_postgres_payload_conflict_is_durable_and_never_opens_twice(
    owned_agents_pg, admission_lab_factory
):
    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)
    request = lab.request()
    accepted = lab.execution.start(request)
    altered = request.model_copy(update={"text": "changed payload"})

    with pytest.raises(AgentError) as caught:
        lab.execution.start(altered)
    assert caught.value.code == "operation_conflict"
    assert lab.store.get_start_receipt(
        lab.workspace_id, request.operation_id
    ).run_id == (accepted.run_id)
    lab.pump()
    assert len(lab.native.open_calls) == 1
    lab.service.close()


def test_postgres_conversation_idle_separates_active_and_session_capacity(
    owned_agents_pg, admission_lab_factory
):
    lab = _postgres_lab(
        owned_agents_pg,
        admission_lab_factory,
        runtime_max_active=2,
        runtime_max_sessions=1,
        profile_max_active=2,
    )
    first = lab.execution.start(lab.request(policy="conversation"))
    lab.pump()
    idle = lab.execution.get_run(lab.workspace_id, first.run_id, grant_id=lab.grant_id)
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation

    assert idle.state.value == "idle"
    assert installation.active_count == 0
    assert installation.session_count == 1

    second = lab.execution.start(lab.request())
    assert second.status.value == "busy"
    assert second.run_id is None
    assert len(lab.native.open_calls) == 1
    lab.service.close()


def test_postgres_fresh_store_recovers_unknown_operation_without_reopening(
    owned_agents_pg, admission_lab_factory
):
    from sidera_agents.bootstrap import create_agent_service, open_postgres_store
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader

    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)
    attempted = []

    def uncertain_open(open_request):
        attempted.append(open_request.run_id)
        raise RuntimeError("native acknowledgement lost")

    lab.native.open = uncertain_open
    request = lab.request()
    accepted = lab.execution.start(request)
    lab.pump()
    lab.service.close()

    fresh_store = open_postgres_store(
        owned_agents_pg.url.render_as_string(hide_password=False)
    )
    fresh = create_agent_service(
        fresh_store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        assert fresh.readiness() is True
        fresh.poll_once()
        lookup = fresh.execution.get_start(
            lab.workspace_id, request.operation_id, grant_id=lab.grant_id
        )
        repeated = fresh.execution.start(request)
        fresh.poll_once()
        assert lookup.receipt.status.value == "unknown"
        assert repeated.run_id == accepted.run_id
        assert len(attempted) == 1
        counts = fresh_store.get_runtime_entry(lab.runtime.ref).installation
        assert counts.active_count == 1
        assert counts.session_count == 1
    finally:
        fresh.close()
        fresh_store.close()


@pytest.mark.parametrize(
    ("process_state", "response_state", "expected_active", "expected_sessions"),
    (("gone", "idle", 0, 0), ("alive", "active", 1, 1)),
)
def test_postgres_fresh_store_reconciles_running_handle_without_replay(
    owned_agents_pg,
    admission_lab_factory,
    process_state,
    response_state,
    expected_active,
    expected_sessions,
):
    from threading import Event, Thread

    from sqlalchemy import select

    from sidera_agents.bootstrap import create_agent_service
    from sidera_agents.contracts import (
        NativeInspection,
        NativeProcessState,
        NativeResponseState,
    )
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader
    from sidera_agents.postgres.models import AgentRunRow
    from sidera_agents.postgres.store import PostgresAgentStore

    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)
    prompt_entered = Event()
    resume_prompt = Event()
    prompt_attempts = []
    poll_errors = []
    original_prompt = lab.native.prompt

    def blocked_prompt(handle, assembled_input, response_id):
        prompt_attempts.append(handle.run_id)
        prompt_entered.set()
        assert resume_prompt.wait(timeout=2)
        return original_prompt(handle, assembled_input, response_id)

    lab.native.prompt = blocked_prompt
    accepted = lab.execution.start(lab.request())

    def original_poll():
        try:
            lab.service.poll_once()
        except AgentError as error:
            poll_errors.append(error)

    old_poll = Thread(target=original_poll)
    old_poll.start()
    assert prompt_entered.wait(timeout=1)

    with owned_agents_pg.engine.connect() as connection:
        persisted = (
            connection.execute(
                select(AgentRunRow.__table__).where(
                    AgentRunRow.run_id == accepted.run_id
                )
            )
            .mappings()
            .one()
        )
    assert persisted["state"] == "running"
    assert persisted["native_handle"] is not None

    lab.store.release_execution_owner(lab.service._owner_id)
    inspected_handles = []
    evidence = NativeInspection(
        process_state=NativeProcessState(process_state),
        response_state=NativeResponseState(response_state),
        reason_code=None,
    )

    def inspect(handle):
        inspected_handles.append(handle)
        return evidence

    lab.native.inspect = inspect
    fresh_store = PostgresAgentStore(owned_agents_pg.engine)
    fresh = create_agent_service(
        fresh_store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        fresh.poll_once()
        snapshot = fresh.execution.get_run(
            lab.workspace_id, accepted.run_id, grant_id=lab.grant_id
        )
        installation = fresh_store.get_runtime_entry(lab.runtime.ref).installation
        assert snapshot.state.value == "unknown"
        assert installation.active_count == expected_active
        assert installation.session_count == expected_sessions
        assert len(inspected_handles) == 1
        assert inspected_handles[0].run_id == accepted.run_id
        assert len(lab.native.open_calls) == 1
        assert prompt_attempts == [accepted.run_id]
    finally:
        resume_prompt.set()
        old_poll.join(timeout=1)
        fresh.close()
        fresh_store.close()
        lab.service.close()

    assert not old_poll.is_alive()
    assert poll_errors and poll_errors[0].code == "execution_owner_unavailable"


def test_postgres_terminal_failure_reason_survives_uncertain_close(
    owned_agents_pg, admission_lab_factory
):
    from sidera_agents.bootstrap import create_agent_service
    from sidera_agents.contracts import (
        NativeCloseOutcome,
        NativeCloseState,
        NativeInspection,
        NativeOutcomeStatus,
        NativeProcessState,
        NativeResponseState,
    )
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader

    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)
    receipt = lab.execution.start(lab.request())
    lab.native.outcomes[receipt.run_id] = NativeOutcomeStatus.FAILED
    original_prompt = lab.native.prompt

    def fail_response(handle, assembled_input, response_id):
        return original_prompt(handle, assembled_input, response_id).model_copy(
            update={"reason_code": "native_response_failed"}
        )

    lab.native.prompt = fail_response
    lab.native.close = lambda handle: NativeCloseOutcome(
        status=NativeCloseState.UNKNOWN,
        reason_code="native_close_unknown",
    )
    lab.pump()

    closing = lab.execution.get_run(
        lab.workspace_id, receipt.run_id, grant_id=lab.grant_id
    )
    assert closing.state.value == "closing"
    assert closing.reason_code == "native_close_unknown"
    lab.service.close()

    lab.native.inspections[receipt.run_id] = NativeInspection(
        process_state=NativeProcessState.GONE,
        response_state=NativeResponseState.IDLE,
        reason_code=None,
    )
    fresh = create_agent_service(
        lab.store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        fresh.poll_once()
        failed = fresh.execution.get_run(
            lab.workspace_id, receipt.run_id, grant_id=lab.grant_id
        )
    finally:
        fresh.close()

    assert failed.state.value == "failed"
    assert failed.reason_code == "native_response_failed"
    assert len(lab.native.open_calls) == 1
    assert len(lab.native.prompt_calls) == 1


def test_postgres_owner_lock_loss_blocks_admission(
    owned_agents_pg, admission_lab_factory
):
    from sidera_agents.bootstrap import create_agent_service, open_postgres_store
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader

    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)
    owner_id = lab.service._owner_id
    lab.store.release_execution_owner(owner_id)

    with pytest.raises(AgentError) as caught:
        lab.execution.start(lab.request())
    assert caught.value.code == "execution_owner_unavailable"
    assert lab.native.open_calls == []

    replacement_store = open_postgres_store(
        owned_agents_pg.url.render_as_string(hide_password=False)
    )
    replacement = create_agent_service(
        replacement_store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        replacement.poll_once()
        with pytest.raises(AgentError) as second_owner:
            lab.execution.start(lab.request())
        assert second_owner.value.code == "execution_owner_unavailable"
    finally:
        replacement.close()
        replacement_store.close()
        lab.service.close()


def test_postgres_close_fails_closed_when_owner_unlock_is_unconfirmed(
    owned_agents_pg, admission_lab_factory
):
    from sqlalchemy import event

    lab = _postgres_lab(owned_agents_pg, admission_lab_factory)

    def fail_owner_unlock(connection, cursor, statement, parameters, context, many):
        if "pg_advisory_unlock" in statement:
            raise RuntimeError("synthetic owner unlock failure")

    event.listen(owned_agents_pg.engine, "before_cursor_execute", fail_owner_unlock)
    try:
        with pytest.raises(AgentError) as caught:
            lab.service.close()

        assert caught.value.code == "shutdown_pending"
        assert lab.service._owner_claimed
        assert lab.store.execution_owner_is_held(lab.service._owner_id)
    finally:
        event.remove(owned_agents_pg.engine, "before_cursor_execute", fail_owner_unlock)
        if lab.service._owner_claimed:
            lab.service.close(timeout=1)
        else:
            owned_agents_pg.engine.dispose()

    assert not lab.store.execution_owner_is_held(lab.service._owner_id)
