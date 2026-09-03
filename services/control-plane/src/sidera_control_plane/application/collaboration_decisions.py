from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.collaboration_governance_disposition import (
    CollaborationGovernanceDispositionService,
)
from sidera_control_plane.application.collaboration_projection import (
    CollaborationProjectionAssembler,
)
from sidera_control_plane.application.collaboration_release_validation import (
    CollaborationReleaseEligibilityValidator,
    ReleaseEligibilityAssessment,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import (
    CollaborationFactStorePort,
    ControlPlaneUnitOfWork,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationEvaluation,
    CollaborationEvaluationStatus,
    CollaborationGateMode,
    CollaborationRun,
    CollaborationRunStatus,
    RuntimeDecision,
    RuntimeIntent,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
    RuntimeRelease,
)
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationReleaseProposal,
)
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeDecisionOutcome,
    RuntimeProjectionV1,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution_requests import (
    ActivationDecision,
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.plan_revisions import RootTaskPlanHead

if TYPE_CHECKING:
    from sidera_control_plane.application.collaboration_reconciler import (
        PreparedCollaborationEvaluation,
    )
    from sidera_control_plane.application.collaboration_runtime_invoker import (
        RuntimeInvocationResult,
    )


FactStoreFactory = Callable[[ControlPlaneUnitOfWork], CollaborationFactStorePort]
PlanTransitionCheck = Callable[[ControlPlaneUnitOfWork, ExecutionRequest], bool]


@dataclass(frozen=True, slots=True)
class CollaborationGateFact:
    """The immutable collaboration contribution to an activation decision."""

    allows: bool
    reason: str | None = None
    details: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.allows and self.reason is not None:
            raise ValidationError("an allowed collaboration gate cannot have a reason")
        if not self.allows and (self.reason is None or not self.reason.strip()):
            raise ValidationError("a blocked collaboration gate requires a reason")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    @classmethod
    def unmanaged(cls) -> CollaborationGateFact:
        return cls(allows=True)

    @classmethod
    def run_pending(cls, *, run_id: UUID | None = None) -> CollaborationGateFact:
        return cls(
            allows=False,
            reason="collaboration_run_pending",
            details=_run_details(run_id),
        )

    @classmethod
    def run_blocked(cls, *, run_id: UUID | None = None) -> CollaborationGateFact:
        return cls(
            allows=False,
            reason="collaboration_run_blocked",
            details=_run_details(run_id),
        )

    @classmethod
    def release_pending(cls, *, run_id: UUID | None = None) -> CollaborationGateFact:
        return cls(
            allows=False,
            reason="collaboration_release_pending",
            details=_run_details(run_id),
        )

    @classmethod
    def released(cls, *, run_id: UUID) -> CollaborationGateFact:
        return cls(allows=True, details=_run_details(run_id))

    @classmethod
    def for_request(
        cls,
        request: ExecutionRequest,
        *,
        run: CollaborationRun | None,
        release: RuntimeRelease | None,
        plan_head: RootTaskPlanHead | None,
    ) -> CollaborationGateFact:
        if request.collaboration_gate_mode is CollaborationGateMode.UNMANAGED:
            return cls.unmanaged()
        if run is None:
            return cls.run_pending()
        if run.status is CollaborationRunStatus.BLOCKED:
            return cls.run_blocked(run_id=run.id)
        if run.status is not CollaborationRunStatus.ACTIVE:
            return cls.run_pending(run_id=run.id)
        if not _release_matches(
            request,
            run=run,
            release=release,
            plan_head=plan_head,
        ):
            return cls.release_pending(run_id=run.id)
        return cls.released(run_id=run.id)


class ComposedActivationGate:
    """Combine plan transition, static facts, and collaboration release state."""

    @staticmethod
    def evaluate(
        *,
        plan_transition_allows: bool,
        static_decision: ActivationDecision,
        collaboration: CollaborationGateFact,
    ) -> ActivationDecision:
        if not plan_transition_allows:
            return ActivationDecision(ready=False, reason="plan_generation_stale")
        if not static_decision.ready:
            return static_decision
        if not collaboration.allows:
            return ActivationDecision(
                ready=False,
                reason=collaboration.reason or "collaboration_release_pending",
                details=collaboration.details,
            )
        return ActivationDecision(ready=True, reason="ready")


@dataclass(frozen=True, slots=True)
class CollaborationDecisionApplicationResult:
    decision: RuntimeDecision
    intents: tuple[RuntimeIntent, ...]
    applications: tuple[RuntimeIntentApplication, ...]
    releases: tuple[RuntimeRelease, ...]
    proposals: tuple[CollaborationReleaseProposal, ...]
    status: str

    @property
    def release_ids(self) -> tuple[UUID, ...]:
        return tuple(item.id for item in self.releases)


_IntentAssessment = ReleaseEligibilityAssessment


class CollaborationDecisionService:
    """Apply one parsed Runtime Decision in one short, authoritative transaction."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        fact_store_factory: FactStoreFactory,
        projection_assembler: CollaborationProjectionAssembler | None = None,
        owner_id: str = "collaboration-worker",
        plan_transition_allows: PlanTransitionCheck | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        self._uow_factory = uow_factory
        self._fact_store_factory = fact_store_factory
        self._projection_assembler = (
            projection_assembler or CollaborationProjectionAssembler()
        )
        self._owner_id = owner_id.strip()
        self._release_validator = CollaborationReleaseEligibilityValidator()
        self._governance_disposition = CollaborationGovernanceDispositionService()
        self._plan_transition_allows = plan_transition_allows or (
            lambda _uow, _request: True
        )

    def apply(
        self,
        prepared: PreparedCollaborationEvaluation,
        result: RuntimeInvocationResult,
        *,
        now: datetime,
    ) -> CollaborationDecisionApplicationResult:
        _validate_now(now)
        self._validate_result(prepared, result)

        with self._uow_factory() as uow:
            evaluation = uow.collaboration_evaluations.get(
                prepared.run.workspace_id,
                prepared.evaluation.id,
                for_update=True,
            )
            run = uow.collaboration_runs.get(
                prepared.run.workspace_id,
                prepared.run.id,
                for_update=True,
            )
            if evaluation is None or run is None:
                raise CollaborationDecisionConflict(
                    "collaboration evaluation or run no longer exists"
                )
            self._validate_evaluation_lease(prepared, evaluation, now=now)
            if (
                evaluation.response_hash is not None
                and evaluation.response_hash != result.response_hash
            ):
                raise CollaborationDecisionConflict(
                    "Runtime response differs from the recorded evaluation response"
                )

            head = uow.plans.get_head(
                run.root_task_id,
                run.workspace_id,
                for_update=True,
            )
            target_ids = tuple(
                sorted(
                    {
                        item.target_execution_request_id
                        for item in result.decision.intents
                    }
                )
            )
            requests = {
                request_id: uow.execution_requests.get(
                    request_id,
                    run.workspace_id,
                    for_update=True,
                )
                for request_id in target_ids
            }
            releases = {
                request_id: uow.collaboration_ledger.get_release_for_request(
                    run.workspace_id,
                    request_id,
                    for_update=True,
                )
                for request_id in target_ids
            }
            if any(requests[request_id] is None for request_id in target_ids):
                raise CollaborationDecisionConflict(
                    "Runtime Decision targets an unknown request in this workspace"
                )

            stale_reason = self._freshness_reason(
                uow,
                prepared=prepared,
                run=run,
                head=head,
            )
            current_projection = None
            if stale_reason is None:
                current_projection = self._current_projection(
                    uow,
                    prepared=prepared,
                    run=run,
                )
                if current_projection.content_hash != prepared.projection.content_hash:
                    stale_reason = "projection_stale"

            decision = self._decision_record(
                prepared=prepared,
                result=result,
                sequence=uow.collaboration_ledger.next_decision_sequence(
                    run.id,
                    run.workspace_id,
                ),
                now=now,
            )
            intents = self._intent_records(
                decision,
                result,
                now=now,
            )
            uow.collaboration_ledger.add_decision_bundle(decision, intents)

            if result.decision.outcome is RuntimeDecisionOutcome.NO_CHANGE:
                assessments: tuple[_IntentAssessment, ...] = ()
                applications: tuple[RuntimeIntentApplication, ...] = ()
                new_releases: tuple[RuntimeRelease, ...] = ()
                proposals: tuple[CollaborationReleaseProposal, ...] = ()
                status = "stale" if stale_reason is not None else "no_change"
            else:
                assessments = self._assess_intents(
                    intents,
                    requests=requests,
                    releases=releases,
                    run=run,
                    head=head,
                    current_projection=current_projection,
                    stale_reason=stale_reason,
                )
                proposals = ()
                if current_projection is not None and not any(
                    item.status is RuntimeIntentApplicationStatus.STALE
                    for item in assessments
                ) and not any(
                    item.status is RuntimeIntentApplicationStatus.REJECTED
                    for item in assessments
                ):
                    disposition = self._governance_disposition.dispose(
                        uow=uow,
                        intents=intents,
                        assessments=assessments,
                        run=run,
                        current_projection=current_projection,
                        now=now,
                    )
                    assessments = disposition.assessments
                    proposals = disposition.proposals
                if any(
                    item.status is RuntimeIntentApplicationStatus.STALE
                    for item in assessments
                ):
                    assessments = tuple(
                        replace(
                            item,
                            status=RuntimeIntentApplicationStatus.STALE,
                            result_code=stale_reason or "projection_stale",
                            result_summary="the Runtime Decision was based on stale facts",
                        )
                        for item in assessments
                    )
                    status = "stale"
                elif any(
                    item.status is RuntimeIntentApplicationStatus.REJECTED
                    for item in assessments
                ):
                    assessments = tuple(
                        replace(
                            item,
                            status=RuntimeIntentApplicationStatus.REJECTED,
                            result_code=_first_rejection_code(assessments),
                            result_summary="the complete Runtime Decision was rejected",
                        )
                        for item in assessments
                    )
                    status = "rejected"
                elif all(
                    item.status is RuntimeIntentApplicationStatus.DUPLICATE
                    for item in assessments
                ):
                    status = "duplicate"
                elif proposals and not any(
                    item.status is RuntimeIntentApplicationStatus.APPLIED
                    for item in assessments
                ):
                    status = "deferred"
                else:
                    status = "applied"

                new_releases = self._new_releases(
                    intents,
                    assessments,
                    run=run,
                    decision=decision,
                    now=now,
                )
                if new_releases:
                    uow.collaboration_ledger.add_releases(new_releases)
                    # The repository intentionally exposes immutable ledger rows
                    # without ORM relationships; establish Release FKs before
                    # adding Applications that point at them.
                    uow.flush()
                applications = self._applications(
                    intents,
                    assessments,
                    new_releases=new_releases,
                    expected_run_version=prepared.run.resource_version,
                    observed_run_version=run.resource_version,
                    now=now,
                )
                uow.collaboration_ledger.add_applications(applications)
                self._update_request_gates(
                    uow,
                    assessments,
                    releases=releases,
                    new_releases=new_releases,
                    run=run,
                    head=head,
                    current_projection=current_projection,
                    now=now,
                )

            response_hash = result.response_hash
            previous_evaluation_version = evaluation.resource_version
            evaluation.complete(now=now, response_hash=response_hash)
            uow.collaboration_evaluations.save(
                evaluation,
                previous_evaluation_version,
            )
            self._emit_evaluation_completed(uow, evaluation, run)
            self._emit_decision_recorded(
                uow,
                decision,
                run=run,
                application_status=status,
                intent_count=len(intents),
            )
            for release in new_releases:
                intent = next(item for item in intents if item.id == release.intent_id)
                request = requests[release.execution_request_id]
                if request is not None:
                    self._emit_release_issued(
                        uow,
                        release,
                        request=request,
                        run=run,
                        intent=intent,
                    )
            for proposal in proposals:
                self._emit_proposal_created(uow, proposal, run=run)
            if stale_reason is not None:
                uow.projection_changes.changed(
                    workspace_id=run.workspace_id,
                    projection_type="collaboration_runtime",
                    resource_id=run.root_task_id,
                    change_type="collaboration_decision_stale",
                    resource_version=run.resource_version,
                    correlation_id=decision.id,
                )

            final_cursor = max(
                prepared.projection.fact_cursor,
                uow.projection_changes.latest_target_cursor(
                    run.workspace_id,
                    "collaboration_runtime",
                    run.root_task_id,
                ),
            )
            previous_run_version = run.resource_version
            run.record_decision(
                decision_id=decision.id,
                fact_cursor=final_cursor,
                now=now,
            )
            uow.collaboration_runs.save(run, previous_run_version)
            uow.flush()

            return CollaborationDecisionApplicationResult(
                decision=decision,
                intents=intents,
                applications=applications,
                releases=new_releases,
                proposals=proposals,
                status=status,
            )

    def _validate_result(
        self,
        prepared: PreparedCollaborationEvaluation,
        result: RuntimeInvocationResult,
    ) -> None:
        decision = result.decision
        if (
            decision.operation_id != prepared.operation_id
            or decision.run_id != prepared.run.id
            or decision.projection_id != prepared.projection.projection_id
            or decision.projection_hash != prepared.projection.content_hash
        ):
            raise CollaborationDecisionConflict(
                "Runtime Decision identity does not match the prepared evaluation"
            )
        if (
            result.response_bytes != result.decision.canonical_bytes()
            or sha256(result.response_bytes).hexdigest() != result.response_hash
        ):
            raise CollaborationDecisionConflict("Runtime response hash is invalid")

    @staticmethod
    def _validate_evaluation_lease(
        prepared: PreparedCollaborationEvaluation,
        evaluation: CollaborationEvaluation,
        *,
        now: datetime,
    ) -> None:
        if (
            evaluation.id != prepared.evaluation.id
            or evaluation.workspace_id != prepared.run.workspace_id
            or evaluation.run_id != prepared.run.id
            or evaluation.projection_snapshot_id != prepared.projection.projection_id
            or evaluation.operation_id != prepared.operation_id
            or evaluation.status is not CollaborationEvaluationStatus.EVALUATING
            or evaluation.lease_owner != prepared.lease_owner
            or evaluation.lease_expires_at is None
            or evaluation.lease_expires_at <= now
        ):
            raise CollaborationDecisionConflict(
                "collaboration evaluation lease is no longer current"
            )

    @staticmethod
    def _freshness_reason(
        uow: ControlPlaneUnitOfWork,
        *,
        prepared: PreparedCollaborationEvaluation,
        run: CollaborationRun,
        head: RootTaskPlanHead | None,
    ) -> str | None:
        if head is None or not _head_matches_run(head, run):
            return "plan_generation_stale"
        if run.last_projection_snapshot_id != prepared.projection.projection_id:
            return "projection_stale"
        if run.resource_version != prepared.run.resource_version:
            return "projection_stale"
        stored = uow.collaboration_ledger.get_projection(
            run.workspace_id,
            prepared.projection.projection_id,
        )
        if (
            stored is None
            or stored.content_hash != prepared.projection.content_hash
            or stored.plan_snapshot_id != prepared.projection.plan_snapshot_id
            or stored.plan_generation != prepared.projection.plan_generation
            or stored.plan_content_hash != prepared.projection.plan_content_hash
        ):
            return "projection_stale"
        return None

    def _current_projection(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        prepared: PreparedCollaborationEvaluation,
        run: CollaborationRun,
    ) -> RuntimeProjectionV1:
        facts = self._fact_store_factory(uow).load(
            run.workspace_id,
            run,
            fact_cursor=prepared.projection.fact_cursor,
        )
        # record_projection increments the Run version after the Projection is
        # assembled. Normalize that preparation-only increment for comparison.
        if (
            facts.plan_head.run_resource_version
            != prepared.projection.run_resource_version
        ):
            facts = replace(
                facts,
                plan_head=replace(
                    facts.plan_head,
                    run_resource_version=prepared.projection.run_resource_version,
                ),
            )
        return self._projection_assembler.assemble(
            facts,
            fact_cursor=prepared.projection.fact_cursor,
            projection_id=prepared.projection.projection_id,
        )

    @staticmethod
    def _decision_record(
        *,
        prepared: PreparedCollaborationEvaluation,
        result: RuntimeInvocationResult,
        sequence: int,
        now: datetime,
    ) -> RuntimeDecision:
        decision = result.decision
        return RuntimeDecision(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera:collaboration-decision:{prepared.evaluation.id}:"
                f"{result.response_hash}",
            ),
            workspace_id=prepared.run.workspace_id,
            run_id=prepared.run.id,
            evaluation_id=prepared.evaluation.id,
            projection_snapshot_id=prepared.projection.projection_id,
            projection_hash=decision.projection_hash,
            operation_id=prepared.operation_id,
            schema_version=decision.schema_version,
            sequence=sequence,
            outcome=decision.outcome.value,
            reason_code=decision.reason_code,
            summary=decision.summary,
            strategy_state=decision.strategy_state,
            content_hash=decision.content_hash,
            runtime_duration_ms=result.duration_ms,
            created_at=now,
        )

    @staticmethod
    def _intent_records(
        decision: RuntimeDecision,
        result: RuntimeInvocationResult,
        *,
        now: datetime,
    ) -> tuple[RuntimeIntent, ...]:
        return tuple(
            RuntimeIntent(
                id=uuid5(
                    NAMESPACE_URL,
                    f"sidera:collaboration-intent:{decision.id}:{item.ordinal}",
                ),
                workspace_id=decision.workspace_id,
                run_id=decision.run_id,
                decision_id=decision.id,
                ordinal=item.ordinal,
                intent_type=item.intent_type,
                target_execution_request_id=item.target_execution_request_id,
                target_request_version=item.target_request_version,
                reason_code=item.reason_code,
                summary=item.summary,
                idempotency_key=item.idempotency_key,
                payload_json=item.to_mapping(),
                created_at=now,
            )
            for item in result.decision.intents
        )

    def _assess_intents(
        self,
        intents: tuple[RuntimeIntent, ...],
        *,
        requests: Mapping[UUID, ExecutionRequest | None],
        releases: Mapping[UUID, RuntimeRelease | None],
        run: CollaborationRun,
        head: RootTaskPlanHead | None,
        current_projection: RuntimeProjectionV1 | None,
        stale_reason: str | None,
    ) -> tuple[_IntentAssessment, ...]:
        if stale_reason is not None:
            return tuple(
                _IntentAssessment(
                    status=RuntimeIntentApplicationStatus.STALE,
                    result_code=stale_reason,
                    result_summary="the Runtime Decision was based on stale facts",
                    request=requests.get(item.target_execution_request_id),
                    existing_release=releases.get(item.target_execution_request_id),
                )
                for item in intents
            )
        assessments: list[_IntentAssessment] = []
        seen_targets: set[UUID] = set()
        for intent in intents:
            request_id = intent.target_execution_request_id
            request = requests.get(request_id)
            existing = releases.get(request_id)
            if request_id in seen_targets:
                assessments.append(
                    _rejected(
                        request,
                        "decision_conflict",
                        "a Decision cannot contain two intents for one request",
                        existing_release=existing,
                    )
                )
                continue
            seen_targets.add(request_id)
            assessments.append(
                self._release_validator.assess(
                    intent,
                    request=request,
                    run=run,
                    plan_head=head,
                    current_projection=current_projection,
                    existing_release=existing,
                )
            )
        return tuple(assessments)

    @staticmethod
    def _applications(
        intents: tuple[RuntimeIntent, ...],
        assessments: tuple[_IntentAssessment, ...],
        *,
        new_releases: tuple[RuntimeRelease, ...],
        expected_run_version: int,
        observed_run_version: int,
        now: datetime,
    ) -> tuple[RuntimeIntentApplication, ...]:
        release_by_intent = {item.intent_id: item.id for item in new_releases}
        return tuple(
            RuntimeIntentApplication(
                id=uuid5(
                    NAMESPACE_URL, f"sidera:collaboration-application:{intent.id}"
                ),
                workspace_id=intent.workspace_id,
                intent_id=intent.id,
                status=assessment.status,
                expected_run_version=expected_run_version,
                observed_run_version=observed_run_version,
                expected_request_version=intent.target_request_version,
                observed_request_version=(
                    assessment.request.version
                    if assessment.request is not None
                    else None
                ),
                release_id=(
                    assessment.existing_release.id
                    if assessment.status is RuntimeIntentApplicationStatus.DUPLICATE
                    and assessment.existing_release is not None
                    else release_by_intent.get(intent.id)
                ),
                result_code=assessment.result_code,
                result_summary=assessment.result_summary,
                resource_version=1,
                created_at=now,
                applied_at=(
                    None
                    if assessment.status is RuntimeIntentApplicationStatus.DEFERRED
                    else now
                ),
            )
            for intent, assessment in zip(intents, assessments, strict=True)
        )

    @staticmethod
    def _new_releases(
        intents: tuple[RuntimeIntent, ...],
        assessments: tuple[_IntentAssessment, ...],
        *,
        run: CollaborationRun,
        decision: RuntimeDecision,
        now: datetime,
    ) -> tuple[RuntimeRelease, ...]:
        return tuple(
            RuntimeRelease(
                id=uuid5(NAMESPACE_URL, f"sidera:collaboration-release:{intent.id}"),
                workspace_id=run.workspace_id,
                run_id=run.id,
                plan_snapshot_id=run.plan_snapshot_id,
                plan_generation=run.plan_generation,
                plan_content_hash=run.plan_content_hash,
                decision_id=decision.id,
                intent_id=intent.id,
                execution_request_id=intent.target_execution_request_id,
                request_version_at_release=intent.target_request_version,
                issued_at=now,
            )
            for intent, assessment in zip(intents, assessments, strict=True)
            if assessment.status is RuntimeIntentApplicationStatus.APPLIED
        )

    def _update_request_gates(
        self,
        uow: ControlPlaneUnitOfWork,
        assessments: tuple[_IntentAssessment, ...],
        *,
        releases: Mapping[UUID, RuntimeRelease | None],
        new_releases: tuple[RuntimeRelease, ...],
        run: CollaborationRun,
        head: RootTaskPlanHead | None,
        current_projection: RuntimeProjectionV1 | None,
        now: datetime,
    ) -> None:
        new_by_request = {item.execution_request_id: item for item in new_releases}
        entries = _projection_requests(current_projection)
        for assessment in assessments:
            if assessment.status not in {
                RuntimeIntentApplicationStatus.APPLIED,
                RuntimeIntentApplicationStatus.DUPLICATE,
            }:
                continue
            request = assessment.request
            if request is None or request.status not in {
                ExecutionRequestStatus.BLOCKED,
                ExecutionRequestStatus.READY,
            }:
                continue
            entry = entries.get(request.id)
            static_decision = _static_decision(entry)
            release = new_by_request.get(request.id) or releases.get(request.id)
            collaboration = CollaborationGateFact.for_request(
                request,
                run=run,
                release=release,
                plan_head=head,
            )
            decision = ComposedActivationGate.evaluate(
                plan_transition_allows=self._plan_transition_allows(uow, request),
                static_decision=static_decision,
                collaboration=collaboration,
            )
            if decision.ready:
                if (
                    request.gate_reason == "ready"
                    and request.status is ExecutionRequestStatus.READY
                ):
                    continue
                previous_version = request.version
                request.mark_ready(now=now, decision=decision)
                uow.execution_requests.save(request, previous_version)
            elif (
                request.gate_reason != decision.reason
                or request.status is not ExecutionRequestStatus.BLOCKED
            ):
                previous_version = request.version
                request.refresh_gate(decision=decision, now=now)
                uow.execution_requests.save(request, previous_version)

    def _emit_evaluation_completed(
        self,
        uow: ControlPlaneUnitOfWork,
        evaluation: CollaborationEvaluation,
        run: CollaborationRun,
    ) -> None:
        self._emit_event(
            uow,
            event_type="CollaborationEvaluationCompleted",
            aggregate_type="collaboration_evaluation",
            aggregate_id=evaluation.id,
            aggregate_version=evaluation.resource_version,
            run=run,
            payload={
                "evaluation_id": str(evaluation.id),
                "operation_id": str(evaluation.operation_id),
                "status": evaluation.status.value,
            },
        )

    def _emit_decision_recorded(
        self,
        uow: ControlPlaneUnitOfWork,
        decision: RuntimeDecision,
        *,
        run: CollaborationRun,
        application_status: str,
        intent_count: int,
    ) -> None:
        self._emit_event(
            uow,
            event_type="CollaborationDecisionRecorded",
            aggregate_type="runtime_decision",
            aggregate_id=decision.id,
            aggregate_version=decision.sequence,
            run=run,
            payload={
                "decision_id": str(decision.id),
                "evaluation_id": str(decision.evaluation_id),
                "projection_snapshot_id": str(decision.projection_snapshot_id),
                "operation_id": str(decision.operation_id),
                "sequence": decision.sequence,
                "outcome": decision.outcome,
                "reason_code": decision.reason_code,
                "summary": decision.summary,
                "intent_count": intent_count,
                "application_status": application_status,
            },
        )

    def _emit_release_issued(
        self,
        uow: ControlPlaneUnitOfWork,
        release: RuntimeRelease,
        *,
        request: ExecutionRequest,
        run: CollaborationRun,
        intent: RuntimeIntent,
    ) -> None:
        self._emit_event(
            uow,
            event_type="ExecutionRequestReleased",
            aggregate_type="execution_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            run=run,
            payload={
                "release_id": str(release.id),
                "decision_id": str(release.decision_id),
                "intent_id": str(intent.id),
                "execution_request_id": str(request.id),
                "request_version_at_release": release.request_version_at_release,
                "status": request.status.value,
                "gate_reason": request.gate_reason,
            },
        )

    def _emit_proposal_created(
        self,
        uow: ControlPlaneUnitOfWork,
        proposal: CollaborationReleaseProposal,
        *,
        run: CollaborationRun,
    ) -> None:
        self._emit_event(
            uow,
            event_type="CollaborationReleaseProposalCreated",
            aggregate_type="collaboration_release_proposal",
            aggregate_id=proposal.id,
            aggregate_version=proposal.resource_version,
            run=run,
            payload={
                "proposal_id": str(proposal.id),
                "hold_id": str(proposal.hold_id),
                "execution_request_id": str(proposal.execution_request_id),
                "intent_id": str(proposal.intent_id),
                "status": proposal.status.value,
                "reason_code": proposal.reason_code,
                "input_fingerprint_version": proposal.input_fingerprint_version,
            },
        )

    def _emit_event(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        run: CollaborationRun,
        payload: dict[str, object],
    ) -> None:
        command_id = uuid5(
            NAMESPACE_URL,
            f"sidera:collaboration-event:{event_type}:{aggregate_id}:v{aggregate_version}",
        )
        command = CommandEnvelope(
            id=command_id,
            workspace_id=run.workspace_id,
            command_type="collaboration.decision.apply",
            target_type=aggregate_type,
            actor_type="system",
            actor_id=self._owner_id,
            target_id=aggregate_id,
            payload=payload,
            expected_version=aggregate_version - 1,
            idempotency_key=(
                f"collaboration-event:{event_type}:{aggregate_id}:v{aggregate_version}"
            ),
            correlation_id=run.id,
        )
        event_payload = {
            "root_task_id": str(run.root_task_id),
            "run_id": str(run.id),
            "plan_snapshot_id": str(run.plan_snapshot_id),
            "plan_generation": run.plan_generation,
            **payload,
        }
        event = EventData(
            id=uuid4(),
            workspace_id=run.workspace_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            event_type=event_type,
            payload=event_payload,
            actor_type="system",
            actor_id=self._owner_id,
            causation_id=command.id,
            correlation_id=run.id,
        )
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command.id,
                aggregate_id=aggregate_id,
                aggregate_version=aggregate_version,
                event_ids=(event.id,),
            ),
        )
        uow.events.append_with_outbox(event)


class CollaborationDecisionConflict(RuntimeError):
    """The response cannot safely be joined to the prepared evaluation."""


def _head_matches_run(head: RootTaskPlanHead, run: CollaborationRun) -> bool:
    return (
        head.workspace_id == run.workspace_id
        and head.root_task_id == run.root_task_id
        and head.active_snapshot_id == run.plan_snapshot_id
        and head.active_generation == run.plan_generation
        and head.active_snapshot_hash == run.plan_content_hash
    )


def _request_matches_run(request: ExecutionRequest, run: CollaborationRun) -> bool:
    return (
        request.workspace_id == run.workspace_id
        and request.root_task_id == run.root_task_id
        and request.plan_snapshot_id == run.plan_snapshot_id
        and request.plan_generation == run.plan_generation
    )


def _release_matches(
    request: ExecutionRequest,
    *,
    run: CollaborationRun | None,
    release: RuntimeRelease | None,
    plan_head: RootTaskPlanHead | None,
) -> bool:
    if run is None or release is None or plan_head is None:
        return False
    return (
        release.workspace_id == request.workspace_id == run.workspace_id
        and release.run_id == run.id
        and release.plan_snapshot_id == request.plan_snapshot_id == run.plan_snapshot_id
        and release.plan_generation == request.plan_generation == run.plan_generation
        and release.plan_content_hash == run.plan_content_hash
        and release.execution_request_id == request.id
        and _head_matches_run(plan_head, run)
        and release.request_version_at_release <= request.version
    )


def _run_details(run_id: UUID | None) -> Mapping[str, object]:
    return MappingProxyType({"run_id": str(run_id)} if run_id is not None else {})


def _projection_requests(
    projection: RuntimeProjectionV1 | None,
) -> dict[UUID, Mapping[str, object]]:
    if projection is None:
        return {}
    result: dict[UUID, Mapping[str, object]] = {}
    for item in projection.execution_requests:
        value = item.get("id")
        if isinstance(value, str):
            try:
                result[UUID(value)] = item
            except ValueError:
                continue
    return result


def _static_decision(
    entry: Mapping[str, object] | None,
) -> ActivationDecision:
    if entry is None or entry.get("eligible") is True:
        return ActivationDecision(ready=True, reason="ready")
    reason = entry.get("eligibility_reason")
    clean_reason = (
        reason if isinstance(reason, str) and reason.strip() else "dependencies_blocked"
    )
    if clean_reason == "already_released":
        return ActivationDecision(ready=True, reason="ready")
    return ActivationDecision(
        ready=False,
        reason=clean_reason,
        details={"eligibility_reason": clean_reason},
    )


def _rejected(
    request: ExecutionRequest | None,
    code: str,
    summary: str,
    *,
    existing_release: RuntimeRelease | None = None,
) -> _IntentAssessment:
    return _IntentAssessment(
        status=RuntimeIntentApplicationStatus.REJECTED,
        result_code=code,
        result_summary=summary,
        request=request,
        existing_release=existing_release,
    )


def _first_rejection_code(assessments: tuple[_IntentAssessment, ...]) -> str:
    return next(
        (
            item.result_code
            for item in assessments
            if item.status is RuntimeIntentApplicationStatus.REJECTED
        ),
        "decision_rejected",
    )


def _validate_now(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
