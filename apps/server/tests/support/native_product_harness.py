import asyncio
import hashlib
import json
import os
import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from threading import Event
from uuid import UUID, uuid5

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import SecretStr
from sidera_agents.contracts import (
    CapabilitySupport,
    InputStatus,
    NativeCancelOutcome,
    NativeCloseOutcome,
    NativeEvent,
    NativeInspection,
    NativeOutcome,
    RunState,
)
from sidera_agents.errors import AgentError
from sidera_agents.runs import VerifiedNativeOpenFailure
from sidera_control_plane.application.planner_contract import _proposal_shape_hint
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.context_packages import ResultInputManifestV1
from sidera_control_plane.domain.errors import DomainError

from sidera_product.task_tools.server import visible_tools

from .controlled_native import ControlledHarness


async def sdk_exchange(connection, *, name=None, arguments=None):
    headers = {
        key: value.get_secret_value() for key, value in connection.headers.items()
    }
    async with (
        streamablehttp_client(
            connection.endpoint_or_argv,
            headers=headers,
            timeout=20,
            sse_read_timeout=20,
        ) as (read, write, _),
        ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=20)
        ) as session,
    ):
        await session.initialize()
        if name is None:
            return {tool.name for tool in (await session.list_tools()).tools}
        return await session.call_tool(name, arguments or {})


def sdk_call(connection, name, arguments):
    result = asyncio.run(sdk_exchange(connection, name=name, arguments=arguments))
    if result.isError or not isinstance(result.structuredContent, dict):
        raise ValueError("native_product_tool_refused")
    return result.structuredContent


def native_proposal():
    body = _proposal_shape_hint(6)
    body.pop("usage")
    body.pop("knowledge_candidates")
    proposal = body["proposal"]
    proposal["root_task"].update(
        title="Native product result", goal_summary="Build the checked result"
    )
    worker, intent = (
        deepcopy(proposal["worker_items"][0]),
        deepcopy(proposal["execution_intents"][0]),
    )
    proposal["worker_items"], proposal["execution_intents"] = [], []
    proposal["worker_item_dependencies"] = []
    for key, upstream in (
        ("source", None),
        ("hard-only", "source"),
        ("integration", "hard-only"),
    ):
        item = deepcopy(worker)
        item.update(key=key, title=key, role=key, responsibility=key)
        proposal["worker_items"].append(item)
        entry = deepcopy(intent)
        entry.update(worker_item_key=key, purpose=key, adapter_preferences=[])
        entry["activation_dependencies"] = (
            []
            if upstream is None
            else [
                {
                    "upstream_worker_item_key": upstream,
                    "downstream_worker_item_key": key,
                    "kind": "hard",
                    "required_artifact_refs": ["artifact_type:diff"],
                }
            ]
        )
        proposal["execution_intents"].append(entry)
        proposal["worker_item_dependencies"].extend(
            deepcopy(entry["activation_dependencies"])
        )
    group = proposal["collaboration_policy_request"]["input_groups"][0]
    group.update(
        consumer_worker_item_key="integration",
        candidates=[{"worker_item_key": "hard-only", "priority": 1}],
    )
    group["requirements"]["required_evidence_refs"] = []
    spec = proposal["integration_spec"]
    spec["integration_worker_item_key"] = "integration"
    spec["rework_policy"]["allowed_worker_item_keys"] = [
        "source",
        "hard-only",
        "integration",
    ]
    spec["criteria"][0]["check_keys"] = ["owned-check"]
    spec["checks"][0]["key"] = "owned-check"
    return json.dumps(body, separators=(",", ":"))


def local_git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "user.name=Owned native fixture",
            "-c",
            "user.email=owned-native@example.invalid",
            *arguments,
        ],
        cwd=directory,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(directory),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _sections(assembled):
    result = {}
    for item in assembled.context:
        if item.origin != "control-plane":
            continue
        if (
            item.item_id in result
            or item.text is None
            or item.content_hash
            != "sha256:" + hashlib.sha256(item.text.encode("utf-8")).hexdigest()
        ):
            raise ValueError("native_product_context_pin_refused")
        result[item.item_id] = json.loads(item.text)
    return result


class NativeProductJourneyHarness(ControlledHarness):
    harness_id = "controlled-native-product-flow"

    def __init__(self, *, source_gate_seconds=3600):
        if (
            type(source_gate_seconds) not in {int, float}
            or not isfinite(source_gate_seconds)
            or source_gate_seconds <= 0
        ):
            raise ValueError("native_source_gate_seconds_refused")
        super().__init__()
        self.source_gate_seconds = source_gate_seconds
        self.runtime = None
        self.connections, self.roles, self.requests, self.authorities = {}, {}, {}, {}
        self.events, self.open_facts, self.inputs, self.worker_facts = {}, [], [], {}
        self.source_entered = Event()
        self.source_gates, self.accepted_inputs = {}, {}
        self.original_run_id = None
        self.unknown_exits, self.cancelled, self.close_attempts = set(), set(), set()
        self.goal = None
        self.goal_responses = {}
        self.physical_inputs = []
        self.assembled_inputs = []

    def probe(self, candidate, checked_at):
        result = super().probe(candidate, checked_at)
        return result.model_copy(
            update={
                "capabilities": {
                    **result.capabilities,
                    "mcp": CapabilitySupport(
                        status="supported",
                        reason_code="controlled-native-sdk",
                        constraints={},
                    ),
                }
            }
        )

    def open(self, request):
        # This controlled adapter has not allocated any handle during these checks.
        try:
            if self.runtime is None:
                raise ValueError("native_product_runtime_required")
            connections = [
                tool for tool in request.tools if tool.name == "SIDERA Task Tools"
            ]
            if not connections or any(tool != connections[0] for tool in connections):
                raise ValueError("native_product_tool_connection_refused")
            connection = connections[0]
            authorization = connection.headers.get("Authorization")
            if authorization is None or not authorization.get_secret_value().startswith(
                "Bearer "
            ):
                raise ValueError("native_product_grant_refused")
            verified = self.runtime.grants.verify(
                SecretStr(authorization.get_secret_value()[7:])
            )
            role = verified.record.role
            workspace = self.runtime.settings.workspace_id
            directory = Path(request.workspace.directory)
            root = self.runtime.settings.workspace_root
            if (
                role not in {"main", "planner", "worker"}
                or verified.run.run_id != request.run_id
                or verified.run.state is not RunState.STARTING
                or verified.record.workspace_id != workspace
                or verified.run.workspace_id != workspace
                or verified.record.profile_ref != verified.run.profile_ref
                or verified.record.profile_ref.workspace_id != workspace
                or verified.run.effective_config.profile_ref != verified.run.profile_ref
                or verified.run.effective_config.runtime_ref != request.runtime.ref
                or verified.run.effective_config.actual_runtime_version
                != request.runtime.actual_version
                or request.runtime.ref.workspace_id != workspace
                or not directory.is_absolute()
                or directory.resolve() != directory
                or not directory.is_dir()
                or not directory.is_relative_to(root.resolve())
                or (role == "worker" and not request.workspace.writable)
            ):
                raise ValueError("native_product_open_scope_refused")
            names = asyncio.run(sdk_exchange(connection))
            if names != visible_tools(verified):
                raise ValueError("native_product_tools_refused")
            denied = asyncio.run(sdk_exchange(connection, name=sorted(names)[0]))
            if not denied.isError:
                raise ValueError("native_product_starting_call_was_not_denied")
        except Exception:  # noqa: BLE001 - pre-allocation failures prove no native creation
            raise VerifiedNativeOpenFailure("native_start_failed") from None
        handle = super().open(request)
        with self.lock:
            self.connections[request.run_id], self.roles[request.run_id] = (
                connection,
                role,
            )
            self.requests[request.run_id] = request
            self.authorities[request.run_id] = verified
            if role == "worker" and self.original_run_id is None:
                self.original_run_id = request.run_id
            self.open_facts.append(
                {
                    "run_id": str(request.run_id),
                    "role": role,
                    "profile_ref": verified.record.profile_ref.model_dump(mode="json"),
                    "tools": sorted(names),
                    "starting_call_denied": True,
                }
            )
        return handle

    def _owned_file(self, run_id, name):
        directory = Path(self.requests[run_id].workspace.directory)
        location = directory / name
        if (
            directory.resolve() != directory
            or location.resolve() != location
            or location.parent != directory
        ):
            raise ValueError("native_product_file_scope_refused")
        return location

    def _write(self, run_id, name, body):
        location = self._owned_file(run_id, name)
        if location.exists():
            if location.read_text() != body:
                raise ValueError("native_product_output_collision")
            return
        descriptor = os.open(
            location, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as output:
            output.write(body)

    def _consume_inputs(self, run_id, role, sections, facts):
        if not {"results", "code"}.issubset(sections):
            raise ValueError("native_product_authorized_inputs_missing")
        results = sections["results"]
        if not isinstance(results, dict) or set(results) != {"manifest", "inputs"}:
            raise ValueError("native_product_result_shape_refused")
        try:
            manifest = ResultInputManifestV1.from_mapping(results["manifest"])
            code = CodeInputManifestV1.from_mapping(sections["code"])
        except DomainError as error:
            raise ValueError("native_product_input_manifest_refused") from error
        upstream = "source" if role == "hard-only" else "hard-only"
        original = self.worker_facts.get(upstream)
        if original is None:
            raise ValueError("native_product_original_source_missing")
        hard_ref = "request:" + original["execution_request"]["id"]
        groups = () if role == "hard-only" else ("implementation-inputs",)
        if (
            tuple(item.group_key for item in manifest.groups) != groups
            or tuple(item.dependency_key for item in manifest.hard_inputs)
            != (hard_ref,)
            or code.workspace_id != self.runtime.settings.workspace_id
            or str(code.request_id) != facts["execution_request"]["id"]
            or str(code.execution_id) != facts["execution"]["id"]
            or code.selection_id != manifest.input_selection_id
            or code.selection_hash != manifest.input_selection_hash
        ):
            raise ValueError("native_product_input_scope_refused")
        if "root_task" in facts and str(code.root_task_id) != facts["root_task"]["id"]:
            raise ValueError("native_product_input_root_refused")
        artifacts = {str(item.artifact_id): item for item in manifest.artifacts}
        seen = set()
        for item in results["inputs"]:
            artifact = artifacts.get(item.get("artifact_id"))
            if artifact is None or item["artifact_id"] in seen:
                raise ValueError("native_product_input_artifact_refused")
            seen.add(item["artifact_id"])
            expected = artifact.to_mapping()
            if any(
                item.get(key) != expected[key]
                for key in (
                    "content_hash",
                    "size_bytes",
                    "media_type",
                    "sources",
                    "group_keys",
                    "hard_refs",
                )
            ):
                raise ValueError("native_product_input_metadata_refused")
            encoded = item["body_text"].encode("utf-8")
            if (
                len(encoded) != artifact.size_bytes
                or hashlib.sha256(encoded).hexdigest() != artifact.content_hash
            ):
                raise ValueError("native_product_input_bytes_refused")
            if (
                artifact.group_keys != groups
                or artifact.hard_refs != (hard_ref,)
                or len(artifact.sources) != 1
            ):
                raise ValueError("native_product_input_lineage_refused")
            source = artifact.sources[0]
            if (
                source.worker_item_key != upstream
                or str(source.worker_item_id) != original["worker_item"]["id"]
                or str(source.effective_request_id)
                != original["execution_request"]["id"]
                or str(source.effective_execution_id) != original["execution"]["id"]
                or str(source.effective_lineage_id)
                != original["execution"]["lineage_id"]
                or source.allowed_lineage_ids != (source.effective_lineage_id,)
                or source.attempt != original["execution"]["attempt"]
                or source.inheritance_refs
            ):
                raise ValueError("native_product_input_original_lineage_refused")
            evidence = [
                entry.to_mapping()
                for entry in manifest.evidence
                if artifact.artifact_id in entry.artifact_ids
            ]
            if item.get("candidate_evidence") != evidence:
                raise ValueError("native_product_input_evidence_refused")
        if seen != artifacts.keys() or not any(
            item.artifact_type == "diff" for item in manifest.artifacts
        ):
            raise ValueError("native_product_input_diff_missing")
        # CP has already installed this physical input. Inspect it; never apply its diff again.
        source_file = self._owned_file(run_id, "native_value.py")
        if not source_file.is_file() or source_file.read_text() != "VALUE = 42\n":
            raise ValueError("native_product_physical_source_refused")
        if role == "integration":
            hard_file = self._owned_file(run_id, "hard_value.py")
            if (
                not hard_file.is_file()
                or hard_file.read_text()
                != "from native_value import VALUE\nANSWER = VALUE\n"
            ):
                raise ValueError("native_product_physical_hard_input_refused")
        directory = Path(self.requests[run_id].workspace.directory)
        self.physical_inputs.append(
            {
                "run_id": str(run_id),
                "role": role,
                "execution_id": str(code.execution_id),
                "input_hash": code.input_hash,
                "selection_id": str(code.selection_id),
                "selection_hash": code.selection_hash,
                "input_commit": local_git(directory, "rev-parse", "HEAD"),
                "input_tree": local_git(directory, "rev-parse", "HEAD^{tree}"),
                "artifact_hashes": [item.content_hash for item in manifest.artifacts],
            }
        )

    def prompt(self, handle, assembled_input, response_id):
        with self.lock:
            if self.handles.get(handle.run_id) != handle:
                raise ValueError("native_product_handle_refused")
            role = self.roles[handle.run_id]
            prior = sum(item["run_id"] == str(handle.run_id) for item in self.inputs)
            self.calls.append(("prompt", handle.run_id, response_id))
            self.inputs.append(
                {
                    "run_id": str(handle.run_id),
                    "response_id": str(response_id),
                    "role": role,
                    "text_hash": hashlib.sha256(
                        assembled_input.text.encode()
                    ).hexdigest(),
                }
            )
            self.assembled_inputs.append((handle.run_id, response_id, assembled_input))
            self.inspections[handle.run_id] = NativeInspection(
                process_state="alive", response_state="active", reason_code=None
            )
        if role == "main":
            if not prior or response_id in self.goal_responses:
                args = {
                    "request_id": str(
                        uuid5(self.runtime.settings.workspace_id, "native-product-goal")
                    ),
                    "title": "Native product result",
                    "objective": "Build the checked result",
                    "acceptance_criteria": ["acceptance:one"],
                    "requested_outputs": ["diff"],
                    "constraints": [],
                    "context_refs": [],
                }
                result = sdk_call(
                    self.connections[handle.run_id], "sidera_submit_goal", args
                )
                if (
                    result.get("outcome") != "applied"
                    or not result.get("root_task_id")
                    or not result.get("planning_request_id")
                ):
                    raise ValueError("native_product_original_goal_incomplete")
                self.goal = {
                    key: result[key]
                    for key in ("command_id", "root_task_id", "planning_request_id")
                }
                self.goal["request_id"] = args["request_id"]
                self.goal_responses[response_id] = self.goal
            self.emit(handle, response_id, json.dumps(self.goal, separators=(",", ":")))
        elif role == "planner":
            self.emit(handle, response_id, native_proposal())
        elif prior:
            self.emit(
                handle,
                response_id,
                "Received original intervention: " + assembled_input.text,
            )
        else:
            sections = _sections(assembled_input)
            if not {"task", "facts"}.issubset(sections):
                raise ValueError("native_product_worker_context_missing")
            instructions = [
                json.loads(item)
                for item in assembled_input.instructions
                if item.startswith("{")
            ]
            roles = [item.get("role") for item in instructions if "role" in item]
            if len(roles) != 1 or roles[0] not in {
                "source",
                "hard-only",
                "integration",
            }:
                raise ValueError("native_product_worker_role_refused")
            worker_role, facts = roles[0], sections["facts"]
            if worker_role != "source":
                self._consume_inputs(handle.run_id, worker_role, sections, facts)
            with self.lock:
                self.worker_facts[worker_role] = facts
            if worker_role == "source" and handle.run_id == self.original_run_id:
                gate = self.source_gates.setdefault(handle.run_id, Event())
                self.source_entered.set()
                if not gate.wait(self.source_gate_seconds):
                    raise ValueError("native_product_original_prompt_not_released")
            if (
                handle.run_id not in self.cancelled
                and self.inspect(handle).process_state.value != "gone"
            ):
                name, body = {
                    "source": ("native_value.py", "VALUE = 42\n"),
                    "hard-only": (
                        "hard_value.py",
                        "from native_value import VALUE\nANSWER = VALUE\n",
                    ),
                    "integration": ("integration.txt", "Integrated 42\n"),
                }[worker_role]
                self._write(handle.run_id, name, body)
                self.emit(
                    handle,
                    response_id,
                    "Produced authorized " + worker_role + " output",
                )
        with self.lock:
            gone = self.inspections[handle.run_id].process_state.value == "gone"
            self.inspections[handle.run_id] = NativeInspection(
                process_state="gone" if gone else "alive",
                response_state="idle",
                reason_code=None,
            )
        return NativeOutcome(
            response_id=response_id,
            status="cancelled" if handle.run_id in self.cancelled else "completed",
            reason_code=None,
            observed_model=None,
            usage=None,
        )

    def emit(self, handle, response_id, text):
        with self.lock:
            events = self.events.setdefault(handle.run_id, [])
            events.append(
                NativeEvent(
                    cursor=len(events) + 1,
                    native_id="owned-native-event-" + str(len(events) + 1),
                    kind="message",
                    response_id=response_id,
                    operation_id=None,
                    occurred_at=datetime.now(UTC),
                    data={"update_type": "agent_message", "text": text},
                )
            )

    def read_events(self, handle, after_cursor, limit=100):
        with self.lock:
            return tuple(
                event
                for event in self.events.get(handle.run_id, ())
                if event.cursor > after_cursor
            )[:limit]

    def release_accepted_prompt(self, run_id: UUID, operation_id: UUID) -> bool:
        if run_id != self.original_run_id or run_id not in self.source_gates:
            return False
        verified = self.authorities[run_id]
        try:
            receipt = self.runtime.execution.get_input(
                self.runtime.settings.workspace_id,
                run_id,
                operation_id,
                grant_id=verified.record.native_grant_id or verified.record.grant_id,
            )
        except (AgentError, KeyError):
            return False
        if (
            receipt.run_id != run_id
            or receipt.operation_id != operation_id
            or receipt.status != InputStatus.ACCEPTED
        ):
            return False
        with self.lock:
            previous = self.accepted_inputs.get(run_id)
            if previous is not None and previous != operation_id:
                return False
            self.accepted_inputs[run_id] = operation_id
        return self.release_original_prompt(run_id)

    def release_original_prompt(self, run_id: UUID) -> bool:
        with self.lock:
            if run_id != self.original_run_id or run_id not in self.accepted_inputs:
                return False
            self.source_gates[run_id].set()
            return True

    def cancel(self, handle, response_id):
        with self.lock:
            self.calls.append(("cancel", handle.run_id, response_id))
            unknown = handle.run_id in self.unknown_exits
            if not unknown:
                self.cancelled.add(handle.run_id)
                if handle.run_id in self.source_gates:
                    self.source_gates[handle.run_id].set()
            return NativeCancelOutcome(
                response_id=response_id,
                status="unknown" if unknown else "confirmed",
                reason_code="controlled_exit_unknown" if unknown else None,
            )

    def close(self, handle):
        with self.lock:
            self.close_attempts.add(handle.run_id)
            if (
                handle.run_id in self.unknown_exits
                and self.inspections[handle.run_id].process_state.value != "gone"
            ):
                self.calls.append(("close", handle.run_id))
                return NativeCloseOutcome(
                    status="unknown", reason_code="controlled_exit_unknown"
                )
            return super().close(handle)

    def confirm_original_exit(self, run_id: UUID) -> bool:
        with self.lock:
            if run_id != self.original_run_id or run_id not in self.close_attempts:
                return False
            self.inspections[run_id] = NativeInspection(
                process_state="gone", response_state="idle", reason_code=None
            )
            self.unknown_exits.discard(run_id)
            self.cancelled.add(run_id)
            if run_id in self.source_gates:
                self.source_gates[run_id].set()
            return True

    def public_facts(self):
        with self.lock:
            worker_ids = {
                role: {
                    section: {
                        key: facts[section][key]
                        for key in keys
                        if key in facts.get(section, {})
                    }
                    for section, keys in {
                        "execution": ("id", "lineage_id", "attempt"),
                        "execution_request": ("id",),
                        "worker_item": ("id",),
                    }.items()
                }
                for role, facts in self.worker_facts.items()
            }
            return {
                "opens": deepcopy(self.open_facts),
                "inputs": deepcopy(self.inputs),
                "goal": deepcopy(self.goal),
                "original_run_id": None
                if self.original_run_id is None
                else str(self.original_run_id),
                "accepted_inputs": {
                    str(run): str(op) for run, op in self.accepted_inputs.items()
                },
                "worker_facts": worker_ids,
                "physical_inputs": deepcopy(self.physical_inputs),
                "inspections": {
                    str(run): {
                        "process_state": value.process_state.value,
                        "response_state": value.response_state.value,
                    }
                    for run, value in self.inspections.items()
                },
            }
