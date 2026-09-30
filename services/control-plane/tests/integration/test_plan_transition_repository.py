from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import (
    PlanTransition,
    PlanTransitionStatus,
    WorkerTransition,
    WorkerTransitionStatus,
    WriterState,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.models import (
    PlanSnapshotModel,
    TaskModel,
    WorkerItemModel,
)
from sidera_control_plane.persistence.plan_transition_repository import (
    PlanEntityLineageRepository,
    PlanTransitionRepository,
    WorkerTransitionRepository,
)
from sidera_control_plane.persistence.planning_repository import (
    PlanningProfileRepository,
    PlanningRecordRepository,
    PlanningRequestRepository,
)
from tests.integration.test_planning_repository import (
    add_module_instance,
    make_profile,
    make_proposal,
    make_request,
)

pytestmark = pytest.mark.integration

ROOT_TASK_ID = UUID("43000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("43000000-0000-0000-0000-000000000002")
FIRST_SNAPSHOT_ID = UUID("43000000-0000-0000-0000-000000000003")
SECOND_SNAPSHOT_ID = UUID("43000000-0000-0000-0000-000000000004")
PLAN_TRANSITION_ID = UUID("43000000-0000-0000-0000-000000000005")
WORKER_TRANSITION_ID = UUID("43000000-0000-0000-0000-000000000006")
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _snapshot(
    workspace_id: UUID,
    *,
    snapshot_id: UUID,
    generation: int,
    parent_snapshot_id: UUID | None,
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="integration",
        source_ref=f"test:transition:{generation}",
        title="Transition repository test",
        goal_summary="Exercise transition persistence.",
        acceptance_refs=("transition:test",),
        work_items=(),
        worker_slots=(),
        milestones=(),
        dependencies=(),
        published_by="integration",
        published_at=NOW,
    )


def _seed_records(database, workspace_id: UUID) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    module_id = add_module_instance(database, workspace_id)
    profile = make_profile(workspace_id, module_id)
    request = make_request(workspace_id, profile)
    first = _snapshot(
        workspace_id,
        snapshot_id=FIRST_SNAPSHOT_ID,
        generation=1,
        parent_snapshot_id=None,
    )
    second = _snapshot(
        workspace_id,
        snapshot_id=SECOND_SNAPSHOT_ID,
        generation=2,
        parent_snapshot_id=first.id,
    )
    with database.session() as session:
        session.add(
            TaskModel(
                id=ROOT_TASK_ID,
                workspace_id=workspace_id,
                title="Transition root",
                description="Root for transition tests.",
                acceptance_criteria=["transition:test"],
                status="pending",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        session.add(
            WorkerItemModel(
                id=WORKER_ITEM_ID,
                workspace_id=workspace_id,
                root_task_id=ROOT_TASK_ID,
                stable_key="transition-worker",
                title="Transition worker",
                responsibility="Persist transition state.",
                status="active",
                context_version=1,
                acknowledged_context_version=1,
                derived_from_worker_item_ids=[],
                version=1,
            )
        )
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=first.id,
                schema_version=first.schema_version,
                workspace_id=first.workspace_id,
                root_task_id=first.root_task_id,
                generation=first.generation,
                parent_snapshot_id=first.parent_snapshot_id,
                source_type=first.source_type,
                source_ref=first.source_ref,
                title=first.title,
                goal_summary=first.goal_summary,
                payload=first.to_payload(),
                published_by=first.published_by,
                published_at=first.published_at,
            )
        )
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=second.id,
                schema_version=second.schema_version,
                workspace_id=second.workspace_id,
                root_task_id=second.root_task_id,
                generation=second.generation,
                parent_snapshot_id=second.parent_snapshot_id,
                source_type=second.source_type,
                source_ref=second.source_ref,
                title=second.title,
                goal_summary=second.goal_summary,
                payload=second.to_payload(),
                published_by=second.published_by,
                published_at=second.published_at,
            )
        )
        session.flush()
        PlanningProfileRepository(session).add(profile)
        PlanningRequestRepository(session).add(request)
        session.flush()
        proposal = make_proposal(request, revision=1, run_id=request.runs[0].id)
        PlanningRecordRepository(session).add_proposal(proposal, workspace_id)
        session.commit()
    return request.id, proposal.id, first.id, second.id, module_id


def _transition(
    workspace_id: UUID,
    request_id: UUID,
    proposal_id: UUID,
    first_snapshot_id: UUID,
    second_snapshot_id: UUID,
) -> PlanTransition:
    return PlanTransition(
        id=PLAN_TRANSITION_ID,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        planning_request_id=request_id,
        proposal_revision_id=proposal_id,
        from_snapshot_id=first_snapshot_id,
        from_generation=1,
        to_snapshot_id=second_snapshot_id,
        to_generation=2,
        status=PlanTransitionStatus.CONVERGING,
        started_at=NOW,
        completed_at=None,
        version=1,
    )


def _worker_transition(workspace_id: UUID) -> WorkerTransition:
    return WorkerTransition(
        id=WORKER_TRANSITION_ID,
        plan_transition_id=PLAN_TRANSITION_ID,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        from_subtask_id=None,
        to_subtask_id=None,
        action=WorkerTransitionAction.PRESERVE,
        fallback_action=None,
        target_context_version=1,
        source_execution_id=None,
        source_session_id=None,
        target_execution_id=None,
        target_session_id=None,
        writer_state=WriterState.FENCED,
        status=WorkerTransitionStatus.PENDING,
        failure_code=None,
        lease_id=None,
        fencing_token=0,
        attempt_count=0,
        next_attempt_at=None,
        started_at=None,
        completed_at=None,
        version=1,
    )


def test_transition_claims_use_skip_locked_and_round_trip_domain(
    database, workspace_id: UUID
) -> None:
    request_id, proposal_id, first_id, second_id, _ = _seed_records(
        database, workspace_id
    )
    transition = _transition(
        workspace_id, request_id, proposal_id, first_id, second_id
    )
    worker_transition = _worker_transition(workspace_id)
    with database.session() as session:
        PlanTransitionRepository(session).add(transition)
        session.flush()
        WorkerTransitionRepository(session).add(worker_transition)
        session.commit()

    with database.session() as first_session:
        first_repository = PlanTransitionRepository(first_session)
        claimed = first_repository.claim_converging(now=NOW, limit=1)
        assert [item.id for item in claimed] == [transition.id]
        with database.session() as second_session:
            assert PlanTransitionRepository(second_session).claim_converging(
                now=NOW, limit=1
            ) == ()
        first_session.rollback()

    with database.session() as session:
        loaded = PlanTransitionRepository(session).get(transition.id, workspace_id)
        worker_loaded = WorkerTransitionRepository(session).get(
            worker_transition.id, workspace_id
        )
        assert loaded == transition
        assert worker_loaded == worker_transition


def test_get_for_target_is_scoped_to_exact_revision_identity(
    database, workspace_id: UUID
) -> None:
    request_id, proposal_id, first_id, second_id, _ = _seed_records(
        database, workspace_id
    )
    transition = _transition(
        workspace_id, request_id, proposal_id, first_id, second_id
    )
    with database.session() as session:
        repository = PlanTransitionRepository(session)
        repository.add(transition)
        session.commit()

    with database.session() as session:
        repository = PlanTransitionRepository(session)
        exact = {
            "workspace_id": workspace_id,
            "root_task_id": ROOT_TASK_ID,
            "plan_snapshot_id": second_id,
            "plan_generation": 2,
        }
        assert repository.get_for_target(**exact) == transition
        assert repository.get_for_target(
            **{**exact, "workspace_id": uuid4()}
        ) is None
        assert repository.get_for_target(
            **{**exact, "root_task_id": uuid4()}
        ) is None
        assert repository.get_for_target(
            **{**exact, "plan_snapshot_id": first_id}
        ) is None
        assert repository.get_for_target(
            **{**exact, "plan_generation": 3}
        ) is None


def test_get_for_target_rejects_ambiguous_target_transitions(
    database, workspace_id: UUID
) -> None:
    request_id, proposal_id, first_id, second_id, module_id = _seed_records(
        database, workspace_id
    )
    second_profile = make_profile(workspace_id, module_id)
    second_request = make_request(workspace_id, second_profile)
    with database.session() as session:
        PlanningProfileRepository(session).add(second_profile)
        PlanningRequestRepository(session).add(second_request)
        session.flush()
        second_proposal = make_proposal(
            second_request,
            revision=1,
            run_id=second_request.runs[0].id,
        )
        PlanningRecordRepository(session).add_proposal(second_proposal, workspace_id)
        session.commit()

    transition = _transition(
        workspace_id, request_id, proposal_id, first_id, second_id
    )
    ambiguous_transition = replace(
        transition,
        id=uuid4(),
        planning_request_id=second_request.id,
        proposal_revision_id=second_proposal.id,
    )
    with database.session() as session:
        repository = PlanTransitionRepository(session)
        repository.add(transition)
        repository.add(ambiguous_transition)
        session.commit()

    with (
        database.session() as session,
        pytest.raises(ValidationError, match="ambiguous target plan transition"),
    ):
        PlanTransitionRepository(session).get_for_target(
            workspace_id=workspace_id,
            root_task_id=ROOT_TASK_ID,
            plan_snapshot_id=second_id,
            plan_generation=2,
        )


def test_worker_item_has_one_active_transition_and_lineage_is_workspace_scoped(
    database, workspace_id: UUID
) -> None:
    request_id, proposal_id, first_id, second_id, _ = _seed_records(
        database, workspace_id
    )
    transition = _transition(
        workspace_id, request_id, proposal_id, first_id, second_id
    )
    worker_transition = _worker_transition(workspace_id)
    with database.session() as session:
        transitions = PlanTransitionRepository(session)
        workers = WorkerTransitionRepository(session)
        lineages = PlanEntityLineageRepository(session)
        transitions.add(transition)
        session.flush()
        workers.add(worker_transition)
        lineage = lineages.add_mapping(
            plan_transition_id=transition.id,
            workspace_id=workspace_id,
            target_type="worker_item",
            target_id=WORKER_ITEM_ID,
            source_type="worker_item",
            source_id=WORKER_ITEM_ID,
            reason="preserve stable workspace",
        )
        session.commit()
        assert workers.get_active_for_worker_item(WORKER_ITEM_ID, workspace_id) == worker_transition
        assert lineages.get(lineage.id, workspace_id) == lineage
        assert lineages.list_for_transition(transition.id, workspace_id) == (lineage,)
