from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError
from sidera_agents.contracts import (
    EffectiveConfig,
    RunSnapshot,
    StartLookup,
    StartReceipt,
)
from sidera_agents.errors import AgentError

from sidera_control_plane.api.schemas import CommandRequest
from sidera_control_plane.application.agent_worker_adapter import NativeWorkerServices
from sidera_control_plane.application.assignments import (
    AssignmentTarget,
    TrustedAssignmentIdentity,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
)
from sidera_control_plane.domain.agent_bindings import AgentExecutionLink
from sidera_control_plane.domain.agent_tools import WorkerProgressTarget
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.errors import ValidationError as DomainValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.persistence.models import EventModel
from tests.unit.test_command_service import FakeCommandRepository
from tests.unit.test_task_tool_scope import worker_scope_fixture


def progress_fixture():
    factory, _, catalog, authority, connection, _, _, _, root, operation = (
        worker_scope_fixture()
    )
    uow = factory().__enter__()
    state = SimpleNamespace(
        uow=uow,
        authority=authority,
        connection=connection,
        root=root,
        now=datetime.now(UTC),
        grant=uuid4(),
        denied=False,
        lookups=[],
        native_reads=[],
        events=[],
        outboxes=[],
        keys=set(),
        locks=[],
    )
    state.execution = uow.executions.get(authority.execution_id, authority.workspace_id)
    state.item = uow.worker_items.get(
        state.execution.worker_item_id, authority.workspace_id
    )
    state.lease = uow.leases.get(authority.lease_id, authority.workspace_id)
    state.binding = uow.agent_bindings.get(
        authority.workspace_id, "worker_item", authority.target_id
    )
    state.manifest = uow.workspaces.get(authority.workspace_ref, authority.workspace_id)
    state.run = RunSnapshot(
        run_id=uuid4(),
        workspace_id=authority.workspace_id,
        profile_ref=authority.profile_ref,
        policy="bounded",
        state="running",
        input_open=True,
        active_response_id=uuid4(),
        last_event_sequence=11,
        effective_config=EffectiveConfig(
            profile_ref=authority.profile_ref,
            config_hash="owned-pin",
            runtime_ref=authority.runtime_ref,
            actual_runtime_version="controlled",
            selected_model=None,
            observed_model=None,
            inherited_fields=(),
            applied_fields=(),
            resource_refs=authority.resources,
            context_receipts=(),
        ),
        capabilities={},
        reason_code=None,
    )
    state.link = AgentExecutionLink(
        workspace_id=authority.workspace_id,
        execution_id=authority.execution_id,
        start_operation_id=operation,
        profile_ref=authority.profile_ref,
        run_id=None,
        event_cursor=11,
        status="starting",
        authority_id=authority.id,
        binding_version=1,
    )
    state.lookup = StartLookup(
        found=True,
        receipt=StartReceipt(
            operation_id=operation,
            status="accepted",
            run_id=state.run.run_id,
            reason_code=None,
        ),
    )
    state.original_run, state.original_response = (
        state.run.run_id,
        state.run.active_response_id,
    )

    def require(grant, action, target):
        if grant == state.grant and action == "progress.report":
            expected = WorkerProgressTarget(
                authority.workspace_id,
                authority.principal_id,
                authority.execution_id,
                state.item.id,
                authority.id,
                connection,
                state.execution.version,
                authority.binding_version,
                authority.lease_id,
                authority.fencing_token,
                state.original_run,
                state.original_response,
            )
            if state.denied or target != expected:
                raise AgentError(
                    "permission_denied", "Controlled current authority denied"
                )
            return
        if (
            grant != authority.upstream_grant_id
            or action not in authority.actions
            or not isinstance(target, AssignmentTarget)
            or target
            != AssignmentTarget(
                authority.workspace_id,
                "worker_item",
                state.item.id,
                authority.principal_id,
            )
        ):
            raise AgentError("permission_denied", "Controlled parent denied")

    host = SimpleNamespace(require=require)
    uow.execution_authorities.get = lambda supplied, **kwargs: (
        state.authority if supplied == state.authority.id else None
    )
    uow.execution_authorities.get_link = lambda *args, **kwargs: state.link
    uow.executions.get = lambda *args, **kwargs: state.execution
    uow.worker_items.get = lambda *args, **kwargs: state.item
    uow.leases.get = lambda *args, **kwargs: state.lease
    uow.agent_bindings.get = lambda *args, **kwargs: state.binding
    uow.workspaces.get = lambda *args, **kwargs: state.manifest
    uow.handoff_transitions = SimpleNamespace(get=lambda *args: None)

    def lock_intention(expected, *, now, allow_observation_advance=False):
        assert (
            expected == state.link
            and now == state.now
            and allow_observation_advance is True
        )
        state.locks.append(expected)
        return state.execution, state.lease, state.link, state.authority

    uow.execution_authorities.lock_intention = lock_intention
    reader = ExecutionAuthorityReader(
        lambda: nullcontext(uow),
        principal=lambda: authority.principal_id,
        authorizer=host,
        clock=lambda: state.now,
    )

    def get_start(workspace_id, supplied, *, grant_id):
        assert (workspace_id, supplied, grant_id) == (
            authority.workspace_id,
            operation,
            authority.id,
        )
        reader.require(grant_id, "run.read", workspace_id)
        state.native_reads.append("get_start")
        return state.lookup

    def get_run(workspace_id, supplied, *, grant_id):
        assert (workspace_id, supplied, grant_id) == (
            authority.workspace_id,
            state.lookup.receipt.run_id,
            authority.id,
        )
        reader.require(grant_id, "run.read", workspace_id)
        state.native_reads.append("get_run")
        return state.run

    def forbidden_start(*args, **kwargs):
        pytest.fail("A progress report attempted another native Start")

    state.services = NativeWorkerServices(
        catalog=catalog,
        runtimes=SimpleNamespace(),
        execution=SimpleNamespace(
            get_start=get_start, get_run=get_run, start_run=forbidden_start
        ),
        authorizer=host,
        principal_id=authority.principal_id,
        catalog_grant_id=authority.upstream_grant_id,
    )
    state.host = host
    state.identity = TrustedAssignmentIdentity(
        authority.principal_id, state.grant, "agent"
    )

    class Commands(FakeCommandRepository):
        def find_by_id(self, workspace_id, command_id):
            state.lookups.append(command_id)
            return super().find_by_id(workspace_id, command_id)

    uow.commands = Commands()
    uow.savepoint = nullcontext

    def append(event):
        key = (event.aggregate_type, event.aggregate_id, event.aggregate_version)
        assert key not in state.keys, (
            "Progress collided with the actual event aggregate uniqueness key"
        )
        state.keys.add(key)
        state.events.append(event)
        state.outboxes.append(event.id)

    uow.events = SimpleNamespace(append_with_outbox=append)
    state.payload = {
        "worker_item_id": str(state.item.id),
        "authority_id": str(authority.id),
        "connection_id": str(connection),
        "binding_version": 1,
        "lease_id": str(authority.lease_id),
        "fencing_token": 1,
        "run_id": str(state.run.run_id),
        "response_id": str(state.run.active_response_id),
        "summary": "Inspected the input",
        "steps": [{"step": "read", "finished": False, "detail": [1, None]}],
    }
    state.envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=authority.workspace_id,
        command_type="ReportWorkerProgress",
        target_type="execution",
        actor_type="agent",
        actor_id=authority.principal_id,
        target_id=authority.execution_id,
        expected_version=7,
        payload=state.payload,
        idempotency_key=str(uuid4()),
        correlation_id=state.run.run_id,
    )
    return state


def progress_service(state):
    module = import_module("sidera_control_plane.application.worker_progress")
    handler = module.WorkerProgressCommandHandler(
        lambda: state.now,
        identity=state.identity,
        authorizer=state.host,
        native_services=state.services,
    )
    return CommandService(
        lambda: nullcontext(state.uow), (handler,), preflight=handler.preflight
    )


def test_progress_reports_are_independent_facts_without_lifecycle_or_cursor_changes():
    state = progress_fixture()
    lifecycle = EventData(
        id=uuid4(),
        workspace_id=state.authority.workspace_id,
        aggregate_type="execution",
        aggregate_id=state.execution.id,
        aggregate_version=7,
        event_type="ExecutionStarted",
        payload={"execution_id": str(state.execution.id)},
        actor_type="agent",
        actor_id=state.authority.principal_id,
        causation_id=uuid4(),
        correlation_id=state.root,
    )
    state.uow.events.append_with_outbox(lifecycle)
    initial_execution, initial_item, initial_lease = (
        vars(state.execution).copy(),
        vars(state.item).copy(),
        vars(state.lease).copy(),
    )
    original_link, original_run = state.link, state.run
    service = progress_service(state)
    first = service.handle(state.envelope)
    second_envelope = replace(
        state.envelope,
        id=uuid4(),
        idempotency_key=str(uuid4()),
        payload={**state.payload, "summary": "Reviewed the next input"},
    )
    second = service.handle(second_envelope)
    replay = service.handle(state.envelope)
    reports = state.events[1:]
    assert first == replay and len(reports) == 2 and len(state.outboxes) == 3
    assert first.aggregate_id == second.aggregate_id == state.execution.id
    assert first.aggregate_version == second.aggregate_version == 7
    assert first.event_ids == (UUID(first.data["report_id"]),)
    assert second.event_ids == (UUID(second.data["report_id"]),)
    assert first.event_ids != second.event_ids
    for event, command in zip(reports, (state.envelope, second_envelope), strict=True):
        assert (event.id, event.aggregate_id, event.aggregate_version) == (
            event.id,
            event.id,
            1,
        )
        assert (
            event.aggregate_type == "worker_progress_report"
            and event.event_type == "WorkerProgressReported"
        )
        assert event.payload["execution_id"] == str(state.execution.id)
        assert (
            event.payload["execution_version"] == 7
            and event.payload["binding_version"] == 1
        )
        assert event.payload["root_task_id"] == str(state.root)
        assert event.payload["summary"] == command.payload["summary"]
        assert (
            event.payload["steps"] == state.payload["steps"]
            and event.payload["verified"] is False
        )
        assert event.payload["run_id"] == str(original_run.run_id)
        assert event.payload["response_id"] == str(original_run.active_response_id)
        assert datetime.fromisoformat(event.payload["reported_at"]) == state.now
        assert event.causation_id == command.id
    unique = next(
        constraint
        for constraint in EventModel.__table__.constraints
        if constraint.name == "uq_event_aggregate_version"
    )
    assert tuple(column.name for column in unique.columns) == (
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
    )
    assert len(state.keys) == 3
    assert (
        vars(state.execution) == initial_execution
        and vars(state.item) == initial_item
        and vars(state.lease) == initial_lease
    )
    assert state.link == original_link and state.run == original_run
    assert state.locks and set(state.native_reads) == {"get_start", "get_run"}


@pytest.mark.parametrize(
    "committed,status",
    ((False, "leased"), (False, "running"), (True, "leased"), (True, "running")),
)
def test_progress_uses_only_the_original_accepted_run_across_cp_link_commit(
    committed, status
):
    state = progress_fixture()
    state.link = replace(state.link, run_id=state.run.run_id if committed else None)
    state.execution.status = SimpleNamespace(value=status)
    result = progress_service(state).handle(state.envelope)
    assert result.aggregate_version == 7 and len(state.events) == 1
    assert state.events[0].payload["run_id"] == str(state.run.run_id)
    assert state.link.run_id == (state.run.run_id if committed else None)


def command_document(state):
    value = state.envelope
    return {
        "command_id": str(value.id),
        "workspace_id": str(value.workspace_id),
        "command_type": value.command_type,
        "target_type": value.target_type,
        "target_id": str(value.target_id),
        "expected_version": value.expected_version,
        "actor": {"type": value.actor_type, "id": value.actor_id},
        "idempotency_key": value.idempotency_key,
        "correlation_id": str(value.correlation_id),
        "payload": dict(value.payload),
    }


@pytest.mark.parametrize(
    "invalid",
    (
        "summary_empty",
        "summary_long",
        "too_many_steps",
        "byte_limit",
        "nonfinite",
        "extra",
        "missing",
        "binding_bool",
        "fence_zero",
        "version_bool",
        "target",
    ),
)
def test_progress_schema_rejects_nonexact_or_oversized_report_data(invalid):
    document = command_document(progress_fixture())
    payload = document["payload"]
    if invalid == "summary_empty":
        payload["summary"] = "   "
    elif invalid == "summary_long":
        payload["summary"] = "x" * 4001
    elif invalid == "too_many_steps":
        payload["steps"] = [{}] * 65
    elif invalid == "byte_limit":
        payload["steps"] = [{"data": "x" * 32768}]
    elif invalid == "nonfinite":
        payload["steps"] = [{"number": float("nan")}]
    elif invalid == "extra":
        payload["completed"] = True
    elif invalid == "missing":
        del payload["authority_id"]
    elif invalid == "binding_bool":
        payload["binding_version"] = True
    elif invalid == "fence_zero":
        payload["fencing_token"] = 0
    elif invalid == "version_bool":
        document["expected_version"] = True
    else:
        document["target_type"] = "task"
    with pytest.raises(PydanticValidationError):
        CommandRequest.model_validate(document)


@pytest.mark.parametrize(
    "invalid",
    (
        "user",
        "actor",
        "identity",
        "services",
        "host",
        "workspace",
        "version",
        "authority",
        "item",
        "connection",
        "binding_pin",
        "lease_pin",
        "fence_pin",
        "run_pin",
        "response_pin",
        "retired",
        "stopped",
        "terminal",
        "binding_changed",
        "binding_disabled",
        "lease_expired",
        "lease_fence",
        "revoked",
        "manifest",
        "handoff",
        "unknown_start",
        "absent_start",
        "start_operation",
        "start_run",
        "linked_run",
        "run_workspace",
        "run_profile",
        "idle",
        "response_closed",
        "response_changed",
    ),
)
def test_progress_denies_noncurrent_business_or_native_authority_before_saved_receipt(
    invalid,
):
    state = progress_fixture()
    service = progress_service(state)
    service.handle(state.envelope)
    state.lookups.clear()
    state.native_reads.clear()
    if invalid == "user":
        state.envelope = replace(state.envelope, actor_type="user")
    elif invalid == "actor":
        state.envelope = replace(state.envelope, actor_id="forged")
    elif invalid == "identity":
        state.identity = None
    elif invalid == "services":
        state.services = None
    elif invalid == "host":
        state.denied = True
    elif invalid == "workspace":
        state.envelope = replace(state.envelope, workspace_id=uuid4())
    elif invalid == "version":
        state.execution.version += 1
    elif invalid in {
        "authority",
        "item",
        "connection",
        "lease_pin",
        "run_pin",
        "response_pin",
    }:
        key = {
            "authority": "authority_id",
            "item": "worker_item_id",
            "connection": "connection_id",
            "lease_pin": "lease_id",
            "run_pin": "run_id",
            "response_pin": "response_id",
        }[invalid]
        state.envelope = replace(
            state.envelope, payload={**state.payload, key: str(uuid4())}
        )
    elif invalid in {"binding_pin", "fence_pin"}:
        key = "binding_version" if invalid == "binding_pin" else "fencing_token"
        state.envelope = replace(state.envelope, payload={**state.payload, key: 2})
    elif invalid == "retired":
        state.item.status = SimpleNamespace(value="retired")
    elif invalid == "stopped":
        state.execution.stop_requested = True
    elif invalid == "terminal":
        state.execution.status = SimpleNamespace(value="succeeded")
    elif invalid == "binding_changed":
        state.binding.version = 2
    elif invalid == "binding_disabled":
        state.binding.runnable = False
    elif invalid == "lease_expired":
        state.lease.expires_at = state.now
    elif invalid == "lease_fence":
        state.lease.fencing_token = 2
    elif invalid == "revoked":
        state.authority = replace(state.authority, revoked=True)
    elif invalid == "manifest":
        state.manifest = None
    elif invalid == "handoff":
        state.execution.handoff_transition_id = uuid4()
    elif invalid == "unknown_start":
        state.lookup = StartLookup(
            found=True,
            receipt=state.lookup.receipt.model_copy(update={"status": "unknown"}),
        )
    elif invalid == "absent_start":
        state.lookup = StartLookup(found=False, receipt=None)
    elif invalid == "start_operation":
        state.lookup = StartLookup(
            found=True,
            receipt=state.lookup.receipt.model_copy(update={"operation_id": uuid4()}),
        )
    elif invalid == "start_run":
        state.lookup = StartLookup(
            found=True,
            receipt=state.lookup.receipt.model_copy(update={"run_id": uuid4()}),
        )
    elif invalid == "linked_run":
        state.link = replace(state.link, run_id=uuid4())
    elif invalid == "run_workspace":
        state.run = state.run.model_copy(update={"workspace_id": uuid4()})
    elif invalid == "run_profile":
        state.run = state.run.model_copy(
            update={
                "profile_ref": state.run.profile_ref.model_copy(update={"revision": 2})
            }
        )
    elif invalid == "idle":
        state.run = RunSnapshot.model_validate(
            {**state.run.model_dump(mode="json"), "state": "idle"}
        )
    elif invalid == "response_closed":
        state.run = state.run.model_copy(update={"active_response_id": None})
    else:
        state.run = state.run.model_copy(update={"active_response_id": uuid4()})
    with pytest.raises((DomainError, AgentError)):
        progress_service(state).handle(state.envelope)
    assert state.lookups == [] and len(state.events) == len(state.outboxes) == 1


def test_progress_concurrent_winner_is_not_disclosed_after_authority_revocation():
    from sidera_control_plane.persistence.repositories import ConcurrentCommandCommit

    state = progress_fixture()
    service = progress_service(state)
    result = service.handle(state.envelope)
    original_lookup = state.uow.commands.find_by_id
    state.lookups.clear()

    def concurrent_lookup(*args):
        state.denied = True
        raise ConcurrentCommandCommit("uq_commands_idempotency")

    state.uow.commands.find_by_id = concurrent_lookup
    with pytest.raises((DomainError, AgentError)):
        service.handle(state.envelope)
    state.uow.commands.find_by_id = original_lookup
    assert result.event_ids == (state.events[0].id,)
    assert state.lookups == [] and len(state.events) == len(state.outboxes) == 1


def real_lock_fixture(monkeypatch):
    module = import_module(
        "sidera_control_plane.persistence.execution_authority_repository"
    )
    bindings = import_module(
        "sidera_control_plane.persistence.agent_binding_repository"
    )
    state = progress_fixture()
    state.lock_order, state.observed_link = [], None
    worker_id = uuid4()
    state.execution.assigned_worker_id = worker_id
    state.lease = AssignmentLease.issue(
        lease_id=state.authority.lease_id,
        workspace_id=state.authority.workspace_id,
        execution_id=state.authority.execution_id,
        worker_id=worker_id,
        issued_at=state.now,
        expires_at=state.now + timedelta(minutes=1),
        fencing_token=1,
        assignment_reason={"reason": "owned progress fixture"},
    )
    state.link = replace(
        state.link, run_id=state.original_run, status="running", request_sha256="1" * 64
    )

    def aggregate_read(statement):
        state.lock_order.append("aggregate")
        return state.item

    repository = module.ExecutionAuthorityRepository(
        SimpleNamespace(scalar=aggregate_read)
    )
    repository.get = lambda supplied, **kwargs: (
        state.authority if supplied == state.authority.id else None
    )
    repository.prepared_location_ref = lambda *args: state.manifest.location_ref

    def execution_read(self, execution_id, workspace_id, *, for_update=False):
        assert (execution_id, workspace_id) == (
            state.execution.id,
            state.authority.workspace_id,
        )
        if for_update:
            state.lock_order.append("execution")
            if state.observed_link is not None:
                state.link, state.observed_link = state.observed_link, None
        return state.execution

    def lease_read(self, lease_id, workspace_id, *, for_update=False):
        assert (lease_id, workspace_id) == (
            state.authority.lease_id,
            state.authority.workspace_id,
        )
        if for_update:
            state.lock_order.append("lease")
        return state.lease

    def link_read(workspace_id, execution_id, *, for_update=False):
        assert (workspace_id, execution_id) == (
            state.authority.workspace_id,
            state.execution.id,
        )
        if for_update:
            state.lock_order.append("link")
        return state.link

    def binding_read(self, workspace_id, target_type, target_id, *, for_update=False):
        assert (workspace_id, target_type, target_id) == (
            state.authority.workspace_id,
            "worker_item",
            state.item.id,
        )
        if for_update:
            state.lock_order.append("binding")
        return state.binding

    repository.get_link = link_read
    monkeypatch.setattr(module.ExecutionRepository, "get", execution_read)
    monkeypatch.setattr(module.LeaseStore, "get", lease_read)
    monkeypatch.setattr(bindings.AgentBindingRepository, "get", binding_read)
    state.repository = state.uow.execution_authorities = repository
    return state


@pytest.mark.parametrize(
    "change", ("cursor", "waiting", "accepted_commit", "receipt_commit")
)
def test_real_progress_lock_accepts_only_monotonic_original_observation(
    change, monkeypatch
):
    state = real_lock_fixture(monkeypatch)
    expected = state.link
    if change == "accepted_commit":
        expected = replace(expected, run_id=None, event_cursor=0, status="starting")
        state.observed_link = replace(
            expected, run_id=state.original_run, start_receipt=state.lookup.receipt
        )
    elif change == "receipt_commit":
        state.observed_link = replace(expected, start_receipt=state.lookup.receipt)
    else:
        state.observed_link = replace(
            expected,
            event_cursor=12,
            status="waiting" if change == "waiting" else "running",
        )
    result = state.repository.lock_intention(
        expected, now=state.now, allow_observation_advance=True
    )
    assert result == (state.execution, state.lease, state.link, state.authority)
    assert result[2] is state.link and state.link != expected
    assert state.lock_order == ["aggregate", "execution", "lease", "link", "binding"]
    assert state.events == state.outboxes == []


def test_real_lock_default_exact_mode_still_denies_observation_advance(monkeypatch):
    state = real_lock_fixture(monkeypatch)
    expected = state.link
    state.observed_link = replace(expected, event_cursor=12)
    with pytest.raises(DomainError) as error:
        state.repository.lock_intention(expected, now=state.now)
    assert error.value.code == "assignment_forbidden"
    assert state.lock_order == ["aggregate", "execution", "lease", "link", "binding"]


@pytest.mark.parametrize(
    "change",
    (
        "run",
        "profile",
        "start",
        "authority",
        "binding",
        "workspace",
        "execution",
        "request_hash",
        "preparation",
        "cursor_regression",
        "closing",
        "unknown",
        "terminal",
        "accepted_receipt_changed",
        "run_without_receipt",
        "receipt_wrong_run",
        "receipt_wrong_operation",
        "receipt_unknown",
        "receipt_without_run",
        "missing_link",
        "revoked",
        "lease_expired",
        "fence_changed",
    ),
)
def test_real_progress_lock_preserves_immutable_pins_and_current_fences(
    change, monkeypatch
):
    state = real_lock_fixture(monkeypatch)
    expected = state.link
    current = replace(expected, event_cursor=12)
    if change in {"run", "start", "authority", "execution"}:
        key = {
            "run": "run_id",
            "start": "start_operation_id",
            "authority": "authority_id",
            "execution": "execution_id",
        }[change]
        current = replace(current, **{key: uuid4()})
    elif change == "workspace":
        foreign = uuid4()
        current = replace(
            current,
            workspace_id=foreign,
            profile_ref=current.profile_ref.model_copy(
                update={"workspace_id": foreign}
            ),
        )
    elif change == "profile":
        current = replace(
            current, profile_ref=current.profile_ref.model_copy(update={"revision": 2})
        )
    elif change == "binding":
        current = replace(current, binding_version=2)
    elif change == "request_hash":
        current = replace(current, request_sha256="2" * 64)
    elif change == "preparation":
        current = replace(current, preparation={"changed": True})
    elif change == "cursor_regression":
        current = replace(current, event_cursor=10)
    elif change in {"closing", "unknown", "terminal"}:
        current = replace(
            current, status="succeeded" if change == "terminal" else change
        )
    elif change == "accepted_receipt_changed":
        expected = replace(expected, start_receipt=state.lookup.receipt)
        current = replace(
            current,
            start_receipt=state.lookup.receipt.model_copy(
                update={"reason_code": "changed"}
            ),
        )
    elif change in {
        "run_without_receipt",
        "receipt_wrong_run",
        "receipt_wrong_operation",
        "receipt_unknown",
        "receipt_without_run",
    }:
        expected = replace(expected, run_id=None, event_cursor=0, status="starting")
        receipt = state.lookup.receipt
        if change == "receipt_wrong_run":
            receipt = receipt.model_copy(update={"run_id": uuid4()})
        elif change == "receipt_wrong_operation":
            receipt = receipt.model_copy(update={"operation_id": uuid4()})
            with pytest.raises(
                DomainValidationError, match="native admission operation mismatch"
            ):
                replace(expected, run_id=state.original_run, start_receipt=receipt)
            assert state.lock_order == []
            assert state.events == state.outboxes == []
            return
        elif change == "receipt_unknown":
            receipt = StartReceipt(
                operation_id=expected.start_operation_id,
                status="unknown",
                run_id=None,
                reason_code=None,
            )
        current = replace(
            expected,
            run_id=None if change == "receipt_without_run" else state.original_run,
            start_receipt=None if change == "run_without_receipt" else receipt,
        )
    elif change == "missing_link":
        state.link = None
    elif change == "revoked":
        state.authority = replace(state.authority, revoked=True)
    elif change == "lease_expired":
        state.lease.expires_at = state.now
    else:
        state.lease.fencing_token = 2
    state.observed_link = None if change == "missing_link" else current
    with pytest.raises(DomainError):
        state.repository.lock_intention(
            expected, now=state.now, allow_observation_advance=True
        )
    assert state.lock_order == ["aggregate", "execution", "lease", "link", "binding"]
    assert state.events == state.outboxes == []


def test_progress_handler_opts_into_real_lock_observation_comparison(monkeypatch):
    state = real_lock_fixture(monkeypatch)
    state.observed_link = replace(state.link, event_cursor=12, status="waiting")
    original_run = state.run
    result = progress_service(state).handle(state.envelope)
    assert result.aggregate_version == 7 and len(state.events) == 1
    assert state.link.event_cursor == 12 and state.link.status == "waiting"
    assert state.run == original_run and state.execution.version == 7
    assert state.lock_order[:5] == [
        "aggregate",
        "execution",
        "lease",
        "link",
        "binding",
    ]
