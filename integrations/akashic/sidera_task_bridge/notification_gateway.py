from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from .client import (
    CommandExecution,
    CommandIntent,
    CommandStatus,
    ControlPlaneRejected,
    DispatchResult,
    MalformedNotificationDelivery,
    NotificationDeliveryEnvelope,
    NotificationDeliveryErrorCode,
    TransportUnavailable,
)
from .config import TaskBridgeConfig
from .state import BridgeStateStore

logger = logging.getLogger(__name__)

_PRIVATE_CHANNEL_TYPE = "akashic_private"
_RECIPIENT_TYPE = "workspace_operator"
_RECIPIENT_ID = "local"
_MAX_MESSAGE_BYTES = 8_192
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "transport_unavailable",
        "timeout",
        "rate_limited",
        "provider_unavailable",
        "unknown_result",
    }
)
_TERMINAL_ERROR_CODES = frozenset(
    {
        "route_not_found",
        "route_revoked",
        "permission_denied",
        "contract_unsupported",
        "payload_rejected",
    }
)


class GatewayNotReady(RuntimeError):
    """The local Adapter cannot safely start external notification delivery."""


class GatewayNotificationRunner:
    """Pull safe delivery envelopes and send them through Akashic's host tool."""

    delivery_mode = "gateway"

    def __init__(
        self,
        config: TaskBridgeConfig,
        *,
        client: Any,
        state: BridgeStateStore,
        dispatcher: Any,
        tool_registry: Any,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._state = state
        self._dispatcher = dispatcher
        self._tool_registry = tool_registry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper or asyncio.sleep
        self._stop = asyncio.Event()
        self._startup_lock = asyncio.Lock()
        self._started = False

    def stop(self) -> None:
        self._stop.set()

    async def startup(self) -> None:
        """Run all fail-closed checks before the first claim."""

        if self._started:
            return
        if self._config.notification_mode != "gateway":
            raise GatewayNotReady("notification_mode_not_gateway")
        async with self._startup_lock:
            if self._started:
                return
            pending = await self._state.list_pending_notifications()
            if pending:
                raise GatewayNotReady("legacy_pending_notifications")
            await self._verify_control_plane_readiness()
            await self._bootstrap_routes()
            self._started = True

    async def run(self) -> None:
        """Run the gateway poll loop until Akashic retires the plugin."""

        await self.startup()
        failures = 0
        while not self._stop.is_set():
            try:
                await self.run_once()
                failures = 0
            except (ControlPlaneRejected, TransportUnavailable) as exc:
                logger.warning(
                    "notification gateway control-plane operation failed type=%s",
                    type(exc).__name__,
                )
                delay = min(
                    self._config.notification_retry_base_seconds * (2**failures),
                    self._config.notification_retry_max_seconds,
                )
                failures += 1
                await self._wait_delay(delay)
                continue
            await self._wait_delay(self._config.notification_gateway_poll_seconds)

    async def run_once(self) -> int:
        """Claim and process one bounded batch; return the item count."""

        await self.startup()
        if await self._state.list_pending_notifications():
            raise GatewayNotReady("legacy_pending_notifications")
        module_id = self._config.notification_channel_module_instance_id
        if module_id is None:
            raise GatewayNotReady("notification_channel_module_unavailable")
        items = await self._client.claim_notification_deliveries(
            module_instance_id=module_id,
            supported_contract_versions=self._config.notification_gateway_contract_versions,
            batch_size=self._config.notification_gateway_batch_size,
            lease_seconds=self._config.notification_gateway_lease_seconds,
        )
        for item in items:
            await self._process_item(item)
        return len(items)

    async def _verify_control_plane_readiness(self) -> None:
        readiness = await self._client.get_notification_readiness()
        status = _value(readiness, "status")
        checks = _value(readiness, "checks")
        if status != "ready":
            raise GatewayNotReady("notification_readiness_not_ready")
        if isinstance(checks, (list, tuple)) and any(
            _value(check, "ready") is not True for check in checks
        ):
            raise GatewayNotReady("notification_readiness_check_failed")

        modules = await self._client.list_notification_modules()
        module_id = self._config.notification_channel_module_instance_id
        if module_id is None:
            raise GatewayNotReady("notification_channel_module_unavailable")
        selected = next(
            (item for item in modules if _parse_uuid(_value(item, "id")) == module_id),
            None,
        )
        if selected is None:
            raise GatewayNotReady("notification_channel_module_not_found")
        if (
            _value(selected, "module_type") != "notification_channel"
            or _value(selected, "definition_lifecycle") != "active"
            or _value(selected, "instance_lifecycle") != "active"
        ):
            raise GatewayNotReady("notification_channel_module_not_active")

    async def _bootstrap_routes(self) -> None:
        module_id = self._config.notification_channel_module_instance_id
        if module_id is None:
            raise GatewayNotReady("notification_channel_module_unavailable")
        routes = await self._state.list_private_routes()
        for source_ref, route in routes:
            if route.control_plane_route_id is not None:
                continue
            command = CommandIntent(
                command_type="RegisterNotificationRoute",
                target_type="notification_route",
                target_id=None,
                expected_version=None,
                payload={
                    "channel_module_instance_id": str(module_id),
                    "channel_type": _PRIVATE_CHANNEL_TYPE,
                    "recipient_type": _RECIPIENT_TYPE,
                    "recipient_id": _RECIPIENT_ID,
                    "source_ref": source_ref,
                },
            )
            result = await self._dispatcher.execute(
                f"notification:route:{source_ref}", command
            )
            if result.status != "applied":
                raise GatewayNotReady("notification_route_registration_failed")
            route_id = _result_uuid(result, "notification_route_id")
            if route_id is None:
                raise GatewayNotReady("notification_route_reference_missing")
            await self._state.save_route(
                source_ref,
                route.model_copy(update={"control_plane_route_id": route_id}),
            )

    async def _process_item(self, item: object) -> None:
        if isinstance(item, MalformedNotificationDelivery):
            await self._settle_malformed(item)
            return
        if not isinstance(item, NotificationDeliveryEnvelope):
            await self._settle_malformed(
                MalformedNotificationDelivery(None, None, None)
            )
            return
        if item.channel_type != _PRIVATE_CHANNEL_TYPE:
            await self._nack(
                item,
                outcome="terminal_failure",
                error_code="contract_unsupported",
            )
            return
        if (
            item.contract_version
            not in self._config.notification_gateway_contract_versions
        ):
            await self._nack(
                item,
                outcome="terminal_failure",
                error_code="contract_unsupported",
            )
            return

        route = (await self._state.snapshot()).source_routes.get(item.source_ref)
        if route is None:
            await self._nack(
                item,
                outcome="terminal_failure",
                error_code="route_not_found",
            )
            return
        try:
            message = render_notification_message(item)
        except (TypeError, ValueError):
            await self._nack(
                item,
                outcome="terminal_failure",
                error_code="payload_rejected",
            )
            return

        try:
            receipt = await asyncio.wait_for(
                self._tool_registry.execute(
                    "message_push",
                    {
                        "target_channel": route.channel,
                        "target_chat_id": route.chat_id,
                        "message": message,
                    },
                    internal_arguments={
                        "_commit_role": "passive",
                        "_outbound_metadata": {
                            "delivery_id": str(item.delivery_id),
                            "idempotency_key": item.idempotency_key,
                            "notification_ref": item.notification_ref,
                            "_channel_commit_role": "passive",
                        },
                    },
                    raise_errors=True,
                ),
                timeout=self._config.notification_delivery_timeout_seconds,
            )
        except TimeoutError:
            await self._nack(
                item,
                outcome="retryable_failure",
                error_code="timeout",
            )
            return
        except Exception as exc:  # noqa: BLE001 - provider boundary is normalized below
            error_code = normalize_provider_error(exc)
            await self._nack(
                item,
                outcome=(
                    "retryable_failure"
                    if error_code in _RETRYABLE_ERROR_CODES
                    else "terminal_failure"
                ),
                error_code=error_code,
            )
            return

        if receipt != "消息已发送":
            await self._nack(
                item,
                outcome="retryable_failure",
                error_code=(
                    "unknown_result"
                    if isinstance(receipt, str) and receipt.startswith("消息部分送达")
                    else "provider_unavailable"
                ),
            )
            return

        try:
            await self._client.ack_notification_delivery(
                item.delivery_id,
                claim_token=item.claim_token,
                fencing_token=item.fencing_token,
                completed_at=self._clock(),
            )
        except Exception as exc:  # noqa: BLE001 - leave claim for lease recovery
            logger.warning(
                "notification gateway settle deferred type=%s",
                type(exc).__name__,
            )

    async def _settle_malformed(self, item: MalformedNotificationDelivery) -> None:
        if (
            item.delivery_id is None
            or item.claim_token is None
            or item.fencing_token is None
        ):
            logger.warning(
                "notification gateway discarded an unidentifiable claim item"
            )
            return
        await self._nack_identity(
            item.delivery_id,
            claim_token=item.claim_token,
            fencing_token=item.fencing_token,
            outcome="terminal_failure",
            error_code=item.error_code,
        )

    async def _nack(
        self,
        item: NotificationDeliveryEnvelope,
        *,
        outcome: str,
        error_code: NotificationDeliveryErrorCode,
    ) -> None:
        await self._nack_identity(
            item.delivery_id,
            claim_token=item.claim_token,
            fencing_token=item.fencing_token,
            outcome=outcome,
            error_code=error_code,
        )

    async def _nack_identity(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        fencing_token: int,
        outcome: str,
        error_code: NotificationDeliveryErrorCode,
    ) -> None:
        try:
            await self._client.nack_notification_delivery(
                delivery_id,
                claim_token=claim_token,
                fencing_token=fencing_token,
                outcome=cast(Any, outcome),
                stable_error_code=error_code,
            )
        except Exception as exc:  # noqa: BLE001 - lease recovery is the fallback
            logger.warning(
                "notification gateway nack deferred type=%s",
                type(exc).__name__,
            )

    async def _wait_delay(self, delay: float) -> None:
        async def wait_for_delay() -> None:
            await self._sleeper(delay)

        sleep_task = asyncio.create_task(wait_for_delay())
        stop_task = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            (sleep_task, stop_task), return_when=asyncio.FIRST_COMPLETED
        )
        del done
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(
                *(cast(Awaitable[object], task) for task in pending),
                return_exceptions=True,
            )


def render_notification_message(
    envelope: NotificationDeliveryEnvelope,
    *,
    max_bytes: int = _MAX_MESSAGE_BYTES,
) -> str:
    """Render only safe envelope fields while retaining ref and deep link."""

    if max_bytes < 256:
        raise ValueError("max_bytes is too small for a notification")
    title = _escaped(envelope.title)
    root = _escaped(envelope.root_task_title)
    path = " / ".join(_escaped(item) for item in envelope.subtask_path)
    worker = (
        f"\nWorkerItem: {_escaped(envelope.worker_item_title)}"
        if envelope.worker_item_title
        else ""
    )
    location = f"\nPath: {path}" if path else ""
    stable = (
        f"[{_escaped(envelope.notification_ref)}] {title}\n"
        f"Task: {root}{location}{worker}\n"
        f"Open: {_escaped(envelope.deep_link)}"
    )
    details = (
        f"Summary: {_escaped(envelope.summary)}\n"
        f"Next: {_escaped(envelope.next_action)}\n"
    )
    budget = max_bytes - len(stable.encode("utf-8")) - 1
    if budget < 0:
        raise ValueError("notification envelope cannot fit the message limit")
    details = _truncate_utf8(details, budget)
    return f"{details}\n{stable}" if details else stable


def normalize_provider_error(error: BaseException) -> NotificationDeliveryErrorCode:
    """Map host/provider failures to the public stable error vocabulary."""

    explicit = getattr(error, "error_code", None)
    if (
        isinstance(explicit, str)
        and explicit in _RETRYABLE_ERROR_CODES | _TERMINAL_ERROR_CODES
    ):
        return cast(NotificationDeliveryErrorCode, explicit)
    if isinstance(error, TimeoutError):
        return "timeout"
    marker = f"{type(error).__name__} {error}".lower()
    if "rate" in marker or "429" in marker:
        return "rate_limited"
    if "permission" in marker or "forbidden" in marker or "unauthor" in marker:
        return "permission_denied"
    if "revoked" in marker:
        return "route_revoked"
    if "route" in marker and ("missing" in marker or "not found" in marker):
        return "route_not_found"
    if "payload" in marker or "invalid message" in marker:
        return "payload_rejected"
    if isinstance(error, TransportUnavailable) or "transport" in marker:
        return "transport_unavailable"
    return "provider_unavailable"


def _escaped(value: str) -> str:
    cleaned = "".join(
        character
        for character in value.strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    return html.escape(cleaned, quote=False)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _value(value: object, field: str) -> object:
    if isinstance(value, Mapping):
        return value.get(field)
    return getattr(value, field, None)


def _parse_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _result_uuid(result: DispatchResult, key: str) -> UUID | None:
    execution = result.execution
    data: Mapping[str, object] = {}
    if isinstance(execution, (CommandExecution, CommandStatus)) and execution.result:
        data = execution.result.data
    parsed = _parse_uuid(data.get(key))
    if parsed is not None:
        return parsed
    if isinstance(execution, (CommandExecution, CommandStatus)) and execution.result:
        return execution.result.aggregate_id
    return None


__all__ = [
    "GatewayNotReady",
    "GatewayNotificationRunner",
    "MalformedNotificationDelivery",
    "normalize_provider_error",
    "render_notification_message",
]
