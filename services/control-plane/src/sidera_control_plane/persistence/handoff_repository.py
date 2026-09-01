from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import (
    HandoffContextBundleRepositoryPort,
    HandoffProposalRepositoryPort,
    HandoffRecoveryDecisionRepositoryPort,
    HandoffTransitionRepositoryPort,
    HandoffWorkspaceTransferRepositoryPort,
)
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
    ACTIVE_HANDOFF_TRANSITION_STATUSES,
    HandoffPhase,
    HandoffProposal,
    HandoffProposalPayload,
    HandoffProposalStatus,
    HandoffSourceChannel,
    HandoffTransferMode,
    HandoffTransition,
    HandoffTransitionStatus,
    HandoffWriterState,
)
from sidera_control_plane.persistence.models import (
    ExecutionWorkspaceTransferModel,
    HandoffContextBundleModel,
    HandoffProposalModel,
    HandoffRecoveryDecisionModel,
    HandoffTransitionModel,
)

HANDOFF_CLAIM_LEASE_SECONDS = 30


class HandoffProposalRepository(HandoffProposalRepositoryPort):
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        proposal_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffProposal | None:
        statement = select(HandoffProposalModel).where(
            HandoffProposalModel.id == proposal_id,
            HandoffProposalModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_proposal(row) if row is not None else None

    def find_prepared_for_worker_item(
        self,
        worker_item_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffProposal | None:
        statement = select(HandoffProposalModel).where(
            HandoffProposalModel.worker_item_id == worker_item_id,
            HandoffProposalModel.workspace_id == workspace_id,
            HandoffProposalModel.status == HandoffProposalStatus.PREPARED.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_proposal(row) if row is not None else None

    def add(self, proposal: HandoffProposal) -> None:
        payload = proposal.payload
        self._session.add(
            HandoffProposalModel(
                id=proposal.id,
                workspace_id=payload.workspace_id,
                root_task_id=payload.root_task_id,
                subtask_id=payload.subtask_id,
                worker_item_id=payload.worker_item_id,
                source_execution_id=payload.source_execution_id,
                source_execution_version=payload.source_execution_version,
                source_session_id=payload.source_session_id,
                source_session_version=payload.source_session_version,
                source_session_last_sequence=payload.source_session_last_sequence,
                source_worker_id=payload.source_worker_id,
                source_worker_version=payload.source_worker_version,
                source_runtime_snapshot=dict(payload.source_runtime_snapshot),
                source_workspace_manifest_id=payload.source_workspace_manifest_id,
                source_workspace_manifest_version=payload.source_workspace_manifest_version,
                source_context_package_id=payload.source_context_package_id,
                source_context_package_hash=payload.source_context_package_hash,
                source_worker_context_version=payload.source_worker_context_version,
                observed_plan_snapshot_id=payload.observed_plan_snapshot_id,
                observed_plan_generation=payload.observed_plan_generation,
                observed_plan_head_version=payload.observed_plan_head_version,
                target_worker_id=payload.target_worker_id,
                target_worker_version=payload.target_worker_version,
                target_runtime_snapshot=dict(payload.target_runtime_snapshot),
                target_capability_snapshot_hash=payload.target_capability_snapshot_hash,
                target_policy_reason=dict(payload.target_policy_reason),
                target_policy_facts=dict(payload.target_policy_facts),
                transfer_mode=payload.transfer_mode.value,
                handoff_reason=payload.handoff_reason,
                handoff_instruction=payload.handoff_instruction,
                input_plan=dict(payload.input_plan),
                impact_summary=dict(payload.impact_summary),
                payload=payload.to_mapping(),
                proposal_schema_version=payload.proposal_schema_version,
                proposal_hash=proposal.proposal_hash,
                status=proposal.status.value,
                stale_reason=proposal.stale_reason,
                version=proposal.version,
                created_by_type=proposal.created_by_type,
                created_by_id=proposal.created_by_id,
                source_channel=proposal.source_channel.value,
                expires_at=proposal.expires_at,
                confirmed_at=proposal.confirmed_at,
                cancelled_at=proposal.cancelled_at,
                created_at=proposal.created_at,
            )
        )

    def save_status(self, proposal: HandoffProposal, previous_version: int) -> None:
        row = self._session.scalar(
            select(HandoffProposalModel)
            .where(
                HandoffProposalModel.id == proposal.id,
                HandoffProposalModel.workspace_id == proposal.payload.workspace_id,
            )
            .with_for_update()
        )
        if row is None:
            raise VersionConflict(previous_version, 0)
        if row.version != previous_version:
            raise VersionConflict(previous_version, row.version)
        if (
            row.proposal_hash != proposal.proposal_hash
            or row.payload != proposal.payload.to_mapping()
        ):
            raise ValidationError("handoff proposal payload is immutable")
        result = self._session.execute(
            update(HandoffProposalModel)
            .where(
                HandoffProposalModel.id == proposal.id,
                HandoffProposalModel.workspace_id == proposal.payload.workspace_id,
                HandoffProposalModel.version == previous_version,
            )
            .values(
                status=proposal.status.value,
                stale_reason=proposal.stale_reason,
                version=proposal.version,
                confirmed_at=proposal.confirmed_at,
                cancelled_at=proposal.cancelled_at,
                updated_at=datetime.now(UTC),
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_version(proposal.id, proposal.payload.workspace_id),
            )

    def _current_version(self, proposal_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(HandoffProposalModel.version).where(
                HandoffProposalModel.id == proposal_id,
                HandoffProposalModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


class HandoffTransitionRepository(HandoffTransitionRepositoryPort):
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffTransition | None:
        statement = select(HandoffTransitionModel).where(
            HandoffTransitionModel.id == transition_id,
            HandoffTransitionModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_transition(row) if row is not None else None

    def get_active_for_worker_item(
        self,
        worker_item_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffTransition | None:
        statement = select(HandoffTransitionModel).where(
            HandoffTransitionModel.worker_item_id == worker_item_id,
            HandoffTransitionModel.workspace_id == workspace_id,
            HandoffTransitionModel.status.in_(_active_status_values()),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_transition(row) if row is not None else None

    def get_active_for_worker_items(
        self,
        worker_item_ids: tuple[UUID, ...],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[HandoffTransition, ...]:
        ordered_ids = tuple(sorted(set(worker_item_ids), key=str))
        if not ordered_ids:
            return ()
        statement = (
            select(HandoffTransitionModel)
            .where(
                HandoffTransitionModel.worker_item_id.in_(ordered_ids),
                HandoffTransitionModel.workspace_id == workspace_id,
                HandoffTransitionModel.status.in_(_active_status_values()),
            )
            .order_by(
                HandoffTransitionModel.worker_item_id.asc(),
                HandoffTransitionModel.id.asc(),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        rows = self._session.scalars(statement).all()
        return tuple(_to_transition(row) for row in rows)

    def claim_next(
        self,
        *,
        owner_id: str,
        now: datetime,
        limit: int,
        statuses: tuple[HandoffTransitionStatus, ...] | None = None,
        lease_ttl_seconds: int | None = None,
    ) -> tuple[HandoffTransition, ...]:
        owner = owner_id.strip()
        if not owner:
            raise ValidationError("handoff claim owner_id is required")
        if limit < 1:
            raise ValidationError("handoff claim limit must be positive")
        claim_statuses = statuses or tuple(ACTIVE_HANDOFF_TRANSITION_STATUSES)
        if not claim_statuses:
            raise ValidationError("handoff claim statuses cannot be empty")
        if any(
            not isinstance(status, HandoffTransitionStatus) for status in claim_statuses
        ):
            raise ValidationError("handoff claim statuses are invalid")
        claim_lease_seconds = (
            HANDOFF_CLAIM_LEASE_SECONDS
            if lease_ttl_seconds is None
            else lease_ttl_seconds
        )
        if claim_lease_seconds < 1:
            raise ValidationError("handoff claim lease ttl must be positive")
        statement = (
            select(HandoffTransitionModel)
            .where(
                HandoffTransitionModel.status.in_(
                    _active_status_values(claim_statuses)
                ),
                or_(
                    HandoffTransitionModel.next_attempt_at.is_(None),
                    HandoffTransitionModel.next_attempt_at <= now,
                ),
                or_(
                    HandoffTransitionModel.lease_owner.is_(None),
                    HandoffTransitionModel.lease_expires_at.is_(None),
                    HandoffTransitionModel.lease_expires_at <= now,
                ),
            )
            .order_by(
                func.coalesce(
                    HandoffTransitionModel.next_attempt_at,
                    HandoffTransitionModel.created_at,
                ).asc(),
                HandoffTransitionModel.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = self._session.scalars(statement).all()
        lease_expires_at = now + timedelta(seconds=claim_lease_seconds)
        for row in rows:
            row.lease_owner = owner
            row.lease_expires_at = lease_expires_at
            row.next_attempt_at = None
            row.version += 1
            row.updated_at = now
        self._session.flush()
        return tuple(_to_transition(row) for row in rows)

    def add(self, transition: HandoffTransition) -> None:
        self._session.add(_transition_model(transition))

    def save(self, transition: HandoffTransition, previous_version: int) -> None:
        self._save(transition, previous_version, allow_target_replacement=False)

    def save_recovery(
        self, transition: HandoffTransition, previous_version: int
    ) -> None:
        self._save(transition, previous_version, allow_target_replacement=True)

    def _save(
        self,
        transition: HandoffTransition,
        previous_version: int,
        *,
        allow_target_replacement: bool,
    ) -> None:
        row = self._session.scalar(
            select(HandoffTransitionModel)
            .where(
                HandoffTransitionModel.id == transition.id,
                HandoffTransitionModel.workspace_id == transition.workspace_id,
            )
            .with_for_update()
        )
        if row is None:
            raise VersionConflict(previous_version, 0)
        if row.version != previous_version:
            raise VersionConflict(previous_version, row.version)
        if not _transition_immutable_matches(
            row,
            transition,
            allow_target_replacement=allow_target_replacement,
        ):
            raise ValidationError("handoff transition source facts are immutable")
        result = self._session.execute(
            update(HandoffTransitionModel)
            .where(
                HandoffTransitionModel.id == transition.id,
                HandoffTransitionModel.workspace_id == transition.workspace_id,
                HandoffTransitionModel.version == previous_version,
            )
            .values(
                target_worker_id=transition.target_worker_id,
                target_runtime_snapshot=dict(transition.target_runtime_snapshot),
                target_execution_id=transition.target_execution_id,
                target_session_id=transition.target_session_id,
                target_workspace_manifest_id=transition.target_workspace_manifest_id,
                handoff_context_bundle_id=transition.handoff_context_bundle_id,
                target_context_package_id=transition.target_context_package_id,
                target_context_package_hash=transition.target_context_package_hash,
                status=transition.status.value,
                phase=transition.phase.value,
                writer_state=transition.writer_state.value,
                source_checkpoint_sequence=transition.source_checkpoint_sequence,
                source_checkpoint_facts=_optional_dict(
                    transition.source_checkpoint_facts
                ),
                source_stop_sequence=transition.source_stop_sequence,
                source_stop_facts=_optional_dict(transition.source_stop_facts),
                source_workspace_freeze_operation_id=transition.source_workspace_freeze_operation_id,
                source_workspace_freeze_facts=_optional_dict(
                    transition.source_workspace_freeze_facts
                ),
                failure_code=transition.failure_code,
                blocked_operation_id=transition.blocked_operation_id,
                logical_attempt=transition.logical_attempt,
                lease_owner=transition.lease_owner,
                lease_expires_at=transition.lease_expires_at,
                next_attempt_at=transition.next_attempt_at,
                version=transition.version,
                started_at=transition.started_at,
                completed_at=transition.completed_at,
                updated_at=datetime.now(UTC),
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_version(transition.id, transition.workspace_id),
            )

    def _current_version(self, transition_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(HandoffTransitionModel.version).where(
                HandoffTransitionModel.id == transition_id,
                HandoffTransitionModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


class HandoffContextBundleRepository(HandoffContextBundleRepositoryPort):
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        bundle_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffContextBundle | None:
        statement = select(HandoffContextBundleModel).where(
            HandoffContextBundleModel.id == bundle_id,
            HandoffContextBundleModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_bundle(row) if row is not None else None

    def get_by_attempt(
        self,
        transition_id: UUID,
        logical_attempt: int,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffContextBundle | None:
        statement = select(HandoffContextBundleModel).where(
            HandoffContextBundleModel.handoff_transition_id == transition_id,
            HandoffContextBundleModel.logical_attempt == logical_attempt,
            HandoffContextBundleModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_bundle(row) if row is not None else None

    def add_append_only(self, bundle: HandoffContextBundle) -> None:
        existing = self.get(bundle.id, bundle.workspace_id)
        if existing is not None:
            if existing == bundle:
                return
            raise ValidationError("handoff context bundle is immutable")
        existing_attempt = self.get_by_attempt(
            bundle.handoff_transition_id,
            bundle.logical_attempt,
            bundle.workspace_id,
        )
        if existing_attempt is not None:
            raise ValidationError("handoff context bundle attempt is immutable")
        self._session.add(
            HandoffContextBundleModel(
                id=bundle.id,
                schema_version=bundle.schema_version,
                workspace_id=bundle.workspace_id,
                worker_item_id=bundle.worker_item_id,
                handoff_transition_id=bundle.handoff_transition_id,
                logical_attempt=bundle.logical_attempt,
                recovery_decision_id=bundle.recovery_decision_id,
                source_execution_id=bundle.source_execution_id,
                source_session_id=bundle.source_session_id,
                source_checkpoint_sequence=bundle.source_checkpoint_sequence,
                source_last_accepted_sequence=bundle.source_last_accepted_sequence,
                source_context_package_id=bundle.source_context_package_id,
                source_context_package_hash=bundle.source_context_package_hash,
                source_worker_context_version=bundle.source_worker_context_version,
                target_execution_id=bundle.target_execution_id,
                target_worker_id=bundle.target_worker_id,
                target_runtime_snapshot_hash=bundle.target_runtime_snapshot_hash,
                target_context_version=bundle.target_context_version,
                source_workspace_manifest_id=bundle.source_workspace_manifest_id,
                source_workspace_version=bundle.source_workspace_version,
                workspace_location_ref_hash=bundle.workspace_location_ref_hash,
                repository_state=[dict(item) for item in bundle.repository_state],
                artifact_refs=list(bundle.artifact_refs),
                evidence_refs=list(bundle.evidence_refs),
                handoff_reason=bundle.handoff_reason,
                handoff_instruction=bundle.handoff_instruction,
                delivered_input_refs=[
                    str(item) for item in bundle.delivered_input_refs
                ],
                transferable_input_refs=[
                    str(item) for item in bundle.transferable_input_refs
                ],
                excluded_input_refs=[str(item) for item in bundle.excluded_input_refs],
                bundle_hash=bundle.bundle_hash,
                version=1,
                created_at=bundle.created_at,
            )
        )

    def list_for_transition(
        self, transition_id: UUID, workspace_id: UUID
    ) -> tuple[HandoffContextBundle, ...]:
        rows = self._session.scalars(
            select(HandoffContextBundleModel)
            .where(
                HandoffContextBundleModel.handoff_transition_id == transition_id,
                HandoffContextBundleModel.workspace_id == workspace_id,
            )
            .order_by(
                HandoffContextBundleModel.logical_attempt.asc(),
                HandoffContextBundleModel.id.asc(),
            )
        ).all()
        return tuple(_to_bundle(row) for row in rows)


class HandoffRecoveryDecisionRepository(HandoffRecoveryDecisionRepositoryPort):
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        decision_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffRecoveryDecision | None:
        statement = select(HandoffRecoveryDecisionModel).where(
            HandoffRecoveryDecisionModel.id == decision_id,
            HandoffRecoveryDecisionModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_recovery_decision(row) if row is not None else None

    def add_append_only(
        self, decision: HandoffRecoveryDecision, workspace_id: UUID
    ) -> None:
        existing = self.get(decision.id, workspace_id)
        if existing is not None:
            if existing == decision:
                return
            raise ValidationError("handoff recovery decision is immutable")
        self._session.add(
            HandoffRecoveryDecisionModel(
                id=decision.id,
                workspace_id=workspace_id,
                handoff_transition_id=decision.handoff_transition_id,
                expected_transition_version=decision.expected_transition_version,
                strategy=decision.strategy.value,
                actor_type=decision.actor_type,
                actor_id=decision.actor_id,
                source_channel=decision.source_channel.value,
                reason=decision.reason,
                evidence_refs=list(decision.evidence_refs),
                replacement_target_snapshot=_optional_dict(
                    decision.replacement_target_snapshot
                ),
                resulting_operation_refs=[
                    str(item) for item in decision.resulting_operation_refs
                ],
                version=1,
                created_at=decision.created_at,
            )
        )

    def list_for_transition(
        self, transition_id: UUID, workspace_id: UUID
    ) -> tuple[HandoffRecoveryDecision, ...]:
        rows = self._session.scalars(
            select(HandoffRecoveryDecisionModel)
            .where(
                HandoffRecoveryDecisionModel.handoff_transition_id == transition_id,
                HandoffRecoveryDecisionModel.workspace_id == workspace_id,
            )
            .order_by(
                HandoffRecoveryDecisionModel.created_at.asc(),
                HandoffRecoveryDecisionModel.id.asc(),
            )
        ).all()
        return tuple(_to_recovery_decision(row) for row in rows)


class HandoffWorkspaceTransferRepository(HandoffWorkspaceTransferRepositoryPort):
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        transfer_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffWorkspaceTransfer | None:
        statement = select(ExecutionWorkspaceTransferModel).where(
            ExecutionWorkspaceTransferModel.id == transfer_id,
            ExecutionWorkspaceTransferModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_transfer(row) if row is not None else None

    def get_by_attempt(
        self,
        transition_id: UUID,
        logical_attempt: int,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffWorkspaceTransfer | None:
        statement = select(ExecutionWorkspaceTransferModel).where(
            ExecutionWorkspaceTransferModel.handoff_transition_id == transition_id,
            ExecutionWorkspaceTransferModel.logical_attempt == logical_attempt,
            ExecutionWorkspaceTransferModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_transfer(row) if row is not None else None

    def get_by_operation(
        self,
        operation_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffWorkspaceTransfer | None:
        statement = select(ExecutionWorkspaceTransferModel).where(
            ExecutionWorkspaceTransferModel.operation_id == operation_id,
            ExecutionWorkspaceTransferModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_transfer(row) if row is not None else None

    def add_append_only(
        self, transfer: HandoffWorkspaceTransfer, workspace_id: UUID
    ) -> None:
        existing = self.get_by_operation(transfer.operation_id, workspace_id)
        if existing is not None:
            if existing == transfer:
                return
            raise ValidationError("workspace transfer provenance is immutable")
        existing_attempt = self.get_by_attempt(
            transfer.transition_id,
            transfer.logical_attempt,
            workspace_id,
        )
        if existing_attempt is not None:
            raise ValidationError("workspace transfer attempt is immutable")
        self._session.add(
            ExecutionWorkspaceTransferModel(
                id=transfer.operation_id,
                workspace_id=workspace_id,
                handoff_transition_id=transfer.transition_id,
                logical_attempt=transfer.logical_attempt,
                operation_id=transfer.operation_id,
                source_manifest_id=transfer.source_manifest_id,
                target_manifest_id=transfer.target_manifest_id,
                source_execution_id=transfer.source_execution_id,
                target_execution_id=transfer.target_execution_id,
                source_fencing_token=transfer.source_fencing_token,
                target_fencing_token=transfer.target_fencing_token,
                location_ref_hash=transfer.location_ref_hash,
                repository_state_digest=transfer.repository_state_digest,
                transfer_mode=HandoffTransferMode.REUSE_EXACT.value,
                provider_operation_id=transfer.operation_id,
                status=transfer.status.value,
                failure_code=transfer.failure_code,
                version=transfer.version,
                created_at=transfer.created_at,
                updated_at=transfer.updated_at,
            )
        )

    def save(self, transfer: HandoffWorkspaceTransfer, previous_version: int) -> None:
        row = self._session.scalar(
            select(ExecutionWorkspaceTransferModel)
            .where(
                ExecutionWorkspaceTransferModel.operation_id == transfer.operation_id
            )
            .with_for_update()
        )
        if row is None:
            raise VersionConflict(previous_version, 0)
        if row.version != previous_version:
            raise VersionConflict(previous_version, row.version)
        if not _transfer_immutable_matches(row, transfer):
            raise ValidationError("workspace transfer provenance is immutable")
        result = self._session.execute(
            update(ExecutionWorkspaceTransferModel)
            .where(
                ExecutionWorkspaceTransferModel.id == row.id,
                ExecutionWorkspaceTransferModel.workspace_id == row.workspace_id,
                ExecutionWorkspaceTransferModel.version == previous_version,
            )
            .values(
                target_manifest_id=transfer.target_manifest_id,
                status=transfer.status.value,
                failure_code=transfer.failure_code,
                version=transfer.version,
                updated_at=transfer.updated_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(previous_version, row.version)

    def list_for_transition(
        self, transition_id: UUID, workspace_id: UUID
    ) -> tuple[HandoffWorkspaceTransfer, ...]:
        rows = self._session.scalars(
            select(ExecutionWorkspaceTransferModel)
            .where(
                ExecutionWorkspaceTransferModel.handoff_transition_id == transition_id,
                ExecutionWorkspaceTransferModel.workspace_id == workspace_id,
            )
            .order_by(
                ExecutionWorkspaceTransferModel.logical_attempt.asc(),
                ExecutionWorkspaceTransferModel.id.asc(),
            )
        ).all()
        return tuple(_to_transfer(row) for row in rows)


def _active_status_values(
    statuses: tuple[HandoffTransitionStatus, ...] | None = None,
) -> tuple[str, ...]:
    values = statuses or tuple(ACTIVE_HANDOFF_TRANSITION_STATUSES)
    return tuple(item.value for item in values)


def _optional_dict(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    return dict(value)  # type: ignore[arg-type]


def _transition_model(transition: HandoffTransition) -> HandoffTransitionModel:
    return HandoffTransitionModel(
        id=transition.id,
        workspace_id=transition.workspace_id,
        worker_item_id=transition.worker_item_id,
        proposal_id=transition.proposal_id,
        source_execution_id=transition.source_execution_id,
        source_session_id=transition.source_session_id,
        source_workspace_manifest_id=transition.source_workspace_manifest_id,
        target_worker_id=transition.target_worker_id,
        target_runtime_snapshot=dict(transition.target_runtime_snapshot),
        target_execution_id=transition.target_execution_id,
        target_session_id=transition.target_session_id,
        target_workspace_manifest_id=transition.target_workspace_manifest_id,
        handoff_context_bundle_id=transition.handoff_context_bundle_id,
        target_context_version=transition.target_context_version,
        target_context_package_id=transition.target_context_package_id,
        target_context_package_hash=transition.target_context_package_hash,
        status=transition.status.value,
        phase=transition.phase.value,
        writer_state=transition.writer_state.value,
        source_checkpoint_sequence=transition.source_checkpoint_sequence,
        source_checkpoint_facts=_optional_dict(transition.source_checkpoint_facts),
        source_stop_sequence=transition.source_stop_sequence,
        source_stop_facts=_optional_dict(transition.source_stop_facts),
        source_workspace_freeze_operation_id=transition.source_workspace_freeze_operation_id,
        source_workspace_freeze_facts=_optional_dict(
            transition.source_workspace_freeze_facts
        ),
        source_input_cutoff_sequence=transition.source_input_cutoff_sequence,
        failure_code=transition.failure_code,
        blocked_operation_id=transition.blocked_operation_id,
        logical_attempt=transition.logical_attempt,
        lease_owner=transition.lease_owner,
        lease_expires_at=transition.lease_expires_at,
        next_attempt_at=transition.next_attempt_at,
        version=transition.version,
        confirmed_by_type=transition.confirmed_by_type,
        confirmed_by_id=transition.confirmed_by_id,
        confirmed_source_channel=transition.confirmed_source_channel.value,
        confirmed_at=transition.confirmed_at,
        started_at=transition.started_at,
        completed_at=transition.completed_at,
    )


def _transition_immutable_matches(
    row: HandoffTransitionModel,
    transition: HandoffTransition,
    *,
    allow_target_replacement: bool = False,
) -> bool:
    source_matches = (
        row.workspace_id == transition.workspace_id
        and row.worker_item_id == transition.worker_item_id
        and row.proposal_id == transition.proposal_id
        and row.source_execution_id == transition.source_execution_id
        and row.source_session_id == transition.source_session_id
        and row.source_workspace_manifest_id == transition.source_workspace_manifest_id
        and row.target_context_version == transition.target_context_version
        and row.source_input_cutoff_sequence == transition.source_input_cutoff_sequence
        and row.confirmed_by_type == transition.confirmed_by_type
        and row.confirmed_by_id == transition.confirmed_by_id
        and row.confirmed_source_channel == transition.confirmed_source_channel.value
        and row.confirmed_at == transition.confirmed_at
    )
    if not source_matches:
        return False
    if allow_target_replacement:
        return True
    return (
        row.target_worker_id == transition.target_worker_id
        and row.target_runtime_snapshot == dict(transition.target_runtime_snapshot)
    )


def _to_proposal(row: HandoffProposalModel) -> HandoffProposal:
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
        transfer_mode=HandoffTransferMode(row.transfer_mode),
        handoff_reason=row.handoff_reason,
        handoff_instruction=row.handoff_instruction,
        input_plan=dict(row.input_plan),
        impact_summary=dict(row.impact_summary),
    )
    return HandoffProposal(
        id=row.id,
        payload=payload,
        proposal_hash=row.proposal_hash,
        status=HandoffProposalStatus(row.status),
        stale_reason=row.stale_reason,
        version=row.version,
        created_by_type=row.created_by_type,
        created_by_id=row.created_by_id,
        source_channel=HandoffSourceChannel(row.source_channel),
        created_at=row.created_at,
        expires_at=row.expires_at,
        confirmed_at=row.confirmed_at,
        cancelled_at=row.cancelled_at,
    )


def _to_transition(row: HandoffTransitionModel) -> HandoffTransition:
    return HandoffTransition(
        id=row.id,
        workspace_id=row.workspace_id,
        worker_item_id=row.worker_item_id,
        proposal_id=row.proposal_id,
        source_execution_id=row.source_execution_id,
        source_session_id=row.source_session_id,
        source_workspace_manifest_id=row.source_workspace_manifest_id,
        target_worker_id=row.target_worker_id,
        target_runtime_snapshot=dict(row.target_runtime_snapshot),
        target_execution_id=row.target_execution_id,
        target_session_id=row.target_session_id,
        target_workspace_manifest_id=row.target_workspace_manifest_id,
        handoff_context_bundle_id=row.handoff_context_bundle_id,
        target_context_version=row.target_context_version,
        target_context_package_id=row.target_context_package_id,
        target_context_package_hash=row.target_context_package_hash,
        status=HandoffTransitionStatus(row.status),
        phase=HandoffPhase(row.phase),
        writer_state=HandoffWriterState(row.writer_state),
        source_checkpoint_sequence=row.source_checkpoint_sequence,
        source_checkpoint_facts=_optional_dict(row.source_checkpoint_facts),
        source_stop_sequence=row.source_stop_sequence,
        source_stop_facts=_optional_dict(row.source_stop_facts),
        source_workspace_freeze_operation_id=row.source_workspace_freeze_operation_id,
        source_workspace_freeze_facts=_optional_dict(row.source_workspace_freeze_facts),
        source_input_cutoff_sequence=row.source_input_cutoff_sequence,
        failure_code=row.failure_code,
        blocked_operation_id=row.blocked_operation_id,
        logical_attempt=row.logical_attempt,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        next_attempt_at=row.next_attempt_at,
        version=row.version,
        confirmed_by_type=row.confirmed_by_type,
        confirmed_by_id=row.confirmed_by_id,
        confirmed_source_channel=HandoffSourceChannel(row.confirmed_source_channel),
        confirmed_at=row.confirmed_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _to_bundle(row: HandoffContextBundleModel) -> HandoffContextBundle:
    return HandoffContextBundle(
        id=row.id,
        schema_version=row.schema_version,
        workspace_id=row.workspace_id,
        worker_item_id=row.worker_item_id,
        handoff_transition_id=row.handoff_transition_id,
        logical_attempt=row.logical_attempt,
        recovery_decision_id=row.recovery_decision_id,
        source_execution_id=row.source_execution_id,
        source_session_id=row.source_session_id,
        source_checkpoint_sequence=row.source_checkpoint_sequence,
        source_last_accepted_sequence=row.source_last_accepted_sequence,
        source_context_package_id=row.source_context_package_id,
        source_context_package_hash=row.source_context_package_hash,
        source_worker_context_version=row.source_worker_context_version,
        target_execution_id=row.target_execution_id,
        target_worker_id=row.target_worker_id,
        target_runtime_snapshot_hash=row.target_runtime_snapshot_hash,
        target_context_version=row.target_context_version,
        source_workspace_manifest_id=row.source_workspace_manifest_id,
        source_workspace_version=row.source_workspace_version,
        workspace_location_ref_hash=row.workspace_location_ref_hash,
        repository_state=tuple(dict(item) for item in row.repository_state),
        artifact_refs=tuple(row.artifact_refs),
        evidence_refs=tuple(row.evidence_refs),
        handoff_reason=row.handoff_reason,
        handoff_instruction=row.handoff_instruction,
        delivered_input_refs=_uuid_values(row.delivered_input_refs),
        transferable_input_refs=_uuid_values(row.transferable_input_refs),
        excluded_input_refs=_uuid_values(row.excluded_input_refs),
        bundle_hash=row.bundle_hash,
        created_at=row.created_at,
    )


def _to_recovery_decision(
    row: HandoffRecoveryDecisionModel,
) -> HandoffRecoveryDecision:
    return HandoffRecoveryDecision(
        id=row.id,
        handoff_transition_id=row.handoff_transition_id,
        expected_transition_version=row.expected_transition_version,
        strategy=HandoffRecoveryStrategy(row.strategy),
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        source_channel=HandoffSourceChannel(row.source_channel),
        reason=row.reason,
        evidence_refs=tuple(row.evidence_refs),
        replacement_target_snapshot=_optional_dict(row.replacement_target_snapshot),
        resulting_operation_refs=_uuid_values(row.resulting_operation_refs),
        created_at=row.created_at,
    )


def _to_transfer(row: ExecutionWorkspaceTransferModel) -> HandoffWorkspaceTransfer:
    return HandoffWorkspaceTransfer(
        transition_id=row.handoff_transition_id,
        logical_attempt=row.logical_attempt,
        operation_id=row.operation_id,
        source_manifest_id=row.source_manifest_id,
        target_manifest_id=row.target_manifest_id,
        source_execution_id=row.source_execution_id,
        target_execution_id=row.target_execution_id,
        source_fencing_token=row.source_fencing_token,
        target_fencing_token=row.target_fencing_token,
        location_ref_hash=row.location_ref_hash,
        repository_state_digest=row.repository_state_digest,
        status=HandoffWorkspaceTransferStatus(row.status),
        failure_code=row.failure_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _transfer_immutable_matches(
    row: ExecutionWorkspaceTransferModel,
    transfer: HandoffWorkspaceTransfer,
) -> bool:
    target_manifest_can_be_bound = (
        row.target_manifest_id is None
        and transfer.target_manifest_id is not None
        and row.status == HandoffWorkspaceTransferStatus.REQUESTED.value
    )
    return (
        row.handoff_transition_id == transfer.transition_id
        and row.logical_attempt == transfer.logical_attempt
        and row.operation_id == transfer.operation_id
        and (
            row.target_manifest_id == transfer.target_manifest_id
            or target_manifest_can_be_bound
        )
        and row.source_manifest_id == transfer.source_manifest_id
        and row.source_execution_id == transfer.source_execution_id
        and row.target_execution_id == transfer.target_execution_id
        and row.source_fencing_token == transfer.source_fencing_token
        and row.target_fencing_token == transfer.target_fencing_token
        and row.location_ref_hash == transfer.location_ref_hash
        and row.repository_state_digest == transfer.repository_state_digest
        and row.provider_operation_id == transfer.operation_id
        and row.transfer_mode == HandoffTransferMode.REUSE_EXACT.value
    )


def _uuid_values(values: Iterable[object]) -> tuple[UUID, ...]:
    return tuple(
        value if isinstance(value, UUID) else UUID(str(value)) for value in values
    )


WorkspaceTransferRepository = HandoffWorkspaceTransferRepository
ExecutionWorkspaceTransferRepository = HandoffWorkspaceTransferRepository
