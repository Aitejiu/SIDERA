from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from sidera_control_plane.adapters.knowledge_http import KnowledgeAdapterError
from sidera_control_plane.application.context_requests import (
    build_planning_context_assembly_request,
)
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactSectionBody,
    KnowledgeCatalogPort,
    SearchHit,
)
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.context_packages import ContextPurpose
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevisionReason,
)
from sidera_control_plane.persistence.models import (
    CommandModel,
    ContextAssemblyRequestModel,
    ContextPackageModel,
    EventModel,
    OutboxMessageModel,
)
from tests.integration.test_planning_runner_integration import seed_request

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class EmptyCatalog(KnowledgeCatalogPort):
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

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
        if self.error is not None:
            raise self.error
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


def _seed_assembly(uow_factory, workspace_id: UUID) -> tuple[UUID, UUID]:
    planning_request_id = seed_request(uow_factory, workspace_id)
    with uow_factory() as uow:
        planning_request = uow.planning_requests.get(
            planning_request_id, workspace_id
        )
        assert planning_request is not None
        run = planning_request.runs[0]
        revision = WorkContextRevisionFactory().create(
            work_context_id=UUID(int=planning_request_id.int ^ 1),
            workspace_id=workspace_id,
            brief=planning_request.briefs[0],
            revision_number=1,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
            actor="operator",
            created_at=NOW,
        )
        context = WorkContext.create(
            work_context_id=revision.work_context_id,
            workspace_id=workspace_id,
            planning_request_id=planning_request.id,
            first_revision=revision,
            created_at=NOW,
        )
        uow.work_contexts.add(context, revision)
        request = build_planning_context_assembly_request(
            run=run,
            brief=planning_request.briefs[0],
            work_context_revision=revision,
            ttl_seconds=1800,
            created_at=NOW,
        )
        uow.context_assemblies.add(request)
        return planning_request.id, request.request_id


def _worker(
    uow_factory,
    catalog: KnowledgeCatalogPort,
    owner_id: str,
    *,
    clock=None,
) -> ContextPackageWorker:
    return ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=catalog,
        owner_id=owner_id,
        lease_seconds=60,
        clock=clock or (lambda: NOW),
    )


def test_two_context_workers_claim_one_request_and_write_one_event(
    database, workspace_id: UUID, uow_factory
) -> None:
    planning_request_id, assembly_request_id = _seed_assembly(uow_factory, workspace_id)
    catalog = EmptyCatalog()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _worker(uow_factory, catalog, f"context-worker-{index}").run_once, 1
            )
            for index in ("a", "b")
        ]
        results = [future.result() for future in futures]

    assert sum(item.claimed for item in results) == 1
    assert sum(item.ready for item in results) == 1
    assert sum(item.failed for item in results) == 0

    with database.session() as session:
        assembly = session.get(ContextAssemblyRequestModel, assembly_request_id)
        assert assembly is not None
        assert assembly.status == "ready"
        assert session.scalar(select(func.count()).select_from(ContextPackageModel)) == 1
        events = session.scalars(
            select(EventModel).where(EventModel.event_type == "ContextPackageCreated")
        ).all()
        assert len(events) == 1
        commands = session.scalars(
            select(CommandModel).where(CommandModel.command_type == "ContextAssemblyWorker")
        ).all()
        assert len(commands) == 1
        assert events[0].causation_id == commands[0].id
        outbox = session.scalar(
            select(OutboxMessageModel).where(OutboxMessageModel.event_id == events[0].id)
        )
        assert outbox is not None
        assert outbox.topic == "context_package.created"

    with uow_factory() as uow:
        planning_request = uow.planning_requests.get(planning_request_id, workspace_id)
        assert planning_request is not None
        assert planning_request.runs[0].status.value == "queued"


def test_permanent_context_failure_blocks_planning_run_with_audited_causation(
    database, workspace_id: UUID, uow_factory
) -> None:
    planning_request_id, assembly_request_id = _seed_assembly(uow_factory, workspace_id)
    catalog = EmptyCatalog(
        KnowledgeAdapterError(
            "context_service_rejected",
            "Knowledge service rejected the query",
            temporary=False,
        )
    )

    result = _worker(uow_factory, catalog, "context-worker-failing").run_once(1)

    assert result.claimed == 1
    assert result.blocked == 1
    assert result.ready == 0
    with database.session() as session:
        assembly = session.get(ContextAssemblyRequestModel, assembly_request_id)
        assert assembly is not None
        assert assembly.status == "blocked"
        assert assembly.error_code == "context_service_rejected"
        assert session.scalar(select(func.count()).select_from(ContextPackageModel)) == 0
        event = session.scalar(
            select(EventModel).where(EventModel.event_type == "PlanningRunContextBlocked")
        )
        assert event is not None
        command = session.get(CommandModel, event.causation_id)
        assert command is not None
        assert command.command_type == "ContextAssemblyWorker"
        assert session.scalar(
            select(func.count()).select_from(OutboxMessageModel).where(
                OutboxMessageModel.event_id == event.id
            )
        ) == 1

    with uow_factory() as uow:
        planning_request = uow.planning_requests.get(planning_request_id, workspace_id)
        assert planning_request is not None
        assert planning_request.status.value == "failed"
        assert planning_request.runs[0].status.value == "blocked"
        assert planning_request.runs[0].error_code == "context_service_rejected"


def test_worker_reclaims_expired_lease_after_restart(
    workspace_id: UUID, uow_factory
) -> None:
    _planning_request_id, assembly_request_id = _seed_assembly(
        uow_factory, workspace_id
    )
    with uow_factory() as uow:
        claimed = uow.context_assemblies.claim_next(
            workspace_id=workspace_id,
            owner="context-worker-before-restart",
            now=NOW,
            lease_ttl=timedelta(seconds=30),
            limit=1,
        )
        assert len(claimed) == 1

    result = _worker(
        uow_factory,
        EmptyCatalog(),
        "context-worker-after-restart",
        clock=lambda: NOW + timedelta(seconds=31),
    ).run_once(limit=1)

    assert result.claimed == 1
    assert result.ready == 1
    with uow_factory() as uow:
        request = uow.context_assemblies.get_for_consumer(
            ContextPurpose.PLANNING_RUN,
            claimed[0].consumer_id,
            workspace_id,
        )
        assert request is not None
        assert request.request_id == assembly_request_id
