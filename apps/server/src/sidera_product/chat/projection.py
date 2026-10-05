from datetime import datetime
from uuid import UUID

from pydantic import JsonValue, TypeAdapter, ValidationError
from sidera_agents.contracts import RunEvent, RunSnapshot, RunState

from sidera_product.postgres.authority_store import ToolReceiptRecord
from sidera_product.task_tools.transport import MutationResult

from .contracts import ChatEvent, ChatTurn, TurnItem, TurnStatus, TurnView


class ProjectionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def resume_cursor(after: int, last_event_id: str | None, *, head: int) -> int:
    if type(after) is not int or after < 0:
        raise ProjectionError("resync_required")
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdecimal():
            raise ProjectionError("resync_required")
        after = max(after, int(last_event_id))
    if after > head:
        raise ProjectionError("resync_required")
    return after


def response_binding(turn: ChatTurn, events: tuple[RunEvent, ...]) -> UUID | None:
    refs = {
        event.response_id
        for event in events
        if event.operation_id == turn.native_operation_id
        and event.response_id is not None
        and event.kind in ("input.accepted", "response.started")
    }
    if len(refs) > 1 or (
        turn.response_id is not None and refs and refs != {turn.response_id}
    ):
        raise ProjectionError("original_response_mismatch")
    return turn.response_id or next(iter(refs), None)


def _matching(
    turn: ChatTurn, response_id: UUID | None, events: tuple[RunEvent, ...]
) -> tuple[RunEvent, ...]:
    if response_id is None:
        return ()
    matching: list[RunEvent] = []
    prior = 0
    for event in events:
        if event.sequence <= prior:
            raise ProjectionError("native_cursor_invalid")
        prior = event.sequence
        if event.response_id == response_id:
            if event.operation_id not in (None, turn.native_operation_id):
                raise ProjectionError("original_response_mismatch")
            matching.append(event)
    return tuple(matching)


def project_turn(
    turn: ChatTurn, run: RunSnapshot, events: tuple[RunEvent, ...]
) -> TurnView:
    if run.workspace_id != turn.workspace_id or any(
        event.run_id != run.run_id for event in events
    ):
        raise ProjectionError("original_run_mismatch")
    response_id = response_binding(turn, events)
    selected = _matching(turn, response_id, events)
    items: list[TurnItem] = [
        TurnItem(
            id=f"{turn.id}:user", type="userMessage", data={"text": turn.input_text}
        )
    ]
    text = ""
    started: datetime | None = None
    completed: datetime | None = None
    usage: dict[str, JsonValue] | None = None
    error: dict[str, JsonValue] | None = None
    non_admitted = turn.request_state in ("busy", "rejected")
    status: TurnStatus = "failed" if non_admitted else "queued"
    input_unknown = False
    if turn.reason_code is not None:
        error = {
            "code": turn.reason_code,
            "message": turn.reason_code,
            "retryable": turn.request_state == "busy",
        }
    assistant_index: int | None = None
    for event in selected:
        if event.kind == "response.started":
            started, status = event.occurred_at, "in_progress"
        elif event.kind == "message":
            value = event.data.get("text")
            update = event.data.get("update_type")
            if not isinstance(value, str):
                continue
            if update in ("agent_message_chunk", "agent_message"):
                text = value if update == "agent_message" else text + value
                item = TurnItem(
                    id=f"{response_id}:assistant",
                    type="assistantMessage",
                    data={"text": text},
                )
                if assistant_index is None:
                    assistant_index = len(items)
                    items.append(item)
                else:
                    items[assistant_index] = item
            elif update in ("agent_thought_chunk", "plan"):
                items.append(
                    TurnItem(
                        id=f"{response_id}:{event.sequence}",
                        type="assistantMessage",
                        data={
                            "text": value,
                            "kind": "thought"
                            if update == "agent_thought_chunk"
                            else "plan",
                        },
                    )
                )
        elif event.kind == "tool":
            items.append(
                TurnItem(
                    id=f"{response_id}:{event.sequence}",
                    type="toolCall",
                    data={
                        "name": event.data.get("tool_name", "tool"),
                        "status": event.data.get("status", "pending"),
                        "text": event.data.get("text"),
                        "verified": False,
                    },
                )
            )
        elif event.kind == "usage":
            value = event.data.get("usage")
            if isinstance(value, dict):
                usage = value
        elif event.kind in (
            "response.completed",
            "response.interrupted",
            "response.failed",
        ):
            completed = event.occurred_at
            terminal_statuses: dict[str, TurnStatus] = {
                "response.completed": "completed",
                "response.interrupted": "interrupted",
                "response.failed": "failed",
            }
            status = terminal_statuses[event.kind]
        elif event.kind == "input.unknown" and completed is None:
            input_unknown = True
            code = event.data.get("reason_code")
            error = {
                "code": code if isinstance(code, str) else "native_operation_unknown",
                "retryable": False,
            }
        elif event.kind == "input.failed" and completed is None:
            status = "failed"
            code = event.data.get("reason_code")
            error = {
                "code": code if isinstance(code, str) else "native_operation_unknown",
                "retryable": False,
            }
    if completed is None and (
        input_unknown
        or (run.state is RunState.UNKNOWN and not non_admitted and status != "failed")
    ):
        status = "queued"
        error = error or {
            "code": run.reason_code or "native_run_unknown",
            "retryable": False,
        }
    current = (
        run.state is RunState.RUNNING
        and response_id is not None
        and run.active_response_id == response_id
        and completed is None
    )
    return TurnView(
        id=str(turn.id),
        threadId=str(turn.conversation_id),
        status=status,
        createdAt=turn.created_at,
        startedAt=started,
        completedAt=completed,
        finalResponse=text if completed is not None and text else None,
        items=tuple(items),
        usage=usage,
        error=error,
        profileRef=run.profile_ref,
        runId=run.run_id,
        responseId=response_id,
        operationId=turn.operation_id,
        operationState=turn.request_state,
        streamAvailable=completed is None
        and turn.request_state != "busy"
        and not input_unknown
        and run.state in (RunState.STARTING, RunState.RUNNING),
        recoveryMode="poll"
        if completed is None and turn.request_state != "busy"
        else None,
        interruptAvailable=False,
        interruptReasonCode="cancel_unconfirmed" if current else "response_not_current",
    )


def event_frames(
    turn: ChatTurn, run: RunSnapshot, events: tuple[RunEvent, ...], *, after: int
) -> tuple[ChatEvent, ...]:
    response_id = response_binding(turn, events)
    selected = _matching(turn, response_id, events)
    frames: list[ChatEvent] = []
    for event in selected:
        if event.sequence <= after:
            continue
        view = project_turn(
            turn, run, tuple(item for item in events if item.sequence <= event.sequence)
        )
        data: dict[str, JsonValue]
        if event.kind == "response.started":
            kind = "started"
            data = {"turnId": str(turn.id), "startedAt": event.occurred_at.isoformat()}
        elif event.kind in ("message", "tool"):
            identity = (
                f"{response_id}:assistant"
                if event.data.get("update_type")
                in ("agent_message_chunk", "agent_message")
                else f"{response_id}:{event.sequence}"
            )
            item = next((item for item in view.items if item.id == identity), None)
            if item is None:
                continue
            kind = "item"
            data = {
                "item": TypeAdapter(dict[str, JsonValue]).validate_python(
                    item.model_dump(mode="json")
                )
            }
        elif event.kind in (
            "response.completed",
            "response.interrupted",
            "response.failed",
        ):
            kind = view.status
            data = {
                "status": view.status,
                "finalResponse": view.final_response,
                "usage": view.usage,
                "completedAt": event.occurred_at.isoformat(),
            }
        else:
            continue
        frames.append(ChatEvent(sequence=event.sequence, type=kind, data=data))
    return tuple(frames)


def receipt_items(
    rows: tuple[ToolReceiptRecord, ...],
    *,
    grant_id: UUID,
    run_id: UUID,
    response_id: UUID,
    workspace_id: UUID,
) -> tuple[TurnItem, ...]:
    items: list[TurnItem] = []
    for row in rows:
        if (
            row.grant_id != grant_id
            or row.run_id != run_id
            or row.response_id != response_id
        ):
            continue
        result = None
        if row.result is not None and row.tool_name in (
            "sidera_submit_goal",
            "sidera_request_revision",
        ):
            try:
                parsed = MutationResult.model_validate(row.result)
                if (
                    parsed.command_id == row.command_id
                    and parsed.outcome == row.outcome
                ):
                    result = parsed
            except ValidationError:
                pass
        verified = result is not None and result.outcome == "applied"
        root = result.root_task_id if verified and result is not None else None
        planning = (
            result.planning_request_id if verified and result is not None else None
        )
        items.append(
            TurnItem(
                id=f"product-receipt:{row.receipt_id}",
                type="toolCall",
                data={
                    "receiptSource": "sidera.product.task-receipt.v1",
                    "receiptId": str(row.receipt_id),
                    "name": row.tool_name,
                    "verified": verified,
                    "logicalRequestId": str(row.logical_request_id),
                    "commandId": str(row.command_id),
                    "runId": str(row.run_id),
                    "responseId": str(row.response_id),
                    "outcome": result.outcome if result is not None else "unknown",
                    "status": result.status if result is not None else "unknown",
                    "errorCode": result.error_code
                    if result is not None
                    else "command_result_unknown",
                    "rootTaskId": None if root is None else str(root),
                    "planningRequestId": None if planning is None else str(planning),
                    "webUrl": f"/workspaces/{workspace_id}/tasks/{root}"
                    if root is not None
                    else None,
                },
            )
        )
    return tuple(items)
