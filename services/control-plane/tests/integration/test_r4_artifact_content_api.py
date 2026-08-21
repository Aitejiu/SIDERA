from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import update
from test_r4_task_first_api import (
    ARTIFACT_ID,
    BACKEND_EXECUTION_ID,
    ROOT_TASK_ID,
    WORKSPACE_ID,
    _seed,
)

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import ArtifactModel

pytestmark = pytest.mark.integration

CONTENT = b"diff --git a/a.py b/a.py\n"
CONTENT_HASH = sha256(CONTENT).hexdigest()


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=database_url,
        artifact_root=tmp_path / "artifacts",
        artifact_download_token_secret=SecretStr("integration-secret"),
        local_actor_type="local",
        local_actor_id="integration-user",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _seed_content(database: Database, root: Path) -> None:
    _seed(database)
    reference = LocalArtifactContentStore(root).put(
        WORKSPACE_ID,
        BACKEND_EXECUTION_ID,
        CONTENT_HASH,
        CONTENT,
    )
    with database.session() as session:
        session.execute(
            update(ArtifactModel)
            .where(ArtifactModel.id == ARTIFACT_ID)
            .values(
                storage_ref=reference,
                content_hash=CONTENT_HASH,
                size_bytes=len(CONTENT),
            )
        )
        session.commit()


def test_artifact_descriptor_content_range_and_token(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    tmp_path: Path,
) -> None:
    assert workspace_id == WORKSPACE_ID
    _seed_content(database, tmp_path / "artifacts")
    base = f"/api/v1/workspaces/{WORKSPACE_ID}/artifacts/{ARTIFACT_ID}"

    descriptor = client.get(f"{base}/descriptor")
    assert descriptor.status_code == 200, descriptor.text
    assert descriptor.json() == {
        "artifact_id": str(ARTIFACT_ID),
        "workspace_id": str(WORKSPACE_ID),
        "media_type": "text/plain",
        "byte_size": len(CONTENT),
        "content_hash": CONTENT_HASH,
        "download_name": "report.txt",
        "content_available": True,
        "unavailable_reason": None,
    }

    legacy_metadata = client.get(
        "/api/v1/gateway/artifacts",
        params={"workspace_id": str(WORKSPACE_ID), "task_id": str(ROOT_TASK_ID)},
    )
    assert legacy_metadata.status_code == 200
    assert "storage_ref" not in legacy_metadata.text
    assert "artifact-local://" not in legacy_metadata.text

    full = client.get(f"{base}/content")
    assert full.status_code == 200
    assert full.content == CONTENT
    assert full.headers["content-disposition"] == 'inline; filename="report.txt"'
    assert full.headers["x-content-type-options"] == "nosniff"

    ranged = client.get(f"{base}/content", headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206
    assert ranged.content == CONTENT[:10]
    assert ranged.headers["content-range"] == f"bytes 0-9/{len(CONTENT)}"
    assert ranged.headers["accept-ranges"] == "bytes"

    token_response = client.post(f"{base}/download-token")
    assert token_response.status_code == 200
    token = token_response.json()["token"]
    token_read = client.get(f"{base}/content", params={"token": token})
    assert token_read.status_code == 200
    assert token_read.content == CONTENT

    invalid_token = client.get(
        f"{base}/content", params={"token": "v1.invalid.invalid"}
    )
    assert invalid_token.status_code == 404

    wrong_artifact = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/artifacts/{uuid4()}/content",
        params={"token": token},
    )
    assert wrong_artifact.status_code == 404


def test_metadata_only_artifact_is_described_but_not_downloadable(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    assert workspace_id == WORKSPACE_ID
    _seed(database)
    base = f"/api/v1/workspaces/{WORKSPACE_ID}/artifacts/{ARTIFACT_ID}"

    descriptor = client.get(f"{base}/descriptor")
    assert descriptor.status_code == 200
    assert descriptor.json()["content_available"] is False
    assert descriptor.json()["unavailable_reason"] == "provider_did_not_supply_content"

    content = client.get(f"{base}/content")
    assert content.status_code == 404
    assert content.json()["error"]["code"] == "artifact_content_unavailable"

    wrong_workspace = client.get(
        f"/api/v1/workspaces/{uuid4()}/artifacts/{ARTIFACT_ID}/descriptor"
    )
    assert wrong_workspace.status_code == 404


def test_artifact_range_and_integrity_fail_closed(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    tmp_path: Path,
) -> None:
    assert workspace_id == WORKSPACE_ID
    _seed_content(database, tmp_path / "artifacts")
    base = f"/api/v1/workspaces/{WORKSPACE_ID}/artifacts/{ARTIFACT_ID}"

    invalid_range = client.get(
        f"{base}/content", headers={"Range": "bytes=999-1000"}
    )
    assert invalid_range.status_code == 416
    assert invalid_range.json()["error"]["code"] == "artifact_range_not_satisfiable"

    path = tmp_path / "artifacts" / str(WORKSPACE_ID) / CONTENT_HASH[:2] / CONTENT_HASH
    path.write_bytes(b"tampered")
    tampered = client.get(f"{base}/content")
    assert tampered.status_code == 409
    assert tampered.json()["error"]["code"] == "artifact_integrity_failed"


def test_download_tokens_report_configuration_failure_without_a_secret(
    database_url: str,
    database: Database,
    workspace_id: UUID,
    tmp_path: Path,
) -> None:
    assert workspace_id == WORKSPACE_ID
    _seed(database)
    settings = Settings(
        database_url=database_url,
        artifact_root=tmp_path / "artifacts",
    )
    with TestClient(create_app(settings)) as no_token_client:
        response = no_token_client.post(
            f"/api/v1/workspaces/{WORKSPACE_ID}/artifacts/{ARTIFACT_ID}/download-token"
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "artifact_download_tokens_disabled"
