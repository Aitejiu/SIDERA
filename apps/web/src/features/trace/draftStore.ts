import type { ExecutionContextAddress } from "./types";

export interface WorkerItemInterventionDraft {
  text: string;
  clientIdempotencyKey: string;
  lastContextFingerprint: string;
}

export function workerItemDraftKey(workspaceId: string, workerItemId: string): string {
  return ["sidera:worker-item-draft", workspaceId, workerItemId].join(":");
}

export function contextFingerprint(context: ExecutionContextAddress | null): string {
  if (context === null) return "";
  return [
    context.worker_item_id,
    context.plan_generation,
    context.context_version,
    context.execution_id,
    context.session_id,
    context.session_version,
    context.worker_id,
  ].join("|");
}

export function loadWorkerItemDraft(key: string): WorkerItemInterventionDraft {
  const empty = () => ({
    text: "",
    clientIdempotencyKey: crypto.randomUUID(),
    lastContextFingerprint: "",
  });
  if (typeof window === "undefined") return empty();
  const raw = window.localStorage.getItem(key);
  if (raw === null) return empty();
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      isRecord(parsed)
      && typeof parsed.text === "string"
      && typeof parsed.clientIdempotencyKey === "string"
      && typeof parsed.lastContextFingerprint === "string"
    ) {
      return {
        text: parsed.text,
        clientIdempotencyKey: parsed.clientIdempotencyKey,
        lastContextFingerprint: parsed.lastContextFingerprint,
      };
    }
  } catch {
    // A malformed local draft must never block a WorkerItem workspace.
  }
  return empty();
}

export function saveWorkerItemDraft(key: string, draft: WorkerItemInterventionDraft): void {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(key, JSON.stringify(draft));
  }
}

export function clearWorkerItemDraft(key: string): void {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(key);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
