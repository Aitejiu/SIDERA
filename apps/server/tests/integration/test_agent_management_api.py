import hashlib
import json
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sidera_agents.contracts import ProfileDraft, ProfileRef, RuntimeRef
from sidera_model_connections.http.schemas import CredentialWriteSummarySchema

from sidera_product.agents.contracts import ConfigurationResult
from tests.support.agent_management_inventory import (
    read_private_json,
    write_private_json,
)
from tests.support.agent_management_lab import (
    ManagementColdLab,
    begin_cold_profile_stage,
    complete_cold_profile_stage,
    load_cold_profile_stage,
)
from tests.support.agent_management_lab import (
    management_cold_lab as management_cold_lab,
)
from tests.support.agent_management_work import (
    WorkPinCheckpoint,
    WorkPinIds,
    write_work_pin,
)
from tests.support.product_targets import ProductTargetError


class CommittedReplyLoss:
    def __init__(self, app, path: str) -> None:
        self.app = app
        self.path = path
        self.lost = False

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] != self.path or self.lost:
            await self.app(scope, receive, send)
            return
        messages = []

        async def buffer(message) -> None:
            messages.append(message)

        await self.app(scope, receive, buffer)
        start = next(
            (
                message
                for message in messages
                if message["type"] == "http.response.start"
            ),
            None,
        )
        if start is None or start["status"] != 201:
            for message in messages:
                await send(message)
            return
        self.lost = True
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})


def _public_runtime(lab: ManagementColdLab) -> RuntimeRef:
    listed = lab.client.get(lab.prefix + "/runtimes", headers=lab.headers)
    assert listed.status_code == 200
    matches = [
        item
        for item in listed.json()["items"]
        if item["harness_id"] == lab.native.harness_id
    ]
    if not matches:
        registered = lab.client.post(
            lab.prefix + "/runtimes",
            json={
                "harness_id": lab.native.harness_id,
                "executable": sys.executable,
                "max_active": 2,
                "max_sessions": 2,
            },
            headers=lab.headers,
        )
        assert registered.status_code == 201
        snapshot = registered.json()
    else:
        assert len(matches) == 1
        snapshot = matches[0]
    assert snapshot["installed"] is True and snapshot["enabled"] is True
    ref = RuntimeRef.model_validate(snapshot["ref"])
    lab.owned["harness_installation"] = {
        "ref": ref.model_dump(mode="json"),
        "harness_id": snapshot["harness_id"],
        "installed": snapshot["installed"],
        "enabled": snapshot["enabled"],
        "version": snapshot["version"],
    }
    return ref


def _profile_draft(
    runtime_ref: RuntimeRef, *, instructions: str, name: str = "N20 retained Work pin"
) -> ProfileDraft:
    return ProfileDraft(
        name=name,
        description="Owned cold fixture profile",
        runtime_ref=runtime_ref,
        instructions=instructions,
        resource_refs=(),
        model_id=None,
        thinking=None,
        max_active=2,
        environment={},
        secret_refs=(),
        native_settings={},
    )


def _draft_sha256(draft: ProfileDraft) -> str:
    payload = json.dumps(
        draft.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_configuration_request(
    lab: ManagementColdLab, operation_id: str
) -> dict[str, object] | None:
    response = lab.client.get(
        f"/product/v1/workspaces/{lab.workspace_id}/configuration-requests",
        params={"operationId": operation_id},
        headers=lab.headers,
    )
    try:
        body = response.json()
    except ValueError:
        raise ProductTargetError("cold_configuration_lookup_result_invalid") from None
    if response.status_code == 404 and body == {
        "detail": {"code": "configuration_request_not_found"}
    }:
        return None
    try:
        result = ConfigurationResult.model_validate(body)
    except ValidationError:
        raise ProductTargetError("cold_configuration_lookup_result_invalid") from None
    if (
        result.operation_id != operation_id
        or result.target_ref.workspace_id != lab.workspace_id
        or (result.result_ref is not None and result.result_ref != result.target_ref)
        or (result.state in {"succeeded", "reconciled"} and result.result_ref is None)
    ):
        raise ProductTargetError("cold_configuration_lookup_result_unmatched")
    expected_status = (
        202
        if result.state in {"claimed", "dispatching", "unknown"}
        else (
            422
            if result.reason_code == "configuration_invalid"
            else 403
            if result.reason_code == "permission_denied"
            else 404
            if result.reason_code == "not_found"
            else 409
        )
        if result.state == "rejected"
        else 200
    )
    if response.status_code != expected_status:
        raise ProductTargetError("cold_configuration_lookup_status_unmatched")
    return result.model_dump(mode="json", by_alias=True)


def _configuration_facts(result: dict[str, object]) -> dict[str, object]:
    return {
        key: result[key]
        for key in ("operationId", "state", "reason_code", "target_ref", "result_ref")
        if key in result
    }


def _validate_pending_revision_result(
    operation_id: str,
    parent_ref: ProfileRef,
    result_facts: dict[str, object],
) -> ProfileRef:
    if type(operation_id) is not str or not operation_id:
        raise ProductTargetError("cold_profile_revise_operation_mismatch")
    if type(parent_ref) is not ProfileRef or type(result_facts) is not dict:
        raise ProductTargetError("cold_profile_revise_result_invalid")
    try:
        result = ConfigurationResult.model_validate(result_facts)
    except ValidationError:
        raise ProductTargetError("cold_profile_revise_result_invalid") from None
    if result.operation_id != operation_id:
        raise ProductTargetError("cold_profile_revise_operation_mismatch")
    if result.state not in {"succeeded", "reconciled"}:
        raise ProductTargetError("cold_profile_revise_result_unconfirmed")
    if (
        type(result.target_ref) is not ProfileRef
        or type(result.result_ref) is not ProfileRef
    ):
        raise ProductTargetError("cold_profile_revise_result_invalid")
    expected_child_values = parent_ref.model_dump(mode="python")
    expected_child_values["revision"] = parent_ref.revision + 1
    try:
        expected_child = ProfileRef.model_validate(expected_child_values)
    except ValidationError:
        raise ProductTargetError("cold_profile_revise_parent_invalid") from None
    if result.target_ref != expected_child or result.result_ref != expected_child:
        raise ProductTargetError("cold_profile_revise_result_mismatch")
    return result.result_ref


def _restore_work_pin(
    lab: ManagementColdLab,
    facts: dict[str, object],
    profile_ref: ProfileRef,
) -> WorkPinCheckpoint:
    expected_keys = {
        "workspace_id",
        "task_id",
        "execution_id",
        "authority_id",
        "start_operation_id",
        "principal_id",
        "profile_ref",
    }
    if type(facts) is not dict or set(facts) != expected_keys:
        raise ProductTargetError("cold_work_pin_stage_refused")
    string_fields = (
        "workspace_id",
        "task_id",
        "execution_id",
        "authority_id",
        "start_operation_id",
        "principal_id",
    )
    if any(type(facts[key]) is not str for key in string_fields):
        raise ProductTargetError("cold_work_pin_stage_refused")
    try:
        ids = WorkPinIds(
            workspace_id=UUID(facts["workspace_id"]),
            task_id=UUID(facts["task_id"]),
            execution_id=UUID(facts["execution_id"]),
            authority_id=UUID(facts["authority_id"]),
            start_operation_id=UUID(facts["start_operation_id"]),
        )
        saved_profile_ref = ProfileRef.model_validate(facts["profile_ref"])
    except (KeyError, TypeError, ValueError):
        raise ProductTargetError("cold_work_pin_stage_refused") from None
    if (
        ids.workspace_id != lab.workspace_id
        or facts["principal_id"] != lab.runtime.settings.local_principal_id
        or saved_profile_ref != profile_ref
    ):
        raise ProductTargetError("cold_work_pin_stage_refused")
    return WorkPinCheckpoint(
        ids=ids,
        principal_id=lab.runtime.settings.local_principal_id,
        profile_ref=profile_ref,
        checkpoint_path=lab.evidence_directory
        / "work-pin"
        / f"{lab.workspace_id}.json",
    )


@pytest.mark.integration
def test_cold_public_profile_work_keeps_exact_historical_pin_after_revision(
    management_cold_lab: ManagementColdLab,
) -> None:
    lab = management_cold_lab
    assert lab.runtime.status()["lifecycle"] == "constructed"
    lab.exchange_session()
    runtime_ref = _public_runtime(lab)
    draft_r1 = _profile_draft(runtime_ref, instructions="Retained revision one.")
    operation_id = "n20-cold-profile-create"
    create_body = {
        "operationId": operation_id,
        "draft": draft_r1.model_dump(mode="json"),
    }
    create_intent = {
        "operation_id": operation_id,
        "draft_sha256": _draft_sha256(draft_r1),
    }
    create_started, create_stage = begin_cold_profile_stage(
        lab.evidence_directory, lab.workspace_id, "profile_create", create_intent
    )
    if create_started:
        lost_reply = CommittedReplyLoss(lab.client.app, lab.prefix + "/profiles")
        uncertain_client = TestClient(
            lost_reply, base_url=lab.runtime.settings.local_url
        )
        uncertain_client.cookies.update(lab.client.cookies)
        try:
            uncertain = uncertain_client.post(
                lab.prefix + "/profiles", json=create_body, headers=lab.headers
            )
        finally:
            uncertain_client.close()
        assert uncertain.status_code == 503 and lost_reply.lost is True
        original = _read_configuration_request(lab, operation_id)
        assert original is not None
        create_operation = _configuration_facts(original)
        assert (
            create_operation["operationId"] == operation_id
            and create_operation["state"] == "succeeded"
            and create_operation["target_ref"] == create_operation["result_ref"]
        )
        duplicate = lab.client.post(
            lab.prefix + "/profiles", json=create_body, headers=lab.headers
        )
        assert duplicate.status_code == 201 and duplicate.json() == original
        create_stage = {
            "operation": create_operation,
            "reply_loss_status": 503,
        }
        complete_cold_profile_stage(
            lab.evidence_directory,
            lab.workspace_id,
            "profile_create",
            create_intent,
            create_stage,
        )
    else:
        original = _read_configuration_request(lab, operation_id)
        if create_stage is None:
            raise ProductTargetError("cold_profile_create_pending_reply_unavailable")
        if original is None or type(create_stage) is not dict:
            raise ProductTargetError("cold_profile_create_recorded_read_refused")
        else:
            create_operation = _configuration_facts(original)
            if create_operation != create_stage.get("operation"):
                raise ProductTargetError("cold_profile_create_recorded_read_refused")
    if (
        type(create_stage) is not dict
        or type(create_stage.get("operation")) is not dict
    ):
        raise ProductTargetError("cold_profile_create_recorded_read_refused")
    if (
        set(create_stage) != {"operation", "reply_loss_status"}
        or create_stage.get("reply_loss_status") != 503
    ):
        raise ProductTargetError("cold_profile_create_recorded_read_refused")
    create_operation = create_stage["operation"]
    ref_r1 = ProfileRef.model_validate(create_operation["result_ref"])
    lab.record_profile(operation_id, create_operation)
    lab.owned["reply_loss_boundary"] = {
        "boundary": "outer ASGI response send after route completion",
        "observed_status": create_stage["reply_loss_status"],
        "original_operation_id": operation_id,
    }

    work_intent = {"profile_ref": ref_r1.model_dump(mode="json")}
    _, work_stage = begin_cold_profile_stage(
        lab.evidence_directory, lab.workspace_id, "work_seed", work_intent
    )
    if work_stage is None:
        work_pin = write_work_pin(
            lab.guards[2],
            workspace_id=lab.workspace_id,
            principal_id=lab.runtime.settings.local_principal_id,
            profile_ref=ref_r1,
            evidence_directory=lab.evidence_directory,
        )
        work_stage = {"work_pin": work_pin.facts()}
        complete_cold_profile_stage(
            lab.evidence_directory,
            lab.workspace_id,
            "work_seed",
            work_intent,
            work_stage,
        )
    else:
        if type(work_stage) is not dict or type(work_stage.get("work_pin")) is not dict:
            raise ProductTargetError("cold_work_pin_stage_refused")
        work_pin = _restore_work_pin(lab, work_stage["work_pin"], ref_r1)
    lab.owned["work_pin"] = work_pin.facts()

    draft_r2 = _profile_draft(runtime_ref, instructions="Reviewed revision two.")
    revision_operation = "n20-cold-profile-revision-2"
    stale_revision_operation = "n20-cold-profile-stale-r1"
    revision_intent = {
        "operation_id": revision_operation,
        "stale_operation_id": stale_revision_operation,
        "expected_ref": ref_r1.model_dump(mode="json"),
        "draft_sha256": _draft_sha256(draft_r2),
    }
    revision_started, revision_stage = begin_cold_profile_stage(
        lab.evidence_directory,
        lab.workspace_id,
        "profile_revise",
        revision_intent,
    )
    if revision_started:
        revised = lab.client.post(
            lab.prefix + f"/profiles/{ref_r1.profile_id}/revisions",
            json={
                "operationId": revision_operation,
                "expected_ref": ref_r1.model_dump(mode="json"),
                "draft": draft_r2.model_dump(mode="json"),
            },
            headers=lab.headers,
        )
        assert revised.status_code == 200
        revision_result = revised.json()
        ref_r2 = ProfileRef.model_validate(revision_result["result_ref"])
        assert ref_r2.profile_id == ref_r1.profile_id and ref_r2.revision == 2
        stale_revision = lab.client.post(
            lab.prefix + f"/profiles/{ref_r1.profile_id}/revisions",
            json={
                "operationId": stale_revision_operation,
                "expected_ref": ref_r1.model_dump(mode="json"),
                "draft": draft_r2.model_dump(mode="json"),
            },
            headers=lab.headers,
        )
        assert stale_revision.status_code == 409
        assert stale_revision.json()["reason_code"] == "revision_conflict"
        stale_request = _read_configuration_request(lab, stale_revision_operation)
        revision_request = _read_configuration_request(lab, revision_operation)
        assert revision_request is not None and stale_request is not None
        revision_facts = _configuration_facts(revision_request)
        stale_facts = _configuration_facts(stale_request)
        assert revision_facts["result_ref"] == ref_r2.model_dump(mode="json")
        assert stale_facts.get("reason_code") == "revision_conflict"
        revision_stage = {
            "revision_operation": revision_facts,
            "stale_operation": stale_facts,
            "revision_response_status": revised.status_code,
            "stale_response_status": stale_revision.status_code,
            "reconciled_via_public_reads": False,
        }
        complete_cold_profile_stage(
            lab.evidence_directory,
            lab.workspace_id,
            "profile_revise",
            revision_intent,
            revision_stage,
        )
    elif revision_stage is None:
        revision_request = _read_configuration_request(lab, revision_operation)
        stale_request = _read_configuration_request(lab, stale_revision_operation)
        if revision_request is None or stale_request is None:
            raise ProductTargetError(
                "cold_profile_revise_pending_operation_unavailable"
            )
        revision_facts = _configuration_facts(revision_request)
        stale_facts = _configuration_facts(stale_request)
        ref_r2 = _validate_pending_revision_result(
            revision_operation, ref_r1, revision_facts
        )
        if (
            stale_facts.get("operationId") != stale_revision_operation
            or stale_facts.get("reason_code") != "revision_conflict"
        ):
            raise ProductTargetError("cold_profile_revise_pending_result_unmatched")
        revision_stage = {
            "revision_operation": revision_facts,
            "stale_operation": stale_facts,
            "reconciled_via_public_reads": True,
        }
        complete_cold_profile_stage(
            lab.evidence_directory,
            lab.workspace_id,
            "profile_revise",
            revision_intent,
            revision_stage,
        )
    else:
        revision_request = _read_configuration_request(lab, revision_operation)
        stale_request = _read_configuration_request(lab, stale_revision_operation)
        if revision_request is None or stale_request is None:
            raise ProductTargetError("cold_profile_revise_recorded_read_refused")
        revision_facts = _configuration_facts(revision_request)
        stale_facts = _configuration_facts(stale_request)
        if revision_facts != revision_stage.get(
            "revision_operation"
        ) or stale_facts != revision_stage.get("stale_operation"):
            raise ProductTargetError("cold_profile_revise_recorded_read_refused")
        ref_r2 = ProfileRef.model_validate(revision_facts.get("result_ref"))
    if type(revision_stage) is not dict:
        raise ProductTargetError("cold_profile_revise_recorded_read_refused")
    if revision_stage.get("reconciled_via_public_reads") is True:
        assert "revision_response_status" not in revision_stage
        assert "stale_response_status" not in revision_stage
    else:
        assert revision_stage.get("revision_response_status") == 200
        assert revision_stage.get("stale_response_status") == 409
    assert ref_r2.profile_id == ref_r1.profile_id and ref_r2.revision == 2
    lab.record_profile(revision_operation, revision_facts)

    status_path = lab.prefix + f"/profiles/{ref_r1.profile_id}/status"
    status_record = load_cold_profile_stage(
        lab.evidence_directory, lab.workspace_id, "status_cas"
    )
    if status_record is None:
        initial_head = lab.client.get(lab.prefix + f"/profiles/{ref_r1.profile_id}")
        assert initial_head.status_code == 200
        initial_status = initial_head.json()["status"]
        initial_version = initial_status["version"]
        initial_enabled = initial_status["enabled"]
        assert type(initial_version) is int and type(initial_enabled) is bool
        normalize = initial_enabled is False
        disable_expected_version = initial_version + (1 if normalize else 0)
        status_intent = {
            "profile_ref": ref_r2.model_dump(mode="json"),
            "initial_version": initial_version,
            "initial_enabled": initial_enabled,
            "normalize_to_enabled": normalize,
            "normalization_expected_version": initial_version if normalize else None,
            "disable_expected_version": disable_expected_version,
            "stale_expected_version": disable_expected_version,
            "restore_expected_version": disable_expected_version + 1,
            "desired_enabled": {
                "normalized": True,
                "disabled": False,
                "restored": True,
            },
        }
    else:
        status_intent = status_record[0]
        expected_status_keys = {
            "profile_ref",
            "initial_version",
            "initial_enabled",
            "normalize_to_enabled",
            "normalization_expected_version",
            "disable_expected_version",
            "stale_expected_version",
            "restore_expected_version",
            "desired_enabled",
        }
        if (
            set(status_intent) != expected_status_keys
            or status_intent.get("profile_ref") != ref_r2.model_dump(mode="json")
            or status_intent.get("desired_enabled")
            != {"normalized": True, "disabled": False, "restored": True}
            or type(status_intent.get("initial_version")) is not int
            or status_intent.get("initial_version", 0) < 1
            or type(status_intent.get("initial_enabled")) is not bool
            or type(status_intent.get("normalize_to_enabled")) is not bool
            or status_intent.get("normalize_to_enabled")
            is not (status_intent.get("initial_enabled") is False)
            or type(status_intent.get("disable_expected_version")) is not int
            or type(status_intent.get("stale_expected_version")) is not int
            or type(status_intent.get("restore_expected_version")) is not int
            or status_intent.get("disable_expected_version")
            != status_intent.get("initial_version")
            + (1 if status_intent.get("normalize_to_enabled") else 0)
            or status_intent.get("stale_expected_version")
            != status_intent.get("disable_expected_version")
            or status_intent.get("restore_expected_version")
            != status_intent.get("disable_expected_version") + 1
            or status_intent.get("normalization_expected_version")
            != (
                status_intent.get("initial_version")
                if status_intent.get("normalize_to_enabled")
                else None
            )
        ):
            raise ProductTargetError("cold_status_cas_intent_refused")
    status_started, status_stage = begin_cold_profile_stage(
        lab.evidence_directory,
        lab.workspace_id,
        "status_cas",
        status_intent,
    )
    if status_started:
        if status_intent["normalize_to_enabled"] is True:
            normalized = lab.client.put(
                status_path,
                json={
                    "enabled": True,
                    "expected_version": status_intent["normalization_expected_version"],
                },
                headers=lab.headers,
            )
            assert normalized.status_code == 200
            assert (
                normalized.json()["version"]
                == status_intent["disable_expected_version"]
            )
            assert normalized.json()["enabled"] is True
        disabled = lab.client.put(
            status_path,
            json={
                "enabled": False,
                "expected_version": status_intent["disable_expected_version"],
            },
            headers=lab.headers,
        )
        assert disabled.status_code == 200 and disabled.json()["enabled"] is False
        assert disabled.json()["version"] == status_intent["restore_expected_version"]
        stale_status = lab.client.put(
            status_path,
            json={
                "enabled": True,
                "expected_version": status_intent["stale_expected_version"],
            },
            headers=lab.headers,
        )
        assert stale_status.status_code == 409
        restored = lab.client.put(
            status_path,
            json={
                "enabled": True,
                "expected_version": status_intent["restore_expected_version"],
            },
            headers=lab.headers,
        )
        assert restored.status_code == 200 and restored.json()["enabled"] is True
        assert (
            restored.json()["version"] == status_intent["restore_expected_version"] + 1
        )
        status_stage = {
            "initial_version": status_intent["initial_version"],
            "initial_enabled": status_intent["initial_enabled"],
            "normalized": status_intent["normalize_to_enabled"],
            "disable_expected_version": status_intent["disable_expected_version"],
            "disable_version": disabled.json()["version"],
            "stale_status": stale_status.status_code,
            "restore_expected_version": status_intent["restore_expected_version"],
            "final_version": restored.json()["version"],
            "final_enabled": restored.json()["enabled"],
        }
        complete_cold_profile_stage(
            lab.evidence_directory,
            lab.workspace_id,
            "status_cas",
            status_intent,
            status_stage,
        )
    elif status_stage is None:
        current_head = lab.client.get(lab.prefix + f"/profiles/{ref_r1.profile_id}")
        assert current_head.status_code == 200
        raise ProductTargetError("cold_status_cas_pending_response_unavailable")
    else:
        current_head = lab.client.get(lab.prefix + f"/profiles/{ref_r1.profile_id}")
        assert current_head.status_code == 200
        current_status = current_head.json()["status"]
        if (
            type(status_stage) is not dict
            or set(status_stage)
            != {
                "initial_version",
                "initial_enabled",
                "normalized",
                "disable_expected_version",
                "disable_version",
                "stale_status",
                "restore_expected_version",
                "final_version",
                "final_enabled",
            }
            or type(status_stage.get("final_version")) is not int
            or status_stage.get("initial_version") != status_intent["initial_version"]
            or status_stage.get("normalized")
            is not status_intent["normalize_to_enabled"]
            or status_stage.get("stale_status") != 409
            or status_stage.get("final_enabled") is not True
            or current_status["version"] != status_stage["final_version"]
            or current_status["enabled"] is not True
        ):
            raise ProductTargetError("cold_status_cas_recorded_read_refused")

    defaults_path = f"/product/v1/workspaces/{lab.workspace_id}/defaults"
    defaults_record = load_cold_profile_stage(
        lab.evidence_directory, lab.workspace_id, "defaults_cas"
    )
    expected_profile_ids = {
        "main_profile_id": str(ref_r1.profile_id),
        "planner_profile_id": str(ref_r1.profile_id),
        "worker_profile_id": str(ref_r1.profile_id),
    }
    if defaults_record is None:
        defaults_before = lab.client.get(defaults_path, headers=lab.headers)
        assert defaults_before.status_code == 200
        defaults_intent = {
            "expected_version": defaults_before.json()["version"],
            **expected_profile_ids,
        }
        assert type(defaults_intent["expected_version"]) is int
    else:
        defaults_intent = defaults_record[0]
        if (
            set(defaults_intent) != {"expected_version", *expected_profile_ids}
            or any(
                defaults_intent.get(key) != value
                for key, value in expected_profile_ids.items()
            )
            or type(defaults_intent.get("expected_version")) is not int
        ):
            raise ProductTargetError("cold_defaults_cas_intent_refused")
    defaults_started, defaults_stage = begin_cold_profile_stage(
        lab.evidence_directory,
        lab.workspace_id,
        "defaults_cas",
        defaults_intent,
    )
    if defaults_started:
        defaults_saved = lab.client.put(
            defaults_path, json=defaults_intent, headers=lab.headers
        )
        assert defaults_saved.status_code == 200
        assert (
            defaults_saved.json()["version"] == defaults_intent["expected_version"] + 1
        )
        assert all(
            defaults_saved.json()[key] == value
            for key, value in expected_profile_ids.items()
        )
        defaults_stale = lab.client.put(
            defaults_path, json=defaults_intent, headers=lab.headers
        )
        assert defaults_stale.status_code == 409
        defaults_stage = {
            "expected_version": defaults_intent["expected_version"],
            "version": defaults_saved.json()["version"],
            "stale_status": defaults_stale.status_code,
            **expected_profile_ids,
        }
        complete_cold_profile_stage(
            lab.evidence_directory,
            lab.workspace_id,
            "defaults_cas",
            defaults_intent,
            defaults_stage,
        )
    elif defaults_stage is None:
        current_defaults = lab.client.get(defaults_path, headers=lab.headers)
        assert current_defaults.status_code == 200
        raise ProductTargetError("cold_defaults_cas_pending_response_unavailable")
    else:
        current_defaults = lab.client.get(defaults_path, headers=lab.headers)
        assert current_defaults.status_code == 200
        current = current_defaults.json()
        if (
            type(defaults_stage) is not dict
            or set(defaults_stage)
            != {
                "expected_version",
                "version",
                "stale_status",
                *expected_profile_ids,
            }
            or defaults_stage.get("expected_version")
            != defaults_intent["expected_version"]
            or type(defaults_stage.get("version")) is not int
            or defaults_stage.get("stale_status") != 409
            or current["version"] != defaults_stage.get("version")
            or any(
                current.get(key) != value for key, value in expected_profile_ids.items()
            )
        ):
            raise ProductTargetError("cold_defaults_cas_recorded_read_refused")

    final_intent = {
        "profile_ref_r1": ref_r1.model_dump(mode="json"),
        "profile_ref_r2": ref_r2.model_dump(mode="json"),
        "task_id": str(work_pin.ids.task_id),
        "execution_id": str(work_pin.ids.execution_id),
    }
    _, final_stage = begin_cold_profile_stage(
        lab.evidence_directory, lab.workspace_id, "final_reads", final_intent
    )
    head = lab.client.get(lab.prefix + f"/profiles/{ref_r1.profile_id}")
    old = lab.client.get(
        lab.prefix + f"/profiles/{ref_r1.profile_id}", params={"revision": 1}
    )
    assignment = lab.client.get(
        f"/api/v1/workspaces/{lab.workspace_id}/assignments/subtask/{work_pin.ids.task_id}"
    )
    execution = lab.client.get(
        f"/api/v1/workspaces/{lab.workspace_id}/agent-executions/{work_pin.ids.execution_id}"
    )
    profile_work = lab.client.get(lab.prefix + f"/profiles/{ref_r1.profile_id}/work")
    run_summary = lab.client.get(
        f"/api/v1/workspaces/{lab.workspace_id}/profile-work/execution/{work_pin.ids.execution_id}/run"
    )
    defaults_current = lab.client.get(defaults_path, headers=lab.headers)
    assert head.status_code == old.status_code == 200
    assert head.json()["profile"]["ref"] == ref_r2.model_dump(mode="json")
    assert old.json()["profile"]["ref"] == ref_r1.model_dump(mode="json")
    assert head.json()["status"]["enabled"] is True
    assert head.json()["status"]["version"] == status_stage["final_version"]
    assert assignment.status_code == execution.status_code == 200
    assert assignment.json()["version"] == 1
    assert assignment.json()["profile_ref"] == ref_r1.model_dump(mode="json")
    assert execution.json()["profile_ref"] == ref_r1.model_dump(mode="json")
    assert execution.json()["status"] == "queued"
    assert execution.json()["link_status"] == "pending"
    assert execution.json()["run_id"] is None and execution.json()["event_cursor"] == 0
    assert profile_work.status_code == 200
    work_rows = profile_work.json()["task_work"]["items"]
    execution_rows = [
        row
        for row in work_rows
        if row["kind"] == "execution"
        and row["work_id"] == str(work_pin.ids.execution_id)
    ]
    assert len(execution_rows) == 1
    assert execution_rows[0]["profile_ref"] == ref_r1.model_dump(mode="json")
    assert execution_rows[0]["link_status"] == "pending"
    assert run_summary.status_code == 200
    assert run_summary.json()["available"] is False
    assert run_summary.json()["run_id"] is None
    assert run_summary.json()["reason_code"] == "run_not_admitted"
    assert defaults_current.status_code == 200
    assert defaults_current.json()["version"] == defaults_stage["version"]
    assert all(
        defaults_current.json()[key] == value
        for key, value in expected_profile_ids.items()
    )

    foreign_workspace = lab.client.get(
        f"/api/v1/workspaces/{uuid5(lab.workspace_id, 'foreign')}/agent-executions/{work_pin.ids.execution_id}"
    )
    assert foreign_workspace.status_code == 403
    no_native_open_or_prompt = all(
        call[0] not in {"open", "prompt"} for call in lab.native.calls
    )
    assert no_native_open_or_prompt is True
    assert lab.runtime.status()["lifecycle"] == "constructed"
    final_result = {
        "head_ref": head.json()["profile"]["ref"],
        "historical_ref": old.json()["profile"]["ref"],
        "status_version": head.json()["status"]["version"],
        "status_enabled": head.json()["status"]["enabled"],
        "defaults_version": defaults_current.json()["version"],
        "assignment_version": assignment.json()["version"],
        "execution_status": execution.json()["status"],
        "link_status": execution.json()["link_status"],
        "run_available": run_summary.json()["available"],
        "run_reason_code": run_summary.json()["reason_code"],
        "foreign_workspace_status": foreign_workspace.status_code,
        "no_native_open_or_prompt": no_native_open_or_prompt,
        "runtime_lifecycle": lab.runtime.status()["lifecycle"],
    }
    if final_stage is not None and final_stage != final_result:
        raise ProductTargetError("cold_final_reads_recorded_result_refused")
    complete_cold_profile_stage(
        lab.evidence_directory,
        lab.workspace_id,
        "final_reads",
        final_intent,
        final_result,
    )


@pytest.mark.integration
def test_cold_concurrent_profile_create_reserves_one_public_revision(
    management_cold_lab: ManagementColdLab,
) -> None:
    lab = management_cold_lab
    assert lab.runtime.status()["lifecycle"] == "constructed"
    lab.exchange_session()
    runtime_ref = _public_runtime(lab)
    draft = _profile_draft(
        runtime_ref,
        name="N20 concurrent create",
        instructions="One canonical concurrent create.",
    )
    operation_id = "n20-cold-concurrent-profile-create"
    intent = {
        "workspace_id": str(lab.workspace_id),
        "operation_id": operation_id,
        "draft_sha256": _draft_sha256(draft),
    }
    evidence_root = (
        lab.evidence_directory
        / "cold-concurrent-profile-create"
        / str(lab.workspace_id)
    )
    pending_path = evidence_root / "pending.json"
    complete_path = evidence_root / "complete.json"

    def read_record(path, phase: str) -> dict[str, object]:
        try:
            value = read_private_json(path)
        except (OSError, ValueError, ProductTargetError):
            raise ProductTargetError("cold_concurrent_profile_stage_refused") from None
        fields = {"schema_version", "case", "workspace_id", "phase", "intent"}
        if phase == "complete":
            fields.add("result")
        if (
            type(value) is not dict
            or set(value) != fields
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or value.get("case") != "cold-concurrent-profile-create"
            or value.get("workspace_id") != str(lab.workspace_id)
            or value.get("phase") != phase
            or value.get("intent") != intent
            or (phase == "complete" and type(value.get("result")) is not dict)
        ):
            raise ProductTargetError("cold_concurrent_profile_stage_refused")
        return value

    def read_public_snapshot() -> dict[str, object]:
        operation = None
        for _ in range(40):
            operation = _read_configuration_request(lab, operation_id)
            if operation is not None and operation.get("state") in {
                "succeeded",
                "reconciled",
            }:
                break
            time.sleep(0.05)
        if operation is None or operation.get("state") not in {
            "succeeded",
            "reconciled",
        }:
            raise ProductTargetError("cold_concurrent_original_operation_unsettled")
        operation_facts = _configuration_facts(operation)
        target_ref = ProfileRef.model_validate(operation_facts.get("target_ref"))
        result_ref = ProfileRef.model_validate(operation_facts.get("result_ref"))
        if target_ref != result_ref or result_ref.revision != 1:
            raise ProductTargetError("cold_concurrent_original_result_unmatched")
        listed = lab.client.get(lab.prefix + "/profiles", headers=lab.headers)
        assert listed.status_code == 200
        matches = [
            item
            for item in listed.json()["items"]
            if item["profile"]["ref"]["profile_id"] == str(result_ref.profile_id)
        ]
        detail = lab.client.get(lab.prefix + f"/profiles/{result_ref.profile_id}")
        revision_two = lab.client.get(
            lab.prefix + f"/profiles/{result_ref.profile_id}",
            params={"revision": 2},
        )
        assert detail.status_code == 200 and revision_two.status_code == 404
        head_ref = detail.json()["profile"]["ref"]
        assert len(matches) == 1
        assert head_ref == result_ref.model_dump(mode="json")
        assert result_ref.revision == 1
        return {
            "operation": operation_facts,
            "profile_ref": result_ref.model_dump(mode="json"),
            "listed_count": len(matches),
            "head_ref": head_ref,
            "revision_two_status": revision_two.status_code,
        }

    pending_exists = pending_path.exists()
    complete_exists = complete_path.exists()
    if complete_exists:
        if not pending_exists:
            raise ProductTargetError("cold_concurrent_profile_stage_refused")
        read_record(pending_path, "pending")
        completed_record = read_record(complete_path, "complete")
        saved_result = completed_record["result"]
        observation = read_public_snapshot()
        expected_result_keys = {
            "response_statuses",
            "reserved_target_ref",
            "operation",
            "profile_ref",
            "listed_count",
            "head_ref",
            "revision_two_status",
        }
        if (
            type(saved_result) is not dict
            or set(saved_result) != expected_result_keys
            or type(saved_result.get("response_statuses")) is not list
            or len(saved_result["response_statuses"]) != 2
            or any(
                type(status) is not int or status not in {201, 202}
                for status in saved_result["response_statuses"]
            )
            or saved_result.get("reserved_target_ref") != observation["profile_ref"]
            or saved_result.get("operation") != observation["operation"]
            or saved_result.get("profile_ref") != observation["profile_ref"]
            or saved_result.get("head_ref") != observation["head_ref"]
            or saved_result.get("listed_count") != 1
            or observation["listed_count"] != 1
            or saved_result.get("revision_two_status") != 404
            or observation["revision_two_status"] != 404
        ):
            raise ProductTargetError("cold_concurrent_profile_recorded_read_refused")
    elif pending_exists:
        read_record(pending_path, "pending")
        pending_operation = _read_configuration_request(lab, operation_id)
        lab.client.get(lab.prefix + "/profiles", headers=lab.headers)
        if pending_operation is not None:
            target_ref = pending_operation.get("target_ref")
            if isinstance(target_ref, dict):
                pending_ref = ProfileRef.model_validate(target_ref)
                lab.client.get(lab.prefix + f"/profiles/{pending_ref.profile_id}")
                lab.client.get(
                    lab.prefix + f"/profiles/{pending_ref.profile_id}",
                    params={"revision": 2},
                )
        raise ProductTargetError("cold_concurrent_pending_response_facts_unavailable")
    else:
        write_private_json(
            pending_path,
            {
                "schema_version": 1,
                "case": "cold-concurrent-profile-create",
                "workspace_id": str(lab.workspace_id),
                "phase": "pending",
                "intent": intent,
            },
        )
        body = {
            "operationId": operation_id,
            "draft": draft.model_dump(mode="json"),
        }
        barrier = Barrier(2)
        clients: list[TestClient] = []
        executor: ThreadPoolExecutor | None = None
        try:
            for _ in range(2):
                client = TestClient(
                    lab.client.app, base_url=lab.runtime.settings.local_url
                )
                client.cookies.update(lab.client.cookies)
                clients.append(client)
            executor = ThreadPoolExecutor(max_workers=2)

            def submit(client: TestClient):
                barrier.wait(timeout=10)
                return client.post(
                    lab.prefix + "/profiles", json=body, headers=lab.headers
                )

            futures = [executor.submit(submit, client) for client in clients]
            responses = [future.result(timeout=20) for future in futures]
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            for client in clients:
                client.close()

        response_bodies = [response.json() for response in responses]
        assert all(response.status_code in {201, 202} for response in responses)
        reserved_refs = [body.get("target_ref") for body in response_bodies]
        assert reserved_refs[0] is not None and reserved_refs[0] == reserved_refs[1]
        reserved_ref = ProfileRef.model_validate(reserved_refs[0])
        observation = read_public_snapshot()
        assert observation["profile_ref"] == reserved_ref.model_dump(mode="json")
        assert observation["operation"]["result_ref"] == reserved_ref.model_dump(
            mode="json"
        )
        result = {
            "response_statuses": [response.status_code for response in responses],
            "reserved_target_ref": reserved_ref.model_dump(mode="json"),
            **observation,
        }
        write_private_json(
            complete_path,
            {
                "schema_version": 1,
                "case": "cold-concurrent-profile-create",
                "workspace_id": str(lab.workspace_id),
                "phase": "complete",
                "intent": intent,
                "result": result,
            },
        )
        saved_result = result

    no_native_open_or_prompt = all(
        call[0] not in {"open", "prompt"} for call in lab.native.calls
    )
    assert no_native_open_or_prompt is True
    assert lab.runtime.status()["lifecycle"] == "constructed"
    lab.owned["concurrent_profile_create"] = {
        "operation_id": operation_id,
        "draft_sha256": intent["draft_sha256"],
        "response_statuses": saved_result.get("response_statuses"),
        "reserved_target_ref": saved_result.get("reserved_target_ref"),
        "profile_ref": saved_result.get("profile_ref"),
        "listed_count": 1,
        "revision_two_status": 404,
        "no_native_open_or_prompt": no_native_open_or_prompt,
    }


_MANAGED_CREDENTIAL_WRITE_CASE = "cold-managed-credential-write"
_MANAGED_CREDENTIAL_WRITE_ALIAS = "N20 controlled managed credential"


def _managed_credential_write_paths(
    evidence_directory: Path, workspace_id: UUID
) -> tuple[Path, Path]:
    directory = evidence_directory / _MANAGED_CREDENTIAL_WRITE_CASE / str(workspace_id)
    return directory / "pending.json", directory / "complete.json"


def _managed_credential_write_refused() -> ProductTargetError:
    return ProductTargetError("cold_managed_credential_write_refused")


def _managed_credential_write_path_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise _managed_credential_write_refused() from None


def _read_managed_credential_write_record(
    path: Path,
    *,
    workspace_id: UUID,
    phase: str,
    intent: dict[str, str],
) -> dict[str, object]:
    try:
        value = read_private_json(path)
    except (OSError, ValueError, ProductTargetError):
        raise _managed_credential_write_refused() from None
    fields = {"schema_version", "case", "workspace_id", "phase", "intent"}
    if phase == "complete":
        fields.add("result")
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("case") != _MANAGED_CREDENTIAL_WRITE_CASE
        or value.get("workspace_id") != str(workspace_id)
        or value.get("phase") != phase
        or value.get("intent") != intent
        or (phase == "complete" and type(value.get("result")) is not dict)
    ):
        raise _managed_credential_write_refused()
    return value


def _validated_managed_credential_summary(
    value: object,
    *,
    workspace_id: UUID,
    operation_id: UUID,
    alias: str,
) -> dict[str, object]:
    expected_fields = {"workspace_id", "operation_id", "status", "credential"}
    credential_fields = {
        "workspace_id",
        "credential_id",
        "alias",
        "source",
        "status",
        "version",
        "replaces_id",
    }
    if (
        type(value) is not dict
        or set(value) != expected_fields
        or any(
            type(value.get(key)) is not str for key in expected_fields - {"credential"}
        )
        or type(value.get("credential")) is not dict
        or set(value["credential"]) != credential_fields
        or any(
            type(value["credential"].get(key)) is not str
            for key in credential_fields - {"version", "replaces_id"}
        )
        or type(value["credential"].get("version")) is not int
        or value["credential"].get("replaces_id") is not None
    ):
        raise _managed_credential_write_refused()
    try:
        summary = CredentialWriteSummarySchema.model_validate(value)
        canonical = summary.model_dump(mode="json")
    except (ValidationError, TypeError, ValueError):
        raise _managed_credential_write_refused() from None
    if (
        canonical != value
        or summary.workspace_id != workspace_id
        or summary.operation_id != operation_id
        or summary.status != "completed"
        or summary.credential.workspace_id != workspace_id
        or summary.credential.alias != alias
        or summary.credential.source != "macos_keychain"
        or summary.credential.status != "active"
        or summary.credential.version != 2
        or summary.credential.replaces_id is not None
    ):
        raise _managed_credential_write_refused()
    return canonical


def _read_original_managed_credential_write(
    lab: ManagementColdLab,
    *,
    path: str,
    workspace_id: UUID,
    operation_id: UUID,
    alias: str,
) -> dict[str, object]:
    try:
        response = lab.client.get(path, headers=lab.headers)
    except (httpx.HTTPError, OSError, TimeoutError):
        raise _managed_credential_write_refused() from None
    if response.status_code != 200:
        raise _managed_credential_write_refused()
    try:
        payload = response.json()
    except (TypeError, ValueError):
        raise _managed_credential_write_refused() from None
    return _validated_managed_credential_summary(
        payload,
        workspace_id=workspace_id,
        operation_id=operation_id,
        alias=alias,
    )


def _managed_credential_write(lab: ManagementColdLab) -> dict[str, object]:
    workspace_id = lab.workspace_id
    operation_id = uuid5(workspace_id, "n20-managed-credential-write")
    operation_id_text = str(operation_id)
    intent = {
        "workspace_id": str(workspace_id),
        "operation_id": operation_id_text,
        "alias": _MANAGED_CREDENTIAL_WRITE_ALIAS,
        "expected_source": "macos_keychain",
    }
    pending_path, complete_path = _managed_credential_write_paths(
        lab.evidence_directory, workspace_id
    )
    pending_exists = _managed_credential_write_path_exists(pending_path)
    complete_exists = _managed_credential_write_path_exists(complete_path)
    if complete_exists and not pending_exists:
        raise _managed_credential_write_refused()
    if pending_exists:
        pending_record = _read_managed_credential_write_record(
            pending_path,
            workspace_id=workspace_id,
            phase="pending",
            intent=intent,
        )
    else:
        pending_record = {
            "schema_version": 1,
            "case": _MANAGED_CREDENTIAL_WRITE_CASE,
            "workspace_id": str(workspace_id),
            "phase": "pending",
            "intent": intent,
        }
        try:
            write_private_json(pending_path, pending_record)
        except (OSError, ProductTargetError, TypeError, ValueError):
            raise _managed_credential_write_refused() from None

    recorded_result = None
    if complete_exists:
        complete_record = _read_managed_credential_write_record(
            complete_path,
            workspace_id=workspace_id,
            phase="complete",
            intent=intent,
        )
        recorded_result = _validated_managed_credential_summary(
            complete_record["result"],
            workspace_id=workspace_id,
            operation_id=operation_id,
            alias=_MANAGED_CREDENTIAL_WRITE_ALIAS,
        )

    base = f"/model-config/v1/workspaces/{workspace_id}"
    original_path = base + f"/credential-writes/{operation_id_text}"
    if pending_exists:
        summary = _read_original_managed_credential_write(
            lab,
            path=original_path,
            workspace_id=workspace_id,
            operation_id=operation_id,
            alias=_MANAGED_CREDENTIAL_WRITE_ALIAS,
        )
        if recorded_result is not None and summary != recorded_result:
            raise _managed_credential_write_refused()
    else:
        secret_value = secrets.token_urlsafe(32)
        try:
            saved = lab.client.post(
                base + "/credential-writes",
                json={
                    "operation_id": operation_id_text,
                    "alias": _MANAGED_CREDENTIAL_WRITE_ALIAS,
                    "secret": secret_value,
                },
                headers=lab.headers,
            )
        except (httpx.HTTPError, OSError, TimeoutError):
            raise _managed_credential_write_refused() from None
        if saved.status_code != 201 or secret_value in saved.text:
            raise _managed_credential_write_refused()
        try:
            payload = saved.json()
        except (TypeError, ValueError):
            raise _managed_credential_write_refused() from None
        summary = _validated_managed_credential_summary(
            payload,
            workspace_id=workspace_id,
            operation_id=operation_id,
            alias=_MANAGED_CREDENTIAL_WRITE_ALIAS,
        )

    complete_record = {
        **pending_record,
        "phase": "complete",
        "result": summary,
    }
    if not complete_exists:
        try:
            write_private_json(complete_path, complete_record)
        except (OSError, ProductTargetError, TypeError, ValueError):
            raise _managed_credential_write_refused() from None
    lab.owned["managed_credential_write"] = {
        "operation_id": operation_id_text,
        "credential_id": summary["credential"]["credential_id"],
        "source": summary["credential"]["source"],
        "status": summary["credential"]["status"],
        "write_status": summary["status"],
    }
    return summary


@pytest.mark.integration
def test_cold_public_credential_metadata_and_managed_write_use_controlled_secret_store(
    management_cold_lab: ManagementColdLab,
) -> None:
    lab = management_cold_lab
    lab.exchange_session()
    base = f"/model-config/v1/workspaces/{lab.workspace_id}"
    credentials_path = base + "/credentials"
    alias = "N20 metadata " + lab.workspace_id.hex[:12]
    env_name = "N20_METADATA_" + lab.workspace_id.hex[:12].upper()
    listed = lab.client.get(credentials_path, headers=lab.headers)
    assert listed.status_code == 200
    env_matches = [item for item in listed.json() if item["alias"] == alias]
    if not env_matches:
        registered = lab.client.post(
            base + "/env-credentials",
            json={"alias": alias, "env_name": env_name},
            headers=lab.headers,
        )
        assert registered.status_code == 201
        metadata = registered.json()
    else:
        assert len(env_matches) == 1
        metadata = env_matches[0]
    assert (
        metadata["source"] == "env"
        and metadata["status"] == "active"
        and metadata["version"] == 1
    )
    assert "env_name" not in metadata
    metadata_fields_are_safe = set(metadata) == {
        "workspace_id",
        "credential_id",
        "alias",
        "source",
        "status",
        "version",
        "replaces_id",
    }
    assert metadata_fields_are_safe is True
    lab.owned["env_credential"] = {
        "credential_id": metadata["credential_id"],
        "alias": metadata["alias"],
        "source": metadata["source"],
        "status": metadata["status"],
        "version": metadata["version"],
    }

    summary = _managed_credential_write(lab)
    assert summary["status"] == "completed"
    assert lab.secrets.reads == []
