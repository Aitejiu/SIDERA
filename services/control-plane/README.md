# SIDERA Control Plane

The SIDERA control plane is a deterministic FastAPI service for Task commands,
normalized operational queries, immutable Events, transactional Outbox
delivery, modular Workers, Execution leases, scheduling, and Gateway recovery.
PostgreSQL is its only authoritative store. The R3 Gateway launches one
OpenCode ACP child process per Execution through a replaceable Adapter; it does
not own the Main Agent, collaboration topology, user memory, or knowledge
content.

## Prerequisites

- Python 3.12 or newer and `uv`.
- PostgreSQL 16 binaries. The commands below use the user-local installation
  created for this workspace; another PostgreSQL 16 installation is equivalent.
- macOS Homebrew and the pinned OpenCode executable described below.

Install Python dependencies from the repository root:

```bash
uv sync --project services/control-plane --all-groups --frozen
```

## macOS Local Profile

The verified local profile on 2026-08-20 uses OpenCode `1.18.19` from the
`anomalyco/tap` Homebrew tap. Keep the expected version in the environment so
an upgrade cannot silently change the Adapter contract:

```bash
brew tap anomalyco/tap
brew install anomalyco/tap/opencode
brew pin opencode
opencode --version

export SIDERA_OPENCODE_EXECUTABLE="$(command -v opencode)"
export SIDERA_OPENCODE_EXPECTED_VERSION="1.18.19"
export SIDERA_WORKSPACE_ROOT="$PWD/.sidera/workspaces"
mkdir -p "$SIDERA_WORKSPACE_ROOT"
```

OpenCode credentials remain in the OpenCode credential store. SIDERA module
instances record only opaque references such as
`opencode.default-credential`; API keys must not be placed in module JSON,
Events, Outbox payloads, or logs.

Run the explicit executable and ACP preflight before starting a real Gateway:

```bash
uv run --project services/control-plane python \
  -m sidera_control_plane.opencode_preflight
```

The command prints a compact JSON capability report and returns non-zero when
the configured executable, version, Git worktree, ACP `initialize`, or ACP
`session/new` check fails. There is no automatic fake-worker fallback.

## Native PostgreSQL

Set local development paths:

```bash
export SIDERA_PG_BIN="$HOME/.local/share/sidera/postgresql-16.14/bin"
export SIDERA_PG_DATA_DIR="$HOME/.local/share/sidera/postgres-data"
export SIDERA_PG_LOG="$HOME/.local/share/sidera/postgres.log"
```

Initialize the cluster and database once. The `trust` authentication below is
acceptable only for a single-user development cluster that listens on
`127.0.0.1`; do not use it on a shared or externally reachable server.

```bash
if [ ! -d "$SIDERA_PG_DATA_DIR/base" ]; then
  "$SIDERA_PG_BIN/initdb" -D "$SIDERA_PG_DATA_DIR" --auth=trust
fi
"$SIDERA_PG_BIN/pg_ctl" -D "$SIDERA_PG_DATA_DIR" \
  -l "$SIDERA_PG_LOG" -o "-p 55432 -h 127.0.0.1" start
"$SIDERA_PG_BIN/createuser" -h 127.0.0.1 -p 55432 sidera
"$SIDERA_PG_BIN/createdb" -h 127.0.0.1 -p 55432 \
  -O sidera sidera_test
```

The role/database creation commands are also one-time operations and report an
error if the objects already exist. Verify readiness and set the application
DSN:

```bash
"$SIDERA_PG_BIN/pg_isready" -h 127.0.0.1 -p 55432 -d sidera_test
export SIDERA_DATABASE_URL="postgresql+psycopg://sidera@127.0.0.1:55432/sidera_test"
```

Stop the local server without deleting its data:

```bash
"$SIDERA_PG_BIN/pg_ctl" -D "$SIDERA_PG_DATA_DIR" stop
```

## Migrate And Run

Run migrations explicitly before starting the API:

```bash
uv run --project services/control-plane alembic \
  -c services/control-plane/alembic.ini upgrade head
uv run --project services/control-plane uvicorn \
  sidera_control_plane.api.app:create_app \
  --factory --reload --host 127.0.0.1 --port 8000
```

Check the service at `http://127.0.0.1:8000/healthz`. R1 trusts the actor fields
in the internal command envelope. Do not expose this API publicly before the
SIDERA Gateway supplies authentication and actor identity.

Dispatch one due Outbox batch:

```bash
uv run --project services/control-plane python \
  -m sidera_control_plane.outbox_worker --once
```

`StdoutEventSink` is a development diagnostic, not a production transport or a
delivery contract. External delivery is at least once.

Run the Scheduler, Reconciler, and R3 Gateway as independent native processes:

```bash
uv run --project services/control-plane python \
  -m sidera_control_plane.scheduler_worker --once
uv run --project services/control-plane python \
  -m sidera_control_plane.reconciler_worker --once
uv run --project services/control-plane python \
  -m sidera_control_plane.gateway_worker --once
```

Omit `--once` for continuous PostgreSQL polling. The five processes are
independent and can be stopped or restarted separately: API, Outbox worker,
Scheduler, Reconciler, and Gateway. `Ctrl-C` exits cleanly; all durable
ownership, attempt, retry, Session, input, Artifact, and audit facts remain in
PostgreSQL and are reloaded after a restart. Each batch writes one compact JSON
summary to stdout; failures return non-zero and do not report a successful
batch.

## R2 Timing And Limits

Configure these settings with the `SIDERA_` environment prefix:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `SIDERA_ASSIGNMENT_LEASE_SECONDS` | `30` | Initial and renewed Lease TTL |
| `SIDERA_WORKER_HEARTBEAT_TIMEOUT_SECONDS` | `45` | Stale Worker cutoff |
| `SIDERA_SCHEDULER_BATCH_SIZE` | `20` | Executions examined per Scheduler batch |
| `SIDERA_SCHEDULER_POLL_INTERVAL_SECONDS` | `1.0` | Continuous Scheduler delay |
| `SIDERA_RECONCILER_BATCH_SIZE` | `50` | Workers/Leases examined per Reconciler batch |
| `SIDERA_RECONCILER_POLL_INTERVAL_SECONDS` | `2.0` | Continuous Reconciler delay |

Keep these timing relationships explicit when deploying:

```text
worker heartbeat interval < worker heartbeat timeout
worker heartbeat timeout <= assignment Lease TTL only when rapid exclusion is desired
Reconciler polling interval << assignment Lease TTL
```

R2 Workers are virtual resources and the Scheduler only grants a Lease. The R3
Gateway owns real Adapter capability negotiation, isolated workspaces,
OpenCode process identity, Session events, input delivery, Artifact
registration, exact stop, and continuation recovery. OpenCode reports
unsupported capabilities as disabled in the public contract; the current
profile is experimental and does not claim durable ACP resume or external
idempotency.

## R3 Public Queries

The normalized Gateway projections are available under `/api/v1/gateway`:

- `/executions/{execution_id}/binding`
- `/executions/{execution_id}/workspace`
- `/sessions`, `/events`, `/artifacts`, `/evidence`, and `/inputs`

Every list query requires an explicit `workspace_id`; cursors are opaque to
callers and event history also supports `after_sequence`. The API returns
stable SIDERA projections instead of raw ACP messages.

## Verification

Run these commands from the repository root with PostgreSQL running and
`SIDERA_DATABASE_URL` exported:

```bash
uv run --project services/control-plane pytest \
  services/control-plane/tests/unit -q
uv run --project services/control-plane pytest -m integration \
  services/control-plane/tests/integration -q
uv run --project services/control-plane ruff check \
  services/control-plane/src services/control-plane/tests \
  services/control-plane/migrations
uv run --project services/control-plane pyright \
  --project services/control-plane/pyproject.toml
uv run --project services/control-plane alembic \
  -c services/control-plane/alembic.ini check
```

Run the real OpenCode E2E only after the preflight succeeds and only when the
explicit switch is set:

```bash
SIDERA_RUN_OPENCODE_E2E=1 \
uv run --project services/control-plane pytest \
  services/control-plane/tests/integration/test_opencode_end_to_end.py -q
```

Akashic, Web intervention UI, Main Agent, DSH integration, memory, and
knowledge remain outside this service. Container packaging is deferred until
the deployment topology is selected.
