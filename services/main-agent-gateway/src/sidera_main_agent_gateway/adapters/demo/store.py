from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from typing import IO, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sidera_task_bridge.state import KeyValueStore

_PROJECT_ROOT = Path(__file__).resolve().parents[6]


class DemoManifestEndpoints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    control_plane: str
    knowledge: str
    main_agent: str
    web: str


class DemoManifestIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 1
    mode: Literal["interview-demo"] = "interview-demo"
    run_id: UUID
    workspace_id: UUID
    scenario: Literal[
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
    run_root: Path
    repository: Path
    base_commit: str
    code_commit: str
    implementation_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    fingerprint_version: int | None = Field(default=None, ge=1)
    database_name: str
    knowledge_database_name: str
    pg_host: Literal["127.0.0.1"] = "127.0.0.1"
    pg_port: int = Field(default=55432, ge=1024, le=65535)
    pg_user: str = "sidera"
    endpoints: DemoManifestEndpoints
    module_ids: dict[str, UUID]
    worker_ids: dict[str, UUID]
    planning_profile_id: UUID
    planning_profile_version: int = Field(ge=1)
    execution_deadline_seconds: int = Field(default=3600, ge=1, le=86_400)
    maintenance_inventory_ref: str | None = None
    restore_mode: Literal["read_only"] | None = None
    runtime_root: Path | None = None

    @field_validator("run_root", "repository", "runtime_root")
    @classmethod
    def absolute_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if not value.expanduser().is_absolute():
            raise ValueError("manifest paths must be absolute")
        return value.expanduser().resolve(strict=False)

    @field_validator("base_commit", "code_commit")
    @classmethod
    def required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("manifest commit is required")
        return value.strip()

    @model_validator(mode="after")
    def validate_database_identity(self) -> Self:
        if self.database_name != f"sidera_interview_test_{self.run_id.hex}":
            raise ValueError("manifest database name is not tied to run id")
        if self.knowledge_database_name != (
            f"sidera_interview_knowledge_{self.run_id.hex}"
        ):
            raise ValueError("manifest knowledge database name is not tied to run id")
        if not self.pg_user.strip():
            raise ValueError("manifest pg user is required")
        return self

    @model_validator(mode="after")
    def validate_schema_identity(self) -> Self:
        if self.schema_version == 2 and (
            self.fingerprint_version != 2
            or self.implementation_fingerprint is None
        ):
            raise ValueError("schema 2 manifest identity is incomplete")
        return self


class DemoStoreAlreadyOpen(RuntimeError):
    """Another Gateway process already owns this run's conversation store."""


_MODULE_KEYS = frozenset(
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
_WORKER_KEYS = frozenset(
    {
        "validation",
        "report",
        "review",
        "integration",
        "replacement",
        "verification",
    }
)


def load_demo_manifest(path: Path) -> DemoManifestIdentity:
    if not path.is_absolute():
        raise ValueError("demo manifest path must be absolute")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("demo manifest must be an object")
        if any(
            "secret" in str(key).casefold()
            or "token" in str(key).casefold()
            or "password" in str(key).casefold()
            or "credential" in str(key).casefold()
            for key in raw
        ):
            raise ValueError("demo manifest must not contain credentials")
        identity = DemoManifestIdentity.model_validate(raw)
    except (OSError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("demo manifest is invalid") from exc
    if set(identity.module_ids) != _MODULE_KEYS:
        raise ValueError("demo manifest module identity is incomplete")
    if set(identity.worker_ids) != _WORKER_KEYS:
        raise ValueError("demo manifest worker identity is incomplete")
    _validate_manifest_paths(identity)
    _validate_endpoints(identity.endpoints)
    return identity


def _validate_manifest_paths(identity: DemoManifestIdentity) -> None:
    project_root = _PROJECT_ROOT.resolve(strict=False)
    runtime_root = (identity.runtime_root or identity.run_root).resolve(strict=False)
    if identity.restore_mode == "read_only":
        if (
            _is_within(identity.run_root, project_root)
            or _is_within(project_root, identity.run_root)
            or _is_within(runtime_root, project_root)
            or _is_within(project_root, runtime_root)
            or runtime_root == identity.run_root
            or _is_within(runtime_root, identity.run_root)
            or _is_within(identity.run_root, runtime_root)
        ):
            raise ValueError("demo manifest restore paths are not isolated")
    else:
        expected_run_root = (
            project_root / ".sidera" / "interview-demo" / str(identity.run_id)
        ).resolve(strict=False)
        if identity.run_root != expected_run_root:
            raise ValueError("demo manifest run root is not scoped to this run")
    if _is_within(identity.repository, project_root) or _is_within(
        project_root, identity.repository
    ):
        raise ValueError("demo manifest repository overlaps SIDERA")
    if identity.repository == identity.run_root or _is_within(
        identity.run_root, identity.repository
    ):
        raise ValueError("demo manifest repository overlaps run data")


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _validate_endpoints(endpoints: DemoManifestEndpoints) -> None:
    ports: list[int] = []
    for value in (
        endpoints.control_plane,
        endpoints.knowledge,
        endpoints.main_agent,
        endpoints.web,
    ):
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("demo manifest endpoint is not local and canonical") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1024 <= port <= 65535
            or parsed.netloc != f"127.0.0.1:{port}"
        ):
            raise ValueError("demo manifest endpoint is not local and canonical")
        ports.append(port)
    if len(set(ports)) != len(ports):
        raise ValueError("demo manifest endpoint ports must be distinct")


class DemoKeyValueStore(KeyValueStore):
    """Small durable JSON store for one Gateway process and one demo run."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path.expanduser()
        if not self.path.is_absolute():
            raise ValueError("demo store path must be absolute")
        self._read_only = read_only
        self._owner_lock_file: IO[str] | None = None
        if self._read_only:
            if self.path.is_symlink():
                raise ValueError("demo store path must not be a symlink")
            if self.path.exists() and not self.path.is_file():
                raise ValueError("demo store path must be a file")
            self._read()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_path.touch(exist_ok=True)
        os.chmod(self.lock_path, 0o600)
        self.owner_lock_path = self.path.with_suffix(self.path.suffix + ".owner.lock")
        self.owner_lock_path.touch(exist_ok=True)
        os.chmod(self.owner_lock_path, 0o600)
        self._owner_lock_file = self.owner_lock_path.open(
            "a+", encoding="utf-8"
        )
        try:
            flock(self._owner_lock_file.fileno(), LOCK_EX | LOCK_NB)
        except BlockingIOError as exc:
            self._owner_lock_file.close()
            raise DemoStoreAlreadyOpen("demo conversation store is already open") from exc
        try:
            if not self.path.exists():
                self._write({})
            else:
                self._read()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._owner_lock_file is None or self._owner_lock_file.closed:
            return
        flock(self._owner_lock_file.fileno(), LOCK_UN)
        self._owner_lock_file.close()

    def get(self, key: str, default: object = None) -> object:
        if not key.strip():
            raise ValueError("demo store key is required")
        if self._read_only:
            return self._read().get(key, default)
        with self._locked():
            return self._read().get(key, default)

    def set(self, key: str, value: object) -> None:
        if not key.strip():
            raise ValueError("demo store key is required")
        if self._read_only:
            raise RuntimeError("demo store is read-only")
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("demo store values must be JSON-compatible") from exc
        with self._locked():
            document = self._read()
            document[key] = value
            self._write(document)

    def _read(self) -> dict[str, object]:
        if self._read_only and not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("demo store is corrupt") from exc
        if not isinstance(value, dict):
            raise TypeError("demo store must contain an object")
        return value

    def _write(self, document: dict[str, object]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(document, temporary, ensure_ascii=False, sort_keys=True)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self._owner_lock_file is None or self._owner_lock_file.closed:
            raise RuntimeError("demo conversation store is closed")
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            flock(lock_file.fileno(), LOCK_EX)
            try:
                yield
            finally:
                flock(lock_file.fileno(), LOCK_UN)
