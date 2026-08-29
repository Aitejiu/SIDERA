import type { components } from "@/shared/api/schema";

export type TaskGraph = components["schemas"]["TaskConsoleGraphProjectionResponse"];
export type SubtaskNode = components["schemas"]["SubtaskNodeProjectionResponse"];
export type TaskEdge = components["schemas"]["SubtaskEdgeProjectionResponse"];
export type Milestone = components["schemas"]["SubtaskMilestoneProjectionResponse"];
export type TaskDetail = components["schemas"]["TaskDetailProjectionResponse"];
export type TaskActivity = components["schemas"]["TaskConsoleActivityProjectionResponse"];
export type ActivityItem = components["schemas"]["TaskConsoleActivityItemProjectionResponse"];
export type TaskView = components["schemas"]["TaskResponse"];
export type PlanSnapshot = components["schemas"]["TaskPlanSnapshotResponse"];
export type PlanDiff = components["schemas"]["PlanDiffResponse"];
export type PlanConvergence = components["schemas"]["PlanConvergenceProjectionResponse"];
