from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.integration


CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[2]
BASE_REVISION = "0016_r5_3_work_context_packages"
PLAN_HEAD_REVISION = "0017_r6_1_plan_revisions"
CURRENT_REVISION = "0018_r6_1_execution_request_lineage"
SAFE_BASE_DATABASE = re.compile(r"sidera_r61_[a-z0-9_-]+$")


def _rehearsal_base_url() -> str:
    value = os.environ.get("SIDERA_R6_1_MIGRATION_DATABASE_URL") or os.environ.get(
        "SIDERA_R6_1_TEST_DATABASE_URL"
    )
    if value is None:
        pytest.skip(
            "SIDERA_R6_1_MIGRATION_DATABASE_URL or "
            "SIDERA_R6_1_TEST_DATABASE_URL is required"
        )

    parsed = make_url(value)
    if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
        pytest.fail("migration rehearsal requires a PostgreSQL URL")
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("migration rehearsal requires a loopback PostgreSQL host")
    if parsed.database is None or SAFE_BASE_DATABASE.fullmatch(parsed.database) is None:
        pytest.fail(
            "migration rehearsal base database must use the sidera_r61_ prefix"
        )
    return value


@contextmanager
def _temporary_database(base_url: str) -> Iterator[str]:
    parsed = make_url(base_url)
    database_name = f"sidera_r61_rehearsal_{uuid4().hex[:12]}"
    database_owner = parsed.username
    if database_owner is None or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database_owner) is None:
        pytest.fail("migration rehearsal base URL must include a simple database user")
    admin_value = os.environ.get("SIDERA_R6_1_MIGRATION_ADMIN_DATABASE_URL")
    if admin_value is None:
        admin_url = parsed.set(database="postgres")
    else:
        admin_parsed = make_url(admin_value)
        if admin_parsed.database not in {None, "postgres", "template1"}:
            pytest.fail("migration rehearsal admin URL must target postgres")
        admin_url = admin_parsed.set(database=admin_parsed.database or "postgres")
    target_url = parsed.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    created = False
    try:
        try:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        f'CREATE DATABASE "{database_name}" '
                        f'OWNER "{database_owner}"'
                    )
                )
        except ProgrammingError as exc:
            if admin_value is None and "permission denied to create database" in str(exc):
                pytest.skip(
                    "application database role cannot create rehearsal databases; "
                    "set SIDERA_R6_1_MIGRATION_ADMIN_DATABASE_URL"
                )
            raise
        created = True
        yield str(target_url)
    finally:
        admin_engine.dispose()
        if created:
            cleanup_engine = create_engine(
                admin_url,
                isolation_level="AUTOCOMMIT",
            )
            try:
                with cleanup_engine.connect() as connection:
                    connection.execute(text(f'DROP DATABASE "{database_name}"'))
            finally:
                cleanup_engine.dispose()


def _upgrade(database_url: str, revision: str) -> None:
    previous = os.environ.get("SIDERA_DATABASE_URL")
    os.environ["SIDERA_DATABASE_URL"] = database_url
    try:
        config = Config(str(CONTROL_PLANE_ROOT / "alembic.ini"))
        command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("SIDERA_DATABASE_URL", None)
        else:
            os.environ["SIDERA_DATABASE_URL"] = previous


def _canonical_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _seed_at_0016(
    database_url: str,
    *,
    duplicate_generation: bool = False,
) -> dict[str, object]:
    workspace_id = uuid4()
    root_task_id = uuid4()
    no_snapshot_root_id = uuid4()
    first_snapshot_id = uuid4()
    second_snapshot_id = uuid4()
    duplicate_snapshot_id = uuid4()
    published_at = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    second_payload: dict[str, object] = {
        "schema_version": 1,
        "root_task_id": str(root_task_id),
        "generation": 2,
        "work_items": [
            {"key": "parallel-a", "status": "running"},
            {"key": "parallel-b", "status": "queued"},
        ],
        "dependencies": [{"from": "parallel-a", "to": "parallel-b"}],
    }
    first_payload = {
        "schema_version": 1,
        "root_task_id": str(root_task_id),
        "generation": 1,
        "work_items": [{"key": "parallel-a", "status": "queued"}],
        "dependencies": [],
    }
    duplicate_payload = {
        "schema_version": 1,
        "root_task_id": str(root_task_id),
        "generation": 1,
        "work_items": [{"key": "duplicate", "status": "queued"}],
        "dependencies": [],
    }

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, status, version) "
                    "VALUES (CAST(:id AS uuid), :name, 'active', 1)"
                ),
                {"id": str(workspace_id), "name": "R6.1 migration rehearsal"},
            )
            for task_id, title in (
                (root_task_id, "R6.1 backfill root"),
                (no_snapshot_root_id, "R6.1 no snapshot root"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO tasks "
                        "(id, workspace_id, title, description, "
                        "acceptance_criteria, status, priority, requested_by, version) "
                        "VALUES (CAST(:id AS uuid), CAST(:workspace_id AS uuid), "
                        ":title, :description, CAST(:criteria AS jsonb), "
                        "'pending', 50, 'r6.1-migration-rehearsal', 1)"
                    ),
                    {
                        "id": str(task_id),
                        "workspace_id": str(workspace_id),
                        "title": title,
                        "description": "Isolated migration fixture",
                        "criteria": json.dumps(["backfill"], ensure_ascii=True),
                    },
                )

            if duplicate_generation:
                connection.execute(
                    text(
                        "ALTER TABLE task_plan_snapshots "
                        "DROP CONSTRAINT uq_plan_snapshots_root_generation"
                    )
                )

            snapshots = (
                (first_snapshot_id, 1, None, first_payload),
                (second_snapshot_id, 2, first_snapshot_id, second_payload),
            )
            if duplicate_generation:
                snapshots = (
                    (first_snapshot_id, 1, None, first_payload),
                    (duplicate_snapshot_id, 1, None, duplicate_payload),
                )
            for snapshot_id, generation, parent_id, payload in snapshots:
                connection.execute(
                    text(
                        "INSERT INTO task_plan_snapshots "
                        "(id, schema_version, workspace_id, root_task_id, generation, "
                        "parent_snapshot_id, source_type, source_ref, title, "
                        "goal_summary, payload, published_by, published_at) "
                        "VALUES (CAST(:id AS uuid), 1, CAST(:workspace_id AS uuid), "
                        "CAST(:root_task_id AS uuid), :generation, "
                        "CAST(:parent_id AS uuid), 'migration-test', :source_ref, "
                        ":title, :goal_summary, CAST(:payload AS jsonb), 'test', "
                        ":published_at)"
                    ),
                    {
                        "id": str(snapshot_id),
                        "workspace_id": str(workspace_id),
                        "root_task_id": str(root_task_id),
                        "generation": generation,
                        "parent_id": str(parent_id) if parent_id else None,
                        "source_ref": f"r6.1-migration:{generation}",
                        "title": "R6.1 migration fixture",
                        "goal_summary": "Verify explicit plan head backfill",
                        "payload": json.dumps(payload, ensure_ascii=True),
                        "published_at": published_at,
                    },
                )
    finally:
        engine.dispose()

    return {
        "workspace_id": workspace_id,
        "root_task_id": root_task_id,
        "no_snapshot_root_id": no_snapshot_root_id,
        "active_snapshot_id": second_snapshot_id,
        "active_generation": 2,
        "active_snapshot_hash": _canonical_hash(second_payload),
    }


def _alembic_version(database_url: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    finally:
        engine.dispose()


def test_r6_1_backfill_rehearsal_isolated_and_rejects_generation_conflicts() -> None:
    base_url = _rehearsal_base_url()

    with _temporary_database(base_url) as database_url:
        _upgrade(database_url, BASE_REVISION)
        fixture = _seed_at_0016(database_url)
        _upgrade(database_url, PLAN_HEAD_REVISION)

        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                head = connection.execute(
                    text(
                        "SELECT active_snapshot_id, active_generation, "
                        "active_snapshot_hash FROM root_task_plan_heads "
                        "WHERE workspace_id = CAST(:workspace_id AS uuid) "
                        "AND root_task_id = CAST(:root_task_id AS uuid)"
                    ),
                    {
                        "workspace_id": str(fixture["workspace_id"]),
                        "root_task_id": str(fixture["root_task_id"]),
                    },
                ).mappings().one()
                assert head["active_snapshot_id"] == fixture["active_snapshot_id"]
                assert head["active_generation"] == fixture["active_generation"]
                assert head["active_snapshot_hash"] == fixture["active_snapshot_hash"]
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM root_task_plan_heads "
                            "WHERE workspace_id = CAST(:workspace_id AS uuid) "
                            "AND root_task_id = CAST(:root_task_id AS uuid)"
                        ),
                        {
                            "workspace_id": str(fixture["workspace_id"]),
                            "root_task_id": str(fixture["no_snapshot_root_id"]),
                        },
                    ).scalar_one()
                    == 0
                )
        finally:
            engine.dispose()
        _upgrade(database_url, "head")
        assert _alembic_version(database_url) == CURRENT_REVISION

    with _temporary_database(base_url) as database_url:
        _upgrade(database_url, BASE_REVISION)
        _seed_at_0016(database_url, duplicate_generation=True)
        with pytest.raises(RuntimeError, match="duplicate root/generation"):
            _upgrade(database_url, PLAN_HEAD_REVISION)
        assert _alembic_version(database_url) == BASE_REVISION
