import json
import stat
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.fake_planner import _proposal_payload
from sidera_control_plane.adapters.opencode_planner import OpenCodePlannerAdapter
from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogSnapshot,
    PlannerInput,
    PlannerPolicyConstraints,
    PlannerRunControl,
)
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import PlanningBriefRevision
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("70000000-0000-0000-0000-000000000001")


def module_facts() -> tuple[ModuleDefinition, ModuleInstance]:
    definition_id = uuid4()
    instance_id = uuid4()
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=WORKSPACE_ID,
        module_type="planner_adapter",
        provider="fake",
        interface_version="sidera.planner.v1",
        config_schema_version=1,
        capabilities=PlannerAdapterCapabilityProfile(
            structured_output=StructuredOutputCapability.JSON_SCHEMA,
            clarification=True,
            idempotency=IdempotencyCapability.OPERATION_ID,
            cancellation=CancellationCapability.RUN,
            usage_reporting=UsageReportingCapability.TOKENS,
            max_context_bytes=128_000,
            stability=StabilityLevel.STABLE,
            interface_version="sidera.planner.v1",
            proposal_schema_versions=(2,),
        ),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=instance_id,
        workspace_id=WORKSPACE_ID,
        definition_id=definition_id,
        instance_name="test-planner",
        config_version=1,
        config_namespace="planning",
        config={"planning.mode": "test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    return definition, instance


def profile(instance_id: UUID) -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_key="default",
        version=1,
        display_name="Default planner",
        planner_module_instance_id=instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={"approval": {"required": True}},
        proposal_schema_version=2,
        allowed_capabilities=("code",),
        adapter_preferences=("opencode",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=10_000,
            max_cost_microunits=1_000_000,
            max_runtime_seconds=600,
            max_revisions=3,
            max_nodes=20,
            max_concurrency=2,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(5,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=(),
    )


def _fake_opencode(tmp_path: Path) -> Path:
    script = tmp_path / "fake-opencode-contract"
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys

            def write(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()

            for raw in sys.stdin:
                request = json.loads(raw)
                method = request.get("method")
                if method == "initialize":
                    write({"jsonrpc": "2.0", "id": request["id"], "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {"loadSession": False},
                    }})
                elif method == "session/new":
                    write({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": "contract"}})
                elif method == "session/prompt":
                    write({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": "contract",
                        "update": {"sessionUpdate": "agent_message_chunk", "content": {
                            "type": "text", "text": os.environ["SIDERA_TEST_PLANNER_RESPONSE"]
                        }}
                    }})
                    write({"jsonrpc": "2.0", "id": request["id"], "result": {"stopReason": "end_turn"}})
            """
        ).strip()
        + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_real_provider_contract_keeps_structured_output_honest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition, instance = module_facts()
    assert isinstance(definition.capabilities, PlannerAdapterCapabilityProfile)
    snapshot = profile(instance.id).snapshot(definition.capabilities)
    request_id = uuid4()
    brief = PlanningBriefRevision.create(
        planning_request_id=request_id,
        revision=1,
        objective="Validate OpenCode planner contract",
        constraints=(),
        acceptance_criteria=("Strict JSON is accepted",),
        context_refs=(),
        requested_outputs=("proposal",),
        user_feedback=None,
        source_correlation=uuid4(),
        created_by="integration-test",
        created_at=NOW,
    )
    planner_input = PlannerInput(
        interface_version=snapshot.planner_interface_version,
        proposal_schema_version=snapshot.proposal_schema_version,
        operation_id=uuid4(),
        planning_request_id=request_id,
        planning_run_id=uuid4(),
        brief_revision=1,
        brief=brief,
        profile_snapshot=snapshot,
        capability_catalog=CapabilityCatalogSnapshot(
            snapshot_version=1,
            captured_at=NOW,
            capabilities=(),
            adapter_preferences=(),
            budget_level="standard",
            max_concurrency=1,
            workspace_providers=(),
        ),
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(snapshot),
        context_refs=(),
    )
    response = json.dumps({
        "result_type": "proposal",
        "proposal": _proposal_payload().to_mapping(),
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cost_microunits": 0,
            "runtime_seconds": 1,
            "context_bytes": 10,
        },
    })
    monkeypatch.setenv("SIDERA_TEST_PLANNER_RESPONSE", response)
    adapter = OpenCodePlannerAdapter(
        executable=str(_fake_opencode(tmp_path)),
        temp_root=tmp_path / "runs",
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
    )

    assert adapter.probe({"cwd": str(tmp_path)}).structured_output is StructuredOutputCapability.JSON
    result = adapter.plan(
        planner_input,
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=NOW,
            deadline_at=None,
        ),
    )

    assert result.usage.output_tokens == 2
