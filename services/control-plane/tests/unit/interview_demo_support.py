from pathlib import Path
from uuid import UUID, uuid4

from sidera_control_plane.demo.contracts import DemoManifest


def manifest_for(root: Path, *, run_id: UUID | None = None) -> DemoManifest:
    run_id = run_id or uuid4()
    return DemoManifest(
        run_id=run_id,
        workspace_id=uuid4(),
        scenario="mainline",
        run_root=root / "run",
        repository=root / "repository",
        base_commit="a" * 40,
        code_commit="a" * 40,
        database_name=f"sidera_interview_test_{run_id.hex}",
        knowledge_database_name=f"sidera_interview_knowledge_{run_id.hex}",
        endpoints={
            "control_plane": "http://127.0.0.1:18001",
            "knowledge": "http://127.0.0.1:18002",
            "main_agent": "http://127.0.0.1:18003",
            "web": "http://127.0.0.1:18004",
        },
        module_ids={
            key: uuid4()
            for key in (
                "worker_primary",
                "worker_secondary",
                "workspace",
                "planner",
                "collaboration",
                "verification",
                "notification",
            )
        },
        worker_ids={
            key: uuid4()
            for key in (
                "validation",
                "report",
                "review",
                "integration",
                "replacement",
                "verification",
            )
        },
        planning_profile_id=uuid4(),
    )
