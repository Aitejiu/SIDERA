from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from sidera_control_plane.maintenance_inventory import update_writer_state
from sidera_control_plane.maintenance_lock import MaintenanceLockBusy, maintenance_lock

from .contracts import DemoManifest
from .environment import (
    DemoTokens,
    child_environment,
    component_environment,
    load_demo_tokens,
    runtime_root_for,
)
from .safety import assert_safe_child_environment

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_CONTROL_PLANE_ROOT = _PROJECT_ROOT / "services" / "control-plane"
_KNOWLEDGE_ROOT = _PROJECT_ROOT / "services" / "knowledge-service"
_GATEWAY_ROOT = _PROJECT_ROOT / "services" / "main-agent-gateway"
_WEB_ROOT = _PROJECT_ROOT / "apps" / "web"

_COMPONENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PROCESS_PHASES = frozenset(
    {"preparing", "prepared", "starting", "running", "stopping", "stopped", "failed"}
)
_PROCESS_SCHEMA_VERSION = 1


class SupervisorError(RuntimeError):
    """A local process could not be safely managed."""


class ProcessIdentityError(SupervisorError):
    """Stored process identity does not match the live process."""


class SupervisorStartError(SupervisorError):
    """One or more local demo components failed to start."""


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    explicit_env: Mapping[str, str]
    port: int | None = None
    health_url: str | None = None
    require_log_ready_status: bool = False
    startup_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not _COMPONENT_NAME.fullmatch(self.name):
            raise ValueError("invalid_component_name")
        if not self.argv or not self.argv[0]:
            raise ValueError("process_argv_required")
        if not self.cwd.is_absolute() or not self.cwd.is_dir():
            raise ValueError("process_cwd_must_be_an_existing_absolute_directory")
        if self.port is not None and not 1024 <= self.port <= 65535:
            raise ValueError("process_port_invalid")
        if not 0 < self.startup_timeout_seconds <= 60:
            raise ValueError("startup_timeout_must_not_exceed_60_seconds")


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    component: str
    run_id: UUID
    pid: int
    started_at: str
    argv_hash: str


class DemoSupervisor:
    def __init__(
        self,
        manifest: DemoManifest,
        *,
        specs: Sequence[ProcessSpec],
        inherited_env: Mapping[str, str] | None = None,
    ) -> None:
        self.manifest = manifest
        self.specs = tuple(specs)
        if len({spec.name for spec in self.specs}) != len(self.specs):
            raise ValueError("duplicate_component_name")
        self._inherited_env = dict(os.environ if inherited_env is None else inherited_env)
        self._children: dict[str, subprocess.Popen[bytes]] = {}

    def start(self) -> dict[str, ProcessIdentity]:
        self._ensure_directories()
        started_here: list[ProcessSpec] = []
        identities: dict[str, ProcessIdentity] = {}
        with self._locked():
            write_run_phase(self.manifest, "starting")
            try:
                for spec in self.specs:
                    existing = self._existing_owned_process(spec)
                    if existing is not None:
                        if not self._wait_until_ready(spec, existing.pid):
                            raise SupervisorStartError(
                                f"component_not_ready:{spec.name}"
                            )
                        identities[spec.name] = existing
                        continue
                    if spec.port is not None:
                        _assert_port_available(spec.port)
                    child = self._spawn(spec)
                    self._children[spec.name] = child
                    try:
                        identity = self._capture_identity(spec, child)
                        self._write_identity(identity)
                    except Exception:
                        self._terminate_unrecorded_child(child)
                        raise
                    started_here.append(spec)
                    identities[spec.name] = identity
                    if not self._wait_until_ready(spec, identity.pid):
                        raise SupervisorStartError(f"component_not_ready:{spec.name}")
            except Exception as exc:
                for spec in reversed(started_here):
                    try:
                        self._stop_component(spec)
                    except SupervisorError:
                        pass
                write_run_phase(self.manifest, "failed")
                if isinstance(exc, SupervisorStartError):
                    raise
                message = str(exc) if isinstance(exc, SupervisorError) else "process_start_failed"
                prefix = "partial_start" if started_here else "start_failed"
                raise SupervisorStartError(f"{prefix}:{message}") from exc
            write_run_phase(self.manifest, "running")
            self._update_inventory("running", tuple(identities))
        return identities

    def restart(
        self, component: str, *, timeout_seconds: float = 20.0
    ) -> ProcessIdentity:
        """Restart exactly one owned component and preserve the run identity."""

        if not _COMPONENT_NAME.fullmatch(component):
            raise ValueError("invalid_component_name")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("restart_timeout_must_not_exceed_60_seconds")
        spec = next((item for item in self.specs if item.name == component), None)
        if spec is None:
            raise SupervisorError(f"component_not_registered:{component}")

        self._ensure_directories()
        with self._locked():
            write_run_phase(self.manifest, "starting")
            child: subprocess.Popen[bytes] | None = None
            try:
                self._existing_owned_process(spec)
                self._stop_component(spec, timeout_seconds=timeout_seconds / 2)
                if spec.port is not None:
                    _assert_port_available(spec.port)
                child = self._spawn(spec)
                self._children[spec.name] = child
                identity = self._capture_identity(spec, child)
                self._write_identity(identity)
                if not self._wait_until_ready(spec, identity.pid):
                    raise SupervisorStartError(f"component_not_ready:{component}")
                write_run_phase(self.manifest, "running")
                self._update_inventory(
                    "running", tuple(item.name for item in self.specs)
                )
                return identity
            except Exception as exc:
                if child is not None:
                    self._terminate_unrecorded_child(child)
                write_run_phase(self.manifest, "failed")
                if isinstance(exc, (SupervisorError, ValueError)):
                    raise
                raise SupervisorStartError(f"restart_failed:{component}") from exc

    def stop(self, *, timeout_seconds: float = 20.0) -> tuple[str, ...]:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("stop_timeout_must_not_exceed_60_seconds")
        self._ensure_directories()
        stopped: list[str] = []
        failures: list[str] = []
        identity_failure = False
        deadline = time.monotonic() + timeout_seconds
        ordered_specs = tuple(reversed(self.specs))
        with self._locked():
            write_run_phase(self.manifest, "stopping")
            for index, spec in enumerate(ordered_specs):
                remaining = deadline - time.monotonic()
                remaining_components = len(ordered_specs) - index
                if remaining <= 0:
                    failures.extend(
                        f"{pending.name}:stop_deadline_exhausted"
                        for pending in ordered_specs[index:]
                    )
                    break
                try:
                    if self._stop_component(
                        spec,
                        timeout_seconds=remaining / remaining_components,
                    ):
                        stopped.append(spec.name)
                except ProcessIdentityError as exc:
                    identity_failure = True
                    failures.append(f"{spec.name}:{exc}")
                except SupervisorError as exc:
                    failures.append(f"{spec.name}:{exc}")
            write_run_phase(self.manifest, "failed" if failures else "stopped")
            if not failures:
                self._update_inventory("stopped", ())
        if failures:
            error_type = ProcessIdentityError if identity_failure else SupervisorError
            raise error_type(";".join(failures))
        return tuple(stopped)

    def _update_inventory(
        self, status: str, active_component_ids: tuple[str, ...]
    ) -> None:
        reference = self.manifest.maintenance_inventory_ref
        if reference is None:
            return
        update_writer_state(
            Path(reference.removeprefix("file:")),
            status=status,
            active_component_ids=list(active_component_ids),
        )

    def check(self) -> dict[str, bool]:
        states: dict[str, bool] = {}
        with self._locked():
            for spec in self.specs:
                try:
                    states[spec.name] = self._existing_owned_process(spec) is not None
                except ProcessIdentityError:
                    states[spec.name] = False
        return states

    def _spawn(self, spec: ProcessSpec) -> subprocess.Popen[bytes]:
        environment = child_environment(self._inherited_env, dict(spec.explicit_env))
        assert_safe_child_environment(environment)
        log_path = self._log_path(spec.name)
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(log_path.parent, 0o700)
        with log_path.open("ab", buffering=0) as log_file:
            os.chmod(log_path, 0o600) if log_path.exists() else None
            return subprocess.Popen(
                spec.argv,
                cwd=spec.cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )

    def _capture_identity(
        self, spec: ProcessSpec, child: subprocess.Popen[bytes]
    ) -> ProcessIdentity:
        deadline = time.monotonic() + min(3.0, spec.startup_timeout_seconds)
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise SupervisorStartError(f"component_exited_early:{spec.name}")
            info = _process_info(child.pid)
            if info is not None:
                started_at, command = info
                if command != _expected_command(spec.argv):
                    raise ProcessIdentityError("started_process_argv_mismatch")
                return ProcessIdentity(
                    component=spec.name,
                    run_id=self.manifest.run_id,
                    pid=child.pid,
                    started_at=started_at,
                    argv_hash=_hash_command(command),
                )
            time.sleep(0.025)
        raise SupervisorStartError(f"process_identity_unavailable:{spec.name}")

    def _existing_owned_process(self, spec: ProcessSpec) -> ProcessIdentity | None:
        identity = self._read_identity(spec.name)
        if identity is None:
            return None
        if identity.run_id != self.manifest.run_id:
            raise ProcessIdentityError("run_id_mismatch")
        info = _process_info(identity.pid)
        if info is None:
            return None
        started_at, command = info
        if (
            started_at != identity.started_at
            or _hash_command(command) != identity.argv_hash
            or command != _expected_command(spec.argv)
        ):
            raise ProcessIdentityError("identity_mismatch")
        return identity

    def _wait_until_ready(self, spec: ProcessSpec, pid: int) -> bool:
        started = time.monotonic()
        deadline = started + spec.startup_timeout_seconds
        with httpx.Client(
            timeout=1.0, trust_env=False, follow_redirects=False
        ) as client:
            while time.monotonic() < deadline:
                info = _process_info(pid)
                if info is None:
                    return False
                started_at, command = info
                identity = self._read_identity(spec.name)
                if identity is None or started_at != identity.started_at:
                    return False
                if (
                    _hash_command(command) != identity.argv_hash
                    or command != _expected_command(spec.argv)
                ):
                    return False
                if spec.health_url is not None:
                    try:
                        response = client.get(spec.health_url)
                        if 200 <= response.status_code < 300:
                            return True
                    except httpx.HTTPError:
                        pass
                elif _log_has_ready_status(self._log_path(spec.name)) or (
                    not spec.require_log_ready_status
                    and time.monotonic() - started >= 0.1
                ):
                    return True
                time.sleep(0.1)
        return False

    def _stop_component(
        self, spec: ProcessSpec, *, timeout_seconds: float = 20.0
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        grace_deadline = time.monotonic() + timeout_seconds / 2
        identity = self._read_identity(spec.name)
        if identity is None:
            return False
        if identity.run_id != self.manifest.run_id:
            raise ProcessIdentityError("run_id_mismatch")
        info = _process_info(identity.pid)
        if info is None:
            child = self._children.get(spec.name)
            if child is not None:
                try:
                    child.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    pass
            return False
        started_at, command = info
        if (
            started_at != identity.started_at
            or _hash_command(command) != identity.argv_hash
        ):
            raise ProcessIdentityError("identity_mismatch")
        if command != _expected_command(spec.argv):
            raise ProcessIdentityError("argv_hash_mismatch")
        try:
            process_group = os.getpgid(identity.pid)
        except ProcessLookupError:
            return False
        if process_group != identity.pid:
            raise ProcessIdentityError("process_group_mismatch")
        try:
            os.killpg(identity.pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        child = self._children.get(spec.name)
        if child is not None:
            try:
                child.wait(timeout=max(0.0, grace_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        if self._wait_for_identity_to_exit(
            identity, max(0.0, grace_deadline - time.monotonic())
        ):
            return True
        current = _process_info(identity.pid)
        if current is None:
            return True
        current_start, current_command = current
        if (
            current_start != identity.started_at
            or _hash_command(current_command) != identity.argv_hash
            or current_command != _expected_command(spec.argv)
        ):
            raise ProcessIdentityError("identity_changed_during_stop")
        try:
            process_group = os.getpgid(identity.pid)
        except ProcessLookupError:
            return True
        if process_group != identity.pid:
            raise ProcessIdentityError("process_group_mismatch")
        try:
            os.killpg(identity.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        remaining = max(0.0, deadline - time.monotonic())
        if child is not None:
            try:
                child.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise SupervisorError(f"component_stop_timeout:{spec.name}") from exc
        elif not self._wait_for_identity_to_exit(identity, remaining):
            raise SupervisorError(f"component_stop_timeout:{spec.name}")
        return True

    def _wait_for_identity_to_exit(
        self, identity: ProcessIdentity, timeout_seconds: float
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            info = _process_info(identity.pid)
            if info is None:
                return True
            started_at, command = info
            if (
                started_at != identity.started_at
                or _hash_command(command) != identity.argv_hash
            ):
                return True
            time.sleep(0.05)
        return _process_info(identity.pid) is None

    def _terminate_unrecorded_child(self, child: subprocess.Popen[bytes]) -> None:
        if child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=2.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            child.wait(timeout=2.0)

    def _ensure_directories(self) -> None:
        root = runtime_root_for(self.manifest)
        if not root.is_absolute():
            raise SupervisorError("run_root_must_be_absolute")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        for name in ("processes", "logs"):
            path = root / name
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)

    def _identity_path(self, component: str) -> Path:
        return runtime_root_for(self.manifest) / "processes" / f"{component}.json"

    def _log_path(self, component: str) -> Path:
        return runtime_root_for(self.manifest) / "logs" / f"{component}.log"

    def _read_identity(self, component: str) -> ProcessIdentity | None:
        path = self._identity_path(component)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcessIdentityError("identity_corrupt") from exc
        try:
            return ProcessIdentity(
                component=str(raw["component"]),
                run_id=UUID(str(raw["run_id"])),
                pid=int(raw["pid"]),
                started_at=str(raw["started_at"]),
                argv_hash=str(raw["argv_hash"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProcessIdentityError("identity_corrupt") from exc

    def _write_identity(self, identity: ProcessIdentity) -> None:
        path = self._identity_path(identity.component)
        payload = {
            "schema_version": _PROCESS_SCHEMA_VERSION,
            "run_id": str(identity.run_id),
            "workspace_id": str(self.manifest.workspace_id),
            "component": identity.component,
            "pid": identity.pid,
            "started_at": identity.started_at,
            "argv_hash": identity.argv_hash,
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(payload, temporary, sort_keys=True, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
            _fsync_directory(path.parent)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            with maintenance_lock(runtime_root_for(self.manifest), owner="demo-supervisor"):
                yield
        except MaintenanceLockBusy as exc:
            raise SupervisorError("maintenance_busy") from exc


def build_demo_process_specs(
    manifest: DemoManifest,
    *,
    inherited_env: Mapping[str, str] | None = None,
    pgpassfile: Path | None = None,
    include_environment: bool = True,
) -> tuple[ProcessSpec, ...]:
    inherited = dict(os.environ if inherited_env is None else inherited_env)
    tokens: DemoTokens | None = (
        load_demo_tokens(runtime_root_for(manifest)) if include_environment else None
    )
    control_python = _python_executable(_CONTROL_PLANE_ROOT)
    knowledge_python = _python_executable(_KNOWLEDGE_ROOT)
    gateway_python = _python_executable(_GATEWAY_ROOT)
    node = shutil.which("node")
    if node is None:
        raise SupervisorError("node_unavailable")

    control_port = _endpoint_port(manifest.endpoints.control_plane)
    knowledge_port = _endpoint_port(manifest.endpoints.knowledge)
    gateway_port = _endpoint_port(manifest.endpoints.main_agent)
    web_port = _endpoint_port(manifest.endpoints.web)

    def env(component: str) -> dict[str, str]:
        if tokens is None:
            return {}
        selected_pgpass = (
            pgpassfile
            if component in {"control-plane", "knowledge-service"}
            else None
        )
        return component_environment(
            manifest,
            component,
            tokens,
            inherited,
            pgpassfile=selected_pgpass,
        )

    specs: list[ProcessSpec] = [
        ProcessSpec(
            name="control-plane",
            argv=(
                str(control_python),
                "-m",
                "uvicorn",
                "sidera_control_plane.api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(control_port),
            ),
            cwd=_CONTROL_PLANE_ROOT,
            explicit_env=env("control-plane"),
            port=control_port,
            health_url=f"{manifest.endpoints.control_plane}/healthz",
        ),
        ProcessSpec(
            name="knowledge-service",
            argv=(
                str(knowledge_python),
                "-m",
                "uvicorn",
                "sidera_knowledge.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(knowledge_port),
            ),
            cwd=_KNOWLEDGE_ROOT,
            explicit_env=env("knowledge-service"),
            port=knowledge_port,
            health_url=f"{manifest.endpoints.knowledge}/health/ready",
        ),
        ProcessSpec(
            name="demo-worker-heartbeat",
            argv=(
                str(control_python),
                "-u",
                "-m",
                "sidera_control_plane.demo.heartbeat",
            ),
            cwd=_CONTROL_PLANE_ROOT,
            explicit_env=env("demo-worker-heartbeat"),
            require_log_ready_status=True,
        ),
    ]

    worker_names = (
        "planning-worker",
        "context-package-worker",
        "knowledge-candidate-worker",
        "activation-worker",
        "scheduler-worker",
        "gateway-worker",
        "plan-transition-worker",
        "collaboration-worker",
        "notification-worker",
        "result-worker",
    )
    specs.extend(
        ProcessSpec(
            name=name,
            argv=(
                str(control_python),
                "-u",
                "-m",
                f"sidera_control_plane.{name.replace('-', '_')}",
            ),
            cwd=_CONTROL_PLANE_ROOT,
            explicit_env=env(name),
            require_log_ready_status=True,
        )
        for name in worker_names
    )
    specs.extend(
        (
            ProcessSpec(
                name="demo-notification-sink",
                argv=(
                    str(control_python),
                    "-u",
                    "-m",
                    "sidera_control_plane.demo.notification_sink",
                    "--manifest",
                    str(runtime_root_for(manifest) / "manifest.json"),
                ),
                cwd=_CONTROL_PLANE_ROOT,
                explicit_env=env("demo-notification-sink"),
                require_log_ready_status=True,
            ),
            ProcessSpec(
                name="main-agent-gateway",
                argv=(
                    str(gateway_python),
                    "-m",
                    "uvicorn",
                    "sidera_main_agent_gateway.api.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(gateway_port),
                ),
                cwd=_GATEWAY_ROOT,
                explicit_env=env("main-agent-gateway"),
                port=gateway_port,
                health_url=(
                    f"{manifest.endpoints.main_agent}/main-agent/v1/health"
                ),
            ),
            ProcessSpec(
                name="web",
                argv=(
                    node,
                    str(_WEB_ROOT / "node_modules" / "vite" / "bin" / "vite.js"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                ),
                cwd=_WEB_ROOT,
                explicit_env=env("web"),
                port=web_port,
                health_url=manifest.endpoints.web,
            ),
        )
    )
    if manifest.restore_mode == "read_only":
        allowed = {
            "control-plane",
            "knowledge-service",
            "main-agent-gateway",
            "web",
        }
        specs = [spec for spec in specs if spec.name in allowed]
    return tuple(specs)


def _python_executable(project_root: Path) -> Path:
    executable = project_root / ".venv" / "bin" / "python"
    if not executable.is_file():
        executable = project_root / ".venv" / "bin" / "python3"
    if not executable.is_file():
        raise SupervisorError(f"project_environment_missing:{project_root.name}")
    return executable


def _endpoint_port(endpoint: str) -> int:
    try:
        port = urlsplit(endpoint).port
    except ValueError as exc:
        raise SupervisorError("manifest_endpoint_invalid") from exc
    if port is None:
        raise SupervisorError("manifest_endpoint_port_missing")
    return port


def write_run_phase(
    manifest: DemoManifest,
    phase: Literal[
        "preparing", "prepared", "starting", "running", "stopping", "stopped", "failed"
    ],
) -> None:
    if phase not in _PROCESS_PHASES:
        raise ValueError("invalid_demo_phase")
    root = runtime_root_for(manifest)
    if not root.is_absolute():
        raise ValueError("run_root_must_be_absolute")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = root / "runtime.json"
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcessIdentityError("runtime_state_corrupt") from exc
        if not isinstance(current, dict) or current.get("run_id") != str(manifest.run_id):
            raise ProcessIdentityError("runtime_run_id_mismatch")
    payload = {
        "schema_version": _PROCESS_SCHEMA_VERSION,
        "run_id": str(manifest.run_id),
        "phase": phase,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=".runtime.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, sort_keys=True, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        _fsync_directory(root)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def read_run_phase(manifest: DemoManifest) -> str:
    path = runtime_root_for(manifest) / "runtime.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "unknown"
    except (OSError, json.JSONDecodeError) as exc:
        raise ProcessIdentityError("runtime_state_corrupt") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != _PROCESS_SCHEMA_VERSION
        or value.get("run_id") != str(manifest.run_id)
        or value.get("phase") not in _PROCESS_PHASES
    ):
        raise ProcessIdentityError("runtime_run_id_mismatch")
    return str(value["phase"])


def _assert_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SupervisorStartError(f"port_in_use:{port}") from exc


def _process_info(pid: int) -> tuple[str, str] | None:
    ps = shutil.which("ps")
    if ps is None:
        raise SupervisorError("ps_unavailable")
    try:
        result = subprocess.run(
            [
                ps,
                "-p",
                str(pid),
                "-o",
                "stat=",
                "-o",
                "lstart=",
                "-o",
                "command=",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SupervisorError("process_identity_timeout") from exc
    line = result.stdout.strip()
    if not line:
        return None
    fields = line.split(maxsplit=6)
    if len(fields) < 7:
        return None
    # macOS keeps a terminated child as a zombie until its parent reaps it.
    # It is no longer a process we can safely manage, so do not compare its
    # placeholder command with the stored identity.
    if fields[0].startswith("Z") or fields[6] == "<defunct>":
        return None
    started_at = " ".join(fields[1:6])
    command = fields[6]
    if not command:
        return None
    return started_at, command


def _expected_command(argv: Sequence[str]) -> str:
    return " ".join(argv)


def _hash_command(command: str) -> str:
    return sha256(command.encode("utf-8")).hexdigest()


def _log_has_ready_status(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines[-50:]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("status") in {"ready", "ok"}:
            return True
        if isinstance(value, dict) and value.get("status") == "error":
            return False
    return False


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
