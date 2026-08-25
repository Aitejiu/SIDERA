from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import SecretStr

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_materialization import (
    ContextMaterializationError,
    ContextMaterializer,
    build_materialization_audit,
)
from sidera_control_plane.application.knowledge_candidate_submission import (
    CandidateProposalSpoolError,
    KnowledgeCandidateProposalV1,
    KnowledgeCandidateSubmissionService,
    KnowledgeCandidateSubmissionStatus,
)
from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogEntry,
    CapabilityCatalogSnapshot,
    ContextReference,
    PlannerClarificationResult,
    PlannerExplicitFailure,
    PlannerFailure,
    PlannerInputAssembler,
    PlannerInvalidResult,
    PlannerPolicyConstraints,
    PlannerProposalResult,
    PlannerResult,
    PlannerRunControl,
)
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    PlannerAdapter,
)
from sidera_control_plane.application.secret_resolution import (
    EnvironmentSecretResolver,
    SecretResolutionError,
    SecretResolver,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextAssemblyStatus,
    ContextMaterializationMode,
    ContextMaterializationOutcome,
    ContextMaterializationV1,
    ContextPackageManifestV1,
    ContextPackageStatus,
    ContextPurpose,
)
from sidera_control_plane.domain.errors import (
    DomainError,
    StaleFencingToken,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.modules import (
    PLANNER_ADAPTER_MODULE_TYPE,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningRequest,
    PlanningRequestStatus,
    PlanningRun,
    PlanningRunLease,
    PlanningRunLeaseStatus,
    PlanningRunStatus,
    PlanningUsage,
    planning_operation_id,
)
from sidera_control_plane.domain.planning_profiles import (
    PlanningProfileSnapshot,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    ProposalRevision,
)
from sidera_control_plane.domain.work_context import WorkContextRevision

logger = logging.getLogger(__name__)


class PlanningAdapterUnavailable(Exception):
    def __init__(self, code: str, *, blocked: bool) -> None:
        super().__init__(code)
        self.code = code
        self.blocked = blocked


class ContextGateError(Exception):
    def __init__(self, code: str, message: str, *, blocked: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.blocked = blocked


class PlannerAdapterFactory(Protocol):
    def create(
        self,
        definition: ModuleDefinition | None,
        instance: ModuleInstance | None,
        profile_snapshot: PlanningProfileSnapshot,
    ) -> PlannerAdapter:
        raise NotImplementedError


PlannerAdapterBuilder = Callable[
    [Mapping[str, object], tuple[SecretStr, ...]], PlannerAdapter
]


class RegistryPlannerAdapterFactory:
    """Resolve a planner provider explicitly and keep secrets out of durable facts."""

    def __init__(
        self,
        builders: Mapping[str, PlannerAdapterBuilder],
        *,
        secret_resolver: SecretResolver | None = None,
        temp_root: Path = Path(".sidera/planning"),
    ) -> None:
        self._builders = dict(builders)
        self._secret_resolver = secret_resolver or EnvironmentSecretResolver()
        self._temp_root = temp_root

    def create(
        self,
        definition: ModuleDefinition | None,
        instance: ModuleInstance | None,
        profile_snapshot: PlanningProfileSnapshot,
    ) -> PlannerAdapter:
        if definition is None or instance is None:
            raise PlanningAdapterUnavailable(
                "planning_profile_unavailable", blocked=False
            )
        if definition.module_type != PLANNER_ADAPTER_MODULE_TYPE:
            raise PlanningAdapterUnavailable(
                "planning_profile_unavailable", blocked=False
            )
        if definition.lifecycle is not ModuleLifecycle.ACTIVE:
            raise PlanningAdapterUnavailable(
                "planning_profile_unavailable", blocked=False
            )
        if instance.config_version != profile_snapshot.planner_module_config_version:
            raise PlanningAdapterUnavailable(
                "planning_profile_unavailable", blocked=False
            )
        if not isinstance(
            definition.capabilities, PlannerAdapterCapabilityProfile
        ):
            raise PlanningAdapterUnavailable(
                "planning_profile_unavailable", blocked=False
            )
        builder = self._builders.get(definition.provider)
        if builder is None:
            raise PlanningAdapterUnavailable(
                "planner_provider_unsupported", blocked=True
            )
        references = tuple(
            dict.fromkeys(
                (*profile_snapshot.planner_secret_refs, *instance.secret_refs)
            )
        )
        try:
            secrets = tuple(
                self._secret_resolver.resolve(reference) for reference in references
            )
        except SecretResolutionError as exc:
            raise PlanningAdapterUnavailable(
                "planning_profile_unavailable", blocked=False
            ) from exc
        try:
            return builder(instance.config, secrets)
        except PlanningAdapterUnavailable:
            raise
        except Exception as exc:
            raise PlanningAdapterUnavailable(
                "planning_profile_unavailable", blocked=False
            ) from exc
        finally:
            del secrets


@dataclass(frozen=True, slots=True)
class PlanningRunOutcome:
    run_id: UUID
    status: PlanningRunStatus | str
    attempt: int
    code: str | None = None
    proposal_revision: int | None = None
    usage: PlanningUsage = field(default_factory=PlanningUsage.zero)


@dataclass(frozen=True, slots=True)
class PlanningBatchResult:
    claimed: int
    succeeded: int
    clarifications: int
    failed: int
    blocked: int
    outcomes: tuple[PlanningRunOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class _ClaimedRun:
    run_id: UUID
    request_id: UUID
    workspace_id: UUID
    operation_id: UUID
    lease_id: UUID
    fencing_token: int
    attempt: int
    brief: PlanningBriefRevision
    profile_snapshot: PlanningProfileSnapshot
    correlation_id: UUID
    definition: ModuleDefinition | None
    instance: ModuleInstance | None
    context_package: ContextPackageManifestV1 | None = None
    context_revision: WorkContextRevision | None = None


@dataclass(frozen=True, slots=True)
class _ClaimBatch:
    items: tuple[_ClaimedRun, ...]
    preblocked: tuple[PlanningRunOutcome, ...]


ContextClaimGate = tuple[ContextPackageManifestV1, WorkContextRevision] | str


class PlanningLeaseKeeper:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        lease_id: UUID,
        planning_run_id: UUID,
        workspace_id: UUID,
        owner_id: str,
        fencing_token: int,
        lease_ttl_seconds: int,
        renew_interval_seconds: float = 0.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        self._uow_factory = uow_factory
        self._lease_id = lease_id
        self._planning_run_id = planning_run_id
        self._workspace_id = workspace_id
        self._owner_id = owner_id
        self._fencing_token = fencing_token
        self._lease_ttl_seconds = lease_ttl_seconds
        self._renew_interval_seconds = renew_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()
        self._health_error: Exception | None = None
        self._thread: threading.Thread | None = None

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def health_error(self) -> Exception | None:
        return self._health_error

    def start(self) -> None:
        if self._renew_interval_seconds <= 0:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"planning-lease-{self._lease_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._renew_interval_seconds, 1.0) + 1.0)

    def renew_once(self) -> bool:
        now = self._clock()
        with self._uow_factory() as uow:
            lease = uow.planning_leases.get(
                self._lease_id, self._workspace_id, for_update=True
            )
            request = uow.planning_requests.get(
                self._request_id_for_run(uow), self._workspace_id
            )
            if request is not None and request.status is PlanningRequestStatus.CANCELLED:
                self._cancel_event.set()
                return False
            if lease is None or not self._owns_active_lease(lease, now):
                self._cancel_event.set()
                return False
            previous_version = lease.version
            lease.expires_at = now + timedelta(seconds=self._lease_ttl_seconds)
            lease.version += 1
            uow.planning_leases.save(lease, previous_version)
        return True

    def _request_id_for_run(self, uow: ControlPlaneUnitOfWork) -> UUID:
        run = uow.planning_runs.get(
            self._planning_run_id, self._workspace_id, for_update=False
        )
        if run is None:
            return self._planning_run_id
        return run.planning_request_id

    def _owns_active_lease(self, lease: PlanningRunLease, now: datetime) -> bool:
        return (
            lease.status is PlanningRunLeaseStatus.ACTIVE
            and lease.owner_id == self._owner_id
            and lease.fencing_token == self._fencing_token
            and lease.expires_at > now
        )

    def _run(self) -> None:
        while not self._stop_event.wait(self._renew_interval_seconds):
            try:
                if not self.renew_once():
                    return
            except Exception as exc:  # noqa: BLE001
                self._health_error = exc
                self._cancel_event.set()
                return


class PlanningRunner:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        adapter_factory: PlannerAdapterFactory,
        owner_id: str,
        lease_ttl_seconds: int = 60,
        lease_renew_interval_seconds: float = 10.0,
        max_diagnostic_bytes: int = 4096,
        planner_temp_root: Path = Path(".sidera/planning"),
        clock: Callable[[], datetime] | None = None,
        context_materializer: ContextMaterializer | None = None,
        context_package_cutover_at: datetime | None = None,
        candidate_submission_service: KnowledgeCandidateSubmissionService | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if lease_ttl_seconds <= 0 or max_diagnostic_bytes <= 0:
            raise ValueError("planning runner limits must be positive")
        if lease_renew_interval_seconds >= lease_ttl_seconds / 2:
            raise ValueError("lease renewal interval must be less than half the TTL")
        if (
            context_package_cutover_at is not None
            and (
                context_package_cutover_at.tzinfo is None
                or context_package_cutover_at.utcoffset() is None
            )
        ):
            raise ValueError("context_package_cutover_at must be timezone aware")
        self._uow_factory = uow_factory
        self._adapter_factory = adapter_factory
        self._owner_id = owner_id
        self._lease_ttl_seconds = lease_ttl_seconds
        self._lease_renew_interval_seconds = lease_renew_interval_seconds
        self._max_diagnostic_bytes = max_diagnostic_bytes
        self._planner_temp_root = planner_temp_root
        self._clock = clock or (lambda: datetime.now(UTC))
        self._context_materializer = context_materializer
        self._context_package_cutover_at = context_package_cutover_at
        self._candidate_submission_service = candidate_submission_service

    def run_once(self, limit: int = 10) -> PlanningBatchResult:
        if limit <= 0:
            raise ValueError("planning runner limit must be positive")
        claim_batch = self._claim(limit)
        outcomes: list[PlanningRunOutcome] = list(claim_batch.preblocked)
        for item in claim_batch.items:
            started_at = self._clock()
            try:
                outcome = self._execute(item)
            except Exception:  # noqa: BLE001
                logger.error(
                    "planning run failed outside result boundary",
                    extra={
                        "workspace_id": str(item.workspace_id),
                        "planning_request_id": str(item.request_id),
                        "planning_run_id": str(item.run_id),
                        "operation_id": str(item.operation_id),
                        "lease_id": str(item.lease_id),
                        "fencing_token": item.fencing_token,
                    },
                )
                outcome = self._persist_failure(
                    item,
                    PlannerExplicitFailure(
                        code="planning_worker_error",
                        safe_message="planning worker failed before a durable result",
                        retryable=True,
                        unknown_outcome=False,
                    ),
                )
            outcomes.append(outcome)
            self._log_outcome(item, outcome, started_at)
        return PlanningBatchResult(
            claimed=len(claim_batch.items) + len(claim_batch.preblocked),
            succeeded=sum(
                outcome.status is PlanningRunStatus.SUCCEEDED
                for outcome in outcomes
            ),
            clarifications=sum(
                outcome.status is PlanningRunStatus.NEEDS_CLARIFICATION
                for outcome in outcomes
            ),
            failed=sum(
                outcome.status == PlanningRunStatus.FAILED
                for outcome in outcomes
            ),
            blocked=sum(
                outcome.status is PlanningRunStatus.BLOCKED for outcome in outcomes
            ),
            outcomes=tuple(outcomes),
        )

    def _claim(self, limit: int) -> _ClaimBatch:
        now = self._clock()
        claimed: list[_ClaimedRun] = []
        preblocked: list[PlanningRunOutcome] = []
        with self._uow_factory() as uow:
            for queued_run in uow.planning_runs.claim_queued(now=now, limit=limit):
                request = uow.planning_requests.get(
                    queued_run.planning_request_id,
                    queued_run.workspace_id,
                    for_update=True,
                )
                if request is None:
                    continue
                request_run = request.run(queued_run.id)
                context_package: ContextPackageManifestV1 | None = None
                context_revision: WorkContextRevision | None = None
                if self._context_materializer is not None and not self._legacy_run(
                    request_run.queued_at
                ):
                    gate = self._context_claim_gate(
                        uow, request_run, now=now
                    )
                    if gate == "wait":
                        continue
                    if isinstance(gate, str):
                        previous_request_version = request.version
                        previous_run_version = request_run.version
                        request.block_queued_run(
                            run_id=request_run.id,
                            error_code=gate,
                            error_message="planner context is not eligible for a new start",
                            now=now,
                        )
                        uow.planning_runs.save(request_run, previous_run_version)
                        uow.planning_requests.save(request, previous_request_version)
                        self._record_event(
                            uow,
                            request=request,
                            event_type="PlanningRunContextBlocked",
                            payload={
                                "planning_run_id": str(request_run.id),
                                "error_code": gate,
                                "status": request_run.status.value,
                            },
                        )
                        preblocked.append(
                            PlanningRunOutcome(
                                run_id=request_run.id,
                                status=request_run.status,
                                attempt=request_run.attempt,
                                code=gate,
                            )
                        )
                        continue
                    context_package, context_revision = gate
                previous_request_version = request.version
                previous_run_version = request_run.version
                lease = PlanningRunLease(
                    id=uuid4(),
                    workspace_id=request.workspace_id,
                    planning_run_id=request_run.id,
                    owner_id=self._owner_id,
                    status=PlanningRunLeaseStatus.ACTIVE,
                    fencing_token=request_run.attempt,
                    acquired_at=now,
                    expires_at=now + timedelta(seconds=self._lease_ttl_seconds),
                    released_at=None,
                    version=1,
                )
                request.start_planning(
                    run_id=request_run.id,
                    lease_id=lease.id,
                    fencing_token=lease.fencing_token,
                    now=now,
                )
                uow.planning_leases.add(lease)
                uow.planning_runs.save(request_run, previous_run_version)
                uow.planning_requests.save(request, previous_request_version)
                self._record_event(
                    uow,
                    request=request,
                    event_type="PlanningRunUpdated",
                    payload={
                        "planning_run_id": str(request_run.id),
                        "operation_id": str(request_run.operation_id),
                        "lease_id": str(lease.id),
                        "fencing_token": lease.fencing_token,
                        "status": request_run.status.value,
                    },
                )
                instance = uow.modules.get_instance(
                    request_run.planner_module_instance_id, request.workspace_id
                )
                definition = (
                    None
                    if instance is None
                    else uow.modules.get_definition(
                        instance.definition_id, request.workspace_id
                    )
                )
                claimed.append(
                    _ClaimedRun(
                        run_id=request_run.id,
                        request_id=request.id,
                        workspace_id=request.workspace_id,
                        operation_id=request_run.operation_id,
                        lease_id=lease.id,
                        fencing_token=lease.fencing_token,
                        attempt=request_run.attempt,
                        brief=request.briefs[-1],
                        profile_snapshot=request.profile_snapshot,
                        correlation_id=request.correlation_id,
                        definition=definition,
                        instance=instance,
                        context_package=context_package,
                        context_revision=context_revision,
                    )
                )
        return _ClaimBatch(items=tuple(claimed), preblocked=tuple(preblocked))

    def _legacy_run(self, queued_at: datetime) -> bool:
        return (
            self._context_package_cutover_at is not None
            and queued_at < self._context_package_cutover_at
        )

    @staticmethod
    def _context_claim_gate(
        uow: ControlPlaneUnitOfWork,
        run: PlanningRun,
        *,
        now: datetime,
    ) -> ContextClaimGate:
        if 1 not in run.capability_snapshot.context_package_versions:
            return "context_adapter_unsupported"
        assembly = uow.context_assemblies.get_for_consumer(
            ContextPurpose.PLANNING_RUN,
            run.id,
            run.workspace_id,
        )
        if assembly is None:
            return "wait"
        if assembly.status is not ContextAssemblyStatus.READY:
            if assembly.status in {
                ContextAssemblyStatus.QUEUED,
                ContextAssemblyStatus.ASSEMBLING,
                ContextAssemblyStatus.RETRY_WAIT,
            }:
                return "wait"
            return "context_assembly_blocked"
        package = uow.context_packages.get_for_consumer(
            ContextPurpose.PLANNING_RUN,
            run.id,
            run.workspace_id,
        )
        if package is None:
            return "wait"
        if package.status is not ContextPackageStatus.ACTIVE:
            return "context_package_unavailable"
        if package.expires_at <= now:
            return "context_package_expired"
        revision = uow.work_contexts.get_revision(
            package.work_context_revision_id,
            run.workspace_id,
        )
        if revision is None:
            return "context_reference_not_found"
        return package, revision

    def _execute(self, item: _ClaimedRun) -> PlanningRunOutcome:
        try:
            adapter = self._adapter_factory.create(
                item.definition, item.instance, item.profile_snapshot
            )
        except PlanningAdapterUnavailable as exc:
            failure = PlannerExplicitFailure(
                code=exc.code,
                safe_message="planner adapter is unavailable",
                retryable=not exc.blocked,
                unknown_outcome=False,
            )
            return self._persist_failure(item, failure, blocked=exc.blocked)
        except Exception:  # noqa: BLE001
            return self._persist_failure(
                item,
                PlannerExplicitFailure(
                    code="planning_profile_unavailable",
                    safe_message="planner profile is unavailable",
                    retryable=False,
                    unknown_outcome=False,
                ),
            )

        now = self._clock()
        context_materialization = None
        if self._context_materializer is not None and item.context_package is not None:
            try:
                context_materialization = self._prepare_context(
                    item,
                    adapter,
                    now=now,
                )
            except ContextMaterializationError as exc:
                try:
                    self._record_context_materialization_failure(item, exc, now=now)
                except Exception:
                    logger.exception(
                        "failed to persist planner context materialization failure",
                        extra={"planning_run_id": str(item.run_id)},
                    )
                return self._persist_failure(
                    item,
                    PlannerExplicitFailure(
                        code=exc.code,
                        safe_message="planner context could not be materialized",
                        retryable=exc.temporary,
                        unknown_outcome=False,
                    ),
                    blocked=exc.corrupt or not exc.temporary,
                )
            except ContextGateError as exc:
                return self._persist_failure(
                    item,
                    PlannerExplicitFailure(
                        code=exc.code,
                        safe_message=exc.message,
                        retryable=not exc.blocked,
                        unknown_outcome=False,
                    ),
                    blocked=exc.blocked,
                )
            except Exception:  # noqa: BLE001
                return self._persist_failure(
                    item,
                    PlannerExplicitFailure(
                        code="context_gate_error",
                        safe_message="planner context gate failed before model call",
                        retryable=True,
                        unknown_outcome=False,
                    ),
                )
        planner_input = PlannerInputAssembler().assemble(
            brief=item.brief,
            profile_snapshot=item.profile_snapshot,
            capability_catalog=_capability_catalog(item.profile_snapshot, now),
            policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(
                item.profile_snapshot
            ),
            operation_id=planning_operation_id(item.run_id, item.attempt),
            planning_request_id=item.request_id,
            planning_run_id=item.run_id,
            context_refs=tuple(
                ContextReference(ref=ref, safe_summary=None)
                for ref in item.brief.context_refs
            ),
            context_materialization=context_materialization,
        )
        deadline = now + timedelta(
            seconds=item.profile_snapshot.budget.max_runtime_seconds
        )
        keeper = PlanningLeaseKeeper(
            uow_factory=self._uow_factory,
            lease_id=item.lease_id,
            planning_run_id=item.run_id,
            workspace_id=item.workspace_id,
            owner_id=self._owner_id,
            fencing_token=item.fencing_token,
            lease_ttl_seconds=self._lease_ttl_seconds,
            renew_interval_seconds=self._lease_renew_interval_seconds,
            clock=self._clock,
        )
        result: PlannerResult
        try:
            keeper.start()
            result = adapter.plan(
                planner_input,
                PlannerRunControl(
                    operation_id=planner_input.operation_id,
                    started_at=now,
                    deadline_at=deadline,
                    cancel_requested=keeper.cancel_requested,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            result = PlannerExplicitFailure(
                code="planner_exception",
                safe_message=f"planner adapter raised {type(exc).__name__}",
                retryable=False,
                unknown_outcome=False,
            )
        finally:
            keeper.stop()

        if keeper.cancel_requested:
            try:
                adapter.cancel(planner_input.operation_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "planner cancellation observation failed",
                    extra={"operation_id": str(planner_input.operation_id)},
                )
            return PlanningRunOutcome(
                run_id=item.run_id,
                status="cancelled",
                attempt=item.attempt,
                code="cancelled",
            )
        if keeper.health_error is not None:
            result = PlannerExplicitFailure(
                code="planning_lease_unavailable",
                safe_message="planning lease could not be renewed",
                retryable=True,
                unknown_outcome=False,
            )
        return self._persist_result(item, result)

    def _prepare_context(
        self,
        item: _ClaimedRun,
        adapter: PlannerAdapter,
        *,
        now: datetime,
    ) -> ContextMaterializationV1 | None:
        if self._context_materializer is None or item.context_package is None:
            return None
        if item.context_revision is None:
            raise ContextGateError(
                "context_reference_not_found",
                "planner work context revision is unavailable",
                blocked=True,
            )
        materialization = self._context_materializer.materialize(
            item.context_package,
            item.context_revision,
            mode=ContextMaterializationMode.CONSUMER_START,
            workspace_id=item.workspace_id,
            consumer_id=item.run_id,
            purpose=ContextPurpose.PLANNING_RUN,
            context_version=item.context_package.context_version,
            expected_package_hash=item.context_package.package_hash,
            now=now,
            correlation_id=item.correlation_id,
            caller_request_id=item.request_id,
        )
        accept_context = getattr(adapter, "accept_context", None)
        if not callable(accept_context):
            raise ContextGateError(
                "context_adapter_unsupported",
                "planner adapter does not accept context packages",
                blocked=True,
            )
        try:
            acknowledgement = accept_context(
                materialization,
                operation_id=item.operation_id,
            )
        except ContextGateError:
            raise
        except Exception as exc:
            raise ContextGateError(
                "context_adapter_rejected",
                "planner adapter rejected the context package",
                blocked=True,
            ) from exc
        if not isinstance(acknowledgement, ContextAcknowledgement):
            raise ContextGateError(
                "context_acknowledgement_mismatch",
                "planner adapter returned an invalid context acknowledgement",
                blocked=True,
            )
        self._validate_context_acknowledgement(
            acknowledgement,
            materialization=materialization,
            operation_id=item.operation_id,
        )
        try:
            self._persist_context_acceptance(
                item,
                materialization=materialization,
                acknowledgement=acknowledgement,
                now=now,
            )
        except Exception as exc:
            raise ContextGateError(
                "context_acknowledgement_persistence_failed",
                "planner context acknowledgement could not be persisted",
                blocked=False,
            ) from exc
        return materialization

    @staticmethod
    def _validate_context_acknowledgement(
        acknowledgement: ContextAcknowledgement,
        *,
        materialization: ContextMaterializationV1,
        operation_id: UUID,
    ) -> None:
        metadata = materialization.metadata
        if (
            acknowledgement.package_id != metadata.package_id
            or acknowledgement.package_hash != metadata.package_hash
            or acknowledgement.consumer_type != metadata.consumer_type
            or acknowledgement.consumer_id != metadata.consumer_id
            or acknowledgement.context_version != metadata.context_version
            or acknowledgement.schema_version != materialization.schema_version
            or acknowledgement.operation_id != operation_id
        ):
            raise ContextGateError(
                "context_acknowledgement_mismatch",
                "planner adapter acknowledgement does not match the context package",
                blocked=True,
            )

    def _persist_context_acceptance(
        self,
        item: _ClaimedRun,
        *,
        materialization: ContextMaterializationV1,
        acknowledgement: ContextAcknowledgement,
        now: datetime,
    ) -> None:
        package = item.context_package
        if package is None:
            raise ContextGateError(
                "context_reference_not_found",
                "planner context package is unavailable",
                blocked=True,
            )
        durable_ack = replace(
            acknowledgement,
            adapter_module_id=item.profile_snapshot.planner_module_instance_id,
            adapter_config_version=item.profile_snapshot.planner_module_config_version,
        )
        command = self._context_command(item, action="acknowledge")
        audit = build_materialization_audit(
            package,
            attempt=item.attempt,
            outcome=ContextMaterializationOutcome.SUCCEEDED,
            occurred_at=now,
            materialization=materialization,
            audit_id=uuid5(
                NAMESPACE_URL,
                f"sidera:planner-context-audit:{item.run_id}:{item.attempt}",
            ),
        )
        with self._uow_factory() as uow:
            if uow.commands.find_by_id(item.workspace_id, command.id) is None:
                uow.commands.record_applied(
                    command,
                    hash_request(command),
                    CommandResult(
                        command_id=command.id,
                        aggregate_id=package.package_id,
                        aggregate_version=1,
                        event_ids=(),
                        data={
                            "planning_run_id": str(item.run_id),
                            "package_id": str(package.package_id),
                        },
                    ),
                )
            uow.context_packages.add_ack(
                durable_ack,
                actor_type="system",
                actor_id=self._owner_id,
                causation_id=command.id,
                correlation_id=item.correlation_id,
            )
            uow.context_packages.append_materialization_audit(
                audit,
                actor_type="system",
                actor_id=self._owner_id,
                causation_id=command.id,
                correlation_id=item.correlation_id,
            )

    def _record_context_materialization_failure(
        self,
        item: _ClaimedRun,
        error: ContextMaterializationError,
        *,
        now: datetime,
    ) -> None:
        package = item.context_package
        if package is None:
            return
        command = self._context_command(item, action="materialize-failure")
        audit = build_materialization_audit(
            package,
            attempt=item.attempt,
            outcome=ContextMaterializationOutcome.FAILED,
            occurred_at=now,
            failure_code=error.code,
            audit_id=uuid5(
                NAMESPACE_URL,
                f"sidera:planner-context-failure:{item.run_id}:{item.attempt}",
            ),
        )
        with self._uow_factory() as uow:
            if uow.commands.find_by_id(item.workspace_id, command.id) is None:
                uow.commands.record_applied(
                    command,
                    hash_request(command),
                    CommandResult(
                        command_id=command.id,
                        aggregate_id=package.package_id,
                        aggregate_version=1,
                        event_ids=(),
                        data={"failure_code": error.code},
                    ),
                )
            if error.corrupt:
                uow.context_packages.mark_corrupt(
                    package.package_id,
                    item.workspace_id,
                    reason=error.code,
                    now=now,
                    actor_type="system",
                    actor_id=self._owner_id,
                    causation_id=command.id,
                    correlation_id=item.correlation_id,
                )
            uow.context_packages.append_materialization_audit(
                audit,
                actor_type="system",
                actor_id=self._owner_id,
                causation_id=command.id,
                correlation_id=item.correlation_id,
            )

    def _context_command(self, item: _ClaimedRun, *, action: str) -> CommandEnvelope:
        package_id = item.context_package.package_id if item.context_package else item.run_id
        command_id = uuid5(
            NAMESPACE_URL,
            f"sidera:planner-context:{action}:{item.run_id}:{item.attempt}",
        )
        return CommandEnvelope(
            id=command_id,
            workspace_id=item.workspace_id,
            command_type="PlanningContextGate",
            target_type="context_package",
            actor_type="system",
            actor_id=self._owner_id,
            target_id=package_id,
            payload={
                "planning_run_id": str(item.run_id),
                "package_id": str(package_id),
                "action": action,
            },
            expected_version=None,
            idempotency_key=f"planner-context:{action}:{item.run_id}:attempt:{item.attempt}",
            correlation_id=item.correlation_id,
        )

    def _persist_result(
        self,
        item: _ClaimedRun,
        result: PlannerResult,
    ) -> PlanningRunOutcome:
        if isinstance(result, PlannerProposalResult):
            try:
                return self._persist_proposal(
                    item,
                    result.proposal,
                    result.usage,
                    result.knowledge_candidates,
                    result.knowledge_candidate_diagnostics,
                )
            except (DomainError, ValueError, TypeError):
                return self._persist_failure(
                    item,
                    PlannerInvalidResult(
                        code="invalid_result",
                        safe_message="planner returned an invalid proposal",
                        retryable=True,
                        unknown_outcome=False,
                    ),
                )
        if isinstance(result, PlannerClarificationResult):
            try:
                return self._persist_clarification(item, result)
            except (DomainError, ValueError, TypeError):
                return self._persist_failure(
                    item,
                    PlannerInvalidResult(
                        code="invalid_result",
                        safe_message="planner returned invalid clarification questions",
                        retryable=True,
                        unknown_outcome=False,
                    ),
                )
        if isinstance(result, PlannerFailure):
            blocked = result.unknown_outcome and (
                item.profile_snapshot.planner_capabilities.idempotency
                is not IdempotencyCapability.OPERATION_ID
                or item.profile_snapshot.retry_policy.unknown_outcome_policy
                is UnknownOutcomePolicy.BLOCK
            )
            return self._persist_failure(
                item, result, blocked=blocked, usage=result.usage
            )
        return self._persist_failure(
            item,
            PlannerInvalidResult(
                code="invalid_result",
                safe_message="planner returned an unsupported result",
                retryable=True,
                unknown_outcome=False,
            ),
        )

    def _persist_proposal(
        self,
        item: _ClaimedRun,
        adapter_proposal: ProposalRevision,
        usage: PlanningUsage,
        knowledge_candidates: tuple[KnowledgeCandidateProposalV1, ...] = (),
        knowledge_candidate_diagnostics: tuple[str, ...] = (),
    ) -> PlanningRunOutcome:
        now = self._clock()
        with self._uow_factory() as uow:
            locked = self._lock_owned_run(uow, item, now)
            if locked is None:
                return self._stale_outcome(item)
            request, run, lease = locked
            proposals = uow.planning_records.list_proposals(
                item.request_id, item.workspace_id
            )
            proposal = ProposalRevision.create(
                planning_request_id=request.id,
                revision=max((entry.revision for entry in proposals), default=0) + 1,
                brief_revision=run.brief_revision,
                planning_run_id=run.id,
                kind=adapter_proposal.kind,
                payload=adapter_proposal.payload,
                profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                    request.profile_snapshot
                ),
                profile_snapshot=request.profile_snapshot,
                created_at=now,
            )
            previous_request_version = request.version
            previous_run_version = run.version
            uow.planning_records.add_proposal(proposal, request.workspace_id)
            request.succeed_run(
                run_id=run.id,
                lease_id=lease.id,
                fencing_token=lease.fencing_token,
                provider_ref=_provider_ref(item),
                structured_result_type="proposal",
                usage=usage,
                now=now,
            )
            uow.planning_runs.save(run, previous_run_version)
            run_event = self._record_event(
                uow,
                request=request,
                event_type="PlanningRunUpdated",
                payload={
                    "planning_run_id": str(run.id),
                    "status": run.status.value,
                    "result_kind": "proposal",
                },
            )
            request.mark_proposal_ready(
                run_id=run.id,
                proposal_revision=proposal.revision,
                content_hash=proposal.content_hash,
                now=now,
            )
            uow.planning_requests.save(request, previous_request_version)
            self._release_lease(uow, lease, now)
            proposal_event = self._record_event(
                uow,
                request=request,
                event_type="PlanningProposalReady",
                payload={
                    "planning_run_id": str(run.id),
                    "proposal_revision": proposal.revision,
                    "content_hash": proposal.content_hash,
                },
            )
            if self._candidate_submission_service is not None:
                for candidate in knowledge_candidates:
                    try:
                        with uow.savepoint():
                            self._candidate_submission_service.queue_in_uow(
                                uow,
                                workspace_id=request.workspace_id,
                                producer_type="planner",
                                producer_id=run.id,
                                proposal=candidate,
                                status=KnowledgeCandidateSubmissionStatus.WAITING_FOR_TASK,
                                now=now,
                                correlation_id=request.correlation_id,
                                planning_run_id=run.id,
                                actor_id=self._owner_id,
                            )
                    except (CandidateProposalSpoolError, DomainError, TypeError, ValueError):
                        logger.warning(
                            "planner knowledge candidate was not queued",
                            extra={
                                "planning_run_id": str(run.id),
                                "proposal_hash": candidate.proposal_hash,
                            },
                        )
            for diagnostic in knowledge_candidate_diagnostics:
                logger.warning(
                    "planner knowledge candidate diagnostic",
                    extra={
                        "planning_run_id": str(run.id),
                        "diagnostic": diagnostic,
                    },
                )
            _ = (run_event, proposal_event)
        return PlanningRunOutcome(
            run_id=item.run_id,
            status=PlanningRunStatus.SUCCEEDED,
            attempt=item.attempt,
            proposal_revision=proposal.revision,
            usage=usage,
        )

    def _persist_clarification(
        self, item: _ClaimedRun, result: PlannerClarificationResult
    ) -> PlanningRunOutcome:
        now = self._clock()
        with self._uow_factory() as uow:
            locked = self._lock_owned_run(uow, item, now)
            if locked is None:
                return self._stale_outcome(item)
            request, run, lease = locked
            run.usage = result.usage
            previous_request_version = request.version
            previous_run_version = run.version
            for clarification in result.clarifications:
                uow.planning_records.add_clarification(clarification)
            request.mark_needs_clarification(
                run_id=run.id,
                clarifications=result.clarifications,
                lease_id=lease.id,
                fencing_token=lease.fencing_token,
                now=now,
            )
            uow.planning_runs.save(run, previous_run_version)
            uow.planning_requests.save(request, previous_request_version)
            self._release_lease(uow, lease, now)
            self._record_event(
                uow,
                request=request,
                event_type="PlanningClarificationRequired",
                payload={
                    "planning_run_id": str(run.id),
                    "clarification_count": len(result.clarifications),
                },
            )
        return PlanningRunOutcome(
            run_id=item.run_id,
            status=PlanningRunStatus.NEEDS_CLARIFICATION,
            attempt=item.attempt,
            usage=result.usage,
        )

    def _persist_failure(
        self,
        item: _ClaimedRun,
        failure: PlannerFailure,
        *,
        blocked: bool = False,
        usage: PlanningUsage | None = None,
    ) -> PlanningRunOutcome:
        now = self._clock()
        with self._uow_factory() as uow:
            locked = self._lock_owned_run(uow, item, now)
            if locked is None:
                return self._stale_outcome(item)
            request, run, lease = locked
            previous_request_version = request.version
            previous_run_version = run.version
            run.provider_ref = failure.provider_ref
            run.structured_result_type = "failure"
            run.usage = usage or failure.usage
            if blocked:
                request.block_run_unknown(
                    run_id=run.id,
                    lease_id=lease.id,
                    fencing_token=lease.fencing_token,
                    error_code=failure.code,
                    error_message=self._diagnostic(failure.safe_message),
                    now=now,
                )
            else:
                request.fail_run(
                    run_id=run.id,
                    lease_id=lease.id,
                    fencing_token=lease.fencing_token,
                    error_code=failure.code,
                    error_message=self._diagnostic(failure.safe_message),
                    now=now,
                )
            uow.planning_runs.save(run, previous_run_version)
            uow.planning_requests.save(request, previous_request_version)
            self._release_lease(uow, lease, now)
            self._record_event(
                uow,
                request=request,
                event_type="PlanningRunUpdated",
                payload={
                    "planning_run_id": str(run.id),
                    "status": run.status.value,
                    "error_code": failure.code,
                },
            )
        return PlanningRunOutcome(
            run_id=item.run_id,
            status=run.status,
            attempt=item.attempt,
            code=failure.code,
            usage=usage or failure.usage,
        )

    def _lock_owned_run(
        self,
        uow: ControlPlaneUnitOfWork,
        item: _ClaimedRun,
        now: datetime,
    ) -> tuple[PlanningRequest, PlanningRun, PlanningRunLease] | None:
        request = uow.planning_requests.get(
            item.request_id, item.workspace_id, for_update=True
        )
        lease = uow.planning_leases.get(
            item.lease_id, item.workspace_id, for_update=True
        )
        if request is None or lease is None:
            return None
        run = request.run(item.run_id)
        if request.status is PlanningRequestStatus.CANCELLED:
            return None
        if (
            run.status is not PlanningRunStatus.RUNNING
            or lease.status is not PlanningRunLeaseStatus.ACTIVE
            or lease.owner_id != self._owner_id
            or lease.fencing_token != item.fencing_token
            or lease.expires_at <= now
        ):
            return None
        run.lease_id = lease.id
        run.fencing_token = lease.fencing_token
        return request, run, lease

    def _release_lease(
        self, uow: ControlPlaneUnitOfWork, lease: PlanningRunLease, now: datetime
    ) -> None:
        previous_version = lease.version
        lease.status = PlanningRunLeaseStatus.RELEASED
        lease.released_at = now
        lease.version += 1
        uow.planning_leases.save(lease, previous_version)

    def _record_event(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        request: PlanningRequest,
        event_type: str,
        payload: dict[str, object],
    ) -> EventData:
        envelope = CommandEnvelope(
            id=uuid4(),
            workspace_id=request.workspace_id,
            command_type="PlanningWorkerRun",
            target_type="planning_request",
            actor_type="system",
            actor_id=self._owner_id,
            target_id=request.id,
            payload=payload,
            expected_version=None,
            idempotency_key=f"{event_type}:{request.id}:{request.version}:{uuid4()}",
            correlation_id=request.correlation_id,
        )
        event = EventData(
            id=uuid4(),
            workspace_id=request.workspace_id,
            aggregate_type="planning_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type=event_type,
            payload=payload,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=request.correlation_id,
        )
        uow.events.append_with_outbox(event)
        uow.commands.record_applied(
            envelope,
            hash_request(envelope),
            CommandResult(
                command_id=envelope.id,
                aggregate_id=request.id,
                aggregate_version=request.version,
                event_ids=(event.id,),
                data={"planning_run_id": payload.get("planning_run_id")},
            ),
        )
        return event

    def _diagnostic(self, value: str) -> str:
        encoded = value.encode("utf-8")[: self._max_diagnostic_bytes]
        return encoded.decode("utf-8", errors="ignore") or "planner failure"

    def _log_outcome(
        self,
        item: _ClaimedRun,
        outcome: PlanningRunOutcome,
        started_at: datetime,
    ) -> None:
        elapsed = max(0.0, (self._clock() - started_at).total_seconds())
        logger.info(
            "planning run completed",
            extra={
                "workspace_id": str(item.workspace_id),
                "planning_request_id": str(item.request_id),
                "planning_run_id": str(item.run_id),
                "operation_id": str(item.operation_id),
                "lease_id": str(item.lease_id),
                "fencing_token": item.fencing_token,
                "profile_id": str(item.profile_snapshot.profile_id),
                "profile_version": item.profile_snapshot.profile_version,
                "module_instance_id": str(
                    item.profile_snapshot.planner_module_instance_id
                ),
                "proposal_revision": outcome.proposal_revision,
                "elapsed_seconds": elapsed,
                "usage": {
                    "input_tokens": outcome.usage.input_tokens,
                    "output_tokens": outcome.usage.output_tokens,
                    "cost_microunits": outcome.usage.cost_microunits,
                    "runtime_seconds": outcome.usage.runtime_seconds,
                    "context_bytes": outcome.usage.context_bytes,
                },
                "result_code": outcome.code,
                "result_status": str(outcome.status),
            },
        )

    def _stale_outcome(self, item: _ClaimedRun) -> PlanningRunOutcome:
        return PlanningRunOutcome(
            run_id=item.run_id,
            status="stale",
            attempt=item.attempt,
            code=StaleFencingToken.code,
        )


def _capability_catalog(
    profile_snapshot: PlanningProfileSnapshot, captured_at: datetime
) -> CapabilityCatalogSnapshot:
    return CapabilityCatalogSnapshot(
        snapshot_version=1,
        captured_at=captured_at,
        capabilities=tuple(
            CapabilityCatalogEntry(name=name, description="profile-allowed capability")
            for name in profile_snapshot.allowed_capabilities
        ),
        adapter_preferences=profile_snapshot.adapter_preferences,
        budget_level=profile_snapshot.execution_budget_level,
        max_concurrency=profile_snapshot.budget.max_concurrency,
        workspace_providers=(),
    )


def _provider_ref(item: _ClaimedRun) -> str:
    if item.definition is not None:
        return item.definition.provider
    return "planner"
