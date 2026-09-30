import { useQuery } from "@tanstack/react-query";

import { getDemoStatus } from "./api";

export function demoStatusLabel(mode: string | null): string | null {
  return mode === "interview-demo" ? "模拟演示" : null;
}

export function DemoModeIndicator({ workspaceId }: { workspaceId: string }) {
  const query = useQuery({
    queryKey: ["demo", workspaceId, "status"],
    queryFn: ({ signal }) => getDemoStatus(signal),
    retry: false,
    staleTime: 30_000,
  });
  const label = demoStatusLabel(query.data?.mode ?? null);
  if (label === null) {
    return null;
  }
  return (
    <span className="demo-mode-indicator" title={`当前来源：${label}`}>
      <span className="demo-mode-indicator__dot" aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
