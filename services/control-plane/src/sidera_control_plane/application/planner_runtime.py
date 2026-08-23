from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningClarification,
    PlanningUsage,
)
from sidera_control_plane.domain.planning_profiles import PlanningProfileSnapshot
from sidera_control_plane.domain.planning_proposals import ProposalRevision


@dataclass(frozen=True, slots=True)
class CapabilityCatalogEntry:
    name: str
    description: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.description.strip():
            raise ValueError("capability catalog entries require name and description")


@dataclass(frozen=True, slots=True)
class WorkspaceProviderCapabilitySnapshot:
    provider: str
    isolation: str
    writable: bool
    git_worktree: bool
    stability: str

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.isolation.strip():
            raise ValueError("workspace provider capability fields are required")
        if not isinstance(self.writable, bool) or not isinstance(self.git_worktree, bool):
            raise TypeError("workspace provider capability flags must be boolean")


@dataclass(frozen=True, slots=True)
class CapabilityCatalogSnapshot:
    snapshot_version: int
    captured_at: datetime
    capabilities: tuple[CapabilityCatalogEntry, ...]
    adapter_preferences: tuple[str, ...]
    budget_level: str
    max_concurrency: int
    workspace_providers: tuple[WorkspaceProviderCapabilitySnapshot, ...]

    def __post_init__(self) -> None:
        if self.snapshot_version < 1:
            raise ValueError("capability catalog snapshot_version must be positive")
        if not self.budget_level.strip() or self.max_concurrency < 1:
            raise ValueError("capability catalog budget fields are invalid")
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "adapter_preferences", tuple(self.adapter_preferences))
        object.__setattr__(self, "workspace_providers", tuple(self.workspace_providers))


@dataclass(frozen=True, slots=True)
class PlannerPolicyConstraints:
    allowed_capabilities: tuple[str, ...]
    adapter_preferences: tuple[str, ...]
    adapter_forbidden: tuple[str, ...]
    max_context_bytes: int
    max_tokens: int
    max_cost_microunits: int
    max_runtime_seconds: int
    max_nodes: int
    max_concurrency: int
    budget_level: str
    execution_max_attempts: int

    @classmethod
    def from_profile_snapshot(
        cls, profile_snapshot: PlanningProfileSnapshot
    ) -> PlannerPolicyConstraints:
        return cls(
            allowed_capabilities=profile_snapshot.allowed_capabilities,
            adapter_preferences=profile_snapshot.adapter_preferences,
            adapter_forbidden=profile_snapshot.adapter_forbidden,
            max_context_bytes=profile_snapshot.budget.max_context_bytes,
            max_tokens=profile_snapshot.budget.max_tokens,
            max_cost_microunits=profile_snapshot.budget.max_cost_microunits,
            max_runtime_seconds=profile_snapshot.budget.max_runtime_seconds,
            max_nodes=profile_snapshot.budget.max_nodes,
            max_concurrency=profile_snapshot.budget.max_concurrency,
            budget_level=profile_snapshot.execution_budget_level,
            execution_max_attempts=profile_snapshot.execution_max_attempts,
        )

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.max_context_bytes,
                self.max_tokens,
                self.max_cost_microunits,
                self.max_runtime_seconds,
                self.max_nodes,
                self.max_concurrency,
                self.execution_max_attempts,
            )
        ):
            raise ValueError("planner policy limits must be positive")
        if not self.budget_level.strip():
            raise ValueError("planner budget_level is required")
        object.__setattr__(self, "allowed_capabilities", tuple(self.allowed_capabilities))
        object.__setattr__(self, "adapter_preferences", tuple(self.adapter_preferences))
        object.__setattr__(self, "adapter_forbidden", tuple(self.adapter_forbidden))


@dataclass(frozen=True, slots=True)
class ContextReference:
    ref: str
    safe_summary: str | None

    def __post_init__(self) -> None:
        if not self.ref.strip():
            raise ValueError("context reference is required")
        if self.safe_summary is not None and len(self.safe_summary) > 1000:
            raise ValueError("context safe_summary is too long")


@dataclass(frozen=True, slots=True)
class PlannerInput:
    interface_version: str
    proposal_schema_version: int
    operation_id: UUID
    planning_request_id: UUID
    planning_run_id: UUID
    brief_revision: int
    brief: PlanningBriefRevision
    profile_snapshot: PlanningProfileSnapshot
    capability_catalog: CapabilityCatalogSnapshot
    policy_constraints: PlannerPolicyConstraints
    context_refs: tuple[ContextReference, ...]

    def __post_init__(self) -> None:
        if not self.interface_version.strip():
            raise ValueError("planner interface_version is required")
        if self.proposal_schema_version < 1 or self.brief_revision < 1:
            raise ValueError("planner schema and brief revisions must be positive")
        if self.brief.revision != self.brief_revision:
            raise ValueError("planner input brief revision does not match brief")
        if self.brief.planning_request_id != self.planning_request_id:
            raise ValueError("planner input request id does not match brief")
        object.__setattr__(self, "context_refs", tuple(self.context_refs))


@dataclass(frozen=True, slots=True)
class PlannerRunControl:
    operation_id: UUID
    started_at: datetime
    deadline_at: datetime | None
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.cancel_requested, bool):
            raise TypeError("cancel_requested must be boolean")


class PlannerInputAssembler:
    def assemble(
        self,
        *,
        brief: PlanningBriefRevision,
        profile_snapshot: PlanningProfileSnapshot,
        capability_catalog: CapabilityCatalogSnapshot,
        policy_constraints: PlannerPolicyConstraints,
        operation_id: UUID,
        planning_request_id: UUID,
        planning_run_id: UUID,
        context_refs: Sequence[ContextReference] = (),
    ) -> PlannerInput:
        normalized_refs = tuple(context_refs)
        brief_refs = set(brief.context_refs)
        if any(item.ref not in brief_refs for item in normalized_refs):
            raise ValueError("planner context reference is not present in the brief")
        return PlannerInput(
            interface_version=profile_snapshot.planner_interface_version,
            proposal_schema_version=profile_snapshot.proposal_schema_version,
            operation_id=operation_id,
            planning_request_id=planning_request_id,
            planning_run_id=planning_run_id,
            brief_revision=brief.revision,
            brief=brief,
            profile_snapshot=profile_snapshot,
            capability_catalog=capability_catalog,
            policy_constraints=policy_constraints,
            context_refs=normalized_refs,
        )


@dataclass(frozen=True, slots=True)
class PlannerProposalResult:
    proposal: ProposalRevision
    usage: PlanningUsage = field(default_factory=PlanningUsage.zero)


@dataclass(frozen=True, slots=True)
class PlannerClarificationResult:
    clarifications: tuple[PlanningClarification, ...]
    usage: PlanningUsage = field(default_factory=PlanningUsage.zero)

    def __post_init__(self) -> None:
        if not self.clarifications:
            raise ValueError("planner clarification result cannot be empty")
        object.__setattr__(self, "clarifications", tuple(self.clarifications))


@dataclass(frozen=True, slots=True)
class PlannerFailure:
    code: str
    safe_message: str
    retryable: bool
    unknown_outcome: bool
    provider_ref: str | None = None
    usage: PlanningUsage = field(default_factory=PlanningUsage.zero)

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.safe_message.strip():
            raise ValueError("planner failure code and safe_message are required")
        if not isinstance(self.usage, PlanningUsage):
            raise TypeError("planner failure usage must be PlanningUsage")
        lowered = self.safe_message.lower()
        if any(marker in lowered for marker in ("password", "api_key", "secret_value")):
            raise ValueError("planner failure safe_message contains a secret marker")


@dataclass(frozen=True, slots=True)
class PlannerExplicitFailure(PlannerFailure):
    pass


@dataclass(frozen=True, slots=True)
class PlannerRateLimited(PlannerFailure):
    pass


@dataclass(frozen=True, slots=True)
class PlannerTimeout(PlannerFailure):
    pass


@dataclass(frozen=True, slots=True)
class PlannerUnknownOutcome(PlannerFailure):
    pass


@dataclass(frozen=True, slots=True)
class PlannerInvalidResult(PlannerFailure):
    pass


type PlannerResult = PlannerProposalResult | PlannerClarificationResult | PlannerFailure
