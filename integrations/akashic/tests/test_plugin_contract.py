import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import plugin
from sidera_task_bridge.config import TaskBridgeConfig

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
        self.session_manager = None
        self.tasks: list[asyncio.Task[object]] = []
        self.task_names: list[str | None] = []

    def create_task(self, coroutine: object, *, name: str | None = None) -> asyncio.Task[object]:
        task = asyncio.create_task(coroutine)  # type: ignore[arg-type]
        self.tasks.append(task)
        self.task_names.append(name)
        return task


def test_plugin_declares_exact_seven_tools_and_risks() -> None:
    from agent.plugins.registry import MetadataKind, plugin_registry

    metadata = [
        item
        for item in plugin_registry.get_handlers_by_module_path(plugin.__name__)
        if item.kind is MetadataKind.TOOL
    ]
    assert len(metadata) == 7
    assert {item.tool_name for item in metadata} == {
        "sidera_submit_brief",
        "sidera_get_planning_status",
        "sidera_answer_clarifications",
        "sidera_request_proposal_revision",
        "sidera_confirm_proposal",
        "sidera_reject_proposal",
        "sidera_get_task_status",
    }
    read_only = {"sidera_get_planning_status", "sidera_get_task_status"}
    for item in metadata:
        assert item.tool_risk == ("read-only" if item.tool_name in read_only else "write")
        assert item.tool_schema is not None
        assert "self" not in item.tool_schema["properties"]
        assert "event" not in item.tool_schema["properties"]
    always_on = {item.tool_name for item in metadata if item.tool_always_on}
    assert always_on == {"sidera_submit_brief", "sidera_get_planning_status"}
    hints = {
        item.tool_name for item in metadata if item.tool_search_hint is not None
    }
    assert hints == {
        "sidera_answer_clarifications",
        "sidera_request_proposal_revision",
        "sidera_confirm_proposal",
        "sidera_reject_proposal",
        "sidera_get_task_status",
    }


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
        assert bridge._dispatcher is not None
        assert bridge._tools is not None
        assert context.tasks == []
        assert context.kv_store.writes == 0
        await bridge.terminate()

    asyncio.run(scenario())


def test_activate_starts_one_named_recovery_task_and_retire_is_read_only() -> None:
    async def scenario() -> None:
        context = FakeContext()
        bridge = plugin.SideraTaskBridge()
        cast(Any, bridge).context = context
        await bridge.prepare()

        bridge.activate()
        assert context.task_names == ["sidera-task-bridge-command-recovery"]
        await asyncio.gather(*context.tasks)
        bridge.retire()
        assert context.kv_store.writes == 0
        await bridge.terminate()

    asyncio.run(scenario())
