import importlib
import importlib.util
import os
import runpy
import tomllib
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sidera_agents.errors import AgentError

from sidera_control_plane.application.assignments import TrustedAssignmentIdentity
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.models import Base
from tests.unit.test_native_request_authority import NoNativeCalls, runtime_module

CP_ROOT = Path(__file__).resolve().parents[2]
BUSINESS_NAMES = (
    "scheduler",
    "activation",
    "context",
    "collaboration",
    "plan_transition",
    "result",
    "reconciler",
    "notification",
)
ALL_STARTUP_NAMES = ("owner", "planning", *BUSINESS_NAMES)
ALL_STARTUP_CLOSED = Counter(
    owner=1,
    planning_catalog=1,
    planning_database=1,
    scheduler_database=1,
    activation_database=1,
    context_database=1,
    context_catalog=1,
    collaboration_database=1,
    plan_transition_database=1,
    result_database=1,
    reconciler_database=1,
    notification_database=1,
)


def migration_resources():
    name = "sidera_control_plane.persistence.migration_resources"
    assert importlib.util.find_spec(name) is not None, (
        "Installed migration resource resolver is missing"
    )
    return importlib.import_module(name)


def test_cp_wheel_declares_migration_resources_and_resolves_expected_head():
    project = tomllib.loads((CP_ROOT / "pyproject.toml").read_text())
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"].get(
        "force-include"
    ) == {
        "alembic.ini": "sidera_control_plane/alembic.ini",
        "migrations": "sidera_control_plane/migrations",
    }
    config = migration_resources().migration_config()
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads()
    assert Path(scripts.dir).is_dir()


@pytest.mark.parametrize("maintenance", (False, True))
def test_consumer_failure_logs_only_name_and_class_with_throttle_and_retry(
    maintenance, caplog, monkeypatch
):
    import threading
    import time

    module = runtime_module()
    native = NoNativeCalls()
    runtime = module.create_control_plane_runtime(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused"
        ),
        native,
        native,
        native,
        native,
    )
    timestamps = iter((0.0, 1.0, 29.0, 30.0, 30.5, 31.0))
    clock = [0.0]
    real_monotonic = time.monotonic
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    finished, release = threading.Event(), threading.Event()
    calls = []
    if maintenance:
        runtime._closing = True

    def callback():
        clock[0] = next(timestamps)
        calls.append(clock[0])
        if len(calls) == 5:
            return
        if len(calls) == 6:
            finished.set()
            assert release.wait(1)
        raise ValueError(
            "PRIVATE SQL password grant prompt /user/path postgresql://secret"
        )

    caplog.set_level("WARNING", logger=module.__name__)
    runtime._launch("result", callback, 0.001, maintenance=maintenance)
    try:
        assert finished.wait(1)
        release.set()
        deadline = real_monotonic() + 1
        while len(caplog.records) < 3 and real_monotonic() < deadline:
            threading.Event().wait(0.001)
        runtime._stop.set()
        assert runtime.close(grace_seconds=1) is True
        records = [
            record for record in caplog.records if record.name == module.__name__
        ]
        assert len(records) == 3
        assert all(
            record.getMessage()
            == "control_plane_consumer_failed callback=result exception=ValueError"
            for record in records
        )
        assert all(
            record.exc_info is None and record.stack_info is None for record in records
        )
        assert calls[:6] == [0.0, 1.0, 29.0, 30.0, 30.5, 31.0]
        assert "PRIVATE" not in caplog.text and "postgresql" not in caplog.text
        assert not native.calls
    finally:
        release.set()
        runtime._stop.set()
        runtime.close(grace_seconds=1)


def test_migration_env_prefers_explicit_connection_to_hostile_parent_url(monkeypatch):
    import sqlalchemy
    from alembic import context

    connection = object()
    calls = []
    config = Config()
    config.attributes["connection"] = connection
    monkeypatch.setenv(
        "SIDERA_DATABASE_URL",
        "postgresql+psycopg://private:credential@not-owned/forbidden",
    )
    before = dict(os.environ)
    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(
        context, "configure", lambda **kwargs: calls.append(("configure", kwargs))
    )
    monkeypatch.setattr(context, "begin_transaction", lambda: nullcontext())
    monkeypatch.setattr(
        context, "run_migrations", lambda: calls.append(("migrations",))
    )

    def forbidden_engine(*args, **kwargs):
        pytest.fail("Explicit migration connection was replaced by an ambient engine")

    monkeypatch.setattr(sqlalchemy, "engine_from_config", forbidden_engine)
    runpy.run_path(str(CP_ROOT / "migrations" / "env.py"))
    assert calls[0] == (
        "configure",
        {"connection": connection, "target_metadata": Base.metadata},
    )
    assert calls[1] == ("migrations",)
    assert "credential" not in repr(config.get_main_option("sqlalchemy.url"))
    assert dict(os.environ) == before


@pytest.fixture
def schema_runtime(monkeypatch):
    module = runtime_module()
    calls = []
    schema = SimpleNamespace(heads=None, columns=None, unavailable=False)

    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return list(self.rows)

    class Connection:
        def execute(self, statement):
            sql = str(statement)
            calls.append(sql)
            if schema.unavailable:
                raise RuntimeError("PRIVATE DSN")
            if "alembic_version" in sql:
                return Rows(schema.heads or ())
            if "information_schema.columns" in sql:
                return Rows(schema.columns or ())
            pytest.fail("Unexpected schema query: " + sql)

    connection = Connection()

    class Engine:
        def connect(self):
            return nullcontext(connection)

        def begin(self):
            calls.append("begin_owned_migration")
            return nullcontext(connection)

    database = SimpleNamespace(
        engine=Engine(), dispose=lambda: calls.append("disposed")
    )
    monkeypatch.setattr(module, "Database", lambda url: database)
    native = NoNativeCalls()
    runtime = module.create_control_plane_runtime(
        Settings(database_url="postgresql+psycopg://unused"),
        native,
        native,
        native,
        native,
    )
    try:
        yield SimpleNamespace(
            runtime=runtime,
            schema=schema,
            calls=calls,
            native=native,
            connection=connection,
        )
    finally:
        runtime.close()


def test_public_native_migrate_uses_owned_connection_without_parent_mutation(
    schema_runtime, monkeypatch
):
    from alembic import command

    s = schema_runtime
    monkeypatch.setenv(
        "SIDERA_DATABASE_URL",
        "postgresql+psycopg://private:credential@not-owned/forbidden",
    )
    before = dict(os.environ)
    upgrades = []
    monkeypatch.setattr(
        command, "upgrade", lambda config, revision: upgrades.append((config, revision))
    )
    s.runtime.migrate()
    assert len(upgrades) == 1
    config, revision = upgrades[0]
    assert config.attributes["connection"] is s.connection and revision == "head"
    assert ScriptDirectory.from_config(config).get_heads()
    assert s.calls == ["begin_owned_migration"] and not s.native.calls
    assert dict(os.environ) == before


@pytest.mark.parametrize("bad", (None, "head", "columns", "unavailable"))
def test_public_readiness_checks_head_and_schema_without_mutation(schema_runtime, bad):
    s = schema_runtime
    s.schema.heads = ScriptDirectory.from_config(
        migration_resources().migration_config()
    ).get_heads()
    s.schema.columns = tuple(
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
    )
    if bad == "head":
        s.schema.heads = ("prior_or_unknown_head",)
    elif bad == "columns":
        s.schema.columns = ()
    elif bad == "unavailable":
        s.schema.unavailable = True
    assert s.runtime.readiness() is (bad is None)
    assert s.calls and all(sql.lstrip().startswith("SELECT ") for sql in s.calls)
    assert not s.native.calls


@pytest.mark.parametrize(
    "invalid",
    (
        "missing",
        "shape",
        "falsy_shape",
        "empty_principal",
        "grant",
        "user",
        "agent",
        "wrong_principal",
        "revoked",
        "mismatch",
        "lookup_error",
        "no_authorizer",
    ),
)
def test_public_startup_requires_current_explicit_system_authority_before_consumers(
    invalid, monkeypatch
):
    module = runtime_module()
    identity = TrustedAssignmentIdentity("operator", uuid4(), "system")
    supplied = identity
    if invalid == "missing":
        supplied = None
    elif invalid == "shape":
        supplied = {
            "principal_id": "operator",
            "grant_id": identity.grant_id,
            "actor_type": "system",
        }
    elif invalid == "falsy_shape":
        supplied = {}
    elif invalid == "empty_principal":
        supplied = replace(identity, principal_id=" ")
    elif invalid == "grant":
        supplied = replace(identity, grant_id="untrusted-uuid")
    elif invalid in {"user", "agent"}:
        supplied = replace(identity, actor_type=invalid)
    elif invalid == "wrong_principal":
        supplied = replace(identity, principal_id="other")
    lookups = []

    def lookup(grant_id):
        lookups.append(grant_id)
        if invalid == "lookup_error":
            raise RuntimeError("PRIVATE credential/DSN")
        if invalid == "revoked":
            return None
        return (
            replace(identity, grant_id=uuid4()) if invalid == "mismatch" else identity
        )

    authority = (
        None
        if invalid == "no_authorizer"
        else SimpleNamespace(identity_for_grant=lookup)
    )
    native = NoNativeCalls()
    runtime = module.create_control_plane_runtime(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
            local_actor_id="operator",
        ),
        native,
        native,
        native,
        native,
        authority,
        agent_runtimes=native,
    )
    assert not lookups and not native.calls
    owners = []

    def forbidden_owner(*args, **kwargs):
        owners.append(kwargs)
        pytest.fail("Invalid startup identity reached owner/consumer creation")

    monkeypatch.setattr(module, "build_gateway_owner", forbidden_owner, raising=False)
    try:
        with pytest.raises(AgentError) as denied:
            runtime.start_background(background_identity=supplied)
        assert denied.value.code == (
            "background_authority_required"
            if invalid == "missing"
            else "permission_denied"
        )
        assert "PRIVATE" not in str(denied.value)
        assert not owners and not native.calls
    finally:
        runtime.close()


@pytest.mark.parametrize("ambiguous", (False, True))
def test_public_execution_authorizer_only_accepts_exact_cp_owned_grant(
    ambiguous, monkeypatch
):
    module = runtime_module()
    native = NoNativeCalls()
    runtime = module.create_control_plane_runtime(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused"
        ),
        native,
        native,
        native,
        native,
    )
    lookups = []
    grant = uuid4()

    def lookup(family, supplied):
        lookups.append((family, supplied))
        return object() if ambiguous else None

    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(
            get=lambda supplied: lookup("worker", supplied)
        ),
        planning_authorities=SimpleNamespace(
            get=lambda supplied: lookup("planner", supplied)
        ),
    )
    monkeypatch.setattr(
        module,
        "SqlAlchemyUnitOfWork",
        lambda database, **kwargs: nullcontext(uow),
        raising=False,
    )
    try:
        authorizer = runtime.execution_authorizer
        assert not lookups and not native.calls
        with pytest.raises(AgentError) as denied:
            authorizer.require(grant, "run.read", uuid4())
        assert denied.value.code == "permission_denied"
        assert lookups == [("worker", grant), ("planner", grant)]
        assert not native.calls
    finally:
        runtime.close()


@pytest.fixture
def startup_case(monkeypatch, tmp_path, request):
    import threading

    module = runtime_module()
    identity = TrustedAssignmentIdentity("operator", uuid4(), "system")
    current = {identity.grant_id: identity}
    lookups, builds, closed, ticks = [], [], [], []
    observed = threading.Event()
    business_observed = threading.Event()
    heartbeat_observed = threading.Event()
    heartbeat_reads = []
    business, limits = {}, []

    def lookup(grant_id):
        lookups.append(grant_id)
        return current.get(grant_id)

    native = NoNativeCalls()
    human = replace(identity, actor_type="user", grant_id=uuid4())
    authority = SimpleNamespace(
        identity_for_grant=lookup,
        authenticate_request=lambda request: human,
        require=lambda *args: None,
    )
    alive = [True]
    source_calls = []

    def reader(family):
        from hashlib import sha256

        from sidera_agents.contracts import SourceContent

        def read(grant_id, ref):
            source_calls.append((family, grant_id, ref))
            return SourceContent(
                text=family,
                actual_version=ref.version,
                content_hash=sha256(family.encode()).hexdigest(),
            )

        return SimpleNamespace(read=read)

    def tick(family):
        def run(*args):
            assert alive[0]
            ticks.append(family)
            if {"worker_original", "planner_original"} <= set(ticks):
                observed.set()
            if set(BUSINESS_NAMES) <= set(ticks):
                business_observed.set()
            limits.append((family, args))
            return (0, 0, 0)

        return run

    owner = SimpleNamespace(
        gateway=SimpleNamespace(run_once=tick("gateway"), run_stops_once=tick("stops")),
        gateway_runtime=SimpleNamespace(
            owner_check=lambda: alive[0],
            worker_gateway=SimpleNamespace(),
            native_reconciler=SimpleNamespace(run_once=tick("worker_original")),
            native_source_reader=reader("worker"),
        ),
        input_runtime=SimpleNamespace(
            dispatcher=SimpleNamespace(run_once=tick("input"))
        ),
        handoff_runtime=SimpleNamespace(
            coordinator=SimpleNamespace(run_once=tick("handoff"))
        ),
        require_owner=lambda: None,
    )

    def owner_close():
        closed.append("owner")
        alive[0] = False

    owner.close = owner_close
    planning = SimpleNamespace(
        worker=SimpleNamespace(run_once=tick("planning")),
        runner=SimpleNamespace(),
        native_reconciler=SimpleNamespace(run_once=tick("planner_original")),
        native_source_reader=reader("planner"),
        catalog=SimpleNamespace(close=lambda: closed.append("planning_catalog")),
        database=SimpleNamespace(dispose=lambda: closed.append("planning_database")),
    )

    def build_owner(settings, **kwargs):
        builds.append(("owner", kwargs))
        assert kwargs["use_acceptance_guard"] is False
        return owner

    def build_planning(settings, **kwargs):
        builds.append(("planning", kwargs))
        assert kwargs["use_acceptance_guard"] is False
        assert kwargs["owner_check"] is owner.gateway_runtime.owner_check
        return planning

    monkeypatch.setattr(module, "build_gateway_owner", build_owner, raising=False)
    monkeypatch.setattr(module, "build_planning_worker", build_planning, raising=False)
    for name, factory, service_name in (
        ("scheduler", "build_scheduler", "scheduler"),
        ("activation", "build_activation_worker", "worker"),
        ("context", "build_context_package_worker", "worker"),
        ("collaboration", "build_collaboration_worker", "worker"),
        ("plan_transition", "build_plan_transition_worker", "coordinator"),
        ("result", "build_result_worker", "worker"),
        ("reconciler", "build_reconciler", "reconciler"),
        ("notification", "build_notification_worker", "worker"),
    ):

        def build_business(settings, *, name=name, service_name=service_name, **kwargs):
            builds.append((name, kwargs))
            assert kwargs["owner_check"] is owner.gateway_runtime.owner_check
            if name == "scheduler":
                assert kwargs["native_services"] is builds[0][1]["native_services"]
            handle = SimpleNamespace(
                database=SimpleNamespace(
                    dispose=lambda: closed.append(name + "_database")
                )
            )
            setattr(handle, service_name, SimpleNamespace(run_once=tick(name)))
            if name == "context":
                handle.catalog = SimpleNamespace(
                    close=lambda: closed.append("context_catalog")
                )
            if name == "collaboration":
                handle.recovery_sweep = lambda now: tick("collaboration_recovery")(now)
            business[name] = handle
            return handle

        monkeypatch.setattr(module, factory, build_business, raising=False)
    empty = SimpleNamespace(list_reconciliation_links=lambda *args, **kwargs: ())
    uow = SimpleNamespace(execution_authorities=empty, planning_authorities=empty)
    monkeypatch.setattr(
        module, "SqlAlchemyUnitOfWork", lambda *args, **kwargs: nullcontext(uow)
    )

    class EmptyOwnedSlots:
        def __init__(self, factory):
            pass

        def owned_slots(self):
            heartbeat_reads.append((runtime._starting, runtime._closing))
            heartbeat_observed.set()
            return ()

    monkeypatch.setattr(
        module, "SavedNativeWorkerSlots", EmptyOwnedSlots, raising=False
    )

    class EmptyOwnedVerifications:
        def __init__(self, factory):
            pass

        def page(self, *, after, limit):
            return ()

        def has_outstanding(self):
            return False

    monkeypatch.setattr(
        module, "SavedNativeVerificationSessions", EmptyOwnedVerifications
    )
    workspace_id = uuid4()
    settings = Settings(
        database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
        local_actor_id="operator",
        gateway_sync_workspace_id=None
        if getattr(request, "param", "explicit") is None
        else workspace_id,
        planning_temp_root=tmp_path / "planning",
        gateway_poll_interval_seconds=0.01,
        planning_poll_interval_seconds=0.01,
        scheduler_poll_interval_seconds=0.01,
        activation_poll_interval_seconds=0.01,
        collaboration_poll_seconds=0.01,
        plan_transition_poll_interval_seconds=0.01,
        reconciler_poll_interval_seconds=0.01,
        notification_poll_seconds=0.01,
        collaboration_recovery_sweep_seconds=1,
        collaboration_workspace_id=uuid4(),
        notification_enabled=getattr(request, "param", "explicit")
        != "notifications_disabled",
        assignment_lease_renew_interval_seconds=0.01,
    )
    runtime = module.create_control_plane_runtime(
        settings,
        native,
        native,
        native,
        native,
        authority,
        agent_runtimes=native,
    )
    case = SimpleNamespace(
        runtime=runtime,
        identity=identity,
        authority=authority,
        workspace_id=workspace_id,
        current=current,
        lookups=lookups,
        native=native,
        builds=builds,
        closed=closed,
        alive=alive,
        ticks=ticks,
        observed=observed,
        source_calls=source_calls,
        business=business,
        business_observed=business_observed,
        limits=limits,
        heartbeat_reads=heartbeat_reads,
        heartbeat_observed=heartbeat_observed,
    )
    try:
        yield case
    finally:
        runtime.close()


def test_public_heartbeat_uses_normal_gate_and_discovers_membership_after_startup(
    startup_case,
):
    import threading

    s = startup_case
    assert s.heartbeat_reads == []
    s.runtime.start_background(background_identity=s.identity)
    assert s.heartbeat_observed.wait(1), (
        "Owned slots are not maintained by public startup"
    )
    assert all(state == (False, False) for state in s.heartbeat_reads)
    with s.runtime._start_admission():
        assert s.runtime.close(grace_seconds=0) is False
        before = len(s.heartbeat_reads)
        threading.Event().wait(0.04)
        assert len(s.heartbeat_reads) == before
    assert s.runtime.close() is True
    assert not s.native.calls


def test_public_startup_shares_exact_host_services_and_is_currently_idempotent(
    startup_case,
):
    s = startup_case
    assert not s.lookups and not s.builds and not s.native.calls
    s.runtime.start_background(background_identity=s.identity)
    assert tuple(family for family, _ in s.builds) == ALL_STARTUP_NAMES
    services = s.builds[0][1]["native_services"]
    assert s.builds[1][1]["native_services"] is services
    assert (
        services.execution
        is services.catalog
        is services.runtimes
        is services.observations
        is s.native
    )
    assert (services.principal_id, services.catalog_grant_id) == (
        "operator",
        s.identity.grant_id,
    )
    assert services.authorizer is s.authority
    assert s.observed.wait(1), (
        "Existing original-operation consumers were not scheduled"
    )
    for supplied in (None, s.identity):
        s.runtime.start_background(background_identity=supplied)
    assert len(s.builds) == 10 and len(s.lookups) >= 3
    assert all(grant == s.identity.grant_id for grant in s.lookups)
    assert not s.native.calls


def test_native_http_services_dependency_stays_inert_until_public_startup(
    monkeypatch,
    request,
):
    from fastapi import Depends
    from sqlalchemy.engine import Engine

    from sidera_control_plane.api.dependencies import get_native_worker_services
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeWorkerServices,
    )

    connects, resolved = [], []

    def reject_connect(*args, **kwargs):
        connects.append(True)
        pytest.fail("Native HTTP service resolution attempted a database connection")

    monkeypatch.setattr(Engine, "connect", reject_connect)
    s = request.getfixturevalue("startup_case")

    @s.runtime.app.get("/api/v1/workspaces/{workspace_id}/native-services-probe")
    def read_services(
        services: Annotated[
            NativeWorkerServices | None, Depends(get_native_worker_services)
        ],
    ):
        resolved.append(services)
        return {"available": services is not None}

    path = f"/api/v1/workspaces/{s.workspace_id}/native-services-probe"
    assert not s.lookups and not s.builds and not s.native.calls and not connects
    with TestClient(s.runtime.app) as client:
        cold = client.get(path)
        assert cold.status_code == 200 and cold.json() == {"available": False}
        assert resolved == [None]
        assert not s.lookups and not s.builds and not s.native.calls and not connects
        s.runtime.start_background(background_identity=s.identity)
        started = client.get(path)
        assert started.status_code == 200 and started.json() == {"available": True}
        assert resolved[1] is s.builds[0][1]["native_services"]
    assert not s.native.calls and not connects


def test_native_http_services_dependency_uses_exact_started_owner_service(
    startup_case,
    monkeypatch,
):
    from fastapi import Depends
    from sqlalchemy.engine import Engine

    from sidera_control_plane.api.dependencies import get_native_worker_services
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeWorkerServices,
    )

    def reject_connect(*args, **kwargs):
        pytest.fail("Native HTTP service resolution attempted a database connection")

    monkeypatch.setattr(Engine, "connect", reject_connect)
    s = startup_case
    resolved = []

    @s.runtime.app.get("/api/v1/workspaces/{workspace_id}/native-services-probe")
    def read_services(
        services: Annotated[
            NativeWorkerServices | None, Depends(get_native_worker_services)
        ],
    ):
        resolved.append(services)
        return {"available": services is not None}

    s.runtime.start_background(background_identity=s.identity)
    services = s.builds[0][1]["native_services"]
    with TestClient(s.runtime.app) as client:
        response = client.get(
            f"/api/v1/workspaces/{s.workspace_id}/native-services-probe"
        )
        assert response.status_code == 200 and response.json() == {"available": True}
    assert resolved[0] is services
    assert s.builds[1][1]["native_services"] is services
    assert (
        next(options for name, options in s.builds if name == "scheduler")[
            "native_services"
        ]
        is services
    )
    assert (
        services.execution
        is services.catalog
        is services.observations
        is services.runtimes
        is s.native
    )
    assert not s.native.calls


def test_native_http_services_dependency_reuses_owner_and_keeps_default_inert(
    startup_case,
    monkeypatch,
):
    from fastapi import Depends, FastAPI
    from sqlalchemy.engine import Engine

    from sidera_control_plane.api.dependencies import get_native_worker_services
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeWorkerServices,
    )

    def reject_connect(*args, **kwargs):
        pytest.fail("Native HTTP service resolution attempted a database connection")

    monkeypatch.setattr(Engine, "connect", reject_connect)
    s = startup_case
    resolved = []

    @s.runtime.app.get("/api/v1/workspaces/{workspace_id}/native-services-probe")
    def read_services(
        services: Annotated[
            NativeWorkerServices | None, Depends(get_native_worker_services)
        ],
    ):
        resolved.append(services)
        return {"available": services is not None}

    s.runtime.start_background(background_identity=s.identity)
    services = s.builds[0][1]["native_services"]
    with TestClient(s.runtime.app) as client:
        for supplied in (None, s.identity):
            s.runtime.start_background(background_identity=supplied)
            response = client.get(
                f"/api/v1/workspaces/{s.workspace_id}/native-services-probe"
            )
            assert response.status_code == 200 and response.json() == {
                "available": True
            }
    assert len(resolved) == 2 and all(value is services for value in resolved)
    assert tuple(name for name, _ in s.builds) == ALL_STARTUP_NAMES
    default_app = FastAPI()

    @default_app.get("/native-services-probe")
    def read_default_services(
        services: Annotated[
            NativeWorkerServices | None, Depends(get_native_worker_services)
        ],
    ):
        return {"available": services is not None}

    with TestClient(default_app) as client:
        response = client.get("/native-services-probe")
        assert response.status_code == 200 and response.json() == {"available": False}
    assert get_native_worker_services() is None and not s.native.calls


@pytest.mark.parametrize("startup_case", (None,), indirect=True)
def test_public_runtime_observes_persisted_originals_without_legacy_workspace_hint(
    startup_case,
):
    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    assert s.observed.wait(1)
    assert {"worker_original", "planner_original"} <= set(s.ticks)
    fence = s.builds[0][1].get("assignment_claim_check")
    assert callable(fence) and fence()
    permit = s.builds[0][1]["native_services"].start_admission
    with permit():
        assert s.runtime.close(grace_seconds=0) is False
        assert fence() is False and s.alive[0]
    assert s.runtime.close() is True
    assert not s.native.calls


def test_public_startup_cannot_replace_or_reuse_revoked_background_identity(
    startup_case,
):
    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    replacement = replace(s.identity, grant_id=uuid4())
    s.current[replacement.grant_id] = replacement
    with pytest.raises(AgentError) as conflict:
        s.runtime.start_background(background_identity=replacement)
    assert conflict.value.code == "operation_conflict"
    s.current[s.identity.grant_id] = None
    with pytest.raises(AgentError) as denied:
        s.runtime.start_background()
    assert denied.value.code == "permission_denied"
    assert len(s.builds) == 10 and s.alive[0] and not s.closed and not s.native.calls


def test_runtime_permit_revalidates_without_disabling_original_owner(startup_case):
    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    permit = s.builds[0][1]["native_services"].start_admission
    with permit():
        assert s.alive[0]
    s.current[s.identity.grant_id] = None
    with pytest.raises(AgentError) as denied, permit():
        pytest.fail("Revoked background identity received a start permit")
    assert denied.value.code == "permission_denied"
    assert s.alive[0] and not s.closed and not s.native.calls


def test_runtime_close_fences_next_permit_and_retains_admitted_work(startup_case):
    import time

    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    permit = s.builds[0][1]["native_services"].start_admission
    with permit():
        started = time.monotonic()
        assert s.runtime.close(grace_seconds=0.02) is False
        assert time.monotonic() - started < 0.5
        assert s.alive[0] and not s.closed
        with pytest.raises(AgentError) as denied, permit():
            pytest.fail("Closing runtime received a new native admission")
        assert denied.value.code == "control_plane_closed"
    assert s.runtime.close() is True
    assert s.runtime.close() is True
    assert Counter(s.closed) == ALL_STARTUP_CLOSED
    assert not s.native.calls


def test_runtime_closing_keeps_authorized_reads_but_fences_mutations(startup_case):
    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    business = []

    @s.runtime.app.get("/api/v1/workspaces/{workspace_id}/lifecycle-probe")
    def read_probe():
        business.append("read")
        return {"status": "available"}

    @s.runtime.app.post("/api/v1/workspaces/{workspace_id}/lifecycle-probe")
    def mutate_probe():
        business.append("write")
        return {"status": "changed"}

    route = f"/api/v1/workspaces/{s.workspace_id}/lifecycle-probe"
    permit = s.builds[0][1]["native_services"].start_admission
    with TestClient(s.runtime.app) as client:
        with permit():
            assert s.runtime.close(grace_seconds=0) is False
            assert client.get(route).json() == {"status": "available"}
            denied = client.post(route)
            assert denied.status_code == 503
            assert denied.json()["error"]["code"] == "control_plane_closed"
            assert business == ["read"] and s.alive[0]
        assert s.runtime.close() is True
        assert client.get(route).status_code == 503
    assert business == ["read"] and not s.native.calls


@pytest.mark.parametrize("method", ("GET", "POST"))
@pytest.mark.parametrize("hold_cleanup", (False, True))
def test_protected_http_lifetime_retains_owned_resources_through_cleanup(
    startup_case, method, hold_cleanup
):
    import threading

    from fastapi import Depends

    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    entered, release = threading.Event(), threading.Event()
    responses, calls = [], []

    def cleanup():
        try:
            yield
        finally:
            if hold_cleanup:
                entered.set()
                assert release.wait(2)

    def handler():
        calls.append(method)
        if not hold_cleanup:
            entered.set()
            assert release.wait(2)
        return {"status": "finished"}

    s.runtime.app.add_api_route(
        "/api/v1/workspaces/{workspace_id}/held-request",
        handler,
        methods=[method],
        dependencies=[Depends(cleanup)],
    )

    @s.runtime.app.get("/api/v1/workspaces/{workspace_id}/closing-read")
    def read():
        return {"status": "available"}

    path = f"/api/v1/workspaces/{s.workspace_id}/held-request"
    with TestClient(s.runtime.app) as client:
        request = threading.Thread(
            target=lambda: responses.append(client.request(method, path))
        )
        request.start()
        try:
            assert entered.wait(2)
            assert s.runtime.close(grace_seconds=0.02) is False
            assert not s.closed and s.alive[0]
            assert client.post(path).status_code == 503
            assert (
                client.get(
                    f"/api/v1/workspaces/{s.workspace_id}/closing-read"
                ).status_code
                == 200
            )
            assert calls == [method]
        finally:
            release.set()
            request.join(timeout=2)
        assert not request.is_alive() and responses[0].status_code == 200
        assert s.runtime._active_work == s.runtime._regular_work == 0
        assert s.runtime.close() is True and s.runtime.close() is True
    assert Counter(s.closed) == ALL_STARTUP_CLOSED and not s.native.calls


@pytest.fixture
def native_projection_stream(startup_case, monkeypatch):
    import threading

    from sidera_control_plane.api import projection_stream
    from sidera_control_plane.api.dependencies import get_database

    s = startup_case
    entered, abort = threading.Event(), threading.Event()
    responses, failures, database_disposals = [], [], []
    sessions = SimpleNamespace(open=0, opened=0)
    boundary = object()

    @contextmanager
    def session():
        assert s.alive[0] and not s.closed and not database_disposals
        sessions.open += 1
        sessions.opened += 1
        try:
            yield boundary
        finally:
            sessions.open -= 1

    class EmptyChanges:
        def __init__(self, session, *, retention_seconds):
            assert session is boundary
            assert (
                retention_seconds
                == s.runtime._settings.projection_change_retention_seconds
            )

        def cursor_expired(self, workspace_id, cursor):
            assert (workspace_id, cursor) == (s.workspace_id, 0)
            return abort.is_set()

        def list_after(self, workspace_id, *, cursor, limit):
            assert (workspace_id, cursor, limit) == (s.workspace_id, 0, 100)
            entered.set()
            return ()

    s.runtime.app.dependency_overrides[get_database] = lambda: SimpleNamespace(
        session=session
    )
    monkeypatch.setattr(projection_stream, "ProjectionChangeRepository", EmptyChanges)
    monkeypatch.setattr(
        s.runtime._settings, "projection_stream_poll_interval_seconds", 0.01
    )
    dispose = s.runtime._database.dispose

    def record_disposal():
        assert sessions.open == 0
        assert s.runtime._active_work == s.runtime._regular_work == 0
        assert all(not thread.is_alive() for thread in s.runtime._threads)
        database_disposals.append(True)
        dispose()

    monkeypatch.setattr(s.runtime._database, "dispose", record_disposal)
    s.runtime.start_background(background_identity=s.identity)
    with TestClient(s.runtime.app) as client:

        def read():
            try:
                responses.append(
                    client.get(
                        f"/api/v1/workspaces/{s.workspace_id}/projection-changes"
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - Forward joined reader failures.
                failures.append(exc)

        reader = threading.Thread(target=read, name="test-native-projection-stream")
        reader.start()
        try:
            yield SimpleNamespace(
                case=s,
                entered=entered,
                abort=abort,
                reader=reader,
                responses=responses,
                failures=failures,
                sessions=sessions,
                database_disposals=database_disposals,
            )
        finally:
            # Settle an unchanged producer after the intended RED assertion fails.
            abort.set()
            reader.join(timeout=2)
            assert not reader.is_alive() and not failures
            assert s.runtime.close(grace_seconds=1) is True
            assert all(not thread.is_alive() for thread in s.runtime._threads)


def test_close_ends_native_projection_stream_before_disposal(native_projection_stream):
    stream = native_projection_stream
    s = stream.case
    assert stream.entered.wait(2), "Projection route did not poll its repository"
    assert stream.reader.is_alive() and not stream.responses and not stream.failures
    assert s.alive[0] and not s.closed and not stream.database_disposals
    with s.runtime._condition:
        assert s.runtime._regular_work >= 1

    assert s.runtime.close(grace_seconds=1) is True

    stream.reader.join(timeout=2)
    assert not stream.reader.is_alive() and not stream.failures
    response = stream.responses[0]
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.text == ": connected\n\n"
    assert not stream.abort.is_set()
    assert stream.sessions.open == 0 and stream.sessions.opened >= 1
    assert all(not thread.is_alive() for thread in s.runtime._threads)
    assert Counter(s.closed) == ALL_STARTUP_CLOSED
    assert stream.database_disposals == [True] and not s.native.calls
    assert s.runtime.close(grace_seconds=1) is True
    assert Counter(s.closed) == ALL_STARTUP_CLOSED
    assert stream.database_disposals == [True] and not s.native.calls


def test_close_ends_projection_stream_but_retains_outstanding_original(
    native_projection_stream, monkeypatch
):
    import threading

    stream = native_projection_stream
    s = stream.case
    original_drained = threading.Event()
    monkeypatch.setattr(
        s.runtime._verifications,
        "has_outstanding",
        lambda: not original_drained.is_set(),
    )
    try:
        assert stream.entered.wait(2), "Projection route did not poll its repository"
        assert stream.reader.is_alive() and not stream.responses and not stream.failures
        assert s.alive[0] and not s.closed and not stream.database_disposals

        assert s.runtime.close(grace_seconds=0.1) is False

        stream.reader.join(timeout=2)
        assert not stream.reader.is_alive() and not stream.failures
        response = stream.responses[0]
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert response.text == ": connected\n\n"
        assert not stream.abort.is_set() and stream.sessions.open == 0
        with s.runtime._condition:
            assert s.runtime._regular_work == 0
        assert not s.runtime._stop.is_set() and not s.runtime._drained.is_set()
        assert not s.runtime._closed and not s.runtime._disposal_complete
        assert s.alive[0] and not s.closed and not stream.database_disposals
        assert not s.native.calls

        original_drained.set()
        assert s.runtime.close(grace_seconds=1) is True
        assert all(not thread.is_alive() for thread in s.runtime._threads)
        assert Counter(s.closed) == ALL_STARTUP_CLOSED
        assert stream.database_disposals == [True] and not s.native.calls
        assert s.runtime.close(grace_seconds=1) is True
        assert Counter(s.closed) == ALL_STARTUP_CLOSED
        assert stream.database_disposals == [True] and not s.native.calls
    finally:
        original_drained.set()


def test_closing_wins_authenticated_pre_admission_gap_without_business(startup_case):
    import threading

    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    entered, release = threading.Event(), threading.Event()
    responses, calls = [], []

    def require(grant, action, target):
        if action == "control_plane.query":
            entered.set()
            assert release.wait(2)

    s.authority.require = require

    @s.runtime.app.get("/api/v1/workspaces/{workspace_id}/admission-gap")
    def handler():
        calls.append(True)
        return {"status": "finished"}

    with TestClient(s.runtime.app) as client:
        request = threading.Thread(
            target=lambda: responses.append(
                client.get(f"/api/v1/workspaces/{s.workspace_id}/admission-gap")
            )
        )
        request.start()
        try:
            assert entered.wait(2)
            assert s.runtime.close() is True
        finally:
            release.set()
            request.join(timeout=2)
    assert not request.is_alive() and responses[0].status_code == 503
    assert not calls and s.runtime._active_work == s.runtime._regular_work == 0
    assert Counter(s.closed) == ALL_STARTUP_CLOSED and not s.native.calls


def test_protected_http_exception_releases_work_before_repeated_close(startup_case):
    s = startup_case
    s.runtime.start_background(background_identity=s.identity)

    @s.runtime.app.get("/api/v1/workspaces/{workspace_id}/failed-request")
    def handler():
        raise RuntimeError("controlled handler failure")

    with (
        TestClient(s.runtime.app) as client,
        pytest.raises(RuntimeError, match="controlled handler failure"),
    ):
        client.get(f"/api/v1/workspaces/{s.workspace_id}/failed-request")
    assert s.runtime._active_work == s.runtime._regular_work == 0
    assert s.runtime.close() is True and s.runtime.close() is True
    assert Counter(s.closed) == ALL_STARTUP_CLOSED and not s.native.calls


@pytest.mark.parametrize("invalid", (True, False, -1, float("nan"), float("inf"), "1"))
def test_runtime_close_rejects_invalid_grace_without_fencing_startup(
    startup_case, invalid
):
    s = startup_case
    with pytest.raises(ValueError):
        s.runtime.close(grace_seconds=invalid)
    s.runtime.start_background(background_identity=s.identity)
    assert s.alive[0] and len(s.builds) == 10


def test_runtime_close_before_start_and_repeat_are_true_without_native_calls(
    schema_runtime,
):
    s = schema_runtime
    assert s.runtime.close(grace_seconds=0) is True
    assert s.runtime.close() is True
    assert s.calls == ["disposed"] and not s.native.calls


def test_close_signals_drained_maintenance_before_joining_consumers(startup_case):
    import threading

    s = startup_case
    entered, release = threading.Event(), threading.Event()
    stopped, exited = threading.Event(), threading.Event()
    exit_resources = []

    def probe():
        entered.set()
        while not release.is_set():
            if s.runtime._stop.wait(0.01):
                stopped.set()
                break
        exit_resources.append((s.alive[0], tuple(s.closed)))
        exited.set()

    try:
        s.runtime.start_background(background_identity=s.identity)
        s.runtime._launch("quiescence-probe", probe, 0.01, maintenance=True)
        assert entered.wait(1)
        with s.runtime._condition:
            assert s.runtime._condition.wait_for(
                lambda: not s.runtime._regular_work, timeout=1
            )
            s.runtime._check_drain()
            assert s.runtime._drained.is_set()
        result = s.runtime.close(grace_seconds=1)
        if result is False:
            assert s.runtime._drained.is_set()
            assert not s.runtime._stop.is_set() and not exited.is_set()
            assert s.alive[0] and not s.closed
        assert result is True
        assert stopped.is_set() and exited.is_set()
        assert exit_resources == [(True, ())]
        assert all(not thread.is_alive() for thread in s.runtime._threads)
        assert Counter(s.closed) == ALL_STARTUP_CLOSED and not s.native.calls
        assert s.runtime.close(grace_seconds=1) is True
        assert Counter(s.closed) == ALL_STARTUP_CLOSED and not s.native.calls
    finally:
        release.set()
        s.runtime._stop.set()
        s.runtime.close(grace_seconds=1)
        assert all(not thread.is_alive() for thread in s.runtime._threads)


def test_close_retains_maintenance_until_original_verification_drains(
    startup_case, monkeypatch
):
    import threading

    s = startup_case
    entered, release = threading.Event(), threading.Event()
    stopped, exited = threading.Event(), threading.Event()
    original_drained = threading.Event()
    monkeypatch.setattr(
        s.runtime._verifications,
        "has_outstanding",
        lambda: not original_drained.is_set(),
    )

    def probe():
        entered.set()
        while not release.is_set():
            if s.runtime._stop.wait(0.01):
                stopped.set()
                break
        exited.set()

    try:
        s.runtime.start_background(background_identity=s.identity)
        s.runtime._launch("quiescence-probe", probe, 0.01, maintenance=True)
        assert entered.wait(1)
        s.runtime._check_drain()
        assert not s.runtime._drained.is_set()
        assert s.runtime.close(grace_seconds=0.02) is False
        assert not s.runtime._stop.is_set() and not stopped.is_set()
        assert not exited.is_set() and s.alive[0] and not s.closed
        assert not s.native.calls
        original_drained.set()
        with s.runtime._condition:
            assert s.runtime._condition.wait_for(
                lambda: not s.runtime._regular_work, timeout=1
            )
            s.runtime._check_drain()
            assert s.runtime._drained.is_set()
        assert s.runtime.close(grace_seconds=1) is True
        assert stopped.is_set() and exited.is_set()
        assert all(not thread.is_alive() for thread in s.runtime._threads)
        assert Counter(s.closed) == ALL_STARTUP_CLOSED and not s.native.calls
        assert s.runtime.close(grace_seconds=1) is True
        assert Counter(s.closed) == ALL_STARTUP_CLOSED and not s.native.calls
    finally:
        original_drained.set()
        release.set()
        s.runtime._stop.set()
        s.runtime.close(grace_seconds=1)
        assert all(not thread.is_alive() for thread in s.runtime._threads)


def test_close_does_not_report_complete_while_first_owned_disposer_is_held(
    startup_case, monkeypatch
):
    import threading

    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    entered, release = threading.Event(), threading.Event()
    results, failures = [], []
    dispose = s.runtime._dispose_business

    def held(cleanup):
        entered.set()
        assert release.wait(2)
        dispose(cleanup)

    def close():
        try:
            results.append(s.runtime.close(grace_seconds=1))
        except BaseException as exc:  # noqa: BLE001 - Forward thread failures to the joined caller.
            failures.append(exc)

    monkeypatch.setattr(s.runtime, "_dispose_business", held)
    caller = threading.Thread(target=close, name="test-held-disposal")
    with TestClient(s.runtime.app) as client:
        caller.start()
        try:
            assert entered.wait(1)
            assert s.runtime.close(grace_seconds=0.02) is False
            assert not s.closed and s.alive[0]
            denied = client.get(f"/api/v1/workspaces/{s.workspace_id}/tasks")
            assert denied.status_code == 503
            assert denied.json()["error"]["code"] == "control_plane_closed"
            assert not s.native.calls
        finally:
            release.set()
            caller.join(timeout=2)
    assert not caller.is_alive() and not failures and results == [True]
    assert s.runtime.close(grace_seconds=0) is True
    assert Counter(s.closed) == ALL_STARTUP_CLOSED
    assert all(not thread.is_alive() for thread in s.runtime._threads)


def test_two_close_callers_claim_final_disposal_only_once(startup_case, monkeypatch):
    import threading

    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    ready = threading.Barrier(2)
    entered, release, duplicate = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    calls, results, failures = [], [], []
    guard = threading.Lock()
    first_thread = s.runtime._threads[0]
    join = first_thread.join
    dispose = s.runtime._dispose_business

    def synchronized_join(*args, **kwargs):
        if threading.current_thread().name.startswith("test-final-close-"):
            ready.wait(timeout=1)
        return join(*args, **kwargs)

    def held(cleanup):
        with guard:
            calls.append(True)
            first = len(calls) == 1
        if first:
            entered.set()
            assert release.wait(2)
        else:
            duplicate.set()
        dispose(cleanup)

    def close():
        try:
            results.append(s.runtime.close(grace_seconds=0.2))
        except BaseException as exc:  # noqa: BLE001 - Forward thread failures to the joined caller.
            failures.append(exc)

    monkeypatch.setattr(first_thread, "join", synchronized_join)
    monkeypatch.setattr(s.runtime, "_dispose_business", held)
    callers = [
        threading.Thread(target=close, name=f"test-final-close-{index}")
        for index in range(2)
    ]
    for caller in callers:
        caller.start()
    try:
        assert entered.wait(1)
        assert not duplicate.wait(0.3), "Two callers entered owned disposal"
        assert results == [False] and not s.closed and s.alive[0]
    finally:
        release.set()
        for caller in callers:
            caller.join(timeout=2)
    assert all(not caller.is_alive() for caller in callers)
    assert not failures and results == [False, True] and len(calls) == 1
    assert s.runtime.close(grace_seconds=0) is True
    assert Counter(s.closed) == ALL_STARTUP_CLOSED and not s.native.calls


def test_failed_owned_disposer_retains_remaining_actions_for_later_close(
    startup_case, monkeypatch, caplog
):
    s = startup_case
    module = runtime_module()
    s.runtime.start_background(background_identity=s.identity)
    close_catalog = s.runtime._planning.catalog.close
    attempts = []

    def fail_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError(
                "PRIVATE SQL password grant /user/path postgresql://secret"
            )
        close_catalog()

    monkeypatch.setattr(s.runtime._planning.catalog, "close", fail_once)
    caplog.set_level("WARNING", logger=module.__name__)
    assert s.runtime.close(grace_seconds=1) is False
    business_closed = ALL_STARTUP_CLOSED - Counter(
        owner=1, planning_catalog=1, planning_database=1
    )
    assert Counter(s.closed) == business_closed and s.alive[0]
    assert all(not thread.is_alive() for thread in s.runtime._threads)
    assert s.runtime.readiness() is False
    with pytest.raises(AgentError) as denied, s.runtime._start_admission():
        pytest.fail("Failed teardown reopened native admission")
    assert denied.value.code == "control_plane_closed"
    records = [record for record in caplog.records if record.name == module.__name__]
    assert len(records) == 1
    assert (
        records[0].getMessage()
        == "control_plane_disposal_failed callback=planning_catalog exception=RuntimeError"
    )
    assert records[0].exc_info is None and records[0].stack_info is None
    assert "PRIVATE" not in caplog.text and "postgresql" not in caplog.text
    assert s.runtime.close(grace_seconds=1) is True
    assert s.runtime.close(grace_seconds=0) is True
    assert len(attempts) == 2 and Counter(s.closed) == ALL_STARTUP_CLOSED
    assert not s.native.calls


def test_close_wins_held_startup_before_publication_and_consumer_admission(
    startup_case, monkeypatch
):
    import threading

    s = startup_case
    module = runtime_module()
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = module.build_gateway_owner

    def held_builder(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "build_gateway_owner", held_builder)

    def start():
        try:
            s.runtime.start_background(background_identity=s.identity)
        except BaseException as exc:  # noqa: BLE001 - Forward thread failures to the joined caller.
            errors.append(exc)

    started = threading.Thread(target=start, name="test-held-startup")
    started.start()
    try:
        assert entered.wait(1)
        assert s.runtime.close(grace_seconds=0) is False
    finally:
        release.set()
        started.join(timeout=2)
    assert not started.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], AgentError)
    assert errors[0].code == "control_plane_closed"
    assert not s.ticks and not s.native.calls
    assert s.runtime.close(grace_seconds=0.05) is True
    assert Counter(s.closed) == ALL_STARTUP_CLOSED


def test_partial_consumer_thread_start_failure_joins_before_disposing(
    startup_case, monkeypatch
):
    import threading

    s = startup_case
    original = threading.Thread.start
    started = []

    def fail_second(thread):
        if thread.name.startswith("sidera-cp-"):
            if started:
                raise RuntimeError("controlled thread start failure")
            started.append(thread)
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_second)
    monkeypatch.setattr(runtime_module(), "schema_ready", lambda database: True)
    try:
        with pytest.raises(RuntimeError, match="controlled thread start failure"):
            s.runtime.start_background(background_identity=s.identity)
        assert started and all(not thread.is_alive() for thread in started)
        assert not s.ticks and not s.native.calls
        assert Counter(s.closed) == ALL_STARTUP_CLOSED
        assert s.runtime.readiness() is False
    finally:
        # A failing implementation must not leave this test's owned fake consumers alive.
        s.runtime._stop.set()
        for thread in started:
            thread.join(timeout=1)


def test_public_runtime_composes_existing_business_ticks_and_same_saved_repository_reader(
    startup_case,
):
    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    assert s.business_observed.wait(2), "Existing business consumers were not scheduled"
    options = {name: kwargs for name, kwargs in s.builds}
    assert (
        options["scheduler"]["native_services"] is options["owner"]["native_services"]
    )
    assert options["collaboration"]["scan_all_workspaces"] is True
    assert options["notification"].get("auto_baseline") is True
    assert options["notification"]["owner_check"] is options["planning"]["owner_check"]
    assert (
        options["result"]["repository_resolver"]
        is options["owner"]["repository_resolver"]
    )
    assert options["result"]["repository_refs"] == options["owner"]["repository_refs"]
    assert callable(options["result"]["repository_resolver"])
    assert set(s.limits) >= {
        ("scheduler", (20,)),
        ("activation", (20,)),
        ("context", (10,)),
        ("collaboration", (20,)),
        ("plan_transition", (20,)),
        ("result", ()),
        ("reconciler", (50,)),
        ("notification", (50,)),
    }
    assert s.runtime.close() is True and s.runtime.close() is True
    assert Counter(s.closed) == ALL_STARTUP_CLOSED
    assert not s.native.calls


@pytest.mark.parametrize("startup_case", ("notifications_disabled",), indirect=True)
def test_public_runtime_does_not_enable_disabled_notification_consumer(startup_case):
    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    assert tuple(name for name, _ in s.builds) == ALL_STARTUP_NAMES[:-1]
    assert "notification" not in s.ticks
    assert s.runtime.close() is True
    assert "notification_database" not in s.closed and not s.native.calls


def test_later_business_builder_failure_closes_all_preceding_handles_without_ticks(
    startup_case, monkeypatch
):
    s = startup_case

    def fail_builder(*args, **kwargs):
        raise RuntimeError("controlled result builder failure")

    monkeypatch.setattr(runtime_module(), "build_result_worker", fail_builder)
    with pytest.raises(RuntimeError, match="controlled result builder failure"):
        s.runtime.start_background(background_identity=s.identity)
    assert tuple(name for name, _ in s.builds) == ALL_STARTUP_NAMES[:7]
    assert Counter(s.closed) == Counter(
        owner=1,
        planning_catalog=1,
        planning_database=1,
        scheduler_database=1,
        activation_database=1,
        context_database=1,
        context_catalog=1,
        collaboration_database=1,
        plan_transition_database=1,
    )
    assert not s.ticks and not s.native.calls
    assert s.runtime.close() is True


def test_held_normal_business_tick_retains_stores_until_bounded_close_can_join(
    startup_case,
):
    import threading

    s = startup_case
    entered, release = threading.Event(), threading.Event()
    s.runtime.start_background(background_identity=s.identity)

    def held(*args):
        entered.set()
        assert release.wait(2)

    s.business["activation"].worker.run_once = held
    try:
        assert entered.wait(1)
        assert s.runtime.close(grace_seconds=0.02) is False
        assert not s.closed and s.alive[0]
    finally:
        release.set()
    assert s.runtime.close() is True
    assert Counter(s.closed) == ALL_STARTUP_CLOSED
    assert all(not thread.is_alive() for thread in s.runtime._threads)
    assert not s.native.calls


@pytest.mark.parametrize("unavailable", ("closing", "owner_lost"))
def test_public_readiness_reflects_closing_and_actual_owner_loss(
    startup_case, monkeypatch, unavailable
):
    s = startup_case
    monkeypatch.setattr(runtime_module(), "schema_ready", lambda database: True)
    s.runtime.start_background(background_identity=s.identity)
    assert s.runtime.readiness() is True
    if unavailable == "closing":
        permit = s.builds[0][1]["native_services"].start_admission
        with permit():
            assert s.runtime.close(grace_seconds=0) is False
            assert s.runtime.readiness() is False
    else:
        s.alive[0] = False
        try:
            assert s.runtime.readiness() is False
        finally:
            s.alive[0] = True


def test_public_source_facade_is_stable_and_uses_unique_saved_grant_family(
    startup_case, monkeypatch
):
    from sidera_agents.contracts import SourceRef

    s = startup_case
    facade = s.runtime.context_source_reader
    assert facade is s.runtime.context_source_reader and facade is not s.native
    assert not s.builds and not s.lookups and not s.source_calls and not s.native.calls
    s.runtime.start_background(background_identity=s.identity)
    assert s.runtime.context_source_reader is facade
    grants = {"worker": uuid4(), "planner": uuid4()}
    gets = []

    def get(family, grant_id):
        gets.append((family, grant_id))
        return object() if grants[family] == grant_id else None

    repositories = SimpleNamespace(
        **{
            family + "_authorities": SimpleNamespace(
                get=lambda grant_id, family=family: get(
                    "worker" if family == "execution" else family, grant_id
                ),
                list_reconciliation_links=lambda *args, **kwargs: (),
            )
            for family in ("execution", "planning")
        }
    )
    repositories.planning_authorities.get = lambda grant_id: get("planner", grant_id)
    monkeypatch.setattr(
        runtime_module(),
        "SqlAlchemyUnitOfWork",
        lambda *args, **kwargs: nullcontext(repositories),
    )
    ref = SourceRef(
        provider="control-plane", key="same-opaque-source", version="exact-version"
    )
    for family in ("worker", "planner"):
        result = facade.read(grants[family], ref)
        assert result.text == family and result.actual_version == "exact-version"
    assert s.source_calls == [
        (family, grants[family], ref) for family in ("worker", "planner")
    ]
    assert gets == [
        (family, grant) for grant in grants.values() for family in ("worker", "planner")
    ]
    assert not s.native.calls


@pytest.mark.parametrize(
    "denied", ("unknown", "upstream", "ambiguous", "foreign", "invalid")
)
def test_public_source_facade_never_trials_or_falls_back_to_host_router(
    startup_case, monkeypatch, denied
):
    from sidera_agents.contracts import SourceRef

    s = startup_case
    s.runtime.start_background(background_identity=s.identity)
    grant = s.identity.grant_id if denied == "upstream" else uuid4()
    if denied == "invalid":
        grant = "untrusted"
    repositories = SimpleNamespace(
        **{
            family + "_authorities": SimpleNamespace(
                get=lambda supplied: object() if denied == "ambiguous" else None,
                list_reconciliation_links=lambda *args, **kwargs: (),
            )
            for family in ("execution", "planning")
        }
    )
    monkeypatch.setattr(
        runtime_module(),
        "SqlAlchemyUnitOfWork",
        lambda *args, **kwargs: nullcontext(repositories),
    )
    ref = SourceRef(
        provider="foreign" if denied == "foreign" else "control-plane",
        key="same-opaque-source",
        version="exact-version",
    )
    with pytest.raises(AgentError) as rejected:
        s.runtime.context_source_reader.read(grant, ref)
    assert rejected.value.code == "permission_denied"
    assert not s.source_calls and not s.native.calls
    assert s.runtime.close() is True
    with pytest.raises(AgentError) as closed:
        s.runtime.context_source_reader.read(grant, ref)
    assert closed.value.code == "control_plane_closed"


def test_owner_without_check_is_not_ready_or_close_complete(schema_runtime):
    s = schema_runtime
    s.runtime._owner = SimpleNamespace(
        gateway_runtime=SimpleNamespace(owner_check=None)
    )
    try:
        assert s.runtime.readiness() is False
        assert s.runtime.close(grace_seconds=0) is False
        assert not s.runtime._closed and not s.runtime._disposal_complete
        assert s.calls == [] and not s.native.calls
    finally:
        s.runtime._owner = None
    assert s.runtime.close(grace_seconds=0) is True


@pytest.mark.parametrize(
    "invalid",
    (
        None,
        "session_id",
        "lease_id",
        "worker_id",
        "fencing_token",
        "observable",
        "stop_requested",
    ),
)
def test_original_verification_targets_require_all_saved_pins(
    schema_runtime, monkeypatch, invalid
):
    from sidera_control_plane.persistence.native_verification_sessions import (
        OriginalVerification,
    )

    s = schema_runtime
    original = OriginalVerification(
        workspace_id=uuid4(),
        execution_id=uuid4(),
        session_id=uuid4(),
        lease_id=uuid4(),
        worker_id=uuid4(),
        fencing_token=3,
        observable=True,
    )
    if invalid is not None:
        original = replace(
            original,
            **{
                invalid: invalid == "stop_requested"
                if invalid in {"observable", "stop_requested"}
                else None
            },
        )
    selected = []
    owner = SimpleNamespace(
        require_owner=lambda: selected.append("owner"),
        gateway_runtime=SimpleNamespace(
            worker_gateway=SimpleNamespace(
                sync_selected_sessions=lambda targets: selected.append(targets)
            )
        ),
    )
    verifications = SimpleNamespace(
        page=lambda **kwargs: (original,),
        has_outstanding=lambda: True,
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(s.runtime, "_owner", owner)
        scoped.setattr(s.runtime, "_verifications", verifications)
        s.runtime._observe_original_verifications()
        assert verifications.has_outstanding() is True
    expected = (
        (
            (
                original.workspace_id,
                original.execution_id,
                original.session_id,
                original.lease_id,
                original.worker_id,
                original.fencing_token,
            ),
        )
        if invalid is None
        else ()
    )
    assert selected == ["owner", expected]
    assert s.calls == [] and not s.native.calls


def test_worker_close_missing_original_link_cannot_authorize_or_mutate(
    schema_runtime, monkeypatch
):
    s = schema_runtime
    expected = SimpleNamespace(
        workspace_id=uuid4(), execution_id=uuid4(), authority_id=uuid4()
    )
    reads = []

    def missing_link(workspace_id, execution_id):
        assert (workspace_id, execution_id) == (
            expected.workspace_id,
            expected.execution_id,
        )
        reads.append("link")

    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(
            get_link=missing_link,
            get=lambda *args, **kwargs: object(),
        ),
        executions=SimpleNamespace(get=lambda *args: object()),
    )
    monkeypatch.setattr(s.runtime, "_uow", lambda: nullcontext(uow))
    s.runtime._request_original_worker_stop(expected)
    assert reads == ["link"] and s.calls == [] and not s.native.calls


@pytest.fixture
def original_drain_runtime(schema_runtime, monkeypatch):
    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )
    from tests.unit.test_native_handoff_drain import NOW, make_drain_case

    s = schema_runtime
    case = make_drain_case()
    scheduled, disposed = [], []
    s.runtime._settings = s.runtime._settings.model_copy(
        update={"local_actor_id": "operator"}
    )
    monkeypatch.setattr(s.runtime, "_uow", case.uow_factory)
    monkeypatch.setattr(
        s.runtime, "_launch", lambda *args, **kwargs: scheduled.append((args, kwargs))
    )
    s.runtime._execution_authorizer = ExecutionAuthorityReader(
        case.uow_factory,
        principal=lambda: "operator",
        authorizer=None,
        clock=lambda: NOW,
    )
    s.runtime._owner = SimpleNamespace(
        gateway_runtime=SimpleNamespace(owner_check=lambda: case.owner),
        handoff_runtime=SimpleNamespace(coordinator=case.coordinator),
        close=lambda: disposed.append("owner"),
    )
    s.runtime._verifications = SimpleNamespace(has_outstanding=lambda: False)
    try:
        yield SimpleNamespace(
            runtime=s.runtime, case=case, scheduled=scheduled, disposed=disposed
        )
    finally:
        s.runtime._owner = None
        s.runtime._planning = None


def test_close_handoff_queries_original_stop_instead_of_rejected_ordinary_stop(
    original_drain_runtime,
):
    from sidera_control_plane.domain.handoffs import HandoffPhase
    from tests.unit.test_native_handoff_drain import known_stop, source_resources

    s = original_drain_runtime
    case = s.case
    before = source_resources(case)
    assert s.runtime.close(grace_seconds=0) is False
    s.runtime._check_drain()
    assert case.public.calls == [("get_stop", case.operation_id)]
    assert not case.uow.commands.applied and not case.uow.commands.rejected
    assert source_resources(case) == before
    assert not s.runtime._drained.is_set()
    assert s.runtime.close(grace_seconds=0) is False
    known_stop(case)
    s.runtime._check_drain()
    assert case.uow.handoff_transitions.item.phase is HandoffPhase.STOP_SOURCE
    assert not s.runtime._drained.is_set()
    assert s.runtime.close(grace_seconds=0) is False
    case.link = replace(case.link, status="stopped", event_cursor=2)
    s.runtime._check_drain()
    assert s.runtime._drained.is_set()
    assert s.runtime.close(grace_seconds=0) is True
    assert s.disposed == ["owner"]
    assert case.uow.handoff_transitions.item.target_execution_id is None
    assert [command.command_type for command in case.uow.commands.applied] == [
        "StopHandoffSource"
    ]
    assert not case.uow.commands.rejected


@pytest.mark.parametrize(
    "kind",
    [
        "other_phase",
        "legacy",
        "blocked",
        "blocked_unknown",
        "failed",
        "no_service",
        "no_owner",
    ],
)
def test_close_active_handoff_cannot_fall_back_to_ordinary_stop(
    original_drain_runtime,
    kind,
):
    from sidera_control_plane.domain.handoffs import (
        HandoffPhase,
        HandoffTransitionStatus,
    )
    from tests.unit.test_native_handoff_drain import source_resources

    s = original_drain_runtime
    case = s.case
    current = case.uow.handoff_transitions.item
    if kind == "other_phase":
        current.phase = HandoffPhase.CREATE_TARGET_EXECUTION
    elif kind == "legacy":
        current.handoff_schema_version, current.native_payload = 1, None
    elif kind == "no_service":
        case.coordinator._native_handoff = None
    elif kind == "no_owner":
        s.runtime._owner = None
    else:
        current.status = HandoffTransitionStatus(kind)
    before = source_resources(case)
    s.runtime._request_original_worker_stop(case.link)
    assert source_resources(case) == before and not case.public.calls
    assert not case.uow.commands.applied and not case.uow.commands.rejected


def test_close_without_handoff_retains_real_ordinary_stop(original_drain_runtime):
    s = original_drain_runtime
    case = s.case
    case.uow.handoff_transitions.item = None
    s.runtime._request_original_worker_stop(case.link)
    assert case.uow.executions.values[case.link.execution_id].stop_requested
    assert [command.command_type for command in case.uow.commands.applied] == [
        "StopExecution"
    ]
    assert [event.event_type for event in case.events] == ["ExecutionStopRequested"]
    assert not case.uow.commands.rejected and not case.public.calls


@pytest.mark.parametrize("first_receipt", ["unknown", "failed"])
def test_close_unknown_or_failed_handoff_still_attempts_other_worker_and_planner(
    original_drain_runtime,
    first_receipt,
):
    from sidera_agents.contracts import StopReceipt

    from tests.unit.test_native_handoff_drain import make_drain_case

    s = original_drain_runtime
    first, second = s.case, make_drain_case()
    first.receipt = StopReceipt(
        operation_id=first.operation_id,
        run_id=first.link.run_id,
        status=first_receipt,
        reason_code=None,
        evidence_sequence=None,
    )
    for name in (
        "executions",
        "sessions",
        "leases",
        "workers",
        "bindings",
        "workspaces",
    ):
        getattr(first.uow, name).values.update(getattr(second.uow, name).values)
    cases = {case.link.workspace_id: case for case in (first, second)}
    first.uow.execution_authorities.get = lambda identifier, **kwargs: next(
        (case.authority for case in cases.values() if case.authority.id == identifier),
        None,
    )
    first.uow.execution_authorities.get_link = (
        lambda workspace_id, execution_id, **kwargs: (
            cases[workspace_id].link
            if workspace_id in cases
            and cases[workspace_id].link.execution_id == execution_id
            else None
        )
    )
    first.uow.execution_authorities.list_reconciliation_links = lambda *a, **k: (
        first.link,
        second.link,
    )
    first.uow.agent_bindings.history = lambda workspace_id, *a, **k: (
        cases[workspace_id].original_selected,
    )
    first.uow.agent_bindings.get = lambda workspace_id, *a, **k: (
        cases[workspace_id].selected
    )
    planner, attempted = object(), []
    first.uow.planning_authorities.list_reconciliation_links = lambda *a, **k: (
        planner,
    )
    s.runtime._planning = SimpleNamespace(
        runner=SimpleNamespace(
            drain_original_run=lambda link: attempted.append(link) or True
        )
    )
    assert s.runtime.close(grace_seconds=0) is False
    s.runtime._check_drain()
    assert first.public.calls == [("get_stop", first.operation_id)]
    assert first.uow.executions.values[second.link.execution_id].stop_requested
    assert attempted == [planner]
    assert not first.uow.commands.rejected and not s.runtime._drained.is_set()
    assert s.runtime.close(grace_seconds=0) is False


@pytest.mark.parametrize("held", ["_admitted", "_regular_work"])
def test_close_drain_retains_admitted_and_regular_work_fence(
    original_drain_runtime,
    held,
):
    s = original_drain_runtime
    setattr(s.runtime, held, 1)
    try:
        assert s.runtime.close(grace_seconds=0) is False
        s.runtime._check_drain()
        assert not s.case.public.calls and not s.runtime._drained.is_set()
        assert not s.case.uow.commands.applied and not s.case.uow.commands.rejected
    finally:
        setattr(s.runtime, held, 0)
