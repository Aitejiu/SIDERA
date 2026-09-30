"""Provider-neutral contracts for bounded acceptance runtime calls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

OperationState = Literal["reserved", "started", "completed", "failed", "unknown"]
DecisionStatus = Literal["reserved", "send", "reconcile", "blocked"]


class RuntimeGuardError(RuntimeError):
    """Raised when a runtime guard identity or ledger contract is invalid."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class RuntimeRouteIdentity:
    """The effective route facts safe to put in an acceptance ledger."""

    runtime: str
    provider: str
    endpoint_host: str
    model_id: str
    credential_ref: str
    config_version: int
    fallbacks: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> RuntimeRouteIdentity:
        from .routes import _text, _validate_credential_ref, _validate_endpoint_host

        raw_fallbacks = raw.get("fallbacks", ())
        if not isinstance(raw_fallbacks, (list, tuple)):
            raise RuntimeGuardError("route_fallbacks_invalid")
        fallbacks = tuple(
            _text(value, "route_fallbacks_invalid") for value in raw_fallbacks
        )
        if len(fallbacks) != len(set(fallbacks)):
            raise RuntimeGuardError("route_fallbacks_invalid")
        config_version = raw.get("config_version")
        if isinstance(config_version, bool) or not isinstance(config_version, int):
            raise RuntimeGuardError("route_config_version_invalid")
        if config_version < 1:
            raise RuntimeGuardError("route_config_version_invalid")
        return cls(
            runtime=_text(raw.get("runtime"), "route_runtime_invalid"),
            provider=_text(raw.get("provider"), "route_provider_invalid"),
            endpoint_host=_validate_endpoint_host(raw.get("endpoint_host")),
            model_id=_text(raw.get("model_id"), "route_model_invalid"),
            credential_ref=_validate_credential_ref(raw.get("credential_ref")),
            config_version=config_version,
            fallbacks=fallbacks,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime": self.runtime,
            "provider": self.provider,
            "endpoint_host": self.endpoint_host,
            "model_id": self.model_id,
            "credential_ref": self.credential_ref,
            "config_version": self.config_version,
            "fallbacks": list(self.fallbacks),
        }


@dataclass(frozen=True, slots=True)
class BudgetRequest:
    run_id: str
    workspace_id: str
    implementation_fingerprint: str
    runtime_fingerprint: str
    scenario: Literal["profile", "revision", "second-adapter"]
    role: Literal["main_agent", "planner", "worker", "second_worker"]
    operation_id: str
    resource_key: str
    route_id: str
    payload_hash: str
    costs: dict[str, int]
    resource_chain: tuple[str, ...] = ()
    route_identity: RuntimeRouteIdentity | None = None

    def identity(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "implementation_fingerprint": self.implementation_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "scenario": self.scenario,
            "role": self.role,
            "operation_id": self.operation_id,
            "resource_key": self.resource_key,
            "route_id": self.route_id,
            "payload_hash": self.payload_hash,
            "costs": dict(sorted(self.costs.items())),
            "resource_chain": list(self.resource_chain),
            "route_identity": (
                None
                if self.route_identity is None
                else self.route_identity.as_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    status: DecisionStatus
    operation_id: str
    code: str
    deadline: str | None
    resource_ids: tuple[str, ...] = ()
