# SIDERA Web

SIDERA Web is the standalone Task First control room. It consumes the
versioned SIDERA Public API; it does not import a Worker adapter or read
PostgreSQL directly.

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
npm --prefix apps/web install
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

Open <http://127.0.0.1:4174/>. The Vite proxy forwards `/api` to the API.
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

## Real OpenCode Gate

Run the real provider only after the pinned executable passes preflight:

```bash
export SIDERA_OPENCODE_EXECUTABLE="$(command -v opencode)"
export SIDERA_OPENCODE_EXPECTED_VERSION="1.18.19"
uv run --project services/control-plane python \
  -m sidera_control_plane.opencode_preflight

SIDERA_RUN_OPENCODE_E2E=1 \
SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
SIDERA_OPENCODE_EXECUTABLE="$SIDERA_OPENCODE_EXECUTABLE" \
SIDERA_OPENCODE_EXPECTED_VERSION="$SIDERA_OPENCODE_EXPECTED_VERSION" \
npm --prefix apps/web run test:e2e -- \
  --project=real-opencode tests/e2e/real-opencode.spec.ts
```

The real browser case runs the R3 integration test, opens the generated Task,
follows the real Execution/Session trace, reads the hash-addressed Artifact
descriptor and cleans the dedicated `Integration Test Workspace` afterwards.
It does not enable a fake Worker fallback.

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
  executable path/version and the PostgreSQL DSN. A skipped provider test is
  not R4 real-operation evidence.

R4 delivers the standalone complete mode. A future DSH Plugin may embed the
same Public API projections and deep links, but it must not access this
database or create a second state source. DSH Worker support, Main Agent and
Planner proposals, strong TaskRelation gates, remote identity and remote
Artifact storage remain later extension points.
