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

`sidera workspace configure --workspace <UUID> --repository <absolute-path>
--planner-profile '<ProfileRef JSON>' --verification-checks '<CheckSpecV1 array>'`
uses only public CP setup. `ProfileRef` has exactly `workspace_id`, `profile_id`
and positive `revision`. Verification checks are validated by CP's existing
`CheckSpecV1` codec. Omitting a profile/checks can retain saved setup; a new
setup requires explicit pins/policy. The command cannot establish authority:
before N18 provides a trusted local session it fails `local_session_required`.

## Ownership And HTTP

Construction is inert. Startup checks schemas, starts the sole agents owner,
verifies its actual owner lock and starts CP with a registered internal system
identity. That identity cannot authenticate HTTP, configure a repository or
perform human confirmation. Anonymous protected routes remain denied. Browser
and tool authority (N18), main conversation (N19), and catalog/configuration
route implementations are not provided by this delivery.

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

Strict product typing reports 31 public-foundation boundary diagnostics: the
previous 30 (29 missing type markers and the agents factory's unannotated clock
parameter), plus the missing marker for the actual Planner manifest import.
This is not a globally clean type result. Installed typing metadata remains an
N22/N24 follow-up; this delivery does not suppress or modify foundation debt.

Integration requires all 19 explicit `SIDERA_PRODUCT_TEST_*` selectors: shared
allocation ID/socket/port and each of PRODUCT/AGENTS/CP/CREDENTIALS' database URL,
expected database/owner/marker. Unconfigured integration is NOT_RUN; partial or
unsafe selection fails. One fixture validates all four targets and holds their
guards before any owned migration or cleanup. Unit fixtures use non-live local
placeholders and never consume the private allocation.
