import type { ExecutionContextAddress } from "./types";

export interface InterventionDraft {
  text: string;
  clientIdempotencyKey: string;
}

export function interventionDraftKey(
  workspaceId: string,
  context: ExecutionContextAddress,
): string {
  return [
    "sidera:draft",
    workspaceId,
    context.plan_snapshot_id,
    context.work_item_key,
    context.execution_id,
    context.session_id,
  ].join(":");
}

export function loadInterventionDraft(key: string): InterventionDraft {
  if (typeof window === "undefined") {
    return { text: "", clientIdempotencyKey: crypto.randomUUID() };
  }
  const raw = window.localStorage.getItem(key);
  if (raw === null) {
    return { text: "", clientIdempotencyKey: crypto.randomUUID() };
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (isRecord(parsed) && typeof parsed.text === "string" && typeof parsed.clientIdempotencyKey === "string") {
      return { text: parsed.text, clientIdempotencyKey: parsed.clientIdempotencyKey };
    }
  } catch {
    // A malformed local draft should never block the trace page.
  }
  return { text: "", clientIdempotencyKey: crypto.randomUUID() };
}

export function saveInterventionDraft(key: string, draft: InterventionDraft): void {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(key, JSON.stringify(draft));
  }
}

export function clearInterventionDraft(key: string): void {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(key);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
