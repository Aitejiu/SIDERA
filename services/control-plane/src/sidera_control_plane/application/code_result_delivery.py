from __future__ import annotations

from uuid import UUID

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_rework import (
    require_authorized_source_candidate,
)
from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.execution_requests import ExecutionRequest
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.result_operations import (
    ResultError,
    ReworkRecordV1,
    ReworkStatus,
)


def resolve_code_inputs(
    uow: ControlPlaneUnitOfWork,
    manifest: CodeInputManifestV1,
) -> CodeInputManifestV1:
    """Reconcile an input manifest with the immutable selection and snapshots."""

    if not isinstance(manifest, CodeInputManifestV1):
        raise ResultError("code_input_manifest_invalid", {})
    execution_request = uow.execution_requests.get(
        manifest.request_id,
        manifest.workspace_id,
    )
    execution = uow.executions.get(manifest.execution_id, manifest.workspace_id)
    if (
        execution_request is None
        or execution is None
        or execution_request.worker_item_id != execution.worker_item_id
        or execution_request.subtask_id != execution.task_id
        or execution.worker_item_id is None
    ):
        raise ResultError(
            "code_input_consumer_mismatch",
            {"execution_id": str(manifest.execution_id)},
        )
    if (
        execution_request.result_derivation_kind == "rework"
        and execution_request.purpose != "reintegrate"
    ):
        return _resolve_rework_code_inputs(uow, manifest, execution_request)

    selection = uow.input_selections.get(manifest.workspace_id, manifest.selection_id)
    if selection is None:
        raise ResultError(
            "code_input_selection_not_found",
            {"selection_id": str(manifest.selection_id)},
        )
    if (
        selection.workspace_id != manifest.workspace_id
        or selection.root_task_id != manifest.root_task_id
        or selection.target_execution_request_id != manifest.request_id
        or selection.selection_hash != manifest.selection_hash
    ):
        raise ResultError(
            "code_input_selection_scope_mismatch",
            {"selection_id": str(manifest.selection_id)},
        )

    selected_sources = _selected_sources(selection)
    selected_execution_ids = {
        source.effective_execution_id for source in selected_sources
    }
    selected_by_execution = {
        source.effective_execution_id: source for source in selected_sources
    }
    manifest_execution_ids: set[UUID] = set()
    for source in manifest.sources:
        record = uow.results.get_record(
            "code_snapshot",
            source.result_id,
            manifest.workspace_id,
        )
        if record is None:
            raise ResultError(
                "code_input_result_not_found",
                {"result_id": str(source.result_id)},
            )
        if not isinstance(record, CodeResultSnapshotV1):
            raise ResultError(
                "code_input_result_invalid",
                {"result_id": str(source.result_id)},
            )
        if record.execution_id not in selected_execution_ids:
            raise ResultError(
                "code_input_result_not_selected",
                {"result_id": str(source.result_id)},
            )
        if (
            record.workspace_id != manifest.workspace_id
            or record.root_task_id != manifest.root_task_id
            or record.content_hash != source.result_hash
            or record.request_id
            != selected_by_execution[record.execution_id].effective_request_id
            or record.worker_item_id
            != selected_by_execution[record.execution_id].worker_item_id
            or record.repository_ref != manifest.repository_ref
            or record.input_commit != source.input_commit
            or record.input_tree != source.input_tree
            or record.result_commit != source.result_commit
            or record.result_tree != source.result_tree
        ):
            raise ResultError(
                "code_input_result_mismatch",
                {"result_id": str(source.result_id)},
            )
        manifest_execution_ids.add(record.execution_id)
    if manifest_execution_ids != selected_execution_ids:
        raise ResultError(
            "code_input_result_set_mismatch",
            {
                "selected_count": len(selected_execution_ids),
                "manifest_count": len(manifest_execution_ids),
            },
        )
    return manifest


def _resolve_rework_code_inputs(
    uow: ControlPlaneUnitOfWork,
    manifest: CodeInputManifestV1,
    execution_request: ExecutionRequest,
) -> CodeInputManifestV1:
    candidate_id = execution_request.source_candidate_id
    rework_record_id = execution_request.rework_record_id
    if candidate_id is None or rework_record_id is None:
        raise ResultError("code_input_rework_authorization_missing", {})
    candidate = uow.results.get_record("candidate", candidate_id, manifest.workspace_id)
    record = uow.results.get_record("rework", rework_record_id, manifest.workspace_id)
    if not isinstance(candidate, IntegrationCandidateV1) or not isinstance(
        record, ReworkRecordV1
    ):
        raise ResultError("code_input_rework_authorization_missing", {})
    try:
        require_authorized_source_candidate(uow, execution_request)
    except ResultError as exc:
        raise ResultError(exc.code, dict(exc.details)) from exc
    if record.status not in {ReworkStatus.REQUESTED, ReworkStatus.RUNNING}:
        raise ResultError("code_input_rework_not_active", {})
    if (
        record.candidate_id != candidate.id
        or candidate.workspace_id != manifest.workspace_id
        or candidate.root_task_id != execution_request.root_task_id
        or candidate.plan_snapshot_id != execution_request.plan_snapshot_id
        or execution_request.worker_item_id not in record.target_worker_item_ids
        or manifest.workspace_id != execution_request.workspace_id
        or manifest.root_task_id != execution_request.root_task_id
        or manifest.request_id != execution_request.id
        or manifest.selection_id != candidate.input_selection_id
        or manifest.selection_hash != candidate.input_selection_hash
        or manifest.base_commit != candidate.base_commit
    ):
        raise ResultError("code_input_rework_scope_mismatch", {})

    if tuple(source.result_id for source in manifest.sources) != candidate.source_result_ids:
        raise ResultError("code_input_rework_result_set_mismatch", {})
    for source in manifest.sources:
        record_value = uow.results.get_record(
            "code_snapshot", source.result_id, manifest.workspace_id
        )
        if not isinstance(record_value, CodeResultSnapshotV1):
            raise ResultError(
                "code_input_result_not_found", {"result_id": str(source.result_id)}
            )
        if (
            record_value.workspace_id != manifest.workspace_id
            or record_value.root_task_id != manifest.root_task_id
            or record_value.content_hash != source.result_hash
            or record_value.repository_ref != manifest.repository_ref
            or record_value.input_commit != source.input_commit
            or record_value.input_tree != source.input_tree
            or record_value.result_commit != source.result_commit
            or record_value.result_tree != source.result_tree
        ):
            raise ResultError(
                "code_input_rework_result_mismatch",
                {"result_id": str(source.result_id)},
            )
    return manifest


def _selected_sources(
    selection: CollaborationInputSelectionV1,
) -> tuple[InputSelectionSourceV1, ...]:
    values: list[InputSelectionSourceV1] = []
    seen: set[UUID] = set()
    for group in selection.groups:
        for source in group.sources:
            if source.effective_execution_id not in seen:
                seen.add(source.effective_execution_id)
                values.append(source)
    for hard_input in selection.hard_inputs:
        source = hard_input.source
        if source.effective_execution_id not in seen:
            seen.add(source.effective_execution_id)
            values.append(source)
    return tuple(values)


__all__ = ["resolve_code_inputs"]
