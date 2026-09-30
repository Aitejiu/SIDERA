"""Process-boundary helpers for provider-free and approved acceptance runs."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from sidera_runtime_guard import (
    RuntimeBudgetGuard,
    RuntimeGuardError,
    route_binding_from_env,
)


def load_acceptance_guard() -> RuntimeBudgetGuard | None:
    """Load the immutable acceptance ledger when this process is in a run."""

    required = (
        os.environ.get("SIDERA_ACCEPTANCE_GUARD_REQUIRED") == "1"
        or bool(os.environ.get("SIDERA_R6_BASIC_RUN_ID"))
    )
    manifest_ref = os.environ.get("SIDERA_ACCEPTANCE_MANIFEST")
    if not required and not manifest_ref:
        return None
    if not manifest_ref:
        raise RuntimeGuardError("acceptance_manifest_missing")
    guard = RuntimeBudgetGuard(Path(manifest_ref))
    expected_run_id = os.environ.get("SIDERA_R6_BASIC_RUN_ID")
    if expected_run_id is not None and expected_run_id != guard.run_id:
        raise RuntimeGuardError("budget_identity_mismatch")
    return guard


def payload_hash(value: str) -> str:
    """Hash a payload without retaining its text in the acceptance ledger."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def begin_call(
    guard: RuntimeBudgetGuard | None,
    *,
    scenario: str | None,
    role: str,
    operation_id: str,
    resource_key: str,
    payload_digest: str,
    route_id: str | None = None,
    resource_chain: tuple[str, ...] = (),
    costs: Mapping[str, int] | None = None,
) -> str | None:
    """Begin exactly one guarded external call and return its operation id."""

    if guard is None:
        return None
    binding = route_binding_from_env(
        role,
        route_id=route_id,
        required=guard.resource_binding_mode == "persisted",
    )
    if binding is not None and route_id is None:
        route_id = binding.route_id
    request = guard.build_request(
        scenario=scenario or os.environ.get("SIDERA_ACCEPTANCE_SCENARIO", "profile"),
        role=role,
        operation_id=operation_id,
        resource_key=resource_key,
        payload_hash=payload_digest,
        route_id=route_id,
        resource_chain=resource_chain,
        route_identity=None if binding is None else binding.identity,
        costs=costs,
    )
    decision = guard.begin(request)
    if decision.status != "send":
        raise RuntimeGuardError(decision.code)
    return operation_id


def observe_call(
    guard: RuntimeBudgetGuard | None,
    operation_id: str | None,
    *,
    outcome: str,
    resource_ids: tuple[str, ...] = (),
) -> None:
    if guard is None or operation_id is None:
        return
    guard.observe(operation_id, outcome=outcome, resource_ids=resource_ids)


def route_id_from_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


__all__ = [
    "begin_call",
    "load_acceptance_guard",
    "observe_call",
    "payload_hash",
    "route_id_from_env",
]
