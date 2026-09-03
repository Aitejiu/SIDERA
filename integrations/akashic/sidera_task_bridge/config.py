from __future__ import annotations

import os
from collections.abc import Mapping
from ipaddress import IPv6Address
from typing import Any, Self, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _validate_http_url(
    value: AnyHttpUrl,
    *,
    origin_only: bool,
    loopback_only: bool,
) -> AnyHttpUrl:
    """Validate an HTTP URL and optionally require a local-only origin."""

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
    if loopback_only and host not in _LOOPBACK_HOSTS:
        try:
            if not IPv6Address(host).is_loopback:
                raise ValueError("URL host must be loopback")
        except ValueError as exc:
            raise ValueError("URL host must be loopback") from exc
    if origin_only and parsed.path not in {"", "/"}:
        raise ValueError("web_base_url must contain only an origin")
    return value


def _validate_loopback_url(value: AnyHttpUrl, *, origin_only: bool) -> AnyHttpUrl:
    """Require an HTTP origin that stays on the local machine."""

    return _validate_http_url(
        value,
        origin_only=origin_only,
        loopback_only=True,
    )


class TaskBridgeConfig(BaseModel):
    """Configuration owned by the SIDERA Task Bridge plugin."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    control_plane_url: AnyHttpUrl = Field(
        default=cast(AnyHttpUrl, "http://127.0.0.1:8000")
    )
    workspace_id: UUID
    actor_type: str = "integration"
    actor_id: str = "akashic-task-bridge"
    governance_actor_type: str | None = None
    governance_actor_id: str | None = None
    planning_profile_id: UUID
    planning_profile_version: int = Field(ge=1)
    web_base_url: AnyHttpUrl = Field(
        default=cast(AnyHttpUrl, "http://127.0.0.1:4174")
    )
    knowledge_base_url: AnyHttpUrl = Field(
        default=cast(AnyHttpUrl, "http://127.0.0.1:8020")
    )
    knowledge_allow_remote: bool = False
    knowledge_main_agent_token: SecretStr | None = Field(default=None, repr=False)
    knowledge_timeout_seconds: float = Field(default=5.0, gt=0, le=10)
    connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    notification_retry_base_seconds: float = Field(default=0.5, gt=0, le=30)
    notification_retry_max_seconds: float = Field(default=15.0, ge=0.5, le=300)
    notification_delivery_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    notification_event_limit_bytes: int = Field(
        default=262_144, ge=4096, le=1_048_576
    )

    @model_validator(mode="before")
    @classmethod
    def load_knowledge_environment(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        env_base_url = os.environ.get("SIDERA_KNOWLEDGE_BASE_URL")
        if "knowledge_base_url" not in values and env_base_url:
            values["knowledge_base_url"] = env_base_url
        env_timeout = os.environ.get("SIDERA_KNOWLEDGE_TIMEOUT_SECONDS")
        if "knowledge_timeout_seconds" not in values and env_timeout:
            values["knowledge_timeout_seconds"] = env_timeout

        configured_token = values.get("knowledge_main_agent_token")
        if configured_token not in (None,):
            if not isinstance(configured_token, SecretStr):
                raise ValueError(
                    "knowledge_main_agent_token must be provided through the environment"
                )
        else:
            env_token = os.environ.get("SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN")
            if env_token is not None:
                values["knowledge_main_agent_token"] = SecretStr(env_token)
        return cast(Any, values)

    @field_validator("control_plane_url")
    @classmethod
    def validate_control_plane_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        return _validate_loopback_url(value, origin_only=False)

    @field_validator("web_base_url")
    @classmethod
    def validate_web_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        return _validate_loopback_url(value, origin_only=True)

    @model_validator(mode="after")
    def validate_knowledge_base_url(self) -> Self:
        _validate_http_url(
            self.knowledge_base_url,
            origin_only=False,
            loopback_only=not self.knowledge_allow_remote,
        )
        return self

    @field_validator("knowledge_main_agent_token")
    @classmethod
    def validate_knowledge_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("knowledge_main_agent_token must not be empty")
        return value

    @field_validator("actor_type", "actor_id")
    @classmethod
    def validate_actor_identity(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor identity must not be empty")
        return value

    @field_validator("governance_actor_type", "governance_actor_id")
    @classmethod
    def validate_governance_actor_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("governance actor identity must not be empty")
        return value

    @model_validator(mode="after")
    def validate_governance_actor_pair(self) -> Self:
        if (self.governance_actor_type is None) != (
            self.governance_actor_id is None
        ):
            raise ValueError(
                "governance actor type and id must be provided together"
            )
        return self

    def governance_principal_matches(self, actor_type: str, actor_id: str) -> bool:
        """Check the opt-in bridge principal against Control Plane readiness data."""

        return (
            self.governance_actor_type == actor_type.strip()
            and self.governance_actor_id == actor_id.strip()
        )
