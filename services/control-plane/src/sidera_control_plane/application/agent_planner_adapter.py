"""The CP planning contract over one bounded public agents Run."""

import time
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from sidera_agents.contracts import (
    EventPage,
    RunEvent,
    RunSnapshot,
    RunStart,
    RunStop,
    StartLookup,
    StartReceipt,
    StopReceipt,
)
from sidera_agents.errors import AgentError

from sidera_control_plane.application.agent_sources import (
    PlanningContextSourceReader,
    _sections,
    _source_text,
    context_source_items,
)
from sidera_control_plane.application.agent_worker_adapter import (
    native_authority_pins,
    request_fingerprint,
    require_gateway_owner,
)
from sidera_control_plane.application.execution_authority import (
    PlanningActionTarget,
    PlanningAuthorityReader,
    require_active_planning,
)
from sidera_control_plane.application.planner_contract import (
    PlannerContractError,
    native_planning_instructions,
    parse_planner_result,
)
from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogEntry,
    PlannerExplicitFailure,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.planning import PlanningUsage
from sidera_control_plane.domain.planning_authority import (
    PlanningAgentLink,
    PlanningAuthority,
)


@dataclass
class _AssistantSegment:
    text: str = ""
    output_bytes: int = 0

    def consume(self, event: RunEvent) -> None:
        if event.kind == "tool":
            self.text = ""
            return
        if event.kind not in {"message", "progress.updated"}:
            return
        update_type, value = event.data.get("update_type"), event.data.get("text")
        if update_type not in {
            "agent_message_chunk",
            "agent_message",
        } or not isinstance(value, str):
            return
        self.output_bytes += len(value.encode("utf-8"))
        self.text = self.text + value if update_type == "agent_message_chunk" else value


class AgentPlannerAdapter:
    def __init__(
        self,
        *,
        services,
        uow_factory,
        workspace_provider,
        materializer,
        authorizer,
        clock=None,
        wait=None,
        poll_interval_seconds=1.0,
        owner_check=None,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("native planner poll interval must be positive")
        self._services, self._uow_factory = services, uow_factory
        self._workspace_provider, self._authorizer = workspace_provider, authorizer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_authorizer = PlanningAuthorityReader(
            uow_factory,
            principal=lambda: services.principal_id,
            authorizer=authorizer,
            clock=self._clock,
        )
        self._wait = wait or time.sleep
        self._poll_interval = poll_interval_seconds
        self._owner_check = owner_check
        self._sources = PlanningContextSourceReader(
            uow_factory,
            authorizer=self._run_authorizer,
            materializer=materializer,
            clock=self._clock,
        )

    def plan(self, planner_input, control):
        require_gateway_owner(self._owner_check)
        usage = PlanningUsage.unknown()
        if (
            control.operation_id != planner_input.operation_id
            or control.owner_id is None
            or control.lease_id is None
            or control.fencing_token is None
        ):
            return self._failure("permission_denied", False, usage)
        try:
            link, planner_input, context_bytes = self._prepare(planner_input, control)
            usage = replace(usage, context_bytes=context_bytes)
        except (AgentError, DomainError) as exc:
            return self.classify_failure(
                planner_input, getattr(exc, "code", "permission_denied"), usage=usage
            )
        assistant = _AssistantSegment()
        cursor, response_id, response_completed = 0, None, False
        busy_count = 0
        while True:
            usage = replace(
                usage,
                runtime_seconds=max(
                    0, int((self._clock() - control.started_at).total_seconds())
                ),
            )
            cancelled = control.cancel_requested or (
                control.cancel_check is not None and control.cancel_check()
            )
            expired = (
                control.deadline_at is not None and self._clock() >= control.deadline_at
            )
            if cancelled or expired:
                return self._halt(
                    planner_input,
                    link,
                    "native_cancelled" if cancelled else "native_deadline",
                    usage,
                )
            try:
                self._run_authorizer.require(
                    link.authority_id, "profile.read", link.profile_ref
                )
            except (AgentError, DomainError):
                return self._halt(planner_input, link, "native_authority_lost", usage)
            if link.status in {"pending", "waiting_capacity"}:
                with ExitStack() as admission:
                    try:
                        admission.enter_context(
                            (self._services.start_admission or nullcontext)()
                        )
                    except AgentError as exc:
                        return self._failure(exc.code, False, usage)
                    try:
                        link = self._save(link, status="starting")
                        self._run_authorizer.require(
                            link.authority_id, "run.start", link.profile_ref
                        )
                        self._run_authorizer.require(
                            link.authority_id,
                            "workspace.execute",
                            link.start_request.workspace,
                        )
                        require_gateway_owner(self._owner_check)
                        receipt = self._services.execution.start(link.start_request)
                        require_gateway_owner(self._owner_check)
                        link = self._record_receipt(link, receipt)
                    except Exception:  # noqa: BLE001 - Preserve uncertain public admission or commit.
                        require_gateway_owner(self._owner_check)
                        # A failed public call or receipt commit cannot prove non-admission.
                        link = self._original(planner_input)
                        if link is None:
                            return self._failure(
                                "native_admission_unknown", True, usage
                            )
                        if link.status != "starting":
                            return self.classify_failure(
                                planner_input, "native_admission_unknown", usage=usage
                            )
                        link = self._try_save(
                            link,
                            status="unknown",
                            reason_code="native_admission_unknown",
                        )
            elif link.run_id is None:
                try:
                    self._require_original(link, "run.read")
                    found = self._services.execution.get_start(
                        link.workspace_id,
                        link.start_operation_id,
                        grant_id=link.authority_id,
                    )
                    require_gateway_owner(self._owner_check)
                    if found.found and found.receipt is not None:
                        link = self._record_receipt(link, found.receipt)
                except Exception:  # noqa: BLE001 - Keep original uncertainty; no private diagnostics.
                    require_gateway_owner(self._owner_check)
            if link.status == "waiting_capacity":
                backoff = planner_input.profile_snapshot.retry_policy.backoff_seconds
                delay = (
                    backoff[min(busy_count, len(backoff) - 1)]
                    if backoff
                    else self._poll_interval
                )
                busy_count += 1
                self._pause(max(float(delay), self._poll_interval), control)
                continue
            if link.status == "rejected":
                return self._failure(
                    link.reason_code or "native_start_rejected", False, usage
                )
            if link.run_id is None:
                self._pause(self._poll_interval, control)
                continue
            try:
                self._require_original(link, "run.read")
                page = self._services.execution.list_events(
                    link.workspace_id, link.run_id, cursor, grant_id=link.authority_id
                )
                previous_cursor = cursor
                for event in page.events:
                    if (
                        event.run_id != link.run_id
                        or event.sequence != cursor + 1
                        or (
                            event.operation_id is not None
                            and event.operation_id != link.start_operation_id
                        )
                    ):
                        raise AgentError(
                            "configuration_mismatch",
                            "Native planning observation identity changed.",
                        )
                    cursor = event.sequence
                    if event.kind == "response.started":
                        if response_id is not None and event.response_id != response_id:
                            raise AgentError(
                                "configuration_mismatch",
                                "Native planning response identity changed.",
                            )
                        response_id = event.response_id
                    elif response_id is not None and event.response_id == response_id:
                        assistant.consume(event)
                        if (
                            assistant.output_bytes
                            > planner_input.profile_snapshot.budget.max_context_bytes
                        ):
                            return self._halt(
                                planner_input,
                                link,
                                "native_output_budget_exceeded",
                                usage,
                            )
                        if event.kind == "response.completed":
                            response_completed = True
                        elif event.kind == "usage":
                            usage = self._observed_usage(event, usage)
                if page.next_sequence != cursor:
                    raise AgentError(
                        "configuration_mismatch", "Native planning cursor changed."
                    )
                snapshot = self._services.execution.get_run(
                    link.workspace_id, link.run_id, grant_id=link.authority_id
                )
                self._check_snapshot(link, snapshot)
                terminal = snapshot.state.value in {"completed", "stopped", "failed"}
                closed = (
                    terminal
                    and not page.has_more
                    and cursor >= snapshot.last_event_sequence
                )
                link = self._save(
                    link,
                    event_cursor=max(link.event_cursor, cursor),
                    response_id=response_id,
                    status="closed" if closed else "running",
                    closed=closed,
                )
                if closed:
                    if not response_completed or snapshot.state.value != "completed":
                        return self._failure(
                            "invalid_planner_output", False, usage, retryable=True
                        )
                    try:
                        return parse_planner_result(
                            assistant.text,
                            planner_input,
                            "agent-run",
                            observed_usage=usage,
                        )
                    except (PlannerContractError, DomainError, TypeError, ValueError):
                        return self._failure(
                            "invalid_planner_output", False, usage, retryable=True
                        )
                if cursor > previous_cursor and (page.has_more or terminal):
                    continue
            except Exception:  # noqa: BLE001 - Observation or persistence cannot prove closure.
                return self._halt(
                    planner_input,
                    self._original(planner_input) or link,
                    "native_observation_unknown",
                    usage,
                )
            self._pause(self._poll_interval, control)

    def recover_original_result(self, planner_input):
        require_gateway_owner(self._owner_check)
        link = self._original(planner_input)
        if link is None or link.run_id is None:
            return None
        assistant = _AssistantSegment()
        cursor, response_id, completed = 0, None, False
        usage = PlanningUsage.unknown()
        # The saved cursor is observation progress, not a durable output buffer.
        for _ in range(100):
            self._require_original(link, "run.read")
            page = self._services.execution.list_events(
                link.workspace_id,
                link.run_id,
                cursor,
                limit=100,
                grant_id=link.authority_id,
            )
            require_gateway_owner(self._owner_check)
            for event in page.events:
                if (
                    event.run_id != link.run_id
                    or event.sequence != cursor + 1
                    or (
                        event.operation_id is not None
                        and event.operation_id != link.start_operation_id
                    )
                ):
                    raise AgentError(
                        "configuration_mismatch",
                        "Original planning event identity changed.",
                    )
                cursor = event.sequence
                if event.kind == "response.started":
                    if response_id is not None and event.response_id != response_id:
                        raise AgentError(
                            "configuration_mismatch",
                            "Original planning response changed.",
                        )
                    response_id = event.response_id
                elif response_id is not None and event.response_id == response_id:
                    assistant.consume(event)
                    if (
                        assistant.output_bytes
                        > planner_input.profile_snapshot.budget.max_context_bytes
                    ):
                        raise AgentError(
                            "native_output_budget_exceeded",
                            "Original planning output exceeds its saved budget.",
                        )
                    if event.kind == "response.completed":
                        completed = True
                    elif event.kind == "usage":
                        usage = self._observed_usage(event, usage)
            if page.next_sequence != cursor:
                raise AgentError(
                    "configuration_mismatch", "Original planning event cursor changed."
                )
            self._require_original(link, "run.read")
            snapshot = self._services.execution.get_run(
                link.workspace_id, link.run_id, grant_id=link.authority_id
            )
            require_gateway_owner(self._owner_check)
            self._check_snapshot(link, snapshot)
            terminal = snapshot.state.value in {"completed", "failed", "stopped"}
            closed = (
                terminal
                and not page.has_more
                and cursor >= snapshot.last_event_sequence
            )
            if closed:
                with self._uow_factory() as uow:
                    uow.planning_authorities.save_original_observation(
                        link,
                        event_cursor=max(cursor, link.event_cursor),
                        response_id=response_id,
                        closed=True,
                    )
                if completed and snapshot.state.value == "completed":
                    try:
                        return parse_planner_result(
                            assistant.text,
                            planner_input,
                            "agent-run",
                            observed_usage=usage,
                        )
                    except (PlannerContractError, DomainError, TypeError, ValueError):
                        return self._failure(
                            "invalid_planner_output", False, usage, retryable=True
                        )
                return self._failure(
                    "invalid_planner_output", False, usage, retryable=True
                )
            if not page.has_more:
                return None
        raise AgentError(
            "native_recovery_required",
            "Original planning output needs a larger bounded recovery scan.",
        )

    def _prepare(self, planner_input, control):
        require_gateway_owner(self._owner_check)
        if self._workspace_provider is None:
            raise AgentError(
                "planning_workspace_unavailable", "Planning workspace is unavailable."
            )
        with self._uow_factory() as uow:
            run = uow.planning_runs.get(
                planner_input.planning_run_id,
                planner_input.profile_snapshot.workspace_id,
            )
            request = uow.planning_requests.get(
                planner_input.planning_request_id,
                planner_input.profile_snapshot.workspace_id,
            )
            if (
                run is None
                or request is None
                or run.operation_id != planner_input.operation_id
                or run.profile_snapshot != planner_input.profile_snapshot
            ):
                raise AgentError("permission_denied", "Planning preparation denied.")
            require_active_planning(
                uow,
                run,
                request,
                now=self._clock(),
                owner_id=control.owner_id,
                lease_id=control.lease_id,
                fencing_token=control.fencing_token,
            )
            owner_id, lease_id, fence = (
                control.owner_id,
                control.lease_id,
                control.fencing_token,
            )
            existing = uow.planning_authorities.get_link(run.workspace_id, run.id)
            if existing is not None:
                authority = uow.planning_authorities.get(existing.authority_id)
                if (
                    authority is None
                    or (
                        authority.owner_id,
                        authority.lease_id,
                        authority.fencing_token,
                        authority.operation_id,
                        authority.profile_ref,
                    )
                    != (
                        owner_id,
                        lease_id,
                        fence,
                        run.operation_id,
                        run.profile_snapshot.planner_agent_ref,
                    )
                    or request_fingerprint(existing.start_request)
                    != existing.request_sha256
                ):
                    raise AgentError(
                        "permission_denied", "Original planning ownership changed."
                    )
                return existing, planner_input, None
            target = PlanningActionTarget(
                run.workspace_id,
                self._services.principal_id,
                request.id,
                run.id,
                run.profile_snapshot.profile_id,
                run.profile_snapshot.profile_version,
                run.profile_snapshot.planner_agent_ref,
                run.operation_id,
                run.profile_snapshot.planner_agent_ref,
            )
        if self._authorizer is None:
            raise AgentError("permission_denied", "Planning preparation denied.")
        self._authorizer.require(
            self._services.catalog_grant_id, "planning.prepare", target
        )
        self._authorizer.require(
            self._services.catalog_grant_id, "profile.read", target
        )
        pins = native_authority_pins(self._services, target.profile_ref)
        self._authorizer.require(
            self._services.catalog_grant_id,
            "runtime.read",
            replace(target, target=pins.runtime_ref),
        )
        runtime = self._services.runtimes.get_runtime(
            pins.runtime_ref, grant_id=self._services.catalog_grant_id
        )
        if (
            runtime.ref != pins.runtime_ref
            or not runtime.enabled
            or not runtime.installed
        ):
            raise AgentError(
                "planning_profile_unavailable", "Planning Runtime is unavailable."
            )
        catalog = replace(
            planner_input.capability_catalog,
            capabilities=tuple(
                CapabilityCatalogEntry(
                    name, f"{support.status.value}: {support.reason_code}"
                )
                for name, support in runtime.capabilities.items()
                if name in planner_input.profile_snapshot.allowed_capabilities
            ),
        )
        planner_input = replace(planner_input, capability_catalog=catalog)
        facts, assembly_pin = self._sources.prepare_facts(
            target.workspace_id,
            target.planning_run_id,
            principal_id=target.principal_id,
            grant_id=self._services.catalog_grant_id,
            upstream_authorizer=self._authorizer,
            owner_id=owner_id,
            lease_id=lease_id,
            fencing_token=fence,
        )
        items = context_source_items(facts)
        instructions = native_planning_instructions(planner_input)
        total = sum(
            len(_source_text(facts, section).encode("utf-8"))
            for section, _required in _sections(facts)
        )
        total += len(planner_input.brief.objective.encode("utf-8")) + sum(
            len(item.encode("utf-8")) for item in instructions
        )
        if (
            total > planner_input.profile_snapshot.budget.max_context_bytes
            or (total + 3) // 4 > planner_input.profile_snapshot.budget.max_tokens
        ):
            raise AgentError(
                "context_budget_exceeded", "Planning inputs exceed the selected budget."
            )
        with self._uow_factory() as uow:
            current = uow.planning_runs.get(target.planning_run_id, target.workspace_id)
            current_request = uow.planning_requests.get(
                target.planning_request_id, target.workspace_id
            )
            if current is None or current_request is None:
                raise AgentError("permission_denied", "Planning preparation denied.")
            require_active_planning(
                uow,
                current,
                current_request,
                now=self._clock(),
                owner_id=owner_id,
                lease_id=lease_id,
                fencing_token=fence,
            )
            self._authorizer.require(
                self._services.catalog_grant_id, "planning.prepare", target
            )
        workspace = self._workspace_provider.prepare(
            workspace_id=target.workspace_id,
            planning_run_id=target.planning_run_id,
            operation_id=target.operation_id,
        )
        authority = PlanningAuthority(
            id=uuid4(),
            workspace_id=target.workspace_id,
            planning_request_id=target.planning_request_id,
            planning_run_id=target.planning_run_id,
            planning_profile_id=target.planning_profile_id,
            planning_profile_version=target.planning_profile_version,
            operation_id=target.operation_id,
            principal_id=target.principal_id,
            upstream_grant_id=self._services.catalog_grant_id,
            profile_ref=target.profile_ref,
            runtime_ref=pins.runtime_ref,
            lease_id=lease_id,
            owner_id=owner_id,
            fencing_token=fence,
            execution_workspace=workspace,
            actions=(
                "profile.read",
                "runtime.read",
                "workspace.execute",
                "source.read",
                "run.start",
                "run.read",
                "run.stop",
                *pins.actions,
            ),
            sources=tuple(item.source_ref for item in items),
            credentials=pins.credentials,
            resources=pins.resources,
            tools=pins.tools,
        )
        start = RunStart(
            operation_id=target.operation_id,
            profile_ref=target.profile_ref,
            grant_id=authority.id,
            policy="bounded",
            instructions=instructions,
            text=planner_input.brief.objective,
            context=items,
            tools=pins.tools,
            workspace=workspace,
            required_capabilities=(),
        )
        link = PlanningAgentLink(
            target.workspace_id,
            target.planning_run_id,
            authority.id,
            target.operation_id,
            target.profile_ref,
            start,
            request_fingerprint(start),
            "pending",
        )
        with self._uow_factory() as uow:
            current = uow.planning_runs.get(target.planning_run_id, target.workspace_id)
            current_request = uow.planning_requests.get(
                target.planning_request_id, target.workspace_id
            )
            if current is None or current_request is None:
                raise AgentError("permission_denied", "Planning preparation denied.")
            require_active_planning(
                uow,
                current,
                current_request,
                now=self._clock(),
                owner_id=owner_id,
                lease_id=lease_id,
                fencing_token=fence,
            )
            self._authorizer.require(
                self._services.catalog_grant_id, "planning.prepare", target
            )
            self._sources.revalidate_prepared_facts(
                uow,
                current,
                current_request,
                facts,
                assembly_pin,
                principal_id=target.principal_id,
                grant_id=self._services.catalog_grant_id,
                upstream_authorizer=self._authorizer,
            )
            uow.planning_authorities.add(authority)
            uow.planning_authorities.add_link(link)
        return link, planner_input, total

    def _original(self, planner_input):
        with self._uow_factory() as uow:
            link = uow.planning_authorities.get_link(
                planner_input.profile_snapshot.workspace_id,
                planner_input.planning_run_id,
            )
            if link is not None and (
                link.start_operation_id != planner_input.operation_id
                or link.profile_ref != planner_input.profile_snapshot.planner_agent_ref
                or request_fingerprint(link.start_request) != link.request_sha256
            ):
                raise AgentError(
                    "permission_denied", "Original planning intention changed."
                )
            return link

    def _save(self, link, **changes):
        require_gateway_owner(self._owner_check)
        with self._uow_factory() as uow:
            authority = uow.planning_authorities.get(link.authority_id)
            run = uow.planning_runs.get(link.planning_run_id, link.workspace_id)
            lease = (
                None
                if authority is None
                else uow.planning_leases.get(authority.lease_id, link.workspace_id)
            )
            if (
                authority is None
                or run is None
                or lease is None
                or (
                    lease.id,
                    lease.workspace_id,
                    lease.planning_run_id,
                    lease.owner_id,
                    lease.fencing_token,
                    run.operation_id,
                )
                != (
                    authority.lease_id,
                    link.workspace_id,
                    link.planning_run_id,
                    authority.owner_id,
                    authority.fencing_token,
                    authority.operation_id,
                )
                or lease.status.value != "active"
                or lease.expires_at <= self._clock()
            ):
                raise AgentError(
                    "permission_denied", "Planning observation fence expired."
                )
            if request_fingerprint(link.start_request) != link.request_sha256:
                raise AgentError(
                    "permission_denied", "Original planning intention changed."
                )
            return uow.planning_authorities.save_link(link, **changes)

    def _try_save(self, link, **changes):
        try:
            return self._save(link, **changes)
        except Exception:  # noqa: BLE001 - Retain the last saved original intention.
            return link

    def _record_receipt(self, link, receipt):
        if receipt.operation_id != link.start_operation_id or (
            link.run_id is not None and receipt.run_id != link.run_id
        ):
            raise AgentError(
                "configuration_mismatch", "Native planning receipt changed."
            )
        status = {
            "accepted": "running",
            "busy": "waiting_capacity",
            "rejected": "rejected",
            "unknown": "unknown",
        }[receipt.status.value]
        return self._save(
            link,
            run_id=receipt.run_id,
            start_receipt=receipt,
            status=status,
            reason_code=receipt.reason_code,
        )

    def _require_original(self, link, action):
        require_gateway_owner(self._owner_check)
        with self._uow_factory() as uow:
            saved = uow.planning_authorities.get_link(
                link.workspace_id, link.planning_run_id
            )
            if saved is None or (
                saved.authority_id,
                saved.start_operation_id,
                saved.run_id,
                saved.request_sha256,
                saved.start_request,
                saved.profile_ref,
            ) != (
                link.authority_id,
                link.start_operation_id,
                link.run_id,
                link.request_sha256,
                link.start_request,
                link.profile_ref,
            ):
                raise AgentError(
                    "permission_denied", "Original planning Run is unavailable."
                )
            if request_fingerprint(link.start_request) != link.request_sha256:
                raise AgentError(
                    "permission_denied", "Original planning intention changed."
                )
        self._run_authorizer.require(link.authority_id, action, link.workspace_id)
        require_gateway_owner(self._owner_check)

    def _check_snapshot(self, link, snapshot):
        with self._uow_factory() as uow:
            authority = uow.planning_authorities.get(link.authority_id)
        if (
            not isinstance(snapshot, RunSnapshot)
            or authority is None
            or (
                snapshot.run_id,
                snapshot.workspace_id,
                snapshot.profile_ref,
                snapshot.policy.value,
                snapshot.effective_config.profile_ref,
                snapshot.effective_config.runtime_ref,
            )
            != (
                link.run_id,
                link.workspace_id,
                link.profile_ref,
                "bounded",
                link.profile_ref,
                authority.runtime_ref,
            )
        ):
            raise AgentError(
                "configuration_mismatch", "Native planning Run identity changed."
            )

    def drain_original_run(self, expected: PlanningAgentLink) -> bool:
        return self._drain_original(expected, reason_code=expected.reason_code)

    def _drain_original(self, link, *, reason_code):
        require_gateway_owner(self._owner_check)
        try:
            self._require_original(link, "run.read")
            with self._uow_factory() as uow:
                link = uow.planning_authorities.get_link(
                    link.workspace_id, link.planning_run_id
                )
            if link is None:
                return False
            if link.closed or link.status == "pending" or link.not_admitted:
                return True
            if link.run_id is None:
                found = self._services.execution.get_start(
                    link.workspace_id,
                    link.start_operation_id,
                    grant_id=link.authority_id,
                )
                require_gateway_owner(self._owner_check)
                if not isinstance(found, StartLookup) or not found.found:
                    return False
                receipt = found.receipt
                if (
                    not isinstance(receipt, StartReceipt)
                    or receipt.operation_id != link.start_operation_id
                ):
                    return False
                receipt = StartReceipt.model_validate(receipt.model_dump())
                self._require_original(link, "run.read")
                with self._uow_factory() as uow:
                    require_gateway_owner(self._owner_check)
                    link = uow.planning_authorities.save_link(
                        link,
                        run_id=receipt.run_id,
                        start_receipt=receipt,
                        status={
                            "accepted": "running",
                            "unknown": "unknown",
                            "busy": "waiting_capacity",
                            "rejected": "rejected",
                        }[receipt.status.value],
                        reason_code=receipt.reason_code,
                    )
                    require_gateway_owner(self._owner_check)
                if link.not_admitted:
                    return True
                if link.run_id is None:
                    return False
            snapshot = self._read_original_snapshot(link)
            if link.status != "cleanup_pending" and snapshot.state.value in {
                "completed",
                "failed",
            }:
                return self._consume_drain_events(link, snapshot, receipt=None)

            self._require_original(link, "run.stop")
            with self._uow_factory() as uow:
                require_gateway_owner(self._owner_check)
                current = uow.planning_authorities.get_link(
                    link.workspace_id,
                    link.planning_run_id,
                    for_update=True,
                )
                if current is None or (
                    current.authority_id,
                    current.start_operation_id,
                    current.run_id,
                    current.start_request,
                    current.request_sha256,
                ) != (
                    link.authority_id,
                    link.start_operation_id,
                    link.run_id,
                    link.start_request,
                    link.request_sha256,
                ):
                    raise AgentError(
                        "permission_denied", "Original planning intention changed."
                    )
                first_stop = not current.closed and current.status != "cleanup_pending"
                link = (
                    uow.planning_authorities.save_link(
                        current,
                        status="cleanup_pending",
                        reason_code=reason_code,
                    )
                    if first_stop
                    else current
                )
                require_gateway_owner(self._owner_check)
            if link.closed:
                return True
            self._require_original(link, "run.stop" if first_stop else "run.read")
            receipt = (
                self._services.execution.stop(
                    RunStop(
                        workspace_id=link.workspace_id,
                        run_id=link.run_id,
                        operation_id=link.start_operation_id,
                        grant_id=link.authority_id,
                    )
                )
                if first_stop
                else self._services.execution.get_stop(
                    link.workspace_id,
                    link.run_id,
                    link.start_operation_id,
                    grant_id=link.authority_id,
                )
            )
            require_gateway_owner(self._owner_check)
            snapshot = self._read_original_snapshot(link)
            if not self._stop_confirmed(link, receipt, snapshot):
                return False
            return self._consume_drain_events(link, snapshot, receipt=receipt)
        except (
            Exception
        ) as exc:  # Unconfirmed originals retain their durable Stop latch.
            if isinstance(exc, AgentError) and exc.code == "gateway_owner_lost":
                raise
            return False

    def _read_original_snapshot(self, link):
        self._require_original(link, "run.read")
        snapshot = self._services.execution.get_run(
            link.workspace_id,
            link.run_id,
            grant_id=link.authority_id,
        )
        require_gateway_owner(self._owner_check)
        self._check_snapshot(link, snapshot)
        return snapshot

    @staticmethod
    def _stop_confirmed(link, receipt, snapshot):
        return (
            isinstance(receipt, StopReceipt)
            and receipt.run_id == link.run_id
            and receipt.operation_id == link.start_operation_id
            and receipt.status.value == "stopped"
            and type(receipt.evidence_sequence) is int
            and receipt.evidence_sequence > 0
            and snapshot.state.value == "stopped"
            and not snapshot.input_open
            and snapshot.active_response_id is None
            and snapshot.last_event_sequence >= receipt.evidence_sequence
        )

    def _consume_drain_events(self, link, snapshot, *, receipt):
        cursor, response_id = link.event_cursor, link.response_id
        complete = False
        for _ in range(100):
            self._require_original(link, "run.read")
            page = self._services.execution.list_events(
                link.workspace_id,
                link.run_id,
                cursor,
                limit=100,
                grant_id=link.authority_id,
            )
            require_gateway_owner(self._owner_check)
            if not isinstance(page, EventPage):
                raise AgentError(
                    "configuration_mismatch", "Original planning event page changed."
                )
            for event in page.events:
                if (
                    event.run_id != link.run_id
                    or event.sequence != cursor + 1
                    or (
                        event.operation_id is not None
                        and event.operation_id != link.start_operation_id
                    )
                    or (
                        event.response_id is not None
                        and response_id is not None
                        and event.response_id != response_id
                    )
                ):
                    raise AgentError(
                        "configuration_mismatch",
                        "Original planning event identity changed.",
                    )
                if event.kind == "response.started":
                    response_id = event.response_id
                cursor = event.sequence
            if page.next_sequence != cursor:
                raise AgentError(
                    "configuration_mismatch", "Original planning event cursor changed."
                )
            if not page.has_more:
                current_snapshot = self._read_original_snapshot(link)
                complete = cursor >= current_snapshot.last_event_sequence and (
                    self._stop_confirmed(link, receipt, current_snapshot)
                    if receipt is not None
                    else (
                        current_snapshot.state.value in {"completed", "failed"}
                        and not current_snapshot.input_open
                        and current_snapshot.active_response_id is None
                    )
                )
                break
        self._require_original(link, "run.read")
        with self._uow_factory() as uow:
            require_gateway_owner(self._owner_check)
            uow.planning_authorities.save_original_observation(
                link,
                event_cursor=cursor,
                response_id=response_id,
                closed=complete,
            )
            require_gateway_owner(self._owner_check)
        return complete

    def _halt(self, planner_input, link, code, usage):
        require_gateway_owner(self._owner_check)
        if link.not_admitted or link.status == "pending":
            return self._failure(
                "native_capacity_timeout" if code == "native_deadline" else code,
                False,
                usage,
            )
        if link.closed:
            return self._failure(code, False, usage)
        if self._drain_original(link, reason_code=code):
            return self._failure(code, False, usage)
        with self._uow_factory() as uow:
            current = uow.planning_authorities.get_link(
                link.workspace_id, link.planning_run_id
            )
        return self._failure(
            "native_admission_unknown"
            if current is None or current.run_id is None
            else "native_cleanup_pending",
            True,
            usage,
        )

    def classify_failure(self, planner_input, code, *, usage=None):
        require_gateway_owner(self._owner_check)
        link = self._original(planner_input)
        unknown = (
            link is not None
            and not link.retry_boundary_closed
            and link.status != "pending"
        )
        return self._failure(
            "native_cleanup_pending"
            if unknown and link.run_id is not None
            else "native_admission_unknown"
            if unknown
            else code,
            unknown,
            usage or PlanningUsage.unknown(),
        )

    @staticmethod
    def _failure(code, unknown, usage, *, retryable=False):
        return PlannerExplicitFailure(
            code=code,
            safe_message="Native planning did not produce an eligible business result.",
            retryable=retryable and not unknown,
            unknown_outcome=unknown,
            provider_ref="agent-run",
            usage=usage,
        )

    @staticmethod
    def _observed_usage(event, usage):
        evidence = event.data.get("usage")
        if not isinstance(evidence, dict):
            return usage
        observed = {
            name: evidence[name]
            for name in ("input_tokens", "output_tokens", "cost_microunits")
            if type(evidence.get(name)) is int and evidence[name] >= 0
        }
        return replace(usage, **observed)

    def _pause(self, seconds, control):
        if control.deadline_at is not None:
            seconds = min(
                seconds, max(0.0, (control.deadline_at - self._clock()).total_seconds())
            )
        if seconds > 0:
            self._wait(seconds)

    def cancel(self, operation_id):
        # Cancellation is observed dynamically within the owned plan call.
        return None
