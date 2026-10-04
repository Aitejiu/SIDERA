from uuid import uuid4

import pytest
from sidera_agents.errors import AgentError
from sqlalchemy import func, select, update

from sidera_control_plane.application.collaboration_reconciler import (
    CollaborationReconciler,
)
from sidera_control_plane.gateway_owner import require_gateway_owner
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.models import (
    CollaborationEvaluationModel,
    CollaborationRunModel,
    ModuleDefinitionModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_to_payload,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_collaboration_reconciler import NOW, _capability, _snapshot
from tests.integration.test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)
from tests.support.native_agents import (
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)
from tests.unit.test_collaboration_reconciler import FakeRuntimeResolver

pytestmark = pytest.mark.integration


def seed_scope(database, uow_factory, workspace_id):
    ids = seed_execution_request_facts(database, workspace_id)
    snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        snapshot_id=ids["plan"],
        definition_id=ids["module_definition"],
        instance_id=ids["module_instance"],
    )
    with database.session() as session:
        request = request_row(ids, request_id=ids["request"])
        request.workspace_id = workspace_id
        request.collaboration_gate_mode = "release_required"
        request.gate_reason = "collaboration_run_pending"
        session.add(request)
        session.execute(
            update(ModuleDefinitionModel)
            .where(ModuleDefinitionModel.id == ids["module_definition"])
            .values(
                module_type="collaboration_runtime",
                provider="reference",
                interface_version="collaboration-runtime.v1",
                capabilities=capability_profile_to_payload(_capability()),
            )
        )
        session.execute(
            update(PlanSnapshotModel)
            .where(PlanSnapshotModel.id == snapshot.id)
            .values(
                schema_version=snapshot.schema_version,
                payload=snapshot.to_payload(),
            )
        )
        session.commit()
    from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash

    with database.session() as session:
        session.add(
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=snapshot.root_task_id,
                active_snapshot_id=snapshot.id,
                active_generation=1,
                active_snapshot_hash=plan_snapshot_content_hash(snapshot),
                version=1,
                updated_at=NOW,
            )
        )
        session.commit()
    with uow_factory() as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_runtime",
            resource_id=snapshot.root_task_id,
            change_type="plan.published",
            resource_version=1,
            correlation_id=uuid4(),
        )
    return snapshot


def test_saved_scope_keyset_and_live_evaluations_do_not_starve_second_workspace(
    database, uow_factory, workspace_id
):
    snapshots = [
        seed_scope(database, uow_factory, ws) for ws in (workspace_id, uuid4())
    ]
    expected = tuple(
        sorted((item.workspace_id, item.root_task_id) for item in snapshots)
    )
    with uow_factory() as uow:
        assert uow.plans.list_root_scopes_with_snapshots(limit=1) == expected[:1]
        assert (
            uow.plans.list_root_scopes_with_snapshots(after=expected[0], limit=1)
            == expected[1:]
        )
        assert (
            uow.plans.list_root_scopes_with_snapshots(after=expected[-1], limit=1) == ()
        )
        for limit in (0, -1, 201):
            with pytest.raises(ValueError):
                uow.plans.list_root_scopes_with_snapshots(limit=limit)
        assert uow.plans.list_roots_with_snapshots(expected[0][0]) == (expected[0][1],)
        cursors = {
            pair: uow.projection_changes.latest_target_cursor(
                pair[0], "collaboration_runtime", pair[1]
            )
            for pair in expected
        }
    reconciler = CollaborationReconciler(
        uow_factory=uow_factory,
        fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
            session=uow.session
        ),
        runtime_resolver_factory=lambda _: FakeRuntimeResolver(),
        scan_all_workspaces=True,
        recovery_limit=1,
    )
    prepared = [
        reconciler.prepare_next(owner_id="scope-owner", now=NOW) for _ in range(2)
    ]
    assert (
        tuple((item.run.workspace_id, item.run.root_task_id) for item in prepared)
        == expected
    )
    assert reconciler.prepare_next(owner_id="scope-owner", now=NOW) is None
    with uow_factory() as uow:
        for item in prepared:
            projection = uow.collaboration_ledger.get_projection(
                item.run.workspace_id, item.evaluation.projection_snapshot_id
            )
            assert (
                projection.fact_cursor
                == cursors[(item.run.workspace_id, item.run.root_task_id)]
            )
        assert (
            uow.session.scalar(
                select(func.count()).select_from(CollaborationEvaluationModel)
            )
            == 2
        )


def test_owner_loss_rolls_back_owner_wide_preparation(
    database, uow_factory, workspace_id
):
    snapshot = seed_scope(database, uow_factory, workspace_id)
    alive = [True]

    def owned_uow():
        require_gateway_owner(lambda: alive[0])
        return SqlAlchemyUnitOfWork(
            database, before_commit=lambda: require_gateway_owner(lambda: alive[0])
        )

    class LosingFacts(SqlAlchemyCollaborationFactStore):
        def load(self, *args, **kwargs):
            facts = super().load(*args, **kwargs)
            alive[0] = False
            return facts

    reconciler = CollaborationReconciler(
        uow_factory=owned_uow,
        fact_store_factory=lambda uow: LosingFacts(session=uow.session),
        runtime_resolver_factory=lambda _: FakeRuntimeResolver(),
        scan_all_workspaces=True,
    )
    with pytest.raises(AgentError) as denied:
        reconciler.prepare_next(owner_id="scope-owner", now=NOW)
    assert denied.value.code == "gateway_owner_lost"
    with uow_factory() as uow:
        assert (
            uow.session.scalar(select(func.count()).select_from(CollaborationRunModel))
            == 0
        )
        assert (
            uow.session.scalar(
                select(func.count()).select_from(CollaborationEvaluationModel)
            )
            == 0
        )
        assert uow.plans.get(snapshot.id, workspace_id) == snapshot
