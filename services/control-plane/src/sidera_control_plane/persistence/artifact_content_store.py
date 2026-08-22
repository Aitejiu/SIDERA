from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from uuid import UUID

_REFERENCE_PATTERN = re.compile(
    r"^artifact-local://(?P<workspace>[0-9a-fA-F-]{36})/(?P<execution>[0-9a-fA-F-]{36})/(?P<digest>[0-9a-fA-F]{64})$"
)


class ArtifactContentError(RuntimeError):
    pass


class ArtifactContentIntegrityError(ArtifactContentError):
    pass


class ArtifactContentUnavailable(ArtifactContentError):
    pass


class LocalArtifactContentStore:
    def __init__(self, root: Path, *, max_bytes: int = 10_485_760) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._root = Path(os.path.abspath(Path(root).expanduser()))
        self._max_bytes = max_bytes

    def put(
        self,
        workspace_id: UUID,
        execution_id: UUID,
        expected_hash: str,
        content: bytes,
    ) -> str:
        digest = hashlib.sha256(content).hexdigest()
        expected = expected_hash.lower()
        if digest != expected:
            raise ArtifactContentIntegrityError("artifact content hash mismatch")
        if len(content) > self._max_bytes:
            raise ArtifactContentIntegrityError("artifact content exceeds configured size")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ArtifactContentIntegrityError("artifact content hash is invalid")
        directory = self._content_directory(workspace_id, expected)
        self._ensure_directory(directory)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory,
                prefix=f".{expected}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._content_path(workspace_id, expected))
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return f"artifact-local://{workspace_id}/{execution_id}/{expected}"

    def read(self, reference: str) -> bytes:
        workspace_id, _execution_id, digest = self._parse_reference(reference)
        return self._read_verified(workspace_id, digest)

    def read_range(
        self, reference: str, byte_range: tuple[int | None, int | None]
    ) -> bytes:
        content = self.read(reference)
        start, end = _resolve_range(len(content), byte_range)
        return content[start : end + 1]

    def content_metadata(self, reference: str) -> tuple[UUID, UUID, str, int]:
        workspace_id, execution_id, digest = self._parse_reference(reference)
        content = self._read_verified(workspace_id, digest)
        return workspace_id, execution_id, digest, len(content)

    def _read_verified(self, workspace_id: UUID, digest: str) -> bytes:
        path = self._content_path(workspace_id, digest)
        self._assert_no_symlink(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise ArtifactContentUnavailable("artifact content is unavailable") from exc
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                content = stream.read(self._max_bytes + 1)
        except OSError as exc:
            raise ArtifactContentUnavailable("artifact content cannot be read") from exc
        if len(content) > self._max_bytes:
            raise ArtifactContentIntegrityError("artifact content exceeds configured size")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ArtifactContentIntegrityError("artifact content integrity check failed")
        return content

    def _content_directory(self, workspace_id: UUID, digest: str) -> Path:
        return self._root / str(workspace_id) / digest[:2]

    def _content_path(self, workspace_id: UUID, digest: str) -> Path:
        return self._content_directory(workspace_id, digest) / digest

    def _ensure_directory(self, directory: Path) -> None:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._assert_no_symlink(self._root)
        workspace_directory = directory.parent
        workspace_directory.mkdir(mode=0o700, exist_ok=True)
        self._assert_no_symlink(workspace_directory)
        directory.mkdir(mode=0o700, exist_ok=True)
        self._assert_no_symlink(directory)

    def _assert_no_symlink(self, path: Path) -> None:
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise ArtifactContentUnavailable("artifact path escapes content root") from exc
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                if os.path.islink(current):
                    raise ArtifactContentUnavailable("artifact path contains a symlink")
                os.lstat(current)
            except FileNotFoundError as exc:
                raise ArtifactContentUnavailable("artifact content is unavailable") from exc

    @staticmethod
    def _parse_reference(reference: str) -> tuple[UUID, UUID, str]:
        match = _REFERENCE_PATTERN.fullmatch(reference)
        if match is None:
            raise ArtifactContentUnavailable("artifact reference is invalid")
        try:
            return (
                UUID(match.group("workspace")),
                UUID(match.group("execution")),
                match.group("digest").lower(),
            )
        except ValueError as exc:
            raise ArtifactContentUnavailable("artifact reference is invalid") from exc


def _resolve_range(
    size: int, byte_range: tuple[int | None, int | None]
) -> tuple[int, int]:
    start, end = byte_range
    if size == 0:
        raise ArtifactContentUnavailable("artifact content is empty")
    if start is None:
        if end is None or end <= 0:
            raise ArtifactContentUnavailable("artifact byte range is invalid")
        start, end = max(size - end, 0), size - 1
    else:
        if start < 0:
            raise ArtifactContentUnavailable("artifact byte range is invalid")
        end = size - 1 if end is None else end
    if end < start or start >= size:
        raise ArtifactContentUnavailable("artifact byte range is unsatisfiable")
    return start, min(end, size - 1)
