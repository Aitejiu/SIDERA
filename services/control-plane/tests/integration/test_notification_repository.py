from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.modules import (
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    NotificationChannelCapabilityProfile,
    NotificationReceiptCapability,
    StabilityLevel,
)
from sidera_control_plane.domain.notifications import (
    InboxThread,
    NotificationBindingKind,
    NotificationDeliveryAttempt,
    NotificationDeliveryIntent,
    NotificationDeliveryStatus,
    NotificationFact,
    NotificationPolicyState,
    NotificationPriority,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationSourceBinding,
    NotificationTransition,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    NotificationFactModel,
    NotificationReconciliationCursorModel,
    NotificationThreadModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)
OTHER_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000099")


@dataclass(frozen=True, slots=True)
class Seed:
    task: Task
    module_definition: ModuleDefinition
    module_instance: ModuleInstance


@pytest.fixture
def seed(uow_factory: Callable[[], SqlAlchemyUnitOfWork], workspace_id: UUID) -> Seed:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="通知网关集成测试",
        description="验证通知 Ledger 的事务和租户边界",
        acceptance_criteria=("Fact 和 Delivery 可恢复",),
        priority=50,
        requested_by="test:notification",
    )
    definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="notification_channel",
        provider="test-channel",
        interface_version="notification.v1",
        config_schema_version=1,
        capabilities=NotificationChannelCapabilityProfile(
            channel_types=("akashic_private",),
            delivery_contract_versions=(1,),
            idempotency=IdempotencyCapability.OPERATION_ID,
            receipt=NotificationReceiptCapability.PROVIDER_REF,
            max_message_bytes=16_384,
            supports_deep_link=True,
            stability=StabilityLevel.EXPERIMENTAL,
        ),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=definition.id,
        instance_name="test-notification-channel",
        config_version=1,
        config_namespace="notification",
        config={"notification.channel_type": "akashic_private"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    with uow_factory() as uow:
        uow.tasks.add(task)
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
    return Seed(task, definition, instance)


def make_fact(
    workspace_id: UUID, root_task_id: UUID, *, fingerprint: str = "a" * 64
) -> NotificationFact:
    return NotificationFact.create(
        workspace_id=workspace_id,
        policy_version=1,
        recipient_type="workspace_operator",
        recipient_id="local",
        kind="execution_attention",
        priority=NotificationPriority.ACTION,
        transition=NotificationTransition.OPENED,
        topic="execution_attention",
        root_task_id=root_task_id,
        subtask_id=None,
        worker_item_id=None,
        source_projection_cursor=12,
        source_projection_type="task_overview",
        source_resource_id=root_task_id,
        source_resource_version=1,
        authoritative_fingerprint=fingerprint,
        title="通知网关集成测试",
        summary="任务需要关注。",
        action="打开任务查看",
        deep_link=f"/tasks/{root_task_id}",
        correlation_id="notification-repository-test",
        baseline_import=False,
        occurred_at=NOW,
        created_at=NOW,
    )


def make_route(
    workspace_id: UUID, module_instance_id: UUID, *, source_ref_char: str = "r"
) -> NotificationRoute:
    return NotificationRoute(
        id=uuid4(),
        workspace_id=workspace_id,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=module_instance_id,
        source_ref="src_" + source_ref_char * 32,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )


def make_policy_state(
    workspace_id: UUID,
    root_task_id: UUID,
    *,
    active_thread_id: UUID | None = None,
    topic: str = "execution_attention",
    first_observed_at: datetime | None = None,
    not_before: datetime | None = None,
) -> NotificationPolicyState:
    return NotificationPolicyState(
        workspace_id=workspace_id,
        recipient_type="workspace_operator",
        recipient_id="local",
        subject_type="root_task",
        subject_id=str(root_task_id),
        topic=topic,
        policy_version=1,
        observed_status="attention",
        observed_fingerprint="a" * 64,
        observed_payload={"kind": "execution_attention", "resource_version": 1},
        first_observed_at=first_observed_at,
        not_before=not_before,
        last_notified_fingerprint="a" * 64,
        active_thread_id=active_thread_id,
        resource_version=1,
        updated_at=NOW,
    )


def make_intent(
    workspace_id: UUID,
    fact_id: UUID,
    route_id: UUID,
    channel_module_instance_id: UUID,
    *,
    delivery_round: int = 1,
    next_attempt_at: datetime = NOW,
) -> NotificationDeliveryIntent:
    return NotificationDeliveryIntent(
        id=uuid4(),
        workspace_id=workspace_id,
        notification_fact_id=fact_id,
        notification_route_id=route_id,
        channel_module_instance_id=channel_module_instance_id,
        delivery_round=delivery_round,
        contract_version=1,
        status=NotificationDeliveryStatus.PENDING,
        attempts=0,
        next_attempt_at=next_attempt_at,
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


def test_notification_repository_persists_and_scopes_all_records(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    seed: Seed,
) -> None:
    fact = make_fact(workspace_id, seed.task.id)
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)
    state = make_policy_state(workspace_id, seed.task.id, active_thread_id=thread.id)
    route = make_route(workspace_id, seed.module_instance.id)
    binding = NotificationSourceBinding(
        workspace_id=workspace_id,
        resource_type="root_task",
        resource_id=seed.task.id,
        notification_route_id=route.id,
        binding_kind=NotificationBindingKind.ORIGIN,
        bound_at=NOW,
    )
    intent = NotificationDeliveryIntent(
        id=uuid4(),
        workspace_id=workspace_id,
        notification_fact_id=fact.id,
        notification_route_id=route.id,
        channel_module_instance_id=seed.module_instance.id,
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

    with uow_factory() as uow:
        assert uow.notifications.add_fact_or_get_existing(fact) == fact
        uow.notifications.add_thread(thread)
        uow.notifications.add_policy_state(state)
        uow.notifications.add_route(route)
        assert uow.notifications.add_binding_or_get_existing(binding) == binding
        assert uow.notifications.add_intent_or_get_existing(intent) == intent
        attempt = NotificationDeliveryAttempt.open(
            delivery_intent_id=intent.id,
            attempt_number=1,
            claim_token=uuid4(),
            fencing_token=1,
            started_at=NOW,
        )
        uow.notifications.add_attempt(workspace_id, attempt)

    with uow_factory() as uow:
        assert uow.notifications.get_fact(OTHER_WORKSPACE_ID, fact.id) is None
        assert uow.notifications.get_thread(OTHER_WORKSPACE_ID, thread.id) is None
        assert (
            uow.notifications.get_policy_state(
                OTHER_WORKSPACE_ID,
                state.recipient_type,
                state.recipient_id,
                state.subject_type,
                state.subject_id,
                state.topic,
            )
            is None
        )
        assert uow.notifications.get_route(OTHER_WORKSPACE_ID, route.id) is None
        assert (
            uow.notifications.get_binding(
                OTHER_WORKSPACE_ID,
                binding.resource_type,
                binding.resource_id,
                binding.binding_kind.value,
            )
            is None
        )
        assert uow.notifications.get_intent(OTHER_WORKSPACE_ID, intent.id) is None
        assert uow.notifications.list_attempts(OTHER_WORKSPACE_ID, intent.id) == ()

        assert uow.notifications.get_fact(workspace_id, fact.id) == fact
        assert uow.notifications.get_thread(workspace_id, thread.id) == thread
        assert uow.notifications.get_route(workspace_id, route.id) == route
        assert uow.notifications.list_thread_facts(workspace_id, thread.id) == (fact,)
        assert uow.notifications.list_routes_for_recipient(
            workspace_id, route.recipient_type, route.recipient_id
        ) == (route,)
        assert uow.notifications.list_routes_for_resource(
            workspace_id, binding.resource_type, binding.resource_id
        ) == (route,)
        assert len(uow.notifications.list_attempts(workspace_id, intent.id)) == 1

        conflicting_route = make_route(
            workspace_id, seed.module_instance.id, source_ref_char="s"
        )
        uow.notifications.add_route(conflicting_route)
        with pytest.raises(ValidationError, match="conflicts with existing route"):
            uow.notifications.add_binding_or_get_existing(
                replace(binding, notification_route_id=conflicting_route.id)
            )


def test_notification_repository_rolls_back_ledger_records_atomically(
    database: Database,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    seed: Seed,
) -> None:
    fact = make_fact(workspace_id, seed.task.id)
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)

    with (
        pytest.raises(RuntimeError, match="rollback notification ledger"),
        uow_factory() as uow,
    ):
        uow.notifications.add_fact_or_get_existing(fact)
        uow.notifications.add_thread(thread)
        uow.notifications.get_or_create_cursor(workspace_id, 1, now=NOW)
        raise RuntimeError("rollback notification ledger")

    with database.session() as session:
        assert (
            session.scalar(select(func.count()).select_from(NotificationFactModel)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(NotificationThreadModel))
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(NotificationReconciliationCursorModel)
            )
            == 0
        )


def test_notification_repository_replays_exact_facts_and_rejects_conflicting_identity(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    seed: Seed,
) -> None:
    fact = make_fact(workspace_id, seed.task.id)
    with uow_factory() as uow:
        assert uow.notifications.add_fact_or_get_existing(fact) == fact
        assert uow.notifications.add_fact_or_get_existing(fact) == fact
        conflicting = NotificationFact(
            **{
                **fact.to_mapping(),
                "id": fact.id,
                "workspace_id": fact.workspace_id,
                "priority": fact.priority,
                "transition": fact.transition,
                "root_task_id": fact.root_task_id,
                "subtask_id": fact.subtask_id,
                "worker_item_id": fact.worker_item_id,
                "occurred_at": fact.occurred_at,
                "created_at": fact.created_at,
                "source_resource_id": fact.source_resource_id,
                "source_resource_version": fact.source_resource_version,
                "source_projection_cursor": fact.source_projection_cursor,
                "baseline_import": fact.baseline_import,
                "semantic_fingerprint": fact.semantic_fingerprint,
                "title": "changed title",
            }
        )
        with pytest.raises(ValidationError, match="immutable identity"):
            uow.notifications.add_fact_or_get_existing(conflicting)


def test_notification_repository_enforces_cas_for_policy_thread_route_and_intent(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    seed: Seed,
) -> None:
    fact = make_fact(workspace_id, seed.task.id)
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)
    state = make_policy_state(workspace_id, seed.task.id, active_thread_id=thread.id)
    route = make_route(workspace_id, seed.module_instance.id)
    intent = NotificationDeliveryIntent(
        id=uuid4(),
        workspace_id=workspace_id,
        notification_fact_id=fact.id,
        notification_route_id=route.id,
        channel_module_instance_id=seed.module_instance.id,
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
    with uow_factory() as uow:
        uow.notifications.add_fact_or_get_existing(fact)
        uow.notifications.add_thread(thread)
        uow.notifications.add_policy_state(state)
        uow.notifications.add_route(route)
        uow.notifications.add_intent_or_get_existing(intent)

        changed_state = state.with_observation(
            observed_status="running",
            observed_fingerprint="b" * 64,
            observed_payload={"kind": "execution_attention", "resource_version": 2},
            now=NOW + timedelta(seconds=1),
        )
        uow.notifications.save_policy_state(changed_state, state.resource_version)
        with pytest.raises(VersionConflict):
            uow.notifications.save_policy_state(changed_state, state.resource_version)

        changed_thread = thread.mark_read(
            expected_version=thread.version,
            expected_latest_fact_id=thread.latest_fact_id,
            now=NOW + timedelta(seconds=1),
        )
        uow.notifications.save_thread(changed_thread, thread.version)
        with pytest.raises(VersionConflict):
            uow.notifications.save_thread(changed_thread, thread.version)

        changed_route = route.with_status(
            NotificationRouteStatus.DRAINING, now=NOW + timedelta(seconds=1)
        )
        uow.notifications.save_route(changed_route, route.version)
        with pytest.raises(VersionConflict):
            uow.notifications.save_route(changed_route, route.version)

        claimed, attempt = intent.claim(
            claim_token=uuid4(),
            fencing_token=1,
            claimed_until=NOW + timedelta(minutes=5),
            now=NOW,
        )
        uow.notifications.save_intent(
            claimed, intent.fencing_token, NotificationDeliveryStatus.PENDING
        )
        uow.notifications.add_attempt(workspace_id, attempt)
        with pytest.raises(VersionConflict):
            uow.notifications.save_intent(
                claimed, intent.fencing_token, NotificationDeliveryStatus.PENDING
            )


def test_notification_repository_settles_attempt_once_and_keeps_rounds_auditable(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    seed: Seed,
) -> None:
    fact = make_fact(workspace_id, seed.task.id)
    route = make_route(workspace_id, seed.module_instance.id)
    intent = NotificationDeliveryIntent(
        id=uuid4(),
        workspace_id=workspace_id,
        notification_fact_id=fact.id,
        notification_route_id=route.id,
        channel_module_instance_id=seed.module_instance.id,
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
    with uow_factory() as uow:
        uow.notifications.add_fact_or_get_existing(fact)
        uow.notifications.add_route(route)
        uow.notifications.add_intent_or_get_existing(intent)
        claimed, attempt = intent.claim(
            claim_token=uuid4(),
            fencing_token=1,
            claimed_until=NOW + timedelta(minutes=5),
            now=NOW,
        )
        uow.notifications.save_intent(
            claimed, intent.fencing_token, NotificationDeliveryStatus.PENDING
        )
        uow.notifications.add_attempt(workspace_id, attempt)
        forged_attempt = replace(
            attempt,
            claim_token=uuid4(),
        ).settle(
            outcome="delivered",
            provider_message_ref="forged-provider-ref",
            now=NOW + timedelta(seconds=1),
        )
        with pytest.raises(ValidationError, match="stale"):
            uow.notifications.finish_attempt(workspace_id, forged_attempt)
        settled = attempt.settle(
            outcome="delivered",
            provider_message_ref="provider-ref-1",
            now=NOW + timedelta(seconds=1),
        )
        uow.notifications.finish_attempt(workspace_id, settled)
        with pytest.raises(ValidationError, match="already settled"):
            uow.notifications.finish_attempt(workspace_id, settled)
        delivered = claimed.ack(
            claim_token=claimed.claim_token,
            fencing_token=claimed.fencing_token,
            provider_message_ref="provider-ref-1",
            now=NOW + timedelta(seconds=1),
        )
        uow.notifications.save_intent(
            delivered, claimed.fencing_token, NotificationDeliveryStatus.CLAIMED
        )
        with pytest.raises(InvalidTransition, match="delivered"):
            delivered.suppress(reason="route_revoked", now=NOW + timedelta(seconds=2))
        with pytest.raises(VersionConflict):
            uow.notifications.save_intent(
                delivered, delivered.fencing_token, NotificationDeliveryStatus.PENDING
            )


def test_notification_repository_claims_cursor_and_due_rows_with_workspace_scope(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    seed: Seed,
) -> None:
    fact = make_fact(workspace_id, seed.task.id)
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)
    due_state = make_policy_state(
        workspace_id,
        seed.task.id,
        active_thread_id=thread.id,
        first_observed_at=NOW,
        not_before=NOW + timedelta(minutes=1),
    )
    route = make_route(workspace_id, seed.module_instance.id)
    with uow_factory() as uow:
        uow.notifications.add_fact_or_get_existing(fact)
        uow.notifications.add_thread(thread)
        uow.notifications.add_policy_state(due_state)
        uow.notifications.add_route(route)
        first_cursor = uow.notifications.get_or_create_cursor(workspace_id, 1, now=NOW)
        assert first_cursor.last_projection_cursor == 0
        claimed_cursor = uow.notifications.claim_cursor(
            workspace_id, 1, "worker-a", NOW, 30
        )
        assert claimed_cursor is not None

    with uow_factory() as uow:
        unleased = uow.notifications.get_or_create_cursor(workspace_id, 2, now=NOW)
        advanced = unleased.advance(cursor=1, now=NOW)
        with pytest.raises(VersionConflict):
            uow.notifications.save_cursor(advanced, unleased.version, "worker-b")

    with uow_factory() as uow:
        assert (
            uow.notifications.claim_cursor(
                workspace_id, 1, "worker-b", NOW + timedelta(seconds=1), 30
            )
            is None
        )
        due = uow.notifications.list_due_policy_states(
            workspace_id, NOW + timedelta(seconds=1), 10
        )
        assert due == ()

    with uow_factory() as uow:
        due = uow.notifications.list_due_policy_states(
            workspace_id, NOW + timedelta(minutes=1), 10
        )
        assert len(due) == 1
        assert due[0].workspace_id == workspace_id
        assert due[0].subject_id == str(seed.task.id)


def test_notification_repository_skip_locked_claims_disjoint_due_rows(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    seed: Seed,
) -> None:
    first_fact = make_fact(workspace_id, seed.task.id, fingerprint="b" * 64)
    second_fact = make_fact(workspace_id, seed.task.id, fingerprint="c" * 64)
    route = make_route(workspace_id, seed.module_instance.id)
    first_intent = make_intent(
        workspace_id, first_fact.id, route.id, seed.module_instance.id
    )
    second_intent = make_intent(
        workspace_id, second_fact.id, route.id, seed.module_instance.id
    )
    first_state = make_policy_state(
        workspace_id,
        seed.task.id,
        topic="execution_attention_first",
        first_observed_at=NOW,
        not_before=NOW,
    )
    second_state = make_policy_state(
        workspace_id,
        seed.task.id,
        topic="execution_attention_second",
        first_observed_at=NOW,
        not_before=NOW,
    )
    with uow_factory() as uow:
        uow.notifications.add_fact_or_get_existing(first_fact)
        uow.notifications.add_fact_or_get_existing(second_fact)
        uow.notifications.add_route(route)
        uow.notifications.add_intent_or_get_existing(first_intent)
        uow.notifications.add_intent_or_get_existing(second_intent)
        uow.notifications.add_policy_state(first_state)
        uow.notifications.add_policy_state(second_state)

    with uow_factory() as first_uow:
        first_states = first_uow.notifications.list_due_policy_states(
            workspace_id, NOW, 1, for_update_skip_locked=True
        )
        with uow_factory() as second_uow:
            second_states = second_uow.notifications.list_due_policy_states(
                workspace_id, NOW, 1, for_update_skip_locked=True
            )
            assert {item.topic for item in first_states}.isdisjoint(
                item.topic for item in second_states
            )

    with uow_factory() as first_uow:
        first_intents = first_uow.notifications.list_due_intents(
            workspace_id,
            seed.module_instance.id,
            NOW,
            1,
            for_update_skip_locked=True,
        )
        with uow_factory() as second_uow:
            second_intents = second_uow.notifications.list_due_intents(
                workspace_id,
                seed.module_instance.id,
                NOW,
                1,
                for_update_skip_locked=True,
            )
            assert {item.id for item in first_intents}.isdisjoint(
                item.id for item in second_intents
            )
            assert {
                *(item.id for item in first_intents),
                *(item.id for item in second_intents),
            } == {first_intent.id, second_intent.id}
