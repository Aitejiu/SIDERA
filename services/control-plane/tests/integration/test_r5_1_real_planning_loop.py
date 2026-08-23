from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.worker_gateway import WorkerGateway
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.modules import (
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    SandboxCapability,
    StabilityLevel,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.opencode_preflight import run_preflight
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    EvidenceClaimModel,
    ExecutionModel,
    ExecutionSessionModel,
)
from sidera_control_plane.persistence.outbox_store import SqlAlchemyOutboxStore
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

REAL_GATE = "SIDERA_RUN_REAL_OPENCODE"
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_real_opencode_planning_to_gateway_vertical_path(
    client: TestClient,
    database: Database,
    database_url: str,
    workspace_id: UUID,
    tmp_path: Path,
) -> None:
    if os.environ.get(REAL_GATE) != "1":
        pytest.skip(f"set {REAL_GATE}=1 to run the real OpenCode vertical gate")

    executable = os.environ.get("SIDERA_OPENCODE_EXECUTABLE", "opencode")
    model = os.environ.get("SIDERA_OPENCODE_MODEL", "deepseek/deepseek-chat")
    preflight = run_preflight(
        executable=executable,
        expected_version=os.environ.get("SIDERA_OPENCODE_EXPECTED_VERSION"),
        timeout_seconds=30,
    )
    if preflight.status != "ok" or preflight.capability_profile is None:
        pytest.fail(f"OpenCode preflight failed: {preflight.as_dict()}")

    module_instance_id = _register_real_planner(
        client, workspace_id, executable, model
    )
    profile_id = _register_real_profile(client, workspace_id, module_instance_id)
    submitted = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "SubmitPlanningRequest",
            "workspace",
            {
                "profile_id": str(profile_id),
                "profile_version": 1,
                "objective": "Make one small, reviewable repository change.",
                "constraints": [
                    "Return exactly one subtask and one WorkerItem.",
                    "Use the opencode worker adapter preference.",
                    "Do not ask for clarification.",
                    "Set the proposal to activate immediately after confirmation.",
                ],
                "acceptance_criteria": ["A reviewable Git diff is produced."],
                "context_refs": ["repo://real-opencode-gate"],
                "requested_outputs": ["diff", "evidence"],
                "source_type": "integration",
                "source_ref": "test:r5.1-real-planning-loop",
            },
            idempotency_key="r5.1-real-planning-request",
        ),
    )
    assert submitted.status_code == 201, submitted.text
    request_id = UUID(submitted.json()["data"]["planning_request_id"])

    settings = _settings(database_url, executable, tmp_path)
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    planning_runtime = None
    activation_runtime = None
    scheduler_runtime = None
    worker_gateway: WorkerGateway | None = None
    adapters: list[OpenCodeACPAdapter] = []
    try:
        from sidera_control_plane.activation_worker import build_activation_worker
        from sidera_control_plane.planning_worker import build_planning_worker
        from sidera_control_plane.scheduler_worker import build_scheduler

        planning_runtime = build_planning_worker(settings)
        planning_summary = planning_runtime.worker.run_once(1)
        assert planning_summary.succeeded == 1, planning_summary

        detail = client.get(
            f"/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}"
        )
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert detail_body["status"] == "proposal_ready"
        assert detail_body["current_proposal_revision"] == 1

        proposal_response = client.get(
            f"/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}/proposals/1"
        )
        assert proposal_response.status_code == 200, proposal_response.text
        proposal = proposal_response.json()
        assert proposal["proposal_schema_version"] == 2
        assert proposal["kind"] == "create"
        assert proposal["derived_diff"]["added_subtask_keys"]
        assert proposal["derived_diff"]["added_worker_item_keys"]

        confirmed = client.post(
            "/api/v1/commands",
            json=_command(
                workspace_id,
                "ConfirmPlanningProposal",
                "planning_request",
                {
                    "proposal_revision": 1,
                    "proposal_content_hash": proposal["content_hash"],
                    "profile_id": str(profile_id),
                    "profile_version": 1,
                },
                idempotency_key="r5.1-real-confirm-proposal",
                target_id=request_id,
                expected_version=detail_body["version"],
            ),
        )
        assert confirmed.status_code == 201, confirmed.text
        root_task_id = UUID(confirmed.json()["data"]["root_task_id"])

        repository, revision = _repository(tmp_path)
        worker_facts = _seed_real_worker_facts(
            database,
            workspace_id,
            executable,
            model,
            preflight.capability_profile,
            repository,
            revision,
        )

        activation_runtime = build_activation_worker(settings)
        activation_summary = activation_runtime.worker.run_once(20)
        assert activation_summary.materialized >= 1, activation_summary

        scheduler_runtime = build_scheduler(settings)
        scheduler_summary = scheduler_runtime.scheduler.run_once(20)
        assert scheduler_summary.assigned >= 1, scheduler_summary

        execution_id = _execution_for_root(database, workspace_id, root_task_id)
        lease_id, fencing_token = _read_scheduler_binding(
            database,
            workspace_id,
            execution_id,
            worker_facts,
        )

        worker_root = settings.workspace_root / str(execution_id)
        worker_gateway = _real_gateway(
            database,
            settings,
            executable,
            model,
            adapters,
        )
        gateway_runner = _gateway_runner(database, worker_gateway)
        claimed, processed, failed = gateway_runner.run_once(20)
        assert claimed >= 1
        assert failed == 0, (claimed, processed, failed)
        assert processed >= 1

        session_id = _session_for_execution(database, workspace_id, execution_id)
        (worker_root / "README.md").write_text("SIDERA real planning gate\n")
        sync = worker_gateway.sync_session(
            workspace_id=workspace_id,
            execution_id=execution_id,
            session_id=session_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
        )
        assert sync.artifact_report.artifacts
        assert sync.artifact_report.evidence_claims

        with database.session() as session:
            artifact = session.scalar(
                select(ArtifactModel).where(
                    ArtifactModel.workspace_id == workspace_id,
                    ArtifactModel.execution_id == execution_id,
                )
            )
            evidence = session.scalar(
                select(EvidenceClaimModel).where(
                    EvidenceClaimModel.workspace_id == workspace_id,
                    EvidenceClaimModel.execution_id == execution_id,
                )
            )
            stored_session = session.get(ExecutionSessionModel, session_id)
            assert artifact is not None
            assert evidence is not None
            assert evidence.status == "candidate"
            assert stored_session is not None
    finally:
        for adapter in adapters:
            for process in adapter.active_processes():
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
        if scheduler_runtime is not None:
            scheduler_runtime.database.dispose()
        if activation_runtime is not None:
            activation_runtime.database.dispose()
        if planning_runtime is not None:
            planning_runtime.database.dispose()


def _register_real_planner(
    client: TestClient, workspace_id: UUID, executable: str, model: str
) -> UUID:
    response = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "RegisterPlannerModule",
            "workspace",
            {
                "provider": "opencode-acp-planner",
                "interface_version": "sidera.planner.v1",
                "config_schema_version": 1,
                "capabilities": {
                    "structured_output": "json",
                    "clarification": True,
                    "idempotency": "none",
                    "cancellation": "process",
                    "usage_reporting": "tokens",
                    "max_context_bytes": 128000,
                    "stability": "experimental",
                    "interface_version": "sidera.planner.v1",
                    "proposal_schema_versions": [2],
                },
                "instance_name": "r5.1-real-opencode-planner",
                "config_namespace": "planning",
                "config": {
                    "planning.executable": executable,
                    "planning.model": model,
                },
                "secret_refs": [],
            },
            idempotency_key="r5.1-real-register-planner",
        ),
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["module_instance_id"])


def _register_real_profile(
    client: TestClient, workspace_id: UUID, module_instance_id: UUID
) -> UUID:
    response = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "RegisterPlanningProfile",
            "workspace",
            {
                "profile_key": "r5.1-real-opencode",
                "version": 1,
                "display_name": "R5.1 Real OpenCode Planner",
                "planner_module_instance_id": str(module_instance_id),
                "planner_module_config_version": 1,
                "planner_interface_version": "sidera.planner.v1",
                "prompt_policy_version": "r5.1-real-gate",
                "system_policy": {},
                "proposal_schema_version": 2,
                "allowed_capabilities": ["code"],
                "adapter_preferences": ["opencode"],
                "adapter_forbidden": [],
                "budget": {
                    "max_context_bytes": 64000,
                    "max_tokens": 20000,
                    "max_cost_microunits": 100000000,
                    "max_runtime_seconds": 180,
                    "max_revisions": 2,
                    "max_nodes": 10,
                    "max_concurrency": 1,
                },
                "retry_policy": {
                    "max_retries": 0,
                    "backoff_seconds": [],
                    "unknown_outcome_policy": "block",
                    "invalid_output_policy": "block",
                },
                "default_priority": 50,
                "execution_max_attempts": 1,
                "execution_budget_level": "standard",
                "user_selectable": True,
                "planner_secret_refs": [],
            },
            idempotency_key="r5.1-real-register-profile",
        ),
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["aggregate_id"])


def _command(
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    payload: dict[str, object],
    *,
    idempotency_key: str,
    target_id: UUID | None = None,
    expected_version: int | None = None,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(target_id) if target_id is not None else None,
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _settings(database_url: str, executable: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=database_url,
        local_actor_id="operator",
        planning_opencode_executable=executable,
        planning_opencode_model=os.environ.get(
            "SIDERA_OPENCODE_MODEL", "deepseek/deepseek-chat"
        ),
        opencode_executable=executable,
        opencode_request_timeout_seconds=90,
        opencode_stop_grace_seconds=2,
        planning_opencode_request_timeout_seconds=60,
        planning_opencode_stop_grace_seconds=2,
        planning_temp_root=tmp_path / "planning",
        workspace_root=tmp_path / "workspaces",
        artifact_root=tmp_path / "artifacts",
    )


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "SIDERA R5.1 Real Gate")
    _git(repository, "config", "user.email", "real-gate@sidera.local")
    (repository / "README.md").write_text("SIDERA real gate base\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "real gate base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _seed_real_worker_facts(
    database: Database,
    workspace_id: UUID,
    executable: str,
    model: str,
    worker_profile: WorkerAdapterCapabilityProfile,
    repository: Path,
    revision: str,
) -> dict[str, UUID | WorkerAdapterCapabilityProfile]:
    adapter_definition_id = uuid4()
    workspace_definition_id = uuid4()
    adapter_instance_id = uuid4()
    workspace_instance_id = uuid4()
    worker_id = uuid4()
    workspace_profile = WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
    )
    now = datetime.now(UTC)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(
            ModuleDefinition(
                id=adapter_definition_id,
                workspace_id=workspace_id,
                module_type="worker_adapter",
                provider="opencode",
                interface_version="sidera.worker-adapter.v1",
                config_schema_version=1,
                capabilities=worker_profile,
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_definition(
            ModuleDefinition(
                id=workspace_definition_id,
                workspace_id=workspace_id,
                module_type="workspace_provider",
                provider="git-worktree",
                interface_version="sidera.workspace-provider.v1",
                config_schema_version=1,
                capabilities=workspace_profile,
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_instance(
            ModuleInstance(
                id=adapter_instance_id,
                workspace_id=workspace_id,
                definition_id=adapter_definition_id,
                instance_name="r5.1-real-opencode-worker",
                config_version=1,
                config_namespace="opencode",
                config={
                    "opencode.executable": executable,
                    "opencode.model": model,
                    "opencode.request_timeout_seconds": 90,
                    "opencode.stop_grace_seconds": 2,
                },
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_instance(
            ModuleInstance(
                id=workspace_instance_id,
                workspace_id=workspace_id,
                definition_id=workspace_definition_id,
                instance_name="r5.1-real-git-worktree",
                config_version=1,
                config_namespace="workspace",
                config={
                    "workspace.repository_ref": str(repository),
                    "workspace.base_revision": revision,
                    "workspace.expected_outputs": ["diff"],
                },
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        worker = Worker.register(
            worker_id=worker_id,
            workspace_id=workspace_id,
            adapter_type="opencode",
            display_name="R5.1 Real OpenCode Worker",
            capabilities=frozenset({"code", "python"}),
            capacity=1,
            cost_per_hour_microunits=1,
            startup_latency_ms=1,
            now=now,
        )
        uow.workers.add(worker)
        uow.flush()
        uow.worker_runtime_configurations.add(
            WorkerRuntimeConfiguration.create(
                worker_id=worker.id,
                workspace_id=workspace_id,
                adapter_module_instance_id=adapter_instance_id,
                adapter_module_config_version=1,
                workspace_module_instance_id=workspace_instance_id,
                workspace_module_config_version=1,
            )
        )
    return {
        "adapter_instance_id": adapter_instance_id,
        "workspace_instance_id": workspace_instance_id,
        "worker_id": worker_id,
        "worker_profile": worker_profile,
    }


def _execution_for_root(database: Database, workspace_id: UUID, root_task_id: UUID) -> UUID:
    with database.session() as session:
        execution = session.scalar(
            select(ExecutionModel).where(
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.task_id != root_task_id,
            )
        )
        assert execution is not None
        return execution.id


def _read_scheduler_binding(
    database: Database,
    workspace_id: UUID,
    execution_id: UUID,
    facts: dict[str, UUID | WorkerAdapterCapabilityProfile],
) -> tuple[UUID, int]:
    with SqlAlchemyUnitOfWork(database) as uow:
        execution = uow.executions.get(execution_id, workspace_id, for_update=True)
        assert execution is not None
        assert execution.active_lease_id is not None
        lease = uow.leases.get(execution.active_lease_id, workspace_id)
        assert lease is not None
        worker_id = facts["worker_id"]
        adapter_instance_id = facts["adapter_instance_id"]
        workspace_instance_id = facts["workspace_instance_id"]
        assert isinstance(worker_id, UUID)
        assert isinstance(adapter_instance_id, UUID)
        assert isinstance(workspace_instance_id, UUID)
        configuration = uow.worker_runtime_configurations.get_for_worker(
            worker_id, workspace_id
        )
        assert configuration is not None
        assert configuration.adapter_module_instance_id == adapter_instance_id
        assert configuration.workspace_module_instance_id == workspace_instance_id
        binding = uow.bindings.get_for_execution(execution_id, workspace_id)
        assert binding is not None
        assert binding.worker_id == worker_id
        assert binding.adapter_module_id == configuration.adapter_module_instance_id
        assert binding.workspace_module_id == configuration.workspace_module_instance_id
        assert binding.adapter_config_version == configuration.adapter_module_config_version
        assert binding.workspace_config_version == configuration.workspace_module_config_version
        assert binding.lease_id == lease.id
        assert binding.fencing_token == lease.fencing_token
        return lease.id, lease.fencing_token


def _real_gateway(
    database: Database,
    settings: Settings,
    executable: str,
    model: str,
    adapters: list[OpenCodeACPAdapter],
) -> WorkerGateway:
    workspace_manager = GitWorktreeWorkspaceManager(settings.workspace_root)

    def make_adapter(_instance: ModuleInstance) -> OpenCodeACPAdapter:
        adapter = OpenCodeACPAdapter(
            executable=executable,
            model=model,
            workspace_root=settings.workspace_root,
            request_timeout_seconds=settings.opencode_request_timeout_seconds,
            stop_grace_seconds=settings.opencode_stop_grace_seconds,
        )
        adapters.append(adapter)
        return adapter

    return WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"opencode": make_adapter},
        workspace_factories={"git-worktree": lambda _instance: workspace_manager},
        artifact_content_store=LocalArtifactContentStore(
            settings.artifact_root,
            max_bytes=settings.artifact_max_bytes,
        ),
        clock=lambda: datetime.now(UTC),
    )


def _gateway_runner(database: Database, gateway: WorkerGateway):
    from sidera_control_plane.application.worker_gateway import GatewayAssignmentRunner

    return GatewayAssignmentRunner(
        SqlAlchemyOutboxStore(database, claim_seconds=30),
        gateway,
        clock=lambda: datetime.now(UTC),
    )


def _session_for_execution(database: Database, workspace_id: UUID, execution_id: UUID) -> UUID:
    with database.session() as session:
        item = session.scalar(
            select(ExecutionSessionModel).where(
                ExecutionSessionModel.workspace_id == workspace_id,
                ExecutionSessionModel.execution_id == execution_id,
            )
        )
        assert item is not None
        return item.id
