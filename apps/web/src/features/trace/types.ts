import type { components } from "@/shared/api/schema";

export type WorkerTrace = components["schemas"]["WorkerTraceResponse"];
export type WorkerTraceEvent = components["schemas"]["WorkerTraceEventResponse"];
export type TraceTiming = components["schemas"]["TraceTimingResponse"];
export type ExecutionSession = components["schemas"]["ExecutionSessionResponse"];
export type Execution = components["schemas"]["ExecutionResponse"];
export type ExecutionInput = components["schemas"]["ExecutionInputStatusResponse"];
export type ArtifactDescriptor = components["schemas"]["ArtifactContentDescriptorResponse"];
export type CommandResponse = components["schemas"]["CommandResponse"];

export interface ExecutionContextAddress {
  plan_snapshot_id: string;
  root_task_id: string;
  work_item_key: string;
  worker_slot_key: string;
  worker_id: string;
  execution_id: string;
  session_id: string;
}
