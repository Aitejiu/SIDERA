from uuid import uuid4

import httpx
import pytest
from sidera_agents.contracts import SourceRef
from sidera_agents.errors import AgentError
from sidera_control_plane_client import ControlPlaneRejected

from sidera_product.composition import build_product_runtime
from sidera_product.config import ProductSettings


@pytest.mark.anyio
async def test_owned_public_cp_client_uses_asgi_and_keeps_default_auth_denial(
    settings_values, no_database_connections, monkeypatch
):
    async def network_forbidden(*args, **kwargs):
        pytest.fail("Owned public CP calls must not use loopback HTTP")

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", network_forbidden
    )
    runtime = build_product_runtime(ProductSettings.model_validate(settings_values))
    client = runtime.new_control_plane_client()
    try:
        with pytest.raises(ControlPlaneRejected) as rejected:
            await client.get_task(uuid4())
        assert rejected.value.code == "authentication_required"
        assert runtime.status()["lifecycle"] == "constructed"
    finally:
        await client.close()
        assert runtime.close(grace_seconds=0) is True


@pytest.mark.parametrize("knowledge_url", [None, "http://127.0.0.1:19000"])
def test_optional_knowledge_is_explicitly_unavailable_without_blocking_config(
    settings_values, no_database_connections, knowledge_url
):
    settings = ProductSettings.model_validate(
        settings_values | {"knowledge_base_url": knowledge_url}
    )
    runtime = build_product_runtime(settings)
    try:
        assert runtime.status()["knowledge"] == {
            "available": False,
            "configured": knowledge_url is not None,
        }
        with pytest.raises(AgentError) as unavailable:
            runtime.sources.read(
                uuid4(), SourceRef(provider="knowledge", key="owned", version="1")
            )
        assert unavailable.value.code == "source_unavailable"
        assert runtime.status()["execution"]["ready"] is False
    finally:
        assert runtime.close(grace_seconds=0) is True
