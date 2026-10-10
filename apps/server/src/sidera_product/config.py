import json
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, StrictBool, StrictInt, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from sidera_control_plane.config import Settings as ControlPlaneSettings
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class ExplicitControlPlaneSettings(ControlPlaneSettings):
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


class ProductSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIDERA_PRODUCT_",
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    product_database_url: SecretStr = Field(repr=False)
    agent_database_url: SecretStr = Field(repr=False)
    cp_database_url: SecretStr = Field(repr=False)
    credential_database_url: SecretStr = Field(repr=False)
    deployment_namespace: UUID
    workspace_id: UUID
    local_principal_id: str = Field(min_length=1, max_length=200)
    workspace_root: Path
    artifact_root: Path
    bind_host: str = "127.0.0.1"
    port: StrictInt = Field(default=18080, ge=1, le=65535)
    workers: StrictInt = 1
    reload: StrictBool = False
    web_origin: str = "http://127.0.0.1:5173"
    close_grace_seconds: float = Field(default=30.0, ge=0, le=300, allow_inf_nan=False)
    migration_timeout_seconds: float = Field(
        default=60.0, gt=0, le=300, allow_inf_nan=False
    )
    agent_poll_interval_seconds: float = Field(default=0.25, gt=0, le=60)
    agent_input_drain_deadline_seconds: float = Field(
        default=60.0, gt=0, allow_inf_nan=False
    )
    result_inputs_enabled: StrictBool = True
    result_integration_enabled: StrictBool = True
    notification_enabled: StrictBool = True
    managed_credentials_enabled: StrictBool = False
    bootstrap_ttl_seconds: StrictInt = Field(default=300, ge=1, le=3600)
    local_session_ttl_seconds: StrictInt = Field(default=43200, ge=1, le=604800)
    tool_grant_ttl_seconds: StrictInt = Field(default=86400, ge=1, le=604800)
    selected_secrets: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    knowledge_base_url: str | None = None
    knowledge_token: SecretStr | None = Field(default=None, repr=False)

    @field_validator(
        "port",
        "workers",
        "reload",
        "result_inputs_enabled",
        "result_integration_enabled",
        "notification_enabled",
        "managed_credentials_enabled",
        "bootstrap_ttl_seconds",
        "local_session_ttl_seconds",
        "tool_grant_ttl_seconds",
        mode="before",
    )
    @classmethod
    def decode_config_scalar(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            raise ValueError(
                "A valid JSON scalar configuration value is required"
            ) from None

    @field_validator(
        "product_database_url",
        "agent_database_url",
        "cp_database_url",
        "credential_database_url",
    )
    @classmethod
    def validate_target(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except (ArgumentError, ValueError):
            raise ValueError("An explicit PostgreSQL target is required") from None
        if (
            url.drivername != "postgresql+psycopg"
            or not url.database
            or not url.username
            or not (url.host or url.query.get("host"))
            or any(key in url.query for key in ("service", "servicefile", "hostaddr"))
        ):
            raise ValueError("An explicit PostgreSQL target is required")
        return value

    @field_validator("workspace_root", "artifact_root")
    @classmethod
    def validate_location(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("An explicit absolute storage location is required")
        return value

    @field_validator("local_principal_id")
    @classmethod
    def validate_principal(cls, value: str) -> str:
        if not value.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("An explicit local principal is required")
        return value

    @field_validator("bind_host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not is_loopback(value):
            raise ValueError("The native product must bind to loopback")
        return value

    @field_validator("workers")
    @classmethod
    def validate_workers(cls, value: int) -> int:
        if value != 1:
            raise ValueError("The native product requires exactly one worker")
        return value

    @field_validator("reload")
    @classmethod
    def validate_reload(cls, value: bool) -> bool:
        if value:
            raise ValueError("Implicit native owner reload is not supported")
        return value

    @field_validator("web_origin", "knowledge_base_url")
    @classmethod
    def validate_http_location(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError("An explicit loopback HTTP location is required") from None
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or not is_loopback(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("An explicit loopback HTTP location is required")
        return value.rstrip("/")

    @property
    def local_url(self) -> str:
        host = f"[{self.bind_host}]" if ":" in self.bind_host else self.bind_host
        return f"http://{host}:{self.port}"

    def control_plane_settings(self) -> ExplicitControlPlaneSettings:
        values: dict[str, object] = {
            "database_url": self.cp_database_url.get_secret_value(),
            "local_actor_type": "user",
            "local_actor_id": self.local_principal_id,
            "workspace_root": self.workspace_root,
            "artifact_root": self.artifact_root,
            "planning_temp_root": self.workspace_root / "planning",
            "result_store_root": self.artifact_root / "results",
            "knowledge_candidate_spool_dir": self.artifact_root
            / "knowledge-candidates",
            "web_origin": self.web_origin,
            "api_bind_host": self.bind_host,
            "result_inputs_enabled": self.result_inputs_enabled,
            "result_integration_enabled": self.result_integration_enabled,
            "notification_enabled": self.notification_enabled,
            "result_repository_path": None,
            "interview_demo_manifest": None,
            "gateway_sync_workspace_id": None,
            "knowledge_control_plane_token": self.knowledge_token,
        }
        if self.knowledge_base_url is not None:
            values["knowledge_base_url"] = self.knowledge_base_url
        return ExplicitControlPlaneSettings.model_validate(values)
