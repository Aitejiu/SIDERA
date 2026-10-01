import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from sidera_agents.contracts import (
    CapabilityResourceDraft,
    ProfileDraft,
    RuntimeRef,
)
from sidera_agents.errors import AgentError

_SCRATCH_URL_ENV = "SIDERA_AGENTS_TEST_DATABASE_URL"
_SCRATCH_TARGET_FAILURE = (
    "The dedicated PostgreSQL test target is unsafe or unavailable."
)
_SCRATCH_CLEANUP_FAILURE = (
    "The owned PostgreSQL test target could not be safely cleaned."
)
_OWNED_TABLES = {
    "alembic_version",
    "profile_heads",
    "profile_revisions",
    "resource_heads",
    "resource_revisions",
}


class _ScratchTargetNotConfigured(Exception):
    pass


class _ScratchTargetUnavailable(Exception):
    pass


class _ScratchTargetCleanupFailed(Exception):
    pass


@dataclass(frozen=True)
class _OwnedAgentsPostgres:
    engine: Any
    url: Any
    lock_connection: Any
    configured_name: str

    def migrate(self, revision: str | None = None) -> None:
        _run_agents_migration(self.engine, revision)


def _is_local_host(host: object) -> bool:
    return isinstance(host, str) and (
        host.lower() in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")
    )


def _is_local_scratch_url(url: Any, environ: Mapping[str, str]) -> bool:
    query = {str(key).casefold(): value for key, value in url.query.items()}
    if (
        url.get_backend_name() != "postgresql"
        or not isinstance(url.database, str)
        or re.fullmatch(r"sidera_agents_test(?:_[a-z0-9_-]+)?", url.database) is None
        or "service" in query
        or "servicefile" in query
        or environ.get("PGSERVICE")
        or environ.get("PGSERVICEFILE")
        or environ.get("PGHOSTADDR")
    ):
        return False

    hosts: list[object] = []
    if url.host is not None:
        hosts.append(url.host)
    query_host = query.get("host")
    if query_host is not None:
        hosts.extend(
            query_host if isinstance(query_host, (tuple, list)) else (query_host,)
        )

    hostaddrs = query.get("hostaddr")
    if hostaddrs is not None:
        values = hostaddrs if isinstance(hostaddrs, (tuple, list)) else (hostaddrs,)
        if not all(_is_loopback_ip(value) for value in values):
            return False

    if hosts and not all(_is_local_host(host) for host in hosts):
        return False
    if hosts or hostaddrs is not None:
        return True

    environment_host = environ.get("PGHOST")
    return environment_host is None or _is_local_host(environment_host)


def _is_loopback_ip(value: object) -> bool:
    if not isinstance(value, str) or "%" in value:
        return False
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _scratch_database_identity(connection: Any) -> Any:
    from sqlalchemy import text

    return connection.execute(
        text(
            "SELECT current_database(), "
            "pg_get_userbyid(datdba) = current_user "
            "FROM pg_database WHERE datname = current_database()"
        )
    ).one()


def _scratch_target_facts(
    connection: Any, configured_name: str, identity: Any | None = None
) -> dict[str, object]:
    from sqlalchemy import text

    if identity is None:
        identity = _scratch_database_identity(connection)
    schemas = (
        connection.execute(
            text(
                "SELECT nspname FROM pg_namespace "
                "WHERE left(nspname, 3) <> 'pg_' "
                "AND nspname NOT IN ('information_schema', 'public')"
            )
        )
        .scalars()
        .all()
    )
    objects = connection.execute(
        text(
            "SELECT n.nspname, c.relname, "
            "CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' "
            "WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized_view' "
            "WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign_table' "
            "ELSE 'relation' END AS object_kind "
            "FROM pg_class AS c JOIN pg_namespace AS n "
            "ON n.oid = c.relnamespace "
            "WHERE n.nspname IN ('public', 'agents') "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') "
            "UNION ALL "
            "SELECT n.nspname, p.proname, 'function' "
            "FROM pg_proc AS p JOIN pg_namespace AS n "
            "ON n.oid = p.pronamespace "
            "WHERE n.nspname IN ('public', 'agents') "
            "UNION ALL "
            "SELECT n.nspname, t.typname, "
            "CASE t.typtype WHEN 'e' THEN 'enum' ELSE 'domain' END "
            "FROM pg_type AS t JOIN pg_namespace AS n "
            "ON n.oid = t.typnamespace "
            "WHERE n.nspname IN ('public', 'agents') "
            "AND t.typtype IN ('e', 'd')"
        )
    ).all()
    return {
        "database_name": identity[0],
        "configured_name": configured_name,
        "owner_matches": identity[1] is True,
        "custom_schemas": set(schemas),
        "objects": {tuple(row) for row in objects},
    }


def _acquire_owned_agents_pg(
    environ: Mapping[str, str],
    engine_factory: Callable[..., Any] | None = None,
) -> _OwnedAgentsPostgres:
    raw_url = environ.get(_SCRATCH_URL_ENV)
    if raw_url is None:
        raise _ScratchTargetNotConfigured

    try:
        from sqlalchemy.engine import make_url

        url = make_url(raw_url)
    except Exception:  # noqa: BLE001 - normalize URL parser details without echoing input
        raise _ScratchTargetUnavailable from None
    if not _is_local_scratch_url(url, environ):
        raise _ScratchTargetUnavailable

    if engine_factory is None:
        from sqlalchemy import create_engine

        engine_factory = create_engine

    engine = None
    connection = None
    lock_acquired = False
    try:
        from sqlalchemy import text

        engine = engine_factory(
            url,
            pool_pre_ping=True,
            echo=False,
            hide_parameters=True,
        )
        connection = engine.connect()
        identity = _scratch_database_identity(connection)
        if identity[0] != url.database or identity[1] is not True:
            raise _ScratchTargetUnavailable
        lock_acquired = (
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_class, :lock_id)"),
                {"lock_class": 1397310533, "lock_id": 2},
            ).scalar_one()
            is True
        )
        if not lock_acquired:
            raise _ScratchTargetUnavailable

        facts = _scratch_target_facts(connection, url.database, identity)
        if not _scratch_target_is_safe(facts):
            raise _ScratchTargetUnavailable
        return _OwnedAgentsPostgres(engine, url, connection, url.database)
    except Exception:  # noqa: BLE001 - sanitize driver errors and always release owned resources
        _dispose_scratch_connection(engine, connection, lock_acquired)
        raise _ScratchTargetUnavailable from None


def _dispose_scratch_connection(
    engine: Any, connection: Any, lock_acquired: bool
) -> bool:
    released = True
    if connection is not None and lock_acquired:
        try:
            from sqlalchemy import text

            released = (
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_class, :lock_id)"),
                    {"lock_class": 1397310533, "lock_id": 2},
                ).scalar_one()
                is True
            )
        except Exception:  # noqa: BLE001 - cleanup must fail closed on driver errors
            released = False
    if connection is not None:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - cleanup must fail closed on driver errors
            released = False
    if engine is not None:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001 - cleanup must fail closed on driver errors
            released = False
    return released


def _run_agents_migration(engine: Any, revision: str | None) -> None:
    try:
        from alembic import command
        from alembic.config import Config

        package_dir = Path(__file__).resolve().parents[1]
        config = Config(str(package_dir / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            if revision == "base":
                command.downgrade(config, "base")
            else:
                command.upgrade(config, revision or "head")
    except Exception:  # noqa: BLE001 - hide migration/driver details from test output
        raise _ScratchTargetUnavailable from None


def _cleanup_owned_agents_pg(
    target: _OwnedAgentsPostgres,
    *,
    migration: Callable[[Any, str | None], None] | None = None,
) -> None:
    migration = migration or _run_agents_migration
    expected_objects = {("agents", name, "table") for name in _OWNED_TABLES}
    try:
        facts = _scratch_target_facts(target.lock_connection, target.configured_name)
        if (
            facts["database_name"] != target.configured_name
            or facts["owner_matches"] is not True
            or facts["custom_schemas"] - {"agents"}
            or facts["objects"] - expected_objects
        ):
            raise _ScratchTargetCleanupFailed

        object_names = {obj[1] for obj in facts["objects"]}
        if not object_names:
            return
        if "alembic_version" not in object_names:
            raise _ScratchTargetCleanupFailed
        if object_names - {"alembic_version"}:
            migration(target.engine, "base")

        after_downgrade = _scratch_target_facts(
            target.lock_connection, target.configured_name
        )
        version_object = ("agents", "alembic_version", "table")
        if (
            after_downgrade["database_name"] != target.configured_name
            or after_downgrade["owner_matches"] is not True
            or after_downgrade["custom_schemas"] - {"agents"}
            or after_downgrade["objects"] - {version_object}
        ):
            raise _ScratchTargetCleanupFailed
        if after_downgrade["objects"]:
            from sqlalchemy import text

            versions = target.lock_connection.execute(
                text("SELECT version_num FROM agents.alembic_version")
            ).all()
            if versions:
                raise _ScratchTargetCleanupFailed
            target.lock_connection.execute(text("DROP TABLE agents.alembic_version"))

        final_facts = _scratch_target_facts(
            target.lock_connection, target.configured_name
        )
        if (
            final_facts["database_name"] != target.configured_name
            or final_facts["owner_matches"] is not True
            or final_facts["custom_schemas"] - {"agents"}
            or final_facts["objects"]
        ):
            raise _ScratchTargetCleanupFailed
    except _ScratchTargetCleanupFailed:
        raise
    except Exception:  # noqa: BLE001 - expose only the owned-target cleanup status
        raise _ScratchTargetCleanupFailed from None


def _scratch_target_is_safe(facts: dict[str, object]) -> bool:
    database_name = facts.get("database_name")
    configured_name = facts.get("configured_name")
    custom_schemas = facts.get("custom_schemas")
    objects = facts.get("objects")
    return (
        isinstance(database_name, str)
        and isinstance(configured_name, str)
        and re.fullmatch(r"sidera_agents_test(?:_[a-z0-9_-]+)?", database_name)
        is not None
        and database_name == configured_name
        and facts.get("owner_matches") is True
        and isinstance(custom_schemas, (set, frozenset))
        and custom_schemas <= {"agents"}
        and isinstance(objects, (set, frozenset, tuple, list))
        and not objects
    )


@pytest.fixture
def scratch_gate():
    return _scratch_target_is_safe


@pytest.fixture
def scratch_pg_acquire():
    return _acquire_owned_agents_pg


@pytest.fixture
def scratch_pg_cleanup():
    return _cleanup_owned_agents_pg


@pytest.fixture
def scratch_pg_release():
    return _dispose_scratch_connection


@pytest.fixture
def owned_agents_pg():
    try:
        target = _acquire_owned_agents_pg(os.environ)
    except _ScratchTargetNotConfigured:
        pytest.skip("SIDERA_AGENTS_TEST_DATABASE_URL is unset.")
    except Exception:  # noqa: BLE001 - do not echo target or driver details
        pytest.fail(_SCRATCH_TARGET_FAILURE, pytrace=False)

    cleanup_failed = False
    try:
        yield target
    finally:
        try:
            _cleanup_owned_agents_pg(target)
        except Exception:  # noqa: BLE001 - record cleanup failure without leaking driver data
            cleanup_failed = True
        released = _dispose_scratch_connection(
            target.engine, target.lock_connection, True
        )
        if cleanup_failed or not released:
            pytest.fail(_SCRATCH_CLEANUP_FAILURE, pytrace=False)


class OwnedWorkspaceAuthorizer:
    def __init__(self, workspace_id: UUID, grant_id: UUID) -> None:
        self.workspace_id = workspace_id
        self.grant_id = grant_id
        self.calls: list[tuple[UUID, str, object]] = []

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        self.calls.append((grant_id, action, target))
        target_workspace = getattr(target, "workspace_id", target)
        if grant_id != self.grant_id or target_workspace != self.workspace_id:
            raise AgentError("permission_denied", "Access denied.")


@pytest.fixture
def catalog_lab():
    from sidera_agents.catalog import ProfileCatalog
    from sidera_agents.memory import MemoryAgentStore

    workspace_id = uuid4()
    grant_id = uuid4()
    runtime_ref = RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4())
    skill_draft = CapabilityResourceDraft(
        name="review",
        kind="skill",
        settings={"body": "Check tests"},
        secret_refs=(),
    )
    draft = ProfileDraft(
        name="reviewer",
        description="Reviews changes",
        runtime_ref=runtime_ref,
        instructions="Review carefully.",
        resource_refs=(),
        model_id=None,
        thinking=None,
        max_active=1,
        environment={},
        secret_refs=(),
        native_settings={},
    )
    authorizer = OwnedWorkspaceAuthorizer(workspace_id, grant_id)
    clock = lambda: datetime(2026, 10, 1, tzinfo=UTC)
    catalog = ProfileCatalog(
        MemoryAgentStore(),
        authorizer,
        clock,
    )
    return SimpleNamespace(
        catalog=catalog,
        workspace_id=workspace_id,
        runtime_ref=runtime_ref,
        draft=draft,
        grant_id=grant_id,
        skill_draft=skill_draft,
        authorizer=authorizer,
        clock=clock,
    )
