from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sidera_control_plane.domain.artifacts import ArtifactType, EvidenceClaimStatus
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    CollaborationPolicySnapshotV1,
)
from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.execution import ExecutionStatus


class ExecutionRequestStatus(StrEnum):
    BLOCKED = "blocked"
    READY = "ready"
    MATERIALIZED = "materialized"
    CANCELLED = "cancelled"
    OBSOLETE = "obsolete"


class DependencyEnforcement(StrEnum):
    HARD = "hard"
    INFORMATIONAL = "informational"


@dataclass(frozen=True, slots=True)
class ExecutionRequestDependency:
    downstream_request_id: UUID
    upstream_request_id: UUID
    enforcement: DependencyEnforcement
    required_artifact_refs: tuple[str, ...] = ()
    required_evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.downstream_request_id, UUID) or not isinstance(
            self.upstream_request_id, UUID
        ):
            raise ValidationError("execution request dependency ids must be UUIDs")
        if self.downstream_request_id == self.upstream_request_id:
            raise ValidationError("execution request dependency cannot be a self edge")
        object.__setattr__(
            self,
            "enforcement",
            _dependency_enforcement(self.enforcement),
        )
        object.__setattr__(
            self,
            "required_artifact_refs",
            _artifact_refs(self.required_artifact_refs, "required artifact refs"),
        )
        object.__setattr__(
            self,
            "required_evidence_refs",
            _evidence_refs(self.required_evidence_refs, "required evidence refs"),
        )

    @property
    def is_blocking(self) -> bool:
        return self.enforcement is DependencyEnforcement.HARD


@dataclass(frozen=True, slots=True)
class UpstreamActivationFact:
    request_id: UUID
    execution_status: ExecutionStatus | None = None
    execution_lineage_id: UUID | None = None
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    evidence_status: EvidenceClaimStatus | str = EvidenceClaimStatus.CANDIDATE

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, UUID):
            raise ValidationError("upstream activation request_id must be a UUID")
        if self.execution_status is not None:
            object.__setattr__(
                self,
                "execution_status",
                _execution_status(self.execution_status),
            )
        if self.execution_lineage_id is not None and not isinstance(
            self.execution_lineage_id, UUID
        ):
            raise ValidationError("execution_lineage_id must be a UUID")
        object.__setattr__(
            self,
            "artifact_refs",
            _artifact_refs(self.artifact_refs, "artifact refs"),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _evidence_refs(self.evidence_refs, "evidence refs"),
        )
        object.__setattr__(
            self,
            "evidence_status",
            _evidence_status(self.evidence_status),
        )

    @property
    def produced_artifact_refs(self) -> tuple[str, ...]:
        return self.artifact_refs

    @property
    def candidate_evidence_refs(self) -> tuple[str, ...]:
        return self.evidence_refs


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    ready: bool
    reason: str
    missing_upstream_request_ids: tuple[UUID, ...] = ()
    blocked_upstream_request_ids: tuple[UUID, ...] = ()
    missing_artifact_refs: tuple[str, ...] = ()
    missing_evidence_refs: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValidationError("activation decision reason is required")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    @property
    def is_ready(self) -> bool:
        return self.ready

    @property
    def code(self) -> str:
        return self.reason


class ActivationGate:
    """Pure evaluator for static ExecutionRequest activation dependencies."""

    @staticmethod
    def evaluate(
        *,
        dependencies: Iterable[ExecutionRequestDependency] = (),
        upstream_facts: Mapping[UUID, UpstreamActivationFact]
        | Iterable[UpstreamActivationFact]
        | None = None,
        facts: Mapping[UUID, UpstreamActivationFact]
        | Iterable[UpstreamActivationFact]
        | None = None,
    ) -> ActivationDecision:
        if upstream_facts is not None and facts is not None:
            raise ValidationError("provide upstream_facts or facts, not both")
        source = upstream_facts if upstream_facts is not None else facts
        facts_by_id = _fact_map(source or ())

        missing_upstream: list[UUID] = []
        blocked_upstream: list[UUID] = []
        missing_artifacts: list[str] = []
        missing_evidence: list[str] = []

        for dependency in dependencies:
            if not dependency.is_blocking:
                continue
            upstream = facts_by_id.get(dependency.upstream_request_id)
            if upstream is None:
                _append_unique(missing_upstream, dependency.upstream_request_id)
                continue
            if (
                upstream.execution_status is not ExecutionStatus.SUCCEEDED
                or upstream.execution_lineage_id is None
            ):
                _append_unique(blocked_upstream, dependency.upstream_request_id)
                continue
            for ref in dependency.required_artifact_refs:
                if ref not in upstream.artifact_refs:
                    _append_unique(missing_artifacts, ref)
            for ref in dependency.required_evidence_refs:
                if (
                    upstream.evidence_status is not EvidenceClaimStatus.CANDIDATE
                    or ref not in upstream.evidence_refs
                ):
                    _append_unique(missing_evidence, ref)

        ready = not (
            missing_upstream
            or blocked_upstream
            or missing_artifacts
            or missing_evidence
        )
        reason = "ready" if ready else "dependencies_blocked"
        return ActivationDecision(
            ready=ready,
            reason=reason,
            missing_upstream_request_ids=tuple(missing_upstream),
            blocked_upstream_request_ids=tuple(blocked_upstream),
            missing_artifact_refs=tuple(missing_artifacts),
            missing_evidence_refs=tuple(missing_evidence),
            details=MappingProxyType(
                {
                    "missing_upstream_request_ids": tuple(missing_upstream),
                    "blocked_upstream_request_ids": tuple(blocked_upstream),
                    "missing_artifact_refs": tuple(missing_artifacts),
                    "missing_evidence_refs": tuple(missing_evidence),
                }
            ),
        )


@dataclass(slots=True)
class ExecutionRequest:
    id: UUID
    workspace_id: UUID
    planning_request_id: UUID
    proposal_revision_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    root_task_id: UUID
    subtask_id: UUID
    worker_item_id: UUID
    worker_item_key: str
    intent_key: str
    purpose: str
    capability_requirements: frozenset[str]
    adapter_preferences: tuple[str, ...]
    budget_class: str
    not_before: datetime
    max_attempts: int
    expected_artifact_refs: tuple[str, ...]
    expected_evidence_refs: tuple[str, ...]
    status: ExecutionRequestStatus
    gate_reason: str
    gate_details: Mapping[str, object]
    gate_evaluated_at: datetime | None
    status_changed_at: datetime
    execution_lineage_id: UUID | None
    first_execution_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
    expected_outputs: tuple[str, ...] = ()
    adapter_forbidden: frozenset[str] = frozenset()
    source_execution_id: UUID | None = None
    source_session_id: UUID | None = None
    handoff_transition_id: UUID | None = None
    collaboration_gate_mode: CollaborationGateMode = CollaborationGateMode.UNMANAGED

    @classmethod
    def create(
        cls,
        *,
        request_id: UUID,
        workspace_id: UUID,
        planning_request_id: UUID,
        proposal_revision_id: UUID,
        plan_snapshot_id: UUID,
        plan_generation: int,
        root_task_id: UUID,
        subtask_id: UUID,
        worker_item_id: UUID,
        worker_item_key: str,
        intent_key: str,
        purpose: str,
        capability_requirements: frozenset[str] | Iterable[str],
        adapter_preferences: tuple[str, ...] | Iterable[str],
        budget_class: str,
        not_before: datetime,
        max_attempts: int,
        expected_artifact_refs: tuple[str, ...] | Iterable[str],
        expected_evidence_refs: tuple[str, ...] | Iterable[str],
        now: datetime,
        expected_outputs: tuple[str, ...] | Iterable[str] = (),
        adapter_forbidden: tuple[str, ...] | Iterable[str] = (),
        collaboration_gate_mode: CollaborationGateMode | str = (
            CollaborationGateMode.UNMANAGED
        ),
        source_execution_id: UUID | None = None,
        source_session_id: UUID | None = None,
        handoff_transition_id: UUID | None = None,
    ) -> ExecutionRequest:
        for name, value in (
            ("request_id", request_id),
            ("workspace_id", workspace_id),
            ("planning_request_id", planning_request_id),
            ("proposal_revision_id", proposal_revision_id),
            ("plan_snapshot_id", plan_snapshot_id),
            ("root_task_id", root_task_id),
            ("subtask_id", subtask_id),
            ("worker_item_id", worker_item_id),
        ):
            if not isinstance(value, UUID):
                raise ValidationError(f"{name} must be a UUID")
        for name, value in (
            ("source_execution_id", source_execution_id),
            ("source_session_id", source_session_id),
            ("handoff_transition_id", handoff_transition_id),
        ):
            if value is not None and not isinstance(value, UUID):
                raise ValidationError(f"{name} must be a UUID")
        if source_session_id is not None and source_execution_id is None:
            raise ValidationError("source_session_id requires source_execution_id")
        if handoff_transition_id is not None and (
            source_execution_id is None or source_session_id is None
        ):
            raise ValidationError(
                "handoff target requires source execution and session lineage"
            )
        if plan_generation < 1:
            raise ValidationError("plan_generation must be positive")
        clean_key = _required_text(worker_item_key, "worker_item_key")
        clean_intent = _required_text(intent_key, "intent_key")
        clean_purpose = _required_text(purpose, "purpose")
        clean_budget = _required_text(budget_class, "budget_class")
        capabilities = frozenset(
            _clean_strings(capability_requirements, "capability_requirements")
        )
        if not capabilities:
            raise ValidationError("capability_requirements are required")
        preferences = _clean_strings(adapter_preferences, "adapter_preferences")
        forbidden = frozenset(_clean_strings(adapter_forbidden, "adapter_forbidden"))
        if set(preferences).intersection(forbidden):
            raise ValidationError(
                "adapter preferences cannot contain forbidden adapter types"
            )
        if max_attempts < 1:
            raise ValidationError("max_attempts must be positive")
        _aware(not_before, "not_before")
        _aware(now, "now")
        artifact_refs = _artifact_refs(expected_artifact_refs, "expected artifact refs")
        evidence_refs = _evidence_refs(expected_evidence_refs, "expected evidence refs")
        outputs = _clean_strings(expected_outputs, "expected outputs")
        gate_mode = _collaboration_gate_mode(collaboration_gate_mode)
        return cls(
            id=request_id,
            workspace_id=workspace_id,
            planning_request_id=planning_request_id,
            proposal_revision_id=proposal_revision_id,
            plan_snapshot_id=plan_snapshot_id,
            plan_generation=plan_generation,
            root_task_id=root_task_id,
            subtask_id=subtask_id,
            worker_item_id=worker_item_id,
            worker_item_key=clean_key,
            intent_key=clean_intent,
            purpose=clean_purpose,
            capability_requirements=capabilities,
            adapter_preferences=preferences,
            budget_class=clean_budget,
            not_before=not_before,
            max_attempts=max_attempts,
            expected_artifact_refs=artifact_refs,
            expected_evidence_refs=evidence_refs,
            status=ExecutionRequestStatus.BLOCKED,
            gate_reason="not_evaluated",
            gate_details=MappingProxyType({}),
            gate_evaluated_at=None,
            status_changed_at=now,
            execution_lineage_id=None,
            first_execution_id=None,
            version=1,
            created_at=now,
            updated_at=now,
            expected_outputs=outputs,
            adapter_forbidden=forbidden,
            source_execution_id=source_execution_id,
            source_session_id=source_session_id,
            handoff_transition_id=handoff_transition_id,
            collaboration_gate_mode=gate_mode,
        )

    @property
    def subtask_task_id(self) -> UUID:
        return self.subtask_id

    @property
    def dependency_key(self) -> tuple[UUID, str]:
        return self.proposal_revision_id, self.worker_item_key

    def mark_ready(
        self,
        *,
        now: datetime,
        decision: ActivationDecision | None = None,
    ) -> None:
        if self.status not in {
            ExecutionRequestStatus.BLOCKED,
            ExecutionRequestStatus.READY,
        }:
            raise InvalidTransition(f"request cannot become ready from {self.status}")
        if decision is not None and not decision.ready:
            raise ValidationError("a blocked activation decision cannot mark ready")
        _aware(now, "now")
        self.status = ExecutionRequestStatus.READY
        self._record_gate(decision=decision, now=now)

    def refresh_gate(self, *, decision: ActivationDecision, now: datetime) -> None:
        if self.status in {
            ExecutionRequestStatus.MATERIALIZED,
            ExecutionRequestStatus.CANCELLED,
            ExecutionRequestStatus.OBSOLETE,
        }:
            raise InvalidTransition(f"request gate is closed from {self.status}")
        _aware(now, "now")
        if decision.ready:
            self.status = ExecutionRequestStatus.READY
        else:
            self.status = ExecutionRequestStatus.BLOCKED
        self._record_gate(decision=decision, now=now)

    def materialize(
        self,
        *,
        execution_lineage_id: UUID,
        first_execution_id: UUID,
        now: datetime,
    ) -> None:
        if self.status is not ExecutionRequestStatus.READY:
            raise InvalidTransition(
                f"only a ready request can be materialized, got {self.status}"
            )
        if not isinstance(execution_lineage_id, UUID) or not isinstance(
            first_execution_id, UUID
        ):
            raise ValidationError(
                "execution lineage and first execution ids are required"
            )
        if self.execution_lineage_id is not None or self.first_execution_id is not None:
            raise InvalidTransition("execution request has already been materialized")
        _aware(now, "now")
        self.status = ExecutionRequestStatus.MATERIALIZED
        self.execution_lineage_id = execution_lineage_id
        self.first_execution_id = first_execution_id
        self.status_changed_at = now
        self.updated_at = now
        self.version += 1

    def build_handoff_target(
        self,
        *,
        request_id: UUID,
        execution_lineage_id: UUID,
        first_execution_id: UUID,
        source_execution_id: UUID,
        source_session_id: UUID,
        handoff_transition_id: UUID,
        now: datetime,
    ) -> ExecutionRequest:
        """Clone immutable plan facts into a new materialized handoff request."""

        if self.status is not ExecutionRequestStatus.MATERIALIZED:
            raise InvalidTransition("only a materialized request can create a handoff target")
        if self.execution_lineage_id is None or self.first_execution_id is None:
            raise ValidationError("materialized request lineage is incomplete")
        return _materialized_handoff_target(
            source=self,
            request_id=request_id,
            execution_lineage_id=execution_lineage_id,
            first_execution_id=first_execution_id,
            source_execution_id=source_execution_id,
            source_session_id=source_session_id,
            handoff_transition_id=handoff_transition_id,
            now=now,
        )

    def cancel(self, *, now: datetime, reason: str = "cancelled") -> None:
        self._close(status=ExecutionRequestStatus.CANCELLED, now=now, reason=reason)

    def obsolete(self, *, now: datetime, reason: str = "obsolete") -> None:
        self._close(status=ExecutionRequestStatus.OBSOLETE, now=now, reason=reason)

    def retire_handoff_target(
        self,
        *,
        now: datetime,
        reason: str = "handoff_recovery_retry",
    ) -> None:
        """Retire a materialized handoff target before creating a new attempt.

        A handoff target request is materialized before the external target
        session starts. Recovery must preserve that attempt as terminal audit
        history while releasing the partial unique handoff slot. The request
        schema requires terminal requests to have no materialized lineage, so
        the retired IDs remain in gate details and the transition/execution
        records remain the authoritative runtime history.
        """

        if self.status is not ExecutionRequestStatus.MATERIALIZED:
            raise InvalidTransition(
                f"only a materialized handoff target can be retired, got {self.status}"
            )
        if self.handoff_transition_id is None:
            raise ValidationError("handoff target transition is required for retirement")
        _aware(now, "now")
        clean_reason = _required_text(reason, "reason")
        self.status = ExecutionRequestStatus.OBSOLETE
        self.gate_reason = clean_reason
        self.gate_details = MappingProxyType(
            {
                "reason": clean_reason,
                "retired_execution_lineage_id": (
                    str(self.execution_lineage_id)
                    if self.execution_lineage_id is not None
                    else None
                ),
                "retired_first_execution_id": (
                    str(self.first_execution_id)
                    if self.first_execution_id is not None
                    else None
                ),
                "retired_handoff_transition_id": str(self.handoff_transition_id),
            }
        )
        self.status_changed_at = now
        self.updated_at = now
        self.version += 1

    def _close(
        self,
        *,
        status: ExecutionRequestStatus,
        now: datetime,
        reason: str,
    ) -> None:
        if self.status not in {
            ExecutionRequestStatus.BLOCKED,
            ExecutionRequestStatus.READY,
        }:
            raise InvalidTransition(f"request cannot close from {self.status}")
        _aware(now, "now")
        clean_reason = _required_text(reason, "reason")
        self.status = status
        self.gate_reason = clean_reason
        self.gate_details = MappingProxyType({"reason": clean_reason})
        self.status_changed_at = now
        self.updated_at = now
        self.version += 1

    def _record_gate(
        self,
        *,
        decision: ActivationDecision | None,
        now: datetime,
    ) -> None:
        self.gate_reason = decision.reason if decision is not None else "ready"
        self.gate_details = (
            decision.details if decision is not None else MappingProxyType({})
        )
        self.gate_evaluated_at = now
        self.status_changed_at = now
        self.updated_at = now
        self.version += 1


def _fact_map(
    facts: Mapping[UUID, UpstreamActivationFact] | Iterable[UpstreamActivationFact],
) -> dict[UUID, UpstreamActivationFact]:
    values = facts.values() if isinstance(facts, Mapping) else facts
    result: dict[UUID, UpstreamActivationFact] = {}
    for fact in values:
        if fact.request_id in result:
            raise ValidationError(
                f"duplicate upstream activation fact: {fact.request_id}"
            )
        result[fact.request_id] = fact
    return result


def _materialized_handoff_target(
    *,
    source: ExecutionRequest,
    request_id: UUID,
    execution_lineage_id: UUID,
    first_execution_id: UUID,
    source_execution_id: UUID,
    source_session_id: UUID,
    handoff_transition_id: UUID,
    now: datetime,
) -> ExecutionRequest:
    target = ExecutionRequest.create(
        request_id=request_id,
        workspace_id=source.workspace_id,
        planning_request_id=source.planning_request_id,
        proposal_revision_id=source.proposal_revision_id,
        plan_snapshot_id=source.plan_snapshot_id,
        plan_generation=source.plan_generation,
        root_task_id=source.root_task_id,
        subtask_id=source.subtask_id,
        worker_item_id=source.worker_item_id,
        worker_item_key=source.worker_item_key,
        intent_key=source.intent_key,
        purpose="handoff",
        capability_requirements=source.capability_requirements,
        adapter_preferences=source.adapter_preferences,
        adapter_forbidden=source.adapter_forbidden,
        budget_class=source.budget_class,
        not_before=source.not_before,
        max_attempts=source.max_attempts,
        expected_artifact_refs=source.expected_artifact_refs,
        expected_evidence_refs=source.expected_evidence_refs,
        expected_outputs=source.expected_outputs,
        now=now,
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
        handoff_transition_id=handoff_transition_id,
        collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
    )
    target.mark_ready(now=now)
    target.materialize(
        execution_lineage_id=execution_lineage_id,
        first_execution_id=first_execution_id,
        now=now,
    )
    return target


def _dependency_enforcement(
    value: DependencyEnforcement | str,
) -> DependencyEnforcement:
    try:
        return (
            value
            if isinstance(value, DependencyEnforcement)
            else DependencyEnforcement(value)
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported dependency enforcement: {value!r}") from exc


def _collaboration_gate_mode(
    value: CollaborationGateMode | str,
) -> CollaborationGateMode:
    try:
        return (
            value
            if isinstance(value, CollaborationGateMode)
            else CollaborationGateMode(value)
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"unsupported collaboration gate mode: {value!r}"
        ) from exc


def collaboration_gate_mode_for_policy(
    policy: CollaborationPolicySnapshotV1 | None,
    *,
    subtask_key: str,
) -> CollaborationGateMode:
    """Resolve the request-level gate mode from the immutable plan policy."""
    if policy is not None and policy.effective_policy(subtask_key).enabled:
        return CollaborationGateMode.RELEASE_REQUIRED
    return CollaborationGateMode.UNMANAGED


def _execution_status(value: ExecutionStatus | str) -> ExecutionStatus:
    try:
        return value if isinstance(value, ExecutionStatus) else ExecutionStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported execution status: {value!r}") from exc


def _evidence_status(value: EvidenceClaimStatus | str) -> EvidenceClaimStatus:
    try:
        return (
            value
            if isinstance(value, EvidenceClaimStatus)
            else EvidenceClaimStatus(value)
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "R5.1 activation only supports candidate evidence claims"
        ) from exc


def _artifact_refs(values: Iterable[str], label: str) -> tuple[str, ...]:
    refs = _clean_strings(values, label)
    for ref in refs:
        match = re.fullmatch(r"artifact_type:([a-z_]+)", ref)
        if match is None:
            raise ValidationError(f"{label} must use artifact_type:<type>")
        try:
            ArtifactType(match.group(1))
        except ValueError as exc:
            raise ValidationError(
                f"unsupported artifact type in {label}: {ref}"
            ) from exc
    return refs


def _evidence_refs(values: Iterable[str], label: str) -> tuple[str, ...]:
    refs = _clean_strings(values, label)
    for ref in refs:
        if re.fullmatch(r"acceptance:\S+", ref) is None:
            raise ValidationError(f"{label} must use acceptance:<criterion-ref>")
    return refs


def _clean_strings(values: Iterable[str], label: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValidationError(f"{label} must contain strings")
        item = value.strip()
        if item and item not in cleaned:
            cleaned.append(item)
    return tuple(cleaned)


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    return value.strip()


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise ValidationError(f"{name} must be timezone-aware")


def _append_unique[ValueT](values: list[ValueT], value: ValueT) -> None:
    if value not in values:
        values.append(value)
