from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIDERA_")

    database_url: str
    environment: str = "development"
    assignment_lease_seconds: int = Field(default=30, gt=0)
    worker_heartbeat_timeout_seconds: int = Field(default=45, gt=0)
    scheduler_batch_size: int = Field(default=20, gt=0)
    scheduler_poll_interval_seconds: float = Field(default=1.0, gt=0)
    reconciler_batch_size: int = Field(default=50, gt=0)
    reconciler_poll_interval_seconds: float = Field(default=2.0, gt=0)
    gateway_batch_size: int = Field(default=20, gt=0)
    gateway_poll_interval_seconds: float = Field(default=1.0, gt=0)
    workspace_root: Path = Path(".sidera/workspaces")
    workspace_retention_seconds: int = Field(default=86_400, gt=0)
    git_executable: str = Field(default="git", min_length=1)
    opencode_executable: str = Field(default="opencode", min_length=1)
