from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.application.artifact_content import ArtifactContentService
from sidera_control_plane.application.context_materialization import (
    ContextMaterializationError,
    ContextMaterializer,
)
from sidera_control_plane.domain.context_packages import (
    ContextMaterializationMode,
    ContextMaterializationV2,
)
from sidera_control_plane.persistence.models import ArtifactModel

from .result_input_scenario import ResultInputScenario, _EmptyCatalog

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def _prepare_materialization(
    scenario: ResultInputScenario,
) -> tuple[ContextMaterializationV2, ResultInputScenario]:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()
    scenario.activate()
    package = scenario.assemble_context("summary")
    with scenario.uow_factory() as uow:
        revision = uow.work_contexts.get_revision(
            package.work_context_revision_id,
            scenario.workspace_id,
        )
    assert revision is not None
    materializer = ContextMaterializer(
        _EmptyCatalog(),
        result_input_content=ArtifactContentService(
            scenario.database,
            scenario.artifact_store,
        ),
    )
    value = materializer.materialize(
        package,
        revision,
        mode=ContextMaterializationMode.OWNER_AUDIT,
        workspace_id=scenario.workspace_id,
        expected_package_hash=package.package_hash,
        owner_capability=True,
        now=scenario.now,
        correlation_id=uuid4(),
        caller_request_id=uuid4(),
    )
    assert isinstance(value, ContextMaterializationV2)
    return value, scenario


def test_materializer_reads_selected_artifacts_from_workspace_store(
    scenario: ResultInputScenario,
) -> None:
    value, _ = _prepare_materialization(scenario)

    assert {item.body_text for item in value.result_inputs} == {
        "report-b\n",
        "report-c\n",
    }
    assert {item.size_bytes for item in value.result_inputs} == {9}
    assert all(
        item.body_text.encode("utf-8")
        and item.content_hash
        for item in value.result_inputs
    )
    assert "body_text" in value.to_mapping()["result_inputs"][0]  # type: ignore[index]


def test_storage_reference_is_never_taken_from_the_selection_manifest(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()
    scenario.activate()
    package = scenario.assemble_context("summary")
    artifact_id = package.result_input_manifest.artifacts[0].artifact_id
    with scenario.database.session() as session:
        session.execute(
            update(ArtifactModel)
            .where(
                ArtifactModel.id == artifact_id,
                ArtifactModel.workspace_id == scenario.workspace_id,
            )
            .values(storage_ref="https://attacker.invalid/result.txt")
        )
        session.commit()

    with scenario.uow_factory() as uow:
        revision = uow.work_contexts.get_revision(
            package.work_context_revision_id,
            scenario.workspace_id,
        )
    assert revision is not None
    materializer = ContextMaterializer(
        _EmptyCatalog(),
        result_input_content=ArtifactContentService(
            scenario.database,
            scenario.artifact_store,
        ),
    )
    with pytest.raises(ContextMaterializationError) as error_info:
        materializer.materialize(
            package,
            revision,
            mode=ContextMaterializationMode.OWNER_AUDIT,
            workspace_id=scenario.workspace_id,
            expected_package_hash=package.package_hash,
            owner_capability=True,
            now=scenario.now,
            correlation_id=uuid4(),
            caller_request_id=uuid4(),
        )

    assert error_info.value.code == "result_input_unavailable"
    assert error_info.value.temporary is True
