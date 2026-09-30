"""The shared, secret-free manifest contract for R6 basic acceptance runs."""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCENARIO_NAMES = (
    "profile",
    "result-inputs",
    "result-integration",
    "user-flow",
    "intervention",
    "revision",
    "recovery",
    "second-adapter",
    "notification",
    "web-notification",
)
_ROLES = frozenset({"main_agent", "planner", "worker", "second_worker"})
_SHA1 = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_ENV_REF = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_STORE_REF = re.compile(r"^store:[A-Za-z0-9_.-]+$")
_MODULE_REF = re.compile(r"^module:[A-Za-z0-9_.:/-]+$")

type AgentRole = Literal["main_agent", "planner", "worker", "second_worker"]


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: AgentRole
    runtime: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=255)
    endpoint_host: str = Field(min_length=1, max_length=255)
    credential_ref: str = Field(min_length=1, max_length=512)
    config_ref: str = Field(min_length=1, max_length=512)
    config_version: int = Field(ge=1)
    fallbacks: tuple[str, ...] = ()

    @field_validator("runtime", "provider", "model_id")
    @classmethod
    def _trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("profile text must not be empty")
        return value

    @field_validator("endpoint_host")
    @classmethod
    def _validate_endpoint_host(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("endpoint_host must be a host and optional port")
        if "://" in value or any(character in value for character in "/?#@"):
            raise ValueError("endpoint_host must not contain a URL scheme, path or userinfo")
        try:
            parsed = urlsplit(f"//{value}")
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("endpoint_host must contain a valid port") from exc
        if parsed.hostname is None or parsed.username or parsed.password:
            raise ValueError("endpoint_host must contain a host only")
        return value.lower()

    @field_validator("credential_ref")
    @classmethod
    def _validate_credential_ref(cls, value: str) -> str:
        value = value.strip()
        if value == "none" or _ENV_REF.fullmatch(value) or _STORE_REF.fullmatch(value):
            return value
        if value.startswith("file:"):
            path = Path(value.removeprefix("file:"))
            if not path.is_absolute():
                raise ValueError("file credential_ref must be absolute")
            return value
        raise ValueError("credential_ref must be none, env:, store: or absolute file:")

    @field_validator("config_ref")
    @classmethod
    def _validate_config_ref(cls, value: str) -> str:
        value = value.strip()
        if _MODULE_REF.fullmatch(value) or _ENV_REF.fullmatch(value):
            return value
        if value.startswith("file:"):
            path = Path(value.removeprefix("file:"))
            if not path.is_absolute():
                raise ValueError("file config_ref must be absolute")
            return value
        raise ValueError("config_ref must be module:, env: or absolute file:")

    @field_validator("fallbacks")
    @classmethod
    def _validate_fallbacks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("fallbacks must not contain empty values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("fallbacks must be unique")
        return normalized


class ScenarioLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    main_agent_turns: int = Field(ge=0)
    planner_runs: int = Field(ge=0)
    worker_sessions: int = Field(ge=0)
    adapter_prompts: int = Field(ge=0)
    max_seconds: int = Field(gt=0, le=3600)


class AllowedRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: str = Field(min_length=1, max_length=128)
    role: AgentRole
    runtime: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=255)
    endpoint_host: str = Field(min_length=1, max_length=255)
    credential_ref: str = Field(min_length=1, max_length=512)
    config_version: int = Field(ge=1)
    fallbacks: tuple[str, ...] = ()
    costs: dict[str, int]


class BasicRunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = 1
    run_id: UUID
    workspace_id: UUID
    local_gate_record: Path
    runtime_fingerprint: str = Field(pattern=_SHA256.pattern)
    run_root: Path
    repository: Path
    base_commit: str = Field(pattern=_SHA1.pattern)
    database_ref: str = Field(min_length=1, max_length=512)
    knowledge_database_ref: str = Field(min_length=1, max_length=512)
    control_plane_url: str
    knowledge_url: str
    main_agent_gateway_url: str
    web_url: str
    profiles: tuple[AgentProfile, ...]
    scenarios: dict[str, ScenarioLimits]
    notification_route_ref: str | None = None
    implementation_fingerprint: str | None = Field(
        default=None, pattern=_SHA256.pattern
    )
    fingerprint_version: int | None = Field(default=None, ge=1)
    batch_limits: dict[str, int] | None = None
    allowed_routes: tuple[AllowedRoute, ...] | None = None
    approval_ref: str | None = None
    maintenance_inventory_ref: str | None = None
    restore_mode: Literal["read_only"] | None = None
    resource_binding_mode: Literal["legacy", "persisted"] = "persisted"

    @field_validator("local_gate_record", "run_root", "repository")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        value = value.expanduser()
        if not value.is_absolute():
            raise ValueError("manifest paths must be absolute")
        return value

    @field_validator("database_ref", "knowledge_database_ref")
    @classmethod
    def _database_ref(cls, value: str) -> str:
        value = value.strip()
        if not (_ENV_REF.fullmatch(value) or value.startswith("file:")):
            raise ValueError("database references must be env: or file:")
        if value.startswith("file:") and not Path(value.removeprefix("file:")).is_absolute():
            raise ValueError("file database references must be absolute")
        return value

    @field_validator("notification_route_ref")
    @classmethod
    def _notification_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or not (_ENV_REF.fullmatch(value) or _MODULE_REF.fullmatch(value)):
            raise ValueError("notification_route_ref must be env: or module:")
        return value

    @model_validator(mode="after")
    def _validate_structure(self) -> BasicRunManifest:
        roles = [profile.role for profile in self.profiles]
        if set(roles) != _ROLES or len(roles) != len(_ROLES):
            raise ValueError("roles must appear exactly once")
        if not self.scenarios:
            raise ValueError("at least one scenario is required")
        unknown = set(self.scenarios) - set(SCENARIO_NAMES)
        if unknown:
            raise ValueError(f"unknown scenario names: {sorted(unknown)}")
        for value in (
            self.control_plane_url,
            self.knowledge_url,
            self.main_agent_gateway_url,
            self.web_url,
        ):
            _validate_loopback_url(value)
        if self.schema_version == 2:
            if (
                self.implementation_fingerprint is None
                or self.fingerprint_version != 2
                or self.batch_limits is None
                or self.allowed_routes is None
                or self.approval_ref is None
            ):
                raise ValueError("schema 2 manifest identity and guard fields are required")
            if not self.allowed_routes:
                raise ValueError("schema 2 manifest requires allowed routes")
            if any(value < 0 for value in self.batch_limits.values()):
                raise ValueError("schema 2 batch limits must be non-negative")
        return self


def _validate_loopback_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("service URLs must use HTTP(S)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("service URLs must not contain userinfo, query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("service URLs must be origins")
    host = parsed.hostname
    if host is None:
        raise ValueError("service URL host is required")
    if host.lower() != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("service URLs must be loopback")
        except ValueError as exc:
            raise ValueError("service URLs must be loopback") from exc
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("service URL port is invalid") from exc


def validate_manifest_isolation(
    manifest: BasicRunManifest,
    *,
    sidera_root: Path,
) -> None:
    """Verify resolved paths do not point into SIDERA or its Git common dir."""

    root = sidera_root.expanduser().resolve()
    run_root = manifest.run_root.expanduser().resolve(strict=False)
    repository = manifest.repository.expanduser().resolve(strict=False)
    if root == run_root or root in run_root.parents or root == repository or root in repository.parents:
        raise ValueError("run_root/repository must be outside the SIDERA work tree")
    if run_root == repository or run_root in repository.parents or repository in run_root.parents:
        raise ValueError("run_root and repository must be independent paths")
    if not repository.exists() or not repository.is_dir():
        raise ValueError("acceptance repository does not exist")
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("acceptance repository is not a Git repository") from exc
    common_dir = Path(completed.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repository / common_dir
    common_dir = common_dir.resolve(strict=False)
    if root == common_dir or root in common_dir.parents:
        raise ValueError("repository Git common dir must be outside the SIDERA work tree")


def load_manifest(path: Path) -> BasicRunManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is not valid JSON") from exc
    return BasicRunManifest.model_validate(raw)


__all__ = [
    "SCENARIO_NAMES",
    "AgentProfile",
    "AllowedRoute",
    "BasicRunManifest",
    "ScenarioLimits",
    "load_manifest",
    "validate_manifest_isolation",
]
