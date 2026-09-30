from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast
from uuid import UUID

from sidera_control_plane.domain.artifacts import ArtifactType, EvidenceClaimStatus
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import ExecutionStatus

MAX_INPUT_GROUP_CANDIDATES = 32
MAX_INPUT_GROUP_ARTIFACT_SELECTORS = 8
MAX_INPUT_GROUP_EVIDENCE_SELECTORS = 8
_MAX_TEXT_LENGTH = 16_384
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class InputGroupInvalid(ValidationError):
    code = "input_group_invalid"

    def __init__(self, message: str) -> None:
        super().__init__(f"input_group_invalid: {message}")


class InputWaitCycle(ValidationError):
    code = "input_wait_cycle"

    def __init__(self, message: str, *, nodes: tuple[str, ...] = ()) -> None:
        super().__init__(
            f"input_wait_cycle: {message}",
            details={"cycle_nodes": list(nodes)},
        )


class InputGroupMode(StrEnum):
    ALL = "all"
    AT_LEAST = "at_least"


class InputSelectionStrategy(StrEnum):
    PLAN_ORDER = "plan_order"


@dataclass(frozen=True, slots=True)
class ResultInputArtifact:
    """Storage-independent identity for one exact result artifact."""

    artifact_id: UUID
    artifact_type: ArtifactType
    content_hash: str
    size_bytes: int
    media_type: str

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputArtifact:
        mapping = _descriptor_mapping(
            value,
            "result input artifact",
            {
                "artifact_id",
                "artifact_type",
                "content_hash",
                "size_bytes",
                "media_type",
            },
        )
        return cls(
            artifact_id=_descriptor_uuid(mapping.get("artifact_id"), "artifact_id"),
            artifact_type=_descriptor_artifact_type(mapping.get("artifact_type")),
            content_hash=_descriptor_hash(mapping.get("content_hash"), "content_hash"),
            size_bytes=_descriptor_non_negative_int(
                mapping.get("size_bytes"), "size_bytes"
            ),
            media_type=_descriptor_text(mapping.get("media_type"), "media_type"),
        )

    def __post_init__(self) -> None:
        _descriptor_uuid(self.artifact_id, "artifact_id")
        object.__setattr__(
            self, "artifact_type", _descriptor_artifact_type(self.artifact_type)
        )
        object.__setattr__(
            self,
            "content_hash",
            _descriptor_hash(self.content_hash, "content_hash"),
        )
        _descriptor_non_negative_int(self.size_bytes, "size_bytes")
        object.__setattr__(
            self, "media_type", _descriptor_text(self.media_type, "media_type")
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_id": str(self.artifact_id),
            "artifact_type": self.artifact_type.value,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }


@dataclass(frozen=True, slots=True)
class ResultInputEvidence:
    """Exact Evidence identity and the Artifact closure it authorizes."""

    evidence_id: UUID
    acceptance_criterion_ref: str
    artifact_ids: tuple[UUID, ...]

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputEvidence:
        mapping = _descriptor_mapping(
            value,
            "result input evidence",
            {"evidence_id", "acceptance_criterion_ref", "artifact_ids"},
        )
        return cls(
            evidence_id=_descriptor_uuid(mapping.get("evidence_id"), "evidence_id"),
            acceptance_criterion_ref=_descriptor_text(
                mapping.get("acceptance_criterion_ref"),
                "acceptance_criterion_ref",
            ),
            artifact_ids=_descriptor_uuid_array(
                mapping.get("artifact_ids"), "artifact_ids"
            ),
        )

    def __post_init__(self) -> None:
        _descriptor_uuid(self.evidence_id, "evidence_id")
        object.__setattr__(
            self,
            "acceptance_criterion_ref",
            _descriptor_text(self.acceptance_criterion_ref, "acceptance_criterion_ref"),
        )
        object.__setattr__(
            self,
            "artifact_ids",
            _descriptor_uuid_array(self.artifact_ids, "artifact_ids"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "evidence_id": str(self.evidence_id),
            "acceptance_criterion_ref": self.acceptance_criterion_ref,
            "artifact_ids": [str(item) for item in self.artifact_ids],
        }


@dataclass(frozen=True, slots=True)
class ResultInputSource:
    """One explicitly resolved result identity before readiness evaluation."""

    worker_item_key: str
    worker_item_id: UUID | None
    subtask_path: tuple[str, ...]
    effective_request_id: UUID | None
    allowed_lineage_ids: tuple[UUID, ...]
    effective_execution_id: UUID | None
    attempt: int | None
    execution_status: ExecutionStatus | None
    inheritance_refs: tuple[str, ...]
    artifacts: tuple[ResultInputArtifact, ...]
    evidence: tuple[ResultInputEvidence, ...]
    retry_possible: bool
    outcome_unknown: bool
    reason_code: str | None
    source_basis_hash: str | None = None

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputSource:
        mapping = _descriptor_mapping(
            value,
            "result input source",
            {
                "worker_item_key",
                "worker_item_id",
                "subtask_path",
                "effective_request_id",
                "allowed_lineage_ids",
                "effective_execution_id",
                "attempt",
                "execution_status",
                "inheritance_refs",
                "artifacts",
                "evidence",
                "retry_possible",
                "outcome_unknown",
                "reason_code",
                "source_basis_hash",
            },
        )
        raw_status = mapping.get("execution_status")
        raw_attempt = mapping.get("attempt")
        return cls(
            worker_item_key=_key(mapping.get("worker_item_key"), "worker_item_key"),
            worker_item_id=_optional_descriptor_uuid(
                mapping.get("worker_item_id"), "worker_item_id"
            ),
            subtask_path=_source_text_array(
                mapping.get("subtask_path"), "subtask_path"
            ),
            effective_request_id=_optional_descriptor_uuid(
                mapping.get("effective_request_id"), "effective_request_id"
            ),
            allowed_lineage_ids=_source_uuid_array(
                mapping.get("allowed_lineage_ids"), "allowed_lineage_ids"
            ),
            effective_execution_id=_optional_descriptor_uuid(
                mapping.get("effective_execution_id"), "effective_execution_id"
            ),
            attempt=(
                None if raw_attempt is None else _positive_int(raw_attempt, "attempt")
            ),
            execution_status=(
                None
                if raw_status is None
                else _enum(raw_status, ExecutionStatus, "execution_status")
            ),
            inheritance_refs=_source_text_array(
                mapping.get("inheritance_refs"), "inheritance_refs"
            ),
            artifacts=_source_artifacts(mapping.get("artifacts")),
            evidence=_source_evidence(mapping.get("evidence")),
            retry_possible=_descriptor_bool(
                mapping.get("retry_possible"), "retry_possible"
            ),
            outcome_unknown=_descriptor_bool(
                mapping.get("outcome_unknown"), "outcome_unknown"
            ),
            reason_code=_optional_descriptor_text(
                mapping.get("reason_code"), "reason_code"
            ),
            source_basis_hash=_optional_descriptor_hash(
                mapping.get("source_basis_hash"), "source_basis_hash"
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "worker_item_key", _key(self.worker_item_key, "worker_item_key")
        )
        for name in (
            "worker_item_id",
            "effective_request_id",
            "effective_execution_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _descriptor_uuid(value, name)
        object.__setattr__(
            self,
            "subtask_path",
            _source_text_array(self.subtask_path, "subtask_path"),
        )
        object.__setattr__(
            self,
            "allowed_lineage_ids",
            _source_uuid_array(self.allowed_lineage_ids, "allowed_lineage_ids"),
        )
        if self.attempt is not None:
            _positive_int(self.attempt, "attempt")
        if self.execution_status is not None:
            object.__setattr__(
                self,
                "execution_status",
                _enum(self.execution_status, ExecutionStatus, "execution_status"),
            )
        object.__setattr__(
            self,
            "inheritance_refs",
            _source_text_array(self.inheritance_refs, "inheritance_refs"),
        )
        if any(not isinstance(item, ResultInputArtifact) for item in self.artifacts):
            raise ValidationError("result source artifacts must be typed")
        if any(not isinstance(item, ResultInputEvidence) for item in self.evidence):
            raise ValidationError("result source evidence must be typed")
        object.__setattr__(
            self,
            "artifacts",
            tuple(sorted(self.artifacts, key=lambda item: str(item.artifact_id))),
        )
        object.__setattr__(
            self,
            "evidence",
            tuple(sorted(self.evidence, key=lambda item: str(item.evidence_id))),
        )
        if (
            type(self.retry_possible) is not bool
            or type(self.outcome_unknown) is not bool
        ):
            raise ValidationError("result source possibility flags must be booleans")
        if self.reason_code is not None:
            object.__setattr__(
                self, "reason_code", _descriptor_text(self.reason_code, "reason_code")
            )
        if self.source_basis_hash is not None:
            object.__setattr__(
                self,
                "source_basis_hash",
                _descriptor_hash(self.source_basis_hash, "source_basis_hash"),
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "worker_item_key": self.worker_item_key,
            "worker_item_id": (
                str(self.worker_item_id) if self.worker_item_id is not None else None
            ),
            "subtask_path": list(self.subtask_path),
            "effective_request_id": (
                str(self.effective_request_id)
                if self.effective_request_id is not None
                else None
            ),
            "allowed_lineage_ids": [str(item) for item in self.allowed_lineage_ids],
            "effective_execution_id": (
                str(self.effective_execution_id)
                if self.effective_execution_id is not None
                else None
            ),
            "attempt": self.attempt,
            "execution_status": (
                self.execution_status.value
                if self.execution_status is not None
                else None
            ),
            "inheritance_refs": list(self.inheritance_refs),
            "artifacts": [item.to_mapping() for item in self.artifacts],
            "evidence": [item.to_mapping() for item in self.evidence],
            "retry_possible": self.retry_possible,
            "outcome_unknown": self.outcome_unknown,
            "reason_code": self.reason_code,
            "source_basis_hash": self.source_basis_hash,
        }


@dataclass(frozen=True, slots=True)
class InputCandidateState:
    """Readiness of one configured candidate on one authoritative source basis."""

    worker_item_key: str
    source: ResultInputSource | None
    ready: bool
    missing_artifact_refs: tuple[str, ...]
    missing_evidence_refs: tuple[str, ...]
    retry_possible: bool
    outcome_unknown: bool
    source_basis_hash: str | None
    reason_code: str | None

    @classmethod
    def from_mapping(cls, value: object) -> InputCandidateState:
        mapping = _descriptor_mapping(
            value,
            "input candidate state",
            {
                "worker_item_key",
                "source",
                "ready",
                "missing_artifact_refs",
                "missing_evidence_refs",
                "retry_possible",
                "outcome_unknown",
                "source_basis_hash",
                "reason_code",
            },
        )
        raw_source = mapping.get("source")
        return cls(
            worker_item_key=_key(mapping.get("worker_item_key"), "worker_item_key"),
            source=(
                None
                if raw_source is None
                else ResultInputSource.from_mapping(raw_source)
            ),
            ready=_descriptor_bool(mapping.get("ready"), "ready"),
            missing_artifact_refs=_source_text_array(
                mapping.get("missing_artifact_refs"), "missing_artifact_refs"
            ),
            missing_evidence_refs=_source_text_array(
                mapping.get("missing_evidence_refs"), "missing_evidence_refs"
            ),
            retry_possible=_descriptor_bool(
                mapping.get("retry_possible"), "retry_possible"
            ),
            outcome_unknown=_descriptor_bool(
                mapping.get("outcome_unknown"), "outcome_unknown"
            ),
            source_basis_hash=_optional_descriptor_hash(
                mapping.get("source_basis_hash"), "source_basis_hash"
            ),
            reason_code=_optional_descriptor_text(
                mapping.get("reason_code"), "reason_code"
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "worker_item_key", _key(self.worker_item_key, "worker_item_key")
        )
        if self.source is not None:
            if not isinstance(self.source, ResultInputSource):
                raise ValidationError("candidate state source must be typed")
            if self.source.worker_item_key != self.worker_item_key:
                raise ValidationError("candidate state source key does not match")
        for name in ("ready", "retry_possible", "outcome_unknown"):
            if type(getattr(self, name)) is not bool:
                raise ValidationError(f"candidate state {name} must be a boolean")
        object.__setattr__(
            self,
            "missing_artifact_refs",
            _source_text_array(self.missing_artifact_refs, "missing_artifact_refs"),
        )
        object.__setattr__(
            self,
            "missing_evidence_refs",
            _source_text_array(self.missing_evidence_refs, "missing_evidence_refs"),
        )
        if self.source_basis_hash is not None:
            object.__setattr__(
                self,
                "source_basis_hash",
                _descriptor_hash(self.source_basis_hash, "source_basis_hash"),
            )
        if self.reason_code is not None:
            object.__setattr__(
                self, "reason_code", _descriptor_text(self.reason_code, "reason_code")
            )
        if self.ready and (
            self.source is None
            or self.source_basis_hash is None
            or self.missing_artifact_refs
            or self.missing_evidence_refs
            or self.outcome_unknown
        ):
            raise ValidationError(
                "ready candidate state has an incomplete source basis"
            )
        if self.ready and self.reason_code is not None:
            raise ValidationError("ready candidate state cannot have a reason code")
        if self.source is not None and (
            self.source.source_basis_hash != self.source_basis_hash
        ):
            raise ValidationError("candidate state source basis hash does not match")

    def to_mapping(self) -> dict[str, object]:
        return {
            "worker_item_key": self.worker_item_key,
            "source": self.source.to_mapping() if self.source is not None else None,
            "ready": self.ready,
            "missing_artifact_refs": list(self.missing_artifact_refs),
            "missing_evidence_refs": list(self.missing_evidence_refs),
            "retry_possible": self.retry_possible,
            "outcome_unknown": self.outcome_unknown,
            "source_basis_hash": self.source_basis_hash,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class InputGroupEvaluation:
    """Deterministic result-readiness facts for one configured input group."""

    group_key: str
    required_count: int
    ready_count: int
    ready_sources: tuple[ResultInputSource, ...]
    candidate_states: tuple[InputCandidateState, ...]
    satisfied: bool
    reason_code: str | None

    @classmethod
    def from_mapping(cls, value: object) -> InputGroupEvaluation:
        mapping = _descriptor_mapping(
            value,
            "input group evaluation",
            {
                "group_key",
                "required_count",
                "ready_count",
                "ready_sources",
                "candidate_states",
                "satisfied",
                "reason_code",
            },
        )
        raw_ready = mapping.get("ready_sources")
        raw_states = mapping.get("candidate_states")
        if not isinstance(raw_ready, (list, tuple)):
            raise ValidationError("ready_sources must be an array")
        if not isinstance(raw_states, (list, tuple)):
            raise ValidationError("candidate_states must be an array")
        return cls(
            group_key=_key(mapping.get("group_key"), "group_key"),
            required_count=_positive_int(
                mapping.get("required_count"), "required_count"
            ),
            ready_count=_descriptor_non_negative_int(
                mapping.get("ready_count"), "ready_count"
            ),
            ready_sources=tuple(
                ResultInputSource.from_mapping(item) for item in raw_ready
            ),
            candidate_states=tuple(
                InputCandidateState.from_mapping(item) for item in raw_states
            ),
            satisfied=_descriptor_bool(mapping.get("satisfied"), "satisfied"),
            reason_code=_optional_descriptor_text(
                mapping.get("reason_code"), "reason_code"
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_key", _key(self.group_key, "group_key"))
        _positive_int(self.required_count, "required_count")
        _descriptor_non_negative_int(self.ready_count, "ready_count")
        if any(not isinstance(item, ResultInputSource) for item in self.ready_sources):
            raise ValidationError("ready_sources must contain typed result sources")
        if any(
            not isinstance(item, InputCandidateState) for item in self.candidate_states
        ):
            raise ValidationError("candidate_states must contain typed states")
        candidate_keys = tuple(item.worker_item_key for item in self.candidate_states)
        if not candidate_keys:
            raise ValidationError("input group evaluation requires candidate states")
        if len(candidate_keys) > MAX_INPUT_GROUP_CANDIDATES:
            raise ValidationError("input group evaluation contains too many candidates")
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValidationError("candidate state WorkerItems must be unique")
        if self.required_count > len(candidate_keys):
            raise ValidationError("input group required_count exceeds its candidates")
        ready_keys = tuple(item.worker_item_key for item in self.ready_sources)
        state_ready_keys = tuple(
            item.worker_item_key for item in self.candidate_states if item.ready
        )
        if (
            self.ready_count != len(self.ready_sources)
            or ready_keys != state_ready_keys
        ):
            raise ValidationError("input group ready source counts are inconsistent")
        if (
            tuple(item.source for item in self.candidate_states if item.ready)
            != self.ready_sources
        ):
            raise ValidationError("input group ready source facts are inconsistent")
        if type(self.satisfied) is not bool:
            raise ValidationError("input group satisfied must be a boolean")
        if self.satisfied != (self.ready_count >= self.required_count):
            raise ValidationError("input group satisfied does not match its counts")
        if self.reason_code is not None:
            object.__setattr__(
                self, "reason_code", _descriptor_text(self.reason_code, "reason_code")
            )
        if self.satisfied and self.reason_code is not None:
            raise ValidationError("satisfied input group cannot have a reason code")

    def to_mapping(self) -> dict[str, object]:
        return {
            "group_key": self.group_key,
            "required_count": self.required_count,
            "ready_count": self.ready_count,
            "ready_sources": [item.to_mapping() for item in self.ready_sources],
            "candidate_states": [item.to_mapping() for item in self.candidate_states],
            "satisfied": self.satisfied,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class InputGroupCandidateV1:
    worker_item_key: str
    priority: int

    @classmethod
    def from_mapping(cls, value: object) -> InputGroupCandidateV1:
        mapping = _strict_mapping(
            value,
            "input candidate",
            {"worker_item_key", "priority"},
        )
        return cls(
            worker_item_key=_key(mapping.get("worker_item_key"), "worker_item_key"),
            priority=_positive_int(mapping.get("priority"), "priority"),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "worker_item_key",
            _key(self.worker_item_key, "worker_item_key"),
        )
        object.__setattr__(self, "priority", _positive_int(self.priority, "priority"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "worker_item_key": self.worker_item_key,
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class InputGroupRequirementsV1:
    execution_status: ExecutionStatus
    required_artifact_refs: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]
    evidence_status: EvidenceClaimStatus

    @classmethod
    def from_mapping(cls, value: object) -> InputGroupRequirementsV1:
        mapping = _strict_mapping(
            value,
            "input requirements",
            {
                "execution_status",
                "required_artifact_refs",
                "required_evidence_refs",
                "evidence_status",
            },
        )
        execution_status = _enum(
            mapping.get("execution_status"),
            ExecutionStatus,
            "execution_status",
        )
        if execution_status is not ExecutionStatus.SUCCEEDED:
            raise InputGroupInvalid("input group execution_status must be succeeded")
        evidence_status = _enum(
            mapping.get("evidence_status"),
            EvidenceClaimStatus,
            "evidence_status",
        )
        return cls(
            execution_status=execution_status,
            required_artifact_refs=_artifact_refs(
                mapping.get("required_artifact_refs"),
                "required_artifact_refs",
            ),
            required_evidence_refs=_evidence_refs(
                mapping.get("required_evidence_refs"),
                "required_evidence_refs",
            ),
            evidence_status=evidence_status,
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_status",
            _enum(self.execution_status, ExecutionStatus, "execution_status"),
        )
        if self.execution_status is not ExecutionStatus.SUCCEEDED:
            raise InputGroupInvalid("input group execution_status must be succeeded")
        object.__setattr__(
            self,
            "required_artifact_refs",
            _artifact_refs(self.required_artifact_refs, "required_artifact_refs"),
        )
        object.__setattr__(
            self,
            "required_evidence_refs",
            _evidence_refs(self.required_evidence_refs, "required_evidence_refs"),
        )
        if not self.required_artifact_refs and not self.required_evidence_refs:
            raise InputGroupInvalid(
                "input requirements need an artifact or evidence selector"
            )
        object.__setattr__(
            self,
            "evidence_status",
            _enum(self.evidence_status, EvidenceClaimStatus, "evidence_status"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "execution_status": self.execution_status.value,
            "required_artifact_refs": list(self.required_artifact_refs),
            "required_evidence_refs": list(self.required_evidence_refs),
            "evidence_status": self.evidence_status.value,
        }


@dataclass(frozen=True, slots=True)
class InputGroupV1:
    group_key: str
    title: str
    consumer_worker_item_key: str
    candidates: tuple[InputGroupCandidateV1, ...]
    requirements: InputGroupRequirementsV1
    mode: InputGroupMode
    minimum_count: int | None
    selection_strategy: InputSelectionStrategy

    @classmethod
    def from_mapping(cls, value: object) -> InputGroupV1:
        mapping = _strict_mapping(
            value,
            "input group",
            {
                "group_key",
                "title",
                "consumer_worker_item_key",
                "candidates",
                "requirements",
                "mode",
                "minimum_count",
                "selection_strategy",
            },
        )
        raw_candidates = mapping.get("candidates")
        if not isinstance(raw_candidates, (list, tuple)):
            raise InputGroupInvalid("input candidates must be an array")
        candidates = tuple(
            InputGroupCandidateV1.from_mapping(item) for item in raw_candidates
        )
        return cls(
            group_key=_key(mapping.get("group_key"), "group_key"),
            title=_required_text(mapping.get("title"), "title"),
            consumer_worker_item_key=_key(
                mapping.get("consumer_worker_item_key"),
                "consumer_worker_item_key",
            ),
            candidates=candidates,
            requirements=InputGroupRequirementsV1.from_mapping(
                mapping.get("requirements")
            ),
            mode=_enum(mapping.get("mode"), InputGroupMode, "mode"),
            minimum_count=_optional_int(mapping.get("minimum_count"), "minimum_count"),
            selection_strategy=_enum(
                mapping.get("selection_strategy"),
                InputSelectionStrategy,
                "selection_strategy",
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_key", _key(self.group_key, "group_key"))
        object.__setattr__(self, "title", _required_text(self.title, "title"))
        object.__setattr__(
            self,
            "consumer_worker_item_key",
            _key(self.consumer_worker_item_key, "consumer_worker_item_key"),
        )
        if not self.candidates:
            raise InputGroupInvalid("input candidates must not be empty")
        if len(self.candidates) > MAX_INPUT_GROUP_CANDIDATES:
            raise InputGroupInvalid(
                f"input candidates cannot exceed {MAX_INPUT_GROUP_CANDIDATES}"
            )
        if any(not isinstance(item, InputGroupCandidateV1) for item in self.candidates):
            raise InputGroupInvalid("input candidates must be typed")
        normalized_candidates = tuple(
            sorted(self.candidates, key=lambda item: item.priority)
        )
        object.__setattr__(self, "candidates", normalized_candidates)
        candidate_keys = tuple(item.worker_item_key for item in normalized_candidates)
        if len(candidate_keys) != len(set(candidate_keys)):
            raise InputGroupInvalid("input candidate worker_item_key must be unique")
        if self.consumer_worker_item_key in candidate_keys:
            raise InputGroupInvalid("input group cannot reference its consumer")
        priorities = tuple(item.priority for item in normalized_candidates)
        if priorities != tuple(range(1, len(normalized_candidates) + 1)):
            raise InputGroupInvalid(
                "input candidate priorities must be contiguous from 1"
            )
        if not isinstance(self.requirements, InputGroupRequirementsV1):
            raise InputGroupInvalid("input requirements must be typed")
        object.__setattr__(
            self,
            "mode",
            _enum(self.mode, InputGroupMode, "mode"),
        )
        object.__setattr__(
            self,
            "minimum_count",
            _optional_int(self.minimum_count, "minimum_count"),
        )
        if self.mode is InputGroupMode.ALL:
            if self.minimum_count is not None:
                raise InputGroupInvalid("all input groups require null minimum_count")
        elif self.minimum_count is None or self.minimum_count > len(self.candidates):
            raise InputGroupInvalid(
                "at_least input groups require minimum_count between 1 and candidate count"
            )
        object.__setattr__(
            self,
            "selection_strategy",
            _enum(
                self.selection_strategy,
                InputSelectionStrategy,
                "selection_strategy",
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "group_key": self.group_key,
            "title": self.title,
            "consumer_worker_item_key": self.consumer_worker_item_key,
            "candidates": [item.to_mapping() for item in self.candidates],
            "requirements": self.requirements.to_mapping(),
            "mode": self.mode.value,
            "minimum_count": self.minimum_count,
            "selection_strategy": self.selection_strategy.value,
        }


def validate_input_wait_graph(
    *,
    worker_keys: frozenset[str],
    hard_edges: tuple[tuple[str, str], ...],
    input_groups: tuple[InputGroupV1, ...],
) -> None:
    """Validate hard and potential result-input edges as one wait graph."""

    known_workers = set(worker_keys)
    edges: set[tuple[str, str]] = set()
    group_keys: set[str] = set()

    for upstream, downstream in hard_edges:
        if upstream not in known_workers or downstream not in known_workers:
            raise InputGroupInvalid("hard edge references an unknown WorkerItem")
        if upstream == downstream:
            raise InputWaitCycle("hard edge cannot point to itself", nodes=(upstream,))
        edges.add((upstream, downstream))

    for group in input_groups:
        if not isinstance(group, InputGroupV1):
            raise InputGroupInvalid("input groups must be typed")
        if group.group_key in group_keys:
            raise InputGroupInvalid(f"duplicate input group key: {group.group_key}")
        group_keys.add(group.group_key)
        if group.consumer_worker_item_key not in known_workers:
            raise InputGroupInvalid(
                "input group consumer references an unknown WorkerItem"
            )
        for candidate in group.candidates:
            if candidate.worker_item_key not in known_workers:
                raise InputGroupInvalid(
                    "input group candidate references an unknown WorkerItem"
                )
            edges.add((candidate.worker_item_key, group.consumer_worker_item_key))

    _assert_acyclic(known_workers, edges)


def _assert_acyclic(worker_keys: set[str], edges: set[tuple[str, str]]) -> None:
    indegree = {key: 0 for key in worker_keys}
    adjacency: dict[str, set[str]] = {key: set() for key in worker_keys}
    for upstream, downstream in edges:
        adjacency[upstream].add(downstream)
        indegree[downstream] += 1

    queue = sorted(key for key, count in indegree.items() if count == 0)
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for downstream in sorted(adjacency[current]):
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                queue.append(downstream)
        queue.sort()
    if visited != len(worker_keys):
        raise InputWaitCycle(
            "hard and result-input edges contain a cycle",
            nodes=tuple(sorted(key for key, count in indegree.items() if count > 0)),
        )


def _strict_mapping(
    value: object, name: str, allowed: set[str]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InputGroupInvalid(f"{name} must be a mapping")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise InputGroupInvalid(f"unknown {name} fields: {', '.join(unknown)}")
    return cast(Mapping[str, object], value)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputGroupInvalid(f"{name} is required")
    normalized = value.strip()
    if len(normalized) > _MAX_TEXT_LENGTH:
        raise InputGroupInvalid(f"{name} is too long")
    return normalized


def _key(value: object, name: str) -> str:
    normalized = _required_text(value, name)
    if any(character.isspace() for character in normalized):
        raise InputGroupInvalid(f"{name} cannot contain whitespace")
    return normalized


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise InputGroupInvalid(f"{name} must be a positive integer")
    return value


def _optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, name)


def _enum[T: StrEnum](value: object, enum_type: type[T], name: str) -> T:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise InputGroupInvalid(f"unsupported {name}: {value!r}") from exc


def _string_array(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InputGroupInvalid(f"{name} must be an array")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InputGroupInvalid(f"{name} must contain non-empty strings")
        normalized = item.strip()
        if normalized in values:
            raise InputGroupInvalid(f"{name} must be unique")
        values.append(normalized)
    return tuple(values)


def _artifact_refs(value: object, name: str) -> tuple[str, ...]:
    refs = _string_array(value, name)
    if len(refs) > MAX_INPUT_GROUP_ARTIFACT_SELECTORS:
        raise InputGroupInvalid(
            f"{name} cannot exceed {MAX_INPUT_GROUP_ARTIFACT_SELECTORS} selectors"
        )
    for ref in refs:
        match = re.fullmatch(r"artifact_type:([a-z_]+)", ref)
        if match is None:
            raise InputGroupInvalid(f"{name} must use artifact_type:<type>")
        try:
            ArtifactType(match.group(1))
        except ValueError as exc:
            raise InputGroupInvalid(
                f"unsupported artifact type in {name}: {ref}"
            ) from exc
    return refs


def _evidence_refs(value: object, name: str) -> tuple[str, ...]:
    refs = _string_array(value, name)
    if len(refs) > MAX_INPUT_GROUP_EVIDENCE_SELECTORS:
        raise InputGroupInvalid(
            f"{name} cannot exceed {MAX_INPUT_GROUP_EVIDENCE_SELECTORS} selectors"
        )
    for ref in refs:
        if re.fullmatch(r"acceptance:\S+", ref) is None:
            raise InputGroupInvalid(f"{name} must use acceptance:<criterion-ref>")
    return refs


def _descriptor_mapping(
    value: object, name: str, allowed: set[str]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValidationError(f"unknown {name} fields: {', '.join(unknown)}")
    missing = sorted(key for key in allowed if key not in value)
    if missing:
        raise ValidationError(f"missing {name} fields: {', '.join(missing)}")
    return cast(Mapping[str, object], value)


def _descriptor_uuid(value: object, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a UUID") from exc


def _optional_descriptor_uuid(value: object, name: str) -> UUID | None:
    return None if value is None else _descriptor_uuid(value, name)


def _descriptor_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a lowercase SHA-256 hash")
    return value


def _optional_descriptor_hash(value: object, name: str) -> str | None:
    return None if value is None else _descriptor_hash(value, name)


def _descriptor_non_negative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer")
    return value


def _descriptor_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{name} must be a boolean")
    return value


def _descriptor_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    normalized = value.strip()
    if len(normalized) > _MAX_TEXT_LENGTH:
        raise ValidationError(f"{name} is too long")
    return normalized


def _optional_descriptor_text(value: object, name: str) -> str | None:
    return None if value is None else _descriptor_text(value, name)


def _descriptor_artifact_type(value: object) -> ArtifactType:
    if isinstance(value, ArtifactType):
        return value
    try:
        return ArtifactType(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported artifact_type: {value!r}") from exc


def _descriptor_uuid_array(value: object, name: str) -> tuple[UUID, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be an array")
    items = tuple(sorted((_descriptor_uuid(item, name) for item in value), key=str))
    if not items:
        raise ValidationError(f"{name} must not be empty")
    if len(items) != len(set(items)):
        raise ValidationError(f"{name} must be unique")
    return items


def _source_text_array(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be an array")
    items = tuple(_descriptor_text(item, name) for item in value)
    if len(items) != len(set(items)):
        raise ValidationError(f"{name} must be unique")
    return items


def _source_uuid_array(value: object, name: str) -> tuple[UUID, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be an array")
    items = tuple(_descriptor_uuid(item, name) for item in value)
    if len(items) != len(set(items)):
        raise ValidationError(f"{name} must be unique")
    return items


def _source_artifacts(value: object) -> tuple[ResultInputArtifact, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("result source artifacts must be an array")
    items = tuple(ResultInputArtifact.from_mapping(item) for item in value)
    if len({item.artifact_id for item in items}) != len(items):
        raise ValidationError("result source artifact IDs must be unique")
    return items


def _source_evidence(value: object) -> tuple[ResultInputEvidence, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("result source evidence must be an array")
    items = tuple(ResultInputEvidence.from_mapping(item) for item in value)
    if len({item.evidence_id for item in items}) != len(items):
        raise ValidationError("result source Evidence IDs must be unique")
    return items
