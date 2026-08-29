import asyncio
import json
from typing import Any
from uuid import UUID

from agent.tools.base import ToolExecutionContext, tool_execution_context_scope

from sidera_task_bridge.client import (
    CommandExecution,
    CommandIntent,
    CommandResponse,
    DispatchResult,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.source_routes import SourceContextResolver
from sidera_task_bridge.state import BridgeStateStore
from sidera_task_bridge.tools import SideraTaskTools

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000010")
TASK_ID = UUID("00000000-0000-0000-0000-000000000020")
COMMAND_ID = UUID("00000000-0000-0000-0000-000000000030")
BASE_SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000040")
TRANSITION_ID = UUID("00000000-0000-0000-0000-000000000050")


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value


class FakeDispatcher:
    def __init__(self, result: DispatchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, object]] = []

    async def execute(self, resource_lock_key: str, command: object) -> DispatchResult:
        self.calls.append((resource_lock_key, command))
        return self.result


class FakeClient:
    def __init__(self) -> None:
        self.detail: dict[str, Any] = {}
        self.clarifications: tuple[dict[str, Any], ...] = ()
        self.proposal: dict[str, Any] = {}
        self.materialization: dict[str, Any] = {}
        self.overview: dict[str, Any] = {}
        self.task: dict[str, Any] = {}
        self.active_plan: dict[str, Any] = {}
        self.revision_diff: dict[str, Any] = {}
        self.transition: dict[str, Any] = {}

    async def get_planning_detail(self, _request_id: UUID) -> dict[str, Any]:
        return self.detail

    async def list_clarifications(
        self, _request_id: UUID
    ) -> tuple[dict[str, Any], ...]:
        return self.clarifications

    async def get_proposal(
        self, _request_id: UUID, _revision: int
    ) -> dict[str, Any]:
        return self.proposal

    async def get_materialization(self, _request_id: UUID) -> dict[str, Any]:
        return self.materialization

    async def get_task_overview(self, _task_id: UUID) -> dict[str, Any]:
        return self.overview

    async def get_task(self, _task_id: UUID) -> dict[str, Any]:
        return self.task

    async def get_active_plan(self, _task_id: UUID) -> dict[str, Any]:
        return self.active_plan

    async def get_revision_diff(self, _request_id: UUID) -> dict[str, Any]:
        return self.revision_diff

    async def get_plan_transition(self, _transition_id: UUID) -> dict[str, Any]:
        return self.transition


def config() -> TaskBridgeConfig:
    return TaskBridgeConfig.model_validate(
        {
            "workspace_id": WORKSPACE_ID,
            "planning_profile_id": PROFILE_ID,
            "planning_profile_version": 3,
        }
    )


def tools(
    *,
    dispatcher: FakeDispatcher | None = None,
    client: FakeClient | None = None,
    source: str = "telegram",
) -> tuple[SideraTaskTools, FakeDispatcher, FakeClient]:
    resolved_client = client or FakeClient()
    resolved_dispatcher = dispatcher or FakeDispatcher(
        DispatchResult(status="applied", command_id=COMMAND_ID)
    )
    return (
        SideraTaskTools(
            config(),
            client=resolved_client,
            dispatcher=resolved_dispatcher,
            state=BridgeStateStore(MemoryKV()),
            source_resolver=SourceContextResolver(
                source_ref_factory=lambda: "src_test"
            ),
            session_metadata_provider=lambda _key: {"source": source},
        ),
        resolved_dispatcher,
        resolved_client,
    )


def private_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        origin_channel="telegram",
        origin_chat_id="chat-42",
        origin_session_key="telegram:chat-42",
    )


def response_data(command_id: UUID) -> CommandExecution:
    return CommandExecution(
        status="applied",
        command_id=command_id,
        result=CommandResponse(
            command_id=command_id,
            aggregate_id=REQUEST_ID,
            aggregate_version=1,
            event_ids=(),
            data={
                "planning_request_id": str(REQUEST_ID),
                "planning_run_id": "00000000-0000-0000-0000-000000000011",
                "brief_revision": 1,
            },
        ),
    )


def decode(value: str) -> dict[str, Any]:
    return json.loads(value)


def test_submit_schema_exposes_only_business_fields() -> None:
    from agent.plugins.registry import MetadataKind, plugin_registry

    import plugin

    metadata = [
        item
        for item in plugin_registry.get_handlers_by_module_path(plugin.__name__)
        if item.kind is MetadataKind.TOOL
    ]
    assert {item.tool_name for item in metadata} == {
        "sidera_submit_brief",
        "sidera_get_planning_status",
        "sidera_get_task_status",
        "sidera_answer_clarifications",
        "sidera_request_proposal_revision",
        "sidera_confirm_proposal",
        "sidera_reject_proposal",
        "sidera_create_plan_revision",
        "sidera_get_plan_revision",
        "sidera_submit_plan_revision_feedback",
        "sidera_confirm_plan_revision",
        "sidera_reject_plan_revision",
        "sidera_cancel_plan_revision",
        "sidera_get_plan_transition",
        "sidera_search_knowledge",
        "sidera_get_knowledge_page",
        "sidera_submit_knowledge_candidate",
    }
    forbidden = {
        "workspace_id",
        "actor",
        "source_type",
        "source_ref",
        "command_id",
        "idempotency_key",
        "correlation_id",
        "control_plane_url",
        "planning_profile_id",
        "planning_profile_version",
    }
    for item in metadata:
        assert item.tool_schema is not None
        assert not forbidden.intersection(item.tool_schema["properties"])


def test_submit_maps_source_and_config_without_exposing_them_to_model() -> None:
    async def scenario() -> None:
        dispatcher = FakeDispatcher(
            DispatchResult(
                status="applied",
                command_id=COMMAND_ID,
                execution=response_data(COMMAND_ID),
            )
        )
        bridge, dispatcher, _ = tools(dispatcher=dispatcher)
        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_submit_brief(
                    None,
                    "Build an API",
                    ["Keep it local"],
                    ["Tests pass"],
                    ["brief:1"],
                    ["source code"],
                )
            )

        assert result["outcome"] == "ok"
        assert result["resource_ref"] == f"planning:{REQUEST_ID}"
        assert result["web_url"] == (
            f"http://127.0.0.1:4174/workspaces/{WORKSPACE_ID}/planning/{REQUEST_ID}"
        )
        resource_lock_key, command = dispatcher.calls[0]
        assert resource_lock_key == "planning:source:src_test"
        assert isinstance(command, CommandIntent)
        assert command.payload == {
            "profile_id": str(PROFILE_ID),
            "profile_version": 3,
            "objective": "Build an API",
            "constraints": ["Keep it local"],
            "acceptance_criteria": ["Tests pass"],
            "context_refs": ["brief:1"],
            "requested_outputs": ["source code"],
            "source_type": "akashic_private",
            "source_ref": "src_test",
        }

    asyncio.run(scenario())


def test_planning_status_returns_bounded_proposal_summary() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {
            "id": str(REQUEST_ID),
            "status": "proposal_ready",
            "version": 4,
            "current_brief_revision": 2,
            "current_proposal_revision": 1,
            "current_proposal_content_hash": "hash-1",
            "pending_clarifications": [],
        }
        client.proposal = {
            "revision": 1,
            "content_hash": "hash-1",
            "display": {
                "root_task": {"title": "Build API", "goal_summary": "A" * 500},
                "subtasks": [{"key": "one"}, {"key": "two"}],
                "worker_items": [{"key": "worker"}],
                "subtask_dependencies": [{"from": "one", "to": "two"}],
                "worker_item_dependencies": [],
                "decision_summary": {
                    "risks": [f"risk-{i}" for i in range(20)],
                    "estimated_cost_microunits": 99,
                    "estimated_runtime_seconds": 12,
                    "estimated_concurrency": 2,
                },
                "impact_policy": {"budget_level": "standard"},
            },
        }
        bridge, _, _ = tools(client=client)
        result = decode(await bridge.sidera_get_planning_status(None, str(REQUEST_ID)))

        assert result["outcome"] == "needs_user"
        data = result["data"]
        assert data["status"] == "proposal_ready"
        assert data["root_title"] == "Build API"
        assert data["subtask_count"] == 2
        assert data["worker_item_count"] == 1
        assert data["dependency_count"] == 1
        assert len(data["risks"]) == 10
        assert len(data["root_title"]) <= 200
        assert "display" not in data
        assert "goal_summary" not in data

    asyncio.run(scenario())


def test_planning_status_returns_needs_user_for_blocking_questions() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {"status": "needs_clarification", "version": 2}
        client.clarifications = tuple(
            {
                "id": str(UUID("00000000-0000-0000-0000-000000000040")),
                "question": "Which API?",
                "rationale": "The target is unclear.",
                "blocking": True,
                "options": ["REST", "GraphQL"],
            }
            for _ in range(25)
        )
        bridge, _, _ = tools(client=client)
        result = decode(await bridge.sidera_get_planning_status(None, str(REQUEST_ID)))

        assert result["outcome"] == "needs_user"
        assert len(result["data"]["blocking_questions"]) == 20
        assert result["data"]["blocking_questions"][0]["question"] == "Which API?"

    asyncio.run(scenario())


def test_task_status_returns_overview_without_percentage() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.overview = {
            "root_task_id": str(TASK_ID),
            "title": "Build API",
            "goal_summary": "Ship a local API",
            "current_milestone_key": "implementation",
            "materialized_count": 3,
            "planned_count": 5,
            "running_count": 1,
            "completed_count": 1,
            "failed_count": 0,
            "next_work_item_keys": ["worker-2"],
            "attention_items": [
                {
                    "key": "attention-1",
                    "kind": "blocked",
                    "title": "Needs input",
                    "summary": "Waiting for a decision",
                }
            ],
        }
        bridge, _, _ = tools(client=client)
        result = decode(await bridge.sidera_get_task_status(None, str(TASK_ID)))

        assert result["outcome"] == "ok"
        assert result["resource_ref"] == f"task:{TASK_ID}"
        assert result["web_url"] == (
            f"http://127.0.0.1:4174/workspaces/{WORKSPACE_ID}/tasks/{TASK_ID}"
        )
        assert result["data"]["counts"] == {
            "materialized": 3,
            "planned": 5,
            "running": 1,
            "completed": 1,
            "failed": 0,
        }
        assert "percentage" not in json.dumps(result)

    asyncio.run(scenario())


def test_invalid_resource_id_is_local_failed_without_client_call() -> None:
    async def scenario() -> None:
        client = FakeClient()
        bridge, _, _ = tools(client=client)
        result = decode(await bridge.sidera_get_task_status(None, "not-a-uuid"))
        assert result["outcome"] == "failed"
        assert result["error_code"] == "invalid_resource_id"

    asyncio.run(scenario())


def test_answer_clarifications_validates_and_dispatches_exact_target() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {"version": 4, "status": "needs_clarification"}
        dispatcher = FakeDispatcher(
            DispatchResult(
                status="applied",
                command_id=COMMAND_ID,
                execution=response_data(COMMAND_ID),
            )
        )
        bridge, dispatcher, _ = tools(client=client, dispatcher=dispatcher)
        answer_id = "00000000-0000-0000-0000-000000000040"
        result = decode(
            await bridge.sidera_answer_clarifications(
                None,
                str(REQUEST_ID),
                4,
                [{"clarification_id": answer_id, "answer": "REST"}],
            )
        )

        assert result["outcome"] == "ok"
        resource, command = dispatcher.calls[0]
        assert resource == f"planning:request:{REQUEST_ID}"
        assert isinstance(command, CommandIntent)
        assert command.target_type == "planning_request"
        assert command.target_id == REQUEST_ID
        assert command.expected_version == 4
        assert command.payload == {
            "answers": [{"clarification_id": answer_id, "answer": "REST"}]
        }

    asyncio.run(scenario())


def test_duplicate_or_empty_clarification_answers_fail_before_http() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {"version": 4, "status": "needs_clarification"}
        dispatcher = FakeDispatcher(
            DispatchResult(status="applied", command_id=COMMAND_ID)
        )
        bridge, dispatcher, _ = tools(client=client, dispatcher=dispatcher)
        answer_id = "00000000-0000-0000-0000-000000000040"
        result = decode(
            await bridge.sidera_answer_clarifications(
                None,
                str(REQUEST_ID),
                4,
                [
                    {"clarification_id": answer_id, "answer": ""},
                    {"clarification_id": answer_id, "answer": "GraphQL"},
                ],
            )
        )

        assert result["outcome"] == "failed"
        assert result["error_code"] == "invalid_answers"
        assert dispatcher.calls == []

    asyncio.run(scenario())


def test_stale_proposal_revision_is_conflict_without_dispatch() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {
            "version": 4,
            "current_proposal_revision": 2,
            "current_proposal_content_hash": "hash-current",
        }
        dispatcher = FakeDispatcher(
            DispatchResult(status="applied", command_id=COMMAND_ID)
        )
        bridge, dispatcher, _ = tools(client=client, dispatcher=dispatcher)
        result = decode(
            await bridge.sidera_request_proposal_revision(
                None,
                str(REQUEST_ID),
                4,
                1,
                "hash-old",
                "Change the worker split",
            )
        )

        assert result["outcome"] == "conflict"
        assert result["error_code"] == "proposal_conflict"
        assert dispatcher.calls == []

    asyncio.run(scenario())


def test_confirm_requires_explicit_user_confirmation_without_preflight() -> None:
    async def scenario() -> None:
        client = FakeClient()
        dispatcher = FakeDispatcher(
            DispatchResult(status="applied", command_id=COMMAND_ID)
        )
        bridge, dispatcher, _ = tools(client=client, dispatcher=dispatcher)
        result = decode(
            await bridge.sidera_confirm_proposal(
                None,
                str(REQUEST_ID),
                4,
                2,
                "hash-current",
                False,
            )
        )

        assert result["outcome"] == "needs_user"
        assert result["error_code"] == "explicit_confirmation_required"
        assert dispatcher.calls == []
        assert client.detail == {}

    asyncio.run(scenario())


def test_confirm_sends_exact_proposal_and_profile_snapshot() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {
            "version": 4,
            "current_proposal_revision": 2,
            "current_proposal_content_hash": "hash-current",
            "planning_profile_id": str(PROFILE_ID),
            "planning_profile_version": 3,
        }
        dispatcher = FakeDispatcher(
            DispatchResult(
                status="applied",
                command_id=COMMAND_ID,
                execution=response_data(COMMAND_ID),
            )
        )
        bridge, dispatcher, _ = tools(client=client, dispatcher=dispatcher)
        result = decode(
            await bridge.sidera_confirm_proposal(
                None,
                str(REQUEST_ID),
                4,
                2,
                "hash-current",
                True,
            )
        )

        assert result["outcome"] == "ok"
        _, command = dispatcher.calls[0]
        assert isinstance(command, CommandIntent)
        assert command.command_type == "ConfirmPlanningProposal"
        assert command.target_type == "planning_request"
        assert command.target_id == REQUEST_ID
        assert command.expected_version == 4
        assert command.payload == {
            "proposal_revision": 2,
            "proposal_content_hash": "hash-current",
            "profile_id": str(PROFILE_ID),
            "profile_version": 3,
        }
        assert "explicit_user_confirmation" not in command.payload

    asyncio.run(scenario())


def test_control_plane_conflict_maps_to_conflict_for_reject() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {
            "version": 4,
            "current_proposal_revision": 2,
            "current_proposal_content_hash": "hash-current",
        }
        dispatcher = FakeDispatcher(
            DispatchResult(
                status="rejected",
                command_id=COMMAND_ID,
                error_code="planning_conflict",
            )
        )
        bridge, _, _ = tools(client=client, dispatcher=dispatcher)
        result = decode(
            await bridge.sidera_reject_proposal(
                None,
                str(REQUEST_ID),
                4,
                2,
                "hash-current",
                "The proposal needs a different scope.",
            )
        )

        assert result["outcome"] == "conflict"
        assert result["error_code"] == "planning_conflict"

    asyncio.run(scenario())


def _revision_detail() -> dict[str, Any]:
    return {
        "id": str(REQUEST_ID),
        "kind": "revision",
        "status": "proposal_ready",
        "version": 4,
        "current_proposal_revision": 2,
        "current_proposal_content_hash": "proposal-hash",
        "planning_profile_id": str(PROFILE_ID),
        "planning_profile_version": 3,
        "revision_base": {
            "root_task_id": str(TASK_ID),
            "plan_snapshot_id": str(BASE_SNAPSHOT_ID),
            "plan_generation": 3,
            "plan_content_hash": "a" * 64,
            "root_task_version": 7,
        },
        "revision_freshness": {"base_fresh": True, "stale_reasons": []},
    }


def _revision_diff() -> dict[str, Any]:
    return {
        "planning_request_id": str(REQUEST_ID),
        "root_task_id": str(TASK_ID),
        "proposal_revision": 2,
        "proposal_content_hash": "proposal-hash",
        "diff_hash": "b" * 64,
        "base": {
            "plan_snapshot_id": str(BASE_SNAPSHOT_ID),
            "plan_generation": 3,
            "plan_content_hash": "a" * 64,
            "root_task_version": 7,
        },
        "target_descriptor_preview": {"target_generation": 4},
        "impact_counts": {
            "preserve": 1,
            "move": 0,
            "patch": 1,
            "continue": 0,
            "create": 0,
            "derive": 0,
            "retire": 0,
            "affected_branches": 1,
        },
        "risk_flags": ["worker_context_patch"],
        "subtask_changes": [
            {"path": "Task/Build > Subtask/API", "strategy": "preserve"}
        ],
        "worker_item_changes": [
            {"path": "Task/Build > Subtask/API > WorkerItem/Backend", "strategy": "patch"}
        ],
        "accepted_transition_policy": {
            "failure_policy": "block_affected_branch",
            "manual_gate": False,
        },
    }


def test_create_plan_revision_reads_current_base_and_excludes_chat_history() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.task = {"id": str(TASK_ID), "version": 7}
        client.active_plan = {
            "root_task_id": str(TASK_ID),
            "active_snapshot_id": str(BASE_SNAPSHOT_ID),
            "active_generation": 3,
            "active_snapshot_hash": "a" * 64,
        }
        dispatcher = FakeDispatcher(
            DispatchResult(
                status="applied",
                command_id=COMMAND_ID,
                execution=response_data(COMMAND_ID),
            )
        )
        bridge, dispatcher, _ = tools(client=client, dispatcher=dispatcher)
        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_create_plan_revision(
                    None,
                    str(TASK_ID),
                    "Change the backend split",
                    ["Keep the public API stable"],
                    ["Existing tests remain green"],
                )
            )

        assert result["outcome"] == "ok"
        assert result["resource_ref"] == f"revision:{REQUEST_ID}"
        assert result["web_url"] == (
            f"http://127.0.0.1:4174/workspaces/{WORKSPACE_ID}/tasks/"
            f"{TASK_ID}/revisions/{REQUEST_ID}"
        )
        resource, command = dispatcher.calls[0]
        assert resource == f"planning:root:{TASK_ID}"
        assert isinstance(command, CommandIntent)
        assert command.command_type == "SubmitPlanRevision"
        assert command.target_type == "task"
        assert command.target_id == TASK_ID
        assert command.expected_version == 7
        assert command.payload == {
            "root_task_id": str(TASK_ID),
            "expected_base_snapshot_id": str(BASE_SNAPSHOT_ID),
            "expected_base_generation": 3,
            "expected_base_content_hash": "a" * 64,
            "objective": "Change the backend split",
            "constraints": ["Keep the public API stable"],
            "acceptance_criteria": ["Existing tests remain green"],
            "planning_profile_id": str(PROFILE_ID),
            "planning_profile_version": 3,
        }
        encoded = json.dumps(command.payload)
        assert "chat-42" not in encoded
        assert "conversation" not in encoded

    asyncio.run(scenario())


def test_get_plan_revision_returns_bounded_diff_and_needs_confirmation() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = _revision_detail()
        client.revision_diff = _revision_diff()
        client.proposal = {
            "derived_diff": {"patch_body": "private patch body"},
            "display": {"private": "proposal transcript"},
        }
        bridge, _, _ = tools(client=client)
        result = decode(await bridge.sidera_get_plan_revision(None, str(REQUEST_ID)))

        assert result["outcome"] == "needs_user"
        assert result["resource_ref"] == f"revision:{REQUEST_ID}"
        assert result["web_url"].endswith(f"/tasks/{TASK_ID}/revisions/{REQUEST_ID}")
        assert result["data"]["root_task_id"] == str(TASK_ID)
        assert result["data"]["diff"]["diff_hash"] == "b" * 64
        assert result["data"]["diff"]["impact_counts"]["patch"] == 1
        assert "display" not in json.dumps(result)
        assert "patch_body" not in json.dumps(result)
        assert "private patch body" not in json.dumps(result)

    asyncio.run(scenario())


def test_confirm_plan_revision_sends_every_exact_fence_without_self_confirmation() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = _revision_detail()
        client.revision_diff = _revision_diff()
        dispatcher = FakeDispatcher(
            DispatchResult(
                status="applied",
                command_id=COMMAND_ID,
                execution=response_data(COMMAND_ID),
            )
        )
        bridge, dispatcher, _ = tools(client=client, dispatcher=dispatcher)
        result = decode(
            await bridge.sidera_confirm_plan_revision(
                None,
                str(REQUEST_ID),
                4,
                2,
                "proposal-hash",
                "b" * 64,
                str(BASE_SNAPSHOT_ID),
                "a" * 64,
                True,
            )
        )

        assert result["outcome"] == "ok"
        _, command = dispatcher.calls[0]
        assert isinstance(command, CommandIntent)
        assert command.command_type == "ConfirmPlanRevision"
        assert command.payload == {
            "expected_proposal_revision": 2,
            "expected_proposal_content_hash": "proposal-hash",
            "expected_diff_hash": "b" * 64,
            "expected_base_snapshot_id": str(BASE_SNAPSHOT_ID),
            "expected_base_content_hash": "a" * 64,
            "profile_id": str(PROFILE_ID),
            "profile_version": 3,
        }
        assert "explicit_user_confirmation" not in command.payload

    asyncio.run(scenario())


def test_revision_mutations_use_revision_commands_and_exact_version() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = _revision_detail()
        client.revision_diff = _revision_diff()
        dispatcher = FakeDispatcher(
            DispatchResult(
                status="applied",
                command_id=COMMAND_ID,
                execution=response_data(COMMAND_ID),
            )
        )
        bridge, dispatcher, _ = tools(client=client, dispatcher=dispatcher)

        feedback = decode(
            await bridge.sidera_submit_plan_revision_feedback(
                None, str(REQUEST_ID), 4, 2, "proposal-hash", "Move one worker"
            )
        )
        reject = decode(
            await bridge.sidera_reject_plan_revision(
                None, str(REQUEST_ID), 4, 2, "proposal-hash", "Scope is wrong"
            )
        )
        cancel = decode(
            await bridge.sidera_cancel_plan_revision(None, str(REQUEST_ID), 4)
        )

        assert feedback["outcome"] == "ok"
        assert reject["outcome"] == "ok"
        assert cancel["outcome"] == "ok"
        assert [
            (command.command_type, command.expected_version)
            for _, command in dispatcher.calls
            if isinstance(command, CommandIntent)
        ] == [
            ("RequestProposalRevision", 4),
            ("RejectPlanningProposal", 4),
            ("CancelPlanningRequest", 4),
        ]

    asyncio.run(scenario())


def test_plan_revision_surfaces_rejected_and_unknown_without_replay() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.task = {"id": str(TASK_ID), "version": 7}
        client.active_plan = {
            "root_task_id": str(TASK_ID),
            "active_snapshot_id": str(BASE_SNAPSHOT_ID),
            "active_generation": 3,
            "active_snapshot_hash": "a" * 64,
        }
        dispatcher = FakeDispatcher(
            DispatchResult(
                status="rejected",
                command_id=COMMAND_ID,
                error_code="planning_conflict",
            )
        )
        bridge, dispatcher, _ = tools(client=client, dispatcher=dispatcher)
        with tool_execution_context_scope(private_context()):
            rejected = decode(
                await bridge.sidera_create_plan_revision(
                    None,
                    str(TASK_ID),
                    "Change the backend split",
                    [],
                    ["Existing tests remain green"],
                )
            )

        dispatcher.result = DispatchResult(
            status="result_unknown", command_id=COMMAND_ID
        )
        with tool_execution_context_scope(private_context()):
            unknown = decode(
                await bridge.sidera_create_plan_revision(
                    None,
                    str(TASK_ID),
                    "Change the backend split",
                    [],
                    ["Existing tests remain green"],
                )
            )

        assert rejected["outcome"] == "conflict"
        assert rejected["error_code"] == "planning_conflict"
        assert rejected["resource_ref"] == f"task:{TASK_ID}"
        assert unknown["outcome"] == "result_unknown"
        assert unknown["error_code"] == "result_unknown"
        assert unknown["resource_ref"] == f"task:{TASK_ID}"
        assert len(dispatcher.calls) == 2

    asyncio.run(scenario())


def test_get_plan_transition_redacts_runtime_details_and_surfaces_blocked_unknown() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.transition = {
            "plan_transition_id": str(TRANSITION_ID),
            "workspace_id": str(WORKSPACE_ID),
            "root_task": {"id": str(TASK_ID), "title": "Build API", "path": "Task/Build API"},
            "from_generation": 3,
            "to_generation": 4,
            "status": "degraded",
            "version": 2,
            "as_of": "2026-08-29T00:00:00Z",
            "workers": [
                {
                    "worker_transition_id": str(UUID(int=51)),
                    "path": "Task/Build API > Subtask/API > WorkerItem/Backend",
                    "action": "patch",
                    "fallback_action": "continue",
                    "status": "blocked_unknown",
                    "failure_code": "gateway_outcome_unknown",
                    "source_execution_id": str(UUID(int=52)),
                    "source_session_id": str(UUID(int=53)),
                    "context": {
                        "current_context_version": 3,
                        "acknowledged_context_version": 2,
                        "target_context_version": 4,
                        "patch_id": str(UUID(int=54)),
                        "patch_hash": "private-patch-hash",
                        "patch_acknowledged": False,
                    },
                    "queued_input_disposition": {
                        "queued_count": 1,
                        "superseded_count": 2,
                    },
                    "operations": [{"operation_id": str(UUID(int=55)), "status": "failed"}],
                    "blocked_dependencies": [],
                }
            ],
            "blocked_dependencies": [],
        }
        bridge, _, _ = tools(client=client)
        result = decode(await bridge.sidera_get_plan_transition(None, str(TRANSITION_ID)))

        assert result["outcome"] == "needs_user"
        assert result["resource_ref"] == f"plan-transition:{TRANSITION_ID}"
        worker = result["data"]["workers"][0]
        assert worker["status"] == "blocked_unknown"
        assert worker["queued_input_count"] == 1
        assert "source_session_id" not in json.dumps(result)
        assert "private-patch-hash" not in json.dumps(result)
        assert "operations" not in json.dumps(result)

    asyncio.run(scenario())
