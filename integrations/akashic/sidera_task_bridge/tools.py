from collections.abc import Callable, Mapping
from typing import Any, Literal
from uuid import UUID

from agent.tools.base import get_current_tool_context

from .client import (
    CommandExecution,
    CommandIntent,
    CommandResultUnknown,
    CommandStatus,
    ControlPlaneRejected,
    DispatchResult,
    TransportUnavailable,
)
from .config import TaskBridgeConfig
from .contracts import (
    GovernanceProposalView,
    GovernanceWorkerItemView,
    JsonValue,
    PlanRevisionBrief,
    PlanRevisionConfirmation,
    ToolEnvelope,
)
from .source_routes import (
    MissingToolContext,
    SourceContextResolver,
    SourceResolution,
)
from .state import BridgeStateStore, StateConflict

_CONFLICT_CODES = {
    "planning_conflict",
    "profile_conflict",
    "version_conflict",
    "idempotency_conflict",
    "resource_busy",
    "command_identity_conflict",
}
_UNAVAILABLE_CODES = {
    "service_unavailable",
    "command_status_unavailable",
}
_GOVERNANCE_ACTIONS = {"pause", "approve", "reject", "resume_automatic"}


class SideraTaskTools:
    """Akashic-facing tools backed by the neutral SIDERA bridge services."""

    def __init__(
        self,
        config: TaskBridgeConfig,
        *,
        client: Any,
        dispatcher: Any,
        state: BridgeStateStore,
        source_resolver: SourceContextResolver,
        session_metadata_provider: Callable[[str], Mapping[str, object]] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._dispatcher = dispatcher
        self._state = state
        self._source_resolver = source_resolver
        self._session_metadata_provider = session_metadata_provider

    async def sidera_submit_brief(
        self,
        event: object,
        goal: str,
        constraints: list[str],
        acceptance_criteria: list[str],
        context_refs: list[str],
        requested_outputs: list[str],
    ) -> str:
        """Submit a structured user brief to the SIDERA planning control plane."""

        del event
        if not _non_empty_text(goal) or not acceptance_criteria or not requested_outputs:
            return _encode(
                ToolEnvelope(
                    outcome="failed",
                    summary="The brief is missing a goal, acceptance criteria, or requested outputs.",
                    error_code="invalid_input",
                )
            )
        try:
            source = await self._resolve_source()
            command = CommandIntent(
                command_type="SubmitPlanningRequest",
                target_type="workspace",
                target_id=None,
                expected_version=None,
                payload={
                    "profile_id": str(self._config.planning_profile_id),
                    "profile_version": self._config.planning_profile_version,
                    "objective": goal.strip(),
                    "constraints": _clean_texts(constraints),
                    "acceptance_criteria": _clean_texts(acceptance_criteria),
                    "context_refs": _clean_texts(context_refs),
                    "requested_outputs": _clean_texts(requested_outputs),
                    "source_type": source.source_type,
                    "source_ref": source.source_ref,
                },
            )
            result = await self._dispatcher.execute(
                f"planning:source:{source.source_ref}", command
            )
            if result.status != "applied":
                return _encode(_dispatch_envelope(result, "brief submission"))
            planning_request_id = _result_uuid(result, "planning_request_id")
            if planning_request_id is None:
                return _encode(
                    ToolEnvelope(
                        outcome="failed",
                        summary="The control plane accepted the brief without a request reference.",
                        error_code="invalid_control_plane_response",
                    )
                )
            try:
                await self._state.bind_resource_source(
                    "planning_request",
                    planning_request_id,
                    source.source_type,
                    source.source_ref,
                )
            except StateConflict:
                return _encode(
                    ToolEnvelope(
                        outcome="failed",
                        summary="The planning request owner binding conflicts with existing state.",
                        resource_ref=f"planning:{planning_request_id}",
                        web_url=self._planning_url(planning_request_id),
                        error_code="source_binding_conflict",
                    )
                )
            data = _result_data(result)
            data = {
                key: data[key]
                for key in ("planning_run_id", "brief_revision")
                if key in data and isinstance(data[key], (str, int))
            }
            data["planning_request_id"] = str(planning_request_id)
            return _encode(
                ToolEnvelope(
                    outcome="ok",
                    summary="The brief was submitted; planning is running asynchronously.",
                    resource_ref=f"planning:{planning_request_id}",
                    web_url=self._planning_url(planning_request_id),
                    data=data,
                )
            )
        except (MissingToolContext, ValueError):
            return _encode(
                ToolEnvelope(
                    outcome="failed",
                    summary="The request source could not be identified safely.",
                    error_code="missing_tool_context",
                )
            )
        except TransportUnavailable:
            return _encode(
                ToolEnvelope(
                    outcome="unavailable",
                    summary="The SIDERA control plane is temporarily unavailable.",
                    error_code="service_unavailable",
                )
            )
        except CommandResultUnknown:
            return _encode(
                ToolEnvelope(
                    outcome="result_unknown",
                    summary="The brief submission result is pending confirmation.",
                    error_code="result_unknown",
                )
            )
        except ControlPlaneRejected as exc:
            return _encode(_rejected_envelope(exc.code, "brief submission"))

    async def sidera_get_planning_status(
        self, event: object, planning_request_id: str
    ) -> str:
        """Read the current planning, clarification, proposal, and materialization summary."""

        del event
        request_id = _parse_uuid(planning_request_id)
        if request_id is None:
            return _encode(_invalid_resource("planning request"))
        try:
            detail = await self._client.get_planning_detail(request_id)
            status = _string(detail.get("status")) or "unknown"
            clarifications: tuple[dict[str, JsonValue], ...] = ()
            if status in {"needs_clarification", "clarification_required"}:
                clarifications = await self._client.list_clarifications(request_id)
            proposal: dict[str, JsonValue] = {}
            revision = _positive_int(detail.get("current_proposal_revision"))
            if revision is not None and status in {
                "proposal_ready",
                "awaiting_confirmation",
                "needs_confirmation",
                "materialized",
            }:
                proposal = await self._client.get_proposal(request_id, revision)
            materialization: dict[str, JsonValue] = {}
            if status in {"materialized", "executing", "running", "completed"}:
                materialization = await self._client.get_materialization(request_id)

            data = _planning_summary(detail, clarifications, proposal, materialization)
            task_id = _parse_uuid(materialization.get("root_task_id"))
            if task_id is not None:
                data["task_id"] = str(task_id)
            if data.get("blocking_questions"):
                outcome = "needs_user"
                summary = "Planning is waiting for answers to blocking questions."
            elif status in {
                "proposal_ready",
                "awaiting_confirmation",
                "needs_confirmation",
            }:
                outcome = "needs_user"
                summary = "The planning proposal is ready for explicit user confirmation."
            elif status in {"failed", "blocked", "cancelled"}:
                outcome = "failed"
                summary = "Planning reached a terminal state that needs review."
            else:
                outcome = "ok"
                summary = "Planning status loaded."
            return _encode(
                ToolEnvelope(
                    outcome=outcome,
                    summary=summary,
                    resource_ref=f"planning:{request_id}",
                    web_url=(
                        self._task_url(task_id)
                        if task_id is not None
                        else self._planning_url(request_id)
                    ),
                    data=data,
                )
            )
        except TransportUnavailable:
            return _encode(
                ToolEnvelope(
                    outcome="unavailable",
                    summary="The SIDERA control plane is temporarily unavailable.",
                    error_code="service_unavailable",
                )
            )
        except ControlPlaneRejected as exc:
            return _encode(_rejected_envelope(exc.code, "planning status"))

    async def sidera_create_plan_revision(
        self,
        event: object,
        root_task_id: str,
        goal: str,
        constraints: list[str],
        acceptance_criteria: list[str],
    ) -> str:
        """Create a revision request against the current active plan head."""

        del event
        task_id = _parse_uuid(root_task_id)
        brief = _revision_brief(goal, constraints, acceptance_criteria)
        if task_id is None:
            return _encode(_invalid_resource("root Task"))
        if brief is None:
            return _encode(
                ToolEnvelope(
                    outcome="failed",
                    summary="The plan revision brief is invalid.",
                    resource_ref=f"task:{task_id}",
                    web_url=self._task_url(task_id),
                    error_code="invalid_revision_brief",
                )
            )
        try:
            source = await self._resolve_source()
            task = await self._client.get_task(task_id)
            active_plan = await self._client.get_active_plan(task_id)
            root_version = _positive_int(task.get("version"))
            base = _active_plan_base(active_plan, task_id)
            if root_version is None or base is None:
                return _encode(
                    ToolEnvelope(
                        outcome="failed",
                        summary="The control plane returned an incomplete revision base.",
                        resource_ref=f"task:{task_id}",
                        web_url=self._task_url(task_id),
                        error_code="invalid_control_plane_response",
                    )
                )
            result = await self._dispatcher.execute(
                f"planning:root:{task_id}",
                CommandIntent(
                    command_type="SubmitPlanRevision",
                    target_type="task",
                    target_id=task_id,
                    expected_version=root_version,
                    payload={
                        "root_task_id": str(task_id),
                        **base,
                        **brief.model_dump(mode="json"),
                        "planning_profile_id": str(self._config.planning_profile_id),
                        "planning_profile_version": self._config.planning_profile_version,
                    },
                ),
            )
            if result.status != "applied":
                return _encode(
                    self._with_task_ref(_dispatch_envelope(result, "plan revision"), task_id)
                )
            revision_id = _result_uuid(result, "planning_request_id")
            if revision_id is None:
                return _encode(
                    self._with_task_ref(
                        ToolEnvelope(
                            outcome="failed",
                            summary="The control plane accepted the revision without a request reference.",
                            error_code="invalid_control_plane_response",
                        ),
                        task_id,
                    )
                )
            try:
                await self._state.bind_resource_source(
                    "planning_request",
                    revision_id,
                    source.source_type,
                    source.source_ref,
                )
            except StateConflict:
                return _encode(
                    self._with_revision_ref(
                        ToolEnvelope(
                            outcome="failed",
                            summary="The revision owner binding conflicts with existing state.",
                            error_code="source_binding_conflict",
                        ),
                        revision_id,
                        task_id,
                    )
                )
            data = _safe_mutation_data(result)
            data["planning_request_id"] = str(revision_id)
            data["root_task_id"] = str(task_id)
            return _encode(
                ToolEnvelope(
                    outcome="ok",
                    summary="The plan revision was created; planning is running asynchronously.",
                    resource_ref=f"revision:{revision_id}",
                    web_url=self._revision_url(revision_id, task_id),
                    data=data,
                )
            )
        except (MissingToolContext, ValueError):
            return _encode(
                self._with_task_ref(
                    ToolEnvelope(
                        outcome="failed",
                        summary="The request source or revision base could not be identified safely.",
                        error_code="missing_tool_context",
                    ),
                    task_id,
                )
            )
        except TransportUnavailable:
            return _encode(self._with_task_ref(_unavailable_error(), task_id))
        except CommandResultUnknown:
            return _encode(
                self._with_task_ref(
                    ToolEnvelope(
                        outcome="result_unknown",
                        summary="The plan revision result is pending confirmation.",
                        error_code="result_unknown",
                    ),
                    task_id,
                )
            )
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_task_ref(_rejected_envelope(exc.code, "plan revision"), task_id)
            )

    async def sidera_get_plan_revision(
        self, event: object, planning_request_id: str
    ) -> str:
        """Read a bounded revision state and hierarchy diff."""

        del event
        request_id = _parse_uuid(planning_request_id)
        if request_id is None:
            return _encode(_invalid_resource("plan revision"))
        try:
            detail = await self._client.get_planning_detail(request_id)
            kind_result = _revision_kind_preflight(detail)
            if kind_result is not None:
                return _encode(self._with_revision_ref(kind_result, request_id))
            status = _string(detail.get("status")) or "unknown"
            diff: dict[str, JsonValue] = {}
            revision = _positive_int(detail.get("current_proposal_revision"))
            if revision is not None and status not in {
                "queued",
                "planning",
                "running",
                "cancelled",
            }:
                diff = await self._client.get_revision_diff(request_id)
            materialization: dict[str, JsonValue] = {}
            if status in {"materialized", "executing", "running", "completed"}:
                materialization = await self._client.get_materialization(request_id)
            root_task_id = _revision_root_id(detail, diff, materialization)
            data = _plan_revision_summary(detail, diff, materialization)
            if root_task_id is not None:
                data["root_task_id"] = str(root_task_id)
            outcome, summary = _revision_read_outcome(status, detail, diff)
            return _encode(
                ToolEnvelope(
                    outcome=outcome,
                    summary=summary,
                    resource_ref=f"revision:{request_id}",
                    web_url=self._revision_url(request_id, root_task_id),
                    data=data,
                )
            )
        except TransportUnavailable:
            return _encode(
                self._with_revision_ref(_unavailable_error(), request_id)
            )
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_revision_ref(_rejected_envelope(exc.code, "plan revision"), request_id)
            )

    async def sidera_submit_plan_revision_feedback(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        feedback: str,
    ) -> str:
        """Request a new revision proposal from one exact current proposal."""

        del event
        request_id = _parse_uuid(planning_request_id)
        if request_id is None:
            return _encode(_invalid_resource("plan revision"))
        if (
            request_version < 1
            or proposal_revision < 1
            or not _non_empty_text(proposal_content_hash)
            or not _non_empty_text(feedback)
        ):
            return _encode(
                self._with_revision_ref(
                    ToolEnvelope(
                        outcome="failed",
                        summary="The plan revision feedback is invalid.",
                        error_code="invalid_revision_feedback",
                    ),
                    request_id,
                )
            )
        try:
            detail = await self._client.get_planning_detail(request_id)
            preflight = _revision_proposal_preflight(
                detail,
                request_version,
                proposal_revision,
                proposal_content_hash,
            )
            if preflight is not None:
                return _encode(self._with_revision_ref(preflight, request_id))
            root_task_id = _revision_root_id(detail)
            result = await self._dispatcher.execute(
                f"planning:request:{request_id}",
                CommandIntent(
                    command_type="RequestProposalRevision",
                    target_type="planning_request",
                    target_id=request_id,
                    expected_version=request_version,
                    payload={
                        "current_proposal_revision": proposal_revision,
                        "current_proposal_content_hash": proposal_content_hash.strip(),
                        "feedback": feedback.strip(),
                    },
                ),
            )
            return _encode(
                self._revision_mutation_result(
                    result, request_id, "plan revision feedback", root_task_id
                )
            )
        except TransportUnavailable:
            return _encode(self._with_revision_ref(_unavailable_error(), request_id))
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_revision_ref(
                    _rejected_envelope(exc.code, "plan revision feedback"), request_id
                )
            )

    async def sidera_confirm_plan_revision(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        diff_hash: str,
        base_snapshot_id: str,
        base_content_hash: str,
        explicit_user_confirmation: bool,
    ) -> str:
        """Confirm one exact revision only after an explicit user decision."""

        del event
        request_id = _parse_uuid(planning_request_id)
        if request_id is None:
            return _encode(_invalid_resource("plan revision"))
        if not explicit_user_confirmation:
            return _encode(
                self._with_revision_ref(
                    ToolEnvelope(
                        outcome="needs_user",
                        summary="Explicit user confirmation is required before activating the new plan.",
                        error_code="explicit_confirmation_required",
                    ),
                    request_id,
                )
            )
        confirmation = _revision_confirmation(
            proposal_revision,
            proposal_content_hash,
            diff_hash,
            base_snapshot_id,
            base_content_hash,
        )
        if request_version < 1 or confirmation is None:
            return _encode(
                self._with_revision_ref(
                    ToolEnvelope(
                        outcome="failed",
                        summary="The exact plan revision confirmation fields are invalid.",
                        error_code="invalid_revision_confirmation",
                    ),
                    request_id,
                )
            )
        root_task_id: UUID | None = None
        try:
            detail = await self._client.get_planning_detail(request_id)
            root_task_id = _revision_root_id(detail)
            preflight = _revision_proposal_preflight(
                detail,
                request_version,
                confirmation.proposal_revision,
                confirmation.proposal_content_hash,
                expected_profile=(
                    self._config.planning_profile_id,
                    self._config.planning_profile_version,
                ),
            )
            if preflight is not None:
                return _encode(
                    self._with_revision_ref(preflight, request_id, root_task_id)
                )
            diff = await self._client.get_revision_diff(request_id)
            fence_result = _revision_fence_preflight(confirmation, diff)
            if fence_result is not None:
                return _encode(
                    self._with_revision_ref(fence_result, request_id, root_task_id)
                )
            result = await self._dispatcher.execute(
                f"planning:request:{request_id}",
                CommandIntent(
                    command_type="ConfirmPlanRevision",
                    target_type="planning_request",
                    target_id=request_id,
                    expected_version=request_version,
                    payload={
                        "expected_proposal_revision": confirmation.proposal_revision,
                        "expected_proposal_content_hash": confirmation.proposal_content_hash,
                        "expected_diff_hash": confirmation.diff_hash,
                        "expected_base_snapshot_id": str(confirmation.base_snapshot_id),
                        "expected_base_content_hash": confirmation.base_content_hash,
                        "profile_id": str(self._config.planning_profile_id),
                        "profile_version": self._config.planning_profile_version,
                    },
                ),
            )
            return _encode(
                self._revision_mutation_result(
                    result, request_id, "plan revision confirmation", root_task_id
                )
            )
        except TransportUnavailable:
            return _encode(
                self._with_revision_ref(_unavailable_error(), request_id, root_task_id)
            )
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_revision_ref(
                    _rejected_envelope(exc.code, "plan revision confirmation"),
                    request_id,
                    root_task_id,
                )
            )

    async def sidera_reject_plan_revision(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        reason: str,
    ) -> str:
        """Reject one exact revision proposal with a user-provided reason."""

        del event
        request_id = _parse_uuid(planning_request_id)
        if request_id is None:
            return _encode(_invalid_resource("plan revision"))
        if (
            request_version < 1
            or proposal_revision < 1
            or not _non_empty_text(proposal_content_hash)
            or not _non_empty_text(reason)
        ):
            return _encode(
                self._with_revision_ref(
                    ToolEnvelope(
                        outcome="failed",
                        summary="The plan revision rejection fields are invalid.",
                        error_code="invalid_revision_rejection",
                    ),
                    request_id,
                )
            )
        try:
            detail = await self._client.get_planning_detail(request_id)
            preflight = _revision_proposal_preflight(
                detail,
                request_version,
                proposal_revision,
                proposal_content_hash,
            )
            if preflight is not None:
                return _encode(self._with_revision_ref(preflight, request_id))
            root_task_id = _revision_root_id(detail)
            result = await self._dispatcher.execute(
                f"planning:request:{request_id}",
                CommandIntent(
                    command_type="RejectPlanningProposal",
                    target_type="planning_request",
                    target_id=request_id,
                    expected_version=request_version,
                    payload={
                        "current_proposal_revision": proposal_revision,
                        "current_proposal_content_hash": proposal_content_hash.strip(),
                        "reason": reason.strip(),
                    },
                ),
            )
            return _encode(
                self._revision_mutation_result(
                    result, request_id, "plan revision rejection", root_task_id
                )
            )
        except TransportUnavailable:
            return _encode(self._with_revision_ref(_unavailable_error(), request_id))
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_revision_ref(
                    _rejected_envelope(exc.code, "plan revision rejection"), request_id
                )
            )

    async def sidera_cancel_plan_revision(
        self, event: object, planning_request_id: str, request_version: int
    ) -> str:
        """Cancel one revision request after checking its exact request version."""

        del event
        request_id = _parse_uuid(planning_request_id)
        if request_id is None:
            return _encode(_invalid_resource("plan revision"))
        if request_version < 1:
            return _encode(
                self._with_revision_ref(
                    ToolEnvelope(
                        outcome="failed",
                        summary="The plan revision request version is invalid.",
                        error_code="invalid_request_version",
                    ),
                    request_id,
                )
            )
        try:
            detail = await self._client.get_planning_detail(request_id)
            preflight = _revision_version_preflight(detail, request_version)
            if preflight is not None:
                return _encode(self._with_revision_ref(preflight, request_id))
            root_task_id = _revision_root_id(detail)
            result = await self._dispatcher.execute(
                f"planning:request:{request_id}",
                CommandIntent(
                    command_type="CancelPlanningRequest",
                    target_type="planning_request",
                    target_id=request_id,
                    expected_version=request_version,
                    payload={},
                ),
            )
            return _encode(
                self._revision_mutation_result(
                    result, request_id, "plan revision cancellation", root_task_id
                )
            )
        except TransportUnavailable:
            return _encode(self._with_revision_ref(_unavailable_error(), request_id))
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_revision_ref(
                    _rejected_envelope(exc.code, "plan revision cancellation"), request_id
                )
            )

    async def sidera_get_plan_transition(
        self, event: object, plan_transition_id: str
    ) -> str:
        """Read bounded plan and WorkerItem transition convergence facts."""

        del event
        transition_id = _parse_uuid(plan_transition_id)
        if transition_id is None:
            return _encode(_invalid_resource("plan transition"))
        try:
            transition = await self._client.get_plan_transition(transition_id)
            data = _plan_transition_summary(transition, transition_id)
            root_task_id = _parse_uuid(data.get("root_task_id"))
            blocked_unknown = bool(data.get("blocked_unknown_count", 0))
            degraded = (_string(data.get("status")) or "").lower() == "degraded"
            if blocked_unknown or degraded:
                outcome = "needs_user"
                summary = "The plan transition needs review before convergence can continue."
            else:
                outcome = "ok"
                summary = "Plan transition status loaded."
            return _encode(
                ToolEnvelope(
                    outcome=outcome,
                    summary=summary,
                    resource_ref=f"plan-transition:{transition_id}",
                    web_url=(
                        self._task_url(root_task_id) if root_task_id is not None else None
                    ),
                    data=data,
                )
            )
        except TransportUnavailable:
            return _encode(_unavailable_error())
        except ControlPlaneRejected as exc:
            return _encode(_rejected_envelope(exc.code, "plan transition"))

    async def sidera_answer_clarifications(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        answers: list[dict[str, str]],
    ) -> str:
        """Submit explicit answers for the current blocking planning questions."""

        del event
        request_id = _parse_uuid(planning_request_id)
        normalized = _normalize_answers(answers)
        if request_id is None:
            return _encode(_invalid_resource("planning request"))
        if normalized is None or request_version < 1:
            return _encode(
                ToolEnvelope(
                    outcome="failed",
                    summary="The clarification answers are invalid.",
                    error_code="invalid_answers",
                )
            )
        try:
            detail = await self._client.get_planning_detail(request_id)
            preflight = _version_preflight(detail, request_version)
            if preflight is not None:
                return _encode(self._with_request_ref(preflight, request_id))
            result = await self._dispatcher.execute(
                f"planning:request:{request_id}",
                CommandIntent(
                    command_type="AnswerPlanningClarification",
                    target_type="planning_request",
                    target_id=request_id,
                    expected_version=request_version,
                    payload={"answers": normalized},
                ),
            )
            return _encode(
                self._mutation_result(
                    result,
                    request_id,
                    "clarification answers",
                )
            )
        except TransportUnavailable:
            return _encode(self._with_request_ref(_unavailable_error(), request_id))
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_request_ref(
                    _rejected_envelope(exc.code, "clarification answers"), request_id
                )
            )

    async def sidera_request_proposal_revision(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        feedback: str,
    ) -> str:
        """Request a new immutable planning proposal revision with user feedback."""

        del event
        request_id = _parse_uuid(planning_request_id)
        if request_id is None:
            return _encode(_invalid_resource("planning request"))
        if (
            request_version < 1
            or proposal_revision < 1
            or not _non_empty_text(proposal_content_hash)
            or not _non_empty_text(feedback)
        ):
            return _encode(
                ToolEnvelope(
                    outcome="failed",
                    summary="The proposal revision request is invalid.",
                    error_code="invalid_proposal_revision",
                )
            )
        try:
            detail = await self._client.get_planning_detail(request_id)
            preflight = _proposal_preflight(
                detail,
                request_version,
                proposal_revision,
                proposal_content_hash,
            )
            if preflight is not None:
                return _encode(self._with_request_ref(preflight, request_id))
            result = await self._dispatcher.execute(
                f"planning:request:{request_id}",
                CommandIntent(
                    command_type="RequestProposalRevision",
                    target_type="planning_request",
                    target_id=request_id,
                    expected_version=request_version,
                    payload={
                        "current_proposal_revision": proposal_revision,
                        "current_proposal_content_hash": proposal_content_hash.strip(),
                        "feedback": feedback.strip(),
                    },
                ),
            )
            return _encode(
                self._mutation_result(result, request_id, "proposal revision request")
            )
        except TransportUnavailable:
            return _encode(self._with_request_ref(_unavailable_error(), request_id))
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_request_ref(
                    _rejected_envelope(exc.code, "proposal revision request"),
                    request_id,
                )
            )

    async def sidera_confirm_proposal(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        explicit_user_confirmation: bool,
    ) -> str:
        """Confirm one exact proposal revision after an explicit user confirmation."""

        del event
        request_id = _parse_uuid(planning_request_id)
        if request_id is None:
            return _encode(_invalid_resource("planning request"))
        if not explicit_user_confirmation:
            return _encode(
                ToolEnvelope(
                    outcome="needs_user",
                    summary="Explicit user confirmation is required before materialization.",
                    resource_ref=f"planning:{request_id}",
                    web_url=self._planning_url(request_id),
                    error_code="explicit_confirmation_required",
                )
            )
        if request_version < 1 or proposal_revision < 1 or not _non_empty_text(
            proposal_content_hash
        ):
            return _encode(
                ToolEnvelope(
                    outcome="failed",
                    summary="The proposal confirmation fields are invalid.",
                    error_code="invalid_proposal_confirmation",
                )
            )
        try:
            detail = await self._client.get_planning_detail(request_id)
            preflight = _proposal_preflight(
                detail,
                request_version,
                proposal_revision,
                proposal_content_hash,
                expected_profile=(
                    self._config.planning_profile_id,
                    self._config.planning_profile_version,
                ),
            )
            if preflight is not None:
                return _encode(self._with_request_ref(preflight, request_id))
            result = await self._dispatcher.execute(
                f"planning:request:{request_id}",
                CommandIntent(
                    command_type="ConfirmPlanningProposal",
                    target_type="planning_request",
                    target_id=request_id,
                    expected_version=request_version,
                    payload={
                        "proposal_revision": proposal_revision,
                        "proposal_content_hash": proposal_content_hash.strip(),
                        "profile_id": str(self._config.planning_profile_id),
                        "profile_version": self._config.planning_profile_version,
                    },
                ),
            )
            return _encode(
                self._mutation_result(result, request_id, "proposal confirmation")
            )
        except TransportUnavailable:
            return _encode(self._with_request_ref(_unavailable_error(), request_id))
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_request_ref(
                    _rejected_envelope(exc.code, "proposal confirmation"), request_id
                )
            )

    async def sidera_reject_proposal(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        reason: str,
    ) -> str:
        """Reject one exact proposal revision with a user-provided reason."""

        del event
        request_id = _parse_uuid(planning_request_id)
        if request_id is None:
            return _encode(_invalid_resource("planning request"))
        if request_version < 1 or proposal_revision < 1 or not _non_empty_text(
            proposal_content_hash
        ) or not _non_empty_text(reason):
            return _encode(
                ToolEnvelope(
                    outcome="failed",
                    summary="The proposal rejection fields are invalid.",
                    error_code="invalid_proposal_rejection",
                )
            )
        try:
            detail = await self._client.get_planning_detail(request_id)
            preflight = _proposal_preflight(
                detail,
                request_version,
                proposal_revision,
                proposal_content_hash,
            )
            if preflight is not None:
                return _encode(self._with_request_ref(preflight, request_id))
            result = await self._dispatcher.execute(
                f"planning:request:{request_id}",
                CommandIntent(
                    command_type="RejectPlanningProposal",
                    target_type="planning_request",
                    target_id=request_id,
                    expected_version=request_version,
                    payload={
                        "current_proposal_revision": proposal_revision,
                        "current_proposal_content_hash": proposal_content_hash.strip(),
                        "reason": reason.strip(),
                    },
                ),
            )
            return _encode(self._mutation_result(result, request_id, "proposal rejection"))
        except TransportUnavailable:
            return _encode(self._with_request_ref(_unavailable_error(), request_id))
        except ControlPlaneRejected as exc:
            return _encode(
                self._with_request_ref(
                    _rejected_envelope(exc.code, "proposal rejection"), request_id
                )
            )

    def _mutation_result(
        self, result: DispatchResult, request_id: UUID, operation: str
    ) -> ToolEnvelope:
        if result.status != "applied":
            return self._with_request_ref(_dispatch_envelope(result, operation), request_id)
        data = _result_data(result)
        safe_data: dict[str, JsonValue] = {
            key: data[key]
            for key in (
                "planning_request_id",
                "planning_run_id",
                "brief_revision",
                "status",
            )
            if key in data and isinstance(data[key], (str, int))
        }
        safe_data.setdefault("planning_request_id", str(request_id))
        return ToolEnvelope(
            outcome="ok",
            summary=f"The {operation} was accepted.",
            resource_ref=f"planning:{request_id}",
            web_url=self._planning_url(request_id),
            data=safe_data,
        )

    def _revision_mutation_result(
        self,
        result: DispatchResult,
        request_id: UUID,
        operation: str,
        root_task_id: UUID | None = None,
    ) -> ToolEnvelope:
        if result.status != "applied":
            return self._with_revision_ref(
                _dispatch_envelope(result, operation), request_id, root_task_id
            )
        safe_data = _safe_mutation_data(result)
        safe_data.setdefault("planning_request_id", str(request_id))
        if root_task_id is not None:
            safe_data.setdefault("root_task_id", str(root_task_id))
        return ToolEnvelope(
            outcome="ok",
            summary=f"The {operation} was accepted.",
            resource_ref=f"revision:{request_id}",
            web_url=self._revision_url(request_id, root_task_id),
            data=safe_data,
        )

    def _with_request_ref(
        self, envelope: ToolEnvelope, request_id: UUID
    ) -> ToolEnvelope:
        return envelope.model_copy(
            update={
                "resource_ref": f"planning:{request_id}",
                "web_url": self._planning_url(request_id),
            }
        )

    def _with_revision_ref(
        self,
        envelope: ToolEnvelope,
        request_id: UUID,
        root_task_id: UUID | None = None,
    ) -> ToolEnvelope:
        return envelope.model_copy(
            update={
                "resource_ref": f"revision:{request_id}",
                "web_url": self._revision_url(request_id, root_task_id),
            }
        )

    def _with_task_ref(
        self, envelope: ToolEnvelope, task_id: UUID
    ) -> ToolEnvelope:
        return envelope.model_copy(
            update={
                "resource_ref": f"task:{task_id}",
                "web_url": self._task_url(task_id),
            }
        )

    async def sidera_get_task_status(self, event: object, task_id: str) -> str:
        """Read the bounded Task First overview for a materialized root task."""

        del event
        root_task_id = _parse_uuid(task_id)
        if root_task_id is None:
            return _encode(_invalid_resource("Task"))
        try:
            overview = await self._client.get_task_overview(root_task_id)
            data = _task_summary(overview)
            return _encode(
                ToolEnvelope(
                    outcome="ok",
                    summary="Task progress loaded.",
                    resource_ref=f"task:{root_task_id}",
                    web_url=self._task_url(root_task_id),
                    data=data,
                )
            )
        except TransportUnavailable:
            return _encode(
                ToolEnvelope(
                    outcome="unavailable",
                    summary="The SIDERA control plane is temporarily unavailable.",
                    error_code="service_unavailable",
                )
            )
        except ControlPlaneRejected as exc:
            return _encode(_rejected_envelope(exc.code, "Task status"))

    async def sidera_get_collaboration_governance(
        self, event: object, worker_item_id: str
    ) -> str:
        """Read one bounded WorkerItem release-governance summary."""

        del event
        parsed_worker_item_id = _parse_uuid(worker_item_id)
        if parsed_worker_item_id is None:
            return _encode(_invalid_resource("WorkerItem"))
        try:
            view = await self._governance_worker_view(parsed_worker_item_id)
            view_error = _worker_view_preflight(
                view, parsed_worker_item_id, self._config.workspace_id
            )
            if view_error is not None:
                return _encode(view_error)
            return _encode(
                _governance_read_envelope(
                    view, web_url=self._worker_item_url(parsed_worker_item_id)
                )
            )
        except TransportUnavailable:
            return _encode(_unavailable_error())
        except ControlPlaneRejected as exc:
            return _encode(_governance_rejected(exc, "governance status"))
        except (TypeError, ValueError):
            return _encode(
                _governance_invalid_control_plane(
                    f"worker-item:{parsed_worker_item_id}",
                    self._worker_item_url(parsed_worker_item_id),
                )
            )

    async def sidera_pause_collaboration_release(
        self,
        event: object,
        worker_item_id: str,
        worker_item_version: int,
        target_plan_generation: int,
        reason: str | None = None,
    ) -> str:
        """Pause automatic release for exactly one current WorkerItem."""

        del event
        parsed_worker_item_id = _parse_uuid(worker_item_id)
        if parsed_worker_item_id is None:
            return _encode(_invalid_resource("WorkerItem"))
        invalid = _governance_input_preflight(
            worker_item_version, target_plan_generation
        )
        if invalid is not None:
            return _encode(invalid)
        if not self._governance_principal_configured():
            return _encode(_governance_unavailable())
        normalized_reason, reason_error = _governance_reason(reason)
        if reason_error is not None:
            return _encode(reason_error)
        try:
            view = await self._governance_worker_view(parsed_worker_item_id)
            view_error = _worker_view_preflight(
                view, parsed_worker_item_id, self._config.workspace_id
            )
            if view_error is not None:
                return _encode(view_error)
            version = await self._worker_item_version(
                parsed_worker_item_id, view.root_task_id
            )
            if version != worker_item_version:
                return _encode(_governance_version_conflict("WorkerItem"))
            generation = await self._active_plan_generation(view.root_task_id)
            if generation != target_plan_generation:
                return _encode(_governance_generation_conflict())
            source_error = await self._governance_source_preflight(
                view.root_task_id,
                resource_ref=f"worker-item:{parsed_worker_item_id}",
                web_url=self._worker_item_url(parsed_worker_item_id),
            )
            if source_error is not None:
                return _encode(source_error)
            action_error = _available_action_preflight(view, "pause")
            if action_error is not None:
                return _encode(action_error)
            payload: dict[str, JsonValue] = {
                "transport": "main_agent",
                "target_plan_generation": target_plan_generation,
            }
            if normalized_reason is not None:
                payload["reason"] = normalized_reason
            result = await self._dispatcher.execute(
                f"collaboration:worker-item:{parsed_worker_item_id}",
                CommandIntent(
                    command_type="PauseCollaborationRelease",
                    target_type="worker_item",
                    target_id=parsed_worker_item_id,
                    expected_version=worker_item_version,
                    payload=payload,
                ),
            )
            return _encode(
                _governance_mutation_result(
                    result,
                    operation="collaboration release pause",
                    resource_ref=f"worker-item:{parsed_worker_item_id}",
                    web_url=self._worker_item_url(parsed_worker_item_id),
                    allowed_data=("hold_id", "hold_version", "governance_mode"),
                )
            )
        except (MissingToolContext, StateConflict):
            return _encode(_governance_source_context_error())
        except (TypeError, ValueError):
            return _encode(
                _governance_invalid_control_plane(
                    f"worker-item:{parsed_worker_item_id}",
                    self._worker_item_url(parsed_worker_item_id),
                )
            )
        except TransportUnavailable:
            return _encode(
                _with_governance_ref(
                    _unavailable_error(),
                    f"worker-item:{parsed_worker_item_id}",
                    self._worker_item_url(parsed_worker_item_id),
                )
            )
        except CommandResultUnknown:
            return _encode(
                _with_governance_ref(
                    ToolEnvelope(
                        outcome="result_unknown",
                        summary="The collaboration release pause result is pending confirmation.",
                        error_code="result_unknown",
                    ),
                    f"worker-item:{parsed_worker_item_id}",
                    self._worker_item_url(parsed_worker_item_id),
                )
            )
        except ControlPlaneRejected as exc:
            return _encode(
                _with_governance_ref(
                    _rejected_envelope(exc.code, "collaboration release pause"),
                    f"worker-item:{parsed_worker_item_id}",
                    self._worker_item_url(parsed_worker_item_id),
                )
            )

    async def sidera_approve_collaboration_release(
        self,
        event: object,
        proposal_id: str,
        proposal_version: int,
        explicit_user_confirmation: bool,
        reason: str | None = None,
    ) -> str:
        """Approve exactly one current release Proposal after user confirmation."""

        del event
        parsed_proposal_id = _parse_uuid(proposal_id)
        if parsed_proposal_id is None:
            return _encode(_invalid_resource("release Proposal"))
        if explicit_user_confirmation is not True:
            return _encode(_explicit_confirmation_required())
        if not _valid_positive_int(proposal_version):
            return _encode(_invalid_governance_input())
        if not self._governance_principal_configured():
            return _encode(_governance_unavailable())
        normalized_reason, reason_error = _governance_reason(reason)
        if reason_error is not None:
            return _encode(reason_error)
        return await self._resolve_proposal_governance_write(
            parsed_proposal_id,
            proposal_version,
            command_type="ApproveCollaborationReleaseProposal",
            operation="collaboration release approval",
            normalized_reason=normalized_reason,
        )

    async def sidera_reject_collaboration_release(
        self,
        event: object,
        proposal_id: str,
        proposal_version: int,
        reason: str | None = None,
    ) -> str:
        """Reject exactly one current release Proposal."""

        del event
        parsed_proposal_id = _parse_uuid(proposal_id)
        if parsed_proposal_id is None:
            return _encode(_invalid_resource("release Proposal"))
        if not _valid_positive_int(proposal_version):
            return _encode(_invalid_governance_input())
        if not self._governance_principal_configured():
            return _encode(_governance_unavailable())
        normalized_reason, reason_error = _governance_reason(reason)
        if reason_error is not None:
            return _encode(reason_error)
        return await self._resolve_proposal_governance_write(
            parsed_proposal_id,
            proposal_version,
            command_type="RejectCollaborationReleaseProposal",
            operation="collaboration release rejection",
            normalized_reason=normalized_reason,
        )

    async def sidera_resume_automatic_collaboration(
        self,
        event: object,
        worker_item_id: str,
        hold_id: str,
        hold_version: int,
        reason: str | None = None,
    ) -> str:
        """Resume automatic release for exactly one active governance Hold."""

        del event
        parsed_worker_item_id = _parse_uuid(worker_item_id)
        parsed_hold_id = _parse_uuid(hold_id)
        if parsed_worker_item_id is None:
            return _encode(_invalid_resource("WorkerItem"))
        if parsed_hold_id is None:
            return _encode(_invalid_resource("governance Hold"))
        if not _valid_positive_int(hold_version):
            return _encode(_invalid_governance_input())
        if not self._governance_principal_configured():
            return _encode(_governance_unavailable())
        normalized_reason, reason_error = _governance_reason(reason)
        if reason_error is not None:
            return _encode(reason_error)
        resource_ref = f"worker-item:{parsed_worker_item_id}"
        web_url = self._worker_item_url(parsed_worker_item_id)
        try:
            view = await self._governance_worker_view(parsed_worker_item_id)
            view_error = _worker_view_preflight(
                view, parsed_worker_item_id, self._config.workspace_id
            )
            if view_error is not None:
                return _encode(view_error)
            await self._worker_item_version(
                parsed_worker_item_id, view.root_task_id
            )
            governance = _governance_mapping(view.governance)
            active_hold_id = _parse_uuid(governance.get("active_hold_id"))
            actual_hold_version = _positive_int(
                governance.get("hold_resource_version")
            )
            if active_hold_id != parsed_hold_id or actual_hold_version != hold_version:
                return _encode(_governance_hold_conflict())
            source_error = await self._governance_source_preflight(
                view.root_task_id,
                resource_ref=resource_ref,
                web_url=web_url,
            )
            if source_error is not None:
                return _encode(source_error)
            action_error = _available_action_preflight(view, "resume_automatic")
            if action_error is not None:
                return _encode(action_error)
            payload: dict[str, JsonValue] = {"transport": "main_agent"}
            if normalized_reason is not None:
                payload["reason"] = normalized_reason
            result = await self._dispatcher.execute(
                f"collaboration:hold:{parsed_hold_id}",
                CommandIntent(
                    command_type="ResumeAutomaticCollaboration",
                    target_type="collaboration_governance_hold",
                    target_id=parsed_hold_id,
                    expected_version=hold_version,
                    payload=payload,
                ),
            )
            return _encode(
                _governance_mutation_result(
                    result,
                    operation="automatic collaboration resume",
                    resource_ref=resource_ref,
                    web_url=web_url,
                    allowed_data=(
                        "hold_id",
                        "hold_version",
                        "governance_mode",
                        "staled_proposal_count",
                    ),
                    warning="Pending release proposals become stale when automatic collaboration resumes.",
                )
            )
        except (MissingToolContext, StateConflict):
            return _encode(_governance_source_context_error())
        except (TypeError, ValueError):
            return _encode(_governance_invalid_control_plane(resource_ref, web_url))
        except TransportUnavailable:
            return _encode(_with_governance_ref(_unavailable_error(), resource_ref, web_url))
        except CommandResultUnknown:
            return _encode(
                _with_governance_ref(
                    ToolEnvelope(
                        outcome="result_unknown",
                        summary="The automatic collaboration resume result is pending confirmation.",
                        error_code="result_unknown",
                    ),
                    resource_ref,
                    web_url,
                )
            )
        except ControlPlaneRejected as exc:
            return _encode(
                _with_governance_ref(
                    _rejected_envelope(exc.code, "automatic collaboration resume"),
                    resource_ref,
                    web_url,
                )
            )

    async def _resolve_proposal_governance_write(
        self,
        proposal_id: UUID,
        proposal_version: int,
        *,
        command_type: str,
        operation: str,
        normalized_reason: str | None,
    ) -> str:
        proposal_ref = f"proposal:{proposal_id}"
        try:
            proposal = await self._governance_proposal_view(proposal_id)
            proposal_error = _proposal_view_preflight(
                proposal, proposal_id, self._config.workspace_id
            )
            if proposal_error is not None:
                return _encode(proposal_error)
            if proposal.resource_version != proposal_version:
                return _encode(_governance_version_conflict("Proposal"))
            if proposal.status != "pending":
                return _encode(_governance_proposal_not_pending())
            action = "approve" if command_type.startswith("Approve") else "reject"

            worker = await self._governance_worker_view(proposal.worker_item_id)
            worker_error = _worker_view_preflight(
                worker, proposal.worker_item_id, self._config.workspace_id
            )
            if worker_error is not None:
                return _encode(worker_error)
            worker_governance = _governance_mapping(worker.governance)
            worker_hold_id = _parse_uuid(worker_governance.get("active_hold_id"))
            worker_proposal = _active_proposal_id(worker_governance)
            if (
                worker.root_task_id != proposal.root_task_id
                or worker_hold_id != proposal.hold_id
                or worker_proposal != proposal.proposal_id
            ):
                return _encode(_governance_lineage_conflict())
            source_error = await self._governance_source_preflight(
                proposal.root_task_id,
                resource_ref=proposal_ref,
                web_url=self._proposal_url(proposal_id, proposal.deep_link),
            )
            if source_error is not None:
                return _encode(source_error)
            action_error = _available_action_preflight(proposal, action)
            if action_error is not None:
                return _encode(action_error)
            payload: dict[str, JsonValue] = {"transport": "main_agent"}
            if normalized_reason is not None:
                payload["reason"] = normalized_reason
            result = await self._dispatcher.execute(
                f"collaboration:proposal:{proposal_id}",
                CommandIntent(
                    command_type=command_type,
                    target_type="collaboration_release_proposal",
                    target_id=proposal_id,
                    expected_version=proposal_version,
                    payload=payload,
                ),
            )
            return _encode(
                _governance_mutation_result(
                    result,
                    operation=operation,
                    resource_ref=proposal_ref,
                    web_url=self._proposal_url(proposal_id, proposal.deep_link),
                    allowed_data=(
                        "proposal_id",
                        "proposal_status",
                        "application_status",
                        "hold_id",
                        "release_id",
                        "result",
                    ),
                )
            )
        except (MissingToolContext, StateConflict):
            return _encode(_governance_source_context_error())
        except (TypeError, ValueError):
            return _encode(
                _governance_invalid_control_plane(
                    proposal_ref, self._proposal_url(proposal_id)
                )
            )
        except TransportUnavailable:
            return _encode(
                _with_governance_ref(
                    _unavailable_error(), proposal_ref, self._proposal_url(proposal_id)
                )
            )
        except CommandResultUnknown:
            return _encode(
                _with_governance_ref(
                    ToolEnvelope(
                        outcome="result_unknown",
                        summary=f"The {operation} result is pending confirmation.",
                        error_code="result_unknown",
                    ),
                    proposal_ref,
                    self._proposal_url(proposal_id),
                )
            )
        except ControlPlaneRejected as exc:
            return _encode(
                _with_governance_ref(
                    _rejected_envelope(exc.code, operation),
                    proposal_ref,
                    self._proposal_url(proposal_id),
                )
            )

    async def _governance_worker_view(
        self, worker_item_id: UUID
    ) -> GovernanceWorkerItemView:
        return GovernanceWorkerItemView.model_validate(
            await self._client.get_collaboration_governance_worker_item(worker_item_id)
        )

    async def _governance_proposal_view(
        self, proposal_id: UUID
    ) -> GovernanceProposalView:
        return GovernanceProposalView.model_validate(
            await self._client.get_collaboration_governance_proposal(proposal_id)
        )

    async def _worker_item_version(
        self, worker_item_id: UUID, root_task_id: UUID
    ) -> int:
        workspace = await self._client.get_worker_item_workspace(worker_item_id)
        worker = workspace.get("worker_item")
        if not isinstance(worker, Mapping):
            raise TypeError("worker item identity is missing")
        returned_id = _parse_uuid(worker.get("id"))
        returned_root_task_id = _parse_uuid(worker.get("root_task_id"))
        version = _positive_int(worker.get("version"))
        if (
            returned_id != worker_item_id
            or returned_root_task_id != root_task_id
            or version is None
        ):
            raise ValueError("worker item identity is invalid")
        return version

    async def _active_plan_generation(self, root_task_id: UUID) -> int:
        active_plan = await self._client.get_active_plan(root_task_id)
        returned_root = _parse_uuid(active_plan.get("root_task_id"))
        generation = _positive_int(active_plan.get("active_generation"))
        if returned_root != root_task_id or generation is None:
            raise ValueError("active plan identity is invalid")
        return generation

    async def _governance_source_preflight(
        self,
        root_task_id: UUID,
        *,
        resource_ref: str,
        web_url: str,
    ) -> ToolEnvelope | None:
        source = await self._resolve_source()
        if source.source_type == "sidera_web_chat":
            return None
        snapshot = await self._state.snapshot()
        binding = snapshot.resource_sources.get(f"task:{root_task_id}")
        if binding is None:
            return ToolEnvelope(
                outcome="needs_user",
                summary="This task is not bound to the current private source yet.",
                resource_ref=resource_ref,
                web_url=web_url,
                error_code="source_binding_required",
            )
        if (
            binding.source_type != source.source_type
            or binding.source_ref != source.source_ref
        ):
            return ToolEnvelope(
                outcome="conflict",
                summary="This private source cannot govern another task source.",
                resource_ref=resource_ref,
                web_url=web_url,
                error_code="source_binding_conflict",
            )
        return None

    def _governance_principal_configured(self) -> bool:
        return (
            self._config.governance_actor_type is not None
            and self._config.governance_actor_id is not None
        )

    async def _resolve_source(self) -> SourceResolution:
        context = get_current_tool_context()
        if context is None:
            raise MissingToolContext("tool context is missing")
        metadata: Mapping[str, object] = {}
        if self._session_metadata_provider is not None:
            metadata = self._session_metadata_provider(context.origin_session_key)
        snapshot = await self._state.snapshot()
        result = self._source_resolver.resolve(
            context,
            metadata,
            existing_routes=snapshot.source_routes,
        )
        if result.private_route is not None:
            await self._state.save_route(result.source_ref, result.private_route)
        return result

    def _planning_url(self, request_id: UUID) -> str:
        return (
            f"{str(self._config.web_base_url).rstrip('/')}/workspaces/"
            f"{self._config.workspace_id}/planning/{request_id}"
        )

    def _revision_url(self, request_id: UUID, root_task_id: UUID | None) -> str:
        if root_task_id is None:
            return self._planning_url(request_id)
        return (
            f"{str(self._config.web_base_url).rstrip('/')}/workspaces/"
            f"{self._config.workspace_id}/tasks/{root_task_id}/revisions/{request_id}"
        )

    def _task_url(self, task_id: UUID) -> str:
        return (
            f"{str(self._config.web_base_url).rstrip('/')}/workspaces/"
            f"{self._config.workspace_id}/tasks/{task_id}"
        )

    def _worker_item_url(self, worker_item_id: UUID) -> str:
        return (
            f"{str(self._config.web_base_url).rstrip('/')}/workspaces/"
            f"{self._config.workspace_id}/worker-items/{worker_item_id}"
        )

    def _proposal_url(self, proposal_id: UUID, deep_link: str | None = None) -> str:
        if deep_link is not None and deep_link.startswith("/"):
            return f"{str(self._config.web_base_url).rstrip('/')}{deep_link}"
        return (
            f"{str(self._config.web_base_url).rstrip('/')}/workspaces/"
            f"{self._config.workspace_id}/collaboration-governance/proposals/{proposal_id}"
        )


def _revision_brief(
    goal: str, constraints: list[str], acceptance_criteria: list[str]
) -> PlanRevisionBrief | None:
    normalized_constraints = tuple(_clean_text_values(constraints))
    normalized_acceptance = tuple(_clean_text_values(acceptance_criteria))
    if not _non_empty_text(goal) or not normalized_acceptance:
        return None
    try:
        return PlanRevisionBrief(
            objective=goal.strip(),
            constraints=normalized_constraints,
            acceptance_criteria=normalized_acceptance,
        )
    except ValueError:
        return None


def _active_plan_base(
    active_plan: Mapping[str, JsonValue], task_id: UUID
) -> dict[str, JsonValue] | None:
    returned_task_id = _parse_uuid(active_plan.get("root_task_id"))
    snapshot_id = _parse_uuid(active_plan.get("active_snapshot_id"))
    generation = _positive_int(active_plan.get("active_generation"))
    content_hash = _string(active_plan.get("active_snapshot_hash"))
    if (
        returned_task_id is not None
        and returned_task_id != task_id
    ) or snapshot_id is None or generation is None or content_hash is None:
        return None
    return {
        "expected_base_snapshot_id": str(snapshot_id),
        "expected_base_generation": generation,
        "expected_base_content_hash": content_hash,
    }


def _revision_root_id(
    *sources: Mapping[str, JsonValue],
) -> UUID | None:
    for source in sources:
        candidates: list[object] = [source]
        for key in ("revision_base", "revision_freshness", "base"):
            nested = source.get(key)
            if isinstance(nested, Mapping):
                candidates.append(nested)
                nested_base = nested.get("base")
                if isinstance(nested_base, Mapping):
                    candidates.append(nested_base)
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                root_task_id = _parse_uuid(candidate.get("root_task_id"))
                if root_task_id is not None:
                    return root_task_id
    return None


def _revision_confirmation(
    proposal_revision: int,
    proposal_content_hash: str,
    diff_hash: str,
    base_snapshot_id: str,
    base_content_hash: str,
) -> PlanRevisionConfirmation | None:
    snapshot_id = _parse_uuid(base_snapshot_id)
    if (
        snapshot_id is None
        or proposal_revision < 1
        or not _non_empty_text(proposal_content_hash)
        or not _non_empty_text(diff_hash)
        or not _non_empty_text(base_content_hash)
    ):
        return None
    try:
        return PlanRevisionConfirmation(
            proposal_revision=proposal_revision,
            proposal_content_hash=proposal_content_hash.strip(),
            diff_hash=diff_hash.strip(),
            base_snapshot_id=snapshot_id,
            base_content_hash=base_content_hash.strip(),
        )
    except ValueError:
        return None


def _revision_kind_preflight(
    detail: Mapping[str, JsonValue],
) -> ToolEnvelope | None:
    kind = _string(detail.get("kind"))
    if kind is not None and kind.lower() != "revision":
        return ToolEnvelope(
            outcome="failed",
            summary="The planning request is not a plan revision.",
            error_code="not_plan_revision",
        )
    return None


def _revision_version_preflight(
    detail: Mapping[str, JsonValue], expected_version: int
) -> ToolEnvelope | None:
    kind_result = _revision_kind_preflight(detail)
    if kind_result is not None:
        return kind_result
    return _version_preflight(detail, expected_version)


def _revision_proposal_preflight(
    detail: Mapping[str, JsonValue],
    expected_version: int,
    expected_revision: int,
    expected_hash: str,
    *,
    expected_profile: tuple[UUID, int] | None = None,
) -> ToolEnvelope | None:
    kind_result = _revision_kind_preflight(detail)
    if kind_result is not None:
        return kind_result
    return _proposal_preflight(
        detail,
        expected_version,
        expected_revision,
        expected_hash,
        expected_profile=expected_profile,
    )


def _revision_fence_preflight(
    confirmation: PlanRevisionConfirmation,
    diff: Mapping[str, JsonValue],
) -> ToolEnvelope | None:
    actual_diff_hash = _string(diff.get("diff_hash"))
    base = diff.get("base")
    if not isinstance(base, Mapping):
        return ToolEnvelope(
            outcome="failed",
            summary="The control plane returned an incomplete revision diff.",
            error_code="invalid_control_plane_response",
        )
    actual_snapshot_id = _parse_uuid(base.get("plan_snapshot_id"))
    actual_base_hash = _string(base.get("plan_content_hash"))
    if (
        actual_diff_hash is None
        or actual_snapshot_id is None
        or actual_base_hash is None
    ):
        return ToolEnvelope(
            outcome="failed",
            summary="The control plane returned an incomplete revision fence.",
            error_code="invalid_control_plane_response",
        )
    if actual_diff_hash != confirmation.diff_hash:
        return ToolEnvelope(
            outcome="conflict",
            summary="The revision diff has changed; reload it before confirming.",
            error_code="diff_conflict",
        )
    if actual_snapshot_id != confirmation.base_snapshot_id:
        return ToolEnvelope(
            outcome="conflict",
            summary="The revision base snapshot has changed; reload it before confirming.",
            error_code="base_conflict",
        )
    if actual_base_hash != confirmation.base_content_hash:
        return ToolEnvelope(
            outcome="conflict",
            summary="The revision base content has changed; reload it before confirming.",
            error_code="base_conflict",
        )
    return None


def _plan_revision_summary(
    detail: Mapping[str, JsonValue],
    diff: Mapping[str, JsonValue],
    materialization: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {}
    for source, target in (
        (detail, "status"),
    ):
        status = _string(source.get(target))
        if status is not None:
            data[target] = status
    version = _positive_int(detail.get("version"))
    if version is not None:
        data["version"] = version
    for key in ("current_brief_revision", "current_proposal_revision"):
        value = _positive_int(detail.get(key))
        if value is not None:
            data[key] = value
    content_hash = _string(detail.get("current_proposal_content_hash"))
    if content_hash is not None:
        data["current_proposal_content_hash"] = content_hash

    base = detail.get("revision_base")
    if isinstance(base, Mapping):
        safe_base = _revision_base_summary(base)
        if safe_base:
            data["base"] = safe_base
    freshness = detail.get("revision_freshness")
    if isinstance(freshness, Mapping):
        safe_freshness: dict[str, JsonValue] = {
            "base_fresh": bool(freshness.get("base_fresh")),
            "stale_reasons": _bounded_strings(
                freshness.get("stale_reasons"), limit=10
            ),
        }
        current_generation = _positive_int(freshness.get("current_plan_generation"))
        current_root_version = _positive_int(
            freshness.get("current_root_task_version")
        )
        if current_generation is not None:
            safe_freshness["current_plan_generation"] = current_generation
        if current_root_version is not None:
            safe_freshness["current_root_task_version"] = current_root_version
        data["freshness"] = safe_freshness

    if diff:
        data["diff"] = _revision_diff_summary(diff)
    safe_materialization: dict[str, JsonValue] = {}
    for key in (
        "planning_request_id",
        "proposal_revision_id",
        "root_task_id",
        "plan_snapshot_id",
        "plan_generation",
        "plan_transition_id",
    ):
        value = materialization.get(key)
        if isinstance(value, (str, int)):
            safe_materialization[key] = value
    if safe_materialization:
        data["materialization"] = safe_materialization
    return data


def _revision_base_summary(base: Mapping[str, object]) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {}
    for source, target in (
        ("root_task_id", "root_task_id"),
        ("plan_snapshot_id", "plan_snapshot_id"),
        ("plan_content_hash", "plan_content_hash"),
    ):
        value = base.get(source)
        if isinstance(value, str):
            data[target] = value
    for source, target in (
        ("plan_generation", "plan_generation"),
        ("root_task_version", "root_task_version"),
    ):
        value = _positive_int(base.get(source))
        if value is not None:
            data[target] = value
    return data


def _revision_diff_summary(diff: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {}
    for key in ("diff_hash", "proposal_content_hash"):
        value = _string(diff.get(key))
        if value is not None:
            data[key] = value
    target = diff.get("target_descriptor_preview")
    if isinstance(target, Mapping):
        generation = _positive_int(target.get("target_generation"))
        if generation is not None:
            data["target_generation"] = generation
    base = diff.get("base")
    if isinstance(base, Mapping):
        safe_base = _revision_base_summary(base)
        if safe_base:
            data["base"] = safe_base
    impact = diff.get("impact_counts")
    if isinstance(impact, Mapping):
        safe_impact: dict[str, JsonValue] = {}
        for key in (
            "preserve",
            "move",
            "patch",
            "continue",
            "create",
            "derive",
            "retire",
            "affected_branches",
        ):
            value = _count(impact.get(key))
            safe_impact[key] = value
        data["impact_counts"] = safe_impact
    risk_flags = _bounded_strings(diff.get("risk_flags"), limit=20)
    if risk_flags:
        data["risk_flags"] = risk_flags
    branch_paths = _bounded_strings(diff.get("affected_branch_paths"), limit=40)
    if branch_paths:
        data["affected_branch_paths"] = branch_paths
    for source, target in (
        ("subtask_changes", "subtasks"),
        ("worker_item_changes", "worker_items"),
        ("transition_preview", "transition_preview"),
    ):
        changes = _safe_revision_changes(diff.get(source))
        if changes:
            data[target] = changes
    policy = diff.get("accepted_transition_policy")
    if isinstance(policy, Mapping):
        safe_policy: dict[str, JsonValue] = {}
        for key in ("failure_policy", "budget_level", "revision_operation"):
            value = _string(policy.get(key))
            if value is not None:
                safe_policy[key] = value
        for key in ("activate_immediately", "manual_gate"):
            if isinstance(policy.get(key), bool):
                safe_policy[key] = bool(policy.get(key))
        if safe_policy:
            data["accepted_transition_policy"] = safe_policy
    return data


def _safe_revision_changes(value: object) -> list[JsonValue]:
    if not isinstance(value, list | tuple):
        return []
    result: list[JsonValue] = []
    for item in value[:40]:
        if not isinstance(item, Mapping):
            continue
        entry: dict[str, JsonValue] = {}
        for key in (
            "path",
            "strategy",
            "reason",
            "context_disposition",
            "fallback_action",
        ):
            text = _string(item.get(key))
            if text is not None:
                entry[key] = text
        for key in ("target_worker_item_key", "target_subtask_key"):
            text = _string(item.get(key))
            if text is not None:
                entry[key] = text
        actions = _bounded_strings(item.get("allowed_actions"), limit=10)
        if actions:
            entry["allowed_actions"] = actions
        reasons = _bounded_strings(item.get("reason_codes"), limit=10)
        if reasons:
            entry["reason_codes"] = reasons
        if entry:
            result.append(entry)
    return result


def _revision_read_outcome(
    status: str,
    detail: Mapping[str, JsonValue],
    diff: Mapping[str, JsonValue],
) -> tuple[Literal["ok", "needs_user", "conflict", "failed"], str]:
    freshness = detail.get("revision_freshness")
    if isinstance(freshness, Mapping) and freshness.get("base_fresh") is False:
        return "conflict", "The revision base is stale; review the latest plan before acting."
    normalized = status.lower()
    if normalized in {"conflicted", "stale"}:
        return "conflict", "The plan revision is stale and needs a fresh review."
    if normalized in {
        "proposal_ready",
        "awaiting_confirmation",
        "needs_confirmation",
    }:
        if not diff:
            return "failed", "The revision proposal diff is not available yet."
        return "needs_user", "The plan revision is ready for explicit user confirmation."
    if normalized in {"failed", "blocked", "cancelled"}:
        return "failed", "The plan revision reached a terminal state that needs review."
    return "ok", "Plan revision status loaded."


def _safe_mutation_data(result: DispatchResult) -> dict[str, JsonValue]:
    data = _result_data(result)
    return {
        key: data[key]
        for key in (
            "planning_request_id",
            "planning_run_id",
            "brief_revision",
            "root_task_id",
            "base_snapshot_id",
            "base_generation",
            "plan_snapshot_id",
            "plan_generation",
            "plan_transition_id",
            "proposal_revision",
            "diff_hash",
            "status",
        )
        if key in data and isinstance(data[key], (str, int))
    }


def _plan_transition_summary(
    transition: Mapping[str, JsonValue], transition_id: UUID
) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {"plan_transition_id": str(transition_id)}
    for key in ("from_generation", "to_generation", "version"):
        value = _positive_int(transition.get(key))
        if value is not None:
            data[key] = value
    status = _string(transition.get("status")) or "unknown"
    data["status"] = status
    root = transition.get("root_task")
    if isinstance(root, Mapping):
        root_id = _parse_uuid(root.get("id"))
        if root_id is not None:
            data["root_task_id"] = str(root_id)
        root_path = _string(root.get("path"))
        if root_path is not None:
            data["root_path"] = root_path

    workers = transition.get("workers")
    safe_workers: list[JsonValue] = []
    if isinstance(workers, list | tuple):
        for item in workers[:100]:
            if not isinstance(item, Mapping):
                continue
            worker: dict[str, JsonValue] = {}
            worker_id = _parse_uuid(item.get("worker_transition_id"))
            if worker_id is not None:
                worker["worker_transition_id"] = str(worker_id)
            for key in ("path", "action", "fallback_action", "status", "failure_code"):
                value = _string(item.get(key))
                if value is not None:
                    worker[key] = value
            for key in ("source_execution_id", "target_execution_id"):
                value = _parse_uuid(item.get(key))
                if value is not None:
                    worker[key] = str(value)
            context = item.get("context")
            if isinstance(context, Mapping):
                safe_context: dict[str, JsonValue] = {}
                for key in (
                    "current_context_version",
                    "acknowledged_context_version",
                    "target_context_version",
                ):
                    value = _positive_int(context.get(key))
                    if value is not None:
                        safe_context[key] = value
                if isinstance(context.get("patch_acknowledged"), bool):
                    safe_context["patch_acknowledged"] = bool(
                        context.get("patch_acknowledged")
                    )
                if safe_context:
                    worker["context"] = safe_context
            disposition = item.get("queued_input_disposition")
            if isinstance(disposition, Mapping):
                worker["queued_input_count"] = _count(disposition.get("queued_count"))
                worker["superseded_input_count"] = _count(
                    disposition.get("superseded_count")
                )
            dependencies = item.get("blocked_dependencies")
            worker["blocked_dependency_count"] = (
                len(dependencies) if isinstance(dependencies, list | tuple) else 0
            )
            if worker:
                safe_workers.append(worker)
    data["workers"] = safe_workers
    data["worker_count"] = len(safe_workers)
    data["blocked_unknown_count"] = sum(
        isinstance(item, Mapping) and item.get("status") == "blocked_unknown"
        for item in safe_workers
    )
    blocked_dependencies = transition.get("blocked_dependencies")
    data["blocked_dependency_count"] = (
        len(blocked_dependencies)
        if isinstance(blocked_dependencies, list | tuple)
        else 0
    )
    return data


def _governance_read_envelope(
    view: GovernanceWorkerItemView, *, web_url: str
) -> ToolEnvelope:
    data: dict[str, JsonValue] = {
        "root_task_id": str(view.root_task_id),
        "subtask_id": str(view.subtask_id) if view.subtask_id is not None else None,
        "subtask_key": view.subtask_key,
        "subtask_title": view.subtask_title[:200],
        "worker_item_id": str(view.worker_item_id),
        "worker_item_key": view.worker_item_key,
        "worker_item_title": view.worker_item_title[:200],
        "worker_item_role": view.worker_item_role[:100],
        "worker_item_responsibility": view.worker_item_responsibility[:500],
        "path": (
            f"Task/{view.root_task_id} > Subtask/{view.subtask_key} > "
            f"WorkerItem/{view.worker_item_key}"
        ),
    }
    governance = _governance_mapping(view.governance)
    safe_governance: dict[str, JsonValue] = {}
    if isinstance(governance.get("enabled"), bool):
        safe_governance["enabled"] = bool(governance["enabled"])
    for key in (
        "state",
        "waiting_summary",
        "governance_mode",
        "release_state",
        "attention_state",
        "proposal_summary",
    ):
        value = _string(governance.get(key), limit=2000)
        if value is not None:
            safe_governance[key] = value
    for key in ("active_hold_id",):
        value = _parse_uuid(governance.get(key))
        if value is not None:
            safe_governance[key] = str(value)
    for key in ("hold_resource_version",):
        value = _positive_int(governance.get(key))
        if value is not None:
            safe_governance[key] = value
    actions = governance.get("available_actions")
    if isinstance(actions, list | tuple):
        safe_actions: list[JsonValue] = []
        for action in actions:
            if isinstance(action, str) and action in _GOVERNANCE_ACTIONS:
                safe_actions.append(action)
            if len(safe_actions) == 4:
                break
        safe_governance["available_actions"] = safe_actions
    proposal = governance.get("active_proposal")
    if isinstance(proposal, Mapping):
        safe_proposal: dict[str, JsonValue] = {}
        proposal_id = _parse_uuid(proposal.get("proposal_id"))
        if proposal_id is not None:
            safe_proposal["proposal_id"] = str(proposal_id)
        for key in ("status", "reason_code"):
            value = _string(proposal.get(key), limit=200)
            if value is not None:
                safe_proposal[key] = value
        summary = _string(proposal.get("summary"), limit=2000)
        if summary is not None:
            safe_proposal["summary"] = summary
        for key in ("resource_version", "target_request_version"):
            value = _positive_int(proposal.get(key))
            if value is not None:
                safe_proposal[key] = value
        for key in ("created_at", "resolved_at"):
            value = _string(proposal.get(key), limit=64)
            if value is not None:
                safe_proposal[key] = value
        if safe_proposal:
            safe_governance["active_proposal"] = safe_proposal
    history = governance.get("governance_history")
    if isinstance(history, list | tuple):
        safe_history: list[JsonValue] = []
        for item in history[:20]:
            if not isinstance(item, Mapping):
                continue
            safe_item: dict[str, JsonValue] = {}
            for key in ("kind", "status", "action", "result"):
                value = _string(item.get(key), limit=100)
                if value is not None:
                    safe_item[key] = value
            for key in ("resource_version",):
                value = _positive_int(item.get(key))
                if value is not None:
                    safe_item[key] = value
            occurred_at = _string(item.get("occurred_at"), limit=64)
            if occurred_at is not None:
                safe_item["occurred_at"] = occurred_at
            if safe_item:
                safe_history.append(safe_item)
        if safe_history:
            safe_governance["governance_history"] = safe_history
    data["governance"] = safe_governance
    return ToolEnvelope(
        outcome="ok",
        summary="Collaboration governance status loaded.",
        resource_ref=f"worker-item:{view.worker_item_id}",
        web_url=web_url,
        data=data,
    )


def _governance_mapping(value: object) -> Mapping[str, JsonValue]:
    return value if isinstance(value, Mapping) else {}


def _worker_view_preflight(
    view: GovernanceWorkerItemView,
    expected_worker_item_id: UUID,
    expected_workspace_id: UUID,
) -> ToolEnvelope | None:
    if (
        view.workspace_id != expected_workspace_id
        or view.worker_item_id != expected_worker_item_id
        or view.root_task_id is None
    ):
        return ToolEnvelope(
            outcome="failed",
            summary="The control plane returned an invalid WorkerItem lineage.",
            error_code="invalid_control_plane_response",
        )
    if not isinstance(view.governance, Mapping):
        return ToolEnvelope(
            outcome="failed",
            summary="The control plane returned an incomplete governance view.",
            error_code="invalid_control_plane_response",
        )
    return None


def _proposal_view_preflight(
    view: GovernanceProposalView,
    expected_proposal_id: UUID,
    expected_workspace_id: UUID,
) -> ToolEnvelope | None:
    if (
        view.workspace_id != expected_workspace_id
        or view.proposal_id != expected_proposal_id
        or view.root_task_id is None
        or view.worker_item_id is None
    ):
        return ToolEnvelope(
            outcome="failed",
            summary="The control plane returned an invalid Proposal lineage.",
            error_code="invalid_control_plane_response",
        )
    return None


def _available_action_preflight(
    view: GovernanceWorkerItemView | GovernanceProposalView,
    action: str,
) -> ToolEnvelope | None:
    if isinstance(view, GovernanceWorkerItemView):
        raw_actions = _governance_mapping(view.governance).get("available_actions")
    else:
        raw_actions = view.available_actions
    actions = raw_actions if isinstance(raw_actions, (list, tuple)) else ()
    if action not in actions:
        return ToolEnvelope(
            outcome="conflict",
            summary="The requested governance action is no longer available.",
            error_code="governance_action_unavailable",
        )
    return None


def _active_proposal_id(governance: Mapping[str, JsonValue]) -> UUID | None:
    proposal = governance.get("active_proposal")
    if not isinstance(proposal, Mapping):
        return None
    return _parse_uuid(proposal.get("proposal_id"))


def _valid_positive_int(value: object) -> bool:
    return _positive_int(value) is not None


def _governance_input_preflight(
    worker_item_version: object, target_plan_generation: object
) -> ToolEnvelope | None:
    if not _valid_positive_int(worker_item_version) or not _valid_positive_int(
        target_plan_generation
    ):
        return _invalid_governance_input()
    return None


def _governance_reason(value: object) -> tuple[str | None, ToolEnvelope | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, _invalid_governance_input()
    normalized = value.strip()
    if len(normalized) > 2000:
        return None, _invalid_governance_input()
    return normalized or None, None


def _invalid_governance_input() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="failed",
        summary="The governance command arguments are invalid.",
        error_code="invalid_input",
    )


def _governance_unavailable() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="unavailable",
        summary="Governance writes are unavailable until the local principal is configured.",
        error_code="governance_principal_unavailable",
    )


def _explicit_confirmation_required() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="needs_user",
        summary="Explicit user confirmation is required before approving a release.",
        error_code="explicit_confirmation_required",
    )


def _governance_source_context_error() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="failed",
        summary="The request source could not be identified safely.",
        error_code="missing_tool_context",
    )


def _governance_invalid_control_plane(resource_ref: str, web_url: str) -> ToolEnvelope:
    return _with_governance_ref(
        ToolEnvelope(
            outcome="failed",
            summary="The control plane returned an invalid governance view.",
            error_code="invalid_control_plane_response",
        ),
        resource_ref,
        web_url,
    )


def _governance_version_conflict(resource_name: str) -> ToolEnvelope:
    return ToolEnvelope(
        outcome="conflict",
        summary=f"The {resource_name} has changed; reload the latest governance status.",
        error_code="version_conflict",
    )


def _governance_generation_conflict() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="conflict",
        summary="The plan generation has changed; reload the current WorkerItem status.",
        error_code="generation_conflict",
    )


def _governance_hold_conflict() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="conflict",
        summary="The requested governance Hold is no longer current.",
        error_code="hold_conflict",
    )


def _governance_proposal_not_pending() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="conflict",
        summary="The release Proposal is no longer pending.",
        error_code="proposal_not_pending",
    )


def _governance_lineage_conflict() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="conflict",
        summary="The release Proposal is no longer attached to the current WorkerItem Hold.",
        error_code="governance_lineage_conflict",
    )


def _with_governance_ref(
    envelope: ToolEnvelope, resource_ref: str, web_url: str
) -> ToolEnvelope:
    return envelope.model_copy(update={"resource_ref": resource_ref, "web_url": web_url})


def _governance_rejected(
    error: ControlPlaneRejected, operation: str
) -> ToolEnvelope:
    if error.code == "not_found":
        return ToolEnvelope(
            outcome="failed",
            summary=f"The requested {operation} resource was not found.",
            error_code="not_found",
        )
    return _rejected_envelope(error.code, operation)


def _governance_mutation_result(
    result: DispatchResult,
    *,
    operation: str,
    resource_ref: str,
    web_url: str,
    allowed_data: tuple[str, ...],
    warning: str | None = None,
) -> ToolEnvelope:
    if result.status != "applied":
        return _with_governance_ref(
            _dispatch_envelope(result, operation), resource_ref, web_url
        )
    data = _result_data(result)
    safe_data: dict[str, JsonValue] = {}
    for key in allowed_data:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)):
            safe_data[key] = value
    if warning is not None:
        safe_data["warning"] = warning
    return ToolEnvelope(
        outcome="ok",
        summary=(
            f"The {operation} was accepted. {warning}"
            if warning is not None
            else f"The {operation} was accepted."
        ),
        resource_ref=resource_ref,
        web_url=web_url,
        data=safe_data,
    )


def _encode(envelope: ToolEnvelope) -> str:
    return envelope.model_dump_json()


def _non_empty_text(value: str) -> bool:
    return bool(value.strip())


def _clean_texts(values: list[str]) -> list[JsonValue]:
    return [value.strip() for value in values if value.strip()]


def _clean_text_values(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def _parse_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return UUID(value.strip())
    except ValueError:
        return None


def _string(value: object, *, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _bounded_strings(value: object, *, limit: int) -> list[JsonValue]:
    if not isinstance(value, list | tuple):
        return []
    result: list[JsonValue] = []
    for item in value[:limit]:
        text = _string(item)
        if text is not None:
            result.append(text)
    return result


def _planning_summary(
    detail: Mapping[str, JsonValue],
    clarifications: tuple[dict[str, JsonValue], ...],
    proposal: Mapping[str, JsonValue],
    materialization: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {}
    status = _string(detail.get("status")) or "unknown"
    data["status"] = status
    data["version"] = _count(detail.get("version"))
    for key in (
        "current_brief_revision",
        "current_proposal_revision",
    ):
        value = _positive_int(detail.get(key))
        if value is not None:
            data[key] = value
    content_hash = _string(detail.get("current_proposal_content_hash"))
    if content_hash is not None:
        data["current_proposal_content_hash"] = content_hash

    questions: list[JsonValue] = []
    for item in clarifications[:20]:
        question = _string(item.get("question"))
        if question is None:
            continue
        entry: dict[str, JsonValue] = {"question": question}
        item_id = _parse_uuid(item.get("id"))
        if item_id is not None:
            entry["id"] = str(item_id)
        rationale = _string(item.get("rationale"))
        if rationale is not None:
            entry["rationale"] = rationale
        entry["blocking"] = bool(item.get("blocking", True))
        options = _bounded_strings(item.get("options"), limit=10)
        if options:
            entry["options"] = options
        questions.append(entry)
    data["blocking_questions"] = questions

    display = proposal.get("display")
    if isinstance(display, Mapping):
        root = display.get("root_task")
        if isinstance(root, Mapping):
            root_title = _string(root.get("title"))
            if root_title is not None:
                data["root_title"] = root_title
        data["subtask_count"] = _sequence_count(display.get("subtasks"))
        data["worker_item_count"] = _sequence_count(display.get("worker_items"))
        data["dependency_count"] = _sequence_count(
            display.get("subtask_dependencies")
        ) + _sequence_count(display.get("worker_item_dependencies"))
        decision = display.get("decision_summary")
        if isinstance(decision, Mapping):
            data["risks"] = _bounded_strings(decision.get("risks"), limit=10)
            budget: dict[str, JsonValue] = {}
            for key in (
                "estimated_cost_microunits",
                "estimated_runtime_seconds",
                "estimated_concurrency",
            ):
                value = _count(decision.get(key))
                budget[key] = value
            impact = display.get("impact_policy")
            if isinstance(impact, Mapping):
                level = _string(impact.get("budget_level"))
                if level is not None:
                    budget["level"] = level
            data["budget"] = budget

    task_id = _parse_uuid(materialization.get("root_task_id"))
    if task_id is not None:
        data["task_id"] = str(task_id)
    return data


def _sequence_count(value: object) -> int:
    return len(value) if isinstance(value, list | tuple) else 0


def _task_summary(overview: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {}
    for source, target in (
        ("title", "title"),
        ("goal_summary", "goal"),
        ("current_milestone_key", "milestone"),
    ):
        value = _string(overview.get(source))
        if value is not None:
            data[target] = value
        elif source == "current_milestone_key":
            data[target] = None
    data["counts"] = {
        "materialized": _count(overview.get("materialized_count")),
        "planned": _count(overview.get("planned_count")),
        "running": _count(overview.get("running_count")),
        "completed": _count(overview.get("completed_count")),
        "failed": _count(overview.get("failed_count")),
    }
    data["next_keys"] = _bounded_strings(
        overview.get("next_work_item_keys"), limit=20
    )
    attention: list[JsonValue] = []
    raw_attention = overview.get("attention_items")
    if isinstance(raw_attention, list | tuple):
        for item in raw_attention[:10]:
            if not isinstance(item, Mapping):
                continue
            entry: dict[str, JsonValue] = {}
            for key in ("key", "kind", "title", "summary"):
                value = _string(item.get(key))
                if value is not None:
                    entry[key] = value
            if entry:
                attention.append(entry)
    data["attention"] = attention
    return data


def _result_data(result: DispatchResult) -> dict[str, JsonValue]:
    execution = result.execution
    if isinstance(execution, CommandExecution) and execution.result is not None:
        return dict(execution.result.data)
    if isinstance(execution, CommandStatus) and execution.result is not None:
        return dict(execution.result.data)
    return {}


def _result_uuid(result: DispatchResult, key: str) -> UUID | None:
    value = _result_data(result).get(key)
    if value is not None:
        parsed = _parse_uuid(value)
        if parsed is not None:
            return parsed
    execution = result.execution
    if isinstance(execution, CommandExecution) and execution.result is not None:
        return execution.result.aggregate_id
    if isinstance(execution, CommandStatus) and execution.result is not None:
        return execution.result.aggregate_id
    return None


def _dispatch_envelope(result: DispatchResult, operation: str) -> ToolEnvelope:
    if result.status == "unavailable":
        return ToolEnvelope(
            outcome="unavailable",
            summary=f"The SIDERA control plane is unavailable during {operation}.",
            error_code=result.error_code or "service_unavailable",
        )
    if result.status == "result_unknown":
        return ToolEnvelope(
            outcome="result_unknown",
            summary=f"The {operation} result is pending confirmation.",
            error_code=result.error_code or "result_unknown",
        )
    return _rejected_envelope(result.error_code or "command_rejected", operation)


def _rejected_envelope(error_code: str, operation: str) -> ToolEnvelope:
    if error_code in _CONFLICT_CODES:
        outcome = "conflict"
        summary = f"The {operation} conflicts with a newer control-plane state."
    elif error_code in _UNAVAILABLE_CODES:
        outcome = "unavailable"
        summary = f"The SIDERA control plane is unavailable during {operation}."
    else:
        outcome = "failed"
        summary = f"The {operation} was rejected by the control plane."
    return ToolEnvelope(outcome=outcome, summary=summary, error_code=error_code)


def _invalid_resource(resource_name: str) -> ToolEnvelope:
    return ToolEnvelope(
        outcome="failed",
        summary=f"The {resource_name} reference is invalid.",
        error_code="invalid_resource_id",
    )


def _unavailable_error() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="unavailable",
        summary="The SIDERA control plane is temporarily unavailable.",
        error_code="service_unavailable",
    )


def _normalize_answers(
    answers: list[dict[str, str]],
) -> list[JsonValue] | None:
    normalized: list[JsonValue] = []
    seen: set[UUID] = set()
    for item in answers:
        clarification_id = _parse_uuid(item.get("clarification_id"))
        answer = item.get("answer")
        if clarification_id is None or not isinstance(answer, str) or not answer.strip():
            return None
        if clarification_id in seen:
            return None
        seen.add(clarification_id)
        normalized.append(
            {
                "clarification_id": str(clarification_id),
                "answer": answer.strip(),
            }
        )
    return normalized or None


def _version_preflight(
    detail: Mapping[str, JsonValue], expected_version: int
) -> ToolEnvelope | None:
    actual_version = detail.get("version")
    if isinstance(actual_version, bool) or not isinstance(actual_version, int):
        return ToolEnvelope(
            outcome="failed",
            summary="The control plane returned an invalid planning version.",
            error_code="invalid_control_plane_response",
        )
    if actual_version != expected_version:
        return ToolEnvelope(
            outcome="conflict",
            summary="The planning request has changed; reload the latest status.",
            error_code="version_conflict",
        )
    return None


def _proposal_preflight(
    detail: Mapping[str, JsonValue],
    expected_version: int,
    expected_revision: int,
    expected_hash: str,
    *,
    expected_profile: tuple[UUID, int] | None = None,
) -> ToolEnvelope | None:
    version_result = _version_preflight(detail, expected_version)
    if version_result is not None:
        return version_result
    actual_revision = detail.get("current_proposal_revision")
    actual_hash = detail.get("current_proposal_content_hash")
    if (
        isinstance(actual_revision, bool)
        or not isinstance(actual_revision, int)
        or actual_revision != expected_revision
        or not isinstance(actual_hash, str)
        or actual_hash != expected_hash.strip()
    ):
        return ToolEnvelope(
            outcome="conflict",
            summary="The proposal has changed; reload the latest proposal before acting.",
            error_code="proposal_conflict",
        )
    if expected_profile is not None:
        actual_profile = _parse_uuid(detail.get("planning_profile_id"))
        actual_profile_version = detail.get("planning_profile_version")
        if (
            actual_profile is None
            or actual_profile != expected_profile[0]
            or isinstance(actual_profile_version, bool)
            or not isinstance(actual_profile_version, int)
            or actual_profile_version != expected_profile[1]
        ):
            return ToolEnvelope(
                outcome="conflict",
                summary="The planning profile has changed; reload the latest status.",
                error_code="profile_conflict",
            )
    return None
