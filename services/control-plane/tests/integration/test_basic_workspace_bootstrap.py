from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.basic_acceptance_manifest import (
    AgentProfile,
    BasicRunManifest,
    ScenarioLimits,
)
from sidera_control_plane.bootstrap_basic_workspace import (
    BootstrapConflict,
    _resources,
    _worker_config,
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


def test_worker_config_uses_explicit_opencode_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = AgentProfile(
        role="worker",
        runtime="opencode-acp",
        provider="opencode-acp",
        model_id="opencode-go/deepseek-v4.1-flash",
        endpoint_host="opencode.ai",
        credential_ref="store:opencode-go",
        config_ref="module:r6/worker",
        config_version=12,
        fallbacks=(),
    )

    monkeypatch.delenv("SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS", raising=False)
    default_config = _worker_config(profile)
    assert default_config["worker.request_timeout_seconds"] == 60.0
    assert default_config["worker.handoff_request_timeout_seconds"] == 60.0

    monkeypatch.setenv("SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS", "300")
    extended_config = _worker_config(profile)
    assert extended_config["worker.request_timeout_seconds"] == 300.0
    assert extended_config["worker.handoff_request_timeout_seconds"] == 300.0


def test_result_enabled_bootstrap_uses_v6_planner_and_runtime_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    workspace_id = uuid4()
    manifest = _manifest(
        tmp_path,
        workspace_id,
        repository,
        base_commit,
        run_id=uuid4(),
    )

    monkeypatch.setenv("SIDERA_RESULT_INPUTS_ENABLED", "1")
    resources = _resources(manifest)

    assert resources.profile.proposal_schema_version == 6
    assert resources.profile.collaboration_runtime_binding is not None
    assert resources.revision_profile.proposal_schema_version == 3
    result_integration = resources.profile.system_policy["result_integration"]
    assert {
        key: result_integration[key]
        for key in (
            "repository_ref",
            "base_commit",
            "verification_environment_ref",
            "verification_binding",
        )
    } == {
        "repository_ref": opaque_repository_ref(repository),
        "base_commit": base_commit,
        "verification_environment_ref": "env://interview-demo",
        "verification_binding": {
            "adapter_module_ref": str(resources.module_ids["local_verification"]),
            "adapter_config_version": 1,
            "workspace_module_ref": str(resources.module_ids["workspace"]),
            "workspace_config_version": 1,
        },
    }
    assert {
        item["key"] for item in result_integration["verification_check_specs"]
    } == {"closure", "patch-integrity"}
    assert result_integration["verification_check_specs"][0]["argv"] == (
        "git",
        "diff",
        "--check",
        base_commit,
        "HEAD",
    )
    closure_check = next(
        item
        for item in result_integration["verification_check_specs"]
        if item["key"] == "closure"
    )
    assert closure_check["argv"][0] == "python3"
    assert closure_check["argv"][1].endswith("scripts/check-closure-sample.py")
    assert closure_check["argv"][2] == "--repository-cwd"
    verification = next(
        instance
        for instance in resources.instances
        if instance.id == resources.module_ids["local_verification"]
    )
    assert {
        item["key"] for item in verification.config["verification.check_specs"]
    } == {"unit", "closure", "summary-json", "patch-integrity"}
    planner_instance = next(
        instance
        for instance in resources.instances
        if instance.id == resources.profile.planner_module_instance_id
    )
    planner = next(
        definition
        for definition in resources.definitions
        if definition.id == planner_instance.definition_id
    )
    assert planner.capabilities.proposal_schema_versions == (6,)
    assert planner.capabilities is not None
    collaboration = next(
        definition
        for definition in resources.definitions
        if definition.id
        == next(
            instance.definition_id
            for instance in resources.instances
            if instance.id == resources.module_ids["collaboration"]
        )
    )
    assert collaboration.capabilities.strategy_ids == (
        "all_required",
        "input_groups_v1",
    )
    workspace = next(
        definition
        for definition in resources.definitions
        if definition.id
        == next(
            instance.definition_id
            for instance in resources.instances
            if instance.id == resources.module_ids["workspace"]
        )
    )
    assert workspace.capabilities.code_input_versions == (1,)


@pytest.mark.integration
def test_result_enabled_bootstrap_registers_a_local_verification_worker(
    database: Database,
    workspace_id: UUID,
    clean_database: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_commit = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        workspace_id,
        repository,
        base_commit,
        run_id=uuid4(),
    )
    monkeypatch.setenv("SIDERA_RESULT_INPUTS_ENABLED", "1")

    result = bootstrap_manifest(
        manifest,
        database=database,
        mode="apply",
        state_root=tmp_path / "state",
    )

    assert "verification" in result.worker_ids
    assert result.runtime_versions["verification"] == 1
    assert _counts(database)["workers"] == 3
    assert _counts(database)["runtimes"] == 3
    with database.session() as session:
        verification = session.get(WorkerModel, result.worker_ids["verification"])
        assert verification is not None
        assert verification.adapter_type == "local_verification"
        assert set(verification.capabilities) == {"context_package:v3", "verification"}


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
        "modules": 8,
        "profiles": 2,
        "workers": 2,
        "runtimes": 2,
    }
    with database.session() as session:
        policies = session.scalars(select(PlanningProfileModel.policy))
        assert {policy["proposal_schema_version"] for policy in policies} == {2, 3}
        worker_instances = session.scalars(
            select(ModuleInstanceModel).where(
                ModuleInstanceModel.workspace_id == workspace_id,
                ModuleInstanceModel.instance_name.in_(("worker", "second-worker")),
            )
        ).all()
        assert {instance.instance_name for instance in worker_instances} == {
            "worker",
            "second-worker",
        }
        assert all(
            instance.config["worker.completion_policy"] == "initial_turn"
            for instance in worker_instances
        )
        workers = session.scalars(
            select(WorkerModel).where(WorkerModel.workspace_id == workspace_id)
        ).all()
        assert len(workers) == 2
        assert all("turn-completion:v1" in worker.capabilities for worker in workers)
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
def test_legacy_worker_config_requires_a_new_run_to_opt_in(
    database: Database,
    workspace_id: UUID,
    clean_database: None,
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    state_root = tmp_path / "state"
    legacy_run_id = uuid4()
    legacy = _manifest(
        tmp_path,
        workspace_id,
        repository,
        base_commit,
        run_id=legacy_run_id,
        worker_config_version=1,
    )
    first = bootstrap_manifest(
        legacy,
        database=database,
        mode="apply",
        state_root=state_root,
    )

    with database.session() as session:
        for instance_name in ("worker", "second-worker"):
            instance = session.scalar(
                select(ModuleInstanceModel).where(
                        ModuleInstanceModel.workspace_id == workspace_id,
                        ModuleInstanceModel.instance_name == instance_name,
                        ModuleInstanceModel.id == first.module_ids[
                            "worker" if instance_name == "worker" else "second_worker"
                        ],
                )
            )
            assert instance is not None
            instance.config = {
                key: value
                for key, value in instance.config.items()
                if key != "worker.completion_policy"
            }
        session.commit()

    with pytest.raises(BootstrapConflict, match="module instance config_version conflict"):
        bootstrap_manifest(
            legacy,
            database=database,
            mode="apply",
            state_root=state_root,
        )

    revised = _manifest(
        tmp_path,
        UUID("00000000-0000-0000-0000-000000000099"),
        repository,
        _git(repository, "rev-parse", "HEAD"),
        run_id=uuid4(),
        worker_config_version=2,
    )
    second = bootstrap_manifest(
        revised,
        database=database,
        mode="apply",
        state_root=state_root,
    )

    with database.session() as session:
        legacy_worker = session.get(ModuleInstanceModel, first.module_ids["worker"])
        legacy_second = session.get(ModuleInstanceModel, first.module_ids["second_worker"])
        fresh_worker = session.get(ModuleInstanceModel, second.module_ids["worker"])
        fresh_second = session.get(ModuleInstanceModel, second.module_ids["second_worker"])
        assert legacy_worker is not None and legacy_second is not None
        assert fresh_worker is not None and fresh_second is not None
        assert "worker.completion_policy" not in legacy_worker.config
        assert "worker.completion_policy" not in legacy_second.config
        assert fresh_worker.config["worker.completion_policy"] == "initial_turn"
        assert fresh_second.config["worker.completion_policy"] == "initial_turn"


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
