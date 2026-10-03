from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sidera_agents.contracts import (
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    RuntimeRef,
    RuntimeSnapshot,
)
from sqlalchemy import func, select

from sidera_control_plane.application.assignments import (
    AssignmentCommandHandler,
    TrustedAssignmentIdentity,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.planning_commands import (
    PlanningCommandHandler,
    PlanningCommandTarget,
)
from sidera_control_plane.domain.agent_bindings import AssignmentDenied
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    PlanningRunModel,
    TaskModel,
)
from tests.support.native_agents import (
    AssignmentScope,
    PublicAgentCatalog,
    PublicAgentRuntimes,
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)
from tests.unit.test_planning_profiles import planning_profile

pytestmark = pytest.mark.integration


class PlanningScope(AssignmentScope):
    def __init__(self, workspace_id, principal_id, grant_id, *, policy, actions):
        super().__init__(workspace_id, principal_id, grant_id, actions=actions)
        self.policy, self.targets = policy, []
        self.root_scope = None
        self.context_scope = ()

    def require(self, grant_id, action, target):
        self.targets.append((action, target))
        super().require(grant_id, action, target)
        if isinstance(target, PlanningCommandTarget) and (
            (
                target.planning_profile_id,
                target.planning_profile_version,
                target.planner_agent_ref,
            )
            != (self.policy.id, self.policy.version, self.policy.planner_agent_ref)
            or target.context_refs != self.context_scope
            or (self.root_scope is not None and target.root_task_id != self.root_scope)
        ):
            raise AssignmentDenied("exact planning scope denied")


@pytest.fixture
def native_case(workspace_id, uow_factory, request):
    pin = ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=2)
    policy = planning_profile(
        workspace_id=workspace_id,
        execution_mode="native_profile",
        planner_agent_ref=pin,
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_secret_refs=(),
        proposal_schema_version=2,
        allowed_capabilities=("native_tools",),
        **getattr(request, "param", {}),
    )
    with uow_factory() as uow:
        uow.planning_profiles.add(policy)
    identity = TrustedAssignmentIdentity("human", uuid4())
    scope = PlanningScope(
        workspace_id,
        identity.principal_id,
        identity.grant_id,
        policy=policy,
        actions={
            "planning.submit",
            "planning.confirm",
            "planning.read",
            "planning.write",
            "assignment.write",
            "planning.prepare",
            "source.read",
            "profile.read",
            "runtime.read",
            "workspace.execute",
            "run.start",
        },
    )
    runtime_ref = RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4())
    catalog = PublicAgentCatalog(
        (
            ProfileRevision(
                ref=pin,
                config_hash="fixture-profile",
                created_at=datetime.now(UTC),
                draft=ProfileDraft(
                    name="Shared Planner and Worker",
                    description="",
                    runtime_ref=runtime_ref,
                    instructions="",
                    resource_refs=(),
                    model_id=None,
                    thinking=None,
                    max_active=2,
                    environment={},
                    secret_refs=(),
                    native_settings={"native_tools": "write"},
                ),
            ),
        )
    )
    runtimes = PublicAgentRuntimes(
        RuntimeSnapshot(
            ref=runtime_ref,
            harness_id="public-fixture",
            executable="/unused",
            actual_version="fixture-1",
            installed=True,
            enabled=True,
            version=1,
            protocol_state="ready",
            auth_state="unconfirmed",
            capabilities={
                "native_tools": {
                    "status": "supported",
                    "reason_code": "fixture_declared",
                    "constraints": {},
                }
            },
            checked_at=datetime.now(UTC),
            limits={},
        )
    )
    assignments = AssignmentCommandHandler(
        catalog=catalog, runtimes=runtimes, identity=identity, authorizer=scope
    )
    handler = PlanningCommandHandler(
        local_actor_type="user",
        local_actor_id="human",
        assignment_handler=assignments,
        identity=identity,
        authorizer=scope,
    )
    service = CommandService(uow_factory, (handler,), preflight=handler.preflight)
    envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type="SubmitTaskGoal",
        target_type="workspace",
        target_id=None,
        actor_type="user",
        actor_id="human",
        expected_version=None,
        idempotency_key=str(uuid4()),
        correlation_id=uuid4(),
        payload={
            "title": "Native planning goal",
            "objective": "Plan the requested work",
            "constraints": ["Keep responsibilities explicit"],
            "acceptance_criteria": ["Approved work has one stable root"],
            "requested_outputs": ["proposal"],
            "context_refs": [],
            "planning_profile_id": str(policy.id),
            "planning_profile_version": policy.version,
            "planner_agent_ref": pin.model_dump(mode="json"),
            "source_ref": None,
        },
    )
    return policy, identity, scope, catalog, runtimes, service, envelope


@pytest.mark.parametrize("entry", ["SubmitTaskGoal", "SubmitPlanningRequest"])
def test_native_submission_scope_precedes_receipt_replay(
    native_case, workspace_id, uow_factory, entry
):
    from sidera_control_plane.application.command_service import hash_request

    policy, _identity, scope, _catalog, _runtimes, service, envelope = native_case
    if entry == "SubmitPlanningRequest":
        envelope = replace(
            envelope,
            command_type=entry,
            payload={
                key: value
                for key, value in envelope.payload.items()
                if key
                not in {
                    "title",
                    "planning_profile_id",
                    "planning_profile_version",
                    "planner_agent_ref",
                }
            }
            | {
                "profile_id": str(policy.id),
                "profile_version": policy.version,
                "source_type": "user",
            },
        )
    receipt = service.handle(envelope)
    assert service.handle(envelope) == receipt
    targets = [
        target for action, target in scope.targets if action == "planning.submit"
    ]
    assert targets
    assert all(
        target.planning_request_id is None
        and target.context_refs == ()
        and target.planner_agent_ref == policy.planner_agent_ref
        for target in targets
    )
    with pytest.raises(AssignmentDenied):
        service.handle(replace(envelope, actor_id="forged"))
    scope.revoked = True
    with pytest.raises(AssignmentDenied):
        service.handle(envelope)
    with pytest.raises(AssignmentDenied):
        service._recover_concurrent(envelope, hash_request(envelope))
    with uow_factory() as uow:
        assert (
            uow.planning_requests.get(
                UUID(receipt.data["planning_request_id"]), workspace_id
            )
            is not None
        )


@pytest.mark.parametrize("entry", ["SubmitTaskGoal", "SubmitPlanningRequest"])
def test_direct_native_handler_denies_uninjected_identity(
    native_case, uow_factory, entry
):
    policy, _identity, _scope, _catalog, _runtimes, _service, envelope = native_case
    if entry == "SubmitPlanningRequest":
        envelope = replace(
            envelope,
            command_type=entry,
            payload={"profile_id": str(policy.id), "profile_version": policy.version},
        )
    handler = PlanningCommandHandler(local_actor_type="user", local_actor_id="human")
    with uow_factory() as uow, pytest.raises(AssignmentDenied):
        handler.handle(uow, envelope)


def test_native_request_command_authorizes_real_submitted_root_and_replays_old_version(
    native_case, workspace_id, uow_factory
):
    _policy, _identity, scope, _catalog, _runtimes, service, envelope = native_case
    submitted = service.handle(envelope)
    scope.root_scope = submitted.aggregate_id
    cancel = replace(
        envelope,
        id=uuid4(),
        command_type="CancelPlanningRequest",
        target_type="planning_request",
        target_id=UUID(submitted.data["planning_request_id"]),
        expected_version=1,
        idempotency_key=str(uuid4()),
        payload={"reason": "cancel"},
    )
    receipt = service.handle(cancel)
    assert service.handle(cancel) == receipt
    target = next(
        target for action, target in scope.targets if action == "planning.write"
    )
    assert target.root_task_id == submitted.aggregate_id
    assert target.planning_request_id == cancel.target_id
    with uow_factory() as uow:
        assert (
            uow.tasks.get(submitted.aggregate_id, workspace_id).status.value
            == "pending"
        )


@pytest.mark.parametrize(
    "command_type",
    [
        "CancelPlanningRequest",
        "AnswerPlanningClarification",
        "RequestProposalRevision",
        "RejectPlanningProposal",
        "ConfirmPlanningProposal",
        "ConfirmPlanRevision",
    ],
)
@pytest.mark.parametrize(
    "target_type", ["task", "planning_run", "workspace", "missing_id"]
)
def test_saved_native_request_commands_reject_mislabeled_targets_before_lookup(
    native_case, uow_factory, monkeypatch, command_type, target_type
):
    from sidera_control_plane.application.command_service import hash_request
    from sidera_control_plane.domain.errors import ValidationError
    from sidera_control_plane.persistence.planning_repository import (
        PlanningRequestRepository,
    )
    from sidera_control_plane.persistence.repositories import CommandRepository

    _policy, _identity, _scope, _catalog, _runtimes, service, envelope = native_case
    submitted = service.handle(envelope)
    malformed = replace(
        envelope,
        id=uuid4(),
        command_type=command_type,
        target_type="planning_request" if target_type == "missing_id" else target_type,
        target_id=None
        if target_type == "missing_id"
        else UUID(submitted.data["planning_request_id"]),
        expected_version=1,
        idempotency_key=str(uuid4()),
        payload={},
    )

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("malformed request target reached lookup")

    monkeypatch.setattr(PlanningRequestRepository, "get", unexpected_lookup)
    monkeypatch.setattr(CommandRepository, "find_by_id", unexpected_lookup)
    monkeypatch.setattr(CommandRepository, "find_duplicate", unexpected_lookup)
    for call in (
        lambda: service.handle(malformed),
        lambda: service._recover_concurrent(malformed, hash_request(malformed)),
    ):
        with pytest.raises(ValidationError):
            call()
    trusted = service._handlers[command_type]
    uninjected = PlanningCommandHandler(local_actor_type="user", local_actor_id="human")
    for handler in (trusted, uninjected):
        with uow_factory() as uow, pytest.raises(ValidationError):
            handler.handle(uow, replace(malformed, actor_id="forged"))


def test_saved_native_request_old_receipt_replay_rechecks_current_scope(
    native_case, workspace_id, uow_factory, monkeypatch
):
    from sidera_control_plane.application.command_service import hash_request
    from sidera_control_plane.persistence.repositories import CommandRepository

    _policy, _identity, scope, _catalog, _runtimes, service, envelope = native_case
    submitted = service.handle(envelope)
    scope.root_scope = submitted.aggregate_id
    cancel = replace(
        envelope,
        id=uuid4(),
        command_type="CancelPlanningRequest",
        target_type="planning_request",
        target_id=UUID(submitted.data["planning_request_id"]),
        expected_version=1,
        idempotency_key=str(uuid4()),
        payload={},
    )
    receipt = service.handle(cancel)
    assert service.handle(cancel) == receipt
    assert service._recover_concurrent(cancel, hash_request(cancel)) == receipt

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("denied request replay reached receipt lookup")

    monkeypatch.setattr(CommandRepository, "find_by_id", unexpected_lookup)
    monkeypatch.setattr(CommandRepository, "find_duplicate", unexpected_lookup)
    with pytest.raises(AssignmentDenied):
        service.handle(replace(cancel, actor_id="forged"))
    scope.revoked = True
    with pytest.raises(AssignmentDenied):
        service.handle(cancel)
    with pytest.raises(AssignmentDenied):
        service._recover_concurrent(cancel, hash_request(cancel))


class PublicPlanningExecution:
    def __init__(self, runtime_ref, body):
        self.runtime_ref, self.body = runtime_ref, body
        self.requests, self.lookups, self.stops = [], [], []
        self.run_id, self.response_id = uuid4(), uuid4()
        self.cursors = []

    def event_values(self):
        return (
            ("response.started", {}),
            (
                "progress.updated",
                {"update_type": "agent_thought_chunk", "text": "private thought"},
            ),
            (
                "progress.updated",
                {"update_type": "agent_message_chunk", "text": self.body},
            ),
            ("response.completed", {}),
            ("run.closed", {"status": "completed"}),
        )

    def start(self, request):
        from sidera_agents.contracts import StartReceipt

        self.requests.append(request)
        return StartReceipt(
            operation_id=request.operation_id,
            status="accepted",
            run_id=self.run_id,
            reason_code=None,
        )

    def get_run(self, workspace_id, run_id, *, grant_id):
        from sidera_agents.contracts import EffectiveConfig, RunSnapshot

        request = self.requests[-1]
        assert (workspace_id, run_id, grant_id) == (
            request.profile_ref.workspace_id,
            self.run_id,
            request.grant_id,
        )
        return RunSnapshot(
            run_id=self.run_id,
            workspace_id=workspace_id,
            profile_ref=request.profile_ref,
            policy="bounded",
            state="completed",
            input_open=False,
            active_response_id=None,
            effective_config=EffectiveConfig(
                profile_ref=request.profile_ref,
                config_hash="fixture-profile",
                runtime_ref=self.runtime_ref,
                actual_runtime_version="fixture-1",
                selected_model=None,
                observed_model=None,
                inherited_fields=(),
                applied_fields=(),
                resource_refs=(),
                context_receipts=(),
            ),
            last_event_sequence=len(self.event_values()),
            capabilities={},
            reason_code=None,
        )

    def list_events(self, workspace_id, run_id, after_sequence, limit=100, *, grant_id):
        from sidera_agents.contracts import EventPage, RunEvent

        request = self.requests[-1]
        assert (workspace_id, run_id, grant_id) == (
            request.profile_ref.workspace_id,
            self.run_id,
            request.grant_id,
        )
        self.cursors.append(after_sequence)
        values = self.event_values()
        events = tuple(
            RunEvent(
                run_id=run_id,
                sequence=i,
                kind=kind,
                occurred_at=datetime.now(UTC),
                operation_id=request.operation_id,
                response_id=self.response_id if kind != "run.closed" else None,
                data=data,
            )
            for i, (kind, data) in enumerate(values, 1)
            if i > after_sequence
        )
        visible = events[:limit]
        return EventPage(
            events=visible,
            next_sequence=visible[-1].sequence if visible else after_sequence,
            has_more=len(events) > limit,
        )


def run_native_case(
    native_case,
    uow_factory,
    tmp_path,
    body,
    *,
    execution_factory=PublicPlanningExecution,
    runner_options=None,
    workspace_provider=None,
    **native_options,
):
    from sidera_control_plane.adapters.planning_workspace import (
        PrivatePlanningWorkspaceProvider,
    )
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeWorkerServices,
    )
    from sidera_control_plane.application.context_materialization import (
        ContextMaterializer,
    )
    from sidera_control_plane.application.execution_authority import (
        NativeAuthorityReader,
        PlanningAuthorityReader,
    )
    from sidera_control_plane.application.planning_runner import (
        PlanningRunner,
        RegistryPlannerAdapterFactory,
    )
    from sidera_control_plane.context_package_worker import ContextPackageWorker
    from tests.integration.test_context_package_worker import EmptyCatalog

    _policy, identity, scope, catalog, runtimes, service, envelope = native_case
    submitted = service.handle(envelope)
    knowledge = EmptyCatalog()
    assembled = ContextPackageWorker(
        uow_factory=uow_factory, catalog=knowledge
    ).run_once()
    with uow_factory() as uow:
        saved = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), envelope.workspace_id
        )
        assembly_error = saved.runs[-1].error_code
    assert assembled.ready == 1, assembly_error
    reader = PlanningAuthorityReader(
        uow_factory,
        principal=lambda: identity.principal_id,
        authorizer=scope,
        clock=native_options.get("native_clock"),
    )
    facade = NativeAuthorityReader(uow_factory, planner=reader, worker=None)
    execution = execution_factory(runtimes.runtime.ref, body)
    services = NativeWorkerServices(
        catalog, runtimes, execution, facade, identity.principal_id, identity.grant_id
    )
    factory = RegistryPlannerAdapterFactory(
        {},
        native_services=services,
        uow_factory=uow_factory,
        planning_workspace_provider=workspace_provider
        or PrivatePlanningWorkspaceProvider(tmp_path / "scratch"),
        planning_context_materializer=ContextMaterializer(knowledge),
        planning_authorizer=scope,
        **native_options,
    )
    result = PlanningRunner(
        uow_factory=uow_factory,
        adapter_factory=factory,
        owner_id="native-planner",
        clock=native_options.get("native_clock"),
        context_materializer=ContextMaterializer(knowledge),
        **(runner_options or {}),
    ).run_once()
    return submitted, execution, result


def native_proposal_body(envelope, *, unchanged_root=False):
    import json

    from sidera_control_plane.application.planner_contract import _proposal_shape_hint

    body = _proposal_shape_hint(2)
    body.pop("usage")
    body.pop("knowledge_candidates")
    proposal = body["proposal"]
    for item in (*proposal["worker_items"], *proposal["execution_intents"]):
        item["capability_requirements"] = ["native_tools"]
    for item in proposal["execution_intents"]:
        item["adapter_preferences"] = []
    if unchanged_root:
        proposal["root_task"] = {
            "title": envelope.payload["title"],
            "goal_summary": envelope.payload["objective"],
            "acceptance_refs": envelope.payload["acceptance_criteria"],
        }
    return json.dumps(body)


def native_confirmation(envelope, request, proposal, choices):
    return replace(
        envelope,
        id=uuid4(),
        command_type="ConfirmPlanningProposal",
        target_type="planning_request",
        target_id=request.id,
        expected_version=request.version,
        idempotency_key=str(uuid4()),
        payload={
            "proposal_revision": proposal.revision,
            "proposal_content_hash": proposal.content_hash,
            "profile_id": str(request.planning_profile_id),
            "profile_version": request.planning_profile_version,
            "assignee_bindings": choices,
        },
    )


def native_choices(policy, identity, proposal):
    return [
        {
            "target_type": "subtask",
            "target_key": item.key,
            "assignee_kind": "human",
            "assignee_id": identity.principal_id,
            "profile_revision": None,
            "expected_binding_version": None,
        }
        for item in proposal.payload.subtasks
    ] + [
        {
            "target_type": "worker_item",
            "target_key": item.key,
            "assignee_kind": "profile",
            "assignee_id": str(policy.planner_agent_ref.profile_id),
            "profile_revision": policy.planner_agent_ref.revision,
            "expected_binding_version": None,
        }
        for item in proposal.payload.worker_items
    ]


def test_native_revision_runner_preserves_reuse_and_binds_only_new_targets(
    native_case, workspace_id, uow_factory, tmp_path
):
    import json

    from sidera_control_plane.application.planner_contract import (
        _revision_proposal_shape_hint,
    )
    from sidera_control_plane.application.revision_planning_context import (
        decode_native_revision_facts,
    )
    from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
    from sidera_control_plane.domain.plans import TaskPlanSnapshot
    from sidera_control_plane.persistence.models import ContextPackageModel

    policy, identity, scope, _catalog, _runtimes, service, envelope = native_case
    submitted, first_execution, initial_outcome = run_native_case(
        native_case, uow_factory, tmp_path, native_proposal_body(envelope)
    )
    assert initial_outcome.succeeded == 1
    with uow_factory() as uow:
        initial = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        proposal = uow.planning_records.get_current_proposal(initial.id, workspace_id)
    confirmed = service.handle(
        native_confirmation(
            envelope, initial, proposal, native_choices(policy, identity, proposal)
        )
    )
    root_id = UUID(confirmed.data["root_task_id"])
    old_subtask = UUID(confirmed.data["subtask_task_mapping"]["subtask-one"])
    old_worker = UUID(confirmed.data["worker_item_mapping"]["worker-one"])
    revision_policy = replace(
        policy,
        id=uuid4(),
        profile_key="native-revision",
        version=1,
        proposal_schema_version=3,
    )
    with uow_factory() as uow:
        root = uow.tasks.get(root_id, workspace_id)
        head = uow.plans.get_head(root_id, workspace_id)
        preserved = (
            uow.agent_bindings.get(workspace_id, "subtask", old_subtask),
            uow.agent_bindings.get(workspace_id, "worker_item", old_worker),
        )
        uow.planning_profiles.add(revision_policy)
    scope.policy, scope.root_scope = revision_policy, root_id
    revision_envelope = replace(
        envelope,
        id=uuid4(),
        command_type="SubmitPlanRevision",
        target_type="task",
        target_id=root_id,
        expected_version=root.version,
        idempotency_key=str(uuid4()),
        payload={
            "root_task_id": str(root_id),
            "expected_base_snapshot_id": str(head.active_snapshot_id),
            "expected_base_generation": head.active_generation,
            "expected_base_content_hash": head.active_snapshot_hash,
            "objective": "Revise the plan without replacing owners",
            "constraints": [],
            "acceptance_criteria": ["acceptance:one"],
            "planning_profile_id": str(revision_policy.id),
            "planning_profile_version": revision_policy.version,
        },
    )
    revision_submitted = service.handle(revision_envelope)
    assert service.handle(revision_envelope) == revision_submitted
    with uow_factory() as uow:
        revision_request = uow.planning_requests.get(
            revision_submitted.aggregate_id, workspace_id
        )
    body = _revision_proposal_shape_hint(3)
    body.pop("usage")
    payload = body["proposal"]
    approved = proposal.payload.to_mapping()
    for field in ("root_task", "subtasks", "worker_items", "execution_intents"):
        payload[field] = approved[field]
    payload["base_descriptor"] = revision_request.base_descriptor.to_mapping()
    payload["subtask_identity_decisions"][0]["existing_subtask_task_id"] = str(
        old_subtask
    )
    payload["worker_item_identity_decisions"][0]["existing_worker_item_id"] = str(
        old_worker
    )
    payload["subtasks"].append(
        {
            **payload["subtasks"][0],
            "key": "subtask-two",
            "title": "new independent subtask",
        }
    )
    payload["worker_items"].append(
        {
            **payload["worker_items"][0],
            "key": "worker-two",
            "subtask_key": "subtask-two",
            "title": "new manual child",
        }
    )
    payload["execution_intents"].append(
        {**payload["execution_intents"][0], "worker_item_key": "worker-two"}
    )
    payload["subtask_identity_decisions"].append(
        {
            "target_subtask_key": "subtask-two",
            "strategy": "create",
            "existing_subtask_task_id": None,
            "derived_from_subtask_task_ids": [],
            "reason": "new independent work",
        }
    )
    payload["worker_item_identity_decisions"].append(
        {
            "target_worker_item_key": "worker-two",
            "strategy": "create",
            "existing_worker_item_id": None,
            "derived_from_worker_item_ids": [],
            "target_subtask_key": "subtask-two",
            "context_disposition": "unchanged",
            "change_reasons": ["new work"],
        }
    )
    for item in (*payload["worker_items"], *payload["execution_intents"]):
        item["capability_requirements"] = ["native_tools"]
    for intent in payload["execution_intents"]:
        intent["adapter_preferences"] = []
    revision_case = (*native_case[:-1], revision_envelope)
    _same_receipt, revision_execution, revision_outcome = run_native_case(
        revision_case, uow_factory, tmp_path, json.dumps(body)
    )
    assert revision_outcome.succeeded == 1, tuple(
        item.code for item in revision_outcome.outcomes
    )
    assert len(first_execution.requests) == len(revision_execution.requests) == 1
    assert first_execution.run_id != revision_execution.run_id
    assert (
        first_execution.requests[0].profile_ref
        == revision_execution.requests[0].profile_ref
        == policy.planner_agent_ref
    )
    with uow_factory() as uow:
        revision_request = uow.planning_requests.get(revision_request.id, workspace_id)
        revision_proposal = uow.planning_records.get_current_proposal(
            revision_request.id, workspace_id
        )
        package = uow.session.scalars(
            select(ContextPackageModel).where(
                ContextPackageModel.consumer_id == revision_request.current_run_id
            )
        ).one()
        graph = package.control_plane_fact_snapshot["revision_base"]
        assert graph["encoding_version"] == 1 and graph["base_descriptor"][
            "plan_id"
        ] == str(head.active_snapshot_id)
        assert (
            "plan_snapshot" not in graph
            and "plan_snapshot_id" not in graph["base_descriptor"]
        )
        assert (
            graph["base_descriptor"]["plan_content_hash"] == head.active_snapshot_hash
        )
        restored = decode_native_revision_facts(graph)
        assert (
            plan_snapshot_content_hash(
                TaskPlanSnapshot.from_payload(restored["plan_snapshot"])
            )
            == head.active_snapshot_hash
        )
        assert graph["context_package_refs"] == []
    choices = [
        {
            "target_type": "subtask",
            "target_key": "subtask-two",
            "assignee_kind": "profile",
            "assignee_id": str(policy.planner_agent_ref.profile_id),
            "profile_revision": policy.planner_agent_ref.revision,
            "expected_binding_version": None,
        },
        {
            "target_type": "worker_item",
            "target_key": "worker-two",
            "assignee_kind": "human",
            "assignee_id": identity.principal_id,
            "profile_revision": None,
            "expected_binding_version": None,
        },
    ]
    confirmation = replace(
        revision_envelope,
        id=uuid4(),
        command_type="ConfirmPlanRevision",
        target_type="planning_request",
        target_id=revision_request.id,
        expected_version=revision_request.version,
        idempotency_key=str(uuid4()),
        payload={
            "expected_proposal_revision": revision_proposal.revision,
            "expected_proposal_content_hash": revision_proposal.content_hash,
            "expected_diff_hash": revision_proposal.derived_diff.diff_hash,
            "expected_base_snapshot_id": str(head.active_snapshot_id),
            "expected_base_content_hash": head.active_snapshot_hash,
            "profile_id": str(revision_policy.id),
            "profile_version": revision_policy.version,
            "assignee_bindings": choices,
        },
    )
    receipt = service.handle(confirmation)
    assert (
        service.handle(confirmation) == receipt and receipt.data["plan_generation"] == 2
    )
    assert receipt.data["subtask_task_mapping"]["subtask-one"] == str(old_subtask)
    assert receipt.data["worker_item_mapping"]["worker-one"] == str(old_worker)
    with uow_factory() as uow:
        assert (
            uow.agent_bindings.get(workspace_id, "subtask", old_subtask) == preserved[0]
        )
        assert (
            uow.agent_bindings.get(workspace_id, "worker_item", old_worker)
            == preserved[1]
        )
        new_subtask = uow.agent_bindings.get(
            workspace_id,
            "subtask",
            UUID(receipt.data["subtask_task_mapping"]["subtask-two"]),
        )
        new_worker = uow.agent_bindings.get(
            workspace_id,
            "worker_item",
            UUID(receipt.data["worker_item_mapping"]["worker-two"]),
        )
        assert (
            new_subtask.profile_ref == policy.planner_agent_ref
            and new_subtask.version == 1
        )
        assert (
            new_worker.assignee.kind == "human"
            and new_worker.version == 1
            and not new_worker.runnable
        )
        assert uow.session.scalar(select(func.count()).select_from(ExecutionModel)) == 0
    scope.revoked = True
    with pytest.raises(AssignmentDenied):
        service.handle(confirmation)


class SplitPlanningExecution(PublicPlanningExecution):
    def event_values(self):
        split = len(self.body) // 2
        return (
            ("response.started", {}),
            *(
                (
                    "progress.updated",
                    {"update_type": "tool_call", "text": "ignored tool output"},
                )
                for _ in range(98)
            ),
            (
                "progress.updated",
                {"update_type": "agent_message_chunk", "text": self.body[:split]},
            ),
            (
                "progress.updated",
                {"update_type": "agent_message_chunk", "text": self.body[split:]},
            ),
            ("response.completed", {}),
            ("usage", {"usage": {"input_tokens": 7, "output_tokens": 5}}),
            ("run.closed", {"status": "completed"}),
        )


class BusyPlanningExecution(PublicPlanningExecution):
    busy_count = 1

    def start(self, request):
        from sidera_agents.contracts import StartReceipt

        if len(self.requests) < self.busy_count:
            self.requests.append(request)
            return StartReceipt(
                operation_id=request.operation_id,
                status="busy",
                run_id=None,
                reason_code="profile_capacity",
            )
        return super().start(request)


class UnknownPlanningExecution(PublicPlanningExecution):
    eventual_receipt = False

    def start(self, request):
        self.requests.append(request)
        raise TimeoutError("synthetic lost start acknowledgement")

    def get_start(self, workspace_id, operation_id, *, grant_id):
        from sidera_agents.contracts import StartLookup, StartReceipt

        request = self.requests[0]
        assert (workspace_id, operation_id, grant_id) == (
            request.profile_ref.workspace_id,
            request.operation_id,
            request.grant_id,
        )
        self.lookups.append((workspace_id, operation_id, grant_id))
        if len(self.lookups) == 2:
            raise TimeoutError("synthetic lookup timeout")
        if self.eventual_receipt and len(self.lookups) > 2:
            return StartLookup(
                found=True,
                receipt=StartReceipt(
                    operation_id=operation_id,
                    status="accepted",
                    run_id=self.run_id,
                    reason_code=None,
                ),
            )
        return StartLookup(found=False, receipt=None)


class OpenPlanningExecution(PublicPlanningExecution):
    closed_on_stop = False

    def event_values(self):
        return (*super().event_values()[:-1], ("run.closing", {}))

    def get_run(self, workspace_id, run_id, *, grant_id):
        from sidera_agents.contracts import RunSnapshot

        snapshot = super().get_run(workspace_id, run_id, grant_id=grant_id)
        return RunSnapshot.model_validate(
            {
                **snapshot.model_dump(),
                "state": "stopped" if self.stops and self.closed_on_stop else "closing",
            }
        )

    def stop(self, request):
        from sidera_agents.contracts import StopReceipt

        original = self.requests[0]
        assert (
            request.workspace_id,
            request.run_id,
            request.operation_id,
            request.grant_id,
        ) == (
            original.profile_ref.workspace_id,
            self.run_id,
            original.operation_id,
            original.grant_id,
        )
        self.stops.append(request)
        return StopReceipt(
            operation_id=request.operation_id,
            run_id=request.run_id,
            status="accepted",
            reason_code=None,
            evidence_sequence=None,
        )


def test_native_busy_wait_retains_one_claim_and_renews_keeper(
    native_case, workspace_id, uow_factory, database, tmp_path, monkeypatch
):
    import threading

    from sidera_control_plane.application.planning_runner import PlanningLeaseKeeper

    renewed = threading.Event()
    original_renew = PlanningLeaseKeeper.renew_once

    def renew(keeper):
        result = original_renew(keeper)
        with uow_factory() as uow:
            link = uow.planning_authorities.get_link(
                workspace_id, keeper._planning_run_id
            )
            if result and link is not None and link.status == "waiting_capacity":
                renewed.set()
        return result

    monkeypatch.setattr(PlanningLeaseKeeper, "renew_once", renew)
    observed = []

    def wait(seconds):
        assert seconds > 0
        assert renewed.wait(2), (
            "actual independent lease renewal did not occur during BUSY"
        )
        with uow_factory() as uow:
            run = uow.session.scalars(select(PlanningRunModel)).one()
            link = uow.planning_authorities.get_link(workspace_id, run.id)
            lease = uow.planning_leases.get_active_for_run(run.id, workspace_id)
            assert (
                link.status == "waiting_capacity"
                and link.reason_code == "profile_capacity"
            )
            assert run.status == "running" and run.attempt == 1 and lease.version > 1
            observed.append(
                (
                    run.id,
                    run.operation_id,
                    lease.id,
                    link.authority_id,
                    link.start_request.workspace,
                )
            )
            from sidera_control_plane.persistence.planning_query_store import (
                SqlAlchemyPlanningQueryStore,
            )

            history = SqlAlchemyPlanningQueryStore(database).list_runs(
                workspace_id, run.planning_request_id
            )
            assert history[0].execution_mode == "native_profile"
            assert history[0].planner_agent_ref == native_case[0].planner_agent_ref
            assert (
                history[0].native_run.status == "waiting_capacity"
                and history[0].native_run.run_id is None
            )
            assert history[0].native_run.reason_code == "profile_capacity"

    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        native_proposal_body(native_case[-1]),
        execution_factory=BusyPlanningExecution,
        native_wait=wait,
        runner_options={"lease_renew_interval_seconds": 0.01},
    )
    assert outcome.succeeded == 1 and len(observed) == 1
    assert (
        len(execution.requests) == 2 and execution.requests[0] == execution.requests[1]
    )
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        run = request.runs[-1]
        link = uow.planning_authorities.get_link(workspace_id, run.id)
        assert len(request.runs) == 1
        assert observed[0][0:2] == (run.id, run.operation_id)
        assert observed[0][3:] == (link.authority_id, link.start_request.workspace)
    assert not any(t.name.startswith("planning-lease-") for t in threading.enumerate())


@pytest.mark.parametrize(
    "native_case",
    [{"budget": replace(planning_profile().budget, max_runtime_seconds=3)}],
    indirect=True,
)
def test_native_unknown_admission_uses_only_original_lookup_and_blocks_manual_retry(
    native_case, workspace_id, uow_factory, tmp_path
):
    from sidera_control_plane.application.planning_reconciler import PlanningReconciler
    from sidera_control_plane.domain.errors import DomainError, ValidationError

    now = [datetime.now(UTC) + timedelta(minutes=1)]
    waits = []

    def wait(seconds):
        assert seconds > 0
        waits.append(seconds)
        now[0] += timedelta(seconds=seconds)

    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        "{}",
        execution_factory=UnknownPlanningExecution,
        native_clock=lambda: now[0],
        native_wait=wait,
    )
    assert (
        outcome.blocked == 1 and outcome.outcomes[0].code == "native_admission_unknown"
    )
    assert (
        len(execution.requests) == 1 and len(execution.lookups) == 2 and len(waits) == 3
    )
    _policy, _identity, _scope, _catalog, _runtimes, service, envelope = native_case
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        run = request.runs[-1]
        link = uow.planning_authorities.get_link(workspace_id, run.id)
        assert link.status == "unknown" and not link.closed
        assert run.usage.input_tokens is None and run.usage.cost_microunits is None
        from pathlib import Path

        assert Path(link.start_request.workspace.directory).is_dir()
    retry = replace(
        envelope,
        id=uuid4(),
        command_type="RetryBlockedPlanningRun",
        target_type="planning_run",
        target_id=run.id,
        expected_version=run.version,
        idempotency_key=str(uuid4()),
        payload={"unknown_outcome_decision": "retry"},
    )
    with uow_factory() as uow, pytest.raises(ValidationError):
        service._handlers["RetryBlockedPlanningRun"].handle(
            uow, replace(retry, target_type="task")
        )
    with pytest.raises(DomainError) as denied:
        service.handle(retry)
    assert denied.value.code == "native_cleanup_pending"
    recovery = PlanningReconciler(
        uow_factory=uow_factory, owner_id="native-reconciler", clock=lambda: now[0]
    ).run_once()
    assert recovery.retried == 0
    with uow_factory() as uow:
        assert len(uow.planning_requests.get(request.id, workspace_id).runs) == 1


def test_native_expired_original_lease_blocks_without_replacement(
    native_case, workspace_id, uow_factory, tmp_path
):
    from sidera_control_plane.application.planning_reconciler import PlanningReconciler

    now = [datetime.now(UTC) + timedelta(minutes=1)]

    class ExpiredPlanningExecution(UnknownPlanningExecution):
        def start(self, request):
            with uow_factory() as uow:
                run = uow.session.scalars(select(PlanningRunModel)).one()
                lease = uow.planning_leases.get_active_for_run(
                    run.id, workspace_id, for_update=True
                )
                before = lease.version
                lease.expires_at = now[0]
                lease.version += 1
                uow.planning_leases.save(lease, before)
            now[0] += timedelta(seconds=1)
            return super().start(request)

    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        "{}",
        execution_factory=ExpiredPlanningExecution,
        native_clock=lambda: now[0],
        native_wait=lambda _: None,
    )
    assert outcome.outcomes[0].status == "stale"
    recovery = PlanningReconciler(
        uow_factory=uow_factory, owner_id="native-reconciler", clock=lambda: now[0]
    ).run_once()
    assert recovery.expired == 1 and recovery.blocked == 1 and recovery.retried == 0
    assert len(execution.requests) == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        assert len(request.runs) == 1
        run = request.runs[0]
        assert (
            run.status.value == "blocked" and run.error_code == "native_lease_expired"
        )
        link = uow.planning_authorities.get_link(workspace_id, run.id)
        assert link.start_operation_id == run.operation_id and not link.closed


def test_native_lost_start_ack_eventually_observes_only_original_run(
    native_case, workspace_id, uow_factory, tmp_path
):
    class EventuallyFound(UnknownPlanningExecution):
        eventual_receipt = True

    now = [datetime.now(UTC) + timedelta(minutes=1)]

    def wait(seconds):
        assert seconds > 0
        now[0] += timedelta(seconds=seconds)

    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        native_proposal_body(native_case[-1]),
        execution_factory=EventuallyFound,
        native_clock=lambda: now[0],
        native_wait=wait,
    )
    assert (
        outcome.succeeded == 1
        and len(execution.requests) == 1
        and len(execution.lookups) == 3
    )
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        assert len(request.runs) == 1
        link = uow.planning_authorities.get_link(workspace_id, request.runs[0].id)
        assert link.run_id == execution.run_id and link.closed
        assert request.runs[0].usage.input_tokens is None


@pytest.mark.parametrize(
    "native_case",
    [
        {
            "budget": replace(planning_profile().budget, max_runtime_seconds=2),
            "retry_policy": replace(
                planning_profile().retry_policy, max_retries=0, backoff_seconds=()
            ),
        }
    ],
    indirect=True,
)
def test_native_empty_backoff_capacity_timeout_never_changes_attempt(
    native_case, workspace_id, uow_factory, tmp_path
):
    from sidera_control_plane.application.planning_reconciler import PlanningReconciler

    class AlwaysBusy(BusyPlanningExecution):
        busy_count = 100

    now = [datetime.now(UTC) + timedelta(minutes=1)]
    waits = []

    def wait(seconds):
        assert seconds > 0
        waits.append(seconds)
        now[0] += timedelta(seconds=seconds)

    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        "{}",
        execution_factory=AlwaysBusy,
        native_clock=lambda: now[0],
        native_wait=wait,
    )
    assert outcome.failed == 1 and outcome.outcomes[0].code == "native_capacity_timeout"
    assert waits == [1.0, 1.0] and len(execution.requests) == 2 and not execution.stops
    assert execution.requests[0] == execution.requests[1]
    assert (
        PlanningReconciler(
            uow_factory=uow_factory, owner_id="native-reconciler", clock=lambda: now[0]
        )
        .run_once()
        .retried
        == 0
    )
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        assert len(request.runs) == 1
        assert uow.planning_authorities.get_link(
            workspace_id, request.runs[0].id
        ).not_admitted


@pytest.mark.parametrize(
    "native_case",
    [{"budget": replace(planning_profile().budget, max_runtime_seconds=2)}],
    indirect=True,
)
@pytest.mark.parametrize("closed", [False, True])
def test_native_deadline_retains_cleanup_until_original_run_closed(
    native_case, workspace_id, uow_factory, tmp_path, closed
):
    class DeadlineExecution(OpenPlanningExecution):
        closed_on_stop = closed

    now = [datetime.now(UTC) + timedelta(minutes=1)]

    def wait(seconds):
        assert seconds > 0
        now[0] += timedelta(seconds=seconds)

    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        native_proposal_body(native_case[-1]),
        execution_factory=DeadlineExecution,
        native_clock=lambda: now[0],
        native_wait=wait,
    )
    assert len(execution.requests) == 1 and len(execution.stops) == 1
    assert outcome.failed == int(closed) and outcome.blocked == int(not closed)
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        link = uow.planning_authorities.get_link(workspace_id, request.runs[0].id)
        assert len(request.runs) == 1
        assert link.closed is closed and link.status == (
            "closed" if closed else "cleanup_pending"
        )
        assert uow.planning_records.list_proposals(request.id, workspace_id) == ()


@pytest.mark.parametrize("closed", [False, True])
def test_native_dynamic_command_cancel_is_seen_by_actual_keeper_and_stops_original(
    native_case, workspace_id, uow_factory, tmp_path, monkeypatch, closed
):
    import threading
    from pathlib import Path

    from sidera_control_plane.application.planning_runner import PlanningLeaseKeeper

    class CancelExecution(OpenPlanningExecution):
        closed_on_stop = closed

    observed_cancel = threading.Event()
    original_renew = PlanningLeaseKeeper.renew_once

    def renew(keeper):
        result = original_renew(keeper)
        if keeper.cancel_requested:
            observed_cancel.set()
        return result

    monkeypatch.setattr(PlanningLeaseKeeper, "renew_once", renew)
    _policy, _identity, _scope, _catalog, _runtimes, service, envelope = native_case

    def wait(seconds):
        assert seconds > 0
        with uow_factory() as uow:
            run = uow.session.scalars(select(PlanningRunModel)).one()
            request = uow.planning_requests.get(run.planning_request_id, workspace_id)
        cancel = replace(
            envelope,
            id=uuid4(),
            command_type="CancelPlanningRequest",
            target_type="planning_request",
            target_id=request.id,
            expected_version=request.version,
            idempotency_key=str(uuid4()),
            payload={},
        )
        assert service.handle(cancel).data["status"] == "cancelled"
        assert observed_cancel.wait(2), (
            "actual leasekeeper did not observe command cancellation"
        )

    submitted, execution, _outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        native_proposal_body(envelope),
        execution_factory=CancelExecution,
        native_wait=wait,
        runner_options={"lease_renew_interval_seconds": 0.01},
    )
    assert (
        observed_cancel.is_set()
        and len(execution.requests) == len(execution.stops) == 1
    )
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        link = uow.planning_authorities.get_link(workspace_id, request.runs[0].id)
        assert (
            request.status.value == "cancelled"
            and request.runs[0].status.value == "cancelled"
        )
        assert len(request.runs) == 1 and link.closed is closed
        assert link.status == ("closed" if closed else "cleanup_pending")
        assert Path(link.start_request.workspace.directory).is_dir()
        assert (execution.stops[0].run_id, execution.stops[0].operation_id) == (
            link.run_id,
            link.start_operation_id,
        )
        assert uow.planning_records.list_proposals(request.id, workspace_id) == ()
    assert not any(
        thread.name.startswith("planning-lease-") for thread in threading.enumerate()
    )


@pytest.mark.parametrize("closed", [False, True])
def test_native_revoked_grant_denies_new_work_but_keeps_original_cleanup(
    native_case, workspace_id, uow_factory, tmp_path, closed
):
    from sidera_agents.errors import AgentError

    from sidera_control_plane.application.execution_authority import (
        PlanningAuthorityReader,
    )

    class RevokedExecution(OpenPlanningExecution):
        closed_on_stop = closed

    _policy, identity, scope, _catalog, _runtimes, _service, envelope = native_case

    def wait(seconds):
        assert seconds > 0
        scope.revoked = True

    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        native_proposal_body(envelope),
        execution_factory=RevokedExecution,
        native_wait=wait,
    )
    assert outcome.failed == int(closed) and outcome.blocked == int(not closed)
    assert len(execution.requests) == len(execution.stops) == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        link = uow.planning_authorities.get_link(workspace_id, request.runs[0].id)
        assert link.closed is closed and len(request.runs) == 1
        assert uow.planning_records.list_proposals(request.id, workspace_id) == ()
    reader = PlanningAuthorityReader(
        uow_factory, principal=lambda: identity.principal_id, authorizer=scope
    )
    for action in ("run.read", "run.stop"):
        reader.require(link.authority_id, action, workspace_id)
        with pytest.raises(AgentError):
            reader.require(link.authority_id, action, uuid4())
    with pytest.raises(AgentError):
        reader.require(link.authority_id, "run.start", link.profile_ref)


def test_native_workspace_io_stale_context_is_denied_before_admission(
    native_case, workspace_id, uow_factory, tmp_path
):
    from sidera_control_plane.adapters.planning_workspace import (
        PrivatePlanningWorkspaceProvider,
    )
    from sidera_control_plane.application.work_context_commands import (
        WorkContextRevisionFactory,
    )
    from sidera_control_plane.domain.work_context import WorkContextRevisionReason

    private = PrivatePlanningWorkspaceProvider(tmp_path / "scratch")

    class StaleContextWorkspace:
        def prepare(self, **selection):
            workspace = private.prepare(**selection)
            with uow_factory() as uow:
                run = uow.planning_runs.get(selection["planning_run_id"], workspace_id)
                request = uow.planning_requests.get(
                    run.planning_request_id, workspace_id
                )
                context = uow.work_contexts.get_by_planning_request(
                    request.id, workspace_id, for_update=True
                )
                current = uow.work_contexts.get_revision(
                    context.current_revision_id, workspace_id
                )
                revision = WorkContextRevisionFactory().create(
                    work_context_id=context.id,
                    workspace_id=workspace_id,
                    brief=request.briefs[-1],
                    revision_number=context.current_revision_number + 1,
                    reason=WorkContextRevisionReason.REPLANNED,
                    actor="human",
                    created_at=datetime.now(UTC),
                    base_snapshot=current.snapshot,
                )
                version = context.version
                context.append_revision(
                    revision, expected_version=version, now=revision.created_at
                )
                uow.work_contexts.append_revision(
                    context, revision, expected_version=version
                )
            return workspace

    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        native_proposal_body(native_case[-1]),
        workspace_provider=StaleContextWorkspace(),
    )
    assert outcome.failed == 1 and outcome.outcomes[0].code == "permission_denied"
    assert not execution.requests
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        assert (
            uow.planning_authorities.get_link(workspace_id, request.runs[0].id) is None
        )


@pytest.mark.parametrize("provider_available", [False, True])
def test_native_worker_composition_uses_injected_services_and_private_workspace(
    native_case, workspace_id, uow_factory, database_url, tmp_path, provider_available
):
    from sidera_control_plane.adapters.planning_workspace import (
        PrivatePlanningWorkspaceProvider,
    )
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeWorkerServices,
    )
    from sidera_control_plane.application.execution_authority import (
        NativeAuthorityReader,
        PlanningAuthorityReader,
    )
    from sidera_control_plane.config import Settings
    from sidera_control_plane.context_package_worker import ContextPackageWorker
    from sidera_control_plane.planning_worker import build_planning_worker
    from tests.integration.test_context_package_worker import EmptyCatalog

    _policy, identity, scope, catalog, runtimes, service, envelope = native_case
    submitted = service.handle(envelope)
    assert (
        ContextPackageWorker(uow_factory=uow_factory, catalog=EmptyCatalog())
        .run_once()
        .ready
        == 1
    )
    reader = PlanningAuthorityReader(
        uow_factory, principal=lambda: identity.principal_id, authorizer=scope
    )
    execution = PublicPlanningExecution(
        runtimes.runtime.ref, native_proposal_body(envelope)
    )
    services = NativeWorkerServices(
        catalog,
        runtimes,
        execution,
        NativeAuthorityReader(uow_factory, planner=reader, worker=None),
        identity.principal_id,
        identity.grant_id,
    )
    runtime = build_planning_worker(
        Settings(
            database_url=database_url,
            local_actor_id="human",
            planning_temp_root=tmp_path / "legacy",
            knowledge_candidate_spool_dir=tmp_path / "spool",
        ),
        native_services=services,
        planning_workspace_provider=PrivatePlanningWorkspaceProvider(
            tmp_path / "scratch"
        )
        if provider_available
        else None,
        planning_authorizer=scope,
    )
    try:
        outcome = runtime.worker.run_once(1)
        assert outcome.succeeded == int(provider_available) and outcome.failed == int(
            not provider_available
        )
        assert len(execution.requests) == int(provider_available)
        with uow_factory() as uow:
            request = uow.planning_requests.get(
                UUID(submitted.data["planning_request_id"]), workspace_id
            )
            assert request.runs[0].usage.input_tokens is None
            assert request.runs[0].usage.cost_microunits is None
            if not provider_available:
                assert request.runs[0].error_code == "planning_workspace_unavailable"
                assert (
                    uow.planning_authorities.get_link(workspace_id, request.runs[0].id)
                    is None
                )
    finally:
        runtime.catalog.close()
        runtime.database.dispose()


def test_native_closed_invalid_output_queues_only_policy_allowed_cp_attempt(
    native_case, workspace_id, uow_factory, tmp_path
):
    from sidera_control_plane.application.planning_reconciler import PlanningReconciler

    submitted, execution, outcome = run_native_case(
        native_case, uow_factory, tmp_path, '{"invalid":true}'
    )
    assert outcome.failed == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        original = request.runs[0]
        assert uow.planning_authorities.get_link(workspace_id, original.id).closed
    recovery = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="native-reconciler",
        clock=lambda: original.finished_at + timedelta(seconds=5),
        planning_context_ttl_seconds=1800,
    ).run_once()
    assert recovery.retried == 1 and len(execution.requests) == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(request.id, workspace_id)
        assert len(request.runs) == 2 and request.runs[-1].status.value == "queued"
        assert (
            request.runs[-1].attempt == 2
            and request.runs[-1].operation_id != original.operation_id
        )
        assert (
            uow.planning_authorities.get_link(workspace_id, request.runs[-1].id) is None
        )


def test_native_actual_history_reports_pins_run_closure_and_unknown_usage(
    native_case, workspace_id, uow_factory, database, tmp_path
):
    from sidera_control_plane.api.schemas import PlanningRunResponse
    from sidera_control_plane.persistence.planning_query_store import (
        SqlAlchemyPlanningQueryStore,
    )

    submitted, execution, outcome = run_native_case(
        native_case, uow_factory, tmp_path, '{"invalid":true}'
    )
    assert outcome.failed == 1
    query = SqlAlchemyPlanningQueryStore(database)
    request_id = UUID(submitted.data["planning_request_id"])
    profiles = query.list_profiles(workspace_id, limit=10, cursor=None)
    assert profiles.items[0].planner_agent_ref == native_case[0].planner_agent_ref
    history = query.list_runs(workspace_id, request_id)
    assert (
        history[0].execution_mode == "native_profile"
        and history[0].planner_agent_ref == native_case[0].planner_agent_ref
    )
    assert (
        history[0].capability_snapshot is None and history[0].usage.input_tokens is None
    )
    assert (
        history[0].native_run.run_id == execution.run_id
        and history[0].native_run.closed
    )
    assert (
        query.get_request_detail(workspace_id, request_id).active_run.native_run
        == history[0].native_run
    )
    wire = PlanningRunResponse.from_view(history[0]).model_dump(mode="json")
    assert (
        wire["native_run"]["run_id"] == str(execution.run_id)
        and wire["planner_agent_ref"]["revision"] == 2
    )
    assert wire["usage"]["cost_microunits"] is None
    assert not {
        "start_request",
        "grant_id",
        "instructions",
        "context",
        "workspace",
    } & set(wire["native_run"])


def test_native_run_target_historical_receipt_replay_uses_exact_current_permission(
    native_case, workspace_id, uow_factory, tmp_path, monkeypatch
):
    from sidera_control_plane.application.command_service import hash_request
    from sidera_control_plane.application.errors import NotFound
    from sidera_control_plane.application.planning_reconciler import PlanningReconciler
    from sidera_control_plane.domain.commands import CommandResult
    from sidera_control_plane.domain.errors import DomainError
    from sidera_control_plane.persistence.repositories import CommandRepository

    _policy, _identity, scope, _catalog, _runtimes, service, envelope = native_case
    submitted, execution, outcome = run_native_case(
        native_case, uow_factory, tmp_path, '{"invalid":true}'
    )
    assert outcome.failed == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        old_run = request.runs[0]
    # Declared advanced-head fixture; the receipt below is historical compatibility,
    # not evidence that this FAILED run executed a manual blocked-run retry.
    assert (
        PlanningReconciler(
            uow_factory=uow_factory,
            owner_id="fixture-reconciler",
            clock=lambda: old_run.finished_at + timedelta(seconds=5),
            planning_context_ttl_seconds=1800,
        )
        .run_once()
        .retried
        == 1
    )
    retry = replace(
        envelope,
        id=uuid4(),
        command_type="RetryBlockedPlanningRun",
        target_type="planning_run",
        target_id=old_run.id,
        expected_version=1,
        idempotency_key=str(uuid4()),
        payload={"unknown_outcome_decision": "retry"},
    )
    with uow_factory() as uow:
        advanced = uow.planning_requests.get(request.id, workspace_id)
        stored = CommandResult(
            retry.id,
            request.id,
            advanced.version,
            (),
            {
                "planning_request_id": str(request.id),
                "planning_run_id": str(advanced.current_run_id),
                "attempt": 2,
                "unknown_outcome_decision": "retry",
            },
        )
        uow.commands.record_applied(retry, hash_request(retry), stored)
    scope.root_scope = submitted.aggregate_id
    assert service.handle(retry) == stored
    assert service._recover_concurrent(retry, hash_request(retry)) == stored
    target = next(
        target
        for action, target in reversed(scope.targets)
        if action == "planning.write"
    )
    assert (
        target.planning_request_id,
        target.root_task_id,
        target.planning_profile_id,
        target.planner_agent_ref,
    ) == (request.id, submitted.aggregate_id, _policy.id, _policy.planner_agent_ref)

    def no_receipt_read(*_args, **_kwargs):
        raise AssertionError("denied native retry reached command receipt lookup")

    monkeypatch.setattr(CommandRepository, "find_by_id", no_receipt_read)
    monkeypatch.setattr(CommandRepository, "find_duplicate", no_receipt_read)
    with pytest.raises(AssignmentDenied):
        service.handle(replace(retry, actor_id="forged"))
    uninjected = PlanningCommandHandler(local_actor_type="user", local_actor_id="human")
    with pytest.raises(AssignmentDenied):
        CommandService(
            uow_factory, (uninjected,), preflight=uninjected.preflight
        ).handle(retry)
    with pytest.raises(AssignmentDenied), uow_factory() as uow:
        uninjected.handle(uow, retry)
    scope.root_scope = uuid4()
    with pytest.raises(AssignmentDenied):
        service.handle(retry)
    scope.root_scope = submitted.aggregate_id
    with pytest.raises(NotFound):
        service.handle(replace(retry, workspace_id=uuid4()))
    scope.revoked = True
    with pytest.raises(DomainError):
        service.handle(retry)
    with pytest.raises(DomainError):
        service._recover_concurrent(retry, hash_request(retry))
    assert len(execution.requests) == 1
    with uow_factory() as uow:
        assert len(uow.planning_requests.get(request.id, workspace_id).runs) == 2


class WatermarkRacePlanningExecution(SplitPlanningExecution):
    def list_events(self, workspace_id, run_id, after_sequence, limit=100, *, grant_id):
        from sidera_agents.contracts import EventPage

        if not self.cursors:
            return (
                super()
                .list_events(
                    workspace_id, run_id, after_sequence, limit, grant_id=grant_id
                )
                .model_copy(update={"has_more": False})
            )
        if len(self.cursors) == 1:
            self.cursors.append(after_sequence)
            return EventPage(events=(), next_sequence=after_sequence, has_more=False)
        return super().list_events(
            workspace_id, run_id, after_sequence, limit, grant_id=grant_id
        )


@pytest.mark.parametrize(
    "execution_factory", [SplitPlanningExecution, WatermarkRacePlanningExecution]
)
def test_native_runner_drains_terminal_response_to_watermark(
    native_case, workspace_id, uow_factory, tmp_path, execution_factory
):
    _policy, _identity, _scope, _catalog, _runtimes, _service, envelope = native_case
    now = [datetime.now(UTC) + timedelta(minutes=1)]
    waits = []

    def wait(seconds):
        assert seconds > 0
        waits.append(seconds)
        now[0] += timedelta(seconds=seconds)

    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        native_proposal_body(envelope),
        execution_factory=execution_factory,
        native_clock=lambda: now[0],
        native_wait=wait,
    )
    assert outcome.succeeded == 1
    assert len(execution.requests) == 1
    assert execution.cursors == (
        [0, 100, 100]
        if execution_factory is WatermarkRacePlanningExecution
        else [0, 100]
    )
    assert bool(waits) is (execution_factory is WatermarkRacePlanningExecution)
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        run = request.runs[-1]
        link = uow.planning_authorities.get_link(workspace_id, run.id)
        assert (
            link.closed
            and link.event_cursor == 104
            and link.response_id == execution.response_id
        )
        assert run.usage.input_tokens == 7 and run.usage.output_tokens == 5
        assert run.usage.cost_microunits is None
        assert len(uow.planning_records.list_proposals(request.id, workspace_id)) == 1
        assert uow.session.scalar(select(func.count()).select_from(ExecutionModel)) == 0


@pytest.mark.parametrize("unchanged_root", [False, True])
def test_native_confirmation_reuses_submitted_root_and_explicit_independent_bindings(
    native_case, workspace_id, uow_factory, tmp_path, unchanged_root
):
    from sidera_control_plane.persistence.models import EventModel

    _policy, identity, scope, _catalog, _runtimes, service, envelope = native_case
    if unchanged_root:
        envelope = replace(
            envelope,
            payload={**envelope.payload, "acceptance_criteria": ["acceptance:one"]},
        )
        native_case = (*native_case[:-1], envelope)
    submitted, execution, outcome = run_native_case(
        native_case,
        uow_factory,
        tmp_path,
        native_proposal_body(envelope, unchanged_root=unchanged_root),
    )
    assert outcome.succeeded == 1
    assert len(execution.requests) == 1
    request_id = UUID(submitted.data["planning_request_id"])
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        proposal = uow.planning_records.get_current_proposal(request_id, workspace_id)
        original = uow.tasks.get(submitted.aggregate_id, workspace_id)
    scope.root_scope = submitted.aggregate_id
    choices = [
        {
            "target_type": "subtask",
            "target_key": item.key,
            "assignee_kind": "human",
            "assignee_id": identity.principal_id,
            "profile_revision": None,
            "expected_binding_version": None,
        }
        for item in proposal.payload.subtasks
    ]
    choices += [
        {
            "target_type": "worker_item",
            "target_key": item.key,
            "assignee_kind": "profile",
            "assignee_id": str(_policy.planner_agent_ref.profile_id),
            "profile_revision": _policy.planner_agent_ref.revision,
            "expected_binding_version": None,
        }
        for item in proposal.payload.worker_items
    ]
    confirm = replace(
        envelope,
        id=uuid4(),
        command_type="ConfirmPlanningProposal",
        target_type="planning_request",
        target_id=request_id,
        expected_version=request.version,
        idempotency_key=str(uuid4()),
        payload={
            "proposal_revision": proposal.revision,
            "proposal_content_hash": proposal.content_hash,
            "profile_id": str(_policy.id),
            "profile_version": _policy.version,
            "assignee_bindings": choices,
        },
    )
    receipt = service.handle(confirm)
    assert service.handle(confirm) == receipt
    assert receipt.data["root_task_id"] == str(submitted.aggregate_id)
    assert len(receipt.data["assignee_bindings"]) == len(choices)
    with uow_factory() as uow:
        root = uow.tasks.get(submitted.aggregate_id, workspace_id)
        assert root.requested_by == original.requested_by
        assert root.version == (1 if unchanged_root else 2)
        events = uow.session.scalars(
            select(EventModel).where(EventModel.aggregate_id == root.id)
        ).all()
        assert sum(item.event_type == "TaskCreated" for item in events) == 1
        assert sum(
            item.event_type == "TaskSpecificationUpdated" for item in events
        ) == (0 if unchanged_root else 1)
        for kind, mapping_name in (
            ("subtask", "subtask_task_mapping"),
            ("worker_item", "worker_item_mapping"),
        ):
            for target_id in receipt.data[mapping_name].values():
                binding = uow.agent_bindings.get(workspace_id, kind, UUID(target_id))
                assert binding.version == 1
                assert binding.assignee.kind == (
                    "human" if kind == "subtask" else "profile"
                )
                assert binding.profile_ref == (
                    None if kind == "subtask" else _policy.planner_agent_ref
                )
        assert uow.session.scalar(select(func.count()).select_from(ExecutionModel)) == 0
    scope.revoked = True
    with pytest.raises(AssignmentDenied):
        service.handle(confirm)


def test_actual_native_runner_rejects_invalid_business_json_before_confirmation(
    native_case, workspace_id, uow_factory, tmp_path
):
    from sidera_control_plane.adapters.planning_workspace import (
        PrivatePlanningWorkspaceProvider,
    )
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeWorkerServices,
    )
    from sidera_control_plane.application.context_materialization import (
        ContextMaterializer,
    )
    from sidera_control_plane.application.execution_authority import (
        NativeAuthorityReader,
        PlanningAuthorityReader,
    )
    from sidera_control_plane.application.planning_runner import (
        PlanningRunner,
        RegistryPlannerAdapterFactory,
    )
    from sidera_control_plane.context_package_worker import ContextPackageWorker
    from tests.integration.test_context_package_worker import EmptyCatalog

    policy, identity, scope, catalog, runtimes, service, envelope = native_case
    submitted = service.handle(envelope)
    knowledge = EmptyCatalog()
    assert (
        ContextPackageWorker(uow_factory=uow_factory, catalog=knowledge)
        .run_once()
        .ready
        == 1
    )
    reader = PlanningAuthorityReader(
        uow_factory, principal=lambda: identity.principal_id, authorizer=scope
    )
    facade = NativeAuthorityReader(uow_factory, planner=reader, worker=None)
    execution = PublicPlanningExecution(runtimes.runtime.ref, '{"invalid":true}')
    services = NativeWorkerServices(
        catalog, runtimes, execution, facade, identity.principal_id, identity.grant_id
    )
    factory = RegistryPlannerAdapterFactory(
        {},
        native_services=services,
        uow_factory=uow_factory,
        planning_workspace_provider=PrivatePlanningWorkspaceProvider(
            tmp_path / "scratch"
        ),
        planning_context_materializer=ContextMaterializer(knowledge),
        planning_authorizer=scope,
    )
    result = PlanningRunner(
        uow_factory=uow_factory,
        adapter_factory=factory,
        owner_id="native-planner",
        context_materializer=ContextMaterializer(knowledge),
    ).run_once()
    assert result.failed == 1
    assert result.outcomes[0].code == "invalid_planner_output"
    assert len(execution.requests) == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), workspace_id
        )
        run = request.runs[-1]
        assert run.error_code == "invalid_planner_output"
        assert run.usage.input_tokens is None
        assert uow.planning_records.list_proposals(request.id, workspace_id) == ()
        assert (
            uow.session.scalar(select(func.count()).select_from(PlanningRunModel)) == 1
        )
        assert uow.session.scalar(select(func.count()).select_from(ExecutionModel)) == 0
        link = uow.planning_authorities.get_link(workspace_id, run.id)
        assert link.start_operation_id == run.operation_id
        assert link.profile_ref == policy.planner_agent_ref
        assert link.closed is True
        assert (
            execution.requests[0].workspace.directory
            == uow.planning_authorities.get(
                link.authority_id
            ).execution_workspace.directory
        )


def test_native_goal_submission_creates_one_pending_root_without_worker_start(
    workspace_id,
    uow_factory,
):
    pin = ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=2)
    policy = planning_profile(
        workspace_id=workspace_id,
        execution_mode="native_profile",
        planner_agent_ref=pin,
        planner_module_instance_id=None,
        planner_module_config_version=None,
        planner_secret_refs=(),
        proposal_schema_version=2,
    )
    with uow_factory() as uow:
        uow.planning_profiles.add(policy)
    identity = TrustedAssignmentIdentity("human", uuid4())
    scope = AssignmentScope(
        workspace_id,
        identity.principal_id,
        identity.grant_id,
        actions={
            "planning.submit",
            "planning.confirm",
            "planning.read",
            "assignment.write",
        },
    )
    runtime_ref = RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4())
    catalog = PublicAgentCatalog(
        (
            ProfileRevision(
                ref=pin,
                config_hash="fixture-profile",
                created_at=datetime.now(UTC),
                draft=ProfileDraft(
                    name="Shared Planner and Worker",
                    description="",
                    runtime_ref=runtime_ref,
                    instructions="",
                    resource_refs=(),
                    model_id=None,
                    thinking=None,
                    max_active=2,
                    environment={},
                    secret_refs=(),
                    native_settings={"native_tools": "write"},
                ),
            ),
        )
    )
    runtimes = PublicAgentRuntimes(
        RuntimeSnapshot(
            ref=runtime_ref,
            harness_id="public-fixture",
            executable="/unused",
            actual_version="fixture-1",
            installed=True,
            enabled=True,
            version=1,
            protocol_state="ready",
            auth_state="unconfirmed",
            capabilities={},
            checked_at=datetime.now(UTC),
            limits={},
        )
    )
    assignments = AssignmentCommandHandler(
        catalog=catalog,
        runtimes=runtimes,
        identity=identity,
        authorizer=scope,
    )
    handler = PlanningCommandHandler(
        local_actor_type="user",
        local_actor_id="human",
        assignment_handler=assignments,
        identity=identity,
        authorizer=scope,
    )
    service = CommandService(uow_factory, (handler,), preflight=handler.preflight)
    envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type="SubmitTaskGoal",
        target_type="workspace",
        target_id=None,
        actor_type="user",
        actor_id="human",
        expected_version=None,
        idempotency_key=str(uuid4()),
        correlation_id=uuid4(),
        payload={
            "title": "Native planning goal",
            "objective": "Plan the requested work",
            "constraints": ["Keep responsibilities explicit"],
            "acceptance_criteria": ["Approved work has one stable root"],
            "requested_outputs": ["proposal"],
            "context_refs": [],
            "planning_profile_id": str(policy.id),
            "planning_profile_version": policy.version,
            "planner_agent_ref": pin.model_dump(mode="json"),
            "source_ref": None,
        },
    )
    receipt = service.handle(envelope)
    assert service.handle(envelope) == receipt
    with uow_factory() as uow:
        root = uow.tasks.get(receipt.aggregate_id, workspace_id)
        request = uow.planning_requests.get(
            receipt.data["planning_request_id"],
            workspace_id,
        )
        assert root.status.value == "pending"
        assert request.profile_snapshot.planner_agent_ref == pin
        assert request.kind.value == "create"
        assert request.base_descriptor is None
        assert uow.session.scalar(select(func.count()).select_from(TaskModel)) == 1
        assert uow.session.scalar(select(func.count()).select_from(ExecutionModel)) == 0
