from dataclasses import dataclass
from uuid import UUID

from sidera_control_plane.domain.plans import (
    SubtaskDependency,
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemDependency,
)


@dataclass(frozen=True, slots=True)
class CanonicalWorkerItemPlan:
    key: str
    worker_item_id: UUID | None
    legacy_identity_key: str | None
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
class CanonicalTaskPlan:
    snapshot_id: UUID
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    generation: int
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    subtasks: tuple[SubtaskPlanItem, ...]
    worker_items: tuple[CanonicalWorkerItemPlan, ...]
    milestones: tuple[SubtaskMilestone, ...]
    subtask_dependencies: tuple[SubtaskDependency, ...]
    worker_item_dependencies: tuple[WorkerItemDependency, ...]


class PlanNormalizer:
    """Convert versioned plan snapshots into the fixed R4.1 plan vocabulary."""

    def normalize(self, snapshot: TaskPlanSnapshot) -> CanonicalTaskPlan:
        if snapshot.schema_version == 1:
            return self._normalize_v1(snapshot)
        if snapshot.schema_version in {2, 3, 4}:
            return self._normalize_v2(snapshot)
        raise ValueError(f"unsupported plan schema version: {snapshot.schema_version}")

    def _normalize_v1(self, snapshot: TaskPlanSnapshot) -> CanonicalTaskPlan:
        slots = {slot.key: slot for slot in snapshot.worker_slots}
        subtasks = tuple(
            SubtaskPlanItem(
                key=item.key,
                task_id=item.task_id,
                kind=item.kind,
                title=item.title,
                goal_summary=item.goal_summary,
                acceptance_refs=item.acceptance_refs,
                milestone_key=item.milestone_key,
                expected_outputs=item.expected_outputs,
                source_refs=item.source_refs,
            )
            for item in snapshot.work_items
        )
        worker_items = tuple(
            CanonicalWorkerItemPlan(
                key=f"{item.key}:{item.worker_slot_key}",
                worker_item_id=None,
                legacy_identity_key=f"legacy:{item.key}:{item.worker_slot_key}",
                subtask_key=item.key,
                title=item.title,
                role=slots[item.worker_slot_key].role,
                responsibility=item.goal_summary,
                capability_requirements=slots[
                    item.worker_slot_key
                ].capability_requirements,
                worker_policy_ref=slots[item.worker_slot_key].worker_policy_ref,
                configuration_ref=slots[item.worker_slot_key].configuration_ref,
                concurrency_limit=slots[item.worker_slot_key].concurrency_limit,
                source_refs=item.source_refs,
            )
            for item in snapshot.work_items
            if item.worker_slot_key is not None
        )
        subtask_keys = {item.key for item in snapshot.work_items}
        milestones = tuple(
            SubtaskMilestone(
                key=milestone.key,
                title=milestone.title,
                subtask_keys=tuple(
                    key for key in milestone.work_item_keys if key in subtask_keys
                ),
                acceptance_refs=milestone.acceptance_refs,
                order_hint=milestone.order_hint,
            )
            for milestone in snapshot.milestones
        )
        dependencies = tuple(
            SubtaskDependency(
                upstream_subtask_key=dependency.upstream_work_item_key,
                downstream_subtask_key=dependency.downstream_work_item_key,
                kind=dependency.kind,
                required_evidence_refs=dependency.required_evidence_refs,
                enforcement=dependency.enforcement,
            )
            for dependency in snapshot.dependencies
        )
        return CanonicalTaskPlan(
            snapshot_id=snapshot.id,
            schema_version=snapshot.schema_version,
            workspace_id=snapshot.workspace_id,
            root_task_id=snapshot.root_task_id,
            generation=snapshot.generation,
            title=snapshot.title,
            goal_summary=snapshot.goal_summary,
            acceptance_refs=snapshot.acceptance_refs,
            subtasks=subtasks,
            worker_items=worker_items,
            milestones=milestones,
            subtask_dependencies=dependencies,
            worker_item_dependencies=(),
        )

    def _normalize_v2(self, snapshot: TaskPlanSnapshot) -> CanonicalTaskPlan:
        return CanonicalTaskPlan(
            snapshot_id=snapshot.id,
            schema_version=snapshot.schema_version,
            workspace_id=snapshot.workspace_id,
            root_task_id=snapshot.root_task_id,
            generation=snapshot.generation,
            title=snapshot.title,
            goal_summary=snapshot.goal_summary,
            acceptance_refs=snapshot.acceptance_refs,
            subtasks=snapshot.subtasks,
            worker_items=tuple(
                CanonicalWorkerItemPlan(
                    key=item.key,
                    worker_item_id=item.worker_item_id,
                    legacy_identity_key=None,
                    subtask_key=item.subtask_key,
                    title=item.title,
                    role=item.role,
                    responsibility=item.responsibility,
                    capability_requirements=item.capability_requirements,
                    worker_policy_ref=item.worker_policy_ref,
                    configuration_ref=item.configuration_ref,
                    concurrency_limit=item.concurrency_limit,
                    source_refs=item.source_refs,
                )
                for item in snapshot.worker_item_plans
            ),
            milestones=snapshot.subtask_milestones,
            subtask_dependencies=snapshot.subtask_dependencies,
            worker_item_dependencies=snapshot.worker_item_dependencies,
        )
