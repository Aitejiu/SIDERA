from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyRequestV1,
    CollaborationPolicyRequestV2,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.plan_revisions import (
    ContextDisposition,
    IdentityStrategy,
    RevisionBaseDescriptorV1,
    SubtaskIdentityDecision,
    WorkerItemIdentityDecision,
    plan_snapshot_content_hash,
    validate_subtask_identity_decisions,
    validate_worker_item_identity_decisions,
)
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_proposals import (
    ActivationDependency,
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
from sidera_control_plane.domain.plans import (
    DependencyKind,
    SubtaskDependency,
    SubtaskMilestone,
    TaskPlanSnapshot,
    WorkerItemDependency,
    WorkItemKind,
)
from sidera_control_plane.domain.result_inputs import (
    InputGroupInvalid,
    InputGroupV1,
    InputWaitCycle,
    validate_input_wait_graph,
)


@dataclass(frozen=True, slots=True)
class PlanningRevisionProposalPayloadV3:
    schema_version: int
    kind: PlanningRequestKind
    base_descriptor: RevisionBaseDescriptorV1
    root_task: ProposedRootTask
    subtasks: tuple[ProposedSubtask, ...]
    worker_items: tuple[ProposedWorkerItem, ...]
    subtask_milestones: tuple[SubtaskMilestone, ...]
    subtask_dependencies: tuple[SubtaskDependency, ...]
    worker_item_dependencies: tuple[WorkerItemDependency, ...]
    execution_intents: tuple[ExecutionIntent, ...]
    subtask_identity_decisions: tuple[SubtaskIdentityDecision, ...]
    worker_item_identity_decisions: tuple[WorkerItemIdentityDecision, ...]
    retired_subtask_ids: tuple[UUID, ...]
    retired_worker_item_ids: tuple[UUID, ...]
    decision_summary: DecisionSummary
    impact_policy: ImpactPolicy

    def __post_init__(self) -> None:
        if self.schema_version != 3:
            raise ValidationError("revision proposal schema_version must be 3")
        if self.kind is not PlanningRequestKind.REVISION:
            raise ValidationError("revision proposal kind must be revision")
        if not isinstance(self.base_descriptor, RevisionBaseDescriptorV1):
            raise ValidationError("revision proposal requires an exact base descriptor")
        for name in (
            "subtasks",
            "worker_items",
            "subtask_milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "subtask_identity_decisions",
            "worker_item_identity_decisions",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(
            self,
            "retired_subtask_ids",
            _unique_uuids(self.retired_subtask_ids, "retired_subtask_ids"),
        )
        object.__setattr__(
            self,
            "retired_worker_item_ids",
            _unique_uuids(self.retired_worker_item_ids, "retired_worker_item_ids"),
        )
        self.validate()

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": 3,
            "kind": PlanningRequestKind.REVISION.value,
            "base_descriptor": self.base_descriptor.to_mapping(),
            "root_task": {
                "title": self.root_task.title,
                "goal_summary": self.root_task.goal_summary,
                "acceptance_refs": list(self.root_task.acceptance_refs),
            },
            "subtasks": [_subtask_mapping(item) for item in self.subtasks],
            "worker_items": [_worker_mapping(item) for item in self.worker_items],
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
            "subtask_identity_decisions": [
                _subtask_identity_mapping(item)
                for item in self.subtask_identity_decisions
            ],
            "worker_item_identity_decisions": [
                _worker_identity_mapping(item)
                for item in self.worker_item_identity_decisions
            ],
            "retired_subtask_ids": [str(item) for item in self.retired_subtask_ids],
            "retired_worker_item_ids": [
                str(item) for item in self.retired_worker_item_ids
            ],
            "decision_summary": _summary_mapping(self.decision_summary),
            "impact_policy": _impact_mapping(self.impact_policy),
        }

    def to_mapping(self) -> dict[str, object]:
        return self.canonical_payload()

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> PlanningRevisionProposalPayloadV3:
        if not isinstance(value, Mapping):
            raise ValidationError("revision proposal payload must be a mapping")
        allowed = {
            "schema_version",
            "kind",
            "base_descriptor",
            "root_task",
            "subtasks",
            "worker_items",
            "milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "subtask_identity_decisions",
            "worker_item_identity_decisions",
            "retired_subtask_ids",
            "retired_worker_item_ids",
            "decision_summary",
            "impact_policy",
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValidationError(
                f"unknown revision proposal fields: {', '.join(unknown)}"
            )
        if _int(value, "schema_version") != 3:
            raise ValidationError("revision proposal schema_version must be 3")
        if _string(value, "kind") != PlanningRequestKind.REVISION.value:
            raise ValidationError("revision proposal kind must be revision")
        try:
            return cls(
                schema_version=3,
                kind=PlanningRequestKind.REVISION,
                base_descriptor=_base_descriptor(
                    _mapping(value.get("base_descriptor"), "base_descriptor")
                ),
                root_task=_root_task(_mapping(value.get("root_task"), "root_task")),
                subtasks=tuple(
                    _subtask(_mapping(item, "subtask"))
                    for item in _sequence(value, "subtasks")
                ),
                worker_items=tuple(
                    _worker(_mapping(item, "worker item"))
                    for item in _sequence(value, "worker_items")
                ),
                subtask_milestones=tuple(
                    _milestone(_mapping(item, "milestone"))
                    for item in _sequence(value, "milestones")
                ),
                subtask_dependencies=tuple(
                    _subtask_dependency(_mapping(item, "subtask dependency"))
                    for item in _sequence(value, "subtask_dependencies")
                ),
                worker_item_dependencies=tuple(
                    _worker_dependency(_mapping(item, "worker item dependency"))
                    for item in _sequence(value, "worker_item_dependencies")
                ),
                execution_intents=tuple(
                    _intent(_mapping(item, "execution intent"))
                    for item in _sequence(value, "execution_intents")
                ),
                subtask_identity_decisions=tuple(
                    _subtask_identity(_mapping(item, "subtask identity decision"))
                    for item in _sequence(value, "subtask_identity_decisions")
                ),
                worker_item_identity_decisions=tuple(
                    _worker_identity(_mapping(item, "WorkerItem identity decision"))
                    for item in _sequence(value, "worker_item_identity_decisions")
                ),
                retired_subtask_ids=tuple(
                    _uuid(item, "retired_subtask_ids")
                    for item in _sequence(value, "retired_subtask_ids")
                ),
                retired_worker_item_ids=tuple(
                    _uuid(item, "retired_worker_item_ids")
                    for item in _sequence(value, "retired_worker_item_ids")
                ),
                decision_summary=_summary(
                    _mapping(value.get("decision_summary"), "decision_summary")
                ),
                impact_policy=_impact(
                    _mapping(value.get("impact_policy"), "impact_policy")
                ),
            )
        except ValidationError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise ValidationError(f"invalid revision proposal payload: {exc}") from exc

    def validate(self) -> None:
        subtask_keys = _unique_keys((item.key for item in self.subtasks), "subtask")
        worker_keys = _unique_keys(
            (item.key for item in self.worker_items), "WorkerItem"
        )
        if not subtask_keys:
            raise ValidationError("revision proposal must contain at least one subtask")
        if not worker_keys:
            raise ValidationError(
                "revision proposal must contain at least one WorkerItem"
            )
        if {item.target_subtask_key for item in self.subtask_identity_decisions} != set(
            subtask_keys
        ):
            raise ValidationError("subtask identity decisions must cover every target")
        if {
            item.target_worker_item_key for item in self.worker_item_identity_decisions
        } != set(worker_keys):
            raise ValidationError(
                "WorkerItem identity decisions must cover every target"
            )
        validate_subtask_identity_decisions(self.subtask_identity_decisions)
        validate_worker_item_identity_decisions(
            self.worker_item_identity_decisions,
            retired_worker_item_ids=self.retired_worker_item_ids,
        )
        workers_by_subtask = {key: 0 for key in subtask_keys}
        for item in self.worker_items:
            if item.subtask_key not in workers_by_subtask:
                raise ValidationError("WorkerItem references an unknown subtask")
            workers_by_subtask[item.subtask_key] += 1
        if any(count == 0 for count in workers_by_subtask.values()):
            raise ValidationError("every target subtask needs a WorkerItem")
        milestone_keys = _unique_keys(
            (item.key for item in self.subtask_milestones), "milestone"
        )
        for item in self.subtasks:
            if item.milestone_key and item.milestone_key not in milestone_keys:
                raise ValidationError("subtask milestone reference is unknown")
        for milestone in self.subtask_milestones:
            if not set(milestone.subtask_keys).issubset(set(subtask_keys)):
                raise ValidationError("milestone references an unknown subtask")
        _assert_acyclic(
            subtask_keys,
            tuple(
                (item.upstream_subtask_key, item.downstream_subtask_key)
                for item in self.subtask_dependencies
                if item.kind is DependencyKind.HARD
            ),
            "subtask dependency graph",
        )
        _assert_acyclic(
            worker_keys,
            tuple(
                (item.upstream_worker_item_key, item.downstream_worker_item_key)
                for item in self.worker_item_dependencies
                if item.kind == DependencyKind.HARD.value
            ),
            "WorkerItem dependency graph",
        )
        for item in self.subtask_dependencies:
            _assert_edge(
                item.upstream_subtask_key,
                item.downstream_subtask_key,
                set(subtask_keys),
                "subtask dependency",
            )
        for item in self.worker_item_dependencies:
            _assert_edge(
                item.upstream_worker_item_key,
                item.downstream_worker_item_key,
                set(worker_keys),
                "WorkerItem dependency",
            )
        intent_keys = tuple(item.worker_item_key for item in self.execution_intents)
        if set(intent_keys) != set(worker_keys) or len(intent_keys) != len(worker_keys):
            raise ValidationError(
                "every target WorkerItem must have exactly one execution intent"
            )
        workers = {item.key: item for item in self.worker_items}
        for intent in self.execution_intents:
            if set(intent.capability_requirements) != set(
                workers[intent.worker_item_key].capability_requirements
            ):
                raise ValidationError("execution intent capabilities do not match")

    def validate_against_base(self, base_snapshot: TaskPlanSnapshot) -> None:
        self.validate()
        descriptor = self.base_descriptor
        if descriptor.workspace_id != base_snapshot.workspace_id:
            raise ValidationError("revision base workspace does not match snapshot")
        if descriptor.root_task_id != base_snapshot.root_task_id:
            raise ValidationError("revision base root task does not match snapshot")
        if descriptor.plan_snapshot_id != base_snapshot.id:
            raise ValidationError("revision base snapshot does not match snapshot")
        if descriptor.plan_generation != base_snapshot.generation:
            raise ValidationError("revision base generation does not match snapshot")
        if descriptor.plan_content_hash != plan_snapshot_content_hash(base_snapshot):
            raise ValidationError("revision base content hash does not match snapshot")

        existing_subtasks = {
            item.task_id for item in base_snapshot.subtasks if item.task_id is not None
        }
        existing_workers = {
            item.worker_item_id
            for item in base_snapshot.worker_item_plans
            if item.worker_item_id is not None
        }
        reused_subtasks = {
            item.existing_subtask_task_id
            for item in self.subtask_identity_decisions
            if item.existing_subtask_task_id is not None
        }
        reused_workers = {
            item.existing_worker_item_id
            for item in self.worker_item_identity_decisions
            if item.existing_worker_item_id is not None
        }
        _assert_identity_cover(
            existing_subtasks,
            reused_subtasks,
            set(self.retired_subtask_ids),
            "subtask",
        )
        _assert_identity_cover(
            existing_workers,
            reused_workers,
            set(self.retired_worker_item_ids),
            "WorkerItem",
        )
        for decision in self.subtask_identity_decisions:
            if not set(decision.derived_from_subtask_task_ids).issubset(
                existing_subtasks
            ):
                raise ValidationError("derived subtask source is outside the base")
        for decision in self.worker_item_identity_decisions:
            if not set(decision.derived_from_worker_item_ids).issubset(
                existing_workers
            ):
                raise ValidationError("derived WorkerItem source is outside the base")


@dataclass(frozen=True, slots=True)
class PlanningRevisionProposalPayloadV4:
    schema_version: int
    kind: PlanningRequestKind
    base_descriptor: RevisionBaseDescriptorV1
    root_task: ProposedRootTask
    subtasks: tuple[ProposedSubtask, ...]
    worker_items: tuple[ProposedWorkerItem, ...]
    subtask_milestones: tuple[SubtaskMilestone, ...]
    subtask_dependencies: tuple[SubtaskDependency, ...]
    worker_item_dependencies: tuple[WorkerItemDependency, ...]
    execution_intents: tuple[ExecutionIntent, ...]
    subtask_identity_decisions: tuple[SubtaskIdentityDecision, ...]
    worker_item_identity_decisions: tuple[WorkerItemIdentityDecision, ...]
    retired_subtask_ids: tuple[UUID, ...]
    retired_worker_item_ids: tuple[UUID, ...]
    decision_summary: DecisionSummary
    impact_policy: ImpactPolicy
    collaboration_policy_request: CollaborationPolicyRequestV1

    def __post_init__(self) -> None:
        if self.schema_version != 4:
            raise ValidationError("revision proposal schema_version must be 4")
        if self.kind is not PlanningRequestKind.REVISION:
            raise ValidationError("revision proposal kind must be revision")
        if not isinstance(self.base_descriptor, RevisionBaseDescriptorV1):
            raise ValidationError("revision proposal requires an exact base descriptor")
        for name in (
            "subtasks",
            "worker_items",
            "subtask_milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "subtask_identity_decisions",
            "worker_item_identity_decisions",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(
            self,
            "retired_subtask_ids",
            _unique_uuids(self.retired_subtask_ids, "retired_subtask_ids"),
        )
        object.__setattr__(
            self,
            "retired_worker_item_ids",
            _unique_uuids(self.retired_worker_item_ids, "retired_worker_item_ids"),
        )
        if not isinstance(
            self.collaboration_policy_request, CollaborationPolicyRequestV1
        ):
            raise ValidationError(
                "revision proposal v4 requires a collaboration policy request"
            )
        self.validate()

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> PlanningRevisionProposalPayloadV4:
        if not isinstance(value, Mapping):
            raise ValidationError("revision proposal v4 payload must be a mapping")
        allowed = {
            "schema_version",
            "kind",
            "base_descriptor",
            "root_task",
            "subtasks",
            "worker_items",
            "milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "subtask_identity_decisions",
            "worker_item_identity_decisions",
            "retired_subtask_ids",
            "retired_worker_item_ids",
            "decision_summary",
            "impact_policy",
            "collaboration_policy_request",
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValidationError(
                f"unknown revision proposal v4 fields: {', '.join(unknown)}"
            )
        if _int(value, "schema_version") != 4:
            raise ValidationError("revision proposal schema_version must be 4")
        policy_value = value.get("collaboration_policy_request")
        if not isinstance(policy_value, Mapping):
            raise ValidationError("collaboration_policy_request must be a mapping")
        legacy_mapping = dict(value)
        legacy_mapping.pop("collaboration_policy_request", None)
        legacy_mapping["schema_version"] = 3
        legacy = PlanningRevisionProposalPayloadV3.from_mapping(legacy_mapping)
        return cls(
            schema_version=4,
            kind=legacy.kind,
            base_descriptor=legacy.base_descriptor,
            root_task=legacy.root_task,
            subtasks=legacy.subtasks,
            worker_items=legacy.worker_items,
            subtask_milestones=legacy.subtask_milestones,
            subtask_dependencies=legacy.subtask_dependencies,
            worker_item_dependencies=legacy.worker_item_dependencies,
            execution_intents=legacy.execution_intents,
            subtask_identity_decisions=legacy.subtask_identity_decisions,
            worker_item_identity_decisions=legacy.worker_item_identity_decisions,
            retired_subtask_ids=legacy.retired_subtask_ids,
            retired_worker_item_ids=legacy.retired_worker_item_ids,
            decision_summary=legacy.decision_summary,
            impact_policy=legacy.impact_policy,
            collaboration_policy_request=CollaborationPolicyRequestV1.from_mapping(
                policy_value
            ),
        )

    def as_v3(self) -> PlanningRevisionProposalPayloadV3:
        return PlanningRevisionProposalPayloadV3(
            schema_version=3,
            kind=self.kind,
            base_descriptor=self.base_descriptor,
            root_task=self.root_task,
            subtasks=self.subtasks,
            worker_items=self.worker_items,
            subtask_milestones=self.subtask_milestones,
            subtask_dependencies=self.subtask_dependencies,
            worker_item_dependencies=self.worker_item_dependencies,
            execution_intents=self.execution_intents,
            subtask_identity_decisions=self.subtask_identity_decisions,
            worker_item_identity_decisions=self.worker_item_identity_decisions,
            retired_subtask_ids=self.retired_subtask_ids,
            retired_worker_item_ids=self.retired_worker_item_ids,
            decision_summary=self.decision_summary,
            impact_policy=self.impact_policy,
        )

    def _as_v3(self) -> PlanningRevisionProposalPayloadV3:
        """Keep the private alias for adapters released before v4."""
        return self.as_v3()

    def validate(self) -> None:
        self.as_v3().validate()

    def validate_against_base(self, base_snapshot: TaskPlanSnapshot) -> None:
        self.as_v3().validate_against_base(base_snapshot)

    def to_mapping(self) -> dict[str, object]:
        mapping = self.as_v3().canonical_payload()
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
class PlanningRevisionProposalPayloadV5:
    schema_version: int
    kind: PlanningRequestKind
    base_descriptor: RevisionBaseDescriptorV1
    root_task: ProposedRootTask
    subtasks: tuple[ProposedSubtask, ...]
    worker_items: tuple[ProposedWorkerItem, ...]
    subtask_milestones: tuple[SubtaskMilestone, ...]
    subtask_dependencies: tuple[SubtaskDependency, ...]
    worker_item_dependencies: tuple[WorkerItemDependency, ...]
    execution_intents: tuple[ExecutionIntent, ...]
    subtask_identity_decisions: tuple[SubtaskIdentityDecision, ...]
    worker_item_identity_decisions: tuple[WorkerItemIdentityDecision, ...]
    retired_subtask_ids: tuple[UUID, ...]
    retired_worker_item_ids: tuple[UUID, ...]
    decision_summary: DecisionSummary
    impact_policy: ImpactPolicy
    collaboration_policy_request: CollaborationPolicyRequestV2

    def __post_init__(self) -> None:
        if self.schema_version != 5:
            raise ValidationError("revision proposal schema_version must be 5")
        if self.kind is not PlanningRequestKind.REVISION:
            raise ValidationError("revision proposal kind must be revision")
        if not isinstance(self.base_descriptor, RevisionBaseDescriptorV1):
            raise ValidationError("revision proposal requires an exact base descriptor")
        for name in (
            "subtasks",
            "worker_items",
            "subtask_milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "subtask_identity_decisions",
            "worker_item_identity_decisions",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(
            self,
            "retired_subtask_ids",
            _unique_uuids(self.retired_subtask_ids, "retired_subtask_ids"),
        )
        object.__setattr__(
            self,
            "retired_worker_item_ids",
            _unique_uuids(self.retired_worker_item_ids, "retired_worker_item_ids"),
        )
        if not isinstance(
            self.collaboration_policy_request, CollaborationPolicyRequestV2
        ):
            raise ValidationError(
                "revision proposal v5 requires a collaboration policy request v2"
            )
        self.validate()

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> PlanningRevisionProposalPayloadV5:
        if not isinstance(value, Mapping):
            raise ValidationError("revision proposal v5 payload must be a mapping")
        allowed = {
            "schema_version",
            "kind",
            "base_descriptor",
            "root_task",
            "subtasks",
            "worker_items",
            "milestones",
            "subtask_dependencies",
            "worker_item_dependencies",
            "execution_intents",
            "subtask_identity_decisions",
            "worker_item_identity_decisions",
            "retired_subtask_ids",
            "retired_worker_item_ids",
            "decision_summary",
            "impact_policy",
            "collaboration_policy_request",
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValidationError(
                f"unknown revision proposal v5 fields: {', '.join(unknown)}"
            )
        if _int(value, "schema_version") != 5:
            raise ValidationError("revision proposal schema_version must be 5")
        policy_value = value.get("collaboration_policy_request")
        if not isinstance(policy_value, Mapping):
            raise ValidationError("collaboration_policy_request must be a mapping")
        legacy_mapping = dict(value)
        legacy_mapping.pop("collaboration_policy_request", None)
        legacy_mapping["schema_version"] = 3
        legacy = PlanningRevisionProposalPayloadV3.from_mapping(legacy_mapping)
        return cls(
            schema_version=5,
            kind=legacy.kind,
            base_descriptor=legacy.base_descriptor,
            root_task=legacy.root_task,
            subtasks=legacy.subtasks,
            worker_items=legacy.worker_items,
            subtask_milestones=legacy.subtask_milestones,
            subtask_dependencies=legacy.subtask_dependencies,
            worker_item_dependencies=legacy.worker_item_dependencies,
            execution_intents=legacy.execution_intents,
            subtask_identity_decisions=legacy.subtask_identity_decisions,
            worker_item_identity_decisions=legacy.worker_item_identity_decisions,
            retired_subtask_ids=legacy.retired_subtask_ids,
            retired_worker_item_ids=legacy.retired_worker_item_ids,
            decision_summary=legacy.decision_summary,
            impact_policy=legacy.impact_policy,
            collaboration_policy_request=CollaborationPolicyRequestV2.from_mapping(
                policy_value
            ),
        )

    def as_v3(self) -> PlanningRevisionProposalPayloadV3:
        return PlanningRevisionProposalPayloadV3(
            schema_version=3,
            kind=self.kind,
            base_descriptor=self.base_descriptor,
            root_task=self.root_task,
            subtasks=self.subtasks,
            worker_items=self.worker_items,
            subtask_milestones=self.subtask_milestones,
            subtask_dependencies=self.subtask_dependencies,
            worker_item_dependencies=self.worker_item_dependencies,
            execution_intents=self.execution_intents,
            subtask_identity_decisions=self.subtask_identity_decisions,
            worker_item_identity_decisions=self.worker_item_identity_decisions,
            retired_subtask_ids=self.retired_subtask_ids,
            retired_worker_item_ids=self.retired_worker_item_ids,
            decision_summary=self.decision_summary,
            impact_policy=self.impact_policy,
        )

    def _as_v3(self) -> PlanningRevisionProposalPayloadV3:
        """Keep the private legacy alias available to older callers."""
        return self.as_v3()

    def validate(self) -> None:
        self.as_v3().validate()
        self.collaboration_policy_request.validate_for_subtasks(
            frozenset(item.key for item in self.subtasks)
        )

    def validate_against_base(self, base_snapshot: TaskPlanSnapshot) -> None:
        self.validate()
        _validate_input_group_wait_graph(
            worker_items=self.worker_items,
            subtask_dependencies=self.subtask_dependencies,
            worker_item_dependencies=self.worker_item_dependencies,
            execution_intents=self.execution_intents,
            input_groups=self.collaboration_policy_request.input_groups,
        )
        self.as_v3().validate_against_base(base_snapshot)

    def to_mapping(self) -> dict[str, object]:
        mapping = self.as_v3().canonical_payload()
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


def _validate_input_group_wait_graph(
    *,
    worker_items: Sequence[ProposedWorkerItem],
    subtask_dependencies: Sequence[SubtaskDependency],
    worker_item_dependencies: Sequence[WorkerItemDependency],
    execution_intents: Sequence[ExecutionIntent],
    input_groups: tuple[InputGroupV1, ...],
) -> None:
    workers_by_subtask: dict[str, tuple[str, ...]] = {}
    grouped: dict[str, list[str]] = {}
    for item in worker_items:
        grouped.setdefault(item.subtask_key, []).append(item.key)
    workers_by_subtask = {
        key: tuple(values) for key, values in grouped.items()
    }
    hard_edges: set[tuple[str, str]] = set()
    for dependency in worker_item_dependencies:
        if dependency.kind == DependencyKind.HARD.value:
            hard_edges.add(
                (
                    dependency.upstream_worker_item_key,
                    dependency.downstream_worker_item_key,
                )
            )
    for intent in execution_intents:
        for dependency in intent.activation_dependencies:
            if dependency.kind == DependencyKind.HARD.value:
                hard_edges.add(
                    (
                        dependency.upstream_worker_item_key,
                        dependency.downstream_worker_item_key,
                    )
                )
    for dependency in subtask_dependencies:
        if dependency.kind is not DependencyKind.HARD:
            continue
        for upstream in workers_by_subtask.get(dependency.upstream_subtask_key, ()):
            for downstream in workers_by_subtask.get(
                dependency.downstream_subtask_key, ()
            ):
                hard_edges.add((upstream, downstream))
    try:
        validate_input_wait_graph(
            worker_keys=frozenset(item.key for item in worker_items),
            hard_edges=tuple(sorted(hard_edges)),
            input_groups=input_groups,
        )
    except (InputGroupInvalid, InputWaitCycle) as exc:
        raise ValidationError(str(exc), details=exc.details) from exc


def validate_revision_proposal(
    payload: (
        PlanningRevisionProposalPayloadV3
        | PlanningRevisionProposalPayloadV4
        | PlanningRevisionProposalPayloadV5
    ),
    *,
    profile_snapshot: object,
    brief_revision: int,
) -> (
    PlanningRevisionProposalPayloadV3
    | PlanningRevisionProposalPayloadV4
    | PlanningRevisionProposalPayloadV5
):
    if not isinstance(
        payload,
        (
            PlanningRevisionProposalPayloadV3,
            PlanningRevisionProposalPayloadV4,
            PlanningRevisionProposalPayloadV5,
        ),
    ):
        raise ValidationError("revision proposal must use schema v3, v4, or v5")
    if (
        getattr(profile_snapshot, "proposal_schema_version", None)
        != payload.schema_version
    ):
        raise ValidationError(
            f"revision proposal schema v{payload.schema_version} is not selected by the profile"
        )
    if brief_revision < 1:
        raise ValidationError("brief revision must be positive")
    payload.validate()
    if isinstance(payload, PlanningRevisionProposalPayloadV5):
        _validate_input_group_wait_graph(
            worker_items=payload.worker_items,
            subtask_dependencies=payload.subtask_dependencies,
            worker_item_dependencies=payload.worker_item_dependencies,
            execution_intents=payload.execution_intents,
            input_groups=payload.collaboration_policy_request.input_groups,
        )
    return payload


def _base_descriptor(value: Mapping[str, object]) -> RevisionBaseDescriptorV1:
    return RevisionBaseDescriptorV1(
        schema_version=_int(value, "schema_version"),
        workspace_id=_uuid(value.get("workspace_id"), "workspace_id"),
        root_task_id=_uuid(value.get("root_task_id"), "root_task_id"),
        plan_snapshot_id=_uuid(value.get("plan_snapshot_id"), "plan_snapshot_id"),
        plan_generation=_int(value, "plan_generation"),
        plan_content_hash=_string(value, "plan_content_hash"),
        root_task_version=_int(value, "root_task_version"),
        captured_at=_datetime(value.get("captured_at"), "captured_at"),
    )


def _root_task(value: Mapping[str, object]) -> ProposedRootTask:
    return ProposedRootTask(
        title=_string(value, "title"),
        goal_summary=_string(value, "goal_summary"),
        acceptance_refs=_strings(value, "acceptance_refs"),
    )


def _subtask(value: Mapping[str, object]) -> ProposedSubtask:
    return ProposedSubtask(
        key=_string(value, "key"),
        kind=WorkItemKind(_string(value, "kind")),
        title=_string(value, "title"),
        goal_summary=_string(value, "goal_summary"),
        acceptance_refs=_strings(value, "acceptance_refs"),
        expected_outputs=_strings(value, "expected_outputs"),
        milestone_key=(
            None
            if value.get("milestone_key") is None
            else _string(value, "milestone_key")
        ),
        source_refs=_strings(value, "source_refs"),
    )


def _worker(value: Mapping[str, object]) -> ProposedWorkerItem:
    return ProposedWorkerItem(
        key=_string(value, "key"),
        subtask_key=_string(value, "subtask_key"),
        title=_string(value, "title"),
        role=_string(value, "role"),
        responsibility=_string(value, "responsibility"),
        capability_requirements=_strings(value, "capability_requirements"),
        worker_policy_ref=_optional_string(value.get("worker_policy_ref")),
        configuration_ref=_optional_string(value.get("configuration_ref")),
        concurrency_limit=_optional_int(value.get("concurrency_limit")),
        source_refs=_strings(value, "source_refs"),
        pinned_worker_id=_optional_string(value.get("pinned_worker_id")),
    )


def _milestone(value: Mapping[str, object]) -> SubtaskMilestone:
    return SubtaskMilestone(
        key=_string(value, "key"),
        title=_string(value, "title"),
        subtask_keys=_strings(value, "subtask_keys"),
        acceptance_refs=_strings(value, "acceptance_refs"),
        order_hint=_int(value, "order_hint"),
    )


def _subtask_dependency(value: Mapping[str, object]) -> SubtaskDependency:
    from sidera_control_plane.domain.plans import DependencyEnforcement

    return SubtaskDependency(
        upstream_subtask_key=_string(value, "upstream_subtask_key"),
        downstream_subtask_key=_string(value, "downstream_subtask_key"),
        kind=DependencyKind(_string(value, "kind")),
        required_evidence_refs=_strings(value, "required_evidence_refs"),
        enforcement=DependencyEnforcement(_string(value, "enforcement")),
    )


def _worker_dependency(value: Mapping[str, object]) -> WorkerItemDependency:
    return WorkerItemDependency(
        upstream_worker_item_key=_string(value, "upstream_worker_item_key"),
        downstream_worker_item_key=_string(value, "downstream_worker_item_key"),
        kind=_string(value, "kind"),
        required_artifact_refs=_strings(value, "required_artifact_refs"),
    )


def _intent(value: Mapping[str, object]) -> ExecutionIntent:
    activations = tuple(
        ActivationDependency(
            upstream_worker_item_key=_string(item, "upstream_worker_item_key"),
            downstream_worker_item_key=_string(item, "downstream_worker_item_key"),
            kind=_string(item, "kind"),
            required_artifact_refs=_strings(item, "required_artifact_refs"),
        )
        for item in (
            _mapping(item, "activation dependency")
            for item in _sequence(value, "activation_dependencies")
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
        deadline_seconds=_optional_int(value.get("deadline_seconds")),
        not_before_seconds=_optional_int(value.get("not_before_seconds")),
        max_attempts=_int(value, "max_attempts"),
        activation_dependencies=activations,
    )


def _subtask_identity(value: Mapping[str, object]) -> SubtaskIdentityDecision:
    return SubtaskIdentityDecision(
        target_subtask_key=_string(value, "target_subtask_key"),
        strategy=IdentityStrategy(_string(value, "strategy")),
        existing_subtask_task_id=_optional_uuid(
            value.get("existing_subtask_task_id"), "existing_subtask_task_id"
        ),
        derived_from_subtask_task_ids=tuple(
            _uuid(item, "derived_from_subtask_task_ids")
            for item in _sequence(value, "derived_from_subtask_task_ids")
        ),
        reason=_string(value, "reason"),
    )


def _worker_identity(value: Mapping[str, object]) -> WorkerItemIdentityDecision:
    return WorkerItemIdentityDecision(
        target_worker_item_key=_string(value, "target_worker_item_key"),
        strategy=IdentityStrategy(_string(value, "strategy")),
        existing_worker_item_id=_optional_uuid(
            value.get("existing_worker_item_id"), "existing_worker_item_id"
        ),
        derived_from_worker_item_ids=tuple(
            _uuid(item, "derived_from_worker_item_ids")
            for item in _sequence(value, "derived_from_worker_item_ids")
        ),
        target_subtask_key=_string(value, "target_subtask_key"),
        context_disposition=ContextDisposition(_string(value, "context_disposition")),
        change_reasons=_strings(value, "change_reasons"),
    )


def _summary(value: Mapping[str, object]) -> DecisionSummary:
    return DecisionSummary(
        understanding=_string(value, "understanding"),
        assumptions=_strings(value, "assumptions"),
        risks=_strings(value, "risks"),
        estimated_cost_microunits=_int(value, "estimated_cost_microunits"),
        estimated_tokens=_int(value, "estimated_tokens", default=0),
        estimated_runtime_seconds=_int(value, "estimated_runtime_seconds"),
        estimated_concurrency=_int(value, "estimated_concurrency"),
        scope_includes=_strings(value, "scope_includes"),
        scope_excludes=_strings(value, "scope_excludes"),
        metadata=_mapping(value.get("metadata", {}), "metadata"),
    )


def _impact(value: Mapping[str, object]) -> ImpactPolicy:
    return ImpactPolicy(
        activate_immediately=_bool(value, "activate_immediately"),
        budget_level=_string(value, "budget_level"),
        failure_policy=_string(value, "failure_policy"),
        manual_gate=bool(value.get("manual_gate", False)),
        revision_operation=_optional_string(value.get("revision_operation")),
    )


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


def _worker_mapping(value: ProposedWorkerItem) -> dict[str, object]:
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


def _subtask_identity_mapping(value: SubtaskIdentityDecision) -> dict[str, object]:
    return {
        "target_subtask_key": value.target_subtask_key,
        "strategy": value.strategy.value,
        "existing_subtask_task_id": (
            str(value.existing_subtask_task_id)
            if value.existing_subtask_task_id is not None
            else None
        ),
        "derived_from_subtask_task_ids": [
            str(item) for item in value.derived_from_subtask_task_ids
        ],
        "reason": value.reason,
    }


def _worker_identity_mapping(
    value: WorkerItemIdentityDecision,
) -> dict[str, object]:
    return {
        "target_worker_item_key": value.target_worker_item_key,
        "strategy": value.strategy.value,
        "existing_worker_item_id": (
            str(value.existing_worker_item_id)
            if value.existing_worker_item_id is not None
            else None
        ),
        "derived_from_worker_item_ids": [
            str(item) for item in value.derived_from_worker_item_ids
        ],
        "target_subtask_key": value.target_subtask_key,
        "context_disposition": value.context_disposition.value,
        "change_reasons": list(value.change_reasons),
    }


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


def _impact_mapping(value: ImpactPolicy) -> dict[str, object]:
    return {
        "activate_immediately": value.activate_immediately,
        "budget_level": value.budget_level,
        "failure_policy": value.failure_policy,
        "manual_gate": value.manual_gate,
        "revision_operation": value.revision_operation,
    }


def _assert_identity_cover(
    existing: set[UUID], reused: set[UUID], retired: set[UUID], label: str
) -> None:
    if reused.intersection(retired):
        raise ValidationError(f"{label} identity cannot be both reused and retired")
    if not reused.union(retired) == existing:
        raise ValidationError(
            f"{label} identity decisions must cover every existing identity"
        )


def _assert_edge(upstream: str, downstream: str, keys: set[str], label: str) -> None:
    if upstream == downstream:
        raise ValidationError(f"{label} cannot point to itself")
    if upstream not in keys or downstream not in keys:
        raise ValidationError(f"{label} references an unknown node")


def _assert_acyclic(
    keys: tuple[str, ...], edges: tuple[tuple[str, str], ...], label: str
) -> None:
    adjacency: dict[str, list[str]] = {key: [] for key in keys}
    indegree = {key: 0 for key in keys}
    for upstream, downstream in edges:
        _assert_edge(upstream, downstream, set(keys), label)
        adjacency[upstream].append(downstream)
        indegree[downstream] += 1
    queue = [key for key in keys if indegree[key] == 0]
    visited = 0
    while queue:
        key = queue.pop(0)
        visited += 1
        for downstream in adjacency[key]:
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                queue.append(downstream)
    if visited != len(keys):
        raise ValidationError(f"{label} must be acyclic")


def _unique_keys(values: Iterable[str], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if len(result) != len(set(result)):
        raise ValidationError(f"{label} keys must be unique")
    return result


def _unique_uuids(values: Sequence[UUID], label: str) -> tuple[UUID, ...]:
    result = tuple(values)
    if any(not isinstance(value, UUID) for value in result):
        raise ValidationError(f"{label} must contain UUIDs")
    if len(result) != len(set(result)):
        raise ValidationError(f"{label} must be unique")
    return result


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be a mapping")
    return value


def _sequence(value: Mapping[str, object], label: str) -> tuple[object, ...]:
    raw = value.get(label)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValidationError(f"{label} must be a sequence")
    return tuple(raw)


def _string(value: Mapping[str, object], label: str) -> str:
    raw = value.get(label)
    if not isinstance(raw, str):
        raise ValidationError(f"{label} must be a string")
    return raw


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("optional field must be a string")
    return value


def _strings(value: Mapping[str, object], label: str) -> tuple[str, ...]:
    return tuple(_string({label: item}, label) for item in _sequence(value, label))


def _int(value: Mapping[str, object], label: str, *, default: int | None = None) -> int:
    raw = value.get(label, default)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValidationError(f"{label} must be an integer")
    return raw


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError("optional integer must be an integer")
    return value


def _bool(value: Mapping[str, object], label: str) -> bool:
    raw = value.get(label)
    if not isinstance(raw, bool):
        raise ValidationError(f"{label} must be a boolean")
    return raw


def _uuid(value: object, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UUID") from exc


def _optional_uuid(value: object, label: str) -> UUID | None:
    return None if value is None else _uuid(value, label)


def _datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{label} must be timezone-aware")
    return parsed


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValidationError(f"unsupported JSON value: {type(value).__name__}")
