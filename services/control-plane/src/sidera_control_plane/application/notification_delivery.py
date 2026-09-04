from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from random import random
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    NOTIFICATION_CHANNEL_MODULE_TYPE,
    ModuleLifecycle,
    NotificationChannelCapabilityProfile,
)
from sidera_control_plane.domain.notifications import (
    InboxThread,
    NotificationAttemptErrorCode,
    NotificationAttemptOutcome,
    NotificationDeliveryAttempt,
    NotificationDeliveryIntent,
    NotificationDeliveryStatus,
    NotificationFact,
    NotificationRoute,
    NotificationRouteStatus,
    notification_thread_id,
    notification_thread_key,
)

DEFAULT_NOTIFICATION_DELIVERY_BATCH_SIZE = 50
DEFAULT_NOTIFICATION_CLAIM_LEASE_SECONDS = 30
DEFAULT_NOTIFICATION_DELIVERY_MAX_ATTEMPTS = 10
DEFAULT_NOTIFICATION_DELIVERY_MAX_WINDOW_SECONDS = 86_400
DEFAULT_NOTIFICATION_RETRY_BASE_SECONDS = 2
DEFAULT_NOTIFICATION_RETRY_BACKOFF_MAX_SECONDS = 300


@dataclass(frozen=True, slots=True)
class NotificationDeliveryEnvelope:
    delivery_id: UUID
    notification_id: UUID
    thread_id: UUID
    contract_version: int
    claim_token: UUID
    fencing_token: int
    idempotency_key: str
    channel_type: str
    source_ref: str
    kind: str
    priority: str
    root_task_title: str
    subtask_path: tuple[str, ...]
    worker_item_title: str | None
    title: str
    summary: str
    next_action: str
    deep_link: str
    correlation_id: str
    notification_ref: str
    created_at: datetime

    def to_mapping(self) -> dict[str, object]:
        return {
            "delivery_id": str(self.delivery_id),
            "notification_id": str(self.notification_id),
            "thread_id": str(self.thread_id),
            "contract_version": self.contract_version,
            "claim_token": str(self.claim_token),
            "fencing_token": self.fencing_token,
            "idempotency_key": self.idempotency_key,
            "channel_type": self.channel_type,
            "source_ref": self.source_ref,
            "kind": self.kind,
            "priority": self.priority,
            "root_task_title": self.root_task_title,
            "subtask_path": list(self.subtask_path),
            "worker_item_title": self.worker_item_title,
            "title": self.title,
            "summary": self.summary,
            "next_action": self.next_action,
            "deep_link": self.deep_link,
            "correlation_id": self.correlation_id,
            "notification_ref": self.notification_ref,
            "created_at": _isoformat(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class NotificationDeliverySettlement:
    delivery_id: UUID
    status: NotificationDeliveryStatus
    attempt_number: int
    outcome: NotificationAttemptOutcome
    provider_message_ref: str | None
    next_attempt_at: datetime | None
    fencing_token: int
    error_code: NotificationAttemptErrorCode | None = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "delivery_id": str(self.delivery_id),
            "status": self.status.value,
            "attempt_number": self.attempt_number,
            "outcome": self.outcome.value,
            "provider_message_ref": self.provider_message_ref,
            "next_attempt_at": (
                _isoformat(self.next_attempt_at)
                if self.next_attempt_at is not None
                else None
            ),
            "fencing_token": self.fencing_token,
            "error_code": self.error_code.value if self.error_code else None,
        }


class NotificationDeliveryService:
    """Claim and settle notification deliveries through the control-plane ledger."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], UUID] | None = None,
        random_source: Callable[[], float] | None = None,
        batch_size: int = DEFAULT_NOTIFICATION_DELIVERY_BATCH_SIZE,
        claim_lease_seconds: int = DEFAULT_NOTIFICATION_CLAIM_LEASE_SECONDS,
        max_attempts: int = DEFAULT_NOTIFICATION_DELIVERY_MAX_ATTEMPTS,
        max_window_seconds: int = DEFAULT_NOTIFICATION_DELIVERY_MAX_WINDOW_SECONDS,
        retry_base_seconds: int = DEFAULT_NOTIFICATION_RETRY_BASE_SECONDS,
        retry_backoff_max_seconds: int = (
            DEFAULT_NOTIFICATION_RETRY_BACKOFF_MAX_SECONDS
        ),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or uuid4
        self._random_source = random_source or random
        self._batch_size = _bounded_int(batch_size, "batch_size", 1, 200)
        self._claim_lease_seconds = _bounded_int(
            claim_lease_seconds, "claim_lease_seconds", 5, 300
        )
        self._max_attempts = _bounded_int(max_attempts, "max_attempts", 1, 100)
        self._max_window_seconds = _positive_int(
            max_window_seconds, "max_window_seconds"
        )
        self._retry_base_seconds = _positive_int(
            retry_base_seconds, "retry_base_seconds"
        )
        self._retry_backoff_max_seconds = _positive_int(
            retry_backoff_max_seconds, "retry_backoff_max_seconds"
        )

    def claim(
        self,
        workspace_id: UUID,
        module_instance_id: UUID,
        *,
        supported_contract_versions: Sequence[int],
        batch_size: int | None = None,
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> tuple[NotificationDeliveryEnvelope, ...]:
        current_time = _aware(now or self._clock(), "claim time")
        requested_batch = self._bounded_batch(batch_size)
        requested_lease = self._bounded_lease(lease_seconds)
        versions = _contract_versions(supported_contract_versions)

        with self._uow_factory() as uow:
            instance = uow.modules.get_instance(module_instance_id, workspace_id)
            if instance is None or instance.workspace_id != workspace_id:
                raise ValidationError("notification channel module instance is unavailable")
            definition = uow.modules.get_definition(
                instance.definition_id, workspace_id
            )
            if definition is None or definition.workspace_id != workspace_id:
                raise ValidationError("notification channel module definition is unavailable")
            if definition.module_type != NOTIFICATION_CHANNEL_MODULE_TYPE:
                raise ValidationError("module definition is not a notification channel")
            if instance.definition_id != definition.id:
                raise ValidationError("notification channel module binding is invalid")
            if not isinstance(
                definition.capabilities, NotificationChannelCapabilityProfile
            ):
                raise ValidationError("notification channel capabilities are unavailable")
            capabilities = definition.capabilities
            if not set(versions).intersection(capabilities.delivery_contract_versions):
                raise ValidationError(
                    "notification channel does not support any requested contract version"
                )
            if (
                definition.lifecycle is not ModuleLifecycle.ACTIVE
                or instance.lifecycle is not ModuleLifecycle.ACTIVE
            ):
                return ()

            due_intents = uow.notifications.list_due_intents(
                workspace_id,
                module_instance_id,
                current_time,
                requested_batch,
                for_update_skip_locked=True,
            )
            envelopes: list[NotificationDeliveryEnvelope] = []
            for intent in due_intents:
                if self._is_past_delivery_window(intent, current_time) or (
                    intent.attempts >= self._max_attempts
                ):
                    self._exhaust_due_intent(uow, intent, current_time)
                    continue
                route = uow.notifications.get_route(
                    workspace_id, intent.notification_route_id, for_update=True
                )
                if not self._route_is_claimable(
                    route,
                    intent,
                    module_instance_id,
                    capabilities,
                    versions,
                ):
                    continue
                fact = uow.notifications.get_fact(
                    workspace_id, intent.notification_fact_id
                )
                if fact is None:
                    raise ValidationError("notification fact is unavailable")
                thread = self._get_thread(uow, fact)
                token = self._token_factory()
                if not isinstance(token, UUID):
                    raise ValidationError("claim token factory must return a UUID")
                claim = intent.claim(
                    claim_token=token,
                    fencing_token=intent.fencing_token + 1,
                    claimed_until=current_time
                    + timedelta(seconds=requested_lease),
                    now=current_time,
                )
                uow.notifications.save_intent(
                    claim.intent,
                    intent.fencing_token,
                    intent.status,
                )
                uow.notifications.add_attempt(workspace_id, claim.attempt)
                envelopes.append(
                    self._envelope(uow, claim.intent, fact, route, thread)
                )
            uow.flush()
            return tuple(envelopes)

    def ack(
        self,
        workspace_id: UUID,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        fencing_token: int,
        completed_at: datetime | None = None,
        provider_message_ref: str | None = None,
    ) -> NotificationDeliverySettlement:
        settled_at = _aware(completed_at or self._clock(), "completed_at")
        with self._uow_factory() as uow:
            intent = uow.notifications.get_intent(
                workspace_id, delivery_id, for_update=True
            )
            if intent is None:
                raise ValidationError("notification delivery is unavailable")
            attempt = self._current_attempt(uow, workspace_id, intent)
            next_intent = intent.ack(
                claim_token=claim_token,
                fencing_token=fencing_token,
                provider_message_ref=provider_message_ref,
                now=settled_at,
            )
            if next_intent == intent:
                return self._settlement(next_intent, attempt)
            intent.validate_attempt(attempt)
            settled_attempt = attempt.settle(
                outcome=NotificationAttemptOutcome.DELIVERED,
                now=settled_at,
                provider_message_ref=provider_message_ref,
            )
            uow.notifications.finish_attempt(workspace_id, settled_attempt)
            uow.notifications.save_intent(
                next_intent,
                intent.fencing_token,
                intent.status,
            )
            uow.flush()
            return self._settlement(next_intent, settled_attempt)

    def nack(
        self,
        workspace_id: UUID,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        fencing_token: int,
        outcome: NotificationAttemptOutcome | str,
        error_code: NotificationAttemptErrorCode | str,
        retry_after_seconds: float | None = None,
        provider_message_ref: str | None = None,
        now: datetime | None = None,
    ) -> NotificationDeliverySettlement:
        settled_at = _aware(now or self._clock(), "nack time")
        parsed_outcome = _enum(outcome, NotificationAttemptOutcome, "outcome")
        if parsed_outcome is NotificationAttemptOutcome.DELIVERED:
            raise ValidationError("delivered outcome must use ack")
        parsed_error = _enum(
            error_code, NotificationAttemptErrorCode, "stable_error_code"
        )
        retry_after = _retry_after(retry_after_seconds)

        with self._uow_factory() as uow:
            intent = uow.notifications.get_intent(
                workspace_id, delivery_id, for_update=True
            )
            if intent is None:
                raise ValidationError("notification delivery is unavailable")
            attempt = self._current_attempt(uow, workspace_id, intent)
            intent.validate_attempt(attempt)
            route = uow.notifications.get_route(
                workspace_id, intent.notification_route_id, for_update=True
            )
            settled_attempt = attempt.settle(
                outcome=parsed_outcome,
                now=settled_at,
                error_code=parsed_error,
                provider_message_ref=provider_message_ref,
            )
            if route is None or route.status in {
                NotificationRouteStatus.DISABLED,
                NotificationRouteStatus.REVOKED,
            }:
                route_error = (
                    NotificationAttemptErrorCode.ROUTE_NOT_FOUND
                    if route is None
                    else NotificationAttemptErrorCode.ROUTE_REVOKED
                )
                next_intent = intent.nack(
                    claim_token=claim_token,
                    fencing_token=fencing_token,
                    outcome=NotificationAttemptOutcome.RETRYABLE_FAILURE,
                    error_code=route_error,
                    next_attempt_at=settled_at,
                    now=settled_at,
                ).suppress(reason=route_error, now=settled_at)
            else:
                next_attempt_at = self._next_attempt_at(
                    intent,
                    parsed_outcome,
                    settled_at,
                    retry_after,
                )
                exhaust = (
                    parsed_outcome is NotificationAttemptOutcome.TERMINAL_FAILURE
                    or next_attempt_at is None
                )
                next_intent = intent.nack(
                    claim_token=claim_token,
                    fencing_token=fencing_token,
                    outcome=(
                        NotificationAttemptOutcome.TERMINAL_FAILURE
                        if exhaust
                        else parsed_outcome
                    ),
                    error_code=parsed_error,
                    next_attempt_at=None if exhaust else next_attempt_at,
                    now=settled_at,
                )
            uow.notifications.finish_attempt(workspace_id, settled_attempt)
            uow.notifications.save_intent(
                next_intent,
                intent.fencing_token,
                intent.status,
            )
            uow.flush()
            return self._settlement(next_intent, settled_attempt)

    def recover_expired_claims(
        self,
        workspace_id: UUID,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> int:
        recovered_at = _aware(now or self._clock(), "claim expiry time")
        requested_limit = self._bounded_batch(limit)
        recovered = 0
        with self._uow_factory() as uow:
            expired_claims = uow.notifications.list_expired_claims(
                workspace_id,
                recovered_at,
                requested_limit,
                for_update_skip_locked=True,
            )
            for intent in expired_claims:
                attempt = self._current_attempt(uow, workspace_id, intent)
                if attempt.outcome is None:
                    settled_attempt = attempt.settle(
                        outcome=NotificationAttemptOutcome.UNKNOWN_RESULT,
                        now=recovered_at,
                        error_code=NotificationAttemptErrorCode.UNKNOWN_RESULT,
                    )
                    uow.notifications.finish_attempt(workspace_id, settled_attempt)
                next_intent = intent.expire_claim(now=recovered_at)
                if self._is_past_delivery_window(next_intent, recovered_at) or (
                    next_intent.attempts >= self._max_attempts
                ):
                    next_intent = replace(
                        next_intent,
                        status=NotificationDeliveryStatus.EXHAUSTED,
                        next_attempt_at=recovered_at,
                        exhausted_at=recovered_at,
                        updated_at=recovered_at,
                    )
                uow.notifications.save_intent(
                    next_intent,
                    intent.fencing_token,
                    intent.status,
                )
                recovered += 1
            uow.flush()
        return recovered

    def _bounded_batch(self, value: int | None) -> int:
        return _bounded_int(
            self._batch_size if value is None else value,
            "batch_size",
            1,
            200,
        )

    def _bounded_lease(self, value: int | None) -> int:
        return _bounded_int(
            self._claim_lease_seconds if value is None else value,
            "lease_seconds",
            5,
            300,
        )

    @staticmethod
    def _route_is_claimable(
        route: NotificationRoute | None,
        intent: NotificationDeliveryIntent,
        module_instance_id: UUID,
        capabilities: NotificationChannelCapabilityProfile,
        versions: tuple[int, ...],
    ) -> bool:
        return bool(
            route is not None
            and route.workspace_id == intent.workspace_id
            and route.status is NotificationRouteStatus.ACTIVE
            and route.channel_module_instance_id == module_instance_id
            and route.channel_type in capabilities.channel_types
            and intent.channel_module_instance_id == module_instance_id
            and intent.contract_version in versions
            and intent.contract_version in capabilities.delivery_contract_versions
        )

    def _get_thread(
        self, uow: ControlPlaneUnitOfWork, fact: NotificationFact
    ) -> InboxThread:
        thread_key = notification_thread_key(
            recipient_type=fact.recipient_type,
            recipient_id=fact.recipient_id,
            root_task_id=fact.root_task_id,
            subtask_id=fact.subtask_id,
            worker_item_id=fact.worker_item_id,
            topic=fact.topic,
        )
        thread = uow.notifications.get_thread(
            fact.workspace_id,
            notification_thread_id(thread_key),
        )
        if thread is None:
            raise ValidationError("notification thread is unavailable")
        return thread

    def _envelope(
        self,
        uow: ControlPlaneUnitOfWork,
        intent: NotificationDeliveryIntent,
        fact: NotificationFact,
        route: NotificationRoute | None,
        thread: InboxThread,
    ) -> NotificationDeliveryEnvelope:
        if route is None or intent.claim_token is None:
            raise ValidationError("claimed notification delivery is incomplete")
        root_task = uow.tasks.get(fact.root_task_id, fact.workspace_id)
        root_task_title = root_task.title if root_task is not None else fact.title
        subtask_path: tuple[str, ...] = ()
        if fact.subtask_id is not None:
            subtask = uow.tasks.get(fact.subtask_id, fact.workspace_id)
            if subtask is not None:
                subtask_path = (subtask.title,)
        worker_item_title: str | None = None
        if fact.worker_item_id is not None:
            worker_items = getattr(uow, "worker_items", None)
            if worker_items is not None:
                worker_item = worker_items.get(
                    fact.worker_item_id, fact.workspace_id
                )
                if worker_item is not None:
                    worker_item_title = worker_item.title
        return NotificationDeliveryEnvelope(
            delivery_id=intent.id,
            notification_id=fact.id,
            thread_id=thread.id,
            contract_version=intent.contract_version,
            claim_token=intent.claim_token,
            fencing_token=intent.fencing_token,
            idempotency_key=f"sidera:notification-delivery:{intent.id}",
            channel_type=route.channel_type,
            source_ref=route.source_ref,
            kind=fact.kind,
            priority=fact.priority.value,
            root_task_title=root_task_title,
            subtask_path=subtask_path,
            worker_item_title=worker_item_title,
            title=fact.title,
            summary=fact.summary,
            next_action=fact.action,
            deep_link=fact.deep_link,
            correlation_id=fact.correlation_id,
            notification_ref=f"NTF-{fact.id.hex[:4].upper()}",
            created_at=fact.created_at,
        )

    @staticmethod
    def _current_attempt(
        uow: ControlPlaneUnitOfWork,
        workspace_id: UUID,
        intent: NotificationDeliveryIntent,
    ) -> NotificationDeliveryAttempt:
        attempts = uow.notifications.list_attempts(workspace_id, intent.id)
        for attempt in attempts:
            if attempt.attempt_number == intent.attempts:
                return attempt
        raise ValidationError("current notification delivery attempt is unavailable")

    def _next_attempt_at(
        self,
        intent: NotificationDeliveryIntent,
        outcome: NotificationAttemptOutcome,
        now: datetime,
        retry_after_seconds: float | None,
    ) -> datetime | None:
        if outcome is NotificationAttemptOutcome.TERMINAL_FAILURE:
            return None
        if self._is_past_delivery_window(intent, now) or (
            intent.attempts >= self._max_attempts
        ):
            return None
        exponent = min(intent.attempts - 1, 99)
        nominal = min(
            self._retry_backoff_max_seconds,
            self._retry_base_seconds * (2**exponent),
        )
        jitter = self._random_source()
        if not isinstance(jitter, (int, float)) or isinstance(jitter, bool) or not 0 <= jitter <= 1:
            raise ValidationError("retry jitter must be between 0 and 1")
        delay = nominal * float(jitter)
        if retry_after_seconds is not None:
            delay = max(
                delay,
                min(float(retry_after_seconds), self._retry_backoff_max_seconds),
            )
        next_attempt = now + timedelta(seconds=delay)
        deadline = intent.created_at + timedelta(seconds=self._max_window_seconds)
        if next_attempt > deadline:
            return None
        return next_attempt

    def _is_past_delivery_window(
        self, intent: NotificationDeliveryIntent, now: datetime
    ) -> bool:
        return now >= intent.created_at + timedelta(seconds=self._max_window_seconds)

    @staticmethod
    def _exhaust_due_intent(
        uow: ControlPlaneUnitOfWork,
        intent: NotificationDeliveryIntent,
        now: datetime,
    ) -> None:
        next_intent = replace(
            intent,
            status=NotificationDeliveryStatus.EXHAUSTED,
            next_attempt_at=now,
            last_error_code=(
                intent.last_error_code or NotificationAttemptErrorCode.UNKNOWN_RESULT
            ),
            exhausted_at=now,
            updated_at=now,
        )
        uow.notifications.save_intent(
            next_intent,
            intent.fencing_token,
            intent.status,
        )

    @staticmethod
    def _settlement(
        intent: NotificationDeliveryIntent,
        attempt: NotificationDeliveryAttempt,
    ) -> NotificationDeliverySettlement:
        if attempt.outcome is None:
            raise ValidationError("notification delivery attempt is not settled")
        return NotificationDeliverySettlement(
            delivery_id=intent.id,
            status=intent.status,
            attempt_number=attempt.attempt_number,
            outcome=attempt.outcome,
            provider_message_ref=attempt.provider_message_ref,
            next_attempt_at=(
                intent.next_attempt_at
                if intent.status is NotificationDeliveryStatus.RETRY_WAIT
                else None
            ),
            fencing_token=intent.fencing_token,
            error_code=intent.last_error_code,
        )


def _contract_versions(value: Sequence[int]) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)):
        raise ValidationError("supported_contract_versions must be a sequence")
    versions = tuple(value)
    if not versions or len(versions) > 16:
        raise ValidationError("supported_contract_versions must contain 1 to 16 items")
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in versions):
        raise ValidationError("supported contract versions must be positive integers")
    return tuple(sorted(set(versions)))


def _bounded_int(value: int, name: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ValidationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _positive_int(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be positive")
    return value


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")
    return value


def _enum[T](value: T | str, enum_type: type[T], name: str) -> T:
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} is invalid") from exc


def _retry_after(value: float | None) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
        or value > 86_400
    ):
        raise ValidationError("retry_after_seconds must be between 0 and 86400")
    return float(value)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
