from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sidera_control_plane.basic_acceptance_config import (
    inspect_profiles,
    prepare_process_environment,
    write_process_environment,
)
from sidera_control_plane.basic_acceptance_manifest import (
    SCENARIO_NAMES,
    AgentProfile,
    BasicRunManifest,
    ScenarioLimits,
    validate_manifest_isolation,
)


def _profile(
    role: str,
    *,
    model_id: str = "opencode-go/MiMo-V2.5",
    credential_ref: str = "env:SIDERA_ACCEPTANCE_KEY",
) -> dict[str, object]:
    return {
        "role": role,
        "runtime": "opencode-acp" if role in {"worker", "second_worker"} else "akashic",
        "provider": "opencode-go",
        "model_id": model_id,
        "endpoint_host": "opencode.ai",
        "credential_ref": credential_ref,
        "config_ref": "module:acceptance-config",
        "config_version": 1,
        "fallbacks": ("opencode-go/Ox-Alpha-Free",),
    }


def _manifest(tmp_path: Path) -> BasicRunManifest:
    repository = tmp_path / "business-repository"
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
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    profiles = tuple(
        AgentProfile.model_validate(_profile(role))
        for role in ("main_agent", "planner", "worker", "second_worker")
    )
    scenarios = {
        name: ScenarioLimits(
            main_agent_turns=1,
            planner_runs=1,
            worker_sessions=2,
            adapter_prompts=2,
            max_seconds=60,
        )
        for name in SCENARIO_NAMES
    }
    return BasicRunManifest(
        run_id=uuid4(),
        workspace_id=uuid4(),
        local_gate_record=tmp_path / "gate.json",
        runtime_fingerprint="a" * 64,
        run_root=tmp_path / "run",
        repository=repository,
        base_commit=commit,
        database_ref="env:SIDERA_ACCEPTANCE_DATABASE_URL",
        knowledge_database_ref="env:SIDERA_ACCEPTANCE_KNOWLEDGE_DATABASE_URL",
        control_plane_url="http://127.0.0.1:18000",
        knowledge_url="http://localhost:18020",
        main_agent_gateway_url="http://127.0.0.1:18010",
        web_url="http://127.0.0.1:18418",
        profiles=profiles,
        scenarios=scenarios,
    )


def test_manifest_requires_each_agent_role_once(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    raw = manifest.model_dump(mode="json")
    raw["profiles"][-1]["role"] = "worker"

    with pytest.raises(ValidationError, match="roles must appear exactly once"):
        BasicRunManifest.model_validate(raw)


def test_manifest_rejects_non_loopback_service_urls(tmp_path: Path) -> None:
    raw = _manifest(tmp_path).model_dump(mode="json")
    raw["control_plane_url"] = "https://example.com"

    with pytest.raises(ValidationError, match="loopback"):
        BasicRunManifest.model_validate(raw)


def test_manifest_rejects_provider_endpoint_userinfo_or_path(tmp_path: Path) -> None:
    raw = _manifest(tmp_path).model_dump(mode="json")
    raw["profiles"][0]["endpoint_host"] = "user:secret@example.com/path"

    with pytest.raises(ValidationError, match="endpoint_host"):
        BasicRunManifest.model_validate(raw)


def test_project_override_is_not_reported_as_global_model(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    report = inspect_profiles(
        manifest,
        overrides={"worker": {"model_id": "opencode-go/model-b"}},
        environ={"SIDERA_ACCEPTANCE_KEY": "fake-key"},
    )

    worker = report.profile("worker")
    assert worker.model_id == "opencode-go/model-b"
    assert worker.status == "mismatch"
    assert worker.configured_model_id == "opencode-go/MiMo-V2.5"


def test_missing_selected_key_does_not_fall_back_to_ambient_deepseek(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    report = inspect_profiles(
        manifest,
        environ={"DEEPSEEK_API_KEY": "ambient-secret"},
    )

    worker = report.profile("worker")
    assert worker.code == "selected_credential_unavailable"
    assert worker.status == "unavailable"
    assert report.started_sessions == 0


def test_credential_contents_do_not_change_profile_fingerprint(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    first = inspect_profiles(
        manifest,
        environ={"SIDERA_ACCEPTANCE_KEY": "first-secret"},
    )
    second = inspect_profiles(
        manifest,
        environ={"SIDERA_ACCEPTANCE_KEY": "second-secret"},
    )

    assert first.profile("worker").fingerprint == second.profile("worker").fingerprint
    assert "secret" not in json.dumps(first.model_dump()).casefold()


def test_process_environment_only_keeps_selected_refs_and_safe_inheritance(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    values = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/home",
        "SIDERA_ACCEPTANCE_KEY": "selected-secret",
        "DEEPSEEK_API_KEY": "ambient-secret",
        "OPENAI_API_KEY": "ambient-secret",
    }

    environment = prepare_process_environment(manifest, environ=values)
    assert environment["SIDERA_ACCEPTANCE_KEY"] == "selected-secret"
    assert environment["PATH"] == "/usr/bin"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment

    destination = tmp_path / "private" / "environment.json"
    write_process_environment(destination, environment)
    assert destination.stat().st_mode & 0o077 == 0
    assert json.loads(destination.read_text(encoding="utf-8"))["PATH"] == "/usr/bin"


def test_akashic_toml_resolves_main_runtime_without_printing_key(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    config_path = tmp_path / "akashic.toml"
    config_path.write_text(
        """
[llm]
main = "selected"

[llm.runtimes.selected]
provider = "opencode-go"
model = "opencode-go/model-from-toml"
api_key = "${SELECTED_KEY}"
base_url = "https://opencode.ai/zen/go/v1"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    profiles = list(manifest.profiles)
    profiles[0] = AgentProfile.model_validate(
        _profile(
            "main_agent",
            model_id="opencode-go/declared-model",
        )
        | {"config_ref": f"file:{config_path}"}
    )
    manifest.profiles = tuple(profiles)

    report = inspect_profiles(
        manifest,
        environ={"SELECTED_KEY": "secret-value"},
    )
    main_agent = report.profile("main_agent")
    assert main_agent.model_id == "opencode-go/model-from-toml"
    assert main_agent.endpoint_host == "opencode.ai"
    assert main_agent.credential_source == "env:SELECTED_KEY"
    assert main_agent.status == "mismatch"
    assert "secret-value" not in json.dumps(report.model_dump())


def test_manifest_paths_are_outside_sidera_git_common_dir(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    sidera_root = tmp_path / "sidera"
    sidera_root.mkdir()
    nested = sidera_root / "nested"
    nested.mkdir()
    manifest.repository = nested

    with pytest.raises(ValueError, match="SIDERA work tree"):
        validate_manifest_isolation(manifest, sidera_root=sidera_root)
