"""Cross-process operation reservations for the acceptance harness."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import ReservationResult


def reserve_action(
    manifest: Mapping[str, Any],
    scenario: str,
    role: str,
    operation_id: str,
) -> ReservationResult:
    if not operation_id or any(character.isspace() for character in operation_id):
        raise ValueError("operation_id must be a stable non-empty token")
    path = _state_path(manifest)
    with _locked(path):
        state = _read_state(path, manifest)
        existing = state["operations"].get(operation_id)
        if existing is not None:
            return ReservationResult(
                status="replay",
                operation_id=operation_id,
                scenario=existing["scenario"],
                role=existing["role"],
                started_at=existing["started_at"],
                deadline=existing["deadline"],
                diagnostic_code="operation_already_reserved",
                resource_ids=tuple(existing.get("resource_ids", ())),
            )
        limits = state["scenarios"].get(scenario)
        if not isinstance(limits, dict):
            raise TypeError(f"unknown scenario: {scenario}")
        limit_name = _limit_name(role)
        now = datetime.now(UTC)
        deadline = datetime.fromisoformat(state["deadlines"][scenario])
        if now >= deadline:
            return _blocked(operation_id, scenario, role, state, "deadline_exceeded")
        if state["counts"].get(limit_name, 0) >= int(limits.get(limit_name, 0)):
            return _blocked(operation_id, scenario, role, state, "budget_exhausted")
        result = {
            "scenario": scenario,
            "role": role,
            "started_at": now.isoformat(),
            "deadline": deadline.isoformat(),
            "status": "pending",
            "resource_ids": [],
        }
        state["operations"][operation_id] = result
        state["counts"][limit_name] = state["counts"].get(limit_name, 0) + 1
        _write_state(path, state)
        return ReservationResult(
            status="reserved",
            operation_id=operation_id,
            scenario=scenario,
            role=role,
            started_at=result["started_at"],
            deadline=result["deadline"],
            diagnostic_code="reservation_created",
        )


def record_observation(
    operation_id: str,
    outcome: str,
    resource_ids: tuple[str, ...] = (),
    *,
    manifest: Mapping[str, Any] | None = None,
) -> None:
    if manifest is None:
        raise ValueError("manifest is required to locate the run state")
    path = _state_path(manifest)
    with _locked(path):
        state = _read_state(path, manifest)
        operation = state["operations"].get(operation_id)
        if operation is None:
            raise KeyError(operation_id)
        operation["status"] = outcome
        operation["resource_ids"] = list(resource_ids)
        _write_state(path, state)


def _limit_name(role: str) -> str:
    return {
        "main_agent": "main_agent_turns",
        "planner": "planner_runs",
        "worker": "worker_sessions",
        "second_worker": "adapter_prompts",
    }.get(role, "worker_sessions")


def _blocked(
    operation_id: str,
    scenario: str,
    role: str,
    state: Mapping[str, Any],
    code: str,
) -> ReservationResult:
    return ReservationResult(
        status="blocked",
        operation_id=operation_id,
        scenario=scenario,
        role=role,
        started_at=datetime.now(UTC).isoformat(),
        deadline=str(state["deadlines"][scenario]),
        diagnostic_code=code,
    )


def _state_path(manifest: Mapping[str, Any]) -> Path:
    return Path(str(manifest["run_root"])).expanduser() / "acceptance-state.json"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    os.chmod(lock_path, 0o600)


def _read_state(path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now(UTC)
    scenarios = manifest.get("scenarios", {})
    deadlines = {
        name: (now + timedelta(seconds=int(values["max_seconds"]))).isoformat()
        for name, values in scenarios.items()
    }
    return {
        "schema_version": 1,
        "run_id": str(manifest["run_id"]),
        "scenarios": scenarios,
        "deadlines": deadlines,
        "counts": {
            "main_agent_turns": 0,
            "planner_runs": 0,
            "worker_sessions": 0,
            "adapter_prompts": 0,
        },
        "operations": {},
    }


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
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
