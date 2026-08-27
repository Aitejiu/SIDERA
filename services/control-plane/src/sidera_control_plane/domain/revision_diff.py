from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.plan_revisions import (
    ContextDisposition,
    IdentityStrategy,
    RevisionBaseDescriptorV1,
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot


@dataclass(frozen=True, slots=True)
class TargetPlanDescriptorPreview:
    target_generation: int
    parent_snapshot_id: UUID
    target_plan_content_hash: str


@dataclass(frozen=True, slots=True)
class FieldChange:
    field_path: str
    before: object
    after: object


@dataclass(frozen=True, slots=True)
class SubtaskChange:
    target_subtask_key: str
    existing_task_id: UUID | None
    strategy: IdentityStrategy
    field_changes: tuple[FieldChange, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class WorkerItemChange:
    target_worker_item_key: str
    existing_worker_item_id: UUID | None
    strategy: IdentityStrategy
    from_subtask_key: str | None
    to_subtask_key: str
    context_disposition: ContextDisposition
    field_changes: tuple[FieldChange, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DependencyChange:
    graph_scope: str
    upstream_key: str
    downstream_key: str
    dependency_kind: str
    change_type: str


@dataclass(frozen=True, slots=True)
class WorkerTransitionPreview:
    target_worker_item_key: str
    worker_item_id: UUID | None
    allowed_actions: tuple[WorkerTransitionAction, ...]
    fallback_action: WorkerTransitionAction | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RevisionImpactCounts:
    preserve: int
    move: int
    patch: int
    continue_: int
    create: int
    derive: int
    retire: int
    affected_branches: int


@dataclass(frozen=True, slots=True)
class NormalizedRevisionDiff:
    base_descriptor: RevisionBaseDescriptorV1
    target_descriptor_preview: TargetPlanDescriptorPreview
    root_changes: tuple[FieldChange, ...]
    subtask_changes: tuple[SubtaskChange, ...]
    worker_changes: tuple[WorkerItemChange, ...]
    subtask_dependency_changes: tuple[DependencyChange, ...]
    worker_dependency_changes: tuple[DependencyChange, ...]
    transition_preview: tuple[WorkerTransitionPreview, ...]
    impact_counts: RevisionImpactCounts
    affected_branch_keys: tuple[str, ...]
    risk_flags: tuple[str, ...]
    diff_hash: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "base_descriptor": self.base_descriptor.to_mapping(),
            "target_descriptor_preview": {
                "target_generation": self.target_descriptor_preview.target_generation,
                "parent_snapshot_id": str(
                    self.target_descriptor_preview.parent_snapshot_id
                ),
                "target_plan_content_hash": self.target_descriptor_preview.target_plan_content_hash,
            },
            "root_changes": [_field_mapping(item) for item in self.root_changes],
            "subtask_changes": [_subtask_change_mapping(item) for item in self.subtask_changes],
            "worker_changes": [_worker_change_mapping(item) for item in self.worker_changes],
            "subtask_dependency_changes": [
                _dependency_change_mapping(item)
                for item in self.subtask_dependency_changes
            ],
            "worker_dependency_changes": [
                _dependency_change_mapping(item)
                for item in self.worker_dependency_changes
            ],
            "transition_preview": [
                _transition_mapping(item) for item in self.transition_preview
            ],
            "impact_counts": {
                "preserve": self.impact_counts.preserve,
                "move": self.impact_counts.move,
                "patch": self.impact_counts.patch,
                "continue": self.impact_counts.continue_,
                "create": self.impact_counts.create,
                "derive": self.impact_counts.derive,
                "retire": self.impact_counts.retire,
                "affected_branches": self.impact_counts.affected_branches,
            },
            "affected_branch_keys": list(self.affected_branch_keys),
            "risk_flags": list(self.risk_flags),
        }


class RevisionDiffNormalizer:
    def normalize(
        self,
        base_plan: TaskPlanSnapshot,
        payload: PlanningRevisionProposalPayloadV3,
    ) -> NormalizedRevisionDiff:
        payload.validate_against_base(base_plan)
        base = payload.base_descriptor
        if base.plan_content_hash != plan_snapshot_content_hash(base_plan):
            raise ValidationError("revision base content hash does not match snapshot")

        base_subtasks_by_key = {item.key: item for item in base_plan.subtasks}
        base_subtasks_by_id = {
            item.task_id: item
            for item in base_plan.subtasks
            if item.task_id is not None
        }
        target_subtasks = {item.key: item for item in payload.subtasks}
        subtask_decisions = {
            item.target_subtask_key: item
            for item in payload.subtask_identity_decisions
        }
        base_workers = {
            item.worker_item_id: item
            for item in base_plan.worker_item_plans
            if item.worker_item_id is not None
        }
        target_workers = {item.key: item for item in payload.worker_items}
        worker_decisions = {
            item.target_worker_item_key: item
            for item in payload.worker_item_identity_decisions
        }

        root_changes = _root_changes(base_plan, payload)
        subtask_changes = tuple(
            _subtask_change(target_subtasks[key], base_subtasks_by_id, subtask_decisions[key])
            for key in sorted(target_subtasks)
        )
        worker_changes = tuple(
            _worker_change(
                target_workers[key],
                base_workers,
                worker_decisions[key],
                base_subtasks_by_key,
                payload,
            )
            for key in sorted(target_workers)
        )
        _validate_context_dispositions(root_changes, subtask_changes, worker_changes)
        subtask_dependency_changes = _dependency_changes(
            "subtask",
            _base_subtask_edges(base_plan),
            _target_subtask_edges(payload),
        )
        worker_dependency_changes = _dependency_changes(
            "worker_item",
            _base_worker_edges(base_plan),
            _target_worker_edges(payload),
        )
        transitions = _transition_preview(
            payload,
            worker_changes,
        )
        affected = tuple(
            sorted(
                {
                    item.target_worker_item_key
                    for item in transitions
                    if item.reason_codes
                }
                | {
                    f"retired:{item}"
                    for item in sorted(payload.retired_worker_item_ids, key=str)
                }
            )
        )
        counts = _impact_counts(transitions, payload, affected)
        risks = _risk_flags(
            root_changes,
            subtask_changes,
            worker_changes,
            subtask_dependency_changes,
            worker_dependency_changes,
            payload,
        )
        target_hash = _target_plan_hash(payload)
        preview = TargetPlanDescriptorPreview(
            target_generation=base_plan.generation + 1,
            parent_snapshot_id=base_plan.id,
            target_plan_content_hash=target_hash,
        )
        diff = NormalizedRevisionDiff(
            base_descriptor=base,
            target_descriptor_preview=preview,
            root_changes=root_changes,
            subtask_changes=subtask_changes,
            worker_changes=worker_changes,
            subtask_dependency_changes=subtask_dependency_changes,
            worker_dependency_changes=worker_dependency_changes,
            transition_preview=transitions,
            impact_counts=counts,
            affected_branch_keys=affected,
            risk_flags=risks,
            diff_hash="",
        )
        diff_hash = hashlib.sha256(
            json.dumps(
                diff.to_mapping(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return NormalizedRevisionDiff(
            base_descriptor=diff.base_descriptor,
            target_descriptor_preview=diff.target_descriptor_preview,
            root_changes=diff.root_changes,
            subtask_changes=diff.subtask_changes,
            worker_changes=diff.worker_changes,
            subtask_dependency_changes=diff.subtask_dependency_changes,
            worker_dependency_changes=diff.worker_dependency_changes,
            transition_preview=diff.transition_preview,
            impact_counts=diff.impact_counts,
            affected_branch_keys=diff.affected_branch_keys,
            risk_flags=diff.risk_flags,
            diff_hash=diff_hash,
        )


def _root_changes(
    base: TaskPlanSnapshot, payload: PlanningRevisionProposalPayloadV3
) -> tuple[FieldChange, ...]:
    values = (
        ("title", base.title, payload.root_task.title),
        ("goal_summary", base.goal_summary, payload.root_task.goal_summary),
        (
            "acceptance_refs",
            base.acceptance_refs,
            payload.root_task.acceptance_refs,
        ),
    )
    return tuple(
        FieldChange(path, before, after)
        for path, before, after in values
        if before != after
    )


def _subtask_change(
    target: Any,
    base: Mapping[UUID, Any],
    decision: Any,
) -> SubtaskChange:
    before = base.get(decision.existing_subtask_task_id)
    changes: tuple[FieldChange, ...] = ()
    if before is not None:
        changes = _field_changes(
            (
                ("kind", before.kind.value, target.kind.value),
                ("title", before.title, target.title),
                ("goal_summary", before.goal_summary, target.goal_summary),
                ("acceptance_refs", before.acceptance_refs, target.acceptance_refs),
                ("expected_outputs", before.expected_outputs, target.expected_outputs),
                ("milestone_key", before.milestone_key, target.milestone_key),
                ("source_refs", before.source_refs, target.source_refs),
            )
        )
    return SubtaskChange(
        target_subtask_key=target.key,
        existing_task_id=decision.existing_subtask_task_id,
        strategy=decision.strategy,
        field_changes=changes,
        reason=decision.reason,
    )


def _worker_change(
    target: Any,
    base: Mapping[UUID, Any],
    decision: Any,
    base_subtasks: Mapping[str, Any],
    payload: PlanningRevisionProposalPayloadV3,
) -> WorkerItemChange:
    before = base.get(decision.existing_worker_item_id)
    from_subtask = before.subtask_key if before is not None else None
    changes: tuple[FieldChange, ...] = ()
    if before is not None:
        changes = _field_changes(
            (
                ("subtask_key", before.subtask_key, target.subtask_key),
                ("title", before.title, target.title),
                ("role", before.role, target.role),
                ("responsibility", before.responsibility, target.responsibility),
                (
                    "capability_requirements",
                    before.capability_requirements,
                    target.capability_requirements,
                ),
                ("worker_policy_ref", before.worker_policy_ref, target.worker_policy_ref),
                ("configuration_ref", before.configuration_ref, target.configuration_ref),
                ("concurrency_limit", before.concurrency_limit, target.concurrency_limit),
                ("source_refs", before.source_refs, target.source_refs),
            )
        )
        existing_subtask = base_subtasks.get(before.subtask_key)
        target_subtask = next(
            item for item in payload.subtasks if item.key == target.subtask_key
        )
        changes = changes + _field_changes(
            (
                (
                    "subtask.goal_summary",
                    existing_subtask.goal_summary if existing_subtask else None,
                    target_subtask.goal_summary,
                ),
                (
                    "subtask.acceptance_refs",
                    existing_subtask.acceptance_refs if existing_subtask else None,
                    target_subtask.acceptance_refs,
                ),
                (
                    "root.goal_summary",
                    None,
                    payload.root_task.goal_summary,
                ),
            )
        )
    return WorkerItemChange(
        target_worker_item_key=target.key,
        existing_worker_item_id=decision.existing_worker_item_id,
        strategy=decision.strategy,
        from_subtask_key=from_subtask,
        to_subtask_key=target.subtask_key,
        context_disposition=decision.context_disposition,
        field_changes=changes,
        reasons=decision.change_reasons,
    )


def _field_changes(
    values: tuple[tuple[str, object, object], ...]
) -> tuple[FieldChange, ...]:
    return tuple(
        FieldChange(path, before, after)
        for path, before, after in values
        if before != after
    )


def _validate_context_dispositions(
    root_changes: tuple[FieldChange, ...],
    subtask_changes: tuple[SubtaskChange, ...],
    worker_changes: tuple[WorkerItemChange, ...],
) -> None:
    context_changed = bool(root_changes)
    changed_subtasks = {
        item.target_subtask_key
        for item in subtask_changes
        if item.field_changes
    }
    for item in worker_changes:
        changed = bool(item.field_changes) or item.to_subtask_key in changed_subtasks
        if context_changed:
            changed = True
        if (
            changed
            and item.context_disposition is ContextDisposition.UNCHANGED
        ):
            raise ValidationError(
                "context fingerprint changed but disposition is unchanged"
            )


def _transition_preview(
    payload: PlanningRevisionProposalPayloadV3,
    worker_changes: tuple[WorkerItemChange, ...],
) -> tuple[WorkerTransitionPreview, ...]:
    result: list[WorkerTransitionPreview] = []
    for item in worker_changes:
        reasons = list(item.reasons)
        if item.strategy is IdentityStrategy.CREATE:
            actions = (WorkerTransitionAction.CREATE,)
            fallback = None
            reasons.append("new_worker_item")
        elif item.strategy is IdentityStrategy.DERIVE:
            actions = (WorkerTransitionAction.CREATE,)
            fallback = None
            reasons.append("derived_worker_item")
        elif item.context_disposition is ContextDisposition.PATCH_REQUIRED:
            actions = (WorkerTransitionAction.PATCH, WorkerTransitionAction.CONTINUE)
            fallback = WorkerTransitionAction.CONTINUE
            reasons.append("context_patch_required")
        elif item.from_subtask_key != item.to_subtask_key:
            actions = (WorkerTransitionAction.MOVE,)
            fallback = None
            reasons.append("subtask_moved")
        elif item.field_changes:
            actions = (WorkerTransitionAction.PATCH, WorkerTransitionAction.CONTINUE)
            fallback = WorkerTransitionAction.CONTINUE
            reasons.append("context_changed")
        else:
            actions = (WorkerTransitionAction.PRESERVE,)
            fallback = None
        result.append(
            WorkerTransitionPreview(
                target_worker_item_key=item.target_worker_item_key,
                worker_item_id=item.existing_worker_item_id,
                allowed_actions=actions,
                fallback_action=fallback,
                reason_codes=tuple(sorted(set(reasons))),
            )
        )
    for worker_id in sorted(payload.retired_worker_item_ids, key=str):
        result.append(
            WorkerTransitionPreview(
                target_worker_item_key=f"retired:{worker_id}",
                worker_item_id=worker_id,
                allowed_actions=(WorkerTransitionAction.RETIRE,),
                fallback_action=None,
                reason_codes=("retired_worker_item",),
            )
        )
    return tuple(sorted(result, key=lambda item: item.target_worker_item_key))


def _impact_counts(
    transitions: tuple[WorkerTransitionPreview, ...],
    payload: PlanningRevisionProposalPayloadV3,
    affected: tuple[str, ...],
) -> RevisionImpactCounts:
    return RevisionImpactCounts(
        preserve=sum(WorkerTransitionAction.PRESERVE in item.allowed_actions for item in transitions),
        move=sum(WorkerTransitionAction.MOVE in item.allowed_actions for item in transitions),
        patch=sum(WorkerTransitionAction.PATCH in item.allowed_actions for item in transitions),
        continue_=sum(WorkerTransitionAction.CONTINUE in item.allowed_actions for item in transitions),
        create=sum(
            WorkerTransitionAction.CREATE in item.allowed_actions
            and item.worker_item_id is None
            for item in transitions
        ),
        derive=sum(
            1
            for item in payload.worker_item_identity_decisions
            if item.strategy is IdentityStrategy.DERIVE
        ),
        retire=sum(WorkerTransitionAction.RETIRE in item.allowed_actions for item in transitions),
        affected_branches=len(affected),
    )


def _risk_flags(
    root_changes: tuple[FieldChange, ...],
    subtask_changes: tuple[SubtaskChange, ...],
    worker_changes: tuple[WorkerItemChange, ...],
    subtask_dependencies: tuple[DependencyChange, ...],
    worker_dependencies: tuple[DependencyChange, ...],
    payload: PlanningRevisionProposalPayloadV3,
) -> tuple[str, ...]:
    risks: set[str] = set()
    if root_changes:
        risks.add("root_context_changed")
    if any(item.context_disposition is ContextDisposition.PATCH_REQUIRED for item in worker_changes):
        risks.add("context_patch_required")
    if any(item.strategy is IdentityStrategy.DERIVE for item in worker_changes):
        risks.add("worker_item_derived")
    if payload.retired_worker_item_ids:
        risks.add("worker_item_retired")
    if subtask_dependencies or worker_dependencies:
        risks.add("dependency_graph_changed")
    if any(item.field_changes for item in subtask_changes):
        risks.add("subtask_context_changed")
    return tuple(sorted(risks))


def _base_subtask_edges(snapshot: TaskPlanSnapshot) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (
                item.upstream_subtask_key,
                item.downstream_subtask_key,
                item.kind.value,
            )
            for item in snapshot.subtask_dependencies
        )
    )


def _target_subtask_edges(payload: PlanningRevisionProposalPayloadV3) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (
                item.upstream_subtask_key,
                item.downstream_subtask_key,
                item.kind.value,
            )
            for item in payload.subtask_dependencies
        )
    )


def _base_worker_edges(snapshot: TaskPlanSnapshot) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (
                item.upstream_worker_item_key,
                item.downstream_worker_item_key,
                item.kind,
            )
            for item in snapshot.worker_item_dependencies
        )
    )


def _target_worker_edges(payload: PlanningRevisionProposalPayloadV3) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (
                item.upstream_worker_item_key,
                item.downstream_worker_item_key,
                item.kind,
            )
            for item in payload.worker_item_dependencies
        )
    )


def _dependency_changes(
    scope: str,
    before: tuple[tuple[str, str, str], ...],
    after: tuple[tuple[str, str, str], ...],
) -> tuple[DependencyChange, ...]:
    before_set = set(before)
    after_set = set(after)
    changes: list[DependencyChange] = []
    for upstream, downstream, kind in sorted(before_set | after_set):
        if (upstream, downstream, kind) in before_set and (upstream, downstream, kind) in after_set:
            continue
        changes.append(
            DependencyChange(
                graph_scope=scope,
                upstream_key=upstream,
                downstream_key=downstream,
                dependency_kind=kind,
                change_type="added"
                if (upstream, downstream, kind) in after_set
                else "removed",
            )
        )
    return tuple(changes)


def _target_plan_hash(payload: PlanningRevisionProposalPayloadV3) -> str:
    target = payload.canonical_payload()
    target.pop("decision_summary", None)
    target.pop("impact_policy", None)
    target.pop("base_descriptor", None)
    return hashlib.sha256(
        json.dumps(
            target,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _field_mapping(value: FieldChange) -> dict[str, object]:
    return {"field_path": value.field_path, "before": value.before, "after": value.after}


def _subtask_change_mapping(value: SubtaskChange) -> dict[str, object]:
    return {
        "target_subtask_key": value.target_subtask_key,
        "existing_task_id": str(value.existing_task_id) if value.existing_task_id else None,
        "strategy": value.strategy.value,
        "field_changes": [_field_mapping(item) for item in value.field_changes],
        "reason": value.reason,
    }


def _worker_change_mapping(value: WorkerItemChange) -> dict[str, object]:
    return {
        "target_worker_item_key": value.target_worker_item_key,
        "existing_worker_item_id": str(value.existing_worker_item_id) if value.existing_worker_item_id else None,
        "strategy": value.strategy.value,
        "from_subtask_key": value.from_subtask_key,
        "to_subtask_key": value.to_subtask_key,
        "context_disposition": value.context_disposition.value,
        "field_changes": [_field_mapping(item) for item in value.field_changes],
        "reasons": list(value.reasons),
    }


def _dependency_change_mapping(value: DependencyChange) -> dict[str, object]:
    return {
        "graph_scope": value.graph_scope,
        "upstream_key": value.upstream_key,
        "downstream_key": value.downstream_key,
        "dependency_kind": value.dependency_kind,
        "change_type": value.change_type,
    }


def _transition_mapping(value: WorkerTransitionPreview) -> dict[str, object]:
    return {
        "target_worker_item_key": value.target_worker_item_key,
        "worker_item_id": str(value.worker_item_id) if value.worker_item_id else None,
        "allowed_actions": [item.value for item in value.allowed_actions],
        "fallback_action": value.fallback_action.value if value.fallback_action else None,
        "reason_codes": list(value.reason_codes),
    }
