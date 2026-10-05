from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sidera_agents.contracts import (
    EffectiveConfig,
    EventPage,
    RunEvent,
    RunSnapshot,
    StopReceipt,
)
from sidera_agents.errors import AgentError
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from sidera_control_plane.application.agent_worker_adapter import (
    NativeContextPreparationReceipt,
    NativeWorkerServices,
    native_capabilities,
    native_operation_id,
    native_preparation_command,
    project_run_event,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.worker_gateway import (
    STOP_TOPIC,
    GatewayError,
    GatewayRecoveryRequired,
    WorkerGateway,
    _AssignmentContext,
)
from sidera_control_plane.domain.agent_bindings import (
    AgentBinding,
    AgentExecutionLink,
    AssigneeRef,
)
from sidera_control_plane.domain.commands import StoredCommand
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.gateway import (
    ExecutionEvent,
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
    RuntimeModuleOperation,
    gateway_operation_id,
)
from sidera_control_plane.domain.lease import AssignmentLease, LeaseStatus
from sidera_control_plane.domain.modules import (
    ExecutionBinding,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    SandboxCapability,
    StabilityLevel,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.agent_binding_repository import (
    AgentBindingRepository,
)
from sidera_control_plane.persistence.execution_authority_repository import (
    ExecutionAuthorityRepository,
)
from sidera_control_plane.persistence.execution_repository import ExecutionRepository
from sidera_control_plane.persistence.lease_store import LeaseStore
from sidera_control_plane.persistence.models import TaskModel
from sidera_control_plane.persistence.session_repository import SessionRepository
from tests.unit.test_execution_authority_provenance import authority as make_authority

NOW = datetime(2026, 10, 5, 3, tzinfo=UTC)


def forbidden(*args, **kwargs):
    pytest.fail("Original Stop recovery attempted a new native action or lease renewal")


class Records:
    def __init__(self, state, name):
        self.state, self.name = state, name

    def get(self, identifier, workspace_id=None, **kwargs):
        row = getattr(self.state, self.name)
        return (
            row
            if row is not None
            and row.id == identifier
            and (workspace_id is None or row.workspace_id == workspace_id)
            else None
        )

    get_any = get

    def get_by_operation_id(self, identifier, workspace_id, **kwargs):
        row = getattr(self.state, self.name)
        return (
            row
            if row is not None
            and row.operation_id == identifier
            and (row.workspace_id == workspace_id)
            else None
        )

    def save(self, row, previous_version):
        assert row is getattr(self.state, self.name)
        assert row.version == previous_version + 1
        self.state.saves.append((self.name, previous_version, row.version))

    def has_live_for_task(self, task_id):
        assert task_id == self.state.task.id
        return self.state.execution.status is ExecutionStatus.RUNNING


class ExecutionEvents:
    def __init__(self, state):
        self.state = state

    def get(self, session_id, sequence, workspace_id):
        assert (session_id, workspace_id) == (
            self.state.session.id,
            self.state.execution.workspace_id,
        )
        return self.state.execution_events.get(sequence)

    def add(self, event):
        assert event.sequence not in self.state.execution_events
        self.state.execution_events[event.sequence] = event


class BusinessEvents:
    def __init__(self, state):
        self.state = state

    def append_with_outbox(self, event):
        key = (event.aggregate_type, event.aggregate_id, event.aggregate_version)
        assert key not in self.state.event_keys, "uq_event_aggregate_version"
        self.state.event_keys.add(key)
        self.state.business_events.append(event)


class PublicOriginal:
    def __init__(self, state):
        self.state = state

    def get_stop(self, workspace_id, run_id, operation_id, *, grant_id):
        s = self.state
        assert (workspace_id, run_id, operation_id, grant_id) == (
            s.authority.workspace_id,
            s.link.run_id,
            s.original_operation_id,
            s.authority.id,
        )
        s.native_reads.append("get_stop")
        return s.receipt

    def get_run(self, workspace_id, run_id, *, grant_id):
        s = self.state
        assert (workspace_id, run_id, grant_id) == (
            s.authority.workspace_id,
            s.link.run_id,
            s.authority.id,
        )
        s.native_reads.append("get_run")
        return s.snapshot

    def list_events(self, workspace_id, run_id, after_sequence, *, grant_id):
        s = self.state
        assert (workspace_id, run_id, grant_id) == (
            s.authority.workspace_id,
            s.link.run_id,
            s.authority.id,
        )
        s.native_reads.append("list_events")
        events = tuple(
            event for event in s.native_events if event.sequence > after_sequence
        )
        return EventPage(events=events, next_sequence=2, has_more=False)

    def __getattr__(self, name):
        return forbidden


def preparation(authority):
    package_id = uuid4()
    command_id = uuid5(
        NAMESPACE_URL,
        f"sidera.native.prepare-context:{authority.execution_id}:1:{package_id}:{'a' * 64}:{'b' * 64}",
    )
    receipt = NativeContextPreparationReceipt(
        command_id=command_id,
        workspace_id=authority.workspace_id,
        execution_id=authority.execution_id,
        binding_version=1,
        authority_id=authority.id,
        lease_id=authority.lease_id,
        fencing_token=1,
        package_id=package_id,
        package_hash="a" * 64,
        revision_id=uuid4(),
        revision_hash="b" * 64,
        context_version=1,
        sources=(),
        validated_bytes=0,
        validated_sections=0,
        workspace_manifest_id=authority.workspace_ref,
        workspace_manifest_digest="c" * 64,
        workspace_location_hash="d" * 64,
        physical_input=None,
        capture_operation_id=native_operation_id(
            authority.execution_id, 1, "code-capture"
        ),
    )
    command = native_preparation_command(receipt)
    saved = StoredCommand(
        command.id,
        command.workspace_id,
        command.actor_type,
        command.actor_id,
        command.idempotency_key,
        command.target_type,
        hash_request(command),
        "applied",
        {"data": {"preparation": receipt.model_dump(mode="json")}},
        None,
    )
    return receipt, saved


@pytest.fixture
def terminal_stop_factory(monkeypatch):
    io_sessions = []

    def build(*, terminal="succeeded", closed=True, expired=False, ingest=False):
        authority = make_authority()
        now = NOW
        worker = Worker.register(
            worker_id=uuid4(),
            workspace_id=authority.workspace_id,
            adapter_type="controlled-native-terminal",
            display_name="Original Worker",
            capabilities=frozenset({"code"}),
            capacity=1,
            cost_per_hour_microunits=0,
            startup_latency_ms=0,
            now=now,
        )
        worker.reserve()
        worker.mark_running()
        task = Task.create(
            task_id=authority.target_id,
            workspace_id=authority.workspace_id,
            title="Original terminal Stop",
            description="Controlled original",
            acceptance_criteria=("Original closure only",),
            priority=1,
            requested_by=authority.principal_id,
        )
        task.mark_ready()
        task.mark_running()
        execution = Execution.create(
            execution_id=authority.execution_id,
            workspace_id=authority.workspace_id,
            task_id=task.id,
            lineage_id=uuid4(),
            attempt=1,
            max_attempts=1,
            purpose="code",
            capability_requirements=frozenset({"code"}),
            pinned_worker_id=worker.id,
            not_before=now,
        )
        execution.assign(
            lease_id=authority.lease_id, worker_id=worker.id, fencing_token=1
        )
        execution.start(lease_id=authority.lease_id, fencing_token=1, now=now)
        execution.request_stop()
        lease = AssignmentLease.issue(
            lease_id=authority.lease_id,
            workspace_id=authority.workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            issued_at=now - timedelta(minutes=2),
            expires_at=now + timedelta(seconds=-1 if expired else 60),
            fencing_token=1,
            assignment_reason={"reason": "Original Stop fixture"},
        )
        capabilities = native_capabilities()
        adapter_definition = ModuleDefinition(
            uuid4(),
            authority.workspace_id,
            "worker_adapter",
            worker.adapter_type,
            "1",
            1,
            capabilities,
            ModuleLifecycle.ACTIVE,
        )
        workspace_definition = ModuleDefinition(
            uuid4(),
            authority.workspace_id,
            "workspace_provider",
            "controlled-workspace",
            "1",
            1,
            WorkspaceProviderCapabilityProfile(
                SandboxCapability.FILESYSTEM,
                True,
                False,
                StabilityLevel.STABLE,
            ),
            ModuleLifecycle.ACTIVE,
        )
        adapter_instance = ModuleInstance(
            uuid4(),
            authority.workspace_id,
            adapter_definition.id,
            "Original adapter",
            1,
            "adapter",
            {},
            (),
            ModuleLifecycle.ACTIVE,
        )
        workspace_instance = ModuleInstance(
            uuid4(),
            authority.workspace_id,
            workspace_definition.id,
            "Original workspace",
            1,
            "workspace",
            {},
            (),
            ModuleLifecycle.ACTIVE,
        )
        binding = ExecutionBinding(
            uuid4(),
            authority.workspace_id,
            execution.id,
            worker.id,
            adapter_instance.id,
            workspace_instance.id,
            1,
            1,
            capabilities,
            lease.id,
            1,
            1,
        )
        selected = AgentBinding(
            authority.workspace_id,
            "subtask",
            task.id,
            AssigneeRef("profile", authority.profile_ref.profile_id),
            authority.profile_ref,
            1,
            now,
            now,
            runnable=True,
        )
        run_id = uuid4()
        session = ExecutionSession.start(
            session_id=uuid5(NAMESPACE_URL, f"sidera.native.session:{run_id}"),
            workspace_id=authority.workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            adapter_session_id=f"agent-run:{run_id}",
            now=now,
            process_ref=f"agent-run-reference:{run_id}",
            adapter_module_id=adapter_instance.id,
            adapter_config_version=1,
            workspace_module_id=workspace_instance.id,
            workspace_config_version=1,
            capability_snapshot=capabilities,
        )
        session.record_activity(sequence=1, now=now)
        if not ingest:
            session.record_activity(sequence=2, now=now)
        if closed:
            session.close(lost=False, now=now)
        receipt, saved_command = preparation(authority)
        operation_id = gateway_operation_id(
            execution.id, GatewayOperationType.STOP_SESSION
        )
        operation = GatewayOperation.create(
            id=uuid4(),
            operation_id=operation_id,
            workspace_id=authority.workspace_id,
            task_id=task.id,
            execution_id=execution.id,
            session_id=session.id,
            operation_type=GatewayOperationType.STOP_SESSION,
            module_instance_id=adapter_instance.id,
            config_version=1,
            lease_id=lease.id,
            fencing_token=1,
            now=now,
        )
        operation.start(now=now)
        operation.mark_unknown(failure_code="native_stop_unconfirmed", now=now)
        link = AgentExecutionLink(
            authority.workspace_id,
            execution.id,
            uuid4(),
            authority.profile_ref,
            run_id,
            session.last_sequence,
            terminal,
            authority.id,
            1,
            preparation=receipt.model_dump(mode="json"),
        )
        native_event = RunEvent(
            run_id=run_id,
            sequence=2,
            occurred_at=now,
            kind="stop.stopped",
            operation_id=operation_id,
            response_id=None,
            data={"status": "stopped"},
        )
        projected = project_run_event(native_event)
        persisted = ExecutionEvent(
            uuid4(),
            authority.workspace_id,
            execution.id,
            session.id,
            2,
            projected.event_type,
            projected.payload,
            now,
            projected.raw_ref,
        )
        snapshot = RunSnapshot(
            run_id=run_id,
            workspace_id=authority.workspace_id,
            profile_ref=authority.profile_ref,
            policy="bounded",
            state="completed" if terminal == "succeeded" else "failed",
            input_open=False,
            active_response_id=None,
            last_event_sequence=2,
            capabilities={},
            reason_code=None,
            effective_config=EffectiveConfig(
                profile_ref=authority.profile_ref,
                config_hash="controlled-config",
                runtime_ref=authority.runtime_ref,
                actual_runtime_version="controlled",
                selected_model=None,
                observed_model=None,
                inherited_fields=(),
                applied_fields=(),
                resource_refs=(),
                context_receipts=(),
            ),
        )
        state = SimpleNamespace(
            authority=authority,
            execution=execution,
            task=task,
            lease=lease,
            worker=worker,
            session=session,
            binding=binding,
            selected=selected,
            original_selected=selected,
            link=link,
            operation=operation,
            original_operation_id=operation_id,
            owner=True,
            native_reads=[],
            native_events=(native_event,),
            snapshot=snapshot,
            receipt=StopReceipt(
                operation_id=operation_id,
                run_id=run_id,
                status="stopped",
                reason_code=None,
                evidence_sequence=2,
            ),
            saves=[],
            business_events=[],
            event_keys=set(),
            original_locks=[],
            execution_events={} if ingest else {2: persisted},
            link_saves=[],
        )
        modules = {row.id: row for row in (adapter_instance, workspace_instance)}
        definitions = {
            row.id: row for row in (adapter_definition, workspace_definition)
        }
        uow = SimpleNamespace(
            executions=Records(state, "execution"),
            sessions=Records(state, "session"),
            leases=Records(state, "lease"),
            workers=Records(state, "worker"),
            tasks=Records(state, "task"),
            gateway_operations=Records(state, "operation"),
            bindings=SimpleNamespace(
                get_for_execution=lambda *args, **kwargs: state.binding
            ),
            agent_bindings=SimpleNamespace(
                get=lambda *args, **kwargs: state.selected,
                history=lambda *args, **kwargs: (state.original_selected,),
            ),
            modules=SimpleNamespace(
                get_instance=lambda identifier, workspace: modules.get(identifier),
                get_definition=lambda identifier, workspace: definitions.get(
                    identifier
                ),
            ),
            commands=SimpleNamespace(find_by_id=lambda *args: saved_command),
            execution_events=ExecutionEvents(state),
            events=BusinessEvents(state),
        )
        io_session = Session()
        io_sessions.append(io_session)
        repository = ExecutionAuthorityRepository(io_session)
        repository.get = lambda identifier, **kwargs: (
            state.authority if identifier == state.authority.id else None
        )
        repository.get_link = lambda *args, **kwargs: state.link

        def aggregate_read(statement):
            assert statement.column_descriptions[0]["entity"] is TaskModel
            state.original_locks.append("task")
            return state.task

        def original_update(statement):
            assert isinstance(statement, Update)
            assert statement.table.name == "agent_execution_links"
            values = statement.compile().params
            assert values["workspace_id_1"] == state.link.workspace_id
            assert values["execution_id_1"] == state.link.execution_id
            assert values["event_cursor_1"] == state.link.event_cursor
            previous = state.link
            state.link = replace(
                previous, event_cursor=values["event_cursor"], status=values["status"]
            )
            state.link_saves.append((previous, state.link))

        monkeypatch.setattr(io_session, "scalar", aggregate_read)
        monkeypatch.setattr(io_session, "execute", original_update)
        monkeypatch.setattr(
            ExecutionRepository,
            "get",
            lambda self, *args, **kwargs: uow.executions.get(*args, **kwargs),
        )
        monkeypatch.setattr(
            LeaseStore,
            "get",
            lambda self, *args, **kwargs: uow.leases.get(*args, **kwargs),
        )
        monkeypatch.setattr(
            SessionRepository,
            "get_any",
            lambda self, *args, **kwargs: uow.sessions.get_any(*args, **kwargs),
        )
        monkeypatch.setattr(
            AgentBindingRepository,
            "get",
            lambda self, *args, **kwargs: uow.agent_bindings.get(*args, **kwargs),
        )
        monkeypatch.setattr(
            AgentBindingRepository,
            "history",
            lambda self, *args, **kwargs: uow.agent_bindings.history(*args, **kwargs),
        )
        monkeypatch.setattr(AssignmentLease, "renew", forbidden)
        uow.execution_authorities = repository
        public = PublicOriginal(state)
        services = NativeWorkerServices(
            public,
            public,
            public,
            SimpleNamespace(require=forbidden),
            authority.principal_id,
            uuid4(),
            observations=public,
        )
        gateway = WorkerGateway(
            lambda: nullcontext(uow),
            adapter_factories={},
            workspace_factories={},
            native_services=services,
            native_source_reader=public,
            owner_check=lambda: state.owner,
            clock=lambda: now,
        )
        gateway.sync_session = forbidden
        state.gateway, state.uow, state.public = gateway, uow, public
        state.context = _AssignmentContext(
            execution,
            task,
            None,
            worker,
            lease,
            binding,
            adapter_definition,
            adapter_instance,
            workspace_definition,
            workspace_instance,
        )
        state.command_id = uuid4()
        state.message = OutboxMessage(
            uuid4(),
            uuid4(),
            STOP_TOPIC,
            {
                "workspace_id": str(authority.workspace_id),
                "aggregate_id": str(execution.id),
                "lease_id": str(lease.id),
                "session_id": str(session.id),
                "command_id": str(state.command_id),
                "fencing_token": 1,
            },
            0,
            uuid4(),
        )
        if closed:
            uow.events.append_with_outbox(
                EventData(
                    uuid4(),
                    authority.workspace_id,
                    "execution_session",
                    session.id,
                    session.version,
                    "ExecutionSessionClosed",
                    {"status": "closed"},
                    "system",
                    "controlled",
                    state.command_id,
                    state.command_id,
                )
            )
        state.initial_business_count = len(state.business_events)
        return state

    try:
        yield build
    finally:
        for session in io_sessions:
            session.close()


@pytest.mark.parametrize("terminal", ("succeeded", "failed"))
@pytest.mark.parametrize("closed", (False, True))
def test_terminal_original_cleanup_preserves_outcome_and_replays_once(
    terminal_stop_factory,
    terminal,
    closed,
):
    s = terminal_stop_factory(terminal=terminal, closed=closed)
    original_link, session_version = s.link, s.session.version
    s.gateway._persist_stop_success(
        s.context,
        session_id=s.session.id,
        operation_id=s.original_operation_id,
        accepted_sequence=2,
        command_id=s.command_id,
    )
    assert s.link == original_link and s.link.status == terminal
    assert s.operation.status is GatewayOperationStatus.SUCCEEDED
    assert s.operation.result_ref == "sequence:2"
    assert s.execution.status is ExecutionStatus.CANCELED
    assert (
        s.execution.active_lease_id is None and s.lease.status is LeaseStatus.RELEASED
    )
    assert (
        s.worker.current_load == 0 and s.session.status is ExecutionSessionStatus.CLOSED
    )
    assert s.session.version == session_version + (not closed)
    new_events = s.business_events[s.initial_business_count :]
    assert sum(
        event.event_type == "ExecutionSessionClosed" for event in new_events
    ) == (not closed)
    assert {event.event_type for event in new_events} >= {
        "ExecutionCanceledByStop",
        "LeaseReleasedByStop",
    }
    before = (
        tuple(s.saves),
        tuple(s.business_events),
        s.execution.version,
        s.lease.version,
        s.worker.version,
        s.session.version,
        s.operation.version,
    )
    replay = s.gateway.handle_stop(s.message)
    assert (
        replay.operation_id == s.original_operation_id and replay.accepted_sequence == 2
    )
    assert before == (
        tuple(s.saves),
        tuple(s.business_events),
        s.execution.version,
        s.lease.version,
        s.worker.version,
        s.session.version,
        s.operation.version,
    )
    assert not s.native_reads


@pytest.mark.parametrize("terminal", ("succeeded", "failed"))
@pytest.mark.parametrize("closed", (False, True))
def test_expired_original_query_uses_real_historical_ingestion_without_sync(
    terminal_stop_factory,
    terminal,
    closed,
):
    s = terminal_stop_factory(
        terminal=terminal, closed=closed, expired=True, ingest=True
    )
    original_link, deadline = s.link, s.lease.expires_at
    result = s.gateway.handle_stop(s.message)
    assert (
        result.operation_id == s.original_operation_id and result.accepted_sequence == 2
    )
    assert s.native_reads == ["get_stop", "get_run", "get_run", "list_events"]
    assert s.original_locks and s.link_saves
    assert s.link == replace(original_link, event_cursor=2)
    assert s.link.status == terminal and s.session.last_sequence == 2
    assert s.execution_events[2].event_type == "native.stop.stopped"
    assert s.operation.status is GatewayOperationStatus.SUCCEEDED
    assert (
        s.execution.status is ExecutionStatus.CANCELED
        and s.execution.active_lease_id is None
    )
    assert s.lease.status is LeaseStatus.RELEASED and s.lease.expires_at == deadline
    assert s.lease.renewed_at is None and s.worker.current_load == 0
    assert s.session.status is ExecutionSessionStatus.CLOSED
    assert sum(
        event.event_type == "ExecutionSessionClosed"
        for event in s.business_events[s.initial_business_count :]
    ) == (not closed)


def cleanup_facts(s):
    return (
        s.execution.status,
        s.execution.active_lease_id,
        s.execution.version,
        s.lease.status,
        s.lease.expires_at,
        s.lease.renewed_at,
        s.lease.version,
        s.worker.current_load,
        s.worker.version,
        s.session.status,
        s.session.version,
        s.task.status,
        s.task.version,
        tuple(s.business_events),
        tuple(save for save in s.saves if save[0] != "operation"),
    )


def persist_original(s):
    s.gateway._persist_stop_success(
        s.context,
        session_id=s.session.id,
        operation_id=s.original_operation_id,
        accepted_sequence=2,
        command_id=s.command_id,
    )


@pytest.mark.parametrize("stage", ("cleanup", "replay"))
@pytest.mark.parametrize(
    "bad",
    (
        "missing",
        "workspace",
        "execution",
        "session",
        "sequence",
        "kind",
        "native_kind",
        "run",
        "operation",
        "no_raw_ref",
        "bad_raw_ref",
        "session_cursor",
        "link_cursor",
        "contradictory_terminal",
    ),
)
def test_terminal_stop_requires_exact_saved_positive_event(
    terminal_stop_factory,
    stage,
    bad,
):
    s = terminal_stop_factory()
    if stage == "replay":
        persist_original(s)
    event = s.execution_events[2]
    if bad == "missing":
        s.execution_events.clear()
    elif bad == "session_cursor":
        s.session.last_sequence = 1
    elif bad == "link_cursor":
        s.link = replace(s.link, event_cursor=1)
    elif bad == "contradictory_terminal":
        s.link = replace(s.link, status="cancelled")
    else:
        changes = {
            "workspace": {"workspace_id": uuid4()},
            "execution": {"execution_id": uuid4()},
            "session": {"session_id": uuid4()},
            "sequence": {"sequence": 3},
            "kind": {"event_type": "native.stop.failed"},
            "native_kind": {
                "payload": {**event.payload, "native_kind": "run.completed"}
            },
            "run": {"payload": {**event.payload, "run_id": str(uuid4())}},
            "operation": {"payload": {**event.payload, "operation_id": str(uuid4())}},
            "no_raw_ref": {"raw_ref": None},
            "bad_raw_ref": {"raw_ref": "native-event-sha256:not-a-hash"},
        }
        s.execution_events[2] = replace(event, **changes[bad])
    before = cleanup_facts(s)
    with pytest.raises(GatewayRecoveryRequired):
        if stage == "cleanup":
            persist_original(s)
        else:
            s.gateway.handle_stop(s.message)
    assert cleanup_facts(s) == before and not s.native_reads


@pytest.mark.parametrize("stage", ("cleanup", "query"))
@pytest.mark.parametrize(
    "bad",
    (
        "binding",
        "profile",
        "binding_version",
        "revoked",
        "authority_lease",
        "authority_fence",
        "worker",
        "execution_lease",
        "execution_fence",
        "lease_workspace",
        "lease_execution",
        "lease_worker",
        "lease_fence",
        "released",
        "expired",
        "session_worker",
        "session_module",
        "session_config",
        "session_run",
        "operation_session",
        "operation_type",
        "operation_requested",
        "operation_failed",
        "stop_not_requested",
        "owner",
    ),
)
def test_original_stop_denies_changed_current_ownership_without_cleanup(
    terminal_stop_factory,
    stage,
    bad,
):
    s = terminal_stop_factory(expired=stage == "query")
    if bad == "binding":
        s.binding = replace(s.binding, adapter_config_version=2)
    elif bad == "profile":
        profile = type(s.selected.profile_ref).model_validate(
            {**s.selected.profile_ref.model_dump(), "revision": 2}
        )
        s.selected = replace(s.selected, profile_ref=profile)
    elif bad == "binding_version":
        s.selected = replace(s.selected, version=2)
    elif bad == "revoked":
        s.authority = replace(s.authority, revoked=True)
    elif bad == "authority_lease":
        s.authority = replace(s.authority, lease_id=uuid4())
    elif bad == "authority_fence":
        s.authority = replace(s.authority, fencing_token=2)
    elif bad == "worker":
        s.execution.assigned_worker_id = uuid4()
    elif bad == "execution_lease":
        s.execution.active_lease_id = uuid4()
    elif bad == "execution_fence":
        s.execution.last_fencing_token = 2
    elif bad == "lease_workspace":
        s.lease.workspace_id = uuid4()
    elif bad == "lease_execution":
        s.lease.execution_id = uuid4()
    elif bad == "lease_worker":
        s.lease.worker_id = uuid4()
    elif bad == "lease_fence":
        s.lease.fencing_token = 2
    elif bad == "released":
        s.lease.revoke(now=NOW)
    elif bad == "expired":
        s.lease.expire(now=s.lease.expires_at)
    elif bad == "session_worker":
        s.session.worker_id = uuid4()
    elif bad == "session_module":
        s.session.adapter_module_id = uuid4()
    elif bad == "session_config":
        s.session.adapter_config_version = 2
    elif bad == "session_run":
        s.session.adapter_session_id = f"agent-run:{uuid4()}"
    elif bad == "operation_session":
        s.operation.session_id = uuid4()
    elif bad == "operation_type":
        s.operation.operation_type = GatewayOperationType.START_SESSION
    elif bad == "operation_requested":
        s.operation = GatewayOperation.create(
            id=uuid4(),
            operation_id=s.original_operation_id,
            workspace_id=s.authority.workspace_id,
            task_id=s.task.id,
            execution_id=s.execution.id,
            session_id=s.session.id,
            operation_type=GatewayOperationType.STOP_SESSION,
            module_instance_id=s.binding.adapter_module_id,
            config_version=1,
            lease_id=s.lease.id,
            fencing_token=1,
            now=NOW,
        )
    elif bad == "operation_failed":
        s.operation.resolve_stop(now=NOW, failure_code="controlled_failure")
    elif bad == "stop_not_requested":
        s.execution.stop_requested = False
    else:
        s.owner = False
    before = cleanup_facts(s)
    with pytest.raises((GatewayError, DomainError, AgentError)):
        if stage == "cleanup":
            persist_original(s)
        else:
            s.gateway.handle_stop(s.message)
    assert cleanup_facts(s) == before and not s.native_reads


@pytest.mark.parametrize(
    "bad",
    (
        "accepted",
        "unknown",
        "failed",
        "missing_receipt",
        "pending_run",
        "open_input",
        "active_response",
        "missing_event",
    ),
)
def test_expired_original_uncertainty_never_reissues_or_cleans_up(
    terminal_stop_factory,
    monkeypatch,
    bad,
):
    s = terminal_stop_factory(expired=True, ingest=True)
    if bad in {"accepted", "unknown", "failed"}:
        s.receipt = StopReceipt.model_validate(
            {**s.receipt.model_dump(), "status": bad, "evidence_sequence": None}
        )
    elif bad == "missing_receipt":

        def missing(*args, **kwargs):
            s.native_reads.append("get_stop")
            raise AgentError("not_found", "Controlled missing original")

        monkeypatch.setattr(s.public, "get_stop", missing)
    elif bad == "missing_event":
        s.native_events = ()
    else:
        changes = {
            "pending_run": {"state": "running"},
            "open_input": {"input_open": True},
            "active_response": {"active_response_id": uuid4()},
        }
        s.snapshot = RunSnapshot.model_validate(
            {**s.snapshot.model_dump(), **changes[bad]}
        )
    before = cleanup_facts(s)
    with pytest.raises((GatewayError, DomainError)):
        s.gateway.handle_stop(s.message)
    assert cleanup_facts(s) == before
    assert s.native_reads[0] == "get_stop"
    assert not s.link_saves and not s.execution_events


@pytest.mark.parametrize("kind", ("fresh_stop", "legacy_stop", "start", "input"))
def test_expired_original_read_does_not_admit_fresh_or_other_actions(
    terminal_stop_factory,
    kind,
):
    s = terminal_stop_factory(expired=True)
    if kind == "fresh_stop":
        s.operation = None
    elif kind == "legacy_stop":
        s.link, s.selected = None, None
    before = cleanup_facts(s)
    with pytest.raises((GatewayError, DomainError)):
        if kind in {"fresh_stop", "legacy_stop"}:
            s.gateway.handle_stop(s.message)
        else:
            s.gateway._load_context_by_ids(
                workspace_id=s.execution.workspace_id,
                execution_id=s.execution.id,
                lease_id=s.lease.id,
                fencing_token=1,
                operation=RuntimeModuleOperation.START
                if kind == "start"
                else RuntimeModuleOperation.INPUT,
            )
    assert cleanup_facts(s) == before and not s.native_reads


@pytest.mark.parametrize("closed", (False, True))
def test_fresh_saved_running_stop_queries_original_and_ingests_real_page(
    terminal_stop_factory,
    closed,
):
    s = terminal_stop_factory(closed=closed, ingest=True)
    s.operation = GatewayOperation.create(
        id=uuid4(),
        operation_id=s.original_operation_id,
        workspace_id=s.authority.workspace_id,
        task_id=s.task.id,
        execution_id=s.execution.id,
        session_id=s.session.id,
        operation_type=GatewayOperationType.STOP_SESSION,
        module_instance_id=s.binding.adapter_module_id,
        config_version=1,
        lease_id=s.lease.id,
        fencing_token=1,
        now=NOW,
    )
    s.operation.start(now=NOW)
    result = s.gateway.handle_stop(s.message)
    assert result.accepted_sequence == 2
    assert s.native_reads == ["get_stop", "get_run", "get_run", "list_events"]
    assert s.original_locks and s.link_saves
    assert s.operation.status is GatewayOperationStatus.SUCCEEDED
    assert s.link.status == "succeeded" and s.link.event_cursor == 2
    assert s.lease.status is LeaseStatus.RELEASED and s.worker.current_load == 0
