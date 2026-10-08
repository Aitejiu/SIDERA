import hashlib
import importlib.util
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sidera_agents.contracts import (
    AssembledInput,
    ContextItem,
    ExecutionWorkspace,
    InputReceipt,
    NativeOpenRequest,
    ProfileRef,
    RunState,
    RuntimeSnapshot,
    ToolConnection,
)
from sidera_agents.runs import VerifiedNativeOpenFailure
from sidera_control_plane.application.proposal_materializer import (
    _plan_worker_dependencies,
)
from sidera_control_plane.domain.context_packages import ResultInputManifestV1
from sidera_control_plane.domain.subtask_projections import _worker_graph


@pytest.fixture
def module():
    assert importlib.util.find_spec("tests.support.native_product_harness") is not None
    from tests.support import native_product_harness

    return native_product_harness


def section(name, value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return ContextItem(
        item_id=name,
        origin="control-plane",
        required=True,
        ordinal=0,
        version="pinned",
        text=text,
        content_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
    )


@pytest.fixture
def lab(module, tmp_path, monkeypatch):
    workspace = uuid4()
    profile = ProfileRef(workspace_id=workspace, profile_id=uuid4(), revision=1)
    verified = {}
    receipts = {}
    reads = []
    tool_calls = []
    runtime = RuntimeSnapshot(
        ref={"workspace_id": workspace, "runtime_id": uuid4()},
        harness_id=module.NativeProductJourneyHarness.harness_id,
        executable=sys.executable,
        actual_version="controlled-1",
        installed=True,
        enabled=True,
        version=1,
        protocol_state="ready",
        auth_state="ready",
        capabilities={},
        checked_at=datetime.now(UTC),
        limits={"max_active": 4},
    )
    harness = module.NativeProductJourneyHarness()

    def get_input(ws, run, op, *, grant_id):
        reads.append((ws, run, op, grant_id))
        return receipts[op]

    harness.runtime = SimpleNamespace(
        settings=SimpleNamespace(workspace_id=workspace, workspace_root=tmp_path),
        grants=SimpleNamespace(verify=lambda token: verified[token.get_secret_value()]),
        execution=SimpleNamespace(get_input=get_input),
    )

    async def exchange(connection, *, name=None, arguments=None):
        grant = verified[connection.headers["Authorization"].get_secret_value()[7:]]
        if name is None:
            if grant.record.role == "main":
                return {
                    "sidera_list_profiles",
                    "sidera_submit_goal",
                    "sidera_get_planning",
                    "sidera_get_task",
                    "sidera_list_tasks",
                    "sidera_request_revision",
                }
            if grant.record.role == "worker":
                return {"sidera_get_task", "sidera_report_progress"}
            return (
                {"sidera_get_planning", "sidera_get_task"}
                if grant.record.root_task_id is not None
                else {"sidera_get_planning"}
            )
        tool_calls.append((name, arguments))
        return SimpleNamespace(isError=True, structuredContent=None)

    monkeypatch.setattr(module, "sdk_exchange", exchange)

    def opened(role, *, directory=None, root_task_id=None):
        run = uuid4()
        directory = directory or tmp_path / str(run)
        directory.mkdir(exist_ok=True)
        record = SimpleNamespace(
            role=role,
            workspace_id=workspace,
            profile_ref=profile,
            native_grant_id=uuid4(),
            grant_id=uuid4(),
            root_task_id=root_task_id,
        )
        verified[str(run)] = SimpleNamespace(
            record=record,
            scope=None,
            run=SimpleNamespace(
                run_id=run,
                workspace_id=workspace,
                profile_ref=profile,
                state=RunState.STARTING,
                effective_config=SimpleNamespace(
                    profile_ref=profile,
                    runtime_ref=runtime.ref,
                    actual_runtime_version=runtime.actual_version,
                ),
            ),
        )
        request = NativeOpenRequest(
            run_id=run,
            runtime=runtime,
            projected_config={},
            workspace=ExecutionWorkspace(
                directory=str(directory), writable=True, artifact_directory=None
            ),
            tools=(
                ToolConnection(
                    name="SIDERA Task Tools",
                    transport="http",
                    endpoint_or_argv="http://127.0.0.1:1/tools",
                    headers={"Authorization": SecretStr("Bearer " + str(run))},
                    environment={},
                ),
            ),
            environment={},
        )
        return request

    return SimpleNamespace(
        native=harness,
        opened=opened,
        verified=verified,
        receipts=receipts,
        reads=reads,
        tool_calls=tool_calls,
        workspace=workspace,
        profile=profile,
    )


@pytest.mark.parametrize("role", ["main", "planner", "worker"])
def test_actual_open_role_sets_use_same_profile_and_deny_starting_call(
    module, lab, role
):
    request = lab.opened(role)
    handle = lab.native.open(request)
    assert handle.run_id == request.run_id
    facts = lab.native.public_facts()
    assert facts["opens"][0]["role"] == role
    assert facts["opens"][0]["profile_ref"] == lab.profile.model_dump(mode="json")
    assert facts["opens"][0]["starting_call_denied"] is True
    assert "Bearer" not in json.dumps(facts)
    assert handle.session_id not in json.dumps(facts)


@pytest.mark.parametrize(
    "case",
    ["role", "run", "profile", "workspace", "state", "runtime", "path", "symlink"],
)
def test_foreign_open_fails_before_handle_or_write(module, lab, tmp_path, case):
    request = lab.opened("worker")
    verified = lab.verified[str(request.run_id)]
    if case == "role":
        verified.record.role = "unknown"
    elif case == "run":
        verified.run.run_id = uuid4()
    elif case == "profile":
        verified.run.profile_ref = lab.profile.model_copy(update={"revision": 2})
    elif case == "workspace":
        verified.record.workspace_id = uuid4()
    elif case == "state":
        verified.run.state = RunState.RUNNING
    elif case == "runtime":
        verified.run.effective_config.runtime_ref = request.runtime.ref.model_copy(
            update={"runtime_id": uuid4()}
        )
    else:
        directory = tmp_path.parent / "foreign-native-directory"
        if case == "symlink":
            directory = tmp_path / "link"
            directory.symlink_to(tmp_path, target_is_directory=True)
        request = request.model_copy(
            update={
                "workspace": request.workspace.model_copy(
                    update={"directory": str(directory)}
                )
            }
        )
    with pytest.raises(VerifiedNativeOpenFailure) as error:
        lab.native.open(request)
    assert error.value.reason_code == "native_start_failed"
    assert not lab.native.handles
    assert not list(tmp_path.rglob("native_value.py"))


@pytest.mark.parametrize(
    "revision,expected",
    [
        (False, ["sidera_get_planning"]),
        (True, ["sidera_get_planning", "sidera_get_task"]),
    ],
)
def test_planner_open_compares_exact_tools_for_the_verified_create_or_revision_grant(
    lab, revision, expected
):
    request = lab.opened("planner", root_task_id=uuid4() if revision else None)
    lab.native.open(request)
    assert lab.native.public_facts()["opens"][0]["tools"] == expected
    assert lab.tool_calls == [(expected[0], None)]


@pytest.mark.parametrize(
    "names",
    [
        set(),
        {"sidera_get_task"},
        {"sidera_get_planning", "sidera_get_task"},
        {"sidera_get_planning", "sidera_submit_goal"},
    ],
)
def test_planner_sdk_missing_wrong_or_excess_names_prove_no_handle_created(
    module, lab, monkeypatch, names
):
    async def exchange(connection, *, name=None, arguments=None):
        return names if name is None else SimpleNamespace(isError=True)

    monkeypatch.setattr(module, "sdk_exchange", exchange)
    with pytest.raises(VerifiedNativeOpenFailure) as error:
        lab.native.open(lab.opened("planner"))
    assert error.value.reason_code == "native_start_failed"
    assert lab.native.handles == {} and lab.native.open_facts == []


@pytest.mark.parametrize(
    "case", ["tools", "authorization", "grant", "sdk", "starting_call"]
)
def test_pre_handle_validation_or_sdk_failure_is_sanitized_verified_no_creation(
    module, lab, monkeypatch, case
):
    request = lab.opened("worker")
    if case == "tools":
        request = request.model_copy(update={"tools": ()})
    elif case == "authorization":
        request = request.model_copy(
            update={"tools": (request.tools[0].model_copy(update={"headers": {}}),)}
        )
    elif case == "grant":

        def refused(token):
            raise ValueError("PRIVATE_GRANT_DETAILS")

        monkeypatch.setattr(lab.native.runtime.grants, "verify", refused)
    else:

        async def exchange(connection, *, name=None, arguments=None):
            if case == "sdk":
                raise RuntimeError("PRIVATE_SDK_DETAILS")
            return (
                {"sidera_get_task", "sidera_report_progress"}
                if name is None
                else SimpleNamespace(isError=False)
            )

        monkeypatch.setattr(module, "sdk_exchange", exchange)
    with pytest.raises(VerifiedNativeOpenFailure) as error:
        lab.native.open(request)
    assert error.value.reason_code == "native_start_failed"
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert "PRIVATE" not in str(error.value)
    assert lab.native.handles == {} and lab.native.open_facts == []


def test_open_scope_is_verified_before_any_sdk_call(module, lab, monkeypatch):
    request = lab.opened("worker")
    lab.verified[str(request.run_id)].run.state = RunState.RUNNING
    calls = []

    async def exchange(connection, *, name=None, arguments=None):
        calls.append(name)
        return {"sidera_get_task", "sidera_report_progress"}

    monkeypatch.setattr(module, "sdk_exchange", exchange)
    with pytest.raises(VerifiedNativeOpenFailure):
        lab.native.open(request)
    assert calls == [] and lab.native.handles == {}


def test_failure_after_native_handle_allocation_does_not_claim_no_creation(
    module, lab, monkeypatch
):
    original = module.ControlledHarness.open

    def allocated_then_failed(native, request):
        original(native, request)
        raise RuntimeError("controlled_after_handle_failure")

    monkeypatch.setattr(module.ControlledHarness, "open", allocated_then_failed)
    request = lab.opened("worker")
    with pytest.raises(RuntimeError, match="controlled_after_handle_failure"):
        lab.native.open(request)
    assert request.run_id in lab.native.handles


def worker_input(
    role, request_id=None, execution_id=None, worker_id=None, lineage=None
):
    facts = {
        "execution_request": {"id": str(request_id or uuid4())},
        "execution": {
            "id": str(execution_id or uuid4()),
            "lineage_id": str(lineage or uuid4()),
            "attempt": 1,
        },
        "worker_item": {"id": str(worker_id or uuid4())},
    }
    return AssembledInput(
        instructions=(json.dumps({"role": role}),),
        text=role,
        context=(
            section(
                "task",
                {
                    "revision_id": str(uuid4()),
                    "revision_number": 1,
                    "content_hash": "a" * 64,
                    "snapshot": {},
                },
            ),
            section("facts", facts),
        ),
    )


def test_source_gate_requires_actual_accepted_original_input_and_second_response(
    module, lab
):
    request = lab.opened("worker")
    handle = lab.native.open(request)
    first, second, op = uuid4(), uuid4(), uuid4()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lab.native.prompt, handle, worker_input("source"), first)
        assert lab.native.source_entered.wait(2)
        assert not lab.native.release_original_prompt(request.run_id)
        lab.receipts[op] = InputReceipt(
            operation_id=op,
            run_id=request.run_id,
            status="delivered",
            reason_code=None,
            evidence_sequence=1,
        )
        assert not lab.native.release_accepted_prompt(request.run_id, op)
        assert not pending.done()
        lab.receipts[op] = lab.receipts[op].model_copy(update={"status": "accepted"})
        assert lab.native.release_accepted_prompt(request.run_id, op)
        assert pending.result(2).status.value == "completed"
    assert (
        lab.native.prompt(
            handle,
            AssembledInput(instructions=(), text="owned input", context=()),
            second,
        ).response_id
        == second
    )
    assert lab.reads[-1] == (
        lab.workspace,
        request.run_id,
        op,
        lab.verified[str(request.run_id)].record.native_grant_id,
    )
    assert (
        module.Path(request.workspace.directory) / "native_value.py"
    ).read_text() == "VALUE = 42\n"
    events = lab.native.read_events(handle, 0)
    assert [e.cursor for e in events] == [1, 2]
    assert [e.response_id for e in events] == [first, second]
    assert lab.native.read_events(handle, 1, 1) == (events[1],)
    assert (
        lab.native.public_facts()["inputs"][-1]["text_hash"]
        == hashlib.sha256(b"owned input").hexdigest()
    )


def test_source_gate_default_uses_wide_budget_without_waiting(module, lab, monkeypatch):
    waits = []
    monkeypatch.setattr(
        module,
        "Event",
        lambda: SimpleNamespace(wait=lambda seconds: waits.append(seconds) or False),
    )
    request = lab.opened("worker")
    handle = lab.native.open(request)
    with pytest.raises(ValueError, match="native_product_original_prompt_not_released"):
        lab.native.prompt(handle, worker_input("source"), uuid4())
    assert waits == [3600]
    assert not (module.Path(request.workspace.directory) / "native_value.py").exists()


@pytest.mark.parametrize(
    "seconds", [False, True, None, 0, -1, float("inf"), float("-inf"), float("nan")]
)
def test_source_gate_override_refuses_invalid_allowance(module, seconds):
    with pytest.raises(ValueError, match="native_source_gate_seconds_refused"):
        module.NativeProductJourneyHarness(source_gate_seconds=seconds)


def test_source_gate_directed_timeout_remains_honest(module, lab):
    runtime = lab.native.runtime
    lab.native = module.NativeProductJourneyHarness(source_gate_seconds=0.001)
    lab.native.runtime = runtime
    request = lab.opened("worker")
    handle = lab.native.open(request)
    with pytest.raises(ValueError, match="native_product_original_prompt_not_released"):
        lab.native.prompt(handle, worker_input("source"), uuid4())
    assert not lab.native.read_events(handle, 0)
    assert not (module.Path(request.workspace.directory) / "native_value.py").exists()


@pytest.mark.parametrize("kind", ["cancel", "original_exit"])
def test_source_gate_known_original_cancel_or_exit_releases_prompt(module, lab, kind):
    request = lab.opened("worker")
    handle, response = lab.native.open(request), uuid4()
    with ThreadPoolExecutor(max_workers=1) as pool:
        prompt = pool.submit(
            lab.native.prompt, handle, worker_input("source"), response
        )
        assert lab.native.source_entered.wait(1)
        if kind == "cancel":
            assert lab.native.cancel(handle, response).status.value == "confirmed"
        else:
            lab.native.unknown_exits.add(request.run_id)
            assert lab.native.close(handle).status.value == "unknown"
            assert lab.native.confirm_original_exit(request.run_id)
        assert prompt.result(1).status.value == "cancelled"
    assert not (module.Path(request.workspace.directory) / "native_value.py").exists()


def test_original_unknown_close_confirmation_does_not_release_other_run(module, lab):
    original, other = lab.opened("worker"), lab.opened("planner")
    a, b = lab.native.open(original), lab.native.open(other)
    lab.native.unknown_exits.add(a.run_id)
    assert lab.native.close(a).status.value == "unknown"
    assert not lab.native.confirm_original_exit(uuid4())
    assert lab.native.confirm_original_exit(a.run_id)
    assert lab.native.inspect(a).process_state.value == "gone"
    assert lab.native.inspect(b).process_state.value == "alive"
    assert lab.native.close(a).status.value == "closed"


def test_planner_emits_actual_schema_six_dependency_chain(module, lab):
    handle = lab.native.open(lab.opened("planner"))
    lab.native.prompt(
        handle, AssembledInput(instructions=(), text="plan", context=()), uuid4()
    )
    proposal = json.loads(lab.native.read_events(handle, 0)[0].data["text"])["proposal"]
    assert proposal["schema_version"] == 6
    assert [item["role"] for item in proposal["worker_items"]] == [
        "source",
        "hard-only",
        "integration",
    ]
    assert [item["activation_dependencies"] for item in proposal["execution_intents"]][
        1:
    ] == [
        [
            {
                "upstream_worker_item_key": "source",
                "downstream_worker_item_key": "hard-only",
                "kind": "hard",
                "required_artifact_refs": ["artifact_type:diff"],
            }
        ],
        [
            {
                "upstream_worker_item_key": "hard-only",
                "downstream_worker_item_key": "integration",
                "kind": "hard",
                "required_artifact_refs": ["artifact_type:diff"],
            }
        ],
    ]
    expected_edges = [
        {
            "upstream_worker_item_key": "source",
            "downstream_worker_item_key": "hard-only",
            "kind": "hard",
            "required_artifact_refs": ["artifact_type:diff"],
        },
        {
            "upstream_worker_item_key": "hard-only",
            "downstream_worker_item_key": "integration",
            "kind": "hard",
            "required_artifact_refs": ["artifact_type:diff"],
        },
    ]
    assert proposal["worker_item_dependencies"] == expected_edges
    assert proposal["integration_spec"]["checks"][0]["key"] == "owned-check"


def test_planner_dependency_chain_projects_actual_canonical_graph(module):
    proposal = json.loads(module.native_proposal())["proposal"]
    canonical = _plan_worker_dependencies(
        SimpleNamespace(
            worker_item_dependencies=tuple(
                SimpleNamespace(
                    upstream_worker_item_key=edge["upstream_worker_item_key"],
                    downstream_worker_item_key=edge["downstream_worker_item_key"],
                    kind=edge["kind"],
                    required_artifact_refs=tuple(edge["required_artifact_refs"]),
                )
                for edge in proposal["worker_item_dependencies"]
            )
        )
    )
    assert [
        (
            edge.upstream_worker_item_key,
            edge.downstream_worker_item_key,
            edge.kind,
            edge.required_artifact_refs,
        )
        for edge in canonical
    ] == [
        ("source", "hard-only", "dependency", ("artifact_type:diff",)),
        ("hard-only", "integration", "dependency", ("artifact_type:diff",)),
    ]
    source, hard_only, integration = uuid4(), uuid4(), uuid4()
    graph = _worker_graph(
        SimpleNamespace(worker_item_dependencies=canonical),
        {
            "source": SimpleNamespace(worker_item_id=source),
            "hard-only": SimpleNamespace(worker_item_id=hard_only),
            "integration": SimpleNamespace(worker_item_id=integration),
        },
    )
    assert graph.worker_item_ids == (source, hard_only, integration)
    assert [
        (edge.upstream_worker_item_id, edge.downstream_worker_item_id, edge.kind)
        for edge in graph.edges
    ] == [
        (source, hard_only, "dependency"),
        (hard_only, integration, "dependency"),
    ]


def test_public_worker_facts_exclude_authority_and_private_context_bodies(module, lab):
    lab.native.worker_facts["source"] = {
        "execution": {"id": str(uuid4()), "lineage_id": str(uuid4()), "attempt": 1},
        "execution_request": {"id": str(uuid4())},
        "worker_item": {"id": str(uuid4())},
        "private_authority": "PRIVATE-GRANT-BODY",
        "instruction": "PRIVATE-PROMPT-BODY",
    }
    facts = lab.native.public_facts()
    assert (
        facts["worker_facts"]["source"]["execution"]
        == lab.native.worker_facts["source"]["execution"]
    )
    assert "PRIVATE-" not in json.dumps(facts)


def test_main_repeated_response_keeps_goal_request_identity(module, lab, monkeypatch):
    observed = []
    root, planning = uuid4(), uuid4()

    def call(connection, name, arguments):
        observed.append(arguments)
        return {
            "outcome": "applied",
            "command_id": str(uuid4()),
            "root_task_id": str(root),
            "planning_request_id": str(planning),
        }

    monkeypatch.setattr(module, "sdk_call", call)
    handle = lab.native.open(lab.opened("main"))
    assembled = AssembledInput(instructions=(), text="build", context=())
    response = uuid4()
    lab.native.prompt(handle, assembled, response)
    lab.native.prompt(handle, assembled, response)
    assert observed[0]["request_id"] == observed[1]["request_id"]
    assert lab.native.public_facts()["goal"]["root_task_id"] == str(root)


@pytest.mark.parametrize("case", ["missing", "hash", "origin", "role"])
def test_invalid_worker_context_refuses_output(module, lab, case):
    request = lab.opened("worker")
    handle = lab.native.open(request)
    assembled = worker_input("hard-only")
    if case == "missing":
        assembled = assembled.model_copy(update={"context": ()})
    elif case in ("hash", "origin"):
        item = assembled.context[0].model_copy(
            update={"content_hash": "sha256:" + "0" * 64}
            if case == "hash"
            else {"origin": "results"}
        )
        assembled = assembled.model_copy(
            update={"context": (item, *assembled.context[1:])}
        )
    else:
        assembled = assembled.model_copy(
            update={"instructions": (json.dumps({"role": "other"}),)}
        )
    with pytest.raises((ValueError, AssertionError)):
        lab.native.prompt(handle, assembled, uuid4())
    assert not (module.Path(request.workspace.directory) / "hard_value.py").exists()


def git(directory, *arguments):
    return subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "user.name=Owned unit",
            "-c",
            "user.email=owned-unit@example.invalid",
            *arguments,
        ],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


@pytest.mark.parametrize(
    "case", ["valid", "payload", "code_hash", "physical", "lineage"]
)
def test_hard_input_consumes_preinstalled_physical_state_without_applying_patch(
    module, lab, case
):
    upstream = worker_input("source")
    original = json.loads(upstream.context[1].text)
    lab.native.worker_facts["source"] = original
    request = lab.opened("worker")
    directory = module.Path(request.workspace.directory)
    git(directory, "init")
    (directory / "native_value.py").write_text("VALUE = 42\n")
    git(directory, "add", "native_value.py")
    git(directory, "commit", "-m", "Preinstalled authorized physical input")
    commit, tree = (
        git(directory, "rev-parse", "HEAD"),
        git(directory, "rev-parse", "HEAD^{tree}"),
    )
    body = "diff --git a/native_value.py b/native_value.py\n+VALUE = 42\n"
    source = {
        "worker_item_key": "source",
        "worker_item_id": original["worker_item"]["id"],
        "subtask_path": ["subtask-one"],
        "effective_request_id": original["execution_request"]["id"],
        "allowed_lineage_ids": [original["execution"]["lineage_id"]],
        "effective_lineage_id": original["execution"]["lineage_id"],
        "effective_execution_id": original["execution"]["id"],
        "attempt": 1,
        "inheritance_refs": [],
        "priority": 1,
        "source_basis_hash": "a" * 64,
    }
    hard = "request:" + source["effective_request_id"]
    artifact = {
        "artifact_id": str(uuid4()),
        "artifact_type": "diff",
        "content_hash": hashlib.sha256(body.encode()).hexdigest(),
        "size_bytes": len(body.encode()),
        "media_type": "text/x-diff",
        "sources": [source],
        "group_keys": [],
        "hard_refs": [hard],
    }
    selection = uuid4()
    manifest = ResultInputManifestV1.from_mapping(
        {
            "schema_version": 1,
            "input_selection_id": str(selection),
            "input_selection_hash": "b" * 64,
            "delivery_mode": "bounded_text_v1",
            "groups": [],
            "hard_inputs": [{"dependency_key": hard, "rule_hash": "c" * 64}],
            "artifacts": [artifact],
            "evidence": [],
            "budget": {
                "schema_version": 1,
                "max_artifact_count": 128,
                "max_total_artifact_bytes": 131072,
                "max_artifact_bytes": 32768,
                "max_payload_bytes": 262144,
            },
        }
    )
    payload = {k: v for k, v in artifact.items() if k != "artifact_type"}
    payload.update(body_text=body, candidate_evidence=[])
    assembled = worker_input("hard-only")
    facts = json.loads(assembled.context[1].text)
    code = {
        "schema_version": 1,
        "workspace_id": str(lab.workspace),
        "root_task_id": str(uuid4()),
        "request_id": facts["execution_request"]["id"],
        "execution_id": facts["execution"]["id"],
        "selection_id": str(selection),
        "selection_hash": "b" * 64,
        "repository_ref": "owned",
        "base_commit": commit,
        "sources": [
            {
                "result_id": str(uuid4()),
                "result_hash": "d" * 64,
                "input_commit": commit,
                "input_tree": tree,
                "result_commit": commit,
                "result_tree": tree,
            }
        ],
    }
    code["input_hash"] = hashlib.sha256(
        json.dumps(code, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if case == "payload":
        payload["body_text"] += "foreign"
    if case == "code_hash":
        code["input_hash"] = "0" * 64
    if case == "physical":
        (directory / "native_value.py").unlink()
    if case == "lineage":
        payload["sources"] = [{**source, "effective_lineage_id": str(uuid4())}]
    assembled = assembled.model_copy(
        update={
            "context": (
                *assembled.context,
                section(
                    "results", {"manifest": manifest.to_mapping(), "inputs": [payload]}
                ),
                section("code", code),
            )
        }
    )
    handle = lab.native.open(request)
    if case == "valid":
        lab.native.prompt(handle, assembled, uuid4())
        assert (
            directory / "hard_value.py"
        ).read_text() == "from native_value import VALUE\nANSWER = VALUE\n"
        assert git(directory, "rev-parse", "HEAD") == commit
        assert (directory / "native_value.py").read_text() == "VALUE = 42\n"
    else:
        with pytest.raises((ValueError, AssertionError)):
            lab.native.prompt(handle, assembled, uuid4())
        assert not (directory / "hard_value.py").exists()
