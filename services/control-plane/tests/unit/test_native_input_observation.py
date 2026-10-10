from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.execution_commands import (
    ExecutionInputCommandHandler,
)
from sidera_control_plane.application.ports import WorkerItemPlacement
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import DomainError, ExecutionContextChanged
from sidera_control_plane.domain.execution import (
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.inputs import ExecutionInput
from sidera_control_plane.domain.worker_item import WorkerItem
from tests.unit.test_native_handoff_drain import NOW, DrainRecords, make_drain_case
from tests.unit.test_native_terminal_stop import preparation


class _Sessions(DrainRecords):
    def __init__(self, values):
        super().__init__(values)
        self.on_lock = lambda: None
        self.locked_observations = []

    def get_any(self, identifier, workspace_id, *, for_update=False):
        if for_update:
            self.on_lock()
        value = self.get(identifier, workspace_id)
        if for_update and value is not None:
            self.locked_observations.append((value.version, value.last_sequence))
        return value


class _Inputs(DrainRecords):
    def find_by_client_idempotency_key(
        self, session_id, workspace_id, client_key, **kwargs
    ):
        return next(
            (
                value
                for value in self.values.values()
                if value.session_id == session_id
                and value.workspace_id == workspace_id
                and value.client_idempotency_key == client_key
            ),
            None,
        )

    def get_pending_for_session(self, session_id, workspace_id, **kwargs):
        return next(
            (
                value
                for value in self.values.values()
                if value.session_id == session_id
                and value.workspace_id == workspace_id
                and value.status.value
                in {"accepted", "queued", "dispatching", "blocked"}
            ),
            None,
        )


def _case():
    case = make_drain_case()
    uow = case.uow
    execution = uow.executions.values[case.link.execution_id]
    case.authority = replace(
        case.authority,
        target_type="worker_item",
        target_id=execution.worker_item_id,
        actions=(*case.authority.actions, "run.input"),
        upstream_grant_id=uuid4(),
    )
    case.selected = replace(
        case.selected, target_type="worker_item", target_id=execution.worker_item_id
    )
    case.original_selected = case.selected
    prepared, saved_command = preparation(case.authority)
    case.link = replace(case.link, preparation=prepared.model_dump(mode="json"))
    uow.commands.find_by_id = lambda workspace_id, command_id: (
        saved_command
        if (workspace_id, command_id) == (saved_command.workspace_id, saved_command.id)
        else None
    )
    case.transition = uow.handoff_transitions.item
    uow.handoff_transitions.item = None
    uow.execution_authorities.prepared_location_ref = lambda *_args: (
        "controlled://original"
    )
    session = next(iter(uow.sessions.values.values()))
    session.version = 2
    session.input_mode = "queued"
    for sequence in range(1, 4):
        session.record_activity(sequence=sequence, now=NOW)
    uow.sessions = _Sessions((session,))
    worker_item = WorkerItem.create(
        item_id=execution.worker_item_id,
        workspace_id=execution.workspace_id,
        root_task_id=uuid4(),
        title="Original native worker",
        responsibility="Accept the original input",
        now=NOW,
    )
    case.placement = WorkerItemPlacement(
        plan_snapshot_id=uuid4(),
        plan_generation=1,
        workspace_id=execution.workspace_id,
        root_task_id=worker_item.root_task_id,
        worker_item_id=worker_item.id,
        worker_item_key=worker_item.stable_key,
        subtask_key="native-task",
        subtask_task_id=execution.task_id,
        role="implementer",
        capability_requirements=("code",),
        worker_policy_ref=None,
        configuration_ref=None,
        created_at=NOW,
    )
    uow.worker_items = DrainRecords((worker_item,))
    uow.worker_items.get_current_placement = lambda *_args: case.placement
    uow.plans = SimpleNamespace(
        get_current_for_task=lambda *_args: None,
        get_head=lambda *_args, **_kwargs: None,
    )
    case.terminal_operation = None
    uow.results = SimpleNamespace(
        get_head=lambda *_args, **_kwargs: None,
        get_operation=lambda *_args, **_kwargs: case.terminal_operation,
    )
    case.host_calls = []

    def require_host(grant_id, action, target):
        assert grant_id == case.authority.upstream_grant_id
        assert action == "run.input"
        assert target.workspace_id == execution.workspace_id
        assert target.target_type == "worker_item"
        assert target.target_id == worker_item.id
        assert target.principal_id == case.authority.principal_id
        case.host_calls.append((grant_id, action, target))

    case.on_get_run = lambda: None

    def get_run(workspace_id, run_id, *, grant_id):
        assert (workspace_id, run_id, grant_id) == (
            case.link.workspace_id,
            case.link.run_id,
            case.link.authority_id,
        )
        case.public.calls.append(("get_run", run_id))
        case.on_get_run()
        return case.snapshot

    case.public.get_run = get_run
    case.snapshot = case.snapshot.model_copy(
        update={"state": type(case.snapshot.state)("idle"), "input_open": True}
    )
    case.services = replace(
        case.services, authorizer=SimpleNamespace(require=require_host)
    )
    case.identity = ControlPlaneIdentity(
        case.authority.principal_id, case.authority.upstream_grant_id, "user"
    )
    case.handler = ExecutionInputCommandHandler(
        lambda: NOW, identity=case.identity, native_services=case.services
    )
    uow.execution_inputs = _Inputs(())
    case.envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=execution.workspace_id,
        command_type="SendExecutionInput",
        target_type="execution_session",
        actor_type="user",
        actor_id=case.identity.principal_id,
        target_id=session.id,
        expected_version=5,
        idempotency_key="original-command",
        correlation_id=uuid4(),
        payload={
            "context": {
                "worker_item_id": str(worker_item.id),
                "plan_generation": 1,
                "context_version": 1,
                "execution_id": str(execution.id),
                "session_id": str(session.id),
                "session_version": 5,
                "session_last_sequence": 3,
                "worker_id": str(session.worker_id),
                "execution_mode": "native_profile",
                "binding_version": case.link.binding_version,
                "run_id": str(case.link.run_id),
            },
            "client_idempotency_key": "original-client-key",
            "text": "  preserve original input bytes  ",
            "attachment_refs": [],
        },
    )
    return case


def _advance(case, sequence):
    session = case.uow.sessions.values[case.envelope.target_id]
    while session.last_sequence < sequence:
        session.record_activity(sequence=session.last_sequence + 1, now=NOW)


def _assert_accepted(case, original):
    original_hash = hash_request(case.envelope)
    result = case.handler.handle(case.uow, case.envelope)
    assert asdict(case.envelope) == original
    assert hash_request(case.envelope) == original_hash
    assert len(case.uow.execution_inputs.added) == 1
    item = case.uow.execution_inputs.added[0]
    assert item.id == result.aggregate_id
    assert item.status.value == "queued"
    assert item.text == "  preserve original input bytes  "
    assert item.client_idempotency_key == "original-client-key"
    assert item.session_id == case.envelope.target_id
    assert item.execution_id == case.link.execution_id
    assert item.source_command_id == case.envelope.id
    assert len(case.events) == 1
    assert case.events[0].event_type == "ExecutionInputAccepted"
    recorded = case.events[0].payload["execution_context"]
    assert recorded["session_version"] == "5"
    if original["payload"]["context"].get("session_last_sequence") is not None:
        assert recorded["session_last_sequence"] == "3"
    else:
        assert "session_last_sequence" not in recorded
    assert result.event_ids == (case.events[0].id,)
    assert case.public.calls == [("get_run", case.link.run_id)]
    assert case.host_calls


def test_native_send_exact_context_uses_real_authority_guards():
    case = _case()
    _assert_accepted(case, deepcopy(asdict(case.envelope)))


def test_native_send_accepts_trace_progress_before_admission_and_locked_send():
    case = _case()
    original = deepcopy(asdict(case.envelope))
    _advance(case, 6)
    assert (case.uow.sessions.values[case.envelope.target_id].version, 6) == (8, 6)
    case.on_get_run = lambda: _advance(case, 8)

    _assert_accepted(case, original)

    assert case.uow.sessions.locked_observations == [(10, 8)]


def _assert_rejected(case, *, changed_field=None, expected_error=DomainError):
    original = deepcopy(asdict(case.envelope))
    original_hash = hash_request(case.envelope)
    inputs = dict(case.uow.execution_inputs.values)
    with pytest.raises(expected_error) as error:
        case.handler.handle(case.uow, case.envelope)
    if changed_field is not None:
        assert isinstance(error.value, ExecutionContextChanged)
        assert changed_field in error.value.details["changed_fields"]
    assert asdict(case.envelope) == original
    assert hash_request(case.envelope) == original_hash
    assert case.uow.execution_inputs.values == inputs
    assert case.uow.execution_inputs.added == []
    assert case.events == []
    assert case.uow.gateway_operations.added == []
    assert case.uow.commands.applied == []
    assert case.uow.commands.rejected == []
    assert all(name == "get_run" for name, _ in case.public.calls)


@pytest.mark.parametrize("boundary", ["admission", "locked_send"])
@pytest.mark.parametrize(
    "version,sequence",
    [(6, 3), (5, 4), (4, 2), (5, -1), (6, None), (True, 0), (6, 6)],
)
def test_native_send_rejects_unproved_movement_at_both_boundaries(
    boundary, version, sequence
):
    case = _case()

    def change_session():
        session = case.uow.sessions.values[case.envelope.target_id]
        session.version, session.last_sequence = version, sequence

    if boundary == "admission":
        change_session()
    else:
        case.uow.sessions.on_lock = change_session

    _assert_rejected(case, changed_field="session_version")
    if boundary == "locked_send":
        assert case.uow.sessions.locked_observations == [(version, sequence)]


@pytest.mark.parametrize("proof", ["missing", None, True, -1, 5, 3.0, "3", {}])
def test_native_send_missing_null_or_invalid_proof_never_allows_stale_context(proof):
    case = _case()
    if proof == "missing":
        case.envelope.payload["context"].pop("session_last_sequence")
    else:
        case.envelope.payload["context"]["session_last_sequence"] = proof
    _advance(case, 6)
    _assert_rejected(case)
    assert case.public.calls == []


@pytest.mark.parametrize("proof", ["missing", None])
def test_native_send_old_context_accepts_only_exact_session_version(proof):
    case = _case()
    if proof == "missing":
        case.envelope.payload["context"].pop("session_last_sequence")
    else:
        case.envelope.payload["context"]["session_last_sequence"] = proof
    _assert_accepted(case, deepcopy(asdict(case.envelope)))


@pytest.mark.parametrize("current_sequence,expected_version", [(3, 6), (6, 8)])
def test_native_send_proof_cannot_replace_original_envelope_version(
    current_sequence, expected_version
):
    case = _case()
    _advance(case, current_sequence)
    case.envelope = replace(case.envelope, expected_version=expected_version)
    _assert_rejected(case)
    assert case.public.calls == []


@pytest.mark.parametrize(
    "change",
    [
        "run",
        "session",
        "execution",
        "worker",
        "worker_item",
        "workspace",
        "principal",
        "binding",
        "runtime_binding",
        "context",
        "generation",
        "closed_session",
        "closed_run",
        "closed_execution",
        "revoked_authority",
        "profile",
        "expired_lease",
        "fence",
        "active_handoff",
        "terminal_capture",
        "pending_input",
    ],
)
def test_native_send_trace_proof_preserves_existing_admission_guards(change):
    case = _case()
    _advance(case, 6)
    uow = case.uow
    session = uow.sessions.values[case.envelope.target_id]
    execution = uow.executions.values[case.link.execution_id]
    worker_item = uow.worker_items.values[execution.worker_item_id]
    if change == "run":
        case.link = replace(case.link, run_id=uuid4())
    elif change == "session":
        session.id = uuid4()
    elif change == "execution":
        session.execution_id = uuid4()
    elif change == "worker":
        session.worker_id = uuid4()
    elif change == "worker_item":
        case.envelope.payload["context"]["worker_item_id"] = str(uuid4())
    elif change == "workspace":
        case.envelope = replace(case.envelope, workspace_id=uuid4())
    elif change == "principal":
        case.envelope = replace(case.envelope, actor_id="another-principal")
    elif change == "binding":
        case.selected = replace(case.selected, version=2)
    elif change == "runtime_binding":
        binding = next(iter(uow.bindings.values.values()))
        uow.bindings.values[binding.id] = replace(binding, worker_id=uuid4())
    elif change == "context":
        worker_item.advance_context(now=NOW)
    elif change == "generation":
        case.placement = replace(case.placement, plan_generation=2)
    elif change == "closed_session":
        session.close(lost=False, now=NOW)
    elif change == "closed_run":
        case.snapshot = case.snapshot.model_copy(
            update={
                "state": type(case.snapshot.state)("completed"),
                "input_open": False,
            }
        )
    elif change == "closed_execution":
        execution.status = ExecutionStatus.SUCCEEDED
    elif change == "revoked_authority":
        case.authority = replace(case.authority, revoked=True)
    elif change == "profile":
        case.selected = replace(
            case.selected,
            profile_ref=case.selected.profile_ref.model_copy(update={"revision": 2}),
        )
    elif change == "expired_lease":
        uow.leases.values[case.authority.lease_id].expires_at = NOW
    elif change == "fence":
        execution.last_fencing_token = 2
    elif change == "active_handoff":
        uow.handoff_transitions.item = case.transition
    elif change == "terminal_capture":
        case.terminal_operation = SimpleNamespace(payload={"terminal_observed": True})
    else:
        pending = ExecutionInput.accept(
            id=uuid4(),
            workspace_id=execution.workspace_id,
            execution_id=execution.id,
            session_id=session.id,
            client_idempotency_key="earlier-input",
            text="Earlier pending input",
            attachment_refs=(),
            now=NOW,
        )
        pending.queue()
        uow.execution_inputs.values[pending.id] = pending

    _assert_rejected(
        case, expected_error=NotFound if change == "workspace" else DomainError
    )


@pytest.mark.parametrize("change", ["closed_session", "handoff", "terminal", "context"])
def test_native_send_revalidates_control_changes_after_admission(change):
    case = _case()
    _advance(case, 6)

    def change_locked():
        if change == "closed_session":
            case.uow.sessions.values[
                case.envelope.target_id
            ].status = ExecutionSessionStatus.CLOSED
        elif change == "handoff":
            case.uow.handoff_transitions.item = case.transition
        elif change == "terminal":
            case.terminal_operation = SimpleNamespace(
                payload={"terminal_observed": True}
            )
        else:
            case.uow.worker_items.values[case.placement.worker_item_id].advance_context(
                now=NOW
            )

    if change == "context":
        case.on_get_run = change_locked
    else:
        case.uow.sessions.on_lock = change_locked
    _assert_rejected(case)
    assert case.uow.sessions.locked_observations == [(8, 6)]


def test_legacy_send_retains_exact_context_version_check():
    case = _case()
    context = case.envelope.payload["context"]
    for key in ("execution_mode", "binding_version", "run_id", "session_last_sequence"):
        context.pop(key)
    case.uow.execution_authorities.get_link = lambda *_args: None
    _advance(case, 6)
    _assert_rejected(case, changed_field="session_version")
    assert case.public.calls == []


@pytest.mark.parametrize("proof", ["missing", None])
def test_native_send_old_context_rejects_trace_progress_before_locked_send(proof):
    case = _case()
    if proof == "missing":
        case.envelope.payload["context"].pop("session_last_sequence")
    else:
        case.envelope.payload["context"]["session_last_sequence"] = proof
    case.on_get_run = lambda: _advance(case, 6)
    _assert_rejected(case, changed_field="session_version")
    assert case.uow.sessions.locked_observations == [(8, 6)]


@pytest.mark.parametrize(
    "command_type",
    [
        "UpdatePendingExecutionInput",
        "CancelExecutionInput",
        "RetryBlockedExecutionInput",
    ],
)
def test_pending_native_input_commands_keep_exact_session_version(command_type):
    case = _case()
    item = ExecutionInput.accept(
        id=uuid4(),
        workspace_id=case.envelope.workspace_id,
        execution_id=case.link.execution_id,
        session_id=case.envelope.target_id,
        client_idempotency_key="original-client-key",
        text="Pending input",
        attachment_refs=(),
        now=NOW,
    )
    item.queue()
    case.uow.execution_inputs.values[item.id] = item
    case.envelope = replace(
        case.envelope,
        command_type=command_type,
        payload={
            "execution_id": str(case.link.execution_id),
            "client_idempotency_key": item.client_idempotency_key,
            **(
                {"text": "Updated input"}
                if command_type == "UpdatePendingExecutionInput"
                else {}
            ),
        },
    )
    _advance(case, 6)
    _assert_rejected(case)
    assert item.text == "Pending input"
    assert item.status.value == "queued"
