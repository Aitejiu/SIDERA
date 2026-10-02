from __future__ import annotations

import os
import tempfile
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ..contracts import (
    AssembledInput,
    CapabilitySupport,
    NativeCancelOutcome,
    NativeCancelState,
    NativeCloseOutcome,
    NativeCloseState,
    NativeEvent,
    NativeHandle,
    NativeInspection,
    NativeOpenRequest,
    NativeOutcome,
    NativeOutcomeStatus,
    NativeProcessState,
    NativeResponseState,
    ProtocolState,
    RuntimeCandidate,
    RuntimeProbe,
    SupportStatus,
)
from ..errors import AgentError
from . import opencode_descriptor
from .acp_transport import ACPProcessTransport, ACPTransportError
from .opencode_config import (
    PreparedOpenCodeConfig,
    build_child_environment,
    build_mcp_servers,
    prepare_opencode_config,
)

_REQUEST_TIMEOUT = 30.0
_STOP_GRACE = 1.0


def _support(status: SupportStatus, reason: str) -> CapabilitySupport:
    return CapabilitySupport(status=status, reason_code=reason, constraints={})


@dataclass(slots=True)
class _Session:
    handle: NativeHandle
    transport: ACPProcessTransport
    prepared: PreparedOpenCodeConfig
    http_supported: bool
    close_supported: bool
    secret_values: tuple[str, ...]
    pending_updates: deque[Mapping[str, Any]] = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)
    response_active: bool = False
    cursor: int = 0


class OpenCodeHarness(opencode_descriptor.OpenCodeDescriptor):
    """Generic OpenCode ACP adapter for the agents package."""

    def __init__(
        self,
        *,
        request_timeout_seconds: float = _REQUEST_TIMEOUT,
        stop_grace_seconds: float = _STOP_GRACE,
        parent_environment: Mapping[str, str] | None = None,
    ) -> None:
        if request_timeout_seconds <= 0 or stop_grace_seconds <= 0:
            raise ValueError("OpenCode timeouts must be positive")
        self._request_timeout = request_timeout_seconds
        self._stop_grace = stop_grace_seconds
        self._parent_environment = dict(
            os.environ if parent_environment is None else parent_environment
        )
        self._sessions: dict[UUID, _Session] = {}
        self._lock = threading.RLock()

    def probe(
        self, candidate: RuntimeCandidate, checked_at: datetime | None = None
    ) -> RuntimeProbe:
        checked = checked_at or datetime.now(UTC)
        try:
            version = opencode_descriptor.read_version(candidate.executable)
        except AgentError as error:
            capabilities = self._unavailable_capabilities(error.code)
            return RuntimeProbe(
                actual_version="unknown",
                protocol_state=ProtocolState.UNAVAILABLE,
                capabilities=capabilities,
                checked_at=checked,
            )

        prepared: PreparedOpenCodeConfig | None = None
        transport: ACPProcessTransport | None = None
        try:
            with tempfile.TemporaryDirectory(
                prefix="sidera-agents-probe-"
            ) as temporary:
                prepared = prepare_opencode_config({}, (), private_root=Path(temporary))
                environment = build_child_environment(
                    self._parent_environment, {}, prepared
                )
                transport = ACPProcessTransport.spawn_argv(
                    (candidate.executable, "acp"),
                    cwd=Path(temporary),
                    environment=environment,
                )
                initialize = transport.request(
                    "initialize", _initialize_params(), timeout=self._request_timeout
                )
                capabilities = self._negotiated_capabilities(initialize)
                protocol_state = ProtocolState.READY
        except (ACPTransportError, AgentError):
            capabilities = self._unavailable_capabilities("acp_handshake_unavailable")
            protocol_state = ProtocolState.UNAVAILABLE
        finally:
            if transport is not None:
                transport.terminate(grace_seconds=self._stop_grace)
                transport.close()
            if prepared is not None:
                prepared.close()
        return RuntimeProbe(
            actual_version=version,
            protocol_state=protocol_state,
            capabilities=capabilities,
            checked_at=checked,
        )

    def open(self, request: NativeOpenRequest) -> NativeHandle:
        if request.runtime.harness_id != self.harness_id:
            raise AgentError("configuration_invalid", "Runtime target is invalid.")
        if request.projected_config:
            raise AgentError(
                "configuration_invalid",
                "OpenCode profile settings are not confirmed for this runtime.",
                "projected_config",
            )
        workspace = Path(request.workspace.directory)
        if not workspace.is_absolute() or not workspace.is_dir():
            raise AgentError(
                "configuration_invalid", "Execution workspace is unavailable."
            )
        mcp_servers = build_mcp_servers(request.tools, supports_http=True)

        prepared: PreparedOpenCodeConfig | None = None
        transport: ACPProcessTransport | None = None
        try:
            prepared = prepare_opencode_config(request.projected_config, request.skills)
            environment = build_child_environment(
                self._parent_environment, request.environment, prepared
            )
            transport = ACPProcessTransport.spawn_argv(
                (request.runtime.executable, "acp"),
                cwd=workspace,
                environment=environment,
            )
            initialize = transport.request(
                "initialize", _initialize_params(), timeout=self._request_timeout
            )
            negotiated = _agent_capabilities(initialize)
            http_supported = _http_supported(negotiated)
            if any(server.get("type") == "http" for server in mcp_servers):
                snapshot_support = request.runtime.capabilities.get("mcp_http")
                if (
                    not http_supported
                    or snapshot_support is None
                    or snapshot_support.status is not SupportStatus.SUPPORTED
                ):
                    raise AgentError(
                        "unsupported_capability",
                        "OpenCode did not negotiate HTTP MCP support.",
                        "tools",
                    )
            session_result = transport.request(
                "session/new",
                {"cwd": str(workspace.resolve()), "mcpServers": list(mcp_servers)},
                timeout=self._request_timeout,
            )
            session_id = session_result.get("sessionId")
            if (
                not isinstance(session_id, str)
                or not session_id
                or "\x00" in session_id
            ):
                raise AgentError(
                    "native_identity_unverified",
                    "OpenCode session identity is invalid.",
                )
            handle = NativeHandle(
                run_id=request.run_id,
                session_id=session_id,
                process_identity=transport.identity,
                capabilities=(
                    "acp_v1",
                    "mcp_stdio",
                    *(("mcp_http",) if http_supported else ()),
                ),
            )
            session = _Session(
                handle=handle,
                transport=transport,
                prepared=prepared,
                http_supported=http_supported,
                close_supported=_close_supported(negotiated),
                secret_values=_secret_values(request),
            )
            with self._lock:
                self._sessions[handle.process_identity.owned_nonce] = session
            return handle
        except AgentError:
            if transport is not None:
                transport.terminate(grace_seconds=self._stop_grace)
                transport.close()
            if prepared is not None:
                prepared.close()
            raise
        except ACPTransportError as error:
            if transport is not None:
                transport.terminate(grace_seconds=self._stop_grace)
                transport.close()
            if prepared is not None:
                prepared.close()
            code = (
                "native_start_unknown"
                if error.reason == "acp_timeout"
                else "native_start_failed"
            )
            raise AgentError(code, "OpenCode session could not be confirmed.") from None

    def prompt(
        self, handle: NativeHandle, input: AssembledInput, response_id: UUID
    ) -> NativeOutcome:
        session = self._session(handle)
        if handle.session_id is None:
            raise AgentError(
                "native_identity_unverified", "OpenCode session is invalid."
            )
        with session.lock:
            session.response_active = True
        try:
            result = session.transport.request(
                "session/prompt",
                {
                    "sessionId": handle.session_id,
                    "prompt": _prompt_blocks(input),
                },
                timeout=self._request_timeout,
            )
        except ACPTransportError as error:
            return NativeOutcome(
                response_id=response_id,
                status=NativeOutcomeStatus.UNKNOWN,
                reason_code=(
                    "native_response_unknown"
                    if error.reason == "acp_timeout"
                    else "native_response_failed"
                ),
                observed_model=None,
                usage=None,
            )
        finally:
            with session.lock:
                session.response_active = False
        stop_reason = result.get("stopReason")
        if stop_reason == "end_turn":
            status = NativeOutcomeStatus.COMPLETED
            reason = None
        elif stop_reason == "cancelled":
            status = NativeOutcomeStatus.CANCELLED
            reason = None
        else:
            status = NativeOutcomeStatus.UNKNOWN
            reason = "native_response_unknown"
        return NativeOutcome(
            response_id=response_id,
            status=status,
            reason_code=reason,
            observed_model=None,
            usage=None,
        )

    def read_events(
        self, handle: NativeHandle, after_cursor: int, limit: int = 100
    ) -> tuple[NativeEvent, ...]:
        if after_cursor < 0 or limit < 1:
            raise AgentError("configuration_invalid", "Event cursor is invalid.")
        session = self._session(handle)
        try:
            session.transport.read_available(timeout=0.0)
        except ACPTransportError:
            return ()
        with session.lock:
            session.pending_updates.extend(
                message
                for message in session.transport.drain_notifications()
                if message.get("method") == "session/update"
            )
            events: list[NativeEvent] = []
            while session.pending_updates and len(events) < limit:
                message = session.pending_updates.popleft()
                params = message.get("params")
                update = params.get("update") if isinstance(params, Mapping) else None
                if not isinstance(update, Mapping):
                    continue
                update_type = update.get("sessionUpdate")
                if not isinstance(update_type, str):
                    continue
                text = _update_text(update)
                data: dict[str, object] = {"update_type": update_type}
                if text is not None:
                    data["text"] = _redact(text, session.secret_values)
                session.cursor += 1
                event = NativeEvent(
                    cursor=session.cursor,
                    native_id=None,
                    kind=_event_kind(update_type),
                    response_id=None,
                    operation_id=None,
                    occurred_at=datetime.now(UTC),
                    data=data,
                )
                if event.cursor > after_cursor:
                    events.append(event)
            return tuple(events)

    def cancel(self, handle: NativeHandle, response_id: UUID) -> NativeCancelOutcome:
        session = self._session(handle)
        if handle.session_id is None:
            return NativeCancelOutcome(
                response_id=response_id,
                status=NativeCancelState.FAILED,
                reason_code="native_identity_unverified",
            )
        try:
            session.transport.notify("session/cancel", {"sessionId": handle.session_id})
        except ACPTransportError:
            return NativeCancelOutcome(
                response_id=response_id,
                status=NativeCancelState.UNKNOWN,
                reason_code="native_response_unknown",
            )
        return NativeCancelOutcome(
            response_id=response_id,
            status=NativeCancelState.ACCEPTED,
            reason_code=None,
        )

    def inspect(self, handle: NativeHandle) -> NativeInspection:
        session = self._session(handle)
        alive = session.transport.identity_matches(handle.process_identity)
        with session.lock:
            response_state = (
                NativeResponseState.ACTIVE
                if session.response_active
                else NativeResponseState.IDLE
            )
        return NativeInspection(
            process_state=NativeProcessState.ALIVE
            if alive
            else NativeProcessState.GONE,
            response_state=response_state,
            reason_code=None if alive else "native_process_gone",
        )

    def close(self, handle: NativeHandle) -> NativeCloseOutcome:
        session = self._session(handle)
        reason = None
        if (
            session.close_supported
            and handle.session_id is not None
            and session.transport.process.poll() is None
        ):
            try:
                session.transport.request(
                    "session/close",
                    {"sessionId": handle.session_id},
                    timeout=self._request_timeout,
                )
            except ACPTransportError:
                reason = "native_close_unknown"
        closed = session.transport.terminate(grace_seconds=self._stop_grace)
        session.transport.close()
        session.prepared.close()
        with self._lock:
            self._sessions.pop(handle.process_identity.owned_nonce, None)
        return NativeCloseOutcome(
            status=NativeCloseState.CLOSED if closed else NativeCloseState.UNKNOWN,
            reason_code=None if closed else (reason or "native_close_unknown"),
        )

    @staticmethod
    def _unavailable_capabilities(reason: str) -> dict[str, CapabilitySupport]:
        return {
            "acp": _support(SupportStatus.UNSUPPORTED, reason),
            "skill": _support(SupportStatus.UNCONFIRMED, "skill_not_negotiated"),
            "mcp": _support(SupportStatus.UNSUPPORTED, reason),
            "mcp_http": _support(SupportStatus.UNSUPPORTED, "http_not_negotiated"),
            "integration": _support(
                SupportStatus.UNSUPPORTED, "integration_not_supported"
            ),
        }

    @staticmethod
    def _negotiated_capabilities(
        initialize: Mapping[str, Any],
    ) -> dict[str, CapabilitySupport]:
        if initialize.get("protocolVersion") != 1:
            raise ACPTransportError("acp_protocol_version_unsupported")
        agent = _agent_capabilities(initialize)
        return {
            "acp": _support(SupportStatus.SUPPORTED, "acp_v1_initialized"),
            "skill": _support(SupportStatus.UNCONFIRMED, "skill_effect_unconfirmed"),
            "mcp": _support(SupportStatus.SUPPORTED, "acp_v1_stdio"),
            "mcp_http": _support(
                SupportStatus.SUPPORTED
                if _http_supported(agent)
                else SupportStatus.UNSUPPORTED,
                "http_advertised" if _http_supported(agent) else "http_not_advertised",
            ),
            "integration": _support(
                SupportStatus.UNSUPPORTED, "integration_not_supported"
            ),
        }

    def _session(self, handle: NativeHandle) -> _Session:
        with self._lock:
            session = self._sessions.get(handle.process_identity.owned_nonce)
        if session is None or session.handle != handle:
            raise AgentError(
                "native_identity_unverified", "OpenCode process is unavailable."
            )
        return session


def _initialize_params() -> dict[str, object]:
    return {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
        "clientInfo": {
            "name": "sidera-agents",
            "title": "Sidera Agents",
            "version": "0.1.0",
        },
    }


def _agent_capabilities(initialize: Mapping[str, Any]) -> Mapping[str, Any]:
    value = initialize.get("agentCapabilities")
    return value if isinstance(value, Mapping) else {}


def _http_supported(capabilities: Mapping[str, Any]) -> bool:
    mcp = capabilities.get("mcpCapabilities")
    return isinstance(mcp, Mapping) and mcp.get("http") is True


def _close_supported(capabilities: Mapping[str, Any]) -> bool:
    sessions = capabilities.get("sessionCapabilities")
    return isinstance(sessions, Mapping) and isinstance(sessions.get("close"), Mapping)


def _secret_values(request: NativeOpenRequest) -> tuple[str, ...]:
    values: set[str] = set()
    for value in request.environment.values():
        if hasattr(value, "get_secret_value"):
            secret = value.get_secret_value()
            if secret:
                values.add(secret)
    for tool in request.tools:
        for value in (*tool.headers.values(), *tool.environment.values()):
            secret = value.get_secret_value()
            if secret:
                values.add(secret)
    return tuple(sorted(values, key=len, reverse=True))


def _prompt_blocks(input: AssembledInput) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    if input.instructions:
        blocks.append(
            {
                "type": "text",
                "text": "Instructions:\n" + "\n\n".join(input.instructions),
            }
        )
    if input.text:
        blocks.append({"type": "text", "text": input.text})
    for item in input.context:
        if item.text is None:
            raise AgentError(
                "configuration_invalid", "Resolved context is unavailable."
            )
        blocks.append(
            {
                "type": "text",
                "text": (
                    f"Context material ({item.origin}, version {item.version}, "
                    f"{item.content_hash}):\n{item.text}"
                ),
            }
        )
    return blocks


def _update_text(update: Mapping[str, Any]) -> str | None:
    content = update.get("content")
    if isinstance(content, Mapping) and isinstance(content.get("text"), str):
        return content["text"]
    return None


def _event_kind(update_type: str) -> str:
    if update_type in {"agent_message", "agent_message_chunk", "plan"}:
        return "progress.updated"
    if update_type in {"tool_call", "tool_call_update"}:
        return "tool.updated"
    return "native.updated"


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    return value[:8192]
