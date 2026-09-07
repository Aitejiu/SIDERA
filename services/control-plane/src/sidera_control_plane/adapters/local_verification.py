from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.result_ports import (
    CheckRunObservation,
    CheckRunRequest,
    CheckRunStatus,
    LocalCheckRunnerPort,
    ProcessStartIdentity,
    ReportAvailability,
    ReportAvailabilityStatus,
)
from sidera_control_plane.domain.result_integration import CheckSpecV1


class LocalCheckRunnerError(RuntimeError):
    """The local verification runner cannot establish a safe execution boundary."""


_OID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_SAFE_ENVIRONMENT_KEYS = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT"}
)
_SENSITIVE_ENVIRONMENT_KEY = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|credential)"
)
_TERMINAL_STATUSES = frozenset(
    {
        CheckRunStatus.PASSED,
        CheckRunStatus.FAILED,
        CheckRunStatus.ENVIRONMENT_BLOCKED,
        CheckRunStatus.TIMED_OUT,
        CheckRunStatus.INTERRUPTED,
        CheckRunStatus.UNKNOWN,
    }
)


StreamReader = Callable[[Any, int], bytes]
ProcessFactory = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class _GitSnapshot:
    commit: str
    tree: str
    dirty_paths: tuple[str, ...]


@dataclass(slots=True)
class _LogCapture:
    stream: Any
    path: Path
    limit_bytes: int
    reader: StreamReader
    initial_size: int
    bytes_written: int = 0
    failure_code: str | None = None
    thread: threading.Thread | None = None

    def start(self) -> None:
        self.bytes_written = self.initial_size
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def join(self, timeout: float = 1.0) -> None:
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def close_stream(self) -> None:
        try:
            self.stream.close()
        except (OSError, ValueError):
            pass

    def _read(self) -> None:
        if self.bytes_written > self.limit_bytes:
            self.failure_code = "log_limit_exceeded"
            return
        try:
            with self.path.open("ab") as output:
                while True:
                    chunk = self.reader(self.stream, 64 * 1024)
                    if not isinstance(chunk, bytes):
                        self.failure_code = "log_read_error"
                        return
                    if not chunk:
                        return
                    remaining = self.limit_bytes - self.bytes_written
                    if remaining <= 0:
                        self.failure_code = "log_limit_exceeded"
                        return
                    output.write(chunk[:remaining])
                    output.flush()
                    self.bytes_written += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        self.failure_code = "log_limit_exceeded"
                        return
        except OSError:
            self.failure_code = "log_read_error"
        finally:
            self.close_stream()


@dataclass(slots=True)
class _Run:
    request: CheckRunRequest
    process: Any
    identity: ProcessStartIdentity
    baseline: _GitSnapshot
    log_refs: tuple[str, str]
    captures: tuple[_LogCapture, _LogCapture]
    observation: CheckRunObservation


class LocalCheckRunner(LocalCheckRunnerPort):
    """Execute one fixed CheckSpec in a candidate-owned local worktree.

    The runner owns a small JSON state record per operation. A fresh runner can
    safely observe a still-running process only when its PID, process group,
    and OS start time still match the saved identity. It never starts a second
    process for an operation whose previous state is not provably terminal.
    """

    def __init__(
        self,
        *,
        state_root: Path,
        log_limit_bytes: int = 1 * 1024 * 1024,
        report_limit_bytes: int = 10 * 1024 * 1024,
        approved_environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        process_factory: ProcessFactory = subprocess.Popen,
        stream_reader: StreamReader | None = None,
        termination_grace_seconds: float = 0.25,
    ) -> None:
        if log_limit_bytes <= 0:
            raise ValueError("log_limit_bytes must be positive")
        if report_limit_bytes <= 0:
            raise ValueError("report_limit_bytes must be positive")
        if termination_grace_seconds <= 0:
            raise ValueError("termination_grace_seconds must be positive")
        root = Path(state_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise LocalCheckRunnerError("runner state root is not a directory")
        self._state_root = root
        self._log_limit_bytes = log_limit_bytes
        self._report_limit_bytes = report_limit_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._process_factory = process_factory
        self._stream_reader = stream_reader or _read_stream
        self._termination_grace_seconds = termination_grace_seconds
        self._environment = _approved_environment(approved_environment)
        self._runs: dict[Any, _Run] = {}
        self._lock = threading.RLock()

    @property
    def state_root(self) -> Path:
        return self._state_root

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise LocalCheckRunnerError("runner clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def start(self, request: CheckRunRequest) -> CheckRunObservation:
        if not isinstance(request, CheckRunRequest):
            raise TypeError("local check runner requires a CheckRunRequest")
        with self._lock:
            existing = self._runs.get(request.operation_id)
            if existing is not None:
                return self._refresh_locked(existing)
            persisted = self._read_state(request.operation_id)
            if persisted is not None:
                return self._inspect_persisted_locked(persisted)

            root = request.workspace_location.expanduser().resolve()
            log_refs = _log_refs(request.log_ref)
            log_paths: tuple[Path, Path] = (
                root / PurePosixPath(log_refs[0]),
                root / PurePosixPath(log_refs[1]),
            )
            if not root.is_dir():
                return self._blocked(
                    request,
                    log_refs,
                    "workspace_unavailable",
                )
            try:
                _validate_check_paths(request.check, root)
                log_paths = (
                    _resolve_workspace_path(root, log_refs[0], "log path"),
                    _resolve_workspace_path(root, log_refs[1], "log path"),
                )
                _create_log_files(log_paths)
            except (OSError, TypeError, ValueError) as exc:
                return self._blocked(
                    request,
                    log_refs,
                    "check_path_invalid" if isinstance(exc, ValueError) else "log_open_failed",
                )

            baseline = _git_snapshot(root, self._environment)
            if baseline is None:
                return self._blocked(request, log_refs, "source_state_unavailable")
            if baseline.commit != request.actual_commit or baseline.tree != request.actual_tree:
                return self._blocked(
                    request,
                    log_refs,
                    "candidate_source_mismatch",
                    source_dirty=bool(baseline.dirty_paths),
                )
            allowed_paths = _allowed_dirty_paths(request)
            if _unexpected_dirty_paths(baseline.dirty_paths, allowed_paths):
                return self._blocked(
                    request,
                    log_refs,
                    "source_dirty_before_start",
                    source_dirty=True,
                )

            preparation_failure = self._run_preparations(
                request,
                root,
                log_paths,
            )
            if preparation_failure is not None:
                code, exit_code = preparation_failure
                status = (
                    CheckRunStatus.TIMED_OUT
                    if code == "deadline_exceeded"
                    else CheckRunStatus.ENVIRONMENT_BLOCKED
                    if code in {"prepare_start_failed", "log_read_error"}
                    else CheckRunStatus.FAILED
                )
                observation = self._observation(
                    request,
                    status=status,
                    pid=None,
                    identity=None,
                    started_at=None,
                    finished_at=None,
                    exit_code=exit_code,
                    log_refs=log_refs,
                    source_dirty=False,
                    failure_code=code,
                )
                self._persist(request, observation)
                return observation

            cwd = _resolve_workspace_path(root, request.check.cwd, "check cwd")
            try:
                process = self._spawn(request.check.argv, cwd)
            except (OSError, ValueError):
                return self._blocked(request, log_refs, "process_start_failed")
            try:
                identity = _process_identity(process, self._now())
            except OSError:
                _terminate_unverified_process(process)
                return self._blocked(request, log_refs, "process_identity_unavailable")
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.stdout is None or process.stderr is None:
                _terminate_unverified_process(process)
                return self._blocked(request, log_refs, "process_pipes_unavailable")

            captures = (
                _LogCapture(
                    process.stdout,
                    log_paths[0],
                    self._log_limit_bytes,
                    self._stream_reader,
                    _file_size(log_paths[0]),
                ),
                _LogCapture(
                    process.stderr,
                    log_paths[1],
                    self._log_limit_bytes,
                    self._stream_reader,
                    _file_size(log_paths[1]),
                ),
            )
            for capture in captures:
                capture.start()
            observation = self._observation(
                request,
                status=CheckRunStatus.RUNNING,
                pid=identity.pid,
                identity=identity,
                started_at=identity.started_at,
                finished_at=None,
                exit_code=None,
                log_refs=log_refs,
                source_dirty=False,
                failure_code=None,
            )
            run = _Run(
                request=request,
                process=process,
                identity=identity,
                baseline=baseline,
                log_refs=log_refs,
                captures=captures,
                observation=observation,
            )
            self._runs[request.operation_id] = run
            self._persist(request, observation)
            threading.Thread(
                target=self._monitor,
                args=(request.operation_id,),
                daemon=True,
            ).start()
            return observation

    def inspect(self, operation_id: UUID) -> CheckRunObservation | None:
        with self._lock:
            run = self._runs.get(operation_id)
            if run is not None:
                return self._refresh_locked(run)
            persisted = self._read_state(operation_id)
            if persisted is None:
                return None
            return self._inspect_persisted_locked(persisted)

    def stop(self, operation_id: UUID) -> CheckRunObservation | None:
        with self._lock:
            run = self._runs.get(operation_id)
            if run is not None:
                if run.observation.status in _TERMINAL_STATUSES:
                    return run.observation
                if run.process.poll() is not None:
                    return self._finalize_locked(run, forced_failure=None)
                if not _identity_matches(run.identity):
                    return self._mark_unknown_locked(run, "process_identity_mismatch")
                if not _terminate_process_group(
                    run.process,
                    run.identity,
                    self._termination_grace_seconds,
                ):
                    return self._mark_unknown_locked(run, "process_state_unknown")
                return self._finalize_locked(run, forced_failure="stopped_by_request")

            persisted = self._read_state(operation_id)
            if persisted is None:
                return None
            observation, _request = persisted
            if observation.status in _TERMINAL_STATUSES:
                return observation
            identity = observation.process_start_identity
            if identity is None or not _identity_matches(identity):
                unknown = replace(
                    observation,
                    status=CheckRunStatus.UNKNOWN,
                    exit_code=None,
                    finished_at=None,
                    failure_code="process_state_unknown",
                )
                self._persist(_request, unknown)
                return unknown
            if not _terminate_process_group_by_identity(
                identity,
                self._termination_grace_seconds,
            ):
                unknown = replace(
                    observation,
                    status=CheckRunStatus.UNKNOWN,
                    exit_code=None,
                    finished_at=None,
                    failure_code="process_state_unknown",
                )
                self._persist(_request, unknown)
                return unknown
            unknown = replace(
                observation,
                status=CheckRunStatus.UNKNOWN,
                exit_code=None,
                finished_at=None,
                failure_code="process_exit_code_unavailable",
            )
            self._persist(_request, unknown)
            return unknown

    def _monitor(self, operation_id) -> None:
        while True:
            with self._lock:
                run = self._runs.get(operation_id)
                if run is None or run.observation.status in _TERMINAL_STATUSES:
                    return
                if run.process.poll() is not None:
                    self._finalize_locked(run, forced_failure=None)
                    return
                issue = _capture_failure(run.captures)
                if issue is not None:
                    if not _identity_matches(run.identity):
                        self._mark_unknown_locked(run, "process_state_unknown")
                    elif _terminate_process_group(
                        run.process,
                        run.identity,
                        self._termination_grace_seconds,
                    ):
                        self._finalize_locked(run, forced_failure=issue)
                    else:
                        self._mark_unknown_locked(run, "process_state_unknown")
                    return
                if self._now() >= run.request.deadline:
                    if not _identity_matches(run.identity):
                        self._mark_unknown_locked(run, "process_state_unknown")
                    elif _terminate_process_group(
                        run.process,
                        run.identity,
                        self._termination_grace_seconds,
                    ):
                        self._finalize_locked(run, forced_failure="deadline_exceeded")
                    else:
                        self._mark_unknown_locked(run, "process_state_unknown")
                    return
            time.sleep(0.02)

    def _refresh_locked(self, run: _Run) -> CheckRunObservation:
        if run.observation.status in _TERMINAL_STATUSES:
            return run.observation
        if run.process.poll() is not None:
            return self._finalize_locked(run, forced_failure=None)
        issue = _capture_failure(run.captures)
        if issue is not None:
            if not _identity_matches(run.identity):
                return self._mark_unknown_locked(run, "process_state_unknown")
            if not _terminate_process_group(
                run.process,
                run.identity,
                self._termination_grace_seconds,
            ):
                return self._mark_unknown_locked(run, "process_state_unknown")
            return self._finalize_locked(run, forced_failure=issue)
        if self._now() >= run.request.deadline:
            if not _identity_matches(run.identity):
                return self._mark_unknown_locked(run, "process_state_unknown")
            if not _terminate_process_group(
                run.process,
                run.identity,
                self._termination_grace_seconds,
            ):
                return self._mark_unknown_locked(run, "process_state_unknown")
            return self._finalize_locked(run, forced_failure="deadline_exceeded")
        return run.observation

    def _finalize_locked(
        self,
        run: _Run,
        *,
        forced_failure: str | None,
    ) -> CheckRunObservation:
        for capture in run.captures:
            capture.join()
            if capture.thread is not None and capture.thread.is_alive():
                capture.close_stream()
                capture.join(timeout=0.1)
                if capture.thread.is_alive() and capture.failure_code is None:
                    capture.failure_code = "log_read_error"
        returncode = run.process.poll()
        if returncode is None:
            return self._mark_unknown_locked(run, "process_state_unknown")

        root = run.request.workspace_location.expanduser().resolve()
        snapshot = _git_snapshot(root, self._environment)
        source_dirty: bool | None
        if snapshot is None:
            source_dirty = None
        else:
            source_dirty = (
                snapshot.commit != run.request.actual_commit
                or snapshot.tree != run.request.actual_tree
                or bool(
                    _unexpected_dirty_paths(
                        snapshot.dirty_paths,
                        _allowed_dirty_paths(run.request),
                    )
                )
            )
        reports = _report_availability(
            run.request,
            root,
            self._report_limit_bytes,
        )
        capture_issue = _capture_failure(run.captures)
        report_issue = _report_failure(reports)
        status: CheckRunStatus
        failure_code: str | None
        if forced_failure == "deadline_exceeded":
            status, failure_code = CheckRunStatus.TIMED_OUT, forced_failure
        elif forced_failure == "stopped_by_request":
            status, failure_code = CheckRunStatus.INTERRUPTED, forced_failure
        elif forced_failure == "log_read_error":
            status, failure_code = CheckRunStatus.ENVIRONMENT_BLOCKED, forced_failure
        elif forced_failure is not None:
            status, failure_code = CheckRunStatus.FAILED, forced_failure
        elif capture_issue == "log_read_error":
            status, failure_code = CheckRunStatus.ENVIRONMENT_BLOCKED, capture_issue
        elif capture_issue is not None:
            status, failure_code = CheckRunStatus.FAILED, capture_issue
        elif snapshot is None:
            status, failure_code = CheckRunStatus.ENVIRONMENT_BLOCKED, "source_state_unavailable"
        elif source_dirty:
            status, failure_code = (
                CheckRunStatus.ENVIRONMENT_BLOCKED,
                "source_modified_during_check",
            )
        elif report_issue is not None:
            status, failure_code = CheckRunStatus.FAILED, report_issue
        elif returncode not in run.request.check.success_exit_codes:
            status, failure_code = CheckRunStatus.FAILED, "check_failed"
        else:
            status, failure_code = CheckRunStatus.PASSED, None
        observation = self._observation(
            run.request,
            status=status,
            pid=run.identity.pid,
            identity=run.identity,
            started_at=run.identity.started_at,
            finished_at=self._now(),
            exit_code=returncode,
            log_refs=run.log_refs,
            source_dirty=source_dirty,
            report_availability=reports,
            failure_code=failure_code,
        )
        run.observation = observation
        self._persist(run.request, observation)
        return observation

    def _mark_unknown_locked(
        self,
        run: _Run,
        failure_code: str,
    ) -> CheckRunObservation:
        observation = replace(
            run.observation,
            status=CheckRunStatus.UNKNOWN,
            exit_code=None,
            finished_at=None,
            failure_code=failure_code,
        )
        run.observation = observation
        self._persist(run.request, observation)
        return observation

    def _blocked(
        self,
        request: CheckRunRequest,
        log_refs: tuple[str, str],
        failure_code: str,
        *,
        source_dirty: bool | None = None,
    ) -> CheckRunObservation:
        observation = self._observation(
            request,
            status=CheckRunStatus.ENVIRONMENT_BLOCKED,
            pid=None,
            identity=None,
            started_at=None,
            finished_at=None,
            exit_code=None,
            log_refs=log_refs,
            source_dirty=source_dirty,
            report_availability=(),
            failure_code=failure_code,
        )
        self._persist(request, observation)
        return observation

    def _observation(
        self,
        request: CheckRunRequest,
        *,
        status: CheckRunStatus,
        pid: int | None,
        identity: ProcessStartIdentity | None,
        started_at: datetime | None,
        finished_at: datetime | None,
        exit_code: int | None,
        log_refs: tuple[str, str],
        source_dirty: bool | None,
        report_availability: tuple[ReportAvailability, ...] = (),
        failure_code: str | None,
    ) -> CheckRunObservation:
        return CheckRunObservation(
            operation_id=request.operation_id,
            status=status,
            pid=pid,
            process_start_identity=identity,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=exit_code,
            log_refs=log_refs,
            actual_commit=request.actual_commit,
            actual_tree=request.actual_tree,
            source_dirty=source_dirty,
            report_availability=report_availability,
            failure_code=failure_code,
        )

    def _spawn(self, argv: tuple[str, ...], cwd: Path) -> Any:
        return self._process_factory(
            list(argv),
            cwd=cwd,
            env=dict(self._environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )

    def _run_preparations(
        self,
        request: CheckRunRequest,
        root: Path,
        log_paths: tuple[Path, Path],
    ) -> tuple[str, int | None] | None:
        for argv in request.check.prepare_argvs:
            if self._now() >= request.deadline:
                return "deadline_exceeded", None
            cwd = _resolve_workspace_path(root, request.check.cwd, "prepare cwd")
            try:
                process = self._spawn(argv, cwd)
            except (OSError, ValueError):
                return "prepare_start_failed", None
            try:
                identity = _process_identity(process, self._now())
            except OSError:
                _terminate_unverified_process(process)
                return "prepare_start_failed", None
            if process.stdout is None or process.stderr is None:
                _terminate_unverified_process(process)
                return "prepare_start_failed", None
            captures = (
                _LogCapture(
                    process.stdout,
                    log_paths[0],
                    self._log_limit_bytes,
                    self._stream_reader,
                    _file_size(log_paths[0]),
                ),
                _LogCapture(
                    process.stderr,
                    log_paths[1],
                    self._log_limit_bytes,
                    self._stream_reader,
                    _file_size(log_paths[1]),
                ),
            )
            for capture in captures:
                capture.start()
            while process.poll() is None:
                issue = _capture_failure(captures)
                if issue is not None:
                    if _identity_matches(identity):
                        _terminate_process_group(
                            process,
                            identity,
                            self._termination_grace_seconds,
                        )
                    self._join_captures(captures)
                    return issue, process.poll()
                if self._now() >= request.deadline:
                    if _identity_matches(identity):
                        _terminate_process_group(
                            process,
                            identity,
                            self._termination_grace_seconds,
                        )
                    self._join_captures(captures)
                    return "deadline_exceeded", process.poll()
                time.sleep(0.02)
            self._join_captures(captures)
            issue = _capture_failure(captures)
            if issue is not None:
                return issue, process.returncode
            if process.returncode not in request.check.success_exit_codes:
                return "prepare_failed", process.returncode
        return None

    def _join_captures(self, captures: tuple[_LogCapture, _LogCapture]) -> None:
        for capture in captures:
            capture.join()
            if capture.thread is not None and capture.thread.is_alive():
                capture.close_stream()
                capture.join(timeout=0.1)

    def _persist(
        self,
        request: CheckRunRequest,
        observation: CheckRunObservation,
    ) -> None:
        payload = {
            "schema_version": 1,
            "operation_id": str(request.operation_id),
            "request": request.to_mapping(),
            "pid": observation.pid,
            "process_start_identity": (
                None
                if observation.process_start_identity is None
                else observation.process_start_identity.to_mapping()
            ),
            "observation": observation.to_mapping(),
        }
        destination = self._state_path(request.operation_id)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._state_root,
                prefix=f".{request.operation_id}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            raise LocalCheckRunnerError("check state could not be persisted") from exc

    def _read_state(
        self, operation_id: UUID
    ) -> tuple[CheckRunObservation, CheckRunRequest] | None:
        path = self._state_path(operation_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise LocalCheckRunnerError("check state could not be read") from exc
        if not isinstance(payload, Mapping):
            raise LocalCheckRunnerError("check state is invalid")
        request = CheckRunRequest.from_mapping(payload.get("request"))
        observation_mapping = payload.get("observation")
        if not isinstance(observation_mapping, Mapping):
            raise LocalCheckRunnerError("check observation state is invalid")
        observation_data = dict(observation_mapping)
        if "pid" in payload:
            observation_data["pid"] = payload["pid"]
        if "process_start_identity" in payload:
            observation_data["process_start_identity"] = payload[
                "process_start_identity"
            ]
        observation = CheckRunObservation.from_mapping(observation_data)
        if observation.operation_id != operation_id or request.operation_id != operation_id:
            raise LocalCheckRunnerError("check state operation identity changed")
        return observation, request

    def _inspect_persisted_locked(
        self,
        persisted: tuple[CheckRunObservation, CheckRunRequest],
    ) -> CheckRunObservation:
        observation, request = persisted
        if observation.status in _TERMINAL_STATUSES:
            return observation
        identity = observation.process_start_identity
        if identity is None or not _identity_matches(identity):
            unknown = replace(
                observation,
                status=CheckRunStatus.UNKNOWN,
                exit_code=None,
                finished_at=None,
                failure_code="process_state_unknown",
            )
            self._persist(request, unknown)
            return unknown
        if self._now() >= request.deadline and _terminate_process_group_by_identity(
            identity,
            self._termination_grace_seconds,
        ):
                unknown = replace(
                    observation,
                    status=CheckRunStatus.UNKNOWN,
                    exit_code=None,
                    finished_at=None,
                    failure_code="process_exit_code_unavailable",
                )
                self._persist(request, unknown)
                return unknown
        return observation

    def _state_path(self, operation_id: UUID) -> Path:
        return self._state_root / f"{operation_id}.json"


def _read_stream(stream: Any, size: int) -> bytes:
    return stream.read(size)


def _approved_environment(
    approved_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENVIRONMENT_KEYS
    }
    if approved_environment is None:
        return environment
    for key, value in approved_environment.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("approved environment names must be non-empty strings")
        if _SENSITIVE_ENVIRONMENT_KEY.search(key):
            raise ValueError("sensitive environment values are not allowed")
        if not isinstance(value, str):
            raise TypeError("approved environment values must be strings")
        environment[key] = value
    return environment


def _validate_check_paths(check: CheckSpecV1, root: Path) -> None:
    _resolve_workspace_path(root, check.cwd, "check cwd")
    for path in check.report_paths:
        _resolve_workspace_path(root, path, "report path")
    for argv in (check.argv, *check.prepare_argvs):
        if not argv or any(not isinstance(token, str) or not token or "\x00" in token for token in argv):
            raise ValueError("check argv must contain non-empty strings")


def _resolve_workspace_path(root: Path, reference: str, label: str) -> Path:
    if (
        not isinstance(reference, str)
        or not reference.strip()
        or reference.startswith(("/", "\\", "http:", "https:", "file:"))
        or "\\" in reference
        or "\x00" in reference
        or "://" in reference
    ):
        raise ValueError(f"{label} must be a relative local path")
    path = PurePosixPath(reference)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a relative local path")
    resolved_root = root.resolve()
    resolved = (resolved_root / path).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the workspace") from exc
    return resolved


def _create_log_files(paths: tuple[Path, Path]) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _log_refs(log_ref: str) -> tuple[str, str]:
    return (f"{log_ref}.stdout.log", f"{log_ref}.stderr.log")


def _allowed_dirty_paths(request: CheckRunRequest) -> tuple[str, ...]:
    return (*request.check.report_paths, *_log_refs(request.log_ref))


def _unexpected_dirty_paths(
    dirty_paths: tuple[str, ...],
    allowed_paths: tuple[str, ...],
) -> tuple[str, ...]:
    normalized = tuple(_normalize_git_path(path) for path in allowed_paths)
    return tuple(
        path
        for path in dirty_paths
        if not any(path == allowed or path.startswith(f"{allowed}/") for allowed in normalized)
    )


def _normalize_git_path(path: str) -> str:
    value = path.strip().strip('"')
    if " -> " in value:
        value = value.rsplit(" -> ", 1)[-1]
    return value


def _git_snapshot(root: Path, environment: Mapping[str, str]) -> _GitSnapshot | None:
    try:
        revisions = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
            env=dict(environment),
        )
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=dict(environment),
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    values = tuple(line.strip() for line in revisions.stdout.splitlines() if line.strip())
    if len(values) != 2 or _OID_PATTERN.fullmatch(values[0]) is None or _OID_PATTERN.fullmatch(values[1]) is None:
        return None
    dirty_paths = tuple(
        _normalize_git_path(line[3:])
        for line in status.stdout.splitlines()
        if len(line) >= 4 and line[:2].strip() != ""
    )
    return _GitSnapshot(values[0].lower(), values[1].lower(), dirty_paths)


def _report_availability(
    request: CheckRunRequest,
    root: Path,
    limit_bytes: int,
) -> tuple[ReportAvailability, ...]:
    results: list[ReportAvailability] = []
    for reference in request.check.report_paths:
        try:
            path = _resolve_workspace_path(root, reference, "report path")
        except ValueError:
            results.append(
                ReportAvailability(
                    path=reference,
                    status=ReportAvailabilityStatus.OUT_OF_BOUNDS,
                    size_bytes=None,
                    sha256=None,
                )
            )
            continue
        lexical = root / PurePosixPath(reference)
        if not lexical.exists():
            results.append(
                ReportAvailability(
                    path=reference,
                    status=ReportAvailabilityStatus.MISSING,
                    size_bytes=None,
                    sha256=None,
                )
            )
            continue
        try:
            if not path.is_file():
                raise OSError("report is not a regular file")
            size = path.stat().st_size
            if size > limit_bytes:
                results.append(
                    ReportAvailability(
                        path=reference,
                        status=ReportAvailabilityStatus.TOO_LARGE,
                        size_bytes=size,
                        sha256=None,
                    )
                )
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            results.append(
                ReportAvailability(
                    path=reference,
                    status=ReportAvailabilityStatus.READ_ERROR,
                    size_bytes=None,
                    sha256=None,
                )
            )
            continue
        results.append(
            ReportAvailability(
                path=reference,
                status=ReportAvailabilityStatus.PRESENT,
                size_bytes=size,
                sha256=digest,
            )
        )
    return tuple(results)


def _report_failure(reports: tuple[ReportAvailability, ...]) -> str | None:
    for report in reports:
        if report.status is ReportAvailabilityStatus.MISSING:
            return "report_missing"
        if report.status is ReportAvailabilityStatus.OUT_OF_BOUNDS:
            return "report_out_of_bounds"
        if report.status is ReportAvailabilityStatus.TOO_LARGE:
            return "report_too_large"
        if report.status is ReportAvailabilityStatus.READ_ERROR:
            return "report_read_error"
    return None


def _capture_failure(captures: tuple[_LogCapture, _LogCapture]) -> str | None:
    for capture in captures:
        if capture.failure_code == "log_limit_exceeded":
            return capture.failure_code
    for capture in captures:
        if capture.failure_code is not None:
            return capture.failure_code
    return None


def _process_identity(process: Any, fallback_started_at: datetime) -> ProcessStartIdentity:
    pid = process.pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise OSError("process pid is unavailable")
    process_group_id = os.getpgid(pid)
    observed_started_at = _observed_process_start(pid)
    return ProcessStartIdentity(
        pid=pid,
        process_group_id=process_group_id,
        started_at=observed_started_at or fallback_started_at,
        nonce=uuid4().hex,
    )


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
            local = datetime.strptime(value, fmt).replace(
                tzinfo=datetime.now().astimezone().tzinfo
            )
            return local.astimezone(UTC)
        except ValueError:
            continue
    return None


def _identity_matches(identity: ProcessStartIdentity) -> bool:
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


def _terminate_process_group(
    process: Any,
    identity: ProcessStartIdentity,
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
    except OSError:
        return False
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if process.poll() is not None:
        return True
    if not _identity_matches(identity):
        return False
    try:
        os.killpg(identity.process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return process.poll() is not None
    except OSError:
        return False
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        return False
    return process.poll() is not None


def _terminate_process_group_by_identity(
    identity: ProcessStartIdentity,
    grace_seconds: float,
) -> bool:
    if not _identity_matches(identity):
        return False
    try:
        os.killpg(identity.process_group_id, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _identity_matches(identity):
            return True
        time.sleep(0.01)
    if not _identity_matches(identity):
        return True
    try:
        os.killpg(identity.process_group_id, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _identity_matches(identity):
            return True
        time.sleep(0.01)
    return not _identity_matches(identity)


def _terminate_unverified_process(process: Any) -> None:
    try:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass
