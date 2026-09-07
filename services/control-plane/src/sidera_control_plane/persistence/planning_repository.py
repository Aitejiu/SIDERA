from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.modules import PlannerAdapterCapabilityProfile
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningClarification,
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
    PlanningRun,
    PlanningRunLease,
    PlanningRunLeaseStatus,
    PlanningRunStatus,
    PlanningUsage,
    ProposalMaterialization,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileSnapshot,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    PlanningProposalPayload,
    PlanningProposalPayloadV4,
    PlanningProposalPayloadV5,
    PlanningProposalPayloadV6,
    ProposalDiff,
    ProposalRevision,
    derive_proposal_diff,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
    PlanningRevisionProposalPayloadV4,
    PlanningRevisionProposalPayloadV5,
    PlanningRevisionProposalPayloadV6,
)
from sidera_control_plane.domain.revision_diff import NormalizedRevisionDiff
from sidera_control_plane.persistence.models import (
    PlanningBriefRevisionModel,
    PlanningClarificationModel,
    PlanningProfileModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunLeaseModel,
    PlanningRunModel,
    ProposalMaterializationModel,
)


class PlanningProfileRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, profile: PlanningProfile) -> None:
        self._session.add(
            PlanningProfileModel(
                id=profile.id,
                workspace_id=profile.workspace_id,
                profile_key=profile.profile_key,
                version=profile.version,
                display_name=profile.display_name,
                planner_module_instance_id=profile.planner_module_instance_id,
                planner_module_config_version=profile.planner_module_config_version,
                planner_interface_version=profile.planner_interface_version,
                status=profile.status.value,
                user_selectable=profile.user_selectable,
                policy=_profile_policy(profile),
            )
        )

    def get(
        self,
        profile_id: UUID,
        workspace_id: UUID,
        *,
        version: int | None = None,
    ) -> PlanningProfile | None:
        statement = select(PlanningProfileModel).where(
            PlanningProfileModel.id == profile_id,
            PlanningProfileModel.workspace_id == workspace_id,
        )
        if version is not None:
            statement = statement.where(PlanningProfileModel.version == version)
        row = self._session.scalar(statement)
        return _to_profile(row) if row is not None else None

    def list_selectable(self, workspace_id: UUID) -> tuple[PlanningProfile, ...]:
        rows = self._session.scalars(
            select(PlanningProfileModel)
            .where(
                PlanningProfileModel.workspace_id == workspace_id,
                PlanningProfileModel.status == PlanningProfileStatus.ACTIVE.value,
                PlanningProfileModel.user_selectable.is_(True),
            )
            .order_by(
                PlanningProfileModel.profile_key.asc(),
                PlanningProfileModel.version.desc(),
                PlanningProfileModel.id.asc(),
            )
        ).all()
        return tuple(_to_profile(row) for row in rows)


class PlanningRequestRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, request: PlanningRequest) -> None:
        row = PlanningRequestModel(
            id=request.id,
            workspace_id=request.workspace_id,
            kind=request.kind.value,
            status=request.status.value,
            root_task_id=request.root_task_id,
            base_plan_snapshot_id=request.base_plan_snapshot_id,
            base_plan_generation=request.base_plan_generation,
            base_plan_content_hash=request.base_plan_content_hash,
            base_root_task_version=request.base_root_task_version,
            base_captured_at=request.base_captured_at,
            conflict_reason=request.conflict_reason,
            current_brief_revision=None,
            current_proposal_revision=None,
            current_proposal_content_hash=None,
            planning_profile_id=request.planning_profile_id,
            planning_profile_version=request.planning_profile_version,
            profile_snapshot=_snapshot_to_mapping(request.profile_snapshot),
            source_type=request.source_type,
            source_ref=request.source_ref,
            actor=request.actor,
            correlation_id=request.correlation_id,
            version=request.version,
        )
        self._session.add(row)
        self._session.flush()
        for brief in request.briefs:
            self._session.add(_brief_model(brief, request.workspace_id))
        self._session.flush()
        for run in request.runs:
            self._session.add(_run_model(run))
        for clarification in request.clarifications:
            self._session.add(
                _clarification_model(
                    clarification,
                    request.workspace_id,
                    self._next_clarification_sequence(
                        request.id, request.workspace_id
                    ),
                )
            )
        row.current_brief_revision = request.current_brief_revision
        row.current_proposal_revision = request.current_proposal_revision
        row.current_proposal_content_hash = request.current_proposal_content_hash

    def get(
        self,
        request_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRequest | None:
        statement = select(PlanningRequestModel).where(
            PlanningRequestModel.id == request_id,
            PlanningRequestModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        if row is None:
            return None
        return _to_request(
            row,
            briefs=_list_briefs(self._session, request_id, workspace_id),
            runs=_list_runs(self._session, request_id, workspace_id, row.profile_snapshot),
            clarifications=tuple(
                _to_clarification(item)
                for item in self._session.scalars(
                    select(PlanningClarificationModel)
                    .where(
                        PlanningClarificationModel.planning_request_id == request_id,
                        PlanningClarificationModel.workspace_id == workspace_id,
                    )
                    .order_by(PlanningClarificationModel.sequence.asc())
                ).all()
            ),
        )

    def list_by_source_ref(
        self, workspace_id: UUID, source_type: str, source_ref: str
    ) -> tuple[PlanningRequest, ...]:
        rows = self._session.scalars(
            select(PlanningRequestModel)
            .where(
                PlanningRequestModel.workspace_id == workspace_id,
                PlanningRequestModel.source_type == source_type,
                PlanningRequestModel.source_ref == source_ref,
            )
            .order_by(
                PlanningRequestModel.created_at.asc(),
                PlanningRequestModel.id.asc(),
            )
        ).all()
        return tuple(
            _to_request(
                row,
                briefs=_list_briefs(self._session, row.id, workspace_id),
                runs=_list_runs(self._session, row.id, workspace_id, row.profile_snapshot),
                clarifications=tuple(
                    _to_clarification(item)
                    for item in self._session.scalars(
                        select(PlanningClarificationModel)
                        .where(
                            PlanningClarificationModel.planning_request_id == row.id,
                            PlanningClarificationModel.workspace_id == workspace_id,
                        )
                        .order_by(PlanningClarificationModel.sequence.asc())
                    ).all()
                ),
            )
            for row in rows
        )

    def get_active_revision_for_root(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRequest | None:
        statement = select(PlanningRequestModel).where(
            PlanningRequestModel.root_task_id == root_task_id,
            PlanningRequestModel.workspace_id == workspace_id,
            PlanningRequestModel.kind == PlanningRequestKind.REVISION.value,
            PlanningRequestModel.status.in_(
                (
                    PlanningRequestStatus.SUBMITTED.value,
                    PlanningRequestStatus.PLANNING.value,
                    PlanningRequestStatus.WAITING_FOR_USER.value,
                    PlanningRequestStatus.PROPOSAL_READY.value,
                    PlanningRequestStatus.FAILED.value,
                )
            ),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        if row is None:
            return None
        return _to_request(
            row,
            briefs=_list_briefs(self._session, row.id, workspace_id),
            runs=_list_runs(self._session, row.id, workspace_id, row.profile_snapshot),
            clarifications=tuple(
                _to_clarification(item)
                for item in self._session.scalars(
                    select(PlanningClarificationModel)
                    .where(
                        PlanningClarificationModel.planning_request_id == row.id,
                        PlanningClarificationModel.workspace_id == workspace_id,
                    )
                    .order_by(PlanningClarificationModel.sequence.asc())
                ).all()
            ),
        )

    def save(self, request: PlanningRequest, previous_version: int) -> None:
        result = self._session.execute(
            update(PlanningRequestModel)
            .where(
                PlanningRequestModel.id == request.id,
                PlanningRequestModel.workspace_id == request.workspace_id,
                PlanningRequestModel.version == previous_version,
            )
            .values(
                status=request.status.value,
                current_brief_revision=request.current_brief_revision,
                current_proposal_revision=request.current_proposal_revision,
                current_proposal_content_hash=request.current_proposal_content_hash,
                conflict_reason=request.conflict_reason,
                version=request.version,
                updated_at=datetime.now(UTC),
            )
        )
        _assert_saved(
            result,
            self._current_version(request.id, request.workspace_id),
            previous_version,
        )

    def _next_clarification_sequence(
        self, request_id: UUID, workspace_id: UUID
    ) -> int:
        current = self._session.scalar(
            select(func.max(PlanningClarificationModel.sequence)).where(
                PlanningClarificationModel.planning_request_id == request_id,
                PlanningClarificationModel.workspace_id == workspace_id,
            )
        )
        return (current or 0) + 1

    def _current_version(self, request_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(PlanningRequestModel.version).where(
                PlanningRequestModel.id == request_id,
                PlanningRequestModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


class PlanningRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: PlanningRun) -> None:
        self._session.add(_run_model(run))

    def get(
        self,
        run_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRun | None:
        statement = select(PlanningRunModel).where(
            PlanningRunModel.id == run_id,
            PlanningRunModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        if row is None:
            return None
        snapshot = self._snapshot_for_request(row.planning_request_id, workspace_id)
        return _to_run(row, snapshot)

    def save(self, run: PlanningRun, previous_version: int) -> None:
        result = self._session.execute(
            update(PlanningRunModel)
            .where(
                PlanningRunModel.id == run.id,
                PlanningRunModel.workspace_id == run.workspace_id,
                PlanningRunModel.version == previous_version,
            )
            .values(
                status=run.status.value,
                result_kind=run.structured_result_type,
                usage=_usage_to_mapping(run.usage),
                provider_ref=run.provider_ref,
                error_code=run.error_code,
                error_message=run.error_message,
                diagnostic_refs=(
                    [run.diagnostic_artifact_ref]
                    if run.diagnostic_artifact_ref is not None
                    else []
                ),
                started_at=run.started_at,
                finished_at=run.finished_at,
                version=run.version,
                updated_at=datetime.now(UTC),
            )
        )
        _assert_saved(
            result,
            self._current_version(run.id, run.workspace_id),
            previous_version,
        )

    def claim_queued(self, *, now: datetime, limit: int) -> tuple[PlanningRun, ...]:
        statement = (
            select(PlanningRunModel, PlanningRequestModel.profile_snapshot)
            .join(
                PlanningRequestModel,
                (PlanningRequestModel.id == PlanningRunModel.planning_request_id)
                & (PlanningRequestModel.workspace_id == PlanningRunModel.workspace_id),
            )
            .where(
                PlanningRunModel.status == PlanningRunStatus.QUEUED.value,
                PlanningRunModel.queued_at <= now,
            )
            .order_by(PlanningRunModel.queued_at.asc(), PlanningRunModel.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = self._session.execute(statement).all()
        return tuple(_to_run(row, snapshot) for row, snapshot in rows)

    def claim_failed(self, *, now: datetime, limit: int) -> tuple[PlanningRun, ...]:
        statement = (
            select(PlanningRunModel, PlanningRequestModel.profile_snapshot)
            .join(
                PlanningRequestModel,
                (PlanningRequestModel.id == PlanningRunModel.planning_request_id)
                & (PlanningRequestModel.workspace_id == PlanningRunModel.workspace_id),
            )
            .where(
                PlanningRunModel.status == PlanningRunStatus.FAILED.value,
                PlanningRunModel.finished_at <= now,
            )
            .order_by(
                PlanningRunModel.finished_at.asc(), PlanningRunModel.id.asc()
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = self._session.execute(statement).all()
        return tuple(_to_run(row, snapshot) for row, snapshot in rows)

    def _snapshot_for_request(
        self, request_id: UUID, workspace_id: UUID
    ) -> PlanningProfileSnapshot:
        snapshot = self._session.scalar(
            select(PlanningRequestModel.profile_snapshot).where(
                PlanningRequestModel.id == request_id,
                PlanningRequestModel.workspace_id == workspace_id,
            )
        )
        if snapshot is None:
            raise ValidationError("planning run request snapshot is missing")
        return _snapshot_from_mapping(snapshot)

    def _current_version(self, run_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(PlanningRunModel.version).where(
                PlanningRunModel.id == run_id,
                PlanningRunModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


class PlanningRunLeaseRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, lease: PlanningRunLease) -> None:
        self._session.add(
            PlanningRunLeaseModel(
                id=lease.id,
                workspace_id=lease.workspace_id,
                planning_run_id=lease.planning_run_id,
                owner_id=lease.owner_id,
                status=lease.status.value,
                fencing_token=lease.fencing_token,
                acquired_at=lease.acquired_at,
                expires_at=lease.expires_at,
                released_at=lease.released_at,
                version=lease.version,
            )
        )

    def get(
        self,
        lease_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRunLease | None:
        statement = select(PlanningRunLeaseModel).where(
            PlanningRunLeaseModel.id == lease_id,
            PlanningRunLeaseModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_lease(row) if row is not None else None

    def get_active_for_run(
        self,
        run_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRunLease | None:
        statement = select(PlanningRunLeaseModel).where(
            PlanningRunLeaseModel.planning_run_id == run_id,
            PlanningRunLeaseModel.workspace_id == workspace_id,
            PlanningRunLeaseModel.status == PlanningRunLeaseStatus.ACTIVE.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_lease(row) if row is not None else None

    def save(self, lease: PlanningRunLease, previous_version: int) -> None:
        result = self._session.execute(
            update(PlanningRunLeaseModel)
            .where(
                PlanningRunLeaseModel.id == lease.id,
                PlanningRunLeaseModel.workspace_id == lease.workspace_id,
                PlanningRunLeaseModel.version == previous_version,
            )
            .values(
                owner_id=lease.owner_id,
                status=lease.status.value,
                fencing_token=lease.fencing_token,
                acquired_at=lease.acquired_at,
                expires_at=lease.expires_at,
                released_at=lease.released_at,
                version=lease.version,
                updated_at=datetime.now(UTC),
            )
        )
        _assert_saved(
            result,
            self._current_version(lease.id, lease.workspace_id),
            previous_version,
        )

    def claim_expired(
        self, *, now: datetime, limit: int
    ) -> tuple[PlanningRunLease, ...]:
        rows = self._session.scalars(
            select(PlanningRunLeaseModel)
            .where(
                PlanningRunLeaseModel.status == PlanningRunLeaseStatus.ACTIVE.value,
                PlanningRunLeaseModel.expires_at <= now,
            )
            .order_by(
                PlanningRunLeaseModel.expires_at.asc(),
                PlanningRunLeaseModel.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        return tuple(_to_lease(row) for row in rows)

    def _current_version(self, lease_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(PlanningRunLeaseModel.version).where(
                PlanningRunLeaseModel.id == lease_id,
                PlanningRunLeaseModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


class PlanningRecordRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_brief(self, brief: PlanningBriefRevision) -> None:
        workspace_id = _workspace_for_request(self._session, brief.planning_request_id)
        self._session.add(_brief_model(brief, workspace_id))

    def get_brief(
        self, request_id: UUID, workspace_id: UUID, revision: int
    ) -> PlanningBriefRevision | None:
        row = self._session.scalar(
            select(PlanningBriefRevisionModel).where(
                PlanningBriefRevisionModel.planning_request_id == request_id,
                PlanningBriefRevisionModel.workspace_id == workspace_id,
                PlanningBriefRevisionModel.revision == revision,
            )
        )
        return _to_brief(row) if row is not None else None

    def list_briefs(
        self, request_id: UUID, workspace_id: UUID
    ) -> tuple[PlanningBriefRevision, ...]:
        return _list_briefs(self._session, request_id, workspace_id)

    def add_proposal(self, proposal: ProposalRevision, workspace_id: UUID) -> None:
        mapping = proposal.payload.to_mapping()
        plan_template = {
            key: value
            for key, value in mapping.items()
            if key not in {"schema_version", "kind", "decision_summary", "impact_policy", "execution_intents"}
        }
        self._session.add(
            PlanningProposalRevisionModel(
                id=proposal.id,
                planning_request_id=proposal.planning_request_id,
                revision=proposal.revision,
                workspace_id=workspace_id,
                brief_revision=proposal.brief_revision,
                planning_run_id=proposal.planning_run_id,
                proposal_schema_version=proposal.proposal_schema_version,
                kind=proposal.kind.value,
                profile_id=proposal.profile_descriptor.profile_id,
                profile_version=proposal.profile_descriptor.profile_version,
                planner_module_instance_id=proposal.profile_descriptor.planner_module_instance_id,
                planner_module_config_version=proposal.profile_descriptor.planner_module_config_version,
                decision_summary=_json_value(mapping["decision_summary"]),
                plan_template=_json_value(plan_template),
                execution_intents=_json_value(mapping["execution_intents"]),
                impact_policy=_json_value(mapping["impact_policy"]),
                derived_diff=_json_value(_diff_to_mapping(proposal.derived_diff)),
                canonical_payload=_json_value(json.loads(proposal.canonical_payload)),
                content_hash=proposal.content_hash,
                created_at=proposal.created_at,
            )
        )

    def get_proposal(
        self, proposal_id: UUID, workspace_id: UUID
    ) -> ProposalRevision | None:
        row = self._session.scalar(
            select(PlanningProposalRevisionModel).where(
                PlanningProposalRevisionModel.id == proposal_id,
                PlanningProposalRevisionModel.workspace_id == workspace_id,
            )
        )
        return _to_proposal(row) if row is not None else None

    def get_proposal_revision(
        self, request_id: UUID, workspace_id: UUID, revision: int
    ) -> ProposalRevision | None:
        row = self._session.scalar(
            select(PlanningProposalRevisionModel).where(
                PlanningProposalRevisionModel.planning_request_id == request_id,
                PlanningProposalRevisionModel.workspace_id == workspace_id,
                PlanningProposalRevisionModel.revision == revision,
            )
        )
        return _to_proposal(row) if row is not None else None

    def list_proposals(
        self, request_id: UUID, workspace_id: UUID
    ) -> tuple[ProposalRevision, ...]:
        rows = self._session.scalars(
            select(PlanningProposalRevisionModel)
            .where(
                PlanningProposalRevisionModel.planning_request_id == request_id,
                PlanningProposalRevisionModel.workspace_id == workspace_id,
            )
            .order_by(
                PlanningProposalRevisionModel.revision.asc(),
                PlanningProposalRevisionModel.id.asc(),
            )
        ).all()
        return tuple(_to_proposal(row) for row in rows)

    def get_current_proposal(
        self, request_id: UUID, workspace_id: UUID
    ) -> ProposalRevision | None:
        revision = self._session.scalar(
            select(PlanningRequestModel.current_proposal_revision).where(
                PlanningRequestModel.id == request_id,
                PlanningRequestModel.workspace_id == workspace_id,
            )
        )
        if revision is None:
            return None
        return self.get_proposal_revision(request_id, workspace_id, revision)

    def add_clarification(self, clarification: PlanningClarification) -> None:
        workspace_id = _workspace_for_request(
            self._session, clarification.planning_request_id
        )
        sequence = self._next_clarification_sequence(
            clarification.planning_request_id, workspace_id
        )
        self._session.add(
            _clarification_model(clarification, workspace_id, sequence)
        )

    def list_pending_clarifications(
        self, request_id: UUID, workspace_id: UUID
    ) -> tuple[PlanningClarification, ...]:
        rows = self._session.scalars(
            select(PlanningClarificationModel)
            .where(
                PlanningClarificationModel.planning_request_id == request_id,
                PlanningClarificationModel.workspace_id == workspace_id,
                PlanningClarificationModel.blocking.is_(True),
                PlanningClarificationModel.answer.is_(None),
            )
            .order_by(PlanningClarificationModel.sequence.asc())
        ).all()
        return tuple(_to_clarification(row) for row in rows)

    def save_clarification(
        self, clarification: PlanningClarification, previous_version: int
    ) -> None:
        result = self._session.execute(
            update(PlanningClarificationModel)
            .where(
                PlanningClarificationModel.id == clarification.id,
                PlanningClarificationModel.workspace_id
                == _workspace_for_request(
                    self._session, clarification.planning_request_id
                ),
                PlanningClarificationModel.version == previous_version,
            )
            .values(
                answer=clarification.answer,
                answered_by=clarification.answered_by,
                answered_at=clarification.answered_at,
                answer_brief_revision=clarification.answer_brief_revision,
                version=clarification.version,
                updated_at=datetime.now(UTC),
            )
        )
        actual = self._session.scalar(
            select(PlanningClarificationModel.version).where(
                PlanningClarificationModel.id == clarification.id
            )
        )
        _assert_saved(result, actual or 0, previous_version)

    def _next_clarification_sequence(self, request_id: UUID, workspace_id: UUID) -> int:
        current = self._session.scalar(
            select(func.max(PlanningClarificationModel.sequence)).where(
                PlanningClarificationModel.planning_request_id == request_id,
                PlanningClarificationModel.workspace_id == workspace_id,
            )
        )
        return (current or 0) + 1


class ProposalMaterializationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, materialization: ProposalMaterialization) -> None:
        self._session.add(
            ProposalMaterializationModel(
                id=materialization.id,
                workspace_id=materialization.workspace_id,
                planning_request_id=materialization.planning_request_id,
                kind=materialization.kind.value,
                proposal_revision_id=materialization.proposal_revision_id,
                root_task_id=materialization.root_task_id,
                plan_snapshot_id=materialization.plan_snapshot_id,
                plan_generation=materialization.plan_generation,
                diff_hash=materialization.diff_hash,
                plan_transition_id=materialization.plan_transition_id,
                subtask_task_mapping=_uuid_mapping_to_json(
                    materialization.subtask_task_mapping
                ),
                worker_item_mapping=_uuid_mapping_to_json(
                    materialization.worker_item_mapping
                ),
                execution_request_mapping=_uuid_mapping_to_json(
                    materialization.execution_request_mapping
                ),
                deep_link=materialization.deep_link,
                confirmed_by=materialization.confirmed_by,
                created_at=materialization.created_at,
            )
        )

    def get_by_request(
        self, request_id: UUID, workspace_id: UUID
    ) -> ProposalMaterialization | None:
        row = self._session.scalar(
            select(ProposalMaterializationModel).where(
                ProposalMaterializationModel.planning_request_id == request_id,
                ProposalMaterializationModel.workspace_id == workspace_id,
            )
        )
        return _to_materialization(row) if row is not None else None

    def get_by_proposal(
        self, proposal_id: UUID, workspace_id: UUID
    ) -> ProposalMaterialization | None:
        row = self._session.scalar(
            select(ProposalMaterializationModel).where(
                ProposalMaterializationModel.proposal_revision_id == proposal_id,
                ProposalMaterializationModel.workspace_id == workspace_id,
            )
        )
        return _to_materialization(row) if row is not None else None


def _profile_policy(profile: PlanningProfile) -> dict[str, object]:
    return {
        "prompt_policy_version": profile.prompt_policy_version,
        "system_policy": _json_value(profile.system_policy),
        "proposal_schema_version": profile.proposal_schema_version,
        "allowed_capabilities": list(profile.allowed_capabilities),
        "adapter_preferences": list(profile.adapter_preferences),
        "adapter_forbidden": list(profile.adapter_forbidden),
        "budget": {
            "max_context_bytes": profile.budget.max_context_bytes,
            "max_tokens": profile.budget.max_tokens,
            "max_cost_microunits": profile.budget.max_cost_microunits,
            "max_runtime_seconds": profile.budget.max_runtime_seconds,
            "max_revisions": profile.budget.max_revisions,
            "max_nodes": profile.budget.max_nodes,
            "max_concurrency": profile.budget.max_concurrency,
        },
        "retry_policy": {
            "max_retries": profile.retry_policy.max_retries,
            "backoff_seconds": list(profile.retry_policy.backoff_seconds),
            "unknown_outcome_policy": profile.retry_policy.unknown_outcome_policy.value,
            "invalid_output_policy": profile.retry_policy.invalid_output_policy.value,
        },
        "default_priority": profile.default_priority,
        "execution_max_attempts": profile.execution_max_attempts,
        "execution_budget_level": profile.execution_budget_level,
        "planner_secret_refs": list(profile.planner_secret_refs),
        "collaboration_runtime_binding": (
            None
            if profile.collaboration_runtime_binding is None
            else profile.collaboration_runtime_binding.to_mapping()
        ),
    }


def _to_profile(row: PlanningProfileModel) -> PlanningProfile:
    policy = _mapping(row.policy, "planning profile policy")
    budget = _mapping(policy.get("budget"), "planning profile budget")
    retry = _mapping(policy.get("retry_policy"), "planning profile retry_policy")
    return PlanningProfile(
        id=row.id,
        workspace_id=row.workspace_id,
        profile_key=row.profile_key,
        version=row.version,
        display_name=row.display_name,
        planner_module_instance_id=row.planner_module_instance_id,
        planner_module_config_version=row.planner_module_config_version,
        planner_interface_version=row.planner_interface_version,
        prompt_policy_version=_string(policy, "prompt_policy_version"),
        system_policy=_mapping(policy.get("system_policy"), "system_policy"),
        proposal_schema_version=_int(policy, "proposal_schema_version"),
        allowed_capabilities=_strings(policy, "allowed_capabilities"),
        adapter_preferences=_strings(policy, "adapter_preferences"),
        adapter_forbidden=_strings(policy, "adapter_forbidden"),
        budget=PlanningBudgetPolicy(
            max_context_bytes=_int(budget, "max_context_bytes"),
            max_tokens=_int(budget, "max_tokens"),
            max_cost_microunits=_int(budget, "max_cost_microunits"),
            max_runtime_seconds=_int(budget, "max_runtime_seconds"),
            max_revisions=_int(budget, "max_revisions"),
            max_nodes=_int(budget, "max_nodes"),
            max_concurrency=_int(budget, "max_concurrency"),
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=_int(retry, "max_retries"),
            backoff_seconds=_ints(retry, "backoff_seconds"),
            unknown_outcome_policy=UnknownOutcomePolicy(
                _string(retry, "unknown_outcome_policy")
            ),
            invalid_output_policy=InvalidOutputPolicy(
                _string(retry, "invalid_output_policy")
            ),
        ),
        default_priority=_int(policy, "default_priority"),
        execution_max_attempts=_int(policy, "execution_max_attempts"),
        execution_budget_level=_string(policy, "execution_budget_level"),
        user_selectable=row.user_selectable,
        status=PlanningProfileStatus(row.status),
        planner_secret_refs=_strings(policy, "planner_secret_refs"),
        collaboration_runtime_binding=_binding_from_mapping(
            policy.get("collaboration_runtime_binding")
        ),
    )


def _snapshot_to_mapping(snapshot: PlanningProfileSnapshot) -> dict[str, object]:
    capabilities = snapshot.planner_capabilities
    return {
        "profile_id": str(snapshot.profile_id),
        "workspace_id": str(snapshot.workspace_id),
        "profile_key": snapshot.profile_key,
        "profile_version": snapshot.profile_version,
        "display_name": snapshot.display_name,
        "planner_module_instance_id": str(snapshot.planner_module_instance_id),
        "planner_module_config_version": snapshot.planner_module_config_version,
        "planner_interface_version": snapshot.planner_interface_version,
        "prompt_policy_version": snapshot.prompt_policy_version,
        "system_policy": _json_value(snapshot.system_policy),
        "proposal_schema_version": snapshot.proposal_schema_version,
        "allowed_capabilities": list(snapshot.allowed_capabilities),
        "adapter_preferences": list(snapshot.adapter_preferences),
        "adapter_forbidden": list(snapshot.adapter_forbidden),
        "budget": _budget_to_mapping(snapshot.budget),
        "retry_policy": _retry_to_mapping(snapshot.retry_policy),
        "default_priority": snapshot.default_priority,
        "execution_max_attempts": snapshot.execution_max_attempts,
        "execution_budget_level": snapshot.execution_budget_level,
        "planner_secret_refs": list(snapshot.planner_secret_refs),
        "planner_capabilities": _capability_to_mapping(capabilities),
        "collaboration_runtime_binding": (
            None
            if snapshot.collaboration_runtime_binding is None
            else snapshot.collaboration_runtime_binding.to_mapping()
        ),
    }


def _snapshot_from_mapping(value: Mapping[str, object]) -> PlanningProfileSnapshot:
    budget = _mapping(value.get("budget"), "snapshot budget")
    retry = _mapping(value.get("retry_policy"), "snapshot retry_policy")
    return PlanningProfileSnapshot(
        profile_id=_uuid(value, "profile_id"),
        workspace_id=_uuid(value, "workspace_id"),
        profile_key=_string(value, "profile_key"),
        profile_version=_int(value, "profile_version"),
        display_name=_string(value, "display_name"),
        planner_module_instance_id=_uuid(value, "planner_module_instance_id"),
        planner_module_config_version=_int(value, "planner_module_config_version"),
        planner_interface_version=_string(value, "planner_interface_version"),
        prompt_policy_version=_string(value, "prompt_policy_version"),
        system_policy=_mapping(value.get("system_policy"), "snapshot system_policy"),
        proposal_schema_version=_int(value, "proposal_schema_version"),
        allowed_capabilities=_strings(value, "allowed_capabilities"),
        adapter_preferences=_strings(value, "adapter_preferences"),
        adapter_forbidden=_strings(value, "adapter_forbidden"),
        budget=PlanningBudgetPolicy(
            max_context_bytes=_int(budget, "max_context_bytes"),
            max_tokens=_int(budget, "max_tokens"),
            max_cost_microunits=_int(budget, "max_cost_microunits"),
            max_runtime_seconds=_int(budget, "max_runtime_seconds"),
            max_revisions=_int(budget, "max_revisions"),
            max_nodes=_int(budget, "max_nodes"),
            max_concurrency=_int(budget, "max_concurrency"),
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=_int(retry, "max_retries"),
            backoff_seconds=_ints(retry, "backoff_seconds"),
            unknown_outcome_policy=UnknownOutcomePolicy(
                _string(retry, "unknown_outcome_policy")
            ),
            invalid_output_policy=InvalidOutputPolicy(
                _string(retry, "invalid_output_policy")
            ),
        ),
        default_priority=_int(value, "default_priority"),
        execution_max_attempts=_int(value, "execution_max_attempts"),
        execution_budget_level=_string(value, "execution_budget_level"),
        planner_secret_refs=_strings(value, "planner_secret_refs"),
        planner_capabilities=_capability_from_mapping(
            _mapping(value.get("planner_capabilities"), "planner_capabilities")
        ),
        collaboration_runtime_binding=_binding_from_mapping(
            value.get("collaboration_runtime_binding")
        ),
    )


def _capability_to_mapping(
    capability: PlannerAdapterCapabilityProfile,
) -> dict[str, object]:
    return {
        "structured_output": capability.structured_output.value,
        "clarification": capability.clarification,
        "idempotency": capability.idempotency.value,
        "cancellation": capability.cancellation.value,
        "usage_reporting": capability.usage_reporting.value,
        "max_context_bytes": capability.max_context_bytes,
        "stability": capability.stability.value,
        "interface_version": capability.interface_version,
        "proposal_schema_versions": list(capability.proposal_schema_versions),
        "context_package_versions": list(capability.context_package_versions),
    }


def _capability_from_mapping(
    value: Mapping[str, object],
) -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=_string(value, "structured_output"),  # type: ignore[arg-type]
        clarification=_bool(value, "clarification"),
        idempotency=_string(value, "idempotency"),  # type: ignore[arg-type]
        cancellation=_string(value, "cancellation"),  # type: ignore[arg-type]
        usage_reporting=_string(value, "usage_reporting"),  # type: ignore[arg-type]
        max_context_bytes=_int(value, "max_context_bytes"),
        stability=_string(value, "stability"),  # type: ignore[arg-type]
        interface_version=_string(value, "interface_version"),
        proposal_schema_versions=tuple(_ints(value, "proposal_schema_versions")),
        context_package_versions=tuple(
            _ints(value, "context_package_versions")
            if "context_package_versions" in value
            else ()
        ),
    )


def _budget_to_mapping(budget: PlanningBudgetPolicy) -> dict[str, object]:
    return {
        "max_context_bytes": budget.max_context_bytes,
        "max_tokens": budget.max_tokens,
        "max_cost_microunits": budget.max_cost_microunits,
        "max_runtime_seconds": budget.max_runtime_seconds,
        "max_revisions": budget.max_revisions,
        "max_nodes": budget.max_nodes,
        "max_concurrency": budget.max_concurrency,
    }


def _retry_to_mapping(retry: PlanningRetryPolicy) -> dict[str, object]:
    return {
        "max_retries": retry.max_retries,
        "backoff_seconds": list(retry.backoff_seconds),
        "unknown_outcome_policy": retry.unknown_outcome_policy.value,
        "invalid_output_policy": retry.invalid_output_policy.value,
    }


def _binding_from_mapping(value: object) -> CollaborationRuntimeBindingSnapshot | None:
    if value is None:
        return None
    return CollaborationRuntimeBindingSnapshot.from_mapping(
        _mapping(value, "collaboration_runtime_binding")
    )


def _brief_model(brief: PlanningBriefRevision, workspace_id: UUID) -> PlanningBriefRevisionModel:
    return PlanningBriefRevisionModel(
        planning_request_id=brief.planning_request_id,
        revision=brief.revision,
        workspace_id=workspace_id,
        payload={
            "objective": brief.objective,
            "constraints": list(brief.constraints),
            "acceptance_criteria": list(brief.acceptance_criteria),
            "context_refs": list(brief.context_refs),
            "requested_outputs": list(brief.requested_outputs),
            "user_feedback": brief.user_feedback,
            "source_correlation": str(brief.source_correlation),
        },
        created_by=brief.created_by,
        created_at=brief.created_at,
    )


def _to_brief(row: PlanningBriefRevisionModel) -> PlanningBriefRevision:
    payload = _mapping(row.payload, "brief payload")
    return PlanningBriefRevision.create(
        planning_request_id=row.planning_request_id,
        revision=row.revision,
        objective=_string(payload, "objective"),
        constraints=_strings(payload, "constraints"),
        acceptance_criteria=_strings(payload, "acceptance_criteria"),
        context_refs=_strings(payload, "context_refs"),
        requested_outputs=_strings(payload, "requested_outputs"),
        user_feedback=_optional_string(payload.get("user_feedback")),
        source_correlation=_uuid(payload, "source_correlation"),
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _run_model(run: PlanningRun) -> PlanningRunModel:
    return PlanningRunModel(
        id=run.id,
        workspace_id=run.workspace_id,
        planning_request_id=run.planning_request_id,
        brief_revision=run.brief_revision,
        planning_profile_id=run.profile_snapshot.profile_id,
        planning_profile_version=run.profile_snapshot.profile_version,
        planner_module_instance_id=run.planner_module_instance_id,
        planner_module_config_version=run.planner_module_config_version,
        attempt=run.attempt,
        operation_id=run.operation_id,
        status=run.status.value,
        result_kind=run.structured_result_type,
        capability_snapshot=_capability_to_mapping(run.capability_snapshot),
        usage=_usage_to_mapping(run.usage),
        provider_ref=run.provider_ref,
        error_code=run.error_code,
        error_message=run.error_message,
        diagnostic_refs=(
            [run.diagnostic_artifact_ref]
            if run.diagnostic_artifact_ref is not None
            else []
        ),
        queued_at=run.queued_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        version=run.version,
    )


def _to_run(
    row: PlanningRunModel,
    snapshot_value: Mapping[str, object] | PlanningProfileSnapshot,
) -> PlanningRun:
    snapshot = (
        snapshot_value
        if isinstance(snapshot_value, PlanningProfileSnapshot)
        else _snapshot_from_mapping(snapshot_value)
    )
    diagnostic_refs = tuple(row.diagnostic_refs)
    return PlanningRun(
        id=row.id,
        planning_request_id=row.planning_request_id,
        workspace_id=row.workspace_id,
        brief_revision=row.brief_revision,
        attempt=row.attempt,
        operation_id=row.operation_id,
        profile_snapshot=snapshot,
        planner_module_instance_id=row.planner_module_instance_id,
        planner_module_config_version=row.planner_module_config_version,
        capability_snapshot=(
            _capability_from_mapping(row.capability_snapshot)
            if row.capability_snapshot
            else snapshot.planner_capabilities
        ),
        status=PlanningRunStatus(row.status),
        lease_id=None,
        fencing_token=0,
        queued_at=row.queued_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        provider_ref=row.provider_ref,
        structured_result_type=row.result_kind,
        usage=_usage_from_mapping(row.usage),
        error_code=row.error_code,
        error_message=row.error_message,
        diagnostic_artifact_ref=diagnostic_refs[0] if diagnostic_refs else None,
        version=row.version,
    )


def _to_request(
    row: PlanningRequestModel,
    *,
    briefs: tuple[PlanningBriefRevision, ...],
    runs: tuple[PlanningRun, ...],
    clarifications: tuple[PlanningClarification, ...],
) -> PlanningRequest:
    return PlanningRequest(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=PlanningRequestKind(row.kind),
        status=PlanningRequestStatus(row.status),
        current_brief_revision=row.current_brief_revision or 0,
        current_proposal_revision=row.current_proposal_revision,
        current_proposal_content_hash=row.current_proposal_content_hash,
        root_task_id=row.root_task_id,
        base_plan_snapshot_id=row.base_plan_snapshot_id,
        base_plan_generation=row.base_plan_generation,
        base_plan_content_hash=row.base_plan_content_hash,
        base_root_task_version=row.base_root_task_version,
        base_captured_at=row.base_captured_at,
        conflict_reason=row.conflict_reason,
        planning_profile_id=row.planning_profile_id,
        planning_profile_version=row.planning_profile_version,
        profile_snapshot=_snapshot_from_mapping(row.profile_snapshot),
        source_type=row.source_type,
        source_ref=row.source_ref,
        actor=row.actor,
        correlation_id=row.correlation_id,
        briefs=briefs,
        runs=runs,
        clarifications=clarifications,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _list_briefs(
    session: Session, request_id: UUID, workspace_id: UUID
) -> tuple[PlanningBriefRevision, ...]:
    rows = session.scalars(
        select(PlanningBriefRevisionModel)
        .where(
            PlanningBriefRevisionModel.planning_request_id == request_id,
            PlanningBriefRevisionModel.workspace_id == workspace_id,
        )
        .order_by(PlanningBriefRevisionModel.revision.asc())
    ).all()
    return tuple(_to_brief(row) for row in rows)


def _list_runs(
    session: Session,
    request_id: UUID,
    workspace_id: UUID,
    snapshot: Mapping[str, object],
) -> tuple[PlanningRun, ...]:
    rows = session.scalars(
        select(PlanningRunModel)
        .where(
            PlanningRunModel.planning_request_id == request_id,
            PlanningRunModel.workspace_id == workspace_id,
        )
        .order_by(PlanningRunModel.attempt.asc(), PlanningRunModel.id.asc())
    ).all()
    return tuple(_to_run(row, snapshot) for row in rows)


def _clarification_model(
    clarification: PlanningClarification, workspace_id: UUID, sequence: int
) -> PlanningClarificationModel:
    return PlanningClarificationModel(
        id=clarification.id,
        workspace_id=workspace_id,
        planning_request_id=clarification.planning_request_id,
        planning_run_id=clarification.planning_run_id,
        sequence=sequence,
        question=clarification.question,
        rationale=clarification.rationale,
        answer_type=clarification.answer_type,
        options=list(clarification.options),
        blocking=clarification.blocking,
        answer=clarification.answer,
        answered_by=clarification.answered_by,
        answered_at=clarification.answered_at,
        answer_brief_revision=clarification.answer_brief_revision,
        version=clarification.version,
    )


def _to_clarification(row: PlanningClarificationModel) -> PlanningClarification:
    return PlanningClarification(
        id=row.id,
        planning_request_id=row.planning_request_id,
        planning_run_id=row.planning_run_id,
        question=row.question,
        rationale=row.rationale,
        answer_type=row.answer_type,
        options=tuple(row.options),
        blocking=row.blocking,
        answer=row.answer,
        answered_by=row.answered_by,
        answered_at=row.answered_at,
        answer_brief_revision=row.answer_brief_revision,
        created_at=row.created_at,
        version=row.version,
    )


def _to_lease(row: PlanningRunLeaseModel) -> PlanningRunLease:
    return PlanningRunLease(
        id=row.id,
        workspace_id=row.workspace_id,
        planning_run_id=row.planning_run_id,
        owner_id=row.owner_id,
        status=PlanningRunLeaseStatus(row.status),
        fencing_token=row.fencing_token,
        acquired_at=row.acquired_at,
        expires_at=row.expires_at,
        released_at=row.released_at,
        version=row.version,
    )


def _to_proposal(row: PlanningProposalRevisionModel) -> ProposalRevision:
    template = _mapping(row.plan_template, "proposal plan_template")
    payload_mapping = {
        "schema_version": row.proposal_schema_version,
        "kind": row.kind,
        **template,
        "decision_summary": row.decision_summary,
        "execution_intents": row.execution_intents,
        "impact_policy": row.impact_policy,
    }
    if row.proposal_schema_version == 6:
        if row.kind == PlanningRequestKind.REVISION.value:
            payload = PlanningRevisionProposalPayloadV6.from_mapping(payload_mapping)
            derived_diff = NormalizedRevisionDiff.from_mapping(
                _mapping(row.derived_diff, "proposal derived_diff")
            )
        else:
            payload = PlanningProposalPayloadV6.from_mapping(payload_mapping)
            derived_diff = derive_proposal_diff(payload.to_legacy())
    elif row.proposal_schema_version == 5:
        if row.kind == PlanningRequestKind.REVISION.value:
            payload = PlanningRevisionProposalPayloadV5.from_mapping(payload_mapping)
            derived_diff = NormalizedRevisionDiff.from_mapping(
                _mapping(row.derived_diff, "proposal derived_diff")
            )
        else:
            payload = PlanningProposalPayloadV5.from_mapping(payload_mapping)
            derived_diff = derive_proposal_diff(payload.to_legacy())
    elif row.proposal_schema_version == 4:
        payload = (
            PlanningRevisionProposalPayloadV4.from_mapping(payload_mapping)
            if row.kind == PlanningRequestKind.REVISION.value
            else PlanningProposalPayloadV4.from_mapping(payload_mapping)
        )
        if isinstance(payload, PlanningProposalPayloadV4):
            derived_diff = derive_proposal_diff(payload.to_legacy())
        else:
            derived_diff = NormalizedRevisionDiff.from_mapping(
                _mapping(row.derived_diff, "proposal derived_diff")
            )
    elif row.proposal_schema_version == 3:
        payload = PlanningRevisionProposalPayloadV3.from_mapping(payload_mapping)
        derived_diff = NormalizedRevisionDiff.from_mapping(
            _mapping(row.derived_diff, "proposal derived_diff")
        )
    else:
        payload = PlanningProposalPayload.from_mapping(payload_mapping)
        derived_diff = derive_proposal_diff(payload)
    descriptor = PlanningProfileDescriptor(
        profile_id=row.profile_id,
        profile_version=row.profile_version,
        planner_module_instance_id=row.planner_module_instance_id,
        planner_module_config_version=row.planner_module_config_version,
    )
    canonical = json.dumps(
        row.canonical_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ProposalRevision(
        id=row.id,
        planning_request_id=row.planning_request_id,
        revision=row.revision,
        proposal_schema_version=row.proposal_schema_version,
        brief_revision=row.brief_revision,
        planning_run_id=row.planning_run_id,
        kind=PlanningRequestKind(row.kind),
        profile_descriptor=descriptor,
        payload=payload,
        derived_diff=derived_diff,
        canonical_payload=canonical,
        content_hash=row.content_hash,
        created_at=row.created_at,
    )


def _to_materialization(row: ProposalMaterializationModel) -> ProposalMaterialization:
    return ProposalMaterialization(
        id=row.id,
        workspace_id=row.workspace_id,
        planning_request_id=row.planning_request_id,
        proposal_revision_id=row.proposal_revision_id,
        root_task_id=row.root_task_id,
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        kind=PlanningRequestKind(row.kind),
        diff_hash=row.diff_hash,
        plan_transition_id=row.plan_transition_id,
        subtask_task_mapping=_uuid_mapping_from_json(row.subtask_task_mapping),
        worker_item_mapping=_uuid_mapping_from_json(row.worker_item_mapping),
        execution_request_mapping=_uuid_mapping_from_json(
            row.execution_request_mapping
        ),
        deep_link=row.deep_link,
        confirmed_by=row.confirmed_by,
        created_at=row.created_at,
    )


def _usage_to_mapping(usage: PlanningUsage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_microunits": usage.cost_microunits,
        "runtime_seconds": usage.runtime_seconds,
        "context_bytes": usage.context_bytes,
    }


def _usage_from_mapping(value: Mapping[str, object]) -> PlanningUsage:
    return PlanningUsage(
        input_tokens=_int(value, "input_tokens"),
        output_tokens=_int(value, "output_tokens"),
        cost_microunits=_int(value, "cost_microunits"),
        runtime_seconds=_int(value, "runtime_seconds"),
        context_bytes=_int(value, "context_bytes"),
    )


def _diff_to_mapping(
    diff: ProposalDiff | NormalizedRevisionDiff,
) -> dict[str, object]:
    if isinstance(diff, NormalizedRevisionDiff):
        return diff.to_mapping()
    return {
        "root_task_added": diff.root_task_added,
        "added_subtask_keys": list(diff.added_subtask_keys),
        "added_worker_item_keys": list(diff.added_worker_item_keys),
        "added_subtask_dependency_edges": [
            list(edge) for edge in diff.added_subtask_dependency_edges
        ],
        "added_worker_item_dependency_edges": [
            list(edge) for edge in diff.added_worker_item_dependency_edges
        ],
        "added_execution_intent_keys": list(diff.added_execution_intent_keys),
        "immediately_ready_worker_item_keys": list(
            diff.immediately_ready_worker_item_keys
        ),
        "blocked_worker_item_keys": list(diff.blocked_worker_item_keys),
    }


def _workspace_for_request(session: Session, request_id: UUID) -> UUID:
    workspace_id = session.scalar(
        select(PlanningRequestModel.workspace_id).where(
            PlanningRequestModel.id == request_id
        )
    )
    if workspace_id is None:
        raise ValidationError("planning request does not exist")
    return workspace_id


def _assert_saved(result: Any, actual: int, expected: int) -> None:
    if result.rowcount != 1:
        raise VersionConflict(expected, actual)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    return value


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValidationError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("optional string value must be a string")
    return value


def _int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValidationError(f"{key} must be an integer")
    return item


def _bool(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise ValidationError(f"{key} must be a boolean")
    return item


def _strings(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if not isinstance(item, (list, tuple)) or any(
        not isinstance(entry, str) for entry in item
    ):
        raise ValidationError(f"{key} must be a string array")
    return tuple(item)


def _ints(value: Mapping[str, object], key: str) -> tuple[int, ...]:
    item = value.get(key)
    if not isinstance(item, (list, tuple)) or any(
        not isinstance(entry, int) or isinstance(entry, bool) for entry in item
    ):
        raise ValidationError(f"{key} must be an integer array")
    return tuple(item)


def _uuid(value: Mapping[str, object], key: str) -> UUID:
    item = value.get(key)
    try:
        return item if isinstance(item, UUID) else UUID(str(item))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError(f"{key} must be a UUID") from exc


def _json_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def _uuid_mapping_to_json(value: Mapping[str, UUID]) -> dict[str, str]:
    return {key: str(item) for key, item in value.items()}


def _uuid_mapping_from_json(value: Mapping[str, object]) -> dict[str, UUID]:
    return {key: _uuid({"value": item}, "value") for key, item in value.items()}
