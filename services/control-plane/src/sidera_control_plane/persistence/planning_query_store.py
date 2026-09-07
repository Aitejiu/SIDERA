from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from sidera_control_plane.application.errors import InvalidCursor
from sidera_control_plane.application.planning_metrics import (
    PlanningContextPatchDeliveryMetricView,
    PlanningExecutionRequestMetricsView,
    PlanningLatencyMetricsView,
    PlanningLifecycleMetricsView,
    PlanningMetricReasonView,
    PlanningMetricsView,
    PlanningOutcomeMetricsView,
    PlanningRevisionMetricsView,
    PlanningRunMetricsView,
    PlanningUsageMetricsView,
    PlanningWorkerTransitionMetricView,
)
from sidera_control_plane.application.planning_queries import (
    PlanningBriefView,
    PlanningCapabilityView,
    PlanningClarificationView,
    PlanningCollaborationView,
    PlanningMaterializationView,
    PlanningPage,
    PlanningProfileView,
    PlanningProposalView,
    PlanningRequestDetailView,
    PlanningRequestPage,
    PlanningRequestSummaryView,
    PlanningRunView,
    PlanningUsageView,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextPatchAcknowledgementModel,
    ContextPatchModel,
    ExecutionRequestModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningBriefRevisionModel,
    PlanningClarificationModel,
    PlanningProfileModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunModel,
    PlanTransitionModel,
    ProposalMaterializationModel,
    WorkerTransitionModel,
)
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)

_ATTENTION_STATUSES = {
    "waiting_for_user",
    "proposal_ready",
    "conflicted",
    "failed",
}
_CLOSED_STATUSES = {"materialized", "rejected", "cancelled"}


class SqlAlchemyPlanningQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_profiles(
        self, workspace_id: UUID, *, limit: int, cursor: str | None
    ) -> PlanningPage[PlanningProfileView]:
        with self._database.session() as session:
            statement = (
                select(
                    PlanningProfileModel,
                    ModuleDefinitionModel.provider,
                    ModuleDefinitionModel.capabilities,
                )
                .join(
                    ModuleInstanceModel,
                    and_(
                        ModuleInstanceModel.id
                        == PlanningProfileModel.planner_module_instance_id,
                        ModuleInstanceModel.workspace_id == workspace_id,
                    ),
                )
                .join(
                    ModuleDefinitionModel,
                    and_(
                        ModuleDefinitionModel.id == ModuleInstanceModel.definition_id,
                        ModuleDefinitionModel.workspace_id == workspace_id,
                    ),
                )
                .where(PlanningProfileModel.workspace_id == workspace_id)
            )
            statement = _apply_cursor(statement, PlanningProfileModel, cursor)
            rows = session.execute(
                statement.order_by(
                    PlanningProfileModel.created_at.desc(),
                    PlanningProfileModel.id.desc(),
                ).limit(limit + 1)
            ).all()

        page_rows = rows[:limit]
        next_cursor = (
            _encode_cursor(page_rows[-1][0].created_at, page_rows[-1][0].id)
            if len(rows) > limit
            else None
        )
        return PlanningPage(
            items=tuple(_profile_view(row[0], row[1], row[2]) for row in page_rows),
            next_cursor=next_cursor,
        )

    def list_requests(
        self,
        workspace_id: UUID,
        *,
        limit: int,
        cursor: str | None,
        status: str | None,
        closed_status: bool | None,
        requires_attention: bool,
    ) -> PlanningRequestPage:
        with self._database.session() as session:
            filters = [PlanningRequestModel.workspace_id == workspace_id]
            if status is not None:
                filters.append(PlanningRequestModel.status == status)
            if closed_status is True:
                filters.append(PlanningRequestModel.status.in_(_CLOSED_STATUSES))
            elif closed_status is False:
                filters.append(~PlanningRequestModel.status.in_(_CLOSED_STATUSES))
            if requires_attention:
                filters.append(PlanningRequestModel.status.in_(_ATTENTION_STATUSES))
            statement = select(PlanningRequestModel).where(*filters)
            statement = _apply_cursor(statement, PlanningRequestModel, cursor)
            rows = session.scalars(
                statement.order_by(
                    PlanningRequestModel.created_at.desc(),
                    PlanningRequestModel.id.desc(),
                ).limit(limit + 1)
            ).all()
            request_ids = tuple(row.id for row in rows[:limit])
            profile_keys = _profile_keys(session, workspace_id, rows[:limit])
            current_runs = _current_runs(session, workspace_id, request_ids)
            last_change_cursor = ProjectionChangeRepository(session).latest_cursor(
                workspace_id
            )

        page_rows = rows[:limit]
        next_cursor = (
            _encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
            if len(rows) > limit
            else None
        )
        return PlanningRequestPage(
            items=tuple(
                _request_summary_view(
                    row,
                    profile_key=profile_keys.get(row.planning_profile_id),
                    current_run=current_runs.get(row.id),
                )
                for row in page_rows
            ),
            next_cursor=next_cursor,
            last_change_cursor=last_change_cursor,
        )

    def get_request_detail(
        self, workspace_id: UUID, request_id: UUID
    ) -> PlanningRequestDetailView | None:
        with self._database.session() as session:
            request = session.scalar(
                select(PlanningRequestModel).where(
                    PlanningRequestModel.workspace_id == workspace_id,
                    PlanningRequestModel.id == request_id,
                )
            )
            if request is None:
                return None
            profile_row = session.execute(
                select(
                    PlanningProfileModel,
                    ModuleDefinitionModel.provider,
                    ModuleDefinitionModel.capabilities,
                )
                .join(
                    ModuleInstanceModel,
                    and_(
                        ModuleInstanceModel.id
                        == PlanningProfileModel.planner_module_instance_id,
                        ModuleInstanceModel.workspace_id == workspace_id,
                    ),
                )
                .join(
                    ModuleDefinitionModel,
                    and_(
                        ModuleDefinitionModel.id == ModuleInstanceModel.definition_id,
                        ModuleDefinitionModel.workspace_id == workspace_id,
                    ),
                )
                .where(
                    PlanningProfileModel.workspace_id == workspace_id,
                    PlanningProfileModel.id == request.planning_profile_id,
                )
            ).one_or_none()
            runs = tuple(
                session.scalars(
                    select(PlanningRunModel)
                    .where(
                        PlanningRunModel.workspace_id == workspace_id,
                        PlanningRunModel.planning_request_id == request_id,
                    )
                    .order_by(PlanningRunModel.attempt.asc(), PlanningRunModel.id.asc())
                ).all()
            )
            clarifications = tuple(
                session.scalars(
                    select(PlanningClarificationModel)
                    .where(
                        PlanningClarificationModel.workspace_id == workspace_id,
                        PlanningClarificationModel.planning_request_id == request_id,
                    )
                    .order_by(PlanningClarificationModel.sequence.asc())
                ).all()
            )
            last_cursor = ProjectionChangeRepository(session).latest_cursor(
                workspace_id
            )
            profile = (
                _profile_view(profile_row[0], profile_row[1], profile_row[2])
                if profile_row is not None
                else None
            )
            run_views = tuple(_run_view(row, request.profile_snapshot) for row in runs)
            active_run = run_views[-1] if run_views else None
            pending = tuple(
                _clarification_view(row)
                for row in clarifications
                if row.blocking and row.answer is None
            )
            usage = _sum_usage(run_views)

        return PlanningRequestDetailView(
            summary=_request_summary_view(
                request,
                profile_key=profile.profile_key if profile else None,
                current_run=runs[-1] if runs else None,
            ),
            profile=profile,
            active_run=active_run,
            pending_clarifications=pending,
            usage=usage,
            last_change_cursor=last_cursor,
        )

    def list_briefs(
        self, workspace_id: UUID, request_id: UUID
    ) -> tuple[PlanningBriefView, ...] | None:
        with self._database.session() as session:
            if not _request_exists(session, workspace_id, request_id):
                return None
            rows = session.scalars(
                select(PlanningBriefRevisionModel)
                .where(
                    PlanningBriefRevisionModel.workspace_id == workspace_id,
                    PlanningBriefRevisionModel.planning_request_id == request_id,
                )
                .order_by(PlanningBriefRevisionModel.revision.asc())
            ).all()
        return tuple(_brief_view(row) for row in rows)

    def list_runs(
        self, workspace_id: UUID, request_id: UUID
    ) -> tuple[PlanningRunView, ...] | None:
        with self._database.session() as session:
            request = session.scalar(
                select(PlanningRequestModel).where(
                    PlanningRequestModel.workspace_id == workspace_id,
                    PlanningRequestModel.id == request_id,
                )
            )
            if request is None:
                return None
            rows = session.scalars(
                select(PlanningRunModel)
                .where(
                    PlanningRunModel.workspace_id == workspace_id,
                    PlanningRunModel.planning_request_id == request_id,
                )
                .order_by(PlanningRunModel.attempt.asc(), PlanningRunModel.id.asc())
            ).all()
        return tuple(_run_view(row, request.profile_snapshot) for row in rows)

    def list_clarifications(
        self, workspace_id: UUID, request_id: UUID
    ) -> tuple[PlanningClarificationView, ...] | None:
        with self._database.session() as session:
            if not _request_exists(session, workspace_id, request_id):
                return None
            rows = session.scalars(
                select(PlanningClarificationModel)
                .where(
                    PlanningClarificationModel.workspace_id == workspace_id,
                    PlanningClarificationModel.planning_request_id == request_id,
                )
                .order_by(PlanningClarificationModel.sequence.asc())
            ).all()
        return tuple(_clarification_view(row) for row in rows)

    def get_proposal(
        self, workspace_id: UUID, request_id: UUID, revision: int
    ) -> PlanningProposalView | None:
        with self._database.session() as session:
            row = session.scalar(
                select(PlanningProposalRevisionModel).where(
                    PlanningProposalRevisionModel.workspace_id == workspace_id,
                    PlanningProposalRevisionModel.planning_request_id == request_id,
                    PlanningProposalRevisionModel.revision == revision,
                )
            )
            if row is None:
                return None
            profile_snapshot = session.scalar(
                select(PlanningRequestModel.profile_snapshot).where(
                    PlanningRequestModel.workspace_id == workspace_id,
                    PlanningRequestModel.id == request_id,
                )
            )
            policy_request = _mapping(row.plan_template).get(
                "collaboration_policy_request"
            )
            collaboration = PlanningCollaborationView.from_request_mapping(
                policy_request,
                profile_snapshot,
                provider=_collaboration_runtime_provider(
                    session,
                    workspace_id,
                    profile_snapshot,
                    enabled=(
                        isinstance(policy_request, Mapping)
                        and policy_request.get("mode") == "enabled"
                    ),
                ),
            )
            return _proposal_view(row, collaboration)

    def get_materialization(
        self, workspace_id: UUID, request_id: UUID
    ) -> PlanningMaterializationView | None:
        with self._database.session() as session:
            row = session.scalar(
                select(ProposalMaterializationModel).where(
                    ProposalMaterializationModel.workspace_id == workspace_id,
                    ProposalMaterializationModel.planning_request_id == request_id,
                )
            )
        return _materialization_view(row) if row is not None else None


class SqlAlchemyPlanningMetricsStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get_metrics(self, workspace_id: UUID) -> PlanningMetricsView:
        with self._database.session() as session:
            status_rows = session.execute(
                select(PlanningRunModel.status, func.count())
                .where(PlanningRunModel.workspace_id == workspace_id)
                .group_by(PlanningRunModel.status)
            ).all()
            run_counts = {status: int(count) for status, count in status_rows}
            total_runs = sum(run_counts.values())
            usage_row = session.execute(
                select(
                    func.coalesce(
                        func.sum(PlanningRunModel.usage["input_tokens"].as_integer()),
                        0,
                    ),
                    func.coalesce(
                        func.sum(PlanningRunModel.usage["output_tokens"].as_integer()),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            PlanningRunModel.usage["cost_microunits"].as_integer()
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(PlanningRunModel.usage["context_bytes"].as_integer()),
                        0,
                    ),
                ).where(PlanningRunModel.workspace_id == workspace_id)
            ).one()
            queue_seconds = _scalar_float(
                session.scalar(
                    select(
                        func.avg(
                            func.extract(
                                "epoch",
                                PlanningRunModel.started_at
                                - PlanningRunModel.queued_at,
                            )
                        )
                    ).where(
                        PlanningRunModel.workspace_id == workspace_id,
                        PlanningRunModel.started_at.is_not(None),
                    )
                )
            )
            planning_seconds = _scalar_float(
                session.scalar(
                    select(
                        func.avg(
                            func.extract(
                                "epoch",
                                PlanningRunModel.finished_at
                                - PlanningRunModel.started_at,
                            )
                        )
                    ).where(
                        PlanningRunModel.workspace_id == workspace_id,
                        PlanningRunModel.finished_at.is_not(None),
                        PlanningRunModel.started_at.is_not(None),
                    )
                )
            )
            materialization_seconds = _scalar_float(
                session.scalar(
                    select(
                        func.avg(
                            func.extract(
                                "epoch",
                                ProposalMaterializationModel.created_at
                                - PlanningProposalRevisionModel.created_at,
                            )
                        )
                    )
                    .select_from(ProposalMaterializationModel)
                    .join(
                        PlanningProposalRevisionModel,
                        and_(
                            PlanningProposalRevisionModel.id
                            == ProposalMaterializationModel.proposal_revision_id,
                            PlanningProposalRevisionModel.workspace_id == workspace_id,
                        ),
                    )
                    .where(ProposalMaterializationModel.workspace_id == workspace_id)
                )
            )
            retry_count = (
                session.scalar(
                    select(func.count())
                    .select_from(PlanningRunModel)
                    .where(
                        PlanningRunModel.workspace_id == workspace_id,
                        PlanningRunModel.attempt > 1,
                    )
                )
                or 0
            )
            timeout_count = _error_count(session, workspace_id, "%timeout%")
            unknown_count = _error_count(session, workspace_id, "%unknown%")
            invalid_count = _error_count(session, workspace_id, "%invalid%")
            clarifications = (
                session.scalar(
                    select(func.count())
                    .select_from(PlanningClarificationModel)
                    .where(PlanningClarificationModel.workspace_id == workspace_id)
                )
                or 0
            )
            revisions = (
                session.scalar(
                    select(func.count())
                    .select_from(PlanningProposalRevisionModel)
                    .where(
                        PlanningProposalRevisionModel.workspace_id == workspace_id,
                        PlanningProposalRevisionModel.revision > 1,
                    )
                )
                or 0
            )
            confirmed = (
                session.scalar(
                    select(func.count())
                    .select_from(ProposalMaterializationModel)
                    .where(ProposalMaterializationModel.workspace_id == workspace_id)
                )
                or 0
            )
            rejected = (
                session.scalar(
                    select(func.count())
                    .select_from(PlanningRequestModel)
                    .where(
                        PlanningRequestModel.workspace_id == workspace_id,
                        PlanningRequestModel.status == "rejected",
                    )
                )
                or 0
            )
            execution_counts = session.execute(
                select(ExecutionRequestModel.status, func.count())
                .where(ExecutionRequestModel.workspace_id == workspace_id)
                .group_by(ExecutionRequestModel.status)
            ).all()
            gate_wait = _scalar_float(
                session.scalar(
                    select(
                        func.avg(
                            func.extract(
                                "epoch",
                                ExecutionRequestModel.gate_evaluated_at
                                - ExecutionRequestModel.status_changed_at,
                            )
                        )
                    ).where(
                        ExecutionRequestModel.workspace_id == workspace_id,
                        ExecutionRequestModel.gate_evaluated_at.is_not(None),
                    )
                )
            )
            active_revisions = (
                session.scalar(
                    select(func.count())
                    .select_from(PlanningRequestModel)
                    .where(
                        PlanningRequestModel.workspace_id == workspace_id,
                        PlanningRequestModel.kind == "revision",
                        PlanningRequestModel.status.in_(
                            (
                                "submitted",
                                "planning",
                                "waiting_for_user",
                                "proposal_ready",
                                "failed",
                            )
                        ),
                    )
                )
                or 0
            )
            conflict_rows = session.execute(
                select(
                    func.coalesce(PlanningRequestModel.conflict_reason, "unknown"),
                    func.count(),
                )
                .where(
                    PlanningRequestModel.workspace_id == workspace_id,
                    PlanningRequestModel.kind == "revision",
                    PlanningRequestModel.status == "conflicted",
                )
                .group_by(PlanningRequestModel.conflict_reason)
            ).all()
            active_transitions = (
                session.scalar(
                    select(func.count())
                    .select_from(PlanTransitionModel)
                    .where(
                        PlanTransitionModel.workspace_id == workspace_id,
                        PlanTransitionModel.status == "converging",
                    )
                )
                or 0
            )
            worker_transition_rows = session.execute(
                select(
                    WorkerTransitionModel.action,
                    WorkerTransitionModel.status,
                    func.count(),
                )
                .where(WorkerTransitionModel.workspace_id == workspace_id)
                .group_by(WorkerTransitionModel.action, WorkerTransitionModel.status)
            ).all()
            patch_rows = session.execute(
                select(
                    ContextPatchModel.id, ContextPatchAcknowledgementModel.adapter_ref
                )
                .select_from(ContextPatchModel)
                .outerjoin(
                    ContextPatchAcknowledgementModel,
                    and_(
                        ContextPatchAcknowledgementModel.patch_id
                        == ContextPatchModel.id,
                        ContextPatchAcknowledgementModel.workspace_id == workspace_id,
                    ),
                )
                .where(ContextPatchModel.workspace_id == workspace_id)
            ).all()
            blocking_worker_rows = session.execute(
                select(
                    WorkerTransitionModel.status,
                    WorkerTransitionModel.writer_state,
                    func.count(),
                )
                .where(WorkerTransitionModel.workspace_id == workspace_id)
                .group_by(
                    WorkerTransitionModel.status,
                    WorkerTransitionModel.writer_state,
                )
            ).all()
            degraded_transitions = (
                session.scalar(
                    select(func.count())
                    .select_from(PlanTransitionModel)
                    .where(
                        PlanTransitionModel.workspace_id == workspace_id,
                        PlanTransitionModel.status == "degraded",
                    )
                )
                or 0
            )

        conflict_metrics = tuple(
            PlanningMetricReasonView(reason=str(reason), count=int(count))
            for reason, count in sorted(
                ((str(reason), int(count)) for reason, count in conflict_rows),
                key=lambda item: item[0],
            )
        )
        worker_metrics = tuple(
            PlanningWorkerTransitionMetricView(
                action=str(action),
                outcome=_worker_transition_metric_outcome(str(status)),
                count=int(count),
            )
            for action, status, count in sorted(
                worker_transition_rows,
                key=lambda item: (
                    str(item[0]),
                    _worker_transition_metric_outcome(str(item[1])),
                ),
            )
        )
        patch_counts: dict[tuple[str, str], int] = {}
        for _patch_id, adapter_ref in patch_rows:
            adapter = _adapter_metric_label(adapter_ref)
            outcome = "acknowledged" if adapter_ref is not None else "pending"
            key = (adapter, outcome)
            patch_counts[key] = patch_counts.get(key, 0) + 1
        patch_metrics = tuple(
            PlanningContextPatchDeliveryMetricView(
                adapter=adapter,
                outcome=outcome,
                count=count,
            )
            for (adapter, outcome), count in sorted(patch_counts.items())
        )
        blocked_counts: dict[str, int] = {}
        if degraded_transitions:
            blocked_counts["transition_degraded"] = int(degraded_transitions)
        for status, writer_state, count in blocking_worker_rows:
            count = int(count)
            if status in {
                "pending",
                "waiting_safe_boundary",
                "applying",
                "waiting_activation",
            }:
                blocked_counts["in_flight"] = blocked_counts.get("in_flight", 0) + count
            if status == "blocked_unknown":
                blocked_counts["worker_blocked_unknown"] = (
                    blocked_counts.get("worker_blocked_unknown", 0) + count
                )
            if writer_state == "unknown" and status != "blocked_unknown":
                blocked_counts["writer_unknown"] = (
                    blocked_counts.get("writer_unknown", 0) + count
                )
            if writer_state in {"active", "fenced"} and status not in {
                "converged",
                "failed",
            }:
                blocked_counts["active_fence"] = (
                    blocked_counts.get("active_fence", 0) + count
                )
        blocked_metrics = tuple(
            PlanningMetricReasonView(reason=reason, count=count)
            for reason, count in sorted(blocked_counts.items())
        )

        return PlanningMetricsView(
            workspace_id=workspace_id,
            as_of=datetime.now(UTC),
            runs=PlanningRunMetricsView(
                queued=run_counts.get("queued", 0),
                running=run_counts.get("running", 0),
                needs_clarification=run_counts.get("needs_clarification", 0),
                blocked=run_counts.get("blocked", 0),
                failed=run_counts.get("failed", 0),
                succeeded=run_counts.get("succeeded", 0),
                cancelled=run_counts.get("cancelled", 0),
            ),
            latency=PlanningLatencyMetricsView(
                queue_seconds=queue_seconds,
                planning_seconds=planning_seconds,
                materialization_seconds=materialization_seconds,
            ),
            outcomes=PlanningOutcomeMetricsView(
                retry_rate=retry_count / total_runs if total_runs else 0.0,
                timeout_rate=timeout_count / total_runs if total_runs else 0.0,
                unknown_outcome_rate=unknown_count / total_runs if total_runs else 0.0,
                invalid_output_rate=invalid_count / total_runs if total_runs else 0.0,
            ),
            usage=PlanningUsageMetricsView(
                input_tokens=int(usage_row[0] or 0),
                output_tokens=int(usage_row[1] or 0),
                cost_microunits=int(usage_row[2] or 0),
                context_bytes=int(usage_row[3] or 0),
            ),
            planning=PlanningLifecycleMetricsView(
                clarifications=int(clarifications),
                revisions=int(revisions),
                confirmed=int(confirmed),
                rejected=int(rejected),
            ),
            execution_requests=PlanningExecutionRequestMetricsView(
                blocked=_status_count(execution_counts, "blocked"),
                ready=_status_count(execution_counts, "ready"),
                gate_wait_seconds=gate_wait,
            ),
            revision=PlanningRevisionMetricsView(
                plan_revisions_active=int(active_revisions),
                plan_revision_conflicts_total=conflict_metrics,
                plan_transitions_active=int(active_transitions),
                worker_transitions_total=worker_metrics,
                context_patch_delivery_total=patch_metrics,
                revision_confirmation_blocked_total=blocked_metrics,
            ),
        )


def _profile_view(
    row: PlanningProfileModel,
    provider: str,
    capabilities: Mapping[str, object],
) -> PlanningProfileView:
    return PlanningProfileView(
        id=row.id,
        workspace_id=row.workspace_id,
        profile_key=row.profile_key,
        version=row.version,
        display_name=row.display_name,
        planner_provider=provider,
        planner_module_instance_id=row.planner_module_instance_id,
        planner_module_config_version=row.planner_module_config_version,
        planner_interface_version=row.planner_interface_version,
        status=row.status,
        user_selectable=row.user_selectable,
        capabilities=_capability_view(capabilities),
        created_at=row.created_at,
    )


def _request_summary_view(
    row: PlanningRequestModel,
    *,
    profile_key: str | None,
    current_run: PlanningRunModel | None,
) -> PlanningRequestSummaryView:
    return PlanningRequestSummaryView(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        status=row.status,
        source_type=row.source_type,
        source_ref=row.source_ref,
        current_brief_revision=row.current_brief_revision or 0,
        current_proposal_revision=row.current_proposal_revision,
        current_proposal_content_hash=row.current_proposal_content_hash,
        planning_profile_id=row.planning_profile_id,
        planning_profile_version=row.planning_profile_version,
        profile_key=profile_key,
        current_run_id=current_run.id if current_run is not None else None,
        requires_attention=row.status in _ATTENTION_STATUSES,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _brief_view(row: PlanningBriefRevisionModel) -> PlanningBriefView:
    payload = _mapping(row.payload)
    return PlanningBriefView(
        planning_request_id=row.planning_request_id,
        revision=row.revision,
        objective=_text(payload.get("objective")),
        constraints=_texts(payload.get("constraints")),
        acceptance_criteria=_texts(payload.get("acceptance_criteria")),
        context_refs=_texts(payload.get("context_refs")),
        requested_outputs=_texts(payload.get("requested_outputs")),
        user_feedback=_optional_text(payload.get("user_feedback")),
        source_correlation=_uuid(payload.get("source_correlation")),
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _run_view(row: PlanningRunModel, snapshot: Mapping[str, object]) -> PlanningRunView:
    capability = row.capability_snapshot or snapshot.get("planner_capabilities", {})
    return PlanningRunView(
        id=row.id,
        planning_request_id=row.planning_request_id,
        workspace_id=row.workspace_id,
        brief_revision=row.brief_revision,
        attempt=row.attempt,
        operation_id=row.operation_id,
        status=row.status,
        result_kind=row.result_kind,
        capability_snapshot=_capability_view(_mapping(capability)),
        usage=_usage_view(row.usage),
        provider_ref=row.provider_ref,
        error_code=row.error_code,
        queued_at=row.queued_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        version=row.version,
    )


def _clarification_view(row: PlanningClarificationModel) -> PlanningClarificationView:
    return PlanningClarificationView(
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


def _proposal_view(
    row: PlanningProposalRevisionModel,
    collaboration: PlanningCollaborationView,
) -> PlanningProposalView:
    return PlanningProposalView(
        id=row.id,
        planning_request_id=row.planning_request_id,
        revision=row.revision,
        brief_revision=row.brief_revision,
        planning_run_id=row.planning_run_id,
        proposal_schema_version=row.proposal_schema_version,
        kind=row.kind,
        profile_version=row.profile_version,
        created_at=row.created_at,
        content_hash=row.content_hash,
        derived_diff=dict(row.derived_diff),
        display=_safe_proposal_display(
            row.plan_template,
            row.decision_summary,
            collaboration=collaboration,
        ),
        collaboration=collaboration,
    )


def _safe_proposal_display(
    plan_template: Mapping[str, object],
    decision_summary: Mapping[str, object],
    *,
    collaboration: PlanningCollaborationView,
) -> dict[str, object]:
    safe_summary = {
        key: decision_summary[key]
        for key in (
            "understanding",
            "assumptions",
            "risks",
            "estimated_tokens",
            "estimated_cost_microunits",
            "estimated_runtime_seconds",
            "estimated_concurrency",
            "scope_includes",
            "scope_excludes",
        )
        if key in decision_summary
    }
    display: dict[str, object] = {
        "root_task": plan_template.get("root_task", {}),
        "subtasks": plan_template.get("subtasks", []),
        "worker_items": plan_template.get("worker_items", []),
        "milestones": plan_template.get("milestones", []),
        "subtask_dependencies": plan_template.get("subtask_dependencies", []),
        "worker_item_dependencies": plan_template.get("worker_item_dependencies", []),
        "execution_intents": plan_template.get("execution_intents", []),
        "decision_summary": safe_summary,
        "impact_policy": plan_template.get("impact_policy", {}),
        "collaboration": collaboration.to_mapping(),
    }
    integration_spec = plan_template.get("integration_spec")
    if isinstance(integration_spec, Mapping):
        display["integration_spec"] = dict(integration_spec)
    return display


def _materialization_view(
    row: ProposalMaterializationModel,
) -> PlanningMaterializationView:
    return PlanningMaterializationView(
        id=row.id,
        workspace_id=row.workspace_id,
        planning_request_id=row.planning_request_id,
        proposal_revision_id=row.proposal_revision_id,
        root_task_id=row.root_task_id,
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        subtask_task_mapping=_uuid_mapping(row.subtask_task_mapping),
        worker_item_mapping=_uuid_mapping(row.worker_item_mapping),
        execution_request_mapping=_uuid_mapping(row.execution_request_mapping),
        deep_link=row.deep_link,
        confirmed_by=row.confirmed_by,
        created_at=row.created_at,
    )


def _capability_view(value: Mapping[str, object]) -> PlanningCapabilityView:
    versions = value.get("proposal_schema_versions", ())
    version_values = versions if isinstance(versions, list | tuple) else ()
    context_versions = value.get("context_package_versions", ())
    context_version_values = (
        context_versions if isinstance(context_versions, list | tuple) else ()
    )
    return PlanningCapabilityView(
        structured_output=str(value.get("structured_output", "none")),
        clarification=bool(value.get("clarification", False)),
        idempotency=str(value.get("idempotency", "none")),
        cancellation=str(value.get("cancellation", "none")),
        usage_reporting=str(value.get("usage_reporting", "none")),
        max_context_bytes=_non_negative_int(value.get("max_context_bytes")),
        stability=str(value.get("stability", "unknown")),
        interface_version=str(value.get("interface_version", "unknown")),
        proposal_schema_versions=tuple(
            int(item) for item in version_values if isinstance(item, int)
        ),
        context_package_versions=tuple(
            int(item) for item in context_version_values if isinstance(item, int)
        ),
    )


def _usage_view(value: Mapping[str, object] | None) -> PlanningUsageView:
    value = value or {}
    return PlanningUsageView(
        input_tokens=_non_negative_int(value.get("input_tokens")),
        output_tokens=_non_negative_int(value.get("output_tokens")),
        cost_microunits=_non_negative_int(value.get("cost_microunits")),
        runtime_seconds=_non_negative_int(value.get("runtime_seconds")),
        context_bytes=_non_negative_int(value.get("context_bytes")),
    )


def _sum_usage(runs: tuple[PlanningRunView, ...]) -> PlanningUsageView:
    return PlanningUsageView(
        input_tokens=sum(run.usage.input_tokens for run in runs),
        output_tokens=sum(run.usage.output_tokens for run in runs),
        cost_microunits=sum(run.usage.cost_microunits for run in runs),
        runtime_seconds=sum(run.usage.runtime_seconds for run in runs),
        context_bytes=sum(run.usage.context_bytes for run in runs),
    )


def _profile_keys(session: Any, workspace_id: UUID, rows: object) -> dict[UUID, str]:
    request_rows = rows if isinstance(rows, list) else []
    ids = tuple({row.planning_profile_id for row in request_rows})
    if not ids:
        return {}
    result = session.execute(
        select(PlanningProfileModel.id, PlanningProfileModel.profile_key).where(
            PlanningProfileModel.workspace_id == workspace_id,
            PlanningProfileModel.id.in_(ids),
        )
    ).all()
    return {profile_id: profile_key for profile_id, profile_key in result}


def _current_runs(
    session: Any, workspace_id: UUID, request_ids: tuple[UUID, ...]
) -> dict[UUID, PlanningRunModel]:
    if not request_ids:
        return {}
    rows = session.scalars(
        select(PlanningRunModel)
        .where(
            PlanningRunModel.workspace_id == workspace_id,
            PlanningRunModel.planning_request_id.in_(request_ids),
        )
        .order_by(
            PlanningRunModel.planning_request_id.asc(),
            PlanningRunModel.attempt.desc(),
            PlanningRunModel.id.desc(),
        )
    ).all()
    result: dict[UUID, PlanningRunModel] = {}
    for row in rows:
        result.setdefault(row.planning_request_id, row)
    return result


def _request_exists(session: Any, workspace_id: UUID, request_id: UUID) -> bool:
    return (
        session.scalar(
            select(PlanningRequestModel.id).where(
                PlanningRequestModel.workspace_id == workspace_id,
                PlanningRequestModel.id == request_id,
            )
        )
        is not None
    )


def _apply_cursor(statement: Any, model: Any, cursor: str | None) -> Any:
    if cursor is None:
        return statement
    created_at, row_id = _decode_cursor(cursor)
    return statement.where(
        or_(
            model.created_at < created_at,
            and_(model.created_at == created_at, model.id < row_id),
        )
    )


def _encode_cursor(created_at: datetime, row_id: UUID) -> str:
    payload = {"created_at": created_at.isoformat(), "id": str(row_id)}
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        row_id = UUID(str(payload["id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidCursor("invalid planning query cursor") from exc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at, row_id


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return str(value) if value is not None else ""


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _collaboration_runtime_provider(
    session: Any,
    workspace_id: UUID,
    profile_snapshot: object,
    *,
    enabled: bool,
) -> str | None:
    if not enabled:
        return None
    snapshot = _mapping(profile_snapshot)
    binding = _mapping(snapshot.get("collaboration_runtime_binding"))
    raw_definition_id = binding.get("runtime_definition_id")
    try:
        definition_id = _uuid(raw_definition_id)
    except (TypeError, ValueError):
        return None
    provider = session.scalar(
        select(ModuleDefinitionModel.provider).where(
            ModuleDefinitionModel.workspace_id == workspace_id,
            ModuleDefinitionModel.id == definition_id,
        )
    )
    return _optional_text(provider)


def _texts(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list | tuple) else ()


def _uuid(value: object) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _uuid_mapping(value: object) -> dict[str, str]:
    mapping = _mapping(value)
    return {str(key): str(item) for key, item in mapping.items()}


def _non_negative_int(value: object) -> int:
    return int(value) if isinstance(value, int) and value >= 0 else 0


def _scalar_float(value: object) -> float:
    return float(value) if isinstance(value, int | float | Decimal) else 0.0


def _error_count(session: Any, workspace_id: UUID, pattern: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(PlanningRunModel)
            .where(
                PlanningRunModel.workspace_id == workspace_id,
                func.lower(PlanningRunModel.error_code).like(pattern),
            )
        )
        or 0
    )


def _status_count(rows: object, status: str) -> int:
    if not isinstance(rows, list | tuple):
        return 0
    return sum(int(row[1]) for row in rows if len(row) >= 2 and row[0] == status)


def _worker_transition_metric_outcome(status: str) -> str:
    return {
        "converged": "converged",
        "failed": "failed",
        "blocked_unknown": "unknown",
    }.get(status, "in_flight")


def _adapter_metric_label(adapter_ref: str | None) -> str:
    if adapter_ref is None:
        return "unknown"
    prefix = adapter_ref.split(":", 1)[0].strip().lower()
    return (
        prefix
        if prefix in {"akashic", "dsh", "fake", "opencode", "session"}
        else "other"
    )
