import type { PlanningInputGroup, PlanningProposal, RevisionDiff } from "./types";

export interface ProposalRootTask {
  title: string;
  goalSummary: string;
  acceptanceRefs: string[];
}

export interface ProposalSubtask {
  key: string;
  kind: string;
  title: string;
  goalSummary: string;
  acceptanceRefs: string[];
  expectedOutputs: string[];
  milestoneKey: string | null;
  sourceRefs: string[];
}

export interface ProposalWorkerItem {
  key: string;
  subtaskKey: string;
  title: string;
  role: string;
  responsibility: string;
  capabilityRequirements: string[];
  workerPolicyRef: string | null;
  configurationRef: string | null;
  concurrencyLimit: number | null;
  sourceRefs: string[];
  pinnedWorkerId: string | null;
  readiness: "ready" | "blocked" | "unknown";
}

export interface ProposalSubtaskDependency {
  upstreamSubtaskKey: string;
  downstreamSubtaskKey: string;
  kind: string;
  requiredEvidenceRefs: string[];
  enforcement: string;
}

export interface ProposalWorkerDependency {
  upstreamWorkerItemKey: string;
  downstreamWorkerItemKey: string;
  kind: string;
  requiredArtifactRefs: string[];
}

export interface ProposalDecisionSummary {
  understanding: string;
  assumptions: string[];
  risks: string[];
  estimatedTokens: number;
  estimatedCostMicrounits: number;
  estimatedRuntimeSeconds: number;
  estimatedConcurrency: number;
  scopeIncludes: string[];
  scopeExcludes: string[];
}

export interface ProposalImpactPolicy {
  activateImmediately: boolean;
  budgetLevel: string;
  failurePolicy: string;
  manualGate: boolean;
  revisionOperation: string | null;
}

export interface ProposalDiffSummary {
  rootTaskAdded: boolean;
  addedSubtaskKeys: string[];
  addedWorkerItemKeys: string[];
  addedSubtaskDependencyEdges: string[][];
  addedWorkerItemDependencyEdges: string[][];
  immediatelyReadyWorkerItemKeys: string[];
  blockedWorkerItemKeys: string[];
}

export interface InputGroupSourceDisplay {
  workerItemKey: string;
  workerItemTitle: string;
  subtaskPath: string[];
  priority: number;
}

export interface InputGroupHardDependencyDisplay {
  upstreamWorkerItemKey: string;
  upstreamWorkerItemTitle: string;
  requiredArtifactRefs: string[];
  requiredEvidenceRefs: string[];
}

export interface InputGroupDisplay {
  groupKey: string;
  title: string;
  consumerWorkerItemKey: string;
  consumerSubtaskPath: string[];
  mode: string;
  minimumCount: number | null;
  requiredCount: number;
  selectionStrategy: string;
  candidates: InputGroupSourceDisplay[];
  requiredArtifactRefs: string[];
  requiredEvidenceRefs: string[];
  hardDependencies: InputGroupHardDependencyDisplay[];
}

export interface ProposalViewModel {
  rootTask: ProposalRootTask;
  subtasks: ProposalSubtask[];
  workerItems: ProposalWorkerItem[];
  subtaskDependencies: ProposalSubtaskDependency[];
  workerItemDependencies: ProposalWorkerDependency[];
  decisionSummary: ProposalDecisionSummary;
  impactPolicy: ProposalImpactPolicy;
  diff: ProposalDiffSummary;
  inputGroups: InputGroupDisplay[];
  displayIntegrationSpec: unknown;
}

export function normalizePlanningProposal(proposal: PlanningProposal): ProposalViewModel {
  const display = record(proposal.display);
  const derivedDiff = record(proposal.derived_diff);
  const root = record(display.root_task);
  const readyKeys = new Set(stringList(derivedDiff.immediately_ready_worker_item_keys));
  const blockedKeys = new Set(stringList(derivedDiff.blocked_worker_item_keys));

  const subtasks = recordList(display.subtasks).map((item, index) => ({
    key: stringValue(item.key, `subtask-${index + 1}`),
    kind: stringValue(item.kind, "implementation"),
    title: stringValue(item.title, `子任务 ${index + 1}`),
    goalSummary: stringValue(item.goal_summary, "等待规划器提供目标说明。"),
    acceptanceRefs: stringList(item.acceptance_refs),
    expectedOutputs: stringList(item.expected_outputs),
    milestoneKey: nullableString(item.milestone_key),
    sourceRefs: stringList(item.source_refs),
  }));
  const workers = recordList(display.worker_items).map((item, index) => {
    const key = stringValue(item.key, `worker-item-${index + 1}`);
    return {
      key,
      subtaskKey: stringValue(item.subtask_key, "unknown-subtask"),
      title: stringValue(item.title, `WorkerItem ${index + 1}`),
      role: stringValue(item.role, "未指定角色"),
      responsibility: stringValue(item.responsibility, "未指定职责。"),
      capabilityRequirements: stringList(item.capability_requirements),
      workerPolicyRef: nullableString(item.worker_policy_ref),
      configurationRef: nullableString(item.configuration_ref),
      concurrencyLimit: numberOrNull(item.concurrency_limit),
      sourceRefs: stringList(item.source_refs),
      pinnedWorkerId: nullableString(item.pinned_worker_id),
      readiness: readyKeys.has(key) ? "ready" : blockedKeys.has(key) ? "blocked" : "unknown",
    } as ProposalWorkerItem;
  });
  const workerByKey = new Map(workers.map((worker) => [worker.key, worker]));
  const subtasksByKey = new Map(subtasks.map((subtask) => [subtask.key, subtask]));
  const workerDependencies = recordList(display.worker_item_dependencies).map((item) => ({
    upstreamWorkerItemKey: stringValue(item.upstream_worker_item_key, "unknown"),
    downstreamWorkerItemKey: stringValue(item.downstream_worker_item_key, "unknown"),
    kind: stringValue(item.kind, "hard"),
    requiredArtifactRefs: stringList(item.required_artifact_refs),
  }));
  const inputGroups = (proposal.collaboration?.input_groups ?? []).map((group) => (
    normalizePlanningInputGroup(group, {
      rootTitle: stringValue(root.title, "根任务"),
      workersByKey: workerByKey,
      subtasksByKey,
      workerDependencies,
    })
  ));

  const summary = record(display.decision_summary);
  const impact = record(display.impact_policy);
  return {
    rootTask: {
      title: stringValue(root.title, "未命名 Root Task"),
      goalSummary: stringValue(root.goal_summary, "规划器没有返回 Root Task 目标说明。"),
      acceptanceRefs: stringList(root.acceptance_refs),
    },
    subtasks,
    workerItems: workers,
    subtaskDependencies: recordList(display.subtask_dependencies).map((item) => ({
      upstreamSubtaskKey: stringValue(item.upstream_subtask_key, "unknown"),
      downstreamSubtaskKey: stringValue(item.downstream_subtask_key, "unknown"),
      kind: stringValue(item.kind, "hard"),
      requiredEvidenceRefs: stringList(item.required_evidence_refs),
      enforcement: stringValue(item.enforcement, "gate"),
    })),
    workerItemDependencies: workerDependencies,
    decisionSummary: {
      understanding: stringValue(summary.understanding, "规划器没有返回理解摘要。"),
      assumptions: stringList(summary.assumptions),
      risks: stringList(summary.risks),
      estimatedTokens: numberValue(summary.estimated_tokens),
      estimatedCostMicrounits: numberValue(summary.estimated_cost_microunits),
      estimatedRuntimeSeconds: numberValue(summary.estimated_runtime_seconds),
      estimatedConcurrency: numberValue(summary.estimated_concurrency, 1),
      scopeIncludes: stringList(summary.scope_includes),
      scopeExcludes: stringList(summary.scope_excludes),
    },
    impactPolicy: {
      activateImmediately: booleanValue(impact.activate_immediately),
      budgetLevel: stringValue(impact.budget_level, "未指定"),
      failurePolicy: stringValue(impact.failure_policy, "未指定"),
      manualGate: booleanValue(impact.manual_gate),
      revisionOperation: nullableString(impact.revision_operation),
    },
    diff: {
      rootTaskAdded: booleanValue(derivedDiff.root_task_added, true),
      addedSubtaskKeys: stringList(derivedDiff.added_subtask_keys, subtasks.map((item) => item.key)),
      addedWorkerItemKeys: stringList(derivedDiff.added_worker_item_keys, workers.map((item) => item.key)),
      addedSubtaskDependencyEdges: stringMatrix(derivedDiff.added_subtask_dependency_edges),
      addedWorkerItemDependencyEdges: stringMatrix(derivedDiff.added_worker_item_dependency_edges),
      immediatelyReadyWorkerItemKeys: stringList(derivedDiff.immediately_ready_worker_item_keys),
      blockedWorkerItemKeys: stringList(derivedDiff.blocked_worker_item_keys),
    },
    inputGroups,
    displayIntegrationSpec: display.integration_spec,
  };
}

export function normalizeRevisionInputGroups(
  changes: RevisionDiff["input_group_changes"] = [],
  workerChanges: RevisionDiff["worker_item_changes"] = [],
  dependencyChanges: RevisionDiff["worker_dependency_changes"] = [],
  rootTitle: string,
): { before: InputGroupDisplay[]; after: InputGroupDisplay[] } {
  const workerPaths = new Map(
    workerChanges.map((change) => [change.target_worker_item_key, change.path]),
  );
  const normalize = (value: unknown, change: RevisionDiff["input_group_changes"][number]) =>
    normalizeRawInputGroup(value, {
      groupKey: change.group_key,
      consumerWorkerItemKey: change.consumer_worker_item_key,
      rootTitle,
      workerPaths,
      dependencyChanges,
    });

  return {
    before: changes.flatMap((change) => {
      const group = normalize(change.before, change);
      return group === null ? [] : [group];
    }),
    after: changes.flatMap((change) => {
      const group = normalize(change.after, change);
      return group === null ? [] : [group];
    }),
  };
}

function normalizePlanningInputGroup(
  group: PlanningInputGroup,
  context: {
    rootTitle: string;
    workersByKey: Map<string, ProposalWorkerItem>;
    subtasksByKey: Map<string, ProposalSubtask>;
    workerDependencies: ProposalWorkerDependency[];
  },
): InputGroupDisplay {
  const consumer = context.workersByKey.get(group.consumer_worker_item_key);
  const consumerSubtask = consumer === undefined
    ? undefined
    : context.subtasksByKey.get(consumer.subtaskKey);
  const candidates = group.candidates.map((candidate) => {
    const worker = context.workersByKey.get(candidate.worker_item_key);
    const subtask = worker === undefined ? undefined : context.subtasksByKey.get(worker.subtaskKey);
    return {
      workerItemKey: candidate.worker_item_key,
      workerItemTitle: worker?.title ?? candidate.worker_item_key,
      subtaskPath: [context.rootTitle, ...(subtask === undefined ? [] : [subtask.title])],
      priority: candidate.priority,
    };
  });
  const hardDependencies = context.workerDependencies
    .filter((dependency) => (
      dependency.downstreamWorkerItemKey === group.consumer_worker_item_key
      && dependency.kind === "hard"
    ))
    .map((dependency) => {
      const worker = context.workersByKey.get(dependency.upstreamWorkerItemKey);
      return {
        upstreamWorkerItemKey: dependency.upstreamWorkerItemKey,
        upstreamWorkerItemTitle: worker?.title ?? dependency.upstreamWorkerItemKey,
        requiredArtifactRefs: dependency.requiredArtifactRefs,
        requiredEvidenceRefs: [],
      };
    });
  return {
    groupKey: group.group_key,
    title: group.title,
    consumerWorkerItemKey: group.consumer_worker_item_key,
    consumerSubtaskPath: [
      context.rootTitle,
      ...(consumerSubtask === undefined ? [] : [consumerSubtask.title]),
    ],
    mode: group.mode,
    minimumCount: group.minimum_count,
    requiredCount: group.mode === "all" ? candidates.length : group.minimum_count ?? candidates.length,
    selectionStrategy: group.selection_strategy,
    candidates,
    requiredArtifactRefs: group.requirements.required_artifact_refs,
    requiredEvidenceRefs: group.requirements.required_evidence_refs,
    hardDependencies,
  };
}

function normalizeRawInputGroup(
  value: unknown,
  context: {
    groupKey: string;
    consumerWorkerItemKey: string;
    rootTitle: string;
    workerPaths: Map<string, string>;
    dependencyChanges: RevisionDiff["worker_dependency_changes"];
  },
): InputGroupDisplay | null {
  const group = record(value);
  const rawCandidates = recordList(group.candidates);
  if (rawCandidates.length === 0) return null;
  const consumerPath = pathForWorker(
    context.workerPaths.get(context.consumerWorkerItemKey),
    context.rootTitle,
    context.consumerWorkerItemKey,
  );
  const candidates = rawCandidates.map((candidate, index) => {
    const key = stringValue(candidate.worker_item_key, `source-${index + 1}`);
    const path = pathForWorker(context.workerPaths.get(key), context.rootTitle, key);
    return {
      workerItemKey: key,
      workerItemTitle: path.at(-1) ?? key,
      subtaskPath: path.length > 1 ? path.slice(0, -1) : path,
      priority: numberValue(candidate.priority, index + 1),
    };
  });
  const requirements = record(group.requirements);
  const mode = stringValue(group.mode, "all");
  return {
    groupKey: stringValue(group.group_key, context.groupKey),
    title: stringValue(group.title, context.groupKey),
    consumerWorkerItemKey: stringValue(
      group.consumer_worker_item_key,
      context.consumerWorkerItemKey,
    ),
    consumerSubtaskPath: consumerPath.length > 1 ? consumerPath.slice(0, -1) : consumerPath,
    mode,
    minimumCount: numberOrNull(group.minimum_count),
    requiredCount: mode === "all"
      ? candidates.length
      : numberOrNull(group.minimum_count) ?? candidates.length,
    selectionStrategy: stringValue(group.selection_strategy, "plan_order"),
    candidates,
    requiredArtifactRefs: stringList(requirements.required_artifact_refs),
    requiredEvidenceRefs: stringList(requirements.required_evidence_refs),
    hardDependencies: context.dependencyChanges
      .filter((dependency) => (
        dependency.downstream_key === context.consumerWorkerItemKey
        && dependency.dependency_kind === "hard"
      ))
      .map((dependency) => ({
        upstreamWorkerItemKey: dependency.upstream_key,
        upstreamWorkerItemTitle: dependency.upstream_path.split(" / ").at(-1) ?? dependency.upstream_key,
        requiredArtifactRefs: [],
        requiredEvidenceRefs: [],
      })),
  };
}

function pathForWorker(path: string | undefined, rootTitle: string, fallback: string): string[] {
  if (path === undefined || path.trim() === "") return [rootTitle, fallback];
  return path.split(" / ").map((part) => part.trim()).filter(Boolean);
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function recordList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() !== "" ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function stringList(value: unknown, fallback: string[] = []): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.trim() !== "")
    : fallback;
}

function stringMatrix(value: unknown): string[][] {
  return Array.isArray(value)
    ? value.map((row) => Array.isArray(row) ? row.filter((item): item is string => typeof item === "string") : [])
    : [];
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function booleanValue(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}
