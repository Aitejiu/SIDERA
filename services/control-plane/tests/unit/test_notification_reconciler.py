from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from sidera_control_plane.application.notification_observations import (
    NotificationObservationSource,
)
from sidera_control_plane.application.notification_policy import (
    NotificationObservation,
    NotificationPolicyV1,
)
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
    NotificationDeliveryIntent,
    NotificationFact,
    NotificationPolicyState,
    NotificationReconciliationCursor,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationTransition,
)
from sidera_control_plane.domain.projections import ProjectionChange

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("10000000-0000-0000-0000-000000000001")


def _change(cursor: int, projection_type: str = "task_overview") -> ProjectionChange:
    return ProjectionChange(
        workspace_id=WORKSPACE_ID,
        cursor=cursor,
        schema_version=1,
        projection_type=projection_type,
        resource_id=ROOT_TASK_ID,
        change_type="task_attention",
        resource_version=cursor,
        occurred_at=NOW,
        correlation_id=UUID(f"{cursor:032x}"),
    )


def _observation(change: ProjectionChange) -> NotificationObservation:
    return NotificationObservation(
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        subject_type="root_task",
        subject_id=str(ROOT_TASK_ID),
        topic="execution_attention",
        root_task_id=ROOT_TASK_ID,
        subtask_id=None,
        worker_item_id=None,
        source_projection_cursor=change.cursor,
        source_projection_type=change.projection_type,
        source_resource_id=ROOT_TASK_ID,
        source_resource_version=change.resource_version or 1,
        kind="execution_attention",
        observed_status="attention",
        authoritative_fingerprint="a" * 64,
        root_task_title="实现通知网关",
        subtask_path=(),
        worker_item_title=None,
        deep_link=f"/workspaces/{WORKSPACE_ID}/tasks/{ROOT_TASK_ID}",
        correlation_id=str(change.correlation_id),
        transition=NotificationTransition.OPENED,
        requires_operator=True,
        source_type="akashic_private",
        origin_route_active=True,
    )


def _module_pair() -> tuple[ModuleDefinition, ModuleInstance]:
    definition_id = uuid4()
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=WORKSPACE_ID,
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
        workspace_id=WORKSPACE_ID,
        definition_id=definition_id,
        instance_name="test-channel",
        config_version=1,
        config_namespace="notification",
        config={"notification.mode": "test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    return definition, instance


@dataclass
class _ProjectionChanges:
    changes: list[dict[str, object]]

    def changed(self, **kwargs: object) -> SimpleNamespace:
        self.changes.append(kwargs)
        return SimpleNamespace(**kwargs)


class _ObservationStore(NotificationObservationSource):
    def __init__(self, changes: tuple[ProjectionChange, ...]) -> None:
        self.changes = changes
        self.observed: list[ProjectionChange] = []

    def list_after(
        self, workspace_id: UUID, *, cursor: int, limit: int
    ) -> tuple[ProjectionChange, ...]:
        assert workspace_id == WORKSPACE_ID
        return tuple(item for item in self.changes if item.cursor > cursor)[:limit]

    def observe(self, change: ProjectionChange) -> NotificationObservation:
        self.observed.append(change)
        return _observation(change)

    def list_route_backfill_candidates(self, workspace_id, *, now, window):
        del workspace_id, now, window
        return ()


class _Modules:
    def __init__(self, definition: ModuleDefinition, instance: ModuleInstance) -> None:
        self.definition = definition
        self.instance = instance

    def get_instance(self, module_id: UUID, workspace_id: UUID):
        return self.instance if module_id == self.instance.id and workspace_id == WORKSPACE_ID else None

    def get_definition(self, module_id: UUID, workspace_id: UUID):
        return self.definition if module_id == self.definition.id and workspace_id == WORKSPACE_ID else None


class _Notifications:
    def __init__(self, route: NotificationRoute) -> None:
        self.route = route
        self.policy_states: dict[tuple[object, ...], NotificationPolicyState] = {}
        self.facts: dict[UUID, NotificationFact] = {}
        self.threads: dict[str, InboxThread] = {}
        self.intents: dict[UUID, NotificationDeliveryIntent] = {}
        self.cursor = NotificationReconciliationCursor(
            workspace_id=WORKSPACE_ID,
            policy_version=1,
            last_projection_cursor=0,
            last_reconciled_at=NOW,
            lease_owner=None,
            lease_until=None,
            version=1,
        )

    def claim_cursor(self, workspace_id, policy_version, owner, now, lease_seconds):
        if self.cursor.lease_owner and self.cursor.lease_owner != owner and self.cursor.lease_until and self.cursor.lease_until > now:
            return None
        self.cursor = self.cursor.claim(
            lease_owner=owner,
            lease_until=now + timedelta(seconds=lease_seconds),
            now=now,
        )
        return self.cursor

    def save_cursor(self, cursor, previous_version, expected_owner):
        assert cursor.version > previous_version
        assert expected_owner == cursor.lease_owner or cursor.lease_owner is None
        self.cursor = cursor

    def get_policy_state(self, workspace_id, recipient_type, recipient_id, subject_type, subject_id, topic, *, for_update=False):
        return self.policy_states.get((workspace_id, recipient_type, recipient_id, subject_type, subject_id, topic))

    def add_policy_state(self, state):
        self.policy_states[_state_key(state)] = state

    def save_policy_state(self, state, previous_version):
        current = self.policy_states[_state_key(state)]
        assert current.resource_version == previous_version
        self.policy_states[_state_key(state)] = state

    def add_fact_or_get_existing(self, fact):
        existing = self.facts.get(fact.id)
        if existing is not None:
            return existing
        self.facts[fact.id] = fact
        return fact

    def get_fact(self, workspace_id, fact_id):
        fact = self.facts.get(fact_id)
        return fact if fact is not None and fact.workspace_id == workspace_id else None

    def get_thread_by_key(self, workspace_id, recipient_type, recipient_id, thread_key, *, for_update=False):
        return self.threads.get(thread_key)

    def add_thread(self, thread):
        self.threads[thread.thread_key] = thread

    def save_thread(self, thread, previous_version):
        current = self.threads[thread.thread_key]
        assert current.version == previous_version
        self.threads[thread.thread_key] = thread

    def list_routes_for_resource(self, workspace_id, resource_type, resource_id):
        if resource_type == "root_task" and resource_id == ROOT_TASK_ID:
            return (self.route,)
        return ()

    def add_intent_or_get_existing(self, intent):
        existing = self.intents.get(intent.id)
        if existing is not None:
            return existing
        self.intents[intent.id] = intent
        return intent

    def get_intent(self, workspace_id, intent_id, *, for_update=False):
        del for_update
        intent = self.intents.get(intent_id)
        return (
            intent
            if intent is not None and intent.workspace_id == workspace_id
            else None
        )


def _state_key(state: NotificationPolicyState) -> tuple[object, ...]:
    return (
        state.workspace_id,
        state.recipient_type,
        state.recipient_id,
        state.subject_type,
        state.subject_id,
        state.topic,
    )


class _Uow(AbstractContextManager["_Uow"]):
    def __init__(self, notifications, source, modules, projection_changes) -> None:
        self.notifications = notifications
        self.notification_observations = source
        self.modules = modules
        self.projection_changes = projection_changes
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        return False

    def flush(self):
        return None


def test_reconciler_deduplicates_subjects_and_creates_one_external_intent() -> None:
    definition, instance = _module_pair()
    route = NotificationRoute(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=instance.id,
        source_ref="src_" + "a" * 32,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )
    notifications = _Notifications(route)
    source = _ObservationStore((_change(1), _change(2, "task_graph")))
    projection_changes = _ProjectionChanges([])
    uow = _Uow(notifications, source, _Modules(definition, instance), projection_changes)

    result = NotificationReconciler(
        lambda: uow,
        policy=NotificationPolicyV1(),
        clock=lambda: NOW,
        owner="test-reconciler",
        batch_size=10,
    ).reconcile_workspace(WORKSPACE_ID)

    assert result.acquired is True
    assert result.processed_changes == 2
    assert result.observations == 1
    assert result.facts_created == 1
    assert result.threads_changed == 1
    assert result.intents_created == 1
    assert notifications.cursor.last_projection_cursor == 2
    assert len(source.observed) == 1
    assert len(notifications.facts) == 1
    assert len(notifications.threads) == 1
    assert len(notifications.intents) == 1
    assert len(projection_changes.changes) == 2


def test_reconciler_does_not_advance_cursor_when_observation_or_write_fails() -> None:
    definition, instance = _module_pair()
    route = NotificationRoute(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=instance.id,
        source_ref="src_" + "b" * 32,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )
    notifications = _Notifications(route)
    source = _ObservationStore((_change(1),))

    def fail_add_fact(fact):
        raise RuntimeError("injected ledger failure")

    notifications.add_fact_or_get_existing = fail_add_fact
    uow = _Uow(notifications, source, _Modules(definition, instance), _ProjectionChanges([]))

    result = NotificationReconciler(
        lambda: uow,
        clock=lambda: NOW,
        owner="test-reconciler",
    ).reconcile_workspace(WORKSPACE_ID)

    assert result.acquired is True
    assert result.diagnostic_code == "notification_reconciliation_write_failed"
    assert notifications.cursor.last_projection_cursor == 0
    assert notifications.policy_states == {}
    assert notifications.facts == {}
