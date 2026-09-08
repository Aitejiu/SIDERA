from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from tests.e2e.basic_acceptance.client import BasicAcceptanceClient
from tests.e2e.basic_acceptance.contracts import PlanningBrief
from tests.e2e.basic_acceptance.planning import (
    PlanningClients,
    PlanningEvidenceError,
    create_confirmed_plan,
)

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000002")
PLANNING_REQUEST_ID = UUID("20000000-0000-0000-0000-000000000001")
THREAD_ID = "thread-basic-planning"
TURN_ID = "turn-basic-planning"
PROFILE_ID = UUID("30000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")
PLANNING_RUN_ID = UUID("50000000-0000-0000-0000-000000000001")
COMMAND_ID = UUID("60000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("70000000-0000-0000-0000-000000000001")
PROPOSAL_HASH = "a" * 64


def _brief() -> PlanningBrief:
    return PlanningBrief(
        objective="Implement validation, formatting, and integration in the acceptance repository.",
        constraints=("Keep validation and formatting independent.",),
        acceptance_criteria=("Run the repository unittest suite.",),
        context_refs=("repo:acceptance",),
        requested_outputs=("validated output", "integration report"),
    )


def _manifest() -> dict[str, object]:
    return {
        "run_id": str(uuid4()),
        "workspace_id": str(WORKSPACE_ID),
        "main_agent_gateway_url": "http://gateway.test",
        "control_plane_url": "http://control-plane.test",
    }


def _response_body(*, status: str = "proposal_ready") -> dict[str, object]:
    return {
        "id": str(PLANNING_REQUEST_ID),
        "workspace_id": str(WORKSPACE_ID),
        "status": status,
        "version": 4,
        "current_proposal_revision": 1,
        "current_proposal_content_hash": PROPOSAL_HASH,
        "planning_profile_id": str(PROFILE_ID),
        "planning_profile_version": 1,
        "active_run": {"id": str(PLANNING_RUN_ID), "status": "succeeded"},
        "pending_clarifications": [],
    }


def _proposal_body(*, include_independent_workers: bool = True) -> dict[str, object]:
    workers = [
        {"key": "validation", "subtask_key": "implementation"},
        {"key": "formatting", "subtask_key": "implementation"},
        {"key": "integration", "subtask_key": "delivery"},
    ]
    dependencies = [
        {
            "upstream_worker_item_key": "validation",
            "downstream_worker_item_key": "integration",
        },
        {
            "upstream_worker_item_key": "formatting",
            "downstream_worker_item_key": "integration",
        },
    ]
    if not include_independent_workers:
        dependencies.append(
            {
                "upstream_worker_item_key": "validation",
                "downstream_worker_item_key": "formatting",
            }
        )
    return {
        "id": str(PROPOSAL_ID),
        "planning_request_id": str(PLANNING_REQUEST_ID),
        "revision": 1,
        "brief_revision": 1,
        "planning_run_id": str(PLANNING_RUN_ID),
        "proposal_schema_version": 6,
        "kind": "initial",
        "profile_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "content_hash": PROPOSAL_HASH,
        "display": {
            "subtasks": [
                {"key": "implementation"},
                {"key": "delivery"},
            ],
            "worker_items": workers,
            "worker_item_dependencies": dependencies,
        },
        "collaboration": {"mode": "parallel_then_integrate", "input_groups": []},
    }


def _clients(handler):
    transport = httpx.MockTransport(handler)
    return PlanningClients(
        main_agent=BasicAcceptanceClient("http://gateway.test", transport=transport),
        control_plane=BasicAcceptanceClient("http://control-plane.test", transport=transport),
    )


def test_planning_uses_gateway_then_public_control_plane_confirmation() -> None:
    calls: list[tuple[str, str]] = []
    turn_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn_reads
        calls.append((request.method, request.url.host + request.url.path))
        if request.url.host == "gateway.test" and request.url.path.endswith("/threads"):
            return httpx.Response(
                201,
                json={"id": THREAD_ID, "title": "basic acceptance", "turns": []},
            )
        if request.url.host == "gateway.test" and request.method == "POST":
            return httpx.Response(202, json={"id": TURN_ID, "threadId": THREAD_ID, "status": "queued"})
        if request.url.host == "gateway.test" and request.method == "GET":
            turn_reads += 1
            return httpx.Response(
                200,
                json={
                    "id": TURN_ID,
                    "threadId": THREAD_ID,
                    "status": "completed",
                    "items": [
                        {
                            "id": "tool-1",
                            "type": "toolCall",
                            "data": {
                                "name": "sidera_submit_brief",
                                "result": {
                                    "planning_request_id": str(PLANNING_REQUEST_ID)
                                },
                            },
                        }
                    ],
                },
            )
        if request.url.host == "control-plane.test" and request.method == "GET":
            if "/proposals/1" in request.url.path:
                return httpx.Response(200, json=_proposal_body())
            return httpx.Response(200, json=_response_body())
        if request.url.host == "control-plane.test" and request.method == "POST":
            payload = json.loads(request.content)
            assert payload["command_type"] == "ConfirmPlanningProposal"
            assert payload["actor"]["id"] == "test-operator"
            assert payload["target_id"] == str(PLANNING_REQUEST_ID)
            return httpx.Response(
                200,
                json={
                    "command_id": str(COMMAND_ID),
                    "status": "applied",
                    "data": {"root_task_id": str(ROOT_TASK_ID)},
                },
            )
        return httpx.Response(404, json={"error": "unexpected route"})

    clients = _clients(handler)
    try:
        evidence = create_confirmed_plan(clients, _brief(), _manifest())
    finally:
        clients.close()

    assert evidence.planning_request_id == PLANNING_REQUEST_ID
    assert evidence.root_task_id == ROOT_TASK_ID
    assert evidence.confirmed_by == "test-operator"
    assert evidence.pre_confirmation_status == "proposal_ready"
    assert evidence.post_confirmation_status == "materialized"
    assert turn_reads == 1
    assert calls.index(("GET", "control-plane.test/api/v1/workspaces/10000000-0000-0000-0000-000000000002/planning-requests/20000000-0000-0000-0000-000000000001")) < calls.index(("POST", "control-plane.test/api/v1/commands"))


def test_missing_main_agent_submit_brief_does_not_confirm() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gateway.test" and request.method == "POST":
            if request.url.path.endswith("/threads"):
                return httpx.Response(201, json={"id": THREAD_ID})
            return httpx.Response(202, json={"id": TURN_ID, "threadId": THREAD_ID})
        if request.url.host == "gateway.test":
            return httpx.Response(
                200,
                json={"id": TURN_ID, "threadId": THREAD_ID, "status": "completed", "items": []},
            )
        return httpx.Response(404)

    clients = _clients(handler)
    try:
        with pytest.raises(PlanningEvidenceError, match="main_agent_submit_brief_missing"):
            create_confirmed_plan(clients, _brief(), _manifest())
    finally:
        clients.close()


def test_dependency_shape_must_keep_two_implementation_workers_parallel() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gateway.test" and request.method == "POST":
            if request.url.path.endswith("/threads"):
                return httpx.Response(201, json={"id": THREAD_ID})
            return httpx.Response(202, json={"id": TURN_ID, "threadId": THREAD_ID})
        if request.url.host == "gateway.test":
            return httpx.Response(
                200,
                json={
                    "id": TURN_ID,
                    "threadId": THREAD_ID,
                    "status": "completed",
                    "items": [{"type": "toolCall", "data": {"name": "sidera_submit_brief", "result": {"planning_request_id": str(PLANNING_REQUEST_ID)}}}],
                },
            )
        if request.url.host == "control-plane.test" and "/proposals/1" in request.url.path:
            return httpx.Response(200, json=_proposal_body(include_independent_workers=False))
        if request.url.host == "control-plane.test":
            return httpx.Response(200, json=_response_body())
        return httpx.Response(404)

    clients = _clients(handler)
    try:
        with pytest.raises(PlanningEvidenceError, match="implementation_workers_not_parallel"):
            create_confirmed_plan(clients, _brief(), _manifest())
    finally:
        clients.close()
