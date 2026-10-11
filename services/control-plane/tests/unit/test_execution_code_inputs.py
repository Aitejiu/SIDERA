from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from sidera_control_plane.application.execution_code_inputs import (
    execution_code_input_manifest,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.artifacts import ArtifactType
from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeResultSnapshotV1,
)
from sidera_control_plane.domain.collaboration import CollaborationGateMode
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionHardInputV1,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.result_integration import IntegrationSpecV1

from .result_input_fixtures import selection_mapping
from .result_integration_fixtures import (
    _impact_snapshot,
    integration_mapping,
    rework_impact_case,
)
from .test_revision_diff import _v4_snapshot_with_two_workers


def rehash_selection(
    selection: CollaborationInputSelectionV1,
) -> CollaborationInputSelectionV1:
    return replace(selection, selection_hash=selection.compute_selection_hash())


def changed_snapshot(
    snapshot: CodeResultSnapshotV1, **changes: object
) -> CodeResultSnapshotV1:
    return _impact_snapshot(**(snapshot.to_mapping() | changes))


@dataclass(frozen=True, slots=True)
class CodeInputCase:
    plan: TaskPlanSnapshot
    request: ExecutionRequest
    execution: Execution
    selection: CollaborationInputSelectionV1
    snapshots: tuple[CodeResultSnapshotV1, ...]
    source_requests: tuple[ExecutionRequest, ...]
    source_executions: tuple[Execution, ...]

    def uow(self) -> SimpleNamespace:
        requests = {item.id: item for item in (self.request, *self.source_requests)}
        executions = {
            item.id: item for item in (self.execution, *self.source_executions)
        }
        records = {item.id: item for item in self.snapshots}
        return SimpleNamespace(
            plans=SimpleNamespace(
                get=lambda plan_id, workspace_id: (
                    self.plan
                    if (plan_id, workspace_id) == (self.plan.id, self.plan.workspace_id)
                    else None
                )
            ),
            results=SimpleNamespace(
                retained_code_results=lambda workspace_id: (
                    self.snapshots if workspace_id == self.request.workspace_id else ()
                ),
                get_record=lambda kind, result_id, workspace_id: (
                    records.get(result_id)
                    if kind == "code_snapshot"
                    and workspace_id == self.request.workspace_id
                    else None
                ),
            ),
            executions=SimpleNamespace(
                get=lambda execution_id, workspace_id: (
                    executions.get(execution_id)
                    if workspace_id == self.request.workspace_id
                    else None
                )
            ),
            execution_requests=SimpleNamespace(
                get=lambda request_id, workspace_id: (
                    requests.get(request_id)
                    if workspace_id == self.request.workspace_id
                    else None
                )
            ),
        )


def code_input_case(
    *, hard_only: bool = False, consumes: bool = False
) -> CodeInputCase:
    base = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    impact = rework_impact_case()
    spec_mapping = integration_mapping()
    spec_mapping.update(
        integration_worker_item_key="backend",
        input_group_keys=["source"],
        rework_policy={
            "max_auto_rework_rounds": 2,
            "max_auto_rework_seconds": 1800,
            "allowed_worker_item_keys": ["source", "backend"],
        },
    )
    plan = replace(
        _v4_snapshot_with_two_workers(),
        id=base.plan_snapshot_id,
        workspace_id=impact.a1.workspace_id,
        root_task_id=impact.a1.root_task_id,
        generation=base.plan_generation,
        schema_version=5,
        integration_spec=IntegrationSpecV1.from_mapping(spec_mapping),
    )
    plan = TaskPlanSnapshot.from_payload(plan.to_payload())
    direct = impact.b if consumes else impact.a1
    original = base.groups[0].sources[0]
    source = replace(
        original,
        worker_item_key="source",
        worker_item_id=direct.worker_item_id,
        effective_request_id=direct.request_id,
        effective_execution_id=direct.execution_id,
        artifacts=(
            replace(
                original.artifacts[0],
                artifact_id=direct.artifact_ids[0],
                artifact_type=ArtifactType.DIFF,
            ),
        ),
        evidence=(),
    )
    group = replace(base.groups[0], group_key="source", sources=(source,))
    hard_input = InputSelectionHardInputV1(
        dependency_key=f"request:{source.effective_request_id}",
        rule_hash="f" * 64,
        source=source,
    )
    selection = rehash_selection(
        replace(
            base,
            workspace_id=plan.workspace_id,
            root_task_id=plan.root_task_id,
            plan_content_hash=plan_snapshot_content_hash(plan),
            groups=() if hard_only else (group,),
            hard_inputs=(hard_input,) if hard_only else (),
        )
    )
    now = selection.created_at
    subtask_id = plan.subtasks[0].task_id
    assert subtask_id is not None
    request = ExecutionRequest.create(
        request_id=selection.target_execution_request_id,
        workspace_id=selection.workspace_id,
        planning_request_id=UUID(int=901),
        proposal_revision_id=UUID(int=902),
        plan_snapshot_id=selection.plan_snapshot_id,
        plan_generation=selection.plan_generation,
        root_task_id=selection.root_task_id,
        subtask_id=subtask_id,
        worker_item_id=selection.target_worker_item_id,
        worker_item_key="backend",
        intent_key="backend",
        purpose="Integrate selected code",
        capability_requirements=("code",),
        adapter_preferences=(),
        budget_class="standard",
        not_before=now,
        max_attempts=2,
        expected_artifact_refs=("artifact_type:diff",),
        expected_evidence_refs=(),
        collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED,
        now=now,
    )
    request.mark_ready(now=now)
    request.bind_input_selection(
        selection_id=selection.id, selection_hash=selection.selection_hash, now=now
    )
    assert request.version == selection.target_request_version
    execution = Execution.create(
        execution_id=UUID(int=903),
        workspace_id=request.workspace_id,
        task_id=request.subtask_id,
        worker_item_id=request.worker_item_id,
        lineage_id=UUID(int=903),
        attempt=1,
        max_attempts=2,
        purpose=request.purpose,
        capability_requirements=request.capability_requirements,
        pinned_worker_id=None,
        not_before=now,
    )
    source_execution = replace(
        execution,
        id=source.effective_execution_id,
        worker_item_id=source.worker_item_id,
        lineage_id=source.effective_lineage_id,
        status=ExecutionStatus.SUCCEEDED,
        finished_at=now,
    )
    source_request = replace(
        request,
        id=source.effective_request_id,
        worker_item_id=source.worker_item_id,
        worker_item_key=source.worker_item_key,
        input_selection_id=None,
        input_selection_hash=None,
    )
    source_request.materialize(
        execution_lineage_id=source.effective_lineage_id,
        first_execution_id=source_execution.id,
        now=now,
    )
    return CodeInputCase(
        plan,
        request,
        execution,
        selection,
        (impact.a1, impact.b) if consumes else (impact.a1,),
        (source_request,),
        (source_execution,),
    )


def manifest_for(
    case: CodeInputCase, *, supplied_plan: bool = False
) -> CodeInputManifestV1 | None:
    return execution_code_input_manifest(
        cast(ControlPlaneUnitOfWork, case.uow()),
        case.request,
        case.execution,
        case.selection,
        plan=case.plan if supplied_plan else None,
    )


@pytest.mark.parametrize("hard_only", (False, True))
@pytest.mark.parametrize("supplied_plan", (False, True))
def test_selected_code_pins_the_exact_request_and_execution(
    hard_only: bool, supplied_plan: bool
) -> None:
    case = code_input_case(hard_only=hard_only)
    manifest = manifest_for(case, supplied_plan=supplied_plan)
    assert manifest is not None
    assert (manifest.selection_id, manifest.selection_hash) == (
        case.selection.id,
        case.selection.selection_hash,
    )
    assert (manifest.request_id, manifest.execution_id) == (
        case.request.id,
        case.execution.id,
    )
    assert manifest.repository_ref == "repo://integration-test"
    assert manifest.base_commit == "a" * 40
    assert tuple(item.result_id for item in manifest.sources) == (case.snapshots[0].id,)


def test_group_and_hard_source_deduplicate_with_group_specific_priority() -> None:
    case = code_input_case()
    source = case.selection.groups[0].sources[0]
    selection = rehash_selection(
        replace(
            case.selection,
            hard_inputs=(
                InputSelectionHardInputV1(
                    dependency_key="hard:source",
                    rule_hash="f" * 64,
                    source=replace(source, priority=2),
                ),
            ),
        )
    )
    case = replace(
        case,
        selection=selection,
        request=replace(case.request, input_selection_hash=selection.selection_hash),
    )
    manifest = manifest_for(case)
    assert manifest is not None
    assert len(manifest.sources) == 1


def test_hard_only_consumed_snapshot_preserves_dependencies_and_ignores_late_results() -> (
    None
):
    case = code_input_case(hard_only=True, consumes=True)
    impact = rework_impact_case()
    first = manifest_for(case)
    replay = manifest_for(
        replace(case, snapshots=(impact.c, impact.b, impact.a2, impact.a1))
    )
    assert first is not None
    assert tuple(item.result_id for item in first.sources) == (
        impact.a1.id,
        impact.b.id,
    )
    assert replay == first


def test_group_and_distinct_hard_source_keep_selection_order_on_replay() -> None:
    case = code_input_case()
    impact = rework_impact_case()
    source = case.selection.groups[0].sources[0]
    lineage_id = UUID(int=940)
    hard_source = replace(
        source,
        worker_item_key="b",
        worker_item_id=impact.b.worker_item_id,
        effective_request_id=impact.b.request_id,
        effective_execution_id=impact.b.execution_id,
        effective_lineage_id=lineage_id,
        allowed_lineage_ids=(lineage_id,),
        artifacts=(replace(source.artifacts[0], artifact_id=impact.b.artifact_ids[0]),),
    )
    selection = rehash_selection(
        replace(
            case.selection,
            hard_inputs=(
                InputSelectionHardInputV1(
                    dependency_key="hard:b", rule_hash="f" * 64, source=hard_source
                ),
            ),
        )
    )
    case = replace(
        case,
        selection=selection,
        request=replace(case.request, input_selection_hash=selection.selection_hash),
        snapshots=(impact.b, impact.a1),
        source_requests=(
            *case.source_requests,
            replace(
                case.source_requests[0],
                id=hard_source.effective_request_id,
                worker_item_id=hard_source.worker_item_id,
                worker_item_key="b",
                execution_lineage_id=lineage_id,
                first_execution_id=hard_source.effective_execution_id,
            ),
        ),
        source_executions=(
            *case.source_executions,
            replace(
                case.source_executions[0],
                id=hard_source.effective_execution_id,
                worker_item_id=hard_source.worker_item_id,
                lineage_id=lineage_id,
            ),
        ),
    )
    first = manifest_for(case)
    foreign = changed_snapshot(impact.c, root_task_id=str(UUID(int=941)))
    replay = manifest_for(
        replace(case, snapshots=(foreign, impact.a1, impact.a2, impact.b))
    )
    assert first is not None
    assert tuple(item.result_id for item in first.sources) == (
        impact.a1.id,
        impact.b.id,
    )
    assert replay == first


def test_dependency_repository_reads_only_reachable_immutable_records() -> None:
    case = code_input_case(hard_only=True, consumes=True)
    dependency, direct = case.snapshots
    uow = case.uow()
    load = uow.results.get_record
    reads: list[tuple[str, UUID, UUID]] = []

    def load_record(kind: str, result_id: UUID, workspace_id: UUID) -> object:
        reads.append((kind, result_id, workspace_id))
        return load(kind, result_id, workspace_id)

    uow.results.get_record = load_record
    uow.results.retained_code_results = lambda *_args: (direct,)
    manifest = execution_code_input_manifest(
        cast(ControlPlaneUnitOfWork, uow), case.request, case.execution, case.selection
    )
    assert manifest is not None
    assert reads == [("code_snapshot", dependency.id, case.request.workspace_id)]
    assert tuple(item.result_id for item in manifest.sources) == (
        dependency.id,
        direct.id,
    )


@pytest.mark.parametrize(
    "missing", ("source_request", "source_execution", "typed_snapshot")
)
def test_missing_or_untyped_selected_records_fail_closed(missing: str) -> None:
    case = code_input_case(hard_only=True)
    uow = case.uow()
    if missing == "source_request":
        uow.execution_requests = SimpleNamespace(get=lambda *_args: None)
    elif missing == "source_execution":
        uow.executions = SimpleNamespace(get=lambda *_args: None)
    else:
        uow.results.retained_code_results = lambda *_args: (
            case.snapshots[0].to_mapping(),
        )
    with pytest.raises(ValidationError):
        execution_code_input_manifest(
            cast(ControlPlaneUnitOfWork, uow),
            case.request,
            case.execution,
            case.selection,
        )


def test_materialized_request_keeps_historical_selection_version() -> None:
    case = code_input_case(hard_only=True)
    case.request.materialize(
        execution_lineage_id=case.execution.lineage_id,
        first_execution_id=case.execution.id,
        now=case.selection.created_at,
    )
    assert case.request.version > case.selection.target_request_version
    assert manifest_for(case) is not None


@pytest.mark.parametrize("legacy", ("no_selection", "no_diff", "no_spec"))
def test_legacy_inputs_need_no_code_repositories(legacy: str) -> None:
    case = code_input_case()
    selection = case.selection
    plan = case.plan
    if legacy == "no_selection":
        selection = None
    elif legacy == "no_diff":
        source = selection.groups[0].sources[0]
        selection = replace(
            selection,
            groups=(
                replace(
                    selection.groups[0],
                    sources=(
                        replace(
                            source,
                            artifacts=(
                                replace(
                                    source.artifacts[0],
                                    artifact_type=ArtifactType.REPORT,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
    else:
        plan = replace(plan, integration_spec=None)
    assert (
        execution_code_input_manifest(
            cast(ControlPlaneUnitOfWork, SimpleNamespace()),
            case.request,
            case.execution,
            selection,
            plan=plan,
        )
        is None
    )


@pytest.mark.parametrize(
    "field",
    (
        "workspace_id",
        "root_task_id",
        "request_id",
        "worker_item_id",
        "repository_ref",
    ),
)
def test_selected_snapshot_scope_mismatch_fails_closed(field: str) -> None:
    case = code_input_case(hard_only=True)
    value = "repo://foreign" if field == "repository_ref" else str(UUID(int=999))
    snapshot = changed_snapshot(case.snapshots[0], **{field: value})
    with pytest.raises(ValidationError):
        manifest_for(replace(case, snapshots=(snapshot,)))


@pytest.mark.parametrize("invalid", ("missing", "duplicate", "outside_artifact"))
def test_selected_snapshot_must_be_unique_and_own_the_diff(invalid: str) -> None:
    case = code_input_case()
    snapshot = case.snapshots[0]
    snapshots = (
        ()
        if invalid == "missing"
        else (
            (snapshot, replace(snapshot, id=UUID(int=999)))
            if invalid == "duplicate"
            else (changed_snapshot(snapshot, artifact_ids=[str(UUID(int=999))]),)
        )
    )
    with pytest.raises(ValidationError):
        manifest_for(replace(case, snapshots=snapshots))


@pytest.mark.parametrize(
    "field",
    (
        "workspace_id",
        "id",
        "root_task_id",
        "generation",
        "integration_spec",
    ),
)
def test_plan_scope_or_untyped_spec_fails_closed(field: str) -> None:
    case = code_input_case()
    value = (
        case.plan.generation + 1
        if field == "generation"
        else integration_mapping()
        if field == "integration_spec"
        else UUID(int=999)
    )
    with pytest.raises(ValidationError):
        manifest_for(
            replace(case, plan=replace(case.plan, **{field: value})), supplied_plan=True
        )


def test_missing_plan_fails_closed() -> None:
    case = code_input_case()
    uow = case.uow()
    uow.plans = SimpleNamespace(get=lambda *_args: None)
    with pytest.raises(ValidationError):
        execution_code_input_manifest(
            cast(ControlPlaneUnitOfWork, uow),
            case.request,
            case.execution,
            case.selection,
        )


@pytest.mark.parametrize(
    "field",
    (
        "workspace_id",
        "root_task_id",
        "plan_snapshot_id",
        "plan_generation",
        "plan_content_hash",
        "target_worker_item_id",
        "target_execution_request_id",
        "selection_hash",
        "id",
        "target_request_version",
    ),
)
def test_selection_must_match_its_persisted_request_and_plan(field: str) -> None:
    case = code_input_case()
    value = (
        99
        if field in {"plan_generation", "target_request_version"}
        else "f" * 64
        if field in {"plan_content_hash", "selection_hash"}
        else UUID(int=999)
    )
    with pytest.raises(ValidationError):
        manifest_for(replace(case, selection=replace(case.selection, **{field: value})))


@pytest.mark.parametrize("field", ("workspace_id", "task_id", "worker_item_id"))
def test_consumer_scope_mismatch_fails_closed(field: str) -> None:
    case = code_input_case()
    with pytest.raises(ValidationError):
        manifest_for(
            replace(case, execution=replace(case.execution, **{field: UUID(int=999)}))
        )


@pytest.mark.parametrize(
    "field",
    (
        "id",
        "workspace_id",
        "worker_item_id",
        "task_id",
        "lineage_id",
        "attempt",
        "status",
    ),
)
def test_source_execution_must_match_fixed_lineage_attempt_and_success(
    field: str,
) -> None:
    case = code_input_case()
    source = case.source_executions[0]
    value = (
        2
        if field == "attempt"
        else ExecutionStatus.FAILED
        if field == "status"
        else UUID(int=999)
    )
    changed = replace(source, **{field: value})
    uow = case.uow()
    uow.executions = SimpleNamespace(get=lambda *_args: changed)
    with pytest.raises(ValidationError):
        execution_code_input_manifest(
            cast(ControlPlaneUnitOfWork, uow),
            case.request,
            case.execution,
            case.selection,
            plan=case.plan,
        )


@pytest.mark.parametrize(
    "field",
    (
        "id",
        "workspace_id",
        "root_task_id",
        "worker_item_id",
        "worker_item_key",
        "subtask_id",
        "execution_lineage_id",
        "status",
    ),
)
def test_effective_source_request_must_match_selection(field: str) -> None:
    case = code_input_case()
    value = (
        ExecutionRequestStatus.READY
        if field == "status"
        else "foreign"
        if field == "worker_item_key"
        else UUID(int=999)
    )
    changed = replace(case.source_requests[0], **{field: value})
    uow = case.uow()
    uow.execution_requests = SimpleNamespace(get=lambda *_args: changed)
    with pytest.raises(ValidationError):
        execution_code_input_manifest(
            cast(ControlPlaneUnitOfWork, uow),
            case.request,
            case.execution,
            case.selection,
            plan=case.plan,
        )


def test_contradictory_group_and_hard_source_identity_fails_closed() -> None:
    case = code_input_case()
    source = case.selection.groups[0].sources[0]
    selection = rehash_selection(
        replace(
            case.selection,
            hard_inputs=(
                InputSelectionHardInputV1(
                    dependency_key="hard:source",
                    rule_hash="f" * 64,
                    source=replace(source, effective_request_id=UUID(int=999)),
                ),
            ),
        )
    )
    request = replace(case.request, input_selection_hash=selection.selection_hash)
    with pytest.raises(ValidationError):
        manifest_for(replace(case, request=request, selection=selection))


@pytest.mark.parametrize("invalid", ("missing", "foreign", "cycle"))
def test_only_valid_reachable_dependencies_can_enter_manifest(invalid: str) -> None:
    case = code_input_case(hard_only=True, consumes=True)
    dependency, direct = case.snapshots
    if invalid == "missing":
        snapshots = (direct,)
    elif invalid == "foreign":
        snapshots = (
            changed_snapshot(dependency, root_task_id=str(UUID(int=999))),
            direct,
        )
    else:
        snapshots = (
            changed_snapshot(dependency, consumed_result_ids=[str(direct.id)]),
            direct,
        )
    with pytest.raises(ValidationError):
        manifest_for(replace(case, snapshots=snapshots))
