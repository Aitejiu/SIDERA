# SIDERA Native Product Server

`sidera-product==0.1.0` composes the public agents and control-plane owners in
one process. It does not use Akashic, Docker or the legacy main-agent gateway.

For end-user installation, Web startup and first project setup, use the
[native product guide](../../docs/deployment/initial-demo.md#安装新版). Six local
packages install as wheels; Web still requires a source checkout. Legacy ACP
entrypoints remain separate, with their migration deferred.

## Explicit Configuration

Product settings use `SIDERA_PRODUCT_`. Required selections are
`PRODUCT_DATABASE_URL`, `AGENT_DATABASE_URL`, `CP_DATABASE_URL`,
`CREDENTIAL_DATABASE_URL`, `DEPLOYMENT_NAMESPACE` (stable UUID), `WORKSPACE_ID`
(UUID), `LOCAL_PRINCIPAL_ID`, `WORKSPACE_ROOT` and `ARTIFACT_ROOT` (absolute paths).
Each target must be an explicit `postgresql+psycopg` URL with a database, user
and host or Unix socket. There is no inference from legacy `SIDERA_*` targets.
The targets need not share a server. Each schema remains owned by its component.
Settings come from the invoking process, not automatically from a project `.env`.
Keep identities, databases and roots stable across restarts; do not seed/reset
an existing workspace to recover it.

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

After wheel installation, invoke `sidera` directly. For source development,
run the following from `apps/server` with the same explicit environment in each
terminal:

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

`sidera open` opens the configured Web origin's workspace tasks route, not the
backend. Start Vite separately with `SIDERA_WEB_BACKEND=native` and the explicit
`SIDERA_WEB_PRODUCT_URL`; browser origin and product settings must agree.

After migration, `sidera session bootstrap --output <absolute-private-file>`
creates a one-use local bootstrap without starting native owners. The output
must be outside repositories, in an owned 0700 directory; the exclusive file is
0600. Omitting `--output` creates a private temporary directory. Only its safe
path and expiry are printed. The default bootstrap lifetime is 300 seconds.
In the Web agents page, use 本地会话 and paste the file's raw `token` into
CLI 令牌, not the path or JSON object. Browser exchange consumes the token;
workspace CLI setup needs a separately issued unused bootstrap file.

With the product server running, `sidera workspace configure --workspace <UUID>
--repository <absolute-path> --planner-profile '<ProfileRef JSON>'
--verification-checks '<CheckSpecV1 array>' --bootstrap-file <absolute-private-file>`
exchanges that bootstrap with the live server and uses only public CP setup.
It does not start another owner. `ProfileRef` has exactly `workspace_id`,
`profile_id` and positive `revision`; checks use CP's existing `CheckSpecV1`
codec. Omitting profile/checks can retain saved setup, while a new setup needs
explicit pins/policy. Missing or consumed bootstrap authority is denied.
First setup uses `--expected-version 0`; updates use the saved workspace version.
See the complete [CheckSpecV1 setup example](../../docs/deployment/initial-demo.md#配置项目与验证).

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
another Run. Web exposes local-session connection, Runtime registration, Profile
creation/settings/capabilities/work history and default appointments. The same
Profile may serve Main, Planner and Worker; saved defaults do not rebind old Runs.

Managed credential UI is opt-in: install `sidera-model-connections[macos]` on a
supported macOS host and enable `MANAGED_CREDENTIALS_ENABLED`. Otherwise explicit
selected server-side secret sources and registered credential metadata are
required. Profile `secret_refs` bind authorized references; native Runs never
inherit global CLI auth. Knowledge remains independently configured.

## Persistent Native Chat

`/main-agent/v1` serves the existing thread/turn/events wire from product-owned
conversations and the public agents Run engine. Empty threads pin an explicit
authorized Profile revision or the configured Main default without starting a
Run. First input starts one conversation Run; later replies use that same open
Run and original grant. Opaque operation IDs retain their exact intent through
duplicates, lost replies and explicit non-admitted BUSY retries. UNKNOWN is
query-only and keeps admission blocked until actual terminal evidence exists.

History requires the current local human session and live original structural
authority. Revoked or unavailable original authority cannot expose cached native
messages or trusted task receipts. Cards are verified only from saved original
Run/response receipts and actual CP outcomes, never native tool preview text.
Response interruption remains disabled because the current public port exposes
no authoritative live cancellation negotiation. Conversation Stop/query at
shutdown is separate and must confirm original cleanup before owner disposal.

The Web's default/`legacy` Vite proxy selection is unchanged. Native selection
requires `SIDERA_WEB_BACKEND=native` and an explicit loopback root
`SIDERA_WEB_PRODUCT_URL=http://127.0.0.1:<port>`. Configure the product Web origin
to the actual browser origin. Native `/api`, `/healthz`, `/main-agent`, `/product`
and `/agents` share that product target; Knowledge remains independently routed.
Cookie-authenticated JSON writes share the exact CSRF helper. No browser bearer
or Provider settings are added.

## Ownership And HTTP

Construction is inert. Startup checks schemas, starts the sole agents owner,
verifies its actual owner lock and starts CP with a registered internal system
identity. That identity cannot authenticate HTTP, configure a repository or
perform human confirmation. Anonymous protected routes remain denied.
Catalog/configuration route delivery remains separate from persistent chat.

`/healthz` is inert liveness; `/readyz` checks component readiness;
`/product/v1/status` reports safe availability. CP is mounted last at `/`, so
existing `/api/v1` contracts retain their paths. In-process public CP clients
use ASGI transport, not blocking same-port HTTP. Knowledge retains its independent
public service/API; unavailable required sources fail the corresponding work.

Shutdown fences product writes and drains CP while agents remains alive. Then
chat reconciles its own original conversation Stops before agents shutdown.
Only confirmed component close permits disposal of the owned stores.
Pending/UNKNOWN cleanup is reported incomplete and may be retried through the
same owners; no foreign Run enumeration or process kill is performed.

## Focused Verification

```sh
uv run pytest tests/unit -q
uv run ruff check .
uv run ruff format --check .
uv run pyright --pythonpath .venv/bin/python
```

Public packages ship typing metadata for installed consumers. The isolated
native-product installation checker verifies the six-wheel composition,
resource provenance and inert boundaries without calling a Provider. Installed
consumer typing checks are separate from whole-foundation strict checking;
their success does not establish globally clean Control Plane typing.
Developer checks above are not a claim that every historical real adapter or
acceptance scenario passes.

Integration requires all 19 explicit `SIDERA_PRODUCT_TEST_*` selectors: shared
allocation ID/socket/port and each of PRODUCT/AGENTS/CP/CREDENTIALS' database URL,
expected database/owner/marker. Unconfigured integration is NOT_RUN; partial or
unsafe selection fails. One fixture validates all four targets and holds their
guards before any owned migration or cleanup. Unit fixtures use non-live local
placeholders and never consume the private allocation.
