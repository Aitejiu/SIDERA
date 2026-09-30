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
from collections.abc import Callable, Mapping
from pathlib import Path
from subprocess import Popen
from typing import Any

ServerRequestHandler = Callable[
    [Mapping[str, Any]], Mapping[str, object] | None
]


class ACPProcessError(RuntimeError):
    """An owned ACP process could not complete a transport operation."""


class ACPProcessTimeoutError(ACPProcessError):
    """An ACP response did not arrive before its deadline."""

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        timeout_seconds: float | None = None,
        elapsed_ms: int | None = None,
        process_exited: bool | None = None,
    ) -> None:
        self.method = method
        self.timeout_seconds = timeout_seconds
        self.elapsed_ms = elapsed_ms
        self.process_exited = process_exited
        super().__init__(message)


class ACPProcessProtocolError(ACPProcessError):
    """An ACP process emitted an invalid JSON-RPC message."""


def _safe_error_classification(
    error_data: object,
) -> tuple[str | None, str | None]:
    if not isinstance(error_data, Mapping):
        return None, None
    service = error_data.get("service")
    error_name = error_data.get("errorName")
    return (
        "session" if isinstance(service, str) and service == "session" else None,
        error_name
        if isinstance(error_name, str)
        and error_name in ("provider.invalid-request", "APIError")
        else None,
    )


class ACPRemoteError(ACPProcessProtocolError):
    """A matching ACP JSON-RPC request returned a remote error."""

    def __init__(
        self,
        method: str,
        code: int,
        *,
        error_data: object = None,
    ) -> None:
        self._method = method
        self._code = code
        self._service, self._error_name = _safe_error_classification(error_data)
        super().__init__(f"ACP {method} returned JSON-RPC error code {code}")

    @property
    def method(self) -> str:
        return self._method

    @property
    def code(self) -> int:
        return self._code

    @property
    def service(self) -> str | None:
        return self._service

    @property
    def error_name(self) -> str | None:
        return self._error_name


class ACPProcessTransport:
    """Small JSON-RPC transport for one owned ACP child process."""

    def __init__(
        self,
        process: Popen[bytes],
        stderr: _BoundedStderr,
        *,
        server_request_handler: ServerRequestHandler | None = None,
        server_request_timeout_seconds: float = 5.0,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise ACPProcessError("ACP process pipes are unavailable")
        if server_request_timeout_seconds <= 0:
            raise ValueError("ACP server request timeout must be positive")
        self.process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr = stderr
        self._process_group_id = _process_group_id(process)
        self._next_id = 1
        self._notifications: deque[Mapping[str, Any]] = deque()
        self._read_buffer = bytearray()
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._server_request_handler = server_request_handler
        self._server_request_timeout_seconds = server_request_timeout_seconds

    @classmethod
    def spawn(
        cls,
        executable: str,
        *,
        cwd: Path,
        stderr_limit_bytes: int = 64 * 1024,
        env: Mapping[str, str] | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        server_request_timeout_seconds: float = 5.0,
    ) -> ACPProcessTransport:
        executable = executable.strip()
        if not executable:
            raise ACPProcessError("ACP executable is required")
        merged_env = None if env is None else {**os.environ, **dict(env)}
        return cls._spawn_process(
            (executable, "acp"),
            cwd=cwd,
            stderr_limit_bytes=stderr_limit_bytes,
            env=merged_env,
            server_request_handler=server_request_handler,
            server_request_timeout_seconds=server_request_timeout_seconds,
        )

    @classmethod
    def spawn_argv(
        cls,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stderr_limit_bytes: int = 64 * 1024,
        server_request_handler: ServerRequestHandler | None = None,
        server_request_timeout_seconds: float = 5.0,
    ) -> ACPProcessTransport:
        """Start an ACP child with an explicit argv and exact environment.

        This is the boundary used by adapters that are not tied to an
        executable's historical ``acp`` subcommand.  In particular, ``env``
        replaces the parent environment instead of extending it.
        """

        if not argv or any(not isinstance(part, str) or not part for part in argv):
            raise ACPProcessError("ACP argv must contain non-empty strings")
        return cls._spawn_process(
            argv,
            cwd=cwd,
            stderr_limit_bytes=stderr_limit_bytes,
            env=dict(env),
            server_request_handler=server_request_handler,
            server_request_timeout_seconds=server_request_timeout_seconds,
        )

    @classmethod
    def _spawn_process(
        cls,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        stderr_limit_bytes: int,
        env: Mapping[str, str] | None,
        server_request_handler: ServerRequestHandler | None,
        server_request_timeout_seconds: float,
    ) -> ACPProcessTransport:
        if not cwd.is_dir():
            raise ACPProcessError(f"ACP working directory is unavailable: {cwd}")
        if stderr_limit_bytes <= 0:
            raise ValueError("ACP stderr limit must be positive")
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
                env=(None if env is None else dict(env)),
            )
        except FileNotFoundError as exc:
            raise ACPProcessError(
                f"ACP executable is unavailable: {argv[0]}"
            ) from exc
        except OSError as exc:
            raise ACPProcessError(f"ACP process could not start: {type(exc).__name__}") from exc
        try:
            return cls(
                process,
                _BoundedStderr(process.stderr, stderr_limit_bytes),
                server_request_handler=server_request_handler,
                server_request_timeout_seconds=server_request_timeout_seconds,
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
        with self._request_lock:
            with self._lock:
                request_id = self._next_id
                self._next_id += 1
                self._write(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
            started_at = time.monotonic()
            deadline = started_at + timeout
            while True:
                try:
                    message = self._read_message(
                        max(0.0, deadline - time.monotonic())
                    )
                except ACPProcessTimeoutError:
                    raise ACPProcessTimeoutError(
                        "ACP response timed out",
                        method=method,
                        timeout_seconds=timeout,
                        elapsed_ms=max(
                            0, int((time.monotonic() - started_at) * 1000)
                        ),
                        process_exited=self.process.poll() is not None,
                    ) from None
                # A server request can legally reuse a numeric id.  The
                # presence of method makes it a request, never our response.
                if isinstance(message.get("method"), str):
                    self._handle_server_request(message)
                    continue
                if message.get("id") != request_id:
                    with self._lock:
                        self._notifications.append(message)
                    continue
                if "error" in message:
                    error = message["error"]
                    code = error.get("code") if isinstance(error, Mapping) else None
                    if type(code) is not int:
                        raise ACPProcessProtocolError(
                            "ACP returned an invalid JSON-RPC error"
                        )
                    raise ACPRemoteError(
                        method,
                        code,
                        error_data=error.get("data"),
                    )
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
        # A request owns the single reader while it waits for its response.
        # Observation is best-effort and must never queue behind that request;
        # notifications already buffered by the request remain drainable.
        if not self._request_lock.acquire(blocking=False):
            return
        try:
            while True:
                if b"\n" not in self._read_buffer:
                    ready, _, _ = select.select([self._stdout], [], [], timeout)
                    timeout = 0
                    if not ready:
                        return
                try:
                    message = self._read_message(0)
                except ACPProcessTimeoutError:
                    return
                if isinstance(message.get("method"), str):
                    self._handle_server_request(message)
                else:
                    with self._lock:
                        self._notifications.append(message)
        finally:
            self._request_lock.release()

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

    def _handle_server_request(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        if request_id is None:
            # Notifications have no response frame.  There is no safe way to
            # invent an acknowledgement for them, so keep them observable.
            with self._lock:
                self._notifications.append(message)
            return

        result: Mapping[str, object] | None
        error: Mapping[str, object] | None = None
        if self._server_request_handler is None:
            if message.get("method") == "session/request_permission":
                result = {"outcome": "reject"}
            else:
                result = None
                error = {
                    "code": -32601,
                    "message": "unsupported ACP server request",
                }
        else:
            result, error = self._invoke_server_request_handler(message)

        response: dict[str, object] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = dict(result or {})
        with self._lock:
            self._write(response)

    def _invoke_server_request_handler(
        self, message: Mapping[str, Any]
    ) -> tuple[Mapping[str, object] | None, Mapping[str, object] | None]:
        result: list[Mapping[str, object] | None] = [None]
        failure: list[BaseException] = []

        def invoke() -> None:
            try:
                result[0] = self._server_request_handler(message)  # type: ignore[misc]
            except BaseException as exc:  # noqa: BLE001 - convert to safe JSON-RPC error
                failure.append(exc)

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        thread.join(self._server_request_timeout_seconds)
        if thread.is_alive():
            return None, {
                "code": -32001,
                "message": "ACP server request timed out",
            }
        if failure:
            return None, {
                "code": -32603,
                "message": "ACP server request handler failed",
            }
        if result[0] is not None and not isinstance(result[0], Mapping):
            return None, {
                "code": -32603,
                "message": "ACP server request handler returned an invalid result",
            }
        return result[0], None

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
