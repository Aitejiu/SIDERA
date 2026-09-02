from __future__ import annotations

import json
import stat
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextMaterializationV1,
    ContextMetadata,
    ContextPurpose,
    ControlPlaneFactSection,
    SystemConstraintSection,
    WorkContextSection,
)
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import HandoffInspectionOutcome
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
    workspace_manifest_digest,
)

pytestmark = pytest.mark.integration

HASH = "a" * 64
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _context_package(
    execution_id: UUID, *, handoff_instruction: str | None = None
) -> ContextMaterializationV1:
    facts: dict[str, object] = {"workspace": "opaque-test-workspace"}
    if handoff_instruction is not None:
        facts["handoff"] = {"instruction": handoff_instruction}
    return ContextMaterializationV1(
        metadata=ContextMetadata(
            package_id=uuid4(),
            package_hash=HASH,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=execution_id,
            context_version=1,
            expires_at=NOW + timedelta(hours=1),
        ),
        system_constraints=SystemConstraintSection(
            objective="Continue the exact execution context",
            constraints=(),
            acceptance_criteria=(),
        ),
        work_context=WorkContextSection(
            revision_id=uuid4(),
            revision_number=1,
            content_hash=HASH,
            snapshot_schema_version=1,
            requested_outputs=(),
            clarification_decisions=(),
            accepted_decisions=(),
            unresolved_questions=(),
            checkpoint_refs=(),
            missing_fact_codes=(),
        ),
        control_plane_facts=ControlPlaneFactSection(facts=facts),
        project_knowledge=(),
        artifact_and_evidence_refs=(),
    )


def _spec(
    execution_id: UUID,
    *,
    fencing_token: int,
    context_package: ContextMaterializationV1,
) -> ExecutionSpec:
    from sidera_control_plane.domain.context_packages import context_package_reference

    return ExecutionSpec(
        schema_version=2,
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=execution_id,
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=fencing_token,
        goal="Continue the execution",
        acceptance_criteria=("the handoff boundary is exact",),
        context_package_ref=context_package_reference(
            context_package.metadata.package_id,
            context_package.metadata.package_hash,
        ),
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="repo://sidera",
                base_revision="b" * 40,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={},
        sandbox_policy={"mode": "filesystem"},
        budget={"max_seconds": 60},
        deadline=None,
        expected_outputs=("diff",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
        context_package=context_package,
    )


def _workspace(spec: ExecutionSpec, workspace_root: Path) -> ExecutionWorkspaceManifest:
    path = workspace_root / str(spec.execution_id)
    path.mkdir()
    return ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=spec.workspace_id,
        task_id=spec.task_id,
        execution_id=spec.execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision="b" * 40,
                branch=f"sidera/execution/{spec.execution_id}",
                current_head="b" * 40,
            ),
        ),
        location_ref=f"git-worktree://{spec.execution_id}",
        retention_seconds=3600,
        fencing_token=spec.fencing_token,
        created_at=NOW,
    )


def _fake_handoff_acp(tmp_path: Path) -> tuple[Path, Path, Path]:
    executable = tmp_path / "fake-opencode-handoff"
    log_path = tmp_path / "acp-messages.jsonl"
    count_path = tmp_path / "acp-process-count"
    executable.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            session_id = f"handoff-session-{os.getpid()}"
            log_path = os.environ["SIDERA_TEST_ACP_LOG"]
            count_path = os.environ["SIDERA_TEST_ACP_COUNT"]
            Path(count_path).write_text(
                str(int(Path(count_path).read_text() or "0") + 1)
                if Path(count_path).exists()
                else "1"
            )

            def log(message):
                with Path(log_path).open("a") as stream:
                    stream.write(json.dumps(message, sort_keys=True) + "\\n")

            def write(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()

            for raw in sys.stdin:
                request = json.loads(raw)
                log(request)
                method = request.get("method")
                request_id = request.get("id")
                if method == "initialize":
                    write({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": 1,
                            "agentCapabilities": {"loadSession": False},
                            "agentInfo": {"name": "fake-opencode", "version": "handoff-test"},
                        },
                    })
                elif method == "session/new":
                    write({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "sessionId": session_id,
                            "configOptions": [{
                                "id": "mode",
                                "name": "Session Mode",
                                "category": "mode",
                                "type": "select",
                                "currentValue": "build",
                                "options": [
                                    {"value": "build", "name": "build"},
                                    {"value": "plan", "name": "plan"},
                                ],
                            }],
                        },
                    })
                elif method == "session/set_mode":
                    write({"jsonrpc": "2.0", "id": request_id, "result": {}})
                elif method == "session/prompt":
                    prompt = request["params"].get("prompt", [])
                    text = " ".join(
                        part.get("text", "")
                        for part in prompt
                        if isinstance(part, dict)
                    )
                    summary = "checkpoint ready" if "SIDERA_HANDOFF_CHECKPOINT_V1" in text else "resumed"
                    write({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": summary},
                            },
                        },
                    })
                    write({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"stopReason": "end_turn"},
                    })
                elif method == "session/cancel":
                    continue
            """
        ).strip()
        + "\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, log_path, count_path


def _logged_messages(log_path: Path) -> list[dict[str, object]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines()]


def test_opencode_handoff_contract_uses_exact_sessions_and_one_writer_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, log_path, count_path = _fake_handoff_acp(tmp_path)
    monkeypatch.setenv("SIDERA_TEST_ACP_LOG", str(log_path))
    monkeypatch.setenv("SIDERA_TEST_ACP_COUNT", str(count_path))
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        workspace_root=tmp_path,
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
    )

    profile = adapter.probe({"cwd": str(tmp_path)})
    assert profile.handoff_checkpoint_mode.value == "control-event"
    assert profile.handoff_checkpoint_versions == (1,)
    assert profile.handoff_session_context_versions == (1,)
    assert profile.handoff_writer_gate_versions == (1,)
    assert profile.handoff_inspection is True
    log_path.write_text("")

    source_execution_id = uuid4()
    source_spec = _spec(
        source_execution_id,
        fencing_token=3,
        context_package=_context_package(source_execution_id),
    )
    source_workspace = _workspace(source_spec, tmp_path)
    source_handle = adapter.start_session(
        source_spec, source_workspace, operation_id=uuid4()
    )
    accepted = adapter.send_input(
        source_handle,
        "perform the ordinary turn",
        (),
        operation_id=uuid4(),
    )
    assert accepted.outcome is DeliveryOutcome.DELIVERED
    assert accepted.accepted_sequence is not None

    transition_id = uuid4()
    checkpoint = adapter.capture_handoff_checkpoint(
        source_handle,
        transition_id=transition_id,
        source_input_cutoff_sequence=accepted.accepted_sequence,
        operation_id=uuid4(),
    )
    assert checkpoint.source_session_id == source_handle.execution_session_id
    assert checkpoint.accepted_sequence == accepted.accepted_sequence
    assert source_spec.context_package is not None
    assert checkpoint.context_package_hash == source_spec.context_package.metadata.package_hash
    assert checkpoint.workspace_digest == workspace_manifest_digest(source_workspace)
    assert checkpoint.quiescent is True

    blocked = adapter.send_input(
        source_handle,
        "this must not start another turn",
        (),
        operation_id=uuid4(),
    )
    assert blocked.outcome is DeliveryOutcome.BLOCKED
    assert blocked.failure_code == "handoff_checkpointed"

    checkpoint_inspection = adapter.inspect_handoff_operation(
        source_handle, operation_id=checkpoint.operation_id
    )
    assert checkpoint_inspection.outcome is HandoffInspectionOutcome.SUCCEEDED
    absent = adapter.inspect_handoff_operation(source_handle, operation_id=uuid4())
    assert absent.outcome is HandoffInspectionOutcome.ABSENT

    stop_operation_id = gateway_operation_id(
        source_spec.execution_id, GatewayOperationType.STOP_SESSION
    )
    stopped = adapter.stop_session(source_handle, operation_id=stop_operation_id)
    repeated_stop = adapter.stop_session(source_handle, operation_id=stop_operation_id)
    assert stopped.outcome is DeliveryOutcome.DELIVERED
    assert stopped.execution_session_id == source_handle.execution_session_id
    assert stopped.accepted_sequence == checkpoint.accepted_sequence
    assert repeated_stop == stopped
    stop_inspection = adapter.inspect_handoff_operation(
        source_handle, operation_id=stop_operation_id
    )
    assert stop_inspection.outcome is HandoffInspectionOutcome.SUCCEEDED

    target_execution_id = uuid4()
    target_spec = _spec(
        target_execution_id,
        fencing_token=8,
        context_package=_context_package(
            target_execution_id,
            handoff_instruction="continue from the exact transferred context",
        ),
    )
    target_handle = adapter.start_session(
        target_spec, _workspace(target_spec, tmp_path), operation_id=uuid4()
    )
    assert target_spec.context_package is not None
    assert target_handle.execution_session_id != source_handle.execution_session_id
    assert target_handle.context_binding is not None
    assert target_handle.context_binding.consumer_execution_id == target_spec.execution_id
    assert target_handle.context_binding.package_id == target_spec.context_package.metadata.package_id

    target_blocked = adapter.send_input(
        target_handle,
        "a write before enable",
        (),
        operation_id=uuid4(),
    )
    assert target_blocked.outcome is DeliveryOutcome.BLOCKED
    assert target_blocked.failure_code == "handoff_writer_disabled"

    enable_operation_id = uuid4()
    enabled = adapter.enable_handoff_writer(
        target_handle,
        fencing_token=target_spec.fencing_token,
        operation_id=enable_operation_id,
    )
    duplicate_enable = adapter.enable_handoff_writer(
        target_handle,
        fencing_token=target_spec.fencing_token,
        operation_id=enable_operation_id,
    )
    assert enabled.outcome is DeliveryOutcome.DELIVERED
    assert duplicate_enable == enabled
    assert enabled.execution_session_id == target_handle.execution_session_id
    resumed = adapter.send_input(
        target_handle,
        "continue after the explicit writer enable",
        (),
        operation_id=uuid4(),
    )
    assert resumed.outcome is DeliveryOutcome.DELIVERED

    enable_inspection = adapter.inspect_handoff_operation(
        target_handle, operation_id=enable_operation_id
    )
    assert enable_inspection.outcome is HandoffInspectionOutcome.SUCCEEDED
    assert enable_inspection.context_binding == target_handle.context_binding

    messages = _logged_messages(log_path)
    method_names = [str(message.get("method")) for message in messages]
    assert "session/set_mode" in method_names
    resume_prompts = [
        message
        for message in messages
        if message.get("method") == "session/prompt"
        and "SIDERA_HANDOFF_RESUME_V1"
        in json.dumps(message, sort_keys=True)
    ]
    assert len(resume_prompts) == 1
    assert "perform the ordinary turn" not in json.dumps(resume_prompts[0])
    assert count_path.read_text() == "3"

    adapter.stop_session(target_handle, operation_id=uuid4())
