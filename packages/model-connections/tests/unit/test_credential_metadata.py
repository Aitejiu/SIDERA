from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from sidera_model_connections.postgres.catalog import PostgresConnectionCatalog
from sidera_model_connections.postgres.models import CredentialRow


def test_credential_metadata_reader_has_scoped_ordered_nonlocking_sql(monkeypatch):
    statements = []
    workspace_id = UUID(int=1)
    row = CredentialRow(
        workspace_id=workspace_id,
        id=UUID(int=2),
        alias="Managed alias",
        source="macos_keychain",
        status="active",
        env_name=None,
        replaces_id=None,
        version=2,
        created_at=datetime(2026, 10, 5, tzinfo=UTC),
    )

    def scalars(session, statement):
        statements.append(statement)
        return SimpleNamespace(all=lambda: [row])

    monkeypatch.setattr(Session, "scalars", scalars)
    catalog = PostgresConnectionCatalog(sessionmaker())
    metadata = catalog.list_credentials(workspace_id)
    assert len(metadata) == 1 and metadata[0].ref.workspace_id == workspace_id
    compiled = statements[0].compile()
    sql = str(compiled)
    assert compiled.params == {"workspace_id_1": workspace_id}
    assert "WHERE sidera_configuration.credentials.workspace_id =" in sql
    assert (
        "ORDER BY sidera_configuration.credentials.created_at, sidera_configuration.credentials.id"
        in sql
    )
    assert "FOR UPDATE" not in sql
    assert metadata[0].alias == "Managed alias"
    assert metadata[0].status == "active" and metadata[0].version == 2
