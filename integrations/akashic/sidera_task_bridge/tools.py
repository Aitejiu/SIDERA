from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from agent.plugins.decorators import tool
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
from .contracts import JsonValue, ToolEnvelope
from .source_routes import (
    MissingToolContext,
    SourceContextResolver,
    SourceResolution,
)
from .state import BridgeStateStore

_CONFLICT_CODES = {
    "planning_conflict",
    "version_conflict",
    "idempotency_conflict",
    "resource_busy",
    "command_identity_conflict",
}
_UNAVAILABLE_CODES = {
    "service_unavailable",
    "command_status_unavailable",
}


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

    @tool(name="sidera_submit_brief", risk="write", always_on=True)
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

    @tool(name="sidera_get_planning_status", risk="read-only", always_on=True)
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

    @tool(
        name="sidera_get_task_status",
        risk="read-only",
        search_hint="SIDERA task progress status milestone worker next work item",
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

    def _task_url(self, task_id: UUID) -> str:
        return (
            f"{str(self._config.web_base_url).rstrip('/')}/workspaces/"
            f"{self._config.workspace_id}/tasks/{task_id}"
        )


def _encode(envelope: ToolEnvelope) -> str:
    return envelope.model_dump_json()


def _non_empty_text(value: str) -> bool:
    return bool(value.strip())


def _clean_texts(values: list[str]) -> list[JsonValue]:
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
