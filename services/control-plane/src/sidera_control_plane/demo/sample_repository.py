from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from .sample_content import (
    BROKEN_INTEGRATION_SOURCE,
    CONTACTS_CSV,
    CONTACTS_TSV,
    IMPORTER_TESTS,
    INITIAL_IMPORTER,
    INITIAL_REPORT,
    INITIAL_VALIDATION,
    INTEGRATION_SOURCE,
    OWNER_MARKER,
    REPORT_SOURCE,
    REVIEW_ARTIFACT,
    TSV_TESTS,
    VALIDATION_SOURCE,
)

SampleStage = Literal[
    "validation", "report", "review", "integration", "broken", "repaired", "tsv"
]

_OWNER_FILENAME = ".sidera-interview-demo-owner"
_PROJECT_ROOT = Path(__file__).resolve().parents[5]


def create_sample_repository(parent: Path | None = None) -> tuple[Path, str]:
    parent_path = _temporary_parent(parent)
    repository = Path(tempfile.mkdtemp(prefix="sample-repository-", dir=parent_path))
    _reject_project_path(repository)
    (repository / _OWNER_FILENAME).write_text(OWNER_MARKER, encoding="utf-8")
    (repository / ".gitignore").write_text(
        "__pycache__/\n*.py[cod]\n",
        encoding="utf-8",
    )
    (repository / "csv_importer").mkdir()
    (repository / "tests").mkdir()
    (repository / "fixtures").mkdir()
    (repository / "csv_importer" / "__init__.py").write_text("", encoding="utf-8")
    (repository / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repository / "csv_importer" / "validation.py").write_text(
        INITIAL_VALIDATION, encoding="utf-8"
    )
    (repository / "csv_importer" / "report.py").write_text(
        INITIAL_REPORT, encoding="utf-8"
    )
    (repository / "csv_importer" / "importer.py").write_text(
        INITIAL_IMPORTER, encoding="utf-8"
    )
    (repository / "tests" / "test_importer.py").write_text(
        IMPORTER_TESTS, encoding="utf-8"
    )
    (repository / "fixtures" / "contacts.csv").write_text(
        CONTACTS_CSV, encoding="utf-8"
    )
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.email", "interview-demo@sidera.local")
    _run_git(repository, "config", "user.name", "SIDERA Interview Demo")
    _run_git(repository, "add", "--", ".")
    _run_git(repository, "commit", "-q", "-m", "chore: create csv demo skeleton")
    base_commit = _run_git(repository, "rev-parse", "HEAD").stdout.decode().strip()
    return repository, base_commit


def materialize_sample_stage(
    repository: Path,
    stage: SampleStage,
    phone_optional: bool,
) -> tuple[str, ...]:
    del phone_optional
    repository = repository.expanduser().resolve(strict=False)
    _assert_owned(repository)
    stage_files: dict[SampleStage, dict[str, str]] = {
        "validation": {"csv_importer/validation.py": VALIDATION_SOURCE},
        "report": {"csv_importer/report.py": REPORT_SOURCE},
        "review": {"artifacts/review.md": REVIEW_ARTIFACT},
        "integration": {"csv_importer/importer.py": INTEGRATION_SOURCE},
        "broken": {"csv_importer/importer.py": BROKEN_INTEGRATION_SOURCE},
        "repaired": {"csv_importer/importer.py": INTEGRATION_SOURCE},
        "tsv": {
            "fixtures/contacts.tsv": CONTACTS_TSV,
            "tests/test_tsv.py": TSV_TESTS,
        },
    }
    if stage not in stage_files:
        raise ValueError(f"unsupported sample stage: {stage}")
    changed: list[str] = []
    for relative_path, content in stage_files[stage].items():
        target = repository / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")
            changed.append(relative_path)
    if changed:
        _run_git(repository, "add", "--", *changed)
        _run_git(repository, "commit", "-q", "-m", f"demo: materialize {stage} stage")
    return tuple(changed)


def _temporary_parent(parent: Path | None) -> Path:
    if parent is None:
        parent = Path(tempfile.gettempdir())
    parent = parent.expanduser().resolve(strict=False)
    _reject_project_path(parent)
    _reject_user_git_repository(parent)
    if not parent.exists() or not parent.is_dir():
        raise ValueError("sample repository parent must be an existing directory")
    return parent


def _assert_owned(repository: Path) -> None:
    _reject_project_path(repository)
    marker = repository / _OWNER_FILENAME
    if not marker.is_file() or marker.read_text(encoding="utf-8") != OWNER_MARKER:
        raise ValueError("not an owned sample repository")
    if not (repository / ".git").is_dir():
        raise ValueError("owned sample repository must be a Git repository")
    if _run_git(repository, "remote").stdout.strip():
        raise ValueError("sample repository must not have a remote")


def _reject_project_path(path: Path) -> None:
    resolved = path.expanduser().resolve(strict=False)
    project = _PROJECT_ROOT.resolve(strict=False)
    if resolved == project or project in resolved.parents:
        raise ValueError("sample repository must be outside SIDERA")


def _reject_user_git_repository(path: Path) -> None:
    current = path.expanduser().resolve(strict=False)
    while True:
        if (current / ".git").exists():
            raise ValueError("sample repository must be outside user Git repositories")
        if current.parent == current:
            return
        current = current.parent


def _run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = None
    if arguments and arguments[0] == "commit":
        environment = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        }
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=True,
        env=environment,
    )
