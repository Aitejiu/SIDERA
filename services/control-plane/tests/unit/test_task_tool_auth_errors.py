import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidera_control_plane.api.errors import (
    install_error_handlers,
    install_native_http_error_handler,
)
from sidera_control_plane.domain.agent_bindings import AssignmentDenied
from sidera_control_plane.domain.errors import ReplayedDomainError


@pytest.mark.parametrize("native", (False, True))
@pytest.mark.parametrize(
    "code",
    (
        "assignment_forbidden",
        "permission_denied",
        "grant_revoked",
        "version_conflict",
        "validation_error",
    ),
)
def test_native_authority_denial_is_safe_403_and_legacy_conflicts_remain(native, code):
    app = FastAPI()
    install_error_handlers(app)
    if native:
        install_native_http_error_handler(app)

    @app.get("/owned-error")
    def error():
        raise ReplayedDomainError(code, "PRIVATE credential path", {"token": "PRIVATE"})

    with TestClient(app) as client:
        response = client.get("/owned-error")
    denied = code in {"assignment_forbidden", "permission_denied", "grant_revoked"}
    assert response.status_code == (
        403 if native and denied else 422 if code == "validation_error" else 409
    )
    assert response.json()["error"]["code"] == code
    if native and denied:
        assert "PRIVATE" not in response.text
        assert response.json()["error"]["details"] == {}


def test_fresh_native_assignment_denial_is_safe_403():
    app = FastAPI()
    install_error_handlers(app)
    install_native_http_error_handler(app)

    @app.get("/owned-error")
    def error():
        raise AssignmentDenied("PRIVATE caller scope")

    with TestClient(app) as client:
        reply = client.get("/owned-error")
    assert reply.status_code == 403 and "PRIVATE" not in reply.text
