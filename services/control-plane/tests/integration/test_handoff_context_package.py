from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update
from test_context_package_worker import EmptyCatalog
from test_handoff_target_execution import _seed as seed_handoff
from test_planning_runner_integration import seed_request

from sidera_control_plane.application.handoff_context import HandoffContextService
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.context_packages import ContextPurpose
from sidera_control_plane.domain.execution_requests import ExecutionRequest
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffInputClassification,
    HandoffStopEvidence,
)
from sidera_control_plane.domain.handoffs import (
    HandoffProposal,
    HandoffProposalPayload,
)
from sidera_control_plane.domain.workspace import workspace_manifest_digest
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextAssemblyRequestModel,
    ContextPackageModel,
    HandoffProposalModel,
    HandoffTransitionModel,
    PlanningProposalRevisionModel,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _reset_transition_for_bundle(
    database: Database,
    fixture,
    checkpoint: HandoffCheckpointEvidence,
    stop: HandoffStopEvidence,
    manifest,
) -> None:
    with database.session() as session:
        session.execute(
            update(HandoffTransitionModel)
            .where(HandoffTransitionModel.id == fixture.transition_id)
            .values(
                handoff_context_bundle_id=None,
                target_context_package_id=None,
                target_context_package_hash=None,
                status="running",
                phase="assemble_bundle",
                writer_state="target_starting",
                source_checkpoint_sequence=checkpoint.accepted_sequence,
                source_checkpoint_facts=checkpoint.to_mapping(),
                source_stop_sequence=stop.accepted_sequence,
                source_stop_facts=stop.to_mapping(),
                source_workspace_freeze_operation_id=uuid4(),
                source_workspace_freeze_facts={
                    "manifest_id": str(manifest.id),
                    "workspace_digest": checkpoint.workspace_digest,
                    "lifecycle": "frozen",
                    "fencing_token": manifest.fencing_token,
                },
                version=1,
            )
        )
        session.execute(
            delete(ContextPackageModel).where(
                ContextPackageModel.consumer_id == fixture.target_execution_id
            )
        )
        session.execute(
            delete(ContextAssemblyRequestModel).where(
                ContextAssemblyRequestModel.consumer_id == fixture.target_execution_id
            )
        )
        session.commit()


def _seed_proposal(database: Database, fixture) -> HandoffProposal:
    with database.session() as session:
        transition = session.get(HandoffTransitionModel, fixture.transition_id)
        assert transition is not None
        row = session.get(HandoffProposalModel, transition.proposal_id)
        assert row is not None
        payload = HandoffProposalPayload(
            proposal_schema_version=row.proposal_schema_version,
            workspace_id=row.workspace_id,
            root_task_id=row.root_task_id,
            subtask_id=row.subtask_id,
            worker_item_id=row.worker_item_id,
            source_execution_id=row.source_execution_id,
            source_execution_version=row.source_execution_version,
            source_session_id=row.source_session_id,
            source_session_version=row.source_session_version,
            source_session_last_sequence=row.source_session_last_sequence,
            source_worker_id=row.source_worker_id,
            source_worker_version=row.source_worker_version,
            source_runtime_snapshot=dict(row.source_runtime_snapshot),
            source_workspace_manifest_id=row.source_workspace_manifest_id,
            source_workspace_manifest_version=row.source_workspace_manifest_version,
            source_context_package_id=row.source_context_package_id,
            source_context_package_hash=row.source_context_package_hash,
            source_worker_context_version=row.source_worker_context_version,
            observed_plan_snapshot_id=row.observed_plan_snapshot_id,
            observed_plan_generation=row.observed_plan_generation,
            observed_plan_head_version=row.observed_plan_head_version,
            target_worker_id=row.target_worker_id,
            target_worker_version=row.target_worker_version,
            target_runtime_snapshot=dict(row.target_runtime_snapshot),
            target_capability_snapshot_hash=row.target_capability_snapshot_hash,
            target_policy_reason=dict(row.target_policy_reason),
            target_policy_facts=dict(row.target_policy_facts),
            transfer_mode=row.transfer_mode,
            handoff_reason=row.handoff_reason,
            handoff_instruction=row.handoff_instruction,
            input_plan=dict(row.input_plan),
            impact_summary=dict(row.impact_summary),
        )
        return HandoffProposal(
            id=row.id,
            payload=payload,
            proposal_hash=payload.content_hash,
            status=row.status,
            stale_reason=row.stale_reason,
            version=row.version,
            created_by_type=row.created_by_type,
            created_by_id=row.created_by_id,
            source_channel=row.source_channel,
            created_at=row.created_at,
            expires_at=row.expires_at,
            confirmed_at=row.confirmed_at,
            cancelled_at=row.cancelled_at,
        )


def _add_target_request(
    uow: ControlPlaneUnitOfWork,
    *,
    planning_request_id: UUID,
    transition,
    proposal,
    target_execution,
    worker_item,
) -> ExecutionRequest:
    planning_request = uow.planning_requests.get(
        planning_request_id,
        transition.workspace_id,
    )
    assert planning_request is not None
    assert planning_request.runs
    run = planning_request.runs[0]
    proposal_revision_id = uuid4()
    uow.session.add(
        PlanningProposalRevisionModel(
            id=proposal_revision_id,
            planning_request_id=planning_request.id,
            revision=1,
            workspace_id=transition.workspace_id,
            brief_revision=1,
            planning_run_id=run.id,
            proposal_schema_version=2,
            kind="create",
            profile_id=planning_request.planning_profile_id,
            profile_version=planning_request.planning_profile_version,
            planner_module_instance_id=run.planner_module_instance_id,
            planner_module_config_version=run.planner_module_config_version,
            decision_summary={},
            plan_template={},
            execution_intents=[],
            impact_policy={},
            derived_diff={},
            canonical_payload={},
            content_hash="a" * 64,
            created_at=NOW,
        )
    )
    uow.session.flush()
    target_request = ExecutionRequest.create(
        request_id=uuid4(),
        workspace_id=transition.workspace_id,
        planning_request_id=planning_request.id,
        proposal_revision_id=proposal_revision_id,
        plan_snapshot_id=proposal.payload.observed_plan_snapshot_id,
        plan_generation=proposal.payload.observed_plan_generation,
        root_task_id=proposal.payload.root_task_id,
        subtask_id=proposal.payload.subtask_id,
        worker_item_id=worker_item.id,
        worker_item_key=worker_item.stable_key,
        intent_key=f"handoff:{worker_item.stable_key}",
        purpose="handoff",
        capability_requirements=("python",),
        adapter_preferences=("fake",),
        budget_class="standard",
        not_before=NOW,
        max_attempts=3,
        expected_artifact_refs=(),
        expected_evidence_refs=(),
        expected_outputs=("diff",),
        source_execution_id=transition.source_execution_id,
        source_session_id=transition.source_session_id,
        handoff_transition_id=transition.id,
        now=NOW,
    )
    target_request.mark_ready(now=NOW)
    target_request.materialize(
        execution_lineage_id=target_execution.lineage_id,
        first_execution_id=target_execution.id,
        now=NOW,
    )
    uow.execution_requests.add(target_request)
    return target_request


def test_handoff_bundle_drives_one_real_target_context_package(
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    fixture, _runtime = seed_handoff(database, workspace_id)
    seed_proposal = _seed_proposal(database, fixture)
    planning_request_id = seed_request(uow_factory, workspace_id)

    with uow_factory() as uow:
        transition = uow.handoff_transitions.get(fixture.transition_id, workspace_id)
        proposal = seed_proposal if transition is not None else None
        source_package = (
            uow.context_packages.get(
                proposal.payload.source_context_package_id,
                workspace_id,
            )
            if proposal is not None
            else None
        )
        source_manifest = (
            uow.workspaces.get(transition.source_workspace_manifest_id, workspace_id)
            if transition is not None
            else None
        )
        assert transition is not None
        assert proposal is not None
        assert source_package is not None
        assert source_manifest is not None
        checkpoint = HandoffCheckpointEvidence(
            operation_id=uuid4(),
            transition_id=transition.id,
            source_session_id=transition.source_session_id,
            accepted_sequence=3,
            workspace_digest=workspace_manifest_digest(source_manifest),
            context_package_id=source_package.package_id,
            context_package_hash=source_package.package_hash,
            context_version=source_package.context_version or 0,
            progress_summary_ref="progress:handoff",
            artifact_refs=("artifact:diff",),
            evidence_refs=("evidence:checkpoint",),
            quiescent=True,
            produced_at=NOW,
        )
        stop = HandoffStopEvidence(
            operation_id=uuid4(),
            source_session_id=transition.source_session_id,
            accepted_sequence=3,
            stopped=True,
            produced_at=NOW,
        )

    _reset_transition_for_bundle(database, fixture, checkpoint, stop, source_manifest)

    service = HandoffContextService(uow_factory)
    with uow_factory() as uow:
        transition = uow.handoff_transitions.get(fixture.transition_id, workspace_id)
        assert transition is not None
        proposal = seed_proposal
        target_execution = uow.executions.get(fixture.target_execution_id, workspace_id)
        worker_item = uow.worker_items.get(transition.worker_item_id, workspace_id)
        assert proposal is not None
        assert target_execution is not None
        assert worker_item is not None
        target_request = _add_target_request(
            uow,
            planning_request_id=planning_request_id,
            transition=transition,
            proposal=proposal,
            target_execution=target_execution,
            worker_item=worker_item,
        )
        classification = HandoffInputClassification(
            source_session_id=transition.source_session_id,
            cutoff_sequence=transition.source_input_cutoff_sequence,
            dispositions=(),
            transferable_input_ids=(),
            excluded_input_ids=(),
            unknown_input_ids=(),
        )
        source_facts = service.collect_source_facts(
            transition=transition,
            workspace_id=workspace_id,
            input_classification=classification,
        )
        bundle, assembly = service.create_bundle_and_request(
            transition=transition,
            proposal=proposal,
            target_request=target_request,
            target_execution=target_execution,
            source_facts=source_facts,
            now=NOW,
            uow=uow,
        )

    assert assembly.consumer_id == fixture.target_execution_id
    assert assembly.budget["handoff_context_bundle_id"] == str(bundle.id)
    worker = ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=EmptyCatalog(),
        owner_id="handoff-context-worker",
        clock=lambda: NOW,
    )
    result = worker.run_once(limit=1)

    assert result.claimed == 1
    assert result.ready == 1
    assert result.blocked == 0
    with uow_factory() as uow:
        package = uow.context_packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            fixture.target_execution_id,
            workspace_id,
        )
        transition = uow.handoff_transitions.get(fixture.transition_id, workspace_id)
        assert package is not None
        assert transition is not None
        assert package.status.value == "active"
        assert package.control_plane_fact_snapshot["handoff"]["bundle_id"] == str(bundle.id)
        assert package.control_plane_fact_snapshot["handoff"]["bundle_hash"] == bundle.bundle_hash
        assert package.control_plane_fact_snapshot["handoff"]["source"]["last_accepted_sequence"] == 3
        assert transition.handoff_context_bundle_id == bundle.id
        assert "trace" not in str(package.control_plane_fact_snapshot).lower()
        assert "snapshot" not in str(package.control_plane_fact_snapshot).lower()
