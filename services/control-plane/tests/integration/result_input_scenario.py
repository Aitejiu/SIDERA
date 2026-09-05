from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

FROZEN_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class ResultInputScenario:
    """Shared real-persistence harness grown incrementally by R6.3.4 tasks."""

    def __init__(
        self, database: Database, workspace_id: UUID, tmp_path: Path
    ) -> None:
        self.database = database
        self.workspace_id = workspace_id
        self.tmp_path = tmp_path
        self.now = FROZEN_NOW
        self.clock: Callable[[], datetime] = lambda: self.now
        self.uow_factory: Callable[[], SqlAlchemyUnitOfWork] = lambda: (
            SqlAlchemyUnitOfWork(self.database)
        )
        self.artifact_store = LocalArtifactContentStore(
            tmp_path / "result-input-artifacts"
        )
        self.ids: dict[str, UUID] = {
            name: uuid4()
            for name in (
                "root_task",
                "plan_snapshot",
                "collaboration_run",
                "projection_snapshot",
                "decision",
                "intent",
                "target_worker_item",
                "target_execution_request",
            )
        }


@pytest.fixture
def scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)
