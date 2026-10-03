"""The CP planning contract over one bounded public agents Run."""

import time
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from sidera_agents.contracts import RunStart, RunStop
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
)
from sidera_control_plane.application.execution_authority import (
    PlanningActionTarget,
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
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("native planner poll interval must be positive")
        self._services, self._uow_factory = services, uow_factory
        self._workspace_provider, self._authorizer = workspace_provider, authorizer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wait = wait or time.sleep
        self._poll_interval = poll_interval_seconds
        self._sources = PlanningContextSourceReader(
            uow_factory,
            authorizer=services.authorizer,
            materializer=materializer,
            clock=self._clock,
        )

    def plan(self, planner_input, control):
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
        text, cursor, response_id, response_completed = "", 0, None, False
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
                self._services.authorizer.require(
                    link.authority_id, "profile.read", link.profile_ref
                )
            except (AgentError, DomainError):
                return self._halt(planner_input, link, "native_authority_lost", usage)
            if link.status in {"pending", "waiting_capacity"}:
                try:
                    link = self._save(link, status="starting")
                    self._services.authorizer.require(
                        link.authority_id, "run.start", link.profile_ref
                    )
                    self._services.authorizer.require(
                        link.authority_id,
                        "workspace.execute",
                        link.start_request.workspace,
                    )
                    receipt = self._services.execution.start(link.start_request)
                    link = self._record_receipt(link, receipt)
                except Exception:  # noqa: BLE001 - Preserve uncertain public admission or commit.
                    # A failed public call or receipt commit cannot prove non-admission.
                    link = self._original(planner_input)
                    if link is None:
                        return self._failure("native_admission_unknown", True, usage)
                    if link.status != "starting":
                        return self.classify_failure(
                            planner_input, "native_admission_unknown", usage=usage
                        )
                    link = self._try_save(
                        link, status="unknown", reason_code="native_admission_unknown"
                    )
            elif link.run_id is None:
                try:
                    self._require_original(link, "run.read")
                    found = self._services.execution.get_start(
                        link.workspace_id,
                        link.start_operation_id,
                        grant_id=link.authority_id,
                    )
                    if found.found and found.receipt is not None:
                        link = self._record_receipt(link, found.receipt)
                except Exception:  # noqa: BLE001, S110 - Keep original uncertainty; no private diagnostics.
                    pass
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
                        if event.kind == "progress.updated":
                            update_type = event.data.get("update_type")
                            value = event.data.get("text")
                            if update_type in {
                                "agent_message_chunk",
                                "agent_message",
                            } and isinstance(value, str):
                                text = (
                                    text + value
                                    if update_type == "agent_message_chunk"
                                    else value
                                )
                                if (
                                    len(text.encode("utf-8"))
                                    > planner_input.profile_snapshot.budget.max_context_bytes
                                ):
                                    return self._halt(
                                        planner_input,
                                        link,
                                        "native_output_budget_exceeded",
                                        usage,
                                    )
                        elif event.kind == "response.completed":
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
                            text, planner_input, "agent-run", observed_usage=usage
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

    def _prepare(self, planner_input, control):
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
        with self._uow_factory() as uow:
            saved = uow.planning_authorities.get_link(
                link.workspace_id, link.planning_run_id
            )
            if saved is None or (
                saved.authority_id,
                saved.start_operation_id,
                saved.run_id,
                saved.request_sha256,
            ) != (
                link.authority_id,
                link.start_operation_id,
                link.run_id,
                link.request_sha256,
            ):
                raise AgentError(
                    "permission_denied", "Original planning Run is unavailable."
                )
        self._services.authorizer.require(link.authority_id, action, link.workspace_id)

    def _check_snapshot(self, link, snapshot):
        with self._uow_factory() as uow:
            authority = uow.planning_authorities.get(link.authority_id)
        if authority is None or (
            snapshot.run_id,
            snapshot.workspace_id,
            snapshot.profile_ref,
            snapshot.policy.value,
            snapshot.effective_config.profile_ref,
            snapshot.effective_config.runtime_ref,
        ) != (
            link.run_id,
            link.workspace_id,
            link.profile_ref,
            "bounded",
            link.profile_ref,
            authority.runtime_ref,
        ):
            raise AgentError(
                "configuration_mismatch", "Native planning Run identity changed."
            )

    def _halt(self, planner_input, link, code, usage):
        if link.not_admitted or link.status == "pending":
            return self._failure(
                "native_capacity_timeout" if code == "native_deadline" else code,
                False,
                usage,
            )
        if link.closed:
            return self._failure(code, False, usage)
        if link.run_id is None:
            self._try_save(link, status="unknown", reason_code=code)
            return self._failure("native_admission_unknown", True, usage)
        link = self._try_save(link, status="cleanup_pending", reason_code=code)
        try:
            self._require_original(link, "run.stop")
            receipt = self._services.execution.stop(
                RunStop(
                    workspace_id=link.workspace_id,
                    run_id=link.run_id,
                    operation_id=link.start_operation_id,
                    grant_id=link.authority_id,
                )
            )
            if (
                receipt.run_id != link.run_id
                or receipt.operation_id != link.start_operation_id
            ):
                raise AgentError(
                    "configuration_mismatch", "Native planning Stop identity changed."
                )
            self._require_original(link, "run.read")
            snapshot = self._services.execution.get_run(
                link.workspace_id, link.run_id, grant_id=link.authority_id
            )
            self._check_snapshot(link, snapshot)
            if snapshot.state.value in {"completed", "stopped", "failed"}:
                self._save(link, status="closed", closed=True)
                return self._failure(code, False, usage)
        except Exception:  # noqa: BLE001, S110 - Unconfirmed Stop must retain cleanup uncertainty.
            pass
        return self._failure("native_cleanup_pending", True, usage)

    def classify_failure(self, planner_input, code, *, usage=None):
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
