from contextlib import contextmanager, nullcontext
from copy import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sidera_agents.contracts import EffectiveConfig, RunSnapshot, StopReceipt

from sidera_control_plane.application.agent_worker_adapter import (
    NativeWorkerServices,
    native_capabilities,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.handoff_coordinator import HandoffCoordinator
from sidera_control_plane.application.profile_handoff import ProfileHandoffPhaseService
from sidera_control_plane.application.worker_gateway import GatewayMessageError
from sidera_control_plane.domain.agent_bindings import (
    AgentBinding,
    AgentExecutionLink,
    AssigneeRef,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.gateway import (
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import HandoffCheckpointEvidence
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransitionStatus,
    HandoffWriterState,
    NativeHandoffProposalPayload,
)
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import ExecutionBinding
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.workspace import ExecutionWorkspaceManifest
from tests.unit.test_execution_authority_provenance import authority as make_authority
from tests.unit.test_handoff_coordinator import _transition, _TransitionRepository
from tests.unit.test_handoff_target import MemoryRepository
from tests.unit.test_native_terminal_stop import preparation

NOW = datetime(2026, 10, 8, tzinfo=UTC)


class DrainRecords(MemoryRepository):
    def get(self, identifier, workspace_id, **kwargs):
        return copy(super().get(identifier, workspace_id, **kwargs))

    get_any = get

    def get_by_operation_id(self, operation_id, workspace_id, **kwargs):
        return next(
            (
                copy(value)
                for value in self.values.values()
                if value.operation_id == operation_id
                and value.workspace_id == workspace_id
            ),
            None,
        )

    def get_for_execution(self, execution_id, workspace_id, **kwargs):
        return next(
            (
                copy(value)
                for value in self.values.values()
                if value.execution_id == execution_id
                and value.workspace_id == workspace_id
            ),
            None,
        )

    def get_active_for_execution(self, execution_id, **kwargs):
        return next(
            (
                copy(value)
                for value in self.values.values()
                if value.execution_id == execution_id and value.status.value == "active"
            ),
            None,
        )

    def save(self, value, previous_version):
        assert self.values[value.id].version == previous_version
        assert value.version > previous_version
        super().save(copy(value), previous_version)


class DrainTransitions(_TransitionRepository):
    def get_active_for_worker_item(self, worker_item_id, workspace_id, **kwargs):
        if (
            self.item is not None
            and self.item.worker_item_id == worker_item_id
            and self.item.workspace_id == workspace_id
            and self.item.status is not HandoffTransitionStatus.CONVERGED
        ):
            return copy(self.item)
        return None


class DrainCommands:
    def __init__(self):
        self.applied = []
        self.rejected = []

    def find_by_id(self, *args):
        return None

    find_duplicate = find_by_id

    def record_applied(self, command, request_hash, result):
        self.applied.append(command)

    def record_rejected(self, command, request_hash, code, details):
        self.rejected.append((command, code))


class OriginalExecution:
    def __init__(self, case):
        self.case = case
        self.calls = []
        self.on_get_stop = lambda: None

    def get_stop(self, workspace_id, run_id, operation_id, *, grant_id):
        assert (workspace_id, run_id, operation_id, grant_id) == (
            self.case.link.workspace_id,
            self.case.link.run_id,
            self.case.operation_id,
            self.case.link.authority_id,
        )
        self.calls.append(("get_stop", operation_id))
        self.on_get_stop()
        return self.case.receipt

    def get_run(self, workspace_id, run_id, *, grant_id):
        assert (workspace_id, run_id, grant_id) == (
            self.case.link.workspace_id,
            self.case.link.run_id,
            self.case.link.authority_id,
        )
        self.calls.append(("get_run", run_id))
        return self.case.snapshot

    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            pytest.fail(f"Drain attempted a new native action: {name}")

        return forbidden


def make_drain_case():
    authority = make_authority()
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=authority.workspace_id,
        adapter_type="controlled-native-drain",
        display_name="Original handoff source",
        capabilities=frozenset({"code"}),
        capacity=1,
        cost_per_hour_microunits=0,
        startup_latency_ms=0,
        now=NOW,
    )
    worker.reserve()
    worker.mark_running()
    worker_item_id, run_id = uuid4(), uuid4()
    execution = Execution.create(
        execution_id=authority.execution_id,
        workspace_id=authority.workspace_id,
        task_id=authority.target_id,
        worker_item_id=worker_item_id,
        lineage_id=uuid4(),
        attempt=1,
        max_attempts=1,
        purpose="code",
        capability_requirements=frozenset({"code"}),
        pinned_worker_id=worker.id,
        not_before=NOW,
    )
    execution.assign(lease_id=authority.lease_id, worker_id=worker.id, fencing_token=1)
    execution.start(lease_id=authority.lease_id, fencing_token=1, now=NOW)
    session = ExecutionSession.start(
        session_id=uuid5(NAMESPACE_URL, f"sidera.native.session:{run_id}"),
        workspace_id=authority.workspace_id,
        execution_id=execution.id,
        worker_id=worker.id,
        adapter_session_id=f"agent-run:{run_id}",
        now=NOW,
    )
    lease = AssignmentLease.issue(
        lease_id=authority.lease_id,
        workspace_id=authority.workspace_id,
        execution_id=execution.id,
        worker_id=worker.id,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        fencing_token=1,
        assignment_reason={"reason": "Owned original handoff"},
    )
    manifest = ExecutionWorkspaceManifest.create(
        id=authority.workspace_ref,
        workspace_id=authority.workspace_id,
        task_id=execution.task_id,
        execution_id=execution.id,
        checkouts=(),
        location_ref="controlled://original",
        retention_seconds=3600,
        fencing_token=1,
        created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    prepared, _ = preparation(authority)
    link = AgentExecutionLink(
        authority.workspace_id,
        execution.id,
        uuid4(),
        authority.profile_ref,
        run_id,
        1,
        "running",
        authority.id,
        1,
        preparation=prepared.model_dump(mode="json"),
    )
    selected = AgentBinding(
        authority.workspace_id,
        authority.target_type,
        authority.target_id,
        AssigneeRef("profile", authority.profile_ref.profile_id),
        authority.profile_ref,
        1,
        NOW,
        NOW,
        runnable=True,
    )
    binding = ExecutionBinding(
        uuid4(),
        authority.workspace_id,
        execution.id,
        worker.id,
        uuid4(),
        uuid4(),
        1,
        1,
        native_capabilities(),
        lease.id,
        1,
        1,
    )
    target_profile = authority.profile_ref.model_copy(update={"profile_id": uuid4()})
    payload = NativeHandoffProposalPayload(
        proposal_schema_version=2,
        workspace_id=authority.workspace_id,
        root_task_id=uuid4(),
        subtask_id=execution.task_id,
        worker_item_id=worker_item_id,
        source_worker_item_version=1,
        source_execution_id=execution.id,
        source_execution_version=execution.version,
        source_session_id=session.id,
        source_session_version=session.version,
        source_session_last_sequence=1,
        source_worker_id=worker.id,
        source_worker_version=worker.version,
        source_workspace_manifest_id=manifest.id,
        source_workspace_manifest_version=manifest.version,
        source_context_package_id=prepared.package_id,
        source_context_package_hash=prepared.package_hash,
        source_worker_context_version=1,
        observed_plan_snapshot_id=uuid4(),
        observed_plan_generation=1,
        observed_plan_head_version=1,
        source_run_id=run_id,
        source_authority_id=authority.id,
        source_binding_version=1,
        source_profile_ref=authority.profile_ref,
        source_binding_target_type=authority.target_type,
        source_binding_target_id=authority.target_id,
        work_context_id=uuid4(),
        work_context_revision_id=prepared.revision_id,
        work_context_hash=prepared.revision_hash,
        work_context_version=1,
        target_assignee=AssigneeRef("profile", target_profile.profile_id),
        target_profile_ref=target_profile,
        handoff_reason="Controlled handoff drain",
        handoff_instruction="Retain the exact original Stop",
        input_plan={},
        impact_summary={"worker_item_context_version": 1},
    )
    transition = replace(
        _transition(),
        workspace_id=authority.workspace_id,
        worker_item_id=worker_item_id,
        source_execution_id=execution.id,
        source_session_id=session.id,
        source_workspace_manifest_id=manifest.id,
        target_worker_id=None,
        target_runtime_snapshot=payload.target_runtime_snapshot,
        handoff_schema_version=2,
        native_payload=payload,
        confirmed_by_id=authority.principal_id,
    )
    transition.start(now=NOW)
    operation_id = handoff_step_operation_id(
        transition.id, HandoffPhase.STOP_SOURCE, logical_attempt=1, action="stop_source"
    )
    checkpoint = HandoffCheckpointEvidence(
        operation_id=handoff_step_operation_id(
            transition.id,
            HandoffPhase.CHECKPOINT_SOURCE,
            logical_attempt=1,
            action="checkpoint",
        ),
        transition_id=transition.id,
        source_session_id=session.id,
        accepted_sequence=1,
        workspace_digest="c" * 64,
        context_package_id=prepared.package_id,
        context_package_hash=prepared.package_hash,
        context_version=1,
        progress_summary_ref=f"execution:{execution.id}",
        artifact_refs=(),
        evidence_refs=(),
        quiescent=False,
        produced_at=NOW,
        schema_version=2,
        workspace_id=authority.workspace_id,
        source_execution_id=execution.id,
        source_run_id=run_id,
        source_authority_id=authority.id,
        source_binding_version=1,
        source_profile_ref=authority.profile_ref,
        observation_kind="cp_run_snapshot",
        expected_stop_operation_id=operation_id,
    )
    transition.record_checkpoint(sequence=1, facts=checkpoint.to_mapping())
    transition.advance(HandoffPhase.STOP_SOURCE, now=NOW)
    operation = GatewayOperation.create(
        id=uuid4(),
        operation_id=operation_id,
        workspace_id=authority.workspace_id,
        task_id=execution.task_id,
        execution_id=execution.id,
        session_id=session.id,
        operation_type=GatewayOperationType.STOP_SESSION,
        module_instance_id=None,
        config_version=None,
        lease_id=lease.id,
        fencing_token=1,
        now=NOW,
    )
    operation.start(now=NOW)
    case = SimpleNamespace(
        authority=authority,
        link=link,
        selected=selected,
        original_selected=selected,
        operation_id=operation_id,
        checkpoint=checkpoint,
        owner=True,
        events=[],
        receipt=StopReceipt(
            operation_id=operation_id,
            run_id=run_id,
            status="unknown",
            reason_code=None,
            evidence_sequence=None,
        ),
        snapshot=RunSnapshot(
            workspace_id=authority.workspace_id,
            run_id=run_id,
            profile_ref=authority.profile_ref,
            policy="bounded",
            state="stopped",
            input_open=False,
            active_response_id=None,
            last_event_sequence=2,
            capabilities={},
            reason_code=None,
            effective_config=EffectiveConfig(
                profile_ref=authority.profile_ref,
                config_hash="controlled",
                runtime_ref=authority.runtime_ref,
                actual_runtime_version="controlled",
                selected_model=None,
                observed_model=None,
                inherited_fields=(),
                applied_fields=(),
                resource_refs=(),
                context_receipts=(),
            ),
        ),
    )
    case.uow = SimpleNamespace(
        executions=DrainRecords((execution,)),
        sessions=DrainRecords((session,)),
        leases=DrainRecords((lease,)),
        workers=DrainRecords((worker,)),
        bindings=DrainRecords((binding,)),
        workspaces=DrainRecords((manifest,)),
        gateway_operations=DrainRecords((operation,)),
        handoff_transitions=DrainTransitions(transition),
        commands=DrainCommands(),
        events=SimpleNamespace(append_with_outbox=case.events.append),
        agent_bindings=SimpleNamespace(
            get=lambda *args, **kwargs: case.selected,
            history=lambda *args, **kwargs: (case.original_selected,),
        ),
        execution_authorities=SimpleNamespace(
            get=lambda identifier, **kwargs: (
                case.authority if identifier == case.authority.id else None
            ),
            get_link=lambda workspace_id, execution_id, **kwargs: (
                case.link
                if (workspace_id, execution_id)
                == (case.link.workspace_id, case.link.execution_id)
                else None
            ),
        ),
        planning_authorities=SimpleNamespace(
            list_reconciliation_links=lambda *a, **k: ()
        ),
        savepoint=lambda: nullcontext(),
    )

    def reconciliation_links(*args, **kwargs):
        current = case.uow.executions.values[case.link.execution_id]
        return (
            (case.link,)
            if (
                case.link.status not in {"succeeded", "failed", "stopped", "cancelled"}
                or current.status.value in {"leased", "running"}
            )
            else ()
        )

    case.uow.execution_authorities.list_reconciliation_links = reconciliation_links

    @contextmanager
    def uow_factory():
        records = [
            value
            for value in vars(case.uow).values()
            if isinstance(value, DrainRecords)
        ]
        before = [
            (record, dict(record.values), list(record.saved), list(record.added))
            for record in records
        ]
        previous_transition = copy(case.uow.handoff_transitions.item)
        try:
            yield case.uow
        except BaseException:
            for record, values, saved, added in before:
                record.values, record.saved, record.added = values, saved, added
            case.uow.handoff_transitions.item = previous_transition
            raise

    case.uow_factory = uow_factory
    case.public = OriginalExecution(case)
    case.services = NativeWorkerServices(
        case.public, case.public, case.public, None, authority.principal_id, uuid4()
    )
    case.service = ProfileHandoffPhaseService(
        uow_factory,
        native_services=case.services,
        clock=lambda: NOW,
        owner_check=lambda: case.owner,
    )
    case.coordinator = HandoffCoordinator(
        uow_factory=uow_factory,
        native_handoff=case.service,
        clock=lambda: NOW,
        owner_id="owned-drain",
        lease_ttl_seconds=30,
    )
    return case


@pytest.fixture
def drain_case():
    return make_drain_case()


def source_resources(case):
    return tuple(
        tuple(
            (
                row.id,
                row.version,
                getattr(row, "status", None),
                getattr(row, "current_load", None),
            )
            for row in repository.values.values()
        )
        for repository in (
            case.uow.executions,
            case.uow.sessions,
            case.uow.leases,
            case.uow.workers,
        )
    )


def known_stop(case):
    case.receipt = StopReceipt(
        operation_id=case.operation_id,
        run_id=case.link.run_id,
        status="stopped",
        reason_code=None,
        evidence_sequence=2,
    )


def test_query_only_missing_intention_cannot_create_or_send_a_stop(drain_case):
    s = drain_case
    s.uow.gateway_operations.values.clear()
    transition = s.uow.handoff_transitions.claim_next(
        owner_id="owned-drain",
        now=NOW,
        limit=1,
        statuses=(HandoffTransitionStatus.RUNNING,),
        lease_ttl_seconds=30,
    )[0]
    before = source_resources(s)
    assert s.service.stop(transition, query_only=True) is None
    assert not s.uow.gateway_operations.values and not s.public.calls
    assert source_resources(s) == before and not s.events


def test_query_only_cannot_substitute_another_persisted_stop_intention(drain_case):
    s = drain_case
    alternate_id = uuid4()
    original = next(iter(s.uow.gateway_operations.values.values()))
    alternate = replace(original, id=uuid4(), operation_id=alternate_id)
    s.uow.gateway_operations.values[alternate.id] = alternate
    current = s.uow.handoff_transitions.item
    current.source_checkpoint_facts = replace(
        s.checkpoint, expected_stop_operation_id=alternate_id
    ).to_mapping()
    claimed = s.uow.handoff_transitions.claim_next(
        owner_id="owned-drain",
        now=NOW,
        limit=1,
        statuses=(HandoffTransitionStatus.RUNNING,),
        lease_ttl_seconds=30,
    )[0]
    before = source_resources(s)
    with pytest.raises(GatewayMessageError):
        s.service.stop(claimed, query_only=True)
    assert source_resources(s) == before and not s.public.calls
    assert not s.events and not s.uow.commands.applied


def test_drain_unknown_observes_only_original_stop_without_advancing(drain_case):
    s = drain_case
    before = source_resources(s)
    s.uow.handoff_transitions.item.next_attempt_at = NOW + timedelta(minutes=1)
    assert s.coordinator.drain_original_source_stop(s.link) is False
    assert s.public.calls == [("get_stop", s.operation_id)]
    assert source_resources(s) == before
    current = s.uow.handoff_transitions.item
    assert current.phase is HandoffPhase.STOP_SOURCE
    assert current.status is HandoffTransitionStatus.RUNNING
    assert current.writer_state is HandoffWriterState.SOURCE_STOP_REQUESTED
    assert current.source_stop_facts is None and current.lease_owner is None
    assert current.next_attempt_at is None
    assert not s.events and not s.uow.commands.applied and not s.uow.commands.rejected


def test_drain_known_stop_releases_once_and_restart_consumes_saved_proof(drain_case):
    s = drain_case
    assert s.coordinator.drain_original_source_stop(s.link) is False
    known_stop(s)
    selected, payload = s.selected, s.uow.handoff_transitions.item.native_payload
    assert s.coordinator.drain_original_source_stop(s.link) is True
    assert {row.status.value for row in s.uow.executions.values.values()} == {
        "canceled"
    }
    assert {row.status.value for row in s.uow.sessions.values.values()} == {"closed"}
    assert {row.status.value for row in s.uow.leases.values.values()} == {"released"}
    assert [row.current_load for row in s.uow.workers.values.values()] == [0]
    assert {row.status for row in s.uow.gateway_operations.values.values()} == {
        GatewayOperationStatus.SUCCEEDED
    }
    saved_proof = s.uow.handoff_transitions.item.source_stop_facts
    released = source_resources(s)
    audit = tuple(s.events), tuple(s.uow.commands.applied)
    calls = tuple(s.public.calls)
    assert calls == (
        ("get_stop", s.operation_id),
        ("get_stop", s.operation_id),
        ("get_run", s.link.run_id),
    )
    assert s.coordinator.drain_original_source_stop(s.link) is True
    assert (
        source_resources(s) == released
        and s.uow.handoff_transitions.item.source_stop_facts == saved_proof
    )
    assert (tuple(s.events), tuple(s.uow.commands.applied)) == audit
    assert tuple(s.public.calls) == calls
    current = s.uow.handoff_transitions.item
    assert (
        current.phase is HandoffPhase.STOP_SOURCE
        and current.target_execution_id is None
    )
    assert current.target_session_id is None and current.target_worker_id is None
    assert current.native_payload == payload and s.selected == selected
    assert len(s.uow.executions.values) == len(s.uow.sessions.values) == 1
    assert [event.event_type for event in s.events] == [
        "ExecutionCanceledByHandoff",
        "ExecutionSessionClosedByHandoff",
        "LeaseReleasedByHandoff",
    ]
    restarted = HandoffCoordinator(
        uow_factory=s.uow_factory,
        native_handoff=s.service,
        owner_id="after-restart",
        clock=lambda: NOW,
    )
    assert restarted.run_once(limit=1).progressed == 1
    assert s.uow.handoff_transitions.item.phase is HandoffPhase.FREEZE_SOURCE_WORKSPACE
    assert tuple(s.public.calls) == calls
    assert [event.event_type for event in s.events].count("HandoffSourceStopped") == 1


@pytest.mark.parametrize(
    "pin",
    [
        "run",
        "authority",
        "profile",
        "binding",
        "session",
        "worker_item",
        "workspace",
        "revoked",
        "expired",
        "superseded",
        "checkpoint",
        "operation",
    ],
)
def test_drain_changed_original_pins_cannot_mutate_or_call_native(drain_case, pin):
    s = drain_case
    current = s.uow.handoff_transitions.item
    if pin in {"run", "authority", "profile", "binding"}:
        field = {
            "run": "run_id",
            "authority": "authority_id",
            "profile": "profile_ref",
            "binding": "binding_version",
        }[pin]
        value = (
            2
            if pin == "binding"
            else s.link.profile_ref.model_copy(update={"profile_id": uuid4()})
            if pin == "profile"
            else uuid4()
        )
        s.link = replace(s.link, **{field: value})
    elif pin == "session":
        session = next(iter(s.uow.sessions.values.values()))
        session.adapter_session_id = f"agent-run:{uuid4()}"
    elif pin == "worker_item":
        s.uow.executions.values[s.link.execution_id].worker_item_id = uuid4()
    elif pin == "workspace":
        s.uow.executions.values[s.link.execution_id].workspace_id = uuid4()
    elif pin == "revoked":
        s.authority = replace(s.authority, revoked=True)
    elif pin == "expired":
        next(iter(s.uow.leases.values.values())).expires_at = NOW
    elif pin == "superseded":
        s.uow.executions.values[s.link.execution_id].active_lease_id = uuid4()
    elif pin == "checkpoint":
        current.source_checkpoint_facts = replace(
            s.checkpoint, expected_stop_operation_id=uuid4()
        ).to_mapping()
    elif pin == "operation":
        next(iter(s.uow.gateway_operations.values.values())).session_id = uuid4()
    known_stop(s)
    before = source_resources(s)
    try:
        assert s.coordinator.drain_original_source_stop(s.link) is False
    except (DomainError, GatewayMessageError) as exc:
        assert exc.code in {
            "native_handoff_source_changed",
            "native_handoff_source_stop_invalid",
            "native_handoff_operation_changed",
        }
    assert source_resources(s) == before and not s.public.calls
    assert s.uow.handoff_transitions.item.source_stop_facts is None and not s.events
    assert not s.uow.commands.applied and not s.uow.commands.rejected


@pytest.mark.parametrize("phase", list(HandoffPhase))
def test_drain_never_dispatches_other_handoff_phases(drain_case, phase):
    s = drain_case
    s.uow.handoff_transitions.item.phase = phase
    if phase is HandoffPhase.STOP_SOURCE:
        s.coordinator._native_handoff = None
    before = source_resources(s)
    assert s.coordinator.drain_original_source_stop(s.link) is False
    assert source_resources(s) == before and not s.public.calls and not s.events


@pytest.mark.parametrize(
    "kind",
    [
        "legacy",
        "blocked",
        "blocked_unknown",
        "failed",
        "missing_run",
        "missing_worker_item",
        "stale_caller",
    ],
)
def test_drain_ineligible_original_retains_state_without_fallback(drain_case, kind):
    s = drain_case
    current = s.uow.handoff_transitions.item
    expected = s.link
    if kind == "legacy":
        current.handoff_schema_version, current.native_payload = 1, None
    elif kind == "missing_run":
        s.link = replace(s.link, run_id=None)
        expected = s.link
    elif kind == "missing_worker_item":
        s.uow.executions.values[s.link.execution_id].worker_item_id = None
    elif kind == "stale_caller":
        expected = replace(s.link, start_operation_id=uuid4())
    else:
        current.status = HandoffTransitionStatus(kind)
        current.failure_code = "retained_failure"
    before = source_resources(s), copy(current)
    assert s.coordinator.drain_original_source_stop(expected) is False
    assert (source_resources(s), s.uow.handoff_transitions.item) == before
    assert not s.public.calls and not s.events


@pytest.mark.parametrize("owner", ["another-tick", "owned-drain"])
def test_drain_cannot_steal_any_unexpired_claim(drain_case, owner):
    s = drain_case
    current = s.uow.handoff_transitions.item
    current.lease_owner, current.lease_expires_at = owner, NOW + timedelta(seconds=10)
    before = copy(current)
    assert s.coordinator.drain_original_source_stop(s.link) is False
    assert s.uow.handoff_transitions.item == before and not s.public.calls


@pytest.mark.parametrize(
    "replacement", ["different_owner", "same_owner_new_expiry", "new_attempt"]
)
def test_drain_lost_claim_during_get_cannot_release_replacement_or_resources(
    drain_case, replacement
):
    s = drain_case
    known_stop(s)
    replacements = []

    def replace_claim():
        current = copy(s.uow.handoff_transitions.item)
        if replacement == "different_owner":
            current.lease_owner = "new-owner"
        elif replacement == "same_owner_new_expiry":
            current.lease_expires_at += timedelta(seconds=1)
        else:
            current.logical_attempt += 1
        current.version += 1
        s.uow.handoff_transitions.item = current
        replacements.append(copy(current))

    s.public.on_get_stop = replace_claim
    before = source_resources(s)
    with pytest.raises(GatewayMessageError) as lost:
        s.coordinator.drain_original_source_stop(s.link)
    assert lost.value.code == "native_handoff_claim_changed"
    assert s.uow.handoff_transitions.item == replacements[0]
    assert source_resources(s) == before and not s.events and not s.uow.commands.applied


def test_public_ordinary_stop_still_rejects_active_handoff(drain_case):
    s = drain_case
    execution = s.uow.executions.values[s.link.execution_id]
    command = CommandEnvelope(
        id=uuid4(),
        workspace_id=s.link.workspace_id,
        command_type="StopExecution",
        target_type="execution",
        target_id=execution.id,
        actor_type="user",
        actor_id="operator",
        payload={},
        expected_version=execution.version,
        idempotency_key="public-ordinary-stop",
        correlation_id=uuid4(),
    )
    with pytest.raises(DomainError) as blocked:
        CommandService(s.uow_factory, (ExecutionCommandHandler(lambda: NOW),)).handle(
            command
        )
    assert blocked.value.code == "handoff_in_progress"
    assert s.uow.commands.rejected == [(command, "handoff_in_progress")]
    assert not s.uow.commands.applied and not s.events and not s.public.calls
