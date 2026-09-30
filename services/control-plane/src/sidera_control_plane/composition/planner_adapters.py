from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from pydantic import SecretStr
from sidera_runtime_guard import RuntimeBudgetGuard

from sidera_control_plane.adapters.demo_planner import DemoPlannerAdapter
from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
    FakeScenario,
)
from sidera_control_plane.adapters.opencode_planner import OpenCodePlannerAdapter
from sidera_control_plane.application.planning_runner import PlannerAdapterBuilder
from sidera_control_plane.config import Settings
from sidera_control_plane.demo.manifest import load_manifest, validate_manifest


def build_planner_adapter_builders(
    settings: Settings,
    *,
    acceptance_guard: RuntimeBudgetGuard | None = None,
) -> dict[str, PlannerAdapterBuilder]:
    def fake_builder(
        config: Mapping[str, object], _secrets: tuple[SecretStr, ...]
    ) -> FakePlannerAdapter:
        scenario = config.get("planning.scenario")
        if not isinstance(scenario, str):
            raise TypeError("fake planner requires planning.scenario")
        allowed = {
            "proposal",
            "revision",
            "revision_control_room",
            "clarification",
            "scale",
            "invalid_schema",
            "timeout",
            "unknown_outcome",
        }
        if scenario not in allowed:
            raise ValueError("fake planner scenario is unsupported")
        return FakePlannerAdapter(
            FakePlannerConfig(scenario=cast(FakeScenario, scenario))
        )

    def opencode_builder(
        config: Mapping[str, object], secrets: tuple[SecretStr, ...]
    ) -> OpenCodePlannerAdapter:
        configured_executable = config.get(
            "planning.executable", settings.planning_opencode_executable
        )
        if not isinstance(configured_executable, str) or not configured_executable.strip():
            raise TypeError("OpenCode planner executable is invalid")
        configured_model = config.get(
            "planning.model", settings.planning_opencode_model
        )
        if configured_model is not None and not isinstance(configured_model, str):
            raise TypeError("OpenCode planner model is invalid")
        return OpenCodePlannerAdapter(
            executable=configured_executable,
            model=configured_model,
            temp_root=settings.planning_temp_root,
            request_timeout_seconds=settings.planning_opencode_request_timeout_seconds,
            stop_grace_seconds=settings.planning_opencode_stop_grace_seconds,
            stderr_limit_bytes=settings.planning_acp_stderr_limit_bytes,
            secret_values=secrets,
            acceptance_guard=acceptance_guard,
        )

    builders: dict[str, PlannerAdapterBuilder] = {
        "fake": fake_builder,
        "opencode-acp-planner": opencode_builder,
    }
    if settings.interview_demo_manifest is not None:
        manifest = load_manifest(settings.interview_demo_manifest)
        validate_manifest(manifest, Path(__file__).resolve().parents[5])

        def demo_builder(
            _config: Mapping[str, object], secrets: tuple[SecretStr, ...]
        ) -> DemoPlannerAdapter:
            if secrets:
                raise ValueError("interview demo planner does not accept secrets")
            return DemoPlannerAdapter(manifest)

        builders = {"interview-demo-planner": demo_builder}

    return builders
