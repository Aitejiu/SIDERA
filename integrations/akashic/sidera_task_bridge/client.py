from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

import httpx
from sidera_control_plane_client import (
    ActorRef,
    CommandEnvelope,
    CommandExecution,
    CommandIntent,
    CommandResponse,
    CommandResultUnknown,
    CommandStatus,
    ControlPlaneClientConfig,
    ControlPlaneRejected,
    JsonValue,
    MalformedNotificationDelivery,
    NotificationCategory,
    NotificationDeliveryEnvelope,
    NotificationDeliveryErrorCode,
    NotificationDeliverySettlement,
    NotificationModuleSummary,
    NotificationReadiness,
    NotificationReadinessCheck,
    NotificationSummary,
    NotificationThreadDetail,
    NotificationThreadPage,
    ProjectionChange,
    ProjectionResyncRequired,
    ProjectionStreamDisconnected,
    TaskResultOverviewView,
    TransportUnavailable,
)
from sidera_control_plane_client import (
    ControlPlaneClient as PublicControlPlaneClient,
)

from .config import TaskBridgeConfig
from .state import (
    BridgeStateStore,
    OutboundCommand,
    SourceType,
    StateConflict,
    same_logical_request,
)

__all__ = [
    "CommandClient",
    "CommandDispatcher",
    "CommandEnvelope",
    "CommandExecution",
    "CommandIntent",
    "CommandResponse",
    "CommandResultUnknown",
    "CommandStatus",
    "ControlPlaneClient",
    "ControlPlaneRejected",
    "DispatchResult",
    "JsonValue",
    "MalformedNotificationDelivery",
    "NotificationCategory",
    "NotificationDeliveryEnvelope",
    "NotificationDeliveryErrorCode",
    "NotificationDeliverySettlement",
    "NotificationModuleSummary",
    "NotificationReadiness",
    "NotificationReadinessCheck",
    "NotificationSummary",
    "NotificationThreadDetail",
    "NotificationThreadPage",
    "ProjectionChange",
    "ProjectionResyncRequired",
    "ProjectionStreamDisconnected",
    "TaskResultOverviewView",
    "TransportUnavailable",
]

_GOVERNANCE_COMMAND_TYPES = frozenset(
    {
        "PauseCollaborationRelease",
        "ApproveCollaborationReleaseProposal",
        "RejectCollaborationReleaseProposal",
        "ResumeAutomaticCollaboration",
    }
)
_NOTIFICATION_COMMAND_TYPES = frozenset(
    {
        "RegisterNotificationRoute",
        "MarkNotificationThreadRead",
        "ArchiveNotificationThread",
        "RequeueNotificationDelivery",
    }
)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Safe local result of a persisted command dispatch."""

    status: Literal["applied", "rejected", "result_unknown", "unavailable"]
    command_id: UUID
    error_code: str | None = None
    execution: CommandExecution | CommandStatus | None = None


class CommandClient(Protocol):
    def build_request_json(self, envelope: CommandEnvelope) -> dict[str, JsonValue]: ...

    def build_intent_request_json(
        self, intent: CommandIntent
    ) -> dict[str, JsonValue]: ...

    async def execute_command_json(
        self, request_json: dict[str, JsonValue], command_id: UUID
    ) -> CommandExecution: ...

    async def get_command_status(self, command_id: UUID) -> CommandStatus: ...


class ControlPlaneClient(PublicControlPlaneClient):
    """Task Bridge compatibility client with host-owned actor policy."""

    def __init__(
        self,
        config: TaskBridgeConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._bridge_config = config
        super().__init__(
            ControlPlaneClientConfig(
                control_plane_url=config.control_plane_url,
                workspace_id=config.workspace_id,
                actor=ActorRef(type=config.actor_type, id=config.actor_id),
                connect_timeout_seconds=config.connect_timeout_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
            ),
            transport=transport,
        )
        self._projection_event_limit_bytes = config.notification_event_limit_bytes

    def build_request_json(self, envelope: CommandEnvelope) -> dict[str, JsonValue]:
        body = super().build_request_json(envelope)
        body["actor"] = self._actor_json(envelope.command_type)
        self._fix_governance_transport(body)
        return body

    def build_intent_request_json(self, intent: CommandIntent) -> dict[str, JsonValue]:
        body = super().build_intent_request_json(intent)
        body["actor"] = self._actor_json(intent.command_type)
        self._fix_governance_transport(body)
        return body

    async def claim_notification_deliveries(
        self,
        *,
        module_instance_id: UUID | None = None,
        supported_contract_versions: Sequence[int] | None = None,
        batch_size: int | None = None,
        lease_seconds: int | None = None,
    ) -> tuple[NotificationDeliveryEnvelope | MalformedNotificationDelivery, ...]:
        config = self._bridge_config
        return await super().claim_notification_deliveries(
            module_instance_id=(
                module_instance_id
                if module_instance_id is not None
                else config.notification_channel_module_instance_id
            ),
            supported_contract_versions=(
                supported_contract_versions
                if supported_contract_versions is not None
                else config.notification_gateway_contract_versions
            ),
            batch_size=(
                batch_size
                if batch_size is not None
                else config.notification_gateway_batch_size
            ),
            lease_seconds=(
                lease_seconds
                if lease_seconds is not None
                else config.notification_gateway_lease_seconds
            ),
        )

    def _actor_json(self, command_type: str) -> dict[str, JsonValue]:
        if command_type in _GOVERNANCE_COMMAND_TYPES | _NOTIFICATION_COMMAND_TYPES:
            actor_type = self._bridge_config.governance_actor_type
            actor_id = self._bridge_config.governance_actor_id
            if actor_type is None or actor_id is None:
                raise ControlPlaneRejected(
                    (
                        "governance_principal_unavailable"
                        if command_type in _GOVERNANCE_COMMAND_TYPES
                        else "notification_principal_unavailable"
                    ),
                    {},
                )
            return {"type": actor_type, "id": actor_id}
        return {
            "type": self._bridge_config.actor_type,
            "id": self._bridge_config.actor_id,
        }

    @staticmethod
    def _fix_governance_transport(body: dict[str, JsonValue]) -> None:
        command_type = body.get("command_type")
        if command_type not in _GOVERNANCE_COMMAND_TYPES:
            return
        payload = body.get("payload")
        if not isinstance(payload, dict):
            payload = {}
            body["payload"] = payload
        payload["transport"] = "main_agent"


class CommandDispatcher:
    """Persist, dispatch, and recover one control-plane command at a time."""

    def __init__(
        self,
        client: CommandClient,
        state: BridgeStateStore,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] | None = None,
        owned_command_types: Collection[str] | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._uuid_factory = uuid_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owned_command_types = (
            frozenset(owned_command_types) if owned_command_types is not None else None
        )

    def new_envelope(self, intent: CommandIntent) -> CommandEnvelope:
        command_id = self._uuid_factory()
        return CommandEnvelope(
            command_id=command_id,
            command_type=intent.command_type,
            target_type=intent.target_type,
            target_id=intent.target_id,
            expected_version=intent.expected_version,
            idempotency_key=f"sidera:{intent.command_type}:{command_id}",
            correlation_id=command_id,
            payload=intent.payload.copy(),
        )

    async def execute(
        self,
        resource_lock_key: str,
        command: CommandEnvelope | CommandIntent,
    ) -> DispatchResult:
        if isinstance(command, CommandIntent):
            existing, matches = await self._find_intent(resource_lock_key, command)
            if existing is not None:
                if matches:
                    return await self._recover_existing(existing)
                return self._busy(existing)
            command = self.new_envelope(command)

        outbound = self._to_outbound(resource_lock_key, command)
        try:
            existing, disposition = await self._state.reserve_or_recover(outbound)
        except StateConflict:
            return DispatchResult(
                status="rejected",
                command_id=command.command_id,
                error_code="command_identity_conflict",
            )
        if disposition == "busy":
            return self._busy(existing)
        if disposition == "recovered":
            return await self._recover_existing(existing)
        return await self._send(existing)

    async def recover_all(self) -> tuple[DispatchResult, ...]:
        """Recover oldest unresolved commands until the dependency is unavailable."""

        results: list[DispatchResult] = []
        for command in await self._state.list_unresolved():
            if not self._owns(command.request_json):
                continue
            try:
                status = await self._client.get_command_status(command.command_id)
            except TransportUnavailable:
                results.append(self._unavailable(command.command_id))
                break
            except ControlPlaneRejected as exc:
                results.append(
                    DispatchResult(
                        status="result_unknown",
                        command_id=command.command_id,
                        error_code=exc.code,
                    )
                )
                continue
            if status.status == "pending":
                results.append(await self._send(command))
            else:
                results.append(await self._settle_status(status))
        return tuple(results)

    async def _find_intent(
        self, resource_lock_key: str, intent: CommandIntent
    ) -> tuple[OutboundCommand | None, bool]:
        request_json = self._client.build_intent_request_json(intent)
        for existing in await self._state.list_resource(resource_lock_key):
            if not self._owns(existing.request_json):
                continue
            if same_logical_request(existing.request_json, request_json):
                return existing, True
            return existing, False
        return None, False

    def _owns(self, request_json: Mapping[str, JsonValue]) -> bool:
        if self._owned_command_types is None:
            return True
        command_type = request_json.get("command_type")
        return (
            isinstance(command_type, str) and command_type in self._owned_command_types
        )

    def _to_outbound(
        self, resource_lock_key: str, command: CommandEnvelope
    ) -> OutboundCommand:
        now = self._clock()
        return OutboundCommand(
            command_id=command.command_id,
            resource_lock_key=resource_lock_key,
            request_json=self._client.build_request_json(command),
            created_at=now,
            last_attempted_at=now,
            attempt_count=0,
        )

    async def _recover_existing(self, command: OutboundCommand) -> DispatchResult:
        try:
            status = await self._client.get_command_status(command.command_id)
        except TransportUnavailable:
            return self._unavailable(command.command_id)
        except ControlPlaneRejected as exc:
            return DispatchResult(
                status="result_unknown",
                command_id=command.command_id,
                error_code=exc.code,
            )
        if status.status == "pending":
            return await self._send(command)
        return await self._settle_status(status)

    async def _send(self, command: OutboundCommand) -> DispatchResult:
        await self._state.record_attempt(command.command_id, at=self._clock())
        stored = await self._state.get_unresolved(command.command_id)
        if stored is None:
            return DispatchResult(
                status="result_unknown",
                command_id=command.command_id,
                error_code="command_state_missing",
            )
        try:
            execution = await self._client.execute_command_json(
                stored.request_json, stored.command_id
            )
        except CommandResultUnknown:
            return await self._resolve_unknown(stored.command_id)
        except TransportUnavailable:
            return self._unavailable(stored.command_id)
        except ControlPlaneRejected as exc:
            await self._state.settle(
                stored.command_id,
                outcome="rejected",
                error_code=exc.code,
            )
            return DispatchResult(
                status="rejected",
                command_id=stored.command_id,
                error_code=exc.code,
            )

        if execution.status == "applied":
            await self._settle_applied(stored, execution.result)
            return DispatchResult(
                status="applied",
                command_id=stored.command_id,
                execution=execution,
            )
        error_code = execution.error_code or "command_rejected"
        await self._state.settle(
            stored.command_id,
            outcome="rejected",
            error_code=error_code,
        )
        return DispatchResult(
            status="rejected",
            command_id=stored.command_id,
            error_code=error_code,
            execution=execution,
        )

    async def _resolve_unknown(self, command_id: UUID) -> DispatchResult:
        try:
            status = await self._client.get_command_status(command_id)
        except TransportUnavailable:
            return DispatchResult(
                status="result_unknown",
                command_id=command_id,
                error_code="command_status_unavailable",
            )
        except ControlPlaneRejected as exc:
            return DispatchResult(
                status="result_unknown",
                command_id=command_id,
                error_code=exc.code,
            )
        if status.status == "pending":
            return DispatchResult(
                status="result_unknown",
                command_id=command_id,
                error_code="command_pending",
                execution=status,
            )
        return await self._settle_status(status)

    async def _settle_status(self, status: CommandStatus) -> DispatchResult:
        if status.status == "applied":
            command = await self._state.get_unresolved(status.command_id)
            if command is None:
                raise StateConflict("cannot settle an unknown command")
            await self._settle_applied(command, status.result)
            return DispatchResult(
                status="applied",
                command_id=status.command_id,
                execution=status,
            )
        error_code = status.error_code or "command_rejected"
        await self._state.settle(
            status.command_id,
            outcome="rejected",
            error_code=error_code,
        )
        return DispatchResult(
            status="rejected",
            command_id=status.command_id,
            error_code=error_code,
            execution=status,
        )

    async def _settle_applied(
        self,
        command: OutboundCommand,
        result: CommandResponse | None,
    ) -> None:
        binding = _submitted_brief_binding(command, result)
        if binding is None:
            await self._state.settle(
                command.command_id,
                outcome="applied",
                error_code=None,
            )
            return
        request_id, source_type, source_ref = binding
        await self._state.settle_applied_command(
            command.command_id,
            planning_request_id=request_id,
            source_type=source_type,
            source_ref=source_ref,
        )

    @staticmethod
    def _busy(command: OutboundCommand) -> DispatchResult:
        return DispatchResult(
            status="result_unknown",
            command_id=command.command_id,
            error_code="resource_busy",
        )

    @staticmethod
    def _unavailable(command_id: UUID) -> DispatchResult:
        return DispatchResult(
            status="unavailable",
            command_id=command_id,
            error_code="service_unavailable",
        )


def _submitted_brief_binding(
    command: OutboundCommand,
    result: CommandResponse | None,
) -> tuple[UUID, SourceType, str] | None:
    if (
        result is None
        or command.request_json.get("command_type") != "SubmitPlanningRequest"
    ):
        return None
    payload = command.request_json.get("payload")
    if not isinstance(payload, dict):
        return None
    source_type = payload.get("source_type")
    source_ref = payload.get("source_ref")
    request_id_value = result.data.get("planning_request_id")
    if source_type not in {"akashic_private", "sidera_web_chat"}:
        return None
    if not isinstance(source_ref, str) or not source_ref.strip():
        return None
    if not isinstance(request_id_value, str):
        return None
    try:
        request_id = UUID(request_id_value)
    except ValueError:
        return None
    return request_id, cast(SourceType, source_type), source_ref
