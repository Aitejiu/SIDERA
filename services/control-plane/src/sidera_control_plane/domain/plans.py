from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.collaboration import (
    CollaborationPolicySnapshotV1,
    CollaborationPolicySnapshotV2,
)
from sidera_control_plane.domain.errors import ValidationError

SUPPORTED_PLAN_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})


class WorkItemKind(StrEnum):
    CHILD = "child"
    INTEGRATION = "integration"


class DependencyKind(StrEnum):
    HARD = "hard"
    INFORMATIONAL = "informational"


class DependencyEnforcement(StrEnum):
    INFORMATIONAL = "informational"
    CONTROL_PLANE = "control_plane"


class PlanValidationError(ValidationError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(
            f"{reason}: {detail}", details={"reason": reason, "detail": detail}
        )
        self.reason = reason


@dataclass(frozen=True, slots=True)
class WorkItem:
    key: str
    task_id: UUID | None
    kind: WorkItemKind
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    milestone_key: str | None
    worker_slot_key: str | None
    expected_outputs: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerSlot:
    key: str
    role: str
    capability_requirements: tuple[str, ...]
    worker_policy_ref: str | None
    configuration_ref: str | None
    concurrency_limit: int | None


@dataclass(frozen=True, slots=True)
class SubtaskPlanItem:
    key: str
    task_id: UUID | None
    kind: WorkItemKind
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    milestone_key: str | None
    expected_outputs: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerItemPlanItem:
    key: str
    worker_item_id: UUID | None
    subtask_key: str
    title: str
    role: str
    responsibility: str
    capability_requirements: tuple[str, ...]
    worker_policy_ref: str | None
    configuration_ref: str | None
    concurrency_limit: int | None
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Milestone:
    key: str
    title: str
    work_item_keys: tuple[str, ...]
    acceptance_refs: tuple[str, ...]
    order_hint: int


@dataclass(frozen=True, slots=True)
class Dependency:
    upstream_work_item_key: str
    downstream_work_item_key: str
    kind: DependencyKind
    required_evidence_refs: tuple[str, ...]
    enforcement: DependencyEnforcement


@dataclass(frozen=True, slots=True)
class SubtaskMilestone:
    key: str
    title: str
    subtask_keys: tuple[str, ...]
    acceptance_refs: tuple[str, ...]
    order_hint: int


@dataclass(frozen=True, slots=True)
class SubtaskDependency:
    upstream_subtask_key: str
    downstream_subtask_key: str
    kind: DependencyKind
    required_evidence_refs: tuple[str, ...]
    enforcement: DependencyEnforcement


@dataclass(frozen=True, slots=True)
class WorkerItemDependency:
    upstream_worker_item_key: str
    downstream_worker_item_key: str
    kind: str
    required_artifact_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskPlanSnapshot:
    id: UUID
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    generation: int
    parent_snapshot_id: UUID | None
    source_type: str
    source_ref: str
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    work_items: tuple[WorkItem, ...]
    worker_slots: tuple[WorkerSlot, ...]
    milestones: tuple[Milestone, ...]
    dependencies: tuple[Dependency, ...]
    published_by: str
    published_at: datetime
    subtasks: tuple[SubtaskPlanItem, ...] = ()
    worker_item_plans: tuple[WorkerItemPlanItem, ...] = ()
    subtask_milestones: tuple[SubtaskMilestone, ...] = ()
    subtask_dependencies: tuple[SubtaskDependency, ...] = ()
    worker_item_dependencies: tuple[WorkerItemDependency, ...] = ()
    collaboration_policy: (
        CollaborationPolicySnapshotV1 | CollaborationPolicySnapshotV2 | None
    ) = None

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: UUID,
        schema_version: int,
        workspace_id: UUID,
        root_task_id: UUID,
        generation: int,
        parent_snapshot_id: UUID | None,
        source_type: str,
        source_ref: str,
        title: str,
        goal_summary: str,
        acceptance_refs: tuple[str, ...],
        published_by: str,
        published_at: datetime,
        work_items: tuple[WorkItem, ...] = (),
        worker_slots: tuple[WorkerSlot, ...] = (),
        milestones: tuple[Milestone, ...] = (),
        dependencies: tuple[Dependency, ...] = (),
        subtasks: tuple[SubtaskPlanItem, ...] = (),
        worker_item_plans: tuple[WorkerItemPlanItem, ...] = (),
        subtask_milestones: tuple[SubtaskMilestone, ...] = (),
        subtask_dependencies: tuple[SubtaskDependency, ...] = (),
        worker_item_dependencies: tuple[WorkerItemDependency, ...] = (),
        collaboration_policy: (
            CollaborationPolicySnapshotV1 | CollaborationPolicySnapshotV2 | None
        ) = None,
    ) -> TaskPlanSnapshot:
        snapshot = cls(
            id=snapshot_id,
            schema_version=schema_version,
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            generation=generation,
            parent_snapshot_id=parent_snapshot_id,
            source_type=source_type.strip(),
            source_ref=source_ref.strip(),
            title=title.strip(),
            goal_summary=goal_summary.strip(),
            acceptance_refs=_clean_tuple(acceptance_refs),
            work_items=tuple(work_items),
            worker_slots=tuple(worker_slots),
            milestones=tuple(milestones),
            dependencies=tuple(dependencies),
            published_by=published_by.strip(),
            published_at=published_at,
            subtasks=tuple(subtasks),
            worker_item_plans=tuple(worker_item_plans),
            subtask_milestones=tuple(subtask_milestones),
            subtask_dependencies=tuple(subtask_dependencies),
            worker_item_dependencies=tuple(worker_item_dependencies),
            collaboration_policy=collaboration_policy,
        )
        snapshot._validate()
        return snapshot

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TaskPlanSnapshot:
        def required(name: str) -> Any:
            if name not in payload:
                raise PlanValidationError("missing_field", f"missing field: {name}")
            return payload[name]

        try:
            schema_version = int(required("schema_version"))
            common: dict[str, Any] = {
                "snapshot_id": UUID(str(required("id"))),
                "schema_version": schema_version,
                "workspace_id": UUID(str(required("workspace_id"))),
                "root_task_id": UUID(str(required("root_task_id"))),
                "generation": int(required("generation")),
                "parent_snapshot_id": (
                    UUID(str(payload["parent_snapshot_id"]))
                    if payload.get("parent_snapshot_id")
                    else None
                ),
                "source_type": str(required("source_type")),
                "source_ref": str(required("source_ref")),
                "title": str(required("title")),
                "goal_summary": str(required("goal_summary")),
                "acceptance_refs": tuple(
                    str(item) for item in required("acceptance_refs")
                ),
                "published_by": str(required("published_by")),
                "published_at": datetime.fromisoformat(
                    str(required("published_at"))
                ),
            }
            if schema_version == 1:
                _reject_unknown_fields(
                    payload,
                    {
                        "id",
                        "schema_version",
                        "workspace_id",
                        "root_task_id",
                        "generation",
                        "parent_snapshot_id",
                        "source_type",
                        "source_ref",
                        "title",
                        "goal_summary",
                        "acceptance_refs",
                        "work_items",
                        "worker_slots",
                        "milestones",
                        "dependencies",
                        "published_by",
                        "published_at",
                    },
                    "plan",
                )
                return cls.create(
                    **common,
                    work_items=tuple(
                        _work_item_from_payload(item) for item in required("work_items")
                    ),
                    worker_slots=tuple(
                        _worker_slot_from_payload(item)
                        for item in required("worker_slots")
                    ),
                    milestones=tuple(
                        _milestone_from_payload(item)
                        for item in required("milestones")
                    ),
                    dependencies=tuple(
                        _dependency_from_payload(item)
                        for item in required("dependencies")
                    ),
                )
            if schema_version == 2:
                _reject_unknown_fields(
                    payload,
                    {
                        "id",
                        "schema_version",
                        "workspace_id",
                        "root_task_id",
                        "generation",
                        "parent_snapshot_id",
                        "source_type",
                        "source_ref",
                        "title",
                        "goal_summary",
                        "acceptance_refs",
                        "subtasks",
                        "worker_items",
                        "milestones",
                        "subtask_dependencies",
                        "worker_item_dependencies",
                        "published_by",
                        "published_at",
                    },
                    "plan",
                )
                return cls.create(
                    **common,
                    work_items=(),
                    worker_slots=(),
                    milestones=(),
                    dependencies=(),
                    subtasks=tuple(
                        _subtask_from_payload(item) for item in required("subtasks")
                    ),
                    worker_item_plans=tuple(
                        _worker_item_plan_from_payload(item)
                        for item in required("worker_items")
                    ),
                    subtask_milestones=tuple(
                        _subtask_milestone_from_payload(item)
                        for item in required("milestones")
                    ),
                    subtask_dependencies=tuple(
                        _subtask_dependency_from_payload(item)
                        for item in required("subtask_dependencies")
                    ),
                    worker_item_dependencies=tuple(
                        _worker_item_dependency_from_payload(item)
                        for item in required("worker_item_dependencies")
                    ),
                )
            if schema_version in {3, 4}:
                _reject_unknown_fields(
                    payload,
                    {
                        "id",
                        "schema_version",
                        "workspace_id",
                        "root_task_id",
                        "generation",
                        "parent_snapshot_id",
                        "source_type",
                        "source_ref",
                        "title",
                        "goal_summary",
                        "acceptance_refs",
                        "subtasks",
                        "worker_items",
                        "milestones",
                        "subtask_dependencies",
                        "worker_item_dependencies",
                        "collaboration_policy",
                        "published_by",
                        "published_at",
                    },
                    "plan",
                )
                raw_policy = required("collaboration_policy")
                policy = (
                    None
                    if raw_policy is None
                    else (
                        CollaborationPolicySnapshotV1.from_mapping(
                            _payload_mapping(raw_policy, "collaboration_policy")
                        )
                        if schema_version == 3
                        else CollaborationPolicySnapshotV2.from_mapping(
                            _payload_mapping(raw_policy, "collaboration_policy")
                        )
                    )
                )
                return cls.create(
                    **common,
                    work_items=(),
                    worker_slots=(),
                    milestones=(),
                    dependencies=(),
                    subtasks=tuple(
                        _subtask_from_payload(item) for item in required("subtasks")
                    ),
                    worker_item_plans=tuple(
                        _worker_item_plan_from_payload(item)
                        for item in required("worker_items")
                    ),
                    subtask_milestones=tuple(
                        _subtask_milestone_from_payload(item)
                        for item in required("milestones")
                    ),
                    subtask_dependencies=tuple(
                        _subtask_dependency_from_payload(item)
                        for item in required("subtask_dependencies")
                    ),
                    worker_item_dependencies=tuple(
                        _worker_item_dependency_from_payload(item)
                        for item in required("worker_item_dependencies")
                    ),
                    collaboration_policy=policy,
                )
            return cls.create(**common, work_items=(), worker_slots=(), milestones=(), dependencies=())
        except PlanValidationError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise PlanValidationError("invalid_payload", str(exc)) from exc

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "id": str(self.id),
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "generation": self.generation,
            "parent_snapshot_id": (
                str(self.parent_snapshot_id) if self.parent_snapshot_id else None
            ),
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "title": self.title,
            "goal_summary": self.goal_summary,
            "acceptance_refs": list(self.acceptance_refs),
            "published_by": self.published_by,
            "published_at": self.published_at.astimezone(UTC).isoformat(),
        }
        if self.schema_version == 1:
            payload.update(
                {
                    "work_items": [
                        _work_item_to_payload(item) for item in self.work_items
                    ],
                    "worker_slots": [
                        _worker_slot_to_payload(slot) for slot in self.worker_slots
                    ],
                    "milestones": [
                        _milestone_to_payload(item) for item in self.milestones
                    ],
                    "dependencies": [
                        _dependency_to_payload(item)
                        for item in self.dependencies
                    ],
                }
            )
        elif self.schema_version in {2, 3, 4}:
            payload.update(
                {
                    "subtasks": [
                        _subtask_to_payload(item) for item in self.subtasks
                    ],
                    "worker_items": [
                        _worker_item_plan_to_payload(item)
                        for item in self.worker_item_plans
                    ],
                    "milestones": [
                        _subtask_milestone_to_payload(item)
                        for item in self.subtask_milestones
                    ],
                    "subtask_dependencies": [
                        _subtask_dependency_to_payload(item)
                        for item in self.subtask_dependencies
                    ],
                    "worker_item_dependencies": [
                        _worker_item_dependency_to_payload(item)
                        for item in self.worker_item_dependencies
                    ],
                }
            )
            if self.schema_version in {3, 4}:
                payload["collaboration_policy"] = (
                    self.collaboration_policy.to_mapping()
                    if self.collaboration_policy is not None
                    else None
                )
        return payload

    def _validate(self) -> None:
        if self.schema_version not in SUPPORTED_PLAN_SCHEMA_VERSIONS:
            self._fail(
                "unsupported_schema_version",
                f"unsupported plan schema version: {self.schema_version}",
            )
        if self.schema_version in {2, 3, 4}:
            self._validate_v2()
            if (
                self.schema_version == 3
                and self.collaboration_policy is not None
                and not isinstance(
                    self.collaboration_policy, CollaborationPolicySnapshotV1
                )
            ):
                self._fail(
                    "invalid_collaboration_policy",
                    "schema v3 collaboration_policy must be a policy snapshot",
                )
            if self.schema_version == 4 and not isinstance(
                self.collaboration_policy, CollaborationPolicySnapshotV2
            ):
                self._fail(
                    "invalid_collaboration_policy",
                    "schema v4 collaboration_policy must be a v2 policy snapshot",
                )
            return
        if self.generation < 1:
            self._fail("invalid_generation", "generation must be positive")
        if self.published_at.tzinfo is None:
            self._fail("invalid_timestamp", "published_at must be timezone-aware")
        for name, value in (
            ("source_type", self.source_type),
            ("source_ref", self.source_ref),
            ("title", self.title),
            ("goal_summary", self.goal_summary),
            ("published_by", self.published_by),
        ):
            if not value:
                self._fail("missing_field", f"{name} is required")

        work_item_keys = _unique_keys(
            (item.key for item in self.work_items), "work item"
        )
        worker_slot_keys = _unique_keys(
            (slot.key for slot in self.worker_slots), "worker slot"
        )
        milestone_keys = _unique_keys(
            (milestone.key for milestone in self.milestones), "milestone"
        )
        task_ids = [item.task_id for item in self.work_items if item.task_id]
        if len(task_ids) != len(set(task_ids)):
            self._fail("duplicate_task_id", "a materialized Task cannot bind twice")

        for item in self.work_items:
            if item.milestone_key and item.milestone_key not in milestone_keys:
                self._fail(
                    "unknown_reference",
                    f"work item references unknown milestone: {item.milestone_key}",
                )
            if item.worker_slot_key and item.worker_slot_key not in worker_slot_keys:
                self._fail(
                    "unknown_reference",
                    f"work item references unknown worker slot: {item.worker_slot_key}",
                )
            if not item.title.strip() or not item.goal_summary.strip():
                self._fail("missing_field", f"work item {item.key} needs title and goal")

        for slot in self.worker_slots:
            if not slot.key.strip() or not slot.role.strip():
                self._fail("missing_field", "worker slot key and role are required")
            if slot.concurrency_limit is not None and slot.concurrency_limit < 1:
                self._fail("invalid_concurrency", f"worker slot {slot.key} limit must be positive")

        for milestone in self.milestones:
            if milestone.order_hint < 0:
                self._fail("invalid_order_hint", f"milestone {milestone.key} order must be non-negative")
            if len(set(milestone.work_item_keys)) != len(milestone.work_item_keys):
                self._fail("duplicate_reference", f"milestone {milestone.key} repeats a work item")
            for key in milestone.work_item_keys:
                if key not in work_item_keys:
                    self._fail(
                        "unknown_reference",
                        f"milestone references unknown work item: {key}",
                    )

        graph: dict[str, set[str]] = {key: set() for key in work_item_keys}
        indegree = {key: 0 for key in work_item_keys}
        for dependency in self.dependencies:
            upstream = dependency.upstream_work_item_key
            downstream = dependency.downstream_work_item_key
            if upstream not in work_item_keys or downstream not in work_item_keys:
                self._fail(
                    "unknown_reference",
                    "dependency references unknown work item",
                )
            if upstream == downstream:
                self._fail("self_edge", "dependency cannot point to itself")
            if dependency.enforcement is DependencyEnforcement.CONTROL_PLANE:
                self._fail(
                    "unsupported_dependency_enforcement",
                    "R4 only accepts informational dependency enforcement",
                )
            if downstream not in graph[upstream]:
                graph[upstream].add(downstream)
                indegree[downstream] += 1

        queue = [key for key, degree in indegree.items() if degree == 0]
        visited = 0
        while queue:
            key = queue.pop()
            visited += 1
            for downstream in graph[key]:
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    queue.append(downstream)
        if visited != len(work_item_keys):
            self._fail("dependency_cycle", "dependency graph must be acyclic")

    def _validate_v2(self) -> None:
        if self.generation < 1:
            self._fail("invalid_generation", "generation must be positive")
        if self.published_at.tzinfo is None:
            self._fail("invalid_timestamp", "published_at must be timezone-aware")
        for name, value in (
            ("source_type", self.source_type),
            ("source_ref", self.source_ref),
            ("title", self.title),
            ("goal_summary", self.goal_summary),
            ("published_by", self.published_by),
        ):
            if not value:
                self._fail("missing_field", f"{name} is required")
        if self.work_items or self.worker_slots or self.milestones or self.dependencies:
            self._fail("mixed_schema_fields", "schema v2 cannot contain schema v1 fields")
        if self.schema_version == 2 and self.collaboration_policy is not None:
            self._fail(
                "mixed_schema_fields",
                "schema v2 cannot contain collaboration policy",
            )

        subtask_keys = _unique_keys(
            (item.key for item in self.subtasks), "subtask"
        )
        milestone_keys = _unique_keys(
            (item.key for item in self.subtask_milestones), "milestone"
        )
        worker_item_keys = _unique_keys(
            (item.key for item in self.worker_item_plans), "worker item plan"
        )
        task_ids = [item.task_id for item in self.subtasks if item.task_id]
        if len(task_ids) != len(set(task_ids)):
            self._fail("duplicate_task_id", "a materialized Task cannot bind twice")

        for item in self.subtasks:
            if not item.title.strip() or not item.goal_summary.strip():
                self._fail("missing_field", f"subtask {item.key} needs title and goal")
            if item.milestone_key and item.milestone_key not in milestone_keys:
                self._fail(
                    "unknown_reference",
                    f"subtask references unknown milestone: {item.milestone_key}",
                )

        worker_item_ids = [
            item.worker_item_id
            for item in self.worker_item_plans
            if item.worker_item_id is not None
        ]
        if len(worker_item_ids) != len(set(worker_item_ids)):
            self._fail(
                "duplicate_worker_item_id",
                "a WorkerItem can have only one placement in a plan generation",
            )
        worker_item_subtasks: dict[str, str] = {}
        for item in self.worker_item_plans:
            if item.subtask_key not in subtask_keys:
                self._fail(
                    "unknown_reference",
                    f"worker item references unknown subtask: {item.subtask_key}",
                )
            if not item.title.strip() or not item.role.strip() or not item.responsibility.strip():
                self._fail(
                    "missing_field",
                    f"worker item {item.key} needs title, role and responsibility",
                )
            if item.concurrency_limit is not None and item.concurrency_limit < 1:
                self._fail(
                    "invalid_concurrency",
                    f"worker item {item.key} limit must be positive",
                )
            worker_item_subtasks[item.key] = item.subtask_key

        for milestone in self.subtask_milestones:
            if milestone.order_hint < 0:
                self._fail(
                    "invalid_order_hint",
                    f"milestone {milestone.key} order must be non-negative",
                )
            if len(set(milestone.subtask_keys)) != len(milestone.subtask_keys):
                self._fail(
                    "duplicate_reference",
                    f"milestone {milestone.key} repeats a subtask",
                )
            for key in milestone.subtask_keys:
                if key not in subtask_keys:
                    self._fail(
                        "unknown_reference",
                        f"milestone references unknown subtask: {key}",
                    )

        subtask_graph: dict[str, set[str]] = {
            key: set() for key in subtask_keys
        }
        subtask_indegree = {key: 0 for key in subtask_keys}
        for dependency in self.subtask_dependencies:
            upstream = dependency.upstream_subtask_key
            downstream = dependency.downstream_subtask_key
            if upstream not in subtask_keys or downstream not in subtask_keys:
                self._fail(
                    "unknown_reference",
                    "subtask dependency references unknown subtask",
                )
            if upstream == downstream:
                self._fail("self_edge", "subtask dependency cannot point to itself")
            if dependency.kind not in {
                DependencyKind.HARD,
                DependencyKind.INFORMATIONAL,
            }:
                self._fail("invalid_dependency_kind", "unknown subtask dependency kind")
            if dependency.enforcement not in {
                DependencyEnforcement.INFORMATIONAL,
                DependencyEnforcement.CONTROL_PLANE,
            }:
                self._fail("invalid_dependency_enforcement", "unknown enforcement")
            if downstream not in subtask_graph[upstream]:
                subtask_graph[upstream].add(downstream)
                subtask_indegree[downstream] += 1
        if not _is_acyclic(subtask_graph, subtask_indegree):
            self._fail("cyclic_dependency", "subtask dependency graph must be acyclic")

        valid_worker_dependency_kinds = {"dependency", "review", "context"}
        for dependency in self.worker_item_dependencies:
            upstream = dependency.upstream_worker_item_key
            downstream = dependency.downstream_worker_item_key
            if upstream not in worker_item_keys or downstream not in worker_item_keys:
                self._fail(
                    "unknown_reference",
                    "worker item dependency references unknown worker item",
                )
            if worker_item_subtasks[upstream] != worker_item_subtasks[downstream]:
                self._fail(
                    "cross_subtask_worker_dependency",
                    "WorkerItem Graph edges must stay within one Subtask",
                )
            if dependency.kind not in valid_worker_dependency_kinds:
                self._fail("invalid_worker_item_dependency_kind", "unknown WorkerItem edge kind")
            if upstream == downstream and dependency.kind != "context":
                self._fail("self_edge", "WorkerItem dependency cannot point to itself")

    @staticmethod
    def _fail(reason: str, detail: str) -> None:
        raise PlanValidationError(reason, detail)


def _clean_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value.strip())


def _unique_keys(values: Any, label: str) -> set[str]:
    keys = tuple(str(value).strip() for value in values)
    if any(not key for key in keys):
        raise PlanValidationError("missing_key", f"{label} key is required")
    if len(keys) != len(set(keys)):
        raise PlanValidationError(
            "duplicate_key", f"{label} keys must be unique"
        )
    return set(keys)


def _is_acyclic(graph: dict[str, set[str]], indegree: dict[str, int]) -> bool:
    queue = [key for key, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        key = queue.pop()
        visited += 1
        for downstream in graph[key]:
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                queue.append(downstream)
    return visited == len(graph)


def _reject_unknown_fields(
    payload: dict[str, Any], allowed: set[str], label: str
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise PlanValidationError(
            "unknown_field", f"{label} contains unknown field: {unknown[0]}"
        )


def _payload_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanValidationError("invalid_payload", f"{label} must be an object")
    return dict(value)


def _subtask_to_payload(item: SubtaskPlanItem) -> dict[str, Any]:
    return {
        "key": item.key,
        "task_id": str(item.task_id) if item.task_id else None,
        "kind": item.kind.value,
        "title": item.title,
        "goal_summary": item.goal_summary,
        "acceptance_refs": list(item.acceptance_refs),
        "milestone_key": item.milestone_key,
        "expected_outputs": list(item.expected_outputs),
        "source_refs": list(item.source_refs),
    }


def _subtask_from_payload(value: Any) -> SubtaskPlanItem:
    payload = _payload_mapping(value, "subtask")
    _reject_unknown_fields(
        payload,
        {
            "key",
            "task_id",
            "kind",
            "title",
            "goal_summary",
            "acceptance_refs",
            "milestone_key",
            "expected_outputs",
            "source_refs",
        },
        "subtask",
    )
    return SubtaskPlanItem(
        key=str(payload["key"]),
        task_id=UUID(str(payload["task_id"])) if payload.get("task_id") else None,
        kind=WorkItemKind(str(payload["kind"])),
        title=str(payload["title"]),
        goal_summary=str(payload["goal_summary"]),
        acceptance_refs=tuple(str(value) for value in payload.get("acceptance_refs", ())),
        milestone_key=(str(payload["milestone_key"]) if payload.get("milestone_key") else None),
        expected_outputs=tuple(str(value) for value in payload.get("expected_outputs", ())),
        source_refs=tuple(str(value) for value in payload.get("source_refs", ())),
    )


def _worker_item_plan_to_payload(item: WorkerItemPlanItem) -> dict[str, Any]:
    return {
        "key": item.key,
        "worker_item_id": str(item.worker_item_id) if item.worker_item_id else None,
        "subtask_key": item.subtask_key,
        "title": item.title,
        "role": item.role,
        "responsibility": item.responsibility,
        "capability_requirements": list(item.capability_requirements),
        "worker_policy_ref": item.worker_policy_ref,
        "configuration_ref": item.configuration_ref,
        "concurrency_limit": item.concurrency_limit,
        "source_refs": list(item.source_refs),
    }


def _worker_item_plan_from_payload(value: Any) -> WorkerItemPlanItem:
    payload = _payload_mapping(value, "worker item")
    _reject_unknown_fields(
        payload,
        {
            "key",
            "worker_item_id",
            "subtask_key",
            "title",
            "role",
            "responsibility",
            "capability_requirements",
            "worker_policy_ref",
            "configuration_ref",
            "concurrency_limit",
            "source_refs",
        },
        "worker item",
    )
    return WorkerItemPlanItem(
        key=str(payload["key"]),
        worker_item_id=(
            UUID(str(payload["worker_item_id"]))
            if payload.get("worker_item_id")
            else None
        ),
        subtask_key=str(payload["subtask_key"]),
        title=str(payload["title"]),
        role=str(payload["role"]),
        responsibility=str(payload["responsibility"]),
        capability_requirements=tuple(
            str(value) for value in payload.get("capability_requirements", ())
        ),
        worker_policy_ref=(
            str(payload["worker_policy_ref"])
            if payload.get("worker_policy_ref")
            else None
        ),
        configuration_ref=(
            str(payload["configuration_ref"])
            if payload.get("configuration_ref")
            else None
        ),
        concurrency_limit=(
            int(payload["concurrency_limit"])
            if payload.get("concurrency_limit") is not None
            else None
        ),
        source_refs=tuple(str(value) for value in payload.get("source_refs", ())),
    )


def _subtask_milestone_to_payload(item: SubtaskMilestone) -> dict[str, Any]:
    return {
        "key": item.key,
        "title": item.title,
        "subtask_keys": list(item.subtask_keys),
        "acceptance_refs": list(item.acceptance_refs),
        "order_hint": item.order_hint,
    }


def _subtask_milestone_from_payload(value: Any) -> SubtaskMilestone:
    payload = _payload_mapping(value, "subtask milestone")
    _reject_unknown_fields(
        payload,
        {"key", "title", "subtask_keys", "acceptance_refs", "order_hint"},
        "subtask milestone",
    )
    return SubtaskMilestone(
        key=str(payload["key"]),
        title=str(payload["title"]),
        subtask_keys=tuple(str(value) for value in payload.get("subtask_keys", ())),
        acceptance_refs=tuple(str(value) for value in payload.get("acceptance_refs", ())),
        order_hint=int(payload["order_hint"]),
    )


def _subtask_dependency_to_payload(item: SubtaskDependency) -> dict[str, Any]:
    return {
        "upstream_subtask_key": item.upstream_subtask_key,
        "downstream_subtask_key": item.downstream_subtask_key,
        "kind": item.kind.value,
        "required_evidence_refs": list(item.required_evidence_refs),
        "enforcement": item.enforcement.value,
    }


def _subtask_dependency_from_payload(value: Any) -> SubtaskDependency:
    payload = _payload_mapping(value, "subtask dependency")
    _reject_unknown_fields(
        payload,
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
        upstream_subtask_key=str(payload["upstream_subtask_key"]),
        downstream_subtask_key=str(payload["downstream_subtask_key"]),
        kind=DependencyKind(str(payload["kind"])),
        required_evidence_refs=tuple(
            str(value) for value in payload.get("required_evidence_refs", ())
        ),
        enforcement=DependencyEnforcement(str(payload["enforcement"])),
    )


def _worker_item_dependency_to_payload(item: WorkerItemDependency) -> dict[str, Any]:
    return {
        "upstream_worker_item_key": item.upstream_worker_item_key,
        "downstream_worker_item_key": item.downstream_worker_item_key,
        "kind": item.kind,
        "required_artifact_refs": list(item.required_artifact_refs),
    }


def _worker_item_dependency_from_payload(value: Any) -> WorkerItemDependency:
    payload = _payload_mapping(value, "worker item dependency")
    _reject_unknown_fields(
        payload,
        {
            "upstream_worker_item_key",
            "downstream_worker_item_key",
            "kind",
            "required_artifact_refs",
        },
        "worker item dependency",
    )
    return WorkerItemDependency(
        upstream_worker_item_key=str(payload["upstream_worker_item_key"]),
        downstream_worker_item_key=str(payload["downstream_worker_item_key"]),
        kind=str(payload["kind"]),
        required_artifact_refs=tuple(
            str(value) for value in payload.get("required_artifact_refs", ())
        ),
    )


def _work_item_to_payload(item: WorkItem) -> dict[str, Any]:
    return {
        "key": item.key,
        "task_id": str(item.task_id) if item.task_id else None,
        "kind": item.kind.value,
        "title": item.title,
        "goal_summary": item.goal_summary,
        "acceptance_refs": list(item.acceptance_refs),
        "milestone_key": item.milestone_key,
        "worker_slot_key": item.worker_slot_key,
        "expected_outputs": list(item.expected_outputs),
        "source_refs": list(item.source_refs),
    }


def _work_item_from_payload(payload: dict[str, Any]) -> WorkItem:
    return WorkItem(
        key=str(payload["key"]),
        task_id=UUID(str(payload["task_id"])) if payload.get("task_id") else None,
        kind=WorkItemKind(str(payload["kind"])),
        title=str(payload["title"]),
        goal_summary=str(payload["goal_summary"]),
        acceptance_refs=tuple(str(value) for value in payload.get("acceptance_refs", ())),
        milestone_key=(str(payload["milestone_key"]) if payload.get("milestone_key") else None),
        worker_slot_key=(str(payload["worker_slot_key"]) if payload.get("worker_slot_key") else None),
        expected_outputs=tuple(str(value) for value in payload.get("expected_outputs", ())),
        source_refs=tuple(str(value) for value in payload.get("source_refs", ())),
    )


def _worker_slot_to_payload(slot: WorkerSlot) -> dict[str, Any]:
    return {
        "key": slot.key,
        "role": slot.role,
        "capability_requirements": list(slot.capability_requirements),
        "worker_policy_ref": slot.worker_policy_ref,
        "configuration_ref": slot.configuration_ref,
        "concurrency_limit": slot.concurrency_limit,
    }


def _worker_slot_from_payload(payload: dict[str, Any]) -> WorkerSlot:
    return WorkerSlot(
        key=str(payload["key"]),
        role=str(payload["role"]),
        capability_requirements=tuple(
            str(value) for value in payload.get("capability_requirements", ())
        ),
        worker_policy_ref=(
            str(payload["worker_policy_ref"])
            if payload.get("worker_policy_ref")
            else None
        ),
        configuration_ref=(
            str(payload["configuration_ref"])
            if payload.get("configuration_ref")
            else None
        ),
        concurrency_limit=(
            int(payload["concurrency_limit"])
            if payload.get("concurrency_limit") is not None
            else None
        ),
    )


def _milestone_to_payload(milestone: Milestone) -> dict[str, Any]:
    return {
        "key": milestone.key,
        "title": milestone.title,
        "work_item_keys": list(milestone.work_item_keys),
        "acceptance_refs": list(milestone.acceptance_refs),
        "order_hint": milestone.order_hint,
    }


def _milestone_from_payload(payload: dict[str, Any]) -> Milestone:
    return Milestone(
        key=str(payload["key"]),
        title=str(payload["title"]),
        work_item_keys=tuple(str(value) for value in payload.get("work_item_keys", ())),
        acceptance_refs=tuple(str(value) for value in payload.get("acceptance_refs", ())),
        order_hint=int(payload["order_hint"]),
    )


def _dependency_to_payload(dependency: Dependency) -> dict[str, Any]:
    return {
        "upstream_work_item_key": dependency.upstream_work_item_key,
        "downstream_work_item_key": dependency.downstream_work_item_key,
        "kind": dependency.kind.value,
        "required_evidence_refs": list(dependency.required_evidence_refs),
        "enforcement": dependency.enforcement.value,
    }


def _dependency_from_payload(payload: dict[str, Any]) -> Dependency:
    return Dependency(
        upstream_work_item_key=str(payload["upstream_work_item_key"]),
        downstream_work_item_key=str(payload["downstream_work_item_key"]),
        kind=DependencyKind(str(payload["kind"])),
        required_evidence_refs=tuple(
            str(value) for value in payload.get("required_evidence_refs", ())
        ),
        enforcement=DependencyEnforcement(str(payload["enforcement"])),
    )
