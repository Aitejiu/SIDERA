"""Bounded HTTP client for public Control Plane acceptance paths."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Self
from uuid import UUID

import httpx

from .contracts import ClientOutcome


class AcceptanceTimeout(RuntimeError):
    """A bounded query did not reach its predicate before its deadline."""

    def __init__(self, code: str = "deadline_exceeded") -> None:
        super().__init__(code)
        self.code = code


class BasicAcceptanceClient:
    def __init__(
        self,
        base_url: str,
        *,
        workspace_id: UUID | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 5.0,
        max_retries: int = 2,
    ) -> None:
        self._workspace_id = workspace_id
        self._max_retries = max(0, max_retries)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str) -> ClientOutcome:
        return self._request_json("GET", path)

    def post_json(self, path: str, payload: Mapping[str, Any]) -> ClientOutcome:
        return self._request_json("POST", path, payload)

    def command(self, envelope: Mapping[str, Any]) -> ClientOutcome:
        command_id = _uuid_or_none(envelope.get("command_id"))
        response = self.post_json("/api/v1/commands", envelope)
        if response.status != "incomplete" or command_id is None:
            return response
        return self.reconcile_command(command_id)

    def reconcile_command(self, command_id: UUID) -> ClientOutcome:
        if self._workspace_id is None:
            return ClientOutcome(
                status="incomplete",
                command_id=command_id,
                diagnostic_code="workspace_id_required_for_reconcile",
            )
        path = f"/api/v1/workspaces/{self._workspace_id}/commands/{command_id}"
        response = self._request_json("GET", path)
        if response.status != "accepted" or not response.body:
            return response.model_copy(
                update={"command_id": command_id, "status": "incomplete"}
            )
        status = response.body.get("status")
        if status == "applied":
            return ClientOutcome(
                status="observed",
                command_id=command_id,
                body=response.body,
                diagnostic_code="command_observed_after_disconnect",
            )
        return ClientOutcome(
            status="incomplete",
            command_id=command_id,
            body=response.body,
            diagnostic_code="command_state_not_terminal",
        )

    def wait_for(
        self,
        path: str,
        predicate: Callable[[Mapping[str, Any]], bool],
        deadline: float,
    ) -> ClientOutcome:
        while time.monotonic() < deadline:
            response = self.get_json(path)
            if response.status == "accepted" and response.body is not None:
                if predicate(response.body):
                    return response
            elif response.status == "incomplete":
                return response
            time.sleep(0.05)
        return ClientOutcome(status="incomplete", diagnostic_code="deadline_exceeded")

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> ClientOutcome:
        retry_limit = self._max_retries if method == "GET" else 0
        for attempt in range(retry_limit + 1):
            try:
                response = self._client.request(method, path, json=payload)
            except httpx.TimeoutException:
                if attempt < self._max_retries:
                    continue
                return ClientOutcome(status="incomplete", diagnostic_code="transport_timeout")
            except httpx.TransportError:
                if attempt < self._max_retries:
                    continue
                return ClientOutcome(status="incomplete", diagnostic_code="transport_unavailable")

            if response.status_code == 409:
                current_version = _current_version(response)
                return ClientOutcome(
                    status="conflict",
                    current_version=current_version,
                    diagnostic_code="version_conflict",
                )
            if response.status_code >= 400:
                return ClientOutcome(
                    status="rejected",
                    diagnostic_code=f"http_{response.status_code}",
                )
            try:
                body = response.json()
            except ValueError:
                return ClientOutcome(status="incomplete", diagnostic_code="invalid_json")
            if not isinstance(body, dict):
                return ClientOutcome(status="incomplete", diagnostic_code="invalid_json_shape")
            command_id = _uuid_or_none(body.get("command_id"))
            return ClientOutcome(
                status="accepted",
                command_id=command_id,
                body=body,
                diagnostic_code="http_ok",
            )
        return ClientOutcome(status="incomplete", diagnostic_code="retry_budget_exhausted")


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _current_version(response: httpx.Response) -> int | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    value = body.get("current_version")
    if isinstance(value, int) and value >= 1:
        return value
    details = body.get("detail")
    if isinstance(details, dict) and isinstance(details.get("current_version"), int):
        return details["current_version"]
    return None
