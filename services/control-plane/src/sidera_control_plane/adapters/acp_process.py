from __future__ import annotations

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
from pathlib import Path
from subprocess import Popen
from typing import Any


class ACPProcessError(RuntimeError):
    """An owned ACP process could not complete a transport operation."""


class ACPProcessTimeoutError(ACPProcessError):
    """An ACP response did not arrive before its deadline."""


class ACPProcessProtocolError(ACPProcessError):
    """An ACP process emitted an invalid JSON-RPC message."""


class ACPProcessTransport:
    """Small JSON-RPC transport for one owned ACP child process."""

    def __init__(self, process: Popen[bytes], stderr: _BoundedStderr) -> None:
        if process.stdin is None or process.stdout is None:
            raise ACPProcessError("ACP process pipes are unavailable")
        self.process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr = stderr
        self._process_group_id = _process_group_id(process)
        self._next_id = 1
        self._notifications: deque[Mapping[str, Any]] = deque()
        self._read_buffer = bytearray()
        self._lock = threading.RLock()

    @classmethod
    def spawn(
        cls,
        executable: str,
        *,
        cwd: Path,
        stderr_limit_bytes: int = 64 * 1024,
        env: Mapping[str, str] | None = None,
    ) -> ACPProcessTransport:
        executable = executable.strip()
        if not executable:
            raise ACPProcessError("ACP executable is required")
        if not cwd.is_dir():
            raise ACPProcessError(f"ACP working directory is unavailable: {cwd}")
        if stderr_limit_bytes <= 0:
            raise ValueError("ACP stderr limit must be positive")
        try:
            process = subprocess.Popen(
                [executable, "acp"],
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
                env=(None if env is None else {**os.environ, **dict(env)}),
            )
        except FileNotFoundError as exc:
            raise ACPProcessError(
                f"ACP executable is unavailable: {executable}"
            ) from exc
        except OSError as exc:
            raise ACPProcessError(f"ACP process could not start: {type(exc).__name__}") from exc
        try:
            return cls(
                process,
                _BoundedStderr(process.stderr, stderr_limit_bytes),
            )
        except Exception:
            _terminate_process_group(process, _process_group_id(process), 0.2)
            raise

    @property
    def stderr_text(self) -> str:
        return self._stderr.text

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, Any]:
        if timeout <= 0:
            raise ValueError("ACP request timeout must be positive")
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
                    raise ACPProcessProtocolError("ACP returned a JSON-RPC error")
                result = message.get("result")
                if not isinstance(result, Mapping):
                    raise ACPProcessProtocolError(
                        f"ACP {method} response has no result object"
                    )
                return result

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def read_available(self, *, timeout: float) -> None:
        if timeout < 0:
            raise ValueError("ACP read timeout cannot be negative")
        with self._lock:
            while True:
                if b"\n" not in self._read_buffer:
                    ready, _, _ = select.select([self._stdout], [], [], timeout)
                    timeout = 0
                    if not ready:
                        return
                try:
                    self._notifications.append(self._read_message(0))
                except ACPProcessTimeoutError:
                    return

    def drain_notifications(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            messages = tuple(self._notifications)
            self._notifications.clear()
            return messages

    def terminate(self, *, grace_seconds: float) -> bool:
        if grace_seconds <= 0:
            raise ValueError("ACP termination grace must be positive")
        if self.process.poll() is not None:
            return True
        return _terminate_process_group(
            self.process,
            self._process_group_id,
            grace_seconds,
        )

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
            raise ACPProcessError("ACP process stdin is closed") from exc

    def _read_message(self, timeout: float) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout
        while b"\n" not in self._read_buffer:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self._stdout], [], [], remaining)
            if not ready:
                raise ACPProcessTimeoutError("ACP response timed out")
            chunk = os.read(self._stdout.fileno(), 4096)
            if not chunk:
                raise ACPProcessError("ACP stdout closed")
            self._read_buffer.extend(chunk)
        line, _, remainder = self._read_buffer.partition(b"\n")
        self._read_buffer = bytearray(remainder)
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ACPProcessProtocolError("ACP emitted invalid JSON") from exc
        if not isinstance(message, Mapping):
            raise ACPProcessProtocolError("ACP message must be an object")
        return message


class _BoundedStderr:
    def __init__(self, stream: Any, limit_bytes: int) -> None:
        self._stream = stream
        self._limit_bytes = limit_bytes
        self._buffer = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    @property
    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")

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
            encoded = _redact_stderr(str(chunk)).encode("utf-8", errors="replace")
            remaining = self._limit_bytes - len(self._buffer)
            if remaining > 0:
                self._buffer.extend(encoded[:remaining])


def _process_group_id(process: Popen[Any]) -> int:
    try:
        return os.getpgid(process.pid)
    except OSError as exc:
        raise ACPProcessError("ACP process group identity is unavailable") from exc


def _terminate_process_group(
    process: Popen[Any], process_group_id: int, grace_seconds: float
) -> bool:
    if process.poll() is not None:
        return True
    try:
        if os.getpgid(process.pid) != process_group_id:
            return False
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return process.poll() is not None
    except OSError:
        return False
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is not None:
        return True
    try:
        if os.getpgid(process.pid) != process_group_id:
            return False
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return process.poll() is not None
    except OSError:
        return False
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        return False
    return process.poll() is not None


_SECRET_VALUE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)\s*([=:])\s*([^\s,;]+)"
)


def _redact_stderr(value: str) -> str:
    return _SECRET_VALUE.sub(r"\1\2[redacted]", value)
