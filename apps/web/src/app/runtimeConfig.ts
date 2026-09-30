export const FALLBACK_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function resolveDefaultWorkspaceId(value: string | undefined): string {
  const workspaceId = value?.trim();
  return workspaceId ? workspaceId : FALLBACK_WORKSPACE_ID;
}

export const DEFAULT_WORKSPACE_ID = resolveDefaultWorkspaceId(
  import.meta.env.VITE_SIDERA_WORKSPACE_ID,
);
