import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning import PlanningUsage
from tests.unit.test_opencode_planner import _planner_input


def test_private_workspace_reuses_original_directory_without_clearing(tmp_path):
    from sidera_control_plane.adapters.planning_workspace import (
        PrivatePlanningWorkspaceProvider,
    )

    provider = PrivatePlanningWorkspaceProvider(tmp_path / "planning")
    identity = {
        "workspace_id": uuid4(),
        "planning_run_id": uuid4(),
        "operation_id": uuid4(),
    }
    first = provider.prepare(**identity)
    sentinel = Path(first.directory) / "original-operation"
    sentinel.write_text("retained", encoding="utf-8")
    assert provider.prepare(**identity) == first
    assert sentinel.read_text(encoding="utf-8") == "retained"
    assert first.writable is True
    assert first.artifact_directory is None
    assert Path(first.directory).is_relative_to(tmp_path.resolve())
    assert Path(first.directory).stat().st_mode & 0o777 == 0o700
    for changed in ("workspace_id", "planning_run_id", "operation_id"):
        other = provider.prepare(**{**identity, changed: uuid4()})
        assert other.directory != first.directory


@pytest.mark.parametrize("case", ["relative", "parent", "file", "symlink"])
def test_private_workspace_rejects_untrusted_root(tmp_path, case):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.adapters.planning_workspace import (
        PrivatePlanningWorkspaceProvider,
    )

    root = tmp_path / "planning"
    if case == "relative":
        root = Path("planning")
    elif case == "parent":
        root = tmp_path / ".." / "escape"
    elif case == "file":
        root.write_text("file", encoding="utf-8")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AgentError) as denied:
        PrivatePlanningWorkspaceProvider(root).prepare(
            workspace_id=uuid4(), planning_run_id=uuid4(), operation_id=uuid4()
        )
    assert denied.value.code == "planning_workspace_unavailable"


def test_private_workspace_rejects_replaced_child(tmp_path):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.adapters.planning_workspace import (
        PrivatePlanningWorkspaceProvider,
    )

    root = tmp_path / "planning"
    provider = PrivatePlanningWorkspaceProvider(root)
    identity = {
        "workspace_id": uuid4(),
        "planning_run_id": uuid4(),
        "operation_id": uuid4(),
    }
    first = provider.prepare(**identity)
    directory = Path(first.directory)
    directory.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AgentError):
        provider.prepare(**identity)
    assert list(outside.iterdir()) == []


def test_native_clarification_uses_unknown_runtime_usage_without_body_accounting():
    from sidera_control_plane.application.planner_contract import parse_planner_result

    planner_input, _ = _planner_input()
    observed = PlanningUsage.unknown(runtime_seconds=2, context_bytes=41)
    body = {
        "result_type": "clarification",
        "clarifications": [
            {
                "question": "Which output?",
                "rationale": "The output is unspecified",
                "answer_type": "text",
                "options": [],
                "blocking": True,
            }
        ],
    }
    result = parse_planner_result(
        json.dumps(body), planner_input, "agent-run", observed_usage=observed
    )
    assert result.usage == observed
    assert result.usage.input_tokens is None
    assert result.usage.cost_microunits is None
    body["usage"] = {
        "input_tokens": 100,
        "output_tokens": 200,
        "cost_microunits": 300,
        "runtime_seconds": 0,
        "context_bytes": 0,
    }
    assert (
        parse_planner_result(
            json.dumps(body), planner_input, "agent-run", observed_usage=observed
        ).usage
        == observed
    )


@pytest.mark.parametrize(
    "text", ['{"invalid":true}', 'ready {"result_type":"proposal"}', "[]"]
)
def test_native_parser_rejects_noncontract_json(text):
    from sidera_control_plane.application.planner_contract import (
        PlannerContractError,
        parse_planner_result,
    )

    planner_input, _ = _planner_input()
    with pytest.raises(PlannerContractError):
        parse_planner_result(
            text, planner_input, "agent-run", observed_usage=PlanningUsage.unknown()
        )


def test_legacy_parser_still_requires_usage_and_native_body_usage_is_strict():
    from sidera_control_plane.application.planner_contract import (
        PlannerContractError,
        parse_planner_result,
    )

    planner_input, _ = _planner_input()
    body = {
        "result_type": "clarification",
        "question": "Which output?",
        "rationale": "Missing output",
        "answer_type": "text",
        "options": [],
        "blocking": True,
    }
    with pytest.raises(PlannerContractError):
        parse_planner_result(json.dumps(body), planner_input, "legacy")
    body["usage"] = {"invented": 0}
    with pytest.raises(PlannerContractError):
        parse_planner_result(
            json.dumps(body),
            planner_input,
            "agent-run",
            observed_usage=PlanningUsage.unknown(),
        )


def test_native_unknown_usage_roundtrips_without_changing_legacy_shape():
    from sidera_control_plane.persistence.planning_repository import (
        _usage_from_mapping,
        _usage_to_mapping,
    )

    usage = PlanningUsage.unknown(runtime_seconds=3, context_bytes=27)
    wire = _usage_to_mapping(usage)
    assert wire == {
        "input_tokens": None,
        "output_tokens": None,
        "cost_microunits": None,
        "runtime_seconds": 3,
        "context_bytes": 27,
    }
    assert _usage_from_mapping(wire) == usage
    assert _usage_to_mapping(PlanningUsage.zero()) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_microunits": 0,
        "runtime_seconds": 0,
        "context_bytes": 0,
    }
    with pytest.raises(ValidationError):
        replace(usage, input_tokens=True)


def native_snapshot(snapshot):
    from sidera_agents.contracts import ProfileRef

    return replace(
        snapshot,
        execution_mode="native_profile",
        planner_agent_ref=ProfileRef(
            workspace_id=snapshot.workspace_id, profile_id=uuid4(), revision=2
        ),
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_capabilities=None,
        planner_secret_refs=(),
    )


def test_native_role_contract_has_strict_schema_without_legacy_accounting_or_capability_alias():
    from sidera_control_plane.application.planner_contract import (
        native_planning_instructions,
    )
    from sidera_control_plane.application.planner_runtime import CapabilityCatalogEntry

    original, snapshot = _planner_input()
    planner_input = replace(
        original,
        profile_snapshot=native_snapshot(snapshot),
        capability_catalog=replace(
            original.capability_catalog,
            capabilities=(
                CapabilityCatalogEntry("native_tools", "supported: fixture_declared"),
            ),
        ),
    )
    instructions = native_planning_instructions(planner_input)
    assert "strict JSON" in instructions[0]
    shape = json.loads(instructions[-1])
    assert "usage" not in shape
    assert shape["proposal"]["worker_items"][0]["capability_requirements"] == [
        "native_tools"
    ]
    assert "legacy_prompt" not in " ".join(instructions)


def test_native_run_and_proposal_descriptor_roundtrip_without_module_placeholders():
    from datetime import UTC, datetime

    from sidera_control_plane.domain.planning import PlanningRun
    from sidera_control_plane.domain.planning_proposals import PlanningProfileDescriptor
    from sidera_control_plane.persistence.planning_repository import _run_model, _to_run

    _, legacy = _planner_input()
    snapshot = native_snapshot(legacy)
    run = PlanningRun.create(
        run_id=uuid4(),
        planning_request_id=uuid4(),
        workspace_id=snapshot.workspace_id,
        brief_revision=1,
        attempt=1,
        profile_snapshot=snapshot,
        queued_at=datetime.now(UTC),
    )
    assert run.usage.input_tokens is None
    row = _run_model(run)
    assert row.execution_mode == "native_profile"
    assert row.planner_module_instance_id is None
    assert row.capability_snapshot is None
    assert _to_run(row, snapshot).profile_snapshot == snapshot
    descriptor = PlanningProfileDescriptor.from_snapshot(snapshot)
    assert descriptor.to_mapping()["planner_module_instance_id"] is None
    assert descriptor.to_mapping()[
        "planner_agent_ref"
    ] == snapshot.planner_agent_ref.model_dump(mode="json")
    assert (
        "execution_mode"
        not in PlanningProfileDescriptor.from_snapshot(legacy).to_mapping()
    )
    with pytest.raises(ValidationError):
        replace(run, profile_snapshot=legacy)


def test_native_unknown_usage_remains_unknown_in_query_and_api():
    from sidera_control_plane.api.schemas import PlanningUsageResponse
    from sidera_control_plane.persistence.planning_query_store import _usage_view

    view = _usage_view(
        {
            "input_tokens": None,
            "output_tokens": None,
            "cost_microunits": None,
            "runtime_seconds": 2,
            "context_bytes": 17,
        }
    )
    assert view.input_tokens is None
    assert PlanningUsageResponse.from_view(view).model_dump() == {
        "input_tokens": None,
        "output_tokens": None,
        "cost_microunits": None,
        "runtime_seconds": 2,
        "context_bytes": 17,
    }


def test_native_revision_parser_uses_cp_schema_gate_without_legacy_capabilities():
    from sidera_control_plane.adapters.fake_planner import (
        FakePlannerAdapter,
        FakePlannerConfig,
    )
    from sidera_control_plane.application.planner_contract import (
        PlannerContractError,
        parse_planner_result,
    )
    from tests.unit.test_opencode_planner import _revision_planner_input, _run_control

    legacy_input, _ = _revision_planner_input()
    proposal = (
        FakePlannerAdapter(FakePlannerConfig(scenario="revision"))
        .plan(legacy_input, _run_control(legacy_input.operation_id))
        .proposal.payload.to_mapping()
    )
    planner_input = replace(
        legacy_input, profile_snapshot=native_snapshot(legacy_input.profile_snapshot)
    )
    body = {"result_type": "proposal", "proposal": proposal}
    result = parse_planner_result(
        json.dumps(body),
        planner_input,
        "agent-run",
        observed_usage=PlanningUsage.unknown(),
    )
    assert result.proposal.payload.schema_version == 3
    assert result.proposal.profile_descriptor.planner_module_instance_id is None
    with pytest.raises(PlannerContractError):
        parse_planner_result(
            json.dumps(body),
            replace(
                planner_input,
                profile_snapshot=replace(
                    planner_input.profile_snapshot, proposal_schema_version=4
                ),
            ),
            "agent-run",
            observed_usage=PlanningUsage.unknown(),
        )
    proposal["base_descriptor"]["plan_content_hash"] = "0" * 64
    with pytest.raises(PlannerContractError):
        parse_planner_result(
            json.dumps(body),
            planner_input,
            "agent-run",
            observed_usage=PlanningUsage.unknown(),
        )


def test_planner_link_requires_exact_start_and_durable_not_admitted_evidence():
    from sidera_agents.contracts import (
        ExecutionWorkspace,
        RunPolicy,
        RunStart,
        StartReceipt,
    )

    from sidera_control_plane.application.agent_worker_adapter import (
        request_fingerprint,
    )
    from sidera_control_plane.domain.planning_authority import PlanningAgentLink

    _, legacy = _planner_input()
    pin = native_snapshot(legacy).planner_agent_ref
    request = RunStart(
        operation_id=uuid4(),
        profile_ref=pin,
        grant_id=uuid4(),
        policy=RunPolicy.BOUNDED,
        instructions=("Return the CP schema",),
        text="Plan the goal",
        context=(),
        tools=(),
        workspace=ExecutionWorkspace(
            directory="/owned/scratch", writable=True, artifact_directory=None
        ),
        required_capabilities=(),
    )
    link = PlanningAgentLink(
        workspace_id=pin.workspace_id,
        planning_run_id=uuid4(),
        authority_id=request.grant_id,
        start_operation_id=request.operation_id,
        profile_ref=pin,
        start_request=request,
        request_sha256=request_fingerprint(request),
        status="starting",
    )
    assert not link.not_admitted
    receipt = StartReceipt(
        operation_id=request.operation_id,
        status="busy",
        run_id=None,
        reason_code="capacity",
    )
    waiting = replace(link, status="waiting_capacity", start_receipt=receipt)
    assert waiting.not_admitted
    assert not replace(waiting, status="starting").not_admitted
    assert not replace(waiting, status="unknown").retry_boundary_closed
    with pytest.raises(ValidationError):
        replace(link, start_operation_id=uuid4())
    with pytest.raises(ValidationError):
        replace(link, status="waiting_capacity")
    with pytest.raises(ValidationError):
        replace(link, run_id=uuid4(), status="closed", closed=False)


@pytest.mark.parametrize(("interval", "expected_timeout"), [(0.01, 2.0), (5.0, 6.0)])
def test_planning_keeper_stop_sets_event_before_finite_join(interval, expected_timeout):
    from sidera_control_plane.application.planning_runner import PlanningLeaseKeeper

    keeper = PlanningLeaseKeeper(
        uow_factory=lambda: None,
        lease_id=uuid4(),
        planning_run_id=uuid4(),
        workspace_id=uuid4(),
        owner_id="planner",
        fencing_token=1,
        lease_ttl_seconds=60,
        renew_interval_seconds=interval,
    )

    class JoinBoundary:
        def join(self, *, timeout=None):
            assert keeper._stop_event.is_set()
            assert timeout == expected_timeout, (
                "keeper shutdown lost its existing finite bound"
            )

    keeper._thread = JoinBoundary()
    keeper.stop()
