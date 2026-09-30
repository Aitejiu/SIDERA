from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

if os.environ.get("SIDERA_INTERVIEW_DEMO_TEST_RUN") != "1":
    raise RuntimeError(
        "test_interview_bootstrap.py must run through the isolated demo test runner"
    )

from sidera_control_plane.demo.bootstrap import DemoBootstrapConflict, bootstrap_demo
from sidera_control_plane.demo.contracts import (
    WORKER_ITEM_CAPABILITIES,
    DemoManifest,
)
from sidera_control_plane.demo.sample_repository import create_sample_repository
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ModuleInstanceModel,
    PlanningProfileModel,
    TaskAcceptanceDecisionModel,
    TaskModel,
    WorkerModel,
    WorkerRuntimeConfigurationModel,
)


def _manifest(tmp_path: Path) -> DemoManifest:
    run_id = uuid4()
    project_root = Path(__file__).resolve().parents[4]
    run_root = project_root / ".sidera" / "interview-demo" / str(run_id)
    repository, base_commit = create_sample_repository(tmp_path)
    return DemoManifest(
        run_id=run_id,
        workspace_id=uuid4(),
        scenario="mainline",
        run_root=run_root,
        repository=repository,
        base_commit=base_commit,
        code_commit="b" * 40,
        database_name=f"sidera_interview_test_{run_id.hex}",
        knowledge_database_name=f"sidera_interview_knowledge_{run_id.hex}",
        endpoints={
            "control_plane": "http://127.0.0.1:15432",
            "knowledge": "http://127.0.0.1:15433",
            "main_agent": "http://127.0.0.1:15434",
            "web": "http://127.0.0.1:15435",
        },
        module_ids={key: uuid4() for key in (
            "worker_primary", "worker_secondary", "workspace", "planner",
            "collaboration", "verification", "notification",
        )},
        worker_ids={key: uuid4() for key in (
            "validation", "report", "review", "integration", "replacement",
            "verification",
        )},
        planning_profile_id=uuid4(),
    )


def _counts(database: Database, workspace_id) -> dict[str, int]:
    with database.session() as session:
        return {
            "tasks": session.scalar(select(func.count()).select_from(TaskModel)) or 0,
            "executions": session.scalar(select(func.count()).select_from(ExecutionModel)) or 0,
            "acceptances": session.scalar(
                select(func.count()).select_from(TaskAcceptanceDecisionModel)
            ) or 0,
            "modules": session.scalar(
                select(func.count()).select_from(ModuleInstanceModel).where(
                    ModuleInstanceModel.workspace_id == workspace_id
                )
            ) or 0,
            "profiles": session.scalar(
                select(func.count()).select_from(PlanningProfileModel).where(
                    PlanningProfileModel.workspace_id == workspace_id
                )
            ) or 0,
            "workers": session.scalar(
                select(func.count()).select_from(WorkerModel).where(
                    WorkerModel.workspace_id == workspace_id
                )
            ) or 0,
            "runtimes": session.scalar(
                select(func.count()).select_from(WorkerRuntimeConfigurationModel).where(
                    WorkerRuntimeConfigurationModel.workspace_id == workspace_id
                )
            ) or 0,
        }


@pytest.mark.integration
def test_demo_bootstrap_is_idempotent_and_does_not_create_business_rows(
    database: Database,
    clean_database: None,
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)

    first = bootstrap_demo(manifest, database=database, check_only=False)
    second = bootstrap_demo(manifest, database=database, check_only=False)

    assert first.status == "created"
    assert second.status == "idempotent"
    assert first.worker_ids == manifest.worker_ids
    assert second.worker_ids == manifest.worker_ids
    assert _counts(database, manifest.workspace_id) == {
        "tasks": 0,
        "executions": 0,
        "acceptances": 0,
        "modules": 7,
        "profiles": 1,
            "workers": 6,
        "runtimes": 6,
    }
    with database.session() as session:
        replacement = session.get(WorkerModel, manifest.worker_ids["replacement"])
        assert replacement is not None
        assert WORKER_ITEM_CAPABILITIES["validation"] in replacement.capabilities
        assert WORKER_ITEM_CAPABILITIES["replacement"] in replacement.capabilities


@pytest.mark.integration
def test_demo_bootstrap_rejects_configuration_drift_without_mutation(
    database: Database,
    clean_database: None,
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    bootstrap_demo(manifest, database=database, check_only=False)
    before = _counts(database, manifest.workspace_id)

    with pytest.raises(DemoBootstrapConflict, match="fingerprint"):
        bootstrap_demo(
            manifest.model_copy(update={"code_commit": "c" * 40}),
            database=database,
            check_only=False,
        )

    assert _counts(database, manifest.workspace_id) == before
