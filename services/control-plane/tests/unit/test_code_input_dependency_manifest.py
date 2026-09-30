from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from sidera_control_plane.application.code_result_capture import (
    CodeResultCaptureService,
)
from sidera_control_plane.application.code_result_delivery import resolve_code_inputs
from sidera_control_plane.application.result_input_evaluation import (
    build_code_input_manifest,
)
from sidera_control_plane.application.result_input_sources import (
    load_code_result_dependency_closure,
    order_code_result_dependency_closure,
)
from sidera_control_plane.domain.context_packages import ContextPackageManifestV3

from .result_integration_fixtures import context_v3_mapping, rework_impact_case


def test_manifest_includes_consumed_result_dependencies_in_stable_order() -> None:
    case = rework_impact_case()
    request_id = UUID("50000000-0000-0000-0000-000000000201")
    selection = SimpleNamespace(
        id=UUID("b0000000-0000-0000-0000-000000000201"),
        selection_hash="5" * 64,
        workspace_id=case.b.workspace_id,
        root_task_id=case.b.root_task_id,
        target_execution_request_id=request_id,
        groups=(
            SimpleNamespace(
                sources=(
                    SimpleNamespace(
                        effective_execution_id=case.b.execution_id,
                        effective_request_id=case.b.request_id,
                        worker_item_id=case.b.worker_item_id,
                    ),
                )
            ),
        ),
        hard_inputs=(),
    )

    first = build_code_input_manifest(
        selection=selection,
        snapshots=(case.b, case.a1),
        request_id=request_id,
        execution_id=UUID("60000000-0000-0000-0000-000000000201"),
        repository_ref=case.b.repository_ref,
        base_commit=case.a1.input_commit,
    )
    replay = build_code_input_manifest(
        selection=selection,
        snapshots=(case.a1, case.b),
        request_id=request_id,
        execution_id=UUID("60000000-0000-0000-0000-000000000201"),
        repository_ref=case.b.repository_ref,
        base_commit=case.a1.input_commit,
    )

    assert tuple(item.result_id for item in first.sources) == (case.a1.id, case.b.id)
    assert replay.sources == first.sources
    assert replay.input_hash == first.input_hash


def test_resolver_allows_only_dependencies_of_selected_snapshots() -> None:
    case = rework_impact_case()
    request_id = UUID("50000000-0000-0000-0000-000000000202")
    execution_id = UUID("60000000-0000-0000-0000-000000000202")
    selection = SimpleNamespace(
        id=UUID("b0000000-0000-0000-0000-000000000202"),
        selection_hash="6" * 64,
        workspace_id=case.b.workspace_id,
        root_task_id=case.b.root_task_id,
        target_execution_request_id=request_id,
        groups=(
            SimpleNamespace(
                sources=(
                    SimpleNamespace(
                        effective_execution_id=case.b.execution_id,
                        effective_request_id=case.b.request_id,
                        worker_item_id=case.b.worker_item_id,
                    ),
                )
            ),
        ),
        hard_inputs=(),
    )
    manifest = build_code_input_manifest(
        selection=selection,
        snapshots=(case.b, case.a1),
        request_id=request_id,
        execution_id=execution_id,
        repository_ref=case.b.repository_ref,
        base_commit=case.a1.input_commit,
    )
    request = SimpleNamespace(
        worker_item_id=case.b.worker_item_id,
        subtask_id=case.b.root_task_id,
        result_derivation_kind=None,
        purpose="execution",
    )
    execution = SimpleNamespace(
        worker_item_id=case.b.worker_item_id,
        task_id=case.b.root_task_id,
    )
    snapshots_by_id = {case.a1.id: case.a1, case.b.id: case.b}
    uow = SimpleNamespace(
        input_selections=SimpleNamespace(
            get=lambda workspace_id, selection_id: selection
        ),
        execution_requests=SimpleNamespace(
            get=lambda selected_request_id, workspace_id: request
        ),
        executions=SimpleNamespace(
            get=lambda selected_execution_id, workspace_id: execution
        ),
        results=SimpleNamespace(
            get_record=lambda kind, result_id, workspace_id: snapshots_by_id.get(
                result_id
            )
        ),
    )

    assert resolve_code_inputs(uow, manifest) == manifest


def test_dependency_closure_rejects_missing_and_unrelated_snapshots() -> None:
    case = rework_impact_case()

    with pytest.raises(ValueError, match="dependency snapshot is missing"):
        order_code_result_dependency_closure(
            direct_snapshots=(case.b,),
            snapshots=(case.b,),
            workspace_id=case.b.workspace_id,
            root_task_id=case.b.root_task_id,
            repository_ref=case.b.repository_ref,
        )

    with pytest.raises(ValueError, match="unrelated snapshot"):
        order_code_result_dependency_closure(
            direct_snapshots=(case.b,),
            snapshots=(case.a1, case.b, case.c),
            workspace_id=case.b.workspace_id,
            root_task_id=case.b.root_task_id,
            repository_ref=case.b.repository_ref,
        )


def test_dependency_loader_fetches_only_the_reachable_source_closure() -> None:
    case = rework_impact_case()
    records = {item.id: item for item in (case.a1, case.b, case.c)}

    ordered = load_code_result_dependency_closure(
        direct_snapshots=(case.b,),
        load_snapshot=records.get,
        workspace_id=case.b.workspace_id,
        root_task_id=case.b.root_task_id,
        repository_ref=case.b.repository_ref,
    )

    assert tuple(item.id for item in ordered) == (case.a1.id, case.b.id)


def test_capture_provenance_comes_from_the_persisted_code_context_package() -> None:
    case = rework_impact_case()
    request_id = UUID("50000000-0000-0000-0000-000000000203")
    execution_id = UUID("60000000-0000-0000-0000-000000000203")
    selection = SimpleNamespace(
        id=UUID("b0000000-0000-0000-0000-000000000203"),
        selection_hash="7" * 64,
        workspace_id=case.b.workspace_id,
        root_task_id=case.b.root_task_id,
        target_execution_request_id=request_id,
        groups=(
            SimpleNamespace(
                sources=(
                    SimpleNamespace(
                        effective_execution_id=case.b.execution_id,
                        effective_request_id=case.b.request_id,
                        worker_item_id=case.b.worker_item_id,
                    ),
                )
            ),
        ),
        hard_inputs=(),
    )
    code_manifest = build_code_input_manifest(
        selection=selection,
        snapshots=(case.b, case.a1),
        request_id=request_id,
        execution_id=execution_id,
        repository_ref=case.b.repository_ref,
        base_commit=case.a1.input_commit,
    )
    package_mapping = context_v3_mapping(
        consumer_id=str(execution_id),
        worker_item_id=str(case.b.worker_item_id),
        control_plane_fact_snapshot={
            "execution_request_id": str(request_id),
            "execution_id": str(execution_id),
        },
        code_input_manifest=code_manifest.to_mapping(),
    )
    package = ContextPackageManifestV3.from_mapping(package_mapping)
    uow = SimpleNamespace(
        context_packages=SimpleNamespace(
            get_for_consumer=lambda *args, **kwargs: package
        )
    )

    consumed = CodeResultCaptureService._consumed_result_ids_for_execution(
        uow,
        workspace_id=case.b.workspace_id,
        root_task_id=case.b.root_task_id,
        worker_item_id=case.b.worker_item_id,
        execution_id=execution_id,
        request_id=request_id,
        repository_ref=case.b.repository_ref,
    )

    assert consumed == (case.a1.id, case.b.id)
