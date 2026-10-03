import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    StaleFencingToken,
    ValidationError,
)
from sidera_control_plane.domain.modules import PlannerAdapterCapabilityProfile
from sidera_control_plane.domain.plan_revisions import RevisionBaseDescriptorV1
from sidera_control_plane.domain.planning_profiles import PlanningProfileSnapshot


class PlanningRequestKind(StrEnum):
    CREATE = "create"
    REVISION = "revision"


class PlanningRequestStatus(StrEnum):
    SUBMITTED = "submitted"
    PLANNING = "planning"
    WAITING_FOR_USER = "waiting_for_user"
    PROPOSAL_READY = "proposal_ready"
    MATERIALIZED = "materialized"
    FAILED = "failed"
    CONFLICTED = "conflicted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class PlanningRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_CLARIFICATION = "needs_clarification"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TaskGoalSubmission:
    workspace_id: UUID
    root_task_id: UUID
    planning_request_id: UUID
    root_task_version: int
    principal_id: str
    grant_id: UUID

    def __post_init__(self) -> None:
        if (
            type(self.root_task_version) is not int
            or self.root_task_version < 1
            or not self.principal_id
        ):
            raise ValidationError(
                "submitted task requires an exact version and trusted principal"
            )


class PlanningRunLeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


RUN_TERMINAL_STATUSES = {
    PlanningRunStatus.SUCCEEDED,
    PlanningRunStatus.FAILED,
    PlanningRunStatus.BLOCKED,
    PlanningRunStatus.CANCELLED,
}

MAX_ERROR_MESSAGE_LENGTH = 2048


@dataclass(frozen=True, slots=True)
class PlanningUsage:
    input_tokens: int | None
    output_tokens: int | None
    cost_microunits: int | None
    runtime_seconds: int | None
    context_bytes: int | None

    @classmethod
    def unknown(
        cls, *, runtime_seconds: int | None = None, context_bytes: int | None = None
    ) -> "PlanningUsage":
        return cls(None, None, None, runtime_seconds, context_bytes)

    @classmethod
    def zero(cls) -> "PlanningUsage":
        return cls(
            input_tokens=0,
            output_tokens=0,
            cost_microunits=0,
            runtime_seconds=0,
            context_bytes=0,
        )

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cost_microunits",
            "runtime_seconds",
            "context_bytes",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValidationError(f"{name} cannot be negative")


@dataclass(slots=True)
class PlanningRunLease:
    id: UUID
    workspace_id: UUID
    planning_run_id: UUID
    owner_id: str
    status: PlanningRunLeaseStatus
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None
    version: int

    def __post_init__(self) -> None:
        for name in ("id", "workspace_id", "planning_run_id"):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"planning lease {name} must be a UUID")
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValidationError("planning lease owner_id is required")
        try:
            self.status = (
                self.status
                if isinstance(self.status, PlanningRunLeaseStatus)
                else PlanningRunLeaseStatus(self.status)
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"unsupported planning lease status: {self.status!r}"
            ) from exc
        if self.fencing_token < 1:
            raise ValidationError("planning lease fencing_token must be positive")
        if self.version < 1:
            raise ValidationError("planning lease version must be positive")
        _aware(self.acquired_at, "planning lease acquired_at")
        _aware(self.expires_at, "planning lease expires_at")
        if self.released_at is not None:
            _aware(self.released_at, "planning lease released_at")


@dataclass(frozen=True, slots=True)
class ProposalMaterialization:
    id: UUID
    workspace_id: UUID
    planning_request_id: UUID
    proposal_revision_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    subtask_task_mapping: Mapping[str, UUID]
    worker_item_mapping: Mapping[str, UUID]
    execution_request_mapping: Mapping[str, UUID]
    deep_link: str
    confirmed_by: str
    created_at: datetime
    kind: PlanningRequestKind = PlanningRequestKind.CREATE
    diff_hash: str | None = None
    plan_transition_id: UUID | None = None

    def __post_init__(self) -> None:
        for name in (
            "id",
            "workspace_id",
            "planning_request_id",
            "proposal_revision_id",
            "root_task_id",
            "plan_snapshot_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"materialization {name} must be a UUID")
        if self.plan_generation < 1:
            raise ValidationError("materialization plan_generation must be positive")
        for name in (
            "subtask_task_mapping",
            "worker_item_mapping",
            "execution_request_mapping",
        ):
            mapping = getattr(self, name)
            if not isinstance(mapping, Mapping):
                raise ValidationError(f"materialization {name} must be a mapping")
            if any(
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, UUID)
                for key, value in mapping.items()
            ):
                raise ValidationError(f"materialization {name} must map keys to UUIDs")
            object.__setattr__(self, name, MappingProxyType(dict(mapping)))
        if not isinstance(self.deep_link, str) or not self.deep_link.strip():
            raise ValidationError("materialization deep_link is required")
        if not isinstance(self.confirmed_by, str) or not self.confirmed_by.strip():
            raise ValidationError("materialization confirmed_by is required")
        try:
            object.__setattr__(
                self,
                "kind",
                self.kind
                if isinstance(self.kind, PlanningRequestKind)
                else PlanningRequestKind(self.kind),
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"unsupported materialization kind: {self.kind!r}"
            ) from exc
        if self.diff_hash is not None:
            _validate_content_hash(self.diff_hash)
        if self.plan_transition_id is not None and not isinstance(
            self.plan_transition_id, UUID
        ):
            raise ValidationError("materialization plan_transition_id must be a UUID")
        _aware(self.created_at, "materialization created_at")


@dataclass(frozen=True, slots=True)
class PlanningBriefRevision:
    planning_request_id: UUID
    revision: int
    objective: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    context_refs: tuple[str, ...]
    requested_outputs: tuple[str, ...]
    user_feedback: str | None
    source_correlation: UUID
    created_by: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        planning_request_id: UUID,
        revision: int,
        objective: str,
        constraints: Sequence[str],
        acceptance_criteria: Sequence[str],
        context_refs: Sequence[str],
        requested_outputs: Sequence[str],
        user_feedback: str | None,
        source_correlation: UUID,
        created_by: str,
        created_at: datetime,
    ) -> "PlanningBriefRevision":
        return cls(
            planning_request_id=planning_request_id,
            revision=revision,
            objective=objective,
            constraints=_text_tuple(constraints, "constraints"),
            acceptance_criteria=_text_tuple(
                acceptance_criteria, "acceptance_criteria", allow_empty=False
            ),
            context_refs=_text_tuple(context_refs, "context_refs"),
            requested_outputs=_text_tuple(
                requested_outputs, "requested_outputs", allow_empty=False
            ),
            user_feedback=_optional_text(user_feedback, "user_feedback"),
            source_correlation=source_correlation,
            created_by=_required_text(created_by, "created_by"),
            created_at=_aware(created_at, "created_at"),
        )

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValidationError("brief revision must be positive")
        _required_text(self.objective, "objective")
        if not self.acceptance_criteria or not self.requested_outputs:
            raise ValidationError(
                "acceptance_criteria and requested_outputs cannot be empty"
            )
        _aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class PlanningClarification:
    id: UUID
    planning_request_id: UUID
    planning_run_id: UUID
    question: str
    rationale: str
    answer_type: str
    options: tuple[str, ...]
    blocking: bool
    answer: str | None
    answered_by: str | None
    answered_at: datetime | None
    answer_brief_revision: int | None
    created_at: datetime
    version: int

    @classmethod
    def create(
        cls,
        *,
        clarification_id: UUID,
        planning_request_id: UUID,
        planning_run_id: UUID,
        question: str,
        rationale: str,
        answer_type: str,
        options: Sequence[str],
        blocking: bool,
        created_at: datetime,
    ) -> "PlanningClarification":
        return cls(
            id=clarification_id,
            planning_request_id=planning_request_id,
            planning_run_id=planning_run_id,
            question=_required_text(question, "clarification question"),
            rationale=_required_text(rationale, "clarification rationale"),
            answer_type=_required_text(answer_type, "clarification answer_type"),
            options=_text_tuple(options, "clarification options"),
            blocking=blocking,
            answer=None,
            answered_by=None,
            answered_at=None,
            answer_brief_revision=None,
            created_at=_aware(created_at, "clarification created_at"),
            version=1,
        )

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValidationError("clarification version must be positive")
        if not isinstance(self.blocking, bool):
            raise ValidationError("clarification blocking must be a boolean")
        if self.answered_at is None and any(
            value is not None
            for value in (self.answer, self.answered_by, self.answer_brief_revision)
        ):
            raise ValidationError(
                "unanswered clarification cannot contain answer facts"
            )
        if self.answered_at is not None:
            _aware(self.answered_at, "clarification answered_at")
            if (
                not self.answer
                or not self.answered_by
                or self.answer_brief_revision is None
            ):
                raise ValidationError("answered clarification facts are incomplete")
            if self.answer_brief_revision < 1:
                raise ValidationError("answer_brief_revision must be positive")
        if self.answer_type == "choice" and not self.options:
            raise ValidationError("choice clarification requires options")

    def answer_question(
        self,
        *,
        answer: str,
        actor: str,
        brief_revision: int,
        answered_at: datetime,
    ) -> "PlanningClarification":
        if self.answer is not None:
            raise InvalidTransition("clarification has already been answered")
        clean_answer = _required_text(answer, "clarification answer")
        if self.options and clean_answer not in self.options:
            raise ValidationError("clarification answer is not one of the options")
        if brief_revision < 1:
            raise ValidationError("answer_brief_revision must be positive")
        return PlanningClarification(
            id=self.id,
            planning_request_id=self.planning_request_id,
            planning_run_id=self.planning_run_id,
            question=self.question,
            rationale=self.rationale,
            answer_type=self.answer_type,
            options=self.options,
            blocking=self.blocking,
            answer=clean_answer,
            answered_by=_required_text(actor, "clarification actor"),
            answered_at=_aware(answered_at, "clarification answered_at"),
            answer_brief_revision=brief_revision,
            created_at=self.created_at,
            version=self.version + 1,
        )


@dataclass(slots=True)
class PlanningRun:
    id: UUID
    planning_request_id: UUID
    workspace_id: UUID
    brief_revision: int
    attempt: int
    operation_id: UUID
    profile_snapshot: PlanningProfileSnapshot
    planner_module_instance_id: UUID | None
    planner_module_config_version: int | None
    capability_snapshot: PlannerAdapterCapabilityProfile | None
    status: PlanningRunStatus
    lease_id: UUID | None
    fencing_token: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    provider_ref: str | None
    structured_result_type: str | None
    usage: PlanningUsage
    error_code: str | None
    error_message: str | None
    diagnostic_artifact_ref: str | None
    version: int

    def __post_init__(self) -> None:
        if (
            self.planner_module_instance_id
            != self.profile_snapshot.planner_module_instance_id
            or self.planner_module_config_version
            != self.profile_snapshot.planner_module_config_version
            or self.capability_snapshot != self.profile_snapshot.planner_capabilities
        ):
            raise ValidationError("planning run must retain its exact policy snapshot")

    @classmethod
    def create(
        cls,
        *,
        run_id: UUID,
        planning_request_id: UUID,
        workspace_id: UUID,
        brief_revision: int,
        attempt: int,
        profile_snapshot: PlanningProfileSnapshot,
        queued_at: datetime,
    ) -> "PlanningRun":
        if brief_revision < 1 or attempt < 1:
            raise ValidationError("brief revision and attempt must be positive")
        if profile_snapshot.workspace_id != workspace_id:
            raise ValidationError(
                "planning profile snapshot belongs to another workspace"
            )
        return cls(
            id=run_id,
            planning_request_id=planning_request_id,
            workspace_id=workspace_id,
            brief_revision=brief_revision,
            attempt=attempt,
            operation_id=planning_operation_id(run_id, attempt),
            profile_snapshot=profile_snapshot,
            planner_module_instance_id=profile_snapshot.planner_module_instance_id,
            planner_module_config_version=profile_snapshot.planner_module_config_version,
            capability_snapshot=profile_snapshot.planner_capabilities,
            status=PlanningRunStatus.QUEUED,
            lease_id=None,
            fencing_token=0,
            queued_at=_aware(queued_at, "run queued_at"),
            started_at=None,
            finished_at=None,
            provider_ref=None,
            structured_result_type=None,
            usage=(
                PlanningUsage.unknown()
                if profile_snapshot.execution_mode == "native_profile"
                else PlanningUsage.zero()
            ),
            error_code=None,
            error_message=None,
            diagnostic_artifact_ref=None,
            version=1,
        )

    def start(self, *, lease_id: UUID, fencing_token: int, now: datetime) -> None:
        if self.status is not PlanningRunStatus.QUEUED:
            raise InvalidTransition("only a queued planning run can start")
        if fencing_token < 1:
            raise ValidationError("planning fencing_token must be positive")
        self.lease_id = lease_id
        self.fencing_token = fencing_token
        self.started_at = _aware(now, "run started_at")
        self.status = PlanningRunStatus.RUNNING
        self.version += 1

    def block_before_start(
        self,
        *,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> None:
        if self.status is not PlanningRunStatus.QUEUED:
            raise InvalidTransition("only a queued planning run can block before start")
        self._record_error(
            error_code=error_code,
            error_message=error_message,
            diagnostic_artifact_ref=None,
        )
        self._finish(PlanningRunStatus.BLOCKED, now=now)

    def succeed(
        self,
        *,
        lease_id: UUID,
        fencing_token: int,
        provider_ref: str,
        structured_result_type: str,
        usage: PlanningUsage,
        now: datetime,
        diagnostic_artifact_ref: str | None = None,
    ) -> None:
        self._assert_running_fence(lease_id=lease_id, fencing_token=fencing_token)
        self.provider_ref = _required_text(provider_ref, "provider_ref")
        self.structured_result_type = _required_text(
            structured_result_type, "structured_result_type"
        )
        self.usage = usage
        self.diagnostic_artifact_ref = _optional_text(
            diagnostic_artifact_ref, "diagnostic_artifact_ref"
        )
        self._finish(PlanningRunStatus.SUCCEEDED, now=now)

    def needs_clarification(
        self, *, lease_id: UUID, fencing_token: int, now: datetime
    ) -> None:
        self._assert_running_fence(lease_id=lease_id, fencing_token=fencing_token)
        self._finish(PlanningRunStatus.NEEDS_CLARIFICATION, now=now)

    def fail(
        self,
        *,
        lease_id: UUID,
        fencing_token: int,
        error_code: str,
        error_message: str,
        now: datetime,
        diagnostic_artifact_ref: str | None = None,
    ) -> None:
        self._assert_running_fence(lease_id=lease_id, fencing_token=fencing_token)
        self._record_error(
            error_code=error_code,
            error_message=error_message,
            diagnostic_artifact_ref=diagnostic_artifact_ref,
        )
        self._finish(PlanningRunStatus.FAILED, now=now)

    def block_unknown(
        self,
        *,
        lease_id: UUID,
        fencing_token: int,
        error_code: str,
        error_message: str,
        now: datetime,
        diagnostic_artifact_ref: str | None = None,
    ) -> None:
        self._assert_running_fence(lease_id=lease_id, fencing_token=fencing_token)
        self._record_error(
            error_code=error_code,
            error_message=error_message,
            diagnostic_artifact_ref=diagnostic_artifact_ref,
        )
        self._finish(PlanningRunStatus.BLOCKED, now=now)

    def cancel(self, *, now: datetime) -> None:
        if self.status in RUN_TERMINAL_STATUSES:
            raise InvalidTransition("planning run is already terminal")
        self.status = PlanningRunStatus.CANCELLED
        self.finished_at = _aware(now, "run finished_at")
        self.version += 1

    def retry_after_context_block(self, *, now: datetime) -> None:
        if self.status is not PlanningRunStatus.BLOCKED:
            raise InvalidTransition(
                "only a blocked planning run can retry context assembly"
            )
        if not (self.error_code or "").startswith("context_"):
            raise InvalidTransition("planning run is not blocked by context assembly")
        self.status = PlanningRunStatus.QUEUED
        self.finished_at = None
        self.error_code = None
        self.error_message = None
        self.diagnostic_artifact_ref = None
        self.lease_id = None
        self.fencing_token = 0
        self.queued_at = _aware(now, "run queued_at")
        self.version += 1

    def _assert_running_fence(self, *, lease_id: UUID, fencing_token: int) -> None:
        if self.status is not PlanningRunStatus.RUNNING:
            raise InvalidTransition("only a running planning run can finish")
        if self.lease_id != lease_id or self.fencing_token != fencing_token:
            raise StaleFencingToken("planning run is owned by another lease or fence")

    def _record_error(
        self,
        *,
        error_code: str,
        error_message: str,
        diagnostic_artifact_ref: str | None,
    ) -> None:
        self.error_code = _required_text(error_code, "error_code")
        self.error_message = _safe_error_message(error_message)
        self.diagnostic_artifact_ref = _optional_text(
            diagnostic_artifact_ref, "diagnostic_artifact_ref"
        )

    def _finish(self, status: PlanningRunStatus, *, now: datetime) -> None:
        self.status = status
        self.finished_at = _aware(now, "run finished_at")
        self.version += 1


@dataclass(slots=True)
class PlanningRequest:
    id: UUID
    workspace_id: UUID
    kind: PlanningRequestKind
    status: PlanningRequestStatus
    current_brief_revision: int
    current_proposal_revision: int | None
    current_proposal_content_hash: str | None
    planning_profile_id: UUID
    planning_profile_version: int
    profile_snapshot: PlanningProfileSnapshot
    source_type: str
    source_ref: str | None
    actor: str
    correlation_id: UUID
    briefs: tuple[PlanningBriefRevision, ...]
    runs: tuple[PlanningRun, ...]
    clarifications: tuple[PlanningClarification, ...]
    version: int
    created_at: datetime
    updated_at: datetime
    root_task_id: UUID | None = None
    base_plan_snapshot_id: UUID | None = None
    base_plan_generation: int | None = None
    base_plan_content_hash: str | None = None
    base_root_task_version: int | None = None
    base_captured_at: datetime | None = None
    conflict_reason: str | None = None
    _base_initialized: bool = False

    def __post_init__(self) -> None:
        base_values = (
            self.root_task_id,
            self.base_plan_snapshot_id,
            self.base_plan_generation,
            self.base_plan_content_hash,
            self.base_root_task_version,
            self.base_captured_at,
        )
        if self.kind is PlanningRequestKind.CREATE:
            if any(value is not None for value in base_values):
                raise ValidationError("create request cannot contain a revision base")
        else:
            if any(value is None for value in base_values):
                raise ValidationError("revision request requires an exact base")
            if self.base_descriptor is None:
                raise ValidationError("revision request requires an exact base")
        object.__setattr__(self, "_base_initialized", True)

    def __setattr__(self, name: str, value: object) -> None:
        if name in {
            "root_task_id",
            "base_plan_snapshot_id",
            "base_plan_generation",
            "base_plan_content_hash",
            "base_root_task_version",
            "base_captured_at",
        }:
            try:
                initialized = object.__getattribute__(self, "_base_initialized")
            except AttributeError:
                initialized = False
            if initialized:
                current = object.__getattribute__(self, name)
                if current != value:
                    raise InvalidTransition("revision base is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def create(
        cls,
        *,
        request_id: UUID,
        workspace_id: UUID,
        profile_snapshot: PlanningProfileSnapshot,
        objective: str,
        constraints: Sequence[str],
        acceptance_criteria: Sequence[str],
        context_refs: Sequence[str],
        requested_outputs: Sequence[str],
        source_type: str,
        source_ref: str | None,
        actor: str,
        correlation_id: UUID,
        run_id: UUID,
        now: datetime,
        kind: PlanningRequestKind = PlanningRequestKind.CREATE,
    ) -> "PlanningRequest":
        if kind is not PlanningRequestKind.CREATE:
            raise ValidationError("R5.1 only accepts planning request kind=create")
        return cls._create(
            request_id=request_id,
            workspace_id=workspace_id,
            profile_snapshot=profile_snapshot,
            objective=objective,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            context_refs=context_refs,
            requested_outputs=requested_outputs,
            source_type=source_type,
            source_ref=source_ref,
            actor=actor,
            correlation_id=correlation_id,
            run_id=run_id,
            now=now,
            kind=PlanningRequestKind.CREATE,
            base=None,
        )

    @classmethod
    def create_revision(
        cls,
        *,
        request_id: UUID,
        workspace_id: UUID,
        profile_snapshot: PlanningProfileSnapshot,
        objective: str,
        constraints: Sequence[str],
        acceptance_criteria: Sequence[str],
        context_refs: Sequence[str],
        requested_outputs: Sequence[str],
        source_type: str,
        source_ref: str | None,
        actor: str,
        correlation_id: UUID,
        run_id: UUID,
        now: datetime,
        base: RevisionBaseDescriptorV1 | None,
    ) -> "PlanningRequest":
        if base is None:
            raise ValidationError("revision request requires an exact base")
        if not isinstance(base, RevisionBaseDescriptorV1):
            raise ValidationError("revision request requires an exact base descriptor")
        if base.workspace_id != workspace_id:
            raise ValidationError("revision base belongs to another workspace")
        return cls._create(
            request_id=request_id,
            workspace_id=workspace_id,
            profile_snapshot=profile_snapshot,
            objective=objective,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            context_refs=context_refs,
            requested_outputs=requested_outputs,
            source_type=source_type,
            source_ref=source_ref,
            actor=actor,
            correlation_id=correlation_id,
            run_id=run_id,
            now=now,
            kind=PlanningRequestKind.REVISION,
            base=base,
        )

    @classmethod
    def _create(
        cls,
        *,
        request_id: UUID,
        workspace_id: UUID,
        profile_snapshot: PlanningProfileSnapshot,
        objective: str,
        constraints: Sequence[str],
        acceptance_criteria: Sequence[str],
        context_refs: Sequence[str],
        requested_outputs: Sequence[str],
        source_type: str,
        source_ref: str | None,
        actor: str,
        correlation_id: UUID,
        run_id: UUID,
        now: datetime,
        kind: PlanningRequestKind,
        base: RevisionBaseDescriptorV1 | None,
    ) -> "PlanningRequest":
        if profile_snapshot.workspace_id != workspace_id:
            raise ValidationError(
                "planning profile snapshot belongs to another workspace"
            )
        timestamp = _aware(now, "planning request timestamp")
        brief = PlanningBriefRevision.create(
            planning_request_id=request_id,
            revision=1,
            objective=objective,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            context_refs=context_refs,
            requested_outputs=requested_outputs,
            user_feedback=None,
            source_correlation=correlation_id,
            created_by=actor,
            created_at=timestamp,
        )
        run = PlanningRun.create(
            run_id=run_id,
            planning_request_id=request_id,
            workspace_id=workspace_id,
            brief_revision=brief.revision,
            attempt=1,
            profile_snapshot=profile_snapshot,
            queued_at=timestamp,
        )
        return cls(
            id=request_id,
            workspace_id=workspace_id,
            kind=kind,
            status=PlanningRequestStatus.SUBMITTED,
            current_brief_revision=brief.revision,
            current_proposal_revision=None,
            current_proposal_content_hash=None,
            planning_profile_id=profile_snapshot.profile_id,
            planning_profile_version=profile_snapshot.profile_version,
            profile_snapshot=profile_snapshot,
            source_type=_required_text(source_type, "source_type"),
            source_ref=_optional_text(source_ref, "source_ref"),
            actor=_required_text(actor, "actor"),
            correlation_id=correlation_id,
            briefs=(brief,),
            runs=(run,),
            clarifications=(),
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
            root_task_id=base.root_task_id if base else None,
            base_plan_snapshot_id=base.plan_snapshot_id if base else None,
            base_plan_generation=base.plan_generation if base else None,
            base_plan_content_hash=base.plan_content_hash if base else None,
            base_root_task_version=base.root_task_version if base else None,
            base_captured_at=base.captured_at if base else None,
        )

    @property
    def base_descriptor(self) -> RevisionBaseDescriptorV1 | None:
        if self.kind is PlanningRequestKind.CREATE:
            return None
        values = (
            self.root_task_id,
            self.base_plan_snapshot_id,
            self.base_plan_generation,
            self.base_plan_content_hash,
            self.base_root_task_version,
            self.base_captured_at,
        )
        if any(value is None for value in values):
            raise ValidationError("revision request requires an exact base")
        return RevisionBaseDescriptorV1(
            schema_version=1,
            workspace_id=self.workspace_id,
            root_task_id=cast(UUID, self.root_task_id),
            plan_snapshot_id=cast(UUID, self.base_plan_snapshot_id),
            plan_generation=cast(int, self.base_plan_generation),
            plan_content_hash=cast(str, self.base_plan_content_hash),
            root_task_version=cast(int, self.base_root_task_version),
            captured_at=cast(datetime, self.base_captured_at),
        )

    def replace_revision_base(self, base: RevisionBaseDescriptorV1) -> None:
        del base
        raise InvalidTransition("revision base is immutable")

    @property
    def current_run_id(self) -> UUID:
        if not self.runs:
            raise ValidationError("planning request has no planning run")
        return self.runs[-1].id

    def run(self, run_id: UUID) -> PlanningRun:
        for item in self.runs:
            if item.id == run_id:
                return item
        raise ValidationError("planning run does not belong to request")

    def start_planning(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        now: datetime,
    ) -> None:
        if self.status in {
            PlanningRequestStatus.MATERIALIZED,
            PlanningRequestStatus.REJECTED,
            PlanningRequestStatus.CANCELLED,
        }:
            raise InvalidTransition("terminal planning request cannot start")
        run = self.run(run_id)
        if run.brief_revision != self.current_brief_revision:
            raise ValidationError("planning run is not for the current brief revision")
        run.start(lease_id=lease_id, fencing_token=fencing_token, now=now)
        self.status = PlanningRequestStatus.PLANNING
        self._touch(now)

    def succeed_run(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        provider_ref: str,
        structured_result_type: str,
        usage: PlanningUsage,
        now: datetime,
        diagnostic_artifact_ref: str | None = None,
    ) -> None:
        if self.status is not PlanningRequestStatus.PLANNING:
            raise InvalidTransition("only a planning request in planning can succeed")
        run = self.run(run_id)
        run.succeed(
            lease_id=lease_id,
            fencing_token=fencing_token,
            provider_ref=provider_ref,
            structured_result_type=structured_result_type,
            usage=usage,
            now=now,
            diagnostic_artifact_ref=diagnostic_artifact_ref,
        )
        self._touch(now)

    def mark_needs_clarification(
        self,
        *,
        run_id: UUID,
        clarifications: Sequence[PlanningClarification],
        lease_id: UUID,
        fencing_token: int,
        now: datetime,
    ) -> None:
        if self.status is not PlanningRequestStatus.PLANNING:
            raise InvalidTransition("only a planning request in planning can wait")
        items = tuple(clarifications)
        if not items or not any(
            item.blocking and item.answer is None for item in items
        ):
            raise ValidationError("waiting_for_user requires a blocking clarification")
        for item in items:
            if item.planning_request_id != self.id or item.planning_run_id != run_id:
                raise ValidationError("clarification belongs to another planning run")
        run = self.run(run_id)
        run.needs_clarification(lease_id=lease_id, fencing_token=fencing_token, now=now)
        self.clarifications = (*self.clarifications, *items)
        self.status = PlanningRequestStatus.WAITING_FOR_USER
        self._touch(now)

    def answer_clarifications(
        self,
        *,
        run_id: UUID,
        answers: Mapping[UUID, str],
        brief: PlanningBriefRevision,
        new_run_id: UUID,
        actor: str,
        now: datetime,
    ) -> None:
        if self.status is not PlanningRequestStatus.WAITING_FOR_USER:
            raise InvalidTransition("planning request is not waiting for user")
        run = self.run(run_id)
        if run.status is not PlanningRunStatus.NEEDS_CLARIFICATION:
            raise InvalidTransition("planning run is not waiting for clarification")
        pending = {
            item.id
            for item in self.clarifications
            if item.planning_run_id == run_id and item.blocking and item.answer is None
        }
        if set(answers) != pending:
            raise ValidationError(
                "all blocking clarifications must be answered together"
            )
        self._validate_new_brief(brief, actor)
        updated: list[PlanningClarification] = []
        for item in self.clarifications:
            answer = answers.get(item.id)
            if answer is not None:
                updated.append(
                    item.answer_question(
                        answer=answer,
                        actor=actor,
                        brief_revision=brief.revision,
                        answered_at=now,
                    )
                )
            else:
                updated.append(item)
        self.clarifications = tuple(updated)
        self._append_brief_and_run(
            brief=brief, run_id=new_run_id, now=now, attempt=run.attempt + 1
        )

    def mark_proposal_ready(
        self,
        *,
        run_id: UUID,
        proposal_revision: int,
        content_hash: str,
        now: datetime,
    ) -> None:
        if self.status is not PlanningRequestStatus.PLANNING:
            raise InvalidTransition("planning request is not planning")
        run = self.run(run_id)
        if run.status is not PlanningRunStatus.SUCCEEDED:
            raise InvalidTransition("proposal requires a succeeded planning run")
        if proposal_revision < 1:
            raise ValidationError("proposal revision must be positive")
        _validate_content_hash(content_hash)
        self.current_proposal_revision = proposal_revision
        self.current_proposal_content_hash = content_hash
        self.status = PlanningRequestStatus.PROPOSAL_READY
        self._touch(now)

    def materialize(
        self, *, proposal_revision: int, content_hash: str, now: datetime
    ) -> None:
        if self.status is not PlanningRequestStatus.PROPOSAL_READY:
            raise InvalidTransition("only a proposal-ready request can materialize")
        self._assert_current_proposal(proposal_revision, content_hash)
        self.status = PlanningRequestStatus.MATERIALIZED
        self._touch(now)

    def request_revision(
        self,
        *,
        expected_proposal_revision: int,
        expected_content_hash: str,
        feedback: str,
        brief: PlanningBriefRevision,
        new_run_id: UUID,
        actor: str,
        now: datetime,
    ) -> None:
        if self.status is not PlanningRequestStatus.PROPOSAL_READY:
            raise InvalidTransition("only a proposal-ready request can be revised")
        self._assert_current_proposal(expected_proposal_revision, expected_content_hash)
        clean_feedback = _required_text(feedback, "revision feedback")
        if brief.user_feedback != clean_feedback:
            raise ValidationError("brief user_feedback must match revision feedback")
        self._validate_new_brief(brief, actor)
        self._append_brief_and_run(
            brief=brief,
            run_id=new_run_id,
            now=now,
            attempt=self.runs[-1].attempt + 1,
        )
        self.current_proposal_revision = None
        self.current_proposal_content_hash = None

    def fail_run(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        error_code: str,
        error_message: str,
        now: datetime,
        diagnostic_artifact_ref: str | None = None,
    ) -> None:
        if self.status is not PlanningRequestStatus.PLANNING:
            raise InvalidTransition("only a planning request in planning can fail")
        run = self.run(run_id)
        run.fail(
            lease_id=lease_id,
            fencing_token=fencing_token,
            error_code=error_code,
            error_message=error_message,
            now=now,
            diagnostic_artifact_ref=diagnostic_artifact_ref,
        )
        self.status = PlanningRequestStatus.FAILED
        self._touch(now)

    def block_run_unknown(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        error_code: str,
        error_message: str,
        now: datetime,
        diagnostic_artifact_ref: str | None = None,
    ) -> None:
        if self.status is not PlanningRequestStatus.PLANNING:
            raise InvalidTransition("only a planning request in planning can block")
        run = self.run(run_id)
        run.block_unknown(
            lease_id=lease_id,
            fencing_token=fencing_token,
            error_code=error_code,
            error_message=error_message,
            now=now,
            diagnostic_artifact_ref=diagnostic_artifact_ref,
        )
        self.status = PlanningRequestStatus.FAILED
        self._touch(now)

    def block_queued_run(
        self,
        *,
        run_id: UUID,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> None:
        if self.status in {
            PlanningRequestStatus.MATERIALIZED,
            PlanningRequestStatus.REJECTED,
            PlanningRequestStatus.CANCELLED,
        }:
            raise InvalidTransition("terminal planning request cannot block")
        run = self.run(run_id)
        run.block_before_start(
            error_code=error_code,
            error_message=error_message,
            now=now,
        )
        self.status = PlanningRequestStatus.FAILED
        self._touch(now)

    def retry_blocked(self, *, new_run_id: UUID, now: datetime) -> None:
        self._retry_run(
            expected_status=PlanningRunStatus.BLOCKED,
            new_run_id=new_run_id,
            now=now,
        )

    def retry_failed(self, *, new_run_id: UUID, now: datetime) -> None:
        self._retry_run(
            expected_status=PlanningRunStatus.FAILED,
            new_run_id=new_run_id,
            now=now,
        )

    def retry_context_assembly(self, run_id: UUID, *, now: datetime) -> None:
        if self.status is not PlanningRequestStatus.FAILED:
            raise InvalidTransition(
                "only a failed planning request can retry context assembly"
            )
        run = self.run(run_id)
        run.retry_after_context_block(now=now)
        self.status = PlanningRequestStatus.PLANNING
        self._touch(now)

    def mark_conflicted(self, *, now: datetime, reason: str = "stale_base") -> None:
        if self.status is not PlanningRequestStatus.PROPOSAL_READY:
            raise InvalidTransition("only a proposal-ready request can conflict")
        clean_reason = _required_text(reason, "conflict reason")
        self.status = PlanningRequestStatus.CONFLICTED
        self.conflict_reason = clean_reason
        self._touch(now)

    def reject(self, *, now: datetime) -> None:
        self._assert_request_mutable()
        self._cancel_active_runs(now)
        self.status = PlanningRequestStatus.REJECTED
        self._touch(now)

    def cancel(self, *, now: datetime) -> None:
        self._assert_request_mutable()
        self._cancel_active_runs(now)
        self.status = PlanningRequestStatus.CANCELLED
        self._touch(now)

    def _retry_run(
        self,
        *,
        expected_status: PlanningRunStatus,
        new_run_id: UUID,
        now: datetime,
    ) -> None:
        if self.status is not PlanningRequestStatus.FAILED:
            raise InvalidTransition("only a failed planning request can be retried")
        previous = self.runs[-1]
        if previous.status is not expected_status:
            raise InvalidTransition(f"latest planning run is not {expected_status}")
        self._append_brief_and_run(
            brief=self.briefs[-1],
            run_id=new_run_id,
            now=now,
            attempt=previous.attempt + 1,
        )

    def _append_brief_and_run(
        self,
        *,
        brief: PlanningBriefRevision,
        run_id: UUID,
        now: datetime,
        attempt: int,
    ) -> None:
        run = PlanningRun.create(
            run_id=run_id,
            planning_request_id=self.id,
            workspace_id=self.workspace_id,
            brief_revision=brief.revision,
            attempt=attempt,
            profile_snapshot=self.profile_snapshot,
            queued_at=now,
        )
        self.briefs = (*self.briefs, brief)
        self.runs = (*self.runs, run)
        self.current_brief_revision = brief.revision
        self.status = PlanningRequestStatus.PLANNING
        self._touch(now)

    def _validate_new_brief(self, brief: PlanningBriefRevision, actor: str) -> None:
        if brief.planning_request_id != self.id:
            raise ValidationError("brief belongs to another planning request")
        if brief.revision != self.current_brief_revision + 1:
            raise ValidationError("brief revision must increment by one")
        if brief.created_by != _required_text(actor, "actor"):
            raise ValidationError("brief actor does not match command actor")

    def _assert_current_proposal(self, revision: int, content_hash: str) -> None:
        if self.current_proposal_revision != revision:
            raise ValidationError("proposal revision does not match current proposal")
        _validate_content_hash(content_hash)
        if self.current_proposal_content_hash != content_hash:
            raise ValidationError(
                "proposal content hash does not match current proposal"
            )

    def _cancel_active_runs(self, now: datetime) -> None:
        for run in self.runs:
            if run.status not in RUN_TERMINAL_STATUSES:
                run.cancel(now=now)

    def _assert_request_mutable(self) -> None:
        if self.status in {
            PlanningRequestStatus.MATERIALIZED,
            PlanningRequestStatus.REJECTED,
            PlanningRequestStatus.CANCELLED,
        }:
            raise InvalidTransition(f"planning request is terminal: {self.status}")

    def _touch(self, now: datetime) -> None:
        self.updated_at = _aware(now, "planning request updated_at")
        self.version += 1


def planning_operation_id(run_id: UUID, attempt: int) -> UUID:
    if attempt < 1:
        raise ValidationError("planning attempt must be positive")
    return uuid5(NAMESPACE_URL, f"sidera.planning.run:{run_id}:attempt:{attempt}")


def _text_tuple(
    values: Sequence[str], name: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValidationError(f"{name} must be a sequence of strings")
    clean: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _required_text(value, name)
        if item not in seen:
            seen.add(item)
            clean.append(item)
    if not allow_empty and not clean:
        raise ValidationError(f"{name} cannot be empty")
    return tuple(clean)


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    return value.strip()


def _optional_text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValidationError(f"{name} must be timezone-aware")
    return value


def _enum_value(value: object, enum_type: type[StrEnum], label: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {label}: {value!r}") from exc


def _safe_error_message(value: str) -> str:
    clean = _required_text(value, "error_message")
    lowered = clean.lower()
    if any(
        marker in lowered
        for marker in ("api_key", "access_token", "secret_value", "password=")
    ):
        raise ValidationError("error message cannot contain secret fields")
    return clean[:MAX_ERROR_MESSAGE_LENGTH]


def _validate_content_hash(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError("content hash must be a lowercase SHA-256 value")
