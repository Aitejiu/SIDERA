from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import func, select

if os.environ.get("SIDERA_INTERVIEW_DEMO_TEST_RUN") != "1":
    raise RuntimeError(
        "test_interview_scenario_preparation.py must run through the isolated demo test runner"
    )

from sidera_control_plane.demo.scenarios import (
    preparation_steps,
    prepare_checkpoint,
    record_preparation_checkpoint,
)
from sidera_control_plane.demo.state import DemoStateStore
from sidera_control_plane.demo.test_runner import validate_interview_test_context
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import TaskModel

pytestmark = pytest.mark.integration


def test_preparation_checkpoint_does_not_write_business_rows(
    database: Database,
    clean_database: None,
    tmp_path: Path,
) -> None:
    del clean_database
    del tmp_path
    manifest = validate_interview_test_context(os.environ)
    DemoStateStore(manifest.run_root / "state", manifest.run_id)

    before = _task_count(database)
    audit = record_preparation_checkpoint(manifest)
    checkpoint = prepare_checkpoint(manifest)
    after = _task_count(database)

    assert before == after == 0
    assert audit["business_commands"] == []
    assert checkpoint["preparation_steps"] == list(preparation_steps(manifest.scenario))
    assert checkpoint["gates"]["governance"] is False


def _task_count(database: Database) -> int:
    with database.session() as session:
        return session.scalar(select(func.count()).select_from(TaskModel)) or 0
