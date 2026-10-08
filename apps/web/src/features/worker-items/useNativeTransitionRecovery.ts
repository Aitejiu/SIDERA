import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { buildNativeCommand, lookupNativeCommand, submitNativeCommand } from "../assignments/commands";
import type { LocalSession } from "../local-session/api";
import { readBusinessCommandAuthority, type BusinessCommandAuthority } from "../local-session/businessAuthority";
import { getWorkerItemWorkspace, getWorkerTransition } from "./api";
import {
  emptyNativeTransitionRecoveryDraft, freezeNativeTransitionRecoveryIntent, loadNativeTransitionRecoveryDraft,
  nativeTransitionRecoveryDraftKey, nativeTransitionRecoveryPayload, reviewNativeTransitionRecovery,
  reviewNativeTransitionRecoveryResult, sameNativeTransitionRecoveryPayload, sameNativeTransitionRecoveryReview,
  saveNativeTransitionRecoveryDraft, type NativeTransitionRecoveryDraft, type NativeTransitionRecoveryIntent,
  type NativeTransitionRecoveryScope, type NativeTransitionRecoveryStorageError,
} from "./nativeTransitionRecovery";

type Owner = {
  active: boolean; abort: AbortController; scope: NativeTransitionRecoveryScope; action: NativeTransitionRecoveryDraft;
  busy: boolean; error: string | null; storageError: NativeTransitionRecoveryStorageError | null;
  storageOperation: "load" | "save" | null; authorityDenied: boolean;
};
type Snapshot = Pick<Owner, "action" | "busy" | "error" | "storageError" | "authorityDenied">;
const messages = {
  authority: "当前操作身份未确认，请重新授权本地会话。",
  changed: "计划同步依据已变化，请重新打开并确认当前 Transition。",
  source: "当前计划同步依据暂不可用，请重新读取。",
  storage: "本地恢复记录未能确认，请重试读取或保存。",
  lookup: "原恢复结果暂未确认，请稍后再次查询。",
  refresh: "恢复请求已处理，当前事实暂未刷新。",
};

export function useNativeTransitionRecovery({ workspaceId, workerItemId, onRefresh }: {
  workspaceId: string; workerItemId: string; onRefresh: () => Promise<void>;
}) {
  const query = useQuery({ queryKey: ["native-transition-recovery-authority", workspaceId, workerItemId],
    queryFn: ({ signal }) => readBusinessCommandAuthority(workspaceId, { signal }),
    enabled: Boolean(workspaceId && workerItemId), retry: false });
  const authority = query.data ?? null;
  const [now, setNow] = useState(Date.now);
  const expiresAt = authority?.kind === "product" ? Date.parse(authority.session.expires_at) : null;
  useEffect(() => {
    if (expiresAt === null || !Number.isFinite(expiresAt) || expiresAt <= now) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.min(Math.max(0, expiresAt - Date.now()), 2_147_483_647));
    return () => clearTimeout(timer);
  }, [expiresAt, now]);
  const principalId = authority?.kind === "product" ? authority.session.principal_id : "";
  const scope = useMemo<NativeTransitionRecoveryScope | null>(() => principalId ? { workspaceId, workerItemId, principalId } : null,
    [workspaceId, workerItemId, principalId]);
  const owner = useRef<Owner | null>(null);
  const lifetime = useRef<object | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const current = useCallback((lease: Owner) => lease.active && owner.current === lease, []);
  const publish = useCallback((lease: Owner) => {
    if (current(lease)) setSnapshot({ action: lease.action, busy: lease.busy, error: lease.error,
      storageError: lease.storageError, authorityDenied: lease.authorityDenied });
  }, [current]);
  useLayoutEffect(() => {
    const entry = {}; lifetime.current = entry;
    return () => {
      if (lifetime.current === entry) lifetime.current = null;
      const lease = owner.current;
      if (lease) { lease.active = false; lease.abort.abort(); owner.current = null; }
    };
  }, [workspaceId, workerItemId]);
  useLayoutEffect(() => {
    if (scope === null) return;
    const prior = owner.current;
    if (prior && nativeTransitionRecoveryDraftKey(prior.scope) === nativeTransitionRecoveryDraftKey(scope)) return;
    if (prior) { prior.active = false; prior.abort.abort(); }
    const loaded = loadNativeTransitionRecoveryDraft(scope);
    const lease: Owner = { active: true, abort: new AbortController(), scope,
      action: loaded.ok ? loaded.draft : emptyNativeTransitionRecoveryDraft(scope), busy: false,
      error: loaded.ok ? null : messages.storage, storageError: loaded.ok ? null : loaded.error,
      storageOperation: loaded.ok ? null : "load", authorityDenied: false };
    owner.current = lease; queueMicrotask(() => publish(lease));
  }, [scope, publish]);

  function persist(lease: Owner, action: NativeTransitionRecoveryDraft) {
    if (!current(lease)) return;
    lease.action = action;
    const saved = saveNativeTransitionRecoveryDraft(lease.scope, action);
    lease.storageError = saved.ok ? null : saved.error; lease.storageOperation = saved.ok ? null : "save";
    if (!saved.ok) lease.error = messages.storage;
    publish(lease);
  }
  function deny(lease: Owner) {
    if (current(lease)) { lease.authorityDenied = true; lease.error = messages.authority; publish(lease); }
  }
  function live(lease: Owner, session: LocalSession) {
    if (!current(lease)) return false;
    if (!sessionFor({ kind: "product", session }, lease.scope)) { deny(lease); return false; }
    return true;
  }
  async function freshSession(lease: Owner) {
    const reviewed = await readBusinessCommandAuthority(lease.scope.workspaceId, {
      signal: lease.abort.signal, expectedPrincipalId: lease.scope.principalId,
    });
    if (!current(lease)) return null;
    const session = sessionFor(reviewed, lease.scope);
    if (!session) deny(lease);
    return session;
  }
  async function refresh(lease: Owner) {
    if (!current(lease)) return;
    try { await onRefresh(); }
    catch { if (current(lease)) { lease.error = lease.storageError ? messages.storage : messages.refresh; publish(lease); } }
  }
  async function perform(captured: NativeTransitionRecoveryIntent): Promise<boolean> {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || lease.storageError || lease.authorityDenied || unresolved(lease.action)
      || !authority || !sessionFor(authority, lease.scope)) return false;
    const intent = freezeNativeTransitionRecoveryIntent(captured);
    if (!intent) { lease.error = messages.changed; publish(lease); return false; }
    lease.busy = true; lease.error = null; publish(lease);
    let posted = false;
    try {
      const session = await freshSession(lease);
      if (!session || !live(lease, session)) return false;
      const workspace = await getWorkerItemWorkspace(lease.scope.workspaceId, lease.scope.workerItemId, lease.abort.signal);
      if (!live(lease, session)) return false;
      if (workspace.workspace_id !== lease.scope.workspaceId || workspace.worker_item?.id !== lease.scope.workerItemId
        || workspace.worker_item.root_task_id !== intent.rootTaskId || workspace.plan_sync?.status !== "blocked_unknown"
        || workspace.plan_sync.worker_transition_id !== intent.workerTransitionId) {
        lease.error = messages.changed; publish(lease); return false;
      }
      const transition = await getWorkerTransition(lease.scope.workspaceId, intent.workerTransitionId, lease.abort.signal);
      if (!live(lease, session)) return false;
      const reviewed = reviewNativeTransitionRecovery(workspace, lease.scope, transition);
      if (!reviewed || !sameNativeTransitionRecoveryReview(reviewed, intent)) {
        lease.error = messages.changed; publish(lease); return false;
      }
      const original = buildNativeCommand({ session, workspaceId: lease.scope.workspaceId, commandType: "RecoverWorkerTransition",
        targetType: "worker_transition", targetId: intent.workerTransitionId, expectedVersion: intent.version,
        payload: nativeTransitionRecoveryPayload(intent) });
      const pending: NativeTransitionRecoveryDraft = { ...emptyNativeTransitionRecoveryDraft(lease.scope), intent, original, status: "pending" };
      const saved = saveNativeTransitionRecoveryDraft(lease.scope, pending);
      if (!saved.ok) {
        lease.storageError = saved.error; lease.storageOperation = "save"; lease.error = messages.storage; publish(lease); return false;
      }
      lease.action = pending; publish(lease);
      posted = true;
      const response = await submitNativeCommand(original);
      if (!current(lease)) return false;
      const reviewedResult = reviewNativeTransitionRecoveryResult(pending, response);
      persist(lease, { ...pending, ...reviewedResult });
      if (reviewedResult.status === "applied") await refresh(lease);
      return true;
    } catch {
      if (!current(lease)) return false;
      if (posted) { lease.error = messages.lookup; persist(lease, { ...lease.action, status: "unknown",
        aggregate_id: null, aggregate_version: null, outcome: null }); }
      else { lease.error = messages.source; publish(lease); }
      return posted;
    } finally { if (current(lease)) { lease.busy = false; publish(lease); } }
  }
  async function lookupOriginal() {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || !lease.action.original) return;
    const original = lease.action.original;
    lease.busy = true; lease.error = null; publish(lease);
    try {
      const session = await freshSession(lease);
      if (!session || !live(lease, session)) return;
      const journal = await lookupNativeCommand(original, lease.abort.signal);
      if (!live(lease, session)) return;
      if (journal.payload !== undefined && !sameNativeTransitionRecoveryPayload(journal.payload, original.payload)) {
        lease.error = messages.lookup; publish(lease); return;
      }
      let next = lease.action;
      if (journal.status === "applied") {
        const result = journal.result === null ? { status: "applied" as const, aggregate_id: null, aggregate_version: null, outcome: null }
          : reviewNativeTransitionRecoveryResult(next, journal.result);
        if (result.status !== "applied" || next.status === "rejected" || next.outcome !== null && result.outcome !== null
          && (result.outcome !== next.outcome || result.aggregate_id !== next.aggregate_id || result.aggregate_version !== next.aggregate_version)) {
          lease.error = messages.lookup; publish(lease); return;
        }
        if (next.outcome === null) next = { ...next, ...result };
      } else if (journal.status === "rejected") {
        if (next.status === "applied") { lease.error = messages.lookup; publish(lease); return; }
        next = { ...next, status: "rejected", rejection_code: "command_rejected", aggregate_id: null, aggregate_version: null, outcome: null };
      } else if (journal.status === "pending") {
        if (next.status !== "applied" && next.status !== "rejected") next = { ...next, status: "pending" };
      } else { lease.error = messages.lookup; publish(lease); return; }
      persist(lease, next);
      if (next.status === "applied" || next.status === "rejected") await refresh(lease);
    } catch { if (current(lease)) { lease.error = messages.lookup; publish(lease); } }
    finally { if (current(lease)) { lease.busy = false; publish(lease); } }
  }
  function retryStorage() {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || !lease.storageError) return;
    if (lease.storageOperation === "load") {
      const loaded = loadNativeTransitionRecoveryDraft(lease.scope);
      if (!loaded.ok) { lease.storageError = loaded.error; lease.error = messages.storage; publish(lease); return; }
      lease.action = loaded.draft;
    } else {
      const saved = saveNativeTransitionRecoveryDraft(lease.scope, lease.action);
      if (!saved.ok) { lease.storageError = saved.error; lease.error = messages.storage; publish(lease); return; }
    }
    lease.storageError = null; lease.storageOperation = null; lease.error = null; publish(lease);
  }
  async function onSessionAuthorized(session: LocalSession) {
    if (session.workspace_id !== workspaceId) return;
    const entry = lifetime.current, captured = owner.current;
    const completed = await query.refetch();
    if (entry === null || lifetime.current !== entry || !captured || !current(captured)) return;
    if (completed.isSuccess && completed.data && sessionFor(completed.data, captured.scope)) {
      captured.authorityDenied = false; captured.error = captured.storageError ? messages.storage : null; publish(captured);
    } else deny(captured);
  }

  const visible = snapshot && snapshot.action.scope.workspaceId === workspaceId && snapshot.action.scope.workerItemId === workerItemId
    && (!principalId || snapshot.action.scope.principalId === principalId) ? snapshot : null;
  const authorized = visible !== null && authority !== null && expiresAt !== null && Number.isFinite(expiresAt)
    && expiresAt > now && sessionFor(authority, visible.action.scope) !== null && !visible.authorityDenied;
  const action = visible?.action ?? null;
  return { authority, scope: action?.scope ?? scope, action, original: action?.original ?? null, status: action?.status ?? "idle",
    authorized, canPerform: authorized && visible !== null && !visible.busy && !visible.storageError && !unresolved(visible.action),
    busy: visible?.busy ?? false, error: visible?.error ?? null, storageError: visible?.storageError ?? null,
    perform, lookupOriginal, retryStorage, onSessionAuthorized,
  };
}

function sessionFor(authority: BusinessCommandAuthority, scope: NativeTransitionRecoveryScope): LocalSession | null {
  return authority.kind === "product" && authority.session.workspace_id === scope.workspaceId
    && authority.session.principal_id === scope.principalId && Date.parse(authority.session.expires_at) > Date.now() ? authority.session : null;
}
function unresolved(action: NativeTransitionRecoveryDraft): boolean {
  return action.status === "pending" || action.status === "unknown" || action.status === "applied" && action.outcome === null;
}
