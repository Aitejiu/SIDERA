from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from pydantic import SecretStr

from ..contracts import NativeSkill, ToolConnection
from ..errors import AgentError

_ENVIRONMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RESERVED_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "PATH",
        "PWD",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG_DIR",
        "CODEX_HOME",
        "PYTHONHOME",
        "PYTHONPATH",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    }
)
_RESERVED_ENVIRONMENT_PREFIXES = ("SIDERA_", "OPENCODE_", "CODEX_", "DYLD_")
_SAFE_PARENT_ENVIRONMENT = frozenset(
    {"PATH", "HOME", "TMPDIR", "TEMP", "LANG", "LC_ALL", "TERM"}
)


def _invalid(field: str, code: str = "configuration_invalid") -> AgentError:
    message = (
        "The selected native capability is not supported."
        if code == "unsupported_capability"
        else "OpenCode configuration is invalid."
    )
    return AgentError(code, message, field)


@dataclass(frozen=True, slots=True)
class PreparedOpenCodeConfig:
    directory: Path
    config_dir: Path
    xdg_config_home: Path
    xdg_data_home: Path
    xdg_state_home: Path
    xdg_cache_home: Path
    content: str
    skill_paths: tuple[Path, ...]
    skill_hashes: tuple[str, ...]

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def prepare_opencode_config(
    projected_config: Mapping[str, object],
    skills: tuple[NativeSkill, ...],
    *,
    private_root: Path | None = None,
    tools: tuple[ToolConnection, ...] = (),
) -> PreparedOpenCodeConfig:
    if projected_config:
        raise _invalid("projected_config")
    try:
        directory = Path(
            tempfile.mkdtemp(prefix="sidera-agents-opencode-", dir=private_root)
        )
        directory.chmod(0o700)
        config_dir = directory / "config"
        xdg_config_home = directory / "xdg-config"
        xdg_data_home = directory / "xdg-data"
        xdg_state_home = directory / "xdg-state"
        xdg_cache_home = directory / "xdg-cache"
        for path in (
            config_dir,
            xdg_config_home,
            xdg_data_home,
            xdg_state_home,
            xdg_cache_home,
        ):
            path.mkdir(mode=0o700)
            path.chmod(0o700)
        skill_dir = directory / "skills"
        paths: list[Path] = []
        hashes: list[str] = []
        if skills:
            skill_dir.mkdir(mode=0o700)
            for index, skill in enumerate(skills):
                path = skill_dir / f"skill-{index:04d}.md"
                path.write_text(skill.content, encoding="utf-8")
                path.chmod(0o600)
                paths.append(path)
                hashes.append(skill.content_hash)
        config: dict[str, object] = {
            "instructions": [str(path) for path in paths],
            "mcp": {},
            "plugin": [],
            "permission": _tool_permission_policy(tools),
        }
        content = json.dumps(config, sort_keys=True, separators=(",", ":"))
        return PreparedOpenCodeConfig(
            directory=directory,
            config_dir=config_dir,
            xdg_config_home=xdg_config_home,
            xdg_data_home=xdg_data_home,
            xdg_state_home=xdg_state_home,
            xdg_cache_home=xdg_cache_home,
            content=content,
            skill_paths=tuple(paths),
            skill_hashes=tuple(hashes),
        )
    except OSError:
        if "directory" in locals():
            shutil.rmtree(directory, ignore_errors=True)
        raise AgentError(
            "configuration_invalid", "Run-local configuration is unavailable."
        ) from None


def build_child_environment(
    parent: Mapping[str, str],
    profile_environment: Mapping[str, object],
    prepared: PreparedOpenCodeConfig,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in parent.items()
        if key in _SAFE_PARENT_ENVIRONMENT
        and isinstance(value, str)
        and "\x00" not in value
    }
    for key, raw_value in profile_environment.items():
        if (
            not isinstance(key, str)
            or _ENVIRONMENT_KEY.fullmatch(key) is None
            or key.upper() in _RESERVED_ENVIRONMENT_KEYS
            or key.upper().startswith(_RESERVED_ENVIRONMENT_PREFIXES)
        ):
            raise _invalid("environment")
        value = (
            raw_value.get_secret_value()
            if isinstance(raw_value, SecretStr)
            else raw_value
        )
        if not isinstance(value, str) or "\x00" in value:
            raise _invalid("environment")
        environment[key] = value
    environment["OPENCODE_CONFIG_CONTENT"] = prepared.content
    environment["OPENCODE_CONFIG_DIR"] = str(prepared.config_dir)
    environment["HOME"] = str(prepared.directory)
    environment["XDG_CONFIG_HOME"] = str(prepared.xdg_config_home)
    environment["XDG_DATA_HOME"] = str(prepared.xdg_data_home)
    environment["XDG_STATE_HOME"] = str(prepared.xdg_state_home)
    environment["XDG_CACHE_HOME"] = str(prepared.xdg_cache_home)
    return environment


def managed_opencode_config_sources() -> tuple[Path, ...]:
    if sys.platform == "darwin":
        import pwd

        managed_preferences = Path("/Library/Managed Preferences")
        try:
            username = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            username = "user"
        return (
            Path("/Library/Application Support/opencode/opencode.json"),
            Path("/Library/Application Support/opencode/opencode.jsonc"),
            managed_preferences / username / "ai.opencode.managed.plist",
            managed_preferences / "ai.opencode.managed.plist",
        )
    if sys.platform.startswith("linux"):
        return (
            Path("/etc/opencode/opencode.json"),
            Path("/etc/opencode/opencode.jsonc"),
        )
    if sys.platform == "win32":
        program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        return (
            program_data / "opencode" / "opencode.json",
            program_data / "opencode" / "opencode.jsonc",
        )
    return ()


def preflight_opencode_config_sources(workspace: Path) -> None:
    try:
        canonical_workspace = workspace.resolve(strict=True)
    except OSError:
        raise _invalid("workspace", "unsupported_capability") from None

    candidates: list[Path] = []
    for directory in (canonical_workspace, *canonical_workspace.parents):
        candidates.extend(
            (
                directory / "opencode.json",
                directory / "opencode.jsonc",
                directory / ".opencode",
            )
        )
    candidates.extend(managed_opencode_config_sources())
    for candidate in candidates:
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise _invalid("configuration_sources", "unsupported_capability") from None
        raise _invalid("configuration_sources", "unsupported_capability")


def _tool_permission_policy(tools: tuple[ToolConnection, ...]) -> dict[str, str]:
    policy = {"*": "deny"}
    names: set[str] = set()
    for tool in tools:
        name = tool.name.strip()
        if not name or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None or name in names:
            raise _invalid("tools")
        names.add(name)
        policy[f"{name}_*"] = "allow"
    return policy


def build_mcp_servers(
    tools: tuple[ToolConnection, ...], *, supports_http: bool
) -> tuple[dict[str, object], ...]:
    servers: list[dict[str, object]] = []
    names: set[str] = set()
    for tool in tools:
        name = tool.name.strip()
        if not name or "\x00" in name or name in names:
            raise _invalid("tools")
        names.add(name)
        if tool.transport == "stdio":
            servers.append(_stdio_server(tool, name))
        elif tool.transport == "http":
            if not supports_http:
                raise _invalid("tools", "unsupported_capability")
            servers.append(_http_server(tool, name))
        else:
            raise _invalid("tools", "unsupported_capability")
    return tuple(servers)


def _stdio_server(tool: ToolConnection, name: str) -> dict[str, object]:
    argv = tool.endpoint_or_argv
    if (
        not isinstance(argv, tuple)
        or not argv
        or any(not isinstance(part, str) or not part or "\x00" in part for part in argv)
    ):
        raise _invalid("tools")
    executable = PurePosixPath(argv[0])
    if (
        not executable.is_absolute()
        or ".." in executable.parts
        or "~" in executable.parts
        or argv[0].startswith("~")
    ):
        raise _invalid("tools")
    path = Path(argv[0])
    try:
        info = path.lstat()
    except OSError:
        raise _invalid("tools") from None
    executable_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not info.st_mode & executable_bits
        or not os.access(path, os.X_OK)
    ):
        raise _invalid("tools")
    environment = _tool_environment(tool.environment)
    return {
        "name": name,
        "command": argv[0],
        "args": list(argv[1:]),
        "env": [
            {"name": key, "value": environment[key]} for key in sorted(environment)
        ],
    }


def _tool_environment(values: Mapping[str, SecretStr]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, secret in values.items():
        if (
            _ENVIRONMENT_KEY.fullmatch(key) is None
            or key.upper() in _RESERVED_ENVIRONMENT_KEYS
            or key.upper().startswith(_RESERVED_ENVIRONMENT_PREFIXES)
        ):
            raise _invalid("tools")
        value = secret.get_secret_value()
        if "\x00" in value:
            raise _invalid("tools")
        environment[key] = value
    return environment


def _http_server(tool: ToolConnection, name: str) -> dict[str, object]:
    endpoint = tool.endpoint_or_argv
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or any(character.isspace() for character in endpoint)
    ):
        raise _invalid("tools")
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise _invalid("tools") from None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise _invalid("tools")
    if parsed.scheme == "http":
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = host.casefold() == "localhost"
        if not loopback:
            raise _invalid("tools")
    headers: list[dict[str, str]] = []
    for key, secret in sorted(tool.headers.items()):
        value = secret.get_secret_value()
        if (
            not key
            or any(
                not (character.isalnum() or character in "!#$%&'*+-.^_`|~")
                for character in key
            )
            or any(character in value for character in "\x00\r\n")
        ):
            raise _invalid("tools")
        headers.append({"name": key, "value": value})
    return {"type": "http", "name": name, "url": endpoint, "headers": headers}
