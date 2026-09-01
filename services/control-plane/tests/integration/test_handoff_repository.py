from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from test_context_package_repository import _seed_context

from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.handoff_evidence import (
    HandoffContextBundle,
    HandoffWorkspaceTransfer,
    HandoffWorkspaceTransferStatus,
)
from sidera_control_plane.domain.handoff_recovery import (
    HandoffRecoveryDecision,
    HandoffRecoveryStrategy,
)
from sidera_control_plane.domain.handoffs import (
    HandoffProposal,
    HandoffProposalPayload,
    HandoffProposalStatus,
    HandoffSourceChannel,
    HandoffTransferMode,
    HandoffTransition,
    HandoffTransitionStatus,
)
from sidera_control_plane.persistence.handoff_repository import (
    HandoffContextBundleRepository,
    HandoffProposalRepository,
    HandoffRecoveryDecisionRepository,
    HandoffTransitionRepository,
    HandoffWorkspaceTransferRepository,
)
from sidera_control_plane.persistence.models import (
    ContextAssemblyRequestModel,
    ContextPackageModel,
    ExecutionModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    PlanSnapshotModel,
    TaskModel,
    WorkerItemModel,
    WorkerModel,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class HandoffSeed:
    workspace_id: UUID
    worker_item_id: UUID
    source_worker_id: UUID
    target_worker_id: UUID
    source_execution_id: UUID
    source_session_id: UUID
    source_manifest_id: UUID
    target_execution_id: UUID
    target_manifest_id: UUID
    proposal: HandoffProposal
    transition: HandoffTransition


def _worker(*, worker_id: UUID, workspace_id: UUID, display_name: str) -> WorkerModel:
    return WorkerModel(
        id=worker_id,
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name=display_name,
        status="active",
        desired_state="active",
        capabilities=["code"],
        capacity=1,
        current_load=0,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        last_heartbeat_at=NOW,
        version=1,
    )


def _execution(
    *,
    execution_id: UUID,
    workspace_id: UUID,
    task_id: UUID,
    worker_item_id: UUID,
    worker_id: UUID,
    lineage_id: UUID,
    attempt: int,
    source_execution_id: UUID | None = None,
    source_session_id: UUID | None = None,
    handoff_transition_id: UUID | None = None,
) -> ExecutionModel:
    return ExecutionModel(
        id=execution_id,
        workspace_id=workspace_id,
        task_id=task_id,
        lineage_id=lineage_id,
        attempt=attempt,
        max_attempts=3,
        purpose="handoff repository test",
        status="running" if attempt == 1 else "queued",
        capability_requirements=["code"],
        adapter_preferences=["fake"],
        adapter_forbidden=[],
        pinned_worker_id=worker_id,
        assigned_worker_id=worker_id,
        active_lease_id=None,
        worker_item_id=worker_item_id,
        last_fencing_token=1,
        not_before=NOW,
        failure_code=None,
        started_at=NOW,
        finished_at=None,
        version=1,
        stop_requested=False,
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
        handoff_transition_id=handoff_transition_id,
        created_at=NOW,
    )


def _manifest(
    *,
    manifest_id: UUID,
    workspace_id: UUID,
    task_id: UUID,
    execution_id: UUID,
    lifecycle: str,
    location_ref: str,
    fencing_token: int,
) -> ExecutionWorkspaceModel:
    return ExecutionWorkspaceModel(
        id=manifest_id,
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        checkouts=[{"repository_ref": "repo://sidera", "current_head": "a" * 40}],
        location_ref=location_ref,
        lifecycle=lifecycle,
        retention_seconds=3600,
        retain_until=NOW + timedelta(hours=1),
        fencing_token=fencing_token,
        frozen_at=NOW if lifecycle == "frozen" else None,
        released_at=None,
        version=1,
        created_at=NOW,
    )


def _payload(
    *,
    workspace_id: UUID,
    worker_item_id: UUID,
    source_worker_id: UUID,
    target_worker_id: UUID,
    source_execution_id: UUID,
    source_session_id: UUID,
    source_manifest_id: UUID,
    source_context_package_id: UUID,
    plan_snapshot_id: UUID,
    root_task_id: UUID,
    subtask_id: UUID,
) -> HandoffProposalPayload:
    return HandoffProposalPayload(
        proposal_schema_version=1,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        subtask_id=subtask_id,
        worker_item_id=worker_item_id,
        source_execution_id=source_execution_id,
        source_execution_version=1,
        source_session_id=source_session_id,
        source_session_version=1,
        source_session_last_sequence=3,
        source_worker_id=source_worker_id,
        source_worker_version=1,
        source_runtime_snapshot={"worker_id": str(source_worker_id), "version": 1},
        source_workspace_manifest_id=source_manifest_id,
        source_workspace_manifest_version=1,
        source_context_package_id=source_context_package_id,
        source_context_package_hash="b" * 64,
        source_worker_context_version=1,
        observed_plan_snapshot_id=plan_snapshot_id,
        observed_plan_generation=1,
        observed_plan_head_version=1,
        target_worker_id=target_worker_id,
        target_worker_version=1,
        target_runtime_snapshot={"worker_id": str(target_worker_id), "version": 1},
        target_capability_snapshot_hash="c" * 64,
        target_policy_reason={"rank": 1},
        target_policy_facts={"healthy": True},
        transfer_mode=HandoffTransferMode.REUSE_EXACT,
        handoff_reason="source worker is unavailable",
        handoff_instruction="continue from the accepted checkpoint",
        input_plan={"cutoff_sequence": 3},
        impact_summary={"scope": "worker_item"},
    )


def _seed(database, workspace_id: UUID, *, with_transition: bool) -> HandoffSeed:
    _, context_revision_id, _, _ = _seed_context(database, workspace_id)
    root_task_id = uuid4()
    subtask_id = uuid4()
    worker_item_id = uuid4()
    source_worker_id = uuid4()
    target_worker_id = uuid4()
    source_execution_id = uuid4()
    source_session_id = uuid4()
    source_manifest_id = uuid4()
    source_context_package_id = uuid4()
    context_request_id = uuid4()
    plan_snapshot_id = uuid4()
    target_execution_id = uuid4()
    target_manifest_id = uuid4()

    with database.session() as session:
        session.add_all(
            [
                TaskModel(
                    id=root_task_id,
                    workspace_id=workspace_id,
                    title="Handoff root",
                    description="Handoff repository root",
                    acceptance_criteria=["handoff:test"],
                    status="running",
                    priority=50,
                    requested_by="integration",
                    version=1,
                    created_at=NOW,
                ),
                TaskModel(
                    id=subtask_id,
                    workspace_id=workspace_id,
                    title="Handoff subtask",
                    description="Handoff repository subtask",
                    acceptance_criteria=["handoff:test"],
                    status="running",
                    priority=50,
                    requested_by="integration",
                    version=1,
                    created_at=NOW,
                ),
                WorkerItemModel(
                    id=worker_item_id,
                    workspace_id=workspace_id,
                    root_task_id=root_task_id,
                    stable_key=f"handoff-worker-{worker_item_id.hex[:8]}",
                    title="Handoff worker item",
                    responsibility="Persist handoff state",
                    status="active",
                    context_version=1,
                    acknowledged_context_version=1,
                    derived_from_worker_item_ids=[],
                    version=1,
                    created_at=NOW,
                ),
                _worker(
                    worker_id=source_worker_id,
                    workspace_id=workspace_id,
                    display_name="Source worker",
                ),
                _worker(
                    worker_id=target_worker_id,
                    workspace_id=workspace_id,
                    display_name="Target worker",
                ),
            ]
        )
        session.flush()
        session.add(
            _execution(
                execution_id=source_execution_id,
                workspace_id=workspace_id,
                task_id=root_task_id,
                worker_item_id=worker_item_id,
                worker_id=source_worker_id,
                lineage_id=source_execution_id,
                attempt=1,
            )
        )
        session.flush()
        session.add(
            ExecutionSessionModel(
                id=source_session_id,
                workspace_id=workspace_id,
                execution_id=source_execution_id,
                worker_id=source_worker_id,
                adapter_session_id=f"handoff-session-{source_session_id.hex[:8]}",
                status="active",
                last_sequence=3,
                last_activity_at=NOW,
                input_mode="queued",
                adapter_module_id=None,
                adapter_config_version=None,
                workspace_module_id=None,
                workspace_config_version=None,
                capability_snapshot={},
                process_ref=None,
                workspace_ref=None,
                version=1,
                created_at=NOW,
            )
        )
        session.add(
            _manifest(
                manifest_id=source_manifest_id,
                workspace_id=workspace_id,
                task_id=root_task_id,
                execution_id=source_execution_id,
                lifecycle="frozen",
                location_ref=f"git-worktree://source/{source_execution_id}",
                fencing_token=1,
            )
        )
        session.add(
            ContextAssemblyRequestModel(
                id=context_request_id,
                workspace_id=workspace_id,
                purpose="execution",
                consumer_type="worker",
                consumer_id=source_execution_id,
                worker_item_id=worker_item_id,
                work_context_revision_id=context_revision_id,
                context_version=1,
                knowledge_search_scope={},
                knowledge_filter_refs=[],
                fact_scope_refs=[],
                budget={},
                expires_at=NOW + timedelta(hours=1),
                status="ready",
                lease_owner=None,
                lease_expires_at=None,
                attempt=1,
                next_attempt_at=None,
                error_code=None,
                request_fingerprint="a" * 64,
                idempotency_key=f"handoff-context-request-{context_request_id}",
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ContextPackageModel(
                id=source_context_package_id,
                workspace_id=workspace_id,
                assembly_request_id=context_request_id,
                purpose="execution",
                consumer_type="worker",
                consumer_id=source_execution_id,
                worker_item_id=worker_item_id,
                work_context_revision_id=context_revision_id,
                context_version=1,
                control_plane_fact_snapshot={},
                artifact_refs=[],
                evidence_refs=[],
                budget={},
                expires_at=NOW + timedelta(hours=1),
                package_hash="b" * 64,
                status="active",
                revoked_at=None,
                revocation_reason=None,
                revocation_scope=None,
                idempotency_key=f"handoff-context-package-{source_context_package_id}",
                materialized_at=NOW,
                created_at=NOW,
            )
        )
        session.add(
            PlanSnapshotModel(
                id=plan_snapshot_id,
                schema_version=1,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                generation=1,
                parent_snapshot_id=None,
                source_type="integration",
                source_ref="test:handoff-repository",
                title="Handoff plan",
                goal_summary="Persist handoff state",
                payload={},
                published_by="integration",
                published_at=NOW,
                created_at=NOW,
            )
        )
        session.commit()

    proposal = HandoffProposal.create(
        id=uuid4(),
        payload=_payload(
            workspace_id=workspace_id,
            worker_item_id=worker_item_id,
            source_worker_id=source_worker_id,
            target_worker_id=target_worker_id,
            source_execution_id=source_execution_id,
            source_session_id=source_session_id,
            source_manifest_id=source_manifest_id,
            source_context_package_id=source_context_package_id,
            plan_snapshot_id=plan_snapshot_id,
            root_task_id=root_task_id,
            subtask_id=subtask_id,
        ),
        created_by_type="user",
        created_by_id="integration",
        source_channel=HandoffSourceChannel.WEB,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    confirmed_proposal = replace(
        proposal,
        status=HandoffProposalStatus.CONFIRMED,
        version=2,
        confirmed_at=NOW,
    )
    transition = HandoffTransition.create(
        id=uuid4(),
        proposal=confirmed_proposal,
        confirmed_by_type="user",
        confirmed_by_id="integration",
        confirmed_at=NOW,
    )

    with database.session() as session:
        proposals = HandoffProposalRepository(session)
        proposals.add(proposal)
        session.flush()
        if with_transition:
            proposal.mark_confirmed(now=NOW)
            proposals.save_status(proposal, previous_version=1)
            HandoffTransitionRepository(session).add(transition)
            session.flush()
            session.add(
                _execution(
                    execution_id=target_execution_id,
                    workspace_id=workspace_id,
                    task_id=root_task_id,
                    worker_item_id=worker_item_id,
                    worker_id=target_worker_id,
                    lineage_id=source_execution_id,
                    attempt=2,
                    source_execution_id=source_execution_id,
                    source_session_id=source_session_id,
                    handoff_transition_id=transition.id,
                )
            )
            session.flush()
            session.add(
                _manifest(
                    manifest_id=target_manifest_id,
                    workspace_id=workspace_id,
                    task_id=root_task_id,
                    execution_id=target_execution_id,
                    lifecycle="ready",
                    location_ref=f"git-worktree://target/{target_execution_id}",
                    fencing_token=2,
                )
            )
        session.commit()

    return HandoffSeed(
        workspace_id=workspace_id,
        worker_item_id=worker_item_id,
        source_worker_id=source_worker_id,
        target_worker_id=target_worker_id,
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
        source_manifest_id=source_manifest_id,
        target_execution_id=target_execution_id,
        target_manifest_id=target_manifest_id,
        proposal=proposal,
        transition=transition,
    )


def _bundle(
    seed: HandoffSeed, *, handoff_instruction: str = "continue from the accepted checkpoint"
) -> HandoffContextBundle:
    return HandoffContextBundle.create(
        id=uuid4(),
        schema_version=1,
        workspace_id=seed.workspace_id,
        worker_item_id=seed.worker_item_id,
        handoff_transition_id=seed.transition.id,
        logical_attempt=1,
        recovery_decision_id=None,
        source_execution_id=seed.source_execution_id,
        source_session_id=seed.source_session_id,
        source_checkpoint_sequence=3,
        source_last_accepted_sequence=3,
        source_context_package_id=seed.proposal.payload.source_context_package_id,
        source_context_package_hash="b" * 64,
        source_worker_context_version=1,
        target_execution_id=seed.target_execution_id,
        target_worker_id=seed.target_worker_id,
        target_runtime_snapshot_hash="d" * 64,
        target_context_version=1,
        source_workspace_manifest_id=seed.source_manifest_id,
        source_workspace_version=1,
        workspace_location_ref_hash="e" * 64,
        repository_state=({"repository_ref": "repo://sidera", "head": "a" * 40},),
        artifact_refs=("artifact:diff",),
        evidence_refs=("evidence:checkpoint",),
        handoff_reason="source worker is unavailable",
        handoff_instruction=handoff_instruction,
        delivered_input_refs=(),
        transferable_input_refs=(),
        excluded_input_refs=(),
        created_at=NOW,
    )


def _recovery(seed: HandoffSeed) -> HandoffRecoveryDecision:
    return HandoffRecoveryDecision(
        id=uuid4(),
        handoff_transition_id=seed.transition.id,
        expected_transition_version=1,
        strategy=HandoffRecoveryStrategy.RETRY_CURRENT_PHASE,
        actor_type="user",
        actor_id="integration",
        source_channel="web",
        reason="retry after a transient coordinator failure",
        evidence_refs=("evidence:operation-unknown",),
        replacement_target_snapshot=None,
        resulting_operation_refs=(uuid4(),),
        created_at=NOW,
    )


def _transfer(seed: HandoffSeed) -> HandoffWorkspaceTransfer:
    return HandoffWorkspaceTransfer(
        transition_id=seed.transition.id,
        logical_attempt=1,
        operation_id=uuid4(),
        source_manifest_id=seed.source_manifest_id,
        target_manifest_id=seed.target_manifest_id,
        source_execution_id=seed.source_execution_id,
        target_execution_id=seed.target_execution_id,
        source_fencing_token=1,
        target_fencing_token=2,
        location_ref_hash="e" * 64,
        repository_state_digest="f" * 64,
        status=HandoffWorkspaceTransferStatus.SUCCEEDED,
        failure_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_proposal_repository_round_trip_and_status_cas(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=False)
    with database.session() as session:
        repository = HandoffProposalRepository(session)
        loaded = repository.get(seed.proposal.id, workspace_id, for_update=True)
        assert loaded == seed.proposal
        assert repository.find_prepared_for_worker_item(
            seed.worker_item_id, workspace_id
        ) == seed.proposal

        loaded.mark_confirmed(now=NOW)
        repository.save_status(loaded, previous_version=1)
        session.commit()

    with database.session() as session:
        repository = HandoffProposalRepository(session)
        confirmed = repository.get(seed.proposal.id, workspace_id)
        assert confirmed is not None
        assert confirmed.status.value == "confirmed"
        with pytest.raises(VersionConflict):
            repository.save_status(confirmed, previous_version=1)
        session.rollback()

        tampered_payload = replace(
            confirmed.payload, handoff_instruction="tampered"
        )
        tampered = replace(
            confirmed,
            payload=tampered_payload,
            proposal_hash=tampered_payload.content_hash,
        )
        with pytest.raises(ValidationError, match="immutable"):
            repository.save_status(tampered, previous_version=confirmed.version)


def test_transition_repository_active_lookup_includes_failed(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    with database.session() as session:
        repository = HandoffTransitionRepository(session)
        transition = repository.get(seed.transition.id, workspace_id, for_update=True)
        assert transition is not None
        transition.block(
            failure_code="source_checkpoint_failed",
            now=NOW,
            status=HandoffTransitionStatus.FAILED,
        )
        repository.save(transition, previous_version=1)
        session.commit()

    with database.session() as session:
        repository = HandoffTransitionRepository(session)
        active = repository.get_active_for_worker_item(
            seed.worker_item_id, workspace_id
        )
        assert active is not None
        assert active.status is HandoffTransitionStatus.FAILED
        assert repository.get_active_for_worker_items(
            (seed.worker_item_id,), workspace_id
        ) == (active,)


def test_transition_claim_uses_skip_locked_and_lease(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    with database.session() as first_session:
        claimed = HandoffTransitionRepository(first_session).claim_next(
            owner_id="coordinator-a", now=NOW, limit=1
        )
        assert [item.id for item in claimed] == [seed.transition.id]
        assert claimed[0].lease_owner == "coordinator-a"
        assert claimed[0].lease_expires_at is not None
        assert claimed[0].lease_expires_at > NOW
        with database.session() as second_session:
            assert HandoffTransitionRepository(second_session).claim_next(
                owner_id="coordinator-b", now=NOW, limit=1
            ) == ()
        first_session.rollback()


def test_bundle_repository_is_append_only_per_attempt(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    bundle = _bundle(seed)
    with database.session() as session:
        repository = HandoffContextBundleRepository(session)
        repository.add_append_only(bundle)
        repository.add_append_only(bundle)
        session.commit()

    with database.session() as session:
        repository = HandoffContextBundleRepository(session)
        assert repository.get_by_attempt(
            seed.transition.id, 1, workspace_id, for_update=True
        ) == bundle
        assert repository.list_for_transition(seed.transition.id, workspace_id) == (
            bundle,
        )
        changed = _bundle(seed, handoff_instruction="different")
        with pytest.raises(ValidationError, match="immutable|attempt"):
            repository.add_append_only(changed)


def test_recovery_decision_repository_never_updates(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    decision = _recovery(seed)
    with database.session() as session:
        repository = HandoffRecoveryDecisionRepository(session)
        repository.add_append_only(decision, workspace_id)
        session.commit()

    with database.session() as session:
        repository = HandoffRecoveryDecisionRepository(session)
        assert repository.get(decision.id, workspace_id, for_update=True) == decision
        assert repository.list_for_transition(seed.transition.id, workspace_id) == (
            decision,
        )
        assert not hasattr(repository, "save")


def test_workspace_transfer_repository_inspects_by_operation_identity(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    transfer = _transfer(seed)
    with database.session() as session:
        repository = HandoffWorkspaceTransferRepository(session)
        repository.add_append_only(transfer, workspace_id)
        session.commit()

    with database.session() as session:
        repository = HandoffWorkspaceTransferRepository(session)
        assert repository.get_by_operation(
            transfer.operation_id, workspace_id, for_update=True
        ) == transfer
        assert repository.get_by_attempt(
            seed.transition.id, transfer.logical_attempt, workspace_id
        ) == transfer
        assert repository.list_for_transition(seed.transition.id, workspace_id) == (
            transfer,
        )


def test_workspace_transfer_repository_updates_outcome_with_cas(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    requested = replace(
        _transfer(seed),
        target_manifest_id=None,
        status=HandoffWorkspaceTransferStatus.REQUESTED,
        failure_code=None,
        version=1,
    )
    with database.session() as session:
        repository = HandoffWorkspaceTransferRepository(session)
        repository.add_append_only(requested, workspace_id)
        session.commit()

    succeeded = replace(
        requested,
        target_manifest_id=seed.target_manifest_id,
        status=HandoffWorkspaceTransferStatus.SUCCEEDED,
        version=2,
        updated_at=NOW,
    )
    with database.session() as session:
        repository = HandoffWorkspaceTransferRepository(session)
        repository.save(succeeded, previous_version=1)
        session.commit()

    with database.session() as session:
        loaded = HandoffWorkspaceTransferRepository(session).get_by_operation(
            requested.operation_id,
            workspace_id,
        )
        assert loaded == succeeded
        with pytest.raises(VersionConflict):
            HandoffWorkspaceTransferRepository(session).save(
                succeeded,
                previous_version=1,
            )


def test_all_handoff_repositories_reject_cross_workspace_lookup(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    bundle = _bundle(seed)
    decision = _recovery(seed)
    transfer = _transfer(seed)
    with database.session() as session:
        HandoffContextBundleRepository(session).add_append_only(bundle)
        HandoffRecoveryDecisionRepository(session).add_append_only(
            decision, workspace_id
        )
        HandoffWorkspaceTransferRepository(session).add_append_only(
            transfer, workspace_id
        )
        session.commit()

    other_workspace_id = uuid4()
    with database.session() as session:
        assert HandoffProposalRepository(session).get(
            seed.proposal.id, other_workspace_id
        ) is None
        assert HandoffProposalRepository(session).find_prepared_for_worker_item(
            seed.worker_item_id, other_workspace_id
        ) is None
        assert HandoffTransitionRepository(session).get(
            seed.transition.id, other_workspace_id
        ) is None
        assert HandoffTransitionRepository(session).get_active_for_worker_item(
            seed.worker_item_id, other_workspace_id
        ) is None
        assert HandoffContextBundleRepository(session).get_by_attempt(
            seed.transition.id, 1, other_workspace_id
        ) is None
        assert HandoffRecoveryDecisionRepository(session).get(
            decision.id, other_workspace_id
        ) is None
        assert HandoffWorkspaceTransferRepository(session).get_by_operation(
            transfer.operation_id, other_workspace_id
        ) is None
