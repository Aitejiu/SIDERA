from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module, util
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import ProfileRef
from sidera_agents.errors import AgentError

from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.application.request_authority import ControlPlaneIdentity


def query_module():
    name = "sidera_control_plane.application.assignment_queries"
    assert util.find_spec(name) is not None, (
        "The approved public assignment reader is missing"
    )
    return import_module(name)


@pytest.fixture
def query_lab():
    module = query_module()
    workspace, grant = uuid4(), uuid4()
    identity = ControlPlaneIdentity("operator", grant, "user")
    calls = []
    authority = SimpleNamespace(
        identity_for_grant=lambda supplied: identity if supplied == grant else None,
        require=lambda supplied, action, target: calls.append(
            (supplied, action, target)
        ),
    )
    reads = []
    store = SimpleNamespace(
        read_assignment=lambda *args: reads.append(("assignment", args)) or None,
        read_execution=lambda *args: reads.append(("execution", args)) or None,
        list_profile_work=lambda *args: reads.append(("work", args)) or (),
        read_original_run=lambda *args, **kwargs: (
            reads.append(("original", args)) or None
        ),
    )
    native_calls = []
    execution = SimpleNamespace(
        get_run=lambda *args, **kwargs: native_calls.append((args, kwargs))
    )
    service = module.AssignmentQueryService(
        store=store,
        execution=execution,
        authorizer=authority,
        identity=identity,
        principal_id="operator",
        workspace_id=workspace,
    )
    return SimpleNamespace(
        module=module,
        service=service,
        workspace=workspace,
        grant=grant,
        identity=identity,
        calls=calls,
        reads=reads,
        native_calls=native_calls,
        store=store,
        execution=execution,
        authority=authority,
    )


@pytest.mark.parametrize(
    "method",
    [
        "read_assignment",
        "read_agent_execution",
        "list_profile_work",
        "read_profile_work_run",
    ],
)
@pytest.mark.parametrize("denial", ["foreign", "revoked", "nonhuman"])
def test_assignment_queries_deny_before_persistence_or_native(
    query_lab, method, denial
):
    lab = query_lab
    workspace = uuid4() if denial == "foreign" else lab.workspace
    if denial == "revoked":
        lab.authority.identity_for_grant = lambda supplied: None
    elif denial == "nonhuman":
        lab.authority.identity_for_grant = lambda supplied: replace(
            lab.identity, actor_type="agent"
        )
    args = (
        (workspace, "subtask", uuid4())
        if method == "read_assignment"
        else (
            (workspace, "execution", uuid4())
            if method == "read_profile_work_run"
            else (workspace, uuid4())
        )
    )
    with pytest.raises(AgentError) as error:
        getattr(lab.service, method)(*args)
    assert error.value.code == "permission_denied"
    assert lab.reads == lab.native_calls == []


def test_assignment_query_authorizes_exact_target_before_null_binding(query_lab):
    lab = query_lab
    target_id = uuid4()
    assert lab.service.read_assignment(lab.workspace, "subtask", target_id) is None
    assert lab.calls == [
        (
            lab.grant,
            "assignment.read",
            AssignmentTarget(lab.workspace, "subtask", target_id, "operator"),
        )
    ]
    assert lab.reads == [("assignment", (lab.workspace, "subtask", target_id))]
    assert lab.native_calls == []


@pytest.mark.parametrize("limit", [0, 101, True, "50"])
def test_profile_work_rejects_invalid_bound_without_query(query_lab, limit):
    with pytest.raises(AgentError) as error:
        query_lab.service.list_profile_work(query_lab.workspace, uuid4(), limit=limit)
    assert error.value.code == "configuration_invalid"
    assert query_lab.reads == query_lab.native_calls == []


def test_profile_work_cursor_is_scope_version_and_order_bound(query_lab):
    lab = query_lab
    views = import_module("sidera_control_plane.domain.assignment_views")
    profile_id = uuid4()
    ref = ProfileRef(workspace_id=lab.workspace, profile_id=profile_id, revision=3)
    rows = tuple(
        views.ProfileWorkRow(
            workspace_id=lab.workspace,
            work_id=uuid4(),
            kind="execution",
            title="Actual work",
            status="running",
            updated_at=datetime(2026, 10, 5, tzinfo=UTC),
            profile_ref=ref,
            execution_id=uuid4(),
        )
        for _ in range(2)
    )
    lab.store.list_profile_work = lambda *args: rows
    page = lab.service.list_profile_work(lab.workspace, profile_id, limit=1)
    assert page.items == rows[:1] and page.next_cursor is not None
    with pytest.raises(AgentError) as error:
        lab.service.list_profile_work(lab.workspace, uuid4(), after=page.next_cursor)
    assert error.value.code == "configuration_invalid"


def test_no_admitted_original_run_performs_zero_native_reads(query_lab):
    lab = query_lab
    result = lab.service.read_profile_work_run(lab.workspace, "execution", uuid4())
    assert result.available is False and result.run_id is None
    assert result.reason_code == "run_not_admitted"
    assert lab.native_calls == []


def read_store_module():
    name = "sidera_control_plane.persistence.assignment_read_store"
    assert util.find_spec(name) is not None, (
        "The CP-owned nonlocking read store is missing"
    )
    return import_module(name)


@pytest.mark.parametrize("exists", [True, False])
def test_actual_read_store_proves_target_existence_before_null_binding(
    monkeypatch, exists
):
    from sqlalchemy.orm import Session

    module = read_store_module()
    workspace, target_id = uuid4(), uuid4()
    statements, exits = [], []
    values = iter([target_id if exists else None, None])

    def scalar(session, statement):
        statements.append(statement)
        return next(values)

    @contextmanager
    def sessions():
        try:
            with Session() as session:
                yield session
        finally:
            exits.append(True)

    monkeypatch.setattr(Session, "scalar", scalar)
    store = module.PostgresAssignmentReadStore(sessions)
    if exists:
        assert store.read_assignment(workspace, "subtask", target_id) is None
    else:
        with pytest.raises(AgentError) as error:
            store.read_assignment(workspace, "subtask", target_id)
        assert error.value.code == "not_found"
    assert exits == [True]
    assert len(statements) == (2 if exists else 1)
    for statement in statements:
        compiled = statement.compile()
        assert (
            workspace in compiled.params.values()
            and target_id in compiled.params.values()
        )
        assert "FOR UPDATE" not in str(compiled)


def test_actual_profile_work_sql_is_bounded_scoped_and_three_distinct_kinds():
    module = read_store_module()
    workspace, profile_id = uuid4(), uuid4()
    statement = module.profile_work_statement(workspace, profile_id, 51, None)
    compiled = statement.compile()
    sql = str(compiled)
    assert sql.count("UNION ALL") == 2 and "FOR UPDATE" not in sql
    assert (
        workspace in compiled.params.values()
        and str(profile_id) in compiled.params.values()
    )
    assert 51 in compiled.params.values()
    assert all(
        kind in compiled.params.values()
        for kind in ("assignment", "execution", "planning")
    )
    assert "ORDER BY" in sql and "LIMIT" in sql


@pytest.mark.parametrize("column_name", ["planning_request_id", "planning_run_id"])
def test_profile_work_union_planning_identity_nulls_are_uuid(monkeypatch, column_name):
    from sqlalchemy import Uuid
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.engine import Engine

    def refuse_connection(*args, **kwargs):
        raise AssertionError("database_io_refused")

    monkeypatch.setattr(Engine, "connect", refuse_connection)
    statement = read_store_module().profile_work_statement(uuid4(), uuid4(), 51, None)
    branches = statement.get_final_froms()[0].element.selects
    assert len(branches) == 3
    for branch in branches:
        expression = branch.selected_columns[column_name]
        assert isinstance(expression.type, Uuid)
    for branch in branches[:2]:
        sql = str(
            branch.selected_columns[column_name].compile(dialect=postgresql.dialect())
        )
        assert sql == "CAST(NULL AS UUID)"


@pytest.mark.parametrize(
    "mismatch",
    [None, "workspace", "run", "profile", "effective_profile", "receipt", "binding"],
)
def test_actual_original_reader_closes_transaction_and_preserves_old_pins(
    query_lab, monkeypatch, mismatch
):
    from sidera_agents.contracts import (
        EffectiveConfig,
        RunSnapshot,
        RuntimeRef,
        StartReceipt,
    )
    from sqlalchemy.orm import Session

    from sidera_control_plane.persistence.models import (
        AgentBindingModel,
        AgentExecutionLinkModel,
        ExecutionAuthorityModel,
        ExecutionModel,
    )

    lab = query_lab
    execution_id, task_id, native_id, operation_id, authority_id = (
        uuid4() for _ in range(5)
    )
    old_ref = ProfileRef(workspace_id=lab.workspace, profile_id=uuid4(), revision=2)
    effective = EffectiveConfig(
        profile_ref=old_ref,
        config_hash="sha256:stored-original",
        runtime_ref=RuntimeRef(workspace_id=lab.workspace, runtime_id=uuid4()),
        actual_runtime_version="controlled-1",
        selected_model="selected-model",
        observed_model=None,
        inherited_fields=(),
        applied_fields=("instructions",),
        resource_refs=(),
        context_receipts=(),
    )
    snapshot = RunSnapshot(
        run_id=native_id,
        workspace_id=lab.workspace,
        profile_ref=old_ref,
        policy="bounded",
        state="running",
        input_open=True,
        active_response_id=None,
        effective_config=effective,
        last_event_sequence=4,
        capabilities={},
        reason_code=None,
    )
    receipt = StartReceipt(
        operation_id=operation_id, status="accepted", run_id=native_id, reason_code=None
    )
    values = iter(
        [
            ExecutionModel(
                id=execution_id,
                workspace_id=lab.workspace,
                task_id=task_id,
                lineage_id=execution_id,
                worker_item_id=None,
            ),
            AgentExecutionLinkModel(
                workspace_id=lab.workspace,
                execution_id=execution_id,
                profile_ref=old_ref.model_dump(mode="json"),
                start_operation_id=operation_id,
                authority_id=authority_id,
                run_id=native_id,
                binding_version=3,
                start_receipt=receipt.model_copy(
                    update={"operation_id": uuid4()} if mismatch == "receipt" else {}
                ).model_dump(mode="json"),
            ),
            ExecutionAuthorityModel(
                id=authority_id,
                workspace_id=lab.workspace,
                execution_id=execution_id,
                target_type="subtask",
                target_id=task_id,
                binding_version=3,
                policy={"profile_ref": old_ref.model_dump(mode="json")},
            ),
            AgentBindingModel(
                workspace_id=lab.workspace,
                target_type="subtask",
                target_id=task_id,
                version=3,
                is_current=False,
                profile_ref=(
                    old_ref.model_copy(update={"revision": 4})
                    if mismatch == "binding"
                    else old_ref
                ).model_dump(mode="json"),
            ),
        ]
    )
    exits = []

    @contextmanager
    def sessions():
        try:
            with Session() as session:
                yield session
        finally:
            exits.append(True)

    monkeypatch.setattr(Session, "scalar", lambda session, statement: next(values))
    lab.service._store = read_store_module().PostgresAssignmentReadStore(sessions)
    if mismatch == "workspace":
        snapshot = snapshot.model_copy(update={"workspace_id": uuid4()})
    elif mismatch == "run":
        snapshot = snapshot.model_copy(update={"run_id": uuid4()})
    elif mismatch == "profile":
        snapshot = snapshot.model_copy(
            update={"profile_ref": old_ref.model_copy(update={"revision": 4})}
        )
    elif mismatch == "effective_profile":
        snapshot = snapshot.model_copy(
            update={
                "effective_config": effective.model_copy(
                    update={"profile_ref": old_ref.model_copy(update={"revision": 4})}
                )
            }
        )

    def get_run(workspace_id, run_id, *, grant_id):
        assert exits == [True]
        assert (workspace_id, run_id, grant_id) == (
            lab.workspace,
            native_id,
            authority_id,
        )
        lab.native_calls.append(run_id)
        return snapshot

    lab.execution.get_run = get_run
    result = lab.service.read_profile_work_run(lab.workspace, "execution", execution_id)
    assert exits == [True]
    if mismatch in {"receipt", "binding"}:
        assert result.reason_code == "native_run_unavailable" and lab.native_calls == []
    elif mismatch is not None:
        assert result.available is False and result.reason_code == "native_run_mismatch"
    else:
        assert result.available is True and result.run.profile_ref == old_ref
        assert (
            result.run.selected_model == "selected-model"
            and result.run.observed_model is None
        )
        assert not any(
            key in result.model_dump_json()
            for key in ("grant_id", "authority_id", "start_request", "context_receipts")
        )


@pytest.mark.parametrize(
    "kind,target_type",
    [
        ("assignment", "subtask"),
        ("assignment", "worker_item"),
        ("execution", None),
        ("planning", None),
    ],
)
@pytest.mark.parametrize("denial", ["foreign", "revoked", "nonhuman"])
def test_original_summary_all_kinds_deny_before_opaque_lookup(
    query_lab, kind, target_type, denial
):
    lab = query_lab
    workspace = uuid4() if denial == "foreign" else lab.workspace
    if denial == "revoked":
        lab.authority.identity_for_grant = lambda supplied: None
    elif denial == "nonhuman":
        lab.authority.identity_for_grant = lambda supplied: replace(
            lab.identity, actor_type="agent"
        )
    with pytest.raises(AgentError) as error:
        lab.service.read_profile_work_run(
            workspace, kind, uuid4(), target_type=target_type
        )
    assert error.value.code == "permission_denied"
    assert lab.reads == lab.native_calls == []


@pytest.mark.parametrize(
    "kind,target_type",
    [
        ("assignment", None),
        ("assignment", "execution"),
        ("execution", "subtask"),
        ("planning", "worker_item"),
        ("other", None),
    ],
)
def test_original_summary_invalid_discriminator_precedes_reads(
    query_lab, kind, target_type
):
    with pytest.raises(AgentError) as error:
        query_lab.service.read_profile_work_run(
            query_lab.workspace, kind, uuid4(), target_type=target_type
        )
    assert error.value.code == "configuration_invalid"
    assert query_lab.calls == query_lab.reads == query_lab.native_calls == []


@pytest.mark.parametrize(
    "kind,target_type",
    [
        ("assignment", "subtask"),
        ("assignment", "worker_item"),
        ("execution", None),
        ("planning", None),
    ],
)
def test_original_summary_uses_public_initial_target(query_lab, kind, target_type):
    lab = query_lab
    views = import_module("sidera_control_plane.domain.assignment_views")
    work_id = uuid4()
    result = lab.service.read_profile_work_run(
        lab.workspace, kind, work_id, target_type=target_type
    )
    assert lab.calls[0] == (
        lab.grant,
        "assignment.read",
        views.ProfileWorkRunTarget(
            lab.workspace, kind, work_id, "operator", target_type
        ),
    )
    if kind != "planning":
        assert lab.calls[1] == (
            lab.grant,
            "assignment.read",
            AssignmentTarget(
                lab.workspace, target_type or "execution", work_id, "operator"
            ),
        )
    assert result.reason_code == "run_not_admitted" and lab.native_calls == []


@pytest.mark.parametrize(
    "fields",
    [
        {"workspace_id": "not-a-uuid"},
        {"work_id": "not-a-uuid"},
        {"principal_id": " "},
        {"kind": "other"},
        {"kind": "assignment"},
        {"target_type": "subtask"},
    ],
)
def test_public_work_run_target_strict_finite_shape(fields):
    from sidera_control_plane.domain.errors import (
        ValidationError as DomainValidationError,
    )

    views = import_module("sidera_control_plane.domain.assignment_views")
    values = {
        "workspace_id": uuid4(),
        "kind": "execution",
        "work_id": uuid4(),
        "principal_id": "operator",
    }
    values.update(fields)
    with pytest.raises(DomainValidationError):
        views.ProfileWorkRunTarget(**values)


@pytest.fixture
def original_rows(query_lab, monkeypatch):
    from sidera_agents.contracts import (
        EffectiveConfig,
        ExecutionWorkspace,
        RunSnapshot,
        RunStart,
        RuntimeRef,
        StartReceipt,
    )
    from sqlalchemy.orm import Session

    from sidera_control_plane.persistence.models import (
        AgentBindingModel,
        AgentExecutionLinkModel,
        ExecutionAuthorityModel,
        ExecutionModel,
        PlanningAgentLinkModel,
        PlanningAuthorityModel,
        PlanningRunModel,
        TaskModel,
        WorkerItemModel,
    )

    lab = query_lab
    views = import_module("sidera_control_plane.domain.assignment_views")
    ref = ProfileRef(workspace_id=lab.workspace, profile_id=uuid4(), revision=2)
    work_id, target_id, native_id, operation_id, authority_id, request_id = (
        uuid4() for _ in range(6)
    )
    effective = EffectiveConfig(
        profile_ref=ref,
        config_hash="sha256:old-pin",
        runtime_ref=RuntimeRef(workspace_id=lab.workspace, runtime_id=uuid4()),
        actual_runtime_version="controlled-1",
        selected_model=None,
        observed_model=None,
        inherited_fields=(),
        applied_fields=(),
        resource_refs=(),
        context_receipts=(),
    )
    snapshot = RunSnapshot(
        run_id=native_id,
        workspace_id=lab.workspace,
        profile_ref=ref,
        policy="bounded",
        state="running",
        input_open=True,
        active_response_id=None,
        effective_config=effective,
        last_event_sequence=3,
        capabilities={},
        reason_code=None,
    )
    receipt = StartReceipt(
        operation_id=operation_id, status="accepted", run_id=native_id, reason_code=None
    )
    start = RunStart(
        operation_id=operation_id,
        profile_ref=ref,
        grant_id=authority_id,
        policy="bounded",
        instructions=(),
        text="Saved input.",
        context=(),
        tools=(),
        workspace=ExecutionWorkspace(
            directory="/controlled/owned", writable=True, artifact_directory=None
        ),
        required_capabilities=("mcp",),
    )
    rows = {
        TaskModel: target_id,
        WorkerItemModel: target_id,
        ExecutionModel: ExecutionModel(
            id=work_id,
            workspace_id=lab.workspace,
            task_id=target_id,
            lineage_id=work_id,
            worker_item_id=None,
        ),
        AgentExecutionLinkModel: AgentExecutionLinkModel(
            workspace_id=lab.workspace,
            execution_id=work_id,
            profile_ref=ref.model_dump(mode="json"),
            start_operation_id=operation_id,
            authority_id=authority_id,
            run_id=native_id,
            binding_version=3,
            start_receipt=receipt.model_dump(mode="json"),
        ),
        ExecutionAuthorityModel: ExecutionAuthorityModel(
            id=authority_id,
            workspace_id=lab.workspace,
            execution_id=work_id,
            target_type="subtask",
            target_id=target_id,
            binding_version=3,
            policy={"profile_ref": ref.model_dump(mode="json")},
        ),
        AgentBindingModel: AgentBindingModel(
            workspace_id=lab.workspace,
            target_type="subtask",
            target_id=target_id,
            version=3,
            is_current=True,
            profile_ref=ref.model_dump(mode="json"),
        ),
        PlanningRunModel: PlanningRunModel(
            id=work_id,
            workspace_id=lab.workspace,
            planning_request_id=request_id,
            execution_mode="native_profile",
            planner_agent_ref=ref.model_dump(mode="json"),
            operation_id=operation_id,
        ),
        PlanningAgentLinkModel: PlanningAgentLinkModel(
            workspace_id=lab.workspace,
            planning_run_id=work_id,
            start_operation_id=operation_id,
            authority_id=authority_id,
            start_request=start.model_dump(mode="json"),
            run_id=native_id,
            start_receipt=receipt.model_dump(mode="json"),
        ),
        PlanningAuthorityModel: PlanningAuthorityModel(
            id=authority_id,
            workspace_id=lab.workspace,
            planning_run_id=work_id,
            planning_request_id=request_id,
            policy={
                "profile_ref": ref.model_dump(mode="json"),
                "operation_id": str(operation_id),
            },
        ),
    }
    statements, exits, candidates = [], [], [work_id]

    def scalar(session, statement):
        statements.append(statement)
        return rows[statement.column_descriptions[0]["entity"]]

    def scalars(session, statement):
        statements.append(statement)
        return SimpleNamespace(all=lambda: tuple(candidates))

    @contextmanager
    def sessions():
        try:
            with Session() as session:
                yield session
        finally:
            exits.append(True)

    monkeypatch.setattr(Session, "scalar", scalar)
    monkeypatch.setattr(Session, "scalars", scalars)
    lab.service._store = read_store_module().PostgresAssignmentReadStore(sessions)

    def get_run(workspace_id, run_id, *, grant_id):
        assert exits == [True]
        assert (workspace_id, run_id, grant_id) == (
            lab.workspace,
            native_id,
            authority_id,
        )
        lab.native_calls.append(run_id)
        return snapshot

    lab.execution.get_run = get_run
    return SimpleNamespace(
        lab=lab,
        views=views,
        rows=rows,
        statements=statements,
        exits=exits,
        candidates=candidates,
        ref=ref,
        work_id=work_id,
        target_id=target_id,
        native_id=native_id,
        operation_id=operation_id,
        authority_id=authority_id,
    )


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "policy_missing",
        "policy_profile",
        "receipt_busy",
        "receipt_rejected",
        "binding_version",
    ],
)
def test_execution_original_requires_saved_policy_and_accepted_receipt(
    original_rows, mismatch
):
    from sidera_control_plane.persistence.models import (
        AgentBindingModel,
        AgentExecutionLinkModel,
        ExecutionAuthorityModel,
    )

    fixture = original_rows
    authority = fixture.rows[ExecutionAuthorityModel]
    if mismatch == "policy_missing":
        authority.policy = {}
    elif mismatch == "policy_profile":
        authority.policy = {
            "profile_ref": fixture.ref.model_copy(update={"revision": 9}).model_dump(
                mode="json"
            )
        }
    elif mismatch in {"receipt_busy", "receipt_rejected"}:
        link = fixture.rows[AgentExecutionLinkModel]
        link.start_receipt = {
            **link.start_receipt,
            "status": mismatch.removeprefix("receipt_"),
        }
    elif mismatch == "binding_version":
        fixture.rows[AgentBindingModel].version = 4
    result = fixture.lab.service.read_profile_work_run(
        fixture.lab.workspace, "execution", fixture.work_id
    )
    assert result.available is (mismatch is None)
    assert fixture.lab.native_calls == ([fixture.native_id] if mismatch is None else [])


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "policy_missing",
        "policy_profile",
        "policy_operation",
        "run_operation",
        "run_profile",
        "start_grant",
        "start_operation",
        "receipt_busy",
    ],
)
def test_planning_original_preserves_saved_profile_operation_and_grant(
    original_rows, mismatch
):
    from sidera_control_plane.persistence.models import (
        PlanningAgentLinkModel,
        PlanningAuthorityModel,
        PlanningRunModel,
    )

    fixture = original_rows
    authority, link, run = (
        fixture.rows[model]
        for model in (PlanningAuthorityModel, PlanningAgentLinkModel, PlanningRunModel)
    )
    if mismatch == "policy_missing":
        authority.policy = {}
    elif mismatch == "policy_profile":
        authority.policy = {
            **authority.policy,
            "profile_ref": fixture.ref.model_copy(update={"revision": 9}).model_dump(
                mode="json"
            ),
        }
    elif mismatch == "policy_operation":
        authority.policy = {**authority.policy, "operation_id": str(uuid4())}
    elif mismatch == "run_operation":
        run.operation_id = uuid4()
    elif mismatch == "run_profile":
        run.planner_agent_ref = fixture.ref.model_copy(
            update={"revision": 9}
        ).model_dump(mode="json")
    elif mismatch in {"start_grant", "start_operation"}:
        field = "grant_id" if mismatch == "start_grant" else "operation_id"
        link.start_request = {**link.start_request, field: str(uuid4())}
    elif mismatch == "receipt_busy":
        link.start_receipt = {**link.start_receipt, "status": "busy"}
    result = fixture.lab.service.read_profile_work_run(
        fixture.lab.workspace, "planning", fixture.work_id
    )
    assert result.available is (mismatch is None)
    assert fixture.lab.native_calls == ([fixture.native_id] if mismatch is None else [])
    if mismatch is None:
        assert result.run.profile_ref == fixture.ref
        assert fixture.lab.calls[-1] == (
            fixture.lab.grant,
            "assignment.read",
            fixture.views.ProfileWorkTarget(
                fixture.lab.workspace, fixture.ref.profile_id, "operator"
            ),
        )
        assert (
            "authority_id" not in result.model_dump_json()
            and "grant_id" not in result.model_dump_json()
        )


def test_planning_derived_profile_denial_precedes_native_read(original_rows):
    fixture = original_rows

    def require(grant, action, target):
        if isinstance(target, fixture.views.ProfileWorkTarget):
            raise AgentError("permission_denied", "Denied.")
        fixture.lab.calls.append((grant, action, target))

    fixture.lab.authority.require = require
    with pytest.raises(AgentError) as error:
        fixture.lab.service.read_profile_work_run(
            fixture.lab.workspace, "planning", fixture.work_id
        )
    assert error.value.code == "permission_denied" and fixture.lab.native_calls == []
    assert fixture.exits == [True]


@pytest.mark.parametrize("kind", ["execution", "planning"])
def test_original_summary_missing_work_is_not_empty_success(original_rows, kind):
    from sidera_control_plane.persistence.models import ExecutionModel, PlanningRunModel

    fixture = original_rows
    fixture.rows[ExecutionModel if kind == "execution" else PlanningRunModel] = None
    with pytest.raises(AgentError) as error:
        fixture.lab.service.read_profile_work_run(
            fixture.lab.workspace, kind, fixture.work_id
        )
    assert error.value.code == "not_found" and fixture.lab.native_calls == []


@pytest.mark.parametrize("target_type", ["subtask", "worker_item"])
@pytest.mark.parametrize("case", ["single", "none", "multiple", "binding_version"])
def test_assignment_original_uses_exact_current_binding_and_bounded_candidates(
    original_rows, target_type, case
):
    from sidera_control_plane.persistence.models import (
        AgentBindingModel,
        ExecutionAuthorityModel,
        ExecutionModel,
    )

    fixture = original_rows
    fixture.rows[AgentBindingModel].target_type = target_type
    fixture.rows[ExecutionAuthorityModel].target_type = target_type
    if target_type == "worker_item":
        fixture.rows[ExecutionModel].worker_item_id = fixture.target_id
    if case == "none":
        fixture.candidates.clear()
    elif case == "multiple":
        fixture.candidates.append(uuid4())
    elif case == "binding_version":
        fixture.rows[ExecutionAuthorityModel].binding_version = 4
    result = fixture.lab.service.read_profile_work_run(
        fixture.lab.workspace, "assignment", fixture.target_id, target_type=target_type
    )
    assert result.available is (case == "single")
    assert fixture.lab.native_calls == ([fixture.native_id] if case == "single" else [])
    if case == "multiple":
        assert result.reason_code == "native_run_ambiguous"
    elif case == "none":
        assert result.reason_code == "run_not_admitted"
    sql = [statement.compile() for statement in fixture.statements]
    assert any(target_type in compiled.params.values() for compiled in sql)
    assert any(2 in compiled.params.values() for compiled in sql)


def test_profile_work_cursor_preserves_tied_assignment_target_types(query_lab):
    lab = query_lab
    views = import_module("sidera_control_plane.domain.assignment_views")
    profile_id, work_id = uuid4(), uuid4()
    ref = ProfileRef(workspace_id=lab.workspace, profile_id=profile_id, revision=2)
    rows = tuple(
        views.ProfileWorkRow(
            workspace_id=lab.workspace,
            work_id=work_id,
            kind="assignment",
            title="Pinned work",
            status="ready",
            updated_at=datetime(2026, 10, 5, tzinfo=UTC),
            profile_ref=ref,
            target_type=kind,
        )
        for kind in ("subtask", "worker_item")
    )
    lab.store.list_profile_work = lambda *args: rows
    page = lab.service.list_profile_work(lab.workspace, profile_id, limit=1)
    cursor = lab.module._decode_cursor(page.next_cursor, lab.workspace, profile_id)
    assert cursor.target_type == "subtask"
    statement = read_store_module().profile_work_statement(
        lab.workspace, profile_id, 2, cursor
    )
    compiled = statement.compile()
    assert "subtask" in compiled.params.values()
    assert "coalesce(profile_work.target_type" in str(compiled).lower()


@pytest.mark.parametrize(
    "root_source", [None, "revision", "goal_submission", "materialization"]
)
def test_actual_planning_work_uses_brief_objective_and_owning_root_sources(
    monkeypatch, root_source
):
    from sqlalchemy.orm import Session

    from sidera_control_plane.domain.planning import PlanningBriefRevision
    from sidera_control_plane.persistence.models import (
        PlanningRequestModel,
        ProposalMaterializationModel,
        TaskGoalSubmissionModel,
    )
    from sidera_control_plane.persistence.planning_repository import _brief_model

    module = read_store_module()
    workspace, profile_id, request_id, run_id, root_id = (uuid4() for _ in range(5))
    when = datetime(2026, 10, 5, tzinfo=UTC)
    brief = PlanningBriefRevision.create(
        planning_request_id=request_id,
        revision=2,
        objective="Controlled stored objective",
        constraints=(),
        acceptance_criteria=("Safe result",),
        context_refs=(),
        requested_outputs=("Proposal",),
        user_feedback=None,
        source_correlation=uuid4(),
        created_by="operator",
        created_at=when,
    )
    stored = _brief_model(brief, workspace)
    assert "title" not in stored.payload
    request = PlanningRequestModel(
        id=request_id,
        workspace_id=workspace,
        root_task_id=root_id if root_source == "revision" else None,
    )
    submission = (
        TaskGoalSubmissionModel(
            workspace_id=workspace, planning_request_id=request_id, root_task_id=root_id
        )
        if root_source == "goal_submission"
        else None
    )
    materialization = (
        ProposalMaterializationModel(
            workspace_id=workspace, planning_request_id=request_id, root_task_id=root_id
        )
        if root_source == "materialization"
        else None
    )
    ref = ProfileRef(workspace_id=workspace, profile_id=profile_id, revision=2)
    exits = []

    def execute(session, statement):
        compiled = statement.compile()
        assert (
            "objective" in compiled.params.values()
            and "title" not in compiled.params.values()
        )
        sql = str(compiled)
        for table in ("task_goal_submissions", "proposal_materializations"):
            assert f"LEFT OUTER JOIN {table}" in sql
            assert (
                f"{table}.workspace_id" in sql and f"{table}.planning_request_id" in sql
            )
        assert "planning_brief_revisions.revision = planning_runs.brief_revision" in sql
        assert "FOR UPDATE" not in sql and "LIMIT" in sql
        root = (
            request.root_task_id
            or (submission.root_task_id if submission is not None else None)
            or (materialization.root_task_id if materialization is not None else None)
        )
        row = {
            "workspace_id": workspace,
            "work_id": run_id,
            "kind": "planning",
            "title": stored.payload["objective"],
            "status": "queued",
            "updated_at": when,
            "profile_ref": ref.model_dump(mode="json"),
            "root_task_id": root,
            "planning_request_id": request_id,
            "planning_run_id": run_id,
            "run_id": None,
        }
        return SimpleNamespace(mappings=lambda: (row,))

    @contextmanager
    def sessions():
        try:
            with Session() as session:
                yield session
        finally:
            exits.append(True)

    monkeypatch.setattr(Session, "execute", execute)
    rows = module.PostgresAssignmentReadStore(sessions).list_profile_work(
        workspace, profile_id, 51, None
    )
    assert exits == [True] and len(rows) == 1
    assert (
        rows[0].title == brief.objective
        and rows[0].work_id == rows[0].planning_run_id == run_id
    )
    assert rows[0].root_task_id == (None if root_source is None else root_id)
    assert rows[0].planning_request_id == request_id and rows[0].profile_ref == ref


@pytest.mark.parametrize("kind", ["execution", "planning"])
@pytest.mark.parametrize(
    "status",
    ["unknown", "starting", "cleanup", "pending", "busy", "rejected", "malformed"],
)
def test_runless_original_uses_actual_owning_nonadmission_proof(
    original_rows, kind, status
):
    from sidera_agents.contracts import StartReceipt

    from sidera_control_plane.domain.agent_bindings import AgentExecutionLink
    from sidera_control_plane.domain.planning_authority import PlanningAgentLink
    from sidera_control_plane.persistence.models import (
        AgentExecutionLinkModel,
        PlanningAgentLinkModel,
    )

    fixture = original_rows
    link = fixture.rows[
        AgentExecutionLinkModel if kind == "execution" else PlanningAgentLinkModel
    ]
    link.run_id, link.event_cursor, link.start_receipt = None, 0, None
    link.request_sha256 = (
        None if kind == "execution" and status == "pending" else "a" * 64
    )
    link.status = {
        "cleanup": "closing" if kind == "execution" else "cleanup_pending",
        "busy": "waiting_capacity",
        "rejected": "failed" if kind == "execution" else "rejected",
        "malformed": "waiting_capacity",
    }.get(status, status)
    link.closed, link.response_id, link.reason_code = False, None, None
    if status in {"busy", "rejected", "malformed"}:
        receipt = StartReceipt(
            operation_id=uuid4() if status == "malformed" else fixture.operation_id,
            status="rejected" if status == "rejected" else "busy",
            run_id=None,
            reason_code="controlled",
        )
        link.start_receipt = receipt.model_dump(mode="json")
    if status != "malformed":
        receipt = (
            None
            if link.start_receipt is None
            else StartReceipt.model_validate(link.start_receipt)
        )
        if kind == "execution":
            proof = AgentExecutionLink(
                workspace_id=fixture.lab.workspace,
                execution_id=fixture.work_id,
                start_operation_id=fixture.operation_id,
                profile_ref=fixture.ref,
                run_id=None,
                event_cursor=0,
                status=link.status,
                authority_id=fixture.authority_id,
                binding_version=3,
                request_sha256=link.request_sha256,
                start_receipt=receipt,
            )
            known = proof.known_unadmitted
        else:
            from sidera_agents.contracts import RunStart

            proof = PlanningAgentLink(
                workspace_id=fixture.lab.workspace,
                planning_run_id=fixture.work_id,
                authority_id=fixture.authority_id,
                start_operation_id=fixture.operation_id,
                profile_ref=fixture.ref,
                start_request=RunStart.model_validate(link.start_request),
                request_sha256=link.request_sha256,
                status=link.status,
                start_receipt=receipt,
            )
            known = proof.not_admitted or (
                proof.status == "pending"
                and proof.event_cursor == 0
                and proof.start_receipt is None
            )
        assert known is (status in {"pending", "busy", "rejected"})
    result = fixture.lab.service.read_profile_work_run(
        fixture.lab.workspace, kind, fixture.work_id
    )
    expected = (
        "native_run_unavailable"
        if status == "malformed"
        else "run_not_admitted"
        if status in {"pending", "busy", "rejected"}
        else "run_admission_unconfirmed"
    )
    assert (
        result.available is False
        and result.run_id is None
        and result.reason_code == expected
    )
    assert fixture.lab.native_calls == [] and fixture.exits == [True]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_assignment_keeps_original_unknown_intention_in_bounded_selection(
    original_rows, ambiguous
):
    from sidera_control_plane.persistence.models import AgentExecutionLinkModel

    fixture = original_rows
    link = fixture.rows[AgentExecutionLinkModel]
    (
        link.run_id,
        link.start_receipt,
        link.event_cursor,
        link.status,
        link.request_sha256,
    ) = None, None, 0, "unknown", "a" * 64
    if ambiguous:
        fixture.candidates.append(uuid4())
    result = fixture.lab.service.read_profile_work_run(
        fixture.lab.workspace, "assignment", fixture.target_id, target_type="subtask"
    )
    assert result.reason_code == (
        "native_run_ambiguous" if ambiguous else "run_admission_unconfirmed"
    )
    assert result.available is False and fixture.lab.native_calls == []
    candidates = next(
        statement
        for statement in fixture.statements
        if statement.column_descriptions[0]["name"] == "execution_id"
    )
    compiled = candidates.compile()
    assert 2 in compiled.params.values()
    assert all(
        status in compiled.params.values()
        for status in ("pending", "waiting_capacity", "failed")
    )
    assert "coalesce" in str(compiled).lower() and " OR " in str(compiled)
