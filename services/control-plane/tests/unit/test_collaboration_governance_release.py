from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandError,
)
from sidera_control_plane.application.collaboration_governance_fingerprint import (
    build_release_proposal_fingerprint,
)
from sidera_control_plane.application.collaboration_governance_release import (
    CollaborationGovernanceReleaseApplier,
)
from sidera_control_plane.application.collaboration_projection import (
    CollaborationProjectionAssembler,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    CollaborationRunStatus,
    RuntimeDecision,
    RuntimeIntent,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
)
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationReleaseProposal,
    GovernanceResolutionResult,
    GovernanceTransport,
)
from sidera_control_plane.domain.collaboration_runtime import RuntimeProjectionV1
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.execution_requests import ExecutionRequest

from .test_collaboration_governance_commands import (
    INTENT_ID,
    PROPOSAL_ID,
    REQUEST_ID,
    WORKSPACE_ID,
    FakeExecutionRequests,
    FakeGovernance,
    FakeLedger,
    FakeProjectionChanges,
    FakeUnitOfWork,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class SavingExecutionRequests(FakeExecutionRequests):
    def save(self, request, previous_version):
        assert request.version == previous_version + 1
        self.request = request


class ApprovalLedger(FakeLedger):
    def __init__(self, application, decision, intent, projection):
        super().__init__(application)
        self.decision = decision
        self.intent = intent
        self.projection = projection
        self.releases = []
        self.release_calls = []

    def get_decision(self, workspace_id, decision_id, *, for_update=False):
        if (
            self.decision is not None
            and workspace_id == WORKSPACE_ID
            and decision_id == self.decision.id
        ):
            return self.decision
        return None

    def get_intent(self, workspace_id, intent_id, *, for_update=False):
        if (
            self.intent is not None
            and workspace_id == WORKSPACE_ID
            and intent_id == self.intent.id
        ):
            return self.intent
        return None

    def get_projection(self, workspace_id, projection_id):
        if (
            self.projection is None
            or workspace_id != WORKSPACE_ID
            or projection_id != self.projection.projection_id
        ):
            return None
        return SimpleNamespace(
            id=self.projection.projection_id,
            workspace_id=self.projection.workspace_id,
            run_id=self.projection.run_id,
            payload=self.projection.to_mapping(),
            content_hash=self.projection.content_hash,
            created_at=self.projection.created_at,
        )

    def get_release_for_request(self, workspace_id, request_id, *, for_update=False):
        self.release_calls.append(for_update)
        return next(
            (
                release
                for release in self.releases
                if release.workspace_id == workspace_id
                and release.execution_request_id == request_id
            ),
            None,
        )

    def add_releases(self, releases):
        self.releases.extend(releases)


class MutatingLockedHoldGovernance(FakeGovernance):
    def get_hold(self, workspace_id, hold_id, *, for_update=False):
        hold = super().get_hold(workspace_id, hold_id, for_update=for_update)
        if for_update and hold is not None:
            return replace(hold, plan_snapshot_id=uuid4())
        return hold


class FakeFactStore:
    def load(self, workspace_id, run, *, fact_cursor) -> Any:
        return SimpleNamespace(
            plan_head=SimpleNamespace(run_resource_version=run.resource_version)
        )


class StaticProjectionAssembler(CollaborationProjectionAssembler):
    def __init__(self, projection: RuntimeProjectionV1) -> None:
        self.projection = projection

    def assemble(self, *args, **kwargs):
        return self.projection


class SavingProjectionChanges(FakeProjectionChanges):
    def latest_target_cursor(self, workspace_id, projection_type, resource_id):
        return 1


def _request(uow: FakeUnitOfWork) -> ExecutionRequest:
    return ExecutionRequest.create(
        request_id=REQUEST_ID,
        workspace_id=WORKSPACE_ID,
        planning_request_id=uuid4(),
        proposal_revision_id=uuid4(),
        plan_snapshot_id=uow.head.active_snapshot_id,
        plan_generation=uow.head.active_generation,
        root_task_id=uow.head.root_task_id,
        subtask_id=uow.placement.subtask_task_id or uuid4(),
        worker_item_id=uow.item.id,
        worker_item_key="build.api",
        intent_key="release",
        purpose="run the build worker",
        capability_requirements=("code",),
        adapter_preferences=(),
        budget_class="standard",
        not_before=NOW,
        max_attempts=3,
        expected_artifact_refs=(),
        expected_evidence_refs=(),
        collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED,
        now=NOW,
    )


def _projection(uow: FakeUnitOfWork, request: ExecutionRequest) -> RuntimeProjectionV1:
    return RuntimeProjectionV1(
        schema_version=1,
        projection_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=uow.head.root_task_id,
        run_id=uow.run.id,
        plan_snapshot_id=uow.head.active_snapshot_id,
        plan_generation=uow.head.active_generation,
        plan_content_hash=uow.head.active_snapshot_hash,
        run_resource_version=uow.run.resource_version,
        fact_cursor=1,
        policy={"default_policy": {"enabled": True, "strategy_id": "all_required"}},
        tasks=(),
        subtasks=(),
        worker_items=({"id": str(request.worker_item_id), "key": "build.api"},),
        dependencies=(),
        execution_requests=(
            {
                "id": str(request.id),
                "worker_item_id": str(request.worker_item_id),
                "worker_item_key": request.worker_item_key,
                "subtask_key": "build",
                "status": request.status.value,
                "collaboration_gate_mode": request.collaboration_gate_mode.value,
                "expected_artifact_refs": [],
                "expected_evidence_refs": [],
                "eligible": True,
            },
        ),
        executions=(),
        artifacts=(),
        evidence=(),
        releases=(),
        previous_strategy_state={},
        fingerprints={},
        created_at=NOW,
    )


def _arrange(current_projection: RuntimeProjectionV1 | None = None):
    uow = FakeUnitOfWork()
    uow.run.resource_version = 1
    request = _request(uow)
    uow.execution_requests = SavingExecutionRequests(cast(SimpleNamespace, request))
    projection = current_projection or _projection(uow, request)
    decision = RuntimeDecision(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        run_id=uow.run.id,
        evaluation_id=uuid4(),
        projection_snapshot_id=projection.projection_id,
        projection_hash=projection.content_hash,
        operation_id=uuid4(),
        schema_version=1,
        sequence=1,
        outcome="intents",
        reason_code="test",
        summary="test decision",
        strategy_state={},
        content_hash="b" * 64,
        runtime_duration_ms=1,
        created_at=NOW,
    )
    intent = RuntimeIntent(
        id=INTENT_ID,
        workspace_id=WORKSPACE_ID,
        run_id=uow.run.id,
        decision_id=decision.id,
        ordinal=0,
        intent_type="release_execution_request",
        target_execution_request_id=request.id,
        target_request_version=request.version,
        reason_code="test_release",
        summary="release request",
        idempotency_key="test-release",
        payload_json={"target_execution_request_id": str(request.id)},
        created_at=NOW,
    )
    proposal = CollaborationReleaseProposal.create(
        id=PROPOSAL_ID,
        workspace_id=WORKSPACE_ID,
        hold_id=uow.hold.id,
        run_id=uow.run.id,
        projection_snapshot_id=projection.projection_id,
        projection_hash=projection.content_hash,
        decision_id=decision.id,
        intent_id=intent.id,
        execution_request_id=request.id,
        target_request_version=request.version,
        input_fingerprint_version=1,
        input_fingerprint="0" * 64,
        reason_code="test_release",
        summary="release request",
        now=NOW,
    )
    fingerprint = build_release_proposal_fingerprint(
        projection, intent, worker_item_id=request.worker_item_id
    )
    proposal = replace(proposal, input_fingerprint=fingerprint.value)
    application = RuntimeIntentApplication(
        id=uuid5(NAMESPACE_URL, f"sidera:collaboration-application:{intent.id}"),
        workspace_id=WORKSPACE_ID,
        intent_id=intent.id,
        status=RuntimeIntentApplicationStatus.DEFERRED,
        expected_run_version=1,
        observed_run_version=1,
        expected_request_version=request.version,
        observed_request_version=request.version,
        release_id=None,
        result_code="governance_hold_active",
        result_summary="waiting for approval",
        resource_version=1,
        created_at=NOW,
        applied_at=None,
    )
    uow.governance = FakeGovernance(uow.hold, proposal)
    uow.collaboration_governance = uow.governance
    uow.collaboration_ledger = ApprovalLedger(application, decision, intent, projection)
    uow.projection_changes = SavingProjectionChanges()
    return uow, proposal, projection, intent


def _command(proposal_id: UUID) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        command_type="ApproveCollaborationReleaseProposal",
        target_type="collaboration_release_proposal",
        actor_type="local",
        actor_id="aitejiu",
        target_id=proposal_id,
        payload={"transport": GovernanceTransport.WEB.value},
        expected_version=1,
        idempotency_key=str(uuid4()),
        correlation_id=uuid4(),
    )


def test_approval_applies_original_lineage_and_refreshes_gate() -> None:
    uow, proposal, projection, _intent = _arrange()
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    result = applier.approve_in_transaction(
        cast(Any, uow), _command(proposal.id), now=NOW
    )

    ledger = cast(ApprovalLedger, uow.collaboration_ledger)
    assert result.data["result"] == GovernanceResolutionResult.APPLIED.value
    assert len(ledger.releases) == 1
    assert (
        ledger.application is not None
        and ledger.application.status is RuntimeIntentApplicationStatus.APPLIED
    )
    assert uow.execution_requests.request.status.value == "ready"
    assert uow.governance.proposals[proposal.id].status.value == "approved"
    assert uow.governance.resolutions[0].result is GovernanceResolutionResult.APPLIED
    assert ledger.release_calls == [True]


def test_changed_target_fingerprint_stales_without_a_release() -> None:
    uow, proposal, projection, _intent = _arrange()
    changed_projection = replace(
        projection,
        execution_requests=(
            {
                **projection.execution_requests[0],
                "expected_evidence_refs": ["acceptance:changed"],
            },
        ),
    )
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(changed_projection),
    )

    result = applier.approve_in_transaction(
        cast(Any, uow), _command(proposal.id), now=NOW
    )

    ledger = cast(ApprovalLedger, uow.collaboration_ledger)
    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert ledger.releases == []
    assert (
        ledger.application is not None
        and ledger.application.status is RuntimeIntentApplicationStatus.STALE
    )
    assert uow.governance.proposals[proposal.id].status.value == "stale"
    assert uow.governance.resolutions[0].result is GovernanceResolutionResult.STALE
    assert any(
        change["projection_type"] == "collaboration_runtime"
        for change in uow.projection_changes.changes
    )


def test_generation_change_stales_without_runtime_wakeup() -> None:
    uow, proposal, projection, _intent = _arrange()
    uow.head.active_generation += 1
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    result = applier.approve_in_transaction(
        cast(Any, uow), _command(proposal.id), now=NOW
    )

    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert result.data["stale_reason"] == "plan_generation_stale"
    assert not any(
        change["projection_type"] == "collaboration_runtime"
        for change in uow.projection_changes.changes
    )


def test_non_active_run_stales_without_runtime_wakeup() -> None:
    uow, proposal, projection, _intent = _arrange()
    uow.run.status = CollaborationRunStatus.SUPERSEDED
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    result = applier.approve_in_transaction(
        cast(Any, uow), _command(proposal.id), now=NOW
    )

    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert result.data["stale_reason"] == "collaboration_run_not_active"
    assert not any(
        change["projection_type"] == "collaboration_runtime"
        for change in uow.projection_changes.changes
    )


def test_static_eligibility_regression_stales_without_runtime_wakeup() -> None:
    uow, proposal, projection, _intent = _arrange()
    ineligible_projection = replace(
        projection,
        execution_requests=(
            {
                **projection.execution_requests[0],
                "eligible": False,
            },
        ),
    )
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(ineligible_projection),
    )

    result = applier.approve_in_transaction(
        cast(Any, uow), _command(proposal.id), now=NOW
    )

    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert result.data["stale_reason"] == "join_condition_unsatisfied"
    assert not any(
        change["projection_type"] == "collaboration_runtime"
        for change in uow.projection_changes.changes
    )


def test_request_version_change_stales_and_wakes_current_runtime() -> None:
    uow, proposal, projection, _intent = _arrange()
    uow.execution_requests.request.version = 2
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    result = applier.approve_in_transaction(
        cast(Any, uow), _command(proposal.id), now=NOW
    )

    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert result.data["stale_reason"] == "projection_stale"
    assert (
        sum(
            change["projection_type"] == "collaboration_runtime"
            for change in uow.projection_changes.changes
        )
        == 1
    )


def test_moved_request_stales_without_runtime_wakeup() -> None:
    uow, proposal, projection, _intent = _arrange()
    uow.execution_requests.request.worker_item_id = uuid4()
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    result = applier.approve_in_transaction(
        cast(Any, uow), _command(proposal.id), now=NOW
    )

    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert result.data["stale_reason"] == "intent_target_invalid"
    assert not any(
        change["projection_type"] == "collaboration_runtime"
        for change in uow.projection_changes.changes
    )


def test_run_and_hold_identity_mismatch_is_rejected_without_resolution() -> None:
    uow, proposal, projection, _intent = _arrange()
    uow.run.root_task_id = uuid4()
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    with pytest.raises(CollaborationGovernanceCommandError) as caught:
        applier.approve_in_transaction(cast(Any, uow), _command(proposal.id), now=NOW)

    assert caught.value.code == "collaboration_governance_lineage_invalid"
    assert uow.governance.resolutions == []


def test_application_request_fence_mismatch_is_rejected() -> None:
    uow, proposal, projection, _intent = _arrange()
    ledger = cast(ApprovalLedger, uow.collaboration_ledger)
    assert ledger.application is not None
    ledger.application = replace(ledger.application, expected_request_version=2)
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    with pytest.raises(CollaborationGovernanceCommandError) as caught:
        applier.approve_in_transaction(cast(Any, uow), _command(proposal.id), now=NOW)

    assert caught.value.code == "collaboration_governance_lineage_invalid"


def test_proposal_state_and_expected_version_are_hard_fences() -> None:
    uow, proposal, projection, _intent = _arrange()
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    with pytest.raises(VersionConflict):
        applier.approve_in_transaction(
            cast(Any, uow), replace(_command(proposal.id), expected_version=2), now=NOW
        )

    approved = proposal.approve(now=NOW)
    uow.governance.proposals[proposal.id] = approved
    with pytest.raises(CollaborationGovernanceCommandError) as caught:
        applier.approve_in_transaction(
            cast(Any, uow), replace(_command(proposal.id), expected_version=2), now=NOW
        )
    assert caught.value.code == "collaboration_governance_proposal_not_pending"


def test_inactive_hold_is_a_hard_fence() -> None:
    uow, proposal, projection, _intent = _arrange()
    resumed = uow.hold.resume(now=NOW)
    uow.governance = FakeGovernance(resumed, proposal)
    uow.collaboration_governance = uow.governance
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    with pytest.raises(CollaborationGovernanceCommandError) as caught:
        applier.approve_in_transaction(cast(Any, uow), _command(proposal.id), now=NOW)

    assert caught.value.code == "collaboration_governance_hold_not_active"
    assert uow.governance.resolutions == []


def test_locked_hold_identity_mismatch_fails_closed() -> None:
    uow, proposal, projection, _intent = _arrange()
    uow.governance = MutatingLockedHoldGovernance(uow.hold, proposal)
    uow.collaboration_governance = uow.governance
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    with pytest.raises(CollaborationGovernanceCommandError) as caught:
        applier.approve_in_transaction(cast(Any, uow), _command(proposal.id), now=NOW)

    assert caught.value.code == "collaboration_governance_lineage_invalid"
    assert uow.governance.resolutions == []


@pytest.mark.parametrize("missing", ("decision", "intent", "projection"))
def test_missing_immutable_lineage_fails_closed(missing: str) -> None:
    uow, proposal, projection, _intent = _arrange()
    ledger = cast(ApprovalLedger, uow.collaboration_ledger)
    setattr(ledger, missing, None)
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    with pytest.raises(CollaborationGovernanceCommandError) as caught:
        applier.approve_in_transaction(cast(Any, uow), _command(proposal.id), now=NOW)

    assert caught.value.code == "collaboration_governance_lineage_invalid"


def test_non_deferred_application_is_a_lineage_error() -> None:
    uow, proposal, projection, _intent = _arrange()
    ledger = cast(ApprovalLedger, uow.collaboration_ledger)
    assert ledger.application is not None
    ledger.application = replace(
        ledger.application,
        status=RuntimeIntentApplicationStatus.APPLIED,
        release_id=uuid4(),
        applied_at=NOW,
    )
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    with pytest.raises(CollaborationGovernanceCommandError) as caught:
        applier.approve_in_transaction(cast(Any, uow), _command(proposal.id), now=NOW)

    assert caught.value.code == "collaboration_governance_lineage_invalid"


@pytest.mark.parametrize(
    "status",
    (
        "materialized",
        "cancelled",
        "obsolete",
    ),
)
def test_closed_request_stales_without_runtime_wakeup(status: str) -> None:
    uow, proposal, projection, _intent = _arrange()
    uow.execution_requests.request.status = status
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda _uow: FakeFactStore(),
        projection_assembler=StaticProjectionAssembler(projection),
    )

    result = applier.approve_in_transaction(
        cast(Any, uow), _command(proposal.id), now=NOW
    )

    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert result.data["stale_reason"] == "intent_target_invalid"
    assert not any(
        change["projection_type"] == "collaboration_runtime"
        for change in uow.projection_changes.changes
    )
