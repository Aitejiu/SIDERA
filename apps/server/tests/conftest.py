from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.engine import URL, Engine


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings_values(tmp_path: Path) -> dict[str, object]:
    def inert_url(component: str) -> str:
        return URL.create(
            "postgresql+psycopg",
            username=f"unit_{component}",
            database=f"sidera_unit_{component}_inert",
            query={"host": str(tmp_path / "missing-db-socket"), "port": "5432"},
        ).render_as_string(hide_password=False)

    return {
        "product_database_url": inert_url("product"),
        "agent_database_url": inert_url("agents"),
        "cp_database_url": inert_url("cp"),
        "credential_database_url": inert_url("credentials"),
        "deployment_namespace": UUID("898937f8-0a89-51ea-bfee-563b8524f4ad"),
        "workspace_id": UUID("b9e469c3-ad4a-5bcd-a2b9-75f9c45e93fc"),
        "local_principal_id": "n17-test-user",
        "workspace_root": tmp_path / "workspaces",
        "artifact_root": tmp_path / "artifacts",
    }


@pytest.fixture
def no_database_connections(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("Inert product construction/close attempted a DB connection")

    monkeypatch.setattr(Engine, "connect", reject)
