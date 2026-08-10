# SIDERA Control Plane

R1 control plane is a deterministic FastAPI service for Task commands, queries,
immutable Events, and transactional Outbox delivery. PostgreSQL is its only
authoritative store. It does not call an LLM and does not contain Worker
selection, collaboration topology, user memory, or knowledge content.

## Prerequisites

- Python 3.12 or newer and `uv`.
- PostgreSQL 16 binaries. The commands below use the user-local installation
  created for this workspace; another PostgreSQL 16 installation is equivalent.

Install Python dependencies from the repository root:

```bash
uv sync --project services/control-plane --all-groups --frozen
```

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

Akashic, OpenCode, Worker/Execution scheduling, Web intervention, memory, and
knowledge integration are intentionally outside R1. Container packaging is
deferred until the deployment topology is selected.
