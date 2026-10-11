# SIDERA Control Plane

The SIDERA control plane is a deterministic FastAPI service for Task commands,
normalized operational queries, immutable Events, transactional Outbox
delivery, modular Workers, Execution leases, scheduling, and Gateway recovery.
PostgreSQL is its only authoritative store. Native composition consumes public
Agents execution/catalog/observation ports to manage task dependencies,
concurrency, leases, intervention and results. It does not maintain another
Harness engine or select Provider credentials and models.

For the native product or a separate provider-free preview, follow the
[quick start](../../docs/deployment/initial-demo.md).
Public HTTP contracts are documented in the [API guide](../../docs/api/README.md).
This component README covers Control Plane-specific preflight, migrations,
planning profile payloads, WorkContext backfill, ContextPackage assembly, and
operator commands.

## Native Public Composition

An embedding host supplies the public `sidera_agents` execution, catalog,
observations, runtime inventory, source routing and trusted request authority.
The Control Plane does not construct an agents engine, probe a Harness, read
credentials, select a model or start a Run during construction.

```python
from sidera_control_plane.composition.native_runtime import (
    create_control_plane_runtime,
)

runtime = create_control_plane_runtime(
    settings,
    agent_execution,
    agent_catalog,
    agent_observations,
    source_router,
    request_authorizer,
    agent_runtimes=agent_runtimes,
)
```

Use an explicit owned PostgreSQL URL and owned execution/result/artifact paths in
`Settings`. Enable `result_inputs_enabled` and `result_integration_enabled` for
the checked-result workflow. Install the Control Plane with its declared local
`sidera-agents` and `sidera-runtime-guard` dependencies. Existing migrations and
their configuration are packaged in the wheel; no repository checkout or
editable import is required by the runtime.

`runtime.readiness()` checks the expected schema without changing it. Explicit
`runtime.migrate()` upgrades only its configured database; serving and workspace
setup do not migrate or seed an unknown target. `/healthz` is public and inert.
Protected reads and writes require `request_authorizer.authenticate_request`,
`identity_for_grant` and action/target `require` checks. Command body actor labels
are not authentication. Human confirmation additionally requires the configured
local user identity and exact proposal version/hash.

Register the stable `runtime.context_source_reader` and
`runtime.execution_authorizer` with the host's default-deny routers before
starting background work. These readers authorize saved CP Worker/Planner grants;
the incoming host source router is not a fallback for CP-owned context. Shared
agents services remain owned by the host.

```python
runtime.migrate()
assert runtime.readiness()
# Bind the CP public readers to the host routers here.
runtime.start_background(background_identity=verified_system_identity)
```

The background identity is an explicit host-verified `ControlPlaneIdentity` with
actor type `system`, the configured local principal and its original host grant.
It is separate from HTTP user identity. Startup composes the existing scheduler,
activation, context, collaboration, transition, result, lease recovery,
notification and Gateway/Planner consumers under the existing single CP owner.
No separate application task loop or stdout Outbox consumer is needed.

### Workspace Setup

`runtime.configure_workspace(request, grant_id=verified_user_grant)` accepts
`WorkspaceSetupRequest` from `sidera_control_plane.composition.native_runtime`:

```python
request = WorkspaceSetupRequest(
    workspace_id,
    explicit_repository_directory,
    0,  # Create; use the saved version for subsequent compare-and-swap updates.
    planning_budget,
    selected_planner_profile_ref,
    verification_checks=explicit_check_specs,
)
receipt = runtime.configure_workspace(request, grant_id=verified_user_grant)
```

The repository must be an authorized absolute local Git root with an existing
HEAD, not a symlink escape. First setup requires an explicit enabled, compatible
Planner `ProfileRef` and nonempty existing `CheckSpecV1` checks. No Profile, model,
verification command, task or repository content is invented. Setup stores
configuration only; it does not run checks or modify the user's Git repository.

The receipt contains the workspace version, an opaque repository reference and
`PlanningPolicyRef(policy_id, version)`, not an agents Profile ID. Submit that
policy and its pinned Planner through ordinary `SubmitTaskGoal`, then approve
the exact proposal through the public human command boundary. Identical setup is
idempotent. Changed setup requires the current expected version and retains old
queued module/policy pins. On update, omitted Planner/check inputs retain existing
pins; an explicit empty check tuple cannot disable verification.

### Shutdown

`runtime.close(grace_seconds=30.0)` returns a boolean. It fences new admissions
and mutations, retains authorized reads while draining, and observes/stops only
the original saved CP Worker/Planner operations and owned local verification
processes. Protected requests and their
dependency cleanup are included in the drain. A timeout or unresolved original
operation returns `False` and retains owned resources; keep the host agents
background service running and repeat close. `True` confirms CP consumers/stores
are closed. Repeated completed close is idempotent. CP never closes the externally
supplied agents service or an unrelated conversation.

## Legacy Standalone Consumers

The standalone `create_app`/worker CLI material below describes retained legacy
consumers, not the authenticated native composition entrypoint. The old R3
Gateway starts OpenCode ACP per Execution. Its transport and Planner still have
callers; they have not been removed or converted to thin shims. Their migration
is separate and is not required to use the native product. Never run a second
scheduler/Gateway over the native product's store.

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

The verified local profile on 2026-08-23 uses OpenCode `1.18.19` from the
`anomalyco/tap` Homebrew tap. Keep the expected version in the environment so
an upgrade cannot silently change the Adapter contract:

```bash
brew tap anomalyco/tap
brew install anomalyco/tap/opencode
brew pin opencode
opencode --version

export SIDERA_OPENCODE_EXECUTABLE="$(command -v opencode)"
export SIDERA_OPENCODE_EXPECTED_VERSION="1.18.19"
export SIDERA_OPENCODE_MODEL="<authorized-provider>/<model-id>"
export SIDERA_WORKSPACE_ROOT="$PWD/.sidera/workspaces"
mkdir -p "$SIDERA_WORKSPACE_ROOT"
```

In this legacy path, credentials remain in the OpenCode credential store. SIDERA module
instances record only opaque references such as
`opencode.default-credential`; API keys must not be placed in module JSON,
Events, Outbox payloads, or logs.

This is not native authentication: native Runs isolate HOME/XDG and use authorized
Profile secret references, never global-login inheritance. See
[Agents prerequisites](../../packages/agents/README.md#opencode-prerequisites-and-limits).

Run the explicit executable and ACP preflight before starting a real Gateway:

```bash
uv run --project services/control-plane python \
  -m sidera_control_plane.opencode_preflight
```

The command prints a compact JSON capability report and returns non-zero when
the configured executable, version, Git worktree, ACP `initialize`, or ACP
`session/new` check fails. There is no automatic fake-worker fallback.

Run the Planner-specific preflight before enabling a PlanningProfile:

```bash
uv run --project services/control-plane python \
  -m sidera_control_plane.planner_preflight
```

This additionally validates the Planner ACP contract and Proposal schema
round-trip. Planner and Worker preflight are independent; a passing Worker
probe does not make a Planner profile available.

## Native PostgreSQL

Set local development paths:

```bash
export SIDERA_PG_BIN="$(brew --prefix postgresql@16)/bin"
export SIDERA_PG_DATA_DIR="$HOME/.local/share/sidera/postgres-data"
export SIDERA_PG_LOG="$HOME/.local/share/sidera/postgres.log"
mkdir -p "$HOME/.local/share/sidera"
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

## R5.3.3 Context Package Runtime

R5.3.3 makes WorkContext and exact Knowledge references a durable
`ContextPackage`. The Control Plane stores the immutable manifest, hashes,
consumer identity, budget, expiry, acknowledgement, and audit metadata. It
does not store Knowledge section bodies in the package projection, Events,
Outbox payloads, or logs.

### Context Worker

Run the Context Package Worker as a separate process after both databases have
been migrated. It claims short-lived assembly leases, closes the database unit
of work, calls the Knowledge Service, and opens a new unit of work only to
persist the result:

```bash
export SIDERA_DATABASE_URL="postgresql+psycopg://sidera@127.0.0.1:55432/sidera_test"
export SIDERA_KNOWLEDGE_BASE_URL="http://127.0.0.1:8020"
export SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN="<local-secret>"
export SIDERA_CONTEXT_ASSEMBLY_BATCH_SIZE=10
export SIDERA_CONTEXT_ASSEMBLY_LEASE_SECONDS=60
export SIDERA_CONTEXT_ASSEMBLY_MAX_ATTEMPTS=5

uv run --project services/control-plane python \
  -m sidera_control_plane.context_package_worker \
  --owner-id context-worker-local \
  --poll-seconds 1 \
  --batch-size 10
```

Use `--once` for a smoke run. The worker prints counts and safe error codes
only. Never put the Knowledge token, Markdown body, or resolved secret value in
module configuration, command payloads, Events, or logs. The Knowledge Service
uses its own `SIDERA_KNOWLEDGE_DATABASE_URL` and must be migrated independently.

The HTTP call is deliberately outside a Control Plane database transaction.
This prevents a slow or unavailable Knowledge Service from holding row locks.
The lease expiry allows another worker to reclaim work after a process restart;
unique `(purpose, consumer_id)` and package hashes prevent duplicate packages.
Temporary failures enter bounded retry and eventually block the assembly;
permanent reference, permission, workspace, or hash failures block it without
falling back to an empty context.

### Cutover And Recovery

Set `SIDERA_CONTEXT_PACKAGE_CUTOVER_AT` to an explicit timezone-aware instant
when enabling the R5.3.3 path. Consumers created before the cutover retain the
frozen legacy execution shape. New planning and execution consumers require a
ready assembly, an active non-expired package, and an adapter capability that
declares ContextPackage schema version `1`; missing or unsupported context is a
stable scheduling/planning block, never an implicit legacy or empty-context
fallback.

Retry keeps the original request, fingerprint, consumer, and absolute expiry.
Once the package has expired, create a new consumer instead of extending the
old authorization. Revoke is owner-only and records an attention/event; it does
not claim to erase information from a running Worker or automatically kill its
process. `new_starts` blocks new consumer starts while an owner audit remains
permitted; `all_materialization` also blocks owner body materialization.

The read API exposes the manifest and audit metadata without body content. The
owner materialization endpoint returns the typed body only in memory and sends
`Cache-Control: no-store`. Use the package and submission query routes for
operator diagnosis rather than reading PostgreSQL directly.

## R5.1 Planning Control Loop

The Planning control loop is a separate control-plane path. A restricted local
operator registers a Planner ModuleInstance and a versioned PlanningProfile;
the Web or a future Main Agent submits a Brief through the Public API. The
Planner only returns a candidate Proposal. It cannot create a Task or start an
Execution directly.

The registration commands are not user-form commands and must remain behind the
local operator identity. The payloads below are the important provider-specific
parts of the standard `POST /api/v1/commands` envelope; use fresh command,
correlation, and idempotency UUIDs for each request:

```json
{
  "command_type": "RegisterPlannerModule",
  "target_type": "workspace",
  "payload": {
    "provider": "opencode-acp-planner",
    "interface_version": "sidera.planner.v1",
    "config_schema_version": 1,
    "capabilities": {
      "structured_output": "json",
      "clarification": true,
      "idempotency": "none",
      "cancellation": "process",
      "usage_reporting": "tokens",
      "max_context_bytes": 128000,
      "stability": "experimental",
      "interface_version": "sidera.planner.v1",
      "proposal_schema_versions": [2]
    },
    "instance_name": "opencode-r5-planner",
    "config_namespace": "planning",
    "config": {
      "planning.executable": "opencode",
      "planning.model": "deepseek/deepseek-v4-flash"
    },
    "secret_refs": []
  }
}
```

Use the returned `module_instance_id` in a second restricted
`RegisterPlanningProfile` command. The profile must pin
`planner_module_config_version`, `planner_interface_version`, Proposal schema
`2`, budget, retry policy, and `user_selectable`. Submit a task only after the
profile query returns the expected active version.

For a real local gate, use a model and timeout that are explicit and independent
for Planner and Worker:

```bash
export SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=90
export SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS=90
export SIDERA_OPENCODE_STOP_GRACE_SECONDS=5
uv run --project services/control-plane python \
  -m sidera_control_plane.opencode_preflight
```

`opencode_preflight` is the Worker preflight command. It checks the executable,
pinned version, Git worktree support, ACP `initialize`, and `session/new`; it
does not run a user task. Planner runs use an isolated
`.sidera/planning/run-*` directory and remove it in `finally`. If a process is
interrupted, stop the worker first and remove only confirmed stale `run-*`
directories under the configured planning temp root. Never print resolved secret
values or put them in Module config; use opaque `env:NAME` references only.

The current R5.1 public command set has no generic profile-lifecycle mutation
endpoint. A disabled profile is rejected by the domain state machine, but an
operator must not edit PostgreSQL directly. Until an admin lifecycle command is
added, stop new submissions by removing the profile from the upstream selector,
stop the planning worker, and register a replacement profile when changing
configuration. Existing PlanningRequest snapshots remain immutable.

## R5.1.1 Runtime Binding And Deployment

R5.1.1 is implemented in the PostgreSQL control-plane profile. `Worker` stores
health, capacity, heartbeat, and workload capabilities; the independent
`WorkerRuntimeConfiguration` stores the exact Adapter and Workspace Provider
ModuleInstance IDs plus their config versions. A missing or invalid runtime
configuration is visible to operators and keeps new executions queued.

The Scheduler resolves and locks that configuration in the same transaction as
the Lease, Worker reservation, Execution assignment, immutable
`ExecutionBinding`, SchedulingBlock episode, Event, and Outbox record. The
Gateway then loads only those exact module IDs and versions. Reconfiguring a
Worker affects future Bindings; existing leased/running executions remain on
their immutable Binding.

Runtime inspection endpoints are:

- `GET /api/v1/workers?workspace_id=<uuid>` and
  `GET /api/v1/workers/<worker-id>?workspace_id=<uuid>`;
- `GET /api/v1/modules?workspace_id=<uuid>&module_type=worker_adapter` or
  `workspace_provider`;
- `GET /api/v1/workspaces/<workspace-id>/runtime-metrics`.

### Deployment Order

Use this order for an existing workspace. Do not infer or backfill a runtime
from an adapter type, instance name, or latest active module:

1. Pause the Scheduler assignment writer; leave valid existing Gateway sessions
   running.
2. Back up PostgreSQL and run the additive Alembic migration to head.
3. Deploy the new API and Web.
4. Explicitly configure every existing Worker that should receive new work
   with `ConfigureWorkerRuntime`; inspect the returned version and do not place
   config bodies or secrets in Events or logs.
5. Run the readiness audit:

   ```bash
   uv run --project services/control-plane python \
     -m sidera_control_plane.runtime_readiness \
     --workspace-id "<workspace-uuid>" --json
   ```

   Continue only when the report has no missing/invalid runtime, active
   leased/running Execution without Binding, or module mismatch.
6. Start the Scheduler and observe `SchedulingBlock` changes, runtime metrics,
   and structured assignment/binding logs.

Queued executions can be pinned, changed, or returned to automatic selection.
Leased/running executions cannot be hot-switched in this release; use the
future R5.1.2 reassignment flow so the old trace and Binding remain auditable.

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
     --workspace-id "<workspace-uuid>"
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

Run the Planning Worker, Activation Worker, Scheduler, Reconciler, and R3
Gateway as independent native processes:

```bash
uv run --project services/control-plane python \
  -m sidera_control_plane.planning_worker --once
uv run --project services/control-plane python \
  -m sidera_control_plane.activation_worker --once
uv run --project services/control-plane python \
  -m sidera_control_plane.scheduler_worker --once
uv run --project services/control-plane python \
  -m sidera_control_plane.reconciler_worker --once
uv run --project services/control-plane python \
  -m sidera_control_plane.gateway_worker --once
```

Omit `--once` for continuous PostgreSQL polling. The seven processes are
independent and can be stopped or restarted separately: API, Outbox worker,
Planning Worker, Activation Worker, Scheduler, Reconciler, and Gateway.
Start the API and migration first, then Outbox/Planning/Activation/Scheduler,
and keep Reconciler and Gateway running for recovery and execution. `Ctrl-C` exits cleanly; all durable
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
| `SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS` | `60.0` | Planner ACP request timeout |
| `SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS` | `60.0` | Worker ACP request timeout |
| `SIDERA_OPENCODE_STOP_GRACE_SECONDS` | `5.0` | Worker process stop grace period |

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

## R5.3.2 Legacy WorkContext Backfill

Requests created before durable `WorkContext` creation can be rebuilt from the
immutable planning and task records already in PostgreSQL. The backfill never
calls a Planner, reads chat/network state, or invents missing user content. It
creates one `legacy_backfill` revision per request, skips requests that already
have a WorkContext, and orders batches by planning-request UUID so a reported
cursor can be used to resume a run.

Run a read-only preview first:

```bash
uv run --project services/control-plane python \
  -m sidera_control_plane.work_context_backfill \
  --workspace-id "$SIDERA_WORKSPACE_ID" \
  --batch-size 100 \
  --dry-run
```

Writing requires an explicit confirmation flag:

```bash
uv run --project services/control-plane python \
  -m sidera_control_plane.work_context_backfill \
  --workspace-id "$SIDERA_WORKSPACE_ID" \
  --batch-size 100 \
  --confirm-write
```

The JSON summary contains only counts and the last planning-request UUID. A
request with a Brief but missing Proposal, Materialization, Plan, or root Task
is stored as `legacy_incomplete` with deterministic `missing_fact_codes`; a
request with no Brief row is reported as incomplete and is not written because
the revision schema requires an exact Brief reference. Each batch is its own
transaction, so a corrupt batch is rolled back and reported while later UUID
batches can continue.

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

## R5.1 Fake And Real Gates

Fake Planner scenarios are deterministic fixtures for UI and control-plane
state-machine tests. They do not prove model output, ACP process handling, or
real Worker artifacts. The opt-in real gate uses PostgreSQL, the registered
OpenCode Planner, the actual planning/activation/scheduler code, a Git worktree,
the actual OpenCode Worker ACP session, and persisted Artifact/Evidence
candidate facts. It must be run explicitly:

```bash
SIDERA_RUN_REAL_OPENCODE=1 \
SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
SIDERA_OPENCODE_EXECUTABLE="$SIDERA_OPENCODE_EXECUTABLE" \
SIDERA_OPENCODE_EXPECTED_VERSION="$SIDERA_OPENCODE_EXPECTED_VERSION" \
SIDERA_OPENCODE_MODEL="deepseek/deepseek-v4-flash" \
SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=90 \
SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS=90 \
uv run --project services/control-plane pytest -m integration \
  services/control-plane/tests/integration/test_r5_1_real_planning_loop.py -q -s
```

The gate explicitly configures the Worker runtime and lets the production
Scheduler create the `ExecutionBinding`. It does not insert a Binding in the
planning fixture. The only direct Binding fixtures left are lower-level Gateway
repository/boundary tests; the real planning path still exercises the Gateway,
workspace, ACP process, Session, Artifact, and Evidence path.

## Verification

The following commands exercise the retained standalone/legacy service. Use an
explicitly owned test database, from the repository root with PostgreSQL running and
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

Knowledge remains an independent service and database; this process only stores
exact Knowledge references and ContextPackage manifests, and calls Knowledge
through the bounded HTTP adapter. Main Agent conversation and Personal Memory
remain outside this service. See the [API guide](../../docs/api/README.md) for
integration boundaries and the local-only security profile.
