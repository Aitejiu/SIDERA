from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import (
    CapabilityResourceDraft,
    ProfileDraft,
    ProfileRevision,
    ProfileStatus,
    ResourceKind,
    ResourceRevision,
    ResourceStatus,
    RunState,
    ToolBinding,
)
from sidera_agents.errors import AgentError

from sidera_control_plane.application.task_tool_scope import read_task_tool_scope
from sidera_control_plane.application.workspace_handoff import WorkspaceHandoffService
from sidera_control_plane.domain.execution import ExecutionSessionStatus
from sidera_control_plane.domain.handoffs import HandoffPhase
from tests.unit.test_native_handoff_start import NOW, make_start_case


class PinnedTaskToolCatalog:
    def __init__(self, case, connection_id):
        self.case = case
        self.reads = []
        resource_ref = case.authority.resources[0]
        self.resource = ResourceRevision(
            ref=resource_ref,
            draft=CapabilityResourceDraft(
                name="Pinned task tools", kind=ResourceKind.MCP,
                settings={"connection_id": str(connection_id)}, secret_refs=(),
            ),
            content_hash="pinned-resource", created_at=NOW,
        )
        self.resource_status = ResourceStatus(
            resource_id=resource_ref.resource_id, enabled=True, version=1,
        )
        self.profile = ProfileRevision(
            ref=case.authority.profile_ref,
            draft=ProfileDraft(
                name="Admitted target", description="", runtime_ref=case.authority.runtime_ref,
                instructions="", resource_refs=(resource_ref,), model_id=None,
                thinking=None, max_active=1, environment={}, secret_refs=(),
                native_settings={},
            ),
            config_hash="pinned-profile", created_at=NOW,
        )
        self.profile_status = ProfileStatus(
            profile_id=self.profile.ref.profile_id, enabled=True, version=1,
        )

    def _read(self, kind, ref, grant_id):
        action = "resource.read" if kind.startswith("resource") else "profile.read"
        self.case.reader.require(grant_id, action, ref)
        self.reads.append((kind, ref, grant_id))
        return getattr(self, kind)

    def get_resource(self, ref, *, grant_id):
        return self._read("resource", ref, grant_id)

    def get_resource_status(self, ref, *, grant_id):
        return self._read("resource_status", ref, grant_id)

    def get_profile(self, ref, *, grant_id):
        return self._read("profile", ref, grant_id)

    def get_profile_status(self, ref, *, grant_id):
        return self._read("profile_status", ref, grant_id)


def make_task_tool_case(*, claim="released", session=True, real_scheduler=False):
    s = make_start_case(claim=claim, session=session, real_scheduler=real_scheduler)
    s.connection_id = uuid4()
    s.authority = replace(
        s.authority, tools=(ToolBinding(resource_ref=s.authority.resources[0]),),
    )
    s.uow.planning_authorities = SimpleNamespace(get=lambda identifier: None)
    s.catalog = PinnedTaskToolCatalog(s, s.connection_id)
    return s


def read_scope(s, *, connection_id=None):
    return read_task_tool_scope(
        s.uow_factory, catalog=s.catalog, authorizer=s.reader,
        grant_id=s.authority.id,
        connection_id=s.connection_id if connection_id is None else connection_id,
    )


@pytest.mark.parametrize("claim", ("released", "expired", "held"))
def test_pinned_mcp_task_tool_scope_keeps_original_admitted_authority(claim):
    s = make_task_tool_case(claim=claim)
    scope = read_scope(s)
    execution = s.uow.executions.values[s.authority.execution_id]
    assert (
        scope.workspace_id, scope.principal_id, scope.role, scope.native_grant_id,
        scope.connection_id, scope.profile_ref, scope.start_operation_id, scope.run_id,
        scope.root_task_id, scope.execution_id, scope.worker_item_id,
        scope.execution_version, scope.binding_version,
        scope.planning_request_id, scope.planning_run_id, scope.lease_id, scope.fencing_token,
    ) == (
        s.authority.workspace_id, s.authority.principal_id, "worker", s.authority.id,
        s.connection_id, s.authority.profile_ref, s.link.start_operation_id, s.link.run_id,
        s.uow.handoff_transitions.item.native_payload.root_task_id, execution.id,
        execution.worker_item_id, execution.version, s.authority.binding_version,
        None, None, s.authority.lease_id, s.authority.fencing_token,
    )
    assert s.catalog.reads == [
        ("resource", s.authority.resources[0], s.authority.id),
        ("resource_status", s.authority.resources[0], s.authority.id),
        ("profile", s.authority.profile_ref, s.authority.id),
        ("profile_status", s.authority.profile_ref, s.authority.id),
    ]
    assert len(s.native.starts) == 1 and not s.native.reads


def test_pinned_mcp_task_tool_scope_rejects_unpinned_connection():
    s = make_task_tool_case()
    with pytest.raises(AgentError) as caught:
        read_scope(s, connection_id=uuid4())
    assert caught.value.code == "permission_denied"


@pytest.mark.parametrize("bad", ("resource_revision", "resource_disabled", "profile_revision", "profile_disabled"))
def test_pinned_mcp_task_tool_scope_retains_catalog_pins(bad):
    s = make_task_tool_case()
    if bad.endswith("revision"):
        kind = bad.removesuffix("_revision")
        value = getattr(s.catalog, kind)
        setattr(s.catalog, kind, value.model_copy(update={
            "ref": value.ref.model_copy(update={"revision": 2}),
        }))
    else:
        kind = bad.removesuffix("_disabled") + "_status"
        value = getattr(s.catalog, kind)
        setattr(s.catalog, kind, value.model_copy(update={"enabled": False}))
    with pytest.raises(AgentError) as caught:
        read_scope(s)
    assert caught.value.code == "permission_denied"


def test_ordered_task_tool_initialization_between_real_start_claims_keeps_original_run():
    s = make_task_tool_case(session=False, real_scheduler=True)
    original_operation, original_run = s.link.start_operation_id, s.link.run_id
    original_sessions = set(s.uow.sessions.values)
    assert s.session_id not in original_sessions
    first = s.coordinator.run_once(limit=1)
    assert first.waiting == 1 and first.progressed == first.blocked == 0
    stored = s.uow.handoff_transitions.item
    assert stored.phase is HandoffPhase.START_TARGET_SESSION
    assert stored.target_session_id is None and stored.lease_owner is None
    assert s.uow.sessions.values[s.session_id].status is ExecutionSessionStatus.ACTIVE
    assert set(s.uow.sessions.values) == original_sessions | {s.session_id}
    WorkspaceHandoffService._validate_native_cleanup(s.uow, stored)
    scope = read_scope(s)
    assert scope.run_id == original_run and scope.start_operation_id == original_operation
    assert s.snapshot.state is RunState.STARTING
    assert s.coordinator.run_once(limit=1).waiting == 1
    assert s.uow.handoff_transitions.item.phase is HandoffPhase.START_TARGET_SESSION
    s.snapshot = s.snapshot.model_copy(update={"state": RunState.RUNNING})
    assert s.coordinator.run_once(limit=1).progressed == 1
    stored = s.uow.handoff_transitions.item
    assert stored.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT
    assert stored.target_session_id == s.session_id
    assert s.link.start_operation_id == original_operation and s.link.run_id == original_run
    assert len(s.native.starts) == len(s.native_events) == 1
    assert len(s.uow.executions.values) == 2
    assert set(s.uow.sessions.values) == original_sessions | {s.session_id}
    assert s.transactions == 0
