from pathlib import Path

import pytest
from pydantic import ValidationError

from sidera_control_plane.config import Settings


def test_result_integration_is_disabled_by_default() -> None:
    settings = Settings(database_url="postgresql+psycopg://sidera@127.0.0.1/test")

    assert settings.result_integration_enabled is False
    assert settings.result_apply_on_accept is False
    assert settings.result_store_root == Path(".sidera/results")


def test_result_integration_config_is_strict_and_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://sidera@127.0.0.1/test",
            result_readiness_timeout_seconds=0,
        )

    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://sidera@127.0.0.1/test",
            result_store_root="",
        )
