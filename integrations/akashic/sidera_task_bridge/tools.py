from collections.abc import Callable, Mapping
from typing import Any, Literal, cast
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


async def resolve_source_context(
    state: BridgeStateStore,
    source_resolver: SourceContextResolver,
    session_metadata_provider: Callable[[str], Mapping[str, object]] | None = None,
) -> SourceResolution:
    """Resolve host provenance once and persist only the local route mapping."""

    context = get_current_tool_context()
    if context is None:
        raise MissingToolContext("tool context is missing")
    metadata: Mapping[str, object] = {}
    if session_metadata_provider is not None:
        supplied = session_metadata_provider(context.origin_session_key)
        if isinstance(supplied, Mapping):
            metadata = supplied
    snapshot = await state.snapshot()
    result = source_resolver.resolve(
        context,
        metadata,
        existing_routes=snapshot.source_routes,
    )
    if result.private_route is not None:
        await state.save_route(result.source_ref, result.private_route)
    return result


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

    async def sidera_get_worker_result_inputs(
        self,
        event: object,
        worker_item_id: str,
        execution_request_id: str | None = None,
    ) -> str:
        """Read bounded result-input provenance and delivery state for one WorkerItem."""

        del event
        parsed_worker_item_id = _parse_uuid(worker_item_id)
        if parsed_worker_item_id is None:
            return _encode(_invalid_resource("WorkerItem"))
        parsed_request_id: UUID | None = None
        if execution_request_id is not None:
            parsed_request_id = _parse_uuid(execution_request_id)
            if parsed_request_id is None:
                return _encode(_invalid_resource("execution request"))
        web_url = self._worker_item_url(parsed_worker_item_id)
        resource_ref = f"worker-item:{parsed_worker_item_id}"
        try:
            raw_view = await self._client.get_worker_result_inputs(
                parsed_worker_item_id, parsed_request_id
            )
            data = _result_input_summary(
                raw_view,
                expected_workspace_id=self._config.workspace_id,
                expected_worker_item_id=parsed_worker_item_id,
            )
            if data is None:
                return _encode(
                    _with_governance_ref(
                        ToolEnvelope(
                            outcome="failed",
                            summary="The control plane returned an invalid result-input view.",
                            error_code="invalid_control_plane_response",
                        ),
                        resource_ref,
                        web_url,
                    )
                )
            return _encode(
                ToolEnvelope(
                    outcome="ok",
                    summary=_result_input_summary_text(data),
                    resource_ref=resource_ref,
                    web_url=web_url,
                    data=data,
                )
            )
        except TransportUnavailable:
            return _encode(_with_governance_ref(_unavailable_error(), resource_ref, web_url))
        except ControlPlaneRejected as exc:
            return _encode(
                _with_governance_ref(
                    _rejected_envelope(exc.code, "WorkerItem result inputs"),
                    resource_ref,
                    web_url,
                )
            )
        except (TypeError, ValueError):
            return _encode(
                _with_governance_ref(
                    ToolEnvelope(
                        outcome="failed",
                        summary="The control plane returned an invalid result-input view.",
                        error_code="invalid_control_plane_response",
                    ),
                    resource_ref,
                    web_url,
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
            if action == "approve":
                worker_selection_error = _worker_proposal_selection_preflight(
                    worker, proposal
                )
                if worker_selection_error is not None:
                    return _encode(worker_selection_error)
                selection_error = await self._approval_selection_preflight(proposal)
                if selection_error is not None:
                    return _encode(selection_error)
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
            if action == "approve" and proposal.input_selection_id is not None:
                payload["expected_input_selection_id"] = (
                    str(proposal.input_selection_id)
                )
                payload["expected_input_selection_hash"] = proposal.input_selection_hash
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

    async def _approval_selection_preflight(
        self, proposal: GovernanceProposalView
    ) -> ToolEnvelope | None:
        if proposal.input_selection_id is None:
            return None
        try:
            selection = await self._client.get_input_selection(
                proposal.input_selection_id
            )
        except ControlPlaneRejected as exc:
            if exc.code == "not_found":
                return _governance_selection_conflict()
            raise
        return _input_selection_preflight(
            selection,
            expected_workspace_id=self._config.workspace_id,
            expected_root_task_id=proposal.root_task_id,
            expected_worker_item_id=proposal.worker_item_id,
            expected_request_version=proposal.target_request_version,
            expected_selection_id=proposal.input_selection_id,
            expected_selection_hash=proposal.input_selection_hash,
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
        return await resolve_source_context(
            self._state,
            self._source_resolver,
            self._session_metadata_provider,
        )

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
        input_selection_id = _parse_uuid(proposal.get("input_selection_id"))
        input_selection_hash = _selection_hash(proposal.get("input_selection_hash"))
        if input_selection_id is not None and input_selection_hash is not None:
            safe_proposal["input_selection_id"] = str(input_selection_id)
            safe_proposal["input_selection_hash"] = input_selection_hash
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


def _result_input_summary(
    raw: object,
    *,
    expected_workspace_id: UUID,
    expected_worker_item_id: UUID,
) -> dict[str, JsonValue] | None:
    """Project the Control Plane result-input view into safe adapter metadata."""

    if not isinstance(raw, Mapping):
        return None
    workspace_id = _parse_uuid(raw.get("workspace_id"))
    root_task_id = _parse_uuid(raw.get("root_task_id"))
    worker_item_id = _parse_uuid(raw.get("worker_item_id"))
    schema_version = _positive_int(raw.get("schema_version"))
    worker_item_key = _string(raw.get("worker_item_key"), limit=200)
    worker_item_title = _string(raw.get("worker_item_title"), limit=300)
    if (
        workspace_id != expected_workspace_id
        or worker_item_id != expected_worker_item_id
        or root_task_id is None
        or schema_version is None
        or worker_item_key is None
        or worker_item_title is None
    ):
        return None

    request_value = raw.get("request")
    request: dict[str, JsonValue] | None = None
    subtask_key: str | None = None
    if request_value is not None:
        if not isinstance(request_value, Mapping):
            return None
        request_id = _parse_uuid(request_value.get("execution_request_id"))
        request_worker_id = _parse_uuid(request_value.get("worker_item_id"))
        request_key = _string(request_value.get("worker_item_key"), limit=200)
        request_subtask_key = _string(request_value.get("subtask_key"), limit=200)
        request_version = _positive_int(request_value.get("version"))
        status = _string(request_value.get("status"), limit=100)
        gate_mode = _string(request_value.get("collaboration_gate_mode"), limit=100)
        gate_reason = _string(request_value.get("gate_reason"), limit=300)
        if (
            request_id is None
            or request_worker_id != worker_item_id
            or request_key is None
            or request_subtask_key is None
            or request_version is None
            or status is None
            or gate_mode is None
            or gate_reason is None
        ):
            return None
        subtask_key = request_subtask_key
        request = {
            "execution_request_id": str(request_id),
            "worker_item_id": str(request_worker_id),
            "worker_item_key": request_key,
            "subtask_key": request_subtask_key,
            "version": request_version,
            "status": status,
            "collaboration_gate_mode": gate_mode,
            "gate_reason": gate_reason,
        }

    groups_value = raw.get("groups")
    if not isinstance(groups_value, list | tuple):
        return None
    groups: list[JsonValue] = []
    missing: list[JsonValue] = []
    for group_value in groups_value[:32]:
        group = _result_input_group(group_value)
        if group is None:
            return None
        groups.append(group)
        ready_count = cast(int, group["ready_count"])
        required_count = cast(int, group["required_count"])
        if (
            ready_count < required_count
            or group["reason_code"] is not None
        ):
            missing.append(
                {
                    "scope": "input_group",
                    "group_key": group["group_key"],
                    "title": group["title"],
                    "reason_code": group["reason_code"],
                    "missing_artifact_refs": group["missing_artifact_refs"],
                    "missing_evidence_refs": group["missing_evidence_refs"],
                }
            )

    hard_dependencies_value = raw.get("hard_dependencies")
    if not isinstance(hard_dependencies_value, list | tuple):
        return None
    hard_dependencies: list[JsonValue] = []
    for dependency_value in hard_dependencies_value[:32]:
        dependency = _result_input_dependency(dependency_value)
        if dependency is None:
            return None
        hard_dependencies.append(dependency)
        if dependency["ready"] is not True:
            missing.append(
                {
                    "scope": "hard_dependency",
                    "dependency_key": dependency["dependency_key"],
                    "upstream_worker_item_key": dependency[
                        "upstream_worker_item_key"
                    ],
                    "reason_code": dependency["reason_code"],
                    "required_artifact_refs": dependency["required_artifact_refs"],
                    "required_evidence_refs": dependency["required_evidence_refs"],
                }
            )

    governance = _result_input_governance(raw.get("governance"))
    delivery = _result_input_delivery(raw.get("delivery"))
    if governance is None or delivery is None:
        return None
    if delivery["reason_code"] is not None:
        missing.append(
            {
                "scope": "delivery",
                "reason_code": delivery["reason_code"],
                "assembly_status": delivery["assembly_status"],
                "acknowledgement_status": delivery["acknowledgement_status"],
            }
        )

    execution_value = raw.get("execution")
    execution = (
        _result_input_execution(execution_value)
        if execution_value is not None
        else None
    )
    if execution_value is not None and execution is None:
        return None

    actions_value = raw.get("available_actions")
    if not isinstance(actions_value, list | tuple):
        return None
    actions: list[JsonValue] = []
    for action_value in actions_value[:8]:
        action = _result_input_action(action_value)
        if action is not None:
            actions.append(action)

    plan_snapshot_id = _parse_uuid(raw.get("plan_snapshot_id"))
    plan_generation = _positive_int(raw.get("plan_generation"))
    plan_content_hash = _string(raw.get("plan_content_hash"), limit=64)
    historical = raw.get("is_historical")
    if not isinstance(historical, bool):
        return None

    data: dict[str, JsonValue] = {
        "schema_version": schema_version,
        "root_task_id": str(root_task_id),
        "worker_item_id": str(worker_item_id),
        "worker_item_key": worker_item_key,
        "worker_item_title": worker_item_title,
        "subtask_key": subtask_key,
        "is_historical": historical,
        "groups": groups,
        "hard_dependencies": hard_dependencies,
        "governance": governance,
        "delivery": delivery,
        "execution": execution,
        "available_actions": actions,
        "missing": missing,
        "next_action": _result_input_next_action(
            missing=missing,
            governance=governance,
            delivery=delivery,
            execution=execution,
            actions=actions,
            historical=historical,
        ),
    }
    if request is not None:
        data["request"] = request
    if plan_snapshot_id is not None:
        data["plan_snapshot_id"] = str(plan_snapshot_id)
    if plan_generation is not None:
        data["plan_generation"] = plan_generation
    if plan_content_hash is not None:
        data["plan_content_hash"] = plan_content_hash
    return data


def _result_input_group(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    group_key = _string(value.get("group_key"), limit=200)
    title = _string(value.get("title"), limit=300)
    mode = _string(value.get("mode"), limit=100)
    required_count = _non_negative_int(value.get("required_count"))
    ready_count = _non_negative_int(value.get("ready_count"))
    minimum_count = value.get("minimum_count")
    if minimum_count is not None:
        minimum_count = _non_negative_int(minimum_count)
    reason_code = _string(value.get("reason_code"), limit=200)
    if (
        group_key is None
        or title is None
        or mode is None
        or required_count is None
        or ready_count is None
        or (value.get("minimum_count") is not None and minimum_count is None)
    ):
        return None
    candidates_value = value.get("candidates")
    selected_value = value.get("selected_sources")
    if not isinstance(candidates_value, list | tuple) or not isinstance(
        selected_value, list | tuple
    ):
        return None
    candidate_views: list[dict[str, JsonValue]] = []
    for candidate_value in candidates_value[:32]:
        candidate = _result_input_candidate(candidate_value)
        if candidate is None:
            return None
        candidate_views.append(candidate)
    candidates = cast(list[JsonValue], candidate_views)
    selected_views: list[dict[str, JsonValue]] = []
    for source_value in selected_value[:32]:
        source = _result_input_source(source_value)
        if source is None:
            return None
        selected_views.append(source)
    selected_sources = cast(list[JsonValue], selected_views)
    missing_artifacts: list[JsonValue] = []
    missing_evidence: list[JsonValue] = []
    for candidate in candidate_views:
        if candidate["ready"] is True:
            continue
        missing_artifacts.extend(
            item
            for item in cast(list[JsonValue], candidate["missing_artifact_refs"])
            if item not in missing_artifacts
        )
        missing_evidence.extend(
            item
            for item in cast(list[JsonValue], candidate["missing_evidence_refs"])
            if item not in missing_evidence
        )
    selection_id = _parse_uuid(value.get("selection_id"))
    selection_hash = _selection_hash(value.get("selection_hash"))
    if (value.get("selection_id") is not None and selection_id is None) or (
        value.get("selection_hash") is not None and selection_hash is None
    ):
        return None
    if (selection_id is None) != (selection_hash is None):
        return None
    return {
        "group_key": group_key,
        "title": title,
        "mode": mode,
        "minimum_count": minimum_count,
        "required_count": required_count,
        "ready_count": ready_count,
        "reason_code": reason_code,
        "candidate_count": len(candidates),
        "selected_count": len(selected_sources),
        "candidates": candidates,
        "selected_sources": selected_sources,
        "selection_id": str(selection_id) if selection_id is not None else None,
        "selection_hash": selection_hash,
        "missing_artifact_refs": missing_artifacts[:32],
        "missing_evidence_refs": missing_evidence[:32],
    }


def _result_input_candidate(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    key = _string(value.get("worker_item_key"), limit=200)
    title = _string(value.get("worker_item_title"), limit=300)
    priority = _positive_int(value.get("priority"))
    ready = value.get("ready")
    retry_possible = value.get("retry_possible")
    outcome_unknown = value.get("outcome_unknown")
    path = _bounded_strings(value.get("subtask_path"), limit=16)
    if (
        key is None
        or title is None
        or priority is None
        or not isinstance(ready, bool)
        or not isinstance(retry_possible, bool)
        or not isinstance(outcome_unknown, bool)
        or not isinstance(value.get("subtask_path"), list | tuple)
    ):
        return None
    worker_id = _optional_uuid(value.get("worker_item_id"))
    if value.get("worker_item_id") is not None and worker_id is None:
        return None
    request_id = _optional_uuid(value.get("execution_request_id"))
    execution_id = _optional_uuid(value.get("execution_id"))
    attempt = _optional_positive_int(value.get("attempt"))
    if value.get("attempt") is not None and attempt is None:
        return None
    artifacts = _result_input_artifacts(value.get("artifacts"))
    evidence = _result_input_evidence(value.get("evidence"))
    if artifacts is None or evidence is None:
        return None
    return {
        "worker_item_key": key,
        "worker_item_id": str(worker_id) if worker_id is not None else None,
        "worker_item_title": title,
        "subtask_key": _string(value.get("subtask_key"), limit=200),
        "subtask_path": path,
        "priority": priority,
        "ready": ready,
        "reason_code": _string(value.get("reason_code"), limit=200),
        "missing_artifact_refs": _bounded_strings(
            value.get("missing_artifact_refs"), limit=32
        ),
        "missing_evidence_refs": _bounded_strings(
            value.get("missing_evidence_refs"), limit=32
        ),
        "retry_possible": retry_possible,
        "outcome_unknown": outcome_unknown,
        "execution_request_id": (
            str(request_id) if request_id is not None else None
        ),
        "execution_id": str(execution_id) if execution_id is not None else None,
        "attempt": attempt,
        "execution_status": _string(value.get("execution_status"), limit=100),
        "artifacts": artifacts,
        "evidence": evidence,
    }


def _result_input_source(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    worker_id = _parse_uuid(value.get("worker_item_id"))
    request_id = _parse_uuid(value.get("effective_request_id"))
    lineage_id = _parse_uuid(value.get("effective_lineage_id"))
    execution_id = _parse_uuid(value.get("effective_execution_id"))
    key = _string(value.get("worker_item_key"), limit=200)
    title = _string(value.get("worker_item_title"), limit=300)
    attempt = _positive_int(value.get("attempt"))
    priority = _positive_int(value.get("priority"))
    basis_hash = _string(value.get("source_basis_hash"), limit=64)
    path = _bounded_strings(value.get("subtask_path"), limit=16)
    inheritance_refs = _bounded_strings(value.get("inheritance_refs"), limit=32)
    if (
        worker_id is None
        or request_id is None
        or lineage_id is None
        or execution_id is None
        or key is None
        or title is None
        or attempt is None
        or priority is None
        or basis_hash is None
        or not isinstance(value.get("subtask_path"), list | tuple)
        or not isinstance(value.get("inheritance_refs"), list | tuple)
    ):
        return None
    artifacts = _result_input_artifacts(value.get("artifacts"))
    evidence = _result_input_evidence(value.get("evidence"))
    if artifacts is None or evidence is None:
        return None
    return {
        "worker_item_key": key,
        "worker_item_id": str(worker_id),
        "worker_item_title": title,
        "subtask_key": _string(value.get("subtask_key"), limit=200),
        "subtask_path": path,
        "effective_request_id": str(request_id),
        "effective_lineage_id": str(lineage_id),
        "effective_execution_id": str(execution_id),
        "attempt": attempt,
        "priority": priority,
        "inheritance_refs": inheritance_refs,
        "source_basis_hash": basis_hash,
        "artifacts": artifacts,
        "evidence": evidence,
    }


def _result_input_artifacts(value: object) -> list[JsonValue] | None:
    if not isinstance(value, list | tuple):
        return None
    result: list[JsonValue] = []
    for item in value[:32]:
        if not isinstance(item, Mapping):
            return None
        artifact_id = _parse_uuid(item.get("artifact_id"))
        artifact_type = _string(item.get("artifact_type"), limit=128)
        content_hash = _string(item.get("content_hash"), limit=128)
        size_bytes = _non_negative_int(item.get("size_bytes"))
        media_type = _string(item.get("media_type"), limit=128)
        if (
            artifact_id is None
            or artifact_type is None
            or content_hash is None
            or size_bytes is None
            or media_type is None
        ):
            return None
        result.append(
            {
                "artifact_id": str(artifact_id),
                "artifact_type": artifact_type,
                "content_hash": content_hash,
                "size_bytes": size_bytes,
                "media_type": media_type,
            }
        )
    return result


def _result_input_evidence(value: object) -> list[JsonValue] | None:
    if not isinstance(value, list | tuple):
        return None
    result: list[JsonValue] = []
    for item in value[:32]:
        if not isinstance(item, Mapping):
            return None
        evidence_id = _parse_uuid(item.get("evidence_id"))
        criterion = _string(item.get("acceptance_criterion_ref"), limit=300)
        artifact_ids = _uuid_strings(item.get("artifact_ids"), limit=32)
        if evidence_id is None or criterion is None or artifact_ids is None:
            return None
        result.append(
            {
                "evidence_id": str(evidence_id),
                "acceptance_criterion_ref": criterion,
                "artifact_ids": artifact_ids,
            }
        )
    return result


def _result_input_dependency(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    dependency_key = _string(value.get("dependency_key"), limit=200)
    upstream_request_id = _parse_uuid(value.get("upstream_request_id"))
    upstream_key = _string(value.get("upstream_worker_item_key"), limit=200)
    upstream_title = _string(value.get("upstream_worker_item_title"), limit=300)
    ready = value.get("ready")
    if (
        dependency_key is None
        or upstream_request_id is None
        or upstream_key is None
        or upstream_title is None
        or not isinstance(ready, bool)
    ):
        return None
    upstream_worker_id = _optional_uuid(value.get("upstream_worker_item_id"))
    if value.get("upstream_worker_item_id") is not None and upstream_worker_id is None:
        return None
    artifact_refs = _bounded_strings(value.get("required_artifact_refs"), limit=32)
    evidence_refs = _bounded_strings(value.get("required_evidence_refs"), limit=32)
    if not isinstance(value.get("required_artifact_refs"), list | tuple) or not isinstance(
        value.get("required_evidence_refs"), list | tuple
    ):
        return None
    selected_value = value.get("selected_source")
    selected = (
        _result_input_source(selected_value) if selected_value is not None else None
    )
    if selected_value is not None and selected is None:
        return None
    return {
        "dependency_key": dependency_key,
        "upstream_request_id": str(upstream_request_id),
        "upstream_worker_item_id": (
            str(upstream_worker_id) if upstream_worker_id is not None else None
        ),
        "upstream_worker_item_key": upstream_key,
        "upstream_worker_item_title": upstream_title,
        "required_artifact_refs": artifact_refs,
        "required_evidence_refs": evidence_refs,
        "ready": ready,
        "reason_code": _string(value.get("reason_code"), limit=200),
        "selected_source": selected,
    }


def _result_input_governance(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        return None
    fields: dict[str, JsonValue] = {
        "enabled": enabled,
        "state": _string(value.get("state"), limit=100) or "unknown",
        "waiting_summary": _string(value.get("waiting_summary"), limit=500),
        "governance_mode": _string(value.get("governance_mode"), limit=100)
        or "unknown",
        "release_state": _string(value.get("release_state"), limit=100) or "unknown",
        "attention_state": _string(value.get("attention_state"), limit=100) or "none",
    }
    for key in ("active_hold_id", "active_proposal_id", "active_proposal_input_selection_id"):
        parsed = _optional_uuid(value.get(key))
        if value.get(key) is not None and parsed is None:
            return None
        fields[key] = str(parsed) if parsed is not None else None
    for key in ("hold_resource_version", "active_proposal_version"):
        version = _optional_positive_int(value.get(key))
        if value.get(key) is not None and version is None:
            return None
        fields[key] = version
    selection_hash = _selection_hash(value.get("active_proposal_input_selection_hash"))
    if (
        value.get("active_proposal_input_selection_hash") is not None
        and selection_hash is None
    ):
        return None
    fields["active_proposal_input_selection_hash"] = selection_hash
    action_names = value.get("available_action_names")
    if not isinstance(action_names, list | tuple):
        return None
    fields["available_action_names"] = [
        item
        for item in _bounded_strings(action_names, limit=8)
        if item in _GOVERNANCE_ACTIONS
    ]
    history_value = value.get("history")
    if not isinstance(history_value, list | tuple):
        return None
    history: list[JsonValue] = []
    for item in history_value[:20]:
        if not isinstance(item, Mapping):
            return None
        resource_id = _parse_uuid(item.get("resource_id"))
        kind = _string(item.get("kind"), limit=100)
        status = _string(item.get("status"), limit=100)
        summary = _string(item.get("summary"), limit=500)
        occurred_at = _string(item.get("occurred_at"), limit=64)
        if (
            resource_id is None
            or kind is None
            or status is None
            or summary is None
            or occurred_at is None
        ):
            return None
        history.append(
            {
                "kind": kind,
                "resource_id": str(resource_id),
                "status": status,
                "summary": summary,
                "occurred_at": occurred_at,
                "result_code": _string(item.get("result_code"), limit=128),
                "proposal_id": (
                    str(_optional_uuid(item.get("proposal_id")))
                    if _optional_uuid(item.get("proposal_id")) is not None
                    else None
                ),
                "resolution_action": _string(
                    item.get("resolution_action"), limit=64
                ),
                "resolution_result": _string(
                    item.get("resolution_result"), limit=64
                ),
            }
        )
    fields["history"] = history
    return fields


def _result_input_delivery(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    consumer_type = _string(value.get("consumer_type"), limit=100)
    acknowledgement = _string(value.get("acknowledgement_status"), limit=100)
    if consumer_type is None or acknowledgement is None:
        return None
    result: dict[str, JsonValue] = {
        "consumer_type": consumer_type,
        "acknowledgement_status": acknowledgement,
        "reason_code": _string(value.get("reason_code"), limit=200),
    }
    for key in (
        "consumer_id",
        "package_id",
        "assembly_request_id",
    ):
        parsed = _optional_uuid(value.get(key))
        if value.get(key) is not None and parsed is None:
            return None
        result[key] = str(parsed) if parsed is not None else None
    for key in (
        "context_version",
        "assembly_attempt",
        "assembly_version",
        "materialization_attempt",
    ):
        number = _optional_non_negative_int(value.get(key))
        if value.get(key) is not None and number is None:
            return None
        result[key] = number
    for key, limit in (
        ("package_hash", 128),
        ("package_status", 100),
        ("assembly_status", 100),
        ("assembly_request_fingerprint", 128),
        ("materialization_outcome", 100),
        ("materialization_failure_code", 200),
        ("acknowledged_package_hash", 128),
    ):
        result[key] = _string(value.get(key), limit=limit)
    for key in ("materialization_occurred_at", "acknowledged_at"):
        result[key] = _string(value.get(key), limit=64)
    return result


def _result_input_execution(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    request_id = _parse_uuid(value.get("execution_request_id"))
    request_version = _positive_int(value.get("request_version"))
    if request_id is None or request_version is None:
        return None
    result: dict[str, JsonValue] = {
        "execution_request_id": str(request_id),
        "request_status": _string(value.get("request_status"), limit=100) or "unknown",
        "request_version": request_version,
        "collaboration_gate_mode": _string(
            value.get("collaboration_gate_mode"), limit=100
        )
        or "unknown",
        "gate_reason": _string(value.get("gate_reason"), limit=300) or "unknown",
        "status": _string(value.get("status"), limit=100),
        "failure_code": _string(value.get("failure_code"), limit=200),
        "attempt": _optional_positive_int(value.get("attempt")),
        "release_status": _string(value.get("release_status"), limit=100)
        or "unknown",
        "started_at": _string(value.get("started_at"), limit=64),
        "finished_at": _string(value.get("finished_at"), limit=64),
        "released_at": _string(value.get("released_at"), limit=64),
    }
    for key in ("execution_id", "lineage_id"):
        parsed = _optional_uuid(value.get(key))
        if value.get(key) is not None and parsed is None:
            return None
        result[key] = str(parsed) if parsed is not None else None
    if value.get("attempt") is not None and result["attempt"] is None:
        return None
    return result


def _result_input_action(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    action = _string(value.get("action"), limit=100)
    command_type = _string(value.get("command_type"), limit=150)
    target_type = _string(value.get("target_type"), limit=150)
    target_id = _parse_uuid(value.get("target_id"))
    if action is None or command_type is None or target_type is None or target_id is None:
        return None
    result: dict[str, JsonValue] = {
        "action": action,
        "command_type": command_type,
        "target_type": target_type,
        "target_id": str(target_id),
    }
    for key in ("expected_version", "expected_plan_generation"):
        number = _optional_positive_int(value.get(key))
        if value.get(key) is not None and number is None:
            return None
        result[key] = number
    fingerprint = _string(value.get("expected_fingerprint"), limit=128)
    if value.get("expected_fingerprint") is not None and fingerprint is None:
        return None
    result["expected_fingerprint"] = fingerprint
    selection_id = _optional_uuid(value.get("expected_input_selection_id"))
    if value.get("expected_input_selection_id") is not None and selection_id is None:
        return None
    selection_hash = _selection_hash(value.get("expected_input_selection_hash"))
    if value.get("expected_input_selection_hash") is not None and selection_hash is None:
        return None
    if (selection_id is None) != (selection_hash is None):
        return None
    result["expected_input_selection_id"] = (
        str(selection_id) if selection_id is not None else None
    )
    result["expected_input_selection_hash"] = selection_hash
    return result


def _result_input_next_action(
    *,
    missing: list[JsonValue],
    governance: Mapping[str, JsonValue],
    delivery: Mapping[str, JsonValue],
    execution: Mapping[str, JsonValue] | None,
    actions: list[JsonValue],
    historical: bool,
) -> str:
    if missing:
        return "resolve_input_blocker"
    action_names = governance.get("available_action_names")
    if (
        not historical
        and isinstance(action_names, list)
        and "approve" in action_names
        and governance.get("active_proposal_id") is not None
    ):
        return "review_release_proposal"
    if delivery.get("acknowledgement_status") != "acknowledged":
        return "wait_for_result_delivery"
    if execution is not None and execution.get("status") is None:
        return "wait_for_execution"
    if actions:
        first = actions[0]
        if isinstance(first, Mapping):
            action = first.get("action")
            if isinstance(action, str):
                return action
    return "monitor_worker_item"


def _result_input_summary_text(data: Mapping[str, JsonValue]) -> str:
    missing = data.get("missing")
    if isinstance(missing, list) and missing:
        return "Worker result inputs are blocked by one or more reported prerequisites."
    if data.get("next_action") == "review_release_proposal":
        return "Worker result inputs are ready; the release Proposal needs review."
    return "Worker result input status loaded."


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


def _worker_proposal_selection_preflight(
    view: GovernanceWorkerItemView, proposal: GovernanceProposalView
) -> ToolEnvelope | None:
    """Reject a governance view that disagrees with the exact Proposal view."""

    active = _governance_mapping(view.governance).get("active_proposal")
    if not isinstance(active, Mapping):
        return None
    raw_id = active.get("input_selection_id")
    raw_hash = active.get("input_selection_hash")
    if raw_id is None and raw_hash is None:
        return None
    active_id = _parse_uuid(raw_id)
    active_hash = _selection_hash(raw_hash)
    if (
        active_id is None
        or active_hash is None
        or proposal.input_selection_id != active_id
        or proposal.input_selection_hash != active_hash
    ):
        return _governance_selection_conflict()
    return None


def _input_selection_preflight(
    raw: object,
    *,
    expected_workspace_id: UUID,
    expected_root_task_id: UUID,
    expected_worker_item_id: UUID,
    expected_request_version: int,
    expected_selection_id: UUID,
    expected_selection_hash: str | None,
) -> ToolEnvelope | None:
    if not isinstance(raw, Mapping):
        return _governance_selection_conflict()
    selection_id = _parse_uuid(raw.get("selection_id"))
    workspace_id = _parse_uuid(raw.get("workspace_id"))
    root_task_id = _parse_uuid(raw.get("root_task_id"))
    target_worker_item_id = _parse_uuid(raw.get("target_worker_item_id"))
    target_request_version = _positive_int(raw.get("target_request_version"))
    selection_hash = _selection_hash(raw.get("selection_hash"))
    if (
        selection_id != expected_selection_id
        or workspace_id != expected_workspace_id
        or root_task_id != expected_root_task_id
        or target_worker_item_id != expected_worker_item_id
        or target_request_version != expected_request_version
        or selection_hash is None
        or expected_selection_hash != selection_hash
    ):
        return _governance_selection_conflict()
    return None


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


def _governance_selection_conflict() -> ToolEnvelope:
    return ToolEnvelope(
        outcome="conflict",
        summary="The fixed input Selection is no longer current; reload and confirm again.",
        error_code="input_selection_conflict",
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


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    return _positive_int(value)


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value)


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    return _parse_uuid(value)


def _uuid_strings(value: object, *, limit: int) -> list[JsonValue] | None:
    if not isinstance(value, list | tuple):
        return None
    result: list[JsonValue] = []
    for item in value[:limit]:
        parsed = _parse_uuid(item)
        if parsed is None:
            return None
        result.append(str(parsed))
    return result


def _selection_hash(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
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
