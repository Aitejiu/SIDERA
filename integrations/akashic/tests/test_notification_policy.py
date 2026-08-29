import asyncio
from typing import Any, Literal
from uuid import UUID

import pytest

from sidera_task_bridge.client import ControlPlaneRejected, TransportUnavailable
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.contracts import ProjectionChange
from sidera_task_bridge.notifications import (
    NotificationEvaluator,
    NotificationQueryUnavailable,
)
from sidera_task_bridge.state import BridgeStateStore

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000010")
TASK_ID = UUID("00000000-0000-0000-0000-000000000020")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000030")


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value


class FakeClient:
    def __init__(self) -> None:
        self.detail: dict[str, Any] = {}
        self.clarifications: tuple[dict[str, Any], ...] = ()
        self.materialization: dict[str, Any] = {}
        self.overview: dict[str, Any] = {}
        self.error: Exception | None = None

    async def get_planning_detail(self, _request_id: UUID) -> dict[str, Any]:
        if self.error:
            raise self.error
        return self.detail

    async def list_clarifications(
        self, _request_id: UUID
    ) -> tuple[dict[str, Any], ...]:
        if self.error:
            raise self.error
        return self.clarifications

    async def get_materialization(self, _request_id: UUID) -> dict[str, Any]:
        if self.error:
            raise self.error
        return self.materialization

    async def get_task_overview(self, _task_id: UUID) -> dict[str, Any]:
        if self.error:
            raise self.error
        return self.overview


def config() -> TaskBridgeConfig:
    return TaskBridgeConfig.model_validate(
        {
            "workspace_id": WORKSPACE_ID,
            "planning_profile_id": PROFILE_ID,
            "planning_profile_version": 1,
        }
    )


def change(
    cursor: int,
    projection_type: str,
    resource_id: UUID,
    change_type: str,
) -> ProjectionChange:
    return ProjectionChange.model_validate(
        {
            "workspace_id": str(WORKSPACE_ID),
            "cursor": cursor,
            "schema_version": 1,
            "projection_type": projection_type,
            "resource_id": str(resource_id),
            "change_type": change_type,
            "resource_version": cursor,
            "occurred_at": "2026-08-24T03:30:00Z",
            "correlation_id": str(CORRELATION_ID),
        }
    )


async def private_state(
    kind: Literal["planning_request", "task"], resource_id: UUID
) -> BridgeStateStore:
    state = BridgeStateStore(MemoryKV())
    await state.bind_resource_source(kind, resource_id, "akashic_private", "src_private")
    return state


def test_planning_policy_notifies_only_actionable_state_and_deduplicates_snapshots() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {
            "status": "needs_clarification",
            "version": 2,
            "source_type": "akashic_private",
            "source_ref": "src_private",
        }
        client.clarifications = (
            {
                "id": "clarification-1",
                "question": "Which API?",
                "rationale": "The target is unclear.",
                "blocking": True,
                "version": 1,
            },
        )
        state = await private_state("planning_request", REQUEST_ID)
        evaluator = NotificationEvaluator(config(), client=client, state=state)

        first = await evaluator.evaluate(
            change(1, "planning_request", REQUEST_ID, "planning_clarification"),
            mode="live",
        )
        assert first is not None and first.notice is not None
        assert first.notice.kind == "planning_clarification"
        await state.save_notification_snapshot(first.resource_key, first.snapshot)

        replay = await evaluator.evaluate(
            change(2, "planning_request", REQUEST_ID, "planning_request_updated"),
            mode="resync",
        )
        assert replay is not None and replay.notice is None

        client.detail = {
            **client.detail,
            "status": "proposal_ready",
            "version": 3,
            "current_proposal_revision": 2,
            "current_proposal_content_hash": "proposal-hash",
        }
        proposal = await evaluator.evaluate(
            change(3, "planning_request", REQUEST_ID, "planning_proposal_ready"),
            mode="live",
        )
        assert proposal is not None and proposal.notice is not None
        assert proposal.notice.kind == "planning_proposal_ready"

        client.detail = {**client.detail, "status": "running", "version": 4}
        running = await evaluator.evaluate(
            change(4, "planning_request", REQUEST_ID, "planning_run_updated"),
            mode="live",
        )
        assert running is not None and running.notice is None

    asyncio.run(scenario())


def test_planning_materialization_notifies_and_inherits_original_owner() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {
            "status": "materialized",
            "version": 5,
            "source_type": "akashic_private",
            "source_ref": "src_private",
        }
        client.materialization = {
            "root_task_id": str(TASK_ID),
            "deep_link": "http://127.0.0.1:4174/workspaces/ws/tasks/task",
        }
        state = await private_state("planning_request", REQUEST_ID)
        evaluator = NotificationEvaluator(config(), client=client, state=state)

        result = await evaluator.evaluate(
            change(5, "planning_request", REQUEST_ID, "planning_materialized"),
            mode="resync",
        )
        assert result is not None and result.notice is not None
        assert result.notice.kind == "planning_materialized"
        binding = (await state.snapshot()).resource_sources[f"task:{TASK_ID}"]
        assert binding.source_ref == "src_private"
        assert result.notice.web_url.endswith(f"/tasks/{TASK_ID}")

    asyncio.run(scenario())


def test_revision_proposal_notification_uses_bound_private_route_and_nested_task_link() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {
            "kind": "revision",
            "status": "proposal_ready",
            "version": 3,
            "source_type": "plan_revision",
            "source_ref": "revision-command-id",
            "revision_base": {
                "root_task_id": str(TASK_ID),
                "plan_snapshot_id": str(UUID(int=41)),
                "plan_generation": 2,
                "plan_content_hash": "a" * 64,
                "root_task_version": 6,
            },
            "current_proposal_revision": 1,
            "current_proposal_content_hash": "proposal-hash",
        }
        state = await private_state("planning_request", REQUEST_ID)
        evaluator = NotificationEvaluator(config(), client=client, state=state)

        result = await evaluator.evaluate(
            change(15, "planning_request", REQUEST_ID, "planning_proposal_ready"),
            mode="live",
        )

        assert result is not None and result.notice is not None
        assert result.notice.kind == "planning_proposal_ready"
        assert result.notice.web_url.endswith(
            f"/tasks/{TASK_ID}/revisions/{REQUEST_ID}"
        )
        assert "revision-command-id" not in result.notice.message

    asyncio.run(scenario())


def test_task_policy_prioritizes_attention_and_excludes_worker_details() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.overview = {
            "root_task_id": str(TASK_ID),
            "title": "Release API",
            "subtask_count": 3,
            "completed_subtask_count": 1,
            "failed_subtask_count": 0,
            "generation": 2,
            "attention_items": [
                {
                    "kind": "blocked",
                    "task_title": "Release API",
                    "subtask_title": "Contract tests",
                    "reason": "Waiting for decision",
                    "resource_id": str(UUID("00000000-0000-0000-0000-000000000040")),
                    "resource_version": 2,
                    "requires_operator": True,
                    "worker_display_name": "secret-worker-name",
                }
            ],
        }
        state = await private_state("task", TASK_ID)
        evaluator = NotificationEvaluator(config(), client=client, state=state)

        result = await evaluator.evaluate(
            change(6, "task_overview", TASK_ID, "task_attention_changed"),
            mode="live",
        )
        assert result is not None and result.notice is not None
        assert result.notice.kind == "task_blocked"
        assert "Release API" in result.notice.message
        assert "Contract tests" in result.notice.message
        assert "secret-worker-name" not in result.notice.message
        assert "Waiting for decision" in result.notice.message
        assert len(result.notice.message) <= 1200
        assert "percentage" not in result.notice.message

        await state.save_notification_snapshot(result.resource_key, result.snapshot)
        replay = await evaluator.evaluate(
            change(7, "task_overview", TASK_ID, "task_overview_updated"),
            mode="resync",
        )
        assert replay is not None and replay.notice is None

    asyncio.run(scenario())


def test_task_policy_handles_failed_and_completed_root_without_historical_milestone_burst() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.overview = {
            "root_task_id": str(TASK_ID),
            "title": "Release API",
            "subtask_count": 2,
            "completed_subtask_count": 2,
            "failed_subtask_count": 0,
            "generation": 3,
            "attention_items": [],
            "milestones": [
                {"key": "m1", "title": "Build", "state": "completed"},
                {"key": "m2", "title": "Ship", "state": "completed"},
            ],
        }
        state = await private_state("task", TASK_ID)
        evaluator = NotificationEvaluator(config(), client=client, state=state)
        result = await evaluator.evaluate(
            change(8, "task_overview", TASK_ID, "task_completed"),
            mode="resync",
        )
        assert result is not None and result.notice is not None
        assert result.notice.kind == "task_completed"
        await state.save_notification_snapshot(result.resource_key, result.snapshot)

        live_replay = await evaluator.evaluate(
            change(9, "task_overview", TASK_ID, "task_milestone_completed"),
            mode="resync",
        )
        assert live_replay is not None and live_replay.notice is None

        client.overview = {
            **client.overview,
            "completed_subtask_count": 1,
            "attention_items": [
                {
                    "kind": "failed",
                    "task_title": "Release API",
                    "subtask_title": "Ship",
                    "reason": "Acceptance failed",
                    "resource_id": str(UUID("00000000-0000-0000-0000-000000000041")),
                    "resource_version": 4,
                }
            ],
        }
        failed = await evaluator.evaluate(
            change(10, "task_overview", TASK_ID, "task_failed"),
            mode="live",
        )
        assert failed is not None and failed.notice is not None
        assert failed.notice.kind == "task_failed"

    asyncio.run(scenario())


def test_notification_id_and_message_are_safe_and_source_owner_is_strict() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.detail = {
            "status": "proposal_ready",
            "version": 2,
            "source_type": "sidera_web_chat",
            "source_ref": "web-thread-secret",
            "error": "raw exception token=secret-token",
        }
        state = await private_state("planning_request", REQUEST_ID)
        evaluator = NotificationEvaluator(config(), client=client, state=state)
        assert await evaluator.evaluate(
            change(11, "planning_request", REQUEST_ID, "planning_proposal_ready"),
            mode="live",
        ) is None

        client.detail = {**client.detail, "source_type": "akashic_private", "source_ref": "other"}
        with pytest.raises(ValueError, match="source_binding_mismatch"):
            await evaluator.evaluate(
                change(12, "planning_request", REQUEST_ID, "planning_proposal_ready"),
                mode="live",
            )

    asyncio.run(scenario())


def test_control_plane_unavailable_is_retryable_and_not_undeliverable() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.error = TransportUnavailable("secret endpoint")
        state = await private_state("planning_request", REQUEST_ID)
        evaluator = NotificationEvaluator(config(), client=client, state=state)
        with pytest.raises(NotificationQueryUnavailable):
            await evaluator.evaluate(
                change(13, "planning_request", REQUEST_ID, "planning_proposal_ready"),
                mode="live",
            )

        client.error = ControlPlaneRejected("not_found", {})
        missing = await evaluator.evaluate(
            change(14, "planning_request", REQUEST_ID, "planning_proposal_ready"),
            mode="live",
        )
        assert missing is None

    asyncio.run(scenario())
