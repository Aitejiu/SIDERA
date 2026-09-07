"""Deterministic CLI driver for the R6.3.4 result-input E2E loop."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CollaborationInputSelectionModel,
    ContextPackageAcknowledgementModel,
    ExecutionModel,
    RuntimeReleaseModel,
)
from tests.e2e.seed_r6_3_4_result_inputs import (
    SOURCE_BODIES,
    ResultInputSeed,
    pending_from_mapping,
    seed_result_input_plan,
)
from tests.integration.result_input_scenario import ResultInputScenario

ACTIONS = (
    "seed",
    "prepare-browser",
    "finish-late-source",
    "recover-body",
    "inspect",
)
STATE_FILENAME = "r6-3-4-result-input-seed.json"


class DriverError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        workspace_id = UUID(args.workspace_id)
        root = _test_root()
        database_url = _database_url()
        database = Database(database_url)
        try:
            result = _dispatch(args.action, database, workspace_id, root)
        finally:
            database.dispose()
    except (DriverError, TypeError, ValueError) as exc:
        code = exc.code if isinstance(exc, DriverError) else "invalid_argument"
        print(json.dumps({"status": "error", "error_code": code}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one deterministic R6.3.4 result-input test action."
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--action", choices=ACTIONS, required=True)
    return parser


def _dispatch(
    action: str, database: Database, workspace_id: UUID, root: Path
) -> dict[str, object]:
    if action == "seed":
        if (root / STATE_FILENAME).exists():
            raise DriverError("seed_state_exists")
        seed, _scenario = seed_result_input_plan(
            database, workspace_id, root / "sources"
        )
        state = _new_state(seed, root)
        _write_state(root, state)
        return _public_state(state, action)

    state = _read_state(root, workspace_id)
    if action == "prepare-browser":
        if state["phase"] != "pending_confirmation":
            raise DriverError("browser_prepare_requires_pending_confirmation")
        return _public_state(
            {
                **state,
                "browser_state": "awaiting_confirmation",
            },
            action,
        )
    if action == "finish-late-source":
        return _finish_late_source(database, workspace_id, root, state, action)
    if action == "recover-body":
        return _recover_body(database, workspace_id, root, state, action)
    if action == "inspect":
        return _inspect(database, workspace_id, root, state, action)
    raise DriverError("unsupported_action")


def _finish_late_source(
    database: Database,
    workspace_id: UUID,
    root: Path,
    state: Mapping[str, Any],
    action: str,
) -> dict[str, object]:
    if state["phase"] != "pending_confirmation":
        raise DriverError("late_source_requires_pending_confirmation")
    pending = pending_from_mapping(state["pending"])
    scenario = ResultInputScenario(database, workspace_id, root)
    scenario.now = scenario.now.replace(year=2026, month=9, day=7)
    scenario.load_pending_confirmation(pending)
    scenario.confirm_prepared_plan()
    scenario.complete_source("b", body=SOURCE_BODIES["b"])
    scenario.complete_source("c", body=SOURCE_BODIES["c"])
    scenario.reconcile()
    scenario.complete_source("a", body=SOURCE_BODIES["a"])
    updated = _materialized_state(state, scenario, phase="late_source_ready")
    _write_state(root, updated)
    return _public_state(updated, action)


def _recover_body(
    database: Database,
    workspace_id: UUID,
    root: Path,
    state: Mapping[str, Any],
    action: str,
) -> dict[str, object]:
    if state["phase"] == "recovered":
        return _public_state(state, action)
    scenario = ResultInputScenario(database, workspace_id, root)
    scenario.now = scenario.now.replace(year=2026, month=9, day=7)
    if state["phase"] == "pending_confirmation":
        pending = pending_from_mapping(state["pending"])
        scenario.load_pending_confirmation(pending)
        scenario.confirm_prepared_plan()
        scenario.complete_source("b", body=SOURCE_BODIES["b"])
        scenario.complete_source("c", body=SOURCE_BODIES["c"])
        scenario.reconcile()
    elif state["phase"] != "late_source_ready":
        raise DriverError("body_recovery_requires_late_source")
    else:
        request_id = _state_uuid(state, "planning_request_id")
        scenario.load_materialized_plan(request_id)
    scenario.activate()
    scenario.assemble_context("summary")
    if scenario._execution_id("a") is None:
        scenario.complete_source("a", body=SOURCE_BODIES["a"])
    artifact_id = scenario.source_artifact_id("b")
    with database.session() as session:
        artifact = session.get(ArtifactModel, artifact_id)
        if artifact is None:
            raise DriverError("selected_artifact_missing")
        digest = artifact.content_hash
    scenario.make_artifact_unavailable(artifact_id)
    blocked = scenario.start_worker("summary")
    if getattr(blocked, "code", None) != "result_input_unavailable":
        raise DriverError("body_failure_was_not_observed")
    scenario.artifact_store.put(
        workspace_id,
        scenario.latest_execution_id("b"),
        digest,
        SOURCE_BODIES["b"],
    )
    started = scenario.start_worker("summary")
    if getattr(started, "session_id", None) is None:
        raise DriverError("body_recovery_did_not_start")
    updated = _materialized_state(state, scenario, phase="recovered")
    _write_state(root, updated)
    return _public_state(updated, action)


def _inspect(
    database: Database,
    workspace_id: UUID,
    root: Path,
    state: Mapping[str, Any],
    action: str,
) -> dict[str, object]:
    del root
    with database.session() as session:
        counts = {
            "input_selections": int(
                session.scalar(
                    select(func.count()).where(
                        CollaborationInputSelectionModel.workspace_id == workspace_id
                    )
                )
                or 0
            ),
            "runtime_releases": int(
                session.scalar(
                    select(func.count()).select_from(RuntimeReleaseModel).where(
                        RuntimeReleaseModel.workspace_id == workspace_id
                    )
                )
                or 0
            ),
            "context_acknowledgements": int(
                session.scalar(
                    select(func.count())
                    .select_from(ContextPackageAcknowledgementModel)
                    .where(
                        ContextPackageAcknowledgementModel.workspace_id == workspace_id
                    )
                )
                or 0
            ),
            "executions": int(
                session.scalar(
                    select(func.count()).select_from(ExecutionModel).where(
                        ExecutionModel.workspace_id == workspace_id
                    )
                )
                or 0
            ),
        }
    return {
        "action": action,
        "status": "ok",
        "workspace_id": str(workspace_id),
        "phase": state["phase"],
        "counts": counts,
    }


def _new_state(seed: ResultInputSeed, root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_id": str(seed.pending.workspace_id),
        "phase": "pending_confirmation",
        "pending": seed.pending.to_mapping(),
        "source_files": {
            key: str(path.relative_to(root))
            for key, path in seed.source_files.items()
        },
    }


def _materialized_state(
    previous: Mapping[str, Any], scenario: ResultInputScenario, *, phase: str
) -> dict[str, object]:
    return {
        **previous,
        "phase": phase,
        "planning_request_id": str(scenario.ids["planning_request"]),
        "root_task_id": str(scenario.ids["root_task"]),
        "worker_item_ids": {
            key: str(value) for key, value in scenario.worker_item_ids.items()
        },
        "request_ids": {key: str(value) for key, value in scenario.request_ids.items()},
    }


def _read_state(root: Path, workspace_id: UUID) -> dict[str, Any]:
    path = root / STATE_FILENAME
    if not path.is_file():
        raise DriverError("seed_state_missing")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverError("seed_state_unreadable") from exc
    if not isinstance(value, dict) or value.get("workspace_id") != str(workspace_id):
        raise DriverError("seed_state_workspace_mismatch")
    if value.get("schema_version") != 1 or not isinstance(value.get("phase"), str):
        raise DriverError("seed_state_invalid")
    return value


def _write_state(root: Path, value: Mapping[str, object]) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / STATE_FILENAME
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def _public_state(value: Mapping[str, Any], action: str) -> dict[str, object]:
    result = {
        "action": action,
        "status": "ok",
        "workspace_id": value["workspace_id"],
        "phase": value["phase"],
    }
    for key in (
        "planning_request_id",
        "root_task_id",
        "worker_item_ids",
        "request_ids",
        "browser_state",
    ):
        if key in value:
            result[key] = value[key]
    return result


def _test_root() -> Path:
    raw = os.environ.get("SIDERA_R6_3_4_TEST_ROOT")
    if not raw:
        raise DriverError("test_root_required")
    root = Path(raw).expanduser().resolve()
    if root == Path(root.anchor):
        raise DriverError("test_root_too_broad")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise DriverError("test_root_not_directory")
    return root


def _database_url() -> str:
    if os.environ.get("SIDERA_R6_3_4_TEST_MODE") != "1":
        raise DriverError("test_mode_required")
    raw = os.environ.get("SIDERA_DATABASE_URL")
    if not raw:
        raise DriverError("database_url_required")
    try:
        url = make_url(raw)
    except Exception as exc:
        raise DriverError("database_url_invalid") from exc
    if not url.drivername.startswith("postgresql"):
        raise DriverError("database_must_be_postgresql")
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise DriverError("database_must_be_loopback")
    database_name = (url.database or "").lower()
    if database_name != "sidera_test" and not database_name.startswith("sidera_test_"):
        raise DriverError("database_must_be_test_database")
    return raw


def _state_uuid(state: Mapping[str, Any], name: str) -> UUID:
    raw = state.get(name)
    if not isinstance(raw, str):
        raise DriverError(f"state_{name}_missing")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise DriverError(f"state_{name}_invalid") from exc


if __name__ == "__main__":
    raise SystemExit(main())
