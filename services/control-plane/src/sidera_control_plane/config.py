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
    notification_enabled: bool = False
    notification_policy_version: int = Field(default=1, ge=1, le=1)
    notification_blocked_grace_seconds: int = Field(
        default=300, ge=60, le=86_400
    )
    notification_reconcile_batch_size: int = Field(default=50, ge=1, le=200)
    notification_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    notification_cursor_lease_seconds: int = Field(default=30, gt=0, le=600)
    notification_due_sweep_seconds: float = Field(default=15.0, gt=0, le=3_600)
    notification_baseline_required: bool = True
    notification_delivery_batch_size: int = Field(default=50, ge=1, le=200)
    notification_claim_lease_seconds: int = Field(default=30, ge=5, le=300)
    notification_delivery_max_attempts: int = Field(default=10, ge=1, le=100)
    notification_delivery_max_window_seconds: int = Field(default=86_400, gt=0)
    notification_retry_base_seconds: int = Field(default=2, gt=0)
    notification_retry_backoff_max_seconds: int = Field(default=300, gt=0)
    plan_transition_batch_size: int = Field(default=20, gt=0)
    plan_transition_poll_interval_seconds: float = Field(default=1.0, gt=0)
    plan_transition_lease_ttl_seconds: int = Field(default=60, gt=0)
    plan_transition_backoff_max_seconds: int = Field(default=60, gt=0)
    collaboration_batch_size: int = Field(default=20, gt=0, le=200)
    collaboration_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    collaboration_lease_ttl_seconds: int = Field(default=60, gt=5, le=600)
    collaboration_evaluation_timeout_seconds: float = Field(
        default=30.0, gt=0, le=300
    )
    collaboration_max_attempts: int = Field(default=5, gt=0, le=20)
    collaboration_backoff_seconds: int = Field(default=2, gt=0, le=300)
    collaboration_backoff_max_seconds: int = Field(default=60, gt=0, le=3600)
    collaboration_recovery_sweep_seconds: int = Field(default=30, gt=0, le=3600)
    collaboration_governance_enabled: bool = False
    collaboration_max_projection_bytes: int = Field(
        default=262_144, gt=0, le=1_048_576
    )
    collaboration_max_decision_bytes: int = Field(
        default=65_536, gt=0, le=262_144
    )
    collaboration_max_intents_per_decision: int = Field(
        default=128, gt=0, le=1_000
    )
    collaboration_max_strategy_state_bytes: int = Field(
        default=16_384, gt=0, le=65_536
    )
    collaboration_max_diagnostic_bytes: int = Field(
        default=4_096, gt=0, le=16_384
    )
    handoff_batch_size: int = Field(default=20, gt=0)
    handoff_poll_interval_seconds: float = Field(default=1.0, gt=0)
    handoff_lease_ttl_seconds: int = Field(default=30, gt=0)
    handoff_backoff_max_seconds: int = Field(default=60, gt=0)
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
    opencode_handoff_request_timeout_seconds: float = Field(default=60.0, gt=0)
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

    @model_validator(mode="after")
    def validate_collaboration_deadline(self) -> "Settings":
        safety_margin_seconds = 5.0
        if (
            self.collaboration_evaluation_timeout_seconds + safety_margin_seconds
            >= self.collaboration_lease_ttl_seconds
        ):
            raise ValueError(
                "collaboration evaluation timeout must be below collaboration "
                "lease ttl with a safety margin"
            )
        return self
