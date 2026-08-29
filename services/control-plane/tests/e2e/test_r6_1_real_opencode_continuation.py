"""Opt-in R6.1 vertical gate for OpenCode continuation fallback."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from r6_1_fake_runtime import activate_fake_target
from seed_r6_1_revision import seed_revision_fixture
from sqlalchemy import func, select, update

pytest_plugins = ("integration.conftest",)

from sidera_control_plane.adapters.acp_process import ACPProcessTransport
from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
)
from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.execution_commands import (
    ExecutionCommandHandler,
    ExecutionInputCommandHandler,
)
from sidera_control_plane.application.input_dispatcher import InputDispatcher
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
)
from sidera_control_plane.application.planning_runner import PlanningRunner
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactSectionBody,
    KnowledgeCatalogPort,
    SearchHit,
)
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    STOP_TOPIC,
    GatewayExecutionResult,
    WorkerGateway,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import ExecutionSessionStatus
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    ExecutionBinding,
    ModuleLifecycle,
    SandboxCapability,
    StabilityLevel,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.plan_revisions import (
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextPackageAcknowledgementModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    GatewayOperationModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    WorkerItemModel,
    WorkerModel,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_to_payload,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


class _StaticPlannerFactory:
    def __init__(self, adapter: FakePlannerAdapter) -> None:
        self._adapter = adapter

    def create(
        self, definition: Any, instance: Any, profile_snapshot: Any
    ) -> FakePlannerAdapter:
        del definition, instance, profile_snapshot
        return self._adapter


class _EmptyCatalog(KnowledgeCatalogPort):
    def search(
        self,
        workspace_id: UUID,
        query: str,
        *,
        tags=(),
        source_types=(),
        status="active",
        limit=20,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[SearchHit, ...]:
        del workspace_id, query, tags, source_types, status, limit
        del correlation_id, caller_request_id
        return ()

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        del workspace_id, page_id, revision_id, correlation_id, caller_request_id
        raise AssertionError("test context has no exact Knowledge refs")

    def materialize(
        self,
        workspace_id: UUID,
        sections,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        del workspace_id, sections, correlation_id, caller_request_id
        raise AssertionError("test context has no exact Knowledge refs")


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
    _git(repository, "config", "user.name", "SIDERA R6.1 E2E")
    _git(repository, "config", "user.email", "r6-1-e2e@sidera.local")
    (repository / "README.md").write_text("base\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _capture_opencode_configs(monkeypatch) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []
    original_spawn = ACPProcessTransport.spawn

    def spawn(
        executable: str,
        *,
        cwd: Path,
        stderr_limit_bytes: int = 64 * 1024,
        env: Mapping[str, str] | None = None,
    ) -> ACPProcessTransport:
        raw_config = env.get("OPENCODE_CONFIG_CONTENT") if env is not None else None
        if raw_config:
            parsed = json.loads(raw_config)
            if not isinstance(parsed, dict):
                raise AssertionError("OpenCode config must be a JSON object")
            captured.append(parsed)
        return original_spawn(
            executable,
            cwd=cwd,
            stderr_limit_bytes=stderr_limit_bytes,
            env=env,
        )

    monkeypatch.setattr(ACPProcessTransport, "spawn", staticmethod(spawn))
    return captured


def _assignment_message(
    workspace_id: UUID,
    execution_id: UUID,
    lease_id: UUID,
    *,
    fencing_token: int = 1,
) -> OutboxMessage:
    return OutboxMessage(
        id=uuid4(),
        event_id=uuid4(),
        topic=ASSIGNMENT_TOPIC,
        payload={
            "workspace_id": str(workspace_id),
            "aggregate_id": str(execution_id),
            "data": {"lease_id": str(lease_id), "fencing_token": fencing_token},
        },
        attempts=1,
        claim_token=uuid4(),
    )


def _message_from_row(row: OutboxMessageModel) -> OutboxMessage:
    return OutboxMessage(
        id=row.id,
        event_id=row.event_id,
        topic=row.topic,
        payload=dict(row.payload),
        attempts=max(1, row.attempts),
        claim_token=uuid4(),
    )


def _service(database: Database, now: datetime) -> CommandService:
    return CommandService(
        lambda: SqlAlchemyUnitOfWork(database),
        (
            ExecutionCommandHandler(lambda: now, lease_ttl_seconds=3_600),
            ExecutionInputCommandHandler(lambda: now),
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
        actor_id="user:r6-1-opencode-e2e",
        target_id=target_id,
        payload=payload,
        expected_version=expected_version,
        idempotency_key=f"{command_type}:{command_id}",
        correlation_id=uuid4(),
    )


def _configure_real_source(
    database: Database,
    uow_factory,
    *,
    fixture,
    workspace_id: UUID,
    repository: Path,
    revision: str,
    executable: str,
    model: str,
    profile: WorkerAdapterCapabilityProfile,
    now: datetime,
) -> dict[str, UUID | int]:
    """Convert only Worker B's deterministic runtime facts to OpenCode facts."""
    with uow_factory() as uow:
        old_session = uow.sessions.get(
            fixture.worker_b_session_id,
            workspace_id,
            for_update=True,
        )
        if old_session is None:
            raise AssertionError("fixture source Session is missing")
        previous_version = old_session.version
        old_session.close(lost=False, now=now)
        uow.sessions.save(old_session, previous_version)

    adapter_definition_id = uuid4()
    workspace_definition_id = uuid4()
    adapter_instance_id = uuid4()
    workspace_instance_id = uuid4()
    capabilities = capability_profile_to_payload(profile)
    workspace_profile = WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.FILESYSTEM,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
    )
    workspace_capabilities = capability_profile_to_payload(workspace_profile)

    with database.session() as session:
        binding = session.get(
            ExecutionBindingModel,
            uuid5(
                UUID("f6000000-0000-0000-0000-000000000001"),
                f"{workspace_id}:binding:{fixture.worker_item_ids['worker-b']}",
            ),
        )
        if binding is None:
            raise AssertionError("fixture source binding is missing")
        worker_id = binding.worker_id
        session.add_all(
            [
                ModuleDefinitionModel(
                    id=adapter_definition_id,
                    workspace_id=workspace_id,
                    module_type="worker_adapter",
                    provider="opencode",
                    interface_version="sidera.worker-adapter.v1",
                    config_schema_version=1,
                    capabilities=capabilities,
                    lifecycle=ModuleLifecycle.ACTIVE.value,
                ),
                ModuleDefinitionModel(
                    id=workspace_definition_id,
                    workspace_id=workspace_id,
                    module_type="workspace_provider",
                    provider="git-worktree",
                    interface_version="sidera.workspace-provider.v1",
                    config_schema_version=1,
                    capabilities=workspace_capabilities,
                    lifecycle=ModuleLifecycle.ACTIVE.value,
                ),
                ModuleInstanceModel(
                    id=adapter_instance_id,
                    workspace_id=workspace_id,
                    definition_id=adapter_definition_id,
                    instance_name="r6-1-opencode-adapter",
                    config_version=1,
                    config_namespace="opencode",
                    config={
                        "opencode.executable": executable,
                        "opencode.model": model,
                    },
                    secret_refs=[],
                    lifecycle=ModuleLifecycle.ACTIVE.value,
                ),
                ModuleInstanceModel(
                    id=workspace_instance_id,
                    workspace_id=workspace_id,
                    definition_id=workspace_definition_id,
                    instance_name="r6-1-opencode-workspace",
                    config_version=1,
                    config_namespace="workspace",
                    config={
                        "workspace.repository_ref": str(repository),
                        "workspace.base_revision": revision,
                        "workspace.expected_outputs": ["diff"],
                    },
                    secret_refs=[],
                    lifecycle=ModuleLifecycle.ACTIVE.value,
                ),
            ]
        )
        session.flush()
        session.execute(
            update(WorkerModel)
            .where(
                WorkerModel.id == worker_id,
                WorkerModel.workspace_id == workspace_id,
            )
            .values(
                adapter_type="opencode",
                status="reserved",
                version=WorkerModel.version + 1,
            )
        )
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == fixture.worker_b_execution_id,
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(
                status="leased",
                started_at=None,
                finished_at=None,
                stop_requested=False,
                version=ExecutionModel.version + 1,
            )
        )
        session.execute(
            update(WorkerItemModel)
            .where(
                WorkerItemModel.id == fixture.worker_item_ids["worker-b"],
                WorkerItemModel.workspace_id == workspace_id,
            )
            .values(
                acknowledged_context_version=0,
                version=WorkerItemModel.version + 1,
            )
        )
        session.execute(
            update(ExecutionBindingModel)
            .where(
                ExecutionBindingModel.id == binding.id,
                ExecutionBindingModel.workspace_id == workspace_id,
            )
            .values(
                adapter_module_id=adapter_instance_id,
                workspace_module_id=workspace_instance_id,
                adapter_config_version=1,
                workspace_config_version=1,
                capability_snapshot=capabilities,
                version=ExecutionBindingModel.version + 1,
            )
        )
        session.commit()
    return {
        "worker_id": worker_id,
        "binding_id": binding.id,
        "adapter_instance_id": adapter_instance_id,
        "workspace_instance_id": workspace_instance_id,
        "lease_id": binding.lease_id,
        "fencing_token": binding.fencing_token,
    }


def _activate_real_target(
    uow_factory,
    *,
    execution_id: UUID,
    worker_item_id: UUID,
    workspace_id: UUID,
    worker_id: UUID,
    adapter_instance_id: UUID,
    workspace_instance_id: UUID,
    profile: WorkerAdapterCapabilityProfile,
    now: datetime,
) -> tuple[UUID, int]:
    with uow_factory() as uow:
        execution = uow.executions.get(execution_id, workspace_id, for_update=True)
        worker_item = uow.worker_items.get(
            worker_item_id, workspace_id, for_update=True
        )
        worker = uow.workers.get(worker_id, workspace_id, for_update=True)
        if execution is None or worker_item is None or worker is None:
            raise AssertionError("real target runtime facts are missing")
        if (
            uow.context_packages.get_for_consumer(
                ContextPurpose.EXECUTION, execution.id, workspace_id
            )
            is None
        ):
            raise AssertionError("real target context package is missing")

        fencing_token = max(1, execution.last_fencing_token + 1)
        lease = AssignmentLease.issue(
            lease_id=uuid5(
                NAMESPACE_URL, f"sidera.r6-1.real-target:{execution.id}:lease"
            ),
            workspace_id=workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            fencing_token=fencing_token,
            assignment_reason={"fixture": "r6.1-real-opencode-continuation"},
        )
        previous_execution_version = execution.version
        previous_worker_version = worker.version
        worker.reserve()
        execution.assign(
            lease_id=lease.id,
            worker_id=worker.id,
            fencing_token=fencing_token,
        )
        binding = ExecutionBinding(
            id=uuid5(NAMESPACE_URL, f"sidera.r6-1.real-target:{execution.id}:binding"),
            workspace_id=workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            adapter_module_id=adapter_instance_id,
            workspace_module_id=workspace_instance_id,
            adapter_config_version=1,
            workspace_config_version=1,
            capability_snapshot=profile,
            lease_id=lease.id,
            fencing_token=fencing_token,
            version=1,
        )
        uow.leases.add(lease)
        uow.session.flush()
        uow.bindings.add(binding)
        uow.executions.save(execution, previous_execution_version)
        uow.workers.save(worker, previous_worker_version)
        return lease.id, fencing_token


def _confirm_revision(
    client: TestClient,
    database: Database,
    uow_factory,
    *,
    fixture,
    workspace_id: UUID,
    now: datetime,
) -> tuple[UUID, UUID]:
    with uow_factory() as uow:
        root_task = uow.tasks.get(fixture.root_task_id, workspace_id)
        base_snapshot = uow.plans.get(fixture.base_snapshot_id, workspace_id)
        if root_task is None or base_snapshot is None:
            raise AssertionError("revision fixture plan facts are missing")

    submit = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "SubmitPlanRevision",
            "target_type": "task",
            "actor": {"type": "local", "id": "operator"},
            "target_id": str(fixture.root_task_id),
            "expected_version": root_task.version,
            "idempotency_key": "r6-1-real-opencode-submit",
            "correlation_id": str(uuid4()),
            "payload": {
                "root_task_id": str(fixture.root_task_id),
                "expected_base_snapshot_id": str(base_snapshot.id),
                "expected_base_generation": base_snapshot.generation,
                "expected_base_content_hash": plan_snapshot_content_hash(base_snapshot),
                "objective": "Revise the running plan through continuation fallback",
                "constraints": ["Keep stable WorkerItem identities reusable"],
                "acceptance_criteria": ["The revised plan is reviewable"],
                "planning_profile_id": str(fixture.planning_profile_id),
                "planning_profile_version": 1,
            },
        },
    )
    assert submit.status_code == 201, submit.text
    planning_request_id = UUID(submit.json()["data"]["planning_request_id"])

    context_result = ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=_EmptyCatalog(),
        owner_id="r6-1-real-opencode-context",
        clock=lambda: datetime.now(UTC),
    ).run_once(10)
    assert context_result.claimed == 1
    assert context_result.ready == 1
    assert context_result.failed == 0

    with TemporaryDirectory(prefix="sidera-r6-1-real-planning-") as temp_root:
        planning_result = PlanningRunner(
            uow_factory=uow_factory,
            adapter_factory=_StaticPlannerFactory(
                FakePlannerAdapter(FakePlannerConfig(scenario="revision_control_room"))
            ),
            owner_id="r6-1-real-opencode-planning",
            lease_ttl_seconds=60,
            lease_renew_interval_seconds=1,
            clock=lambda: datetime.now(UTC),
            context_materializer=ContextMaterializer(_EmptyCatalog()),
            planner_temp_root=Path(temp_root),
        ).run_once(1)
    assert planning_result.claimed == 1
    assert planning_result.succeeded == 1, planning_result
    assert planning_result.failed == 0

    with uow_factory() as uow:
        request = uow.planning_requests.get(planning_request_id, workspace_id)
        if request is None:
            raise AssertionError("revision planning request is missing")
        proposals = uow.planning_records.list_proposals(
            planning_request_id, workspace_id
        )
        assert len(proposals) == 1
        proposal = proposals[0]
        assert isinstance(proposal.payload, PlanningRevisionProposalPayloadV3)
        assert proposal.payload.base_descriptor.plan_generation == 3
        assert proposal.derived_diff.impact_counts.patch == 1
        assert proposal.derived_diff.impact_counts.create == 1
        assert proposal.derived_diff.impact_counts.retire == 1

        confirm = client.post(
            "/api/v1/commands",
            json={
                "command_id": str(uuid4()),
                "workspace_id": str(workspace_id),
                "command_type": "ConfirmPlanRevision",
                "target_type": "planning_request",
                "actor": {"type": "local", "id": "operator"},
                "target_id": str(request.id),
                "expected_version": request.version,
                "idempotency_key": "r6-1-real-opencode-confirm",
                "correlation_id": str(uuid4()),
                "payload": {
                    "expected_proposal_revision": proposal.revision,
                    "expected_proposal_content_hash": proposal.content_hash,
                    "expected_diff_hash": proposal.derived_diff.diff_hash,
                    "expected_base_snapshot_id": str(base_snapshot.id),
                    "expected_base_content_hash": plan_snapshot_content_hash(
                        base_snapshot
                    ),
                    "profile_id": str(fixture.planning_profile_id),
                    "profile_version": 1,
                },
            },
        )
    assert confirm.status_code == 201, confirm.text
    data = confirm.json()["data"]
    assert data["plan_generation"] == 4
    return UUID(data["plan_transition_id"]), UUID(data["proposal_revision_id"])


def _active_session_count(
    database: Database, worker_item_id: UUID, workspace_id: UUID
) -> int:
    with database.session() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(ExecutionSessionModel)
                .join(
                    ExecutionModel,
                    ExecutionModel.id == ExecutionSessionModel.execution_id,
                )
                .where(
                    ExecutionSessionModel.workspace_id == workspace_id,
                    ExecutionSessionModel.status == ExecutionSessionStatus.ACTIVE.value,
                    ExecutionModel.worker_item_id == worker_item_id,
                )
            )
            or 0
        )


def test_real_opencode_continuation_fallback(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    uow_factory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    if os.environ.get("SIDERA_R6_1_REAL_OPENCODE") != "1":
        pytest.skip("set SIDERA_R6_1_REAL_OPENCODE=1 to run the real OpenCode gate")

    executable = os.environ.get("SIDERA_OPENCODE_EXECUTABLE")
    if not executable:
        pytest.fail("SIDERA_OPENCODE_EXECUTABLE is required for the opt-in gate")
    model = os.environ.get("SIDERA_OPENCODE_MODEL")
    if not model or not model.strip():
        pytest.fail("SIDERA_OPENCODE_MODEL is required; no model fallback is allowed")
    try:
        timeout_seconds = float(
            os.environ.get("SIDERA_R6_1_REAL_OPENCODE_TIMEOUT_SECONDS", "60")
        )
    except ValueError as exc:
        pytest.fail("SIDERA_R6_1_REAL_OPENCODE_TIMEOUT_SECONDS must be numeric")
        raise AssertionError from exc
    if timeout_seconds <= 0:
        pytest.fail("real OpenCode timeout must be positive")

    captured_configs = _capture_opencode_configs(monkeypatch)
    from sidera_control_plane.opencode_preflight import run_preflight

    preflight = run_preflight(
        executable=executable,
        expected_version=os.environ.get("SIDERA_OPENCODE_EXPECTED_VERSION"),
        timeout_seconds=timeout_seconds,
    )
    if preflight.status != "ok" or preflight.capability_profile is None:
        pytest.fail(f"OpenCode preflight failed: {preflight.as_dict()}")
    assert preflight.resolved_executable is not None
    assert preflight.version
    assert preflight.capability_profile.context_patch_versions == ()

    repository, revision = _repository(tmp_path)
    assert os.access(repository, os.W_OK)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    pinned_probe = OpenCodeACPAdapter(
        executable=preflight.resolved_executable,
        model=model.strip(),
        request_timeout_seconds=timeout_seconds,
        stop_grace_seconds=2,
    ).probe({"cwd": str(repository)})
    assert pinned_probe.context_patch_versions == ()

    now = datetime.now(UTC)
    fixture = seed_revision_fixture(database, workspace_id, now=now)
    runtime = _configure_real_source(
        database,
        uow_factory,
        fixture=fixture,
        workspace_id=workspace_id,
        repository=repository,
        revision=revision,
        executable=preflight.resolved_executable,
        model=model.strip(),
        profile=preflight.capability_profile,
        now=now,
    )

    workspace_manager = GitWorktreeWorkspaceManager(workspace_root)
    adapters: dict[UUID, OpenCodeACPAdapter] = {}

    def adapter_factory(instance) -> OpenCodeACPAdapter:
        configured_model = instance.config.get("opencode.model")
        if configured_model != model.strip():
            raise AssertionError("real gate adapter model changed during binding")
        adapter = adapters.get(instance.id)
        if adapter is None:
            adapter = OpenCodeACPAdapter(
                executable=preflight.resolved_executable,
                model=model.strip(),
                workspace_root=workspace_root,
                request_timeout_seconds=timeout_seconds,
                stop_grace_seconds=2,
            )
            adapters[instance.id] = adapter
        return adapter

    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"opencode": adapter_factory},
        workspace_factories={"git-worktree": lambda _instance: workspace_manager},
        context_materializer=ContextMaterializer(_EmptyCatalog()),
        assignment_lease_ttl_seconds=3_600,
        clock=lambda: now,
    )
    source_result: GatewayExecutionResult | None = None
    target_result: GatewayExecutionResult | None = None
    try:
        source_result = gateway.handle(
            _assignment_message(
                workspace_id,
                fixture.worker_b_execution_id,
                runtime["lease_id"],
                fencing_token=int(runtime["fencing_token"]),
            )
        )
        assert source_result.session_id != fixture.worker_b_session_id
        source_path = workspace_root / str(fixture.worker_b_execution_id)
        assert source_path.is_dir()
        assert os.access(source_path, os.W_OK)
        assert len(adapters[runtime["adapter_instance_id"]].active_processes()) == 1
        assert (
            _active_session_count(
                database, fixture.worker_item_ids["worker-b"], workspace_id
            )
            == 1
        )

        with database.session() as session:
            source_session = session.get(
                ExecutionSessionModel, source_result.session_id
            )
            assert source_session is not None
            assert source_session.status == ExecutionSessionStatus.ACTIVE.value
            assert source_session.last_sequence >= 1
            assert source_session.process_ref
            events = session.scalars(
                select(ExecutionEventModel).where(
                    ExecutionEventModel.session_id == source_result.session_id
                )
            ).all()
            assert events
            session.execute(
                update(ExecutionInputModel)
                .where(
                    ExecutionInputModel.id == fixture.worker_b_input_id,
                    ExecutionInputModel.workspace_id == workspace_id,
                )
                .values(session_id=source_result.session_id)
            )
            session.commit()

        plan_transition_id, proposal_revision_id = _confirm_revision(
            client,
            database,
            uow_factory,
            fixture=fixture,
            workspace_id=workspace_id,
            now=now,
        )
        coordinator = PlanTransitionCoordinator(
            uow_factory=uow_factory,
            owner_id="r6-1-real-opencode-coordinator",
            clock=lambda: now,
            gateway=gateway,
        )
        first_transition = coordinator.run_once(20)
        assert first_transition.failed == 0, first_transition
        with uow_factory() as uow:
            transitions = uow.worker_transitions.list_for_plan_transition(
                plan_transition_id, workspace_id
            )
            source_transition = next(
                item
                for item in transitions
                if item.worker_item_id == fixture.worker_item_ids["worker-b"]
            )
            assert source_transition.action is WorkerTransitionAction.CONTINUE
            assert (
                source_transition.source_execution_id == fixture.worker_b_execution_id
            )
            assert source_transition.source_session_id == source_result.session_id
            assert source_transition.status.value == "waiting_safe_boundary"
            source_execution = uow.executions.get(
                fixture.worker_b_execution_id, workspace_id
            )
            assert source_execution is not None
            assert source_execution.stop_requested
        assert len(adapters[runtime["adapter_instance_id"]].active_processes()) == 1

        with database.session() as session:
            stop_row = session.scalar(
                select(OutboxMessageModel).where(OutboxMessageModel.topic == STOP_TOPIC)
            )
            assert stop_row is not None
            stop_message = _message_from_row(stop_row)
        gateway.handle(stop_message)
        assert len(adapters[runtime["adapter_instance_id"]].active_processes()) == 0
        assert (
            _active_session_count(
                database, fixture.worker_item_ids["worker-b"], workspace_id
            )
            == 0
        )
        with database.session() as session:
            old_session = session.get(ExecutionSessionModel, source_result.session_id)
            old_execution = session.get(ExecutionModel, fixture.worker_b_execution_id)
            queued_input = session.get(ExecutionInputModel, fixture.worker_b_input_id)
            assert old_session is not None and old_session.status == "closed"
            assert old_execution is not None and old_execution.status == "canceled"
            assert queued_input is not None and queued_input.status == "queued"

        second_transition = coordinator.run_once(20)
        assert second_transition.failed == 0, second_transition
        with uow_factory() as uow:
            continuation_request = uow.execution_requests.get_by_proposal_worker_item(
                proposal_revision_id, "worker-b", workspace_id
            )
            create_request = uow.execution_requests.get_by_proposal_worker_item(
                proposal_revision_id, "worker-e", workspace_id
            )
            assert continuation_request is not None
            assert (
                continuation_request.source_execution_id
                == fixture.worker_b_execution_id
            )
            assert continuation_request.source_session_id == source_result.session_id
            assert create_request is not None
            assert (
                uow.execution_inputs.get(
                    fixture.worker_b_input_id, workspace_id
                ).status.value
                == "superseded"
            )

        activation = ActivationReconciler(
            uow_factory=uow_factory,
            owner_id="r6-1-real-opencode-activation",
            clock=lambda: now,
        ).run_once(20)
        assert activation.failed == 0, activation
        with uow_factory() as uow:
            continuation_request = uow.execution_requests.get_by_proposal_worker_item(
                proposal_revision_id, "worker-b", workspace_id
            )
            create_request = uow.execution_requests.get_by_proposal_worker_item(
                proposal_revision_id, "worker-e", workspace_id
            )
            assert continuation_request is not None
            assert continuation_request.first_execution_id is not None
            assert create_request is not None
            assert create_request.first_execution_id is not None
            target_execution_id = continuation_request.first_execution_id
            create_execution_id = create_request.first_execution_id
            target_item_id = continuation_request.worker_item_id
            create_item_id = create_request.worker_item_id

        packages = ContextPackageWorker(
            uow_factory=uow_factory,
            catalog=_EmptyCatalog(),
            owner_id="r6-1-real-opencode-target-context",
            clock=lambda: now,
        ).run_once(20)
        assert packages.failed == 0, packages
        activate_fake_target(
            uow_factory,
            create_execution_id,
            create_item_id,
            workspace_id,
            now=now,
        )
        target_lease_id, target_fence = _activate_real_target(
            uow_factory,
            execution_id=target_execution_id,
            worker_item_id=target_item_id,
            workspace_id=workspace_id,
            worker_id=runtime["worker_id"],
            adapter_instance_id=runtime["adapter_instance_id"],
            workspace_instance_id=runtime["workspace_instance_id"],
            profile=preflight.capability_profile,
            now=now,
        )
        target_result = gateway.handle(
            _assignment_message(
                workspace_id,
                target_execution_id,
                target_lease_id,
                fencing_token=target_fence,
            )
        )
        assert target_result.session_id != source_result.session_id
        assert len(adapters[runtime["adapter_instance_id"]].active_processes()) == 1
        assert (
            _active_session_count(
                database, fixture.worker_item_ids["worker-b"], workspace_id
            )
            == 1
        )
        with uow_factory() as uow:
            target_execution = uow.executions.get(target_execution_id, workspace_id)
            source_execution = uow.executions.get(
                fixture.worker_b_execution_id, workspace_id
            )
            target_session = uow.sessions.get_any(
                target_result.session_id, workspace_id
            )
            source_session = uow.sessions.get_any(
                source_result.session_id, workspace_id
            )
            target_package = uow.context_packages.get_for_consumer(
                ContextPurpose.EXECUTION, target_execution_id, workspace_id
            )
            target_ack = (
                uow.context_packages.get_ack(
                    target_package.package_id,
                    ContextConsumerType.WORKER,
                    target_execution_id,
                    workspace_id,
                )
                if target_package is not None
                else None
            )
            assert target_execution is not None
            assert source_execution is not None
            assert target_session is not None and source_session is not None
            assert target_execution.source_execution_id == source_execution.id
            assert target_execution.source_session_id == source_session.id
            assert target_session.process_ref != source_session.process_ref
            assert target_package is not None
            assert target_package.context_version == 2
            assert target_ack is not None
            assert target_ack.context_version == 2

        final_transition = coordinator.run_once(20)
        assert final_transition.failed == 0, final_transition
        with uow_factory() as uow:
            plan = uow.plan_transitions.get(plan_transition_id, workspace_id)
            transitions = uow.worker_transitions.list_for_plan_transition(
                plan_transition_id, workspace_id
            )
            assert plan is not None and plan.status.value == "converged"
            by_item = {item.worker_item_id: item for item in transitions}
            assert (
                by_item[fixture.worker_item_ids["worker-b"]].status.value == "converged"
            )
            assert (
                by_item[fixture.worker_item_ids["worker-b"]].action
                is WorkerTransitionAction.CONTINUE
            )

        with database.session() as session:
            target_session_row = session.get(
                ExecutionSessionModel, target_result.session_id
            )
            assert target_session_row is not None
            input_version = target_session_row.version
        _service(database, now).handle(
            _command(
                workspace_id=workspace_id,
                command_type="SendExecutionInput",
                target_type="execution_session",
                target_id=target_result.session_id,
                payload={
                    "context": {
                        "worker_item_id": str(fixture.worker_item_ids["worker-b"]),
                        "plan_generation": 4,
                        "context_version": 2,
                        "execution_id": str(target_execution_id),
                        "session_id": str(target_result.session_id),
                        "session_version": input_version,
                        "worker_id": str(runtime["worker_id"]),
                    },
                    "execution_id": str(target_execution_id),
                    "client_idempotency_key": "r6-1-real-opencode-marker",
                    "text": (
                        "Create a file named SIDERA_R6_1_CONTINUATION_MARKER.txt "
                        "with exactly the text 'r6.1 continuation ok' and reply "
                        "with a short acknowledgement."
                    ),
                    "attachment_refs": [],
                },
                expected_version=input_version,
            )
        )
        dispatch = InputDispatcher(
            lambda: SqlAlchemyUnitOfWork(database), gateway, clock=lambda: now
        ).run_once()
        assert dispatch.delivered == 1, dispatch
        target_path = workspace_root / str(target_execution_id)
        marker = target_path / "SIDERA_R6_1_CONTINUATION_MARKER.txt"
        assert marker.read_text().removesuffix("\n") == "r6.1 continuation ok"
        gateway.sync_session(
            workspace_id=workspace_id,
            execution_id=target_execution_id,
            session_id=target_result.session_id,
            lease_id=target_lease_id,
            fencing_token=target_fence,
        )
        with database.session() as session:
            delivered_input = session.scalar(
                select(ExecutionInputModel).where(
                    ExecutionInputModel.execution_id == target_execution_id,
                    ExecutionInputModel.client_idempotency_key
                    == "r6-1-real-opencode-marker",
                )
            )
            assert delivered_input is not None
            assert delivered_input.status == "delivered"

        with database.session() as session:
            operations = session.scalars(
                select(GatewayOperationModel).where(
                    GatewayOperationModel.execution_id.in_(
                        [fixture.worker_b_execution_id, target_execution_id]
                    )
                )
            ).all()
            assert operations
            assert {operation.status for operation in operations} == {"succeeded"}
            assert {operation.operation_type for operation in operations} >= {
                "prepare_workspace",
                "start_session",
                "send_input",
                "stop_session",
            }
            acknowledgements = session.scalars(
                select(ContextPackageAcknowledgementModel).where(
                    ContextPackageAcknowledgementModel.consumer_id
                    == target_execution_id
                )
            ).all()
            assert len(acknowledgements) == 1

        pinned_configs = [
            config
            for config in captured_configs
            if config.get("model") == model.strip()
        ]
        assert len(pinned_configs) >= 3
        for config in pinned_configs:
            assert config.get("disabled_providers") == ["deepseek"]
            assert config.get("enabled_providers") == ["opencode-go"]
            assert config.get("model") == model.strip()
    finally:
        for adapter in adapters.values():
            for process in adapter.active_processes():
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
