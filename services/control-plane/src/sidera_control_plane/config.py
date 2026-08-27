from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIDERA_")

    database_url: str
    environment: str = "development"
    assignment_lease_seconds: int = Field(default=30, gt=0)
    assignment_lease_renew_interval_seconds: float = Field(default=10.0, gt=0)
    worker_heartbeat_timeout_seconds: int = Field(default=45, gt=0)
    scheduler_batch_size: int = Field(default=20, gt=0)
    scheduler_poll_interval_seconds: float = Field(default=1.0, gt=0)
    activation_batch_size: int = Field(default=20, gt=0)
    activation_poll_interval_seconds: float = Field(default=1.0, gt=0)
    reconciler_batch_size: int = Field(default=50, gt=0)
    reconciler_poll_interval_seconds: float = Field(default=2.0, gt=0)
    gateway_batch_size: int = Field(default=20, gt=0)
    gateway_poll_interval_seconds: float = Field(default=1.0, gt=0)
    planning_batch_size: int = Field(default=10, gt=0)
    planning_poll_interval_seconds: float = Field(default=1.0, gt=0)
    planning_lease_ttl_seconds: int = Field(default=60, gt=0)
    planning_lease_renew_interval_seconds: float = Field(default=10.0, gt=0)
    planning_max_diagnostic_bytes: int = Field(default=4096, gt=0)
    planning_temp_root: Path = Path(".sidera/planning")
    planning_opencode_executable: str = Field(default="opencode", min_length=1)
    planning_opencode_model: str | None = None
    planning_opencode_expected_version: str | None = None
    planning_opencode_request_timeout_seconds: float = Field(default=60.0, gt=0)
    planning_opencode_stop_grace_seconds: float = Field(default=5.0, gt=0)
    planning_acp_stderr_limit_bytes: int = Field(default=65_536, gt=0)
    knowledge_base_url: str = Field(default="http://127.0.0.1:8020", min_length=1)
    knowledge_control_plane_token: SecretStr | None = None
    knowledge_metadata_timeout_seconds: float = Field(default=5.0, gt=0, le=10)
    knowledge_materialization_timeout_seconds: float = Field(
        default=15.0, gt=0, le=30
    )
    context_assembly_batch_size: int = Field(default=10, gt=0)
    context_assembly_lease_seconds: int = Field(default=60, gt=0)
    context_assembly_max_attempts: int = Field(default=5, gt=0, le=5)
    context_package_cutover_at: datetime | None = None
    planning_context_ttl_seconds: int = Field(default=1_800, gt=0, le=7_200)
    execution_context_ttl_seconds: int = Field(default=7_200, gt=0, le=86_400)
    knowledge_candidate_spool_dir: Path = Path(".sidera/runtime/candidate-spool")
    knowledge_candidate_spool_quota_bytes: int = Field(
        default=64 * 1024 * 1024, gt=0
    )
    workspace_root: Path = Path(".sidera/workspaces")
    workspace_retention_seconds: int = Field(default=86_400, gt=0)
    git_executable: str = Field(default="git", min_length=1)
    opencode_executable: str = Field(default="opencode", min_length=1)
    opencode_expected_version: str | None = None
    opencode_probe_timeout_seconds: float = Field(default=30.0, gt=0)
    opencode_request_timeout_seconds: float = Field(default=60.0, gt=0)
    opencode_stop_grace_seconds: float = Field(default=5.0, gt=0)
    local_actor_type: str = Field(default="local", min_length=1)
    local_actor_id: str = Field(default="aitejiu", min_length=1)
    projection_stream_poll_interval_seconds: float = Field(default=0.5, gt=0)
    projection_stream_heartbeat_seconds: float = Field(default=15.0, gt=0)
    projection_change_retention_seconds: int = Field(default=604_800, gt=0)
    main_agent_entry_uri: str | None = None
    artifact_root: Path = Path(".sidera/artifacts")
    artifact_max_bytes: int = Field(default=10_485_760, gt=0)
    artifact_download_token_secret: SecretStr | None = None
    artifact_download_token_ttl_seconds: int = Field(default=300, gt=0)
    web_origin: str = "http://127.0.0.1:5173"
    api_bind_host: str = "127.0.0.1"

    @field_validator("knowledge_base_url")
    @classmethod
    def validate_knowledge_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("knowledge_base_url must be an HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("knowledge_base_url cannot contain query or fragment")
        return value.rstrip("/")

    @field_validator("context_package_cutover_at")
    @classmethod
    def validate_context_package_cutover_at(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("context_package_cutover_at must be timezone aware")
        return value

    @model_validator(mode="after")
    def validate_planning_lease_intervals(self) -> "Settings":
        if self.planning_lease_renew_interval_seconds >= (
            self.planning_lease_ttl_seconds / 2
        ):
            raise ValueError(
                "planning_lease_renew_interval_seconds must be less than half "
                "planning_lease_ttl_seconds"
            )
        return self

    @model_validator(mode="after")
    def validate_assignment_lease_intervals(self) -> "Settings":
        if self.assignment_lease_renew_interval_seconds >= (
            self.assignment_lease_seconds / 2
        ):
            raise ValueError(
                "assignment_lease_renew_interval_seconds must be less than half "
                "assignment_lease_seconds"
            )
        return self
