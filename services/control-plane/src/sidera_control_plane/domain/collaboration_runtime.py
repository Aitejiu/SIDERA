from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, cast
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.input_selections import InputSelectionGroupV1
from sidera_control_plane.domain.result_inputs import (
    InputGroupEvaluation,
    InputGroupMode,
    InputGroupV1,
)

_MAX_TEXT_LENGTH = 16_384
_MAX_REASON_LENGTH = 256
_MAX_IDEMPOTENCY_KEY_LENGTH = 512
_MAX_JSON_ENTRIES = 256
_MAX_JSON_DEPTH = 8
_MAX_INPUT_GROUPS_PER_CONSUMER = 8
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_PROJECTION_FIELDS = frozenset(
    {
        "api_key",
        "config",
        "file_content",
        "knowledge_body",
        "password",
        "provider_api_key",
        "secret",
        "secret_value",
        "stderr",
        "stdout",
        "trace",
        "user_message",
    }
)


class RuntimeEnvelopeError(ValidationError):
    code = "runtime_envelope_invalid"


class RuntimeDecisionOutcome(StrEnum):
    INTENTS = "intents"
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class RuntimeProjectionV1:
    schema_version: int
    projection_id: UUID
    workspace_id: UUID
    root_task_id: UUID
    run_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    run_resource_version: int
    fact_cursor: int
    policy: Mapping[str, object]
    tasks: tuple[Mapping[str, object], ...]
    subtasks: tuple[Mapping[str, object], ...]
    worker_items: tuple[Mapping[str, object], ...]
    dependencies: tuple[Mapping[str, object], ...]
    execution_requests: tuple[Mapping[str, object], ...]
    executions: tuple[Mapping[str, object], ...]
    artifacts: tuple[Mapping[str, object], ...]
    evidence: tuple[Mapping[str, object], ...]
    releases: tuple[Mapping[str, object], ...]
    previous_strategy_state: Mapping[str, object]
    fingerprints: Mapping[str, object]
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "projection_id",
            "workspace_id",
            "root_task_id",
            "run_id",
            "plan_snapshot_id",
        ):
            _uuid(getattr(self, name), f"projection {name}")
        _positive_int(self.schema_version, "projection schema_version")
        if self.schema_version != 1:
            raise RuntimeEnvelopeError("unsupported projection schema_version")
        _positive_int(self.plan_generation, "projection plan_generation")
        _hash(self.plan_content_hash, "projection plan_content_hash")
        _positive_int(self.run_resource_version, "projection run_resource_version")
        _non_negative_int(self.fact_cursor, "projection fact_cursor")
        object.__setattr__(
            self, "policy", _freeze_projection_mapping(self.policy, "policy")
        )
        for name in (
            "tasks",
            "subtasks",
            "worker_items",
            "dependencies",
            "execution_requests",
            "executions",
            "artifacts",
            "evidence",
            "releases",
        ):
            object.__setattr__(
                self,
                name,
                _freeze_projection_sequence(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            "previous_strategy_state",
            _freeze_projection_mapping(
                self.previous_strategy_state, "previous_strategy_state"
            ),
        )
        object.__setattr__(
            self,
            "fingerprints",
            _freeze_projection_mapping(self.fingerprints, "fingerprints"),
        )
        _aware(self.created_at, "projection created_at")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "projection_id": str(self.projection_id),
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "run_id": str(self.run_id),
            "plan_snapshot_id": str(self.plan_snapshot_id),
            "plan_generation": self.plan_generation,
            "plan_content_hash": self.plan_content_hash,
            "run_resource_version": self.run_resource_version,
            "fact_cursor": self.fact_cursor,
            "policy": _json_value(self.policy),
            "tasks": _sorted_json_mappings(self.tasks),
            "subtasks": _sorted_json_mappings(self.subtasks),
            "worker_items": _sorted_json_mappings(self.worker_items),
            "dependencies": _sorted_json_mappings(self.dependencies),
            "execution_requests": _sorted_json_mappings(self.execution_requests),
            "executions": _sorted_json_mappings(self.executions),
            "artifacts": _sorted_json_mappings(self.artifacts),
            "evidence": _sorted_json_mappings(self.evidence),
            "releases": _sorted_json_mappings(self.releases),
            "previous_strategy_state": _json_value(self.previous_strategy_state),
            "fingerprints": _json_value(self.fingerprints),
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], *, created_at: datetime
    ) -> RuntimeProjectionV1:
        """Rehydrate a stored canonical projection without changing its identity."""
        mapping = _strict_object(
            value,
            "projection",
            {
                "schema_version",
                "projection_id",
                "workspace_id",
                "root_task_id",
                "run_id",
                "plan_snapshot_id",
                "plan_generation",
                "plan_content_hash",
                "run_resource_version",
                "fact_cursor",
                "policy",
                "tasks",
                "subtasks",
                "worker_items",
                "dependencies",
                "execution_requests",
                "executions",
                "artifacts",
                "evidence",
                "releases",
                "previous_strategy_state",
                "fingerprints",
            },
        )
        return cls(
            schema_version=_version(
                mapping.get("schema_version"), "projection schema_version"
            ),
            projection_id=_wire_uuid(mapping.get("projection_id"), "projection_id"),
            workspace_id=_wire_uuid(mapping.get("workspace_id"), "workspace_id"),
            root_task_id=_wire_uuid(mapping.get("root_task_id"), "root_task_id"),
            run_id=_wire_uuid(mapping.get("run_id"), "run_id"),
            plan_snapshot_id=_wire_uuid(
                mapping.get("plan_snapshot_id"), "plan_snapshot_id"
            ),
            plan_generation=_version(
                mapping.get("plan_generation"), "projection plan_generation"
            ),
            plan_content_hash=_wire_hash(
                mapping.get("plan_content_hash"), "plan_content_hash"
            ),
            run_resource_version=_version(
                mapping.get("run_resource_version"), "projection run_resource_version"
            ),
            fact_cursor=_non_negative_int(
                mapping.get("fact_cursor"), "projection fact_cursor"
            ),
            policy=_wire_mapping(mapping.get("policy"), "projection policy"),
            tasks=_wire_mapping_sequence(mapping.get("tasks"), "projection tasks"),
            subtasks=_wire_mapping_sequence(
                mapping.get("subtasks"), "projection subtasks"
            ),
            worker_items=_wire_mapping_sequence(
                mapping.get("worker_items"), "projection worker_items"
            ),
            dependencies=_wire_mapping_sequence(
                mapping.get("dependencies"), "projection dependencies"
            ),
            execution_requests=_wire_mapping_sequence(
                mapping.get("execution_requests"), "projection execution_requests"
            ),
            executions=_wire_mapping_sequence(
                mapping.get("executions"), "projection executions"
            ),
            artifacts=_wire_mapping_sequence(
                mapping.get("artifacts"), "projection artifacts"
            ),
            evidence=_wire_mapping_sequence(
                mapping.get("evidence"), "projection evidence"
            ),
            releases=_wire_mapping_sequence(
                mapping.get("releases"), "projection releases"
            ),
            previous_strategy_state=_wire_mapping(
                mapping.get("previous_strategy_state"),
                "projection previous_strategy_state",
            ),
            fingerprints=_wire_mapping(
                mapping.get("fingerprints"), "projection fingerprints"
            ),
            created_at=created_at,
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    @property
    def content_hash(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()

    def with_fingerprint(
        self, fingerprints: Mapping[str, object]
    ) -> RuntimeProjectionV1:
        return replace(self, fingerprints=fingerprints)


@dataclass(frozen=True, slots=True)
class RuntimeResultInputEvaluation:
    consumer_execution_request_id: UUID
    group_key: str
    evaluation: InputGroupEvaluation

    @classmethod
    def from_mapping(cls, value: object) -> RuntimeResultInputEvaluation:
        mapping = _strict_object(
            value,
            "result input evaluation",
            {"consumer_execution_request_id", "group_key", "evaluation"},
        )
        try:
            evaluation = InputGroupEvaluation.from_mapping(mapping.get("evaluation"))
        except ValidationError as exc:
            raise RuntimeEnvelopeError(
                "result input evaluation payload is invalid"
            ) from exc
        return cls(
            consumer_execution_request_id=_wire_uuid(
                mapping.get("consumer_execution_request_id"),
                "consumer_execution_request_id",
            ),
            group_key=_wire_text(
                mapping.get("group_key"), "group_key", _MAX_TEXT_LENGTH
            ),
            evaluation=evaluation,
        )

    def __post_init__(self) -> None:
        _uuid(
            self.consumer_execution_request_id,
            "result input evaluation consumer_execution_request_id",
        )
        _bounded_text(
            self.group_key, "result input evaluation group_key", _MAX_TEXT_LENGTH
        )
        if not isinstance(self.evaluation, InputGroupEvaluation):
            raise RuntimeEnvelopeError("result input evaluation must be typed")
        if self.group_key != self.evaluation.group_key:
            raise RuntimeEnvelopeError(
                "result input evaluation group key does not match"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "consumer_execution_request_id": str(self.consumer_execution_request_id),
            "group_key": self.group_key,
            "evaluation": self.evaluation.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class RuntimeProjectionV2:
    schema_version: int
    projection_id: UUID
    workspace_id: UUID
    root_task_id: UUID
    run_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    run_resource_version: int
    fact_cursor: int
    policy: Mapping[str, object]
    tasks: tuple[Mapping[str, object], ...]
    subtasks: tuple[Mapping[str, object], ...]
    worker_items: tuple[Mapping[str, object], ...]
    dependencies: tuple[Mapping[str, object], ...]
    execution_requests: tuple[Mapping[str, object], ...]
    executions: tuple[Mapping[str, object], ...]
    artifacts: tuple[Mapping[str, object], ...]
    evidence: tuple[Mapping[str, object], ...]
    releases: tuple[Mapping[str, object], ...]
    previous_strategy_state: Mapping[str, object]
    result_input_evaluations: tuple[RuntimeResultInputEvaluation, ...]
    fingerprints: Mapping[str, object]
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "projection_id",
            "workspace_id",
            "root_task_id",
            "run_id",
            "plan_snapshot_id",
        ):
            _uuid(getattr(self, name), f"projection {name}")
        _positive_int(self.schema_version, "projection schema_version")
        if self.schema_version != 2:
            raise RuntimeEnvelopeError("unsupported projection schema_version")
        _positive_int(self.plan_generation, "projection plan_generation")
        _hash(self.plan_content_hash, "projection plan_content_hash")
        _positive_int(self.run_resource_version, "projection run_resource_version")
        _non_negative_int(self.fact_cursor, "projection fact_cursor")
        object.__setattr__(
            self, "policy", _freeze_projection_mapping(self.policy, "policy")
        )
        for name in (
            "tasks",
            "subtasks",
            "worker_items",
            "dependencies",
            "execution_requests",
            "executions",
            "artifacts",
            "evidence",
            "releases",
        ):
            frozen_values = _freeze_projection_sequence(getattr(self, name), name)
            object.__setattr__(
                self,
                name,
                tuple(sorted(frozen_values, key=_mapping_sort_key)),
            )
        object.__setattr__(
            self,
            "previous_strategy_state",
            _freeze_projection_mapping(
                self.previous_strategy_state, "previous_strategy_state"
            ),
        )
        evaluations = tuple(self.result_input_evaluations)
        if any(
            not isinstance(item, RuntimeResultInputEvaluation) for item in evaluations
        ):
            raise RuntimeEnvelopeError(
                "projection contains an invalid result input evaluation"
            )
        if len(evaluations) > _MAX_JSON_ENTRIES:
            raise RuntimeEnvelopeError(
                "projection contains too many result input evaluations"
            )
        object.__setattr__(
            self,
            "result_input_evaluations",
            tuple(
                sorted(
                    evaluations,
                    key=lambda item: (
                        item.group_key,
                        str(item.consumer_execution_request_id),
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "fingerprints",
            _freeze_projection_mapping(self.fingerprints, "fingerprints"),
        )
        _aware(self.created_at, "projection created_at")
        self._validate_result_input_contract()

    def _validate_result_input_contract(self) -> None:
        if self.policy.get("schema_version") != 2:
            raise RuntimeEnvelopeError("Projection v2 requires policy schema_version 2")
        raw_groups = self.policy.get("input_groups")
        if not isinstance(raw_groups, (list, tuple)):
            raise RuntimeEnvelopeError(
                "Projection v2 policy input_groups must be an array"
            )
        try:
            groups = tuple(InputGroupV1.from_mapping(item) for item in raw_groups)
        except ValidationError as exc:
            raise RuntimeEnvelopeError(
                "Projection v2 input_groups are invalid"
            ) from exc
        groups_by_key = {item.group_key: item for item in groups}
        if len(groups_by_key) != len(groups):
            raise RuntimeEnvelopeError("Projection v2 input group keys must be unique")
        groups_per_consumer: dict[str, int] = {}
        for group in groups:
            consumer_key = group.consumer_worker_item_key
            groups_per_consumer[consumer_key] = (
                groups_per_consumer.get(consumer_key, 0) + 1
            )
        if any(
            count > _MAX_INPUT_GROUPS_PER_CONSUMER
            for count in groups_per_consumer.values()
        ):
            raise RuntimeEnvelopeError(
                "Projection v2 consumer has too many input groups"
            )
        evaluations = self.result_input_evaluations
        evaluation_keys = tuple(item.group_key for item in evaluations)
        if len(evaluation_keys) != len(set(evaluation_keys)):
            raise RuntimeEnvelopeError(
                "Projection v2 result input evaluations must be unique"
            )
        if set(evaluation_keys) != set(groups_by_key):
            raise RuntimeEnvelopeError(
                "Projection v2 must evaluate every configured input group"
            )
        requests_by_id: dict[str, Mapping[str, object]] = {}
        for request in self.execution_requests:
            request_id = request.get("id")
            if isinstance(request_id, str):
                if request_id in requests_by_id:
                    raise RuntimeEnvelopeError(
                        "projection contains duplicate execution requests"
                    )
                requests_by_id[request_id] = request
        for item in evaluations:
            group = groups_by_key[item.group_key]
            request = requests_by_id.get(str(item.consumer_execution_request_id))
            if request is None:
                raise RuntimeEnvelopeError(
                    "result input evaluation consumer execution request is missing"
                )
            if request.get("worker_item_key") != group.consumer_worker_item_key:
                raise RuntimeEnvelopeError(
                    "result input evaluation consumer execution request does not match"
                )
            candidate_keys = tuple(
                candidate.worker_item_key for candidate in group.candidates
            )
            state_keys = tuple(
                state.worker_item_key for state in item.evaluation.candidate_states
            )
            if state_keys != candidate_keys:
                raise RuntimeEnvelopeError(
                    "result input evaluation candidates do not match policy"
                )
            required_count = (
                len(group.candidates)
                if group.mode is InputGroupMode.ALL
                else group.minimum_count
            )
            if item.evaluation.required_count != required_count:
                raise RuntimeEnvelopeError(
                    "result input evaluation threshold does not match policy"
                )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "projection_id": str(self.projection_id),
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "run_id": str(self.run_id),
            "plan_snapshot_id": str(self.plan_snapshot_id),
            "plan_generation": self.plan_generation,
            "plan_content_hash": self.plan_content_hash,
            "run_resource_version": self.run_resource_version,
            "fact_cursor": self.fact_cursor,
            "policy": _json_value(self.policy),
            "tasks": _sorted_json_mappings(self.tasks),
            "subtasks": _sorted_json_mappings(self.subtasks),
            "worker_items": _sorted_json_mappings(self.worker_items),
            "dependencies": _sorted_json_mappings(self.dependencies),
            "execution_requests": _sorted_json_mappings(self.execution_requests),
            "executions": _sorted_json_mappings(self.executions),
            "artifacts": _sorted_json_mappings(self.artifacts),
            "evidence": _sorted_json_mappings(self.evidence),
            "releases": _sorted_json_mappings(self.releases),
            "previous_strategy_state": _json_value(self.previous_strategy_state),
            "result_input_evaluations": [
                item.to_mapping() for item in self.result_input_evaluations
            ],
            "fingerprints": _json_value(self.fingerprints),
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], *, created_at: datetime
    ) -> RuntimeProjectionV2:
        mapping = _strict_object(
            value,
            "projection",
            {
                "schema_version",
                "projection_id",
                "workspace_id",
                "root_task_id",
                "run_id",
                "plan_snapshot_id",
                "plan_generation",
                "plan_content_hash",
                "run_resource_version",
                "fact_cursor",
                "policy",
                "tasks",
                "subtasks",
                "worker_items",
                "dependencies",
                "execution_requests",
                "executions",
                "artifacts",
                "evidence",
                "releases",
                "previous_strategy_state",
                "result_input_evaluations",
                "fingerprints",
            },
        )
        schema_version = _version(
            mapping.get("schema_version"), "projection schema_version"
        )
        if schema_version != 2:
            raise RuntimeEnvelopeError("unsupported projection schema_version")
        raw_evaluations = mapping.get("result_input_evaluations")
        if not isinstance(raw_evaluations, (list, tuple)):
            raise RuntimeEnvelopeError(
                "projection result_input_evaluations must be an array"
            )
        return cls(
            schema_version=schema_version,
            projection_id=_wire_uuid(mapping.get("projection_id"), "projection_id"),
            workspace_id=_wire_uuid(mapping.get("workspace_id"), "workspace_id"),
            root_task_id=_wire_uuid(mapping.get("root_task_id"), "root_task_id"),
            run_id=_wire_uuid(mapping.get("run_id"), "run_id"),
            plan_snapshot_id=_wire_uuid(
                mapping.get("plan_snapshot_id"), "plan_snapshot_id"
            ),
            plan_generation=_version(
                mapping.get("plan_generation"), "projection plan_generation"
            ),
            plan_content_hash=_wire_hash(
                mapping.get("plan_content_hash"), "plan_content_hash"
            ),
            run_resource_version=_version(
                mapping.get("run_resource_version"), "projection run_resource_version"
            ),
            fact_cursor=_non_negative_int(
                mapping.get("fact_cursor"), "projection fact_cursor"
            ),
            policy=_wire_mapping(mapping.get("policy"), "projection policy"),
            tasks=_wire_mapping_sequence(mapping.get("tasks"), "projection tasks"),
            subtasks=_wire_mapping_sequence(
                mapping.get("subtasks"), "projection subtasks"
            ),
            worker_items=_wire_mapping_sequence(
                mapping.get("worker_items"), "projection worker_items"
            ),
            dependencies=_wire_mapping_sequence(
                mapping.get("dependencies"), "projection dependencies"
            ),
            execution_requests=_wire_mapping_sequence(
                mapping.get("execution_requests"), "projection execution_requests"
            ),
            executions=_wire_mapping_sequence(
                mapping.get("executions"), "projection executions"
            ),
            artifacts=_wire_mapping_sequence(
                mapping.get("artifacts"), "projection artifacts"
            ),
            evidence=_wire_mapping_sequence(
                mapping.get("evidence"), "projection evidence"
            ),
            releases=_wire_mapping_sequence(
                mapping.get("releases"), "projection releases"
            ),
            previous_strategy_state=_wire_mapping(
                mapping.get("previous_strategy_state"),
                "projection previous_strategy_state",
            ),
            result_input_evaluations=tuple(
                RuntimeResultInputEvaluation.from_mapping(item)
                for item in raw_evaluations
            ),
            fingerprints=_wire_mapping(
                mapping.get("fingerprints"), "projection fingerprints"
            ),
            created_at=created_at,
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    @property
    def content_hash(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()

    def with_fingerprint(
        self, fingerprints: Mapping[str, object]
    ) -> RuntimeProjectionV2:
        return replace(self, fingerprints=fingerprints)


@dataclass(frozen=True, slots=True)
class ReleaseExecutionRequestIntentV1:
    intent_type: ClassVar[str] = "release_execution_request"
    ordinal: int
    target_execution_request_id: UUID
    target_request_version: int
    reason_code: str
    summary: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _non_negative_int(self.ordinal, "intent ordinal")
        _uuid(self.target_execution_request_id, "intent target_execution_request_id")
        _positive_int(self.target_request_version, "intent target_request_version")
        _bounded_text(self.reason_code, "intent reason_code", _MAX_REASON_LENGTH)
        _bounded_text(self.summary, "intent summary", _MAX_TEXT_LENGTH)
        _bounded_text(
            self.idempotency_key,
            "intent idempotency_key",
            _MAX_IDEMPOTENCY_KEY_LENGTH,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "intent_type": self.intent_type,
            "ordinal": self.ordinal,
            "target_execution_request_id": str(self.target_execution_request_id),
            "target_request_version": self.target_request_version,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class RuntimeDecisionV1:
    schema_version: int
    operation_id: UUID
    run_id: UUID
    projection_id: UUID
    projection_hash: str
    outcome: RuntimeDecisionOutcome
    reason_code: str
    summary: str
    strategy_state: Mapping[str, object]
    intents: tuple[ReleaseExecutionRequestIntentV1, ...]

    def __post_init__(self) -> None:
        _positive_int(self.schema_version, "decision schema_version")
        if self.schema_version != 1:
            raise RuntimeEnvelopeError("unsupported decision schema_version")
        for name in ("operation_id", "run_id", "projection_id"):
            _uuid(getattr(self, name), f"decision {name}")
        _hash(self.projection_hash, "decision projection_hash")
        object.__setattr__(
            self,
            "outcome",
            _enum(self.outcome, RuntimeDecisionOutcome, "decision outcome"),
        )
        _bounded_text(self.reason_code, "decision reason_code", _MAX_REASON_LENGTH)
        _bounded_text(self.summary, "decision summary", _MAX_TEXT_LENGTH)
        object.__setattr__(
            self,
            "strategy_state",
            _freeze_json_mapping(self.strategy_state, "strategy_state"),
        )
        intents = tuple(self.intents)
        if any(
            not isinstance(item, ReleaseExecutionRequestIntentV1) for item in intents
        ):
            raise RuntimeEnvelopeError("decision contains an invalid intent")
        ordinals = [item.ordinal for item in intents]
        if ordinals != list(range(len(ordinals))):
            raise RuntimeEnvelopeError(
                "contiguous intent ordinals must be unique and start at zero"
            )
        if self.outcome is RuntimeDecisionOutcome.NO_CHANGE and intents:
            raise RuntimeEnvelopeError("no_change decision cannot contain intents")
        if self.outcome is RuntimeDecisionOutcome.INTENTS and not intents:
            raise RuntimeEnvelopeError("intents decision must contain intents")
        object.__setattr__(self, "intents", intents)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_id": str(self.operation_id),
            "run_id": str(self.run_id),
            "projection_id": str(self.projection_id),
            "projection_hash": self.projection_hash,
            "outcome": self.outcome.value,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "strategy_state": _json_value(self.strategy_state),
            "intents": [item.to_mapping() for item in self.intents],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    @property
    def content_hash(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReleaseExecutionRequestIntentV2:
    intent_type: ClassVar[str] = "release_execution_request"
    ordinal: int
    target_execution_request_id: UUID
    target_request_version: int
    reason_code: str
    summary: str
    idempotency_key: str
    input_group_selections: tuple[InputSelectionGroupV1, ...]

    def __post_init__(self) -> None:
        _non_negative_int(self.ordinal, "intent ordinal")
        _uuid(self.target_execution_request_id, "intent target_execution_request_id")
        _positive_int(self.target_request_version, "intent target_request_version")
        _bounded_text(self.reason_code, "intent reason_code", _MAX_REASON_LENGTH)
        _bounded_text(self.summary, "intent summary", _MAX_TEXT_LENGTH)
        _bounded_text(
            self.idempotency_key,
            "intent idempotency_key",
            _MAX_IDEMPOTENCY_KEY_LENGTH,
        )
        groups = tuple(self.input_group_selections)
        if any(not isinstance(item, InputSelectionGroupV1) for item in groups):
            raise RuntimeEnvelopeError(
                "intent contains an invalid input group selection"
            )
        if len(groups) > _MAX_INPUT_GROUPS_PER_CONSUMER:
            raise RuntimeEnvelopeError(
                "intent contains too many input group selections"
            )
        group_keys = tuple(item.group_key for item in groups)
        if len(group_keys) != len(set(group_keys)):
            raise RuntimeEnvelopeError("intent input group selections must be unique")
        if any(len(item.sources) > 32 for item in groups):
            raise RuntimeEnvelopeError(
                "intent input group selection has too many sources"
            )
        object.__setattr__(
            self,
            "input_group_selections",
            tuple(sorted(groups, key=lambda item: item.group_key)),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "intent_type": self.intent_type,
            "ordinal": self.ordinal,
            "target_execution_request_id": str(self.target_execution_request_id),
            "target_request_version": self.target_request_version,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "idempotency_key": self.idempotency_key,
            "input_group_selections": [
                item.to_mapping() for item in self.input_group_selections
            ],
        }


@dataclass(frozen=True, slots=True)
class RuntimeDecisionV2:
    schema_version: int
    operation_id: UUID
    run_id: UUID
    projection_id: UUID
    projection_hash: str
    outcome: RuntimeDecisionOutcome
    reason_code: str
    summary: str
    strategy_state: Mapping[str, object]
    intents: tuple[ReleaseExecutionRequestIntentV2, ...]

    def __post_init__(self) -> None:
        _positive_int(self.schema_version, "decision schema_version")
        if self.schema_version != 2:
            raise RuntimeEnvelopeError("unsupported decision schema_version")
        for name in ("operation_id", "run_id", "projection_id"):
            _uuid(getattr(self, name), f"decision {name}")
        _hash(self.projection_hash, "decision projection_hash")
        object.__setattr__(
            self,
            "outcome",
            _enum(self.outcome, RuntimeDecisionOutcome, "decision outcome"),
        )
        _bounded_text(self.reason_code, "decision reason_code", _MAX_REASON_LENGTH)
        _bounded_text(self.summary, "decision summary", _MAX_TEXT_LENGTH)
        object.__setattr__(
            self,
            "strategy_state",
            _freeze_json_mapping(self.strategy_state, "strategy_state"),
        )
        intents = tuple(self.intents)
        if any(
            not isinstance(item, ReleaseExecutionRequestIntentV2) for item in intents
        ):
            raise RuntimeEnvelopeError("decision contains an invalid intent")
        ordinals = [item.ordinal for item in intents]
        if ordinals != list(range(len(ordinals))):
            raise RuntimeEnvelopeError(
                "contiguous intent ordinals must be unique and start at zero"
            )
        if self.outcome is RuntimeDecisionOutcome.NO_CHANGE and intents:
            raise RuntimeEnvelopeError("no_change decision cannot contain intents")
        if self.outcome is RuntimeDecisionOutcome.INTENTS and not intents:
            raise RuntimeEnvelopeError("intents decision must contain intents")
        object.__setattr__(self, "intents", intents)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_id": str(self.operation_id),
            "run_id": str(self.run_id),
            "projection_id": str(self.projection_id),
            "projection_hash": self.projection_hash,
            "outcome": self.outcome.value,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "strategy_state": _json_value(self.strategy_state),
            "intents": [item.to_mapping() for item in self.intents],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    @property
    def content_hash(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()


RuntimeProjection = RuntimeProjectionV1 | RuntimeProjectionV2
RuntimeDecision = RuntimeDecisionV1 | RuntimeDecisionV2


class CollaborationRuntime(Protocol):
    def evaluate(
        self,
        projection: RuntimeProjection,
        *,
        operation_id: UUID,
        deadline: datetime,
    ) -> RuntimeDecision:
        raise NotImplementedError


def canonical_json_bytes(value: object) -> bytes:
    try:
        normalized = _json_value(value)
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RuntimeEnvelopeError("value cannot be canonicalized as JSON") from exc


def parse_runtime_decision_v1(
    raw: bytes,
    *,
    max_bytes: int,
    max_intents: int,
    max_state_bytes: int,
) -> RuntimeDecisionV1:
    _positive_limit(max_bytes, "maximum response size")
    _positive_limit(max_intents, "maximum intent count")
    _positive_limit(max_state_bytes, "maximum strategy_state size")
    if not isinstance(raw, bytes):
        raise RuntimeEnvelopeError("runtime response must be bytes")
    if len(raw) > max_bytes:
        raise RuntimeEnvelopeError("runtime response exceeds maximum response size")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeEnvelopeError("runtime response is not valid UTF-8") from exc
    try:
        decoder = json.JSONDecoder(
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_non_finite,
        )
        value, end = decoder.raw_decode(text)
    except RuntimeEnvelopeError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeEnvelopeError("runtime response is invalid JSON") from exc
    if text[end:].strip():
        raise RuntimeEnvelopeError("runtime response contains trailing JSON")
    mapping = _strict_object(
        value,
        "decision",
        {
            "schema_version",
            "operation_id",
            "run_id",
            "projection_id",
            "projection_hash",
            "outcome",
            "reason_code",
            "summary",
            "strategy_state",
            "intents",
        },
    )
    schema_version = _version(mapping.get("schema_version"), "decision schema_version")
    if schema_version != 1:
        raise RuntimeEnvelopeError("unsupported decision schema_version")
    raw_state = mapping.get("strategy_state")
    strategy_state = _freeze_json_mapping(raw_state, "strategy_state")
    if len(canonical_json_bytes(strategy_state)) > max_state_bytes:
        raise RuntimeEnvelopeError("strategy_state exceeds maximum strategy_state size")
    raw_intents = mapping.get("intents")
    if not isinstance(raw_intents, list):
        raise RuntimeEnvelopeError("decision intents must be an array")
    if len(raw_intents) > max_intents:
        raise RuntimeEnvelopeError("decision exceeds maximum intent count")
    outcome = _enum(mapping.get("outcome"), RuntimeDecisionOutcome, "decision outcome")
    if outcome is RuntimeDecisionOutcome.NO_CHANGE and raw_intents:
        raise RuntimeEnvelopeError("no_change decision cannot contain intents")
    if outcome is RuntimeDecisionOutcome.INTENTS and not raw_intents:
        raise RuntimeEnvelopeError("intents decision must contain intents")
    intents = tuple(_parse_intent(item) for item in raw_intents)
    return RuntimeDecisionV1(
        schema_version=schema_version,
        operation_id=_wire_uuid(mapping.get("operation_id"), "operation_id"),
        run_id=_wire_uuid(mapping.get("run_id"), "run_id"),
        projection_id=_wire_uuid(mapping.get("projection_id"), "projection_id"),
        projection_hash=_wire_hash(mapping.get("projection_hash"), "projection_hash"),
        outcome=outcome,
        reason_code=_wire_text(
            mapping.get("reason_code"), "reason_code", _MAX_REASON_LENGTH
        ),
        summary=_wire_text(mapping.get("summary"), "summary", _MAX_TEXT_LENGTH),
        strategy_state=strategy_state,
        intents=intents,
    )


def parse_runtime_decision_v2(
    raw: bytes,
    *,
    max_bytes: int,
    max_intents: int,
    max_state_bytes: int,
) -> RuntimeDecisionV2:
    _positive_limit(max_bytes, "maximum response size")
    _positive_limit(max_intents, "maximum intent count")
    _positive_limit(max_state_bytes, "maximum strategy_state size")
    if not isinstance(raw, bytes):
        raise RuntimeEnvelopeError("runtime response must be bytes")
    if len(raw) > max_bytes:
        raise RuntimeEnvelopeError("runtime response exceeds maximum response size")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeEnvelopeError("runtime response is not valid UTF-8") from exc
    try:
        decoder = json.JSONDecoder(
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_non_finite,
        )
        value, end = decoder.raw_decode(text)
    except RuntimeEnvelopeError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeEnvelopeError("runtime response is invalid JSON") from exc
    if text[end:].strip():
        raise RuntimeEnvelopeError("runtime response contains trailing JSON")
    mapping = _strict_object(
        value,
        "decision",
        {
            "schema_version",
            "operation_id",
            "run_id",
            "projection_id",
            "projection_hash",
            "outcome",
            "reason_code",
            "summary",
            "strategy_state",
            "intents",
        },
    )
    schema_version = _version(mapping.get("schema_version"), "decision schema_version")
    if schema_version != 2:
        raise RuntimeEnvelopeError("unsupported decision schema_version")
    strategy_state = _freeze_json_mapping(
        mapping.get("strategy_state"), "strategy_state"
    )
    if len(canonical_json_bytes(strategy_state)) > max_state_bytes:
        raise RuntimeEnvelopeError("strategy_state exceeds maximum strategy_state size")
    raw_intents = mapping.get("intents")
    if not isinstance(raw_intents, list):
        raise RuntimeEnvelopeError("decision intents must be an array")
    if len(raw_intents) > max_intents:
        raise RuntimeEnvelopeError("decision exceeds maximum intent count")
    outcome = _enum(mapping.get("outcome"), RuntimeDecisionOutcome, "decision outcome")
    if outcome is RuntimeDecisionOutcome.NO_CHANGE and raw_intents:
        raise RuntimeEnvelopeError("no_change decision cannot contain intents")
    if outcome is RuntimeDecisionOutcome.INTENTS and not raw_intents:
        raise RuntimeEnvelopeError("intents decision must contain intents")
    intents = tuple(_parse_intent_v2(item) for item in raw_intents)
    return RuntimeDecisionV2(
        schema_version=schema_version,
        operation_id=_wire_uuid(mapping.get("operation_id"), "operation_id"),
        run_id=_wire_uuid(mapping.get("run_id"), "run_id"),
        projection_id=_wire_uuid(mapping.get("projection_id"), "projection_id"),
        projection_hash=_wire_hash(mapping.get("projection_hash"), "projection_hash"),
        outcome=outcome,
        reason_code=_wire_text(
            mapping.get("reason_code"), "reason_code", _MAX_REASON_LENGTH
        ),
        summary=_wire_text(mapping.get("summary"), "summary", _MAX_TEXT_LENGTH),
        strategy_state=strategy_state,
        intents=intents,
    )


def _parse_intent(value: object) -> ReleaseExecutionRequestIntentV1:
    mapping = _strict_object(
        value,
        "intent",
        {
            "intent_type",
            "ordinal",
            "target_execution_request_id",
            "target_request_version",
            "reason_code",
            "summary",
            "idempotency_key",
        },
    )
    intent_type = mapping.get("intent_type")
    if intent_type != ReleaseExecutionRequestIntentV1.intent_type:
        raise RuntimeEnvelopeError(f"unsupported intent_type: {intent_type!r}")
    return ReleaseExecutionRequestIntentV1(
        ordinal=_non_negative_version(mapping.get("ordinal"), "intent ordinal"),
        target_execution_request_id=_wire_uuid(
            mapping.get("target_execution_request_id"),
            "target_execution_request_id",
        ),
        target_request_version=_version(
            mapping.get("target_request_version"), "target_request_version"
        ),
        reason_code=_wire_text(
            mapping.get("reason_code"), "reason_code", _MAX_REASON_LENGTH
        ),
        summary=_wire_text(mapping.get("summary"), "summary", _MAX_TEXT_LENGTH),
        idempotency_key=_wire_text(
            mapping.get("idempotency_key"),
            "idempotency_key",
            _MAX_IDEMPOTENCY_KEY_LENGTH,
        ),
    )


def _parse_intent_v2(value: object) -> ReleaseExecutionRequestIntentV2:
    mapping = _strict_object(
        value,
        "intent",
        {
            "intent_type",
            "ordinal",
            "target_execution_request_id",
            "target_request_version",
            "reason_code",
            "summary",
            "idempotency_key",
            "input_group_selections",
        },
    )
    intent_type = mapping.get("intent_type")
    if intent_type != ReleaseExecutionRequestIntentV2.intent_type:
        raise RuntimeEnvelopeError(f"unsupported intent_type: {intent_type!r}")
    raw_groups = mapping.get("input_group_selections")
    if not isinstance(raw_groups, list):
        raise RuntimeEnvelopeError("intent input_group_selections must be an array")
    if len(raw_groups) > _MAX_INPUT_GROUPS_PER_CONSUMER:
        raise RuntimeEnvelopeError("intent contains too many input group selections")
    try:
        groups = tuple(InputSelectionGroupV1.from_mapping(item) for item in raw_groups)
    except ValidationError as exc:
        raise RuntimeEnvelopeError("intent input group selections are invalid") from exc
    return ReleaseExecutionRequestIntentV2(
        ordinal=_non_negative_version(mapping.get("ordinal"), "intent ordinal"),
        target_execution_request_id=_wire_uuid(
            mapping.get("target_execution_request_id"),
            "target_execution_request_id",
        ),
        target_request_version=_version(
            mapping.get("target_request_version"), "target_request_version"
        ),
        reason_code=_wire_text(
            mapping.get("reason_code"), "reason_code", _MAX_REASON_LENGTH
        ),
        summary=_wire_text(mapping.get("summary"), "summary", _MAX_TEXT_LENGTH),
        idempotency_key=_wire_text(
            mapping.get("idempotency_key"),
            "idempotency_key",
            _MAX_IDEMPOTENCY_KEY_LENGTH,
        ),
        input_group_selections=groups,
    )


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeEnvelopeError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise RuntimeEnvelopeError(f"JSON contains non-finite number: {value}")


def _strict_object(value: object, name: str, allowed: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeEnvelopeError(f"{name} must be a JSON object")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise RuntimeEnvelopeError(f"unknown {name} fields: {', '.join(unknown)}")
    return cast(Mapping[str, object], value)


def _freeze_projection_mapping(
    value: Mapping[str, object], name: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeEnvelopeError(f"projection {name} must be an object")
    _reject_private_projection_fields(value, name)
    return _freeze_json_mapping(value, name)


def _freeze_projection_sequence(
    value: Sequence[Mapping[str, object]], name: str
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        raise RuntimeEnvelopeError(f"projection {name} must be a sequence")
    if len(value) > _MAX_JSON_ENTRIES:
        raise RuntimeEnvelopeError(f"projection {name} contains too many items")
    frozen: list[Mapping[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise RuntimeEnvelopeError(f"projection {name}[{index}] must be an object")
        _reject_private_projection_fields(item, f"{name}[{index}]")
        frozen_item = _freeze_json_mapping(item, f"{name}[{index}]")
        frozen.append(frozen_item)
    return tuple(frozen)


def _reject_private_projection_fields(value: Mapping[str, object], name: str) -> None:
    for key, item in value.items():
        if not isinstance(key, str):
            raise RuntimeEnvelopeError(f"{name} keys must be strings")
        normalized = key.strip().lower().replace("-", "_")
        if normalized in _PRIVATE_PROJECTION_FIELDS:
            raise RuntimeEnvelopeError(f"private projection field: {key}")
        if isinstance(item, Mapping):
            _reject_private_projection_fields(item, f"{name}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                if isinstance(nested, Mapping):
                    _reject_private_projection_fields(nested, f"{name}.{key}[{index}]")


def _freeze_json_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeEnvelopeError(f"{name} must be a JSON object")
    frozen = _freeze_json(value, name, depth=0)
    if not isinstance(frozen, Mapping):
        raise RuntimeEnvelopeError(f"{name} must be a JSON object")
    return cast(Mapping[str, object], frozen)


def _freeze_json(value: object, name: str, *, depth: int) -> object:
    if depth > _MAX_JSON_DEPTH:
        raise RuntimeEnvelopeError(f"{name} exceeds maximum JSON depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeEnvelopeError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT_LENGTH:
            raise RuntimeEnvelopeError(f"{name} contains an oversized string")
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        if len(value) > _MAX_JSON_ENTRIES:
            raise RuntimeEnvelopeError(f"{name} contains too many entries")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise RuntimeEnvelopeError(f"{name} keys must be non-empty strings")
            result[key] = _freeze_json(item, f"{name}.{key}", depth=depth + 1)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_JSON_ENTRIES:
            raise RuntimeEnvelopeError(f"{name} contains too many items")
        return tuple(
            _freeze_json(item, f"{name}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        )
    raise RuntimeEnvelopeError(f"{name} contains a non-JSON value")


def _sorted_json_mappings(
    values: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], _json_value(item))
        for item in sorted(values, key=_mapping_sort_key)
    ]


def _mapping_sort_key(value: Mapping[str, object]) -> tuple[str, str]:
    for field in (
        "key",
        "task_id",
        "subtask_key",
        "worker_item_key",
        "execution_request_id",
        "execution_id",
        "artifact_id",
        "evidence_id",
        "release_id",
        "id",
    ):
        item = value.get(field)
        if item is not None:
            return field, str(item)
    return "canonical", canonical_json_bytes(value).decode("utf-8")


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise RuntimeEnvelopeError(f"{name} must be a UUID")
    return value


def _wire_uuid(value: object, name: str) -> UUID:
    if not isinstance(value, str):
        raise RuntimeEnvelopeError(f"{name} must be a UUID string")
    try:
        return UUID(value)
    except ValueError as exc:
        raise RuntimeEnvelopeError(f"{name} must be a UUID string") from exc


def _wire_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeEnvelopeError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _wire_mapping_sequence(
    value: object, name: str
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        raise RuntimeEnvelopeError(f"{name} must be an array")
    items: list[Mapping[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise RuntimeEnvelopeError(f"{name}[{index}] must be an object")
        items.append(cast(Mapping[str, object], item))
    return tuple(items)


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeEnvelopeError(f"{name} must be positive")
    return value


def _version(value: object, name: str) -> int:
    return _positive_int(value, name)


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeEnvelopeError(f"{name} must be non-negative")
    return value


def _non_negative_version(value: object, name: str) -> int:
    return _non_negative_int(value, name)


def _positive_limit(value: object, name: str) -> int:
    return _positive_int(value, name)


def _hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise RuntimeEnvelopeError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _wire_hash(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeEnvelopeError(f"{name} must be a SHA-256 hex digest")
    return _hash(value, name)


def _bounded_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeEnvelopeError(f"{name} is required")
    if len(value) > maximum:
        raise RuntimeEnvelopeError(f"{name} exceeds maximum length")
    return value


def _wire_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise RuntimeEnvelopeError(f"{name} must be a string")
    return _bounded_text(value, name, maximum)


def _aware(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RuntimeEnvelopeError(f"{name} must be timezone-aware")
    return value


def _enum[T: StrEnum](value: object, enum_type: type[T], name: str) -> T:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeEnvelopeError(f"unsupported {name}: {value!r}") from exc
