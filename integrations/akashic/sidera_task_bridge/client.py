from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, Protocol, cast
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import TaskBridgeConfig
from .contracts import JsonValue
from .state import (
    BridgeStateStore,
    OutboundCommand,
    StateConflict,
    same_logical_request,
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


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Safe local result of a persisted command dispatch."""

    status: Literal["applied", "rejected", "result_unknown", "unavailable"]
    command_id: UUID
    error_code: str | None = None
    execution: CommandExecution | CommandStatus | None = None


class CommandClient(Protocol):
    def build_request_json(self, envelope: CommandEnvelope) -> dict[str, JsonValue]: ...

    def build_intent_request_json(self, intent: CommandIntent) -> dict[str, JsonValue]: ...

    async def execute_command_json(
        self, request_json: dict[str, JsonValue], command_id: UUID
    ) -> CommandExecution: ...

    async def get_command_status(self, command_id: UUID) -> CommandStatus: ...


class TransportUnavailable(RuntimeError):
    """The control plane could not be read or reached."""


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
        body["actor"] = {
            "type": self._config.actor_type,
            "id": self._config.actor_id,
        }
        return body

    def build_intent_request_json(
        self, intent: CommandIntent
    ) -> dict[str, JsonValue]:
        body = cast(dict[str, JsonValue], intent.model_dump(mode="json"))
        body["workspace_id"] = str(self._config.workspace_id)
        body["actor"] = {
            "type": self._config.actor_type,
            "id": self._config.actor_id,
        }
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

    async def get_planning_detail(self, request_id: UUID) -> dict[str, JsonValue]:
        """Read one PlanningRequest detail projection."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/planning-requests/{request_id}"
        )

    async def list_clarifications(
        self, request_id: UUID
    ) -> tuple[dict[str, JsonValue], ...]:
        """Read pending and answered clarification projections."""

        body = await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/planning-requests/{request_id}/clarifications"
        )
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ControlPlaneRejected("invalid_control_plane_response", {})
        return tuple(cast(dict[str, JsonValue], item) for item in items)

    async def get_proposal(
        self, request_id: UUID, revision: int
    ) -> dict[str, JsonValue]:
        """Read one exact proposal revision."""

        return await self._get_object(
            f"/api/v1/workspaces/{self._config.workspace_id}/planning-requests/{request_id}/proposals/{revision}"
        )

    async def get_materialization(
        self, request_id: UUID
    ) -> dict[str, JsonValue]:
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

    async def close(self) -> None:
        """Release the HTTP transport owned by this client."""

        await self._client.aclose()

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
    ) -> None:
        self._client = client
        self._state = state
        self._uuid_factory = uuid_factory
        self._clock = clock or (lambda: datetime.now(UTC))

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
            if same_logical_request(existing.request_json, request_json):
                return existing, True
            return existing, False
        return None, False

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
            await self._state.settle(
                stored.command_id,
                outcome="applied",
                error_code=None,
            )
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
            await self._state.settle(
                status.command_id,
                outcome="applied",
                error_code=None,
            )
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
