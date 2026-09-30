"""Read-only, identity-bound observations for real acceptance batches."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .contracts import ConfirmedPlanEvidence


class LiveObservationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GroupRunObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    workspace_id: UUID
    implementation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    group: Literal["A", "B", "C"]
    root_task_id: UUID
    command_ids: tuple[UUID, ...] = ()
    resource_ids: tuple[str, ...] = ()
    source_execution_ids: tuple[UUID, ...] = ()
    integration_execution_id: UUID | None = None
    terminal_execution_ids: tuple[UUID, ...] = ()
    released_lease_execution_ids: tuple[UUID, ...] = ()
    released_worker_ids: tuple[UUID, ...] = ()
    result_candidate_id: UUID | None = None
    result_accepted: bool = False
    status: Literal["reserved", "running", "completed", "unknown"]


class ScenarioObservation(BaseModel):
    """A read-only scenario payload collected from the public runtime."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    workspace_id: UUID
    implementation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario: str = Field(min_length=1, max_length=64)
    snapshot: dict[str, object]


def load_observation(
    manifest: object,
    scenario: str,
) -> dict[str, object]:
    """Read one observation and reject stale or cross-run evidence."""

    raw_manifest = _mapping(manifest, "manifest")
    run_root = _absolute_path(raw_manifest.get("run_root"))
    path = run_root / "observations" / f"{scenario}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveObservationError("observation_missing") from exc
    try:
        observation: GroupRunObservation | ScenarioObservation
        if "snapshot" in raw:
            observation = ScenarioObservation.model_validate(raw)
        else:
            observation = GroupRunObservation.model_validate(raw)
    except ValueError as exc:
        raise LiveObservationError("observation_invalid") from exc
    for field in (
        "run_id",
        "workspace_id",
        "implementation_fingerprint",
        "runtime_fingerprint",
    ):
        if str(getattr(observation, field)) != str(raw_manifest.get(field)):
            raise LiveObservationError("observation_identity_mismatch")
    if isinstance(observation, ScenarioObservation) and observation.scenario != scenario:
        raise LiveObservationError("observation_scenario_mismatch")
    return observation.model_dump(mode="json")


def ensure_group_run(
    group: Literal["A", "B", "C"],
    manifest: object,
) -> GroupRunObservation:
    """Return an already-created group fact; never invent a root task."""

    if group not in {"A", "B", "C"}:
        raise LiveObservationError("unknown_group")
    raw = load_observation(manifest, f"group-{group}")
    try:
        observation = GroupRunObservation.model_validate(raw)
    except ValueError as exc:
        raise LiveObservationError("group_observation_invalid") from exc
    if observation.group != group:
        raise LiveObservationError("group_observation_group_mismatch")
    return observation


def record_group_run(
    manifest: Mapping[str, object],
    confirmed_plan: ConfirmedPlanEvidence,
    *,
    group: Literal["A", "B", "C"],
    status: Literal["reserved", "running", "completed", "unknown"] = "completed",
) -> Path:
    """Persist one group identity from confirmed public API facts.

    The manifest supplies only the run/runtime identity. Root, command, planning
    request and proposal IDs come from ``ConfirmedPlanEvidence``, which is built
    from public API reads rather than an operator-authored snapshot.
    """

    if confirmed_plan.run_id != _uuid(manifest.get("run_id"), "run_id"):
        raise LiveObservationError("group_observation_run_id_mismatch")
    if confirmed_plan.workspace_id != _uuid(manifest.get("workspace_id"), "workspace_id"):
        raise LiveObservationError("group_observation_workspace_id_mismatch")
    try:
        observation = GroupRunObservation(
            run_id=confirmed_plan.run_id,
            workspace_id=confirmed_plan.workspace_id,
            implementation_fingerprint=str(manifest["implementation_fingerprint"]),
            runtime_fingerprint=str(manifest["runtime_fingerprint"]),
            group=group,
            root_task_id=confirmed_plan.root_task_id,
            command_ids=(confirmed_plan.confirmation_command_id,),
            resource_ids=(
                f"planning_request:{confirmed_plan.planning_request_id}",
                f"proposal:{confirmed_plan.proposal_id}",
                f"root_task:{confirmed_plan.root_task_id}",
            ),
            status=status,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveObservationError("group_observation_facts_invalid") from exc

    path = _observation_path(manifest, f"group-{group}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with _observation_lock(path):
        if path.exists():
            existing = _read_group_observation(path)
            if existing != observation:
                raise LiveObservationError("observation_conflict")
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise LiveObservationError("observation_permissions_invalid")
            return path
        _write_observation(path, observation)
    return path


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LiveObservationError(f"{label}_invalid")
    return value


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise LiveObservationError("run_root_invalid")
    return Path(value).expanduser()


def _observation_path(manifest: Mapping[str, object], scenario: str) -> Path:
    return _absolute_path(manifest.get("run_root")) / "observations" / f"{scenario}.json"


def _read_group_observation(path: Path) -> GroupRunObservation:
    try:
        return GroupRunObservation.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise LiveObservationError("observation_existing_invalid") from exc


def _write_observation(path: Path, observation: GroupRunObservation) -> None:
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(observation.model_dump(mode="json"), handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


@contextmanager
def _observation_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    os.chmod(lock_path, 0o600)


def _uuid(value: object, name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise LiveObservationError(f"{name}_invalid") from exc


__all__ = [
    "GroupRunObservation",
    "LiveObservationError",
    "ScenarioObservation",
    "ensure_group_run",
    "load_observation",
    "record_group_run",
]
