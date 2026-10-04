import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sidera_agents.errors import AgentError
from sqlalchemy import select

from sidera_control_plane.adapters.local_verification import LocalVerificationAdapter
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.application.result_verification import (
    verification_manifest_for_request,
)
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    GatewayRecoveryRequired,
)
from sidera_control_plane.composition.native_runtime import create_control_plane_runtime
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.execution import ExecutionSession
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    DeliveryResult,
    GatewayOperationType,
    RuntimeModuleOperation,
    gateway_operation_id,
)
from sidera_control_plane.domain.input_selections import CollaborationInputSelectionV1
from sidera_control_plane.domain.native_setup import native_local_slot_ids
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    EventModel,
    ExecutionBindingModel,
    ExecutionModel,
    ExecutionSessionModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningProfileModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    WorkerModel,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_to_payload,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.contract.test_local_verification_adapter import (
    _canonical_hash,
    _check_spec,
    _materialization,
    _spec,
)
from tests.integration.test_collaboration_repository import (
    _decision,
    _evaluation,
    _intent,
    _projection,
    _run,
)
from tests.integration.test_gateway_runner import _seed_assignment
from tests.integration.test_native_workspace_setup import (
    public_setup_case as native_setup_case,  # noqa: F401
)
from tests.integration.test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)
from tests.support.native_agents import (
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)
from tests.unit.result_input_fixtures import selection_mapping
from tests.unit.result_integration_fixtures import (
    _snapshot_content_hash,
    candidate_mapping,
    integration_mapping,
    snapshot_mapping,
)
from tests.unit.test_plan_snapshot_v4 import plan_v4_payload

pytestmark = pytest.mark.integration


def _wait(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _verification_inputs(
    database, workspace_id, execution_id, check, binding, commit, tree
):
    """Seed immutable domain inputs only; the real process supplies every outcome."""
    ids = seed_execution_request_facts(database, workspace_id)
    raw = plan_v4_payload()
    raw.update(
        id=str(ids["plan"]),
        workspace_id=str(workspace_id),
        root_task_id=str(ids["root_task"]),
        schema_version=5,
        generation=1,
    )
    for subtask in raw["subtasks"]:
        subtask["task_id"] = (
            str(ids["subtask"]) if subtask["key"] == "integration" else None
        )
    for item in raw["worker_items"]:
        item["worker_item_id"] = (
            str(ids["worker_item"]) if item["key"] == "integration-checker" else None
        )
    spec = integration_mapping()
    spec.update(
        base_commit=commit,
        integration_worker_item_key="integration-checker",
        input_group_keys=["research"],
        checks=[check.to_mapping()],
        verification_binding=binding,
    )
    spec["rework_policy"]["allowed_worker_item_keys"] = ["integration-checker"]
    raw["integration_spec"] = spec
    plan = TaskPlanSnapshot.from_payload(raw)
    plan_hash = plan_snapshot_content_hash(plan)
    source = request_row(
        ids, request_id=ids["request"], worker_key="integration-checker"
    )
    source.workspace_id = workspace_id
    source.status, source.execution_lineage_id, source.first_execution_id = (
        "materialized",
        ids["execution"],
        ids["execution"],
    )
    with database.session() as session:
        row = session.get(PlanSnapshotModel, ids["plan"])
        row.payload, row.schema_version = plan.to_payload(), 5
        session.add(
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=ids["root_task"],
                active_snapshot_id=plan.id,
                active_generation=plan.generation,
                active_snapshot_hash=plan_hash,
                version=1,
            )
        )
        session.add(source)
        current = session.get(ExecutionModel, execution_id)
        current.task_id, current.worker_item_id = ids["subtask"], ids["worker_item"]
        lineage_id = current.lineage_id
        session.commit()
    run = replace(_run(ids, workspace_id), plan_content_hash=plan_hash)
    projection = _projection(run)
    evaluation = _evaluation(run, projection)
    decision = _decision(run, projection, evaluation, sequence=1)
    intent = _intent(run, decision, source.id)
    selection = replace(
        CollaborationInputSelectionV1.from_mapping(selection_mapping()),
        id=uuid4(),
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        run_id=run.id,
        plan_snapshot_id=plan.id,
        plan_generation=1,
        plan_content_hash=plan_hash,
        target_worker_item_id=ids["worker_item"],
        target_execution_request_id=source.id,
        target_request_version=1,
        projection_snapshot_id=projection.id,
        projection_hash=projection.content_hash,
        decision_id=decision.id,
        intent_id=intent.id,
    )
    selection = replace(selection, selection_hash=selection.compute_selection_hash())
    snapshot_raw = snapshot_mapping(
        id=str(uuid4()),
        workspace_id=str(workspace_id),
        root_task_id=str(ids["root_task"]),
        worker_item_id=str(ids["worker_item"]),
        request_id=str(source.id),
        execution_id=str(ids["execution"]),
        input_commit=commit,
        input_tree=tree,
        result_commit=commit,
        result_tree=tree,
    )
    snapshot_raw["content_hash"] = _snapshot_content_hash(snapshot_raw)
    snapshot = CodeResultSnapshotV1.from_mapping(snapshot_raw)
    candidate_raw = candidate_mapping(
        id=str(uuid4()),
        workspace_id=str(workspace_id),
        root_task_id=str(ids["root_task"]),
        plan_snapshot_id=str(plan.id),
        plan_content_hash=plan_hash,
        integration_execution_id=str(ids["execution"]),
        base_commit=commit,
        source_result_ids=[str(snapshot.id)],
        input_selection_id=str(selection.id),
        input_selection_hash=selection.selection_hash,
        code_result_id=str(snapshot.id),
    )
    candidate_raw["content_hash"] = _canonical_hash(
        {
            key: value
            for key, value in candidate_raw.items()
            if key not in {"id", "created_at", "content_hash"}
        }
    )
    candidate = IntegrationCandidateV1.from_mapping(candidate_raw)
    verification = request_row(
        ids, request_id=uuid4(), worker_key="integration-checker"
    )
    verification.workspace_id = workspace_id
    (
        verification.purpose,
        verification.result_derivation_kind,
        verification.source_candidate_id,
    ) = "verification", "verification", candidate.id
    (
        verification.status,
        verification.execution_lineage_id,
        verification.first_execution_id,
    ) = "materialized", lineage_id, execution_id
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.collaboration_runs.add(run)
        uow.flush()
        uow.collaboration_ledger.get_or_add_projection(projection)
        uow.collaboration_evaluations.get_or_add(evaluation)
        uow.collaboration_ledger.add_decision_bundle(decision, (intent,))
        uow.input_selections.add(selection)
        uow.results.add_immutable("code_snapshot", snapshot)
        uow.results.add_immutable("candidate", candidate)
        uow.session.add(verification)
        uow.flush()
        request = uow.execution_requests.get(verification.id, workspace_id)
        manifest = verification_manifest_for_request(uow, request)
    return manifest, ids["subtask"], candidate.id


@pytest.fixture
def held_local_check(request, tmp_path, monkeypatch):
    s = request.getfixturevalue("native_setup_case")
    scenario = getattr(request, "param", 0)
    exit_code = scenario if isinstance(scenario, int) else 0
    release = tmp_path / "release-check"
    entered = tmp_path / "entered-check"
    script = tmp_path / "held-check.py"
    script.write_text(
        "from pathlib import Path\nimport time\n"
        f"Path({str(entered)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists():\n    time.sleep(0.01)\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    check = replace(
        _check_spec(), argv=(sys.executable, "-B", str(script)), timeout_seconds=60
    )
    if scenario == "prestart":
        check = replace(check, cwd="missing-check-directory")
    receipt = s.runtime.configure_workspace(
        replace(s.request, verification_checks=(check,)), grant_id=s.grant
    )
    assert s.runtime.close() is True
    system = ControlPlaneIdentity("operator", uuid4(), "system")

    def require(grant, action, target):
        if grant != system.grant_id or target.workspace_id != s.workspace_id:
            raise AgentError("permission_denied", "Controlled check scope denied")

    authority = SimpleNamespace(
        identity_for_grant=lambda grant: system if grant == system.grant_id else None,
        require=require,
    )
    owned_root = tmp_path / "owned-workspaces"
    owned_root.mkdir()
    runtime = create_control_plane_runtime(
        Settings(
            database_url=s.database.engine.url.render_as_string(hide_password=False),
            local_actor_id="operator",
            workspace_root=owned_root,
            planning_temp_root=tmp_path / "planning",
            result_store_root=tmp_path / "results",
            artifact_root=tmp_path / "artifacts",
            knowledge_candidate_spool_dir=tmp_path / "spool",
            gateway_sync_workspace_id=None,
            gateway_poll_interval_seconds=0.02,
            planning_poll_interval_seconds=0.02,
            assignment_lease_renew_interval_seconds=0.1,
        ),
        s.native,
        s.catalog,
        s.native,
        s.native,
        authority,
        agent_runtimes=s.runtimes,
    )
    adapter = operation_id = None
    observation_ready = Event()
    original_observe = runtime._observe_original_verifications
    monkeypatch.setattr(
        runtime,
        "_observe_original_verifications",
        lambda: original_observe() if observation_ready.is_set() else None,
    )
    try:
        runtime.start_background(background_identity=system)
        with SqlAlchemyUnitOfWork(s.database) as uow:
            policy = uow.planning_profiles.get(
                receipt.planning_policy_ref.policy_id, s.workspace_id, version=1
            )
            binding = policy.system_policy["result_integration"]["verification_binding"]
            adapter_id = UUID(binding["adapter_module_ref"])
            workspace_module_id = UUID(binding["workspace_module_ref"])
            _, worker_id = native_local_slot_ids(workspace_module_id, adapter_id)
            instance = uow.modules.get_instance(adapter_id, s.workspace_id)
            definition = uow.modules.get_definition(
                instance.definition_id, s.workspace_id
            )
            workspace_instance = uow.modules.get_instance(
                workspace_module_id, s.workspace_id
            )
            workspace_definition = uow.modules.get_definition(
                workspace_instance.definition_id, s.workspace_id
            )
        ids = _seed_assignment(s.database, s.workspace_id)
        now = datetime.now(UTC)
        capabilities = LocalVerificationAdapter.capabilities()
        with s.database.session() as session:
            execution = session.get(ExecutionModel, ids["execution_id"])
            execution.assigned_worker_id = worker_id
            execution.purpose, execution.status = "verification", "running"
            execution.capability_requirements = ["verification"]
            execution.started_at = now
            lease = session.get(AssignmentLeaseModel, ids["lease_id"])
            lease.worker_id, lease.issued_at = worker_id, now
            lease.expires_at = now + timedelta(minutes=2)
            row = session.scalar(
                select(ExecutionBindingModel).where(
                    ExecutionBindingModel.execution_id == execution.id
                )
            )
            row.worker_id = worker_id
            row.adapter_module_id, row.workspace_module_id = (
                adapter_id,
                workspace_module_id,
            )
            row.capability_snapshot = capability_profile_to_payload(capabilities)
            worker = session.get(WorkerModel, worker_id)
            worker.status, worker.current_load = "busy", 1
            session.commit()
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=s.repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=s.repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        manifest, task_id, candidate_id = _verification_inputs(
            s.database,
            s.workspace_id,
            ids["execution_id"],
            check,
            binding,
            commit,
            tree,
        )
        materialization = replace(
            _materialization(ids["execution_id"]), verification_manifest=manifest
        )
        spec = _spec(materialization=materialization)
        spec = replace(
            spec,
            workspace_id=s.workspace_id,
            task_id=task_id,
            worker_id=worker_id,
            lease_id=ids["lease_id"],
            adapter_module_ref=adapter_id,
            workspace_module_ref=workspace_module_id,
            repositories=tuple(
                replace(
                    repository,
                    repository_ref=workspace_instance.config[
                        "workspace.repository_ref"
                    ],
                    base_revision=commit,
                )
                for repository in spec.repositories
            ),
            deadline=now + timedelta(seconds=60),
            physical_input_ack={
                **spec.physical_input_ack,
                "actual_commit": commit,
                "actual_tree": tree,
                "candidate_id": str(manifest.candidate_id),
                "candidate_hash": manifest.candidate_hash,
                "verification_basis_hash": manifest.verification_basis_hash,
            },
        )
        gateway = runtime._owner.gateway_runtime.worker_gateway
        provider = gateway._workspace_provider(
            workspace_definition,
            workspace_instance,
            operation=RuntimeModuleOperation.START,
        )
        workspace = provider.prepare(spec, operation_id=uuid4())
        adapter = gateway._adapter(
            definition, instance, operation=RuntimeModuleOperation.OBSERVE
        )
        observed, observed_event = [], Event()
        original_read = adapter.read_events

        def read_events(handle):
            events = tuple(original_read(handle))
            observed.append(tuple(event.event_type for event in events))
            observed_event.set()
            return events

        monkeypatch.setattr(adapter, "read_events", read_events)
        if scenario == "uncertain":

            def unknown_identity(*args):
                raise OSError("Controlled unavailable process identity")

            monkeypatch.setattr(
                "sidera_control_plane.adapters.local_verification._process_identity",
                unknown_identity,
            )
        operation_id = uuid4()
        handle = adapter.start_session(spec, workspace, operation_id=operation_id)
        if isinstance(scenario, int):
            assert _wait(entered.exists), (
                "The real controlled check did not enter its hold"
            )
        session = ExecutionSession.start(
            session_id=handle.execution_session_id,
            workspace_id=s.workspace_id,
            execution_id=ids["execution_id"],
            worker_id=worker_id,
            adapter_session_id=handle.adapter_session_id,
            now=handle.started_at,
            adapter_module_id=adapter_id,
            adapter_config_version=1,
            workspace_module_id=workspace_module_id,
            workspace_config_version=1,
            capability_snapshot=capabilities,
            process_ref=handle.process_ref,
            workspace_ref=workspace.location_ref,
        )
        with SqlAlchemyUnitOfWork(s.database) as uow:
            uow.workspaces.add(workspace)
            uow.sessions.add(session)
        original = runtime._verifications.page(after=None, limit=1)
        assert len(original) == 1 and original[0].observable
        assert original[0].session_id == session.id
        gateway.sync_session(
            workspace_id=s.workspace_id,
            execution_id=ids["execution_id"],
            session_id=session.id,
            lease_id=ids["lease_id"],
            fencing_token=1,
        )
        assert observed_event.is_set() and len(observed) >= 1
        if isinstance(scenario, int):
            assert adapter._runner.inspect(operation_id).status.value == "running"

        def facts():
            with SqlAlchemyUnitOfWork(s.database) as uow:
                current = uow.sessions.get_any(session.id, s.workspace_id)
                lease = uow.leases.get(ids["lease_id"], s.workspace_id)
                execution = uow.executions.get(ids["execution_id"], s.workspace_id)
                links = uow.execution_authorities.list_reconciliation_links(
                    None, limit=200
                )
                results = uow.results.list_verification_results(
                    candidate_id, s.workspace_id
                )
                worker = uow.workers.get(worker_id, s.workspace_id)
                stop = uow.gateway_operations.get_by_operation_id(
                    gateway_operation_id(
                        ids["execution_id"], GatewayOperationType.STOP_SESSION
                    ),
                    s.workspace_id,
                )
            observation = adapter._runner.inspect(operation_id)
            return {
                "reads": len(observed),
                "process_status": observation.status.value,
                "exit_code": observation.exit_code,
                "session_status": current.status.value,
                "execution_status": execution.status.value,
                "lease_status": lease.status.value,
                "native_links": len(links),
                "native_calls": len(s.native.calls),
                "legacy_hint": runtime._settings.gateway_sync_workspace_id,
                "verification_statuses": [result.status.value for result in results],
                "verification_exit_codes": [result.exit_code for result in results],
                "worker_load": worker.current_load,
                "stop_status": None if stop is None else stop.status.value,
            }

        observation_ready.set()
        yield SimpleNamespace(
            runtime=runtime,
            adapter=adapter,
            operation_id=operation_id,
            release=release,
            observed=observed,
            facts=facts,
            database=s.database,
            workspace_id=s.workspace_id,
            session_id=session.id,
            execution_id=ids["execution_id"],
            lease_id=ids["lease_id"],
            worker_id=worker_id,
            adapter_id=adapter_id,
            workspace_module_id=workspace_module_id,
            policy_id=receipt.planning_policy_ref.policy_id,
            exit_code=exit_code,
        )
    finally:
        release.touch()
        if adapter is not None and operation_id is not None:
            adapter._runner.stop(operation_id)
        if not runtime.close(grace_seconds=2):
            runtime._stop.set()
            for thread in runtime._threads:
                thread.join(timeout=5)
            assert not any(thread.is_alive() for thread in runtime._threads)
            if runtime._planning is not None:
                runtime._planning.catalog.close()
                runtime._planning.database.dispose()
            if runtime._owner is not None:
                runtime._owner.close()
            runtime._database.dispose()


@pytest.mark.parametrize("held_local_check", (0, 7), indirect=True)
def test_native_owner_observes_local_check_after_held_first_sync(held_local_check):
    case = held_local_check
    status = "passed" if case.exit_code == 0 else "failed"
    case.release.touch()
    assert _wait(
        lambda: (
            case.adapter._runner.inspect(case.operation_id).status.value != "running"
        )
    )
    refreshed = _wait(
        lambda: (
            case.facts()["verification_statuses"] == [status]
            and case.facts()["lease_status"] != "active"
        )
    )
    facts = case.facts()
    print("N16_HELD_CHECK_OBSERVATION " + json.dumps(facts, sort_keys=True))
    assert facts["process_status"] == status and facts["exit_code"] == case.exit_code
    assert refreshed, (
        "Native owner did not observe the original check after its first sync"
    )
    assert facts["verification_statuses"] == [status]
    assert facts["verification_exit_codes"] == [case.exit_code]
    assert facts["session_status"] == "closed" and facts["worker_load"] == 0
    assert case.runtime.close(grace_seconds=2) is True


def test_native_close_retains_or_stops_held_local_check(held_local_check, monkeypatch):
    case = held_local_check
    stop_entered, stop_release = Event(), Event()
    original_stop = case.adapter.stop_session
    gateway = case.runtime._owner.gateway_runtime.worker_gateway
    original_confirmation = gateway._persist_stop_success
    receipts = []

    def confirm(context, **kwargs):
        receipts.append((context, kwargs))
        return original_confirmation(context, **kwargs)

    monkeypatch.setattr(gateway, "_persist_stop_success", confirm)
    stops, disposed, starts = [], [], []
    original_dispose = case.runtime._database.dispose

    def dispose():
        disposed.append(True)
        original_dispose()

    def held_stop(handle, *, operation_id):
        stops.append((handle.execution_session_id, operation_id))
        stop_entered.set()
        assert stop_release.wait(5), "Test did not release its Stop boundary"
        return original_stop(handle, operation_id=operation_id)

    monkeypatch.setattr(case.adapter, "stop_session", held_stop)
    monkeypatch.setattr(case.runtime._database, "dispose", dispose)
    original_start = case.adapter.start_session

    def start(*args, **kwargs):
        starts.append(True)
        return original_start(*args, **kwargs)

    monkeypatch.setattr(case.adapter, "start_session", start)
    try:
        closed = case.runtime.close(grace_seconds=0.05)
        facts = {**case.facts(), "close_005": closed}
        print("N16_HELD_CHECK_CLOSE " + json.dumps(facts, sort_keys=True))
        assert closed is False, "Native close reported disposal before its real Stop"
        assert _wait(stop_entered.is_set)
        assert facts["session_status"] == "active" and facts["lease_status"] == "active"
        assert disposed == []
        stop_release.set()
        assert _wait(
            lambda: (
                case.facts()["verification_statuses"] == ["interrupted"]
                and case.facts()["lease_status"] != "active"
            )
        )
        assert (
            case.facts()["session_status"] == "closed"
            and case.facts()["worker_load"] == 0
        )
        print("N16_HELD_CHECK_STOP_RESULT " + json.dumps(case.facts(), sort_keys=True))
        assert _wait(lambda: case.facts()["stop_status"] == "succeeded")
        context, receipt = receipts[0]

        def resource_facts():
            with SqlAlchemyUnitOfWork(case.database) as uow:
                execution = uow.executions.get(case.execution_id, case.workspace_id)
                lease = uow.leases.get(case.lease_id, case.workspace_id)
                session = uow.sessions.get_any(case.session_id, case.workspace_id)
                worker = uow.workers.get(case.worker_id, case.workspace_id)
                events = uow.session.scalars(
                    select(EventModel).where(
                        EventModel.workspace_id == case.workspace_id,
                        EventModel.aggregate_id.in_(
                            (
                                case.execution_id,
                                case.lease_id,
                                case.session_id,
                                case.worker_id,
                            )
                        ),
                    )
                ).all()
            return (
                execution.version,
                lease.version,
                session.version,
                worker.version,
            ), Counter(event.event_type for event in events)

        before = resource_facts()
        for event_type in (
            "ExecutionSucceeded",
            "LeaseReleased",
            "WorkerCapacityReleased",
            "ExecutionSessionClosed",
        ):
            assert before[1][event_type] == 1
        assert (
            before[1]["LeaseReleasedByStop"] == 0
            and before[1]["ExecutionCanceledByStop"] == 0
        )
        original_confirmation(context, **receipt)
        assert resource_facts() == before
        for changed_context, changed_receipt in (
            (
                replace(context, binding=replace(context.binding, worker_id=uuid4())),
                receipt,
            ),
            (
                replace(
                    context,
                    binding=replace(
                        context.binding, fencing_token=context.binding.fencing_token + 1
                    ),
                ),
                receipt,
            ),
            (
                replace(
                    context, binding=replace(context.binding, adapter_config_version=2)
                ),
                receipt,
            ),
            (context, {**receipt, "session_id": uuid4()}),
            (
                context,
                {**receipt, "accepted_sequence": receipt["accepted_sequence"] + 1},
            ),
            (
                context,
                {**receipt, "accepted_sequence": receipt["accepted_sequence"] - 1},
            ),
            (context, {**receipt, "operation_id": uuid4()}),
        ):
            with pytest.raises((GatewayRecoveryRequired, GatewayMessageError)):
                original_confirmation(changed_context, **changed_receipt)
            assert resource_facts() == before
        assert case.runtime.close(grace_seconds=2) is True
        assert case.runtime.close(grace_seconds=0.05) is True
        assert case.facts()["stop_status"] == "succeeded"
        assert resource_facts() == before
        assert len(stops) == 1 and stops[0][0] == case.session_id
        assert disposed == [True] and not starts
    finally:
        stop_release.set()


@pytest.mark.parametrize("held_local_check", ("prestart", "uncertain"), indirect=True)
def test_only_proven_prestart_failure_can_drain_without_finished_at(
    held_local_check, request
):
    case = held_local_check
    scenario = request.node.callspec.params["held_local_check"]
    observation = case.adapter._runner.inspect(case.operation_id)
    assert observation.finished_at is None and observation.pid is None
    assert observation.failure_code == (
        "process_start_failed"
        if scenario == "prestart"
        else "process_identity_unavailable"
    )
    assert case.facts()["verification_statuses"] == ["blocked"]
    assert (
        case.facts()["session_status"] == "closed"
        and case.facts()["lease_status"] == "released"
    )
    assert case.runtime._verifications.has_outstanding() is (scenario == "uncertain")
    assert case.runtime.close(grace_seconds=0.05) is (scenario == "prestart")


def test_unknown_original_stop_retains_process_lease_and_owner(
    held_local_check, monkeypatch
):
    case = held_local_check
    stops, disposed = [], []

    def unknown_stop(handle, *, operation_id):
        stops.append((handle.execution_session_id, operation_id))
        return DeliveryResult(
            operation_id, DeliveryOutcome.UNKNOWN, None, "controlled_unknown"
        )

    monkeypatch.setattr(case.adapter, "stop_session", unknown_stop)
    original_dispose = case.runtime._database.dispose

    def dispose():
        disposed.append(True)
        original_dispose()

    monkeypatch.setattr(case.runtime._database, "dispose", dispose)
    assert case.runtime.close(grace_seconds=0.05) is False
    assert _wait(lambda: len(stops) == 1)
    assert case.runtime.close(grace_seconds=0.05) is False
    facts = case.facts()
    assert facts["process_status"] == "running" and facts["lease_status"] == "active"
    assert facts["session_status"] == "active" and facts["worker_load"] == 1
    assert facts["verification_statuses"] == [] and disposed == []
    assert len(stops) == 1


@pytest.mark.parametrize("state", ("expired", "lost", "closed", "mismatched_pin"))
def test_unproven_owned_check_stays_a_nonobservable_drain_blocker(
    held_local_check, state
):
    case = held_local_check
    with case.database.session() as session:
        lease = session.get(AssignmentLeaseModel, case.lease_id)
        if state == "expired":
            lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif state in {"lost", "closed"}:
            session.get(ExecutionSessionModel, case.session_id).status = state
            execution = session.get(ExecutionModel, case.execution_id)
            execution.status, execution.active_lease_id = "lost", None
            lease.status, lease.ended_at = "expired", datetime.now(UTC)
        else:
            binding = session.scalar(
                select(ExecutionBindingModel).where(
                    ExecutionBindingModel.execution_id == case.execution_id
                )
            )
            binding.adapter_config_version = 2
        session.commit()
    originals = case.runtime._verifications.page(after=None, limit=1)
    assert len(originals) == 1 and originals[0].observable is False
    assert case.runtime._verifications.has_outstanding() is True
    assert case.runtime.close(grace_seconds=0.05) is False
    assert case.facts()["process_status"] == "running"


def test_original_disabled_binding_survives_draining_and_newer_policy(held_local_check):
    case = held_local_check
    with case.database.session() as session:
        policy = session.get(PlanningProfileModel, case.policy_id)
        newer = PlanningProfileModel(
            **{
                column.name: getattr(policy, column.name)
                for column in PlanningProfileModel.__table__.columns
                if column.name not in {"created_at", "updated_at"}
            }
        )
        newer.id, newer.version = uuid4(), 2
        newer.policy = {
            **policy.policy,
            "system_policy": {
                **policy.policy["system_policy"],
                "result_integration": {
                    **policy.policy["system_policy"]["result_integration"],
                    "verification_binding": {
                        "adapter_module_ref": str(uuid4()),
                        "adapter_config_version": 1,
                        "workspace_module_ref": str(uuid4()),
                        "workspace_config_version": 1,
                    },
                },
            },
        }
        session.add(newer)
        policy.status, policy.user_selectable = "disabled", False
        for module_id in (case.adapter_id, case.workspace_module_id):
            instance = session.get(ModuleInstanceModel, module_id)
            instance.lifecycle = "disabled"
            session.get(
                ModuleDefinitionModel, instance.definition_id
            ).lifecycle = "draining"
        session.get(WorkerModel, case.worker_id).desired_state = "draining"
        session.commit()
    originals = case.runtime._verifications.page(after=None, limit=1)
    assert len(originals) == 1 and originals[0].observable
    assert case.worker_id not in {
        worker.id for worker in case.runtime._slots.owned_slots()
    }
    case.release.touch()
    assert _wait(
        lambda: (
            case.facts()["verification_statuses"] == ["passed"]
            and case.facts()["session_status"] == "closed"
        )
    )
    assert case.facts()["worker_load"] == 0
    assert case.runtime.close(grace_seconds=2) is True


def test_same_provider_foreign_assignment_is_not_observed_or_stopped(held_local_check):
    case = held_local_check
    ids = _seed_assignment(case.database, case.workspace_id, name_suffix="foreign")
    with case.database.session() as session:
        instance = session.get(ModuleInstanceModel, ids["adapter_instance_id"])
        session.get(
            ModuleDefinitionModel, instance.definition_id
        ).provider = "local_verification"
        session.get(WorkerModel, ids["worker_id"]).adapter_type = "local_verification"
        execution = session.get(ExecutionModel, ids["execution_id"])
        execution.purpose, execution.status = "verification", "running"
        session.commit()
    originals = case.runtime._verifications.page(after=None, limit=200)
    assert [original.execution_id for original in originals] == [case.execution_id]
    case.release.touch()
    assert _wait(
        lambda: (
            case.facts()["verification_statuses"] == ["passed"]
            and case.facts()["session_status"] == "closed"
        )
    )
    assert case.runtime.close(grace_seconds=2) is True
    with SqlAlchemyUnitOfWork(case.database) as uow:
        foreign = uow.executions.get(ids["execution_id"], case.workspace_id)
        lease = uow.leases.get(ids["lease_id"], case.workspace_id)
    assert foreign.status.value == "running" and not foreign.stop_requested
    assert lease.status.value == "active"


def test_terminal_result_cannot_overrule_unknown_original_stop(
    held_local_check, monkeypatch
):
    case = held_local_check
    gateway = case.runtime._owner.gateway_runtime.worker_gateway

    def failed_confirmation(*args, **kwargs):
        raise RuntimeError("Controlled Stop confirmation failure")

    monkeypatch.setattr(gateway, "_persist_stop_success", failed_confirmation)
    assert case.runtime.close(grace_seconds=0.05) is False
    assert _wait(
        lambda: (
            case.facts()["verification_statuses"] == ["interrupted"]
            and case.facts()["session_status"] == "closed"
        )
    )
    assert (
        case.facts()["lease_status"] == "released" and case.facts()["worker_load"] == 0
    )
    assert case.runtime._verifications.has_outstanding() is True
    assert case.runtime.close(grace_seconds=0.05) is False
