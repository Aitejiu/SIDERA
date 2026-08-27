from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.plan_revisions import RevisionBaseDescriptorV1
from sidera_control_plane.domain.planning import (
    PlanningClarification,
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
    PlanningRunLease,
    PlanningRunLeaseStatus,
    ProposalMaterialization,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.planning_proposals import (
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    PlanningProfileDescriptor,
    PlanningProposalPayload,
    ProposalRevision,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
from sidera_control_plane.domain.plans import WorkItemKind
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningRequestModel,
    PlanSnapshotModel,
    TaskModel,
)
from sidera_control_plane.persistence.planning_repository import (
    PlanningProfileRepository,
    PlanningRecordRepository,
    PlanningRequestRepository,
    PlanningRunLeaseRepository,
    PlanningRunRepository,
    ProposalMaterializationRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def capabilities() -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2,),
    )


def make_profile(workspace_id: UUID, module_instance_id: UUID) -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        profile_key=f"profile-{uuid4().hex[:8]}",
        version=1,
        display_name="Integration planner",
        planner_module_instance_id=module_instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={"approval": {"required": True}},
        proposal_schema_version=2,
        allowed_capabilities=("code", "test"),
        adapter_preferences=("opencode",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=10_000,
            max_cost_microunits=1_000_000,
            max_runtime_seconds=600,
            max_revisions=3,
            max_nodes=20,
            max_concurrency=2,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(5,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=("secret://planner/key",),
    )


def add_module_instance(database, workspace_id: UUID) -> UUID:
    definition_id = uuid4()
    instance_id = uuid4()
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
        session.flush()
        session.add(
            ModuleInstanceModel(
                id=instance_id,
                workspace_id=workspace_id,
                definition_id=definition_id,
                instance_name="planning-repository-test",
                config_version=1,
                config_namespace="planning",
                config={},
                secret_refs=[],
                lifecycle="active",
            )
        )
        session.commit()
    return instance_id


def make_proposal(
    request: PlanningRequest,
    *,
    revision: int,
    run_id: UUID,
) -> ProposalRevision:
    payload = PlanningProposalPayload(
        schema_version=2,
        kind=request.kind,
        root_task=ProposedRootTask(
            title="Repository test root",
            goal_summary="Exercise planning persistence",
            acceptance_refs=("acceptance:repository",),
        ),
        subtasks=(
            ProposedSubtask(
                key="implementation",
                kind=WorkItemKind.CHILD,
                title="Implementation",
                goal_summary="Implement the repository",
                acceptance_refs=("acceptance:repository",),
                expected_outputs=("code",),
                milestone_key=None,
                source_refs=("brief:1",),
            ),
        ),
        worker_items=(
            ProposedWorkerItem(
                key="repository-worker",
                subtask_key="implementation",
                title="Repository worker",
                role="backend",
                responsibility="Persist planning state",
                capability_requirements=("code",),
                worker_policy_ref="policy:default",
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("brief:1",),
            ),
        ),
        subtask_milestones=(),
        subtask_dependencies=(),
        worker_item_dependencies=(),
        execution_intents=(
            ExecutionIntent(
                worker_item_key="repository-worker",
                purpose="Persist planning state",
                context_refs=("brief:1",),
                expected_artifact_refs=("artifact_type:diff",),
                required_evidence_refs=("acceptance:repository",),
                capability_requirements=("code",),
                adapter_preferences=("opencode",),
                budget_class="standard",
                deadline_seconds=60,
                not_before_seconds=None,
                max_attempts=1,
                activation_dependencies=(),
            ),
        ),
        decision_summary=DecisionSummary(
            understanding="Persist planning state",
            assumptions=(),
            risks=(),
            estimated_cost_microunits=1,
            estimated_runtime_seconds=1,
            estimated_concurrency=1,
            scope_includes=("control plane",),
            scope_excludes=(),
            metadata={},
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="block",
        ),
        base_descriptor=None,
    )
    return ProposalRevision.create(
        planning_request_id=request.id,
        revision=revision,
        brief_revision=1,
        planning_run_id=run_id,
        kind=request.kind,
        payload=payload,
        profile_descriptor=PlanningProfileDescriptor.from_snapshot(
            request.profile_snapshot
        ),
        profile_snapshot=request.profile_snapshot,
        created_at=NOW + timedelta(minutes=revision),
    )


def make_request(workspace_id: UUID, profile: PlanningProfile) -> PlanningRequest:
    return PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=profile.snapshot(capabilities()),
        objective="Persist planning state",
        constraints=("Keep revisions immutable",),
        acceptance_criteria=("Repository round-trips domain facts",),
        context_refs=("context:test",),
        requested_outputs=("repository",),
        source_type="integration",
        source_ref="test:planning-repository",
        actor="integration",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )


def test_profile_exact_version_and_selectable_list(
    database, workspace_id: UUID
) -> None:
    module_instance_id = add_module_instance(database, workspace_id)
    selectable = make_profile(workspace_id, module_instance_id)
    disabled = replace(
        make_profile(workspace_id, module_instance_id),
        status=PlanningProfileStatus.DISABLED,
        user_selectable=False,
    )

    with database.session() as session:
        repository = PlanningProfileRepository(session)
        repository.add(selectable)
        repository.add(disabled)
        session.commit()

    with database.session() as session:
        repository = PlanningProfileRepository(session)
        exact = repository.get(selectable.id, workspace_id, version=1)
        assert exact is not None
        assert exact.id == selectable.id
        assert repository.get(selectable.id, workspace_id, version=2) is None
        assert tuple(item.id for item in repository.list_selectable(workspace_id)) == (
            selectable.id,
        )


def test_request_round_trip_lock_and_optimistic_version(
    database, workspace_id: UUID
) -> None:
    module_instance_id = add_module_instance(database, workspace_id)
    profile = make_profile(workspace_id, module_instance_id)
    request = make_request(workspace_id, profile)

    with database.session() as session:
        PlanningProfileRepository(session).add(profile)
        session.flush()
        PlanningRequestRepository(session).add(request)
        session.commit()

    with database.session() as session:
        repository = PlanningRequestRepository(session)
        stored = repository.get(request.id, workspace_id, for_update=True)
        assert stored is not None
        assert stored.briefs[0].revision == 1
        assert stored.runs[0].status.value == "queued"
        previous_version = stored.version
        stored.start_planning(
            run_id=stored.current_run_id,
            lease_id=uuid4(),
            fencing_token=1,
            now=NOW + timedelta(seconds=1),
        )
        repository.save(stored, previous_version)
        session.commit()

        with pytest.raises(VersionConflict):
            repository.save(stored, previous_version)


def test_revision_request_preserves_exact_base_and_active_lookup(
    database, workspace_id: UUID
) -> None:
    module_instance_id = add_module_instance(database, workspace_id)
    profile = make_profile(workspace_id, module_instance_id)
    root_task_id = uuid4()
    base_snapshot_id = uuid4()
    base = RevisionBaseDescriptorV1(
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        plan_snapshot_id=base_snapshot_id,
        plan_generation=4,
        plan_content_hash="a" * 64,
        root_task_version=7,
        captured_at=NOW,
    )
    first = PlanningRequest.create_revision(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=profile.snapshot(capabilities()),
        objective="Revise the active task",
        constraints=(),
        acceptance_criteria=("Keep the exact base",),
        context_refs=(),
        requested_outputs=("revision",),
        source_type="integration",
        source_ref="test:revision:first",
        actor="integration",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
        base=base,
    )
    second = PlanningRequest.create_revision(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=profile.snapshot(capabilities()),
        objective="Revise the active task again",
        constraints=(),
        acceptance_criteria=("Keep the exact base",),
        context_refs=(),
        requested_outputs=("revision",),
        source_type="integration",
        source_ref="test:revision:second",
        actor="integration",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW + timedelta(seconds=1),
        base=base,
    )
    with database.session() as session:
        session.add(
            TaskModel(
                id=root_task_id,
                workspace_id=workspace_id,
                title="Revision root",
                description="Root for revision request tests.",
                acceptance_criteria=["Keep the exact base"],
                status="pending",
                priority=50,
                requested_by="integration",
                version=7,
            )
        )
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=base_snapshot_id,
                schema_version=2,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                generation=4,
                parent_snapshot_id=None,
                source_type="integration",
                source_ref="test:revision:base",
                title="Revision base",
                goal_summary="Base snapshot for revision request tests.",
                payload={},
                published_by="integration",
                published_at=NOW,
            )
        )
        session.flush()
        PlanningProfileRepository(session).add(profile)
        session.flush()
        repository = PlanningRequestRepository(session)
        repository.add(first)
        session.commit()

    with database.session() as session:
        repository = PlanningRequestRepository(session)
        loaded = repository.get_active_revision_for_root(
            root_task_id, workspace_id, for_update=True
        )
        assert loaded is not None
        assert loaded.id == first.id
        assert loaded.kind is PlanningRequestKind.REVISION
        assert loaded.base_descriptor == base

        session.execute(
            update(PlanningRequestModel)
            .where(PlanningRequestModel.id == first.id)
            .values(
                status=PlanningRequestStatus.CONFLICTED.value,
                conflict_reason="stale_base",
                updated_at=NOW,
            )
        )
        session.commit()

    with database.session() as session:
        repository = PlanningRequestRepository(session)
        repository.add(second)
        session.commit()
        active = repository.get_active_revision_for_root(root_task_id, workspace_id)
        assert active is not None
        assert active.id == second.id
        assert active.base_descriptor == base


def test_run_claim_lease_fencing_clarification_and_immutable_records(
    database, workspace_id: UUID
) -> None:
    module_instance_id = add_module_instance(database, workspace_id)
    profile = make_profile(workspace_id, module_instance_id)
    request = make_request(workspace_id, profile)
    with database.session() as session:
        PlanningProfileRepository(session).add(profile)
        session.flush()
        PlanningRequestRepository(session).add(request)
        session.commit()

    with database.session() as session_one:
        claimed = PlanningRunRepository(session_one).claim_queued(now=NOW, limit=1)
        assert tuple(item.id for item in claimed) == (request.current_run_id,)
        with Session(database.engine) as session_two:
            assert PlanningRunRepository(session_two).claim_queued(
                now=NOW, limit=1
            ) == ()
        session_one.rollback()

    lease = PlanningRunLease(
        id=uuid4(),
        workspace_id=workspace_id,
        planning_run_id=request.current_run_id,
        owner_id="planning-worker-1",
        status=PlanningRunLeaseStatus.ACTIVE,
        fencing_token=7,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        released_at=None,
        version=1,
    )
    clarification = PlanningClarification.create(
        clarification_id=uuid4(),
        planning_request_id=request.id,
        planning_run_id=request.current_run_id,
        question="Which profile should be used?",
        rationale="More than one profile is available.",
        answer_type="text",
        options=(),
        blocking=True,
        created_at=NOW,
    )
    proposal = make_proposal(request, revision=1, run_id=request.current_run_id)
    materialization = ProposalMaterialization(
        id=uuid4(),
        workspace_id=workspace_id,
        planning_request_id=request.id,
        proposal_revision_id=proposal.id,
        root_task_id=uuid4(),
        plan_snapshot_id=uuid4(),
        plan_generation=1,
        subtask_task_mapping={"implementation": uuid4()},
        worker_item_mapping={"repository-worker": uuid4()},
        execution_request_mapping={"repository-worker": uuid4()},
        deep_link="/workspaces/test/tasks/root",
        confirmed_by="integration",
        created_at=NOW,
    )
    with database.session() as session:
        PlanningRunLeaseRepository(session).add(lease)
        record_repository = PlanningRecordRepository(session)
        record_repository.add_clarification(clarification)
        record_repository.add_proposal(proposal, workspace_id)
        session.flush()
        session.add(
            TaskModel(
                id=materialization.root_task_id,
                workspace_id=workspace_id,
                title="Root",
                description="Root",
                acceptance_criteria=["acceptance:repository"],
                status="pending",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=materialization.plan_snapshot_id,
                schema_version=2,
                workspace_id=workspace_id,
                root_task_id=materialization.root_task_id,
                generation=1,
                parent_snapshot_id=None,
                source_type="integration",
                source_ref="test:planning-repository",
                title="Root",
                goal_summary="Root",
                payload={},
                published_by="integration",
                published_at=NOW,
            )
        )
        ProposalMaterializationRepository(session).add(materialization)
        session.commit()

    with database.session() as session:
        lease_repository = PlanningRunLeaseRepository(session)
        active = lease_repository.get_active_for_run(
            request.current_run_id, workspace_id, for_update=True
        )
        assert active is not None
        assert active.fencing_token == 7
        assert PlanningRecordRepository(session).list_pending_clarifications(
            request.id, workspace_id
        )[0].id == clarification.id
        stored_materialization = ProposalMaterializationRepository(
            session
        ).get_by_request(request.id, workspace_id)
        assert stored_materialization is not None
        assert stored_materialization.id == materialization.id
        proposals = PlanningRecordRepository(session).list_proposals(
            request.id, workspace_id
        )
        assert tuple(item.revision for item in proposals) == (1,)
        assert (
            PlanningRecordRepository(session).get_current_proposal(
                request.id, workspace_id
            )
            is None
        )
