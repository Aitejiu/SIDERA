import type { components } from "@/shared/api/schema";

export type TaskGraph = components["schemas"]["TaskGraphProjectionResponse"];
export type TaskNode = components["schemas"]["TaskNodeProjectionResponse"];
export type TaskEdge = components["schemas"]["TaskEdgeProjectionResponse"];
export type Milestone = components["schemas"]["MilestoneProjectionResponse"];
export type TaskDetail = components["schemas"]["TaskDetailProjectionResponse"];
export type TaskActivity = components["schemas"]["TaskActivityProjectionResponse"];
export type ActivityItem = components["schemas"]["ActivityItemProjectionResponse"];
export type TaskView = components["schemas"]["TaskResponse"];
export type PlanSnapshot = components["schemas"]["TaskPlanSnapshotResponse"];
export type PlanDiff = components["schemas"]["PlanDiffResponse"];
