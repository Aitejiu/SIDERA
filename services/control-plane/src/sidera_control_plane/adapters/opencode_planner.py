from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import SecretStr

from sidera_control_plane.adapters.acp_process import (
    ACPProcessError,
    ACPProcessTransport,
)
from sidera_control_plane.application.planner_runtime import (
    PlannerClarificationResult,
    PlannerExplicitFailure,
    PlannerInput,
    PlannerProposalResult,
    PlannerResult,
    PlannerRunControl,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import (
    PlanningClarification,
    PlanningUsage,
)
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    PlanningProposalPayload,
    ProposalRevision,
)


class OpenCodePlannerError(RuntimeError):
    """The isolated OpenCode planner could not produce a safe result."""


class OpenCodePlannerAdapter:
    """Planner adapter using one isolated OpenCode ACP process per Run."""

    _CAPABILITIES = PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON,
        clarification=True,
        idempotency=IdempotencyCapability.NONE,
        cancellation=CancellationCapability.PROCESS,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.EXPERIMENTAL,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2,),
    )
    provider_ref = "opencode-acp-planner"

    def __init__(
        self,
        *,
        executable: str = "opencode",
        model: str | None = None,
        temp_root: Path = Path(".sidera/planning"),
        request_timeout_seconds: float = 60.0,
        stop_grace_seconds: float = 5.0,
        stderr_limit_bytes: int = 64 * 1024,
        secret_values: tuple[SecretStr, ...] = (),
        client_name: str = "sidera-planner",
        client_version: str = "0.1.0",
    ) -> None:
        executable = executable.strip()
        if not executable:
            raise ValueError("OpenCode planner executable is required")
        if request_timeout_seconds <= 0 or stop_grace_seconds <= 0:
            raise ValueError("OpenCode planner timeouts must be positive")
        if stderr_limit_bytes <= 0:
            raise ValueError("OpenCode planner stderr limit must be positive")
        self._executable = executable
        self._model = model.strip() if model is not None and model.strip() else None
        self._temp_root = temp_root.expanduser()
        self._request_timeout_seconds = request_timeout_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self._stderr_limit_bytes = stderr_limit_bytes
        self._secret_values = tuple(secret_values)
        self._client_name = client_name
        self._client_version = client_version
        self._active: dict[UUID, ACPProcessTransport] = {}
        self._cancelled: set[UUID] = set()
        self._lock = threading.RLock()

    def probe(self, config: Mapping[str, object]) -> PlannerAdapterCapabilityProfile:
        cwd = _config_path(config, "cwd", Path.cwd())
        transport = self._spawn(cwd)
        try:
            initialize = transport.request(
                "initialize",
                self._initialize_params(),
                timeout=self._request_timeout_seconds,
            )
            self._validate_initialize(initialize)
            session = transport.request(
                "session/new",
                {"cwd": str(cwd), "mcpServers": []},
                timeout=self._request_timeout_seconds,
            )
            _session_id(session)
            return self._CAPABILITIES
        except ACPProcessError as exc:
            raise OpenCodePlannerError("OpenCode planner ACP probe failed") from exc
        finally:
            transport.terminate(grace_seconds=self._stop_grace_seconds)
            transport.close()

    def validate_schema_round_trip(self) -> None:
        """Validate the strict response envelope without starting a planner Run."""
        sample = {
            "result_type": "clarification",
            "clarifications": [
                {
                    "question": "sample",
                    "rationale": "sample",
                    "answer_type": "text",
                    "options": [],
                    "blocking": False,
                }
            ],
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_microunits": 0,
                "runtime_seconds": 0,
                "context_bytes": 0,
            },
        }
        encoded = json.dumps(sample, sort_keys=True, separators=(",", ":"))
        value = json.loads(encoded)
        if not isinstance(value, Mapping):
            raise OpenCodePlannerError("planner response schema round trip failed")
        _strict_keys(value, {"result_type", "clarifications", "usage"})
        _parse_usage(value["usage"])

    def plan(
        self,
        planner_input: PlannerInput,
        control: PlannerRunControl,
    ) -> PlannerResult:
        if control.operation_id != planner_input.operation_id:
            return PlannerExplicitFailure(
                code="operation_id_mismatch",
                safe_message="planner operation id does not match input",
                retryable=False,
                unknown_outcome=False,
                provider_ref=self.provider_ref,
            )
        if control.cancel_requested:
            return _cancelled_result()

        run_directory = self._make_run_directory()
        transport: ACPProcessTransport | None = None
        try:
            transport = self._spawn(run_directory)
            self._register(planner_input.operation_id, transport)
            if self._is_cancelled(planner_input.operation_id):
                return _cancelled_result()
            initialize = transport.request(
                "initialize",
                self._initialize_params(),
                timeout=self._remaining_timeout(control),
            )
            self._validate_initialize(initialize)
            session = transport.request(
                "session/new",
                {"cwd": str(run_directory), "mcpServers": []},
                timeout=self._remaining_timeout(control),
            )
            session_id = _session_id(session)
            if self._is_cancelled(planner_input.operation_id):
                return _cancelled_result()
            prompt_result = transport.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": _planner_prompt(planner_input)}],
                },
                timeout=self._remaining_timeout(control),
            )
            if self._is_cancelled(planner_input.operation_id):
                return _cancelled_result()
            transport.read_available(timeout=0)
            response_text = _response_text(
                transport.drain_notifications(),
                session_id=session_id,
                prompt_result=prompt_result,
            )
            return _parse_result(response_text, planner_input, self.provider_ref)
        except OpenCodePlannerError:
            raise
        except ACPProcessError as exc:
            raise OpenCodePlannerError("OpenCode planner ACP execution failed") from exc
        finally:
            with self._lock:
                self._cancelled.discard(planner_input.operation_id)
            self._unregister(planner_input.operation_id, transport)
            if transport is not None:
                transport.terminate(grace_seconds=self._stop_grace_seconds)
                transport.close()
            shutil.rmtree(run_directory, ignore_errors=True)

    def cancel(self, operation_id: UUID) -> None:
        with self._lock:
            self._cancelled.add(operation_id)
            transport = self._active.get(operation_id)
        if transport is not None:
            transport.terminate(grace_seconds=self._stop_grace_seconds)

    def active_processes(self) -> tuple[Any, ...]:
        with self._lock:
            return tuple(
                transport.process
                for transport in self._active.values()
                if transport.process.poll() is None
            )

    def wait_until_active(self, operation_id: UUID, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if operation_id in self._active:
                    return True
            time.sleep(0.01)
        return False

    def _spawn(self, cwd: Path) -> ACPProcessTransport:
        try:
            return ACPProcessTransport.spawn(
                self._executable,
                cwd=cwd,
                stderr_limit_bytes=self._stderr_limit_bytes,
                env={
                    **{
                        f"SIDERA_PLANNER_SECRET_{index}": secret.get_secret_value()
                        for index, secret in enumerate(self._secret_values)
                    },
                    **(
                        {
                            "OPENCODE_CONFIG_CONTENT": json.dumps(
                                {"model": self._model},
                                separators=(",", ":"),
                            )
                        }
                        if self._model is not None
                        else {}
                    ),
                },
            )
        except ACPProcessError as exc:
            raise OpenCodePlannerError("OpenCode planner process is unavailable") from exc

    def _make_run_directory(self) -> Path:
        root = self._temp_root.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        directory = Path(tempfile.mkdtemp(prefix="run-", dir=root))
        os.chmod(directory, 0o700)
        return directory

    def _register(self, operation_id: UUID, transport: ACPProcessTransport) -> None:
        with self._lock:
            self._active[operation_id] = transport

    def _unregister(
        self, operation_id: UUID, transport: ACPProcessTransport | None
    ) -> None:
        with self._lock:
            if self._active.get(operation_id) is transport:
                self._active.pop(operation_id, None)

    def _is_cancelled(self, operation_id: UUID) -> bool:
        with self._lock:
            return operation_id in self._cancelled

    def _remaining_timeout(self, control: PlannerRunControl) -> float:
        if control.deadline_at is None:
            return self._request_timeout_seconds
        remaining = (control.deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise OpenCodePlannerError("planner deadline expired")
        return min(self._request_timeout_seconds, remaining)

    def _initialize_params(self) -> dict[str, object]:
        return {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {
                "name": self._client_name,
                "version": self._client_version,
            },
        }

    @staticmethod
    def _validate_initialize(result: Mapping[str, Any]) -> None:
        if result.get("protocolVersion") != 1:
            raise OpenCodePlannerError("OpenCode planner ACP protocol is unsupported")
        if not isinstance(result.get("agentCapabilities", {}), Mapping):
            raise OpenCodePlannerError("OpenCode planner ACP capabilities are invalid")


def _planner_prompt(planner_input: PlannerInput) -> str:
    document = {
        "contract": "sidera.planner.v1",
        "operation_id": str(planner_input.operation_id),
        "planning_request_id": str(planner_input.planning_request_id),
        "planning_run_id": str(planner_input.planning_run_id),
        "brief": {
            "revision": planner_input.brief.revision,
            "objective": planner_input.brief.objective,
            "constraints": list(planner_input.brief.constraints),
            "acceptance_criteria": list(planner_input.brief.acceptance_criteria),
            "context_refs": list(planner_input.brief.context_refs),
            "requested_outputs": list(planner_input.brief.requested_outputs),
            "user_feedback": planner_input.brief.user_feedback,
        },
        "policy": {
            "interface_version": planner_input.interface_version,
            "proposal_schema_version": planner_input.proposal_schema_version,
            "allowed_capabilities": list(
                planner_input.policy_constraints.allowed_capabilities
            ),
            "adapter_preferences": list(
                planner_input.policy_constraints.adapter_preferences
            ),
            "adapter_forbidden": list(
                planner_input.policy_constraints.adapter_forbidden
            ),
            "max_context_bytes": planner_input.policy_constraints.max_context_bytes,
            "max_tokens": planner_input.policy_constraints.max_tokens,
            "max_cost_microunits": planner_input.policy_constraints.max_cost_microunits,
            "max_runtime_seconds": planner_input.policy_constraints.max_runtime_seconds,
            "max_nodes": planner_input.policy_constraints.max_nodes,
            "max_concurrency": planner_input.policy_constraints.max_concurrency,
            "budget_level": planner_input.policy_constraints.budget_level,
        },
        "context_refs": [item.ref for item in planner_input.context_refs],
    }
    return (
        "Return exactly one JSON object. Do not use Markdown or explanatory text. "
        "The result_type must be proposal or clarification. A proposal uses the "
        "PlanningProposalPayload schema version requested by policy; a clarification "
        "uses question, rationale, answer_type, options, and blocking fields. "
        "Always include usage with integer input_tokens, output_tokens, "
        "cost_microunits, runtime_seconds, and context_bytes. For a proposal, copy "
        "the exact field names from this minimal valid shape, replace the example "
        "values, and omit no required field. Empty dependency and milestone arrays "
        "are valid. Do not add fields.\n"
        + json.dumps(_proposal_shape_hint(), sort_keys=True, separators=(",", ":"))
        + "\n"
        + json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _proposal_shape_hint() -> dict[str, object]:
    return {
        "result_type": "proposal",
        "proposal": {
            "schema_version": 2,
            "kind": "create",
            "root_task": {
                "title": "one root task",
                "goal_summary": "the requested outcome",
                "acceptance_refs": ["acceptance:one"],
            },
            "subtasks": [
                {
                    "key": "subtask-one",
                    "kind": "child",
                    "title": "one subtask",
                    "goal_summary": "a concrete implementation step",
                    "acceptance_refs": ["acceptance:one"],
                    "expected_outputs": ["diff"],
                    "milestone_key": None,
                    "source_refs": [],
                }
            ],
            "worker_items": [
                {
                    "key": "worker-one",
                    "subtask_key": "subtask-one",
                    "title": "one WorkerItem",
                    "role": "implementation",
                    "responsibility": "perform the implementation step",
                    "capability_requirements": ["code"],
                    "worker_policy_ref": None,
                    "configuration_ref": None,
                    "concurrency_limit": 1,
                    "source_refs": [],
                    "pinned_worker_id": None,
                }
            ],
            "milestones": [],
            "subtask_dependencies": [],
            "worker_item_dependencies": [],
            "execution_intents": [
                {
                    "worker_item_key": "worker-one",
                    "purpose": "perform the implementation step",
                    "context_refs": [],
                    "expected_artifact_refs": ["artifact_type:diff"],
                    "required_evidence_refs": ["acceptance:one"],
                    "capability_requirements": ["code"],
                    "adapter_preferences": ["opencode"],
                    "budget_class": "standard",
                    "deadline_seconds": None,
                    "not_before_seconds": None,
                    "max_attempts": 1,
                    "activation_dependencies": [],
                }
            ],
            "decision_summary": {
                "understanding": "the requested outcome",
                "assumptions": [],
                "risks": [],
                "estimated_cost_microunits": 0,
                "estimated_tokens": 0,
                "estimated_runtime_seconds": 60,
                "estimated_concurrency": 1,
                "scope_includes": ["the requested outcome"],
                "scope_excludes": [],
                "metadata": {},
            },
            "impact_policy": {
                "activate_immediately": True,
                "budget_level": "standard",
                "failure_policy": "retry_then_block",
                "manual_gate": False,
                "revision_operation": None,
            },
            "base_descriptor": None,
        },
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_microunits": 0,
            "runtime_seconds": 0,
            "context_bytes": 0,
        },
    }


def _parse_result(
    text: str, planner_input: PlannerInput, provider_ref: str
) -> PlannerResult:
    try:
        value = json.loads(text)
        if not isinstance(value, Mapping):
            raise TypeError("response must be an object")
        result_type = value.get("result_type")
        usage = _parse_usage(value.get("usage"))
        if result_type == "proposal":
            _strict_keys(value, {"result_type", "proposal", "usage"})
            raw_payload = value.get("proposal")
            if not isinstance(raw_payload, Mapping):
                raise ValueError("proposal must be an object")
            payload = PlanningProposalPayload.from_mapping(raw_payload)
            proposal = ProposalRevision.create(
                planning_request_id=planner_input.planning_request_id,
                revision=1,
                brief_revision=planner_input.brief_revision,
                planning_run_id=planner_input.planning_run_id,
                kind=payload.kind,
                payload=payload,
                profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                    planner_input.profile_snapshot
                ),
                profile_snapshot=planner_input.profile_snapshot,
                created_at=datetime.now(UTC),
            )
            return PlannerProposalResult(proposal=proposal, usage=usage)
        if result_type == "clarification":
            _strict_keys(value, {"result_type", "clarifications", "usage"})
            raw_items = value.get("clarifications")
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError("clarifications must be a non-empty list")
            clarifications = tuple(
                _clarification(item, planner_input, index)
                for index, item in enumerate(raw_items)
            )
            return PlannerClarificationResult(
                clarifications=clarifications,
                usage=usage,
            )
        raise ValueError("result_type must be proposal or clarification")
    except (DomainError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OpenCodePlannerError("strict JSON planner response is invalid") from exc


def _strict_keys(value: Mapping[str, object], allowed: set[str]) -> None:
    unknown = [str(key) for key in value if key not in allowed]
    if unknown:
        raise ValueError("response contains unknown fields")


def _parse_usage(value: object) -> PlanningUsage:
    if not isinstance(value, Mapping):
        raise TypeError("usage must be an object")
    fields = {
        "input_tokens",
        "output_tokens",
        "cost_microunits",
        "runtime_seconds",
        "context_bytes",
    }
    _strict_keys(value, fields)
    parsed: dict[str, int] = {}
    for field in fields:
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool):
            raise TypeError("usage values must be integers")
        parsed[field] = item
    return PlanningUsage(**parsed)


def _clarification(
    value: object, planner_input: PlannerInput, index: int
) -> PlanningClarification:
    if not isinstance(value, Mapping):
        raise TypeError("clarification must be an object")
    allowed = {"question", "rationale", "answer_type", "options", "blocking"}
    _strict_keys(value, allowed)
    question = value.get("question")
    rationale = value.get("rationale")
    answer_type = value.get("answer_type")
    options = value.get("options")
    blocking = value.get("blocking")
    if not all(isinstance(item, str) for item in (question, rationale, answer_type)):
        raise ValueError("clarification text fields must be strings")
    if not isinstance(options, list) or not all(
        isinstance(item, str) for item in options
    ):
        raise ValueError("clarification options must be strings")
    if not isinstance(blocking, bool):
        raise TypeError("clarification blocking must be boolean")
    question_text = cast(str, question)
    rationale_text = cast(str, rationale)
    answer_type_text = cast(str, answer_type)
    option_values = cast(list[str], options)
    return PlanningClarification.create(
        clarification_id=uuid5(
            NAMESPACE_URL,
            f"sidera.opencode.planner:{planner_input.operation_id}:clarification:{index}",
        ),
        planning_request_id=planner_input.planning_request_id,
        planning_run_id=planner_input.planning_run_id,
        question=question_text,
        rationale=rationale_text,
        answer_type=answer_type_text,
        options=option_values,
        blocking=blocking,
        created_at=datetime.now(UTC),
    )


def _response_text(
    notifications: tuple[Mapping[str, Any], ...],
    *,
    session_id: str,
    prompt_result: Mapping[str, Any],
) -> str:
    chunks: list[str] = []
    for message in notifications:
        if message.get("method") != "session/update":
            continue
        params = message.get("params")
        if not isinstance(params, Mapping) or params.get("sessionId") != session_id:
            continue
        update = params.get("update")
        if not isinstance(update, Mapping):
            continue
        if update.get("sessionUpdate") not in {
            "agent_message_chunk",
            "agent_message",
        }:
            continue
        content = update.get("content")
        if isinstance(content, Mapping):
            content = content.get("text")
        if isinstance(content, str):
            chunks.append(content)
    if not chunks and isinstance(prompt_result.get("output"), str):
        chunks.append(prompt_result["output"])
    if not chunks:
        raise OpenCodePlannerError("strict JSON planner response is missing")
    return "".join(chunks)


def _session_id(result: Mapping[str, Any]) -> str:
    value = result.get("sessionId")
    if not isinstance(value, str) or not value.strip():
        raise OpenCodePlannerError("OpenCode planner session id is missing")
    return value


def _config_path(config: Mapping[str, object], key: str, default: Path) -> Path:
    value = config.get(key, default)
    if isinstance(value, Path):
        return value.expanduser().resolve()
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().resolve()
    return default.expanduser().resolve()


def _cancelled_result() -> PlannerExplicitFailure:
    return PlannerExplicitFailure(
        code="cancelled",
        safe_message="planner operation was cancelled",
        retryable=False,
        unknown_outcome=False,
        provider_ref=OpenCodePlannerAdapter.provider_ref,
    )
