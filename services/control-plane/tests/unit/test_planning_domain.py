from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    StaleFencingToken,
    ValidationError,
)
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningClarification,
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
    PlanningRun,
    PlanningRunStatus,
    PlanningUsage,
    planning_operation_id,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)

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
        proposal_schema_versions=(1,),
    )


def profile() -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        profile_key="default",
        version=1,
        display_name="Default planner",
        planner_module_instance_id=uuid4(),
        planner_module_config_version=2,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={"approval": {"required": True}},
        proposal_schema_version=1,
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
            max_retries=2,
            backoff_seconds=(5, 30),
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


def create_request() -> PlanningRequest:
    current_profile = profile()
    return PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=current_profile.workspace_id,
        profile_snapshot=current_profile.snapshot(capabilities()),
        objective="Implement the planning loop",
        constraints=("Keep the API idempotent",),
        acceptance_criteria=("The plan is reviewable",),
        context_refs=("artifact://brief/context",),
        requested_outputs=("task graph",),
        source_type="web",
        source_ref="conversation://1",
        actor="user-1",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )


def start_request(request: PlanningRequest) -> tuple[UUID, PlanningRun]:
    lease_id = uuid4()
    run_id = request.current_run_id
    request.start_planning(
        run_id=run_id,
        lease_id=lease_id,
        fencing_token=1,
        now=NOW + timedelta(seconds=1),
    )
    return lease_id, request.run(run_id)


def test_create_request_emits_brief_revision_one_and_queued_run_one() -> None:
    request = create_request()

    assert request.kind is PlanningRequestKind.CREATE
    assert request.status is PlanningRequestStatus.SUBMITTED
    assert request.current_brief_revision == 1
    assert request.briefs[0].revision == 1
    assert len(request.runs) == 1
    assert request.runs[0].attempt == 1
    assert request.runs[0].status is PlanningRunStatus.QUEUED
    assert request.runs[0].operation_id == planning_operation_id(
        request.runs[0].id, request.runs[0].attempt
    )
    assert request.version == 1

    with pytest.raises(ValidationError, match="create"):
        PlanningRequest.create(
            request_id=uuid4(),
            workspace_id=request.workspace_id,
            profile_snapshot=request.profile_snapshot,
            objective="revision",
            constraints=(),
            acceptance_criteria=("criterion",),
            context_refs=(),
            requested_outputs=("plan",),
            source_type="web",
            source_ref=None,
            actor="user-1",
            correlation_id=uuid4(),
            run_id=uuid4(),
            now=NOW,
            kind=PlanningRequestKind.REVISION,
        )


def test_planning_run_follows_happy_path_and_fenced_completion() -> None:
    request = create_request()
    lease_id, run = start_request(request)
    assert request.status is PlanningRequestStatus.PLANNING
    assert run.status is PlanningRunStatus.RUNNING
    assert request.version == 2
    assert run.version == 2

    with pytest.raises(StaleFencingToken):
        request.succeed_run(
            run_id=run.id,
            lease_id=lease_id,
            fencing_token=2,
            provider_ref="provider://run",
            structured_result_type="proposal",
            usage=PlanningUsage.zero(),
            now=NOW + timedelta(seconds=2),
        )

    request.succeed_run(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        provider_ref="provider://run",
        structured_result_type="proposal",
        usage=PlanningUsage(
            input_tokens=10,
            output_tokens=20,
            cost_microunits=30,
            runtime_seconds=2,
            context_bytes=100,
        ),
        now=NOW + timedelta(seconds=2),
    )
    assert run.status is PlanningRunStatus.SUCCEEDED
    assert run.version == 3

    request.mark_proposal_ready(
        run_id=run.id,
        proposal_revision=1,
        content_hash="a" * 64,
        now=NOW + timedelta(seconds=3),
    )
    assert request.status is PlanningRequestStatus.PROPOSAL_READY
    assert request.current_proposal_revision == 1
    request.materialize(
        proposal_revision=1,
        content_hash="a" * 64,
        now=NOW + timedelta(seconds=4),
    )
    assert request.status is PlanningRequestStatus.MATERIALIZED

    with pytest.raises(InvalidTransition):
        request.cancel(now=NOW + timedelta(seconds=5))


def test_waiting_for_user_requires_blocking_clarification_and_answer_creates_run() -> None:
    request = create_request()
    lease_id, run = start_request(request)
    question = PlanningClarification.create(
        clarification_id=uuid4(),
        planning_request_id=request.id,
        planning_run_id=run.id,
        question="Which repository should change?",
        rationale="The request names more than one repository.",
        answer_type="text",
        options=(),
        blocking=True,
        created_at=NOW + timedelta(seconds=2),
    )
    request.mark_needs_clarification(
        run_id=run.id,
        clarifications=(question,),
        lease_id=lease_id,
        fencing_token=1,
        now=NOW + timedelta(seconds=3),
    )
    assert request.status is PlanningRequestStatus.WAITING_FOR_USER
    assert run.status is PlanningRunStatus.NEEDS_CLARIFICATION

    brief = PlanningBriefRevision.create(
        planning_request_id=request.id,
        revision=2,
        objective=request.briefs[-1].objective,
        constraints=request.briefs[-1].constraints,
        acceptance_criteria=request.briefs[-1].acceptance_criteria,
        context_refs=request.briefs[-1].context_refs,
        requested_outputs=request.briefs[-1].requested_outputs,
        user_feedback="Repository is sidera-control-plane.",
        source_correlation=uuid4(),
        created_by="user-1",
        created_at=NOW + timedelta(seconds=4),
    )
    next_run_id = uuid4()
    request.answer_clarifications(
        run_id=run.id,
        answers={question.id: "sidera-control-plane"},
        brief=brief,
        new_run_id=next_run_id,
        actor="user-1",
        now=NOW + timedelta(seconds=4),
    )
    assert request.status is PlanningRequestStatus.PLANNING
    assert request.current_brief_revision == 2
    assert len(request.runs) == 2
    assert request.runs[-1].status is PlanningRunStatus.QUEUED
    assert request.clarifications[-1].answer == "sidera-control-plane"
    assert request.clarifications[-1].answer_brief_revision == 2

    with pytest.raises(InvalidTransition, match="waiting"):
        request.answer_clarifications(
            run_id=run.id,
            answers={question.id: "again"},
            brief=replace_brief(brief, revision=3),
            new_run_id=uuid4(),
            actor="user-1",
            now=NOW + timedelta(seconds=5),
        )


def replace_brief(brief: PlanningBriefRevision, *, revision: int) -> PlanningBriefRevision:
    return PlanningBriefRevision.create(
        planning_request_id=brief.planning_request_id,
        revision=revision,
        objective=brief.objective,
        constraints=brief.constraints,
        acceptance_criteria=brief.acceptance_criteria,
        context_refs=brief.context_refs,
        requested_outputs=brief.requested_outputs,
        user_feedback=brief.user_feedback,
        source_correlation=uuid4(),
        created_by=brief.created_by,
        created_at=brief.created_at + timedelta(seconds=1),
    )


def test_non_blocking_clarification_does_not_enter_waiting_state() -> None:
    request = create_request()
    lease_id, run = start_request(request)
    question = PlanningClarification.create(
        clarification_id=uuid4(),
        planning_request_id=request.id,
        planning_run_id=run.id,
        question="Optional label?",
        rationale="Improves display only.",
        answer_type="text",
        options=(),
        blocking=False,
        created_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(ValidationError, match="blocking"):
        request.mark_needs_clarification(
            run_id=run.id,
            clarifications=(question,),
            lease_id=lease_id,
            fencing_token=1,
            now=NOW + timedelta(seconds=3),
        )


def test_proposal_revision_requires_matching_hash_and_revision_feedback_creates_run() -> None:
    request = create_request()
    lease_id, run = start_request(request)
    request.succeed_run(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        provider_ref="provider://run",
        structured_result_type="proposal",
        usage=PlanningUsage.zero(),
        now=NOW + timedelta(seconds=2),
    )
    request.mark_proposal_ready(
        run_id=run.id,
        proposal_revision=1,
        content_hash="b" * 64,
        now=NOW + timedelta(seconds=3),
    )
    revision_brief = replace_brief(
        request.briefs[-1], revision=2
    )
    revision_brief = PlanningBriefRevision.create(
        planning_request_id=revision_brief.planning_request_id,
        revision=revision_brief.revision,
        objective=revision_brief.objective,
        constraints=revision_brief.constraints,
        acceptance_criteria=revision_brief.acceptance_criteria,
        context_refs=revision_brief.context_refs,
        requested_outputs=revision_brief.requested_outputs,
        user_feedback="Split the deployment work into two subtasks.",
        source_correlation=revision_brief.source_correlation,
        created_by=revision_brief.created_by,
        created_at=revision_brief.created_at,
    )

    with pytest.raises(ValidationError, match="hash"):
        request.request_revision(
            expected_proposal_revision=1,
            expected_content_hash="c" * 64,
            feedback="change",
            brief=revision_brief,
            new_run_id=uuid4(),
            actor="user-1",
            now=NOW + timedelta(seconds=4),
        )

    request.request_revision(
        expected_proposal_revision=1,
        expected_content_hash="b" * 64,
        feedback="Split the deployment work into two subtasks.",
        brief=revision_brief,
        new_run_id=uuid4(),
        actor="user-1",
        now=NOW + timedelta(seconds=4),
    )
    assert request.status is PlanningRequestStatus.PLANNING
    assert request.current_brief_revision == 2
    assert len(request.runs) == 2
    assert request.briefs[-1].user_feedback == (
        "Split the deployment work into two subtasks."
    )


def test_failed_and_unknown_runs_are_recoverable_without_treating_unknown_as_failed() -> None:
    request = create_request()
    lease_id, run = start_request(request)
    request.block_run_unknown(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        error_code="provider_timeout_unknown",
        error_message="response was lost",
        now=NOW + timedelta(seconds=2),
    )
    assert run.status is PlanningRunStatus.BLOCKED
    assert request.status is PlanningRequestStatus.FAILED
    with pytest.raises(InvalidTransition):
        request.retry_failed(new_run_id=uuid4(), now=NOW + timedelta(seconds=3))

    request.retry_blocked(new_run_id=uuid4(), now=NOW + timedelta(seconds=3))
    assert request.status is PlanningRequestStatus.PLANNING
    assert request.runs[-1].attempt == 2
    assert request.runs[-1].operation_id != run.operation_id


def test_cancel_and_reject_are_terminal_and_cancel_active_runs() -> None:
    request = create_request()
    request.cancel(now=NOW + timedelta(seconds=1))
    assert request.status is PlanningRequestStatus.CANCELLED
    assert request.runs[0].status is PlanningRunStatus.CANCELLED

    rejected = create_request()
    rejected.reject(now=NOW + timedelta(seconds=1))
    assert rejected.status is PlanningRequestStatus.REJECTED
    with pytest.raises(InvalidTransition):
        rejected.reject(now=NOW + timedelta(seconds=2))


def test_error_message_is_truncated_and_secret_fields_are_rejected() -> None:
    request = create_request()
    lease_id, run = start_request(request)
    with pytest.raises(ValidationError, match="secret"):
        request.fail_run(
            run_id=run.id,
            lease_id=lease_id,
            fencing_token=1,
            error_code="invalid_output",
            error_message="api_key=do-not-store",
            now=NOW + timedelta(seconds=2),
        )

    request.fail_run(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=1,
        error_code="invalid_output",
        error_message="x" * 5000,
        now=NOW + timedelta(seconds=2),
    )
    assert run.status is PlanningRunStatus.FAILED
    assert len(run.error_message or "") == 2048


def test_planning_usage_rejects_negative_values() -> None:
    with pytest.raises(ValidationError, match="input_tokens"):
        PlanningUsage(
            input_tokens=-1,
            output_tokens=0,
            cost_microunits=0,
            runtime_seconds=0,
            context_bytes=0,
        )
