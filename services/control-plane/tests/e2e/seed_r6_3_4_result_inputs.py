"""Seed only the durable pre-confirmation state for the R6.3.4 E2E loop."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import WorkspaceModel
from tests.integration.result_input_scenario import (
    PendingResultInputSeed,
    ResultInputScenario,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
SOURCE_BODIES: Mapping[str, bytes] = {
    "a": b"R6.3.4 source A\nverified=a\n",
    "b": b"R6.3.4 source B\nverified=b\n",
    "c": b"R6.3.4 source C\nverified=c\n",
}
SOURCE_FILENAMES: Mapping[str, str] = {
    key: f"source-{key}.txt" for key in SOURCE_BODIES
}


@dataclass(frozen=True, slots=True)
class ResultInputSeed:
    pending: PendingResultInputSeed
    source_files: Mapping[str, Path]

    def to_mapping(self) -> dict[str, object]:
        return {
            "pending": self.pending.to_mapping(),
            "source_files": {
                key: str(path) for key, path in self.source_files.items()
            },
        }


def seed_result_input_plan(
    database: Database,
    workspace_id: UUID,
    source_root: Path,
    *,
    now: datetime = NOW,
    groups: tuple[dict[str, object], ...] | None = None,
    hard_edges: tuple[tuple[str, str], ...] = (),
    browser_state: bool = False,
) -> tuple[ResultInputSeed, ResultInputScenario]:
    """Create a V5 proposal without confirming or starting any work."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("fixture timestamp must be timezone-aware")
    _ensure_workspace(database, workspace_id, now)

    scenario = ResultInputScenario(database, workspace_id, source_root.parent)
    scenario.now = now
    pending = scenario.prepare_plan(
        groups=groups,
        hard_edges=hard_edges,
        max_attempts=2,
        browser_state=browser_state,
    )

    source_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_files: dict[str, Path] = {}
    for key, body in SOURCE_BODIES.items():
        path = source_root / SOURCE_FILENAMES[key]
        path.write_bytes(body)
        path.chmod(0o600)
        source_files[key] = path
    return ResultInputSeed(pending=pending, source_files=source_files), scenario


def pending_from_mapping(value: object) -> PendingResultInputSeed:
    if not isinstance(value, Mapping):
        raise TypeError("pending seed must be an object")

    def required_uuid(name: str) -> UUID:
        raw = value.get(name)
        if not isinstance(raw, str):
            raise TypeError(f"pending seed field {name!r} must be a UUID string")
        try:
            return UUID(raw)
        except ValueError as exc:
            raise ValueError(f"pending seed field {name!r} is invalid") from exc

    def required_int(name: str) -> int:
        raw = value.get(name)
        if type(raw) is not int or raw < 1:
            raise ValueError(f"pending seed field {name!r} must be a positive integer")
        return raw

    content_hash = value.get("proposal_content_hash")
    if not isinstance(content_hash, str) or len(content_hash) != 64:
        raise ValueError("pending seed proposal_content_hash is invalid")
    return PendingResultInputSeed(
        workspace_id=required_uuid("workspace_id"),
        planning_request_id=required_uuid("planning_request_id"),
        proposal_id=required_uuid("proposal_id"),
        proposal_revision=required_int("proposal_revision"),
        proposal_content_hash=content_hash,
        planning_profile_id=required_uuid("planning_profile_id"),
        planning_profile_version=required_int("planning_profile_version"),
        expected_request_version=required_int("expected_request_version"),
    )


def _ensure_workspace(database: Database, workspace_id: UUID, now: datetime) -> None:
    with database.session() as session:
        if session.get(WorkspaceModel, workspace_id) is None:
            session.add(
                WorkspaceModel(
                    id=workspace_id,
                    name="SIDERA R6.3.4 Result Inputs",
                    status="active",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
