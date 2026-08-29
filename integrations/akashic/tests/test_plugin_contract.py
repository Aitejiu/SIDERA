import asyncio
import importlib.util
import inspect
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import plugin
from sidera_task_bridge.client import DispatchResult
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.state import PendingNotification

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None
        self.writes = 0

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value
        self.writes += 1


class FakeContext:
    def __init__(self) -> None:
        self.config = TaskBridgeConfig.model_validate(
            {
                "workspace_id": WORKSPACE_ID,
                "planning_profile_id": PROFILE_ID,
                "planning_profile_version": 1,
            }
        )
        self.kv_store = MemoryKV()
        self.tool_registry = object()
        self.session_manager = None
        self.data_dir = Path("/tmp/sidera/plugin-data")
        self.generation_id = ""
        self._can_start_tasks: Any = None
        self.tasks: list[asyncio.Task[object]] = []
        self.task_names: list[str | None] = []

    def create_task(self, coroutine: object, *, name: str | None = None) -> asyncio.Task[object]:
        task = asyncio.create_task(coroutine)  # type: ignore[arg-type]
        self.tasks.append(task)
        self.task_names.append(name)
        return task


def test_plugin_declares_exact_seventeen_tools_and_risks() -> None:
    from agent.plugins.registry import MetadataKind, plugin_registry

    metadata = [
        item
        for item in plugin_registry.get_handlers_by_module_path(plugin.__name__)
        if item.kind is MetadataKind.TOOL
    ]
    assert len(metadata) == 17
    assert {item.tool_name for item in metadata} == {
        "sidera_submit_brief",
        "sidera_get_planning_status",
        "sidera_answer_clarifications",
        "sidera_request_proposal_revision",
        "sidera_confirm_proposal",
        "sidera_reject_proposal",
        "sidera_create_plan_revision",
        "sidera_get_plan_revision",
        "sidera_submit_plan_revision_feedback",
        "sidera_confirm_plan_revision",
        "sidera_reject_plan_revision",
        "sidera_cancel_plan_revision",
        "sidera_get_plan_transition",
        "sidera_get_task_status",
        "sidera_search_knowledge",
        "sidera_get_knowledge_page",
        "sidera_submit_knowledge_candidate",
    }
    read_only = {
        "sidera_get_planning_status",
        "sidera_get_task_status",
        "sidera_get_plan_revision",
        "sidera_get_plan_transition",
        "sidera_search_knowledge",
        "sidera_get_knowledge_page",
    }
    for item in metadata:
        assert item.tool_risk == ("read-only" if item.tool_name in read_only else "write")
        assert item.tool_schema is not None
        assert "self" not in item.tool_schema["properties"]
        assert "event" not in item.tool_schema["properties"]
    always_on = {item.tool_name for item in metadata if item.tool_always_on}
    assert always_on == {
        "sidera_submit_brief",
        "sidera_get_planning_status",
        "sidera_get_task_status",
        "sidera_search_knowledge",
        "sidera_get_knowledge_page",
    }
    hints = {
        item.tool_name for item in metadata if item.tool_search_hint is not None
    }
    assert hints == {
        "sidera_answer_clarifications",
        "sidera_request_proposal_revision",
        "sidera_confirm_proposal",
        "sidera_reject_proposal",
        "sidera_create_plan_revision",
        "sidera_get_plan_revision",
        "sidera_submit_plan_revision_feedback",
        "sidera_confirm_plan_revision",
        "sidera_reject_plan_revision",
        "sidera_cancel_plan_revision",
        "sidera_get_plan_transition",
        "sidera_search_knowledge",
        "sidera_get_knowledge_page",
        "sidera_submit_knowledge_candidate",
    }


def test_knowledge_tool_schemas_expose_business_fields_only() -> None:
    from agent.plugins.registry import MetadataKind, plugin_registry

    metadata = {
        item.tool_name: item
        for item in plugin_registry.get_handlers_by_module_path(plugin.__name__)
        if item.kind is MetadataKind.TOOL
    }
    forbidden = {
        "workspace_id",
        "actor",
        "token",
        "authorization",
        "command_id",
        "idempotency_key",
        "correlation_id",
        "source_route",
    }
    for name in (
        "sidera_search_knowledge",
        "sidera_get_knowledge_page",
        "sidera_submit_knowledge_candidate",
    ):
        schema = metadata[name].tool_schema
        assert schema is not None
        assert not forbidden.intersection(schema["properties"])

    search = metadata["sidera_search_knowledge"].tool_schema
    assert search is not None
    assert search["properties"]["tags"]["type"] == "array"
    assert search["properties"]["source_types"]["type"] == "array"
    assert search["properties"]["limit"] == {
        "type": "number",
        "minimum": 1,
        "maximum": 20,
    }

    get_page = metadata["sidera_get_knowledge_page"].tool_schema
    assert get_page is not None
    assert get_page["properties"]["page_id"]["type"] == "string"
    assert "revision_id" not in get_page["required"]

    submit = metadata["sidera_submit_knowledge_candidate"].tool_schema
    assert submit is not None
    assert submit["properties"]["sections"]["type"] == "array"
    assert submit["properties"]["sources"]["type"] == "array"


def test_plugin_schema_preserves_list_arguments_as_arrays() -> None:
    from agent.plugins.registry import MetadataKind, plugin_registry

    metadata = {
        item.tool_name: item
        for item in plugin_registry.get_handlers_by_module_path(plugin.__name__)
        if item.kind is MetadataKind.TOOL
    }

    submit_schema = metadata["sidera_submit_brief"].tool_schema
    assert submit_schema is not None
    assert all(
        submit_schema["properties"][name]["type"] == "array"
        for name in ("constraints", "acceptance_criteria", "context_refs", "requested_outputs")
    )

    clarification_schema = metadata["sidera_answer_clarifications"].tool_schema
    assert clarification_schema is not None
    assert clarification_schema["properties"]["answers"]["type"] == "array"

    revision_create = metadata["sidera_create_plan_revision"].tool_schema
    assert revision_create is not None
    assert revision_create["properties"]["constraints"]["type"] == "array"
    assert revision_create["properties"]["acceptance_criteria"]["type"] == "array"


def test_plugin_schema_preserves_write_argument_scalar_types() -> None:
    from agent.plugins.registry import MetadataKind, plugin_registry

    metadata = {
        item.tool_name: item
        for item in plugin_registry.get_handlers_by_module_path(plugin.__name__)
        if item.kind is MetadataKind.TOOL
    }

    revision_schema = metadata["sidera_request_proposal_revision"].tool_schema
    assert revision_schema is not None
    assert revision_schema["properties"]["request_version"]["type"] == "number"
    assert revision_schema["properties"]["proposal_revision"]["type"] == "number"

    confirmation_schema = metadata["sidera_confirm_proposal"].tool_schema
    assert confirmation_schema is not None
    assert confirmation_schema["properties"]["request_version"]["type"] == "number"
    assert confirmation_schema["properties"]["proposal_revision"]["type"] == "number"
    assert (
        confirmation_schema["properties"]["explicit_user_confirmation"]["type"]
        == "boolean"
    )

    revision_confirmation = metadata["sidera_confirm_plan_revision"].tool_schema
    assert revision_confirmation is not None
    assert revision_confirmation["properties"]["request_version"]["type"] == "number"
    assert revision_confirmation["properties"]["proposal_revision"]["type"] == "number"
    assert revision_confirmation["properties"]["explicit_user_confirmation"]["type"] == "boolean"


def test_plugin_handlers_use_host_compatible_list_annotations() -> None:
    submit = inspect.signature(plugin.SideraTaskBridge.sidera_submit_brief)
    clarification = inspect.signature(
        plugin.SideraTaskBridge.sidera_answer_clarifications
    )

    assert all(
        submit.parameters[name].annotation is list
        for name in (
            "constraints",
            "acceptance_criteria",
            "context_refs",
            "requested_outputs",
        )
    )
    assert clarification.parameters["answers"].annotation is list
    revision_create = inspect.signature(plugin.SideraTaskBridge.sidera_create_plan_revision)
    assert revision_create.parameters["constraints"].annotation is list
    assert revision_create.parameters["acceptance_criteria"].annotation is list


def test_plugin_loads_with_akashic_installer_module_boundary() -> None:
    """Mirror the host installer, which gives plugin.py a package search root."""

    from agent.plugins.importer import FreshPluginImporter
    from agent.plugins.registry import plugin_registry

    root = Path(__file__).parents[1]
    module_name = "task_bridge_installer_boundary_test"
    importer = FreshPluginImporter()
    importer.register(module_name, root)
    spec = importer.root_spec(module_name, root / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    root_text = str(root.resolve())
    saved_path = sys.path[:]
    saved_bridge_modules = {
        name: value
        for name, value in sys.modules.items()
        if name == "sidera_task_bridge" or name.startswith("sidera_task_bridge.")
    }
    sys.path[:] = [item for item in sys.path if item != root_text]
    for imported_name in saved_bridge_modules:
        _ = sys.modules.pop(imported_name, None)
    try:
        spec.loader.exec_module(module)
        assert module.SideraTaskBridge.name == "sidera-task-bridge"
        metadata = {
            item.tool_name: item
            for item in plugin_registry.get_handlers_by_module_path(module_name)
            if item.kind.name == "TOOL"
        }
        submit_schema = metadata["sidera_submit_brief"].tool_schema
        assert submit_schema is not None
        assert all(
            submit_schema["properties"][name]["type"] == "array"
            for name in (
                "constraints",
                "acceptance_criteria",
                "context_refs",
                "requested_outputs",
            )
        )
        clarification_schema = metadata["sidera_answer_clarifications"].tool_schema
        assert clarification_schema is not None
        assert clarification_schema["properties"]["answers"] == {
            "type": "array",
            "items": {"type": "object"},
        }
        revision_schema = metadata["sidera_request_proposal_revision"].tool_schema
        assert revision_schema is not None
        assert revision_schema["properties"]["request_version"]["type"] == "number"
        assert revision_schema["properties"]["proposal_revision"]["type"] == "number"
        confirmation_schema = metadata["sidera_confirm_proposal"].tool_schema
        assert confirmation_schema is not None
        assert confirmation_schema["properties"]["request_version"]["type"] == "number"
        assert confirmation_schema["properties"]["proposal_revision"]["type"] == "number"
        assert (
            confirmation_schema["properties"]["explicit_user_confirmation"]["type"]
            == "boolean"
        )
        revision_confirmation = metadata["sidera_confirm_plan_revision"].tool_schema
        assert revision_confirmation is not None
        assert revision_confirmation["properties"]["request_version"]["type"] == "number"
        assert revision_confirmation["properties"]["proposal_revision"]["type"] == "number"
    finally:
        importer.unregister(module_name)
        plugin_registry.remove_module_tree(module_name)
        for imported_name in tuple(sys.modules):
            if imported_name == module_name or imported_name.startswith(f"{module_name}."):
                _ = sys.modules.pop(imported_name, None)
        for imported_name in tuple(sys.modules):
            if imported_name == "sidera_task_bridge" or imported_name.startswith(
                "sidera_task_bridge."
            ):
                _ = sys.modules.pop(imported_name, None)
        sys.modules.update(saved_bridge_modules)
        sys.path[:] = saved_path


def test_prepare_constructs_bridge_without_starting_recovery_or_network() -> None:
    async def scenario() -> None:
        context = FakeContext()
        bridge = plugin.SideraTaskBridge()
        cast(Any, bridge).context = context

        await bridge.prepare()

        assert bridge._client is not None
        assert bridge._knowledge_client is not None
        assert bridge._dispatcher is not None
        assert bridge._knowledge_dispatcher is not None
        assert bridge._tools is not None
        assert bridge._knowledge_tools is not None
        assert context.tasks == []
        assert context.kv_store.writes == 0
        await bridge.terminate()

    asyncio.run(scenario())


def test_activate_starts_one_named_supervisor_and_retire_is_read_only() -> None:
    async def scenario() -> None:
        context = FakeContext()
        bridge = plugin.SideraTaskBridge()
        cast(Any, bridge).context = context
        await bridge.prepare()

        bridge.activate()
        assert context.task_names == ["sidera-task-bridge-supervisor"]
        bridge.retire()
        await asyncio.gather(*context.tasks)
        assert context.kv_store.writes == 0
        await bridge.terminate()

    asyncio.run(scenario())


def test_candidate_activation_defers_supervisor_until_promotion() -> None:
    async def scenario() -> None:
        context = FakeContext()
        generation = SimpleNamespace(
            generation_id="candidate-generation",
            reload_tx_id="reload-transaction",
            state="activating",
        )
        context.generation_id = generation.generation_id
        context._can_start_tasks = lambda: generation.state in {
            "activating",
            "active",
            "candidate",
        }
        bridge = plugin.SideraTaskBridge()
        cast(Any, bridge).context = context
        await bridge.prepare()

        bridge.activate()
        try:
            assert context.tasks == []
        finally:
            bridge.retire()
            await asyncio.gather(*context.tasks)
            await bridge.terminate()

    asyncio.run(scenario())


def test_promoted_runtime_starts_deferred_supervisor_on_first_tool_access() -> None:
    async def scenario() -> None:
        context = FakeContext()
        generation = SimpleNamespace(
            generation_id="candidate-generation",
            reload_tx_id="reload-transaction",
            state="candidate",
        )
        context.generation_id = generation.generation_id
        context._can_start_tasks = lambda: generation.state in {
            "activating",
            "active",
            "candidate",
        }
        bridge = plugin.SideraTaskBridge()
        cast(Any, bridge).context = context
        await bridge.prepare()

        bridge.activate()
        assert context.tasks == []

        generation.state = "active"
        context.kv_store = MemoryKV()
        bridge._toolset()
        try:
            assert len(context.tasks) == 1
            assert cast(Any, bridge)._bound_kv_store is context.kv_store
        finally:
            bridge.retire()
            await asyncio.gather(*context.tasks)
            await bridge.terminate()

    asyncio.run(scenario())


def test_supervisor_recovers_commands_before_starting_notification_runner() -> None:
    async def scenario() -> None:
        context = FakeContext()
        bridge = plugin.SideraTaskBridge()
        cast(Any, bridge).context = context
        await bridge.prepare()
        events: list[str] = []

        async def recover() -> tuple[DispatchResult, ...]:
            events.append("recover")
            return ()

        async def run_runner() -> None:
            events.append("run")

        bridge._dispatcher.recover_all = recover  # type: ignore[method-assign]
        bridge._runner.run = run_runner  # type: ignore[method-assign]
        bridge.activate()
        await asyncio.gather(*context.tasks)
        assert events == ["recover", "run"]
        await bridge.terminate()

    asyncio.run(scenario())


def test_supervisor_does_not_start_notifications_when_command_recovery_is_unavailable() -> None:
    async def scenario() -> None:
        context = FakeContext()
        bridge = plugin.SideraTaskBridge()
        cast(Any, bridge).context = context
        await bridge.prepare()
        events: list[str] = []

        async def recover() -> tuple[DispatchResult, ...]:
            events.append("recover")
            return (DispatchResult(status="unavailable", command_id=UUID(int=1)),)

        async def run_runner() -> None:
            events.append("run")

        bridge._dispatcher.recover_all = recover  # type: ignore[method-assign]
        bridge._runner.run = run_runner  # type: ignore[method-assign]
        bridge.activate()
        await asyncio.sleep(0.01)
        assert events == ["recover"]
        bridge.retire()
        await asyncio.gather(*context.tasks)
        await bridge.terminate()

    asyncio.run(scenario())


def test_hot_reload_reads_v2_pending_state_without_clearing_it() -> None:
    async def scenario() -> None:
        context = FakeContext()
        context.kv_store.value = {
            "schema_version": 2,
            "pending_notifications": [
                PendingNotification(
                    notice_id="notice-1",
                    cursor=4,
                    resource_key="planning_request:00000000-0000-0000-0000-000000000010",
                    source_ref="src_private",
                    kind="planning_proposal_ready",
                    message="方案待确认",
                    web_url="http://127.0.0.1:4174/workspaces/ws/planning/request",
                    created_at=datetime(2026, 8, 24, 3, 30, tzinfo=UTC),
                ).model_dump(mode="json")
            ],
        }
        bridge = plugin.SideraTaskBridge()
        cast(Any, bridge).context = context
        await bridge.prepare()
        assert bridge._state is not None
        state = await bridge._state.snapshot()
        assert [item.notice_id for item in state.pending_notifications] == ["notice-1"]
        assert context.kv_store.writes == 0
        await bridge.terminate()

    asyncio.run(scenario())
