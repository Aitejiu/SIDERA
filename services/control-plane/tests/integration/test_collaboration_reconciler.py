import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_reconciler import (
    CollaborationReconciler,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    CollaborationRuntimeResolver,
    RegistryCollaborationRuntimeFactory,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyEntry,
    CollaborationPolicySnapshotV1,
    CollaborationStrategy,
)
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)
from sidera_control_plane.domain.plan_revisions import (
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plan_transitions import (
    PlanTransition,
    PlanTransitionStatus,
    WorkerTransition,
    WorkerTransitionStatus,
    WriterState,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.models import (
    CollaborationEvaluationModel,
    CollaborationRunModel,
    EventModel,
    ExecutionModel,
    ExecutionRequestModel,
    ModuleDefinitionModel,
    PlanningBriefRevisionModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunModel,
    PlanSnapshotModel,
    PlanTransitionModel,
    RootTaskPlanHeadModel,
    RuntimeDecisionModel,
    RuntimeReleaseModel,
    WorkerTransitionModel,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_to_payload,
)

from .test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _capability() -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime.v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=(CollaborationStrategy.ALL_REQUIRED.value,),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.STABLE,
    )


def _policy(definition_id: UUID, instance_id: UUID) -> CollaborationPolicySnapshotV1:
    return CollaborationPolicySnapshotV1(
        schema_version=1,
        runtime_definition_id=definition_id,
        runtime_instance_id=instance_id,
        runtime_interface_version="collaboration-runtime.v1",
        runtime_config_version=1,
        runtime_capability_snapshot=_capability(),
        default_policy=CollaborationPolicyEntry(
            enabled=True,
            strategy_id=CollaborationStrategy.ALL_REQUIRED,
        ),
        subtask_overrides=(),
    )


def _snapshot(
    *,
    workspace_id: UUID,
    root_task_id: UUID,
    snapshot_id: UUID,
    definition_id: UUID,
    instance_id: UUID,
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=3,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=1,
        parent_snapshot_id=None,
        source_type="integration",
        source_ref="collaboration-reconciler",
        title="Collaboration reconciler plan",
        goal_summary="Prepare a durable collaboration evaluation",
        acceptance_refs=("acceptance:test",),
        published_by="integration",
        published_at=NOW,
        collaboration_policy=_policy(definition_id, instance_id),
    )


def test_reconciler_persists_one_pinned_run_and_evaluation(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        snapshot_id=ids["plan"],
        definition_id=ids["module_definition"],
        instance_id=ids["module_instance"],
    )
    snapshot_hash = plan_snapshot_content_hash(snapshot)
    capability_payload = capability_profile_to_payload(_capability())

    with database.session() as session:
        session.add(request_row(ids, request_id=ids["request"]))
        session.flush()
        session.execute(
            update(ModuleDefinitionModel)
            .where(
                ModuleDefinitionModel.id == ids["module_definition"],
                ModuleDefinitionModel.workspace_id == workspace_id,
            )
            .values(
                module_type="collaboration_runtime",
                provider="reference",
                interface_version="collaboration-runtime.v1",
                capabilities=capability_payload,
            )
        )
        session.execute(
            update(PlanSnapshotModel)
            .where(
                PlanSnapshotModel.id == snapshot.id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
            .values(
                schema_version=snapshot.schema_version,
                root_task_id=snapshot.root_task_id,
                generation=snapshot.generation,
                parent_snapshot_id=snapshot.parent_snapshot_id,
                source_type=snapshot.source_type,
                source_ref=snapshot.source_ref,
                title=snapshot.title,
                goal_summary=snapshot.goal_summary,
                payload=snapshot.to_payload(),
                published_by=snapshot.published_by,
                published_at=snapshot.published_at,
            )
        )
        session.add(
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=snapshot.root_task_id,
                active_snapshot_id=snapshot.id,
                active_generation=snapshot.generation,
                active_snapshot_hash=snapshot_hash,
                version=1,
                updated_at=NOW,
            )
        )
        session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.plan_snapshot_id == snapshot.id,
            )
            .values(
                collaboration_gate_mode="release_required",
                gate_reason="collaboration_run_pending",
                gate_details={"source": "integration"},
            )
        )
        session.commit()

    with uow_factory() as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_runtime",
            resource_id=snapshot.root_task_id,
            change_type="plan.published",
            resource_version=1,
            correlation_id=uuid4(),
        )

    reconciler = CollaborationReconciler(
        uow_factory=uow_factory,
        fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
            session=uow.session
        ),
        runtime_resolver_factory=lambda modules: CollaborationRuntimeResolver(
            modules,
            RegistryCollaborationRuntimeFactory(
                {
                    "reference": lambda _instance: ReferenceCollaborationRuntime(
                        clock=lambda: NOW
                    )
                }
            ),
        ),
    )
    prepared = reconciler.prepare_next(
        owner_id="collaboration-integration",
        now=NOW,
        workspace_id=workspace_id,
        root_task_id=snapshot.root_task_id,
    )

    assert prepared is not None
    assert prepared.run.plan_snapshot_id == snapshot.id
    assert prepared.run.plan_generation == snapshot.generation
    assert prepared.evaluation.operation_id == prepared.operation_id
    assert prepared.evaluation.lease_owner == "collaboration-integration"

    repeated = reconciler.prepare_next(
        owner_id="collaboration-integration-2",
        now=NOW,
        workspace_id=workspace_id,
        root_task_id=snapshot.root_task_id,
    )
    assert repeated is None

    with database.session() as session:
        runs = session.scalars(
            select(CollaborationRunModel).where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.root_task_id == snapshot.root_task_id,
            )
        ).all()
        evaluations = session.scalars(
            select(CollaborationEvaluationModel).where(
                CollaborationEvaluationModel.workspace_id == workspace_id,
                CollaborationEvaluationModel.run_id == prepared.run.id,
            )
        ).all()

    assert len(runs) == 1
    assert runs[0].plan_snapshot_id == snapshot.id
    assert runs[0].plan_generation == snapshot.generation
    assert len(evaluations) == 1
    assert evaluations[0].operation_id == prepared.operation_id
    assert evaluations[0].lease_owner == "collaboration-integration"


def test_revision_run_waits_for_late_request_then_completes_after_convergence(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    from sidera_control_plane.application.activation_reconciler import (
        ActivationReconciler,
    )
    from sidera_control_plane.application.collaboration_runtime_invoker import (
        RuntimeInvocationResult,
    )
    from sidera_control_plane.application.plan_transition_coordinator import (
        PlanTransitionCoordinator,
    )
    from sidera_control_plane.application.worker_item_materializer import (
        WorkerItemMaterializer,
    )
    from sidera_control_plane.persistence.planning_repository import (
        _diff_to_mapping,
        _snapshot_to_mapping,
    )
    from tests.integration.test_collaboration_decision_application import _service
    from tests.integration.test_collaboration_evaluation_recovery import (
        _invoker,
        _prepare_fixture,
    )
    from tests.integration.test_planning_repository import (
        capabilities,
        make_profile,
        make_proposal,
    )
    from tests.integration.test_r5_1_execution_request_schema import request_row
    from tests.integration.test_revision_confirmation import _base_plan
    from tests.integration.test_worker_transition_continuation import (
        _seed_work_context,
    )

    ids, first_snapshot, reconciler = _prepare_fixture(
        database, workspace_id, uow_factory
    )
    base_plan = _base_plan(workspace_id)
    first_snapshot = replace(
        first_snapshot,
        subtasks=(
            replace(base_plan.subtasks[0], task_id=ids["subtask"]),
        ),
        worker_item_plans=(
            replace(
                base_plan.worker_item_plans[0],
                worker_item_id=ids["worker_item"],
                title="Backend",
                responsibility="Implement backend",
            ),
        ),
        subtask_milestones=base_plan.subtask_milestones,
    )
    with database.session() as session:
        session.execute(
            update(PlanSnapshotModel)
            .where(
                PlanSnapshotModel.id == first_snapshot.id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
            .values(payload=first_snapshot.to_payload())
        )
        session.execute(
            update(RootTaskPlanHeadModel)
            .where(
                RootTaskPlanHeadModel.root_task_id == first_snapshot.root_task_id,
                RootTaskPlanHeadModel.workspace_id == workspace_id,
            )
            .values(active_snapshot_hash=plan_snapshot_content_hash(first_snapshot))
        )
        session.commit()
    first = reconciler.prepare_next(
        owner_id="revision-collaboration",
        now=NOW,
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
    )
    assert first is not None
    with uow_factory() as uow:
        WorkerItemMaterializer().materialize(uow, first_snapshot, now=NOW)

    planning_profile = replace(
        make_profile(workspace_id, ids["module_instance"]),
        id=ids["profile"],
        profile_key="default",
        display_name="Default planner",
    )
    profile_snapshot = _snapshot_to_mapping(
        planning_profile.snapshot(capabilities())
    )

    with database.session() as session:
        session.execute(
            update(PlanningRequestModel)
            .where(
                PlanningRequestModel.id == ids["request"],
                PlanningRequestModel.workspace_id == workspace_id,
            )
            .values(
                profile_snapshot=profile_snapshot,
                current_brief_revision=1,
            )
        )
        session.execute(
            update(PlanningRunModel)
            .where(
                PlanningRunModel.id == ids["run"],
                PlanningRunModel.workspace_id == workspace_id,
            )
            .values(
                usage={
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_microunits": 0,
                    "runtime_seconds": 0,
                    "context_bytes": 0,
                }
            )
        )
        session.execute(
            update(PlanningBriefRevisionModel)
            .where(
                PlanningBriefRevisionModel.planning_request_id == ids["request"],
                PlanningBriefRevisionModel.workspace_id == workspace_id,
                PlanningBriefRevisionModel.revision == 1,
            )
            .values(
                payload={
                    "objective": "Exercise the revision collaboration lifecycle.",
                    "constraints": [],
                    "acceptance_criteria": ["Accept the revision lifecycle fixture."],
                    "context_refs": [],
                    "requested_outputs": ["revision lifecycle test"],
                    "user_feedback": None,
                    "source_correlation": str(uuid4()),
                }
            )
        )
        session.commit()

    with uow_factory() as uow:
        planning_request = uow.planning_requests.get(ids["request"], workspace_id)
        assert planning_request is not None
        proposal = replace(
            make_proposal(
                planning_request,
                revision=1,
                run_id=ids["run"],
            ),
            id=ids["proposal"],
        )
    proposal_mapping = proposal.payload.to_mapping()
    plan_template = {
        key: value
        for key, value in proposal_mapping.items()
        if key
        not in {
            "schema_version",
            "kind",
            "decision_summary",
            "impact_policy",
            "execution_intents",
        }
    }
    with database.session() as session:
        session.execute(
            update(PlanningProposalRevisionModel)
            .where(
                PlanningProposalRevisionModel.id == ids["proposal"],
                PlanningProposalRevisionModel.workspace_id == workspace_id,
            )
            .values(
                proposal_schema_version=proposal.proposal_schema_version,
                kind=proposal.kind.value,
                profile_id=proposal.profile_descriptor.profile_id,
                profile_version=proposal.profile_descriptor.profile_version,
                planner_module_instance_id=(
                    proposal.profile_descriptor.planner_module_instance_id
                ),
                planner_module_config_version=(
                    proposal.profile_descriptor.planner_module_config_version
                ),
                decision_summary=proposal_mapping["decision_summary"],
                plan_template=plan_template,
                execution_intents=proposal_mapping["execution_intents"],
                impact_policy=proposal_mapping["impact_policy"],
                derived_diff=_diff_to_mapping(proposal.derived_diff),
                canonical_payload=json.loads(proposal.canonical_payload),
                content_hash=proposal.content_hash,
            )
        )
        session.execute(
            update(PlanningRequestModel)
            .where(
                PlanningRequestModel.id == ids["request"],
                PlanningRequestModel.workspace_id == workspace_id,
            )
            .values(
                current_proposal_revision=proposal.revision,
                current_proposal_content_hash=proposal.content_hash,
            )
        )
        session.commit()

    _seed_work_context(uow_factory, ids["request"], workspace_id)
    with uow_factory() as uow:
        planning_request = uow.planning_requests.get(
            ids["request"], workspace_id, for_update=True
        )
        assert planning_request is not None
        previous_version = planning_request.version
        planning_request.materialize(
            proposal_revision=proposal.revision,
            content_hash=proposal.content_hash,
            now=NOW,
        )
        uow.planning_requests.save(planning_request, previous_version)

    revised_snapshot = replace(
        first_snapshot,
        id=uuid4(),
        generation=2,
        parent_snapshot_id=first_snapshot.id,
        published_at=NOW + timedelta(seconds=1),
    )
    plan_transition_id = uuid4()
    plan_transition = PlanTransition(
        id=plan_transition_id,
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
        planning_request_id=ids["request"],
        proposal_revision_id=ids["proposal"],
        from_snapshot_id=first_snapshot.id,
        from_generation=1,
        to_snapshot_id=revised_snapshot.id,
        to_generation=2,
        status=PlanTransitionStatus.CONVERGING,
        started_at=NOW + timedelta(seconds=1),
        completed_at=None,
        version=1,
    )
    pending_worker_item_id = uuid4()
    pending_worker_item = WorkerItem.create(
        item_id=pending_worker_item_id,
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
        title="Pending revision branch",
        responsibility="Keep the revision producer open during the test.",
        now=NOW + timedelta(seconds=1),
    )
    pending_worker_transition = WorkerTransition(
        id=uuid4(),
        plan_transition_id=plan_transition_id,
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
        worker_item_id=pending_worker_item_id,
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
    with uow_factory() as uow:
        head = uow.plans.get_head(
            first_snapshot.root_task_id, workspace_id, for_update=True
        )
        assert head is not None
        uow.plans.add_snapshot_and_advance_head(
            revised_snapshot, expected_head=head
        )
        WorkerItemMaterializer().materialize(
            uow, revised_snapshot, now=NOW + timedelta(seconds=1)
        )
        uow.worker_items.add(pending_worker_item)
        uow.plan_transitions.add(plan_transition)
        uow.worker_transitions.add(pending_worker_transition)
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_runtime",
            resource_id=first_snapshot.root_task_id,
            change_type="plan.revision.confirmed",
            resource_version=head.version + 1,
            correlation_id=plan_transition_id,
        )

    waiting = reconciler.prepare_next(
        owner_id="revision-collaboration",
        now=NOW + timedelta(seconds=2),
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
    )
    assert waiting is None
    with database.session() as session:
        revision_run = session.scalar(
            select(CollaborationRunModel).where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.root_task_id == first_snapshot.root_task_id,
                CollaborationRunModel.plan_snapshot_id == revised_snapshot.id,
            )
        )
        assert revision_run is not None
        assert revision_run.status == "active"
        assert session.scalar(
            select(CollaborationEvaluationModel.id).where(
                CollaborationEvaluationModel.workspace_id == workspace_id,
                CollaborationEvaluationModel.run_id == revision_run.id,
            )
        ) is None
        assert session.scalar(
            select(EventModel.id).where(
                EventModel.workspace_id == workspace_id,
                EventModel.aggregate_id == revision_run.id,
                EventModel.event_type == "CollaborationRunCompleted",
            )
        ) is None
        assert session.scalar(
            select(ExecutionRequestModel.id).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.plan_snapshot_id == revised_snapshot.id,
            )
        ) is None

    waiting_again = reconciler.prepare_next(
        owner_id="revision-collaboration-retry",
        now=NOW + timedelta(seconds=3),
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
    )
    assert waiting_again is None

    revision_request_id = uuid4()
    revision_request = request_row(
        ids,
        request_id=revision_request_id,
        worker_key="backend-revision-2",
    )
    revision_request.plan_snapshot_id = revised_snapshot.id
    revision_request.plan_generation = 2
    revision_request.collaboration_gate_mode = "release_required"
    revision_request.gate_reason = "collaboration_run_pending"
    revision_request.gate_details = {"source": "plan_transition"}
    revision_request.gate_evaluated_at = NOW + timedelta(seconds=4)
    revision_request.status_changed_at = NOW + timedelta(seconds=4)
    with database.session() as session:
        session.add(revision_request)
        session.commit()
    with uow_factory() as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_runtime",
            resource_id=first_snapshot.root_task_id,
            change_type="ExecutionRequestCreatedByPlanTransition",
            resource_version=revision_request.version,
            correlation_id=plan_transition_id,
        )

    prepared = reconciler.prepare_next(
        owner_id="revision-collaboration",
        now=NOW + timedelta(seconds=5),
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
    )
    assert prepared is not None
    assert prepared.run.id == revision_run.id
    assert prepared.run.plan_generation == 2
    assert str(revision_request_id) in {
        str(item["id"]) for item in prepared.projection.execution_requests
    }

    invocation = _invoker(
        ReferenceCollaborationRuntime(clock=lambda: NOW + timedelta(seconds=6))
    ).invoke(prepared, now=NOW + timedelta(seconds=6))
    assert isinstance(invocation, RuntimeInvocationResult)
    decision = _service(uow_factory).apply(
        prepared, invocation, now=NOW + timedelta(seconds=6)
    )
    assert decision.status == "applied"
    assert len(decision.release_ids) == 1
    with database.session() as session:
        released_request = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == revision_request_id,
            )
        )
        assert released_request is not None
        assert released_request.status == "ready"
        assert released_request.gate_reason == "ready"

    with uow_factory() as uow:
        task = uow.tasks.get(ids["subtask"], workspace_id, for_update=True)
        assert task is not None
        previous_version = task.version
        task.mark_ready()
        uow.tasks.save(task, previous_version)

    activation = ActivationReconciler(
        uow_factory=uow_factory,
        owner_id="revision-collaboration-activation",
        clock=lambda: NOW + timedelta(seconds=7),
    ).run_once(limit=10)
    with uow_factory() as uow:
        activation_gate_states = tuple(
            (
                request.id,
                request.status.value,
                request.gate_reason,
                dict(request.gate_details),
            )
            for request in uow.execution_requests.list_for_root(
                first_snapshot.root_task_id, workspace_id
            )
        )
    assert activation.materialized == 1, (
        f"activation={activation!r}; gates={activation_gate_states!r}"
    )
    assert activation.failed == 0
    execution_id = activation.execution_ids[0]
    with uow_factory() as uow:
        execution = uow.executions.get(execution_id, workspace_id, for_update=True)
        assert execution is not None
        previous_version = execution.version
        execution.cancel(now=NOW + timedelta(seconds=8))
        uow.executions.save(execution, previous_version)

    before_convergence = reconciler.prepare_next(
        owner_id="revision-collaboration-before-convergence",
        now=NOW + timedelta(seconds=9),
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
    )
    assert before_convergence is None
    with database.session() as session:
        assert session.scalar(
            select(CollaborationRunModel.status).where(
                CollaborationRunModel.id == revision_run.id,
                CollaborationRunModel.workspace_id == workspace_id,
            )
        ) == "active"

    coordinator = PlanTransitionCoordinator(
        uow_factory=uow_factory,
        owner_id="revision-collaboration-transition",
        clock=lambda: NOW + timedelta(seconds=10),
    )
    transition_result = coordinator.run_once(limit=10)
    assert transition_result.failed == 0
    assert transition_result.converged == 1
    with database.session() as session:
        assert session.scalar(
            select(PlanTransitionModel.status).where(
                PlanTransitionModel.id == plan_transition_id,
                PlanTransitionModel.workspace_id == workspace_id,
            )
        ) == PlanTransitionStatus.CONVERGED.value
        assert session.scalar(
            select(WorkerTransitionModel.status).where(
                WorkerTransitionModel.id == pending_worker_transition.id,
                WorkerTransitionModel.workspace_id == workspace_id,
            )
        ) == WorkerTransitionStatus.CONVERGED.value
        assert session.scalar(
            select(ExecutionModel.status).where(
                ExecutionModel.id == execution_id,
                ExecutionModel.workspace_id == workspace_id,
            )
        ) == "canceled"

    completed = reconciler.prepare_next(
        owner_id="revision-collaboration-after-convergence",
        now=NOW + timedelta(seconds=11),
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
    )
    assert completed is None
    repeated = reconciler.prepare_next(
        owner_id="revision-collaboration-repeat",
        now=NOW + timedelta(seconds=12),
        workspace_id=workspace_id,
        root_task_id=first_snapshot.root_task_id,
    )
    assert repeated is None

    with database.session() as session:
        assert session.scalar(
            select(CollaborationRunModel.status).where(
                CollaborationRunModel.id == revision_run.id,
                CollaborationRunModel.workspace_id == workspace_id,
            )
        ) == "completed"
        assert session.scalar(
            select(CollaborationEvaluationModel.id).where(
                CollaborationEvaluationModel.workspace_id == workspace_id,
                CollaborationEvaluationModel.run_id == revision_run.id,
            )
        ) == prepared.evaluation.id
        decisions = session.scalars(
            select(RuntimeDecisionModel).where(
                RuntimeDecisionModel.workspace_id == workspace_id,
                RuntimeDecisionModel.run_id == revision_run.id,
            )
        ).all()
        assert len(decisions) == 1
        assert decisions[0].evaluation_id == prepared.evaluation.id
        assert session.scalar(
            select(EventModel.id).where(
                EventModel.workspace_id == workspace_id,
                EventModel.aggregate_id == revision_run.id,
                EventModel.event_type == "CollaborationRunCompleted",
            )
        ) is not None
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id == revision_request_id,
            )
        ).all()
        assert len(releases) == 1
        assert releases[0].run_id == revision_run.id
        assert releases[0].plan_snapshot_id == revised_snapshot.id
        assert releases[0].plan_generation == revised_snapshot.generation
        assert releases[0].decision_id == decisions[0].id
