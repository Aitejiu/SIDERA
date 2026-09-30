from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.domain.artifacts import (
    Artifact,
    ArtifactCandidate,
    ArtifactType,
    EvidenceClaim,
)
from sidera_control_plane.domain.execution import ExecutionSession
from sidera_control_plane.domain.gateway import (
    ExecutionEvent,
    GatewayOperation,
    GatewayOperationType,
)
from sidera_control_plane.domain.inputs import ExecutionInput
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    CapabilityProfile,
    ExecutionBinding,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
)
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    ExecutionModel,
    TaskModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _capabilities() -> CapabilityProfile:
    return CapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.EXPERIMENTAL,
    )


def _seed_execution(database: Database, workspace_id: UUID) -> dict[str, UUID]:
    task_id = uuid4()
    worker_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    with database.session() as session:
        session.add(
            TaskModel(
                id=task_id,
                workspace_id=workspace_id,
                title="Gateway persistence",
                description="Round-trip gateway state",
                acceptance_criteria=["records round-trip"],
                status="running",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        session.add(
            WorkerModel(
                id=worker_id,
                workspace_id=workspace_id,
                adapter_type="fake",
                display_name="Fake Gateway Worker",
                status="busy",
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
                purpose="Persist gateway state",
                status="running",
                capability_requirements=["python"],
                pinned_worker_id=None,
                assigned_worker_id=worker_id,
                active_lease_id=None,
                last_fencing_token=1,
                not_before=NOW,
                failure_code=None,
                started_at=NOW,
                finished_at=None,
                version=1,
            )
        )
        session.commit()
    with database.session() as session:
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
                assignment_reason={"source": "integration"},
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
        "worker_id": worker_id,
        "execution_id": execution_id,
        "lease_id": lease_id,
    }


def _make_records(
    workspace_id: UUID, ids: dict[str, UUID]
) -> tuple[
    ModuleDefinition,
    ModuleInstance,
    ExecutionBinding,
    ExecutionSession,
    ExecutionWorkspaceManifest,
    GatewayOperation,
    ExecutionEvent,
    Artifact,
    EvidenceClaim,
    ExecutionInput,
]:
    capabilities = _capabilities()
    definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="worker_adapter",
        provider="fake",
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=capabilities,
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=definition.id,
        instance_name="fake-default",
        config_version=1,
        config_namespace="fake",
        config={"fake.executable": "fake"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    binding = ExecutionBinding(
        id=uuid4(),
        workspace_id=workspace_id,
        execution_id=ids["execution_id"],
        worker_id=ids["worker_id"],
        adapter_module_id=instance.id,
        workspace_module_id=instance.id,
        adapter_config_version=1,
        workspace_config_version=1,
        capability_snapshot=capabilities,
        lease_id=ids["lease_id"],
        fencing_token=1,
        version=1,
    )
    session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=workspace_id,
        execution_id=ids["execution_id"],
        worker_id=ids["worker_id"],
        adapter_session_id="fake-session-1",
        now=NOW,
        adapter_module_id=instance.id,
        adapter_config_version=1,
        workspace_module_id=instance.id,
        workspace_config_version=1,
        capability_snapshot=capabilities,
        process_ref="process://fake/1",
        workspace_ref="git-worktree://execution/1",
    )
    manifest = ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=workspace_id,
        task_id=ids["task_id"],
        execution_id=ids["execution_id"],
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision="a" * 40,
                branch="sidera/execution/1",
                current_head="a" * 40,
            ),
        ),
        location_ref="git-worktree://execution/1",
        retention_seconds=300,
        fencing_token=1,
        created_at=NOW,
    )
    operation = GatewayOperation.create(
        id=uuid4(),
        operation_id=uuid4(),
        workspace_id=workspace_id,
        task_id=ids["task_id"],
        execution_id=ids["execution_id"],
        session_id=session.id,
        operation_type=GatewayOperationType.START_SESSION,
        module_instance_id=instance.id,
        config_version=1,
        lease_id=ids["lease_id"],
        fencing_token=1,
        now=NOW,
    )
    event = ExecutionEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        execution_id=ids["execution_id"],
        session_id=session.id,
        sequence=1,
        event_type="session.started",
        payload={"adapter": "fake"},
        occurred_at=NOW,
        raw_ref="raw://event/1",
    )
    artifact = Artifact.from_candidate(
        id=uuid4(),
        workspace_id=workspace_id,
        task_id=ids["task_id"],
        execution_id=ids["execution_id"],
        session_id=session.id,
        candidate=ArtifactCandidate(
            artifact_type=ArtifactType.REPORT,
            storage_ref="artifact://report/1",
            content_hash="b" * 64,
            size_bytes=12,
            media_type="text/plain",
            provenance={"producer": "fake"},
        ),
    )
    claim = EvidenceClaim.create_candidate(
        id=uuid4(),
        workspace_id=workspace_id,
        task_id=ids["task_id"],
        execution_id=ids["execution_id"],
        acceptance_criterion_ref="records round-trip",
        conclusion="The gateway records round-trip",
        environment_ref="env://integration",
        artifact_ids=(artifact.id,),
        producer="fake",
        created_at=NOW,
    )
    item = ExecutionInput.accept(
        id=uuid4(),
        workspace_id=workspace_id,
        execution_id=ids["execution_id"],
        session_id=session.id,
        client_idempotency_key="input-1",
        text="continue",
        attachment_refs=("artifact://context/1",),
        now=NOW,
    )
    return (
        definition,
        instance,
        binding,
        session,
        manifest,
        operation,
        event,
        artifact,
        claim,
        item,
    )


def test_r3_gateway_records_round_trip_through_unit_of_work(
    database: Database,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
) -> None:
    ids = _seed_execution(database, workspace_id)
    records = _make_records(workspace_id, ids)
    (
        definition,
        instance,
        binding,
        session,
        manifest,
        operation,
        event,
        artifact,
        claim,
        item,
    ) = records
    events = (
        event,
        replace(event, id=uuid4(), sequence=2, raw_ref="raw://event/2"),
        replace(event, id=uuid4(), sequence=3, raw_ref="raw://event/3"),
    )

    with uow_factory() as uow:
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
    with uow_factory() as uow:
        uow.bindings.add(binding)
        uow.sessions.add(session)
        uow.workspaces.add(manifest)
    with uow_factory() as uow:
        uow.gateway_operations.add(operation)
        for item in events:
            uow.execution_events.add(item)
        uow.artifacts.add(artifact)
        uow.evidence_claims.add(claim)
        uow.execution_inputs.add(item)

    with uow_factory() as uow:
        assert uow.modules.get_definition(definition.id, workspace_id) == definition
        assert uow.modules.get_instance(instance.id, workspace_id) == instance
        assert uow.bindings.get_for_execution(ids["execution_id"], workspace_id) == binding
        assert uow.sessions.get_active_for_execution(ids["execution_id"]) == session
        assert uow.workspaces.get_for_execution(ids["execution_id"], workspace_id) == manifest
        assert uow.gateway_operations.get_by_operation_id(
            operation.operation_id, workspace_id
        ) == operation
        assert uow.gateway_operations.list_for_execution(
            ids["execution_id"], workspace_id
        ) == (operation,)
        assert uow.execution_events.get(event.session_id, event.sequence, workspace_id) == event
        assert uow.execution_events.list_for_session(
            event.session_id, workspace_id
        ) == events
        assert uow.artifacts.list_for_execution(ids["execution_id"], workspace_id) == (artifact,)
        assert uow.evidence_claims.list_for_task(ids["task_id"], workspace_id) == (claim,)
        assert uow.execution_inputs.get(item.id, workspace_id) == item
        assert uow.execution_inputs.get_pending_for_session(
            session.id, workspace_id
        ) == item


def test_r3_gateway_versioned_records_can_be_saved_and_reloaded(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_execution(database, workspace_id)
    records = _make_records(workspace_id, ids)
    operation = records[5]
    item = records[9]

    with uow_factory() as uow:
        uow.modules.add_definition(records[0])
        uow.modules.add_instance(records[1])
    with uow_factory() as uow:
        uow.sessions.add(records[3])
    with uow_factory() as uow:
        uow.gateway_operations.add(operation)
        uow.execution_inputs.add(item)

    operation_previous_version = operation.version
    operation.start(now=NOW)
    item.queue()
    with uow_factory() as uow:
        uow.gateway_operations.save(operation, operation_previous_version)
        uow.execution_inputs.save(item, 1)

    with uow_factory() as uow:
        stored_operation = uow.gateway_operations.get_by_operation_id(
            operation.operation_id, workspace_id
        )
        stored_item = uow.execution_inputs.get(item.id, workspace_id)

    assert stored_operation is not None
    assert stored_operation.status is operation.status
    assert stored_operation.attempt_count == 1
    assert stored_item is not None
    assert stored_item.status is item.status
    assert stored_item.version == 2
