from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from test_gateway_runner import (
    FakeWorkspaceProvider,
    RecordingFakeAdapter,
    _message,
    _seed_assignment,
)
from test_planning_context_gate import EmptyKnowledgeCatalog, seed_request
from test_scheduler import WorkerSpec, seed_scheduler_case

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    WorkerGateway,
)
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.models import (
    ContextPackageAcknowledgementModel,
    ContextPackageMaterializationAuditModel,
    ExecutionModel,
    SchedulingBlockModel,
    WorkerItemModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def test_scheduler_does_not_lease_before_execution_context_is_ready(
    database, workspace_id: UUID
) -> None:
    _task, _workers, execution = seed_scheduler_case(
        database,
        workspace_id,
        worker_specs=(WorkerSpec("context worker", frozenset({"code", "python"})),),
    )
    scheduler = Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=LowestCostWorkerPolicy(),
        clock=lambda: NOW,
        context_enforcement=True,
    )

    result = scheduler.run_once(1)

    assert result.examined == 1
    assert result.assigned == 0
    assert result.deferred == 1
    with database.session() as session:
        execution_row = session.get(ExecutionModel, execution.id)
        block = session.scalar(
            select(SchedulingBlockModel).where(
                SchedulingBlockModel.execution_id == execution.id,
                SchedulingBlockModel.status == "active",
            )
        )
    assert execution_row is not None
    assert execution_row.status == "queued"
    assert execution_row.active_lease_id is None
    assert block is not None
    assert block.reason_code == "context_package_missing"


def test_pre_cutover_execution_keeps_legacy_scheduler_path(
    database, workspace_id: UUID
) -> None:
    _task, _workers, execution = seed_scheduler_case(
        database,
        workspace_id,
        worker_specs=(WorkerSpec("legacy worker", frozenset({"code", "python"})),),
    )
    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution.id)
            .values(created_at=NOW)
        )
        session.commit()
    scheduler = Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=LowestCostWorkerPolicy(),
        clock=lambda: NOW,
        context_enforcement=True,
        context_package_cutover_at=NOW + timedelta(days=1),
    )

    result = scheduler.run_once(1)

    assert result.assigned == 1
    with database.session() as session:
        execution_row = session.get(ExecutionModel, execution.id)
    assert execution_row is not None
    assert execution_row.status == "leased"


def _install_execution_context(database, workspace_id: UUID, ids: dict[str, UUID]) -> UUID:
    _request_id, _run_id = seed_request(
        lambda: SqlAlchemyUnitOfWork(database), workspace_id
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        execution = uow.executions.get(ids["execution_id"], workspace_id)
        task = uow.tasks.get(ids["task_id"], workspace_id)
        context = uow.work_contexts.get_by_planning_request(_request_id, workspace_id)
        assert execution is not None
        assert task is not None
        assert context is not None
        revision = uow.work_contexts.get_revision(
            context.current_revision_id, workspace_id
        )
        assert revision is not None
        worker_item = WorkerItem.create(
            item_id=uuid4(),
            workspace_id=workspace_id,
            root_task_id=task.id,
            title="Context-gated worker item",
            responsibility="Execute with the exact context package",
            now=NOW,
        )
        worker_item.context_version = 2
        uow.worker_items.add(worker_item)
        uow.session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution.id)
            .values(worker_item_id=worker_item.id)
        )
        uow.context_assemblies.add(
            ContextAssemblyRequest.create(
                request_id=uuid4(),
                workspace_id=workspace_id,
                purpose=ContextPurpose.EXECUTION,
                consumer_type=ContextConsumerType.WORKER,
                consumer_id=execution.id,
                worker_item_id=worker_item.id,
                work_context_revision_id=revision.revision_id,
                context_version=worker_item.context_version,
                knowledge_search_scope={"query": ""},
                knowledge_filter_refs=(),
                fact_scope_refs=("execution",),
                budget={"max_chars": 64_000},
                expires_at=NOW + timedelta(hours=1),
                request_fingerprint="a" * 64,
                idempotency_key=f"test:execution-context:{execution.id}",
                created_at=NOW,
            )
        )
        claimed = uow.context_assemblies.claim_next(
            workspace_id=workspace_id,
            owner="context-gate-test",
            now=NOW,
            lease_ttl=timedelta(minutes=1),
            limit=1,
        )[0]
        package = ContextPackageManifestV1.create(
            package_id=uuid4(),
            workspace_id=workspace_id,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=execution.id,
            worker_item_id=worker_item.id,
            work_context_revision_id=revision.revision_id,
            context_version=worker_item.context_version,
            control_plane_fact_snapshot={"execution_id": str(execution.id)},
            knowledge_refs=(),
            artifact_refs=(),
            evidence_refs=(),
            budget=ContextBudgetV1(
                profile_id=uuid4(),
                profile_version=1,
                max_sections=16,
                max_chars=64_000,
                max_input_tokens=10_000,
            ),
            expires_at=NOW + timedelta(hours=1),
            idempotency_key=claimed.idempotency_key,
            created_at=NOW,
        )
        command_id = uuid4()
        command = CommandEnvelope(
            id=command_id,
            workspace_id=workspace_id,
            command_type="TestAssembleExecutionContext",
            target_type="context_package",
            actor_type="system",
            actor_id="context-gate-test",
            target_id=package.package_id,
            payload={},
            expected_version=None,
            idempotency_key=f"test:assemble-execution-context:{package.package_id}",
            correlation_id=uuid4(),
        )
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command_id,
                aggregate_id=package.package_id,
                aggregate_version=1,
                event_ids=(),
            ),
        )
        uow.context_assemblies.complete_with_package(
            claimed,
            package,
            owner="context-gate-test",
            expected_version=claimed.version,
            actor_type="system",
            actor_id="context-gate-test",
            causation_id=command_id,
            correlation_id=command.correlation_id,
        )
        return worker_item.id


class MismatchingContextAdapter(RecordingFakeAdapter):
    def accept_context(self, materialization, *, operation_id):
        acknowledgement = super().accept_context(
            materialization,
            operation_id=operation_id,
        )
        return acknowledgement.__class__(
            package_id=acknowledgement.package_id,
            package_hash="0" * 64,
            consumer_type=acknowledgement.consumer_type,
            consumer_id=acknowledgement.consumer_id,
            context_version=acknowledgement.context_version,
            adapter_module_id=acknowledgement.adapter_module_id,
            adapter_config_version=acknowledgement.adapter_config_version,
            acknowledged_at=acknowledgement.acknowledged_at,
            schema_version=acknowledgement.schema_version,
            operation_id=acknowledgement.operation_id,
        )


def test_gateway_rejects_context_before_workspace_or_session_side_effect(
    database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    worker_item_id = _install_execution_context(database, workspace_id, ids)
    adapter = MismatchingContextAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        context_materializer=ContextMaterializer(EmptyKnowledgeCatalog()),
        clock=lambda: NOW,
    )

    with pytest.raises(GatewayMessageError, match="acknowledgement"):
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert adapter.start_count == 0
    assert adapter.input_count == 0
    assert provider.prepare_count == 0
    with database.session() as session:
        assert session.scalar(
            select(ContextPackageAcknowledgementModel).where(
                ContextPackageAcknowledgementModel.consumer_id == ids["execution_id"]
            )
        ) is None
        assert session.scalar(
            select(ContextPackageMaterializationAuditModel).where(
                ContextPackageMaterializationAuditModel.consumer_id == ids["execution_id"]
            )
        ) is None
        item = session.get(WorkerItemModel, worker_item_id)
        assert item is not None
        assert item.acknowledged_context_version == 1
