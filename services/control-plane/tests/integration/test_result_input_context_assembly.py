from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from sidera_control_plane.application.context_requests import (
    validate_execution_context_selection,
)
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.context_packages import (
    ContextPackageManifestV1,
    ContextPackageManifestV2,
    ContextPurpose,
)

from .result_input_scenario import ResultInputScenario, _EmptyCatalog

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_context_assembly_uses_release_selection_after_late_result(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()
    selection = scenario.selection("summary")

    scenario.complete_source("a", body=b"late-a\n")
    scenario.activate()
    manifest = scenario.assemble_context("summary")

    assert isinstance(manifest, ContextPackageManifestV2)
    assert manifest.result_input_manifest.input_selection_hash == selection.selection_hash
    assert manifest.result_input_manifest.input_selection_id == selection.id
    assert manifest.result_input_manifest.artifacts
    assert all(
        source.worker_item_key in {"b", "c"}
        for artifact in manifest.result_input_manifest.artifacts
        for source in artifact.sources
    )
    assert scenario.package_ids("summary") == (manifest.package_id,)


def test_context_package_ids_and_assembly_are_protocol_and_selection_bound(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()
    scenario.activate()

    manifest = scenario.assemble_context("summary")
    repeated = scenario.assemble_context("summary")

    assert repeated.package_id == manifest.package_id
    assert repeated.package_hash == manifest.package_hash
    summary_execution_id = scenario._execution_id("summary")
    source_execution_id = scenario._execution_id("a")
    assert summary_execution_id is not None
    assert source_execution_id is not None
    with scenario.uow_factory() as uow:
        assembly = uow.context_assemblies.get_for_consumer(
            ContextPurpose.EXECUTION,
            summary_execution_id,
            scenario.workspace_id,
            manifest_schema_version=2,
        )
        source_assembly = uow.context_assemblies.get_for_consumer(
            ContextPurpose.EXECUTION,
            source_execution_id,
            scenario.workspace_id,
        )
        source_package = uow.context_packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            source_execution_id,
            scenario.workspace_id,
        )
    assert assembly is not None
    assert assembly.manifest_schema_version == 2
    assert assembly.input_selection_id == manifest.result_input_manifest.input_selection_id
    assert assembly.input_selection_hash is not None
    assert ":v2:" in assembly.idempotency_key
    assert assembly.input_selection_hash in assembly.idempotency_key
    assert source_assembly is not None
    assert source_assembly.manifest_schema_version == 1
    assert isinstance(source_package, ContextPackageManifestV1)
    assert not isinstance(source_package, ContextPackageManifestV2)
    assert source_package.package_id == uuid5(
        NAMESPACE_URL,
        f"sidera:context-package:{source_assembly.request_id}:v1",
    )

    restarted = ContextPackageWorker(
        uow_factory=scenario.uow_factory,
        catalog=_EmptyCatalog(),
        owner_id="result-input-context-restarted",
        lease_seconds=60,
        clock=scenario.clock,
    ).run_once(limit=20)
    assert restarted.claimed == 0


def test_context_assembly_rejects_selection_from_another_workspace(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()
    scenario.activate()

    selection = scenario.selection("summary")
    with scenario.uow_factory() as uow:
        execution_request = uow.execution_requests.get(
            scenario.request_ids["summary"], scenario.workspace_id
        )
        release = uow.collaboration_ledger.get_release_for_request(
            scenario.workspace_id,
            scenario.request_ids["summary"],
        )
    assert execution_request is not None
    assert release is not None

    forged_selection = replace(selection, workspace_id=uuid4())
    with pytest.raises(ValueError, match="selection workspace"):
        validate_execution_context_selection(
            execution_request=execution_request,
            release=release,
            selection=forged_selection,
        )
