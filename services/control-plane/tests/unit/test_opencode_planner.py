import json
import stat
import textwrap
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sidera_runtime_guard import RuntimeGuardError

from sidera_control_plane.adapters import opencode_planner
from sidera_control_plane.adapters.acp_process import ACPProcessTransport
from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
    _proposal_payload,
)
from sidera_control_plane.adapters.opencode_planner import (
    OpenCodePlannerAdapter,
    OpenCodePlannerError,
    _parse_result,
    _planner_opencode_config,
    _planner_prompt,
    _proposal_shape_hint,
    _repair_prompt,
    _response_text,
    _safe_diagnostic,
)
from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogSnapshot,
    ContextReference,
    PlannerClarificationResult,
    PlannerExplicitFailure,
    PlannerInput,
    PlannerInputAssembler,
    PlannerPolicyConstraints,
    PlannerProposalResult,
    PlannerRunControl,
    PlannerUnknownOutcome,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningRequestKind,
)
from sidera_control_plane.domain.planning_profiles import PlanningProfileSnapshot
from sidera_control_plane.domain.planning_proposals import (
    PlanningProposalPayloadV4,
    PlanningProposalPayloadV5,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
    PlanningRevisionProposalPayloadV4,
)
from tests.unit.test_planning_commands import module_facts, profile

NOW = datetime.now(UTC)


def _run_control(operation_id: UUID, *, timeout_seconds: int = 30) -> PlannerRunControl:
    started_at = datetime.now(UTC)
    return PlannerRunControl(
        operation_id=operation_id,
        started_at=started_at,
        deadline_at=started_at + timedelta(seconds=timeout_seconds),
    )


def _fake_opencode(tmp_path: Path, *, hang_prompt: bool = False) -> Path:
    script = tmp_path / "fake-opencode-planner"
    script.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time

            SESSION_ID = "planner-session"
            prompt_count = 0
            seen_prompt_count = 0
            started_file = os.environ.get("SIDERA_TEST_PLANNER_STARTED_FILE")
            if started_file:
                with open(started_file, "w", encoding="utf-8") as stream:
                    stream.write("started")

            def write(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()

            def send_response(request):
                global prompt_count
                prompt_count += 1
                response = os.environ["SIDERA_TEST_PLANNER_RESPONSE"]
                if prompt_count > 1:
                    response = os.environ.get(
                        f"SIDERA_TEST_PLANNER_REPAIR_RESPONSE_{{prompt_count - 1}}",
                        os.environ.get("SIDERA_TEST_PLANNER_REPAIR_RESPONSE", response),
                    )
                write({{"jsonrpc": "2.0", "method": "session/update", "params": {{
                    "sessionId": SESSION_ID,
                    "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": response}}}}
                }}}})
                write({{"jsonrpc": "2.0", "id": request["id"], "result": {{"stopReason": "end_turn"}}}})

            for raw in sys.stdin:
                request = json.loads(raw)
                method = request.get("method")
                if method == "initialize":
                    write({{"jsonrpc": "2.0", "id": request["id"], "result": {{
                        "protocolVersion": 1,
                        "agentCapabilities": {{"loadSession": False}},
                    }}}})
                elif method == "session/new":
                    if os.environ.get("SIDERA_TEST_PLANNER_ACP_MODE") == "session_error":
                        write({{"jsonrpc": "2.0", "id": request["id"], "error": {{
                            "code": -32603,
                            "message": "token=remote-secret",
                            "data": {{
                                "service": "session",
                                "errorName": "provider.invalid-request",
                                "authorization": "remote-secret",
                                "details": {{"token": "remote-secret"}},
                            }},
                        }}}})
                    else:
                        write({{"jsonrpc": "2.0", "id": request["id"], "result": {{"sessionId": SESSION_ID}}}})
                elif method == "session/prompt":
                    seen_prompt_count += 1
                    capture = os.environ.get("SIDERA_TEST_PLANNER_PROMPT_CAPTURE")
                    if capture:
                        with open(capture, "a", encoding="utf-8") as stream:
                            stream.write("prompt\\n")
                    mode = os.environ.get("SIDERA_TEST_PLANNER_ACP_MODE")
                    if mode == "prompt_timeout" or (mode == "repair_timeout" and seen_prompt_count > 1) or {hang_prompt}:
                        time.sleep(30)
                    elif mode in {"prompt_error", "prompt_error_untrusted"} or (mode == "repair_error" and seen_prompt_count > 1):
                        write({{"jsonrpc": "2.0", "id": request["id"], "error": {{
                            "code": -32603,
                            "message": "token=remote-secret",
                            "data": {{
                                "service": "session" if mode != "prompt_error_untrusted" else "unknown-service",
                                "errorName": "provider.invalid-request" if mode != "prompt_error_untrusted" else "remote-secret",
                                "authorization": "remote-secret",
                                "details": {{"token": "remote-secret"}},
                            }},
                        }}}})
                    else:
                        send_response(request)
                elif method == "session/cancel":
                    pass
            """
        ).strip()
        + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _planner_input() -> tuple[PlannerInput, PlanningProfileSnapshot]:
    definition, instance = module_facts()
    assert isinstance(definition.capabilities, PlannerAdapterCapabilityProfile)
    snapshot = profile(instance.id).snapshot(definition.capabilities)
    request_id = uuid4()
    run_id = uuid4()
    brief = PlanningBriefRevision.create(
        planning_request_id=request_id,
        revision=1,
        objective="Build a planner adapter",
        constraints=("Use strict JSON",),
        acceptance_criteria=("The proposal is valid",),
        context_refs=(),
        requested_outputs=("proposal",),
        user_feedback=None,
        source_correlation=uuid4(),
        created_by="test",
        created_at=NOW,
    )
    planner_input = PlannerInput(
        interface_version=snapshot.planner_interface_version,
        proposal_schema_version=snapshot.proposal_schema_version,
        operation_id=uuid4(),
        planning_request_id=request_id,
        planning_run_id=run_id,
        brief_revision=brief.revision,
        brief=brief,
        profile_snapshot=snapshot,
        capability_catalog=CapabilityCatalogSnapshot(
            snapshot_version=1,
            captured_at=NOW,
            capabilities=(),
            adapter_preferences=snapshot.adapter_preferences,
            budget_level=snapshot.execution_budget_level,
            max_concurrency=snapshot.budget.max_concurrency,
            workspace_providers=(),
        ),
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
        context_refs=tuple(ContextReference(ref=ref, safe_summary=None) for ref in ()),
    )
    return planner_input, snapshot


def _revision_planner_input() -> tuple[PlannerInput, PlanningProfileSnapshot]:
    from tests.unit.test_plan_revisions import profile_snapshot
    from tests.unit.test_revision_planning_context import _build_context

    context = _build_context()
    request_id = uuid4()
    brief = PlanningBriefRevision.create(
        planning_request_id=request_id,
        revision=1,
        objective="Revise the running plan",
        constraints=("Keep completed work auditable",),
        acceptance_criteria=("The revision is reviewable",),
        context_refs=(),
        requested_outputs=("revision plan",),
        user_feedback=None,
        source_correlation=uuid4(),
        created_by="test",
        created_at=NOW,
    )
    snapshot = replace(
        profile_snapshot(),
        allowed_capabilities=("code", "test"),
    )
    planner_input = PlannerInputAssembler().assemble(
        brief=brief,
        profile_snapshot=snapshot,
        capability_catalog=CapabilityCatalogSnapshot(
            snapshot_version=1,
            captured_at=NOW,
            capabilities=(),
            adapter_preferences=snapshot.adapter_preferences,
            budget_level=snapshot.execution_budget_level,
            max_concurrency=snapshot.budget.max_concurrency,
            workspace_providers=(),
        ),
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
        operation_id=uuid4(),
        planning_request_id=request_id,
        planning_run_id=uuid4(),
        planning_context=context,
        kind=PlanningRequestKind.REVISION,
    )
    return planner_input, snapshot


def _response(
    result_type: str, body: object, *, usage: dict[str, int] | None = None
) -> str:
    return json.dumps(
        {
            "result_type": result_type,
            "proposal" if result_type == "proposal" else "clarifications": body,
            "usage": usage
            or {
                "input_tokens": 10,
                "output_tokens": 20,
                "cost_microunits": 3,
                "runtime_seconds": 4,
                "context_bytes": 100,
            },
        }
    )


def _disabled_collaboration_policy() -> dict[str, object]:
    return {
        "mode": "disabled",
        "default_policy": {"strategy_id": "all_required"},
        "subtask_overrides": [],
    }


def _v4_snapshot(snapshot: PlanningProfileSnapshot) -> PlanningProfileSnapshot:
    assert isinstance(snapshot.planner_capabilities, PlannerAdapterCapabilityProfile)
    return replace(
        snapshot,
        proposal_schema_version=4,
        planner_capabilities=replace(
            snapshot.planner_capabilities,
            proposal_schema_versions=(2, 3, 4),
        ),
    )


def test_opencode_parser_dispatches_initial_and_revision_v4() -> None:
    initial_input, initial_snapshot = _planner_input()
    initial_snapshot = _v4_snapshot(initial_snapshot)
    initial_input = replace(
        initial_input,
        proposal_schema_version=4,
        profile_snapshot=initial_snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(
            initial_snapshot
        ),
    )
    initial_payload = _proposal_payload().to_mapping()
    initial_payload.pop("base_descriptor")
    initial_payload["schema_version"] = 4
    initial_payload["collaboration_policy_request"] = _disabled_collaboration_policy()

    initial_result = _parse_result(
        _response("proposal", initial_payload),
        initial_input,
        "test-opencode",
    )

    revision_input, revision_snapshot = _revision_planner_input()
    revision_snapshot = _v4_snapshot(revision_snapshot)
    revision_input = replace(
        revision_input,
        proposal_schema_version=4,
        profile_snapshot=revision_snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(
            revision_snapshot
        ),
    )
    fake_result = FakePlannerAdapter(FakePlannerConfig(scenario="revision")).plan(
        revision_input,
        _run_control(revision_input.operation_id),
    )
    assert isinstance(fake_result, PlannerProposalResult)
    revision_payload = fake_result.proposal.payload.to_mapping()
    revision_payload["schema_version"] = 4
    revision_payload["collaboration_policy_request"] = _disabled_collaboration_policy()

    revision_result = _parse_result(
        _response("proposal", revision_payload),
        revision_input,
        "test-opencode",
    )

    assert isinstance(initial_result, PlannerProposalResult)
    assert isinstance(initial_result.proposal.payload, PlanningProposalPayloadV4)
    assert isinstance(revision_result, PlannerProposalResult)
    assert isinstance(
        revision_result.proposal.payload, PlanningRevisionProposalPayloadV4
    )


def _adapter(
    tmp_path: Path,
    *,
    request_timeout_seconds: float = 5,
    acceptance_guard=None,
    hang_prompt: bool = False,
) -> OpenCodePlannerAdapter:
    return OpenCodePlannerAdapter(
        executable=str(_fake_opencode(tmp_path, hang_prompt=hang_prompt)),
        temp_root=tmp_path / "planner-runs",
        request_timeout_seconds=request_timeout_seconds,
        stop_grace_seconds=0.2,
        acceptance_guard=acceptance_guard,
    )


def _observe_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    observations: list[tuple[str, str]] = []

    def begin_call(_guard, **kwargs):
        return str(kwargs["operation_id"])

    def observe_call(_guard, operation_id, *, outcome, resource_ids=()):
        observations.append((operation_id, outcome))

    monkeypatch.setattr(opencode_planner, "begin_call", begin_call)
    monkeypatch.setattr(opencode_planner, "observe_call", observe_call)
    return observations


def test_planner_reports_prompt_json_rpc_error_as_unknown_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    planner_input, _ = _planner_input()
    capture = tmp_path / "prompt-calls.txt"
    monkeypatch.setenv("SIDERA_TEST_PLANNER_ACP_MODE", "prompt_error")
    monkeypatch.setenv("SIDERA_TEST_PLANNER_PROMPT_CAPTURE", str(capture))
    observations = _observe_calls(monkeypatch)

    result = _adapter(tmp_path, acceptance_guard=object()).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerUnknownOutcome)
    assert result.code == "unknown_outcome"
    assert result.retryable is False
    assert result.unknown_outcome is True
    assert capture.read_text(encoding="utf-8").splitlines() == ["prompt"]
    assert observations == [(f"{planner_input.operation_id}:prompt:0", "unknown")]
    prompt_errors = [
        record
        for record in caplog.records
        if record.getMessage().startswith("OpenCode planner prompt outcome is unknown:")
    ]
    assert len(prompt_errors) == 1
    assert prompt_errors[0].acp_method == "session/prompt"
    assert prompt_errors[0].acp_error_code == -32603
    assert prompt_errors[0].acp_service == "session"
    assert prompt_errors[0].acp_error_name == "provider.invalid-request"
    assert "service=session" in prompt_errors[0].getMessage()
    assert "error_name=provider.invalid-request" in prompt_errors[0].getMessage()
    assert "timeout_seconds=None" in prompt_errors[0].getMessage()
    assert "remote-secret" not in repr(prompt_errors[0].__dict__)
    assert "remote-secret" not in caplog.text


def _allow_synthetic_handshake(monkeypatch):
    original = ACPProcessTransport.request

    def request(self, method, params, *, timeout):
        if method in {"initialize", "session/new"}:
            timeout = max(timeout, 5.0)
        return original(self, method, params, timeout=timeout)

    monkeypatch.setattr(ACPProcessTransport, "request", request)


def test_planner_reports_prompt_timeout_as_unknown_without_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _allow_synthetic_handshake(monkeypatch)
    planner_input, _ = _planner_input()
    capture = tmp_path / "prompt-calls.txt"
    monkeypatch.setenv("SIDERA_TEST_PLANNER_ACP_MODE", "prompt_timeout")
    monkeypatch.setenv("SIDERA_TEST_PLANNER_PROMPT_CAPTURE", str(capture))

    result = _adapter(tmp_path, request_timeout_seconds=0.5).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerUnknownOutcome)
    assert capture.read_text(encoding="utf-8").splitlines() == ["prompt"]
    prompt_error = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("OpenCode planner prompt outcome is unknown:")
    )
    assert prompt_error.acp_method == "session/prompt"
    assert prompt_error.acp_timeout_seconds == 0.5
    assert prompt_error.acp_elapsed_ms >= 400
    assert prompt_error.acp_process_exited is False
    assert prompt_error.acp_service is None
    assert prompt_error.acp_error_name is None
    assert (
        f"timeout_seconds={prompt_error.acp_timeout_seconds}"
        in prompt_error.getMessage()
    )
    assert f"elapsed_ms={prompt_error.acp_elapsed_ms}" in prompt_error.getMessage()
    assert "process_exited=False" in prompt_error.getMessage()


def test_planner_reports_repair_prompt_timeout_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _allow_synthetic_handshake(monkeypatch)
    planner_input, _ = _planner_input()
    capture = tmp_path / "prompt-calls.txt"
    monkeypatch.setenv("SIDERA_TEST_PLANNER_ACP_MODE", "repair_timeout")
    monkeypatch.setenv("SIDERA_TEST_PLANNER_PROMPT_CAPTURE", str(capture))
    monkeypatch.setenv("SIDERA_TEST_PLANNER_RESPONSE", '{"result_type":"proposal"}')
    observations = _observe_calls(monkeypatch)

    result = _adapter(
        tmp_path,
        request_timeout_seconds=0.5,
        acceptance_guard=object(),
    ).plan(planner_input, _run_control(planner_input.operation_id))

    assert isinstance(result, PlannerUnknownOutcome)
    assert capture.read_text(encoding="utf-8").splitlines() == ["prompt", "prompt"]
    assert observations == [
        (f"{planner_input.operation_id}:prompt:0", "completed"),
        (f"{planner_input.operation_id}:repair:1", "unknown"),
    ]
    repair_error = next(
        record
        for record in caplog.records
        if record.getMessage().startswith(
            "OpenCode planner repair prompt outcome is unknown:"
        )
    )
    assert repair_error.acp_method == "session/prompt"
    assert repair_error.acp_timeout_seconds == 0.5
    assert repair_error.acp_elapsed_ms >= 400
    assert repair_error.acp_process_exited is False
    assert (
        f"timeout_seconds={repair_error.acp_timeout_seconds}"
        in repair_error.getMessage()
    )
    assert f"elapsed_ms={repair_error.acp_elapsed_ms}" in repair_error.getMessage()
    assert "process_exited=False" in repair_error.getMessage()
    assert "remote-secret" not in repr(repair_error.__dict__)
    assert "remote-secret" not in caplog.text


def test_planner_drops_unrecognized_remote_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    planner_input, _ = _planner_input()
    capture = tmp_path / "prompt-calls.txt"
    monkeypatch.setenv("SIDERA_TEST_PLANNER_ACP_MODE", "prompt_error_untrusted")
    monkeypatch.setenv("SIDERA_TEST_PLANNER_PROMPT_CAPTURE", str(capture))

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerUnknownOutcome)
    assert capture.read_text(encoding="utf-8").splitlines() == ["prompt"]
    prompt_error = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("OpenCode planner prompt outcome is unknown:")
    )
    assert prompt_error.acp_service is None
    assert prompt_error.acp_error_name is None
    assert "service=None" in prompt_error.getMessage()
    assert "error_name=None" in prompt_error.getMessage()
    assert "remote-secret" not in repr(prompt_error.__dict__)
    assert "remote-secret" not in caplog.text


def test_planner_marks_pre_prompt_session_failure_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner_input, _ = _planner_input()
    capture = tmp_path / "prompt-calls.txt"
    monkeypatch.setenv("SIDERA_TEST_PLANNER_ACP_MODE", "session_error")
    monkeypatch.setenv("SIDERA_TEST_PLANNER_PROMPT_CAPTURE", str(capture))
    observations = _observe_calls(monkeypatch)

    with pytest.raises(OpenCodePlannerError):
        _adapter(tmp_path, acceptance_guard=object()).plan(
            planner_input,
            _run_control(planner_input.operation_id),
        )

    assert not capture.exists()
    assert observations == [(f"{planner_input.operation_id}:prompt:0", "failed")]


def test_planner_marks_only_failed_repair_prompt_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    planner_input, _ = _planner_input()
    capture = tmp_path / "prompt-calls.txt"
    monkeypatch.setenv("SIDERA_TEST_PLANNER_ACP_MODE", "repair_error")
    monkeypatch.setenv("SIDERA_TEST_PLANNER_PROMPT_CAPTURE", str(capture))
    monkeypatch.setenv("SIDERA_TEST_PLANNER_RESPONSE", '{"result_type":"proposal"}')
    observations = _observe_calls(monkeypatch)

    result = _adapter(tmp_path, acceptance_guard=object()).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerUnknownOutcome)
    assert capture.read_text(encoding="utf-8").splitlines() == ["prompt", "prompt"]
    assert observations == [
        (f"{planner_input.operation_id}:prompt:0", "completed"),
        (f"{planner_input.operation_id}:repair:1", "unknown"),
    ]
    repair_errors = [
        record
        for record in caplog.records
        if record.getMessage().startswith(
            "OpenCode planner repair prompt outcome is unknown:"
        )
    ]
    assert len(repair_errors) == 1
    assert repair_errors[0].acp_method == "session/prompt"
    assert repair_errors[0].acp_error_code == -32603
    assert repair_errors[0].acp_service == "session"
    assert repair_errors[0].acp_error_name == "provider.invalid-request"
    assert "service=session" in repair_errors[0].getMessage()
    assert "error_name=provider.invalid-request" in repair_errors[0].getMessage()
    assert "remote-secret" not in repr(repair_errors[0].__dict__)
    assert "remote-secret" not in caplog.text


def test_planner_guard_rejection_is_explicit_and_does_not_spawn_acp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner_input, _ = _planner_input()
    started = tmp_path / "acp-started.txt"
    monkeypatch.setenv("SIDERA_TEST_PLANNER_STARTED_FILE", str(started))

    def reject_call(_guard, **_kwargs):
        raise RuntimeGuardError("budget_exhausted")

    monkeypatch.setattr(opencode_planner, "begin_call", reject_call)
    result = _adapter(tmp_path, acceptance_guard=object()).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerExplicitFailure)
    assert result.code == "acceptance_guard:budget_exhausted"
    assert result.retryable is False
    assert result.unknown_outcome is False
    assert not started.exists()


def test_repair_guard_rejection_does_not_dispatch_another_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner_input, _ = _planner_input()
    capture = tmp_path / "prompt-calls.txt"
    monkeypatch.setenv("SIDERA_TEST_PLANNER_PROMPT_CAPTURE", str(capture))
    monkeypatch.setenv("SIDERA_TEST_PLANNER_RESPONSE", '{"result_type":"proposal"}')
    observations = _observe_calls(monkeypatch)
    begin_count = 0

    def begin_then_reject(_guard, **kwargs):
        nonlocal begin_count
        begin_count += 1
        if begin_count == 1:
            return str(kwargs["operation_id"])
        raise RuntimeGuardError("budget_exhausted")

    monkeypatch.setattr(opencode_planner, "begin_call", begin_then_reject)
    result = _adapter(tmp_path, acceptance_guard=object()).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerExplicitFailure)
    assert result.code == "acceptance_guard:budget_exhausted"
    assert result.retryable is False
    assert result.unknown_outcome is False
    assert capture.read_text(encoding="utf-8").splitlines() == ["prompt"]
    assert observations == [(f"{planner_input.operation_id}:prompt:0", "completed")]


def test_planner_probe_declares_json_text_not_json_schema(tmp_path: Path) -> None:
    profile = _adapter(tmp_path).probe({"cwd": str(tmp_path)})

    assert profile.structured_output is StructuredOutputCapability.JSON
    assert profile.idempotency is IdempotencyCapability.NONE
    assert profile.cancellation is CancellationCapability.PROCESS
    assert profile.usage_reporting is UsageReportingCapability.TOKENS
    assert profile.proposal_schema_versions == (2, 3, 4, 5, 6)
    assert profile.context_package_versions == (1,)


def test_safe_planner_diagnostic_redacts_provider_credentials() -> None:
    diagnostic = _safe_diagnostic(
        "request failed api_key=sk-provider-secret-value "
        "Authorization: Bearer sk-another-secret-value"
    )

    assert "sk-provider-secret-value" not in diagnostic
    assert "sk-another-secret-value" not in diagnostic
    assert "[redacted]" in diagnostic


def test_opencode_parser_dispatches_initial_v5() -> None:
    from .result_input_fixtures import proposal_v5_mapping
    from .test_planning_proposals_v5 import v5_profile_snapshot

    planner_input, _ = _planner_input()
    snapshot = v5_profile_snapshot()
    planner_input = replace(
        planner_input,
        proposal_schema_version=5,
        profile_snapshot=snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
    )

    result = _parse_result(
        _response("proposal", proposal_v5_mapping()),
        planner_input,
        "test-opencode",
    )

    assert isinstance(result, PlannerProposalResult)
    assert isinstance(result.proposal.payload, PlanningProposalPayloadV5)


def test_v5_planner_prompt_requires_input_group_policy_shape() -> None:
    from .test_planning_proposals_v5 import v5_profile_snapshot

    planner_input, _ = _planner_input()
    snapshot = v5_profile_snapshot()
    planner_input = replace(
        planner_input,
        proposal_schema_version=5,
        profile_snapshot=snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
    )

    prompt = _planner_prompt(planner_input)

    assert "Schema v5 requires a complete collaboration_policy_request" in prompt
    assert "input_groups" in prompt
    assert "ordered candidate WorkerItems" in prompt


def test_planner_prompt_binds_proposal_shape_and_worker_coverage() -> None:
    planner_input, _ = _planner_input()

    prompt = _planner_prompt(planner_input)

    assert (
        "clarifications: [{question, rationale, answer_type, options, blocking}]"
        in prompt
    )
    assert (
        "Every subtask with expected outputs must have a corresponding WorkerItem"
        in prompt
    )
    assert "Do not invent extra verification-only subtasks or WorkerItems" in prompt
    assert "impact_policy.manual_gate to false" in prompt
    assert "artifact_type:diff, artifact_type:log, artifact_type:report" in prompt
    assert (
        "Every acceptance_refs and required_evidence_refs entry must use the "
        "acceptance:<criterion-ref> format"
    ) in prompt
    assert (
        "Every execution_intents required_evidence_refs must be a non-empty JSON "
        "array of strings"
    ) in prompt
    assert "Never use acceptance_criterion: values" in prompt
    assert "Dependency kind values are exactly hard or informational" in prompt
    assert "Never use depends_on" in prompt
    assert "WorkerItem dependency edges must stay within one Subtask" in prompt
    assert "cross-Subtask ordering must use subtask_dependencies" in prompt
    assert "parallel source WorkerItems under one implementation Subtask" in prompt
    assert "activation_dependencies entries must be objects" in prompt
    assert (
        "upstream_worker_item_key, downstream_worker_item_key, kind, and required_artifact_refs"
        in prompt
    )
    assert "Every execution_intents budget_class must be a string" in prompt
    assert "never put them at the proposal root" in prompt
    assert "The proposal object root has only these fields" in prompt
    assert "required_evidence_refs, expected_artifact_refs" in prompt


def test_planner_prompt_spells_out_worker_dependency_shape() -> None:
    planner_input, _ = _planner_input()

    prompt = _planner_prompt(planner_input)

    assert (
        "Each subtask_dependencies item must use exactly upstream_subtask_key, "
        "downstream_subtask_key, kind, required_evidence_refs, and enforcement"
    ) in prompt
    assert (
        "never include required_artifact_refs in a subtask_dependencies item"
    ) in prompt
    assert (
        "Each worker_item_dependencies item must use exactly "
        "upstream_worker_item_key, downstream_worker_item_key, kind, and "
        "required_artifact_refs"
    ) in prompt
    assert (
        "never include required_evidence_refs in a worker_item_dependencies item"
    ) in prompt
    assert (
        "Subtask dependencies carry required acceptance evidence; WorkerItem "
        "dependencies carry required artifacts"
    ) in prompt
    assert (
        "worker_item_dependencies item must use exactly "
        "upstream_worker_item_key, downstream_worker_item_key, kind, and "
        "required_artifact_refs"
    ) in prompt
    assert (
        "Never use worker_item_key, depends_on_worker_item_key, or enforcement"
        in prompt
    )
    assert "Cross-Subtask result inputs must be represented by input_groups" in prompt
    assert (
        "never duplicate an input_groups relationship as a worker dependency" in prompt
    )


def test_v4_planner_prompt_exposes_policy_vocabulary_without_runtime_bindings() -> None:
    planner_input, snapshot = _planner_input()
    runtime_definition_id = uuid4()
    runtime_instance_id = uuid4()
    binding = CollaborationRuntimeBindingSnapshot(
        runtime_definition_id=runtime_definition_id,
        runtime_instance_id=runtime_instance_id,
        runtime_interface_version="collaboration-runtime-v1",
        runtime_config_version=3,
        runtime_capability_snapshot=CollaborationRuntimeCapabilityProfile(
            interface_version="collaboration-runtime-v1",
            projection_schema_versions=(1,),
            decision_schema_versions=(1,),
            strategy_ids=("all_required",),
            idempotency=IdempotencyCapability.OPERATION_ID,
            max_projection_bytes=262_144,
            max_decision_bytes=65_536,
            max_intents_per_decision=128,
            max_strategy_state_bytes=16_384,
            stability=StabilityLevel.EXPERIMENTAL,
        ),
    )
    snapshot = replace(
        _v4_snapshot(snapshot),
        collaboration_runtime_binding=binding,
    )
    planner_input = replace(
        planner_input,
        proposal_schema_version=4,
        profile_snapshot=snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
    )

    prompt = _planner_prompt(planner_input)

    assert "collaboration_policy_request" in prompt
    assert '"modes":["disabled","enabled"]' in prompt
    assert '"strategy_ids":["all_required"]' in prompt
    assert str(runtime_definition_id) not in prompt
    assert str(runtime_instance_id) not in prompt
    assert "collaboration-runtime-v1" not in prompt
    assert "runtime_config_version" not in prompt


def test_revision_planner_prompt_binds_complete_target_plan_and_exact_identity() -> (
    None
):
    planner_input, _ = _revision_planner_input()

    prompt = _planner_prompt(planner_input)

    assert '"schema_version":3' in prompt
    assert '"kind":"revision"' in prompt
    assert '"revision_context"' in prompt
    assert "complete target plan" in prompt
    assert "exact base_descriptor" in prompt
    assert (
        "every existing subtask and WorkerItem identity decision exactly once" in prompt
    )
    assert "complete subtask and WorkerItem DAG" in prompt
    assert "full replacement plan, not a patch fragment" in prompt
    assert "identity decisions must cover every target node exactly once" in prompt
    assert "context_disposition is exactly unchanged or patch_required" in prompt
    assert "Every revision execution_intents item must use arrays" in prompt
    assert 'required_evidence_refs: ["acceptance:<criterion-ref>"]' in prompt
    assert "required_artifact_refs: []" in prompt


def test_planner_opencode_config_denies_tools_and_external_directories() -> None:
    assert _planner_opencode_config("opencode-go/mimo-v2.5") == {
        "$schema": "https://opencode.ai/config.json",
        "disabled_providers": ["deepseek"],
        "enabled_providers": ["opencode-go"],
        "model": "opencode-go/mimo-v2.5",
        "permission": {"*": "deny", "external_directory": "deny"},
    }
    assert "model" not in _planner_opencode_config(None)


def test_repair_prompt_carries_safe_validation_diagnostic() -> None:
    prompt = _repair_prompt(
        "proposal_reference_invalid: evidence reference must use acceptance:<criterion-ref>"
    )

    assert (
        "Each subtask_dependencies item must use exactly upstream_subtask_key, "
        "downstream_subtask_key, kind, required_evidence_refs, and enforcement"
    ) in prompt
    assert (
        "never include required_artifact_refs in a subtask_dependencies item"
    ) in prompt
    assert (
        "Each worker_item_dependencies item must use exactly "
        "upstream_worker_item_key, downstream_worker_item_key, kind, and "
        "required_artifact_refs"
    ) in prompt
    assert (
        "never include required_evidence_refs in a worker_item_dependencies item"
    ) in prompt
    assert (
        "Subtask dependencies carry required acceptance evidence; WorkerItem "
        "dependencies carry required artifacts"
    ) in prompt
    assert (
        "Every acceptance_refs and required_evidence_refs value must use the "
        "acceptance:<criterion-ref> format"
    ) in prompt
    assert "never acceptance_criterion:0001" in prompt
    assert "The validator reported this safe diagnostic" in prompt
    assert "proposal_reference_invalid" in prompt
    assert "Dependency kind values are exactly hard or informational" in prompt
    assert "never use depends_on" in prompt
    assert (
        "execution_intents required_evidence_refs must reuse at least one exact value"
    ) in prompt
    assert "Never use a string for any *_refs field" in prompt
    assert "required_artifact_refs must always be a JSON array" in prompt
    assert "budget_class must be a string" in prompt
    assert "never put them at the proposal root" in prompt
    assert "context_disposition is exactly unchanged or patch_required" in prompt


def test_repair_prompt_carries_supported_collaboration_strategy_ids() -> None:
    prompt = _repair_prompt(
        "proposal_collaboration_strategy_unsupported: all_required",
        collaboration_strategy_ids=("input_groups_v1",),
    )

    assert (
        "The only supported collaboration strategy IDs for this run are "
        '["input_groups_v1"]'
    ) in prompt
    assert "never invent all_required or another ID" in prompt


def test_planner_uses_latest_acp_agent_message_when_provider_retries_output() -> None:
    valid = _response(
        "clarification",
        [
            {
                "question": "What should be debugged?",
                "rationale": "The request is underspecified.",
                "answer_type": "string",
                "options": [],
                "blocking": True,
            }
        ],
    )
    notifications = (
        {
            "method": "session/update",
            "params": {
                "sessionId": "session",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "old-message",
                    "content": {"type": "text", "text": '{"result_type":"proposal"}'},
                },
            },
        },
        {
            "method": "session/update",
            "params": {
                "sessionId": "session",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "new-message",
                    "content": {"type": "text", "text": valid},
                },
            },
        },
    )

    assert (
        _response_text(notifications, session_id="session", prompt_result={}) == valid
    )


def test_planner_parses_proposal_and_usage_and_cleans_temp_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _planner_input()
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", _proposal_payload().to_mapping()),
    )
    adapter = _adapter(tmp_path)

    result = adapter.plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)
    assert result.proposal.payload.schema_version == 2
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20
    assert adapter.provider_ref == "opencode-acp-planner"
    assert not any((tmp_path / "planner-runs").iterdir())


def test_planner_parses_revision_proposal_against_exact_context_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _revision_planner_input()
    fake_result = FakePlannerAdapter(FakePlannerConfig(scenario="revision")).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )
    assert isinstance(fake_result, PlannerProposalResult)
    assert isinstance(fake_result.proposal.payload, PlanningRevisionProposalPayloadV3)
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", fake_result.proposal.payload.to_mapping()),
    )

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)
    assert isinstance(result.proposal.payload, PlanningRevisionProposalPayloadV3)
    assert result.proposal.payload.schema_version == 3
    assert result.proposal.derived_diff is not None


def test_planner_normalizes_scalar_revision_reference_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _revision_planner_input()
    fake_result = FakePlannerAdapter(FakePlannerConfig(scenario="revision")).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )
    assert isinstance(fake_result, PlannerProposalResult)
    payload = fake_result.proposal.payload.to_mapping()
    intents = payload["execution_intents"]
    assert isinstance(intents, list)
    intent = intents[0]
    assert isinstance(intent, dict)
    intent["required_evidence_refs"] = "acceptance:implementation"

    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", payload),
    )

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)
    assert result.proposal.payload.execution_intents[0].required_evidence_refs == (
        "acceptance:implementation",
    )


def test_planner_normalizes_null_subtask_dependency_reference_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _revision_planner_input()
    fake_result = FakePlannerAdapter(FakePlannerConfig(scenario="revision")).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )
    assert isinstance(fake_result, PlannerProposalResult)
    payload = fake_result.proposal.payload.to_mapping()
    dependencies = payload["subtask_dependencies"]
    assert isinstance(dependencies, list)
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    dependency["required_evidence_refs"] = None

    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", payload),
    )

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)
    assert result.proposal.payload.subtask_dependencies[0].required_evidence_refs == ()


def test_planner_defaults_missing_v6_subtask_dependency_reference_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .result_integration_fixtures import proposal_v6_mapping
    from .test_result_plan_versions import v6_profile_snapshot

    planner_input, _ = _planner_input()
    snapshot = v6_profile_snapshot()
    planner_input = replace(
        planner_input,
        proposal_schema_version=6,
        profile_snapshot=snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
    )
    payload = proposal_v6_mapping()
    payload["subtask_dependencies"] = [
        {
            "upstream_subtask_key": "build",
            "downstream_subtask_key": "review",
            "kind": "hard",
            "enforcement": "control_plane",
        }
    ]

    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", payload),
    )

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)
    assert result.proposal.payload.subtask_dependencies[0].required_evidence_refs == ()


def test_planner_canonicalizes_v6_result_integration_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .result_integration_fixtures import proposal_v6_mapping
    from .test_result_plan_versions import v6_profile_snapshot

    planner_input, snapshot = _planner_input()
    configured_adapter = uuid4()
    configured_workspace = uuid4()
    snapshot = replace(
        v6_profile_snapshot(),
        system_policy={
            "result_integration": {
                "repository_ref": "git-repository://configured",
                "base_commit": "b" * 40,
                "verification_environment_ref": "env://interview-demo",
                "verification_binding": {
                    "adapter_module_ref": str(configured_adapter),
                    "adapter_config_version": 2,
                    "workspace_module_ref": str(configured_workspace),
                    "workspace_config_version": 3,
                },
            }
        },
    )
    planner_input = replace(
        planner_input,
        proposal_schema_version=6,
        profile_snapshot=snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
    )
    payload = proposal_v6_mapping()
    payload["integration_spec"]["repository_ref"] = "model-invented-repository"  # type: ignore[index]
    payload["integration_spec"]["base_commit"] = "c" * 40  # type: ignore[index]
    payload["integration_spec"]["verification_binding"] = {  # type: ignore[index]
        "adapter_module_ref": str(uuid4()),
        "adapter_config_version": 99,
        "workspace_module_ref": str(uuid4()),
        "workspace_config_version": 99,
    }
    payload["integration_spec"]["checks"][0]["environment_ref"] = None  # type: ignore[index]

    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", payload),
    )

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)
    integration = result.proposal.payload.integration_spec
    assert integration.repository_ref == "git-repository://configured"
    assert integration.base_commit == "b" * 40
    assert integration.checks[0].environment_ref == "env://interview-demo"
    assert integration.verification_binding.adapter_module_ref == configured_adapter
    assert integration.verification_binding.adapter_config_version == 2
    assert integration.verification_binding.workspace_module_ref == configured_workspace
    assert integration.verification_binding.workspace_config_version == 3


def test_planner_canonicalizes_v6_verification_checks_from_system_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .result_integration_fixtures import proposal_v6_mapping
    from .test_result_plan_versions import v6_profile_snapshot

    configured_check = {
        "key": "unit",
        "version": 1,
        "argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
        "cwd": ".",
        "environment_ref": "env://interview-demo",
        "prepare_argvs": [],
        "timeout_seconds": 60,
        "success_exit_codes": [0],
        "report_paths": [],
        "required": True,
    }
    planner_input, snapshot = _planner_input()
    snapshot = replace(
        v6_profile_snapshot(),
        system_policy={
            "result_integration": {
                "repository_ref": "git-repository://configured",
                "base_commit": "b" * 40,
                "verification_environment_ref": "env://interview-demo",
                "verification_check_specs": [configured_check],
                "verification_binding": {
                    "adapter_module_ref": str(uuid4()),
                    "adapter_config_version": 1,
                    "workspace_module_ref": str(uuid4()),
                    "workspace_config_version": 1,
                },
            }
        },
    )
    planner_input = replace(
        planner_input,
        proposal_schema_version=6,
        profile_snapshot=snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
    )
    payload = proposal_v6_mapping()
    payload["integration_spec"]["checks"][0]["argv"] = [  # type: ignore[index]
        "python3",
        "-m",
        "unittest",
    ]

    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", payload),
    )

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)
    integration = result.proposal.payload.integration_spec
    assert integration.checks[0].to_mapping() == configured_check


def test_v6_planner_prompt_exposes_system_owned_result_integration_environment() -> (
    None
):
    planner_input, _ = _planner_input()
    from .test_result_plan_versions import v6_profile_snapshot

    snapshot = v6_profile_snapshot()
    snapshot = replace(
        snapshot,
        proposal_schema_version=6,
        planner_capabilities=replace(
            snapshot.planner_capabilities,
            proposal_schema_versions=(6,),
        ),
        system_policy={
            "result_integration": {
                "repository_ref": "git-repository://configured",
                "base_commit": "b" * 40,
                "verification_environment_ref": "env://interview-demo",
                "verification_binding": {
                    "adapter_module_ref": str(uuid4()),
                    "adapter_config_version": 1,
                    "workspace_module_ref": str(uuid4()),
                    "workspace_config_version": 1,
                },
            }
        },
    )
    planner_input = replace(
        planner_input,
        proposal_schema_version=6,
        profile_snapshot=snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
    )

    prompt = _planner_prompt(planner_input)

    assert '"result_integration_environment"' in prompt
    assert (
        "SIDERA canonicalizes these system-owned fields before confirmation" in prompt
    )
    assert "exactly one check per integration spec" in prompt
    assert "verification environment" in prompt


def test_v6_planner_prompt_uses_runtime_supported_collaboration_strategy() -> None:
    planner_input, _ = _planner_input()
    from .test_result_plan_versions import v6_profile_snapshot

    v6_snapshot = v6_profile_snapshot()
    binding = v6_snapshot.collaboration_runtime_binding
    assert binding is not None
    v6_snapshot = replace(
        v6_snapshot,
        collaboration_runtime_binding=replace(
            binding,
            runtime_capability_snapshot=replace(
                binding.runtime_capability_snapshot,
                strategy_ids=("input_groups_v1", "all_required"),
            ),
        ),
    )
    planner_input = replace(
        planner_input,
        proposal_schema_version=6,
        profile_snapshot=v6_snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(v6_snapshot),
    )

    prompt = _planner_prompt(planner_input)

    assert '"strategy_ids":["all_required"]' in prompt
    assert '"default_policy":{"strategy_id":"all_required"}' in prompt
    assert "Never emit all_required or any other strategy ID" in prompt


@pytest.mark.parametrize("is_revision", [False, True])
def test_v6_planner_prompt_maps_required_sources_to_input_group_cardinality(
    is_revision: bool,
) -> None:
    from .test_result_plan_versions import v6_profile_snapshot

    if is_revision:
        planner_input, _ = _revision_planner_input()
    else:
        planner_input, _ = _planner_input()
    snapshot = v6_profile_snapshot()
    snapshot = replace(
        snapshot,
        proposal_schema_version=6,
        planner_capabilities=replace(
            snapshot.planner_capabilities,
            proposal_schema_versions=(6,),
        ),
    )
    planner_input = replace(
        planner_input,
        proposal_schema_version=6,
        profile_snapshot=snapshot,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
    )

    prompt = _planner_prompt(planner_input)

    assert "Every explicitly required source must be selected" in prompt
    assert "minimum_count must equal the candidate count" in prompt
    assert (
        "Only use a smaller minimum_count when the brief permits alternatives" in prompt
    )


def test_v6_shape_hint_requires_enabled_result_input_groups() -> None:
    proposal = _proposal_shape_hint(6)["proposal"]
    assert isinstance(proposal, dict)
    policy = proposal["collaboration_policy_request"]
    assert isinstance(policy, dict)
    assert policy["mode"] == "enabled"
    assert policy["input_groups"]
    integration_spec = proposal["integration_spec"]
    assert isinstance(integration_spec, dict)
    assert integration_spec["input_group_keys"] == ["implementation-inputs"]


def test_planner_rejects_create_schema_for_revision_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _revision_planner_input()
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", _proposal_payload().to_mapping()),
    )

    with pytest.raises(OpenCodePlannerError, match="strict JSON"):
        _adapter(tmp_path).plan(
            planner_input,
            _run_control(planner_input.operation_id),
        )


def test_planner_parses_blocking_clarifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _planner_input()
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response(
            "clarification",
            [
                {
                    "question": "Which adapter should execute the plan?",
                    "rationale": "The request does not constrain the execution adapter.",
                    "answer_type": "choice",
                    "options": ["opencode", "fake"],
                    "blocking": True,
                }
            ],
        ),
    )

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerClarificationResult)
    assert result.clarifications[0].blocking is True
    assert result.clarifications[0].options == ("opencode", "fake")


def test_planner_normalizes_single_flat_clarification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _planner_input()
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        json.dumps(
            {
                "result_type": "clarification",
                "question": "What should be debugged?",
                "rationale": "The requested outcome is not specific enough.",
                "answer_type": "string",
                "options": [],
                "blocking": True,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cost_microunits": 3,
                    "runtime_seconds": 4,
                    "context_bytes": 100,
                },
            }
        ),
    )

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerClarificationResult)
    assert len(result.clarifications) == 1
    assert result.clarifications[0].question == "What should be debugged?"
    assert result.clarifications[0].blocking is True


def test_planner_rejects_explanations_and_markdown_around_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _planner_input()
    valid = _response("proposal", _proposal_payload().to_mapping())
    adapter = _adapter(tmp_path)

    for value in (f"```json\n{valid}\n```", f"Here is the result: {valid}"):
        monkeypatch.setenv("SIDERA_TEST_PLANNER_RESPONSE", value)
        with pytest.raises(OpenCodePlannerError, match="strict JSON"):
            adapter.plan(
                planner_input,
                _run_control(planner_input.operation_id),
            )


def test_planner_repairs_one_invalid_structured_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _planner_input()
    valid = _response("proposal", _proposal_payload().to_mapping())
    invalid = json.loads(valid)
    invalid["unexpected"] = True
    monkeypatch.setenv("SIDERA_TEST_PLANNER_RESPONSE", json.dumps(invalid))
    monkeypatch.setenv("SIDERA_TEST_PLANNER_REPAIR_RESPONSE", valid)

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)


def test_planner_repairs_semantically_invalid_create_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _planner_input()
    valid_payload = _proposal_payload().to_mapping()
    valid_payload["subtasks"].append(
        {
            **valid_payload["subtasks"][0],
            "key": "verification",
            "title": "Verify the request",
            "goal_summary": "Verify the requested change.",
        }
    )
    valid_payload["worker_items"].append(
        {
            **valid_payload["worker_items"][0],
            "key": "verification-worker",
            "subtask_key": "verification",
            "title": "Verification worker",
            "role": "qa",
            "responsibility": "Verify the requested change.",
        }
    )
    valid_payload["execution_intents"].append(
        {
            **valid_payload["execution_intents"][0],
            "worker_item_key": "verification-worker",
            "purpose": "Verify the requested change.",
        }
    )
    valid_payload["worker_item_dependencies"].append(
        {
            "upstream_worker_item_key": "implementation-worker",
            "downstream_worker_item_key": "verification-worker",
            "kind": "dependency",
            "required_artifact_refs": [],
        }
    )
    invalid = _response("proposal", valid_payload)
    valid = _response("proposal", _proposal_payload().to_mapping())
    monkeypatch.setenv("SIDERA_TEST_PLANNER_RESPONSE", invalid)
    monkeypatch.setenv("SIDERA_TEST_PLANNER_REPAIR_RESPONSE", valid)

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)
    assert result.proposal.payload.worker_item_dependencies == ()


def test_planner_retries_schema_repair_when_first_repair_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _planner_input()
    valid = _response("proposal", _proposal_payload().to_mapping())
    invalid = json.loads(valid)
    invalid["unexpected"] = True
    invalid_again = json.loads(valid)
    invalid_again["unexpected_again"] = True
    monkeypatch.setenv("SIDERA_TEST_PLANNER_RESPONSE", json.dumps(invalid))
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_REPAIR_RESPONSE_1", json.dumps(invalid_again)
    )
    monkeypatch.setenv("SIDERA_TEST_PLANNER_REPAIR_RESPONSE_2", valid)

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)


def test_planner_keeps_repairs_in_one_session_before_worker_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _planner_input()
    valid = _response("proposal", _proposal_payload().to_mapping())
    invalid = json.loads(valid)
    invalid["unexpected"] = True
    invalid_again = json.loads(valid)
    invalid_again["unexpected_again"] = True
    invalid_third = json.loads(valid)
    invalid_third["unexpected_third"] = True
    monkeypatch.setenv("SIDERA_TEST_PLANNER_RESPONSE", json.dumps(invalid))
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_REPAIR_RESPONSE_1", json.dumps(invalid_again)
    )
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_REPAIR_RESPONSE_2", json.dumps(invalid_third)
    )
    monkeypatch.setenv("SIDERA_TEST_PLANNER_REPAIR_RESPONSE_3", valid)

    result = _adapter(tmp_path).plan(
        planner_input,
        _run_control(planner_input.operation_id),
    )

    assert isinstance(result, PlannerProposalResult)


def test_planner_cancel_terminates_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner_input, _ = _planner_input()
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", _proposal_payload().to_mapping()),
    )
    adapter = _adapter(tmp_path, hang_prompt=True)
    result: list[BaseException] = []

    def run() -> None:
        try:
            adapter.plan(
                planner_input,
                _run_control(planner_input.operation_id),
            )
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert adapter.wait_until_active(planner_input.operation_id, timeout=1)
    adapter.cancel(planner_input.operation_id)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result and isinstance(result[0], OpenCodePlannerError)
    assert not adapter.active_processes()
