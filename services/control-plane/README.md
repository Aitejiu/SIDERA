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

## R4.1 WorkerItem Migration Runbook

Use this order when upgrading an existing PostgreSQL workspace to the R4.1
three-level model:

1. Take a PostgreSQL backup and verify that it can be restored in an isolated
   database. Keep the backup identifier with the deployment record.
2. Stop writers that can publish plans or create Executions, then run the
   additive migration:

   ```bash
   uv run --project services/control-plane alembic \
     -c services/control-plane/alembic.ini upgrade head
   ```

   Revision `0012_r4_1_worker_items` creates `worker_items`,
   `worker_item_placements`, and the nullable `executions.worker_item_id`
   foreign key. It does not rewrite plan payloads or delete legacy data.
3. Run the idempotent v1 backfill once per workspace:

   ```bash
   uv run --project services/control-plane python \
     -m sidera_control_plane.worker_item_backfill \
     --workspace-id <workspace-uuid>
   ```

   The command emits one JSON report per Root Task. Confirm that every report
   has no `error` and that `ambiguous_execution_ids` is empty before allowing
   new R4.1 writes. A non-zero exit means at least one Root Task transaction
   failed; fix that Root Task and rerun the command. Re-running successful
   roots is safe and does not duplicate WorkerItems or placements.
4. Review the reports and query projections. A uniquely matchable historical
   Execution receives `worker_item_id`; ambiguous Executions remain nullable
   and read-only until an operator resolves their ownership. Historical v1
   snapshots remain immutable and are normalized at read time.
5. Start the API and workers again, then verify `/healthz`, the current Task
   projection, and the WorkerItem workspace before reopening plan or Execution
   writers.

Rollback is application-first: stop R4.1 writers, restore the application
version, and keep revision `0012_r4_1_worker_items` and its tables in place.
Do not run `alembic downgrade` while any R4.1 WorkerItem, placement, or
Execution association exists. A schema downgrade is permitted only after a
verified backup and an explicit confirmation that no R4.1 writes remain; it
must be treated as a separate migration exercise because the downgrade drops
the new tables and column. Never use recursive deletion commands against the
repository, PostgreSQL data directory, or database.

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

R4.1 Task First and WorkerItem paths are:

- `GET /api/v1/workspaces/{workspace_id}/task-overviews`
- `GET /api/v1/tasks/{root_task_id}/overview`
- `GET /api/v1/tasks/{root_task_id}/graph`
- `GET /api/v1/tasks/{root_task_id}/subtasks/{subtask_key}`
- `GET /api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace`
- `GET /api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/activity`
- `GET /api/v1/tasks/{root_task_id}/executions/{execution_id}/sessions/{session_id}/trace`
- `GET /api/v1/tasks/{root_task_id}/plan/current`
- `GET /api/v1/tasks/{root_task_id}/plan/generations`
- `GET /api/v1/tasks/{root_task_id}/plan-diff`
- `GET /api/v1/workspaces/{workspace_id}/projection-changes`

User acknowledgements, exact WorkerItem intervention, and exact Execution stop
are submitted through `POST /api/v1/commands`. The command envelope remains
the concurrency boundary; the Web does not write control-plane tables.

## R4 Web And E2E

The standalone Web is in `apps/web`. It reads Task First projections, durable
projection changes, normalized Worker traces and hash-addressed Artifact
descriptors through the Public API. Start it with the instructions in
[`apps/web/README.md`](../../apps/web/README.md).

The deterministic browser seed is guarded by `SIDERA_ALLOW_E2E_SEED=1` and
scoped to the exact workspace name `SIDERA R4 E2E Workspace`:

```bash
export SIDERA_DATABASE_URL="postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_test"
export SIDERA_ALLOW_E2E_SEED=1
python tests/e2e/seed_r4_workspace.py --scenario task-first
npm --prefix ../../apps/web run test:e2e
```

The scale scenario verifies 200 Subtasks, 400 Subtask dependency edges, 40
WorkerItems with 80 same-Subtask graph edges, 2,000 WorkerItem activities and
50,000 reverse-paginated trace events. The fixture is isolated by its explicit
workspace name and stable fixture IDs; it never truncates shared tables. The
real browser gate uses the fixed OpenCode integration workspace
and cleans it after the test:

```bash
SIDERA_RUN_OPENCODE_E2E=1 \
SIDERA_OPENCODE_EXECUTABLE="$(command -v opencode)" \
SIDERA_OPENCODE_EXPECTED_VERSION="1.18.19" \
SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
npm --prefix ../../apps/web run test:e2e -- \
  --project=real-opencode tests/e2e/real-opencode.spec.ts
```

`SIDERA_ARTIFACT_ROOT` controls local Artifact content storage and
`SIDERA_PROJECTION_CHANGE_RETENTION_SECONDS` controls replay retention. Keep
`SIDERA_WEB_ORIGIN` exact and loopback-only for the Local Identity Profile.
The API does not fall back to a fake Worker when OpenCode preflight fails.

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
