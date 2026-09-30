from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.application.planner_runtime import (
    PlannerClarificationResult,
    PlannerExplicitFailure,
    PlannerInput,
    PlannerProposalResult,
    PlannerResult,
    PlannerRunControl,
)
from sidera_control_plane.application.revision_planning_context import (
    RevisionPlanningContextV1,
)
from sidera_control_plane.demo.contracts import (
    WORKER_ITEM_CAPABILITIES,
    DemoManifest,
)
from sidera_control_plane.demo.results import closure_check_spec, demo_check_spec
from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyRequestV2,
    resolve_collaboration_policy_v2,
)
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextMaterializationV1,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.plan_revisions import (
    ContextDisposition,
    IdentityStrategy,
    SubtaskIdentityDecision,
    WorkerItemIdentityDecision,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.planning import (
    PlanningClarification,
    PlanningRequestKind,
)
from sidera_control_plane.domain.planning_proposals import (
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    PlanningProfileDescriptor,
    PlanningProposalPayloadV6,
    ProposalRevision,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV6,
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemDependency,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.revision_diff import RevisionDiffNormalizer

DemoPlannerScenario = Literal[
    "mainline",
    "revision",
    "handoff",
    "rework",
    "recovery",
    "knowledge",
    "governance",
    "controls",
    "history",
]


class DemoPlannerAdapter:
    """A deterministic planner used only by an explicitly configured demo run."""

    provider_ref = "interview-demo-planner"
    _CAPABILITIES = PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.NONE,
        max_context_bytes=128_000,
        stability=StabilityLevel.EXPERIMENTAL,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(6,),
        context_package_versions=(1, 2, 3),
    )

    def __init__(self, manifest: DemoManifest) -> None:
        self.manifest = manifest
        self._cancelled_operations: set[UUID] = set()

    @property
    def cancelled_operations(self) -> frozenset[UUID]:
        return frozenset(self._cancelled_operations)

    def probe(self, config: Mapping[str, object]) -> PlannerAdapterCapabilityProfile:
        configured_provider = config.get("planning.provider")
        if configured_provider is not None and configured_provider != self.provider_ref:
            raise ValueError("interview demo planner provider is fixed")
        return self._CAPABILITIES

    def accept_context(
        self,
        materialization: ContextMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextAcknowledgement:
        if not isinstance(operation_id, UUID):
            raise TypeError("interview demo planner operation_id must be a UUID")
        if (
            materialization.schema_version
            not in self._CAPABILITIES.context_package_versions
        ):
            raise ValueError(
                "interview demo planner context materialization is unsupported"
            )
        if materialization.total_byte_count > self._CAPABILITIES.max_context_bytes:
            raise ValueError(
                "interview demo planner context materialization exceeds its limit"
            )
        return ContextAcknowledgement(
            package_id=materialization.metadata.package_id,
            package_hash=materialization.metadata.package_hash,
            consumer_type=materialization.metadata.consumer_type,
            consumer_id=materialization.metadata.consumer_id,
            context_version=materialization.metadata.context_version,
            adapter_module_id=self.manifest.module_ids["planner"],
            adapter_config_version=1,
            acknowledged_at=datetime.now(UTC),
            schema_version=materialization.schema_version,
            operation_id=operation_id,
        )

    def plan(
        self, planner_input: PlannerInput, control: PlannerRunControl
    ) -> PlannerResult:
        if control.operation_id != planner_input.operation_id:
            return _failure(
                "operation_id_mismatch",
                "planner operation id does not match input",
            )
        if (
            control.cancel_requested
            or control.operation_id in self._cancelled_operations
        ):
            return _failure("cancelled", "planner operation was cancelled")
        feedback = planner_input.brief.user_feedback
        if self.manifest.scenario == "controls" and not feedback:
            return PlannerClarificationResult(
                clarifications=(
                    PlanningClarification.create(
                        clarification_id=uuid5(
                            NAMESPACE_URL,
                            f"sidera.interview-demo:{planner_input.operation_id}:clarification",
                        ),
                        planning_request_id=planner_input.planning_request_id,
                        planning_run_id=planner_input.planning_run_id,
                        question="手机号为空时是否允许继续导入？",
                        rationale="主线要求把这一执行策略交给用户确认，而不是由 Planner 猜测。",
                        answer_type="choice",
                        options=("optional", "required"),
                        blocking=True,
                        created_at=control.started_at,
                    ),
                )
            )
        if feedback and _contains_any(feedback, "拒绝", "reject"):
            return _failure(
                "proposal_rejected", "the user rejected the current proposal"
            )
        if feedback and _contains_any(feedback, "取消", "cancel"):
            return _failure("cancelled", "planner operation was cancelled")

        if planner_input.kind is PlanningRequestKind.REVISION:
            context = planner_input.planning_context
            if not isinstance(context, RevisionPlanningContextV1):
                return _failure(
                    "invalid_revision_context",
                    "interview demo revision context is invalid",
                )
            try:
                payload = build_revision_proposal(self.manifest, context)
                base_plan = TaskPlanSnapshot.from_payload(
                    dict(context.plan_snapshot_payload)
                )
                collaboration_policy = resolve_collaboration_policy_v2(
                    payload.collaboration_policy_request,
                    planner_input.profile_snapshot.collaboration_runtime_binding,
                    subtask_keys=frozenset(item.key for item in payload.subtasks),
                )
                derived_diff = RevisionDiffNormalizer().normalize(
                    base_plan,
                    payload,
                    collaboration_policy=collaboration_policy,
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                return _failure("invalid_revision_context", str(exc))
            proposal = ProposalRevision.create(
                planning_request_id=planner_input.planning_request_id,
                revision=1,
                brief_revision=planner_input.brief_revision,
                planning_run_id=planner_input.planning_run_id,
                kind=PlanningRequestKind.REVISION,
                payload=payload,
                profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                    planner_input.profile_snapshot
                ),
                profile_snapshot=planner_input.profile_snapshot,
                created_at=control.started_at,
                derived_diff=derived_diff,
            )
            return PlannerProposalResult(
                proposal=replace(
                    proposal,
                    id=uuid5(
                        self.manifest.run_id,
                        f"proposal:{planner_input.planning_run_id}:revision",
                    ),
                )
            )

        proposal_builder = (
            build_closure_sample_proposal
            if _is_closure_sample_brief(planner_input.brief.objective)
            else build_initial_proposal
        )
        payload = proposal_builder(
            self.manifest,
            user_feedback=feedback,
            context_refs=planner_input.brief.context_refs,
        )
        proposal = ProposalRevision.create(
            planning_request_id=planner_input.planning_request_id,
            revision=1,
            brief_revision=planner_input.brief_revision,
            planning_run_id=planner_input.planning_run_id,
            kind=PlanningRequestKind.CREATE,
            payload=payload,
            profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                planner_input.profile_snapshot
            ),
            profile_snapshot=planner_input.profile_snapshot,
            created_at=control.started_at,
        )
        return PlannerProposalResult(
            proposal=replace(
                proposal,
                id=uuid5(
                    self.manifest.run_id,
                    f"proposal:{planner_input.planning_run_id}:create",
                ),
            )
        )

    def cancel(self, operation_id: UUID) -> None:
        self._cancelled_operations.add(operation_id)


def build_initial_proposal(
    manifest: DemoManifest,
    *,
    user_feedback: str | None = None,
    context_refs: tuple[str, ...] = (),
) -> PlanningProposalPayloadV6:
    """Build the mainline plan without binding concrete Worker identities."""

    workers = (
        ProposedWorkerItem(
            key="validate",
            subtask_key="validation",
            title="CSV 校验 WorkerItem",
            role="validator",
            responsibility="读取 CSV 样例并执行字段、邮箱和手机号规则校验。",
            capability_requirements=(
                "python",
                WORKER_ITEM_CAPABILITIES["validation"],
            ),
            worker_policy_ref="policy:demo",
            configuration_ref="config:demo-validation",
            concurrency_limit=1,
            source_refs=("brief:current",),
        ),
        ProposedWorkerItem(
            key="report",
            subtask_key="reporting",
            title="错误报告 WorkerItem",
            role="reporter",
            responsibility="汇总 CSV 校验结果，生成可供复核的报告。",
            capability_requirements=("python", WORKER_ITEM_CAPABILITIES["report"]),
            worker_policy_ref="policy:demo",
            configuration_ref="config:demo-report",
            concurrency_limit=1,
            source_refs=("brief:current",),
        ),
        ProposedWorkerItem(
            key="review-report",
            subtask_key="reporting",
            title="报告复核 WorkerItem",
            role="reviewer",
            responsibility="读取报告结果，检查报告是否完整且与校验结果一致。",
            capability_requirements=("python", WORKER_ITEM_CAPABILITIES["review"]),
            worker_policy_ref="policy:demo",
            configuration_ref="config:demo-review",
            concurrency_limit=1,
            source_refs=("brief:current",),
        ),
        ProposedWorkerItem(
            key="integrate",
            subtask_key="integration",
            title="集成验证 WorkerItem",
            role="integrator",
            responsibility="汇总前置结果，在样例 Git 工作区运行最终验证并生成候选。",
            capability_requirements=(
                "python",
                WORKER_ITEM_CAPABILITIES["integration"],
            ),
            worker_policy_ref="policy:demo",
            configuration_ref="config:demo-integration",
            concurrency_limit=1,
            source_refs=("brief:current",),
        ),
    )

    worker_keys = tuple(item.key for item in workers)
    return PlanningProposalPayloadV6(
        schema_version=6,
        kind=PlanningRequestKind.CREATE,
        root_task=ProposedRootTask(
            title="CSV 批量导入",
            goal_summary="在保留手机号策略决策点的前提下，完成 CSV 导入、报告、复核和集成验证。",
            acceptance_refs=("acceptance:csv-import",),
        ),
        subtasks=(
            ProposedSubtask(
                key="validation",
                kind=WorkItemKind.CHILD,
                title="CSV 数据校验",
                goal_summary="读取样例数据并确认字段校验结果。",
                acceptance_refs=("acceptance:validation",),
                expected_outputs=("artifact_type:report",),
                milestone_key=None,
                source_refs=("brief:current",),
            ),
            ProposedSubtask(
                key="reporting",
                kind=WorkItemKind.CHILD,
                title="报告与复核",
                goal_summary="生成错误报告并由独立 WorkerItem 复核。",
                acceptance_refs=("acceptance:report",),
                expected_outputs=("artifact_type:report",),
                milestone_key=None,
                source_refs=("brief:current",),
            ),
            ProposedSubtask(
                key="integration",
                kind=WorkItemKind.INTEGRATION,
                title="集成验证",
                goal_summary="把前置结果带入样例 Git 工作区并运行真实测试。",
                acceptance_refs=("acceptance:integration",),
                expected_outputs=("artifact_type:diff",),
                milestone_key=None,
                source_refs=("brief:current",),
            ),
        ),
        worker_items=workers,
        subtask_milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="validation",
                downstream_subtask_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=("acceptance:validation",),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
            SubtaskDependency(
                upstream_subtask_key="reporting",
                downstream_subtask_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=("acceptance:report",),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(
            WorkerItemDependency(
                upstream_worker_item_key="report",
                downstream_worker_item_key="review-report",
                kind=DependencyKind.HARD.value,
                required_artifact_refs=("artifact_type:report",),
            ),
        ),
        execution_intents=tuple(
            _intent(
                item.key,
                item.responsibility,
                item.capability_requirements,
                context_refs=context_refs,
            )
            for item in workers
        ),
        decision_summary=DecisionSummary(
            understanding="用三层任务结构组织 CSV 导入主线，并让报告复核作为同一子任务中的顺序 WorkerItem。",
            assumptions=(
                "手机号策略在 validate WorkerItem 进入等待点后由用户确认。",
                "集成阶段只使用本地样例 Git 仓库，不更新用户分支。",
            ),
            risks=("用户未确认手机号策略时，校验结果不会被伪造为完成。",),
            estimated_cost_microunits=1,
            estimated_runtime_seconds=300,
            estimated_concurrency=2,
            scope_includes=("CSV 读取", "字段校验", "错误报告", "集成验证"),
            scope_excludes=("生产部署", "远程 Git 推送"),
            metadata={
                "planner": "interview-demo",
                "simulation": True,
                "user_feedback": user_feedback,
            },
            estimated_tokens=500,
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="demo",
            failure_policy="retry_then_block",
        ),
        collaboration_policy_request=_collaboration_request(),
        integration_spec=_integration_spec(manifest, worker_keys),
    )


def build_closure_sample_proposal(
    manifest: DemoManifest,
    *,
    user_feedback: str | None = None,
    context_refs: tuple[str, ...] = (),
) -> PlanningProposalPayloadV6:
    """Build the fixed closure sample graph used by provider-free acceptance."""

    source_a = ProposedWorkerItem(
        key="source-a",
        subtask_key="implementation",
        title="Source A WorkerItem",
        role="source-builder",
        responsibility="只生成 out/source-a.json，不修改其他成果或 README。",
        capability_requirements=("python", WORKER_ITEM_CAPABILITIES["validation"]),
        worker_policy_ref="policy:closure-sample",
        configuration_ref="config:closure-source-a",
        concurrency_limit=1,
        source_refs=("brief:closure-sample-v1",),
    )
    source_b = ProposedWorkerItem(
        key="source-b",
        subtask_key="implementation",
        title="Source B WorkerItem",
        role="source-builder",
        responsibility="只生成 out/source-b.json，不修改其他成果或 README。",
        capability_requirements=("python", WORKER_ITEM_CAPABILITIES["report"]),
        worker_policy_ref="policy:closure-sample",
        configuration_ref="config:closure-source-b",
        concurrency_limit=1,
        source_refs=("brief:closure-sample-v1",),
    )
    integration = ProposedWorkerItem(
        key="integration",
        subtask_key="integration",
        title="Closure Integration WorkerItem",
        role="integrator",
        responsibility="读取控制面选定的 source-a/source-b 成果并生成 out/summary.json。",
        capability_requirements=("python", WORKER_ITEM_CAPABILITIES["integration"]),
        worker_policy_ref="policy:closure-sample",
        configuration_ref="config:closure-integration",
        concurrency_limit=1,
        source_refs=("brief:closure-sample-v1",),
    )
    workers = (source_a, source_b, integration)
    return replace(
        build_initial_proposal(
            manifest,
            user_feedback=user_feedback,
            context_refs=context_refs,
        ),
        root_task=ProposedRootTask(
            title="离线任务状态汇总",
            goal_summary="并行生成两个固定源成果，再由 integration 汇总为可验收的状态摘要。",
            acceptance_refs=("acceptance:closure-sample",),
        ),
        subtasks=(
            ProposedSubtask(
                key="implementation",
                kind=WorkItemKind.CHILD,
                title="并行生成 Source A/B",
                goal_summary="并行生成两个相互独立的源成果文件。",
                acceptance_refs=(
                    "acceptance:closure-source-a",
                    "acceptance:closure-source-b",
                ),
                expected_outputs=("out/source-a.json", "out/source-b.json"),
                milestone_key=None,
                source_refs=("brief:closure-sample-v1",),
            ),
            ProposedSubtask(
                key="integration",
                kind=WorkItemKind.INTEGRATION,
                title="汇总闭环样例",
                goal_summary="读取两个控制面选定的源成果并生成 out/summary.json。",
                acceptance_refs=("acceptance:closure-integration",),
                expected_outputs=("out/summary.json",),
                milestone_key=None,
                source_refs=("brief:closure-sample-v1",),
            ),
        ),
        worker_items=workers,
        subtask_milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="implementation",
                downstream_subtask_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(),
        execution_intents=(
            _closure_intent(
                source_a,
                expected_artifact="artifact_type:external",
                evidence_ref="acceptance:closure-source-a",
            ),
            _closure_intent(
                source_b,
                expected_artifact="artifact_type:external",
                evidence_ref="acceptance:closure-source-b",
            ),
            _closure_intent(
                integration,
                expected_artifact="artifact_type:report",
                evidence_ref="acceptance:closure-integration",
            ),
        ),
        decision_summary=DecisionSummary(
            understanding="按 closure-sample-v1 固定需求先并行生成 source-a/source-b，再由 integration 汇总。",
            assumptions=(
                "两个 source WorkerItem 只写各自成果文件。",
                "验收 checker 由控制面提供，不放入 Worker 仓库。",
            ),
            risks=("任一 source 成果缺失时，integration 不得启动。",),
            estimated_cost_microunits=1,
            estimated_runtime_seconds=180,
            estimated_concurrency=2,
            scope_includes=("source-a.json", "source-b.json", "summary.json"),
            scope_excludes=("README 修改", "联网", "依赖安装"),
            metadata={
                "planner": "interview-demo",
                "simulation": True,
                "sample_version": "closure-sample-v1",
                "checker_ref": "external:sidera/scripts/check-closure-sample.py",
                "user_feedback": user_feedback,
            },
            estimated_tokens=300,
        ),
        collaboration_policy_request=_closure_collaboration_request(),
        integration_spec=_integration_spec(
            manifest,
            tuple(item.key for item in workers),
            integration_worker_item_key="integration",
            input_group_keys=("closure-inputs",),
        ),
    )


def build_revision_proposal(
    manifest: DemoManifest,
    context: RevisionPlanningContextV1,
) -> PlanningRevisionProposalPayloadV6:
    """Add TSV support while preserving the current plan's stable identities."""

    if context.base_descriptor.workspace_id != manifest.workspace_id:
        raise ValueError("revision context workspace does not match the demo manifest")
    base_plan = TaskPlanSnapshot.from_payload(dict(context.plan_snapshot_payload))
    if base_plan.id != context.base_descriptor.plan_snapshot_id:
        raise ValueError("revision context snapshot does not match its base")
    if plan_snapshot_content_hash(base_plan) != context.base_descriptor.plan_content_hash:
        raise ValueError("revision context snapshot hash does not match its base")
    if base_plan.root_task_id != context.base_descriptor.root_task_id:
        raise ValueError("revision context root task does not match its base")
    if base_plan.generation != context.base_descriptor.plan_generation:
        raise ValueError("revision context generation does not match its base")

    base_subtasks = tuple(_proposed_subtask(item) for item in base_plan.subtasks)
    base_workers = tuple(_proposed_worker(item) for item in base_plan.worker_item_plans)
    if not any(item.key == "integration" for item in base_subtasks):
        raise ValueError("revision base is missing the integration subtask")
    if not any(item.key == "integrate" for item in base_workers):
        raise ValueError("revision base is missing the integration WorkerItem")

    parse_subtask = ProposedSubtask(
        key="parse-tsv",
        kind=WorkItemKind.CHILD,
        title="TSV 数据解析",
        goal_summary="读取 TSV 文件并复用现有字段校验与报告合同。",
        acceptance_refs=("acceptance:parse-tsv",),
        expected_outputs=("artifact_type:report",),
        milestone_key=None,
        source_refs=("brief:current", "revision:tsv"),
    )
    parse_worker = ProposedWorkerItem(
        key="parse-tsv",
        subtask_key="parse-tsv",
        title="TSV 解析 WorkerItem",
        role="parser",
        responsibility="读取 TSV 分隔格式，并交付与 CSV 相同的行校验输入。",
        capability_requirements=(
            "python",
            WORKER_ITEM_CAPABILITIES["replacement"],
        ),
        worker_policy_ref="policy:demo",
        configuration_ref="config:demo-tsv",
        concurrency_limit=1,
        source_refs=("brief:current", "revision:tsv"),
    )
    subtasks = (*base_subtasks, parse_subtask)
    workers = (*base_workers, parse_worker)
    subtask_dependencies = (
        *base_plan.subtask_dependencies,
        SubtaskDependency(
            upstream_subtask_key="parse-tsv",
            downstream_subtask_key="integration",
            kind=DependencyKind.HARD,
            required_evidence_refs=("acceptance:parse-tsv",),
            enforcement=DependencyEnforcement.CONTROL_PLANE,
        ),
    )
    worker_item_dependencies = tuple(base_plan.worker_item_dependencies)
    worker_keys = tuple(item.key for item in workers)
    base_subtask_by_key = {item.key: item for item in base_plan.subtasks}
    base_worker_by_key = {
        item.key: item for item in base_plan.worker_item_plans
    }
    intents = tuple(
        _intent(
            item.key,
            item.responsibility,
            item.capability_requirements,
        )
        for item in workers
    )
    subtask_identity_decisions = tuple(
        SubtaskIdentityDecision(
            target_subtask_key=item.key,
            strategy=(
                IdentityStrategy.REUSE
                if (
                    item.key in base_subtask_by_key
                    and base_subtask_by_key[item.key].task_id is not None
                )
                else IdentityStrategy.CREATE
            ),
            existing_subtask_task_id=(
                base_subtask_by_key[item.key].task_id
                if item.key in base_subtask_by_key
                else None
            ),
            derived_from_subtask_task_ids=(),
            reason=(
                "preserve the existing subtask identity"
                if item.key in base_subtask_by_key
                else "create the TSV parsing branch"
            ),
        )
        for item in subtasks
    )
    worker_item_identity_decisions = tuple(
        WorkerItemIdentityDecision(
            target_worker_item_key=item.key,
            strategy=(
                IdentityStrategy.REUSE
                if (
                    item.key in base_worker_by_key
                    and base_worker_by_key[item.key].worker_item_id is not None
                )
                else IdentityStrategy.CREATE
            ),
            existing_worker_item_id=(
                base_worker_by_key[item.key].worker_item_id
                if item.key in base_worker_by_key
                else None
            ),
            derived_from_worker_item_ids=(),
            target_subtask_key=item.subtask_key,
            context_disposition=(
                ContextDisposition.PATCH_REQUIRED
                if item.key == "integrate"
                and item.key in base_worker_by_key
                and base_worker_by_key[item.key].worker_item_id is not None
                else ContextDisposition.UNCHANGED
            ),
            change_reasons=(
                ("TSV integration input added",)
                if item.key == "integrate"
                and item.key in base_worker_by_key
                and base_worker_by_key[item.key].worker_item_id is not None
                else ()
            ),
        )
        for item in workers
    )
    base_acceptance = tuple(base_plan.acceptance_refs)
    return PlanningRevisionProposalPayloadV6(
        schema_version=6,
        kind=PlanningRequestKind.REVISION,
        base_descriptor=context.base_descriptor,
        root_task=ProposedRootTask(
            title=base_plan.title,
            goal_summary=base_plan.goal_summary,
            acceptance_refs=base_acceptance,
        ),
        subtasks=subtasks,
        worker_items=workers,
        subtask_milestones=tuple(
            SubtaskMilestone(
                key=item.key,
                title=item.title,
                subtask_keys=item.subtask_keys,
                acceptance_refs=item.acceptance_refs,
                order_hint=item.order_hint,
            )
            for item in base_plan.subtask_milestones
        ),
        subtask_dependencies=subtask_dependencies,
        worker_item_dependencies=worker_item_dependencies,
        execution_intents=intents,
        subtask_identity_decisions=subtask_identity_decisions,
        worker_item_identity_decisions=worker_item_identity_decisions,
        retired_subtask_ids=(),
        retired_worker_item_ids=(),
        decision_summary=DecisionSummary(
            understanding="在当前 CSV 计划上增加 TSV 解析分支，报告链和手机号校验合同保持不变。",
            assumptions=(
                "已完成的 report 与 review-report 结果继续作为当前代的事实。",
                "TSV 与 CSV 使用相同的行校验和报告结构。",
            ),
            risks=("集成 WorkerItem 需要接收新增 TSV 结果输入。",),
            estimated_cost_microunits=1,
            estimated_runtime_seconds=300,
            estimated_concurrency=2,
            scope_includes=("TSV 解析", "CSV/TSV 集成验证"),
            scope_excludes=("生产部署", "远程 Git 推送"),
            metadata={
                "planner": "interview-demo",
                "simulation": True,
                "preserved_contracts": ("phone_optional", "reporting"),
                "reused_worker_item_keys": tuple(
                    item.key
                    for item in workers
                    if item.key in base_worker_by_key
                    and base_worker_by_key[item.key].worker_item_id is not None
                ),
                "context_reference": "revision-context",
            },
            estimated_tokens=500,
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="demo",
            failure_policy="retry_then_block",
            revision_operation="revise",
        ),
        collaboration_policy_request=_collaboration_request(include_tsv=True),
        integration_spec=_integration_spec(manifest, worker_keys),
    )


def _proposed_subtask(item: SubtaskPlanItem) -> ProposedSubtask:
    return ProposedSubtask(
        key=item.key,
        kind=item.kind,
        title=item.title,
        goal_summary=item.goal_summary,
        acceptance_refs=item.acceptance_refs,
        expected_outputs=item.expected_outputs,
        milestone_key=item.milestone_key,
        source_refs=item.source_refs,
    )


def _proposed_worker(item: WorkerItemPlanItem) -> ProposedWorkerItem:
    return ProposedWorkerItem(
        key=item.key,
        subtask_key=item.subtask_key,
        title=item.title,
        role=item.role,
        responsibility=item.responsibility,
        capability_requirements=item.capability_requirements,
        worker_policy_ref=item.worker_policy_ref,
        configuration_ref=item.configuration_ref,
        concurrency_limit=item.concurrency_limit,
        source_refs=item.source_refs,
    )


def _intent(
    worker_item_key: str,
    purpose: str,
    capabilities: tuple[str, ...],
    *,
    context_refs: tuple[str, ...] = (),
) -> ExecutionIntent:
    artifact_type = "diff" if worker_item_key == "integrate" else "report"
    acceptance = {
        "validate": "acceptance:validation",
        "report": "acceptance:report",
        "review-report": "acceptance:report",
        "integrate": "acceptance:integration",
        "parse-tsv": "acceptance:parse-tsv",
    }[worker_item_key]
    return ExecutionIntent(
        worker_item_key=worker_item_key,
        purpose=purpose,
        context_refs=context_refs or ("context:brief",),
        expected_artifact_refs=(f"artifact_type:{artifact_type}",),
        required_evidence_refs=(acceptance,),
        capability_requirements=capabilities,
        adapter_preferences=("interview-demo-worker",),
        budget_class="demo",
        deadline_seconds=300,
        not_before_seconds=None,
        max_attempts=1,
        activation_dependencies=(),
    )


def _closure_intent(
    worker: ProposedWorkerItem,
    *,
    expected_artifact: str,
    evidence_ref: str,
) -> ExecutionIntent:
    return ExecutionIntent(
        worker_item_key=worker.key,
        purpose=worker.responsibility,
        context_refs=("context:closure-sample-v1",),
        expected_artifact_refs=(expected_artifact,),
        required_evidence_refs=(evidence_ref,),
        capability_requirements=worker.capability_requirements,
        adapter_preferences=("interview-demo-worker",),
        budget_class="demo",
        deadline_seconds=180,
        not_before_seconds=None,
        max_attempts=1,
        activation_dependencies=(),
    )


def _collaboration_request(*, include_tsv: bool = False) -> CollaborationPolicyRequestV2:
    integration_candidates = [
        {"worker_item_key": "validate", "priority": 1},
        {"worker_item_key": "report", "priority": 2},
    ]
    if include_tsv:
        integration_candidates.append({"worker_item_key": "parse-tsv", "priority": 3})
    return CollaborationPolicyRequestV2.from_mapping(
        {
            "schema_version": 2,
            "mode": "enabled",
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": [],
            "input_groups": [
                {
                    "group_key": "review-report-input",
                    "title": "报告复核输入",
                    "consumer_worker_item_key": "review-report",
                    "candidates": [
                        {"worker_item_key": "report", "priority": 1},
                    ],
                    "requirements": {
                        "execution_status": "succeeded",
                        "required_artifact_refs": ["artifact_type:report"],
                        "required_evidence_refs": ["acceptance:report"],
                        "evidence_status": "candidate",
                    },
                    "mode": "at_least",
                    "minimum_count": 1,
                    "selection_strategy": "plan_order",
                },
                {
                    "group_key": "integration-inputs",
                    "title": "集成前置结果",
                    "consumer_worker_item_key": "integrate",
                    "candidates": integration_candidates,
                    "requirements": {
                        "execution_status": "succeeded",
                        "required_artifact_refs": ["artifact_type:report"],
                        "required_evidence_refs": [],
                        "evidence_status": "candidate",
                    },
                    "mode": "at_least",
                    "minimum_count": 3 if include_tsv else 2,
                    "selection_strategy": "plan_order",
                },
            ],
        }
    )


def _closure_collaboration_request() -> CollaborationPolicyRequestV2:
    return CollaborationPolicyRequestV2.from_mapping(
        {
            "schema_version": 2,
            "mode": "enabled",
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": [],
            "input_groups": [
                {
                    "group_key": "closure-inputs",
                    "title": "Closure source inputs",
                    "consumer_worker_item_key": "integration",
                    "candidates": [
                        {"worker_item_key": "source-a", "priority": 1},
                        {"worker_item_key": "source-b", "priority": 2},
                    ],
                    "requirements": {
                        "execution_status": "succeeded",
                        "required_artifact_refs": [
                            "artifact_type:external",
                        ],
                        "required_evidence_refs": [],
                        "evidence_status": "candidate",
                    },
                    "mode": "at_least",
                    "minimum_count": 2,
                    "selection_strategy": "plan_order",
                }
            ],
        }
    )


def _integration_spec(
    manifest: DemoManifest,
    worker_keys: tuple[str, ...],
    *,
    integration_worker_item_key: str = "integrate",
    input_group_keys: tuple[str, ...] = ("integration-inputs",),
) -> IntegrationSpecV1:
    is_closure_sample = integration_worker_item_key == "integration"
    check_key = "closure" if is_closure_sample else "unit"
    return IntegrationSpecV1.from_mapping(
        {
            "schema_version": 1,
            "repository_ref": opaque_repository_ref(manifest.repository),
            "base_commit": manifest.base_commit,
            "integration_worker_item_key": integration_worker_item_key,
            "input_group_keys": list(input_group_keys),
            "criteria": [
                {
                    "key": (
                        "closure-sample-ready"
                        if is_closure_sample
                        else "csv-import-ready"
                    ),
                    "text": (
                        "closure-sample 输出通过控制面提供的固定 checker。"
                        if is_closure_sample
                        else "CSV 导入实现通过样例测试并具备可验收的结果候选。"
                    ),
                    "mode": "automatic",
                    "check_keys": [check_key],
                }
            ],
            "checks": [
                (closure_check_spec() if is_closure_sample else demo_check_spec()).to_mapping()
            ],
            "verification_binding": {
                "adapter_module_ref": str(manifest.module_ids["verification"]),
                "adapter_config_version": 1,
                "workspace_module_ref": str(manifest.module_ids["workspace"]),
                "workspace_config_version": 1,
            },
            "rework_policy": {
                "max_auto_rework_rounds": 2,
                "max_auto_rework_seconds": 1800,
                "allowed_worker_item_keys": list(worker_keys),
            },
            "apply_policy": {
                "apply_on_accept": False,
                "mode": "fast_forward_only",
                "target_ref": None,
            },
            "finalization_policy": "stop_unused",
        }
    )


def _is_closure_sample_brief(objective: str) -> bool:
    normalized = objective.casefold()
    return all(
        marker in normalized
        for marker in ("closure-sample-v1", "source-a", "source-b", "integration")
    )


def _failure(code: str, safe_message: str) -> PlannerExplicitFailure:
    return PlannerExplicitFailure(
        code=code,
        safe_message=safe_message,
        retryable=False,
        unknown_outcome=False,
        provider_ref=DemoPlannerAdapter.provider_ref,
    )


def _contains_any(value: str, *needles: str) -> bool:
    lowered = value.casefold()
    return any(needle.casefold() in lowered for needle in needles)
