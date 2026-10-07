import { useCallback, useLayoutEffect, useRef, useState } from "react";

import type { LocalSession } from "@/features/local-session/api";
import { lookupNativeCommand, submitNativeCommand } from "@/features/assignments/commands";
import type { CommandRequest, CommandResponse, CommandStatusResponse } from "@/features/assignments/types";
import { planningDraftKey } from "./planningConfirmationDraft";
import type { PlanningConfirmationDraft, PlanningDraftScope } from "./planningConfirmationDraft";

type DraftWriter = (draft: PlanningConfirmationDraft) => boolean | Promise<boolean>;

interface NativePlanningCommandOptions {
  scope: PlanningDraftScope | null;
  session: LocalSession | null;
  draft: PlanningConfirmationDraft | null;
  onDraftChange: DraftWriter;
  onApplied: (original: CommandRequest, result: CommandResponse | null) => void | Promise<void>;
}

interface NativePlanningCommandState {
  submitOriginal(original: CommandRequest): Promise<void>;
  lookupOriginal(): Promise<void>;
  original: CommandRequest | null;
  status: PlanningConfirmationDraft["status"];
  busy: boolean;
  unresolved: boolean;
  error: string | null;
}

const SESSION_ERROR = "需要当前工作区的有效本地会话";
const SAVE_ERROR = "无法保存本地确认草稿";
const LOOKUP_ERROR = "暂无法核对原始命令";
const RESULT_ERROR = "命令结果不可确认";
const APPLIED_VIEW_ERROR = "命令已生效，但页面状态未能更新";

export function useNativePlanningCommand({
  scope,
  session,
  draft,
  onDraftChange,
  onApplied,
}: NativePlanningCommandOptions): NativePlanningCommandState {
  const scopeKey = scope === null ? "" : planningDraftKey(scope);
  const currentScopeKey = useRef(scopeKey);
  const currentOptions = useRef({ onDraftChange, onApplied });
  const busyKeysRef = useRef(new Set<string>());
  const [busyKeys, setBusyKeys] = useState<ReadonlySet<string>>(() => new Set());
  const [errors, setErrors] = useState<ReadonlyMap<string, string>>(() => new Map());
  const [localDrafts, setLocalDrafts] = useState<ReadonlyMap<string, PlanningConfirmationDraft>>(() => new Map());

  useLayoutEffect(() => {
    currentScopeKey.current = scopeKey;
  }, [scopeKey]);
  useLayoutEffect(() => {
    currentOptions.current = { onDraftChange, onApplied };
  }, [onApplied, onDraftChange]);

  const contextValid = scope !== null && validSession(scope, session);
  const inputMatches = contextValid
    && scope !== null && draft !== null && draftMatchesScope(scope, draft);
  const suppliedDraft = inputMatches ? draft : null;
  const priorDraft = !contextValid || scopeKey === "" ? undefined : localDrafts.get(scopeKey);
  let currentDraft = suppliedDraft ?? priorDraft ?? null;
  if (suppliedDraft !== null && priorDraft !== undefined) {
    const sameOriginal = suppliedDraft.original?.command_id === priorDraft.original?.command_id
      || suppliedDraft.original === null;
    currentDraft = sameOriginal
      ? { ...suppliedDraft, original: priorDraft.original, status: priorDraft.status }
      : suppliedDraft;
  }

  const setBusyFor = useCallback((key: string, value: boolean) => {
    if (value) busyKeysRef.current.add(key);
    else busyKeysRef.current.delete(key);
    setBusyKeys(new Set(busyKeysRef.current));
  }, [setBusyKeys]);

  const setErrorFor = useCallback((key: string, value: string | null) => {
    setErrors((previous) => {
      const next = new Map(previous);
      if (value === null) next.delete(key);
      else next.set(key, value);
      return next;
    });
  }, [setErrors]);

  const setDraftFor = useCallback((key: string, value: PlanningConfirmationDraft) => {
    setLocalDrafts((previous) => {
      const next = new Map(previous);
      next.set(key, value);
      return next;
    });
  }, [setLocalDrafts]);

  const writeDraft = useCallback(async (
    key: string,
    next: PlanningConfirmationDraft,
    requireDurable: boolean,
  ): Promise<boolean> => {
    if (currentScopeKey.current !== key) return false;
    let persisted: boolean;
    try {
      persisted = await currentOptions.current.onDraftChange(next);
    } catch {
      persisted = false;
    }
    if (currentScopeKey.current !== key) return false;
    if (persisted !== true) {
      setErrorFor(key, SAVE_ERROR);
      if (!requireDurable) setDraftFor(key, next);
      return !requireDurable;
    }
    setDraftFor(key, next);
    setErrorFor(key, null);
    return true;
  }, [setDraftFor, setErrorFor]);

  const notifyApplied = useCallback(async (
    key: string,
    original: CommandRequest,
    result: CommandResponse | null,
  ) => {
    if (currentScopeKey.current !== key) return;
    try {
      await currentOptions.current.onApplied(original, result);
    } catch {
      if (currentScopeKey.current === key) setErrorFor(key, APPLIED_VIEW_ERROR);
    }
  }, [setErrorFor]);

  const observeStatus = useCallback(async (
    key: string,
    base: PlanningConfirmationDraft,
    nextStatus: PlanningConfirmationDraft["status"],
    error: string | null,
    result: CommandResponse | null,
  ) => {
    const observedDraft = { ...base, status: nextStatus };
    const persisted = await writeDraft(key, observedDraft, false);
    if (error !== null) setErrorFor(key, error);
    else if (persisted) setErrorFor(key, null);
    if (nextStatus === "applied" && observedDraft.original !== null) {
      await notifyApplied(key, observedDraft.original, result);
    }
  }, [notifyApplied, setErrorFor, writeDraft]);

  const submitOriginal = useCallback(async (original: CommandRequest) => {
    const activeScope = scope;
    const key = scopeKey;
    const activeSession = session;
    if (activeScope === null || key === "" || !validSession(activeScope, activeSession)) {
      if (key !== "") setErrorFor(key, SESSION_ERROR);
      return;
    }
    if (!matchesCommand(activeScope, original)) {
      setErrorFor(key, RESULT_ERROR);
      return;
    }
    const latest = currentDraft;
    if (latest === null) {
      setErrorFor(key, SAVE_ERROR);
      return;
    }
    if (busyKeysRef.current.has(key) || latest.status === "pending" || latest.status === "unknown") return;
    if (latest.original?.command_id === original.command_id) return;
    const confirmationType = activeScope.kind === "revision"
      ? "ConfirmPlanRevision"
      : "ConfirmPlanningProposal";
    if (latest.original?.command_type === confirmationType
      && latest.status === "applied" && original.command_type === confirmationType) return;

    setBusyFor(key, true);
    setErrorFor(key, null);
    const pendingDraft: PlanningConfirmationDraft = {
      ...latest,
      original,
      status: "pending",
    };
    const durable = await writeDraft(key, pendingDraft, true);
    if (!durable || currentScopeKey.current !== key) {
      setBusyFor(key, false);
      return;
    }

    try {
      const response = await submitNativeCommand(original);
      if (currentScopeKey.current !== key) return;
      if (!responseMatches(original, response)) {
        await observeStatus(key, pendingDraft, "unknown", RESULT_ERROR, null);
        return;
      }
      const appliedDraft = { ...pendingDraft, status: "applied" as const };
      await writeDraft(key, appliedDraft, false);
      await notifyApplied(key, original, response);
    } catch {
      if (currentScopeKey.current !== key) return;
      await observeStatus(key, pendingDraft, "unknown", LOOKUP_ERROR, null);
    } finally {
      setBusyFor(key, false);
    }
  }, [currentDraft, notifyApplied, observeStatus, scope, scopeKey, session, setBusyFor, setErrorFor, writeDraft]);

  const lookupOriginal = useCallback(async () => {
    const activeScope = scope;
    const key = scopeKey;
    if (activeScope === null || key === "" || !validSession(activeScope, session)) {
      if (key !== "") setErrorFor(key, SESSION_ERROR);
      return;
    }
    const latest = currentDraft;
    const original = latest?.original ?? null;
    if (original === null || (latest?.status !== "pending" && latest?.status !== "unknown")
      || busyKeysRef.current.has(key)) return;

    setBusyFor(key, true);
    setErrorFor(key, null);
    try {
      const observed = await lookupNativeCommand(original);
      if (currentScopeKey.current !== key) return;
      if (!statusMatches(original, observed)) {
        await observeStatus(key, latest, "unknown", RESULT_ERROR, null);
        return;
      }
      if (observed.status === "pending") {
        await observeStatus(key, latest, "pending", null, null);
        return;
      }
      if (observed.status === "rejected") {
        await observeStatus(key, latest, "rejected", null, null);
        return;
      }
      if (observed.status !== "applied") {
        await observeStatus(key, latest, "unknown", RESULT_ERROR, null);
        return;
      }

      const result = observed.result ?? null;
      if (result !== null && !responseMatches(original, result)) {
        await observeStatus(key, latest, "unknown", RESULT_ERROR, null);
        return;
      }
      await observeStatus(key, latest, "applied", null, result);
    } catch {
      if (currentScopeKey.current === key) await observeStatus(key, latest, "unknown", LOOKUP_ERROR, null);
    } finally {
      setBusyFor(key, false);
    }
  }, [currentDraft, observeStatus, scope, scopeKey, session, setBusyFor, setErrorFor]);

  const activeDraft = scopeKey === "" ? null : currentDraft;
  const currentOriginal = activeDraft?.original ?? null;
  const status = activeDraft?.status ?? "idle";
  const unresolved = status === "pending" || status === "unknown";
  return {
    submitOriginal,
    lookupOriginal,
    original: currentOriginal,
    status,
    busy: scopeKey !== "" && busyKeys.has(scopeKey),
    unresolved,
    error: scopeKey === "" ? null : errors.get(scopeKey) ?? null,
  };
}

function validSession(scope: PlanningDraftScope, session: LocalSession | null): session is LocalSession {
  if (session === null || session.workspace_id !== scope.workspace_id
    || session.principal_id !== scope.principal_id || !nonblank(session.session_id)) return false;
  const expiry = Date.parse(session.expires_at);
  return Number.isFinite(expiry) && expiry > Date.now();
}

function matchesCommand(scope: PlanningDraftScope, original: CommandRequest): boolean {
  const matchesScope = original.workspace_id === scope.workspace_id
    && original.target_type === "planning_request"
    && original.target_id === scope.request_id
    && original.actor.type === "user"
    && original.actor.id === scope.principal_id
    && Number.isInteger(original.expected_version)
    && (original.expected_version ?? 0) > 0;
  return matchesScope && (scope.kind === "create" || original.command_type === "ConfirmPlanRevision");
}

function draftMatchesScope(scope: PlanningDraftScope, draft: PlanningConfirmationDraft): boolean {
  if (draft.kind !== scope.kind || draft.workspace_id !== scope.workspace_id
    || draft.request_id !== scope.request_id || draft.principal_id !== scope.principal_id) return false;
  return scope.kind === "create"
    || draft.kind === "revision" && draft.root_task_id === scope.root_task_id;
}

function responseMatches(original: CommandRequest, result: CommandResponse): boolean {
  return result.command_id === original.command_id && result.aggregate_id === original.target_id;
}

function statusMatches(original: CommandRequest, status: CommandStatusResponse): boolean {
  return status.command_id === original.command_id
    && (status.target_type === undefined || status.target_type === original.target_type)
    && (status.target_id === undefined || status.target_id === original.target_id)
    && (status.command_type === undefined || status.command_type === original.command_type);
}

function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
