from __future__ import annotations

from ipaddress import IPv6Address
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ActorRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=200)

    @field_validator("type", "id")
    @classmethod
    def validate_identity_text(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("actor identity contains control characters")
        value = value.strip()
        if not value:
            raise ValueError("actor identity must not be empty")
        return value


class ControlPlaneClientConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    control_plane_url: AnyHttpUrl
    workspace_id: UUID
    actor: ActorRef
    connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    @field_validator("control_plane_url")
    @classmethod
    def validate_control_plane_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        parsed = urlsplit(str(value))
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("URL scheme must be http or https")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL userinfo is not allowed")
        if parsed.fragment:
            raise ValueError("URL fragments are not allowed")
        if parsed.query:
            raise ValueError("URL queries are not allowed")
        host = parsed.hostname
        if host is None:
            raise ValueError("URL host is required")
        host = host.lower()
        if host not in _LOOPBACK_HOSTS:
            try:
                if not IPv6Address(host).is_loopback:
                    raise ValueError("URL host must be loopback")
            except ValueError as exc:
                raise ValueError("URL host must be loopback") from exc
        return value
