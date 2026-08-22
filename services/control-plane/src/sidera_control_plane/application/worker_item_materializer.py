from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkerItemPlacement,
)
from sidera_control_plane.domain.plan_normalization import (
    CanonicalTaskPlan,
    CanonicalWorkerItemPlan,
    PlanNormalizer,
)
from sidera_control_plane.domain.plans import PlanValidationError, TaskPlanSnapshot
from sidera_control_plane.domain.worker_item import WorkerItem, WorkerItemStatus


@dataclass(frozen=True, slots=True)
class MaterializedPlan:
    snapshot: TaskPlanSnapshot
    canonical: CanonicalTaskPlan
    placements: tuple[WorkerItemPlacement, ...]
    created_worker_item_ids: tuple[UUID, ...]


class WorkerItemMaterializer:
    def __init__(self, normalizer: PlanNormalizer | None = None) -> None:
        self._normalizer = normalizer or PlanNormalizer()

    def materialize(
        self,
        uow: ControlPlaneUnitOfWork,
        snapshot: TaskPlanSnapshot,
        *,
        now: datetime,
    ) -> MaterializedPlan:
        canonical = self._normalizer.normalize(snapshot)
        if snapshot.schema_version == 1:
            return MaterializedPlan(snapshot, canonical, (), ())

        subtask_task_ids = {
            subtask.key: subtask.task_id for subtask in canonical.subtasks
        }
        resolved_worker_items: list[CanonicalWorkerItemPlan] = []
        placements: list[WorkerItemPlacement] = []
        created_ids: list[UUID] = []

        for worker_plan in canonical.worker_items:
            existing_placement = self._find_existing_placement(
                uow, snapshot, worker_plan
            )
            item = self._resolve_worker_item(
                uow,
                snapshot,
                worker_plan,
                existing_placement,
                now=now,
            )
            if (
                item.id not in created_ids
                and worker_plan.worker_item_id is None
                and existing_placement is None
            ):
                created_ids.append(item.id)

            if (
                existing_placement is not None
                and worker_plan.worker_item_id is not None
                and existing_placement.worker_item_id != item.id
            ):
                raise PlanValidationError(
                    "worker_item_key_conflict",
                    "plan key already belongs to another WorkerItem in this snapshot",
                )

            resolved_worker_items.append(replace(worker_plan, worker_item_id=item.id))
            placements.append(
                WorkerItemPlacement(
                    plan_snapshot_id=snapshot.id,
                    plan_generation=snapshot.generation,
                    workspace_id=snapshot.workspace_id,
                    root_task_id=snapshot.root_task_id,
                    worker_item_id=item.id,
                    worker_item_key=worker_plan.key,
                    subtask_key=worker_plan.subtask_key,
                    subtask_task_id=subtask_task_ids[worker_plan.subtask_key],
                    role=worker_plan.role,
                    capability_requirements=worker_plan.capability_requirements,
                    worker_policy_ref=worker_plan.worker_policy_ref,
                    configuration_ref=worker_plan.configuration_ref,
                    created_at=now,
                )
            )

        for placement in placements:
            uow.worker_items.add_placement(placement)

        resolved_snapshot = replace(
            snapshot, worker_item_plans=tuple(resolved_worker_items)
        )
        resolved_canonical = self._normalizer.normalize(resolved_snapshot)
        return MaterializedPlan(
            snapshot=resolved_snapshot,
            canonical=resolved_canonical,
            placements=tuple(placements),
            created_worker_item_ids=tuple(created_ids),
        )

    def _find_existing_placement(
        self,
        uow: ControlPlaneUnitOfWork,
        snapshot: TaskPlanSnapshot,
        worker_plan: CanonicalWorkerItemPlan,
    ) -> WorkerItemPlacement | None:
        for placement in uow.worker_items.list_for_subtask(
            snapshot.id, worker_plan.subtask_key
        ):
            if placement.worker_item_key == worker_plan.key:
                return placement
        return None

    def _resolve_worker_item(
        self,
        uow: ControlPlaneUnitOfWork,
        snapshot: TaskPlanSnapshot,
        worker_plan: CanonicalWorkerItemPlan,
        existing_placement: WorkerItemPlacement | None,
        *,
        now: datetime,
    ) -> WorkerItem:
        requested_id = worker_plan.worker_item_id
        if requested_id is None and existing_placement is not None:
            requested_id = existing_placement.worker_item_id

        if requested_id is not None:
            item = uow.worker_items.get(
                requested_id, snapshot.workspace_id, for_update=True
            )
            if item is None:
                raise PlanValidationError(
                    "worker_item_reuse_not_found",
                    "explicit WorkerItem ID does not belong to this workspace",
                )
            self._validate_reuse(item, snapshot, worker_plan)
            return item

        item = WorkerItem.create(
            item_id=uuid4(),
            workspace_id=snapshot.workspace_id,
            root_task_id=snapshot.root_task_id,
            title=worker_plan.title,
            responsibility=worker_plan.responsibility,
            now=now,
        )
        uow.worker_items.add(item)
        return item

    @staticmethod
    def _validate_reuse(
        item: WorkerItem,
        snapshot: TaskPlanSnapshot,
        worker_plan: CanonicalWorkerItemPlan,
    ) -> None:
        if item.workspace_id != snapshot.workspace_id:
            raise PlanValidationError(
                "worker_item_workspace_mismatch",
                "WorkerItem workspace does not match the plan workspace",
            )
        if item.root_task_id != snapshot.root_task_id:
            raise PlanValidationError(
                "worker_item_root_mismatch",
                "WorkerItem root task does not match the plan root task",
            )
        if item.title != worker_plan.title:
            raise PlanValidationError(
                "worker_item_title_change_not_supported",
                "changing a WorkerItem title requires a separate context workflow",
            )
        if item.responsibility != worker_plan.responsibility:
            raise PlanValidationError(
                "worker_item_responsibility_change_not_supported",
                "changing WorkerItem responsibility requires a separate context workflow",
            )
        if item.status is not WorkerItemStatus.ACTIVE:
            raise PlanValidationError(
                "worker_item_not_active",
                "only an active WorkerItem can receive a new placement",
            )
