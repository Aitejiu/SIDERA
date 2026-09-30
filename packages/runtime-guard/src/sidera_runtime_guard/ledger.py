"""Atomic, provider-neutral acceptance budget ledger."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import (
    BudgetDecision,
    BudgetRequest,
    RuntimeGuardError,
    RuntimeRouteIdentity,
)

_COUNT_NAMES = (
    "main_agent_turns",
    "planner_runs",
    "worker_sessions",
    "adapter_prompts",
)
_ROLE_COST = {
    "main_agent": "main_agent_turns",
    "planner": "planner_runs",
    "worker": "worker_sessions",
    "second_worker": "adapter_prompts",
}
_SCENARIOS = frozenset({"profile", "revision", "second-adapter"})
_STATE_KEYS = (
    "schema_version",
    "run_id",
    "workspace_id",
    "implementation_fingerprint",
    "runtime_fingerprint",
    "manifest_hash",
    "operations",
    "resource_bindings",
    "scenario_counts",
    "batch_counts",
    "deadlines",
)
_BASIC_MANIFEST_FIELDS = frozenset(
    {
        "local_gate_record",
        "repository",
        "base_commit",
        "database_ref",
        "knowledge_database_ref",
        "control_plane_url",
        "knowledge_url",
        "main_agent_gateway_url",
        "web_url",
        "profiles",
        "notification_route_ref",
        "fingerprint_version",
        "maintenance_inventory_ref",
        "restore_mode",
        "resource_binding_mode",
    }
)


def prepare_run(manifest_path: Path) -> dict[str, object]:
    """Create the only valid empty ledger, or return an existing one."""

    manifest_path = _absolute_path(manifest_path, "manifest")
    manifest = _read_manifest(manifest_path)
    run_root = Path(str(manifest["run_root"]))
    run_root.mkdir(parents=True, exist_ok=True)
    os.chmod(run_root, 0o700)
    ledger_path = run_root / "acceptance-state.json"
    marker_path = run_root / "acceptance-state.initialized.json"
    manifest_hash = _hash_file(manifest_path)
    with _locked(ledger_path):
        if marker_path.exists():
            if not ledger_path.exists():
                raise RuntimeGuardError("ledger_missing")
            state = _read_state(ledger_path)
            _validate_state(state, manifest, manifest_hash)
            return {
                "status": "existing",
                "run_id": str(manifest["run_id"]),
                "ledger_ref": str(ledger_path),
            }
        if ledger_path.exists():
            raise RuntimeGuardError("initialization_marker_missing")
        _validate_approval(manifest, manifest_hash)
        marker = {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "manifest_hash": manifest_hash,
        }
        _atomic_json_write(marker_path, marker)
        state = _empty_state(manifest, manifest_hash)
        _atomic_json_write(ledger_path, state)
    return {
        "status": "prepared",
        "run_id": str(manifest["run_id"]),
        "ledger_ref": str(ledger_path),
    }


class RuntimeBudgetGuard:
    """Guard all externally visible calls in one acceptance run."""

    def __init__(
        self,
        manifest_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.manifest_path = _absolute_path(manifest_path, "manifest")
        self._manifest = _read_manifest(self.manifest_path)
        self._manifest_hash = _hash_file(self.manifest_path)
        self._run_root = Path(str(self._manifest["run_root"]))
        self._ledger_path = self._run_root / "acceptance-state.json"
        self._marker_path = self._run_root / "acceptance-state.initialized.json"
        self._clock = clock or (lambda: datetime.now(UTC))
        if not self._marker_path.exists():
            raise RuntimeGuardError("initialization_marker_missing")
        if not self._ledger_path.exists():
            raise RuntimeGuardError("ledger_missing")
        _validate_approval(self._manifest, self._manifest_hash)
        _validate_state(
            _read_state(self._ledger_path),
            self._manifest,
            self._manifest_hash,
        )

    def build_request(
        self,
        *,
        scenario: str,
        role: str,
        operation_id: str,
        resource_key: str,
        payload_hash: str,
        route_id: str | None = None,
        resource_chain: tuple[str, ...] = (),
        route_identity: RuntimeRouteIdentity | None = None,
        costs: Mapping[str, int] | None = None,
    ) -> BudgetRequest:
        routes = [
            route
            for route in self._manifest["allowed_routes"]
            if route["role"] == role
            and (route_id is None or route["route_id"] == route_id)
        ]
        if len(routes) != 1:
            raise RuntimeGuardError("route_not_allowed")
        route = routes[0]
        request_costs = dict(route["costs"] if costs is None else costs)
        return BudgetRequest(
            run_id=str(self._manifest["run_id"]),
            workspace_id=str(self._manifest["workspace_id"]),
            implementation_fingerprint=str(
                self._manifest["implementation_fingerprint"]
            ),
            runtime_fingerprint=str(self._manifest["runtime_fingerprint"]),
            scenario=scenario,  # type: ignore[arg-type]
            role=role,  # type: ignore[arg-type]
            operation_id=operation_id,
            resource_key=resource_key,
            route_id=str(route["route_id"]),
            payload_hash=payload_hash,
            costs=request_costs,
            resource_chain=tuple(resource_chain),
            route_identity=route_identity,
        )

    @property
    def run_id(self) -> str:
        return str(self._manifest["run_id"])

    @property
    def workspace_id(self) -> str:
        return str(self._manifest["workspace_id"])

    @property
    def resource_binding_mode(self) -> str:
        return str(self._manifest.get("resource_binding_mode", "legacy"))

    def reserve(self, request: BudgetRequest) -> BudgetDecision:
        with _locked(self._ledger_path):
            state = self._read_valid_state()
            return self._reserve_locked(state, request)

    def begin(self, request: BudgetRequest) -> BudgetDecision:
        with _locked(self._ledger_path):
            state = self._read_valid_state()
            _validate_request(request, self._manifest)
            _validate_route(request, self._manifest)
            _validate_resource_binding(state, request, self._manifest)
            existing = state["operations"].get(request.operation_id)
            if existing is not None:
                _assert_operation_identity(existing, request)
                existing_state = existing["state"]
                if existing_state != "reserved":
                    return BudgetDecision(
                        status="reconcile",
                        operation_id=request.operation_id,
                        code="operation_already_started",
                        deadline=existing["deadline"],
                        resource_ids=tuple(existing["resource_ids"]),
                    )
                now = _now(self._clock)
                if now >= _parse_datetime(existing["deadline"]):
                    return BudgetDecision(
                        status="blocked",
                        operation_id=request.operation_id,
                        code="deadline_exceeded",
                        deadline=existing["deadline"],
                    )
                existing["state"] = "started"
                existing["started_at"] = now.isoformat()
                _atomic_json_write(self._ledger_path, state)
                return BudgetDecision(
                    status="send",
                    operation_id=request.operation_id,
                    code="operation_started",
                    deadline=existing["deadline"],
                )
            decision = self._reserve_locked(state, request, write=False)
            if decision.status != "reserved":
                return decision
            operation = state["operations"][request.operation_id]
            operation["state"] = "started"
            operation["started_at"] = _now(self._clock).isoformat()
            _atomic_json_write(self._ledger_path, state)
            return BudgetDecision(
                status="send",
                operation_id=request.operation_id,
                code="operation_started",
                deadline=operation["deadline"],
            )

    def observe(
        self,
        operation_id: str,
        *,
        outcome: str,
        resource_ids: tuple[str, ...] = (),
    ) -> None:
        if outcome not in {"completed", "failed", "unknown"}:
            raise RuntimeGuardError("outcome_invalid")
        with _locked(self._ledger_path):
            state = self._read_valid_state()
            operation = state["operations"].get(operation_id)
            if operation is None:
                raise RuntimeGuardError("operation_missing")
            if any(not isinstance(value, str) or not value for value in resource_ids):
                raise RuntimeGuardError("resource_id_invalid")
            if operation["state"] in {"completed", "failed", "unknown"}:
                if (
                    operation["state"] != outcome
                    or tuple(operation["resource_ids"]) != resource_ids
                ):
                    raise RuntimeGuardError("operation_observation_conflict")
                return
            operation["state"] = outcome
            operation["resource_ids"] = list(resource_ids)
            operation["observed_at"] = _now(self._clock).isoformat()
            _atomic_json_write(self._ledger_path, state)

    def bind_thread(
        self,
        thread_id: str,
        *,
        group: str,
        resource_chain: tuple[str, ...] = (),
    ) -> None:
        if group not in {"A", "B", "C"}:
            raise RuntimeGuardError("group_invalid")
        if not thread_id:
            raise RuntimeGuardError("thread_id_invalid")
        with _locked(self._ledger_path):
            state = self._read_valid_state()
            bindings = state["resource_bindings"]
            existing = bindings.get(thread_id)
            value: dict[str, object] = {"kind": "thread", "group": group}
            if resource_chain:
                value["chain"] = list(resource_chain)
            if existing is not None and existing != value:
                raise RuntimeGuardError("thread_binding_conflict")
            bindings[thread_id] = value
            _atomic_json_write(self._ledger_path, state)

    def bind_resource(
        self,
        parent_key: str,
        resource_key: str,
        *,
        resource_chain: tuple[str, ...] = (),
    ) -> None:
        if not parent_key or not resource_key:
            raise RuntimeGuardError("resource_binding_invalid")
        with _locked(self._ledger_path):
            state = self._read_valid_state()
            bindings = state["resource_bindings"]
            existing = bindings.get(resource_key)
            value: dict[str, object] = {"kind": "resource", "parent": parent_key}
            if resource_chain:
                value["chain"] = list(resource_chain)
            if existing is not None and existing != value:
                raise RuntimeGuardError("resource_binding_conflict")
            bindings[resource_key] = value
            _atomic_json_write(self._ledger_path, state)

    def assert_resource_bound(
        self,
        resource_key: str,
        *,
        resource_chain: tuple[str, ...],
    ) -> None:
        if not resource_key or not resource_chain:
            raise RuntimeGuardError("resource_chain_missing")
        with _locked(self._ledger_path):
            state = self._read_valid_state()
            binding = state["resource_bindings"].get(resource_key)
            if not isinstance(binding, dict):
                raise RuntimeGuardError("resource_binding_missing")
            if binding.get("chain") != list(resource_chain):
                raise RuntimeGuardError("resource_binding_conflict")

    def _read_valid_state(self) -> dict[str, Any]:
        state = _read_state(self._ledger_path)
        _validate_state(state, self._manifest, self._manifest_hash)
        return state

    def _reserve_locked(
        self,
        state: dict[str, Any],
        request: BudgetRequest,
        *,
        write: bool = True,
    ) -> BudgetDecision:
        _validate_request(request, self._manifest)
        _validate_route(request, self._manifest)
        _validate_resource_binding(state, request, self._manifest)
        existing = state["operations"].get(request.operation_id)
        if existing is not None:
            _assert_operation_identity(existing, request)
            if existing["state"] == "reserved":
                return BudgetDecision(
                    status="reserved",
                    operation_id=request.operation_id,
                    code="operation_already_reserved",
                    deadline=existing["deadline"],
                    resource_ids=tuple(existing["resource_ids"]),
                )
            return BudgetDecision(
                status="reconcile",
                operation_id=request.operation_id,
                code="operation_already_started",
                deadline=existing["deadline"],
                resource_ids=tuple(existing["resource_ids"]),
            )
        scenario = request.scenario
        scenario_config = self._manifest["scenarios"].get(scenario)
        if not isinstance(scenario_config, Mapping):
            raise RuntimeGuardError("budget_scope_missing")
        now = _now(self._clock)
        deadline = state["deadlines"].get(scenario)
        if deadline is None:
            deadline = (now + timedelta(seconds=int(scenario_config["max_seconds"]))).isoformat()
        if now >= _parse_datetime(str(deadline)):
            return BudgetDecision(
                status="blocked",
                operation_id=request.operation_id,
                code="deadline_exceeded",
                deadline=str(deadline),
            )
        scenario_limits = scenario_config.get("limits", {})
        batch_limits = self._manifest["batch_limits"]
        for name, amount in request.costs.items():
            if (
                state["batch_counts"].get(name, 0) + amount
                > int(batch_limits.get(name, 0))
                or state["scenario_counts"][scenario].get(name, 0) + amount
                > int(scenario_limits.get(name, 0))
            ):
                return BudgetDecision(
                    status="blocked",
                    operation_id=request.operation_id,
                    code="budget_exhausted",
                    deadline=str(deadline),
                )
        operation = {
            **request.identity(),
            "state": "reserved",
            "reserved_at": now.isoformat(),
            "started_at": None,
            "deadline": str(deadline),
            "resource_ids": [],
        }
        state["operations"][request.operation_id] = operation
        state["deadlines"][scenario] = str(deadline)
        for name, amount in request.costs.items():
            state["batch_counts"][name] += amount
            state["scenario_counts"][scenario][name] += amount
        if write:
            _atomic_json_write(self._ledger_path, state)
        return BudgetDecision(
            status="reserved",
            operation_id=request.operation_id,
            code="reservation_created",
            deadline=str(deadline),
        )


def _validate_request(request: BudgetRequest, manifest: Mapping[str, Any]) -> None:
    if (
        request.run_id != manifest["run_id"]
        or request.workspace_id != manifest["workspace_id"]
        or request.implementation_fingerprint != manifest["implementation_fingerprint"]
        or request.runtime_fingerprint != manifest["runtime_fingerprint"]
    ):
        raise RuntimeGuardError("budget_identity_mismatch")
    if request.scenario not in _SCENARIOS:
        raise RuntimeGuardError("budget_scope_missing")
    if not request.operation_id or not request.resource_key:
        raise RuntimeGuardError("operation_identity_invalid")
    if len(request.payload_hash) != 64 or any(
        char not in "0123456789abcdef" for char in request.payload_hash
    ):
        raise RuntimeGuardError("payload_hash_invalid")
    if not request.costs or any(
        name not in _COUNT_NAMES or not isinstance(amount, int) or amount < 0
        for name, amount in request.costs.items()
    ):
        raise RuntimeGuardError("costs_invalid")
    if any(
        not isinstance(value, str) or not value for value in request.resource_chain
    ) or len(request.resource_chain) != len(set(request.resource_chain)):
        raise RuntimeGuardError("resource_chain_invalid")


def _validate_resource_binding(
    state: Mapping[str, Any],
    request: BudgetRequest,
    manifest: Mapping[str, Any],
) -> None:
    if manifest.get("resource_binding_mode", "legacy") != "persisted":
        return
    if not request.resource_chain:
        raise RuntimeGuardError("resource_chain_missing")
    binding = state["resource_bindings"].get(request.resource_key)
    if not isinstance(binding, Mapping):
        raise RuntimeGuardError("resource_binding_missing")
    if binding.get("chain") != list(request.resource_chain):
        raise RuntimeGuardError("resource_binding_conflict")


def _validate_route(request: BudgetRequest, manifest: Mapping[str, Any]) -> None:
    route = next(
        (
            item
            for item in manifest["allowed_routes"]
            if item["route_id"] == request.route_id
        ),
        None,
    )
    if route is None or route["role"] != request.role:
        raise RuntimeGuardError("route_not_allowed")
    route_costs = dict(route["costs"])
    if any(
        name not in route_costs or amount > route_costs[name]
        for name, amount in request.costs.items()
    ):
        raise RuntimeGuardError("operation_identity_conflict")
    if request.route_identity is None:
        return
    expected = {
        key: route[key]
        for key in (
            "runtime",
            "provider",
            "endpoint_host",
            "model_id",
            "credential_ref",
            "config_version",
            "fallbacks",
        )
    }
    if expected != request.route_identity.as_dict():
        raise RuntimeGuardError("route_configuration_mismatch")


def _assert_operation_identity(
    operation: Mapping[str, Any],
    request: BudgetRequest,
) -> None:
    identity = request.identity()
    stored = {
        key: operation.get(key)
        for key in identity
    }
    if stored != identity:
        raise RuntimeGuardError("operation_identity_conflict")


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeGuardError("manifest_invalid") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        raise RuntimeGuardError("manifest_schema_unsupported")
    required = {
        "schema_version",
        "run_id",
        "workspace_id",
        "implementation_fingerprint",
        "runtime_fingerprint",
        "run_root",
        "approval_ref",
        "batch_limits",
        "scenarios",
        "allowed_routes",
    }
    raw_keys = set(raw)
    if not required.issubset(raw_keys) or not raw_keys - required <= _BASIC_MANIFEST_FIELDS:
        raise RuntimeGuardError("manifest_fields_invalid")
    if raw_keys - required - {"resource_binding_mode"}:
        raw = _normalize_basic_manifest(raw, required)
    if not isinstance(raw["run_root"], str) or not Path(raw["run_root"]).is_absolute():
        raise RuntimeGuardError("run_root_invalid")
    if raw.get("resource_binding_mode", "legacy") not in {"legacy", "persisted"}:
        raise RuntimeGuardError("manifest_fields_invalid")
    if not isinstance(raw["batch_limits"], dict) or not isinstance(
        raw["scenarios"], dict
    ) or not isinstance(raw["allowed_routes"], list):
        raise RuntimeGuardError("manifest_fields_invalid")
    for value in (raw["implementation_fingerprint"], raw["runtime_fingerprint"]):
        if not isinstance(value, str) or len(value) != 64:
            raise RuntimeGuardError("manifest_identity_invalid")
    if not raw["allowed_routes"]:
        raise RuntimeGuardError("route_not_allowed")
    for name, config in raw["scenarios"].items():
        if name not in _SCENARIOS or not isinstance(config, dict):
            raise RuntimeGuardError("budget_scope_missing")
        if set(config) != {"max_seconds", "limits"}:
            raise RuntimeGuardError("manifest_fields_invalid")
        if not isinstance(config["max_seconds"], int) or config["max_seconds"] <= 0:
            raise RuntimeGuardError("deadline_invalid")
        if not isinstance(config["limits"], dict):
            raise RuntimeGuardError("manifest_fields_invalid")
    for name in _COUNT_NAMES:
        value = raw["batch_limits"].get(name)
        if not isinstance(value, int) or value < 0:
            raise RuntimeGuardError("batch_limits_invalid")
    return raw


def _normalize_basic_manifest(
    raw: dict[str, Any], required: set[str]
) -> dict[str, Any]:
    """Project the public schema 2 manifest onto the guard's compact contract."""

    if raw.get("fingerprint_version") != 2 or not isinstance(raw.get("profiles"), list):
        raise RuntimeGuardError("manifest_fields_invalid")
    scenarios = raw.get("scenarios")
    if not isinstance(scenarios, dict):
        raise RuntimeGuardError("manifest_fields_invalid")
    normalized_scenarios: dict[str, dict[str, Any]] = {}
    for name in _SCENARIOS:
        value = scenarios.get(name)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise RuntimeGuardError("manifest_fields_invalid")
        expected = set(_COUNT_NAMES) | {"max_seconds"}
        if set(value) != expected:
            raise RuntimeGuardError("manifest_fields_invalid")
        limits = {count_name: value[count_name] for count_name in _COUNT_NAMES}
        normalized_scenarios[name] = {
            "max_seconds": value["max_seconds"],
            "limits": limits,
        }
    if not normalized_scenarios:
        raise RuntimeGuardError("budget_scope_missing")
    normalized = {
        key: raw[key]
        for key in required
        if key not in {"scenarios"}
    } | {"scenarios": normalized_scenarios}
    if "resource_binding_mode" in raw:
        normalized["resource_binding_mode"] = raw["resource_binding_mode"]
    return normalized


def _validate_approval(manifest: Mapping[str, Any], manifest_hash: str) -> None:
    reference = manifest["approval_ref"]
    path = Path(str(reference))
    if not path.is_absolute():
        path = Path(str(manifest["run_root"])) / path
    run_root = Path(str(manifest["run_root"])).resolve(strict=False)
    resolved = path.resolve(strict=False)
    if resolved != run_root and run_root not in resolved.parents:
        raise RuntimeGuardError("approval_scope_invalid")
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeGuardError("approval_missing") from exc
    if not isinstance(raw, dict) or raw.get("approved") is not True:
        raise RuntimeGuardError("approval_missing")
    if raw.get("run_id") != manifest["run_id"] or raw.get("workspace_id") != manifest["workspace_id"]:
        raise RuntimeGuardError("approval_identity_mismatch")
    if raw.get("implementation_fingerprint") != manifest["implementation_fingerprint"]:
        raise RuntimeGuardError("approval_identity_mismatch")
    if raw.get("runtime_fingerprint") != manifest["runtime_fingerprint"]:
        raise RuntimeGuardError("approval_identity_mismatch")
    if raw.get("manifest_hash") not in {None, manifest_hash}:
        raise RuntimeGuardError("approval_identity_mismatch")


def _empty_state(manifest: Mapping[str, Any], manifest_hash: str) -> dict[str, Any]:
    zero = {name: 0 for name in _COUNT_NAMES}
    scenario_counts = {
        name: dict(zero)
        for name in manifest["scenarios"]
    }
    return {
        "schema_version": 2,
        "run_id": manifest["run_id"],
        "workspace_id": manifest["workspace_id"],
        "implementation_fingerprint": manifest["implementation_fingerprint"],
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "manifest_hash": manifest_hash,
        "operations": {},
        "resource_bindings": {},
        "scenario_counts": scenario_counts,
        "batch_counts": zero,
        "deadlines": {},
    }


def _validate_state(
    state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_hash: str,
) -> None:
    if set(state) != set(_STATE_KEYS):
        raise RuntimeGuardError("ledger_invalid")
    if (
        state["schema_version"] != 2
        or state["run_id"] != manifest["run_id"]
        or state["workspace_id"] != manifest["workspace_id"]
        or state["implementation_fingerprint"] != manifest["implementation_fingerprint"]
        or state["runtime_fingerprint"] != manifest["runtime_fingerprint"]
        or state["manifest_hash"] != manifest_hash
    ):
        raise RuntimeGuardError("ledger_identity_mismatch")
    if not isinstance(state["operations"], dict) or not isinstance(
        state["resource_bindings"], dict
    ) or not isinstance(state["scenario_counts"], dict) or not isinstance(
        state["batch_counts"], dict
    ) or not isinstance(state["deadlines"], dict):
        raise RuntimeGuardError("ledger_invalid")
    if set(state["batch_counts"]) != set(_COUNT_NAMES):
        raise RuntimeGuardError("ledger_invalid")
    for value in state["batch_counts"].values():
        if not isinstance(value, int) or value < 0:
            raise RuntimeGuardError("ledger_invalid")
    if set(state["scenario_counts"]) != set(manifest["scenarios"]):
        raise RuntimeGuardError("ledger_invalid")


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeGuardError("ledger_invalid") from exc
    if not isinstance(raw, dict):
        raise RuntimeGuardError("ledger_invalid")
    return raw


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _absolute_path(path: Path, name: str) -> Path:
    value = path.expanduser()
    if not value.is_absolute():
        raise RuntimeGuardError(f"{name}_path_invalid")
    return value


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeGuardError("manifest_unavailable") from exc


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeGuardError("clock_invalid")
    return value.astimezone(UTC)


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeGuardError("deadline_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeGuardError("deadline_invalid")
    return parsed.astimezone(UTC)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    os.chmod(lock_path, 0o600)


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
