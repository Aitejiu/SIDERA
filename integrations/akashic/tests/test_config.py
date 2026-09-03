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


def test_config_accepts_loopback_urls_and_explicit_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN", "knowledge-token-for-tests"
    )
    config = make_config(
        control_plane_url="http://localhost:8000",
        web_base_url="http://[::1]:4174/",
        knowledge_base_url="http://127.0.0.1:8020",
        knowledge_timeout_seconds=5,
        actor_type="integration",
        actor_id="akashic-task-bridge",
    )

    assert config.workspace_id == WORKSPACE_ID
    assert config.planning_profile_id == PROFILE_ID
    assert str(config.control_plane_url) == "http://localhost:8000/"
    assert str(config.web_base_url) == "http://[::1]:4174/"
    assert str(config.knowledge_base_url) == "http://127.0.0.1:8020/"
    assert config.knowledge_timeout_seconds == 5
    assert config.knowledge_main_agent_token is not None
    assert (
        config.knowledge_main_agent_token.get_secret_value()
        == "knowledge-token-for-tests"
    )


def test_config_masks_knowledge_token_from_repr_and_serialized_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "knowledge-token-must-not-leak"
    monkeypatch.setenv("SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN", token)
    config = make_config()

    assert token not in repr(config)
    assert token not in str(config.model_dump())
    assert token not in str(config.model_dump(mode="json"))


def test_config_rejects_token_in_plugin_config() -> None:
    with pytest.raises(ValidationError, match="provided through the environment"):
        make_config(knowledge_main_agent_token="must-not-be-in-toml")


def test_config_loads_knowledge_defaults_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIDERA_KNOWLEDGE_BASE_URL", "http://127.0.0.1:8021")
    monkeypatch.setenv("SIDERA_KNOWLEDGE_TIMEOUT_SECONDS", "6")

    config = make_config()

    assert str(config.knowledge_base_url) == "http://127.0.0.1:8021/"
    assert config.knowledge_timeout_seconds == 6


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.2:8020",
        "http://user:password@127.0.0.1:8020",
        "http://127.0.0.1:8020/?token=secret",
        "http://127.0.0.1:8020/#fragment",
    ],
)
def test_config_rejects_non_loopback_or_unsafe_knowledge_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        make_config(knowledge_base_url=url)


def test_config_allows_remote_knowledge_url_only_with_explicit_opt_in() -> None:
    config = make_config(
        knowledge_base_url="https://knowledge.example.com",
        knowledge_allow_remote=True,
    )

    assert str(config.knowledge_base_url) == "https://knowledge.example.com/"
    assert config.knowledge_allow_remote is True


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


def test_config_requires_an_opt_in_governance_principal_pair() -> None:
    config = make_config(
        governance_actor_type="local",
        governance_actor_id="main-agent",
    )

    assert config.governance_actor_type == "local"
    assert config.governance_actor_id == "main-agent"

    with pytest.raises(ValidationError, match="governance actor"):
        make_config(governance_actor_type="local")
    with pytest.raises(ValidationError, match="governance actor"):
        make_config(governance_actor_id="main-agent")
    with pytest.raises(ValidationError):
        make_config(governance_actor_type="   ", governance_actor_id="main-agent")


def test_config_can_verify_governance_principal_against_control_plane_identity() -> None:
    config = make_config(
        governance_actor_type="local",
        governance_actor_id="main-agent",
    )

    assert config.governance_principal_matches("local", "main-agent") is True
    assert config.governance_principal_matches("local", "other-agent") is False
    assert config.governance_principal_matches("integration", "main-agent") is False


def test_config_rejects_extra_fields_and_invalid_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        make_config(secret="must not become a config field")
    with pytest.raises(ValidationError):
        make_config(connect_timeout_seconds=0)
    with pytest.raises(ValidationError):
        make_config(request_timeout_seconds=121)
    with pytest.raises(ValidationError):
        make_config(knowledge_timeout_seconds=0)
    with pytest.raises(ValidationError):
        make_config(knowledge_timeout_seconds=11)


def test_config_exposes_bounded_notification_retry_and_stream_limits() -> None:
    config = make_config()
    assert config.notification_retry_base_seconds == 0.5
    assert config.notification_retry_max_seconds == 15.0
    assert config.notification_delivery_timeout_seconds == 15.0
    assert config.notification_event_limit_bytes == 262_144

    with pytest.raises(ValidationError):
        make_config(notification_retry_base_seconds=0)
    with pytest.raises(ValidationError):
        make_config(notification_retry_max_seconds=0.4)
    with pytest.raises(ValidationError):
        make_config(notification_delivery_timeout_seconds=121)
    with pytest.raises(ValidationError):
        make_config(notification_event_limit_bytes=4095)
