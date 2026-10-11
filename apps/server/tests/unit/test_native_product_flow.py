import ast
import hashlib
import importlib
import json
import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import httpx
import pytest


def flow_module():
    return importlib.import_module("tests.support.native_product_flow")


def source_projection(workspace=None, *, version=3, cursor=2):
    ws = workspace or str(uuid4())
    item, execution, run, session, worker = map(str, [uuid4() for _ in range(5)])
    profile = {"workspace_id": ws, "profile_id": str(uuid4()), "revision": 1}
    now = "2026-10-08T00:00:00Z"
    address = {
        "worker_item_id": item,
        "plan_generation": 1,
        "context_version": 1,
        "execution_id": execution,
        "session_id": session,
        "session_version": version,
        "worker_id": worker,
        "execution_mode": "native_profile",
        "binding_version": 1,
        "run_id": run,
    }
    view = {
        "workspace_id": ws,
        "worker_item_id": item,
        "execution_mode": "native_profile",
        "worker_item_version": 1,
        "binding": None,
        "execution": {
            "workspace_id": ws,
            "execution_id": execution,
            "root_task_id": str(uuid4()),
            "subtask_id": str(uuid4()),
            "worker_item_id": item,
            "status": "running",
            "updated_at": now,
            "profile_ref": profile,
            "binding_version": 1,
            "run_id": run,
            "link_status": "running",
            "event_cursor": cursor,
        },
        "execution_version": 2,
        "context": address,
        "input_enabled": True,
        "read_only_reason": None,
        "run": {
            "state": "running",
            "input_open": True,
            "profile_ref": profile,
            "config_hash": "owned-config",
            "runtime_ref": {"workspace_id": ws, "runtime_id": str(uuid4())},
            "actual_runtime_version": "controlled",
            "selected_model": None,
            "observed_model": None,
            "applied_fields": [],
            "reason_code": None,
        },
        "as_of": now,
    }
    trace = {
        "workspace_id": ws,
        "worker_item_id": item,
        "execution_id": execution,
        "run_id": run,
        "profile_ref": profile,
        "events": [
            {
                "run_id": run,
                "sequence": sequence,
                "occurred_at": now,
                "kind": kind,
                "operation_id": None,
                "response_id": str(uuid4()) if kind == "response.started" else None,
                "data": {},
            }
            for sequence, kind in (
                (cursor - 1, "run.started"),
                (cursor, "response.started"),
            )
        ],
        "next_sequence": cursor,
        "has_more": False,
    }
    sessions = {
        "next_cursor": None,
        "items": [
            {
                "id": session,
                "workspace_id": ws,
                "execution_id": execution,
                "worker_id": worker,
                "adapter_session_id": "agent-run:" + run,
                "status": "active",
                "last_sequence": cursor,
                "last_activity_at": now,
                "input_mode": "message",
                "adapter_module_id": str(uuid4()),
                "adapter_config_version": 1,
                "workspace_module_id": str(uuid4()),
                "workspace_config_version": 1,
                "capability_snapshot": {},
                "workspace_ref": None,
                "version": version,
                "created_at": now,
                "updated_at": now,
            }
        ],
    }
    return {"view": view, "trace": trace, "sessions": sessions}


@pytest.fixture
def source_context_client(no_database_connections):
    module = flow_module()
    data = source_projection()
    address = data["view"]["context"]
    entered = Event()
    entered.set()
    requests, attempts = [], []
    case = SimpleNamespace(data=data, contexts=[], context_reads=0)
    client = module.NativeProductClient.__new__(module.NativeProductClient)
    client.lab = SimpleNamespace(
        workspace_id=UUID(data["view"]["workspace_id"]),
        native=SimpleNamespace(
            source_entered=entered, original_run_id=UUID(address["run_id"])
        ),
        record=lambda *_: pytest.fail("Readiness issued a command or evidence write"),
    )
    client.prefix = f"/api/v1/workspaces/{client.lab.workspace_id}/"

    def handle(request):
        assert entered.is_set()
        assert request.method == "GET"
        requests.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/native-context"):
            index = case.context_reads
            case.context_reads += 1
            view = (
                case.contexts[min(index, len(case.contexts) - 1)]
                if case.contexts
                else case.data["view"]
            )
            return httpx.Response(200, json=deepcopy(view))
        if request.url.path.endswith("/native-trace"):
            assert (
                request.url.path
                == client.prefix
                + f"worker-items/{address['worker_item_id']}/executions/{address['execution_id']}/native-trace"
            )
            assert dict(request.url.params) == {"after_sequence": "0", "limit": "100"}
            return httpx.Response(200, json=deepcopy(case.data["trace"]))
        assert request.url.path == "/api/v1/gateway/sessions"
        assert dict(request.url.params) == {
            "workspace_id": str(client.lab.workspace_id),
            "session_id": address["session_id"],
            "limit": "1",
        }
        return httpx.Response(200, json=deepcopy(case.data["sessions"]))

    def wait(read, ready, stage, **kwargs):
        for _ in range(3):
            value = read()
            attempts.append((stage, ready(value)))
            if ready(value):
                return value
        raise AssertionError("readiness_not_ready")

    client.wait = wait
    client.client = httpx.Client(
        base_url="http://127.0.0.1:1", transport=httpx.MockTransport(handle)
    )
    case.client, case.entered, case.requests, case.attempts = (
        client,
        entered,
        requests,
        attempts,
    )
    yield case
    client.client.close()


@pytest.mark.parametrize(("before", "current", "cursor"), [(2, 3, 2), (17, 18, 9)])
def test_source_context_waits_for_current_session_projection_before_returning(
    source_context_client, before, current, cursor
):
    case = source_context_client
    case.data["view"]["context"]["session_version"] = current
    case.data["sessions"]["items"][0].update(version=current, last_sequence=cursor)
    case.data["trace"]["next_sequence"] = cursor
    for sequence, event in zip(
        (cursor - 1, cursor), case.data["trace"]["events"], strict=True
    ):
        event["sequence"] = sequence
    old = deepcopy(case.data["view"])
    old["context"]["session_version"] = before
    case.contexts = [old, case.data["view"]]

    result = case.client.wait_for_source_context(case.data["view"]["worker_item_id"])

    assert result == case.data["view"]
    assert result["context"]["session_version"] == current
    assert case.context_reads == 4
    assert [
        ready for stage, ready in case.attempts if stage != "source-prompt-entered"
    ] == [False, True]
    assert [path.rsplit("/", 1)[-1] for path, _ in case.requests] == [
        "native-context",
        "native-trace",
        "sessions",
        "native-context",
        "native-context",
        "native-trace",
        "sessions",
        "native-context",
    ]


@pytest.mark.parametrize(
    "bad",
    (
        "behind_cursor",
        "missing_response",
        "has_more",
        "zero_cursor",
        "inactive_session",
        "disabled_input",
        "disabled_reread",
        "changed_run",
        "changed_address",
        "stale_session",
        "context_workspace",
        "context_worker",
        "context_execution",
        "context_run",
        "execution_workspace",
        "execution_worker",
        "execution_run",
        "trace_workspace",
        "trace_worker",
        "trace_execution",
        "trace_run",
        "trace_event_run",
        "session_workspace",
        "session_execution",
        "session_id",
        "session_worker",
        "session_run",
    ),
)
def test_source_context_never_admits_unready_or_foreign_projection(
    source_context_client, bad
):
    case = source_context_client
    case.contexts = corrupt_source_projection(case.data, bad)
    with pytest.raises(AssertionError, match="readiness_not_ready"):
        case.client.wait_for_source_context(
            case.data["view"]["context"]["worker_item_id"]
        )
    assert not any(
        ready for stage, ready in case.attempts if stage != "source-prompt-entered"
    )


def corrupt_source_projection(data, bad):
    view, trace, session = data["view"], data["trace"], data["sessions"]["items"][0]
    contexts = []
    if bad == "behind_cursor":
        session["last_sequence"] = 1
    elif bad == "missing_response":
        trace["events"][-1]["kind"] = "run.waiting"
    elif bad == "has_more":
        trace["has_more"] = True
    elif bad == "zero_cursor":
        trace.update(next_sequence=0, events=[])
    elif bad == "inactive_session":
        session["status"] = "closed"
    elif bad == "disabled_input":
        view["input_enabled"] = False
    elif bad in {"disabled_reread", "changed_run", "changed_address"}:
        changed = deepcopy(view)
        if bad == "disabled_reread":
            changed["input_enabled"] = False
        elif bad == "changed_run":
            changed["context"]["run_id"] = changed["execution"]["run_id"] = str(uuid4())
        else:
            changed["context"]["context_version"] += 1
        contexts = [view, changed] * 3
    elif bad == "stale_session":
        session["version"] = 2
    elif bad.startswith("context_"):
        key = {
            "context_workspace": "workspace_id",
            "context_worker": "worker_item_id",
        }.get(bad)
        if key:
            view[key] = str(uuid4())
        else:
            view["context"][
                "execution_id" if bad == "context_execution" else "run_id"
            ] = str(uuid4())
    elif bad.startswith("execution_"):
        view["execution"][
            {
                "execution_workspace": "workspace_id",
                "execution_worker": "worker_item_id",
                "execution_run": "run_id",
            }[bad]
        ] = str(uuid4())
    elif bad == "trace_event_run":
        trace["events"][-1]["run_id"] = str(uuid4())
    elif bad.startswith("trace_"):
        trace[
            {
                "trace_workspace": "workspace_id",
                "trace_worker": "worker_item_id",
                "trace_execution": "execution_id",
                "trace_run": "run_id",
            }[bad]
        ] = str(uuid4())
    else:
        session[
            {
                "session_workspace": "workspace_id",
                "session_execution": "execution_id",
                "session_id": "id",
                "session_worker": "worker_id",
                "session_run": "adapter_session_id",
            }[bad]
        ] = str(uuid4())

    return contexts


def test_source_context_requires_held_source_entry_before_public_reads(
    source_context_client,
):
    case = source_context_client
    case.entered.clear()
    with pytest.raises(AssertionError, match="readiness_not_ready"):
        case.client.wait_for_source_context(case.data["view"]["worker_item_id"])
    assert case.requests == []


@pytest.fixture
def configuration_client(tmp_path, no_database_connections):
    module = flow_module()
    workspace = str(uuid4())
    runtime_ref = {"workspace_id": workspace, "runtime_id": str(uuid4())}
    resource_ref = {
        "workspace_id": workspace,
        "resource_id": str(uuid4()),
        "revision": 1,
    }
    profile_ref = {"workspace_id": workspace, "profile_id": str(uuid4()), "revision": 1}
    metadata = {
        "workspace_id": workspace,
        "connection_id": str(uuid4()),
        "name": "SIDERA Task Tools",
        "availability": "configured",
    }
    records, requests, facts, intents_seen = [], [], {}, []
    lab = SimpleNamespace(
        workspace_id=UUID(workspace),
        repository=tmp_path / "repository",
        native=SimpleNamespace(harness_id="controlled-native-product-flow"),
        facts=facts,
        metadata=[metadata],
        corrupt=None,
    )

    def record(stage, value):
        records.append((stage, deepcopy(value)))
        facts[stage] = value

    lab.record = record

    def handle(request):
        assert request.headers["Origin"] == "http://127.0.0.1:1"
        assert request.headers["X-Sidera-CSRF"] == "owned-csrf"
        assert request.headers["Cookie"] == "sidera_session=owned-session"
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/tool-connections"):
            return httpx.Response(200, json=lab.metadata)
        if request.url.path.endswith("/runtimes"):
            return httpx.Response(201, json={"ref": runtime_ref})
        if request.url.path.endswith(("/resources", "/profiles")):
            field = (
                "resource_id"
                if request.url.path.endswith("/resources")
                else "profile_id"
            )
            ref = resource_ref if field == "resource_id" else profile_ref
            intents_seen.append(
                any(
                    value.get("operation_id") == body["operationId"]
                    for _, value in records
                )
            )
            result = {
                "operationId": body["operationId"],
                "state": "succeeded",
                "target_ref": deepcopy(ref),
                "result_ref": deepcopy(ref),
                "reason_code": None,
            }
            if lab.corrupt == field:
                result["result_ref"][field] = str(uuid4())
            if lab.corrupt == field + "_operation":
                result["operationId"] = str(uuid4())
            if lab.corrupt == field + "_revision":
                result["result_ref"]["revision"] = 2
                result["target_ref"]["revision"] = 2
            return httpx.Response(201, json=result)
        if request.url.path.endswith(("/defaults", "/setup")):
            return httpx.Response(200, json=body)
        raise AssertionError("Unexpected configuration request: " + request.url.path)

    client = module.NativeProductClient.__new__(module.NativeProductClient)
    client.lab = lab
    client.client = httpx.Client(
        base_url="http://127.0.0.1:1",
        transport=httpx.MockTransport(handle),
        headers={"Origin": "http://127.0.0.1:1", "X-Sidera-CSRF": "owned-csrf"},
        cookies={"sidera_session": "owned-session"},
    )
    yield SimpleNamespace(
        client=client,
        lab=lab,
        metadata=metadata,
        requests=requests,
        records=records,
        intents_seen=intents_seen,
        resource_ref=resource_ref,
        profile_ref=profile_ref,
    )
    client.client.close()


def test_api_configuration_binds_the_advertised_public_mcp_resource_before_profile_defaults_and_setup(
    configuration_client,
):
    case = configuration_client
    profile, setup = case.client.configure()
    creates = [
        (path, body)
        for method, path, body in case.requests
        if method == "POST" and path.endswith(("/resources", "/profiles"))
    ]
    assert [path.rsplit("/", 1)[-1] for path, _ in creates] == ["resources", "profiles"]
    assert case.intents_seen == [True, True]
    assert creates[0][1]["draft"] == {
        "name": "SIDERA Task Tools",
        "kind": "mcp",
        "settings": {"connection_id": case.metadata["connection_id"]},
        "secret_refs": [],
    }
    assert creates[1][1]["draft"]["resource_refs"] == [case.resource_ref]
    assert profile == case.profile_ref and setup["planner_agent_ref"] == profile
    configuration = case.lab.facts["configuration"]
    assert (
        configuration["task_tool_resource_operation_id"] == creates[0][1]["operationId"]
    )
    assert configuration["task_tool_resource_ref"] == case.resource_ref
    assert configuration["task_tool_connection_id"] == case.metadata["connection_id"]
    assert configuration["configuration_operation_id"] == creates[1][1]["operationId"]
    assert configuration["profile_create_ref"] == profile
    assert configuration["profile_create_operation_id"] == creates[1][1]["operationId"]
    defaults = next(
        body
        for method, path, body in case.requests
        if method == "PUT" and path.endswith("/defaults")
    )
    assert [
        defaults[key]
        for key in ("main_profile_id", "planner_profile_id", "worker_profile_id")
    ] == [profile["profile_id"]] * 3


@pytest.mark.parametrize(
    "case", ["missing", "foreign", "unavailable", "name", "duplicate"]
)
def test_api_configuration_refuses_unadvertised_or_ambiguous_product_connection_before_writes(
    configuration_client, case
):
    lab = configuration_client.lab
    if case == "missing":
        lab.metadata = []
    elif case == "duplicate":
        lab.metadata *= 2
    else:
        lab.metadata[0] = {
            **lab.metadata[0],
            **(
                {"workspace_id": str(uuid4())}
                if case == "foreign"
                else {"availability": "unavailable"}
                if case == "unavailable"
                else {"name": "Pretend provider"}
            ),
        }
    with pytest.raises(AssertionError):
        configuration_client.client.configure()
    assert all(method == "GET" for method, _, _ in configuration_client.requests)


@pytest.mark.parametrize(
    "corrupt",
    [
        "resource_id",
        "resource_id_operation",
        "resource_id_revision",
        "profile_id",
        "profile_id_operation",
        "profile_id_revision",
    ],
)
def test_api_configuration_refuses_changed_original_result_before_defaults_or_setup(
    configuration_client, corrupt
):
    case = configuration_client
    case.lab.corrupt = corrupt
    with pytest.raises(AssertionError):
        case.client.configure()
    assert not any(
        path.endswith(("/defaults", "/setup")) for _, path, _ in case.requests
    )


@pytest.mark.parametrize("scenario", ["main", "transition"])
def test_native_lab_scenario_is_explicit_and_default_is_main(tmp_path, scenario):
    module = flow_module()
    args = {
        "targets": (),
        "guards": (),
        "directory": tmp_path,
        "evidence": tmp_path,
        "start_web": False,
    }
    assert module.NativeProductFlowLab(**args).scenario == "main"
    assert module.NativeProductFlowLab(**args, scenario=scenario).scenario == scenario


@pytest.mark.parametrize("scenario", [None, True, "", "provider", "Main"])
def test_native_lab_scenario_refused_before_any_resource_check(
    tmp_path, monkeypatch, scenario
):
    module = flow_module()
    monkeypatch.setattr(
        module,
        "target_environment",
        lambda *_: pytest.fail("target resource checked for invalid scenario"),
    )
    with pytest.raises(module.ProductTargetError, match="native_scenario_refused"):
        module.build_native_product_flow_lab(
            (), (), tmp_path, tmp_path, start_web=False, scenario=scenario
        )
    assert list(tmp_path.iterdir()) == []


class Guard:
    def __init__(self, component, calls, *, idle=True, released=True):
        self.target = SimpleNamespace(component=component)
        self.calls, self.idle, self.released = calls, idle, released
        self.attempted = False

    def inspect(self):
        assert not self.attempted, "Closed guard connection queried"
        self.calls.append(("inspect", self.target.component))
        return {
            "idle": {
                "other_clients": 0 if self.idle else 1,
                "other_advisory_locks": 0,
                "prepared_transactions": 0,
            }
        }

    def release(self):
        assert not self.attempted, "Guard release attempted twice"
        self.attempted = True
        self.calls.append(("release", self.target.component))
        return self.released


def lab_case(
    tmp_path, *, owns_guards=False, runtime_closes=True, idle=True, released=True
):
    module = flow_module()
    calls = []
    guards = [
        Guard(key, calls, idle=idle, released=released)
        for key in ("product", "agents", "cp", "credentials")
    ]
    lab = module.NativeProductFlowLab(
        targets=(),
        guards=guards,
        directory=tmp_path,
        evidence=tmp_path,
        start_web=False,
        owns_guards=owns_guards,
    )

    def close_runtime(**kwargs):
        calls.append(("runtime", None))
        return runtime_closes

    lab.runtime = SimpleNamespace(close=close_runtime)
    lab.backend = SimpleNamespace(close=lambda: calls.append(("backend", None)) or True)
    lab.knowledge = SimpleNamespace(
        close=lambda: calls.append(("knowledge", None)) or True,
        facts=lambda: {"closed": True},
    )
    lab.controller = SimpleNamespace(
        close=lambda: calls.append(("controller", None)) or True
    )
    lab.bootstrap_export = SimpleNamespace(
        close=lambda: calls.append(("bootstrap", None)) or True
    )
    return lab, calls


def test_borrowed_guards_are_never_inspected_or_released_by_lab_close(tmp_path):
    lab, calls = lab_case(tmp_path)
    assert lab.close()
    assert not any(name in {"inspect", "release"} for name, _ in calls)
    facts = lab.stage_facts()
    assert facts["guard_custody"] == "caller"
    assert facts["cleanup"]["guards_released"] is None
    assert lab.close()
    assert calls.count(("runtime", None)) == 1


def test_transferred_guards_release_once_after_owned_resources_close(tmp_path):
    lab, calls = lab_case(tmp_path, owns_guards=True)
    assert lab.close()
    assert calls.index(("inspect", "product")) > calls.index(("controller", None))
    assert [component for name, component in calls if name == "release"] == [
        "credentials",
        "cp",
        "agents",
        "product",
    ]
    assert lab.stage_facts()["cleanup"]["guards_released"] is True
    saved = list(calls)
    assert lab.close()
    assert calls == saved


def test_runtime_cleanup_failure_retains_bridge_and_guards(tmp_path):
    lab, calls = lab_case(tmp_path, owns_guards=True, runtime_closes=False)
    runtime, controller = lab.runtime, lab.controller
    assert not lab.close()
    assert lab.runtime is runtime and lab.controller is controller
    assert calls == [("runtime", None)]
    assert lab.stage_facts()["cleanup"]["guards_released"] is None


def test_nonidle_guard_refuses_all_release_after_lab_close(tmp_path):
    lab, calls = lab_case(tmp_path, owns_guards=True, idle=False)
    assert not lab.close()
    assert not any(name == "release" for name, _ in calls)
    assert lab.stage_facts()["cleanup"]["guards_inspected"] is False


def test_failed_guard_release_is_retained_without_requerying_closed_connection(
    tmp_path,
):
    lab, calls = lab_case(tmp_path, owns_guards=True, released=False)
    assert not lab.close()
    saved = list(calls)
    assert not lab.close()
    assert calls == saved
    assert lab.stage_facts()["cleanup"]["guards_released"] is False


def cleanup_lab(tmp_path, **options):
    directory = tmp_path / "owned-cleanup"
    directory.mkdir(mode=0o700)
    (directory / "cleanup").mkdir(mode=0o700)
    lab, calls = lab_case(directory, **options)
    lab.manifest = {
        "schema_version": 1,
        "owner_uid": os.getuid(),
        "allocation_id": "sidera-native-harness:product:n17:20261007:cleanupunit",
        "workspace_id": str(lab.workspace_id),
        "namespace": str(lab.namespace),
        "config_hash": "a" * 64,
        "owner_receipt_path": str(directory / "owner.json"),
        "process_identities": {"owner_pid": os.getpid()},
    }
    reference = {
        key: lab.manifest[key]
        for key in (
            "schema_version",
            "owner_uid",
            "allocation_id",
            "workspace_id",
            "namespace",
            "config_hash",
        )
    }
    reference.update(
        owner_pid=os.getpid(),
        operation_id=str(uuid5(UUID(lab.manifest["namespace"]), "owned_cleanup")),
    )
    path = directory / "owner-cleanup-request.json"
    lab.facts["functional_accepted"] = {"original_candidate": str(uuid4())}
    return lab, calls, reference, path


def write_cleanup_request(path, reference):
    path.write_text(json.dumps(reference))
    path.chmod(0o600)


def test_owner_cleanup_request_absent_does_not_attempt_closure(tmp_path):
    lab, calls, _, _ = cleanup_lab(tmp_path)
    assert lab.service_cleanup_request() is None
    assert calls == []
    assert list((lab.directory / "cleanup").iterdir()) == []


@pytest.mark.parametrize("owns_guards", [False, True])
def test_owner_cleanup_request_closes_actual_handles_once_and_proves_custody(
    tmp_path, owns_guards
):
    lab, calls, reference, path = cleanup_lab(tmp_path, owns_guards=owns_guards)
    original = deepcopy(lab.facts)
    write_cleanup_request(path, reference)
    assert lab.service_cleanup_request() is True
    assert [name for name, _ in calls[:5]] == [
        "runtime",
        "backend",
        "bootstrap",
        "knowledge",
        "controller",
    ]
    proof_paths = list((lab.directory / "cleanup").iterdir())
    assert len(proof_paths) == 1
    proof = json.loads(proof_paths[0].read_text())
    assert {key: proof[key] for key in reference} == reference
    assert proof["complete"] is True
    assert all(
        proof["cleanup"][name + "_closed"] is True
        for name in (
            "runtime",
            "native",
            "backend",
            "listener",
            "vite",
            "bootstrap",
            "knowledge",
            "controller",
        )
    )
    assert (proof_paths[0].stat().st_mode & 0o777) == 0o600
    if owns_guards:
        assert calls.index(("inspect", "product")) > calls.index(("controller", None))
        assert [name for kind, name in calls if kind == "release"] == [
            "credentials",
            "cp",
            "agents",
            "product",
        ]
    else:
        assert not any(kind in {"inspect", "release"} for kind, _ in calls)
        assert (
            proof["cleanup"]["guards_inspected"]
            is proof["cleanup"]["guards_released"]
            is None
        )
    saved = list(calls)
    assert lab.service_cleanup_request() is True
    assert calls == saved
    assert list((lab.directory / "cleanup").iterdir()) == proof_paths
    assert lab.facts == original


@pytest.mark.parametrize("failure", ["runtime", "idle", "release"])
def test_owner_cleanup_request_incomplete_retains_custody_without_blind_retry(
    tmp_path, failure
):
    lab, calls, reference, path = cleanup_lab(
        tmp_path,
        owns_guards=True,
        runtime_closes=failure != "runtime",
        idle=failure != "idle",
        released=failure != "release",
    )
    runtime, controller, original = lab.runtime, lab.controller, deepcopy(lab.facts)
    write_cleanup_request(path, reference)
    assert lab.service_cleanup_request() is False
    saved = list(calls)
    assert lab.service_cleanup_request() is False
    assert calls == saved
    assert lab.runtime is runtime and lab.controller is controller
    assert any(owner is lab for owner in flow_module()._retained_native_labs)
    proofs = list((lab.directory / "cleanup").iterdir())
    assert len(proofs) == 1
    proof = json.loads(proofs[0].read_text())
    assert proof["complete"] is False
    assert {key: proof[key] for key in reference} == reference
    assert lab.facts == original
    if failure == "runtime":
        assert calls == [("runtime", None)]
    if failure != "release":
        assert not any(kind == "release" for kind, _ in calls)


@pytest.mark.parametrize(
    "case",
    [
        "schema_version",
        "owner_uid",
        "allocation_id",
        "workspace_id",
        "namespace",
        "config_hash",
        "owner_pid",
        "operation_id",
        "extra",
        "schema_bool",
        "uid_bool",
        "pid_bool",
        "permissive",
        "parent_mode",
        "symlink",
        "malformed",
        "oversize",
    ],
)
def test_owner_cleanup_request_refuses_foreign_or_unsafe_reference_before_close(
    tmp_path, case
):
    from tests.support.product_targets import ProductTargetError

    lab, calls, reference, path = cleanup_lab(tmp_path, owns_guards=True)
    value = deepcopy(reference)
    if case in reference:
        value[case] = value[case] + 1 if type(value[case]) is int else str(uuid4())
    elif case == "extra":
        value["argv"] = ["/usr/bin/true"]
    elif case.endswith("_bool"):
        value[
            {
                "schema_bool": "schema_version",
                "uid_bool": "owner_uid",
                "pid_bool": "owner_pid",
            }[case]
        ] = True
    write_cleanup_request(path, value)
    if case == "permissive":
        path.chmod(0o644)
    elif case == "parent_mode":
        path.parent.chmod(0o755)
    elif case == "symlink":
        target = path.parent / "original.json"
        path.rename(target)
        path.symlink_to(target)
    elif case == "malformed":
        path.write_bytes(b"PRIVATE malformed reference")
    elif case == "oversize":
        path.write_bytes(b" " * 2097153)
    with pytest.raises(ProductTargetError, match="native_cleanup_request_refused"):
        lab.service_cleanup_request()
    assert calls == []
    assert lab.cleanup["guards_released"] is None
    assert list((lab.directory / "cleanup").iterdir()) == []


def test_owner_cleanup_request_cannot_close_on_another_thread(tmp_path):
    from tests.support.product_targets import ProductTargetError

    lab, calls, reference, path = cleanup_lab(tmp_path)
    write_cleanup_request(path, reference)
    observed = []

    def foreign():
        try:
            lab.service_cleanup_request()
        except ProductTargetError as error:
            observed.append(str(error))

    thread = Thread(target=foreign)
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert observed == ["native_cleanup_owner_thread_refused"]
    assert calls == []


@pytest.mark.parametrize(
    "case",
    ["missing_token", "foreign_workspace", "foreign_run", "unknown_input", "valid"],
)
def test_control_bridge_only_releases_exact_original_accepted_prompt(tmp_path, case):
    module = flow_module()
    ws, run, op = uuid4(), uuid4(), uuid4()
    changed = []

    def release(selected, operation):
        if selected == run and operation == op:
            changed.append((selected, operation))
            return True
        return False

    native = SimpleNamespace(
        original_run_id=run,
        release_accepted_prompt=release,
        confirm_original_exit=lambda selected: False,
    )
    lab = SimpleNamespace(
        workspace_id=ws,
        native=native,
        stage_facts=lambda: {"workspace_id": str(ws), "original_run_id": str(run)},
    )
    server = module.NativeControlBridge(lab)
    server.start()
    try:
        headers = {
            "Authorization": "Bearer " + server.token.get_secret_value(),
            "X-Sidera-Native-Workspace": str(ws),
        }
        if case == "missing_token":
            headers.pop("Authorization")
        if case == "foreign_workspace":
            headers["X-Sidera-Native-Workspace"] = str(uuid4())
        body = {
            "run_id": str(uuid4() if case == "foreign_run" else run),
            "input_operation_id": str(uuid4() if case == "unknown_input" else op),
        }
        with httpx.Client(base_url=server.url, trust_env=False, timeout=2) as client:
            response = client.post("/release-prompt", headers=headers, json=body)
            assert (
                response.status_code
                == {
                    "missing_token": 401,
                    "foreign_workspace": 403,
                    "foreign_run": 404,
                    "unknown_input": 409,
                    "valid": 200,
                }[case]
            )
            if case == "valid":
                assert client.get("/facts", headers=headers).json()[
                    "workspace_id"
                ] == str(ws)
                assert server.token.get_secret_value() not in json.dumps(
                    client.get("/facts", headers=headers).json()
                )
                assert (
                    client.post(
                        "/business-command", headers=headers, json={}
                    ).status_code
                    == 404
                )
                assert (
                    client.post(
                        "/confirm-exit", headers=headers, json={"run_id": str(run)}
                    ).status_code
                    == 409
                )
        assert changed == ([(run, op)] if case == "valid" else [])
    finally:
        assert server.close()


@pytest.fixture
def transition_control_lab():
    from sidera_agents.contracts import NativeInspection, NativeProcessState

    ws, run = uuid4(), uuid4()
    records, observations = [], {}

    def record(name, value):
        records.append((name, value))
        observations[name] = value

    native = SimpleNamespace(
        lock=Lock(),
        original_run_id=run,
        handles={run: SimpleNamespace(run_id=run)},
        roles={run: "worker"},
        inspections={
            run: NativeInspection(
                process_state=NativeProcessState.ALIVE,
                response_state="active",
                reason_code=None,
            )
        },
        close_attempts=set(),
        unknown_exits={uuid4()},
    )
    lab = SimpleNamespace(
        scenario="transition",
        workspace_id=ws,
        native=native,
        record=record,
        stage_facts=lambda: {
            "workspace_id": str(ws),
            "original_run_id": str(run),
            "functional": dict(observations),
        },
    )
    return lab, records


def test_control_bridge_marks_exact_original_live_worker_unknown_once(
    transition_control_lab,
):
    lab, records = transition_control_lab
    native, run = lab.native, lab.native.original_run_id
    before = deepcopy(
        {key: value for key, value in vars(native).items() if key != "lock"}
    )
    server = flow_module().NativeControlBridge(lab)
    server.start()
    try:
        headers = {
            "Authorization": "Bearer " + server.token.get_secret_value(),
            "X-Sidera-Native-Workspace": str(lab.workspace_id),
        }
        with httpx.Client(base_url=server.url, trust_env=False, timeout=2) as client:
            for _ in range(2):
                response = client.post(
                    "/mark-exit-unknown", headers=headers, json={"run_id": str(run)}
                )
                assert response.status_code == 200
                assert response.json() == {"confirmed": True}
                assert native.unknown_exits == before["unknown_exits"] | {run}
                assert records == [
                    ("transition_source_unknown_ready", {"run_id": str(run)})
                ]
                assert server.token.get_secret_value() not in response.text
            facts = client.get("/facts", headers=headers)
            assert facts.status_code == 200
            assert facts.json()["functional"] == {
                "transition_source_unknown_ready": {"run_id": str(run)}
            }
            assert server.token.get_secret_value() not in facts.text
        for key, value in before.items():
            if key != "unknown_exits":
                assert getattr(native, key) == value
    finally:
        assert server.close()


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("missing_token", 401),
        ("foreign_workspace", 403),
        ("main_scenario", 404),
        ("invalid_scenario", 404),
        ("missing_scenario", 404),
        ("foreign_run", 404),
        ("absent_handle", 409),
        ("non_worker_role", 409),
        ("gone_process", 409),
        ("unknown_process", 409),
        ("absent_inspection", 409),
        ("prior_close_attempt", 409),
        ("malformed_json", 400),
        ("extra_field", 400),
        ("missing_run_id", 400),
        ("non_object", 400),
        ("malformed_run_id", 400),
        ("null_run_id", 400),
        ("integer_run_id", 400),
        ("boolean_run_id", 400),
        ("list_run_id", 400),
        ("object_run_id", 400),
        ("oversize_body", 400),
        ("empty_body", 400),
    ],
)
def test_control_bridge_refuses_unknown_exit_without_mutation(
    transition_control_lab, case, status
):
    from sidera_agents.contracts import NativeInspection, NativeProcessState

    lab, records = transition_control_lab
    native, run = lab.native, lab.native.original_run_id
    if case in {"main_scenario", "invalid_scenario"}:
        lab.scenario = "main" if case == "main_scenario" else "invalid"
    elif case == "missing_scenario":
        del lab.scenario
    elif case == "absent_handle":
        native.handles.clear()
    elif case == "non_worker_role":
        native.roles[run] = "main"
    elif case in {"gone_process", "unknown_process"}:
        native.inspections[run] = NativeInspection(
            process_state=(
                NativeProcessState.GONE
                if case == "gone_process"
                else NativeProcessState.UNKNOWN
            ),
            response_state="idle",
            reason_code=None,
        )
    elif case == "absent_inspection":
        native.inspections.clear()
    elif case == "prior_close_attempt":
        native.close_attempts.add(run)
    value = {"run_id": str(uuid4() if case == "foreign_run" else run)}
    if case == "extra_field":
        value["extra"] = True
    elif case == "missing_run_id":
        value = {}
    elif case == "non_object":
        value = []
    elif case.endswith("_run_id"):
        value["run_id"] = {
            "malformed_run_id": "not-a-uuid",
            "null_run_id": None,
            "integer_run_id": 1,
            "boolean_run_id": True,
            "list_run_id": [],
            "object_run_id": {},
        }[case]
    body = {
        "malformed_json": b"{",
        "oversize_body": b" " * 1025,
        "empty_body": b"",
    }.get(case, json.dumps(value).encode())
    before = deepcopy(
        {key: value for key, value in vars(native).items() if key != "lock"}
    )
    original_facts = deepcopy(lab.stage_facts())
    server = flow_module().NativeControlBridge(lab)
    server.start()
    try:
        headers = {
            "Authorization": "Bearer " + server.token.get_secret_value(),
            "X-Sidera-Native-Workspace": str(lab.workspace_id),
        }
        if case == "missing_token":
            headers.pop("Authorization")
        elif case == "foreign_workspace":
            headers["X-Sidera-Native-Workspace"] = str(uuid4())
        with httpx.Client(base_url=server.url, trust_env=False, timeout=2) as client:
            response = client.post("/mark-exit-unknown", headers=headers, content=body)
            assert response.status_code == status
            assert server.token.get_secret_value() not in response.text
        assert records == []
        assert lab.stage_facts() == original_facts
        assert server.token.get_secret_value() not in json.dumps(lab.stage_facts())
        assert {
            key: value for key, value in vars(native).items() if key != "lock"
        } == before
    finally:
        assert server.close()


def test_integration_source_is_readable_without_collecting_db_fixtures():
    path = Path(__file__).parents[1] / "integration" / "test_product_native_flow.py"
    tree = ast.parse(path.read_text())
    assert any(
        isinstance(node, ast.FunctionDef)
        and node.name == "test_complete_native_flow_with_controlled_harness"
        for node in tree.body
    )
    focused = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        == "test_unknown_original_source_handoff_never_admits_target_writer"
    )
    calls = [node for node in ast.walk(focused) if isinstance(node, ast.Call)]
    target_post = next(
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "request"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "POST"
    )
    intent = next(
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "record"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "transition-target-profile-intent"
    )
    assert intent.lineno < target_post.lineno
    assert any(
        isinstance(node, ast.Name) and node.id == "target_profile"
        for node in ast.walk(focused)
    )


EXPECTED_RESULT_BYTES = {
    "native_value.py": b"VALUE = 42\n",
    "hard_value.py": b"from native_value import VALUE\nANSWER = VALUE\n",
    "integration.txt": b"Integrated 42\n",
}


@pytest.fixture
def captured_repository(tmp_path):
    from tests.support.native_product_harness import local_git

    repository = tmp_path / "repository"
    execution = tmp_path / "execution"
    repository.mkdir(mode=0o700)
    local_git(repository, "init", "--initial-branch=owned")
    (repository / "check.py").write_bytes(b"assert True\n")
    local_git(repository, "add", "check.py")
    local_git(repository, "commit", "-m", "Owned baseline")
    head = local_git(repository, "rev-parse", "HEAD")
    local_git(repository, "worktree", "add", "--detach", str(execution), head)
    for name, body in EXPECTED_RESULT_BYTES.items():
        (execution / name).write_bytes(body)
    local_git(execution, "add", *EXPECTED_RESULT_BYTES)
    local_git(execution, "commit", "-m", "Owned captured result")
    commit = local_git(execution, "rev-parse", "HEAD")
    tree = local_git(execution, "rev-parse", "HEAD^{tree}")
    candidate = {
        "candidate_id": str(uuid4()),
        "candidate_hash": "sha256:" + "a" * 64,
        "base_commit": head,
        "result_commit": commit,
        "result_tree": tree,
        "code_result_id": str(uuid4()),
        "integration_execution_id": str(uuid4()),
    }
    check = {
        "verification_id": str(uuid4()),
        "candidate_id": candidate["candidate_id"],
        "check_key": "owned-check",
        "status": "passed",
        "exit_code": 0,
        "actual_commit": commit,
        "actual_tree": tree,
    }
    lab = SimpleNamespace(repository=repository, manifest={"repository_head": head})
    return lab, execution, candidate, [check]


def repository_snapshot(repository):
    from tests.support.native_product_harness import local_git

    return {
        "head": local_git(repository, "rev-parse", "HEAD"),
        "refs": local_git(repository, "show-ref", "--head"),
        "status": local_git(
            repository, "status", "--porcelain=v1", "--untracked-files=all"
        ),
        "files": {
            str(path.relative_to(repository)): path.read_bytes()
            for path in repository.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(repository).parts
        },
    }


def test_result_repository_proves_immutable_blobs_without_applying_checkout(
    captured_repository,
):
    lab, _, candidate, checks = captured_repository
    before = repository_snapshot(lab.repository)
    original = deepcopy((candidate, checks))
    proof = flow_module().verify_result_repository(lab, candidate, checks)
    assert proof["original_head"] == lab.manifest["repository_head"]
    assert proof["result_commit"] == candidate["result_commit"]
    assert proof["result_tree"] == candidate["result_tree"]
    assert proof["candidate_id"] == candidate["candidate_id"]
    assert proof["verification_id"] == checks[0]["verification_id"]
    assert proof["file_sha256"] == {
        name: hashlib.sha256(body).hexdigest()
        for name, body in EXPECTED_RESULT_BYTES.items()
    }
    assert repository_snapshot(lab.repository) == before
    assert (candidate, checks) == original
    assert not any((lab.repository / name).exists() for name in EXPECTED_RESULT_BYTES)


def test_node_probe_uses_selected_path_node_without_personal_literal(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/node" if name == "node" else None
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs)) or SimpleNamespace(stdout="{}")
        ),
    )
    assert node_probe("process.stdout.write('{}')") == {}
    assert len(calls) == 1 and calls[0][0][0] == "/usr/bin/node"
    assert calls[0][1]["env"]["PATH"] == "/usr/bin:/usr/bin:/bin"
    assert (
        calls[0][1]["timeout"] == 10
        and calls[0][1]["check"] is True
        and calls[0][1]["capture_output"] is True
    )


def test_node_probe_missing_node_refuses_before_any_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (
            calls.append((args, kwargs)) or SimpleNamespace(stdout="{}")
        ),
    )
    with pytest.raises(AssertionError, match="node24_required"):
        node_probe("process.stdout.write('{}')")
    assert calls == []


def node_probe(code, *arguments):
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node24_required")
    source = Path(__file__).resolve().parents[4]
    module = source / "apps/web/tests/e2e/native-harness-fixtures.ts"
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            "const fixture = await import(process.argv[1]);\n" + code,
            module.as_uri(),
            *map(str, arguments),
        ],
        cwd=source / "apps/web",
        env={
            "PATH": str(Path(node).parent) + ":/usr/bin:/bin",
            "HOME": str(source),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "bad",
    (
        "version_race",
        "behind_cursor",
        "missing_response",
        "has_more",
        "zero_cursor",
        "inactive_session",
        "disabled_input",
        "disabled_reread",
        "changed_run",
        "changed_address",
        "stale_session",
        "context_workspace",
        "context_worker",
        "context_execution",
        "context_run",
        "execution_workspace",
        "execution_worker",
        "execution_run",
        "trace_workspace",
        "trace_worker",
        "trace_execution",
        "trace_run",
        "trace_event_run",
        "session_workspace",
        "session_execution",
        "session_id",
        "session_worker",
        "session_run",
    ),
)
def test_desktop_source_context_reads_require_current_original_projection(
    desktop_manifest, bad
):
    path, manifest, _ = desktop_manifest
    data = source_projection(manifest["workspace_id"])
    address = deepcopy(data["view"]["context"])
    if bad == "version_race":
        old = deepcopy(data["view"])
        old["context"]["session_version"] = 2
        data["contexts"] = [old, data["view"]]
    else:
        data["contexts"] = corrupt_source_projection(data, bad)
    proof = node_probe(
        """
const manifest=fixture.loadOwnedManifest(process.argv[2]), data=JSON.parse(process.argv[3]), address=JSON.parse(process.argv[4]);
let contextReads=0; const reads=[];
const page={request:{get:async(url,options)=>{
  if(options.headers.Origin!==manifest.base_url) throw Error('foreign-origin');
  const parsed=new URL(url); reads.push({path:parsed.pathname,query:Object.fromEntries(parsed.searchParams)});
  let value;
  if(parsed.pathname.endsWith('/native-context')) { const contexts=data.contexts.length?data.contexts:[data.view]; value=contexts[Math.min(contextReads++,contexts.length-1)]; }
  else if(parsed.pathname.endsWith('/native-trace')) value=data.trace;
  else if(parsed.pathname==='/api/v1/gateway/sessions') value=data.sessions;
  else throw Error('unexpected-public-read');
  return {ok:()=>true,json:async()=>structuredClone(value)};
}}};
const native=new fixture.NativeFixture(page,manifest);
const first=await native.readSourceContext(address.worker_item_id,address.run_id);
const second=await native.readSourceContext(address.worker_item_id,address.run_id);
process.stdout.write(JSON.stringify({first:first?.context?.session_version??null,second:second?.context?.session_version??null,contextReads,reads,mutations:native.mutations.length}));
""",
        path,
        json.dumps(data),
        json.dumps(address),
    )
    assert proof["first"] is None
    assert proof["second"] == (3 if bad == "version_race" else None)
    assert proof["mutations"] == 0
    for read in proof["reads"]:
        if read["path"].endswith("/native-trace"):
            assert read["query"] == {"after_sequence": "0", "limit": "100"}
        elif read["path"] == "/api/v1/gateway/sessions":
            assert read["query"] == {
                "workspace_id": manifest["workspace_id"],
                "session_id": address["session_id"],
                "limit": "1",
            }
    if bad == "version_race":
        assert proof["contextReads"] == 4


@pytest.fixture
def desktop_manifest(tmp_path):
    from tests.support.native_product_flow import SOURCE_PATHS, _digest

    directory = tmp_path / "desktop"
    directory.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.mkdir()
    pins = {}
    for name in SOURCE_PATHS:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Owned synthetic source\n")
        pins[name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "scope": "ui" if name.startswith("apps/web/") else "producer",
        }
    ws = str(uuid4())
    config = {
        "settings": {"workspace_id": ws},
        "selectors": {},
        "planning_budget": {"max_concurrency": 4},
    }

    def save(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    save(directory / "config.json", config)
    for name in ("repository", "checkpoints", "cleanup", "bootstrap"):
        (directory / name).mkdir(mode=0o700)
    value = {
        "schema_version": 1,
        "owner_uid": os.getuid(),
        "workspace_id": ws,
        "allocation_id": "sidera-native-harness:product:n17:20261007:desktopunit",
        "namespace": str(uuid4()),
        "principal_id": "owned-human",
        "repository_directory": str(directory / "repository"),
        "repository_head": "a" * 40,
        "source_root": str(source),
        "source_pins": pins,
        "config_path": str(directory / "config.json"),
        "config_hash": _digest(config),
        "mode": "fake",
        "controlled_bridge": True,
        "guard_custody": "caller",
        "base_url": "http://[::1]:43141",
        "backend_url": "http://127.0.0.1:43142",
        "controller_url": "http://127.0.0.1:43143",
        "transport_path": str(directory / "transport.json"),
        "checkpoint_path": str(directory / "checkpoints"),
        "cleanup_path": str(directory / "cleanup"),
        "owner_receipt_path": str(directory / "owner.json"),
        "process_identities": {
            "owner_pid": os.getpid(),
            "backend_port": 43142,
            "web_port": 43141,
        },
        "target_facts": [],
    }
    save(
        directory / "owner.json",
        {
            "workspace_id": ws,
            "config_hash": value["config_hash"],
            "owner_pid": os.getpid(),
        },
    )
    save(directory / "bootstrap/session.json", {"token": "PRIVATE-BOOTSTRAP-UNIT"})
    save(
        directory / "transport.json",
        {
            "schema_version": 1,
            "workspace_id": ws,
            "bootstrap_file": str(directory / "bootstrap/session.json"),
            "controller_token": "PRIVATE-CONTROLLER-UNIT",
        },
    )
    save(directory / "manifest.json", value)
    return directory / "manifest.json", value, save


def configuration_capture_probe(desktop_manifest, *, case="valid", target=False):
    path, manifest, _ = desktop_manifest
    ws = manifest["workspace_id"]
    data = {
        "case": case,
        "target": target,
        "runtime": {"workspace_id": ws, "runtime_id": str(uuid4())},
        "connection": str(uuid4()),
        "create_op": str(uuid4()),
        "bind_op": str(uuid4()),
        "resource_op": str(uuid4()),
        "ref": {"workspace_id": ws, "profile_id": str(uuid4()), "revision": 1},
        "resource": {"workspace_id": ws, "resource_id": str(uuid4()), "revision": 1},
        "target_create_op": str(uuid4()),
        "target_bind_op": str(uuid4()),
        "target_ref": {"workspace_id": ws, "profile_id": str(uuid4()), "revision": 1},
        "foreign": str(uuid4()),
    }
    proof = node_probe(
        """
const manifest=fixture.loadOwnedManifest(process.argv[2]), d=JSON.parse(process.argv[3]);
let handler; const page={route:async(_pattern,fn)=>{handler=fn;},on:()=>{}};
const n=new fixture.NativeFixture(page,manifest), root=manifest.base_url+'/agents/v1/workspaces/'+manifest.workspace_id;
n.ids.runtime_id=d.runtime.runtime_id; n.ids.task_tool_connection_id=d.connection;
await n.installObservation(); const before=[];
async function send(path,body,key) {
  await handler({request:()=>({url:()=>root+path,method:()=> 'POST',postDataJSON:()=>body}),continue:async()=>{
    const r=fixture.nativeRecords(manifest).at(-1); before.push(r.original_ids[key]===body.operationId && r.observed.mutating_intent===true);
  }});
}
function result(op,ref) {return {operationId:op,state:'succeeded',target_ref:ref,result_ref:ref,reason_code:null};}
let refused=false;
try {
  await send('/profiles',{operationId:d.create_op,draft:{runtime_ref:d.runtime,resource_refs:[]}},'profile_create_operation_id');
  await n.capture(root+'/profiles',201,result(d.create_op,d.ref));
  const rd={name:'SIDERA Task Tools',kind:'mcp',settings:{connection_id:d.connection},secret_refs:[]};
  await send('/resources',{operationId:d.resource_op,draft:rd},'task_tool_resource_operation_id');
  await n.capture(root+'/resources',201,result(d.resource_op,d.resource));
  const body={operationId:d.bind_op,expected_ref:d.ref,draft:{runtime_ref:d.runtime,resource_refs:[d.resource]}};
  if(d.case==='expected') body.expected_ref={...d.ref,revision:2};
  if(d.case==='resource') body.draft.resource_refs=[{...d.resource,resource_id:d.foreign}];
  if(d.case==='missing') body.draft.resource_refs=[];
  await send('/profiles/'+d.ref.profile_id+'/revisions',body,'configuration_operation_id');
  if(d.case==='replacement') await send('/profiles/'+d.ref.profile_id+'/revisions',{...body,operationId:d.foreign},'configuration_operation_id');
  let final={...d.ref,revision:d.case==='jump'?3:2};
  if(d.case==='foreign') final={...final,workspace_id:d.foreign};
  if(d.case==='profile') final={...final,profile_id:d.foreign};
  const response=result(d.case==='operation'?d.foreign:d.bind_op,final);
  if(d.case==='result') response.result_ref={...final,profile_id:d.foreign};
  await n.capture(root+'/profiles/'+d.ref.profile_id+'/revisions',200,response);
  if(d.case==='valid') {
    await n.capture(root+'/profiles',201,result(d.create_op,d.ref));
    await n.capture(root+'/profiles/'+d.ref.profile_id+'/revisions',200,response);
    if(d.target) {
      n.configurationCapture='target';
      await send('/profiles',{operationId:d.target_create_op,draft:{runtime_ref:d.runtime,resource_refs:[]}},'target_profile_create_operation_id');
      await n.capture(root+'/profiles',201,result(d.target_create_op,d.target_ref));
      await send('/profiles/'+d.target_ref.profile_id+'/revisions',{operationId:d.target_bind_op,expected_ref:d.target_ref,draft:{runtime_ref:d.runtime,resource_refs:[d.resource]}},'target_configuration_operation_id');
      n.configurationCapture='primary';
      await n.capture(root+'/profiles/'+d.target_ref.profile_id+'/revisions',200,result(d.target_bind_op,{...d.target_ref,revision:2}));
    }
  }
} catch {refused=true;}
process.stdout.write(JSON.stringify({ids:n.ids,before,refused}));
""",
        path,
        json.dumps(data),
    )
    return data, proof


@pytest.mark.parametrize("target", [False, True])
def test_desktop_configuration_captures_original_create_resource_and_bind_without_regressing_final_ref(
    desktop_manifest, target
):
    data, proof = configuration_capture_probe(desktop_manifest, target=target)
    assert proof["refused"] is False and all(proof["before"])
    ids = proof["ids"]
    assert ids["profile_create_operation_id"] == data["create_op"]
    assert ids["profile_create_ref"] == data["ref"]
    assert ids["configuration_operation_id"] == data["bind_op"]
    assert ids["profile_ref"] == {**data["ref"], "revision": 2}
    assert ids["task_tool_resource_operation_id"] == data["resource_op"]
    assert ids["task_tool_resource_ref"] == data["resource"]
    assert ids["task_tool_connection_id"] == data["connection"]
    if target:
        assert ids["target_profile_create_operation_id"] == data["target_create_op"]
        assert ids["target_profile_create_ref"] == data["target_ref"]
        assert ids["target_configuration_operation_id"] == data["target_bind_op"]
        assert ids["target_profile_ref"] == {**data["target_ref"], "revision": 2}


@pytest.mark.parametrize(
    "case",
    [
        "expected",
        "resource",
        "missing",
        "replacement",
        "jump",
        "foreign",
        "profile",
        "operation",
        "result",
    ],
)
def test_desktop_configuration_refuses_changed_bind_intent_or_response_without_replacing_originals(
    desktop_manifest, case
):
    data, proof = configuration_capture_probe(desktop_manifest, case=case)
    assert proof["refused"] is True
    assert proof["ids"]["profile_create_operation_id"] == data["create_op"]
    assert proof["ids"]["task_tool_resource_operation_id"] == data["resource_op"]
    assert proof["ids"]["profile_ref"] == data["ref"]
    assert proof["ids"].get("configuration_operation_id") in {None, data["bind_op"]}


@pytest.mark.parametrize(
    "scenario",
    [None, "main", "transition", "provider", True, "foreign", "changed", "null"],
)
def test_desktop_scenario_reads_only_protected_pinned_config(
    desktop_manifest, scenario
):
    path, manifest, save = desktop_manifest
    config = json.loads(Path(manifest["config_path"]).read_text())
    if scenario is not None:
        config["scenario"] = None if scenario == "null" else scenario
    if scenario == "foreign":
        config["settings"]["workspace_id"] = str(uuid4())
    save(Path(manifest["config_path"]), config)
    if scenario != "changed":
        manifest["config_hash"] = flow_module()._digest(config)
    proof = node_probe(
        "const manifest=JSON.parse(process.argv[2]); let value=null,refused=false; try {value=fixture.nativeScenario(manifest);} catch {refused=true;} process.stdout.write(JSON.stringify({value,refused}));",
        json.dumps(manifest),
    )
    assert proof == (
        {"value": scenario or "main", "refused": False}
        if scenario in {None, "main", "transition"}
        else {"value": None, "refused": True}
    )


@pytest.mark.parametrize(
    "state",
    [
        "succeeded",
        "reconciled",
        "rejected",
        "dispatching",
        "unknown",
        "foreign",
        "wrong_operation",
        "wrong_target",
        "boolean_revision",
    ],
)
def test_desktop_target_configuration_capture_never_overwrites_source(
    desktop_manifest, state
):
    path, manifest, _ = desktop_manifest
    source_ref = {
        "workspace_id": manifest["workspace_id"],
        "profile_id": str(uuid4()),
        "revision": 1,
    }
    target_ref = {**source_ref, "profile_id": str(uuid4())}
    operation, target_op, runtime = str(uuid4()), str(uuid4()), str(uuid4())
    value = {
        "operationId": target_op,
        "state": state
        if state in {"succeeded", "reconciled", "rejected", "dispatching", "unknown"}
        else "succeeded",
        "result_ref": deepcopy(target_ref),
        "target_ref": deepcopy(target_ref),
        "reason_code": None,
    }
    if state == "foreign":
        value["result_ref"]["workspace_id"] = str(uuid4())
    if state == "wrong_operation":
        value["operationId"] = str(uuid4())
    if state == "wrong_target":
        value["target_ref"]["profile_id"] = str(uuid4())
    if state == "boolean_revision":
        value["result_ref"]["revision"] = True
    proof = node_probe(
        "const manifest=fixture.loadOwnedManifest(process.argv[2]); const data=JSON.parse(process.argv[3]); const n=new fixture.NativeFixture({},manifest); n.ids.profile_create_operation_id=data.operation; await n.capture(manifest.base_url+'/agents/v1/workspaces/'+manifest.workspace_id+'/runtimes',201,{ref:{workspace_id:manifest.workspace_id,runtime_id:data.runtime}}); await n.capture(manifest.base_url+'/agents/v1/workspaces/'+manifest.workspace_id+'/profiles',200,{operationId:data.operation,state:'succeeded',target_ref:data.source,result_ref:data.source,reason_code:null}); n.configurationCapture='target'; n.ids.target_profile_create_operation_id=data.target_op; let refused=false; try {await n.capture(manifest.base_url+'/agents/v1/workspaces/'+manifest.workspace_id+'/profiles',200,data.value);} catch {refused=true;} process.stdout.write(JSON.stringify({source:n.ids.profile_ref,profile_id:n.ids.profile_id,operation:n.ids.profile_create_operation_id,runtime_id:n.ids.runtime_id,target:n.ids.target_profile_ref??null,target_id:n.ids.target_profile_id??null,target_op:n.ids.target_profile_create_operation_id,refused}));",
        path,
        json.dumps(
            {
                "source": source_ref,
                "target_op": target_op,
                "operation": operation,
                "runtime": runtime,
                "value": value,
            }
        ),
    )
    assert (
        proof["source"] == source_ref
        and proof["profile_id"] == source_ref["profile_id"]
    )
    assert (
        proof["operation"] == operation
        and proof["runtime_id"] == runtime
        and proof["target_op"] == target_op
    )
    successful = state in {"succeeded", "reconciled"}
    assert proof["target"] == (target_ref if successful else None)
    assert proof["target_id"] == (target_ref["profile_id"] if successful else None)
    assert proof["refused"] is (
        state in {"foreign", "wrong_operation", "wrong_target", "boolean_revision"}
    )


def test_desktop_target_intent_precedes_post_without_replacing_source(desktop_manifest):
    path, manifest, _ = desktop_manifest
    source_op, target_op, runtime = str(uuid4()), str(uuid4()), str(uuid4())
    proof = node_probe(
        "const manifest=fixture.loadOwnedManifest(process.argv[2]); let handler; const page={route:async(_pattern,fn)=>{handler=fn;},on:()=>{}}; const n=new fixture.NativeFixture(page,manifest); n.ids.configuration_operation_id=process.argv[3]; n.ids.runtime_id=process.argv[5]; n.configurationCapture='target'; await n.installObservation(); let continued=0; await handler({request:()=>({url:()=>manifest.base_url+'/agents/v1/workspaces/'+manifest.workspace_id+'/profiles',method:()=> 'POST',postDataJSON:()=>({operationId:process.argv[4],draft:{runtime_ref:{workspace_id:manifest.workspace_id,runtime_id:process.argv[5]},resource_refs:[]}})}),continue:async()=>{continued++;const r=fixture.nativeRecords(manifest).at(-1);if(r.observed.mutating_intent!==true||r.original_ids.target_profile_create_operation_id!==process.argv[4])throw Error('intent-not-saved');}}); process.stdout.write(JSON.stringify({continued,source_op:n.ids.configuration_operation_id,target_op:n.ids.target_profile_create_operation_id}));",
        path,
        source_op,
        target_op,
        runtime,
    )
    assert proof == {"continued": 1, "source_op": source_op, "target_op": target_op}


def test_desktop_focused_receipt_is_scoped_private_and_never_overwritten(
    desktop_manifest,
):
    path, manifest, save = desktop_manifest
    config = json.loads(Path(manifest["config_path"]).read_text())
    config["scenario"] = "transition"
    save(Path(manifest["config_path"]), config)
    manifest["config_hash"] = flow_module()._digest(config)
    save(
        Path(manifest["owner_receipt_path"]),
        {
            "workspace_id": manifest["workspace_id"],
            "config_hash": manifest["config_hash"],
            "owner_pid": os.getpid(),
        },
    )
    save(path, manifest)
    proof = node_probe(
        "const fs=await import('node:fs'); const manifest=fixture.loadOwnedManifest(process.argv[2]);const n=new fixture.NativeFixture({},manifest);const one=n.recordFocusedTransition({focused_transition:true,known_inspection:'gone',canonical_converged:false});const original=fs.readFileSync(one,'utf8');const two=n.recordFocusedTransition({focused_transition:true});let refused=false;try{n.recordFocusedTransition({token:'must-not-write'});}catch{refused=true;}process.stdout.write(JSON.stringify({unique:one!==two,unchanged:fs.readFileSync(one,'utf8')===original,mode:fs.statSync(one).mode&0o777,receipt:JSON.parse(original),refused}));",
        path,
    )
    assert (
        proof["unique"]
        and proof["unchanged"]
        and proof["mode"] == 0o600
        and proof["refused"]
    )
    assert proof["receipt"]["workspace_id"] == manifest["workspace_id"]
    assert proof["receipt"]["config_hash"] == manifest["config_hash"]
    assert proof["receipt"]["observed"]["canonical_converged"] is False


def test_desktop_config_budget_keeps_single_worker_and_no_retries():
    source = Path(__file__).resolve().parents[4]
    proof = node_probe(
        "const ts=await import('typescript'); const fs=await import('node:fs'); const tree=ts.createSourceFile('config.ts',fs.readFileSync(process.argv[2],'utf8'),ts.ScriptTarget.Latest,true); const out={}; const visit=n=>{if(ts.isPropertyAssignment(n)&&['timeout','workers','retries'].includes(n.name.getText(tree))&&ts.isNumericLiteral(n.initializer)&&n.parent.properties.some(p=>p.name?.getText(tree)==='testDir'))out[n.name.getText(tree)]=Number(n.initializer.getText(tree).replaceAll('_',''));ts.forEachChild(n,visit);}; visit(tree); process.stdout.write(JSON.stringify(out));",
        source / "apps/web/playwright.native.config.ts",
    )
    assert proof == {"timeout": 3600000, "workers": 1, "retries": 0}


def test_desktop_reads_exact_private_manifest_without_exposing_transport(
    desktop_manifest,
):
    path, value, _ = desktop_manifest
    proof = node_probe(
        "const value = fixture.loadOwnedManifest(process.argv[2]); process.stdout.write(JSON.stringify({workspace_id:value.workspace_id, fields:Object.keys(value)}));",
        path,
    )
    assert proof["workspace_id"] == value["workspace_id"]
    assert (
        "controller_token" not in proof["fields"]
        and "bootstrap_file" not in proof["fields"]
    )


def test_desktop_screenshot_directory_reuses_owned_private_evidence(desktop_manifest):
    path, _, _ = desktop_manifest
    proof = node_probe(
        "const fs=await import('node:fs'); const manifest=fixture.loadOwnedManifest(process.argv[2]); const first=fixture.nativeScreenshotDirectory(manifest); fs.writeFileSync(first+'/original.png','original',{mode:0o600,flag:'wx'}); const second=fixture.nativeScreenshotDirectory(manifest); process.stdout.write(JSON.stringify({same:first===second,body:fs.readFileSync(second+'/original.png','utf8'),mode:fs.statSync(second).mode&0o777}));",
        path,
    )
    assert proof == {"same": True, "body": "original", "mode": 0o700}


@pytest.mark.parametrize("case", ["symlink", "permissive", "regular_file"])
def test_desktop_screenshot_directory_refuses_foreign_evidence(desktop_manifest, case):
    path, _, _ = desktop_manifest
    directory = path.parent / "screenshots"
    if case == "symlink":
        directory.symlink_to(path.parent / "repository", target_is_directory=True)
    elif case == "permissive":
        directory.mkdir()
        directory.chmod(0o755)
    else:
        directory.write_bytes(b"Existing material evidence")
    proof = node_probe(
        "let refused=false; try {fixture.nativeScreenshotDirectory(fixture.loadOwnedManifest(process.argv[2]));} catch {refused=true;} process.stdout.write(JSON.stringify(refused));",
        path,
    )
    assert proof is True
    if case == "regular_file":
        assert directory.read_bytes() == b"Existing material evidence"


def test_desktop_checkpoints_keep_strict_order_and_original_identity(desktop_manifest):
    path, _, _ = desktop_manifest
    proof = node_probe(
        "const manifest=fixture.loadOwnedManifest(process.argv[2]); const lab=new fixture.NativeFixture({},manifest); Date.now=()=>1700000000000; const first=lab.record('pending'); lab.ids.root_task_id='d87b86d0-b54e-4fbc-a3eb-dd09b88828db'; const second=lab.record('failed',{reason:'original_response_lost'}); const records=fixture.nativeRecords(manifest); process.stdout.write(JSON.stringify({ordered:second.sequence>first.sequence,original:records.at(-1).original_ids.root_task_id,count:records.length}));",
        path,
    )
    assert proof == {
        "ordered": True,
        "original": "d87b86d0-b54e-4fbc-a3eb-dd09b88828db",
        "count": 2,
    }


@pytest.mark.parametrize(
    "case", ["fresh", "sent_unknown", "passed", "original_reconciled"]
)
def test_desktop_stage_queries_original_before_any_repeated_intent(
    desktop_manifest, case
):
    path, _, _ = desktop_manifest
    proof = node_probe(
        "const manifest=fixture.loadOwnedManifest(process.argv[2]); const page={context:()=>({storageState:async()=>({cookies:[],origins:[]})})}; const lab=new fixture.NativeFixture(page,manifest); let actions=0,reads=0,blocked=false; const kind=process.argv[3]; const first=lab.record(kind==='passed'?'passed':'pending',{resume:kind!=='fresh',mutating_intent:kind==='sent_unknown'}); process.env.SIDERA_NATIVE_STEP_ID=first.step_id; lab.observe=async()=>{reads++; return kind!=='sent_unknown';}; try {await lab.step('configuration',async()=>{actions++; return {unit_sequence_only:true};});} catch {blocked=true;} process.stdout.write(JSON.stringify({actions,reads,blocked,last:fixture.nativeRecords(manifest).at(-1).status,step_id_unchanged:fixture.nativeRecords(manifest).every(item=>item.step_id===first.step_id)}));",
        path,
        case,
    )
    assert proof["actions"] == (1 if case == "fresh" else 0)
    assert proof["reads"] == 1
    assert proof["blocked"] is (case == "sent_unknown")
    assert proof["last"] == ("pending" if case == "sent_unknown" else "passed")
    assert proof["step_id_unchanged"] is True


@pytest.mark.parametrize(
    "case", ["symlink", "permissive", "foreign_config", "producer_change", "extra_argv"]
)
def test_desktop_refuses_unsafe_manifest_before_browser_allocation(
    desktop_manifest, case
):
    path, value, save = desktop_manifest
    if case == "symlink":
        selected = path.parent / "linked.json"
        selected.symlink_to(path)
        path = selected
    elif case == "permissive":
        path.chmod(0o644)
    elif case == "foreign_config":
        config = json.loads(Path(value["config_path"]).read_text())
        config["settings"]["workspace_id"] = str(uuid4())
        save(Path(value["config_path"]), config)
    elif case == "producer_change":
        name = next(
            name for name in value["source_pins"] if not name.startswith("apps/web/")
        )
        (Path(value["source_root"]) / name).write_bytes(b"Changed producer\n")
    elif case == "extra_argv":
        value["argv"] = ["/usr/bin/true"]
        save(path, value)
    assert (
        node_probe(
            "let refused=false; try {fixture.loadOwnedManifest(process.argv[2]);} catch {refused=true;} process.stdout.write(JSON.stringify(refused));",
            path,
        )
        is True
    )


@pytest.mark.parametrize(
    "producer",
    (
        "services/control-plane/src/sidera_control_plane/application/event_ingestor.py",
        "services/control-plane/src/sidera_control_plane/application/worker_gateway.py",
    ),
)
def test_desktop_manifest_binds_real_stop_producer_hashes(desktop_manifest, producer):
    path, value, save = desktop_manifest
    original = (Path(__file__).resolve().parents[4] / producer).read_bytes()
    source = Path(value["source_root"]) / producer
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(original)
    pin = {"scope": "producer", "sha256": hashlib.sha256(original).hexdigest()}
    assert producer in value["source_pins"]
    value["source_pins"][producer] = pin
    save(path, value)
    assert (
        node_probe(
            "const m=fixture.loadOwnedManifest(process.argv[2]); process.stdout.write(JSON.stringify(m.source_pins[process.argv[3]]));",
            path,
            producer,
        )
        == pin
    )
    source.write_bytes(original + b"\n# changed producer\n")
    assert (
        node_probe(
            "let refused=false; try {fixture.loadOwnedManifest(process.argv[2],true);} catch {refused=true;} process.stdout.write(JSON.stringify(refused));",
            path,
        )
        is True
    )


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "foreign_workspace",
        "foreign_worker",
        "foreign_execution",
        "foreign_run",
        "foreign_profile",
        "wrong_cursor",
    ],
)
def test_desktop_trace_requires_actual_original_scope_and_forward_cursor(case):
    ws, item, execution, run, profile = map(
        str, [uuid4(), uuid4(), uuid4(), uuid4(), uuid4()]
    )
    pin = {
        "workspace_id": ws,
        "worker_item_id": item,
        "execution_id": execution,
        "run_id": run,
        "profile_ref": {"workspace_id": ws, "profile_id": profile, "revision": 1},
    }
    value = {
        **deepcopy(pin),
        "events": [{"run_id": run, "sequence": 1}, {"run_id": run, "sequence": 2}],
        "next_sequence": 2,
        "has_more": False,
    }
    if case.startswith("foreign_"):
        key = {
            "foreign_workspace": "workspace_id",
            "foreign_worker": "worker_item_id",
            "foreign_execution": "execution_id",
            "foreign_run": "run_id",
        }.get(case)
        if key:
            value[key] = str(uuid4())
        else:
            value["profile_ref"]["profile_id"] = str(uuid4())
    elif case == "wrong_cursor":
        value["next_sequence"] = 1
    assert node_probe(
        "process.stdout.write(JSON.stringify(fixture.exactNativeTrace(JSON.parse(process.argv[2]),JSON.parse(process.argv[3]))));",
        json.dumps(value),
        json.dumps(pin),
    ) is (case == "valid")


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "foreign_workspace",
        "foreign_worker",
        "foreign_execution",
        "foreign_run",
        "foreign_profile",
    ],
)
def test_desktop_current_trace_uses_execution_pin_not_run_metadata_id(case):
    ws, item, execution, run, profile = map(
        str, [uuid4(), uuid4(), uuid4(), uuid4(), uuid4()]
    )
    ref = {"workspace_id": ws, "profile_id": profile, "revision": 1}
    context = {
        "workspace_id": ws,
        "worker_item_id": item,
        "run": {"profile_ref": ref, "state": "closed", "input_open": False},
        "execution": {
            "workspace_id": ws,
            "worker_item_id": item,
            "execution_id": execution,
            "run_id": run,
            "profile_ref": ref,
        },
    }
    trace = {
        "workspace_id": ws,
        "worker_item_id": item,
        "execution_id": execution,
        "run_id": run,
        "profile_ref": deepcopy(ref),
        "events": [{"run_id": run, "sequence": 1}],
        "next_sequence": 1,
        "has_more": False,
    }
    if case == "foreign_profile":
        trace["profile_ref"]["profile_id"] = str(uuid4())
    elif case != "valid":
        trace[
            {
                "foreign_workspace": "workspace_id",
                "foreign_worker": "worker_item_id",
                "foreign_execution": "execution_id",
                "foreign_run": "run_id",
            }[case]
        ] = str(uuid4())
    assert "run_id" not in context["run"]
    assert node_probe(
        "const pin=fixture.currentNativeTracePin(JSON.parse(process.argv[2])); process.stdout.write(JSON.stringify(fixture.exactNativeTrace(JSON.parse(process.argv[3]),pin)));",
        json.dumps(context),
        json.dumps(trace),
    ) is (case == "valid")


@pytest.mark.parametrize(
    "case",
    [
        "missing_commit",
        "missing_tree",
        "invalid_commit",
        "invalid_tree",
        "absent_commit",
        "tree_as_commit",
        "wrong_tree",
        "wrong_candidate",
        "wrong_check_commit",
        "wrong_check_tree",
        "failed_check",
        "wrong_exit",
        "wrong_bytes",
        "missing_newline",
        "changed_head",
        "dirty_checkout",
        "installed_output",
    ],
)
def test_result_repository_refuses_uncorrelated_or_changed_proof_without_writes(
    captured_repository, case
):
    from tests.support.native_product_harness import local_git

    lab, execution, candidate, checks = captured_repository
    if case == "missing_commit":
        candidate["result_commit"] = None
    elif case == "missing_tree":
        candidate.pop("result_tree")
    elif case == "invalid_commit":
        candidate["result_commit"] = "HEAD; false"
    elif case == "invalid_tree":
        candidate["result_tree"] = "--help"
    elif case == "absent_commit":
        candidate["result_commit"] = "f" * 40
    elif case == "tree_as_commit":
        candidate["result_commit"] = candidate["result_tree"]
    elif case == "wrong_tree":
        candidate["result_tree"] = local_git(lab.repository, "rev-parse", "HEAD^{tree}")
    elif case == "wrong_candidate":
        checks[0]["candidate_id"] = str(uuid4())
    elif case == "wrong_check_commit":
        checks[0]["actual_commit"] = lab.manifest["repository_head"]
    elif case == "wrong_check_tree":
        checks[0]["actual_tree"] = "b" * 40
    elif case == "failed_check":
        checks[0]["status"] = "failed"
    elif case == "wrong_exit":
        checks[0]["exit_code"] = 1
    elif case in {"wrong_bytes", "missing_newline"}:
        (execution / "integration.txt").write_bytes(
            b"Integrated 41\n" if case == "wrong_bytes" else b"Integrated 42"
        )
        local_git(execution, "add", "integration.txt")
        local_git(execution, "commit", "-m", "Wrong captured bytes")
        candidate["result_commit"] = checks[0]["actual_commit"] = local_git(
            execution, "rev-parse", "HEAD"
        )
        candidate["result_tree"] = checks[0]["actual_tree"] = local_git(
            execution, "rev-parse", "HEAD^{tree}"
        )
    elif case == "changed_head":
        local_git(lab.repository, "commit", "--allow-empty", "-m", "Changed original")
    elif case == "dirty_checkout":
        (lab.repository / "check.py").write_bytes(b"assert False\n")
    elif case == "installed_output":
        (lab.repository / "integration.txt").write_bytes(
            EXPECTED_RESULT_BYTES["integration.txt"]
        )
    before = repository_snapshot(lab.repository)
    original = deepcopy((candidate, checks))
    with pytest.raises(ValueError, match="native_result_repository_refused"):
        flow_module().verify_result_repository(lab, candidate, checks)
    assert repository_snapshot(lab.repository) == before
    assert (candidate, checks) == original
