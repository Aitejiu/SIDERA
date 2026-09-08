from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.basic_acceptance_manifest import (
    AgentProfile,
    BasicRunManifest,
    ScenarioLimits,
)
from sidera_control_plane.bootstrap_basic_workspace import (
    BootstrapConflict,
    bootstrap_manifest,
)
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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "acceptance-repository"
    repository.mkdir()
    _git(repository, "init", "-q", "--initial-branch=main")
    _git(repository, "config", "user.name", "SIDERA bootstrap test")
    _git(repository, "config", "user.email", "bootstrap@sidera.local")
    (repository / "README.md").write_text("acceptance baseline\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-qm", "acceptance baseline")
    return repository, _git(repository, "rev-parse", "HEAD")


def _manifest(
    tmp_path: Path,
    workspace_id: UUID,
    repository: Path,
    base_commit: str,
    *,
    run_id: UUID,
    worker_config_version: int = 1,
) -> BasicRunManifest:
    profiles = tuple(
        AgentProfile(
            role=role,
            runtime="opencode-acp-planner" if role == "planner" else "opencode-acp",
            provider=(
                "opencode-acp-planner" if role == "planner" else "opencode-acp"
            ),
            model_id="opencode-go/MiMo-V2.5",
            endpoint_host="127.0.0.1:4096",
            credential_ref="none",
            config_ref=f"module:r6/{role}",
            config_version=worker_config_version if role == "worker" else 1,
            fallbacks=(),
        )
        for role in ("main_agent", "planner", "worker", "second_worker")
    )
    return BasicRunManifest(
        run_id=run_id,
        workspace_id=workspace_id,
        local_gate_record=tmp_path / "local-gate.json",
        runtime_fingerprint="a" * 64,
        run_root=tmp_path / "run-root",
        repository=repository,
        base_commit=base_commit,
        database_ref="env:SIDERA_DATABASE_URL",
        knowledge_database_ref="env:SIDERA_DATABASE_URL",
        control_plane_url="http://127.0.0.1:8010",
        knowledge_url="http://127.0.0.1:8020",
        main_agent_gateway_url="http://127.0.0.1:8030",
        web_url="http://127.0.0.1:5173",
        profiles=profiles,
        scenarios={
            "profile": ScenarioLimits(
                main_agent_turns=0,
                planner_runs=0,
                worker_sessions=0,
                adapter_prompts=0,
                max_seconds=60,
            )
        },
    )


def _counts(database: Database) -> dict[str, int]:
    with database.session() as session:
        return {
            "tasks": session.scalar(select(func.count()).select_from(TaskModel)) or 0,
            "executions": session.scalar(select(func.count()).select_from(ExecutionModel)) or 0,
            "acceptances": session.scalar(
                select(func.count()).select_from(TaskAcceptanceDecisionModel)
            )
            or 0,
            "modules": session.scalar(
                select(func.count()).select_from(ModuleInstanceModel)
            )
            or 0,
            "profiles": session.scalar(
                select(func.count()).select_from(PlanningProfileModel)
            )
            or 0,
            "workers": session.scalar(select(func.count()).select_from(WorkerModel)) or 0,
            "runtimes": session.scalar(
                select(func.count()).select_from(WorkerRuntimeConfigurationModel)
            )
            or 0,
        }


@pytest.mark.integration
def test_bootstrap_is_idempotent_and_does_not_create_business_rows(
    database: Database,
    workspace_id: UUID,
    clean_database: None,
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        workspace_id,
        repository,
        base_commit,
        run_id=uuid4(),
    )
    state_root = tmp_path / "state"

    first = bootstrap_manifest(
        manifest,
        database=database,
        mode="apply",
        state_root=state_root,
    )
    second = bootstrap_manifest(
        manifest,
        database=database,
        mode="apply",
        state_root=state_root,
    )

    assert first.status == "created"
    assert second.status == "idempotent"
    assert first.worker_ids == second.worker_ids
    assert _counts(database) == {
        "tasks": 0,
        "executions": 0,
        "acceptances": 0,
        "modules": 7,
        "profiles": 1,
        "workers": 2,
        "runtimes": 2,
    }
    assert (state_root / f"{manifest.run_id}.json").is_file()
    assert _git(repository, "status", "--porcelain") == ""
    assert _git(repository, "rev-parse", "HEAD") != base_commit
    assert "NotImplementedError" in (repository / "record_app.py").read_text()


@pytest.mark.integration
def test_bootstrap_rejects_config_version_conflict_without_mutation(
    database: Database,
    workspace_id: UUID,
    clean_database: None,
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    run_id = uuid4()
    state_root = tmp_path / "state"
    original = _manifest(
        tmp_path,
        workspace_id,
        repository,
        base_commit,
        run_id=run_id,
    )
    bootstrap_manifest(
        original,
        database=database,
        mode="apply",
        state_root=state_root,
    )
    before = _counts(database)

    changed = _manifest(
        tmp_path,
        workspace_id,
        repository,
        base_commit,
        run_id=run_id,
        worker_config_version=2,
    )
    with pytest.raises(BootstrapConflict, match="config_version"):
        bootstrap_manifest(
            changed,
            database=database,
            mode="apply",
            state_root=state_root,
        )

    assert _counts(database) == before


@pytest.mark.integration
def test_bootstrap_rejects_run_reused_for_another_workspace(
    database: Database,
    workspace_id: UUID,
    clean_database: None,
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    run_id = uuid4()
    state_root = tmp_path / "state"
    original = _manifest(
        tmp_path,
        workspace_id,
        repository,
        base_commit,
        run_id=run_id,
    )
    bootstrap_manifest(
        original,
        database=database,
        mode="apply",
        state_root=state_root,
    )

    changed_workspace = _manifest(
        tmp_path,
        UUID("00000000-0000-0000-0000-000000000099"),
        repository,
        base_commit,
        run_id=run_id,
    )
    with pytest.raises(BootstrapConflict, match="workspace"):
        bootstrap_manifest(
            changed_workspace,
            database=database,
            mode="check",
            state_root=state_root,
        )
