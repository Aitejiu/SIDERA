from __future__ import annotations

import ipaddress
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOOPBACK_HOSTNAMES = frozenset({"localhost"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIDERA_MAIN_AGENT_")

    adapter_kind: Literal["akashic", "interview-demo", "unconfigured"] = "akashic"
    restore_read_only: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8010, ge=1, le=65535)
    akashic_endpoint: str | None = None
    akashic_workspace_token: SecretStr | None = None
    demo_manifest: Path | None = None
    sidera_workspace_id: UUID
    web_origin: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://127.0.0.1:4174")
    )
    max_message_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    relay_event_limit: int = Field(default=1024, ge=32, le=8192)
    recovered_turn_poll_seconds: float = Field(default=1.0, gt=0, le=10)

    @field_validator("host")
    @classmethod
    def validate_loopback_host(cls, value: str) -> str:
        if value in _LOOPBACK_HOSTNAMES:
            return value
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("host must be loopback") from exc
        if not address.is_loopback:
            raise ValueError("host must be loopback")
        return value

    @field_validator("akashic_endpoint")
    @classmethod
    def validate_akashic_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("/"):
            if PurePosixPath(value).name in {"", ".", ".."}:
                raise ValueError("Akashic endpoint must be a socket path")
            return value

        if value.count(":") != 1:
            raise ValueError(
                "Akashic endpoint must be loopback TCP host:port or an absolute socket"
            )
        host, raw_port = value.rsplit(":", 1)
        if not host or host in _LOOPBACK_HOSTNAMES:
            host_is_loopback = host in _LOOPBACK_HOSTNAMES
        else:
            try:
                host_is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                host_is_loopback = False
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError("Akashic endpoint port must be an integer") from exc
        if not host_is_loopback or not 1 <= port <= 65535:
            raise ValueError(
                "Akashic endpoint must use a loopback host and valid port"
            )
        return value

    @model_validator(mode="after")
    def validate_adapter_configuration(self) -> Settings:
        if self.adapter_kind == "akashic" and self.akashic_endpoint is None:
            raise ValueError("Akashic adapter requires akashic_endpoint")
        if self.adapter_kind == "interview-demo" and self.demo_manifest is None:
            raise ValueError("interview-demo adapter requires demo_manifest")
        return self

    @field_validator("web_origin")
    @classmethod
    def validate_web_origin(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("web_origin must not contain user information")
        if value.path not in {None, "", "/"}:
            raise ValueError("web_origin must not contain a path")
        if value.query is not None or value.fragment is not None:
            raise ValueError("web_origin must not contain query or fragment")
        return value
