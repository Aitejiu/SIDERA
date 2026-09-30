"""Public-path planning confirmation harness for the basic acceptance run."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

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


_WAITING_PLANNING_STATUSES = {
    "proposal_ready",
    "awaiting_confirmation",
    "needs_confirmation",
}
_CLARIFICATION_PLANNING_STATUSES = {"waiting_for_user", "needs_clarification"}


def create_confirmed_plan(
    client: PlanningClients,
    brief: PlanningBrief,
    manifest: Mapping[str, object],
) -> ConfirmedPlanEvidence:
    """Submit a brief through Main Agent, inspect a Proposal, then confirm it."""

    run_id = _uuid(manifest.get("run_id"), "run_id")
    workspace_id = _uuid(manifest.get("workspace_id"), "workspace_id")
    deadline = _deadline(manifest)
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
            {
                "input": _brief_prompt(brief),
                "operationId": str(uuid4()),
            },
        ),
        "main_agent_turn_start_failed",
    )
    turn_id = _text(turn.get("id"), "main_agent_turn_id")
    turn_outcome = client.main_agent.wait_for(
        f"/main-agent/v1/threads/{thread_id}/turns/{turn_id}",
        lambda body: body.get("status") in {"completed", "failed", "cancelled"},
        deadline,
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
        client.control_plane.wait_for(
            detail_path,
            lambda body: body.get("status")
            in _WAITING_PLANNING_STATUSES | _CLARIFICATION_PLANNING_STATUSES,
            deadline,
        ),
        "planning_request_read_failed",
    )
    pre_confirmation_status = _text(detail.get("status"), "planning_status")
    if pre_confirmation_status in _CLARIFICATION_PLANNING_STATUSES:
        raise PlanningEvidenceError("planning_clarification_required")
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
    _accepted_body(confirmation, "planning_confirmation_failed")
    confirmation_command_id = confirmation.command_id or command_id
    command_path = f"/api/v1/workspaces/{workspace_id}/commands/{confirmation_command_id}"
    confirmation_record = _accepted_body(
        client.control_plane.wait_for(
            command_path,
            lambda body: body.get("status") in {"applied", "rejected"},
            deadline,
        ),
        "planning_confirmation_record_unavailable",
    )
    if confirmation_record.get("status") != "applied":
        raise PlanningEvidenceError("planning_confirmation_not_applied")
    root_task_id = _find_uuid(confirmation_record, "root_task_id")
    if root_task_id is None:
        raise PlanningEvidenceError("confirmation_root_task_missing")
    post_confirmation = _accepted_body(
        client.control_plane.wait_for(
            detail_path,
            lambda body: body.get("status") not in _WAITING_PLANNING_STATUSES,
            deadline,
        ),
        "planning_status_after_confirmation_unavailable",
    )
    post_confirmation_status = _text(
        post_confirmation.get("status"),
        "post_confirmation_status",
    )
    if post_confirmation_status in {
        "proposal_ready",
        "awaiting_confirmation",
        "needs_confirmation",
    } | _CLARIFICATION_PLANNING_STATUSES:
        raise PlanningEvidenceError("planning_confirmation_not_observed")
    return ConfirmedPlanEvidence(
        run_id=run_id,
        workspace_id=workspace_id,
        main_agent_thread_id=thread_id,
        main_agent_turn_id=turn_id,
        planning_request_id=planning_request_id,
        proposal_id=proposal_id,
        proposal_revision=proposal_revision,
        proposal_content_hash=proposal_hash,
        confirmation_command_id=confirmation_command_id,
        root_task_id=root_task_id,
        confirmed_by="test-operator",
        pre_confirmation_status=pre_confirmation_status,
        post_confirmation_status=post_confirmation_status,
        check_codes=(
            "main_agent_turn_observed",
            "planning_request_observed_before_confirmation",
            "proposal_hash_observed",
            "parallel_implementation_workers_observed",
            "explicit_confirmation_command_applied",
        ),
    )


def observe_confirmed_plan(
    client: PlanningClients,
    *,
    thread_id: str,
    turn_id: str,
    manifest: Mapping[str, object],
    confirmation_command_id: UUID | None = None,
) -> ConfirmedPlanEvidence:
    """Read a previously confirmed plan without creating or confirming anything."""

    run_id = _uuid(manifest.get("run_id"), "run_id")
    workspace_id = _uuid(manifest.get("workspace_id"), "workspace_id")
    deadline = _deadline(manifest)
    if not thread_id.strip() or not turn_id.strip():
        raise PlanningEvidenceError("main_agent_identity_missing")

    turn = _accepted_body(
        client.main_agent.get_json(
            f"/main-agent/v1/threads/{thread_id}/turns/{turn_id}"
        ),
        "main_agent_turn_unavailable",
    )
    if turn.get("status") != "completed":
        raise PlanningEvidenceError("main_agent_turn_not_completed")
    planning_request_id = _extract_planning_request_id(turn)
    if planning_request_id is None:
        raise PlanningEvidenceError("main_agent_submit_brief_missing")

    detail_path = (
        f"/api/v1/workspaces/{workspace_id}/planning-requests/{planning_request_id}"
    )
    detail = _accepted_body(
        client.control_plane.wait_for(
            detail_path,
            lambda body: body.get("status") not in _WAITING_PLANNING_STATUSES,
            deadline,
        ),
        "planning_request_read_failed",
    )
    post_status = _text(detail.get("status"), "post_confirmation_status")
    if post_status in _WAITING_PLANNING_STATUSES | _CLARIFICATION_PLANNING_STATUSES:
        raise PlanningEvidenceError("planning_confirmation_not_observed")
    proposal_revision = _positive_int(detail.get("current_proposal_revision"))
    proposal_hash = _hash(detail.get("current_proposal_content_hash"))
    if proposal_revision is None or proposal_hash is None:
        raise PlanningEvidenceError("proposal_identity_missing")
    proposal = _accepted_body(
        client.control_plane.get_json(
            f"{detail_path}/proposals/{proposal_revision}"
        ),
        "planning_proposal_read_failed",
    )
    proposal_id = _uuid(proposal.get("id"), "proposal_id")
    if _hash(proposal.get("content_hash")) != proposal_hash:
        raise PlanningEvidenceError("proposal_hash_mismatch")

    command_id_value: object = confirmation_command_id
    if command_id_value is None:
        command_id_value = _find_uuid(turn, "confirmation_command_id")
    if command_id_value is None:
        command_id_value = _find_uuid(detail, "confirmation_command_id")
    if command_id_value is None:
        # A browser run may persist an identity locator in the manifest; its
        # nested evidence snapshot is never used as a source of truth.
        command_id_value = manifest.get("confirmation_command_id")
    confirmation_command_id = _uuid(
        command_id_value, "confirmation_command_id"
    )
    command = _accepted_body(
        client.control_plane.wait_for(
            f"/api/v1/workspaces/{workspace_id}/commands/{confirmation_command_id}",
            lambda body: body.get("status") in {"applied", "rejected"},
            deadline,
        ),
        "confirmation_command_read_failed",
    )
    if _uuid(command.get("command_id"), "confirmation_command_id") != confirmation_command_id:
        raise PlanningEvidenceError("confirmation_command_id_mismatch")
    if command.get("command_type") != "ConfirmPlanningProposal":
        raise PlanningEvidenceError("confirmation_command_type_mismatch")
    if command.get("target_type") != "planning_request":
        raise PlanningEvidenceError("confirmation_command_target_mismatch")
    if _uuid(command.get("target_id"), "confirmation_target_id") != planning_request_id:
        raise PlanningEvidenceError("confirmation_target_id_mismatch")
    if command.get("status") != "applied":
        raise PlanningEvidenceError("confirmation_command_not_applied")
    expected_version = _positive_int(command.get("expected_version"))
    request_version = _positive_int(detail.get("version"))
    profile_id = _uuid(detail.get("planning_profile_id"), "planning_profile_id")
    profile_version = _positive_int(detail.get("planning_profile_version"))
    if request_version is None or profile_version is None:
        raise PlanningEvidenceError("planning_version_missing")
    if expected_version is None:
        raise PlanningEvidenceError("confirmation_expected_version_missing")
    if expected_version != request_version:
        raise PlanningEvidenceError("confirmation_expected_version_mismatch")
    payload = command.get("payload")
    if not isinstance(payload, Mapping):
        raise PlanningEvidenceError("confirmation_payload_missing")
    payload_hash = _hash(command.get("payload_sha256"))
    if payload_hash is None:
        raise PlanningEvidenceError("confirmation_payload_hash_missing")
    if payload_hash != _canonical_hash(payload):
        raise PlanningEvidenceError("confirmation_payload_hash_mismatch")
    if _positive_int(payload.get("proposal_revision")) != proposal_revision:
        raise PlanningEvidenceError("confirmation_proposal_revision_mismatch")
    if _hash(payload.get("proposal_content_hash")) != proposal_hash:
        raise PlanningEvidenceError("confirmation_proposal_hash_mismatch")
    if _uuid(payload.get("profile_id"), "confirmation_profile_id") != profile_id:
        raise PlanningEvidenceError("confirmation_profile_id_mismatch")
    if _positive_int(payload.get("profile_version")) != profile_version:
        raise PlanningEvidenceError("confirmation_profile_version_mismatch")

    result = command.get("result")
    root_task_id = _find_uuid(result, "root_task_id")
    confirmed_by = _find_text(result, "confirmed_by")
    pre_status = _find_text(result, "pre_confirmation_status")
    if root_task_id is None:
        raise PlanningEvidenceError("confirmation_root_task_missing")
    if confirmed_by is None:
        raise PlanningEvidenceError("confirmed_by_missing")
    if pre_status is None or pre_status not in {
        "proposal_ready",
        "awaiting_confirmation",
        "needs_confirmation",
    }:
        raise PlanningEvidenceError("pre_confirmation_status_missing")
    return ConfirmedPlanEvidence(
        run_id=run_id,
        workspace_id=workspace_id,
        main_agent_thread_id=thread_id,
        main_agent_turn_id=turn_id,
        planning_request_id=planning_request_id,
        proposal_id=proposal_id,
        proposal_revision=proposal_revision,
        proposal_content_hash=proposal_hash,
        confirmation_command_id=confirmation_command_id,
        root_task_id=root_task_id,
        confirmed_by=confirmed_by,
        pre_confirmation_status=pre_status,
        post_confirmation_status=post_status,
        check_codes=(
            "main_agent_turn_observed_read_only",
            "planning_request_observed_after_confirmation",
            "proposal_hash_observed_read_only",
            "confirmation_record_read_only",
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


def _deadline(manifest: Mapping[str, object]) -> float:
    scenarios = manifest.get("scenarios")
    if isinstance(scenarios, Mapping):
        for candidate in ("profile", "user-flow"):
            limits = scenarios.get(candidate)
            if isinstance(limits, Mapping):
                seconds = limits.get("max_seconds")
                if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0:
                    return time.monotonic() + min(float(seconds), 3600.0)
    return time.monotonic() + 30.0


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


def _canonical_hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _mapping_text(value: Mapping[str, object], key: str) -> str | None:
    candidate = value.get(key)
    return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None


def _find_text(value: object, key: str) -> str | None:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        for nested in value.values():
            found = _find_text(nested, key)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            found = _find_text(nested, key)
            if found is not None:
                return found
    return None
