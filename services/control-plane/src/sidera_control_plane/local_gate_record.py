"""Machine-readable, provider-free evidence for local acceptance gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DirtyScope = Literal["clean", "protected-only", "dirty"]
GateStatus = Literal["PASS", "FAIL"]

DEFAULT_PROTECTED_PATHS = (
    ".superpowers/",
    "references/",
    "docs/superpowers/specs/2026-09-05-sidera-r6-3-4-scope-proposal.md",
)
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXCLUDED_FINGERPRINT_PREFIXES = (
    ".superpowers/",
    "docs/",
    "references/",
    ".sidera/",
)
_EXCLUDED_SECRET_NAMES = (
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)


class LocalGateRecordError(RuntimeError):
    """Raised when Git evidence cannot be collected without ambiguity."""


class DatabaseIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheme: str = Field(min_length=1)
    host: str = Field(min_length=1)
    name: str = Field(min_length=1)


class GateCheckRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exit_code: int
    status: GateStatus

    @model_validator(mode="after")
    def _status_matches_exit_code(self) -> GateCheckRecord:
        expected = "PASS" if self.exit_code == 0 else "FAIL"
        if self.status != expected:
            raise ValueError("gate check status does not match exit code")
        return self


class LocalGateRecordV1(BaseModel):
    """Stable local-gate evidence; it intentionally has no approved provider fields."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    commit: str = Field(pattern=_SHA1.pattern)
    implementation_fingerprint: str = Field(pattern=_SHA256.pattern)
    dirty_scope: DirtyScope
    database: DatabaseIdentity
    checks: dict[str, GateCheckRecord]
    status: GateStatus
    provider_free: bool = True

    @model_validator(mode="after")
    def _status_matches_checks(self) -> LocalGateRecordV1:
        checks_pass = all(check.status == "PASS" for check in self.checks.values())
        if self.status == "PASS" and (
            not checks_pass or self.dirty_scope == "dirty" or not self.provider_free
        ):
            raise ValueError("a local gate PASS requires clean checks and provider-free state")
        return self


@dataclass(frozen=True, slots=True)
class GateValidationResult:
    ok: bool
    code: str
    message: str


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _is_protected(path: str, protected_paths: Sequence[str]) -> bool:
    return any(
        path == protected or path.startswith(protected)
        for protected in protected_paths
    )


def parse_git_status_records(
    payload: bytes,
    *,
    protected_paths: Sequence[str] = DEFAULT_PROTECTED_PATHS,
) -> DirtyScope:
    """Classify porcelain-v1 NUL records without converting them to shell text."""

    records = payload.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    if not records:
        return "clean"

    dirty = False
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2:3] != b" ":
            raise LocalGateRecordError("malformed git status record")
        status = record[:2]
        path = _decode_path(record[3:])
        paths = [path]
        if b"R" in status or b"C" in status:
            index += 1
            if index >= len(records) or not records[index]:
                raise LocalGateRecordError("rename/copy record has no source path")
            paths.append(_decode_path(records[index]))
        if not all(_is_protected(value, protected_paths) for value in paths):
            dirty = True
        index += 1
    return "dirty" if dirty else "protected-only"


def classify_dirty_scope(
    repository: Path,
    *,
    protected_paths: Sequence[str] = DEFAULT_PROTECTED_PATHS,
) -> DirtyScope:
    """Run Git and classify every status record, including rename source paths."""

    try:
        completed = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=repository,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LocalGateRecordError("git status failed") from exc
    return parse_git_status_records(
        completed.stdout,
        protected_paths=protected_paths,
    )


def current_commit(repository: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LocalGateRecordError("git commit lookup failed") from exc
    value = completed.stdout.strip().lower()
    if _SHA1.fullmatch(value) is None:
        raise LocalGateRecordError("git commit lookup returned an invalid SHA")
    return value


def _fingerprint_path(path: str) -> bool:
    if any(path.startswith(prefix) for prefix in _EXCLUDED_FINGERPRINT_PREFIXES):
        return False
    name = Path(path).name.casefold()
    return not any(name == suffix or name.endswith(suffix) for suffix in _EXCLUDED_SECRET_NAMES)


def implementation_fingerprint(repository: Path) -> str:
    """Hash tracked executable/configuration inputs while ignoring documentation."""

    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LocalGateRecordError("git tracked-file lookup failed") from exc

    digest = hashlib.sha256()
    paths = sorted(
        _decode_path(raw)
        for raw in completed.stdout.split(b"\0")
        if raw and _fingerprint_path(_decode_path(raw))
    )
    for relative in paths:
        path = repository / relative
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        try:
            content = path.read_bytes()
        except OSError:
            content = b"<missing-tracked-file>"
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def database_identity(database_url: str) -> DatabaseIdentity:
    parsed = urlsplit(database_url)
    host = parsed.hostname or "invalid-host"
    name = unquote(parsed.path.lstrip("/")) or "invalid-database"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return DatabaseIdentity(
        scheme=parsed.scheme or "invalid",
        host=host,
        name=safe_name,
    )


def _normalise_checks(
    checks: Mapping[str, int | GateCheckRecord],
) -> dict[str, GateCheckRecord]:
    result: dict[str, GateCheckRecord] = {}
    for name, value in checks.items():
        result[name] = (
            value
            if isinstance(value, GateCheckRecord)
            else GateCheckRecord(
                exit_code=value,
                status="PASS" if value == 0 else "FAIL",
            )
        )
    return result


def build_local_gate_record(
    repository: Path,
    *,
    database_url: str,
    checks: Mapping[str, int | GateCheckRecord],
    status: GateStatus | None = None,
    dirty_scope: DirtyScope | None = None,
    commit: str | None = None,
) -> LocalGateRecordV1:
    normalized_checks = _normalise_checks(checks)
    resolved_scope = dirty_scope or classify_dirty_scope(repository)
    resolved_status = status or (
        "PASS"
        if normalized_checks
        and all(value.status == "PASS" for value in normalized_checks.values())
        and resolved_scope != "dirty"
        else "FAIL"
    )
    return LocalGateRecordV1(
        commit=commit or current_commit(repository),
        implementation_fingerprint=implementation_fingerprint(repository),
        dirty_scope=resolved_scope,
        database=database_identity(database_url),
        checks=normalized_checks,
        status=resolved_status,
    )


def write_local_gate_record(path: Path, record: LocalGateRecordV1) -> None:
    """Write evidence atomically so a failed run cannot leave a stale PASS file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record.model_dump(mode="json"), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def read_local_gate_record(path: Path) -> LocalGateRecordV1:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LocalGateRecordV1.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise LocalGateRecordError("local gate record is invalid") from exc


def validate_local_gate_record(
    record: LocalGateRecordV1 | Mapping[str, object] | Path,
    repository: Path,
) -> GateValidationResult:
    try:
        value = read_local_gate_record(record) if isinstance(record, Path) else record
        parsed = (
            value
            if isinstance(value, LocalGateRecordV1)
            else LocalGateRecordV1.model_validate(value)
        )
    except (LocalGateRecordError, ValidationError) as exc:
        return GateValidationResult(False, "local_gate_invalid", str(exc))
    if parsed.status != "PASS":
        return GateValidationResult(False, "local_gate_failed", "record status is FAIL")
    try:
        current_fingerprint = implementation_fingerprint(repository)
    except LocalGateRecordError as exc:
        return GateValidationResult(False, "local_gate_unavailable", str(exc))
    if parsed.implementation_fingerprint != current_fingerprint:
        return GateValidationResult(
            False,
            "local_gate_code_changed",
            "tracked implementation fingerprint changed",
        )
    try:
        scope = classify_dirty_scope(repository)
    except LocalGateRecordError as exc:
        return GateValidationResult(False, "local_gate_unavailable", str(exc))
    if scope == "dirty":
        return GateValidationResult(False, "local_gate_dirty", "unprotected changes exist")
    try:
        commit = current_commit(repository)
    except LocalGateRecordError as exc:
        return GateValidationResult(False, "local_gate_unavailable", str(exc))
    if parsed.commit != commit:
        return GateValidationResult(
            True,
            "local_gate_docs_changed",
            "only excluded documentation or protected evidence changed",
        )
    return GateValidationResult(True, "local_gate_valid", "local gate evidence is current")


def _read_checks_file(path: Path) -> dict[str, int | GateCheckRecord]:
    checks: dict[str, int | GateCheckRecord] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise LocalGateRecordError("malformed checks file")
        name, raw_exit, raw_status = parts
        if not name or raw_status not in {"PASS", "FAIL"}:
            raise LocalGateRecordError("malformed checks file")
        try:
            exit_code = int(raw_exit)
        except ValueError as exc:
            raise LocalGateRecordError("malformed checks file") from exc
        status: GateStatus = "PASS" if raw_status == "PASS" else "FAIL"
        checks[name] = GateCheckRecord(exit_code=exit_code, status=status)
    return checks


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser("classify")
    classify.add_argument("--repository", type=Path, required=True)

    fingerprint = subparsers.add_parser("fingerprint")
    fingerprint.add_argument("--repository", type=Path, required=True)

    write = subparsers.add_parser("write")
    write.add_argument("--output", type=Path, required=True)
    write.add_argument("--repository", type=Path, required=True)
    write.add_argument("--database-url", required=True)
    write.add_argument("--checks-file", type=Path, required=True)
    write.add_argument("--status", choices=("PASS", "FAIL"), required=True)
    write.add_argument("--dirty-scope", choices=("clean", "protected-only", "dirty"), required=True)
    write.add_argument("--commit")

    args = parser.parse_args()
    try:
        if args.command == "classify":
            print(classify_dirty_scope(args.repository))
        elif args.command == "fingerprint":
            print(implementation_fingerprint(args.repository))
        else:
            record = build_local_gate_record(
                args.repository,
                database_url=args.database_url,
                checks=_read_checks_file(args.checks_file),
                status=args.status,
                dirty_scope=args.dirty_scope,
                commit=args.commit,
            )
            write_local_gate_record(args.output, record)
    except (LocalGateRecordError, ValidationError, OSError) as exc:
        print(f"local gate record error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
