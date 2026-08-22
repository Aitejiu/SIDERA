import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import (
    ExecutionCommandHandler,
    ExecutionInputCommandHandler,
)
from sidera_control_plane.application.input_dispatcher import InputDispatcher
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    STOP_TOPIC,
    WorkerGateway,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.modules import CapabilityProfile
from sidera_control_plane.domain.plans import (
    TaskPlanSnapshot,
    WorkerSlot,
    WorkItem,
    WorkItemKind,
)
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
)
from sidera_control_plane.opencode_preflight import run_preflight
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    PlanSnapshotModel,
    TaskModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "SIDERA E2E")
    _git(repository, "config", "user.email", "e2e@sidera.local")
    (repository / "README.md").write_text("base\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _capability_payload(profile: CapabilityProfile) -> dict[str, str]:
    return {
        "input": profile.input.value,
        "idempotency": profile.idempotency.value,
        "resume": profile.resume.value,
        "stop": profile.stop.value,
        "sandbox": profile.sandbox.value,
        "stability": profile.stability.value,
    }


class FakeWorkspaceProvider:
    def prepare(self, spec, *, operation_id):
        del operation_id
        return ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=spec.workspace_id,
            task_id=spec.task_id,
            execution_id=spec.execution_id,
            checkouts=(
                WorkspaceCheckout(
                    repository_ref=spec.repositories[0].repository_ref,
                    base_revision=spec.repositories[0].base_revision,
                    branch=f"sidera/execution/{spec.execution_id}",
                    current_head=spec.repositories[0].base_revision,
                ),
            ),
            location_ref=f"fake-workspace://{spec.execution_id}",
            retention_seconds=3_600,
            fencing_token=spec.fencing_token,
            created_at=NOW,
        )

    def freeze(self, manifest, *, operation_id):
        del operation_id
        return manifest

    def retain(self, manifest, *, operation_id):
        del operation_id
        return manifest

    def release(self, manifest, *, operation_id):
        del operation_id
        return manifest

    def inspect(self, location_ref):
        raise RuntimeError(f"fake workspace is not inspectable: {location_ref}")


def _seed_execution(
    database: Database,
    workspace_id: UUID,
    repository: Path,
    revision: str,
    executable: str,
    profile: CapabilityProfile,
    *,
    task_id: UUID | None = None,
    name_suffix: str = "",
    adapter_provider: str = "opencode",
    workspace_provider: str = "git-worktree",
) -> dict[str, UUID]:
    task_id = task_id or uuid4()
    worker_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    adapter_definition_id = uuid4()
    workspace_definition_id = uuid4()
    adapter_instance_id = uuid4()
    workspace_instance_id = uuid4()
    capabilities = _capability_payload(profile)
    plan_snapshot_id = uuid4()
    with database.session() as session:
        task = session.get(TaskModel, task_id)
        if task is None:
            snapshot = TaskPlanSnapshot.create(
                snapshot_id=plan_snapshot_id,
                schema_version=1,
                workspace_id=workspace_id,
                root_task_id=task_id,
                generation=1,
                parent_snapshot_id=None,
                source_type="integration",
                source_ref="integration:opencode-e2e",
                title="OpenCode R3 E2E",
                goal_summary="Run a real OpenCode execution with an exact input address.",
                acceptance_refs=("the execution produces a Git diff",),
                work_items=(
                    WorkItem(
                        key="opencode-e2e",
                        task_id=task_id,
                        kind=WorkItemKind.CHILD,
                        title="OpenCode R3 E2E",
                        goal_summary="Run the real OpenCode execution.",
                        acceptance_refs=("the execution produces a Git diff",),
                        milestone_key=None,
                        worker_slot_key="opencode-e2e",
                        expected_outputs=("diff",),
                        source_refs=(),
                    ),
                ),
                worker_slots=(
                    WorkerSlot(
                        key="opencode-e2e",
                        role="implementation",
                        capability_requirements=("python",),
                        worker_policy_ref=None,
                        configuration_ref=None,
                        concurrency_limit=1,
                    ),
                ),
                milestones=(),
                dependencies=(),
                published_by="integration",
                published_at=NOW,
            )
            session.add(
                TaskModel(
                    id=task_id,
                    workspace_id=workspace_id,
                    title="OpenCode R3 E2E",
                    description=(
                        "Reply briefly that the execution is ready. "
                        "Do not modify the repository."
                    ),
                    acceptance_criteria=["the execution produces a Git diff"],
                    status="running",
                    priority=50,
                    requested_by="opencode-e2e",
                    version=1,
                )
            )
            session.flush()
            session.add(
                PlanSnapshotModel(
                    id=snapshot.id,
                    schema_version=snapshot.schema_version,
                    workspace_id=snapshot.workspace_id,
                    root_task_id=snapshot.root_task_id,
                    generation=snapshot.generation,
                    parent_snapshot_id=None,
                    source_type=snapshot.source_type,
                    source_ref=snapshot.source_ref,
                    title=snapshot.title,
                    goal_summary=snapshot.goal_summary,
                    payload=snapshot.to_payload(),
                    published_by=snapshot.published_by,
                    published_at=snapshot.published_at,
                )
            )
        else:
            existing_snapshot_id = session.scalar(
                select(PlanSnapshotModel.id)
                .where(
                    PlanSnapshotModel.workspace_id == workspace_id,
                    PlanSnapshotModel.root_task_id == task_id,
                )
                .order_by(PlanSnapshotModel.generation.desc())
                .limit(1)
            )
            if existing_snapshot_id is None:
                raise AssertionError("OpenCode E2E task is missing its plan snapshot")
            plan_snapshot_id = existing_snapshot_id
        session.add(
            WorkerModel(
                id=worker_id,
                workspace_id=workspace_id,
                adapter_type=adapter_provider,
                display_name=f"{adapter_provider} E2E Worker{name_suffix}",
                status="reserved",
                desired_state="active",
                capabilities=["python"],
                capacity=1,
                current_load=1,
                cost_per_hour_microunits=1,
                startup_latency_ms=1,
                last_heartbeat_at=NOW,
                version=1,
            )
        )
        session.add_all(
            [
                ModuleDefinitionModel(
                    id=adapter_definition_id,
                    workspace_id=workspace_id,
                    module_type="worker_adapter",
                    provider=adapter_provider,
                    interface_version="sidera.worker-adapter.v1",
                    config_schema_version=1,
                    capabilities=capabilities,
                    lifecycle="active",
                ),
                ModuleDefinitionModel(
                    id=workspace_definition_id,
                    workspace_id=workspace_id,
                    module_type="workspace_provider",
                    provider=workspace_provider,
                    interface_version="sidera.workspace-provider.v1",
                    config_schema_version=1,
                    capabilities=capabilities,
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=adapter_instance_id,
                    workspace_id=workspace_id,
                    definition_id=adapter_definition_id,
                    instance_name=f"{adapter_provider}-adapter{name_suffix}",
                    config_version=1,
                    config_namespace="opencode",
                    config={"opencode.executable": executable},
                    secret_refs=(
                        ["opencode.default-credential"]
                        if adapter_provider == "opencode"
                        else []
                    ),
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=workspace_instance_id,
                    workspace_id=workspace_id,
                    definition_id=workspace_definition_id,
                    instance_name=f"{workspace_provider}-workspace{name_suffix}",
                    config_version=1,
                    config_namespace="workspace",
                    config={
                        "workspace.repository_ref": str(repository),
                        "workspace.base_revision": revision,
                        "workspace.expected_outputs": ["diff"],
                    },
                    secret_refs=[],
                    lifecycle="active",
                ),
            ]
        )
        session.commit()
    with database.session() as session:
        session.add(
            ExecutionModel(
                id=execution_id,
                workspace_id=workspace_id,
                task_id=task_id,
                lineage_id=execution_id,
                attempt=1,
                max_attempts=2,
                purpose="Run OpenCode E2E",
                status="leased",
                capability_requirements=["python"],
                pinned_worker_id=None,
                assigned_worker_id=worker_id,
                active_lease_id=None,
                last_fencing_token=1,
                not_before=NOW,
                failure_code=None,
                started_at=None,
                finished_at=None,
                version=2,
                stop_requested=False,
            )
        )
        session.flush()
        session.add(
            AssignmentLeaseModel(
                id=lease_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                status="active",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                renewed_at=None,
                ended_at=None,
                fencing_token=1,
                assignment_reason={"source": "opencode-e2e"},
                version=1,
            )
        )
        session.add(
            ExecutionBindingModel(
                id=uuid4(),
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                adapter_module_id=adapter_instance_id,
                workspace_module_id=workspace_instance_id,
                adapter_config_version=1,
                workspace_config_version=1,
                capability_snapshot=capabilities,
                lease_id=lease_id,
                fencing_token=1,
                version=1,
            )
        )
        session.commit()
    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == execution_id,
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(active_lease_id=lease_id)
        )
        session.commit()
    return {
        "task_id": task_id,
        "plan_snapshot_id": plan_snapshot_id,
        "worker_id": worker_id,
        "execution_id": execution_id,
        "lease_id": lease_id,
        "adapter_instance_id": adapter_instance_id,
    }


def _assignment_message(
    workspace_id: UUID, execution_id: UUID, lease_id: UUID
) -> OutboxMessage:
    return OutboxMessage(
        id=uuid4(),
        event_id=uuid4(),
        topic=ASSIGNMENT_TOPIC,
        payload={
            "workspace_id": str(workspace_id),
            "aggregate_id": str(execution_id),
            "data": {"lease_id": str(lease_id), "fencing_token": 1},
        },
        attempts=1,
        claim_token=uuid4(),
    )


def _service(database: Database) -> CommandService:
    return CommandService(
        lambda: SqlAlchemyUnitOfWork(database),
        (
            ExecutionCommandHandler(lambda: NOW, lease_ttl_seconds=300),
            ExecutionInputCommandHandler(lambda: NOW),
        ),
    )


def _command(
    *,
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    target_id: UUID,
    payload: dict[str, object],
    expected_version: int,
) -> CommandEnvelope:
    command_id = uuid4()
    return CommandEnvelope(
        id=command_id,
        workspace_id=workspace_id,
        command_type=command_type,
        target_type=target_type,
        actor_type="user",
        actor_id="user:opencode-e2e",
        target_id=target_id,
        payload=payload,
        expected_version=expected_version,
        idempotency_key=f"{command_type}:{command_id}",
        correlation_id=uuid4(),
    )


def test_real_opencode_gateway_task_flow(
    database: Database,
    workspace_id: UUID,
    tmp_path: Path,
) -> None:
    executable = os.environ.get("SIDERA_OPENCODE_EXECUTABLE")
    if not executable:
        pytest.skip("SIDERA_OPENCODE_EXECUTABLE is not configured")
    if os.environ.get("SIDERA_RUN_OPENCODE_E2E") != "1":
        pytest.skip("set SIDERA_RUN_OPENCODE_E2E=1 to run the real OpenCode E2E")

    preflight = run_preflight(
        executable=executable,
        expected_version=os.environ.get("SIDERA_OPENCODE_EXPECTED_VERSION"),
        timeout_seconds=30,
    )
    if preflight.status != "ok" or preflight.capability_profile is None:
        pytest.fail(f"OpenCode preflight failed: {preflight.as_dict()}")

    repository, revision = _repository(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    workspace_manager = GitWorktreeWorkspaceManager(workspace_root)
    adapters: dict[UUID, OpenCodeACPAdapter] = {}
    fake_adapter = FakeWorkerAdapter()
    fake_profile = fake_adapter.probe({})
    fake_workspace = FakeWorkspaceProvider()

    def adapter_factory(instance) -> OpenCodeACPAdapter:
        adapter = adapters.get(instance.id)
        if adapter is None:
            adapter = OpenCodeACPAdapter(
                executable=executable,
                workspace_root=workspace_root,
                request_timeout_seconds=30,
                stop_grace_seconds=1,
            )
            adapters[instance.id] = adapter
        return adapter

    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={
            "opencode": adapter_factory,
            "fake": lambda _instance: fake_adapter,
        },
        workspace_factories={
            "git-worktree": lambda _instance: workspace_manager,
            "fake-workspace": lambda _instance: fake_workspace,
        },
        clock=lambda: NOW,
    )
    ids = _seed_execution(
        database,
        workspace_id,
        repository,
        revision,
        executable,
        preflight.capability_profile,
        name_suffix="-first",
    )
    sibling_ids = _seed_execution(
        database,
        workspace_id,
        repository,
        revision,
        executable,
        fake_profile,
        task_id=ids["task_id"],
        name_suffix="-sibling",
        adapter_provider="fake",
        workspace_provider="fake-workspace",
    )
    first_result = None
    try:
        first_result = gateway.handle(
            _assignment_message(workspace_id, ids["execution_id"], ids["lease_id"])
        )
        gateway.handle(
            _assignment_message(
                workspace_id,
                sibling_ids["execution_id"],
                sibling_ids["lease_id"],
            )
        )
        first_path = workspace_root / str(ids["execution_id"])
        (first_path / "README.md").write_text("OpenCode E2E changed\n")
        sync = gateway.sync_session(
            workspace_id=workspace_id,
            execution_id=ids["execution_id"],
            session_id=first_result.session_id,
            lease_id=ids["lease_id"],
            fencing_token=1,
        )
        assert len(sync.artifact_report.artifacts) == 1
        assert len(sync.artifact_report.evidence_claims) == 1

        with database.session() as session:
            first_session = session.get(
                ExecutionSessionModel, first_result.session_id
            )
            assert first_session is not None
            input_version = first_session.version
        _service(database).handle(
            _command(
                workspace_id=workspace_id,
                command_type="SendExecutionInput",
                target_type="execution_session",
                target_id=first_result.session_id,
                payload={
                    "context": {
                        "plan_snapshot_id": str(ids["plan_snapshot_id"]),
                        "root_task_id": str(ids["task_id"]),
                        "work_item_key": "opencode-e2e",
                        "worker_slot_key": "opencode-e2e",
                        "worker_id": str(ids["worker_id"]),
                        "execution_id": str(ids["execution_id"]),
                        "session_id": str(first_result.session_id),
                    },
                    "execution_id": str(ids["execution_id"]),
                    "client_idempotency_key": "opencode-e2e-input-1",
                    "text": "Confirm the workspace is ready.",
                    "attachment_refs": [],
                },
                expected_version=input_version,
            )
        )
        dispatch = InputDispatcher(
            lambda: SqlAlchemyUnitOfWork(database), gateway, clock=lambda: NOW
        ).run_once()
        assert dispatch.delivered == 1
        with database.session() as session:
            input_row = session.query(ExecutionInputModel).one()
            assert input_row.status == "delivered"

        with database.session() as session:
            execution = session.get(ExecutionModel, ids["execution_id"])
            assert execution is not None
            stop_version = execution.version
        stop_command = _command(
            workspace_id=workspace_id,
            command_type="StopExecution",
            target_type="execution",
            target_id=ids["execution_id"],
            payload={},
            expected_version=stop_version,
        )
        _service(database).handle(stop_command)
        with database.session() as session:
            stop_row = session.query(OutboxMessageModel).filter_by(topic=STOP_TOPIC).one()
            stop_message = OutboxMessage(
                id=stop_row.id,
                event_id=stop_row.event_id,
                topic=stop_row.topic,
                payload=dict(stop_row.payload),
                attempts=1,
                claim_token=uuid4(),
            )
        gateway.handle(stop_message)

        with database.session() as session:
            closed = session.get(ExecutionSessionModel, first_result.session_id)
            sibling = session.get(ExecutionModel, sibling_ids["execution_id"])
            assert closed is not None
            assert sibling is not None
            continuation = _service(database).handle(
                _command(
                    workspace_id=workspace_id,
                    command_type="ContinueExecutionSession",
                    target_type="execution_session",
                    target_id=first_result.session_id,
                    payload={"not_before": NOW.isoformat()},
                    expected_version=closed.version,
                )
            )
            assert sibling.status == "running"
            assert sibling.active_lease_id == sibling_ids["lease_id"]
            new_execution = session.get(ExecutionModel, continuation.aggregate_id)
            assert new_execution is not None
            assert new_execution.status == "queued"
            assert new_execution.source_execution_id == ids["execution_id"]
            assert new_execution.source_session_id == first_result.session_id
            artifact = session.query(ArtifactModel).one()
            evidence = session.query(EvidenceClaimModel).one()
            assert artifact.artifact_type == "diff"
            assert evidence.status == "candidate"
    finally:
        adapter = adapters.get(ids["adapter_instance_id"])
        if adapter is not None:
            for process in adapter.active_processes():
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
    if os.environ.get("SIDERA_KEEP_OPENCODE_E2E") == "1" and first_result is not None:
        print(
            json.dumps(
                {
                    "sidera_real_opencode": {
                        "workspace_id": str(workspace_id),
                        "root_task_id": str(ids["task_id"]),
                        "execution_id": str(ids["execution_id"]),
                        "session_id": str(first_result.session_id),
                    }
                },
                sort_keys=True,
            ),
            flush=True,
        )
