"""Preserve frozen legacy INPUT, without replaying legacy execution/acceptance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, func, insert, null, select, text

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.result_acceptance import (
    build_acceptance_basis_hash,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.input_selections import CollaborationInputSelectionV1
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.planning import PlanningBriefRevision, PlanningUsage
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    PlanningProposalPayloadV6,
    canonical_proposal_bytes,
    derive_proposal_diff,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.result_verification import (
    CriterionDecisionV1,
    TaskAcceptanceDecisionV1,
    VerificationResultV1,
    evaluate_criteria,
)
from sidera_control_plane.domain.work_context import (
    WorkContextRevision,
    WorkContextRevisionReason,
)
from sidera_control_plane.persistence.collaboration_repository import (
    _decision_model,
    _evaluation_values,
    _intent_model,
    _projection_values,
    _run_model,
)
from sidera_control_plane.persistence.context_package_repository import (
    _assembly_request_model,
    _package_model,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.migration_resources import migration_config
from sidera_control_plane.persistence.models import (
    AgentExecutionLinkModel,
    PlanningBriefRevisionModel,
    PlanningRequestModel,
    PlanningRunModel,
)
from sidera_control_plane.persistence.planning_repository import (
    _brief_model,
    _diff_to_mapping,
    _profile_policy,
    _snapshot_from_mapping,
    _snapshot_to_mapping,
    _to_brief,
    _to_request,
    _to_run,
    _usage_from_mapping,
    _usage_to_mapping,
)
from sidera_control_plane.persistence.result_repository import (
    _acceptance_values,
    _candidate_values,
    _snapshot_values,
    _verification_values,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from sidera_control_plane.persistence.work_context_repository import _revision_model
from tests.integration.test_collaboration_repository import (
    _decision,
    _evaluation,
    _intent,
    _projection,
    _run,
)
from tests.integration.test_context_package_repository import (
    _manifest,
    _request,
    _snapshot,
)
from tests.integration.test_planning_repository import capabilities, make_profile
from tests.support.native_agents import (
    NativeCPTestGuard,
    guard_native_cp_test_target,
)
from tests.unit.result_input_fixtures import _selection_hash_payload, selection_mapping
from tests.unit.result_integration_fixtures import (
    _snapshot_content_hash,
    acceptance_mapping,
    candidate_mapping,
    proposal_v6_mapping,
    snapshot_mapping,
    verification_mapping,
)
from tests.unit.test_plan_snapshot_v4 import policy_v2_payload

pytestmark = pytest.mark.integration

LEGACY_HEAD = "0033_execution_purpose_text"
NATIVE_HEAD = "0038_native_assignee_handoff"
CURRENT_HEAD = "0039_hard_only_input_selections"
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid5(NAMESPACE_URL, "sidera:n16:seeded_legacy_fixture:workspace")


def _id(name: str) -> UUID:
    return uuid5(WORKSPACE_ID, name)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


@pytest.fixture(scope="session", autouse=True)
def native_cp_test_target_guard(database_url: str) -> Iterator[NativeCPTestGuard]:
    with guard_native_cp_test_target(database_url) as guard:
        yield guard


@pytest.fixture(scope="session")
def database(
    database_url: str, native_cp_test_target_guard: NativeCPTestGuard
) -> Iterator[Database]:
    guard = native_cp_test_target_guard
    assert guard.active and guard.target.marker.endswith(":legacy")
    instance = Database(database_url)
    assert instance.engine.url == guard.target.url
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture
def clean_database(database: Database, native_cp_test_target_guard: NativeCPTestGuard):
    """Override head-first legacy cleanup; retain the completed migration input."""
    assert native_cp_test_target_guard.active
    assert database.engine.url == native_cp_test_target_guard.target.url


def _upgrade(database: Database, revision: str) -> None:
    config = migration_config()
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == revision
        )


def _insert(connection, metadata: MetaData, table_name: str, **values) -> None:
    table = metadata.tables[table_name]
    assert set(values) <= set(table.c.keys()), (
        table_name,
        set(values) - set(table.c.keys()),
    )
    defaults = {
        "workspace_id": WORKSPACE_ID,
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    row = {key: value for key, value in defaults.items() if key in table.c}
    connection.execute(
        insert(table).values(
            **{
                key: null() if value is None else value
                for key, value in (row | values).items()
            }
        )
    )


def _insert_model(connection, metadata: MetaData, model) -> None:
    _insert(
        connection,
        metadata,
        model.__tablename__,
        **{key: value for key, value in vars(model).items() if not key.startswith("_")},
    )


def _plan(proposal: PlanningProposalPayloadV6, *, generation: int) -> TaskPlanSnapshot:
    raw = proposal.to_mapping()
    policy = policy_v2_payload()
    policy.update(
        runtime_definition_id=str(_id("collaboration-definition")),
        runtime_instance_id=str(_id("collaboration-module")),
        runtime_config_version=1,
        input_groups=raw["collaboration_policy_request"]["input_groups"],
    )
    return TaskPlanSnapshot.from_payload(
        {
            "id": str(_id(f"plan-{generation}")),
            "schema_version": 5,
            "workspace_id": str(WORKSPACE_ID),
            "root_task_id": str(_id("root-task")),
            "generation": generation,
            "parent_snapshot_id": str(_id("plan-1")) if generation == 2 else None,
            "source_type": "seeded_legacy_fixture",
            "source_ref": "n16:frozen0033-input",
            "title": proposal.root_task.title,
            "goal_summary": proposal.root_task.goal_summary,
            "acceptance_refs": list(proposal.root_task.acceptance_refs),
            "subtasks": [
                item | {"task_id": str(_id(f"task-{item['key']}"))}
                for item in raw["subtasks"]
            ],
            "worker_items": [
                {key: value for key, value in item.items() if key != "pinned_worker_id"}
                | {"worker_item_id": str(_id(item["key"]))}
                for item in raw["worker_items"]
            ],
            "milestones": raw["milestones"],
            "subtask_dependencies": raw["subtask_dependencies"],
            "worker_item_dependencies": raw["worker_item_dependencies"],
            "collaboration_policy": policy,
            "integration_spec": raw["integration_spec"],
            "published_by": "seeded_legacy_fixture",
            "published_at": NOW.isoformat(),
        }
    )


def _seed(database: Database) -> dict[str, object]:
    """Insert old reflected columns atomically; every terminal value is INPUT."""
    raw = proposal_v6_mapping()
    raw["integration_spec"]["verification_binding"].update(
        adapter_module_ref=str(_id("verification-module")),
        workspace_module_ref=str(_id("workspace-module")),
    )
    proposal = PlanningProposalPayloadV6.from_mapping(raw)
    brief = PlanningBriefRevision.create(
        planning_request_id=_id("planning-request"),
        revision=1,
        objective=proposal.root_task.goal_summary,
        constraints=("Preserve seeded legacy INPUT without execution replay.",),
        acceptance_criteria=proposal.root_task.acceptance_refs,
        context_refs=(),
        requested_outputs=tuple(
            dict.fromkeys(
                output
                for subtask in proposal.subtasks
                for output in subtask.expected_outputs
            )
        ),
        user_feedback=None,
        source_correlation=_id("correlation"),
        created_by="seeded_legacy_fixture",
        created_at=NOW,
    )
    brief_row = _brief_model(brief, WORKSPACE_ID)
    assert _to_brief(brief_row) == brief
    usage = PlanningUsage.unknown()
    usage_payload = _usage_to_mapping(usage)
    assert len(usage_payload) == 5 and all(
        value is None for value in usage_payload.values()
    )
    assert _usage_from_mapping(usage_payload) == usage
    plans = tuple(_plan(proposal, generation=generation) for generation in (1, 2))
    plan = plans[-1]
    plan_hash = plan_snapshot_content_hash(plan)
    profile = replace(
        make_profile(WORKSPACE_ID, _id("planner-module")),
        id=_id("planning-profile"),
        profile_key="seeded-legacy",
        proposal_schema_version=6,
        planner_secret_refs=(),
        collaboration_runtime_binding=CollaborationRuntimeBindingSnapshot(
            runtime_definition_id=plan.collaboration_policy.runtime_definition_id,
            runtime_instance_id=plan.collaboration_policy.runtime_instance_id,
            runtime_interface_version=plan.collaboration_policy.runtime_interface_version,
            runtime_config_version=plan.collaboration_policy.runtime_config_version,
            runtime_capability_snapshot=plan.collaboration_policy.runtime_capability_snapshot,
        ),
    )
    profile_snapshot = _snapshot_to_mapping(
        profile.snapshot(replace(capabilities(), proposal_schema_versions=(6,)))
    )
    # 0033 persisted no native discriminator or Agent Profile pin in these JSON codecs.
    profile_snapshot.pop("execution_mode")
    profile_snapshot.pop("planner_agent_ref")
    decoded_profile_snapshot = _snapshot_from_mapping(profile_snapshot)
    assert decoded_profile_snapshot.execution_mode == "legacy"
    assert decoded_profile_snapshot.planner_agent_ref is None
    canonical = canonical_proposal_bytes(
        proposal,
        brief_revision=1,
        profile_descriptor=PlanningProfileDescriptor(
            profile.id, 1, _id("planner-module"), 1
        ),
    )
    source_raw = snapshot_mapping(
        id=str(_id("source-result")),
        workspace_id=str(WORKSPACE_ID),
        root_task_id=str(plan.root_task_id),
        worker_item_id=str(_id("build-worker")),
        request_id=str(_id("request-build")),
        execution_id=str(_id("execution-build")),
        artifact_ids=[str(_id("source-artifact"))],
        capture_operation_id=str(_id("capture-build")),
    )
    source_raw["content_hash"] = _snapshot_content_hash(source_raw)
    source = CodeResultSnapshotV1.from_mapping(source_raw)
    run = replace(
        _run(
            {
                "root_task": plan.root_task_id,
                "plan": plan.id,
                "module_definition": _id("collaboration-definition"),
                "module_instance": _id("collaboration-module"),
            },
            WORKSPACE_ID,
        ),
        id=_id("collaboration-run"),
        plan_generation=2,
        plan_content_hash=plan_hash,
        runtime_interface_version=plan.collaboration_policy.runtime_interface_version,
        runtime_capability_snapshot=plan.collaboration_policy.runtime_capability_snapshot,
        policy_snapshot=plan.collaboration_policy.to_mapping(),
    )
    projection = replace(_projection(run), id=_id("projection"))
    projection = replace(projection, content_hash=_hash(projection.payload))
    evaluation = replace(
        _evaluation(run, projection), id=_id("evaluation"), operation_id=_id("evaluate")
    )
    decision = replace(
        _decision(run, projection, evaluation, sequence=1), id=_id("decision")
    )
    intent = replace(_intent(run, decision, _id("request-review")), id=_id("intent"))
    selection_raw = selection_mapping()
    selection_raw.update(
        id=str(_id("selection")),
        workspace_id=str(WORKSPACE_ID),
        root_task_id=str(plan.root_task_id),
        run_id=str(run.id),
        plan_snapshot_id=str(plan.id),
        plan_generation=2,
        plan_content_hash=plan_hash,
        target_worker_item_id=str(_id("review-worker")),
        target_execution_request_id=str(_id("request-review")),
        target_request_version=1,
        projection_snapshot_id=str(projection.id),
        projection_hash=projection.content_hash,
        decision_id=str(decision.id),
        intent_id=str(intent.id),
        created_at=NOW.isoformat(),
    )
    group = selection_raw["groups"][0]
    group.update(
        group_key="implementation",
        rule_hash=_hash(raw["collaboration_policy_request"]["input_groups"][0]),
    )
    group["sources"] = group["sources"][:1]
    selected_source = group["sources"][0]
    content = b"legacy source diff\n"
    artifact_hash = hashlib.sha256(content).hexdigest()
    selected_source.update(
        worker_item_key="build-worker",
        worker_item_id=str(source.worker_item_id),
        subtask_path=["build"],
        effective_request_id=str(source.request_id),
        allowed_lineage_ids=[str(source.execution_id)],
        effective_lineage_id=str(source.execution_id),
        effective_execution_id=str(source.execution_id),
        source_basis_hash=source.content_hash,
    )
    selected_source["artifacts"][0].update(
        artifact_id=str(_id("source-artifact")),
        artifact_type="build",
        content_hash=artifact_hash,
        size_bytes=len(content),
    )
    selected_source["evidence"][0].update(
        evidence_id=str(_id("source-evidence")),
        acceptance_criterion_ref="acceptance:build",
        artifact_ids=[str(_id("source-artifact"))],
    )
    selection_raw["groups"] = [group]
    selection_raw["selection_hash"] = _hash(_selection_hash_payload(selection_raw))
    selection = CollaborationInputSelectionV1.from_mapping(selection_raw)
    selection = replace(selection, selection_hash=selection.compute_selection_hash())
    result_raw = snapshot_mapping(
        id=str(_id("integrated-result")),
        workspace_id=str(WORKSPACE_ID),
        root_task_id=str(plan.root_task_id),
        worker_item_id=str(_id("review-worker")),
        request_id=str(_id("request-review")),
        execution_id=str(_id("execution-review")),
        consumed_result_ids=[str(source.id)],
        artifact_ids=[],
        capture_operation_id=str(_id("capture-review")),
    )
    result_raw["content_hash"] = _snapshot_content_hash(result_raw)
    result = CodeResultSnapshotV1.from_mapping(result_raw)
    candidate_raw = candidate_mapping(
        id=str(_id("candidate")),
        workspace_id=str(WORKSPACE_ID),
        root_task_id=str(plan.root_task_id),
        plan_snapshot_id=str(plan.id),
        plan_content_hash=plan_hash,
        integration_execution_id=str(result.execution_id),
        source_result_ids=[str(source.id)],
        input_selection_id=str(selection.id),
        input_selection_hash=selection.selection_hash,
        code_result_id=str(result.id),
    )
    candidate_raw["content_hash"] = _hash(
        {
            key: value
            for key, value in candidate_raw.items()
            if key not in {"id", "created_at", "content_hash"}
        }
    )
    candidate = IntegrationCandidateV1.from_mapping(candidate_raw)
    check = plan.integration_spec.checks[0]
    verification = VerificationResultV1.from_mapping(
        verification_mapping(
            id=str(_id("verification")),
            workspace_id=str(WORKSPACE_ID),
            root_task_id=str(plan.root_task_id),
            candidate_id=str(candidate.id),
            execution_id=str(_id("execution-verify")),
            check_hash=_hash(check.to_mapping()),
            status="passed",
            exit_code=0,
            failure_code=None,
            artifact_ids=[],
            operation_id=str(_id("verify")),
        )
    )
    criteria = (
        CriterionDecisionV1.from_mapping(
            {
                "criterion_key": "login",
                "decision": "accept_passed",
                "reason": None,
            }
        ),
    )
    observed = evaluate_criteria(
        plan.integration_spec.criteria, (verification,), criteria
    )
    assert all(item.observed_status.value == "passed" for item in observed)
    acceptance = TaskAcceptanceDecisionV1.from_mapping(
        acceptance_mapping(
            id=str(_id("acceptance")),
            workspace_id=str(WORKSPACE_ID),
            root_task_id=str(plan.root_task_id),
            candidate_id=str(candidate.id),
            candidate_hash=candidate.content_hash,
            plan_content_hash=plan_hash,
            criteria=[criterion.to_mapping() for criterion in criteria],
            acceptance_basis_hash=build_acceptance_basis_hash(
                candidate,
                (verification,),
                criteria,
                result_version=1,
                integration_spec=plan.integration_spec,
            ),
            actor_id="seeded_legacy_fixture",
            command_id=str(_id("acceptance-command")),
        )
    )
    assembly = _request(
        workspace_id=WORKSPACE_ID,
        revision_id=_id("context-revision"),
        consumer_id=_id("planning-run"),
        request_id=_id("assembly"),
    )
    manifest = _manifest(
        workspace_id=WORKSPACE_ID,
        revision_id=_id("context-revision"),
        consumer_id=_id("planning-run"),
        request_id=_id("planning-request"),
    )
    manifest = replace(
        manifest,
        package_id=_id("package"),
        knowledge_refs=(
            replace(
                manifest.knowledge_refs[0],
                page_id=_id("knowledge-page"),
                revision_id=_id("knowledge-revision"),
                source_ids=(_id("knowledge-source"),),
            ),
        ),
        budget=replace(manifest.budget, profile_id=_id("context-budget")),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    assembly = replace(
        assembly,
        budget={**assembly.budget, "profile_id": str(_id("context-budget"))},
        created_at=NOW,
        updated_at=NOW,
        expires_at=manifest.expires_at,
    )
    revision = WorkContextRevision.create(
        revision_id=_id("context-revision"),
        work_context_id=_id("context"),
        workspace_id=WORKSPACE_ID,
        revision_number=1,
        brief_planning_request_id=_id("planning-request"),
        brief_revision=1,
        proposal_revision_id=_id("proposal"),
        plan_snapshot_id=plan.id,
        reason=WorkContextRevisionReason.PROPOSAL_CONFIRMED,
        snapshot=_snapshot(),
        created_by="seeded_legacy_fixture",
        created_at=NOW,
    )
    with database.engine.begin() as connection:
        metadata = MetaData()
        metadata.reflect(connection)
        assert "agent_execution_links" not in metadata.tables
        assert "execution_mode" not in metadata.tables["planning_profiles"].c
        _insert(
            connection,
            metadata,
            "workspaces",
            id=WORKSPACE_ID,
            name="Frozen legacy INPUT",
            status="active",
        )
        for name, module_type, interface in (
            ("planner", "planner_adapter", "sidera.planner.v1"),
            ("collaboration", "collaboration_runtime", "collaboration-runtime-v2"),
            ("verification", "worker_adapter", "worker-adapter-v1"),
            ("workspace", "workspace_provider", "workspace-provider-v1"),
        ):
            _insert(
                connection,
                metadata,
                "module_definitions",
                id=_id(f"{name}-definition"),
                module_type=module_type,
                provider="frozen-legacy",
                interface_version=interface,
                config_schema_version=1,
                capabilities={},
                lifecycle="active",
            )
            _insert(
                connection,
                metadata,
                "module_instances",
                id=_id(f"{name}-module"),
                definition_id=_id(f"{name}-definition"),
                instance_name=f"legacy-{name}",
                config_version=1,
                config_namespace="seeded_legacy_fixture",
                config={},
                secret_refs=[],
                lifecycle="active",
            )
        _insert(
            connection,
            metadata,
            "planning_profiles",
            id=profile.id,
            profile_key=profile.profile_key,
            display_name=profile.display_name,
            planner_module_instance_id=profile.planner_module_instance_id,
            planner_module_config_version=1,
            planner_interface_version=profile.planner_interface_version,
            status="active",
            user_selectable=True,
            policy=_profile_policy(profile),
        )
        _insert(
            connection,
            metadata,
            "planning_requests",
            id=_id("planning-request"),
            kind="create",
            status="materialized",
            planning_profile_id=profile.id,
            planning_profile_version=1,
            profile_snapshot=profile_snapshot,
            source_type="seeded_legacy_fixture",
            actor="seeded_legacy_fixture",
            correlation_id=_id("correlation"),
        )
        _insert_model(connection, metadata, brief_row)
        _insert(
            connection,
            metadata,
            "planning_runs",
            id=_id("planning-run"),
            planning_request_id=_id("planning-request"),
            brief_revision=1,
            planning_profile_id=profile.id,
            planning_profile_version=1,
            planner_module_instance_id=profile.planner_module_instance_id,
            planner_module_config_version=1,
            attempt=1,
            operation_id=_id("planning-operation"),
            status="succeeded",
            result_kind="proposal",
            capability_snapshot=profile_snapshot["planner_capabilities"],
            usage=usage_payload,
            diagnostic_refs=[],
            queued_at=NOW,
            started_at=NOW,
            finished_at=NOW,
        )
        canonical_value = json.loads(canonical)
        _insert(
            connection,
            metadata,
            "planning_proposal_revisions",
            id=_id("proposal"),
            planning_request_id=_id("planning-request"),
            revision=1,
            brief_revision=1,
            planning_run_id=_id("planning-run"),
            proposal_schema_version=6,
            kind="create",
            profile_id=profile.id,
            profile_version=1,
            planner_module_instance_id=profile.planner_module_instance_id,
            planner_module_config_version=1,
            decision_summary=raw["decision_summary"],
            plan_template={
                key: value
                for key, value in raw.items()
                if key
                not in {
                    "schema_version",
                    "kind",
                    "decision_summary",
                    "impact_policy",
                    "execution_intents",
                }
            },
            execution_intents=raw["execution_intents"],
            impact_policy=raw["impact_policy"],
            derived_diff=_diff_to_mapping(derive_proposal_diff(proposal.to_legacy())),
            canonical_payload=canonical_value,
            content_hash=hashlib.sha256(canonical).hexdigest(),
        )
        connection.execute(
            metadata.tables["planning_requests"]
            .update()
            .values(
                current_brief_revision=1,
                current_proposal_revision=1,
                current_proposal_content_hash=hashlib.sha256(canonical).hexdigest(),
            )
        )
        for task_id, title, criteria in (
            (plan.root_task_id, plan.title, list(plan.acceptance_refs)),
            *[
                (_id(f"task-{item.key}"), item.title, list(item.acceptance_refs))
                for item in plan.subtasks
            ],
        ):
            _insert(
                connection,
                metadata,
                "tasks",
                id=task_id,
                title=title,
                description="seeded_legacy_fixture; legacy_execution_replay=NOT_RUN",
                acceptance_criteria=criteria,
                status="succeeded",
                priority=50,
                requested_by="seeded_legacy_fixture",
            )
        for snapshot in plans:
            _insert(
                connection,
                metadata,
                "task_plan_snapshots",
                id=snapshot.id,
                schema_version=5,
                root_task_id=plan.root_task_id,
                generation=snapshot.generation,
                parent_snapshot_id=snapshot.parent_snapshot_id,
                source_type=snapshot.source_type,
                source_ref=snapshot.source_ref,
                title=snapshot.title,
                goal_summary=snapshot.goal_summary,
                payload=snapshot.to_payload(),
                published_by=snapshot.published_by,
                published_at=NOW,
            )
        _insert(
            connection,
            metadata,
            "root_task_plan_heads",
            root_task_id=plan.root_task_id,
            active_snapshot_id=plan.id,
            active_generation=2,
            active_snapshot_hash=plan_hash,
        )
        for item in plan.worker_item_plans:
            _insert(
                connection,
                metadata,
                "worker_items",
                id=item.worker_item_id,
                root_task_id=plan.root_task_id,
                stable_key=item.key,
                title=item.title,
                responsibility=item.responsibility,
                status="active",
                context_version=1,
                acknowledged_context_version=1,
                derived_from_worker_item_ids=[],
            )
            _insert(
                connection,
                metadata,
                "worker_item_placements",
                plan_snapshot_id=plan.id,
                plan_generation=2,
                root_task_id=plan.root_task_id,
                worker_item_id=item.worker_item_id,
                worker_item_key=item.key,
                subtask_key=item.subtask_key,
                role=item.role,
                capability_requirements=list(item.capability_requirements),
            )
        _insert(
            connection,
            metadata,
            "workers",
            id=_id("worker"),
            adapter_type="fake",
            display_name="Frozen legacy Worker",
            status="idle",
            capabilities=["code"],
            capacity=1,
            current_load=0,
            cost_per_hour_microunits=0,
            startup_latency_ms=0,
            last_heartbeat_at=NOW,
        )
        for key in ("build", "review", "verify"):
            task_key = "review" if key == "verify" else key
            _insert(
                connection,
                metadata,
                "executions",
                id=_id(f"execution-{key}"),
                task_id=_id(f"task-{task_key}"),
                lineage_id=_id(f"execution-{key}"),
                attempt=1,
                max_attempts=1,
                purpose=f"frozen legacy {key}",
                status="succeeded",
                capability_requirements=["code"],
                worker_item_id=_id(f"{task_key}-worker"),
                assigned_worker_id=_id("worker"),
                last_fencing_token=1,
                not_before=NOW,
                started_at=NOW,
                finished_at=NOW,
            )
        _insert(
            connection,
            metadata,
            "execution_sessions",
            id=_id("session"),
            execution_id=source.execution_id,
            worker_id=_id("worker"),
            adapter_session_id="frozen-legacy-session",
            status="closed",
            last_sequence=1,
            last_activity_at=NOW,
            input_mode="append",
        )
        for key in ("build", "review"):
            _insert(
                connection,
                metadata,
                "execution_requests",
                id=_id(f"request-{key}"),
                planning_request_id=_id("planning-request"),
                proposal_revision_id=_id("proposal"),
                plan_snapshot_id=plan.id,
                plan_generation=2,
                root_task_id=plan.root_task_id,
                subtask_task_id=_id(f"task-{key}"),
                worker_item_id=_id(f"{key}-worker"),
                worker_item_key=f"{key}-worker",
                intent_key=f"{key}-intent",
                purpose=f"frozen legacy {key}",
                capability_requirements=["code"],
                adapter_preferences=["fake"],
                budget_class="standard",
                not_before=NOW,
                max_attempts=1,
                expected_outputs=["diff"],
                expected_artifact_refs=["artifact_type:build"],
                expected_evidence_refs=[f"acceptance:{key}"],
                status="materialized",
                collaboration_gate_mode="unmanaged",
                gate_reason="ready",
                gate_details={},
                status_changed_at=NOW,
                execution_lineage_id=_id(f"execution-{key}"),
                first_execution_id=_id(f"execution-{key}"),
            )
        _insert_model(connection, metadata, _run_model(run))
        _insert(
            connection,
            metadata,
            "runtime_projection_snapshots",
            **_projection_values(projection),
        )
        _insert(
            connection,
            metadata,
            "collaboration_evaluations",
            **_evaluation_values(evaluation),
        )
        _insert_model(connection, metadata, _decision_model(decision))
        _insert_model(connection, metadata, _intent_model(intent))
        _insert(
            connection,
            metadata,
            "collaboration_input_selections",
            id=selection.id,
            schema_version=1,
            root_task_id=selection.root_task_id,
            run_id=run.id,
            plan_snapshot_id=plan.id,
            plan_generation=2,
            plan_content_hash=plan_hash,
            target_worker_item_id=selection.target_worker_item_id,
            target_execution_request_id=selection.target_execution_request_id,
            target_request_version=1,
            projection_snapshot_id=projection.id,
            projection_hash=projection.content_hash,
            decision_id=decision.id,
            intent_id=intent.id,
            groups_json=selection.to_mapping()["groups"],
            hard_inputs_json=[],
            selection_hash=selection.selection_hash,
        )
        _insert(
            connection,
            metadata,
            "artifacts",
            id=_id("source-artifact"),
            task_id=_id("task-build"),
            execution_id=source.execution_id,
            artifact_type="build",
            storage_ref="artifact://seeded-legacy-source",
            content_hash=artifact_hash,
            size_bytes=len(content),
            media_type="text/plain",
            provenance={"seeded_legacy_fixture": True},
        )
        _insert(
            connection,
            metadata,
            "evidence_claims",
            id=_id("source-evidence"),
            task_id=_id("task-build"),
            execution_id=source.execution_id,
            acceptance_criterion_ref="acceptance:build",
            conclusion="Frozen historical input",
            environment_ref="env://python-test",
            artifact_ids=[str(_id("source-artifact"))],
            producer="seeded_legacy_fixture",
            status="candidate",
        )
        for snapshot in (source, result):
            _insert(
                connection,
                metadata,
                "code_result_snapshots",
                **_snapshot_values(snapshot),
            )
        _insert(
            connection,
            metadata,
            "integration_candidates",
            **_candidate_values(candidate),
        )
        verification_values = _verification_values(verification) | {"created_at": NOW}
        _insert(
            connection, metadata, "result_verification_results", **verification_values
        )
        _insert(
            connection,
            metadata,
            "task_acceptance_decisions",
            **_acceptance_values(acceptance),
        )
        _insert(
            connection,
            metadata,
            "task_result_heads",
            root_task_id=plan.root_task_id,
            current_candidate_id=candidate.id,
            current_acceptance_id=acceptance.id,
            result_version=1,
            auto_rounds_used=0,
            auto_rounds_limit=2,
            auto_seconds_limit=1800,
        )
        _insert(
            connection,
            metadata,
            "commands",
            id=acceptance.command_id,
            command_type="CompleteTask",
            actor_type="local",
            actor_id=acceptance.actor_id,
            target_type="task",
            target_id=plan.root_task_id,
            payload={"seeded_legacy_fixture": True},
            expected_version=1,
            idempotency_key="frozen-legacy-acceptance",
            request_hash=_hash(acceptance.to_mapping()),
            correlation_id=_id("correlation"),
            status="applied",
            result={"status": "accepted", "legacy_execution_replay": "NOT_RUN"},
        )
        _insert(
            connection,
            metadata,
            "events",
            id=_id("acceptance-event"),
            aggregate_type="task",
            aggregate_id=plan.root_task_id,
            aggregate_version=1,
            event_type="task.completed",
            payload={
                "acceptance_id": str(acceptance.id),
                "seeded_legacy_fixture": True,
            },
            actor_type="local",
            actor_id=acceptance.actor_id,
            causation_id=acceptance.command_id,
            correlation_id=_id("correlation"),
        )
        _insert(
            connection,
            metadata,
            "work_contexts",
            id=_id("context"),
            planning_request_id=_id("planning-request"),
            root_task_id=plan.root_task_id,
            status="active",
            current_revision_id=revision.revision_id,
            current_revision_number=1,
        )
        _insert_model(connection, metadata, _revision_model(revision))
        _insert_model(connection, metadata, _assembly_request_model(assembly))
        _insert_model(connection, metadata, _package_model(assembly, manifest))
        for ref in manifest.knowledge_refs:
            _insert(
                connection,
                metadata,
                "context_package_knowledge_refs",
                package_id=manifest.package_id,
                ordinal=ref.ordinal,
                page_id=ref.page_id,
                revision_id=ref.revision_id,
                revision_hash=ref.revision_hash,
                section_key=ref.section_key,
                section_hash=ref.section_hash,
                source_ids=[str(item) for item in ref.source_ids],
                selection_reason=ref.selection_reason.value,
                trust_level=ref.trust_level.value,
            )
        # Decode the actual old planning columns before either forward migration.
        decoded_brief = _to_brief(
            PlanningBriefRevisionModel(
                **dict(
                    connection.execute(
                        select(metadata.tables["planning_brief_revisions"])
                    )
                    .mappings()
                    .one()
                )
            )
        )
        decoded_run = _to_run(
            PlanningRunModel(
                **dict(
                    connection.execute(select(metadata.tables["planning_runs"]))
                    .mappings()
                    .one()
                )
            ),
            profile_snapshot,
        )
        decoded_request = _to_request(
            PlanningRequestModel(
                **dict(
                    connection.execute(select(metadata.tables["planning_requests"]))
                    .mappings()
                    .one()
                )
            ),
            briefs=(decoded_brief,),
            runs=(decoded_run,),
            clarifications=(),
        )
        assert decoded_request.briefs == (brief,)
        assert decoded_request.runs[0].usage == usage
        assert decoded_request.profile_snapshot == decoded_profile_snapshot
        for snapshot in plans:
            assert TaskPlanSnapshot.from_payload(snapshot.to_payload()) == snapshot
        assert (
            CollaborationInputSelectionV1.from_mapping(selection.to_mapping())
            == selection
        )
        for snapshot in (source, result):
            assert (
                CodeResultSnapshotV1.from_mapping(_snapshot_values(snapshot)["payload"])
                == snapshot
            )
        assert (
            IntegrationCandidateV1.from_mapping(_candidate_values(candidate)["payload"])
            == candidate
        )
        assert (
            VerificationResultV1.from_mapping(
                _verification_values(verification)["payload"]
            )
            == verification
        )
        assert (
            TaskAcceptanceDecisionV1.from_mapping(acceptance.to_mapping()) == acceptance
        )
        assert _package_model(assembly, manifest).package_hash == manifest.package_hash
    return {
        "plan": plan,
        "plans": plans,
        "selection": selection,
        "source": source,
        "candidate": candidate,
        "result": result,
        "verification": verification,
        "acceptance": acceptance,
        "manifest": manifest,
        "brief": brief,
        "usage": usage,
    }


def _facts(database: Database, metadata: MetaData) -> dict[str, list[dict]]:
    with database.engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        return {
            table.name: sorted(
                [dict(row) for row in connection.execute(select(table)).mappings()],
                key=_json,
            )
            for _, table in sorted(metadata.tables.items())
            if table.name != "alembic_version"
        }


def test_frozen_0033_input_survives_forward_native_and_hard_only_upgrade(
    database: Database, native_cp_test_target_guard: NativeCPTestGuard, record_property
) -> None:
    if native_cp_test_target_guard.relation_count == 0:
        _upgrade(database, LEGACY_HEAD)
    else:
        with database.engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == LEGACY_HEAD
            )
        pristine_columns = MetaData()
        pristine_columns.reflect(database.engine)
        assert not any(_facts(database, pristine_columns).values())
    values = _seed(database)
    original_columns = MetaData()
    original_columns.reflect(database.engine)
    before = _facts(database, original_columns)
    record_property("seeded_legacy_fixture", "true")
    record_property("legacy_execution_replay", "NOT_RUN")
    record_property("original_0033_facts", _json(before))
    assert len(before["tasks"]) == 3
    assert len(before["task_plan_snapshots"]) == 2
    assert len(before["worker_items"]) == 2
    assert len(before["execution_sessions"]) == 1
    assert len(before["task_acceptance_decisions"]) == 1
    assert len(before["context_package_knowledge_refs"]) == 1
    for revision in (NATIVE_HEAD, CURRENT_HEAD):
        _upgrade(database, revision)
        assert _facts(database, original_columns) == before
        print(
            _json(
                {
                    "checkpoint": revision,
                    "seeded_legacy_fixture": True,
                    "legacy_execution_replay": "NOT_RUN",
                    "original_facts_hash": _hash(before),
                    "populated_tables": {
                        name: {"count": len(rows), "hash": _hash(rows)}
                        for name, rows in before.items()
                        if rows
                    },
                }
            )
        )
    with SqlAlchemyUnitOfWork(database) as uow:
        assert uow.plans.get(values["plan"].id, WORKSPACE_ID) == values["plan"]
        assert (
            uow.input_selections.get(WORKSPACE_ID, values["selection"].id)
            == values["selection"]
        )
        for kind in ("candidate", "verification", "acceptance"):
            assert (
                uow.results.get_record(kind, values[kind].id, WORKSPACE_ID)
                == values[kind]
            )
        assert (
            uow.results.get_record("code_snapshot", values["source"].id, WORKSPACE_ID)
            == values["source"]
        )
        profile = uow.planning_profiles.get(
            _id("planning-profile"), WORKSPACE_ID, version=1
        )
        assert profile.execution_mode == "legacy" and profile.planner_agent_ref is None
        request = uow.planning_requests.get(_id("planning-request"), WORKSPACE_ID)
        assert request.profile_snapshot.execution_mode == "legacy"
        assert request.profile_snapshot.planner_agent_ref is None
        assert request.briefs == (values["brief"],)
        assert request.runs[0].usage == values["usage"]
        assert (
            uow.session.scalar(
                select(func.count()).select_from(AgentExecutionLinkModel)
            )
            == 0
        )
    with TestClient(
        create_app(
            Settings(
                database_url=str(database.engine.url), result_integration_enabled=True
            )
        )
    ) as client:
        params = {"workspace_id": str(WORKSPACE_ID)}
        root = f"/api/v1/tasks/{_id('root-task')}"
        task = client.get(root, params=params)
        assert task.status_code == 200, task.text
        assert task.json()["status"] == "succeeded"
        generations = client.get(f"{root}/plan/generations", params=params)
        assert generations.status_code == 200, generations.text
        assert {item["id"] for item in generations.json()} == {
            str(plan.id) for plan in values["plans"]
        }
        timeline = client.get(f"{root}/timeline", params=params)
        assert timeline.status_code == 200, timeline.text
        assert str(_id("acceptance-event")) in _json(timeline.json())
        result_root = (
            f"/api/v1/workspaces/{WORKSPACE_ID}/tasks/{_id('root-task')}/results"
        )
        overview = client.get(result_root)
        assert overview.status_code == 200, overview.text
        assert overview.json()["result_stage"] == "completed"
        assert str(values["acceptance"].id) in _json(overview.json())
        detail = client.get(f"{result_root}/{values['candidate'].id}")
        assert detail.status_code == 200, detail.text
        assert values["candidate"].content_hash in _json(detail.json())
        assert str(values["verification"].id) in _json(detail.json())
        package = client.get(
            f"/api/v1/workspaces/{WORKSPACE_ID}/context-packages/{values['manifest'].package_id}"
        )
        assert package.status_code == 200, package.text
        assert values["manifest"].package_hash in _json(package.json())
        assert values["manifest"].knowledge_refs[0].revision_hash in _json(
            package.json()
        )
    assert _facts(database, original_columns) == before
