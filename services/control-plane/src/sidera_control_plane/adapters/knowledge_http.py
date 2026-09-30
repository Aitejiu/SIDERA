from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from sidera_control_plane.application.knowledge_errors import (
    KnowledgeAdapterError as KnowledgeAdapterError,  # noqa: PLC0414
)
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactRevisionSection,
    ExactSectionBody,
    ExactSectionRef,
    KnowledgeCandidateCatalogPort,
    KnowledgeCatalogPort,
    KnowledgeCommandStatus,
    KnowledgeSourceMetadata,
    SearchHit,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SearchHitResponse(_StrictModel):
    page_id: UUID
    revision_id: UUID
    section_key: str
    revision_number: int = Field(ge=1)
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    section_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str
    summary: str
    tags: list[str]
    source_ids: list[UUID]
    source_types: list[str]
    source_availability: list[str]
    trust: str
    section_byte_count: int = Field(ge=0)
    rank: float
    rank_explanation: str


class _SearchResponse(_StrictModel):
    items: list[_SearchHitResponse]
    next_cursor: str | None


class _SourceResponse(_StrictModel):
    source_id: UUID
    workspace_id: UUID
    source_type: str
    label: str
    uri: str | None
    workspace_relative_path: str | None
    git_revision: str | None
    digest: str | None
    metadata: dict[str, object]
    availability: str
    safe_reason_code: str | None
    last_checked_at: datetime | None
    version: int = Field(ge=1)


class _RevisionSectionResponse(_StrictModel):
    section_key: str
    heading: str
    summary: str
    body_markdown: str
    ordinal: int = Field(ge=0)
    section_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_byte_count: int = Field(ge=0)


class _RelationResponse(_StrictModel):
    relation_key: str
    relation_kind: str
    target_page_id: UUID
    target_revision_id: UUID | None


class _RevisionResponse(_StrictModel):
    revision_id: UUID
    page_id: UUID
    workspace_id: UUID
    revision_number: int = Field(ge=1)
    title: str
    summary: str
    tags: list[str]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_reason: str
    created_by: str
    created_at: datetime
    accepted_candidate_id: UUID | None
    sections: list[_RevisionSectionResponse]
    relations: list[_RelationResponse]
    source_refs: list[UUID]
    sources: list[_SourceResponse]


class _MaterializedSectionResponse(_StrictModel):
    ordinal: int = Field(ge=0)
    page_id: UUID
    revision_id: UUID
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    section_key: str
    section_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_markdown: str
    sources: list[_SourceResponse]
    trust: str
    section_byte_count: int = Field(ge=0)


class _MaterializationResponse(_StrictModel):
    sections: list[_MaterializedSectionResponse]
    total_byte_count: int = Field(ge=0)


class _CommandResponse(_StrictModel):
    command_id: UUID
    workspace_id: UUID | None = None
    status: str
    result: dict[str, object] | None = None
    error_code: str | None = None


class _ErrorBody(_StrictModel):
    code: str
    message: str
    details: dict[str, object]


class _ErrorResponse(_StrictModel):
    error: _ErrorBody


class KnowledgeHttpAdapter(KnowledgeCatalogPort, KnowledgeCandidateCatalogPort):
    """HTTP implementation of the control-plane Knowledge catalog port."""

    def __init__(
        self,
        *,
        base_url: str,
        control_plane_token: SecretStr | None = None,
        metadata_timeout_seconds: float = 5.0,
        materialization_timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        if not normalized_url:
            raise ValueError("Knowledge base URL is required")
        if metadata_timeout_seconds <= 0 or materialization_timeout_seconds <= 0:
            raise ValueError("Knowledge HTTP timeouts must be positive")
        self._base_url = normalized_url
        self._control_plane_token = control_plane_token
        self._metadata_timeout_seconds = metadata_timeout_seconds
        self._materialization_timeout_seconds = materialization_timeout_seconds
        self._client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search(
        self,
        workspace_id: UUID,
        query: str,
        *,
        tags: Sequence[str] = (),
        source_types: Sequence[str] = (),
        status: str | None = "active",
        limit: int = 20,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[SearchHit, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("Knowledge search limit must be between 1 and 50")
        params: list[tuple[str, str | int]] = [("q", query), ("limit", limit)]
        if status is not None:
            params.append(("status", status))
        params.extend(("tags", value) for value in tags)
        params.extend(("source_types", value) for value in source_types)
        response = self._request(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/knowledge/search",
            params=params,
            timeout=self._metadata_timeout_seconds,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        document = self._parse(response, _SearchResponse)
        return tuple(
            SearchHit(
                page_id=item.page_id,
                revision_id=item.revision_id,
                section_key=item.section_key,
                revision_number=item.revision_number,
                revision_hash=item.revision_hash,
                section_hash=item.section_hash,
                title=item.title,
                summary=item.summary,
                tags=tuple(item.tags),
                source_ids=tuple(item.source_ids),
                source_types=tuple(item.source_types),
                source_availability=tuple(item.source_availability),
                trust=item.trust,
                section_byte_count=item.section_byte_count,
                rank=item.rank,
                rank_explanation=item.rank_explanation,
            )
            for item in document.items
        )

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        response = self._request(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/revisions/{revision_id}",
            timeout=self._metadata_timeout_seconds,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        document = self._parse(response, _RevisionResponse)
        if (
            document.workspace_id != workspace_id
            or document.page_id != page_id
            or document.revision_id != revision_id
        ):
            raise KnowledgeAdapterError(
                "workspace_mismatch",
                "Knowledge revision scope does not match the request",
                temporary=False,
            )
        return ExactRevisionManifest(
            workspace_id=document.workspace_id,
            page_id=document.page_id,
            revision_id=document.revision_id,
            revision_number=document.revision_number,
            revision_hash=document.content_hash,
            title=document.title,
            summary=document.summary,
            tags=tuple(document.tags),
            source_ids=tuple(document.source_refs),
            sections=tuple(
                ExactRevisionSection(
                    section_key=item.section_key,
                    heading=item.heading,
                    summary=item.summary,
                    ordinal=item.ordinal,
                    section_hash=item.section_hash,
                    content_byte_count=item.content_byte_count,
                )
                for item in document.sections
            ),
        )

    def materialize(
        self,
        workspace_id: UUID,
        sections: Sequence[ExactSectionRef],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        payload = {
            "sections": [
                {
                    "ordinal": item.ordinal,
                    "page_id": str(item.page_id),
                    "revision_id": str(item.revision_id),
                    "revision_hash": item.revision_hash,
                    "section_key": item.section_key,
                    "section_hash": item.section_hash,
                    "source_ids": [str(source_id) for source_id in item.source_ids],
                }
                for item in sections
            ]
        }
        response = self._request(
            "POST",
            f"/api/v1/internal/workspaces/{workspace_id}/materialize",
            json=payload,
            timeout=self._materialization_timeout_seconds,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        document = self._parse(response, _MaterializationResponse)
        expected = {
            (item.page_id, item.revision_id, item.section_key): item for item in sections
        }
        actual_keys = {
            (item.page_id, item.revision_id, item.section_key)
            for item in document.sections
        }
        if actual_keys != set(expected) or len(actual_keys) != len(document.sections):
            raise KnowledgeAdapterError(
                "permanent_mismatch",
                "Knowledge materialization references do not match the request",
                temporary=False,
            )
        result: list[ExactSectionBody] = []
        for item in document.sections:
            expected_ref = expected[(item.page_id, item.revision_id, item.section_key)]
            if (
                item.revision_hash != expected_ref.revision_hash
                or item.section_hash != expected_ref.section_hash
                or item.ordinal != expected_ref.ordinal
                or tuple(source.source_id for source in item.sources)
                != expected_ref.source_ids
            ):
                raise KnowledgeAdapterError(
                    "permanent_mismatch",
                    "Knowledge materialization hashes do not match the request",
                    temporary=False,
                )
            if any(source.workspace_id != workspace_id for source in item.sources):
                raise KnowledgeAdapterError(
                    "workspace_mismatch",
                    "Knowledge materialization source scope does not match the request",
                    temporary=False,
                )
            result.append(
                ExactSectionBody(
                    ordinal=item.ordinal,
                    page_id=item.page_id,
                    revision_id=item.revision_id,
                    revision_hash=item.revision_hash,
                    section_key=item.section_key,
                    section_hash=item.section_hash,
                    body_markdown=item.body_markdown,
                    sources=tuple(_source(item) for item in item.sources),
                    trust=item.trust,
                    section_byte_count=item.section_byte_count,
                )
            )
        return tuple(sorted(result, key=lambda item: item.ordinal))

    def submit_candidate(
        self,
        workspace_id: UUID,
        command_id: UUID,
        idempotency_key: str,
        candidate_payload: Mapping[str, object],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> KnowledgeCommandStatus:
        response = self._request(
            "POST",
            "/api/v1/commands",
            json={
                "command_id": str(command_id),
                "workspace_id": str(workspace_id),
                "target_type": "knowledge_candidate",
                "target_id": candidate_payload.get("candidate_id"),
                "actor": {"type": "control_plane", "id": "sidera-candidate-worker"},
                "expected_version": None,
                "idempotency_key": idempotency_key,
                "correlation_id": str(correlation_id),
                "command_type": "SubmitKnowledgeCandidate",
                "payload": dict(candidate_payload),
            },
            timeout=self._metadata_timeout_seconds,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        document = self._parse(response, _CommandResponse)
        return self._command_status(document, workspace_id)

    def get_command_status(
        self,
        workspace_id: UUID,
        command_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> KnowledgeCommandStatus:
        response = self._request(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/commands/{command_id}",
            timeout=self._metadata_timeout_seconds,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        document = self._parse(response, _CommandResponse)
        return self._command_status(document, workspace_id)

    @staticmethod
    def _command_status(
        document: _CommandResponse, workspace_id: UUID
    ) -> KnowledgeCommandStatus:
        if document.workspace_id is not None and document.workspace_id != workspace_id:
            raise KnowledgeAdapterError(
                "workspace_mismatch",
                "Knowledge command scope does not match the request",
                temporary=False,
            )
        if document.status not in {"pending", "applied", "rejected"}:
            raise KnowledgeAdapterError(
                "protocol_error",
                "Knowledge service returned an invalid command status",
                temporary=False,
            )
        return KnowledgeCommandStatus(
            command_id=document.command_id,
            workspace_id=workspace_id,
            status=cast(
                Literal["pending", "applied", "rejected"], document.status
            ),
            result=document.result,
            error_code=document.error_code,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        correlation_id: UUID,
        caller_request_id: UUID,
        params: Sequence[tuple[str, str | int]] = (),
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/json",
            "X-Correlation-ID": str(correlation_id),
            "X-Caller-Request-ID": str(caller_request_id),
        }
        if self._control_plane_token is not None:
            headers["Authorization"] = (
                f"Bearer {self._control_plane_token.get_secret_value()}"
            )
        try:
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                params=list(params),
                json=json,
                headers=headers,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise KnowledgeAdapterError(
                "temporary_unavailable",
                "Knowledge service request timed out",
                temporary=True,
            ) from exc
        except httpx.RequestError as exc:
            raise KnowledgeAdapterError(
                "temporary_unavailable",
                "Knowledge service is temporarily unavailable",
                temporary=True,
            ) from exc
        if response.status_code >= 400:
            raise self._map_http_error(response)
        return response

    @staticmethod
    def _parse(response: httpx.Response, model: type[BaseModel]) -> Any:
        try:
            document = response.json()
            return model.model_validate(document)
        except (ValueError, ValidationError) as exc:
            raise KnowledgeAdapterError(
                "protocol_error",
                "Knowledge service returned an invalid response",
                temporary=False,
            ) from exc

    @staticmethod
    def _map_http_error(response: httpx.Response) -> KnowledgeAdapterError:
        status = response.status_code
        if status == 404:
            return KnowledgeAdapterError(
                "reference_not_found",
                "Knowledge reference was not found",
                temporary=False,
                status_code=status,
            )
        if status == 409:
            return KnowledgeAdapterError(
                "permanent_mismatch",
                "Knowledge reference conflicts with current state",
                temporary=False,
                status_code=status,
            )
        if status in {401, 403}:
            return KnowledgeAdapterError(
                "permission_denied",
                "Knowledge service permission was denied",
                temporary=False,
                status_code=status,
            )
        if status == 429 or status >= 500:
            return KnowledgeAdapterError(
                "temporary_unavailable",
                "Knowledge service is temporarily unavailable",
                temporary=True,
                status_code=status,
            )
        return KnowledgeAdapterError(
            "protocol_error",
            "Knowledge service rejected the request",
            temporary=False,
            status_code=status,
        )


def _source(source: _SourceResponse) -> KnowledgeSourceMetadata:
    return KnowledgeSourceMetadata(
        source_id=source.source_id,
        workspace_id=source.workspace_id,
        source_type=source.source_type,
        label=source.label,
        uri=source.uri,
        workspace_relative_path=source.workspace_relative_path,
        git_revision=source.git_revision,
        digest=source.digest,
        metadata=dict(source.metadata),
        availability=source.availability,
        safe_reason_code=source.safe_reason_code,
        last_checked_at=source.last_checked_at,
        version=source.version,
    )
