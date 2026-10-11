import re
from collections.abc import Generator
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr
from sidera_control_plane_client import ControlPlaneClient
from sidera_control_plane_client.contracts import CommandResponse, JsonValue
from sidera_control_plane_client.errors import (
    CommandResultUnknown,
    ControlPlaneRejected,
    TransportUnavailable,
)

_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")


class GrantAuth(httpx.Auth):
    def __init__(self, token: SecretStr) -> None:
        self.token = token

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = "Bearer " + self.token.get_secret_value()
        yield request


class MutationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: Literal["applied", "rejected", "unknown"]
    command_id: UUID
    root_task_id: UUID | None = None
    planning_request_id: UUID | None = None
    status: str
    error_code: str | None = None


def _unknown(command_id: UUID, *, status: str = "unknown") -> MutationResult:
    return MutationResult(
        outcome="unknown",
        command_id=command_id,
        status=status,
        error_code="command_result_unknown",
    )


def _identifier(value: JsonValue) -> UUID | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid receipt reference")
    return UUID(value)


def _applied(command_id: UUID, result: CommandResponse | None) -> MutationResult | None:
    if result is None or result.command_id != command_id:
        return None
    status = result.data.get("status", "applied")
    if not isinstance(status, str) or _CODE.fullmatch(status) is None:
        return None
    try:
        return MutationResult(
            outcome="applied",
            command_id=command_id,
            status=status,
            root_task_id=_identifier(result.data.get("root_task_id")),
            planning_request_id=_identifier(result.data.get("planning_request_id")),
        )
    except ValueError:
        return None


class OriginalCommandTransport:
    def __init__(self, client: ControlPlaneClient) -> None:
        self.client = client

    async def dispatch(
        self, body: dict[str, JsonValue], command_id: UUID
    ) -> MutationResult:
        try:
            execution = await self.client.execute_command_json(body, command_id)
        except ControlPlaneRejected as error:
            if error.code != "invalid_control_plane_response":
                return MutationResult(
                    outcome="rejected",
                    command_id=command_id,
                    status="rejected",
                    error_code=error.code,
                )
        except (CommandResultUnknown, TransportUnavailable):
            pass
        else:
            result = (
                _applied(command_id, execution.result)
                if execution.status == "applied"
                else None
            )
            if result is not None:
                return result
        return await self.reconcile(command_id)

    async def reconcile(self, command_id: UUID) -> MutationResult:
        try:
            saved = await self.client.get_command_status(command_id)
        except (ControlPlaneRejected, TransportUnavailable):
            return _unknown(command_id)
        if saved.command_id != command_id:
            return _unknown(command_id)
        if saved.status == "applied":
            return _applied(command_id, saved.result) or _unknown(command_id)
        if saved.status == "rejected":
            if (
                saved.error_code is not None
                and _CODE.fullmatch(saved.error_code) is not None
            ):
                return MutationResult(
                    outcome="rejected",
                    command_id=command_id,
                    status="rejected",
                    error_code=saved.error_code,
                )
            return _unknown(command_id)
        return _unknown(command_id, status="pending")
