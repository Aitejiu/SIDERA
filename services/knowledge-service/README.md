# SIDERA Knowledge Service

The Knowledge Service independently owns durable knowledge pages, immutable revisions,
sources, candidate review state, and its command/event persistence. It is a separate
Python package and PostgreSQL database from the Control Plane; it must not import
`sidera_control_plane`, share SQLAlchemy models, or use cross-database joins.

## Development

From the repository root:

```bash
uv lock --project services/knowledge-service
uv sync --project services/knowledge-service --all-groups
uv run --project services/knowledge-service pytest services/knowledge-service/tests/unit -q
uv run --project services/knowledge-service ruff check services/knowledge-service/src services/knowledge-service/tests
uv run --project services/knowledge-service pyright services/knowledge-service/src services/knowledge-service/tests
```

Apply the independent schema before starting the service:

```bash
export SIDERA_KNOWLEDGE_DATABASE_URL="postgresql+psycopg://sidera@127.0.0.1:55432/sidera_knowledge_test"
uv run --project services/knowledge-service alembic \
  -c services/knowledge-service/alembic.ini upgrade head
uv run --project services/knowledge-service uvicorn \
  sidera_knowledge.api.app:app --host 127.0.0.1 --port 8020
```

The first foundation endpoints are:

```text
GET http://127.0.0.1:8020/health/live
GET http://127.0.0.1:8020/health/ready
```

The service uses an independent local database URL, for example:

```text
postgresql+psycopg://sidera@127.0.0.1:55432/sidera_knowledge_test
```

The database URL is supplied through `SIDERA_KNOWLEDGE_DATABASE_URL`. No production
credentials belong in this repository.

R5.3.0 currently exposes only health and readiness. Knowledge commands, queries, review,
FTS search, and ContextPackage integration are delivered by the following R5.3 stages.
