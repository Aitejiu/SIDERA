from __future__ import annotations

import json
import math
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

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
        if (
            not math.isfinite(self.drain_deadline_seconds)
            or self.drain_deadline_seconds <= 0
        ):
            raise ValueError("input drain deadline must be finite and positive")


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
    return "sha256:" + sha256(_canonical_input_bytes(request)).hexdigest()


def input_size(request: RunInput) -> int:
    return len(_canonical_input_bytes(request))


def _canonical_input_bytes(request: RunInput) -> bytes:
    payload = request.model_dump(mode="json", warnings=False)
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def input_size_for_record(record: InputOperation) -> int:
    return 0 if record.payload is None else input_size(record.payload)
