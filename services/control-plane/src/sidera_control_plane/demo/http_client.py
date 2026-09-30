from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .contracts import DemoManifest


class DemoClientError(RuntimeError):
    """A local demo HTTP request did not satisfy its contract."""


class DemoClientNotFound(DemoClientError):
    """A local demo resource does not exist."""


class DemoCommandRejected(DemoClientError):
    """The control plane explicitly rejected a command."""


class DemoCommandOutcomeUnknown(DemoClientError):
    """A command may have applied, but no explicit acknowledgement is available."""


class DemoClient:
    def __init__(
        self,
        manifest: DemoManifest,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds_must_be_positive")
        self._manifest = manifest
        self._client = httpx.Client(
            timeout=timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def get(self, path: str) -> object:
        url = self._local_url(path)
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            raise DemoClientError("local_get_failed") from exc
        return self._json_response(response)

    def command(self, envelope: dict[str, object]) -> dict[str, object]:
        command_id = _command_id(envelope)
        try:
            response = self._client.post(
                self._local_url("/api/v1/commands"), json=envelope
            )
        except httpx.HTTPError:
            return self._resolve_uncertain_command(command_id)

        if 200 <= response.status_code < 300:
            result = _object_response(response)
            if result.get("command_id") != command_id:
                raise DemoCommandOutcomeUnknown("command_ack_identity_mismatch")
            return result
        if 400 <= response.status_code < 500:
            raise DemoCommandRejected(f"command_rejected:{response.status_code}")
        return self._resolve_uncertain_command(command_id)

    def close(self) -> None:
        self._client.close()

    def _resolve_uncertain_command(self, command_id: str) -> dict[str, object]:
        path = (
            f"/api/v1/workspaces/{self._manifest.workspace_id}/commands/{command_id}"
        )
        try:
            status = self.get(path)
        except DemoClientError as exc:
            raise DemoCommandOutcomeUnknown("command_status_unavailable") from exc
        if not isinstance(status, dict) or status.get("command_id") != command_id:
            raise DemoCommandOutcomeUnknown("command_status_identity_mismatch")
        if status.get("status") == "applied":
            result = status.get("result")
            if not isinstance(result, dict) or result.get("command_id") != command_id:
                raise DemoCommandOutcomeUnknown("command_result_unavailable")
            return dict(result)
        if status.get("status") == "rejected":
            raise DemoCommandRejected("command_rejected")
        raise DemoCommandOutcomeUnknown("command_not_acknowledged")

    def _local_url(self, path: str) -> str:
        parsed = urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
        ):
            raise DemoClientError("invalid_local_path")
        return f"{self._manifest.endpoints.control_plane}{path}"

    @staticmethod
    def _json_response(response: httpx.Response) -> object:
        if response.status_code == 404:
            raise DemoClientNotFound("local_resource_not_found")
        if not 200 <= response.status_code < 300:
            raise DemoClientError(f"local_get_failed:{response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise DemoClientError("local_response_not_json") from exc


def _command_id(envelope: dict[str, object]) -> str:
    try:
        command_id = UUID(str(envelope["command_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("command_id_required") from exc
    return str(command_id)


def _object_response(response: httpx.Response) -> dict[str, object]:
    try:
        value: Any = response.json()
    except ValueError as exc:
        raise DemoCommandOutcomeUnknown("command_ack_not_json") from exc
    if not isinstance(value, dict):
        raise DemoCommandOutcomeUnknown("command_ack_invalid")
    return dict(value)
