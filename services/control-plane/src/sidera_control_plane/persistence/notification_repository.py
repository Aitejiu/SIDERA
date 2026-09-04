from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import ProjectionChangePublisherPort
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.notifications import (
    InboxThread,
    NotificationAttemptErrorCode,
    NotificationAttemptOutcome,
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


def _policy_state_to_model_values(
    state: NotificationPolicyState,
) -> dict[str, object]:
    return {
        "workspace_id": state.workspace_id,
        "recipient_type": state.recipient_type,
        "recipient_id": state.recipient_id,
        "subject_type": state.subject_type,
        "subject_id": state.subject_id,
        "topic": state.topic,
        "policy_version": state.policy_version,
        "observed_status": state.observed_status,
        "observed_fingerprint": state.observed_fingerprint,
        "observed_payload": _copy_json(state.observed_payload),
        "first_observed_at": state.first_observed_at,
        "not_before": state.not_before,
        "last_notified_fingerprint": state.last_notified_fingerprint,
        "active_thread_id": state.active_thread_id,
        "resource_version": state.resource_version,
        "updated_at": state.updated_at,
    }


def _policy_state_to_domain(
    row: NotificationPolicyStateModel,
) -> NotificationPolicyState:
    return NotificationPolicyState(
        workspace_id=row.workspace_id,
        recipient_type=row.recipient_type,
        recipient_id=row.recipient_id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        topic=row.topic,
        policy_version=row.policy_version,
        observed_status=row.observed_status,
        observed_fingerprint=row.observed_fingerprint,
        observed_payload=_copy_json(row.observed_payload),
        first_observed_at=row.first_observed_at,
        not_before=row.not_before,
        last_notified_fingerprint=row.last_notified_fingerprint,
        active_thread_id=row.active_thread_id,
        resource_version=row.resource_version,
        updated_at=row.updated_at,
    )


def _fact_to_model_values(fact: NotificationFact) -> dict[str, object]:
    return {
        "id": fact.id,
        "workspace_id": fact.workspace_id,
        "policy_version": fact.policy_version,
        "recipient_type": fact.recipient_type,
        "recipient_id": fact.recipient_id,
        "kind": fact.kind,
        "priority": fact.priority.value,
        "transition": fact.transition.value,
        "topic": fact.topic,
        "root_task_id": fact.root_task_id,
        "subtask_id": fact.subtask_id,
        "worker_item_id": fact.worker_item_id,
        "source_projection_cursor": fact.source_projection_cursor,
        "source_projection_type": fact.source_projection_type,
        "source_resource_id": fact.source_resource_id,
        "source_resource_version": fact.source_resource_version,
        "semantic_fingerprint": fact.semantic_fingerprint,
        "title": fact.title,
        "summary": fact.summary,
        "action": fact.action,
        "deep_link": fact.deep_link,
        "correlation_id": fact.correlation_id,
        "baseline_import": fact.baseline_import,
        "occurred_at": fact.occurred_at,
        "created_at": fact.created_at,
    }


def _fact_to_domain(row: NotificationFactModel) -> NotificationFact:
    return NotificationFact(
        id=row.id,
        workspace_id=row.workspace_id,
        policy_version=row.policy_version,
        recipient_type=row.recipient_type,
        recipient_id=row.recipient_id,
        kind=row.kind,
        priority=NotificationPriority(row.priority),
        transition=NotificationTransition(row.transition),
        topic=row.topic,
        root_task_id=row.root_task_id,
        subtask_id=row.subtask_id,
        worker_item_id=row.worker_item_id,
        source_projection_cursor=row.source_projection_cursor,
        source_projection_type=row.source_projection_type,
        source_resource_id=row.source_resource_id,
        source_resource_version=row.source_resource_version,
        semantic_fingerprint=row.semantic_fingerprint,
        title=row.title,
        summary=row.summary,
        action=row.action,
        deep_link=row.deep_link,
        correlation_id=row.correlation_id,
        baseline_import=row.baseline_import,
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


def _thread_to_model_values(thread: InboxThread) -> dict[str, object]:
    return {
        "id": thread.id,
        "workspace_id": thread.workspace_id,
        "recipient_type": thread.recipient_type,
        "recipient_id": thread.recipient_id,
        "thread_key": thread.thread_key,
        "topic": thread.topic,
        "root_task_id": thread.root_task_id,
        "subtask_id": thread.subtask_id,
        "worker_item_id": thread.worker_item_id,
        "latest_fact_id": thread.latest_fact_id,
        "business_status": thread.business_status.value,
        "organization_status": thread.organization_status.value,
        "last_read_fact_id": thread.last_read_fact_id,
        "unread_count": thread.unread_count,
        "version": thread.version,
        "opened_at": thread.opened_at,
        "resolved_at": thread.resolved_at,
        "archived_at": thread.archived_at,
        "updated_at": thread.updated_at,
    }


def _thread_to_domain(row: NotificationThreadModel) -> InboxThread:
    return InboxThread(
        id=row.id,
        workspace_id=row.workspace_id,
        recipient_type=row.recipient_type,
        recipient_id=row.recipient_id,
        thread_key=row.thread_key,
        topic=row.topic,
        root_task_id=row.root_task_id,
        subtask_id=row.subtask_id,
        worker_item_id=row.worker_item_id,
        latest_fact_id=row.latest_fact_id,
        business_status=NotificationThreadBusinessStatus(row.business_status),
        organization_status=NotificationThreadOrganizationStatus(
            row.organization_status
        ),
        last_read_fact_id=row.last_read_fact_id,
        unread_count=row.unread_count,
        version=row.version,
        opened_at=row.opened_at,
        resolved_at=row.resolved_at,
        archived_at=row.archived_at,
        updated_at=row.updated_at,
    )


def _route_to_model_values(route: NotificationRoute) -> dict[str, object]:
    return {
        "id": route.id,
        "workspace_id": route.workspace_id,
        "recipient_type": route.recipient_type,
        "recipient_id": route.recipient_id,
        "channel_type": route.channel_type,
        "channel_module_instance_id": route.channel_module_instance_id,
        "source_ref": route.source_ref,
        "status": route.status.value,
        "version": route.version,
        "registered_at": route.registered_at,
        "updated_at": route.updated_at,
    }


def _route_to_domain(row: NotificationRouteModel) -> NotificationRoute:
    return NotificationRoute(
        id=row.id,
        workspace_id=row.workspace_id,
        recipient_type=row.recipient_type,
        recipient_id=row.recipient_id,
        channel_type=row.channel_type,
        channel_module_instance_id=row.channel_module_instance_id,
        source_ref=row.source_ref,
        status=NotificationRouteStatus(row.status),
        version=row.version,
        registered_at=row.registered_at,
        updated_at=row.updated_at,
    )


def _binding_to_model_values(
    binding: NotificationSourceBinding,
) -> dict[str, object]:
    return {
        "workspace_id": binding.workspace_id,
        "resource_type": binding.resource_type,
        "resource_id": binding.resource_id,
        "notification_route_id": binding.notification_route_id,
        "binding_kind": binding.binding_kind.value,
        "bound_at": binding.bound_at,
    }


def _binding_to_domain(
    row: NotificationSourceBindingModel,
) -> NotificationSourceBinding:
    return NotificationSourceBinding(
        workspace_id=row.workspace_id,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        notification_route_id=row.notification_route_id,
        binding_kind=NotificationBindingKind(row.binding_kind),
        bound_at=row.bound_at,
    )


def _intent_to_model_values(
    intent: NotificationDeliveryIntent,
) -> dict[str, object]:
    return {
        "id": intent.id,
        "workspace_id": intent.workspace_id,
        "notification_fact_id": intent.notification_fact_id,
        "notification_route_id": intent.notification_route_id,
        "channel_module_instance_id": intent.channel_module_instance_id,
        "delivery_round": intent.delivery_round,
        "contract_version": intent.contract_version,
        "status": intent.status.value,
        "attempts": intent.attempts,
        "next_attempt_at": intent.next_attempt_at,
        "claimed_until": intent.claimed_until,
        "claim_token": intent.claim_token,
        "fencing_token": intent.fencing_token,
        "provider_message_ref": intent.provider_message_ref,
        "last_error_code": (
            intent.last_error_code.value if intent.last_error_code else None
        ),
        "delivered_at": intent.delivered_at,
        "exhausted_at": intent.exhausted_at,
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
    }


def _intent_to_domain(
    row: NotificationDeliveryIntentModel,
) -> NotificationDeliveryIntent:
    return NotificationDeliveryIntent(
        id=row.id,
        workspace_id=row.workspace_id,
        notification_fact_id=row.notification_fact_id,
        notification_route_id=row.notification_route_id,
        channel_module_instance_id=row.channel_module_instance_id,
        delivery_round=row.delivery_round,
        contract_version=row.contract_version,
        status=NotificationDeliveryStatus(row.status),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        claimed_until=row.claimed_until,
        claim_token=row.claim_token,
        fencing_token=row.fencing_token,
        provider_message_ref=row.provider_message_ref,
        last_error_code=(
            NotificationAttemptErrorCode(row.last_error_code)
            if row.last_error_code is not None
            else None
        ),
        delivered_at=row.delivered_at,
        exhausted_at=row.exhausted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _attempt_to_model_values(
    attempt: NotificationDeliveryAttempt,
) -> dict[str, object]:
    return {
        "delivery_intent_id": attempt.delivery_intent_id,
        "attempt_number": attempt.attempt_number,
        "claim_token": attempt.claim_token,
        "fencing_token": attempt.fencing_token,
        "outcome": attempt.outcome.value if attempt.outcome else None,
        "error_code": attempt.error_code.value if attempt.error_code else None,
        "provider_message_ref": attempt.provider_message_ref,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
    }


def _attempt_to_domain(
    row: NotificationDeliveryAttemptModel,
) -> NotificationDeliveryAttempt:
    return NotificationDeliveryAttempt(
        delivery_intent_id=row.delivery_intent_id,
        attempt_number=row.attempt_number,
        claim_token=row.claim_token,
        fencing_token=row.fencing_token,
        outcome=(
            NotificationAttemptOutcome(row.outcome) if row.outcome is not None else None
        ),
        error_code=(
            NotificationAttemptErrorCode(row.error_code)
            if row.error_code is not None
            else None
        ),
        provider_message_ref=row.provider_message_ref,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _cursor_to_model_values(
    cursor: NotificationReconciliationCursor,
) -> dict[str, object]:
    return {
        "workspace_id": cursor.workspace_id,
        "policy_version": cursor.policy_version,
        "last_projection_cursor": cursor.last_projection_cursor,
        "last_reconciled_at": cursor.last_reconciled_at,
        "lease_owner": cursor.lease_owner,
        "lease_until": cursor.lease_until,
        "version": cursor.version,
    }


def _cursor_to_domain(
    row: NotificationReconciliationCursorModel,
) -> NotificationReconciliationCursor:
    return NotificationReconciliationCursor(
        workspace_id=row.workspace_id,
        policy_version=row.policy_version,
        last_projection_cursor=row.last_projection_cursor,
        last_reconciled_at=row.last_reconciled_at,
        lease_owner=row.lease_owner,
        lease_until=row.lease_until,
        version=row.version,
    )


class SqlAlchemyNotificationRepository:
    def __init__(
        self,
        session: Session,
        projection_changes: ProjectionChangePublisherPort | None = None,
    ) -> None:
        self._session = session
        self._projection_changes = projection_changes

    def get_policy_state(
        self,
        workspace_id: UUID,
        recipient_type: str,
        recipient_id: str,
        subject_type: str,
        subject_id: str,
        topic: str,
        *,
        for_update: bool = False,
    ) -> NotificationPolicyState | None:
        statement = select(NotificationPolicyStateModel).where(
            NotificationPolicyStateModel.workspace_id == workspace_id,
            NotificationPolicyStateModel.recipient_type == recipient_type,
            NotificationPolicyStateModel.recipient_id == recipient_id,
            NotificationPolicyStateModel.subject_type == subject_type,
            NotificationPolicyStateModel.subject_id == subject_id,
            NotificationPolicyStateModel.topic == topic,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _policy_state_to_domain(row) if row is not None else None

    def list_due_policy_states(
        self,
        workspace_id: UUID,
        now: datetime,
        limit: int,
        *,
        for_update_skip_locked: bool = False,
    ) -> tuple[NotificationPolicyState, ...]:
        _positive_limit(limit)
        statement = (
            select(NotificationPolicyStateModel)
            .where(
                NotificationPolicyStateModel.workspace_id == workspace_id,
                NotificationPolicyStateModel.not_before <= now,
            )
            .order_by(
                NotificationPolicyStateModel.not_before.asc(),
                NotificationPolicyStateModel.updated_at.asc(),
                NotificationPolicyStateModel.subject_id.asc(),
                NotificationPolicyStateModel.topic.asc(),
            )
            .limit(limit)
        )
        if for_update_skip_locked:
            statement = statement.with_for_update(skip_locked=True)
        rows = self._session.scalars(statement).all()
        return tuple(_policy_state_to_domain(row) for row in rows)

    def add_policy_state(self, state: NotificationPolicyState) -> None:
        self._session.add(
            NotificationPolicyStateModel(**_policy_state_to_model_values(state))
        )

    def save_policy_state(
        self, state: NotificationPolicyState, previous_version: int
    ) -> None:
        result = self._session.execute(
            update(NotificationPolicyStateModel)
            .where(
                NotificationPolicyStateModel.workspace_id == state.workspace_id,
                NotificationPolicyStateModel.recipient_type == state.recipient_type,
                NotificationPolicyStateModel.recipient_id == state.recipient_id,
                NotificationPolicyStateModel.subject_type == state.subject_type,
                NotificationPolicyStateModel.subject_id == state.subject_id,
                NotificationPolicyStateModel.topic == state.topic,
                NotificationPolicyStateModel.resource_version == previous_version,
            )
            .values(
                observed_status=state.observed_status,
                observed_fingerprint=state.observed_fingerprint,
                observed_payload=_copy_json(state.observed_payload),
                first_observed_at=state.first_observed_at,
                not_before=state.not_before,
                last_notified_fingerprint=state.last_notified_fingerprint,
                active_thread_id=state.active_thread_id,
                resource_version=state.resource_version,
                updated_at=state.updated_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_policy_state_version(state) or 0,
            )

    def add_fact_or_get_existing(self, fact: NotificationFact) -> NotificationFact:
        values = _fact_to_model_values(fact)
        self._session.execute(
            insert(NotificationFactModel).values(**values).on_conflict_do_nothing()
        )
        row = self._session.scalar(
            select(NotificationFactModel).where(
                NotificationFactModel.workspace_id == fact.workspace_id,
                or_(
                    NotificationFactModel.semantic_fingerprint
                    == fact.semantic_fingerprint,
                    NotificationFactModel.id == fact.id,
                ),
            )
        )
        if row is None:
            raise ValidationError("notification fact insert did not persist")
        existing = _fact_to_domain(row)
        if existing.canonical_bytes() != fact.canonical_bytes():
            raise ValidationError(
                "notification fact replay does not match immutable identity"
            )
        return existing

    def get_fact(self, workspace_id: UUID, fact_id: UUID) -> NotificationFact | None:
        row = self._session.scalar(
            select(NotificationFactModel).where(
                NotificationFactModel.workspace_id == workspace_id,
                NotificationFactModel.id == fact_id,
            )
        )
        return _fact_to_domain(row) if row is not None else None

    def list_thread_facts(
        self,
        workspace_id: UUID,
        thread_id: UUID,
        *,
        cursor: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> tuple[NotificationFact, ...]:
        _positive_limit(limit)
        thread = self._session.scalar(
            select(NotificationThreadModel).where(
                NotificationThreadModel.workspace_id == workspace_id,
                NotificationThreadModel.id == thread_id,
            )
        )
        if thread is None:
            return ()
        conditions = [
            NotificationFactModel.workspace_id == workspace_id,
            NotificationFactModel.recipient_type == thread.recipient_type,
            NotificationFactModel.recipient_id == thread.recipient_id,
            NotificationFactModel.root_task_id == thread.root_task_id,
            NotificationFactModel.subtask_id.is_not_distinct_from(thread.subtask_id),
            NotificationFactModel.worker_item_id.is_not_distinct_from(
                thread.worker_item_id
            ),
            NotificationFactModel.topic == thread.topic,
        ]
        if cursor is not None:
            conditions.append(
                or_(
                    NotificationFactModel.created_at < cursor[0],
                    and_(
                        NotificationFactModel.created_at == cursor[0],
                        NotificationFactModel.id < cursor[1],
                    ),
                )
            )
        rows = self._session.scalars(
            select(NotificationFactModel)
            .where(*conditions)
            .order_by(
                NotificationFactModel.created_at.desc(),
                NotificationFactModel.id.desc(),
            )
            .limit(limit)
        ).all()
        return tuple(_fact_to_domain(row) for row in rows)

    def get_thread(
        self, workspace_id: UUID, thread_id: UUID, *, for_update: bool = False
    ) -> InboxThread | None:
        statement = select(NotificationThreadModel).where(
            NotificationThreadModel.workspace_id == workspace_id,
            NotificationThreadModel.id == thread_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _thread_to_domain(row) if row is not None else None

    def get_thread_by_key(
        self,
        workspace_id: UUID,
        recipient_type: str,
        recipient_id: str,
        thread_key: str,
        *,
        for_update: bool = False,
    ) -> InboxThread | None:
        statement = select(NotificationThreadModel).where(
            NotificationThreadModel.workspace_id == workspace_id,
            NotificationThreadModel.recipient_type == recipient_type,
            NotificationThreadModel.recipient_id == recipient_id,
            NotificationThreadModel.thread_key == thread_key,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _thread_to_domain(row) if row is not None else None

    def add_thread(self, thread: InboxThread) -> None:
        self._session.add(NotificationThreadModel(**_thread_to_model_values(thread)))
        self._session.flush()

    def save_thread(self, thread: InboxThread, previous_version: int) -> None:
        result = self._session.execute(
            update(NotificationThreadModel)
            .where(
                NotificationThreadModel.workspace_id == thread.workspace_id,
                NotificationThreadModel.id == thread.id,
                NotificationThreadModel.version == previous_version,
            )
            .values(
                latest_fact_id=thread.latest_fact_id,
                business_status=thread.business_status.value,
                organization_status=thread.organization_status.value,
                last_read_fact_id=thread.last_read_fact_id,
                unread_count=thread.unread_count,
                version=thread.version,
                resolved_at=thread.resolved_at,
                archived_at=thread.archived_at,
                updated_at=thread.updated_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version, self._current_thread_version(thread) or 0
            )

    def get_route(
        self, workspace_id: UUID, route_id: UUID, *, for_update: bool = False
    ) -> NotificationRoute | None:
        statement = select(NotificationRouteModel).where(
            NotificationRouteModel.workspace_id == workspace_id,
            NotificationRouteModel.id == route_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _route_to_domain(row) if row is not None else None

    def get_route_by_source_ref(
        self,
        workspace_id: UUID,
        channel_module_instance_id: UUID,
        source_ref: str,
        *,
        for_update: bool = False,
    ) -> NotificationRoute | None:
        statement = select(NotificationRouteModel).where(
            NotificationRouteModel.workspace_id == workspace_id,
            NotificationRouteModel.channel_module_instance_id
            == channel_module_instance_id,
            NotificationRouteModel.source_ref == source_ref,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _route_to_domain(row) if row is not None else None

    def list_routes_for_source_ref(
        self, workspace_id: UUID, source_ref: str, *, for_update: bool = False
    ) -> tuple[NotificationRoute, ...]:
        statement = (
            select(NotificationRouteModel)
            .where(
                NotificationRouteModel.workspace_id == workspace_id,
                NotificationRouteModel.source_ref == source_ref,
            )
            .order_by(
                NotificationRouteModel.registered_at.asc(),
                NotificationRouteModel.id.asc(),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        rows = self._session.scalars(statement).all()
        return tuple(_route_to_domain(row) for row in rows)

    def add_route(self, route: NotificationRoute) -> None:
        self._session.add(NotificationRouteModel(**_route_to_model_values(route)))

    def save_route(self, route: NotificationRoute, previous_version: int) -> None:
        result = self._session.execute(
            update(NotificationRouteModel)
            .where(
                NotificationRouteModel.workspace_id == route.workspace_id,
                NotificationRouteModel.id == route.id,
                NotificationRouteModel.version == previous_version,
            )
            .values(
                status=route.status.value,
                version=route.version,
                updated_at=route.updated_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version, self._current_route_version(route) or 0
            )

    def list_routes_for_recipient(
        self, workspace_id: UUID, recipient_type: str, recipient_id: str
    ) -> tuple[NotificationRoute, ...]:
        rows = self._session.scalars(
            select(NotificationRouteModel)
            .where(
                NotificationRouteModel.workspace_id == workspace_id,
                NotificationRouteModel.recipient_type == recipient_type,
                NotificationRouteModel.recipient_id == recipient_id,
            )
            .order_by(
                NotificationRouteModel.registered_at.asc(),
                NotificationRouteModel.id.asc(),
            )
        ).all()
        return tuple(_route_to_domain(row) for row in rows)

    def list_routes_for_resource(
        self, workspace_id: UUID, resource_type: str, resource_id: UUID
    ) -> tuple[NotificationRoute, ...]:
        rows = self._session.scalars(
            select(NotificationRouteModel)
            .join(
                NotificationSourceBindingModel,
                and_(
                    NotificationSourceBindingModel.notification_route_id
                    == NotificationRouteModel.id,
                    NotificationSourceBindingModel.workspace_id
                    == NotificationRouteModel.workspace_id,
                ),
            )
            .where(
                NotificationRouteModel.workspace_id == workspace_id,
                NotificationSourceBindingModel.resource_type == resource_type,
                NotificationSourceBindingModel.resource_id == resource_id,
            )
            .order_by(
                NotificationRouteModel.registered_at.asc(),
                NotificationRouteModel.id.asc(),
            )
        ).all()
        return tuple(_route_to_domain(row) for row in rows)

    def add_binding_or_get_existing(
        self, binding: NotificationSourceBinding
    ) -> NotificationSourceBinding:
        self._session.execute(
            insert(NotificationSourceBindingModel)
            .values(**_binding_to_model_values(binding))
            .on_conflict_do_nothing()
        )
        row = self._session.scalar(
            select(NotificationSourceBindingModel).where(
                NotificationSourceBindingModel.workspace_id == binding.workspace_id,
                NotificationSourceBindingModel.resource_type == binding.resource_type,
                NotificationSourceBindingModel.resource_id == binding.resource_id,
                NotificationSourceBindingModel.binding_kind
                == binding.binding_kind.value,
            )
        )
        if row is None:
            raise ValidationError("notification source binding insert did not persist")
        existing = _binding_to_domain(row)
        if existing != binding:
            raise ValidationError(
                "notification source binding conflicts with existing route"
            )
        return existing

    def get_binding(
        self,
        workspace_id: UUID,
        resource_type: str,
        resource_id: UUID,
        binding_kind: str,
    ) -> NotificationSourceBinding | None:
        statement = select(NotificationSourceBindingModel).where(
            NotificationSourceBindingModel.workspace_id == workspace_id,
            NotificationSourceBindingModel.resource_type == resource_type,
            NotificationSourceBindingModel.resource_id == resource_id,
            NotificationSourceBindingModel.binding_kind == binding_kind,
        )
        with self._session.no_autoflush:
            row = self._session.scalar(statement)
        return _binding_to_domain(row) if row is not None else None

    def copy_origin_binding(
        self,
        workspace_id: UUID,
        source_resource_type: str,
        source_resource_id: UUID,
        target_resource_type: str,
        target_resource_id: UUID,
    ) -> NotificationSourceBinding | None:
        source = self.get_binding(
            workspace_id,
            source_resource_type,
            source_resource_id,
            NotificationBindingKind.ORIGIN.value,
        )
        if source is None:
            return None
        with self._session.no_autoflush:
            return self.add_binding_or_get_existing(
                NotificationSourceBinding(
                    workspace_id=workspace_id,
                    resource_type=target_resource_type,
                    resource_id=target_resource_id,
                    notification_route_id=source.notification_route_id,
                    binding_kind=source.binding_kind,
                    bound_at=source.bound_at,
                )
            )

    def add_intent_or_get_existing(
        self, intent: NotificationDeliveryIntent
    ) -> NotificationDeliveryIntent:
        self._session.execute(
            insert(NotificationDeliveryIntentModel)
            .values(**_intent_to_model_values(intent))
            .on_conflict_do_nothing()
        )
        row = self._session.scalar(
            select(NotificationDeliveryIntentModel).where(
                NotificationDeliveryIntentModel.workspace_id == intent.workspace_id,
                or_(
                    NotificationDeliveryIntentModel.id == intent.id,
                    and_(
                        NotificationDeliveryIntentModel.notification_fact_id
                        == intent.notification_fact_id,
                        NotificationDeliveryIntentModel.notification_route_id
                        == intent.notification_route_id,
                        NotificationDeliveryIntentModel.delivery_round
                        == intent.delivery_round,
                    ),
                ),
            )
        )
        if row is None:
            raise ValidationError("notification delivery intent insert did not persist")
        existing = _intent_to_domain(row)
        if existing != intent:
            raise ValidationError(
                "notification delivery intent conflicts with existing row"
            )
        return existing

    def get_intent(
        self, workspace_id: UUID, intent_id: UUID, *, for_update: bool = False
    ) -> NotificationDeliveryIntent | None:
        statement = select(NotificationDeliveryIntentModel).where(
            NotificationDeliveryIntentModel.workspace_id == workspace_id,
            NotificationDeliveryIntentModel.id == intent_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _intent_to_domain(row) if row is not None else None

    def save_intent(
        self,
        intent: NotificationDeliveryIntent,
        previous_fencing_token: int,
        expected_status: NotificationDeliveryStatus | str,
    ) -> None:
        expected_status_value = (
            expected_status.value
            if isinstance(expected_status, NotificationDeliveryStatus)
            else expected_status
        )
        result = self._session.execute(
            update(NotificationDeliveryIntentModel)
            .where(
                NotificationDeliveryIntentModel.workspace_id == intent.workspace_id,
                NotificationDeliveryIntentModel.id == intent.id,
                NotificationDeliveryIntentModel.fencing_token == previous_fencing_token,
                NotificationDeliveryIntentModel.status == expected_status_value,
            )
            .values(**_intent_update_values(intent))
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            current = self._session.scalar(
                select(NotificationDeliveryIntentModel.fencing_token).where(
                    NotificationDeliveryIntentModel.workspace_id == intent.workspace_id,
                    NotificationDeliveryIntentModel.id == intent.id,
                )
            )
            raise VersionConflict(previous_fencing_token, int(current or 0))

    def list_due_intents(
        self,
        workspace_id: UUID,
        channel_module_instance_id: UUID,
        now: datetime,
        limit: int,
        *,
        for_update_skip_locked: bool = False,
    ) -> tuple[NotificationDeliveryIntent, ...]:
        _positive_limit(limit)
        statement = (
            select(NotificationDeliveryIntentModel)
            .where(
                NotificationDeliveryIntentModel.workspace_id == workspace_id,
                NotificationDeliveryIntentModel.channel_module_instance_id
                == channel_module_instance_id,
                NotificationDeliveryIntentModel.status.in_(
                    (
                        NotificationDeliveryStatus.PENDING.value,
                        NotificationDeliveryStatus.RETRY_WAIT.value,
                    )
                ),
                NotificationDeliveryIntentModel.next_attempt_at <= now,
            )
            .order_by(
                NotificationDeliveryIntentModel.next_attempt_at.asc(),
                NotificationDeliveryIntentModel.created_at.asc(),
                NotificationDeliveryIntentModel.id.asc(),
            )
            .limit(limit)
        )
        if for_update_skip_locked:
            statement = statement.with_for_update(skip_locked=True)
        rows = self._session.scalars(statement).all()
        return tuple(_intent_to_domain(row) for row in rows)

    def list_expired_claims(
        self,
        workspace_id: UUID,
        now: datetime,
        limit: int,
        *,
        for_update_skip_locked: bool = False,
    ) -> tuple[NotificationDeliveryIntent, ...]:
        _positive_limit(limit)
        statement = (
            select(NotificationDeliveryIntentModel)
            .where(
                NotificationDeliveryIntentModel.workspace_id == workspace_id,
                NotificationDeliveryIntentModel.status
                == NotificationDeliveryStatus.CLAIMED.value,
                NotificationDeliveryIntentModel.claimed_until <= now,
            )
            .order_by(
                NotificationDeliveryIntentModel.claimed_until.asc(),
                NotificationDeliveryIntentModel.id.asc(),
            )
            .limit(limit)
        )
        if for_update_skip_locked:
            statement = statement.with_for_update(skip_locked=True)
        rows = self._session.scalars(statement).all()
        return tuple(_intent_to_domain(row) for row in rows)

    def add_attempt(
        self, workspace_id: UUID, attempt: NotificationDeliveryAttempt
    ) -> None:
        if attempt.outcome is not None or attempt.finished_at is not None:
            raise ValidationError("new delivery attempt must be open")
        if not self._intent_exists(workspace_id, attempt.delivery_intent_id):
            raise ValidationError("delivery attempt intent is outside workspace")
        self._session.add(
            NotificationDeliveryAttemptModel(**_attempt_to_model_values(attempt))
        )

    def finish_attempt(
        self, workspace_id: UUID, attempt: NotificationDeliveryAttempt
    ) -> None:
        if attempt.outcome is None or attempt.finished_at is None:
            raise ValidationError("finished delivery attempt requires settlement")
        if not self._intent_exists(workspace_id, attempt.delivery_intent_id):
            raise ValidationError("delivery attempt intent is outside workspace")
        result = self._session.execute(
            update(NotificationDeliveryAttemptModel)
            .where(
                exists().where(
                    NotificationDeliveryIntentModel.id
                    == NotificationDeliveryAttemptModel.delivery_intent_id,
                    NotificationDeliveryIntentModel.workspace_id == workspace_id,
                ),
                NotificationDeliveryAttemptModel.delivery_intent_id
                == attempt.delivery_intent_id,
                NotificationDeliveryAttemptModel.attempt_number
                == attempt.attempt_number,
                NotificationDeliveryAttemptModel.claim_token == attempt.claim_token,
                NotificationDeliveryAttemptModel.fencing_token == attempt.fencing_token,
                NotificationDeliveryAttemptModel.outcome.is_(None),
                NotificationDeliveryAttemptModel.finished_at.is_(None),
            )
            .values(
                outcome=attempt.outcome.value,
                error_code=attempt.error_code.value if attempt.error_code else None,
                provider_message_ref=attempt.provider_message_ref,
                finished_at=attempt.finished_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            current = self._session.scalar(
                select(NotificationDeliveryAttemptModel)
                .join(
                    NotificationDeliveryIntentModel,
                    NotificationDeliveryIntentModel.id
                    == NotificationDeliveryAttemptModel.delivery_intent_id,
                )
                .where(
                    NotificationDeliveryIntentModel.workspace_id == workspace_id,
                    NotificationDeliveryIntentModel.id == attempt.delivery_intent_id,
                    NotificationDeliveryAttemptModel.attempt_number
                    == attempt.attempt_number,
                )
            )
            if current is None:
                raise ValidationError("delivery attempt is missing")
            if current.outcome is not None or current.finished_at is not None:
                raise ValidationError("delivery attempt is already settled")
            raise ValidationError("delivery attempt claim is stale")

    def list_attempts(
        self, workspace_id: UUID, intent_id: UUID
    ) -> tuple[NotificationDeliveryAttempt, ...]:
        statement = (
            select(NotificationDeliveryAttemptModel)
            .join(
                NotificationDeliveryIntentModel,
                NotificationDeliveryIntentModel.id
                == NotificationDeliveryAttemptModel.delivery_intent_id,
            )
            .where(
                NotificationDeliveryIntentModel.workspace_id == workspace_id,
                NotificationDeliveryIntentModel.id == intent_id,
            )
            .order_by(NotificationDeliveryAttemptModel.attempt_number.asc())
        )
        rows = self._session.scalars(statement).all()
        return tuple(_attempt_to_domain(row) for row in rows)

    def next_delivery_round(
        self, intent: NotificationDeliveryIntent, *, now: datetime
    ) -> NotificationDeliveryIntent:
        next_intent = intent.next_delivery_round(now=now)
        return self.add_intent_or_get_existing(next_intent)

    def get_or_create_cursor(
        self, workspace_id: UUID, policy_version: int, *, now: datetime
    ) -> NotificationReconciliationCursor:
        row = self._session.scalar(
            select(NotificationReconciliationCursorModel).where(
                NotificationReconciliationCursorModel.workspace_id == workspace_id,
                NotificationReconciliationCursorModel.policy_version == policy_version,
            )
        )
        if row is None:
            self._session.execute(
                insert(NotificationReconciliationCursorModel)
                .values(
                    workspace_id=workspace_id,
                    policy_version=policy_version,
                    last_projection_cursor=0,
                    last_reconciled_at=now,
                    lease_owner=None,
                    lease_until=None,
                    version=1,
                )
                .on_conflict_do_nothing(
                    constraint="pk_notification_reconciliation_cursors_identity"
                )
            )
            row = self._session.scalar(
                select(NotificationReconciliationCursorModel).where(
                    NotificationReconciliationCursorModel.workspace_id == workspace_id,
                    NotificationReconciliationCursorModel.policy_version
                    == policy_version,
                )
            )
        if row is None:
            raise ValidationError(
                "notification reconciliation cursor insert did not persist"
            )
        return _cursor_to_domain(row)

    def claim_cursor(
        self,
        workspace_id: UUID,
        policy_version: int,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> NotificationReconciliationCursor | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        current = self.get_or_create_cursor(workspace_id, policy_version, now=now)
        locked = self._session.scalar(
            select(NotificationReconciliationCursorModel)
            .where(
                NotificationReconciliationCursorModel.workspace_id == workspace_id,
                NotificationReconciliationCursorModel.policy_version == policy_version,
            )
            .with_for_update()
        )
        if locked is None:
            return None
        current = _cursor_to_domain(locked)
        if (
            current.lease_until is not None
            and current.lease_until > now
            and current.lease_owner != owner
        ):
            return None
        claimed = current.claim(
            lease_owner=owner,
            lease_until=now + timedelta(seconds=lease_seconds),
            now=now,
        )
        result = self._session.execute(
            update(NotificationReconciliationCursorModel)
            .where(
                NotificationReconciliationCursorModel.workspace_id == workspace_id,
                NotificationReconciliationCursorModel.policy_version == policy_version,
                NotificationReconciliationCursorModel.version == current.version,
                or_(
                    NotificationReconciliationCursorModel.lease_owner == owner,
                    NotificationReconciliationCursorModel.lease_owner.is_(None),
                    NotificationReconciliationCursorModel.lease_until <= now,
                ),
            )
            .values(
                lease_owner=claimed.lease_owner,
                lease_until=claimed.lease_until,
                version=claimed.version,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                current.version,
                self._current_cursor_version(claimed) or 0,
            )
        return claimed

    def save_cursor(
        self,
        cursor: NotificationReconciliationCursor,
        previous_version: int,
        expected_owner: str,
    ) -> None:
        ownership_conditions = [
            NotificationReconciliationCursorModel.lease_owner == expected_owner
        ]
        if cursor.lease_owner == expected_owner:
            ownership_conditions.append(
                NotificationReconciliationCursorModel.lease_owner.is_(None)
            )
        result = self._session.execute(
            update(NotificationReconciliationCursorModel)
            .where(
                NotificationReconciliationCursorModel.workspace_id
                == cursor.workspace_id,
                NotificationReconciliationCursorModel.policy_version
                == cursor.policy_version,
                NotificationReconciliationCursorModel.version == previous_version,
                or_(*ownership_conditions),
            )
            .values(
                last_projection_cursor=cursor.last_projection_cursor,
                last_reconciled_at=cursor.last_reconciled_at,
                lease_owner=cursor.lease_owner,
                lease_until=cursor.lease_until,
                version=cursor.version,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version, self._current_cursor_version(cursor) or 0
            )

    def _current_policy_state_version(
        self, state: NotificationPolicyState
    ) -> int | None:
        return self._session.scalar(
            select(NotificationPolicyStateModel.resource_version).where(
                NotificationPolicyStateModel.workspace_id == state.workspace_id,
                NotificationPolicyStateModel.recipient_type == state.recipient_type,
                NotificationPolicyStateModel.recipient_id == state.recipient_id,
                NotificationPolicyStateModel.subject_type == state.subject_type,
                NotificationPolicyStateModel.subject_id == state.subject_id,
                NotificationPolicyStateModel.topic == state.topic,
            )
        )

    def _current_thread_version(self, thread: InboxThread) -> int | None:
        return self._session.scalar(
            select(NotificationThreadModel.version).where(
                NotificationThreadModel.workspace_id == thread.workspace_id,
                NotificationThreadModel.id == thread.id,
            )
        )

    def _current_route_version(self, route: NotificationRoute) -> int | None:
        return self._session.scalar(
            select(NotificationRouteModel.version).where(
                NotificationRouteModel.workspace_id == route.workspace_id,
                NotificationRouteModel.id == route.id,
            )
        )

    def _current_cursor_version(
        self, cursor: NotificationReconciliationCursor
    ) -> int | None:
        return self._session.scalar(
            select(NotificationReconciliationCursorModel.version).where(
                NotificationReconciliationCursorModel.workspace_id
                == cursor.workspace_id,
                NotificationReconciliationCursorModel.policy_version
                == cursor.policy_version,
            )
        )

    def _intent_exists(self, workspace_id: UUID, intent_id: UUID) -> bool:
        return bool(
            self._session.scalar(
                select(
                    exists().where(
                        NotificationDeliveryIntentModel.workspace_id == workspace_id,
                        NotificationDeliveryIntentModel.id == intent_id,
                    )
                )
            )
        )


def _intent_update_values(intent: NotificationDeliveryIntent) -> dict[str, object]:
    return {
        "status": intent.status.value,
        "attempts": intent.attempts,
        "next_attempt_at": intent.next_attempt_at,
        "claimed_until": intent.claimed_until,
        "claim_token": intent.claim_token,
        "fencing_token": intent.fencing_token,
        "provider_message_ref": intent.provider_message_ref,
        "last_error_code": (
            intent.last_error_code.value if intent.last_error_code else None
        ),
        "delivered_at": intent.delivered_at,
        "exhausted_at": intent.exhausted_at,
        "updated_at": intent.updated_at,
    }


def _copy_json(value: Mapping[str, object]) -> dict[str, object]:
    copied = _copy_json_value(value)
    if not isinstance(copied, dict):
        raise ValidationError("notification payload must be a JSON object")
    return copied


def _copy_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValidationError("notification payload keys must be strings")
        return {key: _copy_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json_value(item) for item in value]
    return value


def _positive_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be positive")
