from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.plan_revisions import (
    ACTIVE_REVISION_STATUSES,
    TERMINAL_REVISION_STATUSES,
    ContextDisposition,
    IdentityStrategy,
    RevisionBaseDescriptorV1,
    RootTaskPlanHead,
    SubtaskIdentityDecision,
    WorkerItemIdentityDecision,
    plan_snapshot_content_hash,
    validate_subtask_identity_decisions,
    validate_worker_item_identity_decisions,
)
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
BASE_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000001")
WORKER_A_ID = UUID("40000000-0000-0000-0000-000000000001")
WORKER_B_ID = UUID("40000000-0000-0000-0000-000000000002")
SUBTASK_A_ID = UUID("50000000-0000-0000-0000-000000000001")
SUBTASK_B_ID = UUID("50000000-0000-0000-0000-000000000002")


def planner_capabilities() -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2, 3),
    )


def profile_snapshot():
    profile = PlanningProfile(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_key="default",
        version=3,
        display_name="Revision planner",
        planner_module_instance_id=uuid4(),
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={},
        proposal_schema_version=3,
        allowed_capabilities=("code",),
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
        planner_secret_refs=(),
    )
    return profile.snapshot(planner_capabilities())


def revision_base(*, content_hash: str = "a" * 64) -> RevisionBaseDescriptorV1:
    return RevisionBaseDescriptorV1(
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        plan_snapshot_id=BASE_SNAPSHOT_ID,
        plan_generation=4,
        plan_content_hash=content_hash,
        root_task_version=7,
        captured_at=NOW,
    )


def create_revision_request(
    *, base: RevisionBaseDescriptorV1 | None = None
) -> PlanningRequest:
    return PlanningRequest.create_revision(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_snapshot=profile_snapshot(),
        objective="Change the acceptance conditions",
        constraints=("Keep completed work auditable",),
        acceptance_criteria=("The new plan is reviewable",),
        context_refs=("artifact://current-plan",),
        requested_outputs=("revision plan",),
        source_type="web",
        source_ref="conversation://revision-1",
        actor="user-1",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
        base=base,
    )


def test_revision_request_requires_and_freezes_exact_base() -> None:
    base = revision_base()
    request = create_revision_request(base=base)

    assert request.kind is PlanningRequestKind.REVISION
    assert request.root_task_id == ROOT_TASK_ID
    assert request.base_plan_snapshot_id == BASE_SNAPSHOT_ID
    assert request.base_plan_generation == 4
    assert request.base_plan_content_hash == "a" * 64
    assert request.base_descriptor == base

    with pytest.raises(InvalidTransition, match="base.*immutable"):
        request.replace_revision_base(revision_base(content_hash="b" * 64))
    with pytest.raises(InvalidTransition, match="base.*immutable"):
        request.base_plan_generation = 5

    with pytest.raises(ValidationError, match="exact base"):
        create_revision_request(base=None)
    with pytest.raises(ValidationError, match="lowercase"):
        create_revision_request(base=revision_base(content_hash="A" * 64))


def test_create_request_keeps_revision_base_fields_empty() -> None:
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_snapshot=profile_snapshot(),
        objective="Create a plan",
        constraints=(),
        acceptance_criteria=("It is reviewable",),
        context_refs=(),
        requested_outputs=("plan",),
        source_type="web",
        source_ref=None,
        actor="user-1",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )

    assert request.kind is PlanningRequestKind.CREATE
    assert request.root_task_id is None
    assert request.base_descriptor is None
    assert request.base_plan_snapshot_id is None
    assert request.base_plan_generation is None
    assert request.base_plan_content_hash is None


def test_revision_status_sets_and_conflict_reason_are_explicit() -> None:
    assert ACTIVE_REVISION_STATUSES == {
        PlanningRequestStatus.SUBMITTED,
        PlanningRequestStatus.PLANNING,
        PlanningRequestStatus.WAITING_FOR_USER,
        PlanningRequestStatus.PROPOSAL_READY,
        PlanningRequestStatus.FAILED,
    }
    assert TERMINAL_REVISION_STATUSES == {
        PlanningRequestStatus.MATERIALIZED,
        PlanningRequestStatus.CONFLICTED,
        PlanningRequestStatus.REJECTED,
        PlanningRequestStatus.CANCELLED,
    }

    request = create_revision_request(base=revision_base())
    request.status = PlanningRequestStatus.PROPOSAL_READY
    request.mark_conflicted(now=NOW, reason="stale_base")

    assert request.status is PlanningRequestStatus.CONFLICTED
    assert request.conflict_reason == "stale_base"


def test_plan_head_uses_snapshot_and_version_compare_and_swap() -> None:
    head = RootTaskPlanHead(
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        active_snapshot_id=BASE_SNAPSHOT_ID,
        active_generation=4,
        active_snapshot_hash="a" * 64,
        version=1,
        updated_at=NOW,
    )
    next_snapshot_id = UUID("10000000-0000-0000-0000-000000000002")
    head.advance(
        expected_snapshot_id=BASE_SNAPSHOT_ID,
        expected_version=1,
        target_snapshot_id=next_snapshot_id,
        target_generation=5,
        target_snapshot_hash="b" * 64,
        now=NOW,
    )

    assert head.active_snapshot_id == next_snapshot_id
    assert head.active_generation == 5
    assert head.version == 2

    with pytest.raises(VersionConflict):
        head.advance(
            expected_snapshot_id=next_snapshot_id,
            expected_version=1,
            target_snapshot_id=uuid4(),
            target_generation=6,
            target_snapshot_hash="c" * 64,
            now=NOW,
        )


def worker_decision(
    *,
    key: str,
    strategy: IdentityStrategy,
    existing: UUID | None = None,
    derived: tuple[UUID, ...] = (),
) -> WorkerItemIdentityDecision:
    return WorkerItemIdentityDecision(
        target_worker_item_key=key,
        strategy=strategy,
        existing_worker_item_id=existing,
        derived_from_worker_item_ids=derived,
        target_subtask_key="implementation",
        context_disposition=ContextDisposition.UNCHANGED,
        change_reasons=(),
    )


def test_worker_identity_decisions_are_exact_and_one_to_one() -> None:
    validate_worker_item_identity_decisions(
        (
            worker_decision(
                key="backend", strategy=IdentityStrategy.REUSE, existing=WORKER_A_ID
            ),
            worker_decision(
                key="new", strategy=IdentityStrategy.CREATE
            ),
            worker_decision(
                key="split", strategy=IdentityStrategy.DERIVE, derived=(WORKER_B_ID,)
            ),
        )
    )

    with pytest.raises(ValidationError, match="one target"):
        validate_worker_item_identity_decisions(
            (
                worker_decision(
                    key="first", strategy=IdentityStrategy.REUSE, existing=WORKER_A_ID
                ),
                worker_decision(
                    key="second", strategy=IdentityStrategy.REUSE, existing=WORKER_A_ID
                ),
            )
        )

    with pytest.raises(ValidationError, match="retired"):
        validate_worker_item_identity_decisions(
            (worker_decision(
                key="retired", strategy=IdentityStrategy.REUSE, existing=WORKER_A_ID
            ),),
            retired_worker_item_ids=(WORKER_A_ID,),
        )


def test_subtask_rework_cannot_reuse_terminal_identity() -> None:
    reuse = SubtaskIdentityDecision(
        target_subtask_key="implementation",
        strategy=IdentityStrategy.REUSE,
        existing_subtask_task_id=SUBTASK_A_ID,
        derived_from_subtask_task_ids=(),
        reason="same work",
    )
    derive = SubtaskIdentityDecision(
        target_subtask_key="implementation-v2",
        strategy=IdentityStrategy.DERIVE,
        existing_subtask_task_id=None,
        derived_from_subtask_task_ids=(SUBTASK_A_ID,),
        reason="acceptance changed",
    )

    with pytest.raises(ValidationError, match="derive"):
        validate_subtask_identity_decisions(
            (reuse,), reworked_terminal_subtask_ids=(SUBTASK_A_ID,)
        )
    validate_subtask_identity_decisions(
        (derive,), reworked_terminal_subtask_ids=(SUBTASK_A_ID,)
    )


def test_plan_snapshot_content_hash_is_canonical_and_lowercase() -> None:
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=BASE_SNAPSHOT_ID,
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=4,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="fixture",
        title="Root",
        goal_summary="Goal",
        acceptance_refs=("acceptance:one",),
        published_by="test",
        published_at=NOW,
    )

    content_hash = plan_snapshot_content_hash(snapshot)

    assert len(content_hash) == 64
    assert content_hash == content_hash.lower()
