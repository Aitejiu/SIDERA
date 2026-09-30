from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "akashic-agent"))

from agent.plugins.install import install_git_plugin
from agent.plugins.manifest import set_plugin_enabled

from scripts.r6_akashic_plugin_gate import check_plugin_release

RUN_ID = "10000000-0000-0000-0000-000000000001"
WORKSPACE_ID = "10000000-0000-0000-0000-000000000002"
PLUGIN_ID = "sidera-task-bridge@sidera"

PLUGIN_SOURCE = """
from agent.plugins import Plugin, tool
from agent.plugins.registry import MetadataKind, plugin_registry


class SideraTaskBridge(Plugin):
    name = "sidera-task-bridge"
    version = "0.1.0"

    @classmethod
    def mcp_servers(cls):
        return []

    @tool(name="sidera_create_plan_revision", risk="write")
    def create_plan_revision(
        self, event, root_task_id: str, goal: str,
        constraints: list[str], acceptance_criteria: list[str],
    ) -> str:
        return ""

    @tool(name="sidera_get_plan_revision", risk="read-only")
    def get_plan_revision(self, event, planning_request_id: str) -> str:
        return ""

    @tool(name="sidera_confirm_plan_revision", risk="write")
    def confirm_plan_revision(
        self, event, planning_request_id: str, request_version: int,
        proposal_revision: int, proposal_content_hash: str, diff_hash: str,
        base_snapshot_id: str, base_content_hash: str,
        explicit_user_confirmation: bool,
    ) -> str:
        return ""


for metadata in plugin_registry.get_handlers_by_module_path(__name__):
    if metadata.kind is not MetadataKind.TOOL:
        continue
    properties = metadata.tool_schema["properties"]
    if metadata.tool_name == "sidera_create_plan_revision":
        for name in ("constraints", "acceptance_criteria"):
            properties[name]["type"] = "array"
            properties[name]["items"] = {"type": "string"}
    if metadata.tool_name == "sidera_confirm_plan_revision":
        for name in ("request_version", "proposal_revision"):
            properties[name]["type"] = "number"
        properties["explicit_user_confirmation"]["type"] = "boolean"
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _source_repo(root: Path, source: str = PLUGIN_SOURCE) -> tuple[Path, str]:
    repo = root / "source"
    repo.mkdir()
    (repo / "plugin.py").write_text(source, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "SIDERA gate test")
    _git(repo, "config", "user.email", "gate-test@sidera.local")
    _git(repo, "add", "plugin.py")
    _git(repo, "commit", "-qm", "gate fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _run(root: Path, split_commit: str) -> tuple[Path, Path, dict[str, Any]]:
    runtime = root / "runtime"
    plugin_home = runtime / "akashic-plugin-home"
    manifest = root / "manifest.json"
    manifest_payload = {
        "run_id": RUN_ID,
        "workspace_id": WORKSPACE_ID,
        "run_root": str(root / "run-root"),
    }
    _write_json(manifest, manifest_payload)
    receipt = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "workspace_id": WORKSPACE_ID,
        "plugin_home": str(plugin_home.resolve()),
        "source_commit": split_commit,
        "split_commit": split_commit,
        "source_worktree": str(root / "source"),
        "marketplace": "sidera",
        "plugin_id": PLUGIN_ID,
    }
    _write_json(runtime / "akashic-plugin-release.json", receipt)
    return manifest, plugin_home, receipt


def _install(
    root: Path,
    *,
    source_text: str = PLUGIN_SOURCE,
    marketplace: str = "sidera",
    stage_candidate: bool = False,
) -> tuple[Path, str]:
    repo, commit = _source_repo(root, source_text)
    runtime = root / "runtime"
    plugin_home = runtime / "akashic-plugin-home"
    result = install_git_plugin(
        workspace=root / "workspace",
        source=str(repo),
        marketplace=marketplace,
        plugins_home=plugin_home,
        stage_candidate=stage_candidate,
    )
    plugin_home.chmod(0o700)
    return result.installed_path, commit


def test_matching_stable_run_local_plugin_passes(tmp_path: Path) -> None:
    artifact, commit = _install(tmp_path)
    manifest, _, receipt = _run(tmp_path, commit)

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {
        "ok": True,
        "run_id": RUN_ID,
        "plugin_id": PLUGIN_ID,
        "stable_source_revision": commit,
        "tool_names": [
            "sidera_confirm_plan_revision",
            "sidera_create_plan_revision",
            "sidera_get_plan_revision",
        ],
    }
    assert artifact.is_dir()
    assert receipt["plugin_home"] == str(
        (tmp_path / "runtime" / "akashic-plugin-home").resolve()
    )


def test_empty_private_home_does_not_fall_back_to_ambient_old_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _source_repo(tmp_path)
    ambient_home = tmp_path / "ambient-old-plugin-home"
    install_git_plugin(
        workspace=tmp_path / "ambient-workspace",
        source=str(source),
        marketplace="sidera",
        plugins_home=ambient_home,
    )
    (source / "plugin.py").write_text(
        (source / "plugin.py").read_text(encoding="utf-8") + "\n# newer release\n",
        encoding="utf-8",
    )
    _git(source, "add", "plugin.py")
    _git(source, "commit", "-qm", "new run-local release")
    manifest, _, _ = _run(tmp_path, _git(source, "rev-parse", "HEAD"))
    monkeypatch.setenv("AKASHIC_PLUGIN_HOME", str(ambient_home))

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": "plugin_not_enabled"}


@pytest.mark.parametrize(
    ("receipt_updates", "expected_code"),
    [
        ({"run_id": "10000000-0000-0000-0000-000000000099"}, "run_identity_mismatch"),
        ({"plugin_home": "/tmp/not-this-run"}, "plugin_home_mismatch"),
    ],
)
def test_receipt_must_match_run_and_private_home(
    tmp_path: Path,
    receipt_updates: dict[str, Any],
    expected_code: str,
) -> None:
    _, commit = _install(tmp_path)
    manifest, _, receipt = _run(tmp_path, commit)
    receipt.update(receipt_updates)
    _write_json(tmp_path / "runtime" / "akashic-plugin-release.json", receipt)

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": expected_code}


def test_stable_pointer_cannot_escape_through_symlink(
    tmp_path: Path,
) -> None:
    artifact, commit = _install(tmp_path)
    manifest, plugin_home, _ = _run(tmp_path, commit)
    plugin_base = artifact.parents[1]
    outside = tmp_path / "outside-artifact"
    outside.mkdir()
    (outside / "plugin.py").write_text("# outside\n", encoding="utf-8")
    escaped = plugin_base / ".artifacts" / "escaped"
    escaped.symlink_to(outside, target_is_directory=True)
    _write_json(
        plugin_base / ".pointers.json",
        {"stable": ".artifacts/escaped", "latest": ".artifacts/escaped"},
    )

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": "stable_pointer_invalid"}
    assert plugin_home.is_dir()


def test_json_cli_reports_only_the_gate_receipt_fields(tmp_path: Path) -> None:
    _, commit = _install(tmp_path)
    manifest, _, receipt = _run(tmp_path, commit)
    receipt["credential"] = "must-not-appear-in-output"
    _write_json(tmp_path / "runtime" / "akashic-plugin-release.json", receipt)

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "r6_akashic_plugin_gate.py"),
            "--manifest",
            str(manifest),
            "--runtime",
            str(tmp_path / "runtime"),
            "--json",
        ],
        capture_output=True,
        check=False,
        env=os.environ.copy(),
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "ok",
        "run_id",
        "plugin_id",
        "stable_source_revision",
        "tool_names",
    }
    assert "must-not-appear-in-output" not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("source_text", "marketplace", "receipt_updates", "expected_code"),
    [
        (
            PLUGIN_SOURCE.replace(
                'name = "sidera-task-bridge"', 'name = "wrong-bridge"'
            ),
            "sidera",
            {},
            "plugin_not_enabled",
        ),
        (
            PLUGIN_SOURCE,
            "other-marketplace",
            {
                "marketplace": "other-marketplace",
                "plugin_id": "sidera-task-bridge@other-marketplace",
            },
            "plugin_identity_mismatch",
        ),
        (PLUGIN_SOURCE, "sidera", {"split_commit": "0" * 40}, "revision_mismatch"),
    ],
)
def test_wrong_plugin_identity_marketplace_or_revision_is_rejected(
    tmp_path: Path,
    source_text: str,
    marketplace: str,
    receipt_updates: dict[str, Any],
    expected_code: str,
) -> None:
    _, commit = _install(
        tmp_path,
        source_text=source_text,
        marketplace=marketplace,
    )
    manifest, _, receipt = _run(tmp_path, commit)
    receipt.update(receipt_updates)
    _write_json(tmp_path / "runtime" / "akashic-plugin-release.json", receipt)

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": expected_code}


@pytest.mark.parametrize(
    "tool_name",
    [
        "sidera_create_plan_revision",
        "sidera_get_plan_revision",
        "sidera_confirm_plan_revision",
    ],
)
def test_each_missing_revision_tool_is_rejected(
    tmp_path: Path,
    tool_name: str,
) -> None:
    source_text = PLUGIN_SOURCE.replace(
        f'name="{tool_name}"', f'name="missing_{tool_name}"', 1
    )
    _, commit = _install(tmp_path, source_text=source_text)
    manifest, _, _ = _run(tmp_path, commit)

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": "required_tool_missing"}


def test_wrong_revision_tool_risk_is_rejected(tmp_path: Path) -> None:
    source_text = PLUGIN_SOURCE.replace(
        '@tool(name="sidera_create_plan_revision", risk="write")',
        '@tool(name="sidera_create_plan_revision", risk="read-only")',
        1,
    )
    _, commit = _install(tmp_path, source_text=source_text)
    manifest, _, _ = _run(tmp_path, commit)

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": "tool_contract_invalid"}


def test_wrong_revision_tool_schema_is_rejected(tmp_path: Path) -> None:
    source_text = PLUGIN_SOURCE.replace('{"type": "string"}', '{"type": "number"}', 1)
    _, commit = _install(tmp_path, source_text=source_text)
    manifest, _, _ = _run(tmp_path, commit)

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": "tool_contract_invalid"}


def test_disabled_plugin_manifest_entry_is_rejected(tmp_path: Path) -> None:
    _, commit = _install(tmp_path)
    manifest, plugin_home, _ = _run(tmp_path, commit)
    set_plugin_enabled(PLUGIN_ID, enabled=False, plugins_home=plugin_home)

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": "plugin_disabled"}


def test_private_plugin_home_must_remain_owner_only(tmp_path: Path) -> None:
    _, commit = _install(tmp_path)
    manifest, plugin_home, _ = _run(tmp_path, commit)
    plugin_home.chmod(0o755)

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": "plugin_home_permissions_invalid"}


def test_latest_candidate_without_stable_pointer_is_rejected(tmp_path: Path) -> None:
    _, commit = _install(tmp_path, stage_candidate=True)
    manifest, _, _ = _run(tmp_path, commit)

    result = check_plugin_release(manifest, tmp_path / "runtime")

    assert result == {"ok": False, "code": "stable_pointer_missing"}
