from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sidera_agents.contracts import ProfileRef

from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyRequestV1,
    CollaborationPolicyRequestV2,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_profiles import PlanningProfileSnapshot
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskMilestone,
    WorkerItemDependency,
    WorkItemKind,
)
from sidera_control_plane.domain.result_integration import (
    IntegrationSpecV1,
    validate_integration_spec,
)

if TYPE_CHECKING:
    from sidera_control_plane.domain.planning_revision_proposals import (
        PlanningRevisionProposalPayloadV3,
        PlanningRevisionProposalPayloadV4,
        PlanningRevisionProposalPayloadV5,
        PlanningRevisionProposalPayloadV6,
    )
    from sidera_control_plane.domain.revision_diff import NormalizedRevisionDiff


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    understanding: str
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    estimated_cost_microunits: int
    estimated_runtime_seconds: int
    estimated_concurrency: int
    scope_includes: tuple[str, ...]
    scope_excludes: tuple[str, ...]
    metadata: Mapping[str, object]
    estimated_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "understanding", _required_text(self.understanding, "understanding")
        )
        object.__setattr__(
            self, "assumptions", _text_tuple(self.assumptions, "assumptions")
        )
        object.__setattr__(self, "risks", _text_tuple(self.risks, "risks"))
        object.__setattr__(
            self, "scope_includes", _text_tuple(self.scope_includes, "scope_includes")
        )
        object.__setattr__(
            self, "scope_excludes", _text_tuple(self.scope_excludes, "scope_excludes")
        )
        _non_negative_int(self.estimated_cost_microunits, "estimated_cost_microunits")
        _non_negative_int(self.estimated_tokens, "estimated_tokens")
        _non_negative_int(self.estimated_runtime_seconds, "estimated_runtime_seconds")
        _positive_int(self.estimated_concurrency, "estimated_concurrency")
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class ProposedRootTask:
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _required_text(self.title, "root title"))
        object.__setattr__(
            self, "goal_summary", _required_text(self.goal_summary, "root goal_summary")
        )
        object.__setattr__(
            self,
            "acceptance_refs",
            _text_tuple(
                self.acceptance_refs, "root acceptance_refs", allow_empty=False
            ),
        )


@dataclass(frozen=True, slots=True)
class ProposedSubtask:
    key: str
    kind: WorkItemKind
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    milestone_key: str | None
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _key(self.key, "subtask key"))
        object.__setattr__(self, "kind", _enum(self.kind, WorkItemKind, "subtask kind"))
        object.__setattr__(self, "title", _required_text(self.title, "subtask title"))
        object.__setattr__(
            self,
            "goal_summary",
            _required_text(self.goal_summary, "subtask goal_summary"),
        )
        object.__setattr__(
            self,
            "acceptance_refs",
            _text_tuple(
                self.acceptance_refs, "subtask acceptance_refs", allow_empty=False
            ),
        )
        object.__setattr__(
            self,
            "expected_outputs",
            _text_tuple(
                self.expected_outputs, "subtask expected_outputs", allow_empty=False
            ),
        )
        object.__setattr__(
            self, "milestone_key", _optional_text(self.milestone_key, "milestone_key")
        )
        object.__setattr__(
            self, "source_refs", _text_tuple(self.source_refs, "subtask source_refs")
        )


@dataclass(frozen=True, slots=True)
class ProposedWorkerItem:
    key: str
    subtask_key: str
    title: str
    role: str
    responsibility: str
    capability_requirements: tuple[str, ...]
    worker_policy_ref: str | None
    configuration_ref: str | None
    concurrency_limit: int | None
    source_refs: tuple[str, ...]
    pinned_worker_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _key(self.key, "worker item key"))
        object.__setattr__(
            self, "subtask_key", _key(self.subtask_key, "worker subtask key")
        )
        object.__setattr__(
            self, "title", _required_text(self.title, "worker item title")
        )
        object.__setattr__(self, "role", _required_text(self.role, "worker item role"))
        object.__setattr__(
            self,
            "responsibility",
            _required_text(self.responsibility, "worker item responsibility"),
        )
        object.__setattr__(
            self,
            "capability_requirements",
            _text_tuple(
                self.capability_requirements,
                "capability_requirements",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "worker_policy_ref",
            _optional_text(self.worker_policy_ref, "worker_policy_ref"),
        )
        object.__setattr__(
            self,
            "configuration_ref",
            _optional_text(self.configuration_ref, "configuration_ref"),
        )
        if self.concurrency_limit is not None:
            _positive_int(self.concurrency_limit, "concurrency_limit")
        object.__setattr__(
            self,
            "source_refs",
            _text_tuple(self.source_refs, "worker item source_refs"),
        )
        object.__setattr__(
            self,
            "pinned_worker_id",
            _optional_text(self.pinned_worker_id, "pinned_worker_id"),
        )


@dataclass(frozen=True, slots=True)
class ActivationDependency:
    upstream_worker_item_key: str
    downstream_worker_item_key: str
    kind: str
    required_artifact_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "upstream_worker_item_key",
            _key(self.upstream_worker_item_key, "activation upstream key"),
        )
        object.__setattr__(
            self,
            "downstream_worker_item_key",
            _key(self.downstream_worker_item_key, "activation downstream key"),
        )
        object.__setattr__(
            self, "kind", _required_text(self.kind, "activation dependency kind")
        )
        object.__setattr__(
            self,
            "required_artifact_refs",
            _text_tuple(self.required_artifact_refs, "required_artifact_refs"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    worker_item_key: str
    purpose: str
    context_refs: tuple[str, ...]
    expected_artifact_refs: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]
    capability_requirements: tuple[str, ...]
    adapter_preferences: tuple[str, ...]
    budget_class: str
    deadline_seconds: int | None
    not_before_seconds: int | None
    max_attempts: int
    activation_dependencies: tuple[ActivationDependency, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "worker_item_key",
            _key(self.worker_item_key, "intent worker item key"),
        )
        object.__setattr__(
            self, "purpose", _required_text(self.purpose, "intent purpose")
        )
        object.__setattr__(
            self, "context_refs", _text_tuple(self.context_refs, "intent context_refs")
        )
        object.__setattr__(
            self,
            "expected_artifact_refs",
            _text_tuple(
                self.expected_artifact_refs, "expected_artifact_refs", allow_empty=False
            ),
        )
        object.__setattr__(
            self,
            "required_evidence_refs",
            _text_tuple(
                self.required_evidence_refs, "required_evidence_refs", allow_empty=False
            ),
        )
        object.__setattr__(
            self,
            "capability_requirements",
            _text_tuple(
                self.capability_requirements,
                "intent capability_requirements",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "adapter_preferences",
            _text_tuple(self.adapter_preferences, "adapter_preferences"),
        )
        object.__setattr__(
            self, "budget_class", _required_text(self.budget_class, "budget_class")
        )
        if self.deadline_seconds is not None:
            _non_negative_int(self.deadline_seconds, "deadline_seconds")
        if self.not_before_seconds is not None:
            _non_negative_int(self.not_before_seconds, "not_before_seconds")
        _positive_int(self.max_attempts, "max_attempts")
        object.__setattr__(
            self, "activation_dependencies", tuple(self.activation_dependencies)
        )


@dataclass(frozen=True, slots=True)
class ImpactPolicy:
    activate_immediately: bool
    budget_level: str
    failure_policy: str
    manual_gate: bool = False
    revision_operation: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.activate_immediately, bool):
            raise ValidationError("activate_immediately must be a boolean")
        if not isinstance(self.manual_gate, bool):
            raise ValidationError("manual_gate must be a boolean")
        object.__setattr__(
            self,
            "budget_level",
            _required_text(self.budget_level, "impact budget_level"),
        )
        object.__setattr__(
            self,
            "failure_policy",
            _required_text(self.failure_policy, "failure_policy"),
        )
        object.__setattr__(
            self,
            "revision_operation",
            _optional_text(self.revision_operation, "revision_operation"),
        )


@dataclass(frozen=True, slots=True)
class PlanningProfileDescriptor:
    profile_id: UUID
    profile_version: int
    planner_module_instance_id: UUID | None
    planner_module_config_version: int | None
    execution_mode: str = "legacy"
    planner_agent_ref: ProfileRef | None = None

    @classmethod
    def from_snapshot(
        cls, snapshot: PlanningProfileSnapshot
    ) -> PlanningProfileDescriptor:
        return cls(
            profile_id=snapshot.profile_id,
            profile_version=snapshot.profile_version,
            planner_module_instance_id=snapshot.planner_module_instance_id,
            planner_module_config_version=snapshot.planner_module_config_version,
            execution_mode=snapshot.execution_mode,
            planner_agent_ref=snapshot.planner_agent_ref,
        )

    def __post_init__(self) -> None:
        _positive_int(self.profile_version, "profile_version")
        if self.execution_mode == "native_profile":
            if (
                self.planner_module_instance_id is not None
                or self.planner_module_config_version is not None
                or not isinstance(self.planner_agent_ref, ProfileRef)
            ):
                raise ValidationError(
                    "native proposal requires only its exact Agent Profile pin"
                )
        elif self.execution_mode == "legacy":
            if (
                not isinstance(self.planner_module_instance_id, UUID)
                or self.planner_agent_ref is not None
            ):
                raise ValidationError("legacy proposal requires its planner module")
            _positive_int(
                self.planner_module_config_version, "planner_module_config_version"
            )
        else:
            raise ValidationError("invalid proposal execution mode")

    def to_mapping(self) -> dict[str, object]:
        value = {
            "profile_id": str(self.profile_id),
            "profile_version": self.profile_version,
            "planner_module_instance_id": None
            if self.planner_module_instance_id is None
            else str(self.planner_module_instance_id),
            "planner_module_config_version": self.planner_module_config_version,
        }
        if self.execution_mode == "native_profile":
            value.update(
                execution_mode=self.execution_mode,
                planner_agent_ref=self.planner_agent_ref.model_dump(mode="json"),
            )
        return value


@dataclass(frozen=True, slots=True)
class ProposalDiff:
    root_task_added: bool
    added_subtask_keys: tuple[str, ...]
    added_worker_item_keys: tuple[str, ...]
    added_subtask_dependency_edges: tuple[tuple[str, str], ...]
    added_worker_item_dependency_edges: tuple[tuple[str, str, str], ...]
    added_execution_intent_keys: tuple[str, ...]
    immediately_ready_worker_item_keys: tuple[str, ...]
    blocked_worker_item_keys: tuple[str, ...]

    @property
    def added_worker_item_dependency_count(self) -> int:
        return len(self.added_worker_item_dependency_edges)


@dataclass(frozen=True, slots=True)
class PlanningProposalPayload:
    schema_version: int
    kind: PlanningRequestKind
    root_task: ProposedRootTask
    subtasks: tuple[ProposedSubtask, ...]
    worker_items: tuple[ProposedWorkerItem, ...]
    subtask_milestones: tuple[SubtaskMilestone, ...]
    subtask_dependencies: tuple[SubtaskDependency, ...]
    worker_item_dependencies: tuple[WorkerItemDependency, ...]
    execution_intents: tuple[ExecutionIntent, ...]
    decision_summary: DecisionSummary
    impact_policy: ImpactPolicy
    base_descriptor: Mapping[str, object] | None

    def __post_init__(self) -> None:
        _positive_int(self.schema_version, "proposal schema_version")
        object.__setattr__(
            self, "kind", _enum(self.kind, PlanningRequestKind, "proposal kind")
        )
        object.__setattr__(self, "subtasks", tuple(self.subtasks))
        object.__setattr__(self, "worker_items", tuple(self.worker_items))
        object.__setattr__(self, "subtask_milestones", tuple(self.subtask_milestones))
        object.__setattr__(
            self, "subtask_dependencies", tuple(self.subtask_dependencies)
        )
        object.__setattr__(
            self, "worker_item_dependencies", tuple(self.worker_item_dependencies)
        )
        object.__setattr__(self, "execution_intents", tuple(self.execution_intents))
        if self.base_descriptor is not None:
            object.__setattr__(
                self,
                "base_descriptor",
                _freeze_mapping(self.base_descriptor, "base_descriptor"),
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "root_task": {
                "title": self.root_task.title,
                "goal_summary": self.root_task.goal_summary,
                "acceptance_refs": list(self.root_task.acceptance_refs),
            },
            "subtasks": [_subtask_mapping(item) for item in self.subtasks],
            "worker_items": [_worker_item_mapping(item) for item in self.worker_items],
            "milestones": [
                _milestone_mapping(item) for item in self.subtask_milestones
            ],
            "subtask_dependencies": [
                _subtask_dependency_mapping(item) for item in self.subtask_dependencies
            ],
            "worker_item_dependencies": [
                _worker_dependency_mapping(item)
                for item in self.worker_item_dependencies
            ],
            "execution_intents": [
                _intent_mapping(item) for item in self.execution_intents
            ],
            "decision_summary": _summary_mapping(self.decision_summary),
            "impact_policy": _impact_mapping(self.impact_policy),
            "base_descriptor": _json_value(self.base_descriptor),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PlanningProposalPayload:
        if not isinstance(value, Mapping):
            raise ValidationError("proposal payload must be a mapping")
        allowed = {
            "schema_version",
            "kind",
            "root_task",
            "subtasks",
            "worker_items",
            "milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "decision_summary",
            "impact_policy",
            "base_descriptor",
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValidationError(f"unknown proposal fields: {', '.join(unknown)}")
        try:
            root = _mapping(value, "root_task")
            summary = _mapping(value, "decision_summary")
            impact = _mapping(value, "impact_policy")
            metadata = summary.get("metadata", {})
            if not isinstance(metadata, Mapping):
                raise ValidationError("proposal field 'metadata' must be a mapping")
            raw_base_descriptor = value.get("base_descriptor")
            base_descriptor = (
                None
                if raw_base_descriptor is None
                else _mapping(value, "base_descriptor")
            )
            return cls(
                schema_version=_int(value, "schema_version"),
                kind=PlanningRequestKind(_string(value, "kind")),
                root_task=ProposedRootTask(
                    title=_string(root, "title"),
                    goal_summary=_string(root, "goal_summary"),
                    acceptance_refs=_strings(root, "acceptance_refs"),
                ),
                subtasks=tuple(
                    _subtask_from_mapping(item) for item in _mappings(value, "subtasks")
                ),
                worker_items=tuple(
                    _worker_from_mapping(item)
                    for item in _mappings(value, "worker_items")
                ),
                subtask_milestones=tuple(
                    _milestone_from_mapping(item)
                    for item in _mappings(value, "milestones")
                ),
                subtask_dependencies=tuple(
                    _subtask_dependency_from_mapping(item)
                    for item in _mappings(value, "subtask_dependencies")
                ),
                worker_item_dependencies=tuple(
                    _worker_dependency_from_mapping(item)
                    for item in _mappings(value, "worker_item_dependencies")
                ),
                execution_intents=tuple(
                    _intent_from_mapping(item)
                    for item in _mappings(value, "execution_intents")
                ),
                decision_summary=DecisionSummary(
                    understanding=_string(summary, "understanding"),
                    assumptions=_strings(summary, "assumptions"),
                    risks=_strings(summary, "risks"),
                    estimated_cost_microunits=_int(
                        summary, "estimated_cost_microunits"
                    ),
                    estimated_tokens=(
                        _int(summary, "estimated_tokens")
                        if "estimated_tokens" in summary
                        else 0
                    ),
                    estimated_runtime_seconds=_int(
                        summary, "estimated_runtime_seconds"
                    ),
                    estimated_concurrency=_int(summary, "estimated_concurrency"),
                    scope_includes=_strings(summary, "scope_includes"),
                    scope_excludes=_strings(summary, "scope_excludes"),
                    metadata=metadata,
                ),
                impact_policy=ImpactPolicy(
                    activate_immediately=_bool(impact, "activate_immediately"),
                    budget_level=_string(impact, "budget_level"),
                    failure_policy=_string(impact, "failure_policy"),
                    manual_gate=bool(impact.get("manual_gate", False)),
                    revision_operation=(
                        str(impact["revision_operation"])
                        if impact.get("revision_operation") is not None
                        else None
                    ),
                ),
                base_descriptor=base_descriptor,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"invalid proposal payload: {exc}") from exc


@dataclass(frozen=True, slots=True)
class PlanningProposalPayloadV4:
    schema_version: int
    kind: PlanningRequestKind
    root_task: ProposedRootTask
    subtasks: tuple[ProposedSubtask, ...]
    worker_items: tuple[ProposedWorkerItem, ...]
    subtask_milestones: tuple[SubtaskMilestone, ...]
    subtask_dependencies: tuple[SubtaskDependency, ...]
    worker_item_dependencies: tuple[WorkerItemDependency, ...]
    execution_intents: tuple[ExecutionIntent, ...]
    decision_summary: DecisionSummary
    impact_policy: ImpactPolicy
    collaboration_policy_request: CollaborationPolicyRequestV1

    def __post_init__(self) -> None:
        if self.schema_version != 4:
            raise ValidationError("proposal schema_version must be 4")
        object.__setattr__(
            self, "kind", _enum(self.kind, PlanningRequestKind, "proposal kind")
        )
        if self.kind is not PlanningRequestKind.CREATE:
            raise ValidationError("proposal v4 kind must be create")
        for name in (
            "subtasks",
            "worker_items",
            "subtask_milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not isinstance(
            self.collaboration_policy_request, CollaborationPolicyRequestV1
        ):
            raise ValidationError("proposal v4 requires a collaboration policy request")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PlanningProposalPayloadV4:
        if not isinstance(value, Mapping):
            raise ValidationError("proposal v4 payload must be a mapping")
        allowed = {
            "schema_version",
            "kind",
            "root_task",
            "subtasks",
            "worker_items",
            "milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "decision_summary",
            "impact_policy",
            "collaboration_policy_request",
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValidationError(f"unknown proposal v4 fields: {', '.join(unknown)}")
        if _int(value, "schema_version") != 4:
            raise ValidationError("proposal schema_version must be 4")
        policy_value = value.get("collaboration_policy_request")
        if not isinstance(policy_value, Mapping):
            raise ValidationError("collaboration_policy_request must be a mapping")
        legacy_mapping = dict(value)
        legacy_mapping.pop("collaboration_policy_request", None)
        legacy_mapping["schema_version"] = 2
        legacy_mapping["base_descriptor"] = None
        legacy = PlanningProposalPayload.from_mapping(legacy_mapping)
        if legacy.kind is not PlanningRequestKind.CREATE:
            raise ValidationError("proposal v4 kind must be create")
        return cls(
            schema_version=4,
            kind=legacy.kind,
            root_task=legacy.root_task,
            subtasks=legacy.subtasks,
            worker_items=legacy.worker_items,
            subtask_milestones=legacy.subtask_milestones,
            subtask_dependencies=legacy.subtask_dependencies,
            worker_item_dependencies=legacy.worker_item_dependencies,
            execution_intents=legacy.execution_intents,
            decision_summary=legacy.decision_summary,
            impact_policy=legacy.impact_policy,
            collaboration_policy_request=CollaborationPolicyRequestV1.from_mapping(
                policy_value
            ),
        )

    def to_legacy(self) -> PlanningProposalPayload:
        return PlanningProposalPayload(
            schema_version=2,
            kind=self.kind,
            root_task=self.root_task,
            subtasks=self.subtasks,
            worker_items=self.worker_items,
            subtask_milestones=self.subtask_milestones,
            subtask_dependencies=self.subtask_dependencies,
            worker_item_dependencies=self.worker_item_dependencies,
            execution_intents=self.execution_intents,
            decision_summary=self.decision_summary,
            impact_policy=self.impact_policy,
            base_descriptor=None,
        )

    def to_mapping(self) -> dict[str, object]:
        mapping = self.to_legacy().to_mapping()
        mapping.pop("base_descriptor", None)
        mapping["schema_version"] = 4
        mapping["collaboration_policy_request"] = (
            self.collaboration_policy_request.to_mapping()
        )
        return mapping

    def canonical_payload(self) -> dict[str, object]:
        return self.to_mapping()

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanningProposalPayloadV5:
    schema_version: int
    kind: PlanningRequestKind
    root_task: ProposedRootTask
    subtasks: tuple[ProposedSubtask, ...]
    worker_items: tuple[ProposedWorkerItem, ...]
    subtask_milestones: tuple[SubtaskMilestone, ...]
    subtask_dependencies: tuple[SubtaskDependency, ...]
    worker_item_dependencies: tuple[WorkerItemDependency, ...]
    execution_intents: tuple[ExecutionIntent, ...]
    decision_summary: DecisionSummary
    impact_policy: ImpactPolicy
    collaboration_policy_request: CollaborationPolicyRequestV2

    def __post_init__(self) -> None:
        if self.schema_version != 5:
            raise ValidationError("proposal schema_version must be 5")
        object.__setattr__(
            self, "kind", _enum(self.kind, PlanningRequestKind, "proposal kind")
        )
        if self.kind is not PlanningRequestKind.CREATE:
            raise ValidationError("proposal v5 kind must be create")
        for name in (
            "subtasks",
            "worker_items",
            "subtask_milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not isinstance(
            self.collaboration_policy_request, CollaborationPolicyRequestV2
        ):
            raise ValidationError(
                "proposal v5 requires a collaboration policy request v2"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PlanningProposalPayloadV5:
        if not isinstance(value, Mapping):
            raise ValidationError("proposal v5 payload must be a mapping")
        allowed = {
            "schema_version",
            "kind",
            "root_task",
            "subtasks",
            "worker_items",
            "milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "decision_summary",
            "impact_policy",
            "collaboration_policy_request",
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValidationError(f"unknown proposal v5 fields: {', '.join(unknown)}")
        if _int(value, "schema_version") != 5:
            raise ValidationError("proposal schema_version must be 5")
        policy_value = value.get("collaboration_policy_request")
        if not isinstance(policy_value, Mapping):
            raise ValidationError("collaboration_policy_request must be a mapping")
        legacy_mapping = dict(value)
        legacy_mapping.pop("collaboration_policy_request", None)
        legacy_mapping["schema_version"] = 2
        legacy_mapping["base_descriptor"] = None
        legacy = PlanningProposalPayload.from_mapping(legacy_mapping)
        if legacy.kind is not PlanningRequestKind.CREATE:
            raise ValidationError("proposal v5 kind must be create")
        return cls(
            schema_version=5,
            kind=legacy.kind,
            root_task=legacy.root_task,
            subtasks=legacy.subtasks,
            worker_items=legacy.worker_items,
            subtask_milestones=legacy.subtask_milestones,
            subtask_dependencies=legacy.subtask_dependencies,
            worker_item_dependencies=legacy.worker_item_dependencies,
            execution_intents=legacy.execution_intents,
            decision_summary=legacy.decision_summary,
            impact_policy=legacy.impact_policy,
            collaboration_policy_request=CollaborationPolicyRequestV2.from_mapping(
                policy_value
            ),
        )

    def to_legacy(self) -> PlanningProposalPayload:
        return PlanningProposalPayload(
            schema_version=2,
            kind=self.kind,
            root_task=self.root_task,
            subtasks=self.subtasks,
            worker_items=self.worker_items,
            subtask_milestones=self.subtask_milestones,
            subtask_dependencies=self.subtask_dependencies,
            worker_item_dependencies=self.worker_item_dependencies,
            execution_intents=self.execution_intents,
            decision_summary=self.decision_summary,
            impact_policy=self.impact_policy,
            base_descriptor=None,
        )

    def to_mapping(self) -> dict[str, object]:
        mapping = self.to_legacy().to_mapping()
        mapping.pop("base_descriptor", None)
        mapping["schema_version"] = 5
        mapping["collaboration_policy_request"] = (
            self.collaboration_policy_request.to_mapping()
        )
        return mapping

    def canonical_payload(self) -> dict[str, object]:
        return self.to_mapping()

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanningProposalPayloadV6:
    schema_version: int
    kind: PlanningRequestKind
    root_task: ProposedRootTask
    subtasks: tuple[ProposedSubtask, ...]
    worker_items: tuple[ProposedWorkerItem, ...]
    subtask_milestones: tuple[SubtaskMilestone, ...]
    subtask_dependencies: tuple[SubtaskDependency, ...]
    worker_item_dependencies: tuple[WorkerItemDependency, ...]
    execution_intents: tuple[ExecutionIntent, ...]
    decision_summary: DecisionSummary
    impact_policy: ImpactPolicy
    collaboration_policy_request: CollaborationPolicyRequestV2
    integration_spec: IntegrationSpecV1

    def __post_init__(self) -> None:
        if self.schema_version != 6:
            raise ValidationError("proposal schema_version must be 6")
        object.__setattr__(
            self, "kind", _enum(self.kind, PlanningRequestKind, "proposal kind")
        )
        if self.kind is not PlanningRequestKind.CREATE:
            raise ValidationError("proposal v6 kind must be create")
        for name in (
            "subtasks",
            "worker_items",
            "subtask_milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not isinstance(
            self.collaboration_policy_request, CollaborationPolicyRequestV2
        ):
            raise ValidationError(
                "proposal v6 requires a collaboration policy request v2"
            )
        if not isinstance(self.integration_spec, IntegrationSpecV1):
            raise ValidationError("proposal v6 requires an integration spec")
        validate_integration_spec(
            self.integration_spec,
            frozenset(item.key for item in self.worker_items),
            frozenset(
                group.group_key
                for group in self.collaboration_policy_request.input_groups
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PlanningProposalPayloadV6:
        if not isinstance(value, Mapping):
            raise ValidationError("proposal v6 payload must be a mapping")
        allowed = {
            "schema_version",
            "kind",
            "root_task",
            "subtasks",
            "worker_items",
            "milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "decision_summary",
            "impact_policy",
            "collaboration_policy_request",
            "integration_spec",
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValidationError(f"unknown proposal v6 fields: {', '.join(unknown)}")
        if _int(value, "schema_version") != 6:
            raise ValidationError("proposal schema_version must be 6")
        policy_value = value.get("collaboration_policy_request")
        if not isinstance(policy_value, Mapping):
            raise ValidationError("collaboration_policy_request must be a mapping")
        integration_value = value.get("integration_spec")
        if not isinstance(integration_value, Mapping):
            raise ValidationError("integration_spec must be a mapping")
        legacy_mapping = dict(value)
        legacy_mapping.pop("collaboration_policy_request", None)
        legacy_mapping.pop("integration_spec", None)
        legacy_mapping["schema_version"] = 2
        legacy_mapping["base_descriptor"] = None
        legacy = PlanningProposalPayload.from_mapping(legacy_mapping)
        return cls(
            schema_version=6,
            kind=legacy.kind,
            root_task=legacy.root_task,
            subtasks=legacy.subtasks,
            worker_items=legacy.worker_items,
            subtask_milestones=legacy.subtask_milestones,
            subtask_dependencies=legacy.subtask_dependencies,
            worker_item_dependencies=legacy.worker_item_dependencies,
            execution_intents=legacy.execution_intents,
            decision_summary=legacy.decision_summary,
            impact_policy=legacy.impact_policy,
            collaboration_policy_request=CollaborationPolicyRequestV2.from_mapping(
                policy_value
            ),
            integration_spec=IntegrationSpecV1.from_mapping(integration_value),
        )

    def to_legacy(self) -> PlanningProposalPayload:
        return PlanningProposalPayload(
            schema_version=2,
            kind=self.kind,
            root_task=self.root_task,
            subtasks=self.subtasks,
            worker_items=self.worker_items,
            subtask_milestones=self.subtask_milestones,
            subtask_dependencies=self.subtask_dependencies,
            worker_item_dependencies=self.worker_item_dependencies,
            execution_intents=self.execution_intents,
            decision_summary=self.decision_summary,
            impact_policy=self.impact_policy,
            base_descriptor=None,
        )

    def to_mapping(self) -> dict[str, object]:
        mapping = self.to_legacy().to_mapping()
        mapping.pop("base_descriptor", None)
        mapping["schema_version"] = 6
        mapping["collaboration_policy_request"] = (
            self.collaboration_policy_request.to_mapping()
        )
        mapping["integration_spec"] = self.integration_spec.to_mapping()
        return mapping

    def canonical_payload(self) -> dict[str, object]:
        return self.to_mapping()

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProposalRevision:
    planning_request_id: UUID
    revision: int
    proposal_schema_version: int
    brief_revision: int
    planning_run_id: UUID
    kind: PlanningRequestKind
    profile_descriptor: PlanningProfileDescriptor
    payload: (
        PlanningProposalPayload
        | PlanningProposalPayloadV4
        | PlanningProposalPayloadV5
        | PlanningProposalPayloadV6
        | PlanningRevisionProposalPayloadV3
        | PlanningRevisionProposalPayloadV4
        | PlanningRevisionProposalPayloadV5
        | PlanningRevisionProposalPayloadV6
    )
    derived_diff: ProposalDiff | NormalizedRevisionDiff
    canonical_payload: bytes
    content_hash: str
    created_at: datetime
    id: UUID = field(default_factory=uuid4)

    @classmethod
    def create(
        cls,
        *,
        planning_request_id: UUID,
        revision: int,
        brief_revision: int,
        planning_run_id: UUID,
        kind: PlanningRequestKind,
        payload: (
            PlanningProposalPayload
            | PlanningProposalPayloadV4
            | PlanningProposalPayloadV5
            | PlanningProposalPayloadV6
            | PlanningRevisionProposalPayloadV3
            | PlanningRevisionProposalPayloadV4
            | PlanningRevisionProposalPayloadV5
            | PlanningRevisionProposalPayloadV6
        ),
        profile_descriptor: PlanningProfileDescriptor,
        profile_snapshot: PlanningProfileSnapshot,
        created_at: datetime,
        derived_diff: ProposalDiff | NormalizedRevisionDiff | None = None,
    ) -> ProposalRevision:
        if isinstance(
            payload,
            (
                PlanningProposalPayload,
                PlanningProposalPayloadV4,
                PlanningProposalPayloadV5,
                PlanningProposalPayloadV6,
            ),
        ):
            from sidera_control_plane.domain.proposal_validation import (
                validate_proposal,
            )

            validate_proposal(
                payload,
                profile_snapshot=profile_snapshot,
                brief_revision=brief_revision,
            )
            resolved_diff = (
                derived_diff
                if derived_diff is not None
                else derive_proposal_diff(
                    payload.to_legacy()
                    if isinstance(
                        payload,
                        (
                            PlanningProposalPayloadV4,
                            PlanningProposalPayloadV5,
                            PlanningProposalPayloadV6,
                        ),
                    )
                    else payload
                )
            )
        else:
            from sidera_control_plane.domain.planning_revision_proposals import (
                validate_revision_proposal,
            )

            validate_revision_proposal(
                payload,
                profile_snapshot=profile_snapshot,
                brief_revision=brief_revision,
            )
            if derived_diff is None:
                raise ValidationError(
                    "revision proposal requires a normalized revision diff"
                )
            resolved_diff = derived_diff
        if kind is not payload.kind:
            raise ValidationError("proposal kind does not match revision kind")
        canonical = canonical_proposal_bytes(
            payload,
            brief_revision=brief_revision,
            profile_descriptor=profile_descriptor,
        )
        return cls(
            planning_request_id=planning_request_id,
            revision=_positive_value(revision, "proposal revision"),
            proposal_schema_version=payload.schema_version,
            brief_revision=_positive_value(brief_revision, "brief revision"),
            planning_run_id=planning_run_id,
            kind=kind,
            profile_descriptor=profile_descriptor,
            payload=payload,
            derived_diff=resolved_diff,
            canonical_payload=canonical,
            content_hash=hashlib.sha256(canonical).hexdigest(),
            created_at=created_at,
        )


def canonical_proposal_bytes(
    payload: (
        PlanningProposalPayload
        | PlanningProposalPayloadV4
        | PlanningProposalPayloadV5
        | PlanningProposalPayloadV6
        | PlanningRevisionProposalPayloadV3
        | PlanningRevisionProposalPayloadV4
        | PlanningRevisionProposalPayloadV5
        | PlanningRevisionProposalPayloadV6
    ),
    brief_revision: int,
    profile_descriptor: PlanningProfileDescriptor,
) -> bytes:
    if isinstance(
        payload,
        (
            PlanningProposalPayload,
            PlanningProposalPayloadV4,
            PlanningProposalPayloadV5,
            PlanningProposalPayloadV6,
        ),
    ):
        proposal_mapping = payload.to_mapping()
    else:
        from sidera_control_plane.domain.planning_revision_proposals import (
            PlanningRevisionProposalPayloadV3,
            PlanningRevisionProposalPayloadV4,
            PlanningRevisionProposalPayloadV5,
            PlanningRevisionProposalPayloadV6,
        )

        if not isinstance(
            payload,
            (
                PlanningRevisionProposalPayloadV3,
                PlanningRevisionProposalPayloadV4,
                PlanningRevisionProposalPayloadV5,
                PlanningRevisionProposalPayloadV6,
            ),
        ):
            raise ValidationError(
                "canonical proposal serializer requires typed payload"
            )
        proposal_mapping = payload.canonical_payload()
    _positive_int(brief_revision, "brief_revision")
    document = {
        "brief_revision": brief_revision,
        "profile_descriptor": profile_descriptor.to_mapping(),
        "proposal": proposal_mapping,
    }
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"proposal canonicalization failed: {exc}") from exc


def proposal_content_hash(
    payload: (
        PlanningProposalPayload
        | PlanningProposalPayloadV4
        | PlanningProposalPayloadV5
        | PlanningProposalPayloadV6
        | PlanningRevisionProposalPayloadV3
        | PlanningRevisionProposalPayloadV4
        | PlanningRevisionProposalPayloadV5
        | PlanningRevisionProposalPayloadV6
    ),
    *,
    brief_revision: int,
    profile_descriptor: PlanningProfileDescriptor,
) -> str:
    return hashlib.sha256(
        canonical_proposal_bytes(
            payload,
            brief_revision=brief_revision,
            profile_descriptor=profile_descriptor,
        )
    ).hexdigest()


def derive_proposal_diff(payload: PlanningProposalPayload) -> ProposalDiff:
    worker_keys = tuple(item.key for item in payload.worker_items)
    hard_upstreams: dict[str, set[str]] = {key: set() for key in worker_keys}
    for dependency in payload.worker_item_dependencies:
        if dependency.kind == DependencyKind.HARD.value:
            hard_upstreams[dependency.downstream_worker_item_key].add(
                dependency.upstream_worker_item_key
            )
    hard_subtask_upstreams: dict[str, set[str]] = {
        item.key: set() for item in payload.subtasks
    }
    for dependency in payload.subtask_dependencies:
        if dependency.kind is DependencyKind.HARD:
            hard_subtask_upstreams[dependency.downstream_subtask_key].add(
                dependency.upstream_subtask_key
            )
    worker_to_subtask = {item.key: item.subtask_key for item in payload.worker_items}
    ready = tuple(
        key
        for key in worker_keys
        if not hard_upstreams[key]
        and not hard_subtask_upstreams[worker_to_subtask[key]]
    )
    blocked = tuple(key for key in worker_keys if key not in ready)
    return ProposalDiff(
        root_task_added=True,
        added_subtask_keys=tuple(item.key for item in payload.subtasks),
        added_worker_item_keys=worker_keys,
        added_subtask_dependency_edges=tuple(
            (item.upstream_subtask_key, item.downstream_subtask_key)
            for item in payload.subtask_dependencies
        ),
        added_worker_item_dependency_edges=tuple(
            (
                item.upstream_worker_item_key,
                item.downstream_worker_item_key,
                item.kind,
            )
            for item in payload.worker_item_dependencies
        ),
        added_execution_intent_keys=tuple(
            item.worker_item_key for item in payload.execution_intents
        ),
        immediately_ready_worker_item_keys=ready,
        blocked_worker_item_keys=blocked,
    )


def _summary_mapping(value: DecisionSummary) -> dict[str, object]:
    return {
        "understanding": value.understanding,
        "assumptions": list(value.assumptions),
        "risks": list(value.risks),
        "estimated_cost_microunits": value.estimated_cost_microunits,
        "estimated_tokens": value.estimated_tokens,
        "estimated_runtime_seconds": value.estimated_runtime_seconds,
        "estimated_concurrency": value.estimated_concurrency,
        "scope_includes": list(value.scope_includes),
        "scope_excludes": list(value.scope_excludes),
        "metadata": _json_value(value.metadata),
    }


def _subtask_mapping(value: ProposedSubtask) -> dict[str, object]:
    return {
        "key": value.key,
        "kind": value.kind.value,
        "title": value.title,
        "goal_summary": value.goal_summary,
        "acceptance_refs": list(value.acceptance_refs),
        "expected_outputs": list(value.expected_outputs),
        "milestone_key": value.milestone_key,
        "source_refs": list(value.source_refs),
    }


def _worker_item_mapping(value: ProposedWorkerItem) -> dict[str, object]:
    return {
        "key": value.key,
        "subtask_key": value.subtask_key,
        "title": value.title,
        "role": value.role,
        "responsibility": value.responsibility,
        "capability_requirements": list(value.capability_requirements),
        "worker_policy_ref": value.worker_policy_ref,
        "configuration_ref": value.configuration_ref,
        "concurrency_limit": value.concurrency_limit,
        "source_refs": list(value.source_refs),
        "pinned_worker_id": value.pinned_worker_id,
    }


def _milestone_mapping(value: SubtaskMilestone) -> dict[str, object]:
    return {
        "key": value.key,
        "title": value.title,
        "subtask_keys": list(value.subtask_keys),
        "acceptance_refs": list(value.acceptance_refs),
        "order_hint": value.order_hint,
    }


def _subtask_dependency_mapping(value: SubtaskDependency) -> dict[str, object]:
    return {
        "upstream_subtask_key": value.upstream_subtask_key,
        "downstream_subtask_key": value.downstream_subtask_key,
        "kind": value.kind.value,
        "required_evidence_refs": list(value.required_evidence_refs),
        "enforcement": value.enforcement.value,
    }


def _worker_dependency_mapping(value: WorkerItemDependency) -> dict[str, object]:
    return {
        "upstream_worker_item_key": value.upstream_worker_item_key,
        "downstream_worker_item_key": value.downstream_worker_item_key,
        "kind": value.kind,
        "required_artifact_refs": list(value.required_artifact_refs),
    }


def _intent_mapping(value: ExecutionIntent) -> dict[str, object]:
    return {
        "worker_item_key": value.worker_item_key,
        "purpose": value.purpose,
        "context_refs": list(value.context_refs),
        "expected_artifact_refs": list(value.expected_artifact_refs),
        "required_evidence_refs": list(value.required_evidence_refs),
        "capability_requirements": list(value.capability_requirements),
        "adapter_preferences": list(value.adapter_preferences),
        "budget_class": value.budget_class,
        "deadline_seconds": value.deadline_seconds,
        "not_before_seconds": value.not_before_seconds,
        "max_attempts": value.max_attempts,
        "activation_dependencies": [
            {
                "upstream_worker_item_key": item.upstream_worker_item_key,
                "downstream_worker_item_key": item.downstream_worker_item_key,
                "kind": item.kind,
                "required_artifact_refs": list(item.required_artifact_refs),
            }
            for item in value.activation_dependencies
        ],
    }


def _impact_mapping(value: ImpactPolicy) -> dict[str, object]:
    return {
        "activate_immediately": value.activate_immediately,
        "budget_level": value.budget_level,
        "failure_policy": value.failure_policy,
        "manual_gate": value.manual_gate,
        "revision_operation": value.revision_operation,
    }


def _subtask_from_mapping(value: Mapping[str, object]) -> ProposedSubtask:
    _reject_nested(
        value,
        {
            "key",
            "kind",
            "title",
            "goal_summary",
            "acceptance_refs",
            "expected_outputs",
            "milestone_key",
            "source_refs",
        },
        "subtask",
    )
    return ProposedSubtask(
        key=_string(value, "key"),
        kind=WorkItemKind(_string(value, "kind")),
        title=_string(value, "title"),
        goal_summary=_string(value, "goal_summary"),
        acceptance_refs=_strings(value, "acceptance_refs"),
        expected_outputs=_strings(value, "expected_outputs"),
        milestone_key=(
            str(value["milestone_key"])
            if value.get("milestone_key") is not None
            else None
        ),
        source_refs=_strings(value, "source_refs"),
    )


def _worker_from_mapping(value: Mapping[str, object]) -> ProposedWorkerItem:
    _reject_nested(
        value,
        {
            "key",
            "subtask_key",
            "title",
            "role",
            "responsibility",
            "capability_requirements",
            "worker_policy_ref",
            "configuration_ref",
            "concurrency_limit",
            "source_refs",
            "pinned_worker_id",
        },
        "worker item",
    )
    return ProposedWorkerItem(
        key=_string(value, "key"),
        subtask_key=_string(value, "subtask_key"),
        title=_string(value, "title"),
        role=_string(value, "role"),
        responsibility=_string(value, "responsibility"),
        capability_requirements=_strings(value, "capability_requirements"),
        worker_policy_ref=(
            str(value["worker_policy_ref"])
            if value.get("worker_policy_ref") is not None
            else None
        ),
        configuration_ref=(
            str(value["configuration_ref"])
            if value.get("configuration_ref") is not None
            else None
        ),
        concurrency_limit=_optional_int(value, "concurrency_limit"),
        source_refs=_strings(value, "source_refs"),
        pinned_worker_id=(
            str(value["pinned_worker_id"])
            if value.get("pinned_worker_id") is not None
            else None
        ),
    )


def _milestone_from_mapping(value: Mapping[str, object]) -> SubtaskMilestone:
    _reject_nested(
        value,
        {"key", "title", "subtask_keys", "acceptance_refs", "order_hint"},
        "milestone",
    )
    return SubtaskMilestone(
        key=_string(value, "key"),
        title=_string(value, "title"),
        subtask_keys=_strings(value, "subtask_keys"),
        acceptance_refs=_strings(value, "acceptance_refs"),
        order_hint=_int(value, "order_hint"),
    )


def _subtask_dependency_from_mapping(value: Mapping[str, object]) -> SubtaskDependency:
    _reject_nested(
        value,
        {
            "upstream_subtask_key",
            "downstream_subtask_key",
            "kind",
            "required_evidence_refs",
            "enforcement",
        },
        "subtask dependency",
    )
    return SubtaskDependency(
        upstream_subtask_key=_string(value, "upstream_subtask_key"),
        downstream_subtask_key=_string(value, "downstream_subtask_key"),
        kind=DependencyKind(_string(value, "kind")),
        required_evidence_refs=_strings(value, "required_evidence_refs"),
        enforcement=DependencyEnforcement(_string(value, "enforcement")),
    )


def _worker_dependency_from_mapping(
    value: Mapping[str, object],
) -> WorkerItemDependency:
    _reject_nested(
        value,
        {
            "upstream_worker_item_key",
            "downstream_worker_item_key",
            "kind",
            "required_artifact_refs",
        },
        "worker dependency",
    )
    return WorkerItemDependency(
        upstream_worker_item_key=_string(value, "upstream_worker_item_key"),
        downstream_worker_item_key=_string(value, "downstream_worker_item_key"),
        kind=_string(value, "kind"),
        required_artifact_refs=_strings(value, "required_artifact_refs"),
    )


def _intent_from_mapping(value: Mapping[str, object]) -> ExecutionIntent:
    _reject_nested(
        value,
        {
            "worker_item_key",
            "purpose",
            "context_refs",
            "expected_artifact_refs",
            "required_evidence_refs",
            "capability_requirements",
            "adapter_preferences",
            "budget_class",
            "deadline_seconds",
            "not_before_seconds",
            "max_attempts",
            "activation_dependencies",
        },
        "execution intent",
    )
    dependencies = []
    for item in _mappings(value, "activation_dependencies"):
        _reject_nested(
            item,
            {
                "upstream_worker_item_key",
                "downstream_worker_item_key",
                "kind",
                "required_artifact_refs",
            },
            "activation dependency",
        )
        dependencies.append(
            ActivationDependency(
                upstream_worker_item_key=_string(item, "upstream_worker_item_key"),
                downstream_worker_item_key=_string(item, "downstream_worker_item_key"),
                kind=_string(item, "kind"),
                required_artifact_refs=_strings(item, "required_artifact_refs"),
            )
        )
    return ExecutionIntent(
        worker_item_key=_string(value, "worker_item_key"),
        purpose=_string(value, "purpose"),
        context_refs=_strings(value, "context_refs"),
        expected_artifact_refs=_strings(value, "expected_artifact_refs"),
        required_evidence_refs=_strings(value, "required_evidence_refs"),
        capability_requirements=_strings(value, "capability_requirements"),
        adapter_preferences=_strings(value, "adapter_preferences"),
        budget_class=_string(value, "budget_class"),
        deadline_seconds=_optional_int(value, "deadline_seconds"),
        not_before_seconds=_optional_int(value, "not_before_seconds"),
        max_attempts=_int(value, "max_attempts"),
        activation_dependencies=tuple(dependencies),
    )


def _mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    item = value.get(name)
    if not isinstance(item, Mapping):
        raise ValidationError(f"proposal field {name!r} must be a mapping")
    return item


def _mappings(
    value: Mapping[str, object], name: str
) -> tuple[Mapping[str, object], ...]:
    item = value.get(name)
    if not isinstance(item, (list, tuple)):
        raise ValidationError(f"proposal field {name!r} must be a list")
    result = []
    for entry in item:
        if not isinstance(entry, Mapping):
            raise ValidationError(f"proposal field {name!r} contains a non-mapping")
        result.append(entry)
    return tuple(result)


def _string(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str):
        raise ValidationError(f"proposal field {name!r} must be a string")
    return item


def _strings(value: Mapping[str, object], name: str) -> tuple[str, ...]:
    item = value.get(name)
    if not isinstance(item, (list, tuple)):
        raise ValidationError(f"proposal field {name!r} must be a list")
    if any(not isinstance(entry, str) for entry in item):
        raise ValidationError(f"proposal field {name!r} must contain strings")
    return tuple(item)


def _int(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValidationError(f"proposal field {name!r} must be an integer")
    return item


def _optional_int(value: Mapping[str, object], name: str) -> int | None:
    item = value.get(name)
    if item is None:
        return None
    return _int(value, name)


def _bool(value: Mapping[str, object], name: str) -> bool:
    item = value.get(name)
    if not isinstance(item, bool):
        raise ValidationError(f"proposal field {name!r} must be a boolean")
    return item


def _reject_nested(value: Mapping[str, object], allowed: set[str], name: str) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValidationError(f"unknown {name} fields: {', '.join(unknown)}")


def _text_tuple(
    values: Sequence[str], name: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValidationError(f"{name} must be a sequence of strings")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _required_text(value, name)
        if item not in seen:
            seen.add(item)
            result.append(item)
    if not allow_empty and not result:
        raise ValidationError(f"{name} cannot be empty")
    return tuple(result)


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    return value.strip()


def _optional_text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _key(value: str, name: str) -> str:
    return _required_text(value, name)


def _positive_int(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _positive_value(value: int, name: str) -> int:
    return _positive_int(value, name)


def _non_negative_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer")


def _enum(value: object, enum_type: type[StrEnum], name: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {name}: {value!r}") from exc


def _freeze_mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    return MappingProxyType(
        {
            key: _freeze(item, f"{name}.{key}")
            for key, item in value.items()
            if _mapping_key(key, name)
        }
    )


def _freeze(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value, name)
    if isinstance(value, list):
        return tuple(_freeze(item, name) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item, name) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item, name) for item in value)
    if isinstance(value, float):
        raise ValidationError(f"{name} cannot contain float values")
    return value


def _mapping_key(value: object, name: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} keys must be non-empty strings")
    return True


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(
            (_json_value(item) for item in value),
            key=lambda item: repr(item),
        )
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise ValidationError(
        f"unsupported canonical proposal value: {type(value).__name__}"
    )
