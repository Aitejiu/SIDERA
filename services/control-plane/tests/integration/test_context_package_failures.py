from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sidera_control_plane.application.context_materialization import (
    ContextMaterializationError,
    ContextMaterializer,
    build_materialization_audit,
)
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactSectionBody,
    ExactSectionRef,
    SearchHit,
)
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextBudgetV1,
    ContextConsumerType,
    ContextMaterializationMode,
    ContextMaterializationOutcome,
    ContextPackageKnowledgeRef,
    ContextPackageManifestV1,
    ContextPurpose,
    ContextSelectionReason,
    ContextTrustLevel,
)
from sidera_control_plane.domain.work_context import (
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
    ContextPackageMaterializationAuditModel,
    EventModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
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


class EmptyMaterializationCatalog:
    def search(
        self,
        workspace_id: UUID,
        query: str,
        *,
        tags: Sequence[str] = (),
        source_types: Sequence[str] = (),
        status: str | None = "active",
        limit=20,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[SearchHit, ...]:
        raise NotImplementedError

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        raise NotImplementedError

    def materialize(
        self,
        workspace_id: UUID,
        sections: Sequence[ExactSectionRef],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        return ()


def _snapshot() -> WorkContextSnapshotV1:
    return WorkContextSnapshotV1.create(
        objective="Verify durable context failure handling",
        constraints=(),
        acceptance_criteria=(),
        requested_outputs=(),
        clarification_decisions=(),
        accepted_decisions=(),
        unresolved_questions=(),
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        checkpoint_refs=(),
    )


def _seed_context(database, workspace_id: UUID) -> tuple[UUID, UUID]:
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
                instance_name=f"context-failure-{module_id.hex[:8]}",
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
                profile_key=f"context-failure-{profile_id.hex[:8]}",
                version=1,
                display_name="Context failure test profile",
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
                source_ref="test:context-package-failures",
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
                payload={"objective": "Verify durable context failure handling"},
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
    return request_id, revision_id


def _request(workspace_id: UUID, revision_id: UUID, consumer_id: UUID) -> ContextAssemblyRequest:
    return ContextAssemblyRequest.create(
        request_id=uuid4(),
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
        request_fingerprint="a" * 64,
        idempotency_key="context-failure-package",
        created_at=NOW,
    )


def _manifest(workspace_id: UUID, revision_id: UUID, consumer_id: UUID) -> ContextPackageManifestV1:
    reference = ContextPackageKnowledgeRef(
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
        control_plane_fact_snapshot={"test": "failure"},
        knowledge_refs=(reference,),
        artifact_refs=(),
        evidence_refs=(),
        budget=ContextBudgetV1(
            profile_id=uuid4(),
            profile_version=1,
            max_sections=8,
            max_chars=20_000,
            max_input_tokens=4_000,
            actual_sections=1,
            actual_chars=12,
            actual_input_tokens=3,
        ),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="context-failure-package",
        created_at=NOW,
    )


def _event_context(session: Session, workspace_id: UUID) -> EventContext:
    command_id = uuid4()
    correlation_id = uuid4()
    session.add(
        CommandModel(
            id=command_id,
            workspace_id=workspace_id,
            command_type="MaterializeContextPackage",
            actor_type="system",
            actor_id="context-failure-test",
            target_type="workspace",
            target_id=None,
            payload={},
            expected_version=None,
            idempotency_key=f"context-failure-command-{command_id}",
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
        "actor_id": "context-failure-test",
        "causation_id": command_id,
        "correlation_id": correlation_id,
    }


def test_materialization_failure_marks_corrupt_and_persists_no_body(
    database, workspace_id: UUID
) -> None:
    _, revision_id = _seed_context(database, workspace_id)
    consumer_id = uuid4()
    request = _request(workspace_id, revision_id, consumer_id)
    manifest = _manifest(workspace_id, revision_id, consumer_id)

    with database.session() as session:
        assembly = ContextAssemblyRepository(session, EventRepository(session))
        assembly.add(request)
        session.flush()
        claimed = assembly.claim_next(
            workspace_id=workspace_id,
            owner="context-failure-worker",
            now=NOW,
            lease_ttl=timedelta(minutes=1),
            limit=1,
        )[0]
        assembly.complete_with_package(
            claimed,
            manifest,
            owner="context-failure-worker",
            expected_version=claimed.version,
            **_event_context(session, workspace_id),
        )
        session.commit()

    with database.session() as session:
        revision = WorkContextRepository(session).get_revision(revision_id, workspace_id)
        stored = ContextPackageRepository(session).get(manifest.package_id, workspace_id)
        assert revision is not None
        assert stored is not None
        materializer = ContextMaterializer(EmptyMaterializationCatalog())
        with pytest.raises(ContextMaterializationError) as error_info:
            materializer.materialize(
                stored,
                revision,
                mode=ContextMaterializationMode.CONSUMER_START,
                workspace_id=workspace_id,
                consumer_id=consumer_id,
                purpose=ContextPurpose.PLANNING_RUN,
                context_version=None,
                expected_package_hash=stored.package_hash,
                now=NOW,
                correlation_id=uuid4(),
                caller_request_id=uuid4(),
            )
        assert error_info.value.code == "context_hash_mismatch"
        assert error_info.value.corrupt is True

        package_repository = ContextPackageRepository(session, EventRepository(session))
        package_repository.mark_corrupt(
            stored.package_id,
            workspace_id,
            reason=error_info.value.code,
            now=NOW + timedelta(minutes=1),
            **_event_context(session, workspace_id),
        )
        audit = build_materialization_audit(
            stored,
            attempt=1,
            outcome=ContextMaterializationOutcome.FAILED,
            occurred_at=NOW + timedelta(minutes=1),
            failure_code=error_info.value.code,
        )
        package_repository.append_materialization_audit(
            audit,
            **_event_context(session, workspace_id),
        )
        session.commit()

    with database.session() as session:
        stored = ContextPackageRepository(session).get(manifest.package_id, workspace_id)
        audit = session.scalar(
            select(ContextPackageMaterializationAuditModel).where(
                ContextPackageMaterializationAuditModel.package_id == manifest.package_id
            )
        )
        events = session.scalars(
            select(EventModel)
            .where(EventModel.aggregate_id == manifest.package_id)
            .order_by(EventModel.aggregate_version.asc())
        ).all()
        assert stored is not None
        assert stored.status.value == "corrupt"
        assert audit is not None
        assert (audit.outcome, audit.section_count, audit.byte_count) == (
            "failed",
            0,
            0,
        )
        assert all("body" not in str(event.payload).lower() for event in events)
