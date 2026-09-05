from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from sidera_control_plane.domain.artifacts import ArtifactType, EvidenceClaimStatus
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import ExecutionStatus

MAX_INPUT_GROUP_CANDIDATES = 32
MAX_INPUT_GROUP_ARTIFACT_SELECTORS = 8
MAX_INPUT_GROUP_EVIDENCE_SELECTORS = 8
_MAX_TEXT_LENGTH = 16_384


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
            raise InputGroupInvalid(
                "input group execution_status must be succeeded"
            )
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
            raise InputGroupInvalid(
                "input group execution_status must be succeeded"
            )
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
            raise InputGroupInvalid(
                "hard edge references an unknown WorkerItem"
            )
        if upstream == downstream:
            raise InputWaitCycle(
                "hard edge cannot point to itself", nodes=(upstream,)
            )
        edges.add((upstream, downstream))

    for group in input_groups:
        if not isinstance(group, InputGroupV1):
            raise InputGroupInvalid("input groups must be typed")
        if group.group_key in group_keys:
            raise InputGroupInvalid(
                f"duplicate input group key: {group.group_key}"
            )
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


def _assert_acyclic(
    worker_keys: set[str], edges: set[tuple[str, str]]
) -> None:
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
    if not refs:
        raise InputGroupInvalid(f"{name} must contain at least one selector")
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
            raise InputGroupInvalid(f"unsupported artifact type in {name}: {ref}") from exc
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
