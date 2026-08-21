import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import BinaryIO, Protocol
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select

from sidera_control_plane.application.errors import (
    ApplicationError,
    Conflict,
    NotFound,
    ServiceUnavailable,
)
from sidera_control_plane.persistence.artifact_content_store import (
    ArtifactContentIntegrityError,
    ArtifactContentUnavailable,
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import ArtifactModel

ALLOWED_ARTIFACT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/x-diff",
        "application/json",
        "image/png",
        "image/jpeg",
        "application/octet-stream",
    }
)
INLINE_ARTIFACT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/x-diff",
        "application/json",
        "image/png",
        "image/jpeg",
    }
)


class ArtifactContentUnavailableError(NotFound):
    code = "artifact_content_unavailable"


class ArtifactIntegrityFailed(Conflict):
    code = "artifact_integrity_failed"


class ArtifactRangeNotSatisfiable(ApplicationError):
    code = "artifact_range_not_satisfiable"


class ArtifactMediaTypeUnsupported(Conflict):
    code = "artifact_media_type_unsupported"


class DownloadTokensDisabled(ServiceUnavailable):
    code = "artifact_download_tokens_disabled"


@dataclass(frozen=True, slots=True)
class ArtifactContentDescriptor:
    artifact_id: UUID
    workspace_id: UUID
    media_type: str
    byte_size: int
    content_hash: str
    download_name: str
    content_available: bool
    unavailable_reason: str | None


class ArtifactContentProvider(Protocol):
    def describe(
        self, workspace_id: UUID, artifact_id: UUID, actor_type: str, actor_id: str
    ) -> ArtifactContentDescriptor:
        raise NotImplementedError

    def open_stream(
        self,
        workspace_id: UUID,
        artifact_id: UUID,
        actor_type: str,
        actor_id: str,
        byte_range: tuple[int | None, int | None] | None,
    ) -> BinaryIO:
        raise NotImplementedError

    def create_download_token(
        self,
        workspace_id: UUID,
        artifact_id: UUID,
        actor_type: str,
        actor_id: str,
        ttl_seconds: int,
    ) -> str:
        raise NotImplementedError


class ArtifactContentService(ArtifactContentProvider):
    def __init__(
        self,
        database: Database,
        store: LocalArtifactContentStore,
        *,
        token_secret: SecretStr | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._store = store
        self._token_secret = token_secret
        self._clock = clock

    def describe(
        self, workspace_id: UUID, artifact_id: UUID, actor_type: str, actor_id: str
    ) -> ArtifactContentDescriptor:
        del actor_type, actor_id
        artifact = self._get_artifact(workspace_id, artifact_id)
        content_available, unavailable_reason = self._content_state(artifact)
        return ArtifactContentDescriptor(
            artifact_id=artifact.id,
            workspace_id=artifact.workspace_id,
            media_type=artifact.media_type,
            byte_size=artifact.size_bytes,
            content_hash=artifact.content_hash.lower(),
            download_name=_download_name(artifact.artifact_type, artifact.media_type),
            content_available=content_available,
            unavailable_reason=unavailable_reason,
        )

    def open_stream(
        self,
        workspace_id: UUID,
        artifact_id: UUID,
        actor_type: str,
        actor_id: str,
        byte_range: tuple[int | None, int | None] | None,
    ) -> BinaryIO:
        del actor_type, actor_id
        artifact = self._get_artifact(workspace_id, artifact_id)
        content_available, unavailable_reason = self._content_state(artifact)
        if not content_available:
            raise ArtifactContentUnavailableError(
                "artifact content is unavailable",
                details={"reason": unavailable_reason or "content_unavailable"},
            )
        try:
            if byte_range is None:
                content = self._store.read(artifact.storage_ref)
            else:
                resolve_byte_range(artifact.size_bytes, byte_range)
                content = self._store.read_range(artifact.storage_ref, byte_range)
        except ArtifactContentIntegrityError as exc:
            raise ArtifactIntegrityFailed("artifact content integrity check failed") from exc
        except ArtifactContentUnavailable as exc:
            raise ArtifactContentUnavailableError(
                "artifact content is unavailable"
            ) from exc
        return BytesIO(content)

    def create_download_token(
        self,
        workspace_id: UUID,
        artifact_id: UUID,
        actor_type: str,
        actor_id: str,
        ttl_seconds: int,
    ) -> str:
        self._get_artifact(workspace_id, artifact_id)
        if self._token_secret is None:
            raise DownloadTokensDisabled(
                "artifact download tokens are not configured"
            )
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        expires_at = self._clock() + timedelta(seconds=ttl_seconds)
        payload = {
            "v": 1,
            "workspace_id": str(workspace_id),
            "artifact_id": str(artifact_id),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "exp": int(expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(12),
        }
        encoded_payload = _base64url(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        signature = _base64url(
            hmac.new(
                self._token_secret.get_secret_value().encode(),
                encoded_payload.encode(),
                hashlib.sha256,
            ).digest()
        )
        return f"v1.{encoded_payload}.{signature}"

    def verify_download_token(
        self, workspace_id: UUID, artifact_id: UUID, token: str
    ) -> tuple[str, str]:
        if self._token_secret is None:
            raise ArtifactContentUnavailableError("artifact download token is invalid")
        try:
            version, encoded_payload, encoded_signature = token.split(".", 2)
            if version != "v1":
                raise ValueError
            expected_signature = hmac.new(
                self._token_secret.get_secret_value().encode(),
                encoded_payload.encode(),
                hashlib.sha256,
            ).digest()
            actual_signature = _unbase64url(encoded_signature)
            if not hmac.compare_digest(expected_signature, actual_signature):
                raise ValueError
            payload = json.loads(_unbase64url(encoded_payload))
            if not isinstance(payload, dict) or payload.get("v") != 1:
                raise ValueError
            if UUID(str(payload["workspace_id"])) != workspace_id:
                raise ValueError
            if UUID(str(payload["artifact_id"])) != artifact_id:
                raise ValueError
            if int(payload["exp"]) <= int(self._clock().timestamp()):
                raise ValueError
            actor_type = payload["actor_type"]
            actor_id = payload["actor_id"]
            if not isinstance(actor_type, str) or not actor_type:
                raise ValueError
            if not isinstance(actor_id, str) or not actor_id:
                raise ValueError
            return actor_type, actor_id
        except (
            binascii.Error,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            UnicodeError,
        ) as exc:
            raise ArtifactContentUnavailableError("artifact download token is invalid") from exc

    def _get_artifact(self, workspace_id: UUID, artifact_id: UUID) -> ArtifactModel:
        with self._database.session() as session:
            artifact = session.scalar(
                select(ArtifactModel).where(
                    ArtifactModel.workspace_id == workspace_id,
                    ArtifactModel.id == artifact_id,
                )
            )
            if artifact is None:
                raise NotFound("artifact not found")
            return artifact

    def _content_state(self, artifact: ArtifactModel) -> tuple[bool, str | None]:
        if not artifact.storage_ref.startswith("artifact-local://"):
            return False, "provider_did_not_supply_content"
        if artifact.media_type not in ALLOWED_ARTIFACT_MEDIA_TYPES:
            raise ArtifactMediaTypeUnsupported(
                "artifact media type is not allowed"
            )
        try:
            reference_workspace, reference_execution, digest, byte_size = (
                self._store.content_metadata(artifact.storage_ref)
            )
        except ArtifactContentIntegrityError as exc:
            raise ArtifactIntegrityFailed(
                "artifact content integrity check failed"
            ) from exc
        except ArtifactContentUnavailable:
            return False, "content_unavailable"
        if (
            reference_workspace != artifact.workspace_id
            or reference_execution != artifact.execution_id
            or digest != artifact.content_hash.lower()
            or byte_size != artifact.size_bytes
        ):
            raise ArtifactIntegrityFailed("artifact content integrity check failed")
        return True, None


def resolve_byte_range(
    size: int, byte_range: tuple[int | None, int | None]
) -> tuple[int, int]:
    start, end = byte_range
    if size <= 0:
        raise ArtifactRangeNotSatisfiable("artifact byte range is unsatisfiable")
    if start is None:
        if end is None or end <= 0:
            raise ArtifactRangeNotSatisfiable("artifact byte range is invalid")
        start, end = max(size - end, 0), size - 1
    else:
        if start < 0:
            raise ArtifactRangeNotSatisfiable("artifact byte range is invalid")
        end = size - 1 if end is None else end
    if end < start or start >= size:
        raise ArtifactRangeNotSatisfiable("artifact byte range is unsatisfiable")
    return start, min(end, size - 1)


def _download_name(artifact_type: str, media_type: str) -> str:
    if artifact_type == "diff":
        return "changes.diff"
    if artifact_type == "log":
        return "worker.log"
    if artifact_type == "screenshot":
        return "screenshot.png" if media_type == "image/png" else "screenshot.jpg"
    if artifact_type == "build":
        return "build.bin"
    if artifact_type == "external":
        return "artifact.bin"
    if media_type == "application/json":
        return "report.json"
    if media_type == "text/x-diff":
        return "report.diff"
    if media_type == "text/plain":
        return "report.txt"
    return "report.bin"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unbase64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
