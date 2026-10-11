"""Construct generic native input without dispatching or assembling a prompt."""

from typing import NoReturn
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_agents.contracts import RunInput, RunPolicy, RunStart
from sidera_agents.errors import AgentError

from sidera_control_plane.application.agent_capabilities import (
    runtime_capability_requirements,
)
from sidera_control_plane.application.agent_sources import canonical_source_text
from sidera_control_plane.application.context_requests import (
    resolve_execution_context_authorization,
)
from sidera_control_plane.domain.inputs import ExecutionInputStatus, input_operation_id


def _fail(code="permission_denied") -> NoReturn:
    raise AgentError(code, "Native execution input is unavailable.")


class TaskExecutionInputBuilder:
    def __init__(self, uow_factory, *, authorizer, source_reader, catalog, runtimes):
        self._uow_factory, self._authorizer, self._sources = (
            uow_factory,
            authorizer,
            source_reader,
        )
        self._catalog, self._runtimes = catalog, runtimes

    def _input_pin(
        self,
        execution_id,
        binding,
        authority_id,
        *,
        operation_id,
        text,
        attachment_refs,
    ):
        if binding is None or binding.profile_ref is None or not binding.runnable:
            _fail("binding_not_runnable")
        self._authorizer.require(authority_id, "run.input", binding.workspace_id)
        with self._uow_factory() as uow:
            authority = uow.execution_authorities.get(
                authority_id, workspace_id=binding.workspace_id
            )
            link = uow.execution_authorities.get_link(
                binding.workspace_id, execution_id
            )
            current = uow.agent_bindings.get(
                binding.workspace_id, binding.target_type, binding.target_id
            )
            item = uow.execution_inputs.get_by_operation_id(
                operation_id, binding.workspace_id, execution_id
            )
            execution = uow.executions.get(execution_id, binding.workspace_id)
            session = (
                None
                if item is None
                else uow.sessions.get(item.session_id, binding.workspace_id)
            )
            if (
                authority is None
                or link is None
                or execution is None
                or item is None
                or session is None
                or current != binding
                or authority.execution_id != execution_id
                or authority.target_type != binding.target_type
                or authority.target_id != binding.target_id
                or link.authority_id != authority_id
                or link.run_id is None
                or link.binding_version != binding.version
                or authority.binding_version != binding.version
                or authority.profile_ref != binding.profile_ref
                or link.profile_ref != binding.profile_ref
                or item.operation_id != input_operation_id(item.id)
                or item.text != text
                or item.attachment_refs != attachment_refs
                or item.status is not ExecutionInputStatus.DISPATCHING
                or item.execution_id != execution_id
                or session.execution_id != execution_id
                or session.status.value != "active"
                or session.adapter_session_id != f"agent-run:{link.run_id}"
                or session.id
                != uuid5(NAMESPACE_URL, f"sidera.native.session:{link.run_id}")
                or session.input_mode != "queued"
            ):
                _fail("native_input_identity_mismatch")
            refs = []
            for key in attachment_refs:
                matches = tuple(ref for ref in authority.sources if ref.key == key)
                if len(matches) != 1 or matches[0] in refs:
                    _fail("native_input_attachment_unavailable")
                refs.append(matches[0])
            pin = (
                authority,
                binding,
                item.id,
                item.execution_id,
                item.session_id,
                item.operation_id,
                item.text,
                item.attachment_refs,
                item.version,
                link.run_id,
                link.profile_ref,
                link.binding_version,
                link.authority_id,
            )
            return pin, link, tuple(refs)

    def build_input(
        self,
        execution_id: UUID,
        binding,
        authority_id: UUID,
        *,
        operation_id: UUID,
        text: str,
        attachment_refs: tuple[str, ...],
    ) -> RunInput:
        pin, link, required_refs = self._input_pin(
            execution_id,
            binding,
            authority_id,
            operation_id=operation_id,
            text=text,
            attachment_refs=attachment_refs,
        )
        context = self._sources.context_items(
            authority_id,
            execution_id,
            reserved_bytes=len(text.encode("utf-8")),
            required_refs=required_refs,
        )
        after, current, _ = self._input_pin(
            execution_id,
            binding,
            authority_id,
            operation_id=operation_id,
            text=text,
            attachment_refs=attachment_refs,
        )
        if after != pin or current.run_id != link.run_id:
            _fail("native_input_identity_mismatch")
        self._authorizer.require(authority_id, "run.input", link.workspace_id)
        return RunInput(
            workspace_id=link.workspace_id,
            run_id=link.run_id,
            operation_id=operation_id,
            grant_id=authority_id,
            text=text,
            context=context,
        )

    def build(self, execution_id: UUID, binding, authority_id: UUID) -> RunStart:
        if binding is None or binding.profile_ref is None or not binding.runnable:
            _fail("binding_not_runnable")
        self._authorizer.require(authority_id, "run.start", binding.profile_ref)
        with self._uow_factory() as uow:
            authority = uow.execution_authorities.get(authority_id)
            if authority is None or authority.execution_id != execution_id:
                _fail()
            link = uow.execution_authorities.get_link(
                authority.workspace_id, execution_id
            )
            current = uow.agent_bindings.get(
                authority.workspace_id, authority.target_type, authority.target_id
            )
            if (
                current != binding
                or link is None
                or link.authority_id != authority_id
                or link.profile_ref != binding.profile_ref
                or link.binding_version != binding.version
            ):
                _fail()
            execution = uow.executions.get(execution_id, authority.workspace_id)
            authorization = resolve_execution_context_authorization(uow, execution)
            if authorization is None:
                _fail("context_unavailable")
            request = authorization.request
            plan = uow.plans.get(request.plan_snapshot_id, authority.workspace_id)
            if (
                plan is None
                or plan.root_task_id != request.root_task_id
                or plan.generation != request.plan_generation
            ):
                _fail("context_invalid")
            role = next(
                (
                    item
                    for item in plan.worker_item_plans
                    if item.worker_item_id == execution.worker_item_id
                ),
                None,
            )
            subtask = next(
                (item for item in plan.subtasks if item.task_id == execution.task_id),
                None,
            )
            if role is None or subtask is None or role.subtask_key != subtask.key:
                _fail("binding_not_runnable")
            requirements = runtime_capability_requirements(
                set(execution.capability_requirements)
                | set(role.capability_requirements)
            )
            objective = subtask.goal_summary
        if not authority.sources:
            _fail("context_unavailable")
        facts = self._sources.validated_facts(authority_id, authority.sources[0])
        instructions = (
            canonical_source_text(
                {
                    "role": role.role,
                    "responsibility": role.responsibility,
                    "expected_outputs": facts.manifest.control_plane_fact_snapshot[
                        "execution_request"
                    ]["expected_outputs"],
                    "acceptance_refs": list(subtask.acceptance_refs),
                }
            ),
        )
        self._authorizer.require(authority_id, "profile.read", binding.profile_ref)
        self._authorizer.require(authority_id, "runtime.read", authority.runtime_ref)
        profile = self._catalog.get_profile(binding.profile_ref, grant_id=authority_id)
        status = self._catalog.get_profile_status(
            binding.profile_ref, grant_id=authority_id
        )
        runtime = self._runtimes.get_runtime(
            authority.runtime_ref, grant_id=authority_id
        )
        if (
            profile.ref != binding.profile_ref
            or status.profile_id != binding.profile_ref.profile_id
            or profile.draft.runtime_ref != authority.runtime_ref
            or runtime.ref != authority.runtime_ref
        ):
            _fail("configuration_mismatch")
        if (
            not status.enabled
            or not runtime.enabled
            or not runtime.installed
            or runtime.protocol_state.value != "ready"
            or runtime.auth_state.value == "failed"
        ):
            _fail("runtime_unavailable")
        for capability in requirements:
            support = runtime.capabilities.get(capability)
            if support is None or support.status.value != "supported":
                _fail("required_capability_unavailable")
        self._authorizer.require(
            authority_id, "workspace.execute", authority.execution_workspace
        )
        if authority.execution_workspace.artifact_directory is not None:
            self._authorizer.require(
                authority_id, "artifact.read", authority.execution_workspace
            )
        for tool in authority.tools:
            self._authorizer.require(authority_id, "tool.execute", tool)
        context = self._sources.context_items(
            authority_id,
            execution_id,
            reserved_bytes=len(objective.encode("utf-8"))
            + sum(len(item.encode("utf-8")) for item in instructions),
        )
        self._authorizer.require(authority_id, "run.start", binding.profile_ref)
        return RunStart(
            operation_id=link.start_operation_id,
            profile_ref=binding.profile_ref,
            grant_id=authority_id,
            policy=RunPolicy.BOUNDED,
            instructions=instructions,
            text=objective,
            context=context,
            tools=authority.tools,
            workspace=authority.execution_workspace,
            required_capabilities=requirements,
        )
