from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Scenario = Literal[
    "mainline",
    "revision",
    "handoff",
    "rework",
    "recovery",
    "knowledge",
    "governance",
    "controls",
    "history",
]

DEMO_NOTIFICATION_SOURCE_REF = "module:interview-demo/local-channel"

MODULE_KEYS = frozenset(
    {
        "worker_primary",
        "worker_secondary",
        "workspace",
        "planner",
        "collaboration",
        "verification",
        "notification",
    }
)
WORKER_KEYS = frozenset(
    {
        "validation",
        "report",
        "review",
        "integration",
        "replacement",
        "verification",
    }
)
WORKER_ITEM_CAPABILITIES = {
    "validation": "demo:worker-item:validate",
    "report": "demo:worker-item:report",
    "review": "demo:worker-item:review-report",
    "integration": "demo:worker-item:integrate",
    "replacement": "demo:worker-item:replacement",
}


class DemoEndpoints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    control_plane: str = Field(min_length=1)
    knowledge: str = Field(min_length=1)
    main_agent: str = Field(min_length=1)
    web: str = Field(min_length=1)


class DemoManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 1
    mode: Literal["interview-demo"] = "interview-demo"
    run_id: UUID
    workspace_id: UUID
    scenario: Scenario
    run_root: Path
    repository: Path
    base_commit: str = Field(min_length=1)
    code_commit: str = Field(min_length=1)
    implementation_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    fingerprint_version: int | None = Field(default=None, ge=1)
    database_name: str = Field(min_length=1, max_length=63)
    knowledge_database_name: str = Field(min_length=1, max_length=63)
    pg_host: Literal["127.0.0.1"] = "127.0.0.1"
    pg_port: int = Field(default=55432, ge=1024, le=65535)
    pg_user: str = Field(default="sidera", min_length=1)
    endpoints: DemoEndpoints
    module_ids: dict[str, UUID]
    worker_ids: dict[str, UUID]
    planning_profile_id: UUID
    planning_profile_version: int = Field(default=1, ge=1)
    execution_deadline_seconds: int = Field(default=3600, gt=0)
    maintenance_inventory_ref: str | None = None
    restore_mode: Literal["read_only"] | None = None
    runtime_root: Path | None = None

    @field_validator("run_root", "repository", "runtime_root")
    @classmethod
    def _absolute_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        value = value.expanduser()
        if not value.is_absolute():
            raise ValueError("manifest paths must be absolute")
        return value

    @field_validator("base_commit", "code_commit", "pg_user")
    @classmethod
    def _trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("manifest text must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_resource_keys(self) -> DemoManifest:
        if set(self.module_ids) != MODULE_KEYS:
            raise ValueError("module_ids must contain the fixed demo module keys")
        if set(self.worker_ids) != WORKER_KEYS:
            raise ValueError("worker_ids must contain the fixed demo worker keys")
        if self.schema_version == 2 and (
            self.fingerprint_version != 2
            or self.implementation_fingerprint is None
        ):
            raise ValueError("schema 2 manifests require implementation identity")
        return self
