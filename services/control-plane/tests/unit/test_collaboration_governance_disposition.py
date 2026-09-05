from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sidera_control_plane.application.collaboration_governance_disposition import (
    CollaborationGovernanceDispositionService,
)
from sidera_control_plane.application.collaboration_release_validation import (
    CollaborationReleaseEligibilityValidator,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    RuntimeIntent,
    RuntimeIntentApplicationStatus,
)
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationGovernanceHold,
    CollaborationReleaseProposal,
    GovernanceTransport,
    ReleaseProposalStatus,
)
from sidera_control_plane.domain.collaboration_runtime import RuntimeProjectionV1
from sidera_control_plane.domain.execution_requests import ExecutionRequestStatus

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
ROOT_TASK_ID = uuid4()
RUN_ID = uuid4()
PLAN_SNAPSHOT_ID = uuid4()
TARGET_REQUEST_ID = uuid4()
WORKER_ITEM_ID = uuid4()


def projection() -> RuntimeProjectionV1:
    return RuntimeProjectionV1(
        schema_version=1,
        projection_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        run_id=RUN_ID,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=1,
        plan_content_hash="a" * 64,
        run_resource_version=1,
        fact_cursor=1,
        policy={"mode": "enabled", "default_policy": {"enabled": True, "strategy_id": "all_required"}},
        tasks=(),
        subtasks=(),
        worker_items=({"id": str(WORKER_ITEM_ID), "key": "build.code"},),
        dependencies=(),
        execution_requests=({
            "id": str(TARGET_REQUEST_ID),
            "worker_item_id": str(WORKER_ITEM_ID),
            "worker_item_key": "build.code",
            "subtask_key": "build",
            "version": 1,
            "status": "ready",
            "collaboration_gate_mode": "release_required",
            "gate_reason": "eligible",
            "eligible": True,
            "expected_artifact_refs": [],
            "expected_evidence_refs": [],
        },),
        executions=(),
        artifacts=(),
        evidence=(),
        releases=(),
        previous_strategy_state={},
        fingerprints={},
        created_at=NOW,
    )


def intent() -> RuntimeIntent:
    return RuntimeIntent(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        decision_id=uuid4(),
        ordinal=0,
        intent_type="release_execution_request",
        target_execution_request_id=TARGET_REQUEST_ID,
        target_request_version=1,
        reason_code="eligible",
        summary="release",
        idempotency_key="release:test",
        payload_json={},
        created_at=NOW,
    )


def request() -> SimpleNamespace:
    return SimpleNamespace(
        id=TARGET_REQUEST_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=1,
        worker_item_id=WORKER_ITEM_ID,
        version=1,
        status=ExecutionRequestStatus.READY,
        collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED,
    )


def run() -> SimpleNamespace:
    return SimpleNamespace(
        id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=1,
        plan_content_hash="a" * 64,
        status="active",
    )


def hold() -> CollaborationGovernanceHold:
    return CollaborationGovernanceHold.create(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        run_id=RUN_ID,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=1,
        plan_content_hash="a" * 64,
        worker_item_id=WORKER_ITEM_ID,
        created_by_actor_id="developer",
        created_via=GovernanceTransport.WEB,
        reason="review",
        now=NOW,
    )


class FakeGovernanceRepository:
    def __init__(self, active_hold: CollaborationGovernanceHold | None = None) -> None:
        self.active_hold = active_hold
        self.proposals: list[CollaborationReleaseProposal] = []

    def get_active_holds(self, workspace_id, run_id, worker_item_ids, *, for_update=False):
        if self.active_hold is not None and self.active_hold.worker_item_id in worker_item_ids:
            return (self.active_hold,)
        return ()

    def find_matching_proposal(self, workspace_id, hold_id, execution_request_id, target_request_version, fingerprint_version, fingerprint, *, for_update=False):
        return next((item for item in self.proposals if item.input_fingerprint == fingerprint), None)

    def list_pending_proposals_for_hold(self, workspace_id, hold_id, *, for_update=False):
        return tuple(
            item
            for item in self.proposals
            if item.hold_id == hold_id
            and item.status is ReleaseProposalStatus.PENDING
        )

    def add_proposal(self, proposal):
        self.proposals.append(proposal)


def test_validator_accepts_an_eligible_request_without_governance_knowledge() -> None:
    assessment = CollaborationReleaseEligibilityValidator().assess(
        intent(),
        request=request(),
        run=run(),
        plan_head=None,
        current_projection=projection(),
        existing_release=None,
    )
    assert assessment.status is RuntimeIntentApplicationStatus.APPLIED


def test_valid_eligible_intent_is_deferred_and_creates_one_proposal_when_held() -> None:
    repository = FakeGovernanceRepository(hold())
    result = CollaborationGovernanceDispositionService().dispose(
        uow=SimpleNamespace(collaboration_governance=repository),
        intents=(intent(),),
        assessments=(CollaborationReleaseEligibilityValidator().assess(
            intent(), request=request(), run=run(), plan_head=None,
            current_projection=projection(), existing_release=None,
        ),),
        run=run(),
        current_projection=projection(),
        now=NOW,
    )
    assert result.assessments[0].status is RuntimeIntentApplicationStatus.DEFERRED
    assert len(result.proposals) == 1
    assert result.proposals[0].status is ReleaseProposalStatus.PENDING


def test_same_fingerprint_reuses_proposal_and_marks_new_application_duplicate() -> None:
    repository = FakeGovernanceRepository(hold())
    service = CollaborationGovernanceDispositionService()
    first_intent = intent()
    first = service.dispose(
        uow=SimpleNamespace(collaboration_governance=repository),
        intents=(first_intent,),
        assessments=(CollaborationReleaseEligibilityValidator().assess(
            first_intent, request=request(), run=run(), plan_head=None,
            current_projection=projection(), existing_release=None,
        ),),
        run=run(), current_projection=projection(), now=NOW,
    )
    second_intent = intent()
    second = service.dispose(
        uow=SimpleNamespace(collaboration_governance=repository),
        intents=(second_intent,),
        assessments=(CollaborationReleaseEligibilityValidator().assess(
            second_intent, request=request(), run=run(), plan_head=None,
            current_projection=projection(), existing_release=None,
        ),),
        run=run(), current_projection=projection(), now=NOW,
    )
    assert len(first.proposals) == 1
    assert second.proposals == ()
    assert second.assessments[0].status is RuntimeIntentApplicationStatus.DUPLICATE
