from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from sidera_control_plane.application.handoff_commands import HandoffCommandError
from sidera_control_plane.domain.context_packages import (
    ContextPackageManifestV2,
    ContextPurpose,
)
from sidera_control_plane.domain.handoffs import HandoffTransitionStatus
from sidera_control_plane.persistence.database import Database
from tests.integration.result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_handoff_reuses_fixed_selection_in_a_new_consumer_package(scenario) -> None:
    scenario.run_to_started_summary()
    source_execution_id = scenario.latest_execution_id("summary")
    source_package = scenario.assemble_context("summary")
    selection = scenario.selection("summary")

    transition = scenario.handoff_worker(
        "summary",
        target_context_versions=(1, 2),
        workspace_mode="managed",
    )

    assert transition.status is HandoffTransitionStatus.CONVERGED
    assert transition.target_execution_id is not None
    assert transition.target_execution_id != source_execution_id
    target_package = scenario.context_package_for_execution(
        transition.target_execution_id
    )
    assert isinstance(target_package, ContextPackageManifestV2)
    assert target_package.package_id != source_package.package_id
    assert target_package.result_input_manifest.input_selection_id == selection.id
    assert (
        target_package.result_input_manifest.input_selection_hash
        == selection.selection_hash
    )
    assert scenario.start_call_count("summary") == 2

    with scenario.uow_factory() as uow:
        assembly = uow.context_assemblies.get_for_consumer(
            ContextPurpose.EXECUTION,
            transition.target_execution_id,
            scenario.workspace_id,
            manifest_schema_version=2,
        )
        assert assembly is not None
        inheritance = assembly.budget["execution_context_inheritance"]
        assert inheritance["kind"] == "handoff"
        assert inheritance["source_execution_id"] == str(source_execution_id)
        assert inheritance["source_session_id"] == str(transition.source_session_id)


def test_handoff_v1_only_target_is_rejected_without_starting_it(scenario) -> None:
    scenario.run_to_started_summary()
    before = scenario.start_call_count("summary")

    with pytest.raises(HandoffCommandError) as error:
        scenario.handoff_worker(
            "summary",
            target_context_versions=(1,),
            workspace_mode="unmanaged",
        )

    assert error.value.code in {
        "result_input_adapter_unsupported",
        "handoff_target_ineligible",
    }
    assert scenario.start_call_count("summary") == before


def test_handoff_rejects_a_revoked_source_package_without_starting_target(
    scenario,
) -> None:
    scenario.run_to_started_summary()
    source_package = scenario.assemble_context("summary")
    before = scenario.start_call_count("summary")
    scenario.revoke_package(source_package.package_id)

    with pytest.raises(HandoffCommandError) as error:
        scenario.handoff_worker(
            "summary",
            target_context_versions=(1, 2),
            workspace_mode="managed",
        )

    assert error.value.code in {
        "handoff_workspace_incompatible",
        "handoff_context_package_changed",
    }
    assert scenario.start_call_count("summary") == before
