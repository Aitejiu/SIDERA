import hashlib
from pathlib import Path
from uuid import UUID

import pytest

from sidera_control_plane.persistence.artifact_content_store import (
    ArtifactContentIntegrityError,
    ArtifactContentUnavailable,
    LocalArtifactContentStore,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("70000000-0000-0000-0000-000000000001")
CONTENT = b"diff --git a/a.py b/a.py\n"
CONTENT_HASH = hashlib.sha256(CONTENT).hexdigest()


def test_put_is_hash_addressed_atomic_and_readable(tmp_path: Path) -> None:
    store = LocalArtifactContentStore(tmp_path, max_bytes=1024)

    reference = store.put(WORKSPACE_ID, EXECUTION_ID, CONTENT_HASH, CONTENT)

    path = tmp_path / str(WORKSPACE_ID) / CONTENT_HASH[:2] / CONTENT_HASH
    assert reference == f"artifact-local://{WORKSPACE_ID}/{EXECUTION_ID}/{CONTENT_HASH}"
    assert path.read_bytes() == CONTENT
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.read(reference) == CONTENT
    assert store.read_range(reference, (0, 9)) == CONTENT[:10]
    assert store.read_range(reference, (10, None)) == CONTENT[10:]


def test_put_and_read_reject_integrity_and_path_attacks(tmp_path: Path) -> None:
    store = LocalArtifactContentStore(tmp_path, max_bytes=1024)

    with pytest.raises(ArtifactContentIntegrityError):
        store.put(WORKSPACE_ID, EXECUTION_ID, "0" * 64, CONTENT)

    reference = store.put(WORKSPACE_ID, EXECUTION_ID, CONTENT_HASH, CONTENT)
    path = tmp_path / str(WORKSPACE_ID) / CONTENT_HASH[:2] / CONTENT_HASH
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    (tmp_path / "outside").write_bytes(CONTENT)
    with pytest.raises(ArtifactContentUnavailable):
        store.read(reference)

    with pytest.raises(ArtifactContentUnavailable):
        store.read("artifact-local://../other/execution/hash")

    linked_outside = tmp_path / "linked-outside"
    linked_outside.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(linked_outside, target_is_directory=True)
    with pytest.raises(ArtifactContentUnavailable):
        LocalArtifactContentStore(linked_root).put(
            WORKSPACE_ID,
            EXECUTION_ID,
            CONTENT_HASH,
            CONTENT,
        )


def test_read_rejects_oversized_and_unsatisfiable_ranges(tmp_path: Path) -> None:
    store = LocalArtifactContentStore(tmp_path, max_bytes=4)

    with pytest.raises(ArtifactContentIntegrityError):
        store.put(WORKSPACE_ID, EXECUTION_ID, CONTENT_HASH, CONTENT)

    reference = LocalArtifactContentStore(tmp_path / "ok", max_bytes=1024).put(
        WORKSPACE_ID, EXECUTION_ID, CONTENT_HASH, CONTENT
    )
    safe = LocalArtifactContentStore(tmp_path / "ok", max_bytes=1024)
    with pytest.raises(ArtifactContentUnavailable):
        safe.read_range(reference, (len(CONTENT), len(CONTENT) + 1))
