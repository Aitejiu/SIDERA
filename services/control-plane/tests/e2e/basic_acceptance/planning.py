"""Public-path planning confirmation harness for the basic acceptance run."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .client import BasicAcceptanceClient
from .contracts import ConfirmedPlanEvidence, PlanningBrief


@dataclass(frozen=True, slots=True)
class PlanningClients:
    main_agent: BasicAcceptanceClient
    control_plane: BasicAcceptanceClient

    def close(self) -> None:
        self.main_agent.close()
        self.control_plane.close()


class PlanningEvidenceError(ValueError):
    """A public planning fact did not satisfy the basic acceptance contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def create_confirmed_plan(
    client: PlanningClients,
    brief: PlanningBrief,
    manifest: Mapping[str, object],
) -> ConfirmedPlanEvidence:
    """Submit a brief through Main Agent, inspect a Proposal, then confirm it."""

    run_id = _uuid(manifest.get("run_id"), "run_id")
    workspace_id = _uuid(manifest.get("workspace_id"), "workspace_id")
    thread = _accepted_body(
        client.main_agent.post_json(
            "/main-agent/v1/threads",
            {"title": "SIDERA basic acceptance", "contextRefs": list(brief.context_refs)},
        ),
        "main_agent_thread_create_failed",
    )
    thread_id = _text(thread.get("id"), "main_agent_thread_id")
    turn = _accepted_body(
        client.main_agent.post_json(
            f"/main-agent/v1/threads/{thread_id}/turns",
            {"input": _brief_prompt(brief)},
        ),
        "main_agent_turn_start_failed",
    )
    turn_id = _text(turn.get("id"), "main_agent_turn_id")
    turn_outcome = client.main_agent.wait_for(
        f"/main-agent/v1/threads/{thread_id}/turns/{turn_id}",
        lambda body: body.get("status") in {"completed", "failed", "cancelled"},
        time.monotonic() + 5.0,
    )
    turn_body = _accepted_body(turn_outcome, "main_agent_turn_unavailable")
    if turn_body.get("status") != "completed":
        raise PlanningEvidenceError("main_agent_turn_not_completed")
    planning_request_id = _extract_planning_request_id(turn_body)
    if planning_request_id is None:
        raise PlanningEvidenceError("main_agent_submit_brief_missing")

    detail_path = (
        f"/api/v1/workspaces/{workspace_id}/planning-requests/{planning_request_id}"
    )
    detail = _accepted_body(
        client.control_plane.get_json(detail_path),
        "planning_request_read_failed",
    )
    pre_confirmation_status = _text(detail.get("status"), "planning_status")
    if pre_confirmation_status not in {
        "proposal_ready",
        "awaiting_confirmation",
        "needs_confirmation",
    }:
        raise PlanningEvidenceError("proposal_not_waiting_for_confirmation")
    proposal_revision = _positive_int(detail.get("current_proposal_revision"))
    proposal_hash = _hash(detail.get("current_proposal_content_hash"))
    request_version = _positive_int(detail.get("version"))
    profile_id = _uuid(detail.get("planning_profile_id"), "planning_profile_id")
    profile_version = _positive_int(detail.get("planning_profile_version"))
    if proposal_revision is None or proposal_hash is None:
        raise PlanningEvidenceError("proposal_identity_missing")
    if request_version is None or profile_version is None:
        raise PlanningEvidenceError("planning_version_missing")

    proposal = _accepted_body(
        client.control_plane.get_json(
            f"{detail_path}/proposals/{proposal_revision}"
        ),
        "planning_proposal_read_failed",
    )
    if _hash(proposal.get("content_hash")) != proposal_hash:
        raise PlanningEvidenceError("proposal_hash_mismatch")
    proposal_id = _uuid(proposal.get("id"), "proposal_id")
    _validate_worker_shape(proposal, brief)

    command_id = _uuid4()
    confirmation = client.control_plane.command(
        {
            "command_id": str(command_id),
            "workspace_id": str(workspace_id),
            "command_type": "ConfirmPlanningProposal",
            "target_type": "planning_request",
            "target_id": str(planning_request_id),
            "expected_version": request_version,
            "idempotency_key": f"basic-planning-confirm:{planning_request_id}:{proposal_revision}",
            "correlation_id": str(run_id),
            "actor": {"type": "user", "id": "test-operator"},
            "payload": {
                "proposal_revision": proposal_revision,
                "proposal_content_hash": proposal_hash,
                "profile_id": str(profile_id),
                "profile_version": profile_version,
            },
        }
    )
    confirmation_body = _accepted_body(confirmation, "planning_confirmation_failed")
    root_task_id = _find_uuid(confirmation_body, "root_task_id")
    if root_task_id is None:
        raise PlanningEvidenceError("confirmation_root_task_missing")
    return ConfirmedPlanEvidence(
        run_id=run_id,
        workspace_id=workspace_id,
        main_agent_thread_id=thread_id,
        main_agent_turn_id=turn_id,
        planning_request_id=planning_request_id,
        proposal_id=proposal_id,
        proposal_revision=proposal_revision,
        proposal_content_hash=proposal_hash,
        confirmation_command_id=confirmation.command_id or command_id,
        root_task_id=root_task_id,
        confirmed_by="test-operator",
        pre_confirmation_status=pre_confirmation_status,
        post_confirmation_status="materialized",
        check_codes=(
            "main_agent_turn_observed",
            "planning_request_observed_before_confirmation",
            "proposal_hash_observed",
            "parallel_implementation_workers_observed",
            "explicit_confirmation_command_applied",
        ),
    )


def _brief_prompt(brief: PlanningBrief) -> str:
    return json.dumps(
        {
            "objective": brief.objective,
            "constraints": list(brief.constraints),
            "acceptance_criteria": list(brief.acceptance_criteria),
            "context_refs": list(brief.context_refs),
            "requested_outputs": list(brief.requested_outputs),
            "require_proposal_before_execution": True,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _validate_worker_shape(proposal: Mapping[str, object], brief: PlanningBrief) -> None:
    worker_items = _find_list(proposal, "worker_items")
    dependencies = _find_list(proposal, "worker_item_dependencies")
    if worker_items is None or dependencies is None:
        raise PlanningEvidenceError("proposal_worker_shape_missing")
    worker_keys = {
        _mapping_text(item, "key") or _mapping_text(item, "worker_item_key")
        for item in worker_items
        if isinstance(item, Mapping)
    }
    if not set(brief.implementation_worker_keys).issubset(worker_keys) or (
        brief.integration_worker_key not in worker_keys
    ):
        raise PlanningEvidenceError("proposal_worker_shape_incomplete")
    edges = {
        (
            _mapping_text(item, "upstream_worker_item_key"),
            _mapping_text(item, "downstream_worker_item_key"),
        )
        for item in dependencies
        if isinstance(item, Mapping)
    }
    implementation = set(brief.implementation_worker_keys)
    if any((left, right) in edges or (right, left) in edges for left in implementation for right in implementation if left != right):
        raise PlanningEvidenceError("implementation_workers_not_parallel")
    if any(
        (worker_key, brief.integration_worker_key) not in edges
        for worker_key in brief.implementation_worker_keys
    ):
        raise PlanningEvidenceError("integration_dependency_missing")


def _extract_planning_request_id(turn: Mapping[str, object]) -> UUID | None:
    items = turn.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return None
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != "toolCall":
            continue
        data = item.get("data")
        if not isinstance(data, Mapping) or data.get("name") != "sidera_submit_brief":
            continue
        found = _find_uuid(data, "planning_request_id")
        if found is not None:
            return found
    return None


def _find_list(value: object, key: str) -> list[object] | None:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return candidate
        for nested in value.values():
            found = _find_list(nested, key)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            found = _find_list(nested, key)
            if found is not None:
                return found
    return None


def _find_uuid(value: object, key: str) -> UUID | None:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        try:
            if candidate is not None:
                return UUID(str(candidate))
        except ValueError:
            pass
        for nested in value.values():
            found = _find_uuid(nested, key)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            found = _find_uuid(nested, key)
            if found is not None:
                return found
    return None


def _accepted_body(outcome: Any, code: str) -> dict[str, object]:
    if outcome.status not in {"accepted", "observed"} or outcome.body is None:
        raise PlanningEvidenceError(code)
    return outcome.body


def _uuid(value: object, name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanningEvidenceError(f"{name}_invalid") from exc


def _uuid4() -> UUID:
    from uuid import uuid4

    return uuid4()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningEvidenceError(f"{name}_missing")
    return value.strip()


def _hash(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value) else None


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _mapping_text(value: Mapping[str, object], key: str) -> str | None:
    candidate = value.get(key)
    return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None
