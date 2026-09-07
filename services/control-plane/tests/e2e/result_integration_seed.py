"""Provider-free CLI driver for the R6 result-integration browser scenario."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import make_url

from sidera_control_plane.persistence.database import Database
from tests.integration.result_integration_scenario import ResultIntegrationScenario
from tests.integration.test_r5_1_execution_request_schema import (
    seed_execution_request_facts,
)

MODES = ("seed", "prepare", "advance", "observe")
ADVANCE_ACTIONS = ("repair", "accept")
STATE_FILENAME = "r6-result-integration-seed.json"
TEST_MODE_ENV = "SIDERA_R6_RESULT_INTEGRATION_TEST_MODE"
SCOPE_ENV = "SIDERA_R6_RESULT_INTEGRATION_SCOPE"
ROOT_ENV = "SIDERA_R6_RESULT_INTEGRATION_TEST_ROOT"
REQUIRED_SCOPE = "provider-free-result-integration"


class DriverError(RuntimeError):
    """A user-safe error that can cross the CLI boundary without a traceback."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        workspace_id = UUID(args.workspace_id)
        root = _test_root()
        database = Database(_database_url())
        try:
            result = _dispatch(
                mode=args.mode,
                action=args.action,
                database=database,
                workspace_id=workspace_id,
                root=root,
            )
        finally:
            database.dispose()
    except DriverError as exc:
        print(json.dumps({"status": "error", "error_code": exc.code}), file=sys.stderr)
        return 1
    except (TypeError, ValueError, AssertionError, KeyError) as exc:
        del exc
        print(
            json.dumps({"status": "error", "error_code": "scenario_error"}),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI errors stay strict and redacted.
        del exc
        print(
            json.dumps({"status": "error", "error_code": "scenario_error"}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Drive the provider-free R6 result-integration E2E scenario."
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--action", choices=ADVANCE_ACTIONS)
    return parser


def _dispatch(
    *,
    mode: str,
    action: str | None,
    database: Database,
    workspace_id: UUID,
    root: Path,
) -> dict[str, object]:
    state_path = root / STATE_FILENAME
    if mode == "seed":
        if action is not None:
            raise DriverError("seed_does_not_accept_action")
        if state_path.exists():
            raise DriverError("seed_state_exists")
        ids = seed_execution_request_facts(database, workspace_id)
        scenario = ResultIntegrationScenario(
            database,
            workspace_id,
            root,
            ids,
            uuid4(),
        )
        scenario.seed_plan(apply=False)
        state = _state_from_scenario(scenario, phase="seeded")
        _write_state(root, state)
        return _public_state(state, mode)

    state = _read_state(root, workspace_id)
    if mode == "observe":
        if action is not None:
            raise DriverError("observe_does_not_accept_action")
        scenario = _scenario_from_state(database, root, state)
        return _public_state(_observed_state(state, scenario), mode)

    scenario = _scenario_from_state(database, root, state)
    if mode == "prepare":
        if action is not None:
            raise DriverError("prepare_does_not_accept_action")
        if state["phase"] == "seeded":
            scenario.seed_repairable_candidate()
            state = _state_from_scenario(scenario, phase="candidate_failed")
            _write_state(root, state)
        elif state["phase"] != "candidate_failed":
            raise DriverError("prepare_requires_seeded_state")
        return _public_state(state, mode)

    if mode != "advance":
        raise DriverError("unsupported_mode")
    if action is None:
        raise DriverError("advance_action_required")
    if action == "repair":
        state = _advance_repair(root, state, scenario)
    elif action == "accept":
        state = _advance_accept(root, state, scenario)
    else:
        raise DriverError("unsupported_advance_action")
    return _public_state(state, mode)


def _advance_repair(
    root: Path,
    state: Mapping[str, Any],
    scenario: ResultIntegrationScenario,
) -> dict[str, object]:
    phase = state["phase"]
    if phase == "candidate_passed":
        return dict(state)
    if phase != "candidate_failed":
        raise DriverError("repair_requires_failed_candidate")
    candidate = scenario.current_candidate()
    command_key = f"r6-result-integration-repair:{candidate.id}"
    request_result = scenario.command(
        "RequestTaskRework",
        scenario.rework_payload(candidate),
        idempotency_key=command_key,
    )
    request_ids = request_result.data.get("request_ids")
    if not isinstance(request_ids, list) or not request_ids:
        raise DriverError("repair_request_missing")
    request_id = UUID(str(request_ids[0]))
    replacement = scenario.rework_source(
        request_id,
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
    )
    scenario.command(
        "ReintegrateTaskResult",
        scenario.reintegration_payload(candidate, replacement),
        idempotency_key=f"r6-result-integration-reintegrate:{candidate.id}",
    )
    scenario.drive()
    updated = _state_from_scenario(scenario, phase="candidate_passed")
    _write_state(root, updated)
    return updated


def _advance_accept(
    root: Path,
    state: Mapping[str, Any],
    scenario: ResultIntegrationScenario,
) -> dict[str, object]:
    if state["phase"] == "completed":
        return dict(state)
    if state["phase"] != "candidate_passed":
        raise DriverError("accept_requires_passed_candidate")
    candidate = scenario.current_candidate()
    scenario.command(
        "CompleteTask",
        scenario.accept_payload(candidate),
        idempotency_key=f"r6-result-integration-accept:{candidate.id}",
    )
    scenario.drive()
    phase = "completed" if scenario.root().status.value == "succeeded" else "accepted"
    updated = _state_from_scenario(scenario, phase=phase)
    _write_state(root, updated)
    return updated


def _scenario_from_state(
    database: Database,
    root: Path,
    state: Mapping[str, Any],
) -> ResultIntegrationScenario:
    ids = {
        "root_task": _state_uuid(state, "root_task_id"),
        "plan_snapshot": _state_uuid(state, "plan_snapshot_id"),
        "execution": uuid4(),
    }
    base_commit = state.get("base_commit")
    if not isinstance(base_commit, str) or not base_commit:
        raise DriverError("state_base_commit_missing")
    scenario = ResultIntegrationScenario(
        database,
        _state_uuid(state, "workspace_id"),
        root,
        ids,
        _state_uuid(state, "planning_request_id"),
        base_commit=base_commit,
    )
    scenario.load_materialized_plan(
        _state_uuid(state, "planning_request_id"),
        apply_enabled=False,
        repairable_candidate=True,
    )
    return scenario


def _state_from_scenario(
    scenario: ResultIntegrationScenario,
    *,
    phase: str,
) -> dict[str, object]:
    head = scenario.head()
    return {
        "schema_version": 1,
        "workspace_id": str(scenario.workspace_id),
        "root_task_id": str(scenario.root_task_id),
        "planning_request_id": str(scenario.ids["planning_request"]),
        "plan_snapshot_id": str(scenario.plan_snapshot_id),
        "worker_item_ids": {
            key: str(value) for key, value in scenario.worker_item_ids.items()
        },
        "base_commit": scenario.base_commit,
        "current_candidate_id": (
            None
            if head.current_candidate_id is None
            else str(head.current_candidate_id)
        ),
        "phase": phase,
    }


def _observed_state(
    state: Mapping[str, Any],
    scenario: ResultIntegrationScenario,
) -> dict[str, object]:
    observed = dict(state)
    head = scenario.head()
    observed["current_candidate_id"] = (
        None if head.current_candidate_id is None else str(head.current_candidate_id)
    )
    observed["root_status"] = scenario.root().status.value
    observed["result_version"] = head.result_version
    if head.current_candidate_id is None:
        observed["verification_statuses"] = []
    else:
        observed["verification_statuses"] = [
            result.status.value
            for result in scenario.verification_results(head.current_candidate_id)
        ]
    return observed


def _public_state(state: Mapping[str, Any], mode: str) -> dict[str, object]:
    required = (
        "workspace_id",
        "root_task_id",
        "worker_item_ids",
        "current_candidate_id",
        "phase",
    )
    if any(key not in state for key in required):
        raise DriverError("state_public_fields_missing")
    return {
        "status": "ok",
        "mode": mode,
        "workspace_id": state["workspace_id"],
        "root_task_id": state["root_task_id"],
        "worker_item_ids": state["worker_item_ids"],
        "current_candidate_id": state["current_candidate_id"],
        "phase": state["phase"],
        **{
            key: value
            for key, value in state.items()
            if key in {"root_status", "result_version", "verification_statuses"}
        },
    }


def _read_state(root: Path, workspace_id: UUID) -> dict[str, Any]:
    path = root / STATE_FILENAME
    if not path.is_file():
        raise DriverError("seed_state_missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverError("seed_state_invalid") from exc
    if not isinstance(raw, dict):
        raise DriverError("seed_state_invalid")
    if raw.get("workspace_id") != str(workspace_id):
        raise DriverError("seed_workspace_mismatch")
    return raw


def _write_state(root: Path, state: Mapping[str, Any]) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = root / f".{STATE_FILENAME}.tmp"
    temporary.write_text(
        json.dumps(dict(state), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(root / STATE_FILENAME)


def _test_root() -> Path:
    raw = os.environ.get(ROOT_ENV)
    if not raw:
        raise DriverError("test_root_required")
    root = Path(raw).expanduser().resolve()
    home = Path.home().resolve()
    if root == Path(root.anchor) or root == home or home in root.parents:
        raise DriverError("test_root_too_broad")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise DriverError("test_root_not_directory")
    return root


def _database_url() -> str:
    if os.environ.get(TEST_MODE_ENV) != "1":
        raise DriverError("test_mode_required")
    if os.environ.get(SCOPE_ENV) != REQUIRED_SCOPE:
        raise DriverError("test_scope_required")
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
