from __future__ import annotations

import json
import stat
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sidera_agents.contracts import NativeSkill, ResourceRef, ToolConnection
from sidera_agents.errors import AgentError
from sidera_agents.harnesses import opencode_config
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


def _http_tool(name: str = "SIDERA Task Tools") -> ToolConnection:
    return ToolConnection(
        name=name,
        transport="http",
        endpoint_or_argv="http://127.0.0.1:9912/mcp",
        headers={"Authorization": SecretStr("N23_FAKE_HTTP_SECRET")},
        environment={},
    )


def _invalid_tool_identities(case: str) -> tuple[ToolConnection, ...]:
    if case == "collision":
        return (_http_tool(), _http_tool("SIDERA_Task_Tools"))
    tool = _http_tool("task-bridge")
    if case == "endpoint":
        changed = tool.model_copy(
            update={"endpoint_or_argv": "http://127.0.0.1:9913/mcp"}
        )
    elif case == "header":
        changed = tool.model_copy(
            update={"headers": {"Authorization": SecretStr("N23_FAKE_OTHER_HEADER")}}
        )
    else:
        changed = tool.model_copy(
            update={"environment": {"TOOL_TOKEN": SecretStr("N23_FAKE_OTHER_ENV")}}
        )
    return (tool, changed)


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


def test_explicit_model_projects_one_provider_without_fallback(tmp_path):
    prepared = prepare_opencode_config(
        {"model_id": "opencode-go/deepseek-v4.1-flash"}, (), private_root=tmp_path
    )
    try:
        config = json.loads(prepared.content)
        assert config["model"] == "opencode-go/deepseek-v4.1-flash"
        assert config["small_model"] == "opencode-go/deepseek-v4.1-flash"
        assert config["enabled_providers"] == ["opencode-go"]
        assert "provider" not in config
        assert config["permission"] == {"*": "deny"}
    finally:
        prepared.close()


@pytest.mark.parametrize("projected", [{}, {"model_id": None}])
def test_absent_model_does_not_invent_native_override(tmp_path, projected):
    prepared = prepare_opencode_config(projected, (), private_root=tmp_path)
    try:
        config = json.loads(prepared.content)
        assert not {"model", "small_model", "enabled_providers"} & config.keys()
    finally:
        prepared.close()


@pytest.mark.parametrize(
    "route",
    [
        "",
        "provider",
        "/model",
        "provider/",
        "provider//model",
        "provider/../model",
        "provider/model\n",
        "provider/mo\x00del",
        "provider/{env:MODEL}",
        "provider/$(MODEL)",
        "https://provider/model",
        "provider/model?apiKey=value",
        "provider/sk-1234567890abcdefghijkl",
        "provider/ghp_1234567890abcdefghijkl",
        "p" * 65 + "/model",
        "provider/" + "m" * 193,
        True,
    ],
)
def test_invalid_model_route_fails_before_private_config_allocation(tmp_path, route):
    with pytest.raises(AgentError) as error:
        prepare_opencode_config({"model_id": route}, (), private_root=tmp_path)
    assert error.value.code == "configuration_invalid"
    assert error.value.field == "model_id"
    assert "1234567890abcdefghijkl" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_child_environment_is_exact_and_excludes_parent_secrets(tmp_path):
    prepared = prepare_opencode_config({}, (), private_root=tmp_path)
    parent = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/parent/home",
        "LANG": "C",
        "XDG_DATA_HOME": "/parent/data",
        "XDG_STATE_HOME": "/parent/state",
        "XDG_CACHE_HOME": "/parent/cache",
        "OPENAI_API_KEY": "parent-provider-secret",
        "SIDERA_PARENT_SECRET": "parent-sidera-secret",
        "OPENCODE_CONFIG": "/outside/config.json",
        "OPENCODE_EXPERIMENTAL": "1",
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
        assert environment["HOME"] == str(prepared.directory)
        assert environment["XDG_CONFIG_HOME"] == str(prepared.xdg_config_home)
        assert environment["XDG_DATA_HOME"] == str(prepared.xdg_data_home)
        assert environment["XDG_STATE_HOME"] == str(prepared.xdg_state_home)
        assert environment["XDG_CACHE_HOME"] == str(prepared.xdg_cache_home)
        assert environment["PROFILE_MODE"] == "review"
        assert environment["AUTHORIZED_PROVIDER_KEY"] == "profile-secret"
        assert "OPENAI_API_KEY" not in environment
        assert "SIDERA_PARENT_SECRET" not in environment
        assert "OPENCODE_EXPERIMENTAL" not in environment
        assert "OPENCODE_CONFIG" not in environment
        assert "RANDOM_PARENT_VALUE" not in environment
        assert environment["OPENCODE_CONFIG_CONTENT"] == prepared.content
        assert environment["OPENCODE_CONFIG_DIR"] == str(prepared.config_dir)
        for key in (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        ):
            path = Path(environment[key])
            assert path.is_relative_to(prepared.directory)
            assert path.stat().st_mode & 0o077 == 0
    finally:
        prepared.close()


def test_child_environment_projects_only_explicit_profile_network_values(tmp_path):
    parent = {
        "PATH": "/usr/bin:/bin",
        "HTTP_PROXY": "http://127.0.0.1:9001",
        "HTTPS_PROXY": "http://127.0.0.1:9002",
        "NO_PROXY": "parent-only.invalid",
        "ALL_PROXY": "socks5://127.0.0.1:9003",
        "OPENAI_API_KEY": "N23_FAKE_PARENT_CREDENTIAL",
    }
    explicit = {
        "HTTP_PROXY": "http://127.0.0.1:7897",
        "HTTPS_PROXY": "http://127.0.0.1:7897",
        "NO_PROXY": "localhost,127.0.0.1,::1,[::1]",
    }
    prepared = prepare_opencode_config({}, (), private_root=tmp_path)

    try:
        without_explicit = build_child_environment(parent, {}, prepared)
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "ALL_PROXY",
            "OPENAI_API_KEY",
        ):
            assert key not in without_explicit

        environment = build_child_environment(parent, explicit, prepared)
        assert environment["HTTP_PROXY"] == "http://127.0.0.1:7897"
        assert environment["HTTPS_PROXY"] == "http://127.0.0.1:7897"
        assert environment["NO_PROXY"] == "localhost,127.0.0.1,::1,[::1]"
        assert "ALL_PROXY" not in environment
        assert "OPENAI_API_KEY" not in environment
        for child in (without_explicit, environment):
            assert child["PATH"] == "/usr/bin:/bin"
            assert child["HOME"] == str(prepared.directory)
            assert child["XDG_CONFIG_HOME"] == str(prepared.xdg_config_home)
            assert child["XDG_DATA_HOME"] == str(prepared.xdg_data_home)
            assert child["XDG_STATE_HOME"] == str(prepared.xdg_state_home)
            assert child["XDG_CACHE_HOME"] == str(prepared.xdg_cache_home)
            assert child["OPENCODE_CONFIG_CONTENT"] == prepared.content
            assert child["OPENCODE_CONFIG_DIR"] == str(prepared.config_dir)
    finally:
        prepared.close()
    assert not prepared.directory.exists()


@pytest.mark.parametrize("destination", ["OPENCODE_API_KEY", "BOUND_PROVIDER_KEY"])
def test_provider_credential_uses_placeholder_and_child_only_secret(
    tmp_path, destination
):
    secret = "N09_FAKE_EXPLICIT_PROVIDER_SECRET"
    prepared = prepare_opencode_config(
        {"model_id": "opencode-go/deepseek-v4.1-flash", "provider_env": destination},
        (),
        private_root=tmp_path,
    )
    try:
        config = json.loads(prepared.content)
        assert config["provider"] == {
            "opencode-go": {"options": {"apiKey": "{env:" + destination + "}"}}
        }
        assert secret not in prepared.content
        environment = build_child_environment(
            {"OPENCODE_API_KEY": "N09_FAKE_PARENT_KEY"},
            {destination: SecretStr(secret)},
            prepared,
        )
        assert environment[destination] == secret
        assert "N09_FAKE_PARENT_KEY" not in repr(environment)
    finally:
        prepared.close()


@pytest.mark.parametrize("value", ["N09_FAKE_PLAIN_KEY", None])
def test_provider_routing_requires_authorized_secret_value(tmp_path, value):
    prepared = prepare_opencode_config(
        {
            "model_id": "opencode-go/deepseek-v4.1-flash",
            "provider_env": "OPENCODE_API_KEY",
        },
        (),
        private_root=tmp_path,
    )
    try:
        with pytest.raises(AgentError) as error:
            build_child_environment({}, {"OPENCODE_API_KEY": value}, prepared)
        assert error.value.code == "configuration_invalid"
        assert error.value.field == "provider_env"
        assert "N09_FAKE_PLAIN_KEY" not in str(error.value)
    finally:
        prepared.close()


@pytest.mark.parametrize(
    "projected",
    [
        {"provider_env": "OPENCODE_API_KEY"},
        {"model_id": "opencode-go/model", "provider_env": "OPENCODE_CONFIG_CONTENT"},
        {"model_id": "opencode-go/model", "provider_env": "OPENCODE_EXPERIMENTAL"},
        {"model_id": "opencode-go/model", "provider_env": "XDG_DATA_HOME"},
        {"model_id": "opencode-go/model", "provider_env": "BAD-NAME"},
        {"model_id": "opencode-go/model", "provider_env": "{env:KEY}"},
        {"model_id": "opencode-go/model", "provider_env": {"apiKey": "fake"}},
        {"model_id": "other/model", "provider_env": "OPENCODE_API_KEY"},
    ],
)
def test_provider_routing_rejects_missing_model_or_invalid_destination(
    tmp_path, projected
):
    with pytest.raises(AgentError) as error:
        prepare_opencode_config(projected, (), private_root=tmp_path)
    assert error.value.code == "configuration_invalid"
    assert error.value.field == "provider_env"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "key", ["OPENCODE_CONFIG", "OPENCODE_EXPERIMENTAL", "OPENCODE_CONFIG_DIR"]
)
def test_other_reserved_opencode_keys_remain_rejected_for_secrets(tmp_path, key):
    prepared = prepare_opencode_config({}, (), private_root=tmp_path)
    try:
        with pytest.raises(AgentError) as error:
            build_child_environment(
                {}, {key: SecretStr("N09_FAKE_RESERVED_SECRET")}, prepared
            )
        assert error.value.field == "environment"
    finally:
        prepared.close()


def test_prepare_config_denies_unlisted_tools_and_projects_only_bound_mcp(
    tmp_path,
):
    tools = (
        ToolConnection(
            name="task-bridge",
            transport="stdio",
            endpoint_or_argv=("/not-resolved-during-policy-build",),
            headers={},
            environment={},
        ),
    )

    prepared = prepare_opencode_config({}, (), private_root=tmp_path, tools=tools)

    try:
        config = json.loads(prepared.content)
        assert config["permission"] == {
            "*": "deny",
            "task-bridge_*": "allow",
        }
        assert config["mcp"] == {}
        assert config["plugin"] == []
        assert not any(
            action == "allow" and tool != "task-bridge_*"
            for tool, action in config["permission"].items()
            if tool != "*"
        )
    finally:
        prepared.close()


@pytest.mark.parametrize(
    "mode,allowed",
    [
        ("none", {}),
        ("read", {"read": "allow", "glob": "allow", "grep": "allow"}),
        (
            "write",
            {
                "read": "allow",
                "glob": "allow",
                "grep": "allow",
                "edit": "allow",
                "bash": "allow",
            },
        ),
    ],
)
def test_native_tools_grant_exact_declared_permissions_preserving_scoped_mcp(
    tmp_path, mode, allowed
):
    tool = ToolConnection(
        name="task-bridge",
        transport="stdio",
        endpoint_or_argv=("/not-spawned",),
        headers={},
        environment={},
    )
    prepared = prepare_opencode_config(
        {"native_tools": mode}, (), private_root=tmp_path, tools=(tool,)
    )
    try:
        config = json.loads(prepared.content)
        assert config["permission"] == {
            "*": "deny",
            **allowed,
            "task-bridge_*": "allow",
        }
        assert (
            not {"task", "skill", "webfetch", "external_directory", "*_*"}
            & config["permission"].keys()
        )
    finally:
        prepared.close()


@pytest.mark.parametrize("mode", ["all", "READ", "bash", "*", None, True, ["read"]])
def test_invalid_native_tool_mode_fails_before_allocation(tmp_path, mode):
    with pytest.raises(AgentError) as error:
        prepare_opencode_config({"native_tools": mode}, (), private_root=tmp_path)
    assert error.value.field == "native_tools"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("case", ["collision", "endpoint", "header", "environment"])
def test_prepare_invalid_tool_identity_creates_no_private_directory(tmp_path, case):
    with pytest.raises(AgentError) as error:
        prepare_opencode_config(
            {}, (_skill(),), private_root=tmp_path, tools=_invalid_tool_identities(case)
        )

    assert error.value.code == "configuration_invalid"
    assert error.value.field == "tools"
    assert "N23_" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("case", ["collision", "endpoint", "header", "environment"])
def test_prepare_invalid_tool_identity_forbids_mkdtemp(tmp_path, monkeypatch, case):
    def forbidden_allocation(*args, **kwargs):
        raise AssertionError("Invalid tool identity attempted private allocation.")

    monkeypatch.setattr(opencode_config.tempfile, "mkdtemp", forbidden_allocation)

    with pytest.raises(AgentError) as error:
        prepare_opencode_config(
            {}, (_skill(),), private_root=tmp_path, tools=_invalid_tool_identities(case)
        )

    assert error.value.code == "configuration_invalid"
    assert error.value.field == "tools"
    assert list(tmp_path.iterdir()) == []


def test_prepare_product_tools_matches_acp_aliases_without_private_secrets(tmp_path):
    tools = (
        _http_tool(),
        _http_tool(),
        ToolConnection(
            name="Local Tool",
            transport="stdio",
            endpoint_or_argv=(str(_executable(tmp_path)),),
            headers={},
            environment={"TOOL_TOKEN": SecretStr("N23_FAKE_TOOL_ENV_SECRET")},
        ),
    )
    originals = tuple(tool.model_copy(deep=True) for tool in tools)
    prepared = prepare_opencode_config(
        {}, (_skill(),), private_root=tmp_path, tools=tools
    )

    try:
        config = json.loads(prepared.content)
        assert config["permission"] == {
            "*": "deny",
            "SIDERA_Task_Tools_*": "allow",
            "Local_Tool_*": "allow",
        }
        assert [
            server["name"] for server in build_mcp_servers(tools, supports_http=True)
        ] == [
            "SIDERA_Task_Tools",
            "Local_Tool",
        ]
        assert config["mcp"] == {}
        assert "N23_" not in prepared.content
        assert all(
            "N23_" not in path.read_text(encoding="utf-8")
            for path in prepared.directory.rglob("*")
            if path.is_file()
        )
        assert tools == originals
    finally:
        prepared.close()

    assert not prepared.directory.exists()


def test_prepare_config_oserror_removes_its_owned_directory(tmp_path, monkeypatch):
    def failed_skill_write(*args, **kwargs):
        raise OSError("Synthetic private skill write failure.")

    monkeypatch.setattr(Path, "write_text", failed_skill_write)

    with pytest.raises(AgentError) as error:
        prepare_opencode_config({}, (_skill(),), private_root=tmp_path)

    assert error.value.code == "configuration_invalid"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("key", ["XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"])
def test_profile_cannot_override_private_xdg_roots(tmp_path, key):
    prepared = prepare_opencode_config({}, (), private_root=tmp_path)

    try:
        with pytest.raises(AgentError) as error:
            build_child_environment({"PATH": "/usr/bin"}, {key: "/outside"}, prepared)

        assert error.value.code == "configuration_invalid"
        assert error.value.field == "environment"
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


@pytest.mark.parametrize("projection", ["mcp", "permission"])
@pytest.mark.parametrize(
    "name,alias",
    [
        ("SIDERA Task Tools", "SIDERA_Task_Tools"),
        ("  SIDERA Task Tools  ", "SIDERA_Task_Tools"),
        ("task.bridge/v1!", "task_bridge_v1_"),
        ("m\u00fcp-tool", "m_p-tool"),
        ("\u4efb\u52a1\u5de5\u5177", "____"),
        ("task-bridge", "task-bridge"),
        ("task_bridge", "task_bridge"),
        ("Task42", "Task42"),
    ],
)
def test_tool_identity_projection_uses_native_safe_alias(projection, name, alias):
    tool = _http_tool(name)
    original = tool.model_copy(deep=True)

    if projection == "mcp":
        servers = build_mcp_servers((tool,), supports_http=True)
        assert [server["name"] for server in servers] == [alias]
    else:
        assert opencode_config._tool_permission_policy(
            (tool,), native_tools="none"
        ) == {"*": "deny", f"{alias}_*": "allow"}

    assert tool == original


@pytest.mark.parametrize("projection", ["mcp", "permission"])
def test_exact_product_tool_duplicate_projects_once(projection):
    tools = (_http_tool(), _http_tool())

    if projection == "mcp":
        servers = build_mcp_servers(tools, supports_http=True)
        assert [server["name"] for server in servers] == ["SIDERA_Task_Tools"]
    else:
        assert opencode_config._tool_permission_policy(tools, native_tools="none") == {
            "*": "deny",
            "SIDERA_Task_Tools_*": "allow",
        }


@pytest.mark.parametrize("projection", ["mcp", "permission"])
def test_exact_tool_duplicates_coalesce_in_first_order_without_mutation(projection):
    tools = (
        _http_tool("Z Tool"),
        _http_tool("a-tool"),
        _http_tool("Z Tool"),
        _http_tool("Third/Tool"),
        _http_tool("a-tool"),
    )
    originals = tuple(tool.model_copy(deep=True) for tool in tools)

    if projection == "mcp":
        servers = build_mcp_servers(tools, supports_http=True)
        assert [server["name"] for server in servers] == [
            "Z_Tool",
            "a-tool",
            "Third_Tool",
        ]
    else:
        policy = opencode_config._tool_permission_policy(tools, native_tools="none")
        assert policy == {
            "*": "deny",
            "Z_Tool_*": "allow",
            "a-tool_*": "allow",
            "Third_Tool_*": "allow",
        }
        assert list(policy) == ["*", "Z_Tool_*", "a-tool_*", "Third_Tool_*"]

    assert tools == originals


def test_exact_tool_duplicates_preserve_stdio_secret_environment(tmp_path):
    tool = ToolConnection(
        name="task-bridge",
        transport="stdio",
        endpoint_or_argv=(str(_executable(tmp_path)), "argument with spaces"),
        headers={},
        environment={"TOOL_TOKEN": SecretStr("N23_FAKE_TOOL_ENV_SECRET")},
    )
    original = tool.model_copy(deep=True)

    servers = build_mcp_servers((tool, tool.model_copy(deep=True)), supports_http=False)

    assert len(servers) == 1
    assert servers[0]["name"] == "task-bridge"
    assert servers[0]["args"] == ["argument with spaces"]
    assert servers[0]["env"] == [
        {"name": "TOOL_TOKEN", "value": "N23_FAKE_TOOL_ENV_SECRET"}
    ]
    assert tool == original


@pytest.mark.parametrize("projection", ["mcp", "permission"])
@pytest.mark.parametrize(
    "difference", ["transport", "endpoint", "argv", "header", "environment"]
)
def test_tool_identity_rejects_different_connections_with_same_name(
    tmp_path, projection, difference
):
    tool = _http_tool("task-bridge")
    if difference in {"argv", "environment"}:
        tool = ToolConnection(
            name="task-bridge",
            transport="stdio",
            endpoint_or_argv=(str(_executable(tmp_path)), "first argument"),
            headers={},
            environment={"TOOL_TOKEN": SecretStr("N23_FAKE_TOOL_ENV_SECRET")},
        )
    if difference == "transport":
        changed = tool.model_copy(
            update={
                "transport": "stdio",
                "endpoint_or_argv": (str(_executable(tmp_path)),),
            }
        )
    elif difference == "endpoint":
        changed = tool.model_copy(
            update={"endpoint_or_argv": "http://127.0.0.1:9913/mcp"}
        )
    elif difference == "argv":
        changed = tool.model_copy(
            update={"endpoint_or_argv": (tool.endpoint_or_argv[0], "other argument")}
        )
    elif difference == "header":
        changed = tool.model_copy(
            update={"headers": {"Authorization": SecretStr("N23_FAKE_OTHER_HEADER")}}
        )
    else:
        changed = tool.model_copy(
            update={"environment": {"TOOL_TOKEN": SecretStr("N23_FAKE_OTHER_ENV")}}
        )
    tools = (tool, changed)
    originals = tuple(connection.model_copy(deep=True) for connection in tools)

    with pytest.raises(AgentError) as error:
        if projection == "mcp":
            build_mcp_servers(tools, supports_http=True)
        else:
            opencode_config._tool_permission_policy(tools, native_tools="none")

    assert error.value.code == "configuration_invalid"
    assert error.value.field == "tools"
    assert "N23_" not in str(error.value)
    assert tools == originals


@pytest.mark.parametrize("projection", ["mcp", "permission"])
@pytest.mark.parametrize(
    "names",
    [
        ("SIDERA Task Tools", "SIDERA_Task_Tools"),
        ("task.bridge", "task/bridge"),
        ("m\u00fcp", "m_p"),
        ("task-bridge", " task-bridge "),
    ],
)
def test_tool_identity_rejects_distinct_names_with_same_alias(projection, names):
    tools = tuple(_http_tool(name) for name in names)

    with pytest.raises(AgentError) as error:
        if projection == "mcp":
            build_mcp_servers(tools, supports_http=True)
        else:
            opencode_config._tool_permission_policy(tools, native_tools="none")

    assert error.value.code == "configuration_invalid"
    assert error.value.field == "tools"
    assert "N23_" not in str(error.value)


@pytest.mark.parametrize("projection", ["mcp", "permission"])
@pytest.mark.parametrize("name", ["", " \t\n ", "task\x00tool", "\x00"])
def test_tool_identity_rejects_empty_or_nul_name(projection, name):
    with pytest.raises(AgentError) as error:
        if projection == "mcp":
            build_mcp_servers((_http_tool(name),), supports_http=True)
        else:
            opencode_config._tool_permission_policy(
                (_http_tool(name),), native_tools="none"
            )

    assert error.value.code == "configuration_invalid"
    assert error.value.field == "tools"


def test_managed_macos_policy_uses_os_account_not_login_environment(
    monkeypatch,
):
    monkeypatch.setattr(opencode_config.sys, "platform", "darwin")
    pwd_module = pytest.importorskip("pwd")
    for variable in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.setenv(variable, f"poison-{variable.lower()}")
    monkeypatch.setattr(opencode_config.os, "geteuid", lambda: 501)

    def lookup_account(uid):
        assert uid == 501
        return SimpleNamespace(pw_name="os-account")

    monkeypatch.setattr(pwd_module, "getpwuid", lookup_account)

    paths = opencode_config.managed_opencode_config_sources()

    assert paths[2] == Path(
        "/Library/Managed Preferences/os-account/ai.opencode.managed.plist"
    )


def test_managed_macos_policy_uses_effective_os_account(monkeypatch):
    monkeypatch.setattr(opencode_config.sys, "platform", "darwin")
    pwd_module = pytest.importorskip("pwd")
    for variable in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.setenv(variable, f"poison-{variable.lower()}")
    monkeypatch.setattr(opencode_config.os, "getuid", lambda: 501)
    monkeypatch.setattr(opencode_config.os, "geteuid", lambda: 502)
    accounts = {501: "real-account", 502: "effective-account"}
    monkeypatch.setattr(
        pwd_module,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name=accounts[uid]),
    )

    paths = opencode_config.managed_opencode_config_sources()

    assert paths[2] == Path(
        "/Library/Managed Preferences/effective-account/ai.opencode.managed.plist"
    )


def test_managed_macos_policy_falls_back_when_os_account_lookup_fails(
    monkeypatch,
):
    monkeypatch.setattr(opencode_config.sys, "platform", "darwin")
    pwd_module = pytest.importorskip("pwd")
    for variable in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.setenv(variable, f"poison-{variable.lower()}")
    monkeypatch.setattr(opencode_config.os, "geteuid", lambda: 501)

    def lookup_account(uid):
        assert uid == 501
        raise KeyError(uid)

    monkeypatch.setattr(pwd_module, "getpwuid", lookup_account)

    paths = opencode_config.managed_opencode_config_sources()

    assert paths[2] == Path(
        "/Library/Managed Preferences/user/ai.opencode.managed.plist"
    )


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
