from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.domain.notifications import (
    InboxThread,
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
)
from sidera_control_plane.persistence.models import (
    NotificationDeliveryAttemptModel,
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationPolicyStateModel,
    NotificationReconciliationCursorModel,
    NotificationRouteModel,
    NotificationSourceBindingModel,
    NotificationThreadModel,
)
from sidera_control_plane.persistence.notification_repository import (
    _attempt_to_domain,
    _attempt_to_model_values,
    _binding_to_domain,
    _binding_to_model_values,
    _cursor_to_domain,
    _cursor_to_model_values,
    _fact_to_domain,
    _fact_to_model_values,
    _intent_to_domain,
    _intent_to_model_values,
    _policy_state_to_domain,
    _policy_state_to_model_values,
    _route_to_domain,
    _route_to_model_values,
    _thread_to_domain,
    _thread_to_model_values,
)

NOW = datetime(2026, 9, 4, 13, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("10000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("20000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("30000000-0000-0000-0000-000000000001")
SOURCE_RESOURCE_ID = UUID("40000000-0000-0000-0000-000000000001")
FACT_ID = UUID("50000000-0000-0000-0000-000000000001")
THREAD_ID = UUID("60000000-0000-0000-0000-000000000001")
ROUTE_ID = UUID("70000000-0000-0000-0000-000000000001")
MODULE_INSTANCE_ID = UUID("80000000-0000-0000-0000-000000000001")
INTENT_ID = UUID("90000000-0000-0000-0000-000000000001")
CLAIM_TOKEN = UUID("a0000000-0000-0000-0000-000000000001")


def _fact() -> NotificationFact:
    return NotificationFact(
        id=FACT_ID,
        workspace_id=WORKSPACE_ID,
        policy_version=1,
        recipient_type="workspace_operator",
        recipient_id="local",
        kind="execution_attention",
        priority=NotificationPriority.ACTION,
        transition=NotificationTransition.OPENED,
        topic="execution_attention",
        root_task_id=ROOT_TASK_ID,
        subtask_id=SUBTASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        source_projection_cursor=12,
        source_projection_type="task_overview",
        source_resource_id=SOURCE_RESOURCE_ID,
        source_resource_version=4,
        semantic_fingerprint="a" * 64,
        title="支付平台迁移",
        summary="任务需要关注。",
        action="打开任务查看",
        deep_link=f"/tasks/{ROOT_TASK_ID}",
        correlation_id="correlation-1",
        baseline_import=False,
        occurred_at=NOW,
        created_at=NOW,
    )


def _thread() -> InboxThread:
    return InboxThread(
        id=THREAD_ID,
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        thread_key="workspace_operator/local:root_task/10000000-0000-0000-0000-000000000001",
        topic="execution_attention",
        root_task_id=ROOT_TASK_ID,
        subtask_id=SUBTASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        latest_fact_id=FACT_ID,
        business_status=NotificationThreadBusinessStatus.OPEN,
        organization_status=NotificationThreadOrganizationStatus.ACTIVE,
        last_read_fact_id=None,
        unread_count=1,
        version=1,
        opened_at=NOW,
        resolved_at=None,
        archived_at=None,
        updated_at=NOW,
    )


def _policy_state() -> NotificationPolicyState:
    return NotificationPolicyState(
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        subject_type="root_task",
        subject_id=str(ROOT_TASK_ID),
        topic="execution_attention",
        policy_version=1,
        observed_status="attention",
        observed_fingerprint="b" * 64,
        observed_payload={"kind": "execution_attention", "resource_version": 4},
        first_observed_at=None,
        not_before=None,
        last_notified_fingerprint="b" * 64,
        active_thread_id=THREAD_ID,
        resource_version=2,
        updated_at=NOW,
    )


def _route() -> NotificationRoute:
    return NotificationRoute(
        id=ROUTE_ID,
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=MODULE_INSTANCE_ID,
        source_ref="src_" + "x" * 32,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )


def _binding() -> NotificationSourceBinding:
    return NotificationSourceBinding(
        workspace_id=WORKSPACE_ID,
        resource_type="root_task",
        resource_id=ROOT_TASK_ID,
        notification_route_id=ROUTE_ID,
        binding_kind=NotificationBindingKind.ORIGIN,
        bound_at=NOW,
    )


def _intent() -> NotificationDeliveryIntent:
    return NotificationDeliveryIntent(
        id=INTENT_ID,
        workspace_id=WORKSPACE_ID,
        notification_fact_id=FACT_ID,
        notification_route_id=ROUTE_ID,
        channel_module_instance_id=MODULE_INSTANCE_ID,
        delivery_round=1,
        contract_version=1,
        status=NotificationDeliveryStatus.PENDING,
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


def _attempt() -> NotificationDeliveryAttempt:
    return NotificationDeliveryAttempt.open(
        delivery_intent_id=INTENT_ID,
        attempt_number=1,
        claim_token=CLAIM_TOKEN,
        fencing_token=1,
        started_at=NOW,
    )


def _cursor() -> NotificationReconciliationCursor:
    return NotificationReconciliationCursor(
        workspace_id=WORKSPACE_ID,
        policy_version=1,
        last_projection_cursor=12,
        last_reconciled_at=NOW,
        lease_owner=None,
        lease_until=None,
        version=1,
    )


@pytest.mark.parametrize(
    ("record_factory", "to_values", "to_domain", "model_type"),
    (
        (
            _policy_state,
            _policy_state_to_model_values,
            _policy_state_to_domain,
            NotificationPolicyStateModel,
        ),
        (_fact, _fact_to_model_values, _fact_to_domain, NotificationFactModel),
        (_thread, _thread_to_model_values, _thread_to_domain, NotificationThreadModel),
        (_route, _route_to_model_values, _route_to_domain, NotificationRouteModel),
        (
            _binding,
            _binding_to_model_values,
            _binding_to_domain,
            NotificationSourceBindingModel,
        ),
        (
            _intent,
            _intent_to_model_values,
            _intent_to_domain,
            NotificationDeliveryIntentModel,
        ),
        (
            _attempt,
            _attempt_to_model_values,
            _attempt_to_domain,
            NotificationDeliveryAttemptModel,
        ),
        (
            _cursor,
            _cursor_to_model_values,
            _cursor_to_domain,
            NotificationReconciliationCursorModel,
        ),
    ),
)
def test_notification_records_round_trip_without_orm_leak(
    record_factory,
    to_values,
    to_domain,
    model_type,
) -> None:
    record = record_factory()
    values = to_values(record)
    model = model_type(**values)

    restored = to_domain(model)

    assert restored == record
    assert not isinstance(restored, model_type)


def test_mapping_persists_enum_wire_values_and_copies_observed_payload() -> None:
    state = _policy_state()
    values = _policy_state_to_model_values(state)

    assert values["policy_version"] == 1
    assert values["observed_payload"] == dict(state.observed_payload)
    assert values["observed_payload"] is not state.observed_payload
    values["observed_payload"]["resource_version"] = 99
    assert state.observed_payload["resource_version"] == 4

    fact_values = _fact_to_model_values(_fact())
    assert fact_values["priority"] == "action"
    assert fact_values["transition"] == "opened"
    assert fact_values["baseline_import"] is False


def test_mapping_copies_nested_frozen_observed_payload_to_json_containers() -> None:
    state = NotificationPolicyState(
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        subject_type="root_task",
        subject_id=str(ROOT_TASK_ID),
        topic="execution_attention",
        policy_version=1,
        observed_status="blocked",
        observed_fingerprint="c" * 64,
        observed_payload={
            "attention": [{"requires_operator": True, "labels": ("ops",)}]
        },
        first_observed_at=NOW,
        not_before=NOW,
        last_notified_fingerprint=None,
        active_thread_id=None,
        resource_version=1,
        updated_at=NOW,
    )

    payload = _policy_state_to_model_values(state)["observed_payload"]

    assert payload == {"attention": [{"requires_operator": True, "labels": ["ops"]}]}
    assert isinstance(payload, dict)
    assert isinstance(payload["attention"], list)


def test_mapping_keeps_nullable_delivery_and_policy_values_as_null() -> None:
    policy_values = _policy_state_to_model_values(_policy_state())
    intent_values = _intent_to_model_values(_intent())
    attempt_values = _attempt_to_model_values(_attempt())
    thread_values = _thread_to_model_values(_thread())

    assert policy_values["first_observed_at"] is None
    assert policy_values["not_before"] is None
    assert intent_values["claimed_until"] is None
    assert intent_values["claim_token"] is None
    assert intent_values["delivered_at"] is None
    assert intent_values["exhausted_at"] is None
    assert attempt_values["outcome"] is None
    assert attempt_values["finished_at"] is None
    assert thread_values["last_read_fact_id"] is None
    assert thread_values["resolved_at"] is None
    assert thread_values["archived_at"] is None
