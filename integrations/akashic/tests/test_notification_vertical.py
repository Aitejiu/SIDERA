"""Provider-free Akashic gateway failure and replay proof."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from sidera_task_bridge.client import (
    CommandExecution,
    CommandResponse,
    DispatchResult,
    NotificationDeliveryEnvelope,
    TransportUnavailable,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.notification_gateway import GatewayNotificationRunner
from sidera_task_bridge.state import BridgeStateStore, PrivateRoute

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
MODULE_ID = UUID("00000000-0000-0000-0000-000000000070")
ROUTE_ID = UUID("00000000-0000-0000-0000-000000000071")
DELIVERY_ID = UUID("00000000-0000-0000-0000-000000000072")
NOTIFICATION_ID = UUID("00000000-0000-0000-0000-000000000073")
THREAD_ID = UUID("00000000-0000-0000-0000-000000000074")
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
SOURCE_REF = "src_" + "v" * 32
FORBIDDEN_PRIVATE_TEXT = "telegram-chat-private-42"


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value


def _config(**overrides: Any) -> TaskBridgeConfig:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "planning_profile_id": uuid4(),
        "planning_profile_version": 1,
        "notification_mode": "gateway",
        "notification_channel_module_instance_id": MODULE_ID,
        "notification_delivery_timeout_seconds": 0.05,
    }
    values.update(overrides)
    return TaskBridgeConfig.model_validate(values)


def _envelope(**overrides: Any) -> NotificationDeliveryEnvelope:
    values: dict[str, Any] = {
        "delivery_id": DELIVERY_ID,
        "notification_id": NOTIFICATION_ID,
        "thread_id": THREAD_ID,
        "contract_version": 1,
        "claim_token": uuid4(),
        "fencing_token": 2,
        "idempotency_key": "sidera:notification-delivery:vertical-1",
        "channel_type": "akashic_private",
        "source_ref": SOURCE_REF,
        "kind": "task_blocked",
        "priority": "action",
        "root_task_title": "Provider-free gateway vertical",
        "subtask_path": ("notification", "gateway"),
        "worker_item_title": "Adapter delivery",
        "title": "Task needs attention",
        "summary": "The task is waiting for a decision.",
        "next_action": "Open the task and decide.",
        "deep_link": f"/workspaces/{WORKSPACE_ID}/tasks/{uuid4()}",
        "correlation_id": "vertical-correlation",
        "notification_ref": "NTF-VERT",
        "created_at": NOW,
    }
    values.update(overrides)
    return NotificationDeliveryEnvelope.model_validate(values)


class FakeClient:
    def __init__(self, batches: list[tuple[object, ...]]) -> None:
        self.batches = batches
        self.acks: list[dict[str, object]] = []
        self.nacks: list[dict[str, object]] = []

    async def get_notification_readiness(self) -> object:
        return {"status": "ready", "checks": [{"ready": True}]}

    async def list_notification_modules(self) -> object:
        return [
            {
                "id": str(MODULE_ID),
                "module_type": "notification_channel",
                "definition_lifecycle": "active",
                "instance_lifecycle": "active",
            }
        ]

    async def claim_notification_deliveries(
        self, **kwargs: object
    ) -> tuple[object, ...]:
        del kwargs
        if self.batches:
            return self.batches.pop(0)
        return ()

    async def ack_notification_delivery(
        self, delivery_id: UUID, **kwargs: object
    ) -> object:
        self.acks.append({"delivery_id": delivery_id, **kwargs})
        return {"status": "delivered"}

    async def nack_notification_delivery(
        self, delivery_id: UUID, **kwargs: object
    ) -> object:
        self.nacks.append({"delivery_id": delivery_id, **kwargs})
        return {"status": "retry_wait"}


class FakeDispatcher:
    async def execute(self, resource_lock_key: str, command: object) -> DispatchResult:
        del resource_lock_key, command
        command_id = uuid4()
        return DispatchResult(
            status="applied",
            command_id=command_id,
            execution=CommandExecution(
                status="applied",
                command_id=command_id,
                result=CommandResponse(
                    command_id=command_id,
                    aggregate_id=ROUTE_ID,
                    aggregate_version=1,
                    event_ids=(),
                    data={"notification_route_id": str(ROUTE_ID)},
                ),
            ),
        )


class IdempotentMessageSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.accepted_keys: set[str] = set()
        self.duplicates: list[str] = []
        self.fail_after_first_send = True

    async def execute(
        self,
        tool_name: str,
        payload: dict[str, object],
        *,
        internal_arguments: dict[str, object] | None = None,
        raise_errors: bool,
    ) -> str:
        del raise_errors
        raw_metadata = (internal_arguments or {}).get("_outbound_metadata", {})
        if not isinstance(raw_metadata, dict):
            raise TypeError("outbound metadata must be a mapping")
        metadata = dict(raw_metadata)
        key_value = metadata.get("idempotency_key")
        if not isinstance(key_value, str):
            raise TypeError("outbound metadata must contain an idempotency key")
        key = key_value
        self.calls.append(
            {"tool_name": tool_name, "payload": payload, "metadata": metadata}
        )
        if key in self.accepted_keys:
            self.duplicates.append(key)
        else:
            self.accepted_keys.add(key)
        if self.fail_after_first_send:
            self.fail_after_first_send = False
            raise TransportUnavailable("ack transport lost after provider send")
        return "消息已发送"


def _runner(
    client: FakeClient,
    sink: IdempotentMessageSink,
    state: BridgeStateStore,
) -> GatewayNotificationRunner:
    return GatewayNotificationRunner(
        _config(),
        client=client,
        state=state,
        dispatcher=FakeDispatcher(),
        tool_registry=sink,
        clock=lambda: NOW,
        sleeper=lambda _delay: asyncio.sleep(0),
    )


def test_gateway_provider_send_before_ack_replays_one_stable_identity() -> None:
    async def scenario() -> None:
        state = BridgeStateStore(MemoryKV())
        await state.save_route(
            SOURCE_REF,
            PrivateRoute(
                channel="telegram",
                chat_id=FORBIDDEN_PRIVATE_TEXT,
                session_key="telegram:private",
            ),
        )
        first = _envelope(claim_token=uuid4(), fencing_token=2)
        replay = _envelope(claim_token=uuid4(), fencing_token=3)
        client = FakeClient([(first,), (replay,)])
        sink = IdempotentMessageSink()
        runner = _runner(client, sink, state)

        await runner.run_once()
        await runner.run_once()

        assert len(sink.calls) == 2
        first_metadata = sink.calls[0]["metadata"]
        second_metadata = sink.calls[1]["metadata"]
        assert first_metadata == second_metadata
        assert isinstance(first_metadata, dict)
        assert first_metadata["notification_ref"] == "NTF-VERT"
        assert sink.duplicates == [
            "sidera:notification-delivery:vertical-1"
        ]
        assert client.nacks[0]["stable_error_code"] == "transport_unavailable"
        assert client.acks[0]["delivery_id"] == DELIVERY_ID
        assert client.acks[0]["claim_token"] == replay.claim_token
        assert first.source_ref == replay.source_ref == SOURCE_REF
        assert FORBIDDEN_PRIVATE_TEXT not in str(first)
        assert FORBIDDEN_PRIVATE_TEXT not in str(replay)

    asyncio.run(scenario())


def test_gateway_rejects_legacy_gateway_dual_mode_before_startup() -> None:
    with pytest.raises(ValidationError):
        _config(notification_mode="both")
