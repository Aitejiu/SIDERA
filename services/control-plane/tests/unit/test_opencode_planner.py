import json
import stat
import textwrap
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

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
    _repair_prompt,
    _response_text,
)
from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogSnapshot,
    ContextReference,
    PlannerClarificationResult,
    PlannerInput,
    PlannerInputAssembler,
    PlannerPolicyConstraints,
    PlannerProposalResult,
    PlannerRunControl,
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


def _fake_opencode(tmp_path: Path, *, hang_prompt: bool = False) -> Path:
    script = tmp_path / "fake-opencode-planner"
    prompt_branch = "time.sleep(30)" if hang_prompt else "send_response(request)"
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
                    write({{"jsonrpc": "2.0", "id": request["id"], "result": {{"sessionId": SESSION_ID}}}})
                elif method == "session/prompt":
                    {prompt_branch}
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


def _response(result_type: str, body: object, *, usage: dict[str, int] | None = None) -> str:
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
        PlannerRunControl(
            operation_id=revision_input.operation_id,
            started_at=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        ),
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
    assert isinstance(revision_result.proposal.payload, PlanningRevisionProposalPayloadV4)


def _adapter(tmp_path: Path, *, hang_prompt: bool = False) -> OpenCodePlannerAdapter:
    return OpenCodePlannerAdapter(
        executable=str(_fake_opencode(tmp_path, hang_prompt=hang_prompt)),
        temp_root=tmp_path / "planner-runs",
        request_timeout_seconds=5,
        stop_grace_seconds=0.2,
    )


def test_planner_probe_declares_json_text_not_json_schema(tmp_path: Path) -> None:
    profile = _adapter(tmp_path).probe({"cwd": str(tmp_path)})

    assert profile.structured_output is StructuredOutputCapability.JSON
    assert profile.idempotency is IdempotencyCapability.NONE
    assert profile.cancellation is CancellationCapability.PROCESS
    assert profile.usage_reporting is UsageReportingCapability.TOKENS
    assert profile.proposal_schema_versions == (2, 3, 4, 5, 6)
    assert profile.context_package_versions == (1,)


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

    assert "clarifications: [{question, rationale, answer_type, options, blocking}]" in prompt
    assert "Every subtask with expected outputs must have a corresponding WorkerItem" in prompt
    assert "Do not invent extra verification-only subtasks or WorkerItems" in prompt
    assert "impact_policy.manual_gate to false" in prompt
    assert "artifact_type:diff, artifact_type:log, artifact_type:report" in prompt
    assert (
        "Every acceptance_refs and required_evidence_refs entry must use the "
        "acceptance:<criterion-ref> format"
    ) in prompt
    assert "Never use acceptance_criterion: values" in prompt


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


def test_revision_planner_prompt_binds_complete_target_plan_and_exact_identity() -> None:
    planner_input, _ = _revision_planner_input()

    prompt = _planner_prompt(planner_input)

    assert '"schema_version":3' in prompt
    assert '"kind":"revision"' in prompt
    assert '"revision_context"' in prompt
    assert "complete target plan" in prompt
    assert "exact base_descriptor" in prompt
    assert "every existing subtask and WorkerItem identity decision exactly once" in prompt
    assert "complete subtask and WorkerItem DAG" in prompt
    assert "full replacement plan, not a patch fragment" in prompt


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
        "Every acceptance_refs and required_evidence_refs value must use the "
        "acceptance:<criterion-ref> format"
    ) in prompt
    assert "never acceptance_criterion:0001" in prompt
    assert "The validator reported this safe diagnostic" in prompt
    assert "proposal_reference_invalid" in prompt


def test_planner_uses_latest_acp_agent_message_when_provider_retries_output() -> None:
    valid = _response("clarification", [{
        "question": "What should be debugged?",
        "rationale": "The request is underspecified.",
        "answer_type": "string",
        "options": [],
        "blocking": True,
    }])
    notifications = (
        {
            "method": "session/update",
            "params": {
                "sessionId": "session",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "old-message",
                    "content": {"type": "text", "text": "{\"result_type\":\"proposal\"}"},
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

    assert _response_text(notifications, session_id="session", prompt_result={}) == valid


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
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        ),
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
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        ),
    )
    assert isinstance(fake_result, PlannerProposalResult)
    assert isinstance(fake_result.proposal.payload, PlanningRevisionProposalPayloadV3)
    monkeypatch.setenv(
        "SIDERA_TEST_PLANNER_RESPONSE",
        _response("proposal", fake_result.proposal.payload.to_mapping()),
    )

    result = _adapter(tmp_path).plan(
        planner_input,
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        ),
    )

    assert isinstance(result, PlannerProposalResult)
    assert isinstance(result.proposal.payload, PlanningRevisionProposalPayloadV3)
    assert result.proposal.payload.schema_version == 3
    assert result.proposal.derived_diff is not None


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
            PlannerRunControl(
                operation_id=planner_input.operation_id,
                started_at=NOW,
                deadline_at=NOW + timedelta(seconds=30),
            ),
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
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        ),
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
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        ),
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
                PlannerRunControl(
                    operation_id=planner_input.operation_id,
                    started_at=NOW,
                    deadline_at=NOW + timedelta(seconds=30),
                ),
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
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        ),
    )

    assert isinstance(result, PlannerProposalResult)


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
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        ),
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
                PlannerRunControl(
                    operation_id=planner_input.operation_id,
                    started_at=NOW,
                    deadline_at=NOW + timedelta(seconds=30),
                ),
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
