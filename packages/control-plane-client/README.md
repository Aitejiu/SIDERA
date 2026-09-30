# SIDERA Control Plane Client

`sidera-control-plane-client` is a neutral asynchronous Python client for the
SIDERA Control Plane public HTTP API. It can be used with the Control Plane
without the Main Agent Gateway or any main-agent profile. Its configuration
requires only the Control Plane URL, workspace ID, and a local actor identity;
it does not require a model, provider key, or `SIDERA_MAIN_AGENT_*` settings.

```python
import asyncio
from uuid import UUID

from sidera_control_plane_client import (
    ActorRef,
    ControlPlaneClient,
    ControlPlaneClientConfig,
)


async def main() -> None:
    config = ControlPlaneClientConfig(
        control_plane_url="http://127.0.0.1:8000",
        workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
        actor=ActorRef(type="local", id="my-local-tool"),
    )
    client = ControlPlaneClient(config)
    try:
        summary = await client.get_notification_summary()
        print(summary.model_dump(mode="json"))
    finally:
        await client.close()


asyncio.run(main())
```

The actor is request attribution for the local public API contract, not remote
authentication. The client does not authenticate remote users or make an
otherwise public deployment safe to expose. The default URL policy accepts
loopback origins only; remote use requires a separately secured deployment.
