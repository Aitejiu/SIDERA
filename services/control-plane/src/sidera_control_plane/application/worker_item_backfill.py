from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from hashlib import sha256
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
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.worker_item import WorkerItem


def responsibility_fingerprint(
    first: str,
    second: str,
    third: str | Collection[str],
    capability_requirements: Collection[str] | None = None,
) -> str:
    if capability_requirements is None:
        if isinstance(third, str):
            raise TypeError("capability requirements must be a collection")
        legacy_identity_key = ""
        role = first
        responsibility = second
        capabilities = third
    else:
        if not isinstance(third, str):
            raise TypeError("responsibility must be a string")
        legacy_identity_key = first
        role = second
        responsibility = third
        capabilities = capability_requirements
    payload = {
        "legacy_identity_key": legacy_identity_key.strip(),
        "role": role.strip(),
        "responsibility": responsibility.strip(),
        "capability_requirements": _normalized_capabilities(capabilities),
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RootBackfillReport:
    root_task_id: UUID
    created_worker_items: int
    created_placements: int
    linked_executions: int
    ambiguous_execution_ids: tuple[UUID, ...]


class WorkerItemBackfillService:
    def __init__(self, normalizer: PlanNormalizer | None = None) -> None:
        self._normalizer = normalizer or PlanNormalizer()

    def backfill_root(
        self,
        uow: ControlPlaneUnitOfWork,
        workspace_id: UUID,
        root_task_id: UUID,
    ) -> RootBackfillReport:
        snapshots = tuple(
            sorted(
                uow.plans.list_for_root(root_task_id, workspace_id),
                key=lambda snapshot: snapshot.generation,
            )
        )
        created_worker_items = 0
        created_placements = 0
        previous_by_key: dict[str, tuple[WorkerItem, str]] = {}
        generated: list[
            tuple[TaskPlanSnapshot, CanonicalTaskPlan, tuple[WorkerItemPlacement, ...]]
        ] = []

        for snapshot in snapshots:
            canonical = self._normalizer.normalize(snapshot)
            if snapshot.schema_version != 1:
                generated.append((snapshot, canonical, ()))
                continue

            subtask_task_ids = {
                subtask.key: subtask.task_id for subtask in canonical.subtasks
            }
            placements: list[WorkerItemPlacement] = []
            for worker_plan in canonical.worker_items:
                if worker_plan.legacy_identity_key is None:
                    raise ValueError(
                        "v1 canonical WorkerItem is missing its legacy identity key"
                    )
                fingerprint = responsibility_fingerprint(
                    worker_plan.legacy_identity_key,
                    worker_plan.role,
                    worker_plan.responsibility,
                    worker_plan.capability_requirements,
                )
                previous = previous_by_key.get(worker_plan.legacy_identity_key)
                item, created = self._resolve_item(
                    uow,
                    snapshot,
                    worker_plan,
                    fingerprint,
                    previous,
                )
                if created:
                    uow.worker_items.add(item)
                    created_worker_items += 1
                previous_by_key[worker_plan.legacy_identity_key] = (item, fingerprint)

                placement = WorkerItemPlacement(
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
                    created_at=snapshot.published_at,
                )
                existing = uow.worker_items.get_placement(
                    snapshot.id, item.id
                )
                uow.worker_items.add_placement(placement)
                if existing is None:
                    created_placements += 1
                placements.append(placement)
            generated.append((snapshot, canonical, tuple(placements)))

        linked_executions, ambiguous_execution_ids = self._link_executions(
            uow, workspace_id, generated
        )
        return RootBackfillReport(
            root_task_id=root_task_id,
            created_worker_items=created_worker_items,
            created_placements=created_placements,
            linked_executions=linked_executions,
            ambiguous_execution_ids=tuple(sorted(set(ambiguous_execution_ids))),
        )

    def _resolve_item(
        self,
        uow: ControlPlaneUnitOfWork,
        snapshot: TaskPlanSnapshot,
        worker_plan: CanonicalWorkerItemPlan,
        fingerprint: str,
        previous: tuple[WorkerItem, str] | None,
    ) -> tuple[WorkerItem, bool]:
        assert worker_plan.legacy_identity_key is not None
        if previous is not None and previous[1] == fingerprint:
            return previous[0], False

        if previous is None:
            candidate = uow.worker_items.get_by_stable_key(
                snapshot.root_task_id, worker_plan.legacy_identity_key
            )
            if candidate is None:
                return self._create_item(
                    snapshot,
                    worker_plan,
                    stable_key=worker_plan.legacy_identity_key,
                    derived_from=(),
                )
            if self._matches_plan(uow, candidate, snapshot, worker_plan):
                return candidate, False
            source = candidate
        else:
            source = previous[0]

        stable_key = f"{worker_plan.legacy_identity_key}:derived:{fingerprint}"
        existing = uow.worker_items.get_by_stable_key(
            snapshot.root_task_id, stable_key
        )
        if existing is not None:
            self._validate_existing_item(existing, snapshot, worker_plan)
            return existing, False
        return self._create_item(
            snapshot,
            worker_plan,
            stable_key=stable_key,
            derived_from=(source.id,),
        )

    @staticmethod
    def _create_item(
        snapshot: TaskPlanSnapshot,
        worker_plan: CanonicalWorkerItemPlan,
        *,
        stable_key: str,
        derived_from: tuple[UUID, ...],
    ) -> tuple[WorkerItem, bool]:
        item = WorkerItem.create(
            item_id=uuid4(),
            workspace_id=snapshot.workspace_id,
            root_task_id=snapshot.root_task_id,
            stable_key=stable_key,
            title=worker_plan.title,
            responsibility=worker_plan.responsibility,
            now=snapshot.published_at,
            derived_from_worker_item_ids=derived_from,
        )
        return item, True

    @classmethod
    def _matches_plan(
        cls,
        uow: ControlPlaneUnitOfWork,
        item: WorkerItem,
        snapshot: TaskPlanSnapshot,
        worker_plan: CanonicalWorkerItemPlan,
    ) -> bool:
        if item.workspace_id != snapshot.workspace_id:
            return False
        if item.root_task_id != snapshot.root_task_id:
            return False
        if item.title != worker_plan.title.strip():
            return False
        if item.responsibility != worker_plan.responsibility.strip():
            return False
        placements = tuple(
            placement
            for placement in uow.worker_items.list_placements(
                item.id, snapshot.workspace_id
            )
            if placement.worker_item_key == worker_plan.key
        )
        if not placements:
            return True
        return any(
            placement.role == worker_plan.role
            and _normalized_capabilities(placement.capability_requirements)
            == _normalized_capabilities(worker_plan.capability_requirements)
            for placement in placements
        )

    @staticmethod
    def _validate_existing_item(
        item: WorkerItem,
        snapshot: TaskPlanSnapshot,
        worker_plan: CanonicalWorkerItemPlan,
    ) -> None:
        if item.workspace_id != snapshot.workspace_id:
            raise ValueError("backfill WorkerItem belongs to another workspace")
        if item.root_task_id != snapshot.root_task_id:
            raise ValueError("backfill WorkerItem belongs to another root task")
        if item.title != worker_plan.title.strip():
            raise ValueError("backfill WorkerItem title is immutable")
        if item.responsibility != worker_plan.responsibility.strip():
            raise ValueError("backfill WorkerItem responsibility is immutable")

    @staticmethod
    def _link_executions(
        uow: ControlPlaneUnitOfWork,
        workspace_id: UUID,
        generated: list[
            tuple[TaskPlanSnapshot, CanonicalTaskPlan, tuple[WorkerItemPlacement, ...]]
        ],
    ) -> tuple[int, list[UUID]]:
        linked = 0
        ambiguous: list[UUID] = []
        for index, (snapshot, _canonical, placements) in enumerate(generated):
            if snapshot.schema_version != 1 or not placements:
                continue
            end = (
                generated[index + 1][0].published_at
                if index + 1 < len(generated)
                else None
            )
            all_placements = list(placements)
            for subtask in _canonical.subtasks:
                all_placements.extend(
                    uow.worker_items.list_for_subtask(snapshot.id, subtask.key)
                )
            unique_placements = {
                (placement.plan_snapshot_id, placement.worker_item_id): placement
                for placement in all_placements
            }
            by_task: dict[UUID, list[WorkerItemPlacement]] = {}
            for placement in unique_placements.values():
                if placement.subtask_task_id is not None:
                    by_task.setdefault(placement.subtask_task_id, []).append(
                        placement
                    )
            for task_id, task_placements in by_task.items():
                execution_ids = uow.executions.list_ids_for_task_window(
                    task_id, workspace_id, snapshot.published_at, end
                )
                if len(task_placements) != 1:
                    ambiguous.extend(execution_ids)
                    continue
                worker_item_id = task_placements[0].worker_item_id
                for execution_id in execution_ids:
                    if uow.executions.assign_worker_item_if_unset(
                        execution_id, workspace_id, worker_item_id
                    ):
                        linked += 1
        return linked, ambiguous


def _normalized_capabilities(values: Collection[str]) -> tuple[str, ...]:
    return tuple(sorted(value.strip() for value in values if value.strip()))
