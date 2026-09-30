from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.basic_acceptance.sample_case import prepare_sample_repository

ROOT = Path(__file__).parents[4]
CHECKER = ROOT / "scripts" / "check-closure-sample.py"


def _write_outputs(repository: Path) -> None:
    output = repository / "out"
    output.mkdir(exist_ok=True)
    (output / "source-a.json").write_text(
        '{"marker":"SIDERA_CLOSURE_SOURCE_A_V1","items":[{"id":"api","status":"done"},{"id":"ui","status":"queued"}]}',
        encoding="utf-8",
    )
    (output / "source-b.json").write_text(
        '{"marker":"SIDERA_CLOSURE_SOURCE_B_V1","items":[{"id":"tests","status":"running"},{"id":"docs","status":"blocked"}]}',
        encoding="utf-8",
    )
    (output / "summary.json").write_text(
        '{"total":4,"counts":{"done":1,"queued":1,"running":1,"blocked":1},"source_markers":["SIDERA_CLOSURE_SOURCE_A_V1","SIDERA_CLOSURE_SOURCE_B_V1"]}',
        encoding="utf-8",
    )


def _check(repository: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--repository", str(repository)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_seed_contains_only_readme_and_checker_accepts_exact_outputs(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "sample"
    prepare_sample_repository(repository)
    assert list(repository.rglob("*")) == [repository / ".git", repository / "README.md"] or (
        repository / "README.md"
    ).exists()
    assert not (repository / "out").exists()

    _write_outputs(repository)
    result = _check(repository)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["sample_version"] == "closure-sample-v1"
    assert payload["status"] == "PASS"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda output: output.joinpath("source-a.json").write_text(
            '{"marker":"wrong","items":[{"id":"api","status":"done"},{"id":"ui","status":"queued"}]}',
            encoding="utf-8",
        ),
        lambda output: output.joinpath("summary.json").write_text(
            '{"total":5,"counts":{"done":1,"queued":1,"running":1,"blocked":1},"source_markers":["SIDERA_CLOSURE_SOURCE_A_V1","SIDERA_CLOSURE_SOURCE_B_V1"]}',
            encoding="utf-8",
        ),
        lambda output: output.joinpath("source-b.json").write_text(
            '{"marker":"SIDERA_CLOSURE_SOURCE_B_V1","items":[{"id":"tests","status":true},{"id":"docs","status":"blocked"}]}',
            encoding="utf-8",
        ),
        lambda output: output.joinpath("source-a.json").write_text(
            '{"marker":"SIDERA_CLOSURE_SOURCE_A_V1","marker":"duplicate","items":[]}',
            encoding="utf-8",
        ),
    ],
)
def test_checker_rejects_tampered_outputs(tmp_path: Path, mutation: object) -> None:
    repository = tmp_path / "sample"
    prepare_sample_repository(repository)
    _write_outputs(repository)
    assert callable(mutation)
    mutation(repository / "out")  # type: ignore[operator]

    assert _check(repository).returncode == 1


def test_checker_rejects_missing_and_symlink_outputs(tmp_path: Path) -> None:
    repository = tmp_path / "sample"
    prepare_sample_repository(repository)
    _write_outputs(repository)
    (repository / "out" / "summary.json").unlink()
    assert _check(repository).returncode == 1

    _write_outputs(repository)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (repository / "out" / "source-a.json").unlink()
    (repository / "out" / "source-a.json").symlink_to(outside)
    assert _check(repository).returncode == 2
