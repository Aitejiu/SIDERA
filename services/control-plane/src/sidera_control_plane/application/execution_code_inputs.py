from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_input_evaluation import (
    build_code_input_manifest,
)
from sidera_control_plane.application.result_input_sources import (
    load_code_result_dependency_closure,
)
from sidera_control_plane.domain.artifacts import ArtifactType
from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeResultSnapshotV1,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.result_integration import IntegrationSpecV1


def execution_code_input_manifest(
    uow: ControlPlaneUnitOfWork,
    request: ExecutionRequest,
    execution: Execution,
    selection: CollaborationInputSelectionV1 | None,
    *,
    plan: TaskPlanSnapshot | None = None,
) -> CodeInputManifestV1 | None:
    """Select immutable code inputs without materializing or scheduling them."""
    if selection is None:
        return None
    if not isinstance(selection, CollaborationInputSelectionV1):
        raise ValidationError("code input selection must be typed")
    sources = (
        *(source for group in selection.groups for source in group.sources),
        *(item.source for item in selection.hard_inputs),
    )
    if not any(
        artifact.artifact_type is ArtifactType.DIFF
        for source in sources
        for artifact in source.artifacts
    ):
        return None
    if plan is None:
        plan = uow.plans.get(request.plan_snapshot_id, request.workspace_id)
    if not isinstance(plan, TaskPlanSnapshot):
        raise ValidationError("code input plan is unavailable or untyped")
    spec = plan.integration_spec
    if spec is None:
        return None
    if not isinstance(spec, IntegrationSpecV1):
        raise ValidationError("code input integration spec must be typed")
    if (
        plan.workspace_id != request.workspace_id
        or plan.root_task_id != request.root_task_id
        or plan.id != request.plan_snapshot_id
        or plan.generation != request.plan_generation
        or selection.plan_content_hash != plan_snapshot_content_hash(plan)
    ):
        raise ValidationError("code input plan scope does not match request")
    if (
        execution.workspace_id != request.workspace_id
        or execution.task_id != request.subtask_id
        or execution.worker_item_id != request.worker_item_id
    ):
        raise ValidationError("code input consumer scope does not match request")
    if (
        selection.workspace_id != request.workspace_id
        or selection.root_task_id != request.root_task_id
        or selection.plan_snapshot_id != request.plan_snapshot_id
        or selection.plan_generation != request.plan_generation
        or selection.target_worker_item_id != request.worker_item_id
        or selection.target_execution_request_id != request.id
        or selection.id != request.input_selection_id
        or selection.selection_hash != request.input_selection_hash
        or selection.selection_hash != selection.compute_selection_hash()
        or selection.target_request_version > request.version
    ):
        raise ValidationError("code input selection scope does not match request")

    # Group priority can differ from hard-input priority for the same fixed source.
    selected: dict[UUID, InputSelectionSourceV1] = {}
    for source in sources:
        previous = selected.get(source.effective_execution_id)
        if previous is not None:
            if replace(previous, priority=source.priority) != source:
                raise ValidationError("code input repeated source identity conflicts")
        else:
            selected[source.effective_execution_id] = source

    snapshots: dict[UUID, CodeResultSnapshotV1] = {}
    for snapshot in uow.results.retained_code_results(request.workspace_id):
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise ValidationError("code input snapshot must be typed")
        if snapshot.execution_id not in selected:
            continue
        if snapshot.execution_id in snapshots:
            raise ValidationError("code input selected snapshot is ambiguous")
        source = selected[snapshot.execution_id]
        if (
            snapshot.workspace_id != request.workspace_id
            or snapshot.root_task_id != request.root_task_id
            or snapshot.repository_ref != spec.repository_ref
            or snapshot.request_id != source.effective_request_id
            or snapshot.worker_item_id != source.worker_item_id
        ):
            raise ValidationError("code input selected snapshot scope does not match")
        diff_ids = {
            artifact.artifact_id
            for artifact in source.artifacts
            if artifact.artifact_type is ArtifactType.DIFF
        }
        if not diff_ids.issubset(snapshot.artifact_ids):
            raise ValidationError("code input selected diff is outside its snapshot")
        snapshots[snapshot.execution_id] = snapshot
    if set(snapshots) != set(selected):
        raise ValidationError("code input selected snapshot is missing")

    for source in selected.values():
        source_execution = uow.executions.get(
            source.effective_execution_id, request.workspace_id
        )
        source_request = uow.execution_requests.get(
            source.effective_request_id, request.workspace_id
        )
        if (
            not isinstance(source_execution, Execution)
            or source_execution.id != source.effective_execution_id
            or source_execution.workspace_id != request.workspace_id
            or source_execution.worker_item_id != source.worker_item_id
            or source_execution.lineage_id != source.effective_lineage_id
            or source_execution.lineage_id not in source.allowed_lineage_ids
            or source_execution.attempt != source.attempt
            or source_execution.status is not ExecutionStatus.SUCCEEDED
        ):
            raise ValidationError("code input selected execution does not match")
        if (
            not isinstance(source_request, ExecutionRequest)
            or source_request.id != source.effective_request_id
            or source_request.workspace_id != request.workspace_id
            or source_request.root_task_id != request.root_task_id
            or source_request.worker_item_id != source.worker_item_id
            or source_request.worker_item_key != source.worker_item_key
            or source_request.subtask_id != source_execution.task_id
            or source_request.execution_lineage_id not in source.allowed_lineage_ids
            or source_request.status is not ExecutionRequestStatus.MATERIALIZED
        ):
            raise ValidationError("code input effective source request does not match")

    try:
        closure = load_code_result_dependency_closure(
            direct_snapshots=tuple(snapshots[item] for item in selected),
            load_snapshot=lambda result_id: uow.results.get_record(
                "code_snapshot", result_id, request.workspace_id
            ),
            workspace_id=request.workspace_id,
            root_task_id=request.root_task_id,
            repository_ref=spec.repository_ref,
        )
    except ValueError as exc:
        raise ValidationError("code input dependency closure is invalid") from exc
    return build_code_input_manifest(
        selection=selection,
        snapshots=closure,
        request_id=request.id,
        execution_id=execution.id,
        repository_ref=spec.repository_ref,
        base_commit=spec.base_commit,
    )
