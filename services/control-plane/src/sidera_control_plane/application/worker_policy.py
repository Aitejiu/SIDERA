from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.worker import Worker


@dataclass(frozen=True, slots=True)
class WorkerCandidate:
    id: UUID
    adapter_type: str
    capabilities: frozenset[str]
    capacity: int
    current_load: int
    cost_per_hour_microunits: int
    startup_latency_ms: int

    @classmethod
    def from_worker(cls, worker: Worker) -> "WorkerCandidate":
        return cls(
            id=worker.id,
            adapter_type=worker.adapter_type,
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
        preference_rank = score[0]
        preference_fallback = bool(execution.adapter_preferences) and (
            preference_rank == len(execution.adapter_preferences)
        )
        return AssignmentDecision(
            worker_id=selected.id,
            reason={
                "policy": self.name,
                "eligible_count": len(candidates),
                "required_capabilities": sorted(execution.capability_requirements),
                "selected_adapter_type": selected.adapter_type,
                "adapter_preference_rank": preference_rank,
                "adapter_preference_fallback": preference_fallback,
                "pin_applied": execution.pinned_worker_id == selected.id,
                "selected_score": {
                    "extra_capabilities": score[1],
                    "startup_latency_ms": score[2],
                    "load_ppm": score[3],
                    "cost_per_hour_microunits": score[4],
                },
            },
        )


def _score(
    execution: Execution, candidate: WorkerCandidate
) -> tuple[int, int, int, int, int, int]:
    if execution.adapter_preferences:
        try:
            adapter_preference_rank = execution.adapter_preferences.index(
                candidate.adapter_type
            )
        except ValueError:
            adapter_preference_rank = len(execution.adapter_preferences)
    else:
        adapter_preference_rank = 0
    extra = len(candidate.capabilities - execution.capability_requirements)
    load_ppm = candidate.current_load * 1_000_000 // candidate.capacity
    return (
        adapter_preference_rank,
        extra,
        candidate.startup_latency_ms,
        load_ppm,
        candidate.cost_per_hour_microunits,
        candidate.id.int,
    )
