import re
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response

from sidera_control_plane.api.dependencies import (
    get_artifact_content_service,
    get_settings,
)
from sidera_control_plane.api.schemas import (
    ArtifactContentDescriptorResponse,
    DownloadTokenResponse,
)
from sidera_control_plane.application.artifact_content import (
    ALLOWED_ARTIFACT_MEDIA_TYPES,
    INLINE_ARTIFACT_MEDIA_TYPES,
    ArtifactContentDescriptor,
    ArtifactContentService,
    ArtifactRangeNotSatisfiable,
    resolve_byte_range,
)
from sidera_control_plane.config import Settings

router = APIRouter()


@router.get(
    "/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/descriptor",
    response_model=ArtifactContentDescriptorResponse,
)
def get_artifact_descriptor(
    workspace_id: UUID,
    artifact_id: UUID,
    service: Annotated[ArtifactContentService, Depends(get_artifact_content_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: str | None = Query(default=None),
) -> ArtifactContentDescriptorResponse:
    actor_type, actor_id = _resolve_actor(
        service, settings, workspace_id, artifact_id, token
    )
    descriptor = service.describe(workspace_id, artifact_id, actor_type, actor_id)
    return ArtifactContentDescriptorResponse.model_validate(asdict(descriptor))


@router.get("/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/content")
def get_artifact_content(
    workspace_id: UUID,
    artifact_id: UUID,
    service: Annotated[ArtifactContentService, Depends(get_artifact_content_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: str | None = Query(default=None),
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    actor_type, actor_id = _resolve_actor(
        service, settings, workspace_id, artifact_id, token
    )
    descriptor = service.describe(workspace_id, artifact_id, actor_type, actor_id)
    byte_range = _parse_range_header(range_header)
    stream = service.open_stream(
        workspace_id,
        artifact_id,
        actor_type,
        actor_id,
        byte_range,
    )
    content = stream.read()
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(descriptor),
        "X-Content-Type-Options": "nosniff",
    }
    status_code = 200
    if byte_range is not None:
        start, end = resolve_byte_range(descriptor.byte_size, byte_range)
        headers["Content-Range"] = (
            f"bytes {start}-{end}/{descriptor.byte_size}"
        )
        status_code = 206
    return Response(
        content=content,
        status_code=status_code,
        media_type=descriptor.media_type,
        headers=headers,
    )


@router.post(
    "/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/download-token",
    response_model=DownloadTokenResponse,
)
def create_artifact_download_token(
    workspace_id: UUID,
    artifact_id: UUID,
    service: Annotated[ArtifactContentService, Depends(get_artifact_content_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DownloadTokenResponse:
    token = service.create_download_token(
        workspace_id,
        artifact_id,
        settings.local_actor_type,
        settings.local_actor_id,
        settings.artifact_download_token_ttl_seconds,
    )
    return DownloadTokenResponse(token=token)


def _resolve_actor(
    service: ArtifactContentService,
    settings: Settings,
    workspace_id: UUID,
    artifact_id: UUID,
    token: str | None,
) -> tuple[str, str]:
    if token is None:
        return settings.local_actor_type, settings.local_actor_id
    return service.verify_download_token(workspace_id, artifact_id, token)


def _parse_range_header(value: str | None) -> tuple[int | None, int | None] | None:
    if value is None:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        raise ArtifactRangeNotSatisfiable("artifact byte range is invalid")
    start_text, end_text = match.groups()
    return (
        int(start_text) if start_text else None,
        int(end_text) if end_text else None,
    )


def _content_disposition(descriptor: ArtifactContentDescriptor) -> str:
    disposition = (
        "inline"
        if descriptor.media_type in INLINE_ARTIFACT_MEDIA_TYPES
        and descriptor.media_type in ALLOWED_ARTIFACT_MEDIA_TYPES
        else "attachment"
    )
    return f'{disposition}; filename="{descriptor.download_name}"'
