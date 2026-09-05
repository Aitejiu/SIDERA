from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError

_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_REASON = 2000
_MAX_SUMMARY = 16384


class GovernanceHoldStatus(StrEnum):
    ACTIVE = "active"
    RESUMED = "resumed"
    SUPERSEDED = "superseded"


class ReleaseProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"


class GovernanceResolutionAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class GovernanceResolutionResult(StrEnum):
    APPLIED = "applied"
    STALE = "stale"


class GovernanceTransport(StrEnum):
    WEB = "web"
    MAIN_AGENT = "main_agent"


class GovernanceAction(StrEnum):
    PAUSE = "pause"
    APPROVE = "approve"
    REJECT = "reject"
    RESUME_AUTOMATIC = "resume_automatic"


@dataclass(frozen=True, slots=True)
class CollaborationGovernanceHold:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    run_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    worker_item_id: UUID
    status: GovernanceHoldStatus
    created_by_actor_id: str
    created_via: GovernanceTransport
    reason: str | None
    resource_version: int
    created_at: datetime
    resolved_at: datetime | None

    @classmethod
    def create(cls, *, now: datetime, **values: object) -> CollaborationGovernanceHold:
        values.setdefault("resource_version", 1)
        values.setdefault("status", GovernanceHoldStatus.ACTIVE)
        values.setdefault("resolved_at", None)
        values.setdefault("created_at", now)
        return cls(**cast(dict[str, Any], values))

    def __post_init__(self) -> None:
        _uuids(self, ("id", "workspace_id", "root_task_id", "run_id", "plan_snapshot_id", "worker_item_id"))
        _positive(self.plan_generation, "plan_generation")
        _hash_value(self.plan_content_hash, "plan_content_hash")
        _positive(self.resource_version, "resource_version")
        self._common()
        object.__setattr__(self, "status", _status(self.status, GovernanceHoldStatus, "hold status"))
        if self.status is GovernanceHoldStatus.ACTIVE and self.resolved_at is not None:
            raise ValidationError("active hold must not be resolved")
        if self.status is not GovernanceHoldStatus.ACTIVE and self.resolved_at is None:
            raise ValidationError("terminal hold requires resolved_at")

    def _common(self) -> None:
        _text(self.created_by_actor_id, "created_by_actor_id", 255, required=True)
        object.__setattr__(self, "created_via", _status(self.created_via, GovernanceTransport, "created_via"))
        object.__setattr__(self, "reason", _reason(self.reason))
        _aware(self.created_at, "created_at")
        _optional_aware(self.resolved_at, "resolved_at")

    def resume(self, *, now: datetime) -> CollaborationGovernanceHold:
        return self._transition(GovernanceHoldStatus.RESUMED, now)

    def supersede(self, *, now: datetime) -> CollaborationGovernanceHold:
        return self._transition(GovernanceHoldStatus.SUPERSEDED, now)

    def _transition(self, status: GovernanceHoldStatus, now: datetime) -> CollaborationGovernanceHold:
        if self.status is not GovernanceHoldStatus.ACTIVE:
            raise InvalidTransition("only an active hold can transition")
        _aware(now, "resolved_at")
        return replace(self, status=status, resource_version=self.resource_version + 1, resolved_at=now)


@dataclass(frozen=True, slots=True)
class CollaborationReleaseProposal:
    id: UUID
    workspace_id: UUID
    hold_id: UUID
    run_id: UUID
    projection_snapshot_id: UUID
    projection_hash: str
    decision_id: UUID
    intent_id: UUID
    execution_request_id: UUID
    target_request_version: int
    input_fingerprint_version: int
    input_fingerprint: str
    status: ReleaseProposalStatus
    reason_code: str
    summary: str
    resource_version: int
    created_at: datetime
    resolved_at: datetime | None
    input_selection_id: UUID | None = None
    input_selection_hash: str | None = None

    @classmethod
    def create(cls, *, now: datetime, **values: object) -> CollaborationReleaseProposal:
        values.setdefault("resource_version", 1)
        values.setdefault("status", ReleaseProposalStatus.PENDING)
        values.setdefault("resolved_at", None)
        values.setdefault("created_at", now)
        return cls(**cast(dict[str, Any], values))

    def __post_init__(self) -> None:
        _uuids(self, ("id", "workspace_id", "hold_id", "run_id", "projection_snapshot_id", "decision_id", "intent_id", "execution_request_id"))
        _positive(self.target_request_version, "target_request_version")
        _positive(self.input_fingerprint_version, "input_fingerprint_version")
        _hash_value(self.projection_hash, "projection_hash")
        _hash_value(self.input_fingerprint, "input_fingerprint")
        _positive(self.resource_version, "resource_version")
        _text(self.reason_code, "reason_code", 255, required=True)
        _text(self.summary, "summary", _MAX_SUMMARY, required=True)
        _aware(self.created_at, "created_at")
        _optional_aware(self.resolved_at, "resolved_at")
        if (self.input_selection_id is None) != (self.input_selection_hash is None):
            raise ValidationError(
                "proposal input selection id and hash must be provided together"
            )
        if self.input_selection_id is not None:
            _uuids(self, ("input_selection_id",))
        if self.input_selection_hash is not None:
            _hash_value(self.input_selection_hash, "input_selection_hash")
        object.__setattr__(self, "status", _status(self.status, ReleaseProposalStatus, "proposal status"))
        if self.status is ReleaseProposalStatus.PENDING and self.resolved_at is not None:
            raise ValidationError("pending proposal must not be resolved")
        if self.status is not ReleaseProposalStatus.PENDING and self.resolved_at is None:
            raise ValidationError("terminal proposal requires resolved_at")

    def approve(self, *, now: datetime) -> CollaborationReleaseProposal:
        return self._transition(ReleaseProposalStatus.APPROVED, now)

    def reject(self, *, now: datetime) -> CollaborationReleaseProposal:
        return self._transition(ReleaseProposalStatus.REJECTED, now)

    def stale(self, *, now: datetime) -> CollaborationReleaseProposal:
        return self._transition(ReleaseProposalStatus.STALE, now)

    def _transition(self, status: ReleaseProposalStatus, now: datetime) -> CollaborationReleaseProposal:
        if self.status is not ReleaseProposalStatus.PENDING:
            raise InvalidTransition("only a pending proposal can transition")
        _aware(now, "resolved_at")
        return replace(self, status=status, resource_version=self.resource_version + 1, resolved_at=now)


@dataclass(frozen=True, slots=True)
class CollaborationGovernanceResolution:
    id: UUID
    workspace_id: UUID
    proposal_id: UUID
    action: GovernanceResolutionAction
    result: GovernanceResolutionResult
    actor_id: str
    transport: GovernanceTransport
    reason: str | None
    command_id: UUID
    created_at: datetime

    @classmethod
    def create(cls, *, now: datetime, **values: object) -> CollaborationGovernanceResolution:
        return cls(created_at=now, **cast(dict[str, Any], values))

    def __post_init__(self) -> None:
        _uuids(self, ("id", "workspace_id", "proposal_id", "command_id"))
        object.__setattr__(self, "action", _status(self.action, GovernanceResolutionAction, "resolution action"))
        object.__setattr__(self, "result", _status(self.result, GovernanceResolutionResult, "resolution result"))
        if self.action is GovernanceResolutionAction.REJECT and self.result is GovernanceResolutionResult.STALE:
            raise ValidationError("reject resolution cannot have stale result")
        _text(self.actor_id, "actor_id", 255, required=True)
        object.__setattr__(self, "transport", _status(self.transport, GovernanceTransport, "transport"))
        object.__setattr__(self, "reason", _reason(self.reason))
        _aware(self.created_at, "created_at")


def _uuids(value: object, names: tuple[str, ...]) -> None:
    for name in names:
        if not isinstance(getattr(value, name), UUID):
            raise ValidationError(f"{name} must be a UUID")


def _positive(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be a positive integer")


def _hash_value(value: object, name: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a lowercase SHA-256 hash")


def _text(value: object, name: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValidationError(f"{name} is invalid")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValidationError(f"{name} contains an unsupported control character")
    return value


def _reason(value: object) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _text(value, "reason", _MAX_REASON)


def _aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")


def _optional_aware(value: object, name: str) -> None:
    if value is not None:
        _aware(value, name)


def _status(value: object, enum_type: type[StrEnum], name: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} is invalid") from exc
