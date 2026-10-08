from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.activation_reconciler import (
    ActivationReconciler,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.artifacts import Artifact, ArtifactType, EvidenceClaim
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    CollaborationRunStatus,
    RuntimeRelease,
)
from sidera_control_plane.domain.context_packages import ContextAssemblyRequest
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ActivationDecision,
    ActivationGate,
    DependencyEnforcement,
    ExecutionRequest,
    ExecutionRequestDependency,
    ExecutionRequestStatus,
    UpstreamActivationFact,
)
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionHardInputV1,
)
from sidera_control_plane.domain.planning import PlanningRequestStatus
from sidera_control_plane.domain.task import Task, TaskStatus
from sidera_control_plane.domain.worker_item import WorkerItem

from .test_execution_code_inputs import CodeInputCase, code_input_case, rehash_selection

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("70000000-0000-0000-0000-000000000001")


class FakeExecutionRequestRepository:
    def __init__(self, request: ExecutionRequest) -> None:
        self.request = request
        self.saved: list[tuple[ExecutionRequest, int]] = []
        self.dependencies: tuple[ExecutionRequestDependency, ...] = ()

    def save(self, request: ExecutionRequest, previous_version: int) -> None:
        self.request = request
        self.saved.append((request, previous_version))

    def add(self, request: ExecutionRequest) -> None:
        self.request = request

    def get(self, request_id, workspace_id, *, for_update=False):
        del for_update
        if request_id == self.request.id and workspace_id == self.request.workspace_id:
            return self.request
        return None

    def list_dependencies(self, downstream_request_id, workspace_id):
        if downstream_request_id == self.request.id and workspace_id == self.request.workspace_id:
            return self.dependencies
        return ()

    def claim_blocked(self, *, now, limit):
        del now, limit
        return (
            (self.request,)
            if self.request.status is ExecutionRequestStatus.BLOCKED
            else ()
        )

    def claim_ready(self, *, now, limit):
        del now, limit
        return (
            (self.request,)
            if self.request.status is ExecutionRequestStatus.READY
            else ()
        )


class FakeEvents:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class FakeCommands:
    def __init__(self) -> None:
        self.results = []

    def record_applied(self, envelope, request_hash, result) -> None:
        self.results.append((envelope, request_hash, result))


class FakeUnitOfWork:
    def __init__(self, request: ExecutionRequest) -> None:
        self.execution_requests = FakeExecutionRequestRepository(request)
        self.events = FakeEvents()
        self.commands = FakeCommands()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback

    def flush(self) -> None:
        return None

    @contextmanager
    def savepoint(self):
        yield


def make_request() -> ExecutionRequest:
    return ExecutionRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        planning_request_id=uuid4(),
        proposal_revision_id=uuid4(),
        plan_snapshot_id=uuid4(),
        plan_generation=1,
        root_task_id=uuid4(),
        subtask_id=uuid4(),
        worker_item_id=uuid4(),
        worker_item_key="worker",
        intent_key="worker",
        purpose="Run worker",
        capability_requirements=("code",),
        adapter_preferences=("fake",),
        budget_class="standard",
        not_before=NOW,
        max_attempts=1,
        expected_artifact_refs=("artifact_type:diff",),
        expected_evidence_refs=("acceptance:done",),
        now=NOW,
    )


def test_blocked_reason_fingerprint_does_not_repeat_gate_event() -> None:
    request = make_request()
    uow = FakeUnitOfWork(request)
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        owner_id="test-activation",
        clock=lambda: NOW,
    )
    decision = ActivationDecision(
        ready=False,
        reason="dependencies_blocked",
        missing_artifact_refs=("artifact_type:diff",),
        details={
            "missing_upstream_request_ids": (),
            "blocked_upstream_request_ids": (),
            "missing_artifact_refs": ("artifact_type:diff",),
            "missing_evidence_refs": (),
        },
    )
    context = SimpleNamespace()
    reconciler._current_context = lambda _uow, _request: (context, None)  # type: ignore[method-assign]
    reconciler._decision = lambda _uow, _request, _context, now: decision  # type: ignore[method-assign]

    first = reconciler._reconcile_blocked(
        cast(ControlPlaneUnitOfWork, uow), request, now=NOW
    )
    second = reconciler._reconcile_blocked(
        cast(ControlPlaneUnitOfWork, uow), request, now=NOW
    )

    assert first == (False, True)
    assert second == (False, True)
    assert request.version == 2
    assert len(uow.events.events) == 1


def test_release_required_request_waits_for_collaboration_release() -> None:
    request = replace(
        make_request(),
        collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED,
    )
    uow = FakeUnitOfWork(request)
    uow.collaboration_runs = SimpleNamespace(
        get_current=lambda *_args, **_kwargs: None
    )
    uow.plans = SimpleNamespace(get_head=lambda *_args, **_kwargs: None)
    uow.collaboration_ledger = SimpleNamespace(
        get_release_for_request=lambda *_args, **_kwargs: None
    )
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        owner_id="test-activation",
        clock=lambda: NOW,
    )

    decision = reconciler._decision(
        cast(ControlPlaneUnitOfWork, uow),
        request,
        SimpleNamespace(task=SimpleNamespace(status=TaskStatus.READY)),
        now=NOW,
    )

    assert not decision.ready
    assert decision.reason == "collaboration_run_pending"


def test_exact_collaboration_release_allows_activation_after_static_gate() -> None:
    request = replace(
        make_request(),
        collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED,
    )
    run_id = uuid4()
    plan_hash = "a" * 64
    run = SimpleNamespace(
        id=run_id,
        workspace_id=request.workspace_id,
        root_task_id=request.root_task_id,
        plan_snapshot_id=request.plan_snapshot_id,
        plan_generation=request.plan_generation,
        plan_content_hash=plan_hash,
        status=CollaborationRunStatus.ACTIVE,
    )
    head = SimpleNamespace(
        workspace_id=request.workspace_id,
        root_task_id=request.root_task_id,
        active_snapshot_id=request.plan_snapshot_id,
        active_generation=request.plan_generation,
        active_snapshot_hash=plan_hash,
    )
    release = SimpleNamespace(
        workspace_id=request.workspace_id,
        run_id=run_id,
        plan_snapshot_id=request.plan_snapshot_id,
        plan_generation=request.plan_generation,
        plan_content_hash=plan_hash,
        execution_request_id=request.id,
        request_version_at_release=request.version,
        input_selection_id=None,
        input_selection_hash=None,
    )
    uow = FakeUnitOfWork(request)
    uow.collaboration_runs = SimpleNamespace(
        get_current=lambda *_args, **_kwargs: run
    )
    uow.plans = SimpleNamespace(get_head=lambda *_args, **_kwargs: head)
    uow.collaboration_ledger = SimpleNamespace(
        get_release_for_request=lambda *_args, **_kwargs: release
    )
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        owner_id="test-activation",
        clock=lambda: NOW,
    )

    decision = reconciler._decision(
        cast(ControlPlaneUnitOfWork, uow),
        request,
        SimpleNamespace(task=SimpleNamespace(status=TaskStatus.READY)),
        now=NOW,
    )

    assert decision.ready
    assert decision.reason == "ready"


def test_input_selection_keeps_pure_hard_dependencies_in_activation_facts() -> None:
    from .result_input_fixtures import selection_mapping

    selection = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    request = replace(
        make_request(),
        input_selection_id=selection.id,
        input_selection_hash=selection.selection_hash,
    )
    upstream_ids = (
        UUID("61000000-0000-0000-0000-000000000012"),
        UUID("61000000-0000-0000-0000-000000000018"),
    )
    upstream_requests = {
        upstream_id: replace(make_request(), id=upstream_id)
        for upstream_id in upstream_ids
    }
    uow = SimpleNamespace(
        execution_requests=SimpleNamespace(
            get=lambda request_id, _workspace_id: upstream_requests.get(request_id),
        )
    )
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        owner_id="test-activation",
        clock=lambda: NOW,
    )
    reconciler._fact_for_upstream = lambda _uow, upstream: UpstreamActivationFact(
        request_id=upstream.id,
        execution_status=ExecutionStatus.SUCCEEDED,
        execution_lineage_id=uuid4(),
    )  # type: ignore[method-assign]
    dependencies = tuple(
        ExecutionRequestDependency(
            downstream_request_id=request.id,
            upstream_request_id=upstream_id,
            enforcement=DependencyEnforcement.HARD,
        )
        for upstream_id in upstream_ids
    )

    facts = reconciler._upstream_facts(
        cast(ControlPlaneUnitOfWork, uow),
        request,
        dependencies,
        selection=selection,
    )

    assert set(facts) == set(upstream_ids)


def _reworked_hard_input_case(
    *,
    dependency_key: str | None = None,
    include_evidence: bool = True,
    required_artifact_ref: str = "artifact_type:report",
):
    from .result_input_fixtures import selection_mapping

    base = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    source_a = base.groups[0].sources[0]
    original_request_id = source_a.effective_request_id
    effective_request_id = UUID("61000000-0000-0000-0000-000000000112")
    effective_lineage_id = UUID("61000000-0000-0000-0000-000000000113")
    reworked_source = replace(
        source_a,
        effective_request_id=effective_request_id,
        allowed_lineage_ids=(effective_lineage_id,),
        effective_lineage_id=effective_lineage_id,
        effective_execution_id=UUID("61000000-0000-0000-0000-000000000114"),
        attempt=2,
        inheritance_refs=("rework:source-a",),
        source_basis_hash="9" * 64,
        evidence=source_a.evidence if include_evidence else (),
    )
    hard_input = InputSelectionHardInputV1(
        dependency_key=dependency_key or f"request:{original_request_id}",
        rule_hash="f" * 64,
        source=reworked_source,
    )
    draft = replace(
        base,
        groups=(
            replace(
                base.groups[0],
                sources=(reworked_source, base.groups[0].sources[1]),
            ),
        ),
        hard_inputs=(hard_input,),
        selection_hash="0" * 64,
    )
    selection = replace(draft, selection_hash=draft.compute_selection_hash())
    assert CollaborationInputSelectionV1.from_mapping(selection.to_mapping()) == selection
    dependency = ExecutionRequestDependency(
        downstream_request_id=selection.target_execution_request_id,
        upstream_request_id=original_request_id,
        enforcement=DependencyEnforcement.HARD,
        required_artifact_refs=(required_artifact_ref,),
        required_evidence_refs=("acceptance:research-complete",),
    )
    return (
        selection,
        dependency,
        original_request_id,
        effective_lineage_id,
    )


def test_reworked_hard_input_uses_committed_source_for_original_dependency() -> None:
    selection, dependency, original_id, effective_lineage_id = (
        _reworked_hard_input_case()
    )

    facts = ActivationReconciler._selected_hard_input_facts(
        dependencies=(dependency,), selection=selection
    )
    decision = ActivationGate.evaluate(dependencies=(dependency,), upstream_facts=facts)

    assert set(facts) == {original_id}
    assert facts[original_id].execution_lineage_id == effective_lineage_id
    assert decision.ready


def test_reworked_hard_input_wrong_key_does_not_read_live_upstream() -> None:
    selection, dependency, original_id, _effective_lineage_id = (
        _reworked_hard_input_case(
            dependency_key="request:61000000-0000-0000-0000-000000000999"
        )
    )
    request = replace(
        make_request(),
        id=selection.target_execution_request_id,
        workspace_id=selection.workspace_id,
        input_selection_id=selection.id,
        input_selection_hash=selection.selection_hash,
    )

    def reject_live_read(*_args, **_kwargs):
        raise AssertionError("selector dependency read live upstream")

    uow = SimpleNamespace(
        execution_requests=SimpleNamespace(get=reject_live_read)
    )
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        owner_id="test-activation",
        clock=lambda: NOW,
    )

    facts = reconciler._upstream_facts(
        cast(ControlPlaneUnitOfWork, uow),
        request,
        (dependency,),
        selection=selection,
    )
    decision = ActivationGate.evaluate(dependencies=(dependency,), upstream_facts=facts)

    assert facts == {}
    assert decision.missing_upstream_request_ids == (original_id,)


def test_reworked_hard_input_still_requires_selected_evidence() -> None:
    selection, dependency, _original_id, _effective_lineage_id = (
        _reworked_hard_input_case(include_evidence=False)
    )

    facts = ActivationReconciler._selected_hard_input_facts(
        dependencies=(dependency,), selection=selection
    )
    decision = ActivationGate.evaluate(dependencies=(dependency,), upstream_facts=facts)

    assert set(facts) == {_original_id}
    assert not decision.ready
    assert decision.missing_evidence_refs == ("acceptance:research-complete",)


def test_reworked_hard_input_still_requires_selected_artifact() -> None:
    selection, dependency, _original_id, _effective_lineage_id = (
        _reworked_hard_input_case(required_artifact_ref="artifact_type:screenshot")
    )

    facts = ActivationReconciler._selected_hard_input_facts(
        dependencies=(dependency,), selection=selection
    )
    decision = ActivationGate.evaluate(dependencies=(dependency,), upstream_facts=facts)

    assert set(facts) == {_original_id}
    assert not decision.ready
    assert "artifact_type:screenshot" in decision.missing_artifact_refs


def test_reintegration_release_can_resolve_its_completed_collaboration_run() -> None:
    request = replace(
        make_request(),
        purpose="reintegrate",
        collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED,
    )
    run_id = uuid4()
    plan_hash = "c" * 64
    run = SimpleNamespace(
        id=run_id,
        workspace_id=request.workspace_id,
        root_task_id=request.root_task_id,
        plan_snapshot_id=request.plan_snapshot_id,
        plan_generation=request.plan_generation,
        plan_content_hash=plan_hash,
        status=CollaborationRunStatus.COMPLETED,
    )
    head = SimpleNamespace(
        workspace_id=request.workspace_id,
        root_task_id=request.root_task_id,
        active_snapshot_id=request.plan_snapshot_id,
        active_generation=request.plan_generation,
        active_snapshot_hash=plan_hash,
    )
    release = SimpleNamespace(
        run_id=run_id,
        workspace_id=request.workspace_id,
        plan_snapshot_id=request.plan_snapshot_id,
        plan_generation=request.plan_generation,
        plan_content_hash=plan_hash,
        execution_request_id=request.id,
        request_version_at_release=request.version,
        input_selection_id=None,
        input_selection_hash=None,
    )
    uow = FakeUnitOfWork(request)
    uow.collaboration_runs = SimpleNamespace(
        get_current=lambda *_args, **_kwargs: None,
        get=lambda *_args, **_kwargs: run,
    )
    uow.plans = SimpleNamespace(get_head=lambda *_args, **_kwargs: head)
    uow.collaboration_ledger = SimpleNamespace(
        get_release_for_request=lambda *_args, **_kwargs: release
    )
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        owner_id="test-activation",
        clock=lambda: NOW,
    )

    decision = reconciler._decision(
        cast(ControlPlaneUnitOfWork, uow),
        request,
        SimpleNamespace(task=SimpleNamespace(status=TaskStatus.READY)),
        now=NOW,
    )

    assert decision.ready
    assert decision.reason == "ready"


def test_successful_lineage_fact_requires_real_artifacts_and_evidence() -> None:
    upstream = make_request()
    upstream.mark_ready(now=NOW)
    lineage_id = uuid4()
    upstream.materialize(
        execution_lineage_id=lineage_id,
        first_execution_id=uuid4(),
        now=NOW,
    )
    execution = Execution.create(
        execution_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=uuid4(),
        worker_item_id=uuid4(),
        lineage_id=lineage_id,
        attempt=1,
        max_attempts=1,
        purpose="test",
        capability_requirements=frozenset({"code"}),
        pinned_worker_id=None,
        not_before=NOW,
    )
    execution = replace(
        execution,
        status=ExecutionStatus.SUCCEEDED,
        finished_at=NOW,
        version=2,
    )
    artifact = Artifact(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=execution.task_id,
        execution_id=execution.id,
        session_id=None,
        artifact_type=ArtifactType.DIFF,
        storage_ref="memory://diff",
        content_hash="a" * 64,
        size_bytes=1,
        media_type="text/plain",
        provenance={},
    )
    valid_claim = EvidenceClaim.create_candidate(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=execution.task_id,
        execution_id=execution.id,
        acceptance_criterion_ref="done",
        conclusion="passed",
        environment_ref="test",
        artifact_ids=(artifact.id,),
        producer="worker",
        created_at=NOW,
    )
    orphan_claim = EvidenceClaim.create_candidate(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=execution.task_id,
        execution_id=execution.id,
        acceptance_criterion_ref="orphan",
        conclusion="passed",
        environment_ref="test",
        artifact_ids=(uuid4(),),
        producer="worker",
        created_at=NOW,
    )
    planning_request = SimpleNamespace(status=PlanningRequestStatus.MATERIALIZED)
    uow = SimpleNamespace(
        execution_requests=SimpleNamespace(get=lambda *_args, **_kwargs: upstream),
        planning_requests=SimpleNamespace(get=lambda *_args, **_kwargs: planning_request),
        planning_records=SimpleNamespace(
            get_current_proposal=lambda *_args, **_kwargs: SimpleNamespace(
                id=upstream.proposal_revision_id
            )
        ),
        plans=SimpleNamespace(
            get_current_for_root=lambda *_args, **_kwargs: SimpleNamespace(
                id=upstream.plan_snapshot_id,
                generation=upstream.plan_generation,
            )
        ),
        executions=SimpleNamespace(list_for_lineage=lambda *_args, **_kwargs: (execution,)),
        artifacts=SimpleNamespace(
            list_for_executions=lambda *_args, **_kwargs: (artifact,)
        ),
        evidence_claims=SimpleNamespace(
            list_for_executions=lambda *_args, **_kwargs: (valid_claim, orphan_claim)
        ),
    )
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        owner_id="test-activation",
        clock=lambda: NOW,
    )
    reconciler._request_matches_current_plan = lambda *_args, **_kwargs: True  # type: ignore[method-assign]

    fact = reconciler._fact_for_upstream(
        cast(ControlPlaneUnitOfWork, uow), upstream
    )

    assert fact.execution_status is ExecutionStatus.SUCCEEDED
    assert fact.execution_lineage_id == upstream.execution_lineage_id
    assert fact.artifact_refs == ("artifact_type:diff",)
    assert fact.evidence_refs == ("acceptance:done",)


class MaterializationRequests(FakeExecutionRequestRepository):
    def __init__(self, case: CodeInputCase) -> None:
        super().__init__(case.request)
        self.sources = {item.id: item for item in case.source_requests}

    def get(self, request_id, workspace_id, *, for_update=False):
        return super().get(request_id, workspace_id, for_update=for_update) or (
            self.sources.get(request_id)
            if workspace_id == self.request.workspace_id
            else None
        )

    def list_activation_candidates(self, *, status, now, limit):
        del now, limit
        return (self.request,) if self.request.status is status else ()


class MaterializationUnitOfWork(FakeUnitOfWork):
    def __init__(self, case: CodeInputCase) -> None:
        from .test_context_requests import fixed_planning_inputs

        super().__init__(case.request)
        self.execution_requests = MaterializationRequests(case)
        self.added_executions: list[Execution] = []
        self.assemblies: list[ContextAssemblyRequest] = []
        self.rolled_back = False
        repositories = case.uow()
        self.executions = SimpleNamespace(
            get=repositories.executions.get, add=self.added_executions.append
        )
        self.results = repositories.results
        self.results.get_head = lambda *_args: None
        self.plans = repositories.plans
        self.plans.get_current_for_root = lambda *_args: case.plan
        self.plans.get_head = lambda *_args, **_kwargs: SimpleNamespace(
            workspace_id=case.plan.workspace_id,
            root_task_id=case.plan.root_task_id,
            active_snapshot_id=case.plan.id,
            active_generation=case.plan.generation,
            active_snapshot_hash=case.selection.plan_content_hash,
        )
        self.release = RuntimeRelease(
            id=UUID(int=910),
            workspace_id=case.selection.workspace_id,
            run_id=case.selection.run_id,
            plan_snapshot_id=case.selection.plan_snapshot_id,
            plan_generation=case.selection.plan_generation,
            plan_content_hash=case.selection.plan_content_hash,
            decision_id=case.selection.decision_id,
            intent_id=case.selection.intent_id,
            execution_request_id=case.request.id,
            request_version_at_release=case.selection.target_request_version,
            issued_at=case.selection.created_at,
            input_selection_id=case.request.input_selection_id,
            input_selection_hash=case.request.input_selection_hash,
        )
        self.collaboration_ledger = SimpleNamespace(
            get_release_for_request=lambda *_args: self.release
        )
        self.input_selections = SimpleNamespace(get=lambda *_args: case.selection)
        self.collaboration_runs = SimpleNamespace(
            get_current=lambda *_args: SimpleNamespace(
                id=case.selection.run_id,
                workspace_id=case.selection.workspace_id,
                root_task_id=case.selection.root_task_id,
                plan_snapshot_id=case.selection.plan_snapshot_id,
                plan_generation=case.selection.plan_generation,
                plan_content_hash=case.selection.plan_content_hash,
                status=CollaborationRunStatus.ACTIVE,
            )
        )
        planning_request = SimpleNamespace(
            id=case.request.planning_request_id,
            workspace_id=case.request.workspace_id,
            current_proposal_revision=1,
            status=PlanningRequestStatus.MATERIALIZED,
        )
        self.planning_requests = SimpleNamespace(get=lambda *_args: planning_request)
        self.planning_records = SimpleNamespace(
            get_current_proposal=lambda *_args: SimpleNamespace(
                id=case.request.proposal_revision_id
            )
        )
        task = Task.create(
            task_id=case.request.subtask_id,
            workspace_id=case.request.workspace_id,
            title="Code consumer",
            description="Consume fixed code",
            acceptance_criteria=("done",),
            priority=1,
            requested_by="test",
        )
        task.mark_ready()
        self.tasks = SimpleNamespace(get=lambda *_args, **_kwargs: task)
        self.worker = WorkerItem.create(
            item_id=case.request.worker_item_id,
            workspace_id=case.request.workspace_id,
            root_task_id=case.request.root_task_id,
            title="Code consumer",
            responsibility="Consume fixed code",
            now=case.selection.created_at,
            stable_key=case.request.worker_item_key,
        )
        self.worker_items = SimpleNamespace(
            get=lambda *_args, **_kwargs: self.worker,
            get_current_placement=lambda *_args: SimpleNamespace(
                plan_generation=case.plan.generation
            ),
        )
        _, _, revision, _ = fixed_planning_inputs()
        self.revision = replace(revision, workspace_id=case.request.workspace_id)
        work_context = SimpleNamespace(current_revision_id=self.revision.revision_id)
        self.work_contexts = SimpleNamespace(
            get_by_planning_request=lambda *_args: work_context,
            get_by_root_task=lambda *_args: work_context,
            get_revision=lambda *_args: self.revision,
        )
        self.context_assemblies = SimpleNamespace(
            add=self.assemblies.append,
            get_for_consumer=lambda purpose, execution_id, workspace_id, *, manifest_schema_version: (
                next(
                    (
                        item
                        for item in self.assemblies
                        if item.consumer_id == execution_id
                        and item.workspace_id == workspace_id
                        and item.manifest_schema_version == manifest_schema_version
                    ),
                    None,
                )
            ),
        )

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        super().__exit__(exc_type, exc_value, traceback)
        if exc_type is not None:
            self.rolled_back = True
            self.added_executions.clear()
            self.assemblies.clear()


@pytest.mark.parametrize("hard_only", (False, True))
def test_normal_activation_saves_v3_with_fixed_code_and_result_selection(
    hard_only: bool,
) -> None:
    case = code_input_case(hard_only=hard_only)
    uow = MaterializationUnitOfWork(case)
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        owner_id="test-code-activation",
        clock=lambda: case.selection.created_at,
    )
    execution_id, deferred = reconciler._materialize_ready(
        cast(ControlPlaneUnitOfWork, uow), case.request, now=case.selection.created_at
    )
    assert execution_id is not None and not deferred
    assert len(uow.assemblies) == 1
    assembly = uow.assemblies[0]
    assert assembly.manifest_schema_version == 3
    assert assembly.input_selection_id == case.selection.id
    assert assembly.input_selection_hash == case.selection.selection_hash
    manifest = assembly.code_input_manifest
    assert manifest is not None
    assert (manifest.request_id, manifest.execution_id) == (
        case.request.id,
        execution_id,
    )
    assert (manifest.selection_id, manifest.selection_hash) == (
        case.selection.id,
        case.selection.selection_hash,
    )
    assert assembly.verification_manifest is None
    assert case.request.status is ExecutionRequestStatus.MATERIALIZED
    assert case.request.version == case.selection.target_request_version + 1
    assert (
        uow.release.request_version_at_release == case.selection.target_request_version
    )
    assert uow.execution_requests.saved[0][1] == case.selection.target_request_version
    from sidera_control_plane.application.context_requests import (
        execution_context_manifest_schema_version,
    )

    uow.execution_requests.get_effective_by_execution_lineage = lambda *_args: (
        case.request
    )
    assert (
        execution_context_manifest_schema_version(
            cast(ControlPlaneUnitOfWork, uow), uow.added_executions[0]
        )
        == 3
    )


def test_invalid_selected_code_rolls_back_activation_without_materializing() -> None:
    case = replace(code_input_case(hard_only=True), snapshots=())
    uow = MaterializationUnitOfWork(case)
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        owner_id="test-code-activation",
        clock=lambda: case.selection.created_at,
    )
    result = reconciler.run_once(limit=1)
    assert result.failed == 1
    assert result.materialized == 0
    assert result.execution_ids == ()
    assert uow.rolled_back
    assert uow.assemblies == []
    assert uow.added_executions == []
    assert case.request.status is ExecutionRequestStatus.READY
    assert case.request.version == case.selection.target_request_version
    assert case.request.first_execution_id is None
    assert uow.events.events == []


@pytest.mark.parametrize("branch", ("source", "text", "legacy_diff"))
def test_normal_activation_retains_legacy_schema_choices(branch: str) -> None:
    case = code_input_case()
    if branch == "source":
        case = replace(
            case,
            request=replace(
                case.request,
                input_selection_id=None,
                input_selection_hash=None,
                collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
            ),
        )
    elif branch == "text":
        group = case.selection.groups[0]
        source = group.sources[0]
        selection = rehash_selection(
            replace(
                case.selection,
                groups=(
                    replace(
                        group,
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
        )
        case = replace(
            case,
            selection=selection,
            request=replace(
                case.request, input_selection_hash=selection.selection_hash
            ),
        )
    else:
        case = replace(case, plan=replace(case.plan, integration_spec=None))
    uow = MaterializationUnitOfWork(case)
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow), owner_id="test-legacy"
    )
    reconciler._materialize_ready(
        cast(ControlPlaneUnitOfWork, uow), case.request, now=case.selection.created_at
    )
    assert uow.assemblies[0].manifest_schema_version == (1 if branch == "source" else 2)
    assert uow.assemblies[0].code_input_manifest is None


@pytest.mark.parametrize("branch", ("verification", "rework", "reintegration"))
def test_result_activation_preserves_existing_v3_owners(branch: str) -> None:
    from sidera_control_plane.application.result_integration import (
        ResultIntegrationService,
    )
    from sidera_control_plane.domain.result_operations import (
        ReworkMode,
        ReworkRecordV1,
        ReworkStatus,
    )

    from .result_integration_fixtures import head_fixture

    case = code_input_case()
    snapshot = case.snapshots[0]
    candidate = ResultIntegrationService._build_candidate(
        candidate_id=UUID(int=930),
        workspace_id=case.request.workspace_id,
        root_task_id=case.request.root_task_id,
        plan_snapshot_id=case.plan.id,
        plan_content_hash=case.selection.plan_content_hash,
        integration_execution_id=snapshot.execution_id,
        base_commit="a" * 40,
        source_result_ids=(snapshot.id,),
        input_selection_id=case.selection.id,
        input_selection_hash=case.selection.selection_hash,
        code_result_id=snapshot.id,
        previous_candidate_id=None,
        created_at=case.selection.created_at,
    )
    request = replace(
        case.request,
        result_derivation_kind="verification" if branch == "verification" else "rework",
        source_candidate_id=candidate.id,
        rework_record_id=None if branch == "verification" else UUID(int=931),
        purpose="reintegrate" if branch == "reintegration" else case.request.purpose,
    )
    if branch != "reintegration":
        request = replace(
            request,
            input_selection_id=None,
            input_selection_hash=None,
            collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
        )
    case = replace(case, request=request)
    uow = MaterializationUnitOfWork(case)
    records = {
        ("candidate", candidate.id): candidate,
        ("code_snapshot", snapshot.id): snapshot,
    }
    if branch == "rework":
        record = ReworkRecordV1(
            id=UUID(int=931),
            workspace_id=request.workspace_id,
            root_task_id=request.root_task_id,
            candidate_id=candidate.id,
            source_issue_keys=("login",),
            target_worker_item_ids=(request.worker_item_id,),
            impact_worker_item_ids=(request.worker_item_id,),
            reason="Repair login",
            mode=ReworkMode.USER,
            round_number=None,
            source_command_id=UUID(int=932),
            request_ids=(request.id,),
            status=ReworkStatus.REQUESTED,
            created_at=case.selection.created_at,
        )
        records[("rework", record.id)] = record
    uow.results.get_record = lambda kind, result_id, _workspace_id: records.get(
        (kind, result_id)
    )
    uow.results.get_head = lambda *_args: head_fixture(
        workspace_id=request.workspace_id,
        root_task_id=request.root_task_id,
        current_candidate_id=candidate.id,
        current_acceptance_id=None,
        result_version=1,
        active_operation_id=None,
    )
    source_request = case.source_requests[0]
    if branch == "verification":
        source_request = replace(source_request, worker_item_key="backend")
    uow.execution_requests.get_by_execution_lineage = lambda *_args: source_request
    reconciler = ActivationReconciler(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow), owner_id="test-v3-owners"
    )
    execution_id, deferred = reconciler._materialize_ready(
        cast(ControlPlaneUnitOfWork, uow), request, now=case.selection.created_at
    )
    assert execution_id is not None and not deferred
    assembly = uow.assemblies[0]
    assert assembly.manifest_schema_version == 3
    if branch == "verification":
        assert assembly.code_input_manifest is None
        assert assembly.verification_manifest is not None
        assert assembly.verification_manifest.candidate_id == candidate.id
    else:
        assert assembly.verification_manifest is None
        assert assembly.code_input_manifest is not None
        assert assembly.code_input_manifest.execution_id == execution_id
        assert assembly.code_input_manifest.selection_id == case.selection.id
