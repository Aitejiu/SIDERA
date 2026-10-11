from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from sidera_agents.contracts import InputReceipt
from sidera_agents.errors import AgentError
from sqlalchemy import select

from sidera_control_plane.api import dependencies
from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.agent_execution import TaskExecutionInputBuilder
from sidera_control_plane.application.assignments import TrustedAssignmentIdentity
from sidera_control_plane.application.code_result_capture import (
    capture_operation_for_execution,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.handoff_input_transfer import (
    HandoffInputTransferService,
)
from sidera_control_plane.application.input_dispatcher import InputDispatcher
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.commands import CommandResult
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.handoff_evidence import HandoffInputAction
from sidera_control_plane.domain.inputs import input_operation_id
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.persistence.execution_authority_repository import (
    ExecutionAuthorityRepository,
)
from sidera_control_plane.persistence.models import EventModel
from sidera_control_plane.persistence.repositories import EventRepository
from tests.integration.test_agent_dispatch import NativeScenario
from tests.support.native_agents import (
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)

pytestmark = pytest.mark.integration


class PublicInputControl:
    """Control public Run receipts while the original response waits at a barrier."""

    def __init__(self, scenario):
        self.s = scenario
        self.sent, self.queried, self.receipts = [], [], {}
        self.release = Event()
        self.active = Event()
        self.response_id = uuid4()
        self.threads = []
        self.query_timeout = False
        self.lose_admission_ack = False
        self.admission_receipt_override = None
        self.admission_agent_error = False
        original_start = scenario.execution.start
        original_get_run = scenario.execution.get_run

        def start(request):
            receipt = original_start(request)
            if receipt.run_id is not None:
                scenario.execution.emit(receipt.run_id, "response.started")

                def original_turn():
                    self.active.set()
                    self.release.wait()
                    scenario.execution.emit(receipt.run_id, "response.completed")

                thread = Thread(target=original_turn)
                self.threads.append(thread)
                thread.start()
                assert self.active.wait(3)
            return receipt

        def get_run(*args, **kwargs):
            snapshot = original_get_run(*args, **kwargs)
            return snapshot.model_copy(
                update={
                    "active_response_id": None
                    if self.release.is_set()
                    else self.response_id
                }
            )

        scenario.execution.start = start
        scenario.execution.get_run = get_run
        scenario.execution.send_input = self.send_input
        scenario.execution.get_input = self.get_input

    def send_input(self, request):
        self.sent.append(request)
        receipt = InputReceipt(
            operation_id=request.operation_id,
            run_id=request.run_id,
            status="accepted",
            reason_code=None,
            evidence_sequence=None,
        )
        self.receipts[request.operation_id] = receipt
        if self.lose_admission_ack:
            raise TimeoutError("controlled public admission ACK loss")
        if self.admission_agent_error:
            raise AgentError(
                "native_reply_unavailable", "controlled post-admission exception"
            )
        if self.admission_receipt_override is not None:
            return InputReceipt.model_validate(
                {**receipt.model_dump(), **self.admission_receipt_override}
            )
        return receipt

    def get_input(self, workspace_id, run_id, operation_id, *, grant_id):
        self.queried.append((workspace_id, run_id, operation_id, grant_id))
        if self.query_timeout:
            raise TimeoutError("controlled public input query timeout")
        return self.receipts[operation_id]

    def delivered(self, operation_id, *, sequence=7):
        receipt = self.receipts[operation_id]
        self.receipts[operation_id] = InputReceipt.model_validate(
            {
                **receipt.model_dump(),
                "status": "delivered",
                "evidence_sequence": sequence,
            }
        )

    def complete_original_turn(self):
        self.release.set()
        for thread in self.threads:
            thread.join(3)
            assert not thread.is_alive()


class PublicStopControl:
    def __init__(self, scenario):
        self.s = scenario
        self.sent, self.queried, self.receipts = [], [], {}
        self.lose_ack = False
        scenario.native.execution.stop = self.stop
        scenario.native.execution.get_stop = self.get_stop

    def stop(self, request):
        from sidera_agents.contracts import StopReceipt

        from sidera_control_plane.domain.gateway import GatewayOperationType

        with self.s.native.uow() as uow:
            original = uow.gateway_operations.get_by_operation_id(
                request.operation_id, self.s.native.workspace_id
            )
        assert original is not None and original.status.value == "running"
        assert original.execution_id == self.s.execution.id
        assert original.operation_type is GatewayOperationType.STOP_SESSION
        self.sent.append(request)
        receipt = StopReceipt(
            operation_id=request.operation_id,
            run_id=request.run_id,
            status="accepted",
            reason_code=None,
            evidence_sequence=None,
        )
        self.receipts[request.operation_id] = receipt
        if self.lose_ack:
            raise TimeoutError("controlled public stop ACK loss")
        return receipt

    def get_stop(self, workspace_id, run_id, operation_id, *, grant_id):
        self.queried.append((workspace_id, run_id, operation_id, grant_id))
        return self.receipts[operation_id]

    def close_original(self):
        from sidera_agents.contracts import StopReceipt

        self.s.public.complete_original_turn()
        self.s.native.execution.emit(self.s.link.run_id, "run.stopped", state="stopped")
        request = self.sent[0]
        self.receipts[request.operation_id] = StopReceipt.model_validate(
            {
                **self.receipts[request.operation_id].model_dump(),
                "status": "stopped",
                "evidence_sequence": len(
                    self.s.native.execution.events[request.run_id]
                ),
            }
        )


def native_handoff_worker(s, monkeypatch):
    import inspect

    from sidera_control_plane import handoff_worker
    from sidera_control_plane.gateway_worker import GatewayRuntime

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return s.native.cp.now

    monkeypatch.setattr(handoff_worker, "datetime", FrozenDateTime)
    assert (
        "native_services"
        in inspect.signature(handoff_worker.build_handoff_worker).parameters
    ), "existing handoff factory must consume supplied native ports"
    return handoff_worker.build_handoff_worker(
        Settings(
            workspace_root=s.native.manager._workspace_root,
            local_actor_id=s.native.principal,
        ),
        gateway_runtime=GatewayRuntime(
            s.native.gateway,
            s.native.gateway,
            s.native.cp.database,
            s.native.catalog,
            workspace_provider=s.native.manager,
        ),
        native_services=s.native.services,
    )


class InterventionScenario:
    def __init__(
        self,
        database,
        workspace_id,
        tmp_path,
        monkeypatch,
        *,
        native=None,
        public=None,
        key="a",
    ):
        self.native = s = native or NativeScenario(database, workspace_id, tmp_path)
        self.public = public or PublicInputControl(s)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return s.cp.now

        monkeypatch.setattr(dependencies, "datetime", FrozenDateTime)
        self.commands = dependencies.get_command_service(
            database,
            Settings(local_actor_id=s.principal),
            assignment_identity=TrustedAssignmentIdentity(s.principal, s.setup_grant),
            assignment_authorizer=s.assignment._authorizer,
            agent_catalog=s.catalog,
            agent_runtimes=s.runtimes,
            native_services=s.services,
        )
        self.dispatcher = InputDispatcher(s.uow, s.gateway, clock=s.cp.clock)
        if native is not None:
            s.cp._install_capture_runtime(s.cp._execution_id(key), None)
        s.assign(key)
        assert s.schedule(key).assigned == 1
        self.started = s.start(key)
        self.link = s.link(key)
        with s.uow() as uow:
            self.execution = uow.executions.get(self.link.execution_id, workspace_id)
            self.item = uow.worker_items.get(
                self.execution.worker_item_id, workspace_id
            )
            self.session = uow.sessions.get(self.started.session_id, workspace_id)
            self.placement = uow.worker_items.get_current_placement(
                self.item.id, workspace_id
            )

    def command(
        self, *, text="Preserve the tests", client_key="input-1", attachments=()
    ):
        s = self.native
        return s.command(
            "SendExecutionInput",
            "execution_session",
            self.session.id,
            self.session.version,
            {
                "context": {
                    "worker_item_id": str(self.item.id),
                    "plan_generation": self.placement.plan_generation,
                    "context_version": self.item.context_version,
                    "execution_id": str(self.execution.id),
                    "session_id": str(self.session.id),
                    "session_version": self.session.version,
                    "worker_id": str(self.session.worker_id),
                    "execution_mode": "native_profile",
                    "binding_version": self.link.binding_version,
                    "run_id": str(self.link.run_id),
                },
                "client_idempotency_key": client_key,
                "text": text,
                "attachment_refs": list(attachments),
            },
        )

    def input(self, input_id):
        with self.native.uow() as uow:
            return uow.execution_inputs.get(input_id, self.native.workspace_id)

    def facts(self, input_id):
        s = self.native
        with s.uow() as uow:
            item = uow.execution_inputs.get(input_id, s.workspace_id)
            operation = uow.gateway_operations.get_by_operation_id(
                input_operation_id(input_id), s.workspace_id
            )
            events = tuple(
                uow.session.scalars(
                    select(EventModel).where(
                        EventModel.workspace_id == s.workspace_id,
                        EventModel.aggregate_id == input_id,
                        EventModel.event_type == "ExecutionInputDelivered",
                    )
                )
            )
            placement = uow.worker_items.get_current_placement(
                self.item.id, s.workspace_id
            )
            return item, operation, events, placement


@pytest.fixture
def intervention(database, workspace_id, tmp_path, monkeypatch):
    scenario = InterventionScenario(database, workspace_id, tmp_path, monkeypatch)
    try:
        yield scenario
    finally:
        scenario.public.complete_original_turn()


@pytest.fixture
def additional_interventions(
    intervention, database, workspace_id, tmp_path, monkeypatch
):
    scenarios = [intervention]

    def create():
        scenario = InterventionScenario(
            database,
            workspace_id,
            tmp_path,
            monkeypatch,
            native=intervention.native,
            public=intervention.public,
            key=("a", "b", "c")[len(scenarios)],
        )
        scenarios.append(scenario)
        return scenario

    try:
        yield create
    finally:
        for scenario in scenarios[1:]:
            scenario.public.complete_original_turn()


def test_actual_input_command_admission_then_query_delivers_once(intervention):
    s = intervention
    command = s.command(text="  Preserve the tests  ")
    accepted = s.commands.handle(command)
    original = s.input(accepted.aggregate_id)
    assert original.text == "  Preserve the tests  "
    assert original.execution_id == s.execution.id
    assert original.session_id == s.session.id
    assert s.public.active.is_set() and not s.public.release.is_set()

    first = s.dispatcher.run_once(batch_size=1)
    pending = s.input(original.id)
    assert len(s.public.sent) == 1, "the real native gateway must admit user input"
    assert first.delivered == first.failed == 0
    assert pending.status.value == "dispatching"
    assert pending.operation_id == input_operation_id(original.id)
    request = s.public.sent[0]
    assert (request.workspace_id, request.run_id, request.grant_id) == (
        s.native.workspace_id,
        s.link.run_id,
        s.link.authority_id,
    )
    assert request.text == original.text
    assert s.dispatcher.run_once(batch_size=1).delivered == 0
    assert len(s.public.sent) == len(s.public.queried) == 1
    assert s.input(original.id).version == pending.version
    s.public.complete_original_turn()
    s.native.gateway.sync_active_sessions(s.native.workspace_id)
    assert s.input(original.id).status.value == "dispatching"
    s.public.delivered(pending.operation_id)
    assert s.dispatcher.run_once(batch_size=1).delivered == 1
    assert s.dispatcher.run_once(batch_size=1).delivered == 0
    assert len(s.public.sent) == 1
    item, operation, events, placement = s.facts(original.id)
    assert item.status.value == "delivered"
    assert operation.status.value == "succeeded"
    assert len(events) == 1 and events[0].payload["accepted_sequence"] == 7
    assert events[0].payload["run_id"] == str(s.link.run_id)
    assert events[0].payload["binding_version"] == s.link.binding_version
    assert placement.plan_generation == s.placement.plan_generation
    assert item.execution_id == s.execution.id
    assert len(s.native.execution.starts) == 1


@pytest.mark.parametrize("case", ["query_timeout", "admission_ack_lost"])
def test_unknown_input_reconciles_original_action_without_resend(intervention, case):
    s = intervention
    s.public.lose_admission_ack = case == "admission_ack_lost"
    accepted = s.commands.handle(s.command())
    s.dispatcher.run_once(batch_size=1)
    pending = s.input(accepted.aggregate_id)
    assert len(s.public.sent) == 1
    assert pending.status.value == "dispatching"
    s.public.query_timeout = True
    s.dispatcher.run_once(batch_size=1)
    assert s.input(pending.id).operation_id == pending.operation_id
    assert len(s.public.sent) == 1 and len(s.public.queried) == 1
    uncertain_version = s.input(pending.id).version
    s.dispatcher.run_once(batch_size=1)
    assert s.input(pending.id).version == uncertain_version
    s.public.query_timeout = False
    s.public.delivered(pending.operation_id)
    assert s.dispatcher.run_once(batch_size=1).delivered == 1
    assert len(s.public.sent) == 1


@pytest.mark.parametrize("port", ["authority", "session"])
def test_unavailable_query_identity_retains_original_pending_native_input(
    intervention, monkeypatch, port
):
    s = intervention
    accepted = s.commands.handle(s.command())
    assert s.dispatcher.run_once(batch_size=1).accepted == 1
    pending = s.input(accepted.aggregate_id)
    repository = ExecutionAuthorityRepository
    method = "get"
    if port == "session":
        from sidera_control_plane.persistence.session_repository import (
            SessionRepository,
        )

        repository = SessionRepository
        method = "get_any"

    with monkeypatch.context() as unavailable:
        original_get = getattr(repository, method)

        def missing_original(self, entity_id, workspace_id=None, **kwargs):
            original_id = s.link.authority_id if port == "authority" else s.session.id
            if entity_id == original_id:
                return None
            if port == "authority":
                return original_get(
                    self, entity_id, workspace_id=workspace_id, **kwargs
                )
            return original_get(self, entity_id, workspace_id, **kwargs)

        unavailable.setattr(repository, method, missing_original)
        assert s.dispatcher.run_once(batch_size=1).delivered == 0
        uncertain, operation, events, _ = s.facts(pending.id)
        assert uncertain.status.value == "dispatching"
        assert uncertain.operation_id == pending.operation_id
        assert operation.status.value == "running" and events == ()
        uncertain_version = uncertain.version
        assert s.dispatcher.run_once(batch_size=1).delivered == 0
        assert s.input(pending.id).version == uncertain_version
        assert s.public.queried == []

    s.public.delivered(pending.operation_id)
    assert s.dispatcher.run_once(batch_size=1).delivered == 1
    final, operation, events, _ = s.facts(pending.id)
    assert (
        final.status.value == "delivered" and final.operation_id == pending.operation_id
    )
    assert operation.status.value == "succeeded" and len(events) == 1
    assert len(s.public.sent) == len(s.public.queried) == 1
    assert len(s.native.execution.starts) == 1


def test_native_input_forged_actor_is_denied_before_command_receipt(intervention):
    s = intervention
    command = s.command()
    s.commands.handle(command)
    with pytest.raises(DomainError) as denied:
        s.commands.handle(replace(command, actor_id="forged"))
    assert denied.value.code == "assignment_forbidden"
    assert s.public.sent == []


def native_prepare_command(s, target="profile"):
    target_profile = s.native.profile.ref.model_copy(update={"profile_id": uuid4()})
    s.native.catalog.profiles[target_profile] = s.native.profile.model_copy(
        update={"ref": target_profile}
    )
    return s.native.command(
        "PrepareProfileHandoff",
        "worker_item",
        s.item.id,
        s.item.version,
        {
            "proposal_schema_version": 2,
            "context": s.command().payload["context"],
            "target_assignee": {
                "kind": target,
                "id": str(target_profile.profile_id)
                if target == "profile"
                else s.native.principal,
            },
            "target_profile_ref": target_profile.model_dump(mode="json")
            if target == "profile"
            else None,
            "handoff_reason": "Explicit local reassignment",
            "handoff_instruction": "Preserve the authorized input history",
            "input_plan": {},
            "proposal_ttl_seconds": 900,
        },
    )


def native_confirm_command(s, proposal):
    return s.native.command(
        "ConfirmProfileHandoff",
        "handoff_proposal",
        proposal.id,
        proposal.version,
        {
            "proposal_id": str(proposal.id),
            "expected_proposal_version": proposal.version,
            "expected_proposal_hash": proposal.proposal_hash,
            "expected_worker_item_version": s.item.version,
        },
    )


@pytest.mark.parametrize("target", ["profile", "human"])
def test_native_handoff_prepare_pins_assignee_without_stopping_source(
    intervention, target
):
    s = intervention
    command = native_prepare_command(s, target)
    target_profile = (
        None
        if target == "human"
        else (
            s.native.profile.ref.model_validate(command.payload["target_profile_ref"])
        )
    )
    result = s.commands.handle(command)
    with s.native.uow() as uow:
        proposal = uow.handoff_proposals.get(result.aggregate_id, s.native.workspace_id)
        assert proposal.status.value == "prepared"
        assert proposal.payload.proposal_schema_version == 2
        assert proposal.payload.target_assignee.kind == target
        assert proposal.payload.target_worker_id is None
        assert proposal.payload.source_run_id == s.link.run_id
        assert proposal.payload.source_binding_version == s.link.binding_version
        assert proposal.payload.target_profile_ref == (
            target_profile if target == "profile" else None
        )
        assert (
            uow.handoff_transitions.get_active_for_worker_item(
                s.item.id, s.native.workspace_id
            )
            is None
        )
    assert s.commands.handle(command) == result
    assert len(s.native.execution.starts) == 1
    assert s.public.sent == []


@pytest.mark.parametrize("target", ["profile", "human"])
def test_native_handoff_human_confirm_creates_same_pending_transition(
    intervention, target
):
    s = intervention
    prepared = s.commands.handle(native_prepare_command(s, target))
    with s.native.uow() as uow:
        proposal = uow.handoff_proposals.get(
            prepared.aggregate_id, s.native.workspace_id
        )
    command = native_confirm_command(s, proposal)
    result = s.commands.handle(command)
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(
            result.aggregate_id, s.native.workspace_id
        )
        assert transition.handoff_schema_version == 2
        assert transition.phase.value == "checkpoint_source"
        assert transition.writer_state.value == "source_active"
        assert transition.target_worker_id is transition.target_execution_id is None
        assert transition.native_payload == proposal.payload
        original = uow.agent_bindings.get(
            s.native.workspace_id, "worker_item", s.item.id
        )
        assert original.version == s.link.binding_version
        assert original.profile_ref == s.link.profile_ref
    assert s.commands.handle(command) == result
    assert len(s.native.execution.starts) == 1


@pytest.mark.parametrize("ack", ["accepted", "lost"])
def test_native_factory_pending_stop_queries_original_across_restart(
    intervention, monkeypatch, ack
):
    from sidera_control_plane.domain.gateway import (
        handoff_step_operation_id,
    )
    from sidera_control_plane.domain.handoffs import HandoffPhase

    s = intervention
    with s.native.uow() as uow:
        consumed = uow.sessions.get_any(
            s.session.id, s.native.workspace_id
        ).last_sequence
        consumed_events = uow.execution_events.list_for_session(
            s.session.id, s.native.workspace_id
        )
        original_link = uow.execution_authorities.get_link(
            s.native.workspace_id, s.execution.id
        )
    stops = PublicStopControl(s)
    stops.lose_ack = ack == "lost"
    prepared = s.commands.handle(native_prepare_command(s))
    with s.native.uow() as uow:
        proposal = uow.handoff_proposals.get(
            prepared.aggregate_id, s.native.workspace_id
        )
    confirmed = s.commands.handle(native_confirm_command(s, proposal))
    worker = native_handoff_worker(s, monkeypatch)
    first = worker.coordinator.run_once(1)
    assert first.progressed == 1
    with s.native.uow() as uow:
        checkpoint = uow.handoff_transitions.get(
            confirmed.aggregate_id, s.native.workspace_id
        )
        assert checkpoint.phase is HandoffPhase.STOP_SOURCE
        assert (
            checkpoint.source_checkpoint_facts["observation_kind"] == "cp_run_snapshot"
        )
        assert checkpoint.source_checkpoint_facts["quiescent"] is False
    operation_id = handoff_step_operation_id(
        checkpoint.id, HandoffPhase.STOP_SOURCE, logical_attempt=1, action="stop_source"
    )
    assert worker.coordinator.run_once(1).waiting == 1
    for _ in range(2):
        worker = native_handoff_worker(s, monkeypatch)
        assert worker.coordinator.run_once(1).waiting == 1
    with s.native.uow() as uow:
        pending = uow.handoff_transitions.get(checkpoint.id, s.native.workspace_id)
        operation = uow.gateway_operations.get_by_operation_id(
            operation_id, s.native.workspace_id
        )
        assert pending.logical_attempt == operation.attempt_count == 1
        assert pending.failure_code == "source_stop_unconfirmed"
        assert pending.source_stop_facts is None and pending.target_execution_id is None
        assert pending.phase is HandoffPhase.STOP_SOURCE
    assert len(stops.sent) == 1 and len(stops.queried) == 2
    assert all(query[2] == operation_id for query in stops.queried)
    assert len(s.native.execution.starts) == 1
    stops.close_original()
    worker = native_handoff_worker(s, monkeypatch)
    assert worker.coordinator.run_once(1).progressed == 1
    with s.native.uow() as uow:
        stopped = uow.handoff_transitions.get(checkpoint.id, s.native.workspace_id)
        operation = uow.gateway_operations.get_by_operation_id(
            operation_id, s.native.workspace_id
        )
        session = uow.sessions.get_any(s.session.id, s.native.workspace_id)
        source = uow.executions.get(s.execution.id, s.native.workspace_id)
        assert stopped.phase is HandoffPhase.FREEZE_SOURCE_WORKSPACE
        assert stopped.source_stop_sequence > stopped.source_checkpoint_sequence
        assert stopped.source_stop_facts["operation_id"] == str(operation_id)
        assert stopped.source_stop_facts["run_state"] == "stopped"
        assert stopped.writer_state.value == "writer_none"
        assert operation.status.value == "succeeded"
        assert (
            session.status.value == "closed"
            and source.status is ExecutionStatus.CANCELED
        )
        assert session.last_sequence == consumed
        assert (
            uow.execution_events.list_for_session(session.id, s.native.workspace_id)
            == consumed_events
        )
        link = uow.execution_authorities.get_link(s.native.workspace_id, source.id)
        assert (link.event_cursor, link.status) == (
            original_link.event_cursor,
            original_link.status,
        )
        assert stopped.source_stop_facts["observed_sequence"] > consumed
        assert (
            uow.workers.get(s.session.worker_id, s.native.workspace_id).current_load
            == 0
        )
    assert len(stops.sent) == 1 and len(s.native.execution.starts) == 1
    backlog = s.native.execution.list_events(
        s.native.workspace_id, s.link.run_id, consumed, grant_id=s.link.authority_id
    )
    assert backlog.events and backlog.events[-1].kind == "run.stopped"


def pending_native_stop(s, monkeypatch, target="profile"):
    stops = PublicStopControl(s)
    prepared = s.commands.handle(native_prepare_command(s, target))
    with s.native.uow() as uow:
        proposal = uow.handoff_proposals.get(
            prepared.aggregate_id, s.native.workspace_id
        )
    confirmed = s.commands.handle(native_confirm_command(s, proposal))
    worker = native_handoff_worker(s, monkeypatch)
    assert worker.coordinator.run_once(1).progressed == 1
    assert worker.coordinator.run_once(1).waiting == 1
    return stops, worker, confirmed.aggregate_id, stops.sent[0].operation_id


def test_native_stop_changed_owner_retains_only_closure_proof(
    intervention, monkeypatch
):
    s = intervention
    stops, worker, transition_id, operation_id = pending_native_stop(s, monkeypatch)
    stops.close_original()
    with s.native.uow() as uow:
        authority = uow.execution_authorities.get(s.link.authority_id)
        lease = uow.leases.get(
            authority.lease_id, s.native.workspace_id, for_update=True
        )
        previous = lease.version
        lease.fencing_token += 1
        lease.version += 1
        uow.leases.save(lease, previous)
        replacement = lease
    for _ in range(2):
        assert worker.coordinator.run_once(1).blocked == 1
        with s.native.uow() as uow:
            transition = uow.handoff_transitions.get(
                transition_id, s.native.workspace_id
            )
            operation = uow.gateway_operations.get_by_operation_id(
                operation_id, s.native.workspace_id
            )
            assert (
                transition.phase.value == "stop_source"
                and transition.target_execution_id is None
            )
            assert transition.failure_code == "native_handoff_source_owner_changed"
            assert transition.source_stop_facts["operation_id"] == str(operation_id)
            assert operation.status.value == "running" and operation.result_ref is None
            assert uow.leases.get(replacement.id, s.native.workspace_id) == replacement
            assert (
                uow.executions.get(s.execution.id, s.native.workspace_id).status
                is ExecutionStatus.RUNNING
            )
            assert (
                uow.sessions.get_any(s.session.id, s.native.workspace_id).status.value
                == "active"
            )
            assert (
                uow.workers.get(s.session.worker_id, s.native.workspace_id).current_load
                == 1
            )
        s.native.cp.now += timedelta(seconds=2)
        worker = native_handoff_worker(s, monkeypatch)
    assert len(stops.sent) == len(s.native.execution.starts) == 1


@pytest.mark.parametrize("failure_point", ["worker_save", "stop_audit"])
def test_native_stop_owned_cleanup_rolls_back_all_facts(
    intervention, monkeypatch, failure_point
):
    from sidera_control_plane.persistence.worker_repository import WorkerRepository

    s = intervention
    stops, worker, transition_id, operation_id = pending_native_stop(s, monkeypatch)
    stops.close_original()
    reached = []
    save_worker = WorkerRepository.save
    append_event = EventRepository.append_with_outbox

    def fail_worker(self, worker, previous):
        if worker.id == s.session.worker_id and worker.current_load == 0:
            reached.append("worker_save")
            raise RuntimeError("controlled cleanup persistence failure")
        return save_worker(self, worker, previous)

    def fail_audit(self, event):
        if event.event_type == "ExecutionCanceledByHandoff":
            reached.append("stop_audit")
            raise RuntimeError("controlled stop audit persistence failure")
        return append_event(self, event)

    monkeypatch.setattr(
        WorkerRepository,
        "save",
        fail_worker if failure_point == "worker_save" else save_worker,
    )
    monkeypatch.setattr(
        EventRepository,
        "append_with_outbox",
        fail_audit if failure_point == "stop_audit" else append_event,
    )
    assert worker.coordinator.run_once(1).blocked == 1
    assert reached == [failure_point]
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        operation = uow.gateway_operations.get_by_operation_id(
            operation_id, s.native.workspace_id
        )
        assert (
            transition.source_stop_facts is None
            and transition.source_stop_sequence is None
        )
        assert (
            transition.phase.value == "stop_source"
            and transition.target_execution_id is None
        )
        assert operation.status.value == "running" and operation.result_ref is None
        execution = uow.executions.get(s.execution.id, s.native.workspace_id)
        assert (
            execution.status is ExecutionStatus.RUNNING
            and execution.active_lease_id is not None
        )
        assert (
            uow.sessions.get_any(s.session.id, s.native.workspace_id).status.value
            == "active"
        )
        assert (
            uow.leases.get(
                execution.active_lease_id, s.native.workspace_id
            ).status.value
            == "active"
        )
        assert (
            uow.workers.get(s.session.worker_id, s.native.workspace_id).current_load
            == 1
        )
        assert not tuple(
            uow.session.scalars(
                select(EventModel).where(
                    EventModel.aggregate_id == execution.id,
                    EventModel.event_type == "ExecutionCanceledByHandoff",
                )
            )
        )
    monkeypatch.setattr(WorkerRepository, "save", save_worker)
    monkeypatch.setattr(EventRepository, "append_with_outbox", append_event)
    s.native.cp.now += timedelta(seconds=2)
    assert native_handoff_worker(s, monkeypatch).coordinator.run_once(1).progressed == 1
    assert len(stops.sent) == len(s.native.execution.starts) == 1


def test_native_failed_stop_is_known_and_retains_original_action(
    intervention, monkeypatch
):
    from sidera_agents.contracts import StopReceipt

    s = intervention
    stops, worker, transition_id, operation_id = pending_native_stop(s, monkeypatch)
    stops.receipts[operation_id] = StopReceipt.model_validate(
        {
            **stops.receipts[operation_id].model_dump(),
            "status": "failed",
            "reason_code": "PRIVATE_STOP_DETAIL",
        }
    )
    for _ in range(2):
        assert worker.coordinator.run_once(1).blocked == 1
        with s.native.uow() as uow:
            transition = uow.handoff_transitions.get(
                transition_id, s.native.workspace_id
            )
            operation = uow.gateway_operations.get_by_operation_id(
                operation_id, s.native.workspace_id
            )
            assert (
                transition.failure_code
                == operation.failure_code
                == "native_handoff_source_stop_failed"
            )
            assert (
                transition.phase.value == "stop_source"
                and transition.source_stop_facts is None
            )
            assert operation.status.value == "failed" and operation.attempt_count == 1
            assert (
                uow.executions.get(s.execution.id, s.native.workspace_id).status
                is ExecutionStatus.RUNNING
            )
            assert (
                uow.sessions.get_any(s.session.id, s.native.workspace_id).status.value
                == "active"
            )
            assert (
                uow.workers.get(s.session.worker_id, s.native.workspace_id).current_load
                == 1
            )
            assert "PRIVATE_STOP_DETAIL" not in str(
                tuple(
                    uow.session.scalars(
                        select(EventModel.payload).where(
                            EventModel.aggregate_id == transition.id
                        )
                    )
                )
            )
        s.native.cp.now += timedelta(seconds=2)
        worker = native_handoff_worker(s, monkeypatch)
    assert len(stops.sent) == len(s.native.execution.starts) == 1


@pytest.mark.parametrize("target", ["human", "profile"])
def test_native_factory_freezes_then_assigns_without_dummy_runtime(
    intervention, monkeypatch, database_url, target
):
    from sidera_control_plane.domain.handoffs import HandoffPhase

    s = intervention
    stops = PublicStopControl(s)
    queued = s.commands.handle(s.command(text="Retained original manual context"))
    prepared = s.commands.handle(native_prepare_command(s, target))
    with s.native.uow() as uow:
        proposal = uow.handoff_proposals.get(
            prepared.aggregate_id, s.native.workspace_id
        )
    confirmed = s.commands.handle(native_confirm_command(s, proposal))
    worker = native_handoff_worker(s, monkeypatch)
    assert worker.coordinator.run_once(1).progressed == 1
    assert worker.coordinator.run_once(1).waiting == 1
    stops.close_original()
    assert worker.coordinator.run_once(1).progressed == 1
    assert worker.coordinator.run_once(1).progressed == 1
    with s.native.uow() as uow:
        frozen = uow.handoff_transitions.get(
            confirmed.aggregate_id, s.native.workspace_id
        )
        assert frozen.phase is HandoffPhase.CREATE_TARGET_EXECUTION
        assert (
            uow.workspaces.get(
                frozen.source_workspace_manifest_id, s.native.workspace_id
            ).lifecycle.value
            == "frozen"
        )
    result = worker.coordinator.run_once(1)
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(
            confirmed.aggregate_id, s.native.workspace_id
        )
        binding = uow.agent_bindings.get(
            s.native.workspace_id, "worker_item", s.item.id
        )
        assert binding.version == s.link.binding_version + 1
        assert (
            binding.assignee.kind == target
            and transition.target_binding_version == binding.version
        )
        assert uow.worker_items.get(s.item.id, s.native.workspace_id).id == s.item.id
        assert (
            uow.worker_items.get_current_placement(
                s.item.id, s.native.workspace_id
            ).plan_generation
            == s.placement.plan_generation
        )
        assert (
            uow.execution_inputs.get(queued.aggregate_id, s.native.workspace_id).text
            == "Retained original manual context"
        )
        if target == "human":
            assert result.converged == 1
            assert (
                transition.status.value == "converged"
                and transition.phase is HandoffPhase.COMPLETE
            )
            assert transition.writer_state.value == "writer_none"
            assert (
                transition.target_execution_id
                is transition.target_session_id
                is transition.target_worker_id
                is None
            )
            assert (
                transition.target_workspace_manifest_id
                is transition.target_context_package_id
                is None
            )
            assert (
                uow.execution_requests.get_by_handoff_transition(
                    transition.id, s.native.workspace_id
                )
                is None
            )
            assert tuple(transition.manual_context_facts["source_input_refs"]) == (
                str(queued.aggregate_id),
            )
            assert not binding.runnable and binding.profile_ref is None
            event_types = set(
                uow.session.scalars(
                    select(EventModel.event_type).where(
                        EventModel.aggregate_id == transition.id
                    )
                )
            )
            assert {"HandoffHumanAssigned", "HandoffConverged"} <= event_types
            assert not event_types & {
                "HandoffTargetExecutionCreated",
                "HandoffTargetSessionStarted",
                "HandoffContextAcknowledged",
                "HandoffInputTransferred",
            }
            milestones = tuple(
                uow.session.scalars(
                    select(EventModel).where(
                        EventModel.aggregate_id == transition.id,
                        EventModel.event_type.in_(
                            ("HandoffHumanAssigned", "HandoffConverged")
                        ),
                    )
                )
            )
            assert len({event.aggregate_version for event in milestones}) == 2
            for event in milestones:
                assert event.payload["target_assignee"] == {
                    "kind": "human",
                    "id": s.native.principal,
                }
                assert event.payload["target_binding_version"] == binding.version
                assert event.payload["automatic_target_phases"] == "not_applicable"
                assert str(s.link.authority_id) not in str(event.payload)
            (disposition,) = transition.manual_context_facts[
                "source_input_dispositions"
            ]
            assert (
                disposition["operation_id"] is None
                and disposition["status"] == "queued"
            )
        else:
            assert (
                result.progressed == 1
                and transition.phase is HandoffPhase.ASSEMBLE_BUNDLE
            )
            assert (
                transition.target_execution_id is not None
                and transition.target_worker_id is None
            )
            execution = uow.executions.get(
                transition.target_execution_id, s.native.workspace_id
            )
            request = uow.execution_requests.get_by_handoff_transition(
                transition.id, s.native.workspace_id
            )
            assert (
                execution.pinned_worker_id
                is execution.assigned_worker_id
                is execution.active_lease_id
                is None
            )
            assert execution.worker_item_id == request.worker_item_id == s.item.id
            assert (
                execution.source_execution_id
                == request.source_execution_id
                == s.execution.id
            )
    assert len(s.native.execution.starts) == len(stops.sent) == 1
    with native_api_client(s, database_url) as client:
        viewed = client.get(
            f"/api/v1/workspaces/{s.native.workspace_id}/handoff-transitions/{transition.id}"
        )
        assert viewed.status_code == 200, viewed.text
        assert viewed.json()["target_worker_id"] is None
        assert str(s.link.authority_id) not in viewed.text


def revise_native_goal(s, goal):
    from sidera_control_plane.domain.work_context import WorkContextRevisionReason

    with s.native.uow() as uow:
        context = uow.work_contexts.get_by_root_task(
            s.item.root_task_id, s.native.workspace_id, for_update=True
        )
        old = uow.work_contexts.get_revision(
            context.current_revision_id, s.native.workspace_id
        )
        snapshot = replace(old.snapshot, objective=goal)
        revision = replace(
            old,
            revision_id=uuid4(),
            revision_number=old.revision_number + 1,
            reason=WorkContextRevisionReason.USER_INTERVENTION,
            snapshot=snapshot,
            content_hash=snapshot.content_hash,
            created_by=s.native.principal,
        )
        previous = context.version
        context.append_revision(
            revision, expected_version=previous, now=s.native.cp.now
        )
        uow.work_contexts.append_revision(context, revision, expected_version=previous)
        item = uow.worker_items.get(s.item.id, s.native.workspace_id, for_update=True)
        previous = item.version
        item.advance_context(now=s.native.cp.now)
        uow.worker_items.save(item, previous)
        s.item = item
    return revision


def test_native_profile_bundle_package_and_transfer_use_latest_context(
    intervention, monkeypatch
):
    from sidera_control_plane.domain.handoffs import HandoffPhase

    s = intervention
    latest = revise_native_goal(
        s, "Latest authorized goal after original native preparation"
    )
    stops, worker, transition_id, _ = pending_native_stop(s, monkeypatch)
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert transition.target_context_version == s.item.context_version == 2
        assert transition.native_payload.source_worker_context_version == 1
    stops.close_original()
    for _ in range(3):
        assert worker.coordinator.run_once(1).progressed == 1
    assert worker.coordinator.run_once(1).waiting == 1
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert (
            transition.phase is HandoffPhase.ASSEMBLE_BUNDLE
            and transition.target_worker_id is None
        )
        bundle = uow.handoff_context_bundles.get(
            transition.handoff_context_bundle_id, s.native.workspace_id
        )
        assert bundle.schema_version == 2 and bundle.target_worker_id is None
        assert bundle.target_profile_ref == transition.native_payload.target_profile_ref
        target = uow.executions.get(
            transition.target_execution_id, s.native.workspace_id
        )
        assert target.active_lease_id is target.assigned_worker_id is None
    assert s.native.cp.run_context_worker("native-handoff").ready == 1
    assert worker.coordinator.run_once(1).progressed == 1
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        package = uow.context_packages.get(
            transition.target_context_package_id, s.native.workspace_id
        )
        assert (
            package.work_context_revision_id == latest.revision_id
            and package.context_version == 2
        )
        facts = package.control_plane_fact_snapshot["handoff"]
        assert facts["target"]["profile_ref"] == bundle.target_profile_ref.model_dump(
            mode="json"
        )
        assert (
            "worker_id" not in facts["target"] and "runtime_hash" not in facts["target"]
        )
        assert str(s.link.authority_id) not in str(facts)
        source_manifest = uow.workspaces.get(
            transition.source_workspace_manifest_id, s.native.workspace_id
        )
    assert worker.coordinator.run_once(1).progressed == 1
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert (
            transition.phase is HandoffPhase.START_TARGET_SESSION
            and transition.target_worker_id is None
        )
        target_manifest = uow.workspaces.get(
            transition.target_workspace_manifest_id, s.native.workspace_id
        )
        assert target_manifest.location_ref == source_manifest.location_ref
        assert target_manifest.fencing_token == source_manifest.fencing_token + 1
    assert len(s.native.execution.starts) == len(stops.sent) == 1


def native_profile_ready_to_start(s, monkeypatch):
    stops, worker, transition_id, _ = pending_native_stop(s, monkeypatch)
    stops.close_original()
    for _ in range(3):
        assert worker.coordinator.run_once(1).progressed == 1
    assert worker.coordinator.run_once(1).waiting == 1
    assert s.native.cp.run_context_worker("native-handoff").ready == 1
    for _ in range(2):
        assert worker.coordinator.run_once(1).progressed == 1
    assert len(s.native.execution.starts) == 1
    return stops, worker, transition_id


def native_profile_at_ack(s, monkeypatch):
    stops, worker, transition_id = native_profile_ready_to_start(s, monkeypatch)
    assert worker.coordinator.run_once(1).progressed == 1
    return stops, worker, transition_id


def test_native_profile_factory_starts_actual_target_after_stop_and_transfer(
    intervention, monkeypatch
):
    from sidera_control_plane.domain.handoffs import HandoffPhase

    s = intervention
    queued = s.commands.handle(s.command(text="Keep this explicit queued instruction"))
    latest = revise_native_goal(
        s, "Latest authorized goal for actual handoff convergence"
    )
    stops, worker, transition_id = native_profile_ready_to_start(s, monkeypatch)
    assert worker.coordinator.run_once(1).progressed == 1
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert transition.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT
        target = uow.executions.get(
            transition.target_execution_id, s.native.workspace_id
        )
        session = uow.sessions.get(transition.target_session_id, s.native.workspace_id)
        binding = uow.bindings.get_for_execution(target.id, s.native.workspace_id)
        lease = uow.leases.get(target.active_lease_id, s.native.workspace_id)
        link = uow.execution_authorities.get_link(s.native.workspace_id, target.id)
        assert (
            target.assigned_worker_id
            == transition.target_worker_id
            == binding.worker_id
            == lease.worker_id
        )
        assert (
            session.execution_id == target.id
            and session.adapter_session_id == f"agent-run:{link.run_id}"
        )
        assert link.binding_version == transition.target_binding_version
        assert link.profile_ref == transition.native_payload.target_profile_ref
        assert link.preparation is not None
        assert (
            uow.sessions.get_any(s.session.id, s.native.workspace_id).status.value
            == "closed"
        )
        assert target.worker_item_id == s.item.id
        assert (
            uow.worker_items.get_current_placement(
                s.item.id, s.native.workspace_id
            ).plan_generation
            == s.placement.plan_generation
        )
    assert len(s.native.execution.starts) == 2 and len(stops.sent) == 1
    for _ in range(3):
        progress = worker.coordinator.run_once(1)
        with s.native.uow() as uow:
            saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert progress.progressed == 1, (saved.phase, saved.failure_code)
    assert worker.coordinator.run_once(1).converged == 1
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert transition.status.value == "converged"
        assert transition.writer_state.value == "target_active"
        package = uow.context_packages.get(
            transition.target_context_package_id, s.native.workspace_id
        )
        assert package.work_context_revision_id == latest.revision_id
        assert package.context_version == transition.target_context_version == 2
        derived = uow.execution_inputs.list_for_handoff_transition(
            transition_id, s.native.workspace_id
        )
        assert len(derived) == 1 and derived[0].source_input_id == queued.aggregate_id
        assert derived[0].execution_id == transition.target_execution_id
        assert derived[0].session_id == transition.target_session_id
        assert (
            derived[0].id != queued.aggregate_id and derived[0].status.value == "queued"
        )
        assert (
            uow.execution_inputs.get(
                queued.aggregate_id, s.native.workspace_id
            ).status.value
            == "superseded"
        )
        assert (
            uow.context_packages.get_ack(
                transition.target_context_package_id,
                "worker",
                transition.target_execution_id,
                s.native.workspace_id,
            )
            is None
        )
        events = tuple(
            uow.session.scalars(
                select(EventModel).where(
                    EventModel.workspace_id == s.native.workspace_id,
                    EventModel.aggregate_id == transition_id,
                    EventModel.event_type == "HandoffContextAcknowledged",
                )
            )
        )
        assert len(events) == 1
    assert len(s.native.execution.starts) == 2 and len(stops.sent) == 1
    assert s.public.sent == []


@pytest.mark.parametrize(
    "changed",
    ["profile", "effective_profile", "started", "preparation", "revoked", "context"],
)
def test_native_profile_ack_revalidates_original_public_and_cp_proof(
    intervention, monkeypatch, changed
):
    from sidera_control_plane.domain.gateway import handoff_step_operation_id
    from sidera_control_plane.domain.handoffs import HandoffPhase
    from sidera_control_plane.persistence.models import AgentExecutionLinkModel

    s = intervention
    stops, worker, transition_id = native_profile_at_ack(s, monkeypatch)
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        link = uow.execution_authorities.get_link(
            s.native.workspace_id, transition.target_execution_id
        )
    original_get = s.native.execution.get_run

    def inspect(workspace_id, run_id, **kwargs):
        snapshot = original_get(workspace_id, run_id, **kwargs)
        assert run_id == link.run_id
        if changed == "profile":
            snapshot = snapshot.model_copy(
                update={
                    "profile_ref": snapshot.profile_ref.model_copy(
                        update={"revision": 2}
                    )
                }
            )
        elif changed == "effective_profile":
            snapshot = snapshot.model_copy(
                update={
                    "effective_config": snapshot.effective_config.model_copy(
                        update={
                            "profile_ref": snapshot.profile_ref.model_copy(
                                update={"revision": 2}
                            )
                        }
                    )
                }
            )
        elif changed == "revoked":
            with s.native.uow() as uow:
                uow.execution_authorities.revoke(link.authority_id, workspace_id)
        elif changed == "context":
            revise_native_goal(s, "changed during native ACK inspection")
        return snapshot

    if changed == "preparation":
        with s.native.uow() as uow:
            row = uow.session.get(
                AgentExecutionLinkModel, (s.native.workspace_id, link.execution_id)
            )
            row.preparation = {**row.preparation, "package_hash": "f" * 64}
    elif changed == "started":
        s.native.execution.events[link.run_id][0] = s.native.execution.events[
            link.run_id
        ][0].model_copy(update={"operation_id": uuid4()})
    monkeypatch.setattr(s.native.execution, "get_run", inspect)
    assert worker.coordinator.run_once(1).blocked == 1
    with s.native.uow() as uow:
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert saved.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT
        assert saved.failure_code in {
            "native_handoff_start_invalid",
            "context_hash_mismatch",
            "permission_denied",
            "context_package_stale",
        }
        operation_id = handoff_step_operation_id(
            transition_id,
            HandoffPhase.ACKNOWLEDGE_CONTEXT,
            logical_attempt=saved.logical_attempt,
            action="enable_writer",
        )
        operation = uow.gateway_operations.get_by_operation_id(
            operation_id, s.native.workspace_id
        )
        assert operation is None or operation.status.value != "succeeded"
    assert len(s.native.execution.starts) == 2 and len(stops.sent) == 1
    assert s.public.sent == []


def test_native_profile_ack_same_operation_replay_and_atomic_rollback(
    intervention, monkeypatch
):
    from sidera_control_plane.domain.gateway import handoff_step_operation_id
    from sidera_control_plane.domain.handoffs import HandoffPhase

    s = intervention
    stops, worker, transition_id = native_profile_at_ack(s, monkeypatch)
    (claimed,) = worker.coordinator._claim(now=s.native.cp.now, limit=1)
    operation_id = handoff_step_operation_id(
        transition_id,
        HandoffPhase.ACKNOWLEDGE_CONTEXT,
        logical_attempt=claimed.logical_attempt,
        action="enable_writer",
    )
    append = EventRepository.append_with_outbox

    def fail_confirmation(self, event):
        if event.event_type == "NativeHandoffContextConfirmed":
            raise RuntimeError("controlled ACK publication failure")
        return append(self, event)

    monkeypatch.setattr(EventRepository, "append_with_outbox", fail_confirmation)
    call = lambda: s.native.gateway.enable_handoff_target_writer(
        workspace_id=s.native.workspace_id,
        transition_id=transition_id,
        expected_transition_version=claimed.version,
    )
    with pytest.raises(RuntimeError, match="ACK publication"):
        call()
    with s.native.uow() as uow:
        assert (
            uow.gateway_operations.get_by_operation_id(
                operation_id, s.native.workspace_id
            )
            is None
        )
    monkeypatch.setattr(EventRepository, "append_with_outbox", append)
    first = call()
    second = call()
    assert first == second and first.operation_id == operation_id
    with s.native.uow() as uow:
        events = tuple(
            uow.session.scalars(
                select(EventModel).where(
                    EventModel.workspace_id == s.native.workspace_id,
                    EventModel.aggregate_id == operation_id,
                    EventModel.event_type == "NativeHandoffContextConfirmed",
                )
            )
        )
        assert len(events) == 1
    assert (
        len(s.native.execution.starts) == 2
        and len(stops.sent) == 1
        and s.public.sent == []
    )


@pytest.mark.parametrize("admission", ["capacity", "busy", "lost_ack", "query_timeout"])
def test_native_profile_actual_start_waits_keep_one_intention(
    intervention, monkeypatch, admission
):
    from sidera_control_plane.domain.handoffs import HandoffPhase

    s = intervention
    stops, worker, transition_id = native_profile_ready_to_start(s, monkeypatch)
    original_start = s.native.execution.start
    original_lookup = s.native.execution.get_start
    if admission == "capacity":
        with s.native.uow() as uow:
            runtime = uow.workers.get(
                s.session.worker_id, s.native.workspace_id, for_update=True
            )
            previous = runtime.version
            runtime.heartbeat(healthy=False, now=s.native.cp.now)
            uow.workers.save(runtime, previous)
    elif admission == "busy":
        s.native.execution.next_status = "busy"
    else:

        def lose_ack(request):
            original_start(request)
            raise TimeoutError("controlled target start ACK loss")

        monkeypatch.setattr(s.native.execution, "start", lose_ack)
        if admission == "query_timeout":

            def unknown_lookup(*args, **kwargs):
                raise TimeoutError("controlled original start query timeout")

            monkeypatch.setattr(s.native.execution, "get_start", unknown_lookup)
    assert worker.coordinator.run_once(1).waiting == 1
    with s.native.uow() as uow:
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert saved.phase is HandoffPhase.START_TARGET_SESSION
        assert saved.failure_code in {
            "target_waiting_capacity",
            "target_start_unconfirmed",
        }
        link = uow.execution_authorities.get_link(
            s.native.workspace_id, saved.target_execution_id
        )
        original_operation = None if link is None else link.start_operation_id
    if admission in {"capacity", "query_timeout"}:
        fresh = native_handoff_worker(s, monkeypatch)
        assert fresh.coordinator.run_once(1).waiting == 1
        assert len(s.native.execution.starts) == (1 if admission == "capacity" else 2)
    if admission == "capacity":
        with s.native.uow() as uow:
            runtime = uow.workers.get(
                s.session.worker_id, s.native.workspace_id, for_update=True
            )
            previous = runtime.version
            runtime.heartbeat(healthy=True, now=s.native.cp.now)
            uow.workers.save(runtime, previous)
    s.native.execution.next_status = "accepted"
    monkeypatch.setattr(s.native.execution, "start", original_start)
    monkeypatch.setattr(s.native.execution, "get_start", original_lookup)
    s.native.cp.now += timedelta(seconds=10)
    worker = native_handoff_worker(s, monkeypatch)
    assert worker.coordinator.run_once(1).progressed == 1
    with s.native.uow() as uow:
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert saved.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT
        link = uow.execution_authorities.get_link(
            s.native.workspace_id, saved.target_execution_id
        )
        if admission in {"lost_ack", "query_timeout"}:
            assert link.start_operation_id == original_operation
    assert (
        len(
            [
                receipt
                for receipt in s.native.execution.receipts.values()
                if receipt.run_id is not None
            ]
        )
        == 2
    )
    assert len(stops.sent) == 1
    for _ in range(3):
        assert worker.coordinator.run_once(1).progressed == 1
    assert worker.coordinator.run_once(1).converged == 1


def test_native_profile_second_handoff_uses_exact_previous_target_and_fences_late_source(
    intervention, monkeypatch
):
    from sidera_control_plane.application.agent_worker_adapter import project_run_event
    from sidera_control_plane.application.event_ingestor import (
        EventIngestionError,
        SessionEventIngestor,
    )

    s = intervention
    original_execution, original_session, original_link = s.execution, s.session, s.link
    stops, worker, first_id = native_profile_at_ack(s, monkeypatch)
    for _ in range(3):
        assert worker.coordinator.run_once(1).progressed == 1
    assert worker.coordinator.run_once(1).converged == 1
    with s.native.uow() as uow:
        first = uow.handoff_transitions.get(first_id, s.native.workspace_id)
        s.execution = uow.executions.get(
            first.target_execution_id, s.native.workspace_id
        )
        s.session = uow.sessions.get_any(first.target_session_id, s.native.workspace_id)
        s.link = uow.execution_authorities.get_link(
            s.native.workspace_id, s.execution.id
        )
        s.item = uow.worker_items.get(s.item.id, s.native.workspace_id)
        original_authority = uow.execution_authorities.get(original_link.authority_id)
    second_stops, worker, second_id, _ = pending_native_stop(s, monkeypatch)
    assert len(s.native.execution.starts) == 2
    second_stops.close_original()
    for _ in range(3):
        assert worker.coordinator.run_once(1).progressed == 1
    assert worker.coordinator.run_once(1).waiting == 1
    assert s.native.cp.run_context_worker("native-handoff").ready == 1
    for _ in range(3):
        assert worker.coordinator.run_once(1).progressed == 1
    with s.native.uow() as uow:
        second = uow.handoff_transitions.get(second_id, s.native.workspace_id)
        assert second.source_execution_id == first.target_execution_id
        assert second.source_session_id == first.target_session_id
        assert (
            second.source_stop_facts is not None
            and second.source_workspace_freeze_facts is not None
        )
        assert second.worker_item_id == s.item.id
        assert (
            second.native_payload.observed_plan_generation
            == s.placement.plan_generation
        )
        current = uow.agent_bindings.get(
            s.native.workspace_id, "worker_item", s.item.id
        )
    s.native.execution.emit(original_link.run_id, "run.completed", state="completed")
    event = s.native.execution.events[original_link.run_id][-1]
    with pytest.raises(EventIngestionError):
        SessionEventIngestor(s.native.uow, clock=s.native.cp.clock).ingest_event(
            workspace_id=s.native.workspace_id,
            execution_id=original_execution.id,
            session_id=original_session.id,
            lease_id=original_authority.lease_id,
            fencing_token=original_authority.fencing_token,
            event=project_run_event(event, run_state="completed"),
        )
    for _ in range(3):
        assert worker.coordinator.run_once(1).progressed == 1
    assert worker.coordinator.run_once(1).converged == 1
    with s.native.uow() as uow:
        assert (
            uow.agent_bindings.get(s.native.workspace_id, "worker_item", s.item.id)
            == current
        )
        assert (
            uow.executions.get(original_execution.id, s.native.workspace_id).status
            is ExecutionStatus.CANCELED
        )
        assert (
            uow.handoff_transitions.get(first_id, s.native.workspace_id).status.value
            == "converged"
        )
        assert (
            uow.handoff_transitions.get(second_id, s.native.workspace_id).status.value
            == "converged"
        )
    assert (
        len(s.native.execution.starts) == 3
        and len(stops.sent) == len(second_stops.sent) == 1
    )


def test_native_human_assignment_audit_failure_rolls_back_after_real_stop_and_freeze(
    intervention, monkeypatch
):
    s = intervention
    stops, worker, transition_id, _ = pending_native_stop(s, monkeypatch, "human")
    stops.close_original()
    for _ in range(2):
        assert worker.coordinator.run_once(1).progressed == 1
    append = EventRepository.append_with_outbox

    def fail_manual(self, event):
        if event.event_type == "HandoffHumanAssigned":
            raise RuntimeError("controlled human assignment audit failure")
        return append(self, event)

    monkeypatch.setattr(EventRepository, "append_with_outbox", fail_manual)
    assert worker.coordinator.run_once(1).blocked == 1
    with s.native.uow() as uow:
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert (
            saved.phase.value == "create_target_execution"
            and saved.writer_state.value == "writer_none"
        )
        assert (
            saved.target_binding_version is None and saved.manual_context_facts is None
        )
        assert (
            uow.agent_bindings.get(
                s.native.workspace_id, "worker_item", s.item.id
            ).version
            == s.link.binding_version
        )
        assert uow.execution_authorities.get(s.link.authority_id).revoked is False
        assert (
            uow.executions.get(s.execution.id, s.native.workspace_id).status
            is ExecutionStatus.CANCELED
        )
        assert (
            uow.sessions.get_any(s.session.id, s.native.workspace_id).status.value
            == "closed"
        )
    monkeypatch.setattr(EventRepository, "append_with_outbox", append)
    s.native.cp.now += timedelta(seconds=2)
    assert native_handoff_worker(s, monkeypatch).coordinator.run_once(1).converged == 1
    assert len(s.native.execution.starts) == len(stops.sent) == 1


@pytest.mark.parametrize("failure", ["rejected", "revoked_profile"])
def test_native_profile_target_failure_retains_stopped_source_without_replacement(
    intervention, monkeypatch, failure
):
    s = intervention
    stops, worker, transition_id = native_profile_ready_to_start(s, monkeypatch)
    with s.native.uow() as uow:
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
    if failure == "rejected":
        s.native.execution.next_status = "rejected"
    else:
        s.native.catalog.enabled = False
    assert worker.coordinator.run_once(1).blocked == 1
    with s.native.uow() as uow:
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert (
            saved.phase.value == "start_target_session"
            and saved.target_session_id is None
        )
        assert (
            uow.executions.get(s.execution.id, s.native.workspace_id).status
            is ExecutionStatus.CANCELED
        )
        assert (
            uow.sessions.get_any(s.session.id, s.native.workspace_id).status.value
            == "closed"
        )
        assert uow.execution_authorities.get(s.link.authority_id).revoked
    assert (
        len(
            [
                receipt
                for receipt in s.native.execution.receipts.values()
                if receipt.run_id is not None
            ]
        )
        == 1
    )
    assert len(stops.sent) == 1


def test_native_human_context_change_after_collection_blocks_assignment(
    intervention, monkeypatch
):
    from sidera_control_plane.application.profile_handoff import (
        ProfileHandoffPhaseService,
    )

    s = intervention
    stops, worker, transition_id, _ = pending_native_stop(s, monkeypatch, "human")
    stops.close_original()
    for _ in range(2):
        assert worker.coordinator.run_once(1).progressed == 1
    collect = ProfileHandoffPhaseService.prepare_assignment

    def change_after_collect(self, transition):
        result = collect(self, transition)
        revise_native_goal(s, "new authorized manual goal after collection")
        return result

    monkeypatch.setattr(
        ProfileHandoffPhaseService, "prepare_assignment", change_after_collect
    )
    assert worker.coordinator.run_once(1).blocked == 1
    with s.native.uow() as uow:
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert saved.phase.value == "create_target_execution"
        assert saved.failure_code == "native_handoff_assignment_stale"
        assert saved.target_binding_version is saved.manual_context_facts is None
        assert (
            uow.agent_bindings.get(
                s.native.workspace_id, "worker_item", s.item.id
            ).version
            == s.link.binding_version
        )
    assert len(s.native.execution.starts) == len(stops.sent) == 1


def test_native_profile_ack_requires_saved_workspace_transfer_proof(
    intervention, monkeypatch
):
    s = intervention
    stops, worker, transition_id = native_profile_ready_to_start(s, monkeypatch)
    assert worker.coordinator.run_once(1).progressed == 1
    with s.native.uow() as uow:
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        transfer = uow.workspace_transfers.get_by_attempt(
            transition_id, 1, s.native.workspace_id
        )
        manifest = uow.workspaces.get(
            saved.target_workspace_manifest_id, s.native.workspace_id
        )
        assert manifest.fencing_token == transfer.target_fencing_token
    from sidera_control_plane.persistence.handoff_repository import (
        HandoffWorkspaceTransferRepository,
    )

    get_transfer = HandoffWorkspaceTransferRepository.get_by_attempt

    def unavailable(self, requested_id, attempt, workspace_id, **kwargs):
        if requested_id == transition_id:
            return None
        return get_transfer(self, requested_id, attempt, workspace_id, **kwargs)

    monkeypatch.setattr(
        HandoffWorkspaceTransferRepository, "get_by_attempt", unavailable
    )
    assert worker.coordinator.run_once(1).blocked == 1
    with s.native.uow() as uow:
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        assert saved.phase.value == "acknowledge_context"
        assert saved.failure_code == "native_handoff_workspace_changed"
    monkeypatch.setattr(
        HandoffWorkspaceTransferRepository, "get_by_attempt", get_transfer
    )
    with s.native.uow() as uow:
        assert (
            uow.workspace_transfers.get_by_attempt(
                transition_id, 1, s.native.workspace_id
            )
            == transfer
        )
    assert (
        len([r for r in s.native.execution.receipts.values() if r.run_id is not None])
        == 2
    )
    assert len(stops.sent) == 1


@pytest.mark.parametrize(
    "case", ["valid", "stale_version", "unreserved_pointer", "rollback"]
)
def test_native_profile_reservation_uses_actual_n12_facts(
    intervention, monkeypatch, case
):
    from sidera_control_plane.application.handoff_target import HandoffTargetError
    from sidera_control_plane.domain.errors import ValidationError
    from sidera_control_plane.persistence.handoff_repository import (
        HandoffTransitionRepository,
    )

    s = intervention
    stops, worker, transition_id = native_profile_ready_to_start(s, monkeypatch)
    (claimed,) = worker.coordinator._claim(now=s.native.cp.now, limit=1)
    assert claimed.id == transition_id
    with s.native.uow() as uow:
        source_worker = uow.workers.get(s.session.worker_id, s.native.workspace_id)
        target = uow.executions.get(claimed.target_execution_id, s.native.workspace_id)
        assert target.active_lease_id is target.assigned_worker_id is None
    if case == "unreserved_pointer":
        with pytest.raises(ValidationError), s.native.uow() as uow:
            changed = uow.handoff_transitions.get(
                transition_id, s.native.workspace_id, for_update=True
            )
            previous = changed.version
            changed.target_worker_id = source_worker.id
            changed.version += 1
            uow.handoff_transitions.save(changed, previous)
    else:
        if case == "rollback":
            original = HandoffTransitionRepository.save

            def fail_pointer(self, transition, previous):
                if (
                    transition.id == transition_id
                    and transition.target_worker_id is not None
                ):
                    raise RuntimeError("controlled reservation publication failure")
                return original(self, transition, previous)

            monkeypatch.setattr(HandoffTransitionRepository, "save", fail_pointer)
        call = lambda: worker.coordinator._scheduler.assign_handoff_target(
            workspace_id=s.native.workspace_id,
            transition_id=transition_id,
            expected_transition_version=claimed.version + (case == "stale_version"),
            now=s.native.cp.now,
        )
        if case in {"stale_version", "rollback"}:
            with pytest.raises(
                RuntimeError if case == "rollback" else HandoffTargetError
            ):
                call()
        else:
            assignment = call()
            replay = worker.coordinator._scheduler.assign_handoff_target(
                workspace_id=s.native.workspace_id,
                transition_id=transition_id,
                expected_transition_version=claimed.version + 1,
                now=s.native.cp.now,
            )
            assert replay == assignment and assignment.execution_id == target.id
    with s.native.uow() as uow:
        target = uow.executions.get(target.id, s.native.workspace_id)
        saved = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        if case == "valid":
            lease = uow.leases.get(target.active_lease_id, s.native.workspace_id)
            binding = uow.bindings.get_for_execution(target.id, s.native.workspace_id)
            assert (
                target.assigned_worker_id
                == saved.target_worker_id
                == binding.worker_id
                == lease.worker_id
            )
            assert lease.assignment_reason["policy"] == "handoff-native-v2"
        else:
            assert (
                target.active_lease_id
                is target.assigned_worker_id
                is saved.target_worker_id
                is None
            )
            assert (
                uow.bindings.get_for_execution(target.id, s.native.workspace_id) is None
            )
        assert (
            uow.worker_items.get_current_placement(
                s.item.id, s.native.workspace_id
            ).plan_generation
            == s.placement.plan_generation
        )
    assert len(s.native.execution.starts) == len(stops.sent) == 1


@pytest.mark.parametrize("replay", ["normal", "concurrent"])
@pytest.mark.parametrize(
    "command_type", ["ConfirmProfileHandoff", "ConfirmWorkerHandoff"]
)
def test_native_handoff_matching_trusted_agent_cannot_confirm_or_disclose_receipt(
    intervention, replay, command_type
):
    s = intervention
    prepared = s.commands.handle(native_prepare_command(s))
    with s.native.uow() as uow:
        proposal = uow.handoff_proposals.get(
            prepared.aggregate_id, s.native.workspace_id
        )
    command = replace(
        native_confirm_command(s, proposal),
        actor_type="agent",
        command_type=command_type,
    )
    service = dependencies.get_command_service(
        s.native.cp.database,
        Settings(local_actor_id=s.native.principal),
        assignment_identity=TrustedAssignmentIdentity(
            s.native.principal, s.native.setup_grant, actor_type="agent"
        ),
        assignment_authorizer=s.native.assignment._authorizer,
        agent_catalog=s.native.catalog,
        agent_runtimes=s.native.runtimes,
        native_services=s.native.services,
    )
    with s.native.uow() as uow:
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command.id,
                aggregate_id=proposal.id,
                aggregate_version=1,
                event_ids=(),
                data={"receipt": "must-not-disclose"},
            ),
        )
    with pytest.raises(DomainError) as denied:
        if replay == "normal":
            service.handle(command)
        else:
            service._recover_concurrent(command, hash_request(command))
    assert denied.value.code == (
        "assignment_forbidden"
        if command_type == "ConfirmProfileHandoff"
        else "native_handoff_command_required"
    )
    with s.native.uow() as uow:
        assert (
            uow.handoff_transitions.get_active_for_worker_item(
                s.item.id, s.native.workspace_id
            )
            is None
        )
    assert len(s.native.execution.starts) == 1


@pytest.mark.parametrize("action", ["prepare", "confirm"])
def test_native_target_disabled_during_source_read_denies_mutation(
    intervention, monkeypatch, action
):
    s = intervention
    prepare = native_prepare_command(s)
    target = s.native.profile.ref.model_validate(prepare.payload["target_profile_ref"])
    proposal = None
    command = prepare
    if action == "confirm":
        prepared = s.commands.handle(prepare)
        with s.native.uow() as uow:
            proposal = uow.handoff_proposals.get(
                prepared.aggregate_id, s.native.workspace_id
            )
        command = native_confirm_command(s, proposal)
    disabled = Event()
    get_status = s.native.catalog.get_profile_status
    get_run = s.native.execution.get_run

    def target_status(ref, *, grant_id):
        status = get_status(ref, grant_id=grant_id)
        return (
            status.model_copy(update={"enabled": False})
            if ref == target and disabled.is_set()
            else status
        )

    def disable_target_after_read(*args, **kwargs):
        snapshot = get_run(*args, **kwargs)
        assert (
            snapshot.run_id == s.link.run_id
            and snapshot.profile_ref == s.link.profile_ref
        )
        assert snapshot.state.value in {"running", "idle"}
        disabled.set()
        return snapshot

    monkeypatch.setattr(s.native.catalog, "get_profile_status", target_status)
    monkeypatch.setattr(s.native.execution, "get_run", disable_target_after_read)
    monkeypatch.setattr(
        s.native.execution,
        "stop",
        lambda *a, **kw: pytest.fail("admission must not stop the source"),
        raising=False,
    )
    with pytest.raises(DomainError) as denied:
        s.commands.handle(command)
    assert denied.value.code == "assignment_forbidden" and disabled.is_set()
    with s.native.uow() as uow:
        saved = uow.handoff_proposals.find_prepared_for_worker_item(
            s.item.id, s.native.workspace_id
        )
        assert saved == proposal
        assert (
            uow.handoff_transitions.get_active_for_worker_item(
                s.item.id, s.native.workspace_id
            )
            is None
        )
    assert s.native.catalog.enabled and len(s.native.execution.starts) == 1


@pytest.mark.parametrize("ttl", [60, 61])
def test_native_prepare_obeys_injected_handler_ttl_limit(
    intervention, monkeypatch, ttl
):
    s = intervention
    handler = dependencies.HandoffCommandHandler

    def bounded_handler(*args, **kwargs):
        return handler(*args, max_proposal_ttl_seconds=60, **kwargs)

    monkeypatch.setattr(dependencies, "HandoffCommandHandler", bounded_handler)
    service = dependencies.get_command_service(
        s.native.cp.database,
        Settings(local_actor_id=s.native.principal),
        assignment_identity=TrustedAssignmentIdentity(
            s.native.principal, s.native.setup_grant
        ),
        assignment_authorizer=s.native.assignment._authorizer,
        agent_catalog=s.native.catalog,
        agent_runtimes=s.native.runtimes,
        native_services=s.native.services,
    )
    command = native_prepare_command(s)
    command = replace(command, payload={**command.payload, "proposal_ttl_seconds": ttl})
    if ttl == 61:
        with pytest.raises(DomainError) as denied:
            service.handle(command)
        assert denied.value.code == "validation_error"
        with s.native.uow() as uow:
            assert (
                uow.handoff_proposals.find_prepared_for_worker_item(
                    s.item.id, s.native.workspace_id
                )
                is None
            )
    else:
        result = service.handle(command)
        with s.native.uow() as uow:
            proposal = uow.handoff_proposals.get(
                result.aggregate_id, s.native.workspace_id
            )
        assert (proposal.expires_at - proposal.created_at).total_seconds() == 60
    assert len(s.native.execution.starts) == 1


@contextmanager
def native_api_client(s, database_url):
    app = create_app(
        Settings(
            database_url=database_url,
            local_actor_type="user",
            local_actor_id=s.native.principal,
        )
    )
    app.dependency_overrides[dependencies.get_trusted_assignment_identity] = lambda: (
        TrustedAssignmentIdentity(s.native.principal, s.native.setup_grant)
    )
    app.dependency_overrides[dependencies.get_assignment_authorizer] = lambda: (
        s.native.assignment._authorizer
    )
    app.dependency_overrides[dependencies.get_assignment_agent_catalog] = lambda: (
        s.native.catalog
    )
    app.dependency_overrides[dependencies.get_assignment_agent_runtimes] = lambda: (
        s.native.runtimes
    )
    app.dependency_overrides[dependencies.get_native_worker_services] = lambda: (
        s.native.services
    )
    with TestClient(app) as client:
        yield client


def native_request(command):
    return jsonable_encoder(
        {
            "command_id": command.id,
            "workspace_id": command.workspace_id,
            "command_type": command.command_type,
            "target_type": command.target_type,
            "target_id": command.target_id,
            "expected_version": command.expected_version,
            "actor": {"type": command.actor_type, "id": command.actor_id},
            "idempotency_key": command.idempotency_key,
            "correlation_id": command.correlation_id,
            "payload": command.payload,
        }
    )


@pytest.mark.parametrize("reason", ["forged", "revoked", "cross_scope"])
def test_native_legacy_cancel_requires_saved_assignment_scope(intervention, reason):
    s = intervention
    prepared = s.commands.handle(native_prepare_command(s))
    cancel = s.native.command(
        "CancelWorkerHandoff",
        "handoff_proposal",
        prepared.aggregate_id,
        1,
        {"reason": "Withdraw suggestion"},
    )
    if reason == "forged":
        cancel = replace(cancel, actor_id="forged")
    elif reason == "revoked":
        s.native.assignment._authorizer.revoked = True
    else:
        s.native.assignment._authorizer.workspace_id = uuid4()
    with pytest.raises(DomainError) as denied:
        s.commands.handle(cancel)
    assert denied.value.code == "assignment_forbidden"
    with s.native.uow() as uow:
        assert (
            uow.handoff_proposals.get(
                prepared.aggregate_id, s.native.workspace_id
            ).status.value
            == "prepared"
        )
    assert len(s.native.execution.starts) == 1


def test_native_cancel_post_replay_and_status_omit_private_authority(
    intervention, database_url
):
    s = intervention
    prepare = native_prepare_command(s)
    with native_api_client(s, database_url) as client:
        result = client.post("/api/v1/commands", json=native_request(prepare))
        assert result.status_code == 200, result.text
        prepared = result.json()["data"]["proposal"]
        with s.native.uow() as uow:
            proposal = uow.handoff_proposals.get(prepared["id"], s.native.workspace_id)
            assert proposal.payload.source_authority_id == s.link.authority_id
            assert proposal.proposal_hash == prepared["proposal_hash"]
        cancel = s.native.command(
            "CancelWorkerHandoff",
            "handoff_proposal",
            proposal.id,
            1,
            {"reason": "Withdraw suggestion"},
        )
        responses = [
            result,
            client.post("/api/v1/commands", json=native_request(prepare)),
            client.get(
                f"/api/v1/workspaces/{s.native.workspace_id}/commands/{prepare.id}"
            ),
            client.post("/api/v1/commands", json=native_request(cancel)),
            client.post("/api/v1/commands", json=native_request(cancel)),
            client.get(
                f"/api/v1/workspaces/{s.native.workspace_id}/commands/{cancel.id}"
            ),
        ]
        for response in responses:
            assert response.status_code == 200, response.text
            assert str(s.link.authority_id) not in response.text
            assert "source_authority_id" not in response.text
    with s.native.uow() as uow:
        assert (
            uow.handoff_proposals.get(proposal.id, s.native.workspace_id).status.value
            == "cancelled"
        )
    assert len(s.native.execution.starts) == 1


@pytest.mark.parametrize("target", ["profile", "human"])
def test_native_proposal_and_confirmed_transition_public_gets(
    intervention, database_url, target
):
    s = intervention
    prepared = s.commands.handle(native_prepare_command(s, target))
    with s.native.uow() as uow:
        proposal = uow.handoff_proposals.get(
            prepared.aggregate_id, s.native.workspace_id
        )
    with native_api_client(s, database_url) as client:
        viewed = client.get(
            f"/api/v1/workspaces/{s.native.workspace_id}/handoff-proposals/{proposal.id}"
        )
        assert viewed.status_code == 200, viewed.text
        assert viewed.json()["status"] == "prepared"
        assert viewed.json()["source_worker_item_version"] == s.item.version
        assert viewed.json()["target_assignee"]["kind"] == target
        assert str(s.link.authority_id) not in viewed.text
        confirm_command = native_confirm_command(s, proposal)
        confirm = client.post("/api/v1/commands", json=native_request(confirm_command))
        assert confirm.status_code == 200, confirm.text
        for receipt in [
            confirm,
            client.post("/api/v1/commands", json=native_request(confirm_command)),
            client.get(
                f"/api/v1/workspaces/{s.native.workspace_id}/commands/{confirm_command.id}"
            ),
        ]:
            assert receipt.status_code == 200, receipt.text
            assert str(s.link.authority_id) not in receipt.text
            assert "source_authority_id" not in receipt.text
            data = (
                receipt.json()["data"]
                if "data" in receipt.json()
                else receipt.json()["result"]["data"]
            )
            assert data["proposal"]["source_worker_item_version"] == s.item.version
            assert data["transition"]["target_worker_id"] is None
        transition = client.get(
            f"/api/v1/workspaces/{s.native.workspace_id}/handoff-transitions/{confirm.json()['aggregate_id']}"
        )
        assert transition.status_code == 200, transition.text
        assert transition.json()["target_worker_id"] is None
        assert transition.json()["phase"] == "checkpoint_source"
        assert str(s.link.authority_id) not in transition.text
    assert len(s.native.execution.starts) == 1


@pytest.mark.parametrize("target", ["profile", "human"])
def test_native_active_summary_keeps_unreserved_target_null(
    intervention, database_url, target
):
    from pydantic import TypeAdapter

    from sidera_control_plane.api.schemas import WorkerWorkspaceProjectionResponse

    s = intervention
    with native_api_client(s, database_url) as client:
        prepared = client.post(
            "/api/v1/commands", json=native_request(native_prepare_command(s, target))
        )
        assert prepared.status_code == 200, prepared.text
        with s.native.uow() as uow:
            proposal = uow.handoff_proposals.get(
                prepared.json()["aggregate_id"], s.native.workspace_id
            )
        confirmed = client.post(
            "/api/v1/commands", json=native_request(native_confirm_command(s, proposal))
        )
        assert confirmed.status_code == 200, confirmed.text
        viewed = client.get(
            f"/api/v1/workspaces/{s.native.workspace_id}/worker-items/{s.item.id}/handoff"
        )
        assert viewed.status_code == 200, viewed.text
        summary = viewed.json()
        assert summary["transition_id"] == confirmed.json()["aggregate_id"]
        assert summary["proposal_id"] == str(proposal.id)
        assert summary["source_execution_id"] == str(s.execution.id)
        assert summary["source_session_id"] == str(s.session.id)
        assert (
            summary["target_worker_id"]
            is summary["target_execution_id"]
            is summary["target_session_id"]
            is None
        )
        assert summary["available_recovery_strategies"] == []
        assert summary["source_trace_descriptor"]["session_id"] == str(s.session.id)
        embedded = TypeAdapter(
            WorkerWorkspaceProjectionResponse.model_fields["active_handoff"].annotation
        ).validate_python(summary)
        assert embedded.target_worker_id is None
        assert str(s.link.authority_id) not in viewed.text
    assert len(s.native.execution.starts) == 1 and s.public.sent == []


@pytest.mark.parametrize("denial", ["revoked", "cross_scope", "identity", "services"])
def test_native_active_summary_authorizes_original_scope_before_disclosure(
    intervention, database_url, denial
):
    s = intervention
    prepared = s.commands.handle(native_prepare_command(s))
    with s.native.uow() as uow:
        proposal = uow.handoff_proposals.get(
            prepared.aggregate_id, s.native.workspace_id
        )
    confirmed = s.commands.handle(native_confirm_command(s, proposal))
    service = dependencies.get_handoff_public_query_service(
        s.native.cp.database,
        Settings(local_actor_id=s.native.principal),
        native_services=None if denial == "services" else s.native.services,
        assignment_identity=None
        if denial == "identity"
        else TrustedAssignmentIdentity(s.native.principal, s.native.setup_grant),
        assignment_authorizer=s.native.assignment._authorizer,
        agent_catalog=s.native.catalog,
        agent_runtimes=s.native.runtimes,
    )
    if denial == "revoked":
        s.native.assignment._authorizer.revoked = True
    elif denial == "cross_scope":
        s.native.assignment._authorizer.workspace_id = uuid4()
    with pytest.raises(DomainError) as denied:
        service.get_worker_item_handoff(s.native.workspace_id, s.item.id)
    assert denied.value.code == "assignment_forbidden"
    with native_api_client(s, database_url) as client:
        if denial == "identity":
            client.app.dependency_overrides[
                dependencies.get_trusted_assignment_identity
            ] = lambda: None
        elif denial == "services":
            client.app.dependency_overrides[dependencies.get_native_worker_services] = (
                lambda: None
            )
        viewed = client.get(
            f"/api/v1/workspaces/{s.native.workspace_id}/worker-items/{s.item.id}/handoff"
        )
        assert viewed.status_code == 409, viewed.text
        assert viewed.json()["error"]["code"] == "assignment_forbidden"
        assert str(s.link.authority_id) not in viewed.text
    with s.native.uow() as uow:
        assert (
            uow.handoff_transitions.get(
                confirmed.aggregate_id, s.native.workspace_id
            ).version
            == confirmed.aggregate_version
        )
    assert len(s.native.execution.starts) == 1 and s.public.sent == []


def test_native_active_summary_block_does_not_advertise_legacy_retry(
    intervention, database_url, monkeypatch
):
    from sidera_control_plane.persistence.handoff_query_store import (
        SqlAlchemyHandoffQueryStore,
    )

    s = intervention
    stops, worker, transition_id = native_profile_at_ack(s, monkeypatch)
    with s.native.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, s.native.workspace_id)
        link = uow.execution_authorities.get_link(
            s.native.workspace_id, transition.target_execution_id
        )
    s.native.execution.events[link.run_id][0] = s.native.execution.events[link.run_id][
        0
    ].model_copy(update={"operation_id": uuid4()})
    assert worker.coordinator.run_once(1).blocked == 1
    projection = SqlAlchemyHandoffQueryStore(s.native.cp.database).load(
        s.native.workspace_id, s.item.id
    )
    assert projection.status == "blocked"
    assert projection.failure_code == "native_handoff_start_invalid"
    assert (
        projection.attention_required and projection.available_recovery_strategies == ()
    )
    with native_api_client(s, database_url) as client:
        viewed = client.get(
            f"/api/v1/workspaces/{s.native.workspace_id}/worker-items/{s.item.id}/handoff"
        )
        assert viewed.status_code == 200, viewed.text
        assert viewed.json()["failure_code"] == "native_handoff_start_invalid"
        assert viewed.json()["attention_required"]
        assert viewed.json()["available_recovery_strategies"] == []
    assert len(s.native.execution.starts) == 2 and len(stops.sent) == 1


@pytest.mark.parametrize("replay", ["normal", "concurrent"])
@pytest.mark.parametrize("legacy", ["ConfirmWorkerHandoff", "RecoverWorkerHandoff"])
def test_legacy_confirmation_and_recovery_cannot_disclose_native_receipts(
    intervention, replay, legacy
):
    s = intervention
    prepared = s.commands.handle(native_prepare_command(s))
    with s.native.uow() as uow:
        proposal = uow.handoff_proposals.get(
            prepared.aggregate_id, s.native.workspace_id
        )
    command = replace(native_confirm_command(s, proposal), command_type=legacy)
    if legacy == "RecoverWorkerHandoff":
        confirmed = s.commands.handle(native_confirm_command(s, proposal))
        command = s.native.command(
            legacy,
            "handoff_transition",
            confirmed.aggregate_id,
            1,
            {
                "transition_id": str(confirmed.aggregate_id),
                "expected_transition_version": 1,
                "strategy": "retry_current_phase",
                "reason": "Inspect original action",
                "evidence_refs": [],
            },
        )
    with s.native.uow() as uow:
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command.id,
                aggregate_id=command.target_id,
                aggregate_version=1,
                event_ids=(),
                data={"receipt": "must-not-disclose"},
            ),
        )
    with pytest.raises(DomainError) as denied:
        if replay == "normal":
            s.commands.handle(command)
        else:
            s.commands._recover_concurrent(command, hash_request(command))
    assert denied.value.code in {
        "native_handoff_command_required",
        "native_handoff_recovery_unsupported",
    }
    assert not s.public.release.is_set()


def test_closed_first_native_input_creates_no_new_session_or_run(intervention):
    s = intervention
    s.native.execution.emit(s.link.run_id, "run.closed", state="completed")
    with pytest.raises(DomainError):
        s.commands.handle(s.command())
    assert s.public.sent == []
    assert len(s.native.execution.starts) == 1


@pytest.mark.parametrize("field", ["run_id", "operation_id", "evidence_sequence"])
def test_mismatched_or_unproven_receipt_never_delivers(intervention, field):
    s = intervention
    accepted = s.commands.handle(s.command())
    s.dispatcher.run_once(batch_size=1)
    pending = s.input(accepted.aggregate_id)
    original = s.public.receipts[pending.operation_id]
    changes = {"status": "delivered", "evidence_sequence": 7}
    changes[field] = None if field == "evidence_sequence" else uuid4()
    s.public.receipts[pending.operation_id] = InputReceipt.model_validate(
        {**original.model_dump(), **changes}
    )
    assert s.dispatcher.run_once(batch_size=1).delivered == 0
    item, operation, events, _ = s.facts(pending.id)
    assert (
        item.status.value == "dispatching" and item.operation_id == pending.operation_id
    )
    assert operation.status.value == "running" and events == ()
    s.public.receipts[pending.operation_id] = original
    s.public.delivered(pending.operation_id)
    restarted = InputDispatcher(s.native.uow, s.native.gateway, clock=s.native.cp.clock)
    assert restarted.run_once(batch_size=1).delivered == 1
    assert len(s.public.sent) == 1 and len(s.native.execution.starts) == 1


@pytest.mark.parametrize("case", ["wrong_run", "missing_evidence", "agent_error"])
def test_invalid_admission_receipt_is_query_only_uncertainty(intervention, case):
    s = intervention
    s.public.admission_agent_error = case == "agent_error"
    s.public.admission_receipt_override = (
        None
        if case == "agent_error"
        else (
            {"run_id": uuid4()}
            if case == "wrong_run"
            else {"status": "delivered", "evidence_sequence": None}
        )
    )
    accepted = s.commands.handle(s.command())
    s.dispatcher.run_once(batch_size=1)
    pending, operation, events, _ = s.facts(accepted.aggregate_id)
    assert pending.status.value == "dispatching"
    assert operation.status.value == "running" and events == ()
    s.public.delivered(pending.operation_id)
    assert s.dispatcher.run_once(batch_size=1).delivered == 1
    assert len(s.public.sent) == 1


def test_uncertain_native_manual_retry_cannot_clear_original_operation(intervention):
    s = intervention
    accepted = s.commands.handle(s.command())
    s.public.lose_admission_ack = True
    s.dispatcher.run_once(batch_size=1)
    pending = s.input(accepted.aggregate_id)
    command = s.native.command(
        "RetryBlockedExecutionInput",
        "execution_session",
        s.session.id,
        s.session.version,
        {"execution_id": str(s.execution.id), "client_idempotency_key": "input-1"},
    )
    with pytest.raises(DomainError) as denied:
        s.commands.handle(command)
    assert denied.value.code == "input_reconciliation_required"
    assert s.input(pending.id).operation_id == pending.operation_id
    assert len(s.public.sent) == 1


def test_delivery_event_failure_rolls_back_input_and_gateway_outcome(
    intervention, monkeypatch
):
    s = intervention
    accepted = s.commands.handle(s.command())
    s.dispatcher.run_once(batch_size=1)
    pending = s.input(accepted.aggregate_id)
    s.public.delivered(pending.operation_id)
    append = EventRepository.append_with_outbox

    def fail_delivery(self, event):
        if event.event_type == "ExecutionInputDelivered":
            raise RuntimeError("controlled event write failure")
        return append(self, event)

    monkeypatch.setattr(EventRepository, "append_with_outbox", fail_delivery)
    with pytest.raises(RuntimeError, match="controlled event write failure"):
        s.dispatcher.run_once(batch_size=1)
    item, operation, events, _ = s.facts(pending.id)
    assert item.status.value == "dispatching" and item.version == pending.version
    assert operation.status.value == "running" and events == ()
    monkeypatch.setattr(EventRepository, "append_with_outbox", append)
    assert s.dispatcher.run_once(batch_size=1).delivered == 1
    assert len(s.facts(pending.id)[2]) == 1 and len(s.public.sent) == 1


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "binding_version",
        "session_id",
        "session_version",
        "worker_id",
        "worker_item_id",
        "plan_generation",
        "context_version",
        "workspace_id",
        "revoked",
    ],
)
def test_native_command_stale_authority_denied_before_receipt_read(
    intervention, monkeypatch, field
):
    s = intervention
    command = s.command()
    if field == "workspace_id":
        command = replace(command, workspace_id=uuid4())
    elif field == "revoked":
        s.native.denied_worker_actions.add("run.input")
    else:
        context = dict(command.payload["context"])
        context[field] = (
            context[field] + 1
            if field.endswith("version") or field == "plan_generation"
            else str(uuid4())
        )
        command = replace(command, payload={**command.payload, "context": context})

    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized command must not disclose the native Run receipt")

    monkeypatch.setattr(s.native.execution, "get_run", forbidden)
    with pytest.raises((DomainError, NotFound)):
        s.commands.handle(command)
    assert s.public.sent == []


def test_selected_authorized_attachment_preserves_pin_and_reads_once(
    intervention, monkeypatch
):
    s = intervention
    reader = s.native.gateway._native_source_reader
    with s.native.uow() as uow:
        authority = uow.execution_authorities.get(s.link.authority_id)
    selected = authority.sources[0]
    reads = []
    read = reader.read

    def observed_read(grant_id, ref):
        reads.append(ref)
        return read(grant_id, ref)

    monkeypatch.setattr(reader, "read", observed_read)
    accepted = s.commands.handle(s.command(attachments=(selected.key,)))
    s.dispatcher.run_once(batch_size=1)
    assert len(s.public.sent) == 1
    item = next(
        item for item in s.public.sent[0].context if item.source_ref == selected
    )
    assert item.required and item.version == selected.version
    assert item.content_hash == "sha256:" + selected.version.rsplit(":", 1)[1]
    assert reads.count(selected) == 1
    s.dispatcher.run_once(batch_size=1)
    assert (
        reads.count(selected) == 1
        and s.input(accepted.aggregate_id).status.value == "dispatching"
    )


@pytest.mark.parametrize(
    "attachment", ["unknown", "https://example.invalid/input", "/tmp/native-file"]
)
def test_unregistered_attachment_denies_before_context_materialization(
    intervention, monkeypatch, attachment
):
    s = intervention

    def forbidden(*args, **kwargs):
        pytest.fail("unregistered attachment must be rejected before body reads")

    monkeypatch.setattr(
        s.native.gateway._native_source_reader, "context_items", forbidden
    )
    accepted = s.commands.handle(s.command(attachments=(attachment,)))
    assert s.dispatcher.run_once(batch_size=1).delivered == 0
    assert s.public.sent == []
    assert (
        s.input(accepted.aggregate_id).failure_code
        == "native_input_attachment_unavailable"
    )


def test_authority_revoked_during_source_io_prevents_admission(
    intervention, monkeypatch
):
    s = intervention
    reader = s.native.gateway._native_source_reader
    context_items = reader.context_items

    def revoke_after_read(*args, **kwargs):
        result = context_items(*args, **kwargs)
        s.native.denied_worker_actions.add("run.input")
        return result

    monkeypatch.setattr(reader, "context_items", revoke_after_read)
    accepted = s.commands.handle(s.command())
    assert s.dispatcher.run_once(batch_size=1).delivered == 0
    assert s.public.sent == []
    assert s.input(accepted.aggregate_id).status.value == "blocked"


def test_active_input_builder_does_not_reuse_runstart_authority(intervention):
    s = intervention
    accepted = s.commands.handle(s.command())
    with s.native.uow() as uow:
        (pending,) = uow.execution_inputs.claim_queued(1)
        binding = uow.agent_bindings.get(
            s.native.workspace_id, "worker_item", s.item.id
        )
    reader = s.native.gateway._native_source_reader
    builder = TaskExecutionInputBuilder(
        s.native.uow,
        authorizer=reader._authorizer,
        source_reader=reader,
        catalog=s.native.catalog,
        runtimes=s.native.runtimes,
    )
    with pytest.raises(AgentError):
        builder.build(s.execution.id, binding, s.link.authority_id)
    request = builder.build_input(
        s.execution.id,
        binding,
        s.link.authority_id,
        operation_id=pending.operation_id,
        text=pending.text,
        attachment_refs=pending.attachment_refs,
    )
    assert request.run_id == s.link.run_id and request.text == pending.text
    assert request.operation_id == input_operation_id(accepted.aggregate_id)
    assert s.public.sent == [] and len(s.native.execution.starts) == 1


def test_concurrent_pending_queries_commit_one_delivery_fact(intervention, monkeypatch):
    s = intervention
    accepted = s.commands.handle(s.command())
    s.dispatcher.run_once(batch_size=1)
    pending = s.input(accepted.aggregate_id)
    s.public.delivered(pending.operation_id)
    dispatchers = [
        InputDispatcher(s.native.uow, s.native.gateway, clock=s.native.cp.clock)
        for _ in range(2)
    ]
    entered = Event()
    overlapping_reads = Barrier(2, timeout=5)
    get_input = s.native.execution.get_input

    def overlapping_get_input(*args, **kwargs):
        entered.set()
        overlapping_reads.wait()
        return get_input(*args, **kwargs)

    monkeypatch.setattr(s.native.execution, "get_input", overlapping_get_input)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(dispatchers[0].run_once, batch_size=1)
            assert entered.wait(5)
            second = pool.submit(dispatchers[1].run_once, batch_size=1)
            results = [first.result(timeout=10), second.result(timeout=10)]
        finally:
            overlapping_reads.abort()
    assert sum(result.delivered for result in results) == 1
    item, operation, events, _ = s.facts(pending.id)
    assert item.status.value == "delivered" and operation.status.value == "succeeded"
    assert len(events) == 1
    assert len(s.public.sent) == 1 and len(s.public.queried) == 2


@pytest.mark.parametrize("preparation", ["terminal", "missing", "malformed"])
def test_actual_transfer_guard_uses_canonical_native_capture(
    intervention, additional_interventions, monkeypatch, preparation
):
    s = intervention
    accepted = s.commands.handle(s.command())
    target = additional_interventions()
    with s.native.uow() as uow:
        capture_id = capture_operation_for_execution(
            uow,
            workspace_id=s.native.workspace_id,
            execution_id=target.execution.id,
        )
        authority = uow.execution_authorities.get(target.link.authority_id)
    s.native.execution.emit(target.link.run_id, "run.completed", state="completed")
    s.native.capture.observe_terminal(
        workspace_id=s.native.workspace_id,
        execution_id=target.execution.id,
        operation_id=capture_id,
        outcome="succeeded",
        fencing_token=authority.fencing_token,
        details={"run_id": str(target.link.run_id), "event_type": "run.completed"},
    )
    if preparation != "terminal":
        get_link = ExecutionAuthorityRepository.get_link

        def unavailable_preparation(self, workspace_id, execution_id, **kwargs):
            link = get_link(self, workspace_id, execution_id, **kwargs)
            return replace(link, preparation=None if preparation == "missing" else {})

        monkeypatch.setattr(
            ExecutionAuthorityRepository, "get_link", unavailable_preparation
        )
    expected_code = {
        "terminal": "handoff_input_target_finalizing",
        "missing": "context_package_missing",
        "malformed": "context_hash_mismatch",
    }[preparation]
    with s.native.uow() as uow, pytest.raises((DomainError, ResultError)) as denied:
        source = uow.execution_inputs.get(accepted.aggregate_id, s.native.workspace_id)
        HandoffInputTransferService._transfer_one(
            uow,
            SimpleNamespace(id=uuid4(), workspace_id=s.native.workspace_id),
            source,
            target_execution=target.execution,
            target_session_id=target.session.id,
            now=s.native.cp.now,
        )
    assert denied.value.code == expected_code
    assert s.input(accepted.aggregate_id).status.value == "queued"
    assert s.input(accepted.aggregate_id).superseded_reason is None


@pytest.mark.parametrize("path", ["normal", "concurrent"])
def test_original_command_receipt_replays_after_authoritative_close(
    intervention, monkeypatch, path
):
    s = intervention
    command = s.command()
    accepted = s.commands.handle(command)
    s.dispatcher.run_once(batch_size=1)
    s.public.delivered(input_operation_id(accepted.aggregate_id))
    assert s.dispatcher.run_once(batch_size=1).delivered == 1
    s.public.complete_original_turn()
    s.native.execution.emit(s.link.run_id, "run.closed", state="completed")
    s.native.gateway.sync_active_sessions(s.native.workspace_id)
    with s.native.uow() as uow:
        assert (
            uow.sessions.get_any(s.session.id, s.native.workspace_id).status.value
            == "closed"
        )

    def forbidden(*args, **kwargs):
        pytest.fail("historical command receipt must not require Run/body I/O")

    monkeypatch.setattr(s.native.execution, "get_run", forbidden)
    monkeypatch.setattr(
        s.native.gateway._native_source_reader, "context_items", forbidden
    )
    replay = (
        s.commands.handle(command)
        if path == "normal"
        else s.commands._recover_concurrent(command, hash_request(command))
    )
    assert replay == accepted
    assert len(s.public.sent) == 1 and len(s.native.execution.starts) == 1


@pytest.mark.parametrize("path", ["normal", "concurrent"])
@pytest.mark.parametrize(
    "case", ["actor", "target_type", "workspace", "revoked", "upstream"]
)
def test_original_receipt_scope_is_authorized_before_lookup(
    intervention, monkeypatch, path, case
):
    s = intervention
    command = s.command()
    s.commands.handle(command)
    if case == "actor":
        command = replace(command, actor_id="forged")
    elif case == "target_type":
        command = replace(command, target_type="workspace")
    elif case == "workspace":
        command = replace(command, workspace_id=uuid4())
    elif case == "revoked":
        with s.native.uow() as uow:
            uow.execution_authorities.revoke(s.link.authority_id, s.native.workspace_id)
    else:
        s.native.denied_worker_actions.add("run.input")

    from sidera_control_plane.persistence.repositories import CommandRepository

    def forbidden(*args, **kwargs):
        pytest.fail(
            "unauthorized caller must be denied before receipt lookup or Run I/O"
        )

    monkeypatch.setattr(CommandRepository, "find_by_id", forbidden)
    monkeypatch.setattr(CommandRepository, "find_duplicate", forbidden)
    monkeypatch.setattr(s.native.execution, "get_run", forbidden)
    with pytest.raises((DomainError, NotFound)) as denied:
        if path == "normal":
            s.commands.handle(command)
        else:
            s.commands._recover_concurrent(command, hash_request(command))
    assert denied.value.code != "idempotency_conflict"
    assert s.public.sent == []


def test_default_batch_selects_each_new_input_once_without_immediate_query(
    intervention,
):
    s = intervention
    accepted = s.commands.handle(s.command())
    report = s.dispatcher.run_once()
    assert (report.claimed, report.accepted, report.queried) == (1, 1, 0)
    assert len(s.public.sent) == 1 and s.public.queried == []
    assert s.dispatcher.run_once().queried == 1
    assert s.input(accepted.aggregate_id).status.value == "dispatching"


def test_pending_rotation_survives_fresh_dispatchers(
    intervention, additional_interventions, monkeypatch
):
    scenarios = [intervention, additional_interventions(), additional_interventions()]
    inputs = []
    for s in scenarios:
        accepted = s.commands.handle(s.command())
        s.dispatcher.run_once(batch_size=1)
        inputs.append(s.input(accepted.aggregate_id))
    for s, item in zip(scenarios[:2], inputs[:2], strict=True):
        receipt = s.public.receipts[item.operation_id]
        s.public.receipts[item.operation_id] = InputReceipt.model_validate(
            {**receipt.model_dump(), "status": "unknown"}
        )
    scenarios[-1].public.delivered(inputs[-1].operation_id)
    for _ in range(3):
        dispatcher = InputDispatcher(
            intervention.native.uow,
            intervention.native.gateway,
            clock=intervention.native.cp.clock,
        )
        report = dispatcher.run_once(batch_size=1)
        assert report.claimed <= 1 and report.queried == 1
    assert scenarios[-1].input(inputs[-1].id).status.value == "delivered"
    versions = [
        s.input(item.id).version
        for s, item in zip(scenarios[:2], inputs[:2], strict=True)
    ]
    for _ in range(3):
        InputDispatcher(
            intervention.native.uow,
            intervention.native.gateway,
            clock=intervention.native.cp.clock,
        ).run_once(batch_size=1)
    assert [
        s.input(item.id).version
        for s, item in zip(scenarios[:2], inputs[:2], strict=True)
    ] == versions
    assert len(intervention.public.sent) == 3


def test_bounded_mixed_batch_services_queued_and_pending(
    intervention, additional_interventions, monkeypatch
):
    s = intervention
    old = s.commands.handle(s.command())
    s.dispatcher.run_once(batch_size=1)
    later = additional_interventions()
    new = later.commands.handle(later.command())
    report = s.dispatcher.run_once(batch_size=2)
    assert report.claimed == 2 and report.queried == 1 and report.accepted == 2
    assert len(s.public.queried) == 1 and s.public.queried[0][1] == s.link.run_id
    assert len(s.public.sent) == 2 and s.public.sent[-1].run_id == later.link.run_id
    assert (
        s.input(old.aggregate_id).status.value
        == later.input(new.aggregate_id).status.value
        == "dispatching"
    )


@pytest.mark.parametrize("cutoff_sequence", [2, 3])
@pytest.mark.parametrize("receipt_sequence", [3, 4])
def test_native_delivery_proof_survives_actual_source_input_classification(
    intervention, cutoff_sequence, receipt_sequence
):
    s = intervention
    accepted = s.commands.handle(s.command())
    s.dispatcher.run_once(batch_size=1)
    operation_id = input_operation_id(accepted.aggregate_id)
    s.native.execution.emit(s.link.run_id, "input.delivered")
    s.public.delivered(operation_id, sequence=receipt_sequence)
    assert s.dispatcher.run_once(batch_size=1).delivered == 1
    s.native.gateway.sync_active_sessions(s.native.workspace_id)
    with s.native.uow() as uow:
        assert (
            uow.sessions.get_any(s.session.id, s.native.workspace_id).last_sequence == 3
        )
        classification = HandoffInputTransferService(s.native.uow)._classify_in_uow(
            uow,
            SimpleNamespace(
                id=uuid4(),
                workspace_id=s.native.workspace_id,
                worker_item_id=s.item.id,
                source_execution_id=s.execution.id,
                source_session_id=s.session.id,
                source_input_cutoff_sequence=cutoff_sequence,
                status="running",
                confirmed_at=s.native.cp.now,
            ),
            now=s.native.cp.now,
        )
    (disposition,) = classification.dispositions
    assert disposition.source_input_id == accepted.aggregate_id
    if receipt_sequence > 3:
        assert classification.unknown_input_ids == (accepted.aggregate_id,)
        assert disposition.action is HandoffInputAction.BLOCK
        return
    assert classification.unknown_input_ids == ()
    assert disposition.action is HandoffInputAction.PRESERVE_DELIVERED
    assert (
        disposition.accepted_sequence == 3 and disposition.operation_id == operation_id
    )
