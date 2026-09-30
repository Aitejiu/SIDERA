import { getCapabilitySnapshot, type CapabilitySnapshot } from "@/features/demo/api";

export function workspaceCapabilitiesQueryKey(workspaceId: string) {
  return ["workspace", workspaceId, "capabilities"] as const;
}

export function getWorkspaceCapabilities(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<CapabilitySnapshot> {
  return getCapabilitySnapshot(workspaceId, signal);
}
