import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import PlanSnapshotModel, TaskModel

pytestmark = pytest.mark.integration


def test_r6_1_upgrade_downgrade_upgrade_backfills_only_real_heads(
    database: Database,
    workspace_id,
) -> None:
    root_task_id = uuid4()
    snapshot_id = uuid4()
    payload = {
        "id": str(snapshot_id),
        "schema_version": 1,
        "workspace_id": str(workspace_id),
        "root_task_id": str(root_task_id),
        "generation": 1,
        "parent_snapshot_id": None,
        "source_type": "test",
        "source_ref": "r6.1-roundtrip",
        "title": "Roundtrip plan",
        "goal_summary": "Verify migration backfill",
        "acceptance_refs": ["acceptance:roundtrip"],
        "work_items": [],
        "worker_slots": [],
        "milestones": [],
        "dependencies": [],
        "published_by": "integration",
        "published_at": "2026-08-28T12:00:00+00:00",
    }
    with database.session() as session:
        session.add(
            TaskModel(
                id=root_task_id,
                workspace_id=workspace_id,
                title="Roundtrip root",
                description="Migration fixture",
                acceptance_criteria=["backfill"],
                status="pending",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=snapshot_id,
                schema_version=1,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                generation=1,
                parent_snapshot_id=None,
                source_type="test",
                source_ref="r6.1-roundtrip",
                title="Roundtrip plan",
                goal_summary="Verify migration backfill",
                payload=payload,
                published_by="integration",
                published_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with database.engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    if version in {
        "0025_r6_3_3_notification_gateway",
        "0026_r6_3_4_result_inputs",
        "0027_r6_3_4_result_input_context",
        "0028_r6_result_integration",
        "0029_r6_result_context",
        "0030_r6_verification_request_uniqueness",
        "0031_r6_real_result_budget",
        "0032_r6_real_run_identity",
    }:
        pytest.skip(
            "R6.3.3 and later are forward-only; historical downgrade rehearsal "
            "belongs to a pre-R6.3.3 migration database"
        )
    try:
        command.downgrade(config, "0016_r5_3_work_context_packages")
        command.upgrade(config, "head")
    finally:
        command.upgrade(config, "head")

    with database.session() as session:
        head = session.execute(
            text(
                "SELECT active_snapshot_id, active_generation, "
                "active_snapshot_hash "
                "FROM root_task_plan_heads "
                "WHERE workspace_id = :workspace_id AND root_task_id = :root_task_id"
            ),
            {"workspace_id": workspace_id, "root_task_id": root_task_id},
        ).mappings().one()
        expected_hash = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        assert head["active_snapshot_id"] == snapshot_id
        assert head["active_generation"] == 1
        assert head["active_snapshot_hash"] == expected_hash

        no_snapshot_root_id = uuid4()
        session.add(
            TaskModel(
                id=no_snapshot_root_id,
                workspace_id=workspace_id,
                title="No snapshot root",
                description="Must not receive a synthetic head",
                acceptance_criteria=["no synthetic head"],
                status="pending",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        session.commit()
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM root_task_plan_heads "
                    "WHERE workspace_id = :workspace_id "
                    "AND root_task_id = :root_task_id"
                ),
                {"workspace_id": workspace_id, "root_task_id": no_snapshot_root_id},
            ).scalar_one()
            == 0
        )
