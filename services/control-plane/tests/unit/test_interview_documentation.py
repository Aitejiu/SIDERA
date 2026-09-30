from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from sidera_control_plane.demo.cli import build_parser

PROJECT_ROOT = Path(__file__).resolve().parents[4]
PUBLIC_DOCS = (
    PROJECT_ROOT / "docs" / "deployment" / "initial-demo.md",
    PROJECT_ROOT / "docs" / "guide" / "workspace.md",
)


def test_cli_commands_and_documented_arguments_are_present() -> None:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if action.__class__.__name__ == "_SubParsersAction"
    )
    commands = set(subparsers.choices)
    assert {
        "prepare",
        "start",
        "status",
        "doctor",
        "present",
        "open",
        "continue",
        "stop",
        "reset",
        "report",
    } <= commands

    text = PUBLIC_DOCS[0].read_text(encoding="utf-8")
    for example in (
        "prepare --scenario mainline --print-manifest-path",
        "start --manifest",
        "present --manifest",
        "status --manifest",
        "doctor --manifest",
        "open --manifest",
        "continue --manifest",
        "stop --manifest",
        "reset --manifest",
    ):
        assert example in text

    for command in sorted(commands):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "sidera_control_plane.demo.cli",
                command,
                "--help",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, command
        assert f"{command}" in completed.stdout


def test_public_bash_blocks_parse_without_executing() -> None:
    for path in PUBLIC_DOCS:
        text = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
        for block in blocks:
            completed = subprocess.run(
                ["bash", "-n"],
                input=block,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            assert completed.returncode == 0, f"{path}: {completed.stderr}"


def test_public_maintenance_describes_explicit_inventory_scope() -> None:
    text = PUBLIC_DOCS[0].read_text(encoding="utf-8")
    assert "scripts/local-maintenance.py" in text
    assert "不会从 Interview Demo" in text
    assert "inspect-backup" in text
    assert "restore-preflight" in text
    assert "restore_read_only=true" in text
    assert "provider key、模型 token、auth 数据库、系统 Keychain" in text
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
    assert any("scripts/local-maintenance.py backup" in block for block in blocks)


def test_public_relative_links_exist_and_private_material_stays_private() -> None:
    for path in PUBLIC_DOCS:
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            if target.startswith(("http://", "https://", "#")):
                continue
            relative = target.split("#", 1)[0]
            assert relative
            assert (path.parent / relative).resolve().exists(), (path, target)

        assert "docs/superpowers/plans/" not in text
        assert ".sidera/interview-demo/" not in text
        assert "DEEPSEEK_API_KEY" not in text
