"""R6.3.3 notification gateway concurrency and failure matrix."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.notification_commands import (
    NotificationCommandHandler,
)
from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryService,
)
from sidera_control_plane.application.notification_policy import (
    NotificationObservation,
)
from sidera_control_plane.application.notification_reconciler import (
    NotificationReconciler,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import (
    StaleFencingToken,
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
    NotificationAttemptErrorCode,
    NotificationBindingKind,
    NotificationFact,
    NotificationPriority,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationSourceBinding,
    NotificationTransition,
)
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    ModuleInstanceModel,
    NotificationDeliveryAttemptModel,
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationPolicyStateModel,
    NotificationReconciliationCursorModel,
    NotificationThreadModel,
    ProjectionChangeModel,
    TaskModel,
)
from sidera_control_plane.persistence.notification_repository import (
    SqlAlchemyNotificationRepository,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_notification_delivery_concurrency import _seed_delivery
from tests.integration.test_notification_reconciliation import _seed_task_change

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)
PRIVATE_TEXT = "TOKEN_SHOULD_NOT_CROSS_NOTIFICATION_BOUNDARY"


def _ledger(database, workspace_id: UUID) -> tuple[int, int, int, int]:
    with database.session() as session:
        return (
            int(
                session.scalar(
                    select(func.count()).select_from(NotificationFactModel).where(
                        NotificationFactModel.workspace_id == workspace_id
                    )
                )
                or 0
            ),
            int(
                session.scalar(
                    select(func.count()).select_from(NotificationThreadModel).where(
                        NotificationThreadModel.workspace_id == workspace_id
                    )
                )
                or 0
            ),
            int(
                session.scalar(
                    select(func.count()).select_from(
                        NotificationDeliveryIntentModel
                    ).where(NotificationDeliveryIntentModel.workspace_id == workspace_id)
                )
                or 0
            ),
            int(
                session.scalar(
                    select(func.count()).select_from(
                        NotificationDeliveryAttemptModel
                    ).join(
                        NotificationDeliveryIntentModel,
                        NotificationDeliveryIntentModel.id
                        == NotificationDeliveryAttemptModel.delivery_intent_id,
                    ).where(NotificationDeliveryIntentModel.workspace_id == workspace_id)
                )
                or 0
            ),
        )


def _active_origin_route(
    database, workspace_id: UUID, root_task_id: UUID
) -> NotificationRoute:
    definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="notification_channel",
        provider="fault-matrix-channel",
        interface_version="sidera.notification.v1",
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
        instance_name="fault-matrix-channel",
        config_version=1,
        config_namespace="notification",
        config={"notification.mode": "gateway"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    route = NotificationRoute(
        id=uuid4(),
        workspace_id=workspace_id,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=instance.id,
        source_ref="src_" + uuid4().hex,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )
    binding = NotificationSourceBinding(
        workspace_id=workspace_id,
        resource_type="root_task",
        resource_id=root_task_id,
        notification_route_id=route.id,
        binding_kind=NotificationBindingKind.ORIGIN,
        bound_at=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        uow.flush()
        uow.notifications.add_route(route)
        uow.flush()
        uow.notifications.add_binding_or_get_existing(binding)
    return route


def _set_task_status(
    database,
    workspace_id: UUID,
    task_id: UUID,
    *,
    status: str,
    change_type: str,
) -> int:
    with database.session() as session:
        task = session.scalar(
            select(TaskModel).where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.id == task_id,
            )
        )
        if task is None:
            raise AssertionError(f"missing task {task_id}")
        version = task.version + 1
        session.execute(
            update(TaskModel)
            .where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.id == task_id,
            )
            .values(status=status, version=version)
        )
        session.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="task_overview",
            resource_id=task_id,
            change_type=change_type,
            resource_version=version,
            correlation_id=uuid4(),
        )
    return version


def _fact_variant(
    fact: NotificationFact,
    *,
    fingerprint: str,
    priority: NotificationPriority,
    transition: NotificationTransition,
    kind: str = "task_blocked",
    now: datetime,
) -> NotificationFact:
    return NotificationFact.create(
        workspace_id=fact.workspace_id,
        policy_version=fact.policy_version,
        recipient_type=fact.recipient_type,
        recipient_id=fact.recipient_id,
        kind=kind,
        priority=priority,
        transition=transition,
        topic=fact.topic,
        root_task_id=fact.root_task_id,
        subtask_id=fact.subtask_id,
        worker_item_id=fact.worker_item_id,
        source_projection_cursor=fact.source_projection_cursor + 1,
        source_projection_type=fact.source_projection_type,
        source_resource_id=fact.source_resource_id,
        source_resource_version=fact.source_resource_version + 1,
        authoritative_fingerprint=fingerprint,
        title=fact.title,
        summary=fact.summary,
        action=fact.action,
        deep_link=fact.deep_link,
        correlation_id=fact.correlation_id,
        baseline_import=False,
        occurred_at=now,
        created_at=now,
    )


def _requeue_command(
    workspace_id: UUID, intent_id: UUID, route_version: int
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type="RequeueNotificationDelivery",
        target_type="notification_delivery",
        actor_type="local",
        actor_id="operator",
        target_id=intent_id,
        expected_version=None,
        idempotency_key="requeue-" + uuid4().hex,
        correlation_id=uuid4(),
        payload={
            "expected_status": "exhausted",
            "expected_route_version": route_version,
            "reason": "fault matrix retry",
        },
    )


def test_reconciler_crash_before_commit_replays_one_exact_ledger(
    database, workspace_id, uow_factory, monkeypatch
) -> None:
    _seed_task_change(database, workspace_id, title="事务崩溃后重放")
    original = SqlAlchemyNotificationRepository.add_thread

    def fail_after_thread(self, thread):
        original(self, thread)
        raise RuntimeError("injected crash before commit")

    monkeypatch.setattr(SqlAlchemyNotificationRepository, "add_thread", fail_after_thread)
    failed = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="fault-crash"
    ).reconcile_workspace(workspace_id)

    assert failed.diagnostic_code == "notification_reconciliation_write_failed"
    assert _ledger(database, workspace_id) == (0, 0, 0, 0)

    monkeypatch.undo()
    replayed = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="fault-crash-replay"
    ).reconcile_workspace(workspace_id)
    repeated = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="fault-crash-replay-2"
    ).reconcile_workspace(workspace_id)

    assert replayed.facts_created == 1
    assert repeated.facts_created == 0
    assert _ledger(database, workspace_id) == (1, 1, 0, 0)


def test_deterministic_fact_collision_is_idempotent_in_reconciler(
    database, workspace_id, uow_factory, monkeypatch
) -> None:
    _seed_task_change(database, workspace_id, title="Fact 碰撞重放")
    original = SqlAlchemyNotificationRepository.add_fact_or_get_existing
    collisions = 0

    def force_deterministic_collision(self, fact):
        nonlocal collisions
        first = original(self, fact)
        second = original(self, fact)
        assert first == second
        collisions += 1
        return second

    monkeypatch.setattr(
        SqlAlchemyNotificationRepository,
        "add_fact_or_get_existing",
        force_deterministic_collision,
    )
    result = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="fact-collision"
    ).reconcile_workspace(workspace_id)

    assert result.diagnostic_code is None
    assert collisions == 1
    assert result.facts_created == 1
    assert _ledger(database, workspace_id) == (1, 1, 0, 0)


def test_live_due_recovery_and_duplicate_blocked_races_keep_one_thread_per_topic(
    database, workspace_id, uow_factory
) -> None:
    task = _seed_task_change(database, workspace_id, title="并发阻塞恢复")
    _active_origin_route(database, workspace_id, task.id)
    _set_task_status(
        database,
        workspace_id,
        task.id,
        status="blocked",
        change_type="task_blocked",
    )

    live = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="live-race"
    ).reconcile_workspace(workspace_id)
    assert live.facts_created == 0
    with database.session() as session:
        pending = session.scalar(
            select(NotificationPolicyStateModel).where(
                NotificationPolicyStateModel.workspace_id == workspace_id,
                NotificationPolicyStateModel.subject_id == str(task.id),
                NotificationPolicyStateModel.topic == "task_blocked",
            )
        )
        assert pending is not None
        assert pending.not_before == NOW + timedelta(seconds=300)

    due_at = NOW + timedelta(seconds=300)

    def scan(owner: str):
        return NotificationReconciler(
            uow_factory, clock=lambda: due_at, owner=owner
        ).scan_due(workspace_id, owner_id=owner, now=due_at)

    with ThreadPoolExecutor(max_workers=2) as executor:
        due_results = tuple(executor.map(scan, ("due-a", "due-b")))
    assert all(result.diagnostic_code is None for result in due_results)
    assert sum(result.facts_created for result in due_results) == 1
    assert _ledger(database, workspace_id) == (1, 1, 1, 0)

    _set_task_status(
        database,
        workspace_id,
        task.id,
        status="running",
        change_type="task_blocked_recovered",
    )

    def reconcile(owner: str):
        return NotificationReconciler(
            uow_factory, clock=lambda: due_at, owner=owner
        ).reconcile_workspace(workspace_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery_results = tuple(executor.map(reconcile, ("recovery-a", "recovery-b")))
    assert all(
        result.diagnostic_code in {None, "notification_cursor_busy"}
        for result in recovery_results
    )
    assert sum(result.facts_created for result in recovery_results) == 1
    assert _ledger(database, workspace_id) == (2, 1, 2, 0)

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="task_overview",
            resource_id=task.id,
            change_type="task_blocked_duplicate",
            resource_version=3,
            correlation_id=uuid4(),
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        duplicate_results = tuple(executor.map(reconcile, ("duplicate-a", "duplicate-b")))
    assert all(
        result.diagnostic_code in {None, "notification_cursor_busy"}
        for result in duplicate_results
    )
    assert sum(result.facts_created for result in duplicate_results) == 0
    assert _ledger(database, workspace_id) == (2, 1, 2, 0)


def test_mark_read_race_and_archive_reopen_preserve_thread_identity(
    database, workspace_id, uow_factory
) -> None:
    seed = _seed_delivery(uow_factory, workspace_id)
    with uow_factory() as uow:
        fact = uow.notifications.get_fact(workspace_id, seed.fact_id)
        assert fact is not None
        thread_id = uow.session.scalar(
            select(NotificationThreadModel.id).where(
                NotificationThreadModel.workspace_id == workspace_id,
                NotificationThreadModel.latest_fact_id == fact.id,
            )
        )
        assert thread_id is not None
        thread = uow.notifications.get_thread(workspace_id, thread_id, for_update=True)
        assert thread is not None
        stale_thread = thread

        changed_fact = _fact_variant(
            fact,
            fingerprint="c" * 64,
            priority=NotificationPriority.ACTION,
            transition=NotificationTransition.UPDATED,
            now=NOW + timedelta(seconds=1),
        )
        persisted = uow.notifications.add_fact_or_get_existing(changed_fact)
        changed_thread = thread.apply_fact(persisted, now=NOW + timedelta(seconds=1))
        uow.notifications.save_thread(changed_thread, thread.version)

        with pytest.raises(VersionConflict):
            changed_thread.mark_read(
                expected_version=stale_thread.version,
                expected_latest_fact_id=stale_thread.latest_fact_id,
                now=NOW + timedelta(seconds=2),
            )

        recovered_fact = _fact_variant(
            persisted,
            fingerprint="d" * 64,
            priority=NotificationPriority.PROGRESS,
            transition=NotificationTransition.RECOVERED,
            now=NOW + timedelta(seconds=2),
        )
        recovered = uow.notifications.add_fact_or_get_existing(recovered_fact)
        resolved_thread = changed_thread.apply_fact(recovered, now=NOW + timedelta(seconds=2))
        uow.notifications.save_thread(resolved_thread, changed_thread.version)

        archived_thread = resolved_thread.archive(now=NOW + timedelta(seconds=3))
        uow.notifications.save_thread(archived_thread, resolved_thread.version)

        reopened_fact = _fact_variant(
            recovered,
            fingerprint="e" * 64,
            priority=NotificationPriority.ACTION,
            transition=NotificationTransition.OPENED,
            now=NOW + timedelta(seconds=4),
        )
        reopened = uow.notifications.add_fact_or_get_existing(reopened_fact)
        reopened_thread = archived_thread.apply_fact(reopened, now=NOW + timedelta(seconds=4))
        uow.notifications.save_thread(reopened_thread, archived_thread.version)

    assert _ledger(database, workspace_id) == (4, 1, 1, 0)
    with database.session() as session:
        row = session.scalar(
            select(NotificationThreadModel).where(
                NotificationThreadModel.workspace_id == workspace_id
            )
        )
        assert row is not None
        assert row.business_status == "open"
        assert row.organization_status == "active"
        assert row.unread_count == 4
        assert row.version == 5


def test_claim_expiry_route_disable_and_two_requeues_keep_exact_attempt_ledger(
    database, workspace_id, uow_factory
) -> None:
    first_seed = _seed_delivery(uow_factory, workspace_id)
    service = NotificationDeliveryService(uow_factory, clock=lambda: NOW)

    def claim_once():
        return service.claim(
            workspace_id,
            first_seed.module_id,
            supported_contract_versions=(1,),
            lease_seconds=5,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_results = tuple(executor.map(lambda _: claim_once(), (1, 2)))
    claims = [item for result in claim_results for item in result]
    assert len(claims) == 1
    first_claim = claims[0]

    assert service.recover_expired_claims(
        workspace_id, now=NOW + timedelta(seconds=6), limit=50
    ) == 1
    with pytest.raises(StaleFencingToken):
        service.ack(
            workspace_id,
            first_seed.intent_id,
            claim_token=first_claim.claim_token,
            fencing_token=first_claim.fencing_token,
            completed_at=NOW + timedelta(seconds=6),
            provider_message_ref="provider-msg-stale",
        )
    second_claim = service.claim(
        workspace_id,
        first_seed.module_id,
        supported_contract_versions=(1,),
        lease_seconds=30,
        now=NOW + timedelta(seconds=6),
    )[0]
    with uow_factory() as uow:
        route = uow.notifications.get_route(workspace_id, first_seed.route_id)
        assert route is not None
        uow.notifications.save_route(
            route.with_status(NotificationRouteStatus.DISABLED, now=NOW + timedelta(seconds=7)),
            route.version,
        )
    suppressed = service.nack(
        workspace_id,
        first_seed.intent_id,
        claim_token=second_claim.claim_token,
        fencing_token=second_claim.fencing_token,
        outcome="retryable_failure",
        error_code=NotificationAttemptErrorCode.TIMEOUT,
        now=NOW + timedelta(seconds=7),
    )
    assert suppressed.status.value == "suppressed"
    assert _ledger(database, workspace_id) == (2, 2, 1, 2)

    with database.session() as session:
        session.execute(
            update(ModuleInstanceModel)
            .where(
                ModuleInstanceModel.workspace_id == workspace_id,
                ModuleInstanceModel.id == first_seed.module_id,
            )
            .values(instance_name="concurrency-test-channel-1")
        )
        session.commit()

    second_seed = _seed_delivery(uow_factory, workspace_id)
    second_service = NotificationDeliveryService(
        uow_factory, clock=lambda: NOW, max_attempts=1
    )
    exhausted_claim = second_service.claim(
        workspace_id,
        second_seed.module_id,
        supported_contract_versions=(1,),
        now=NOW,
    )[0]
    exhausted = second_service.nack(
        workspace_id,
        second_seed.intent_id,
        claim_token=exhausted_claim.claim_token,
        fencing_token=exhausted_claim.fencing_token,
        outcome="terminal_failure",
        error_code=NotificationAttemptErrorCode.PROVIDER_UNAVAILABLE,
        now=NOW,
    )
    assert exhausted.status.value == "exhausted"
    with uow_factory() as uow:
        route = uow.notifications.get_route(workspace_id, second_seed.route_id)
        intent = uow.notifications.get_intent(workspace_id, second_seed.intent_id)
        assert route is not None
        assert intent is not None
        route_version = route.version
        command_values = (
            _requeue_command(workspace_id, intent.id, route_version),
            _requeue_command(workspace_id, intent.id, route_version),
        )

    def requeue(command: CommandEnvelope):
        return CommandService(
            uow_factory,
            (
                NotificationCommandHandler(
                    clock=lambda: NOW + timedelta(seconds=1),
                    local_actor_id="operator",
                ),
            ),
        ).handle(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        requeue_results = tuple(executor.map(requeue, command_values))
    assert requeue_results[0].aggregate_id == requeue_results[1].aggregate_id
    assert {result.data["delivery_round"] for result in requeue_results} == {2}

    with database.session() as session:
        root_task_id = session.scalar(
            select(NotificationFactModel.root_task_id).where(
                NotificationFactModel.id == second_seed.fact_id
            )
        )
        assert root_task_id is not None
        facts = session.scalars(
            select(NotificationFactModel).where(
                NotificationFactModel.workspace_id == workspace_id,
                NotificationFactModel.root_task_id == root_task_id,
            )
        ).all()
        assert len(facts) == 3
        assert sorted(fact.transition for fact in facts) == [
            "degraded",
            "opened",
            "recovered",
        ]
        intents = session.scalars(
            select(NotificationDeliveryIntentModel).where(
                NotificationDeliveryIntentModel.workspace_id == workspace_id,
                NotificationDeliveryIntentModel.notification_route_id == second_seed.route_id,
            )
        ).all()
        assert len(intents) == 2
        assert sorted(intent.delivery_round for intent in intents) == [1, 2]
    assert _ledger(database, workspace_id) == (5, 4, 3, 3)


@pytest.mark.parametrize(
    ("target", "lifecycle"),
    (
        ("route", NotificationRouteStatus.DRAINING),
        ("route", NotificationRouteStatus.DISABLED),
        ("route", NotificationRouteStatus.REVOKED),
        ("instance", ModuleLifecycle.DRAINING),
        ("instance", ModuleLifecycle.DISABLED),
        ("definition", ModuleLifecycle.DRAINING),
        ("definition", ModuleLifecycle.DISABLED),
    ),
)
def test_claim_fails_closed_for_route_and_module_lifecycle(
    database, workspace_id, uow_factory, target: str, lifecycle
) -> None:
    seed = _seed_delivery(uow_factory, workspace_id)
    with uow_factory() as uow:
        if target == "route":
            route = uow.notifications.get_route(workspace_id, seed.route_id)
            assert route is not None
            uow.notifications.save_route(
                route.with_status(lifecycle, now=NOW + timedelta(seconds=1)),
                route.version,
            )
        elif target == "instance":
            uow.session.execute(
                update(ModuleInstanceModel)
                .where(
                    ModuleInstanceModel.workspace_id == workspace_id,
                    ModuleInstanceModel.id == seed.module_id,
                )
                .values(lifecycle=lifecycle.value)
            )
        else:
            instance = uow.modules.get_instance(seed.module_id, workspace_id)
            assert instance is not None
            uow.session.execute(
                update(ModuleDefinitionModel)
                .where(
                    ModuleDefinitionModel.workspace_id == workspace_id,
                    ModuleDefinitionModel.id == instance.definition_id,
                )
                .values(lifecycle=lifecycle.value)
            )
    claimed = NotificationDeliveryService(
        uow_factory, clock=lambda: NOW
    ).claim(
        workspace_id,
        seed.module_id,
        supported_contract_versions=(1,),
        now=NOW + timedelta(seconds=2),
    )
    assert claimed == ()
    assert _ledger(database, workspace_id) == (1, 1, 1, 0)
    with database.session() as session:
        intent = session.scalar(
            select(NotificationDeliveryIntentModel).where(
                NotificationDeliveryIntentModel.id == seed.intent_id
            )
        )
        assert intent is not None
        assert intent.status == "pending"
        assert intent.attempts == 0


def test_missing_route_suppresses_claim_without_private_identity(
    database, workspace_id, uow_factory, monkeypatch
) -> None:
    seed = _seed_delivery(uow_factory, workspace_id)
    service = NotificationDeliveryService(uow_factory, clock=lambda: NOW)
    envelope = service.claim(
        workspace_id,
        seed.module_id,
        supported_contract_versions=(1,),
        now=NOW,
    )[0]
    original = SqlAlchemyNotificationRepository.get_route

    def hide_route(self, target_workspace_id, route_id, *, for_update=False):
        if target_workspace_id == workspace_id and route_id == seed.route_id:
            return None
        return original(self, target_workspace_id, route_id, for_update=for_update)

    monkeypatch.setattr(SqlAlchemyNotificationRepository, "get_route", hide_route)
    settlement = service.nack(
        workspace_id,
        seed.intent_id,
        claim_token=envelope.claim_token,
        fencing_token=envelope.fencing_token,
        outcome="retryable_failure",
        error_code=NotificationAttemptErrorCode.TIMEOUT,
        now=NOW + timedelta(seconds=1),
    )
    assert settlement.status.value == "suppressed"
    assert settlement.error_code is NotificationAttemptErrorCode.ROUTE_NOT_FOUND
    assert _ledger(database, workspace_id) == (2, 2, 1, 1)
    with database.session() as session:
        attempt = session.scalar(
            select(NotificationDeliveryAttemptModel).where(
                NotificationDeliveryAttemptModel.delivery_intent_id == seed.intent_id
            )
        )
        assert attempt is not None
        assert attempt.error_code == "timeout"
        intent = session.scalar(
            select(NotificationDeliveryIntentModel).where(
                NotificationDeliveryIntentModel.id == seed.intent_id
            )
        )
        assert intent is not None
        assert intent.last_error_code == "route_not_found"
        serialized = str(attempt)
        assert PRIVATE_TEXT not in serialized


class _MalformedObservationSource:
    def observe(self, change):
        del change
        return object()


class _SecretObservationSource:
    def observe(self, change):
        del change
        raise ValidationError(PRIVATE_TEXT)


def test_malformed_and_secret_source_projection_is_rejected_before_ledger_write(
    database, workspace_id, uow_factory
) -> None:
    _seed_task_change(database, workspace_id, title="坏投影不会落账")
    for source in (_MalformedObservationSource(), _SecretObservationSource()):
        result = NotificationReconciler(
            uow_factory,
            observation_source=source,
            clock=lambda: NOW,
            owner="malformed-source",
        ).reconcile_workspace(workspace_id)
        assert result.diagnostic_code == "notification_observation_invalid"
        assert _ledger(database, workspace_id) == (0, 0, 0, 0)
        assert PRIVATE_TEXT not in str(result)

    with pytest.raises(ValidationError):
        NotificationObservation(
            workspace_id=workspace_id,
            recipient_type="workspace_operator",
            recipient_id="local",
            subject_type="root_task",
            subject_id=str(uuid4()),
            topic="task_blocked",
            root_task_id=uuid4(),
            subtask_id=None,
            worker_item_id=None,
            source_projection_cursor=1,
            source_projection_type="task_overview",
            source_resource_id=uuid4(),
            source_resource_version=1,
            kind="task_blocked",
            observed_status="blocked",
            authoritative_fingerprint="a" * 64,
            root_task_title=PRIVATE_TEXT,
            subtask_path=(),
            worker_item_title=None,
            deep_link="/tasks/test",
            correlation_id="source-test",
        )


def test_cursor_gap_resync_and_database_restart_keep_one_baseline_fact(
    database, workspace_id, uow_factory
) -> None:
    task = _seed_task_change(database, workspace_id, title="游标断档恢复")
    _set_task_status(
        database,
        workspace_id,
        task.id,
        status="blocked",
        change_type="task_blocked",
    )
    with database.session() as session:
        session.execute(
            delete(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.cursor == 1,
            )
        )
        session.commit()

    reconciler = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="gap-recovery"
    )
    gap = reconciler.reconcile_workspace(workspace_id)
    assert gap.diagnostic_code == "notification_resync_required"
    assert _ledger(database, workspace_id) == (0, 0, 0, 0)

    baseline = reconciler.baseline_workspace(workspace_id)
    assert baseline.diagnostic_code is None
    assert baseline.facts_created == 1
    assert _ledger(database, workspace_id) == (1, 1, 0, 0)

    database.dispose()
    restarted = NotificationReconciler(
        uow_factory, clock=lambda: NOW + timedelta(seconds=1), owner="gap-restarted"
    )
    repeated = restarted.baseline_workspace(workspace_id)
    assert repeated.facts_created == 0
    assert _ledger(database, workspace_id) == (1, 1, 0, 0)
    with database.session() as session:
        cursor = session.scalar(
            select(NotificationReconciliationCursorModel).where(
                NotificationReconciliationCursorModel.workspace_id == workspace_id
            )
        )
        assert cursor is not None
        assert cursor.last_projection_cursor == 3
