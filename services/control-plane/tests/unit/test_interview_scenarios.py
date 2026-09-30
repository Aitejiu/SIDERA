from pathlib import Path
from typing import Any

import pytest

from sidera_control_plane.demo import scenarios
from tests.unit.interview_demo_support import manifest_for


def test_mainline_preparation_has_no_business_commands() -> None:
    assert scenarios.preparation_steps("mainline") == ()


def test_extension_preparation_uses_a_fixed_reviewed_vocabulary() -> None:
    assert scenarios.preparation_steps("knowledge") == (
        "publish_knowledge_candidate",
        "await_candidate_review",
    )
    assert scenarios.preparation_steps("recovery") == (
        "arm_after_input_persist_fault",
        "await_intervention",
    )


def test_every_scenario_has_a_reviewed_preparation_vocabulary() -> None:
    assert scenarios.preparation_steps("revision") == (
        "complete_mainline_checkpoint",
        "await_revision_request",
    )
    assert scenarios.preparation_steps("handoff") == (
        "record_worker_input_checkpoint",
        "await_handoff_request",
    )
    assert scenarios.preparation_steps("rework") == (
        "publish_failed_candidate_checkpoint",
        "await_rework_request",
    )
    assert scenarios.preparation_steps("governance") == (
        "create_release_governance_hold",
        "await_governance_decision",
    )
    assert scenarios.preparation_steps("controls") == (
        "prepare_control_facts",
        "await_control_action",
    )
    assert scenarios.preparation_steps("history") == (
        "complete_history_run",
        "open_historical_result",
    )


def test_checkpoint_is_a_read_only_description_of_the_run(monkeypatch, tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path).model_copy(
        update={"run_root": tmp_path / "run"}
    )
    manifest.run_root.mkdir()
    monkeypatch.setattr(scenarios, "validate_manifest", lambda *_args: None)
    monkeypatch.setattr(scenarios, "read_run_phase", lambda _manifest: "prepared")

    checkpoint = scenarios.prepare_checkpoint(manifest)

    assert checkpoint["scenario"] == "mainline"
    assert checkpoint["pending_action"] == "在 Web Chat 提交需求"
    assert checkpoint["preparation_steps"] == []
    assert checkpoint["gates"] == {
        "parallel": False,
        "before-artifact": False,
        "handoff": False,
        "governance": False,
    }


def test_present_preparation_audit_is_idempotent_and_has_no_business_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = manifest_for(tmp_path).model_copy(
        update={"run_root": tmp_path / "run"}
    )
    manifest.run_root.mkdir()
    monkeypatch.setattr(scenarios, "validate_manifest", lambda *_args: None)

    first = scenarios.record_preparation_checkpoint(manifest)
    second = scenarios.record_preparation_checkpoint(manifest)

    assert second == first
    assert first["actor"] == "demo-preparation"
    assert first["scenario"] == "mainline"
    assert first["status"] == "checkpointed"
    assert first["business_commands"] == []
    assert (manifest.run_root / "reports" / "preparation-audit.json").stat().st_mode & 0o777 == 0o600


def test_public_preparation_is_not_allowed_to_write_business_rows_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = manifest_for(tmp_path).model_copy(
        update={"run_root": tmp_path / "run", "scenario": "revision"}
    )
    manifest.run_root.mkdir()
    monkeypatch.setattr(scenarios, "validate_manifest", lambda *_args: None)

    calls: list[dict[str, Any]] = []

    class FakePreparation:
        def __init__(self, prepared_manifest: object) -> None:
            assert prepared_manifest is manifest

        def prepare(self) -> dict[str, object]:
            calls.append({"kind": "public-api"})
            return {
                "schema_version": 1,
                "run_id": str(manifest.run_id),
                "scenario": "revision",
                "actor": "demo-preparation",
                "status": "completed",
                "preparation_steps": list(scenarios.preparation_steps("revision")),
                "business_commands": ["command-1"],
                "stages": [
                    {
                        "stage": "complete_mainline_checkpoint",
                        "actor": "demo-preparation",
                        "command_id": "command-1",
                        "status": "applied",
                    }
                ],
            }

    monkeypatch.setattr(scenarios, "_ScenarioPreparation", FakePreparation)

    result = scenarios.prepare_scenario(manifest)

    assert calls == [{"kind": "public-api"}]
    assert result["status"] == "completed"
    assert result["business_commands"] == ["command-1"]
    assert result["stages"]
