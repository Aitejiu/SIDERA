"""Small, secret-free contracts used by the staged R6 acceptance runner."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

EvidenceStatus = Literal["NOT_RUN", "PASS", "FAIL", "INCOMPLETE"]
EvidenceKind = Literal["contract", "real_worker", "real_user_flow"]


class ScenarioEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    scenario: str = Field(min_length=1, max_length=64)
    status: EvidenceStatus
    evidence_kind: EvidenceKind
    code_commit: str = Field(min_length=40, max_length=40)
    runtime_fingerprint: str = Field(min_length=64, max_length=64)
    root_task_id: UUID | None = None
    command_ids: tuple[UUID, ...] = ()
    execution_ids: tuple[UUID, ...] = ()
    session_ids: tuple[UUID, ...] = ()
    check_codes: tuple[str, ...] = ()
    failure_code: str | None = None


class ClientOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "accepted", "observed", "conflict", "rejected", "incomplete"
    ]
    command_id: UUID | None = None
    body: dict[str, object] | None = None
    current_version: int | None = None
    diagnostic_code: str = Field(min_length=1, max_length=96)


class ReservationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["reserved", "replay", "blocked"]
    operation_id: str
    scenario: str
    role: str
    started_at: str
    deadline: str
    diagnostic_code: str
    resource_ids: tuple[str, ...] = ()
