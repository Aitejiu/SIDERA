from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.work_context import (
    AcceptedDecision,
    ContextTextItem,
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
    WorkReference,
)
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningBriefRevisionModel,
    PlanningProfileModel,
    PlanningRequestModel,
    TaskModel,
    WorkContextRevisionModel,
)
from sidera_control_plane.persistence.work_context_repository import (
    WorkContextRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _seed_request(database, workspace_id: UUID) -> UUID:
    definition_id = uuid4()
    module_id = uuid4()
    profile_id = uuid4()
    request_id = uuid4()
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
                instance_name=f"work-context-{module_id.hex[:8]}",
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
                profile_key=f"work-context-{profile_id.hex[:8]}",
                version=1,
                display_name="Work context test profile",
                planner_module_instance_id=module_id,
                planner_module_config_version=1,
                planner_interface_version="sidera.planner.v1",
                status="active",
                user_selectable=True,
                policy={},
            )
        )
        session.add(
            PlanningRequestModel(
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
                source_ref="test:work-context",
                actor="integration",
                correlation_id=uuid4(),
                version=1,
            )
        )
        session.flush()
        session.add(
            PlanningBriefRevisionModel(
                planning_request_id=request_id,
                revision=1,
                workspace_id=workspace_id,
                payload={"objective": "Persist work context"},
                created_by="integration",
                created_at=NOW,
            )
        )
        session.commit()
    return request_id


def _snapshot(seed: str) -> WorkContextSnapshotV1:
    return WorkContextSnapshotV1.create(
        objective=f"Persist work context {seed}",
        constraints=(
            ContextTextItem(key="tenant", text="Keep workspace scoped", ordinal=0),
        ),
        acceptance_criteria=(
            ContextTextItem(key="round_trip", text="Round trip all facts", ordinal=0),
        ),
        requested_outputs=(
            ContextTextItem(key="revision", text="A durable revision", ordinal=0),
        ),
        clarification_decisions=(),
        accepted_decisions=(
            AcceptedDecision(
                key="immutable",
                statement="Never mutate an old revision",
                source="integration",
                ordinal=0,
            ),
        ),
        unresolved_questions=(),
        knowledge_refs=(),
        artifact_refs=(
            WorkReference(ref_id=uuid4(), ref_type="artifact", ordinal=0),
        ),
        evidence_refs=(),
        checkpoint_refs=(),
    )


def _revision(
    *,
    context_id: UUID,
    workspace_id: UUID,
    planning_request_id: UUID,
    number: int,
    reason: WorkContextRevisionReason,
    seed: str,
) -> WorkContextRevision:
    return WorkContextRevision.create(
        revision_id=uuid4(),
        work_context_id=context_id,
        workspace_id=workspace_id,
        revision_number=number,
        brief_planning_request_id=planning_request_id,
        brief_revision=1,
        proposal_revision_id=None,
        plan_snapshot_id=None,
        reason=reason,
        snapshot=_snapshot(seed),
        created_by="integration",
        created_at=NOW,
    )


def _aggregate(workspace_id: UUID, planning_request_id: UUID) -> tuple[WorkContext, WorkContextRevision]:
    context_id = uuid4()
    revision = _revision(
        context_id=context_id,
        workspace_id=workspace_id,
        planning_request_id=planning_request_id,
        number=1,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        seed="one",
    )
    return (
        WorkContext.create(
            work_context_id=context_id,
            workspace_id=workspace_id,
            planning_request_id=planning_request_id,
            first_revision=revision,
            created_at=NOW,
        ),
        revision,
    )


def test_work_context_repository_round_trips_and_is_workspace_scoped(
    database, workspace_id: UUID
) -> None:
    request_id = _seed_request(database, workspace_id)
    context, revision = _aggregate(workspace_id, request_id)

    with database.session() as session:
        repository = WorkContextRepository(session)
        repository.add(context, revision)
        session.commit()

    with database.session() as session:
        repository = WorkContextRepository(session)
        stored = repository.get(context.id, workspace_id, for_update=True)
        assert stored is not None
        assert stored.id == context.id
        assert stored.current_revision_id == revision.revision_id
        assert stored.current_revision_number == 1
        assert repository.get_by_planning_request(request_id, workspace_id) is not None
        assert repository.get(context.id, UUID("00000000-0000-0000-0000-000000000099")) is None
        assert session.scalar(
            select(func.count()).select_from(WorkContextRevisionModel).where(
                WorkContextRevisionModel.work_context_id == context.id
            )
        ) == 1
        loaded_revision = repository.get_revision(revision.revision_id, workspace_id)
        assert loaded_revision is not None
        assert loaded_revision.snapshot.content_hash == revision.snapshot.content_hash


def test_work_context_repository_appends_without_mutating_old_revision(
    database, workspace_id: UUID
) -> None:
    request_id = _seed_request(database, workspace_id)
    context, first = _aggregate(workspace_id, request_id)
    with database.session() as session:
        WorkContextRepository(session).add(context, first)
        session.commit()

    second = _revision(
        context_id=context.id,
        workspace_id=workspace_id,
        planning_request_id=request_id,
        number=2,
        reason=WorkContextRevisionReason.CLARIFICATION_ACCEPTED,
        seed="two",
    )
    with database.session() as session:
        repository = WorkContextRepository(session)
        stored = repository.get(context.id, workspace_id, for_update=True)
        assert stored is not None
        stored.append_revision(second, expected_version=1, now=NOW)
        repository.append_revision(stored, second, expected_version=1)
        session.commit()

    with database.session() as session:
        rows = session.scalars(
            select(WorkContextRevisionModel)
            .where(WorkContextRevisionModel.work_context_id == context.id)
            .order_by(WorkContextRevisionModel.revision_number)
        ).all()
        assert [row.revision_number for row in rows] == [1, 2]
        assert rows[0].content_hash == first.content_hash
        current = WorkContextRepository(session).get(context.id, workspace_id)
        assert current is not None
        assert current.version == 2
        assert current.current_revision_id == second.revision_id


def test_stale_revision_transition_rolls_back_new_revision(
    database, workspace_id: UUID
) -> None:
    request_id = _seed_request(database, workspace_id)
    context, first = _aggregate(workspace_id, request_id)
    with database.session() as session:
        WorkContextRepository(session).add(context, first)
        session.commit()

    second = _revision(
        context_id=context.id,
        workspace_id=workspace_id,
        planning_request_id=request_id,
        number=2,
        reason=WorkContextRevisionReason.REPLANNED,
        seed="stale",
    )
    with database.session() as session:
        repository = WorkContextRepository(session)
        stale = repository.get(context.id, workspace_id)
        assert stale is not None
        stale.append_revision(second, expected_version=1, now=NOW)
        repository.append_revision(stale, second, expected_version=1)
        session.commit()

    third = _revision(
        context_id=context.id,
        workspace_id=workspace_id,
        planning_request_id=request_id,
        number=3,
        reason=WorkContextRevisionReason.USER_INTERVENTION,
        seed="rejected",
    )
    with database.session() as session:
        repository = WorkContextRepository(session)
        stale_again = repository.get(context.id, workspace_id)
        assert stale_again is not None
        stale_again.version = 1
        stale_again.current_revision_number = 2
        stale_again.current_revision_id = second.revision_id
        with pytest.raises(VersionConflict):
            repository.append_revision(stale_again, third, expected_version=1)
        session.rollback()

    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(WorkContextRevisionModel).where(
                WorkContextRevisionModel.work_context_id == context.id,
                WorkContextRevisionModel.revision_number == 3,
            )
        ) == 0


def test_bind_root_task_persists_revision_and_pointer_together(
    database, workspace_id: UUID
) -> None:
    request_id = _seed_request(database, workspace_id)
    context, first = _aggregate(workspace_id, request_id)
    root_task_id = uuid4()
    with database.session() as session:
        session.add(
            TaskModel(
                id=root_task_id,
                workspace_id=workspace_id,
                title="Root",
                description="Materialized root",
                acceptance_criteria=["done"],
                status="pending",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        WorkContextRepository(session).add(context, first)
        session.commit()

    second = _revision(
        context_id=context.id,
        workspace_id=workspace_id,
        planning_request_id=request_id,
        number=2,
        reason=WorkContextRevisionReason.PROPOSAL_CONFIRMED,
        seed="bound",
    )
    with database.session() as session:
        repository = WorkContextRepository(session)
        stored = repository.get(context.id, workspace_id, for_update=True)
        assert stored is not None
        repository.bind_root_task(stored, root_task_id, second, expected_version=1)
        session.commit()

    with database.session() as session:
        stored = WorkContextRepository(session).get(context.id, workspace_id)
        assert stored is not None
        assert stored.root_task_id == root_task_id
        assert stored.current_revision_id == second.revision_id
        assert stored.version == 2
