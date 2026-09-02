import json
import os
import stat
import subprocess
import textwrap
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
from sidera_control_plane.domain.gateway import DeliveryOutcome
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
)


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


def _fake_acp_executable(tmp_path: Path, *, hang_initialize: bool = False) -> Path:
    script = tmp_path / "fake-opencode"
    initialize_branch = (
        "while True: pass"
        if hang_initialize
        else 'write({"jsonrpc": "2.0", "id": request_id, "result": '
        '{"protocolVersion": 1, "agentCapabilities": '
        '{"loadSession": False}, "agentInfo": '
        '{"name": "fake-opencode", "version": "test"}}})'
    )
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            SESSION_ID = "acp-test-session"

            if capture_path := os.environ.get("SIDERA_TEST_CONFIG_CAPTURE"):
                Path(capture_path).write_text(
                    os.environ.get("OPENCODE_CONFIG_CONTENT", "")
                )

            def write(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()

            for raw in sys.stdin:
                request = json.loads(raw)
                method = request.get("method")
                request_id = request.get("id")
                if method == "initialize":
                    __INITIALIZE_BRANCH__
                elif method == "session/new":
                    write({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": SESSION_ID}})
                elif method == "session/prompt":
                    write({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": SESSION_ID, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}}}})
                    write({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})
                elif method == "session/cancel":
                    continue
            """
        ).replace("__INITIALIZE_BRANCH__", initialize_branch).strip()
        + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


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

    result = adapter.send_input(
        handle,
        "make the change",
        (),
        operation_id=uuid4(),
    )
    assert result.outcome is DeliveryOutcome.DELIVERED
    events = adapter.read_events(handle)
    assert any(event.event_type == "progress.updated" for event in events)
    progress = next(event for event in events if event.event_type == "progress.updated")
    assert progress.payload["trace_category"] == "assistant_summary"
    assert progress.payload["trace_summary"] == "done"

    stopped = adapter.stop_session(handle, operation_id=uuid4())
    assert stopped.outcome is DeliveryOutcome.DELIVERED


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
    subprocess.run(
        ["git", "-C", str(workspace_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace_path), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    (workspace_path / "README.md").write_text("changed\n")

    handle = adapter.start_session(spec, _workspace(spec), operation_id=uuid4())
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
