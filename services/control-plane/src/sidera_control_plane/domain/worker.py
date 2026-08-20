from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.domain.errors import (
    CapacityExceeded,
    InvalidTransition,
    ValidationError,
)


class WorkerStatus(StrEnum):
    OFFLINE = "offline"
    IDLE = "idle"
    RESERVED = "reserved"
    BUSY = "busy"
    DRAINING = "draining"
    UNHEALTHY = "unhealthy"


SCHEDULABLE_WORKER_STATUSES = {
    WorkerStatus.IDLE,
    WorkerStatus.RESERVED,
    WorkerStatus.BUSY,
}


@dataclass(slots=True)
class Worker:
    id: UUID
    workspace_id: UUID
    adapter_type: str
    display_name: str
    status: WorkerStatus
    capabilities: frozenset[str]
    capacity: int
    current_load: int
    cost_per_hour_microunits: int
    startup_latency_ms: int
    last_heartbeat_at: datetime
    version: int

    @classmethod
    def register(
        cls,
        *,
        worker_id: UUID,
        workspace_id: UUID,
        adapter_type: str,
        display_name: str,
        capabilities: frozenset[str],
        capacity: int,
        cost_per_hour_microunits: int,
        startup_latency_ms: int,
        now: datetime,
    ) -> "Worker":
        clean_capabilities = frozenset(
            item.strip() for item in capabilities if item.strip()
        )
        if not adapter_type.strip() or not display_name.strip() or not clean_capabilities:
            raise ValidationError(
                "adapter_type, display_name, and capabilities are required"
            )
        if capacity < 1 or cost_per_hour_microunits < 0 or startup_latency_ms < 0:
            raise ValidationError("capacity must be positive and costs cannot be negative")
        return cls(
            id=worker_id,
            workspace_id=workspace_id,
            adapter_type=adapter_type.strip(),
            display_name=display_name.strip(),
            status=WorkerStatus.IDLE,
            capabilities=clean_capabilities,
            capacity=capacity,
            current_load=0,
            cost_per_hour_microunits=cost_per_hour_microunits,
            startup_latency_ms=startup_latency_ms,
            last_heartbeat_at=now,
            version=1,
        )

    def heartbeat(self, *, healthy: bool, now: datetime) -> None:
        if now < self.last_heartbeat_at:
            raise ValidationError("heartbeat time cannot move backwards")
        self.last_heartbeat_at = now
        if not healthy:
            self.status = WorkerStatus.UNHEALTHY
        elif self.status is not WorkerStatus.DRAINING:
            self.status = WorkerStatus.IDLE if self.current_load == 0 else WorkerStatus.BUSY
        self.version += 1

    def is_fresh(self, *, now: datetime, timeout: timedelta) -> bool:
        return self.last_heartbeat_at > now - timeout

    def reserve(self) -> None:
        if self.status not in SCHEDULABLE_WORKER_STATUSES:
            raise InvalidTransition(f"worker cannot accept work: {self.status}")
        if self.current_load >= self.capacity:
            raise CapacityExceeded("worker has no remaining capacity")
        self.current_load += 1
        self.status = WorkerStatus.RESERVED if self.current_load == 1 else WorkerStatus.BUSY
        self.version += 1

    def mark_running(self) -> None:
        if self.current_load < 1:
            raise InvalidTransition("worker has no reserved execution")
        if self.status not in {
            WorkerStatus.RESERVED,
            WorkerStatus.BUSY,
            WorkerStatus.DRAINING,
        }:
            raise InvalidTransition(f"worker cannot start work: {self.status}")
        if self.status is not WorkerStatus.DRAINING:
            self.status = WorkerStatus.BUSY
        self.version += 1

    def release(self) -> None:
        if self.current_load < 1:
            raise InvalidTransition("worker load is already zero")
        self.current_load -= 1
        if self.status in SCHEDULABLE_WORKER_STATUSES:
            self.status = WorkerStatus.IDLE if self.current_load == 0 else WorkerStatus.BUSY
        self.version += 1

    def drain(self) -> None:
        if self.status is WorkerStatus.OFFLINE:
            raise InvalidTransition("offline worker cannot enter draining")
        self.status = WorkerStatus.DRAINING
        self.version += 1

    def mark_offline(self) -> None:
        if self.status is not WorkerStatus.OFFLINE:
            self.status = WorkerStatus.OFFLINE
            self.version += 1
