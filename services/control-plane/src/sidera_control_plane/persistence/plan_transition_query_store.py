from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from sidera_control_plane.application.plan_transition_queries import (
    PlanTransitionSummaryView,
    PlanTransitionView,
    TransitionContextView,
    TransitionDependencyView,
    TransitionInputDispositionView,
    TransitionNodeView,
    TransitionOperationView,
    WorkerTransitionView,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextPatchAcknowledgementModel,
    ContextPatchModel,
    ExecutionInputModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    GatewayOperationModel,
    PlanTransitionModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerTransitionModel,
)


class SqlAlchemyPlanTransitionQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get_plan_transition(
        self, workspace_id: UUID, plan_transition_id: UUID
    ) -> PlanTransitionView | None:
        with self._read_session() as session:
            plan_row = session.scalar(
                select(PlanTransitionModel).where(
                    PlanTransitionModel.workspace_id == workspace_id,
                    PlanTransitionModel.id == plan_transition_id,
                )
            )
            if plan_row is None:
                return None
            return self._compose_plan(session, plan_row)

    def list_plan_transitions(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> tuple[PlanTransitionSummaryView, ...]:
        with self._read_session() as session:
            plan_rows = session.scalars(
                select(PlanTransitionModel)
                .where(
                    PlanTransitionModel.workspace_id == workspace_id,
                    PlanTransitionModel.root_task_id == root_task_id,
                )
                .order_by(
                    PlanTransitionModel.started_at.asc(),
                    PlanTransitionModel.id.asc(),
                )
            ).all()
            if not plan_rows:
                return ()
            root = self._task_map(session, workspace_id, {root_task_id}).get(
                root_task_id
            )
            root_view = _root_node(root, root_task_id)
            summaries: list[PlanTransitionSummaryView] = []
            for plan_row in plan_rows:
                worker_rows = session.scalars(
                    select(WorkerTransitionModel).where(
                        WorkerTransitionModel.workspace_id == workspace_id,
                        WorkerTransitionModel.plan_transition_id == plan_row.id,
                    )
                ).all()
                summaries.append(
                    PlanTransitionSummaryView(
                        plan_transition_id=plan_row.id,
                        workspace_id=plan_row.workspace_id,
                        root_task=root_view,
                        from_generation=plan_row.from_generation,
                        to_generation=plan_row.to_generation,
                        status=plan_row.status,
                        started_at=plan_row.started_at,
                        completed_at=plan_row.completed_at,
                        version=plan_row.version,
                        worker_count=len(worker_rows),
                        blocked_worker_count=sum(
                            row.status in {"failed", "blocked_unknown"}
                            for row in worker_rows
                        ),
                        unknown_worker_count=sum(
                            row.status == "blocked_unknown" for row in worker_rows
                        ),
                    )
                )
            return tuple(summaries)

    def get_worker_transition(
        self, workspace_id: UUID, worker_transition_id: UUID
    ) -> WorkerTransitionView | None:
        with self._read_session() as session:
            worker_row = session.scalar(
                select(WorkerTransitionModel).where(
                    WorkerTransitionModel.workspace_id == workspace_id,
                    WorkerTransitionModel.id == worker_transition_id,
                )
            )
            if worker_row is None:
                return None
            plan_row = session.scalar(
                select(PlanTransitionModel).where(
                    PlanTransitionModel.workspace_id == workspace_id,
                    PlanTransitionModel.id == worker_row.plan_transition_id,
                )
            )
            if plan_row is None:
                return None
            workers = self._compose_workers(session, plan_row, (worker_row,))
            return workers[0] if workers else None

    def _compose_plan(
        self, session: Session, plan_row: PlanTransitionModel
    ) -> PlanTransitionView:
        worker_rows = tuple(
            session.scalars(
                select(WorkerTransitionModel)
                .where(
                    WorkerTransitionModel.workspace_id == plan_row.workspace_id,
                    WorkerTransitionModel.plan_transition_id == plan_row.id,
                )
                .order_by(WorkerTransitionModel.created_at.asc(), WorkerTransitionModel.id.asc())
            ).all()
        )
        workers = self._compose_workers(session, plan_row, worker_rows)
        root_row = self._task_map(
            session, plan_row.workspace_id, {plan_row.root_task_id}
        ).get(plan_row.root_task_id)
        root_view = _root_node(root_row, plan_row.root_task_id)
        dependencies = _unique_dependencies(
            dependency
            for worker in workers
            for dependency in worker.blocked_dependencies
        )
        timestamps = [plan_row.created_at, *(row.created_at for row in worker_rows)]
        return PlanTransitionView(
            plan_transition_id=plan_row.id,
            workspace_id=plan_row.workspace_id,
            root_task=root_view,
            from_generation=plan_row.from_generation,
            to_generation=plan_row.to_generation,
            status=plan_row.status,
            started_at=plan_row.started_at,
            completed_at=plan_row.completed_at,
            version=plan_row.version,
            workers=workers,
            blocked_dependencies=dependencies,
            as_of=max(timestamps),
        )

    def _compose_workers(
        self,
        session: Session,
        plan_row: PlanTransitionModel,
        worker_rows: Iterable[WorkerTransitionModel],
    ) -> tuple[WorkerTransitionView, ...]:
        worker_rows = tuple(worker_rows)
        workspace_id = plan_row.workspace_id
        worker_item_ids = {row.worker_item_id for row in worker_rows}
        execution_ids = {
            execution_id
            for row in worker_rows
            for execution_id in (
                row.source_execution_id,
                row.target_execution_id,
            )
            if execution_id is not None
        }
        task_ids = {plan_row.root_task_id}
        task_ids.update(
            task_id
            for row in worker_rows
            for task_id in (row.from_subtask_id, row.to_subtask_id)
            if task_id is not None
        )
        item_rows = session.scalars(
            select(WorkerItemModel).where(
                WorkerItemModel.workspace_id == workspace_id,
                WorkerItemModel.id.in_(worker_item_ids or {UUID(int=0)}),
            )
        ).all()
        item_by_id = {row.id: row for row in item_rows}
        placement_rows = session.scalars(
            select(WorkerItemPlacementModel).where(
                WorkerItemPlacementModel.workspace_id == workspace_id,
                WorkerItemPlacementModel.root_task_id == plan_row.root_task_id,
                WorkerItemPlacementModel.worker_item_id.in_(
                    worker_item_ids or {UUID(int=0)}
                ),
                WorkerItemPlacementModel.plan_snapshot_id.in_(
                    {plan_row.from_snapshot_id, plan_row.to_snapshot_id}
                ),
            )
        ).all()
        task_ids.update(
            row.subtask_task_id
            for row in placement_rows
            if row.subtask_task_id is not None
        )
        task_by_id = self._task_map(session, workspace_id, task_ids)
        patch_rows = session.scalars(
            select(ContextPatchModel).where(
                ContextPatchModel.workspace_id == workspace_id,
                ContextPatchModel.worker_transition_id.in_(
                    {row.id for row in worker_rows} or {UUID(int=0)}
                ),
            )
        ).all()
        patch_by_transition = {row.worker_transition_id: row for row in patch_rows}
        patch_ids = {row.id for row in patch_rows}
        ack_rows = session.scalars(
            select(ContextPatchAcknowledgementModel).where(
                ContextPatchAcknowledgementModel.workspace_id == workspace_id,
                ContextPatchAcknowledgementModel.patch_id.in_(patch_ids or {UUID(int=0)}),
            )
        ).all()
        ack_by_patch = {row.patch_id: row for row in ack_rows}
        operation_rows = session.scalars(
            select(GatewayOperationModel)
            .where(
                GatewayOperationModel.workspace_id == workspace_id,
                GatewayOperationModel.execution_id.in_(execution_ids or {UUID(int=0)}),
            )
            .order_by(
                GatewayOperationModel.updated_at.asc(),
                GatewayOperationModel.operation_id.asc(),
            )
        ).all()
        operations_by_execution: dict[UUID, list[GatewayOperationModel]] = {}
        for operation in operation_rows:
            operations_by_execution.setdefault(operation.execution_id, []).append(operation)
        input_rows = session.scalars(
            select(ExecutionInputModel).where(
                ExecutionInputModel.workspace_id == workspace_id,
                ExecutionInputModel.execution_id.in_(execution_ids or {UUID(int=0)}),
            )
        ).all()
        inputs_by_execution: dict[UUID, list[ExecutionInputModel]] = {}
        for item in input_rows:
            inputs_by_execution.setdefault(item.execution_id, []).append(item)
        request_rows = session.scalars(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.root_task_id == plan_row.root_task_id,
                ExecutionRequestModel.plan_snapshot_id.in_(
                    {plan_row.from_snapshot_id, plan_row.to_snapshot_id}
                ),
            )
        ).all()
        request_ids = {row.id for row in request_rows} or {UUID(int=0)}
        dependency_rows = session.scalars(
            select(ExecutionRequestDependencyModel).where(
                ExecutionRequestDependencyModel.workspace_id == workspace_id,
                or_(
                    ExecutionRequestDependencyModel.downstream_request_id.in_(request_ids),
                    ExecutionRequestDependencyModel.upstream_request_id.in_(request_ids),
                ),
            )
        ).all()
        worker_by_request = {
            row.id: row.worker_item_id for row in request_rows
        }
        placement_by_key = {
            (row.plan_snapshot_id, row.worker_item_id): row for row in placement_rows
        }
        root_view = _root_node(
            task_by_id.get(plan_row.root_task_id), plan_row.root_task_id
        )
        views: list[WorkerTransitionView] = []
        for row in worker_rows:
            item = item_by_id.get(row.worker_item_id)
            placement = _select_placement(
                row,
                plan_row.from_snapshot_id,
                plan_row.to_snapshot_id,
                placement_by_key,
            )
            subtask_id = (
                placement.subtask_task_id
                if placement is not None and placement.subtask_task_id is not None
                else row.from_subtask_id or row.to_subtask_id
            )
            subtask_key = placement.subtask_key if placement is not None else None
            subtask_view = _subtask_node(
                task_by_id.get(subtask_id) if subtask_id is not None else None,
                root_view,
                subtask_id,
                subtask_key,
            )
            item_title = item.title if item is not None else str(row.worker_item_id)
            item_key = item.stable_key if item is not None else (
                placement.worker_item_key if placement is not None else None
            )
            item_path = _worker_path(root_view, subtask_view, item_title)
            worker_view = TransitionNodeView(
                id=row.worker_item_id,
                key=item_key,
                title=item_title,
                path=item_path,
            )
            patch = patch_by_transition.get(row.id)
            patch_ack = ack_by_patch.get(patch.id) if patch is not None else None
            context_version = item.context_version if item is not None else row.target_context_version
            acknowledged_version = (
                item.acknowledged_context_version if item is not None else 0
            )
            execution_ids_for_worker = {
                execution_id
                for execution_id in (row.source_execution_id, row.target_execution_id)
                if execution_id is not None
            }
            operations = tuple(
                TransitionOperationView(
                    operation_id=operation.operation_id,
                    operation_type=operation.operation_type,
                    status=operation.status,
                    failure_code=operation.failure_code,
                )
                for execution_id in sorted(execution_ids_for_worker)
                for operation in operations_by_execution.get(execution_id, ())
            )
            inputs = tuple(
                item
                for execution_id in sorted(execution_ids_for_worker)
                for item in inputs_by_execution.get(execution_id, ())
            )
            queued_ids = tuple(
                item.id
                for item in sorted(inputs, key=lambda value: (value.created_at, value.id))
                if item.status in {"accepted", "queued", "dispatching", "blocked"}
            )
            superseded_ids = tuple(
                item.id
                for item in sorted(inputs, key=lambda value: (value.created_at, value.id))
                if item.status == "superseded"
            )
            views.append(
                WorkerTransitionView(
                    worker_transition_id=row.id,
                    plan_transition_id=row.plan_transition_id,
                    root_task=root_view,
                    subtask=subtask_view,
                    worker_item=worker_view,
                    path=item_path,
                    action=row.action,
                    fallback_action=row.fallback_action,
                    status=row.status,
                    writer_state=row.writer_state,
                    failure_code=row.failure_code,
                    source_execution_id=row.source_execution_id,
                    source_session_id=row.source_session_id,
                    target_execution_id=row.target_execution_id,
                    target_session_id=row.target_session_id,
                    from_subtask_id=row.from_subtask_id,
                    to_subtask_id=row.to_subtask_id,
                    context=TransitionContextView(
                        current_context_version=context_version,
                        acknowledged_context_version=acknowledged_version,
                        target_context_version=row.target_context_version,
                        patch_id=patch.id if patch is not None else None,
                        patch_hash=patch.patch_hash if patch is not None else None,
                        patch_acknowledged=patch_ack is not None,
                    ),
                    operations=operations,
                    queued_input_disposition=TransitionInputDispositionView(
                        policy=_input_policy(row.action),
                        queued_input_ids=queued_ids,
                        superseded_input_ids=superseded_ids,
                        queued_count=len(queued_ids),
                        superseded_count=len(superseded_ids),
                    ),
                    blocked_dependencies=(),
                    started_at=row.started_at,
                    completed_at=row.completed_at,
                    version=row.version,
                )
            )
        worker_by_item = {view.worker_item.id: view for view in views}
        return tuple(
            replace(
                view,
                blocked_dependencies=self._dependencies(
                    dependency_rows,
                    worker_by_request,
                    worker_by_item,
                    root_view,
                    view.worker_item.id,
                ),
            )
            for view in views
        )

    @staticmethod
    def _dependencies(
        dependency_rows: Iterable[ExecutionRequestDependencyModel],
        worker_by_request: dict[UUID, UUID],
        worker_by_item: dict[UUID, WorkerTransitionView],
        root_view: TransitionNodeView,
        worker_item_id: UUID,
    ) -> tuple[TransitionDependencyView, ...]:
        result: list[TransitionDependencyView] = []
        for dependency in dependency_rows:
            upstream_worker = worker_by_request.get(dependency.upstream_request_id)
            downstream_worker = worker_by_request.get(dependency.downstream_request_id)
            if (
                upstream_worker is None
                or downstream_worker is None
                or downstream_worker != worker_item_id
            ):
                continue
            upstream_view = worker_by_item.get(upstream_worker)
            upstream_status = (
                upstream_view.status if upstream_view is not None else None
            )
            blocked = dependency.enforcement == "hard" and upstream_status not in {
                None,
                "converged",
            }
            upstream_path = (
                f"{root_view.path} > WorkerItem/{upstream_worker}"
                if upstream_view is None
                else upstream_view.path
            )
            downstream_path = (
                worker_by_item[worker_item_id].path
                if worker_item_id in worker_by_item
                else f"{root_view.path} > WorkerItem/{worker_item_id}"
            )
            result.append(
                TransitionDependencyView(
                    upstream_worker_item_id=upstream_worker,
                    downstream_worker_item_id=downstream_worker,
                    upstream_path=upstream_path,
                    downstream_path=downstream_path,
                    enforcement=dependency.enforcement,
                    upstream_status=upstream_status,
                    blocked=blocked,
                    required_artifact_refs=tuple(dependency.required_artifact_refs),
                    required_evidence_refs=tuple(dependency.required_evidence_refs),
                )
            )
        return tuple(result)

    def _task_map(
        self, session: Session, workspace_id: UUID, task_ids: set[UUID]
    ) -> dict[UUID, TaskModel]:
        rows = session.scalars(
            select(TaskModel).where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.id.in_(task_ids or {UUID(int=0)}),
            )
        ).all()
        return {row.id: row for row in rows}

    @contextmanager
    def _read_session(self) -> Iterator[Session]:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            yield session


def _root_node(row: TaskModel | None, task_id: UUID) -> TransitionNodeView:
    title = row.title if row is not None else str(task_id)
    return TransitionNodeView(
        id=task_id,
        key=None,
        title=title,
        path=f"Task/{title}",
    )


def _subtask_node(
    row: TaskModel | None,
    root: TransitionNodeView,
    task_id: UUID | None,
    key: str | None,
) -> TransitionNodeView | None:
    if task_id is None and key is None:
        return None
    title = row.title if row is not None else key or str(task_id)
    return TransitionNodeView(
        id=task_id or UUID(int=0),
        key=key,
        title=title,
        path=f"{root.path} > Subtask/{title}",
    )


def _worker_path(
    root: TransitionNodeView,
    subtask: TransitionNodeView | None,
    title: str,
) -> str:
    prefix = subtask.path if subtask is not None else root.path
    return f"{prefix} > WorkerItem/{title}"


def _select_placement(
    transition: WorkerTransitionModel,
    from_snapshot_id: UUID,
    to_snapshot_id: UUID,
    placement_by_key: dict[tuple[UUID, UUID], WorkerItemPlacementModel],
) -> WorkerItemPlacementModel | None:
    for snapshot_id in (from_snapshot_id, to_snapshot_id):
        placement = placement_by_key.get((snapshot_id, transition.worker_item_id))
        if placement is not None:
            return placement
    return None


def _input_policy(action: str) -> str:
    if action in {"continue", "retire"}:
        return "explicit_resend_only"
    if action == "patch":
        return "preserve_until_patch_ack"
    return "unchanged"


def _unique_dependencies(
    dependencies: Iterable[TransitionDependencyView],
) -> tuple[TransitionDependencyView, ...]:
    unique: dict[tuple[UUID, UUID], TransitionDependencyView] = {}
    for dependency in dependencies:
        unique.setdefault(
            (dependency.upstream_worker_item_id, dependency.downstream_worker_item_id),
            dependency,
        )
    return tuple(
        unique[key]
        for key in sorted(unique, key=lambda value: (str(value[0]), str(value[1])))
    )
