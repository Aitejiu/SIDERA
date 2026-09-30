import { ApiProblem, apiRequestJson } from "@/shared/api/client";

export type DemoStatus = {
  mode: string;
  run_id: string;
  workspace_id: string;
  scenario: string;
  phase: string;
};

export type ServiceState = "available" | "unavailable" | "unknown";

export type ModuleStatus = {
  id: string;
  instance_name: string;
  module_type: string;
  provider: string;
  definition_lifecycle: string;
  instance_lifecycle: string;
  config_version: number;
};

export type CapabilitySnapshot = {
  controlPlane: ServiceState;
  mainAgent: ServiceState;
  workerAdapters: ModuleStatus[];
  workspaceProviders: ModuleStatus[];
  demo: DemoStatus | null;
};

type HealthResponse = {
  status?: string;
  provider?: string;
};

export async function getDemoStatus(signal?: AbortSignal): Promise<DemoStatus | null> {
  try {
    return await apiRequestJson<DemoStatus>("/api/v1/demo/status", requestOptions(signal));
  } catch (error) {
    if (error instanceof ApiProblem && error.status === 404) {
      return null;
    }
    throw error;
  }
}

export async function getCapabilitySnapshot(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<CapabilitySnapshot> {
  const [controlPlane, mainAgent, workerAdapters, workspaceProviders, demo] = await Promise.all([
    probeHealth("/healthz", signal, (value) => value.status === "ok"),
    probeHealth("/main-agent/v1/health", signal, (value) => value.provider === "available"),
    probeModules(workspaceId, "worker_adapter", signal),
    probeModules(workspaceId, "workspace_provider", signal),
    probeDemo(signal),
  ]);
  return {
    controlPlane,
    mainAgent,
    workerAdapters,
    workspaceProviders,
    demo,
  };
}

async function probeHealth(
  path: string,
  signal: AbortSignal | undefined,
  isAvailable: (value: HealthResponse) => boolean,
): Promise<ServiceState> {
  try {
    const value = await apiRequestJson<HealthResponse>(path, requestOptions(signal));
    return isAvailable(value) ? "available" : "unavailable";
  } catch {
    return "unknown";
  }
}

async function probeModules(
  workspaceId: string,
  moduleType: "worker_adapter" | "workspace_provider",
  signal: AbortSignal | undefined,
): Promise<ModuleStatus[]> {
  try {
    const query = new URLSearchParams({ workspace_id: workspaceId, module_type: moduleType });
    return await apiRequestJson<ModuleStatus[]>(`/api/v1/modules?${query.toString()}`, requestOptions(signal));
  } catch {
    return [];
  }
}

async function probeDemo(signal: AbortSignal | undefined): Promise<DemoStatus | null> {
  try {
    return await getDemoStatus(signal);
  } catch {
    return null;
  }
}

function requestOptions(signal?: AbortSignal): RequestInit | undefined {
  return signal === undefined ? undefined : { signal };
}
