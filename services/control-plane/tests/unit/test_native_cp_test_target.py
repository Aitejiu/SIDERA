from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.dml import Delete

from tests.support import native_agents

pytest_plugins = ("pytester",)


def _environment(**query: str) -> dict[str, str]:
    url = URL.create(
        "postgresql+psycopg",
        username="cp_test_owner",
        database="sidera_native_cp_test_synthetic",
        query={"host": "/synthetic/private/socket", "port": "5432", **query},
    )
    return {
        "SIDERA_NATIVE_CP_TEST_DATABASE_URL": url.render_as_string(hide_password=False),
        "SIDERA_NATIVE_CP_TEST_OWNER": "cp_test_owner",
        "SIDERA_NATIVE_CP_TEST_SOCKET": "/synthetic/private/socket",
        "SIDERA_NATIVE_CP_TEST_MARKER": "sidera-native-harness:cp:synthetic",
    }


def _relation(
    name: str,
    *,
    schema: str = "public",
    kind: str = "r",
    owner: str = "cp_test_owner",
    parent_name: str | None = None,
    parent_schema: str | None = None,
) -> dict[str, object]:
    return {
        "schema": schema,
        "name": name,
        "kind": kind,
        "owner": owner,
        "parent_name": parent_name,
        "parent_schema": parent_schema,
    }


def _function(**changes: object) -> dict[str, object]:
    return {
        "schema": "public",
        "name": "sidera_reject_handoff_recovery_decision_mutation",
        "owner": "cp_test_owner",
        "arguments": "",
        "result": "trigger",
        "security_definer": False,
        **changes,
    }


class _Result:
    def __init__(self, rows=(), scalar=None):
        self.rows = rows
        self.value = scalar

    def mappings(self):
        return self

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self):
        return self.rows

    def scalar_one(self):
        return self.value


@dataclass
class _Server:
    identity: dict[str, object] = field(
        default_factory=lambda: {
            "database_name": "sidera_native_cp_test_synthetic",
            "current_user": "cp_test_owner",
            "owner": "cp_test_owner",
            "marker": "sidera-native-harness:cp:synthetic",
            "superuser": False,
            "create_role": False,
            "create_database": False,
        }
    )
    schemas: list[dict[str, object]] = field(
        default_factory=lambda: [{"name": "public", "owner": "pg_database_owner"}]
    )
    relations: list[dict[str, object]] = field(default_factory=list)
    functions: list[dict[str, object]] = field(default_factory=list)
    types: list[dict[str, object]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    connections: list[_Connection] = field(default_factory=list)
    engines: list[_Engine] = field(default_factory=list)
    locked: bool = False
    fail_at: str | None = None

    def create_engine(self, url, **settings):
        engine = _Engine(self, url, settings)
        self.engines.append(engine)
        return engine


class _Connection:
    def __init__(self, server: _Server):
        self.server = server
        self.read_only = False
        self.owns_lock = False
        self.closed = False

    def execute(self, statement, parameters=None):
        sql = str(statement)
        if isinstance(statement, Delete):
            assert not self.read_only
            self.server.events.append(f"delete:{statement.table.name}")
            return _Result()
        if sql == "SET TRANSACTION READ ONLY":
            self.read_only = True
            self.server.events.append("read_only")
            return _Result()
        if "pg_catalog.set_config" in sql:
            assert parameters and 0 < int(parameters["statement"]) <= 5000
            assert 0 < int(parameters["lock"]) <= 5000
            self.server.events.append("cleanup_deadlines")
            return _Result()
        if "pg_try_advisory_lock" in sql:
            self.server.events.append("lock")
            assert parameters and "namespace" in parameters
            if self.server.locked:
                return _Result(scalar=False)
            self.server.locked = self.owns_lock = True
            return _Result(scalar=True)
        if "pg_advisory_unlock" in sql:
            self.server.events.append("unlock")
            self.server.locked = self.owns_lock = False
            return _Result(scalar=True)
        for token, name, rows in (
            ("FROM pg_catalog.pg_database", "identity", [self.server.identity]),
            ("FROM pg_catalog.pg_namespace", "schemas", self.server.schemas),
            ("FROM pg_catalog.pg_class", "relations", self.server.relations),
            ("FROM pg_catalog.pg_proc", "functions", self.server.functions),
            ("FROM pg_catalog.pg_type", "types", self.server.types),
        ):
            if token in sql:
                self.server.events.append(name)
                if self.server.fail_at == name:
                    raise RuntimeError("private-driver-diagnostic")
                return _Result(rows)
        raise AssertionError(f"Unexpected guard SQL: {sql}")

    def rollback(self):
        self.server.events.append("rollback")
        self.read_only = False
        if self.server.fail_at == "rollback":
            raise RuntimeError("private-driver-diagnostic")

    def close(self):
        self.server.events.append("close")
        self.closed = True
        if self.owns_lock:
            self.server.locked = self.owns_lock = False
        if self.server.fail_at == "close":
            raise RuntimeError("private-driver-diagnostic")


class _Engine:
    def __init__(self, server: _Server, url: URL, settings):
        self.server = server
        self.url = url
        self.settings = settings
        self.disposed = False

    def connect(self):
        self.server.events.append("connect")
        if self.server.fail_at == "connect":
            raise RuntimeError("private-driver-diagnostic")
        connection = _Connection(self.server)
        self.server.connections.append(connection)
        return connection

    @contextmanager
    def begin(self) -> Iterator[_Connection]:
        connection = self.connect()
        try:
            yield connection
            self.server.events.append("commit")
        finally:
            connection.close()

    def dispose(self):
        self.server.events.append("dispose")
        self.disposed = True
        if self.server.fail_at == "dispose":
            raise RuntimeError("private-driver-diagnostic")


def _guard(server: _Server, environment: dict[str, str] | None = None):
    environment = _environment() if environment is None else environment
    return native_agents.guard_native_cp_test_target(
        environment.get("SIDERA_NATIVE_CP_TEST_DATABASE_URL", ""),
        environ=environment,
        engine_factory=server.create_engine,
    )


@pytest.mark.parametrize("missing", tuple(_environment()))
def test_missing_dedicated_selection_never_connects(missing: str) -> None:
    environment = _environment()
    environment.pop(missing)
    server = _Server()

    with (
        pytest.raises(native_agents.NativeCPTestTargetError),
        _guard(server, environment),
    ):
        pytest.fail("unsafe target was admitted")

    assert server.events == []


def test_app_environment_is_not_a_target_fallback() -> None:
    server = _Server()
    environment = {
        "SIDERA_DATABASE_URL": _environment()["SIDERA_NATIVE_CP_TEST_DATABASE_URL"]
    }

    with (
        pytest.raises(native_agents.NativeCPTestTargetError),
        _guard(server, environment),
    ):
        pytest.fail("app-only target was admitted")

    assert server.events == []


def test_child_environment_mirrors_selection_without_mutating_parent() -> None:
    parent = {**_environment(), "SIDERA_DATABASE_URL": "private-app-selection"}
    child = native_agents.native_cp_test_environment(parent)

    assert child["SIDERA_DATABASE_URL"] == parent["SIDERA_NATIVE_CP_TEST_DATABASE_URL"]
    assert parent["SIDERA_DATABASE_URL"] == "private-app-selection"


def test_fixture_url_mismatch_never_connects() -> None:
    server = _Server()

    with (
        pytest.raises(native_agents.NativeCPTestTargetError),
        native_agents.guard_native_cp_test_target(
            "private-app-selection",
            environ=_environment(),
            engine_factory=server.create_engine,
        ),
    ):
        pytest.fail("mismatched app fixture was admitted")

    assert server.events == []


@pytest.mark.parametrize(
    "changes",
    [
        {"drivername": "sqlite"},
        {"drivername": "postgresql+psycopg2"},
        {"database": "agents_test_synthetic"},
        {"database": "sidera_native_cp_test_"},
        {"database": "sidera_native_cp_test_bad/name"},
        {"username": "postgres"},
        {"host": "remote.invalid"},
        {"query": {"host": "remote.invalid", "port": "5432"}},
        {"query": {}},
        {"query": {"host": "/other/socket", "port": "5432"}},
        {"query": {"host": ("/synthetic/private/socket", "/other/socket")}},
        {"query": {"host": "/synthetic/private/socket,remote.invalid"}},
        {"query": {"host": "/synthetic/private/socket", "hostaddr": "8.8.8.8"}},
        {"query": {"host": "/synthetic/private/socket", "hostaddr": "127.0.0.1"}},
        {"query": {"host": "/synthetic/private/socket", "service": "private"}},
        {"query": {"host": "/synthetic/private/socket", "servicefile": "private"}},
        {"query": {"host": "/synthetic/private/socket", "options": "-c role=x"}},
        {"query": {"host": "/synthetic/private/socket", "port": ("5432", "5433")}},
        {"port": 5433},
    ],
    ids=[
        "non-postgres",
        "other-driver",
        "other-database",
        "empty-suffix",
        "bad-database",
        "admin-user",
        "remote-authority",
        "remote-query",
        "implicit-route",
        "other-socket",
        "repeated-host",
        "multi-host",
        "remote-hostaddr",
        "socket-hostaddr-override",
        "service",
        "servicefile",
        "session-options",
        "repeated-port",
        "conflicting-port",
    ],
)
def test_unsafe_url_never_connects(changes: dict[str, object]) -> None:
    environment = _environment()
    original = URL.create(
        "postgresql+psycopg",
        username="cp_test_owner",
        database="sidera_native_cp_test_synthetic",
        query={"host": "/synthetic/private/socket", "port": "5432"},
    )
    environment["SIDERA_NATIVE_CP_TEST_DATABASE_URL"] = original.set(
        **changes
    ).render_as_string(hide_password=False)
    server = _Server()

    with (
        pytest.raises(native_agents.NativeCPTestTargetError),
        _guard(server, environment),
    ):
        pytest.fail("unsafe route was admitted")

    assert server.events == []


@pytest.mark.parametrize(
    "key",
    [
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSYSCONFDIR",
        "PGOPTIONS",
        "PGTARGETSESSIONATTRS",
        "PGLOADBALANCEHOSTS",
    ],
)
def test_libpq_environment_override_never_connects(key: str) -> None:
    environment = {**_environment(), key: "private-override"}
    server = _Server()

    with (
        pytest.raises(native_agents.NativeCPTestTargetError),
        _guard(server, environment),
    ):
        pytest.fail("routing override was admitted")

    assert server.events == []


@pytest.mark.parametrize("endpoint", ["/synthetic/private/socket", "127.0.0.1", "::1"])
def test_exact_local_selection_can_hold_a_lock(endpoint: str) -> None:
    environment = _environment(host=endpoint)
    environment["SIDERA_NATIVE_CP_TEST_SOCKET"] = endpoint
    server = _Server()

    with _guard(server, environment) as guard:
        assert guard.active
        assert server.locked
        assert not server.connections[0].closed
        assert not server.engines[0].disposed

    assert not guard.active
    assert not server.locked
    assert server.connections[0].closed
    assert server.engines[0].disposed


@pytest.mark.parametrize(
    "changes",
    [
        {"database_name": "sidera_native_cp_test_other"},
        {"current_user": "postgres"},
        {"owner": "other_owner"},
        {"marker": None},
        {"marker": "sidera-native-harness:cp:other-run"},
        {"superuser": True},
        {"create_role": True},
        {"create_database": True},
    ],
    ids=[
        "database",
        "user",
        "owner",
        "missing-marker",
        "marker",
        "superuser",
        "create-role",
        "create-database",
    ],
)
def test_unowned_identity_is_refused_and_closed(changes: dict[str, object]) -> None:
    server = _Server()
    server.identity.update(changes)

    with pytest.raises(native_agents.NativeCPTestTargetError), _guard(server):
        pytest.fail("unowned identity was admitted")

    assert "lock" not in server.events
    assert not server.locked
    assert all(connection.closed for connection in server.connections)
    assert server.engines[0].disposed


@pytest.mark.parametrize(
    "objects",
    [
        {"schemas": [{"name": "agents", "owner": "cp_test_owner"}]},
        {"schemas": [{"name": "knowledge", "owner": "cp_test_owner"}]},
        {"schemas": [{"name": "product", "owner": "cp_test_owner"}]},
        {"schemas": [{"name": "public", "owner": "other_owner"}]},
        {"relations": [_relation("user_data")]},
        {"relations": [_relation("tasks", owner="other_owner")]},
        {"relations": [_relation("tasks", schema="agents")]},
        {"relations": [_relation("tasks", kind="v")]},
        {
            "relations": [
                _relation(
                    "foreign_index",
                    kind="i",
                    parent_name="user_data",
                    parent_schema="public",
                )
            ]
        },
        {"relations": [_relation("foreign_sequence", kind="S")]},
        {"functions": [_function(name="user_function")]},
        {"functions": [_function(owner="other_owner")]},
        {"functions": [_function(arguments="integer")]},
        {"functions": [_function(security_definer=True)]},
        {"types": [{"schema": "public", "name": "user_enum", "kind": "e"}]},
    ],
    ids=[
        "agents-schema",
        "knowledge-schema",
        "product-schema",
        "schema-owner",
        "foreign-table",
        "table-owner",
        "foreign-table-schema",
        "view",
        "foreign-index",
        "unowned-sequence",
        "foreign-function",
        "function-owner",
        "function-overload",
        "security-definer",
        "foreign-type",
    ],
)
def test_foreign_objects_are_refused_without_writes(objects) -> None:
    server = _Server(**objects)

    with pytest.raises(native_agents.NativeCPTestTargetError), _guard(server):
        pytest.fail("foreign objects were admitted")

    assert not any(event.startswith("delete:") for event in server.events)
    assert not server.locked
    assert "unlock" in server.events
    assert server.connections[0].closed
    assert server.engines[0].disposed


def test_owned_migrated_cp_objects_are_admitted_without_writes() -> None:
    server = _Server(
        relations=[
            _relation("tasks"),
            _relation("workspaces"),
            _relation("alembic_version"),
            _relation(
                "tasks_pkey", kind="i", parent_name="tasks", parent_schema="public"
            ),
        ],
        functions=[_function()],
    )

    with _guard(server):
        assert server.locked

    assert not any(event.startswith("delete:") for event in server.events)
    assert server.events.index("identity") < server.events.index("lock")
    assert server.events.index("lock") < server.events.index("relations")


def test_guard_engine_hides_parameters_and_bounds_deadlines() -> None:
    server = _Server()

    with _guard(server):
        settings = server.engines[0].settings
        assert settings["hide_parameters"] is True
        assert settings["poolclass"] is NullPool
        assert 0 < settings["connect_args"]["connect_timeout"] <= 5
        options = settings["connect_args"]["options"]
        assert "statement_timeout=" in options
        assert "lock_timeout=" in options
        assert server.events.index("read_only") < server.events.index("identity")


def test_busy_target_is_refused_without_unlocking_another_owner() -> None:
    server = _Server(locked=True)

    with pytest.raises(native_agents.NativeCPTestTargetError), _guard(server):
        pytest.fail("busy target was admitted")

    assert server.locked
    assert "unlock" not in server.events
    assert server.connections[0].closed
    assert server.engines[0].disposed


@pytest.mark.parametrize("fail_at", ["connect", "identity", "relations"])
def test_driver_failure_is_sanitized_and_disposed(fail_at: str) -> None:
    server = _Server(fail_at=fail_at)

    with pytest.raises(native_agents.NativeCPTestTargetError) as caught, _guard(server):
        pytest.fail("failed connection was admitted")

    assert "private-driver-diagnostic" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not server.locked
    assert all(connection.closed for connection in server.connections)
    assert server.engines[0].disposed


def test_body_failure_releases_lock_and_preserves_original_exception() -> None:
    server = _Server()

    with pytest.raises(ValueError, match="test-body-failed"), _guard(server):
        raise ValueError("test-body-failed")

    assert not server.locked
    assert "unlock" in server.events
    assert server.connections[0].closed
    assert server.engines[0].disposed


@pytest.mark.parametrize("fail_at", ["rollback", "close", "dispose"])
def test_release_failure_is_safe_and_still_disposes_resources(fail_at) -> None:
    server = _Server(fail_at=fail_at)

    with pytest.raises(native_agents.NativeCPTestTargetError) as caught, _guard(server):
        assert server.locked

    assert "private-driver-diagnostic" not in str(caught.value)
    assert caught.value.__context__ is None
    assert not server.locked
    assert server.connections[0].closed
    assert server.engines[0].disposed


@pytest.fixture
def native_metadata(monkeypatch):
    metadata = MetaData()
    for name in ("agent_bindings", "execution_authorities", "agent_execution_links"):
        Table(name, metadata, Column("id", Integer, primary_key=True))
    monkeypatch.setattr(native_agents.Base, "metadata", metadata)
    return metadata


def test_cleanup_refuses_expired_guard_before_writes(native_metadata) -> None:
    server = _Server()
    with _guard(server) as guard:
        database = SimpleNamespace(engine=server.create_engine(guard.target.url))

    before = list(server.events)
    with pytest.raises(native_agents.NativeCPTestTargetError):
        native_agents.cleanup_native_cp_tables(database, guard)

    assert server.events == before


def test_cleanup_refuses_another_database_before_writes(native_metadata) -> None:
    server = _Server()
    with _guard(server) as guard:
        database = SimpleNamespace(
            engine=server.create_engine(guard.target.url.set(database="other_database"))
        )
        before = list(server.events)

        with pytest.raises(native_agents.NativeCPTestTargetError):
            native_agents.cleanup_native_cp_tables(database, guard)

        assert server.events == before


def test_cleanup_uses_a_bounded_owned_engine(monkeypatch, native_metadata) -> None:
    server = _Server()
    monkeypatch.setattr(native_agents, "create_engine", server.create_engine)
    with _guard(server) as guard:
        legacy_engine = server.create_engine(guard.target.url)
        database = SimpleNamespace(engine=legacy_engine)
        native_agents.cleanup_native_cp_tables(database, guard)
        cleanup_engine = server.engines[-1]

        assert cleanup_engine is not legacy_engine
        assert cleanup_engine.url == guard.target.url
        assert cleanup_engine.settings["hide_parameters"] is True
        assert 0 < cleanup_engine.settings["connect_args"]["connect_timeout"] <= 5
        assert cleanup_engine.disposed
        assert server.locked


def test_cleanup_rechecks_identity_before_writes(monkeypatch, native_metadata) -> None:
    server = _Server()
    monkeypatch.setattr(native_agents, "create_engine", server.create_engine)
    with _guard(server) as guard:
        database = SimpleNamespace(engine=server.create_engine(guard.target.url))
        server.identity["marker"] = "sidera-native-harness:cp:another-run"

        with pytest.raises(native_agents.NativeCPTestTargetError):
            native_agents.cleanup_native_cp_tables(database, guard)

        assert not any(event.startswith("delete:") for event in server.events)
        assert server.locked


def _fixture_run(pytester, monkeypatch, server, *, safe, failing_body=False):
    from tests.integration import database_fixtures

    for key in tuple(os.environ):
        if key.startswith(("PG", "SIDERA_NATIVE_CP_TEST_")):
            monkeypatch.delenv(key)
    environment = _environment()
    for key, value in environment.items():
        if safe or key == "SIDERA_NATIVE_CP_TEST_DATABASE_URL":
            monkeypatch.setenv(key, value)
    monkeypatch.setenv(
        "SIDERA_DATABASE_URL", environment["SIDERA_NATIVE_CP_TEST_DATABASE_URL"]
    )
    monkeypatch.delenv("SIDERA_KEEP_OPENCODE_E2E", raising=False)
    monkeypatch.setattr(native_agents, "create_engine", server.create_engine)
    monkeypatch.setattr(
        database_fixtures.command,
        "upgrade",
        lambda *_args: server.events.append("migration"),
    )

    class _Database:
        def __init__(self, url):
            from sqlalchemy.engine import make_url

            self.engine = server.create_engine(make_url(url))

        def dispose(self):
            self.engine.dispose()

    monkeypatch.setattr(database_fixtures, "Database", _Database)
    monkeypatch.setattr(
        database_fixtures,
        "_clear_database",
        lambda _database: server.events.append("legacy_cleanup"),
    )
    body = (
        'assert False, "synthetic-body-failed"' if failing_body else "assert database"
    )
    pytester.makepyfile(
        f"""
        from tests.integration.conftest import _clean_database_between_integration_tests
        from tests.support.native_agents import (
            native_cp_test_target_guard, native_cp_guarded_database, native_cp_test_cleanup,
        )

        def test_uses_real_legacy_fixture_dependencies(database):
            {body}
        """
    )
    return pytester.inline_run(
        "-q", "--tb=short", "-p", "no:cacheprovider", plugins=[database_fixtures]
    )


def test_unsafe_selection_prevents_real_fixture_migration_and_cleanup(
    pytester, monkeypatch
) -> None:
    server = _Server()
    result = _fixture_run(pytester, monkeypatch, server, safe=False)

    reports = result.getreports("pytest_runtest_logreport")
    assert [(report.when, report.outcome) for report in reports] == [
        ("setup", "failed"),
        ("teardown", "passed"),
    ]
    assert "migration" not in server.events
    assert "legacy_cleanup" not in server.events
    assert not any(event.startswith("delete:") for event in server.events)
    assert server.events == []


@pytest.mark.parametrize("failing_body", [False, True], ids=["passed", "failed"])
def test_native_cleanup_precedes_real_legacy_dependency_teardown(
    pytester, monkeypatch, native_metadata, failing_body
) -> None:
    server = _Server(
        relations=[
            _relation(name)
            for name in (
                "agent_bindings",
                "execution_authorities",
                "agent_execution_links",
            )
        ]
    )
    result = _fixture_run(
        pytester, monkeypatch, server, safe=True, failing_body=failing_body
    )

    result.assertoutcome(passed=not failing_body, failed=failing_body)
    reports = result.getreports("pytest_runtest_logreport")
    assert [(report.when, report.outcome) for report in reports] == [
        ("setup", "passed"),
        ("call", "failed" if failing_body else "passed"),
        ("teardown", "passed"),
    ]
    writes = [event for event in server.events if event.startswith("delete:")]
    assert writes == [
        "delete:agent_execution_links",
        "delete:execution_authorities",
        "delete:agent_bindings",
    ]
    assert server.events.index("lock") < server.events.index("migration")
    assert server.events.index("relations") < server.events.index("migration")
    cleanups = [i for i, event in enumerate(server.events) if event == "legacy_cleanup"]
    assert len(cleanups) == 2
    assert cleanups[0] < server.events.index("delete:agent_execution_links")
    assert server.events.index("delete:agent_bindings") < cleanups[1]
    assert cleanups[1] < server.events.index("unlock")
    assert not server.locked
    assert all(connection.closed for connection in server.connections)
