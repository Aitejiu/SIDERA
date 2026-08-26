from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn, cast, overload
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ValidationError

from .config import TaskBridgeConfig
from .contracts import JsonValue, ToolOutcome
from .knowledge_contracts import (
    KnowledgeCommandExecution,
    KnowledgeCommandStatus,
    KnowledgePageDetail,
    KnowledgeRevisionDetail,
    KnowledgeSearchResponse,
)

_MAX_RESPONSE_BYTES = 256 * 1024
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CONFLICT_CODES = {
    "candidate_stale",
    "expected_version_conflict",
    "hash_mismatch",
    "idempotency_conflict",
    "command_conflict",
    "stale_revision",
}
_NEEDS_USER_CODES = {
    "content_limit_exceeded",
    "invalid_request",
    "review_required",
    "source_invalid",
    "validation_error",
}
_UNAVAILABLE_CODES = {
    "service_unavailable",
    "temporarily_unavailable",
}


class KnowledgeClientError(RuntimeError):
    """A safe Knowledge Service failure with a stable tool outcome."""

    def __init__(
        self,
        code: str,
        *,
        outcome: ToolOutcome,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.outcome = outcome
        self.status_code = status_code


class KnowledgeCommandResultUnknown(KnowledgeClientError):
    """A write may have reached Knowledge Service without a terminal response."""

    def __init__(self, command_id: UUID) -> None:
        super().__init__("result_unknown", outcome="result_unknown")
        self.command_id = command_id


class KnowledgeClient:
    """Call only the workspace-scoped public Knowledge Service contract."""

    def __init__(
        self,
        config: TaskBridgeConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._token = (
            config.knowledge_main_agent_token.get_secret_value()
            if config.knowledge_main_agent_token is not None
            else None
        )
        self._client = httpx.AsyncClient(
            base_url=str(config.knowledge_base_url).rstrip("/"),
            trust_env=False,
            timeout=httpx.Timeout(config.knowledge_timeout_seconds),
            transport=transport,
        )

    async def search_knowledge(
        self,
        query: str,
        *,
        tags: Sequence[str] = (),
        source_types: Sequence[str] = (),
        status: str | None = "active",
        limit: int = 20,
        correlation_id: UUID | None = None,
    ) -> KnowledgeSearchResponse:
        self._validate_limit(limit)
        params: list[tuple[str, str]] = [("q", query)]
        params.extend(("tags", value) for value in tags)
        params.extend(("source_types", value) for value in source_types)
        if status is not None:
            params.append(("status", status))
        params.append(("limit", str(limit)))
        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/knowledge/search",
            params=httpx.QueryParams(tuple(params)),
            correlation_id=correlation_id,
        )
        return self._parse_model(response, KnowledgeSearchResponse)

    @overload
    async def get_page(
        self, page_id: UUID, *, revision_id: None = None, correlation_id: UUID | None = None
    ) -> KnowledgePageDetail: ...

    @overload
    async def get_page(
        self,
        page_id: UUID,
        *,
        revision_id: UUID,
        correlation_id: UUID | None = None,
    ) -> KnowledgeRevisionDetail: ...

    async def get_page(
        self,
        page_id: UUID,
        *,
        revision_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> KnowledgePageDetail | KnowledgeRevisionDetail:
        if revision_id is None:
            path = (
                f"/api/v1/workspaces/{self._config.workspace_id}/knowledge/pages/{page_id}"
            )
            model: type[BaseModel] = KnowledgePageDetail
        else:
            path = (
                f"/api/v1/workspaces/{self._config.workspace_id}/knowledge/pages/{page_id}"
                f"/revisions/{revision_id}"
            )
            model = KnowledgeRevisionDetail
        response = await self._request(
            "GET", path, correlation_id=correlation_id
        )
        return cast(KnowledgePageDetail | KnowledgeRevisionDetail, self._parse_model(response, model))

    async def submit_candidate(
        self,
        command_id: UUID,
        payload: Mapping[str, JsonValue],
        *,
        idempotency_key: str,
        correlation_id: UUID | None = None,
    ) -> KnowledgeCommandExecution:
        candidate_id = self._candidate_id(payload)
        request_correlation_id = correlation_id or uuid4()
        body: dict[str, JsonValue] = {
            "command_id": str(command_id),
            "workspace_id": str(self._config.workspace_id),
            "command_type": "SubmitKnowledgeCandidate",
            "target_type": "knowledge_candidate",
            "target_id": str(candidate_id),
            "actor": {"type": "main_agent", "id": self._config.actor_id},
            "expected_version": None,
            "idempotency_key": idempotency_key,
            "correlation_id": str(request_correlation_id),
            "payload": dict(payload),
        }
        response = await self._request(
            "POST",
            "/api/v1/commands",
            json_body=body,
            command_id=command_id,
            correlation_id=request_correlation_id,
        )
        execution = cast(
            KnowledgeCommandExecution,
            self._parse_model(response, KnowledgeCommandExecution),
        )
        if execution.command_id != command_id:
            raise KnowledgeClientError(
                "invalid_knowledge_response",
                outcome="failed",
            )
        return execution

    async def get_command_status(
        self,
        command_id: UUID,
        *,
        correlation_id: UUID | None = None,
    ) -> KnowledgeCommandStatus:
        response = await self._request(
            "GET",
            f"/api/v1/workspaces/{self._config.workspace_id}/commands/{command_id}",
            correlation_id=correlation_id,
        )
        status = cast(KnowledgeCommandStatus, self._parse_model(response, KnowledgeCommandStatus))
        if status.command_id != command_id:
            raise KnowledgeClientError(
                "invalid_knowledge_response",
                outcome="failed",
            )
        if status.workspace_id != self._config.workspace_id:
            raise KnowledgeClientError(
                "workspace_mismatch",
                outcome="failed",
            )
        return status

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit < 1 or limit > 20:
            raise KnowledgeClientError("invalid_request", outcome="needs_user")

    @staticmethod
    def _candidate_id(payload: Mapping[str, JsonValue]) -> UUID:
        value = payload.get("candidate_id")
        if not isinstance(value, str):
            raise KnowledgeClientError("invalid_request", outcome="needs_user")
        try:
            return UUID(value)
        except ValueError:
            raise KnowledgeClientError("invalid_request", outcome="needs_user") from None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        params: httpx.QueryParams | None = None,
        command_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> httpx.Response:
        if self._token is None:
            raise KnowledgeClientError("knowledge_not_configured", outcome="unavailable")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-Correlation-ID": str(correlation_id or uuid4()),
        }
        try:
            response = await self._client.request(
                method,
                path,
                headers=headers,
                json=json_body,
                params=params,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            if command_id is not None:
                raise KnowledgeCommandResultUnknown(command_id) from None
            raise KnowledgeClientError(
                "temporarily_unavailable", outcome="unavailable"
            ) from None
        if response.status_code >= 400:
            self._raise_http_error(response)
        return response

    @staticmethod
    def _parse_model(response: httpx.Response, model: type[BaseModel]) -> Any:
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise KnowledgeClientError("content_limit_exceeded", outcome="failed")
        try:
            body = response.json()
            return model.model_validate(body)
        except (ValueError, ValidationError):
            raise KnowledgeClientError(
                "invalid_knowledge_response", outcome="failed"
            ) from None

    @classmethod
    def _raise_http_error(cls, response: httpx.Response) -> NoReturn:
        code = cls._safe_error_code(response)
        if code is None:
            code = cls._fallback_error_code(response.status_code)
        raise KnowledgeClientError(
            code,
            outcome=cls._outcome_for(code, response.status_code),
            status_code=response.status_code,
        )

    @staticmethod
    def _safe_error_code(response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        if not isinstance(error, dict):
            return None
        code = error.get("code")
        if not isinstance(code, str) or _SAFE_ERROR_CODE.fullmatch(code) is None:
            return None
        return code

    @staticmethod
    def _fallback_error_code(status_code: int) -> str:
        if status_code in {408, 425, 429} or status_code >= 500:
            return "temporarily_unavailable"
        if status_code == 403:
            return "permission_denied"
        if status_code == 404:
            return "not_found"
        if status_code == 409:
            return "command_conflict"
        if status_code == 422:
            return "validation_error"
        return "knowledge_request_failed"

    @staticmethod
    def _outcome_for(code: str, status_code: int) -> ToolOutcome:
        if code in _CONFLICT_CODES or status_code == 409:
            return "conflict"
        if code in _NEEDS_USER_CODES or status_code == 422:
            return "needs_user"
        if code in _UNAVAILABLE_CODES or status_code in {408, 425, 429} or status_code >= 500:
            return "unavailable"
        return "failed"
