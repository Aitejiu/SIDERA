from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from tests.e2e import result_integration_seed as seed


def test_cli_requires_provider_free_test_scope(monkeypatch) -> None:
    monkeypatch.setenv(seed.TEST_MODE_ENV, "1")
    monkeypatch.delenv(seed.SCOPE_ENV, raising=False)
    monkeypatch.setenv(
        "SIDERA_DATABASE_URL",
        "postgresql+psycopg://sidera@127.0.0.1:55432/sidera_test",
    )

    with pytest.raises(seed.DriverError, match="test_scope_required"):
        seed._database_url()


def test_cli_rejects_non_loopback_database(monkeypatch) -> None:
    monkeypatch.setenv(seed.TEST_MODE_ENV, "1")
    monkeypatch.setenv(seed.SCOPE_ENV, seed.REQUIRED_SCOPE)
    monkeypatch.setenv(
        "SIDERA_DATABASE_URL",
        "postgresql+psycopg://sidera@db.internal:5432/sidera_test",
    )

    with pytest.raises(seed.DriverError, match="database_must_be_loopback"):
        seed._database_url()


def test_cli_rejects_home_as_test_root(monkeypatch) -> None:
    monkeypatch.setenv(seed.ROOT_ENV, str(Path.home()))

    with pytest.raises(seed.DriverError, match="test_root_too_broad"):
        seed._test_root()


def test_main_errors_are_one_redacted_json_object(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    workspace_id = UUID("20000000-0000-0000-0000-000000000099")
    monkeypatch.setenv(seed.ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(seed.TEST_MODE_ENV, raising=False)

    assert (
        seed.main(
            [
                "--workspace-id",
                str(workspace_id),
                "--mode",
                "observe",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "error",
        "error_code": "test_mode_required",
    }
