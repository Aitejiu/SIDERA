from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.errors import AgentError

from sidera_control_plane.application.agent_sources import PlanningContextSourceReader
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.domain.context_packages import ContextPackageManifestV1
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestKind,
    PlanningRunLease,
    PlanningRunLeaseStatus,
)
from tests.unit.test_agent_planner import native_snapshot
from tests.unit.test_context_materialization import (
    FakeCatalog,
    _body,
    _knowledge_ref,
    _manifest,
)
from tests.unit.test_context_requests import fixed_planning_inputs


@pytest.fixture
def reader_case(request):
    run, brief, revision, now = fixed_planning_inputs()
    snapshot = native_snapshot(run.profile_snapshot)
    kind = getattr(request, "param", "create")
    graph = None
    if kind == "revision":
        from tests.unit.test_revision_planning_context import _build_context

        graph = _build_context()
        snapshot = native_snapshot(
            replace(
                snapshot,
                workspace_id=graph.base_descriptor.workspace_id,
                execution_mode="legacy",
                planner_agent_ref=None,
                planner_module_instance_id=uuid4(),
                planner_module_config_version=1,
                planner_capabilities=run.profile_snapshot.planner_capabilities,
            )
        )
    create = (
        PlanningRequest.create if graph is None else PlanningRequest.create_revision
    )
    request = create(
        request_id=brief.planning_request_id,
        workspace_id=snapshot.workspace_id,
        profile_snapshot=snapshot,
        objective=brief.objective,
        constraints=brief.constraints,
        acceptance_criteria=brief.acceptance_criteria,
        context_refs=brief.context_refs,
        requested_outputs=brief.requested_outputs,
        source_type="user",
        source_ref=None,
        actor="human",
        correlation_id=brief.source_correlation,
        run_id=run.id,
        now=now,
        **({} if graph is None else {"base": graph.base_descriptor}),
    )
    context = SimpleNamespace(
        id=revision.work_context_id, current_revision_id=revision.revision_id
    )
    if graph is not None:
        from sidera_control_plane.application.work_context_commands import (
            WorkContextRevisionFactory,
        )
        from sidera_control_plane.domain.work_context import (
            WorkContext,
            WorkContextRevisionReason,
        )

        factory = WorkContextRevisionFactory()
        original_brief = replace(brief, planning_request_id=uuid4())
        first = factory.create(
            work_context_id=uuid4(),
            workspace_id=snapshot.workspace_id,
            brief=original_brief,
            revision_number=1,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
            actor="human",
            created_at=now,
        )
        context = WorkContext.create(
            work_context_id=first.work_context_id,
            workspace_id=snapshot.workspace_id,
            planning_request_id=original_brief.planning_request_id,
            first_revision=first,
            created_at=now,
        )
        context.bind_root_task(
            graph.base_descriptor.root_task_id,
            expected_version=context.version,
            now=now,
        )
        revision = factory.create(
            work_context_id=context.id,
            workspace_id=snapshot.workspace_id,
            brief=request.briefs[0],
            revision_number=context.current_revision_number + 1,
            reason=WorkContextRevisionReason.REPLANNED,
            actor="human",
            created_at=now,
            base_snapshot=first.snapshot,
        )
        context.append_external_revision(
            revision, expected_version=context.version, now=now
        )
    run = request.runs[0]
    lease = PlanningRunLease(
        id=uuid4(),
        workspace_id=run.workspace_id,
        planning_run_id=run.id,
        owner_id="planner",
        status=PlanningRunLeaseStatus.ACTIVE,
        fencing_token=1,
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
        released_at=None,
        version=1,
    )
    request.start_planning(
        run_id=run.id, lease_id=lease.id, fencing_token=lease.fencing_token, now=now
    )
    reference = _knowledge_ref(ordinal=0, section_key="overview")
    facts = {
        "planning_request": {"id": str(request.id)},
        "planning_run": {"id": str(run.id), "operation_id": str(run.operation_id)},
        "brief": {
            "revision": brief.revision,
            "planning_request_id": str(request.id),
            "objective": brief.objective,
            "constraints": list(brief.constraints),
            "context_refs": list(brief.context_refs),
            "acceptance_criteria": list(brief.acceptance_criteria),
            "requested_outputs": list(brief.requested_outputs),
        },
        "planning_profile": {
            "profile_id": str(snapshot.profile_id),
            "profile_version": snapshot.profile_version,
        },
        "planner_agent": snapshot.planner_agent_ref.model_dump(mode="json"),
    }
    if graph is not None:
        from sidera_control_plane.application.revision_planning_context import (
            encode_native_revision_facts,
        )

        facts["revision_base"] = encode_native_revision_facts(graph)
    original = _manifest(
        revision, (reference,), created_at=now, expires_at=now + timedelta(minutes=30)
    )
    fields = {
        name: getattr(original, name)
        for name in (
            "package_id",
            "purpose",
            "consumer_type",
            "worker_item_id",
            "work_context_revision_id",
            "context_version",
            "knowledge_refs",
            "artifact_refs",
            "evidence_refs",
            "budget",
            "expires_at",
            "idempotency_key",
            "created_at",
        )
    }
    manifest = ContextPackageManifestV1.create(
        **fields,
        workspace_id=run.workspace_id,
        consumer_id=run.id,
        control_plane_fact_snapshot=facts,
    )
    state = SimpleNamespace(
        run=run,
        request=request,
        revision=revision,
        manifest=manifest,
        lease=lease,
        context=context,
        graph=graph,
        lookups=[],
        assembly=SimpleNamespace(
            request_id=uuid4(),
            request_fingerprint="fixed",
            status=SimpleNamespace(value="ready"),
            work_context_revision_id=revision.revision_id,
            budget={
                "planner_agent_ref": snapshot.planner_agent_ref.model_dump(mode="json")
            },
        ),
        active=0,
        allowed=True,
        reads=0,
    )
    if graph is not None:
        from sidera_control_plane.application.context_requests import (
            build_planning_context_assembly_request,
        )

        state.assembly.budget = build_planning_context_assembly_request(
            run=run,
            brief=request.briefs[0],
            work_context_revision=revision,
            ttl_seconds=1800,
            created_at=now,
            planning_kind=PlanningRequestKind.REVISION,
            revision_base=graph.base_descriptor,
        ).budget

    def by_request(request_id, workspace_id):
        state.lookups.append(("request", request_id, workspace_id))
        return state.context if graph is None else None

    def by_root(root_id, workspace_id):
        state.lookups.append(("root", root_id, workspace_id))
        if state.graph is not None:
            assert (root_id, workspace_id) == (
                state.graph.base_descriptor.root_task_id,
                state.run.workspace_id,
            )
        return state.context

    class UoW:
        def __enter__(self):
            state.active += 1
            self.planning_runs = SimpleNamespace(get=lambda *args: state.run)
            self.planning_requests = SimpleNamespace(get=lambda *args: state.request)
            self.planning_leases = SimpleNamespace(
                get_active_for_run=lambda *args: state.lease
            )
            self.context_packages = SimpleNamespace(
                get_for_consumer=lambda *args: state.manifest
            )
            self.work_contexts = SimpleNamespace(
                get_revision=lambda *args: state.revision,
                get_by_planning_request=by_request,
                get_by_root_task=by_root,
            )
            self.context_assemblies = SimpleNamespace(
                get_for_consumer=lambda *args: state.assembly
            )
            if graph is not None:
                from sidera_control_plane.domain.plans import TaskPlanSnapshot

                self.plans = SimpleNamespace(
                    get=lambda *args: TaskPlanSnapshot.from_payload(
                        graph.to_mapping()["plan_snapshot"]
                    )
                )
            return self

        def __exit__(self, *args):
            state.active -= 1

    class Scope:
        def require(self, grant_id, action, target):
            assert (
                action,
                target.planning_run_id,
                target.operation_id,
                target.profile_ref,
            ) == ("source.read", run.id, run.operation_id, snapshot.planner_agent_ref)
            assert target.target == manifest
            if not state.allowed:
                raise AgentError("permission_denied", "Denied.")

    return state, UoW, Scope(), reference, now


@pytest.mark.parametrize("loss", [None, "lease", "context", "grant"])
def test_preparation_releases_uow_and_revalidates_after_external_io(reader_case, loss):
    state, uow_factory, scope, reference, now = reader_case
    original_lease_id = state.lease.id

    class Catalog(FakeCatalog):
        def materialize(self, workspace_id, sections, **kwargs):
            assert state.active == 0
            assert tuple(item.ordinal for item in sections) == (0,)
            state.reads += 1
            if loss == "lease":
                state.lease = replace(state.lease, fencing_token=2)
            elif loss == "context":
                state.context.current_revision_id = uuid4()
            elif loss == "grant":
                state.allowed = False
            body = _body(reference, "body-overview")
            return (
                replace(
                    body,
                    sources=tuple(
                        replace(item, workspace_id=workspace_id)
                        for item in body.sources
                    ),
                ),
            )

    reader = PlanningContextSourceReader(
        uow_factory,
        authorizer=None,
        materializer=ContextMaterializer(Catalog()),
        clock=lambda: now,
    )

    def prepare():
        return reader.prepare_facts(
            state.run.workspace_id,
            state.run.id,
            principal_id="human",
            grant_id=uuid4(),
            upstream_authorizer=scope,
            owner_id="planner",
            lease_id=original_lease_id,
            fencing_token=1,
        )

    if loss is None:
        facts, _assembly_pin = prepare()
        assert facts.manifest == state.manifest
        assert facts.project_knowledge[0].body_markdown == "body-overview"
    else:
        with pytest.raises(AgentError) as denied:
            prepare()
        assert denied.value.code == "permission_denied"
    assert state.reads == 1
    assert state.active == 0


def test_preparation_denied_before_external_body_io(reader_case):
    state, uow_factory, scope, _reference, now = reader_case
    state.allowed = False
    catalog = FakeCatalog()
    reader = PlanningContextSourceReader(
        uow_factory,
        authorizer=None,
        materializer=ContextMaterializer(catalog),
        clock=lambda: now,
    )
    with pytest.raises(AgentError):
        reader.prepare_facts(
            state.run.workspace_id,
            state.run.id,
            principal_id="human",
            grant_id=uuid4(),
            upstream_authorizer=scope,
            owner_id="planner",
            lease_id=state.lease.id,
            fencing_token=1,
        )
    assert catalog.materialize_calls == 0


def test_preparation_rejects_original_claim_and_active_lease_fence_disagreement(
    reader_case,
):
    state, uow_factory, scope, _reference, now = reader_case
    state.lease = replace(state.lease, fencing_token=2)
    catalog = FakeCatalog()
    reader = PlanningContextSourceReader(
        uow_factory,
        authorizer=None,
        materializer=ContextMaterializer(catalog),
        clock=lambda: now,
    )
    with pytest.raises(AgentError):
        reader.prepare_facts(
            state.run.workspace_id,
            state.run.id,
            principal_id="human",
            grant_id=uuid4(),
            upstream_authorizer=scope,
            owner_id="planner",
            lease_id=state.lease.id,
            fencing_token=1,
        )
    assert catalog.materialize_calls == 0


@pytest.mark.parametrize("reader_case", ["revision"], indirect=True)
@pytest.mark.parametrize(
    "loss", [None, "missing", "root", "workspace", "lineage", "stale"]
)
def test_revision_preparation_uses_exact_retained_root_context_without_request_fallback(
    reader_case, loss
):
    state, uow_factory, scope, reference, now = reader_case
    assert state.context.planning_request_id != state.request.id
    if loss == "missing":
        state.context = None
    elif loss == "root":
        state.context.root_task_id = uuid4()
    elif loss == "workspace":
        state.request = replace(state.request, workspace_id=uuid4())
    elif loss == "lineage":
        state.context.id = uuid4()
    elif loss == "stale":
        state.context.current_revision_id = uuid4()

    class Catalog(FakeCatalog):
        def materialize(self, workspace_id, sections, **kwargs):
            assert state.active == 0
            state.reads += 1
            body = _body(reference, "body-overview")
            return (
                replace(
                    body,
                    sources=tuple(
                        replace(item, workspace_id=workspace_id)
                        for item in body.sources
                    ),
                ),
            )

    reader = PlanningContextSourceReader(
        uow_factory,
        authorizer=None,
        materializer=ContextMaterializer(Catalog()),
        clock=lambda: now,
    )

    def prepare():
        return reader.prepare_facts(
            state.run.workspace_id,
            state.run.id,
            principal_id="human",
            grant_id=uuid4(),
            upstream_authorizer=scope,
            owner_id="planner",
            lease_id=state.lease.id,
            fencing_token=1,
        )

    if loss is None:
        assert prepare()[0].manifest == state.manifest and state.reads == 1
    else:
        with pytest.raises(AgentError) as denied:
            prepare()
        assert denied.value.code == "permission_denied" and state.reads == 0
    assert not any(kind == "request" for kind, *_ in state.lookups)
    assert state.active == 0


def test_create_context_has_no_root_fallback(reader_case):
    state, uow_factory, scope, _reference, now = reader_case
    state.context = None
    catalog = FakeCatalog()
    reader = PlanningContextSourceReader(
        uow_factory,
        authorizer=None,
        materializer=ContextMaterializer(catalog),
        clock=lambda: now,
    )
    with pytest.raises(AgentError):
        reader.prepare_facts(
            state.run.workspace_id,
            state.run.id,
            principal_id="human",
            grant_id=uuid4(),
            upstream_authorizer=scope,
            owner_id="planner",
            lease_id=state.lease.id,
            fencing_token=1,
        )
    assert catalog.materialize_calls == 0 and all(
        kind == "request" for kind, *_ in state.lookups
    )
