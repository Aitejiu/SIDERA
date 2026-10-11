import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import StopReceipt
from sqlalchemy import select, text

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.worker_gateway import (
    STOP_TOPIC,
    GatewayError,
    GatewayRecoveryRequired,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.persistence.models import EventModel, OutboxMessageModel
from tests.integration.test_agent_dispatch import NativeScenario
from tests.integration.test_agent_reconciliation import restarted_gateway
from tests.support.native_agents import (
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)

pytestmark = pytest.mark.integration


def test_public_schema_readiness_and_owned_migration_are_inert(database, monkeypatch):
    from fastapi.testclient import TestClient

    from sidera_control_plane.composition.native_runtime import (
        create_control_plane_runtime,
    )
    from sidera_control_plane.config import Settings
    from tests.unit.test_native_request_authority import NoNativeCalls

    native = NoNativeCalls()
    runtime = create_control_plane_runtime(
        Settings(
            database_url=database.engine.url.render_as_string(hide_password=False)
        ),
        native,
        native,
        native,
        native,
    )
    with database.engine.connect() as connection:
        head_before = tuple(
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars()
        )
        events_before = connection.scalar(select(EventModel.id).limit(1))
    monkeypatch.setenv(
        "SIDERA_DATABASE_URL",
        "postgresql+psycopg://PRIVATE:credential@not-owned/forbidden",
    )
    try:
        assert runtime.readiness() is True
        with TestClient(runtime.app) as client:
            assert client.get("/healthz").status_code == 200
        runtime.migrate()
        assert runtime.readiness() is True
        with database.engine.connect() as connection:
            assert (
                tuple(
                    connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalars()
                )
                == head_before
            )
            assert connection.scalar(select(EventModel.id).limit(1)) == events_before
        assert not native.calls
    finally:
        runtime.close()


def test_public_cp_authorizer_does_not_treat_upstream_grant_as_cp_authority(
    database, workspace_id
):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.composition.native_runtime import (
        create_control_plane_runtime,
    )
    from sidera_control_plane.config import Settings
    from tests.unit.test_native_request_authority import NoNativeCalls

    lookups = []
    upstream_grant = uuid4()
    host = SimpleNamespace(identity_for_grant=lambda grant: lookups.append(grant))
    native = NoNativeCalls()
    runtime = create_control_plane_runtime(
        Settings(
            database_url=database.engine.url.render_as_string(hide_password=False)
        ),
        native,
        native,
        native,
        native,
        host,
    )
    try:
        authorizer = runtime.execution_authorizer
        assert not lookups and not native.calls
        for grant in (upstream_grant, uuid4()):
            with pytest.raises(AgentError) as denied:
                authorizer.require(grant, "run.read", workspace_id)
            assert denied.value.code == "permission_denied"
        assert not lookups and not native.calls
    finally:
        runtime.close()


class OrdinaryStopControl:
    def __init__(self, scenario, monkeypatch):
        self.scenario = scenario
        self.sent, self.queried, self.receipts = [], [], {}
        self.lose_ack = False
        self.query_error = None
        self.after_query = None
        monkeypatch.setattr(scenario.execution, "stop", self.stop, raising=False)
        monkeypatch.setattr(
            scenario.execution, "get_stop", self.get_stop, raising=False
        )

    def stop(self, request):
        s = self.scenario
        with s.uow() as uow:
            operation = uow.gateway_operations.get_by_operation_id(
                request.operation_id, s.workspace_id
            )
        assert operation.status.value == "running"
        assert operation.operation_type is GatewayOperationType.STOP_SESSION
        assert operation.execution_id == s.link().execution_id
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
            raise TimeoutError("controlled ordinary Stop reply loss")
        return receipt

    def get_stop(self, workspace_id, run_id, operation_id, *, grant_id):
        self.queried.append((workspace_id, run_id, operation_id, grant_id))
        if self.query_error is not None:
            raise self.query_error
        receipt = self.receipts[operation_id]
        if self.after_query is not None:
            self.after_query()
        return receipt

    def closed(self):
        s = self.scenario
        request = self.sent[0]
        s.execution.emit(request.run_id, "run.stopped", state="stopped")
        self.receipts[request.operation_id] = StopReceipt.model_validate(
            {
                **self.receipts[request.operation_id].model_dump(),
                "status": "stopped",
                "evidence_sequence": len(s.execution.events[request.run_id]),
            }
        )


@pytest.fixture
def ordinary_native_stop(database, workspace_id, tmp_path, monkeypatch):
    s = NativeScenario(database, workspace_id, tmp_path)
    control = OrdinaryStopControl(s, monkeypatch)
    s.assign()
    assert s.schedule().assigned == 1
    started = s.start()
    assert s.gateway.sync_active_sessions(s.workspace_id) == 1
    with s.uow() as uow:
        execution = uow.executions.get(started.execution_id, s.workspace_id)
    command = s.command(
        "StopExecution", "execution", execution.id, execution.version, {}
    )
    CommandService(s.uow, (ExecutionCommandHandler(s.cp.clock),)).handle(command)
    with s.uow() as uow:
        row = next(
            row
            for row in uow.session.scalars(
                select(OutboxMessageModel).where(OutboxMessageModel.topic == STOP_TOPIC)
            )
            if row.payload["workspace_id"] == str(s.workspace_id)
        )
        message = OutboxMessage(
            row.id, row.event_id, row.topic, dict(row.payload), 1, uuid4()
        )
    return SimpleNamespace(
        s=s, control=control, message=message, started=started, command=command
    )


def stop_facts(case):
    s = case.s
    link = s.link()
    with s.uow() as uow:
        authority = uow.execution_authorities.get(link.authority_id)
        return (
            link,
            uow.executions.get(link.execution_id, s.workspace_id),
            uow.sessions.get_any(case.started.session_id, s.workspace_id),
            uow.leases.get(authority.lease_id, s.workspace_id),
            uow.workers.get(s.worker_id, s.workspace_id),
            uow.gateway_operations.get_by_operation_id(
                gateway_operation_id(
                    link.execution_id, GatewayOperationType.STOP_SESSION
                ),
                s.workspace_id,
            ),
        )


def cleanup_counts(case):
    with case.s.uow() as uow:
        return Counter(
            uow.session.scalars(
                select(EventModel.event_type).where(
                    EventModel.workspace_id == case.s.workspace_id,
                    EventModel.event_type.in_(
                        (
                            "ExecutionCanceledByStop",
                            "ExecutionSessionClosed",
                            "LeaseReleasedByStop",
                        )
                    ),
                )
            )
        )


@pytest.mark.parametrize("lost_reply", (False, True))
def test_ordinary_pending_stop_restart_queries_original_and_cleans_up_once(
    ordinary_native_stop,
    monkeypatch,
    lost_reply,
):
    case = ordinary_native_stop
    s, control = case.s, case.control
    control.lose_ack = lost_reply
    with pytest.raises(GatewayRecoveryRequired):
        s.gateway.handle(case.message)
    before = stop_facts(case)
    assert before[-1].status.value == "unknown" and not cleanup_counts(case)
    control.closed()
    with restarted_gateway(s, monkeypatch) as runtime:
        result = runtime.worker_gateway.handle(case.message)
        assert (
            result.operation_id == before[-1].operation_id
            and result.accepted_sequence == 2
        )
        after = stop_facts(case)
        assert after[0].event_cursor == after[2].last_sequence == 2
        assert after[1].status.value == "canceled" and after[1].active_lease_id is None
        assert after[2].status.value == "closed" and after[3].status.value == "released"
        assert after[4].status.value == "idle"
        assert after[-1].status.value == "succeeded"
        assert (after[-1].id, after[-1].operation_id, after[-1].attempt_count) == (
            before[-1].id,
            before[-1].operation_id,
            1,
        )
        assert cleanup_counts(case) == Counter(
            ExecutionCanceledByStop=1, ExecutionSessionClosed=1, LeaseReleasedByStop=1
        )
        assert runtime.worker_gateway.handle(case.message) == result
        assert stop_facts(case) == after
    assert len(control.sent) == len(s.execution.starts) == 1
    assert control.queried == [
        (
            s.workspace_id,
            before[0].run_id,
            before[-1].operation_id,
            before[0].authority_id,
        )
    ]


def test_failed_original_stop_is_terminal_without_resend_or_release(
    ordinary_native_stop,
):
    case = ordinary_native_stop
    with pytest.raises(GatewayRecoveryRequired):
        case.s.gateway.handle(case.message)
    before = stop_facts(case)
    receipt = case.control.receipts[before[-1].operation_id]
    case.control.receipts[receipt.operation_id] = StopReceipt.model_validate(
        {**receipt.model_dump(), "status": "failed"}
    )
    with pytest.raises(GatewayError, match="stop failed"):
        case.s.gateway.handle(case.message)
    after = stop_facts(case)
    assert after[-1].status.value == "failed" and after[-1].attempt_count == 1
    assert after[:-1] == before[:-1] and not cleanup_counts(case)
    with pytest.raises(GatewayRecoveryRequired, match="original native Stop failed"):
        case.s.gateway.handle(case.message)
    assert stop_facts(case) == after and len(case.control.sent) == 1


@pytest.mark.parametrize("failure", ("pending", "timeout", "missing"))
def test_unconfirmed_original_stop_stays_owned_without_resend(
    ordinary_native_stop, failure
):
    from sidera_agents.errors import AgentError

    case = ordinary_native_stop
    with pytest.raises(GatewayRecoveryRequired):
        case.s.gateway.handle(case.message)
    before = stop_facts(case)
    if failure != "pending":
        case.control.query_error = (
            TimeoutError("PRIVATE")
            if failure == "timeout"
            else AgentError("not_found", "PRIVATE")
        )
    with pytest.raises(GatewayRecoveryRequired):
        case.s.gateway.handle(case.message)
    assert len(case.control.queried) == 1
    assert stop_facts(case) == before and not cleanup_counts(case)
    assert len(case.control.sent) == len(case.s.execution.starts) == 1


@pytest.mark.parametrize("loss_at", ("query", "operation_save"))
def test_detected_owner_loss_cannot_commit_original_stop_cleanup(
    ordinary_native_stop,
    monkeypatch,
    loss_at,
):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.persistence.gateway_repository import (
        GatewayOperationRepository,
    )

    case = ordinary_native_stop
    with pytest.raises(GatewayRecoveryRequired):
        case.s.gateway.handle(case.message)
    before = stop_facts(case)
    case.control.closed()
    alive = [True]

    def lose_owner():
        alive[0] = False

    if loss_at == "query":
        case.control.after_query = lose_owner
    else:
        original = GatewayOperationRepository.save

        def save_then_lose_owner(repository, operation, previous_version):
            original(repository, operation, previous_version)
            if operation.operation_type is GatewayOperationType.STOP_SESSION:
                lose_owner()

        monkeypatch.setattr(GatewayOperationRepository, "save", save_then_lose_owner)
    with restarted_gateway(
        case.s, monkeypatch, owner_check=lambda: alive[0]
    ) as runtime:
        with pytest.raises(AgentError) as denied:
            runtime.worker_gateway.handle(case.message)
        assert denied.value.code == "gateway_owner_lost"
    after = stop_facts(case)
    assert after[1] == before[1] and after[3:] == before[3:]
    assert after[2].status.value == "active" and not cleanup_counts(case)
    if loss_at == "query":
        assert after == before
    else:
        assert after[0].event_cursor == after[2].last_sequence == 2
    assert (
        len(case.control.sent)
        == len(case.control.queried)
        == len(case.s.execution.starts)
        == 1
    )


def test_stop_cursor_save_failure_rolls_back_original_facts_before_cleanup(
    ordinary_native_stop,
    monkeypatch,
):
    from sidera_control_plane.persistence.execution_authority_repository import (
        ExecutionAuthorityRepository,
    )

    case = ordinary_native_stop
    with pytest.raises(GatewayRecoveryRequired):
        case.s.gateway.handle(case.message)
    before = stop_facts(case)
    case.control.closed()
    original = ExecutionAuthorityRepository.save_original_observation

    def save_then_fail(repository, *args, **kwargs):
        original(repository, *args, **kwargs)
        raise RuntimeError("controlled original cursor save failure")

    with restarted_gateway(case.s, monkeypatch) as runtime:
        with monkeypatch.context() as patch:
            patch.setattr(
                ExecutionAuthorityRepository,
                "save_original_observation",
                save_then_fail,
            )
            with pytest.raises(GatewayRecoveryRequired):
                runtime.worker_gateway.handle(case.message)
        assert stop_facts(case) == before and not cleanup_counts(case)
        with case.s.uow() as uow:
            assert (
                uow.execution_events.get(
                    case.started.session_id, 2, case.s.workspace_id
                )
                is None
            )
        assert runtime.worker_gateway.handle(case.message).accepted_sequence == 2
    assert len(case.control.sent) == len(case.s.execution.starts) == 1
    assert len(case.control.queried) == 2


def test_stop_native_sequence_gap_cannot_be_replaced_by_receipt_watermark(
    ordinary_native_stop,
    monkeypatch,
):
    case = ordinary_native_stop
    with pytest.raises(GatewayRecoveryRequired):
        case.s.gateway.handle(case.message)
    before = stop_facts(case)
    case.s.execution.emit(before[0].run_id, "message")
    case.control.closed()
    original = case.s.execution.list_events

    def omit_real_sequence(*args, **kwargs):
        page = original(*args, **kwargs)
        return page.model_copy(
            update={
                "events": tuple(event for event in page.events if event.sequence != 2)
            }
        )

    with monkeypatch.context() as patch:
        patch.setattr(case.s.execution, "list_events", omit_real_sequence)
        with pytest.raises(GatewayRecoveryRequired):
            case.s.gateway.handle(case.message)
    assert stop_facts(case) == before and not cleanup_counts(case)
    with case.s.uow() as uow:
        assert (
            uow.execution_events.get(case.started.session_id, 2, case.s.workspace_id)
            is None
        )
    assert case.s.gateway.handle(case.message).accepted_sequence == 3
    after = stop_facts(case)
    assert after[0].event_cursor == after[2].last_sequence == 3
    assert len(case.control.sent) == len(case.s.execution.starts) == 1


def test_succeeded_stop_replay_checks_original_cleanup_instead_of_status_only(
    ordinary_native_stop,
):
    from sidera_control_plane.domain.execution import ExecutionSessionStatus

    case = ordinary_native_stop
    with pytest.raises(GatewayRecoveryRequired):
        case.s.gateway.handle(case.message)
    case.control.closed()
    case.s.gateway.handle(case.message)
    with case.s.uow() as uow:
        session = uow.sessions.get_any(case.started.session_id, case.s.workspace_id)
        previous_version = session.version
        session.status = ExecutionSessionStatus.LOST
        session.version += 1
        uow.sessions.save(session, previous_version)
    before = stop_facts(case)
    with pytest.raises(GatewayRecoveryRequired):
        case.s.gateway.handle(case.message)
    assert stop_facts(case) == before
    assert cleanup_counts(case) == Counter(
        ExecutionCanceledByStop=1, ExecutionSessionClosed=1, LeaseReleasedByStop=1
    )
    assert (
        len(case.control.sent)
        == len(case.control.queried)
        == len(case.s.execution.starts)
        == 1
    )


def _wait_for(predicate, *, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        Event().wait(0.01)
    assert predicate(), "Controlled native lifecycle did not reach its boundary"


@pytest.fixture
def public_native_closing(database, workspace_id, tmp_path, monkeypatch, request):
    from sidera_control_plane import gateway_worker, planning_worker
    from sidera_control_plane.application import (
        agent_reconciliation,
        agent_sources,
        context_materialization,
        execution_authority,
    )
    from sidera_control_plane.composition import native_runtime
    from sidera_control_plane.config import Settings

    s = NativeScenario(database, workspace_id, tmp_path)
    control = OrdinaryStopControl(s, monkeypatch)
    s.assign()
    assert s.schedule().assigned == 1
    state = getattr(request, "param", "accepted")
    if state == "pending":
        from sidera_agents.errors import AgentError

        @contextmanager
        def no_admission():
            raise AgentError("control_plane_closed", "Controlled start fence is closed")
            yield

        with (
            restarted_gateway(
                s,
                monkeypatch,
                native_services=replace(s.services, start_admission=no_admission),
            ) as prepared,
            monkeypatch.context() as patch,
        ):
            patch.setattr(s, "gateway", prepared.worker_gateway)
            with pytest.raises(GatewayError) as denied:
                s.start()
            assert denied.value.code == "control_plane_closed"
        started = SimpleNamespace(execution_id=s.link().execution_id, session_id=None)
        assert s.link().preparation is not None and s.link().request_sha256 is None
    else:
        s.execution.next_status = state
        if state == "rejected":
            with pytest.raises(GatewayError) as denied:
                s.start()
            assert denied.value.code == "native_admission_rejected"
            started = SimpleNamespace(
                execution_id=s.link().execution_id, session_id=None
            )
        else:
            started = s.start()
        if state == "accepted":
            assert s.gateway.sync_active_sessions(s.workspace_id) == 1

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return s.cp.now

    for module in (
        gateway_worker,
        planning_worker,
        agent_reconciliation,
        agent_sources,
        context_materialization,
        execution_authority,
        native_runtime,
    ):
        monkeypatch.setattr(module, "datetime", FrozenDateTime, raising=False)

    identity = native_runtime.ControlPlaneIdentity(
        principal_id=s.principal, grant_id=uuid4(), actor_type="system"
    )
    permissions = []

    def require(grant_id, action, target):
        assert target.workspace_id == s.workspace_id
        assert target.principal_id == s.principal
        assert grant_id in {s.setup_grant, identity.grant_id}
        permissions.append((grant_id, action, target))

    host = SimpleNamespace(
        identity_for_grant=lambda grant: (
            identity if grant == identity.grant_id else None
        ),
        require=require,
    )
    runtime = native_runtime.create_control_plane_runtime(
        Settings(
            database_url=database.engine.url.render_as_string(hide_password=False),
            local_actor_id=s.principal,
            workspace_root=tmp_path / "native-worktrees",
            artifact_root=tmp_path / "result-input-artifacts",
            planning_temp_root=tmp_path / "planning",
            knowledge_candidate_spool_dir=tmp_path / "candidate-spool",
            assignment_lease_seconds=300,
            assignment_lease_renew_interval_seconds=0.02,
            gateway_poll_interval_seconds=0.01,
            planning_poll_interval_seconds=0.01,
            handoff_poll_interval_seconds=0.01,
            gateway_sync_workspace_id=None,
        ),
        s.execution,
        s.catalog,
        s.execution,
        s.services,
        host,
        agent_runtimes=s.runtimes,
    )
    drain_errors = []
    original_drain = runtime._check_drain

    def observed_drain():
        try:
            return original_drain()
        except Exception as exc:
            drain_errors.append((type(exc).__name__, str(getattr(exc, "orig", exc))))
            raise

    monkeypatch.setattr(runtime, "_check_drain", observed_drain)
    entered, release = Event(), Event()
    requested = []
    original_stop = control.stop

    def held_stop(request):
        requested.append(request)
        entered.set()
        assert release.wait(10), "Controlled original Stop was not released"
        return original_stop(request)

    monkeypatch.setattr(s.execution, "stop", held_stop)
    case = SimpleNamespace(
        s=s,
        control=control,
        started=started,
        runtime=runtime,
        identity=identity,
        entered=entered,
        release=release,
        requested=requested,
        state=state,
        drain_errors=drain_errors,
        permissions=permissions,
    )
    try:
        yield case
    finally:
        release.set()
        if not runtime.close(grace_seconds=0.1):
            # Failed REDs still release only their test-owned background handles.
            runtime._stop.set()
            for thread in runtime._threads:
                thread.join(timeout=5)
            assert not any(thread.is_alive() for thread in runtime._threads)
            if runtime._planning is not None:
                runtime._planning.catalog.close()
                runtime._planning.database.dispose()
            if runtime._owner is not None:
                runtime._owner.close()
            runtime._database.dispose()


def test_public_close_without_hint_keeps_original_worker_and_queries_one_stop(
    public_native_closing,
):
    case = public_native_closing
    runtime, s = case.runtime, case.s
    before = stop_facts(case)
    foreign_workspace = uuid4()
    foreign_request = s.execution.starts[0].model_copy(
        update={
            "operation_id": uuid4(),
            "grant_id": uuid4(),
            "profile_ref": s.profile.ref.model_copy(
                update={"workspace_id": foreign_workspace}
            ),
            "policy": "conversation",
        }
    )
    foreign_run = s.execution.start(foreign_request).run_id
    assert foreign_run != before[0].run_id and before[-1] is None
    runtime.start_background(background_identity=case.identity)
    assert runtime.close(grace_seconds=0.02) is False
    assert case.entered.wait(3), "Public close did not request the existing Worker Stop"
    assert runtime.close(grace_seconds=0.02) is False
    assert runtime._owner.gateway_runtime.owner_check()
    _wait_for(lambda: len(s.execution.reads) >= 2)
    _wait_for(lambda: stop_facts(case)[3].version > before[3].version)
    pending = stop_facts(case)
    assert pending[1].stop_requested and pending[1].active_lease_id == before[3].id
    assert pending[2].status.value == pending[3].status.value == "active"
    assert pending[-1].status.value == "running" and not cleanup_counts(case)
    assert len(s.execution.starts) == 2 and len(case.requested) == 1
    assert s.execution.starts[0].operation_id == before[0].start_operation_id
    assert case.requested[0].run_id == before[0].run_id
    assert case.requested[0].operation_id == gateway_operation_id(
        before[0].execution_id, GatewayOperationType.STOP_SESSION
    )

    case.release.set()
    _wait_for(lambda: stop_facts(case)[-1].status.value == "unknown")
    assert runtime.close(grace_seconds=0.02) is False
    case.control.closed()
    s.cp.now += timedelta(seconds=3)
    assert runtime.close(grace_seconds=5) is True
    after = stop_facts(case)
    assert after[0].event_cursor == after[2].last_sequence == 2
    assert after[1].status.value == "canceled" and after[1].active_lease_id is None
    assert after[2].status.value == "closed" and after[3].status.value == "released"
    assert after[4].status.value == "idle" and after[-1].status.value == "succeeded"
    assert after[-1].attempt_count == 1
    assert cleanup_counts(case) == Counter(
        ExecutionCanceledByStop=1, ExecutionSessionClosed=1, LeaseReleasedByStop=1
    )
    assert len(case.control.sent) == len(case.requested) == 1
    assert len(s.execution.starts) == 2
    assert case.control.queried and {query[:3] for query in case.control.queried} == {
        (s.workspace_id, before[0].run_id, pending[-1].operation_id)
    }
    assert after[-1].operation_id == pending[-1].operation_id
    assert all(read[2] != foreign_run for read in s.execution.reads)
    assert all(read[1] != foreign_workspace for read in s.execution.reads)
    assert runtime.close(grace_seconds=0) is True
    assert stop_facts(case) == after
    assert not any(thread.is_alive() for thread in runtime._threads)


def reservation_facts(case):
    from sidera_control_plane.persistence.models import CommandModel

    s = case.s
    with s.uow() as uow:
        link = uow.execution_authorities.get_link(
            s.workspace_id, case.started.execution_id
        )
        authority = uow.execution_authorities.get(link.authority_id)
        return (
            link,
            authority,
            uow.executions.get(link.execution_id, s.workspace_id),
            uow.leases.get(authority.lease_id, s.workspace_id),
            uow.workers.get(s.worker_id, s.workspace_id),
            uow.execution_authorities.has_session(s.workspace_id, link.execution_id),
            tuple(
                uow.session.scalars(
                    select(CommandModel.id).where(
                        CommandModel.workspace_id == s.workspace_id,
                        CommandModel.command_type == "CancelExecution",
                        CommandModel.target_id == link.execution_id,
                    )
                )
            ),
            tuple(
                uow.session.scalars(
                    select(EventModel.event_type).where(
                        EventModel.workspace_id == s.workspace_id,
                        EventModel.event_type.in_(
                            {
                                "ExecutionCanceled",
                                "LeaseReleased",
                                "WorkerCapacityReleased",
                            }
                        ),
                    )
                )
            ),
        )


@pytest.mark.parametrize(
    "public_native_closing", ("pending", "busy", "rejected"), indirect=True
)
def test_public_close_locally_cancels_only_proved_unadmitted_original(
    public_native_closing,
):
    case = public_native_closing
    before = reservation_facts(case)
    native_before = (tuple(case.s.execution.starts), tuple(case.s.execution.reads))
    case.runtime.start_background(background_identity=case.identity)
    assert case.runtime.close(grace_seconds=3) is True, case.drain_errors[-3:]
    after = reservation_facts(case)
    assert after[0] == replace(before[0], status="cancelled")
    assert after[0].run_id is None and after[0].event_cursor == 0 and not after[5]
    assert after[1] == replace(before[1], revoked=True)
    assert after[2].status.value == "canceled" and after[2].active_lease_id is None
    assert after[2].started_at is None and not after[2].stop_requested
    assert after[3].status.value == "released" and after[4].current_load == 0
    assert len(after[6]) == 1 and Counter(after[7]) == Counter(
        ExecutionCanceled=1,
        LeaseReleased=1,
        WorkerCapacityReleased=1,
    )
    with case.s.uow() as uow:
        from sidera_control_plane.persistence.models import ExecutionEventModel

        assert not uow.execution_authorities.admission_history(
            case.s.workspace_id, after[0].execution_id
        )
        assert (
            uow.session.scalar(
                select(ExecutionEventModel.id).where(
                    ExecutionEventModel.workspace_id == case.s.workspace_id,
                    ExecutionEventModel.execution_id == after[0].execution_id,
                )
            )
            is None
        )
    assert (
        tuple(case.s.execution.starts),
        tuple(case.s.execution.reads),
    ) == native_before
    assert not case.control.sent and not case.control.queried
    assert case.runtime.close(grace_seconds=0) is True
    assert reservation_facts(case) == after


@pytest.mark.parametrize("public_native_closing", ("pending",), indirect=True)
@pytest.mark.parametrize("failure_at", ("after_handler", "after_link"))
def test_local_cancel_failure_rolls_back_original_and_real_command_together(
    public_native_closing,
    monkeypatch,
    failure_at,
):
    from sidera_control_plane.persistence.execution_authority_repository import (
        ExecutionAuthorityRepository,
    )

    case = public_native_closing
    before = reservation_facts(case)
    failed = Event()
    original = ExecutionAuthorityRepository.cancel_unadmitted_original

    def fail_after_business(repository, *args, **kwargs):
        if failure_at == "after_link":
            original(repository, *args, **kwargs)
        failed.set()
        raise RuntimeError("Controlled local cancellation persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            ExecutionAuthorityRepository,
            "cancel_unadmitted_original",
            fail_after_business,
        )
        case.runtime.start_background(background_identity=case.identity)
        assert case.runtime.close(grace_seconds=0.05) is False
        assert failed.wait(2), case.drain_errors[-3:]
        assert reservation_facts(case) == before
        assert not case.control.sent and not case.control.queried
    assert case.runtime.close(grace_seconds=5) is True, case.drain_errors[-3:]
    after = reservation_facts(case)
    assert after[0] == replace(before[0], status="cancelled")
    assert after[1].revoked and after[3].status.value == "released"
    assert after[4].current_load == 0 and not after[5]
    assert len(after[6]) == 1 and Counter(after[7]) == Counter(
        ExecutionCanceled=1,
        LeaseReleased=1,
        WorkerCapacityReleased=1,
    )
    assert case.runtime.close(grace_seconds=0) is True
    assert reservation_facts(case) == after and not case.s.execution.starts


@pytest.mark.parametrize("public_native_closing", ("unknown",), indirect=True)
@pytest.mark.parametrize("not_found", (False, True))
def test_public_close_unknown_start_retains_original_without_local_cancel_or_resend(
    public_native_closing,
    monkeypatch,
    not_found,
):
    from sidera_agents.contracts import StartLookup

    case = public_native_closing
    before = reservation_facts(case)
    if not_found:

        def original_not_found(workspace_id, operation_id, *, grant_id):
            case.s.execution.reads.append(
                ("start", workspace_id, operation_id, grant_id)
            )
            return StartLookup(found=False, receipt=None)

        monkeypatch.setattr(case.s.execution, "get_start", original_not_found)
    case.runtime.start_background(background_identity=case.identity)
    assert case.runtime.close(grace_seconds=0.05) is False
    _wait_for(lambda: any(read[0] == "start" for read in case.s.execution.reads))
    assert case.runtime.close(grace_seconds=0.05) is False
    after = reservation_facts(case)
    assert after[:3] == before[:3] and after[5:] == before[5:]
    assert after[3].status.value == "active" and after[4].current_load == 1
    assert not after[6] and not case.control.sent and not case.control.queried
    assert len(case.s.execution.starts) == 1
    assert {read[1:] for read in case.s.execution.reads} == {
        (case.s.workspace_id, before[0].start_operation_id, before[0].authority_id),
    }
    assert case.runtime._owner.gateway_runtime.owner_check()


@pytest.mark.parametrize("public_native_closing", ("pending",), indirect=True)
def test_pre_latch_native_start_late_link_is_drained_instead_of_locally_canceled(
    public_native_closing,
):
    case = public_native_closing
    original = reservation_facts(case)
    with case.s.uow() as uow:
        expected_worker_item = uow.executions.get(
            original[0].execution_id, case.s.workspace_id
        ).worker_item_id
    assert expected_worker_item is not None
    entered, release = Event(), Event()

    def held_start(request):
        assert request.operation_id == original[0].start_operation_id
        entered.set()
        assert release.wait(10), "Controlled admitted native Start was not released"

    case.s.execution.before_start = held_start
    try:
        case.runtime.start_background(background_identity=case.identity)
        assert entered.wait(3), "Actual Gateway did not enter the native admission"
        starts = [
            (grant, target)
            for grant, action, target in case.permissions
            if action == "run.start"
        ]
        assert starts and all(
            grant == case.s.setup_grant
            and target.target_type == "worker_item"
            and target.target_id == expected_worker_item
            for grant, target in starts
        )
        assert case.runtime.close(grace_seconds=0.02) is False
        pending = reservation_facts(case)
        assert pending[0].status == "starting" and pending[0].run_id is None
        assert pending[3].status.value == "active" and pending[4].current_load == 1
        assert not pending[6] and not case.s.execution.starts and not case.control.sent
        release.set()
        assert case.entered.wait(3), "Late original link was not included in drain"
        link = case.s.link()
        with case.s.uow() as uow:
            session = uow.sessions.get_active_for_execution(link.execution_id)
        case.started = SimpleNamespace(
            execution_id=link.execution_id, session_id=session.id
        )
        assert link.start_operation_id == original[0].start_operation_id
        assert case.requested[0].run_id == link.run_id
        assert case.runtime.close(grace_seconds=0.02) is False
        case.release.set()
        _wait_for(lambda: stop_facts(case)[-1].status.value == "unknown")
        case.control.closed()
        assert case.runtime.close(grace_seconds=5) is True
        after = stop_facts(case)
        assert after[0].event_cursor == after[2].last_sequence == 2
        assert (
            after[1].status.value == "canceled" and after[3].status.value == "released"
        )
        assert not reservation_facts(case)[6]
        assert (
            len(case.s.execution.starts)
            == len(case.control.sent)
            == len(case.requested)
            == 1
        )
        assert case.runtime.close(grace_seconds=0) is True
    finally:
        release.set()
