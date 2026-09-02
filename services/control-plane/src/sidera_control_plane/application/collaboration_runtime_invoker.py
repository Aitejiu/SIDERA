from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Protocol

from sidera_control_plane.application.collaboration_reconciler import (
    PreparedCollaborationEvaluation,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    ResolvedCollaborationRuntime,
    RuntimeResolutionError,
)
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    ModuleRepositoryPort,
)
from sidera_control_plane.domain.collaboration import CollaborationRun
from sidera_control_plane.domain.collaboration_runtime import (
    CollaborationRuntime,
    RuntimeDecisionV1,
    RuntimeEnvelopeError,
    canonical_json_bytes,
    parse_runtime_decision_v1,
)

_DEFAULT_MAX_PROJECTION_BYTES = 1_048_576
_DEFAULT_MAX_DECISION_BYTES = 65_536
_DEFAULT_MAX_INTENTS = 128
_DEFAULT_MAX_STRATEGY_STATE_BYTES = 16_384
_DEFAULT_MAX_DIAGNOSTIC_BYTES = 4_096

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|provider[_-]?key|token|secret|password|authorization)"
    r"\s*[:=]\s*[^\s,;]+"
)
_BEARER_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_API_KEY_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


class RuntimeTimeoutError(TimeoutError):
    """An adapter reports that its call exceeded the supplied deadline."""


class RuntimeUnavailableError(RuntimeError):
    """An adapter reports a temporary provider or transport failure."""


class CollaborationRuntimeResolverPort(Protocol):
    def resolve(self, run: CollaborationRun) -> ResolvedCollaborationRuntime:
        raise NotImplementedError


RuntimeResolverFactory = Callable[
    [ModuleRepositoryPort], CollaborationRuntimeResolverPort
]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class RuntimeInvocationResult:
    response_bytes: bytes
    response_hash: str
    decision: RuntimeDecisionV1
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RuntimeInvocationFailure:
    code: str
    response_hash: str | None
    diagnostic_summary: str
    retryable: bool
    duration_ms: int


class CollaborationRuntimeInvoker:
    """Invoke one prepared Runtime evaluation without holding a database lease."""

    def __init__(
        self,
        *,
        runtime_resolver: CollaborationRuntimeResolverPort | None = None,
        runtime_resolver_factory: RuntimeResolverFactory | None = None,
        resolution_uow_factory: Callable[[], ControlPlaneUnitOfWork] | None = None,
        evaluation_timeout: timedelta = timedelta(seconds=30),
        lease_safety_margin: timedelta = timedelta(seconds=5),
        max_projection_bytes: int = _DEFAULT_MAX_PROJECTION_BYTES,
        max_decision_bytes: int = _DEFAULT_MAX_DECISION_BYTES,
        max_intents: int = _DEFAULT_MAX_INTENTS,
        max_strategy_state_bytes: int = _DEFAULT_MAX_STRATEGY_STATE_BYTES,
        max_diagnostic_bytes: int = _DEFAULT_MAX_DIAGNOSTIC_BYTES,
        redaction_values: Iterable[str] = (),
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        if evaluation_timeout <= timedelta(0):
            raise ValueError("evaluation_timeout must be positive")
        if lease_safety_margin < timedelta(0):
            raise ValueError("lease_safety_margin cannot be negative")
        for value, name in (
            (max_projection_bytes, "max_projection_bytes"),
            (max_decision_bytes, "max_decision_bytes"),
            (max_intents, "max_intents"),
            (max_strategy_state_bytes, "max_strategy_state_bytes"),
            (max_diagnostic_bytes, "max_diagnostic_bytes"),
        ):
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if runtime_resolver_factory is not None and resolution_uow_factory is None:
            raise ValueError(
                "resolution_uow_factory is required with runtime_resolver_factory"
            )
        self._runtime_resolver = runtime_resolver
        self._runtime_resolver_factory = runtime_resolver_factory
        self._resolution_uow_factory = resolution_uow_factory
        self._evaluation_timeout = evaluation_timeout
        self._lease_safety_margin = lease_safety_margin
        self._max_projection_bytes = max_projection_bytes
        self._max_decision_bytes = max_decision_bytes
        self._max_intents = max_intents
        self._max_strategy_state_bytes = max_strategy_state_bytes
        self._max_diagnostic_bytes = max_diagnostic_bytes
        self._redaction_values = tuple(
            value for value in redaction_values if value and value.strip()
        )
        self._monotonic_clock = monotonic_clock or time.monotonic

    def invoke(
        self,
        prepared: PreparedCollaborationEvaluation,
        *,
        now: datetime,
        runtime: CollaborationRuntime | None = None,
    ) -> RuntimeInvocationResult | RuntimeInvocationFailure:
        self._validate_now(now)
        started = self._monotonic_clock()
        response_hash: str | None = None
        response_bytes: bytes | None = None
        elapsed: float | None = None
        try:
            capability = prepared.run.runtime_capability_snapshot
            response_bytes = prepared.projection.canonical_bytes()
            projection_limit = min(
                self._max_projection_bytes,
                capability.max_projection_bytes,
            )
            if len(response_bytes) > projection_limit:
                return self._failure(
                    "projection_too_large",
                    started=started,
                    diagnostic=(
                        "collaboration projection exceeds the configured byte limit"
                    ),
                )

            resolved_runtime = runtime or self._resolve_runtime(prepared.run)

            deadline = min(
                now + self._evaluation_timeout,
                prepared.lease_expires_at - self._lease_safety_margin,
            )
            if deadline <= now:
                return self._failure(
                    "runtime_timeout",
                    started=started,
                    diagnostic="evaluation lease leaves no time for Runtime execution",
                    retryable=True,
                )

            returned = resolved_runtime.evaluate(
                prepared.projection,
                operation_id=prepared.operation_id,
                deadline=deadline,
            )
            elapsed = self._elapsed_seconds(started)
            if now + timedelta(seconds=elapsed) >= deadline:
                return self._failure(
                    "runtime_timeout",
                    started=started,
                    diagnostic="Runtime returned after the evaluation deadline",
                    retryable=True,
                    elapsed=elapsed,
                )
            response_bytes = _canonical_response_bytes(returned)
            response_hash = sha256(response_bytes).hexdigest()
            decision_limit = min(
                self._max_decision_bytes,
                capability.max_decision_bytes,
            )
            if len(response_bytes) > decision_limit:
                return self._failure(
                    "runtime_output_invalid",
                    started=started,
                    response_hash=response_hash,
                    diagnostic="Runtime response exceeds the configured byte limit",
                    elapsed=elapsed,
                )
            decision = parse_runtime_decision_v1(
                response_bytes,
                max_bytes=decision_limit,
                max_intents=min(
                    self._max_intents,
                    capability.max_intents_per_decision,
                ),
                max_state_bytes=min(
                    self._max_strategy_state_bytes,
                    capability.max_strategy_state_bytes,
                ),
            )
            _verify_decision_identity(prepared, decision)
            return RuntimeInvocationResult(
                response_bytes=response_bytes,
                response_hash=response_hash,
                decision=decision,
                duration_ms=self._duration_ms(started, elapsed=elapsed),
            )
        except RuntimeResolutionError as exc:
            code, retryable = _classify_resolution_error(exc.code)
            return self._failure(
                code,
                started=started,
                diagnostic=str(exc),
                retryable=retryable,
                elapsed=elapsed,
            )
        except (RuntimeTimeoutError, TimeoutError) as exc:
            return self._failure(
                "runtime_timeout",
                started=started,
                diagnostic=str(exc),
                retryable=True,
                elapsed=elapsed,
            )
        except (RuntimeUnavailableError, ConnectionError, OSError) as exc:
            return self._failure(
                "runtime_unavailable",
                started=started,
                diagnostic=str(exc),
                retryable=True,
                elapsed=elapsed,
            )
        except RuntimeEnvelopeError as exc:
            if _looks_like_timeout(exc):
                return self._failure(
                    "runtime_timeout",
                    started=started,
                    diagnostic=str(exc),
                    retryable=True,
                    elapsed=elapsed,
                )
            return self._failure(
                "runtime_output_invalid",
                started=started,
                response_hash=response_hash,
                diagnostic=str(exc),
                response_bytes=response_bytes,
                elapsed=elapsed,
            )
        except RuntimeError as exc:
            if _looks_like_timeout(exc):
                return self._failure(
                    "runtime_timeout",
                    started=started,
                    diagnostic=str(exc),
                    retryable=True,
                    elapsed=elapsed,
                )
            return self._failure(
                "runtime_unavailable",
                started=started,
                diagnostic=str(exc),
                retryable=True,
                elapsed=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            return self._failure(
                "runtime_output_invalid",
                started=started,
                response_hash=response_hash,
                diagnostic=str(exc),
                response_bytes=response_bytes,
                elapsed=elapsed,
            )

    def _resolve_runtime(self, run: CollaborationRun) -> CollaborationRuntime:
        if self._runtime_resolver is not None:
            return self._runtime_resolver.resolve(run).runtime
        if (
            self._runtime_resolver_factory is not None
            and self._resolution_uow_factory is not None
        ):
            with self._resolution_uow_factory() as uow:
                resolved = self._runtime_resolver_factory(uow.modules).resolve(run)
            return resolved.runtime
        raise RuntimeResolutionError(
            "runtime_config_mismatch",
            "an exact collaboration Runtime resolver is required",
        )

    def _failure(
        self,
        code: str,
        *,
        started: float,
        diagnostic: str,
        retryable: bool = False,
        response_hash: str | None = None,
        response_bytes: bytes | None = None,
        elapsed: float | None = None,
    ) -> RuntimeInvocationFailure:
        return RuntimeInvocationFailure(
            code=code,
            response_hash=response_hash,
            diagnostic_summary=_safe_diagnostic(
                diagnostic,
                max_bytes=self._max_diagnostic_bytes,
                redaction_values=self._redaction_values,
                response_bytes=response_bytes,
            ),
            retryable=retryable,
            duration_ms=self._duration_ms(started, elapsed=elapsed),
        )

    def _elapsed_seconds(self, started: float) -> float:
        return max(0.0, self._monotonic_clock() - started)

    def _duration_ms(self, started: float, *, elapsed: float | None = None) -> int:
        return max(
            0,
            int(
                (elapsed if elapsed is not None else self._elapsed_seconds(started))
                * 1000
            ),
        )

    @staticmethod
    def _validate_now(now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")


def _canonical_response_bytes(value: object) -> bytes:
    if isinstance(value, RuntimeDecisionV1):
        return value.canonical_bytes()
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, Mapping):
        return canonical_json_bytes(value)
    raise RuntimeEnvelopeError(
        "Runtime response must be a typed Decision or JSON bytes"
    )


def _verify_decision_identity(
    prepared: PreparedCollaborationEvaluation,
    decision: RuntimeDecisionV1,
) -> None:
    if decision.operation_id != prepared.operation_id:
        raise RuntimeEnvelopeError("Runtime response operation_id does not match")
    if decision.run_id != prepared.run.id:
        raise RuntimeEnvelopeError("Runtime response run_id does not match")
    if decision.projection_id != prepared.projection.projection_id:
        raise RuntimeEnvelopeError("Runtime response projection_id does not match")
    if decision.projection_hash != prepared.projection.content_hash:
        raise RuntimeEnvelopeError("Runtime response projection_hash does not match")


def _classify_resolution_error(code: str) -> tuple[str, bool]:
    if code == "runtime_provider_unavailable":
        return "runtime_unavailable", True
    if code in {
        "runtime_capability_unsupported",
        "runtime_capability_mismatch",
        "runtime_capability_invalid",
        "runtime_capability_limit_exceeded",
        "unsupported_runtime_capability",
    }:
        return "unsupported_runtime_capability", False
    if code in {
        "runtime_config_mismatch",
        "runtime_provider_unsupported",
        "runtime_binding_missing",
        "runtime_binding_scope_mismatch",
        "runtime_definition_mismatch",
        "runtime_instance_mismatch",
        "runtime_binding_mismatch",
        "runtime_module_type_mismatch",
        "runtime_module_disabled",
        "runtime_interface_mismatch",
    }:
        return "runtime_config_mismatch", False
    return "runtime_config_mismatch", False


def _looks_like_timeout(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code in {"runtime_timeout", "timeout", "deadline_exceeded"}:
        return True
    text = str(exc).lower()
    return "deadline" in text and ("expired" in text or "exceed" in text)


def _safe_diagnostic(
    value: str,
    *,
    max_bytes: int,
    redaction_values: Iterable[str],
    response_bytes: bytes | None = None,
) -> str:
    text = value or "runtime invocation failed"
    if response_bytes:
        response_text = response_bytes.decode("utf-8", errors="replace")
        if response_text:
            text = text.replace(response_text, "[response omitted]")
    for secret in redaction_values:
        text = text.replace(secret, "[redacted]")
    text = _SECRET_ASSIGNMENT.sub("[secret redacted]", text)
    text = _BEARER_VALUE.sub("[authorization redacted]", text)
    text = _API_KEY_VALUE.sub("[provider key redacted]", text)
    text = "".join(
        character
        for character in text
        if character.isprintable() or character in {" ", "\t"}
    )
    text = " ".join(text.split()) or "runtime invocation failed"
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
