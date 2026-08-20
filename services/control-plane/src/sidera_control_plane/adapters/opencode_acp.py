import hashlib
import json
import os
import re
import select
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from subprocess import Popen
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.gateway import (
    AdapterEvent,
    AdapterSessionHandle,
    DeliveryOutcome,
    DeliveryResult,
)
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    CapabilityProfile,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
)
from sidera_control_plane.domain.workspace import (
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    ProcessDescriptor,
)


class OpenCodeAdapterError(RuntimeError):
    """The OpenCode adapter could not complete a safe operation."""


class AdapterProbeError(OpenCodeAdapterError):
    """The configured OpenCode process is unavailable or incompatible."""


class ACPTimeoutError(OpenCodeAdapterError):
    """An ACP response did not arrive before the configured deadline."""


class ACPProtocolError(OpenCodeAdapterError):
    """The OpenCode process returned malformed or unsupported ACP data."""


class OpenCodeACPAdapter:
    """WorkerAdapter implementation for one OpenCode ACP child process per session."""

    _PROFILE = CapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.EXPERIMENTAL,
    )

    def __init__(
        self,
        *,
        executable: str = "opencode",
        workspace_root: Path | None = None,
        request_timeout_seconds: float = 30.0,
        stop_grace_seconds: float = 5.0,
        git_executable: str = "git",
        stderr_limit_bytes: int = 64 * 1024,
        client_name: str = "sidera-control-plane",
        client_version: str = "0.1.0",
    ) -> None:
        executable = executable.strip()
        if not executable:
            raise ValueError("OpenCode executable is required")
        if request_timeout_seconds <= 0 or stop_grace_seconds <= 0:
            raise ValueError("OpenCode timeouts must be positive")
        if stderr_limit_bytes <= 0:
            raise ValueError("stderr limit must be positive")
        if not git_executable.strip():
            raise ValueError("Git executable is required")
        self._executable = executable
        self._workspace_root = (
            workspace_root.expanduser().resolve() if workspace_root is not None else None
        )
        self._request_timeout_seconds = request_timeout_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self._git_executable = git_executable.strip()
        self._stderr_limit_bytes = stderr_limit_bytes
        self._client_name = client_name
        self._client_version = client_version
        self._sessions: dict[str, _OpenCodeSession] = {}
        self._lock = threading.RLock()

    def probe(self, config: Mapping[str, object]) -> CapabilityProfile:
        executable = _config_text(config, "executable", self._executable)
        cwd = _config_path(config, "cwd", Path.cwd())
        transport = self._spawn(executable, cwd, probe=True)
        try:
            initialize = transport.request(
                "initialize",
                self._initialize_params(config),
                timeout=self._request_timeout_seconds,
            )
            self._validate_initialize(initialize)
            session = transport.request(
                "session/new",
                {"cwd": str(cwd), "mcpServers": _config_mcp_servers(config)},
                timeout=self._request_timeout_seconds,
            )
            _session(session)
            return self._PROFILE
        except ACPTimeoutError:
            raise
        except (ACPProtocolError, OpenCodeAdapterError) as exc:
            raise AdapterProbeError(f"OpenCode ACP handshake failed: {exc}") from exc
        finally:
            _terminate_owned_process(transport.process)
            transport.close()

    def start_session(
        self,
        spec: ExecutionSpec,
        workspace: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> AdapterSessionHandle:
        del operation_id
        cwd = self._workspace_path(workspace)
        transport = self._spawn(self._executable, cwd, probe=False)
        try:
            initialize = transport.request(
                "initialize",
                self._initialize_params({}),
                timeout=self._request_timeout_seconds,
            )
            self._validate_initialize(initialize)
            result = transport.request(
                "session/new",
                {"cwd": str(cwd), "mcpServers": []},
                timeout=self._request_timeout_seconds,
            )
            adapter_session_id = _session(result)
            identity = _process_identity(transport.process)
            handle = AdapterSessionHandle(
                execution_session_id=uuid4(),
                adapter_session_id=adapter_session_id,
                process_ref=f"opencode://{identity.nonce}",
                capability_snapshot=self._PROFILE,
                last_accepted_sequence=0,
                started_at=identity.started_at,
            )
            with self._lock:
                self._sessions[adapter_session_id] = _OpenCodeSession(
                    handle=handle,
                    transport=transport,
                    identity=identity,
                    cwd=cwd,
                    acceptance_criteria=spec.acceptance_criteria,
                )
            return handle
        except Exception:
            _terminate_owned_process(transport.process)
            transport.close()
            raise

    def read_events(self, handle: AdapterSessionHandle) -> tuple[AdapterEvent, ...]:
        session = self._session(handle)
        with session.lock:
            if not session.stopped:
                session.transport.read_available(timeout=0)
            self._append_notifications(session)
            events = tuple(session.events)
            session.events.clear()
            if session.transport.process.poll() is not None and not session.stopped:
                session.stopped = True
                sequence = session.next_sequence
                session.next_sequence += 1
                events = (*events, self._event(
                    session,
                    "session.failed",
                    sequence,
                    {"failure_code": "process_exited"},
                ))
            return events

    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        if not text.strip():
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.FAILED,
                accepted_sequence=None,
                failure_code="empty_input",
            )
        session = self._session(handle)
        with session.lock:
            if session.stopped or session.transport.process.poll() is not None:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="process_identity_unknown",
                )
            if session.prompt_busy:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code="prompt_busy",
                )
            session.prompt_busy = True
            try:
                result = session.transport.request(
                    "session/prompt",
                    {
                        "sessionId": handle.adapter_session_id,
                        "prompt": _prompt_content(text, attachment_refs),
                    },
                    timeout=self._request_timeout_seconds,
                )
                stop_reason = result.get("stopReason") if isinstance(result, Mapping) else None
                if stop_reason == "cancelled":
                    return DeliveryResult(
                        operation_id=operation_id,
                        outcome=DeliveryOutcome.FAILED,
                        accepted_sequence=None,
                        failure_code="prompt_cancelled",
                    )
                self._append_notifications(session)
                sequence = session.next_sequence
                session.next_sequence += 1
                session.events.append(
                    self._event(
                        session,
                        "input.accepted",
                        sequence,
                        {"text_length": len(text)},
                    )
                )
                handle = _with_last_sequence(handle, sequence)
                session.handle = handle
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.DELIVERED,
                    accepted_sequence=sequence,
                    failure_code=None,
                )
            except ACPTimeoutError:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="acp_response_timeout",
                )
            except ACPProtocolError as exc:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.FAILED,
                    accepted_sequence=None,
                    failure_code=_failure_code(exc),
                )
            finally:
                session.prompt_busy = False

    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        session = self._session(handle)
        with session.lock:
            if session.stopped:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.DELIVERED,
                    accepted_sequence=handle.last_accepted_sequence,
                    failure_code=None,
                )
            if not _identity_matches(session.identity):
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="process_identity_unproven",
                )
            session.transport.notify(
                "session/cancel", {"sessionId": handle.adapter_session_id}
            )
            if not _terminate_verified_process(
                session.transport.process,
                session.identity,
                grace_seconds=self._stop_grace_seconds,
            ):
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="process_stop_unconfirmed",
                )
            session.stopped = True
            sequence = session.next_sequence
            session.next_sequence += 1
            session.events.append(
                self._event(session, "session.canceled", sequence, {})
            )
            session.transport.close()
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.DELIVERED,
                accepted_sequence=sequence,
                failure_code=None,
            )

    def collect_artifacts(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[ArtifactCandidate, ...]:
        del operation_id
        session = self._session(handle)
        with session.lock:
            if session.stopped and session.transport.process.poll() is None:
                raise OpenCodeAdapterError("stopped session has a live process")
            try:
                result = subprocess.run(
                    [
                        self._git_executable,
                        "diff",
                        "--binary",
                        "--no-ext-diff",
                        "--no-color",
                        "--",
                        ".",
                    ],
                    cwd=session.cwd,
                    check=True,
                    capture_output=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise OpenCodeAdapterError(
                    "OpenCode Git diff collection failed: "
                    f"{type(exc).__name__}: {exc}"[:500]
                ) from exc
            diff = result.stdout
            if not diff:
                return ()
            content_hash = hashlib.sha256(diff).hexdigest()
            return (
                ArtifactCandidate(
                    artifact_type=ArtifactType.DIFF,
                    storage_ref=f"artifact://opencode/{handle.execution_session_id}/diff",
                    content_hash=content_hash,
                    size_bytes=len(diff),
                    media_type="text/x-diff",
                    provenance={
                        "producer": self._client_name,
                        "environment_ref": str(session.cwd),
                        "acceptance_criterion_refs": session.acceptance_criteria,
                        "conclusion": "OpenCode produced a Git diff in the execution workspace.",
                    },
                ),
            )

    def active_processes(self) -> tuple[Popen[Any], ...]:
        with self._lock:
            return tuple(
                session.transport.process
                for session in self._sessions.values()
                if session.transport.process.poll() is None
            )

    def _spawn(self, executable: str, cwd: Path, *, probe: bool) -> "_JsonRpcTransport":
        if not cwd.is_dir():
            raise OpenCodeAdapterError(f"OpenCode working directory is unavailable: {cwd}")
        try:
            process = subprocess.Popen(
                [executable, "acp"],
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            message = f"OpenCode executable is unavailable: {executable}"
            if probe:
                raise AdapterProbeError(message) from exc
            raise OpenCodeAdapterError(message) from exc
        except OSError as exc:
            raise OpenCodeAdapterError(f"OpenCode process could not start: {exc}") from exc
        stderr = _BoundedStderr(process.stderr, self._stderr_limit_bytes)
        return _JsonRpcTransport(process, stderr)

    def _workspace_path(self, manifest: ExecutionWorkspaceManifest) -> Path:
        if self._workspace_root is not None:
            path = (self._workspace_root / str(manifest.execution_id)).resolve()
            if self._workspace_root not in path.parents:
                raise OpenCodeAdapterError("workspace location escapes configured root")
        elif manifest.location_ref.startswith("/"):
            path = Path(manifest.location_ref).resolve()
        else:
            raise OpenCodeAdapterError(
                "opaque workspace reference requires an adapter workspace root"
            )
        if not path.is_dir() or path.is_symlink():
            raise OpenCodeAdapterError(f"OpenCode workspace is unavailable: {path}")
        return path

    def _initialize_params(self, config: Mapping[str, object]) -> dict[str, object]:
        return {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {
                "name": self._client_name,
                "version": self._client_version,
            },
            "_meta": {"sidera": {"config": _redacted_config(config)}},
        }

    @staticmethod
    def _validate_initialize(result: Mapping[str, Any]) -> None:
        if result.get("protocolVersion") != 1:
            raise ACPProtocolError("ACP protocol version 1 was not negotiated")
        if not isinstance(result.get("agentCapabilities", {}), Mapping):
            raise ACPProtocolError("ACP initialize response has invalid capabilities")

    def _session(self, handle: AdapterSessionHandle) -> "_OpenCodeSession":
        with self._lock:
            try:
                return self._sessions[handle.adapter_session_id]
            except KeyError as exc:
                raise OpenCodeAdapterError("unknown OpenCode session") from exc

    def _append_notifications(self, session: "_OpenCodeSession") -> None:
        for message in session.transport.drain_notifications():
            event = _notification_event(
                message,
                session_id=session.handle.adapter_session_id,
            )
            if event is None:
                continue
            session.events.append(
                self._event(
                    session,
                    event.event_type,
                    session.next_sequence,
                    event.payload,
                    occurred_at=event.occurred_at,
                )
            )
            session.next_sequence += 1

    @staticmethod
    def _event(
        session: "_OpenCodeSession",
        event_type: str,
        sequence: int,
        payload: Mapping[str, object],
        *,
        occurred_at: datetime | None = None,
    ) -> AdapterEvent:
        return AdapterEvent(
            event_type=event_type,
            sequence=sequence,
            payload=payload,
            occurred_at=occurred_at or datetime.now(UTC),
        )


class _OpenCodeSession:
    def __init__(
        self,
        *,
        handle: AdapterSessionHandle,
        transport: "_JsonRpcTransport",
        identity: ProcessDescriptor,
        cwd: Path,
        acceptance_criteria: tuple[str, ...],
    ) -> None:
        self.handle = handle
        self.transport = transport
        self.identity = identity
        self.cwd = cwd
        self.acceptance_criteria = acceptance_criteria
        self.events: deque[AdapterEvent] = deque()
        self.next_sequence = 1
        self.prompt_busy = False
        self.stopped = False
        self.lock = threading.RLock()


class _JsonRpcTransport:
    def __init__(self, process: Popen[bytes], stderr: "_BoundedStderr") -> None:
        if process.stdin is None or process.stdout is None:
            raise OpenCodeAdapterError("OpenCode ACP pipes are unavailable")
        self.process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr = stderr
        self._next_id = 1
        self._notifications: deque[Mapping[str, Any]] = deque()
        self._read_buffer = bytearray()
        self._lock = threading.RLock()

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            deadline = time.monotonic() + timeout
            while True:
                message = self._read_message(max(0.0, deadline - time.monotonic()))
                if message.get("id") != request_id:
                    self._notifications.append(message)
                    continue
                if "error" in message:
                    raise ACPProtocolError(_rpc_error(message["error"]))
                result = message.get("result")
                if not isinstance(result, Mapping):
                    raise ACPProtocolError(f"ACP {method} response has no result object")
                return result

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def read_available(self, *, timeout: float) -> None:
        with self._lock:
            while True:
                if b"\n" not in self._read_buffer:
                    ready, _, _ = select.select([self._stdout], [], [], timeout)
                    timeout = 0
                    if not ready:
                        return
                try:
                    self._notifications.append(self._read_message(0))
                except ACPTimeoutError:
                    return

    def drain_notifications(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            messages = tuple(self._notifications)
            self._notifications.clear()
            return messages

    def close(self) -> None:
        self._stderr.close()
        try:
            self._stdin.close()
        except OSError:
            pass
        try:
            self._stdout.close()
        except OSError:
            pass

    def _write(self, message: Mapping[str, object]) -> None:
        try:
            self._stdin.write(
                (json.dumps(message, separators=(",", ":")) + "\n").encode()
            )
            self._stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise OpenCodeAdapterError("OpenCode ACP stdin is closed") from exc

    def _read_message(self, timeout: float) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout
        while b"\n" not in self._read_buffer:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self._stdout], [], [], remaining)
            if not ready:
                raise ACPTimeoutError("OpenCode ACP response timed out")
            chunk = os.read(self._stdout.fileno(), 4096)
            if not chunk:
                raise OpenCodeAdapterError("OpenCode ACP stdout closed")
            self._read_buffer.extend(chunk)
        line, _, remainder = self._read_buffer.partition(b"\n")
        self._read_buffer = bytearray(remainder)
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ACPProtocolError("OpenCode ACP emitted invalid JSON") from exc
        if not isinstance(message, Mapping):
            raise ACPProtocolError("OpenCode ACP message must be an object")
        return message


class _BoundedStderr:
    def __init__(self, stream: Any, limit_bytes: int) -> None:
        self._stream = stream
        self._limit_bytes = limit_bytes
        self._buffer = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._stream.close()
        except OSError:
            pass

    def _read(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._stream.read(4096)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            encoded = _redact_stderr(chunk).encode("utf-8", errors="replace")
            remaining = self._limit_bytes - len(self._buffer)
            if remaining > 0:
                self._buffer.extend(encoded[:remaining])


def _config_text(config: Mapping[str, object], key: str, default: str) -> str:
    for candidate in (key, f"opencode.{key}"):
        value = config.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _config_path(config: Mapping[str, object], key: str, default: Path) -> Path:
    value = _config_text(config, key, str(default))
    return Path(value).expanduser().resolve()


def _config_mcp_servers(config: Mapping[str, object]) -> list[object]:
    value = config.get("mcp_servers", config.get("opencode.mcp_servers", []))
    if isinstance(value, list):
        return value
    return []


def _redacted_config(config: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): "[redacted]" if _SECRET_KEY.search(str(key)) else value
        for key, value in config.items()
    }


def _prompt_content(text: str, attachment_refs: tuple[str, ...]) -> list[dict[str, object]]:
    content: list[dict[str, object]] = [{"type": "text", "text": text}]
    content.extend({"type": "resource_link", "uri": ref} for ref in attachment_refs)
    return content


def _session(result: Mapping[str, Any]) -> str:
    value = result.get("sessionId")
    if not isinstance(value, str) or not value.strip():
        raise ACPProtocolError("ACP session/new response has no sessionId")
    return value


def _with_last_sequence(
    handle: AdapterSessionHandle, sequence: int
) -> AdapterSessionHandle:
    return AdapterSessionHandle(
        execution_session_id=handle.execution_session_id,
        adapter_session_id=handle.adapter_session_id,
        process_ref=handle.process_ref,
        capability_snapshot=handle.capability_snapshot,
        last_accepted_sequence=sequence,
        started_at=handle.started_at,
    )


def _notification_event(
    message: Mapping[str, Any], *, session_id: str
) -> AdapterEvent | None:
    if message.get("method") != "session/update":
        return None
    params = message.get("params")
    if not isinstance(params, Mapping) or params.get("sessionId") != session_id:
        return None
    update = params.get("update")
    if not isinstance(update, Mapping):
        return None
    update_type = str(update.get("sessionUpdate", "unknown"))
    event_type = {
        "usage_update": "session.heartbeat",
        "plan": "progress.updated",
        "agent_message_chunk": "progress.updated",
        "tool_call": "progress.updated",
        "tool_call_update": "progress.updated",
    }.get(update_type, "progress.updated")
    return AdapterEvent(
        event_type=event_type,
        sequence=1,
        payload=dict(update),
        occurred_at=datetime.now(UTC),
    )


def _process_identity(process: Popen[Any]) -> ProcessDescriptor:
    try:
        process_group_id = os.getpgid(process.pid)
    except OSError as exc:
        raise OpenCodeAdapterError("OpenCode process group identity is unavailable") from exc
    return ProcessDescriptor(
        pid=process.pid,
        started_at=datetime.now(UTC),
        nonce=uuid4().hex,
        process_group_id=process_group_id,
    )


def _identity_matches(identity: ProcessDescriptor) -> bool:
    try:
        os.kill(identity.pid, 0)
        if os.getpgid(identity.pid) != identity.process_group_id:
            return False
    except OSError:
        return False
    observed = _observed_process_start(identity.pid)
    if observed is None:
        return False
    return abs((observed - identity.started_at).total_seconds()) <= 3


def _observed_process_start(pid: int) -> datetime | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b  %d %H:%M:%S %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=datetime.now().astimezone().tzinfo)
        except ValueError:
            continue
    return None


def _terminate_verified_process(
    process: Popen[Any],
    identity: ProcessDescriptor,
    *,
    grace_seconds: float,
) -> bool:
    if process.poll() is not None:
        return True
    if not _identity_matches(identity):
        return False
    try:
        os.killpg(identity.process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return process.poll() is not None
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is not None:
        return True
    if not _identity_matches(identity):
        return False
    try:
        os.killpg(identity.process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return process.poll() is not None
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        return False
    return process.poll() is not None


def _terminate_owned_process(process: Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _rpc_error(error: object) -> str:
    if isinstance(error, Mapping):
        code = error.get("code", "unknown")
        message = error.get("message", "ACP request failed")
        return f"{code}: {message}"
    return "ACP request failed"


def _failure_code(error: Exception) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(error).lower()).strip("_")
    return value[:100] or "acp_error"


_SECRET_KEY = re.compile(r"(key|token|secret|password|credential)", re.IGNORECASE)
_SECRET_VALUE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)\s*([=:])\s*([^\s,;]+)"
)


def _redact_stderr(value: str) -> str:
    return _SECRET_VALUE.sub(r"\1\2[redacted]", value)
