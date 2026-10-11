from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _gate_module() -> ModuleType:
    path = ROOT / "docker" / "debug" / "gate.py"
    spec = importlib.util.spec_from_file_location("gate_build_timeout", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_image_build_timeout_defaults_to_600(monkeypatch: Any) -> None:
    gate = _gate_module()
    monkeypatch.delenv(
        "AKASHIC_CHANGE_GATE_IMAGE_BUILD_TIMEOUT_SECONDS", raising=False
    )

    assert gate._image_build_timeout_seconds() == 600


@pytest.mark.parametrize("value", ["1", "3600"])
def test_image_build_timeout_accepts_bounded_override(
    monkeypatch: Any, value: str
) -> None:
    gate = _gate_module()
    monkeypatch.setenv("AKASHIC_CHANGE_GATE_IMAGE_BUILD_TIMEOUT_SECONDS", value)

    assert gate._image_build_timeout_seconds() == int(value)


@pytest.mark.parametrize("value", ["", "0", "-1", "3601", "not-a-number"])
def test_image_build_timeout_rejects_invalid_override(
    monkeypatch: Any, value: str
) -> None:
    gate = _gate_module()
    monkeypatch.setenv("AKASHIC_CHANGE_GATE_IMAGE_BUILD_TIMEOUT_SECONDS", value)

    with pytest.raises(
        gate.GateError,
        match="AKASHIC_CHANGE_GATE_IMAGE_BUILD_TIMEOUT_SECONDS",
    ):
        gate._image_build_timeout_seconds()


def test_image_build_uses_and_reports_override(
    tmp_path: Path, monkeypatch: Any
) -> None:
    gate = _gate_module()
    monkeypatch.setenv("AKASHIC_CHANGE_GATE_IMAGE_BUILD_TIMEOUT_SECONDS", "3600")
    monkeypatch.setenv("AKASHIC_CHANGE_GATE_TMPDIR", str(tmp_path))
    commands: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> Any:
        commands.append((command, kwargs))
        return gate.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(gate.subprocess, "run", run)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    build = gate._build_change_gate_image("test-timeout", report_dir)
    image_report = json.loads(
        (report_dir / "image-build.json").read_text(encoding="utf-8")
    )

    assert build["status"] == "passed"
    assert build["timeoutSeconds"] == 3600
    assert commands[0][1]["timeout"] == 3600
    assert image_report["timeoutSeconds"] == 3600


def test_image_build_timeout_failure_reports_effective_override(
    tmp_path: Path, monkeypatch: Any
) -> None:
    gate = _gate_module()
    monkeypatch.setenv("AKASHIC_CHANGE_GATE_IMAGE_BUILD_TIMEOUT_SECONDS", "3600")
    monkeypatch.setenv("AKASHIC_CHANGE_GATE_TMPDIR", str(tmp_path))

    def timeout(command: list[str], **kwargs: Any) -> Any:
        raise gate.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(gate.subprocess, "run", timeout)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    build = gate._build_change_gate_image("test-timeout", report_dir)
    image_report = json.loads(
        (report_dir / "image-build.json").read_text(encoding="utf-8")
    )

    assert build["status"] == "failed"
    assert build["timeoutSeconds"] == 3600
    assert image_report["timeoutSeconds"] == 3600
    assert "3600s" in image_report["stderr"]
