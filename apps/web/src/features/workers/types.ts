import type { components } from "@/shared/api/schema";

export type Worker = components["schemas"]["WorkerResponse"];
export type ModuleSummary = components["schemas"]["ModuleSummaryResponse"];
export type ConfigureWorkerRuntimePayload =
  components["schemas"]["ConfigureWorkerRuntimePayload"];
export type CommandRequest = components["schemas"]["CommandRequest"];
export type CommandResponse = components["schemas"]["CommandResponse"];

export type WorkerModuleType = "worker_adapter" | "workspace_provider";
