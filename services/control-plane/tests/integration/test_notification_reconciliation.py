from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.application.notification_reconciler import (
    NotificationReconciler,
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
    NotificationFact,
    NotificationPriority,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationSourceBinding,
    NotificationTransition,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.persistence.models import (
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationReconciliationCursorModel,
    NotificationThreadModel,
    PlanSnapshotModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.notification_repository import (
    SqlAlchemyNotificationRepository,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
OTHER_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000099")


def _seed_task_change(database, workspace_id: UUID, *, title: str) -> Task:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title=title,
        description="reconcile projection changes",
        acceptance_criteria=("web inbox contains one fact",),
        priority=50,
        requested_by="integration",
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        uow.flush()
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="task_overview",
            resource_id=task.id,
            change_type="task_attention",
            resource_version=task.version,
            correlation_id=uuid4(),
        )
    return task


def _seed_origin_route(
    database,
    workspace_id: UUID,
    task: Task,
    *,
    baseline_import: bool = False,
    created_at: datetime = NOW,
    resolved: bool = False,
) -> NotificationRoute:
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
    fact = NotificationFact.create(
        workspace_id=workspace_id,
        policy_version=1,
        recipient_type="workspace_operator",
        recipient_id="local",
        kind="task_completed" if resolved else "execution_attention",
        priority=NotificationPriority.PROGRESS
        if resolved
        else NotificationPriority.ACTION,
        transition=NotificationTransition.COMPLETED
        if resolved
        else NotificationTransition.OPENED,
        topic="task_completed" if resolved else "execution_attention",
        root_task_id=task.id,
        subtask_id=None,
        worker_item_id=None,
        source_projection_cursor=1,
        source_projection_type="task_overview",
        source_resource_id=task.id,
        source_resource_version=1,
        authoritative_fingerprint=("b" if baseline_import else "a") * 64,
        title=task.title,
        summary="任务需要关注。",
        action="打开任务查看",
        deep_link=f"/workspaces/{workspace_id}/tasks/{task.id}",
        correlation_id=str(uuid4()),
        baseline_import=baseline_import,
        occurred_at=created_at,
        created_at=created_at,
    )
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)
    binding = NotificationSourceBinding(
        workspace_id=workspace_id,
        resource_type="root_task",
        resource_id=task.id,
        notification_route_id=route.id,
        binding_kind=NotificationBindingKind.ORIGIN,
        bound_at=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        uow.flush()
        uow.notifications.add_fact_or_get_existing(fact)
        uow.notifications.add_thread(thread)
        uow.notifications.add_route(route)
        uow.flush()
        uow.notifications.add_binding_or_get_existing(binding)
    return route


def test_live_projection_change_materializes_web_notification_once(
    database, workspace_id, uow_factory
) -> None:
    _seed_task_change(database, workspace_id, title="实现通知网关")

    service = NotificationReconciler(
        uow_factory,
        clock=lambda: NOW,
        owner="integration-reconciler",
        batch_size=100,
    )
    first = service.reconcile_workspace(workspace_id)
    second = service.reconcile_workspace(workspace_id)

    assert first.facts_created == 1
    assert first.threads_changed == 1
    assert second.facts_created == 0
    assert second.processed_changes == 1
    assert second.relevant_changes == 0

    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(NotificationFactModel)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(NotificationThreadModel)
        ) == 1
        cursor = session.scalar(
            select(NotificationReconciliationCursorModel).where(
                NotificationReconciliationCursorModel.workspace_id == workspace_id,
                NotificationReconciliationCursorModel.policy_version == 1,
            )
        )
        assert cursor is not None
        assert cursor.last_projection_cursor == 2


def test_same_authoritative_state_deduplicates_across_invalidation_reasons(
    database, workspace_id, uow_factory
) -> None:
    task = _seed_task_change(database, workspace_id, title="稳定权威状态")
    service = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="stable-state-reconciler"
    )

    first = service.reconcile_workspace(workspace_id)
    assert first.facts_created == 1

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="task_overview",
            resource_id=task.id,
            change_type="task_attention_repeat",
            resource_version=task.version,
            correlation_id=uuid4(),
        )

    second = service.reconcile_workspace(workspace_id)

    assert second.facts_created == 0
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(NotificationFactModel)) == 1


def test_subtask_workspace_change_preserves_root_and_subtask_identity(
    database, workspace_id, uow_factory
) -> None:
    root_task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="通知根任务",
        description="",
        acceptance_criteria=("root",),
        priority=50,
        requested_by="integration",
    )
    subtask = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="通知子任务",
        description="",
        acceptance_criteria=("subtask",),
        priority=50,
        requested_by="integration",
    )
    snapshot_id = uuid4()
    worker_item_id = uuid4()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(root_task)
        uow.tasks.add(subtask)
        uow.flush()
        uow.session.add(
            PlanSnapshotModel(
                id=snapshot_id,
                schema_version=2,
                workspace_id=workspace_id,
                root_task_id=root_task.id,
                generation=1,
                parent_snapshot_id=None,
                source_type="integration",
                source_ref="notification-subtask",
                title="通知测试计划",
                goal_summary="保留子任务身份",
                payload={},
                published_by="integration",
                published_at=NOW,
            )
        )
        uow.session.add(
            WorkerItemModel(
                id=worker_item_id,
                workspace_id=workspace_id,
                root_task_id=root_task.id,
                stable_key="notification-subtask-worker",
                title="子任务 worker",
                responsibility="验证通知层级",
                status="active",
                context_version=1,
                acknowledged_context_version=1,
                derived_from_worker_item_ids=[],
                version=1,
            )
        )
        uow.flush()
        uow.session.add(
            WorkerItemPlacementModel(
                plan_snapshot_id=snapshot_id,
                plan_generation=1,
                workspace_id=workspace_id,
                root_task_id=root_task.id,
                worker_item_id=worker_item_id,
                worker_item_key="notification-subtask-worker",
                subtask_key="notification-subtask",
                subtask_task_id=subtask.id,
                role="implementer",
                capability_requirements=["python"],
                worker_policy_ref=None,
                configuration_ref=None,
            )
        )
        uow.flush()
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="subtask_workspace",
            resource_id=subtask.id,
            change_type="subtask_attention",
            resource_version=subtask.version,
            correlation_id=uuid4(),
        )

    result = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="subtask-reconciler"
    ).reconcile_workspace(workspace_id)

    assert result.facts_created == 1
    with database.session() as session:
        fact = session.scalar(select(NotificationFactModel))
        assert fact is not None
        assert fact.root_task_id == root_task.id
        assert fact.subtask_id == subtask.id

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="worker_item_workspace",
            resource_id=worker_item_id,
            change_type="worker_item_attention",
            resource_version=1,
            correlation_id=uuid4(),
        )

    second = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="worker-item-reconciler"
    ).reconcile_workspace(workspace_id)

    assert second.facts_created == 1
    with database.session() as session:
        fact = session.scalar(
            select(NotificationFactModel).where(
                NotificationFactModel.worker_item_id == worker_item_id
            )
        )
        assert fact is not None
        assert fact.root_task_id == root_task.id
        assert fact.subtask_id == subtask.id
        assert fact.deep_link == (
            f"/workspaces/{workspace_id}/worker-items/{worker_item_id}"
        )


@pytest.mark.parametrize(
    "failure_stage", ("after_fact", "after_thread", "before_cursor")
)
def test_reconciliation_rolls_back_at_each_commit_boundary_and_replays(
    database, workspace_id, uow_factory, monkeypatch, failure_stage: str
) -> None:
    _seed_task_change(database, workspace_id, title=f"失败注入 {failure_stage}")

    if failure_stage == "after_fact":
        original = SqlAlchemyNotificationRepository.add_fact_or_get_existing

        def fail_after_fact(self, fact):
            original(self, fact)
            raise RuntimeError("injected failure after fact")

        monkeypatch.setattr(
            SqlAlchemyNotificationRepository,
            "add_fact_or_get_existing",
            fail_after_fact,
        )
    elif failure_stage == "after_thread":
        original = SqlAlchemyNotificationRepository.add_thread

        def fail_after_thread(self, thread):
            original(self, thread)
            raise RuntimeError("injected failure after thread")

        monkeypatch.setattr(
            SqlAlchemyNotificationRepository, "add_thread", fail_after_thread
        )
    else:
        def fail_before_cursor(self, cursor, previous_version, expected_owner):
            del self, cursor, previous_version, expected_owner
            raise RuntimeError("injected failure before cursor commit")

        monkeypatch.setattr(
            SqlAlchemyNotificationRepository, "save_cursor", fail_before_cursor
        )

    service = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner=f"failure-{failure_stage}"
    )
    failed = service.reconcile_workspace(workspace_id)

    assert failed.diagnostic_code == "notification_reconciliation_write_failed"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(NotificationFactModel)) == 0
        assert session.scalar(select(func.count()).select_from(NotificationThreadModel)) == 0
        assert session.scalar(
            select(func.count()).select_from(NotificationDeliveryIntentModel)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(NotificationReconciliationCursorModel)
        ) == 0

    monkeypatch.undo()
    replayed = service.reconcile_workspace(workspace_id)

    assert replayed.facts_created == 1
    assert replayed.threads_changed == 1
    with database.session() as session:
        cursor = session.scalar(
            select(NotificationReconciliationCursorModel).where(
                NotificationReconciliationCursorModel.workspace_id == workspace_id,
                NotificationReconciliationCursorModel.policy_version == 1,
            )
        )
        assert cursor is not None
        assert cursor.last_projection_cursor == 1


def test_same_workspace_reconciliation_converges_under_two_instances(
    database, workspace_id, uow_factory
) -> None:
    _seed_task_change(database, workspace_id, title="同一 Workspace 并发")

    def run(owner: str):
        return NotificationReconciler(
            uow_factory, clock=lambda: NOW, owner=owner
        ).reconcile_workspace(workspace_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(run, ("concurrent-a", "concurrent-b")))

    assert all(
        result.diagnostic_code in {None, "notification_cursor_busy"}
        for result in results
    )
    assert sum(result.facts_created for result in results) == 1
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(NotificationFactModel)) == 1
        assert session.scalar(select(func.count()).select_from(NotificationThreadModel)) == 1
        assert session.scalar(
            select(func.count()).select_from(NotificationDeliveryIntentModel)
        ) == 0
        cursor = session.scalar(
            select(NotificationReconciliationCursorModel).where(
                NotificationReconciliationCursorModel.workspace_id == workspace_id,
                NotificationReconciliationCursorModel.policy_version == 1,
            )
        )
        assert cursor is not None
        assert cursor.last_projection_cursor == 2


def test_different_workspaces_reconcile_independently(
    database, workspace_id, uow_factory
) -> None:
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=OTHER_WORKSPACE_ID,
                name="Independent Workspace",
                status="active",
                version=1,
            )
        )
        session.commit()
    _seed_task_change(database, workspace_id, title="Workspace A")
    _seed_task_change(database, OTHER_WORKSPACE_ID, title="Workspace B")

    def run(target_workspace_id: UUID):
        return NotificationReconciler(
            uow_factory, clock=lambda: NOW, owner=f"workspace-{target_workspace_id}"
        ).reconcile_workspace(target_workspace_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(run, (workspace_id, OTHER_WORKSPACE_ID))
        )

    assert [result.facts_created for result in results] == [1, 1]
    with database.session() as session:
        for target_workspace_id in (workspace_id, OTHER_WORKSPACE_ID):
            assert session.scalar(
                select(func.count()).select_from(NotificationFactModel).where(
                    NotificationFactModel.workspace_id == target_workspace_id
                )
            ) == 1
            cursor = session.scalar(
                select(NotificationReconciliationCursorModel).where(
                    NotificationReconciliationCursorModel.workspace_id
                    == target_workspace_id,
                    NotificationReconciliationCursorModel.policy_version == 1,
                )
            )
            assert cursor is not None
            assert cursor.last_projection_cursor == 1


def test_late_route_backfill_creates_first_intent_for_open_recent_fact(
    database, workspace_id, uow_factory
) -> None:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="late route fact",
        description="",
        acceptance_criteria=("backfill",),
        priority=50,
        requested_by="integration",
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
    _seed_origin_route(database, workspace_id, task)

    service = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner="late-route-reconciler"
    )
    result = service.reconcile_workspace(workspace_id)

    assert result.intents_created == 1
    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(NotificationDeliveryIntentModel)
        ) == 1


@pytest.mark.parametrize("case", ("baseline", "resolved", "expired"))
def test_late_route_backfill_skips_baseline_resolved_and_expired_facts(
    database, workspace_id, uow_factory, case: str
) -> None:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title=f"late route {case}",
        description="",
        acceptance_criteria=("backfill",),
        priority=50,
        requested_by="integration",
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
    _seed_origin_route(
        database,
        workspace_id,
        task,
        baseline_import=case == "baseline",
        created_at=NOW - timedelta(days=2) if case == "expired" else NOW,
        resolved=case == "resolved",
    )

    result = NotificationReconciler(
        uow_factory, clock=lambda: NOW, owner=f"late-route-{case}"
    ).reconcile_workspace(workspace_id)

    assert result.intents_created == 0
    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(NotificationDeliveryIntentModel)
        ) == 0
