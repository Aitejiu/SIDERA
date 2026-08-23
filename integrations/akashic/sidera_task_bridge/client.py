from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, NoReturn, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import TaskBridgeConfig
from .contracts import JsonValue


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

        body = envelope.model_dump(mode="json")
        body["workspace_id"] = str(self._config.workspace_id)
        body["actor"] = {
            "type": self._config.actor_type,
            "id": self._config.actor_id,
        }
        response = await self._request(
            "POST",
            "/api/v1/commands",
            json=body,
            command_id=envelope.command_id,
        )
        if response.status_code in {200, 201}:
            result = self._parse_model(response, CommandResponse)
            return CommandExecution(
                status="applied",
                command_id=envelope.command_id,
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
