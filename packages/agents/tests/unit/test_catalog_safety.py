from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import URL


def _facts(**changes):
    facts = {
        "database_name": "sidera_agents_test",
        "configured_name": "sidera_agents_test",
        "owner_matches": True,
        "custom_schemas": set(),
        "objects": set(),
    }
    facts.update(changes)
    return facts


def test_catalog_clock_is_callable_as_instance_attribute(catalog_lab):
    assert catalog_lab.clock() == datetime(2026, 10, 1, tzinfo=UTC)
    saved = catalog_lab.catalog.create_profile(
        catalog_lab.workspace_id,
        catalog_lab.draft,
        grant_id=catalog_lab.grant_id,
    )
    assert saved.created_at == catalog_lab.clock()


@pytest.mark.parametrize(
    "kind",
    [
        "table",
        "view",
        "materialized_view",
        "sequence",
        "function",
        "enum",
        "domain",
    ],
)
def test_any_existing_agents_object_rejects_scratch_target(scratch_gate, kind):
    facts = _facts(
        custom_schemas={"agents"},
        objects={("agents", "profile_revisions", kind)},
    )
    assert scratch_gate(facts) is False


@pytest.mark.parametrize("custom_schemas", [set(), {"agents"}])
def test_empty_scratch_target_is_accepted(scratch_gate, custom_schemas):
    assert scratch_gate(_facts(custom_schemas=custom_schemas)) is True


def test_public_preexisting_table_rejects_scratch_target(scratch_gate):
    facts = _facts(objects={("public", "notes", "table")})
    assert scratch_gate(facts) is False


def test_empty_alembic_version_table_is_not_a_blank_target(scratch_gate):
    facts = _facts(
        custom_schemas={"agents"},
        objects={("agents", "alembic_version", "table")},
    )
    assert scratch_gate(facts) is False


@pytest.mark.parametrize("schema", ["control_plane", "product", "knowledge"])
def test_non_agents_custom_schema_rejects_scratch_target(scratch_gate, schema):
    assert scratch_gate(_facts(custom_schemas={"agents", schema})) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"database_name": "sidera_product", "configured_name": "sidera_product"},
        {
            "database_name": "sidera_agents_test",
            "configured_name": "sidera_agents_test_alt",
        },
        {"owner_matches": False},
    ],
)
def test_wrong_database_or_owner_rejects_scratch_target(scratch_gate, changes):
    assert scratch_gate(_facts(**changes)) is False


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class _Connection:
    def __init__(self, *, locked=True, owner_matches=True, schemas=(), objects=()):
        self.locked = locked
        self.owner_matches = owner_matches
        self.schemas = schemas
        self.objects = objects
        self.statements = []
        self.closed = False
        self.failure = None

    def execute(self, statement, parameters=None):
        query = str(statement)
        self.statements.append(query)
        if self.failure is not None:
            raise self.failure
        if "pg_try_advisory_lock" in query:
            return _Result(self.locked)
        if "current_database()" in query:
            return _Result(("sidera_agents_test", self.owner_matches))
        if "FROM pg_namespace" in query:
            return _Result(self.schemas)
        if "FROM pg_class" in query:
            return _Result(self.objects)
        if "pg_advisory_unlock" in query:
            return _Result(True)
        raise AssertionError("Unexpected scratch-gate query.")

    def close(self):
        self.closed = True


class _Engine:
    def __init__(self, connection):
        self.connection = connection
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def _url(host="localhost", database="sidera_agents_test", query=None):
    return str(
        URL.create(
            "postgresql+psycopg",
            username="fixture-user",
            host=host,
            database=database,
            query=query or {},
        )
    )


def test_missing_dedicated_url_never_creates_engine(scratch_pg_acquire):
    created = []

    def engine_factory(*args, **kwargs):
        created.append((args, kwargs))
        raise AssertionError("Engine factory must not run without the dedicated URL.")

    with pytest.raises(Exception) as caught:
        scratch_pg_acquire({}, engine_factory=engine_factory)

    assert type(caught.value).__name__ == "_ScratchTargetNotConfigured"
    assert created == []


@pytest.mark.parametrize(
    "url",
    [_url(host="remote.invalid"), _url(database="sidera_product")],
)
def test_unsafe_url_is_rejected_before_engine_creation(scratch_pg_acquire, url):
    created = []

    def engine_factory(*args, **kwargs):
        created.append((args, kwargs))
        raise AssertionError("Unsafe URL must be rejected before engine creation.")

    with pytest.raises(Exception) as caught:
        scratch_pg_acquire({"SIDERA_AGENTS_TEST_DATABASE_URL": url}, engine_factory)

    assert type(caught.value).__name__ == "_ScratchTargetUnavailable"
    assert created == []


@pytest.mark.parametrize(
    "query",
    [
        {"hostaddr": "192.0.2.44"},
        {"hostaddr": "localhost"},
        {"hostaddr": "not-an-ip-address"},
        {"hostaddr": ("127.0.0.1", "192.0.2.44")},
        {"service": "synthetic-service"},
        {"servicefile": "synthetic-service-file"},
    ],
)
def test_url_routing_overrides_are_rejected_before_engine_creation(
    scratch_pg_acquire, query
):
    created = []

    def engine_factory(*args, **kwargs):
        created.append((args, kwargs))
        raise AssertionError("Unsafe routing override must not create an engine.")

    with pytest.raises(Exception) as caught:
        scratch_pg_acquire(
            {"SIDERA_AGENTS_TEST_DATABASE_URL": _url(query=query)}, engine_factory
        )

    assert type(caught.value).__name__ == "_ScratchTargetUnavailable"
    assert created == []


@pytest.mark.parametrize("fallback", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"])
def test_global_libpq_routing_fallback_is_rejected(scratch_pg_acquire, fallback):
    created = []
    environ = {
        "SIDERA_AGENTS_TEST_DATABASE_URL": _url(),
        fallback: "synthetic-routing-value",
    }

    with pytest.raises(Exception) as caught:
        scratch_pg_acquire(
            environ,
            lambda *args, **kwargs: created.append((args, kwargs)),
        )

    assert type(caught.value).__name__ == "_ScratchTargetUnavailable"
    assert created == []


def test_explicit_loopback_hostaddr_is_accepted_and_released(
    scratch_pg_acquire, scratch_pg_release
):
    connection = _Connection()
    engine = _Engine(connection)
    target = scratch_pg_acquire(
        {
            "SIDERA_AGENTS_TEST_DATABASE_URL": _url(
                query={"hostaddr": ("127.0.0.1", "::1")}
            )
        },
        lambda *args, **kwargs: engine,
    )

    assert target.url.database == "sidera_agents_test"
    assert scratch_pg_release(engine, connection, True) is True
    assert connection.closed is True
    assert engine.disposed is True


def test_existing_objects_block_target_before_any_ddl_and_dispose_engine(
    scratch_pg_acquire,
):
    connection = _Connection(
        schemas=("agents",),
        objects=(("agents", "user_notes", "table"),),
    )
    engine = _Engine(connection)

    def engine_factory(*args, **kwargs):
        return engine

    with pytest.raises(Exception) as caught:
        scratch_pg_acquire({"SIDERA_AGENTS_TEST_DATABASE_URL": _url()}, engine_factory)

    assert type(caught.value).__name__ == "_ScratchTargetUnavailable"
    assert engine.disposed is True
    assert connection.closed is True
    assert not any(
        query.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))
        for query in connection.statements
    )


def test_lock_contention_fails_and_disposes_without_reading_catalog(
    scratch_pg_acquire,
):
    connection = _Connection(locked=False)
    engine = _Engine(connection)

    with pytest.raises(Exception) as caught:
        scratch_pg_acquire(
            {"SIDERA_AGENTS_TEST_DATABASE_URL": _url()},
            lambda *args, **kwargs: engine,
        )

    assert type(caught.value).__name__ == "_ScratchTargetUnavailable"
    assert engine.disposed is True
    assert connection.closed is True
    assert len(connection.statements) == 2
    assert "current_database()" in connection.statements[0]
    assert "pg_try_advisory_lock" in connection.statements[1]


def test_wrong_database_owner_fails_before_lock_or_catalog_enumeration(
    scratch_pg_acquire,
):
    connection = _Connection(owner_matches=False)
    engine = _Engine(connection)

    with pytest.raises(Exception) as caught:
        scratch_pg_acquire(
            {"SIDERA_AGENTS_TEST_DATABASE_URL": _url()},
            lambda *args, **kwargs: engine,
        )

    assert type(caught.value).__name__ == "_ScratchTargetUnavailable"
    assert len(connection.statements) == 1
    assert "current_database()" in connection.statements[0]
    assert engine.disposed is True
    assert connection.closed is True


def test_connection_errors_are_sanitized_and_disposed(scratch_pg_acquire):
    connection = _Connection()
    connection.failure = RuntimeError("synthetic connection detail")
    engine = _Engine(connection)

    with pytest.raises(Exception) as caught:
        scratch_pg_acquire(
            {"SIDERA_AGENTS_TEST_DATABASE_URL": _url()},
            lambda *args, **kwargs: engine,
        )

    assert "synthetic connection detail" not in str(caught.value)
    assert engine.disposed is True
    assert connection.closed is True


def test_cleanup_refuses_unknown_objects_without_migration_or_ddl(
    scratch_pg_cleanup,
):
    connection = _Connection(
        schemas=("agents",),
        objects=(("agents", "user_notes", "table"),),
    )
    engine = _Engine(connection)
    target = SimpleNamespace(
        engine=engine,
        url=URL.create(
            "postgresql+psycopg",
            username="fixture-user",
            host="localhost",
            database="sidera_agents_test",
        ),
        lock_connection=connection,
        configured_name="sidera_agents_test",
    )
    migrations = []

    with pytest.raises(Exception) as caught:
        scratch_pg_cleanup(target, migration=lambda *args: migrations.append(args))

    assert type(caught.value).__name__ == "_ScratchTargetCleanupFailed"
    assert migrations == []
    assert not any(
        query.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))
        for query in connection.statements
    )
