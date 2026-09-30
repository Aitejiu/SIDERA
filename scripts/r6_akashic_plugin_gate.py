from __future__ import annotations

import argparse
import importlib.util
import json
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
AKASHIC_ROOT = REPO_ROOT / "akashic-agent"
if str(AKASHIC_ROOT) not in sys.path:
    sys.path.insert(0, str(AKASHIC_ROOT))

from agent.plugins.artifacts import read_pointers, resolve_pointer
from agent.plugins.importer import FreshPluginImporter
from agent.plugins.manifest import load_plugin_manifest, manifest_path
from agent.plugins.registry import MetadataKind, plugin_registry

PLUGIN_ID = "sidera-task-bridge@sidera"
PLUGIN_NAME = "sidera-task-bridge"
MARKETPLACE = "sidera"
_SHA1 = re.compile(r"[0-9a-f]{40}")
_REVISION_TOOL_CONTRACTS: dict[str, tuple[str, dict[str, str], set[str]]] = {
    "sidera_create_plan_revision": (
        "write",
        {
            "root_task_id": "string",
            "goal": "string",
            "constraints": "array<string>",
            "acceptance_criteria": "array<string>",
        },
        {"root_task_id", "goal", "constraints", "acceptance_criteria"},
    ),
    "sidera_get_plan_revision": (
        "read-only",
        {"planning_request_id": "string"},
        {"planning_request_id"},
    ),
    "sidera_confirm_plan_revision": (
        "write",
        {
            "planning_request_id": "string",
            "request_version": "number",
            "proposal_revision": "number",
            "proposal_content_hash": "string",
            "diff_hash": "string",
            "base_snapshot_id": "string",
            "base_content_hash": "string",
            "explicit_user_confirmation": "boolean",
        },
        {
            "planning_request_id",
            "request_version",
            "proposal_revision",
            "proposal_content_hash",
            "diff_hash",
            "base_snapshot_id",
            "base_content_hash",
            "explicit_user_confirmation",
        },
    ),
}


class _GateFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_plugin_release(
    manifest_file: Path | str,
    runtime_dir: Path | str,
) -> dict[str, object]:
    """Validate one acceptance run's private, stable SIDERA bridge artifact."""

    try:
        return _check_plugin_release(Path(manifest_file), Path(runtime_dir))
    except _GateFailure as failure:
        return {"ok": False, "code": failure.code}
    except (
        OSError,
        ValueError,
        TypeError,
        UnicodeError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        return {"ok": False, "code": "artifact_invalid"}


def _check_plugin_release(
    manifest_file: Path,
    runtime_dir: Path,
) -> dict[str, object]:
    manifest_file = manifest_file.expanduser()
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise _GateFailure("manifest_invalid")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise _GateFailure("manifest_invalid") from error
    if not isinstance(manifest, dict):
        raise _GateFailure("manifest_invalid")

    runtime_input = runtime_dir.expanduser()
    if runtime_input.is_symlink() or not runtime_input.is_dir():
        raise _GateFailure("runtime_invalid")
    runtime = runtime_input.resolve(strict=True)
    plugin_home = runtime / "akashic-plugin-home"
    receipt_path = runtime / "akashic-plugin-release.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise _GateFailure("receipt_missing")
    if receipt_path.stat().st_mode & 0o077:
        raise _GateFailure("receipt_permissions_invalid")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise _GateFailure("receipt_invalid") from error
    if not isinstance(receipt, dict):
        raise _GateFailure("receipt_invalid")

    run_id = manifest.get("run_id")
    workspace_id = manifest.get("workspace_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(workspace_id, str)
        or not workspace_id
        or receipt.get("run_id") != run_id
        or receipt.get("workspace_id") != workspace_id
    ):
        raise _GateFailure("run_identity_mismatch")
    if receipt.get("plugin_home") != str(plugin_home):
        raise _GateFailure("plugin_home_mismatch")
    if (
        receipt.get("plugin_id") != PLUGIN_ID
        or receipt.get("marketplace") != MARKETPLACE
        or receipt.get("schema_version") != 1
    ):
        raise _GateFailure("plugin_identity_mismatch")
    expected_revision = receipt.get("split_commit")
    source_revision = receipt.get("source_commit")
    if (
        not isinstance(expected_revision, str)
        or _SHA1.fullmatch(expected_revision) is None
        or not isinstance(source_revision, str)
        or _SHA1.fullmatch(source_revision) is None
    ):
        raise _GateFailure("receipt_invalid")

    _reject_symlink_chain(plugin_home, plugin_home)
    if plugin_home.exists() and (
        not plugin_home.is_dir() or stat.S_IMODE(plugin_home.stat().st_mode) != 0o700
    ):
        raise _GateFailure("plugin_home_permissions_invalid")
    installed_manifest_path = manifest_path(plugin_home)
    if installed_manifest_path.is_symlink():
        raise _GateFailure("plugin_manifest_invalid")
    try:
        installed = load_plugin_manifest(plugin_home)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        raise _GateFailure("plugin_manifest_invalid") from error
    if PLUGIN_ID not in installed:
        raise _GateFailure("plugin_not_enabled")
    if installed[PLUGIN_ID] is not True:
        raise _GateFailure("plugin_disabled")

    plugin_base = plugin_home / "cache" / MARKETPLACE / PLUGIN_NAME
    _reject_symlink_chain(plugin_home, plugin_base)
    try:
        pointers = read_pointers(plugin_base)
    except (OSError, ValueError) as error:
        raise _GateFailure("stable_pointer_invalid") from error
    if pointers is None or pointers.stable.path is None:
        raise _GateFailure("stable_pointer_missing")
    try:
        stable_root = resolve_pointer(plugin_base, pointers.stable)
    except (OSError, ValueError) as error:
        raise _GateFailure("stable_pointer_invalid") from error
    if stable_root is None:
        raise _GateFailure("stable_pointer_missing")
    stable_root = stable_root.resolve(strict=True)
    if not stable_root.is_relative_to(plugin_base.resolve(strict=True)):
        raise _GateFailure("stable_pointer_invalid")

    git_metadata = stable_root / ".git"
    if git_metadata.is_symlink() or not git_metadata.is_dir():
        raise _GateFailure("artifact_invalid")
    actual_revision = _git_value(stable_root, "rev-parse", "HEAD")
    if _SHA1.fullmatch(actual_revision) is None:
        raise _GateFailure("artifact_invalid")
    if actual_revision != expected_revision:
        raise _GateFailure("revision_mismatch")
    git_root = _git_value(stable_root, "rev-parse", "--show-toplevel")
    if Path(git_root).resolve(strict=True) != stable_root:
        raise _GateFailure("artifact_invalid")
    diff = subprocess.run(
        ["git", "-C", str(stable_root), "diff", "--quiet", "HEAD", "--"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if diff.returncode == 1:
        raise _GateFailure("artifact_modified")
    if diff.returncode != 0:
        raise _GateFailure("artifact_invalid")

    tool_names = _inspect_tool_contract(stable_root)
    return {
        "ok": True,
        "run_id": run_id,
        "plugin_id": PLUGIN_ID,
        "stable_source_revision": actual_revision,
        "tool_names": tool_names,
    }


def _reject_symlink_chain(root: Path, target: Path) -> None:
    if root.is_symlink():
        raise _GateFailure("plugin_home_invalid")
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise _GateFailure("plugin_home_mismatch") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise _GateFailure("stable_pointer_invalid")


def _git_value(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _GateFailure("artifact_invalid") from error
    if result.returncode != 0:
        raise _GateFailure("artifact_invalid")
    return result.stdout.strip()


def _inspect_tool_contract(plugin_root: Path) -> list[str]:
    module_name = f"sidera_r6_release_gate_{uuid.uuid4().hex}"
    importer = FreshPluginImporter()
    importer.register(module_name, plugin_root)
    root_spec = importer.root_spec(module_name, plugin_root / "plugin.py")
    if root_spec is None or root_spec.loader is None:
        importer.unregister(module_name)
        raise _GateFailure("plugin_load_failed")
    module = importlib.util.module_from_spec(root_spec)
    sys.modules[module_name] = module
    try:
        root_spec.loader.exec_module(module)
        plugin_class = plugin_registry.get_class(module_name)
        if plugin_class is None or getattr(plugin_class, "name", None) != PLUGIN_NAME:
            raise _GateFailure("plugin_identity_mismatch")
        tools = {
            item.tool_name: item
            for item in plugin_registry.get_handlers_by_module_path(module_name)
            if item.kind is MetadataKind.TOOL and item.tool_name is not None
        }
        missing = set(_REVISION_TOOL_CONTRACTS) - tools.keys()
        if missing:
            raise _GateFailure("required_tool_missing")
        for name, (
            expected_risk,
            expected_properties,
            expected_required,
        ) in _REVISION_TOOL_CONTRACTS.items():
            metadata = tools[name]
            schema = metadata.tool_schema
            if metadata.tool_risk != expected_risk or not isinstance(schema, dict):
                raise _GateFailure("tool_contract_invalid")
            properties = schema.get("properties")
            required = schema.get("required")
            if (
                schema.get("type") != "object"
                or not isinstance(properties, dict)
                or set(properties) != set(expected_properties)
                or not isinstance(required, list)
                or set(required) != expected_required
            ):
                raise _GateFailure("tool_contract_invalid")
            for property_name, expected_type in expected_properties.items():
                property_schema = properties.get(property_name)
                if not isinstance(property_schema, dict):
                    raise _GateFailure("tool_contract_invalid")
                if expected_type.startswith("array<"):
                    item_type = expected_type.removeprefix("array<").removesuffix(">")
                    items = property_schema.get("items")
                    if (
                        property_schema.get("type") != "array"
                        or not isinstance(items, dict)
                        or items.get("type") != item_type
                    ):
                        raise _GateFailure("tool_contract_invalid")
                elif property_schema.get("type") != expected_type:
                    raise _GateFailure("tool_contract_invalid")
        return sorted(_REVISION_TOOL_CONTRACTS)
    except _GateFailure:
        raise
    except Exception as error:
        raise _GateFailure("plugin_load_failed") from error
    finally:
        importer.unregister(module_name)
        plugin_registry.remove_module_tree(module_name)
        for imported_name in tuple(sys.modules):
            if imported_name == module_name or imported_name.startswith(
                f"{module_name}."
            ):
                _ = sys.modules.pop(imported_name, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a run-local Akashic bridge release"
    )
    _ = parser.add_argument("--manifest", type=Path, required=True)
    _ = parser.add_argument("--runtime", type=Path, required=True)
    _ = parser.add_argument("--json", action="store_true", required=True)
    args = parser.parse_args(argv)
    result = check_plugin_release(args.manifest, args.runtime)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
