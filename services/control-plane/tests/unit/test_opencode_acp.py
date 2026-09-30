import json
import os
import stat
import subprocess
import textwrap
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.opencode_acp import (
    ACPTimeoutError,
    AdapterProbeError,
    OpenCodeACPAdapter,
    handoff_profile_supported,
)
from sidera_control_plane.domain.artifacts import ArtifactType
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.context_packages import (
    ContextMaterializationV2,
    ContextMaterializationV3,
    MaterializedResultInput,
)
from sidera_control_plane.domain.gateway import DeliveryOutcome
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
)
from tests.unit.result_input_fixtures import result_input_manifest
from tests.unit.result_integration_fixtures import context_v3_mapping
from tests.unit.test_context_package_v2 import _materialization_v1


def _spec() -> ExecutionSpec:
    return ExecutionSpec(
        schema_version=1,
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        goal="Implement the ACP adapter",
        acceptance_criteria=("the ACP handshake succeeds",),
        context_package_ref=None,
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="repo://sidera",
                base_revision="a" * 40,
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
    )


def _workspace(spec: ExecutionSpec) -> ExecutionWorkspaceManifest:
    return ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=spec.workspace_id,
        task_id=spec.task_id,
        execution_id=spec.execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision="a" * 40,
                branch=f"sidera/execution/{spec.execution_id}",
                current_head="a" * 40,
            ),
        ),
        location_ref=f"git-worktree://{spec.execution_id}",
        retention_seconds=3600,
        fencing_token=spec.fencing_token,
        created_at=datetime.now(UTC),
    )


def _v2_materialization() -> ContextMaterializationV2:
    legacy = _materialization_v1()
    artifact = result_input_manifest().artifacts[0]
    result_input = MaterializedResultInput(
        artifact_id=artifact.artifact_id,
        content_hash=artifact.content_hash,
        size_bytes=artifact.size_bytes,
        media_type=artifact.media_type,
        sources=artifact.sources,
        group_keys=artifact.group_keys,
        hard_refs=artifact.hard_refs,
        body_text="selected result body\n",
        candidate_evidence=(),
    )
    return ContextMaterializationV2(
        metadata=legacy.metadata,
        system_constraints=legacy.system_constraints,
        work_context=legacy.work_context,
        control_plane_facts=legacy.control_plane_facts,
        project_knowledge=legacy.project_knowledge,
        artifact_and_evidence_refs=legacy.artifact_and_evidence_refs,
        result_inputs=(result_input,),
    )


def _fake_acp_executable(
    tmp_path: Path,
    *,
    hang_initialize: bool = False,
    block_prompt: bool = False,
    prompt_stop_reason: str = "end_turn",
) -> Path:
    script = tmp_path / "fake-opencode"
    initialize_branch = (
        "while True: pass"
        if hang_initialize
        else 'write({"jsonrpc": "2.0", "id": request_id, "result": '
        '{"protocolVersion": 1, "agentCapabilities": '
        '{"loadSession": False}, "agentInfo": '
        '{"name": "fake-opencode", "version": "test"}}})'
    )
    prompt_branch = (
        'write({"jsonrpc": "2.0", "method": "session/update", "params": '
        '{"sessionId": SESSION_ID, "update": {"sessionUpdate": '
        '"agent_message_chunk", "content": {"type": "text", "text": "started"}}}})\n'
        '        threading.Thread(target=finish_prompt, args=(request_id,), daemon=True).start()'
        if block_prompt
        else 'write({"jsonrpc": "2.0", "method": "session/update", "params": '
        '{"sessionId": SESSION_ID, "update": {"sessionUpdate": "agent_message_chunk", '
        '"content": {"type": "text", "text": "done"}}}})\n'
        '        write({"jsonrpc": "2.0", "id": request_id, "result": '
        '{"stopReason": "__PROMPT_STOP_REASON__"}})'
    )
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import threading
            from pathlib import Path

            SESSION_ID = "acp-test-session"
            PROMPT_RELEASE = threading.Event()

            def finish_prompt(request_id):
                PROMPT_RELEASE.wait(timeout=30)
                write({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})

            if capture_path := os.environ.get("SIDERA_TEST_CONFIG_CAPTURE"):
                Path(capture_path).write_text(
                    os.environ.get("OPENCODE_CONFIG_CONTENT", "")
                )

            def write(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()

            for raw in sys.stdin:
                request = json.loads(raw)
                if capture_path := os.environ.get("SIDERA_TEST_REQUEST_CAPTURE"):
                    with Path(capture_path).open("a", encoding="utf-8") as captured:
                        captured.write(json.dumps(request) + "\\n")
                method = request.get("method")
                request_id = request.get("id")
                if method == "initialize":
                    __INITIALIZE_BRANCH__
                elif method == "session/new":
                    write({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": SESSION_ID}})
                elif method == "session/prompt":
                    __PROMPT_BRANCH__
                elif method == "session/cancel":
                    if cancel_capture := os.environ.get("SIDERA_TEST_CANCEL_CAPTURE"):
                        Path(cancel_capture).write_text("cancel-received")
                    PROMPT_RELEASE.set()
            """
        )
        .replace("__INITIALIZE_BRANCH__", initialize_branch)
        .replace("__PROMPT_BRANCH__", prompt_branch)
        .replace("__PROMPT_STOP_REASON__", prompt_stop_reason)
        .strip()
        + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_opencode_stop_is_responsive_while_prompt_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel_capture = tmp_path / "cancel-received"
    monkeypatch.setenv("SIDERA_TEST_CANCEL_CAPTURE", str(cancel_capture))
    executable = _fake_acp_executable(tmp_path, block_prompt=True)
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        workspace_root=tmp_path,
        request_timeout_seconds=5,
        stop_grace_seconds=0.2,
    )
    spec = _spec()
    (tmp_path / str(spec.execution_id)).mkdir()
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())

    result_holder: list[object] = []
    prompt_thread = threading.Thread(
        target=lambda: result_holder.append(
            adapter.send_input(handle, "hold this prompt", (), operation_id=uuid4())
        ),
        daemon=True,
    )
    prompt_thread.start()
    deadline = time.monotonic() + 2
    started = False
    while time.monotonic() < deadline and not started:
        started = any(
            event.event_type == "progress.updated"
            for event in adapter.read_events(handle)
        )
        if not started:
            time.sleep(0.01)
    assert started

    stop_started = time.monotonic()
    stopped = adapter.stop_session(handle, operation_id=uuid4())
    assert time.monotonic() - stop_started < 1
    assert stopped.outcome is DeliveryOutcome.DELIVERED
    prompt_thread.join(timeout=2)
    assert not prompt_thread.is_alive()
    assert result_holder
    assert result_holder[0].outcome is DeliveryOutcome.UNKNOWN
    assert cancel_capture.read_text() == "cancel-received"


def test_opencode_acp_probe_and_prompt_round_trip(tmp_path: Path) -> None:
    executable = _fake_acp_executable(tmp_path)
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        workspace_root=tmp_path,
        request_timeout_seconds=5,
        stop_grace_seconds=0.2,
    )

    profile = adapter.probe({"cwd": str(tmp_path)})
    assert profile.input.value == "queued"
    assert profile.resume.value == "same-process"
    assert profile.stability.value == "experimental"
    assert not handoff_profile_supported(profile)
    assert profile.handoff_checkpoint_versions == ()
    assert profile.handoff_session_context_versions == ()
    assert profile.handoff_writer_gate_versions == ()
    assert profile.handoff_inspection is False

    spec = _spec()
    (tmp_path / str(spec.execution_id)).mkdir()
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())
    assert handle.adapter_session_id == "acp-test-session"
    assert handle.process_ref.startswith("opencode://")

    operation_id = uuid4()
    result = adapter.send_input(
        handle,
        "make the change",
        (),
        operation_id=operation_id,
    )
    assert result.outcome is DeliveryOutcome.DELIVERED
    events = adapter.read_events(handle)
    assert any(event.event_type == "progress.updated" for event in events)
    progress = next(event for event in events if event.event_type == "progress.updated")
    assert progress.payload["trace_category"] == "assistant_summary"
    assert progress.payload["trace_summary"] == "done"
    accepted = next(event for event in events if event.event_type == "input.accepted")
    completed = next(event for event in events if event.event_type == "turn.completed")
    assert completed.sequence == accepted.sequence + 1
    assert completed.payload == {
        "schema_version": 1,
        "operation_id": str(operation_id),
        "stop_reason": "end_turn",
    }
    assert result.accepted_sequence == accepted.sequence
    assert adapter.probe({}).turn_completion_versions == (1,)

    session = adapter._session(handle)
    session.transport.process.terminate()
    session.transport.process.wait(timeout=2)
    terminal = adapter.observe_terminal(handle)
    assert terminal is not None
    assert terminal.event_type == "session.failed"
    assert terminal.payload["failure_code"] == "process_exited"
    assert adapter.observe_terminal(handle) is None

    stopped = adapter.stop_session(handle, operation_id=uuid4())
    assert stopped.outcome is DeliveryOutcome.DELIVERED


def test_opencode_acp_ordinary_inputs_bind_execution_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec()
    source_directory = tmp_path / "source-repository"
    source_directory.mkdir()
    execution_directory = tmp_path / str(spec.execution_id)
    execution_directory.mkdir()
    capture_path = tmp_path / "acp-requests.jsonl"
    monkeypatch.setenv("SIDERA_TEST_REQUEST_CAPTURE", str(capture_path))
    executable = _fake_acp_executable(tmp_path)
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        workspace_root=tmp_path,
        request_timeout_seconds=5,
        stop_grace_seconds=0.2,
    )
    source_file = source_directory / "README.md"
    attachment_uri = (source_directory / "reference.md").resolve().as_uri()
    initial_text = f"Update {source_file.resolve()} using the attached reference."
    followup_text = f"Continue the intervention for {source_file.resolve()}."
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())

    try:
        initial_result = adapter.send_input(
            handle,
            initial_text,
            (attachment_uri,),
            operation_id=uuid4(),
        )
        followup_result = adapter.send_input(
            handle,
            followup_text,
            (),
            operation_id=uuid4(),
        )
        assert initial_result.outcome is DeliveryOutcome.DELIVERED
        assert followup_result.outcome is DeliveryOutcome.DELIVERED
        accepted_events = [
            event
            for event in adapter.read_events(handle)
            if event.event_type == "input.accepted"
        ]
        assert [event.payload["text_length"] for event in accepted_events] == [
            len(initial_text),
            len(followup_text),
        ]
    finally:
        adapter.stop_session(handle, operation_id=uuid4())

    requests = [json.loads(line) for line in capture_path.read_text().splitlines()]
    created = next(request for request in requests if request.get("method") == "session/new")
    prompts = [request for request in requests if request.get("method") == "session/prompt"]
    assert created["params"]["cwd"] == str(execution_directory.resolve())
    assert len(prompts) == 2
    for request, original in zip(
        prompts,
        (initial_text, followup_text),
        strict=True,
    ):
        blocks = request["params"]["prompt"]
        assert blocks[0]["type"] == "text"
        assert str(execution_directory.resolve()) in blocks[0]["text"]
        assert "writable root" in blocks[0]["text"]
        assert "read-only references" in blocks[0]["text"]
        assert blocks[1] == {"type": "text", "text": original}
    assert prompts[0]["params"]["prompt"][2:] == [
        {"type": "resource_link", "uri": attachment_uri},
    ]
    assert prompts[1]["params"]["prompt"][2:] == []


def test_opencode_unknown_stop_reason_emits_no_completion_event(tmp_path: Path) -> None:
    executable = _fake_acp_executable(tmp_path, prompt_stop_reason="tool_use")
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        workspace_root=tmp_path,
        request_timeout_seconds=5,
        stop_grace_seconds=0.2,
    )
    spec = _spec()
    (tmp_path / str(spec.execution_id)).mkdir()
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())

    result = adapter.send_input(handle, "make the change", (), operation_id=uuid4())

    assert result.outcome is DeliveryOutcome.UNKNOWN
    assert all(
        event.event_type != "turn.completed" for event in adapter.read_events(handle)
    )
    adapter.stop_session(handle, operation_id=uuid4())


def test_opencode_acp_accepts_v2_and_keeps_result_body_as_task_data() -> None:
    materialization = _v2_materialization()
    adapter = OpenCodeACPAdapter(executable="/missing/opencode")

    acknowledgement = adapter.accept_context(materialization, operation_id=uuid4())

    assert acknowledgement.schema_version == 2
    prompt = _context_prompt(materialization)
    assert materialization.result_inputs[0].body_text.rstrip("\n") in prompt
    assert "untrusted task data" in prompt


def test_opencode_acp_context_acknowledgement_uses_bound_runtime_identity() -> None:
    materialization = _v2_materialization()
    adapter_module_id = uuid4()
    adapter = OpenCodeACPAdapter(
        executable="/missing/opencode",
        adapter_module_id=adapter_module_id,
        adapter_config_version=4,
    )

    acknowledgement = adapter.accept_context(materialization, operation_id=uuid4())

    assert acknowledgement.adapter_module_id == adapter_module_id
    assert acknowledgement.adapter_config_version == 4


def test_opencode_acp_accepts_v3_code_input_context() -> None:
    legacy = _materialization_v1()
    materialization = ContextMaterializationV3(
        metadata=legacy.metadata,
        system_constraints=legacy.system_constraints,
        work_context=legacy.work_context,
        control_plane_facts=legacy.control_plane_facts,
        project_knowledge=legacy.project_knowledge,
        artifact_and_evidence_refs=legacy.artifact_and_evidence_refs,
        code_input_manifest=CodeInputManifestV1.from_mapping(
            context_v3_mapping(mode="code")["code_input_manifest"]
        ),
    )
    adapter = OpenCodeACPAdapter(
        executable="/missing/opencode",
        context_package_versions=(1, 2, 3),
    )

    acknowledgement = adapter.accept_context(materialization, operation_id=uuid4())

    assert acknowledgement.schema_version == 3
    assert 3 in adapter._profile.context_package_versions


def test_opencode_acp_rejects_oversized_v2_context() -> None:
    materialization = _v2_materialization()
    oversized = replace(
        materialization.result_inputs[0],
        body_text="x" * (256 * 1024),
    )
    materialization = replace(materialization, result_inputs=(oversized,))
    adapter = OpenCodeACPAdapter(executable="/missing/opencode")

    with pytest.raises(ValueError, match="size limit"):
        adapter.accept_context(materialization, operation_id=uuid4())


def _context_prompt(materialization: ContextMaterializationV2) -> str:
    from sidera_control_plane.application.worker_gateway import _initial_prompt

    return _initial_prompt(
        ExecutionSpec(
            schema_version=2,
            workspace_id=materialization.metadata.consumer_id,
            task_id=uuid4(),
            execution_id=uuid4(),
            worker_id=uuid4(),
            lease_id=uuid4(),
            fencing_token=1,
            goal="Use the selected result",
            acceptance_criteria=("the result is reviewed",),
            context_package_ref=(
                f"context-package://{materialization.metadata.package_id}/"
                f"{materialization.metadata.package_hash}"
            ),
            repositories=(
                RepositoryCheckoutSpec(
                    repository_ref="repo://sidera",
                    base_revision="a" * 40,
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
            context_package=materialization,
        )
    )


def test_opencode_acp_allows_only_opencode_go_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_path = tmp_path / "opencode-config.json"
    monkeypatch.setenv("SIDERA_TEST_CONFIG_CAPTURE", str(capture_path))
    adapter = OpenCodeACPAdapter(
        executable=str(_fake_acp_executable(tmp_path)),
        model="opencode-go/mimo-v2.5",
        request_timeout_seconds=5,
        stop_grace_seconds=0.2,
    )

    adapter.probe({"cwd": str(tmp_path)})

    assert json.loads(capture_path.read_text()) == {
        "disabled_providers": ["deepseek"],
        "enabled_providers": ["opencode-go"],
        "model": "opencode-go/mimo-v2.5",
        "permission": {"external_directory": "allow"},
    }


def test_opencode_acp_missing_executable_is_unhealthy() -> None:
    adapter = OpenCodeACPAdapter(executable="/missing/opencode")

    with pytest.raises(AdapterProbeError, match="unavailable"):
        adapter.probe({})


def test_opencode_acp_collects_git_diff_and_acceptance_provenance(
    tmp_path: Path,
) -> None:
    executable = _fake_acp_executable(tmp_path)
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        workspace_root=tmp_path,
        request_timeout_seconds=5,
        stop_grace_seconds=0.2,
    )
    spec = _spec()
    workspace_path = tmp_path / str(spec.execution_id)
    workspace_path.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(workspace_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace_path), "config", "user.name", "SIDERA Test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace_path),
            "config",
            "user.email",
            "test@sidera.local",
        ],
        check=True,
        capture_output=True,
    )
    (workspace_path / "README.md").write_text("base\n")
    (workspace_path / ".gitignore").write_text("ignored.txt\n")
    subprocess.run(
        ["git", "-C", str(workspace_path), "add", "README.md", ".gitignore"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace_path), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())
    source_directory = tmp_path / "source-repository"
    source_directory.mkdir()
    (source_directory / "external-change.txt").write_text("outside execution workspace\n")
    assert adapter.collect_artifacts(handle, operation_id=uuid4()) == ()

    (workspace_path / "README.md").write_text("changed\n")
    subprocess.run(
        ["git", "-C", str(workspace_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    (workspace_path / "NEW_FILE.txt").write_text("new artifact\n")
    (workspace_path / "EMPTY_FILE.txt").touch()
    (workspace_path / "ignored.txt").write_text("must not be collected\n")

    candidates = adapter.collect_artifacts(handle, operation_id=uuid4())

    assert len(candidates) == 1
    assert candidates[0].artifact_type is ArtifactType.DIFF
    assert candidates[0].media_type == "text/x-diff"
    assert candidates[0].provenance["acceptance_criterion_refs"] == (
        "the ACP handshake succeeds",
    )
    assert candidates[0].size_bytes > 0
    assert isinstance(candidates[0].content, bytes)
    assert len(candidates[0].content) == candidates[0].size_bytes
    assert b"README.md" in candidates[0].content
    assert b"NEW_FILE.txt" in candidates[0].content
    assert b"new artifact" in candidates[0].content
    assert b"EMPTY_FILE.txt" in candidates[0].content
    assert b"ignored.txt" not in candidates[0].content
    assert b"must not be collected" not in candidates[0].content
    adapter.stop_session(handle, operation_id=uuid4())


def test_opencode_acp_stop_requires_proven_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_acp_executable(tmp_path)
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        workspace_root=tmp_path,
        request_timeout_seconds=5,
        stop_grace_seconds=0.1,
    )
    spec = _spec()
    (tmp_path / str(spec.execution_id)).mkdir()
    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())

    monkeypatch.setattr(
        "sidera_control_plane.adapters.opencode_acp._identity_matches",
        lambda _identity: False,
    )
    result = adapter.stop_session(handle, operation_id=uuid4())
    assert result.outcome is DeliveryOutcome.UNKNOWN
    assert result.failure_code == "process_identity_unproven"
    assert adapter.active_processes()

    monkeypatch.undo()
    stopped = adapter.stop_session(handle, operation_id=uuid4())
    assert stopped.outcome is DeliveryOutcome.DELIVERED


def test_opencode_acp_timeout_is_never_reported_as_success(tmp_path: Path) -> None:
    executable = _fake_acp_executable(tmp_path, hang_initialize=True)
    adapter = OpenCodeACPAdapter(
        executable=str(executable),
        request_timeout_seconds=0.1,
        stop_grace_seconds=0.1,
    )

    with pytest.raises(ACPTimeoutError):
        adapter.probe({"cwd": str(tmp_path)})

    for process in adapter.active_processes():
        try:
            os.kill(process.pid, 9)
        except ProcessLookupError:
            pass
