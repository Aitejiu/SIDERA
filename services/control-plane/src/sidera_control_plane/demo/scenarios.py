from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast
from uuid import uuid5

import httpx

from .contracts import DemoManifest, Scenario
from .environment import runtime_root_for
from .http_client import DemoClient, DemoClientError, DemoClientNotFound
from .manifest import validate_manifest
from .state import DemoStateStore
from .supervisor import read_run_phase

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_PREPARATION_AUDIT_FILE = "preparation-audit.json"
_PREPARATION_TIMEOUT_SECONDS = 60.0
_PREPARATION_POLL_SECONDS = 0.25
_DEMO_REQUIREMENT = "为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认"
_CONTROLS_REQUIREMENT = "为项目增加 CSV 批量导入功能，手机号策略需要确认"
_PHONE_INPUT = "手机号允许为空，有值时才校验格式"

_PREPARATION_STEPS: Final[dict[Scenario, tuple[str, ...]]] = {
    "mainline": (),
    "revision": ("complete_mainline_checkpoint", "await_revision_request"),
    "handoff": ("record_worker_input_checkpoint", "await_handoff_request"),
    "rework": ("publish_failed_candidate_checkpoint", "await_rework_request"),
    "recovery": ("arm_after_input_persist_fault", "await_intervention"),
    "knowledge": ("publish_knowledge_candidate", "await_candidate_review"),
    "governance": ("create_release_governance_hold", "await_governance_decision"),
    "controls": ("prepare_control_facts", "await_control_action"),
    "history": ("complete_history_run", "open_historical_result"),
}


def preparation_steps(scenario: Scenario) -> tuple[str, ...]:
    """Return the reviewed preparation vocabulary for one independent run.

    The values are labels, not executable Python or shell.  The presenter uses
    them to explain the expected stop point; business actions remain explicit
    API commands driven by a scenario-specific test or operator.
    """
    try:
        return _PREPARATION_STEPS[scenario]
    except KeyError as exc:
        raise ValueError("unknown_demo_scenario") from exc


def prepare_checkpoint(manifest: DemoManifest) -> dict[str, object]:
    """Describe the current, persisted preparation checkpoint without writes."""
    validate_manifest(manifest, _PROJECT_ROOT)
    state = DemoStateStore(runtime_root_for(manifest) / "state", manifest.run_id)
    gates = {
        gate: state.read_gate(gate)
        for gate in ("parallel", "before-artifact", "handoff", "governance")
    }
    audit = _read_preparation_audit_if_present(manifest)
    entry_url = (
        str(audit["entry_url"])
        if audit is not None and isinstance(audit.get("entry_url"), str)
        else manifest.endpoints.web
    )
    return {
        "schema_version": 1,
        "run_id": str(manifest.run_id),
        "workspace_id": str(manifest.workspace_id),
        "scenario": manifest.scenario,
        "phase": read_run_phase(manifest),
        "entry_url": entry_url,
        "pending_action": _pending_action(manifest.scenario),
        "preparation_steps": list(preparation_steps(manifest.scenario)),
        "gates": gates,
        "preparation_status": (
            audit.get("status") if audit is not None else "not_presented"
        ),
        "preparation_stages": (
            audit.get("stages", []) if audit is not None else []
        ),
        "prepared_at": datetime.now(UTC).isoformat(),
    }


def prepare_scenario(manifest: DemoManifest) -> dict[str, object]:
    """Prepare one scenario through the local public HTTP contracts.

    ``prepare`` only creates an empty run.  This function is called by
    ``present`` after the supervisor is running, so all business state below
    is created by the same Gateway and Control Plane APIs that the Web UI
    uses.  The local simulation gates are deliberately the only state file
    writes allowed here.
    """
    validate_manifest(manifest, _PROJECT_ROOT)
    existing = _read_preparation_audit_if_present(manifest)
    if existing is not None and existing.get("status") == "completed":
        return existing
    if manifest.scenario == "mainline":
        return record_preparation_checkpoint(manifest)

    preparation = _ScenarioPreparation(manifest).prepare()
    _write_preparation_audit(manifest, preparation)
    return preparation


def record_preparation_checkpoint(manifest: DemoManifest) -> dict[str, object]:
    """Persist the present stop point without creating business records.

    The mainline checkpoint is intentionally empty.  Extension scenarios use
    :func:`prepare_scenario` and replace this checkpoint with their public API
    preparation audit after the real commands have been observed.
    """
    validate_manifest(manifest, _PROJECT_ROOT)
    path = runtime_root_for(manifest) / "reports" / _PREPARATION_AUDIT_FILE
    try:
        return _read_preparation_audit(path)
    except FileNotFoundError:
        pass

    now = datetime.now(UTC).isoformat()
    record: dict[str, object] = {
        "schema_version": 1,
        "run_id": str(manifest.run_id),
        "scenario": manifest.scenario,
        "actor": "demo-preparation",
        "command_id": str(uuid5(manifest.run_id, "demo-preparation:present")),
        "status": "checkpointed",
        "preparation_steps": list(preparation_steps(manifest.scenario)),
        "business_commands": [],
        "started_at": now,
        "finished_at": now,
        "entry_url": manifest.endpoints.web,
        "stages": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return _read_preparation_audit(path)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump(record, target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.chmod(path, 0o600)
    return record


class _ScenarioPreparation:
    """Drive the reviewed interview stop points through public local APIs."""

    def __init__(self, manifest: DemoManifest) -> None:
        self.manifest = manifest
        self.client = DemoClient(manifest, timeout_seconds=8.0)
        self.http = httpx.Client(
            timeout=8.0,
            trust_env=False,
            follow_redirects=False,
        )
        self.started_at = datetime.now(UTC).isoformat()
        self.stages: list[dict[str, object]] = []
        self.business_commands: list[str] = []
        self.root_task_id: str | None = None
        self.entry_url = manifest.endpoints.web

    def prepare(self) -> dict[str, object]:
        try:
            self._stage(
                "complete_mainline_checkpoint",
                self._prepare_mainline,
            )
            if self.manifest.scenario == "revision":
                self._stage("prepare_revision_stop", self._prepare_revision)
            elif self.manifest.scenario == "handoff":
                self._stage("record_worker_input_checkpoint", self._prepare_handoff)
            elif self.manifest.scenario == "rework":
                self._stage("publish_failed_candidate_checkpoint", self._prepare_rework)
            elif self.manifest.scenario == "recovery":
                self._stage("arm_after_input_persist_fault", self._prepare_recovery)
            elif self.manifest.scenario == "knowledge":
                self._stage("publish_knowledge_candidate", self._prepare_knowledge)
            elif self.manifest.scenario == "governance":
                self._stage("create_release_governance_hold", self._prepare_governance)
            elif self.manifest.scenario == "controls":
                self._stage("prepare_control_facts", self._prepare_controls)
            elif self.manifest.scenario == "history":
                self._stage("complete_history_run", self._prepare_history)
            else:
                raise ValueError("unknown_demo_scenario")

            pending = preparation_steps(self.manifest.scenario)[-1]
            self.stages.append(
                {
                    "stage": pending,
                    "actor": "demo-preparation",
                    "command_id": None,
                    "status": "awaiting_user",
                    "started_at": datetime.now(UTC).isoformat(),
                    "finished_at": None,
                    "result": {"pending_action": _pending_action(self.manifest.scenario)},
                }
            )
            if self.root_task_id is not None:
                self.entry_url = (
                    f"{self.manifest.endpoints.web}/workspaces/"
                    f"{self.manifest.workspace_id}/tasks/{self.root_task_id}"
                )
            return {
                "schema_version": 1,
                "run_id": str(self.manifest.run_id),
                "scenario": self.manifest.scenario,
                "actor": "demo-preparation",
                "command_id": str(uuid5(self.manifest.run_id, "demo-preparation:present")),
                "status": "completed",
                "preparation_steps": list(preparation_steps(self.manifest.scenario)),
                "business_commands": list(self.business_commands),
                "stages": list(self.stages),
                "root_task_id": self.root_task_id,
                "entry_url": self.entry_url,
                "started_at": self.started_at,
                "finished_at": datetime.now(UTC).isoformat(),
            }
        finally:
            self.client.close()
            self.http.close()

    def _stage(self, name: str, action: Any) -> None:
        started_at = datetime.now(UTC).isoformat()
        try:
            result = action()
        except Exception as exc:
            self.stages.append(
                {
                    "stage": name,
                    "actor": "demo-preparation",
                    "command_id": None,
                    "status": "failed",
                    "started_at": started_at,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "result": {"error_code": type(exc).__name__},
                }
            )
            raise
        self.stages.append(
            {
                "stage": name,
                "actor": "demo-preparation",
                "command_id": result.get("command_id") if isinstance(result, dict) else None,
                "status": "applied",
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "result": _audit_value(result),
            }
        )

    def _prepare_mainline(self) -> dict[str, object]:
        requirement = (
            _CONTROLS_REQUIREMENT
            if self.manifest.scenario == "controls"
            else _DEMO_REQUIREMENT
        )
        thread = self._gateway_json(
            "POST",
            "/main-agent/v1/threads",
            {"title": "Interview demo", "context_refs": []},
        )
        thread_id = _required_string(thread, "id")
        turn = self._gateway_json(
            "POST",
            f"/main-agent/v1/threads/{thread_id}/turns",
            {"input": requirement},
        )
        turn_id = _required_string(turn, "id")
        completed = self._wait_for_gateway_turn(thread_id, turn_id)
        planning_request_id = _planning_request_from_turn(completed)
        if planning_request_id is None:
            raise RuntimeError("demo_preparation_planning_request_missing")
        self._wait_for_planning_proposal(planning_request_id)
        confirmation = self._confirm_planning_proposal(planning_request_id)
        self.root_task_id = _string_from_mapping(confirmation.get("data"), "root_task_id")
        if self.root_task_id is None:
            self.root_task_id = self._wait_for_materialization(planning_request_id)
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "planning_request_id": planning_request_id,
            "root_task_id": self.root_task_id,
            "command_id": confirmation.get("command_id"),
        }

    def _prepare_revision(self) -> dict[str, object]:
        self._release_gate("parallel")
        self._wait_for_worker_session("validate")
        self._release_gate("before-artifact")
        self._approve_release("review-report")
        return {"root_task_id": self.root_task_id, "gate": "before-artifact"}

    def _prepare_handoff(self) -> dict[str, object]:
        self._release_gate("parallel")
        workspace = self._wait_for_worker_session("validate")
        return {
            "root_task_id": self.root_task_id,
            "worker_item_id": _worker_item_id_from_workspace(workspace),
            "current_step": workspace.get("current_step"),
        }

    def _prepare_rework(self) -> dict[str, object]:
        self._release_gate("parallel")
        self._wait_for_worker_session("validate")
        self._send_validation_input()
        self._release_gate("before-artifact")
        self._approve_release("review-report")
        self._approve_release("integrate")
        result = self._wait_for_result(lambda value: _result_verification_status(value) == "failed")
        return {"root_task_id": self.root_task_id, "result_stage": result.get("result_stage")}

    def _prepare_recovery(self) -> dict[str, object]:
        self._release_gate("parallel")
        workspace = self._wait_for_worker_session("validate")
        return {
            "root_task_id": self.root_task_id,
            "worker_item_id": _worker_item_id_from_workspace(workspace),
            "fault_point": "after-input-persist",
        }

    def _prepare_knowledge(self) -> dict[str, object]:
        self._release_gate("parallel")
        self._wait_for_worker_session("validate")
        self._send_validation_input()
        self._release_gate("before-artifact")
        candidate = self._wait_for_knowledge_candidate()
        return {"root_task_id": self.root_task_id, "candidate_id": candidate}

    def _prepare_governance(self) -> dict[str, object]:
        self._wait_for_worker_session("report")
        self._pause_release("report")
        self._release_gate("parallel")
        proposal = self._wait_for_governance_proposal("report")
        return {"root_task_id": self.root_task_id, "proposal_id": proposal}

    def _prepare_controls(self) -> dict[str, object]:
        self._release_gate("parallel")
        workspace = self._wait_for_worker_session("validate")
        return {
            "root_task_id": self.root_task_id,
            "worker_item_id": _worker_item_id_from_workspace(workspace),
            "control_facts_ready": True,
        }

    def _prepare_history(self) -> dict[str, object]:
        self._release_gate("parallel")
        self._wait_for_worker_session("validate")
        self._send_validation_input()
        self._release_gate("before-artifact")
        self._approve_release("review-report")
        self._approve_release("integrate")
        self._wait_for_result(lambda value: _result_verification_status(value) == "passed")
        self._complete_task()
        completed = self._wait_for_result(lambda value: value.get("result_stage") == "completed")
        return {"root_task_id": self.root_task_id, "result_stage": completed.get("result_stage")}

    def _release_gate(self, gate: str) -> None:
        from .state import DemoStateStore

        DemoStateStore(runtime_root_for(self.manifest) / "state", self.manifest.run_id).set_gate(
            cast(Any, gate), True
        )
        self._wait_for_worker_observation()

    def _wait_for_worker_observation(self) -> None:
        time.sleep(_PREPARATION_POLL_SECONDS)

    def _gateway_json(self, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        response = self.http.request(
            method,
            f"{self.manifest.endpoints.main_agent}{path}",
            json=body,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"demo_gateway_request_failed:{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("demo_gateway_response_invalid")
        return cast(dict[str, object], value)

    def _wait_for_gateway_turn(self, thread_id: str, turn_id: str) -> dict[str, object]:
        return self._wait_for(
            lambda: self._gateway_json(
                "GET", f"/main-agent/v1/threads/{thread_id}/turns/{turn_id}"
            ),
            lambda value: value.get("status") in {"completed", "failed", "interrupted"},
            "main_agent_turn",
        )

    def _wait_for_planning_proposal(self, request_id: str) -> dict[str, object]:
        def read() -> dict[str, object]:
            value = self.client.get(
                f"/api/v1/workspaces/{self.manifest.workspace_id}/planning-requests/{request_id}"
            )
            if not isinstance(value, dict):
                raise TypeError("planning_request_response_invalid")
            if value.get("status") == "waiting_for_user":
                self._answer_clarifications(request_id, value)
            return value

        return self._wait_for(
            read,
            lambda value: value.get("status") == "proposal_ready",
            "planning_proposal",
        )

    def _answer_clarifications(self, request_id: str, request: Mapping[str, object]) -> None:
        raw = request.get("pending_clarifications")
        if not isinstance(raw, list):
            clarifications = self.client.get(
                f"/api/v1/workspaces/{self.manifest.workspace_id}/planning-requests/{request_id}/clarifications"
            )
            raw = clarifications.get("items") if isinstance(clarifications, dict) else None
        answers = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, Mapping) and item.get("answer") is None:
                clarification_id = item.get("id")
                if clarification_id is not None:
                    answers.append({"clarification_id": str(clarification_id), "answer": "optional"})
        if not answers:
            return
        version = request.get("version")
        if not isinstance(version, int):
            raise TypeError("planning_request_version_missing")
        self._command(
            "AnswerPlanningClarification",
            "planning_request",
            request_id,
            version,
            {"answers": answers},
            "answer-planning-clarification",
        )

    def _confirm_planning_proposal(self, request_id: str) -> dict[str, object]:
        request = self._get_object(
            f"/api/v1/workspaces/{self.manifest.workspace_id}/planning-requests/{request_id}"
        )
        revision = request.get("current_proposal_revision")
        version = request.get("version")
        profile_id = request.get("planning_profile_id")
        profile_version = request.get("planning_profile_version")
        if not isinstance(revision, (int, str)):
            raise TypeError("planning_confirmation_binding_missing")
        if not isinstance(version, (int, str)):
            raise TypeError("planning_confirmation_binding_missing")
        if not isinstance(profile_id, (int, str)):
            raise TypeError("planning_confirmation_binding_missing")
        if not isinstance(profile_version, (int, str)):
            raise TypeError("planning_confirmation_binding_missing")
        proposal = self._get_object(
            f"/api/v1/workspaces/{self.manifest.workspace_id}/planning-requests/{request_id}/proposals/{revision}"
        )
        content_hash = proposal.get("content_hash")
        if not isinstance(content_hash, str):
            raise TypeError("planning_proposal_hash_missing")
        return self._command(
            "ConfirmPlanningProposal",
            "planning_request",
            request_id,
            int(version),
            {
                "proposal_revision": int(revision),
                "proposal_content_hash": content_hash,
                "profile_id": str(profile_id),
                "profile_version": int(profile_version),
            },
            "confirm-planning-proposal",
        )

    def _wait_for_materialization(self, request_id: str) -> str:
        value = self._wait_for(
            lambda: self._get_object(
                f"/api/v1/workspaces/{self.manifest.workspace_id}/planning-requests/{request_id}/materialization"
            ),
            lambda item: _string_from_mapping(item, "root_task_id") is not None,
            "planning_materialization",
        )
        root_task_id = _string_from_mapping(value, "root_task_id")
        if root_task_id is None:
            raise RuntimeError("planning_materialization_root_missing")
        return root_task_id

    def _worker_items(self) -> dict[str, str]:
        if self.root_task_id is None:
            raise RuntimeError("demo_root_task_missing")
        graph = self._get_object(
            f"/api/v1/tasks/{self.root_task_id}/graph?workspace_id={self.manifest.workspace_id}"
        )
        items: dict[str, str] = {}
        nodes = graph.get("nodes")
        if not isinstance(nodes, list):
            return items
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            subtask_key = node.get("subtask_key")
            if not isinstance(subtask_key, str):
                continue
            try:
                workspace = self._get_object(
                    f"/api/v1/tasks/{self.root_task_id}/subtasks/{subtask_key}?workspace_id={self.manifest.workspace_id}"
                )
            except (DemoClientError, ValueError):
                continue
            raw_items = workspace.get("worker_items")
            if not isinstance(raw_items, list):
                continue
            for raw_item in raw_items:
                if not isinstance(raw_item, Mapping):
                    continue
                item_id = raw_item.get("worker_item_id")
                if not isinstance(item_id, str):
                    continue
                try:
                    item_workspace = self._get_object(
                        f"/api/v1/workspaces/{self.manifest.workspace_id}/worker-items/{item_id}/workspace"
                    )
                except (DemoClientError, ValueError):
                    continue
                placement = item_workspace.get("current_placement")
                key = _string_from_mapping(placement, "worker_item_key")
                if key is None:
                    key = _string_from_mapping(raw_item, "stable_key")
                if key is not None:
                    items[key] = item_id
        return items

    def _wait_for_worker_session(self, worker_key: str) -> dict[str, object]:
        def read() -> dict[str, object]:
            item_id = self._worker_items().get(worker_key)
            if item_id is None:
                raise DemoClientNotFound("worker_item_not_found")
            value = self._get_object(
                f"/api/v1/workspaces/{self.manifest.workspace_id}/worker-items/{item_id}/workspace"
            )
            runtime = value.get("current_runtime")
            if isinstance(runtime, Mapping) and runtime.get("session_id") is not None:
                return value
            return value

        return self._wait_for(
            read,
            lambda value: isinstance(value.get("current_runtime"), Mapping)
            and value["current_runtime"].get("session_id") is not None,
            f"worker_session:{worker_key}",
        )

    def _validation_context(self) -> tuple[str, dict[str, object]]:
        workspace = self._wait_for_worker_session("validate")
        context = workspace.get("intervention_context")
        worker_item = workspace.get("worker_item")
        if not isinstance(context, Mapping) or not isinstance(worker_item, Mapping):
            raise TypeError("validation_intervention_context_missing")
        worker_item_id = _worker_item_id_from_workspace(workspace)
        session_id = _string_from_mapping(context, "session_id")
        if worker_item_id is None or session_id is None:
            raise RuntimeError("validation_intervention_identity_missing")
        return worker_item_id, dict(context)

    def _send_validation_input(self) -> dict[str, object]:
        _, context = self._validation_context()
        session_id = _required_string(context, "session_id")
        session_version = context.get("session_version")
        if not isinstance(session_version, int):
            raise TypeError("validation_session_version_missing")
        return self._command(
            "SendExecutionInput",
            "execution_session",
            session_id,
            session_version,
            {
                "context": context,
                "client_idempotency_key": f"demo-preparation:{self.manifest.run_id}:phone-input",
                "text": _PHONE_INPUT,
                "attachment_refs": [],
            },
            "send-validation-input",
        )

    def _approve_release(self, worker_key: str) -> dict[str, object]:
        def find_action() -> tuple[str, dict[str, object]] | None:
            item_id = self._worker_items().get(worker_key)
            if item_id is None:
                return None
            workspace = self._get_object(
                f"/api/v1/workspaces/{self.manifest.workspace_id}/worker-items/{item_id}/workspace"
            )
            collaboration = workspace.get("collaboration")
            if not isinstance(collaboration, Mapping):
                return None
            proposal = collaboration.get("active_proposal")
            actions = collaboration.get("available_actions")
            runtime = workspace.get("current_runtime")
            if proposal is None and (
                collaboration.get("state") in {"released", "materialized"}
                or (
                    isinstance(runtime, Mapping)
                    and _string_from_mapping(runtime, "execution_id") is not None
                )
            ):
                return item_id, {"already_released": True}
            if not isinstance(proposal, Mapping) or not isinstance(actions, list):
                return None
            if "approve" not in actions:
                return None
            proposal_id = _string_from_mapping(proposal, "proposal_id")
            version = proposal.get("resource_version")
            if proposal_id is None or not isinstance(version, int):
                return None
            return item_id, {
                "target_id": proposal_id,
                "expected_version": version,
                "expected_input_selection_id": proposal.get("input_selection_id"),
                "expected_input_selection_hash": proposal.get("input_selection_hash"),
            }

        item_id, action = self._wait_for(
            find_action,
            lambda value: value is not None,
            f"governance_proposal:{worker_key}",
        )
        del item_id
        if action.get("already_released") is True:
            return {"status": "already_released"}
        return self._command(
            "ApproveCollaborationReleaseProposal",
            "collaboration_release_proposal",
            _required_string(action, "target_id"),
            int(action["expected_version"]),
            {
                "reason": "demo preparation",
                "transport": "web",
                "expected_input_selection_id": action.get("expected_input_selection_id"),
                "expected_input_selection_hash": action.get("expected_input_selection_hash"),
            },
            f"approve-release:{worker_key}",
        )

    def _pause_release(self, worker_key: str) -> dict[str, object]:
        def find_action() -> dict[str, object] | None:
            item_id = self._worker_items().get(worker_key)
            if item_id is None:
                return None
            workspace = self._get_object(
                f"/api/v1/workspaces/{self.manifest.workspace_id}/worker-items/{item_id}/workspace"
            )
            collaboration = workspace.get("collaboration")
            worker_item = workspace.get("worker_item")
            if not isinstance(collaboration, Mapping) or not isinstance(worker_item, Mapping):
                return None
            actions = collaboration.get("available_actions")
            version = worker_item.get("version")
            if not isinstance(actions, list) or "pause" not in actions or not isinstance(version, int):
                return None
            target_generation = _nested_int(workspace, "plan_sync", "target_generation")
            if target_generation is None:
                target_generation = 1
            return {
                "target_id": item_id,
                "expected_version": version,
                "target_plan_generation": target_generation,
            }

        action = self._wait_for(find_action, lambda value: value is not None, f"governance_pause:{worker_key}")
        return self._command(
            "PauseCollaborationRelease",
            "worker_item",
            _required_string(action, "target_id"),
            int(action["expected_version"]),
            {
                "reason": "demo preparation hold",
                "target_plan_generation": int(action["target_plan_generation"]),
                "transport": "web",
            },
            f"pause-release:{worker_key}",
        )

    def _wait_for_governance_proposal(self, worker_key: str) -> str:
        def read() -> str | None:
            item_id = self._worker_items().get(worker_key)
            if item_id is None:
                return None
            workspace = self._get_object(
                f"/api/v1/workspaces/{self.manifest.workspace_id}/worker-items/{item_id}/workspace"
            )
            collaboration = workspace.get("collaboration")
            proposal = collaboration.get("active_proposal") if isinstance(collaboration, Mapping) else None
            return _string_from_mapping(proposal, "proposal_id") if isinstance(proposal, Mapping) else None

        return self._wait_for(read, lambda value: isinstance(value, str), f"governance_proposal:{worker_key}")

    def _wait_for_knowledge_candidate(self) -> str:
        def read() -> str | None:
            value = self._knowledge_json(
                "GET",
                f"/api/v1/workspaces/{self.manifest.workspace_id}/knowledge/candidates?limit=100",
            )
            items = value.get("items") if isinstance(value, Mapping) else None
            if not isinstance(items, list):
                return None
            for item in items:
                if isinstance(item, Mapping) and item.get("proposed_title") == "样例项目：手机号允许为空":
                    candidate_id = item.get("candidate_id")
                    if isinstance(candidate_id, str):
                        return candidate_id
            return None

        return self._wait_for(read, lambda value: isinstance(value, str), "knowledge_candidate")

    def _wait_for_result(self, predicate: Any) -> dict[str, object]:
        if self.root_task_id is None:
            raise RuntimeError("demo_root_task_missing")
        return self._wait_for(
            lambda: self._get_object(
                f"/api/v1/workspaces/{self.manifest.workspace_id}/tasks/{self.root_task_id}/results"
            ),
            predicate,
            "task_result",
        )

    def _complete_task(self) -> dict[str, object]:
        root_task_id = self.root_task_id
        if root_task_id is None:
            raise RuntimeError("demo_root_task_missing")
        result = self._get_object(
            f"/api/v1/workspaces/{self.manifest.workspace_id}/tasks/{root_task_id}/results"
        )
        actions = result.get("available_actions")
        if not isinstance(actions, list):
            raise TypeError("complete_task_action_missing")
        action = next(
            (item for item in actions if isinstance(item, Mapping) and item.get("command_type") == "CompleteTask"),
            None,
        )
        candidate = result.get("current_candidate")
        if not isinstance(action, Mapping) or not isinstance(candidate, Mapping):
            raise TypeError("complete_task_binding_missing")
        candidate_id = _required_string(candidate, "candidate_id")
        candidate_hash = _required_string(candidate, "candidate_hash")
        raw_criteria = result.get("criteria")
        criteria = [
            {
                "criterion_key": str(item.get("criterion_key")),
                "decision": "accept_passed",
                "reason": None,
            }
            for item in raw_criteria
            if isinstance(item, Mapping) and item.get("criterion_key") is not None
        ] if isinstance(raw_criteria, list) else []
        basis_response = self._post_control(
            f"/api/v1/workspaces/{self.manifest.workspace_id}/tasks/{root_task_id}/results/{candidate_id}/acceptance-basis",
            {"criteria": criteria},
        )
        return self._command(
            "CompleteTask",
            "task",
            root_task_id,
            int(action.get("expected_version", 1)),
            {
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hash,
                "acceptance_basis_hash": _required_string(basis_response, "acceptance_basis_hash"),
                "expected_result_version": int(action.get("expected_result_version", 1)),
                "criteria": criteria,
            },
            "complete-task",
        )

    def _command(
        self,
        command_type: str,
        target_type: str,
        target_id: str,
        expected_version: int | None,
        payload: dict[str, object],
        key: str,
    ) -> dict[str, object]:
        command_id = uuid5(self.manifest.run_id, f"demo-preparation:command:{key}")
        result = self.client.command(
            {
                "command_id": str(command_id),
                "command_type": command_type,
                "target_type": target_type,
                "target_id": target_id,
                "expected_version": expected_version,
                "workspace_id": str(self.manifest.workspace_id),
                "actor": {"type": "local", "id": "aitejiu"},
                "idempotency_key": f"demo-preparation:{command_type}:{key}",
                "correlation_id": str(command_id),
                "payload": payload,
            }
        )
        self.business_commands.append(str(command_id))
        return result

    def _get_object(self, path: str) -> dict[str, object]:
        value = self.client.get(path)
        if not isinstance(value, dict):
            raise TypeError("control_plane_response_invalid")
        return cast(dict[str, object], value)

    def _post_control(self, path: str, body: dict[str, object]) -> dict[str, object]:
        response = self.http.post(f"{self.manifest.endpoints.control_plane}{path}", json=body)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"control_plane_request_failed:{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("control_plane_response_invalid")
        return cast(dict[str, object], value)

    def _knowledge_json(self, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        response = self.http.request(
            method,
            f"{self.manifest.endpoints.knowledge}{path}",
            json=body,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"knowledge_request_failed:{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("knowledge_response_invalid")
        return cast(dict[str, object], value)

    def _wait_for(self, read: Any, predicate: Any, label: str) -> Any:
        deadline = time.monotonic() + _PREPARATION_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                value = read()
                if predicate(value):
                    return value
            except (DemoClientError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
            time.sleep(_PREPARATION_POLL_SECONDS)
        if last_error is not None:
            raise RuntimeError(f"demo_preparation_timeout:{label}:{type(last_error).__name__}") from last_error
        raise RuntimeError(f"demo_preparation_timeout:{label}")


def _read_preparation_audit(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("preparation_audit_invalid")
    if raw.get("actor") != "demo-preparation" or raw.get("status") not in {
        "checkpointed",
        "completed",
    }:
        raise ValueError("preparation_audit_invalid")
    if not isinstance(raw.get("business_commands"), list):
        raise TypeError("preparation_audit_invalid")
    if "stages" in raw and not isinstance(raw["stages"], list):
        raise TypeError("preparation_audit_invalid")
    return dict(raw)


def _read_preparation_audit_if_present(
    manifest: DemoManifest,
) -> dict[str, object] | None:
    path = runtime_root_for(manifest) / "reports" / _PREPARATION_AUDIT_FILE
    try:
        return _read_preparation_audit(path)
    except FileNotFoundError:
        return None


def _write_preparation_audit(
    manifest: DemoManifest,
    record: Mapping[str, object],
) -> dict[str, object]:
    path = runtime_root_for(manifest) / "reports" / _PREPARATION_AUDIT_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    persisted = dict(record)
    persisted.setdefault("entry_url", manifest.endpoints.web)
    persisted.setdefault("stages", [])
    persisted.setdefault("business_commands", [])
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(
                persisted,
                target,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    os.chmod(path, 0o600)
    return persisted


def _audit_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _audit_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_value(child) for child in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _required_string(value: Mapping[str, object], key: str) -> str:
    result = _string_from_mapping(value, key)
    if result is None:
        raise RuntimeError(f"required_string_missing:{key}")
    return result


def _string_from_mapping(value: object, key: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    result = value.get(key)
    return result if isinstance(result, str) and result else None


def _nested_int(value: object, outer_key: str, inner_key: str) -> int | None:
    if not isinstance(value, Mapping):
        return None
    nested = value.get(outer_key)
    if not isinstance(nested, Mapping):
        return None
    result = nested.get(inner_key)
    return result if isinstance(result, int) else None


def _worker_item_id_from_workspace(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    worker_item = value.get("worker_item")
    worker_item_id = _string_from_mapping(worker_item, "worker_item_id")
    return worker_item_id or _string_from_mapping(worker_item, "id")


def _planning_request_from_turn(turn: Mapping[str, object]) -> str | None:
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        if not isinstance(item, Mapping) or item.get("type") != "toolCall":
            continue
        request_id = _string_from_mapping(item.get("data"), "planning_request_id")
        if request_id is not None:
            return request_id
    return None


def _result_verification_status(value: Mapping[str, object]) -> str | None:
    criteria = value.get("criteria")
    statuses: list[object] = []
    if isinstance(criteria, list):
        statuses = [
            item.get("verification_status")
            for item in criteria
            if isinstance(item, Mapping)
        ]
    if statuses and all(status == "passed" for status in statuses):
        return "passed"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "interrupted" for status in statuses):
        return "interrupted"
    active_operation = value.get("active_operation")
    if isinstance(active_operation, Mapping):
        status = active_operation.get("status")
        if isinstance(status, str):
            return status
    candidate = value.get("current_candidate")
    return "pending" if isinstance(candidate, Mapping) else None


def _pending_action(scenario: Scenario) -> str:
    return {
        "mainline": "在 Web Chat 提交需求",
        "revision": "在 Main Agent 中提出计划变更",
        "handoff": "打开校验 WorkerItem 并选择交接",
        "rework": "查看失败候选并请求局部返工",
        "recovery": "介入校验 WorkerItem 并执行受控恢复",
        "knowledge": "打开知识收件箱审核候选",
        "governance": "打开协同放行控制并作出决定",
        "controls": "从能力页或 WorkerItem 执行一个条件动作",
        "history": "打开历史结果页并检查来源",
    }[scenario]
