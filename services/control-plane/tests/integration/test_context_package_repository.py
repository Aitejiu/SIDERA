from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextBudgetV1,
    ContextConsumerType,
    ContextMaterializationAudit,
    ContextMaterializationOutcome,
    ContextPackageKnowledgeRef,
    ContextPackageManifestV1,
    ContextPackageStatus,
    ContextPurpose,
    ContextSelectionReason,
    ContextTrustLevel,
)
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.work_context import (
    ContextTextItem,
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
)
from sidera_control_plane.persistence.context_package_repository import (
    ContextAssemblyRepository,
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
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    PlanningBriefRevisionModel,
    PlanningProfileModel,
    PlanningRequestModel,
)
from sidera_control_plane.persistence.repositories import EventRepository
from sidera_control_plane.persistence.work_context_repository import (
    WorkContextRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class EventContext(TypedDict):
    actor_type: str
    actor_id: str
    causation_id: UUID
    correlation_id: UUID


def _snapshot() -> WorkContextSnapshotV1:
    return WorkContextSnapshotV1.create(
        objective="Assemble a typed context package",
        constraints=(),
        acceptance_criteria=(),
        requested_outputs=(
            ContextTextItem(key="package", text="A durable package", ordinal=0),
        ),
        clarification_decisions=(),
        accepted_decisions=(),
        unresolved_questions=(),
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        checkpoint_refs=(),
    )


def _seed_context(database, workspace_id: UUID) -> tuple[UUID, UUID, UUID, UUID]:
    definition_id = uuid4()
    module_id = uuid4()
    profile_id = uuid4()
    request_id = uuid4()
    context_id = uuid4()
    revision_id = uuid4()
    with database.session() as session:
        session.add(
            ModuleDefinitionModel(
                id=definition_id,
                workspace_id=workspace_id,
                module_type="planner_adapter",
                provider="fake",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities={},
                lifecycle="active",
            )
        )
        session.add(
            ModuleInstanceModel(
                id=module_id,
                workspace_id=workspace_id,
                definition_id=definition_id,
                instance_name=f"context-package-{module_id.hex[:8]}",
                config_version=1,
                config_namespace="planning",
                config={},
                secret_refs=[],
                lifecycle="active",
            )
        )
        session.add(
            PlanningProfileModel(
                id=profile_id,
                workspace_id=workspace_id,
                profile_key=f"context-package-{profile_id.hex[:8]}",
                version=1,
                display_name="Context package test profile",
                planner_module_instance_id=module_id,
                planner_module_config_version=1,
                planner_interface_version="sidera.planner.v1",
                status="active",
                user_selectable=True,
                policy={},
            )
        )
        session.flush()
        planning_request = PlanningRequestModel(
            id=request_id,
            workspace_id=workspace_id,
            kind="create",
            status="submitted",
            current_brief_revision=None,
            current_proposal_revision=None,
            current_proposal_content_hash=None,
            planning_profile_id=profile_id,
            planning_profile_version=1,
            profile_snapshot={},
            source_type="integration",
            source_ref="test:context-package",
            actor="integration",
            correlation_id=uuid4(),
            version=1,
        )
        session.add(planning_request)
        session.flush()
        session.add(
            PlanningBriefRevisionModel(
                planning_request_id=request_id,
                revision=1,
                workspace_id=workspace_id,
                payload={"objective": "Assemble a typed context package"},
                created_by="integration",
                created_at=NOW,
            )
        )
        session.flush()
        planning_request.current_brief_revision = 1
        session.flush()
        revision = WorkContextRevision.create(
            revision_id=revision_id,
            work_context_id=context_id,
            workspace_id=workspace_id,
            revision_number=1,
            brief_planning_request_id=request_id,
            brief_revision=1,
            proposal_revision_id=None,
            plan_snapshot_id=None,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
            snapshot=_snapshot(),
            created_by="integration",
            created_at=NOW,
        )
        context = WorkContext.create(
            work_context_id=context_id,
            workspace_id=workspace_id,
            planning_request_id=request_id,
            first_revision=revision,
            created_at=NOW,
        )
        WorkContextRepository(session).add(context, revision)
        session.commit()
    return request_id, revision_id, context_id, module_id


def _request(
    *,
    workspace_id: UUID,
    revision_id: UUID,
    consumer_id: UUID,
    request_id: UUID | None = None,
    fingerprint: str = "a" * 64,
    idempotency_key: str = "context-request-1",
) -> ContextAssemblyRequest:
    return ContextAssemblyRequest.create(
        request_id=request_id or uuid4(),
        workspace_id=workspace_id,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=consumer_id,
        worker_item_id=None,
        work_context_revision_id=revision_id,
        context_version=None,
        knowledge_search_scope={},
        knowledge_filter_refs=(),
        fact_scope_refs=(),
        budget={
            "profile_id": str(uuid4()),
            "profile_version": 1,
            "max_sections": 8,
            "max_chars": 20_000,
            "max_input_tokens": 4_000,
        },
        expires_at=NOW + timedelta(hours=1),
        request_fingerprint=fingerprint,
        idempotency_key=idempotency_key,
        created_at=NOW,
    )


def _manifest(
    *,
    workspace_id: UUID,
    revision_id: UUID,
    consumer_id: UUID,
    request_id: UUID,
) -> ContextPackageManifestV1:
    knowledge_ref = ContextPackageKnowledgeRef(
        page_id=uuid4(),
        revision_id=uuid4(),
        revision_hash="b" * 64,
        section_key="overview",
        section_hash="c" * 64,
        source_ids=(uuid4(),),
        selection_reason=ContextSelectionReason.EXPLICIT,
        trust_level=ContextTrustLevel.WORKSPACE_CONFIRMED,
        ordinal=0,
    )
    return ContextPackageManifestV1.create(
        package_id=uuid4(),
        workspace_id=workspace_id,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=consumer_id,
        worker_item_id=None,
        work_context_revision_id=revision_id,
        context_version=None,
        control_plane_fact_snapshot={"planning_request_id": str(request_id)},
        knowledge_refs=(knowledge_ref,),
        artifact_refs=(),
        evidence_refs=(),
        budget=ContextBudgetV1(
            profile_id=uuid4(),
            profile_version=1,
            max_sections=8,
            max_chars=20_000,
            max_input_tokens=4_000,
            actual_sections=1,
            actual_chars=128,
            actual_input_tokens=32,
        ),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="context-request-1",
        created_at=NOW,
    )


def _event_context(session: Session, workspace_id: UUID) -> EventContext:
    command_id = uuid4()
    correlation_id = uuid4()
    session.add(
        CommandModel(
            id=command_id,
            workspace_id=workspace_id,
            command_type="AssembleContextPackage",
            actor_type="system",
            actor_id="context-package-test",
            target_type="workspace",
            target_id=None,
            payload={},
            expected_version=None,
            idempotency_key=f"context-package-test-{command_id}",
            request_hash="a" * 64,
            correlation_id=correlation_id,
            status="applied",
            result={},
            rejection_reason=None,
        )
    )
    session.flush()
    return {
        "actor_type": "system",
        "actor_id": "context-package-test",
        "causation_id": command_id,
        "correlation_id": correlation_id,
    }


def test_assembly_request_is_idempotent_and_lease_fenced(database, workspace_id: UUID) -> None:
    _, revision_id, _, _ = _seed_context(database, workspace_id)
    consumer_id = uuid4()
    first = _request(
        workspace_id=workspace_id,
        revision_id=revision_id,
        consumer_id=consumer_id,
    )
    with database.session() as session:
        repository = ContextAssemblyRepository(session)
        assert repository.add(first) == first
        session.commit()

    replay = _request(
        workspace_id=workspace_id,
        revision_id=revision_id,
        consumer_id=consumer_id,
        request_id=uuid4(),
    )
    with database.session() as session:
        repository = ContextAssemblyRepository(session)
        assert repository.add(replay).request_id == first.request_id
        with pytest.raises(ValidationError, match="fingerprint"):
            repository.add(
                _request(
                    workspace_id=workspace_id,
                    revision_id=revision_id,
                    consumer_id=consumer_id,
                    fingerprint="d" * 64,
                )
            )

    with database.session() as session:
        claimed = ContextAssemblyRepository(session).claim_next(
            workspace_id=workspace_id,
            owner="worker-a",
            now=NOW,
            lease_ttl=timedelta(seconds=10),
            limit=1,
        )
        assert len(claimed) == 1
        first_claim = claimed[0]
        assert first_claim.status is ContextAssemblyStatus.ASSEMBLING
        session.commit()

    with database.session() as session:
        assert (
            ContextAssemblyRepository(session).claim_next(
                workspace_id=workspace_id,
                owner="worker-b",
                now=NOW + timedelta(seconds=5),
                lease_ttl=timedelta(seconds=10),
                limit=1,
            )
            == ()
        )

    with database.session() as session:
        reclaimed = ContextAssemblyRepository(session).claim_next(
            workspace_id=workspace_id,
            owner="worker-b",
            now=NOW + timedelta(seconds=11),
            lease_ttl=timedelta(seconds=10),
            limit=1,
        )
        assert len(reclaimed) == 1
        second_claim = reclaimed[0]
        assert second_claim.attempt == first_claim.attempt + 1
        session.commit()

    with database.session() as session, pytest.raises(VersionConflict):
        ContextAssemblyRepository(session).save_blocked(
            first_claim,
            owner="worker-a",
            expected_version=first_claim.version,
            error_code="context_hash_mismatch",
        )


def test_complete_package_writes_refs_events_and_outbox_atomically(
    database, workspace_id: UUID
) -> None:
    request_id, revision_id, _, module_id = _seed_context(database, workspace_id)
    consumer_id = uuid4()
    request = _request(
        workspace_id=workspace_id,
        revision_id=revision_id,
        consumer_id=consumer_id,
    )
    with database.session() as session:
        events = EventRepository(session)
        assembly = ContextAssemblyRepository(session, events)
        assembly.add(request)
        session.flush()
        claimed = assembly.claim_next(
            workspace_id=workspace_id,
            owner="context-worker",
            now=NOW,
            lease_ttl=timedelta(minutes=1),
            limit=1,
        )[0]
        manifest = _manifest(
            workspace_id=workspace_id,
            revision_id=revision_id,
            consumer_id=consumer_id,
            request_id=request_id,
        )
        assembly.complete_with_package(
            claimed,
            manifest,
            owner="context-worker",
            expected_version=claimed.version,
            **_event_context(session, workspace_id),
        )
        session.commit()

    with database.session() as session:
        stored_request = session.scalar(
            select(ContextAssemblyRequestModel).where(
                ContextAssemblyRequestModel.id == request.request_id
            )
        )
        assert stored_request is not None
        assert stored_request.status == ContextAssemblyStatus.READY.value
        assert stored_request.lease_owner is None
        assert session.scalar(
            select(ContextPackageModel).where(ContextPackageModel.id == manifest.package_id)
        ) is not None
        assert session.scalar(
            select(func.count()).select_from(ContextPackageKnowledgeRefModel).where(
                ContextPackageKnowledgeRefModel.package_id == manifest.package_id
            )
        ) == 1
        event = session.scalar(
            select(EventModel).where(EventModel.aggregate_id == manifest.package_id)
        )
        assert event is not None
        assert event.event_type == "ContextPackageCreated"
        assert session.scalar(
            select(OutboxMessageModel.topic).where(
                OutboxMessageModel.event_id == event.id
            )
        ) == "context_package.created"
        assert event.payload["package_hash"] == manifest.package_hash
        assert "body" not in str(event.payload).lower()
        assert session.scalar(
            select(OutboxMessageModel).where(OutboxMessageModel.event_id == event.id)
        ) is not None

        package_repository = ContextPackageRepository(session, EventRepository(session))
        acknowledgement = ContextAcknowledgement(
            package_id=manifest.package_id,
            package_hash=manifest.package_hash,
            consumer_type=ContextConsumerType.PLANNER,
            consumer_id=consumer_id,
            context_version=None,
            adapter_module_id=module_id,
            adapter_config_version=1,
            acknowledged_at=NOW,
        )
        assert package_repository.add_ack(
            acknowledgement, **_event_context(session, workspace_id)
        )
        assert package_repository.add_ack(
            acknowledgement, **_event_context(session, workspace_id)
        )
        with pytest.raises(ValidationError, match="acknowledgement package hash"):
            package_repository.add_ack(
                replace(acknowledgement, package_hash="d" * 64),
                **_event_context(session, workspace_id),
            )
        audit = ContextMaterializationAudit(
            audit_id=uuid4(),
            package_id=manifest.package_id,
            workspace_id=workspace_id,
            consumer_type=ContextConsumerType.PLANNER,
            consumer_id=consumer_id,
            context_version=None,
            attempt=1,
            outcome=ContextMaterializationOutcome.SUCCEEDED,
            section_count=1,
            byte_count=128,
            input_token_count=32,
            failure_code=None,
            occurred_at=NOW,
        )
        package_repository.append_materialization_audit(
            audit, **_event_context(session, workspace_id)
        )
        session.commit()

    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(ContextPackageAcknowledgementModel).where(
                ContextPackageAcknowledgementModel.package_id == manifest.package_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(ContextPackageMaterializationAuditModel).where(
                ContextPackageMaterializationAuditModel.package_id == manifest.package_id
            )
        ) == 1
        package_repository = ContextPackageRepository(session, EventRepository(session))
        stored = package_repository.get(manifest.package_id, workspace_id)
        assert stored is not None
        assert stored.knowledge_refs[0].section_key == "overview"
        revoked = package_repository.revoke(
            manifest.package_id,
            workspace_id,
            expected_package_hash=manifest.package_hash,
            reason="operator_request",
            scope="new_starts",
            now=NOW + timedelta(minutes=1),
            **_event_context(session, workspace_id),
        )
        assert revoked.status is ContextPackageStatus.REVOKED
        assert revoked.revocation_scope == "new_starts"
        assert session.scalars(
            select(EventModel.aggregate_version)
            .where(EventModel.aggregate_id == manifest.package_id)
            .order_by(EventModel.aggregate_version.asc())
        ).all() == [1, 2, 3, 4]
        session.commit()

    with database.session() as session:
        stored = ContextPackageRepository(session).get(manifest.package_id, workspace_id)
        assert stored is not None
        assert stored.status is ContextPackageStatus.REVOKED
        assert stored.revoked_at == NOW + timedelta(minutes=1)


def test_package_repository_marks_corrupt_and_keeps_workspace_isolation(
    database, workspace_id: UUID
) -> None:
    request_id, revision_id, _, _ = _seed_context(database, workspace_id)
    consumer_id = uuid4()
    request = _request(
        workspace_id=workspace_id,
        revision_id=revision_id,
        consumer_id=consumer_id,
    )
    with database.session() as session:
        assembly = ContextAssemblyRepository(session, EventRepository(session))
        assembly.add(request)
        session.flush()
        claimed = assembly.claim_next(
            workspace_id=workspace_id,
            owner="context-worker",
            now=NOW,
            lease_ttl=timedelta(minutes=1),
            limit=1,
        )[0]
        manifest = _manifest(
            workspace_id=workspace_id,
            revision_id=revision_id,
            consumer_id=consumer_id,
            request_id=request_id,
        )
        assembly.complete_with_package(
            claimed,
            manifest,
            owner="context-worker",
            expected_version=claimed.version,
            **_event_context(session, workspace_id),
        )
        session.commit()

    with database.session() as session:
        repository = ContextPackageRepository(session)
        corrupt = repository.mark_corrupt(
            manifest.package_id,
            workspace_id,
            reason="context_hash_mismatch",
            now=NOW + timedelta(minutes=2),
            **_event_context(session, workspace_id),
        )
        assert corrupt.status is ContextPackageStatus.CORRUPT
        assert repository.get(manifest.package_id, UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")) is None
        session.commit()

    with database.session() as session:
        stored = ContextPackageRepository(session).get(manifest.package_id, workspace_id)
        assert stored is not None
        assert stored.status is ContextPackageStatus.CORRUPT
