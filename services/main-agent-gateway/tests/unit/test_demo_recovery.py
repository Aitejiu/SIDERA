from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")


def _manifest(tmp_path: Path) -> Path:
    module_keys = (
        "worker_primary",
        "worker_secondary",
        "workspace",
        "planner",
        "collaboration",
        "verification",
        "notification",
    )
    worker_keys = (
        "validation",
        "report",
        "review",
        "integration",
        "replacement",
        "verification",
    )
    run_id = uuid4()
    project_root = Path(__file__).resolve().parents[4]
    run_root = project_root / ".sidera" / "interview-demo" / str(run_id)
    repository = tmp_path / f"sample-repository-{run_id.hex}"
    repository.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "mode": "interview-demo",
        "run_id": str(run_id),
        "workspace_id": str(WORKSPACE_ID),
        "scenario": "recovery",
        "run_root": str(run_root),
        "repository": str(repository),
        "base_commit": "a" * 40,
        "code_commit": "b" * 40,
        "database_name": "sidera_interview_test_" + run_id.hex,
        "knowledge_database_name": "sidera_interview_knowledge_" + run_id.hex,
        "pg_host": "127.0.0.1",
        "pg_port": 55432,
        "pg_user": "sidera",
        "endpoints": {
            "control_plane": "http://127.0.0.1:18000",
            "knowledge": "http://127.0.0.1:18001",
            "main_agent": "http://127.0.0.1:18002",
            "web": "http://127.0.0.1:18003",
        },
        "module_ids": {key: str(uuid4()) for key in module_keys},
        "worker_ids": {key: str(uuid4()) for key in worker_keys},
        "planning_profile_id": str(uuid4()),
        "planning_profile_version": 1,
        "execution_deadline_seconds": 3600,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _settings(manifest: Path):
    from sidera_main_agent_gateway.config import Settings

    return Settings(
        adapter_kind="interview-demo",
        demo_manifest=manifest,
        akashic_endpoint=None,
        sidera_workspace_id=WORKSPACE_ID,
    )


@pytest.mark.asyncio
async def test_gateway_restart_keeps_a_queued_turn_pollable_without_duplicate_messages(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter
    from sidera_main_agent_gateway.adapters.demo.store import DemoKeyValueStore

    manifest = _manifest(tmp_path)
    store_path = tmp_path / "conversation.json"
    adapter = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=object(),
        store=DemoKeyValueStore(store_path),
    )
    thread = await adapter.create_thread(title="Gateway recovery", context_refs=())
    handle = await adapter.start_turn(thread.id, "部署到我的生产服务器")
    await adapter.close()

    restarted = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=object(),
        store=DemoKeyValueStore(store_path),
    )
    recovered = await restarted.read_turn(thread.id, handle.id)
    thread_after_restart = await restarted.read_thread(thread.id)

    assert recovered.status == "queued"
    assert len(thread_after_restart.turns) == 1
    assert len(recovered.items) == 1
    assert recovered.items[0].type == "userMessage"
    await restarted.close()


@pytest.mark.asyncio
async def test_read_only_demo_adapter_does_not_materialize_a_missing_store(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter

    manifest = _manifest(tmp_path)
    settings = _settings(manifest).model_copy(update={"restore_read_only": True})
    run_root = Path(json.loads(manifest.read_text(encoding="utf-8"))["run_root"])
    adapter = DemoMainAgentAdapter(settings, dispatcher=object())

    try:
        page = await adapter.list_threads()
        assert page.threads == ()
        assert not run_root.exists()
    finally:
        await adapter.close()


def test_read_only_gateway_gets_history_and_events_without_writing_local_store(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter
    from sidera_main_agent_gateway.adapters.demo.store import DemoKeyValueStore
    from sidera_main_agent_gateway.api.app import create_app

    manifest = _manifest(tmp_path)
    settings = _settings(manifest).model_copy(update={"restore_read_only": True})
    thread_id = "demo-thread-read-only"
    turn_id = "demo-turn-read-only"
    store_path = tmp_path / "restored" / "store.json"
    store_path.parent.mkdir()
    store_path.write_text(
        json.dumps(
            {
                "demo_main_agent_conversations_v1": {
                    "threads": {
                        thread_id: {
                            "id": thread_id,
                            "title": "恢复历史",
                            "created_at": "2026-09-19T04:00:00+00:00",
                            "updated_at": "2026-09-19T04:00:02+00:00",
                            "context_refs": [],
                            "turns": [
                                {
                                    "id": turn_id,
                                    "thread_id": thread_id,
                                    "status": "completed",
                                    "created_at": "2026-09-19T04:00:01+00:00",
                                    "started_at": "2026-09-19T04:00:01+00:00",
                                    "completed_at": "2026-09-19T04:00:02+00:00",
                                    "final_response": "历史可读",
                                    "items": [],
                                    "usage": None,
                                    "error": None,
                                    "events": [],
                                }
                            ],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    before = store_path.read_bytes()
    entries_before = sorted(item.name for item in store_path.parent.iterdir())
    adapter = DemoMainAgentAdapter(
        settings,
        dispatcher=object(),
        store=DemoKeyValueStore(store_path, read_only=True),
    )

    with TestClient(create_app(settings, adapter=adapter)) as client:
        responses = (
            client.get("/main-agent/v1/health"),
            client.get("/main-agent/v1/capabilities"),
            client.get("/main-agent/v1/threads"),
            client.get(f"/main-agent/v1/threads/{thread_id}"),
            client.get(f"/main-agent/v1/threads/{thread_id}/turns/{turn_id}"),
            client.get(f"/main-agent/v1/threads/{thread_id}/turns/{turn_id}/events"),
        )

    assert all(response.status_code < 500 for response in responses)
    assert responses[2].json()["data"][0]["id"] == thread_id
    assert responses[3].json()["id"] == thread_id
    assert responses[4].json()["id"] == turn_id
    assert responses[5].status_code == 409
    assert store_path.read_bytes() == before
    assert sorted(item.name for item in store_path.parent.iterdir()) == entries_before
