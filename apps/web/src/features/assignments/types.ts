import type { ProfileRef, ProfileView } from "@/features/agents/types";
import type { components } from "@/shared/api/schema";

export type AgentBindingView = components["schemas"]["AgentBindingView"];
export type AgentExecutionView = components["schemas"]["AgentExecutionView"];
export type PlanningAssigneeChoice = components["schemas"]["PlanningAssigneeChoice"];
export type CommandRequest = components["schemas"]["CommandRequest"];
export type CommandResponse = components["schemas"]["CommandResponse"];
export type CommandStatusResponse = components["schemas"]["CommandStatusResponse"];
export type NativeWorkerContextView = components["schemas"]["NativeWorkerContextView"];
export type NativeWorkerTraceView = components["schemas"]["NativeWorkerTraceView"];
export type RevisionDiff = components["schemas"]["RevisionDiffResponse"];

export type { ProfileRef, ProfileView };

export type AssigneeValue =
  | { kind: "human"; id: string; profileRef: null }
  | { kind: "profile"; id: string; profileRef: ProfileRef };

export interface ProposalAssignmentTarget {
  kind: "subtask" | "worker_item";
  key: string;
  label: string;
  subtaskKey: string | null;
  strategy: "create" | "derive" | "reuse";
  existingId: string | null;
}

export class AssignmentUnavailableError extends Error {
  readonly code = "assignment_unavailable";

  constructor() {
    super("Assignment data is unavailable.");
    this.name = "AssignmentUnavailableError";
  }
}
