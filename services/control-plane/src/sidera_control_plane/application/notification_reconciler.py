from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sidera_control_plane.application.notification_observations import (
    NotificationObservationSource,
    observation_subject_key,
)
from sidera_control_plane.application.notification_policy import (
    POLICY_VERSION,
    NotificationEvaluationMode,
    NotificationObservation,
    NotificationPolicyV1,
    NotificationRouteEligibility,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    NOTIFICATION_CHANNEL_MODULE_TYPE,
    ModuleLifecycle,
    NotificationChannelCapabilityProfile,
)
from sidera_control_plane.domain.notifications import (
    InboxThread,
    NotificationDeliveryIntent,
    NotificationDeliveryStatus,
    NotificationFact,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationThreadBusinessStatus,
    NotificationThreadOrganizationStatus,
    notification_delivery_intent_id,
    notification_thread_id,
    notification_thread_key,
)
from sidera_control_plane.domain.projections import ProjectionChange

DEFAULT_NOTIFICATION_RECONCILER_BATCH_SIZE = 100
DEFAULT_NOTIFICATION_RECONCILER_LEASE_SECONDS = 30
NOTIFICATION_EXTERNAL_DELIVERY_WINDOW = timedelta(hours=24)
DELIVERY_CONTRACT_VERSION = 1


@dataclass(frozen=True, slots=True)
class NotificationReconciliationResult:
    workspace_id: UUID
    acquired: bool
    processed_changes: int = 0
    relevant_changes: int = 0
    observations: int = 0
    facts_created: int = 0
    threads_changed: int = 0
    intents_created: int = 0
    cursor: int = 0
    diagnostic_code: str | None = None


@dataclass(frozen=True, slots=True)
class _ReconciliationCounts:
    processed_changes: int = 0
    relevant_changes: int = 0
    observations: int = 0
    facts_created: int = 0
    threads_changed: int = 0
    intents_created: int = 0


class _ObservationFailure(Exception):
    pass


class NotificationReconciler:
    """Reconcile projection invalidations into the durable notification ledger."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        observation_source: NotificationObservationSource | None = None,
        policy: NotificationPolicyV1 | None = None,
        clock: Callable[[], datetime] | None = None,
        owner: str = "notification-reconciler",
        batch_size: int = DEFAULT_NOTIFICATION_RECONCILER_BATCH_SIZE,
        lease_seconds: int = DEFAULT_NOTIFICATION_RECONCILER_LEASE_SECONDS,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if not owner.strip():
            raise ValueError("owner is required")
        self._uow_factory = uow_factory
        self._observation_source = observation_source
        self._policy = policy or NotificationPolicyV1()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner = owner
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds

    def reconcile_workspace(
        self,
        workspace_id: UUID,
        *,
        owner: str | None = None,
        limit: int | None = None,
        mode: NotificationEvaluationMode | str = NotificationEvaluationMode.LIVE,
    ) -> NotificationReconciliationResult:
        batch_size = self._batch_size if limit is None else limit
        if batch_size < 1:
            raise ValueError("limit must be positive")
        lease_owner = owner or self._owner
        counts = _ReconciliationCounts()
        acquired = False
        cursor_value = 0

        try:
            with self._uow_factory() as uow:
                now = self._clock()
                cursor = uow.notifications.claim_cursor(
                    workspace_id,
                    POLICY_VERSION,
                    lease_owner,
                    now,
                    self._lease_seconds,
                )
                if cursor is None:
                    return NotificationReconciliationResult(
                        workspace_id=workspace_id,
                        acquired=False,
                        diagnostic_code="notification_cursor_busy",
                    )
                acquired = True
                cursor_value = cursor.last_projection_cursor
                claimed_version = cursor.version
                observation_store = uow.notification_observations
                changes = observation_store.list_after(
                    workspace_id,
                    cursor=cursor.last_projection_cursor,
                    limit=batch_size,
                )
                counts = replace(counts, processed_changes=len(changes))
                observations = self._load_observations(
                    workspace_id,
                    changes,
                    observation_store,
                )
                counts = replace(
                    counts,
                    relevant_changes=len(
                        [
                            change
                            for change in changes
                            if observation_subject_key(change) is not None
                        ]
                    ),
                    observations=len(observations),
                )

                for change, observation in observations:
                    facts_created, threads_changed, intents_created = (
                        self._reconcile_observation(
                            uow,
                            change,
                            observation,
                            now=now,
                            mode=mode,
                        )
                    )
                    counts = replace(
                        counts,
                        facts_created=counts.facts_created + facts_created,
                        threads_changed=counts.threads_changed + threads_changed,
                        intents_created=counts.intents_created + intents_created,
                    )

                intents_created = self._reconcile_late_routes(
                    uow, workspace_id, now=now
                )
                counts = replace(
                    counts,
                    intents_created=counts.intents_created + intents_created,
                )

                target_cursor = max(
                    (change.cursor for change in changes),
                    default=cursor.last_projection_cursor,
                )
                uow.flush()
                advanced = cursor.advance(cursor=target_cursor, now=now)
                released = advanced.release(now=now)
                uow.notifications.save_cursor(
                    released,
                    claimed_version,
                    lease_owner,
                )
                uow.flush()
                cursor_value = released.last_projection_cursor
        except _ObservationFailure as exc:
            return self._failure_result(
                workspace_id,
                acquired=acquired,
                cursor=cursor_value,
                counts=counts,
                diagnostic_code=str(exc),
            )
        except (TimeoutError, TypeError, ValueError) as exc:
            return self._failure_result(
                workspace_id,
                acquired=acquired,
                cursor=cursor_value,
                counts=counts,
                diagnostic_code="notification_observation_invalid",
                cause=exc,
            )
        except Exception as exc:  # noqa: BLE001 - rollback and stable diagnostics
            return self._failure_result(
                workspace_id,
                acquired=acquired,
                cursor=cursor_value,
                counts=counts,
                diagnostic_code="notification_reconciliation_write_failed",
                cause=exc,
            )

        return NotificationReconciliationResult(
            workspace_id=workspace_id,
            acquired=acquired,
            processed_changes=counts.processed_changes,
            relevant_changes=counts.relevant_changes,
            observations=counts.observations,
            facts_created=counts.facts_created,
            threads_changed=counts.threads_changed,
            intents_created=counts.intents_created,
            cursor=cursor_value,
        )

    def _load_observations(
        self,
        workspace_id: UUID,
        changes: tuple[ProjectionChange, ...],
        observation_store: object,
    ) -> tuple[tuple[ProjectionChange, NotificationObservation], ...]:
        latest_by_subject = {}
        for change in changes:
            subject = observation_subject_key(change)
            if subject is None:
                continue
            current = latest_by_subject.get(subject)
            if current is None or change.cursor > current.cursor:
                latest_by_subject[subject] = change

        source = self._observation_source or observation_store
        observe = getattr(source, "observe", None)
        if not callable(observe):
            raise _ObservationFailure("notification_observation_invalid")

        result: list[tuple[ProjectionChange, NotificationObservation]] = []
        for change in sorted(
            latest_by_subject.values(), key=lambda item: item.cursor
        ):
            try:
                observation = observe(change)
            except (TimeoutError, TypeError, ValueError, ValidationError) as exc:
                raise _ObservationFailure(
                    "notification_observation_invalid"
                ) from exc
            if observation is None:
                continue
            if not isinstance(observation, NotificationObservation):
                raise _ObservationFailure("notification_observation_invalid")
            if observation.workspace_id != workspace_id:
                raise _ObservationFailure("notification_observation_invalid")
            result.append((change, observation))
        return tuple(result)

    def _reconcile_observation(
        self,
        uow: ControlPlaneUnitOfWork,
        change: ProjectionChange,
        observation: NotificationObservation,
        *,
        now: datetime,
        mode: NotificationEvaluationMode | str,
    ) -> tuple[int, int, int]:
        previous_state = uow.notifications.get_policy_state(
            observation.workspace_id,
            observation.recipient_type,
            observation.recipient_id,
            observation.subject_type,
            observation.subject_id,
            observation.topic,
            for_update=True,
        )
        try:
            mutation = self._policy.evaluate(
                observation,
                state=previous_state,
                now=now,
                mode=mode,
            )
        except ValidationError as exc:
            raise _ObservationFailure("notification_observation_invalid") from exc

        facts_created = 0
        threads_changed = 0
        intents_created = 0
        next_state = mutation.policy_state
        fact = mutation.fact
        if fact is not None:
            existing_fact = uow.notifications.get_fact(
                fact.workspace_id, fact.id
            )
            persisted_fact = uow.notifications.add_fact_or_get_existing(fact)
            facts_created = 1 if existing_fact is None else 0
            thread_key = notification_thread_key(
                recipient_type=persisted_fact.recipient_type,
                recipient_id=persisted_fact.recipient_id,
                root_task_id=persisted_fact.root_task_id,
                subtask_id=persisted_fact.subtask_id,
                worker_item_id=persisted_fact.worker_item_id,
                topic=persisted_fact.topic,
            )
            thread = uow.notifications.get_thread_by_key(
                persisted_fact.workspace_id,
                persisted_fact.recipient_type,
                persisted_fact.recipient_id,
                thread_key,
                for_update=True,
            )
            if thread is None:
                thread = InboxThread.open_from_fact(
                    persisted_fact,
                    thread_id=notification_thread_id(thread_key),
                    now=now,
                )
                uow.notifications.add_thread(thread)
                threads_changed = 1
                thread_change_type = "notification.thread_opened"
            else:
                next_thread = thread.apply_fact(persisted_fact, now=now)
                if next_thread != thread:
                    uow.notifications.save_thread(next_thread, thread.version)
                    thread = next_thread
                    threads_changed = 1
                    thread_change_type = "notification.thread_updated"
                else:
                    thread_change_type = None
            next_state = replace(next_state, active_thread_id=thread.id)
            if thread_change_type is not None:
                uow.projection_changes.changed(
                    workspace_id=thread.workspace_id,
                    projection_type="notification_thread",
                    resource_id=thread.id,
                    change_type=thread_change_type,
                    resource_version=thread.version,
                    correlation_id=change.correlation_id,
                )
            if (
                not mutation.external_delivery_forbidden
                and mutation.route_eligibility
                is NotificationRouteEligibility.ORIGIN_IF_ACTIVE
            ):
                route = self._resolve_origin_route(uow, observation)
                if route is not None:
                    created = self._enqueue_intent(
                        uow,
                        persisted_fact,
                        route,
                        now=now,
                    )
                    if created:
                        intents_created = 1
                        intent = self._intent_for_fact_route(
                            persisted_fact, route, now=now
                        )
                        uow.projection_changes.changed(
                            workspace_id=intent.workspace_id,
                            projection_type="notification_delivery",
                            resource_id=intent.id,
                            change_type="notification.delivery_enqueued",
                            resource_version=intent.fencing_token,
                            correlation_id=change.correlation_id,
                        )

        if previous_state is None:
            uow.notifications.add_policy_state(next_state)
        else:
            uow.notifications.save_policy_state(
                next_state, previous_state.resource_version
            )
        return facts_created, threads_changed, intents_created

    def _resolve_origin_route(
        self,
        uow: ControlPlaneUnitOfWork,
        observation: NotificationObservation,
    ) -> NotificationRoute | None:
        targets: list[tuple[str, UUID]] = [("root_task", observation.root_task_id)]
        if observation.subject_type == "planning_request":
            try:
                targets.insert(0, ("planning_request", UUID(observation.subject_id)))
            except ValueError:
                return None
        for resource_type, resource_id in targets:
            routes = uow.notifications.list_routes_for_resource(
                observation.workspace_id, resource_type, resource_id
            )
            for route in routes:
                if (
                    route.recipient_type == observation.recipient_type
                    and route.recipient_id == observation.recipient_id
                    and self._route_supports_delivery(uow, route)
                ):
                    return route
        return None

    def _route_supports_delivery(
        self, uow: ControlPlaneUnitOfWork, route: NotificationRoute
    ) -> bool:
        if route.status is not NotificationRouteStatus.ACTIVE:
            return False
        modules = getattr(uow, "modules", None)
        if modules is None:
            return False
        instance = modules.get_instance(
            route.channel_module_instance_id, route.workspace_id
        )
        if instance is None or instance.lifecycle is not ModuleLifecycle.ACTIVE:
            return False
        definition = modules.get_definition(instance.definition_id, route.workspace_id)
        if definition is None:
            return False
        if (
            definition.module_type != NOTIFICATION_CHANNEL_MODULE_TYPE
            or definition.lifecycle is not ModuleLifecycle.ACTIVE
            or not isinstance(
                definition.capabilities, NotificationChannelCapabilityProfile
            )
        ):
            return False
        return (
            route.channel_type in definition.capabilities.channel_types
            and DELIVERY_CONTRACT_VERSION
            in definition.capabilities.delivery_contract_versions
        )

    def _enqueue_intent(
        self,
        uow: ControlPlaneUnitOfWork,
        fact: NotificationFact,
        route: NotificationRoute,
        *,
        now: datetime,
    ) -> bool:
        intent = self._intent_for_fact_route(fact, route, now=now)
        existing = uow.notifications.get_intent(
            intent.workspace_id, intent.id
        )
        uow.notifications.add_intent_or_get_existing(intent)
        return existing is None

    @staticmethod
    def _intent_for_fact_route(
        fact: NotificationFact,
        route: NotificationRoute,
        *,
        now: datetime,
    ) -> NotificationDeliveryIntent:
        return NotificationDeliveryIntent(
            id=notification_delivery_intent_id(
                workspace_id=fact.workspace_id,
                notification_fact_id=fact.id,
                notification_route_id=route.id,
                delivery_round=1,
            ),
            workspace_id=fact.workspace_id,
            notification_fact_id=fact.id,
            notification_route_id=route.id,
            channel_module_instance_id=route.channel_module_instance_id,
            delivery_round=1,
            contract_version=DELIVERY_CONTRACT_VERSION,
            status=NotificationDeliveryStatus.PENDING,
            attempts=0,
            next_attempt_at=now,
            claimed_until=None,
            claim_token=None,
            fencing_token=1,
            provider_message_ref=None,
            last_error_code=None,
            delivered_at=None,
            exhausted_at=None,
            created_at=now,
            updated_at=now,
        )

    def _reconcile_late_routes(
        self,
        uow: ControlPlaneUnitOfWork,
        workspace_id: UUID,
        *,
        now: datetime,
    ) -> int:
        source = uow.notification_observations
        candidates = source.list_route_backfill_candidates(
            workspace_id,
            now=now,
            window=NOTIFICATION_EXTERNAL_DELIVERY_WINDOW,
        )
        created_count = 0
        for candidate in candidates:
            fact = uow.notifications.get_fact(workspace_id, candidate.fact_id)
            thread = uow.notifications.get_thread(
                workspace_id, candidate.thread_id, for_update=True
            )
            route = uow.notifications.get_route(
                workspace_id, candidate.route_id, for_update=True
            )
            if fact is None or thread is None or route is None:
                continue
            if (
                fact.baseline_import
                or thread.business_status is not NotificationThreadBusinessStatus.OPEN
                or thread.organization_status
                is not NotificationThreadOrganizationStatus.ACTIVE
                or fact.created_at < now - NOTIFICATION_EXTERNAL_DELIVERY_WINDOW
                or not self._route_supports_delivery(uow, route)
            ):
                continue
            if self._enqueue_intent(uow, fact, route, now=now):
                created_count += 1
                uow.projection_changes.changed(
                    workspace_id=fact.workspace_id,
                    projection_type="notification_delivery",
                    resource_id=notification_delivery_intent_id(
                        workspace_id=fact.workspace_id,
                        notification_fact_id=fact.id,
                        notification_route_id=route.id,
                        delivery_round=1,
                    ),
                    change_type="notification.delivery_backfilled",
                    resource_version=1,
                    correlation_id=fact.source_resource_id,
                )
        return created_count

    @staticmethod
    def _failure_result(
        workspace_id: UUID,
        *,
        acquired: bool,
        cursor: int,
        counts: _ReconciliationCounts,
        diagnostic_code: str,
        cause: Exception | None = None,
    ) -> NotificationReconciliationResult:
        del cause
        return NotificationReconciliationResult(
            workspace_id=workspace_id,
            acquired=acquired,
            processed_changes=counts.processed_changes,
            relevant_changes=counts.relevant_changes,
            observations=counts.observations,
            cursor=cursor,
            diagnostic_code=diagnostic_code,
        )
