import asyncio
import hashlib
import io
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import redirect_stdout, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.error import URLError
from urllib.request import urlopen
from uuid import UUID

import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr
from sidera_agents.contracts import (
    CapabilitySupport,
    NativeCancelOutcome,
    NativeCancelState,
    NativeCloseState,
    NativeInspection,
    NativeOutcome,
    NativeProcessState,
)

from sidera_product.app import create_app
from sidera_product.auth.local_session import LocalSessionError, read_bootstrap_file
from sidera_product.cli import main as product_cli_main
from sidera_product.cli import migrate_runtime
from sidera_product.composition import build_product_runtime
from sidera_product.config import ProductSettings
from sidera_product.lifecycle import ProductRuntime

from .agent_management_inventory import (
    read_private_json,
    write_evidence,
    write_private_json,
)
from .agent_management_lab import (
    ALLOCATION_NAMESPACE,
    LOCAL_PRINCIPAL_ID,
    ManagementHarness,
    ManagementPrincipal,
    management_evidence_directory,
    management_workspace_id,
)
from .controlled_native import ControlledSecretStore
from .product_targets import (
    COMPONENTS,
    ProductTargetError,
    TargetGuard,
    TargetSelection,
    acquire_guards,
    select_targets,
)

FRESH_MANIFEST_ENV = "SIDERA_PRODUCT_TEST_FRESH_MANIFEST"
_FRESH_MANIFEST_FIELDS = {
    "schema_version",
    "phase",
    "custody",
    "allocation_id",
    "empty_at_transfer",
    "targets",
}
_FRESH_TARGET_FIELDS = {
    "component",
    "database",
    "owner",
    "marker",
    "socket",
    "port",
}
FRESH_DESKTOP_STAGES = frozenset(
    {
        "allocation_transfer",
        "schema_migration",
        "browser_space",
        "browser_authorization",
        "runtime_registration",
        "profile_create",
        "defaults_save",
        "profile_test",
        "unrelated_page",
        "unrelated_conversation",
        "profile_revision",
        "final_cleanup",
    }
)
EXPECTED_FRESH_HEADS = {
    "product": "0004_configuration_requests",
    "agents": "0004_inputs_events",
    "cp": "0039_hard_only_input_selections",
    "credentials": "0001_connections_credentials",
}
_retained_desktop_resources: list[object] = []
_EGO_TASK_SPACE_NAME = "Sidera N20 native-harness"


@dataclass(frozen=True)
class FreshDesktopAdmission:
    allocation_id: str
    targets: tuple[TargetSelection, ...]
    evidence_directory: Path
    manifest_path: Path
    manifest: dict[str, object]


@dataclass(frozen=True)
class FreshDesktopStage:
    stage: str
    intent: dict[str, object]
    result: dict[str, object] | None


class FreshManagementHarness(ManagementHarness):
    def __init__(self) -> None:
        super().__init__()
        self.run_entered: dict[UUID, threading.Event] = {}
        self.run_finished: dict[UUID, threading.Event] = {}
        self.run_gates: dict[UUID, threading.Event] = {}
        self.cancelled_runs: set[UUID] = set()

    def probe(self, candidate, checked_at):
        observed = super().probe(candidate, checked_at)
        return observed.model_copy(
            update={
                "capabilities": {
                    **observed.capabilities,
                    "mcp": CapabilitySupport(
                        status="supported",
                        reason_code="controlled-fresh-desktop",
                        constraints={},
                    ),
                }
            }
        )

    def open(self, request):
        handle = super().open(request)
        with self.lock:
            self.run_entered[handle.run_id] = threading.Event()
            self.run_finished[handle.run_id] = threading.Event()
            self.run_gates[handle.run_id] = threading.Event()
        return handle

    def prompt(self, handle, input, response_id):
        with self.lock:
            self.calls.append(("prompt", handle.run_id, response_id))
            entered = self.run_entered[handle.run_id]
            finished = self.run_finished[handle.run_id]
            gate = self.run_gates[handle.run_id]
            if (
                self.inspections[handle.run_id].process_state
                is not NativeProcessState.GONE
            ):
                self.inspections[handle.run_id] = NativeInspection(
                    process_state="alive", response_state="active", reason_code=None
                )
            self.entered.set()
            entered.set()
        if not gate.wait(timeout=300):
            raise AssertionError("Fresh controlled run was not released")
        with self.lock:
            cancelled = handle.run_id in self.cancelled_runs
            if (
                self.inspections[handle.run_id].process_state
                is not NativeProcessState.GONE
            ):
                self.inspections[handle.run_id] = NativeInspection(
                    process_state="alive", response_state="idle", reason_code=None
                )
            finished.set()
        return NativeOutcome(
            response_id=response_id,
            status="cancelled" if cancelled else "completed",
            reason_code=None,
            observed_model=None,
            usage=None,
        )

    def cancel(self, handle, response_id):
        with self.lock:
            gate = self.run_gates.get(handle.run_id)
            if gate is None:
                return NativeCancelOutcome(
                    response_id=response_id,
                    status=NativeCancelState.UNKNOWN,
                    reason_code="controlled_run_not_entered",
                )
            self.cancelled_runs.add(handle.run_id)
            if (
                self.inspections[handle.run_id].process_state
                is not NativeProcessState.GONE
            ):
                self.inspections[handle.run_id] = NativeInspection(
                    process_state="alive", response_state="idle", reason_code=None
                )
            gate.set()
            self.calls.append(("cancel", handle.run_id, response_id))
        return NativeCancelOutcome(
            response_id=response_id,
            status=NativeCancelState.CONFIRMED,
            reason_code=None,
        )

    def close(self, handle):
        with self.lock:
            outcome = super().close(handle)
            if outcome.status is NativeCloseState.CLOSED:
                self.cancelled_runs.add(handle.run_id)
                gate = self.run_gates.get(handle.run_id)
                if gate is not None:
                    gate.set()
            return outcome

    def wait_for_prompt(self, run_id: UUID, timeout: float = 30) -> bool:
        with self.lock:
            entered = self.run_entered.get(run_id)
        return entered is not None and entered.wait(timeout=timeout)

    def wait_for_finish(self, run_id: UUID, timeout: float = 30) -> bool:
        with self.lock:
            finished = self.run_finished.get(run_id)
        return finished is not None and finished.wait(timeout=timeout)

    def release_run(self, run_id: UUID) -> bool:
        with self.lock:
            gate = self.run_gates.get(run_id)
            if gate is None:
                return False
            gate.set()
            return True


@dataclass
class OwnedUvicorn:
    server: uvicorn.Server
    listener: socket.socket
    thread: threading.Thread
    errors: list[BaseException] = field(default_factory=list)

    def close(self) -> bool:
        self.server.should_exit = True
        self.thread.join(timeout=15)
        try:
            self.listener.close()
            listener_closed = self.listener.fileno() == -1
        except OSError:
            return False
        return not self.thread.is_alive() and listener_closed


@dataclass
class OwnedBootstrapExport:
    location: Path
    parent_device: int
    parent_inode: int
    file_device: int
    file_inode: int
    _file_removed: bool = False
    _closed: bool = False

    @classmethod
    def capture(cls, location: Path) -> "OwnedBootstrapExport":
        if not location.is_absolute() or location.resolve() != location:
            raise ProductTargetError("fresh_cli_bootstrap_refused")
        read_bootstrap_file(location)
        parent = location.parent.lstat()
        metadata = location.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ProductTargetError("fresh_cli_bootstrap_refused")
        return cls(
            location,
            parent.st_dev,
            parent.st_ino,
            metadata.st_dev,
            metadata.st_ino,
        )

    def close(self) -> bool:
        if self._closed:
            return True
        descriptor = None
        try:
            if self.location.parent.resolve() != self.location.parent:
                return False
            descriptor = os.open(
                self.location.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            parent = os.fstat(descriptor)
            if (
                (parent.st_dev, parent.st_ino)
                != (self.parent_device, self.parent_inode)
                or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700
            ):
                return False
            if not self._file_removed:
                metadata = os.stat(
                    self.location.name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    (metadata.st_dev, metadata.st_ino)
                    != (self.file_device, self.file_inode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    return False
                os.unlink(self.location.name, dir_fd=descriptor)
                self._file_removed = True
            os.rmdir(self.location.parent)
            self._closed = True
            return True
        except (OSError, RuntimeError):
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)


class OwnedProcessPort(Protocol):
    backend: OwnedUvicorn | None
    vite: subprocess.Popen[bytes] | None
    bootstrap_export: OwnedBootstrapExport | None
    owned: dict[str, object]


@dataclass
class FreshDesktopLab:
    admission: FreshDesktopAdmission
    guards: list[TargetGuard]
    settings: ProductSettings
    runtime: ProductRuntime | None
    native: FreshManagementHarness
    principal: ManagementPrincipal
    secrets: ControlledSecretStore
    app: FastAPI | None
    backend: OwnedUvicorn | None
    vite: subprocess.Popen[bytes] | None
    bootstrap_file: Path
    vite_root: Path
    browser_space_id: int | None = None
    owned: dict[str, object] = field(default_factory=dict)
    _closed: bool = False
    bootstrap_export: OwnedBootstrapExport | None = None

    @property
    def workspace_id(self) -> UUID:
        return self.settings.workspace_id

    @property
    def backend_url(self) -> str:
        return self.settings.local_url

    @property
    def web_url(self) -> str:
        return self.settings.web_origin

    def close_vite(self) -> bool:
        if self.vite is None:
            return True
        if self.vite.poll() is not None:
            return True
        return _stop_owned_process(self.vite)

    def close(self) -> bool:
        if self._closed:
            return True
        vite_closed = self.close_vite()
        backend_closed = self.backend is None or self.backend.close()
        runtime_closed = self.runtime is None
        if backend_closed and self.runtime is not None:
            try:
                runtime_closed = self.runtime.close(grace_seconds=10) is True
            except Exception:  # noqa: BLE001 - keep cleanup retryable on any close failure.
                runtime_closed = False
        bootstrap_closed = True
        if self.bootstrap_export is not None:
            bootstrap_closed = self.bootstrap_export.close() is True
            self.owned["bootstrap_export"] = {
                "path": str(self.bootstrap_export.location),
                "file_removed": self.bootstrap_export._file_removed,
                "closed": self.bootstrap_export._closed,
            }
        self._closed = (
            vite_closed
            and backend_closed
            and runtime_closed is True
            and bootstrap_closed is True
        )
        return self._closed


def _manifest_path(environ: Mapping[str, str]) -> Path:
    selected = environ.get(FRESH_MANIFEST_ENV)
    if selected is None:
        raise ProductTargetError("fresh_manifest_required")
    path = Path(selected)
    if not path.is_absolute() or ".." in path.parts or str(path) != selected:
        raise ProductTargetError("fresh_manifest_refused")
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except OSError:
        raise ProductTargetError("fresh_manifest_refused") from None
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_IMODE(parent_info.st_mode) != 0o700
        or parent_info.st_uid != os.getuid()
        or parent.resolve() != parent
    ):
        raise ProductTargetError("fresh_manifest_refused")
    return path


def _expected_target_facts(targets: tuple[TargetSelection, ...]):
    return [
        {
            "component": target.component,
            "database": target.database,
            "owner": target.owner,
            "marker": target.marker,
            "socket": target.socket,
            "port": target.port,
        }
        for target in targets
    ]


def fresh_desktop_context_paths(
    admission: FreshDesktopAdmission,
) -> dict[str, Path]:
    directory = admission.evidence_directory / "fresh-desktop" / "context"
    return {
        "directory": directory,
        "file": directory / "context.json",
        "repository": directory / "repository",
        "workspace_root": directory / "workspaces",
        "artifact_root": directory / "artifacts",
        "bootstrap_file": directory / "bootstrap" / "native-session.json",
    }


def _ensure_private_context_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.getuid()
            or directory.resolve() != directory
        ):
            raise ProductTargetError("fresh_context_refused")
    except (OSError, RuntimeError):
        raise ProductTargetError("fresh_context_refused") from None


def _validate_fresh_context(
    value: object, admission: FreshDesktopAdmission
) -> dict[str, object]:
    expected_fields = {
        "schema_version",
        "allocation_id",
        "target_identity",
        "workspace_id",
        "backend_port",
        "vite_port",
    }
    expected_targets = _expected_target_facts(admission.targets)
    expected_workspace = str(management_workspace_id(admission.allocation_id))
    valid_targets = (
        type(value) is dict
        and type(value.get("target_identity")) is list
        and len(value["target_identity"]) == len(COMPONENTS)
        and all(
            type(item) is dict
            and set(item) == _FRESH_TARGET_FIELDS
            and all(
                type(item.get(name)) is str
                for name in ("component", "database", "owner", "marker", "socket")
            )
            and type(item.get("port")) is int
            for item in value["target_identity"]
        )
    )
    if (
        type(value) is not dict
        or set(value) != expected_fields
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("allocation_id") != admission.allocation_id
        or value.get("workspace_id") != expected_workspace
        or not valid_targets
        or value["target_identity"] != expected_targets
        or type(value.get("backend_port")) is not int
        or not 1 <= value["backend_port"] <= 65535
        or type(value.get("vite_port")) is not int
        or not 1 <= value["vite_port"] <= 65535
        or value["backend_port"] == value["vite_port"]
    ):
        raise ProductTargetError("fresh_context_refused")
    return value


class _FreshContextListenerCleanupError(ProductTargetError):
    pass


def _close_rejected_fresh_context_listener(listener: socket.socket) -> None:
    try:
        listener.close()
        closed = listener.fileno() == -1
    except OSError:
        closed = False
    if not closed:
        _retained_desktop_resources.append(listener)
        raise _FreshContextListenerCleanupError("fresh_context_refused") from None


def load_or_create_fresh_context(
    admission: FreshDesktopAdmission,
) -> tuple[dict[str, object], socket.socket]:
    paths = fresh_desktop_context_paths(admission)
    _ensure_private_context_directory(admission.evidence_directory)
    _ensure_private_context_directory(paths["directory"].parent)
    _ensure_private_context_directory(paths["directory"])
    context_file = paths["file"]
    try:
        context_file.lstat()
    except FileNotFoundError:
        existing = False
    except OSError:
        raise ProductTargetError("fresh_context_refused") from None
    else:
        existing = True

    if existing:
        try:
            context = _validate_fresh_context(
                read_private_json(context_file), admission
            )
        except (OSError, ValueError, ProductTargetError):
            raise ProductTargetError("fresh_context_refused") from None
        listener, bound_port = _bind_listener("127.0.0.1", context["backend_port"])
        if bound_port != context["backend_port"]:
            _close_rejected_fresh_context_listener(listener)
            raise ProductTargetError("fresh_context_refused")
        return context, listener

    listener, backend_port = _bind_listener("127.0.0.1", 0)
    try:
        vite_port = 0
        for _ in range(8):
            vite_port = _reserve_port("::1")
            if vite_port != backend_port:
                break
        if vite_port == backend_port:
            raise ProductTargetError("fresh_context_refused")
        context = {
            "schema_version": 1,
            "allocation_id": admission.allocation_id,
            "target_identity": _expected_target_facts(admission.targets),
            "workspace_id": str(management_workspace_id(admission.allocation_id)),
            "backend_port": backend_port,
            "vite_port": vite_port,
        }
        context = _validate_fresh_context(context, admission)
        write_private_json(context_file, context)
    except (OSError, ProductTargetError, TypeError, ValueError):
        _close_rejected_fresh_context_listener(listener)
        raise ProductTargetError("fresh_context_refused") from None
    return context, listener


def read_fresh_manifest(
    path: Path,
    *,
    allocation_id: str,
    targets: tuple[TargetSelection, ...],
) -> dict[str, object]:
    try:
        value = read_private_json(path)
    except (OSError, ValueError, ProductTargetError):
        raise ProductTargetError("fresh_manifest_refused") from None
    if (
        type(value) is not dict
        or set(value) != _FRESH_MANIFEST_FIELDS
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("phase") != "fresh9B"
        or value.get("custody") != "implementation"
        or value.get("allocation_id") != allocation_id
        or value.get("empty_at_transfer") is not True
        or type(value.get("targets")) is not list
        or len(value["targets"]) != len(COMPONENTS)
    ):
        raise ProductTargetError("fresh_manifest_refused")
    for item in value["targets"]:
        if (
            type(item) is not dict
            or set(item) != _FRESH_TARGET_FIELDS
            or any(
                type(item.get(name)) is not str
                for name in ("component", "database", "owner", "marker", "socket")
            )
            or type(item.get("port")) is not int
        ):
            raise ProductTargetError("fresh_manifest_refused")
    if value["targets"] != _expected_target_facts(targets):
        raise ProductTargetError("fresh_manifest_refused")
    return value


def select_management_desktop_targets(
    environ: Mapping[str, str],
) -> FreshDesktopAdmission | None:
    targets = select_targets(environ)
    if targets is None:
        return None
    evidence_directory = management_evidence_directory(environ)
    manifest_path = _manifest_path(environ)
    allocation_id = environ["SIDERA_PRODUCT_TEST_ALLOCATION_ID"]
    manifest = read_fresh_manifest(
        manifest_path,
        allocation_id=allocation_id,
        targets=targets,
    )
    return FreshDesktopAdmission(
        allocation_id,
        targets,
        evidence_directory,
        manifest_path,
        manifest,
    )


def _fresh_stage_paths(
    admission: FreshDesktopAdmission, stage: str
) -> tuple[Path, Path]:
    if stage not in FRESH_DESKTOP_STAGES:
        raise ProductTargetError("fresh_stage_refused")
    allocation_digest = hashlib.sha256(
        admission.allocation_id.encode("utf-8")
    ).hexdigest()
    directory = admission.evidence_directory / "fresh-desktop" / allocation_digest
    return directory / f"{stage}.pending.json", directory / f"{stage}.complete.json"


def _fresh_stage_record(
    value: object,
    *,
    admission: FreshDesktopAdmission,
    stage: str,
    phase: str,
    intent: dict[str, object],
) -> dict[str, object]:
    fields = {
        "schema_version",
        "case",
        "allocation_id",
        "target_identity",
        "stage",
        "phase",
        "intent",
    }
    if phase == "complete":
        fields.add("result")
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("case") != "fresh-desktop"
        or value.get("allocation_id") != admission.allocation_id
        or value.get("target_identity") != _expected_target_facts(admission.targets)
        or value.get("stage") != stage
        or value.get("phase") != phase
        or value.get("intent") != intent
        or (phase == "complete" and type(value.get("result")) is not dict)
    ):
        raise ProductTargetError("fresh_stage_refused")
    return value


def _read_fresh_stage(
    path: Path,
    *,
    admission: FreshDesktopAdmission,
    stage: str,
    phase: str,
    intent: dict[str, object],
) -> dict[str, object]:
    try:
        value = read_private_json(path)
    except (OSError, ValueError, ProductTargetError):
        raise ProductTargetError("fresh_stage_refused") from None
    return _fresh_stage_record(
        value,
        admission=admission,
        stage=stage,
        phase=phase,
        intent=intent,
    )


def begin_fresh_stage(
    admission: FreshDesktopAdmission,
    stage: str,
    intent: dict[str, object],
) -> tuple[bool, dict[str, object] | None]:
    if type(intent) is not dict:
        raise ProductTargetError("fresh_stage_refused")
    pending_path, complete_path = _fresh_stage_paths(admission, stage)
    for directory in (
        admission.evidence_directory,
        pending_path.parent.parent,
        pending_path.parent,
    ):
        _ensure_private_context_directory(directory)
    pending_exists = _path_exists(pending_path)
    complete_exists = _path_exists(complete_path)
    if complete_exists:
        if not pending_exists:
            raise ProductTargetError("fresh_stage_refused")
        _read_fresh_stage(
            pending_path,
            admission=admission,
            stage=stage,
            phase="pending",
            intent=intent,
        )
        record = _read_fresh_stage(
            complete_path,
            admission=admission,
            stage=stage,
            phase="complete",
            intent=intent,
        )
        return False, record["result"]
    if pending_exists:
        _read_fresh_stage(
            pending_path,
            admission=admission,
            stage=stage,
            phase="pending",
            intent=intent,
        )
        raise ProductTargetError("fresh_stage_pending")
    record = {
        "schema_version": 1,
        "case": "fresh-desktop",
        "allocation_id": admission.allocation_id,
        "target_identity": _expected_target_facts(admission.targets),
        "stage": stage,
        "phase": "pending",
        "intent": intent,
    }
    try:
        write_private_json(pending_path, record)
    except (OSError, ProductTargetError, TypeError, ValueError):
        raise ProductTargetError("fresh_stage_refused") from None
    return True, None


def complete_fresh_stage(
    admission: FreshDesktopAdmission,
    stage: str,
    intent: dict[str, object],
    result: dict[str, object],
) -> None:
    if type(result) is not dict:
        raise ProductTargetError("fresh_stage_refused")
    pending_path, complete_path = _fresh_stage_paths(admission, stage)
    if not _path_exists(pending_path):
        raise ProductTargetError("fresh_stage_refused")
    _read_fresh_stage(
        pending_path,
        admission=admission,
        stage=stage,
        phase="pending",
        intent=intent,
    )
    if _path_exists(complete_path):
        saved = _read_fresh_stage(
            complete_path,
            admission=admission,
            stage=stage,
            phase="complete",
            intent=intent,
        )
        if saved["result"] != result:
            raise ProductTargetError("fresh_stage_refused")
        return
    record = {
        "schema_version": 1,
        "case": "fresh-desktop",
        "allocation_id": admission.allocation_id,
        "target_identity": _expected_target_facts(admission.targets),
        "stage": stage,
        "phase": "complete",
        "intent": intent,
        "result": result,
    }
    try:
        write_private_json(complete_path, record)
    except (OSError, ProductTargetError, TypeError, ValueError):
        raise ProductTargetError("fresh_stage_refused") from None


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise ProductTargetError("fresh_stage_refused") from None


def _target_inventory_facts(guard: TargetGuard) -> dict[str, object]:
    inventory = guard.initial.get("inventory")
    if type(inventory) is not dict:
        raise ProductTargetError("fresh_target_inventory_refused")
    relations = inventory.get("relations")
    heads = inventory.get("heads")
    schemas = inventory.get("schemas")
    functions = inventory.get("functions")
    if any(type(value) is not list for value in (relations, heads, schemas, functions)):
        raise ProductTargetError("fresh_target_inventory_refused")
    allowed_schemas = [
        item
        for item in schemas
        if item != {"name": "public", "owner": "pg_database_owner"}
    ]
    if relations or heads or functions or allowed_schemas:
        raise ProductTargetError("fresh_target_not_empty")
    return {
        "component": guard.target.component,
        "relations": relations,
        "heads": heads,
        "schemas": allowed_schemas,
        "functions": functions,
    }


def validate_fresh_empty_targets(
    guards: list[TargetGuard],
) -> dict[str, dict[str, object]]:
    if tuple(guard.target.component for guard in guards) != COMPONENTS:
        raise ProductTargetError("product_guard_order_refused")
    return {guard.target.component: _target_inventory_facts(guard) for guard in guards}


def validate_fresh_heads(guards: list[TargetGuard]) -> dict[str, str]:
    if tuple(guard.target.component for guard in guards) != COMPONENTS:
        raise ProductTargetError("product_guard_order_refused")
    result: dict[str, str] = {}
    for guard in guards:
        current = guard.inspect()["inventory"]["heads"]
        if (
            len(current) != 1
            or current[0] != EXPECTED_FRESH_HEADS[guard.target.component]
        ):
            raise ProductTargetError("fresh_migration_head_refused")
        result[guard.target.component] = current[0]
    return result


def _bind_listener(host: str, port: int = 0) -> tuple[socket.socket, int]:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ProductTargetError("fresh_loopback_bind_refused")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        return listener, int(listener.getsockname()[1])
    except OSError:
        listener.close()
        raise ProductTargetError("fresh_loopback_bind_refused") from None


def _reserve_port(host: str) -> int:
    listener, port = _bind_listener(host)
    listener.close()
    return port


def start_owned_backend(
    app: FastAPI,
    settings: ProductSettings,
    *,
    listener: socket.socket | None = None,
    resource_owner: OwnedProcessPort,
) -> OwnedUvicorn:
    if listener is None:
        listener, port = _bind_listener(settings.bind_host)
    else:
        port = int(listener.getsockname()[1])
    if port != settings.port:
        listener.close()
        raise ProductTargetError("fresh_backend_port_mismatch")
    config = uvicorn.Config(
        app,
        host=settings.bind_host,
        port=settings.port,
        workers=1,
        reload=False,
        lifespan="on",
        log_level="warning",
    )
    server = uvicorn.Server(config)
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            asyncio.run(server.serve(sockets=[listener]))
        except BaseException as error:  # noqa: BLE001 - owner-thread failures are retained
            errors.append(error)

    thread = threading.Thread(target=serve, name="sidera-fresh-product", daemon=True)
    owner = OwnedUvicorn(server, listener, thread, errors)
    resource_owner.backend = owner
    thread.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if errors or not thread.is_alive():
            break
        if server.started:
            return owner
        time.sleep(0.05)
    owner.close()
    raise ProductTargetError("fresh_backend_start_refused")


def _vite_environment(
    tmp_path: Path, web_root: Path
) -> tuple[dict[str, str], str, str]:
    node = shutil.which("node")
    if node is None:
        raise ProductTargetError("fresh_vite_toolchain_unavailable")
    node_path = Path(node).resolve()
    vite_path = (web_root / "node_modules" / "vite" / "bin" / "vite.js").resolve()
    if not vite_path.is_file():
        raise ProductTargetError("fresh_vite_toolchain_unavailable")
    path = os.pathsep.join(dict.fromkeys((str(node_path.parent), "/usr/bin", "/bin")))
    home = tmp_path / "vite-home"
    _ensure_private_context_directory(home)
    environment = {
        "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": path,
        "TMPDIR": str(tmp_path),
    }
    try:
        completed = subprocess.run(
            [str(node_path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
        if completed.returncode != 0 or not completed.stdout.strip().startswith("v24."):
            raise ProductTargetError("fresh_node24_required")
    except (OSError, subprocess.TimeoutExpired):
        raise ProductTargetError("fresh_vite_toolchain_unavailable") from None
    return environment, str(node_path), str(vite_path)


def start_owned_vite(
    *,
    tmp_path: Path,
    web_root: Path,
    backend_url: str,
    port: int,
    resource_owner: OwnedProcessPort,
    knowledge_port: int | None = None,
    knowledge_token: SecretStr | None = None,
) -> subprocess.Popen[bytes]:
    if (knowledge_port is not None or knowledge_token is not None) and (
        type(knowledge_port) is not int
        or not 1 <= knowledge_port <= 65535
        or not isinstance(knowledge_token, SecretStr)
        or not knowledge_token.get_secret_value().strip()
    ):
        raise ProductTargetError("fresh_knowledge_binding_refused")
    environment, node, vite_script = _vite_environment(tmp_path, web_root)
    environment.update(
        {
            "SIDERA_WEB_BACKEND": "native",
            "SIDERA_WEB_PRODUCT_URL": backend_url,
        }
    )
    if knowledge_port is not None and knowledge_token is not None:
        environment.update(
            SIDERA_WEB_KNOWLEDGE_SERVICE_PORT=str(knowledge_port),
            SIDERA_WEB_KNOWLEDGE_TOKEN=knowledge_token.get_secret_value(),
        )
    try:
        process = subprocess.Popen(
            [
                node,
                vite_script,
                "--host",
                "::1",
                "--port",
                str(port),
                "--strictPort",
            ],
            cwd=web_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        raise ProductTargetError("fresh_vite_start_refused") from None
    resource_owner.vite = process
    url = f"http://[::1]:{port}/"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return process
        except (OSError, URLError):
            time.sleep(0.05)
    _stop_owned_process(process)
    raise ProductTargetError("fresh_vite_start_refused")


def _stop_owned_process(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except ProcessLookupError:
        return process.poll() is not None
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


def create_cli_bootstrap(
    settings: ProductSettings,
    *,
    resource_owner: OwnedProcessPort,
) -> dict[str, str]:
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            exit_code = product_cli_main(["session", "bootstrap"], settings=settings)
        if exit_code != 0:
            raise ProductTargetError("fresh_cli_bootstrap_refused")
        value = json.loads(captured.getvalue())
        if (
            type(value) is not dict
            or set(value) != {"output", "expires_at"}
            or type(value.get("output")) is not str
            or not value["output"]
            or type(value.get("expires_at")) is not str
            or not value["expires_at"]
        ):
            raise ProductTargetError("fresh_cli_bootstrap_refused")
        export = OwnedBootstrapExport.capture(Path(value["output"]))
        resource_owner.bootstrap_export = export
        resource_owner.owned["bootstrap_export"] = {
            "path": str(export.location),
            "file_removed": False,
            "closed": False,
        }
        return {"output": str(export.location), "expires_at": value["expires_at"]}
    except (OSError, RuntimeError, ValueError, TypeError, LocalSessionError):
        raise ProductTargetError("fresh_cli_bootstrap_refused") from None


def run_ego_browser(script: str) -> dict[str, object]:
    executable = shutil.which("ego-browser")
    if executable is None:
        raise ProductTargetError("fresh_browser_unavailable")
    try:
        completed = subprocess.run(
            [executable, "nodejs", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProductTargetError("fresh_browser_run_refused") from None
    if completed.returncode != 0:
        raise ProductTargetError("fresh_browser_run_refused")
    result_lines = [
        line.removeprefix("FRESH_RESULT:")
        for stream in (completed.stdout, completed.stderr)
        for line in stream.splitlines()
        if line.startswith("FRESH_RESULT:")
    ]
    if len(result_lines) != 1:
        raise ProductTargetError("fresh_browser_result_refused")
    try:
        result = json.loads(result_lines[0])
    except (ValueError, TypeError):
        raise ProductTargetError("fresh_browser_result_refused") from None
    if type(result) is not dict:
        raise ProductTargetError("fresh_browser_result_refused")
    return result


@pytest.fixture
def management_desktop_lab(tmp_path):
    admission = select_management_desktop_targets(os.environ)
    if admission is None:
        pytest.skip("NOT_RUN: all nineteen product target selectors are absent")
    guards = acquire_guards(admission.targets)
    lab: FreshDesktopLab | None = None
    released = False
    backend_listener: socket.socket | None = None
    context_cleanup_complete = True
    unowned_listener_closed = backend_listener is None
    try:
        transfer_intent = {
            "manifest_sha256": hashlib.sha256(
                admission.manifest_path.read_bytes()
            ).hexdigest(),
            "targets": _expected_target_facts(admission.targets),
        }
        started, saved = begin_fresh_stage(
            admission, "allocation_transfer", transfer_intent
        )
        if started:
            empty = validate_fresh_empty_targets(guards)
            transfer_result = {"empty_inventory": empty, "accepted": True}
            complete_fresh_stage(
                admission, "allocation_transfer", transfer_intent, transfer_result
            )
        elif (
            type(saved) is not dict
            or set(saved) != {"empty_inventory", "accepted"}
            or saved.get("accepted") is not True
            or type(saved.get("empty_inventory")) is not dict
            or set(saved["empty_inventory"]) != set(COMPONENTS)
        ):
            raise ProductTargetError("fresh_stage_result_refused")
        else:
            empty = saved["empty_inventory"]

        try:
            context, backend_listener = load_or_create_fresh_context(admission)
        except _FreshContextListenerCleanupError:
            context_cleanup_complete = False
            raise
        context_paths = fresh_desktop_context_paths(admission)
        for directory in (
            context_paths["repository"],
            context_paths["workspace_root"],
            context_paths["artifact_root"],
            context_paths["bootstrap_file"].parent,
        ):
            _ensure_private_context_directory(directory)
        backend_port = context["backend_port"]
        vite_port = context["vite_port"]
        workspace_id = UUID(context["workspace_id"])
        by_component = {target.component: target for target in admission.targets}
        repository = context_paths["repository"]
        workspace_root = context_paths["workspace_root"]
        artifact_root = context_paths["artifact_root"]
        principal = ManagementPrincipal(workspace_id, LOCAL_PRINCIPAL_ID, repository)
        native = FreshManagementHarness()
        secrets = ControlledSecretStore()
        settings = ProductSettings(
            product_database_url=by_component["product"].url.render_as_string(
                hide_password=False
            ),
            agent_database_url=by_component["agents"].url.render_as_string(
                hide_password=False
            ),
            cp_database_url=by_component["cp"].url.render_as_string(
                hide_password=False
            ),
            credential_database_url=by_component["credentials"].url.render_as_string(
                hide_password=False
            ),
            deployment_namespace=ALLOCATION_NAMESPACE,
            workspace_id=workspace_id,
            local_principal_id=LOCAL_PRINCIPAL_ID,
            workspace_root=workspace_root,
            artifact_root=artifact_root,
            bind_host="127.0.0.1",
            port=backend_port,
            workers=1,
            reload=False,
            web_origin=f"http://[::1]:{vite_port}",
            agent_poll_interval_seconds=0.01,
        )
        runtime = build_product_runtime(
            settings,
            harnesses=(native,),
            request_authorizer=principal,
            product_authorizer=principal,
            secret_store=secrets,
        )
        web_root = Path(__file__).resolve().parents[3] / "web"
        bootstrap_file = context_paths["bootstrap_file"]
        lab = FreshDesktopLab(
            admission,
            guards,
            settings,
            runtime,
            native,
            principal,
            secrets,
            None,
            None,
            None,
            bootstrap_file,
            web_root,
        )
        lab.owned["fresh_context"] = context
        migration_intent = {
            "targets": _expected_target_facts(admission.targets),
            "expected_heads": EXPECTED_FRESH_HEADS,
        }
        migration_started, migration_saved = begin_fresh_stage(
            admission, "schema_migration", migration_intent
        )
        if migration_started:
            validate_fresh_empty_targets(guards)
            migrate_runtime(runtime)
            migrated_heads = validate_fresh_heads(guards)
            complete_fresh_stage(
                admission,
                "schema_migration",
                migration_intent,
                {"heads": migrated_heads},
            )
        else:
            migrated_heads = validate_fresh_heads(guards)
            if migration_saved != {"heads": migrated_heads}:
                raise ProductTargetError("fresh_stage_result_refused")
        if not all(runtime.inspect_readiness().values()):
            raise ProductTargetError("fresh_schema_not_ready")
        app = create_app(runtime=runtime)
        lab.app = app
        try:
            start_owned_backend(
                app,
                settings,
                listener=backend_listener,
                resource_owner=lab,
            )
        finally:
            if lab.backend is not None:
                backend_listener = None
        start_owned_vite(
            tmp_path=context_paths["directory"],
            web_root=web_root,
            backend_url=settings.local_url,
            port=vite_port,
            resource_owner=lab,
        )
        lab.owned["migration_heads"] = migrated_heads
        lab.owned["initial_empty_inventory"] = empty
        lab.owned["backend_listener"] = {
            "host": settings.bind_host,
            "port": settings.port,
            "workers": settings.workers,
            "reload": settings.reload,
        }
        lab.owned["vite_listener"] = {"host": "::1", "port": vite_port}
        yield lab
    finally:
        unowned_listener_closed = backend_listener is None
        if backend_listener is not None:
            try:
                backend_listener.close()
                unowned_listener_closed = backend_listener.fileno() == -1
            except OSError:
                unowned_listener_closed = False
            if not unowned_listener_closed:
                _retained_desktop_resources.append(backend_listener)
        clean = (
            context_cleanup_complete
            and unowned_listener_closed
            and (True if lab is None else lab.close())
        )
        if clean:
            try:
                idle = [guard.inspect()["idle"] for guard in guards]
                clean = all(all(value == 0 for value in item.values()) for item in idle)
                write_evidence(
                    admission.evidence_directory,
                    "fresh-desktop-cleanup",
                    {
                        "clean": clean,
                        "owned": {} if lab is None else lab.owned,
                        "idle": idle,
                        "native_calls": [] if lab is None else lab.native.calls,
                        "backend_error_types": []
                        if lab is None or lab.backend is None
                        else [type(error).__name__ for error in lab.backend.errors],
                    },
                )
            except Exception:
                clean = False
                _retained_desktop_resources.extend((lab, guards))
                raise
        if clean:
            for guard in reversed(guards):
                if not guard.release():
                    clean = False
                    break
            released = clean
        if not released:
            _retained_desktop_resources.extend((lab, guards))
            if sys.exc_info()[0] is None:
                pytest.fail("Fresh desktop cleanup or target handoff is incomplete")
