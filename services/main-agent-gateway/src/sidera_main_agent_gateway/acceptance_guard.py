"""Optional process-boundary guard for real acceptance calls."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sidera_runtime_guard import (
    RuntimeBudgetGuard,
    RuntimeGuardError,
    route_binding_from_env,
)

from .errors import AcceptanceGuardRejected


def load_acceptance_guard() -> RuntimeBudgetGuard | None:
    required = (
        os.environ.get("SIDERA_ACCEPTANCE_GUARD_REQUIRED") == "1"
        or bool(os.environ.get("SIDERA_R6_BASIC_RUN_ID"))
    )
    manifest_ref = os.environ.get("SIDERA_ACCEPTANCE_MANIFEST")
    if not required and not manifest_ref:
        return None
    if not manifest_ref:
        raise AcceptanceGuardRejected("acceptance_manifest_missing")
    try:
        guard = RuntimeBudgetGuard(Path(manifest_ref))
        expected_run_id = os.environ.get("SIDERA_R6_BASIC_RUN_ID")
        if expected_run_id is not None and expected_run_id != guard.run_id:
            raise RuntimeGuardError("budget_identity_mismatch")
        return guard
    except RuntimeGuardError as exc:
        raise AcceptanceGuardRejected(exc.code) from exc


def begin_main_turn(
    guard: RuntimeBudgetGuard | None,
    *,
    operation_id: str | None,
    thread_id: str,
    input_text: str,
) -> tuple[RuntimeBudgetGuard | None, str | None]:
    if guard is None:
        return None, None
    if operation_id is None:
        raise AcceptanceGuardRejected("operation_id_required")
    try:
        resource_chain: tuple[str, ...] = ()
        if guard.resource_binding_mode == "persisted":
            resource_chain = _bind_thread_resource(guard, thread_id)
        route_id = os.environ.get("SIDERA_ACCEPTANCE_MAIN_AGENT_ROUTE_ID")
        binding = route_binding_from_env(
            "main_agent",
            route_id=route_id,
            required=guard.resource_binding_mode == "persisted",
        )
        if binding is not None and route_id is None:
            route_id = binding.route_id
        request = guard.build_request(
            scenario=os.environ.get("SIDERA_ACCEPTANCE_SCENARIO", "profile"),
            role="main_agent",
            operation_id=operation_id,
            resource_key=f"thread:{thread_id}",
            payload_hash=hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
            route_id=route_id,
            resource_chain=resource_chain,
            route_identity=None if binding is None else binding.identity,
        )
        decision = guard.begin(request)
    except RuntimeGuardError as exc:
        raise AcceptanceGuardRejected(exc.code) from exc
    if decision.status != "send":
        raise AcceptanceGuardRejected(decision.code)
    return guard, operation_id


def _bind_thread_resource(
    guard: RuntimeBudgetGuard,
    thread_id: str,
) -> tuple[str, ...]:
    scenario = os.environ.get("SIDERA_ACCEPTANCE_SCENARIO", "profile")
    group = {
        "profile": "A",
        "revision": "B",
        "second-adapter": "C",
    }.get(scenario)
    if group is None:
        raise AcceptanceGuardRejected("resource_scenario_invalid")
    chain = (f"workspace:{guard.workspace_id}", f"thread:{thread_id}")
    try:
        guard.bind_thread(thread_id, group=group, resource_chain=chain)
        guard.bind_resource(
            chain[0],
            chain[1],
            resource_chain=chain,
        )
    except RuntimeGuardError as exc:
        raise AcceptanceGuardRejected(exc.code) from exc
    return chain


def settle_main_turn(
    guard: RuntimeBudgetGuard | None,
    operation_id: str | None,
    *,
    outcome: str,
    resource_id: str | None = None,
) -> None:
    if guard is None or operation_id is None:
        return
    try:
        guard.observe(
            operation_id,
            outcome=outcome,
            resource_ids=() if resource_id is None else (resource_id,),
        )
    except RuntimeGuardError as exc:
        raise AcceptanceGuardRejected(exc.code) from exc


__all__ = ["begin_main_turn", "load_acceptance_guard", "settle_main_turn"]
