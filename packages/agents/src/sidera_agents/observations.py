from __future__ import annotations

import math
import os
import stat
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import RLock
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import SecretStr

from .contracts import (
    EventPage,
    ExecutionWorkspace,
    JsonValue,
    NativeEvent,
    NativeHandle,
    NativeOutcome,
    OutputRef,
    RunEvent,
)
from .errors import AgentError
from .ports import ExecutionAuthorizer
from .redaction import ObservationRedactor

MAX_EVENT_PAGE = 500
_NATIVE_PAGE = 100
_UPDATE_TYPES = frozenset(
    {
        "user_message_chunk",
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "user_message",
        "agent_message",
    }
)


def validate_page(after_sequence: int, limit: int, head: int) -> None:
    """Public pages are bounded to 500 records, and cursors cannot exceed the head."""
    if (
        type(after_sequence) is not int
        or not 0 <= after_sequence <= head
        or type(limit) is not int
        or not 1 <= limit <= MAX_EVENT_PAGE
    ):
        raise AgentError("configuration_invalid", "Event cursor or limit is invalid.")


def event_page(events: tuple[RunEvent, ...], after: int, limit: int) -> EventPage:
    visible = events[:limit]
    return EventPage(
        events=visible,
        next_sequence=visible[-1].sequence if visible else after,
        has_more=len(events) > limit,
    )


def public_event_data(data: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: value for key, value in data.items() if key != "_native"}


class RunObservations:
    def __init__(self, execution, store) -> None:
        self._execution = execution
        self._store = store

    def read_trace(
        self,
        workspace_id: UUID,
        run_id: UUID,
        after_sequence: int,
        limit: int = 100,
        *,
        grant_id: UUID,
    ) -> EventPage:
        return self._execution.list_events(
            workspace_id, run_id, after_sequence, limit, grant_id=grant_id
        )

    def list_outputs(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        grant_id: UUID,
    ) -> tuple[OutputRef, ...]:
        self._execution.get_run(workspace_id, run_id, grant_id=grant_id)
        return self._store.list_output_refs(workspace_id, run_id)


@dataclass(frozen=True)
class SafeObservation:
    kind: str
    occurred_at: datetime
    operation_id: UUID | None
    response_id: UUID | None
    data: dict[str, JsonValue]
    native_cursor: int | None = None
    identity: str | None = None


def native_source(handle: NativeHandle) -> str:
    # Identity is scoped to this exact Session/process, never merely the Run or text.
    return sha256(handle.model_dump_json().encode("utf-8")).hexdigest()


def _usage(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, dict):
        return None
    safe: dict[str, JsonValue] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"):
        amount = value.get(key)
        if type(amount) is int and 0 <= amount <= 2**63 - 1:
            safe[key] = amount
    cost = value.get("cost")
    if (
        (type(cost) is int or type(cost) is float)
        and 0 <= cost <= 10**12
        and math.isfinite(cost)
    ):
        safe["cost"] = cost
    return safe or None


def _output(
    data: Mapping[str, JsonValue],
    artifact_directory: str | None,
    redactor: ObservationRedactor,
) -> OutputRef | None:
    if artifact_directory is None:
        return None
    uri = data.get("uri")
    ref_id = redactor.label(data.get("ref_id"), r"[A-Za-z0-9][A-Za-z0-9._-]*")
    media_type = redactor.label(
        data.get("media_type"), r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+"
    )
    digest = redactor.label(data.get("content_hash"), r"sha256:[0-9a-f]{64}")
    if not isinstance(uri, str) or not ref_id or not media_type or not digest:
        return None
    if redactor.text(uri) != {"text": uri}:
        return None
    try:
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            return None
        path, root = Path(unquote(parsed.path)), Path(artifact_directory)
        if not path.is_absolute() or not root.is_absolute() or uri != path.as_uri():
            return None
        if ".." in path.parts or ".." in root.parts or not path.is_relative_to(root):
            return None
        # Walk from the filesystem root with O_NOFOLLOW; even parent symlink races
        # must not turn an artifact reference into an arbitrary local file read.
        directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
            with os.fdopen(file_fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None
                actual = sha256()
                while chunk := stream.read(65536):
                    actual.update(chunk)
                if "sha256:" + actual.hexdigest() != digest:
                    return None
        finally:
            os.close(directory_fd)
    except (OSError, ValueError):
        return None
    return OutputRef(ref_id=ref_id, media_type=media_type, uri=uri, content_hash=digest)


class ObservationIngestor:
    """Volatile safe page, committed native cursor, and verified response correlation.

    ACP has no durable notification ID. Local positions deduplicate only within
    the original verified Session/process; arbitrary replayed chunks have no
    provable identity. Uncommitted pages survive a write retry, not process loss.
    """

    def __init__(
        self,
        store,
        harness,
        handle,
        workspace_id,
        owner_id,
        secrets,
        *,
        authorizer: ExecutionAuthorizer,
        grant_id: UUID,
        workspace: ExecutionWorkspace,
    ):
        self._store, self._harness = store, harness
        self.handle: NativeHandle = handle
        self.workspace_id: UUID = workspace_id
        self.owner_id: UUID = owner_id
        self.source = native_source(handle)
        self.lock = RLock()
        self._redactor = ObservationRedactor(secrets)
        self._authorizer = authorizer
        self._grant_id: UUID | None = grant_id
        self._workspace: ExecutionWorkspace | None = workspace
        self._cursor: int | None = None
        self._pending: deque[SafeObservation] = deque()
        self._outcome: SafeObservation | None = None
        self._response_id: UUID | None = None
        self._operation_id: UUID | None = None
        self._correlations: dict[UUID, UUID] = {}
        self._write_failed = False
        self.closed = False
        self.closing = False
        self.close_deferred = False

    @property
    def pending(self) -> bool:
        return bool(self._pending or self._outcome)

    def begin_response(self, response_id: UUID, operation_id: UUID) -> None:
        with self.lock:
            self._response_id, self._operation_id = response_id, operation_id
            self._correlations[response_id] = operation_id

    def safe_model(self, value: object) -> str | None:
        return self._redactor.label(value, r"[A-Za-z0-9][A-Za-z0-9._:/-]*")

    def complete_response(self, outcome: NativeOutcome | None) -> str | None:
        with self.lock:
            model = self.safe_model(
                None if outcome is None or self.closed else outcome.observed_model
            )
            usage = _usage(None if outcome is None or self.closed else outcome.usage)
            data: dict[str, JsonValue] = {"usage": usage, "observed_model": model}
            if usage is None:
                data["reason_code"] = "usage_unavailable"
            if (
                outcome is not None
                and outcome.observed_model is not None
                and model is None
            ):
                data["model_reason_code"] = "observed_model_omitted"
            self._outcome = SafeObservation(
                kind="usage",
                occurred_at=datetime.now(UTC),
                operation_id=self._operation_id,
                response_id=self._response_id,
                data=data,
                identity="outcome:" + str(self._response_id),
            )
            self.ingest(final=True)
            return model

    def close_context(self) -> None:
        with self.lock:
            self.closed = True
            self._redactor.clear()
            self._grant_id = None
            self._workspace = None

    def _normalize(self, event: NativeEvent) -> SafeObservation:
        response_id = event.response_id or self._response_id
        operation_id = (
            self._correlations.get(response_id) if response_id is not None else None
        )
        verified = response_id in self._correlations and (
            event.operation_id is None or event.operation_id == operation_id
        )
        data: dict[str, JsonValue]
        if not verified:
            kind = "diagnostic"
            data = {"reason_code": "native_correlation_unverified"}
            response_id, operation_id = None, None
        else:
            kind, data = self._safe_data(event)
        identity = None
        if event.native_id is not None and verified:
            try:
                identity = sha256(
                    (str(response_id) + "\0" + event.native_id).encode("utf-8")
                ).hexdigest()
            except UnicodeEncodeError:
                data["reason_code"] = "native_identity_omitted"
        return SafeObservation(
            kind=kind,
            occurred_at=event.occurred_at,
            response_id=response_id,
            operation_id=operation_id,
            data=data,
            native_cursor=event.cursor,
            identity=identity,
        )

    def _safe_data(self, event: NativeEvent) -> tuple[str, dict[str, JsonValue]]:
        kind = {
            "progress.updated": "message",
            "tool.updated": "tool",
            "stderr": "diagnostic",
        }.get(event.kind, event.kind)
        if kind == "artifact":
            workspace = self._workspace
            if (
                workspace is None
                or workspace.artifact_directory is None
                or self._grant_id is None
            ):
                return kind, {"reason_code": "output_ref_unverified"}
            try:
                self._authorizer.require(self._grant_id, "artifact.read", workspace)
            except Exception:  # noqa: BLE001 - optional evidence fails closed without authority details
                return kind, {"reason_code": "artifact_read_denied"}
            output = _output(event.data, workspace.artifact_directory, self._redactor)
            return kind, {"output": output.model_dump(mode="json")} if output else {
                "reason_code": "output_ref_unverified"
            }
        if kind not in {"message", "tool", "diagnostic"}:
            return "diagnostic", {"reason_code": "native_content_omitted"}
        allowed = {"text", "update_type", "tool_name", "status", "truncated"}
        data: dict[str, JsonValue] = {}
        if "text" in event.data:
            data.update(
                self._redactor.text(
                    event.data["text"],
                    maximum_bytes=4096 if kind == "diagnostic" else 8192,
                )
            )
        update_type = event.data.get("update_type")
        if isinstance(update_type, str) and update_type in _UPDATE_TYPES:
            data["update_type"] = update_type
        if kind == "tool":
            name = self._redactor.label(
                event.data.get("tool_name"), r"[A-Za-z0-9][A-Za-z0-9._:/-]*"
            )
            if name is not None:
                data["tool_name"] = name
            status = event.data.get("status")
            if isinstance(status, str) and status in {
                "pending",
                "in_progress",
                "completed",
                "failed",
            }:
                data["status"] = status
        if event.data.get("truncated") is True and "text" in data:
            data.update(truncated=True, reason_code="text_truncated")
        reason = event.data.get("reason_code")
        if isinstance(reason, str) and reason in {
            "text_truncated",
            "unsafe_text_omitted",
            "native_content_omitted",
        }:
            data["reason_code"] = reason
        if set(event.data) - allowed and "reason_code" not in data:
            data["reason_code"] = "unsafe_fields_omitted"
        if not data:
            data["reason_code"] = "native_content_omitted"
        return kind, data

    def ingest(self, *, final: bool = False, retry: bool = False) -> bool:
        if not self.lock.acquire(blocking=final):
            return False
        try:
            if self._write_failed and not retry:
                return False
            self._write_failed = False
            if self._cursor is None:
                cursor: int = self._store.get_native_event_cursor(
                    self.workspace_id, self.handle.run_id, self.source
                )
                self._cursor = cursor
            else:
                cursor = self._cursor
            for _ in range(100 if final else 1):
                page = None
                if not self._pending and not self.closed and not self.closing:
                    try:
                        page = self._harness.read_events(
                            self.handle, cursor, _NATIVE_PAGE
                        )
                    except Exception:  # noqa: BLE001 - native diagnostics are never persisted raw
                        page = ()
                    self._pending.extend(
                        self._normalize(event)
                        for event in page[:_NATIVE_PAGE]
                        if event.cursor > cursor
                    )
                while self._pending:
                    record = self._pending[0]
                    try:
                        self._store.append_observation(
                            self.workspace_id,
                            self.handle.run_id,
                            record,
                            source=self.source,
                            owner_id=self.owner_id,
                        )
                    except Exception:  # noqa: BLE001 - keep only this safe page for the next owner tick
                        self._write_failed = True
                        return False
                    cursor = max(cursor, record.native_cursor or 0)
                    self._cursor = cursor
                    self._pending.popleft()
                if page is None and not self.closed and not self.closing:
                    if not final:
                        return True
                    continue
                if self.closed or self.closing or not page:
                    if self._outcome is not None:
                        try:
                            self._store.append_observation(
                                self.workspace_id,
                                self.handle.run_id,
                                self._outcome,
                                source=self.source,
                                owner_id=self.owner_id,
                            )
                        except Exception:  # noqa: BLE001 - outcome is already normalized and retryable
                            self._write_failed = True
                            return False
                        self._outcome = None
                    return True
                if not final:
                    return True
            return False
        finally:
            self.lock.release()


def resolved_redaction_secrets(request, grant_id: UUID) -> tuple[SecretStr, ...]:
    secrets = [SecretStr(str(grant_id))]
    secrets.extend(
        value for value in request.environment.values() if isinstance(value, SecretStr)
    )
    for tool in request.tools:
        secrets.extend(tool.headers.values())
        secrets.extend(tool.environment.values())
    return tuple(secrets)
