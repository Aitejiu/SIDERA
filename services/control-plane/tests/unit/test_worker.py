from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import (
    CapacityExceeded,
    InvalidTransition,
    ValidationError,
)
from sidera_control_plane.domain.worker import (
    Worker,
    WorkerDesiredState,
    WorkerStatus,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def make_worker(*, capacity: int = 2) -> Worker:
    return Worker.register(
        worker_id=uuid4(),
        workspace_id=uuid4(),
        adapter_type="opencode",
        display_name="Worker A",
        capabilities=frozenset({"python", "postgres"}),
        capacity=capacity,
        cost_per_hour_microunits=100,
        startup_latency_ms=250,
        now=NOW,
    )


def test_worker_capacity_and_draining_are_enforced() -> None:
    worker = make_worker(capacity=1)

    worker.reserve()
    with pytest.raises(CapacityExceeded):
        worker.reserve()

    worker.mark_running()
    worker.drain()
    assert worker.status is WorkerStatus.DRAINING
    with pytest.raises(InvalidTransition):
        worker.reserve()


def test_worker_release_keeps_draining_status_until_reregistration() -> None:
    worker = make_worker()
    worker.reserve()
    worker.mark_running()
    worker.drain()

    worker.release()

    assert worker.current_load == 0
    assert worker.status is WorkerStatus.DRAINING
    assert worker.desired_state is WorkerDesiredState.DRAINING


def test_draining_intent_survives_offline_and_healthy_heartbeat() -> None:
    worker = make_worker()
    worker.drain()
    worker.mark_offline()

    worker.heartbeat(healthy=True, now=NOW + timedelta(seconds=1))

    assert worker.status is WorkerStatus.DRAINING
    assert worker.desired_state is WorkerDesiredState.DRAINING


@pytest.mark.parametrize(
    ("capabilities", "capacity", "cost", "latency"),
    [
        (frozenset(), 1, 0, 0),
        (frozenset({"python"}), 0, 0, 0),
        (frozenset({"python"}), 1, -1, 0),
        (frozenset({"python"}), 1, 0, -1),
    ],
)
def test_worker_registration_validates_capacity_cost_and_capabilities(
    capabilities: frozenset[str],
    capacity: int,
    cost: int,
    latency: int,
) -> None:
    with pytest.raises(ValidationError):
        Worker.register(
            worker_id=uuid4(),
            workspace_id=uuid4(),
            adapter_type="opencode",
            display_name="Worker A",
            capabilities=capabilities,
            capacity=capacity,
            cost_per_hour_microunits=cost,
            startup_latency_ms=latency,
            now=NOW,
        )


def test_worker_heartbeat_rejects_time_regression_and_unhealthy_workers() -> None:
    worker = make_worker()

    with pytest.raises(ValidationError):
        worker.heartbeat(healthy=True, now=NOW - timedelta(seconds=1))

    worker.heartbeat(healthy=False, now=NOW + timedelta(seconds=1))
    assert worker.status is WorkerStatus.UNHEALTHY
    assert not worker.is_fresh(
        now=NOW + timedelta(seconds=31), timeout=timedelta(seconds=30)
    )
