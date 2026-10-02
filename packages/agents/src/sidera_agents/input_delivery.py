from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .admission import canonical_fingerprint
from .contracts import InputReceipt, InputStatus, RunInput

MAX_PENDING_INPUTS = 16
MAX_INPUT_BYTES = 64 * 1024
INPUT_DRAIN_DEADLINE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class InputDeliverySettings:
    max_pending: int = MAX_PENDING_INPUTS
    max_input_bytes: int = MAX_INPUT_BYTES
    drain_deadline_seconds: float = INPUT_DRAIN_DEADLINE_SECONDS

    def __post_init__(self) -> None:
        if self.max_pending < 1 or self.max_input_bytes < 1:
            raise ValueError("input limits must be positive")
        if self.drain_deadline_seconds <= 0:
            raise ValueError("input drain deadline must be positive")


@dataclass(frozen=True, slots=True)
class InputOperation:
    workspace_id: UUID
    run_id: UUID
    operation_id: UUID
    fingerprint: str
    response_id: UUID
    queue_sequence: int
    status: str
    reason_code: str | None
    evidence_sequence: int | None
    payload: RunInput | None

    def receipt(self) -> InputReceipt:
        public_status = (
            InputStatus.ACCEPTED
            if self.status == "delivering"
            else InputStatus(self.status)
        )
        return InputReceipt(
            operation_id=self.operation_id,
            run_id=self.run_id,
            status=public_status,
            reason_code=self.reason_code,
            evidence_sequence=self.evidence_sequence,
        )


def input_fingerprint(request: RunInput) -> str:
    payload = request.model_dump(mode="json", warnings=False)
    return canonical_fingerprint(payload)


def input_size(request: RunInput) -> int:
    context_bytes = sum(
        len(item.text.encode("utf-8")) if item.text is not None else 0
        for item in request.context
    )
    return len(request.text.encode("utf-8")) + context_bytes


def input_size_for_record(record: InputOperation) -> int:
    return 0 if record.payload is None else input_size(record.payload)
