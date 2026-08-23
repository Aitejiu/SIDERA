from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

from sidera_control_plane.application.activation_reconciler import (
    ActivationReconciler,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.artifacts import Artifact, ArtifactType, EvidenceClaim
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ActivationDecision,
    ExecutionRequest,
    ExecutionRequestDependency,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.planning import PlanningRequestStatus

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
