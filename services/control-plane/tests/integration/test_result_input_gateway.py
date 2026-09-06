from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from sidera_control_plane.application.worker_gateway import GatewayError

from .result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_gateway_delivers_selected_body_before_starting_worker(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"unique-body-b\n")
    scenario.complete_source("c", body=b"unique-body-c\n")
    scenario.reconcile()
    scenario.activate()
    scenario.assemble_context("summary")
    scenario.complete_source("a", body=b"unique-body-a\n")

    scenario.start_worker("summary")

    value = scenario.captured_context("summary")
    assert value.schema_version == 2
    assert {item.body_text for item in value.result_inputs} == {
        "unique-body-b\n",
        "unique-body-c\n",
    }
    assert scenario.start_call_count("summary") == 1


def test_gateway_does_not_start_when_selected_body_is_unavailable(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"unique-body-b\n")
    scenario.complete_source("c", body=b"unique-body-c\n")
    scenario.reconcile()
    scenario.activate()
    scenario.assemble_context("summary")
    scenario.complete_source("a", body=b"unique-body-a\n")
    scenario.remove_source_body("b")

    result = scenario.start_worker("summary")

    assert isinstance(result, GatewayError)
    assert getattr(result, "code", None) == "result_input_unavailable"
    assert scenario.start_call_count("summary") == 0
