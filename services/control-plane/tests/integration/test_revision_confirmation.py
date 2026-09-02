from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from tests.integration.test_plan_revision_commands import _capabilities, _profile

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.revision_materializer import RevisionMaterializer
from sidera_control_plane.application.worker_item_materializer import (
    WorkerItemMaterializer,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
)
from sidera_control_plane.domain.plan_revisions import (
    ContextDisposition,
    IdentityStrategy,
    RevisionBaseDescriptorV1,
    SubtaskIdentityDecision,
    WorkerItemIdentityDecision,
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
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
    PlanningUsage,
)
from sidera_control_plane.domain.planning_proposals import (
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    PlanningProfileDescriptor,
    ProposalRevision,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
)
from sidera_control_plane.domain.plans import (
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.revision_diff import RevisionDiffNormalizer
from sidera_control_plane.domain.task import Task, TaskStatus
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.models import (
    PlanningRequestModel,
    PlanSnapshotModel,
    PlanTransitionModel,
    ProposalMaterializationModel,
    RootTaskPlanHeadModel,
    TaskModel,
    WorkerItemModel,
    WorkerTransitionModel,
)
from sidera_control_plane.persistence.plan_repository import PlanRepository
from sidera_control_plane.persistence.plan_transition_repository import (
    PlanEntityLineageRepository,
    PlanTransitionRepository,
    WorkerTransitionRepository,
)
from sidera_control_plane.persistence.planning_repository import (
    PlanningRequestRepository,
    ProposalMaterializationRepository,
)
from sidera_control_plane.persistence.repositories import (
    CommandRepository,
    EventRepository,
    TaskRepository,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from sidera_control_plane.persistence.worker_item_repository import (
    WorkerItemRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("50000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("40000000-0000-0000-0000-000000000001")
SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000001")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _base_plan(
    workspace_id: UUID,
    *,
    snapshot_id: UUID = SNAPSHOT_ID,
    generation: int = 1,
    parent_snapshot_id: UUID | None = None,
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=2,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="integration",
        source_ref="revision-confirmation-base",
        title="Running control room",
        goal_summary="Implement the current behavior.",
        acceptance_refs=("acceptance:root",),
        published_by="integration",
        published_at=NOW,
        subtasks=(
            SubtaskPlanItem(
                key="implementation",
                task_id=SUBTASK_ID,
                kind=WorkItemKind.CHILD,
                title="Implementation",
                goal_summary="Implement the current behavior.",
                acceptance_refs=("acceptance:implementation",),
                milestone_key="m1",
                expected_outputs=("artifact:implementation",),
                source_refs=("brief:initial",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="backend",
                worker_item_id=WORKER_ITEM_ID,
                subtask_key="implementation",
                title="Backend worker",
                role="backend",
                responsibility="Implement the current behavior.",
                capability_requirements=("python",),
                worker_policy_ref="policy:default",
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("brief:initial",),
            ),
        ),
        subtask_milestones=(
            SubtaskMilestone(
                key="m1",
                title="Implementation",
                subtask_keys=("implementation",),
                acceptance_refs=("acceptance:implementation",),
                order_hint=1,
            ),
        ),
    )


def _revision_payload(
    snapshot: TaskPlanSnapshot,
) -> PlanningRevisionProposalPayloadV3:
    return PlanningRevisionProposalPayloadV3(
        schema_version=3,
        kind=PlanningRequestKind.REVISION,
        base_descriptor=RevisionBaseDescriptorV1(
            schema_version=1,
            workspace_id=snapshot.workspace_id,
            root_task_id=snapshot.root_task_id,
            plan_snapshot_id=snapshot.id,
            plan_generation=snapshot.generation,
            plan_content_hash=plan_snapshot_content_hash(snapshot),
            root_task_version=1,
            captured_at=NOW,
        ),
        root_task=ProposedRootTask(
            title=snapshot.title,
            goal_summary="Implement the revised behavior.",
            acceptance_refs=snapshot.acceptance_refs,
        ),
        subtasks=(
            ProposedSubtask(
                key="implementation",
                kind=WorkItemKind.CHILD,
                title="Implementation",
                goal_summary="Implement the revised behavior.",
                acceptance_refs=("acceptance:implementation",),
                expected_outputs=("artifact:implementation",),
                milestone_key="m1",
                source_refs=("brief:revision",),
            ),
        ),
        worker_items=(
            ProposedWorkerItem(
                key="backend",
                subtask_key="implementation",
                title="Backend worker",
                role="backend",
                responsibility="Implement the revised behavior.",
                capability_requirements=("python",),
                worker_policy_ref="policy:default",
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("brief:revision",),
            ),
        ),
        subtask_milestones=tuple(snapshot.subtask_milestones),
        subtask_dependencies=(),
        worker_item_dependencies=(),
        execution_intents=(
            ExecutionIntent(
                worker_item_key="backend",
                purpose="Implement the revised behavior.",
                context_refs=("artifact://current-context",),
                expected_artifact_refs=("artifact:implementation",),
                required_evidence_refs=("evidence:implementation",),
                capability_requirements=("python",),
                adapter_preferences=("opencode",),
                budget_class="standard",
                deadline_seconds=None,
                not_before_seconds=None,
                max_attempts=2,
                activation_dependencies=(),
            ),
        ),
        subtask_identity_decisions=(
            SubtaskIdentityDecision(
                target_subtask_key="implementation",
                strategy=IdentityStrategy.REUSE,
                existing_subtask_task_id=SUBTASK_ID,
                derived_from_subtask_task_ids=(),
                reason="same subtask",
            ),
        ),
        worker_item_identity_decisions=(
            WorkerItemIdentityDecision(
                target_worker_item_key="backend",
                strategy=IdentityStrategy.REUSE,
                existing_worker_item_id=WORKER_ITEM_ID,
                derived_from_worker_item_ids=(),
                target_subtask_key="implementation",
                context_disposition=ContextDisposition.PATCH_REQUIRED,
                change_reasons=("goal changed",),
            ),
        ),
        retired_subtask_ids=(),
        retired_worker_item_ids=(),
        decision_summary=DecisionSummary(
            understanding="Revise the active behavior.",
            assumptions=(),
            risks=(),
            estimated_cost_microunits=10,
            estimated_runtime_seconds=60,
            estimated_concurrency=1,
            scope_includes=("implementation",),
            scope_excludes=(),
            metadata={},
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="block_affected_branch",
            revision_operation="revise",
        ),
    )


def _preserve_payload(snapshot: TaskPlanSnapshot) -> PlanningRevisionProposalPayloadV3:
    payload = _revision_payload(snapshot)
    subtask = snapshot.subtasks[0]
    worker = snapshot.worker_item_plans[0]
    return replace(
        payload,
        root_task=ProposedRootTask(
            title=snapshot.title,
            goal_summary=snapshot.goal_summary,
            acceptance_refs=snapshot.acceptance_refs,
        ),
        subtasks=(
            replace(
                payload.subtasks[0],
                title=subtask.title,
                goal_summary=subtask.goal_summary,
                acceptance_refs=subtask.acceptance_refs,
                expected_outputs=subtask.expected_outputs,
                source_refs=subtask.source_refs,
            ),
        ),
        worker_items=(
            replace(
                payload.worker_items[0],
                title=worker.title,
                role=worker.role,
                responsibility=worker.responsibility,
                capability_requirements=worker.capability_requirements,
                worker_policy_ref=worker.worker_policy_ref,
                configuration_ref=worker.configuration_ref,
                concurrency_limit=worker.concurrency_limit,
                source_refs=worker.source_refs,
            ),
        ),
        worker_item_identity_decisions=(
            replace(
                payload.worker_item_identity_decisions[0],
                context_disposition=ContextDisposition.UNCHANGED,
                change_reasons=(),
            ),
        ),
    )


def _derive_payload(snapshot: TaskPlanSnapshot) -> PlanningRevisionProposalPayloadV3:
    payload = _preserve_payload(snapshot)
    review_worker = replace(
        payload.worker_items[0],
        key="review",
        title="Review worker",
        responsibility="Review the revised behavior.",
    )
    review_intent = replace(
        payload.execution_intents[0],
        worker_item_key="review",
        purpose="Review the revised behavior.",
    )
    review_decision = WorkerItemIdentityDecision(
        target_worker_item_key="review",
        strategy=IdentityStrategy.DERIVE,
        existing_worker_item_id=None,
        derived_from_worker_item_ids=(WORKER_ITEM_ID,),
        target_subtask_key="implementation",
        context_disposition=ContextDisposition.UNCHANGED,
        change_reasons=("split review responsibility",),
    )
    return replace(
        payload,
        worker_items=(payload.worker_items[0], review_worker),
        execution_intents=(payload.execution_intents[0], review_intent),
        worker_item_identity_decisions=(
            payload.worker_item_identity_decisions[0],
            review_decision,
        ),
    )


def _retire_payload(snapshot: TaskPlanSnapshot) -> PlanningRevisionProposalPayloadV3:
    payload = _preserve_payload(snapshot)
    replacement_worker = replace(
        payload.worker_items[0],
        key="replacement",
        title="Replacement worker",
        responsibility="Implement the revised behavior with a new identity.",
    )
    replacement_intent = replace(
        payload.execution_intents[0],
        worker_item_key="replacement",
        purpose="Implement the revised behavior with a new identity.",
    )
    replacement_decision = WorkerItemIdentityDecision(
        target_worker_item_key="replacement",
        strategy=IdentityStrategy.CREATE,
        existing_worker_item_id=None,
        derived_from_worker_item_ids=(),
        target_subtask_key="implementation",
        context_disposition=ContextDisposition.UNCHANGED,
        change_reasons=("replace retired worker",),
    )
    return replace(
        payload,
        worker_items=(replacement_worker,),
        execution_intents=(replacement_intent,),
        worker_item_identity_decisions=(replacement_decision,),
        retired_worker_item_ids=(WORKER_ITEM_ID,),
    )


def _seed_revision_confirmation(
    database,
    workspace_id: UUID,
    *,
    snapshot: TaskPlanSnapshot | None = None,
    previous_snapshot: TaskPlanSnapshot | None = None,
    payload: PlanningRevisionProposalPayloadV3 | None = None,
) -> dict[str, Any]:
    snapshot = snapshot or _base_plan(workspace_id)
    payload = payload or _revision_payload(snapshot)
    capabilities = _capabilities()
    instance_id = uuid4()
    profile = _profile(workspace_id, instance_id)
    definition_id = uuid4()
    root_task = Task.create(
        task_id=snapshot.root_task_id,
        workspace_id=workspace_id,
        title=snapshot.title,
        description=snapshot.goal_summary,
        acceptance_criteria=snapshot.acceptance_refs,
        priority=50,
        requested_by="integration",
    )
    subtask_plan = snapshot.subtasks[0]
    assert subtask_plan.task_id is not None
    subtask = Task.create(
        task_id=subtask_plan.task_id,
        workspace_id=workspace_id,
        title=subtask_plan.title,
        description=subtask_plan.goal_summary,
        acceptance_criteria=subtask_plan.acceptance_refs,
        priority=50,
        requested_by="integration",
    )
    worker_plan = snapshot.worker_item_plans[0]
    assert worker_plan.worker_item_id is not None
    worker_item = WorkerItem.create(
        item_id=worker_plan.worker_item_id,
        workspace_id=workspace_id,
        root_task_id=snapshot.root_task_id,
        title=worker_plan.title,
        responsibility=worker_plan.responsibility,
        now=NOW,
    )
    request = PlanningRequest.create_revision(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=profile.snapshot(capabilities),
        objective="Revise the running plan",
        constraints=("Keep completed work reusable",),
        acceptance_criteria=("The revised plan is reviewable",),
        context_refs=(),
        requested_outputs=("proposal",),
        source_type="integration",
        source_ref="revision-confirmation",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
        base=payload.base_descriptor,
    )
    run = request.runs[0]
    request.start_planning(
        run_id=run.id,
        lease_id=uuid4(),
        fencing_token=1,
        now=NOW,
    )
    assert run.lease_id is not None
    assert run.fencing_token > 0
    request.succeed_run(
        run_id=run.id,
        lease_id=run.lease_id,
        fencing_token=run.fencing_token,
        provider_ref="fake:revision",
        structured_result_type="planning.proposal.v3",
        usage=PlanningUsage.zero(),
        now=NOW,
    )
    proposal = ProposalRevision.create(
        planning_request_id=request.id,
        revision=1,
        brief_revision=1,
        planning_run_id=run.id,
        kind=PlanningRequestKind.REVISION,
        payload=payload,
        profile_descriptor=PlanningProfileDescriptor.from_snapshot(
            request.profile_snapshot
        ),
        profile_snapshot=request.profile_snapshot,
        created_at=NOW,
        derived_diff=RevisionDiffNormalizer().normalize(snapshot, payload),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(
            ModuleDefinition(
                id=definition_id,
                workspace_id=workspace_id,
                module_type="planner_adapter",
                provider="fake",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities=capabilities,
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_instance(
            ModuleInstance(
                id=instance_id,
                workspace_id=workspace_id,
                definition_id=definition_id,
                instance_name="revision-confirmation-planner",
                config_version=1,
                config_namespace="planning",
                config={},
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.planning_profiles.add(profile)
        uow.tasks.add(root_task)
        uow.tasks.add(subtask)
        uow.worker_items.add(worker_item)
        uow.session.flush()
        if previous_snapshot is None:
            uow.plans.add_snapshot_and_advance_head(snapshot, expected_head=None)
        else:
            uow.plans.add_snapshot_and_advance_head(
                previous_snapshot, expected_head=None
            )
            WorkerItemMaterializer().materialize(uow, previous_snapshot, now=NOW)
            current_head = uow.plans.get_head(
                snapshot.root_task_id, workspace_id, for_update=True
            )
            assert current_head is not None
            uow.plans.add_snapshot_and_advance_head(
                snapshot, expected_head=current_head
            )
        WorkerItemMaterializer().materialize(uow, snapshot, now=NOW)
        uow.planning_requests.add(request)
        uow.session.flush()
        uow.planning_records.add_proposal(proposal, workspace_id)
        uow.session.flush()
        previous_request_version = request.version
        request.mark_proposal_ready(
            run_id=run.id,
            proposal_revision=proposal.revision,
            content_hash=proposal.content_hash,
            now=NOW,
        )
        uow.planning_requests.save(request, previous_request_version)

    return {
        "request": request,
        "proposal": proposal,
        "profile": profile,
        "snapshot": snapshot,
        "payload": payload,
        "root_task": root_task,
        "worker_item": worker_item,
    }


def _confirm_command(seed: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    request = seed["request"]
    proposal = seed["proposal"]
    snapshot = seed["snapshot"]
    profile = seed["profile"]
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(snapshot.workspace_id),
        "command_type": "ConfirmPlanRevision",
        "target_type": "planning_request",
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(request.id),
        "expected_version": request.version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": {
            "expected_proposal_revision": proposal.revision,
            "expected_proposal_content_hash": proposal.content_hash,
            "expected_diff_hash": proposal.derived_diff.diff_hash,
            "expected_base_snapshot_id": str(snapshot.id),
            "expected_base_content_hash": plan_snapshot_content_hash(snapshot),
            "profile_id": str(profile.id),
            "profile_version": profile.version,
        },
    }


def test_confirm_revision_materializes_new_generation_and_patch_fence(
    client: TestClient, database, workspace_id: UUID
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)

    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="confirm-revision"),
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["plan_generation"] == 2
    assert data["proposal_revision_id"] == str(seed["proposal"].id)
    assert data["diff_hash"] == seed["proposal"].derived_diff.diff_hash
    assert data["plan_transition_id"]

    with database.session() as session:
        head = session.scalar(
            select(RootTaskPlanHeadModel).where(
                RootTaskPlanHeadModel.root_task_id == seed["root_task"].id
            )
        )
        assert head is not None
        assert head.active_generation == 2
        assert head.active_snapshot_id == UUID(data["plan_snapshot_id"])
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 2
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 2
        assert session.scalar(select(func.count()).select_from(WorkerItemModel)) == 1
        assert (
            session.scalar(select(func.count()).select_from(PlanTransitionModel)) == 1
        )
        assert (
            session.scalar(select(func.count()).select_from(WorkerTransitionModel)) == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ProposalMaterializationModel)
            )
            == 1
        )
        worker = session.scalar(
            select(WorkerItemModel).where(WorkerItemModel.id == seed["worker_item"].id)
        )
        assert worker is not None
        assert worker.context_version == 2
        assert worker.acknowledged_context_version == 1
        transition = session.scalar(select(WorkerTransitionModel))
        assert transition is not None
        assert transition.status == WorkerTransitionStatus.PENDING.value
        assert transition.writer_state == WriterState.FENCED.value
        assert transition.target_context_version == 2


def test_confirm_revision_replay_returns_same_materialization(
    client: TestClient, database, workspace_id: UUID
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)
    command = _confirm_command(seed, idempotency_key="confirm-revision-replay")

    first = client.post("/api/v1/commands", json=command)
    replay = client.post(
        "/api/v1/commands",
        json={**command, "correlation_id": str(uuid4())},
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 2
        assert (
            session.scalar(select(func.count()).select_from(PlanTransitionModel)) == 1
        )


def test_stale_revision_confirmation_marks_request_conflicted_without_new_generation(
    client: TestClient, database, workspace_id: UUID
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)
    stale_snapshot = replace(
        seed["snapshot"],
        id=uuid4(),
        generation=2,
        parent_snapshot_id=seed["snapshot"].id,
        source_ref="integration:unrelated-generation",
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.plans.add_snapshot_and_advance_head(
            stale_snapshot,
            expected_head=uow.plans.get_head(
                seed["root_task"].id, workspace_id, for_update=True
            ),
        )

    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="confirm-stale-revision"),
    )

    assert response.status_code == 409, response.text
    assert response.json()["data"]["status"] == "conflicted"
    with database.session() as session:
        request_status = session.scalar(
            select(ProposalMaterializationModel).where(
                ProposalMaterializationModel.planning_request_id == seed["request"].id
            )
        )
        assert request_status is None
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 2
        assert (
            session.scalar(select(func.count()).select_from(PlanTransitionModel)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(WorkerTransitionModel)) == 0
        )
        row = session.scalar(
            select(TaskModel).where(TaskModel.id == seed["root_task"].id)
        )
        assert row is not None
        assert row.version == seed["root_task"].version


def test_prior_in_flight_worker_transition_blocks_confirmation(
    client: TestClient, database, workspace_id: UUID
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)
    transition_id = uuid4()
    target_snapshot = replace(
        seed["snapshot"],
        id=uuid4(),
        generation=2,
        parent_snapshot_id=seed["snapshot"].id,
        source_ref="integration:prior-transition",
    )
    plan_transition = PlanTransition(
        id=transition_id,
        workspace_id=workspace_id,
        root_task_id=seed["root_task"].id,
        planning_request_id=seed["request"].id,
        proposal_revision_id=seed["proposal"].id,
        from_snapshot_id=seed["snapshot"].id,
        from_generation=1,
        to_snapshot_id=target_snapshot.id,
        to_generation=2,
        status=PlanTransitionStatus.CONVERGING,
        started_at=NOW,
        completed_at=None,
        version=1,
    )
    worker_transition = WorkerTransition(
        id=uuid4(),
        plan_transition_id=transition_id,
        workspace_id=workspace_id,
        root_task_id=seed["root_task"].id,
        worker_item_id=seed["worker_item"].id,
        from_subtask_id=seed["snapshot"].subtasks[0].task_id,
        to_subtask_id=seed["snapshot"].subtasks[0].task_id,
        action=WorkerTransitionAction.PATCH,
        fallback_action=WorkerTransitionAction.CONTINUE,
        target_context_version=2,
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
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.plans.add(target_snapshot)
        uow.plan_transitions.add(plan_transition)
        uow.session.flush()
        uow.worker_transitions.add(worker_transition)

    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="confirm-fenced-revision"),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "revision_confirmation_blocked"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 2
        assert (
            session.scalar(select(func.count()).select_from(PlanTransitionModel)) == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ProposalMaterializationModel)
            )
            == 0
        )


def test_revision_diff_mismatch_rejects_without_materialization(
    client: TestClient, database, workspace_id: UUID
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)
    command = _confirm_command(seed, idempotency_key="confirm-diff-mismatch")
    command["payload"] = {
        **command["payload"],
        "expected_diff_hash": "0" * 64,
    }

    response = client.post("/api/v1/commands", json=command)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 1
        assert (
            session.scalar(
                select(func.count()).select_from(ProposalMaterializationModel)
            )
            == 0
        )
        request_row = session.scalar(
            select(PlanningRequestModel).where(
                PlanningRequestModel.id == seed["request"].id
            )
        )
        assert request_row is not None
        assert request_row.status == PlanningRequestStatus.PROPOSAL_READY.value


def test_terminal_subtask_changed_work_requires_derived_identity(
    client: TestClient, database, workspace_id: UUID
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)
    with SqlAlchemyUnitOfWork(database) as uow:
        subtask = uow.tasks.get(SUBTASK_ID, workspace_id, for_update=True)
        assert subtask is not None
        previous_version = subtask.version
        subtask.status = TaskStatus.SUCCEEDED
        subtask.version += 1
        uow.tasks.save(subtask, previous_version)

    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="confirm-terminal-subtask"),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert "derived identity" in response.json()["error"]["message"]
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 1
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 2
        assert (
            session.scalar(
                select(TaskModel.version).where(TaskModel.id == ROOT_TASK_ID)
            )
            == seed["root_task"].version
        )


@pytest.mark.parametrize(
    "failure_point",
    (
        "snapshot_add",
        "head_advance",
        "task_save",
        "worker_save",
        "plan_transition_add",
        "worker_transition_add",
        "lineage_add",
        "materialization_add",
        "request_save",
        "event_append",
        "command_record",
    ),
)
def test_revision_materialization_rolls_back_at_every_write_boundary(
    client: TestClient,
    database,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(f"injected revision failure: {failure_point}")

    methods = {
        "snapshot_add": (PlanRepository, "add"),
        "head_advance": (PlanRepository, "advance_head"),
        "task_save": (TaskRepository, "save"),
        "worker_save": (WorkerItemRepository, "save"),
        "plan_transition_add": (PlanTransitionRepository, "add"),
        "worker_transition_add": (WorkerTransitionRepository, "add"),
        "lineage_add": (PlanEntityLineageRepository, "add"),
        "materialization_add": (ProposalMaterializationRepository, "add"),
        "request_save": (PlanningRequestRepository, "save"),
        "event_append": (EventRepository, "append_with_outbox"),
        "command_record": (CommandRepository, "record_applied"),
    }
    repository, method = methods[failure_point]
    monkeypatch.setattr(repository, method, fail)

    with pytest.raises(RuntimeError, match=failure_point):
        client.post(
            "/api/v1/commands",
            json=_confirm_command(
                seed, idempotency_key=f"confirm-rollback-{failure_point}"
            ),
        )

    with database.session() as session:
        head = session.scalar(
            select(RootTaskPlanHeadModel).where(
                RootTaskPlanHeadModel.root_task_id == ROOT_TASK_ID
            )
        )
        assert head is not None
        assert head.active_generation == 1
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 1
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 2
        assert session.scalar(select(func.count()).select_from(WorkerItemModel)) == 1
        assert (
            session.scalar(select(func.count()).select_from(PlanTransitionModel)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(WorkerTransitionModel)) == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ProposalMaterializationModel)
            )
            == 0
        )


def test_preserve_revision_converges_without_fencing_or_context_change(
    client: TestClient, database, workspace_id: UUID
) -> None:
    seed = _seed_revision_confirmation(
        database, workspace_id, payload=_preserve_payload(_base_plan(workspace_id))
    )

    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="confirm-preserve-revision"),
    )

    assert response.status_code == 201, response.text
    with database.session() as session:
        plan_transition = session.scalar(select(PlanTransitionModel))
        worker_transition = session.scalar(select(WorkerTransitionModel))
        assert plan_transition is not None
        assert plan_transition.status == PlanTransitionStatus.CONVERGED.value
        assert worker_transition is not None
        assert worker_transition.action == WorkerTransitionAction.PRESERVE.value
        assert worker_transition.status == WorkerTransitionStatus.CONVERGED.value
        assert worker_transition.writer_state == WriterState.CLOSED.value
        worker = session.scalar(
            select(WorkerItemModel).where(WorkerItemModel.id == WORKER_ITEM_ID)
        )
        assert worker is not None
        assert worker.context_version == worker.acknowledged_context_version == 1


def test_derived_worker_item_gets_new_identity_and_lineage(
    client: TestClient, database, workspace_id: UUID
) -> None:
    seed = _seed_revision_confirmation(
        database, workspace_id, payload=_derive_payload(_base_plan(workspace_id))
    )

    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="confirm-derived-revision"),
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["worker_item_mapping"]["backend"] == str(WORKER_ITEM_ID)
    assert data["worker_item_mapping"]["review"] != str(WORKER_ITEM_ID)
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(WorkerItemModel)) == 2
        transitions = session.scalars(
            select(WorkerTransitionModel).order_by(WorkerTransitionModel.action)
        ).all()
        assert {item.action for item in transitions} == {
            WorkerTransitionAction.PRESERVE.value,
            WorkerTransitionAction.CREATE.value,
        }


def test_retired_worker_item_stays_auditable_until_transition_converges(
    client: TestClient, database, workspace_id: UUID
) -> None:
    seed = _seed_revision_confirmation(
        database, workspace_id, payload=_retire_payload(_base_plan(workspace_id))
    )

    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="confirm-retire-revision"),
    )

    assert response.status_code == 201, response.text
    with database.session() as session:
        transitions = session.scalars(select(WorkerTransitionModel)).all()
        assert {item.action for item in transitions} == {
            WorkerTransitionAction.CREATE.value,
            WorkerTransitionAction.RETIRE.value,
        }
        assert all(
            item.status == WorkerTransitionStatus.PENDING.value for item in transitions
        )
        old_worker = session.scalar(
            select(WorkerItemModel).where(WorkerItemModel.id == WORKER_ITEM_ID)
        )
        assert old_worker is not None
        assert old_worker.status == "active"


def test_known_closed_failure_does_not_block_a_new_revision_confirmation(
    client: TestClient, database, workspace_id: UUID
) -> None:
    previous_snapshot = _base_plan(workspace_id)
    current_snapshot = _base_plan(
        workspace_id,
        snapshot_id=uuid4(),
        generation=2,
        parent_snapshot_id=previous_snapshot.id,
    )
    seed = _seed_revision_confirmation(
        database,
        workspace_id,
        snapshot=current_snapshot,
        previous_snapshot=previous_snapshot,
    )
    prior_transition_id = uuid4()
    prior_payload = _preserve_payload(previous_snapshot)
    prior_request = PlanningRequest.create_revision(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=seed["profile"].snapshot(_capabilities()),
        objective="Previous revision",
        constraints=(),
        acceptance_criteria=("Previous plan was published",),
        context_refs=(),
        requested_outputs=("proposal",),
        source_type="integration",
        source_ref="known-failure-previous-request",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
        base=prior_payload.base_descriptor,
    )
    prior_request.status = PlanningRequestStatus.MATERIALIZED
    prior_proposal = ProposalRevision.create(
        planning_request_id=prior_request.id,
        revision=1,
        brief_revision=1,
        planning_run_id=prior_request.runs[0].id,
        kind=PlanningRequestKind.REVISION,
        payload=prior_payload,
        profile_descriptor=PlanningProfileDescriptor.from_snapshot(
            prior_request.profile_snapshot
        ),
        profile_snapshot=prior_request.profile_snapshot,
        created_at=NOW,
        derived_diff=RevisionDiffNormalizer().normalize(
            previous_snapshot, prior_payload
        ),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.planning_requests.add(prior_request)
        uow.session.flush()
        uow.planning_records.add_proposal(prior_proposal, workspace_id)
        uow.session.flush()
    prior_worker_transition = WorkerTransition(
        id=uuid4(),
        plan_transition_id=prior_transition_id,
        workspace_id=workspace_id,
        root_task_id=seed["root_task"].id,
        worker_item_id=WORKER_ITEM_ID,
        from_subtask_id=SUBTASK_ID,
        to_subtask_id=SUBTASK_ID,
        action=WorkerTransitionAction.PATCH,
        fallback_action=WorkerTransitionAction.CONTINUE,
        target_context_version=1,
        source_execution_id=None,
        source_session_id=None,
        target_execution_id=None,
        target_session_id=None,
        writer_state=WriterState.CLOSED,
        status=WorkerTransitionStatus.FAILED,
        failure_code="known_failure",
        lease_id=None,
        fencing_token=0,
        attempt_count=1,
        next_attempt_at=None,
        started_at=NOW,
        completed_at=NOW,
        version=1,
    )
    prior_plan_transition = PlanTransition(
        id=prior_transition_id,
        workspace_id=workspace_id,
        root_task_id=seed["root_task"].id,
        planning_request_id=prior_request.id,
        proposal_revision_id=prior_proposal.id,
        from_snapshot_id=previous_snapshot.id,
        from_generation=1,
        to_snapshot_id=current_snapshot.id,
        to_generation=2,
        status=PlanTransitionStatus.DEGRADED,
        started_at=NOW,
        completed_at=NOW,
        version=1,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.plan_transitions.add(prior_plan_transition)
        uow.session.flush()
        uow.worker_transitions.add(prior_worker_transition)

    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="confirm-after-known-failure"),
    )

    assert response.status_code == 201, response.text
    with database.session() as session:
        assert (
            session.scalar(select(func.count()).select_from(PlanTransitionModel)) == 2
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ProposalMaterializationModel)
            )
            == 1
        )


def test_revision_materialization_rolls_back_when_event_build_fails(
    client: TestClient,
    database,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)

    def fail_events(*args, **kwargs):
        del args, kwargs
        raise ValidationError("injected event failure")

    monkeypatch.setattr(RevisionMaterializer, "_events", staticmethod(fail_events))
    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="confirm-rollback-revision"),
    )

    assert response.status_code == 422, response.text
    with database.session() as session:
        head = session.scalar(
            select(RootTaskPlanHeadModel).where(
                RootTaskPlanHeadModel.root_task_id == ROOT_TASK_ID
            )
        )
        assert head is not None
        assert head.active_generation == 1
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 1
        assert (
            session.scalar(select(func.count()).select_from(PlanTransitionModel)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(WorkerTransitionModel)) == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ProposalMaterializationModel)
            )
            == 0
        )
