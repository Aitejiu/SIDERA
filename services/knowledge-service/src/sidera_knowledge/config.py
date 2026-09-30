from __future__ import annotations

from pathlib import Path
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class KnowledgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIDERA_KNOWLEDGE_")

    database_url: str
    restore_read_only: bool = False
    restore_schema_validation: bool = False
    workspace_id: UUID | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8020, ge=1, le=65_535)
    local_owner_token: SecretStr | None = None
    main_agent_token: SecretStr | None = None
    control_plane_token: SecretStr | None = None
    request_max_bytes: int = Field(default=524_288, gt=0)
    page_max_bytes: int = Field(default=262_144, gt=0)
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    alembic_head: str = "0001_knowledge_foundation"
    spool_root: Path = Path(".sidera/knowledge-spool")
