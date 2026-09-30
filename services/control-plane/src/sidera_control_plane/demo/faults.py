from __future__ import annotations

from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.environment import runtime_root_for
from sidera_control_plane.demo.state import DemoStateStore, StateIdentityConflict

FaultPoint = Literal[
    "verification-fails-once",
    "after-input-persist",
    "after-candidate-publish",
    "target-start-unknown",
    "notification-ack-lost",
]


class DemoProcessCrash(BaseException):
    """A deterministic demo crash that must bypass normal error handling."""

    def __init__(self, point: FaultPoint) -> None:
        self.point = point
        super().__init__(f"demo_fault:{point}")

_FAULT_POINTS = frozenset(
    {
        "verification-fails-once",
        "after-input-persist",
        "after-candidate-publish",
        "target-start-unknown",
        "notification-ack-lost",
    }
)


def consume_fault(
    manifest: DemoManifest,
    point: FaultPoint,
    operation_id: UUID,
) -> bool:
    """Consume one deterministic fault without changing normal demo runs.

    The durable record is keyed by the fault point, so a replay of the same
    operation sees the original ``True`` result while a different operation
    cannot consume the point a second time.
    """

    if point not in _FAULT_POINTS:
        raise ValueError("unknown_demo_fault_point")
    if not isinstance(operation_id, UUID):
        raise TypeError("fault operation_id must be a UUID")
    if manifest.scenario not in {"rework", "recovery", "controls"}:
        return False

    state = DemoStateStore(runtime_root_for(manifest) / "state", manifest.run_id)
    key = f"fault/{point}"
    request: dict[str, object] = {
        "point": point,
        "operation_id": str(operation_id),
    }
    try:
        record = state.reserve(key, operation_id, request)
    except StateIdentityConflict:
        return False
    if record.status == "applied":
        return record.result.get("consumed") is True
    state.settle(
        key,
        operation_id,
        "applied",
        {"point": point, "operation_id": str(operation_id), "consumed": True},
    )
    return True


def consume_fault_once(
    manifest: DemoManifest,
    point: FaultPoint,
    operation_id: UUID,
) -> bool:
    """Consume a fault at most once for the whole demo run."""

    if point not in _FAULT_POINTS:
        raise ValueError("unknown_demo_fault_point")
    if not isinstance(operation_id, UUID):
        raise TypeError("fault operation_id must be a UUID")
    if manifest.scenario not in {"rework", "recovery", "controls"}:
        return False

    state = DemoStateStore(runtime_root_for(manifest) / "state", manifest.run_id)
    key = f"fault-fired/{point}"
    marker_operation_id = uuid5(
        NAMESPACE_URL,
        f"sidera.demo.fault-fired:{manifest.run_id}:{point}",
    )
    marker = state.reserve(
        key,
        marker_operation_id,
        {"point": point},
    )
    if marker.status == "applied":
        return False
    if not consume_fault(manifest, point, operation_id):
        state.settle(
            key,
            marker_operation_id,
            "rejected",
            {"point": point, "fired": False},
        )
        return False
    state.settle(
        key,
        marker_operation_id,
        "applied",
        {"point": point, "operation_id": str(operation_id), "fired": True},
    )
    return True


__all__ = [
    "DemoProcessCrash",
    "FaultPoint",
    "consume_fault",
    "consume_fault_once",
]
