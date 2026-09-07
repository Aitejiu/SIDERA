from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID

from sidera_control_plane.application.result_ports import CapturedCode
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceLifecycle,
)

DEFAULT_MAX_EXPORT_BYTES = 10 * 1024 * 1024
_GIT_OID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "repository_ref",
        "manifest_id",
        "workspace_id",
        "task_id",
        "execution_id",
        "input_commit",
        "input_tree",
        "result_commit",
        "result_tree",
        "observed_agent_head",
        "result_ref",
        "receipt_ref",
        "writable_paths",
        "created_at",
        "author_name",
        "author_email",
        "workspace_base_revision",
    }
)


RepositoryResolver = Mapping[str, str | Path] | Callable[[str], str | Path]


class GitResultStore:
    """Capture immutable code results without mutating an agent worktree index."""

    def __init__(
        self,
        repository_resolver: RepositoryResolver,
        *,
        git_executable: str = "git",
        max_export_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
        clock: Callable[[], datetime] | None = None,
        author_name: str = "SIDERA Result Capture",
        author_email: str = "results@sidera.local",
    ) -> None:
        if not git_executable.strip():
            raise ValueError("git executable is required")
        if max_export_bytes <= 0:
            raise ValueError("max_export_bytes must be positive")
        if not author_name.strip() or not author_email.strip():
            raise ValueError("result author identity is required")
        if isinstance(repository_resolver, Mapping):
            self._configured_repositories = dict(repository_resolver)
            self._resolver: Callable[[str], str | Path] | None = None
        elif callable(repository_resolver):
            self._configured_repositories = {}
            self._resolver = repository_resolver
        else:
            raise TypeError("repository_resolver must be a mapping or callable")
        self._resolved_repositories: dict[str, Path] = {}
        self._git_executable = git_executable.strip()
        self._max_export_bytes = max_export_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._author_name = author_name.strip()
        self._author_email = author_email.strip()

    def capture(
        self,
        *,
        manifest: ExecutionWorkspaceManifest,
        input_commit: str,
        operation_id: UUID,
        writable_paths: Sequence[str],
    ) -> CapturedCode:
        """Capture the stopped workspace state for one idempotent operation."""

        if not isinstance(operation_id, UUID):
            raise ResultError("result_operation_identity_invalid", {})
        paths = _normalize_writable_paths(writable_paths)
        repository_ref = self._manifest_repository_ref(manifest)

        existing = self.inspect_capture(operation_id)
        if existing is not None:
            if (
                existing.repository_ref != repository_ref
                or existing.input_commit.lower() != input_commit.lower()
            ):
                raise ResultError(
                    "result_capture_identity_changed",
                    {"operation_id": str(operation_id)},
                )
            return existing

        repository_root = self._resolve_repository(repository_ref)
        repository = self._resolve_workspace(manifest, repository_root)
        canonical_input = self._resolve_commit(
            repository, input_commit, code="result_input_commit_invalid"
        )
        observed_agent_head = self._resolve_commit(
            repository, "HEAD", code="result_agent_head_invalid"
        )
        before = self._workspace_fingerprint(repository)
        changed_paths = self._changed_paths(repository, canonical_input)
        out_of_scope = tuple(
            path for path in changed_paths if not _path_in_scope(path, paths)
        )
        if out_of_scope:
            raise ResultError(
                "result_scope_violation",
                {"paths": list(out_of_scope)},
            )

        input_tree = self._resolve_tree(repository, canonical_input)
        with self._temporary_result_tree(
            repository,
            observed_agent_head,
            paths,
        ) as result_tree:
            self._reject_unsupported_entries(
                repository,
                result_tree,
                observed_agent_head=observed_agent_head,
            )
            after = self._workspace_fingerprint(repository)
            if before != after:
                raise ResultError(
                    "result_capture_workspace_changed",
                    {"operation_id": str(operation_id)},
                )
            created_at = self._capture_time()
            result_commit = self._commit_result(
                repository,
                result_tree=result_tree,
                input_commit=canonical_input,
                operation_id=operation_id,
                created_at=created_at,
            )
            if before != self._workspace_fingerprint(repository):
                raise ResultError(
                    "result_capture_workspace_changed",
                    {"operation_id": str(operation_id)},
                )

        result_ref = f"refs/sidera/results/{operation_id}"
        receipt_ref = _metadata_ref(operation_id)
        metadata = self._metadata_document(
            manifest=manifest,
            repository_ref=repository_ref,
            input_commit=canonical_input,
            input_tree=input_tree,
            result_commit=result_commit,
            result_tree=result_tree,
            observed_agent_head=observed_agent_head,
            operation_id=operation_id,
            result_ref=result_ref,
            receipt_ref=receipt_ref,
            writable_paths=paths,
            created_at=created_at,
        )
        metadata_oid = self._write_metadata(repository, metadata)
        updated = self._update_refs(
            repository,
            result_ref=result_ref,
            result_commit=result_commit,
            receipt_ref=receipt_ref,
            metadata_oid=metadata_oid,
        )
        if not updated:
            existing = self.inspect_capture(operation_id)
            if (
                existing is not None
                and existing.repository_ref == repository_ref
                and existing.input_commit == canonical_input
            ):
                return existing
            raise ResultError(
                "result_capture_ref_conflict",
                {"operation_id": str(operation_id)},
            )

        return CapturedCode(
            operation_id=operation_id,
            repository_ref=repository_ref,
            input_commit=canonical_input,
            input_tree=input_tree,
            result_commit=result_commit,
            result_tree=result_tree,
            observed_agent_head=observed_agent_head,
            receipt_ref=receipt_ref,
            created_at=created_at,
            _repository_path=repository,
        )

    def inspect_capture(self, operation_id: UUID) -> CapturedCode | None:
        """Read the immutable capture receipt and Git objects, never the worktree."""

        if not isinstance(operation_id, UUID):
            raise ResultError("result_operation_identity_invalid", {})
        matches: list[CapturedCode] = []
        for repository_ref, repository in self._known_repositories():
            result_ref = f"refs/sidera/results/{operation_id}"
            receipt_ref = _metadata_ref(operation_id)
            has_result = self._ref_exists(repository, result_ref)
            has_receipt = self._ref_exists(repository, receipt_ref)
            if not has_result and not has_receipt:
                continue
            if not has_result or not has_receipt:
                raise ResultError(
                    "result_capture_receipt_corrupt",
                    {"operation_id": str(operation_id)},
                )
            matches.append(
                self._inspect_repository_capture(
                    repository,
                    repository_ref=repository_ref,
                    operation_id=operation_id,
                )
            )
        if len(matches) > 1:
            raise ResultError(
                "result_capture_ambiguous",
                {"operation_id": str(operation_id)},
            )
        return matches[0] if matches else None

    def export_patch(self, snapshot: CapturedCode) -> bytes:
        if not isinstance(snapshot, CapturedCode):
            raise TypeError("snapshot must be CapturedCode")
        recorded = self.inspect_capture(snapshot.operation_id)
        if recorded is None:
            raise ResultError(
                "result_capture_not_found",
                {"operation_id": str(snapshot.operation_id)},
            )
        if recorded.to_mapping() != snapshot.to_mapping():
            raise ResultError(
                "result_capture_identity_changed",
                {"operation_id": str(snapshot.operation_id)},
            )
        repository = self._resolve_repository(snapshot.repository_ref)
        return _export_patch(
            repository,
            input_commit=recorded.input_commit,
            result_commit=recorded.result_commit,
            max_bytes=self._max_export_bytes,
            git_executable=self._git_executable,
        )

    def inspect_revision(
        self, *, repository_ref: str, revision: str
    ) -> tuple[str, str]:
        """Resolve the exact commit/tree used as a durable execution input."""
        repository = self._resolve_repository(repository_ref)
        commit = self._resolve_commit(
            repository, revision, code="result_input_commit_invalid"
        )
        return commit, self._resolve_tree(repository, commit)

    def _known_repositories(self) -> tuple[tuple[str, Path], ...]:
        values: dict[str, Path] = {}
        for repository_ref in self._configured_repositories:
            values[repository_ref] = self._resolve_repository(repository_ref)
        values.update(self._resolved_repositories)
        return tuple(values.items())

    def _resolve_repository(self, repository_ref: str) -> Path:
        if not isinstance(repository_ref, str) or not repository_ref.strip():
            raise ResultError("result_repository_identity_invalid", {})
        key = repository_ref.strip()
        if key in self._resolved_repositories:
            return self._resolved_repositories[key]
        try:
            if self._resolver is None:
                if key not in self._configured_repositories:
                    raise ResultError(
                        "result_repository_unconfigured",
                        {"repository_ref": key},
                    )
                raw_path = self._configured_repositories[key]
            else:
                raw_path = self._resolver(key)
        except ResultError:
            raise
        except Exception as exc:
            raise ResultError(
                "result_repository_unconfigured",
                {"repository_ref": key},
            ) from exc
        if raw_path is None:
            raise ResultError(
                "result_repository_unconfigured",
                {"repository_ref": key},
            )
        try:
            repository = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, TypeError) as exc:
            raise ResultError(
                "result_repository_unavailable",
                {"repository_ref": key},
            ) from exc
        if not repository.is_dir():
            raise ResultError(
                "result_repository_unavailable",
                {"repository_ref": key},
            )
        top_level = Path(
            self._git_output(
                repository,
                ["rev-parse", "--show-toplevel"],
                error_code="result_repository_invalid",
            )
        ).resolve()
        if not top_level.is_dir():
            raise ResultError(
                "result_repository_invalid",
                {"repository_ref": key},
            )
        self._resolved_repositories[key] = top_level
        return top_level

    def _resolve_workspace(
        self,
        manifest: ExecutionWorkspaceManifest,
        repository: Path,
    ) -> Path:
        checkout = manifest.checkouts[0]
        output = self._run_git(
            repository,
            ["worktree", "list", "--porcelain"],
            error_code="result_workspace_unavailable",
        ).stdout.decode("utf-8", errors="strict")
        for record in output.split("\n\n"):
            fields = dict(
                line.split(" ", maxsplit=1)
                for line in record.splitlines()
                if " " in line
            )
            if fields.get("branch") != f"refs/heads/{checkout.branch}":
                continue
            location = Path(fields["worktree"]).resolve()
            if not location.is_dir() or location.is_symlink():
                raise ResultError("result_workspace_unavailable", {})
            branch = self._git_output(
                location,
                ["branch", "--show-current"],
                error_code="result_workspace_unavailable",
            )
            if branch != checkout.branch:
                raise ResultError("result_workspace_unavailable", {})
            return location
        raise ResultError(
            "result_workspace_unavailable",
            {"execution_id": str(manifest.execution_id)},
        )

    @staticmethod
    def _manifest_repository_ref(manifest: ExecutionWorkspaceManifest) -> str:
        if not isinstance(manifest, ExecutionWorkspaceManifest):
            raise ResultError("result_manifest_invalid", {})
        if manifest.lifecycle in {
            WorkspaceLifecycle.RELEASED,
            WorkspaceLifecycle.QUARANTINED,
        }:
            raise ResultError(
                "result_workspace_unavailable",
                {"workspace_id": str(manifest.workspace_id)},
            )
        if len(manifest.checkouts) != 1:
            raise ResultError("result_manifest_invalid", {})
        return manifest.checkouts[0].repository_ref

    def _resolve_commit(self, repository: Path, revision: str, *, code: str) -> str:
        if not isinstance(revision, str):
            raise ResultError(code, {})
        requested = revision.strip()
        if requested != "HEAD" and _GIT_OID_PATTERN.fullmatch(requested) is None:
            raise ResultError(code, {})
        resolved = self._git_output(
            repository,
            ["rev-parse", "--verify", f"{requested}^{{commit}}"],
            error_code=code,
        )
        if requested != "HEAD" and resolved.lower() != requested.lower():
            raise ResultError(code, {})
        return resolved.lower()

    def _resolve_tree(self, repository: Path, commit: str) -> str:
        return self._git_output(
            repository,
            ["rev-parse", "--verify", f"{commit}^{{tree}}"],
            error_code="result_tree_invalid",
        ).lower()

    def _changed_paths(self, repository: Path, input_commit: str) -> tuple[str, ...]:
        tracked = self._run_git(
            repository,
            ["diff", "--name-only", "--no-renames", "-z", input_commit, "--"],
            error_code="result_workspace_inspection_failed",
        ).stdout
        untracked = self._run_git(
            repository,
            ["ls-files", "--others", "--exclude-standard", "-z"],
            error_code="result_workspace_inspection_failed",
        ).stdout
        return tuple(sorted(set(_decode_paths(tracked) + _decode_paths(untracked))))

    def _workspace_fingerprint(self, repository: Path) -> bytes:
        head = self._run_git(
            repository,
            ["rev-parse", "--verify", "HEAD^{commit}"],
            error_code="result_workspace_inspection_failed",
        ).stdout
        status = self._run_git(
            repository,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            error_code="result_workspace_inspection_failed",
        ).stdout
        return head + b"\0" + status

    @contextmanager
    def _temporary_result_tree(
        self,
        repository: Path,
        observed_agent_head: str,
        writable_paths: tuple[str, ...],
    ) -> Iterator[str]:
        temp_dir = Path(tempfile.mkdtemp(prefix="sidera-result-index-"))
        index = temp_dir / "index"
        environment = os.environ.copy()
        environment.pop("GIT_INDEX_FILE", None)
        environment["GIT_INDEX_FILE"] = str(index)
        try:
            self._run_git(
                repository,
                ["read-tree", observed_agent_head],
                env=environment,
                error_code="result_tree_capture_failed",
            )
            self._run_git(
                repository,
                ["add", "-A", "--", *writable_paths],
                env=environment,
                error_code="result_tree_capture_failed",
            )
            yield self._git_output(
                repository,
                ["write-tree"],
                env=environment,
                error_code="result_tree_capture_failed",
            ).lower()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _reject_unsupported_entries(
        self,
        repository: Path,
        result_tree: str,
        *,
        observed_agent_head: str,
    ) -> None:
        result_entries = _parse_tree_entries(
            self._run_git(
                repository,
                ["ls-tree", "-r", "-z", result_tree],
                error_code="result_tree_inspection_failed",
            ).stdout
        )
        observed_entries = _parse_tree_entries(
            self._run_git(
                repository,
                ["ls-tree", "-r", "-z", observed_agent_head],
                error_code="result_tree_inspection_failed",
            ).stdout
        )
        for mode, path in (*result_entries, *observed_entries):
            if mode == "160000":
                raise ResultError(
                    "result_submodule_unsupported",
                    {"path": path},
                )
        paths = tuple(path for mode, path in result_entries if mode != "160000")
        for start in range(0, len(paths), 128):
            batch = paths[start : start + 128]
            if not batch:
                continue
            output = self._run_git(
                repository,
                ["check-attr", "filter", "--", *batch],
                error_code="result_attribute_inspection_failed",
            ).stdout
            if any(b": filter: lfs" in line for line in output.splitlines()):
                raise ResultError("result_lfs_unsupported", {})

    def _capture_time(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ResultError("result_capture_clock_invalid", {})
        return value.astimezone(UTC)

    def _commit_result(
        self,
        repository: Path,
        *,
        result_tree: str,
        input_commit: str,
        operation_id: UUID,
        created_at: datetime,
    ) -> str:
        timestamp = int(created_at.timestamp())
        environment = os.environ.copy()
        environment.pop("GIT_INDEX_FILE", None)
        environment.update(
            {
                "GIT_AUTHOR_NAME": self._author_name,
                "GIT_AUTHOR_EMAIL": self._author_email,
                "GIT_COMMITTER_NAME": self._author_name,
                "GIT_COMMITTER_EMAIL": self._author_email,
                "GIT_AUTHOR_DATE": f"@{timestamp} +0000",
                "GIT_COMMITTER_DATE": f"@{timestamp} +0000",
            }
        )
        return self._git_output(
            repository,
            [
                "commit-tree",
                result_tree,
                "-p",
                input_commit,
                "-m",
                f"SIDERA result capture {operation_id}",
            ],
            env=environment,
            error_code="result_commit_failed",
        ).lower()

    def _metadata_document(
        self,
        *,
        manifest: ExecutionWorkspaceManifest,
        repository_ref: str,
        input_commit: str,
        input_tree: str,
        result_commit: str,
        result_tree: str,
        observed_agent_head: str,
        operation_id: UUID,
        result_ref: str,
        receipt_ref: str,
        writable_paths: tuple[str, ...],
        created_at: datetime,
    ) -> dict[str, object]:
        checkout = manifest.checkouts[0]
        return {
            "schema_version": 1,
            "operation_id": str(operation_id),
            "repository_ref": repository_ref,
            "manifest_id": str(manifest.id),
            "workspace_id": str(manifest.workspace_id),
            "task_id": str(manifest.task_id),
            "execution_id": str(manifest.execution_id),
            "input_commit": input_commit,
            "input_tree": input_tree,
            "result_commit": result_commit,
            "result_tree": result_tree,
            "observed_agent_head": observed_agent_head,
            "result_ref": result_ref,
            "receipt_ref": receipt_ref,
            "writable_paths": list(writable_paths),
            "created_at": created_at.isoformat(),
            "author_name": self._author_name,
            "author_email": self._author_email,
            "workspace_base_revision": checkout.base_revision,
        }

    def _write_metadata(self, repository: Path, metadata: Mapping[str, object]) -> str:
        encoded = (
            json.dumps(
                dict(metadata),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        return self._git_output(
            repository,
            ["hash-object", "-w", "--stdin"],
            input_bytes=encoded,
            error_code="result_metadata_write_failed",
        ).lower()

    def _update_refs(
        self,
        repository: Path,
        *,
        result_ref: str,
        result_commit: str,
        receipt_ref: str,
        metadata_oid: str,
    ) -> bool:
        zero = "0" * len(result_commit)
        transaction = (
            f"start\n"
            f"update {result_ref} {result_commit} {zero}\n"
            f"update {receipt_ref} {metadata_oid} {zero}\n"
            "prepare\n"
            "commit\n"
        ).encode()
        result = self._run_git(
            repository,
            ["update-ref", "--stdin"],
            input_bytes=transaction,
            check=False,
            error_code="result_capture_ref_write_failed",
        )
        return result.returncode == 0

    def _inspect_repository_capture(
        self,
        repository: Path,
        *,
        repository_ref: str,
        operation_id: UUID,
    ) -> CapturedCode:
        result_ref = f"refs/sidera/results/{operation_id}"
        receipt_ref = _metadata_ref(operation_id)
        metadata_oid = self._git_output(
            repository,
            ["rev-parse", "--verify", receipt_ref],
            error_code="result_capture_receipt_corrupt",
        )
        metadata_type = self._git_output(
            repository,
            ["cat-file", "-t", metadata_oid],
            error_code="result_capture_receipt_corrupt",
        )
        if metadata_type != "blob":
            raise ResultError(
                "result_capture_receipt_corrupt",
                {"operation_id": str(operation_id)},
            )
        raw = self._run_git(
            repository,
            ["cat-file", "-p", metadata_oid],
            error_code="result_capture_receipt_corrupt",
        ).stdout
        try:
            metadata = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResultError(
                "result_capture_receipt_corrupt",
                {"operation_id": str(operation_id)},
            ) from exc
        if not isinstance(metadata, dict) or set(metadata) != _METADATA_FIELDS:
            raise ResultError(
                "result_capture_receipt_corrupt",
                {"operation_id": str(operation_id)},
            )
        if metadata.get("schema_version") != 1:
            raise ResultError("result_capture_receipt_corrupt", {})
        if metadata.get("operation_id") != str(operation_id):
            raise ResultError("result_capture_receipt_corrupt", {})
        if metadata.get("repository_ref") != repository_ref:
            raise ResultError("result_capture_receipt_corrupt", {})
        if metadata.get("author_name") != self._author_name or metadata.get(
            "author_email"
        ) != self._author_email:
            raise ResultError("result_capture_receipt_corrupt", {})
        if metadata.get("result_ref") != result_ref or metadata.get("receipt_ref") != receipt_ref:
            raise ResultError("result_capture_receipt_corrupt", {})

        for name in ("manifest_id", "workspace_id", "task_id", "execution_id"):
            try:
                if str(UUID(str(metadata[name]))) != str(metadata[name]):
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise ResultError(
                    "result_capture_receipt_corrupt",
                    {"field": name, "operation_id": str(operation_id)},
                ) from exc

        input_commit = _metadata_oid(metadata, "input_commit")
        input_tree = _metadata_oid(metadata, "input_tree")
        result_commit = _metadata_oid(metadata, "result_commit")
        result_tree = _metadata_oid(metadata, "result_tree")
        observed_agent_head = _metadata_oid(metadata, "observed_agent_head")
        _metadata_oid(metadata, "workspace_base_revision")
        for name, value, suffix in (
            ("input_commit", input_commit, "commit"),
            ("result_commit", result_commit, "commit"),
            ("observed_agent_head", observed_agent_head, "commit"),
            ("input_tree", input_tree, "tree"),
            ("result_tree", result_tree, "tree"),
        ):
            resolved = self._git_output(
                repository,
                ["rev-parse", "--verify", f"{value}^{{{suffix}}}"],
                error_code="result_capture_receipt_corrupt",
            ).lower()
            if resolved != value:
                raise ResultError(
                    "result_capture_receipt_corrupt",
                    {"field": name, "operation_id": str(operation_id)},
                )
        ref_commit = self._git_output(
            repository,
            ["rev-parse", "--verify", f"{result_ref}^{{commit}}"],
            error_code="result_capture_receipt_corrupt",
        ).lower()
        if ref_commit != result_commit:
            raise ResultError("result_capture_receipt_corrupt", {})
        commit_tree = self._git_output(
            repository,
            ["show", "-s", "--format=%T", result_commit],
            error_code="result_capture_receipt_corrupt",
        ).lower()
        if commit_tree != result_tree:
            raise ResultError("result_capture_receipt_corrupt", {})
        parents = self._git_output(
            repository,
            ["show", "-s", "--format=%P", result_commit],
            error_code="result_capture_receipt_corrupt",
        ).split()
        if parents != [input_commit]:
            raise ResultError("result_capture_receipt_corrupt", {})
        try:
            parsed_created_at = datetime.fromisoformat(str(metadata["created_at"]))
            if parsed_created_at.tzinfo is None or parsed_created_at.utcoffset() is None:
                raise ValueError
            created_at = parsed_created_at.astimezone(UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise ResultError("result_capture_receipt_corrupt", {}) from exc
        writable_paths = metadata["writable_paths"]
        if not isinstance(writable_paths, list):
            raise ResultError("result_capture_receipt_corrupt", {})
        _normalize_writable_paths(writable_paths)
        return CapturedCode(
            operation_id=operation_id,
            repository_ref=repository_ref,
            input_commit=input_commit,
            input_tree=input_tree,
            result_commit=result_commit,
            result_tree=result_tree,
            observed_agent_head=observed_agent_head,
            receipt_ref=receipt_ref,
            created_at=created_at,
            _repository_path=repository,
        )

    def _ref_exists(self, repository: Path, ref: str) -> bool:
        result = self._run_git(
            repository,
            ["show-ref", "--verify", "--quiet", ref],
            check=False,
            error_code="result_capture_ref_inspection_failed",
        )
        return result.returncode == 0

    def _git_output(
        self,
        repository: Path,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        error_code: str,
    ) -> str:
        return self._run_git(
            repository,
            args,
            env=env,
            input_bytes=input_bytes,
            error_code=error_code,
        ).stdout.decode("utf-8", errors="strict").strip()

    def _run_git(
        self,
        repository: Path,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        check: bool = True,
        error_code: str,
    ) -> CompletedProcess[bytes]:
        environment = os.environ.copy()
        environment.pop("GIT_INDEX_FILE", None)
        if env is not None:
            environment.update(env)
        try:
            result = subprocess.run(
                [self._git_executable, *args],
                cwd=repository,
                check=check,
                capture_output=True,
                input=input_bytes,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise ResultError("git_unavailable", {}) from exc
        except OSError as exc:
            raise ResultError(error_code, {}) from exc
        except subprocess.CalledProcessError as exc:
            raise ResultError(error_code, {}) from exc
        return result


def export_patch(
    snapshot: CapturedCode,
    *,
    repository: Path | str | None = None,
    max_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
    git_executable: str = "git",
) -> bytes:
    """Export a fixed commit-to-commit patch for a captured result."""

    if not isinstance(snapshot, CapturedCode):
        raise TypeError("snapshot must be CapturedCode")
    if repository is None:
        repository = snapshot._repository_path
    if repository is None:
        raise ResultError(
            "result_repository_required",
            {"operation_id": str(snapshot.operation_id)},
        )
    return _export_patch(
        Path(repository).resolve(),
        input_commit=snapshot.input_commit,
        result_commit=snapshot.result_commit,
        max_bytes=max_bytes,
        git_executable=git_executable,
    )


def _export_patch(
    repository: Path,
    *,
    input_commit: str,
    result_commit: str,
    max_bytes: int,
    git_executable: str,
) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    environment = os.environ.copy()
    environment.pop("GIT_INDEX_FILE", None)
    try:
        result = subprocess.run(
            [
                git_executable,
                "diff",
                "--binary",
                "--no-ext-diff",
                input_commit,
                result_commit,
                "--",
            ],
            cwd=repository,
            check=True,
            capture_output=True,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise ResultError("git_unavailable", {}) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ResultError("result_patch_export_failed", {}) from exc
    if len(result.stdout) > max_bytes:
        raise ResultError(
            "result_export_too_large",
            {"size_bytes": len(result.stdout), "max_bytes": max_bytes},
        )
    return result.stdout


def _normalize_writable_paths(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ResultError("result_writable_scope_invalid", {})
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ResultError("result_writable_scope_invalid", {})
        path = item.strip().replace("\\", "/")
        if path in {"", "."}:
            path = "."
        if (
            path.startswith("/")
            or "\x00" in path
            or any(part in {"", ".."} for part in path.split("/"))
        ):
            raise ResultError("result_writable_scope_invalid", {})
        if path not in normalized:
            normalized.append(path)
    if "." in normalized:
        return (".",)
    return tuple(normalized)


def _path_in_scope(path: str, writable_paths: tuple[str, ...]) -> bool:
    return any(
        scope == "." or path == scope or path.startswith(f"{scope}/")
        for scope in writable_paths
    )


def _decode_paths(value: bytes) -> tuple[str, ...]:
    return tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in value.split(b"\0")
        if item
    )


def _parse_tree_entries(value: bytes) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for raw in value.split(b"\0"):
        if not raw:
            continue
        try:
            header, path = raw.split(b"\t", maxsplit=1)
            mode, _object_type, _object_id = header.split(b" ", maxsplit=2)
        except ValueError as exc:
            raise ResultError("result_tree_inspection_failed", {}) from exc
        entries.append(
            (
                mode.decode("ascii"),
                path.decode("utf-8", errors="surrogateescape"),
            )
        )
    return tuple(entries)


def _metadata_oid(metadata: Mapping[str, object], name: str) -> str:
    value = metadata.get(name)
    if not isinstance(value, str) or _GIT_OID_PATTERN.fullmatch(value) is None:
        raise ResultError("result_capture_receipt_corrupt", {"field": name})
    return value.lower()


def _metadata_ref(operation_id: UUID) -> str:
    return f"refs/sidera/results/{operation_id}.metadata"


__all__ = ["DEFAULT_MAX_EXPORT_BYTES", "GitResultStore", "export_patch"]
