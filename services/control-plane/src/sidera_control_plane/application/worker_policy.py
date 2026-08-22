from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.worker import Worker


@dataclass(frozen=True, slots=True)
class WorkerCandidate:
    id: UUID
    capabilities: frozenset[str]
    capacity: int
    current_load: int
    cost_per_hour_microunits: int
    startup_latency_ms: int

    @classmethod
    def from_worker(cls, worker: Worker) -> "WorkerCandidate":
        return cls(
            id=worker.id,
            capabilities=worker.capabilities,
            capacity=worker.capacity,
            current_load=worker.current_load,
            cost_per_hour_microunits=worker.cost_per_hour_microunits,
            startup_latency_ms=worker.startup_latency_ms,
        )


@dataclass(frozen=True, slots=True)
class AssignmentDecision:
    worker_id: UUID
    reason: dict[str, object]


class WorkerPolicy(Protocol):
    def select(
        self, execution: Execution, candidates: tuple[WorkerCandidate, ...]
    ) -> AssignmentDecision | None:
        raise NotImplementedError


class LowestCostWorkerPolicy:
    name = "lowest-cost-v1"

    def select(
        self, execution: Execution, candidates: tuple[WorkerCandidate, ...]
    ) -> AssignmentDecision | None:
        if not candidates:
            return None
        selected = min(candidates, key=lambda candidate: _score(execution, candidate))
        score = _score(execution, selected)
        return AssignmentDecision(
            worker_id=selected.id,
            reason={
                "policy": self.name,
                "eligible_count": len(candidates),
                "required_capabilities": sorted(execution.capability_requirements),
                "selected_score": {
                    "extra_capabilities": score[0],
                    "startup_latency_ms": score[1],
                    "load_ppm": score[2],
                    "cost_per_hour_microunits": score[3],
                },
            },
        )


def _score(
    execution: Execution, candidate: WorkerCandidate
) -> tuple[int, int, int, int, int]:
    extra = len(candidate.capabilities - execution.capability_requirements)
    load_ppm = candidate.current_load * 1_000_000 // candidate.capacity
    return (
        extra,
        candidate.startup_latency_ms,
        load_ppm,
        candidate.cost_per_hour_microunits,
        candidate.id.int,
    )
