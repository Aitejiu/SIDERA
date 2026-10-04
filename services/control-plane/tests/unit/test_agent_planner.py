import json
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning import PlanningUsage
from tests.unit.test_agent_worker_adapter import native_start_case  # noqa: F401
from tests.unit.test_opencode_planner import _planner_input


@pytest.fixture
def planner_drain_case(request):
    from datetime import timedelta

    from sidera_agents.contracts import (
        EffectiveConfig,
        EventPage,
        RunEvent,
        RunSnapshot,
        RuntimeRef,
        StartLookup,
        StartReceipt,
        StopReceipt,
    )

    from sidera_control_plane.application.agent_planner_adapter import (
        AgentPlannerAdapter,
    )
    from sidera_control_plane.application.agent_worker_adapter import (
        request_fingerprint,
    )
    from sidera_control_plane.domain.planning_authority import (
        PlanningAgentLink,
        PlanningAuthority,
    )

    s = request.getfixturevalue("native_start_case")
    start = s.request
    run_id, response_id, planning_run_id, planning_request_id = (
        uuid4() for _ in range(4)
    )
    runtime = RuntimeRef(
        workspace_id=start.profile_ref.workspace_id, runtime_id=uuid4()
    )
    authority = PlanningAuthority(
        start.grant_id,
        start.profile_ref.workspace_id,
        planning_request_id,
        planning_run_id,
        uuid4(),
        1,
        start.operation_id,
        "operator",
        uuid4(),
        start.profile_ref,
        runtime,
        uuid4(),
        "planner",
        1,
        start.workspace,
        ("run.read", "run.stop"),
    )
    receipt = StartReceipt(
        operation_id=start.operation_id,
        status="accepted",
        run_id=run_id,
        reason_code=None,
    )
    state = SimpleNamespace(
        link=PlanningAgentLink(
            authority.workspace_id,
            planning_run_id,
            authority.id,
            start.operation_id,
            start.profile_ref,
            start,
            request_fingerprint(start),
            "running",
            run_id,
            receipt,
        ),
        owner=True,
        fail_save=False,
        fail_commit=False,
        fail_observation=False,
        stops=[],
        queries=[],
        cursors=[],
        starts=[],
        receipt=StopReceipt(
            operation_id=start.operation_id,
            run_id=run_id,
            status="stopped",
            reason_code=None,
            evidence_sequence=7,
        ),
        snapshot=RunSnapshot(
            run_id=run_id,
            workspace_id=authority.workspace_id,
            profile_ref=start.profile_ref,
            policy="bounded",
            state="stopped",
            input_open=False,
            active_response_id=None,
            effective_config=EffectiveConfig(
                profile_ref=start.profile_ref,
                config_hash="test-pin",
                runtime_ref=runtime,
                actual_runtime_version="test-1",
                selected_model=None,
                observed_model=None,
                inherited_fields=(),
                applied_fields=(),
                resource_refs=(),
                context_receipts=(),
            ),
            last_event_sequence=7,
            capabilities={},
            reason_code=None,
        ),
    )
    state.events = tuple(
        RunEvent(
            run_id=run_id,
            sequence=i,
            kind="response.started"
            if i == 1
            else "run.closed"
            if i == 7
            else "progress.updated",
            occurred_at=s.now,
            operation_id=start.operation_id,
            response_id=response_id if i < 7 else None,
            data={"status": "stopped"} if i == 7 else {},
        )
        for i in range(1, 8)
    )

    def save(link, **changes):
        assert link == state.link
        if state.fail_save:
            raise RuntimeError("controlled original CAS failure")
        state.link = replace(link, **changes)
        return state.link

    def observation(link, *, event_cursor, response_id, closed):
        result = save(
            link,
            event_cursor=event_cursor,
            response_id=response_id,
            status="closed" if closed else link.status,
            closed=closed,
        )
        if state.fail_observation:
            raise RuntimeError("controlled original cursor save failure")
        return result

    saved_run = SimpleNamespace(
        planning_request_id=planning_request_id,
        operation_id=start.operation_id,
        profile_snapshot=SimpleNamespace(
            profile_id=authority.planning_profile_id,
            profile_version=1,
            planner_agent_ref=start.profile_ref,
            execution_mode="native_profile",
        ),
    )
    lease = SimpleNamespace(
        id=authority.lease_id,
        workspace_id=authority.workspace_id,
        planning_run_id=planning_run_id,
        owner_id=authority.owner_id,
        fencing_token=1,
        status=SimpleNamespace(value="active"),
        expires_at=s.now + timedelta(minutes=1),
    )
    uow = SimpleNamespace(
        planning_authorities=SimpleNamespace(
            get=lambda *args, **kwargs: authority,
            get_link=lambda *args, **kwargs: state.link,
            save_link=save,
            save_original_observation=observation,
        ),
        planning_runs=SimpleNamespace(get=lambda *args: saved_run),
        planning_requests=SimpleNamespace(
            get=lambda *args: SimpleNamespace(id=planning_request_id)
        ),
        planning_leases=SimpleNamespace(get=lambda *args: lease),
    )

    @contextmanager
    def owned_uow():
        original = state.link
        try:
            yield uow
            if state.fail_commit or not state.owner:
                raise RuntimeError("controlled owned commit failure")
        except Exception:
            state.link = original
            raise

    def stop(request):
        assert state.link.status == "cleanup_pending", (
            "Stop preceded its durable one-way latch"
        )
        assert (
            request.workspace_id,
            request.run_id,
            request.operation_id,
            request.grant_id,
        ) == (
            authority.workspace_id,
            run_id,
            start.operation_id,
            authority.id,
        )
        state.stops.append(request)
        return state.receipt

    def query(workspace, supplied_run, operation, *, grant_id):
        assert (workspace, supplied_run, operation, grant_id) == (
            authority.workspace_id,
            run_id,
            start.operation_id,
            authority.id,
        )
        state.queries.append(operation)
        return state.receipt

    def events(workspace, supplied_run, after_sequence, limit=100, *, grant_id):
        assert (workspace, supplied_run, grant_id) == (
            authority.workspace_id,
            run_id,
            authority.id,
        )
        state.cursors.append(after_sequence)
        visible = tuple(
            event for event in state.events if event.sequence > after_sequence
        )[:limit]
        return EventPage(
            events=visible,
            next_sequence=visible[-1].sequence if visible else after_sequence,
            has_more=False,
        )

    def no_admission():
        pytest.fail("Original Planner drain entered a new-start permit")

    adapter = AgentPlannerAdapter(
        services=SimpleNamespace(
            principal_id="operator",
            start_admission=no_admission,
            execution=SimpleNamespace(
                start=lambda value: state.starts.append(value),
                stop=stop,
                get_stop=query,
                get_run=lambda *args, **kwargs: state.snapshot,
                list_events=events,
                get_start=lambda *args, **kwargs: StartLookup(
                    found=True, receipt=receipt
                ),
            ),
        ),
        uow_factory=owned_uow,
        workspace_provider=None,
        materializer=None,
        authorizer=None,
        owner_check=lambda: state.owner,
        clock=lambda: s.now,
    )
    return SimpleNamespace(
        adapter=adapter,
        state=state,
        authority=authority,
        response_id=response_id,
        lease=lease,
    )


def test_planner_halt_unknown_stop_does_not_close_stopped_snapshot_without_cursor(
    planner_drain_case,
):
    s = planner_drain_case
    s.state.receipt = type(s.state.receipt).model_validate(
        {**s.state.receipt.model_dump(), "status": "unknown", "evidence_sequence": None}
    )
    result = s.adapter._halt(
        None, s.state.link, "native_cancelled", PlanningUsage.unknown()
    )
    assert result.code == "native_cleanup_pending" and result.unknown_outcome is True
    assert s.state.link.status == "cleanup_pending" and not s.state.link.closed
    assert (
        s.state.link.event_cursor == 0
        and len(s.state.stops) == 1
        and not s.state.starts
    )


def test_planner_drain_stop_once_queries_original_and_consumes_real_cursor(
    planner_drain_case,
):
    s = planner_drain_case
    stopped = s.state.receipt
    s.state.receipt = type(stopped).model_validate(
        {**stopped.model_dump(), "status": "accepted", "evidence_sequence": None}
    )
    assert s.adapter.drain_original_run(s.state.link) is False
    assert not s.state.link.closed and len(s.state.stops) == 1
    s.state.receipt = stopped
    assert s.adapter.drain_original_run(s.state.link) is True
    assert s.state.queries == [s.authority.operation_id] and len(s.state.stops) == 1
    assert s.state.link.closed and s.state.link.event_cursor == 7
    assert s.state.link.response_id == s.response_id and s.state.cursors == [0]
    assert s.adapter.drain_original_run(s.state.link) is True and not s.state.starts


@pytest.mark.parametrize(
    "wrong",
    (
        "failed",
        "unknown",
        "missing_evidence",
        "run",
        "operation",
        "input",
        "response",
        "snapshot_runtime",
        "watermark",
        "gap",
    ),
)
def test_planner_drain_unconfirmed_original_proof_never_closes(
    planner_drain_case, wrong
):
    s = planner_drain_case
    if wrong in {"failed", "unknown"}:
        s.state.receipt = type(s.state.receipt).model_validate(
            {**s.state.receipt.model_dump(), "status": wrong}
        )
    elif wrong == "missing_evidence":
        s.state.receipt = s.state.receipt.model_copy(update={"evidence_sequence": None})
    elif wrong in {"run", "operation"}:
        s.state.receipt = s.state.receipt.model_copy(
            update={"run_id" if wrong == "run" else "operation_id": uuid4()}
        )
    elif wrong == "input":
        s.state.snapshot = s.state.snapshot.model_copy(update={"input_open": True})
    elif wrong == "response":
        s.state.snapshot = s.state.snapshot.model_copy(
            update={"active_response_id": uuid4()}
        )
    elif wrong == "snapshot_runtime":
        config = s.state.snapshot.effective_config.model_copy(
            update={
                "runtime_ref": s.authority.runtime_ref.model_copy(
                    update={"runtime_id": uuid4()}
                )
            }
        )
        s.state.snapshot = s.state.snapshot.model_copy(
            update={"effective_config": config}
        )
    elif wrong == "watermark":
        s.state.snapshot = s.state.snapshot.model_copy(
            update={"last_event_sequence": 8}
        )
    else:
        s.state.events = s.state.events[1:]
    assert s.adapter.drain_original_run(s.state.link) is False
    assert s.state.link.status == (
        "running" if wrong == "snapshot_runtime" else "cleanup_pending"
    )
    assert not s.state.link.closed and not s.state.starts
    assert len(s.state.stops) == (0 if wrong == "snapshot_runtime" else 1)


@pytest.mark.parametrize("failure", ("save", "commit", "owner"))
def test_planner_drain_failed_latch_never_sends_stop(planner_drain_case, failure):
    from sidera_agents.errors import AgentError

    s = planner_drain_case
    original = s.state.link
    s.state.fail_save = failure == "save"
    s.state.fail_commit = failure == "commit"
    s.state.owner = failure != "owner"
    if failure == "owner":
        with pytest.raises(AgentError, match="owner"):
            s.adapter.drain_original_run(original)
    else:
        assert s.adapter.drain_original_run(original) is False
    assert s.state.link == original and not s.state.stops and not s.state.starts


def test_planner_drain_uses_saved_state_not_supplied_fake_closed_fact(
    planner_drain_case,
):
    s = planner_drain_case
    supplied = replace(s.state.link, status="closed", closed=True)
    assert s.adapter.drain_original_run(supplied) is True
    assert (
        s.state.link.closed
        and s.state.link.event_cursor == 7
        and len(s.state.stops) == 1
    )


def test_planner_drain_cursor_failure_rolls_back_and_next_tick_queries_original(
    planner_drain_case,
):
    s = planner_drain_case
    s.state.fail_observation = True
    assert s.adapter.drain_original_run(s.state.link) is False
    assert not s.state.link.closed and s.state.link.event_cursor == 0
    assert s.state.link.status == "cleanup_pending" and len(s.state.stops) == 1
    s.state.fail_observation = False
    assert s.adapter.drain_original_run(s.state.link) is True
    assert (
        len(s.state.stops) == len(s.state.queries) == 1
        and s.state.link.event_cursor == 7
    )


def test_planner_drain_original_unknown_start_queries_and_never_restarts(
    planner_drain_case,
):
    s = planner_drain_case
    s.state.link = replace(
        s.state.link, status="unknown", run_id=None, start_receipt=None
    )
    assert s.adapter.drain_original_run(s.state.link) is True
    assert not s.state.starts and len(s.state.stops) == 1 and s.state.link.closed


def test_planner_drain_natural_completion_scans_without_stop(planner_drain_case):
    s = planner_drain_case
    s.state.snapshot = type(s.state.snapshot).model_validate(
        {**s.state.snapshot.model_dump(), "state": "completed"}
    )
    s.state.events = (
        *s.state.events[:-1],
        s.state.events[-1].model_copy(update={"data": {"status": "completed"}}),
    )
    assert s.adapter.drain_original_run(s.state.link) is True
    assert not s.state.stops and not s.state.queries
    assert s.state.link.closed and s.state.link.event_cursor == 7


def test_planner_drain_expired_original_uses_historical_read_and_stop_only(
    planner_drain_case,
):
    from datetime import timedelta

    s = planner_drain_case
    s.lease.expires_at -= timedelta(days=1)
    s.lease.status = SimpleNamespace(value="expired")
    assert s.adapter.drain_original_run(s.state.link) is True
    assert (
        len(s.state.stops) == 1
        and not s.state.starts
        and s.state.link.event_cursor == 7
    )


@pytest.mark.parametrize("closing", (False, True))
def test_original_cleanup_or_shutdown_never_enters_planning_business_recovery(
    planner_drain_case, closing
):
    from sidera_control_plane.application.planning_runner import PlanningRunner

    def no_write_ownership():
        pytest.fail("Cleanup or shutdown entered PlanningRun result ownership")

    runner = PlanningRunner(
        uow_factory=no_write_ownership,
        adapter_factory=None,
        owner_id="planner",
        native_shutdown_check=lambda: closing,
    )
    link = replace(
        planner_drain_case.state.link,
        status="running" if closing else "cleanup_pending",
    )
    assert runner.reconcile_original_run(link) is None


@pytest.mark.parametrize("busy", (False, True))
@pytest.mark.parametrize("code", ("permission_denied", "control_plane_closed"))
def test_planner_start_admission_denies_before_starting_without_unknown(
    request, monkeypatch, busy, code
):
    from sidera_agents.contracts import StartReceipt

    from sidera_control_plane.application.agent_planner_adapter import (
        AgentPlannerAdapter,
    )
    from sidera_control_plane.application.agent_worker_adapter import (
        request_fingerprint,
    )
    from sidera_control_plane.application.planner_runtime import PlannerRunControl
    from sidera_control_plane.domain.planning_authority import PlanningAgentLink

    s = request.getfixturevalue("native_start_case")
    planner_input, _ = _planner_input()
    request = s.request.model_copy(update={"operation_id": planner_input.operation_id})
    link = PlanningAgentLink(
        request.profile_ref.workspace_id,
        planner_input.planning_run_id,
        request.grant_id,
        request.operation_id,
        request.profile_ref,
        request,
        request_fingerprint(request),
        "pending" if not busy else "waiting_capacity",
        start_receipt=None
        if not busy
        else StartReceipt(
            operation_id=request.operation_id,
            status="busy",
            run_id=None,
            reason_code=None,
        ),
    )
    state = SimpleNamespace(link=link, changes=[], starts=[], stops=[])

    def save_link(supplied, **changes):
        state.changes.append(changes)
        pytest.fail("Known admission denial changed the original Planner intention")

    services = SimpleNamespace(
        authorizer=SimpleNamespace(require=lambda *args: None),
        start_admission=s.permit,
        execution=SimpleNamespace(
            start=lambda request: state.starts.append(request),
            stop=lambda request: state.stops.append(request),
        ),
    )
    adapter = AgentPlannerAdapter(
        services=services,
        uow_factory=lambda: nullcontext(s.uow),
        workspace_provider=None,
        materializer=None,
        authorizer=None,
        owner_check=lambda: True,
        clock=lambda: s.now,
    )
    monkeypatch.setattr(adapter, "_run_authorizer", services.authorizer, raising=False)
    monkeypatch.setattr(
        adapter, "_prepare", lambda supplied, control: (link, supplied, 0)
    )
    monkeypatch.setattr(adapter, "_save", save_link)
    s.state.denied = code
    control = PlannerRunControl(
        request.operation_id,
        s.now,
        None,
        owner_id="planner",
        lease_id=uuid4(),
        fencing_token=1,
    )
    outcome = adapter.plan(planner_input, control)
    assert outcome.code == code and outcome.unknown_outcome is False
    assert (
        state.link == link
        and not state.changes
        and not state.starts
        and not state.stops
    )
    assert s.state.permit == ["enter"]


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
