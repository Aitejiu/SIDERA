from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..contracts import ProcessIdentity

_MAX_FRAME_BYTES = 2 * 1024 * 1024
_DEFAULT_STDERR_LIMIT = 64 * 1024


class ACPTransportError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _BoundedStderr:
    def __init__(self, stream, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self._data = bytearray()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while True:
            try:
                chunk = self._stream.read(4096)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                remaining = self._limit - len(self._data)
                if remaining > 0:
                    self._data.extend(chunk[:remaining])

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError:
            pass
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=0.2)


class ACPProcessTransport:
    """JSON-RPC transport for one directly owned ACP process group."""

    def __init__(
        self, process: subprocess.Popen[bytes], stderr_limit_bytes: int
    ) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise ACPTransportError("acp_pipes_unavailable")
        self.process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr = _BoundedStderr(process.stderr, stderr_limit_bytes)
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._notification_lock = threading.Lock()
        self._notifications: deque[Mapping[str, Any]] = deque()
        self._read_buffer = bytearray()
        self._next_id = 1
        self._closed = False
        try:
            self.identity = ProcessIdentity(
                pid=process.pid,
                start_identity=_process_start_identity(process.pid),
                owned_nonce=uuid4(),
            )
            self._process_group_id = (
                os.getpgid(process.pid) if os.name == "posix" else None
            )
        except (OSError, ValueError, ACPTransportError):
            self.terminate(grace_seconds=0.1)
            self.close()
            raise ACPTransportError("acp_identity_unavailable") from None

    @classmethod
    def spawn_argv(
        cls,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stderr_limit_bytes: int = _DEFAULT_STDERR_LIMIT,
    ) -> ACPProcessTransport:
        if (
            not argv
            or any(
                not isinstance(part, str) or not part or "\x00" in part for part in argv
            )
            or stderr_limit_bytes <= 0
            or not cwd.is_dir()
        ):
            raise ACPTransportError("acp_spawn_configuration_invalid")
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                close_fds=True,
                shell=False,
                start_new_session=os.name == "posix",
            )
        except OSError:
            raise ACPTransportError("acp_spawn_failed") from None
        try:
            return cls(process, stderr_limit_bytes)
        except Exception:
            if process.poll() is None:
                _terminate_process_group(process, 0.1)
            raise

    def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> Mapping[str, Any]:
        if timeout <= 0 or not method:
            raise ACPTransportError("acp_request_invalid")
        with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            deadline = time.monotonic() + timeout
            while True:
                message = self._read_message(max(0.0, deadline - time.monotonic()))
                if isinstance(message.get("method"), str):
                    self._handle_incoming_method(message)
                    continue
                if message.get("id") != request_id:
                    with self._notification_lock:
                        self._notifications.append(message)
                    continue
                error = message.get("error")
                if error is not None:
                    code = error.get("code") if isinstance(error, Mapping) else None
                    reason = (
                        f"acp_remote_error_{code}"
                        if type(code) is int
                        else "acp_protocol_invalid"
                    )
                    raise ACPTransportError(reason)
                result = message.get("result")
                if not isinstance(result, Mapping):
                    raise ACPTransportError("acp_protocol_invalid")
                return result

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        if not method:
            raise ACPTransportError("acp_request_invalid")
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def read_available(self, *, timeout: float = 0.0) -> None:
        if timeout < 0 or not self._request_lock.acquire(blocking=False):
            return
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self._stdout, selectors.EVENT_READ)
                while True:
                    if selector.select(timeout):
                        try:
                            message = self._read_message(0.0)
                        except ACPTransportError as error:
                            if error.reason == "acp_timeout":
                                return
                            raise
                        if isinstance(message.get("method"), str):
                            self._handle_incoming_method(message)
                        else:
                            with self._notification_lock:
                                self._notifications.append(message)
                    else:
                        return
                    timeout = 0.0
        finally:
            self._request_lock.release()

    def drain_notifications(self) -> tuple[Mapping[str, Any], ...]:
        with self._notification_lock:
            messages = tuple(self._notifications)
            self._notifications.clear()
            return messages

    def terminate(self, *, grace_seconds: float) -> bool:
        if grace_seconds <= 0:
            raise ValueError("termination grace must be positive")
        if self.process.poll() is None:
            _signal_process_group(self.process, self._process_group_id, signal.SIGTERM)
            try:
                self.process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                _signal_process_group(
                    self.process, self._process_group_id, signal.SIGKILL
                )
                try:
                    self.process.wait(timeout=max(0.2, grace_seconds))
                except subprocess.TimeoutExpired:
                    return False
        if os.name == "posix" and self._process_group_id is not None:
            try:
                os.killpg(self._process_group_id, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            _signal_process_group(self.process, self._process_group_id, signal.SIGKILL)
        return self.process.poll() is not None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stderr.close()
        for stream in (self._stdin, self._stdout):
            try:
                stream.close()
            except OSError:
                pass

    def identity_matches(self, identity: ProcessIdentity) -> bool:
        if identity != self.identity or self.process.poll() is not None:
            return False
        try:
            if (
                os.name == "posix"
                and os.getpgid(identity.pid) != self._process_group_id
            ):
                return False
            return _process_start_identity(identity.pid) == identity.start_identity
        except (OSError, ACPTransportError):
            return False

    def _write(self, message: Mapping[str, object]) -> None:
        try:
            encoded = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ACPTransportError("acp_request_invalid") from None
        if len(encoded) > _MAX_FRAME_BYTES:
            raise ACPTransportError("acp_frame_too_large")
        with self._write_lock:
            try:
                self._stdin.write(encoded)
                self._stdin.flush()
            except (BrokenPipeError, OSError):
                raise ACPTransportError("acp_process_closed") from None

    def _read_message(self, timeout: float) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            newline = self._read_buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._read_buffer[:newline])
                del self._read_buffer[: newline + 1]
                try:
                    message = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ACPTransportError("acp_protocol_invalid") from None
                if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
                    raise ACPTransportError("acp_protocol_invalid")
                return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ACPTransportError("acp_timeout")
            if self.process.poll() is not None:
                raise ACPTransportError("acp_process_closed")
            with selectors.DefaultSelector() as selector:
                selector.register(self._stdout, selectors.EVENT_READ)
                if not selector.select(remaining):
                    raise ACPTransportError("acp_timeout")
            try:
                chunk = os.read(self._stdout.fileno(), 4096)
            except OSError:
                raise ACPTransportError("acp_process_closed") from None
            if not chunk:
                raise ACPTransportError("acp_process_closed")
            self._read_buffer.extend(chunk)
            if len(self._read_buffer) > _MAX_FRAME_BYTES:
                raise ACPTransportError("acp_frame_too_large")

    def _handle_incoming_method(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        if request_id is None:
            with self._notification_lock:
                self._notifications.append(message)
            return
        response: dict[str, object] = {"jsonrpc": "2.0", "id": request_id}
        if message.get("method") == "session/request_permission":
            response["result"] = {"outcome": "reject"}
        else:
            response["error"] = {
                "code": -32601,
                "message": "unsupported ACP server request",
            }
        self._write(response)


def _process_start_identity(pid: int) -> str:
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            fields = proc_stat.read_text(encoding="ascii").rsplit(")", 1)[1].split()
            return f"linux:{fields[19]}"
        except (OSError, IndexError):
            raise ACPTransportError("acp_identity_unavailable") from None
    try:
        result = subprocess.run(
            ("ps", "-p", str(pid), "-o", "lstart="),
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
            env={"PATH": os.defpath, "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        raise ACPTransportError("acp_identity_unavailable") from None
    value = result.stdout.strip()
    if not value:
        raise ACPTransportError("acp_identity_unavailable")
    return f"ps:{value}"


def _signal_process_group(
    process: subprocess.Popen[bytes], group_id: int | None, sig: signal.Signals
) -> None:
    if os.name == "posix" and group_id is not None:
        try:
            os.killpg(group_id, sig)
            return
        except ProcessLookupError:
            return
    if process.poll() is None:
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass


def _terminate_process_group(
    process: subprocess.Popen[bytes], grace_seconds: float
) -> bool:
    group_id = process.pid if os.name == "posix" else None
    _signal_process_group(process, group_id, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, group_id, signal.SIGKILL)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None
