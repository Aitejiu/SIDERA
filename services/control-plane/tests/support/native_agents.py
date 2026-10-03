"""Import these autouse fixtures only in native CP integration modules.

The dedicated child environment must be selected before pytest starts. Import
native_cp_guarded_database as well as both autouse fixtures: its local database
override explicitly guards the legacy fixture's migration dependency. The
native cleanup fixture depends on legacy cleanup so its finalizer runs first.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import PurePosixPath

import pytest
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import Base

_SELECTION_KEYS = (
    "SIDERA_NATIVE_CP_TEST_DATABASE_URL",
    "SIDERA_NATIVE_CP_TEST_OWNER",
    "SIDERA_NATIVE_CP_TEST_SOCKET",
    "SIDERA_NATIVE_CP_TEST_MARKER",
)
_ROUTING_ENV = (
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
)
_LOCK_PARAMETERS = {"namespace": "sidera-native-harness:cp-tests"}
_LOCK = text(
    "SELECT pg_catalog.pg_try_advisory_lock("
    "pg_catalog.hashtext(:namespace), pg_catalog.hashtext(current_database()))"
)
_UNLOCK = text(
    "SELECT pg_catalog.pg_advisory_unlock("
    "pg_catalog.hashtext(:namespace), pg_catalog.hashtext(current_database()))"
)
_IDENTITY = text("""
    SELECT current_database() AS database_name, current_user AS current_user,
           pg_catalog.pg_get_userbyid(d.datdba) AS owner,
           pg_catalog.shobj_description(d.oid, 'pg_database') AS marker,
           r.rolsuper AS superuser, r.rolcreaterole AS create_role,
           r.rolcreatedb AS create_database
    FROM pg_catalog.pg_database AS d
    JOIN pg_catalog.pg_roles AS r ON r.rolname = current_user
    WHERE d.datname = current_database()
""")
_SCHEMAS = text("""
    SELECT n.nspname AS name, pg_catalog.pg_get_userbyid(n.nspowner) AS owner
    FROM pg_catalog.pg_namespace AS n
    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
""")
_RELATIONS = text("""
    SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
           pg_catalog.pg_get_userbyid(c.relowner) AS owner,
           parent.relname AS parent_name, parent_ns.nspname AS parent_schema
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    LEFT JOIN pg_catalog.pg_index AS i ON i.indexrelid = c.oid
    LEFT JOIN pg_catalog.pg_depend AS d
      ON c.relkind = 'S' AND d.classid = 'pg_catalog.pg_class'::regclass
     AND d.objid = c.oid AND d.refclassid = 'pg_catalog.pg_class'::regclass
     AND d.deptype IN ('a', 'i')
    LEFT JOIN pg_catalog.pg_class AS parent
      ON parent.oid = COALESCE(i.indrelid, d.refobjid)
    LEFT JOIN pg_catalog.pg_namespace AS parent_ns
      ON parent_ns.oid = parent.relnamespace
    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
""")
_FUNCTIONS = text("""
    SELECT n.nspname AS schema, p.proname AS name,
           pg_catalog.pg_get_userbyid(p.proowner) AS owner,
           pg_catalog.oidvectortypes(p.proargtypes) AS arguments,
           p.prorettype::regtype::text AS result, p.prosecdef AS security_definer
    FROM pg_catalog.pg_proc AS p
    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
""")
_TYPES = text("""
    SELECT n.nspname AS schema, t.typname AS name, t.typtype AS kind
    FROM pg_catalog.pg_type AS t
    JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
      AND t.typtype IN ('d', 'e', 'r', 'm')
""")
# Owned trigger functions from the existing 0019/0020 CP migrations.
_CP_FUNCTIONS = frozenset(
    {
        "sidera_reject_handoff_proposal_payload_update",
        "sidera_reject_handoff_bundle_payload_update",
        "sidera_reject_workspace_transfer_provenance_update",
        "sidera_reject_handoff_recovery_decision_mutation",
        "sidera_reject_admission_pin_mutation",
    }
)
_NATIVE_CLEANUP_TABLES = (
    "agent_admission_pin_history",
    "agent_execution_links",
    "execution_authorities",
    "agent_bindings",
)


class NativeCPTestTargetError(RuntimeError):
    """Safe failure before using an unverified native test database."""


@dataclass(frozen=True)
class NativeCPTestTarget:
    url: URL = field(repr=False)
    database_name: str
    owner: str
    socket: str
    marker: str = field(repr=False)


@dataclass
class NativeCPTestGuard:
    target: NativeCPTestTarget
    relation_count: int
    active: bool = True


def _select_target(environ: Mapping[str, str]) -> NativeCPTestTarget:
    if any(not environ.get(key) for key in _SELECTION_KEYS):
        raise NativeCPTestTargetError("native_cp_dedicated_selection_required")
    if any(key in environ or key in os.environ for key in _ROUTING_ENV):
        raise NativeCPTestTargetError("native_cp_routing_override_refused")
    url = None
    try:
        url = make_url(environ[_SELECTION_KEYS[0]])
    except (ArgumentError, ValueError):
        url = None
    if url is None:
        raise NativeCPTestTargetError("native_cp_invalid_url")
    owner, socket, marker = (environ[key] for key in _SELECTION_KEYS[1:])
    if (
        url.drivername != "postgresql+psycopg"
        or not re.fullmatch(r"sidera_native_cp_test_[a-z0-9_]+", url.database or "")
        or len(url.database or "") > 63
        or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", owner)
        or url.username != owner
        or not marker.startswith("sidera-native-harness:cp:")
        or marker == "sidera-native-harness:cp:"
        or any(ord(char) < 32 or ord(char) == 127 for char in socket + marker)
        or any(key not in {"host", "port"} for key in url.query)
        or any(not isinstance(value, str) for value in url.query.values())
        or (url.host is not None and "host" in url.query)
        or (url.port is not None and "port" in url.query)
    ):
        raise NativeCPTestTargetError("native_cp_target_refused")
    host = url.query.get("host", url.host)
    port = url.query.get("port", str(url.port) if url.port is not None else "")
    if host != socket or not port.isdecimal() or not 0 < int(port) < 65536:
        raise NativeCPTestTargetError("native_cp_route_mismatch")
    local = False
    if socket.startswith("/"):
        path = PurePosixPath(socket)
        local = str(path) == socket and ".." not in path.parts and "," not in socket
    else:
        try:
            local = ip_address(socket).is_loopback
        except ValueError:
            pass
    if not local:
        raise NativeCPTestTargetError("native_cp_nonlocal_route_refused")
    return NativeCPTestTarget(url, url.database, owner, socket, marker)


def native_cp_test_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return a validated child selection, without changing the parent env."""
    _select_target(environ)
    return {**environ, "SIDERA_DATABASE_URL": environ[_SELECTION_KEYS[0]]}


def _verify_identity(connection: Connection, target: NativeCPTestTarget) -> None:
    identity = connection.execute(_IDENTITY).mappings().one()
    if (
        identity["database_name"] != target.database_name
        or identity["current_user"] != target.owner
        or identity["owner"] != target.owner
        or identity["marker"] != target.marker
        or any(
            identity[key] is not False
            for key in ("superuser", "create_role", "create_database")
        )
    ):
        raise NativeCPTestTargetError("native_cp_identity_refused")


def _verify_objects(connection: Connection, target: NativeCPTestTarget) -> int:
    allowed_tables = {("public", "alembic_version")}
    for table in Base.metadata.tables.values():
        if table.schema not in (None, "public"):
            raise NativeCPTestTargetError("native_cp_metadata_scope_refused")
        allowed_tables.add(("public", table.name))
    for row in connection.execute(_SCHEMAS).mappings().all():
        if row["name"] != "public" or row["owner"] not in (
            target.owner,
            "pg_database_owner",
        ):
            raise NativeCPTestTargetError("native_cp_schema_refused")
    relations = connection.execute(_RELATIONS).mappings().all()
    for row in relations:
        allowed = (row["schema"], row["name"]) in allowed_tables and row["kind"] in (
            "r",
            "p",
        )
        if row["kind"] in ("i", "I", "S"):
            allowed = (
                row["schema"] == "public"
                and (row["parent_schema"], row["parent_name"]) in allowed_tables
            )
        if not allowed or row["owner"] != target.owner:
            raise NativeCPTestTargetError("native_cp_relation_refused")
    for row in connection.execute(_FUNCTIONS).mappings().all():
        if (
            row["schema"] != "public"
            or row["name"] not in _CP_FUNCTIONS
            or row["owner"] != target.owner
            or row["arguments"] != ""
            or row["result"] != "trigger"
            or row["security_definer"] is not False
        ):
            raise NativeCPTestTargetError("native_cp_function_refused")
    if connection.execute(_TYPES).mappings().all():
        raise NativeCPTestTargetError("native_cp_type_refused")
    return len(relations)


def _release_guard(
    connection: Connection | None, engine: Engine | None, locked: bool
) -> bool:
    failed = False
    if connection is not None:
        try:
            connection.rollback()
            if (
                locked
                and connection.execute(_UNLOCK, _LOCK_PARAMETERS).scalar_one()
                is not True
            ):
                failed = True
        except Exception:  # noqa: BLE001 - Sanitize driver errors during teardown.
            failed = True
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - Still dispose after a close failure.
                failed = True
    if engine is not None:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001 - Never report raw driver diagnostics.
            failed = True
    return failed


def _native_engine(
    target: NativeCPTestTarget, factory: Callable[..., Engine], *, read_only: bool
) -> Engine:
    options = "-c statement_timeout=3000 -c lock_timeout=1000 "
    options += (
        "-c default_transaction_read_only=on -c search_path=pg_catalog"
        if read_only
        else "-c search_path=public,pg_catalog"
    )
    return factory(
        target.url,
        hide_parameters=True,
        echo=False,
        poolclass=NullPool,
        connect_args={"connect_timeout": 3, "options": options},
    )


@contextmanager
def guard_native_cp_test_target(
    database_url: str,
    *,
    environ: Mapping[str, str] | None = None,
    engine_factory: Callable[..., Engine] | None = None,
) -> Iterator[NativeCPTestGuard]:
    environ = os.environ if environ is None else environ
    target = _select_target(environ)
    if database_url != environ[_SELECTION_KEYS[0]]:
        raise NativeCPTestTargetError("native_cp_fixture_url_mismatch")
    engine = connection = guard = None
    locked = False
    try:
        reason = None
        try:
            engine = _native_engine(
                target, engine_factory or create_engine, read_only=True
            )
            connection = engine.connect()
            connection.execute(text("SET TRANSACTION READ ONLY"))
            _verify_identity(connection, target)
            locked = connection.execute(_LOCK, _LOCK_PARAMETERS).scalar_one() is True
            if not locked:
                raise NativeCPTestTargetError("native_cp_target_busy")
            relation_count = _verify_objects(connection, target)
        except NativeCPTestTargetError as exc:
            reason = str(exc)
        except Exception:  # noqa: BLE001 - The I/O boundary must redact diagnostics.
            reason = "native_cp_guard_read_failed"
        # Leave the exception handler before raising, so raw driver diagnostics
        # cannot survive in the safe exception's cause or context.
        if reason is not None:
            raise NativeCPTestTargetError(reason)
        guard = NativeCPTestGuard(target, relation_count)
        yield guard
    finally:
        if guard is not None:
            guard.active = False
        if _release_guard(connection, engine, locked):
            raise NativeCPTestTargetError("native_cp_guard_release_failed")


def cleanup_native_cp_tables(database: Database, guard: NativeCPTestGuard) -> None:
    """Remove only registered N10 CP tables while the target lock is retained."""
    if not guard.active or database.engine.url != guard.target.url:
        raise NativeCPTestTargetError("native_cp_cleanup_target_refused")
    if any(key in os.environ for key in _ROUTING_ENV):
        raise NativeCPTestTargetError("native_cp_routing_override_refused")
    engine = None
    reason = None
    try:
        engine = _native_engine(guard.target, create_engine, read_only=False)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT pg_catalog.set_config('statement_timeout', :statement, true), "
                    "pg_catalog.set_config('lock_timeout', :lock, true)"
                ),
                {"statement": "3000", "lock": "1000"},
            )
            _verify_identity(connection, guard.target)
            _verify_objects(connection, guard.target)
            for name in _NATIVE_CLEANUP_TABLES:
                table = Base.metadata.tables.get(name)
                if table is not None:
                    if name == "agent_admission_pin_history":
                        connection.execute(
                            text(
                                "ALTER TABLE agent_admission_pin_history DISABLE TRIGGER trg_agent_admission_pin_append_only"
                            )
                        )
                        connection.execute(delete(table))
                        connection.execute(
                            text(
                                "ALTER TABLE agent_admission_pin_history ENABLE TRIGGER trg_agent_admission_pin_append_only"
                            )
                        )
                    else:
                        connection.execute(delete(table))
    except NativeCPTestTargetError as exc:
        reason = str(exc)
    except Exception:  # noqa: BLE001 - Cleanup must not expose driver diagnostics.
        reason = "native_cp_cleanup_failed"
    finally:
        if _release_guard(None, engine, False):
            reason = "native_cp_cleanup_release_failed"
    if reason is not None:
        raise NativeCPTestTargetError(reason)


@pytest.fixture(scope="session", autouse=True)
def native_cp_test_target_guard(database_url: str) -> Iterator[NativeCPTestGuard]:
    with guard_native_cp_test_target(database_url) as guard:
        yield guard


@pytest.fixture(scope="session", name="database")
def native_cp_guarded_database(
    native_cp_test_target_guard: NativeCPTestGuard, database: Database
) -> Database:
    """Locally override database, guarding its unchanged legacy dependency."""
    return database


@pytest.fixture(autouse=True)
def native_cp_test_cleanup(
    native_cp_test_target_guard: NativeCPTestGuard,
    database: Database,
    clean_database: None,
) -> Iterator[None]:
    try:
        yield
    finally:
        cleanup_native_cp_tables(database, native_cp_test_target_guard)


class PublicAgentCatalog:
    """Only the external catalog I/O used by assignment tests is replaced."""

    def __init__(self, profiles, resources=()):
        self.profiles = {profile.ref: profile for profile in profiles}
        self.resources = {resource.ref: resource for resource in resources}
        self.enabled = True
        self.calls = []

    def list_profiles(self, workspace_id, *, grant_id):
        self.calls.append(("list_profiles", workspace_id, grant_id))
        return tuple(
            p for p in self.profiles.values() if p.ref.workspace_id == workspace_id
        )

    def get_profile(self, ref, *, grant_id):
        self.calls.append(("get_profile", ref, grant_id))
        return self.profiles[ref]

    def get_profile_status(self, ref, *, grant_id):
        from sidera_agents.contracts import ProfileStatus

        self.calls.append(("get_profile_status", ref, grant_id))
        return ProfileStatus(profile_id=ref.profile_id, enabled=self.enabled, version=1)

    def get_resource(self, ref, *, grant_id):
        self.calls.append(("get_resource", ref, grant_id))
        return self.resources[ref]

    def get_resource_status(self, ref, *, grant_id):
        from sidera_agents.contracts import ResourceStatus

        self.calls.append(("get_resource_status", ref, grant_id))
        return ResourceStatus(
            resource_id=ref.resource_id, enabled=self.enabled, version=1
        )


class PublicAgentRuntimes:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []

    def validate_profile(self, ref, *, grant_id):
        from sidera_agents.contracts import ConfigValidation

        self.calls.append(("validate_profile", ref, grant_id))
        return ConfigValidation(valid=True, issues=(), effective_preview=None)

    def get_runtime(self, ref, *, grant_id):
        self.calls.append(("get_runtime", ref, grant_id))
        assert ref == self.runtime.ref
        return self.runtime


class AssignmentScope:
    def __init__(self, workspace_id, principal_id, grant_id, *, actions=None):
        self.workspace_id = workspace_id
        self.principal_id = principal_id
        self.grant_id = grant_id
        self.revoked = False
        self.actions = frozenset(
            {"assignment.read", "assignment.write"} if actions is None else actions
        )

    def require(self, grant_id, action, target):
        from sidera_control_plane.domain.agent_bindings import AssignmentDenied

        if (
            self.revoked
            or grant_id != self.grant_id
            or target.workspace_id != self.workspace_id
            or target.principal_id != self.principal_id
            or action not in self.actions
        ):
            raise AssignmentDenied("assignment scope denied")


class ControlledAssignmentHarness:
    harness_id = "cp-assignment-fixture"
    executable_name = "cp-assignment-fixture"

    def __init__(self):
        self.opens = self.prompts = self.probes = 0

    def describe(self):
        from sidera_agents.contracts import RuntimeDescriptor

        return RuntimeDescriptor(
            harness_id=self.harness_id,
            display_name="CP fixture",
            fields=(),
            model_options=(),
            thinking_options=(),
        )

    def probe(self, candidate, checked_at):
        from sidera_agents.contracts import CapabilitySupport, RuntimeProbe

        self.probes += 1
        return RuntimeProbe(
            actual_version="fixture-1",
            protocol_state="ready",
            checked_at=checked_at,
            capabilities={
                "model": CapabilitySupport(
                    status="unconfirmed",
                    reason_code="model_response_unconfirmed",
                    constraints={},
                ),
                "native_tools": CapabilitySupport(
                    status="unconfirmed",
                    reason_code="native_tool_execution_unconfirmed",
                    constraints={},
                ),
            },
        )

    def validate(self, profile, runtime):
        from sidera_agents.contracts import ConfigValidation

        return ConfigValidation(valid=True, issues=(), effective_preview=None)

    def open(self, request):
        self.opens += 1
        raise AssertionError("assignment must not open a native process")

    def prompt(self, handle, input, response_id):
        self.prompts += 1
        raise AssertionError("assignment must not prompt a native process")

    def read_events(self, handle, after_cursor, limit=100):
        raise AssertionError("assignment must not read native events")

    def cancel(self, handle, response_id):
        raise AssertionError("assignment must not cancel native execution")

    def inspect(self, handle):
        raise AssertionError("assignment must not inspect native execution")

    def close(self, handle):
        raise AssertionError("assignment must not close native execution")


class PublicWorkspaceScope:
    def __init__(self, workspace_id, grant_id):
        self.workspace_id, self.grant_id = workspace_id, grant_id

    def require(self, grant_id, action, target):
        from sidera_agents.errors import AgentError

        if (
            grant_id != self.grant_id
            or getattr(target, "workspace_id", target) != self.workspace_id
            or action
            not in {
                "runtime.register",
                "runtime.read",
                "profile.create",
                "profile.read",
                "profile.list",
            }
        ):
            raise AgentError("permission_denied", "Access denied.")
