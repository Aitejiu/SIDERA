from __future__ import annotations

import math
import os
import tempfile
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import SecretStr

from ..contracts import (
    AssembledInput,
    CapabilitySupport,
    JsonValue,
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
from ..redaction import ObservationRedactor
from . import opencode_descriptor
from .acp_transport import ACPProcessTransport, ACPTransportError
from .opencode_config import (
    PreparedOpenCodeConfig,
    build_child_environment,
    build_mcp_servers,
    preflight_opencode_config_sources,
    prepare_opencode_config,
    resolve_provider_env,
)

_REQUEST_TIMEOUT = 30.0
_STOP_GRACE = 1.0
_SAFE_SESSION_UPDATE_TYPES = frozenset(
    {
        "user_message_chunk",
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "current_mode_update",
        "config_option_update",
        "session_info_update",
        "available_commands_update",
        "user_message",
        "agent_message",
    }
)
_TERMINAL_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
)


def _support(status: SupportStatus, reason: str) -> CapabilitySupport:
    return CapabilitySupport(status=status, reason_code=reason, constraints={})


@dataclass(slots=True)
class _Session:
    handle: NativeHandle
    transport: ACPProcessTransport
    prepared: PreparedOpenCodeConfig
    http_supported: bool
    close_supported: bool
    cancel_supported: bool
    secret_values: tuple[str, ...]
    pending_updates: deque[tuple[Mapping[str, Any], UUID | None]] = field(
        default_factory=deque
    )
    condition: threading.Condition = field(default_factory=threading.Condition)
    response_state: NativeResponseState = NativeResponseState.IDLE
    active_response_id: UUID | None = None
    last_response_id: UUID | None = None
    cancel_requested_response_id: UUID | None = None
    cancel_waiting_response_id: UUID | None = None
    cancel_outcomes: dict[UUID, NativeCancelOutcome] = field(default_factory=dict)
    cursor: int = 0


@dataclass(slots=True)
class _OrphanedProcess:
    transport: ACPProcessTransport
    prepared: PreparedOpenCodeConfig | None
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None


class OpenCodeHarness(opencode_descriptor.OpenCodeDescriptor):
    """Generic OpenCode ACP adapter for the agents package."""

    def __init__(
        self,
        *,
        request_timeout_seconds: float = _REQUEST_TIMEOUT,
        prompt_timeout_seconds: float | None = None,
        cancel_timeout_seconds: float | None = None,
        close_timeout_seconds: float | None = None,
        stop_grace_seconds: float = _STOP_GRACE,
        parent_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._request_timeout = _positive_timeout(request_timeout_seconds)
        self._prompt_timeout = _positive_timeout(
            request_timeout_seconds
            if prompt_timeout_seconds is None
            else prompt_timeout_seconds
        )
        self._cancel_timeout = _positive_timeout(
            request_timeout_seconds
            if cancel_timeout_seconds is None
            else cancel_timeout_seconds
        )
        self._close_timeout = _positive_timeout(
            request_timeout_seconds
            if close_timeout_seconds is None
            else close_timeout_seconds
        )
        self._stop_grace = _positive_timeout(stop_grace_seconds)
        self._parent_environment = dict(
            os.environ if parent_environment is None else parent_environment
        )
        self._sessions: dict[UUID, _Session] = {}
        self._orphaned: dict[UUID, _OrphanedProcess] = {}
        self._lock = threading.RLock()

    def probe(
        self, candidate: RuntimeCandidate, checked_at: datetime | None = None
    ) -> RuntimeProbe:
        self._reap_orphaned()
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
        temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        try:
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="sidera-agents-probe-"
            )
            temporary = Path(temporary_directory.name)
            preflight_opencode_config_sources(temporary)
            prepared = prepare_opencode_config({}, (), private_root=temporary)
            environment = build_child_environment(
                self._parent_environment, {}, prepared
            )
            transport = ACPProcessTransport.spawn_argv(
                (candidate.executable, "acp"),
                cwd=temporary,
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
                self._terminate_or_retain(
                    transport, prepared, temporary_directory=temporary_directory
                )
            else:
                if prepared is not None:
                    prepared.close()
                if temporary_directory is not None:
                    temporary_directory.cleanup()
        return RuntimeProbe(
            actual_version=version,
            protocol_state=protocol_state,
            capabilities=capabilities,
            checked_at=checked,
        )

    def open(self, request: NativeOpenRequest) -> NativeHandle:
        self._reap_orphaned()
        if request.runtime.harness_id != self.harness_id:
            raise AgentError("configuration_invalid", "Runtime target is invalid.")
        workspace = Path(request.workspace.directory)
        if not workspace.is_absolute() or not workspace.is_dir():
            raise AgentError(
                "configuration_invalid", "Execution workspace is unavailable."
            )
        preflight_opencode_config_sources(workspace)
        mcp_servers = build_mcp_servers(request.tools, supports_http=True)

        prepared: PreparedOpenCodeConfig | None = None
        transport: ACPProcessTransport | None = None
        try:
            projected_config = dict(request.projected_config)
            provider_env = resolve_provider_env(
                projected_config.get("model_id"),
                projected_config.get("provider_env"),
                tuple(request.environment),
            )
            if provider_env is not None:
                projected_config["provider_env"] = provider_env
            prepared = prepare_opencode_config(
                projected_config, request.skills, tools=request.tools
            )
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
            protocol_version = initialize.get("protocolVersion")
            if type(protocol_version) is not int or protocol_version != 1:
                raise AgentError(
                    "unsupported_capability",
                    "OpenCode ACP protocol version is unsupported.",
                    "acp",
                )
            negotiated = _agent_capabilities(initialize)
            http_supported = _http_supported(negotiated)
            cancel_supported = _cancel_current_response_supported(negotiated)
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
                    *(("cancel_current_response",) if cancel_supported else ()),
                ),
            )
            session = _Session(
                handle=handle,
                transport=transport,
                prepared=prepared,
                http_supported=http_supported,
                close_supported=_close_supported(negotiated),
                cancel_supported=cancel_supported,
                secret_values=_secret_values(request),
            )
            with self._lock:
                self._sessions[handle.process_identity.owned_nonce] = session
            return handle
        except AgentError:
            if transport is not None:
                self._terminate_or_retain(transport, prepared)
            elif prepared is not None:
                prepared.close()
            raise
        except ACPTransportError as error:
            if transport is not None:
                self._terminate_or_retain(transport, prepared)
            elif prepared is not None:
                prepared.close()
            code = (
                "native_start_unknown"
                if error.reason == "acp_timeout"
                else "native_start_failed"
            )
            raise AgentError(code, "OpenCode session could not be confirmed.") from None

    def _terminate_or_retain(
        self,
        transport: ACPProcessTransport,
        prepared: PreparedOpenCodeConfig | None,
        *,
        temporary_directory: tempfile.TemporaryDirectory[str] | None = None,
    ) -> bool:
        if not transport.terminate(grace_seconds=self._stop_grace):
            orphan = _OrphanedProcess(
                transport=transport,
                prepared=prepared,
                temporary_directory=temporary_directory,
            )
            with self._lock:
                self._orphaned[transport.identity.owned_nonce] = orphan
            return False
        transport.close()
        if prepared is not None:
            prepared.close()
        if temporary_directory is not None:
            temporary_directory.cleanup()
        return True

    def _reap_orphaned(self) -> None:
        with self._lock:
            for nonce, orphan in tuple(self._orphaned.items()):
                if not orphan.transport.terminate(grace_seconds=self._stop_grace):
                    continue
                orphan.transport.close()
                if orphan.prepared is not None:
                    orphan.prepared.close()
                if orphan.temporary_directory is not None:
                    orphan.temporary_directory.cleanup()
                self._orphaned.pop(nonce, None)

    def prompt(
        self, handle: NativeHandle, input: AssembledInput, response_id: UUID
    ) -> NativeOutcome:
        session = self._session(handle)
        if handle.session_id is None:
            raise AgentError(
                "native_identity_unverified", "OpenCode session is invalid."
            )
        with session.condition:
            if session.active_response_id is not None:
                raise AgentError(
                    "native_response_unknown",
                    "OpenCode has an unresolved response in this session.",
                )
            self._collect_updates(session)
            session.active_response_id = response_id
            session.last_response_id = response_id
            session.cancel_requested_response_id = None
            session.response_state = NativeResponseState.ACTIVE
        try:
            result = session.transport.request(
                "session/prompt",
                {
                    "sessionId": handle.session_id,
                    "prompt": _prompt_blocks(input),
                },
                timeout=self._prompt_timeout,
            )
        except ACPTransportError as error:
            with session.condition:
                self._collect_updates(session)
                if session.active_response_id == response_id:
                    session.response_state = NativeResponseState.UNKNOWN
                session.condition.notify_all()
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
        except Exception:
            with session.condition:
                self._collect_updates(session)
                if session.active_response_id == response_id:
                    session.response_state = NativeResponseState.UNKNOWN
                session.condition.notify_all()
            raise
        stop_reason = result.get("stopReason")
        with session.condition:
            self._collect_updates(session)
            if session.active_response_id != response_id:
                session.response_state = NativeResponseState.UNKNOWN
            elif stop_reason in _TERMINAL_STOP_REASONS:
                session.response_state = NativeResponseState.IDLE
                if (
                    session.cancel_requested_response_id == response_id
                    and session.cancel_waiting_response_id == response_id
                ):
                    confirmed = stop_reason == "cancelled"
                    session.cancel_outcomes[response_id] = NativeCancelOutcome(
                        response_id=response_id,
                        status=(
                            NativeCancelState.CONFIRMED
                            if confirmed
                            else NativeCancelState.FAILED
                        ),
                        reason_code=None if confirmed else "response_completed",
                    )
                session.active_response_id = None
                session.cancel_requested_response_id = None
            else:
                session.response_state = NativeResponseState.UNKNOWN
            session.condition.notify_all()
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
            pass
        late_responses = session.transport.drain_late_responses()
        with session.condition:
            self._collect_updates(session)
            for method, response in late_responses:
                if method != "session/prompt" or response.get("error") is not None:
                    continue
                result = response.get("result")
                stop_reason = (
                    result.get("stopReason") if isinstance(result, Mapping) else None
                )
                if stop_reason in _TERMINAL_STOP_REASONS:
                    session.response_state = NativeResponseState.IDLE
                    response_id = session.active_response_id
                    if (
                        response_id is not None
                        and session.cancel_requested_response_id == response_id
                        and session.cancel_waiting_response_id == response_id
                    ):
                        confirmed = stop_reason == "cancelled"
                        session.cancel_outcomes[response_id] = NativeCancelOutcome(
                            response_id=response_id,
                            status=(
                                NativeCancelState.CONFIRMED
                                if confirmed
                                else NativeCancelState.FAILED
                            ),
                            reason_code=None if confirmed else "response_completed",
                        )
                    session.active_response_id = None
                    session.cancel_requested_response_id = None
            events: list[NativeEvent] = []
            while session.pending_updates and len(events) < limit:
                message, response_id = session.pending_updates.popleft()
                params = message.get("params")
                update = params.get("update") if isinstance(params, Mapping) else None
                if not isinstance(update, Mapping):
                    continue
                update_type = update.get("sessionUpdate")
                if not isinstance(update_type, str):
                    continue
                safe_update_type = (
                    update_type
                    if update_type in _SAFE_SESSION_UPDATE_TYPES
                    else "unknown"
                )
                text = _update_text(update)
                data: dict[str, JsonValue] = {"update_type": safe_update_type}
                if text is not None:
                    data.update(
                        ObservationRedactor(
                            SecretStr(value) for value in session.secret_values
                        ).text(text)
                    )
                if safe_update_type in {"tool_call", "tool_call_update"}:
                    for source_key, target_key in (
                        ("toolName", "tool_name"),
                        ("title", "tool_name"),
                        ("status", "status"),
                    ):
                        value = update.get(source_key)
                        if isinstance(value, str) and target_key not in data:
                            data[target_key] = _redact(value, session.secret_values)
                session.cursor += 1
                event = NativeEvent(
                    cursor=session.cursor,
                    native_id=None,
                    kind=_event_kind(safe_update_type),
                    response_id=response_id,
                    operation_id=None,
                    occurred_at=datetime.now(UTC),
                    data=data,
                )
                if event.cursor > after_cursor:
                    events.append(event)
            session.condition.notify_all()
            return tuple(events)

    @staticmethod
    def _collect_updates(session: _Session) -> None:
        response_id = session.active_response_id or session.last_response_id
        for message in session.transport.drain_notifications():
            params = message.get("params")
            if (
                message.get("method") == "session/update"
                and isinstance(params, Mapping)
                and params.get("sessionId") == session.handle.session_id
            ):
                session.pending_updates.append((message, response_id))

    def cancel(self, handle: NativeHandle, response_id: UUID) -> NativeCancelOutcome:
        session = self._session(handle)
        if not session.cancel_supported:
            return NativeCancelOutcome(
                response_id=response_id,
                status=NativeCancelState.UNSUPPORTED,
                reason_code="cancel_not_negotiated",
            )
        if handle.session_id is None:
            return NativeCancelOutcome(
                response_id=response_id,
                status=NativeCancelState.FAILED,
                reason_code="native_identity_unverified",
            )
        with session.condition:
            if session.active_response_id != response_id:
                return NativeCancelOutcome(
                    response_id=response_id,
                    status=NativeCancelState.FAILED,
                    reason_code="response_not_current",
                )
            if session.response_state is not NativeResponseState.ACTIVE:
                return NativeCancelOutcome(
                    response_id=response_id,
                    status=NativeCancelState.UNKNOWN,
                    reason_code="native_response_unknown",
                )
            if session.cancel_waiting_response_id is not None:
                return NativeCancelOutcome(
                    response_id=response_id,
                    status=NativeCancelState.UNKNOWN,
                    reason_code="native_response_unknown",
                )
            session.cancel_requested_response_id = response_id
            session.cancel_waiting_response_id = response_id
            try:
                session.transport.notify(
                    "session/cancel", {"sessionId": handle.session_id}
                )
            except ACPTransportError:
                session.cancel_requested_response_id = None
                session.cancel_waiting_response_id = None
                return NativeCancelOutcome(
                    response_id=response_id,
                    status=NativeCancelState.UNKNOWN,
                    reason_code="native_response_unknown",
                )

            try:
                deadline = time.monotonic() + self._cancel_timeout
                while True:
                    confirmed = session.cancel_outcomes.pop(response_id, None)
                    if confirmed is not None:
                        return confirmed
                    if (
                        session.active_response_id != response_id
                        or session.response_state is not NativeResponseState.ACTIVE
                    ):
                        return NativeCancelOutcome(
                            response_id=response_id,
                            status=NativeCancelState.UNKNOWN,
                            reason_code="native_response_unknown",
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return NativeCancelOutcome(
                            response_id=response_id,
                            status=NativeCancelState.UNKNOWN,
                            reason_code="native_response_unknown",
                        )
                    session.condition.wait(remaining)
            finally:
                session.cancel_waiting_response_id = None
                if session.cancel_requested_response_id == response_id:
                    session.cancel_requested_response_id = None
                session.cancel_outcomes.pop(response_id, None)

    def inspect(self, handle: NativeHandle) -> NativeInspection:
        session = self._session(handle)
        process_state = session.transport.identity_state(handle.process_identity)
        with session.condition:
            response_state = session.response_state
        return NativeInspection(
            process_state=process_state,
            response_state=response_state,
            reason_code=(
                None
                if process_state is NativeProcessState.ALIVE
                else "native_process_gone"
                if process_state is NativeProcessState.GONE
                else "native_identity_unverified"
            ),
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
                    timeout=self._close_timeout,
                )
            except ACPTransportError:
                reason = "native_close_unknown"
        closed = session.transport.terminate(grace_seconds=self._stop_grace)
        if not closed:
            return NativeCloseOutcome(
                status=NativeCloseState.UNKNOWN,
                reason_code=reason or "native_close_unknown",
            )
        session.transport.close()
        session.prepared.close()
        with self._lock:
            self._sessions.pop(handle.process_identity.owned_nonce, None)
        return NativeCloseOutcome(
            status=NativeCloseState.CLOSED,
            reason_code=None,
        )

    @staticmethod
    def _unavailable_capabilities(reason: str) -> dict[str, CapabilitySupport]:
        return {
            "acp": _support(SupportStatus.UNSUPPORTED, reason),
            "model": _support(SupportStatus.UNCONFIRMED, "model_response_unconfirmed"),
            "native_login": _support(
                SupportStatus.UNSUPPORTED, "global_auth_inheritance_not_supported"
            ),
            "skill": _support(SupportStatus.UNCONFIRMED, "skill_not_negotiated"),
            "mcp": _support(SupportStatus.UNSUPPORTED, reason),
            "mcp_http": _support(SupportStatus.UNSUPPORTED, "http_not_negotiated"),
            "native_tools": _support(
                SupportStatus.UNCONFIRMED, "native_tool_execution_unconfirmed"
            ),
            "integration": _support(
                SupportStatus.UNSUPPORTED, "integration_not_supported"
            ),
        }

    @staticmethod
    def _negotiated_capabilities(
        initialize: Mapping[str, Any],
    ) -> dict[str, CapabilitySupport]:
        protocol_version = initialize.get("protocolVersion")
        if type(protocol_version) is not int or protocol_version != 1:
            raise ACPTransportError("acp_protocol_version_unsupported")
        agent = _agent_capabilities(initialize)
        return {
            "acp": _support(SupportStatus.SUPPORTED, "acp_v1_initialized"),
            "model": _support(SupportStatus.UNCONFIRMED, "model_response_unconfirmed"),
            "native_login": _support(
                SupportStatus.UNSUPPORTED, "global_auth_inheritance_not_supported"
            ),
            "skill": _support(SupportStatus.UNCONFIRMED, "skill_effect_unconfirmed"),
            "mcp": _support(SupportStatus.SUPPORTED, "acp_v1_stdio"),
            "mcp_http": _support(
                SupportStatus.SUPPORTED
                if _http_supported(agent)
                else SupportStatus.UNSUPPORTED,
                "http_advertised" if _http_supported(agent) else "http_not_advertised",
            ),
            "native_tools": _support(
                SupportStatus.UNCONFIRMED, "native_tool_execution_unconfirmed"
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


def _positive_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("OpenCode timeouts must be positive finite numbers")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError("OpenCode timeouts must be positive finite numbers") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError("OpenCode timeouts must be positive finite numbers")
    return number


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


def _cancel_current_response_supported(capabilities: Mapping[str, Any]) -> bool:
    sessions = capabilities.get("sessionCapabilities")
    return isinstance(sessions, Mapping) and isinstance(
        sessions.get("cancelCurrentResponse"), Mapping
    )


def _secret_values(request: NativeOpenRequest) -> tuple[str, ...]:
    values: set[str] = set()
    for value in request.environment.values():
        if isinstance(value, SecretStr):
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
