import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { AgentApiProblem } from "@/features/agents/api";
import { readLocalSession } from "@/features/local-session/api";
import { getExecution, getExecutionSession } from "./api";
import {
  NativeTraceUnavailableError,
  nativeTracePinKey,
  readSavedTraceExecution,
  reviewTraceSelection,
  traceReadError,
  type TraceSelectionAddress,
  type WorkerTraceSelection,
} from "./nativeTrace";
import type { Execution } from "./types";

interface SelectedFacts {
  execution: Execution;
  selection: WorkerTraceSelection;
}

export function useWorkerTraceSelection({ workspaceId, address }: {
  workspaceId: string;
  address: TraceSelectionAddress;
}) {
  const queryClient = useQueryClient();
  const sessionKey = ["worker-trace-session", workspaceId, address.rootTaskId, address.executionId, address.sessionId] as const;
  const localSessionQuery = useQuery({
    queryKey: sessionKey,
    queryFn: ({ signal }) => readLocalSession(signal),
    enabled: Boolean(workspaceId && address.rootTaskId && address.executionId),
    retry: false,
  });
  const localSession = localSessionQuery.data;
  const sessionSample = localSessionQuery.dataUpdatedAt;
  const [expiredSample, setExpiredSample] = useState<number | null>(null);
  const expiresAt = localSession ? Date.parse(localSession.expires_at) : NaN;
  const sessionAuthorized = !localSessionQuery.isError && localSession?.workspace_id === workspaceId
    && typeof localSession.principal_id === "string" && localSession.principal_id.trim().length > 0
    && Number.isFinite(expiresAt) && expiresAt > sessionSample && expiredSample !== sessionSample;
  const principalId = sessionAuthorized ? localSession.principal_id : null;

  useEffect(() => {
    if (!sessionAuthorized) return;
    let timer: ReturnType<typeof setTimeout>;
    const checkExpiry = () => {
      const remaining = expiresAt - Date.now();
      if (remaining <= 0) setExpiredSample(sessionSample);
      else timer = setTimeout(checkExpiry, Math.min(2_147_483_647, remaining));
    };
    checkExpiry();
    return () => clearTimeout(timer);
  }, [expiresAt, sessionAuthorized, sessionSample]);

  const selectionKey = ["worker-trace-selection", workspaceId, principalId, address.executionId,
    "selected", address.rootTaskId, address.sessionId] as const;
  const scopeKey = JSON.stringify(selectionKey);
  const activeScope = useRef<string | null>(scopeKey);
  useLayoutEffect(() => {
    activeScope.current = scopeKey;
    return () => { activeScope.current = null; };
  }, [scopeKey]);

  const selectionQuery = useQuery({
    queryKey: selectionKey,
    enabled: sessionAuthorized,
    retry: false,
    queryFn: async ({ signal }): Promise<SelectedFacts> => {
      if (principalId === null) throw new NativeTraceUnavailableError("denied");
      const execution = await getExecution(workspaceId, address.executionId, signal);
      const agentExecution = await readSavedTraceExecution(workspaceId, address.executionId, signal);
      const session = address.sessionId && address.sessionId !== "unknown"
        ? await getExecutionSession(workspaceId, address.sessionId, signal) : null;
      const selection = reviewTraceSelection({ workspaceId, address, principalId, execution, agentExecution, session });
      const previous = queryClient.getQueryData<SelectedFacts>(selectionKey)?.selection;
      if (previous && (previous.mode !== selection.mode
        || (previous.mode === "native" && selection.mode === "native"
          && nativeTracePinKey(previous.pin) !== nativeTracePinKey(selection.pin)))) {
        throw new NativeTraceUnavailableError();
      }
      return { execution, selection };
    },
  });

  const localError = localSessionQuery.isError
    ? localSessionQuery.error instanceof AgentApiProblem && [401, 403].includes(localSessionQuery.error.status)
      ? new NativeTraceUnavailableError("denied") : new NativeTraceUnavailableError()
    : localSessionQuery.isSuccess && !sessionAuthorized ? new NativeTraceUnavailableError("denied") : null;
  const loading = localSessionQuery.isPending || (sessionAuthorized && selectionQuery.isPending);
  const error = localError ?? (selectionQuery.isError ? traceReadError(selectionQuery.error) : null);
  const facts = sessionAuthorized && !error ? selectionQuery.data : undefined;

  const retry = async () => {
    if (activeScope.current !== scopeKey) return;
    const response = await localSessionQuery.refetch();
    if (activeScope.current !== scopeKey || response.isError
      || response.data?.principal_id !== principalId || response.data?.workspace_id !== workspaceId) return;
    await selectionQuery.refetch();
  };

  return {
    localSession,
    sessionAuthorized,
    authorizationRequired: localError?.kind === "denied",
    selection: facts?.selection ?? null,
    execution: facts?.execution,
    loading,
    error,
    retry,
    scopeKey,
  };
}
