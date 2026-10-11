from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sidera_agents.contracts import ProfileRef
from sidera_control_plane.api.schemas import (
    PlanningProfileResponse,
    PlanningRequestDetailResponse,
)
from sidera_control_plane.persistence.models import (
    PlanningAgentLinkModel,
    PlanningClarificationModel,
    PlanningProfileModel,
    PlanningRequestModel,
    PlanningRunModel,
    ProjectionChangeModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.planning_query_store import (
    SqlAlchemyPlanningQueryStore,
)
from sidera_control_plane.persistence.planning_repository import (
    _capability_to_mapping,
    _profile_policy,
    _snapshot_to_mapping,
)
from sqlalchemy import Select

from tests.unit.test_planning_profiles import planner_capabilities, planning_profile

WORKSPACE = UUID("73000000-0000-0000-0000-000000000001")
POLICY = UUID("73000000-0000-0000-0000-000000000002")
PLANNER = UUID("73000000-0000-0000-0000-000000000003")
REQUEST = UUID("73000000-0000-0000-0000-000000000004")
RUN = UUID("73000000-0000-0000-0000-000000000005")
OPERATION = UUID("73000000-0000-0000-0000-000000000006")
NATIVE_RUN = UUID("73000000-0000-0000-0000-000000000007")
RESPONSE = UUID("73000000-0000-0000-0000-000000000008")
AUTHORITY = UUID("73000000-0000-0000-0000-000000000009")
CLARIFICATION = UUID("73000000-0000-0000-0000-000000000010")
OTHER_PLANNER = UUID("73000000-0000-0000-0000-000000000011")
OTHER_WORKSPACE = UUID("73000000-0000-0000-0000-000000000012")
OTHER_POLICY = UUID("73000000-0000-0000-0000-000000000013")
MODULE = UUID("73000000-0000-0000-0000-000000000014")
NOW = datetime(2026, 10, 10, tzinfo=UTC)
MISSING = object()


@dataclass
class _ReadOnlyRows:
    rows: tuple

    def all(self):
        return list(self.rows)

    def one_or_none(self):
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None


@dataclass
class _ReadOnlySession:
    request: PlanningRequestModel | None = None
    profile: PlanningProfileModel | None = None
    runs: tuple[PlanningRunModel, ...] = ()
    clarifications: tuple[PlanningClarificationModel, ...] = ()
    links: tuple[PlanningAgentLinkModel, ...] = ()
    provider: str | None = None
    capabilities: dict | None = None

    def scalar(self, statement):
        assert isinstance(statement, Select)
        entity = statement.column_descriptions[0]["entity"]
        if entity is PlanningRequestModel:
            return self.request
        if entity is WorkspaceProjectionCursorModel:
            return 41
        if entity is ProjectionChangeModel:
            return 43
        raise AssertionError(f"Unexpected scalar query: {statement}")

    def execute(self, statement):
        assert isinstance(statement, Select)
        assert statement.column_descriptions[0]["entity"] is PlanningProfileModel
        return _ReadOnlyRows(
            ()
            if self.profile is None
            else ((self.profile, self.provider, self.capabilities),)
        )

    def scalars(self, statement):
        assert isinstance(statement, Select)
        rows = {
            PlanningRunModel: self.runs,
            PlanningClarificationModel: self.clarifications,
            PlanningAgentLinkModel: self.links,
        }
        return _ReadOnlyRows(rows[statement.column_descriptions[0]["entity"]])


@dataclass
class _ReadOnlyDatabase:
    reader: _ReadOnlySession

    def session(self):
        return nullcontext(self.reader)


def _native_session() -> _ReadOnlySession:
    policy = planning_profile(
        id=POLICY,
        workspace_id=WORKSPACE,
        execution_mode="native_profile",
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_secret_refs=(),
        system_policy={
            "approval": {"required": True},
            "environment": {"PRIVATE_SETTING": "private-saved-policy-value"},
        },
        planner_agent_ref=ProfileRef(
            workspace_id=WORKSPACE, profile_id=PLANNER, revision=3
        ),
    )
    snapshot = _snapshot_to_mapping(policy.snapshot())
    live = PlanningProfileModel(
        id=POLICY,
        workspace_id=WORKSPACE,
        profile_key="live-planning-policy",
        version=3,
        display_name="Current directory name",
        execution_mode="native_profile",
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_interface_version="sidera.planner.v1",
        planner_agent_ref={
            "workspace_id": str(WORKSPACE),
            "profile_id": str(PLANNER),
            "revision": 2,
        },
        status="active",
        user_selectable=True,
        policy=_profile_policy(policy),
        created_at=NOW,
    )
    request = PlanningRequestModel(
        id=REQUEST,
        workspace_id=WORKSPACE,
        kind="create",
        status="failed",
        planning_profile_id=POLICY,
        planning_profile_version=3,
        profile_snapshot=snapshot,
        current_brief_revision=1,
        current_proposal_revision=None,
        current_proposal_content_hash=None,
        source_type="test",
        source_ref="saved-planner-query-projection",
        actor="operator",
        correlation_id=OPERATION,
        version=5,
        created_at=NOW,
        updated_at=NOW,
    )
    run = PlanningRunModel(
        id=RUN,
        workspace_id=WORKSPACE,
        planning_request_id=REQUEST,
        brief_revision=1,
        planning_profile_id=POLICY,
        planning_profile_version=3,
        execution_mode="native_profile",
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_agent_ref=deepcopy(snapshot["planner_agent_ref"]),
        capability_snapshot=None,
        attempt=1,
        operation_id=OPERATION,
        status="failed",
        result_kind="failure",
        usage={"input_tokens": 12, "output_tokens": 7},
        provider_ref="agent-run",
        error_code="invalid_output",
        error_message="Required title is absent",
        diagnostic_refs=[],
        queued_at=NOW,
        started_at=NOW,
        finished_at=NOW,
        version=4,
        created_at=NOW,
        updated_at=NOW,
    )
    clarification = PlanningClarificationModel(
        id=CLARIFICATION,
        workspace_id=WORKSPACE,
        planning_request_id=REQUEST,
        planning_run_id=RUN,
        sequence=1,
        question="Which output?",
        rationale="Confirm the intended output",
        answer_type="text",
        options=[],
        blocking=True,
        answer=None,
        answered_by=None,
        answered_at=None,
        answer_brief_revision=None,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    link = PlanningAgentLinkModel(
        workspace_id=WORKSPACE,
        planning_run_id=RUN,
        authority_id=AUTHORITY,
        start_operation_id=OPERATION,
        start_request={
            "operation_id": str(OPERATION),
            "grant_id": str(AUTHORITY),
        },
        request_sha256="a" * 64,
        status="closed",
        run_id=NATIVE_RUN,
        start_receipt=None,
        event_cursor=9,
        response_id=RESPONSE,
        closed=True,
        reason_code=None,
    )
    return _ReadOnlySession(
        request=request,
        profile=live,
        runs=(run,),
        clarifications=(clarification,),
        links=(link,),
    )


def _assert_public_fields_only(value: object) -> None:
    if isinstance(value, dict):
        assert not value.keys() & {
            "policy",
            "system_policy",
            "budget",
            "retry_policy",
            "planner_secret_refs",
            "secret_refs",
            "environment",
            "profile_snapshot",
            "prompt_policy_version",
            "allowed_capabilities",
            "adapter_preferences",
            "adapter_forbidden",
        }
        for child in value.values():
            _assert_public_fields_only(child)
    elif isinstance(value, list):
        for child in value:
            _assert_public_fields_only(child)
    elif isinstance(value, str):
        assert "secret://" not in value
        assert "private-saved-policy-value" not in value


def test_native_detail_projects_saved_planner_revision_through_response() -> None:
    reader = _native_session()
    original_snapshot = deepcopy(reader.request.profile_snapshot)
    store = SqlAlchemyPlanningQueryStore(_ReadOnlyDatabase(reader))

    detail = store.get_request_detail(WORKSPACE, REQUEST)

    assert detail is not None
    body = PlanningRequestDetailResponse.from_detail_view(detail).model_dump(
        mode="json"
    )
    assert body["profile"]["planner_agent_ref"] == {
        "workspace_id": str(WORKSPACE),
        "profile_id": str(PLANNER),
        "revision": 3,
    }
    assert (
        body["active_run"]["planner_agent_ref"] == body["profile"]["planner_agent_ref"]
    )
    assert body["profile"]["execution_mode"] == "native_profile"
    assert body["profile"]["capabilities"] is None
    assert body["profile"]["planner_provider"] is None
    assert body["profile"]["planner_module_instance_id"] is None
    assert body["profile"]["planner_module_config_version"] is None
    assert body["active_run"]["native_run"]["closed"] is True
    assert body["pending_clarifications"][0]["id"] == str(CLARIFICATION)
    assert body["last_change_cursor"] == 43
    assert reader.profile.planner_agent_ref["revision"] == 2
    assert reader.request.profile_snapshot == original_snapshot
    _assert_public_fields_only(body)


def test_directory_keeps_live_default_planner_revision() -> None:
    live = _native_session().profile
    store = SqlAlchemyPlanningQueryStore(
        _ReadOnlyDatabase(_ReadOnlySession(profile=live))
    )

    page = store.list_profiles(WORKSPACE, limit=10, cursor=None)

    assert len(page.items) == 1
    body = PlanningProfileResponse.from_view(page.items[0]).model_dump(mode="json")
    assert body["planner_agent_ref"] == {
        "workspace_id": str(WORKSPACE),
        "profile_id": str(PLANNER),
        "revision": 2,
    }
    assert body["capabilities"] is None
    assert page.next_cursor is None
    _assert_public_fields_only(body)


def test_native_detail_keeps_a_different_saved_planner_profile_id() -> None:
    reader = _native_session()
    reader.request.profile_snapshot["planner_agent_ref"]["profile_id"] = str(
        OTHER_PLANNER
    )
    reader.runs[0].planner_agent_ref["profile_id"] = str(OTHER_PLANNER)
    original_snapshot = deepcopy(reader.request.profile_snapshot)
    store = SqlAlchemyPlanningQueryStore(_ReadOnlyDatabase(reader))

    detail = store.get_request_detail(WORKSPACE, REQUEST)

    assert detail is not None
    body = PlanningRequestDetailResponse.from_detail_view(detail).model_dump(
        mode="json"
    )
    assert body["profile"]["planner_agent_ref"] == {
        "workspace_id": str(WORKSPACE),
        "profile_id": str(OTHER_PLANNER),
        "revision": 3,
    }
    assert (
        body["active_run"]["planner_agent_ref"] == body["profile"]["planner_agent_ref"]
    )
    assert reader.profile.planner_agent_ref == {
        "workspace_id": str(WORKSPACE),
        "profile_id": str(PLANNER),
        "revision": 2,
    }
    assert reader.request.profile_snapshot == original_snapshot
    _assert_public_fields_only(body)


def test_native_detail_uses_saved_execution_fields_and_live_metadata() -> None:
    reader = _native_session()
    reader.profile.execution_mode = "legacy"
    reader.profile.planner_agent_ref = None
    reader.profile.planner_module_instance_id = MODULE
    reader.profile.planner_module_config_version = 9
    reader.profile.planner_interface_version = "sidera.planner.v2"
    reader.profile.status = "disabled"
    reader.profile.user_selectable = False
    reader.provider = "legacy-provider"
    reader.capabilities = _capability_to_mapping(
        planner_capabilities(interface_version="sidera.planner.v2")
    )
    store = SqlAlchemyPlanningQueryStore(_ReadOnlyDatabase(reader))

    detail = store.get_request_detail(WORKSPACE, REQUEST)

    assert detail is not None
    body = PlanningRequestDetailResponse.from_detail_view(detail).model_dump(
        mode="json"
    )
    assert body["profile"] == {
        "id": str(POLICY),
        "workspace_id": str(WORKSPACE),
        "profile_key": "live-planning-policy",
        "version": 3,
        "display_name": "Current directory name",
        "planner_provider": None,
        "planner_module_instance_id": None,
        "planner_module_config_version": None,
        "planner_interface_version": "sidera.planner.v1",
        "status": "disabled",
        "user_selectable": False,
        "capabilities": None,
        "created_at": "2026-10-10T00:00:00Z",
        "execution_mode": "native_profile",
        "planner_agent_ref": {
            "workspace_id": str(WORKSPACE),
            "profile_id": str(PLANNER),
            "revision": 3,
        },
    }
    assert reader.profile.execution_mode == "legacy"
    assert reader.profile.planner_module_config_version == 9
    assert reader.profile.planner_interface_version == "sidera.planner.v2"
    _assert_public_fields_only(body)


@pytest.mark.parametrize("explicit_execution_mode", (True, False))
def test_legacy_detail_and_directory_remain_live(explicit_execution_mode: bool) -> None:
    reader = _native_session()
    policy = planning_profile(
        id=POLICY, workspace_id=WORKSPACE, planner_module_instance_id=MODULE
    )
    snapshot = _snapshot_to_mapping(policy.snapshot(planner_capabilities()))
    if not explicit_execution_mode:
        del snapshot["execution_mode"]
    reader.request.profile_snapshot = snapshot
    reader.profile.execution_mode = "legacy"
    reader.profile.planner_agent_ref = None
    reader.profile.planner_module_instance_id = MODULE
    reader.profile.planner_module_config_version = 7
    reader.profile.policy = _profile_policy(policy)
    reader.provider = "legacy-provider"
    reader.capabilities = deepcopy(snapshot["planner_capabilities"])
    reader.runs[0].execution_mode = "legacy"
    reader.runs[0].planner_agent_ref = None
    reader.runs[0].planner_module_instance_id = MODULE
    reader.runs[0].planner_module_config_version = 7
    reader.runs[0].capability_snapshot = deepcopy(snapshot["planner_capabilities"])
    reader.runs[0].provider_ref = "legacy-run"
    reader.links = ()
    original_snapshot = deepcopy(snapshot)
    store = SqlAlchemyPlanningQueryStore(_ReadOnlyDatabase(reader))

    detail = store.get_request_detail(WORKSPACE, REQUEST)
    directory = SqlAlchemyPlanningQueryStore(
        _ReadOnlyDatabase(
            _ReadOnlySession(
                profile=reader.profile,
                provider=reader.provider,
                capabilities=reader.capabilities,
            )
        )
    ).list_profiles(WORKSPACE, limit=10, cursor=None)

    assert detail is not None
    body = PlanningRequestDetailResponse.from_detail_view(detail).model_dump(
        mode="json"
    )
    expected_profile = {
        "id": str(POLICY),
        "workspace_id": str(WORKSPACE),
        "profile_key": "live-planning-policy",
        "version": 3,
        "display_name": "Current directory name",
        "planner_provider": "legacy-provider",
        "planner_module_instance_id": str(MODULE),
        "planner_module_config_version": 7,
        "planner_interface_version": "sidera.planner.v1",
        "status": "active",
        "user_selectable": True,
        "capabilities": {
            "structured_output": "json_schema",
            "clarification": True,
            "idempotency": "operation-id",
            "cancellation": "run",
            "usage_reporting": "tokens",
            "max_context_bytes": 128_000,
            "stability": "stable",
            "interface_version": "sidera.planner.v1",
            "proposal_schema_versions": [1, 2],
            "context_package_versions": [],
        },
        "created_at": "2026-10-10T00:00:00Z",
        "execution_mode": "legacy",
        "planner_agent_ref": None,
    }
    assert body["profile"] == expected_profile
    directory_body = PlanningProfileResponse.from_view(directory.items[0]).model_dump(
        mode="json"
    )
    assert directory_body == expected_profile
    assert body["active_run"]["execution_mode"] == "legacy"
    assert body["active_run"]["planner_agent_ref"] is None
    assert body["active_run"]["native_run"] is None
    assert reader.request.profile_snapshot == original_snapshot
    _assert_public_fields_only(body)
    _assert_public_fields_only(directory_body)


def test_missing_live_profile_keeps_request_readable_without_profile() -> None:
    reader = _native_session()
    reader.profile = None
    store = SqlAlchemyPlanningQueryStore(_ReadOnlyDatabase(reader))

    detail = store.get_request_detail(WORKSPACE, REQUEST)

    assert detail is not None
    body = PlanningRequestDetailResponse.from_detail_view(detail).model_dump(
        mode="json"
    )
    assert body["id"] == str(REQUEST)
    assert body["profile"] is None
    assert body["profile_key"] is None
    assert body["active_run"]["id"] == str(RUN)
    _assert_public_fields_only(body)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("planner_agent_ref", MISSING, id="missing-pin"),
        pytest.param("planner_agent_ref", None, id="null-pin"),
        pytest.param("planner_agent_ref", "not-a-pin", id="malformed-pin"),
        pytest.param(
            "planner_agent_ref",
            {"workspace_id": str(WORKSPACE), "profile_id": str(PLANNER)},
            id="missing-pin-revision",
        ),
        pytest.param(
            "planner_agent_ref",
            {
                "workspace_id": str(WORKSPACE),
                "profile_id": str(PLANNER),
                "revision": 0,
            },
            id="invalid-pin-revision",
        ),
        pytest.param(
            "planner_agent_ref",
            {
                "workspace_id": str(OTHER_WORKSPACE),
                "profile_id": str(PLANNER),
                "revision": 3,
            },
            id="cross-workspace-pin",
        ),
        pytest.param("profile_id", "not-a-uuid", id="malformed-snapshot-profile"),
        pytest.param("profile_id", str(OTHER_POLICY), id="snapshot-policy-mismatch"),
        pytest.param("profile_version", 4, id="snapshot-policy-version-mismatch"),
        pytest.param("budget", None, id="malformed-snapshot-budget"),
        pytest.param("budget", MISSING, id="missing-snapshot-budget"),
        pytest.param(
            "planner_module_instance_id", str(MODULE), id="native-module-binding"
        ),
        pytest.param(
            "planner_secret_refs", ["secret://private/planner"], id="native-secret-refs"
        ),
    ],
)
def test_invalid_native_snapshot_has_no_live_default_fallback(
    field: str, value: object
) -> None:
    reader = _native_session()
    if value is MISSING:
        del reader.request.profile_snapshot[field]
    else:
        reader.request.profile_snapshot[field] = deepcopy(value)
    original_snapshot = deepcopy(reader.request.profile_snapshot)
    store = SqlAlchemyPlanningQueryStore(_ReadOnlyDatabase(reader))

    detail = store.get_request_detail(WORKSPACE, REQUEST)

    assert detail is not None
    body = PlanningRequestDetailResponse.from_detail_view(detail).model_dump(
        mode="json"
    )
    assert body["profile"] is None
    assert body["id"] == str(REQUEST)
    assert body["status"] == "failed"
    assert body["version"] == 5
    assert body["active_run"]["id"] == str(RUN)
    assert body["active_run"]["version"] == 4
    assert reader.profile.planner_agent_ref["revision"] == 2
    assert reader.request.profile_snapshot == original_snapshot
    _assert_public_fields_only(body)


def test_snapshot_from_another_workspace_has_no_live_default_fallback() -> None:
    reader = _native_session()
    reader.request.profile_snapshot["workspace_id"] = str(OTHER_WORKSPACE)
    reader.request.profile_snapshot["planner_agent_ref"]["workspace_id"] = str(
        OTHER_WORKSPACE
    )
    store = SqlAlchemyPlanningQueryStore(_ReadOnlyDatabase(reader))

    detail = store.get_request_detail(WORKSPACE, REQUEST)

    assert detail is not None
    body = PlanningRequestDetailResponse.from_detail_view(detail).model_dump(
        mode="json"
    )
    assert body["profile"] is None
    assert body["workspace_id"] == str(WORKSPACE)
    assert body["active_run"]["planner_agent_ref"]["workspace_id"] == str(WORKSPACE)
    _assert_public_fields_only(body)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("workspace_id", OTHER_WORKSPACE, id="live-workspace-mismatch"),
        pytest.param("id", OTHER_POLICY, id="live-policy-mismatch"),
        pytest.param("version", 4, id="live-policy-version-mismatch"),
    ],
)
def test_mismatched_live_policy_has_no_native_default_fallback(
    field: str, value: object
) -> None:
    reader = _native_session()
    setattr(reader.profile, field, value)
    store = SqlAlchemyPlanningQueryStore(_ReadOnlyDatabase(reader))

    detail = store.get_request_detail(WORKSPACE, REQUEST)

    assert detail is not None
    body = PlanningRequestDetailResponse.from_detail_view(detail).model_dump(
        mode="json"
    )
    assert body["profile"] is None
    assert body["planning_profile_id"] == str(POLICY)
    assert body["planning_profile_version"] == 3
    _assert_public_fields_only(body)
