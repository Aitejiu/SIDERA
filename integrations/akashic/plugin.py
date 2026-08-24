import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from agent.plugins import Plugin, tool
from agent.plugins.registry import MetadataKind, plugin_registry

if __package__:
    from .sidera_task_bridge.client import CommandDispatcher, ControlPlaneClient
    from .sidera_task_bridge.config import TaskBridgeConfig
    from .sidera_task_bridge.notifications import (
        NotificationDelivery,
        NotificationEvaluator,
        NotificationRunner,
    )
    from .sidera_task_bridge.source_routes import SourceContextResolver
    from .sidera_task_bridge.state import BridgeStateStore
    from .sidera_task_bridge.tools import SideraTaskTools
else:
    from sidera_task_bridge.client import CommandDispatcher, ControlPlaneClient
    from sidera_task_bridge.config import TaskBridgeConfig
    from sidera_task_bridge.notifications import (
        NotificationDelivery,
        NotificationEvaluator,
        NotificationRunner,
    )
    from sidera_task_bridge.source_routes import SourceContextResolver
    from sidera_task_bridge.state import BridgeStateStore
    from sidera_task_bridge.tools import SideraTaskTools


class SideraTaskBridge(Plugin):
    """Connect the Akashic Main Agent to the SIDERA control plane."""

    api_version = 2
    name = "sidera-task-bridge"
    version = "0.1.0"
    desc = "Connect Akashic Main Agent to the SIDERA control plane."
    ConfigModel = TaskBridgeConfig

    def __init__(self) -> None:
        self._client: ControlPlaneClient | None = None
        self._dispatcher: CommandDispatcher | None = None
        self._tools: SideraTaskTools | None = None
        self._state: BridgeStateStore | None = None
        self._runner: NotificationRunner | None = None
        self._supervisor_task: asyncio.Task[object] | None = None
        self._supervisor_stop = asyncio.Event()
        self._bound_kv_store: object | None = None

    async def prepare(self) -> None:
        config = self.context.config
        if not isinstance(config, TaskBridgeConfig):
            raise TypeError("SideraTaskBridge requires TaskBridgeConfig")
        self._client = ControlPlaneClient(config)
        self._bind_runtime_components(config, self._client)

    def _bind_runtime_components(
        self,
        config: TaskBridgeConfig,
        client: ControlPlaneClient,
    ) -> None:
        state = BridgeStateStore(self.context.kv_store)
        dispatcher = CommandDispatcher(client, state)
        evaluator = NotificationEvaluator(config, client=client, state=state)
        delivery = NotificationDelivery(
            config,
            state=state,
            tool_registry=self.context.tool_registry,
        )
        runner = NotificationRunner(
            config,
            client=client,
            state=state,
            evaluator=evaluator,
            delivery=delivery,
        )
        self._client = client
        self._dispatcher = dispatcher
        self._state = state
        self._runner = runner
        self._tools = SideraTaskTools(
            config,
            client=client,
            dispatcher=dispatcher,
            state=state,
            source_resolver=SourceContextResolver(),
            session_metadata_provider=self._session_metadata,
        )
        self._bound_kv_store = self.context.kv_store

    def activate(self) -> None:
        self._ensure_runtime_started()

    def _ensure_runtime_started(self) -> None:
        client = self._client
        config = self.context.config
        if client is None or not isinstance(config, TaskBridgeConfig):
            raise RuntimeError("SideraTaskBridge must be prepared before activation")
        if self._is_validation_runtime():
            return
        if self._bound_kv_store is not self.context.kv_store:
            self._bind_runtime_components(config, client)
        dispatcher = self._dispatcher
        runner = self._runner
        if dispatcher is None or runner is None:
            raise RuntimeError("SideraTaskBridge must be prepared before activation")
        if self._supervisor_task is not None and not self._supervisor_task.done():
            return
        self._supervisor_stop = asyncio.Event()
        self._supervisor_task = self.context.create_task(
            self._run_supervisor(dispatcher, runner),
            name="sidera-task-bridge-supervisor",
        )

    def retire(self) -> None:
        self._supervisor_stop.set()
        if self._runner is not None:
            self._runner.stop()

    async def terminate(self) -> None:
        self.retire()
        task = self._supervisor_task
        if task is not None and task is not asyncio.current_task():
            await task
        client = self._client
        if client is not None:
            await client.close()
        self._client = None
        self._dispatcher = None
        self._state = None
        self._runner = None
        self._supervisor_task = None
        self._tools = None
        self._bound_kv_store = None

    async def _run_supervisor(
        self,
        dispatcher: CommandDispatcher,
        runner: NotificationRunner,
    ) -> None:
        config = self.context.config
        if not isinstance(config, TaskBridgeConfig):
            raise TypeError("SideraTaskBridge requires TaskBridgeConfig")
        while not self._supervisor_stop.is_set():
            results = await dispatcher.recover_all()
            if any(
                result.status in {"unavailable", "result_unknown"}
                for result in results
            ):
                try:
                    await asyncio.wait_for(
                        self._supervisor_stop.wait(),
                        timeout=config.notification_retry_base_seconds,
                    )
                except TimeoutError:
                    pass
                continue
            await runner.run()
            return

    def _session_metadata(self, session_key: str) -> Mapping[str, object]:
        manager = self.context.session_manager
        if manager is None or not session_key:
            return {}
        try:
            session = manager.get_existing(session_key)
        except KeyError:
            return {}
        metadata = getattr(session, "metadata", {})
        return dict(metadata) if isinstance(metadata, dict) else {}

    def _toolset(self) -> SideraTaskTools:
        self._ensure_runtime_started()
        if self._tools is None:
            raise RuntimeError("SideraTaskBridge tools are not prepared")
        return self._tools

    def _is_validation_runtime(self) -> bool:
        data_dir = getattr(self.context, "data_dir", None)
        if data_dir is None:
            return False
        return "plugin-validation" in Path(data_dir).parts

    @tool(name="sidera_submit_brief", risk="write", always_on=True)
    async def sidera_submit_brief(
        self,
        event: object,
        goal: str,
        constraints: list,
        acceptance_criteria: list,
        context_refs: list,
        requested_outputs: list,
    ) -> str:
        """Submit a structured user brief to the SIDERA planning control plane."""

        return await self._toolset().sidera_submit_brief(
            event,
            goal,
            cast(list[str], constraints),
            cast(list[str], acceptance_criteria),
            cast(list[str], context_refs),
            cast(list[str], requested_outputs),
        )

    @tool(name="sidera_get_planning_status", risk="read-only", always_on=True)
    async def sidera_get_planning_status(
        self, event: object, planning_request_id: str
    ) -> str:
        """Read the current SIDERA planning status and proposal summary."""

        return await self._toolset().sidera_get_planning_status(
            event, planning_request_id
        )

    @tool(
        name="sidera_answer_clarifications",
        risk="write",
        search_hint="SIDERA planning clarification answer question",
    )
    async def sidera_answer_clarifications(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        answers: list,
    ) -> str:
        """Answer the current blocking SIDERA planning questions."""

        return await self._toolset().sidera_answer_clarifications(
            event,
            planning_request_id,
            request_version,
            cast(list[dict[str, str]], answers),
        )

    @tool(
        name="sidera_request_proposal_revision",
        risk="write",
        search_hint="SIDERA planning proposal revise feedback",
    )
    async def sidera_request_proposal_revision(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        feedback: str,
    ) -> str:
        """Request a new SIDERA proposal revision with user feedback."""

        return await self._toolset().sidera_request_proposal_revision(
            event,
            planning_request_id,
            request_version,
            proposal_revision,
            proposal_content_hash,
            feedback,
        )

    @tool(
        name="sidera_confirm_proposal",
        risk="write",
        search_hint="SIDERA planning proposal confirm approve",
    )
    async def sidera_confirm_proposal(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        explicit_user_confirmation: bool,
    ) -> str:
        """Confirm one exact SIDERA proposal revision after explicit user approval."""

        return await self._toolset().sidera_confirm_proposal(
            event,
            planning_request_id,
            request_version,
            proposal_revision,
            proposal_content_hash,
            explicit_user_confirmation,
        )

    @tool(
        name="sidera_reject_proposal",
        risk="write",
        search_hint="SIDERA planning proposal reject decline",
    )
    async def sidera_reject_proposal(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        reason: str,
    ) -> str:
        """Reject one exact SIDERA proposal revision with a reason."""

        return await self._toolset().sidera_reject_proposal(
            event,
            planning_request_id,
            request_version,
            proposal_revision,
            proposal_content_hash,
            reason,
        )

    @tool(
        name="sidera_get_task_status",
        risk="read-only",
        always_on=True,
    )
    async def sidera_get_task_status(self, event: object, task_id: str) -> str:
        """Read the bounded SIDERA Task First overview.

        Use this tool whenever the user asks for the status or progress of a
        materialized SIDERA task. Do not use shell or direct control-plane
        requests for SIDERA task state.
        """

        return await self._toolset().sidera_get_task_status(event, task_id)


def _patch_host_tool_schemas() -> None:
    """Restore schemas lost by Akashic's future-annotation loader."""

    collection_items = {
        "sidera_submit_brief": {
            "constraints": {"type": "string"},
            "acceptance_criteria": {"type": "string"},
            "context_refs": {"type": "string"},
            "requested_outputs": {"type": "string"},
        },
        "sidera_answer_clarifications": {
            "answers": {"type": "object"},
        },
    }
    scalar_types = {
        "sidera_answer_clarifications": {"request_version": "number"},
        "sidera_request_proposal_revision": {
            "request_version": "number",
            "proposal_revision": "number",
        },
        "sidera_confirm_proposal": {
            "request_version": "number",
            "proposal_revision": "number",
            "explicit_user_confirmation": "boolean",
        },
        "sidera_reject_proposal": {
            "request_version": "number",
            "proposal_revision": "number",
        },
    }
    for metadata in plugin_registry.get_handlers_by_module_path(__name__):
        if metadata.kind is not MetadataKind.TOOL:
            continue
        fields = collection_items.get(metadata.tool_name or "")
        schema = metadata.tool_schema
        scalar_fields = scalar_types.get(metadata.tool_name or "")
        if schema is None:
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        for name, json_type in (scalar_fields or {}).items():
            property_schema = properties.get(name)
            if isinstance(property_schema, dict):
                property_schema["type"] = json_type
        for name, item_schema in (fields or {}).items():
            property_schema = properties.get(name)
            if isinstance(property_schema, dict):
                property_schema["type"] = "array"
                property_schema["items"] = item_schema


_patch_host_tool_schemas()


__all__ = ["SideraTaskBridge"]
