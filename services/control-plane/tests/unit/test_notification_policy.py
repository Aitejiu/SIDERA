from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sidera_control_plane.application.notification_policy import (
    NotificationAttention,
    NotificationDueStatus,
    NotificationEvaluationMode,
    NotificationMilestone,
    NotificationObservation,
    NotificationPolicyV1,
    NotificationRouteEligibility,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.notifications import (
    NotificationKind,
    NotificationPriority,
    NotificationThreadBusinessStatus,
    NotificationTransition,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("10000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("20000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("30000000-0000-0000-0000-000000000001")
SOURCE_RESOURCE_ID = UUID("40000000-0000-0000-0000-000000000001")


def _observation(
    *,
    kind: NotificationKind | str = NotificationKind.EXECUTION_ATTENTION,
    status: str = "attention",
    fingerprint: str = "a" * 64,
    topic: str = "execution_attention",
    transition: NotificationTransition | None = None,
    automatic_recovery: bool = False,
    requires_operator: bool = False,
    baseline_import: bool = False,
    source_type: str = "sidera_web_chat",
    source_projection_type: str = "task_overview",
    origin_route_active: bool = False,
    completed_milestones: tuple[NotificationMilestone, ...] = (),
    attention_items: tuple[NotificationAttention, ...] = (),
) -> NotificationObservation:
    return NotificationObservation(
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        subject_type="root_task",
        subject_id=str(ROOT_TASK_ID),
        topic=topic,
        root_task_id=ROOT_TASK_ID,
        subtask_id=SUBTASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        source_projection_cursor=12,
        source_projection_type=source_projection_type,
        source_resource_id=SOURCE_RESOURCE_ID,
        source_resource_version=4,
        kind=kind,
        observed_status=status,
        authoritative_fingerprint=fingerprint,
        root_task_title="支付平台迁移",
        subtask_path=("后端", "回调幂等"),
        worker_item_title="实现回调",
        deep_link=f"/workspaces/{WORKSPACE_ID}/tasks/{ROOT_TASK_ID}",
        correlation_id="correlation-1",
        transition=transition,
        requires_operator=requires_operator,
        automatic_recovery=automatic_recovery,
        baseline_import=baseline_import,
        source_type=source_type,
        origin_route_active=origin_route_active,
        completed_milestones=completed_milestones,
        attention_items=attention_items,
    )


def _policy() -> NotificationPolicyV1:
    return NotificationPolicyV1(blocked_grace_seconds=300)


def test_immediate_action_matrix_creates_safe_facts() -> None:
    cases = (
        (NotificationKind.PLANNING_CLARIFICATION, "action"),
        (NotificationKind.PLANNING_PROPOSAL_READY, "action"),
        (NotificationKind.PLANNING_PROPOSAL_CONFLICTED, "action"),
        (NotificationKind.PLANNING_PROPOSAL_BLOCKED, "action"),
        (NotificationKind.PLAN_TRANSITION_DEGRADED, "action"),
        (NotificationKind.WORKER_TRANSITION_BLOCKED_UNKNOWN, "action"),
        (NotificationKind.HANDOFF_ATTENTION, "action"),
        (NotificationKind.HANDOFF_FAILED, "action"),
        (NotificationKind.HANDOFF_UNKNOWN, "action"),
        (NotificationKind.COLLABORATION_RELEASE_PENDING, "action"),
        (NotificationKind.GOVERNANCE_DECISION_REQUIRED, "action"),
        (NotificationKind.EXECUTION_ATTENTION, "action"),
        (NotificationKind.EXECUTION_FAILURE, "action"),
    )

    for kind, priority in cases:
        mutation = _policy().evaluate(
            _observation(kind=kind),
            now=NOW,
            mode=NotificationEvaluationMode.LIVE,
        )

        assert mutation.fact is not None
        assert mutation.thread_business_status is NotificationThreadBusinessStatus.OPEN
        assert mutation.fact.kind == kind.value
        assert mutation.fact.priority.value == priority
        assert mutation.fact.transition is NotificationTransition.OPENED
        assert mutation.fact.root_task_id == ROOT_TASK_ID
        assert mutation.fact.subtask_id == SUBTASK_ID
        assert mutation.fact.worker_item_id == WORKER_ITEM_ID
        assert mutation.fact.source_resource_version == 4
        assert "correlation-1" == mutation.fact.correlation_id
        assert "支付平台迁移" in mutation.fact.title
        assert "回调幂等" not in mutation.fact.summary
        assert "provider" not in str(mutation).lower()


def test_result_acceptance_recorded_is_progress_and_not_task_completion() -> None:
    mutation = _policy().evaluate(
        _observation(
            kind="result_acceptance_recorded",
            status="accepted",
            topic="result:accepted",
            transition=NotificationTransition.COMPLETED,
        ),
        now=NOW,
        mode=NotificationEvaluationMode.LIVE,
    )

    assert mutation.fact is not None
    assert mutation.fact.kind == "result_acceptance_recorded"
    assert mutation.fact.priority is NotificationPriority.PROGRESS
    assert mutation.fact.transition is NotificationTransition.COMPLETED
    assert mutation.fact.kind != NotificationKind.TASK_COMPLETED.value


def test_policy_is_deterministic_and_rejects_secret_like_observation_text() -> None:
    observation = _observation()
    first = _policy().evaluate(observation, now=NOW)
    replay = _policy().evaluate(observation, now=NOW)

    assert first.fact is not None
    assert replay.fact is not None
    assert first.fact.id == replay.fact.id
    assert first.fact.semantic_fingerprint == replay.fact.semantic_fingerprint
    assert first.policy_state.policy_version == 1
    with pytest.raises(ValidationError, match="secret-like"):
        _observation_with_title("TOKEN_SHOULD_NOT_LEAK")


def test_blocked_grace_records_due_state_and_emits_only_after_requery() -> None:
    policy = _policy()
    blocked = _observation(
        kind=NotificationKind.TASK_BLOCKED,
        status="blocked",
        automatic_recovery=True,
    )
    first = policy.evaluate(blocked, now=NOW)
    assert first.fact is None
    assert first.policy_state.first_observed_at == NOW
    assert first.policy_state.not_before == NOW + timedelta(seconds=300)
    assert policy.due_condition(first.policy_state, now=NOW).status is NotificationDueStatus.NOT_DUE

    before_due = policy.evaluate(
        blocked,
        state=first.policy_state,
        now=NOW + timedelta(seconds=299),
    )
    assert before_due.fact is None
    due = policy.evaluate(
        blocked,
        state=before_due.policy_state,
        now=NOW + timedelta(seconds=300),
    )
    assert due.fact is not None
    assert due.fact.kind == NotificationKind.TASK_BLOCKED.value
    assert due.fact.priority is NotificationPriority.ACTION
    assert policy.due_condition(due.policy_state, now=NOW + timedelta(seconds=300)).status is NotificationDueStatus.NOT_DUE


def test_blocked_fingerprint_change_restarts_grace_and_recovery_before_due_is_silent() -> None:
    policy = _policy()
    first = policy.evaluate(
        _observation(
            kind=NotificationKind.TASK_BLOCKED,
            status="blocked",
            automatic_recovery=True,
        ),
        now=NOW,
    )
    changed = policy.evaluate(
        _observation(
            kind=NotificationKind.TASK_BLOCKED,
            status="blocked",
            fingerprint="b" * 64,
            automatic_recovery=True,
        ),
        state=first.policy_state,
        now=NOW + timedelta(seconds=100),
    )
    assert changed.fact is None
    assert changed.policy_state.first_observed_at == NOW + timedelta(seconds=100)
    assert changed.policy_state.not_before == NOW + timedelta(seconds=400)

    recovered = policy.evaluate(
        _observation(
            kind=NotificationKind.TASK_BLOCKED,
            status="running",
            transition=NotificationTransition.RECOVERED,
            fingerprint="c" * 64,
            automatic_recovery=True,
        ),
        state=changed.policy_state,
        now=NOW + timedelta(seconds=200),
    )
    assert recovered.fact is None
    assert recovered.policy_state.first_observed_at is None
    assert recovered.policy_state.not_before is None


def test_recovery_after_notified_action_is_a_single_progress_fact() -> None:
    policy = _policy()
    blocked = _observation(
        kind=NotificationKind.TASK_BLOCKED,
        status="blocked",
        automatic_recovery=False,
    )
    opened = policy.evaluate(blocked, now=NOW)
    assert opened.fact is not None
    recovered = policy.evaluate(
        _observation(
            kind=NotificationKind.TASK_BLOCKED,
            status="running",
            transition=NotificationTransition.RECOVERED,
            fingerprint="b" * 64,
        ),
        state=opened.policy_state,
        now=NOW + timedelta(minutes=1),
    )
    assert recovered.fact is not None
    assert recovered.fact.priority is NotificationPriority.PROGRESS
    assert recovered.fact.transition is NotificationTransition.RECOVERED
    assert recovered.thread_business_status is NotificationThreadBusinessStatus.RESOLVED
    replay = policy.evaluate(
        _observation(
            kind=NotificationKind.TASK_BLOCKED,
            status="running",
            transition=NotificationTransition.RECOVERED,
            fingerprint="b" * 64,
        ),
        state=recovered.policy_state,
        now=NOW + timedelta(minutes=2),
    )
    assert replay.fact is None


def test_milestone_difference_and_root_completion_are_sparse() -> None:
    policy = _policy()
    milestone_one = NotificationMilestone(key="m1", title="数据库迁移", completed=True)
    first = policy.evaluate(
        _observation(
            kind=NotificationKind.TASK_MILESTONE_COMPLETED,
            topic="milestone:m1",
            status="running",
            completed_milestones=(milestone_one,),
        ),
        now=NOW,
    )
    assert first.fact is not None
    assert first.fact.priority is NotificationPriority.PROGRESS
    assert first.fact.transition is NotificationTransition.COMPLETED

    duplicate = policy.evaluate(
        _observation(
            kind=NotificationKind.TASK_MILESTONE_COMPLETED,
            topic="milestone:m1",
            status="running",
            completed_milestones=(milestone_one,),
        ),
        state=first.policy_state,
        now=NOW + timedelta(seconds=1),
    )
    assert duplicate.fact is None

    ordinary_subtask = policy.evaluate(
        _observation(kind="subtask_completed", status="completed"),
        now=NOW,
    )
    assert ordinary_subtask.fact is None

    root = policy.evaluate(
        _observation(kind=NotificationKind.TASK_COMPLETED, status="completed"),
        now=NOW,
    )
    assert root.fact is not None
    assert root.fact.priority is NotificationPriority.PROGRESS
    root_replay = policy.evaluate(
        _observation(kind=NotificationKind.TASK_COMPLETED, status="completed"),
        state=root.policy_state,
        now=NOW + timedelta(seconds=1),
    )
    assert root_replay.fact is None


def test_baseline_is_web_only_and_operations_never_route_to_failing_channel() -> None:
    baseline = _policy().evaluate(
        _observation(
            kind=NotificationKind.EXECUTION_ATTENTION,
            baseline_import=True,
            source_type="akashic_private",
            origin_route_active=True,
        ),
        now=NOW,
        mode=NotificationEvaluationMode.RESYNC,
    )
    assert baseline.fact is not None
    assert baseline.fact.baseline_import is True
    assert baseline.route_eligibility is NotificationRouteEligibility.WEB_ONLY
    assert baseline.external_delivery_forbidden is True

    operations = _policy().evaluate(
        _observation(
            kind=NotificationKind.NOTIFICATION_CHANNEL_HEALTH,
            topic="notification_channel_health:opaque-route",
            source_type="akashic_private",
            origin_route_active=True,
        ),
        now=NOW,
    )
    assert operations.fact is not None
    assert operations.fact.priority is NotificationPriority.OPERATIONS
    assert operations.thread_business_status is NotificationThreadBusinessStatus.OPEN
    assert operations.route_eligibility is NotificationRouteEligibility.WEB_ONLY


def test_baseline_does_not_replay_recovery_after_a_prior_notification() -> None:
    policy = _policy()
    blocked = policy.evaluate(
        _observation(
            kind=NotificationKind.TASK_BLOCKED,
            status="blocked",
            automatic_recovery=False,
        ),
        now=NOW,
    )
    assert blocked.fact is not None

    baseline = policy.evaluate(
        _observation(
            kind=NotificationKind.TASK_BLOCKED,
            status="running",
            fingerprint="b" * 64,
            transition=NotificationTransition.RECOVERED,
            baseline_import=True,
        ),
        state=blocked.policy_state,
        now=NOW + timedelta(minutes=1),
        mode=NotificationEvaluationMode.RESYNC,
    )

    assert baseline.fact is None
    assert baseline.policy_state.not_before is None


@pytest.mark.parametrize(
    "kind",
    (
        "worker_step",
        "trace_chunk",
        "tool_call",
        "heartbeat",
        "token_usage",
        "retry_queued",
        "notification_thread",
        "notification_delivery",
    ),
)
def test_non_actionable_and_notification_self_observations_are_silent(kind: str) -> None:
    mutation = _policy().evaluate(
        _observation(kind=kind, source_projection_type=kind),
        now=NOW,
    )
    assert mutation.fact is None


def test_requires_operator_attention_is_actionable_even_with_generic_kind() -> None:
    attention = NotificationAttention(
        kind="runtime_configuration_missing",
        resource_id=SOURCE_RESOURCE_ID,
        resource_version=4,
        requires_operator=True,
        safe_path=("支付平台迁移", "后端", "回调幂等"),
    )
    mutation = _policy().evaluate(
        _observation(
            kind="attention",
            requires_operator=False,
            attention_items=(attention,),
        ),
        now=NOW,
    )
    assert mutation.fact is not None
    assert mutation.fact.priority is NotificationPriority.ACTION
    assert mutation.fact.kind == NotificationKind.EXECUTION_ATTENTION.value


def _observation_with_title(title: str) -> NotificationObservation:
    return replace(_observation(), root_task_title=title)
