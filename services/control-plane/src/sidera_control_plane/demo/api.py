from __future__ import annotations

from fastapi import APIRouter

from .contracts import DemoManifest
from .supervisor import read_run_phase


def create_demo_status_router(manifest: DemoManifest) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/demo/status")
    def get_demo_status() -> dict[str, str]:
        return {
            "mode": manifest.mode,
            "run_id": str(manifest.run_id),
            "workspace_id": str(manifest.workspace_id),
            "scenario": manifest.scenario,
            "phase": read_run_phase(manifest),
        }

    return router
