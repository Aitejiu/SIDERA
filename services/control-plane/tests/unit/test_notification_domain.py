from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    StaleFencingToken,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.notifications import (
    InboxThread,
    NotificationAttemptErrorCode,
    NotificationBindingKind,
    NotificationDeliveryAttempt,
    NotificationDeliveryIntent,
    NotificationDeliveryStatus,
    NotificationFact,
    NotificationPolicyState,
    NotificationPriority,
    NotificationReconciliationCursor,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationSourceBinding,
    NotificationThreadBusinessStatus,
    NotificationThreadOrganizationStatus,
    NotificationTransition,
    normalize_notification_text,
    notification_fact_id,
    notification_identity_name,
    notification_semantic_fingerprint,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
ROOT_TASK_ID = uuid4()
SUBTASK_ID = uuid4()
WORKER_ITEM_ID = uuid4()


def _identity_values() -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "recipient_type": "workspace_operator",
        "recipient_id": "local",
        "policy_version": 1,
        "root_task_id": ROOT_TASK_ID,
        "subtask_id": SUBTASK_ID,
        "worker_item_id": WORKER_ITEM_ID,
        "topic": "execution_attention",
        "transition": NotificationTransition.OPENED,
        "authoritative_fingerprint": "a" * 64,
    }


def _fact(
    *,
    priority: NotificationPriority = NotificationPriority.ACTION,
    transition: NotificationTransition = NotificationTransition.OPENED,
    topic: str = "execution_attention",
    authoritative_fingerprint: str = "a" * 64,
    baseline_import: bool = False,
) -> NotificationFact:
    identity_values = {
        **_identity_values(),
        "transition": transition,
        "topic": topic,
        "authoritative_fingerprint": authoritative_fingerprint,
    }
    identity = notification_identity_name(**identity_values)
    return NotificationFact(
        id=notification_fact_id(**identity_values),
        workspace_id=WORKSPACE_ID,
        policy_version=1,
        recipient_type="workspace_operator",
        recipient_id="local",
        kind="task_needs_user",
        priority=priority,
        transition=transition,
        topic=topic,
        root_task_id=ROOT_TASK_ID,
        subtask_id=SUBTASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        source_projection_cursor=12,
        source_projection_type="task_overview",
        source_resource_id=ROOT_TASK_ID,
        source_resource_version=4,
        semantic_fingerprint=sha256(identity.encode("utf-8")).hexdigest(),
        title="Implement notification gateway",
        summary="子任务需要处理。",
        action="打开任务查看",
        deep_link="/workspaces/example/tasks/example",
        correlation_id="correlation-1",
        baseline_import=baseline_import,
        occurred_at=NOW,
        created_at=NOW,
    )


def _intent(*, status: NotificationDeliveryStatus = NotificationDeliveryStatus.PENDING) -> NotificationDeliveryIntent:
    return NotificationDeliveryIntent(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        notification_fact_id=_fact().id,
        notification_route_id=uuid4(),
        channel_module_instance_id=uuid4(),
        delivery_round=1,
        contract_version=1,
        status=status,
        attempts=0,
        next_attempt_at=NOW,
        claimed_until=None,
        claim_token=None,
        fencing_token=1,
        provider_message_ref=None,
        last_error_code=None,
        delivered_at=None,
        exhausted_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_notification_identity_matches_the_frozen_uuid5_formula() -> None:
    values = _identity_values()

    name = notification_identity_name(**values)
    expected_name = (
        f"sidera:notification:{WORKSPACE_ID}:"
        f"workspace_operator/local:1:"
        f"root_task/{ROOT_TASK_ID}/subtask/{SUBTASK_ID}/worker_item/{WORKER_ITEM_ID}:"
        "execution_attention:opened:"
        f"{'a' * 64}"
    )
    expected_fingerprint = sha256(expected_name.encode("utf-8")).hexdigest()

    assert name == expected_name
    assert notification_fact_id(**values) == uuid5(NAMESPACE_URL, expected_name)
    assert notification_semantic_fingerprint(**values) == expected_fingerprint

    for field in (
        "recipient_id",
        "policy_version",
        "root_task_id",
        "topic",
        "transition",
        "authoritative_fingerprint",
    ):
        changed = dict(values)
        if field == "recipient_id":
            changed[field] = "other"
        elif field == "policy_version":
            changed[field] = 2
        elif field == "root_task_id":
            changed[field] = uuid4()
        elif field == "topic":
            changed[field] = "planning"
        elif field == "transition":
            changed[field] = NotificationTransition.UPDATED
        else:
            changed[field] = "b" * 64
        assert notification_fact_id(**changed) != notification_fact_id(**values)
        assert notification_semantic_fingerprint(**changed) != (
            notification_semantic_fingerprint(**values)
        )


def test_notification_entities_reject_invalid_values() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        replace(_fact(), occurred_at=datetime(2026, 9, 4, 12, 0))  # noqa: DTZ001
    with pytest.raises(ValidationError, match="recipient_id"):
        replace(_fact(), recipient_id=" ")
    with pytest.raises(ValidationError, match="source_resource_version"):
        replace(_fact(), source_resource_version=0)
    with pytest.raises(ValidationError, match="semantic_fingerprint"):
        replace(_fact(), semantic_fingerprint="bad")
    with pytest.raises(ValidationError, match="control"):
        replace(_fact(), title="bad\x00title")
    with pytest.raises(ValidationError, match="secret"):
        replace(_fact(), summary="TOKEN_SHOULD_NOT_LEAK")

    with pytest.raises(ValidationError, match="source_ref"):
        NotificationRoute(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            recipient_type="workspace_operator",
            recipient_id="local",
            channel_type="akashic_private",
            channel_module_instance_id=uuid4(),
            source_ref=" ",
            status=NotificationRouteStatus.ACTIVE,
            version=1,
            registered_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(ValidationError, match="provider_message_ref"):
        replace(
            NotificationDeliveryAttempt.open(
                delivery_intent_id=uuid4(),
                attempt_number=1,
                claim_token=uuid4(),
                fencing_token=1,
                started_at=NOW,
            ),
            provider_message_ref="x" * 513,
        )


def test_policy_state_binding_cursor_and_source_binding_validate() -> None:
    state = NotificationPolicyState(
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        subject_type="root_task",
        subject_id=str(ROOT_TASK_ID),
        topic="execution_attention",
        policy_version=1,
        observed_status="blocked",
        observed_fingerprint="a" * 64,
        observed_payload={"resource_version": 4},
        first_observed_at=NOW,
        not_before=NOW + timedelta(minutes=5),
        last_notified_fingerprint=None,
        active_thread_id=None,
        resource_version=1,
        updated_at=NOW,
    )
    route = NotificationRoute(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=uuid4(),
        source_ref="opaque-route-ref",
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )
    binding = NotificationSourceBinding(
        workspace_id=WORKSPACE_ID,
        resource_type="root_task",
        resource_id=ROOT_TASK_ID,
        notification_route_id=route.id,
        binding_kind=NotificationBindingKind.ORIGIN,
        bound_at=NOW,
    )
    cursor = NotificationReconciliationCursor(
        workspace_id=WORKSPACE_ID,
        policy_version=1,
        last_projection_cursor=0,
        last_reconciled_at=NOW,
        lease_owner=None,
        lease_until=None,
        version=1,
    )

    assert state.observed_payload["resource_version"] == 4
    assert binding.binding_kind is NotificationBindingKind.ORIGIN
    assert cursor.last_projection_cursor == 0

    with pytest.raises(ValidationError, match="policy_version"):
        replace(state, policy_version=0)
    with pytest.raises(ValidationError, match="version"):
        replace(route, version=0)
    with pytest.raises(ValidationError, match="fencing"):
        replace(_intent(), fencing_token=0)


def test_first_action_and_progress_facts_open_or_resolve_threads() -> None:
    action_thread = InboxThread.open_from_fact(_fact(), thread_id=uuid4(), now=NOW)
    progress_fact = _fact(
        priority=NotificationPriority.PROGRESS,
        transition=NotificationTransition.COMPLETED,
        topic="milestone:m1",
    )
    progress_thread = InboxThread.open_from_fact(
        progress_fact, thread_id=uuid4(), now=NOW
    )

    assert action_thread.business_status is NotificationThreadBusinessStatus.OPEN
    assert action_thread.organization_status is NotificationThreadOrganizationStatus.ACTIVE
    assert action_thread.unread_count == 1
    assert progress_thread.business_status is NotificationThreadBusinessStatus.RESOLVED
    assert progress_thread.unread_count == 1


def test_thread_updates_read_archives_and_reopens_without_mutating_old_value() -> None:
    first = _fact()
    thread = InboxThread.open_from_fact(first, thread_id=uuid4(), now=NOW)
    second = _fact(
        transition=NotificationTransition.UPDATED,
        authoritative_fingerprint="b" * 64,
    )
    updated = thread.apply_fact(second, now=NOW + timedelta(seconds=1))

    assert thread.latest_fact_id == first.id
    assert updated.latest_fact_id == second.id
    assert updated.unread_count == 2
    assert updated.version == thread.version + 1

    read = updated.mark_read(
        expected_version=updated.version,
        expected_latest_fact_id=second.id,
        now=NOW + timedelta(seconds=2),
    )
    assert read.unread_count == 0
    assert read.last_read_fact_id == second.id
    with pytest.raises(VersionConflict):
        updated.mark_read(
            expected_version=updated.version - 1,
            expected_latest_fact_id=second.id,
            now=NOW,
        )
    with pytest.raises(InvalidTransition, match="latest"):
        updated.mark_read(
            expected_version=updated.version,
            expected_latest_fact_id=uuid4(),
            now=NOW,
        )

    with pytest.raises(InvalidTransition, match="open"):
        thread.archive(now=NOW)
    recovered = read.apply_fact(
        _fact(
            transition=NotificationTransition.RECOVERED,
            authoritative_fingerprint="c" * 64,
        ),
        now=NOW + timedelta(seconds=3),
    )
    assert recovered.business_status is NotificationThreadBusinessStatus.RESOLVED
    assert recovered.unread_count == 1
    archived = recovered.archive(now=NOW + timedelta(seconds=4))
    assert archived.organization_status is NotificationThreadOrganizationStatus.ARCHIVED
    reopened = archived.apply_fact(
        _fact(
            transition=NotificationTransition.UPDATED,
            authoritative_fingerprint="d" * 64,
        ),
        now=NOW + timedelta(seconds=5),
    )
    assert reopened.organization_status is NotificationThreadOrganizationStatus.ACTIVE
    assert reopened.business_status is NotificationThreadBusinessStatus.OPEN
    assert reopened.unread_count == 2

    with pytest.raises(ValidationError, match="topic"):
        thread.apply_fact(_fact(topic="different-topic"), now=NOW)


def test_delivery_claim_settle_retry_reap_suppress_and_requeue() -> None:
    intent = _intent()
    claim_token = uuid4()
    claim = intent.claim(
        claim_token=claim_token,
        fencing_token=1,
        claimed_until=NOW + timedelta(seconds=30),
        now=NOW,
    )
    assert claim.intent.status is NotificationDeliveryStatus.CLAIMED
    assert claim.attempt.attempt_number == 1
    settled_attempt = claim.attempt.settle(
        outcome="retryable_failure",
        error_code=NotificationAttemptErrorCode.TIMEOUT,
        now=NOW + timedelta(seconds=1),
    )
    retry_wait = claim.intent.nack(
        claim_token=claim_token,
        fencing_token=1,
        outcome=settled_attempt.outcome,
        error_code=settled_attempt.error_code,
        next_attempt_at=NOW + timedelta(seconds=10),
        now=NOW + timedelta(seconds=1),
    )
    assert retry_wait.status is NotificationDeliveryStatus.RETRY_WAIT

    reclaimed = retry_wait.claim(
        claim_token=uuid4(),
        fencing_token=2,
        claimed_until=NOW + timedelta(seconds=40),
        now=NOW + timedelta(seconds=10),
    )
    expired = reclaimed.intent.expire_claim(now=NOW + timedelta(seconds=41))
    assert expired.status is NotificationDeliveryStatus.RETRY_WAIT
    assert expired.fencing_token == 3

    terminal_claim = expired.claim(
        claim_token=uuid4(),
        fencing_token=3,
        claimed_until=NOW + timedelta(seconds=70),
        now=NOW + timedelta(seconds=42),
    )
    exhausted = terminal_claim.intent.nack(
        claim_token=terminal_claim.attempt.claim_token,
        fencing_token=3,
        outcome="terminal_failure",
        error_code=NotificationAttemptErrorCode.PERMISSION_DENIED,
        next_attempt_at=None,
        now=NOW + timedelta(seconds=43),
    )
    assert exhausted.status is NotificationDeliveryStatus.EXHAUSTED

    next_round = exhausted.next_delivery_round(now=NOW + timedelta(seconds=44))
    assert exhausted.status is NotificationDeliveryStatus.EXHAUSTED
    assert next_round.delivery_round == 2
    assert next_round.status is NotificationDeliveryStatus.PENDING
    assert next_round.id != exhausted.id

    suppressed = _intent().suppress(
        reason=NotificationAttemptErrorCode.ROUTE_REVOKED,
        now=NOW,
    )
    assert suppressed.status is NotificationDeliveryStatus.SUPPRESSED


def test_delivery_ack_is_idempotent_and_stale_claims_are_rejected() -> None:
    intent = _intent()
    token = uuid4()
    claim = intent.claim(
        claim_token=token,
        fencing_token=1,
        claimed_until=NOW + timedelta(seconds=30),
        now=NOW,
    )
    delivered = claim.intent.ack(
        claim_token=token,
        fencing_token=1,
        provider_message_ref="provider-ref-1",
        now=NOW + timedelta(seconds=1),
    )
    duplicate = delivered.ack(
        claim_token=token,
        fencing_token=1,
        provider_message_ref="provider-ref-1",
        now=NOW + timedelta(seconds=2),
    )
    assert duplicate == delivered
    with pytest.raises(InvalidTransition):
        delivered.ack(
            claim_token=token,
            fencing_token=1,
            provider_message_ref="different-ref",
            now=NOW + timedelta(seconds=2),
        )

    retry = _intent().claim(
        claim_token=uuid4(),
        fencing_token=1,
        claimed_until=NOW + timedelta(seconds=1),
        now=NOW,
    )
    recovered = retry.intent.expire_claim(now=NOW + timedelta(seconds=2))
    with pytest.raises(StaleFencingToken):
        recovered.ack(
            claim_token=retry.attempt.claim_token,
            fencing_token=1,
            provider_message_ref="late",
            now=NOW + timedelta(seconds=3),
        )

    with pytest.raises(ValidationError, match="attempt_number"):
        NotificationDeliveryAttempt.open(
            delivery_intent_id=uuid4(),
            attempt_number=0,
            claim_token=uuid4(),
            fencing_token=1,
            started_at=NOW,
        )


def test_notification_text_normalization_is_unicode_safe_and_bounded() -> None:
    assert normalize_notification_text("  alpha\n\tbeta  ", maximum=32) == "alpha beta"
    assert normalize_notification_text("é" * 5, maximum=4) == "é..."
    with pytest.raises(ValidationError, match="maximum"):
        normalize_notification_text("x", maximum=0)


def test_route_and_delivery_error_values_are_stable() -> None:
    assert NotificationRouteStatus.REVOKED.value == "revoked"
    assert NotificationDeliveryStatus.RETRY_WAIT.value == "retry_wait"
    assert NotificationAttemptErrorCode.TRANSPORT_UNAVAILABLE.value == (
        "transport_unavailable"
    )
    assert NotificationAttemptErrorCode.UNKNOWN_RESULT.value == "unknown_result"
