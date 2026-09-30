import pytest

from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.validation import endpoint_origin, validate_endpoint


def test_public_endpoint_normalizes_scheme_host_and_default_port() -> None:
    assert (
        validate_endpoint("HTTPS://Example.COM:443/v1/models", "public")
        == "https://example.com/v1/models"
    )


def test_public_endpoint_does_not_accept_embedded_credentials() -> None:
    with pytest.raises(ConfigurationError) as rejected:
        validate_endpoint(
            "https://fake-user:fake-pass@example.test/v1",
            "public",
        )

    assert rejected.value.code == "invalid_configuration"
    assert "fake-pass" not in str(rejected.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.example.test/v1",
        "https://api.example.test/v1?token=x",
        "https://api.example.test/v1?",
        "https://api.example.test/v1#fragment",
        "https://api.example.test/v1#",
        "https://10.2.3.4/v1",
        "https://127.0.0.1/v1",
        "https://169.254.10.20/v1",
        "https://240.0.0.1/v1",
        "https://224.0.0.1/v1",
        "https://0.0.0.0/v1",
        "https://100.64.0.1/v1",
        "https://localhost/v1",
        "https://localhost./v1",
        "https://api.localhost/v1",
        "https://api.example.test../v1",
        "https://api.example.test:/v1",
        "https://api.example.test:0/v1",
        "https://api.example.test:65536/v1",
        "https://api.example.test:notaport/v1",
        "https:///v1",
        "ftp://api.example.test/v1",
        "https://api.example.test/v1\nheader",
    ],
)
def test_public_endpoint_rejects_unsafe_or_malformed_target(endpoint: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_endpoint(endpoint, "public")


def test_public_endpoint_rejects_private_ipv6_literal() -> None:
    with pytest.raises(ConfigurationError):
        validate_endpoint("https://[fd00::1]/v1", "public")


def test_loopback_is_explicit_not_an_intranet_discovery() -> None:
    assert (
        validate_endpoint("http://127.0.0.1:9010/v1", "loopback")
        == "http://127.0.0.1:9010/v1"
    )
    with pytest.raises(ConfigurationError):
        validate_endpoint("http://192.168.1.10:9010/v1", "loopback")


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:9010/v1", "http://127.0.0.1:9010/v1"),
        ("https://[::1]:443/v1", "https://[::1]/v1"),
    ],
)
def test_loopback_accepts_only_explicit_loopback_literals(
    endpoint: str,
    expected: str,
) -> None:
    assert validate_endpoint(endpoint, "loopback") == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.2:9010/v1",
        "http://10.0.0.1:9010/v1",
        "http://localhost:9010/v1",
        "http://model-host:9010/v1",
        "https://[::2]:9010/v1",
    ],
)
def test_loopback_rejects_lan_dns_and_non_loopback_addresses(endpoint: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_endpoint(endpoint, "loopback")


def test_endpoint_origin_uses_normalized_effective_port_and_ignores_path() -> None:
    assert endpoint_origin("HTTPS://Example.COM:443/v1/chat") == "https://example.com"
    assert endpoint_origin("http://127.0.0.1:9010/v1") == "http://127.0.0.1:9010"


def test_endpoint_origin_keeps_non_default_port() -> None:
    assert endpoint_origin("https://api.example.test:8443/v1") == (
        "https://api.example.test:8443"
    )


def test_endpoint_origin_rejects_userinfo_and_query() -> None:
    with pytest.raises(ConfigurationError):
        endpoint_origin("https://user:fake-secret@api.example.test/v1")
    with pytest.raises(ConfigurationError):
        endpoint_origin("https://api.example.test/v1?key=fake-secret")


def test_invalid_network_scope_is_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as rejected:
        validate_endpoint("https://api.example.test/v1", "private")

    assert rejected.value.code == "invalid_configuration"
