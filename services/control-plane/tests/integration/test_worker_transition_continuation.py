from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
)
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextConsumerType,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    ExecutionBinding,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevisionReason,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.models import (
    ContextAssemblyRequestModel,
    ExecutionModel,
    ExecutionRequestModel,
    WorkerTransitionModel,
)
from tests.integration.test_context_package_worker import EmptyCatalog
from tests.integration.test_revision_confirmation import (
    _base_plan,
    _confirm_command,
    _derive_payload,
    _revision_payload,
    _seed_revision_confirmation,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _seed_work_context(uow_factory, request_id: UUID, workspace_id: UUID) -> None:
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        revision = WorkContextRevisionFactory().create(
            work_context_id=UUID(int=request_id.int ^ 1),
            workspace_id=workspace_id,
            brief=request.briefs[0],
            revision_number=1,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
            actor="operator",
            created_at=NOW,
        )
        context = WorkContext.create(
            work_context_id=revision.work_context_id,
            workspace_id=workspace_id,
            planning_request_id=request.id,
            first_revision=revision,
            created_at=NOW,
        )
        uow.work_contexts.add(context, revision)


def _worker_capabilities() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(1,),
    )


def _seed_terminal_source(
    uow_factory,
    worker_item_id: UUID,
    task_id: UUID,
    workspace_id: UUID,
) -> dict[str, UUID]:
    execution_id = uuid4()
    worker_id = UUID("63000000-0000-0000-0000-000000000001")
    lease_id = uuid4()
    session_id = uuid4()
    worker = Worker.register(
        worker_id=worker_id,
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name="continuation-source-worker",
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=0,
        startup_latency_ms=0,
        now=NOW,
    )
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task_id,
        worker_item_id=worker_item_id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=2,
        purpose="Complete the previous implementation",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=worker_id,
        not_before=NOW,
    )
    lease = AssignmentLease.issue(
        lease_id=lease_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        issued_at=NOW,
        expires_at=NOW.replace(hour=13),
        fencing_token=1,
        assignment_reason={"test": "continuation-source"},
    )
    session = ExecutionSession.start(
        session_id=session_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        adapter_session_id="continuation-source-session",
        now=NOW,
    )
    with uow_factory() as uow:
        uow.workers.add(worker)
        uow.session.flush()
        uow.executions.add(execution)
        uow.session.flush()
        previous_execution_version = execution.version
        execution.assign(
            lease_id=lease.id,
            worker_id=worker.id,
            fencing_token=lease.fencing_token,
        )
        execution.start(
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        worker.reserve()
        worker.mark_running()
        uow.leases.add(lease)
        uow.workers.save(worker, 1)
        uow.executions.save(execution, previous_execution_version)
        uow.sessions.add(session)
        uow.session.flush()
        previous_session_version = session.version
        session.close(lost=False, now=NOW)
        uow.sessions.save(session, previous_session_version)
        previous_execution_version = execution.version
        execution.succeed(
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        uow.executions.save(execution, previous_execution_version)
        previous_lease_version = lease.version
        lease.revoke(now=NOW)
        uow.leases.save(lease, previous_lease_version)
        previous_worker_version = worker.version
        worker.release()
        uow.workers.save(worker, previous_worker_version)
    return {
        "execution_id": execution_id,
        "worker_id": worker_id,
        "lease_id": lease_id,
        "session_id": session_id,
    }


def _activate_target(
    uow_factory,
    execution_id: UUID,
    worker_item_id: UUID,
    workspace_id: UUID,
    *,
    worker_id: UUID | None = None,
) -> UUID:
    target_worker_id = worker_id or UUID("60000000-0000-0000-0000-000000000001")
    lease_id = UUID("61000000-0000-0000-0000-000000000001")
    session_id = UUID("62000000-0000-0000-0000-000000000001")
    with uow_factory() as uow:
        execution = uow.executions.get(execution_id, workspace_id, for_update=True)
        worker_item = uow.worker_items.get(worker_item_id, workspace_id, for_update=True)
        assert execution is not None and worker_item is not None
        package = uow.context_packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            execution.id,
            workspace_id,
        )
        assert package is not None
        worker = uow.workers.get(target_worker_id, workspace_id, for_update=True)
        worker_is_new = worker is None
        if worker is None:
            worker = Worker.register(
                worker_id=target_worker_id,
                workspace_id=workspace_id,
                adapter_type="fake",
                display_name="continuation-test-worker",
                capabilities=frozenset({"python"}),
                capacity=1,
                cost_per_hour_microunits=0,
                startup_latency_ms=0,
                now=NOW,
            )
        previous_worker_version = worker.version
        worker.reserve()
        worker.mark_running()
        lease = AssignmentLease.issue(
            lease_id=lease_id,
            workspace_id=workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            issued_at=NOW,
            expires_at=NOW.replace(hour=13),
            fencing_token=1,
            assignment_reason={"test": "continuation-activation"},
        )
        previous_execution_version = execution.version
        execution.assign(
            lease_id=lease.id,
            worker_id=worker.id,
            fencing_token=lease.fencing_token,
        )
        execution.start(
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        adapter_definition = ModuleDefinition(
            id=uuid4(),
            workspace_id=workspace_id,
            module_type="worker_adapter",
            provider="fake",
            interface_version="sidera.worker-adapter.v1",
            config_schema_version=1,
            capabilities=_worker_capabilities(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        workspace_definition = ModuleDefinition(
            id=uuid4(),
            workspace_id=workspace_id,
            module_type="workspace_provider",
            provider="fake-workspace",
            interface_version="sidera.workspace-provider.v1",
            config_schema_version=1,
            capabilities=WorkspaceProviderCapabilityProfile(
                isolation=SandboxCapability.ISOLATED_RUNTIME,
                writable=True,
                git_worktree=True,
                stability=StabilityLevel.STABLE,
            ),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        adapter_instance = ModuleInstance(
            id=uuid4(),
            workspace_id=workspace_id,
            definition_id=adapter_definition.id,
            instance_name="continuation-test-adapter",
            config_version=1,
            config_namespace="adapter",
            config={"adapter.command": "fake"},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        workspace_instance = ModuleInstance(
            id=uuid4(),
            workspace_id=workspace_id,
            definition_id=workspace_definition.id,
            instance_name="continuation-test-workspace",
            config_version=1,
            config_namespace="workspace",
            config={"workspace.root": "/tmp/sidera-transition"},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        binding = ExecutionBinding(
            id=uuid4(),
            workspace_id=workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            adapter_module_id=adapter_instance.id,
            workspace_module_id=workspace_instance.id,
            adapter_config_version=adapter_instance.config_version,
            workspace_config_version=workspace_instance.config_version,
            capability_snapshot=_worker_capabilities(),
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            version=1,
        )
        if not worker_item.context_is_synchronized:
            previous_worker_item_version = worker_item.version
            worker_item.acknowledge_context(
                expected_context_version=worker_item.context_version,
                now=NOW,
            )
            uow.worker_items.save(worker_item, previous_worker_item_version)
        uow.modules.add_definition(adapter_definition)
        uow.modules.add_definition(workspace_definition)
        uow.modules.add_instance(adapter_instance)
        uow.modules.add_instance(workspace_instance)
        if worker_is_new:
            uow.workers.add(worker)
        else:
            uow.workers.save(worker, previous_worker_version)
        uow.session.flush()
        uow.leases.add(lease)
        uow.bindings.add(binding)
        uow.executions.save(execution, previous_execution_version)
        uow.sessions.add(
            ExecutionSession.start(
                session_id=session_id,
                workspace_id=workspace_id,
                execution_id=execution.id,
                worker_id=worker.id,
                adapter_session_id="continuation-target-session",
                now=NOW,
                adapter_module_id=adapter_instance.id,
                adapter_config_version=adapter_instance.config_version,
                workspace_module_id=workspace_instance.id,
                workspace_config_version=workspace_instance.config_version,
                capability_snapshot=_worker_capabilities(),
                process_ref="fake-process:continuation-target",
                workspace_ref=workspace_instance.instance_name,
            )
        )
        acknowledgement = ContextAcknowledgement(
            package_id=package.package_id,
            package_hash=package.package_hash,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=execution.id,
            context_version=package.context_version,
            adapter_module_id=adapter_instance.id,
            adapter_config_version=adapter_instance.config_version,
            acknowledged_at=NOW,
            operation_id=uuid5(
                NAMESPACE_URL,
                f"sidera.test.context-ack:{execution.id}",
            ),
        )
        command = CommandEnvelope(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera.test.context-ack-command:{execution.id}",
            ),
            workspace_id=workspace_id,
            command_type="AcknowledgeExecutionContext",
            actor_type="system",
            actor_id="test-gateway",
            target_type="context_package",
            target_id=package.package_id,
            payload={
                "execution_id": str(execution.id),
                "package_id": str(package.package_id),
                "package_hash": package.package_hash,
            },
            expected_version=None,
            idempotency_key=f"test-context-ack:{execution.id}",
            correlation_id=execution.id,
        )
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command.id,
                aggregate_id=package.package_id,
                aggregate_version=1,
                event_ids=(),
            ),
        )
        uow.context_packages.add_ack(
            acknowledgement,
            actor_type="system",
            actor_id="test-gateway",
            causation_id=command.id,
            correlation_id=execution.id,
        )
    return session_id


def test_create_transition_materializes_request_and_context_assembly(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    from fastapi.testclient import TestClient

    seed = _seed_revision_confirmation(
        database,
        workspace_id,
        snapshot=_base_plan(workspace_id),
        payload=replace(
            _derive_payload(_base_plan(workspace_id)),
            execution_intents=tuple(
                replace(
                    intent,
                    expected_artifact_refs=("artifact_type:diff",),
                    required_evidence_refs=("acceptance:implementation",),
                )
                for intent in _derive_payload(_base_plan(workspace_id)).execution_intents
            ),
        ),
    )
    _seed_work_context(uow_factory, seed["request"].id, workspace_id)
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.post(
            "/api/v1/commands",
            json=_confirm_command(seed, idempotency_key="r6-1-2-create-request"),
        )
    assert response.status_code == 201, response.text

    coordinator = PlanTransitionCoordinator(
        uow_factory=uow_factory,
        clock=lambda: NOW,
        owner_id="continuation-test",
    )
    result = coordinator.run_once(limit=10)

    assert result.failed == 0
    with database.session() as session:
        create_transition = session.scalar(
            select(WorkerTransitionModel).where(
                WorkerTransitionModel.action == "create"
            )
        )
        assert create_transition is not None
        assert create_transition.status == "waiting_activation"
        request = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.worker_item_id
                == create_transition.worker_item_id
            )
        )
        assert request is not None
        assert request.plan_generation == 2
        assert request.status in {"blocked", "ready"}

    activation = ActivationReconciler(
        uow_factory=uow_factory,
        clock=lambda: NOW,
        owner_id="continuation-activation-test",
    )
    activation_result = activation.run_once(limit=10)
    assert activation_result.failed == 0
    assert activation_result.materialized == 1
    execution_id = activation_result.execution_ids[0]

    package_result = ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=EmptyCatalog(),
        owner_id="continuation-context-test",
        clock=lambda: NOW,
    ).run_once(limit=10)
    assert package_result.claimed == 1
    assert package_result.ready == 1
    waiting = coordinator.run_once(limit=10)
    assert waiting.failed == 0
    assert waiting.converged == 0
    assert waiting.deferred >= 1

    with database.session() as session:
        assert session.scalar(
            select(WorkerTransitionModel.status).where(
                WorkerTransitionModel.id == create_transition.id
            )
        ) == "waiting_activation"
        request = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.worker_item_id
                == create_transition.worker_item_id
            )
        )
        assert request is not None
        assert request.status == "materialized"
        assert request.first_execution_id == execution_id
        assembly = session.scalar(
            select(ContextAssemblyRequestModel).where(
                ContextAssemblyRequestModel.consumer_id == execution_id
            )
        )
        assert assembly is not None
        assert assembly.status == "ready"

    _activate_target(
        uow_factory,
        execution_id,
        create_transition.worker_item_id,
        workspace_id,
    )
    final = coordinator.run_once(limit=10)
    assert final.failed == 0
    assert final.converged == 1
    with database.session() as session:
        assert session.scalar(
            select(WorkerTransitionModel.status).where(
                WorkerTransitionModel.id == create_transition.id
            )
        ) == "converged"


def test_continuation_reuses_worker_item_and_source_lineage(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    from fastapi.testclient import TestClient

    snapshot = _base_plan(workspace_id)
    payload = replace(
        _revision_payload(snapshot),
        execution_intents=tuple(
            replace(
                intent,
                expected_artifact_refs=("artifact_type:diff",),
                required_evidence_refs=("acceptance:implementation",),
            )
            for intent in _revision_payload(snapshot).execution_intents
        ),
    )
    seed = _seed_revision_confirmation(
        database,
        workspace_id,
        snapshot=snapshot,
        payload=payload,
    )
    source_task_id = seed["snapshot"].subtasks[0].task_id
    assert source_task_id is not None
    source = _seed_terminal_source(
        uow_factory,
        seed["worker_item"].id,
        source_task_id,
        workspace_id,
    )
    _seed_work_context(uow_factory, seed["request"].id, workspace_id)
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.post(
            "/api/v1/commands",
            json=_confirm_command(seed, idempotency_key="r6-1-2-continuation"),
        )
    assert response.status_code == 201, response.text

    coordinator = PlanTransitionCoordinator(
        uow_factory=uow_factory,
        clock=lambda: NOW,
        owner_id="continuation-test",
    )
    first = coordinator.run_once(limit=10)
    assert first.failed == 0
    assert first.converged == 0

    with database.session() as session:
        transition = session.scalar(
            select(WorkerTransitionModel).where(
                WorkerTransitionModel.worker_item_id == seed["worker_item"].id
            )
        )
        assert transition is not None
        assert transition.action == "continue"
        assert transition.status == "waiting_activation"
        assert transition.source_execution_id == source["execution_id"]
        assert transition.source_session_id == source["session_id"]
        request = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.worker_item_id == seed["worker_item"].id
            )
        )
        assert request is not None
        assert request.source_execution_id == source["execution_id"]
        assert request.source_session_id == source["session_id"]

    activation_result = ActivationReconciler(
        uow_factory=uow_factory,
        clock=lambda: NOW,
        owner_id="continuation-activation-test",
    ).run_once(limit=10)
    assert activation_result.failed == 0
    assert activation_result.materialized == 1
    target_execution_id = activation_result.execution_ids[0]

    with database.session() as session:
        target = session.get(ExecutionModel, target_execution_id)
        source_execution = session.get(ExecutionModel, source["execution_id"])
        assert target is not None and source_execution is not None
        assert target.worker_item_id == seed["worker_item"].id
        assert target.source_execution_id == source["execution_id"]
        assert target.source_session_id == source["session_id"]
        assert target.lineage_id == target.id
        assert source_execution.status == "succeeded"
        assert source_execution.active_lease_id is None

    package_result = ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=EmptyCatalog(),
        owner_id="continuation-context-test",
        clock=lambda: NOW,
    ).run_once(limit=10)
    assert package_result.claimed == 1
    assert package_result.ready == 1
    waiting = coordinator.run_once(limit=10)
    assert waiting.failed == 0
    assert waiting.converged == 0
    assert waiting.deferred >= 1

    _activate_target(
        uow_factory,
        target_execution_id,
        seed["worker_item"].id,
        workspace_id,
        worker_id=source["worker_id"],
    )
    final = coordinator.run_once(limit=10)
    assert final.failed == 0
    assert final.converged == 1

    with database.session() as session:
        transition = session.scalar(
            select(WorkerTransitionModel).where(
                WorkerTransitionModel.worker_item_id == seed["worker_item"].id
            )
        )
        assert transition is not None
        assert transition.action == "continue"
        assert transition.status == "converged"
        assert transition.target_execution_id == target_execution_id
