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
uv run --project services/main-agent-gateway pytest \
  services/main-agent-gateway/tests/integration/test_real_vertical_loop.py -q
```

The helper used by the stale-confirm/offline test must accept only the fixed
actions `pause-control-plane` and `resume-control-plane`. It must operate on a
PID whose runtime identity belongs to the current SIDERA checkout, never use
`pkill`/`killall`, and leave PostgreSQL, Akashic, plugin-data, and task data
untouched. The test captures helper output and does not print credentials or
transcripts. Real resources are marked with a unique test marker and remain in
the configured workspace for post-run inspection.
