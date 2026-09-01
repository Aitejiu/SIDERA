from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sidera_control_plane.domain.worker_item_projections import (
    ActiveHandoffProjection,
)


class HandoffQueryStore(Protocol):
    def load(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> ActiveHandoffProjection | None:
        raise NotImplementedError

    def load_for_root(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        worker_item_ids: tuple[UUID, ...] = (),
    ) -> dict[UUID, ActiveHandoffProjection]:
        raise NotImplementedError


class HandoffQueryService:
    def __init__(self, store: HandoffQueryStore) -> None:
        self._store = store

    def get_for_worker_item(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> ActiveHandoffProjection | None:
        return self._store.load(workspace_id, worker_item_id)

    def list_for_root(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        worker_item_ids: tuple[UUID, ...] = (),
    ) -> dict[UUID, ActiveHandoffProjection]:
        return self._store.load_for_root(workspace_id, root_task_id, worker_item_ids)
