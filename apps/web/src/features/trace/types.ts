import type { components } from "@/shared/api/schema";

export type WorkerTrace = components["schemas"]["WorkerTraceResponse"];
export type WorkerTraceEvent = components["schemas"]["WorkerTraceEventResponse"];
export type TraceTiming = components["schemas"]["TraceTimingResponse"];
export type ExecutionSession = components["schemas"]["ExecutionSessionResponse"];
export type Execution = components["schemas"]["ExecutionResponse"];
export type ExecutionInput = components["schemas"]["ExecutionInputStatusResponse"];
export type ArtifactDescriptor = components["schemas"]["ArtifactContentDescriptorResponse"];
export type CommandResponse = components["schemas"]["CommandResponse"];
export type ExecutionContextAddress = components["schemas"]["ExecutionContextAddressPayload"];
