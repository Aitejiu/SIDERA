import hashlib
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

COMPONENTS = ("product", "agents", "cp", "credentials")
SCHEMAS = {
    "product": "product",
    "agents": "agents",
    "cp": "public",
    "credentials": "sidera_configuration",
}
DB_KINDS = {
    "product": "product",
    "agents": "agents",
    "cp": "native_cp",
    "credentials": "cfg2",
}
SHARED_KEYS = tuple(
    "SIDERA_PRODUCT_TEST_" + name
    for name in ("ALLOCATION_ID", "SOCKET_DIRECTORY", "PORT")
)
SELECTION_KEYS = SHARED_KEYS + tuple(
    "SIDERA_PRODUCT_TEST_" + component.upper() + "_" + field
    for component in COMPONENTS
    for field in (
        "DATABASE_URL",
        "EXPECTED_DATABASE",
        "EXPECTED_OWNER",
        "EXPECTED_MARKER",
    )
)
CP_FUNCTIONS = {
    "sidera_reject_handoff_proposal_payload_update",
    "sidera_reject_handoff_bundle_payload_update",
    "sidera_reject_workspace_transfer_provenance_update",
    "sidera_reject_handoff_recovery_decision_mutation",
    "sidera_reject_admission_pin_mutation",
}


class ProductTargetError(RuntimeError):
    pass


@dataclass(frozen=True)
class TargetSelection:
    component: str
    lock_index: int
    url: URL = field(repr=False)
    database: str
    owner: str
    marker: str
    socket: str
    port: int


def select_targets(environ: Mapping[str, str]) -> tuple[TargetSelection, ...] | None:
    if not any(key in environ for key in SELECTION_KEYS):
        return None
    if any(not environ.get(key) for key in SELECTION_KEYS):
        raise ProductTargetError("product_selection_required")
    if any(key.startswith("PG") for key in set(environ) | set(os.environ)):
        raise ProductTargetError("product_routing_override_refused")
    allocation, socket, port_text = (environ[key] for key in SHARED_KEYS)
    path = Path(socket)
    if (
        not re.fullmatch(
            r"sidera-native-harness:product:n17:[0-9]{8}:[a-z0-9]+", allocation
        )
        or not path.is_absolute()
        or str(path) != socket
        or ".." in path.parts
        or "," in socket
        or any(ord(character) < 32 for character in socket)
        or not port_text.isdecimal()
        or not 0 < int(port_text) < 65536
    ):
        raise ProductTargetError("product_route_refused")
    targets = []
    for index, component in enumerate(COMPONENTS, 1):
        prefix = "SIDERA_PRODUCT_TEST_" + component.upper() + "_"
        database, owner, marker = (
            environ[prefix + name]
            for name in ("EXPECTED_DATABASE", "EXPECTED_OWNER", "EXPECTED_MARKER")
        )
        try:
            url = make_url(environ[prefix + "DATABASE_URL"])
        except (ArgumentError, ValueError):
            raise ProductTargetError("product_invalid_url") from None
        if (
            url.drivername != "postgresql+psycopg"
            or url.host is not None
            or url.port is not None
            or url.password is not None
            or url.username != owner
            or url.database != database
            or not re.fullmatch(
                "sidera_" + DB_KINDS[component] + r"_test_n17_[a-z0-9_]+", database
            )
            or len(database) > 63
            or not re.fullmatch("n17_" + component + r"_[a-z0-9_]+", owner)
            or len(owner) > 63
            or marker != allocation + ":" + component
            or dict(url.query) != {"host": socket, "port": port_text}
        ):
            raise ProductTargetError("product_target_refused")
        targets.append(
            TargetSelection(
                component, index, url, database, owner, marker, socket, int(port_text)
            )
        )
    return tuple(targets)


IDENTITY = text("""
SELECT current_database() AS database_name, current_user AS current_user,
       pg_get_userbyid(d.datdba) AS owner, shobj_description(d.oid, 'pg_database') AS marker,
       inet_server_addr() IS NULL AS unix_connection, r.rolcanlogin AS login,
       r.rolsuper AS superuser, r.rolcreaterole AS create_role,
       r.rolcreatedb AS create_database, r.rolreplication AS replication,
       r.rolbypassrls AS bypass_rls,
       EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid) AS privileged_memberships
FROM pg_database d JOIN pg_roles r ON r.rolname=current_user WHERE d.datname=current_database()
""")
SCHEMA_QUERY = text("""
SELECT nspname AS name, pg_get_userbyid(nspowner) AS owner FROM pg_namespace
WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
""")
RELATION_QUERY = text("""
SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
       pg_get_userbyid(c.relowner) AS owner, parent.relname AS parent_name, pn.nspname AS parent_schema
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN pg_index i ON i.indexrelid=c.oid
LEFT JOIN pg_depend d ON c.relkind='S' AND d.classid='pg_class'::regclass
    AND d.objid=c.oid AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
LEFT JOIN pg_class parent ON parent.oid=COALESCE(i.indrelid,d.refobjid)
LEFT JOIN pg_namespace pn ON pn.oid=parent.relnamespace
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
""")
FUNCTION_QUERY = text("""
SELECT n.nspname AS schema, p.proname AS name, pg_get_userbyid(p.proowner) AS owner,
       oidvectortypes(p.proargtypes) AS arguments, p.prorettype::regtype::text AS result,
       p.prosecdef AS security_definer
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
""")
TYPE_QUERY = text("""
SELECT n.nspname AS schema,t.typname AS name FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND t.typtype IN ('d','e','r','m')
""")
IDLE_QUERY = text("""
SELECT (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()) AS other_clients,
       (SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) AND pid<>pg_backend_pid()) AS other_advisory_locks,
       (SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()) AS prepared_transactions
""")


def verify_identity(target: TargetSelection, identity: Mapping[str, object]) -> None:
    if (
        identity["database_name"] != target.database
        or identity["current_user"] != target.owner
        or identity["owner"] != target.owner
        or identity["marker"] != target.marker
        or identity["unix_connection"] is not True
        or identity["login"] is not True
        or any(
            identity[name] is not False
            for name in (
                "superuser",
                "create_role",
                "create_database",
                "replication",
                "bypass_rls",
                "privileged_memberships",
            )
        )
    ):
        raise ProductTargetError("product_identity_refused")


def owned_tables(component: str) -> set[tuple[str, str]]:
    # Foundation ORM is used only for this test inventory, never product runtime.
    if component == "product":
        from sidera_product.postgres.models import Base
    elif component == "agents":
        from sidera_agents.postgres.models import Base
    elif component == "cp":
        from sidera_control_plane.persistence.models import Base
    else:
        from sidera_model_connections.postgres.models import Base
    return {
        (SCHEMAS[component], table.name) for table in Base.metadata.tables.values()
    } | {(SCHEMAS[component], "alembic_version")}


def verify_objects(
    connection: Connection, target: TargetSelection
) -> dict[str, object]:
    allowed = owned_tables(target.component)
    schema = SCHEMAS[target.component]
    schemas = [dict(row) for row in connection.execute(SCHEMA_QUERY).mappings()]
    relations = [dict(row) for row in connection.execute(RELATION_QUERY).mappings()]
    functions = [dict(row) for row in connection.execute(FUNCTION_QUERY).mappings()]
    for row in schemas:
        baseline = row["name"] == "public" and row["owner"] == "pg_database_owner"
        if not baseline and (row["name"] != schema or row["owner"] != target.owner):
            raise ProductTargetError("product_schema_refused")
    for row in relations:
        permitted = (
            row["kind"] in {"r", "p"} and (row["schema"], row["name"]) in allowed
        )
        if row["kind"] in {"i", "I", "S"}:
            permitted = (
                row["schema"] == schema
                and (row["parent_schema"], row["parent_name"]) in allowed
            )
        if not permitted or row["owner"] != target.owner:
            raise ProductTargetError("product_relation_refused")
    for row in functions:
        if (
            target.component != "cp"
            or row["schema"] != "public"
            or row["name"] not in CP_FUNCTIONS
            or row["owner"] != target.owner
            or row["arguments"] != ""
            or row["result"] != "trigger"
            or row["security_definer"] is not False
        ):
            raise ProductTargetError("product_function_refused")
    if connection.execute(TYPE_QUERY).first() is not None:
        raise ProductTargetError("product_type_refused")
    heads = []
    if any(
        row["schema"] == schema and row["name"] == "alembic_version"
        for row in relations
    ):
        heads = list(
            connection.execute(
                text(f'SELECT version_num FROM "{schema}".alembic_version')
            ).scalars()
        )
    return {
        "schemas": schemas,
        "relations": relations,
        "functions": functions,
        "heads": heads,
    }


@dataclass
class TargetGuard:
    target: TargetSelection
    engine: Engine
    connection: Connection
    initial: dict[str, object]
    active: bool = True

    def row_fingerprints(self) -> dict[str, dict[str, int | str]]:
        self.connection.rollback()
        try:
            inventory = verify_objects(self.connection, self.target)
            result = {}
            for relation in inventory["relations"]:
                if (
                    relation["kind"] not in {"r", "p"}
                    or relation["name"] == "alembic_version"
                ):
                    continue
                schema, name = relation["schema"], relation["name"]
                quote = self.connection.dialect.identifier_preparer.quote
                projection = "to_jsonb(original)"
                if self.target.component == "product" and name == "browser_sessions":
                    projection += " - 'csrf_digest'"
                rows = sorted(
                    self.connection.execute(
                        text(
                            f"SELECT ({projection})::text FROM {quote(schema)}.{quote(name)} AS original"
                        )
                    ).scalars()
                )
                digest = hashlib.sha256()
                for row in rows:
                    encoded = row.encode("utf-8")
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
                result[f"{schema}.{name}"] = {
                    "rows": len(rows),
                    "sha256": digest.hexdigest(),
                }
            return result
        finally:
            self.connection.rollback()

    def inspect(self) -> dict[str, object]:
        try:
            identity = dict(self.connection.execute(IDENTITY).mappings().one())
            verify_identity(self.target, identity)
            return {
                "component": self.target.component,
                "identity": identity,
                "inventory": verify_objects(self.connection, self.target),
                "idle": dict(self.connection.execute(IDLE_QUERY).mappings().one()),
            }
        finally:
            self.connection.rollback()

    def release(self) -> bool:
        if not self.active:
            return True
        complete = False
        try:
            self.connection.rollback()
            complete = (
                self.connection.execute(
                    text("SELECT pg_advisory_unlock(170100,:index)"),
                    {"index": self.target.lock_index},
                ).scalar_one()
                is True
            )
        finally:
            self.connection.close()
            self.engine.dispose()
        self.active = not complete
        return complete


def acquire_target(target: TargetSelection) -> TargetGuard:
    socket = Path(target.socket)
    metadata = socket.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or socket.resolve() != socket
    ):
        raise ProductTargetError("product_socket_refused")
    engine = create_engine(
        target.url,
        poolclass=NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 3,
            "options": "-c default_transaction_read_only=on -c search_path=pg_catalog -c statement_timeout=3000 -c lock_timeout=1000",
        },
    )
    connection = None
    locked = False
    try:
        connection = engine.connect()
        driver = connection.connection.driver_connection
        if (
            not isinstance(driver, psycopg.Connection)
            or driver.info.host != target.socket
            or driver.info.port != target.port
        ):
            raise ProductTargetError("product_actual_route_refused")
        identity = dict(connection.execute(IDENTITY).mappings().one())
        verify_identity(target, identity)
        inventory = verify_objects(connection, target)
        idle = dict(connection.execute(IDLE_QUERY).mappings().one())
        if any(value != 0 for value in idle.values()):
            raise ProductTargetError("product_target_busy")
        connection.rollback()
        locked = (
            connection.execute(
                text("SELECT pg_try_advisory_lock(170100,:index)"),
                {"index": target.lock_index},
            ).scalar_one()
            is True
        )
        if not locked:
            raise ProductTargetError("product_guard_unavailable")
        return TargetGuard(
            target,
            engine,
            connection,
            {
                "component": target.component,
                "identity": identity,
                "inventory": inventory,
                "idle": idle,
                "actual_host": driver.info.host,
                "actual_port": driver.info.port,
            },
        )
    except Exception:  # noqa: BLE001 - all configured target failures are sanitized before migration
        if connection is not None:
            if locked:
                connection.execute(
                    text("SELECT pg_advisory_unlock(170100,:index)"),
                    {"index": target.lock_index},
                )
            connection.close()
        engine.dispose()
        raise ProductTargetError("product_guard_validation_failed") from None


def acquire_guards(targets: Sequence[TargetSelection]) -> list[TargetGuard]:
    if tuple(target.component for target in targets) != COMPONENTS:
        raise ProductTargetError("product_guard_order_refused")
    guards = []
    try:
        for target in targets:
            guards.append(acquire_target(target))
    except Exception:
        for guard in reversed(guards):
            if not guard.release():
                raise ProductTargetError(
                    "product_partial_guard_cleanup_unconfirmed"
                ) from None
        raise
    return guards
