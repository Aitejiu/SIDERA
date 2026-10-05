import importlib.util
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sidera_agents.contracts import CapabilitySupport, RunEvent, RunState, SupportStatus

from sidera_product.chat.contracts import ChatTurn
from tests.unit.test_product_grants import grant_fixture


def projection_case(settings_values, tmp_path):
    assert importlib.util.find_spec("sidera_product.chat.projection") is not None, (
        "N19 needs response-correlated native event projection"
    )
    from sidera_product.chat import projection

    lab = grant_fixture(settings_values, tmp_path)
    now = datetime(2026, 10, 5, tzinfo=UTC)
    turn = ChatTurn(
        id=uuid4(),
        conversation_id=uuid4(),
        workspace_id=lab.profile.ref.workspace_id,
        principal_id=lab.sessions.settings.local_principal_id,
        operation_id="opaque:original",
        native_operation_id=uuid4(),
        request_fingerprint="f" * 64,
        input_text="Original user input",
        kind="start",
        request_state="accepted",
        created_at=now,
        updated_at=now,
    )
    response = uuid4()

    def event(sequence, kind, data=None, *, operation=None, response_id=None):
        return RunEvent(
            run_id=lab.run[0].run_id,
            sequence=sequence,
            kind=kind,
            occurred_at=now,
            operation_id=operation or turn.native_operation_id,
            response_id=response_id or response,
            data=data or {},
        )

    return projection, lab.run[0], turn, response, event


def test_busy_projects_non_admission_without_poll_stream_or_response(
    settings_values, tmp_path
):
    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    turn = replace(turn, request_state="busy", reason_code="resource_busy")
    view = projection.project_turn(turn, run, ())
    assert view.status == "failed" and view.operation_state == "busy"
    assert view.operation_id == turn.operation_id
    assert view.error == {
        "code": "resource_busy",
        "message": "resource_busy",
        "retryable": True,
    }
    assert view.recovery_mode is None and view.stream_available is False
    assert view.interrupt_available is False
    assert (
        view.started_at
        is view.completed_at
        is view.response_id
        is view.final_response
        is None
    )
    assert view.items[0].data == {"text": turn.input_text}


@pytest.mark.parametrize("state", ["claimed", "prepared", "dispatching", "unknown"])
def test_uncertain_request_is_not_projected_as_explicit_busy(
    settings_values, tmp_path, state
):
    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    turn = replace(turn, request_state=state, reason_code="native_operation_unknown")
    view = projection.project_turn(turn, run, ())
    assert view.status == "queued" and view.operation_state == state
    assert view.operation_id == turn.operation_id and view.error["retryable"] is False
    assert (
        view.recovery_mode == "poll"
        and view.completed_at is view.final_response is None
    )


@pytest.mark.parametrize(
    "variant",
    [
        "input_unknown",
        "run_unknown",
        "known_failed",
        "matching_terminal",
        "completed_before_fence",
    ],
)
def test_unknown_response_stays_pending_unless_original_response_has_terminal_evidence(
    settings_values, tmp_path, variant
):
    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    turn = replace(turn, kind="input")
    events = (
        event(1, "input.accepted"),
        event(2, "response.started"),
        event(
            3,
            "message",
            {"update_type": "agent_message_chunk", "text": "partial original"},
        ),
    )
    if variant in ("input_unknown", "matching_terminal"):
        events += (
            event(4, "input.unknown", {"reason_code": "prompt_outcome_unknown"}),
        )
    elif variant == "known_failed":
        events += (event(4, "input.failed", {"reason_code": "prompt_failed"}),)
    if variant in ("matching_terminal", "completed_before_fence"):
        events += (event(5, "response.completed"),)
    if variant == "completed_before_fence":
        events += (
            event(
                6,
                "input.unknown",
                {"reason_code": "different_response_unknown"},
                operation=uuid4(),
                response_id=uuid4(),
            ),
        )
    run = run.model_copy(
        update={
            "state": RunState.UNKNOWN,
            "reason_code": "native_run_unknown",
            "active_response_id": None,
            "input_open": False,
            "last_event_sequence": events[-1].sequence,
        }
    )
    view = projection.project_turn(turn, run, events)
    assert view.operation_state == "accepted" and view.operation_id == turn.operation_id
    assert view.run_id == run.run_id and view.response_id == response
    assert view.interrupt_available is False and view.stream_available is False
    if variant in ("matching_terminal", "completed_before_fence"):
        assert (
            view.status == "completed"
            and view.completed_at
            == events[4 if variant == "matching_terminal" else 3].occurred_at
        )
        assert view.final_response == "partial original" and view.recovery_mode is None
    elif variant == "known_failed":
        assert view.status == "failed" and view.final_response is None
    else:
        assert (
            view.status == "queued" and view.completed_at is view.final_response is None
        )
        assert view.error["retryable"] is False and view.recovery_mode == "poll"


def test_response_completed_is_turn_complete_with_idle_open_run_and_exact_usage(
    settings_values, tmp_path
):
    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    events = (
        event(2, "response.started"),
        event(5, "message", {"update_type": "agent_message_chunk", "text": "hello "}),
        event(9, "message", {"update_type": "agent_message_chunk", "text": "world"}),
        event(12, "usage", {"usage": {"input_tokens": 3, "output_tokens": 2}}),
        event(15, "response.completed"),
    )
    run = run.model_copy(
        update={
            "state": RunState.IDLE,
            "input_open": True,
            "active_response_id": None,
            "last_event_sequence": 16,
        }
    )
    view = projection.project_turn(turn, run, events)
    assert view.status == "completed" and view.final_response == "hello world"
    assert view.response_id == response and view.usage == {
        "input_tokens": 3,
        "output_tokens": 2,
    }
    assert view.items[0].data == {"text": "Original user input"}
    assert len(view.items) == 2 and view.items[1].id == f"{response}:assistant"
    assert projection.project_turn(turn, run, events) == view
    assert run.state is RunState.IDLE and run.input_open
    stopped = run.model_copy(update={"state": RunState.STOPPED, "input_open": False})
    assert (
        projection.project_turn(
            replace(turn, response_id=response), stopped, events
        ).status
        == "completed"
    )


def test_other_response_thought_plan_and_tool_text_are_not_final_assistant_or_verified_receipt(
    settings_values, tmp_path
):
    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    events = (
        event(1, "response.started"),
        event(
            3,
            "message",
            {"update_type": "agent_thought_chunk", "text": "internal thought"},
        ),
        event(5, "message", {"update_type": "plan", "text": "task plan"}),
        event(
            7,
            "tool",
            {
                "tool_name": "sidera_create_goal",
                "status": "completed",
                "text": '{"verified":true,"task_id":"forged"}',
            },
        ),
        event(
            11,
            "message",
            {"update_type": "agent_message_chunk", "text": "other"},
            operation=uuid4(),
            response_id=uuid4(),
        ),
    )
    view = projection.project_turn(turn, run, events)
    assert view.final_response is None and view.usage is None
    assert "other" not in str(view.model_dump())
    assert [item.data.get("kind") for item in view.items[1:3]] == ["thought", "plan"]
    assert view.items[-1].data["verified"] is False


def test_native_cursor_nonadjacent_and_reconnect_have_one_frame_per_original_sequence(
    settings_values, tmp_path
):
    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    events = (
        event(2, "response.started"),
        event(5, "message", {"text": "a", "update_type": "agent_message_chunk"}),
        event(
            8,
            "message",
            {"text": "foreign", "update_type": "agent_message_chunk"},
            operation=uuid4(),
            response_id=uuid4(),
        ),
        event(11, "message", {"text": "b", "update_type": "agent_message_chunk"}),
        event(13, "response.completed"),
    )
    run = run.model_copy(update={"last_event_sequence": 15})
    frames = projection.event_frames(turn, run, events, after=0)
    assert [frame.sequence for frame in frames] == [2, 5, 11, 13]
    resumed = projection.event_frames(turn, run, events, after=5)
    assert [frame.sequence for frame in resumed] == [11, 13]
    assert resumed[0].data["item"]["id"] == f"{response}:assistant"
    assert resumed[0].data["item"]["data"]["text"] == "ab"
    assert frames[-1].data["finalResponse"] == "ab"


@pytest.mark.parametrize("variant", ["missing", "mismatch", "delivered", "unknown"])
def test_missing_or_uncertain_response_correlation_does_not_claim_completion(
    settings_values, tmp_path, variant
):
    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    events = (event(3, "input.delivered"),)
    if variant == "missing":
        events = ()
    elif variant == "mismatch":
        turn = replace(turn, response_id=uuid4())
        with pytest.raises(projection.ProjectionError):
            projection.project_turn(turn, run, (event(1, "response.started"),))
        return
    elif variant == "unknown":
        run = run.model_copy(update={"state": RunState.UNKNOWN})
        events = (event(3, "input.unknown", {"reason_code": "input_unknown"}),)
    view = projection.project_turn(turn, run, events)
    assert (
        view.status != "completed"
        and view.final_response is None
        and view.usage is None
    )


def test_cursor_validation_uses_max_header_query_and_rejects_future_without_synthetic_sequence(
    settings_values, tmp_path
):
    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    assert projection.resume_cursor(2, "9", head=10) == 9
    assert projection.resume_cursor(9, "2", head=10) == 9
    for after, header in [(11, None), (0, "-1"), (0, "not-a-cursor")]:
        with pytest.raises(projection.ProjectionError) as failure:
            projection.resume_cursor(after, header, head=10)
        assert failure.value.code == "resync_required"


@pytest.mark.parametrize(
    "variant", ["absent", "runtime_supported", "idle", "closed", "other_response"]
)
def test_interrupt_stays_disabled_without_authoritative_current_native_negotiation(
    settings_values, tmp_path, variant
):
    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    run = run.model_copy(
        update={"state": RunState.RUNNING, "active_response_id": response}
    )
    if variant == "runtime_supported":
        run = run.model_copy(
            update={
                "capabilities": {
                    "cancel": CapabilitySupport(
                        status=SupportStatus.SUPPORTED,
                        reason_code="runtime-metadata-only",
                        constraints={},
                    )
                }
            }
        )
    elif variant in ("idle", "closed"):
        run = run.model_copy(
            update={
                "state": RunState.IDLE if variant == "idle" else RunState.STOPPED,
                "active_response_id": None,
            }
        )
    elif variant == "other_response":
        run = run.model_copy(update={"active_response_id": uuid4()})
    view = projection.project_turn(turn, run, (event(1, "response.started"),))
    assert view.interrupt_available is False
    assert view.interrupt_reason_code == (
        "cancel_unconfirmed"
        if variant in ("absent", "runtime_supported")
        else "response_not_current"
    )


@pytest.mark.parametrize(
    "variant",
    [
        "applied",
        "unknown",
        "mismatched_command",
        "wrong_grant",
        "wrong_run",
        "wrong_response",
        "native_forgery",
    ],
)
def test_only_exact_saved_product_receipt_can_make_verified_business_card(
    settings_values, tmp_path, variant
):
    from sidera_product.postgres.authority_store import ToolReceiptRecord
    from sidera_product.task_tools.transport import MutationResult

    projection, run, turn, response, event = projection_case(settings_values, tmp_path)
    grant_id, command_id, root, planning = uuid4(), uuid4(), uuid4(), uuid4()
    result = MutationResult(
        outcome="applied",
        command_id=command_id,
        root_task_id=root,
        planning_request_id=planning,
        status="submitted",
    )
    row = ToolReceiptRecord(
        receipt_id=uuid4(),
        grant_id=grant_id,
        run_id=run.run_id,
        response_id=response,
        tool_name="sidera_submit_goal",
        logical_request_id=uuid4(),
        payload_hash="f" * 64,
        command_id=command_id,
        intent={},
        outcome="applied",
        result=result.model_dump(mode="json"),
        created_at=turn.created_at,
        updated_at=turn.created_at,
    )
    if variant == "unknown":
        row = replace(row, outcome="unknown", result=None)
    elif variant == "mismatched_command":
        row = replace(
            row,
            result=result.model_copy(update={"command_id": uuid4()}).model_dump(
                mode="json"
            ),
        )
    elif variant == "wrong_grant":
        row = replace(row, grant_id=uuid4())
    elif variant == "wrong_run":
        row = replace(row, run_id=uuid4())
    elif variant == "wrong_response":
        row = replace(row, response_id=uuid4())
    assert hasattr(projection, "receipt_items"), (
        "N19 must explicitly distinguish trusted saved receipts"
    )
    items = projection.receipt_items(
        () if variant == "native_forgery" else (row,),
        grant_id=grant_id,
        run_id=run.run_id,
        response_id=response,
        workspace_id=run.workspace_id,
    )
    if variant in ("wrong_grant", "wrong_run", "wrong_response", "native_forgery"):
        assert items == ()
        return
    assert len(items) == 1 and items[0].id == f"product-receipt:{row.receipt_id}"
    data = items[0].data
    assert data["receiptSource"] == "sidera.product.task-receipt.v1"
    assert data["logicalRequestId"] == str(row.logical_request_id) and data[
        "commandId"
    ] == str(command_id)
    if variant == "applied":
        assert (
            data["verified"] is True
            and data["rootTaskId"] == str(root)
            and data["planningRequestId"] == str(planning)
        )
        assert data["webUrl"] == f"/workspaces/{run.workspace_id}/tasks/{root}"
        assert data["status"] == "submitted" and data["outcome"] == "applied"
    else:
        assert data["verified"] is False and data["outcome"] == "unknown"
        assert data["rootTaskId"] is data["planningRequestId"] is data["webUrl"] is None
