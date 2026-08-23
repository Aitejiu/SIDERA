from __future__ import annotations

from ipaddress import IPv6Address
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _validate_loopback_url(value: AnyHttpUrl, *, origin_only: bool) -> AnyHttpUrl:
    """Require an HTTP origin that stays on the local machine."""

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
    if origin_only and parsed.path not in {"", "/"}:
        raise ValueError("web_base_url must contain only an origin")
    return value


class TaskBridgeConfig(BaseModel):
    """Configuration owned by the SIDERA Task Bridge plugin."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    control_plane_url: AnyHttpUrl = Field(
        default=cast(AnyHttpUrl, "http://127.0.0.1:8000")
    )
    workspace_id: UUID
    actor_type: str = "integration"
    actor_id: str = "akashic-task-bridge"
    planning_profile_id: UUID
    planning_profile_version: int = Field(ge=1)
    web_base_url: AnyHttpUrl = Field(
        default=cast(AnyHttpUrl, "http://127.0.0.1:4174")
    )
    connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    @field_validator("control_plane_url")
    @classmethod
    def validate_control_plane_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        return _validate_loopback_url(value, origin_only=False)

    @field_validator("web_base_url")
    @classmethod
    def validate_web_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        return _validate_loopback_url(value, origin_only=True)

    @field_validator("actor_type", "actor_id")
    @classmethod
    def validate_actor_identity(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor identity must not be empty")
        return value
