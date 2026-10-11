import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("consumer", ["worker", "planner", "runtime"])
def test_fresh_public_composition_does_not_import_native_transports(
    consumer: str,
) -> None:
    script = r"""
import json
import sys
import tempfile
from pathlib import Path

from sidera_control_plane.config import Settings

class NoPublicCalls:
    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            raise AssertionError("construction invoked public agents: " + name)
        return forbidden

settings = Settings(database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused")
consumer = sys.argv[1]
if consumer == "worker":
    from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
    from sidera_control_plane.composition.worker_adapters import build_worker_adapter_factories
    with tempfile.TemporaryDirectory(prefix="sidera-n16-import-") as directory:
        factories = build_worker_adapter_factories(settings, workspace_manager=GitWorktreeWorkspaceManager(Path(directory)))
        assert {"opencode", "dsh-acp", "local_verification"} <= factories.keys()
elif consumer == "planner":
    from sidera_control_plane.composition.planner_adapters import build_planner_adapter_builders
    assert "opencode-acp-planner" in build_planner_adapter_builders(settings)
else:
    from fastapi.testclient import TestClient
    from sidera_control_plane.composition.native_runtime import create_control_plane_runtime
    public = NoPublicCalls()
    runtime = create_control_plane_runtime(settings, public, public, public, public)
    with TestClient(runtime.app) as client:
        assert client.get("/healthz").json() == {"status": "ok", "service": "sidera-control-plane"}
        assert client.get("/api/tasks").status_code in {401, 403}
    runtime.close()
    runtime.close()

forbidden = (
    "sidera_product", "sidera_main_agent_gateway", "sidera_task_bridge", "akashic",
    "sidera_control_plane.adapters.opencode_acp", "sidera_control_plane.adapters.opencode_planner",
    "sidera_control_plane.adapters.dsh_acp", "sidera_control_plane.adapters.acp_process",
    "sidera_agents.postgres", "sidera_agents.harnesses", "sidera_agents.native",
)
loaded = sorted(name for name in sys.modules if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden))
assert not loaded, loaded
origins = {name: str(module.__file__) for name, module in sys.modules.items() if name.startswith("sidera_control_plane") and getattr(module, "__file__", None)}
assert origins
print(json.dumps({"consumer": consumer, "forbidden": loaded, "origins": origins}))
"""
    child = subprocess.run(
        [sys.executable, "-I", "-c", script, consumer],
        env={"PATH": os.defpath, "LANG": "C"},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    evidence = json.loads(child.stdout)
    assert evidence["consumer"] == consumer and evidence["forbidden"] == []
    assert evidence["origins"]
