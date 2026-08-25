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

The service uses an independent local database URL, for example:

```text
postgresql+psycopg://sidera@127.0.0.1:55432/sidera_knowledge_test
```

The database URL is supplied through `SIDERA_KNOWLEDGE_DATABASE_URL` once the service
configuration is implemented. No production credentials belong in this repository.
