from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update

from sidera_control_plane.adapters.knowledge_http import (
    KnowledgeAdapterError,
    KnowledgeHttpAdapter,
)
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.knowledge_candidate_submission import (
    FileCandidateProposalSpool,
)
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    WorkerGateway,
)
from sidera_control_plane.domain.context_packages import (
    ContextPackageStatus,
    ContextPurpose,
)
from sidera_control_plane.knowledge_candidate_worker import (
    KnowledgeCandidateSubmissionWorker,
)
from sidera_control_plane.persistence.context_package_repository import (
    ContextPackageRepository,
)
from sidera_control_plane.persistence.models import (
    CommandModel,
    ContextAssemblyRequestModel,
    ContextPackageAcknowledgementModel,
    ContextPackageKnowledgeRefModel,
    ContextPackageMaterializationAuditModel,
    ContextPackageModel,
    EventModel,
    ExecutionSessionModel,
    WorkerItemModel,
)
from sidera_control_plane.persistence.repositories import EventRepository
from tests.integration.test_context_package_worker import (
    EmptyCatalog,
    _seed_assembly,
    _worker,
)
from tests.integration.test_execution_context_gate import (
    EmptyKnowledgeCatalog,
    _install_execution_context,
)
from tests.integration.test_gateway_runner import (
    NOW as GATEWAY_NOW,
)
from tests.integration.test_gateway_runner import (
    FakeWorkspaceProvider,
    RecordingFakeAdapter,
    _message,
    _seed_assignment,
)
from tests.integration.test_knowledge_candidate_submission import (
    FakeKnowledgeCatalog,
    _proposal,
    _queue,
)
from tests.integration.test_planning_context_gate import (
    RecordingPlanner,
    runner,
    seed_request,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def test_knowledge_down_retries_ref_assembly_without_creating_an_empty_package(
    database, workspace_id: UUID, uow_factory
) -> None:
    _planning_request_id, assembly_id = _seed_assembly(uow_factory, workspace_id)
    catalog = EmptyCatalog(
        KnowledgeAdapterError(
            "temporary_unavailable",
            "Knowledge service is unavailable",
            temporary=True,
        )
    )

    result = _worker(
        uow_factory,
        catalog,
        "r5-3-fault-context-down",
        clock=lambda: NOW,
    ).run_once(limit=1)

    assert result.claimed == 1
    assert result.retried == 1
    with database.session() as session:
        assembly = session.get(ContextAssemblyRequestModel, assembly_id)
        assert assembly is not None
        assert assembly.status == "retry_wait"
        assert assembly.error_code == "temporary_unavailable"
        assert session.scalar(select(func.count()).select_from(ContextPackageModel)) == 0


def test_context_without_knowledge_refs_completes_locally_when_knowledge_is_down(
    database, workspace_id: UUID, uow_factory
) -> None:
    _planning_request_id, assembly_id = _seed_assembly(uow_factory, workspace_id)
    with database.session() as session:
        session.execute(
            update(ContextAssemblyRequestModel)
            .where(ContextAssemblyRequestModel.id == assembly_id)
            .values(knowledge_search_scope={})
        )
        session.commit()

    catalog = EmptyCatalog(
        KnowledgeAdapterError(
            "temporary_unavailable",
            "Knowledge service is unavailable",
            temporary=True,
        )
    )
    result = _worker(
        uow_factory,
        catalog,
        "r5-3-fault-context-local",
        clock=lambda: NOW,
    ).run_once(limit=1)

    assert result.ready == 1
    assert result.retried == 0
    with database.session() as session:
        assembly = session.get(ContextAssemblyRequestModel, assembly_id)
        assert assembly is not None
        assert assembly.status == "ready"
        package = session.scalar(select(ContextPackageModel))
        assert package is not None
        assert session.scalar(
            select(func.count()).select_from(ContextPackageKnowledgeRefModel)
        ) == 0


def test_wrong_knowledge_capability_token_blocks_without_a_package(
    database, workspace_id: UUID, uow_factory
) -> None:
    _planning_request_id, assembly_id = _seed_assembly(uow_factory, workspace_id)

    def forbidden(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "permission_denied"})

    client = httpx.Client(transport=httpx.MockTransport(forbidden))
    catalog = KnowledgeHttpAdapter(
        base_url="http://knowledge.invalid",
        control_plane_token=SecretStr("wrong-token-fixture"),
        client=client,
    )
    try:
        result = _worker(
            uow_factory,
            catalog,
            "r5-3-fault-wrong-token",
            clock=lambda: NOW,
        ).run_once(limit=1)
    finally:
        catalog.close()
        client.close()

    assert result.blocked == 1
    with database.session() as session:
        assembly = session.get(ContextAssemblyRequestModel, assembly_id)
        assert assembly is not None
        assert assembly.status == "blocked"
        assert assembly.error_code == "permission_denied"
        assert session.scalar(select(func.count()).select_from(ContextPackageModel)) == 0


def test_planner_ack_mismatch_blocks_before_model_call(
    uow_factory, workspace_id: UUID
) -> None:
    request_id, run_id = seed_request(uow_factory, workspace_id)
    adapter = RecordingPlanner(mismatch=True)

    result = runner(
        uow_factory,
        adapter,
        context_materializer=ContextMaterializer(EmptyKnowledgeCatalog()),
    ).run_once(1)

    assert result.blocked == 1
    assert adapter.events == ["accept"]
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.run(run_id).status.value == "blocked"


def test_worker_ack_mismatch_blocks_before_workspace_or_session_side_effect(
    database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    worker_item_id = _install_execution_context(database, workspace_id, ids)
    adapter = _MismatchingWorkerAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: _uow_factory(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        context_materializer=ContextMaterializer(EmptyKnowledgeCatalog()),
        clock=lambda: GATEWAY_NOW,
    )

    with pytest.raises(GatewayMessageError, match="acknowledgement") as error_info:
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert error_info.value.code == "context_acknowledgement_mismatch"
    assert adapter.start_count == 0
    assert adapter.input_count == 0
    assert provider.prepare_count == 0
    with database.session() as session:
        assert session.scalar(select(ExecutionSessionModel)) is None
        assert session.scalar(
            select(ContextPackageAcknowledgementModel).where(
                ContextPackageAcknowledgementModel.consumer_id == ids["execution_id"]
            )
        ) is None
        item = session.get(WorkerItemModel, worker_item_id)
        assert item is not None
        assert item.acknowledged_context_version == 1


def test_expired_context_package_fails_closed_before_worker_start(
    database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    _install_execution_context(database, workspace_id, ids)
    with database.session() as session:
        package = ContextPackageRepository(session).get_for_consumer(
            ContextPurpose.EXECUTION,
            ids["execution_id"],
            workspace_id,
        )
        assert package is not None
        expires_at = GATEWAY_NOW + timedelta(seconds=1)
        expired_manifest = replace(package, expires_at=expires_at)
        session.execute(
            update(ContextPackageModel)
            .where(ContextPackageModel.consumer_id == ids["execution_id"])
            .values(
                expires_at=expires_at,
                package_hash=expired_manifest.package_hash,
            )
        )
        session.commit()

    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: _uow_factory(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        context_materializer=ContextMaterializer(EmptyKnowledgeCatalog()),
        clock=lambda: GATEWAY_NOW + timedelta(seconds=2),
    )

    with pytest.raises(GatewayMessageError) as error_info:
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert error_info.value.code == "context_package_expired"
    assert adapter.start_count == 0
    assert adapter.input_count == 0
    assert provider.prepare_count == 0
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionSessionModel)) == 0


def test_revoked_context_package_rejects_new_start_without_deleting_running_session(
    database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    _install_execution_context(database, workspace_id, ids)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = _gateway(database, adapter, provider)
    first = gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    with database.session() as session:
        package = session.scalar(
            select(ContextPackageModel).where(
                ContextPackageModel.consumer_id == ids["execution_id"]
            )
        )
        assert package is not None
        repository = ContextPackageRepository(session, EventRepository(session))
        causation_id = session.scalar(
            select(CommandModel.id).where(
                CommandModel.command_type == "TestAssembleExecutionContext"
            )
        )
        assert causation_id is not None
        repository.revoke(
            package.id,
            workspace_id,
            expected_package_hash=package.package_hash,
            reason="r5-3-fault-recovery",
            scope="new_starts",
            now=GATEWAY_NOW + timedelta(seconds=1),
            actor_type="system",
            actor_id="r5-3-fault-recovery",
            causation_id=causation_id,
            correlation_id=uuid4(),
        )
        session.commit()

    restarted = _gateway(database, adapter, provider)
    with pytest.raises(GatewayMessageError) as error_info:
        restarted.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert error_info.value.code == "context_package_revoked"
    assert adapter.start_count == 1
    assert adapter.input_count == 1
    assert provider.prepare_count == 1
    with database.session() as session:
        session.expire_all()
        session_row = session.scalar(select(ExecutionSessionModel))
        package = session.scalar(
            select(ContextPackageModel).where(
                ContextPackageModel.consumer_id == ids["execution_id"]
            )
        )
        assert session_row is not None
        assert session_row.id == first.session_id
        assert session_row.status == "active"
        assert package is not None
        assert package.status == ContextPackageStatus.REVOKED.value


def test_context_worker_restart_reclaims_one_lease_and_writes_one_package(
    database, workspace_id: UUID, uow_factory
) -> None:
    _planning_request_id, assembly_id = _seed_assembly(uow_factory, workspace_id)
    with uow_factory() as uow:
        claimed = uow.context_assemblies.claim_next(
            workspace_id=workspace_id,
            owner="r5-3-fault-before-restart",
            now=NOW,
            lease_ttl=timedelta(seconds=30),
            limit=1,
        )
        assert len(claimed) == 1

    recovered = _worker(
        uow_factory,
        EmptyCatalog(),
        "r5-3-fault-after-restart",
        clock=lambda: NOW + timedelta(seconds=31),
    ).run_once(limit=1)
    replay = _worker(
        uow_factory,
        EmptyCatalog(),
        "r5-3-fault-replay",
        clock=lambda: NOW + timedelta(seconds=31),
    ).run_once(limit=1)

    assert recovered.ready == 1
    assert replay.claimed == 0
    with database.session() as session:
        assembly = session.get(ContextAssemblyRequestModel, assembly_id)
        assert assembly is not None
        assert assembly.status == "ready"
        assert session.scalar(select(func.count()).select_from(ContextPackageModel)) == 1
        assert session.scalar(
            select(func.count()).select_from(EventModel).where(
                EventModel.event_type == "ContextPackageCreated"
            )
        ) == 1


def test_gateway_restart_after_context_ack_reuses_package_without_duplicate_session(
    database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    _install_execution_context(database, workspace_id, ids)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    crashed = WorkerGateway(
        lambda: _uow_factory(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        spec_factory=_FailingSpecFactory(),
        context_materializer=ContextMaterializer(EmptyKnowledgeCatalog()),
        clock=lambda: GATEWAY_NOW,
    )

    with pytest.raises(RuntimeError, match="injected spec boundary"):
        crashed.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    recovered = _gateway(database, adapter, provider)
    first = recovered.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    replayed = recovered.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )

    assert first == replayed
    assert adapter.start_count == 1
    assert adapter.input_count == 1
    assert provider.prepare_count == 1
    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(ContextPackageAcknowledgementModel)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(ContextPackageMaterializationAuditModel)
        ) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionSessionModel)) == 1


def test_unknown_knowledge_command_is_reprobed_and_resubmitted(
    workspace_id: UUID, uow_factory, tmp_path: Path
) -> None:
    spool = FileCandidateProposalSpool(
        tmp_path / "candidate-spool",
        quota_bytes=1_000_000,
    )
    submission_id = _queue(uow_factory, workspace_id, spool, _proposal())
    catalog = FakeKnowledgeCatalog(command_missing=True)
    worker = KnowledgeCandidateSubmissionWorker(
        uow_factory=uow_factory,
        catalog=catalog,
        spool=spool,
        owner_id="r5-3-fault-candidate-replay",
        clock=lambda: NOW,
    )

    result = worker.run_once()

    assert result.submitted == 1
    assert result.rejected == 0
    assert catalog.status_calls == 1
    assert len(catalog.submitted) == 1
    assert spool.usage_bytes() == 0
    with uow_factory() as uow:
        submission = uow.knowledge_candidate_submissions.get(
            submission_id, workspace_id
        )
        assert submission is not None
        assert submission.status.value == "submitted"


def _uow_factory(database):
    from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

    return SqlAlchemyUnitOfWork(database)


def _gateway(database, adapter, provider) -> WorkerGateway:
    return WorkerGateway(
        lambda: _uow_factory(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        context_materializer=ContextMaterializer(EmptyKnowledgeCatalog()),
        clock=lambda: GATEWAY_NOW,
    )


class _MismatchingWorkerAdapter(RecordingFakeAdapter):
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


class _FailingSpecFactory:
    def create(self, *args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected spec boundary")
