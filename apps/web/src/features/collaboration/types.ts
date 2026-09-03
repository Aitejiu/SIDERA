import type { components } from "@/shared/api/schema";

export type RootCollaborationSummary =
  components["schemas"]["RootCollaborationSummaryResponse"];
export type CollaborationDetail =
  components["schemas"]["CollaborationDetailResponse"];
export type CollaborationDecision =
  components["schemas"]["CollaborationDecisionResponse"];
export type CollaborationDecisionPage =
  components["schemas"]["CollaborationDecisionPageResponse"];
export type CollaborationStatus = RootCollaborationSummary["status"];
export type WorkerCollaborationSummary =
  components["schemas"]["WorkerItemCollaborationSummaryResponse"];
export type WorkerCollaborationState = WorkerCollaborationSummary["state"];
export type GovernanceAction = WorkerCollaborationSummary["available_actions"][number];
export type GovernanceMode = WorkerCollaborationSummary["governance_mode"];
export type CollaborationReleaseState = WorkerCollaborationSummary["release_state"];
export type ActiveCollaborationProposal = NonNullable<WorkerCollaborationSummary["active_proposal"]>;

export const workerCollaborationCopy: Record<WorkerCollaborationState, string> = {
  unmanaged: "按现有流程调度",
  waiting_dependency: "等待上游结果",
  waiting_artifact: "等待必要产物",
  waiting_evidence: "等待必要证据",
  waiting_release: "等待协同放行",
  released: "协同已放行，等待调度",
  materialized: "已进入执行",
};

export function workerCollaborationLabel(state: string | null | undefined): string | null {
  if (state === null || state === undefined) {
    return null;
  }
  return workerCollaborationCopy[state as WorkerCollaborationState] ?? null;
}

export function collaborationStrategyLabel(strategy: string): string {
  if (strategy === "all_required") {
    return "全部输入就绪后放行";
  }
  return strategy.replaceAll("_", " ");
}
