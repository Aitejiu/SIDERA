from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sidera_agents.contracts import ProfileRef

from sidera_control_plane.domain.agent_bindings import AssigneeRef
from sidera_control_plane.domain.errors import InvalidTransition, ValidationError

_MAX_TEXT_LENGTH = 16_384
_MAX_MAP_ENTRIES = 128
_MAX_MAP_DEPTH = 8
_MAX_MAP_BYTES = 128 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class HandoffProposalStatus(StrEnum):
    PREPARED = "prepared"
    CONFIRMED = "confirmed"
    STALE = "stale"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class HandoffTransitionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    BLOCKED_UNKNOWN = "blocked_unknown"
    FAILED = "failed"
    CONVERGED = "converged"


class HandoffPhase(StrEnum):
    CHECKPOINT_SOURCE = "checkpoint_source"
    STOP_SOURCE = "stop_source"
    FREEZE_SOURCE_WORKSPACE = "freeze_source_workspace"
    CREATE_TARGET_EXECUTION = "create_target_execution"
    ASSEMBLE_BUNDLE = "assemble_bundle"
    TRANSFER_WORKSPACE = "transfer_workspace"
    START_TARGET_SESSION = "start_target_session"
    ACKNOWLEDGE_CONTEXT = "acknowledge_context"
    TRANSFER_SAFE_INPUTS = "transfer_safe_inputs"
    ACTIVATE_TARGET = "activate_target"
    COMPLETE = "complete"


class HandoffWriterState(StrEnum):
    SOURCE_ACTIVE = "source_active"
    SOURCE_STOP_REQUESTED = "source_stop_requested"
    WRITER_NONE = "writer_none"
    TARGET_STARTING = "target_starting"
    TARGET_ACTIVE = "target_active"
    UNKNOWN = "unknown"


class HandoffTransferMode(StrEnum):
    REUSE_EXACT = "reuse_exact"


class HandoffSourceChannel(StrEnum):
    WEB = "web"


ACTIVE_HANDOFF_TRANSITION_STATUSES = frozenset(
    {
        HandoffTransitionStatus.PENDING,
        HandoffTransitionStatus.RUNNING,
        HandoffTransitionStatus.BLOCKED,
        HandoffTransitionStatus.BLOCKED_UNKNOWN,
        HandoffTransitionStatus.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class HandoffCandidateInspection:
    worker_id: UUID
    display_name: str
    adapter_type: str
    eligible: bool
    recommended: bool
    ineligibility_code: str | None
    runtime_snapshot: Mapping[str, object] | None
    capability_snapshot_hash: str | None
    policy_reason: Mapping[str, object]
    workspace_compatibility: Mapping[str, object]

    def __post_init__(self) -> None:
        _uuid(self.worker_id, "candidate worker_id")
        object.__setattr__(
            self,
            "display_name",
            _text(self.display_name, "candidate display_name"),
        )
        object.__setattr__(
            self,
            "adapter_type",
            _text(self.adapter_type, "candidate adapter_type"),
        )
        _boolean(self.eligible, "candidate eligible")
        _boolean(self.recommended, "candidate recommended")
        object.__setattr__(
            self,
            "ineligibility_code",
            _optional_text(self.ineligibility_code, "candidate ineligibility_code"),
        )
        if self.eligible and self.ineligibility_code is not None:
            raise ValidationError(
                "eligible candidate cannot have an ineligibility_code"
            )
        if not self.eligible and self.ineligibility_code is None:
            raise ValidationError("ineligible candidate requires an ineligibility_code")
        if self.recommended and not self.eligible:
            raise ValidationError("ineligible candidate cannot be recommended")
        if self.runtime_snapshot is not None:
            object.__setattr__(
                self,
                "runtime_snapshot",
                _mapping(self.runtime_snapshot, "candidate runtime_snapshot"),
            )
        object.__setattr__(
            self,
            "capability_snapshot_hash",
            _optional_hash(
                self.capability_snapshot_hash,
                "candidate capability_snapshot_hash",
            ),
        )
        object.__setattr__(
            self,
            "policy_reason",
            _mapping(self.policy_reason, "candidate policy_reason"),
        )
        object.__setattr__(
            self,
            "workspace_compatibility",
            _mapping(
                self.workspace_compatibility,
                "candidate workspace_compatibility",
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "worker_id": str(self.worker_id),
            "display_name": self.display_name,
            "adapter_type": self.adapter_type,
            "eligible": self.eligible,
            "recommended": self.recommended,
            "ineligibility_code": self.ineligibility_code,
            "runtime_snapshot": _json_value(self.runtime_snapshot),
            "capability_snapshot_hash": self.capability_snapshot_hash,
            "policy_reason": _json_value(self.policy_reason),
            "workspace_compatibility": _json_value(self.workspace_compatibility),
        }


@dataclass(frozen=True, slots=True)
class HandoffCandidateList:
    workspace_id: UUID
    worker_item_id: UUID
    source_execution_id: UUID
    source_session_id: UUID
    source_worker_id: UUID
    source_worker_item_version: int
    candidates: tuple[HandoffCandidateInspection, ...]
    recommended_worker_id: UUID | None
    blocking_codes: tuple[str, ...]
    inspected_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "worker_item_id",
            "source_execution_id",
            "source_session_id",
            "source_worker_id",
        ):
            _uuid(getattr(self, name), f"candidate list {name}")
        _positive(self.source_worker_item_version, "source_worker_item_version")
        candidates = tuple(self.candidates)
        if any(not isinstance(item, HandoffCandidateInspection) for item in candidates):
            raise ValidationError("candidate list contains an invalid candidate")
        worker_ids = tuple(item.worker_id for item in candidates)
        if len(set(worker_ids)) != len(worker_ids):
            raise ValidationError("candidate worker ids must be unique")
        if self.source_worker_id in worker_ids:
            raise ValidationError("candidate list cannot include the source worker")
        recommended = tuple(item for item in candidates if item.recommended)
        if len(recommended) > 1:
            raise ValidationError("candidate list can have at most one recommendation")
        if self.recommended_worker_id is not None:
            _uuid(self.recommended_worker_id, "recommended_worker_id")
            selected = next(
                (
                    item
                    for item in candidates
                    if item.worker_id == self.recommended_worker_id
                ),
                None,
            )
            if selected is None or not selected.eligible or not selected.recommended:
                raise ValidationError(
                    "recommended_worker_id must reference the eligible recommendation"
                )
        elif recommended:
            raise ValidationError(
                "candidate recommendation must match recommended_worker_id"
            )
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(
            self,
            "blocking_codes",
            _text_tuple(self.blocking_codes, "candidate blocking_codes"),
        )
        _aware(self.inspected_at, "candidate inspected_at")

    def to_mapping(self) -> dict[str, object]:
        return {
            "workspace_id": str(self.workspace_id),
            "worker_item_id": str(self.worker_item_id),
            "source_execution_id": str(self.source_execution_id),
            "source_session_id": str(self.source_session_id),
            "source_worker_id": str(self.source_worker_id),
            "source_worker_item_version": self.source_worker_item_version,
            "candidates": [item.to_mapping() for item in self.candidates],
            "recommended_worker_id": (
                str(self.recommended_worker_id)
                if self.recommended_worker_id is not None
                else None
            ),
            "blocking_codes": list(self.blocking_codes),
            "inspected_at": self.inspected_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class HandoffProposalPayload:
    proposal_schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID
    worker_item_id: UUID
    source_execution_id: UUID
    source_execution_version: int
    source_session_id: UUID
    source_session_version: int
    source_session_last_sequence: int
    source_worker_id: UUID
    source_worker_version: int
    source_runtime_snapshot: Mapping[str, object]
    source_workspace_manifest_id: UUID
    source_workspace_manifest_version: int
    source_context_package_id: UUID
    source_context_package_hash: str
    source_worker_context_version: int
    observed_plan_snapshot_id: UUID
    observed_plan_generation: int
    observed_plan_head_version: int
    target_worker_id: UUID
    target_worker_version: int
    target_runtime_snapshot: Mapping[str, object]
    target_capability_snapshot_hash: str
    target_policy_reason: Mapping[str, object]
    target_policy_facts: Mapping[str, object]
    transfer_mode: HandoffTransferMode
    handoff_reason: str
    handoff_instruction: str
    input_plan: Mapping[str, object]
    impact_summary: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.proposal_schema_version != 1:
            raise ValidationError("handoff proposal schema version must be 1")
        for name in (
            "workspace_id",
            "root_task_id",
            "subtask_id",
            "worker_item_id",
            "source_execution_id",
            "source_session_id",
            "source_worker_id",
            "source_workspace_manifest_id",
            "source_context_package_id",
            "observed_plan_snapshot_id",
            "target_worker_id",
        ):
            _uuid(getattr(self, name), f"handoff proposal {name}")
        if self.source_worker_id == self.target_worker_id:
            raise ValidationError(
                "handoff target worker must differ from source worker"
            )
        for name in (
            "source_execution_version",
            "source_session_version",
            "source_worker_version",
            "source_workspace_manifest_version",
            "source_worker_context_version",
            "observed_plan_generation",
            "observed_plan_head_version",
            "target_worker_version",
        ):
            _positive(getattr(self, name), f"handoff proposal {name}")
        _non_negative(
            self.source_session_last_sequence,
            "source_session_last_sequence",
        )
        object.__setattr__(
            self,
            "source_runtime_snapshot",
            _mapping(self.source_runtime_snapshot, "source_runtime_snapshot"),
        )
        object.__setattr__(
            self,
            "target_runtime_snapshot",
            _mapping(self.target_runtime_snapshot, "target_runtime_snapshot"),
        )
        object.__setattr__(
            self,
            "target_policy_reason",
            _mapping(self.target_policy_reason, "target_policy_reason"),
        )
        object.__setattr__(
            self,
            "target_policy_facts",
            _mapping(self.target_policy_facts, "target_policy_facts"),
        )
        object.__setattr__(
            self,
            "input_plan",
            _mapping(self.input_plan, "input_plan"),
        )
        object.__setattr__(
            self,
            "impact_summary",
            _mapping(self.impact_summary, "impact_summary"),
        )
        for name in (
            "source_context_package_hash",
            "target_capability_snapshot_hash",
        ):
            _hash(getattr(self, name), f"handoff proposal {name}")
        object.__setattr__(
            self,
            "transfer_mode",
            _enum(self.transfer_mode, HandoffTransferMode, "handoff transfer mode"),
        )
        object.__setattr__(
            self,
            "handoff_reason",
            _text(self.handoff_reason, "handoff_reason"),
        )
        object.__setattr__(
            self,
            "handoff_instruction",
            _text(self.handoff_instruction, "handoff_instruction"),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "proposal_schema_version": self.proposal_schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "subtask_id": str(self.subtask_id),
            "worker_item_id": str(self.worker_item_id),
            "source_execution_id": str(self.source_execution_id),
            "source_execution_version": self.source_execution_version,
            "source_session_id": str(self.source_session_id),
            "source_session_version": self.source_session_version,
            "source_session_last_sequence": self.source_session_last_sequence,
            "source_worker_id": str(self.source_worker_id),
            "source_worker_version": self.source_worker_version,
            "source_runtime_snapshot": _json_value(self.source_runtime_snapshot),
            "source_workspace_manifest_id": str(self.source_workspace_manifest_id),
            "source_workspace_manifest_version": self.source_workspace_manifest_version,
            "source_context_package_id": str(self.source_context_package_id),
            "source_context_package_hash": self.source_context_package_hash,
            "source_worker_context_version": self.source_worker_context_version,
            "observed_plan_snapshot_id": str(self.observed_plan_snapshot_id),
            "observed_plan_generation": self.observed_plan_generation,
            "observed_plan_head_version": self.observed_plan_head_version,
            "target_worker_id": str(self.target_worker_id),
            "target_worker_version": self.target_worker_version,
            "target_runtime_snapshot": _json_value(self.target_runtime_snapshot),
            "target_capability_snapshot_hash": self.target_capability_snapshot_hash,
            "target_policy_reason": _json_value(self.target_policy_reason),
            "target_policy_facts": _json_value(self.target_policy_facts),
            "transfer_mode": self.transfer_mode.value,
            "handoff_reason": self.handoff_reason,
            "handoff_instruction": self.handoff_instruction,
            "input_plan": _json_value(self.input_plan),
            "impact_summary": _json_value(self.impact_summary),
        }

    def to_mapping(self) -> dict[str, object]:
        return self.canonical_payload()

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.canonical_payload())

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeHandoffProposalPayload:
    proposal_schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID
    worker_item_id: UUID
    source_worker_item_version: int
    source_execution_id: UUID
    source_execution_version: int
    source_session_id: UUID
    source_session_version: int
    source_session_last_sequence: int
    source_worker_id: UUID
    source_worker_version: int
    source_workspace_manifest_id: UUID
    source_workspace_manifest_version: int
    source_context_package_id: UUID
    source_context_package_hash: str
    source_worker_context_version: int
    observed_plan_snapshot_id: UUID
    observed_plan_generation: int
    observed_plan_head_version: int
    source_run_id: UUID
    source_authority_id: UUID
    source_binding_version: int
    source_profile_ref: ProfileRef
    source_binding_target_type: str
    source_binding_target_id: UUID
    work_context_id: UUID
    work_context_revision_id: UUID
    work_context_hash: str
    work_context_version: int
    target_assignee: AssigneeRef
    target_profile_ref: ProfileRef | None
    handoff_reason: str
    handoff_instruction: str
    input_plan: Mapping[str, object]
    impact_summary: Mapping[str, object]
    transfer_mode: HandoffTransferMode = HandoffTransferMode.REUSE_EXACT

    def __post_init__(self) -> None:
        if (
            type(self.proposal_schema_version) is not int
            or self.proposal_schema_version != 2
        ):
            raise ValidationError("native handoff proposal schema version must be 2")
        for field in fields(self):
            name = field.name
            if name.endswith("_id"):
                _uuid(getattr(self, name), f"native handoff {name}")
            elif name.endswith("_version") and name != "proposal_schema_version":
                _positive(getattr(self, name), f"native handoff {name}")
        _positive(self.observed_plan_generation, "native observed plan generation")
        _non_negative(self.source_session_last_sequence, "native source sequence")
        _hash(self.source_context_package_hash, "native source package hash")
        _hash(self.work_context_hash, "native WorkContext hash")
        if not isinstance(self.source_profile_ref, ProfileRef) or (
            self.source_profile_ref.workspace_id != self.workspace_id
        ):
            raise ValidationError("native source requires exact original Profile")
        if self.source_binding_target_type not in {"worker_item", "subtask"} or (
            self.source_binding_target_id
            != (
                self.worker_item_id
                if self.source_binding_target_type == "worker_item"
                else self.subtask_id
            )
        ):
            raise ValidationError(
                "native source binding scope does not match WorkerItem"
            )
        if not isinstance(self.target_assignee, AssigneeRef):
            raise ValidationError("native target assignee is required")
        if self.target_assignee.kind == "profile":
            if not isinstance(self.target_profile_ref, ProfileRef) or (
                self.target_profile_ref.workspace_id != self.workspace_id
                or self.target_profile_ref.profile_id != self.target_assignee.id
            ):
                raise ValidationError(
                    "native target requires an exact Profile revision"
                )
        elif self.target_profile_ref is not None:
            raise ValidationError("human handoff cannot contain a runtime Profile")
        for name in ("handoff_reason", "handoff_instruction"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in ("input_plan", "impact_summary"):
            object.__setattr__(self, name, _mapping(getattr(self, name), name))
        if _enum(
            self.transfer_mode, HandoffTransferMode, "native transfer mode"
        ) is not (HandoffTransferMode.REUSE_EXACT):
            raise ValidationError("native handoff requires exact workspace reuse")

    @property
    def target_worker_id(self) -> None:
        return None

    @property
    def target_worker_version(self) -> None:
        return None

    @property
    def target_capability_snapshot_hash(self) -> None:
        return None

    @property
    def source_runtime_snapshot(self) -> Mapping[str, object]:
        return _mapping(
            {
                "execution_mode": "native_profile",
                "profile_ref": self.source_profile_ref.model_dump(mode="json"),
                "binding_version": self.source_binding_version,
            },
            "native source identity",
        )

    @property
    def target_runtime_snapshot(self) -> Mapping[str, object]:
        return _mapping(
            {
                "assignee": {
                    "kind": self.target_assignee.kind,
                    "id": str(self.target_assignee.id),
                },
                "profile_ref": None
                if self.target_profile_ref is None
                else (self.target_profile_ref.model_dump(mode="json")),
            },
            "native target identity",
        )

    @property
    def target_policy_reason(self) -> Mapping[str, object]:
        return _mapping({"selection": "explicit_assignee"}, "native selection")

    @property
    def target_policy_facts(self) -> Mapping[str, object]:
        return self.target_policy_reason

    def canonical_payload(self) -> dict[str, object]:
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        values.update(
            source_profile_ref=self.source_profile_ref.model_dump(mode="json"),
            target_profile_ref=None
            if self.target_profile_ref is None
            else (self.target_profile_ref.model_dump(mode="json")),
            target_assignee={
                "kind": self.target_assignee.kind,
                "id": str(self.target_assignee.id),
            },
        )
        return {key: _json_value(value) for key, value in values.items()}

    def to_mapping(self) -> dict[str, object]:
        return self.canonical_payload()

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.canonical_payload())

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_mapping(cls, value) -> NativeHandoffProposalPayload:
        if not isinstance(value, Mapping) or set(value) != {
            field.name for field in fields(cls)
        }:
            raise ValidationError("native handoff payload has an invalid shape")
        values = dict(value)
        for field in fields(cls):
            if field.name.endswith("_id"):
                values[field.name] = UUID(str(values[field.name]))
        values["source_profile_ref"] = ProfileRef.model_validate(
            values["source_profile_ref"]
        )
        values["target_profile_ref"] = (
            None
            if values["target_profile_ref"] is None
            else (ProfileRef.model_validate(values["target_profile_ref"]))
        )
        assignee = values["target_assignee"]
        if not isinstance(assignee, Mapping) or set(assignee) != {"kind", "id"}:
            raise ValidationError("native handoff target assignee has an invalid shape")
        values["target_assignee"] = AssigneeRef(assignee["kind"], assignee["id"])
        values["transfer_mode"] = _enum(
            values["transfer_mode"], HandoffTransferMode, "native transfer mode"
        )
        return cls(**values)


@dataclass(slots=True)
class HandoffProposal:
    id: UUID
    payload: HandoffProposalPayload | NativeHandoffProposalPayload
    proposal_hash: str
    status: HandoffProposalStatus
    stale_reason: str | None
    version: int
    created_by_type: str
    created_by_id: str
    source_channel: HandoffSourceChannel
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None
    cancelled_at: datetime | None

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        payload: HandoffProposalPayload | NativeHandoffProposalPayload,
        created_by_type: str,
        created_by_id: str,
        source_channel: HandoffSourceChannel | str,
        created_at: datetime,
        expires_at: datetime,
    ) -> HandoffProposal:
        if not isinstance(
            payload, (HandoffProposalPayload, NativeHandoffProposalPayload)
        ):
            raise ValidationError("handoff proposal payload is required")
        return cls(
            id=id,
            payload=payload,
            proposal_hash=payload.content_hash,
            status=HandoffProposalStatus.PREPARED,
            stale_reason=None,
            version=1,
            created_by_type=created_by_type,
            created_by_id=created_by_id,
            source_channel=_enum(
                source_channel, HandoffSourceChannel, "handoff source channel"
            ),
            created_at=created_at,
            expires_at=expires_at,
            confirmed_at=None,
            cancelled_at=None,
        )

    def __post_init__(self) -> None:
        _uuid(self.id, "handoff proposal id")
        if not isinstance(
            self.payload, (HandoffProposalPayload, NativeHandoffProposalPayload)
        ):
            raise ValidationError("handoff proposal payload is required")
        _hash(self.proposal_hash, "handoff proposal hash")
        if self.proposal_hash != self.payload.content_hash:
            raise ValidationError("handoff proposal hash does not match payload")
        object.__setattr__(
            self,
            "status",
            _enum(self.status, HandoffProposalStatus, "handoff proposal status"),
        )
        object.__setattr__(
            self,
            "stale_reason",
            _optional_text(self.stale_reason, "handoff proposal stale_reason"),
        )
        _positive(self.version, "handoff proposal version")
        object.__setattr__(
            self,
            "created_by_type",
            _text(self.created_by_type, "handoff proposal created_by_type"),
        )
        object.__setattr__(
            self,
            "created_by_id",
            _text(self.created_by_id, "handoff proposal created_by_id"),
        )
        object.__setattr__(
            self,
            "source_channel",
            _enum(self.source_channel, HandoffSourceChannel, "handoff source channel"),
        )
        _aware(self.created_at, "handoff proposal created_at")
        _aware(self.expires_at, "handoff proposal expires_at")
        if self.expires_at <= self.created_at:
            raise ValidationError(
                "handoff proposal expires_at must be after created_at"
            )
        if self.confirmed_at is not None:
            _aware(self.confirmed_at, "handoff proposal confirmed_at")
        if self.cancelled_at is not None:
            _aware(self.cancelled_at, "handoff proposal cancelled_at")
        self._validate_lifecycle()

    def mark_confirmed(self, *, now: datetime) -> None:
        self._assert_prepared()
        _aware(now, "handoff proposal confirmed_at")
        if now >= self.expires_at:
            raise InvalidTransition("expired handoff proposal cannot be confirmed")
        self.status = HandoffProposalStatus.CONFIRMED
        self.confirmed_at = now
        self.version += 1

    def mark_stale(self, *, reason: str, now: datetime) -> None:
        self._assert_prepared()
        _aware(now, "handoff proposal stale_at")
        self.status = HandoffProposalStatus.STALE
        self.stale_reason = _text(reason, "handoff proposal stale_reason")
        self.version += 1

    def mark_cancelled(self, *, now: datetime) -> None:
        self._assert_prepared()
        _aware(now, "handoff proposal cancelled_at")
        self.status = HandoffProposalStatus.CANCELLED
        self.cancelled_at = now
        self.version += 1

    def mark_expired(self, *, now: datetime) -> None:
        self._assert_prepared()
        _aware(now, "handoff proposal expired_at")
        if now < self.expires_at:
            raise InvalidTransition("handoff proposal has not expired")
        self.status = HandoffProposalStatus.EXPIRED
        self.version += 1

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "payload": self.payload.to_mapping(),
            "proposal_hash": self.proposal_hash,
            "status": self.status.value,
            "stale_reason": self.stale_reason,
            "version": self.version,
            "created_by_type": self.created_by_type,
            "created_by_id": self.created_by_id,
            "source_channel": self.source_channel.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "confirmed_at": (
                self.confirmed_at.isoformat() if self.confirmed_at is not None else None
            ),
            "cancelled_at": (
                self.cancelled_at.isoformat() if self.cancelled_at is not None else None
            ),
        }

    def _assert_prepared(self) -> None:
        if self.status is not HandoffProposalStatus.PREPARED:
            raise InvalidTransition(
                f"handoff proposal cannot change from {self.status}"
            )

    def _validate_lifecycle(self) -> None:
        if self.status is HandoffProposalStatus.PREPARED:
            if self.stale_reason is not None or self.confirmed_at or self.cancelled_at:
                raise ValidationError("prepared handoff proposal has terminal facts")
        elif self.status is HandoffProposalStatus.CONFIRMED:
            if self.confirmed_at is None or self.stale_reason or self.cancelled_at:
                raise ValidationError("confirmed handoff proposal lifecycle is invalid")
        elif self.status is HandoffProposalStatus.STALE:
            if self.stale_reason is None or self.confirmed_at or self.cancelled_at:
                raise ValidationError("stale handoff proposal lifecycle is invalid")
        elif self.status is HandoffProposalStatus.CANCELLED:
            if self.cancelled_at is None or self.stale_reason or self.confirmed_at:
                raise ValidationError("cancelled handoff proposal lifecycle is invalid")
        elif self.status is HandoffProposalStatus.EXPIRED and (
            self.stale_reason or self.confirmed_at or self.cancelled_at
        ):
            raise ValidationError("expired handoff proposal lifecycle is invalid")


@dataclass(slots=True)
class HandoffTransition:
    id: UUID
    workspace_id: UUID
    worker_item_id: UUID
    proposal_id: UUID
    source_execution_id: UUID
    source_session_id: UUID
    source_workspace_manifest_id: UUID
    target_worker_id: UUID | None
    target_runtime_snapshot: Mapping[str, object]
    target_execution_id: UUID | None
    target_session_id: UUID | None
    target_workspace_manifest_id: UUID | None
    handoff_context_bundle_id: UUID | None
    target_context_version: int
    target_context_package_id: UUID | None
    target_context_package_hash: str | None
    status: HandoffTransitionStatus
    phase: HandoffPhase
    writer_state: HandoffWriterState
    source_checkpoint_sequence: int | None
    source_checkpoint_facts: Mapping[str, object] | None
    source_stop_sequence: int | None
    source_stop_facts: Mapping[str, object] | None
    source_workspace_freeze_operation_id: UUID | None
    source_workspace_freeze_facts: Mapping[str, object] | None
    source_input_cutoff_sequence: int
    failure_code: str | None
    blocked_operation_id: UUID | None
    logical_attempt: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    version: int
    confirmed_by_type: str
    confirmed_by_id: str
    confirmed_source_channel: HandoffSourceChannel
    confirmed_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    handoff_schema_version: int = 1
    native_payload: NativeHandoffProposalPayload | None = None
    target_binding_version: int | None = None
    manual_context_facts: Mapping[str, object] | None = None

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        proposal: HandoffProposal,
        confirmed_by_type: str,
        confirmed_by_id: str,
        confirmed_at: datetime,
    ) -> HandoffTransition:
        if not isinstance(proposal, HandoffProposal):
            raise ValidationError("handoff transition requires a proposal")
        if proposal.status is not HandoffProposalStatus.CONFIRMED:
            raise InvalidTransition("handoff transition requires a confirmed proposal")
        payload = proposal.payload
        return cls(
            id=id,
            workspace_id=payload.workspace_id,
            worker_item_id=payload.worker_item_id,
            proposal_id=proposal.id,
            source_execution_id=payload.source_execution_id,
            source_session_id=payload.source_session_id,
            source_workspace_manifest_id=payload.source_workspace_manifest_id,
            target_worker_id=payload.target_worker_id,
            target_runtime_snapshot=payload.target_runtime_snapshot,
            target_execution_id=None,
            target_session_id=None,
            target_workspace_manifest_id=None,
            handoff_context_bundle_id=None,
            target_context_version=(
                _positive(
                    payload.impact_summary["worker_item_context_version"],
                    "native target context version",
                )
                if isinstance(payload, NativeHandoffProposalPayload)
                else payload.source_worker_context_version
            ),
            target_context_package_id=None,
            target_context_package_hash=None,
            status=HandoffTransitionStatus.PENDING,
            phase=HandoffPhase.CHECKPOINT_SOURCE,
            writer_state=HandoffWriterState.SOURCE_ACTIVE,
            source_checkpoint_sequence=None,
            source_checkpoint_facts=None,
            source_stop_sequence=None,
            source_stop_facts=None,
            source_workspace_freeze_operation_id=None,
            source_workspace_freeze_facts=None,
            source_input_cutoff_sequence=payload.source_session_last_sequence,
            failure_code=None,
            blocked_operation_id=None,
            logical_attempt=1,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=None,
            version=1,
            confirmed_by_type=confirmed_by_type,
            confirmed_by_id=confirmed_by_id,
            confirmed_source_channel=proposal.source_channel,
            confirmed_at=confirmed_at,
            started_at=None,
            completed_at=None,
            handoff_schema_version=payload.proposal_schema_version,
            native_payload=payload
            if isinstance(payload, NativeHandoffProposalPayload)
            else None,
        )

    def __post_init__(self) -> None:
        for name in (
            "id",
            "workspace_id",
            "worker_item_id",
            "proposal_id",
            "source_execution_id",
            "source_session_id",
            "source_workspace_manifest_id",
        ):
            _uuid(getattr(self, name), f"handoff transition {name}")
        if self.handoff_schema_version == 1:
            _uuid(self.target_worker_id, "handoff transition target_worker_id")
            if (
                self.native_payload is not None
                or self.target_binding_version is not None
                or (self.manual_context_facts is not None)
            ):
                raise ValidationError("legacy transition cannot contain native fields")
        elif self.handoff_schema_version == 2:
            if type(self.handoff_schema_version) is not int:
                raise ValidationError(
                    "native transition discriminator must be integer 2"
                )
            native = self.native_payload
            if not isinstance(native, NativeHandoffProposalPayload) or (
                (
                    self.workspace_id,
                    self.worker_item_id,
                    self.source_execution_id,
                    self.source_session_id,
                    self.source_workspace_manifest_id,
                )
                != (
                    native.workspace_id,
                    native.worker_item_id,
                    native.source_execution_id,
                    native.source_session_id,
                    native.source_workspace_manifest_id,
                )
                or dict(self.target_runtime_snapshot)
                != dict(native.target_runtime_snapshot)
            ):
                raise ValidationError(
                    "native transition immutable pins do not match proposal"
                )
            _optional_uuid(self.target_worker_id, "native reserved target worker")
            if self.target_binding_version is not None:
                _positive(self.target_binding_version, "native target binding version")
            if native.target_assignee.kind == "human" and any(
                value is not None
                for value in (
                    self.target_worker_id,
                    self.target_execution_id,
                    self.target_session_id,
                    self.target_workspace_manifest_id,
                    self.handoff_context_bundle_id,
                    self.target_context_package_id,
                    self.target_context_package_hash,
                )
            ):
                raise ValidationError(
                    "manual handoff cannot contain automated target records"
                )
            if self.manual_context_facts is not None:
                if native.target_assignee.kind != "human":
                    raise ValidationError(
                        "Profile handoff cannot contain manual convergence facts"
                    )
                self.manual_context_facts = _mapping(
                    self.manual_context_facts, "manual context facts"
                )
        else:
            raise ValidationError("unsupported handoff transition schema")
        for name in (
            "target_execution_id",
            "target_session_id",
            "target_workspace_manifest_id",
            "handoff_context_bundle_id",
            "target_context_package_id",
            "blocked_operation_id",
        ):
            _optional_uuid(getattr(self, name), f"handoff transition {name}")
        object.__setattr__(
            self,
            "target_runtime_snapshot",
            _mapping(self.target_runtime_snapshot, "target_runtime_snapshot"),
        )
        _positive(self.target_context_version, "handoff target_context_version")
        object.__setattr__(
            self,
            "target_context_package_hash",
            _optional_hash(
                self.target_context_package_hash,
                "handoff target_context_package_hash",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum(self.status, HandoffTransitionStatus, "handoff transition status"),
        )
        object.__setattr__(
            self,
            "phase",
            _enum(self.phase, HandoffPhase, "handoff phase"),
        )
        object.__setattr__(
            self,
            "writer_state",
            _enum(self.writer_state, HandoffWriterState, "handoff writer state"),
        )
        _optional_non_negative(
            self.source_checkpoint_sequence,
            "source_checkpoint_sequence",
        )
        _optional_non_negative(self.source_stop_sequence, "source_stop_sequence")
        for name in (
            "source_checkpoint_facts",
            "source_stop_facts",
            "source_workspace_freeze_facts",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _mapping(value, name))
        _optional_uuid(
            self.source_workspace_freeze_operation_id,
            "source_workspace_freeze_operation_id",
        )
        _non_negative(
            self.source_input_cutoff_sequence,
            "source_input_cutoff_sequence",
        )
        object.__setattr__(
            self,
            "failure_code",
            _optional_text(self.failure_code, "handoff transition failure_code"),
        )
        _positive(self.logical_attempt, "handoff logical_attempt")
        object.__setattr__(
            self,
            "lease_owner",
            _optional_text(self.lease_owner, "handoff lease_owner"),
        )
        _optional_aware(self.lease_expires_at, "handoff lease_expires_at")
        _optional_aware(self.next_attempt_at, "handoff next_attempt_at")
        _positive(self.version, "handoff transition version")
        object.__setattr__(
            self,
            "confirmed_by_type",
            _text(self.confirmed_by_type, "handoff confirmed_by_type"),
        )
        object.__setattr__(
            self,
            "confirmed_by_id",
            _text(self.confirmed_by_id, "handoff confirmed_by_id"),
        )
        object.__setattr__(
            self,
            "confirmed_source_channel",
            _enum(
                self.confirmed_source_channel,
                HandoffSourceChannel,
                "handoff confirmed source channel",
            ),
        )
        _aware(self.confirmed_at, "handoff confirmed_at")
        _optional_aware(self.started_at, "handoff started_at")
        _optional_aware(self.completed_at, "handoff completed_at")
        if self.status is HandoffTransitionStatus.CONVERGED and (
            self.phase is not HandoffPhase.COMPLETE or self.completed_at is None
        ):
            raise ValidationError("converged handoff must be complete")
        workspace_only_unknown = (
            self.writer_state is HandoffWriterState.WRITER_NONE
            and self.source_stop_sequence is not None
            and self.phase
            in {
                HandoffPhase.FREEZE_SOURCE_WORKSPACE,
                HandoffPhase.CREATE_TARGET_EXECUTION,
                HandoffPhase.ASSEMBLE_BUNDLE,
                HandoffPhase.TRANSFER_WORKSPACE,
            }
        )
        if self.status is HandoffTransitionStatus.BLOCKED_UNKNOWN and (
            self.blocked_operation_id is None
            or (
                self.writer_state is not HandoffWriterState.UNKNOWN
                and not workspace_only_unknown
            )
        ):
            raise ValidationError(
                "blocked_unknown handoff requires an unknown operation and writer"
            )

    def start(self, *, now: datetime) -> None:
        if self.status is not HandoffTransitionStatus.PENDING:
            raise InvalidTransition("only a pending handoff can start")
        if self.phase is not HandoffPhase.CHECKPOINT_SOURCE:
            raise InvalidTransition("handoff must start at checkpoint_source")
        _aware(now, "handoff transition started_at")
        self.status = HandoffTransitionStatus.RUNNING
        self.started_at = now
        self.version += 1

    def resume(self, *, now: datetime) -> None:
        """Resume a known, retryable block while retaining the same attempt."""

        if self.status is not HandoffTransitionStatus.BLOCKED:
            raise InvalidTransition("only a blocked handoff can resume")
        _aware(now, "handoff transition resumed_at")
        self.status = HandoffTransitionStatus.RUNNING
        self.failure_code = None
        self.blocked_operation_id = None
        self.next_attempt_at = None
        self.version += 1

    def defer(self, *, now: datetime, next_attempt_at: datetime) -> None:
        """Release the coordinator claim and retry this phase later."""

        if self.status is not HandoffTransitionStatus.RUNNING:
            raise InvalidTransition("only a running handoff can be deferred")
        _aware(now, "handoff transition deferred_at")
        _aware(next_attempt_at, "handoff transition next_attempt_at")
        if next_attempt_at < now:
            raise ValidationError("handoff next_attempt_at cannot precede now")
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = next_attempt_at
        self.version += 1

    def release_claim(self) -> None:
        """Release a coordinator claim without changing the handoff phase."""

        self.lease_owner = None
        self.lease_expires_at = None
        self.version += 1

    def advance(self, target: HandoffPhase | str, *, now: datetime) -> None:
        if self.status is not HandoffTransitionStatus.RUNNING:
            raise InvalidTransition("only a running handoff can advance")
        if self.phase is HandoffPhase.COMPLETE:
            raise InvalidTransition("completed handoff phase cannot advance")
        requested = _enum(target, HandoffPhase, "handoff phase")
        phases = tuple(HandoffPhase)
        current_index = phases.index(self.phase)
        if requested is not phases[current_index + 1]:
            raise InvalidTransition(
                f"handoff cannot move from {self.phase} to {requested}"
            )
        expected_writer = _PHASE_WRITERS[self.phase]
        if self.writer_state is not expected_writer:
            raise InvalidTransition(
                f"handoff writer state {self.writer_state} is invalid for {self.phase}"
            )
        _aware(now, "handoff phase timestamp")
        self.phase = requested
        self.writer_state = _PHASE_WRITERS[requested]
        self.failure_code = None
        self.lease_owner = None
        self.lease_expires_at = None
        self.version += 1

    def recover_to_next_phase(self, *, now: datetime) -> None:
        """Apply an evidence-backed phase confirmation without replaying work."""

        if self.status not in {
            HandoffTransitionStatus.BLOCKED,
            HandoffTransitionStatus.BLOCKED_UNKNOWN,
            HandoffTransitionStatus.FAILED,
        }:
            raise InvalidTransition("only a failed handoff can be recovered")
        if self.phase is HandoffPhase.COMPLETE:
            raise InvalidTransition("completed handoff phase cannot be recovered")
        _aware(now, "handoff recovery timestamp")
        phases = tuple(HandoffPhase)
        next_phase = phases[phases.index(self.phase) + 1]
        self.phase = next_phase
        self.writer_state = _PHASE_WRITERS[next_phase]
        self.status = HandoffTransitionStatus.BLOCKED
        self.failure_code = None
        self.blocked_operation_id = None
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = now
        self.version += 1

    def prepare_recovery_retry(
        self,
        *,
        now: datetime,
        new_logical_attempt: bool,
    ) -> None:
        """Make one proven no-side-effect phase eligible for a fresh attempt."""

        if self.status not in {
            HandoffTransitionStatus.BLOCKED,
            HandoffTransitionStatus.BLOCKED_UNKNOWN,
            HandoffTransitionStatus.FAILED,
        }:
            raise InvalidTransition("only a failed handoff can be retried")
        if self.phase is HandoffPhase.COMPLETE:
            raise InvalidTransition("completed handoff phase cannot be retried")
        _aware(now, "handoff recovery retry timestamp")
        if new_logical_attempt:
            self.logical_attempt += 1
            if self.phase is HandoffPhase.FREEZE_SOURCE_WORKSPACE:
                self.source_workspace_freeze_operation_id = None
                self.source_workspace_freeze_facts = None
            elif self.phase is HandoffPhase.TRANSFER_WORKSPACE:
                self.target_workspace_manifest_id = None
        self.status = HandoffTransitionStatus.BLOCKED
        self.writer_state = _PHASE_WRITERS[self.phase]
        self.failure_code = None
        self.blocked_operation_id = None
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = now
        self.version += 1

    def begin_recovery_attempt(
        self,
        *,
        target_worker_id: UUID,
        target_runtime_snapshot: Mapping[str, object],
        now: datetime,
    ) -> None:
        """Retire the current target pointers and reopen target creation."""

        if self.status not in {
            HandoffTransitionStatus.BLOCKED,
            HandoffTransitionStatus.BLOCKED_UNKNOWN,
            HandoffTransitionStatus.FAILED,
        }:
            raise InvalidTransition(
                "only a failed handoff can start a recovery attempt"
            )
        if self.phase is HandoffPhase.COMPLETE:
            raise InvalidTransition("completed handoff cannot start a recovery attempt")
        _uuid(target_worker_id, "recovery target_worker_id")
        snapshot = _mapping(
            target_runtime_snapshot,
            "recovery target_runtime_snapshot",
        )
        if snapshot.get("worker_id") != str(target_worker_id):
            raise ValidationError("recovery target runtime worker identity is invalid")
        _aware(now, "handoff recovery attempt timestamp")
        self.target_worker_id = target_worker_id
        self.target_runtime_snapshot = snapshot
        self.target_execution_id = None
        self.target_session_id = None
        self.target_workspace_manifest_id = None
        self.handoff_context_bundle_id = None
        self.target_context_package_id = None
        self.target_context_package_hash = None
        self.phase = HandoffPhase.CREATE_TARGET_EXECUTION
        self.writer_state = HandoffWriterState.WRITER_NONE
        self.status = HandoffTransitionStatus.BLOCKED
        self.failure_code = None
        self.blocked_operation_id = None
        self.logical_attempt += 1
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = now
        self.version += 1

    def converge(self, *, now: datetime) -> None:
        if self.status is not HandoffTransitionStatus.RUNNING:
            raise InvalidTransition("only a running handoff can converge")
        if self.phase is not HandoffPhase.COMPLETE:
            raise InvalidTransition("handoff can converge only at complete")
        if self.writer_state is not HandoffWriterState.TARGET_ACTIVE:
            raise InvalidTransition("handoff target writer is not active")
        _aware(now, "handoff transition completed_at")
        self.status = HandoffTransitionStatus.CONVERGED
        self.completed_at = now
        self.failure_code = None
        self.lease_owner = None
        self.lease_expires_at = None
        self.version += 1

    def block(
        self,
        *,
        failure_code: str,
        now: datetime,
        status: HandoffTransitionStatus = HandoffTransitionStatus.BLOCKED,
        blocked_operation_id: UUID | None = None,
        workspace_only: bool = False,
    ) -> None:
        if self.status is HandoffTransitionStatus.CONVERGED:
            raise InvalidTransition("converged handoff cannot be blocked")
        requested = _enum(status, HandoffTransitionStatus, "handoff block status")
        if requested not in {
            HandoffTransitionStatus.BLOCKED,
            HandoffTransitionStatus.BLOCKED_UNKNOWN,
            HandoffTransitionStatus.FAILED,
        }:
            raise InvalidTransition("handoff block status is not a failure state")
        if requested is HandoffTransitionStatus.BLOCKED_UNKNOWN:
            _uuid(blocked_operation_id, "blocked_operation_id")
            if workspace_only:
                if (
                    self.source_stop_sequence is None
                    or self.writer_state is not HandoffWriterState.WRITER_NONE
                    or self.phase
                    not in {
                        HandoffPhase.FREEZE_SOURCE_WORKSPACE,
                        HandoffPhase.CREATE_TARGET_EXECUTION,
                        HandoffPhase.ASSEMBLE_BUNDLE,
                        HandoffPhase.TRANSFER_WORKSPACE,
                    }
                ):
                    raise InvalidTransition(
                        "workspace-only unknown requires an exactly stopped source"
                    )
            else:
                self.writer_state = HandoffWriterState.UNKNOWN
        _aware(now, "handoff block timestamp")
        self.failure_code = _text(failure_code, "handoff failure_code")
        self.status = requested
        self.blocked_operation_id = blocked_operation_id
        self.lease_owner = None
        self.lease_expires_at = None
        self.version += 1

    def record_checkpoint(self, *, sequence: int, facts: Mapping[str, object]) -> None:
        if self.source_checkpoint_sequence is not None:
            raise InvalidTransition("handoff checkpoint evidence is write-once")
        _non_negative(sequence, "source_checkpoint_sequence")
        if sequence < self.source_input_cutoff_sequence:
            raise ValidationError(
                "source checkpoint sequence cannot precede the input cutoff"
            )
        self.source_checkpoint_sequence = sequence
        self.source_checkpoint_facts = _mapping(facts, "source_checkpoint_facts")
        self.version += 1

    def record_stop(self, *, sequence: int, facts: Mapping[str, object]) -> None:
        if self.source_stop_sequence is not None:
            raise InvalidTransition("handoff stop evidence is write-once")
        _non_negative(sequence, "source_stop_sequence")
        if self.handoff_schema_version == 2:
            from sidera_control_plane.domain.handoff_evidence import (
                HandoffCheckpointEvidence,
                HandoffStopEvidence,
            )

            if self.source_checkpoint_facts is None:
                raise ValidationError("native stop requires original snapshot")
            checkpoint = HandoffCheckpointEvidence.from_native_mapping(
                self.source_checkpoint_facts
            )
            stop = HandoffStopEvidence.from_native_mapping(facts)
            stop.assert_matches(checkpoint)
            if stop.accepted_sequence != sequence:
                raise ValidationError("native stop sequence does not match proof")
        elif self.source_checkpoint_sequence != sequence:
            raise ValidationError(
                "source stop sequence must match the checkpoint sequence"
            )
        self.source_stop_sequence = sequence
        self.source_stop_facts = _mapping(facts, "source_stop_facts")
        self.version += 1

    def converge_manual_human(
        self, *, facts: Mapping[str, object], now: datetime
    ) -> None:
        native = self.native_payload
        if (
            self.handoff_schema_version != 2
            or native is None
            or native.target_assignee.kind != "human"
            or self.status is not HandoffTransitionStatus.RUNNING
            or self.phase is not HandoffPhase.CREATE_TARGET_EXECUTION
            or self.writer_state is not HandoffWriterState.WRITER_NONE
            or self.source_stop_facts is None
            or self.source_workspace_freeze_facts is None
            or self.target_binding_version is None
            or self.manual_context_facts is not None
        ):
            raise InvalidTransition(
                "manual convergence requires stopped frozen native human handoff"
            )
        _aware(now, "manual handoff completed_at")
        self.manual_context_facts = _mapping(facts, "manual context facts")
        self.phase = HandoffPhase.COMPLETE
        self.status = HandoffTransitionStatus.CONVERGED
        self.writer_state = HandoffWriterState.WRITER_NONE
        self.completed_at = now
        self.failure_code = None
        self.blocked_operation_id = None
        self.lease_owner = None
        self.lease_expires_at = None
        self.version += 1

    def record_workspace_freeze(
        self, *, operation_id: UUID, facts: Mapping[str, object]
    ) -> None:
        _uuid(operation_id, "source_workspace_freeze_operation_id")
        if self.source_stop_sequence is None:
            raise InvalidTransition("workspace freeze requires an exact source stop")
        if self.source_workspace_freeze_operation_id not in (None, operation_id):
            raise InvalidTransition("handoff workspace freeze evidence is write-once")
        if self.source_workspace_freeze_facts is not None:
            raise InvalidTransition("handoff workspace freeze evidence is write-once")
        self.source_workspace_freeze_operation_id = operation_id
        self.source_workspace_freeze_facts = _mapping(
            facts, "source_workspace_freeze_facts"
        )
        self.version += 1

    def reserve_workspace_freeze_operation(self, *, operation_id: UUID) -> None:
        """Reserve the stable freeze identity before crossing the provider boundary."""

        _uuid(operation_id, "source_workspace_freeze_operation_id")
        if self.source_stop_sequence is None:
            raise InvalidTransition("workspace freeze requires an exact source stop")
        if self.source_workspace_freeze_operation_id is not None:
            if self.source_workspace_freeze_operation_id != operation_id:
                raise InvalidTransition(
                    "handoff workspace freeze operation is already reserved"
                )
            return
        if self.source_workspace_freeze_facts is not None:
            raise InvalidTransition("freeze facts cannot precede the freeze operation")
        self.source_workspace_freeze_operation_id = operation_id
        self.version += 1


_PHASE_WRITERS: dict[HandoffPhase, HandoffWriterState] = {
    HandoffPhase.CHECKPOINT_SOURCE: HandoffWriterState.SOURCE_ACTIVE,
    HandoffPhase.STOP_SOURCE: HandoffWriterState.SOURCE_STOP_REQUESTED,
    HandoffPhase.FREEZE_SOURCE_WORKSPACE: HandoffWriterState.WRITER_NONE,
    HandoffPhase.CREATE_TARGET_EXECUTION: HandoffWriterState.WRITER_NONE,
    HandoffPhase.ASSEMBLE_BUNDLE: HandoffWriterState.WRITER_NONE,
    HandoffPhase.TRANSFER_WORKSPACE: HandoffWriterState.WRITER_NONE,
    HandoffPhase.START_TARGET_SESSION: HandoffWriterState.TARGET_STARTING,
    HandoffPhase.ACKNOWLEDGE_CONTEXT: HandoffWriterState.TARGET_STARTING,
    HandoffPhase.TRANSFER_SAFE_INPUTS: HandoffWriterState.TARGET_ACTIVE,
    HandoffPhase.ACTIVATE_TARGET: HandoffWriterState.TARGET_ACTIVE,
    HandoffPhase.COMPLETE: HandoffWriterState.TARGET_ACTIVE,
}


def _enum[EnumValue: StrEnum](
    value: object, enum_type: type[EnumValue], label: str
) -> EnumValue:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {label}: {value!r}") from exc


def _uuid(value: object, label: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValidationError(f"{label} must be a UUID")
    return value


def _optional_uuid(value: object, label: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, label)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required")
    clean = value.strip()
    if len(clean) > _MAX_TEXT_LENGTH:
        raise ValidationError(f"{label} is too long")
    return clean


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _text_tuple(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{label} must be a sequence of strings")
    clean: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _text(value, label)
        if item not in seen:
            seen.add(item)
            clean.append(item)
    return tuple(clean)


def _positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{label} must be positive")
    return value


def _non_negative(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{label} must be non-negative")
    return value


def _optional_non_negative(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _non_negative(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} must be a boolean")
    return value


def _aware(value: object, label: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError(f"{label} must be timezone-aware")
    return value


def _optional_aware(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _aware(value, label)


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a lowercase SHA-256 hash")
    return value


def _optional_hash(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _hash(value, label)


def _mapping(value: object, label: str, *, depth: int = 0) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be a mapping")
    if depth > _MAX_MAP_DEPTH:
        raise ValidationError(f"{label} is too deeply nested")
    if len(value) > _MAX_MAP_ENTRIES:
        raise ValidationError(f"{label} has too many entries")
    frozen = MappingProxyType(
        {
            _mapping_key(key, label): _freeze(item, f"{label}.{key}", depth=depth + 1)
            for key, item in value.items()
        }
    )
    if len(_canonical_bytes(_json_value(frozen))) > _MAX_MAP_BYTES:
        raise ValidationError(f"{label} is too large")
    return frozen


def _mapping_key(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} keys must be non-empty strings")
    return value.strip()


def _freeze(value: object, label: str, *, depth: int) -> object:
    if isinstance(value, Mapping):
        return _mapping(value, label, depth=depth)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_MAP_ENTRIES:
            raise ValidationError(f"{label} has too many items")
        return tuple(_freeze(item, label, depth=depth) for item in value)
    if isinstance(value, (str, int, bool)) or value is None:
        if isinstance(value, str) and len(value) > _MAX_TEXT_LENGTH:
            raise ValidationError(f"{label} contains text that is too long")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, (UUID, datetime, StrEnum)):
        return value
    raise ValidationError(f"{label} contains an unsupported value")


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (UUID, datetime, StrEnum)):
        return str(value) if not isinstance(value, datetime) else value.isoformat()
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _json_value(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("handoff value cannot be canonicalized") from exc
