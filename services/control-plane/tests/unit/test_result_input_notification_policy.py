from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.application.notification_policy import (
    NotificationEvaluationMode,
    NotificationObservation,
    NotificationPolicyV1,
    NotificationPolicyV2,
)
from sidera_control_plane.domain.notifications import (
    NotificationKind,
    NotificationPriority,
    NotificationThreadBusinessStatus,
    NotificationTransition,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("10000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("20000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("30000000-0000-0000-0000-000000000001")
SOURCE_RESOURCE_ID = UUID("40000000-0000-0000-0000-000000000001")


def _observation(
    *,
    kind: str = NotificationKind.RESULT_INPUTS_BLOCKED.value,
    status: str = "blocked",
    fingerprint: str = "a" * 64,
    topic: str = "result_inputs_blocked:result_input_unavailable",
    transition: NotificationTransition | None = None,
) -> NotificationObservation:
    return NotificationObservation(
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        subject_type="execution_request",
        subject_id="50000000-0000-0000-0000-000000000001",
        topic=topic,
        root_task_id=ROOT_TASK_ID,
        subtask_id=SUBTASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        source_projection_cursor=12,
        source_projection_type="executions",
        source_resource_id=SOURCE_RESOURCE_ID,
        source_resource_version=4,
        kind=kind,
        observed_status=status,
        authoritative_fingerprint=fingerprint,
        root_task_title="结果汇总",
        subtask_path=("汇总",),
        worker_item_title="生成摘要",
        deep_link=(
            f"/workspaces/{WORKSPACE_ID}/tasks/{ROOT_TASK_ID}"
            f"/worker-items/{WORKER_ITEM_ID}"
        ),
        correlation_id="result-input-correlation",
        transition=transition,
    )


def test_v2_owns_result_input_failure_and_emits_an_action_fact() -> None:
    mutation = NotificationPolicyV2().evaluate(_observation(), now=NOW)

    assert mutation.policy_state.policy_version == 2
    assert mutation.fact is not None
    assert mutation.fact.policy_version == 2
    assert mutation.fact.kind == NotificationKind.RESULT_INPUTS_BLOCKED.value
    assert mutation.fact.priority is NotificationPriority.ACTION
    assert mutation.fact.transition is NotificationTransition.OPENED
    assert mutation.thread_business_status is NotificationThreadBusinessStatus.OPEN
    assert "结果输入" in mutation.fact.summary


def test_v1_does_not_claim_result_input_failure() -> None:
    mutation = NotificationPolicyV1().evaluate(_observation(), now=NOW)

    assert mutation.fact is None
    assert mutation.policy_state.policy_version == 1


def test_v2_deduplicates_same_reason_and_recovers_after_the_open_fact() -> None:
    policy = NotificationPolicyV2()
    opened = policy.evaluate(_observation(), now=NOW)
    assert opened.fact is not None

    repeated = policy.evaluate(
        _observation(), state=opened.policy_state, now=NOW
    )
    assert repeated.fact is None

    recovered = policy.evaluate(
        _observation(
            status="ready",
            fingerprint="b" * 64,
            transition=NotificationTransition.RECOVERED,
        ),
        state=repeated.policy_state,
        now=NOW,
    )
    assert recovered.fact is not None
    assert recovered.fact.policy_version == 2
    assert recovered.fact.priority is NotificationPriority.PROGRESS
    assert recovered.fact.transition is NotificationTransition.RECOVERED
    assert recovered.thread_business_status is NotificationThreadBusinessStatus.RESOLVED


def test_v2_ignores_transient_input_group_waiting_progress() -> None:
    mutation = NotificationPolicyV2().evaluate(
        _observation(
            kind="input_group_waiting",
            topic="input_group_waiting:summary",
            status="waiting",
        ),
        now=NOW,
    )

    assert mutation.fact is None


def test_v1_still_notifies_an_unrelated_execution_failure() -> None:
    mutation = NotificationPolicyV1().evaluate(
        _observation(
            kind=NotificationKind.EXECUTION_FAILURE.value,
            topic="execution_failure",
            status="failed",
        ),
        now=NOW,
        mode=NotificationEvaluationMode.LIVE,
    )

    assert mutation.fact is not None
    assert mutation.fact.kind == NotificationKind.EXECUTION_FAILURE.value
