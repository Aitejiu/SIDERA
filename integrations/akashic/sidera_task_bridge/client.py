from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, Protocol, cast
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import TaskBridgeConfig
from .contracts import (
    JsonValue,
    NotificationCategory,
    NotificationSummary,
    NotificationThreadDetail,
    NotificationThreadPage,
    ProjectionChange,
)
from .state import (
    BridgeStateStore,
    OutboundCommand,
    SourceType,
    StateConflict,
    same_logical_request,
)

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
_SECRET_MARKER_PATTERN = re.compile(
    r"(?:api[_ -]?key|authorization|bearer|password|secret|token)", re.IGNORECASE
)
_DELIVERY_ERROR_CODES = frozenset(
    {
        "transport_unavailable",
        "timeout",
        "rate_limited",
        "provider_unavailable",
        "unknown_result",
        "route_not_found",
        "route_revoked",
        "permission_denied",
        "contract_unsupported",
        "payload_rejected",
    }
)


class CommandEnvelope(BaseModel):
    """Command fields supplied by the bridge dispatcher."""

    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    command_type: str
    target_type: str
    target_id: UUID | None
    expected_version: int | None
    idempotency_key: str
    correlation_id: UUID
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class CommandIntent(BaseModel):
    """Business fields used to create a new command envelope."""

    model_config = ConfigDict(extra="forbid")

    command_type: str
    target_type: str
    target_id: UUID | None
    expected_version: int | None
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class CommandResponse(BaseModel):
    """Safe applied command result returned by the control plane."""

    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    aggregate_id: UUID
    aggregate_version: int
    event_ids: tuple[UUID, ...]
    data: dict[str, JsonValue] = Field(default_factory=dict)


class CommandExecution(BaseModel):
    """Normalized result of a command POST."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["applied", "rejected"]
    command_id: UUID
    result: CommandResponse | None = None
    error_code: str | None = None


class CommandStatus(BaseModel):
    """Terminal or pending command status from the public query API."""

    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    status: Literal["pending", "applied", "rejected"]
    result: CommandResponse | None = None
    error_code: str | None = None


class NotificationDeliveryEnvelope(BaseModel):
    """Strict provider-neutral payload claimed by a channel Adapter."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    notification_id: UUID
    thread_id: UUID
    contract_version: int = Field(ge=1)
    claim_token: UUID
    fencing_token: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=512)
    channel_type: str = Field(min_length=1, max_length=64)
    source_ref: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=128)
    priority: str = Field(min_length=1, max_length=64)
    root_task_title: str = Field(min_length=1, max_length=300)
    subtask_path: tuple[str, ...] = Field(max_length=32)
    worker_item_title: str | None = Field(default=None, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4_000)
    next_action: str = Field(min_length=1, max_length=500)
    deep_link: str = Field(min_length=1, max_length=1_000)
    correlation_id: str = Field(min_length=1, max_length=200)
    notification_ref: str = Field(min_length=1, max_length=128)
    created_at: datetime

    @field_validator(
        "idempotency_key",
        "channel_type",
        "source_ref",
        "kind",
        "priority",
        "root_task_title",
        "title",
        "summary",
        "next_action",
        "deep_link",
        "correlation_id",
        "notification_ref",
        "worker_item_title",
    )
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("notification delivery text must not be empty")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("notification delivery text contains control characters")
        return value.strip()

    @field_validator("subtask_path")
    @classmethod
    def validate_subtask_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item.strip() or len(item) > 300:
                raise ValueError("subtask path item is outside supported bounds")
            if any(ord(character) < 32 or ord(character) == 127 for character in item):
                raise ValueError("subtask path contains control characters")
        return tuple(item.strip() for item in value)


NotificationDeliveryErrorCode = Literal[
    "transport_unavailable",
    "timeout",
    "rate_limited",
    "provider_unavailable",
    "unknown_result",
    "route_not_found",
    "route_revoked",
    "permission_denied",
    "contract_unsupported",
    "payload_rejected",
]


@dataclass(frozen=True, slots=True)
class MalformedNotificationDelivery:
    """Safe identity extracted from one invalid claim item for terminal nack."""

    delivery_id: UUID | None
    claim_token: UUID | None
    fencing_token: int | None
    error_code: NotificationDeliveryErrorCode = "payload_rejected"


class NotificationDeliverySettlement(BaseModel):
    """Strict response returned after one claim is settled."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    status: Literal[
        "pending",
        "claimed",
        "retry_wait",
        "delivered",
        "exhausted",
        "suppressed",
    ]
    attempt_number: int = Field(ge=1)
    outcome: Literal[
        "delivered", "retryable_failure", "terminal_failure", "unknown_result"
    ]
    provider_message_ref: str | None = Field(default=None, max_length=512)
    next_attempt_at: datetime | None
    fencing_token: int = Field(ge=1)
    error_code: str | None = Field(default=None, max_length=128)


class NotificationReadinessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    ready: bool
    code: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=512)


class NotificationReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str = Field(min_length=1, max_length=128)
    as_of: datetime
    status: Literal["ready", "not_ready"]
    checks: tuple[NotificationReadinessCheck, ...]


class NotificationModuleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    instance_name: str = Field(min_length=1, max_length=300)
    provider: str = Field(min_length=1, max_length=128)
    module_type: str = Field(min_length=1, max_length=128)
    config_version: int = Field(ge=1)
    definition_lifecycle: str = Field(min_length=1, max_length=64)
    instance_lifecycle: str = Field(min_length=1, max_length=64)


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


class TransportUnavailable(RuntimeError):
    """The control plane could not be read or reached."""


class ProjectionResyncRequired(RuntimeError):
    """The stream cannot safely continue from its current cursor."""

    def __init__(self, reason: str, last_confirmed_cursor: int) -> None:
        self.reason = reason
        self.last_confirmed_cursor = last_confirmed_cursor
        super().__init__(f"projection stream requires resync: {reason}")


class ProjectionStreamDisconnected(RuntimeError):
    """The stream ended or lost transport before a safe terminal event."""

    def __init__(self, last_confirmed_cursor: int) -> None:
        self.last_confirmed_cursor = last_confirmed_cursor
        super().__init__("projection stream disconnected")


class CommandResultUnknown(RuntimeError):
    """A command POST may have reached the control plane without a response."""

    def __init__(self, command_id: UUID) -> None:
        super().__init__("command result is unknown")
        self.command_id = command_id


class ControlPlaneRejected(RuntimeError):
    """A safe, structured error returned by the control plane."""

    def __init__(self, code: str, details: Mapping[str, JsonValue]) -> None:
        super().__init__(code)
        self.code = code
        self.details = dict(details)


class ControlPlaneClient:
    """Call only the public, workspace-scoped SIDERA HTTP contract."""

    def __init__(
        self,
        config: TaskBridgeConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=str(config.control_plane_url).rstrip("/"),
            trust_env=False,
            timeout=httpx.Timeout(
                timeout=config.request_timeout_seconds,
                connect=config.connect_timeout_seconds,
                read=config.request_timeout_seconds,
                write=config.request_timeout_seconds,
                pool=config.request_timeout_seconds,
            ),
            transport=transport,
        )

    async def execute_command(self, envelope: CommandEnvelope) -> CommandExecution:
        """Submit one exact command envelope without retrying it."""

        return await self.execute_command_json(
            self.build_request_json(envelope), envelope.command_id
        )

    def build_request_json(self, envelope: CommandEnvelope) -> dict[str, JsonValue]:
        body = cast(dict[str, JsonValue], envelope.model_dump(mode="json"))
        body["workspace_id"] = str(self._config.workspace_id)
        body["actor"] = self._actor_json(envelope.command_type)
        self._fix_governance_transport(body)
        return body

    def build_intent_request_json(self, intent: CommandIntent) -> dict[str, JsonValue]:
        body = cast(dict[str, JsonValue], intent.model_dump(mode="json"))
        body["workspace_id"] = str(self._config.workspace_id)
        body["actor"] = self._actor_json(intent.command_type)
        self._fix_governance_transport(body)
        return body

    async def execute_command_json(
        self, request_json: dict[str, JsonValue], command_id: UUID
    ) -> CommandExecution:
        response = await self._request(
            "POST",
            "/api/v1/commands",
            json=request_json,
            command_id=command_id,
        )
        if response.status_code in {200, 201}:
            result = self._parse_model(response, CommandResponse)
            return CommandExecution(
                status="applied",
                command_id=command_id,
                result=result,
            )
        self._raise_rejected(response)

    async def get_command_status(self, command_id: UUID) -> CommandStatus:
        """Read the public terminal status for one command."""

        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/commands/{command_id}",
        )
        return self._parse_model(response, CommandStatus)

    async def get_notification_summary(self) -> NotificationSummary:
        """Read the current workspace operator notification counters."""

        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/notification-summary",
        )
        return self._parse_model(response, NotificationSummary)

    async def list_notification_threads(
        self,
        *,
        category: NotificationCategory = "action",
        root_task_id: UUID | None = None,
        subtask_id: UUID | None = None,
        unread_only: bool = False,
        cursor: str | None = None,
        limit: int = 20,
    ) -> NotificationThreadPage:
        """Read recipient-scoped notification threads with opaque pagination."""

        params = _notification_thread_params(
            category=category,
            root_task_id=root_task_id,
            subtask_id=subtask_id,
            unread_only=unread_only,
            cursor=cursor,
            limit=limit,
        )
        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/notification-threads",
            params=params,
        )
        return self._parse_model(response, NotificationThreadPage)

    async def list_notification_route_threads(
        self,
        route_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> NotificationThreadPage:
        """Read notification threads for one exact origin Route."""

        params = _notification_pagination_params(cursor=cursor, limit=limit)
        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/"
            f"notification-routes/{route_id}/threads",
            params=params,
        )
        return self._parse_model(response, NotificationThreadPage)

    async def get_notification_thread(
        self,
        thread_id: UUID,
        *,
        fact_cursor: str | None = None,
        fact_limit: int = 50,
    ) -> NotificationThreadDetail:
        """Read one Thread and its bounded Fact/Delivery detail."""

        params = _notification_pagination_params(cursor=fact_cursor, limit=fact_limit)
        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/"
            f"notification-threads/{thread_id}",
            params=params,
        )
        return self._parse_model(response, NotificationThreadDetail)

    async def get_notification_readiness(self) -> NotificationReadiness:
        """Read the Control Plane gates required before gateway delivery."""

        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/notification-readiness",
        )
        return self._parse_model(response, NotificationReadiness)

    async def list_notification_modules(
        self,
    ) -> tuple[NotificationModuleSummary, ...]:
        """Read notification-channel module lifecycle metadata for this workspace."""

        response = await self._request(
            "GET",
            "/api/v1/modules",
            params={
                "workspace_id": str(self._config.workspace_id),
                "module_type": "notification_channel",
            },
        )
        if response.status_code >= 400:
            self._raise_rejected(response)
        try:
            body = response.json()
            if not isinstance(body, list):
                raise TypeError
            return tuple(
                NotificationModuleSummary.model_validate(item) for item in body
            )
        except (TypeError, ValueError, ValidationError):
            raise ControlPlaneRejected("invalid_control_plane_response", {}) from None

    async def claim_notification_deliveries(
        self,
        *,
        module_instance_id: UUID | None = None,
        supported_contract_versions: Sequence[int] | None = None,
        batch_size: int | None = None,
        lease_seconds: int | None = None,
    ) -> tuple[NotificationDeliveryEnvelope | MalformedNotificationDelivery, ...]:
        """Claim a bounded batch and preserve safe identities for malformed items."""

        module_id = (
            module_instance_id or self._config.notification_channel_module_instance_id
        )
        if module_id is None:
            raise ControlPlaneRejected("notification_channel_module_unavailable", {})
        versions = tuple(
            supported_contract_versions
            if supported_contract_versions is not None
            else self._config.notification_gateway_contract_versions
        )
        requested_batch = (
            self._config.notification_gateway_batch_size
            if batch_size is None
            else batch_size
        )
        requested_lease = (
            self._config.notification_gateway_lease_seconds
            if lease_seconds is None
            else lease_seconds
        )
        _validate_delivery_claim_inputs(
            module_id,
            versions,
            requested_batch,
            requested_lease,
        )
        request_json = {
            "module_instance_id": str(module_id),
            "supported_contract_versions": list(versions),
            "batch_size": requested_batch,
            "lease_seconds": requested_lease,
        }
        response = await self._request(
            "POST",
            f"/api/v1/workspaces/{self._config.workspace_id}/notification-deliveries/claims",
            json=request_json,
        )
        if response.status_code >= 400:
            self._raise_rejected(response)
        try:
            body = response.json()
        except ValueError:
            raise ControlPlaneRejected("invalid_control_plane_response", {}) from None
        if not isinstance(body, dict) or set(body) != {"deliveries"}:
            raise ControlPlaneRejected("invalid_control_plane_response", {})
        raw_deliveries = body.get("deliveries")
        if not isinstance(raw_deliveries, list):
            raise ControlPlaneRejected("invalid_control_plane_response", {})
        parsed: list[NotificationDeliveryEnvelope | MalformedNotificationDelivery] = []
        for raw in raw_deliveries:
            if not isinstance(raw, dict):
                parsed.append(MalformedNotificationDelivery(None, None, None))
                continue
            try:
                parsed.append(NotificationDeliveryEnvelope.model_validate(raw))
            except ValidationError:
                parsed.append(_malformed_delivery(raw))
        return tuple(parsed)

    async def ack_notification_delivery(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        fencing_token: int,
        completed_at: datetime,
        provider_message_ref: str | None = None,
    ) -> NotificationDeliverySettlement:
        """Acknowledge one successful provider send with its current fence."""

        _validate_delivery_identity(delivery_id, claim_token, fencing_token)
        _validate_provider_message_ref(provider_message_ref)
        response = await self._request(
            "POST",
            f"/api/v1/workspaces/{self._config.workspace_id}/"
            f"notification-deliveries/{delivery_id}/ack",
            json={
                "claim_token": str(claim_token),
                "fencing_token": fencing_token,
                "completed_at": completed_at.isoformat(),
                "provider_message_ref": provider_message_ref,
            },
        )
        return self._parse_model(response, NotificationDeliverySettlement)

    async def nack_notification_delivery(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        fencing_token: int,
        outcome: Literal["retryable_failure", "terminal_failure", "unknown_result"],
        stable_error_code: NotificationDeliveryErrorCode,
        retry_after_seconds: float | None = None,
        provider_message_ref: str | None = None,
    ) -> NotificationDeliverySettlement:
        """Report one normalized provider result without sending raw error data."""

        _validate_delivery_identity(delivery_id, claim_token, fencing_token)
        if stable_error_code not in _DELIVERY_ERROR_CODES:
            raise ValueError("stable_error_code is unsupported")
        if retry_after_seconds is not None and not 0 <= retry_after_seconds <= 86_400:
            raise ValueError("retry_after_seconds is outside supported bounds")
        _validate_provider_message_ref(provider_message_ref)
        response = await self._request(
            "POST",
            f"/api/v1/workspaces/{self._config.workspace_id}/"
            f"notification-deliveries/{delivery_id}/nack",
            json={
                "claim_token": str(claim_token),
                "fencing_token": fencing_token,
                "outcome": outcome,
                "stable_error_code": stable_error_code,
                "retry_after_seconds": retry_after_seconds,
                "provider_message_ref": provider_message_ref,
            },
        )
        return self._parse_model(response, NotificationDeliverySettlement)

    def build_mark_notification_thread_read(
        self,
        thread_id: UUID,
        *,
        expected_version: int,
        expected_latest_fact_id: UUID,
    ) -> CommandIntent:
        """Build the exact optimistic read command without duplicate IDs."""

        _require_positive_version(expected_version, "expected_version")
        return CommandIntent(
            command_type="MarkNotificationThreadRead",
            target_type="notification_thread",
            target_id=thread_id,
            expected_version=expected_version,
            payload={"expected_latest_fact_id": str(expected_latest_fact_id)},
        )

    def build_archive_notification_thread(
        self, thread_id: UUID, *, expected_version: int
    ) -> CommandIntent:
        """Build the exact empty-payload Thread archive command."""

        _require_positive_version(expected_version, "expected_version")
        return CommandIntent(
            command_type="ArchiveNotificationThread",
            target_type="notification_thread",
            target_id=thread_id,
            expected_version=expected_version,
            payload={},
        )

    def build_requeue_notification_delivery(
        self,
        delivery_id: UUID,
        *,
        expected_route_version: int,
        reason: str,
    ) -> CommandIntent:
        """Build the exhausted-delivery requeue command with one target ID."""

        _require_positive_version(expected_route_version, "expected_route_version")
        normalized_reason = _validate_reason(reason)
        return CommandIntent(
            command_type="RequeueNotificationDelivery",
            target_type="notification_delivery",
            target_id=delivery_id,
            expected_version=None,
            payload={
                "expected_status": "exhausted",
                "expected_route_version": expected_route_version,
                "reason": normalized_reason,
            },
        )

    async def get_planning_detail(self, request_id: UUID) -> dict[str, JsonValue]:
        """Read one PlanningRequest detail projection."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/planning-requests/{request_id}"
        )

    async def get_task(self, task_id: UUID) -> dict[str, JsonValue]:
        """Read one task aggregate, including its optimistic-concurrency version."""

        response = await self._request(
            "GET",
            f"/api/v1/tasks/{task_id}",
            params={"workspace_id": str(self._config.workspace_id)},
        )
        return self._parse_json_object(response)

    async def get_active_plan(self, task_id: UUID) -> dict[str, JsonValue]:
        """Read the active plan head used as a revision base."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/tasks/{task_id}/active-plan"
        )

    async def get_revision_diff(self, request_id: UUID) -> dict[str, JsonValue]:
        """Read the bounded, workspace-scoped revision diff projection."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/planning-requests/{request_id}/diff"
        )

    async def get_plan_transition(
        self, plan_transition_id: UUID
    ) -> dict[str, JsonValue]:
        """Read one plan transition and its worker-level convergence facts."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/plan-transitions/{plan_transition_id}"
        )

    async def list_plan_transitions(
        self, task_id: UUID
    ) -> tuple[dict[str, JsonValue], ...]:
        """List plan transitions for one root Task during notification resync."""

        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/tasks/{task_id}/plan-transitions",
        )
        try:
            body = response.json()
        except ValueError:
            raise ControlPlaneRejected("invalid_control_plane_response", {}) from None
        if response.status_code >= 400:
            self._raise_rejected_body(body if isinstance(body, dict) else {})
        if not isinstance(body, list) or not all(
            isinstance(item, dict) for item in body
        ):
            raise ControlPlaneRejected("invalid_control_plane_response", {})
        return tuple(cast(dict[str, JsonValue], item) for item in body)

    async def list_clarifications(
        self, request_id: UUID
    ) -> tuple[dict[str, JsonValue], ...]:
        """Read pending and answered clarification projections."""

        body = await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/planning-requests/{request_id}/clarifications"
        )
        items = body.get("items")
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise ControlPlaneRejected("invalid_control_plane_response", {})
        return tuple(cast(dict[str, JsonValue], item) for item in items)

    async def get_proposal(
        self, request_id: UUID, revision: int
    ) -> dict[str, JsonValue]:
        """Read one exact proposal revision."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/planning-requests/{request_id}/proposals/{revision}"
        )

    async def get_materialization(self, request_id: UUID) -> dict[str, JsonValue]:
        """Read materialization, returning an empty object before confirmation."""

        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/planning-requests/{request_id}/materialization",
        )
        if response.status_code == 404:
            return {}
        return self._parse_json_object(response)

    async def get_task_overview(self, task_id: UUID) -> dict[str, JsonValue]:
        """Read Task First overview through its public workspace filter."""

        response = await self._request(
            "GET",
            f"/api/v1/tasks/{task_id}/overview",
            params={"workspace_id": str(self._config.workspace_id)},
        )
        return self._parse_json_object(response)

    async def get_collaboration_governance_worker_item(
        self, worker_item_id: UUID
    ) -> dict[str, JsonValue]:
        """Read the bounded governance view for one WorkerItem."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/"
            f"collaboration-governance/worker-items/{worker_item_id}"
        )

    async def get_collaboration_governance_proposal(
        self, proposal_id: UUID
    ) -> dict[str, JsonValue]:
        """Read the bounded governance view for one release Proposal."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/"
            f"collaboration-governance/proposals/{proposal_id}"
        )

    async def get_worker_item_workspace(
        self, worker_item_id: UUID
    ) -> dict[str, JsonValue]:
        """Read current WorkerItem identity/version for optimistic fences."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/"
            f"worker-items/{worker_item_id}/workspace"
        )

    async def iter_projection_changes(
        self, *, after: int
    ) -> AsyncIterator[ProjectionChange]:
        """Consume bounded Projection SSE invalidations without provider imports."""

        if after < 0:
            raise ValueError("after must be non-negative")
        current = after
        parser = _ProjectionSSEParser(
            max_event_bytes=int(
                getattr(self._config, "notification_event_limit_bytes", 262_144)
            )
        )
        path = f"/api/v1/workspaces/{self._config.workspace_id}/projection-changes"
        try:
            async with self._client.stream(
                "GET",
                path,
                params={"after": str(after), "schema_version": "1"},
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status_code >= 400:
                    raise ProjectionResyncRequired("http_status", current)
                async for chunk in response.aiter_bytes():
                    try:
                        events = parser.feed(chunk)
                    except _ProjectionSSEParseError as exc:
                        raise ProjectionResyncRequired(exc.reason, current) from None
                    for event in events:
                        change = _parse_projection_event(
                            event,
                            workspace_id=self._config.workspace_id,
                            current_cursor=current,
                        )
                        if change is None:
                            continue
                        if change.cursor <= current:
                            continue
                        if change.cursor != current + 1:
                            raise ProjectionResyncRequired("cursor_gap", current)
                        current = change.cursor
                        yield change
                if parser.has_partial_event:
                    raise ProjectionStreamDisconnected(current)
                raise ProjectionStreamDisconnected(current)
        except (ProjectionResyncRequired, ProjectionStreamDisconnected):
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise ProjectionStreamDisconnected(current) from None

    async def close(self) -> None:
        """Release the HTTP transport owned by this client."""

        await self._client.aclose()

    def _actor_json(self, command_type: str) -> dict[str, JsonValue]:
        if command_type in _GOVERNANCE_COMMAND_TYPES | _NOTIFICATION_COMMAND_TYPES:
            actor_type = self._config.governance_actor_type
            actor_id = self._config.governance_actor_id
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
            "type": self._config.actor_type,
            "id": self._config.actor_id,
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

    async def _get_object(self, path: str) -> dict[str, JsonValue]:
        response = await self._request("GET", path)
        return self._parse_json_object(response)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        params: Mapping[str, str] | None = None,
        command_id: UUID | None = None,
    ) -> httpx.Response:
        try:
            return await self._client.request(method, path, json=json, params=params)
        except (httpx.TimeoutException, httpx.TransportError):
            if command_id is not None:
                raise CommandResultUnknown(command_id) from None
            raise TransportUnavailable("control plane is unavailable") from None

    @staticmethod
    def _parse_model(response: httpx.Response, model: type[BaseModel]) -> Any:
        if response.status_code >= 400:
            ControlPlaneClient._raise_rejected(response)
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ControlPlaneRejected("invalid_control_plane_response", {}) from None

    @classmethod
    def _parse_json_object(cls, response: httpx.Response) -> dict[str, JsonValue]:
        try:
            body = response.json()
        except ValueError:
            raise ControlPlaneRejected("invalid_control_plane_response", {}) from None
        if not isinstance(body, dict):
            raise ControlPlaneRejected("invalid_control_plane_response", {})
        if response.status_code >= 400:
            cls._raise_rejected_body(body)
        return cast(dict[str, JsonValue], body)

    @classmethod
    def _raise_rejected(cls, response: httpx.Response) -> NoReturn:
        try:
            body = response.json()
        except ValueError:
            raise ControlPlaneRejected("invalid_control_plane_response", {}) from None
        if isinstance(body, dict):
            cls._raise_rejected_body(body)
        raise ControlPlaneRejected("invalid_control_plane_response", {})

    @staticmethod
    def _raise_rejected_body(body: dict[str, Any]) -> NoReturn:
        error = body.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            raise ControlPlaneRejected("invalid_control_plane_response", {})
        details = error.get("details", {})
        if not isinstance(details, dict):
            details = {}
        raise ControlPlaneRejected(
            error["code"],
            cast(Mapping[str, JsonValue], details),
        )


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


def _validate_delivery_claim_inputs(
    module_instance_id: UUID,
    supported_contract_versions: Sequence[int],
    batch_size: int,
    lease_seconds: int,
) -> None:
    if not isinstance(module_instance_id, UUID):
        raise TypeError("module_instance_id must be a UUID")
    if not supported_contract_versions or len(supported_contract_versions) > 16:
        raise ValueError("supported_contract_versions is outside supported bounds")
    if any(
        isinstance(version, bool) or not isinstance(version, int) or version < 1
        for version in supported_contract_versions
    ):
        raise ValueError("supported contract versions must be positive integers")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= 200
    ):
        raise ValueError("batch_size must be between 1 and 200")
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or not 5 <= lease_seconds <= 300
    ):
        raise ValueError("lease_seconds must be between 5 and 300")


def _validate_delivery_identity(
    delivery_id: UUID, claim_token: UUID, fencing_token: int
) -> None:
    if not isinstance(delivery_id, UUID) or not isinstance(claim_token, UUID):
        raise TypeError("delivery and claim identifiers must be UUIDs")
    if (
        isinstance(fencing_token, bool)
        or not isinstance(fencing_token, int)
        or fencing_token < 1
    ):
        raise ValueError("fencing_token must be positive")


def _validate_provider_message_ref(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("provider_message_ref is outside supported bounds")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("provider_message_ref contains unsupported control characters")


def _malformed_delivery(raw: dict[str, Any]) -> MalformedNotificationDelivery:
    delivery_id = _parse_uuid_value(raw.get("delivery_id"))
    claim_token = _parse_uuid_value(raw.get("claim_token"))
    fencing_value = raw.get("fencing_token")
    fencing_token = (
        fencing_value
        if isinstance(fencing_value, int)
        and not isinstance(fencing_value, bool)
        and fencing_value >= 1
        else None
    )
    return MalformedNotificationDelivery(
        delivery_id=delivery_id,
        claim_token=claim_token,
        fencing_token=fencing_token,
    )


def _parse_uuid_value(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _notification_thread_params(
    *,
    category: NotificationCategory,
    root_task_id: UUID | None,
    subtask_id: UUID | None,
    unread_only: bool,
    cursor: str | None,
    limit: int,
) -> dict[str, str]:
    if category not in {"action", "progress", "archived"}:
        raise ValueError("category is invalid")
    params = _notification_pagination_params(cursor=cursor, limit=limit)
    params["category"] = category
    params["unread_only"] = "true" if unread_only else "false"
    if root_task_id is not None:
        params["root_task_id"] = str(root_task_id)
    if subtask_id is not None:
        params["subtask_id"] = str(subtask_id)
    # Keep the query order deterministic for tests and request tracing.
    return {
        key: params[key]
        for key in (
            "category",
            "root_task_id",
            "subtask_id",
            "unread_only",
            "cursor",
            "limit",
        )
        if key in params
    }


def _notification_pagination_params(
    *, cursor: str | None, limit: int
) -> dict[str, str]:
    _validate_opaque_cursor(cursor)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    params: dict[str, str] = {"limit": str(limit)}
    if cursor is not None:
        params["cursor"] = cursor
    return params


def _validate_opaque_cursor(cursor: str | None) -> None:
    if cursor is None:
        return
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise ValueError("cursor must be between 1 and 512 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in cursor):
        raise ValueError("cursor contains unsupported control characters")


def _require_positive_version(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be positive")


def _validate_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise TypeError("reason is required")
    normalized = reason.strip()
    if not normalized or len(normalized) > 256:
        raise ValueError("reason length is outside the supported bounds")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("reason contains unsupported control characters")
    if _SECRET_MARKER_PATTERN.search(normalized) or "sk-" in normalized.lower():
        raise ValueError("reason contains a forbidden secret-like marker")
    return normalized


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


@dataclass(frozen=True, slots=True)
class _SSEEvent:
    event: str
    event_id: str | None
    data: str


class _ProjectionSSEParseError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _ProjectionSSEParser:
    def __init__(self, *, max_event_bytes: int) -> None:
        self._max_event_bytes = max_event_bytes
        self._buffer = bytearray()
        self._skip_lf = False
        self._event_id: str | None = None
        self._event_name: str | None = None
        self._data_lines: list[str] = []
        self._event_bytes = 0

    @property
    def has_partial_event(self) -> bool:
        return bool(
            self._buffer
            or self._event_id is not None
            or self._event_name is not None
            or self._data_lines
        )

    def feed(self, chunk: bytes) -> tuple[_SSEEvent, ...]:
        self._buffer.extend(chunk)
        events: list[_SSEEvent] = []
        while True:
            if self._skip_lf:
                if not self._buffer:
                    self._check_size()
                    break
                if self._buffer and self._buffer[0] == 0x0A:
                    del self._buffer[0]
                self._skip_lf = False
            newline_index = _find_line_ending(self._buffer)
            if newline_index is None:
                self._check_size()
                break
            ending = self._buffer[newline_index]
            raw_line = bytes(self._buffer[:newline_index])
            del self._buffer[: newline_index + 1]
            if ending == 0x0D:
                self._skip_lf = True
            self._event_bytes += len(raw_line) + 1
            self._check_size()
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                raise _ProjectionSSEParseError("invalid_utf8") from None
            if not line:
                event = self._finish_event()
                if event is not None:
                    events.append(event)
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "id":
                self._event_id = value
            elif field == "event":
                self._event_name = value
            elif field == "data":
                self._data_lines.append(value)
        return tuple(events)

    def _finish_event(self) -> _SSEEvent | None:
        if self._event_id is None and self._event_name is None and not self._data_lines:
            self._event_bytes = 0
            return None
        event = _SSEEvent(
            event=self._event_name or "message",
            event_id=self._event_id,
            data="\n".join(self._data_lines),
        )
        self._event_id = None
        self._event_name = None
        self._data_lines = []
        self._event_bytes = 0
        return event

    def _check_size(self) -> None:
        if self._event_bytes + len(self._buffer) > self._max_event_bytes:
            raise _ProjectionSSEParseError("event_too_large")


def _find_line_ending(buffer: bytearray) -> int | None:
    positions = [
        position
        for position in (buffer.find(b"\n"), buffer.find(b"\r"))
        if position >= 0
    ]
    return min(positions) if positions else None


def _parse_projection_event(
    event: _SSEEvent,
    *,
    workspace_id: UUID,
    current_cursor: int,
) -> ProjectionChange | None:
    if event.event == "resync_required":
        reason = _resync_reason(event.data)
        raise ProjectionResyncRequired(reason, current_cursor)
    if event.event != "projection.changed":
        raise ProjectionResyncRequired("invalid_event", current_cursor)
    if not event.data:
        raise ProjectionResyncRequired("invalid_payload", current_cursor)
    try:
        raw = json.loads(event.data)
    except (TypeError, ValueError):
        raise ProjectionResyncRequired("invalid_payload", current_cursor) from None
    if not isinstance(raw, dict):
        raise ProjectionResyncRequired("invalid_payload", current_cursor)
    if raw.get("schema_version") != 1:
        raise ProjectionResyncRequired("unsupported_schema", current_cursor)
    if raw.get("workspace_id") != str(workspace_id):
        raise ProjectionResyncRequired("workspace_mismatch", current_cursor)
    try:
        change = ProjectionChange.model_validate(raw)
    except ValidationError:
        raise ProjectionResyncRequired("invalid_payload", current_cursor) from None
    if event.event_id is not None:
        try:
            event_cursor = int(event.event_id)
        except ValueError:
            raise ProjectionResyncRequired("invalid_cursor", current_cursor) from None
        if event_cursor < 0 or event_cursor != change.cursor:
            raise ProjectionResyncRequired("invalid_cursor", current_cursor)
    return change


def _resync_reason(data: str) -> str:
    try:
        raw = json.loads(data)
    except (TypeError, ValueError):
        return "invalid_payload"
    reason = raw.get("reason") if isinstance(raw, dict) else None
    return reason if isinstance(reason, str) and reason else "invalid_payload"
