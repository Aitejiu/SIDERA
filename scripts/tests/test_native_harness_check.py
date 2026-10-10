import hashlib
import importlib.util
import json
import os
import sys
from copy import deepcopy
from datetime import UTC, datetime
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
        value,
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


REAL_EXTRA_SOURCES = (
    "apps/server/tests/support/real_native_product_flow.py",
    "apps/server/tests/unit/test_real_native_product_flow.py",
    "apps/web/playwright.native-real.config.ts",
    "apps/web/tests/e2e/native-harness-real.spec.ts",
    "packages/agents/src/sidera_agents/harnesses/opencode.py",
    "packages/agents/src/sidera_agents/harnesses/acp_transport.py",
    "packages/agents/src/sidera_agents/harnesses/opencode_config.py",
    "packages/agents/src/sidera_agents/harnesses/opencode_descriptor.py",
    "apps/server/src/sidera_product/composition.py",
    "apps/server/src/sidera_product/chat/projection.py",
    "apps/server/src/sidera_product/task_tools/tools.py",
    "services/control-plane/src/sidera_control_plane/application/agent_worker_adapter.py",
    "services/control-plane/src/sidera_control_plane/application/agent_planner_adapter.py",
)


@pytest.fixture
def real_manifest(manifest):
    cli, path, value = manifest
    executable = path.parent / "private-native-bytes"
    executable.write_bytes(b"NEVER INVOKE THIS NATIVE BYTE FIXTURE\n")
    executable.chmod(0o700)
    for name in REAL_EXTRA_SOURCES:
        source = Path(value["source_root"]) / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("private real source fixture\n")
        value["source_pins"][name] = {
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "scope": "ui" if name.startswith("apps/web/") else "producer",
        }
    config = json.loads(Path(value["config_path"]).read_text())
    config["planning_budget"] = {
        "max_context_bytes": 1048576,
        "max_tokens": 200000,
        "max_cost_microunits": 1000000,
        "max_runtime_seconds": 3600,
        "max_revisions": 16,
        "max_nodes": 64,
        "max_concurrency": 4,
    }
    config["settings"].update(
        executable=str(executable), harness_id="opencode", max_active=4, max_sessions=8
    )
    config["real"] = {
        "schema_version": 1,
        "explicit_provider_run": True,
        "model_id": "opencode-go/deepseek-v4.1-flash",
        "provider_env": "OPENCODE_API_KEY",
        "credential_source_alias": "SIDERA_N23_OPENCODE_API_KEY",
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "actual_runtime_version": "1.18.32",
        "verification_python": sys.executable,
        "budget": {
            "total_seconds": 7200,
            "main_turns": 40,
            "planner_runs": 16,
            "worker_sessions": 64,
            "adapter_prompts": 200,
        },
    }
    value.update(mode="real", controlled_bridge=False, controller_url=None)
    private_json(
        Path(value["transport_path"]),
        {
            "schema_version": 1,
            "workspace_id": value["workspace_id"],
            "bootstrap_file": str(path.parent / "bootstrap" / "session.json"),
        },
    )
    save_real_config(cli, path, value, config)
    return cli, path, value


def save_real_config(cli, path, value, config):
    private_json(Path(value["config_path"]), config)
    value["config_hash"] = cli.fingerprint(config)
    private_json(Path(value["owner_receipt_path"]), cleanup_reference(value))
    private_json(path, value)


@pytest.mark.parametrize("reader", ["config", "manifest"])
@pytest.mark.parametrize("bound", [None, 1, 9007199254740991])
def test_real_planning_budget_accepts_complete_producer_shape(
    real_manifest, reader, bound
):
    cli, path, value = real_manifest
    config = json.loads(Path(value["config_path"]).read_text())
    if bound is not None:
        config["planning_budget"] = dict.fromkeys(config["planning_budget"], bound)
        save_real_config(cli, path, value, config)
    before = deepcopy(config)
    config_bytes = Path(value["config_path"]).read_bytes()
    manifest_bytes = path.read_bytes()
    refusal = None
    result = None
    try:
        result = (
            cli.validate_real_config(config)
            if reader == "config"
            else cli.read_manifest(path)
        )
    except cli.ManifestError as error:
        refusal = str(error)
    assert refusal is None, f"Producer-shaped budget refused by {reader}: {refusal}"
    if reader == "config":
        assert result == config["real"]
    else:
        assert result == {**value, "_source_changes": []}
    assert config == before
    assert config["planning_budget"]["max_tokens"] == (
        200000 if bound is None else bound
    )
    assert Path(value["config_path"]).read_bytes() == config_bytes
    assert path.read_bytes() == manifest_bytes


def assert_real_config_refused_before_effects(
    cli, path, value, config, monkeypatch, capsys, *, reason="real_entry_refused"
):
    save_real_config(cli, path, value, config)
    with pytest.raises(cli.ManifestError, match=reason):
        cli.validate_real_config(config)

    def refuse(*args, **kwargs):
        pytest.fail("Invalid real configuration crossed a side-effect boundary")

    monkeypatch.setattr(cli, "NativeCheck", refuse)
    monkeypatch.setattr(cli, "build_opener", refuse)
    monkeypatch.setattr(cli, "private_write", refuse)
    monkeypatch.setattr(cli.subprocess, "run", refuse)
    monkeypatch.setattr(cli.subprocess, "Popen", refuse)
    assert cli.main(["run", "--manifest", str(path)]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "INCOMPLETE",
        "reason": "manifest_refused",
        "functional": False,
        "cleanup": False,
    }
    assert list(Path(value["checkpoint_path"]).iterdir()) == []
    assert list(Path(value["cleanup_path"]).iterdir()) == []


@pytest.mark.parametrize(
    "bad",
    [
        "missing_budget",
        "budget_null",
        "budget_bool",
        "budget_number",
        "budget_string",
        "budget_list",
        "missing_key",
        "extra_key",
    ],
)
def test_real_planning_budget_refuses_incomplete_or_foreign_structure(
    real_manifest, bad, monkeypatch, capsys
):
    cli, path, value = real_manifest
    config = json.loads(Path(value["config_path"]).read_text())
    if bad == "missing_budget":
        config.pop("planning_budget")
    elif bad == "missing_key":
        config["planning_budget"].pop("max_cost_microunits")
    elif bad == "extra_key":
        config["planning_budget"]["max_subtasks"] = 8
    else:
        config["planning_budget"] = {
            "budget_null": None,
            "budget_bool": True,
            "budget_number": 200000,
            "budget_string": "200000",
            "budget_list": [],
        }[bad]
    assert_real_config_refused_before_effects(
        cli, path, value, config, monkeypatch, capsys
    )


@pytest.mark.parametrize(
    "field",
    [
        "max_context_bytes",
        "max_tokens",
        "max_cost_microunits",
        "max_runtime_seconds",
        "max_revisions",
        "max_nodes",
        "max_concurrency",
    ],
)
@pytest.mark.parametrize(
    "invalid",
    [
        True,
        1.5,
        "200000",
        None,
        {"api_key": "PRIVATE-SENTINEL-NOT-A-KEY"},
        [{"token": "PRIVATE-SENTINEL-NOT-A-KEY"}],
        0,
        -1,
        9007199254740992,
    ],
    ids=[
        "bool",
        "float",
        "string",
        "null",
        "dict",
        "list",
        "zero",
        "negative",
        "unsafe",
    ],
)
def test_real_planning_budget_refuses_non_safe_positive_integer_values(
    real_manifest, field, invalid, monkeypatch, capsys
):
    cli, path, value = real_manifest
    config = json.loads(Path(value["config_path"]).read_text())
    config["planning_budget"][field] = invalid
    assert_real_config_refused_before_effects(
        cli, path, value, config, monkeypatch, capsys
    )


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("top", "max_tokens"),
        ("nested", "max_tokens"),
        ("nested_list", "max_tokens"),
        ("nested", "api_key"),
        ("budget", "api_key"),
        ("budget", "token"),
        ("budget", "cookie"),
        ("budget", "authorization"),
        ("budget", "bootstrap"),
        ("budget", "native_handle"),
        ("budget", "password"),
        ("budget", "secret"),
    ],
)
def test_real_planning_budget_does_not_exempt_secret_or_foreign_token_fields(
    real_manifest, location, field, monkeypatch, capsys
):
    cli, path, value = real_manifest
    config = json.loads(Path(value["config_path"]).read_text())
    item = 200000 if field == "max_tokens" else "PRIVATE-SENTINEL-NOT-A-KEY"
    if location == "top":
        config[field] = item
    elif location == "nested":
        config["settings"]["metadata"] = {field: item}
    elif location == "nested_list":
        config["settings"]["metadata"] = [{field: item}]
    else:
        config["planning_budget"][field] = item
    assert_real_config_refused_before_effects(
        cli,
        path,
        value,
        config,
        monkeypatch,
        capsys,
        reason=(
            "real_entry_refused"
            if location == "budget"
            else "checkpoint_secret_refused"
        ),
    )


def test_real_planning_budget_is_still_refused_as_generic_checkpoint_data(
    real_manifest,
):
    cli, _, value = real_manifest
    config = json.loads(Path(value["config_path"]).read_text())
    with pytest.raises(cli.ManifestError, match="checkpoint_secret_refused"):
        cli._safe_record(config["planning_budget"], reject_api_keys=True)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "unselected",
        "no_real",
        "controller",
        "route",
        "version",
        "hash",
        "missing_producer",
        "mixed",
        "extra_pin",
        "secret",
    ],
)
def test_real_manifest_admission_has_exact_selected_producer_set(real_manifest, bad):
    cli, path, value = real_manifest
    config = json.loads(Path(value["config_path"]).read_text())
    if bad == "unselected":
        config["real"]["explicit_provider_run"] = False
    elif bad == "no_real":
        config.pop("real")
    elif bad == "controller":
        value["controller_url"] = "http://127.0.0.1:43123"
    elif bad in {"route", "version", "hash"}:
        config["real"][
            {
                "route": "model_id",
                "version": "actual_runtime_version",
                "hash": "executable_sha256",
            }[bad]
        ] = "f" * 64 if bad == "hash" else "wrong"
    elif bad == "missing_producer":
        value["source_pins"].pop(
            "packages/agents/src/sidera_agents/harnesses/opencode.py"
        )
    elif bad == "mixed":
        value["source_pins"] = {
            name: pin
            for name, pin in value["source_pins"].items()
            if name in cli.SOURCE_PATHS
        }
    elif bad == "extra_pin":
        value["source_pins"]["unknown.py"] = {"scope": "producer", "sha256": "f" * 64}
    elif bad == "secret":
        config["real"]["api_key"] = "REAL-TEST-SENTINEL-NOT-A-KEY"
    save_real_config(cli, path, value, config)
    if bad is None:
        assert cli.read_manifest(path)["source_pins"] == value["source_pins"]
    else:
        with pytest.raises(cli.ManifestError):
            cli.read_manifest(path)


def test_real_manifest_without_explicit_selection_rejected_even_with_old_pins(manifest):
    cli, path, value = manifest
    value.update(mode="real", controlled_bridge=False, controller_url=None)
    private_json(path, value)
    with pytest.raises(cli.ManifestError):
        cli.read_manifest(path)


def real_planning_detail(workspace, ids):
    from sidera_control_plane.api.schemas import PlanningRequestDetailResponse
    from sidera_control_plane.application.planning_queries import (
        PlanningRequestDetailView,
        PlanningRequestSummaryView,
        PlanningUsageView,
    )

    now = datetime(2026, 10, 9, tzinfo=UTC)
    summary = PlanningRequestSummaryView(
        id=UUID(ids["planning_request_id"]),
        workspace_id=UUID(workspace),
        kind="create",
        status="proposal_ready",
        source_type="task_goal",
        source_ref=None,
        current_brief_revision=1,
        current_proposal_revision=ids["proposal_revision"],
        current_proposal_content_hash=ids["proposal_content_hash"],
        planning_profile_id=UUID(ids["planning_policy_id"]),
        planning_profile_version=1,
        profile_key=None,
        current_run_id=None,
        requires_attention=False,
        version=1,
        created_at=now,
        updated_at=now,
    )
    view = PlanningRequestDetailView(
        summary=summary,
        profile=None,
        active_run=None,
        pending_clarifications=(),
        usage=PlanningUsageView(None, None, None, None, None),
        last_change_cursor=0,
    )
    body = PlanningRequestDetailResponse.from_detail_view(view).model_dump(mode="json")
    assert "root_task_id" not in body
    return body


class RealClient(Client):
    def get(self, path):
        route = urlsplit(path).path
        for suffix, response in self.overrides.items():
            if path.endswith(suffix) or route.endswith(suffix):
                self.reads.append(path)
                if isinstance(response, Exception):
                    raise response
                return deepcopy(response)
        body = super().get(route if route.endswith("/native-trace") else path)
        if path in self.overrides or any(path.endswith(key) for key in self.overrides):
            return body
        ids, ws = self.ids, self.value["workspace_id"]
        if path.endswith("/setup"):
            from sidera_control_plane.adapters.git_workspace import (
                opaque_repository_ref,
            )

            body["repository_ref"] = opaque_repository_ref(
                Path(self.value["repository_directory"])
            )
        if path.endswith("/credentials"):
            return [
                {
                    "workspace_id": ws,
                    "credential_id": ids["credential_ref"]["credential_id"],
                    "alias": "SIDERA_N23_OPENCODE_API_KEY",
                    "source": "env",
                    "status": "active",
                    "version": 1,
                    "replaces_id": None,
                }
            ]
        if "/runtimes/" in path:
            body.update(
                harness_id="opencode",
                executable=self.config["settings"]["executable"],
                actual_version="1.18.32",
                protocol_state="ready",
                auth_state="not_tested",
                limits={"max_active": 4, "max_sessions": 8},
            )
        if "/profiles/" in path:
            body["profile"]["draft"].update(
                model_id="opencode-go/deepseek-v4.1-flash",
                native_settings={
                    "native_tools": "write",
                    "provider_env": "OPENCODE_API_KEY",
                },
                environment={},
                secret_refs=[
                    {
                        "credential_ref": ids["credential_ref"],
                        "destination": "OPENCODE_API_KEY",
                    }
                ],
            )
        if "/turns/" in path:
            body["responseId"] = ids["chat_response_id"]
            body["items"] = [
                {
                    "id": "product-receipt:" + ids["receipt_id"],
                    "type": "toolCall",
                    "data": {
                        "receiptSource": "sidera.product.task-receipt.v1",
                        "receiptId": ids["receipt_id"],
                        "name": "sidera_submit_goal",
                        "verified": True,
                        "outcome": "applied",
                        "commandId": ids["goal_command_id"],
                        "rootTaskId": ids["root_task_id"],
                        "planningRequestId": ids["planning_request_id"],
                        "runId": ids["chat_run_id"],
                        "responseId": ids["chat_response_id"],
                    },
                }
            ]
        if "/commands/" in path:
            body["result"] = {
                "command_id": path.rsplit("/", 1)[1],
                "aggregate_id": ids["root_task_id"],
                "aggregate_version": 1,
                "event_ids": [],
                "data": {},
            }
            if path.endswith(ids["goal_command_id"]):
                body["command_type"] = "SubmitTaskGoal"
                body["result"]["data"] = {
                    "root_task_id": ids["root_task_id"],
                    "planning_request_id": ids["planning_request_id"],
                }
        if "/proposals/" in path:
            return {
                "id": ids["proposal_id"],
                "planning_request_id": ids["planning_request_id"],
                "revision": ids["proposal_revision"],
                "content_hash": ids["proposal_content_hash"],
            }
        if (
            route
            == f"/api/v1/workspaces/{ws}/planning-requests/{ids['planning_request_id']}"
        ):
            return real_planning_detail(ws, ids)
        if path.endswith("/materialization"):
            body["worker_item_mapping"] = ids["worker_item_mapping"]
        if "/assignments/worker_item/" in path:
            item = path.rsplit("/", 1)[1]
            ref = (
                ids["target_profile_ref"]
                if item == ids["worker_item_mapping"]["tests"]
                else ids["profile_ref"]
            )
            return {
                "workspace_id": ws,
                "target_type": "worker_item",
                "target_id": item,
                "assignee": {"kind": "profile", "id": ref["profile_id"]},
                "profile_ref": ref,
                "version": 1,
                "runnable": True,
            }
        if path.endswith("/native-context"):
            body["execution"].update(
                profile_ref=ids["profile_ref"],
                binding_version=ids["binding_version"],
                event_cursor=2,
                root_task_id=ids["root_task_id"],
            )
            body["run"].update(
                runtime_ref={"workspace_id": ws, "runtime_id": ids["runtime_id"]},
                actual_runtime_version="1.18.32",
                selected_model="opencode-go/deepseek-v4.1-flash",
                observed_model=None,
            )
            body["context"].update(context_version=ids["source_context_version"])
        if "/agent-executions/" in path:
            return self.get("/native-context")["execution"]
        if route.endswith("/native-trace"):
            body.update(
                events=[
                    {
                        "run_id": ids["source_run_id"],
                        "sequence": 2,
                        "kind": "response.started",
                    }
                ],
                next_sequence=2,
            )
        if "/gateway/inputs?" in path:
            body["items"][0].update(
                workspace_id=ws,
                session_id=ids["source_session_id"],
                operation_id=ids["input_operation_id"],
            )
        return body


def real_completed(cli, value, *, through="navigation_history"):
    journal, ids = completed(cli, value, through="configuration")
    ids.update(
        {
            key: str(uuid4())
            for key in ("goal_command_id", "chat_response_id", "receipt_id")
        }
    )
    ids.update(
        credential_ref={
            "workspace_id": value["workspace_id"],
            "credential_id": str(uuid4()),
        },
        source_worker_key="cli",
        source_context_version=1,
        original_trace_cursor=2,
    )
    ids["source_session_id"] = str(uuid4())
    target_configuration_ids(ids, value["workspace_id"])
    ids["worker_item_mapping"] = {
        key: str(uuid4()) for key in ("contract", "cli", "tests", "integration")
    }
    ids["worker_item_mapping"]["cli"] = ids["source_worker_item_id"]
    for step in cli.STAGES[: cli.STAGES.index(through) + 1]:
        if step != "worker_input":
            journal.append(
                step, "passed", original_ids=ids, observed={"canonical": True}
            )
    client = RealClient(value, ids)
    client.config = json.loads(Path(value["config_path"]).read_text())
    return journal, ids, client


def real_before_goal(cli, value):
    _, ids, client = real_completed(cli, value, through="conversation")
    checkpoints = Path(value["checkpoint_path"]).with_name("recovery-checkpoints")
    checkpoints.mkdir(mode=0o700)
    value["checkpoint_path"] = str(checkpoints)
    early = deepcopy(ids)
    for key in (
        "goal_command_id",
        "root_task_id",
        "planning_request_id",
        "turn_id",
        "turn_operation_id",
        "chat_run_id",
        "chat_response_id",
        "receipt_id",
    ):
        early.pop(key)
    journal = cli.Journal(value)
    for step in ("configuration", "conversation"):
        journal.append(step, "passed", original_ids=early, observed={"canonical": True})
    return journal, ids, client


def checkpoint_bytes(value):
    return {
        path: path.read_bytes() for path in Path(value["checkpoint_path"]).iterdir()
    }


def test_real_planning_detail_observes_original_goal_from_public_projection(
    real_manifest,
):
    cli, _, value = real_manifest
    journal, ids, client = real_before_goal(cli, value)
    record = journal.append("goal_submitted", "failed", original_ids=ids)
    planning_route = (
        f"/api/v1/workspaces/{value['workspace_id']}/planning-requests/"
        + ids["planning_request_id"]
    )
    body = client.get(planning_route)
    assert body["id"] == ids["planning_request_id"]
    assert body["workspace_id"] == value["workspace_id"]
    assert body["kind"] == "create" and body["source_type"] == "task_goal"
    assert body["source_ref"] is None and "root_task_id" not in body
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()
    calls = []
    check = cli.NativeCheck(
        value, client=client, runner=lambda *args: calls.append(args)
    )
    assert check._observe_real(record) is True
    assert any(read.endswith("/turns/" + ids["turn_id"]) for read in client.reads)
    assert any(
        read.endswith("/commands/" + ids["goal_command_id"]) for read in client.reads
    )
    assert calls == [] and checkpoint_bytes(value) == before
    assert Path(value["config_path"]).read_bytes() == config_before


def test_real_planning_detail_resume_retains_failed_goal_and_admits_only_next_stage(
    real_manifest,
):
    cli, _, value = real_manifest
    journal, ids, client = real_before_goal(cli, value)
    failed = journal.append(
        "goal_submitted",
        "failed",
        original_ids=ids,
        observed={"reason": "lost_planning_detail_assertion"},
    )
    old_records = journal.records()
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()
    calls = []

    def later(step, record):
        calls.append(step)
        assert step == "proposal_ready"
        assert record["observed"]["resume"] is False
        assert record["original_ids"] == ids
        assert any(
            read.endswith("/commands/" + ids["goal_command_id"])
            for read in client.reads
        )
        assert any(
            read.endswith("/planning-requests/" + ids["planning_request_id"])
            for read in client.reads
        )
        raise RuntimeError("stop at the admitted proposal boundary")

    report = cli.NativeCheck(value, client=client, runner=later).run(resume=True)
    assert calls == ["proposal_ready"]
    assert report["status"] == "INCOMPLETE"
    assert report["reason"] == "stage_execution_failed"
    assert report["functional"] is report["whole_n23_accepted"] is False
    records = journal.records()
    assert records[: len(old_records)] == old_records
    added = records[len(old_records) :]
    assert [(record["step"], record["status"]) for record in added] == [
        ("goal_submitted", "passed"),
        ("proposal_ready", "pending"),
        ("proposal_ready", "failed"),
    ]
    assert added[0]["step_id"] == failed["step_id"]
    assert added[0]["original_ids"] == ids
    assert added[0]["observed"]["original_reconciled"] is True
    assert added[1]["step_id"] == added[2]["step_id"]
    assert all(record["original_ids"] == ids for record in added)
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert Path(value["config_path"]).read_bytes() == config_before


@pytest.mark.parametrize(
    ("document", "fields", "replacement"),
    [
        ("planning", ("id",), "foreign"),
        ("planning", ("id",), "missing"),
        ("planning", ("workspace_id",), "foreign"),
        ("planning", ("workspace_id",), "missing"),
        ("planning", ("kind",), "revise"),
        ("planning", ("kind",), "missing"),
        ("planning", ("source_type",), "plan_revision"),
        ("planning", ("source_type",), "missing"),
        ("command", ("result", "data", "root_task_id"), "foreign"),
        ("command", ("result", "data", "planning_request_id"), "foreign"),
        ("command", ("command_id",), "foreign"),
        ("command", ("result", "command_id"), "foreign"),
        ("command", ("command_type",), "ConfirmPlanningProposal"),
        ("command", ("result",), None),
        ("command", ("status",), "pending"),
        ("command", ("status",), "rejected"),
        ("command", ("status",), "unknown"),
        ("turn", ("id",), "foreign"),
        ("turn", ("items",), []),
        ("turn", ("items", 0, "data", "verified"), False),
        ("turn", ("items", 0, "data", "commandId"), "foreign"),
        ("turn", ("items", 0, "data", "rootTaskId"), "foreign"),
        ("turn", ("items", 0, "data", "planningRequestId"), "foreign"),
        ("turn", ("items", 0, "data", "runId"), "foreign"),
        ("turn", ("items", 0, "data", "responseId"), "foreign"),
        ("command", (), "unknown_read"),
        ("planning", (), "unknown_read"),
    ],
)
def test_real_planning_detail_refuses_incomplete_or_foreign_original_without_mutation(
    real_manifest, document, fields, replacement, monkeypatch
):
    cli, _, value = real_manifest
    journal, ids, client = real_before_goal(cli, value)
    record = journal.append("goal_submitted", "failed", original_ids=ids)
    prefix = f"/api/v1/workspaces/{value['workspace_id']}/"
    route = {
        "planning": prefix + "planning-requests/" + ids["planning_request_id"],
        "command": prefix + "commands/" + ids["goal_command_id"],
        "turn": f"/main-agent/v1/threads/{ids['thread_id']}/turns/{ids['turn_id']}",
    }[document]
    if not fields:
        client.overrides[route] = OSError("UNKNOWN original read")
    else:
        body = client.get(route)
        target = body
        for field in fields[:-1]:
            target = target[field]
        if replacement == "missing":
            target.pop(fields[-1])
        else:
            target[fields[-1]] = (
                str(uuid4()) if replacement == "foreign" else deepcopy(replacement)
            )
        client.overrides[route] = body
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()
    runner_calls, mutation_calls = [], []
    monkeypatch.setattr(cli, "private_write", lambda *args: mutation_calls.append(args))
    check = cli.NativeCheck(
        value, client=client, runner=lambda *args: runner_calls.append(args)
    )
    assert check._observe_real(record) is False
    report = check.run(resume=True)
    assert report["status"] == "INCOMPLETE"
    assert report["reason"] == "original_operation_incomplete"
    assert runner_calls == mutation_calls == []
    assert checkpoint_bytes(value) == before
    assert Path(value["config_path"]).read_bytes() == config_before


def real_legacy_configuration(cli, value, *, status="failed"):
    _, full_ids, client = real_completed(cli, value, through="configuration")
    ids = {
        key: deepcopy(item)
        for key, item in full_ids.items()
        if key.startswith(
            (
                "profile_",
                "target_profile_",
                "configuration_",
                "target_configuration_",
                "task_tool_",
            )
        )
        or key in {"runtime_id", "credential_ref", "planning_policy_id"}
    }
    checkpoints = Path(value["checkpoint_path"]).with_name("legacy-configuration")
    checkpoints.mkdir(mode=0o700)
    value["checkpoint_path"] = str(checkpoints)
    journal, step_id = cli.Journal(value), str(uuid4())
    primary_create = {
        key: deepcopy(item)
        for key, item in ids.items()
        if not key.startswith(("configuration_", "target_"))
    }
    primary_create["profile_ref"] = deepcopy(ids["profile_create_ref"])
    primary_bound = {
        key: deepcopy(item)
        for key, item in ids.items()
        if not key.startswith("target_")
    }
    target_create = deepcopy(ids)
    for key in tuple(target_create):
        if key.startswith("target_configuration_"):
            target_create.pop(key)
    target_create["target_profile_ref"] = deepcopy(ids["target_profile_create_ref"])
    for captured in (primary_create, primary_bound, target_create, deepcopy(ids)):
        journal.append(
            "configuration", "pending", original_ids=captured, step_id=step_id
        )
    journal.append("configuration", status, original_ids=deepcopy(ids), step_id=step_id)
    return journal, ids, client


@pytest.mark.parametrize("status", ["failed", "passed"])
def test_real_legacy_configuration_recovers_exact_incremental_revisions(
    real_manifest, status
):
    cli, _, value = real_manifest
    journal, ids, _ = real_legacy_configuration(cli, value, status=status)
    records = journal.records()
    inputs = deepcopy((records, ids))
    before = checkpoint_bytes(value)
    assert "configuration_operation_id" not in records[0]["original_ids"]
    assert "target_profile_ref" not in records[1]["original_ids"]
    assert "target_configuration_operation_id" not in records[2]["original_ids"]
    assert cli._recover_real_original_ids(records, records[-1], {}) == ids
    assert (records, ids) == inputs
    assert checkpoint_bytes(value) == before


@pytest.mark.parametrize("status", ["failed", "passed"])
def test_real_legacy_configuration_resume_observes_before_later_business_only(
    real_manifest, status
):
    cli, _, value = real_manifest
    journal, ids, client = real_legacy_configuration(cli, value, status=status)
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()
    calls = []

    def later(step, record):
        calls.append(step)
        assert step != "configuration"
        for original in ("profile_create", "task_tool_resource", "configuration"):
            assert any(
                read.endswith("operationId=" + ids[original + "_operation_id"])
                for read in client.reads
            )
        assert any(read.endswith("/setup") for read in client.reads)
        if step == "conversation":
            assert record["original_ids"] == ids
        return {"canonical": True, "original_ids": deepcopy(client.ids)}

    report = cli.NativeCheck(value, client=client, runner=later).run(resume=True)
    assert report["status"] == "MAINLINE_PASS"
    assert calls == list(cli.REAL_MAIN_STAGES[1:])
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert Path(value["config_path"]).read_bytes() == config_before
    configuration = [r for r in journal.records() if r["step"] == "configuration"]
    assert configuration[-1]["status"] == "passed"
    assert configuration[-1]["step_id"] == configuration[0]["step_id"]
    assert configuration[-1]["original_ids"] == ids
    assert len(configuration) == (6 if status == "failed" else 5)


@pytest.mark.parametrize("prefix", ["", "target_"])
@pytest.mark.parametrize(
    "bad",
    [
        "backwards",
        "r3",
        "workspace",
        "profile",
        "boolean_revision",
        "extra_field",
        "missing_create",
        "missing_bind",
        "invalid_create",
        "invalid_bind",
        "reused_operation",
        "replaced_operation",
        "missing_created",
        "created_revision",
        "created_profile",
        "missing_expected",
        "expected_revision",
        "missing_resource",
        "resource_revision",
        "resource_identity",
        "missing_resource_operation",
        "missing_profile_id",
        "other_identity",
    ],
)
def test_real_legacy_configuration_refuses_unproven_revision_history(
    real_manifest, prefix, bad, monkeypatch
):
    cli, _, value = real_manifest
    journal, ids, client = real_legacy_configuration(cli, value, status="passed")
    records = journal.records()
    key = prefix + "profile_ref"
    foreign_id = str(uuid4())
    if bad in {"backwards", "r3", "workspace", "profile", "extra_field"}:
        ref = records[-1]["original_ids"][key]
        if bad in {"backwards", "r3"}:
            ref["revision"] = 1 if bad == "backwards" else 3
        else:
            ref[
                {
                    "workspace": "workspace_id",
                    "profile": "profile_id",
                    "extra_field": "extra",
                }[bad]
            ] = foreign_id
    elif bad == "boolean_revision":
        next(r for r in records if key in r["original_ids"])["original_ids"][key][
            "revision"
        ] = True
    elif bad == "replaced_operation":
        records[-1]["original_ids"][prefix + "configuration_operation_id"] = str(
            uuid4()
        )
    elif bad == "other_identity":
        records[-1]["original_ids"]["runtime_id"] = str(uuid4())
    else:
        missing = {
            "missing_create": prefix + "profile_create_operation_id",
            "missing_bind": prefix + "configuration_operation_id",
            "missing_created": prefix + "profile_create_ref",
            "missing_expected": prefix + "configuration_expected_ref",
            "missing_resource": "task_tool_resource_ref",
            "missing_resource_operation": "task_tool_resource_operation_id",
            "missing_profile_id": prefix + "profile_id",
        }
        for record in records:
            captured = record["original_ids"]
            if bad in missing:
                captured.pop(missing[bad], None)
            elif bad in {"invalid_create", "invalid_bind", "reused_operation"}:
                field = prefix + (
                    "profile_create_operation_id"
                    if bad == "invalid_create"
                    else "configuration_operation_id"
                )
                if field in captured:
                    captured[field] = (
                        ids[prefix + "profile_create_operation_id"]
                        if bad == "reused_operation"
                        else "invalid-original-operation"
                    )
            elif bad in {"created_revision", "created_profile", "expected_revision"}:
                field = prefix + (
                    "configuration_expected_ref"
                    if bad == "expected_revision"
                    else "profile_create_ref"
                )
                if field in captured:
                    captured[field][
                        "profile_id" if bad == "created_profile" else "revision"
                    ] = foreign_id if bad == "created_profile" else 2
            elif bad == "resource_revision":
                for field in (
                    "task_tool_resource_ref",
                    "configuration_resource_ref",
                    "target_configuration_resource_ref",
                ):
                    if field in captured:
                        captured[field]["revision"] = 2
            elif (
                bad == "resource_identity"
                and prefix + "configuration_resource_ref" in captured
            ):
                captured[prefix + "configuration_resource_ref"]["resource_id"] = (
                    foreign_id
                )
    with pytest.raises(cli.ManifestError, match="original_identity_changed"):
        cli._recover_real_original_ids(records, records[-1], {})
    rejected = Path(value["checkpoint_path"]).with_name("rejected-configuration")
    rejected.mkdir(mode=0o700)
    value["checkpoint_path"] = str(rejected)
    check = cli.NativeCheck(value, client=client)
    for record in records:
        check.journal.append(
            record["step"],
            record["status"],
            original_ids=record["original_ids"],
            step_id=record["step_id"],
        )
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()

    def refuse(*args, **kwargs):
        pytest.fail("Unproven configuration reached a runner or writer")

    check.runner = refuse
    monkeypatch.setattr(check.journal, "append", refuse)
    assert check.run(resume=True)["reason"] == "original_identity_changed"
    assert checkpoint_bytes(value) == before
    assert Path(value["config_path"]).read_bytes() == config_before


@pytest.mark.parametrize("prefix", ["", "target_"])
@pytest.mark.parametrize(
    "boundary", ["inherited_r1", "inherited_r2", "driver", "stage", "step"]
)
def test_real_legacy_configuration_does_not_cross_identity_authority(
    real_manifest, prefix, boundary
):
    cli, _, value = real_manifest
    journal, ids, _ = real_legacy_configuration(cli, value, status="passed")
    records = journal.records()
    key, inherited, additional = prefix + "profile_ref", {}, None
    if boundary.startswith("inherited_"):
        inherited[key] = deepcopy(
            ids[prefix + "profile_create_ref"]
            if boundary == "inherited_r1"
            else ids[key]
        )
    elif boundary == "driver":
        records = records[:1] if not prefix else records[2:3]
        additional = {key: deepcopy(ids[key])}
    elif boundary == "stage":
        for record in records:
            record["step"] = "conversation"
    else:
        records[-1]["step_id"] = str(uuid4())
        records[-1]["original_ids"][key] = deepcopy(ids[prefix + "profile_create_ref"])
    before = deepcopy((records, inherited, additional))
    if boundary == "step":
        recovered = cli._recover_real_original_ids(records, records[-1], inherited)
        assert recovered == records[-1]["original_ids"]
        assert recovered[key]["revision"] == 1
    else:
        with pytest.raises(cli.ManifestError, match="original_identity_changed"):
            cli._recover_real_original_ids(
                records, records[-1], inherited, additional=additional
            )
    assert (records, inherited, additional) == before


@pytest.mark.parametrize("prefix", ["", "target_"])
@pytest.mark.parametrize(
    "bad",
    [
        "unknown_bind",
        "missing_bind",
        "binding_operation",
        "binding_result",
        "resource",
        "profile",
        "runtime",
        "model",
    ],
)
def test_real_legacy_configuration_requires_canonical_readback_without_writer(
    real_manifest, prefix, bad, monkeypatch
):
    cli, _, value = real_manifest
    journal, ids, client = real_legacy_configuration(cli, value, status="passed")
    if prefix:
        for step in cli.REAL_MAIN_STAGES[
            1 : cli.REAL_MAIN_STAGES.index("plan_confirmed") + 1
        ]:
            journal.append(step, "passed", original_ids=deepcopy(client.ids))
    operation = ids[prefix + "configuration_operation_id"]
    suffix = "/configuration-requests?operationId=" + operation
    if bad in {"unknown_bind", "missing_bind", "binding_operation", "binding_result"}:
        body = client.get(suffix)
        if bad == "missing_bind":
            body = OSError("Original binding read unavailable")
        elif bad == "unknown_bind":
            body["state"] = "unknown"
        elif bad == "binding_operation":
            body["operationId"] = str(uuid4())
        else:
            body["result_ref"]["revision"] = 3
    elif bad == "resource":
        suffix = f"/resources/{ids['task_tool_resource_ref']['resource_id']}?revision=1"
        body = client.get(suffix)
        body["resource"]["ref"]["resource_id"] = str(uuid4())
    else:
        suffix = f"/profiles/{ids[prefix + 'profile_id']}?revision=2"
        body = client.get(suffix)
        if bad == "profile":
            body["profile"]["ref"]["profile_id"] = str(uuid4())
        elif bad == "runtime":
            body["profile"]["draft"]["runtime_ref"]["runtime_id"] = str(uuid4())
        else:
            body["profile"]["draft"]["model_id"] = "unapproved/model"
    client.overrides[suffix] = body
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()

    def refuse(*args, **kwargs):
        pytest.fail("Invalid canonical readback reached a runner or writer")

    check = cli.NativeCheck(value, client=client, runner=refuse)
    monkeypatch.setattr(check.journal, "append", refuse)
    assert check.run(resume=True)["reason"] == "original_operation_incomplete"
    assert checkpoint_bytes(value) == before
    assert Path(value["config_path"]).read_bytes() == config_before


def test_real_repository_reference_uses_installed_producer_literal():
    cli = module()
    directory = "/private/tmp/sidera-n23-repository-reference-fixture"
    assert cli.real_repository_reference(directory) == (
        "git-repository://"
        "383e8d6ddccf1a38650a2bcf79e8009b037c04083dbbb40c6dafe657815ca209"
    )


def test_fake_preflight_does_not_import_real_repository_producer(manifest, monkeypatch):
    _, path, value = manifest
    original_import = __import__

    def without_control_plane(name, *args, **kwargs):
        if name.startswith("sidera_control_plane"):
            pytest.fail("Fake entry imported the real repository producer")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", without_control_plane)
    cli = module()
    _, ids = completed(cli, value)
    client = Client(value, ids)
    check = cli.NativeCheck(cli.read_manifest(path), client=client)
    assert check.preflight()["status"] == "READY"
    assert client.get("/setup")["repository_ref"] == "repo://owned"


def test_real_setup_accepts_exact_repository_producer_and_original_configuration(
    real_manifest,
):
    cli, path, value = real_manifest
    journal, _, client = real_completed(cli, value, through="configuration")
    record = journal.records()[-1]
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()
    setup_path = f"/product/v1/workspaces/{value['workspace_id']}/setup"
    assert client.get(setup_path)["repository_ref"] != value["repository_directory"]
    check = cli.NativeCheck(cli.read_manifest(path), client=client)
    report = check.preflight()
    assert report["status"] == "READY"
    assert report["configuration_required"] is False
    assert report["credentials_tested"] is report["model_route_tested"] is False
    assert check._observe_real(record) is True
    assert setup_path in client.reads
    assert checkpoint_bytes(value) == before
    assert Path(value["config_path"]).read_bytes() == config_before


@pytest.mark.parametrize(
    "bad",
    [
        "different_reference",
        "physical_directory",
        "old_repository_uri",
        "empty_reference",
        "null_reference",
        "missing_reference",
        "foreign_workspace",
        "invalid_version",
        "boolean_version",
        "foreign_planner_workspace",
        "different_planner_profile",
        "different_planner_revision",
        "foreign_defaults_workspace",
        "different_defaults_profile",
        "missing_setup",
        "setup_read_failure",
    ],
)
def test_real_setup_refuses_wrong_identity_or_read_without_runner_or_writer(
    real_manifest, bad, monkeypatch
):
    cli, path, value = real_manifest
    journal, ids, client = real_completed(cli, value, through="configuration")
    record = journal.records()[-1]
    setup = deepcopy(client.get("/setup"))
    defaults = client.get("/defaults")
    if bad == "different_reference":
        setup["repository_ref"] = "git-repository://" + "f" * 64
    elif bad == "physical_directory":
        setup["repository_ref"] = value["repository_directory"]
    elif bad == "old_repository_uri":
        setup["repository_ref"] = "repo://owned"
    elif bad == "empty_reference":
        setup["repository_ref"] = ""
    elif bad == "null_reference":
        setup["repository_ref"] = None
    elif bad == "missing_reference":
        setup.pop("repository_ref")
    elif bad == "foreign_workspace":
        setup["workspace_id"] = str(uuid4())
    elif bad == "invalid_version":
        setup["version"] = 0
    elif bad == "boolean_version":
        setup["version"] = True
    elif bad == "foreign_planner_workspace":
        setup["planner_agent_ref"]["workspace_id"] = str(uuid4())
    elif bad == "different_planner_profile":
        setup["planner_agent_ref"]["profile_id"] = ids["target_profile_id"]
    elif bad == "different_planner_revision":
        setup["planner_agent_ref"]["revision"] = 1
    elif bad == "foreign_defaults_workspace":
        defaults["workspace_id"] = str(uuid4())
    elif bad == "different_defaults_profile":
        defaults["planner_profile_id"] = ids["target_profile_id"]
    elif bad == "missing_setup":
        setup = None
    elif bad == "setup_read_failure":
        setup = OSError("Unit setup read unavailable")
    client.overrides.update({"/setup": setup, "/defaults": defaults})
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()

    def refuse(*args, **kwargs):
        pytest.fail("Invalid setup crossed a runner or journal-write boundary")

    check = cli.NativeCheck(cli.read_manifest(path), client=client, runner=refuse)
    monkeypatch.setattr(check.journal, "append", refuse)
    assert check.preflight()["status"] == "INCOMPLETE"
    assert check._observe_real(record) is False
    assert check.run(resume=True)["reason"] == "original_operation_incomplete"
    assert checkpoint_bytes(value) == before
    assert Path(value["config_path"]).read_bytes() == config_before


def test_real_driver_failure_preserves_same_step_capture_and_resumes_later_only(
    real_manifest,
):
    cli, _, value = real_manifest
    journal, ids, client = real_before_goal(cli, value)
    before = checkpoint_bytes(value)
    calls = []

    def driver(step, record):
        calls.append(step)
        journal.append(
            step,
            "pending",
            original_ids=ids,
            observed={"canonical": False},
            step_id=record["step_id"],
        )
        raise AssertionError("failure after the original goal was captured")

    first = cli.NativeCheck(value, client=client, runner=driver).run()
    assert first["reason"] == "stage_execution_failed"
    assert calls == ["goal_submitted"]
    latest = journal.records()[-1]
    assert latest["status"] == "failed" and latest["original_ids"] == ids
    assert all(path.read_bytes() == raw for path, raw in before.items())
    failed_files = checkpoint_bytes(value)
    client.reads.clear()

    def later(step, record):
        calls.append(step)
        assert step != "goal_submitted"
        assert any(
            read.endswith("/commands/" + ids["goal_command_id"])
            for read in client.reads
        )
        return {"canonical": True, "original_ids": ids}

    resumed = cli.NativeCheck(value, client=client, runner=later).run(resume=True)
    assert resumed["status"] == "MAINLINE_PASS"
    assert calls == [
        "goal_submitted",
        "proposal_ready",
        "plan_confirmed",
        "dependency_execution",
        "result_verified",
        "result_accepted",
        "navigation_history",
    ]
    reconciled = [
        record
        for record in journal.records()
        if record["step"] == "goal_submitted" and record["status"] == "passed"
    ]
    assert len(reconciled) == 1
    assert reconciled[0]["step_id"] == latest["step_id"]
    assert reconciled[0]["original_ids"] == ids
    assert reconciled[0]["observed"]["original_reconciled"] is True
    assert all(path.read_bytes() == raw for path, raw in failed_files.items())


@pytest.mark.parametrize("legacy_status", ["pending", "failed", "incomplete"])
def test_real_legacy_thin_negative_recovers_captured_original_without_goal_driver(
    real_manifest,
    legacy_status,
):
    cli, _, value = real_manifest
    journal, ids, client = real_before_goal(cli, value)
    early = journal.records()[-1]["original_ids"]
    captured = journal.append("goal_submitted", "pending", original_ids=ids)
    journal.append(
        "goal_submitted",
        legacy_status,
        original_ids=early,
        observed={"reason": "legacy_thin_failure"},
        step_id=captured["step_id"],
    )
    before = checkpoint_bytes(value)
    calls = []

    def later(step, record):
        calls.append(step)
        assert step != "goal_submitted"
        assert any(
            read.endswith("/commands/" + ids["goal_command_id"])
            for read in client.reads
        )
        return {"canonical": True, "original_ids": ids}

    report = cli.NativeCheck(value, client=client, runner=later).run(resume=True)
    assert report["status"] == "MAINLINE_PASS"
    assert calls == [
        "proposal_ready",
        "plan_confirmed",
        "dependency_execution",
        "result_verified",
        "result_accepted",
        "navigation_history",
    ]
    reconciled = [
        record
        for record in journal.records()
        if record["step"] == "goal_submitted" and record["status"] == "passed"
    ]
    assert len(reconciled) == 1
    assert reconciled[0]["step_id"] == captured["step_id"]
    assert reconciled[0]["original_ids"] == ids
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.parametrize(
    "conflict",
    [
        "inherited_value",
        "captured_value",
        "scalar_type",
        "nested_ref_type",
        "nested_list_type",
    ],
)
def test_real_same_step_original_identity_conflicts_never_choose_latest(
    real_manifest,
    conflict,
):
    cli, _, value = real_manifest
    journal, ids, client = real_before_goal(cli, value)
    rich = deepcopy(ids)
    rich["identity_context"] = {"versions": [1]}
    captured = journal.append("goal_submitted", "pending", original_ids=rich)
    changed = deepcopy(rich)
    if conflict == "inherited_value":
        changed["thread_id"] = str(uuid4())
    elif conflict == "captured_value":
        changed["root_task_id"] = str(uuid4())
    elif conflict == "scalar_type":
        changed["plan_generation"] = True
    elif conflict == "nested_ref_type":
        changed["profile_create_ref"]["revision"] = True
    else:
        changed["identity_context"]["versions"] = [True]
    journal.append(
        "goal_submitted", "failed", original_ids=changed, step_id=captured["step_id"]
    )
    before = checkpoint_bytes(value)
    calls = []
    report = cli.NativeCheck(
        value, client=client, runner=lambda *args: calls.append(args)
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE"
    assert calls == [] and checkpoint_bytes(value) == before


@pytest.mark.parametrize(
    "bad",
    [
        "other_step_id",
        "focused_step",
        "unknown_command",
        "unknown_turn",
        "missing_original",
    ],
)
def test_real_original_recovery_refuses_foreign_or_unknown_capture(real_manifest, bad):
    cli, _, value = real_manifest
    journal, ids, client = real_before_goal(cli, value)
    early = journal.records()[-1]["original_ids"]
    rich = deepcopy(ids)
    if bad == "missing_original":
        rich.pop("goal_command_id")
    captured = journal.append(
        "worker_input" if bad == "focused_step" else "goal_submitted",
        "pending",
        original_ids=rich,
    )
    if bad in {"unknown_command", "unknown_turn"}:
        suffix = (
            "/commands/" + ids["goal_command_id"]
            if bad == "unknown_command"
            else "/turns/" + ids["turn_id"]
        )
        body = client.get(suffix)
        body["status" if bad == "unknown_command" else "operationState"] = "unknown"
        client.overrides[suffix] = body
    journal.append(
        "goal_submitted",
        "failed",
        original_ids=early,
        step_id=str(uuid4()) if bad == "other_step_id" else captured["step_id"],
    )
    before = checkpoint_bytes(value)
    calls = []
    report = cli.NativeCheck(
        value, client=client, runner=lambda *args: calls.append(args)
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE"
    assert calls == [] and checkpoint_bytes(value) == before


def test_real_driver_return_cannot_replace_nested_typed_original_identity(
    real_manifest,
):
    cli, _, value = real_manifest
    journal, ids, client = real_before_goal(cli, value)
    before = checkpoint_bytes(value)
    changed = deepcopy(ids)
    changed["profile_create_ref"]["revision"] = True
    calls = []

    def driver(step, record):
        calls.append(step)
        return {"canonical": True, "original_ids": changed}

    report = cli.NativeCheck(value, client=client, runner=driver).run()
    assert report["status"] == "INCOMPLETE"
    assert report["reason"] == "original_identity_changed"
    assert calls == ["goal_submitted"]
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert journal.records()[-1]["original_ids"] == journal.records()[1]["original_ids"]


def test_real_driver_exception_with_conflicting_capture_retains_negative_and_refuses_resume(
    real_manifest,
):
    cli, _, value = real_manifest
    journal, ids, client = real_before_goal(cli, value)
    before = checkpoint_bytes(value)
    inherited = journal.records()[-1]["original_ids"]
    changed = deepcopy(ids)
    changed["profile_create_ref"]["revision"] = True
    calls = []

    def driver(step, record):
        calls.append(step)
        journal.append(step, "pending", original_ids=changed, step_id=record["step_id"])
        raise AssertionError("failure with contradictory original identity")

    report = cli.NativeCheck(value, client=client, runner=driver).run()
    assert report["reason"] == "original_identity_changed"
    assert calls == ["goal_submitted"]
    latest = journal.records()[-1]
    assert latest["status"] == "incomplete" and latest["original_ids"] == inherited
    assert all(path.read_bytes() == raw for path, raw in before.items())
    after = checkpoint_bytes(value)
    resumed = cli.NativeCheck(
        value,
        client=client,
        runner=lambda *_: pytest.fail("Conflicting original replaced"),
    ).run(resume=True)
    assert resumed["status"] == "INCOMPLETE"
    assert checkpoint_bytes(value) == after


@pytest.mark.parametrize("target", [False, True])
@pytest.mark.parametrize(
    ("provider_env", "accepted"),
    [
        ("OPENCODE_API_KEY", True),
        (None, True),
        ("omitted", True),
        ("DEEPSEEK_API_KEY", False),
    ],
)
def test_real_configuration_requires_effective_go_environment_for_both_profiles(
    real_manifest,
    target,
    provider_env,
    accepted,
):
    cli, _, value = real_manifest
    _, ids, client = real_completed(cli, value)
    ref = ids["target_profile_ref" if target else "profile_ref"]
    suffix = f"/profiles/{ref['profile_id']}?revision={ref['revision']}"
    body = client.get(suffix)
    assert (
        body["profile"]["draft"]["native_settings"]["provider_env"]
        == "OPENCODE_API_KEY"
    )
    settings = body["profile"]["draft"]["native_settings"]
    if provider_env == "omitted":
        settings.pop("provider_env")
    else:
        settings["provider_env"] = provider_env
    client.overrides[suffix] = body
    before = checkpoint_bytes(value)
    calls = []
    check = cli.NativeCheck(
        value, client=client, runner=lambda *args: calls.append(args)
    )
    assert (
        check._real_configuration_matches(ids, client.config, target=target) is accepted
    )
    assert (check.preflight()["status"] == "READY") is (target or accepted)
    report = check.run(resume=True)
    assert (report["status"] == "MAINLINE_PASS") is accepted
    assert calls == [] and checkpoint_bytes(value) == before
    assert report["whole_n23_accepted"] is False


@pytest.mark.parametrize("target", [False, True])
@pytest.mark.parametrize("provider_env", [None, "omitted"])
def test_real_implicit_environment_requires_exact_go_secret_destination(
    real_manifest,
    target,
    provider_env,
):
    cli, _, value = real_manifest
    _, ids, client = real_completed(cli, value)
    ref = ids["target_profile_ref" if target else "profile_ref"]
    suffix = f"/profiles/{ref['profile_id']}?revision={ref['revision']}"
    body = client.get(suffix)
    draft = body["profile"]["draft"]
    if provider_env == "omitted":
        draft["native_settings"].pop("provider_env")
    else:
        draft["native_settings"]["provider_env"] = provider_env
    draft["secret_refs"][0]["destination"] = "DEEPSEEK_API_KEY"
    client.overrides[suffix] = body
    before = checkpoint_bytes(value)
    calls = []
    check = cli.NativeCheck(
        value, client=client, runner=lambda *args: calls.append(args)
    )
    assert check._real_configuration_matches(ids, client.config, target=target) is False
    assert check.run(resume=True)["status"] == "INCOMPLETE"
    assert calls == [] and checkpoint_bytes(value) == before


def test_real_preflight_admits_fresh_configuration_without_prompt(real_manifest):
    cli, _, value = real_manifest
    _, _, client = real_completed(cli, value)
    client.overrides.update(
        {
            "/defaults": {
                "workspace_id": value["workspace_id"],
                "main_profile_id": None,
                "planner_profile_id": None,
                "worker_profile_id": None,
            },
            "/setup": None,
        }
    )
    report = cli.NativeCheck(value, client=client).preflight()
    assert report["status"] == "READY"
    assert report["configuration_required"] is True
    assert report["credentials_tested"] is report["model_route_tested"] is False


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "verified",
        "source",
        "name",
        "command",
        "task",
        "planning",
        "run",
        "response",
        "missing",
        "text",
    ],
)
def test_real_goal_requires_verified_original_turn_receipt(real_manifest, bad):
    cli, _, value = real_manifest
    journal, ids, client = real_completed(cli, value, through="goal_submitted")
    suffix = "/turns/" + ids["turn_id"]
    body = client.get(suffix)
    data = body["items"][0]["data"]
    if bad == "verified":
        data["verified"] = False
    elif bad in {"source", "name"}:
        data["receiptSource" if bad == "source" else "name"] = "other"
    elif bad in {"command", "task", "planning", "run", "response"}:
        data[
            {
                "command": "commandId",
                "task": "rootTaskId",
                "planning": "planningRequestId",
                "run": "runId",
                "response": "responseId",
            }[bad]
        ] = str(uuid4())
    elif bad in {"missing", "text"}:
        body["items"] = (
            [] if bad == "missing" else [{"type": "assistantMessage", "data": data}]
        )
    client.overrides[suffix] = body
    check = cli.NativeCheck(value, client=client)
    assert check._observe_real(journal.records()[-1]) is (bad is None)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "route",
        "credential",
        "native_tools",
        "runtime_version",
        "missing_key",
        "fake_source",
        "stale_run",
        "wrong_commit",
        "wrong_tree",
        "candidate",
        "hash",
        "check",
        "acceptance",
        "pending",
    ],
)
def test_real_original_business_is_never_replaced_on_resume(real_manifest, bad):
    cli, _, value = real_manifest
    journal, ids, client = real_completed(cli, value)
    suffix = "/results"
    body = client.get(suffix)
    if bad in {"route", "credential", "native_tools"}:
        suffix = f"/profiles/{ids['profile_id']}?revision=2"
        body = client.get(suffix)
        draft = body["profile"]["draft"]
        if bad == "route":
            draft["model_id"] = "other"
        elif bad == "credential":
            draft["secret_refs"][0]["credential_ref"]["credential_id"] = str(uuid4())
        else:
            draft["native_settings"]["native_tools"] = "none"
    elif bad == "runtime_version":
        suffix = "/runtimes/" + ids["runtime_id"]
        body = client.get(suffix)
        body["actual_version"] = "wrong"
    elif bad in {"missing_key", "fake_source"}:
        suffix = "/materialization"
        body = client.get(suffix)
        body["worker_item_mapping"] = dict(body["worker_item_mapping"])
        if bad == "missing_key":
            body["worker_item_mapping"].pop("tests")
        else:
            body["worker_item_mapping"] = {"source": ids["source_worker_item_id"]}
    elif bad == "stale_run":
        suffix = "/agent-executions/" + ids["source_execution_id"]
        body = client.get(suffix)
        body["run_id"] = str(uuid4())
    elif bad in {"wrong_commit", "wrong_tree", "candidate", "hash"}:
        field = {
            "wrong_commit": "result_commit",
            "wrong_tree": "result_tree",
            "candidate": "candidate_id",
            "hash": "candidate_hash",
        }[bad]
        body["current_candidate"][field] = (
            "f" * 40 if bad.startswith("wrong_") else str(uuid4())
        )
    elif bad == "check":
        suffix = "/results/" + ids["candidate_id"]
        body = client.get(suffix)
        body["verifications"][0]["check_key"] = "other-check"
    elif bad == "acceptance":
        body["acceptance"]["command_id"] = str(uuid4())
    elif bad == "pending":
        client.unknown = True
    client.overrides[suffix] = body
    calls = []
    report = cli.NativeCheck(
        value, client=client, runner=lambda *args: calls.append(args)
    ).run(resume=True)
    assert calls == []
    if bad is None:
        assert report["status"] == "MAINLINE_PASS" and report["functional"] is True
        assert report["functional_scope"] == "mainline"
        assert report["cleanup"] is report["whole_n23_accepted"] is False
    else:
        assert report["status"] == "INCOMPLETE"
    assert journal.records()


def test_real_closed_input_does_not_block_mainline_or_imply_cleanup(real_manifest):
    cli, _, value = real_manifest
    journal, ids, client = real_completed(cli, value)
    journal.append(
        "worker_input",
        "failed",
        original_ids=ids,
        observed={"reason": "run_closed", "functional_scope": "focused_input"},
    )
    report = cli.NativeCheck(
        value,
        client=client,
        runner=lambda *_: pytest.fail("Successful business replaced"),
    ).run(resume=True)
    assert report["status"] == "MAINLINE_PASS" and report["cleanup"] is False
    cleanup = cli.NativeCheck(value, client=client).cleanup(wait_seconds=0)
    assert cleanup["functional"] is True and cleanup["status"] == "INCOMPLETE"
    assert cleanup["whole_n23_accepted"] is False


@pytest.mark.parametrize("status", ["pending", "failed", "incomplete"])
def test_real_canonically_completed_stage_reconciles_without_replacement(
    real_manifest, status
):
    cli, _, value = real_manifest
    journal, ids, client = real_completed(cli, value)
    journal.append(
        "goal_submitted",
        status,
        original_ids=ids,
        observed={"reason": "retained_original_negative"},
    )
    original = journal.records()
    report = cli.NativeCheck(
        value, client=client, runner=lambda *_: pytest.fail("Business replaced")
    ).run(resume=True)
    assert report["status"] == "MAINLINE_PASS"
    assert journal.records()[: len(original)] == original
    assert journal.records()[-1]["observed"]["original_reconciled"] is True


def test_real_ui_failure_retries_only_read_only_navigation(real_manifest):
    cli, _, value = real_manifest
    journal, ids, client = real_completed(cli, value)
    negative = journal.append(
        "navigation_history",
        "failed",
        original_ids=ids,
        observed={"reason": "original_ui_failure"},
    )
    calls = []

    def runner(step, record):
        calls.append((step, record["step_id"], record["observed"]["resume"]))
        return {"canonical": True, "original_ids": ids}

    report = cli.NativeCheck(value, client=client, runner=runner).run(resume=True)
    assert report["status"] == "MAINLINE_PASS"
    assert calls == [("navigation_history", negative["step_id"], True)]
    assert negative in journal.records()


@pytest.mark.parametrize(
    "bad", [None, "session", "execution", "operation", "unknown", "different_run"]
)
def test_real_focused_input_requires_original_delivery_and_run(real_manifest, bad):
    cli, _, value = real_manifest
    journal, ids, client = real_completed(cli, value)
    record = journal.append(
        "worker_input",
        "pending",
        original_ids=ids,
        observed={"functional_scope": "focused_input"},
    )
    suffix = (
        "/api/v1/gateway/inputs?workspace_id="
        + value["workspace_id"]
        + "&execution_id="
        + ids["source_execution_id"]
        + "&session_id="
        + ids["source_session_id"]
    )
    body = client.get(suffix)
    if bad in {"session", "execution", "operation"}:
        body["items"][0][bad + "_id"] = str(uuid4())
    elif bad == "unknown":
        body["items"][0]["status"] = "unknown"
    elif bad == "different_run":
        original = client.get("/agent-executions/" + ids["source_execution_id"])
        original["run_id"] = str(uuid4())
        client.overrides["/agent-executions/" + ids["source_execution_id"]] = original
    client.overrides[suffix] = body
    assert cli.NativeCheck(value, client=client)._observe_real(record) is (bad is None)


def test_real_run_admits_fresh_configuration_first(real_manifest):
    cli, _, value = real_manifest
    journal, _, client = real_completed(cli, value)
    for item in Path(value["checkpoint_path"]).iterdir():
        item.unlink()
    client.overrides["/defaults"] = {
        "workspace_id": value["workspace_id"],
        "main_profile_id": None,
        "planner_profile_id": None,
        "worker_profile_id": None,
    }
    calls = []

    def runner(step, record):
        calls.append((step, record["observed"]["resume"]))
        raise RuntimeError("stop at the admitted configuration boundary")

    report = cli.NativeCheck(value, client=client, runner=runner).run()
    assert calls == [("configuration", False)]
    assert report["reason"] == "stage_execution_failed"
    assert [item["status"] for item in journal.records()] == ["pending", "failed"]


def test_real_desktop_selects_only_explicit_real_entry(real_manifest, monkeypatch):
    cli, _, value = real_manifest
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda *_: "/usr/bin/node")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs)) or SimpleNamespace(returncode=1)
        ),
    )
    with pytest.raises(cli.ManifestError):
        cli.NativeCheck(value, client=Client())._desktop_step(
            "configuration", {"step_id": str(uuid4()), "observed": {"resume": False}}
        )
    argv, options = calls[0]
    assert argv[-5:] == [
        "--config",
        "playwright.native-real.config.ts",
        "native-harness-real.spec.ts",
        "--project",
        "native-real-chromium",
    ]
    assert not any("API_KEY" in key for key in options["env"])


@pytest.mark.parametrize(
    ("fixture_name", "expected_seconds"), [("real_manifest", 7260), ("manifest", 3660)]
)
def test_desktop_driver_outer_timeout_covers_only_the_selected_budget(
    request,
    monkeypatch,
    fixture_name,
    expected_seconds,
):
    cli, _, value = request.getfixturevalue(fixture_name)
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda *_: "/usr/bin/node")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs)) or SimpleNamespace(returncode=1)
        ),
    )
    with pytest.raises(cli.ManifestError):
        cli.NativeCheck(value, client=Client())._desktop_step(
            "configuration",
            {"step_id": str(uuid4()), "observed": {"resume": False}},
        )
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == expected_seconds
    assert calls[0][1]["capture_output"] is True
    assert not any("API_KEY" in key for key in calls[0][1]["env"])


def test_real_mainline_exit_is_zero_without_cleanup_claim(
    real_manifest, monkeypatch, capsys
):
    cli, path, value = real_manifest
    _, _, client = real_completed(cli, value)
    check = cli.NativeCheck(value, client=client)
    monkeypatch.setattr(cli, "NativeCheck", lambda *_: check)
    assert cli.main(["resume", "--manifest", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "MAINLINE_PASS" and report["whole_n23_accepted"] is False
    assert report["cleanup"] is False


def test_real_metadata_refuses_nested_api_key_before_client_allocation(
    real_manifest, monkeypatch
):
    cli, path, value = real_manifest
    config = json.loads(Path(value["config_path"]).read_text())
    config["settings"]["api_key"] = "REAL-TEST-SENTINEL-NOT-A-KEY"
    save_real_config(cli, path, value, config)
    monkeypatch.setattr(
        cli,
        "PublicClient",
        lambda *_: pytest.fail("Client allocated before manifest refusal"),
    )
    with pytest.raises(cli.ManifestError):
        cli.read_manifest(path)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "empty_events",
        "foreign_event",
        "stale_cursor",
        "wrong_profile",
        "wrong_binding",
        "wrong_generation",
        "wrong_target_assignment",
    ],
)
def test_real_exact_history_and_current_context_keep_original_scope(real_manifest, bad):
    cli, _, value = real_manifest
    journal, ids, client = real_completed(cli, value)
    suffix = "/native-trace"
    body = client.get(suffix)
    if bad == "empty_events":
        body["events"] = []
    elif bad == "foreign_event":
        body["events"][0]["run_id"] = str(uuid4())
    elif bad == "stale_cursor":
        body.update(events=[], next_sequence=0)
    elif bad == "wrong_profile":
        body["profile_ref"]["revision"] = 7
    elif bad in {"wrong_binding", "wrong_generation"}:
        suffix = "/native-context"
        body = client.get(suffix)
        body["context"][
            "binding_version" if bad == "wrong_binding" else "plan_generation"
        ] = 9
    elif bad == "wrong_target_assignment":
        suffix = "/assignments/worker_item/" + ids["worker_item_mapping"]["tests"]
        body = client.get(suffix)
        body["profile_ref"] = ids["profile_ref"]
    client.overrides[suffix] = body
    assert cli.NativeCheck(value, client=client)._observe_real(
        journal.records()[-1]
    ) is (bad is None)


def test_real_historical_trace_survives_another_current_run(real_manifest):
    cli, _, value = real_manifest
    journal, ids, client = real_completed(cli, value)
    context = client.get("/native-context")
    context["execution"]["run_id"] = str(uuid4())
    context["execution"]["execution_id"] = str(uuid4())
    client.overrides["/native-context"] = context
    original = client.get("/agent-executions/" + ids["source_execution_id"])
    original.update(
        execution_id=ids["source_execution_id"], run_id=ids["source_run_id"]
    )
    client.overrides["/agent-executions/" + ids["source_execution_id"]] = original
    assert (
        cli.NativeCheck(value, client=client)._observe_real(journal.records()[-1])
        is True
    )


@pytest.mark.parametrize("status", ["pending", "failed", "incomplete"])
def test_real_unknown_operation_never_reissues_runner_or_journal(real_manifest, status):
    cli, _, value = real_manifest
    journal, ids, client = real_completed(cli, value)
    journal.append(
        "goal_submitted",
        status,
        original_ids=ids,
        observed={"reason": "original_unknown"},
    )
    before = journal.records()
    suffix = "/commands/" + ids["goal_command_id"]
    body = client.get(suffix)
    body["status"] = "pending"
    client.overrides[suffix] = body
    report = cli.NativeCheck(
        value,
        client=client,
        runner=lambda *_: pytest.fail("Unknown operation replayed"),
    ).run(resume=True)
    assert report["status"] == "INCOMPLETE"
    assert journal.records() == before


def test_real_complete_cleanup_is_scoped_and_preserves_business_journal(real_manifest):
    cli, _, value = real_manifest
    journal, _, client = real_completed(cli, value)
    before = journal.records()
    private_json(Path(value["cleanup_path"]) / "owner-proof.json", cleanup_proof(value))
    report = cli.NativeCheck(value, client=client).cleanup(wait_seconds=0)
    assert (
        report["status"] == "PASS" and report["functional"] is report["cleanup"] is True
    )
    assert (
        report["whole_n23_accepted"] is False
        and report["scope"] == "mainline_and_cleanup"
    )
    assert journal.records() == before


def closed_goal_fixture(cli, value, *, through="goal_submitted", prior_pass=True):
    from sidera_product.agents.schemas import LocalSessionView, ProfileWorkView
    from sidera_product.chat.contracts import TurnItem, TurnView

    if prior_pass:
        journal, ids, client = real_completed(cli, value, through=through)
    else:
        journal, ids, client = real_before_goal(cli, value)
    workspace = value["workspace_id"]
    prefix = f"/api/v1/workspaces/{workspace}/"
    routes = {
        "turn": f"/main-agent/v1/threads/{ids['thread_id']}/turns/{ids['turn_id']}",
        "session": "/product/v1/session",
        "work": f"/agents/v1/workspaces/{workspace}/profiles/{ids['profile_id']}/work",
        "goal": prefix + "commands/" + ids["goal_command_id"],
        "planning": prefix + "planning-requests/" + ids["planning_request_id"],
        "proposal": prefix
        + f"planning-requests/{ids['planning_request_id']}/proposals/{ids['proposal_revision']}",
    }
    now = datetime(2026, 10, 9, tzinfo=UTC)
    client.overrides[routes["turn"]] = TurnView(
        id=ids["turn_id"],
        threadId=ids["thread_id"],
        status="completed",
        createdAt=now,
        profileRef=ids["profile_ref"],
        runId=ids["chat_run_id"],
        operationId=ids["turn_operation_id"],
        operationState="delivered",
        items=(
            TurnItem(
                id=ids["turn_id"] + ":user",
                type="userMessage",
                data={"text": "original owned goal"},
            ),
        ),
        error={
            "code": "permission_denied",
            "message": "permission_denied",
            "retryable": False,
        },
        interruptReasonCode="response_not_current",
    ).model_dump(mode="json", by_alias=True)
    client.overrides[routes["session"]] = LocalSessionView(
        session_id=uuid4(),
        expires_at=datetime(2026, 10, 10, tzinfo=UTC),
        workspace_id=workspace,
        principal_id=value["principal_id"],
    ).model_dump(mode="json")
    client.overrides[routes["work"]] = ProfileWorkView(
        task_work={"items": [], "next_cursor": None},
        conversations=(
            {
                "conversation_id": ids["thread_id"],
                "title": "Original owned conversation",
                "profile_ref": ids["profile_ref"],
                "run_id": ids["chat_run_id"],
                "stop_state": "stopped",
                "updated_at": now,
            },
        ),
    ).model_dump(mode="json")
    return journal, ids, client, routes


def assert_closed_goal_observation(cli, value, client, record, expected):
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()
    overrides_before = {
        route: body if isinstance(body, Exception) else deepcopy(body)
        for route, body in client.overrides.items()
    }
    calls = []
    client.reads.clear()
    check = cli.NativeCheck(
        value, client=client, runner=lambda *args: calls.append(args)
    )
    assert check._observe_real(record) is expected
    assert calls == [] and checkpoint_bytes(value) == before
    assert Path(value["config_path"]).read_bytes() == config_before
    assert client.overrides == overrides_before
    assert all(
        forbidden not in path
        for path in client.reads
        for forbidden in (
            "/events",
            "/runs/",
            "/outputs",
            "/agent-executions/",
            "/native-context",
            "/native-trace",
        )
    )


@pytest.mark.parametrize("operation_state", ["accepted", "delivered"])
@pytest.mark.parametrize("step", ["goal_submitted", "proposal_ready"])
def test_real_closed_goal_reuses_prior_pass_with_current_owned_closure(
    real_manifest, operation_state, step
):
    cli, _, value = real_manifest
    journal, _, client, routes = closed_goal_fixture(cli, value, through=step)
    client.overrides[routes["turn"]]["operationState"] = operation_state
    assert_closed_goal_observation(cli, value, client, journal.records()[-1], True)
    assert all(
        routes[key] in client.reads for key in ("session", "work", "goal", "planning")
    )
    if step == "proposal_ready":
        assert routes["proposal"] in client.reads


def test_real_closed_goal_resume_preserves_passes_and_stops_at_next_proposal(
    real_manifest,
):
    cli, _, value = real_manifest
    journal, ids, client, routes = closed_goal_fixture(cli, value)
    old_records = journal.records()
    before = checkpoint_bytes(value)
    config_before = Path(value["config_path"]).read_bytes()
    denial_before = deepcopy(client.overrides[routes["turn"]])
    calls = []

    def later(step, record):
        calls.append(step)
        assert step == "proposal_ready"
        assert record["original_ids"] == ids
        assert all(
            routes[key] in client.reads
            for key in ("session", "work", "goal", "planning")
        )
        raise RuntimeError("stop at the original proposal boundary")

    report = cli.NativeCheck(value, client=client, runner=later).run(resume=True)
    assert calls == ["proposal_ready"]
    assert report["status"] == "INCOMPLETE"
    assert report["reason"] == "stage_execution_failed"
    assert report["functional"] is report["whole_n23_accepted"] is False
    records = journal.records()
    assert records[: len(old_records)] == old_records
    added = records[len(old_records) :]
    assert [(record["step"], record["status"]) for record in added] == [
        ("proposal_ready", "pending"),
        ("proposal_ready", "failed"),
    ]
    assert added[0]["step_id"] == added[1]["step_id"]
    assert all(record["original_ids"] == ids for record in added)
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert Path(value["config_path"]).read_bytes() == config_before
    assert client.overrides[routes["turn"]] == denial_before
    assert all("/events" not in path and "/runs/" not in path for path in client.reads)


def test_real_closed_goal_recovers_same_step_partial_pass_from_journal(real_manifest):
    cli, _, value = real_manifest
    journal, ids, client, _ = closed_goal_fixture(cli, value, prior_pass=False)
    pending = journal.append("goal_submitted", "pending", original_ids=ids)
    passed = journal.append(
        "goal_submitted",
        "passed",
        original_ids={"root_task_id": ids["root_task_id"]},
        observed={"canonical": True},
        step_id=pending["step_id"],
    )
    assert_closed_goal_observation(
        cli, value, client, {**passed, "original_ids": ids}, True
    )


@pytest.mark.parametrize(
    "bad",
    [
        "absent",
        "pending",
        "failed",
        "incomplete",
        "invalidated",
        "canonical_missing",
        "canonical_false",
        "canonical_zero",
        "canonical_one",
        "canonical_string",
        "canonical_null",
        "observed_list",
        "other_step_id",
    ],
)
def test_real_closed_goal_requires_latest_saved_canonical_pass(real_manifest, bad):
    cli, _, value = real_manifest
    journal, ids, client, _ = closed_goal_fixture(
        cli, value, prior_pass=bad != "absent"
    )
    record = journal.records()[-1]
    if bad == "absent":
        record = {**record, "step": "goal_submitted", "original_ids": ids}
    elif bad == "other_step_id":
        record = {**record, "step_id": str(uuid4())}
    else:
        observed = {"canonical": True}
        status = (
            bad
            if bad in {"pending", "failed", "incomplete", "invalidated"}
            else "passed"
        )
        if bad.startswith("canonical_"):
            observed = {
                "canonical_missing": {},
                "canonical_false": {"canonical": False},
                "canonical_zero": {"canonical": 0},
                "canonical_one": {"canonical": 1},
                "canonical_string": {"canonical": "true"},
                "canonical_null": {"canonical": None},
            }[bad]
        elif bad == "observed_list":
            observed = ["canonical"]
        journal.append(
            "goal_submitted",
            status,
            original_ids=ids,
            observed=observed,
            step_id=record["step_id"],
        )
        record = journal.records()[-1]
    assert_closed_goal_observation(cli, value, client, record, False)


@pytest.mark.parametrize(
    "field",
    [
        "profile_ref",
        "thread_id",
        "turn_id",
        "turn_operation_id",
        "chat_run_id",
        "chat_response_id",
        "goal_command_id",
        "root_task_id",
        "planning_request_id",
    ],
)
def test_real_closed_goal_rejects_changed_original_bindings(real_manifest, field):
    cli, _, value = real_manifest
    journal, ids, client, routes = closed_goal_fixture(cli, value)
    changed = deepcopy(ids)
    if field == "profile_ref":
        changed[field]["revision"] += 1
    else:
        changed[field] = str(uuid4())
    client.ids = changed
    denied = client.overrides.pop(routes["turn"])
    denied.update(
        id=changed["turn_id"],
        threadId=changed["thread_id"],
        operationId=changed["turn_operation_id"],
        profileRef=changed["profile_ref"],
        runId=changed["chat_run_id"],
    )
    denied["items"][0]["id"] = changed["turn_id"] + ":user"
    current_turn = (
        f"/main-agent/v1/threads/{changed['thread_id']}/turns/{changed['turn_id']}"
    )
    client.overrides[current_turn] = denied
    client.overrides[routes["work"]]["conversations"][0].update(
        conversation_id=changed["thread_id"],
        profile_ref=changed["profile_ref"],
        run_id=changed["chat_run_id"],
    )
    record = {**journal.records()[-1], "original_ids": changed}
    assert_closed_goal_observation(cli, value, client, record, False)


@pytest.mark.parametrize(
    "stage", ["configuration", "conversation", "goal_submitted", "proposal_ready"]
)
def test_real_closed_goal_rejects_conflicting_same_step_captures(real_manifest, stage):
    cli, _, value = real_manifest
    through = "proposal_ready" if stage == "proposal_ready" else "goal_submitted"
    journal, ids, client, _ = closed_goal_fixture(cli, value, through=through)
    latest = {record["step"]: record for record in journal.records()}
    journal.append(
        stage,
        "pending",
        original_ids={"chat_response_id": str(uuid4())},
        step_id=latest[stage]["step_id"],
    )
    journal.append(
        stage,
        "passed",
        original_ids=ids,
        observed={"canonical": True},
        step_id=latest[stage]["step_id"],
    )
    assert_closed_goal_observation(cli, value, client, latest[through], False)


@pytest.mark.parametrize(
    "invalid", ["", "not-a-uuid", None, True, 7, "00000000-0000-0000-0000-000000000000"]
)
def test_real_closed_goal_requires_valid_original_response_uuid(real_manifest, invalid):
    cli, _, value = real_manifest
    journal, ids, client, _ = closed_goal_fixture(cli, value)
    ids["chat_response_id"] = invalid
    for path in Path(value["checkpoint_path"]).iterdir():
        record = json.loads(path.read_text())
        record["original_ids"]["chat_response_id"] = invalid
        private_json(path, record)
    assert_closed_goal_observation(cli, value, client, journal.records()[-1], False)


@pytest.mark.parametrize(
    ("document", "fields", "replacement"),
    [
        ("turn", ("id",), "foreign"),
        ("turn", ("threadId",), "foreign"),
        ("turn", ("operationId",), "foreign"),
        ("turn", ("operationState",), "unknown"),
        ("turn", ("operationState",), "pending"),
        ("turn", ("profileRef", "profile_id"), "foreign"),
        ("turn", ("profileRef", "workspace_id"), "foreign"),
        ("turn", ("profileRef", "revision"), 3),
        ("turn", ("runId",), "foreign"),
        ("turn", ("responseId",), "foreign"),
        ("turn", ("error", "code"), "run_closed"),
        ("turn", ("error", "code"), "missing"),
        ("turn", ("error", "retryable"), True),
        ("turn", ("error", "retryable"), 0),
        ("turn", ("error", "retryable"), "missing"),
        ("turn", ("error",), None),
        ("turn", ("error",), []),
        ("turn", ("streamAvailable",), True),
        ("turn", ("streamAvailable",), 0),
        ("turn", ("streamAvailable",), "missing"),
        ("turn", ("interruptAvailable",), True),
        ("turn", ("interruptAvailable",), "missing"),
        ("turn", ("recoveryMode",), "poll"),
        ("turn", ("finalResponse",), "unverified completion"),
        ("turn", ("items",), []),
        ("turn", ("items",), None),
        ("turn", ("items", 0, "type"), "assistantMessage"),
        ("turn", ("items", 0, "type"), "toolCall"),
        ("turn", ("items", 0, "id"), "foreign"),
        ("turn", ("items", 0, "data"), None),
        ("turn", ("items", 0, "data", "text"), None),
        ("turn", (), "unavailable"),
        ("turn", (), {}),
        ("session", ("workspace_id",), "foreign"),
        ("session", ("principal_id",), "foreign"),
        ("session", ("workspace_id",), "missing"),
        ("session", ("principal_id",), "missing"),
        ("session", (), None),
        ("session", (), "unavailable"),
        ("work", ("conversations",), []),
        ("work", ("conversations",), "duplicate"),
        ("work", ("conversations",), "missing"),
        ("work", ("conversations",), None),
        ("work", ("conversations",), {}),
        ("work", ("conversations", 0), None),
        ("work", ("conversations", 0, "conversation_id"), "foreign"),
        ("work", ("conversations", 0, "conversation_id"), "missing"),
        ("work", ("conversations", 0, "profile_ref", "workspace_id"), "foreign"),
        ("work", ("conversations", 0, "profile_ref", "profile_id"), "foreign"),
        ("work", ("conversations", 0, "profile_ref", "revision"), 3),
        ("work", ("conversations", 0, "profile_ref", "revision"), 2.0),
        ("work", ("conversations", 0, "profile_ref"), None),
        ("work", ("conversations", 0, "run_id"), "foreign"),
        ("work", ("conversations", 0, "run_id"), None),
        ("work", ("conversations", 0, "run_id"), "missing"),
        ("work", ("conversations", 0, "stop_state"), "running"),
        ("work", ("conversations", 0, "stop_state"), "stopping"),
        ("work", ("conversations", 0, "stop_state"), None),
        ("work", ("conversations", 0, "stop_state"), "missing"),
        ("work", ("conversations", 0, "title"), "missing"),
        ("work", ("conversations", 0, "updated_at"), "missing"),
        ("work", ("conversations", 0, "updated_at"), "invalid"),
        ("work", ("task_work",), "missing"),
        ("work", ("task_work",), None),
        ("work", (), None),
        ("work", (), []),
        ("work", (), "unavailable"),
        ("goal", ("command_id",), "foreign"),
        ("goal", ("command_type",), "ConfirmPlanningProposal"),
        ("goal", ("status",), "pending"),
        ("goal", ("status",), "unknown"),
        ("goal", ("status",), "rejected"),
        ("goal", ("status",), False),
        ("goal", ("result",), None),
        ("goal", ("result", "command_id"), "foreign"),
        ("goal", ("result", "data", "root_task_id"), "foreign"),
        ("goal", ("result", "data", "planning_request_id"), "foreign"),
        ("goal", ("result", "data", "root_task_id"), "missing"),
        ("goal", (), "unavailable"),
        ("planning", ("id",), "foreign"),
        ("planning", ("workspace_id",), "foreign"),
        ("planning", ("kind",), "revise"),
        ("planning", ("source_type",), "plan_revision"),
        ("planning", ("source_type",), "missing"),
        ("planning", (), "unavailable"),
    ],
)
def test_real_closed_goal_rejects_unowned_open_denied_or_incomplete_evidence(
    real_manifest, document, fields, replacement
):
    cli, _, value = real_manifest
    journal, _, client, routes = closed_goal_fixture(cli, value)
    route = routes[document]
    if not fields:
        client.overrides[route] = (
            OSError("controlled read unavailable")
            if replacement == "unavailable"
            else deepcopy(replacement)
        )
    else:
        body = client.get(route)
        target = body
        for field in fields[:-1]:
            target = target[field]
        if replacement == "missing":
            target.pop(fields[-1])
        elif replacement == "duplicate":
            target[fields[-1]] *= 2
        else:
            target[fields[-1]] = (
                str(uuid4()) if replacement == "foreign" else deepcopy(replacement)
            )
        client.overrides[route] = body
    assert_closed_goal_observation(cli, value, client, journal.records()[-1], False)


@pytest.mark.parametrize(
    ("document", "field", "replacement"),
    [
        ("planning", "current_proposal_revision", 2),
        ("planning", "current_proposal_content_hash", "f" * 64),
        ("planning", "status", "running"),
        ("proposal", "id", "foreign"),
        ("proposal", "planning_request_id", "foreign"),
        ("proposal", "revision", 2),
        ("proposal", "content_hash", "f" * 64),
    ],
)
def test_real_closed_goal_does_not_replace_current_proposal_proof(
    real_manifest, document, field, replacement
):
    cli, _, value = real_manifest
    journal, _, client, routes = closed_goal_fixture(
        cli, value, through="proposal_ready"
    )
    body = client.get(routes[document])
    body[field] = str(uuid4()) if replacement == "foreign" else replacement
    client.overrides[routes[document]] = body
    assert_closed_goal_observation(cli, value, client, journal.records()[-1], False)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "missing",
        "malformed",
        "malformed_null",
        "malformed_list",
        "malformed_text",
        "unverified",
        "wrong",
        "denied_readable",
    ],
)
def test_real_closed_goal_readable_history_still_requires_verified_tool_receipt(
    real_manifest, bad
):
    cli, _, value = real_manifest
    journal, _, client, routes = closed_goal_fixture(cli, value)
    denied = client.overrides.pop(routes["turn"])
    body = client.get(routes["turn"])
    if bad == "missing":
        body["items"] = []
    elif bad == "malformed":
        body["items"] = [{"type": "toolCall", "data": {}}]
    elif bad in {"malformed_null", "malformed_list", "malformed_text"}:
        body["items"][0]["data"] = {
            "malformed_null": None,
            "malformed_list": [],
            "malformed_text": "unverified receipt",
        }[bad]
    elif bad == "unverified":
        body["items"][0]["data"]["verified"] = False
    elif bad == "wrong":
        body["items"][0]["data"]["commandId"] = str(uuid4())
    elif bad == "denied_readable":
        body.update(error=denied["error"], items=denied["items"])
    client.overrides[routes["turn"]] = body
    assert_closed_goal_observation(
        cli, value, client, journal.records()[-1], bad is None
    )
    assert routes["session"] not in client.reads and routes["work"] not in client.reads
