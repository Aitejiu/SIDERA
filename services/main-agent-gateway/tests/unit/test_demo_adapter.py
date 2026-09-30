from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sidera_task_bridge.client import (
    CommandEnvelope,
    CommandExecution,
    CommandIntent,
    CommandResponse,
    ControlPlaneClient,
    DispatchResult,
)

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
        "scenario": "mainline",
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


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, CommandEnvelope]] = []

    def new_envelope(self, intent: CommandIntent) -> CommandEnvelope:
        command_id = uuid4()
        return CommandEnvelope(
            command_id=command_id,
            command_type=intent.command_type,
            target_type=intent.target_type,
            target_id=intent.target_id,
            expected_version=intent.expected_version,
            idempotency_key=f"demo:{command_id}",
            correlation_id=command_id,
            payload=intent.payload,
        )

    async def execute(
        self, resource_lock_key: str, command: CommandEnvelope
    ) -> DispatchResult:
        self.calls.append((resource_lock_key, command))
        request_id = uuid4()
        return DispatchResult(
            status="applied",
            command_id=command.command_id,
            execution=CommandExecution(
                status="applied",
                command_id=command.command_id,
                result=CommandResponse(
                    command_id=command.command_id,
                    aggregate_id=request_id,
                    aggregate_version=1,
                    event_ids=(uuid4(),),
                    data={"planning_request_id": str(request_id)},
                ),
            ),
        )


class BlockingDispatcher(FakeDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self, resource_lock_key: str, command: CommandEnvelope
    ) -> DispatchResult:
        self.started.set()
        await self.release.wait()
        return await super().execute(resource_lock_key, command)


class FakeRevisionClient:
    def __init__(self, root_task_id: UUID) -> None:
        self.root_task_id = root_task_id
        self.planning_request_id: UUID | None = None

    async def get_materialization(self, request_id: UUID) -> dict[str, object]:
        if request_id != self.planning_request_id:
            return {}
        return {"root_task_id": str(self.root_task_id)}

    async def get_task(self, task_id: UUID) -> dict[str, object]:
        assert task_id == self.root_task_id
        return {"id": str(task_id), "version": 7}

    async def get_active_plan(self, task_id: UUID) -> dict[str, object]:
        assert task_id == self.root_task_id
        return {
            "root_task_id": str(task_id),
            "active_snapshot_id": str(UUID("30000000-0000-0000-0000-000000000001")),
            "active_generation": 1,
            "active_snapshot_hash": "c" * 64,
        }


@pytest.mark.asyncio
async def test_demo_adapter_submits_revision_against_current_active_plan(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter
    from sidera_main_agent_gateway.adapters.demo.store import (
        DemoKeyValueStore,
        load_demo_manifest,
    )

    manifest = _manifest(tmp_path)
    demo_manifest = load_demo_manifest(manifest)
    root_task_id = UUID("30000000-0000-0000-0000-000000000002")
    dispatcher = FakeDispatcher()
    client = FakeRevisionClient(root_task_id)
    adapter = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=dispatcher,
        client=cast(ControlPlaneClient, client),
        store=DemoKeyValueStore(tmp_path / "conversation.json"),
    )
    thread = await adapter.create_thread(title="CSV 导入", context_refs=())
    initial = await (
        await adapter.start_turn(thread.id, "为项目增加 CSV 批量导入功能")
    ).result()
    initial_tool = next(item for item in initial.items if item.type == "toolCall")
    client.planning_request_id = UUID(str(initial_tool.data["planning_request_id"]))

    revision = await (
        await adapter.start_turn(thread.id, "还需要支持 TSV 文件")
    ).result()
    revision_tool = [item for item in revision.items if item.type == "toolCall"][-1]
    command = dispatcher.calls[-1][1]

    assert revision.status == "completed"
    assert revision_tool.data["intent"] == "add_tsv"
    assert command.command_type == "SubmitPlanRevision"
    assert command.target_id == root_task_id
    assert command.expected_version == 7
    assert command.payload == {
        "root_task_id": str(root_task_id),
        "expected_base_snapshot_id": "30000000-0000-0000-0000-000000000001",
        "expected_base_generation": 1,
        "expected_base_content_hash": "c" * 64,
        "objective": "还需要支持 TSV 文件",
        "constraints": ["保持既有 CSV 校验和报告结果可复用"],
        "acceptance_criteria": ["CSV 与 TSV 都能完成导入验证"],
        "planning_profile_id": str(demo_manifest.planning_profile_id),
        "planning_profile_version": demo_manifest.planning_profile_version,
    }
    await adapter.close()

def _settings(manifest: Path):
    from sidera_main_agent_gateway.config import Settings

    return Settings(
        adapter_kind="interview-demo",
        demo_manifest=manifest,
        akashic_endpoint=None,
        sidera_workspace_id=WORKSPACE_ID,
    )


def test_demo_gateway_does_not_construct_the_akashic_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    import sidera_main_agent_gateway.api.app as app_module
    from sidera_main_agent_gateway.api.app import create_app

    manifest = _manifest(tmp_path)
    monkeypatch.setattr(
        app_module,
        "AkashicAdapter",
        lambda *_args, **_kwargs: pytest.fail("demo gateway constructed Akashic"),
    )

    with TestClient(create_app(_settings(manifest))) as client:
        response = client.get("/main-agent/v1/health")

    assert response.status_code == 200
    assert response.json()["provider"] == "available"


@pytest.mark.asyncio
async def test_demo_adapter_classifies_finite_intents_and_persists_planning_turn(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter
    from sidera_main_agent_gateway.adapters.demo.intents import classify_demo_intent
    from sidera_main_agent_gateway.adapters.demo.store import DemoKeyValueStore

    assert classify_demo_intent("为项目增加 CSV 批量导入功能") == "create_csv"
    assert classify_demo_intent(
        "为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认"
    ) == "create_csv"
    assert classify_demo_intent("还需要支持 TSV 文件") == "add_tsv"
    assert classify_demo_intent("手机号为空时向我确认") == "optional_phone"
    assert classify_demo_intent("部署到我的生产服务器") == "unsupported"

    manifest = _manifest(tmp_path)
    dispatcher = FakeDispatcher()
    store = DemoKeyValueStore(tmp_path / "conversation.json")
    adapter = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=dispatcher,
        store=store,
    )
    thread = await adapter.create_thread(title="CSV 导入", context_refs=("task:demo",))
    handle = await adapter.start_turn(thread.id, "为项目增加 CSV 批量导入功能")
    turn = await handle.result()
    replayed = await handle.result()
    await adapter.close()
    persisted = await DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=FakeDispatcher(),
        store=DemoKeyValueStore(tmp_path / "conversation.json"),
    ).read_thread(thread.id)

    assert turn.status == "completed"
    assert replayed.id == turn.id
    assert len(dispatcher.calls) == 1
    tool_call = next(item for item in turn.items if item.type == "toolCall")
    assert tool_call.data["planning_request_id"]
    assert dispatcher.calls[0][1].payload["context_refs"] == ["task:demo"]
    assert persisted.turns[0].id == turn.id


@pytest.mark.asyncio
async def test_demo_adapter_does_not_dispatch_unknown_or_optional_phone_inputs(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter
    from sidera_main_agent_gateway.adapters.demo.store import DemoKeyValueStore

    manifest = _manifest(tmp_path)
    dispatcher = FakeDispatcher()
    adapter = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=dispatcher,
        store=DemoKeyValueStore(tmp_path / "conversation.json"),
    )
    thread = await adapter.create_thread(title="Controls", context_refs=())

    optional = await (await adapter.start_turn(thread.id, "手机号为空时向我确认")).result()
    unsupported = await (await adapter.start_turn(thread.id, "部署到我的生产服务器")).result()

    assert optional.status == "completed"
    assert unsupported.status == "completed"
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_demo_adapter_interrupt_persists_and_reopens_after_restart(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter
    from sidera_main_agent_gateway.adapters.demo.store import DemoKeyValueStore

    manifest = _manifest(tmp_path)
    store_path = tmp_path / "conversation.json"
    dispatcher = FakeDispatcher()
    adapter = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=dispatcher,
        store=DemoKeyValueStore(store_path),
    )
    thread = await adapter.create_thread(title="Interrupt", context_refs=())
    handle = await adapter.start_turn(thread.id, "部署到我的生产服务器")
    interrupted = await adapter.interrupt_turn(thread.id, handle.id)
    polled = await handle.result()
    await adapter.close()
    restarted = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=FakeDispatcher(),
        store=DemoKeyValueStore(store_path),
    )

    assert interrupted.status == "interrupted"
    assert polled.status == "interrupted"
    assert dispatcher.calls == []
    assert (await restarted.read_turn(thread.id, handle.id)).status == "interrupted"
    await restarted.close()


@pytest.mark.asyncio
async def test_interrupt_during_provider_call_cannot_be_overwritten_by_turn_completion(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter
    from sidera_main_agent_gateway.adapters.demo.store import DemoKeyValueStore

    manifest = _manifest(tmp_path)
    dispatcher = BlockingDispatcher()
    adapter = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=dispatcher,
        store=DemoKeyValueStore(tmp_path / "conversation.json"),
    )
    thread = await adapter.create_thread(title="Interrupt race", context_refs=())
    handle = await adapter.start_turn(thread.id, "为项目增加 CSV 批量导入功能")

    async def consume_events() -> None:
        async for _event in handle.events():
            pass

    events_task = asyncio.create_task(consume_events())
    await dispatcher.started.wait()

    interrupted = await adapter.interrupt_turn(thread.id, handle.id)
    dispatcher.release.set()
    await events_task
    final = await adapter.read_turn(thread.id, handle.id)
    await adapter.close()

    assert interrupted.status == "interrupted"
    assert final.status == "interrupted"


@pytest.mark.asyncio
async def test_queued_turn_survives_restart_as_pollable_state(tmp_path: Path) -> None:
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter
    from sidera_main_agent_gateway.adapters.demo.store import DemoKeyValueStore
    from sidera_main_agent_gateway.relay import TurnRelayRegistry

    manifest = _manifest(tmp_path)
    store_path = tmp_path / "conversation.json"
    adapter = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=FakeDispatcher(),
        store=DemoKeyValueStore(store_path),
    )
    thread = await adapter.create_thread(title="Restart", context_refs=())
    handle = await adapter.start_turn(thread.id, "部署到我的生产服务器")
    await adapter.close()

    restarted = DemoMainAgentAdapter(
        _settings(manifest),
        dispatcher=FakeDispatcher(),
        store=DemoKeyValueStore(store_path),
    )
    recovered = await restarted.read_turn(thread.id, handle.id)
    availability = TurnRelayRegistry(event_limit=32).availability(
        thread.id, handle.id, recovered.status
    )

    assert recovered.status == "queued"
    assert availability.stream_available is False
    assert availability.recovery_mode == "poll"
    await restarted.close()
