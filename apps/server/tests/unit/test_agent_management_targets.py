import json
import os
import shutil
import stat
import subprocess
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sidera_agents.contracts import (
    CapabilitySupport,
    NativeCancelState,
    NativeCloseState,
    NativeProcessState,
    ProfileRef,
    RuntimeCandidate,
    SupportStatus,
)
from sidera_agents.runs import RunExecution
from sidera_model_connections.http.router import _write_summary
from sidera_model_connections.postgres.catalog import PostgresConnectionCatalog
from sidera_model_connections.postgres.models import CredentialRow, CredentialWriteRow
from sqlalchemy.engine import URL

from sidera_product.agents.contracts import ConfigurationRequest, ConfigurationResult
from sidera_product.auth import local_session as local_session_module
from sidera_product.auth.local_session import (
    BootstrapIssue,
    read_bootstrap_file,
    write_bootstrap_file,
)
from tests.integration import test_agent_management_api as api_integration_module
from tests.integration import (
    test_agent_management_desktop as desktop_integration_module,
)
from tests.integration.test_agent_management_api import (
    _managed_credential_write,
    _read_configuration_request,
    _validate_pending_revision_result,
)
from tests.support import agent_management_desktop as desktop_module
from tests.support import agent_management_inventory as inventory_module
from tests.support import agent_management_lab as lab_module
from tests.support import agent_management_work as work_module
from tests.support.product_targets import (
    COMPONENTS,
    DB_KINDS,
    SELECTION_KEYS,
    ProductTargetError,
    TargetGuard,
    TargetSelection,
)


def _selection_environ(tmp_path, *, include_evidence: bool) -> dict[str, str]:
    allocation = "sidera-native-harness:product:n17:20261006:coldsrc"
    socket = str(tmp_path / "pg-socket")
    port = "5432"
    environ = {
        "SIDERA_PRODUCT_TEST_ALLOCATION_ID": allocation,
        "SIDERA_PRODUCT_TEST_SOCKET_DIRECTORY": socket,
        "SIDERA_PRODUCT_TEST_PORT": port,
    }
    for component in COMPONENTS:
        database = f"sidera_{DB_KINDS[component]}_test_n17_coldsrc"
        owner = f"n17_{component}_coldsrc"
        prefix = f"SIDERA_PRODUCT_TEST_{component.upper()}_"
        environ[prefix + "DATABASE_URL"] = URL.create(
            "postgresql+psycopg",
            username=owner,
            database=database,
            query={"host": socket, "port": port},
        ).render_as_string(hide_password=False)
        environ[prefix + "EXPECTED_DATABASE"] = database
        environ[prefix + "EXPECTED_OWNER"] = owner
        environ[prefix + "EXPECTED_MARKER"] = allocation + ":" + component
    if include_evidence:
        evidence = tmp_path / "private-evidence"
        evidence.mkdir(mode=0o700)
        environ["SIDERA_PRODUCT_TEST_EVIDENCE_DIRECTORY"] = str(evidence)
    return environ


def _url_target(tmp_path, component: str, index: int) -> TargetSelection:
    owner = f"n20_{component}"
    database = f"sidera_{DB_KINDS[component]}_test_n17_cold"
    socket = str(tmp_path / "absent-socket")
    return TargetSelection(
        component=component,
        lock_index=index,
        url=URL.create(
            "postgresql+psycopg",
            username=owner,
            database=database,
            query={"host": socket, "port": "5432"},
        ),
        database=database,
        owner=owner,
        marker="inert:" + component,
        socket=socket,
        port=5432,
    )


def _fresh_desktop_environ(
    tmp_path, *, include_evidence: bool = True, include_manifest: bool = True
) -> dict[str, str]:
    allocation = "sidera-native-harness:product:n17:20261006:fresh9b"
    socket = str(tmp_path / "fresh-pg-socket")
    port = "55439"
    environ = {
        "SIDERA_PRODUCT_TEST_ALLOCATION_ID": allocation,
        "SIDERA_PRODUCT_TEST_SOCKET_DIRECTORY": socket,
        "SIDERA_PRODUCT_TEST_PORT": port,
    }
    targets = []
    for component in COMPONENTS:
        database = f"sidera_{DB_KINDS[component]}_test_n17_fresh9b"
        owner = f"n17_{component}_fresh9b"
        marker = allocation + ":" + component
        prefix = f"SIDERA_PRODUCT_TEST_{component.upper()}_"
        environ[prefix + "DATABASE_URL"] = URL.create(
            "postgresql+psycopg",
            username=owner,
            database=database,
            query={"host": socket, "port": port},
        ).render_as_string(hide_password=False)
        environ[prefix + "EXPECTED_DATABASE"] = database
        environ[prefix + "EXPECTED_OWNER"] = owner
        environ[prefix + "EXPECTED_MARKER"] = marker
        targets.append(
            {
                "component": component,
                "database": database,
                "owner": owner,
                "marker": marker,
                "socket": socket,
                "port": int(port),
            }
        )
    evidence = tmp_path / "fresh-evidence"
    if include_evidence:
        evidence.mkdir(mode=0o700)
        environ["SIDERA_PRODUCT_TEST_EVIDENCE_DIRECTORY"] = str(evidence)
    if include_manifest:
        manifest_directory = tmp_path / "fresh-manifest"
        manifest_directory.mkdir(mode=0o700)
        manifest = manifest_directory / "allocation.json"
        inventory_module.write_private_json(
            manifest,
            {
                "schema_version": 1,
                "phase": "fresh9B",
                "custody": "implementation",
                "allocation_id": allocation,
                "empty_at_transfer": True,
                "targets": targets,
            },
        )
        environ["SIDERA_PRODUCT_TEST_FRESH_MANIFEST"] = str(manifest)
    return environ


def _revision_result_facts(state: str = "succeeded"):
    workspace_id = UUID("2c947615-4112-4ad6-91ac-d54bca170108")
    parent_ref = ProfileRef(
        workspace_id=workspace_id,
        profile_id=UUID("7c7948dd-0c8d-417f-bc5b-aa6f0f2b0a63"),
        revision=1,
    )
    child_ref = ProfileRef(
        workspace_id=workspace_id,
        profile_id=parent_ref.profile_id,
        revision=2,
    )
    timestamp = datetime(2026, 10, 6, tzinfo=UTC)
    request = ConfigurationRequest(
        workspace_id=workspace_id,
        principal_id="n20-resume-characterization",
        operation_id="n20-original-profile-revision",
        action="profile.revise",
        request_fingerprint="a" * 64,
        draft_fingerprint="b" * 64,
        target_ref=child_ref,
        expected_ref=parent_ref,
        state=state,
        version=1,
        created_at=timestamp,
        updated_at=timestamp,
        reason_code=None,
        result_ref=child_ref,
    )
    result = ConfigurationResult.from_request(request)
    return (
        parent_ref,
        child_ref,
        request,
        result.model_dump(mode="json", by_alias=True),
    )


def _configuration_lookup_body(
    workspace_id: UUID,
    state: str,
    reason_code: str | None,
    *,
    operation_id: str = "owned-original",
    target_workspace_id: UUID | None = None,
    result_ref: ProfileRef | None = None,
    result_ref_is_set: bool = False,
) -> dict[str, object]:
    target_ref = ProfileRef(
        workspace_id=target_workspace_id or workspace_id,
        profile_id=UUID("9f3f38a7-507b-43da-a780-2bd6c5798957"),
        revision=2,
    )
    if not result_ref_is_set:
        result_ref = target_ref if state in {"succeeded", "reconciled"} else None
    return ConfigurationResult(
        operationId=operation_id,
        state=state,
        target_ref=target_ref,
        result_ref=result_ref,
        reason_code=reason_code,
    ).model_dump(mode="json", by_alias=True)


def _configuration_lookup_lab(workspace_id: UUID, status_code: int, body: object):
    headers = {"x-n20-lookup-test": "original-operation"}

    class RecordingClient:
        def __init__(self) -> None:
            self.get_calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
            self.write_calls: list[str] = []

        def get(self, path: str, *, params, headers):
            self.get_calls.append((path, params, headers))
            return httpx.Response(status_code=status_code, json=body)

        def _reject_write(self, method: str) -> None:
            self.write_calls.append(method)
            raise AssertionError("unexpected_configuration_write")

        def post(self, *_args, **_kwargs):
            self._reject_write("POST")

        def put(self, *_args, **_kwargs):
            self._reject_write("PUT")

        def patch(self, *_args, **_kwargs):
            self._reject_write("PATCH")

        def delete(self, *_args, **_kwargs):
            self._reject_write("DELETE")

    client = RecordingClient()
    lab = SimpleNamespace(workspace_id=workspace_id, headers=headers, client=client)
    return lab, client, headers


def _assert_configuration_lookup_only(
    client, headers: dict[str, str], workspace_id: UUID
) -> None:
    assert client.get_calls == [
        (
            f"/product/v1/workspaces/{workspace_id}/configuration-requests",
            {"operationId": "owned-original"},
            headers,
        )
    ]
    assert client.write_calls == []


@pytest.mark.parametrize(
    ("state", "reason_code", "status_code"),
    (
        ("succeeded", None, 200),
        ("reconciled", None, 200),
        ("claimed", None, 202),
        ("dispatching", None, 202),
        ("unknown", None, 202),
        ("rejected", "revision_conflict", 409),
        ("rejected", "configuration_invalid", 422),
        ("rejected", "permission_denied", 403),
        ("rejected", "not_found", 404),
    ),
)
def test_cold_configuration_lookup_decodes_original_result(
    state, reason_code, status_code, no_database_connections
) -> None:
    workspace_id = UUID("4f2ddbc6-2af3-4c57-9a5d-42f859a5dfac")
    expected = _configuration_lookup_body(workspace_id, state, reason_code)
    lab, client, headers = _configuration_lookup_lab(
        workspace_id, status_code, expected
    )

    result = _read_configuration_request(lab, "owned-original")

    assert result == expected
    _assert_configuration_lookup_only(client, headers, workspace_id)


def test_cold_configuration_lookup_absent_is_exact_not_found(
    no_database_connections,
) -> None:
    workspace_id = UUID("4f2ddbc6-2af3-4c57-9a5d-42f859a5dfac")
    lab, client, headers = _configuration_lookup_lab(
        workspace_id,
        404,
        {"detail": {"code": "configuration_request_not_found"}},
    )

    result = _read_configuration_request(lab, "owned-original")

    assert result is None
    _assert_configuration_lookup_only(client, headers, workspace_id)


@pytest.mark.parametrize(
    ("case", "expected_error"),
    (
        ("mismatched_operation", "cold_configuration_lookup_result_unmatched"),
        ("foreign_target_workspace", "cold_configuration_lookup_result_unmatched"),
        ("foreign_result_ref", "cold_configuration_lookup_result_unmatched"),
        ("detail_only_409", "cold_configuration_lookup_result_invalid"),
        ("authorization_401", "cold_configuration_lookup_result_invalid"),
        ("succeeded_409", "cold_configuration_lookup_status_unmatched"),
        ("unknown_200", "cold_configuration_lookup_status_unmatched"),
        ("unexpected_500", "cold_configuration_lookup_status_unmatched"),
        ("succeeded_null_result", "cold_configuration_lookup_result_unmatched"),
        ("different_404_detail", "cold_configuration_lookup_result_invalid"),
    ),
)
def test_cold_configuration_lookup_refuses_unmatched_wire_result(
    case, expected_error, no_database_connections
) -> None:
    workspace_id = UUID("4f2ddbc6-2af3-4c57-9a5d-42f859a5dfac")
    status_code = 409
    if case == "mismatched_operation":
        body = _configuration_lookup_body(
            workspace_id,
            "rejected",
            "revision_conflict",
            operation_id="different-original",
        )
    elif case == "foreign_target_workspace":
        body = _configuration_lookup_body(
            workspace_id,
            "rejected",
            "revision_conflict",
            target_workspace_id=UUID("40d6cd8f-62c1-4590-9e56-40a69379c94b"),
        )
    elif case == "foreign_result_ref":
        foreign_result = ProfileRef(
            workspace_id=workspace_id,
            profile_id=UUID("19baae7e-c527-46d6-a66f-b7ad7af16f05"),
            revision=2,
        )
        status_code = 200
        body = _configuration_lookup_body(
            workspace_id,
            "succeeded",
            None,
            result_ref=foreign_result,
            result_ref_is_set=True,
        )
    elif case == "detail_only_409":
        body = {"detail": {"code": "revision_conflict"}}
    elif case == "authorization_401":
        status_code = 401
        body = {"detail": {"code": "authentication_required"}}
    elif case == "succeeded_409":
        body = _configuration_lookup_body(workspace_id, "succeeded", None)
    elif case == "unknown_200":
        status_code = 200
        body = _configuration_lookup_body(workspace_id, "unknown", None)
    elif case == "unexpected_500":
        status_code = 500
        body = _configuration_lookup_body(workspace_id, "succeeded", None)
    elif case == "succeeded_null_result":
        status_code = 200
        body = _configuration_lookup_body(
            workspace_id,
            "succeeded",
            None,
            result_ref_is_set=True,
        )
    else:
        assert case == "different_404_detail"
        status_code = 404
        body = {"detail": {"code": "not_found"}}
    lab, client, headers = _configuration_lookup_lab(workspace_id, status_code, body)

    with pytest.raises(ProductTargetError, match=expected_error):
        _read_configuration_request(lab, "owned-original")

    _assert_configuration_lookup_only(client, headers, workspace_id)


def test_all_nineteen_cold_selectors_absent_needs_no_evidence_or_guard(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    assert len(SELECTION_KEYS) == 19
    monkeypatch.setattr(lab_module.os, "environ", {})
    monkeypatch.setattr(
        lab_module,
        "management_evidence_directory",
        lambda _: pytest.fail("Absent selection inspected evidence configuration"),
    )
    monkeypatch.setattr(
        lab_module,
        "acquire_guards",
        lambda _: pytest.fail("Absent selection attempted target acquisition"),
    )
    fixture = lab_module.management_cold_lab.__wrapped__(tmp_path)
    with pytest.raises(pytest.skip.Exception, match="NOT_RUN:"):
        next(fixture)


@pytest.mark.parametrize("state", ("succeeded", "reconciled"))
def test_pending_revision_resume_accepts_exact_confirmed_child_result(
    state, no_database_connections
) -> None:
    parent_ref, child_ref, request, result_facts = _revision_result_facts(state)
    assert request.expected_ref == parent_ref
    assert request.target_ref == child_ref
    result = ConfigurationResult.from_request(request)
    assert result.state == state and result_facts["state"] == state
    assert result.target_ref == child_ref and result.result_ref == child_ref
    checked = _validate_pending_revision_result(
        request.operation_id, parent_ref, result_facts
    )
    assert checked == child_ref


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (
        ("different_operation", "cold_profile_revise_operation_mismatch"),
        ("parent_target", "cold_profile_revise_result_mismatch"),
        ("different_profile", "cold_profile_revise_result_mismatch"),
        ("foreign_workspace", "cold_profile_revise_result_mismatch"),
        ("wrong_child_revision", "cold_profile_revise_result_mismatch"),
        ("target_result_mismatch", "cold_profile_revise_result_mismatch"),
        ("missing_result", "cold_profile_revise_result_invalid"),
        ("unknown_state", "cold_profile_revise_result_unconfirmed"),
        ("rejected_state", "cold_profile_revise_result_unconfirmed"),
    ),
)
def test_pending_revision_resume_rejects_unmatched_public_result(
    mutation, error_code, no_database_connections
) -> None:
    parent_ref, child_ref, request, result_facts = _revision_result_facts()
    if mutation == "different_operation":
        result_facts["operationId"] = "n20-different-profile-revision"
    elif mutation == "parent_target":
        result_facts["target_ref"] = parent_ref.model_dump(mode="json")
        result_facts["result_ref"] = parent_ref.model_dump(mode="json")
    elif mutation == "different_profile":
        different_profile = ProfileRef(
            workspace_id=child_ref.workspace_id,
            profile_id=UUID("a397bec3-4bc7-4e20-82f5-63c90554deca"),
            revision=child_ref.revision,
        )
        result_facts["target_ref"] = different_profile.model_dump(mode="json")
        result_facts["result_ref"] = different_profile.model_dump(mode="json")
    elif mutation == "foreign_workspace":
        foreign_workspace_ref = ProfileRef(
            workspace_id=UUID("b8973bda-c3ca-44a8-8d73-543bfcf1372b"),
            profile_id=child_ref.profile_id,
            revision=child_ref.revision,
        )
        result_facts["target_ref"] = foreign_workspace_ref.model_dump(mode="json")
        result_facts["result_ref"] = foreign_workspace_ref.model_dump(mode="json")
    elif mutation == "wrong_child_revision":
        wrong_revision = ProfileRef(
            workspace_id=child_ref.workspace_id,
            profile_id=child_ref.profile_id,
            revision=3,
        )
        result_facts["target_ref"] = wrong_revision.model_dump(mode="json")
        result_facts["result_ref"] = wrong_revision.model_dump(mode="json")
    elif mutation == "target_result_mismatch":
        result_facts["result_ref"] = parent_ref.model_dump(mode="json")
    elif mutation == "missing_result":
        result_facts["result_ref"] = None
    elif mutation == "unknown_state":
        result_facts["state"] = "unknown"
    elif mutation == "rejected_state":
        result_facts["state"] = "rejected"

    with pytest.raises(ProductTargetError, match=error_code):
        _validate_pending_revision_result(
            request.operation_id, parent_ref, result_facts
        )


def test_partial_cold_selection_refuses_before_evidence_or_guard(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    monkeypatch.setattr(
        lab_module.os, "environ", {"SIDERA_PRODUCT_TEST_ALLOCATION_ID": "partial"}
    )
    monkeypatch.setattr(
        lab_module,
        "management_evidence_directory",
        lambda _: pytest.fail("Partial selection inspected evidence configuration"),
    )
    monkeypatch.setattr(
        lab_module,
        "acquire_guards",
        lambda _: pytest.fail("Partial selection attempted target acquisition"),
    )
    with pytest.raises(ProductTargetError, match="product_selection_required"):
        next(lab_module.management_cold_lab.__wrapped__(tmp_path))


def test_full_selection_requires_private_evidence_before_guard_acquisition(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    environ = _selection_environ(tmp_path, include_evidence=False)
    monkeypatch.setattr(lab_module.os, "environ", environ)
    monkeypatch.setattr(
        lab_module,
        "acquire_guards",
        lambda _: pytest.fail("Missing evidence attempted target acquisition"),
    )
    with pytest.raises(ProductTargetError, match="product_evidence_directory_required"):
        next(lab_module.management_cold_lab.__wrapped__(tmp_path))


def test_full_selection_validates_private_evidence_without_opening_target(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    monkeypatch.setattr(lab_module.os, "environ", {})
    targets, evidence = lab_module.select_management_cold_targets(
        _selection_environ(tmp_path, include_evidence=True)
    )
    assert targets is not None and len(targets) == 4
    assert evidence == tmp_path / "private-evidence"


def test_fresh_selection_validates_private_manifest_and_exact_four_targets(
    tmp_path, no_database_connections
) -> None:
    admission = desktop_module.select_management_desktop_targets(
        _fresh_desktop_environ(tmp_path)
    )
    assert admission is not None
    assert admission.allocation_id == (
        "sidera-native-harness:product:n17:20261006:fresh9b"
    )
    assert tuple(target.component for target in admission.targets) == COMPONENTS
    assert admission.evidence_directory == tmp_path / "fresh-evidence"
    assert admission.manifest["phase"] == "fresh9B"
    assert admission.manifest["custody"] == "implementation"
    assert admission.manifest["empty_at_transfer"] is True
    assert tuple(item["component"] for item in admission.manifest["targets"]) == (
        "product",
        "agents",
        "cp",
        "credentials",
    )


def test_fresh_all_nineteen_absent_skips_before_evidence_manifest_or_guard(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    assert desktop_module.select_management_desktop_targets({}) is None
    monkeypatch.setattr(desktop_module.os, "environ", {})
    monkeypatch.setattr(
        desktop_module,
        "management_evidence_directory",
        lambda _: pytest.fail("Absent fresh selection inspected evidence"),
    )
    monkeypatch.setattr(
        desktop_module,
        "read_fresh_manifest",
        lambda *_: pytest.fail("Absent fresh selection inspected the manifest"),
    )
    monkeypatch.setattr(
        desktop_module,
        "acquire_guards",
        lambda _: pytest.fail("Absent fresh selection acquired a target"),
    )
    fixture = desktop_module.management_desktop_lab.__wrapped__(tmp_path)
    with pytest.raises(pytest.skip.Exception, match="NOT_RUN:"):
        next(fixture)


def test_fresh_partial_selection_refuses_before_evidence_manifest_or_guard(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    environ = {"SIDERA_PRODUCT_TEST_ALLOCATION_ID": "partial"}
    monkeypatch.setattr(desktop_module.os, "environ", environ)
    monkeypatch.setattr(
        desktop_module,
        "management_evidence_directory",
        lambda _: pytest.fail("Partial fresh selection inspected evidence"),
    )
    monkeypatch.setattr(
        desktop_module,
        "_manifest_path",
        lambda _: pytest.fail("Partial fresh selection inspected manifest path"),
    )
    monkeypatch.setattr(
        desktop_module,
        "acquire_guards",
        lambda _: pytest.fail("Partial fresh selection acquired a target"),
    )
    with pytest.raises(ProductTargetError, match="product_selection_required"):
        next(desktop_module.management_desktop_lab.__wrapped__(tmp_path))


def test_fresh_full_selection_requires_evidence_before_manifest_or_guard(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    environ = _fresh_desktop_environ(tmp_path, include_evidence=False)
    monkeypatch.setattr(desktop_module.os, "environ", environ)
    monkeypatch.setattr(
        desktop_module,
        "_manifest_path",
        lambda _: pytest.fail("Missing evidence inspected manifest path"),
    )
    monkeypatch.setattr(
        desktop_module,
        "acquire_guards",
        lambda _: pytest.fail("Missing evidence acquired a target"),
    )
    with pytest.raises(ProductTargetError, match="product_evidence_directory_required"):
        next(desktop_module.management_desktop_lab.__wrapped__(tmp_path))


def test_fresh_missing_manifest_refuses_before_guard_or_runtime_start(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    environ = _fresh_desktop_environ(tmp_path, include_manifest=False)
    monkeypatch.setattr(desktop_module.os, "environ", environ)
    monkeypatch.setattr(
        desktop_module,
        "acquire_guards",
        lambda _: pytest.fail("Missing fresh manifest acquired a target"),
    )
    monkeypatch.setattr(
        desktop_module,
        "start_owned_backend",
        lambda *_: pytest.fail("Missing fresh manifest started the backend"),
    )
    with pytest.raises(ProductTargetError, match="fresh_manifest_required"):
        next(desktop_module.management_desktop_lab.__wrapped__(tmp_path))


def test_fresh_manifest_parent_must_be_private_before_guard(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    environ = _fresh_desktop_environ(tmp_path)
    manifest_path = Path(environ["SIDERA_PRODUCT_TEST_FRESH_MANIFEST"])
    manifest_path.parent.chmod(0o755)
    monkeypatch.setattr(desktop_module.os, "environ", environ)
    monkeypatch.setattr(
        desktop_module,
        "acquire_guards",
        lambda _: pytest.fail("Non-private manifest parent acquired a target"),
    )
    with pytest.raises(ProductTargetError, match="fresh_manifest_refused"):
        next(desktop_module.management_desktop_lab.__wrapped__(tmp_path))


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (
        ("foreign_allocation", "fresh_manifest_refused"),
        ("wrong_phase", "fresh_manifest_refused"),
        ("admin_field", "fresh_manifest_refused"),
        ("nonempty_transfer", "fresh_manifest_refused"),
        ("target_owner", "fresh_manifest_refused"),
        ("target_port_type", "fresh_manifest_refused"),
    ),
)
def test_fresh_manifest_mismatches_refuse_before_guard_or_runtime_start(
    tmp_path, no_database_connections, monkeypatch, mutation, error_code
) -> None:
    environ = _fresh_desktop_environ(tmp_path)
    manifest_path = Path(environ["SIDERA_PRODUCT_TEST_FRESH_MANIFEST"])
    manifest = inventory_module.read_private_json(manifest_path)
    if mutation == "foreign_allocation":
        manifest["allocation_id"] = "sidera-native-harness:product:n17:20261007:other"
    elif mutation == "wrong_phase":
        manifest["phase"] = "cold9A"
    elif mutation == "admin_field":
        manifest["admin_authority"] = True
    elif mutation == "nonempty_transfer":
        manifest["empty_at_transfer"] = False
    elif mutation == "target_owner":
        manifest["targets"][2]["owner"] = "foreign_owner"
    elif mutation == "target_port_type":
        manifest["targets"][3]["port"] = True
    manifest_path.unlink()
    inventory_module.write_private_json(manifest_path, manifest)

    monkeypatch.setattr(desktop_module.os, "environ", environ)
    monkeypatch.setattr(
        desktop_module,
        "acquire_guards",
        lambda _: pytest.fail("Rejected fresh manifest acquired a target"),
    )
    monkeypatch.setattr(
        desktop_module,
        "start_owned_backend",
        lambda *_: pytest.fail("Rejected fresh manifest started the backend"),
    )
    fixture = desktop_module.management_desktop_lab.__wrapped__(tmp_path)
    with pytest.raises(ProductTargetError, match=error_code):
        next(fixture)


def test_valid_fresh_manifest_is_checked_before_guard_acquisition(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    environ = _fresh_desktop_environ(tmp_path)
    monkeypatch.setattr(desktop_module.os, "environ", environ)
    reached: list[tuple[str, ...]] = []

    class GuardBoundaryReached(Exception):
        pass

    def observe_guard_boundary(targets):
        reached.append(tuple(target.component for target in targets))
        raise GuardBoundaryReached

    monkeypatch.setattr(desktop_module, "acquire_guards", observe_guard_boundary)
    fixture = desktop_module.management_desktop_lab.__wrapped__(tmp_path)
    with pytest.raises(GuardBoundaryReached):
        next(fixture)
    assert reached == [COMPONENTS]


def test_cold_lab_constructs_public_runtime_without_entering_lifespan(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    from sidera_product.composition import ProductRuntime

    starts = []

    def reject_start(self) -> None:
        starts.append(self)
        pytest.fail("Cold lab called ProductRuntime.start")

    monkeypatch.setattr(ProductRuntime, "start", reject_start)
    targets = tuple(
        _url_target(tmp_path, component, index)
        for index, component in enumerate(COMPONENTS, 1)
    )
    evidence = tmp_path / "inert-evidence"
    evidence.mkdir(mode=0o700)
    lab = lab_module.build_management_cold_lab(
        targets,
        [],
        tmp_path,
        evidence,
        workspace_id=UUID("b9e469c3-ad4a-5bcd-a2b9-75f9c45e93fc"),
    )
    try:
        sentinel = "n20-header-repr-sentinel-6d82d1"
        lab.headers["inert-test-header"] = sentinel
        sentinel_is_visible = sentinel in repr(lab)
        assert sentinel_is_visible is False
        response = lab.client.get("/product/v1/status")
        assert response.status_code == 200
        assert response.json()["lifecycle"] == "constructed"
        assert response.json()["execution"]["ready"] is False
        assert starts == []
    finally:
        assert lab.close() is True


def test_row_inventory_hashes_complete_browser_session_rows(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    class FakeResult:
        def __init__(self, row):
            self.row = row

        def mappings(self):
            return (self.row,)

    class FakeConnection:
        dialect = SimpleNamespace(
            identifier_preparer=SimpleNamespace(quote=lambda value: f'"{value}"')
        )

        def __init__(self, row):
            self.row = row
            self.statements = []

        def rollback(self):
            return None

        def execute(self, statement):
            self.statements.append(str(statement))
            return FakeResult(self.row)

    target = _url_target(tmp_path, "product", 1)
    first_row = {
        "identity": '["session-id"]',
        "row_json": '{"session_id":"session-id","csrf_digest":"first"}',
    }
    second_row = {
        "identity": '["session-id"]',
        "row_json": '{"session_id":"session-id","csrf_digest":"second"}',
    }
    connection = FakeConnection(first_row)
    guard = TargetGuard(target, None, connection, {}, active=True)
    monkeypatch.setattr(
        inventory_module,
        "verify_objects",
        lambda *_: {
            "relations": [
                {"kind": "r", "schema": "product", "name": "browser_sessions"}
            ]
        },
    )
    monkeypatch.setattr(
        inventory_module,
        "sqlalchemy_inspect",
        lambda _: SimpleNamespace(
            get_pk_constraint=lambda *_args, **_kwargs: {
                "constrained_columns": ["session_id"]
            }
        ),
    )
    before = inventory_module.capture_rows(guard)
    statement = connection.statements[-1]
    assert "to_jsonb(original)" in statement
    assert "csrf_digest" not in statement
    connection.row = second_row
    after = inventory_module.capture_rows(guard)
    changed = inventory_module.compare_rows(before, after)
    assert changed["preserved"] is False
    assert changed["changed"] == [
        {"table": "product.browser_sessions", "identity": '["session-id"]'}
    ]
    assert "first" not in repr(changed) and "second" not in repr(changed)


def test_original_baseline_is_persisted_once_and_reused(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    evidence = tmp_path / "private-evidence"
    evidence.mkdir(mode=0o700)
    guards = tuple(
        TargetGuard(
            _url_target(tmp_path, component, index), None, None, {}, active=True
        )
        for index, component in enumerate(COMPONENTS, 1)
    )
    expected_digests = {
        "product": "1" * 64,
        "agents": "2" * 64,
        "cp": "3" * 64,
        "credentials": "4" * 64,
    }
    captures = []

    def capture(guard):
        component = guard.target.component
        captures.append(component)
        return inventory_module.RowInventory(
            {f"{component}.items": {'["original"]': expected_digests[component]}}
        )

    monkeypatch.setattr(inventory_module, "capture_rows", capture)
    first = inventory_module.load_or_capture_original_baseline(
        evidence, "allocation-one", guards
    )
    assert captures == list(COMPONENTS)
    saved = inventory_module.read_private_json(first.path)
    assert saved["schema_version"] == 1
    assert saved["allocation_id"] == "allocation-one"
    assert saved["rows"]["product"]["product.items"]['["original"]'] == "1" * 64
    assert "row_body" not in repr(saved)

    monkeypatch.setattr(
        inventory_module,
        "capture_rows",
        lambda _: pytest.fail("Recorded baseline was recaptured on resume"),
    )
    resumed = inventory_module.load_or_capture_original_baseline(
        evidence, "allocation-one", guards
    )
    assert resumed.path == first.path
    assert resumed.rows == first.rows
    assert captures == list(COMPONENTS)


def test_original_baseline_refuses_allocation_or_target_mismatch(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    evidence = tmp_path / "private-evidence"
    evidence.mkdir(mode=0o700)
    guards = tuple(
        TargetGuard(
            _url_target(tmp_path, component, index), None, None, {}, active=True
        )
        for index, component in enumerate(COMPONENTS, 1)
    )
    monkeypatch.setattr(
        inventory_module,
        "capture_rows",
        lambda guard: inventory_module.RowInventory(
            {f"{guard.target.component}.items": {'["original"]': "a" * 64}}
        ),
    )
    inventory_module.load_or_capture_original_baseline(
        evidence, "allocation-one", guards
    )
    monkeypatch.setattr(
        inventory_module,
        "capture_rows",
        lambda _: pytest.fail("Mismatched baseline attempted a new capture"),
    )

    with pytest.raises(ProductTargetError, match="cold_original_baseline_refused"):
        inventory_module.load_or_capture_original_baseline(
            evidence, "allocation-two", guards
        )

    changed_target = replace(guards[0].target, marker="different-marker")
    changed_guards = (
        TargetGuard(changed_target, None, None, {}, active=True),
        *guards[1:],
    )
    with pytest.raises(ProductTargetError, match="cold_original_baseline_refused"):
        inventory_module.load_or_capture_original_baseline(
            evidence, "allocation-one", changed_guards
        )


def test_profile_work_stage_record_resumes_final_reads_without_mutations(
    tmp_path,
) -> None:
    evidence = tmp_path / "private-evidence"
    evidence.mkdir(mode=0o700)
    workspace_id = UUID("efcc826a-3cf2-4a64-90b1-a2df18546a42")
    ref_r1 = {"profile_id": "profile-one", "revision": 1}
    ref_r2 = {"profile_id": "profile-one", "revision": 2}
    records = (
        (
            "profile_create",
            {"operation_id": "n20-create", "draft_sha256": "1" * 64},
            {"profile_ref": ref_r1},
        ),
        (
            "work_seed",
            {"profile_ref": ref_r1},
            {"task_id": "task-one", "execution_id": "execution-one"},
        ),
        (
            "profile_revise",
            {
                "operation_id": "n20-revise",
                "expected_ref": ref_r1,
                "draft_sha256": "2" * 64,
            },
            {"expected_ref": ref_r1, "result_ref": ref_r2},
        ),
        (
            "status_cas",
            {"expected_version": 4, "disable": False, "restore": True},
            {"disable_version": 5, "stale_status": 409, "restore_version": 6},
        ),
        (
            "defaults_cas",
            {
                "expected_version": 2,
                "main_profile_id": "profile-one",
                "planner_profile_id": "profile-one",
                "worker_profile_id": "profile-one",
            },
            {
                "version": 3,
                "main_profile_id": "profile-one",
                "planner_profile_id": "profile-one",
                "worker_profile_id": "profile-one",
            },
        ),
        ("final_reads", {"profile_ref": ref_r2}, {"asserted": True}),
    )
    mutations = []
    public_reads = []
    for stage, intent, result in records:
        started, saved_result = lab_module.begin_cold_profile_stage(
            evidence, workspace_id, stage, intent
        )
        assert started is True and saved_result is None
        if stage != "final_reads":
            mutations.append(stage)
        lab_module.complete_cold_profile_stage(
            evidence, workspace_id, stage, intent, result
        )

    saved_intent, saved_result = lab_module.load_cold_profile_stage(
        evidence, workspace_id, "status_cas"
    )
    assert saved_intent == records[3][1]
    assert saved_result == records[3][2]

    final_read_attempts = 0

    def final_read() -> None:
        nonlocal final_read_attempts
        final_read_attempts += 1
        if final_read_attempts == 1:
            raise RuntimeError("simulated_final_read_failure")

    with pytest.raises(RuntimeError, match="simulated_final_read_failure"):
        final_read()
    mutations_before_resume = tuple(mutations)

    for stage, intent, result in records:
        started, saved_result = lab_module.begin_cold_profile_stage(
            evidence, workspace_id, stage, intent
        )
        assert started is False and saved_result == result
        public_reads.append(stage)
    final_read()

    assert tuple(mutations) == mutations_before_resume
    assert len(public_reads) == len(records)
    assert final_read_attempts == 2
    assert records[0][2]["profile_ref"] == ref_r1
    assert records[2][2] == {"expected_ref": ref_r1, "result_ref": ref_r2}
    assert records[3][2]["stale_status"] == 409
    assert records[4][2]["version"] == 3

    with pytest.raises(ProductTargetError, match="cold_profile_stage_refused"):
        lab_module.begin_cold_profile_stage(
            evidence,
            workspace_id,
            "profile_create",
            {"operation_id": "different-intent", "draft_sha256": "1" * 64},
        )


def test_work_pin_first_seed_and_recorded_resume_are_transaction_owned(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    rows = {}
    calls = {"begins": 0, "gets": 0, "adds": [], "flushes": 0, "disposals": 0}

    class FakeTransaction:
        def __init__(self, session):
            self.session = session

        def __enter__(self):
            assert self.session.active is False
            self.session.active = True

        def __exit__(self, *_args):
            self.session.active = False

    class FakeSession:
        def __init__(self, _engine):
            self.active = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            assert self.active is False

        def begin(self):
            calls["begins"] += 1
            return FakeTransaction(self)

        def get(self, model, identity):
            assert self.active is True
            calls["gets"] += 1
            return rows.get((model, identity))

        def scalar(self, _statement):
            assert self.active is True
            return None

        def add(self, row):
            assert self.active is True
            calls["adds"].append(type(row))
            if type(row).__name__ == "AgentBindingModel":
                identity = (
                    row.workspace_id,
                    row.target_type,
                    row.target_id,
                    row.version,
                )
            elif type(row).__name__ == "AgentExecutionLinkModel":
                identity = (row.workspace_id, row.execution_id)
            else:
                identity = row.id
            rows[(type(row), identity)] = row

        def flush(self):
            assert self.active is True
            calls["flushes"] += 1

    class FakeEngine:
        def dispose(self):
            calls["disposals"] += 1

    monkeypatch.setattr(work_module, "Session", FakeSession)
    monkeypatch.setattr(
        work_module, "create_engine", lambda *_args, **_kwargs: FakeEngine()
    )
    target = _url_target(tmp_path, "cp", 3)
    guard = TargetGuard(target, None, None, {}, active=True)
    workspace_id = uuid4()
    profile_ref = ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1)
    first = work_module.write_work_pin(
        guard,
        workspace_id=workspace_id,
        principal_id="n20-agent-management",
        profile_ref=profile_ref,
        evidence_directory=tmp_path / "evidence",
    )
    assert [kind.__name__ for kind in calls["adds"]] == [
        "WorkspaceModel",
        "TaskModel",
        "AgentBindingModel",
        "ExecutionModel",
        "ExecutionAuthorityModel",
        "AgentExecutionLinkModel",
    ]
    execution = rows[(work_module.ExecutionModel, first.ids.execution_id)]
    task = rows[(work_module.TaskModel, first.ids.task_id)]
    link = rows[
        (work_module.AgentExecutionLinkModel, (workspace_id, first.ids.execution_id))
    ]
    assert task.status == "pending"
    assert execution.status == "queued"
    assert execution.adapter_preferences == [] and execution.adapter_forbidden == []
    assert link.status == "pending" and link.run_id is None

    second = work_module.write_work_pin(
        guard,
        workspace_id=workspace_id,
        principal_id="n20-agent-management",
        profile_ref=profile_ref,
        evidence_directory=tmp_path / "evidence",
    )
    assert second.ids == first.ids
    assert calls["begins"] == 2
    assert calls["flushes"] == 6
    assert calls["disposals"] == 2
    assert len(calls["adds"]) == 6
    assert calls["gets"] > 0


def test_owned_backend_close_keeps_error_history_but_proves_resource_close(
    no_database_connections,
) -> None:
    class FakeThread:
        def join(self, timeout=None):
            self.timeout = timeout

        def is_alive(self):
            return False

    class FakeListener:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def fileno(self):
            return -1 if self.closed else 10

    listener = FakeListener()
    historical_error = RuntimeError("prior_failure")
    owner = desktop_module.OwnedUvicorn(
        SimpleNamespace(should_exit=False),
        listener,
        FakeThread(),
        [historical_error],
    )

    assert owner.close() is True
    assert listener.closed is True
    assert owner.errors == [historical_error]


@pytest.mark.parametrize(
    ("thread_alive", "listener_fails"), ((True, False), (False, True))
)
def test_owned_backend_close_refuses_live_thread_or_failed_listener(
    no_database_connections, thread_alive, listener_fails
) -> None:
    class FakeThread:
        def join(self, timeout=None):
            self.timeout = timeout

        def is_alive(self):
            return thread_alive

    class FakeListener:
        closed = False

        def close(self):
            if listener_fails:
                raise OSError("close failed")
            self.closed = True

        def fileno(self):
            return -1 if self.closed else 10

    owner = desktop_module.OwnedUvicorn(
        SimpleNamespace(should_exit=False),
        FakeListener(),
        FakeThread(),
        [],
    )

    assert owner.close() is False


def test_desktop_runtime_close_retry_does_not_erase_functional_failure(
    no_database_connections,
) -> None:
    class FakeThread:
        def join(self, timeout=None):
            self.timeout = timeout

        def is_alive(self):
            return False

    class FakeListener:
        def __init__(self):
            self.close_count = 0

        def close(self):
            self.close_count += 1

        def fileno(self):
            return -1 if self.close_count else 10

    class FakeRuntime:
        def __init__(self):
            self.results = iter((False, True))
            self.calls = 0

        def close(self, *, grace_seconds):
            self.calls += 1
            assert grace_seconds == 10
            return next(self.results)

    class FakeVite:
        def poll(self):
            return 0

    guard = SimpleNamespace(active=True, release=lambda: pytest.fail("released"))
    native = SimpleNamespace(start_count=0, calls=[])
    listener = FakeListener()
    backend = desktop_module.OwnedUvicorn(
        SimpleNamespace(should_exit=False),
        listener,
        FakeThread(),
        [RuntimeError("prior_failure")],
    )
    runtime = FakeRuntime()
    lab = desktop_module.FreshDesktopLab(
        admission=None,
        guards=[guard],
        settings=None,
        runtime=runtime,
        native=native,
        principal=None,
        secrets=None,
        app=None,
        backend=backend,
        vite=FakeVite(),
        bootstrap_file=Path("/private/bootstrap.json"),
        vite_root=Path("/private/web"),
    )

    assert lab.close() is False
    assert lab.close() is True
    assert runtime.calls == 2
    assert listener.close_count == 2
    assert backend.errors == [backend.errors[0]]
    assert native.start_count == 0
    assert guard.active is True


def test_failed_backend_start_retains_original_owner_for_close_retry(
    no_database_connections, monkeypatch
) -> None:
    class FakeListener:
        def __init__(self):
            self.close_calls = 0

        def getsockname(self):
            return ("127.0.0.1", 55439)

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("inert close refusal")

        def fileno(self):
            return -1 if self.close_calls > 1 else 17

    class FakeThread:
        def __init__(self, **kwargs):
            self.target = kwargs["target"]
            self.start_calls = 0
            self.joined = False

        def start(self):
            self.start_calls += 1

        def join(self, timeout=None):
            self.joined = True

        def is_alive(self):
            return not self.joined

    server = SimpleNamespace(should_exit=False, started=False)
    listener = FakeListener()
    lab = SimpleNamespace(backend=None)
    clock = iter((0.0, 31.0))
    monkeypatch.setattr(
        desktop_module.uvicorn, "Config", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(desktop_module.uvicorn, "Server", lambda config: server)
    monkeypatch.setattr(desktop_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(desktop_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(desktop_module.time, "sleep", lambda _: None)

    with pytest.raises(ProductTargetError, match="fresh_backend_start_refused"):
        desktop_module.start_owned_backend(
            object(),
            SimpleNamespace(bind_host="127.0.0.1", port=55439),
            listener=listener,
            resource_owner=lab,
        )

    original = lab.backend
    assert isinstance(original, desktop_module.OwnedUvicorn)
    assert original.listener is listener
    assert original.thread.start_calls == 1
    assert original.close() is True
    assert lab.backend is original
    assert original.thread.start_calls == 1
    assert listener.close_calls == 2


def test_failed_vite_start_retains_original_process_for_close_retry(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    class FakeProcess:
        def poll(self):
            return None

    process = FakeProcess()
    popen_calls = []
    stop_results = iter((False, True))
    lab = SimpleNamespace(vite=None)
    clock = iter((0.0, 31.0))
    monkeypatch.setattr(
        desktop_module,
        "_vite_environment",
        lambda _tmp_path, _web_root: ({}, "inert-node", "inert-vite.js"),
    )
    monkeypatch.setattr(
        desktop_module.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append(process) or process,
    )
    monkeypatch.setattr(
        desktop_module, "_stop_owned_process", lambda candidate: next(stop_results)
    )
    monkeypatch.setattr(desktop_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(desktop_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        desktop_module,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(desktop_module.URLError("inert")),
    )

    with pytest.raises(ProductTargetError, match="fresh_vite_start_refused"):
        desktop_module.start_owned_vite(
            tmp_path=tmp_path,
            web_root=tmp_path,
            backend_url="http://127.0.0.1:55439",
            port=55440,
            resource_owner=lab,
        )

    assert lab.vite is process
    assert popen_calls == [process]
    assert desktop_module._stop_owned_process(lab.vite) is True
    assert lab.vite is process
    assert popen_calls == [process]


def test_owned_vite_starts_installed_cli_without_pnpm(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    node_path = tmp_path / "node24" / "bin" / "node"
    web_root = tmp_path / "web"
    vite_script = web_root / "node_modules" / "vite" / "bin" / "vite.js"
    vite_script.parent.mkdir(parents=True)
    vite_script.write_text("// inert installed Vite entry point\n")
    which_calls = []
    run_calls = []
    popen_calls = []
    parent_secret = "synthetic-provider-secret"
    monkeypatch.setenv("OPENAI_API_KEY", parent_secret)

    def fake_which(name):
        which_calls.append(name)
        return str(node_path) if name == "node" else None

    def fake_run(args, **kwargs):
        run_calls.append((args, {**kwargs, "env": dict(kwargs["env"])}))
        return desktop_module.subprocess.CompletedProcess(
            args, 0, stdout="v24.19.0\n", stderr=""
        )

    class FakeProcess:
        def poll(self):
            return None

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    process = FakeProcess()
    lab = SimpleNamespace(vite=None)
    monkeypatch.setattr(desktop_module.shutil, "which", fake_which)
    monkeypatch.setattr(desktop_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        desktop_module.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)) or process,
    )
    monkeypatch.setattr(
        desktop_module, "urlopen", lambda *_args, **_kwargs: FakeResponse()
    )
    clock = iter((0.0, 1.0))
    monkeypatch.setattr(desktop_module.time, "monotonic", lambda: next(clock))

    returned = desktop_module.start_owned_vite(
        tmp_path=tmp_path,
        web_root=web_root,
        backend_url="http://127.0.0.1:55439",
        port=55440,
        resource_owner=lab,
    )

    assert returned is process
    assert lab.vite is process
    assert which_calls == ["node"]
    assert len(run_calls) == 1
    probe_args, probe_kwargs = run_calls[0]
    assert probe_args == [str(node_path), "--version"]
    expected_environment = {
        "HOME": str(tmp_path / "vite-home"),
        "LANG": desktop_module.os.environ.get("LANG", "C.UTF-8"),
        "PATH": desktop_module.os.pathsep.join(
            dict.fromkeys((str(node_path.parent), "/usr/bin", "/bin"))
        ),
        "TMPDIR": str(tmp_path),
    }
    assert probe_kwargs == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 5,
        "env": expected_environment,
    }
    assert parent_secret not in probe_kwargs["env"].values()
    assert len(popen_calls) == 1
    popen_args, popen_kwargs = popen_calls[0]
    assert popen_args[0] == [
        str(node_path),
        str(vite_script),
        "--host",
        "::1",
        "--port",
        "55440",
        "--strictPort",
    ]
    assert popen_kwargs["cwd"] == web_root
    assert popen_kwargs["env"] == {
        **expected_environment,
        "SIDERA_WEB_BACKEND": "native",
        "SIDERA_WEB_PRODUCT_URL": "http://127.0.0.1:55439",
    }
    assert parent_secret not in popen_kwargs["env"].values()
    assert popen_kwargs["stdin"] is desktop_module.subprocess.DEVNULL
    assert popen_kwargs["stdout"] is desktop_module.subprocess.DEVNULL
    assert popen_kwargs["stderr"] is desktop_module.subprocess.DEVNULL
    assert popen_kwargs["start_new_session"] is True


def test_owned_vite_refuses_missing_installed_cli_before_popen(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    node_path = tmp_path / "node24" / "bin" / "node"
    web_root = tmp_path / "web"
    web_root.mkdir()
    which_calls = []
    run_calls = []
    popen_calls = []
    lab = SimpleNamespace(vite=None)

    def refuse_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        raise AssertionError("version probe ran before installed Vite validation")

    def refuse_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("Popen ran without an installed Vite entry point")

    monkeypatch.setattr(
        desktop_module.shutil,
        "which",
        lambda name: which_calls.append(name) or str(node_path),
    )
    monkeypatch.setattr(desktop_module.subprocess, "run", refuse_run)
    monkeypatch.setattr(desktop_module.subprocess, "Popen", refuse_popen)

    with pytest.raises(ProductTargetError, match="fresh_vite_toolchain_unavailable"):
        desktop_module.start_owned_vite(
            tmp_path=tmp_path,
            web_root=web_root,
            backend_url="http://127.0.0.1:55439",
            port=55440,
            resource_owner=lab,
        )

    assert which_calls == ["node"]
    assert run_calls == []
    assert popen_calls == []
    assert lab.vite is None


@pytest.mark.parametrize(
    ("node_available", "node_version", "expected_error"),
    [
        (False, "v24.19.0\n", "fresh_vite_toolchain_unavailable"),
        (True, "v22.1.0\n", "fresh_node24_required"),
    ],
)
def test_vite_environment_requires_node24(
    tmp_path,
    no_database_connections,
    monkeypatch,
    node_available,
    node_version,
    expected_error,
) -> None:
    node_path = tmp_path / "node24" / "bin" / "node"
    web_root = tmp_path / "web"
    vite_script = web_root / "node_modules" / "vite" / "bin" / "vite.js"
    if node_available:
        vite_script.parent.mkdir(parents=True)
        vite_script.write_text("// inert installed Vite entry point\n")
    which_calls = []
    run_calls = []

    def fake_which(name):
        which_calls.append(name)
        return str(node_path) if node_available else None

    def fake_run(args, **kwargs):
        run_calls.append((args, kwargs))
        return desktop_module.subprocess.CompletedProcess(
            args, 0, stdout=node_version, stderr=""
        )

    monkeypatch.setattr(desktop_module.shutil, "which", fake_which)
    monkeypatch.setattr(desktop_module.subprocess, "run", fake_run)

    with pytest.raises(ProductTargetError, match=expected_error):
        desktop_module._vite_environment(tmp_path, web_root)

    assert which_calls == ["node"]
    assert len(run_calls) == int(node_available)


def test_vite_environment_probe_timeout_is_refused(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    node_path = tmp_path / "node24" / "bin" / "node"
    web_root = tmp_path / "web"
    vite_script = web_root / "node_modules" / "vite" / "bin" / "vite.js"
    vite_script.parent.mkdir(parents=True)
    vite_script.write_text("// inert installed Vite entry point\n")
    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append((args, kwargs))
        raise desktop_module.subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(desktop_module.shutil, "which", lambda _name: str(node_path))
    monkeypatch.setattr(desktop_module.subprocess, "run", fake_run)

    with pytest.raises(ProductTargetError, match="fresh_vite_toolchain_unavailable"):
        desktop_module._vite_environment(tmp_path, web_root)

    assert len(run_calls) == 1
    assert run_calls[0][1]["timeout"] == 5


def test_reserved_listener_close_failure_prevents_guard_release(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    class FakeGuard:
        def __init__(self):
            self.release_calls = 0

        def release(self):
            self.release_calls += 1
            return True

    class FakeListener:
        def __init__(self):
            self.close_calls = 0

        def fileno(self):
            return 23

        def close(self):
            self.close_calls += 1
            raise OSError("inert close refusal")

    environ = _fresh_desktop_environ(tmp_path)
    guard = FakeGuard()
    listener = FakeListener()
    monkeypatch.setattr(desktop_module.os, "environ", environ)
    monkeypatch.setattr(desktop_module, "acquire_guards", lambda targets: [guard])
    monkeypatch.setattr(
        desktop_module,
        "validate_fresh_empty_targets",
        lambda guards: {component: {} for component in COMPONENTS},
    )
    monkeypatch.setattr(
        desktop_module, "_bind_listener", lambda host: (listener, 55439)
    )
    monkeypatch.setattr(
        desktop_module,
        "_reserve_port",
        lambda host: (_ for _ in ()).throw(ProductTargetError("inert_setup_refused")),
    )

    fixture = desktop_module.management_desktop_lab.__wrapped__(tmp_path)
    with pytest.raises(ProductTargetError, match="inert_setup_refused"):
        next(fixture)

    assert listener.close_calls == 1
    assert guard.release_calls == 0
    assert any(
        resource is listener for resource in desktop_module._retained_desktop_resources
    )


def test_fresh_management_probe_supports_required_mcp(no_database_connections) -> None:
    harness = desktop_module.FreshManagementHarness()
    candidate = RuntimeCandidate(
        harness_id=harness.harness_id,
        executable="/usr/bin/true",
    )

    probe = harness.probe(candidate, datetime.now(UTC))

    RunExecution._check_required_capabilities(
        SimpleNamespace(required_capabilities=("mcp",)), probe.capabilities
    )
    assert set(probe.capabilities) == {
        "prompt",
        "acp",
        "model",
        "native_tools",
        "artifacts",
        "mcp",
    }
    assert all(
        probe.capabilities[name].status is SupportStatus.SUPPORTED
        for name in ("prompt", "acp", "model", "native_tools", "artifacts")
    )
    assert probe.capabilities["mcp"] == CapabilitySupport(
        status=SupportStatus.SUPPORTED,
        reason_code="controlled-fresh-desktop",
        constraints={},
    )
    assert [call[0] for call in harness.calls] == ["probe"]


@pytest.mark.parametrize(
    ("case", "paragraphs", "dialog_present", "expected_thread_id"),
    [
        (
            "valid_and_unrelated_text",
            [
                "Unrelated paragraph",
                "44444444-4444-4444-8444-444444444444",
                "会话 11111111-1111-4111-8111-111111111111",
            ],
            True,
            "11111111-1111-4111-8111-111111111111",
        ),
        ("missing_dialog", [], False, None),
        ("malformed_uuid", ["会话 not-a-uuid"], True, None),
        (
            "multiple_matches",
            [
                "会话 11111111-1111-4111-8111-111111111111",
                "会话 22222222-2222-4222-8222-222222222222",
            ],
            True,
            None,
        ),
        (
            "bare_uuid_only",
            ["55555555-5555-4555-8555-555555555555"],
            True,
            None,
        ),
    ],
    ids=[
        "valid_and_unrelated_text",
        "missing_dialog",
        "malformed_uuid",
        "multiple_matches",
        "bare_uuid_only",
    ],
)
def test_profile_test_driver_reads_one_exact_dialog_thread_id(
    case, paragraphs, dialog_present, expected_thread_id
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node24 is required for the emitted-driver test"
    config = json.dumps(
        {
            "task_space_id": 2,
            "backend_url": "http://127.0.0.1:8000",
            "workspace_id": "33333333-3333-4333-8333-333333333333",
        }
    )
    prelude = f"""
const fixtureParagraphs = {json.dumps(paragraphs)};
const fixtureDialog = {{
  querySelectorAll(selector) {{
    if (selector !== "p") throw new Error("unexpected_selector");
    return fixtureParagraphs.map((textContent) => ({{ textContent }}));
  }},
}};
globalThis.document = {{
  querySelector(selector) {{
    if (selector !== '[role="dialog"][aria-label="测试 Profile"]') {{
      throw new Error("unexpected_dialog_selector");
    }}
    return {"fixtureDialog" if dialog_present else "null"};
  }},
}};
globalThis.taskSpace = async (spaceId) => {{
  if (spaceId !== 2) throw new Error("unexpected_task_space");
  return {{
    page(label) {{
      if (label !== "p1") throw new Error("unexpected_page");
      return {{ async evaluate(callback) {{ return callback(); }} }};
    }},
  }};
}};
"""
    script = prelude + desktop_integration_module._BROWSER_DRIVER.replace(
        "__CONFIG__", config
    ).replace(
        "__BODY__",
        "return { thread_id: await profileTestThreadId(page) };",
    )

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    result_lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith("FRESH_RESULT:")
    ]
    if expected_thread_id is None:
        assert completed.returncode != 0, case
        assert result_lines == [], case
    else:
        assert completed.returncode == 0, completed.stderr
        assert len(result_lines) == 1
        assert json.loads(result_lines[0].removeprefix("FRESH_RESULT:")) == {
            "thread_id": expected_thread_id
        }


@pytest.mark.parametrize(
    (
        "case",
        "paragraphs",
        "revisions",
        "dialog_present",
        "expected_success",
        "expected_evaluations",
    ),
    [
        (
            "valid_dom_snapshot_unavailable",
            ["会话 11111111-1111-4111-8111-111111111111"],
            ["Profile R1"],
            True,
            True,
            2,
        ),
        (
            "wrong_revision",
            ["会话 11111111-1111-4111-8111-111111111111"],
            ["Profile R2"],
            True,
            False,
            2,
        ),
        (
            "foreign_thread",
            ["会话 22222222-2222-4222-8222-222222222222"],
            ["Profile R1"],
            True,
            False,
            2,
        ),
        (
            "missing_dialog",
            ["会话 11111111-1111-4111-8111-111111111111"],
            ["Profile R1"],
            False,
            False,
            1,
        ),
        (
            "ambiguous_revision",
            ["会话 11111111-1111-4111-8111-111111111111"],
            ["Profile R1", "Profile R2"],
            True,
            False,
            2,
        ),
    ],
    ids=[
        "valid_dom_snapshot_unavailable",
        "wrong_revision",
        "foreign_thread",
        "missing_dialog",
        "ambiguous_revision",
    ],
)
def test_profile_test_driver_checks_exact_dialog_pin(
    case, paragraphs, revisions, dialog_present, expected_success, expected_evaluations
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node24 is required for the emitted-driver test"
    expected_thread_id = "11111111-1111-4111-8111-111111111111"
    config = json.dumps(
        {
            "task_space_id": 2,
            "backend_url": "http://127.0.0.1:8000",
            "workspace_id": "33333333-3333-4333-8333-333333333333",
            "expected_thread_id": expected_thread_id,
        }
    )
    prelude = f"""
let evaluationCount = 0;
process.on("exit", () => console.log(`PIN_EVALUATIONS:${{evaluationCount}}`));
const paragraphs = {json.dumps(paragraphs)};
const revisions = {json.dumps(revisions)};
const dialog = {{
  querySelectorAll(selector) {{
    if (selector === "p") return paragraphs.map((textContent) => ({{ textContent }}));
    if (selector === "strong") return revisions.map((textContent) => ({{ textContent }}));
    throw new Error("unexpected_dialog_query");
  }},
}};
globalThis.document = {{
  querySelector(selector) {{
    if (selector !== '[role="dialog"][aria-label="测试 Profile"]') {{
      throw new Error("unexpected_dialog_selector");
    }}
    return {"dialog" if dialog_present else "null"};
  }},
}};
globalThis.taskSpace = async (spaceId) => {{
  if (spaceId !== 2) throw new Error("unexpected_task_space");
  return {{
    page(label) {{
      if (label !== "p1") throw new Error("unexpected_page");
      return {{
        async evaluate(callback) {{ evaluationCount += 1; return callback(); }},
        async snapshot() {{ throw new Error("snapshot_unavailable"); }},
      }};
    }},
  }};
}};
"""
    body = "await assertProfileTestDialogPin(page, config.expected_thread_id, 1);\nreturn { pinned: true };"
    script = prelude + desktop_integration_module._BROWSER_DRIVER.replace(
        "__CONFIG__", config
    ).replace("__BODY__", body)

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    result_lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith("FRESH_RESULT:")
    ]
    evaluation_lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith("PIN_EVALUATIONS:")
    ]
    assert evaluation_lines == [f"PIN_EVALUATIONS:{expected_evaluations}"]
    if expected_success:
        assert completed.returncode == 0, (case, completed.stderr)
        assert len(result_lines) == 1
        assert json.loads(result_lines[0].removeprefix("FRESH_RESULT:")) == {
            "pinned": True
        }
    else:
        assert completed.returncode != 0, case
        assert result_lines == [], case


def _run_profile_stop_driver_fixture(
    *,
    first_state: str = "accepted",
    query_state: str = "stopped",
    mismatch: str | None = None,
    revision: int = 1,
) -> tuple[subprocess.CompletedProcess[str], dict[str, int], list[str]]:
    body = getattr(desktop_integration_module, "_PROFILE_STOP_BODY", None)
    if not isinstance(body, str):
        pytest.fail("missing _PROFILE_STOP_BODY extraction", pytrace=False)

    node = shutil.which("node")
    assert node is not None, "Node24 is required for the emitted-driver test"
    assert type(revision) is int and revision > 0
    config = {
        "task_space_id": 2,
        "backend_url": "http://127.0.0.1:65032",
        "workspace_id": "33333333-3333-4333-8333-333333333333",
        "profile_id": "77777777-7777-4777-8777-777777777777",
        "profile_revision": revision,
        "test_thread_id": "11111111-1111-4111-8111-111111111111",
        "test_turn_id": "22222222-2222-4222-8222-222222222222",
        "test_run_id": "88888888-8888-4888-8888-888888888888",
        "unrelated_thread_id": "44444444-4444-4444-8444-444444444444",
        "unrelated_turn_id": "55555555-5555-4555-8555-555555555555",
        "unrelated_run_id": "66666666-6666-4666-8666-666666666666",
        "unrelated_page_label": "p2",
        "first_state": first_state,
        "query_state": query_state,
        "mismatch": mismatch,
    }
    prelude = f"""
{{
const fixtureConfig = {json.dumps(config, separators=(",", ":"))};
const actions = {{ read_original: 0, initial_stop: 0, query_stop: 0 }};
const fetches = [];
process.on("beforeExit", () => {{
  console.log("STOP_ACTIONS:" + JSON.stringify(actions));
  console.log("STOP_FETCHES:" + JSON.stringify(fetches));
}});
const state = {{ originalRead: false, queryEnabled: false, stopConfirmed: false }};
const expectedDialogs = new Set([
  '[role="dialog"][aria-label="测试 Profile"]',
  "[role=dialog][aria-label='测试 Profile']",
]);
const displayedThread = fixtureConfig.mismatch === "dialog_thread"
  ? "99999999-9999-4999-8999-999999999999" : fixtureConfig.test_thread_id;
const displayedRevision = fixtureConfig.mismatch === "dialog_revision"
  ? "Profile R" + (fixtureConfig.profile_revision === 1 ? 2 : 1)
  : "Profile R" + fixtureConfig.profile_revision;
const dialog = {{
  get textContent() {{ return "测试状态 " + (state.stopConfirmed ? "本次测试清理已确认" : "运行中"); }},
  querySelector(selector) {{
    if (selector !== "[role=status]") throw new Error("unexpected_dialog_selector");
    return {{ get textContent() {{ return state.stopConfirmed ? "本次测试清理已确认" : "原停止结果未确认"; }} }};
  }},
  querySelectorAll(selector) {{
    if (selector === "p") return [{{ textContent: "会话 " + displayedThread }}];
    if (selector === "strong") return [{{ textContent: displayedRevision }}];
    if (selector === "button") return [{{ textContent: "查询原停止结果", disabled: !state.queryEnabled }}];
    throw new Error("unexpected_dialog_query");
  }},
}};
globalThis.document = {{
  querySelector(selector) {{
    if (!expectedDialogs.has(selector)) throw new Error("unexpected_document_query");
    return dialog;
  }},
}};
function thread(threadId, turnId, runId, status) {{
  const profileRef = {{
    workspace_id: fixtureConfig.workspace_id,
    profile_id: fixtureConfig.profile_id,
    revision: fixtureConfig.profile_revision,
  }};
  return {{
    id: threadId,
    contextRefs: [],
    profileRef,
    turns: [{{ id: turnId, runId, operationId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", profileRef, status }}],
  }};
}}
function makePage(label) {{
  return {{
    label,
    async fetch(path, options) {{
      fetches.push({{ page: label, path }});
      if ((label !== "p1" && label !== "p2")
        || JSON.stringify(Object.keys(options ?? {{}}).sort()) !== JSON.stringify(["cache", "credentials"])
        || options.credentials !== "include" || options.cache !== "no-store") {{
        throw new Error("unexpected_fetch_options");
      }}
      const parsed = new URL(path, "http://fixture.invalid");
      if (parsed.origin !== "http://fixture.invalid" || parsed.search !== "") {{
        throw new Error("unexpected_fetch_url");
      }}
      const testPath = "/main-agent/v1/threads/" + fixtureConfig.test_thread_id;
      const unrelatedPath = "/main-agent/v1/threads/" + fixtureConfig.unrelated_thread_id;
      if (parsed.pathname === testPath) {{
        const status = state.stopConfirmed ? "cancelled" : "in_progress";
        return {{ ok: true, body: JSON.stringify(thread(
          fixtureConfig.test_thread_id, fixtureConfig.test_turn_id, fixtureConfig.test_run_id, status,
        )) }};
      }}
      if (parsed.pathname === unrelatedPath) {{
        const status = fixtureConfig.mismatch === "unrelated_terminal" ? "completed" : "in_progress";
        return {{ ok: true, body: JSON.stringify(thread(
          fixtureConfig.unrelated_thread_id, fixtureConfig.unrelated_turn_id, fixtureConfig.unrelated_run_id, status,
        )) }};
      }}
      throw new Error("unexpected_fetch_path");
    }},
    async click(selector) {{
      if (label !== "p1") throw new Error("unexpected_page_action");
      if (selector === "loc=role:button[name='读取原测试结果']") {{
        if (actions.read_original !== 0) throw new Error("duplicate_original_read");
        actions.read_original += 1;
        state.originalRead = true;
        return;
      }}
      if (selector === "loc=role:button[name='停止本次测试']") {{
        if (!state.originalRead || actions.initial_stop !== 0) throw new Error("unexpected_initial_stop");
        actions.initial_stop += 1;
        if (fixtureConfig.first_state === "stopped") state.stopConfirmed = true;
        else state.queryEnabled = true;
        return;
      }}
      if (selector === "loc=role:button[name='查询原停止结果']") {{
        if (!state.queryEnabled || actions.query_stop !== 0) throw new Error("unexpected_query_stop");
        actions.query_stop += 1;
        state.queryEnabled = false;
        if (fixtureConfig.query_state === "stopped") state.stopConfirmed = true;
        return;
      }}
      throw new Error("unexpected_page_action");
    }},
    async evaluate(callback) {{ return callback(); }},
    async waitForFunction(callback, argument, options) {{
      if (typeof callback !== "function" || typeof options?.timeout !== "number") {{
        throw new Error("unexpected_wait_for_function");
      }}
      if (!(await callback(argument))) throw new Error("fake_wait_predicate_not_met");
    }},
    async waitForSelector(selector, options) {{
      if (selector !== "text=本次测试清理已确认" || options?.state !== "visible") {{
        throw new Error("unexpected_wait_for_selector");
      }}
      if (!state.stopConfirmed) throw new Error("stop_confirmation_refused");
    }},
  }};
}}
const pages = {{ p1: makePage("p1"), p2: makePage("p2") }};
globalThis.taskSpace = async (spaceId) => {{
  if (spaceId !== 2) throw new Error("unexpected_task_space");
  return {{
    spaceId: 2,
    ownership: "agent",
    page(label) {{
      if (label !== "p1" && label !== "p2") throw new Error("unexpected_page_label");
      return pages[label];
    }},
  }};
}};
}}
"""
    script = prelude + desktop_integration_module._BROWSER_DRIVER.replace(
        "__CONFIG__", json.dumps(config, separators=(",", ":"))
    ).replace("__BODY__", body)
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    action_markers = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith("STOP_ACTIONS:")
    ]
    assert len(action_markers) == 1, "expected one Stop action marker"
    actions = json.loads(action_markers[0].removeprefix("STOP_ACTIONS:"))
    expected_action_keys = {"read_original", "initial_stop", "query_stop"}
    assert type(actions) is dict and set(actions) == expected_action_keys
    assert all(type(count) is int and count >= 0 for count in actions.values())
    fetch_markers = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith("STOP_FETCHES:")
    ]
    assert len(fetch_markers) == 1, "expected one Stop fetch trace"
    fetches = json.loads(fetch_markers[0].removeprefix("STOP_FETCHES:"))
    test_path = "/main-agent/v1/threads/" + config["test_thread_id"]
    unrelated_path = "/main-agent/v1/threads/" + config["unrelated_thread_id"]
    expected_fetches = [{"page": "p1", "path": test_path}]
    if mismatch == "unrelated_terminal" or (
        mismatch is None and not (first_state == "unknown" and query_state == "unknown")
    ):
        expected_fetches.extend(
            [
                {"page": "p1", "path": test_path},
                {"page": "p2", "path": unrelated_path},
            ]
        )
    assert fetches == expected_fetches
    result_lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith("FRESH_RESULT:")
    ]
    return completed, actions, result_lines


@pytest.mark.parametrize("first_state", ["stopped", "accepted", "unknown"])
@pytest.mark.parametrize("revision", [1, 2])
def test_profile_stop_driver_queries_original_pending_result(
    first_state, revision, no_database_connections
) -> None:
    completed, actions, lines = _run_profile_stop_driver_fixture(
        first_state=first_state,
        revision=revision,
    )
    assert completed.returncode == 0
    assert actions == {
        "read_original": 1,
        "initial_stop": 1,
        "query_stop": 0 if first_state == "stopped" else 1,
    }
    assert len(lines) == 1
    result = json.loads(lines[0].removeprefix("FRESH_RESULT:"))
    assert result == {
        "test_thread_id": "11111111-1111-4111-8111-111111111111",
        "test_turn_id": "22222222-2222-4222-8222-222222222222",
        "test_profile_revision": revision,
        "test_turn_status": "cancelled",
        "unrelated_thread_id": "44444444-4444-4444-8444-444444444444",
        "unrelated_turn_id": "55555555-5555-4555-8555-555555555555",
        "unrelated_profile_revision": revision,
        "unrelated_run_id": "66666666-6666-4666-8666-666666666666",
        "unrelated_turn_status_after_test_stop": "in_progress",
    }


def test_profile_stop_driver_does_not_replay_unconfirmed_query(
    no_database_connections,
) -> None:
    completed, actions, lines = _run_profile_stop_driver_fixture(
        first_state="unknown",
        query_state="unknown",
    )
    assert completed.returncode != 0
    assert lines == []
    assert actions == {
        "read_original": 1,
        "initial_stop": 1,
        "query_stop": 1,
    }


@pytest.mark.parametrize(
    "mismatch", ["dialog_thread", "dialog_revision", "unrelated_terminal"]
)
def test_profile_stop_driver_preserves_original_and_isolation_guards(
    mismatch, no_database_connections
) -> None:
    completed, actions, lines = _run_profile_stop_driver_fixture(
        mismatch=mismatch,
    )
    assert completed.returncode != 0
    assert lines == []
    if mismatch == "unrelated_terminal":
        assert actions == {
            "read_original": 1,
            "initial_stop": 1,
            "query_stop": 1,
        }
    else:
        assert actions == {
            "read_original": 0,
            "initial_stop": 0,
            "query_stop": 0,
        }


@pytest.mark.parametrize(
    ("variant", "unrelated_page_label", "capture_overview"),
    [
        ("foreign_workspace", "p2", False),
        ("revision_two", "p2", False),
        ("primary_page_as_secondary", "p1", False),
        ("invalid_page_label", "workspace", False),
        ("non_boolean_capture_option", "p2", 1),
    ],
)
def test_profile_stop_suffix_rejects_invalid_context_before_side_effects(
    variant,
    unrelated_page_label,
    capture_overview,
    no_database_connections,
    monkeypatch,
) -> None:
    exercise_suffix = getattr(
        desktop_integration_module, "_exercise_profile_stop_isolation", None
    )
    assert callable(exercise_suffix), "profile stop suffix is not extracted"

    workspace_id = UUID("b9e469c3-ad4a-5bcd-a2b9-75f9c45e93fc")
    profile_id = "fbb669f4-d902-4ca0-8e4c-81993081387b"
    selected_workspace = (
        str(UUID("7b27b31a-702d-4dcb-a4cc-e2d8a4a4be11"))
        if variant == "foreign_workspace"
        else str(workspace_id)
    )
    revision = 2 if variant == "revision_two" else 1
    profile_ref = {
        "workspace_id": selected_workspace,
        "profile_id": profile_id,
        "revision": revision,
    }
    lab = SimpleNamespace(workspace_id=workspace_id)
    side_effects: list[str] = []

    def refuse_side_effect(name):
        def trap(*_args, **_kwargs):
            side_effects.append(name)
            pytest.fail(f"invalid suffix context reached {name}")

        return trap

    for name in (
        "begin_fresh_stage",
        "complete_fresh_stage",
        "_run_fresh_input_stage",
        "_run_stage",
        "run_ego_browser",
        "_ensure_unrelated_page",
        "_capture_recent_run_overview",
        "_active_run",
    ):
        monkeypatch.setattr(desktop_integration_module, name, refuse_side_effect(name))

    with pytest.raises(ProductTargetError, match="fresh_stop_suffix_context_refused"):
        exercise_suffix(
            lab,
            space_id=2,
            profile_ref=profile_ref,
            unrelated_page_label=unrelated_page_label,
            capture_overview=capture_overview,
        )

    assert side_effects == []


def test_fresh_native_close_releases_only_original_run_gate(
    no_database_connections,
) -> None:
    harness = desktop_module.FreshManagementHarness()
    first = harness.open(SimpleNamespace(run_id=uuid4()))
    second = harness.open(SimpleNamespace(run_id=uuid4()))
    first_gate = harness.run_gates[first.run_id]
    second_gate = harness.run_gates[second.run_id]

    outcome = harness.close(first)

    assert outcome.status is NativeCloseState.CLOSED
    assert first_gate.is_set()
    assert harness.inspect(first).process_state is NativeProcessState.GONE
    assert not second_gate.is_set()
    assert harness.inspect(second).process_state is NativeProcessState.ALIVE
    assert harness.calls == [
        ("open", first.run_id),
        ("open", second.run_id),
        ("close", first.run_id),
    ]


def test_fresh_native_late_prompt_cannot_revive_closed_run(
    no_database_connections,
) -> None:
    harness = desktop_module.FreshManagementHarness()
    handle = harness.open(SimpleNamespace(run_id=uuid4()))
    response_id = uuid4()

    class CloseDuringWait:
        def __init__(self):
            self.release_calls = 0

        def wait(self, timeout=None):
            harness.close(handle)
            return True

        def set(self):
            self.release_calls += 1

    gate = CloseDuringWait()
    harness.run_gates[handle.run_id] = gate
    original_handle = harness.handles[handle.run_id]

    outcome = harness.prompt(handle, SimpleNamespace(), response_id)

    assert outcome.status == "cancelled"
    assert outcome.response_id == response_id
    assert harness.run_finished[handle.run_id].is_set()
    assert harness.inspect(handle).process_state is NativeProcessState.GONE
    assert harness.handles == {handle.run_id: original_handle}
    assert original_handle is handle
    assert gate.release_calls == 1
    assert harness.calls == [
        ("open", handle.run_id),
        ("prompt", handle.run_id, response_id),
        ("close", handle.run_id),
    ]


def test_fresh_native_cancel_after_close_cannot_revive_closed_run(
    no_database_connections,
) -> None:
    harness = desktop_module.FreshManagementHarness()
    handle = harness.open(SimpleNamespace(run_id=uuid4()))
    other = harness.open(SimpleNamespace(run_id=uuid4()))
    response_id = uuid4()

    close_outcome = harness.close(handle)
    cancel_outcome = harness.cancel(handle, response_id)

    assert close_outcome.status is NativeCloseState.CLOSED
    assert cancel_outcome.status is NativeCancelState.CONFIRMED
    assert cancel_outcome.response_id == response_id
    assert harness.inspect(handle).process_state is NativeProcessState.GONE
    assert not harness.run_gates[other.run_id].is_set()
    assert harness.inspect(other).process_state is NativeProcessState.ALIVE
    assert harness.calls == [
        ("open", handle.run_id),
        ("open", other.run_id),
        ("close", handle.run_id),
        ("cancel", handle.run_id, response_id),
    ]


def test_fresh_native_unknown_close_keeps_original_gate_and_observation(
    no_database_connections,
) -> None:
    harness = desktop_module.FreshManagementHarness()
    handle = harness.open(SimpleNamespace(run_id=uuid4()))
    original_gate = harness.run_gates[handle.run_id]
    original_inspection = harness.inspect(handle)
    harness.close_unknown = True

    outcome = harness.close(handle)

    assert outcome.status is NativeCloseState.UNKNOWN
    assert outcome.reason_code == "controlled_close_unknown"
    assert not original_gate.is_set()
    assert harness.inspect(handle) == original_inspection
    assert harness.close_attempted.is_set()
    assert harness.calls == [
        ("open", handle.run_id),
        ("close", handle.run_id),
    ]


def test_fresh_native_prompt_entering_after_close_preserves_gone(
    no_database_connections,
) -> None:
    harness = desktop_module.FreshManagementHarness()
    handle = harness.open(SimpleNamespace(run_id=uuid4()))
    response_id = uuid4()

    close_outcome = harness.close(handle)
    outcome = harness.prompt(handle, SimpleNamespace(), response_id)

    assert close_outcome.status is NativeCloseState.CLOSED
    assert outcome.status == "cancelled"
    assert outcome.response_id == response_id
    assert harness.run_entered[handle.run_id].is_set()
    assert harness.run_finished[handle.run_id].is_set()
    assert harness.inspect(handle).process_state is NativeProcessState.GONE
    assert harness.calls == [
        ("open", handle.run_id),
        ("close", handle.run_id),
        ("prompt", handle.run_id, response_id),
    ]


def test_fresh_driver_stage_packets_keep_required_intent_fields(
    no_database_connections, monkeypatch
) -> None:
    workspace_id = UUID("3ec2ed26-a9d7-47f0-a3ca-5570a2aa1ee4")
    lab = SimpleNamespace(
        backend_url="http://127.0.0.1:55439",
        web_url="http://[::1]:55440",
        workspace_id=workspace_id,
    )
    captured: list[str] = []
    monkeypatch.setattr(
        desktop_integration_module,
        "run_ego_browser",
        lambda script: captured.append(script) or {"accepted": True},
    )
    variables = {"profile_id": "profile-27", "profile_name": "N20 profile"}

    try:
        result = desktop_integration_module._run_stage(
            lab,
            "return { accepted: true };",
            task_space_id=711,
            variables=variables,
        )
    except TypeError as error:
        pytest.fail(f"stage packet variables are not supported: {error}")

    assert result == {"accepted": True}
    assert len(captured) == 1
    config_source = (
        captured[0].split("const config = ", 1)[1].split(";\nconst task", 1)[0]
    )
    assert json.loads(config_source) == {
        "task_space_id": 711,
        "backend_url": "http://127.0.0.1:55439",
        "web_url": "http://[::1]:55440",
        "workspace_id": str(workspace_id),
        "screenshots": {},
        "profile_id": "profile-27",
        "profile_name": "N20 profile",
    }
    assert "taskSpace(config.task_space_id)" in captured[0]

    with pytest.raises(ProductTargetError, match="fresh_browser_config_refused"):
        desktop_integration_module._run_stage(
            lab,
            "return {};",
            task_space_id=711,
            variables={"workspace_id": "foreign-workspace"},
        )
    assert len(captured) == 1

    desktop_integration_module._run_stage(
        lab,
        "return { accepted: true };",
        task_space_id=712,
        variables={
            "profile_id": "profile-27",
            "unrelated_input": "N20 separate conversation",
        },
    )
    second_config_source = (
        captured[1].split("const config = ", 1)[1].split(";\nconst task", 1)[0]
    )
    assert json.loads(second_config_source) == {
        "task_space_id": 712,
        "backend_url": "http://127.0.0.1:55439",
        "web_url": "http://[::1]:55440",
        "workspace_id": str(workspace_id),
        "screenshots": {},
        "profile_id": "profile-27",
        "unrelated_input": "N20 separate conversation",
    }


def test_fresh_stage_first_order_preserves_private_ancestors(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    admission = desktop_module.select_management_desktop_targets(
        _fresh_desktop_environ(tmp_path)
    )
    assert admission is not None
    fresh_root = admission.evidence_directory / "fresh-desktop"
    assert not fresh_root.exists()

    bind_calls: list[tuple[str, int]] = []
    reserve_calls: list[str] = []

    class FakeListener:
        def __init__(self, port: int):
            self.port = port
            self.closed = False

        def close(self):
            self.closed = True

    def bind(host: str, port: int = 0):
        bind_calls.append((host, port))
        actual_port = 55439 if port == 0 else port
        return FakeListener(actual_port), actual_port

    def reserve(host: str) -> int:
        reserve_calls.append(host)
        return 55440

    monkeypatch.setattr(desktop_module, "_bind_listener", bind)
    monkeypatch.setattr(desktop_module, "_reserve_port", reserve)

    intent = {"source": "inert-unit-transfer"}
    result = {"accepted": True, "source": "inert-unit-transfer"}
    started, saved = desktop_module.begin_fresh_stage(
        admission, "allocation_transfer", intent
    )
    assert (started, saved) == (True, None)
    desktop_module.complete_fresh_stage(
        admission, "allocation_transfer", intent, result
    )
    pending_path, complete_path = desktop_module._fresh_stage_paths(
        admission, "allocation_transfer"
    )
    original_stage_bytes = (pending_path.read_bytes(), complete_path.read_bytes())

    context, listener = desktop_module.load_or_create_fresh_context(admission)

    context_paths = desktop_module.fresh_desktop_context_paths(admission)
    for directory in (
        admission.evidence_directory,
        pending_path.parent.parent,
        pending_path.parent,
        context_paths["directory"],
    ):
        assert directory.stat().st_mode & 0o777 == 0o700
    assert pending_path.stat().st_mode & 0o777 == 0o600
    assert complete_path.stat().st_mode & 0o777 == 0o600
    assert context_paths["file"].stat().st_mode & 0o777 == 0o600
    assert (
        pending_path.read_bytes(),
        complete_path.read_bytes(),
    ) == original_stage_bytes
    assert context["backend_port"] == 55439
    assert context["vite_port"] == 55440
    assert desktop_module.begin_fresh_stage(
        admission, "allocation_transfer", intent
    ) == (False, result)
    assert bind_calls == [("127.0.0.1", 0)]
    assert reserve_calls == ["::1"]
    listener.close()
    assert listener.closed is True


@pytest.mark.parametrize("unsafe_kind", ("permissions", "symlink"))
def test_fresh_stage_refuses_unsafe_ancestor(
    tmp_path, no_database_connections, monkeypatch, unsafe_kind: str
) -> None:
    admission = desktop_module.select_management_desktop_targets(
        _fresh_desktop_environ(tmp_path)
    )
    assert admission is not None
    fresh_root = admission.evidence_directory / "fresh-desktop"
    sibling = admission.evidence_directory / "fresh-desktop-target"
    if unsafe_kind == "permissions":
        fresh_root.mkdir(mode=0o755)
        fresh_root.chmod(0o755)
    else:
        sibling.mkdir(mode=0o700)
        sibling.chmod(0o700)
        fresh_root.symlink_to(sibling, target_is_directory=True)

    writes: list[tuple[Path, object]] = []
    bind_calls: list[tuple[str, int]] = []
    reserve_calls: list[str] = []
    browser_calls: list[str] = []
    monkeypatch.setattr(
        desktop_module,
        "write_private_json",
        lambda path, value: writes.append((path, value)),
    )
    monkeypatch.setattr(
        desktop_module,
        "_bind_listener",
        lambda host, port=0: bind_calls.append((host, port)),
    )
    monkeypatch.setattr(
        desktop_module, "_reserve_port", lambda host: reserve_calls.append(host)
    )
    monkeypatch.setattr(
        desktop_integration_module,
        "run_ego_browser",
        lambda script: browser_calls.append(script),
    )

    with pytest.raises(ProductTargetError, match="fresh_context_refused"):
        desktop_module.begin_fresh_stage(
            admission, "allocation_transfer", {"source": "inert-unit-transfer"}
        )

    assert writes == []
    assert bind_calls == []
    assert reserve_calls == []
    assert browser_calls == []
    if unsafe_kind == "permissions":
        assert fresh_root.stat().st_mode & 0o777 == 0o755
    else:
        assert fresh_root.is_symlink()
        assert list(sibling.iterdir()) == []


def test_fresh_desktop_context_reuses_original_ports_paths_and_targets(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    loader = getattr(desktop_module, "load_or_create_fresh_context", None)
    paths_for = getattr(desktop_module, "fresh_desktop_context_paths", None)
    assert callable(loader), "fresh context persistence is not implemented"
    assert callable(paths_for), "fresh context paths are not derived from saved context"

    case_path = tmp_path / "context"
    case_path.mkdir(mode=0o700)
    admission = desktop_module.select_management_desktop_targets(
        _fresh_desktop_environ(case_path)
    )
    assert admission is not None
    bind_calls: list[tuple[str, int]] = []
    reserve_calls: list[str] = []

    class FakeListener:
        def __init__(self, port: int):
            self.port = port
            self.closed = False

        def close(self):
            self.closed = True

    def bind(host: str, port: int = 0):
        bind_calls.append((host, port))
        actual_port = 55439 if port == 0 else port
        return FakeListener(actual_port), actual_port

    def reserve(host: str) -> int:
        reserve_calls.append(host)
        return 55440

    monkeypatch.setattr(desktop_module, "_bind_listener", bind)
    monkeypatch.setattr(desktop_module, "_reserve_port", reserve)

    original, first_listener = loader(admission)
    assert original == {
        "schema_version": 1,
        "allocation_id": admission.allocation_id,
        "target_identity": [
            {
                "component": target.component,
                "database": target.database,
                "owner": target.owner,
                "marker": target.marker,
                "socket": target.socket,
                "port": target.port,
            }
            for target in admission.targets
        ],
        "workspace_id": str(
            desktop_module.management_workspace_id(admission.allocation_id)
        ),
        "backend_port": 55439,
        "vite_port": 55440,
    }
    first_listener.close()
    resumed, second_listener = loader(admission)
    assert resumed == original
    assert bind_calls == [("127.0.0.1", 0), ("127.0.0.1", 55439)]
    assert reserve_calls == ["::1"]

    paths = paths_for(admission)
    context_directory = admission.evidence_directory / "fresh-desktop" / "context"
    assert paths == {
        "directory": context_directory,
        "file": context_directory / "context.json",
        "repository": context_directory / "repository",
        "workspace_root": context_directory / "workspaces",
        "artifact_root": context_directory / "artifacts",
        "bootstrap_file": context_directory / "bootstrap" / "native-session.json",
    }
    assert paths["file"].stat().st_mode & 0o777 == 0o600
    assert paths["directory"].stat().st_mode & 0o777 == 0o700

    for mutation in ("target", "workspace", "boolean_port"):
        invalid = json.loads(json.dumps(original))
        if mutation == "target":
            invalid["target_identity"][0]["owner"] = "foreign_owner"
        elif mutation == "workspace":
            invalid["workspace_id"] = str(uuid4())
        else:
            invalid["backend_port"] = True
        paths["file"].unlink()
        inventory_module.write_private_json(paths["file"], invalid)
        calls_before = (len(bind_calls), len(reserve_calls))
        with pytest.raises(ProductTargetError, match="fresh_context_refused"):
            loader(admission)
        assert (len(bind_calls), len(reserve_calls)) == calls_before
        paths["file"].unlink()
        inventory_module.write_private_json(paths["file"], original)

    second_listener.close()


def test_fresh_browser_handle_is_saved_before_authorization_and_never_recreated(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    ensure_space = getattr(desktop_integration_module, "_ensure_browser_space", None)
    assert callable(ensure_space), (
        "browser-space creation is not independently checkpointed"
    )

    case_path = tmp_path / "browser-space"
    case_path.mkdir(mode=0o700)
    admission = desktop_module.select_management_desktop_targets(
        _fresh_desktop_environ(case_path)
    )
    assert admission is not None
    invocations: list[str] = []
    monkeypatch.setattr(
        desktop_integration_module,
        "run_ego_browser",
        lambda script: (
            invocations.append(script) or {"space_id": 731, "page_label": "p1"}
        ),
    )
    lab = SimpleNamespace(admission=admission, browser_space_id=None, owned={})

    result = ensure_space(lab)

    assert result == {"space_id": 731, "page_label": "p1"}
    assert lab.browser_space_id == 731
    assert len(invocations) == 1
    assert "taskSpace(config.task_space_name)" in invocations[0]
    assert 'task.page("p1")' in invocations[0]
    assert all(
        action not in invocations[0]
        for action in ("page.goto(", "page.click(", "page.fill(")
    )
    intent = {"task_space_name": "Sidera N20 native-harness"}
    started, saved = desktop_module.begin_fresh_stage(
        admission, "browser_space", intent
    )
    assert started is False
    assert saved == result

    resumed_lab = SimpleNamespace(admission=admission, browser_space_id=None, owned={})
    assert ensure_space(resumed_lab) == result
    assert resumed_lab.browser_space_id == 731
    assert len(invocations) == 1

    pending_path = tmp_path / "pending-space"
    pending_path.mkdir(mode=0o700)
    pending_admission = desktop_module.select_management_desktop_targets(
        _fresh_desktop_environ(pending_path)
    )
    assert pending_admission is not None
    assert desktop_module.begin_fresh_stage(
        pending_admission, "browser_space", intent
    ) == (True, None)
    pending_lab = SimpleNamespace(
        admission=pending_admission, browser_space_id=None, owned={}
    )
    with pytest.raises(ProductTargetError, match="fresh_stage_pending"):
        ensure_space(pending_lab)
    assert len(invocations) == 1


def test_fresh_input_stage_ids_survive_later_failure_without_prompt_replay(
    tmp_path, no_database_connections
) -> None:
    run_stage = getattr(desktop_integration_module, "_run_fresh_input_stage", None)
    assert callable(run_stage), "input stage replay boundary is not implemented"

    case_path = tmp_path / "input-stages"
    case_path.mkdir(mode=0o700)
    admission = desktop_module.select_management_desktop_targets(
        _fresh_desktop_environ(case_path)
    )
    assert admission is not None
    profile_intent = {
        "profile_ref": {
            "workspace_id": "b9e469c3-ad4a-5bcd-a2b9-75f9c45e93fc",
            "profile_id": "fbb669f4-d902-4ca0-8e4c-81993081387b",
            "revision": 1,
        },
        "input_sha256": "a" * 64,
        "context_refs": [],
        "turn_count": 1,
    }
    unrelated_intent = {
        "profile_ref": profile_intent["profile_ref"],
        "input_sha256": "b" * 64,
        "context_refs": [],
        "turn_count": 1,
    }
    profile_result = {
        "workspace_id": profile_intent["profile_ref"]["workspace_id"],
        "profile_id": profile_intent["profile_ref"]["profile_id"],
        "profile_revision": 1,
        "thread_id": "c7524a9c-742f-4877-9c0d-44f8b05d392d",
        "turn_id": "d4328dad-a061-47e7-81c3-31ecfaf38058",
        "run_id": "e1ab28ce-559b-4f3f-9c6a-91f31f4af836",
        "operation_id": "bbd37a95-452d-4d4f-8d1c-b3c986112384",
        "response_id": "8f1e10a0-28d7-47ad-a837-650a43167c14",
        "context_refs": [],
        "turn_count": 1,
        "status": "in_progress",
        "selected_model": None,
        "observed_model": None,
    }
    unrelated_result = {
        "workspace_id": profile_result["workspace_id"],
        "page_label": "p2",
        "profile_id": profile_result["profile_id"],
        "profile_revision": 1,
        "thread_id": "01ad264e-910a-4675-9d8d-869734d04f00",
        "turn_id": "a8b6b803-71b7-40d2-b8f1-4966155fab68",
        "run_id": "e9b27f1d-c9ec-4b3a-9c8d-a71eacb99fac",
        "operation_id": "0e397c53-2c43-4a58-9cd6-0f0bf07a7f5e",
        "context_refs": [],
        "turn_count": 1,
        "status": "in_progress",
    }
    prompt_starts: list[str] = []

    for stage, intent, result in (
        ("profile_test", profile_intent, profile_result),
        ("unrelated_conversation", unrelated_intent, unrelated_result),
    ):
        started, actual = run_stage(
            admission,
            stage,
            intent,
            lambda stage=stage, result=result: prompt_starts.append(stage) or result,
        )
        assert started is True
        assert actual == result
        desktop_module.complete_fresh_stage(admission, stage, intent, actual)

    revision_intent = {"parent_ref": profile_intent["profile_ref"], "description": "R2"}
    assert desktop_module.begin_fresh_stage(
        admission, "profile_revision", revision_intent
    ) == (True, None)
    with pytest.raises(ProductTargetError, match="fresh_stage_pending"):
        desktop_module.begin_fresh_stage(admission, "profile_revision", revision_intent)

    for stage, intent, original in (
        ("profile_test", profile_intent, profile_result),
        ("unrelated_conversation", unrelated_intent, unrelated_result),
    ):
        started, resumed = run_stage(
            admission,
            stage,
            intent,
            lambda: pytest.fail("completed input stage replayed its prompt"),
        )
        assert started is False
        assert resumed == original
        replacement = {**original, "run_id": str(uuid4())}
        with pytest.raises(ProductTargetError, match="fresh_stage_refused"):
            desktop_module.complete_fresh_stage(admission, stage, intent, replacement)

    assert prompt_starts == ["profile_test", "unrelated_conversation"]


@pytest.mark.parametrize("variant", ["valid", "wrong_run", "overflow"])
def test_fresh_recent_run_overview_uses_only_saved_read_context(
    variant, tmp_path, no_database_connections, monkeypatch
) -> None:
    capture = getattr(desktop_integration_module, "_capture_recent_run_overview", None)
    workspace_id = UUID("b9e469c3-ad4a-5bcd-a2b9-75f9c45e93fc")
    profile_id = "fbb669f4-d902-4ca0-8e4c-81993081387b"
    profile_test = {
        "thread_id": "c7524a9c-742f-4877-9c0d-44f8b05d392d",
        "run_id": "e1ab28ce-559b-4f3f-9c6a-91f31f4af836",
    }
    unrelated = {
        "page_label": "p2",
        "thread_id": "01ad264e-910a-4675-9d8d-869734d04f00",
        "run_id": "e9b27f1d-c9ec-4b3a-9c8d-a71eacb99fac",
    }
    run_ids = [profile_test["run_id"], unrelated["run_id"]]
    screenshots = {
        str(width): str(tmp_path / f"profile-r2-recent-{width}.png")
        for width in (1280, 1440, 1600)
    }
    geometry = [
        {
            "width": width,
            "viewport_width": width,
            "viewport_height": 1000,
            "panel_bounds_finite": True,
            "document_overflow_x": False,
        }
        for width in (1280, 1440, 1600)
    ]
    receipt = {
        "profile_id": profile_id,
        "revision": 2,
        "run_ids": run_ids,
        "screenshots": screenshots,
        "geometry": geometry,
    }
    if variant == "wrong_run":
        receipt["run_ids"] = [run_ids[0], str(uuid4())]
    elif variant == "overflow":
        receipt["geometry"] = [
            *geometry[:1],
            {**geometry[1], "document_overflow_x": True},
            *geometry[2:],
        ]

    lab = SimpleNamespace(
        backend_url="http://127.0.0.1:55439",
        web_url="http://[::1]:55440",
        workspace_id=workspace_id,
        browser_space_id=731,
        settings=SimpleNamespace(artifact_root=tmp_path),
        owned={},
    )
    scripts: list[str] = []
    monkeypatch.setattr(
        desktop_integration_module,
        "run_ego_browser",
        lambda script: scripts.append(script) or receipt,
    )

    assert callable(capture), "recent-run overview capture is not implemented"
    if variant == "valid":
        result = capture(
            lab,
            task_space_id=731,
            profile_id=profile_id,
            profile_test_result=profile_test,
            unrelated_result=unrelated,
        )
        assert result == receipt
        assert lab.owned["recent_run_overview"] == receipt
    else:
        with pytest.raises(ProductTargetError, match="fresh_recent_overview"):
            capture(
                lab,
                task_space_id=731,
                profile_id=profile_id,
                profile_test_result=profile_test,
                unrelated_result=unrelated,
            )
        assert "recent_run_overview" not in lab.owned

    assert len(scripts) == 1
    script = scripts[0]
    encoded_config = script.split("const config = ", 1)[1].split(";", 1)[0]
    config = json.loads(encoded_config)
    assert config["task_space_id"] == 731
    assert config["profile_id"] == profile_id
    assert config["run_ids"] == run_ids
    assert config["thread_ids"] == [profile_test["thread_id"], unrelated["thread_id"]]
    assert config["unrelated_page_label"] == "p2"
    assert config["screenshots"] == screenshots
    assert "task.page(config.unrelated_page_label)" in script
    assert "task.newPage(" not in script
    assert ".click(" not in script
    assert ".fill(" not in script
    assert "page2.screenshot(" in script


_MANAGED_WRITE_WORKSPACE_ID = UUID("efcc826a-3cf2-4a64-90b1-a2df18546a42")
_MANAGED_WRITE_OPERATION_ID = "8dc06906-6236-5a0b-81d9-549ac7381d9d"
_MANAGED_WRITE_CREDENTIAL_ID = "688b3eef-fd9a-4ce8-a655-3e577f9ff340"
_MANAGED_WRITE_ALIAS = "N20 controlled managed credential"
_MANAGED_WRITE_SECRET = "inert-synthetic-managed-secret"


class _ManagedWriteResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, sort_keys=True)

    def json(self) -> dict[str, object]:
        return self._payload


class _ManagedWriteClient:
    def __init__(
        self,
        *,
        get_responses: list[_ManagedWriteResponse] | None = None,
        post_responses: list[_ManagedWriteResponse] | None = None,
        get_errors: list[Exception] | None = None,
        post_errors: list[Exception] | None = None,
    ) -> None:
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_errors = list(get_errors or [])
        self.post_errors = list(post_errors or [])
        self.calls: list[dict[str, object]] = []
        self.before_post = None

    def get(self, url: str, *, headers: dict[str, str]) -> _ManagedWriteResponse:
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        if self.get_errors:
            raise self.get_errors.pop(0)
        if not self.get_responses:
            raise AssertionError("unexpected managed credential GET")
        return self.get_responses.pop(0)

    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
    ) -> _ManagedWriteResponse:
        self.calls.append(
            {"method": "POST", "url": url, "json": json, "headers": headers}
        )
        if self.before_post is not None:
            self.before_post(url, json)
        if self.post_errors:
            raise self.post_errors.pop(0)
        if not self.post_responses:
            raise AssertionError("unexpected managed credential POST")
        return self.post_responses.pop(0)


def _managed_write_payload() -> dict[str, object]:
    return {
        "workspace_id": str(_MANAGED_WRITE_WORKSPACE_ID),
        "operation_id": _MANAGED_WRITE_OPERATION_ID,
        "status": "completed",
        "credential": {
            "workspace_id": str(_MANAGED_WRITE_WORKSPACE_ID),
            "credential_id": _MANAGED_WRITE_CREDENTIAL_ID,
            "alias": _MANAGED_WRITE_ALIAS,
            "source": "macos_keychain",
            "status": "active",
            "version": 2,
            "replaces_id": None,
        },
    }


def _managed_write_lab(tmp_path, client: _ManagedWriteClient):
    evidence = tmp_path / "private-evidence"
    evidence.mkdir(mode=0o700)
    return SimpleNamespace(
        workspace_id=_MANAGED_WRITE_WORKSPACE_ID,
        evidence_directory=evidence,
        headers={"x-inert-csrf": "synthetic"},
        client=client,
        owned={},
    )


def _managed_write_stage_paths(evidence: Path) -> tuple[Path, Path]:
    directory = (
        evidence / "cold-managed-credential-write" / str(_MANAGED_WRITE_WORKSPACE_ID)
    )
    return directory / "pending.json", directory / "complete.json"


def _managed_write_stage_record(
    phase: str, result: dict[str, object] | None = None
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "case": "cold-managed-credential-write",
        "workspace_id": str(_MANAGED_WRITE_WORKSPACE_ID),
        "phase": phase,
        "intent": {
            "workspace_id": str(_MANAGED_WRITE_WORKSPACE_ID),
            "operation_id": _MANAGED_WRITE_OPERATION_ID,
            "alias": _MANAGED_WRITE_ALIAS,
            "expected_source": "macos_keychain",
        },
    }
    if phase == "complete":
        assert result is not None
        record["result"] = result
    return record


def _managed_write_urls() -> tuple[str, str]:
    base = f"/model-config/v1/workspaces/{_MANAGED_WRITE_WORKSPACE_ID}"
    return (
        base + f"/credential-writes/{_MANAGED_WRITE_OPERATION_ID}",
        base + "/credential-writes",
    )


def test_cold_managed_write_accepts_actual_catalog_completed_version(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    now = datetime.now(UTC)
    write_row = CredentialWriteRow(
        workspace_id=_MANAGED_WRITE_WORKSPACE_ID,
        operation_id=UUID(_MANAGED_WRITE_OPERATION_ID),
        credential_id=UUID(_MANAGED_WRITE_CREDENTIAL_ID),
        status="pending",
        failure_code=None,
        created_at=now,
        updated_at=now,
    )
    credential_row = CredentialRow(
        workspace_id=_MANAGED_WRITE_WORKSPACE_ID,
        id=UUID(_MANAGED_WRITE_CREDENTIAL_ID),
        alias=_MANAGED_WRITE_ALIAS,
        source="macos_keychain",
        status="pending",
        env_name=None,
        replaces_id=None,
        version=1,
        created_at=now,
        revoked_at=None,
    )

    class InertSession:
        def __init__(self) -> None:
            self.rows = [write_row, credential_row]
            self.executed: list[object] = []

        def scalar(self, _statement):
            assert self.rows
            return self.rows.pop(0)

        def execute(self, statement):
            self.executed.append(statement)
            return SimpleNamespace(rowcount=1)

    session = InertSession()
    catalog = PostgresConnectionCatalog(
        SimpleNamespace(begin=lambda: nullcontext(session))
    )
    produced = catalog.finish_credential_write(
        _MANAGED_WRITE_WORKSPACE_ID,
        UUID(_MANAGED_WRITE_OPERATION_ID),
        status="completed",
    )
    payload = _write_summary(produced).model_dump(mode="json")
    expected = {
        "workspace_id": str(_MANAGED_WRITE_WORKSPACE_ID),
        "operation_id": _MANAGED_WRITE_OPERATION_ID,
        "status": "completed",
        "credential": {
            "workspace_id": str(_MANAGED_WRITE_WORKSPACE_ID),
            "credential_id": _MANAGED_WRITE_CREDENTIAL_ID,
            "alias": _MANAGED_WRITE_ALIAS,
            "source": "macos_keychain",
            "status": "active",
            "version": 2,
            "replaces_id": None,
        },
    }
    assert payload == expected
    assert len(session.executed) == 3

    client = _ManagedWriteClient(get_responses=[_ManagedWriteResponse(200, payload)])
    lab = _managed_write_lab(tmp_path, client)
    pending_path, complete_path = _managed_write_stage_paths(lab.evidence_directory)
    pending = _managed_write_stage_record("pending")
    inventory_module.write_private_json(pending_path, pending)
    monkeypatch.setattr(
        api_integration_module.secrets,
        "token_urlsafe",
        lambda _: pytest.fail("original-operation GET generated a new secret"),
    )

    resumed = _managed_credential_write(lab)

    original_url, _ = _managed_write_urls()
    assert resumed == expected
    assert client.calls == [
        {"method": "GET", "url": original_url, "headers": lab.headers}
    ]
    assert inventory_module.read_private_json(pending_path) == pending
    assert inventory_module.read_private_json(complete_path) == (
        _managed_write_stage_record("complete", expected)
    )


@pytest.mark.parametrize("version", (1, 3))
def test_cold_managed_write_refuses_noncanonical_completed_version(
    tmp_path, no_database_connections, monkeypatch, version: int
) -> None:
    payload = _managed_write_payload()
    payload["credential"]["version"] = version
    client = _ManagedWriteClient(get_responses=[_ManagedWriteResponse(200, payload)])
    lab = _managed_write_lab(tmp_path, client)
    pending_path, complete_path = _managed_write_stage_paths(lab.evidence_directory)
    pending = _managed_write_stage_record("pending")
    inventory_module.write_private_json(pending_path, pending)
    monkeypatch.setattr(
        api_integration_module.secrets,
        "token_urlsafe",
        lambda _: pytest.fail("original-operation GET generated a new secret"),
    )

    with pytest.raises(
        ProductTargetError, match="cold_managed_credential_write_refused"
    ):
        _managed_credential_write(lab)

    original_url, _ = _managed_write_urls()
    assert client.calls == [
        {"method": "GET", "url": original_url, "headers": lab.headers}
    ]
    assert inventory_module.read_private_json(pending_path) == pending
    assert complete_path.exists() is False


def test_cold_managed_write_new_intent_posts_once_without_absence_probe(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    result = _managed_write_payload()
    client = _ManagedWriteClient(post_responses=[_ManagedWriteResponse(201, result)])
    lab = _managed_write_lab(tmp_path, client)
    pending_path, complete_path = _managed_write_stage_paths(lab.evidence_directory)
    observed_pending_before_post: list[bool] = []

    def observe_pending_before_post(url, payload) -> None:
        assert url == _managed_write_urls()[1]
        assert payload == {
            "operation_id": _MANAGED_WRITE_OPERATION_ID,
            "alias": _MANAGED_WRITE_ALIAS,
            "secret": _MANAGED_WRITE_SECRET,
        }
        assert inventory_module.read_private_json(pending_path) == (
            _managed_write_stage_record("pending")
        )
        assert complete_path.exists() is False
        observed_pending_before_post.append(True)

    client.before_post = observe_pending_before_post
    generated: list[int] = []

    def generate_secret(length: int) -> str:
        generated.append(length)
        return _MANAGED_WRITE_SECRET

    monkeypatch.setattr(
        api_integration_module.secrets, "token_urlsafe", generate_secret
    )

    actual = _managed_credential_write(lab)

    _, create_url = _managed_write_urls()
    assert actual == result
    assert generated == [32]
    assert observed_pending_before_post == [True]
    assert client.calls == [
        {
            "method": "POST",
            "url": create_url,
            "json": {
                "operation_id": _MANAGED_WRITE_OPERATION_ID,
                "alias": _MANAGED_WRITE_ALIAS,
                "secret": _MANAGED_WRITE_SECRET,
            },
            "headers": lab.headers,
        }
    ]
    pending = inventory_module.read_private_json(pending_path)
    complete = inventory_module.read_private_json(complete_path)
    assert pending == _managed_write_stage_record("pending")
    assert complete == _managed_write_stage_record("complete", result)
    assert _MANAGED_WRITE_SECRET not in json.dumps([pending, complete], sort_keys=True)


def test_cold_managed_write_unknown_original_never_resends(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    client = _ManagedWriteClient(
        get_responses=[_ManagedWriteResponse(503, {"detail": "unknown"})],
        post_responses=[_ManagedWriteResponse(503, {"detail": "unknown"})],
    )
    lab = _managed_write_lab(tmp_path, client)
    generated: list[int] = []
    monkeypatch.setattr(
        api_integration_module.secrets,
        "token_urlsafe",
        lambda length: generated.append(length) or _MANAGED_WRITE_SECRET,
    )

    with pytest.raises(
        ProductTargetError, match="cold_managed_credential_write_refused"
    ):
        _managed_credential_write(lab)

    pending_path, complete_path = _managed_write_stage_paths(lab.evidence_directory)
    pending = inventory_module.read_private_json(pending_path)
    assert pending == _managed_write_stage_record("pending")
    assert complete_path.exists() is False
    assert _MANAGED_WRITE_SECRET not in json.dumps(pending, sort_keys=True)

    with pytest.raises(
        ProductTargetError, match="cold_managed_credential_write_refused"
    ):
        _managed_credential_write(lab)

    original_url, create_url = _managed_write_urls()
    assert generated == [32]
    assert client.calls == [
        {
            "method": "POST",
            "url": create_url,
            "json": {
                "operation_id": _MANAGED_WRITE_OPERATION_ID,
                "alias": _MANAGED_WRITE_ALIAS,
                "secret": _MANAGED_WRITE_SECRET,
            },
            "headers": lab.headers,
        },
        {"method": "GET", "url": original_url, "headers": lab.headers},
    ]


def test_cold_managed_write_known_original_completes_and_reuses_receipt(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    result = _managed_write_payload()
    client = _ManagedWriteClient(
        get_responses=[
            _ManagedWriteResponse(200, result),
            _ManagedWriteResponse(200, result),
        ],
        post_responses=[_ManagedWriteResponse(503, {"detail": "unknown"})],
    )
    lab = _managed_write_lab(tmp_path, client)
    generated: list[int] = []
    monkeypatch.setattr(
        api_integration_module.secrets,
        "token_urlsafe",
        lambda length: generated.append(length) or _MANAGED_WRITE_SECRET,
    )

    with pytest.raises(
        ProductTargetError, match="cold_managed_credential_write_refused"
    ):
        _managed_credential_write(lab)
    resumed = _managed_credential_write(lab)
    reused = _managed_credential_write(lab)

    original_url, create_url = _managed_write_urls()
    assert resumed == result and reused == result
    assert generated == [32]
    assert client.calls == [
        {
            "method": "POST",
            "url": create_url,
            "json": {
                "operation_id": _MANAGED_WRITE_OPERATION_ID,
                "alias": _MANAGED_WRITE_ALIAS,
                "secret": _MANAGED_WRITE_SECRET,
            },
            "headers": lab.headers,
        },
        {"method": "GET", "url": original_url, "headers": lab.headers},
        {"method": "GET", "url": original_url, "headers": lab.headers},
    ]
    pending_path, complete_path = _managed_write_stage_paths(lab.evidence_directory)
    pending = inventory_module.read_private_json(pending_path)
    complete = inventory_module.read_private_json(complete_path)
    assert pending == _managed_write_stage_record("pending")
    assert complete == _managed_write_stage_record("complete", result)
    assert _MANAGED_WRITE_SECRET not in json.dumps([pending, complete], sort_keys=True)


def test_cold_managed_write_post_transport_loss_is_sanitized_and_resumes_original(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    result = _managed_write_payload()
    client = _ManagedWriteClient(
        get_responses=[_ManagedWriteResponse(200, result)],
        post_errors=[
            httpx.ReadTimeout(
                "lost response for managed write with " + _MANAGED_WRITE_SECRET
            )
        ],
    )
    lab = _managed_write_lab(tmp_path, client)
    generated: list[int] = []
    monkeypatch.setattr(
        api_integration_module.secrets,
        "token_urlsafe",
        lambda length: generated.append(length) or _MANAGED_WRITE_SECRET,
    )

    with pytest.raises(
        ProductTargetError, match="cold_managed_credential_write_refused"
    ) as failure:
        _managed_credential_write(lab)

    assert str(failure.value) == "cold_managed_credential_write_refused"
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True
    assert _MANAGED_WRITE_SECRET not in repr(failure.value)
    pending_path, complete_path = _managed_write_stage_paths(lab.evidence_directory)
    pending = inventory_module.read_private_json(pending_path)
    assert pending == _managed_write_stage_record("pending")
    assert complete_path.exists() is False

    recovered = _managed_credential_write(lab)

    original_url, create_url = _managed_write_urls()
    assert recovered == result
    assert generated == [32]
    assert [call["method"] for call in client.calls] == ["POST", "GET"]
    assert client.calls[0]["url"] == create_url
    assert client.calls[1] == {
        "method": "GET",
        "url": original_url,
        "headers": lab.headers,
    }
    pending = inventory_module.read_private_json(pending_path)
    complete = inventory_module.read_private_json(complete_path)
    assert pending == _managed_write_stage_record("pending")
    assert complete == _managed_write_stage_record("complete", result)
    assert _MANAGED_WRITE_SECRET not in json.dumps([pending, complete], sort_keys=True)


def test_cold_managed_write_get_transport_error_is_sanitized_without_mutation(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    client = _ManagedWriteClient(
        get_errors=[
            httpx.ReadTimeout("original lookup failed with " + _MANAGED_WRITE_SECRET)
        ]
    )
    lab = _managed_write_lab(tmp_path, client)
    pending_path, complete_path = _managed_write_stage_paths(lab.evidence_directory)
    inventory_module.write_private_json(
        pending_path, _managed_write_stage_record("pending")
    )
    pending_before = inventory_module.read_private_json(pending_path)
    monkeypatch.setattr(
        api_integration_module.secrets,
        "token_urlsafe",
        lambda _: pytest.fail("read-only lookup generated a new secret"),
    )

    with pytest.raises(
        ProductTargetError, match="cold_managed_credential_write_refused"
    ) as failure:
        _managed_credential_write(lab)

    assert str(failure.value) == "cold_managed_credential_write_refused"
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True
    assert _MANAGED_WRITE_SECRET not in repr(failure.value)
    assert inventory_module.read_private_json(pending_path) == pending_before
    assert complete_path.exists() is False
    original_url, _ = _managed_write_urls()
    assert client.calls == [
        {"method": "GET", "url": original_url, "headers": lab.headers}
    ]


@pytest.mark.parametrize(
    "mismatch",
    (
        "wrong_operation",
        "foreign_workspace",
        "unexpected_secret_field",
        "pending_status",
        "wrong_source",
        "different_completed_credential_id",
    ),
)
def test_cold_managed_write_refuses_noncanonical_original_summary(
    tmp_path, no_database_connections, monkeypatch, mismatch: str
) -> None:
    result = _managed_write_payload()
    response_payload = _managed_write_payload()
    if mismatch == "wrong_operation":
        response_payload["operation_id"] = "e213f781-3c8b-4938-865d-ea9caac9d60e"
    elif mismatch == "foreign_workspace":
        response_payload["workspace_id"] = "12cf7d12-8a34-4d21-85e3-81fe153ffb7e"
    elif mismatch == "unexpected_secret_field":
        response_payload["secret"] = "inert-secret-field"
    elif mismatch == "pending_status":
        response_payload["status"] = "pending"
    elif mismatch == "wrong_source":
        response_payload["credential"]["source"] = "env"
    else:
        response_payload["credential"]["credential_id"] = (
            "f0d3fd79-5a09-43ea-a8b8-d4799f249258"
        )
    client = _ManagedWriteClient(
        get_responses=[_ManagedWriteResponse(200, response_payload)]
    )
    lab = _managed_write_lab(tmp_path, client)
    pending_path, complete_path = _managed_write_stage_paths(lab.evidence_directory)
    inventory_module.write_private_json(
        pending_path, _managed_write_stage_record("pending")
    )
    complete_before = None
    if mismatch == "different_completed_credential_id":
        complete_before = _managed_write_stage_record("complete", result)
        inventory_module.write_private_json(complete_path, complete_before)
    pending_before = inventory_module.read_private_json(pending_path)
    monkeypatch.setattr(
        api_integration_module.secrets,
        "token_urlsafe",
        lambda _: pytest.fail("read-only recovery generated a new secret"),
    )

    with pytest.raises(
        ProductTargetError, match="cold_managed_credential_write_refused"
    ):
        _managed_credential_write(lab)

    original_url, _ = _managed_write_urls()
    assert client.calls == [
        {"method": "GET", "url": original_url, "headers": lab.headers}
    ]
    assert inventory_module.read_private_json(pending_path) == pending_before
    if complete_before is None:
        assert complete_path.exists() is False
    else:
        assert inventory_module.read_private_json(complete_path) == complete_before


def _stub_ego_browser_run(
    monkeypatch, *, returncode: int = 0, stdout: str = "", stderr: str = ""
):
    executable = "/private/tmp/inert-ego-browser"
    which_calls = []
    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append((args, kwargs))
        return desktop_module.subprocess.CompletedProcess(
            args, returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(
        desktop_module.shutil,
        "which",
        lambda name: which_calls.append(name) or executable,
    )
    monkeypatch.setattr(desktop_module.subprocess, "run", fake_run)
    return executable, which_calls, run_calls


def _assert_ego_browser_invocation(executable, which_calls, run_calls, script):
    assert which_calls == ["ego-browser"]
    assert run_calls == [
        (
            [executable, "nodejs", "-e", script],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 300,
            },
        )
    ]


def test_ego_result_accepts_stderr_console_protocol(
    no_database_connections, monkeypatch
) -> None:
    script = "inert browser script"
    executable, which_calls, run_calls = _stub_ego_browser_run(
        monkeypatch,
        stdout="benign CLI stdout\n",
        stderr='diagnostic\nFRESH_RESULT:{"space_id":2,"page_label":"p1"}\n',
    )

    result = desktop_module.run_ego_browser(script)

    assert result == {"space_id": 2, "page_label": "p1"}
    _assert_ego_browser_invocation(executable, which_calls, run_calls, script)


def test_ego_result_accepts_stdout_protocol(
    no_database_connections, monkeypatch
) -> None:
    script = "inert browser script"
    executable, which_calls, run_calls = _stub_ego_browser_run(
        monkeypatch,
        stdout='FRESH_RESULT:{"space_id":2,"page_label":"p1"}\n',
        stderr="benign CLI diagnostic\n",
    )

    result = desktop_module.run_ego_browser(script)

    assert result == {"space_id": 2, "page_label": "p1"}
    _assert_ego_browser_invocation(executable, which_calls, run_calls, script)


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    (
        (
            'FRESH_RESULT:{"space_id":2,"page_label":"p1"}\n',
            'FRESH_RESULT:{"space_id":2,"page_label":"p1"}\n',
        ),
        (
            'FRESH_RESULT:{"space_id":2,"page_label":"p1"}\n',
            'FRESH_RESULT:{"space_id":3,"page_label":"p1"}\n',
        ),
    ),
    ids=("same-result-twice", "conflicting-results"),
)
def test_ego_result_refuses_multiple_protocol_markers(
    no_database_connections, monkeypatch, stdout: str, stderr: str
) -> None:
    script = "inert browser script"
    executable, which_calls, run_calls = _stub_ego_browser_run(
        monkeypatch, stdout=stdout, stderr=stderr
    )

    with pytest.raises(
        ProductTargetError, match="fresh_browser_result_refused"
    ) as failure:
        desktop_module.run_ego_browser(script)

    assert str(failure.value) == "fresh_browser_result_refused"
    _assert_ego_browser_invocation(executable, which_calls, run_calls, script)


@pytest.mark.parametrize(
    ("stdout", "stderr", "diagnostic"),
    (
        (
            "ordinary CLI diagnostic sk-" + "x" * 20 + "\n",
            "another diagnostic\n",
            "sk-" + "x" * 20,
        ),
        (
            'FRESH_RESULT:{"diagnostic":"sk-' + "x" * 20 + "\n",
            "",
            "sk-" + "x" * 20,
        ),
        (
            "",
            'FRESH_RESULT:["sk-' + "x" * 20 + '"]\n',
            "sk-" + "x" * 20,
        ),
    ),
    ids=("missing-marker", "malformed-json", "non-object-json"),
)
def test_ego_result_refuses_invalid_protocol(
    no_database_connections,
    monkeypatch,
    stdout: str,
    stderr: str,
    diagnostic: str,
) -> None:
    script = "inert browser script"
    executable, which_calls, run_calls = _stub_ego_browser_run(
        monkeypatch, stdout=stdout, stderr=stderr
    )

    with pytest.raises(
        ProductTargetError, match="fresh_browser_result_refused"
    ) as failure:
        desktop_module.run_ego_browser(script)

    assert str(failure.value) == "fresh_browser_result_refused"
    assert diagnostic not in str(failure.value)
    _assert_ego_browser_invocation(executable, which_calls, run_calls, script)


def test_ego_result_refuses_nonzero_exit_with_marker(
    no_database_connections, monkeypatch
) -> None:
    script = "inert browser script"
    executable, which_calls, run_calls = _stub_ego_browser_run(
        monkeypatch,
        returncode=1,
        stderr='FRESH_RESULT:{"space_id":2,"page_label":"p1"}\n',
    )

    with pytest.raises(
        ProductTargetError, match="fresh_browser_run_refused"
    ) as failure:
        desktop_module.run_ego_browser(script)

    assert str(failure.value) == "fresh_browser_run_refused"
    _assert_ego_browser_invocation(executable, which_calls, run_calls, script)


def _synthetic_bootstrap_issue(token: str) -> BootstrapIssue:
    return BootstrapIssue(
        uuid4(),
        SecretStr(token),
        datetime(2030, 1, 1, tzinfo=UTC),
    )


def test_cli_bootstrap_uses_private_default_and_registers_owner(
    tmp_path, no_database_connections, monkeypatch, capsys
) -> None:
    git_root = tmp_path / "synthetic-repository"
    (git_root / ".git").mkdir(parents=True)
    private_default = tmp_path / "private-default"
    private_default.mkdir(mode=0o700)
    os.chmod(private_default, 0o700)
    monkeypatch.setattr(
        local_session_module.tempfile,
        "mkdtemp",
        lambda *, prefix: str(private_default),
    )

    token = "synthetic-bootstrap-token"
    issue = _synthetic_bootstrap_issue(token)
    selected_settings = object()
    outputs: list[Path] = []

    def fake_cli(args, *, settings):
        assert args == ["session", "bootstrap"]
        assert settings is selected_settings
        output = write_bootstrap_file(issue, output=None)
        outputs.append(output)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "expires_at": issue.expires_at.isoformat(),
                }
            )
        )
        return 0

    monkeypatch.setattr(desktop_module, "product_cli_main", fake_cli)
    owner = SimpleNamespace(bootstrap_export=None, owned={})
    try:
        result = desktop_module.create_cli_bootstrap(
            selected_settings, resource_owner=owner
        )
        export = owner.bootstrap_export
        assert export.location == outputs[0]
        assert export.location.parent == private_default
        assert not export.location.is_relative_to(git_root)
        assert stat.S_IMODE(export.location.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(export.location.stat().st_mode) == 0o600
        assert read_bootstrap_file(export.location).get_secret_value() == token
        assert result == {
            "output": str(export.location),
            "expires_at": issue.expires_at.isoformat(),
        }
        assert owner.owned["bootstrap_export"] == {
            "path": str(export.location),
            "file_removed": False,
            "closed": False,
        }
        assert token not in repr(result)
        captured = capsys.readouterr()
        assert token not in captured.out
        assert token not in captured.err
    finally:
        if owner.bootstrap_export is not None:
            assert owner.bootstrap_export.close() is True


@pytest.mark.parametrize(
    "unsafe_kind",
    ("extra-field", "repository-file"),
    ids=("extra-field", "repository-file"),
)
def test_cli_bootstrap_refuses_unsafe_result_without_exposing_stream(
    tmp_path, no_database_connections, monkeypatch, capsys, unsafe_kind: str
) -> None:
    git_root = tmp_path / "synthetic-repository"
    git_directory = git_root / ".git"
    git_directory.mkdir(parents=True)
    preserved = git_directory / "bootstrap.json"
    original = b"preserve-repository-file"
    preserved.write_bytes(original)
    token = "synthetic-unsafe-token"
    output = (
        tmp_path / "unused-output.json" if unsafe_kind == "extra-field" else preserved
    )
    selected_settings = object()

    def fake_cli(args, *, settings):
        assert args == ["session", "bootstrap"]
        assert settings is selected_settings
        value = {
            "output": str(output),
            "expires_at": "2030-01-01T00:00:00+00:00",
        }
        if unsafe_kind == "extra-field":
            value["token"] = token
        print(json.dumps(value))
        return 0

    monkeypatch.setattr(desktop_module, "product_cli_main", fake_cli)
    owner = SimpleNamespace(bootstrap_export=None, owned={})
    with pytest.raises(
        ProductTargetError, match="fresh_cli_bootstrap_refused"
    ) as failure:
        desktop_module.create_cli_bootstrap(selected_settings, resource_owner=owner)

    assert str(failure.value) == "fresh_cli_bootstrap_refused"
    assert token not in str(failure.value)
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert owner.bootstrap_export is None
    assert owner.owned == {}
    assert preserved.read_bytes() == original


def test_bootstrap_export_close_is_owned_and_idempotent(
    tmp_path, no_database_connections
) -> None:
    parent = tmp_path / "owned-private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    location = write_bootstrap_file(
        _synthetic_bootstrap_issue("synthetic-owned-token"),
        output=parent / "bootstrap.json",
    )
    sibling = tmp_path / "outside-sibling.json"
    sibling.write_bytes(b"keep sibling")
    sibling_before = sibling.read_bytes()
    owner = desktop_module.OwnedBootstrapExport.capture(location)

    assert owner.close() is True
    assert owner.close() is True
    assert owner._file_removed is True
    assert owner._closed is True
    assert location.exists() is False
    assert parent.exists() is False
    assert sibling.read_bytes() == sibling_before


def test_bootstrap_export_close_refuses_replaced_file(
    tmp_path, no_database_connections
) -> None:
    parent = tmp_path / "owned-private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    location = write_bootstrap_file(
        _synthetic_bootstrap_issue("synthetic-original-token"),
        output=parent / "bootstrap.json",
    )
    original = location.read_bytes()
    retained = tmp_path / "retained-original.json"
    owner = desktop_module.OwnedBootstrapExport.capture(location)
    location.replace(retained)
    replacement = b"do not remove replacement"
    location.write_bytes(replacement)
    os.chmod(location, 0o600)

    try:
        assert stat.S_IMODE(location.stat().st_mode) == 0o600
        assert owner.close() is False
        assert location.read_bytes() == replacement
        assert retained.read_bytes() == original
    finally:
        location.unlink(missing_ok=True)
        retained.unlink(missing_ok=True)
        parent.rmdir()


def test_bootstrap_export_close_refuses_changed_parent(
    tmp_path, no_database_connections
) -> None:
    parent = tmp_path / "owned-private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    location = write_bootstrap_file(
        _synthetic_bootstrap_issue("synthetic-parent-token"),
        output=parent / "bootstrap.json",
    )
    original = location.read_bytes()
    owner = desktop_module.OwnedBootstrapExport.capture(location)

    os.chmod(parent, 0o755)
    try:
        assert owner.close() is False
        assert location.read_bytes() == original
    finally:
        os.chmod(parent, 0o700)
        assert owner.close() is True


def test_browser_authorization_uses_actual_export_not_checkpoint_hint(
    tmp_path, no_database_connections, monkeypatch
) -> None:
    workspace_id = uuid4()
    actual_export = tmp_path / "secure" / "bootstrap.json"
    legacy_hint = tmp_path / "legacy-checkpoint-hint.json"
    expires_at = "2030-01-01T00:00:00+00:00"
    lab = SimpleNamespace(
        workspace_id=workspace_id,
        settings=object(),
        bootstrap_file=legacy_hint,
    )
    stage_result = {
        "workspace_id": str(workspace_id),
        "initial_lifecycle": "ready",
        "initial_execution_ready": False,
        "pre_registration_provider": "unavailable",
        "authorized_runtime_count": 0,
        "bootstrap_expires_at": expires_at,
    }
    stage_calls = []
    monkeypatch.setattr(
        desktop_integration_module,
        "create_cli_bootstrap",
        lambda settings, *, resource_owner: {
            "output": str(actual_export),
            "expires_at": expires_at,
        },
    )

    def recording_run_stage(actual_lab, body, *, task_space_id, variables):
        stage_calls.append((actual_lab, body, task_space_id, variables))
        return stage_result

    monkeypatch.setattr(desktop_integration_module, "_run_stage", recording_run_stage)

    result = desktop_integration_module._authorize_browser(lab, 2)

    assert result == stage_result
    assert stage_calls[0][0] is lab
    assert stage_calls[0][2] == 2
    assert stage_calls[0][3] == {
        "bootstrap_file": str(actual_export),
        "bootstrap_expires_at": expires_at,
    }
    assert stage_calls[0][3]["bootstrap_file"] != str(legacy_hint)

    stage_result = {**stage_result, "bootstrap_expires_at": "mismatched-expiry"}
    with pytest.raises(ProductTargetError, match="fresh_browser_result_refused"):
        desktop_integration_module._authorize_browser(lab, 2)


def test_fresh_lab_close_includes_bootstrap_cleanup(
    no_database_connections,
) -> None:
    class FakeBootstrapExport:
        def __init__(self):
            self.location = Path("/private/tmp/bootstrap.json")
            self._file_removed = False
            self._closed = False
            self.results = iter((False, True))

        def close(self):
            result = next(self.results)
            if result:
                self._file_removed = True
                self._closed = True
            return result

    guard = SimpleNamespace(active=True, release=lambda: pytest.fail("guard released"))
    native = SimpleNamespace(calls=[])
    export = FakeBootstrapExport()
    lab = desktop_module.FreshDesktopLab(
        admission=None,
        guards=[guard],
        settings=None,
        runtime=None,
        native=native,
        principal=None,
        secrets=None,
        app=None,
        backend=None,
        vite=None,
        bootstrap_file=Path("/private/tmp/legacy-hint.json"),
        vite_root=Path("/private/tmp/web"),
        bootstrap_export=export,
    )

    assert lab.close() is False
    assert lab.owned["bootstrap_export"] == {
        "path": str(export.location),
        "file_removed": False,
        "closed": False,
    }
    assert lab.close() is True
    assert lab.owned["bootstrap_export"] == {
        "path": str(export.location),
        "file_removed": True,
        "closed": True,
    }
    assert guard.active is True
    assert native.calls == []
