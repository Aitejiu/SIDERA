import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text

from .product_targets import COMPONENTS, ProductTargetError, TargetGuard, verify_objects

BASELINE_SCHEMA_VERSION = 1
BASELINE_FILE_PREFIX = "cold-original-baseline-"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RowInventory:
    tables: dict[str, dict[str, str]]

    def facts(self) -> dict[str, dict[str, str]]:
        return self.tables


@dataclass(frozen=True)
class OriginalBaseline:
    allocation_id: str
    target_identity: list[dict[str, object]]
    rows: dict[str, RowInventory]
    path: Path


def _target_identity(guard: TargetGuard) -> dict[str, object]:
    target = guard.target
    if target.url.password is not None:
        raise ProductTargetError("cold_original_baseline_refused")
    return {
        "component": target.component,
        "lock_index": target.lock_index,
        "drivername": target.url.drivername,
        "username": target.url.username,
        "url_database": target.url.database,
        "url_query": dict(target.url.query),
        "database": target.database,
        "owner": target.owner,
        "marker": target.marker,
        "socket": target.socket,
        "port": target.port,
    }


def _decode_original_baseline(
    value: object,
    *,
    path: Path,
    allocation_id: str,
    target_identity: list[dict[str, object]],
) -> OriginalBaseline:
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "allocation_id", "target_identity", "rows"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != BASELINE_SCHEMA_VERSION
        or value.get("allocation_id") != allocation_id
        or value.get("target_identity") != target_identity
        or type(value.get("rows")) is not dict
        or set(value["rows"]) != set(COMPONENTS)
    ):
        raise ProductTargetError("cold_original_baseline_refused")
    rows: dict[str, RowInventory] = {}
    for component in COMPONENTS:
        tables = value["rows"][component]
        if type(tables) is not dict:
            raise ProductTargetError("cold_original_baseline_refused")
        validated_tables: dict[str, dict[str, str]] = {}
        for table, identities in tables.items():
            if type(table) is not str or type(identities) is not dict:
                raise ProductTargetError("cold_original_baseline_refused")
            validated_identities: dict[str, str] = {}
            for identity, digest in identities.items():
                if (
                    type(identity) is not str
                    or type(digest) is not str
                    or _SHA256_PATTERN.fullmatch(digest) is None
                ):
                    raise ProductTargetError("cold_original_baseline_refused")
                validated_identities[identity] = digest
            validated_tables[table] = validated_identities
        rows[component] = RowInventory(validated_tables)
    return OriginalBaseline(allocation_id, target_identity, rows, path)


def load_or_capture_original_baseline(
    evidence_directory: Path,
    allocation_id: str,
    guards: Sequence[TargetGuard],
) -> OriginalBaseline:
    if (
        type(allocation_id) is not str
        or not allocation_id
        or tuple(guard.target.component for guard in guards) != COMPONENTS
        or any(not guard.active for guard in guards)
    ):
        raise ProductTargetError("cold_original_baseline_refused")
    target_identity = [_target_identity(guard) for guard in guards]
    allocation_digest = hashlib.sha256(allocation_id.encode("utf-8")).hexdigest()
    path = evidence_directory / (BASELINE_FILE_PREFIX + allocation_digest + ".json")
    try:
        candidates = tuple(evidence_directory.glob(BASELINE_FILE_PREFIX + "*.json"))
        if path in candidates:
            if candidates != (path,):
                raise ProductTargetError("cold_original_baseline_refused")
            value = read_private_json(path)
            return _decode_original_baseline(
                value,
                path=path,
                allocation_id=allocation_id,
                target_identity=target_identity,
            )
        if candidates:
            raise ProductTargetError("cold_original_baseline_refused")
        rows = {guard.target.component: capture_rows(guard) for guard in guards}
        facts = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "allocation_id": allocation_id,
            "target_identity": target_identity,
            "rows": {component: rows[component].facts() for component in COMPONENTS},
        }
        write_private_json(path, facts)
        return OriginalBaseline(allocation_id, target_identity, rows, path)
    except ProductTargetError:
        raise
    except (OSError, ValueError, TypeError):
        raise ProductTargetError("cold_original_baseline_refused") from None


def capture_rows(guard: TargetGuard) -> RowInventory:
    if not guard.active:
        raise ProductTargetError("product_guard_inactive")
    connection = guard.connection
    connection.rollback()
    try:
        inventory = verify_objects(connection, guard.target)
        quote = connection.dialect.identifier_preparer.quote
        inspector = sqlalchemy_inspect(connection)
        tables: dict[str, dict[str, str]] = {}
        for relation in inventory["relations"]:
            if (
                relation["kind"] not in {"r", "p"}
                or relation["name"] == "alembic_version"
            ):
                continue
            schema, name = relation["schema"], relation["name"]
            primary_key = inspector.get_pk_constraint(name, schema=schema)[
                "constrained_columns"
            ]
            if not primary_key:
                raise ProductTargetError("product_inventory_primary_key_missing")
            identity = (
                "jsonb_build_array("
                + ",".join(f"original.{quote(column)}" for column in primary_key)
                + ")::text"
            )
            row_json = "to_jsonb(original)"
            rows = connection.execute(
                text(
                    f"SELECT {identity} AS identity, ({row_json})::text AS row_json "
                    f"FROM {quote(schema)}.{quote(name)} AS original "
                    f"ORDER BY {identity}"
                )
            ).mappings()
            table_rows: dict[str, str] = {}
            for row in rows:
                table_rows[row["identity"]] = hashlib.sha256(
                    row["row_json"].encode("utf-8")
                ).hexdigest()
            tables[f"{schema}.{name}"] = table_rows
        return RowInventory(tables)
    finally:
        connection.rollback()


def compare_rows(before: RowInventory, after: RowInventory) -> dict[str, object]:
    missing: list[dict[str, str]] = []
    changed: list[dict[str, str]] = []
    added: list[dict[str, str]] = []
    for table, old_rows in before.tables.items():
        new_rows = after.tables.get(table, {})
        for identity, digest in old_rows.items():
            current = new_rows.get(identity)
            if current is None:
                missing.append({"table": table, "identity": identity})
            elif current != digest:
                changed.append({"table": table, "identity": identity})
    for table, new_rows in after.tables.items():
        old_rows = before.tables.get(table, {})
        added.extend(
            {"table": table, "identity": identity, "sha256": digest}
            for identity, digest in new_rows.items()
            if identity not in old_rows
        )
    return {
        "preserved": not missing and not changed,
        "missing": missing,
        "changed": changed,
        "added": added,
    }


def write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or stat.S_IMODE(directory.st_mode) != 0o700
        or directory.st_uid != os.getuid()
        or path.name in {".", ".."}
    ):
        raise ProductTargetError("product_evidence_directory_refused")
    payload = json.dumps(value, sort_keys=True, indent=2, default=str) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)


def read_private_json(path: Path) -> object:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
    ):
        raise ProductTargetError("product_checkpoint_refused")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        return json.load(stream)


def write_evidence(directory: Path, name: str, value: object) -> Path:
    path = directory / f"{name}-{uuid4().hex}.json"
    write_private_json(path, value)
    return path
