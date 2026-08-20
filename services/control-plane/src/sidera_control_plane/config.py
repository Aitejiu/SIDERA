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
