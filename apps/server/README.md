# SIDERA Native Product Server

`sidera-product==0.1.0` composes the public agents and control-plane owners in
one process. It does not use Akashic, Docker or the legacy main-agent gateway.

## Explicit Configuration

Product settings use `SIDERA_PRODUCT_`. Required selections are
`PRODUCT_DATABASE_URL`, `AGENT_DATABASE_URL`, `CP_DATABASE_URL`,
`CREDENTIAL_DATABASE_URL`, `DEPLOYMENT_NAMESPACE` (stable UUID), `WORKSPACE_ID`
(UUID), `LOCAL_PRINCIPAL_ID`, `WORKSPACE_ROOT` and `ARTIFACT_ROOT` (absolute paths).
Each target must be an explicit `postgresql+psycopg` URL with a database, user
and host or Unix socket. There is no inference from legacy `SIDERA_*` targets.
The targets need not share a server. Each schema remains owned by its component.

Defaults: loopback `127.0.0.1:18080`, one worker, no reload, Web origin
`http://127.0.0.1:5173`, close grace 30 seconds, migration timeout 60 seconds.
`RESULT_INPUTS_ENABLED` and `RESULT_INTEGRATION_ENABLED` both default to `true`;
explicit `false` is preserved. Repository selection comes from CP's saved
workspace setup, not a startup directory or acceptance manifest.
Integer and boolean environment values use JSON scalars. Credentials may use
only explicitly selected `SELECTED_SECRETS` values or a managed store enabled
with `MANAGED_CREDENTIALS_ENABLED=true`, scoped to the stable deployment namespace.
No constructor, health or preflight resolves a secret or invokes a native CLI.

## Commands

```sh
uv sync
uv run sidera migrate
uv run sidera preflight --json
uv run sidera serve --host 127.0.0.1 --port 18080
uv run sidera open
```

Migration is explicit and forward-only. Serve and preflight check schema heads
and owned required tables without migration or example data. Credential migration
uses the installed model-connections Alembic resources in a bounded child with
only the selected target; raw driver output is not relayed.

After migration, `sidera session bootstrap --output <absolute-private-file>`
creates a one-use local bootstrap without starting native owners. The output
must be outside repositories, in an owned 0700 directory; the exclusive file is
0600. Omitting `--output` creates a private temporary directory. Only its safe
path and expiry are printed. The default bootstrap lifetime is 300 seconds.

With the product server running, `sidera workspace configure --workspace <UUID>
--repository <absolute-path> --planner-profile '<ProfileRef JSON>'
--verification-checks '<CheckSpecV1 array>' --bootstrap-file <absolute-private-file>`
exchanges that bootstrap with the live server and uses only public CP setup.
It does not start another owner. `ProfileRef` has exactly `workspace_id`,
`profile_id` and positive `revision`; checks use CP's existing `CheckSpecV1`
codec. Omitting profile/checks can retain saved setup, while a new setup needs
explicit pins/policy. Missing or consumed bootstrap authority is denied.

## Local And Task-Tool Authority

`POST /product/v1/session/bootstrap` atomically consumes one bounded token and
issues host-only SameSite=Strict session and CSRF cookies. The session cookie is
HttpOnly. Cookie-authenticated writes, including existing `/api/v1/commands`,
require the exact configured Origin and `X-Sidera-CSRF` matching the current
session. Loopback alone is not authentication. `GET /product/v1/session` reads
safe session metadata; authenticated `POST /product/v1/session/logout` revokes
the session. Default session/tool grant lifetimes are 43200/86400 seconds.

The official MCP v1.x endpoint is exactly
`/product/v1/task-tools/mcp`. Its bearer belongs to one accepted original Run,
current assignment and response, not a Profile name, UUID or client identifier.
Tools may be enumerated during accepted STARTING; calls require RUNNING with the
current response. The same Profile can receive different Run-scoped roles:

- Main: `sidera_list_profiles`, `sidera_submit_goal`, `sidera_get_planning`,
  `sidera_get_task`, `sidera_list_tasks`, `sidera_request_revision`.
- Worker: `sidera_get_task`, `sidera_report_progress`, restricted to its own work.
- Planner: `sidera_get_planning` and `sidera_get_task` when its related root exists.

No Agent tool confirms a plan, accepts a result, edits setup or invokes raw Run
writers. Direct CP routes enforce the same trusted role and target policy.
Mutation tools require a logical `request_id` UUID. A saved intent retains its
original command, Run, response and selected Planner; changed duplicate content
conflicts. Lost replies reconcile the original command only. UNKNOWN is not
permission to resend. New intents may select a current authorized Planner head;
old Run/setup/request pins are not rebound.

Worker progress is bounded arbitrary self-report data with `verified=false`,
visible in the Worker workspace and activity. It does not complete execution,
advance native sequence or accept a result. Stored tokens are digests only.
Original retained bearers can still be verified against durable metadata while
their current Run authority is valid. Raw bearer reprojection after cache loss
returns `tool_connection_unavailable`; it never rotates a live bearer or starts
another Run. Bootstrap UI, persistent ordinary chat and directory/default route
work remain N20, N19 and N20/N21 respectively.

## Ownership And HTTP

Construction is inert. Startup checks schemas, starts the sole agents owner,
verifies its actual owner lock and starts CP with a registered internal system
identity. That identity cannot authenticate HTTP, configure a repository or
perform human confirmation. Anonymous protected routes remain denied. Main
conversation (N19) and catalog/configuration route implementations are not
provided by this delivery.

`/healthz` is inert liveness; `/readyz` checks component readiness;
`/product/v1/status` reports safe availability. CP is mounted last at `/`, so
existing `/api/v1` contracts retain their paths. In-process public CP clients
use ASGI transport, not blocking same-port HTTP. Knowledge retains its independent
public service/API; unavailable required sources fail the corresponding work.

Shutdown fences product writes and drains CP while agents remains alive. Only
confirmed CP close and agents shutdown permit disposal of the owned stores.
Pending/UNKNOWN cleanup is reported incomplete and may be retried through the
same owners; no foreign Run enumeration or process kill is performed.

## Focused Verification

```sh
uv run pytest tests/unit -q
uv run ruff check .
uv run ruff format --check .
uv run pyright --pythonpath .venv/bin/python
```

Strict typing of the current 19 product source files reports 55 public-foundation
boundary diagnostics: 54 missing type-marker sites and the agents factory's
unannotated clock parameter, with no product-owned diagnostics. The accepted N17
nine-file baseline remains 31 (30 missing markers plus that clock parameter);
new imports expose additional marker sites. This is not a globally clean type
result. Installed typing metadata remains an N22/N24 follow-up; this delivery
does not suppress or modify foundation debt.

Integration requires all 19 explicit `SIDERA_PRODUCT_TEST_*` selectors: shared
allocation ID/socket/port and each of PRODUCT/AGENTS/CP/CREDENTIALS' database URL,
expected database/owner/marker. Unconfigured integration is NOT_RUN; partial or
unsafe selection fails. One fixture validates all four targets and holds their
guards before any owned migration or cleanup. Unit fixtures use non-live local
placeholders and never consume the private allocation.
