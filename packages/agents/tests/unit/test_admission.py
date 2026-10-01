from hashlib import sha256
from threading import Event, Thread
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sidera_agents.admission import request_fingerprint
from sidera_agents.contracts import (
    ContextItem,
    CredentialRef,
    ExecutionWorkspace,
    NativeCloseOutcome,
    NativeCloseState,
    NativeInspection,
    NativeOutcomeStatus,
    NativeProcessState,
    NativeResponseState,
    ProfileRef,
    RunPolicy,
    RunStart,
    SecretBinding,
    SourceContent,
    SourceRef,
    ToolBinding,
    ToolConnection,
)
from sidera_agents.errors import AgentError


def test_request_fingerprint_handles_unpaired_surrogate_without_exposing_text():
    marker = "SYNTHETIC_SECRET"
    request = RunStart(
        operation_id=uuid4(),
        profile_ref=ProfileRef(workspace_id=uuid4(), profile_id=uuid4(), revision=1),
        grant_id=uuid4(),
        policy=RunPolicy.BOUNDED,
        instructions=(),
        text=marker + chr(0xD800),
        context=(),
        tools=(),
        workspace=ExecutionWorkspace(
            directory="/tmp/admission-test",
            writable=True,
            artifact_directory=None,
        ),
        required_capabilities=(),
    )

    fingerprint = request_fingerprint(request, "sha256:" + "0" * 64)

    assert fingerprint.startswith("sha256:")
    assert marker not in fingerprint


def test_busy_retry_and_accepted_dedup(request):
    admission_lab = request.getfixturevalue("admission_lab")
    first_request = admission_lab.request(policy="conversation")
    first = admission_lab.execution.start(first_request)
    admission_lab.native.outcomes[first.run_id] = NativeOutcomeStatus.UNKNOWN

    assert first.status == "accepted"
    assert admission_lab.native.open_calls == []
    admission_lab.pump()

    retry_request = admission_lab.request()
    busy = admission_lab.execution.start(retry_request)
    assert busy.status == "busy"
    assert busy.run_id is None

    admission_lab.native.finish(first.run_id)
    admission_lab.pump()
    accepted = admission_lab.execution.start(retry_request)
    repeated = admission_lab.execution.start(retry_request)

    assert accepted.status == "accepted"
    assert repeated.run_id == accepted.run_id
    admission_lab.pump()
    assert len(admission_lab.native.open_calls) == 2


class _CountingSecretReader:
    def __init__(self):
        self.calls = []

    def read(self, grant_id, binding):
        self.calls.append((grant_id, binding))
        return SecretStr("synthetic-secret-value")


class _CountingToolConnectionReader:
    def __init__(self, *, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    def resolve(self, grant_id, connection_id):
        self.calls.append((grant_id, connection_id))
        if connection_id in self.fail_for:
            raise AgentError(
                "tool_connection_unavailable", "Tool connection is unavailable."
            )
        return ToolConnection(
            name="synthetic tool",
            transport="stdio",
            endpoint_or_argv=("/tmp/synthetic-tool",),
            headers={},
            environment={},
        )


def test_accepted_operation_replay_precedes_profile_status_validation(admission_lab):
    lab = admission_lab
    profile = _profile_with_secret(lab)
    source_text = "SYNTHETIC_SOURCE_TEXT"
    source_ref = SourceRef(provider="fixture", key="notes/one", version="v1")
    source_content = SourceContent(
        text=source_text,
        actual_version="v1",
        content_hash="sha256:" + sha256(source_text.encode("utf-8")).hexdigest(),
    )

    class ToggleSourceReader:
        def __init__(self):
            self.calls = []
            self.available = True

        def read(self, grant_id, ref):
            self.calls.append((grant_id, ref))
            if not self.available:
                raise RuntimeError("synthetic source unavailable")
            return source_content

    source_reader = ToggleSourceReader()
    secrets = _CountingSecretReader()
    tools = _CountingToolConnectionReader()
    lab.execution._sources = source_reader
    lab.execution._secrets = secrets
    lab.execution._tool_connections = tools
    request = lab.request(
        profile_ref=profile.ref,
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
    assert source_reader.calls == [(lab.grant_id, source_ref)]

    source_reader.available = False
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

    replayed = lab.execution.start(request)

    assert replayed == accepted
    assert source_reader.calls == [(lab.grant_id, source_ref)]
    assert secrets.calls == []
    assert tools.calls == []
    assert lab.native.open_calls == []

    lab.pump()

    assert source_reader.calls == [(lab.grant_id, source_ref)]
    assert len(secrets.calls) == 1
    assert len(tools.calls) == 1
    assert len(lab.native.open_calls) == 1


def test_changed_payload_conflicts_before_profile_status_validation(admission_lab):
    lab = admission_lab
    request = lab.request()
    accepted = lab.execution.start(request)
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
    altered = request.model_copy(update={"text": "different synthetic request"})

    with pytest.raises(AgentError) as caught:
        lab.execution.start(altered)

    assert accepted.status.value == "accepted"
    assert caught.value.code == "operation_conflict"
    lab.pump()
    assert len(lab.native.open_calls) == 1


def test_accepted_operation_record_persists_identity_without_request_or_secret(
    admission_lab,
):
    lab = admission_lab
    profile = _profile_with_secret(lab)
    request_text = "SYNTHETIC_REQUEST_TEXT"
    request = lab.request(profile_ref=profile.ref, text=request_text)
    receipt = lab.execution.start(request)

    get_operation = getattr(lab.store, "get_start_operation", None)
    assert callable(get_operation)
    record = get_operation(lab.workspace_id, request.operation_id)

    assert receipt.status.value == "accepted"
    assert record.profile_ref == profile.ref
    assert record.profile_config_hash == profile.config_hash
    assert record.fingerprint == request_fingerprint(request, profile.config_hash)
    assert record.receipt == receipt
    assert request_text not in repr(lab.store._run_operations)
    assert "synthetic-secret-value" not in repr(lab.store._run_operations)
    assert request_text not in repr(lab.store._runs)
    assert lab.native.open_calls == []


def test_busy_operation_record_persists_identity_for_exact_retry(admission_lab):
    lab = admission_lab
    occupying = lab.execution.start(lab.request())
    request = lab.request()
    receipt = lab.execution.start(request)

    get_operation = getattr(lab.store, "get_start_operation", None)
    assert callable(get_operation)
    record = get_operation(lab.workspace_id, request.operation_id)

    assert occupying.status.value == "accepted"
    assert receipt.status.value == "busy"
    assert record.profile_ref == lab.profile.ref
    assert record.profile_config_hash == lab.profile.config_hash
    assert record.fingerprint == request_fingerprint(request, lab.profile.config_hash)
    assert record.receipt == receipt


def test_operation_profile_mismatch_requires_authority_before_conflict(admission_lab):
    lab = admission_lab
    stored_profile = lab.profile
    accepted = lab.execution.start(lab.request(profile_ref=stored_profile.ref))
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
    lab.pump()
    assert len(lab.native.open_calls) == 1


def _profile_with_secret(lab):
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=lab.workspace_id, credential_id=uuid4()
        ),
        destination="SYNTHETIC_TOKEN",
    )
    return lab.catalog.revise_profile(
        lab.profile.ref,
        lab.profile.draft.model_copy(update={"secret_refs": (binding,)}),
        grant_id=lab.grant_id,
    )


def test_busy_and_duplicate_starts_do_not_resolve_sensitive_projections(admission_lab):
    lab = admission_lab
    profile = _profile_with_secret(lab)
    secrets = _CountingSecretReader()
    connection_id = uuid4()
    tools = _CountingToolConnectionReader()
    lab.execution._secrets = secrets
    lab.execution._tool_connections = tools
    request = lab.request(
        profile_ref=profile.ref,
        tools=(ToolBinding(connection_id=connection_id),),
    )

    first = lab.execution.start(request)
    duplicate = lab.execution.start(request)
    busy = lab.execution.start(
        lab.request(
            profile_ref=profile.ref,
            tools=(ToolBinding(connection_id=connection_id),),
        )
    )

    assert first.status == "accepted"
    assert duplicate.run_id == first.run_id
    assert busy.status == "busy"
    assert secrets.calls == []
    assert tools.calls == []

    lab.pump()

    assert len(secrets.calls) == 1
    assert len(tools.calls) == 1
    assert len(lab.native.open_calls) == 1


def test_projection_failure_releases_only_its_admission(admission_lab_factory):
    from sidera_agents.memory import MemoryAgentStore

    lab = admission_lab_factory(
        MemoryAgentStore(),
        runtime_max_active=2,
        runtime_max_sessions=2,
        profile_max_active=2,
    )
    profile = _profile_with_secret(lab)
    secrets = _CountingSecretReader()
    failing_connection_id = uuid4()
    tools = _CountingToolConnectionReader(fail_for=(failing_connection_id,))
    lab.execution._secrets = secrets
    lab.execution._tool_connections = tools

    failing_request = lab.request(
        profile_ref=profile.ref,
        tools=(ToolBinding(connection_id=failing_connection_id),),
    )
    retained_request = lab.request(
        profile_ref=profile.ref,
        policy="conversation",
    )
    rejected = lab.execution.start(failing_request)
    retained = lab.execution.start(retained_request)

    assert rejected.status == retained.status == "accepted"
    assert secrets.calls == []
    assert tools.calls == []
    assert lab.store.get_runtime_entry(lab.runtime.ref).installation.session_count == 2

    lab.pump()

    failed_run = lab.execution.get_run(
        lab.workspace_id, rejected.run_id, grant_id=lab.grant_id
    )
    idle_run = lab.execution.get_run(
        lab.workspace_id, retained.run_id, grant_id=lab.grant_id
    )
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    failed_receipt = lab.execution.get_start(
        lab.workspace_id, failing_request.operation_id, grant_id=lab.grant_id
    )

    assert failed_run.state.value == "failed"
    assert idle_run.state.value == "idle"
    assert failed_receipt.receipt.status.value == "rejected"
    assert installation.active_count == 0
    assert installation.session_count == 1
    assert [call[1] for call in tools.calls] == [failing_connection_id]
    assert len(secrets.calls) == 2
    assert len(lab.native.open_calls) == 1


def test_lowered_profile_limit_applies_to_old_pinned_revision(
    admission_lab_factory,
):
    from sidera_agents.memory import MemoryAgentStore

    lab = admission_lab_factory(
        MemoryAgentStore(),
        runtime_max_active=2,
        profile_max_active=2,
    )
    old_revision = lab.profile
    first = lab.execution.start(lab.request(profile_ref=old_revision.ref))
    lab.catalog.revise_profile(
        old_revision.ref,
        old_revision.draft.model_copy(update={"max_active": 1}),
        grant_id=lab.grant_id,
    )
    request = lab.request(profile_ref=old_revision.ref)

    busy = lab.execution.start(request)

    assert first.status == "accepted"
    assert busy.status == "busy"
    lab.pump()
    retried = lab.execution.start(request)
    assert retried.status == "accepted"


def test_uncertain_close_retains_session_until_process_gone(admission_lab):
    lab = admission_lab
    receipt = lab.execution.start(lab.request())
    lab.native.close = lambda handle: NativeCloseOutcome(
        status=NativeCloseState.UNKNOWN,
        reason_code="native_close_unknown",
    )

    lab.pump()

    closing = lab.execution.get_run(
        lab.workspace_id, receipt.run_id, grant_id=lab.grant_id
    )
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert closing.state.value == "closing"
    assert closing.reason_code == "native_close_unknown"
    assert installation.active_count == 0
    assert installation.session_count == 1

    lab.native.inspections[receipt.run_id] = NativeInspection(
        process_state=NativeProcessState.GONE,
        response_state=NativeResponseState.IDLE,
        reason_code=None,
    )
    lab.pump()

    completed = lab.execution.get_run(
        lab.workspace_id, receipt.run_id, grant_id=lab.grant_id
    )
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert completed.state.value == "completed"
    assert completed.reason_code is None
    assert installation.active_count == 0
    assert installation.session_count == 0


def test_close_retains_owner_while_native_boundary_is_blocked(admission_lab):
    lab = admission_lab
    entered = Event()
    unblock = Event()
    open_native = lab.native.open

    def blocked_open(request):
        entered.set()
        if not unblock.wait(timeout=2):
            raise RuntimeError("controlled timeout")
        return open_native(request)

    lab.native.open = blocked_open
    lab.execution.start(lab.request())
    lab.service.start_background(interval_seconds=0.001)
    assert entered.wait(timeout=1)

    thread = lab.service._thread
    actual_join = thread.join
    thread.join = lambda timeout=None: None
    try:
        with pytest.raises(AgentError) as caught:
            lab.service.close()
        assert caught.value.code == "shutdown_pending"
        assert lab.store.execution_owner_is_held(lab.service._owner_id)
    finally:
        thread.join = actual_join
        unblock.set()
        thread.join(timeout=1)

    lab.service.close()
    assert not lab.store.execution_owner_is_held(lab.service._owner_id)


def test_close_waits_for_direct_poll_before_releasing_owner(admission_lab):
    lab = admission_lab
    entered = Event()
    unblock = Event()
    open_native = lab.native.open

    def blocked_open(request):
        entered.set()
        if not unblock.wait(timeout=2):
            raise RuntimeError("controlled timeout")
        return open_native(request)

    lab.native.open = blocked_open
    lab.execution.start(lab.request())
    poll_thread = Thread(target=lab.service.poll_once)
    poll_thread.start()
    assert entered.wait(timeout=1)

    try:
        with pytest.raises(AgentError) as caught:
            lab.service.close(timeout=0.01)
        assert caught.value.code == "shutdown_pending"
        assert lab.store.execution_owner_is_held(lab.service._owner_id)
    finally:
        unblock.set()
        poll_thread.join(timeout=1)

    lab.service.close(timeout=1)
    assert not poll_thread.is_alive()
    assert not lab.store.execution_owner_is_held(lab.service._owner_id)


def test_operation_conflict_does_not_start_an_altered_request(admission_lab):
    lab = admission_lab
    request = lab.request()
    accepted = lab.execution.start(request)
    altered = request.model_copy(update={"text": "different request"})

    with pytest.raises(AgentError) as caught:
        lab.execution.start(altered)

    assert caught.value.code == "operation_conflict"
    assert accepted.status == "accepted"
    assert lab.native.open_calls == []
    lab.pump()
    assert len(lab.native.open_calls) == 1


def test_uncertain_open_is_not_replayed_after_fresh_service(admission_lab):
    from sidera_agents.bootstrap import create_agent_service
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader

    lab = admission_lab
    request = lab.request()
    attempted = []

    def uncertain_open(open_request):
        attempted.append(open_request.run_id)
        raise RuntimeError("native acknowledgement lost")

    lab.native.open = uncertain_open
    accepted = lab.execution.start(request)
    lab.pump()
    original_service = lab.service
    original_service.close()
    recovered = create_agent_service(
        lab.store,
        lab.registry,
        lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        lab.clock,
    )
    try:
        recovered.poll_once()
        receipt = recovered.execution.get_start(
            lab.workspace_id, request.operation_id, grant_id=lab.grant_id
        )
        repeated = recovered.execution.start(request)
        recovered.poll_once()
    finally:
        recovered.close()

    assert accepted.status == "accepted"
    assert len(attempted) == 1
    assert receipt.receipt.status.value == "unknown"
    assert repeated.run_id == accepted.run_id
    assert len(attempted) == 1


def test_verified_open_failure_releases_only_its_reservations(admission_lab_factory):
    from sidera_agents.memory import MemoryAgentStore
    from sidera_agents.runs import VerifiedNativeOpenFailure

    lab = admission_lab_factory(
        MemoryAgentStore(),
        runtime_max_active=2,
        runtime_max_sessions=2,
        profile_max_active=2,
    )
    failed = lab.execution.start(lab.request())
    retained = lab.execution.start(lab.request(policy="conversation"))
    open_native = lab.native.open

    def fail_one(open_request):
        if open_request.run_id == failed.run_id:
            raise VerifiedNativeOpenFailure("native_spawn_failed")
        return open_native(open_request)

    lab.native.open = fail_one
    lab.pump()

    failed_snapshot = lab.execution.get_run(
        lab.workspace_id, failed.run_id, grant_id=lab.grant_id
    )
    retained_snapshot = lab.execution.get_run(
        lab.workspace_id, retained.run_id, grant_id=lab.grant_id
    )
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation

    assert failed_snapshot.state.value == "failed"
    assert retained_snapshot.state.value == "idle"
    assert installation.active_count == 0
    assert installation.session_count == 1
    assert len(lab.native.open_calls) == 1


def test_idle_session_releases_active_capacity_but_retains_session(admission_lab):
    lab = admission_lab
    first = lab.execution.start(lab.request(policy="conversation"))
    lab.pump()
    counts = lab.store.get_runtime_entry(lab.runtime.ref).installation

    assert counts.active_count == 0
    assert counts.session_count == 1

    next_run = lab.execution.start(lab.request())
    assert next_run.status == "accepted"
    lab.pump()
    counts = lab.store.get_runtime_entry(lab.runtime.ref).installation
    assert counts.active_count == 0
    assert counts.session_count == 1
    assert first.run_id != next_run.run_id


def test_revoked_grant_and_lost_owner_block_new_start(admission_lab):
    lab = admission_lab
    lab.authorizer.workspace_grants.pop(lab.grant_id)
    with pytest.raises(AgentError) as revoked:
        lab.execution.start(lab.request())
    assert revoked.value.code == "permission_denied"

    lab.authorizer.grant_workspace(lab.workspace_id, lab.grant_id)
    lab.store.release_execution_owner(lab.service._owner_id)
    with pytest.raises(AgentError) as owner_lost:
        lab.execution.start(lab.request())
    assert owner_lost.value.code == "execution_owner_unavailable"


def test_service_factory_is_thread_free_until_background_start(admission_lab):
    lab = admission_lab
    assert lab.service._thread is None
    assert lab.store.execution_owner_is_held(lab.service._owner_id)

    lab.service.start_background(interval_seconds=0.001)
    thread = lab.service._thread
    assert thread is not None and thread.is_alive()
    lab.service.close()

    assert not thread.is_alive()
    assert not lab.store.execution_owner_is_held(lab.service._owner_id)
