import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from agent.plugins import Plugin, tool
from agent.plugins.registry import MetadataKind, plugin_registry

if __package__:
    from .sidera_task_bridge.client import CommandDispatcher, ControlPlaneClient
    from .sidera_task_bridge.config import TaskBridgeConfig
    from .sidera_task_bridge.knowledge_client import (
        KnowledgeClient,
        KnowledgeCommandClient,
    )
    from .sidera_task_bridge.knowledge_tools import SideraKnowledgeTools
    from .sidera_task_bridge.notification_gateway import GatewayNotificationRunner
    from .sidera_task_bridge.notification_tools import SideraNotificationTools
    from .sidera_task_bridge.notifications import (
        NotificationDelivery,
        NotificationEvaluator,
        NotificationRunner,
        NotificationRunnerProtocol,
    )
    from .sidera_task_bridge.source_routes import SourceContextResolver
    from .sidera_task_bridge.state import BridgeStateStore
    from .sidera_task_bridge.tools import SideraTaskTools
else:
    from sidera_task_bridge.client import CommandDispatcher, ControlPlaneClient
    from sidera_task_bridge.config import TaskBridgeConfig
    from sidera_task_bridge.knowledge_client import (
        KnowledgeClient,
        KnowledgeCommandClient,
    )
    from sidera_task_bridge.knowledge_tools import SideraKnowledgeTools
    from sidera_task_bridge.notification_gateway import GatewayNotificationRunner
    from sidera_task_bridge.notification_tools import SideraNotificationTools
    from sidera_task_bridge.notifications import (
        NotificationDelivery,
        NotificationEvaluator,
        NotificationRunner,
        NotificationRunnerProtocol,
    )
    from sidera_task_bridge.source_routes import SourceContextResolver
    from sidera_task_bridge.state import BridgeStateStore
    from sidera_task_bridge.tools import SideraTaskTools


_CONTROL_PLANE_COMMAND_TYPES = frozenset(
    {
        "SubmitPlanningRequest",
        "SubmitPlanRevision",
        "CancelPlanningRequest",
        "AnswerPlanningClarification",
        "RequestProposalRevision",
        "ConfirmPlanningProposal",
        "RejectPlanningProposal",
        "ConfirmPlanRevision",
        "PauseCollaborationRelease",
        "ApproveCollaborationReleaseProposal",
        "RejectCollaborationReleaseProposal",
        "ResumeAutomaticCollaboration",
        "RegisterNotificationRoute",
        "MarkNotificationThreadRead",
        "ArchiveNotificationThread",
        "RequeueNotificationDelivery",
    }
)
_KNOWLEDGE_COMMAND_TYPES = frozenset({"SubmitKnowledgeCandidate"})


class SideraTaskBridge(Plugin):
    """Connect the Akashic Main Agent to the SIDERA control plane."""

    # Keep the live rollout validation revision distinct without changing behavior.
    api_version = 2
    name = "sidera-task-bridge"
    version = "0.1.0"
    desc = "Connect Akashic Main Agent to the SIDERA control plane."
    ConfigModel = TaskBridgeConfig

    def __init__(self) -> None:
        self._client: ControlPlaneClient | None = None
        self._knowledge_client: KnowledgeClient | None = None
        self._dispatcher: CommandDispatcher | None = None
        self._knowledge_dispatcher: CommandDispatcher | None = None
        self._tools: SideraTaskTools | None = None
        self._notification_tools: SideraNotificationTools | None = None
        self._knowledge_tools: SideraKnowledgeTools | None = None
        self._state: BridgeStateStore | None = None
        self._runner: NotificationRunnerProtocol | None = None
        self._supervisor_task: asyncio.Task[object] | None = None
        self._supervisor_stop = asyncio.Event()
        self._bound_kv_store: object | None = None

    async def prepare(self) -> None:
        config = self.context.config
        if not isinstance(config, TaskBridgeConfig):
            raise TypeError("SideraTaskBridge requires TaskBridgeConfig")
        client = ControlPlaneClient(config)
        knowledge_client = KnowledgeClient(config)
        self._client = client
        self._knowledge_client = knowledge_client
        self._bind_runtime_components(config, client, knowledge_client)

    def _bind_runtime_components(
        self,
        config: TaskBridgeConfig,
        client: ControlPlaneClient,
        knowledge_client: KnowledgeClient,
    ) -> None:
        state = BridgeStateStore(self.context.kv_store)
        dispatcher = CommandDispatcher(
            client, state, owned_command_types=_CONTROL_PLANE_COMMAND_TYPES
        )
        knowledge_dispatcher = CommandDispatcher(
            KnowledgeCommandClient(knowledge_client, config),
            state,
            owned_command_types=_KNOWLEDGE_COMMAND_TYPES,
        )
        if config.notification_mode == "gateway":
            runner: NotificationRunnerProtocol = GatewayNotificationRunner(
                config,
                client=client,
                state=state,
                dispatcher=dispatcher,
                tool_registry=self.context.tool_registry,
            )
        else:
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
        self._knowledge_client = knowledge_client
        self._dispatcher = dispatcher
        self._knowledge_dispatcher = knowledge_dispatcher
        self._state = state
        self._runner = runner
        source_resolver = SourceContextResolver()
        self._tools = SideraTaskTools(
            config,
            client=client,
            dispatcher=dispatcher,
            state=state,
            source_resolver=source_resolver,
            session_metadata_provider=self._session_metadata,
        )
        self._notification_tools = SideraNotificationTools(
            config,
            client=client,
            dispatcher=dispatcher,
            state=state,
            source_resolver=source_resolver,
            session_metadata_provider=self._session_metadata,
        )
        self._knowledge_tools = SideraKnowledgeTools(
            config,
            client=knowledge_client,
            dispatcher=knowledge_dispatcher,
            state=state,
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
            knowledge_client = self._knowledge_client
            if knowledge_client is None:
                raise RuntimeError("SideraTaskBridge Knowledge client is not prepared")
            self._bind_runtime_components(config, client, knowledge_client)
        dispatcher = self._dispatcher
        knowledge_dispatcher = self._knowledge_dispatcher
        runner = self._runner
        if dispatcher is None or knowledge_dispatcher is None or runner is None:
            raise RuntimeError("SideraTaskBridge must be prepared before activation")
        if self._supervisor_task is not None and not self._supervisor_task.done():
            return
        self._supervisor_stop = asyncio.Event()
        self._supervisor_task = self.context.create_task(
            self._run_supervisor(dispatcher, runner, knowledge_dispatcher),
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
        knowledge_client = self._knowledge_client
        if knowledge_client is not None:
            await knowledge_client.close()
        self._client = None
        self._knowledge_client = None
        self._dispatcher = None
        self._knowledge_dispatcher = None
        self._state = None
        self._runner = None
        self._supervisor_task = None
        self._tools = None
        self._notification_tools = None
        self._knowledge_tools = None
        self._bound_kv_store = None

    async def _run_supervisor(
        self,
        dispatcher: CommandDispatcher,
        runner: NotificationRunnerProtocol,
        knowledge_dispatcher: CommandDispatcher,
    ) -> None:
        config = self.context.config
        if not isinstance(config, TaskBridgeConfig):
            raise TypeError("SideraTaskBridge requires TaskBridgeConfig")
        while not self._supervisor_stop.is_set():
            results = (
                *await dispatcher.recover_all(),
                *await knowledge_dispatcher.recover_all(),
            )
            if any(
                result.status in {"unavailable", "result_unknown"} for result in results
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

    def _knowledge_toolset(self) -> SideraKnowledgeTools:
        self._ensure_runtime_started()
        if self._knowledge_tools is None:
            raise RuntimeError("SideraKnowledgeTools are not prepared")
        return self._knowledge_tools

    def _notification_toolset(self) -> SideraNotificationTools:
        self._ensure_runtime_started()
        if self._notification_tools is None:
            raise RuntimeError("SideraNotificationTools are not prepared")
        return self._notification_tools

    def _is_validation_runtime(self) -> bool:
        data_dir = getattr(self.context, "data_dir", None)
        if data_dir is not None and "plugin-validation" in Path(data_dir).parts:
            return True
        return self._is_rollout_candidate()

    def _is_rollout_candidate(self) -> bool:
        """Detect the host's staged generation without changing host code."""

        generation_id = str(getattr(self.context, "generation_id", "") or "")
        task_gate = getattr(self.context, "_can_start_tasks", None)
        closure = getattr(task_gate, "__closure__", None) or ()
        for cell in closure:
            try:
                generation = cell.cell_contents
            except ValueError:
                continue
            if getattr(generation, "generation_id", "") != generation_id:
                continue
            state = str(getattr(generation, "state", "") or "")
            return bool(getattr(generation, "reload_tx_id", "")) and state in {
                "activating",
                "candidate",
            }
        return False

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
        """Create and coordinate a SIDERA task with subtasks and workers from a structured user brief."""

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
        name="sidera_create_plan_revision",
        risk="write",
        search_hint="SIDERA task plan revision change requirement topology",
    )
    async def sidera_create_plan_revision(
        self,
        event: object,
        root_task_id: str,
        goal: str,
        constraints: list,
        acceptance_criteria: list,
    ) -> str:
        """Create a SIDERA plan revision from a normalized user brief."""

        return await self._toolset().sidera_create_plan_revision(
            event,
            root_task_id,
            goal,
            cast(list[str], constraints),
            cast(list[str], acceptance_criteria),
        )

    @tool(
        name="sidera_get_plan_revision",
        risk="read-only",
        search_hint="SIDERA inspect plan revision diff impact",
    )
    async def sidera_get_plan_revision(
        self, event: object, planning_request_id: str
    ) -> str:
        """Read a bounded SIDERA plan revision and its hierarchy diff."""

        return await self._toolset().sidera_get_plan_revision(
            event, planning_request_id
        )

    @tool(
        name="sidera_submit_plan_revision_feedback",
        risk="write",
        search_hint="SIDERA plan revision feedback modify proposal",
    )
    async def sidera_submit_plan_revision_feedback(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        feedback: str,
    ) -> str:
        """Request a new exact SIDERA plan revision proposal with feedback."""

        return await self._toolset().sidera_submit_plan_revision_feedback(
            event,
            planning_request_id,
            request_version,
            proposal_revision,
            proposal_content_hash,
            feedback,
        )

    @tool(
        name="sidera_confirm_plan_revision",
        risk="write",
        search_hint="SIDERA plan revision confirm activate",
    )
    async def sidera_confirm_plan_revision(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        diff_hash: str,
        base_snapshot_id: str,
        base_content_hash: str,
        explicit_user_confirmation: bool,
    ) -> str:
        """Confirm one exact SIDERA plan revision after explicit user approval."""

        return await self._toolset().sidera_confirm_plan_revision(
            event,
            planning_request_id,
            request_version,
            proposal_revision,
            proposal_content_hash,
            diff_hash,
            base_snapshot_id,
            base_content_hash,
            explicit_user_confirmation,
        )

    @tool(
        name="sidera_reject_plan_revision",
        risk="write",
        search_hint="SIDERA plan revision reject decline",
    )
    async def sidera_reject_plan_revision(
        self,
        event: object,
        planning_request_id: str,
        request_version: int,
        proposal_revision: int,
        proposal_content_hash: str,
        reason: str,
    ) -> str:
        """Reject one exact SIDERA plan revision proposal."""

        return await self._toolset().sidera_reject_plan_revision(
            event,
            planning_request_id,
            request_version,
            proposal_revision,
            proposal_content_hash,
            reason,
        )

    @tool(
        name="sidera_cancel_plan_revision",
        risk="write",
        search_hint="SIDERA cancel plan revision request",
    )
    async def sidera_cancel_plan_revision(
        self, event: object, planning_request_id: str, request_version: int
    ) -> str:
        """Cancel one exact SIDERA plan revision request."""

        return await self._toolset().sidera_cancel_plan_revision(
            event, planning_request_id, request_version
        )

    @tool(
        name="sidera_get_plan_transition",
        risk="read-only",
        search_hint="SIDERA inspect plan transition worker convergence",
    )
    async def sidera_get_plan_transition(
        self, event: object, plan_transition_id: str
    ) -> str:
        """Read bounded SIDERA plan transition and WorkerItem convergence facts."""

        return await self._toolset().sidera_get_plan_transition(
            event, plan_transition_id
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

    @tool(
        name="sidera_get_task_results",
        risk="read-only",
        always_on=True,
    )
    async def sidera_get_task_results(self, event: object, task_id: str) -> str:
        """Read the bounded result evidence summary for a materialized task."""

        return await self._toolset().sidera_get_task_results(event, task_id)

    @tool(
        name="sidera_get_collaboration_governance",
        risk="read-only",
        always_on=True,
    )
    async def sidera_get_collaboration_governance(
        self, event: object, worker_item_id: str
    ) -> str:
        """Read bounded release-governance status for one WorkerItem."""

        return await self._toolset().sidera_get_collaboration_governance(
            event, worker_item_id
        )

    @tool(
        name="sidera_get_worker_result_inputs",
        risk="read-only",
        always_on=True,
    )
    async def sidera_get_worker_result_inputs(
        self,
        event: object,
        worker_item_id: str,
        execution_request_id: str | None = None,
    ) -> str:
        """Read bounded result-input provenance for one WorkerItem."""

        return await self._toolset().sidera_get_worker_result_inputs(
            event, worker_item_id, execution_request_id
        )

    @tool(
        name="sidera_list_inbox",
        risk="read-only",
        always_on=True,
    )
    async def sidera_list_inbox(
        self,
        event: object,
        category: Literal["action", "progress", "archived"] = "action",
        unread_only: bool = False,
        cursor: str | None = None,
        limit: int = 20,
    ) -> str:
        """List the current operator inbox without changing read state."""

        return await self._notification_toolset().sidera_list_inbox(
            event, category, unread_only, cursor, limit
        )

    @tool(
        name="sidera_get_notification_thread",
        risk="read-only",
        search_hint="SIDERA notification thread inbox task attention",
    )
    async def sidera_get_notification_thread(
        self,
        event: object,
        notification_ref: str | None = None,
        thread_id: str | None = None,
        fact_cursor: str | None = None,
        fact_limit: int = 50,
    ) -> str:
        """Read one exact notification thread and its current handoff."""

        return await self._notification_toolset().sidera_get_notification_thread(
            event, notification_ref, thread_id, fact_cursor, fact_limit
        )

    @tool(
        name="sidera_manage_notification",
        risk="write",
        search_hint="SIDERA manage notification read archive requeue",
    )
    async def sidera_manage_notification(
        self,
        event: object,
        action: Literal["read", "archive", "requeue"],
        thread_id: str | None = None,
        delivery_intent_id: str | None = None,
        expected_version: int | None = None,
        expected_status: str | None = None,
        expected_route_version: int | None = None,
        expected_latest_fact_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        """Apply one explicit notification lifecycle command."""

        return await self._notification_toolset().sidera_manage_notification(
            event,
            action,
            thread_id,
            delivery_intent_id,
            expected_version,
            expected_status,
            expected_route_version,
            expected_latest_fact_id,
            reason,
        )

    @tool(
        name="sidera_pause_collaboration_release",
        risk="write",
        search_hint="SIDERA pause automatic collaboration release WorkerItem",
    )
    async def sidera_pause_collaboration_release(
        self,
        event: object,
        worker_item_id: str,
        worker_item_version: int,
        target_plan_generation: int,
        reason: str | None = None,
    ) -> str:
        """Pause release for one exact WorkerItem after current-fact checks."""

        return await self._toolset().sidera_pause_collaboration_release(
            event,
            worker_item_id,
            worker_item_version,
            target_plan_generation,
            reason,
        )

    @tool(
        name="sidera_approve_collaboration_release",
        risk="write",
        search_hint="SIDERA approve collaboration release proposal",
    )
    async def sidera_approve_collaboration_release(
        self,
        event: object,
        proposal_id: str,
        proposal_version: int,
        explicit_user_confirmation: bool,
        reason: str | None = None,
    ) -> str:
        """Approve one exact release Proposal after explicit user confirmation."""

        return await self._toolset().sidera_approve_collaboration_release(
            event,
            proposal_id,
            proposal_version,
            explicit_user_confirmation,
            reason,
        )

    @tool(
        name="sidera_reject_collaboration_release",
        risk="write",
        search_hint="SIDERA reject collaboration release proposal",
    )
    async def sidera_reject_collaboration_release(
        self,
        event: object,
        proposal_id: str,
        proposal_version: int,
        reason: str | None = None,
    ) -> str:
        """Reject one exact release Proposal after current-fact checks."""

        return await self._toolset().sidera_reject_collaboration_release(
            event, proposal_id, proposal_version, reason
        )

    @tool(
        name="sidera_resume_automatic_collaboration",
        risk="write",
        search_hint="SIDERA resume automatic collaboration WorkerItem Hold",
    )
    async def sidera_resume_automatic_collaboration(
        self,
        event: object,
        worker_item_id: str,
        hold_id: str,
        hold_version: int,
        reason: str | None = None,
    ) -> str:
        """Resume automatic collaboration for one exact active Hold."""

        return await self._toolset().sidera_resume_automatic_collaboration(
            event,
            worker_item_id,
            hold_id,
            hold_version,
            reason,
        )

    @tool(
        name="sidera_search_knowledge",
        risk="read-only",
        always_on=True,
        search_hint="SIDERA project knowledge search reference",
    )
    async def sidera_search_knowledge(
        self,
        event: object,
        query: str,
        tags: list | None = None,
        source_types: list | None = None,
        limit: int = 20,
    ) -> str:
        """Search bounded SIDERA project knowledge references."""

        return await self._knowledge_toolset().sidera_search_knowledge(
            event,
            query,
            tags or [],
            source_types or [],
            limit,
        )

    @tool(
        name="sidera_get_knowledge_page",
        risk="read-only",
        always_on=True,
        search_hint="SIDERA project knowledge page reference",
    )
    async def sidera_get_knowledge_page(
        self,
        event: object,
        page_id: str,
        revision_id: str | None = None,
    ) -> str:
        """Read one SIDERA project knowledge page or exact revision."""

        return await self._knowledge_toolset().sidera_get_knowledge_page(
            event, page_id, revision_id
        )

    @tool(
        name="sidera_submit_knowledge_candidate",
        risk="write",
        search_hint="SIDERA propose project knowledge for review",
    )
    async def sidera_submit_knowledge_candidate(
        self,
        event: object,
        operation: str,
        title: str,
        slug: str,
        sections: list,
        sources: list | None = None,
        summary: str = "",
        tags: list | None = None,
        relations: list | None = None,
        target_page_id: str | None = None,
        expected_revision_id: str | None = None,
        expected_revision_hash: str | None = None,
        supersedes_candidate_id: str | None = None,
    ) -> str:
        """Submit a SIDERA knowledge candidate for user review only."""

        return await self._knowledge_toolset().sidera_submit_knowledge_candidate(
            event,
            operation,
            title,
            slug,
            sections,
            sources,
            summary,
            tags,
            relations,
            target_page_id,
            expected_revision_id,
            expected_revision_hash,
            supersedes_candidate_id,
        )


def _patch_host_tool_schemas() -> None:
    """Restore schemas lost by Akashic's future-annotation loader."""

    collection_items = {
        "sidera_submit_brief": {
            "constraints": {"type": "string"},
            "acceptance_criteria": {"type": "string"},
            "context_refs": {"type": "string"},
            "requested_outputs": {"type": "string"},
        },
        "sidera_create_plan_revision": {
            "constraints": {"type": "string"},
            "acceptance_criteria": {"type": "string"},
        },
        "sidera_answer_clarifications": {
            "answers": {"type": "object"},
        },
        "sidera_search_knowledge": {
            "tags": {"type": "string"},
            "source_types": {"type": "string"},
        },
        "sidera_submit_knowledge_candidate": {
            "sections": {"type": "object"},
            "sources": {"type": "object"},
            "tags": {"type": "string"},
            "relations": {"type": "object"},
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
        "sidera_submit_plan_revision_feedback": {
            "request_version": "number",
            "proposal_revision": "number",
        },
        "sidera_confirm_plan_revision": {
            "request_version": "number",
            "proposal_revision": "number",
            "explicit_user_confirmation": "boolean",
        },
        "sidera_reject_plan_revision": {
            "request_version": "number",
            "proposal_revision": "number",
        },
        "sidera_cancel_plan_revision": {"request_version": "number"},
        "sidera_reject_proposal": {
            "request_version": "number",
            "proposal_revision": "number",
        },
        "sidera_pause_collaboration_release": {
            "worker_item_version": "number",
            "target_plan_generation": "number",
        },
        "sidera_approve_collaboration_release": {
            "proposal_version": "number",
            "explicit_user_confirmation": "boolean",
        },
        "sidera_reject_collaboration_release": {"proposal_version": "number"},
        "sidera_resume_automatic_collaboration": {"hold_version": "number"},
        "sidera_search_knowledge": {"limit": "number"},
        "sidera_list_inbox": {
            "unread_only": "boolean",
            "limit": "number",
        },
        "sidera_get_notification_thread": {"fact_limit": "number"},
        "sidera_manage_notification": {
            "expected_version": "number",
            "expected_route_version": "number",
        },
    }
    numeric_bounds = {
        "sidera_search_knowledge": {
            "limit": {"minimum": 1, "maximum": 20},
        },
        "sidera_pause_collaboration_release": {
            "worker_item_version": {"minimum": 1},
            "target_plan_generation": {"minimum": 1},
        },
        "sidera_list_inbox": {"limit": {"minimum": 1, "maximum": 20}},
        "sidera_get_notification_thread": {"fact_limit": {"minimum": 1, "maximum": 50}},
        "sidera_manage_notification": {
            "expected_version": {"minimum": 1},
            "expected_route_version": {"minimum": 1},
        },
        "sidera_approve_collaboration_release": {
            "proposal_version": {"minimum": 1},
        },
        "sidera_reject_collaboration_release": {
            "proposal_version": {"minimum": 1},
        },
        "sidera_resume_automatic_collaboration": {
            "hold_version": {"minimum": 1},
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
                property_schema.update(
                    (numeric_bounds.get(metadata.tool_name or "") or {}).get(name, {})
                )
        for name, item_schema in (fields or {}).items():
            property_schema = properties.get(name)
            if isinstance(property_schema, dict):
                property_schema["type"] = "array"
                property_schema["items"] = item_schema


_patch_host_tool_schemas()


__all__ = ["SideraTaskBridge"]
