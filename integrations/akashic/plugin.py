from collections.abc import Mapping
from typing import cast

from agent.plugins import Plugin, tool

if __package__:
    from .sidera_task_bridge.client import CommandDispatcher, ControlPlaneClient
    from .sidera_task_bridge.config import TaskBridgeConfig
    from .sidera_task_bridge.source_routes import SourceContextResolver
    from .sidera_task_bridge.state import BridgeStateStore
    from .sidera_task_bridge.tools import SideraTaskTools
else:
    from sidera_task_bridge.client import CommandDispatcher, ControlPlaneClient
    from sidera_task_bridge.config import TaskBridgeConfig
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

    async def prepare(self) -> None:
        config = self.context.config
        if not isinstance(config, TaskBridgeConfig):
            raise TypeError("SideraTaskBridge requires TaskBridgeConfig")
        client = ControlPlaneClient(config)
        state = BridgeStateStore(self.context.kv_store)
        dispatcher = CommandDispatcher(client, state)
        self._client = client
        self._dispatcher = dispatcher
        self._tools = SideraTaskTools(
            config,
            client=client,
            dispatcher=dispatcher,
            state=state,
            source_resolver=SourceContextResolver(),
            session_metadata_provider=self._session_metadata,
        )

    def activate(self) -> None:
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise RuntimeError("SideraTaskBridge must be prepared before activation")
        self.context.create_task(
            self._recover_unresolved_commands(dispatcher),
            name="sidera-task-bridge-command-recovery",
        )

    def retire(self) -> None:
        return None

    async def terminate(self) -> None:
        client = self._client
        if client is not None:
            await client.close()
        self._client = None
        self._dispatcher = None
        self._tools = None

    async def _recover_unresolved_commands(
        self, dispatcher: CommandDispatcher
    ) -> None:
        await dispatcher.recover_all()

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
        if self._tools is None:
            raise RuntimeError("SideraTaskBridge tools are not prepared")
        return self._tools

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
        search_hint="SIDERA task progress status milestone worker next work item",
    )
    async def sidera_get_task_status(self, event: object, task_id: str) -> str:
        """Read the bounded SIDERA Task First overview."""

        return await self._toolset().sidera_get_task_status(event, task_id)


__all__ = ["SideraTaskBridge"]
