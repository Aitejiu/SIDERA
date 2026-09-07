from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.domain.code_results import CodeResultSnapshotV1
from sidera_control_plane.domain.result_operations import TaskResultHead
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import EventModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.unit.result_integration_fixtures import snapshot_mapping

from .test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class ResultIntegrationScenario:
    """Small real-persistence harness extended by later result tasks."""

    def __init__(
        self,
        database: Database,
        workspace_id: UUID,
        tmp_path: Path,
        ids: dict[str, UUID],
        request_id: UUID,
    ) -> None:
        self.database = database
        self.workspace_id = workspace_id
        self.tmp_path = tmp_path
        self.ids = ids
        self.request_id = request_id
        self.operation_id = UUID("80000000-0000-0000-0000-000000000002")

    @property
    def root_task_id(self) -> UUID:
        return self.ids["root_task"]

    @property
    def execution_id(self) -> UUID:
        return self.ids["execution"]

    def uow(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self.database)

    def root(self):
        with self.uow() as uow:
            root = uow.tasks.get(self.root_task_id, self.workspace_id)
        if root is None:
            raise AssertionError("result scenario root task was not found")
        return root

    def head(self) -> TaskResultHead:
        with self.uow() as uow:
            head = uow.results.get_head(self.workspace_id, self.root_task_id)
        if head is None:
            raise AssertionError("result scenario head was not found")
        return head

    def head_fixture(self) -> TaskResultHead:
        return TaskResultHead(
            workspace_id=self.workspace_id,
            root_task_id=self.root_task_id,
            current_candidate_id=None,
            current_acceptance_id=None,
            result_version=1,
            active_operation_id=None,
            auto_rounds_used=0,
            auto_rounds_limit=2,
            auto_seconds_limit=1800,
            auto_started_at=None,
            auto_deadline=None,
        )

    def snapshot(self, *, result_commit: str | None = None) -> CodeResultSnapshotV1:
        raw = snapshot_mapping(
            workspace_id=str(self.workspace_id),
            root_task_id=str(self.root_task_id),
            worker_item_id=str(self.ids["worker_item"]),
            request_id=str(self.request_id),
            execution_id=str(self.execution_id),
        )
        if result_commit is not None:
            raw["result_commit"] = result_commit
        content = {
            "schema_version": raw["schema_version"],
            "workspace_id": raw["workspace_id"],
            "root_task_id": raw["root_task_id"],
            "worker_item_id": raw["worker_item_id"],
            "request_id": raw["request_id"],
            "execution_id": raw["execution_id"],
            "repository_ref": raw["repository_ref"],
            "input_commit": raw["input_commit"],
            "input_tree": raw["input_tree"],
            "result_commit": raw["result_commit"],
            "result_tree": raw["result_tree"],
            "consumed_result_ids": raw["consumed_result_ids"],
            "artifact_ids": raw["artifact_ids"],
            "capture_operation_id": raw["capture_operation_id"],
        }
        raw["content_hash"] = hashlib.sha256(
            json.dumps(
                content,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return CodeResultSnapshotV1.from_mapping(raw)

    def events(self, event_type: str | None = None) -> tuple[EventModel, ...]:
        with self.database.session() as session:
            statement = select(EventModel).where(
                EventModel.workspace_id == self.workspace_id
            )
            if event_type is not None:
                statement = statement.where(EventModel.event_type == event_type)
            return tuple(
                session.scalars(
                    statement.order_by(EventModel.created_at.asc(), EventModel.id.asc())
                ).all()
            )


@pytest.fixture
def result_scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultIntegrationScenario:
    ids = seed_execution_request_facts(database, workspace_id)
    request_id = uuid4()
    with database.session() as session:
        session.add(request_row(ids, request_id=request_id))
        session.commit()
    return ResultIntegrationScenario(
        database,
        workspace_id,
        tmp_path,
        ids,
        request_id,
    )


__all__ = ["ResultIntegrationScenario", "result_scenario"]
