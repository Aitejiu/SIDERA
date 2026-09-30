import pytest

from sidera_main_agent_gateway.adapters.demo.store import (
    DemoManifestEndpoints,
    _validate_endpoints,
)


def test_demo_gateway_rejects_an_unregistered_endpoint() -> None:
    endpoints = DemoManifestEndpoints(
        control_plane="http://127.0.0.1:15432",
        knowledge="http://127.0.0.1:15433",
        main_agent="http://127.0.0.1:15434",
        web="http://127.0.0.1:15435",
    )
    with pytest.raises(ValueError):
        _validate_endpoints(
            endpoints.model_copy(update={"web": "http://user:pass@127.0.0.1:15435"})
        )
