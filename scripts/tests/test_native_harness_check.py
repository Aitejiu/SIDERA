import hashlib
import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4, uuid5

import pytest


def module():
    path = Path(__file__).parents[1] / "native-harness-check.py"
    spec = importlib.util.spec_from_file_location("native_harness_check", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def cleanup_reference(value):
    return {
        "schema_version": 1,
        "owner_uid": value["owner_uid"],
        "allocation_id": value["allocation_id"],
        "workspace_id": value["workspace_id"],
        "namespace": value["namespace"],
        "config_hash": value["config_hash"],
        "owner_pid": value["process_identities"]["owner_pid"],
        "operation_id": str(uuid5(UUID(value["namespace"]), "owned_cleanup")),
    }


def cleanup_proof(value, *, complete=True):
    flags = {
        name + "_closed": complete
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
    }
    flags.update(
        guards_inspected=True if value["guard_custody"] == "lab" else None,
        guards_released=True if value["guard_custody"] == "lab" else None,
    )
    return {**cleanup_reference(value), "complete": complete, "cleanup": flags}


@pytest.fixture
def manifest(tmp_path):
    cli = module()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.mkdir()
    pins = {}
    for name in cli.SOURCE_PATHS:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("owned fixture source\n")
        pins[name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "scope": "ui" if name.startswith("apps/web/") else "producer",
        }
    ws = str(uuid4())
    config = {
        "selectors": {},
        "planning_budget": {"max_concurrency": 4},
        "settings": {"workspace_id": ws},
    }
    private_json(private / "config.json", config)
    repo = private / "repository"
    repo.mkdir(mode=0o700)
    for name in ("checkpoints", "cleanup"):
        (private / name).mkdir(mode=0o700)
    value = {
        "schema_version": 1,
        "owner_uid": os.getuid(),
        "allocation_id": "sidera-native-harness:product:n17:20261007:ownedunit",
        "workspace_id": ws,
        "namespace": str(uuid4()),
        "principal_id": "owned-user",
        "repository_directory": str(repo),
        "repository_head": "a" * 40,
        "source_root": str(source),
        "source_pins": pins,
        "config_path": str(private / "config.json"),
        "config_hash": cli.fingerprint(config),
        "mode": "fake",
        "controlled_bridge": True,
        "base_url": "http://[::1]:43121",
        "backend_url": "http://127.0.0.1:43122",
        "controller_url": "http://127.0.0.1:43123",
        "transport_path": str(private / "transport.json"),
        "checkpoint_path": str(private / "checkpoints"),
        "cleanup_path": str(private / "cleanup"),
        "owner_receipt_path": str(private / "owner.json"),
        "process_identities": {
            "owner_pid": os.getpid(),
            "backend_port": 43122,
            "web_port": 43121,
        },
        "target_facts": [],
        "guard_custody": "caller",
    }
    private_json(
        private / "transport.json",
        {
            "schema_version": 1,
            "workspace_id": ws,
            "controller_token": "PRIVATE-CONTROL-TOKEN",
        },
    )
    private_json(
        private / "owner.json",
        cleanup_reference(value),
    )
    path = private / "manifest.json"
    private_json(path, value)
    return cli, path, value


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "symlink",
        "file_mode",
        "parent_mode",
        "owner",
        "schema",
        "foreign_config",
        "config_hash",
        "source_hash",
        "extra_argv",
        "real_bridge",
    ],
)
def test_manifest_refuses_unsafe_or_foreign_identity(manifest, case, monkeypatch):
    cli, path, value = manifest
    if case == "relative":
        path = Path("manifest.json")
    elif case == "symlink":
        selected = path.parent / "link.json"
        selected.symlink_to(path)
        path = selected
    elif case == "file_mode":
        path.chmod(0o644)
    elif case == "parent_mode":
        path.parent.chmod(0o755)
    elif case == "owner":
        monkeypatch.setattr(cli.os, "getuid", lambda: value["owner_uid"] + 1)
    elif case == "schema":
        value["schema_version"] = True
    elif case == "foreign_config":
        config = {"settings": {"workspace_id": str(uuid4())}}
        private_json(Path(value["config_path"]), config)
        value["config_hash"] = cli.fingerprint(config)
    elif case == "config_hash":
        value["config_hash"] = "f" * 64
    elif case == "source_hash":
        value["source_pins"][cli.SOURCE_PATHS[0]]["sha256"] = "f" * 64
    elif case == "extra_argv":
        value["argv"] = ["/usr/bin/true"]
    elif case == "real_bridge":
        value["mode"] = "real"
    if case not in {"relative", "symlink", "file_mode", "parent_mode", "owner"}:
        private_json(path, value)
    with pytest.raises(cli.ManifestError):
        cli.read_manifest(path)


@pytest.mark.parametrize(
    "producer",
    (
        "services/control-plane/src/sidera_control_plane/application/event_ingestor.py",
        "services/control-plane/src/sidera_control_plane/application/worker_gateway.py",
        "services/control-plane/src/sidera_control_plane/application/execution_code_inputs.py",
        "services/control-plane/src/sidera_control_plane/application/activation_reconciler.py",
        "services/control-plane/src/sidera_control_plane/application/context_requests.py",
        "services/control-plane/src/sidera_control_plane/application/handoff_context.py",
    ),
)
def test_manifest_binds_real_native_producer_hashes_and_refuses_changes(
    manifest, producer
):
    cli, path, value = manifest
    original = (Path(__file__).resolve().parents[2] / producer).read_bytes()
    source = Path(value["source_root"]) / producer
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(original)
    pin = {"scope": "producer", "sha256": hashlib.sha256(original).hexdigest()}
    assert producer in value["source_pins"]
    value["source_pins"][producer] = pin
    private_json(path, value)
    assert cli.read_manifest(path)["source_pins"][producer] == pin
    source.write_bytes(original + b"\n# changed producer\n")
    with pytest.raises(cli.ManifestError):
        cli.read_manifest(path, allow_ui_change=True)


class Client:
    def __init__(self, value=None, ids=None):
        self.reads = []
        self.unknown = self.unavailable = False
        self.value, self.ids = value, ids
        self.overrides = {}

    def get(self, path):
        self.reads.append(path)
        if self.unavailable:
            raise OSError("PRIVATE unavailable URL")
        for suffix, response in self.overrides.items():
            if path.endswith(suffix):
                if isinstance(response, Exception):
                    raise response
                return deepcopy(response)
        if "/commands/" in path:
            return {
                "command_id": path.rsplit("/", 1)[1],
                "status": "pending" if self.unknown else "applied",
                "result": {"command_id": path.rsplit("/", 1)[1]},
            }
        if self.value is None or self.ids is None:
            return {"status": "ok", "ready": True}
        ws, ids = self.value["workspace_id"], self.ids
        profile = ids["profile_ref"]
        candidate = {
            "candidate_id": ids["candidate_id"],
            "candidate_hash": ids["candidate_hash"],
            "result_commit": ids["result_commit"],
            "result_tree": ids["result_tree"],
        }
        if path == "/product/v1/session":
            return {"workspace_id": ws, "principal_id": self.value["principal_id"]}
        if path.endswith("/defaults"):
            return {
                "workspace_id": ws,
                "version": 1,
                **{
                    key + "_profile_id": profile["profile_id"]
                    for key in ("main", "planner", "worker")
                },
            }
        if path.endswith("/setup"):
            return {
                "workspace_id": ws,
                "version": 1,
                "repository_ref": "repo://owned",
                "planning_policy_ref": {
                    "policy_id": ids["planning_policy_id"],
                    "version": 1,
                },
                "planner_agent_ref": profile,
            }
        if "/configuration-requests?" in path:
            operation = parse_qs(urlsplit(path).query)["operationId"][0]
            refs = {
                ids["profile_create_operation_id"]: ids["profile_create_ref"],
                ids["task_tool_resource_operation_id"]: ids["task_tool_resource_ref"],
                ids["configuration_operation_id"]: profile,
            }
            if ids.get("target_profile_create_operation_id"):
                refs[ids["target_profile_create_operation_id"]] = ids[
                    "target_profile_create_ref"
                ]
                refs[ids["target_configuration_operation_id"]] = ids[
                    "target_profile_ref"
                ]
            ref = refs[operation]
            return deepcopy(
                {
                    "operationId": operation,
                    "state": "succeeded",
                    "target_ref": ref,
                    "result_ref": ref,
                }
            )
        if path.endswith("/tool-connections"):
            return [
                {
                    "workspace_id": ws,
                    "connection_id": ids["task_tool_connection_id"],
                    "name": "SIDERA Task Tools",
                    "availability": "configured",
                }
            ]
        if "/resources/" in path:
            ref = ids["task_tool_resource_ref"]
            return deepcopy(
                {
                    "resource": {
                        "ref": ref,
                        "draft": {
                            "name": "SIDERA Task Tools",
                            "kind": "mcp",
                            "settings": {
                                "connection_id": ids["task_tool_connection_id"]
                            },
                            "secret_refs": [],
                        },
                    },
                    "status": {
                        "resource_id": ref["resource_id"],
                        "enabled": True,
                        "version": 1,
                    },
                }
            )
        if "/runtimes/" in path:
            return {
                "ref": {
                    "workspace_id": ws,
                    "runtime_id": ids["runtime_id"],
                },
                "enabled": True,
                "installed": True,
            }
        if "/profiles/" in path:
            ref = (
                ids["target_profile_ref"]
                if ids.get("target_profile_id") == urlsplit(path).path.rsplit("/", 1)[1]
                else profile
            )
            return deepcopy(
                {
                    "profile": {
                        "ref": ref,
                        "draft": {
                            "runtime_ref": {
                                "workspace_id": ws,
                                "runtime_id": ids["runtime_id"],
                            },
                            "resource_refs": [ids["task_tool_resource_ref"]],
                        },
                    },
                    "status": {
                        "profile_id": ref["profile_id"],
                        "enabled": True,
                        "version": 1,
                    },
                }
            )
        if path.endswith("/materialization"):
            return {
                "id": ids["materialization_id"],
                "workspace_id": ws,
                "planning_request_id": ids["planning_request_id"],
                "root_task_id": ids["root_task_id"],
                "proposal_revision_id": ids["proposal_id"],
                "plan_snapshot_id": ids["plan_snapshot_id"],
                "plan_generation": ids["plan_generation"],
                "worker_item_mapping": {"source": ids["source_worker_item_id"]},
            }
        if "/planning-requests/" in path:
            return {
                "id": ids["planning_request_id"],
                "workspace_id": ws,
                "status": "materialized",
                "current_proposal_revision": 1,
                "current_proposal_content_hash": ids["proposal_content_hash"],
            }
        if path.endswith("/native-context"):
            return {
                "workspace_id": ws,
                "worker_item_id": ids["source_worker_item_id"],
                "execution": {
                    "execution_id": ids["source_execution_id"],
                    "workspace_id": ws,
                    "worker_item_id": ids["source_worker_item_id"],
                    "run_id": ids["source_run_id"],
                },
                "run": {"profile_ref": profile},
                "context": {
                    "worker_item_id": ids["source_worker_item_id"],
                    "execution_id": ids["source_execution_id"],
                    "run_id": ids["source_run_id"],
                    "plan_generation": ids["plan_generation"],
                    "binding_version": ids["binding_version"],
                },
            }
        if path.endswith("/native-trace"):
            return {
                "workspace_id": ws,
                "worker_item_id": ids["source_worker_item_id"],
                "execution_id": ids["source_execution_id"],
                "run_id": ids["source_run_id"],
                "profile_ref": profile,
                "events": [],
                "next_sequence": 0,
                "has_more": False,
            }
        if "/gateway/inputs?" in path:
            return {
                "items": [
                    {
                        "id": ids["input_id"],
                        "execution_id": ids["source_execution_id"],
                        "status": "delivered",
                    }
                ]
            }
        if path.endswith("/results"):
            return {
                "workspace_id": ws,
                "root_task_id": ids["root_task_id"],
                "task_status": "succeeded",
                "result_stage": "accepted",
                "current_candidate": candidate,
                "acceptance": {
                    "candidate_id": ids["candidate_id"],
                    "candidate_hash": ids["candidate_hash"],
                    "command_id": ids["acceptance_command_id"],
                },
            }
        if "/results/" in path:
            return {
                "workspace_id": ws,
                "root_task_id": ids["root_task_id"],
                "candidate": candidate,
                "verifications": [
                    {
                        "verification_id": ids["verification_id"],
                        "candidate_id": ids["candidate_id"],
                        "status": "passed",
                        "check_key": "owned-check",
                        "exit_code": 0,
                        "actual_commit": ids["result_commit"],
                        "actual_tree": ids["result_tree"],
                    }
                ],
            }
        if "/turns/" in path:
            return {
                "id": ids["turn_id"],
                "threadId": ids["thread_id"],
                "runId": ids["chat_run_id"],
                "profileRef": profile,
                "operationId": ids["turn_operation_id"],
                "operationState": "delivered",
                "status": "completed",
            }
        if "/threads/" in path:
            return {
                "id": ids["thread_id"],
                "runId": ids["chat_run_id"],
                "profileRef": profile,
            }
        return {"status": "ok", "ready": True}


def completed(cli, value, *, through="result_accepted"):
    journal = cli.Journal(value)
    ids = {
        key: str(uuid4())
        for key in (
            "command_id",
            "root_task_id",
            "planning_request_id",
            "source_run_id",
            "source_worker_item_id",
            "source_execution_id",
            "candidate_id",
            "thread_id",
            "turn_id",
            "profile_id",
            "configuration_operation_id",
            "profile_create_operation_id",
            "task_tool_resource_operation_id",
            "task_tool_connection_id",
            "runtime_id",
            "planning_policy_id",
            "materialization_id",
            "proposal_id",
            "plan_snapshot_id",
            "confirmation_command_id",
            "input_command_id",
            "input_id",
            "input_operation_id",
            "acceptance_command_id",
            "chat_run_id",
            "turn_operation_id",
            "verification_id",
        )
    }
    ids.update(
        profile_ref={
            "profile_id": ids["profile_id"],
            "workspace_id": value["workspace_id"],
            "revision": 2,
        },
        plan_generation=1,
        binding_version=1,
        proposal_revision=1,
        proposal_content_hash="sha256:" + "b" * 64,
        candidate_hash="sha256:" + "c" * 64,
        result_commit="d" * 40,
        result_tree="e" * 40,
    )
    ids["profile_create_ref"] = {**ids["profile_ref"], "revision": 1}
    ids["task_tool_resource_ref"] = {
        "workspace_id": value["workspace_id"],
        "resource_id": str(uuid4()),
        "revision": 1,
    }
    ids["configuration_expected_ref"] = deepcopy(ids["profile_create_ref"])
    ids["configuration_resource_ref"] = deepcopy(ids["task_tool_resource_ref"])
    for step in cli.STAGES[: cli.STAGES.index(through) + 1]:
        journal.append(step, "passed", original_ids=ids, observed={"canonical": True})
    return journal, ids


def test_configuration_observation_reads_three_originals_and_exact_immutable_bindings(
    manifest,
):
    cli, path, value = manifest
    journal, ids = completed(cli, value, through="configuration")
    client = Client(value, ids)
    assert (
        cli.NativeCheck(cli.read_manifest(path), client=client)._observe(
            journal.records()[-1]
        )
        is True
    )
    operations = [
        parse_qs(urlsplit(read).query)["operationId"][0]
        for read in client.reads
        if "/configuration-requests?" in read
    ]
    assert set(operations) == {
        ids["profile_create_operation_id"],
        ids["task_tool_resource_operation_id"],
        ids["configuration_operation_id"],
    }
    assert any(
        read.endswith(f"/profiles/{ids['profile_id']}?revision=2")
        for read in client.reads
    )
    assert any(
        read.endswith(
            f"/resources/{ids['task_tool_resource_ref']['resource_id']}?revision=1"
        )
        for read in client.reads
    )
    assert any(read.endswith("/tool-connections") for read in client.reads)


@pytest.mark.parametrize(
    "original", ["profile_create", "task_tool_resource", "configuration"]
)
@pytest.mark.parametrize(
    "case", ["unknown", "missing", "operation", "result", "target"]
)
def test_configuration_resume_refuses_changed_or_missing_original_without_runner(
    manifest, original, case
):
    cli, path, value = manifest
    _, ids = completed(cli, value)
    client = Client(value, ids)
    operation = ids[original + "_operation_id"]
    suffix = "/configuration-requests?operationId=" + operation
    body = client.get(suffix)
    if case == "missing":
        body = OSError("Original request is unavailable")
    elif case == "unknown":
        body["state"] = "unknown"
    elif case == "operation":
        body["operationId"] = str(uuid4())
    else:
        body[case + "_ref"] = {**body[case + "_ref"], "revision": 9}
    client.overrides[suffix] = body
    calls = []
    report = cli.NativeCheck(
        cli.read_manifest(path), client=client, runner=lambda *args: calls.append(args)
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE" and calls == []


@pytest.mark.parametrize(
    "case",
    [
        "removed_binding",
        "changed_binding",
        "runtime",
        "disabled_profile",
        "disabled_resource",
        "resource_workspace",
        "resource_kind",
        "resource_connection",
        "resource_secret",
        "metadata_missing",
        "metadata_foreign",
        "metadata_unavailable",
        "metadata_duplicate",
        "wrong_create_revision",
        "wrong_final_revision",
        "missing_create_original",
        "changed_expected",
        "changed_resource_pin",
    ],
)
def test_configuration_resume_refuses_changed_public_binding_or_checkpoint_without_runner(
    manifest, case
):
    cli, path, value = manifest
    _, ids = completed(cli, value)
    client = Client(value, ids)
    profile_path = f"/profiles/{ids['profile_id']}?revision=2"
    resource_path = (
        f"/resources/{ids['task_tool_resource_ref']['resource_id']}?revision=1"
    )
    profile = client.get(profile_path)
    resource = client.get(resource_path)
    metadata = client.get("/tool-connections")
    if case == "removed_binding":
        profile["profile"]["draft"]["resource_refs"] = []
    elif case == "changed_binding":
        profile["profile"]["draft"]["resource_refs"][0]["revision"] = 2
    elif case == "runtime":
        profile["profile"]["draft"]["runtime_ref"]["runtime_id"] = str(uuid4())
    elif case == "disabled_profile":
        profile["status"]["enabled"] = False
    elif case == "disabled_resource":
        resource["status"]["enabled"] = False
    elif case == "resource_workspace":
        resource["resource"]["ref"]["workspace_id"] = str(uuid4())
    elif case.startswith("resource_"):
        draft = resource["resource"]["draft"]
        if case == "resource_kind":
            draft["kind"] = "integration"
        elif case == "resource_connection":
            draft["settings"]["connection_id"] = str(uuid4())
        else:
            draft["secret_refs"] = [{"destination": "EXTRA_CREDENTIAL"}]
    elif case == "metadata_missing":
        metadata = []
    elif case == "metadata_duplicate":
        metadata *= 2
    elif case.startswith("metadata_"):
        metadata[0][
            "workspace_id" if case == "metadata_foreign" else "availability"
        ] = str(uuid4()) if case == "metadata_foreign" else "unavailable"
    elif case == "wrong_create_revision":
        ids["profile_create_ref"]["revision"] = 2
    elif case == "wrong_final_revision":
        ids["profile_ref"]["revision"] = 3
    elif case == "missing_create_original":
        ids.pop("profile_create_operation_id")
    elif case == "changed_expected":
        ids["configuration_expected_ref"]["revision"] = 2
    else:
        ids["configuration_resource_ref"]["revision"] = 2
    client.overrides.update(
        {profile_path: profile, resource_path: resource, "/tool-connections": metadata}
    )
    if case in {
        "wrong_create_revision",
        "wrong_final_revision",
        "missing_create_original",
        "changed_expected",
        "changed_resource_pin",
    }:
        cli.Journal(value).append(
            "configuration", "passed", original_ids=ids, observed={"canonical": True}
        )
    calls = []
    report = cli.NativeCheck(
        cli.read_manifest(path), client=client, runner=lambda *args: calls.append(args)
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE" and calls == []


def target_configuration_ids(ids, workspace):
    ids.update(
        target_profile_id=str(uuid4()),
        target_profile_create_operation_id=str(uuid4()),
        target_configuration_operation_id=str(uuid4()),
    )
    ids["target_profile_create_ref"] = {
        "workspace_id": workspace,
        "profile_id": ids["target_profile_id"],
        "revision": 1,
    }
    ids["target_profile_ref"] = {**ids["target_profile_create_ref"], "revision": 2}
    ids["target_configuration_expected_ref"] = deepcopy(
        ids["target_profile_create_ref"]
    )
    ids["target_configuration_resource_ref"] = deepcopy(ids["task_tool_resource_ref"])


@pytest.mark.parametrize(
    "case",
    [
        "positive",
        "create_unknown",
        "bind_unknown",
        "missing_original",
        "binding",
        "profile",
        "revision",
        "expected",
    ],
)
def test_target_configuration_observation_requires_its_original_create_and_bind(
    manifest, case
):
    cli, path, value = manifest
    config = json.loads(Path(value["config_path"]).read_text())
    config["scenario"] = "transition"
    private_json(Path(value["config_path"]), config)
    value["config_hash"] = cli.fingerprint(config)
    private_json(Path(value["owner_receipt_path"]), cleanup_reference(value))
    private_json(path, value)
    journal, ids = completed(cli, value, through="configuration")
    target_configuration_ids(ids, value["workspace_id"])
    client = Client(value, ids)
    if case in {"create_unknown", "bind_unknown"}:
        operation = ids[
            "target_profile_create_operation_id"
            if case == "create_unknown"
            else "target_configuration_operation_id"
        ]
        suffix = "/configuration-requests?operationId=" + operation
        body = client.get(suffix)
        body["state"] = "unknown"
        client.overrides[suffix] = body
    elif case == "missing_original":
        ids.pop("target_profile_create_operation_id")
    elif case == "binding":
        suffix = "/profiles/" + ids["target_profile_id"] + "?revision=2"
        body = client.get(suffix)
        body["profile"]["draft"]["resource_refs"] = []
        client.overrides[suffix] = body
    elif case == "profile":
        ids["target_profile_ref"]["profile_id"] = str(uuid4())
    elif case == "revision":
        ids["target_profile_ref"]["revision"] = 3
    elif case == "expected":
        ids["target_configuration_expected_ref"]["revision"] = 2
    record = journal.append("configuration", "pending", original_ids=ids)
    assert cli.NativeCheck(cli.read_manifest(path), client=client)._observe(record) is (
        case == "positive"
    )
    if case == "positive":
        assert any(
            read.endswith("operationId=" + ids["target_profile_create_operation_id"])
            for read in client.reads
        )
        assert any(
            read.endswith("operationId=" + ids["target_configuration_operation_id"])
            for read in client.reads
        )


@pytest.mark.parametrize(
    ("through", "next_stage"),
    [
        ("configuration", "conversation"),
        ("goal_submitted", "proposal_ready"),
        ("proposal_ready", "plan_confirmed"),
        ("dependency_execution", "worker_input"),
        ("result_verified", "result_accepted"),
    ],
)
def test_resume_classifies_unrecorded_next_stage_as_fresh(
    manifest, monkeypatch, through, next_stage
):
    cli, path, value = manifest
    journal, ids = completed(cli, value, through=through)
    prior = journal.records()
    assert not any(record["step"] == next_stage for record in prior)
    captured, observed = [], []
    client = Client(value, ids)

    def stop_after_capture(step, record):
        captured.append(
            {
                "step": step,
                "record": deepcopy(record),
                "persisted": journal.records()[-1],
                "observed": list(observed),
                "reads": list(client.reads),
            }
        )
        raise OSError("Controlled stage capture complete")

    check = cli.NativeCheck(
        cli.read_manifest(path), client=client, runner=stop_after_capture
    )
    original_observe = check._observe

    def observe_original(record):
        observed.append(record["step"])
        return original_observe(record)

    monkeypatch.setattr(check, "_observe", observe_original)
    report = check.run(resume=True)
    assert report["reason"] == "stage_execution_failed"
    assert [item["step"] for item in captured] == [next_stage]
    intention = captured[0]
    assert intention["record"] == intention["persisted"]
    assert intention["record"]["status"] == "pending"
    assert intention["record"]["observed"]["resume"] is False
    assert intention["record"]["original_ids"] == ids
    assert intention["record"]["step_id"] not in {record["step_id"] for record in prior}
    assert UUID(intention["record"]["step_id"])
    assert intention["observed"] == [record["step"] for record in prior]
    assert intention["reads"]
    assert journal.records()[: len(prior)] == prior
    assert [record["status"] for record in journal.records()[len(prior) :]] == [
        "pending",
        "failed",
    ]
    assert journal.records()[-1]["step_id"] == intention["record"]["step_id"]


def test_resume_empty_history_requires_original_ids_before_runner(manifest):
    cli, path, value = manifest
    journal, client = cli.Journal(value), Client()
    calls = []
    report = cli.NativeCheck(
        cli.read_manifest(path), client=client, runner=lambda *args: calls.append(args)
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE"
    assert report["reason"] == "original_ids_required"
    assert calls == []
    assert client.reads == []
    assert journal.records() == []


@pytest.mark.parametrize("status", ["pending", "failed", "incomplete"])
@pytest.mark.parametrize("case", ["unknown", "unavailable", "absent_ids"])
def test_resume_existing_incomplete_stage_with_false_flag_never_becomes_fresh(
    manifest, monkeypatch, status, case
):
    cli, path, value = manifest
    journal, ids = completed(cli, value, through="configuration")
    saved_ids = dict(ids)
    if case == "absent_ids":
        saved_ids.pop("thread_id")
    journal.append(
        "conversation", status, original_ids=saved_ids, observed={"resume": False}
    )
    prior = journal.records()
    client, calls, observed = Client(value, ids), [], []
    check = cli.NativeCheck(
        cli.read_manifest(path), client=client, runner=lambda *args: calls.append(args)
    )
    original_observe = check._observe

    def observe_original(record):
        observed.append(record["step"])
        if record["step"] == "conversation":
            client.unknown = case == "unknown"
            client.unavailable = case == "unavailable"
        return original_observe(record)

    monkeypatch.setattr(check, "_observe", observe_original)
    report = check.run(resume=True)
    assert report["status"] == "INCOMPLETE"
    assert report["reason"] == (
        "original_read_or_journal_unavailable"
        if case == "unavailable"
        else "original_operation_incomplete"
    )
    assert observed == ["configuration", "conversation"]
    assert calls == []
    assert journal.records() == prior


def test_resume_reads_originals_and_never_replays_completed_producers(manifest):
    cli, path, value = manifest
    journal, ids = completed(cli, value)
    calls = []
    runner = lambda step, record: (
        calls.append(step) or {"canonical": True, "original_ids": ids}
    )
    client = Client(value, ids)
    report = cli.NativeCheck(cli.read_manifest(path), client=client, runner=runner).run(
        resume=True
    )
    assert report["functional"] is True
    assert calls == ["navigation_history", "owned_cleanup"]
    assert any(item.endswith("/commands/" + ids["command_id"]) for item in client.reads)
    assert any(
        item.endswith("/tasks/" + ids["root_task_id"] + "/results")
        for item in client.reads
    )
    assert any("native-context" in item for item in client.reads)
    assert len(journal.records()) >= len(cli.STAGES)


@pytest.mark.parametrize(
    "case", ["unknown", "unavailable", "absent_ids", "unknown_journal"]
)
def test_incomplete_original_stage_never_issues_new_identity(manifest, case):
    cli, path, value = manifest
    client = Client()
    ids = {"command_id": str(uuid4()), "root_task_id": str(uuid4())}
    journal = cli.Journal(value)
    journal.append(
        "configuration", "passed", original_ids=ids, observed={"canonical": True}
    )
    journal.append(
        "conversation", "pending", original_ids={} if case == "absent_ids" else ids
    )
    if case == "unknown":
        client.unknown = True
    if case == "unavailable":
        client.unavailable = True
    if case == "unknown_journal":
        record = journal.records()[-1]
        record["status"] = "unknown"
        private_json(Path(value["checkpoint_path"]) / "foreign-status.json", record)
    calls = []
    report = cli.NativeCheck(
        cli.read_manifest(path), client=client, runner=lambda *args: calls.append(args)
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE"
    assert calls == []
    assert "PRIVATE" not in json.dumps(report)


def test_ui_source_change_invalidates_navigation_only_and_preserves_failure_history(
    manifest,
):
    cli, path, value = manifest
    journal, ids = completed(cli, value, through="navigation_history")
    journal.append(
        "navigation_history",
        "failed",
        original_ids=ids,
        observed={"reason": "old_ui_assertion"},
    )
    ui = next(name for name in cli.SOURCE_PATHS if name.startswith("apps/web/"))
    (Path(value["source_root"]) / ui).write_text("repaired UI assertion\n")
    selected = cli.read_manifest(path, allow_ui_change=True)
    calls = []
    report = cli.NativeCheck(
        selected,
        client=Client(value, ids),
        runner=lambda step, record: (
            calls.append(step) or {"canonical": True, "original_ids": ids}
        ),
    ).run(resume=True)
    assert report["functional"] is True
    assert calls == ["navigation_history", "owned_cleanup"]
    assert any(
        record["status"] == "failed"
        and record["observed"].get("reason") == "old_ui_assertion"
        for record in journal.records()
    )
    assert not any(
        record["status"] == "invalidated" and record["step"] != "navigation_history"
        for record in journal.records()
    )


def test_pending_intention_is_persisted_before_runner_and_failure_is_retained(manifest):
    cli, path, value = manifest
    journal = cli.Journal(value)

    def fail(step, record):
        assert journal.records()[-1]["status"] == "pending"
        assert record["step_id"] == journal.records()[-1]["step_id"]
        raise OSError("PRIVATE subprocess failure")

    report = cli.NativeCheck(
        cli.read_manifest(path), client=Client(), runner=fail
    ).run()
    assert report["status"] == "INCOMPLETE" and not report["functional"]
    assert journal.records()[-1]["status"] == "failed"
    assert "PRIVATE" not in json.dumps(report)


def test_cleanup_is_separate_from_functional_and_idempotent(manifest):
    cli, path, value = manifest
    check = cli.NativeCheck(
        cli.read_manifest(path), client=Client(), runner=lambda *_: {"canonical": True}
    )
    assert check.cleanup(wait_seconds=0)["cleanup"] is False
    assert check.cleanup(wait_seconds=0)["functional"] is False
    private_json(Path(value["cleanup_path"]) / "owner-proof.json", cleanup_proof(value))
    result = check.cleanup(wait_seconds=0)
    assert result["cleanup"] is True and result["functional"] is False
    assert check.cleanup(wait_seconds=0) == result


def test_cleanup_requests_one_original_owner_then_only_observes_scoped_proof(
    manifest, monkeypatch
):
    cli, path, value = manifest
    completed(cli, value, through="navigation_history")
    client = Client()
    client.get = lambda *_: pytest.fail("Cleanup issued a public read")
    check = cli.NativeCheck(
        cli.read_manifest(path),
        client=client,
        runner=lambda *_: pytest.fail("Cleanup invoked a business runner"),
    )
    monkeypatch.setattr(
        cli.os, "kill", lambda *_: pytest.fail("Cleanup signalled a PID")
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Cleanup allocated a process"),
    )
    pending = check.cleanup(wait_seconds=0)
    assert pending["reason"] == "owner_cleanup_pending"
    assert pending["functional"] is True and pending["cleanup"] is False
    request = Path(value["owner_receipt_path"]).parent / "owner-cleanup-request.json"
    assert json.loads(request.read_text()) == cleanup_reference(value)
    assert (request.stat().st_mode & 0o777) == 0o600
    identity, body = request.stat().st_ino, request.read_bytes()
    monkeypatch.setattr(
        cli,
        "private_write",
        lambda *_: pytest.fail("Cleanup replaced an original request"),
    )
    assert check.cleanup(wait_seconds=0) == pending
    private_json(Path(value["cleanup_path"]) / "owner-proof.json", cleanup_proof(value))
    finished = check.cleanup(wait_seconds=0)
    assert finished["reason"] == "owned_cleanup_confirmed"
    assert finished["functional"] is finished["cleanup"] is True
    assert request.stat().st_ino == identity and request.read_bytes() == body
    assert client.reads == []


@pytest.mark.parametrize("custody", ["caller", "lab"])
@pytest.mark.parametrize("complete", [False, True])
def test_cleanup_reads_existing_final_proof_without_request_or_retry(
    manifest, custody, complete, monkeypatch
):
    cli, path, value = manifest
    value["guard_custody"] = custody
    private_json(path, value)
    completed(cli, value, through="navigation_history")
    check = cli.NativeCheck(cli.read_manifest(path))
    proof = Path(value["cleanup_path"]) / "owner-proof.json"
    private_json(proof, cleanup_proof(value, complete=complete))
    original = proof.read_bytes()
    Path(value["owner_receipt_path"]).unlink()
    monkeypatch.setattr(
        cli,
        "private_write",
        lambda *_: pytest.fail("Existing cleanup proof was retried"),
    )
    observed = check.cleanup(wait_seconds=0)
    assert observed["functional"] is True and observed["cleanup"] is complete
    assert observed["reason"] == (
        "owned_cleanup_confirmed" if complete else "owner_cleanup_incomplete"
    )
    assert check.cleanup(wait_seconds=0) == observed
    assert proof.read_bytes() == original
    assert not (path.parent / "owner-cleanup-request.json").exists()


@pytest.mark.parametrize("plane", ["owner", "request", "proof"])
@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "owner_uid",
        "allocation_id",
        "workspace_id",
        "namespace",
        "config_hash",
        "owner_pid",
        "operation_id",
        "schema_bool",
        "uid_bool",
        "pid_bool",
    ],
)
def test_cleanup_refuses_foreign_reference_before_any_request(
    manifest, plane, field, monkeypatch
):
    cli, path, value = manifest
    completed(cli, value, through="navigation_history")
    check = cli.NativeCheck(cli.read_manifest(path))
    selected = (
        Path(value["owner_receipt_path"])
        if plane == "owner"
        else path.parent / "owner-cleanup-request.json"
        if plane == "request"
        else Path(value["cleanup_path"]) / "owner-proof.json"
    )
    body = cleanup_proof(value) if plane == "proof" else cleanup_reference(value)
    if field.endswith("_bool"):
        body[
            {
                "schema_bool": "schema_version",
                "uid_bool": "owner_uid",
                "pid_bool": "owner_pid",
            }[field]
        ] = True
    else:
        body[field] = body[field] + 1 if type(body[field]) is int else str(uuid4())
    private_json(selected, body)
    monkeypatch.setattr(
        cli,
        "private_write",
        lambda *_: pytest.fail("Refused reference caused a new request"),
    )
    result = check.cleanup(wait_seconds=0)
    assert result["status"] == "INCOMPLETE" and result["cleanup"] is False
    assert result["functional"] is True
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("plane", ["owner", "request", "proof"])
@pytest.mark.parametrize("unsafe", ["symlink", "permissive", "malformed", "oversize"])
def test_cleanup_refuses_unsafe_private_reference_without_new_write(
    manifest, plane, unsafe, monkeypatch
):
    cli, path, value = manifest
    completed(cli, value, through="navigation_history")
    check = cli.NativeCheck(cli.read_manifest(path))
    selected = (
        Path(value["owner_receipt_path"])
        if plane == "owner"
        else path.parent / "owner-cleanup-request.json"
        if plane == "request"
        else Path(value["cleanup_path"]) / "owner-proof.json"
    )
    private_json(
        selected, cleanup_proof(value) if plane == "proof" else cleanup_reference(value)
    )
    if unsafe == "symlink":
        target = selected.parent / "original-reference.json"
        selected.rename(target)
        selected.symlink_to(target)
    elif unsafe == "permissive":
        selected.chmod(0o644)
    elif unsafe == "malformed":
        selected.write_bytes(b"PRIVATE malformed cleanup")
    else:
        selected.write_bytes(b" " * 2097153)
    monkeypatch.setattr(
        cli,
        "private_write",
        lambda *_: pytest.fail("Unsafe reference caused a request"),
    )
    result = check.cleanup(wait_seconds=0)
    assert result["status"] == "INCOMPLETE" and result["functional"] is True
    assert not result["cleanup"] and "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "wait", [-1, float("inf"), float("-inf"), float("nan"), True, None]
)
def test_cleanup_refuses_nonfinite_negative_or_untyped_wait(manifest, wait):
    cli, path, _ = manifest
    with pytest.raises(cli.ManifestError, match="cleanup_wait_refused"):
        cli.NativeCheck(cli.read_manifest(path)).cleanup(wait_seconds=wait)
    assert not (path.parent / "owner-cleanup-request.json").exists()


def test_cleanup_wait_observes_only_the_exact_owner_proof(manifest, monkeypatch):
    cli, path, value = manifest
    check = cli.NativeCheck(cli.read_manifest(path))
    clock = [0.0]
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0], raising=False)

    def owner_observation(delay):
        clock[0] += delay
        private_json(
            Path(value["cleanup_path"]) / "owner-proof.json", cleanup_proof(value)
        )

    monkeypatch.setattr(cli, "sleep", owner_observation, raising=False)
    result = check.cleanup(wait_seconds=0.5)
    assert result["cleanup"] is True and result["functional"] is False
    assert json.loads(
        (path.parent / "owner-cleanup-request.json").read_text()
    ) == cleanup_reference(value)


def test_cleanup_racing_original_request_is_validated_not_overwritten(
    manifest, monkeypatch
):
    cli, path, value = manifest
    check = cli.NativeCheck(cli.read_manifest(path))

    def raced(selected, body):
        private_json(selected, body)
        raise FileExistsError("Owned original request already exists")

    monkeypatch.setattr(cli, "private_write", raced)
    result = check.cleanup(wait_seconds=0)
    assert result["reason"] == "owner_cleanup_pending"
    assert json.loads(
        (path.parent / "owner-cleanup-request.json").read_text()
    ) == cleanup_reference(value)


@pytest.mark.parametrize("custody", ["caller", "lab"])
def test_cleanup_requires_actual_guard_custody_proof(manifest, custody):
    cli, path, value = manifest
    value["guard_custody"] = custody
    private_json(path, value)
    proof = cleanup_proof(value)
    proof["cleanup"]["guards_inspected"] = True if custody == "caller" else None
    private_json(Path(value["cleanup_path"]) / "owner-proof.json", proof)
    result = cli.NativeCheck(cli.read_manifest(path)).cleanup(wait_seconds=0)
    assert result["reason"] == "owner_cleanup_incomplete" and result["cleanup"] is False
    assert not (path.parent / "owner-cleanup-request.json").exists()


@pytest.mark.parametrize("flags", [None, False, [], "unknown"])
def test_cleanup_malformed_observed_flags_are_refused_without_new_request(
    manifest, flags
):
    cli, path, value = manifest
    completed(cli, value, through="navigation_history")
    proof = cleanup_proof(value)
    proof["cleanup"] = flags
    private_json(Path(value["cleanup_path"]) / "owned.json", proof)
    result = cli.NativeCheck(cli.read_manifest(path)).cleanup(wait_seconds=0)
    assert (
        result["status"] == "INCOMPLETE"
        and result["functional"] is True
        and result["cleanup"] is False
    )
    assert not (path.parent / "owner-cleanup-request.json").exists()


def test_real_mode_never_enables_controlled_bridge_or_provider_runner(manifest):
    cli, path, value = manifest
    value.update(mode="real", controlled_bridge=False, controller_url=None)
    private_json(path, value)
    calls = []
    report = cli.NativeCheck(
        cli.read_manifest(path),
        client=Client(),
        runner=lambda *args: calls.append(args),
    ).run()
    assert report["reason"] == "provider_mode_not_authorized" and calls == []


def test_import_does_not_execute_cli(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: pytest.fail("Import started a process"),
    )
    assert module().main


def test_preflight_consumes_actual_setup_get_without_post_status(manifest):
    cli, path, value = manifest
    _, ids = completed(cli, value)
    client = Client(value, ids)
    assert "status" not in client.get(
        f"/product/v1/workspaces/{value['workspace_id']}/setup"
    )
    assert (
        cli.NativeCheck(cli.read_manifest(path), client=client).preflight()["status"]
        == "READY"
    )


@pytest.mark.parametrize(
    "case",
    [
        "null_setup",
        "foreign_setup",
        "missing_repository",
        "missing_policy",
        "invalid_policy_version",
        "foreign_planner_workspace",
        "foreign_planner_profile",
        "foreign_defaults",
        "mismatched_defaults",
    ],
)
def test_preflight_refuses_foreign_or_unconfigured_actual_setup(manifest, case):
    cli, path, value = manifest
    _, ids = completed(cli, value)
    client = Client(value, ids)
    setup = client.get(f"/product/v1/workspaces/{value['workspace_id']}/setup")
    defaults = client.get(f"/product/v1/workspaces/{value['workspace_id']}/defaults")
    if case == "null_setup":
        setup = None
    elif case == "foreign_setup":
        setup["workspace_id"] = str(uuid4())
    elif case == "missing_repository":
        setup["repository_ref"] = None
    elif case == "missing_policy":
        setup["planning_policy_ref"] = None
    elif case == "invalid_policy_version":
        setup["planning_policy_ref"]["version"] = 0
    elif case == "foreign_planner_workspace":
        setup["planner_agent_ref"]["workspace_id"] = str(uuid4())
    elif case == "foreign_planner_profile":
        setup["planner_agent_ref"]["profile_id"] = str(uuid4())
    elif case == "foreign_defaults":
        defaults["workspace_id"] = str(uuid4())
    elif case == "mismatched_defaults":
        defaults["worker_profile_id"] = str(uuid4())
    client.overrides.update({"/setup": setup, "/defaults": defaults})
    report = cli.NativeCheck(cli.read_manifest(path), client=client).preflight()
    assert report["status"] == "INCOMPLETE"
    if case == "null_setup":
        assert report["reason"] == "workspace_setup_required"


@pytest.mark.parametrize(
    "case",
    [
        "foreign_root",
        "foreign_result_workspace",
        "foreign_planning_id",
        "foreign_planning_workspace",
        "foreign_worker",
        "foreign_context_workspace",
        "absent_run_no_history",
        "wrong_history_run",
        "wrong_history_execution",
        "wrong_history_profile",
        "wrong_generation",
        "wrong_binding",
        "pending_command",
        "unavailable",
        "wrong_turn",
        "wrong_turn_operation",
        "wrong_thread",
        "wrong_candidate",
        "wrong_verification",
        "failed_check",
        "wrong_commit",
        "wrong_tree",
    ],
)
def test_resume_refuses_mismatched_actual_original_reads_without_runner(manifest, case):
    cli, path, value = manifest
    _, ids = completed(cli, value)
    client = Client(value, ids)
    suffix = "/results"
    body = client.get("/tasks/root" + suffix)
    if case == "foreign_root":
        body["root_task_id"] = str(uuid4())
    elif case == "foreign_result_workspace":
        body["workspace_id"] = str(uuid4())
    elif case.startswith("foreign_planning"):
        suffix = "/planning-requests/" + ids["planning_request_id"]
        body = client.get(suffix)
        body["id" if case.endswith("id") else "workspace_id"] = str(uuid4())
    elif case.startswith("foreign_worker") or case in {
        "foreign_context_workspace",
        "wrong_generation",
        "wrong_binding",
    }:
        suffix = "/native-context"
        body = client.get(suffix)
        if case == "foreign_worker":
            body["worker_item_id"] = str(uuid4())
        elif case == "foreign_context_workspace":
            body["workspace_id"] = str(uuid4())
        else:
            body["context"][
                "plan_generation" if case == "wrong_generation" else "binding_version"
            ] = 999
    elif case in {
        "absent_run_no_history",
        "wrong_history_run",
        "wrong_history_execution",
        "wrong_history_profile",
    }:
        context = client.get("/native-context")
        context.update(run=None, execution=None, context=None)
        client.overrides["/native-context"] = context
        suffix = "/native-trace"
        body = client.get(suffix)
        if case == "absent_run_no_history":
            body = OSError("original trace unavailable")
        elif case == "wrong_history_profile":
            body["profile_ref"]["profile_id"] = str(uuid4())
        else:
            body["run_id" if case == "wrong_history_run" else "execution_id"] = str(
                uuid4()
            )
    elif case == "pending_command":
        client.unknown = True
    elif case == "unavailable":
        client.unavailable = True
    elif case.startswith("wrong_turn"):
        suffix = "/turns/" + ids["turn_id"]
        body = client.get(suffix)
        body["id" if case == "wrong_turn" else "operationId"] = str(uuid4())
    elif case == "wrong_thread":
        suffix = "/threads/" + ids["thread_id"]
        body = client.get(suffix)
        body["id"] = str(uuid4())
    elif case == "wrong_candidate":
        body["current_candidate"]["candidate_id"] = str(uuid4())
    else:
        suffix = "/results/" + ids["candidate_id"]
        body = client.get(suffix)
        selected = body["verifications"][0]
        if case == "wrong_verification":
            selected["verification_id"] = str(uuid4())
        elif case == "failed_check":
            selected["status"] = "failed"
        else:
            selected["actual_commit" if case == "wrong_commit" else "actual_tree"] = (
                "f" * 40
            )
    client.overrides[suffix] = body
    calls = []
    report = cli.NativeCheck(
        cli.read_manifest(path), client=client, runner=lambda *args: calls.append(args)
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE" and calls == []


@pytest.mark.parametrize(
    "case",
    ["working", "no_acceptance", "wrong_candidate", "wrong_hash", "wrong_command"],
)
def test_resume_acceptance_requires_original_finalization_not_applied_command(
    manifest, case
):
    cli, path, value = manifest
    _, ids = completed(cli, value)
    client = Client(value, ids)
    result = client.get("/results")
    if case == "working":
        result["task_status"] = "working"
    elif case == "no_acceptance":
        result["acceptance"] = None
    else:
        result["acceptance"][
            {
                "wrong_candidate": "candidate_id",
                "wrong_hash": "candidate_hash",
                "wrong_command": "command_id",
            }[case]
        ] = str(uuid4())
    client.overrides["/results"] = result
    calls = []
    report = cli.NativeCheck(
        cli.read_manifest(path), client=client, runner=lambda *args: calls.append(args)
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE" and calls == []


@pytest.mark.parametrize("current", ["absent", "changed"])
def test_resume_uses_exact_historical_native_trace_if_original_not_current(
    manifest, current
):
    cli, path, value = manifest
    _, ids = completed(cli, value)
    client = Client(value, ids)
    context = client.get("/native-context")
    if current == "absent":
        context.update(run=None, execution=None, context=None)
    else:
        context["execution"]["run_id"] = context["context"]["run_id"] = str(uuid4())
        context["execution"]["execution_id"] = context["context"]["execution_id"] = str(
            uuid4()
        )
    client.overrides["/native-context"] = context
    calls = []
    report = cli.NativeCheck(
        cli.read_manifest(path),
        client=client,
        runner=lambda step, record: (
            calls.append(step) or {"canonical": True, "original_ids": ids}
        ),
    ).run(resume=True)
    assert report["functional"] is True and calls == [
        "navigation_history",
        "owned_cleanup",
    ]
    assert any(
        item.endswith(f"/executions/{ids['source_execution_id']}/native-trace")
        for item in client.reads
    )


@pytest.mark.parametrize(
    "step",
    [
        "conversation",
        "goal_submitted",
        "plan_confirmed",
        "worker_input",
        "result_accepted",
    ],
)
def test_pending_different_mutation_cannot_reuse_prior_stage_identity(manifest, step):
    cli, path, value = manifest
    journal, ids = completed(cli, value, through=cli.STAGES[cli.STAGES.index(step) - 1])
    missing = {
        "conversation": ["thread_id"],
        "goal_submitted": ["turn_id", "turn_operation_id"],
        "plan_confirmed": ["confirmation_command_id"],
        "worker_input": ["input_command_id"],
        "result_accepted": ["acceptance_command_id"],
    }[step]
    pending_ids = {key: selected for key, selected in ids.items() if key not in missing}
    journal.append(step, "pending", original_ids=pending_ids)
    calls = []
    report = cli.NativeCheck(
        cli.read_manifest(path),
        client=Client(value, ids),
        runner=lambda *args: calls.append(args),
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE" and calls == []


@pytest.mark.parametrize("resume", [False, True])
def test_focused_transition_cli_refuses_main_runner_without_public_call(
    manifest, resume
):
    cli, path, value = manifest
    config = json.loads(Path(value["config_path"]).read_text())
    config["scenario"] = "transition"
    private_json(Path(value["config_path"]), config)
    value["config_hash"] = cli.fingerprint(config)
    private_json(Path(value["owner_receipt_path"]), cleanup_reference(value))
    private_json(path, value)
    calls = []
    client = SimpleNamespace(
        get=lambda *_: pytest.fail("focused case public read from main runner")
    )
    result = cli.NativeCheck(
        cli.read_manifest(path), client=client, runner=lambda *args: calls.append(args)
    ).run(resume=resume)
    assert (
        result["status"] == "INCOMPLETE"
        and result["reason"] == "focused_case_requires_explicit_desktop_entry"
    )
    assert calls == [] and list(Path(value["checkpoint_path"]).iterdir()) == []


def test_desktop_subprocess_budget_is_finite_and_preserves_original_stage(
    manifest, monkeypatch
):
    cli, path, _ = manifest
    calls = []
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda _: "/usr/bin/node",
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs)) or SimpleNamespace(returncode=1)
        ),
    )
    record = {"step_id": str(uuid4()), "observed": {"resume": True}}
    with pytest.raises(cli.ManifestError, match="desktop_stage_failed"):
        cli.NativeCheck(cli.read_manifest(path))._desktop_step("worker_input", record)
    assert len(calls) == 1 and calls[0][1]["timeout"] == 3660
    assert calls[0][1]["env"]["SIDERA_NATIVE_STAGE"] == "worker_input"
    assert calls[0][1]["env"]["SIDERA_NATIVE_STEP_ID"] == record["step_id"]


@pytest.mark.parametrize(
    "case",
    [
        "positive",
        "missing_original",
        "workspace",
        "worker",
        "execution",
        "run",
        "source_profile",
        "target_profile",
        "binding",
        "generation",
        "target_execution",
        "target_run",
        "prepare_result",
        "confirm_result",
        "configuration",
    ],
)
def test_focused_observation_requires_exact_blocked_original_transition(manifest, case):
    cli, path, value = manifest
    config = json.loads(Path(value["config_path"]).read_text())
    config["scenario"] = "transition"
    private_json(Path(value["config_path"]), config)
    value["config_hash"] = cli.fingerprint(config)
    private_json(Path(value["owner_receipt_path"]), cleanup_reference(value))
    private_json(path, value)
    journal, ids = completed(cli, value, through="dependency_execution")
    ids.update(
        {
            key: str(uuid4())
            for key in (
                "source_session_id",
                "target_profile_id",
                "target_configuration_operation_id",
                "handoff_prepare_command_id",
                "handoff_confirm_command_id",
                "handoff_proposal_id",
                "handoff_transition_id",
            )
        }
    )
    ids["source_context_version"] = 1
    target_configuration_ids(ids, value["workspace_id"])
    source = {
        "workspace_id": value["workspace_id"],
        "worker_item_id": ids["source_worker_item_id"],
        "source_execution_id": ids["source_execution_id"],
        "source_run_id": ids["source_run_id"],
        "source_session_id": ids["source_session_id"],
        "source_profile_ref": ids["profile_ref"],
        "source_binding_version": ids["binding_version"],
        "target_profile_ref": ids["target_profile_ref"],
        "target_assignee": {"kind": "profile", "id": ids["target_profile_id"]},
        "context": {"source_context_version": 1},
        "plan_base": {"generation": ids["plan_generation"]},
    }
    proposal = {
        **deepcopy(source),
        "id": ids["handoff_proposal_id"],
        "root_task_id": ids["root_task_id"],
        "proposal_schema_version": 2,
    }
    transition = {
        **deepcopy(source),
        "id": ids["handoff_transition_id"],
        "proposal_id": ids["handoff_proposal_id"],
        "handoff_schema_version": 2,
        "failure_code": "source_stop_unconfirmed",
        "target_execution_id": None,
        "target_run_id": None,
        "available_recovery_strategies": [],
    }

    def command(key, aggregate):
        return {
            "command_id": ids[key],
            "status": "applied",
            "result": {"command_id": ids[key], "aggregate_id": aggregate},
        }

    prepare = command("handoff_prepare_command_id", ids["handoff_proposal_id"])
    confirm = command("handoff_confirm_command_id", ids["handoff_transition_id"])
    configured = {
        "operationId": ids["target_configuration_operation_id"],
        "state": "succeeded",
        "result_ref": ids["target_profile_ref"],
        "target_ref": ids["target_profile_ref"],
    }
    if case == "missing_original":
        ids.pop("handoff_transition_id")
    elif case in {"workspace", "worker", "execution", "run"}:
        field = {
            "workspace": "workspace_id",
            "worker": "worker_item_id",
            "execution": "source_execution_id",
            "run": "source_run_id",
        }[case]
        transition[field] = str(uuid4())
    elif case in {"source_profile", "target_profile"}:
        transition[case + "_ref"] = {**ids["profile_ref"], "profile_id": str(uuid4())}
    elif case == "binding":
        transition["source_binding_version"] = 2
    elif case == "generation":
        transition["plan_base"]["generation"] = 2
    elif case in {"target_execution", "target_run"}:
        transition[case + "_id"] = str(uuid4())
    elif case == "prepare_result":
        prepare["result"]["aggregate_id"] = str(uuid4())
    elif case == "confirm_result":
        confirm["result"]["aggregate_id"] = str(uuid4())
    elif case == "configuration":
        configured["state"] = "unknown"
    overrides = {
        "handoff-proposals/" + proposal["id"]: proposal,
        "handoff-transitions/" + transition["id"]: transition,
        "commands/" + prepare["command_id"]: prepare,
        "commands/" + confirm["command_id"]: confirm,
        "configuration-requests?operationId="
        + ids["target_configuration_operation_id"]: configured,
        "profiles/" + ids["target_profile_id"] + "?revision=2": {
            "profile": {
                "ref": ids["target_profile_ref"],
                "draft": {
                    "runtime_ref": {
                        "workspace_id": value["workspace_id"],
                        "runtime_id": ids["runtime_id"],
                    },
                    "resource_refs": [ids["task_tool_resource_ref"]],
                },
            },
            "status": {"profile_id": ids["target_profile_id"], "enabled": True},
        },
    }
    record = journal.append(
        "dependency_execution",
        "pending",
        original_ids=ids,
        observed={"focused_transition": True},
    )
    client = Client(value, ids)
    client.overrides.update(overrides)
    assert cli.NativeCheck(cli.read_manifest(path), client=client)._observe(record) is (
        case == "positive"
    )
