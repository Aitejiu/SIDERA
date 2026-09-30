import stat

import pytest

from sidera_control_plane.demo.environment import (
    allocate_local_ports,
    child_environment,
    component_environment,
    create_demo_tokens,
    load_demo_tokens,
)
from tests.unit.interview_demo_support import manifest_for


def test_child_environment_drops_credentials_proxy_and_user_runtime_settings() -> None:
    inherited = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "TMPDIR": "/tmp",
        "DEEPSEEK_API_KEY": "not-forwarded",
        "OPENAI_API_KEY": "not-forwarded",
        "HTTPS_PROXY": "http://proxy.invalid",
        "PGPASSWORD": "not-forwarded",
        "SIDERA_DATABASE_URL": "postgresql://user@remote.invalid/db",
    }

    result = child_environment(
        inherited,
        {
            "SIDERA_DATABASE_URL": "postgresql://sidera@127.0.0.1/demo",
            "SIDERA_INTERVIEW_DEMO_MANIFEST": "/tmp/run/manifest.json",
        },
    )

    assert result == {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "TMPDIR": "/tmp",
        "SIDERA_DATABASE_URL": "postgresql://sidera@127.0.0.1/demo",
        "SIDERA_INTERVIEW_DEMO_MANIFEST": "/tmp/run/manifest.json",
    }


def test_child_environment_does_not_mutate_the_parent_environment() -> None:
    inherited = {"PATH": "/bin", "DEEPSEEK_API_KEY": "private"}
    original = dict(inherited)

    child_environment(inherited, {"SIDERA_ENVIRONMENT": "demo"})

    assert inherited == original


def test_demo_tokens_are_private_and_are_never_overwritten(tmp_path) -> None:
    run_root = tmp_path / "run"

    tokens = create_demo_tokens(run_root)

    token_path = run_root / "tokens.json"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert set(tokens) == {
        "knowledge_local_owner",
        "knowledge_main_agent",
        "knowledge_control_plane",
        "web_knowledge",
    }
    assert all(len(value) >= 32 for value in tokens.values())
    assert load_demo_tokens(run_root) == tokens
    with pytest.raises(FileExistsError):
        create_demo_tokens(run_root)


def test_component_environment_uses_only_manifest_and_run_tokens(tmp_path) -> None:
    manifest = manifest_for(tmp_path)
    tokens = {
        "knowledge_local_owner": "owner-token-value-that-is-long-enough",
        "knowledge_main_agent": "main-agent-token-value-that-is-long-enough",
        "knowledge_control_plane": "control-plane-token-value-that-is-long-enough",
        "web_knowledge": "web-token-value-that-is-long-enough",
    }
    inherited = {
        "PATH": "/usr/bin",
        "DEEPSEEK_API_KEY": "must-not-leak",
        "SIDERA_DATABASE_URL": "postgresql://remote.invalid/user",
        "PGPASSFILE": "/tmp/unselected.pgpass",
        "HTTPS_PROXY": "http://proxy.invalid",
    }

    result = component_environment(
        manifest, "control-plane", tokens, inherited
    )

    assert result["SIDERA_DATABASE_URL"].endswith(
        f"/{manifest.database_name}"
    )
    assert result["SIDERA_INTERVIEW_DEMO_MANIFEST"] == str(
        manifest.run_root / "manifest.json"
    )
    assert result["SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN"] == tokens[
        "knowledge_control_plane"
    ]
    assert result["SIDERA_RESULT_APPLY_ON_ACCEPT"] == "false"
    assert "DEEPSEEK_API_KEY" not in result
    assert "HTTPS_PROXY" not in result
    assert "PGPASSFILE" not in result

    web_result = component_environment(manifest, "web", tokens, inherited)
    assert web_result["SIDERA_WEB_KNOWLEDGE_TOKEN"] == tokens[
        "knowledge_local_owner"
    ]


def test_restore_environment_separates_runtime_metadata_from_restored_data(
    tmp_path,
) -> None:
    manifest = manifest_for(tmp_path).model_copy(
        update={
            "run_root": tmp_path / "restored" / "payload",
            "repository": tmp_path / "restored" / "payload" / "repository",
            "runtime_root": tmp_path / "restored" / "runtime",
            "restore_mode": "read_only",
        }
    )
    tokens = {
        "knowledge_local_owner": "owner-token-value-that-is-long-enough",
        "knowledge_main_agent": "main-agent-token-value-that-is-long-enough",
        "knowledge_control_plane": "control-plane-token-value-that-is-long-enough",
        "web_knowledge": "web-token-value-that-is-long-enough",
    }

    result = component_environment(manifest, "control-plane", tokens, {})

    assert result["SIDERA_INTERVIEW_DEMO_MANIFEST"] == str(
        manifest.runtime_root / "manifest.json"
    )
    assert result["SIDERA_WORKSPACE_ROOT"] == str(manifest.run_root / "workspaces")
    assert result["SIDERA_ARTIFACT_ROOT"] == str(manifest.run_root / "artifacts")


def test_restore_demo_environment_forces_all_services_into_read_only_mode(
    tmp_path,
) -> None:
    manifest = manifest_for(tmp_path).model_copy(
        update={"restore_mode": "read_only"}
    )
    tokens = {
        "knowledge_local_owner": "owner-token-value-that-is-long-enough",
        "knowledge_main_agent": "main-agent-token-value-that-is-long-enough",
        "knowledge_control_plane": "control-plane-token-value-that-is-long-enough",
        "web_knowledge": "web-token-value-that-is-long-enough",
    }

    control = component_environment(manifest, "control-plane", tokens, {})
    knowledge = component_environment(manifest, "knowledge-service", tokens, {})
    gateway = component_environment(
        manifest, "main-agent-gateway", tokens, {}
    )

    assert control["SIDERA_RESTORE_READ_ONLY"] == "true"
    assert control["SIDERA_RESTORE_SCHEMA_VALIDATION"] == "true"
    assert knowledge["SIDERA_KNOWLEDGE_RESTORE_READ_ONLY"] == "true"
    assert knowledge["SIDERA_KNOWLEDGE_RESTORE_SCHEMA_VALIDATION"] == "true"
    assert gateway["SIDERA_MAIN_AGENT_RESTORE_READ_ONLY"] == "true"


def test_component_environment_forwards_only_an_explicit_pgpassfile(
    tmp_path,
) -> None:
    manifest = manifest_for(tmp_path)
    passfile = tmp_path / "pgpass"
    passfile.write_text("local-only", encoding="utf-8")
    tokens = create_demo_tokens(manifest.run_root)

    result = component_environment(
        manifest,
        "knowledge-service",
        tokens,
        {"PATH": "/usr/bin", "PGPASSFILE": "/tmp/ignored.pgpass"},
        pgpassfile=passfile,
    )

    assert result["PGPASSFILE"] == str(passfile)
    assert result["SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN"] == tokens[
        "knowledge_control_plane"
    ]


def test_port_allocator_returns_four_distinct_local_ports() -> None:
    ports = allocate_local_ports(4)

    assert len(ports) == 4
    assert len(set(ports)) == 4
    assert all(1024 <= port <= 65535 for port in ports)
