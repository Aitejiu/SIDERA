from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from sidera_control_plane.domain.plan_convergence_projections import (
    PlanConvergenceFacts,
    PlanHeadProjection,
    PlanRevisionProjection,
    PlanTransitionProjectionFact,
    WorkerTransitionProjectionFact,
)
from sidera_control_plane.domain.plan_normalization import CanonicalTaskPlan
from sidera_control_plane.persistence.models import (
    ContextPatchAcknowledgementModel,
    ContextPatchModel,
    ExecutionInputModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanTransitionModel,
    RootTaskPlanHeadModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerTransitionModel,
)


def load_plan_convergence_facts(
    session: Session,
    *,
    workspace_id: UUID,
    root_task_id: UUID,
    plan: CanonicalTaskPlan | None = None,
    worker_item_ids: Sequence[UUID] | None = None,
) -> PlanConvergenceFacts:
    """Load the revision and transition read model in the caller's snapshot.

    ``worker_item_ids`` limits the worker rows for a WorkerItem query.  The
    plan transition itself is still selected from the root task so all views
    describe the same transition generation.
    """

    head_row = session.scalar(
        select(RootTaskPlanHeadModel).where(
            RootTaskPlanHeadModel.workspace_id == workspace_id,
            RootTaskPlanHeadModel.root_task_id == root_task_id,
        )
    )
    head = (
        PlanHeadProjection(
            active_snapshot_id=head_row.active_snapshot_id,
            active_generation=head_row.active_generation,
            active_snapshot_hash=head_row.active_snapshot_hash,
            version=head_row.version,
            updated_at=_as_utc(head_row.updated_at),
        )
        if head_row is not None
        else None
    )

    request = session.scalar(
        select(PlanningRequestModel)
        .where(
            PlanningRequestModel.workspace_id == workspace_id,
            PlanningRequestModel.root_task_id == root_task_id,
            PlanningRequestModel.kind == "revision",
        )
        .order_by(
            PlanningRequestModel.updated_at.desc(), PlanningRequestModel.id.desc()
        )
        .limit(1)
    )
    proposal = None
    if request is not None and request.current_proposal_revision is not None:
        proposal = session.scalar(
            select(PlanningProposalRevisionModel).where(
                PlanningProposalRevisionModel.workspace_id == workspace_id,
                PlanningProposalRevisionModel.planning_request_id == request.id,
                PlanningProposalRevisionModel.revision == request.current_proposal_revision,
            )
        )

    transition_row = session.scalar(
        select(PlanTransitionModel)
        .where(
            PlanTransitionModel.workspace_id == workspace_id,
            PlanTransitionModel.root_task_id == root_task_id,
        )
        .order_by(PlanTransitionModel.started_at.desc(), PlanTransitionModel.id.desc())
        .limit(1)
    )
    transition = (
        PlanTransitionProjectionFact(
            id=transition_row.id,
            from_generation=transition_row.from_generation,
            to_generation=transition_row.to_generation,
            status=transition_row.status,
            started_at=_as_utc(transition_row.started_at),
            completed_at=_as_utc_optional(transition_row.completed_at),
            version=transition_row.version,
        )
        if transition_row is not None
        else None
    )

    worker_rows: list[WorkerTransitionModel] = []
    if transition_row is not None:
        statement = select(WorkerTransitionModel).where(
            WorkerTransitionModel.workspace_id == workspace_id,
            WorkerTransitionModel.plan_transition_id == transition_row.id,
        )
        if worker_item_ids is not None:
            statement = statement.where(
                WorkerTransitionModel.worker_item_id.in_(
                    tuple(worker_item_ids) or (UUID(int=0),)
                )
            )
        worker_rows = list(
            session.scalars(
                statement.order_by(
                    WorkerTransitionModel.created_at.desc(),
                    WorkerTransitionModel.id.desc(),
                )
            ).all()
        )

    transition_worker_ids = {row.worker_item_id for row in worker_rows}
    requested_worker_ids = set(worker_item_ids or ())
    item_ids = transition_worker_ids | requested_worker_ids
    item_rows = session.scalars(
        select(WorkerItemModel).where(
            WorkerItemModel.workspace_id == workspace_id,
            WorkerItemModel.id.in_(tuple(item_ids) or (UUID(int=0),)),
        )
    ).all()
    item_by_id = {row.id: row for row in item_rows}

    placement_rows = _load_placements(
        session,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        worker_item_ids=item_ids,
        snapshot_ids=(
            {
                snapshot_id
                for snapshot_id in (
                    head_row.active_snapshot_id if head_row is not None else None,
                    transition_row.from_snapshot_id if transition_row is not None else None,
                    transition_row.to_snapshot_id if transition_row is not None else None,
                )
                if snapshot_id is not None
            }
        ),
    )
    placement_by_worker = _select_placements(
        placement_rows,
        transition_row=transition_row,
        head_generation=head_row.active_generation if head_row is not None else None,
    )

    patch_by_transition, acknowledgement_by_patch = _load_patches(
        session,
        workspace_id=workspace_id,
        transition_ids={row.id for row in worker_rows},
    )
    input_ids = {
        execution_id
        for row in worker_rows
        for execution_id in (row.source_execution_id, row.target_execution_id)
        if execution_id is not None
    }
    input_rows = session.scalars(
        select(ExecutionInputModel).where(
            ExecutionInputModel.workspace_id == workspace_id,
            ExecutionInputModel.execution_id.in_(tuple(input_ids) or (UUID(int=0),)),
        )
    ).all()
    inputs_by_execution: dict[UUID, list[ExecutionInputModel]] = defaultdict(list)
    for row in input_rows:
        inputs_by_execution[row.execution_id].append(row)

    workers = tuple(
        _worker_fact(
            row,
            item_by_id.get(row.worker_item_id),
            placement_by_worker.get(row.worker_item_id),
            patch_by_transition.get(row.id),
            acknowledgement_by_patch,
            inputs_by_execution,
        )
        for row in worker_rows
    )
    revision = _revision_fact(
        request=request,
        proposal=proposal,
        plan=plan,
        transition=transition_row,
    )
    return PlanConvergenceFacts(
        head=head,
        revision=revision,
        transition=transition,
        workers=workers,
    )


def _revision_fact(
    *,
    request: PlanningRequestModel | None,
    proposal: PlanningProposalRevisionModel | None,
    plan: CanonicalTaskPlan | None,
    transition: PlanTransitionModel | None,
) -> PlanRevisionProjection | None:
    if request is None:
        return None
    derived_diff = _mapping(proposal.derived_diff) if proposal is not None else {}
    target_descriptor = _mapping(derived_diff.get("target_descriptor_preview"))
    target_generation = _positive_int(target_descriptor.get("target_generation"))
    if target_generation is None and transition is not None:
        target_generation = transition.to_generation
    return PlanRevisionProjection(
        planning_request_id=request.id,
        status=request.status,
        proposal_revision=request.current_proposal_revision,
        proposal_content_hash=request.current_proposal_content_hash,
        base_generation=request.base_plan_generation,
        target_generation=target_generation,
        version=request.version,
        correlation_id=request.correlation_id,
        updated_at=_as_utc(request.updated_at),
        subtask_path=_revision_subtask_path(plan, proposal),
        summary=_revision_summary(request.status),
        action=_revision_action(request.status),
    )


def _revision_subtask_path(
    plan: CanonicalTaskPlan | None,
    proposal: PlanningProposalRevisionModel | None,
) -> tuple[str, ...]:
    if proposal is None:
        return ()
    diff = _mapping(proposal.derived_diff)
    changed_keys: list[str] = []
    for entry in _sequence(diff.get("subtask_changes")):
        mapping = _mapping(entry)
        key = _string(mapping.get("target_subtask_key"))
        if key is not None:
            changed_keys.append(key)
    if not changed_keys:
        for entry in _sequence(diff.get("worker_changes")):
            mapping = _mapping(entry)
            key = _string(mapping.get("to_subtask_key"))
            if key is not None:
                changed_keys.append(key)
    if not changed_keys:
        worker_keys = {item.key: item.subtask_key for item in plan.worker_items} if plan else {}
        for value in _sequence(diff.get("affected_branch_keys")):
            key = _string(value)
            if key is None:
                continue
            changed_keys.append(worker_keys.get(key, key.removeprefix("retired:")))
    if not changed_keys:
        return ()
    titles = {item.key: item.title for item in plan.subtasks} if plan else {}
    proposal_payload = _mapping(_mapping(proposal.canonical_payload).get("proposal"))
    for entry in _sequence(proposal_payload.get("subtasks")):
        mapping = _mapping(entry)
        key = _string(mapping.get("key"))
        title = _string(mapping.get("title"))
        if key is not None and title is not None:
            titles[key] = title
    return tuple(
        titles.get(key, key)
        for key in dict.fromkeys(changed_keys)
    )


def _load_placements(
    session: Session,
    *,
    workspace_id: UUID,
    root_task_id: UUID,
    worker_item_ids: set[UUID],
    snapshot_ids: set[UUID],
) -> tuple[WorkerItemPlacementModel, ...]:
    if not worker_item_ids or not snapshot_ids:
        return ()
    return tuple(
        session.scalars(
            select(WorkerItemPlacementModel).where(
                WorkerItemPlacementModel.workspace_id == workspace_id,
                WorkerItemPlacementModel.root_task_id == root_task_id,
                WorkerItemPlacementModel.worker_item_id.in_(tuple(worker_item_ids)),
                WorkerItemPlacementModel.plan_snapshot_id.in_(tuple(snapshot_ids)),
            )
        ).all()
    )


def _select_placements(
    rows: Sequence[WorkerItemPlacementModel],
    *,
    transition_row: PlanTransitionModel | None,
    head_generation: int | None,
) -> dict[UUID, WorkerItemPlacementModel]:
    by_worker: dict[UUID, list[WorkerItemPlacementModel]] = defaultdict(list)
    for row in rows:
        by_worker[row.worker_item_id].append(row)
    preferred_snapshot_ids = tuple(
        snapshot_id
        for snapshot_id in (
            transition_row.to_snapshot_id if transition_row is not None else None,
            transition_row.from_snapshot_id if transition_row is not None else None,
        )
        if snapshot_id is not None
    )
    result: dict[UUID, WorkerItemPlacementModel] = {}
    for worker_id, values in by_worker.items():
        selected = next(
            (
                row
                for snapshot_id in preferred_snapshot_ids
                for row in values
                if row.plan_snapshot_id == snapshot_id
            ),
            None,
        )
        if selected is None and head_generation is not None:
            selected = next(
                (row for row in values if row.plan_generation == head_generation),
                None,
            )
        if selected is None:
            selected = max(values, key=lambda row: (row.plan_generation, str(row.plan_snapshot_id)))
        result[worker_id] = selected
    return result


def _load_patches(
    session: Session,
    *,
    workspace_id: UUID,
    transition_ids: set[UUID],
) -> tuple[
    dict[UUID, ContextPatchModel],
    dict[UUID, ContextPatchAcknowledgementModel],
]:
    if not transition_ids:
        return {}, {}
    patches = session.scalars(
        select(ContextPatchModel).where(
            ContextPatchModel.workspace_id == workspace_id,
            ContextPatchModel.worker_transition_id.in_(tuple(transition_ids)),
        )
    ).all()
    patch_by_transition = {row.worker_transition_id: row for row in patches}
    acknowledgements = session.scalars(
        select(ContextPatchAcknowledgementModel).where(
            ContextPatchAcknowledgementModel.workspace_id == workspace_id,
            ContextPatchAcknowledgementModel.patch_id.in_(
                tuple(row.id for row in patches) or (UUID(int=0),)
            ),
        )
    ).all()
    return patch_by_transition, {row.patch_id: row for row in acknowledgements}


def _worker_fact(
    row: WorkerTransitionModel,
    item: WorkerItemModel | None,
    placement: WorkerItemPlacementModel | None,
    patch: ContextPatchModel | None,
    acknowledgements: Mapping[UUID, ContextPatchAcknowledgementModel],
    inputs_by_execution: Mapping[UUID, Sequence[ExecutionInputModel]],
) -> WorkerTransitionProjectionFact:
    inputs = tuple(
        input_row
        for execution_id in (row.source_execution_id, row.target_execution_id)
        if execution_id is not None
        for input_row in inputs_by_execution.get(execution_id, ())
    )
    ordered_inputs = tuple(sorted(inputs, key=lambda value: (value.created_at, value.id)))
    return WorkerTransitionProjectionFact(
        id=row.id,
        plan_transition_id=row.plan_transition_id,
        worker_item_id=row.worker_item_id,
        subtask_key=placement.subtask_key if placement is not None else None,
        action=row.action,
        status=row.status,
        writer_state=row.writer_state,
        target_context_version=row.target_context_version,
        current_context_version=(item.context_version if item is not None else row.target_context_version),
        acknowledged_context_version=(
            item.acknowledged_context_version if item is not None else 0
        ),
        patch_id=patch.id if patch is not None else None,
        patch_acknowledged=(patch is not None and patch.id in acknowledgements),
        target_execution_id=row.target_execution_id,
        target_session_id=row.target_session_id,
        queued_input_ids=tuple(
            value.id
            for value in ordered_inputs
            if value.status in {"accepted", "queued", "dispatching", "blocked"}
        ),
        superseded_input_ids=tuple(
            value.id for value in ordered_inputs if value.status == "superseded"
        ),
        failure_code=row.failure_code,
        started_at=_as_utc_optional(row.started_at),
        completed_at=_as_utc_optional(row.completed_at),
        version=row.version,
        source_execution_id=row.source_execution_id,
        source_session_id=row.source_session_id,
    )


def _revision_summary(status: str) -> str | None:
    return {
        "proposal_ready": "新的计划方案已就绪",
        "conflicted": "当前计划已变化，需要重新审阅",
        "waiting_for_user": "规划需要用户决定",
        "failed": "规划失败，需要处理",
    }.get(status)


def _revision_action(status: str) -> str | None:
    return {
        "proposal_ready": "review_revision",
        "conflicted": "refresh_revision",
        "waiting_for_user": "answer_clarification",
        "failed": "retry_revision",
    }.get(status)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> tuple[object, ...]:
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 1 else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_utc_optional(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None
