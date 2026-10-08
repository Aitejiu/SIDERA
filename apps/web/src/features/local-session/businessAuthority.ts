import { getHealth } from "@/features/chat/api";

import { readLocalSession, type LocalSession } from "./api";

export type BusinessAuthorityReason =
  | "host_unavailable"
  | "session_unavailable"
  | "session_mismatch"
  | "session_expired";

export type BusinessCommandAuthority =
  | { kind: "product"; session: LocalSession }
  | { kind: "legacy" }
  | { kind: "unavailable"; reason: BusinessAuthorityReason };

export interface BusinessAuthorityReview {
  workspaceId: string;
  health: unknown;
  session: unknown;
  expectedPrincipalId?: string;
  now: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonblankString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isAbortError(cause: unknown): boolean {
  return isRecord(cause) && cause.name === "AbortError";
}

function businessHostKind(health: unknown): "product" | "legacy" | null {
  if (!isRecord(health) || health.status !== "ok") return null;
  if (health.service === "sidera-product") return "product";
  if (health.service === "sidera-main-agent-gateway") return "legacy";
  return null;
}

function isPublicLocalSession(session: unknown): session is LocalSession {
  return isRecord(session)
    && isNonblankString(session.session_id)
    && isNonblankString(session.workspace_id)
    && isNonblankString(session.principal_id)
    && isNonblankString(session.expires_at)
    && Number.isFinite(Date.parse(session.expires_at));
}

export function reviewBusinessCommandAuthority(
  input: BusinessAuthorityReview,
): BusinessCommandAuthority {
  if (!isNonblankString(input.workspaceId) || !Number.isFinite(input.now)) {
    return { kind: "unavailable", reason: "host_unavailable" };
  }

  const host = businessHostKind(input.health);
  if (host === null) return { kind: "unavailable", reason: "host_unavailable" };
  if (host === "legacy") return { kind: "legacy" };

  const session = input.session;
  if (!isPublicLocalSession(session)) {
    return { kind: "unavailable", reason: "session_unavailable" };
  }
  if (session.workspace_id !== input.workspaceId
    || (input.expectedPrincipalId !== undefined && session.principal_id !== input.expectedPrincipalId)) {
    return { kind: "unavailable", reason: "session_mismatch" };
  }
  if (Date.parse(session.expires_at) <= input.now) {
    return { kind: "unavailable", reason: "session_expired" };
  }

  return {
    kind: "product",
    session: {
      session_id: session.session_id,
      workspace_id: session.workspace_id,
      principal_id: session.principal_id,
      expires_at: session.expires_at,
    },
  };
}

export async function readBusinessCommandAuthority(
  workspaceId: string,
  options?: { signal?: AbortSignal; expectedPrincipalId?: string },
): Promise<BusinessCommandAuthority> {
  const { signal, expectedPrincipalId } = options ?? {};
  signal?.throwIfAborted();
  if (!isNonblankString(workspaceId)) {
    return { kind: "unavailable", reason: "host_unavailable" };
  }

  let health: unknown;
  try {
    health = await getHealth(signal);
    signal?.throwIfAborted();
  } catch (cause) {
    signal?.throwIfAborted();
    if (isAbortError(cause)) throw cause;
    return { kind: "unavailable", reason: "host_unavailable" };
  }

  if (businessHostKind(health) !== "product") {
    return reviewBusinessCommandAuthority({
      workspaceId,
      health,
      session: null,
      ...(expectedPrincipalId === undefined ? {} : { expectedPrincipalId }),
      now: Date.now(),
    });
  }

  let session: unknown;
  try {
    session = await readLocalSession(signal);
    signal?.throwIfAborted();
  } catch (cause) {
    // The session transport wraps fetch aborts; recover the supplied cancellation.
    signal?.throwIfAborted();
    if (isAbortError(cause)) throw cause;
    return { kind: "unavailable", reason: "session_unavailable" };
  }

  return reviewBusinessCommandAuthority({
    workspaceId,
    health,
    session,
    ...(expectedPrincipalId === undefined ? {} : { expectedPrincipalId }),
    now: Date.now(),
  });
}
