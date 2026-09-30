from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from pydantic import SecretStr


def test_planner_builder_map_keeps_fake_and_opencode_keys(tmp_path: Path) -> None:
    from sidera_control_plane.adapters.fake_planner import FakePlannerAdapter
    from sidera_control_plane.composition import planner_adapters

    builders = planner_adapters.build_planner_adapter_builders(
        SimpleNamespace(
            interview_demo_manifest=None,
            planning_opencode_executable="opencode",
            planning_opencode_model=None,
            planning_temp_root=tmp_path,
            planning_opencode_request_timeout_seconds=60.0,
            planning_opencode_stop_grace_seconds=5.0,
            planning_acp_stderr_limit_bytes=65_536,
        )
    )

    assert tuple(builders) == ("fake", "opencode-acp-planner")
    assert isinstance(
        builders["fake"]({"planning.scenario": "proposal"}, ()),
        FakePlannerAdapter,
    )


def test_opencode_planner_builder_forwards_selected_settings_and_secrets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from sidera_control_plane.composition import planner_adapters

    settings = SimpleNamespace(
        interview_demo_manifest=None,
        planning_opencode_executable="default-opencode",
        planning_opencode_model="default/model",
        planning_temp_root=tmp_path / "planner-tmp",
        planning_opencode_request_timeout_seconds=17.5,
        planning_opencode_stop_grace_seconds=0.75,
        planning_acp_stderr_limit_bytes=4_096,
    )
    secret = SecretStr("cfg1-planner-fake")
    secrets = (secret,)
    guard = object()
    captured: dict[str, object] = {}
    adapter = object()

    def capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return adapter

    monkeypatch.setattr(planner_adapters, "OpenCodePlannerAdapter", capture)
    builders = planner_adapters.build_planner_adapter_builders(
        settings,
        acceptance_guard=cast(Any, guard),
    )
    selected_config = {
        "planning.executable": "selected-opencode",
        "planning.model": "opencode-go/deepseek-v4.1-flash",
    }

    assert builders["opencode-acp-planner"](selected_config, secrets) is adapter
    assert captured == {
        "executable": "selected-opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
        "temp_root": settings.planning_temp_root,
        "request_timeout_seconds": 17.5,
        "stop_grace_seconds": 0.75,
        "stderr_limit_bytes": 4_096,
        "secret_values": secrets,
        "acceptance_guard": guard,
    }
    assert captured["secret_values"] is secrets
    assert captured["acceptance_guard"] is guard


def test_planner_demo_mode_replaces_real_builders_and_uses_repository_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from sidera_control_plane.composition import planner_adapters

    manifest = SimpleNamespace(scenario="planner")
    roots: list[Path] = []
    monkeypatch.setattr(planner_adapters, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        planner_adapters,
        "validate_manifest",
        lambda _manifest, root: roots.append(root),
    )

    builders = planner_adapters.build_planner_adapter_builders(
        SimpleNamespace(
            interview_demo_manifest=tmp_path / "manifest.json",
            planning_temp_root=tmp_path / "planner-tmp",
        )
    )

    repository_root = Path(__file__).resolve().parents[4]
    assert (repository_root / "services/control-plane/pyproject.toml").is_file()
    assert tuple(builders) == ("interview-demo-planner",)
    assert roots == [repository_root]
