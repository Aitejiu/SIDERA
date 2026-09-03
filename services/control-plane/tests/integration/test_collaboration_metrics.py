from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.collaboration_metrics import (
    CollaborationMetricSample,
)
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    CollaborationGovernanceResolutionModel,
    CollaborationReleaseProposalModel,
    CommandModel,
)
from sidera_control_plane.persistence.runtime_metrics_store import (
    SqlAlchemyRuntimeMetricsStore,
)

pytestmark = pytest.mark.integration


def test_collaboration_metrics_are_bounded_and_use_only_allowlisted_labels(
    database, workspace_id
) -> None:
    view = SqlAlchemyRuntimeMetricsStore(database).get_collaboration_metrics(
        workspace_id
    )

    assert view.workspace_id == workspace_id
    assert view.samples
    assert all(sample.labels == tuple(sorted(sample.labels)) for sample in view.samples)
    assert all(
        set(dict(sample.labels))
        <= {
            "outcome",
            "status",
            "reason",
            "command",
            "result",
            "action",
            "reason_code",
            "operation",
        }
        for sample in view.samples
    )
    assert all(
        workspace_id.__str__() not in str(dict(sample.labels))
        for sample in view.samples
    )
    assert {sample.name for sample in view.samples} >= {
        "sidera_collaboration_evaluations_total",
        "sidera_collaboration_runs",
        "sidera_collaboration_decisions_total",
        "sidera_collaboration_releases_total",
        "sidera_collaboration_governance_active_holds",
        "sidera_collaboration_governance_pending_proposals",
        "sidera_collaboration_governance_commands_total",
        "sidera_collaboration_governance_resolution_total",
        "sidera_collaboration_governance_proposal_age_seconds",
        "sidera_collaboration_governance_stale_total",
        "sidera_collaboration_governance_conflicts_total",
    }
    samples = {
        (sample.name, tuple(sample.labels)): sample.value for sample in view.samples
    }
    assert (
        samples[("sidera_collaboration_intents_total", (("outcome", "deferred"),))] == 0
    )


def test_governance_metrics_aggregate_ledger_without_high_cardinality_labels(
    database, workspace_id, clean_database
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    run_id = uuid4()
    root_task_id = uuid4()
    plan_snapshot_id = uuid4()
    worker_item_id = uuid4()
    active_hold_id = uuid4()
    pending_proposal_id = uuid4()
    stale_proposal_id = uuid4()
    approve_command_id = uuid4()
    reject_command_id = uuid4()
    resume_command_id = uuid4()

    with database.session() as session:
        session.add_all(
            (
                CollaborationGovernanceHoldModel(
                    id=active_hold_id,
                    workspace_id=workspace_id,
                    root_task_id=root_task_id,
                    run_id=run_id,
                    plan_snapshot_id=plan_snapshot_id,
                    plan_generation=1,
                    plan_content_hash="0" * 64,
                    worker_item_id=worker_item_id,
                    status="active",
                    created_by_actor_id="operator",
                    created_via="web",
                    reason="private hold reason must not become a label",
                    resource_version=1,
                    created_at=now - timedelta(days=1),
                    resolved_at=None,
                ),
                CollaborationReleaseProposalModel(
                    id=pending_proposal_id,
                    workspace_id=workspace_id,
                    hold_id=active_hold_id,
                    run_id=run_id,
                    projection_snapshot_id=uuid4(),
                    projection_hash="1" * 64,
                    decision_id=uuid4(),
                    intent_id=uuid4(),
                    execution_request_id=uuid4(),
                    target_request_version=1,
                    input_fingerprint_version=1,
                    input_fingerprint="2" * 64,
                    status="pending",
                    reason_code="release_required",
                    summary="private proposal summary must not become a label",
                    resource_version=1,
                    created_at=now - timedelta(hours=2),
                    resolved_at=None,
                ),
                CollaborationReleaseProposalModel(
                    id=stale_proposal_id,
                    workspace_id=workspace_id,
                    hold_id=active_hold_id,
                    run_id=run_id,
                    projection_snapshot_id=uuid4(),
                    projection_hash="3" * 64,
                    decision_id=uuid4(),
                    intent_id=uuid4(),
                    execution_request_id=uuid4(),
                    target_request_version=1,
                    input_fingerprint_version=1,
                    input_fingerprint="4" * 64,
                    status="stale",
                    reason_code="user supplied stale explanation",
                    summary="another private proposal summary",
                    resource_version=2,
                    created_at=now - timedelta(days=2),
                    resolved_at=now - timedelta(days=1),
                ),
                CollaborationGovernanceResolutionModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    proposal_id=stale_proposal_id,
                    action="approve",
                    result="stale",
                    actor_id="operator",
                    transport="web",
                    reason="private resolution reason",
                    command_id=approve_command_id,
                    created_at=now - timedelta(hours=1),
                ),
                CollaborationGovernanceResolutionModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    proposal_id=uuid4(),
                    action="reject",
                    result="applied",
                    actor_id="operator",
                    transport="web",
                    reason=None,
                    command_id=reject_command_id,
                    created_at=now - timedelta(minutes=30),
                ),
            )
        )
        session.add_all(
            (
                _command(
                    approve_command_id,
                    workspace_id,
                    "ApproveCollaborationReleaseProposal",
                    status="applied",
                ),
                _command(
                    reject_command_id,
                    workspace_id,
                    "RejectCollaborationReleaseProposal",
                    status="rejected",
                    rejection_reason="version_conflict",
                ),
                _command(
                    resume_command_id,
                    workspace_id,
                    "ResumeAutomaticCollaboration",
                    status="rejected",
                    rejection_reason="collaboration_governance_target_invalid",
                ),
            )
        )
        session.commit()

    view = SqlAlchemyRuntimeMetricsStore(database).get_collaboration_metrics(
        workspace_id
    )
    samples = {
        (sample.name, tuple(sample.labels)): sample.value for sample in view.samples
    }

    assert samples[("sidera_collaboration_governance_active_holds", ())] == 1
    assert samples[("sidera_collaboration_governance_pending_proposals", ())] == 1
    assert (
        samples[
            (
                "sidera_collaboration_governance_commands_total",
                (("command", "approve"), ("result", "stale")),
            )
        ]
        == 1
    )
    assert (
        samples[
            (
                "sidera_collaboration_governance_commands_total",
                (("command", "reject"), ("result", "conflict")),
            )
        ]
        == 1
    )
    assert (
        samples[
            (
                "sidera_collaboration_governance_commands_total",
                (("command", "resume_automatic"), ("result", "rejected")),
            )
        ]
        == 1
    )
    assert (
        samples[
            (
                "sidera_collaboration_governance_resolution_total",
                (("action", "approve"), ("result", "stale")),
            )
        ]
        == 1
    )
    assert (
        samples[
            (
                "sidera_collaboration_governance_stale_total",
                (("reason_code", "unknown"),),
            )
        ]
        == 1
    )
    assert (
        samples[
            (
                "sidera_collaboration_governance_conflicts_total",
                (("operation", "approve"),),
            )
        ]
        == 1
    )
    assert (
        samples[
            (
                "sidera_collaboration_governance_conflicts_total",
                (("operation", "reject"),),
            )
        ]
        == 1
    )
    assert (
        samples[("sidera_collaboration_governance_proposal_age_seconds", ())] >= 7_000
    )
    assert all(
        set(dict(sample.labels))
        <= {
            "outcome",
            "status",
            "reason",
            "command",
            "result",
            "action",
            "reason_code",
            "operation",
        }
        for sample in view.samples
    )
    assert all(
        workspace_id.__str__() not in str(dict(sample.labels))
        for sample in view.samples
    )


def test_governance_metric_labels_reject_ids_and_free_text() -> None:
    with pytest.raises(ValueError):
        CollaborationMetricSample(
            name="sidera_collaboration_governance_commands_total",
            labels=(("command", str(uuid4())), ("result", "applied")),
            value=1,
        )
    with pytest.raises(ValueError):
        CollaborationMetricSample(
            name="sidera_collaboration_governance_stale_total",
            labels=(("reason_code", "user supplied reason"),),
            value=1,
        )


def _command(
    command_id: UUID,
    workspace_id: UUID,
    command_type: str,
    *,
    status: str,
    rejection_reason: str | None = None,
) -> CommandModel:
    return CommandModel(
        id=command_id,
        workspace_id=workspace_id,
        command_type=command_type,
        actor_type="local",
        actor_id="operator",
        target_type="worker_item",
        target_id=uuid4(),
        payload={},
        expected_version=1,
        idempotency_key=f"metrics:{command_id}",
        request_hash="5" * 64,
        correlation_id=command_id,
        status=status,
        result={},
        rejection_reason=rejection_reason,
    )
