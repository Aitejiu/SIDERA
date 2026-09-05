from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.revision_materializer import (
    RevisionMaterializer,
    _actual_action,
    _head_matches_base,
    _worker_transition,
)
from sidera_control_plane.domain.collaboration import CollaborationPolicySnapshotV2
from sidera_control_plane.domain.plan_revisions import (
    RootTaskPlanHead,
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plan_transitions import (
    WorkerTransitionStatus,
    WriterState,
)
from sidera_control_plane.domain.planning_proposals import ProposalRevision
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV5,
)
from sidera_control_plane.domain.revision_diff import (
    RevisionDiffNormalizer,
    WorkerTransitionPreview,
)
from sidera_control_plane.domain.worker_item import WorkerItem
from tests.unit.result_input_fixtures import revision_v5_mapping
from tests.unit.test_collaboration_policy_v2 import runtime_binding
from tests.unit.test_revision_diff import base_snapshot, revision_payload

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
WORKER_ID = UUID("40000000-0000-0000-0000-000000000001")


@pytest.mark.parametrize(
    ("worker_item_id", "allowed_actions", "expected"),
    (
        (None, (WorkerTransitionAction.CREATE,), WorkerTransitionAction.CREATE),
        (
            WORKER_ID,
            (WorkerTransitionAction.PATCH, WorkerTransitionAction.CONTINUE),
            WorkerTransitionAction.PATCH,
        ),
        (WORKER_ID, (WorkerTransitionAction.MOVE,), WorkerTransitionAction.MOVE),
        (
            WORKER_ID,
            (WorkerTransitionAction.PRESERVE,),
            WorkerTransitionAction.PRESERVE,
        ),
    ),
)
def test_actual_action_uses_schema_v3_transition_policy(
    worker_item_id: UUID | None,
    allowed_actions: tuple[WorkerTransitionAction, ...],
    expected: WorkerTransitionAction,
) -> None:
    preview = WorkerTransitionPreview(
        target_worker_item_key="backend",
        worker_item_id=worker_item_id,
        allowed_actions=allowed_actions,
        fallback_action=None,
        reason_codes=(),
    )

    assert _actual_action(preview) is expected


def test_head_match_requires_snapshot_identity_generation_and_hash() -> None:
    snapshot = base_snapshot()
    payload = revision_payload(snapshot)
    head = RootTaskPlanHead(
        workspace_id=snapshot.workspace_id,
        root_task_id=snapshot.root_task_id,
        active_snapshot_id=snapshot.id,
        active_generation=snapshot.generation,
        active_snapshot_hash=plan_snapshot_content_hash(snapshot),
        version=1,
        updated_at=NOW,
    )

    assert _head_matches_base(head, payload, snapshot)
    head.active_generation += 1
    assert not _head_matches_base(head, payload, snapshot)


def test_worker_transition_resolves_from_subtask_key_and_keeps_fence_policy() -> None:
    snapshot = base_snapshot()
    payload = revision_payload(snapshot)
    diff = RevisionDiffNormalizer().normalize(snapshot, payload)
    worker = WorkerItem.create(
        item_id=WORKER_ID,
        workspace_id=snapshot.workspace_id,
        root_task_id=snapshot.root_task_id,
        title="Backend worker",
        responsibility="Implement the current behavior.",
        now=NOW,
    )

    transitions = RevisionMaterializer._worker_transitions(
        cast(ControlPlaneUnitOfWork, None),
        payload=payload,
        diff=diff,
        base_snapshot=snapshot,
        target_snapshot=snapshot,
        target_workers={"backend": worker},
        source_workers={WORKER_ID: worker},
        source_executions={},
        plan_transition_id=uuid4(),
        now=NOW,
    )

    assert len(transitions) == 1
    transition = transitions[0]
    assert transition.action is WorkerTransitionAction.PATCH
    assert transition.status is WorkerTransitionStatus.PENDING
    assert transition.writer_state is WriterState.FENCED
    assert transition.from_subtask_id == snapshot.subtasks[0].task_id


def test_converged_worker_transition_closes_writer() -> None:
    transition = _worker_transition(
        plan_transition_id=uuid4(),
        workspace_id=UUID("20000000-0000-0000-0000-000000000001"),
        root_task_id=UUID("30000000-0000-0000-0000-000000000001"),
        worker_item_id=WORKER_ID,
        from_subtask_id=None,
        to_subtask_id=None,
        action=WorkerTransitionAction.PRESERVE,
        fallback_action=None,
        target_context_version=1,
        source_execution=None,
        status=WorkerTransitionStatus.CONVERGED,
        writer_state=WriterState.CLOSED,
        now=NOW,
    )

    assert transition.can_release_fence


def test_v5_target_snapshot_publishes_schema4_and_keeps_input_groups() -> None:
    base = base_snapshot()
    raw = revision_v5_mapping()
    descriptor = raw["base_descriptor"]
    assert isinstance(descriptor, dict)
    descriptor.update(
        {
            "workspace_id": str(base.workspace_id),
            "root_task_id": str(base.root_task_id),
            "plan_snapshot_id": str(base.id),
            "plan_generation": base.generation,
            "plan_content_hash": plan_snapshot_content_hash(base),
        }
    )
    payload = PlanningRevisionProposalPayloadV5.from_mapping(raw)
    policy = CollaborationPolicySnapshotV2.from_request(
        payload.collaboration_policy_request, runtime_binding()
    )
    proposal = cast(
        ProposalRevision,
        SimpleNamespace(
            id=uuid4(),
            revision=1,
            content_hash="proposal-hash",
            payload=payload,
        ),
    )
    subtask_id = base.subtasks[0].task_id
    assert subtask_id is not None

    snapshot = RevisionMaterializer._target_snapshot(
        request=cast(
            Any,
            SimpleNamespace(
                id=uuid4(),
                workspace_id=base.workspace_id,
            ),
        ),
        proposal=proposal,
        payload=payload.as_v3(),
        subtask_ids={"implementation": subtask_id},
        worker_ids={
            "a": WORKER_ID,
            "b": UUID("40000000-0000-0000-0000-000000000002"),
            "c": UUID("40000000-0000-0000-0000-000000000003"),
            "summary": UUID("40000000-0000-0000-0000-000000000004"),
        },
        published_by="operator",
        now=NOW,
        collaboration_policy=policy,
    )

    assert snapshot.schema_version == 4
    assert isinstance(snapshot.collaboration_policy, CollaborationPolicySnapshotV2)
    assert snapshot.collaboration_policy.input_groups[0].group_key == "research"
