from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sidera_control_plane.local_gate_record import (
    LocalGateRecordError,
    build_local_gate_record,
    classify_dirty_scope,
    implementation_fingerprint,
    read_local_gate_record,
    validate_local_gate_record,
    write_local_gate_record,
)


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Sidera Test")
    (repository / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (repository / "docs").mkdir()
    (repository / "docs" / "note.md").write_text("initial\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initial")
    return repository


def test_classify_clean_and_protected_only_records(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    assert classify_dirty_scope(repository) == "clean"

    protected = repository / "references"
    protected.mkdir()
    (protected / "中文 file\n.txt").write_text("reference\n", encoding="utf-8")
    (repository / ".superpowers").mkdir()
    (repository / ".superpowers" / "state.json").write_text("{}\n", encoding="utf-8")
    assert classify_dirty_scope(repository) == "protected-only"


def test_classify_tracked_change_and_mixed_paths_as_dirty(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "app.py").write_text("print('changed')\n", encoding="utf-8")
    (repository / "new file 中文.txt").write_text("new\n", encoding="utf-8")
    assert classify_dirty_scope(repository) == "dirty"


def test_classify_rename_checks_both_source_and_destination(tmp_path: Path) -> None:
    protected_source = _repository(tmp_path / "protected")
    (protected_source / "references").mkdir()
    (protected_source / "references" / "old name.txt").write_text("ref\n", encoding="utf-8")
    _git(protected_source, "add", ".")
    _git(protected_source, "commit", "-qm", "reference")
    _git(protected_source, "mv", "references/old name.txt", "references/new name.txt")
    assert classify_dirty_scope(protected_source) == "protected-only"

    mixed = _repository(tmp_path / "mixed")
    (mixed / "references").mkdir()
    (mixed / "references" / "old.txt").write_text("ref\n", encoding="utf-8")
    _git(mixed, "add", ".")
    _git(mixed, "commit", "-qm", "reference")
    _git(mixed, "mv", "references/old.txt", "moved.txt")
    assert classify_dirty_scope(mixed) == "dirty"


def test_non_git_repository_is_a_classification_failure(tmp_path: Path) -> None:
    with pytest.raises(LocalGateRecordError):
        classify_dirty_scope(tmp_path)


def test_failed_check_cannot_produce_pass(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = build_local_gate_record(
        repository,
        database_url="postgresql+psycopg://tester:secret@127.0.0.1:55432/sidera_test",
        checks={"unit": 0, "integration": 1},
    )
    assert record.status == "FAIL"
    assert record.checks["integration"].exit_code == 1
    assert record.database.host == "127.0.0.1"
    assert "secret" not in json.dumps(record.model_dump())


def test_code_change_rejects_old_evidence(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = build_local_gate_record(
        repository,
        database_url="postgresql+psycopg://tester@127.0.0.1:55432/sidera_test",
        checks={"unit": 0},
    )
    record_path = tmp_path / "gate.json"
    write_local_gate_record(record_path, record)
    (repository / "app.py").write_text("print('changed')\n", encoding="utf-8")

    validation = validate_local_gate_record(record_path, repository)
    assert validation.ok is False
    assert validation.code == "local_gate_code_changed"


def test_documentation_change_keeps_implementation_evidence_valid(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = build_local_gate_record(
        repository,
        database_url="postgresql+psycopg://tester@127.0.0.1:55432/sidera_test",
        checks={"unit": 0},
    )
    _git(repository, "mv", "docs/note.md", "docs/renamed.md")
    _git(repository, "commit", "-qm", "docs")

    validation = validate_local_gate_record(record, repository)
    assert validation.ok is True
    assert validation.code == "local_gate_docs_changed"


def test_written_record_round_trips_with_atomic_writer(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = build_local_gate_record(
        repository,
        database_url="postgresql+psycopg://tester@127.0.0.1:55432/sidera_test",
        checks={"unit": 0},
    )
    destination = tmp_path / "nested" / "gate.json"
    write_local_gate_record(destination, record)
    assert read_local_gate_record(destination) == record
    assert destination.stat().st_mode & 0o077 == 0
    assert implementation_fingerprint(repository) == record.implementation_fingerprint
