import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.application import worker_gateway as worker_gateway_module
from sidera_control_plane.application.code_result_capture import (
    code_capture_operation_id,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.event_ingestor import ArtifactRegistry
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.input_dispatcher import InputDispatcher
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.reconciler import Reconciler
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    STOP_TOPIC,
    AssignmentLeaseKeeper,
    GatewayMessageError,
    GatewayRecoveryRequired,
    GatewayStopResult,
    WorkerGateway,
    _operation_id,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.context_patches import (
    ContextPatchMaterializationV1,
    ContextPatchV1,
)
from sidera_control_plane.domain.errors import InvalidTransition
from sidera_control_plane.domain.gateway import (
    AdapterSessionHandle,
    DeliveryOutcome,
    DeliveryResult,
    GatewayOperationType,
    context_patch_operation_id,
)
from sidera_control_plane.domain.inputs import ExecutionInput, input_operation_id
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    CapabilityProfile,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
)
from sidera_control_plane.domain.workspace import (
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    EventModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    GatewayOperationModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    TaskModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _capabilities() -> CapabilityProfile:
    return CapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_patch_versions=(1,),
    )


class RecordingFakeAdapter(FakeWorkerAdapter):
    def __init__(
        self,
        *,
        artifacts: tuple[ArtifactCandidate, ...] = (),
        turn_completion_enabled: bool = False,
        observation_order: list[str] | None = None,
    ) -> None:
        super().__init__(
            artifacts=artifacts,
            turn_completion_enabled=turn_completion_enabled,
        )
        self.start_count = 0
        self.input_count = 0
        self.stop_count = 0
        self.stop_results: list[DeliveryResult] = []
        self.observation_order = observation_order

    def read_events(self, handle: AdapterSessionHandle):
        if self.observation_order is not None:
            self.observation_order.append("observe")
        return super().read_events(handle)

    def start_session(
        self,
        spec: ExecutionSpec,
        workspace: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> AdapterSessionHandle:
        self.start_count += 1
        return super().start_session(spec, workspace, operation_id=operation_id)

    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        self.input_count += 1
        return super().send_input(
            handle, text, attachment_refs, operation_id=operation_id
        )

    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        self.stop_count += 1
        result = super().stop_session(handle, operation_id=operation_id)
        self.stop_results.append(result)
        return result


class UnknownInputAdapter(RecordingFakeAdapter):
    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        del handle, text, attachment_refs
        self.input_count += 1
        return DeliveryResult(
            operation_id=operation_id,
            outcome=DeliveryOutcome.UNKNOWN,
            accepted_sequence=None,
            failure_code="acp_response_timeout",
        )


class UnknownStopAdapter(RecordingFakeAdapter):
    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        del handle
        self.stop_count += 1
        return DeliveryResult(
            operation_id=operation_id,
            outcome=DeliveryOutcome.UNKNOWN,
            accepted_sequence=None,
            failure_code="process_stop_unconfirmed",
        )


class FakeWorkspaceProvider:
    def __init__(self) -> None:
        self.prepare_count = 0

    def prepare(self, spec, *, operation_id):
        self.prepare_count += 1
        return ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=spec.workspace_id,
            task_id=spec.task_id,
            execution_id=spec.execution_id,
            checkouts=(
                WorkspaceCheckout(
                    repository_ref="repo://sidera",
                    base_revision="a" * 40,
                    branch=f"sidera/execution/{spec.execution_id}",
                    current_head="a" * 40,
                ),
            ),
            location_ref=f"fake-workspace://{spec.execution_id}",
            retention_seconds=3600,
            fencing_token=spec.fencing_token,
            created_at=NOW,
        )

    def freeze(self, manifest, *, operation_id):
        return manifest

    def retain(self, manifest, *, operation_id):
        return manifest

    def release(self, manifest, *, operation_id):
        return manifest

    def inspect(self, location_ref):
        raise RuntimeError(f"not implemented for {location_ref}")


def _seed_assignment(
    database: Database,
    workspace_id: UUID,
    *,
    name_suffix: str = "",
    completion_policy: str = "explicit",
    turn_completion_versions: tuple[int, ...] = (),
) -> dict[str, UUID]:
    task_id = uuid4()
    worker_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    adapter_definition_id = uuid4()
    workspace_definition_id = uuid4()
    adapter_instance_id = uuid4()
    workspace_instance_id = uuid4()
    capabilities = replace(
        _capabilities(),
        turn_completion_versions=turn_completion_versions,
    )
    with database.session() as session:
        session.add(
            TaskModel(
                id=task_id,
                workspace_id=workspace_id,
                title="Gateway execution",
                description="Run the assigned execution",
                acceptance_criteria=["session starts"],
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
                    provider="fake",
                    interface_version="sidera.worker-adapter.v1",
                    config_schema_version=1,
                    capabilities={
                        "input": capabilities.input.value,
                        "idempotency": capabilities.idempotency.value,
                        "resume": capabilities.resume.value,
                        "stop": capabilities.stop.value,
                        "sandbox": capabilities.sandbox.value,
                        "stability": capabilities.stability.value,
                        "context_patch_versions": list(
                            capabilities.context_patch_versions
                        ),
                        "turn_completion_versions": list(
                            capabilities.turn_completion_versions
                        ),
                    },
                    lifecycle="active",
                ),
                ModuleDefinitionModel(
                    id=workspace_definition_id,
                    workspace_id=workspace_id,
                    module_type="workspace_provider",
                    provider="fake-workspace",
                    interface_version="sidera.workspace-provider.v1",
                    config_schema_version=1,
                    capabilities={
                        "input": capabilities.input.value,
                        "idempotency": capabilities.idempotency.value,
                        "resume": capabilities.resume.value,
                        "stop": capabilities.stop.value,
                        "sandbox": capabilities.sandbox.value,
                        "stability": capabilities.stability.value,
                        "context_patch_versions": list(
                            capabilities.context_patch_versions
                        ),
                    },
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=adapter_instance_id,
                    workspace_id=workspace_id,
                    definition_id=adapter_definition_id,
                    instance_name=f"fake-adapter{name_suffix}",
                    config_version=1,
                    config_namespace="fake",
                    config={
                        "fake.name": "integration",
                        "fake.completion_policy": completion_policy,
                    },
                    secret_refs=[],
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=workspace_instance_id,
                    workspace_id=workspace_id,
                    definition_id=workspace_definition_id,
                    instance_name=f"fake-workspace{name_suffix}",
                    config_version=1,
                    config_namespace="workspace",
                    config={
                        "workspace.repository_ref": "repo://sidera",
                        "workspace.base_revision": "a" * 40,
                    },
                    secret_refs=[],
                    lifecycle="active",
                ),
            ]
        )
        session.commit()
    with database.session() as session:
        execution = ExecutionModel(
            id=execution_id,
            workspace_id=workspace_id,
            task_id=task_id,
            lineage_id=execution_id,
            attempt=1,
            max_attempts=2,
            purpose="Start Gateway",
            status="leased",
            capability_requirements=["python"],
            adapter_preferences=["opencode", "codex"],
            adapter_forbidden=["unsafe"],
            pinned_worker_id=None,
            assigned_worker_id=worker_id,
            active_lease_id=None,
            last_fencing_token=1,
            not_before=NOW,
            failure_code=None,
            started_at=None,
            finished_at=None,
            version=2,
        )
        session.add(execution)
        session.flush([execution])
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
                capability_snapshot={
                    "input": capabilities.input.value,
                    "idempotency": capabilities.idempotency.value,
                    "resume": capabilities.resume.value,
                    "stop": capabilities.stop.value,
                    "sandbox": capabilities.sandbox.value,
                    "stability": capabilities.stability.value,
                    "context_patch_versions": list(
                        capabilities.context_patch_versions
                    ),
                    "turn_completion_versions": list(
                        capabilities.turn_completion_versions
                    ),
                },
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
        "worker_id": worker_id,
        "execution_id": execution_id,
        "lease_id": lease_id,
        "adapter_instance_id": adapter_instance_id,
        "workspace_instance_id": workspace_instance_id,
    }


def _message(workspace_id: UUID, execution_id: UUID, lease_id: UUID) -> OutboxMessage:
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


def _add_input_model(
    database: Database,
    workspace_id: UUID,
    execution_id: UUID,
    session_id: UUID,
    *,
    client_key: str,
    text: str,
    status: str = "queued",
    version: int = 1,
    operation_id: UUID | None = None,
    failure_code: str | None = None,
) -> UUID:
    input_id = uuid4()
    source_command_id = uuid4()
    with database.session() as session:
        session.add(
            CommandModel(
                id=source_command_id,
                workspace_id=workspace_id,
                command_type="SendExecutionInput",
                actor_type="user",
                actor_id="integration",
                target_type="execution_session",
                target_id=session_id,
                payload={"source": "integration-helper"},
                expected_version=1,
                idempotency_key=f"input-helper:{source_command_id}",
                request_hash="2" * 64,
                correlation_id=uuid4(),
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        session.add(
            ExecutionInputModel(
                id=input_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                session_id=session_id,
                client_idempotency_key=client_key,
                text=text,
                attachment_refs=[],
                status=status,
                operation_id=operation_id,
                failure_code=failure_code,
                version=version,
                source_command_id=source_command_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    return input_id


def _patch_materialization(
    workspace_id: UUID, worker_transition_id: UUID
) -> ContextPatchMaterializationV1:
    now = datetime.now(UTC).replace(microsecond=0)
    return ContextPatchMaterializationV1(
        schema_version=1,
        patch=ContextPatchV1.create(
            id=uuid4(),
            workspace_id=workspace_id,
            root_task_id=uuid4(),
            worker_item_id=uuid4(),
            plan_transition_id=uuid4(),
            worker_transition_id=worker_transition_id,
            base_plan_snapshot_id=uuid4(),
            base_generation=1,
            target_plan_snapshot_id=uuid4(),
            target_generation=2,
            base_context_package_id=uuid4(),
            base_package_hash="a" * 64,
            base_work_context_revision_id=uuid4(),
            base_context_hash="b" * 64,
            target_context_version=2,
            changed_requirements=("updated requirement",),
            changed_acceptance_refs=("acceptance:updated",),
            changed_dependency_refs=(),
            decision_refs=(),
            artifact_refs=(),
            evidence_refs=(),
            intervention_refs=(),
            unfinished_work="Continue from the checkpoint.",
            checkpoint_ref="checkpoint://integration/1",
            proposer="integration",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        ),
    )


def _stop_service(database: Database) -> CommandService:
    return CommandService(
        lambda: SqlAlchemyUnitOfWork(database),
        (ExecutionCommandHandler(lambda: NOW, lease_ttl_seconds=300),),
    )


def _request_stop(
    database: Database, workspace_id: UUID, execution_id: UUID, expected_version: int
) -> tuple[UUID, OutboxMessage]:
    command_id = uuid4()
    _stop_service(database).handle(
        CommandEnvelope(
            id=command_id,
            workspace_id=workspace_id,
            command_type="StopExecution",
            target_type="execution",
            actor_type="user",
            actor_id="user:integration",
            target_id=execution_id,
            payload={},
            expected_version=expected_version,
            idempotency_key=f"stop:{execution_id}:{command_id}",
            correlation_id=uuid4(),
        )
    )
    with database.session() as session:
        row = session.query(OutboxMessageModel).filter_by(topic=STOP_TOPIC).one()
        return command_id, OutboxMessage(
            id=row.id,
            event_id=row.event_id,
            topic=row.topic,
            payload=dict(row.payload),
            attempts=1,
            claim_token=uuid4(),
        )


def _request_continue(
    database: Database,
    workspace_id: UUID,
    session_id: UUID,
    expected_version: int,
) -> UUID:
    command_id = uuid4()
    _stop_service(database).handle(
        CommandEnvelope(
            id=command_id,
            workspace_id=workspace_id,
            command_type="ContinueExecutionSession",
            target_type="execution_session",
            actor_type="user",
            actor_id="user:integration",
            target_id=session_id,
            payload={"not_before": NOW.isoformat()},
            expected_version=expected_version,
            idempotency_key=f"continue:{session_id}:{command_id}",
            correlation_id=uuid4(),
        )
    )
    return command_id


def test_gateway_starts_once_and_replays_duplicate_assignment(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )
    message = _message(workspace_id, ids["execution_id"], ids["lease_id"])

    first = gateway.handle(message)
    second = gateway.handle(message)

    assert first == second
    assert adapter.start_count == 1
    assert adapter.input_count == 1
    assert provider.prepare_count == 1
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        assert execution is not None
        assert execution.status == "running"
        stored_session = session.query(ExecutionSessionModel).one()
        assert stored_session.last_sequence == 2
        assert session.query(ExecutionEventModel).count() == 2
        assert session.query(GatewayOperationModel).count() == 3
        assert session.query(GatewayOperationModel).filter_by(status="succeeded").count() == 3


def test_gateway_rejects_unsupported_completion_contract_before_external_calls(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(
        database,
        workspace_id,
        completion_policy="initial_turn",
        turn_completion_versions=(),
    )
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    with pytest.raises(GatewayMessageError) as caught:
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert caught.value.code == "turn_completion_capability_unsupported"
    assert adapter.start_count == 0
    assert provider.prepare_count == 0


def test_gateway_rejects_invalid_completion_policy_before_external_calls(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(
        database,
        workspace_id,
        completion_policy="guess",
        turn_completion_versions=(1,),
    )
    adapter = RecordingFakeAdapter(turn_completion_enabled=True)
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    with pytest.raises(GatewayMessageError) as caught:
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert caught.value.code == "completion_policy_invalid"
    assert adapter.start_count == 0
    assert provider.prepare_count == 0


def test_gateway_completes_initial_turn_once_and_captures_before_artifacts(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(
        database,
        workspace_id,
        completion_policy="initial_turn",
        turn_completion_versions=(1,),
    )
    adapter = RecordingFakeAdapter(turn_completion_enabled=True)
    provider = FakeWorkspaceProvider()
    order: list[str] = []

    class RecordingCompletionCapture:
        def record_execution_start(self, **kwargs):
            del kwargs
            order.append("result_capture_start")
            return object()

        def observe_terminal(self, **kwargs):
            del kwargs
            order.append("result_capture")
            return object()

        def capture_execution(self, **kwargs):
            del kwargs
            order.append("result_capture_snapshot")
            with SqlAlchemyUnitOfWork(database) as uow:
                execution = uow.executions.get(
                    ids["execution_id"], workspace_id, for_update=True
                )
                assert execution is not None
                previous_version = execution.version
                execution.version += 1
                uow.executions.save(execution, previous_version)
            return object()

    class RecordingArtifacts:
        def register(self, **kwargs):
            del kwargs
            order.append("artifact_register")
            return worker_gateway_module.ArtifactRegistrationReport((), ())

    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        artifact_registry=cast(ArtifactRegistry, RecordingArtifacts()),
        code_result_capture_service=cast(object, RecordingCompletionCapture()),
        clock=lambda: NOW,
    )
    original_report = gateway._report_execution_completion

    def report_with_order(*args, **kwargs):
        order.append("completion_command")
        return original_report(*args, **kwargs)

    gateway._report_execution_completion = report_with_order  # type: ignore[method-assign]
    started = gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))
    gateway.sync_session(
        workspace_id=workspace_id,
        execution_id=ids["execution_id"],
        session_id=started.session_id,
        lease_id=ids["lease_id"],
        fencing_token=1,
    )

    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        worker = session.get(WorkerModel, ids["worker_id"])
        stored_session = session.get(ExecutionSessionModel, started.session_id)
        events = (
            session.query(ExecutionEventModel)
            .filter_by(session_id=started.session_id)
            .order_by(ExecutionEventModel.sequence.asc())
            .all()
        )
        command = session.query(CommandModel).filter_by(
            command_type="ReportExecutionSucceeded"
        ).one()
        assert execution is not None
        assert lease is not None
        assert worker is not None
        assert stored_session is not None
        assert execution.status == "succeeded"
        assert execution.active_lease_id is None
        assert execution.finished_at is not None
        assert lease.status == "released"
        assert worker.current_load == 0
        assert stored_session.status == "closed"
        assert events[-1].event_type == "turn.completed"
        assert command.command_type == "ReportExecutionSucceeded"

    assert order.index("result_capture") < order.index("artifact_register")
    assert order.index("artifact_register") < order.index("completion_command")
    assert order.count("completion_command") == 1


@pytest.mark.parametrize("code_capture_enabled", (True, False))
def test_gateway_delivers_input_accepted_before_first_sync_before_completing(
    database: Database,
    workspace_id: UUID,
    code_capture_enabled: bool,
) -> None:
    ids = _seed_assignment(
        database,
        workspace_id,
        completion_policy="initial_turn",
        turn_completion_versions=(1,),
    )

    artifact_collection_calls: list[UUID] = []

    class AcceptInputBeforeFirstSyncAdapter(RecordingFakeAdapter):
        def send_input(
            self,
            handle: AdapterSessionHandle,
            text: str,
            attachment_refs: tuple[str, ...],
            *,
            operation_id: UUID,
        ) -> DeliveryResult:
            result = super().send_input(
                handle, text, attachment_refs, operation_id=operation_id
            )
            if self.input_count == 1:
                command_id = uuid4()
                with database.session() as session:
                    session.add(
                        CommandModel(
                            id=command_id,
                            workspace_id=workspace_id,
                            command_type="SendExecutionInput",
                            actor_type="user",
                            actor_id="integration",
                            target_type="execution_session",
                            target_id=handle.execution_session_id,
                            payload={"source": "integration-race"},
                            expected_version=1,
                            idempotency_key=f"input-race:{command_id}",
                            request_hash="1" * 64,
                            correlation_id=uuid4(),
                            status="applied",
                            result={},
                            rejection_reason=None,
                        )
                    )
                    session.add(
                        ExecutionInputModel(
                            id=uuid4(),
                            workspace_id=workspace_id,
                            execution_id=ids["execution_id"],
                            session_id=handle.execution_session_id,
                            client_idempotency_key="input-before-first-sync",
                            text="Please include the missing edge case",
                            attachment_refs=[],
                            status="queued",
                            operation_id=None,
                            failure_code=None,
                            version=1,
                            source_command_id=command_id,
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )
                    session.commit()
            return result

        def collect_artifacts(
            self,
            handle: AdapterSessionHandle,
            *,
            operation_id: UUID,
        ):
            artifact_collection_calls.append(operation_id)
            if not code_capture_enabled:
                with SqlAlchemyUnitOfWork(database) as uow:
                    fence = uow.results.get_operation(
                        code_capture_operation_id(ids["execution_id"]), workspace_id
                    )
                assert fence is not None
                assert fence.payload.get("terminal_observed") is True
            return super().collect_artifacts(handle, operation_id=operation_id)

    capture_calls: list[str] = []

    class RecordingCompletionCapture:
        def record_execution_start(self, **_kwargs):
            capture_calls.append("start")

        def observe_terminal(self, **_kwargs):
            with database.session() as session:
                pending_count = session.query(ExecutionInputModel).filter(
                    ExecutionInputModel.status.in_(
                        ("accepted", "queued", "dispatching", "blocked")
                    )
                ).count()
            assert pending_count == 0
            capture_calls.append("observe")

        def capture_execution(self, **_kwargs):
            capture_calls.append("capture")
            return object()

    adapter = AcceptInputBeforeFirstSyncAdapter(turn_completion_enabled=True)
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        code_result_capture_service=(
            cast(object, RecordingCompletionCapture())
            if code_capture_enabled
            else None
        ),
        clock=lambda: NOW,
    )

    started = gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )

    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        stored_session = session.get(ExecutionSessionModel, started.session_id)
        queued_input = session.query(ExecutionInputModel).one()
        completion_commands = session.query(CommandModel).filter(
            CommandModel.command_type.in_(
                ("ReportExecutionSucceeded", "ReportExecutionFailed")
            )
        )
        assert execution is not None
        assert lease is not None
        assert stored_session is not None
        assert execution.status == "running"
        assert lease.status == "active"
        assert stored_session.status == "active"
        assert queued_input.status == "queued"
        assert completion_commands.count() == 0
    assert capture_calls == (["start"] if code_capture_enabled else [])
    assert artifact_collection_calls == []

    adapter.release_boundary(adapter.active_sessions()[0])
    dispatcher = InputDispatcher(
        lambda: SqlAlchemyUnitOfWork(database), gateway, clock=lambda: NOW
    )
    delivery = dispatcher.run_once()

    assert delivery.claimed == 1
    assert delivery.delivered == 1
    assert gateway.sync_active_sessions(workspace_id) == 1
    assert capture_calls == (
        ["start", "observe", "capture"] if code_capture_enabled else []
    )
    assert len(artifact_collection_calls) == 1

    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        worker = session.get(WorkerModel, ids["worker_id"])
        stored_session = session.get(ExecutionSessionModel, started.session_id)
        inputs = session.query(ExecutionInputModel).all()
        completion_commands = session.query(CommandModel).filter(
            CommandModel.command_type == "ReportExecutionSucceeded"
        )
        assert execution is not None and execution.status == "succeeded"
        assert lease is not None and lease.status == "released"
        assert worker is not None and worker.current_load == 0
        assert stored_session is not None and stored_session.status == "closed"
        assert len(inputs) == 1 and inputs[0].status == "delivered"
        assert completion_commands.count() == 1
        assert session.query(ExecutionSessionModel).count() == 1
    if not code_capture_enabled:
        with SqlAlchemyUnitOfWork(database) as uow:
            fence = uow.results.get_operation(
                code_capture_operation_id(ids["execution_id"]), workspace_id
            )
        assert fence is not None
        assert fence.operation_kind == "execution_completion_fence"
        assert fence.payload.get("terminal_observed") is True


def test_gateway_persists_completion_fence_before_artifacts_without_code_capture(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(
        database,
        workspace_id,
        completion_policy="initial_turn",
        turn_completion_versions=(1,),
    )

    class FenceCheckingAdapter(RecordingFakeAdapter):
        def collect_artifacts(
            self,
            handle: AdapterSessionHandle,
            *,
            operation_id: UUID,
        ):
            with SqlAlchemyUnitOfWork(database) as uow:
                fence = uow.results.get_operation(
                    code_capture_operation_id(ids["execution_id"]), workspace_id
                )
            assert fence is not None
            assert fence.payload.get("terminal_observed") is True
            assert fence.payload.get("terminal_outcome") == "succeeded"
            return super().collect_artifacts(handle, operation_id=operation_id)

    adapter = FenceCheckingAdapter(turn_completion_enabled=True)
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={
            "fake-workspace": lambda _instance: FakeWorkspaceProvider()
        },
        clock=lambda: NOW,
    )

    gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    with SqlAlchemyUnitOfWork(database) as uow:
        fence = uow.results.get_operation(
            code_capture_operation_id(ids["execution_id"]), workspace_id
        )
        execution = uow.executions.get(ids["execution_id"], workspace_id)
        assert fence is not None
        assert fence.operation_kind == "execution_completion_fence"
        assert fence.payload.get("terminal_observed") is True
        assert execution is not None and execution.status.value == "succeeded"


@pytest.mark.parametrize("succeeded", (True, False))
@pytest.mark.parametrize("settlement", ("delivered", "cancelled"))
def test_report_completion_defers_without_recording_rejection_while_input_pending(
    database: Database,
    workspace_id: UUID,
    succeeded: bool,
    settlement: str,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )
    started = gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    input_id = uuid4()
    with database.session() as session:
        session.add(
            ExecutionInputModel(
                id=input_id,
                workspace_id=workspace_id,
                execution_id=ids["execution_id"],
                session_id=started.session_id,
                client_idempotency_key="completion-waits-for-input",
                text="Wait for this accepted input",
                attachment_refs=[],
                status="queued",
                operation_id=None,
                failure_code=None,
                version=1,
                source_command_id=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    with SqlAlchemyUnitOfWork(database) as uow:
        execution = uow.executions.get(ids["execution_id"], workspace_id)
        assert execution is not None
        payload = {
            "worker_id": str(ids["worker_id"]),
            "lease_id": str(ids["lease_id"]),
            "fencing_token": 1,
        }
        if not succeeded:
            payload["failure_code"] = "worker_session_failed"
        completion = CommandEnvelope(
            id=uuid4(),
            workspace_id=workspace_id,
            command_type=(
                "ReportExecutionSucceeded"
                if succeeded
                else "ReportExecutionFailed"
            ),
            target_type="execution",
            actor_type="worker",
            actor_id=str(ids["worker_id"]),
            target_id=ids["execution_id"],
            payload=payload,
            expected_version=execution.version,
            idempotency_key=f"completion-deferred:{ids['execution_id']}",
            correlation_id=ids["task_id"],
        )
    command_service = CommandService(
        lambda: SqlAlchemyUnitOfWork(database),
        (ExecutionCommandHandler(lambda: NOW),),
    )

    with pytest.raises(RuntimeError, match="execution completion deferred"):
        command_service.handle(completion)

    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        stored_session = session.get(ExecutionSessionModel, started.session_id)
        assert execution is not None and execution.status == "running"
        assert stored_session is not None and stored_session.status == "active"
        assert session.get(CommandModel, completion.id) is None

        pending = session.get(ExecutionInputModel, input_id)
        assert pending is not None
        pending.status = settlement
        session.commit()

    command_service.handle(completion)

    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        command = session.get(CommandModel, completion.id)
        expected_status = "succeeded" if succeeded else "failed"
        assert execution is not None and execution.status == expected_status
        assert command is not None and command.status == "applied"


def test_blocked_input_stops_active_session_lease_renewal(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(
        database,
        workspace_id,
        completion_policy="initial_turn",
        turn_completion_versions=(1,),
    )

    class BlockedInputBeforeFirstSyncAdapter(RecordingFakeAdapter):
        def send_input(
            self,
            handle: AdapterSessionHandle,
            text: str,
            attachment_refs: tuple[str, ...],
            *,
            operation_id: UUID,
        ) -> DeliveryResult:
            result = super().send_input(
                handle, text, attachment_refs, operation_id=operation_id
            )
            if self.input_count == 1:
                _add_input_model(
                    database,
                    workspace_id,
                    ids["execution_id"],
                    handle.execution_session_id,
                    client_key="blocked-before-first-sync",
                    text="Input delivery outcome is unknown",
                    status="blocked",
                    version=4,
                    operation_id=uuid4(),
                    failure_code="prompt_result_unknown",
                )
            return result

    adapter = BlockedInputBeforeFirstSyncAdapter(turn_completion_enabled=True)
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )
    started = gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    with database.session() as session:
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        assert lease is not None
        version_before_sync = lease.version

    assert gateway.sync_active_sessions(workspace_id) == 1
    assert gateway.sync_active_sessions(workspace_id) == 1

    with database.session() as session:
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        execution = session.get(ExecutionModel, ids["execution_id"])
        stored_session = session.get(ExecutionSessionModel, started.session_id)
        pending = session.query(ExecutionInputModel).one()
        assert lease is not None and lease.version == version_before_sync
        assert execution is not None and execution.status == "running"
        assert stored_session is not None and stored_session.status == "active"
        assert pending.status == "blocked"


def test_send_input_renews_lease_during_slow_adapter_prompt(
    database: Database,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    prompt_entered = Event()
    release_prompt = Event()
    renewal_observed = Event()

    class SlowSecondPromptAdapter(RecordingFakeAdapter):
        block_next_prompt = False

        def send_input(
            self,
            handle: AdapterSessionHandle,
            text: str,
            attachment_refs: tuple[str, ...],
            *,
            operation_id: UUID,
        ) -> DeliveryResult:
            if self.block_next_prompt:
                self.block_next_prompt = False
                prompt_entered.set()
                if not release_prompt.wait(timeout=4):
                    raise TimeoutError("test prompt was not released")
            return super().send_input(
                handle, text, attachment_refs, operation_id=operation_id
            )

    adapter = SlowSecondPromptAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        assignment_lease_ttl_seconds=300,
        assignment_lease_renew_interval_seconds=0.02,
        clock=lambda: NOW,
    )
    started = gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    adapter.release_boundary(adapter.active_sessions()[0])
    adapter.block_next_prompt = True
    input_id = _add_input_model(
        database,
        workspace_id,
        ids["execution_id"],
        started.session_id,
        client_key="slow-prompt-renewal",
        text="Hold this prompt while the lease renews",
    )
    with database.session() as session:
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        assert lease is not None
        version_before_prompt = lease.version

    original_renew_once = AssignmentLeaseKeeper.renew_once

    def observe_renewal(keeper: AssignmentLeaseKeeper) -> bool:
        result = original_renew_once(keeper)
        if result:
            renewal_observed.set()
        return result

    monkeypatch.setattr(AssignmentLeaseKeeper, "renew_once", observe_renewal)
    dispatcher = InputDispatcher(
        lambda: SqlAlchemyUnitOfWork(database), gateway, clock=lambda: NOW
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(dispatcher.run_once)
        prompt_started = prompt_entered.wait(timeout=2)
        try:
            renewed = renewal_observed.wait(timeout=1)
        finally:
            release_prompt.set()
        report = future.result(timeout=4)

    assert prompt_started
    assert renewed
    assert report.delivered == 1
    with database.session() as session:
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        stored_input = session.get(ExecutionInputModel, input_id)
        assert lease is not None and lease.version > version_before_prompt
        assert lease.renewed_at == NOW
        assert stored_input is not None and stored_input.status == "delivered"


def test_input_becomes_unknown_when_lease_renewal_fails_during_prompt(
    database: Database,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    prompt_entered = Event()
    release_prompt = Event()
    renewal_failed = Event()

    class SlowSecondPromptAdapter(RecordingFakeAdapter):
        block_next_prompt = False

        def send_input(
            self,
            handle: AdapterSessionHandle,
            text: str,
            attachment_refs: tuple[str, ...],
            *,
            operation_id: UUID,
        ) -> DeliveryResult:
            if self.block_next_prompt:
                self.block_next_prompt = False
                prompt_entered.set()
                if not release_prompt.wait(timeout=4):
                    raise TimeoutError("test prompt was not released")
            return super().send_input(
                handle, text, attachment_refs, operation_id=operation_id
            )

    adapter = SlowSecondPromptAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        assignment_lease_ttl_seconds=300,
        assignment_lease_renew_interval_seconds=0.02,
        clock=lambda: NOW,
    )
    started = gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    adapter.release_boundary(adapter.active_sessions()[0])
    adapter.block_next_prompt = True
    input_id = _add_input_model(
        database,
        workspace_id,
        ids["execution_id"],
        started.session_id,
        client_key="renewal-failure-during-prompt",
        text="Do not replay after lease renewal fails",
    )

    def fail_renewal(_keeper: AssignmentLeaseKeeper) -> bool:
        renewal_failed.set()
        raise RuntimeError("injected lease renewal failure")

    monkeypatch.setattr(AssignmentLeaseKeeper, "renew_once", fail_renewal)
    dispatcher = InputDispatcher(
        lambda: SqlAlchemyUnitOfWork(database), gateway, clock=lambda: NOW
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(dispatcher.run_once)
        prompt_started = prompt_entered.wait(timeout=2)
        try:
            failed = renewal_failed.wait(timeout=1)
        finally:
            release_prompt.set()
        report = future.result(timeout=4)

    assert prompt_started
    assert failed
    assert report.unknown == 1
    with database.session() as session:
        stored_input = session.get(ExecutionInputModel, input_id)
        assert stored_input is not None and stored_input.status == "blocked"
        operation = session.query(GatewayOperationModel).filter_by(
            operation_type=GatewayOperationType.SEND_INPUT.value,
            status="unknown",
        ).one()
        assert operation.failure_code == "assignment_lease_renewal_failed"

    sends_before_recheck = adapter.input_count
    retried = dispatcher.run_once()
    assert retried.claimed == 0
    assert adapter.input_count == sends_before_recheck


def test_two_source_turn_completions_release_capacity_before_downstream_activation(
    database: Database,
    workspace_id: UUID,
) -> None:
    source_a = _seed_assignment(
        database,
        workspace_id,
        name_suffix="-source-a",
        completion_policy="initial_turn",
        turn_completion_versions=(1,),
    )
    source_b = _seed_assignment(
        database,
        workspace_id,
        name_suffix="-source-b",
        completion_policy="initial_turn",
        turn_completion_versions=(1,),
    )

    def artifact(execution_id: UUID, name: str) -> ArtifactCandidate:
        body = f"{name}: complete\n".encode()
        return ArtifactCandidate(
            artifact_type=ArtifactType.BUILD,
            storage_ref=f"fake://artifact/{execution_id}/{name}",
            content_hash=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            media_type="text/plain",
            provenance={"source": name},
            content=body,
        )

    adapters = {
        source_a["adapter_instance_id"]: RecordingFakeAdapter(
            turn_completion_enabled=True,
            artifacts=(artifact(source_a["execution_id"], "source-a"),),
        ),
        source_b["adapter_instance_id"]: RecordingFakeAdapter(
            turn_completion_enabled=True,
            artifacts=(artifact(source_b["execution_id"], "source-b"),),
        ),
    }
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda instance: adapters[instance.id]},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    started = {}
    for ids in (source_a, source_b):
        started[ids["execution_id"]] = gateway.handle(
            _message(workspace_id, ids["execution_id"], ids["lease_id"])
        )
    for ids in (source_a, source_b):
        gateway.sync_session(
            workspace_id=workspace_id,
            execution_id=ids["execution_id"],
            session_id=started[ids["execution_id"]].session_id,
            lease_id=ids["lease_id"],
            fencing_token=1,
        )

    with database.session() as session:
        executions = [
            session.get(ExecutionModel, ids["execution_id"])
            for ids in (source_a, source_b)
        ]
        leases = [
            session.get(AssignmentLeaseModel, ids["lease_id"])
            for ids in (source_a, source_b)
        ]
        workers = [
            session.get(WorkerModel, ids["worker_id"])
            for ids in (source_a, source_b)
        ]
        events = tuple(
            session.query(ExecutionEventModel)
            .filter(ExecutionEventModel.execution_id.in_(
                [ids["execution_id"] for ids in (source_a, source_b)]
            ))
            .all()
        )

    assert all(item is not None and item.status == "succeeded" for item in executions)
    assert all(item is not None and item.active_lease_id is None for item in executions)
    assert all(item is not None and item.status == "released" for item in leases)
    assert all(item is not None and item.current_load == 0 for item in workers)
    assert {event.event_type for event in events} >= {
        "input.accepted",
        "turn.completed",
    }
    windows = tuple(
        (item.started_at, item.finished_at)
        for item in executions
        if item is not None
    )
    assert all(start is not None and finish is not None for start, finish in windows)
    assert max(start for start, _finish in windows if start is not None) <= min(
        finish for _start, finish in windows if finish is not None
    )

    # The downstream assignment is created only after both durable source facts
    # exist, matching the input-selection gate used by the result scheduler.
    downstream = _seed_assignment(
        database,
        workspace_id,
        name_suffix="-integration",
        completion_policy="initial_turn",
        turn_completion_versions=(1,),
    )
    with database.session() as session:
        integration = session.get(ExecutionModel, downstream["execution_id"])
        assert integration is not None
        assert integration.status == "leased"


def test_gateway_delivers_patch_as_independent_idempotent_operation(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )
    started = gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))
    adapter.release_boundary(adapter.active_sessions()[0])
    worker_transition_id = uuid4()
    materialization = _patch_materialization(workspace_id, worker_transition_id)
    kwargs = {
        "workspace_id": workspace_id,
        "execution_id": ids["execution_id"],
        "session_id": started.session_id,
        "lease_id": ids["lease_id"],
        "fencing_token": 1,
        "worker_transition_id": worker_transition_id,
        "action_step": "apply_patch",
        "logical_attempt": 1,
        "materialization": materialization,
    }

    first = gateway.apply_context_patch(**kwargs)
    duplicate = gateway.apply_context_patch(**kwargs)

    expected_operation_id = context_patch_operation_id(
        worker_transition_id, "apply_patch", logical_attempt=1
    )
    assert first.outcome == "delivered"
    assert first.operation_id == expected_operation_id
    assert first.patch_hash == materialization.patch.patch_hash
    assert duplicate == first
    assert adapter.applied_context_patches == (materialization,)
    with database.session() as session:
        operation = session.query(GatewayOperationModel).filter_by(
            operation_type=GatewayOperationType.APPLY_CONTEXT_PATCH.value
        ).one()
        assert operation.status == "succeeded"
        assert operation.result_ref == (
            f"patch:{materialization.patch.patch_hash}:sequence:{first.accepted_sequence}"
        )


def test_assignment_lease_keeper_persists_command_for_audited_renewal(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    keeper = AssignmentLeaseKeeper(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        lease_id=ids["lease_id"],
        workspace_id=workspace_id,
        execution_id=ids["execution_id"],
        worker_id=ids["worker_id"],
        fencing_token=1,
        lease_ttl_seconds=300,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    assert keeper.renew_once() is True

    with database.session() as session:
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        assert lease is not None
        assert lease.renewed_at == NOW + timedelta(seconds=1)
        assert lease.version == 2
        renewal_event = session.query(EventModel).filter_by(
            aggregate_type="lease",
            aggregate_id=ids["lease_id"],
            event_type="LeaseRenewed",
        ).one()
        command = session.get(CommandModel, renewal_event.causation_id)
        assert command is not None
        assert command.command_type == "RenewLease"


def test_active_session_sync_renews_lease_after_successful_observation(
    database: Database,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    order: list[str] = []
    adapter = RecordingFakeAdapter(observation_order=order)
    provider = FakeWorkspaceProvider()
    current_time = [NOW]
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: current_time[0],
    )
    gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))
    order.clear()
    with database.session() as session:
        session.execute(
            update(AssignmentLeaseModel)
            .where(AssignmentLeaseModel.id == ids["lease_id"])
            .values(expires_at=NOW + timedelta(seconds=5))
        )
        session.commit()

    current_time[0] = NOW + timedelta(seconds=4)
    original_renew_once = AssignmentLeaseKeeper.renew_once

    def record_renewal(keeper: AssignmentLeaseKeeper) -> bool:
        order.append("renew")
        return original_renew_once(keeper)

    monkeypatch.setattr(AssignmentLeaseKeeper, "renew_once", record_renewal)
    assert gateway.sync_active_demo_sessions(workspace_id) == 1
    assert order.index("observe") < order.index("renew")

    with database.session() as session:
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        assert lease is not None
        assert lease.expires_at == NOW + timedelta(seconds=34)
        assert lease.version == 2


def test_unobservable_same_process_session_is_not_renewed(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    first_adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    first_gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: first_adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )
    started = first_gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    with database.session() as session:
        session.execute(
            update(AssignmentLeaseModel)
            .where(AssignmentLeaseModel.id == ids["lease_id"])
            .values(expires_at=NOW + timedelta(seconds=5))
        )
        session.commit()
        original = session.get(AssignmentLeaseModel, ids["lease_id"])
        assert original is not None
        original_expires_at = original.expires_at
        original_version = original.version

    restarted_gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: RecordingFakeAdapter()},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW + timedelta(seconds=4),
    )

    synced = restarted_gateway.sync_active_sessions(workspace_id)

    assert synced == 0
    with database.session() as session:
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        assert lease is not None
        assert lease.expires_at == original_expires_at
        assert lease.version == original_version
        assert started.session_id is not None

    reconciler = Reconciler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: NOW + timedelta(seconds=6),
        worker_heartbeat_timeout_seconds=30,
    )
    result = reconciler.run_once(limit=10)

    assert result.expired_leases == 1
    with database.session() as session:
        execution_rows = (
            session.query(ExecutionModel)
            .filter_by(lineage_id=ids["execution_id"])
            .order_by(ExecutionModel.attempt.asc())
            .all()
        )
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        worker = session.get(WorkerModel, ids["worker_id"])
        session_row = session.get(ExecutionSessionModel, started.session_id)
        lost_events = session.query(EventModel).filter_by(event_type="ExecutionLost").all()
    assert len(execution_rows) == 2
    assert execution_rows[0].status == "lost"
    assert execution_rows[1].status == "queued"
    assert lease is not None and lease.status == "expired"
    assert worker is not None and worker.current_load == 0
    assert session_row is not None and session_row.status == "lost"
    assert len(lost_events) == 1


def test_gateway_marks_unresolved_running_operation_unknown_without_restart(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    with database.session() as session:
        session.add(
            GatewayOperationModel(
                id=uuid4(),
                workspace_id=workspace_id,
                operation_id=_operation_id(
                    ids["execution_id"], GatewayOperationType.START_SESSION
                ),
                task_id=ids["task_id"],
                execution_id=ids["execution_id"],
                session_id=None,
                operation_type=GatewayOperationType.START_SESSION.value,
                module_instance_id=ids["adapter_instance_id"],
                config_version=1,
                lease_id=ids["lease_id"],
                fencing_token=1,
                status="running",
                attempt_count=1,
                next_attempt_at=None,
                result_ref=None,
                failure_code=None,
                version=2,
            )
        )
        session.commit()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert adapter.start_count == 0
    with database.session() as session:
        operation = session.query(GatewayOperationModel).filter_by(
            operation_type=GatewayOperationType.START_SESSION.value
        ).one()
        assert operation.status == "unknown"


def test_gateway_preserves_unknown_initial_prompt_result(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = UnknownInputAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert adapter.input_count == 1
    with database.session() as session:
        operation = session.query(GatewayOperationModel).filter_by(
            operation_type=GatewayOperationType.SEND_INPUT.value
        ).one()
        assert operation.status == "unknown"
        assert operation.failure_code == "acp_response_timeout"


def test_input_dispatcher_delivers_to_exact_gateway_session(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    result = gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))
    adapter.release_boundary(adapter.active_sessions()[0])
    input_id = uuid4()
    source_command_id = uuid4()
    with database.session() as session:
        session.add(
            CommandModel(
                id=source_command_id,
                workspace_id=workspace_id,
                command_type="SendExecutionInput",
                actor_type="user",
                actor_id="integration",
                target_type="execution_session",
                target_id=result.session_id,
                payload={"source": "integration"},
                expected_version=1,
                idempotency_key=f"source:{source_command_id}",
                request_hash="0" * 64,
                correlation_id=uuid4(),
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        session.add(
            ExecutionInputModel(
                id=input_id,
                workspace_id=workspace_id,
                execution_id=ids["execution_id"],
                session_id=result.session_id,
                client_idempotency_key="gateway-input-1",
                text="continue the assigned task",
                attachment_refs=[],
                status="queued",
                operation_id=None,
                failure_code=None,
                version=1,
                source_command_id=source_command_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    dispatcher = InputDispatcher(
        lambda: SqlAlchemyUnitOfWork(database), gateway, clock=lambda: NOW
    )
    report = dispatcher.run_once()

    assert report.claimed == 1
    assert report.delivered == 1
    with database.session() as session:
        stored_input = session.get(ExecutionInputModel, input_id)
        assert stored_input is not None
        assert stored_input.status == "delivered"
        assert stored_input.operation_id == input_operation_id(input_id)
        operations = session.query(GatewayOperationModel).filter_by(
            execution_id=ids["execution_id"],
            operation_type=GatewayOperationType.SEND_INPUT.value,
        )
        assert operations.count() == 2
        assert operations.filter_by(status="succeeded").count() == 2

    gateway.sync_session(
        workspace_id=workspace_id,
        execution_id=ids["execution_id"],
        session_id=result.session_id,
        lease_id=ids["lease_id"],
        fencing_token=1,
    )
    with database.session() as session:
        stored_session = session.get(ExecutionSessionModel, result.session_id)
        assert stored_session is not None
        assert stored_session.last_sequence == 4
        assert session.query(ExecutionEventModel).count() == 4


def test_replaying_a_lost_input_response_reuses_operation_and_session(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    started = gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))
    adapter.release_boundary(adapter.active_sessions()[0])
    prompt_count_before_replay = adapter.input_count
    item = ExecutionInput.accept(
        id=uuid4(),
        workspace_id=workspace_id,
        execution_id=ids["execution_id"],
        session_id=started.session_id,
        client_idempotency_key="lost-response-input",
        text="continue on the same Session",
        attachment_refs=(),
        now=NOW,
    )
    item.queue()
    operation_id = uuid4()
    item.begin_dispatch(operation_id=operation_id)

    first = gateway.send_input(item)
    replay = gateway.send_input(item)

    assert first == replay
    assert first.operation_id == operation_id
    assert first.outcome is DeliveryOutcome.DELIVERED
    assert adapter.input_count == prompt_count_before_replay + 1
    with database.session() as session:
        assert session.query(ExecutionSessionModel).count() == 1
        operation = session.query(GatewayOperationModel).filter_by(
            operation_id=operation_id
        ).one()
        assert operation.status == "succeeded"
        assert operation.session_id == started.session_id


def test_gateway_stops_exact_execution_and_continues_without_touching_sibling(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    sibling_ids = _seed_assignment(database, workspace_id, name_suffix="-sibling")
    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == sibling_ids["execution_id"],
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(task_id=ids["task_id"])
        )
        session.commit()
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    first_session = gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    sibling_session = gateway.handle(
        _message(workspace_id, sibling_ids["execution_id"], sibling_ids["lease_id"])
    )
    with database.session() as session:
        first_execution = session.get(ExecutionModel, ids["execution_id"])
        sibling_execution = session.get(ExecutionModel, sibling_ids["execution_id"])
        assert first_execution is not None
        assert sibling_execution is not None
        first_version = first_execution.version
        assert first_execution.status == "running"
        assert sibling_execution.status == "running"

    _command_id, stop_message = _request_stop(
        database, workspace_id, ids["execution_id"], first_version
    )
    stopped = gateway.handle(stop_message)
    replayed = gateway.handle(stop_message)

    assert isinstance(stopped, GatewayStopResult)
    assert isinstance(replayed, GatewayStopResult)
    assert stopped == replayed
    assert stopped.operation_id == _operation_id(
        ids["execution_id"], GatewayOperationType.STOP_SESSION
    )
    assert adapter.stop_count == 1
    assert adapter.stop_results[0].execution_session_id == first_session.session_id
    with database.session() as session:
        stopped_execution = session.get(ExecutionModel, ids["execution_id"])
        sibling_execution = session.get(ExecutionModel, sibling_ids["execution_id"])
        stopped_session = session.get(ExecutionSessionModel, first_session.session_id)
        sibling_session_row = session.get(
            ExecutionSessionModel, sibling_session.session_id
        )
        stopped_lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        stopped_worker = session.get(WorkerModel, ids["worker_id"])
        assert stopped_execution is not None
        assert sibling_execution is not None
        assert stopped_session is not None
        assert sibling_session_row is not None
        assert stopped_lease is not None
        assert stopped_worker is not None
        assert stopped_execution.status == "canceled"
        assert stopped_execution.stop_requested is True
        assert stopped_execution.active_lease_id is None
        assert sibling_execution.status == "running"
        assert sibling_execution.active_lease_id == sibling_ids["lease_id"]
        assert stopped_session.status == "closed"
        assert sibling_session_row.status == "active"
        assert stopped_lease.status == "released"
        assert stopped_worker.current_load == 0
        operation = session.query(GatewayOperationModel).filter_by(
            execution_id=ids["execution_id"],
            operation_type=GatewayOperationType.STOP_SESSION.value,
        ).one()
        assert operation.status == "succeeded"
        closed_session_version = stopped_session.version

    _request_continue(
        database,
        workspace_id,
        first_session.session_id,
        closed_session_version,
    )

    with database.session() as session:
        continuation = (
            session.query(ExecutionModel)
            .filter_by(source_execution_id=ids["execution_id"])
            .one()
        )
        assert continuation.status == "queued"
        assert continuation.source_session_id == first_session.session_id
        assert continuation.task_id == ids["task_id"]
        assert continuation.active_lease_id is None
        assert continuation.adapter_preferences == ["opencode", "codex"]
        assert continuation.adapter_forbidden == ["unsafe"]
        assert session.query(ExecutionModel).count() == 3
        task = session.get(TaskModel, ids["task_id"])
        assert task is not None
        assert task.status == "running"


def test_unknown_stop_blocks_continuation_and_preserves_running_session(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = UnknownStopAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )
    started = gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        assert execution is not None
        execution_version = execution.version

    _, stop_message = _request_stop(
        database, workspace_id, ids["execution_id"], execution_version
    )
    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(stop_message)

    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        stored_session = session.get(ExecutionSessionModel, started.session_id)
        assert execution is not None
        assert stored_session is not None
        assert execution.status == "running"
        assert execution.active_lease_id == ids["lease_id"]
        assert stored_session.status == "active"
        assert session.query(ExecutionModel).count() == 1
        operation = session.query(GatewayOperationModel).filter_by(
            execution_id=ids["execution_id"],
            operation_type=GatewayOperationType.STOP_SESSION.value,
        ).one()
        assert operation.status == "unknown"
        session_version = stored_session.version

    with pytest.raises(InvalidTransition, match="only a terminal Session"):
        _request_continue(
            database, workspace_id, started.session_id, session_version
        )
