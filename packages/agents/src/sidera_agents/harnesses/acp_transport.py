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

from ..contracts import NativeProcessState, ProcessIdentity

_MAX_FRAME_BYTES = 2 * 1024 * 1024
_DEFAULT_STDERR_LIMIT = 64 * 1024
_DEFAULT_WRITE_TIMEOUT_SECONDS = 30.0


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
        self._pending_requests: dict[int, str] = {}
        self._late_responses: deque[tuple[str, Mapping[str, Any]]] = deque()
        self._read_buffer = bytearray()
        self._next_id = 1
        self._closed = False
        self._broken = False
        try:
            self.identity = ProcessIdentity(
                pid=process.pid,
                start_identity=_process_start_identity(process.pid),
                owned_nonce=uuid4(),
            )
            self._process_group_id = (
                os.getpgid(process.pid) if os.name == "posix" else None
            )
            os.set_blocking(self._stdin.fileno(), False)
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
        deadline = time.monotonic() + timeout
        if not self._request_lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            raise ACPTransportError("acp_timeout")
        try:
            if self._broken or self._closed:
                raise ACPTransportError("acp_transport_unusable")
            request_id = self._next_id
            self._next_id += 1
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                deadline=deadline,
            )
            with self._notification_lock:
                self._pending_requests[request_id] = method
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ACPTransportError("acp_timeout")
                message = self._read_message(remaining, deadline=deadline)
                if time.monotonic() >= deadline:
                    if not isinstance(message.get("method"), str):
                        self._route_response(message)
                    elif message.get("id") is None:
                        self._handle_incoming_method(message)
                    raise ACPTransportError("acp_timeout")
                if isinstance(message.get("method"), str):
                    self._handle_incoming_method(message, deadline=deadline)
                    continue
                if message.get("id") != request_id:
                    self._route_response(message)
                    continue
                with self._notification_lock:
                    self._pending_requests.pop(request_id, None)
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
        finally:
            self._request_lock.release()

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        if not method:
            raise ACPTransportError("acp_request_invalid")
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def read_available(self, *, timeout: float = 0.0) -> None:
        if timeout < 0 or not self._request_lock.acquire(blocking=False):
            return
        try:
            wait_timeout = timeout
            while True:
                try:
                    message = self._read_message(wait_timeout)
                except ACPTransportError as error:
                    if error.reason == "acp_timeout":
                        return
                    raise
                if isinstance(message.get("method"), str):
                    self._handle_incoming_method(message)
                else:
                    self._route_response(message)
                wait_timeout = 0.0
        finally:
            self._request_lock.release()

    def drain_notifications(self) -> tuple[Mapping[str, Any], ...]:
        with self._notification_lock:
            messages = tuple(self._notifications)
            self._notifications.clear()
            return messages

    def drain_late_responses(
        self,
    ) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        with self._notification_lock:
            messages = tuple(self._late_responses)
            self._late_responses.clear()
            return messages

    def terminate(self, *, grace_seconds: float) -> bool:
        if grace_seconds <= 0:
            raise ValueError("termination grace must be positive")
        state = self.identity_state(self.identity)
        if state is NativeProcessState.UNKNOWN:
            return False
        if os.name != "posix":
            return self._terminate_process(grace_seconds, state)
        group_id = self._process_group_id
        if group_id is None:
            return False
        group_exists = _process_group_exists(group_id)
        if group_exists is None:
            return False
        if not group_exists:
            self.process.poll()
            return True
        if state is NativeProcessState.GONE:
            return False
        if not self._signal_owned_group(signal.SIGTERM):
            return False

        deadline = time.monotonic() + grace_seconds
        while True:
            group_exists = _process_group_exists(group_id)
            if group_exists is False:
                self.process.poll()
                return True
            if group_exists is None:
                self.process.poll()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.01, remaining))
                continue
            state = self.identity_state(self.identity)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if state is not NativeProcessState.ALIVE:
                    return False
                if not self._signal_owned_group(signal.SIGKILL):
                    return False
                kill_deadline = time.monotonic() + max(0.2, grace_seconds)
                while time.monotonic() < kill_deadline:
                    group_exists = _process_group_exists(group_id)
                    if group_exists is False:
                        self.process.poll()
                        return True
                    if group_exists is None:
                        self.process.poll()
                        time.sleep(0.01)
                        continue
                    time.sleep(min(0.01, kill_deadline - time.monotonic()))
                if _process_group_exists(group_id) is False:
                    self.process.poll()
                    return True
                return False
            time.sleep(min(0.01, remaining))

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

    def identity_state(self, identity: ProcessIdentity) -> NativeProcessState:
        if identity != self.identity:
            return NativeProcessState.GONE
        if self.process.poll() is not None:
            if os.name != "posix":
                return NativeProcessState.GONE
            if self._process_group_id is None:
                return NativeProcessState.UNKNOWN
            group_exists = _process_group_exists(self._process_group_id)
            return (
                NativeProcessState.GONE
                if group_exists is False
                else NativeProcessState.UNKNOWN
            )
        try:
            if os.name == "posix":
                current_group = os.getpgid(identity.pid)
                if current_group != self._process_group_id:
                    return NativeProcessState.GONE
            current_start = _process_start_identity(identity.pid)
        except ProcessLookupError:
            return NativeProcessState.GONE
        except (OSError, ACPTransportError):
            return NativeProcessState.UNKNOWN
        return (
            NativeProcessState.ALIVE
            if current_start == identity.start_identity
            else NativeProcessState.GONE
        )

    def identity_matches(self, identity: ProcessIdentity) -> bool:
        return self.identity_state(identity) is NativeProcessState.ALIVE

    def _signal_owned_group(self, sig: signal.Signals) -> bool:
        if self.identity_state(self.identity) is not NativeProcessState.ALIVE:
            return False
        try:
            _signal_process_group(self.process, self._process_group_id, sig)
        except OSError:
            return False
        return True

    def _terminate_process(
        self, grace_seconds: float, state: NativeProcessState
    ) -> bool:
        if state is NativeProcessState.GONE:
            return self.process.poll() is not None
        if self.identity_state(self.identity) is not NativeProcessState.ALIVE:
            return False
        try:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            if self.identity_state(self.identity) is not NativeProcessState.ALIVE:
                return False
            try:
                self.process.send_signal(signal.SIGKILL)
                self.process.wait(timeout=max(0.2, grace_seconds))
            except (OSError, subprocess.TimeoutExpired):
                return False
        except OSError:
            return False
        return self.process.poll() is not None

    def _route_response(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        with self._notification_lock:
            method = (
                self._pending_requests.pop(request_id, None)
                if type(request_id) in {int, str}
                else None
            )
            if method is None:
                self._notifications.append(message)
            else:
                self._late_responses.append((method, message))

    def _write(
        self, message: Mapping[str, object], *, deadline: float | None = None
    ) -> None:
        if deadline is None:
            deadline = time.monotonic() + _DEFAULT_WRITE_TIMEOUT_SECONDS
        try:
            encoded = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ACPTransportError("acp_request_invalid") from None
        if len(encoded) > _MAX_FRAME_BYTES:
            raise ACPTransportError("acp_frame_too_large")
        if not self._write_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise ACPTransportError("acp_timeout")
        written = 0
        try:
            if self._broken or self._closed:
                raise ACPTransportError("acp_transport_unusable")
            with selectors.DefaultSelector() as selector:
                selector.register(self._stdin, selectors.EVENT_WRITE)
                while written < len(encoded):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if written:
                            self._poison_write_stream()
                        raise ACPTransportError("acp_timeout")
                    if not selector.select(remaining):
                        if written:
                            self._poison_write_stream()
                        raise ACPTransportError("acp_timeout")
                    try:
                        count = os.write(self._stdin.fileno(), encoded[written:])
                    except BlockingIOError:
                        continue
                    except OSError:
                        if written:
                            self._poison_write_stream()
                        raise ACPTransportError("acp_process_closed") from None
                    if count <= 0:
                        if written:
                            self._poison_write_stream()
                        raise ACPTransportError("acp_process_closed")
                    written += count
        finally:
            self._write_lock.release()

    def _poison_write_stream(self) -> None:
        self._broken = True
        try:
            self._stdin.close()
        except OSError:
            pass

    def _read_message(
        self, timeout: float, *, deadline: float | None = None
    ) -> Mapping[str, Any]:
        if timeout < 0:
            timeout = 0.0
        read_deadline = time.monotonic() + timeout
        if deadline is not None:
            read_deadline = min(read_deadline, deadline)
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise ACPTransportError("acp_timeout")
            newline = self._read_buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._read_buffer[:newline])
                try:
                    message = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    del self._read_buffer[: newline + 1]
                    raise ACPTransportError("acp_protocol_invalid") from None
                if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
                    del self._read_buffer[: newline + 1]
                    raise ACPTransportError("acp_protocol_invalid")
                if deadline is not None and time.monotonic() >= deadline:
                    raise ACPTransportError("acp_timeout")
                del self._read_buffer[: newline + 1]
                return message
            remaining = max(0.0, read_deadline - now)
            with selectors.DefaultSelector() as selector:
                selector.register(self._stdout, selectors.EVENT_READ)
                if not selector.select(remaining):
                    if self.process.poll() is not None:
                        raise ACPTransportError("acp_process_closed")
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

    def _handle_incoming_method(
        self, message: Mapping[str, Any], *, deadline: float | None = None
    ) -> None:
        request_id = message.get("id")
        if request_id is None:
            with self._notification_lock:
                self._notifications.append(message)
            return
        response: dict[str, object] = {"jsonrpc": "2.0", "id": request_id}
        if message.get("method") == "session/request_permission":
            params = message.get("params")
            options = params.get("options") if isinstance(params, Mapping) else None
            reject_option = None
            if isinstance(options, list):
                for kind in ("reject_once", "reject_always"):
                    reject_option = next(
                        (
                            option
                            for option in options
                            if isinstance(option, Mapping)
                            and option.get("kind") == kind
                            and isinstance(option.get("optionId"), str)
                        ),
                        None,
                    )
                    if reject_option is not None:
                        break
            outcome: dict[str, str]
            if reject_option is None:
                outcome = {"outcome": "cancelled"}
            else:
                outcome = {
                    "outcome": "selected",
                    "optionId": reject_option["optionId"],
                }
            response["result"] = {"outcome": outcome}
        else:
            response["error"] = {
                "code": -32601,
                "message": "unsupported ACP server request",
            }
        self._write(response, deadline=deadline)


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


def _process_group_exists(group_id: int) -> bool | None:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None
    return True


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
