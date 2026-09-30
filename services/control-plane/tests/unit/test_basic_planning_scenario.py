from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from tests.e2e.basic_acceptance.client import BasicAcceptanceClient
from tests.e2e.basic_acceptance.contracts import PlanningBrief
from tests.e2e.basic_acceptance.planning import (
    PlanningClients,
    PlanningEvidenceError,
    _deadline,
    create_confirmed_plan,
    observe_confirmed_plan,
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
        "confirmation_command_id": str(COMMAND_ID),
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
    confirmed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal confirmed, turn_reads
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
                        {"type": "message", "data": {"text": "当前仍未确认计划"}},
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
            if f"/commands/{COMMAND_ID}" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "command_id": str(COMMAND_ID),
                        "command_type": "ConfirmPlanningProposal",
                        "target_type": "planning_request",
                        "target_id": str(PLANNING_REQUEST_ID),
                        "status": "applied",
                        "result": {"data": {"root_task_id": str(ROOT_TASK_ID)}},
                    },
                )
            return httpx.Response(
                200,
                json=_response_body(
                    status="materialized" if confirmed else "proposal_ready"
                ),
            )
        if request.url.host == "control-plane.test" and request.method == "POST":
            confirmed = True
            payload = json.loads(request.content)
            assert payload["command_type"] == "ConfirmPlanningProposal"
            assert payload["actor"]["id"] == "test-operator"
            assert payload["target_id"] == str(PLANNING_REQUEST_ID)
            return httpx.Response(
                200,
                json={
                    "command_id": str(COMMAND_ID),
                    "status": "applied",
                    "data": {"root_task_id": str(uuid4())},
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
    assert (
        "GET",
        f"control-plane.test/api/v1/workspaces/{WORKSPACE_ID}/commands/{COMMAND_ID}",
    ) in calls


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


def test_planning_clarification_does_not_confirm() -> None:
    command_posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal command_posts
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
                    "items": [
                        {
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
        if request.url.host == "control-plane.test" and request.method == "POST":
            command_posts += 1
            raise AssertionError("clarification must not be confirmed")
        if request.url.host == "control-plane.test":
            body = _response_body(status="waiting_for_user")
            body["pending_clarifications"] = [{"id": str(uuid4()), "blocking": True}]
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    clients = _clients(handler)
    try:
        with pytest.raises(PlanningEvidenceError, match="planning_clarification_required"):
            create_confirmed_plan(clients, _brief(), _manifest())
    finally:
        clients.close()

    assert command_posts == 0


def test_proposal_hash_mismatch_never_confirms() -> None:
    command_posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal command_posts
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
                    "items": [
                        {
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
        if request.url.host == "control-plane.test" and request.method == "POST":
            command_posts += 1
            raise AssertionError("hash mismatch must not be confirmed")
        if request.url.host == "control-plane.test" and "/proposals/1" in request.url.path:
            proposal = _proposal_body()
            proposal["content_hash"] = "b" * 64
            return httpx.Response(200, json=proposal)
        if request.url.host == "control-plane.test":
            return httpx.Response(200, json=_response_body())
        return httpx.Response(404)

    clients = _clients(handler)
    try:
        with pytest.raises(PlanningEvidenceError, match="proposal_hash_mismatch"):
            create_confirmed_plan(clients, _brief(), _manifest())
    finally:
        clients.close()

    assert command_posts == 0


def test_observe_confirmed_plan_is_read_only() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method != "GET":
            raise AssertionError("read-only planning observation issued a write")
        if request.url.host == "gateway.test":
            return httpx.Response(
                200,
                json={
                    "id": TURN_ID,
                    "threadId": THREAD_ID,
                    "status": "completed",
                    "items": [
                        {
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
        if request.url.path.endswith(f"/commands/{COMMAND_ID}"):
            payload = {
                "proposal_revision": 1,
                "proposal_content_hash": PROPOSAL_HASH,
                "profile_id": str(PROFILE_ID),
                "profile_version": 1,
            }
            return httpx.Response(
                200,
                json={
                    "command_id": str(COMMAND_ID),
                    "command_type": "ConfirmPlanningProposal",
                    "target_type": "planning_request",
                    "target_id": str(PLANNING_REQUEST_ID),
                    "status": "applied",
                    "expected_version": 4,
                    "payload": payload,
                    "payload_sha256": hashlib.sha256(
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "result": {
                        "command_id": str(COMMAND_ID),
                        "aggregate_id": str(ROOT_TASK_ID),
                        "aggregate_version": 1,
                        "event_ids": [],
                        "data": {
                            "root_task_id": str(ROOT_TASK_ID),
                            "confirmed_by": "test-operator",
                            "pre_confirmation_status": "proposal_ready",
                        },
                    },
                    "error_code": None,
                },
            )
        if "/proposals/1" in request.url.path:
            return httpx.Response(200, json=_proposal_body())
        return httpx.Response(200, json=_response_body(status="materialized"))

    manifest = {
        **_manifest(),
        "confirmed_plan": {
            "root_task_id": str(uuid4()),
            "confirmation_command_id": str(uuid4()),
            "confirmed_by": "untrusted-manifest-operator",
            "pre_confirmation_status": "proposal_ready",
        },
    }
    clients = _clients(handler)
    try:
        evidence = observe_confirmed_plan(
            clients,
            thread_id=THREAD_ID,
            turn_id=TURN_ID,
            manifest=manifest,
        )
    finally:
        clients.close()

    assert evidence.root_task_id == ROOT_TASK_ID
    assert evidence.confirmation_command_id == COMMAND_ID
    assert evidence.confirmed_by == "test-operator"
    assert evidence.post_confirmation_status == "materialized"
    assert calls and all(method == "GET" for method, _ in calls)
    assert any(path.endswith(f"/commands/{COMMAND_ID}") for _, path in calls)


def test_observe_confirmed_plan_rejects_command_that_does_not_match_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gateway.test":
            return httpx.Response(
                200,
                json={
                    "id": TURN_ID,
                    "threadId": THREAD_ID,
                    "status": "completed",
                    "items": [
                        {
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
        if request.url.path.endswith(f"/commands/{COMMAND_ID}"):
            return httpx.Response(
                200,
                json={
                    "command_id": str(COMMAND_ID),
                    "command_type": "ConfirmPlanningProposal",
                    "target_type": "task",
                    "target_id": str(ROOT_TASK_ID),
                    "status": "applied",
                    "expected_version": 4,
                    "payload": {
                        "proposal_revision": 99,
                        "proposal_content_hash": "b" * 64,
                        "profile_id": str(PROFILE_ID),
                        "profile_version": 2,
                    },
                },
            )
        if "/proposals/1" in request.url.path:
            return httpx.Response(200, json=_proposal_body())
        return httpx.Response(200, json=_response_body(status="materialized"))

    manifest = {
        **_manifest(),
        "confirmed_plan": {
            "root_task_id": str(ROOT_TASK_ID),
            "confirmation_command_id": str(COMMAND_ID),
            "confirmed_by": "test-operator",
            "pre_confirmation_status": "proposal_ready",
        },
    }
    clients = _clients(handler)
    try:
        with pytest.raises(PlanningEvidenceError, match="confirmation_command_target_mismatch"):
            observe_confirmed_plan(
                clients,
                thread_id=THREAD_ID,
                turn_id=TURN_ID,
                manifest=manifest,
            )
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


def test_planning_deadline_preserves_one_hour_scenario_limit() -> None:
    remaining = _deadline(
        {"scenarios": {"profile": {"max_seconds": 3600}}}
    ) - time.monotonic()

    assert 3599 <= remaining <= 3601
