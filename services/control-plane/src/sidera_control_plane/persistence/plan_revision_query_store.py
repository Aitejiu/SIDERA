from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from sidera_control_plane.application.plan_revision_queries import (
    AcceptedTransitionPolicyView,
    ActivePlanHeadView,
    PlanRevisionQueryStore,
    RevisionBaseView,
    RevisionDependencyChangeView,
    RevisionDiffView,
    RevisionFieldChangeView,
    RevisionFreshnessView,
    RevisionImpactCountsView,
    RevisionSubtaskChangeView,
    RevisionTargetDescriptorView,
    RevisionTransitionPreviewView,
    RevisionWorkerItemChangeView,
)
from sidera_control_plane.application.planning_queries import PlanningCollaborationView
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
    PlanningRevisionProposalPayloadV4,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.revision_diff import (
    DependencyChange,
    FieldChange,
    NormalizedRevisionDiff,
    SubtaskChange,
    WorkerItemChange,
    WorkerTransitionPreview,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    TaskModel,
)


class SqlAlchemyPlanRevisionQueryStore(PlanRevisionQueryStore):
    def __init__(self, database: Database) -> None:
        self._database = database

    def get_revision_freshness(
        self, workspace_id: UUID, request_id: UUID
    ) -> RevisionFreshnessView | None:
        with self._database.session() as session:
            request = session.scalar(
                select(PlanningRequestModel).where(
                    PlanningRequestModel.workspace_id == workspace_id,
                    PlanningRequestModel.id == request_id,
                )
            )
            if request is None or request.kind != "revision":
                return None
            return _revision_freshness(session, request)

    def get_revision_diff(
        self, workspace_id: UUID, request_id: UUID
    ) -> RevisionDiffView | None:
        with self._database.session() as session:
            request = session.scalar(
                select(PlanningRequestModel).where(
                    PlanningRequestModel.workspace_id == workspace_id,
                    PlanningRequestModel.id == request_id,
                    PlanningRequestModel.kind == "revision",
                )
            )
            if request is None or request.current_proposal_revision is None:
                return None
            proposal = session.scalar(
                select(PlanningProposalRevisionModel).where(
                    PlanningProposalRevisionModel.workspace_id == workspace_id,
                    PlanningProposalRevisionModel.planning_request_id == request_id,
                    PlanningProposalRevisionModel.revision
                    == request.current_proposal_revision,
                    PlanningProposalRevisionModel.kind == "revision",
                )
            )
            if proposal is None or request.base_plan_snapshot_id is None:
                return None
            base_snapshot_row = session.scalar(
                select(PlanSnapshotModel).where(
                    PlanSnapshotModel.workspace_id == workspace_id,
                    PlanSnapshotModel.id == request.base_plan_snapshot_id,
                    PlanSnapshotModel.root_task_id == request.root_task_id,
                )
            )
            if base_snapshot_row is None:
                return None
            base_snapshot = _snapshot(base_snapshot_row)
            canonical_payload = _mapping(
                proposal.canonical_payload, "proposal canonical_payload"
            )
            payload_mapping = _mapping(
                canonical_payload.get("proposal"), "proposal canonical payload"
            )
            payload = (
                PlanningRevisionProposalPayloadV4.from_mapping(payload_mapping)
                if proposal.proposal_schema_version == 4
                else PlanningRevisionProposalPayloadV3.from_mapping(payload_mapping)
            )
            diff = NormalizedRevisionDiff.from_mapping(dict(proposal.derived_diff))
            policy_request = (
                payload.collaboration_policy_request.to_mapping()
                if isinstance(payload, PlanningRevisionProposalPayloadV4)
                else None
            )
            collaboration = PlanningCollaborationView.from_request_mapping(
                policy_request,
                request.profile_snapshot,
                provider=_collaboration_runtime_provider(
                    session,
                    workspace_id,
                    request.profile_snapshot,
                    enabled=(
                        isinstance(payload, PlanningRevisionProposalPayloadV4)
                        and payload.collaboration_policy_request.mode.value == "enabled"
                    ),
                ),
            )
            return _revision_diff_view(
                request=request,
                proposal=proposal,
                base_snapshot=base_snapshot,
                payload=payload,
                diff=diff,
                freshness=_revision_freshness(session, request),
                collaboration=collaboration,
            )

    def get_active_plan(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> ActivePlanHeadView | None:
        with self._database.session() as session:
            head = session.scalar(
                select(RootTaskPlanHeadModel).where(
                    RootTaskPlanHeadModel.workspace_id == workspace_id,
                    RootTaskPlanHeadModel.root_task_id == root_task_id,
                )
            )
            if head is None:
                return None
            snapshot_row = session.scalar(
                select(PlanSnapshotModel).where(
                    PlanSnapshotModel.workspace_id == workspace_id,
                    PlanSnapshotModel.root_task_id == root_task_id,
                    PlanSnapshotModel.id == head.active_snapshot_id,
                )
            )
            if snapshot_row is None:
                return None
            snapshot = _snapshot(snapshot_row)
            return ActivePlanHeadView(
                workspace_id=head.workspace_id,
                root_task_id=head.root_task_id,
                active_snapshot_id=head.active_snapshot_id,
                active_generation=head.active_generation,
                active_snapshot_hash=head.active_snapshot_hash,
                version=head.version,
                updated_at=head.updated_at,
                snapshot=snapshot,
            )


def _revision_freshness(
    session: Any, request: PlanningRequestModel
) -> RevisionFreshnessView:
    base = _base_view(request)
    head = session.scalar(
        select(RootTaskPlanHeadModel).where(
            RootTaskPlanHeadModel.workspace_id == request.workspace_id,
            RootTaskPlanHeadModel.root_task_id == base.root_task_id,
        )
    )
    current_snapshot_id = head.active_snapshot_id if head is not None else None
    current_generation = head.active_generation if head is not None else None
    current_hash = head.active_snapshot_hash if head is not None else None
    current_root_version = session.scalar(
        select(TaskModel.version).where(
            TaskModel.workspace_id == request.workspace_id,
            TaskModel.id == base.root_task_id,
        )
    )
    stale_reasons: list[str] = []
    if head is None:
        stale_reasons.append("active_head_missing")
    elif (
        head.active_snapshot_id != base.plan_snapshot_id
        or head.active_generation != base.plan_generation
        or head.active_snapshot_hash != base.plan_content_hash
    ):
        stale_reasons.append("active_plan_changed")
    if current_root_version is None:
        stale_reasons.append("root_task_missing")
    elif current_root_version != base.root_task_version:
        stale_reasons.append("root_task_changed")
    if head is not None:
        active_snapshot_exists = session.scalar(
            select(PlanSnapshotModel.id).where(
                PlanSnapshotModel.workspace_id == request.workspace_id,
                PlanSnapshotModel.root_task_id == base.root_task_id,
                PlanSnapshotModel.id == head.active_snapshot_id,
            )
        )
        if active_snapshot_exists is None:
            stale_reasons.append("active_snapshot_missing")
    return RevisionFreshnessView(
        base=base,
        current_plan_snapshot_id=current_snapshot_id,
        current_plan_generation=current_generation,
        current_plan_content_hash=current_hash,
        current_root_task_version=current_root_version,
        base_fresh=not stale_reasons,
        stale_reasons=tuple(stale_reasons),
        checked_at=datetime.now(UTC),
    )


def _base_view(request: PlanningRequestModel) -> RevisionBaseView:
    if (
        request.root_task_id is None
        or request.base_plan_snapshot_id is None
        or request.base_plan_generation is None
        or request.base_plan_content_hash is None
        or request.base_root_task_version is None
        or request.base_captured_at is None
    ):
        raise ValueError("revision request does not contain a complete base")
    return RevisionBaseView(
        schema_version=1,
        workspace_id=request.workspace_id,
        root_task_id=request.root_task_id,
        plan_snapshot_id=request.base_plan_snapshot_id,
        plan_generation=request.base_plan_generation,
        plan_content_hash=request.base_plan_content_hash,
        root_task_version=request.base_root_task_version,
        captured_at=request.base_captured_at,
    )


def _revision_diff_view(
    *,
    request: PlanningRequestModel,
    proposal: PlanningProposalRevisionModel,
    base_snapshot: TaskPlanSnapshot,
    payload: PlanningRevisionProposalPayloadV3 | PlanningRevisionProposalPayloadV4,
    diff: NormalizedRevisionDiff,
    freshness: RevisionFreshnessView,
    collaboration: PlanningCollaborationView,
) -> RevisionDiffView:
    base = freshness.base
    subtask_paths, worker_paths = _path_maps(base_snapshot, payload)
    return RevisionDiffView(
        workspace_id=request.workspace_id,
        root_task_id=base.root_task_id,
        planning_request_id=request.id,
        proposal_revision_id=proposal.id,
        proposal_revision=proposal.revision,
        proposal_content_hash=proposal.content_hash,
        base=base,
        target_descriptor_preview=RevisionTargetDescriptorView(
            target_generation=diff.target_descriptor_preview.target_generation,
            parent_snapshot_id=diff.target_descriptor_preview.parent_snapshot_id,
            target_plan_content_hash=(
                diff.target_descriptor_preview.target_plan_content_hash
            ),
        ),
        root_changes=tuple(_field_view(item) for item in diff.root_changes),
        collaboration_policy_changes=tuple(
            _field_view(item) for item in diff.collaboration_policy_changes
        ),
        collaboration=collaboration,
        subtask_changes=tuple(
            _subtask_view(item, subtask_paths) for item in diff.subtask_changes
        ),
        worker_item_changes=tuple(
            _worker_view(item, worker_paths) for item in diff.worker_changes
        ),
        subtask_dependency_changes=tuple(
            _dependency_view(item, subtask_paths, worker_paths)
            for item in diff.subtask_dependency_changes
        ),
        worker_dependency_changes=tuple(
            _dependency_view(item, subtask_paths, worker_paths)
            for item in diff.worker_dependency_changes
        ),
        transition_preview=tuple(
            _transition_view(item, worker_paths) for item in diff.transition_preview
        ),
        impact_counts=RevisionImpactCountsView(
            preserve=diff.impact_counts.preserve,
            move=diff.impact_counts.move,
            patch=diff.impact_counts.patch,
            continue_=diff.impact_counts.continue_,
            create=diff.impact_counts.create,
            derive=diff.impact_counts.derive,
            retire=diff.impact_counts.retire,
            affected_branches=diff.impact_counts.affected_branches,
        ),
        affected_branch_keys=diff.affected_branch_keys,
        affected_branch_paths=tuple(
            _affected_path(key, worker_paths) for key in diff.affected_branch_keys
        ),
        risk_flags=diff.risk_flags,
        diff_hash=diff.diff_hash,
        accepted_transition_policy=AcceptedTransitionPolicyView(
            activate_immediately=payload.impact_policy.activate_immediately,
            budget_level=payload.impact_policy.budget_level,
            failure_policy=payload.impact_policy.failure_policy,
            manual_gate=payload.impact_policy.manual_gate,
            revision_operation=payload.impact_policy.revision_operation,
        ),
        freshness=freshness,
    )


def _collaboration_runtime_provider(
    session: Any,
    workspace_id: UUID,
    profile_snapshot: object,
    *,
    enabled: bool,
) -> str | None:
    if not enabled:
        return None
    snapshot = _mapping(profile_snapshot, "profile snapshot")
    binding = _mapping(
        snapshot.get("collaboration_runtime_binding"),
        "collaboration runtime binding",
    )
    raw_definition_id = binding.get("runtime_definition_id")
    try:
        definition_id = UUID(str(raw_definition_id))
    except (TypeError, ValueError):
        return None
    provider = session.scalar(
        select(ModuleDefinitionModel.provider).where(
            ModuleDefinitionModel.workspace_id == workspace_id,
            ModuleDefinitionModel.id == definition_id,
        )
    )
    return provider if isinstance(provider, str) and provider.strip() else None


def _path_maps(
    base_snapshot: TaskPlanSnapshot,
    payload: PlanningRevisionProposalPayloadV3 | PlanningRevisionProposalPayloadV4,
) -> tuple[dict[str, str], dict[str, str]]:
    subtask_titles = {item.key: item.title for item in base_snapshot.subtasks}
    subtask_titles.update({item.key: item.title for item in payload.subtasks})
    target_worker_items = {item.key: item for item in payload.worker_items}
    base_worker_items = {item.key: item for item in base_snapshot.worker_item_plans}
    worker_items = {**base_worker_items, **target_worker_items}
    root_title = payload.root_task.title
    subtask_paths = {
        key: f"Task/{root_title} > Subtask/{title}"
        for key, title in subtask_titles.items()
    }
    worker_paths = {
        key: (
            f"{subtask_paths.get(item.subtask_key, f'Subtask/{item.subtask_key}')}"
            f" > WorkerItem/{item.title}"
        )
        for key, item in worker_items.items()
    }
    return subtask_paths, worker_paths


def _field_view(value: FieldChange) -> RevisionFieldChangeView:
    return RevisionFieldChangeView(
        field_path=value.field_path,
        before=value.before,
        after=value.after,
    )


def _subtask_view(
    value: SubtaskChange, paths: Mapping[str, str]
) -> RevisionSubtaskChangeView:
    return RevisionSubtaskChangeView(
        target_subtask_key=value.target_subtask_key,
        path=paths.get(
            value.target_subtask_key,
            f"Subtask/{value.target_subtask_key}",
        ),
        existing_task_id=value.existing_task_id,
        strategy=value.strategy.value,
        field_changes=tuple(_field_view(item) for item in value.field_changes),
        reason=value.reason,
    )


def _worker_view(
    value: WorkerItemChange, paths: Mapping[str, str]
) -> RevisionWorkerItemChangeView:
    return RevisionWorkerItemChangeView(
        target_worker_item_key=value.target_worker_item_key,
        path=paths.get(
            value.target_worker_item_key,
            f"WorkerItem/{value.target_worker_item_key}",
        ),
        existing_worker_item_id=value.existing_worker_item_id,
        strategy=value.strategy.value,
        from_subtask_key=value.from_subtask_key,
        to_subtask_key=value.to_subtask_key,
        context_disposition=value.context_disposition.value,
        field_changes=tuple(_field_view(item) for item in value.field_changes),
        reasons=value.reasons,
    )


def _dependency_view(
    value: DependencyChange,
    subtask_paths: Mapping[str, str],
    worker_paths: Mapping[str, str],
) -> RevisionDependencyChangeView:
    paths = subtask_paths if value.graph_scope == "subtask" else worker_paths
    return RevisionDependencyChangeView(
        graph_scope=value.graph_scope,
        upstream_key=value.upstream_key,
        downstream_key=value.downstream_key,
        upstream_path=paths.get(value.upstream_key, value.upstream_key),
        downstream_path=paths.get(value.downstream_key, value.downstream_key),
        dependency_kind=value.dependency_kind,
        change_type=value.change_type,
    )


def _transition_view(
    value: WorkerTransitionPreview, paths: Mapping[str, str]
) -> RevisionTransitionPreviewView:
    return RevisionTransitionPreviewView(
        target_worker_item_key=value.target_worker_item_key,
        path=paths.get(
            value.target_worker_item_key,
            f"WorkerItem/{value.target_worker_item_key}",
        ),
        worker_item_id=value.worker_item_id,
        allowed_actions=tuple(item.value for item in value.allowed_actions),
        fallback_action=(
            value.fallback_action.value if value.fallback_action is not None else None
        ),
        reason_codes=value.reason_codes,
    )


def _affected_path(key: str, worker_paths: Mapping[str, str]) -> str:
    if key.startswith("retired:"):
        return f"Retired WorkerItem/{key.removeprefix('retired:')}"
    return worker_paths.get(key, f"WorkerItem/{key}")


def _snapshot(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if (
        snapshot.id != row.id
        or snapshot.workspace_id != row.workspace_id
        or snapshot.root_task_id != row.root_task_id
        or snapshot.generation != row.generation
    ):
        raise ValueError("task plan snapshot payload does not match identity columns")
    return snapshot


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value
