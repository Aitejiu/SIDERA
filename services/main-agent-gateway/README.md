# SIDERA Main Agent Gateway

The Main Agent Gateway is a loopback service between SIDERA Web and the
configured main-agent provider. It exposes provider-neutral thread and turn
operations under `/main-agent/v1` and keeps provider credentials server-side.

## Local configuration

Control Plane-only use does not require starting this Gateway; see the
[Control Plane README](../control-plane/README.md). The base Gateway project
install contains no optional adapter SDKs. If running the Gateway without an
adapter, select the explicit unconfigured mode and provide its required
workspace ID:

```bash
SIDERA_MAIN_AGENT_ADAPTER_KIND=unconfigured \
SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID=20000000-0000-0000-0000-000000000002 \
  uv run --project services/main-agent-gateway uvicorn \
  sidera_main_agent_gateway.api.app:create_app --factory \
  --host 127.0.0.1 --port 8010
```

The complete product install includes both optional adapters. Stack startup,
release checks, and capability verification select them explicitly:

```bash
uv run --project services/main-agent-gateway --extra akashic --extra demo \
  uvicorn sidera_main_agent_gateway.api.app:create_app --factory \
  --host 127.0.0.1 --port 8010
```

The extras make the Akashic and demo adapters available; they do not configure
provider credentials or change the selected adapter profile.

Akashic adapter settings use the `SIDERA_MAIN_AGENT_` environment prefix:

```text
SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT=/tmp/akashic/control.sock
SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID=20000000-0000-0000-0000-000000000002
SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN=optional-local-token
```

The service defaults to `127.0.0.1:8010`. The Web application should call the
same-origin `/main-agent/v1` paths through the local development proxy. The
browser must not receive the provider endpoint or workspace token.

Run the complete product locally with:

```bash
uv run --project services/main-agent-gateway --extra akashic --extra demo uvicorn \
  sidera_main_agent_gateway.api.app:create_app --factory \
  --host 127.0.0.1 --port 8010
```

## Real vertical gate

The machine-executed Web-to-Task gate is opt-in. It uses only the Gateway HTTP
API/SSE and Control Plane public query endpoints; it does not import the Task
Bridge, command handlers, or persistence repositories. Without the opt-in it
is expected to collect as two explicit skips, not as real-loop evidence.

```text
SIDERA_RUN_REAL_MAIN_AGENT=1
SIDERA_MAIN_AGENT_GATEWAY_URL=http://127.0.0.1:8010
SIDERA_CONTROL_PLANE_URL=http://127.0.0.1:8000
SIDERA_WORKSPACE_ID=<test-workspace-uuid>
SIDERA_PLANNING_PROFILE_ID=<test-profile-uuid>
SIDERA_PLANNING_PROFILE_VERSION=1
SIDERA_REAL_MAIN_AGENT_PROVIDER=<provider-model-label>
SIDERA_REAL_GATE_HELPER=/absolute/path/to/identity-checked-gate-helper
```

Run the collection gate with:

```bash
uv run --project services/main-agent-gateway --extra akashic --extra demo pytest \
  services/main-agent-gateway/tests/integration/test_real_vertical_loop.py -q
```

The helper used by the stale-confirm/offline and Web restart tests must accept
only the fixed actions `pause-control-plane`, `resume-control-plane`, and
`restart-gateway`. It must operate on a PID whose runtime identity belongs to
the current SIDERA checkout, use only exact identity-checked process control,
and leave PostgreSQL, Akashic, plugin-data, and task data untouched. The test
captures helper output and does not print credentials or
transcripts. Real resources are marked with a unique test marker and remain in
the configured workspace for post-run inspection.

See the [integration and API guide](../../docs/api/README.md) for the separate
Control Plane and Knowledge endpoints. The provider-free demo does not start
this Gateway or configure a real main-agent provider.
