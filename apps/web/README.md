# SIDERA Web

SIDERA Web is the standalone Task First control room. It consumes the
versioned SIDERA Public API; it does not import a Worker adapter or read
PostgreSQL directly.

For the current local developer preview, start with the
[initial demo guide](../../docs/deployment/initial-demo.md). It includes a
provider-free fixture workspace, all three local server commands, the current
browser checks, and the limits of the demo. The versioned R4/R5 sections below
are component and historical gate references, not a complete current product
release checklist.

## macOS Prerequisites

- macOS with Homebrew
- Node 24 and npm
- Python 3.12 and `uv`
- PostgreSQL 16 listening on `127.0.0.1:55432`
- Chromium installed for Playwright E2E

The control-plane PostgreSQL setup and the pinned OpenCode installation are
documented in [`services/control-plane/README.md`](../../services/control-plane/README.md).

Install dependencies and prepare the database from the repository root:

```bash
uv sync --project services/control-plane --all-groups --frozen
npm --prefix apps/web ci
export SIDERA_DATABASE_URL="postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_test"
uv run --project services/control-plane alembic \
  -c services/control-plane/alembic.ini upgrade head
npx --prefix apps/web playwright install chromium
```

## Local Servers

Start the API on port `8000`:

```bash
SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
SIDERA_WEB_ORIGIN="http://127.0.0.1:4174" \
SIDERA_MAIN_AGENT_ENTRY_URI="http://127.0.0.1:18080/main-agent" \
uv run --project services/control-plane uvicorn \
  sidera_control_plane.api.app:create_app --factory \
  --host 127.0.0.1 --port 8000
```

Start the Web dev server on port `4174`:

```bash
npm --prefix apps/web run dev -- --port 4174
```

Open <http://127.0.0.1:4174/>. The Vite proxy forwards `/api` to the API. The
Task First entry is `/workspaces/<workspace-uuid>/tasks`; use `新建任务` there
to submit a Planning Brief.
Playwright starts an isolated Vite server on `4173` and an API process when
the ports are free, so the E2E command does not require the interactive Web
server to be running.

## Deterministic E2E

The seed is deliberately guarded and only touches the workspace named
`SIDERA R4 E2E Workspace`. It never truncates shared tables.

```bash
export SIDERA_ALLOW_E2E_SEED=1
export SIDERA_DATABASE_URL="postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_test"
npm --prefix apps/web run test:e2e
```

The deterministic scenarios are available directly when debugging:

```bash
SIDERA_ALLOW_E2E_SEED=1 SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
  services/control-plane/.venv/bin/python \
  services/control-plane/tests/e2e/seed_r4_workspace.py --scenario task-first

SIDERA_ALLOW_E2E_SEED=1 SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
  services/control-plane/.venv/bin/python \
  services/control-plane/tests/e2e/seed_r4_workspace.py --scenario scale
```

The scale fixture contains 200 WorkItems, 400 informational edges and 50,000
trace events. Playwright checks the HTTP page size, reverse pagination and a
virtualized trace DOM bounded to 120 rows.

## R5.2 Real Main Agent Web Gate

The real Web gate is opt-in and never replaces the deterministic Chat tests.
It uses the running Control Plane, Main Agent Gateway and Akashic provider
through the browser's same-origin `/main-agent/v1` proxy. The browser receives
no provider endpoint or workspace token, and the test does not install a
Playwright route or mock Chat API responses.

Configure the same local values used by the R5.2 stack, then provide an
identity-checked helper that accepts `restart-gateway` for the Gateway recovery
case:

```bash
export SIDERA_RUN_REAL_MAIN_AGENT=1
export SIDERA_WORKSPACE_ID="<test-workspace-uuid>"
export SIDERA_MAIN_AGENT_GATEWAY_URL="http://127.0.0.1:8010"
export SIDERA_CONTROL_PLANE_URL="http://127.0.0.1:8000"
export SIDERA_REAL_GATE_HELPER="/absolute/path/to/identity-checked-gate-helper"
export SIDERA_REAL_MAIN_AGENT_TIMEOUT_SECONDS=300

npm --prefix apps/web run test:e2e -- \
  --project=desktop-chromium tests/e2e/real-main-agent.spec.ts
```

The helper must operate only on the PID recorded by the current SIDERA
runtime, reject every action except the explicitly documented one, and leave
PostgreSQL, Akashic, plugin-data, Tasks, Artifacts and conversation history
untouched. It must not use `pkill`, `killall`, a broad process match or a shell
command string. The test verifies Chat -> Planning -> Task -> WorkerItem ->
Trace -> Chat, and verifies that a Gateway restart polls the original turn
without automatically sending the composer input again.

Without `SIDERA_RUN_REAL_MAIN_AGENT=1`, this file is expected to collect as two
explicit skips. A skipped test is collection evidence only, not a real
provider or release-gate pass.

## R5.1 Real OpenCode Planning Gate

Run the real provider only after the pinned executable passes preflight:

```bash
export SIDERA_OPENCODE_EXECUTABLE="$(command -v opencode)"
export SIDERA_OPENCODE_EXPECTED_VERSION="1.18.19"
export SIDERA_OPENCODE_MODEL="<authorized-provider>/<model-id>"
uv run --project services/control-plane python \
  -m sidera_control_plane.opencode_preflight

SIDERA_RUN_REAL_OPENCODE=1 \
SIDERA_ALLOW_E2E_SEED=1 \
SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
SIDERA_OPENCODE_EXECUTABLE="$SIDERA_OPENCODE_EXECUTABLE" \
SIDERA_OPENCODE_EXPECTED_VERSION="$SIDERA_OPENCODE_EXPECTED_VERSION" \
SIDERA_OPENCODE_MODEL="$SIDERA_OPENCODE_MODEL" \
SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=90 \
SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS=90 \
npm --prefix apps/web run test:e2e -- \
  --project=desktop-chromium tests/e2e/real-planning.spec.ts
```

The browser case submits a real Brief, runs the real Planner worker, renders the
Proposal architecture, confirms the exact revision through the UI, and opens
the generated Root Task. The backend command in the control-plane runbook goes
further through Activation, Scheduler, Worker Gateway, Session, Artifact and
Evidence. The real gate never enables a fake Provider fallback.

The older R3 Worker-only integration remains available separately:

```bash
SIDERA_RUN_OPENCODE_E2E=1 \
SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
SIDERA_OPENCODE_EXECUTABLE="$SIDERA_OPENCODE_EXECUTABLE" \
SIDERA_OPENCODE_EXPECTED_VERSION="$SIDERA_OPENCODE_EXPECTED_VERSION" \
uv run --project services/control-plane pytest \
  services/control-plane/tests/integration/test_opencode_end_to_end.py -q
```

## Configuration And Troubleshooting

- `SIDERA_ARTIFACT_ROOT` is the local content root for Artifact bytes. The API
  process must have read/write access to it; Artifact URLs expose IDs, never
  local absolute paths.
- `SIDERA_PROJECTION_CHANGE_RETENTION_SECONDS` controls how long SSE cursors
  can replay before the client must perform a canonical HTTP resync.
- `SIDERA_WEB_ORIGIN` must exactly match the Web origin. Keep both services on
  loopback for the Local Identity Profile.
- A `resync_required` state is expected after a cursor gap. Refreshing the
  canonical Task snapshot is the recovery path; writes stay disabled until the
  stream is live again.
- If the real gate is skipped, check `SIDERA_RUN_OPENCODE_E2E=1`, the OpenCode
  or `SIDERA_RUN_REAL_OPENCODE=1` switch, the OpenCode executable/model/version,
  the timeout settings and the PostgreSQL DSN. A skipped provider test is not
  real-operation evidence.

The standalone PostgreSQL mode remains the development mainline. Main Agent
integration, planning proposals, DSH Worker ACP hosting, WorkerItem handoff,
and collaboration now have implementations; their acceptance scope is
recorded separately in the [root README](../../README.md#当前状态).
A future DSH Plugin may embed the same Public API projections and deep links,
but it must not access the database or create a second state source. Remote
identity and remote Artifact storage remain outside this local demo.
