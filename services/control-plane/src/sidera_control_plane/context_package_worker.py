from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_agents.contracts import ProfileRef

from sidera_control_plane.adapters.knowledge_http import (
    KnowledgeAdapterError,
    KnowledgeHttpAdapter,
)
from sidera_control_plane.application.agent_worker_adapter import require_gateway_owner
from sidera_control_plane.application.code_result_delivery import resolve_code_inputs
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_assembly import (
    ContextAssembler,
    ContextAssemblyError,
)
from sidera_control_plane.application.context_requests import (
    ExecutionContextAuthorizationError,
    resolve_execution_context_authorization,
    validate_execution_context_selection,
)
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    KnowledgeCatalogPort,
)
from sidera_control_plane.application.revision_planning_context import (
    encode_native_revision_facts,
    load_revision_planning_context,
)
from sidera_control_plane.application.scheduling_blocks import SchedulingBlockService
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextPackageManifest,
    ContextPurpose,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.handoff_evidence import HandoffContextBundle
from sidera_control_plane.domain.input_selections import CollaborationInputSelectionV1
from sidera_control_plane.domain.planning import PlanningRequestKind, PlanningRunStatus
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.domain.scheduling import (
    SchedulingBlockReason,
    SchedulingIssue,
)
from sidera_control_plane.domain.work_context import WorkContextRevision, WorkReference
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

logger = logging.getLogger(__name__)

_RETRY_BACKOFF_SECONDS = (5, 15, 45, 135, 300)
_PACKAGE_NAMESPACE = NAMESPACE_URL
_PACKAGE_V2_NAMESPACE = uuid5(_PACKAGE_NAMESPACE, "sidera:context-package:v2")
_PACKAGE_V3_NAMESPACE = uuid5(_PACKAGE_NAMESPACE, "sidera:context-package:v3")


@dataclass(frozen=True, slots=True)
class ContextPackageWorkerResult:
    claimed: int
    ready: int
    retried: int
    blocked: int
    failed: int


@dataclass(frozen=True, slots=True)
class _AssemblyInput:
    work_context_revision: WorkContextRevision
    control_plane_fact_snapshot: Mapping[str, object]
    artifact_refs: tuple[WorkReference, ...]
    evidence_refs: tuple[WorkReference, ...]
    input_selection: CollaborationInputSelectionV1 | None = None
    code_input_manifest: CodeInputManifestV1 | None = None


class ContextPackageWorker:
    """Lease assembly requests, call Knowledge outside transactions, then commit."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        catalog: KnowledgeCatalogPort,
        owner_id: str = "context-worker-local",
        lease_seconds: int = 60,
        max_attempts: int = 5,
        clock: Any = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if lease_seconds < 1 or not 1 <= max_attempts <= 5:
            raise ValueError("context worker limits are invalid")
        self._uow_factory = uow_factory
        self._catalog = catalog
        self._assembler = ContextAssembler(catalog)
        self._owner_id = owner_id.strip()
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._scheduling_blocks = SchedulingBlockService()

    def run_once(self, limit: int = 10) -> ContextPackageWorkerResult:
        if limit < 1:
            raise ValueError("context worker limit must be positive")
        claimed = self._claim(limit)
        ready = 0
        retried = 0
        blocked = 0
        failed = 0
        for request in claimed:
            try:
                assembly_input = self._load_input(request)
                manifest = self._assemble(request, assembly_input)
                self._complete(request, manifest)
                ready += 1
            except Exception as exc:  # noqa: BLE001
                try:
                    disposition = self._handle_failure(request, exc)
                except Exception:
                    failed += 1
                    logger.exception(
                        "context_assembly_failure_persistence_error",
                        extra={
                            "workspace_id": str(request.workspace_id),
                            "assembly_request_id": str(request.request_id),
                        },
                    )
                else:
                    retried += int(disposition == "retry")
                    blocked += int(disposition == "blocked")
        return ContextPackageWorkerResult(
            claimed=len(claimed),
            ready=ready,
            retried=retried,
            blocked=blocked,
            failed=failed,
        )

    def _claim(self, limit: int) -> tuple[ContextAssemblyRequest, ...]:
        now = self._clock()
        with self._uow_factory() as uow:
            return uow.context_assemblies.claim_next(
                workspace_id=None,
                owner=self._owner_id,
                now=now,
                lease_ttl=timedelta(seconds=self._lease_seconds),
                limit=limit,
            )

    def _load_input(self, request: ContextAssemblyRequest) -> _AssemblyInput:
        with self._uow_factory() as uow:
            revision = uow.work_contexts.get_revision(
                request.work_context_revision_id,
                request.workspace_id,
            )
            if revision is None:
                raise ContextAssemblyError(
                    "context_reference_not_found",
                    "WorkContext revision is no longer available",
                )
            input_selection = None
            code_input_manifest = None
            if request.purpose is ContextPurpose.PLANNING_RUN:
                facts = self._planning_facts(uow, request)
            else:
                input_selection = self._execution_input_selection(uow, request)
                if (
                    request.manifest_schema_version == 3
                    and request.verification_manifest is not None
                    and "verification_request_id" not in request.budget
                ):
                    raise ContextAssemblyError(
                        "context_verification_request_missing",
                        "v3 verification context has no verification request reference",
                    )
                if request.code_input_manifest is not None:
                    try:
                        code_input_manifest = resolve_code_inputs(
                            uow,
                            request.code_input_manifest,
                        )
                    except ResultError as exc:
                        raise ContextAssemblyError(exc.code, str(exc)) from exc
                facts = self._execution_facts(uow, request)
            return _AssemblyInput(
                work_context_revision=revision,
                control_plane_fact_snapshot=facts,
                artifact_refs=revision.snapshot.artifact_refs,
                evidence_refs=revision.snapshot.evidence_refs,
                input_selection=input_selection,
                code_input_manifest=code_input_manifest,
            )

    def _execution_input_selection(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ContextAssemblyRequest,
    ) -> CollaborationInputSelectionV1 | None:
        if request.manifest_schema_version == 1:
            return None
        if request.manifest_schema_version not in {2, 3}:
            raise ContextAssemblyError(
                "context_manifest_schema_unsupported",
                "execution context manifest schema is unsupported",
            )
        if request.manifest_schema_version == 3 and request.input_selection_id is None:
            if request.code_input_manifest is not None:
                execution_request_id = _budget_uuid(request, "execution_request_id")
                execution_request = uow.execution_requests.get(
                    execution_request_id, request.workspace_id
                )
                if (
                    execution_request is None
                    or execution_request.result_derivation_kind != "rework"
                    or execution_request.purpose == "reintegrate"
                ):
                    raise ContextAssemblyError(
                        "context_input_selection_missing",
                        "v3 code execution context has no authorized rework source",
                    )
                return None
            return None
        if request.input_selection_id is None:
            raise ContextAssemblyError(
                "context_input_selection_missing",
                "execution context has no input Selection reference",
            )
        selection = uow.input_selections.get(
            request.workspace_id,
            request.input_selection_id,
        )
        if selection is None:
            raise ContextAssemblyError(
                "context_input_selection_missing",
                "authorized input Selection is no longer available",
            )
        execution_request_id = _budget_uuid(request, "execution_request_id")
        execution_request = uow.execution_requests.get(
            execution_request_id,
            request.workspace_id,
        )
        execution = uow.executions.get(request.consumer_id, request.workspace_id)
        release = uow.collaboration_ledger.get_release_for_request(
            request.workspace_id,
            execution_request_id,
        )
        if execution_request is None or execution is None or release is None:
            raise ContextAssemblyError(
                "context_input_selection_not_released",
                "execution context is missing its released input Selection",
            )
        try:
            authorization = resolve_execution_context_authorization(uow, execution)
        except ExecutionContextAuthorizationError as exc:
            raise ContextAssemblyError(
                "context_input_selection_scope_invalid",
                str(exc),
            ) from exc
        if authorization is None or authorization.request.id != execution_request_id:
            raise ContextAssemblyError(
                "context_input_selection_scope_invalid",
                "execution context Selection is not authorized for this consumer",
            )
        try:
            _validate_inheritance_budget(
                uow,
                request,
                execution=execution,
                authorization=authorization,
            )
        except ExecutionContextAuthorizationError as exc:
            raise ContextAssemblyError(
                "context_input_selection_scope_invalid",
                str(exc),
            ) from exc
        try:
            validate_execution_context_selection(
                execution_request=execution_request,
                release=release,
                selection=selection,
            )
        except ValueError as exc:
            raise ContextAssemblyError(
                "context_input_selection_scope_invalid",
                str(exc),
            ) from exc
        return selection

    def _planning_facts(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ContextAssemblyRequest,
    ) -> Mapping[str, object]:
        run = uow.planning_runs.get(request.consumer_id, request.workspace_id)
        if run is None:
            raise ContextAssemblyError(
                "context_consumer_stale", "PlanningRun is no longer available"
            )
        planning_request = uow.planning_requests.get(
            run.planning_request_id, request.workspace_id
        )
        if planning_request is None:
            raise ContextAssemblyError(
                "context_consumer_stale", "PlanningRequest is no longer available"
            )
        brief = next(
            (
                item
                for item in planning_request.briefs
                if item.revision == run.brief_revision
            ),
            None,
        )
        if brief is None:
            raise ContextAssemblyError(
                "context_consumer_stale",
                "Planning brief revision is no longer available",
            )
        profile = run.profile_snapshot
        facts = {
            "planning_request": {
                "id": str(planning_request.id),
                "status": planning_request.status.value,
                "version": planning_request.version,
                "current_brief_revision": planning_request.current_brief_revision,
            },
            "planning_run": {
                "id": str(run.id),
                "status": run.status.value,
                "attempt": run.attempt,
                "brief_revision": run.brief_revision,
                "operation_id": str(run.operation_id),
            },
            "brief": {
                "planning_request_id": str(brief.planning_request_id),
                "revision": brief.revision,
                "objective": brief.objective,
                "constraints": brief.constraints,
                "acceptance_criteria": brief.acceptance_criteria,
                "context_refs": brief.context_refs,
                "requested_outputs": brief.requested_outputs,
                "user_feedback": brief.user_feedback,
            },
            "planning_profile": {
                "profile_id": str(profile.profile_id),
                "profile_key": profile.profile_key,
                "profile_version": profile.profile_version,
                "planner_module_instance_id": str(profile.planner_module_instance_id),
                "planner_module_config_version": profile.planner_module_config_version,
                "planner_interface_version": profile.planner_interface_version,
                "prompt_policy_version": profile.prompt_policy_version,
                "proposal_schema_version": profile.proposal_schema_version,
            },
            "planner_module": {
                "module_instance_id": str(profile.planner_module_instance_id),
                "config_version": profile.planner_module_config_version,
                "interface_version": profile.planner_interface_version,
            },
        }
        if profile.execution_mode == "native_profile":
            if (
                not isinstance(profile.planner_agent_ref, ProfileRef)
                or profile.planner_agent_ref.workspace_id != request.workspace_id
            ):
                raise ContextAssemblyError(
                    "context_consumer_stale", "Native planning Profile pin is invalid"
                )
            pin = profile.planner_agent_ref.model_dump(mode="json")
            del facts["planning_profile"]["planner_module_instance_id"]
            del facts["planning_profile"]["planner_module_config_version"]
            del facts["planner_module"]
            facts["planning_profile"].update(
                execution_mode="native_profile", planner_agent_ref=pin
            )
            facts["planner_agent"] = pin
            if planning_request.kind is PlanningRequestKind.REVISION:
                facts["revision_base"] = encode_native_revision_facts(
                    load_revision_planning_context(
                        uow, planning_request, context_package=None
                    )
                )
        return facts

    def _execution_facts(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ContextAssemblyRequest,
    ) -> Mapping[str, object]:
        request_budget_key = (
            "verification_request_id"
            if request.verification_manifest is not None
            else "execution_request_id"
        )
        execution_request_id = _budget_uuid(request, request_budget_key)
        execution_request = uow.execution_requests.get(
            execution_request_id, request.workspace_id
        )
        execution = uow.executions.get(request.consumer_id, request.workspace_id)
        if request.worker_item_id is None:
            raise ContextAssemblyError(
                "context_consumer_stale", "Execution assembly has no WorkerItem"
            )
        worker_item = uow.worker_items.get(request.worker_item_id, request.workspace_id)
        planning_request_id = (
            execution_request.planning_request_id
            if execution_request is not None
            else None
        )
        planning_request = (
            None
            if planning_request_id is None
            else uow.planning_requests.get(planning_request_id, request.workspace_id)
        )
        if execution_request is None or execution is None or worker_item is None:
            raise ContextAssemblyError(
                "context_consumer_stale",
                "Execution context consumer is no longer available",
            )
        if worker_item.context_version != request.context_version:
            raise ContextAssemblyError(
                "context_consumer_stale", "WorkerItem context version changed"
            )
        dependencies = uow.execution_requests.list_dependencies(
            execution_request.id, request.workspace_id
        )
        facts: dict[str, object] = {
            "planning_request": {
                "id": str(planning_request.id) if planning_request else None,
                "status": planning_request.status.value if planning_request else None,
                "version": planning_request.version if planning_request else None,
            },
            "execution_request": {
                "id": str(execution_request.id),
                "status": execution_request.status.value,
                "worker_item_id": str(execution_request.worker_item_id),
                "proposal_revision_id": str(execution_request.proposal_revision_id),
                "plan_id": str(execution_request.plan_snapshot_id),
                "plan_generation": execution_request.plan_generation,
                "gate_reason": execution_request.gate_reason,
                "expected_outputs": execution_request.expected_outputs,
            },
            "execution": {
                "id": str(execution.id),
                "status": execution.status.value,
                "lineage_id": str(execution.lineage_id),
                "attempt": execution.attempt,
                "task_id": str(execution.task_id),
            },
            "worker_item": {
                "id": str(worker_item.id),
                "context_version": worker_item.context_version,
                "acknowledged_context_version": worker_item.acknowledged_context_version,
            },
            "proposal_revision": {
                "id": str(execution_request.proposal_revision_id),
            },
            "plan": {
                "id": str(execution_request.plan_snapshot_id),
                "generation": execution_request.plan_generation,
            },
            "dependency_state": tuple(
                {
                    "upstream_request_id": str(item.upstream_request_id),
                    "enforcement": item.enforcement.value,
                    "required_artifact_refs": item.required_artifact_refs,
                    "required_evidence_refs": item.required_evidence_refs,
                }
                for item in dependencies
            ),
        }
        inheritance = request.budget.get("execution_context_inheritance")
        if isinstance(inheritance, Mapping):
            facts["execution_context_inheritance"] = {
                key: inheritance[key]
                for key in (
                    "kind",
                    "authorization_request_id",
                    "source_execution_id",
                    "source_session_id",
                    "source_lineage_id",
                )
                if key in inheritance
            }
        handoff_facts = self._handoff_facts(uow, request)
        if handoff_facts is not None:
            facts["handoff"] = handoff_facts
        return facts

    @staticmethod
    def _handoff_facts(
        uow: ControlPlaneUnitOfWork,
        request: ContextAssemblyRequest,
    ) -> Mapping[str, object] | None:
        bundle_id_value = request.budget.get("handoff_context_bundle_id")
        if bundle_id_value is None:
            return None
        try:
            bundle_id = (
                bundle_id_value
                if isinstance(bundle_id_value, UUID)
                else UUID(str(bundle_id_value))
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise ContextAssemblyError(
                "context_consumer_stale",
                "handoff assembly budget has an invalid Bundle ID",
            ) from exc
        bundle_hash = request.budget.get("handoff_context_bundle_hash")
        if not isinstance(bundle_hash, str) or not bundle_hash.strip():
            raise ContextAssemblyError(
                "context_consumer_stale",
                "handoff assembly budget is missing the Bundle hash",
            )
        bundles = getattr(uow, "handoff_context_bundles", None)
        if bundles is None:
            raise ContextAssemblyError(
                "context_consumer_stale",
                "handoff Bundle repository is unavailable",
            )
        bundle = bundles.get(bundle_id, request.workspace_id)
        if bundle is None:
            raise ContextAssemblyError(
                "context_consumer_stale",
                "handoff context Bundle is no longer available",
            )
        if (
            bundle.bundle_hash != bundle_hash
            or bundle.workspace_id != request.workspace_id
            or bundle.target_execution_id != request.consumer_id
            or bundle.worker_item_id != request.worker_item_id
            or bundle.target_context_version != request.context_version
        ):
            raise ContextAssemblyError(
                "context_consumer_stale",
                "handoff context Bundle does not match the target consumer",
            )
        return _safe_handoff_facts(bundle)

    def _assemble(
        self,
        request: ContextAssemblyRequest,
        assembly_input: _AssemblyInput,
    ) -> ContextPackageManifest:
        if request.manifest_schema_version == 1:
            package_id = uuid5(
                _PACKAGE_NAMESPACE,
                f"sidera:context-package:{request.request_id}:v1",
            )
        elif request.manifest_schema_version == 2:
            if request.input_selection_hash is None:
                raise ContextAssemblyError(
                    "context_input_selection_missing",
                    "v2 execution context has no Selection hash",
                )
            package_id = uuid5(
                _PACKAGE_V2_NAMESPACE,
                "sidera:context-package:"
                f"{request.consumer_type.value}:{request.consumer_id}:"
                f"worker:{request.worker_item_id}:context:{request.context_version}:"
                f"revision:{request.work_context_revision_id}:"
                f"selection:{request.input_selection_hash}:v2",
            )
        elif request.manifest_schema_version == 3:
            input_identity = (
                assembly_input.code_input_manifest.input_hash
                if assembly_input.code_input_manifest is not None
                else request.verification_manifest.verification_basis_hash
                if request.verification_manifest is not None
                else None
            )
            if input_identity is None:
                raise ContextAssemblyError(
                    "context_manifest_schema_unsupported",
                    "v3 execution context has no input identity",
                )
            package_id = uuid5(
                _PACKAGE_V3_NAMESPACE,
                "sidera:context-package:"
                f"{request.consumer_type.value}:{request.consumer_id}:"
                f"worker:{request.worker_item_id}:context:{request.context_version}:"
                f"revision:{request.work_context_revision_id}:"
                f"input:{input_identity}:v3",
            )
        else:
            raise ContextAssemblyError(
                "context_manifest_schema_unsupported",
                "execution context manifest schema is unsupported",
            )
        assembly_request = request
        if assembly_input.code_input_manifest is not None:
            assembly_request = replace(
                request,
                code_input_manifest=assembly_input.code_input_manifest,
            )
        return self._assembler.assemble(
            assembly_request,
            work_context_revision=assembly_input.work_context_revision,
            control_plane_fact_snapshot=assembly_input.control_plane_fact_snapshot,
            artifact_refs=assembly_input.artifact_refs,
            evidence_refs=assembly_input.evidence_refs,
            package_id=package_id,
            created_at=request.created_at,
            correlation_id=request.request_id,
            caller_request_id=request.request_id,
            input_selection=assembly_input.input_selection,
        )

    def _complete(
        self, request: ContextAssemblyRequest, manifest: ContextPackageManifest
    ) -> None:
        with self._uow_factory() as uow:
            envelope = _system_command(
                request,
                action="complete",
                actor_id=self._owner_id,
                target_type="context_assembly_request",
                target_id=request.request_id,
                expected_version=request.version,
                payload={
                    "context_assembly_request_id": str(request.request_id),
                    "package_id": str(manifest.package_id),
                },
            )
            uow.commands.record_applied(
                envelope,
                hash_request(envelope),
                CommandResult(
                    command_id=envelope.id,
                    aggregate_id=manifest.package_id,
                    aggregate_version=1,
                    event_ids=(),
                    data={"context_assembly_request_id": str(request.request_id)},
                ),
            )
            uow.context_assemblies.complete_with_package(
                request,
                manifest,
                owner=self._owner_id,
                expected_version=request.version,
                actor_type="system",
                actor_id=self._owner_id,
                causation_id=envelope.id,
                correlation_id=request.request_id,
            )

    def _handle_failure(
        self,
        request: ContextAssemblyRequest,
        error: Exception,
    ) -> str:
        code = _error_code(error)
        now = self._clock()
        temporary = bool(getattr(error, "temporary", False)) or (
            isinstance(error, KnowledgeAdapterError) and error.temporary
        )
        if temporary and request.attempt < self._max_attempts:
            delay = _RETRY_BACKOFF_SECONDS[
                min(request.attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
            ]
            with self._uow_factory() as uow:
                uow.context_assemblies.save_retry(
                    request,
                    owner=self._owner_id,
                    expected_version=request.version,
                    next_attempt_at=now + timedelta(seconds=delay),
                    error_code=code,
                    now=now,
                )
            return "retry"
        with self._uow_factory() as uow:
            uow.context_assemblies.save_blocked(
                request,
                owner=self._owner_id,
                expected_version=request.version,
                error_code=code,
                now=now,
            )
            if request.purpose is ContextPurpose.PLANNING_RUN:
                self._block_planning_run(uow, request, code=code, now=now)
            else:
                self._block_execution(uow, request, code=code, now=now)
        return "blocked"

    def _block_planning_run(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ContextAssemblyRequest,
        *,
        code: str,
        now: datetime,
    ) -> None:
        run = uow.planning_runs.get(request.consumer_id, request.workspace_id)
        if run is None:
            return
        planning_request = uow.planning_requests.get(
            run.planning_request_id, request.workspace_id, for_update=True
        )
        if planning_request is None:
            return
        run = planning_request.run(request.consumer_id)
        if run.status.value != PlanningRunStatus.QUEUED.value:
            return
        previous_request_version = planning_request.version
        previous_run_version = run.version
        planning_request.block_queued_run(
            run_id=run.id,
            error_code=code,
            error_message="context assembly was blocked",
            now=now,
        )
        uow.planning_runs.save(run, previous_run_version)
        uow.planning_requests.save(planning_request, previous_request_version)
        envelope = _system_command(
            request,
            action="block-planning-run",
            actor_id=self._owner_id,
            target_type="planning_run",
            target_id=run.id,
            expected_version=previous_run_version,
            payload={
                "planning_run_id": str(run.id),
                "context_assembly_request_id": str(request.request_id),
                "error_code": code,
            },
        )
        event = EventData(
            id=uuid5(
                _PACKAGE_NAMESPACE,
                f"context-block:{request.request_id}:{request.version}",
            ),
            workspace_id=request.workspace_id,
            aggregate_type="planning_run",
            aggregate_id=run.id,
            aggregate_version=run.version,
            event_type="PlanningRunContextBlocked",
            payload={
                "planning_run_id": str(run.id),
                "context_assembly_request_id": str(request.request_id),
                "error_code": code,
                "status": run.status.value,
            },
            actor_type="system",
            actor_id=self._owner_id,
            causation_id=envelope.id,
            correlation_id=request.request_id,
        )
        uow.commands.record_applied(
            envelope,
            hash_request(envelope),
            CommandResult(
                command_id=envelope.id,
                aggregate_id=run.id,
                aggregate_version=run.version,
                event_ids=(event.id,),
                data={"context_assembly_request_id": str(request.request_id)},
            ),
        )
        uow.events.append_with_outbox(event)

    def _block_execution(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ContextAssemblyRequest,
        *,
        code: str,
        now: datetime,
    ) -> None:
        execution = uow.executions.get(
            request.consumer_id, request.workspace_id, for_update=True
        )
        if execution is None:
            return
        envelope = CommandEnvelope(
            id=uuid5(
                _PACKAGE_NAMESPACE,
                f"context-block-command:{request.request_id}:{request.version}",
            ),
            workspace_id=execution.workspace_id,
            command_type="ContextAssemblyBlocked",
            target_type="execution",
            actor_type="system",
            actor_id=self._owner_id,
            target_id=execution.id,
            payload={
                "context_assembly_request_id": str(request.request_id),
                "error_code": code,
            },
            expected_version=execution.version,
            idempotency_key=f"context-block:{request.request_id}:v{request.version}",
            correlation_id=request.request_id,
        )
        events = self._scheduling_blocks.record(
            uow,
            execution=execution,
            issue=SchedulingIssue(
                reason_code=SchedulingBlockReason.CONTEXT_ASSEMBLY_BLOCKED,
                reason_details={
                    "reason": code,
                    "context_assembly_request_id": str(request.request_id),
                },
                requires_operator=True,
            ),
            envelope=envelope,
            now=now,
        )
        uow.commands.record_applied(
            envelope,
            hash_request(envelope),
            CommandResult(
                command_id=envelope.id,
                aggregate_id=execution.id,
                aggregate_version=execution.version,
                event_ids=tuple(event.id for event in events),
                data={"context_assembly_request_id": str(request.request_id)},
            ),
        )
        for event in events:
            uow.events.append_with_outbox(event)


def _validate_inheritance_budget(
    uow: ControlPlaneUnitOfWork,
    request: ContextAssemblyRequest,
    *,
    execution: Any,
    authorization: Any,
) -> None:
    raw = request.budget.get("execution_context_inheritance")
    if raw is None:
        if authorization.inherited:
            raise ExecutionContextAuthorizationError(
                "inherited execution context is missing its source reference"
            )
        return
    if not isinstance(raw, Mapping):
        raise ExecutionContextAuthorizationError(
            "execution context inheritance facts are invalid"
        )
    if str(raw.get("authorization_request_id")) != str(authorization.request.id):
        raise ExecutionContextAuthorizationError(
            "execution context inheritance request reference is stale"
        )
    if str(raw.get("source_lineage_id")) != str(authorization.source_lineage_id):
        raise ExecutionContextAuthorizationError(
            "execution context inheritance lineage reference is stale"
        )
    kind = raw.get("kind")
    source_execution_value = raw.get("source_execution_id")
    if not isinstance(kind, str) or not isinstance(source_execution_value, str):
        raise ExecutionContextAuthorizationError(
            "execution context inheritance source reference is incomplete"
        )
    try:
        source_execution_id = UUID(source_execution_value)
    except ValueError as exc:
        raise ExecutionContextAuthorizationError(
            "execution context inheritance source execution is invalid"
        ) from exc
    if authorization.inherited:
        if (
            source_execution_id != authorization.source_execution_id
            or source_execution_id != execution.source_execution_id
            or str(raw.get("source_session_id")) != str(authorization.source_session_id)
            or str(raw.get("source_session_id")) != str(execution.source_session_id)
        ):
            raise ExecutionContextAuthorizationError(
                "execution context inheritance source link is stale"
            )
        if kind not in {"continuation", "retry", "handoff"}:
            raise ExecutionContextAuthorizationError(
                "execution context inheritance kind is invalid"
            )
        return
    if kind != "retry" or raw.get("source_session_id") is not None:
        raise ExecutionContextAuthorizationError(
            "direct execution retry inheritance facts are invalid"
        )
    source = uow.executions.get(source_execution_id, request.workspace_id)
    if source is None or source.lineage_id != authorization.source_lineage_id:
        raise ExecutionContextAuthorizationError(
            "direct execution retry source is not part of the authorized lineage"
        )
    if source.worker_item_id != execution.worker_item_id:
        raise ExecutionContextAuthorizationError(
            "direct execution retry source changes WorkerItem"
        )


def _budget_uuid(request: ContextAssemblyRequest, key: str) -> UUID:
    value = request.budget.get(key)
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ContextAssemblyError(
            "context_consumer_stale", f"assembly budget is missing {key}"
        ) from exc


def _safe_handoff_facts(bundle: HandoffContextBundle) -> Mapping[str, object]:
    """Expose only bounded handoff references and operator-safe execution facts."""

    def ids(values: Sequence[UUID]) -> tuple[str, ...]:
        return tuple(str(value) for value in values)

    facts: dict[str, object] = {
        "bundle_id": str(bundle.id),
        "bundle_hash": bundle.bundle_hash,
        "transition_id": str(bundle.handoff_transition_id),
        "logical_attempt": bundle.logical_attempt,
        "source": {
            "execution_id": str(bundle.source_execution_id),
            "session_id": str(bundle.source_session_id),
            "checkpoint_sequence": bundle.source_checkpoint_sequence,
            "last_accepted_sequence": bundle.source_last_accepted_sequence,
            "context_package_id": str(bundle.source_context_package_id),
            "context_package_hash": bundle.source_context_package_hash,
            "context_version": bundle.source_worker_context_version,
            "workspace_manifest_id": str(bundle.source_workspace_manifest_id),
            "workspace_version": bundle.source_workspace_version,
            "workspace_location_ref_hash": bundle.workspace_location_ref_hash,
            "repository_state": bundle.repository_state,
            "artifact_refs": bundle.artifact_refs,
            "evidence_refs": bundle.evidence_refs,
            "delivered_input_refs": ids(bundle.delivered_input_refs),
            "transferable_input_refs": ids(bundle.transferable_input_refs),
            "excluded_input_refs": ids(bundle.excluded_input_refs),
        },
        "target": {
            "execution_id": str(bundle.target_execution_id),
            "worker_id": str(bundle.target_worker_id),
            "runtime_hash": bundle.target_runtime_snapshot_hash,
            "context_version": bundle.target_context_version,
        },
        "reason": bundle.handoff_reason,
        "instruction": bundle.handoff_instruction,
    }
    if bundle.schema_version == 2:
        if bundle.target_profile_ref is None or bundle.target_native_metadata is None:
            raise ContextAssemblyError(
                "context_consumer_stale", "native handoff bundle appointment is missing"
            )
        facts["target"] = {
            "execution_id": str(bundle.target_execution_id),
            "profile_ref": bundle.target_profile_ref.model_dump(mode="json"),
            "binding_version": bundle.target_binding_version,
            "native_metadata": dict(bundle.target_native_metadata),
            "context_version": bundle.target_context_version,
        }
    return facts


def _system_command(
    request: ContextAssemblyRequest,
    *,
    action: str,
    actor_id: str,
    target_type: str,
    target_id: UUID,
    expected_version: int | None,
    payload: dict[str, object],
) -> CommandEnvelope:
    command_id = uuid5(
        _PACKAGE_NAMESPACE,
        f"context-command:{action}:{request.request_id}:{request.version}",
    )
    return CommandEnvelope(
        id=command_id,
        workspace_id=request.workspace_id,
        command_type="ContextAssemblyWorker",
        target_type=target_type,
        actor_type="system",
        actor_id=actor_id,
        target_id=target_id,
        payload=payload,
        expected_version=expected_version,
        idempotency_key=f"context-worker:{action}:{request.request_id}:v{request.version}",
        correlation_id=request.request_id,
    )


def _error_code(error: Exception) -> str:
    raw = getattr(error, "code", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:100]
    return "context_worker_error"


def summary_json(result: ContextPackageWorkerResult) -> str:
    return json.dumps(
        {
            "component": "context-package-worker",
            "status": "ok",
            "claimed": result.claimed,
            "ready": result.ready,
            "retried": result.retried,
            "blocked": result.blocked,
            "failed": result.failed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(slots=True)
class ContextPackageWorkerRuntime:
    worker: ContextPackageWorker
    database: Database
    catalog: KnowledgeHttpAdapter


def build_context_package_worker(
    settings: Settings,
    *,
    owner_id: str | None = None,
    owner_check: Callable[[], bool] | None = None,
) -> ContextPackageWorkerRuntime:
    database = Database(settings.database_url)
    catalog = KnowledgeHttpAdapter(
        base_url=settings.knowledge_base_url,
        control_plane_token=settings.knowledge_control_plane_token,
        metadata_timeout_seconds=settings.knowledge_metadata_timeout_seconds,
        materialization_timeout_seconds=settings.knowledge_materialization_timeout_seconds,
    )

    def uow_factory() -> SqlAlchemyUnitOfWork:
        require_gateway_owner(owner_check)
        return SqlAlchemyUnitOfWork(
            database,
            projection_change_retention_seconds=settings.projection_change_retention_seconds,
            before_commit=None
            if owner_check is None
            else lambda: require_gateway_owner(owner_check),
        )

    try:
        worker = ContextPackageWorker(
            uow_factory=uow_factory,
            catalog=catalog,
            owner_id=owner_id or f"context-worker:{settings.local_actor_id}",
            lease_seconds=settings.context_assembly_lease_seconds,
            max_attempts=settings.context_assembly_max_attempts,
        )
    except BaseException:
        catalog.close()
        database.dispose()
        raise
    return ContextPackageWorkerRuntime(
        worker=worker, database=database, catalog=catalog
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the SIDERA Context Package Worker"
    )
    parser.add_argument("--owner-id", default="context-worker-local")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0 or args.batch_size < 1:
        parser.error("poll and batch limits must be positive")
    runtime: ContextPackageWorkerRuntime | None = None
    stop_event = Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_event.set())
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_context_package_worker(settings, owner_id=args.owner_id)
        while not stop_event.is_set():
            result = runtime.worker.run_once(args.batch_size)
            print(summary_json(result), flush=True)
            if args.once:
                return 0
            stop_event.wait(args.poll_seconds)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "context-package-worker",
                    "status": "error",
                    "error": "context package worker failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if runtime is not None:
            runtime.catalog.close()
            runtime.database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
