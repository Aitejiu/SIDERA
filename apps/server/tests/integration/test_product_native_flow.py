import sys
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.domain.inputs import input_operation_id

from tests.support.native_product_flow import (
    NativeProductClient,
    build_native_product_flow_lab,
    verify_result_repository,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def native_product_flow_lab(
    selected_product_targets, tmp_path, retain_incomplete_product_work
):
    targets, guards, evidence = selected_product_targets
    directory = tmp_path / "native-product"
    directory.mkdir(mode=0o700)
    if evidence is None:
        evidence = tmp_path / "native-evidence"
        evidence.mkdir(mode=0o700)
    lab = build_native_product_flow_lab(
        targets, guards, directory, evidence, start_web=False
    )
    try:
        lab.start()
        yield lab
    finally:
        complete = lab.close()
        lab.record("cleanup", lab.stage_facts())
        if not complete:
            retain_incomplete_product_work((lab, guards))
            if sys.exception() is None:
                pytest.fail(
                    "Native product cleanup incomplete; original owners retained"
                )


def _source(client, mapping):
    context = client.wait_for_source_context(mapping["source"])
    assert context["context"]["run_id"] == str(client.lab.native.original_run_id)
    return context


def test_complete_native_flow_with_controlled_harness(native_product_flow_lab):
    lab = native_product_flow_lab
    client = NativeProductClient(lab)
    try:
        profile, setup = client.configure()
        goal, planning = client.propose()
        root = goal["root_task_id"]
        assert client.get(f"tasks/{root}/results")["task_status"] != "succeeded"
        mapping = client.confirm(goal, planning, profile, setup)
        assert set(mapping) == {"source", "hard-only", "integration"}
        source = _source(client, mapping)
        original = source["context"]
        assert [key for key in lab.native.worker_facts] == ["source"]
        text = "Keep the original checked value and report this intervention"
        sent = client.command(
            "SendExecutionInput",
            "execution_session",
            original["session_id"],
            original["session_version"],
            {
                "context": original,
                "client_idempotency_key": str(uuid4()),
                "text": text,
                "attachment_refs": [],
            },
        )
        native_operation = input_operation_id(UUID(sent["aggregate_id"]))
        client.wait(
            lambda: lab.native.release_accepted_prompt(
                UUID(original["run_id"]), native_operation
            ),
            bool,
            "actual-native-accepted",
        )
        rows_path = f"/api/v1/gateway/inputs?workspace_id={lab.workspace_id}&execution_id={original['execution_id']}&session_id={original['session_id']}"
        delivered = client.wait(
            lambda: client.request("GET", rows_path)["items"],
            lambda items: any(
                item["id"] == sent["aggregate_id"] and item["status"] == "delivered"
                for item in items
            ),
            "same-run-delivery",
        )
        assert any(item["id"] == sent["aggregate_id"] for item in delivered)
        source_inputs = [
            item for item in lab.native.inputs if item["run_id"] == original["run_id"]
        ]
        assert (
            len(source_inputs) == 2
            and source_inputs[0]["response_id"] != source_inputs[1]["response_id"]
        )
        assert any(
            run == UUID(original["run_id"]) and assembled.text == text
            for run, _, assembled in lab.native.assembled_inputs
        )
        result = client.wait(
            lambda: client.get(f"tasks/{root}/results"),
            lambda value: value["result_stage"] == "awaiting_acceptance",
            "real-capture-integration-verification",
        )
        assert result["task_status"] != "succeeded" and result["acceptance"] is None
        assert result["current_candidate"] is not None
        assert all(
            item["verification_status"] == "passed" for item in result["criteria"]
        )
        assert list(lab.native.worker_facts) == ["source", "hard-only", "integration"]
        assert len(lab.native.physical_inputs) == 2
        store = GitResultStore(
            {setup["repository_ref"]: lab.repository}, git_executable="/usr/bin/git"
        )
        for observed in lab.native.physical_inputs:
            execution = observed["execution_id"]
            package = client.get(
                f"executions/{execution}/context-package?manifest_schema_version=3"
            )
            manifest = package["code_input_manifest"]
            assert manifest["input_hash"] == observed["input_hash"]
            assert manifest["selection_id"] == observed["selection_id"]
            assert manifest["selection_hash"] == observed["selection_hash"]
            operation = uuid5(
                NAMESPACE_URL,
                f"sidera.gateway.code-input:{execution}:{observed['input_hash']}",
            )
            receipt = store.inspect_materialization(operation)
            assert receipt is not None and receipt.execution_id == UUID(execution)
            assert (
                receipt.input_hash == observed["input_hash"]
                and receipt.conflict_paths == ()
            )
            assert (
                receipt.actual_commit == observed["input_commit"]
                and receipt.actual_tree == observed["input_tree"]
            )
        candidate = result["current_candidate"]
        detail = client.get(f"tasks/{root}/results/{candidate['candidate_id']}")
        checks = detail["verifications"]
        assert checks and any(
            item["check_key"] == "owned-check" and item["status"] == "passed"
            for item in checks
        )
        criteria = [
            {
                "criterion_key": item["criterion_key"],
                "decision": "accept_passed",
                "reason": None,
            }
            for item in result["criteria"]
        ]
        basis = client.request(
            "POST",
            client.prefix
            + f"tasks/{root}/results/{candidate['candidate_id']}/acceptance-basis",
            {"criteria": criteria},
        )
        accepted = client.command(
            "CompleteTask",
            "task",
            root,
            result["task_version"],
            {**basis, "criteria": criteria},
        )
        finished = client.wait(
            lambda: client.get(f"tasks/{root}/results"),
            lambda value: value["task_status"] == "succeeded",
            "real-accepted-result-finalization",
        )
        assert finished["acceptance"]["command_id"] == accepted["command_id"]
        assert finished["acceptance"]["candidate_id"] == candidate["candidate_id"]
        assert finished["acceptance"]["candidate_hash"] == candidate["candidate_hash"]
        assert finished["apply_policy"]["apply_on_accept"] is False
        assert finished["apply_policy"]["target_ref"] is None
        accepted_detail = client.get(
            f"tasks/{root}/results/{candidate['candidate_id']}"
        )
        assert accepted_detail["apply_operations"] == []
        repository_proof = verify_result_repository(
            lab, accepted_detail["candidate"], accepted_detail["verifications"]
        )
        for key, item_id in mapping.items():
            view = client.get(f"worker-items/{item_id}/native-context")
            assert view["execution"] is not None and view["run"] is not None
            assert view["run"]["profile_ref"] == profile
            trace = client.get(
                f"worker-items/{item_id}/executions/{view['execution']['execution_id']}/native-trace"
            )
            assert (
                trace["workspace_id"] == str(lab.workspace_id)
                and trace["worker_item_id"] == item_id
            )
            assert (
                trace["execution_id"] == view["execution"]["execution_id"]
                and trace["profile_ref"] == profile
            )
            assert trace["run_id"] == view["execution"]["run_id"] and trace["events"]
            assert trace["next_sequence"] == trace["events"][-1]["sequence"]
            assert all(item["run_id"] == trace["run_id"] for item in trace["events"])
            if key == "source":
                assert (
                    trace["run_id"] == original["run_id"] and not view["input_enabled"]
                )
        lab.record(
            "functional",
            {
                "root_task_id": root,
                "planning_request_id": goal["planning_request_id"],
                "confirmation": lab.facts["confirmation"],
                "input_command_id": sent["command_id"],
                "input_operation_id": str(native_operation),
                "source_run_id": original["run_id"],
                "source_responses": source_inputs,
                "candidate_id": candidate["candidate_id"],
                "acceptance_command_id": accepted["command_id"],
                "task_status": finished["task_status"],
                "physical_inputs": lab.native.physical_inputs,
                "check_ids": [item["verification_id"] for item in checks],
                "repository_proof": repository_proof,
            },
        )
    finally:
        client.client.close()


def test_unknown_original_source_handoff_never_admits_target_writer(
    native_product_flow_lab,
):
    lab = native_product_flow_lab
    client = NativeProductClient(lab)
    try:
        profile, setup = client.configure()
        runtime_ref = lab.facts["configuration"]["runtime_ref"]
        resource_ref = lab.facts["configuration"]["task_tool_resource_ref"]
        target_operation = str(uuid5(lab.namespace, "transition-target-profile"))
        lab.record(
            "transition-target-profile-intent",
            {"operation_id": target_operation, "runtime_ref": runtime_ref},
        )
        target_result = client.request(
            "POST",
            f"/agents/v1/workspaces/{lab.workspace_id}/profiles",
            {
                "operationId": target_operation,
                "draft": {
                    "name": "Native transition target",
                    "description": "",
                    "runtime_ref": runtime_ref,
                    "instructions": "Preserve the original input and generation",
                    "resource_refs": [resource_ref],
                    "model_id": None,
                    "thinking": None,
                    "max_active": 4,
                    "environment": {},
                    "secret_refs": [],
                    "native_settings": {},
                },
            },
        )
        assert target_result["operationId"] == target_operation
        assert target_result["state"] in {"succeeded", "reconciled"}
        target_profile = target_result["result_ref"]
        assert target_profile is not None and target_profile != profile
        assert target_profile == target_result["target_ref"]
        assert (
            target_profile["workspace_id"] == str(lab.workspace_id)
            and type(target_profile["revision"]) is int
            and target_profile["revision"] == 1
        )
        assert target_profile["profile_id"] != profile["profile_id"]
        target_view = client.request(
            "GET",
            f"/agents/v1/workspaces/{lab.workspace_id}/profiles/{target_profile['profile_id']}",
        )
        assert target_view["profile"]["ref"] == target_profile
        assert target_view["profile"]["draft"]["runtime_ref"] == runtime_ref
        assert target_view["profile"]["draft"]["resource_refs"] == [resource_ref]
        lab.record(
            "transition-target-profile",
            {"operation_id": target_operation, "target_profile_ref": target_profile},
        )
        goal, planning = client.propose()
        mapping = client.confirm(goal, planning, profile, setup)
        before = _source(client, mapping)
        original = before["context"]
        with lab.native.lock:
            lab.native.unknown_exits.add(UUID(original["run_id"]))
        prepared = client.command(
            "PrepareProfileHandoff",
            "worker_item",
            mapping["source"],
            before["worker_item_version"],
            {
                "proposal_schema_version": 2,
                "context": original,
                "target_assignee": {
                    "kind": "profile",
                    "id": target_profile["profile_id"],
                },
                "target_profile_ref": target_profile,
                "handoff_reason": "Explicit original source reassignment",
                "handoff_instruction": "Preserve the original input and generation",
                "input_plan": {},
                "proposal_ttl_seconds": 900,
            },
        )
        proposal = client.get(f"handoff-proposals/{prepared['aggregate_id']}")
        assert (
            proposal["source_run_id"] == original["run_id"]
            and proposal["source_profile_ref"] == profile
        )
        assert proposal["target_profile_ref"] == target_profile
        confirmed = client.command(
            "ConfirmProfileHandoff",
            "handoff_proposal",
            proposal["id"],
            proposal["version"],
            {
                "proposal_id": proposal["id"],
                "expected_proposal_version": proposal["version"],
                "expected_proposal_hash": proposal["proposal_hash"],
                "expected_worker_item_version": before["worker_item_version"],
            },
        )
        transition = client.wait(
            lambda: client.get(f"handoff-transitions/{confirmed['aggregate_id']}"),
            lambda value: value["failure_code"] == "source_stop_unconfirmed",
            "unknown-original-source-stop",
        )
        assert (
            transition["source_run_id"] == original["run_id"]
            and transition["target_run_id"] is None
        )
        assert (
            transition["target_execution_id"] is None
            and transition["available_recovery_strategies"] == []
        )
        assert transition["target_profile_ref"] == target_profile
        assert [
            item
            for item in lab.native.open_facts
            if item["profile_ref"] == target_profile
        ] == []
        assert (
            len([value for value in lab.native.roles.values() if value == "worker"])
            == 1
        )
        now = client.get(f"worker-items/{mapping['source']}/native-context")
        assert now["context"]["plan_generation"] == original["plan_generation"]
        assert now["binding"]["version"] == before["binding"]["version"]
        assert lab.native.confirm_original_exit(UUID(original["run_id"]))
        # Query the same commands and transition; never issue another Stop/goal.
        assert client.get(f"commands/{prepared['command_id']}")["status"] == "applied"
        assert client.get(f"commands/{confirmed['command_id']}")["status"] == "applied"
        observed = client.get(f"handoff-transitions/{transition['id']}")
        assert observed["source_execution_id"] == original["execution_id"]
        assert observed["source_binding_version"] == original["binding_version"]
        assert observed["source_profile_ref"] == profile
        lab.record(
            "original-unknown-handoff",
            {
                "root_task_id": goal["root_task_id"],
                "worker_item_id": mapping["source"],
                "prepare_command_id": prepared["command_id"],
                "confirm_command_id": confirmed["command_id"],
                "source_run_id": original["run_id"],
                "target_profile_ref": target_profile,
                "target_configuration_operation_id": target_operation,
                "unknown_transition": transition,
                "after_known_exit": observed,
            },
        )
    finally:
        client.client.close()
