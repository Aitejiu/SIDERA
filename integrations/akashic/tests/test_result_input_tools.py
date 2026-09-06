import asyncio
import json
from typing import Any
from uuid import UUID

from agent.tools.base import tool_execution_context_scope
from test_tools import (
    COMMAND_ID,
    HOLD_ID,
    PROPOSAL_ID,
    TASK_ID,
    WORKER_ITEM_ID,
    FakeClient,
    FakeDispatcher,
    decode,
    governance_command_result,
    governance_worker_view,
    private_context,
    tools,
)

from sidera_task_bridge.client import CommandIntent

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
SELECTION_ID = UUID("61000000-0000-0000-0000-000000000001")
EXECUTION_REQUEST_ID = UUID("62000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("63000000-0000-0000-0000-000000000001")
LINEAGE_ID = UUID("64000000-0000-0000-0000-000000000001")
ARTIFACT_ID = UUID("65000000-0000-0000-0000-000000000001")
EVIDENCE_ID = UUID("66000000-0000-0000-0000-000000000001")


class ResultInputFakeClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.worker_result_inputs: dict[str, Any] = {}
        self.input_selection: dict[str, Any] = {}
        self.result_input_calls: list[tuple[UUID, UUID | None]] = []
        self.selection_calls: list[UUID] = []

    async def get_worker_result_inputs(
        self, worker_item_id: UUID, execution_request_id: UUID | None = None
    ) -> dict[str, Any]:
        self.result_input_calls.append((worker_item_id, execution_request_id))
        return self.worker_result_inputs

    async def get_input_selection(self, selection_id: UUID) -> dict[str, Any]:
        self.selection_calls.append(selection_id)
        return self.input_selection


def artifact(*, artifact_id: UUID = ARTIFACT_ID) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact_id),
        "artifact_type": "report",
        "content_hash": "b" * 64,
        "size_bytes": 42,
        "media_type": "text/plain",
    }


def evidence() -> dict[str, Any]:
    return {
        "evidence_id": str(EVIDENCE_ID),
        "acceptance_criterion_ref": "criterion:1",
        "artifact_ids": [str(ARTIFACT_ID)],
    }


def selected_source(*, key: str, worker_id: UUID) -> dict[str, Any]:
    return {
        "worker_item_key": key,
        "worker_item_id": str(worker_id),
        "worker_item_title": f"{key} report",
        "subtask_key": "research",
        "subtask_path": ["Research"],
        "effective_request_id": str(EXECUTION_REQUEST_ID),
        "effective_lineage_id": str(LINEAGE_ID),
        "effective_execution_id": str(EXECUTION_ID),
        "attempt": 1,
        "priority": 1,
        "inheritance_refs": [],
        "source_basis_hash": "c" * 64,
        "artifacts": [artifact()],
        "evidence": [evidence()],
        # The adapter must never echo this provider/runtime payload.
        "body_text": "private report body must not cross the adapter",
        "runtime_payload": {"provider_key": "do-not-leak"},
    }


def worker_result_inputs_view() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workspace_id": str(WORKSPACE_ID),
        "root_task_id": str(TASK_ID),
        "worker_item_id": str(WORKER_ITEM_ID),
        "worker_item_key": "summary",
        "worker_item_title": "Summarize reports",
        "request": {
            "execution_request_id": str(EXECUTION_REQUEST_ID),
            "worker_item_id": str(WORKER_ITEM_ID),
            "worker_item_key": "summary",
            "subtask_key": "summary",
            "version": 8,
            "status": "waiting_release",
            "collaboration_gate_mode": "manual",
            "gate_reason": "waiting_for_release",
        },
        "plan_snapshot_id": str(UUID("67000000-0000-0000-0000-000000000001")),
        "plan_generation": 3,
        "plan_content_hash": "d" * 64,
        "is_historical": False,
        "groups": [
            {
                "group_key": "research-reports",
                "title": "Research reports",
                "mode": "quorum",
                "minimum_count": 2,
                "required_count": 2,
                "ready_count": 2,
                "reason_code": None,
                "candidates": [],
                "selected_sources": [
                    selected_source(
                        key="research-a",
                        worker_id=UUID("68000000-0000-0000-0000-000000000001"),
                    ),
                    selected_source(
                        key="research-b",
                        worker_id=UUID("68000000-0000-0000-0000-000000000002"),
                    ),
                ],
                "selection_id": str(SELECTION_ID),
                "selection_hash": "a" * 64,
            }
        ],
        "hard_dependencies": [
            {
                "dependency_key": "research-complete",
                "upstream_request_id": str(EXECUTION_REQUEST_ID),
                "upstream_worker_item_id": str(WORKER_ITEM_ID),
                "upstream_worker_item_key": "research",
                "upstream_worker_item_title": "Research",
                "required_artifact_refs": ["report:final"],
                "required_evidence_refs": ["criterion:1"],
                "ready": True,
                "reason_code": None,
                "selected_source": None,
            }
        ],
        "governance": {
            "enabled": True,
            "state": "waiting_release",
            "waiting_summary": "Waiting for release approval",
            "governance_mode": "paused",
            "release_state": "proposed",
            "active_hold_id": str(HOLD_ID),
            "hold_resource_version": 4,
            "active_proposal_id": str(PROPOSAL_ID),
            "active_proposal_version": 3,
            "active_proposal_input_selection_id": str(SELECTION_ID),
            "active_proposal_input_selection_hash": "a" * 64,
            "attention_state": "decision_required",
            "available_action_names": ["approve", "reject"],
            "history": [],
            "actor": {"id": "private-actor-must-not-leak"},
        },
        "delivery": {
            "consumer_type": "execution_request",
            "consumer_id": str(EXECUTION_REQUEST_ID),
            "package_id": str(UUID("69000000-0000-0000-0000-000000000001")),
            "context_version": 2,
            "package_hash": "e" * 64,
            "package_status": "materialized",
            "assembly_request_id": str(UUID("6a000000-0000-0000-0000-000000000001")),
            "assembly_status": "succeeded",
            "assembly_attempt": 1,
            "assembly_version": 2,
            "assembly_request_fingerprint": "f" * 64,
            "materialization_attempt": 1,
            "materialization_outcome": "succeeded",
            "materialization_failure_code": None,
            "materialization_occurred_at": "2026-09-06T00:00:00Z",
            "acknowledgement_status": "acknowledged",
            "acknowledged_package_hash": "e" * 64,
            "acknowledged_at": "2026-09-06T00:00:01Z",
            "reason_code": None,
            "body_text": "delivery body must not leak",
        },
        "execution": {
            "execution_request_id": str(EXECUTION_REQUEST_ID),
            "request_status": "waiting_release",
            "request_version": 8,
            "collaboration_gate_mode": "manual",
            "gate_reason": "waiting_for_release",
            "execution_id": None,
            "lineage_id": None,
            "attempt": None,
            "status": None,
            "failure_code": None,
            "started_at": None,
            "finished_at": None,
            "release_status": "pending",
            "released_at": None,
        },
        "available_actions": [
            {
                "action": "retry_context_assembly",
                "command_type": "RetryContextAssembly",
                "target_type": "context_assembly_request",
                "target_id": str(UUID("6a000000-0000-0000-0000-000000000001")),
                "expected_version": 2,
                "expected_fingerprint": "f" * 64,
                "expected_plan_generation": 3,
                "expected_input_selection_id": str(SELECTION_ID),
                "expected_input_selection_hash": "a" * 64,
            }
        ],
        "actor": {"id": "must-not-leak"},
    }


def v2_governance_proposal() -> dict[str, Any]:
    return {
        "workspace_id": str(WORKSPACE_ID),
        "root_task_id": str(TASK_ID),
        "subtask_id": None,
        "subtask_key": "summary",
        "subtask_title": "Summary",
        "worker_item_id": str(WORKER_ITEM_ID),
        "worker_item_key": "summary",
        "worker_item_title": "Summarize",
        "proposal_id": str(PROPOSAL_ID),
        "status": "pending",
        "resource_version": 3,
        "target_request_version": 8,
        "reason_code": "waiting_evidence",
        "summary": "The required evidence is ready for review.",
        "hold_id": str(HOLD_ID),
        "hold_resource_version": 4,
        "governance_mode": "paused",
        "available_actions": ["approve", "reject", "resume_automatic"],
        "deep_link": f"/workspaces/{WORKSPACE_ID}/worker-items/{WORKER_ITEM_ID}",
        "input_selection_id": str(SELECTION_ID),
        "input_selection_hash": "a" * 64,
    }


def input_selection_identity() -> dict[str, Any]:
    return {
        "selection_id": str(SELECTION_ID),
        "workspace_id": str(WORKSPACE_ID),
        "root_task_id": str(TASK_ID),
        "target_worker_item_id": str(WORKER_ITEM_ID),
        "target_request_version": 8,
        "selection_hash": "a" * 64,
    }


def approved_input_command() -> dict[str, Any]:
    client = ResultInputFakeClient()
    client.governance_worker = governance_worker_view()
    active = client.governance_worker["governance"]["active_proposal"]
    active["input_selection_id"] = str(SELECTION_ID)
    active["input_selection_hash"] = "a" * 64
    client.governance_proposal = v2_governance_proposal()
    client.input_selection = input_selection_identity()
    dispatcher = FakeDispatcher(
        governance_command_result(
            COMMAND_ID,
            aggregate_id=PROPOSAL_ID,
            data={"proposal_id": str(PROPOSAL_ID), "proposal_status": "approved"},
        )
    )
    bridge, dispatcher, _ = tools(
        client=client,
        dispatcher=dispatcher,
        governance=True,
        source="sidera_web",
    )

    async def scenario() -> dict[str, Any]:
        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_approve_collaboration_release(
                    None, str(PROPOSAL_ID), 3, True, None
                )
            )
        assert result["outcome"] == "ok"
        assert len(dispatcher.calls) == 1
        _, command = dispatcher.calls[0]
        assert isinstance(command, CommandIntent)
        return dict(command.payload)

    return asyncio.run(scenario())


def test_main_agent_approval_preserves_expected_selection() -> None:
    payload = approved_input_command()

    assert payload["expected_input_selection_id"] == str(SELECTION_ID)
    assert payload["expected_input_selection_hash"] == "a" * 64


def test_worker_result_inputs_query_is_bounded_and_actionable() -> None:
    async def scenario() -> None:
        client = ResultInputFakeClient()
        client.worker_result_inputs = worker_result_inputs_view()
        bridge, _, _ = tools(client=client)

        result = decode(
            await bridge.sidera_get_worker_result_inputs(None, str(WORKER_ITEM_ID))
        )

        assert result["outcome"] == "ok"
        assert result["resource_ref"] == f"worker-item:{WORKER_ITEM_ID}"
        assert result["web_url"].endswith(f"/worker-items/{WORKER_ITEM_ID}")
        data = result["data"]
        assert data["root_task_id"] == str(TASK_ID)
        assert data["subtask_key"] == "summary"
        assert data["worker_item_key"] == "summary"
        assert data["missing"] == []
        assert data["next_action"] == "review_release_proposal"
        assert data["groups"][0]["selected_count"] == 2
        assert data["groups"][0]["selected_sources"][0]["worker_item_key"] == (
            "research-a"
        )
        assert data["available_actions"][0]["target_id"] == str(
            UUID("6a000000-0000-0000-0000-000000000001")
        )
        encoded = json.dumps(result)
        assert "body_text" not in encoded
        assert "private report body" not in encoded
        assert "do-not-leak" not in encoded
        assert "must-not-leak" not in encoded
        assert "actor" not in encoded

    asyncio.run(scenario())


def test_worker_result_inputs_rejects_a_foreign_workspace_view() -> None:
    async def scenario() -> None:
        client = ResultInputFakeClient()
        client.worker_result_inputs = worker_result_inputs_view()
        client.worker_result_inputs["workspace_id"] = str(
            UUID("71000000-0000-0000-0000-000000000001")
        )
        bridge, _, _ = tools(client=client)

        result = decode(
            await bridge.sidera_get_worker_result_inputs(None, str(WORKER_ITEM_ID))
        )

        assert result["outcome"] == "failed"
        assert result["error_code"] == "invalid_control_plane_response"

    asyncio.run(scenario())


def test_worker_result_inputs_query_forwards_an_exact_execution_request() -> None:
    async def scenario() -> None:
        client = ResultInputFakeClient()
        client.worker_result_inputs = worker_result_inputs_view()
        bridge, _, _ = tools(client=client)

        result = decode(
            await bridge.sidera_get_worker_result_inputs(
                None, str(WORKER_ITEM_ID), str(EXECUTION_REQUEST_ID)
            )
        )

        assert result["outcome"] == "ok"
        assert client.result_input_calls == [(WORKER_ITEM_ID, EXECUTION_REQUEST_ID)]

    asyncio.run(scenario())


def test_approval_requires_the_selection_read_to_match_the_proposal() -> None:
    async def scenario() -> None:
        client = ResultInputFakeClient()
        client.governance_worker = governance_worker_view()
        client.governance_proposal = v2_governance_proposal()
        client.input_selection = input_selection_identity()
        client.input_selection["selection_hash"] = "b" * 64
        dispatcher = FakeDispatcher(
            governance_command_result(
                COMMAND_ID,
                aggregate_id=PROPOSAL_ID,
                data={"proposal_id": str(PROPOSAL_ID)},
            )
        )
        bridge, dispatcher, _ = tools(
            client=client,
            dispatcher=dispatcher,
            governance=True,
            source="sidera_web",
        )

        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_approve_collaboration_release(
                    None, str(PROPOSAL_ID), 3, True, None
                )
            )

        assert result["outcome"] == "conflict"
        assert result["error_code"] == "input_selection_conflict"
        assert dispatcher.calls == []

    asyncio.run(scenario())


def test_approval_rejects_a_governance_view_with_a_forged_selection() -> None:
    async def scenario() -> None:
        client = ResultInputFakeClient()
        client.governance_worker = governance_worker_view()
        active = client.governance_worker["governance"]["active_proposal"]
        active["input_selection_id"] = str(SELECTION_ID)
        active["input_selection_hash"] = "b" * 64
        client.governance_proposal = v2_governance_proposal()
        client.input_selection = input_selection_identity()
        dispatcher = FakeDispatcher(
            governance_command_result(
                COMMAND_ID,
                aggregate_id=PROPOSAL_ID,
                data={"proposal_id": str(PROPOSAL_ID)},
            )
        )
        bridge, dispatcher, _ = tools(
            client=client,
            dispatcher=dispatcher,
            governance=True,
            source="sidera_web",
        )

        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_approve_collaboration_release(
                    None, str(PROPOSAL_ID), 3, True, None
                )
            )

        assert result["outcome"] == "conflict"
        assert result["error_code"] == "input_selection_conflict"
        assert dispatcher.calls == []

    asyncio.run(scenario())
