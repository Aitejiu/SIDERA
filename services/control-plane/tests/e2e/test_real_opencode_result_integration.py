"""Bounded, explicitly authorized real OpenCode result-integration acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.modules import (
    SandboxCapability,
    StabilityLevel,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import ExecutionModel
from sidera_control_plane.persistence.real_result_budget import (
    RealResultExecutionBudgetStore,
)
from sidera_control_plane.result_readiness import authorize_real_result_run
from tests.e2e.test_r6_3_4_real_opencode_inputs import (
    REAL_RESULT_MAX_REPORT_BYTES,
    REAL_RESULT_MAX_SECONDS,
    RealResultInputGateConfig,
    _ensure_real_time,
    _terminate_real_adapters,
    run_real_result_preflight,
)
from tests.integration.result_integration_scenario import ResultIntegrationScenario
from tests.integration.test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)

REAL_RESULT_EXECUTION_STEPS = (
    "source-a",
    "source-b",
    "integration",
    "repair-source-a",
    "reintegration",
)


def test_real_opencode_result_integration_entry(tmp_path: Path) -> None:
    """Run the full real path only after explicit authorization and local PASS."""

    if not _real_run_requested():
        pytest.skip(
            "set both real result-integration authorization switches to collect "
            "the bounded OpenCode entry"
        )

    gate_path = os.environ.get("SIDERA_REAL_RESULT_LOCAL_GATE_FILE")
    if gate_path is None or not gate_path.strip():
        pytest.fail("SIDERA_REAL_RESULT_LOCAL_GATE_FILE is required for a real run")
    try:
        record = json.loads(Path(gate_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError("the local real-result gate record is invalid") from exc

    authorization = authorize_real_result_run(
        _real_settings(),
        record,
        environ=os.environ,
    )
    preflight = run_real_result_preflight(_real_preflight_config(authorization))
    if preflight.resolved_executable is None:
        pytest.fail("real result-integration preflight did not resolve OpenCode")
    if preflight.capability_profile is None:
        pytest.fail("real result-integration preflight did not return a profile")
    if 3 not in preflight.capability_profile.context_package_versions:
        pytest.fail("OpenCode adapter does not support ContextPackage V3")

    database = Database(_real_database_url())
    adapters: list[OpenCodeACPAdapter] = []
    scenario: ResultIntegrationScenario | None = None
    reservations = []
    started_at = datetime.now(UTC)
    deadline = time.monotonic() + min(
        REAL_RESULT_MAX_SECONDS, authorization.wall_clock_seconds
    )
    try:
        workspace_id = uuid4()
        ids = seed_execution_request_facts(database, workspace_id)
        legacy_request_id = uuid4()
        with database.session() as session:
            legacy_request = request_row(ids, request_id=legacy_request_id)
            legacy_request.status = "obsolete"
            legacy_request.gate_reason = "real_result_integration_legacy_request"
            session.add(legacy_request)
            session.commit()

        scenario = ResultIntegrationScenario(
            database,
            workspace_id,
            tmp_path,
            ids,
            legacy_request_id,
            repository_path=authorization.repository,
        )
        repository = scenario.repository_path
        repository_ref = _opaque_repository_ref(repository)
        workspace_root = tmp_path / "real-result-workspaces"
        workspace_root.mkdir(mode=0o700)
        workspace_manager = GitWorktreeWorkspaceManager(workspace_root)

        def adapter_factory(instance: Any) -> OpenCodeACPAdapter:
            if instance.config.get("opencode.model") != authorization.model:
                raise AssertionError("real adapter model changed during binding")
            if instance.config.get("opencode.provider") != authorization.provider:
                raise AssertionError("real adapter provider changed during binding")
            adapter = OpenCodeACPAdapter(
                executable=preflight.resolved_executable,
                model=authorization.model,
                workspace_root=workspace_root,
                request_timeout_seconds=min(
                    60.0, float(authorization.wall_clock_seconds)
                ),
                stop_grace_seconds=2.0,
                context_package_versions=(1, 2, 3),
            )
            adapters.append(adapter)
            return adapter

        runtime = _runtime(
            authorization=authorization,
            preflight=preflight,
            repository=repository,
            revision=scenario.base_commit,
            workspace_manager=workspace_manager,
            adapter_factory=adapter_factory,
        )
        scenario.configure_worker_runtime(runtime, repository_ref=repository_ref)
        scenario.seed_plan(apply=False)
        budget_store = RealResultExecutionBudgetStore(database)

        def reserve(step: str) -> None:
            _ensure_real_time(deadline)
            if len(reservations) >= len(REAL_RESULT_EXECUTION_STEPS):
                raise AssertionError("real result execution budget was overrun")
            if REAL_RESULT_EXECUTION_STEPS[len(reservations)] != step:
                raise AssertionError(
                    f"real result step order changed: expected "
                    f"{REAL_RESULT_EXECUTION_STEPS[len(reservations)]}, got {step}"
                )
            reservation = budget_store.reserve(
                run_id=authorization.run_id,
                max_executions=authorization.max_executions,
                step_key=step,
            )
            if reservation.sequence_no != len(reservations) + 1:
                raise AssertionError("real result reservation sequence is not contiguous")
            reservations.append(reservation)

        # Reserve all three first-pass sessions before entering the helper that
        # creates the two source workers and the first integration worker.
        reserve("source-a")
        reserve("source-b")
        reserve("integration")
        first = scenario.seed_repairable_candidate()
        _ensure_real_time(deadline)
        budget_store.complete(reservations[0].id, status="succeeded")
        budget_store.complete(reservations[1].id, status="succeeded")
        budget_store.complete(reservations[2].id, status="succeeded")
        if len(adapters) != 3:
            raise AssertionError(f"first pass started {len(adapters)} real adapters")
        if scenario.verification_results(first.id)[0].status != "failed":
            raise AssertionError("real first candidate did not produce failed evidence")

        rework = scenario.command(
            "RequestTaskRework",
            scenario.rework_payload(first),
        )
        rework_request_id = UUID(str(rework.data["request_ids"][0]))
        reserve("repair-source-a")
        repaired = scenario.rework_source(
            rework_request_id,
            files={
                "login.py": b"ALLOW_EXPIRED = False\n"
                b"# SIDERA_REAL_RESULT_REPAIR\n",
            },
        )
        budget_store.complete(reservations[3].id, status="succeeded")

        scenario.command(
            "ReintegrateTaskResult",
            scenario.reintegration_payload(first, repaired),
        )
        reserve("reintegration")
        second = scenario.drive()
        _ensure_real_time(deadline)
        budget_store.complete(reservations[4].id, status="succeeded")
        if len(adapters) != 5:
            raise AssertionError(f"real flow started {len(adapters)} adapters")
        if scenario.verification_results(second.id)[0].status != "passed":
            raise AssertionError("real repaired candidate did not pass verification")

        scenario.command("CompleteTask", scenario.accept_payload(second))
        outcome = scenario.drive()
        if scenario.root().status.value != "succeeded":
            raise AssertionError(f"real result acceptance did not complete: {outcome}")
        if scenario.candidate_tree(second) != scenario.restore_candidate(second):
            raise AssertionError("real accepted candidate did not restore independently")
        if len(reservations) != len(REAL_RESULT_EXECUTION_STEPS):
            raise AssertionError("real result flow did not consume its declared budget")
        if budget_store.reserved_count(authorization.run_id) != len(
            REAL_RESULT_EXECUTION_STEPS
        ):
            raise AssertionError("durable real result budget count is incomplete")
        _ensure_real_time(deadline)

        with database.session() as session:
            execution_count = int(
                session.scalar(
                    select(ExecutionModel.id).where(
                        ExecutionModel.workspace_id == workspace_id,
                        ExecutionModel.id.in_(
                            [
                                scenario.latest_execution_id("a"),
                                scenario.latest_execution_id("b"),
                                scenario.latest_execution_id("summary"),
                            ]
                        ),
                    )
                )
                is not None
            )
        if execution_count != 1:
            raise AssertionError("real execution evidence query was not deterministic")

        print(
            json.dumps(
                {
                    "real_result_integration": {
                        **authorization.safe_summary(),
                        "status": "passed",
                        "executions": len(reservations),
                        "steps": list(REAL_RESULT_EXECUTION_STEPS),
                        "adapters": len(adapters),
                        "provider_call_window_seconds": int(
                            (datetime.now(UTC) - started_at).total_seconds()
                        ),
                        "usage": "not_reported_by_adapter",
                    }
                },
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        if scenario is not None:
            scenario.release_terminal_workspaces()
        _terminate_real_adapters(adapters)
        database.dispose()


def _runtime(
    *,
    authorization: Any,
    preflight: Any,
    repository: Path,
    revision: str,
    workspace_manager: GitWorktreeWorkspaceManager,
    adapter_factory: Any,
) -> Any:
    from tests.integration.result_input_scenario import ResultInputWorkerRuntime

    return ResultInputWorkerRuntime(
        adapter_provider="opencode",
        adapter_interface_version="sidera.worker-adapter.v1",
        adapter_capabilities=preflight.capability_profile,
        adapter_factory=adapter_factory,
        adapter_config_namespace="opencode",
        adapter_config={
            "opencode.executable": preflight.resolved_executable,
            "opencode.model": authorization.model,
            "opencode.provider": authorization.provider,
            "opencode.endpoint_host": authorization.endpoint_host,
            "opencode.fallbacks": list(authorization.fallbacks),
        },
        workspace_provider="git-worktree",
        workspace_interface_version="sidera.workspace-provider.v1",
        workspace_capabilities=WorkspaceProviderCapabilityProfile(
            isolation=SandboxCapability.FILESYSTEM,
            writable=True,
            git_worktree=True,
            stability=StabilityLevel.STABLE,
            code_input_versions=(1,),
        ),
        workspace_factory=lambda _instance: workspace_manager,
        workspace_config_namespace="workspace",
        workspace_config={
            "workspace.repository_ref": str(repository),
            "workspace.base_revision": revision,
            "workspace.expected_outputs": ["diff"],
        },
    )


def _real_preflight_config(authorization: Any) -> RealResultInputGateConfig:
    executable = os.environ.get("SIDERA_OPENCODE_EXECUTABLE")
    if executable is None or not executable.strip():
        raise AssertionError("SIDERA_OPENCODE_EXECUTABLE is required for a real run")
    return RealResultInputGateConfig(
        executable=executable.strip(),
        model=authorization.model,
        provider=authorization.provider,
        endpoint_host=authorization.endpoint_host,
        adapter_config_version=1,
        expected_version=os.environ.get("SIDERA_OPENCODE_EXPECTED_VERSION"),
        fallbacks=authorization.fallbacks,
        approved_fallbacks=authorization.fallbacks,
        budget_microunits=None,
        max_seconds=min(REAL_RESULT_MAX_SECONDS, authorization.wall_clock_seconds),
        max_report_bytes=REAL_RESULT_MAX_REPORT_BYTES,
        context_package_versions=(1, 2, 3),
    )


def _real_settings() -> Settings:
    return Settings.model_validate(
        {
            "database_url": _real_database_url(),
            "result_integration_enabled": True,
        }
    )


def _real_database_url() -> str:
    value = os.environ.get("SIDERA_TEST_DATABASE_URL") or os.environ.get(
        "SIDERA_DATABASE_URL"
    )
    if value is None or not value.strip():
        raise AssertionError("SIDERA_TEST_DATABASE_URL is required for a real run")
    return value.strip()


def _opaque_repository_ref(repository: Path) -> str:
    digest = hashlib.sha256(str(repository.resolve()).encode("utf-8")).hexdigest()
    return f"git-repository://{digest}"


def _real_run_requested() -> bool:
    return (
        os.environ.get("SIDERA_RUN_REAL_RESULT_INTEGRATION") == "1"
        and os.environ.get("SIDERA_REAL_RESULT_INTEGRATION_AUTHORIZED") == "1"
    )


__all__ = ["REAL_RESULT_EXECUTION_STEPS"]
