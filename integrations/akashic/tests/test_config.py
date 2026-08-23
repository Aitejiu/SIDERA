from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from sidera_task_bridge.config import TaskBridgeConfig

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")


def make_config(**overrides: Any) -> TaskBridgeConfig:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "planning_profile_id": PROFILE_ID,
        "planning_profile_version": 1,
    }
    values.update(overrides)
    return TaskBridgeConfig.model_validate(values)


def test_config_accepts_loopback_urls_and_explicit_identity() -> None:
    config = make_config(
        control_plane_url="http://localhost:8000",
        web_base_url="http://[::1]:4174/",
        actor_type="integration",
        actor_id="akashic-task-bridge",
    )

    assert config.workspace_id == WORKSPACE_ID
    assert config.planning_profile_id == PROFILE_ID
    assert str(config.control_plane_url) == "http://localhost:8000/"
    assert str(config.web_base_url) == "http://[::1]:4174/"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.2:8000",
        "http://user:password@127.0.0.1:8000",
        "http://127.0.0.1:8000/#fragment",
        "ftp://127.0.0.1:8000",
    ],
)
def test_config_rejects_non_loopback_or_unsafe_control_plane_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        make_config(control_plane_url=url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:4174/chat", "http://localhost:4174?tab=chat"])
def test_config_restricts_web_base_url_to_origin(url: str) -> None:
    with pytest.raises(ValidationError):
        make_config(web_base_url=url)


@pytest.mark.parametrize(
    "field",
    ["actor_type", "actor_id"],
)
def test_config_rejects_empty_actor_identity(field: str) -> None:
    with pytest.raises(ValidationError):
        make_config(**{field: "   "})


def test_config_rejects_extra_fields_and_invalid_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        make_config(secret="must not become a config field")
    with pytest.raises(ValidationError):
        make_config(connect_timeout_seconds=0)
    with pytest.raises(ValidationError):
        make_config(request_timeout_seconds=121)
