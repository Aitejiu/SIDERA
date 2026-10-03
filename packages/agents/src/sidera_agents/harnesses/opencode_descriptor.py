from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import time
from datetime import datetime
from typing import IO, cast

from ..contracts import (
    CapabilitySupport,
    ConfigField,
    ConfigFieldKind,
    ConfigIssue,
    ConfigValidation,
    ProfileRevision,
    ProtocolState,
    RuntimeCandidate,
    RuntimeDescriptor,
    RuntimeProbe,
    RuntimeSnapshot,
    SupportStatus,
)
from ..errors import AgentError
from .opencode_config import (
    parse_model_route,
    reserved_environment_key,
    resolve_provider_env,
    validate_native_tools,
)

_MAX_OUTPUT_BYTES = 2048
_PROBE_TIMEOUT_SECONDS = 4.0
_VERSION_PATTERN = re.compile(
    r"(?i)(?:opencode\s+)?v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)"
)


def _support(status: SupportStatus, reason_code: str) -> CapabilitySupport:
    return CapabilitySupport(status=status, reason_code=reason_code, constraints={})


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()
    process.wait()


def read_version(executable: str) -> str:
    argv = [executable, "--version"]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            env={"PATH": os.environ.get("PATH", os.defpath), "LANG": "C"},
            start_new_session=os.name == "posix",
        )
    except OSError:
        raise AgentError(
            "runtime_probe_failed", "Runtime version check failed."
        ) from None

    stdout, stderr = process.stdout, process.stderr
    if stdout is None or stderr is None:
        try:
            _terminate(process)
        finally:
            for stream in (stdout, stderr):
                if stream is not None:
                    stream.close()
        raise AgentError("runtime_probe_failed", "Runtime version check failed.")

    streams: dict[IO[bytes], bytearray] = {stdout: bytearray(), stderr: bytearray()}
    selector = selectors.DefaultSelector()
    for stream, current in streams.items():
        selector.register(stream, selectors.EVENT_READ, data=(stream, current))
    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                raise AgentError(
                    "runtime_probe_timeout", "Runtime version check timed out."
                )
            for key, _ in selector.select(remaining):
                stream, current = cast(tuple[IO[bytes], bytearray], key.data)
                chunk = os.read(
                    stream.fileno(), min(1024, _MAX_OUTPUT_BYTES + 1 - len(current))
                )
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                current.extend(chunk)
                if len(current) > _MAX_OUTPUT_BYTES:
                    _terminate(process)
                    raise AgentError(
                        "runtime_probe_output_too_large",
                        "Runtime version output exceeded its limit.",
                    )
        return_code = process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _terminate(process)
        raise AgentError(
            "runtime_probe_timeout", "Runtime version check timed out."
        ) from None
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()
        if process.poll() is None:
            _terminate(process)

    if return_code != 0:
        raise AgentError("runtime_probe_failed", "Runtime version check failed.")
    output = bytes(streams[stdout]).decode("utf-8", errors="replace")
    match = _VERSION_PATTERN.search(output)
    if match is None:
        raise AgentError("runtime_probe_failed", "Runtime version could not be read.")
    return match.group(1)


class OpenCodeDescriptor:
    harness_id = "opencode"
    executable_name = "opencode"

    def describe(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            harness_id=self.harness_id,
            display_name="OpenCode",
            fields=(
                ConfigField(
                    key="model_id",
                    label="Model",
                    kind=ConfigFieldKind.TEXT,
                    options=(),
                    minimum=None,
                    maximum=None,
                    support=_support(SupportStatus.SUPPORTED, "model_route_projection"),
                ),
                ConfigField(
                    key="native_settings.provider_env",
                    label="Provider credential environment",
                    kind=ConfigFieldKind.TEXT,
                    options=(),
                    minimum=None,
                    maximum=None,
                    support=_support(
                        SupportStatus.SUPPORTED, "bound_provider_environment"
                    ),
                ),
                ConfigField(
                    key="native_settings.native_tools",
                    label="Native coding tools",
                    kind=ConfigFieldKind.SELECT,
                    options=("none", "read", "write"),
                    minimum=None,
                    maximum=None,
                    support=_support(
                        SupportStatus.SUPPORTED, "native_tool_permission_projection"
                    ),
                ),
                ConfigField(
                    key="thinking",
                    label="Thinking",
                    kind=ConfigFieldKind.SELECT,
                    options=(),
                    minimum=None,
                    maximum=None,
                    support=_support(
                        SupportStatus.UNCONFIRMED, "thinking_support_unconfirmed"
                    ),
                ),
            ),
            model_options=(),
            thinking_options=(),
        )

    def probe(self, candidate: RuntimeCandidate, checked_at: datetime) -> RuntimeProbe:
        acp = _support(SupportStatus.UNCONFIRMED, "acp_handshake_not_performed")
        capabilities = {
            "acp": acp,
            "model": _support(SupportStatus.UNCONFIRMED, "model_response_unconfirmed"),
            "native_login": _support(
                SupportStatus.UNSUPPORTED, "global_auth_inheritance_not_supported"
            ),
            "skill": _support(
                SupportStatus.UNCONFIRMED, "skill_projection_unconfirmed"
            ),
            "mcp": _support(SupportStatus.UNCONFIRMED, "mcp_projection_unconfirmed"),
            "native_tools": _support(
                SupportStatus.UNCONFIRMED, "native_tool_execution_unconfirmed"
            ),
            "integration": _support(
                SupportStatus.UNSUPPORTED, "integration_not_supported"
            ),
        }
        try:
            version = read_version(candidate.executable)
        except AgentError as error:
            capabilities["version_probe"] = _support(
                SupportStatus.UNSUPPORTED, error.code
            )
            return RuntimeProbe(
                actual_version="unknown",
                protocol_state=ProtocolState.UNAVAILABLE,
                capabilities=capabilities,
                checked_at=checked_at,
            )
        return RuntimeProbe(
            actual_version=version,
            protocol_state=ProtocolState.UNCONFIRMED,
            capabilities=capabilities,
            checked_at=checked_at,
        )

    def validate(
        self, profile: ProfileRevision, runtime: RuntimeSnapshot
    ) -> ConfigValidation:
        draft = profile.draft
        issues: list[ConfigIssue] = []
        try:
            parse_model_route(draft.model_id)
        except AgentError:
            issues.append(
                ConfigIssue(
                    field="model_id",
                    code="configuration_invalid",
                    message="The native model route is invalid.",
                    support=_support(SupportStatus.UNSUPPORTED, "model_route_invalid"),
                )
            )
        if draft.thinking is not None:
            issues.append(
                ConfigIssue(
                    field="thinking",
                    code="unsupported_capability",
                    message="Thinking support has not been confirmed.",
                    support=_support(
                        SupportStatus.UNCONFIRMED, "thinking_support_unconfirmed"
                    ),
                )
            )
        for key in sorted(draft.native_settings):
            if key in {"provider_env", "native_tools"}:
                continue
            issues.append(
                ConfigIssue(
                    field=f"native_settings.{key}",
                    code="unsupported_capability",
                    message="This native setting is not declared by the runtime.",
                    support=_support(SupportStatus.UNSUPPORTED, "unknown_native_field"),
                )
            )
        try:
            validate_native_tools(draft.native_settings.get("native_tools", "none"))
        except AgentError:
            issues.append(
                ConfigIssue(
                    field="native_settings.native_tools",
                    code="configuration_invalid",
                    message="Native coding tools must be none, read or write.",
                    support=_support(
                        SupportStatus.UNSUPPORTED, "native_tool_mode_invalid"
                    ),
                )
            )
        provider_env = None
        try:
            provider_env = resolve_provider_env(
                draft.model_id,
                draft.native_settings.get("provider_env"),
                tuple(binding.destination for binding in draft.secret_refs),
            )
        except AgentError:
            issues.append(
                ConfigIssue(
                    field="native_settings.provider_env",
                    code="configuration_invalid",
                    message="Provider credentials require an explicit model and bound environment name.",
                    support=_support(
                        SupportStatus.UNSUPPORTED, "provider_binding_invalid"
                    ),
                )
            )
        for binding in draft.secret_refs:
            if reserved_environment_key(binding.destination) and not (
                binding.destination == "OPENCODE_API_KEY"
                and provider_env == binding.destination
            ):
                issues.append(
                    ConfigIssue(
                        field="secret_refs",
                        code="reserved_environment_field",
                        message="This secret destination is reserved by the runtime.",
                        support=_support(
                            SupportStatus.UNSUPPORTED, "reserved_environment_field"
                        ),
                    )
                )
        for key in sorted(draft.environment):
            if reserved_environment_key(key):
                issues.append(
                    ConfigIssue(
                        field=f"environment.{key}",
                        code="reserved_environment_field",
                        message="This environment field is reserved by the runtime.",
                        support=_support(
                            SupportStatus.UNSUPPORTED, "reserved_environment_field"
                        ),
                    )
                )
        if draft.environment and not any(
            issue.field.startswith("environment.") for issue in issues
        ):
            issues.append(
                ConfigIssue(
                    field="environment",
                    code="unsupported_capability",
                    message="Environment projection has not been confirmed.",
                    support=_support(
                        SupportStatus.UNCONFIRMED, "environment_projection_unconfirmed"
                    ),
                )
            )
        return ConfigValidation(
            valid=not issues,
            issues=tuple(issues),
            effective_preview=None,
        )
