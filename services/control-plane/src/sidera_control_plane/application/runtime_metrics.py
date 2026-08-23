from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RuntimeDurationMetricsView:
    count: int
    total_seconds: float
    average_seconds: float
    max_seconds: float


@dataclass(frozen=True, slots=True)
class RuntimeMetricsView:
    workspace_id: UUID
    as_of: datetime
    scheduler_assignments_total: int
    scheduler_blocks_active: dict[str, int]
    worker_runtime_configuration_status: dict[str, int]
    runtime_binding_failures_total: dict[str, int]
    scheduler_assignment_duration_seconds: RuntimeDurationMetricsView


@dataclass(frozen=True, slots=True)
class RuntimeReadinessWorkerView:
    worker_id: UUID
    worker_status: str
    runtime_status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class RuntimeReadinessView:
    workspace_id: UUID
    as_of: datetime
    workers: tuple[RuntimeReadinessWorkerView, ...]
    missing_runtime_configuration: int
    invalid_runtime_configuration: int
    executions_without_binding: tuple[UUID, ...]
    module_mismatches: tuple[RuntimeReadinessWorkerView, ...]

    @property
    def ready(self) -> bool:
        return not (
            self.missing_runtime_configuration
            or self.invalid_runtime_configuration
            or self.executions_without_binding
            or self.module_mismatches
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "workspace_id": str(self.workspace_id),
            "as_of": self.as_of.isoformat(),
            "ready": self.ready,
            "workers": [
                {
                    "worker_id": str(worker.worker_id),
                    "worker_status": worker.worker_status,
                    "runtime_status": worker.runtime_status,
                    "reason": worker.reason,
                }
                for worker in self.workers
            ],
            "missing_runtime_configuration": self.missing_runtime_configuration,
            "invalid_runtime_configuration": self.invalid_runtime_configuration,
            "executions_without_binding": [
                str(execution_id) for execution_id in self.executions_without_binding
            ],
            "module_mismatches": [
                {
                    "worker_id": str(worker.worker_id),
                    "worker_status": worker.worker_status,
                    "runtime_status": worker.runtime_status,
                    "reason": worker.reason,
                }
                for worker in self.module_mismatches
            ],
        }


class RuntimeMetricsStore(Protocol):
    def get_metrics(self, workspace_id: UUID) -> RuntimeMetricsView: ...


class RuntimeReadinessStore(Protocol):
    def get_readiness(self, workspace_id: UUID) -> RuntimeReadinessView: ...


class RuntimeMetricsService:
    def __init__(self, store: RuntimeMetricsStore) -> None:
        self._store = store

    def get_metrics(self, workspace_id: UUID) -> RuntimeMetricsView:
        return self._store.get_metrics(workspace_id)
