import type { PlanningProposal } from "./types";

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

export interface ProposalViewModel {
  rootTask: ProposalRootTask;
  subtasks: ProposalSubtask[];
  workerItems: ProposalWorkerItem[];
  subtaskDependencies: ProposalSubtaskDependency[];
  workerItemDependencies: ProposalWorkerDependency[];
  decisionSummary: ProposalDecisionSummary;
  impactPolicy: ProposalImpactPolicy;
  diff: ProposalDiffSummary;
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
    workerItemDependencies: recordList(display.worker_item_dependencies).map((item) => ({
      upstreamWorkerItemKey: stringValue(item.upstream_worker_item_key, "unknown"),
      downstreamWorkerItemKey: stringValue(item.downstream_worker_item_key, "unknown"),
      kind: stringValue(item.kind, "hard"),
      requiredArtifactRefs: stringList(item.required_artifact_refs),
    })),
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
  };
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
