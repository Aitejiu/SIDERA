from __future__ import annotations

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeResultSnapshotV1,
)
from sidera_control_plane.domain.result_operations import ResultError


def resolve_code_inputs(
    uow: ControlPlaneUnitOfWork,
    manifest: CodeInputManifestV1,
) -> CodeInputManifestV1:
    """Reconcile an input manifest with the immutable selection and snapshots."""

    if not isinstance(manifest, CodeInputManifestV1):
        raise ResultError("code_input_manifest_invalid", {})
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

    selected_execution_ids = _selected_execution_ids(selection)
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
    return manifest


def _selected_execution_ids(selection: object) -> frozenset[object]:
    values: list[object] = []
    for group in getattr(selection, "groups", ()):
        values.extend(
            getattr(source, "effective_execution_id", None)
            for source in getattr(group, "sources", ())
        )
    for hard_input in getattr(selection, "hard_inputs", ()):
        values.append(
            getattr(getattr(hard_input, "source", None), "effective_execution_id", None)
        )
    return frozenset(value for value in values if value is not None)


__all__ = ["resolve_code_inputs"]
