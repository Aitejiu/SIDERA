from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import (
    ArtifactRepositoryPort,
    AttentionAcknowledgementRepositoryPort,
    CollaborationEvaluationRepositoryPort,
    CollaborationGovernanceRepositoryPort,
    CollaborationLedgerRepositoryPort,
    CollaborationRunRepositoryPort,
    CommandRepositoryPort,
    ContextAssemblyRepositoryPort,
    ContextPackageRepositoryPort,
    ContextPatchRepositoryPort,
    EventStorePort,
    EvidenceClaimRepositoryPort,
    ExecutionBindingRepositoryPort,
    ExecutionEventRepositoryPort,
    ExecutionInputRepositoryPort,
    ExecutionRepositoryPort,
    ExecutionRequestRepositoryPort,
    GatewayOperationRepositoryPort,
    HandoffContextBundleRepositoryPort,
    HandoffProposalRepositoryPort,
    HandoffRecoveryDecisionRepositoryPort,
    HandoffTransitionRepositoryPort,
    HandoffWorkspaceTransferRepositoryPort,
    InputSelectionRepositoryPort,
    KnowledgeCandidateSubmissionRepositoryPort,
    LeaseStorePort,
    ModuleRepositoryPort,
    NotificationLedger,
    NotificationObservationStore,
    PlanEntityLineageRepositoryPort,
    PlanningLeaseRepositoryPort,
    PlanningProfileRepositoryPort,
    PlanningRecordRepositoryPort,
    PlanningRequestRepositoryPort,
    PlanningRunRepositoryPort,
    PlanRepositoryPort,
    PlanTransitionRepositoryPort,
    ProjectionChangePublisherPort,
    ProposalMaterializationRepositoryPort,
    ResultRepositoryPort,
    SchedulingBlockRepositoryPort,
    SessionRepositoryPort,
    TaskRepositoryPort,
    WorkContextRepositoryPort,
    WorkerItemRepositoryPort,
    WorkerRepositoryPort,
    WorkerRuntimeConfigurationRepositoryPort,
    WorkerTransitionRepositoryPort,
    WorkspaceRepositoryPort,
)
from sidera_control_plane.application.projection_changes import (
    ProjectionChangePublisher,
)
from sidera_control_plane.persistence.attention_repository import (
    AttentionAcknowledgementRepository,
)
from sidera_control_plane.persistence.collaboration_governance_repository import (
    CollaborationGovernanceRepository,
)
from sidera_control_plane.persistence.collaboration_repository import (
    CollaborationEvaluationRepository,
    CollaborationLedgerRepository,
    CollaborationRunRepository,
)
from sidera_control_plane.persistence.context_package_repository import (
    ContextAssemblyRepository,
    ContextPackageRepository,
)
from sidera_control_plane.persistence.context_patch_repository import (
    ContextPatchRepository,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.execution_repository import ExecutionRepository
from sidera_control_plane.persistence.execution_request_repository import (
    ExecutionRequestRepository,
)
from sidera_control_plane.persistence.gateway_repository import (
    ArtifactRepository,
    EvidenceClaimRepository,
    ExecutionEventRepository,
    ExecutionInputRepository,
    GatewayOperationRepository,
)
from sidera_control_plane.persistence.handoff_repository import (
    HandoffContextBundleRepository,
    HandoffProposalRepository,
    HandoffRecoveryDecisionRepository,
    HandoffTransitionRepository,
    HandoffWorkspaceTransferRepository,
)
from sidera_control_plane.persistence.input_selection_repository import (
    InputSelectionRepository,
)
from sidera_control_plane.persistence.knowledge_candidate_submission_repository import (
    KnowledgeCandidateSubmissionRepository,
)
from sidera_control_plane.persistence.lease_store import LeaseStore
from sidera_control_plane.persistence.module_repository import (
    ExecutionBindingRepository,
    ModuleRepository,
)
from sidera_control_plane.persistence.notification_observation_store import (
    SqlAlchemyNotificationObservationStore,
)
from sidera_control_plane.persistence.notification_repository import (
    SqlAlchemyNotificationRepository,
)
from sidera_control_plane.persistence.plan_repository import PlanRepository
from sidera_control_plane.persistence.plan_transition_repository import (
    PlanEntityLineageRepository,
    PlanTransitionRepository,
    WorkerTransitionRepository,
)
from sidera_control_plane.persistence.planning_repository import (
    PlanningProfileRepository,
    PlanningRecordRepository,
    PlanningRequestRepository,
    PlanningRunLeaseRepository,
    PlanningRunRepository,
    ProposalMaterializationRepository,
)
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)
from sidera_control_plane.persistence.repositories import (
    CommandRepository,
    EventRepository,
    TaskRepository,
)
from sidera_control_plane.persistence.result_repository import ResultRepository
from sidera_control_plane.persistence.scheduling_block_repository import (
    SqlAlchemySchedulingBlockRepository,
)
from sidera_control_plane.persistence.session_repository import SessionRepository
from sidera_control_plane.persistence.work_context_repository import (
    WorkContextRepository,
)
from sidera_control_plane.persistence.worker_item_repository import WorkerItemRepository
from sidera_control_plane.persistence.worker_repository import WorkerRepository
from sidera_control_plane.persistence.worker_runtime_repository import (
    SqlAlchemyWorkerRuntimeConfigurationRepository,
)
from sidera_control_plane.persistence.workspace_repository import WorkspaceRepository


class SqlAlchemyUnitOfWork:
    session: Session
    tasks: TaskRepositoryPort
    plans: PlanRepositoryPort
    plan_transitions: PlanTransitionRepositoryPort
    worker_transitions: WorkerTransitionRepositoryPort
    plan_entity_lineages: PlanEntityLineageRepositoryPort
    context_patches: ContextPatchRepositoryPort
    attention: AttentionAcknowledgementRepositoryPort
    projection_changes: ProjectionChangePublisherPort
    workers: WorkerRepositoryPort
    worker_items: WorkerItemRepositoryPort
    worker_runtime_configurations: WorkerRuntimeConfigurationRepositoryPort
    scheduling_blocks: SchedulingBlockRepositoryPort
    executions: ExecutionRepositoryPort
    execution_requests: ExecutionRequestRepositoryPort
    collaboration_runs: CollaborationRunRepositoryPort
    collaboration_evaluations: CollaborationEvaluationRepositoryPort
    collaboration_ledger: CollaborationLedgerRepositoryPort
    input_selections: InputSelectionRepositoryPort
    collaboration_governance: CollaborationGovernanceRepositoryPort
    handoff_proposals: HandoffProposalRepositoryPort
    handoff_transitions: HandoffTransitionRepositoryPort
    handoff_context_bundles: HandoffContextBundleRepositoryPort
    handoff_recovery_decisions: HandoffRecoveryDecisionRepositoryPort
    handoff_workspace_transfers: HandoffWorkspaceTransferRepositoryPort
    workspace_transfers: HandoffWorkspaceTransferRepositoryPort
    leases: LeaseStorePort
    planning_profiles: PlanningProfileRepositoryPort
    planning_requests: PlanningRequestRepositoryPort
    planning_runs: PlanningRunRepositoryPort
    planning_leases: PlanningLeaseRepositoryPort
    planning_records: PlanningRecordRepositoryPort
    proposal_materializations: ProposalMaterializationRepositoryPort
    sessions: SessionRepositoryPort
    modules: ModuleRepositoryPort
    bindings: ExecutionBindingRepositoryPort
    gateway_operations: GatewayOperationRepositoryPort
    workspaces: WorkspaceRepositoryPort
    execution_events: ExecutionEventRepositoryPort
    artifacts: ArtifactRepositoryPort
    evidence_claims: EvidenceClaimRepositoryPort
    execution_inputs: ExecutionInputRepositoryPort
    commands: CommandRepositoryPort
    events: EventStorePort
    work_contexts: WorkContextRepositoryPort
    context_assemblies: ContextAssemblyRepositoryPort
    context_packages: ContextPackageRepositoryPort
    knowledge_candidate_submissions: KnowledgeCandidateSubmissionRepositoryPort
    notifications: NotificationLedger
    notification_observations: NotificationObservationStore
    results: ResultRepositoryPort

    def __init__(
        self, database: Database, *, projection_change_retention_seconds: int = 604_800
    ) -> None:
        self._database = database
        self._projection_change_retention_seconds = projection_change_retention_seconds

    def __enter__(self) -> Self:
        self.session = Session(self._database.engine, expire_on_commit=False)
        self.tasks = TaskRepository(self.session)
        self.plans = PlanRepository(self.session)
        self.plan_transitions = PlanTransitionRepository(self.session)
        self.worker_transitions = WorkerTransitionRepository(self.session)
        self.plan_entity_lineages = PlanEntityLineageRepository(self.session)
        self.context_patches = ContextPatchRepository(self.session)
        self.attention = AttentionAcknowledgementRepository(self.session)
        self.projection_changes = ProjectionChangePublisher(
            ProjectionChangeRepository(
                self.session,
                retention_seconds=self._projection_change_retention_seconds,
            )
        )
        self.workers = WorkerRepository(self.session)
        self.worker_items = WorkerItemRepository(self.session, self.projection_changes)
        self.worker_runtime_configurations = (
            SqlAlchemyWorkerRuntimeConfigurationRepository(self.session)
        )
        self.scheduling_blocks = SqlAlchemySchedulingBlockRepository(self.session)
        self.executions = ExecutionRepository(self.session)
        self.execution_requests = ExecutionRequestRepository(self.session)
        self.collaboration_runs = CollaborationRunRepository(self.session)
        self.collaboration_evaluations = CollaborationEvaluationRepository(
            self.session
        )
        self.collaboration_ledger = CollaborationLedgerRepository(self.session)
        self.input_selections = InputSelectionRepository(self.session)
        self.collaboration_governance = CollaborationGovernanceRepository(self.session)
        self.handoff_proposals = HandoffProposalRepository(self.session)
        self.handoff_transitions = HandoffTransitionRepository(self.session)
        self.handoff_context_bundles = HandoffContextBundleRepository(self.session)
        self.handoff_recovery_decisions = HandoffRecoveryDecisionRepository(
            self.session
        )
        self.handoff_workspace_transfers = HandoffWorkspaceTransferRepository(
            self.session
        )
        self.workspace_transfers = self.handoff_workspace_transfers
        self.leases = LeaseStore(self.session)
        self.planning_profiles = PlanningProfileRepository(self.session)
        self.planning_requests = PlanningRequestRepository(self.session)
        self.planning_runs = PlanningRunRepository(self.session)
        self.planning_leases = PlanningRunLeaseRepository(self.session)
        self.planning_records = PlanningRecordRepository(self.session)
        self.proposal_materializations = ProposalMaterializationRepository(
            self.session
        )
        self.sessions = SessionRepository(self.session)
        self.modules = ModuleRepository(self.session, self.projection_changes)
        self.bindings = ExecutionBindingRepository(self.session)
        self.gateway_operations = GatewayOperationRepository(self.session)
        self.workspaces = WorkspaceRepository(self.session)
        self.execution_events = ExecutionEventRepository(
            self.session, self.projection_changes
        )
        self.artifacts = ArtifactRepository(self.session, self.projection_changes)
        self.evidence_claims = EvidenceClaimRepository(
            self.session, self.projection_changes
        )
        self.execution_inputs = ExecutionInputRepository(self.session)
        self.commands = CommandRepository(self.session)
        self.events = EventRepository(self.session, self.projection_changes)
        self.work_contexts = WorkContextRepository(self.session)
        self.context_assemblies = ContextAssemblyRepository(self.session, self.events)
        self.context_packages = ContextPackageRepository(self.session, self.events)
        self.knowledge_candidate_submissions = KnowledgeCandidateSubmissionRepository(
            self.session
        )
        self.notifications = SqlAlchemyNotificationRepository(
            self.session, self.projection_changes
        )
        self.notification_observations = SqlAlchemyNotificationObservationStore(
            self.session
        )
        self.results = ResultRepository(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        except BaseException:
            self.session.rollback()
            raise
        finally:
            self.session.close()

    def flush(self) -> None:
        self.session.flush()

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        with self.session.begin_nested():
            yield
