# SIDERA Main Agent Gateway

The Main Agent Gateway is a loopback service between SIDERA Web and the
configured main-agent provider. It exposes provider-neutral thread and turn
operations under `/main-agent/v1` and keeps provider credentials server-side.

## Local configuration

Required settings use the `SIDERA_MAIN_AGENT_` environment prefix:

```text
SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT=/tmp/akashic/control.sock
SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID=20000000-0000-0000-0000-000000000002
SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN=optional-local-token
```

The service defaults to `127.0.0.1:8010`. The Web application should call the
same-origin `/main-agent/v1` paths through the local development proxy. The
browser must not receive the provider endpoint or workspace token.

Run locally with:

```bash
uv run --project services/main-agent-gateway uvicorn \
  sidera_main_agent_gateway.api.app:create_app --factory
```
