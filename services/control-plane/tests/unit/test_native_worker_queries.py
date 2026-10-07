from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module, util
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError
from sidera_agents.contracts import (
    EffectiveConfig,
    EventPage,
    ProfileRef,
    RunEvent,
    RunSnapshot,
    RuntimeRef,
)
from sidera_agents.errors import AgentError
from sidera_agents.observations import public_event_data

from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.application.worker_workspace_queries import (
    WorkerWorkspaceQuerySnapshot,
)
from sidera_control_plane.domain.assignment_views import (
    AgentBindingView,
    AgentExecutionView,
    AssigneeView,
    OriginalRunEvidence,
)
from sidera_control_plane.domain.worker_item_projections import (
    BindingFact,
    ExecutionFact,
    LeaseFact,
    SessionFact,
    WorkerFact,
    WorkerItemFact,
    WorkerItemPlacementFact,
)

WORKSPACE_ID = UUID("22000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("42000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("32000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("72000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("52000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("62000000-0000-0000-0000-000000000001")
RUN_ID = UUID("82000000-0000-0000-0000-000000000001")
GRANT_ID = UUID("92000000-0000-0000-0000-000000000001")
LEASE_ID = UUID("a2000000-0000-0000-0000-000000000001")
PLAN_ID = UUID("b2000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
PROFILE_REF = ProfileRef(workspace_id=WORKSPACE_ID, profile_id=PROFILE_ID, revision=2)
SESSION_ID = uuid5(NAMESPACE_URL, f"sidera.native.session:{RUN_ID}")
PRINCIPAL = "operator"
_USE_IDENTITY = object()


def _module(name: str):
    try:
        spec = util.find_spec(name)
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, f"Required native worker read module is missing: {name}"
    return import_module(name)


def _run_snapshot(
    *,
    run_id: UUID = RUN_ID,
    workspace_id: UUID = WORKSPACE_ID,
    profile_ref: ProfileRef = PROFILE_REF,
    state: str = "idle",
    input_open: bool = True,
    active_response_id: UUID | None = None,
) -> RunSnapshot:
    config = EffectiveConfig(
        profile_ref=profile_ref,
        config_hash="sha256:" + "a" * 64,
        runtime_ref=RuntimeRef(workspace_id=workspace_id, runtime_id=PROFILE_ID),
        actual_runtime_version="runtime-1",
        selected_model="model-1",
        observed_model="model-1",
        inherited_fields=(),
        applied_fields=("instructions",),
        resource_refs=(),
        context_receipts=(),
    )
    return RunSnapshot(
        run_id=run_id,
        workspace_id=workspace_id,
        profile_ref=profile_ref,
        policy="bounded",
        state=state,
        input_open=input_open,
        active_response_id=active_response_id,
        effective_config=config,
        last_event_sequence=11,
        capabilities={},
        reason_code=None,
    )


def _run_event(
    sequence: int,
    *,
    run_id: UUID = RUN_ID,
    data: dict | None = None,
) -> RunEvent:
    return RunEvent(
        run_id=run_id,
        sequence=sequence,
        kind="agent_message",
        occurred_at=NOW + timedelta(seconds=sequence),
        operation_id=None,
        response_id=None,
        data={"text": f"event-{sequence}"} if data is None else data,
    )


def _workspace_snapshot(
    *,
    execution: ExecutionFact | None = None,
    session: SessionFact | None = None,
    worker_item: WorkerItemFact | None = None,
    runtime_binding: BindingFact | None = None,
    projection_stale: bool = False,
) -> WorkerWorkspaceQuerySnapshot:
    item = worker_item or WorkerItemFact(
        id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=UUID("12000000-0000-0000-0000-000000000001"),
        stable_key="native-worker",
        title="Native worker",
        responsibility="Implement the worker task",
        status="active",
        context_version=4,
        acknowledged_context_version=4,
        version=11,
        updated_at=NOW,
    )
    selected_execution = execution or ExecutionFact(
        id=EXECUTION_ID,
        task_id=SUBTASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        lineage_id=EXECUTION_ID,
        attempt=1,
        status="running",
        failure_code=None,
        assigned_worker_id=WORKER_ID,
        pinned_worker_id=None,
        active_lease_id=LEASE_ID,
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW,
        started_at=NOW - timedelta(minutes=1),
        finished_at=None,
        version=7,
        native_binding_version=2,
    )
    selected_session = session or SessionFact(
        id=SESSION_ID,
        execution_id=EXECUTION_ID,
        worker_id=WORKER_ID,
        adapter_session_id=f"agent-run:{RUN_ID}",
        status="active",
        input_mode="queued",
        version=5,
        created_at=NOW - timedelta(minutes=1),
        last_activity_at=NOW,
    )
    bindings = (
        runtime_binding
        if runtime_binding is not None
        else BindingFact(
            execution_id=EXECUTION_ID,
            worker_id=WORKER_ID,
            lease_id=LEASE_ID,
            fencing_token=9,
            updated_at=NOW,
            version=3,
        ),
    )
    return WorkerWorkspaceQuerySnapshot(
        worker_item=item,
        placements=(
            WorkerItemPlacementFact(
                plan_snapshot_id=PLAN_ID,
                plan_generation=6,
                worker_item_id=WORKER_ITEM_ID,
                worker_item_key="native-worker",
                subtask_key="native-task",
                subtask_task_id=SUBTASK_ID,
                role="implementer",
                capability_requirements=("python",),
            ),
        ),
        executions=() if execution is False else (selected_execution,),
        sessions=() if session is False else (selected_session,),
        workers=(WorkerFact(id=WORKER_ID, display_name="worker", adapter_type="test"),),
        bindings=bindings,
        leases=(
            LeaseFact(
                id=LEASE_ID,
                execution_id=EXECUTION_ID,
                worker_id=WORKER_ID,
                status="active",
                fencing_token=9,
                updated_at=NOW,
            ),
        ),
        events=(),
        inputs=(),
        artifacts=(),
        evidence=(),
        last_change_cursor=3,
        as_of=NOW,
        projection_stale=projection_stale,
    )


def _assignment_view(
    *, version: int = 2, profile_ref: ProfileRef = PROFILE_REF, kind: str = "profile"
) -> AgentBindingView:
    return AgentBindingView(
        workspace_id=WORKSPACE_ID,
        target_type="worker_item",
        target_id=WORKER_ITEM_ID,
        assignee=AssigneeView(
            kind=kind,
            id=str(profile_ref.profile_id) if kind == "profile" else PRINCIPAL,
        ),
        profile_ref=profile_ref if kind == "profile" else None,
        version=version,
        created_at=NOW - timedelta(days=1),
        changed_at=NOW,
        runnable=True,
        not_runnable_reasons=(),
    )


def _execution_view(
    *,
    execution_id: UUID = EXECUTION_ID,
    worker_item_id: UUID | None = WORKER_ITEM_ID,
    profile_ref: ProfileRef | None = PROFILE_REF,
    binding_version: int | None = 2,
    run_id: UUID | None = RUN_ID,
    status: str = "running",
) -> AgentExecutionView:
    return AgentExecutionView(
        workspace_id=WORKSPACE_ID,
        execution_id=execution_id,
        root_task_id=UUID("12000000-0000-0000-0000-000000000001"),
        subtask_id=SUBTASK_ID,
        worker_item_id=worker_item_id,
        status=status,
        updated_at=NOW,
        profile_ref=profile_ref,
        binding_version=binding_version,
        run_id=run_id,
        link_status="running" if run_id is not None else None,
        event_cursor=11 if run_id is not None else None,
    )


def _original_evidence(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    run_id: UUID | None = RUN_ID,
    profile_ref: ProfileRef | None = PROFILE_REF,
    grant_id: UUID | None = GRANT_ID,
    target_type: str | None = "worker_item",
    target_id: UUID | None = WORKER_ITEM_ID,
    verified: bool = True,
    reason_code: str | None = None,
) -> OriginalRunEvidence:
    return OriginalRunEvidence(
        workspace_id=workspace_id,
        run_id=run_id,
        profile_ref=profile_ref,
        operation_id=UUID("c2000000-0000-0000-0000-000000000001"),
        grant_id=grant_id,
        target_type=target_type,
        target_id=target_id,
        verified=verified,
        reason_code=reason_code,
    )


class _RecordingCase:
    def __init__(
        self,
        *,
        identity: ControlPlaneIdentity | None = None,
        current_identity: ControlPlaneIdentity | None | object = _USE_IDENTITY,
    ) -> None:
        self.identity = identity or ControlPlaneIdentity(PRINCIPAL, GRANT_ID, "user")
        self.current_identity = (
            self.identity if current_identity is _USE_IDENTITY else current_identity
        )
        self.snapshot = _workspace_snapshot()
        self.binding: AgentBindingView | None = _assignment_view()
        self.execution_rows = {EXECUTION_ID: _execution_view()}
        self.original_rows = {EXECUTION_ID: _original_evidence()}
        self.run = _run_snapshot()
        self.pages: dict[int, EventPage] = {
            0: EventPage(events=(_run_event(2),), next_sequence=2, has_more=False),
            1: EventPage(events=(_run_event(2),), next_sequence=2, has_more=False),
        }
        self.observations_enabled = True
        self.store_calls: list[tuple[str, tuple, dict]] = []
        self.authorizer_calls: list[tuple[str, UUID, str, object | None]] = []
        self.input_authorizer_calls: list[tuple[UUID, str, object]] = []
        self.native_calls: list[tuple] = []
        self.start_calls: list[tuple] = []
        self.close_events: list[str] = []
        self.active_transactions = 0
        self.denied_human = False
        self.denied_input = False
        self.on_get_run = None
        self.on_read_trace = None
        self.execution_read_error: Exception | None = None
        self.original_read_error: Exception | None = None

        case = self

        class WorkspaceStore:
            def load(self, workspace_id, worker_item_id):
                return case._read(
                    "workspace",
                    (workspace_id, worker_item_id),
                    {},
                    lambda: (
                        case.snapshot
                        if workspace_id == case.snapshot.worker_item.workspace_id
                        and worker_item_id == case.snapshot.worker_item.id
                        else None
                    ),
                )

        class AssignmentStore:
            def read_assignment(self, workspace_id, target_type, target_id):
                return case._read(
                    "assignment",
                    (workspace_id, target_type, target_id),
                    {},
                    lambda: case.binding,
                )

            def read_execution(self, workspace_id, execution_id):
                def read():
                    if case.execution_read_error is not None:
                        raise case.execution_read_error
                    return case.execution_rows.get(execution_id)

                return case._read("execution", (workspace_id, execution_id), {}, read)

            def read_original_run(
                self, workspace_id, kind, work_id, *, target_type=None
            ):
                def read():
                    if case.original_read_error is not None:
                        raise case.original_read_error
                    return case.original_rows.get(work_id)

                return case._read(
                    "original",
                    (workspace_id, kind, work_id),
                    {"target_type": target_type},
                    read,
                )

        class RequestAuthorizer:
            def identity_for_grant(self, grant_id):
                case.authorizer_calls.append(("identity", grant_id, "", None))
                if (
                    case.current_identity is not None
                    and case.current_identity.grant_id == grant_id
                ):
                    return case.current_identity
                return None

            def require(self, grant_id, action, target):
                case.authorizer_calls.append(("require", grant_id, action, target))
                if case.denied_human:
                    raise AgentError("permission_denied", "denied")

        class InputAuthorizer:
            def require(self, grant_id, action, target):
                case.input_authorizer_calls.append((grant_id, action, target))
                if case.denied_input:
                    raise AgentError("permission_denied", "denied")

        class Execution:
            def get_run(self, workspace_id, run_id, *, grant_id):
                case.native_calls.append(("get_run", workspace_id, run_id, grant_id))
                case.native_call_open_transactions.append(case.active_transactions)
                if case.on_get_run is not None:
                    case.on_get_run(case)
                if case.run_error is not None:
                    raise case.run_error
                return case.run

            def start(self, *args, **kwargs):
                case.start_calls.append((args, kwargs))
                raise AssertionError("native reads must not start a Run")

        class Observations:
            def read_trace(
                self, workspace_id, run_id, after_sequence, limit=100, *, grant_id
            ):
                case.native_calls.append(
                    ("trace", workspace_id, run_id, after_sequence, limit, grant_id)
                )
                case.native_call_open_transactions.append(case.active_transactions)
                if case.on_read_trace is not None:
                    case.on_read_trace(case)
                page = case.pages.get(after_sequence)
                if page is None:
                    raise AgentError("query_unavailable", "missing page")
                return page

        self.workspace_store = WorkspaceStore()
        self.assignment_store = AssignmentStore()
        self.authorizer = RequestAuthorizer()
        self.input_authorizer = InputAuthorizer()
        self.execution = Execution()
        self.observations = Observations() if self.observations_enabled else None
        self.native_call_open_transactions: list[int] = []

    def _read(self, name, args, kwargs, callback):
        self.store_calls.append((name, args, kwargs))
        self.active_transactions += 1
        try:
            return callback()
        finally:
            self.active_transactions -= 1
            self.close_events.append(name)

    def service(self, *, observations=...):
        module = _module("sidera_control_plane.application.native_worker_queries")
        selected_observations = (
            self.observations if observations is ... else observations
        )
        return module.NativeWorkerQueryService(
            workspace_store=self.workspace_store,
            assignment_store=self.assignment_store,
            execution=self.execution,
            observations=selected_observations,
            input_authorizer=self.input_authorizer,
            authorizer=self.authorizer,
            identity=self.identity,
            principal_id=self.identity.principal_id,
            workspace_id=WORKSPACE_ID,
        )

    @property
    def run_error(self):
        return getattr(self, "_run_error", None)

    @run_error.setter
    def run_error(self, value):
        self._run_error = value


@pytest.fixture
def native_query_case():
    return _RecordingCase


def test_denied_identity_never_reads_private_stores_or_native(native_query_case):
    identity = ControlPlaneIdentity(PRINCIPAL, GRANT_ID, "agent")
    case = native_query_case(identity=identity)
    with pytest.raises(AgentError) as error:
        case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert error.value.code == "permission_denied"
    assert case.store_calls == []
    assert case.native_calls == []
    assert case.start_calls == []


def test_forged_or_expired_grant_never_reads_private_stores_or_native(
    native_query_case,
):
    identity = ControlPlaneIdentity(PRINCIPAL, GRANT_ID, "user")
    case = native_query_case(identity=identity, current_identity=None)
    with pytest.raises(AgentError) as error:
        case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert error.value.code == "permission_denied"
    assert case.store_calls == []
    assert case.native_calls == []


def test_workspace_mismatch_is_denied_before_private_reads(native_query_case):
    case = native_query_case()
    other_workspace = UUID("f2000000-0000-0000-0000-000000000010")
    with pytest.raises(AgentError) as error:
        case.service().read_context(other_workspace, WORKER_ITEM_ID)
    assert error.value.code == "permission_denied"
    assert case.store_calls == []
    assert case.native_calls == []


def test_item_permission_is_required_before_private_reads(native_query_case):
    case = native_query_case()
    case.denied_human = True
    with pytest.raises(AgentError) as error:
        case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert error.value.code == "permission_denied"
    assert case.store_calls == []
    assert case.native_calls == []
    assert case.authorizer_calls[-1][2:] == (
        "assignment.read",
        AssignmentTarget(WORKSPACE_ID, "worker_item", WORKER_ITEM_ID, PRINCIPAL),
    )


def test_context_is_built_from_canonical_facts_and_saved_original_pin(
    native_query_case,
):
    case = native_query_case()
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)

    assert view.workspace_id == WORKSPACE_ID
    assert view.worker_item_id == WORKER_ITEM_ID
    assert view.execution_mode == "native_profile"
    assert view.worker_item_version == 11
    assert view.execution_version == 7
    assert view.context.model_dump() == {
        "worker_item_id": WORKER_ITEM_ID,
        "plan_generation": 6,
        "context_version": 4,
        "execution_id": EXECUTION_ID,
        "session_id": SESSION_ID,
        "session_version": 5,
        "worker_id": WORKER_ID,
        "execution_mode": "native_profile",
        "binding_version": 2,
        "run_id": RUN_ID,
    }
    assert set(type(view.context).model_fields) == {
        "worker_item_id",
        "plan_generation",
        "context_version",
        "execution_id",
        "session_id",
        "session_version",
        "worker_id",
        "execution_mode",
        "binding_version",
        "run_id",
    }
    assert view.input_enabled is True
    assert view.read_only_reason is None
    assert view.run.state == "idle"
    assert view.run.input_open is True
    assert case.input_authorizer_calls == [
        (GRANT_ID, "run.input", WORKSPACE_ID),
        (GRANT_ID, "run.input", WORKSPACE_ID),
    ]
    assert case.native_call_open_transactions == [0]
    assert case.start_calls == []


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("ordinary", "no_active_session"),
        ("paused", "worker_item_paused"),
        ("retired", "worker_item_retired"),
        ("unacknowledged", "context_not_acknowledged"),
        ("projection_stale", "projection_stale"),
    ],
)
def test_missing_native_session_keeps_canonical_read_only_reason(
    native_query_case, condition, expected
):
    case = native_query_case()
    snapshot = case.snapshot
    if condition in {"paused", "retired"}:
        case.snapshot = replace(
            snapshot,
            worker_item=replace(
                snapshot.worker_item,
                status="paused" if condition == "paused" else "retired",
            ),
        )
    elif condition == "unacknowledged":
        case.snapshot = replace(
            snapshot,
            worker_item=replace(snapshot.worker_item, acknowledged_context_version=3),
        )
    elif condition == "projection_stale":
        case.snapshot = replace(snapshot, projection_stale=True)
    case.snapshot = replace(case.snapshot, sessions=())

    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)

    assert view.execution_mode == "native_profile"
    assert view.execution.execution_id == EXECUTION_ID
    assert view.read_only_reason == expected
    assert view.context is None
    assert view.run is None
    assert view.input_enabled is False
    assert case.native_calls == []
    assert case.input_authorizer_calls == []
    assert case.start_calls == []
    assert case.active_transactions == 0


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("retired", "worker_item_retired"),
        ("paused", "worker_item_paused"),
        ("unacknowledged", "context_not_acknowledged"),
        ("stale_projection", "projection_stale"),
        ("no_active_session", "no_active_session"),
        ("terminal_execution", "execution_not_active"),
        ("unsupported_input", "adapter_input_unsupported"),
    ],
)
def test_canonical_read_only_reason_priority_is_preserved(
    native_query_case, condition, expected
):
    case = native_query_case()
    snapshot = case.snapshot
    if condition in {"retired", "paused", "unacknowledged"}:
        changes = {
            "retired": {"status": "retired"},
            "paused": {"status": "paused"},
            "unacknowledged": {"acknowledged_context_version": 3},
        }[condition]
        case.snapshot = replace(
            snapshot, worker_item=replace(snapshot.worker_item, **changes)
        )
    elif condition == "stale_projection":
        case.snapshot = replace(snapshot, projection_stale=True)
    elif condition == "no_active_session":
        case.snapshot = replace(
            snapshot, sessions=(replace(snapshot.sessions[0], status="closed"),)
        )
    elif condition == "terminal_execution":
        case.snapshot = replace(
            snapshot, executions=(replace(snapshot.executions[0], status="succeeded"),)
        )
        case.execution_rows[EXECUTION_ID] = case.execution_rows[
            EXECUTION_ID
        ].model_copy(update={"status": "succeeded"})
    elif condition == "unsupported_input":
        case.snapshot = replace(
            snapshot,
            sessions=(replace(snapshot.sessions[0], input_mode="unsupported"),),
        )

    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.read_only_reason == expected
    assert view.input_enabled is False
    assert view.context is not None
    assert case.start_calls == []


def test_idle_open_run_allows_input_after_response_completion(native_query_case):
    case = native_query_case()
    case.run = _run_snapshot(state="idle", input_open=True, active_response_id=None)
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.input_enabled is True
    assert view.read_only_reason is None


def test_closed_run_disables_input_but_preserves_address(native_query_case):
    case = native_query_case()
    case.run = _run_snapshot(state="completed", input_open=False)
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.input_enabled is False
    assert view.read_only_reason == "native_input_closed"
    assert view.context.run_id == RUN_ID
    assert view.run.state == "completed"


def test_run_observation_failure_keeps_canonical_address_read_only(native_query_case):
    case = native_query_case()
    case.run_error = AgentError("query_unavailable", "private-run-error")
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.input_enabled is False
    assert view.read_only_reason == "native_run_unavailable"
    assert view.context.run_id == RUN_ID
    assert view.run is None
    assert len(case.input_authorizer_calls) == 2
    assert case.native_call_open_transactions == [0]


def test_denied_original_input_authority_disables_input(native_query_case):
    case = native_query_case()
    case.denied_input = True
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.input_enabled is False
    assert view.read_only_reason == "native_input_denied"
    assert view.context.run_id == RUN_ID
    assert len(case.input_authorizer_calls) == 2


def test_binding_change_during_observation_keeps_original_address_read_only(
    native_query_case,
):
    case = native_query_case()

    def change_binding(current):
        current.binding = _assignment_view(version=3)
        current.snapshot = replace(
            current.snapshot,
            executions=(
                replace(current.snapshot.executions[0], native_binding_version=None),
            ),
        )

    case.on_get_run = change_binding
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.input_enabled is False
    assert view.read_only_reason == "stale_execution_binding"
    assert view.context.binding_version == 2
    assert view.context.run_id == RUN_ID
    assert view.binding.version == 3


def test_context_human_revocation_during_observation_denies_return(
    native_query_case,
):
    case = native_query_case()
    case.on_get_run = lambda current: setattr(current, "current_identity", None)
    with pytest.raises(AgentError) as error:
        case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert error.value.code == "permission_denied"
    assert case.native_calls == [("get_run", WORKSPACE_ID, RUN_ID, GRANT_ID)]
    assert case.native_call_open_transactions == [0]


@pytest.mark.parametrize(
    ("session_change", "expected"),
    [
        ("adapter_session_id", "native_context_mismatch"),
        ("id", "native_context_mismatch"),
        ("worker_id", "native_context_mismatch"),
    ],
)
def test_incoherent_native_session_never_emits_context(
    native_query_case, session_change, expected
):
    case = native_query_case()
    session = case.snapshot.sessions[0]
    changes = {
        "adapter_session_id": "agent-run:other",
        "id": UUID("d2000000-0000-0000-0000-000000000001"),
        "worker_id": UUID("d2000000-0000-0000-0000-000000000002"),
    }
    changed = replace(session, **{session_change: changes[session_change]})
    case.snapshot = replace(case.snapshot, sessions=(changed,))
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.execution_mode == "native_profile"
    assert view.context is None
    assert view.input_enabled is False
    assert view.read_only_reason == expected
    assert case.native_calls == []


@pytest.mark.parametrize(
    ("view_change", "evidence_change"),
    [
        ({"run_id": UUID("e2000000-0000-0000-0000-000000000001")}, {}),
        (
            {
                "profile_ref": ProfileRef(
                    workspace_id=WORKSPACE_ID,
                    profile_id=UUID("e2000000-0000-0000-0000-000000000002"),
                    revision=2,
                )
            },
            {},
        ),
        ({}, {"target_id": UUID("e2000000-0000-0000-0000-000000000003")}),
    ],
)
def test_saved_execution_link_mismatch_never_observes_run(
    native_query_case, view_change, evidence_change
):
    case = native_query_case()
    case.execution_rows[EXECUTION_ID] = case.execution_rows[EXECUTION_ID].model_copy(
        update=view_change
    )
    case.original_rows[EXECUTION_ID] = replace(
        case.original_rows[EXECUTION_ID], **evidence_change
    )
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.execution_mode == "native_profile"
    assert view.context is None
    assert view.input_enabled is False
    assert view.read_only_reason == "native_run_mismatch"
    assert case.native_calls == []


def test_wrong_observed_run_profile_is_unavailable_without_retargeting(
    native_query_case,
):
    case = native_query_case()
    case.run = _run_snapshot(run_id=UUID("e2000000-0000-0000-0000-000000000004"))
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.execution_mode == "native_profile"
    assert view.context is None
    assert view.run is None
    assert view.input_enabled is False
    assert view.read_only_reason == "native_run_mismatch"


def test_unadmitted_native_execution_has_no_run_or_guessed_context(native_query_case):
    case = native_query_case()
    case.execution_rows[EXECUTION_ID] = _execution_view(run_id=None)
    case.original_rows[EXECUTION_ID] = _original_evidence(
        run_id=None, verified=False, reason_code="run_not_admitted"
    )
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.execution_mode == "native_profile"
    assert view.context is None
    assert view.run is None
    assert view.input_enabled is False
    assert view.read_only_reason == "run_not_admitted"
    assert case.native_calls == []


def test_native_link_failure_does_not_downgrade_to_legacy(native_query_case):
    case = native_query_case()
    case.original_read_error = AgentError("query_unavailable", "private-link")
    with pytest.raises(AgentError) as error:
        case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert error.value.code == "query_unavailable"
    assert case.native_calls == []


def test_genuine_legacy_execution_has_no_native_address_or_run(native_query_case):
    case = native_query_case()
    case.binding = _assignment_view(kind="human")
    case.snapshot = replace(
        case.snapshot,
        executions=(replace(case.snapshot.executions[0], native_binding_version=None),),
        sessions=(
            replace(case.snapshot.sessions[0], adapter_session_id="legacy-session"),
        ),
    )
    case.execution_rows[EXECUTION_ID] = _execution_view(
        profile_ref=None, binding_version=None, run_id=None
    )
    case.original_rows[EXECUTION_ID] = None
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.execution_mode == "legacy"
    assert view.context is None
    assert view.run is None
    assert view.input_enabled is False
    assert view.read_only_reason == "execution_mode_legacy"
    assert case.native_calls == []


def test_unadmitted_human_item_is_unassigned_without_native_reads(native_query_case):
    case = native_query_case()
    case.binding = _assignment_view(kind="human")
    case.snapshot = _workspace_snapshot(execution=False, session=False)
    view = case.service().read_context(WORKSPACE_ID, WORKER_ITEM_ID)
    assert view.execution_mode == "unassigned"
    assert view.execution is None
    assert view.execution_version is None
    assert view.context is None
    assert view.run is None
    assert view.input_enabled is False
    assert case.native_calls == []


def test_trace_authorizes_exact_item_and_execution_before_observation(
    native_query_case,
):
    case = native_query_case()
    view = case.service().read_trace(
        WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID, after_sequence=1, limit=1
    )
    assert view.workspace_id == WORKSPACE_ID
    assert view.worker_item_id == WORKER_ITEM_ID
    assert view.execution_id == EXECUTION_ID
    assert view.run_id == RUN_ID
    assert view.profile_ref == PROFILE_REF
    assert [(event.run_id, event.sequence) for event in view.events] == [(RUN_ID, 2)]
    assert case.native_calls == [("trace", WORKSPACE_ID, RUN_ID, 1, 1, GRANT_ID)]
    first_store_index = next(
        index for index, call in enumerate(case.store_calls) if call[0] == "execution"
    )
    required_targets = [
        call[3] for call in case.authorizer_calls if call[0] == "require"
    ]
    assert required_targets[:2] == [
        AssignmentTarget(WORKSPACE_ID, "worker_item", WORKER_ITEM_ID, PRINCIPAL),
        AssignmentTarget(WORKSPACE_ID, "execution", EXECUTION_ID, PRINCIPAL),
    ]
    assert first_store_index >= 0
    assert case.native_call_open_transactions == [0]
    assert case.start_calls == []


def test_trace_forward_pages_use_actual_cursor_and_empty_page_keeps_cursor(
    native_query_case,
):
    case = native_query_case()
    case.pages = {
        0: EventPage(
            events=(_run_event(3), _run_event(8)), next_sequence=8, has_more=True
        ),
        8: EventPage(events=(_run_event(12),), next_sequence=12, has_more=False),
        12: EventPage(events=(), next_sequence=12, has_more=False),
    }
    first = case.service().read_trace(
        WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID, limit=2
    )
    second = case.service().read_trace(
        WORKSPACE_ID,
        WORKER_ITEM_ID,
        EXECUTION_ID,
        after_sequence=first.next_sequence,
        limit=2,
    )
    empty = case.service().read_trace(
        WORKSPACE_ID,
        WORKER_ITEM_ID,
        EXECUTION_ID,
        after_sequence=second.next_sequence,
        limit=2,
    )
    assert [event.sequence for event in first.events] == [3, 8]
    assert first.next_sequence == 8 and first.has_more is True
    assert [event.sequence for event in second.events] == [12]
    assert second.next_sequence == 12 and second.has_more is False
    assert empty.events == () and empty.next_sequence == 12 and not empty.has_more
    assert [call[3] for call in case.native_calls] == [0, 8, 12]


def test_historical_trace_keeps_original_pin_after_owner_change(native_query_case):
    case = native_query_case()
    case.binding = _assignment_view(
        version=3,
        profile_ref=ProfileRef(
            workspace_id=WORKSPACE_ID,
            profile_id=UUID("f2000000-0000-0000-0000-000000000001"),
            revision=1,
        ),
    )
    view = case.service().read_trace(WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID)
    assert view.profile_ref == PROFILE_REF
    assert view.run_id == RUN_ID
    assert case.native_calls[0][2] == RUN_ID
    assert not case.input_authorizer_calls


def test_foreign_execution_never_observes_trace(native_query_case):
    case = native_query_case()
    foreign_execution_id = UUID("f2000000-0000-0000-0000-000000000002")
    case.execution_rows[foreign_execution_id] = _execution_view(
        execution_id=foreign_execution_id,
        worker_item_id=UUID("f2000000-0000-0000-0000-000000000003"),
    )
    case.original_rows[foreign_execution_id] = _original_evidence(
        target_id=UUID("f2000000-0000-0000-0000-000000000003")
    )
    with pytest.raises(AgentError) as error:
        case.service().read_trace(WORKSPACE_ID, WORKER_ITEM_ID, foreign_execution_id)
    assert error.value.code in {"permission_denied", "not_found"}
    assert case.native_calls == []


@pytest.mark.parametrize(
    "page",
    [
        EventPage(
            events=(
                _run_event(2, run_id=UUID("f2000000-0000-0000-0000-000000000004")),
            ),
            next_sequence=2,
            has_more=False,
        ),
        EventPage(
            events=(_run_event(2), _run_event(2)), next_sequence=2, has_more=False
        ),
        EventPage(events=(_run_event(2),), next_sequence=3, has_more=False),
        EventPage(events=(_run_event(2),), next_sequence=2, has_more=True),
    ],
)
def test_malformed_trace_page_is_unavailable(native_query_case, page):
    case = native_query_case()
    case.pages = {0: page}
    with pytest.raises(AgentError) as error:
        case.service().read_trace(WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID, limit=2)
    assert error.value.code == "query_unavailable"


def test_trace_rejects_invalid_cursor_before_any_read(native_query_case):
    case = native_query_case()
    with pytest.raises(AgentError) as error:
        case.service().read_trace(
            WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID, after_sequence=-1
        )
    assert error.value.code == "configuration_invalid"
    assert case.store_calls == []
    assert case.native_calls == []


def test_trace_rechecks_human_and_saved_link_after_observation(native_query_case):
    case = native_query_case()

    def revoke(current):
        current.current_identity = None

    case.on_read_trace = revoke
    with pytest.raises(AgentError) as error:
        case.service().read_trace(WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID)
    assert error.value.code == "permission_denied"
    assert len(case.native_calls) == 1


def test_trace_unsupported_has_safe_fixed_code(native_query_case):
    case = native_query_case()

    def unsupported(_case):
        raise AgentError("unsupported_capability", "private-native-handle-secret")

    case.on_read_trace = unsupported
    with pytest.raises(AgentError) as error:
        case.service().read_trace(WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID)
    assert error.value.code == "native_trace_unsupported"
    assert "private-native-handle-secret" not in str(error.value)
    assert case.native_call_open_transactions == [0]
    assert case.start_calls == []


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [("revocation", "permission_denied"), ("link_drift", "query_unavailable")],
)
def test_unsupported_trace_rechecks_human_and_link_before_classifying(
    native_query_case, change, expected_code
):
    case = native_query_case()

    def unsupported(current):
        if change == "revocation":
            current.current_identity = None
        else:
            current.original_rows[EXECUTION_ID] = replace(
                current.original_rows[EXECUTION_ID],
                run_id=UUID("f2000000-0000-0000-0000-000000000005"),
            )
        raise AgentError("unsupported_capability", "private-native-handle-secret")

    case.on_read_trace = unsupported
    with pytest.raises(AgentError) as error:
        case.service().read_trace(WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID)
    assert error.value.code == expected_code
    assert "private-native-handle-secret" not in str(error.value)
    assert case.native_call_open_transactions == [0]
    assert case.start_calls == []


def test_disabled_trace_service_is_unavailable(native_query_case):
    case = native_query_case()
    with pytest.raises(AgentError) as error:
        case.service(observations=None).read_trace(
            WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID
        )
    assert error.value.code == "query_unavailable"
    assert case.native_calls == []


def test_public_trace_shape_contains_no_authority_or_private_native_fields(
    native_query_case,
):
    case = native_query_case()
    case.pages = {
        0: EventPage(
            events=(
                _run_event(
                    2,
                    data=public_event_data(
                        {"text": "safe", "_native": {"handle": "secret"}}
                    ),
                ),
            ),
            next_sequence=2,
            has_more=False,
        )
    }
    view = case.service().read_trace(WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID)
    payload = view.model_dump_json()
    assert "grant_id" not in payload
    assert "authority" not in payload
    assert "_native" not in payload
    assert view.events[0].data == {"text": "safe"}
    assert public_event_data(
        {"visible": True, "_native": {"credential": "secret"}}
    ) == {"visible": True}


def test_native_dtos_are_frozen_strict_and_forbid_extra_fields():
    views = _module("sidera_control_plane.domain.native_worker_views")
    address_fields = set(views.NativeWorkerContextAddress.model_fields)
    assert address_fields == {
        "worker_item_id",
        "plan_generation",
        "context_version",
        "execution_id",
        "session_id",
        "session_version",
        "worker_id",
        "execution_mode",
        "binding_version",
        "run_id",
    }
    valid = {
        "worker_item_id": WORKER_ITEM_ID,
        "plan_generation": 6,
        "context_version": 4,
        "execution_id": EXECUTION_ID,
        "session_id": SESSION_ID,
        "session_version": 5,
        "worker_id": WORKER_ID,
        "execution_mode": "native_profile",
        "binding_version": 2,
        "run_id": RUN_ID,
    }
    address = views.NativeWorkerContextAddress(**valid)
    with pytest.raises(ValidationError):
        address.plan_generation = 7
    with pytest.raises(ValidationError):
        views.NativeWorkerContextAddress(**{**valid, "run_id_extra": RUN_ID})
    with pytest.raises(ValidationError):
        views.NativeWorkerContextAddress(**{**valid, "session_version": True})
    with pytest.raises(ValidationError):
        views.NativeWorkerContextAddress(**{**valid, "binding_version": 0})
    assert views.NativeWorkerTraceView.model_config["extra"] == "forbid"
    assert views.NativeWorkerTraceView.model_config["frozen"] is True


def test_runtime_facade_accounts_for_and_closes_both_read_calls(
    native_query_case, monkeypatch
):
    from threading import Condition, Event

    runtime_module = import_module("sidera_control_plane.composition.native_runtime")
    case = native_query_case()
    runtime = runtime_module.ControlPlaneRuntime.__new__(
        runtime_module.ControlPlaneRuntime
    )
    runtime._condition = Condition()
    runtime._closed = False
    runtime._closing = False
    runtime._active_work = 0
    runtime._regular_work = 0
    runtime._drained = Event()
    runtime._settings = SimpleNamespace(
        local_actor_type="user", local_actor_id=PRINCIPAL
    )
    runtime._request_authorizer = case.authorizer
    runtime._execution = case.execution
    runtime._observations = case.observations
    runtime._execution_authorizer = case.input_authorizer
    runtime._database = SimpleNamespace(session=lambda: object())
    captured = []

    class Facade:
        def __init__(self, **dependencies):
            captured.append(dependencies)
            assert runtime._active_work == 1
            assert runtime._regular_work == 1

        def read_context(self, workspace_id, worker_item_id):
            assert runtime._active_work == 1
            return ("context", workspace_id, worker_item_id)

        def read_trace(
            self,
            workspace_id,
            worker_item_id,
            execution_id,
            *,
            after_sequence,
            limit,
        ):
            assert runtime._active_work == 1
            return (
                "trace",
                workspace_id,
                worker_item_id,
                execution_id,
                after_sequence,
                limit,
            )

    monkeypatch.setattr(runtime_module, "NativeWorkerQueryService", Facade)
    assert runtime.read_native_worker_context(
        WORKSPACE_ID, WORKER_ITEM_ID, grant_id=GRANT_ID
    ) == ("context", WORKSPACE_ID, WORKER_ITEM_ID)
    assert runtime.read_native_worker_trace(
        WORKSPACE_ID,
        WORKER_ITEM_ID,
        EXECUTION_ID,
        grant_id=GRANT_ID,
        after_sequence=3,
        limit=4,
    ) == ("trace", WORKSPACE_ID, WORKER_ITEM_ID, EXECUTION_ID, 3, 4)
    assert runtime._active_work == runtime._regular_work == 0
    assert len(captured) == 2
    assert all(item["execution"] is case.execution for item in captured)
    assert all(item["observations"] is case.observations for item in captured)
    assert all(item["input_authorizer"] is case.input_authorizer for item in captured)
    assert all(item["authorizer"] is case.authorizer for item in captured)
    assert all(item["workspace_id"] == WORKSPACE_ID for item in captured)

    runtime._closing = True
    with pytest.raises(AgentError) as error:
        runtime.read_native_worker_context(
            WORKSPACE_ID, WORKER_ITEM_ID, grant_id=GRANT_ID
        )
    assert error.value.code == "control_plane_closed"
    assert runtime._active_work == runtime._regular_work == 0
