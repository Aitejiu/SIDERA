from __future__ import annotations

import json
import stat
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sidera_agents.contracts import NativeSkill, ResourceRef, ToolConnection
from sidera_agents.errors import AgentError
from sidera_agents.harnesses.opencode_config import (
    build_child_environment,
    build_mcp_servers,
    prepare_opencode_config,
)


def _skill(body: str = "Preserve this exact guidance.  ") -> NativeSkill:
    return NativeSkill(
        resource_ref=ResourceRef(workspace_id=uuid4(), resource_id=uuid4(), revision=1),
        name="review / skill",
        content=body,
        content_hash="sha256:" + sha256(body.encode()).hexdigest(),
    )


def _executable(tmp_path: Path) -> Path:
    path = tmp_path / "mcp tool"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_opencode_config_writes_exact_skills_to_private_instruction_files(tmp_path):
    skill = _skill()

    prepared = prepare_opencode_config({}, (skill,), private_root=tmp_path)

    try:
        config = json.loads(prepared.content)
        assert config["instructions"] == [str(prepared.skill_paths[0])]
        assert prepared.skill_paths[0].read_text(encoding="utf-8") == skill.content
        assert prepared.skill_hashes == (skill.content_hash,)
        assert prepared.skill_paths[0].parent.stat().st_mode & 0o077 == 0
    finally:
        prepared.close()
    assert not prepared.directory.exists()


@pytest.mark.parametrize(
    "projected_config",
    [
        {"model_id": "provider/model"},
        {"thinking": "deep"},
        {"temperature": 0.1},
        {"provider": {"apiKey": "synthetic"}},
    ],
)
def test_unconfirmed_or_secret_native_config_fails_explicitly(
    tmp_path, projected_config
):
    with pytest.raises(AgentError) as error:
        prepare_opencode_config(projected_config, (), private_root=tmp_path)

    assert error.value.code == "configuration_invalid"
    assert error.value.field == "projected_config"


def test_child_environment_is_exact_and_excludes_parent_secrets(tmp_path):
    prepared = prepare_opencode_config({}, (), private_root=tmp_path)
    parent = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LANG": "C",
        "OPENAI_API_KEY": "parent-provider-secret",
        "SIDERA_PARENT_SECRET": "parent-sidera-secret",
        "OPENCODE_CONFIG": "/outside/config.json",
        "RANDOM_PARENT_VALUE": "drop-me",
    }

    try:
        environment = build_child_environment(
            parent,
            {
                "PROFILE_MODE": "review",
                "AUTHORIZED_PROVIDER_KEY": SecretStr("profile-secret"),
            },
            prepared,
        )
        assert environment["PATH"] == parent["PATH"]
        assert environment["HOME"] == parent["HOME"]
        assert environment["PROFILE_MODE"] == "review"
        assert environment["AUTHORIZED_PROVIDER_KEY"] == "profile-secret"
        assert "OPENAI_API_KEY" not in environment
        assert "SIDERA_PARENT_SECRET" not in environment
        assert "RANDOM_PARENT_VALUE" not in environment
        assert environment["OPENCODE_CONFIG_CONTENT"] == prepared.content
        assert environment["OPENCODE_CONFIG_DIR"] == str(prepared.config_dir)
        assert environment["XDG_CONFIG_HOME"] == str(prepared.xdg_config_home)
    finally:
        prepared.close()


def test_stdio_mcp_projection_preserves_argv_and_scoped_secret(tmp_path):
    executable = _executable(tmp_path)
    token = "mcp-scoped-secret"
    tools = (
        ToolConnection(
            name="task-bridge",
            transport="stdio",
            endpoint_or_argv=(str(executable), "argument with spaces"),
            headers={},
            environment={"TOOL_TOKEN": SecretStr(token)},
        ),
    )

    servers = build_mcp_servers(tools, supports_http=False)

    assert servers == (
        {
            "name": "task-bridge",
            "command": str(executable),
            "args": ["argument with spaces"],
            "env": [{"name": "TOOL_TOKEN", "value": token}],
        },
    )


def test_http_mcp_is_gated_by_negotiated_capability():
    tool = ToolConnection(
        name="http-bridge",
        transport="http",
        endpoint_or_argv="https://mcp.example.invalid/v1",
        headers={"Authorization": SecretStr("scoped-token")},
        environment={},
    )

    with pytest.raises(AgentError) as error:
        build_mcp_servers((tool,), supports_http=False)
    assert error.value.code == "unsupported_capability"

    assert build_mcp_servers((tool,), supports_http=True) == (
        {
            "type": "http",
            "name": "http-bridge",
            "url": "https://mcp.example.invalid/v1",
            "headers": [{"name": "Authorization", "value": "scoped-token"}],
        },
    )


def test_http_mcp_allows_only_https_or_loopback_http():
    loopback = ToolConnection(
        name="local-bridge",
        transport="http",
        endpoint_or_argv="http://[::1]:8765/mcp",
        headers={},
        environment={},
    )

    assert build_mcp_servers((loopback,), supports_http=True)[0]["url"] == (
        "http://[::1]:8765/mcp"
    )

    remote_http = loopback.model_copy(
        update={"endpoint_or_argv": "http://mcp.example.invalid/mcp"}
    )
    with pytest.raises(AgentError) as error:
        build_mcp_servers((remote_http,), supports_http=True)
    assert error.value.code == "configuration_invalid"


def test_stdio_projection_rejects_non_regular_or_symlink_executable(tmp_path):
    target = _executable(tmp_path)
    link = tmp_path / "tool-link"
    link.symlink_to(target)
    tool = ToolConnection(
        name="unsafe",
        transport="stdio",
        endpoint_or_argv=(str(link),),
        headers={},
        environment={},
    )

    with pytest.raises(AgentError) as error:
        build_mcp_servers((tool,), supports_http=False)
    assert error.value.code == "configuration_invalid"
