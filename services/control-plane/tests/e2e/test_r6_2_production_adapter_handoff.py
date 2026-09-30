"""Provider-free R6.2 C handoff through the production ACP adapters."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from sidera_control_plane.adapters.dsh_acp import DshACPAdapter
from sidera_control_plane.adapters.opencode_acp import (
    OpenCodeACPAdapter,
    handoff_profile_supported,
)
from sidera_control_plane.domain.context_packages import (
    ContextMaterializationV1,
    ControlPlaneFactSection,
    context_package_reference,
)
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
    WorkspaceLifecycle,
)
from tests.unit.test_context_package_v2 import _materialization_v1

pytestmark = pytest.mark.integration


_ACP_HOST = textwrap.dedent(
    """
    import json
    import os
    import sys

    session_id = os.environ.get("SIDERA_ACP_SESSION_ID", "acp-c-session")
    capture = os.environ.get("SIDERA_ACP_CAPTURE")
    label = os.environ.get("SIDERA_ACP_LABEL", "unknown")

    def record(method, **payload):
        if not capture:
            return
        with open(capture, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"label": label, "method": method, **payload}) + "\\n")

    def write(message):
        sys.stdout.write(json.dumps(message) + "\\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        request = json.loads(raw)
        method = request.get("method")
        record(method, params=request.get("params", {}))
        if method == "initialize":
            write({"id": request["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": False},
            }})
        elif method == "session/new":
            write({"id": request["id"], "result": {
                "sessionId": session_id,
                "configOptions": [{"id": "mode", "options": [
                    {"value": "plan"}, {"value": "build"}
                ]}],
            }})
        elif method == "session/set_mode":
            write({"id": request["id"], "result": {}})
        elif method == "session/prompt":
            prompt = request["params"]["prompt"][0]["text"]
            record("prompt_text", text=prompt)
            write({"method": "session/update", "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "agent_message", "content": {
                    "type": "text", "text": "committed"
                }},
            }})
            write({"id": request["id"], "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            continue
    """
).strip()


def _write_acp_host(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n" + _ACP_HOST + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o100)
    return path


def _workspace(
    root: Path,
    *,
    workspace_id: UUID,
    task_id: UUID,
    execution_id: UUID,
    fencing_token: int,
    now: datetime,
) -> ExecutionWorkspaceManifest:
    location = f"acp-c://{execution_id}"
    path = root / str(execution_id)
    path.mkdir(parents=True, exist_ok=True)
    manifest = ExecutionWorkspaceManifest.create(
        id=uuid5(NAMESPACE_URL, f"sidera.r6-2-c.workspace:{execution_id}"),
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="acp-c-repository",
                base_revision="a" * 40,
                branch=f"sidera/r6-2-c/{execution_id}",
                current_head="a" * 40,
            ),
        ),
        location_ref=location,
        retention_seconds=3600,
        fencing_token=fencing_token,
        created_at=now,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    assert manifest.lifecycle is WorkspaceLifecycle.ACTIVE
    return manifest


def _context(
    execution_id: UUID,
    *,
    handoff_instruction: str | None,
) -> ContextMaterializationV1:
    base = _materialization_v1()
    facts: dict[str, object] = {"c_root": "provider-free-real-adapter-path"}
    if handoff_instruction is not None:
        facts["handoff"] = {"instruction": handoff_instruction}
    return replace(
        base,
        metadata=replace(
            base.metadata,
            consumer_id=execution_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        control_plane_facts=ControlPlaneFactSection(facts=facts),
    )


def _spec(
    *,
    workspace_id: UUID,
    task_id: UUID,
    execution_id: UUID,
    worker_id: UUID,
    lease_id: UUID,
    adapter_module_id: UUID,
    workspace_module_id: UUID,
    context: ContextMaterializationV1,
    fencing_token: int,
) -> ExecutionSpec:
    return ExecutionSpec(
        schema_version=2,
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        worker_id=worker_id,
        lease_id=lease_id,
        fencing_token=fencing_token,
        goal="Continue the same WorkerItem after an agent handoff.",
        acceptance_criteria=("the target session receives the continuation",),
        context_package_ref=context_package_reference(
            context.metadata.package_id, context.metadata.package_hash
        ),
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="acp-c-repository",
                base_revision="a" * 40,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={"shell": "bounded"},
        sandbox_policy={"filesystem": "workspace"},
        budget={"class": "standard"},
        deadline=None,
        expected_outputs=("diff",),
        adapter_module_ref=adapter_module_id,
        adapter_config_version=1,
        workspace_module_ref=workspace_module_id,
        workspace_config_version=1,
        context_package=context,
    )


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_c_root_handoffs_from_opencode_to_dsh_target_only_adapter(
    tmp_path: Path,
) -> None:
    """Run the C path through both production ACP protocol implementations."""

    now = datetime.now(UTC)
    root_task_id = uuid4()
    worker_item_id = uuid4()
    workspace_id = uuid4()
    source_execution_id = uuid4()
    target_execution_id = uuid4()
    source_worker_id = uuid4()
    target_worker_id = uuid4()
    source_lease_id = uuid4()
    target_lease_id = uuid4()
    source_adapter_module_id = uuid4()
    target_adapter_module_id = uuid4()
    workspace_module_id = uuid4()
    transition_id = uuid5(worker_item_id, "handoff")
    capture = tmp_path / "acp-c.ndjson"
    opencode_host = _write_acp_host(tmp_path, "fake-opencode-c")
    dsh_env = {
        "PATH": os.environ.get("PATH", ""),
        "SIDERA_ACP_CAPTURE": str(capture),
        "SIDERA_ACP_LABEL": "dsh-target",
        "SIDERA_ACP_SESSION_ID": "dsh-target-session",
    }
    source_adapter = OpenCodeACPAdapter(
        executable=str(opencode_host),
        workspace_root=tmp_path / "workspaces",
        request_timeout_seconds=2,
        handoff_request_timeout_seconds=2,
        stop_grace_seconds=0.2,
    )
    target_adapter = DshACPAdapter(
        argv=(sys.executable, str(opencode_host)),
        process_env=dsh_env,
        workspace_root=tmp_path / "workspaces",
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
        handoff_target_enabled=True,
    )
    source_workspace = _workspace(
        tmp_path / "workspaces",
        workspace_id=workspace_id,
        task_id=root_task_id,
        execution_id=source_execution_id,
        fencing_token=1,
        now=now,
    )
    target_workspace = _workspace(
        tmp_path / "workspaces",
        workspace_id=workspace_id,
        task_id=root_task_id,
        execution_id=target_execution_id,
        fencing_token=2,
        now=now,
    )
    source_context = _context(source_execution_id, handoff_instruction=None)
    target_context = replace(
        source_context,
        metadata=replace(
            source_context.metadata,
            consumer_id=target_execution_id,
        ),
        control_plane_facts=ControlPlaneFactSection(
            facts={
                "c_root": "provider-free-real-adapter-path",
                "handoff": {
                    "instruction": "Continue the existing WorkerItem from the bound context package."
                },
            }
        ),
    )
    source_spec = _spec(
        workspace_id=workspace_id,
        task_id=root_task_id,
        execution_id=source_execution_id,
        worker_id=source_worker_id,
        lease_id=source_lease_id,
        adapter_module_id=source_adapter_module_id,
        workspace_module_id=workspace_module_id,
        context=source_context,
        fencing_token=1,
    )
    target_spec = _spec(
        workspace_id=workspace_id,
        task_id=root_task_id,
        execution_id=target_execution_id,
        worker_id=target_worker_id,
        lease_id=target_lease_id,
        adapter_module_id=target_adapter_module_id,
        workspace_module_id=workspace_module_id,
        context=target_context,
        fencing_token=2,
    )

    try:
        source_profile = source_adapter.probe(
            {"cwd": str(tmp_path / "workspaces" / str(source_execution_id))}
        )
        target_profile = target_adapter.probe(
            {"cwd": str(tmp_path / "workspaces" / str(target_execution_id))}
        )
        assert handoff_profile_supported(source_profile)
        assert target_profile.handoff_checkpoint_mode.value == "unsupported"
        assert target_profile.handoff_session_context_versions == (1,)
        assert target_profile.handoff_writer_gate_versions == (1,)
        assert target_profile.handoff_inspection is True

        source_ack = source_adapter.accept_context(
            source_context,
            operation_id=uuid4(),
        )
        assert source_ack.package_hash == source_context.metadata.package_hash
        source_handle = source_adapter.start_session(
            source_spec,
            source_workspace,
            operation_id=gateway_operation_id(
                source_execution_id, GatewayOperationType.START_SESSION
            ),
        )
        source_input = source_adapter.send_input(
            source_handle,
            "OpenCode source work before handoff.",
            (),
            operation_id=gateway_operation_id(
                source_execution_id, GatewayOperationType.SEND_INPUT
            ),
        )
        assert source_input.outcome is DeliveryOutcome.DELIVERED
        checkpoint_operation_id = uuid4()
        checkpoint = source_adapter.capture_handoff_checkpoint(
            source_handle,
            transition_id=transition_id,
            source_input_cutoff_sequence=source_input.accepted_sequence or 0,
            operation_id=checkpoint_operation_id,
        )
        assert checkpoint.quiescent is True
        assert checkpoint.context_package_id == target_context.metadata.package_id
        assert checkpoint.context_package_hash == target_context.metadata.package_hash
        assert source_adapter.capture_handoff_checkpoint(
            source_handle,
            transition_id=transition_id,
            source_input_cutoff_sequence=source_input.accepted_sequence or 0,
            operation_id=checkpoint_operation_id,
        ) == checkpoint
        blocked_source = source_adapter.send_input(
            source_handle,
            "This must not be written by the fenced source.",
            (),
            operation_id=uuid4(),
        )
        assert blocked_source.outcome is DeliveryOutcome.BLOCKED
        assert blocked_source.failure_code == "handoff_checkpointed"
        source_stop = source_adapter.stop_session(
            source_handle,
            operation_id=gateway_operation_id(
                source_execution_id, GatewayOperationType.STOP_SESSION
            ),
        )
        assert source_stop.outcome is DeliveryOutcome.DELIVERED
        assert source_stop.accepted_sequence == checkpoint.accepted_sequence

        target_ack = target_adapter.accept_context(
            target_context,
            operation_id=uuid4(),
        )
        assert target_ack.consumer_id == target_execution_id
        assert target_ack.package_id == checkpoint.context_package_id
        target_handle = target_adapter.start_session(
            target_spec,
            target_workspace,
            operation_id=gateway_operation_id(
                target_execution_id, GatewayOperationType.START_SESSION
            ),
        )
        assert target_handle.context_binding is not None
        assert target_handle.context_binding.consumer_execution_id == target_execution_id
        assert target_handle.context_binding.package_hash == checkpoint.context_package_hash
        target_before_writer = target_adapter.send_input(
            target_handle,
            "The target must remain held until writer enable.",
            (),
            operation_id=uuid4(),
        )
        assert target_before_writer.outcome is DeliveryOutcome.BLOCKED
        assert target_before_writer.failure_code == "handoff_writer_disabled"
        wrong_fence = target_adapter.enable_handoff_writer(
            target_handle,
            fencing_token=1,
            operation_id=uuid4(),
        )
        assert wrong_fence.outcome is DeliveryOutcome.FAILED
        assert wrong_fence.failure_code == "handoff_fencing_mismatch"
        enable_operation_id = uuid4()
        enabled = target_adapter.enable_handoff_writer(
            target_handle,
            fencing_token=2,
            operation_id=enable_operation_id,
        )
        assert enabled.outcome is DeliveryOutcome.DELIVERED
        assert target_adapter.enable_handoff_writer(
            target_handle,
            fencing_token=2,
            operation_id=enable_operation_id,
        ) == enabled
        target_input = target_adapter.send_input(
            target_handle,
            "Continue after the handoff.",
            (),
            operation_id=uuid4(),
        )
        assert target_input.outcome is DeliveryOutcome.DELIVERED
        assert target_input.execution_session_id == target_handle.execution_session_id
        assert target_handle.execution_session_id != source_handle.execution_session_id
        target_events = target_adapter.read_events(target_handle)
        assert any(event.event_type == "handoff.writer_enabled" for event in target_events)
        assert any(event.event_type == "input.accepted" for event in target_events)
        target_stop = target_adapter.stop_session(
            target_handle,
            operation_id=gateway_operation_id(
                target_execution_id, GatewayOperationType.STOP_SESSION
            ),
        )
        assert target_stop.outcome is DeliveryOutcome.DELIVERED

        target_records = [record for record in _records(capture) if record["label"] == "dsh-target"]
        target_prompts = [record for record in target_records if record["method"] == "prompt_text"]
        assert len(target_prompts) == 2
        assert "SIDERA_HANDOFF_RESUME_V1" in str(target_prompts[0]["text"])
        assert "Continue after the handoff." in str(target_prompts[1]["text"])
        assert all(
            "OpenCode source work before handoff." not in str(record.get("text"))
            for record in target_prompts
        )
    finally:
        for adapter in (source_adapter, target_adapter):
            for process in adapter.active_processes():
                process.terminate()
