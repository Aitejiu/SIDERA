from fastapi import FastAPI

from sidera_control_plane.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the control-plane HTTP application."""
    app = FastAPI(title="SIDERA Control Plane", version="0.1.0")
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "sidera-control-plane"}

    return app

