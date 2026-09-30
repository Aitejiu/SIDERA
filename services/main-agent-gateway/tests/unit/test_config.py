from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from sidera_main_agent_gateway.config import Settings

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "akashic_endpoint": "127.0.0.1:9010",
        "sidera_workspace_id": WORKSPACE_ID,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_settings_use_loopback_defaults_and_constrained_runtime_limits() -> None:
    settings = make_settings()

    assert settings.adapter_kind == "akashic"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8010
    assert str(settings.web_origin) == "http://127.0.0.1:4174/"
    assert settings.max_message_bytes == 2 * 1024 * 1024
    assert settings.relay_event_limit == 1024
    assert settings.recovered_turn_poll_seconds == 1.0


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "*"])
def test_settings_reject_wildcard_bind_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        make_settings(host=host)


@pytest.mark.parametrize(
    "endpoint",
    [
        "0.0.0.0:9010",
        "192.168.1.20:9010",
        "akashic.example.com:9010",
        "relative/control.sock",
        ":9010",
        "127.0.0.1:0",
        "127.0.0.1:65536",
    ],
)
def test_settings_reject_non_loopback_or_invalid_akashic_endpoints(
    endpoint: str,
) -> None:
    with pytest.raises(ValidationError, match="Akashic endpoint"):
        make_settings(akashic_endpoint=endpoint)


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "http://user:password@127.0.0.1:4174",
        "http://127.0.0.1:4174/path",
        "http://127.0.0.1:4174?debug=1",
        "http://127.0.0.1:4174#fragment",
    ],
)
def test_settings_reject_non_origin_web_urls(origin: str) -> None:
    with pytest.raises(ValidationError, match="web_origin"):
        make_settings(web_origin=origin)


def test_settings_accept_absolute_socket_and_loopback_tcp_endpoints() -> None:
    assert make_settings(akashic_endpoint="127.0.0.1:9010").akashic_endpoint == (
        "127.0.0.1:9010"
    )
    assert make_settings(akashic_endpoint="localhost:9010").akashic_endpoint == (
        "localhost:9010"
    )
    assert make_settings(akashic_endpoint="/tmp/akashic/control.sock").akashic_endpoint == (
        "/tmp/akashic/control.sock"
    )


def test_demo_adapter_requires_a_manifest_without_akashic_configuration() -> None:
    with pytest.raises(ValidationError, match="demo_manifest"):
        Settings(adapter_kind="interview-demo", sidera_workspace_id=WORKSPACE_ID)

    settings = Settings(
        adapter_kind="interview-demo",
        demo_manifest=Path("/tmp/interview-demo/manifest.json"),
        sidera_workspace_id=WORKSPACE_ID,
    )

    assert settings.akashic_endpoint is None


def test_unconfigured_adapter_does_not_require_provider_configuration() -> None:
    settings = Settings(
        adapter_kind="unconfigured",
        sidera_workspace_id=WORKSPACE_ID,
    )

    assert settings.adapter_kind == "unconfigured"
    assert settings.akashic_endpoint is None
    assert settings.demo_manifest is None


def test_default_akashic_adapter_still_requires_its_endpoint() -> None:
    with pytest.raises(ValidationError, match="akashic_endpoint"):
        Settings(sidera_workspace_id=WORKSPACE_ID)


def test_settings_keep_workspace_token_secret() -> None:
    settings = make_settings(akashic_workspace_token="workspace-secret")

    assert isinstance(settings.akashic_workspace_token, SecretStr)
    assert settings.akashic_workspace_token.get_secret_value() == "workspace-secret"
    assert "workspace-secret" not in repr(settings)
    assert "workspace-secret" not in repr(settings.model_dump())
