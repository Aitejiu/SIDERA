import type { components } from "@/shared/api/schema";

export type WorkerWorkspace = components["schemas"]["WorkerWorkspaceProjectionResponse"];
export type WorkerActivity = components["schemas"]["WorkerActivityProjectionResponse"];
export type WorkerActivityPage = components["schemas"]["WorkerItemActivityPageResponse"];
export type WorkerRuntime = components["schemas"]["WorkerRuntimeProjectionResponse"];
export type WorkerExecution = components["schemas"]["WorkerExecutionSummaryProjectionResponse"];
export type WorkerPlacement = components["schemas"]["WorkerItemPlacementProjectionResponse"];
export type WorkerTransition = components["schemas"]["WorkerTransitionResponse"];
export type RecoverWorkerTransitionPayload = components["schemas"]["RecoverWorkerTransitionPayload"];
export type CommandRequest = components["schemas"]["CommandRequest"];
export type CommandResponse = components["schemas"]["CommandResponse"];
