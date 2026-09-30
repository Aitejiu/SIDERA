from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sidera_control_plane.basic_acceptance_manifest import (
    AgentProfile,
    AllowedRoute,
    BasicRunManifest,
    ScenarioLimits,
)


def test_scenario_limit_accepts_one_hour() -> None:
    limits = ScenarioLimits(
        main_agent_turns=0,
        planner_runs=0,
        worker_sessions=0,
        adapter_prompts=0,
        max_seconds=3600,
    )

    assert limits.max_seconds == 3600


def test_scenario_limit_rejects_more_than_one_hour() -> None:
    with pytest.raises(ValidationError):
        ScenarioLimits(
            main_agent_turns=0,
            planner_runs=0,
            worker_sessions=0,
            adapter_prompts=0,
            max_seconds=3601,
        )


def _entrypoint_module():
    path = Path(__file__).resolve().parents[4] / "scripts" / "r6-basic-acceptance.py"
    spec = importlib.util.spec_from_file_location("sidera_r6_acceptance", path)
    if spec is None or spec.loader is None:
        raise AssertionError("acceptance entrypoint could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Sidera Test"],
        cwd=repository,
        check=True,
    )
    (repository / "README.md").write_text("acceptance\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    run_id = uuid4()
    workspace_id = uuid4()
    run_root = tmp_path / "run"
    run_root.mkdir()
    approval = run_root / "approval.json"
    profiles = tuple(
        AgentProfile(
            role=role,
            runtime="fake-runtime",
            provider="fake-provider",
            model_id=f"fake-{role}",
            endpoint_host="127.0.0.1:18101",
            credential_ref="none",
            config_ref=f"module:test/{role}",
            config_version=1,
        )
        for role in ("main_agent", "planner", "worker", "second_worker")
    )
    route = AllowedRoute(
        route_id="revision-worker",
        role="worker",
        runtime="fake-runtime",
        provider="fake-provider",
        model_id="fake-worker",
        endpoint_host="127.0.0.1:18101",
        credential_ref="none",
        config_version=1,
        costs={"worker_sessions": 1, "adapter_prompts": 1},
    )
    manifest = BasicRunManifest(
        schema_version=2,
        run_id=run_id,
        workspace_id=workspace_id,
        local_gate_record=tmp_path / "gate.json",
        runtime_fingerprint="b" * 64,
        implementation_fingerprint="a" * 64,
        fingerprint_version=2,
        run_root=run_root,
        repository=repository,
        base_commit=commit,
        database_ref="env:SIDERA_TEST_DB",
        knowledge_database_ref="env:SIDERA_KNOWLEDGE_DB",
        control_plane_url="http://127.0.0.1:18111",
        knowledge_url="http://127.0.0.1:18112",
        main_agent_gateway_url="http://127.0.0.1:18113",
        web_url="http://127.0.0.1:18114",
        profiles=profiles,
        scenarios={
            "revision": ScenarioLimits(
                main_agent_turns=1,
                planner_runs=1,
                worker_sessions=1,
                adapter_prompts=1,
                max_seconds=60,
            )
        },
        batch_limits={
            "main_agent_turns": 1,
            "planner_runs": 1,
            "worker_sessions": 1,
            "adapter_prompts": 1,
        },
        allowed_routes=(route,),
        approval_ref=str(approval),
    )
    approval.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "approved": True,
                "run_id": str(run_id),
                "workspace_id": str(workspace_id),
                "implementation_fingerprint": "a" * 64,
                "runtime_fingerprint": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def test_real_preflight_requires_explicit_opt_in(tmp_path: Path, monkeypatch) -> None:
    module = _entrypoint_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.delenv("SIDERA_RUN_REAL_BASIC", raising=False)

    with pytest.raises(module.RealPreflightError, match="real_mode_not_authorized"):
        module.preflight_real_manifest(manifest_path, "revision")

    assert not (tmp_path / "run" / "acceptance-state.json").exists()


def test_real_preflight_validates_and_reuses_the_guarded_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    module = _entrypoint_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("SIDERA_RUN_REAL_BASIC", "1")

    first = module.preflight_real_manifest(manifest_path, "revision")
    second = module.preflight_real_manifest(manifest_path, "revision")

    assert first["status"] == "prepared"
    assert second["status"] == "existing"
    assert first["run_id"] == second["run_id"]
    assert (tmp_path / "run" / "acceptance-state.json").exists()


def test_real_preflight_rejects_schema_one_and_undeclared_scenario(
    tmp_path: Path, monkeypatch
) -> None:
    module = _entrypoint_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("SIDERA_RUN_REAL_BASIC", "1")

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(module.RealPreflightError, match="real_manifest_schema_required"):
        module.preflight_real_manifest(manifest_path, "revision")

    raw["schema_version"] = 2
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(module.RealPreflightError, match="real_scenario_not_declared"):
        module.preflight_real_manifest(manifest_path, "user-flow")


def test_real_preflight_requires_persisted_resource_binding_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _entrypoint_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("SIDERA_RUN_REAL_BASIC", "1")

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["resource_binding_mode"] = "legacy"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(
        module.RealPreflightError,
        match="real_resource_binding_mode_required",
    ):
        module.preflight_real_manifest(manifest_path, "revision")


def test_real_preflight_rejects_approval_identity_without_initializing_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _entrypoint_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("SIDERA_RUN_REAL_BASIC", "1")

    approval = tmp_path / "run" / "approval.json"
    raw_approval = json.loads(approval.read_text(encoding="utf-8"))
    raw_approval["runtime_fingerprint"] = "c" * 64
    approval.write_text(json.dumps(raw_approval), encoding="utf-8")

    with pytest.raises(module.RealPreflightError, match="approval_identity_mismatch"):
        module.preflight_real_manifest(manifest_path, "revision")

    assert not (tmp_path / "run" / "acceptance-state.json").exists()
    assert not (tmp_path / "run" / "acceptance-state.initialized.json").exists()


def test_real_preflight_rejects_manifest_identity_change_without_replacing_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _entrypoint_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("SIDERA_RUN_REAL_BASIC", "1")

    module.preflight_real_manifest(manifest_path, "revision")
    ledger = tmp_path / "run" / "acceptance-state.json"
    marker = tmp_path / "run" / "acceptance-state.initialized.json"
    ledger_before = ledger.read_bytes()
    marker_before = marker.read_bytes()

    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_manifest["workspace_id"] = str(uuid4())
    manifest_path.write_text(json.dumps(raw_manifest), encoding="utf-8")

    with pytest.raises(module.RealPreflightError, match="ledger_identity_mismatch"):
        module.preflight_real_manifest(manifest_path, "revision")

    assert ledger.read_bytes() == ledger_before
    assert marker.read_bytes() == marker_before
    assert sorted(
        path.name
        for path in (tmp_path / "run").glob("acceptance-state*")
        if path.suffix == ".json"
    ) == [
        "acceptance-state.initialized.json",
        "acceptance-state.json",
    ]


def test_real_preflight_rejects_ledger_without_initialization_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _entrypoint_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("SIDERA_RUN_REAL_BASIC", "1")

    module.preflight_real_manifest(manifest_path, "revision")
    ledger = tmp_path / "run" / "acceptance-state.json"
    ledger_before = ledger.read_bytes()
    (tmp_path / "run" / "acceptance-state.initialized.json").unlink()

    with pytest.raises(
        module.RealPreflightError,
        match="initialization_marker_missing",
    ):
        module.preflight_real_manifest(manifest_path, "revision")

    assert ledger.read_bytes() == ledger_before
    assert not (tmp_path / "run" / "acceptance-state.initialized.json").exists()


def test_real_cli_reuses_identity_and_reports_missing_observation_without_side_effects(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _entrypoint_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("SIDERA_RUN_REAL_BASIC", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "r6-basic-acceptance.py",
            "--manifest",
            str(manifest_path),
            "--scenario",
            "revision",
            "--mode",
            "real",
            "--json",
        ],
    )

    assert module.main() == 2
    first_output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    ledger = tmp_path / "run" / "acceptance-state.json"
    first_ledger = ledger.read_bytes()

    assert module.main() == 2
    second_output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert first_output["status"] == "INCOMPLETE"
    assert first_output["failure_code"] == "scenario_incomplete"
    assert "revision_snapshot_not_supplied" in first_output["check_codes"]
    assert second_output == first_output
    assert first_output["run_id"] == json.loads(manifest_path.read_text())["run_id"]
    assert ledger.read_bytes() == first_ledger
    assert sorted(
        path.name
        for path in (tmp_path / "run").glob("acceptance-state*")
        if path.suffix == ".json"
    ) == [
        "acceptance-state.initialized.json",
        "acceptance-state.json",
    ]


def test_real_cli_rejects_unregistered_scenario_before_running_handler(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _entrypoint_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("SIDERA_RUN_REAL_BASIC", "1")
    monkeypatch.setattr(
        module,
        "run_scenario",
        lambda *_args, **_kwargs: pytest.fail("unregistered scenario handler ran"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "r6-basic-acceptance.py",
            "--manifest",
            str(manifest_path),
            "--scenario",
            "python:os.system('touch /tmp/should-not-run')",
            "--mode",
            "real",
        ],
    )

    assert module.main() == 2
    output = json.loads(capsys.readouterr().out.strip())
    assert output == {
        "status": "INCOMPLETE",
        "failure_code": "real_scenario_not_declared",
    }
    assert not (tmp_path / "run" / "acceptance-state.json").exists()
