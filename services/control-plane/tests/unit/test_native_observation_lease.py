from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import ExecutionWorkspace, ProfileRef, RuntimeRef
from sidera_agents.errors import AgentError

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    GatewayRecoveryRequired,
    WorkerGateway,
)
from sidera_control_plane.domain.agent_bindings import (
    AgentExecutionLink,
    ExecutionAuthority,
)
from sidera_control_plane.domain.errors import StaleFencingToken
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.gateway import RuntimeModuleOperation
from sidera_control_plane.domain.lease import AssignmentLease

NOW = datetime(2026, 10, 8, 3, tzinfo=UTC)


class MutableClock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Records:
    def __init__(self, state, name):
        self.state, self.name = state, name

    def get(self, identifier, workspace_id, *, for_update=False):
        row = getattr(self.state, self.name)
        assert (identifier, workspace_id) == (row.id, row.workspace_id)
        self.state.reads.append((self.name, for_update))
        return row


class LeaseRecords(Records):
    def __init__(self, state):
        super().__init__(state, "lease")

    def get(self, identifier, workspace_id, *, for_update=False):
        lease = super().get(identifier, workspace_id, for_update=for_update)
        if for_update and self.state.lease_lock_error is not None:
            raise self.state.lease_lock_error
        return lease

    def save(self, lease, previous_version):
        assert previous_version == lease.version - 1
        self.state.lease = lease
        self.state.saved.append((previous_version, lease.version))


class MemoryUow:
    def __init__(self, state):
        self.state = state
        self.leases = LeaseRecords(state)
        self.executions = Records(state, "execution")
        self.execution_authorities = Records(state, "authority")
        self.commands = SimpleNamespace(record_applied=self.record_applied)
        self.events = SimpleNamespace(append_with_outbox=state.events.append)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def record_applied(self, command, request_hash, result):
        self.state.commands.append(command)
        self.state.command_records.append((command, request_hash, result))


def original_fixture(*, ttl=30, interval=10):
    workspace_id, execution_id, worker_id, lease_id = (uuid4() for _ in range(4))
    lease = AssignmentLease.issue(
        lease_id=lease_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=ttl),
        fencing_token=1,
        assignment_reason={"source": "original-observation-unit-test"},
    )
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=uuid4(),
        lineage_id=uuid4(),
        attempt=1,
        max_attempts=1,
        purpose="code",
        capability_requirements=frozenset({"code"}),
        pinned_worker_id=None,
        not_before=NOW,
    )
    execution.assign(lease_id=lease_id, worker_id=worker_id, fencing_token=1)
    execution.start(lease_id=lease_id, fencing_token=1, now=NOW)
    profile_ref = ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1)
    authority = ExecutionAuthority(
        id=uuid4(),
        workspace_id=workspace_id,
        principal_id="unit-test-owner",
        execution_id=execution_id,
        target_type="worker_item",
        target_id=uuid4(),
        binding_version=1,
        profile_ref=profile_ref,
        runtime_ref=RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4()),
        lease_id=lease_id,
        fencing_token=1,
        workspace_ref=uuid4(),
        execution_workspace=ExecutionWorkspace(
            directory="/unit-original", writable=True, artifact_directory=None
        ),
        actions=("run.read",),
    )
    link = AgentExecutionLink(
        workspace_id=workspace_id,
        execution_id=execution_id,
        start_operation_id=uuid4(),
        profile_ref=profile_ref,
        run_id=uuid4(),
        event_cursor=0,
        status="running",
        authority_id=authority.id,
        binding_version=1,
    )
    state = SimpleNamespace(
        lease=lease,
        execution=execution,
        authority=authority,
        link=link,
        clock=MutableClock(),
        owner=True,
        events=[],
        commands=[],
        command_records=[],
        saved=[],
        reads=[],
        context_loads=[],
        load_error=None,
        after_context_load=None,
        lease_lock_error=None,
    )
    state.gateway = WorkerGateway(
        lambda: MemoryUow(state),
        adapter_factories={},
        workspace_factories={},
        assignment_lease_ttl_seconds=ttl,
        assignment_lease_renew_interval_seconds=interval,
        owner_check=lambda: state.owner,
        clock=state.clock,
    )

    def load_context(**kwargs):
        assert kwargs == {
            "workspace_id": workspace_id,
            "execution_id": execution_id,
            "lease_id": lease_id,
            "fencing_token": 1,
            "operation": RuntimeModuleOperation.OBSERVE,
        }
        state.context_loads.append(kwargs)
        if state.load_error is not None:
            raise state.load_error
        context = SimpleNamespace(
            lease=replace(state.lease),
            execution=state.execution,
            worker=SimpleNamespace(id=worker_id),
            binding=SimpleNamespace(fencing_token=1),
        )
        if state.after_context_load is not None:
            state.after_context_load()
        return context

    state.gateway._load_context_by_ids = load_context
    return state


def assert_renewals(state, offsets):
    assert state.lease.version == len(offsets) + 1
    assert state.lease.fencing_token == state.authority.fencing_token == 1
    assert state.execution.active_lease_id == state.lease.id
    assert state.execution.last_fencing_token == 1
    assert state.saved == [
        (version, version + 1) for version in range(1, len(offsets) + 1)
    ]
    assert (
        len(state.commands)
        == len(state.command_records)
        == len(state.events)
        == len(offsets)
    )
    assert len({command.id for command in state.commands}) == len(offsets)
    assert state.lease.renewed_at == (
        NOW + timedelta(seconds=offsets[-1]) if offsets else None
    )
    assert state.lease.expires_at == NOW + timedelta(
        seconds=(offsets[-1] if offsets else 0) + 30
    )
    old_expires_at = NOW + timedelta(seconds=30)
    for index, offset in enumerate(offsets):
        version = index + 2
        event = state.events[index]
        command, request_hash, result = state.command_records[index]
        expires_at = NOW + timedelta(seconds=offset + 30)
        assert state.commands[index] == command
        assert event.event_type == "LeaseRenewed"
        assert event.workspace_id == state.link.workspace_id
        assert event.aggregate_type == "lease"
        assert event.aggregate_id == state.lease.id
        assert event.aggregate_version == version
        assert event.payload == {
            "status": "active",
            "execution_id": str(state.execution.id),
            "worker_id": str(state.lease.worker_id),
            "old_expires_at": old_expires_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "fencing_token": 1,
        }
        assert command.command_type == "RenewLease"
        assert command.workspace_id == state.link.workspace_id
        assert command.target_type == "lease"
        assert command.target_id == state.lease.id
        assert command.expected_version == version - 1
        assert command.payload == {
            "execution_id": str(state.execution.id),
            "worker_id": str(state.lease.worker_id),
            "lease_id": str(state.lease.id),
            "fencing_token": 1,
        }
        assert (
            command.idempotency_key
            == f"gateway-lease-renew:{state.lease.id}:v{version}"
        )
        assert command.correlation_id == event.correlation_id == event.causation_id
        assert event.causation_id == result.command_id == command.id
        assert request_hash == hash_request(command)
        assert result.aggregate_id == state.lease.id
        assert result.aggregate_version == version
        assert result.event_ids == (event.id,)
        old_expires_at = expires_at


def assert_no_writes(state, lease_before, execution_before, authority_before):
    assert state.lease == lease_before
    assert state.execution == execution_before
    assert state.authority == authority_before
    assert state.saved == state.commands == state.command_records == state.events == []


def test_fast_original_ticks_renew_at_due_time_without_restarting_native():
    state = original_fixture(ttl=30, interval=10)
    execution_before, link_before = replace(state.execution), state.link
    for offset in range(41):
        state.clock.now = NOW + timedelta(seconds=offset)
        with state.gateway.original_observation_keeper(state.link):
            assert state.lease.expires_at > state.clock.now
    assert state.lease.renewed_at == NOW + timedelta(seconds=40)
    assert state.lease.expires_at == NOW + timedelta(seconds=70)
    assert state.lease.version == 5
    assert len(state.events) == len(state.commands) == 4
    assert all(event.event_type == "LeaseRenewed" for event in state.events)
    assert all(command.command_type == "RenewLease" for command in state.commands)
    assert_renewals(state, (10, 20, 30, 40))
    assert state.execution == execution_before
    assert state.link == link_before
    assert len(state.context_loads) == 41
    assert state.reads.count(("lease", True)) == 4


def test_original_renewals_are_throttled_by_the_last_successful_renewal():
    state = original_fixture()
    execution_before, link_before = replace(state.execution), state.link
    for offset, renewals in (
        (0, ()),
        (9, ()),
        (10, (10,)),
        (10, (10,)),
        (19, (10,)),
        (20, (10, 20)),
    ):
        state.clock.now = NOW + timedelta(seconds=offset)
        with state.gateway.original_observation_keeper(state.link):
            assert_renewals(state, renewals)
    assert state.execution == execution_before
    assert state.link == link_before
    assert state.reads.count(("lease", True)) == 2


def test_interval_zero_preserves_disabled_renewal_before_expiry():
    state = original_fixture(interval=0)
    execution_before, link_before = replace(state.execution), state.link
    for offset in (0, 9, 10, 19, 20, 29):
        state.clock.now = NOW + timedelta(seconds=offset)
        with state.gateway.original_observation_keeper(state.link):
            assert state.lease.expires_at > state.clock.now
            assert_renewals(state, ())
    assert state.execution == execution_before
    assert state.link == link_before
    assert len(state.context_loads) == 6
    assert ("lease", True) not in state.reads


@pytest.mark.parametrize(
    "non_live",
    (
        "elapsed",
        "expired",
        "released",
        "lease_revoked",
        "authority_revoked",
        "succeeded",
        "failed",
        "canceled",
        "lost",
    ),
)
def test_non_live_original_observation_is_read_only(non_live):
    state = original_fixture()
    state.clock.now = NOW + timedelta(seconds=10)
    fence = {"lease_id": state.lease.id, "fencing_token": 1, "now": state.clock.now}
    if non_live in {"elapsed", "expired"}:
        state.clock.now = state.lease.expires_at
        if non_live == "expired":
            state.lease.expire(now=state.clock.now)
    elif non_live == "released":
        state.lease.release(worker_id=state.lease.worker_id, fencing_token=1, now=NOW)
    elif non_live == "lease_revoked":
        state.lease.revoke(now=NOW)
    elif non_live == "authority_revoked":
        state.authority = replace(state.authority, revoked=True)
    elif non_live == "succeeded":
        state.execution.succeed(**fence)
    elif non_live == "failed":
        state.execution.fail(**fence, failure_code="original_failed")
    elif non_live == "canceled":
        state.execution.cancel(now=state.clock.now)
    elif non_live == "lost":
        state.execution.mark_lost(**fence, failure_code="original_lost")
    lease_before, execution_before = replace(state.lease), replace(state.execution)
    authority_before, link_before = state.authority, state.link
    observed = False
    with state.gateway.original_observation_keeper(state.link):
        observed = True
    assert observed
    assert_no_writes(state, lease_before, execution_before, authority_before)
    assert state.link == link_before
    assert state.context_loads == []
    assert ("lease", True) not in state.reads


@pytest.mark.parametrize(
    "error",
    (
        GatewayMessageError("OBSERVE binding rejected", code="runtime_binding_missing"),
        StaleFencingToken("OBSERVE fence rejected"),
    ),
    ids=("gateway", "domain"),
)
def test_rejected_observe_context_cannot_renew(error):
    state = original_fixture()
    state.clock.now = NOW + timedelta(seconds=10)
    state.load_error = error
    lease_before, execution_before = replace(state.lease), replace(state.execution)
    observed = False
    with state.gateway.original_observation_keeper(state.link):
        observed = True
    assert observed
    assert_no_writes(state, lease_before, execution_before, state.authority)
    assert len(state.context_loads) == 1
    assert ("lease", True) not in state.reads


def test_owner_lost_before_entry_prevents_observation_and_renewal():
    state = original_fixture()
    state.clock.now = NOW + timedelta(seconds=10)
    state.owner = False
    lease_before, execution_before = replace(state.lease), replace(state.execution)
    observed = False
    with (
        pytest.raises(AgentError) as lost,
        state.gateway.original_observation_keeper(state.link),
    ):
        observed = True
    assert lost.value.code == "gateway_owner_lost"
    assert not observed
    assert_no_writes(state, lease_before, execution_before, state.authority)
    assert state.reads == state.context_loads == []


def test_owner_lost_after_context_load_prevents_observation_and_renewal():
    state = original_fixture()
    state.clock.now = NOW + timedelta(seconds=10)
    lease_before, execution_before = replace(state.lease), replace(state.execution)

    def lose_owner():
        state.owner = False

    state.after_context_load = lose_owner
    observed = False
    with (
        pytest.raises(AgentError) as lost,
        state.gateway.original_observation_keeper(state.link),
    ):
        observed = True
    assert lost.value.code == "gateway_owner_lost"
    assert not observed
    assert_no_writes(state, lease_before, execution_before, state.authority)
    assert len(state.context_loads) == 1
    assert ("lease", True) not in state.reads


def test_owner_lost_after_yield_retains_fail_closed_behavior():
    state = original_fixture()
    state.clock.now = NOW + timedelta(seconds=10)
    execution_before = replace(state.execution)
    observed = False
    with (
        pytest.raises(AgentError) as lost,
        state.gateway.original_observation_keeper(state.link),
    ):
        observed = True
        assert_renewals(state, (10,))
        state.owner = False
    assert lost.value.code == "gateway_owner_lost"
    assert observed
    assert_renewals(state, (10,))
    assert state.execution == execution_before
    assert state.reads.count(("lease", True)) == 1


@pytest.mark.parametrize("race", ("expires", "fence_changes"))
def test_renewal_revalidates_the_lease_after_context_load(race):
    state = original_fixture()
    state.clock.now = NOW + timedelta(seconds=10)
    execution_before = replace(state.execution)

    def change_lease():
        if race == "expires":
            state.clock.now = state.lease.expires_at
        else:
            state.lease = replace(state.lease, fencing_token=2)
        state.lease_after_race = replace(state.lease)

    state.after_context_load = change_lease
    observed = False
    with (
        pytest.raises(GatewayRecoveryRequired) as unavailable,
        state.gateway.original_observation_keeper(state.link),
    ):
        observed = True
    assert unavailable.value.code == "native_recovery_required"
    assert not observed
    assert_no_writes(state, state.lease_after_race, execution_before, state.authority)
    assert state.lease.expires_at == NOW + timedelta(seconds=30)
    assert state.lease.renewed_at is None
    assert len(state.context_loads) == 1
    assert state.reads.count(("lease", True)) == 1


def test_renewal_exception_propagates_before_observation():
    state = original_fixture()
    state.clock.now = NOW + timedelta(seconds=10)
    failure = RuntimeError("lease lock unavailable")
    state.lease_lock_error = failure
    lease_before, execution_before = replace(state.lease), replace(state.execution)
    observed = False
    with (
        pytest.raises(RuntimeError) as unavailable,
        state.gateway.original_observation_keeper(state.link),
    ):
        observed = True
    assert unavailable.value is failure
    assert not observed
    assert_no_writes(state, lease_before, execution_before, state.authority)
    assert state.reads.count(("lease", True)) == 1
