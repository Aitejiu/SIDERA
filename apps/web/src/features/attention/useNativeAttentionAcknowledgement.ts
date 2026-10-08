import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { buildNativeCommand, lookupNativeCommand, submitNativeCommand } from "../assignments/commands";
import type { LocalSession } from "../local-session/api";
import { readBusinessCommandAuthority, type BusinessCommandAuthority } from "../local-session/businessAuthority";
import { getTask, getTaskGraph } from "../tasks/api";
import {
  emptyNativeAttentionDraft, freezeNativeAttentionIntent, loadNativeAttentionDraft, nativeAttentionDraftKey,
  nativeAttentionPayload, reviewNativeAttentionJournal, reviewNativeAttentionResult, saveNativeAttentionDraft,
  unresolvedNativeAttentionDraft, type NativeAttentionDraft, type NativeAttentionItem,
  type NativeAttentionScope, type NativeAttentionStorageError,
} from "./nativeAttentionAcknowledgement";

type Owner = {
  active: boolean;
  abort: AbortController;
  key: string;
  scope: NativeAttentionScope;
  draft: NativeAttentionDraft;
  busy: boolean;
  pendingKey: string | null;
  error: string | null;
  storageError: NativeAttentionStorageError | null;
  storageOperation: "load" | "save" | null;
  authorityDenied: boolean;
};
type Snapshot = Pick<Owner, "key" | "draft" | "busy" | "pendingKey" | "error" | "storageError" | "authorityDenied">;
const messages = {
  authority: "当前操作身份未确认，请重新授权本地会话。",
  changed: "待处理事项已变化，请刷新后重新确认。",
  source: "当前待处理事项暂不可用，请重新读取。",
  storage: "本地确认记录未能确认，请重试读取或保存。",
  lookup: "原确认结果暂未确认，请稍后再次查询。",
  refresh: "确认已处理，当前事实暂未刷新。",
};

export function useNativeAttentionAcknowledgement({ workspaceId, rootTaskId, onRefresh }: {
  workspaceId: string;
  rootTaskId: string;
  onRefresh: () => Promise<void>;
}) {
  const authorityQuery = useQuery({
    queryKey: ["native-attention-authority", workspaceId, rootTaskId],
    queryFn: ({ signal }) => readBusinessCommandAuthority(workspaceId, { signal }),
    enabled: Boolean(workspaceId && rootTaskId), retry: false,
  });
  const authority = authorityQuery.data ?? null;
  const [now, setNow] = useState(Date.now);
  const expiresAt = authority?.kind === "product" ? Date.parse(authority.session.expires_at) : null;
  useEffect(() => {
    if (expiresAt === null || !Number.isFinite(expiresAt) || expiresAt <= now) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.min(Math.max(0, expiresAt - Date.now()), 2_147_483_647));
    return () => clearTimeout(timer);
  }, [expiresAt, now]);
  const principalId = authority?.kind === "product" ? authority.session.principal_id : "";
  const scope = useMemo<NativeAttentionScope | null>(() => workspaceId && rootTaskId && principalId
    ? { workspaceId, rootTaskId, principalId } : null, [workspaceId, rootTaskId, principalId]);
  const owner = useRef<Owner | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const current = useCallback((lease: Owner) => lease.active && owner.current === lease, []);
  const publish = useCallback((lease: Owner) => {
    if (current(lease)) setSnapshot({
      key: lease.key, draft: lease.draft, busy: lease.busy, pendingKey: lease.pendingKey,
      error: lease.error, storageError: lease.storageError, authorityDenied: lease.authorityDenied,
    });
  }, [current]);

  useLayoutEffect(() => {
    if (scope === null) { owner.current = null; return; }
    const loaded = loadNativeAttentionDraft(scope);
    const lease: Owner = {
      active: true, abort: new AbortController(), key: nativeAttentionDraftKey(scope), scope,
      draft: loaded.ok ? loaded.draft : emptyNativeAttentionDraft(scope), busy: false, pendingKey: null,
      error: loaded.ok ? null : messages.storage, storageError: loaded.ok ? null : loaded.error,
      storageOperation: loaded.ok ? null : "load", authorityDenied: false,
    };
    owner.current = lease;
    queueMicrotask(() => publish(lease));
    return () => { lease.active = false; lease.abort.abort(); if (owner.current === lease) owner.current = null; };
  }, [scope, publish]);

  useLayoutEffect(() => {
    const lease = owner.current;
    if (lease && authority && sessionFor(authority, lease.scope) && lease.authorityDenied) {
      lease.authorityDenied = false; lease.error = lease.storageError ? messages.storage : null;
      queueMicrotask(() => publish(lease));
    }
  }, [authority, publish]);

  function persist(lease: Owner, draft: NativeAttentionDraft) {
    if (!current(lease)) return;
    lease.draft = Object.freeze(draft);
    const saved = saveNativeAttentionDraft(lease.scope, draft);
    lease.storageError = saved.ok ? null : saved.error;
    lease.storageOperation = saved.ok ? null : "save";
    if (!saved.ok) lease.error = messages.storage;
    publish(lease);
  }

  async function freshSession(lease: Owner): Promise<LocalSession | null> {
    const reviewed = await readBusinessCommandAuthority(lease.scope.workspaceId, {
      expectedPrincipalId: lease.scope.principalId, signal: lease.abort.signal,
    });
    if (!current(lease)) return null;
    const session = sessionFor(reviewed, lease.scope);
    lease.authorityDenied = session === null;
    if (session === null) lease.error = messages.authority;
    publish(lease);
    return session;
  }

  async function refresh(lease: Owner) {
    if (!current(lease)) return;
    try { await onRefresh(); }
    catch {
      if (current(lease)) { lease.error = lease.storageError ? messages.storage : messages.refresh; publish(lease); }
    }
  }

  async function acknowledge(item: NativeAttentionItem, displayedRootVersion: number | undefined): Promise<boolean> {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || lease.storageError || lease.authorityDenied
      || unresolvedNativeAttentionDraft(lease.draft) || authority === null || sessionFor(authority, lease.scope) === null) return false;
    const intent = freezeNativeAttentionIntent(item, displayedRootVersion);
    if (intent === null) { lease.error = messages.changed; publish(lease); return false; }
    lease.busy = true; lease.pendingKey = intent.attentionKey; lease.error = null; publish(lease);
    let posted = false;
    try {
      const session = await freshSession(lease);
      if (!current(lease) || session === null) return false;
      const task = await getTask(lease.scope.workspaceId, lease.scope.rootTaskId, lease.abort.signal);
      if (!current(lease)) return false;
      if (task.workspace_id !== lease.scope.workspaceId || task.id !== lease.scope.rootTaskId
        || task.version !== intent.rootTaskVersion) {
        lease.error = messages.changed; publish(lease); return false;
      }
      const graph = await getTaskGraph(lease.scope.workspaceId, lease.scope.rootTaskId, lease.abort.signal);
      if (!current(lease)) return false;
      const canonical = graph.overview.attention_items.find(candidate => candidate.key === intent.attentionKey);
      if (graph.overview.workspace_id !== lease.scope.workspaceId || graph.overview.root_task_id !== lease.scope.rootTaskId
        || !canonical || canonical.kind === "collaboration_release_proposal"
        || canonical.correlation_id !== intent.sourceCorrelationId || canonical.resource_version !== intent.sourceResourceVersion) {
        lease.error = messages.changed; publish(lease); return false;
      }
      if (!current(lease) || sessionFor({ kind: "product", session }, lease.scope) === null) {
        if (current(lease)) { lease.authorityDenied = true; lease.error = messages.authority; publish(lease); }
        return false;
      }
      const original = buildNativeCommand({
        session, workspaceId: lease.scope.workspaceId, commandType: "AcknowledgeAttentionItem", targetType: "task",
        targetId: lease.scope.rootTaskId, expectedVersion: intent.rootTaskVersion, payload: nativeAttentionPayload(intent),
      });
      const pending: NativeAttentionDraft = Object.freeze({ ...emptyNativeAttentionDraft(lease.scope), intent, original, status: "pending" });
      const saved = saveNativeAttentionDraft(lease.scope, pending);
      if (!saved.ok) {
        lease.storageError = saved.error; lease.storageOperation = "save"; lease.error = messages.storage; publish(lease);
        return false;
      }
      lease.draft = pending; publish(lease);
      posted = true;
      const response = await submitNativeCommand(original);
      if (!current(lease)) return false;
      const reviewed = reviewNativeAttentionResult(original, response);
      if (reviewed.outcome === null) lease.error = messages.lookup;
      persist(lease, { ...pending, ...reviewed });
      if (reviewed.outcome === "acknowledged") {
        await refresh(lease);
        if (!current(lease)) return false;
        return true;
      }
      return false;
    } catch {
      if (!current(lease)) return false;
      if (posted) {
        lease.error = messages.lookup;
        persist(lease, { ...lease.draft, status: "unknown", outcome: null, rejection_code: null });
      } else { lease.error = messages.source; publish(lease); }
      return false;
    } finally {
      if (current(lease)) { lease.busy = false; lease.pendingKey = null; publish(lease); }
    }
  }

  async function lookupOriginal() {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || lease.draft.original === null) return;
    const original = lease.draft.original;
    lease.busy = true; lease.error = null; publish(lease);
    try {
      const session = await freshSession(lease);
      if (!current(lease) || session === null) return;
      const journal = await lookupNativeCommand(original, lease.abort.signal);
      if (!current(lease)) return;
      const reviewed = reviewNativeAttentionJournal(original, journal);
      if (reviewed === null || (lease.draft.status === "applied" && reviewed.status === "rejected")
        || (lease.draft.status === "rejected" && reviewed.status === "applied")) {
        lease.error = messages.lookup; publish(lease); return;
      }
      const next = lease.draft.outcome === "acknowledged" ? lease.draft : { ...lease.draft, ...reviewed };
      persist(lease, next);
      if (next.outcome === "acknowledged") await refresh(lease);
      if (!current(lease)) return;
    } catch { if (current(lease)) { lease.error = messages.lookup; publish(lease); } }
    finally { if (current(lease)) { lease.busy = false; publish(lease); } }
  }

  function retryStorage() {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || !lease.storageError) return;
    if (lease.storageOperation === "load") {
      const loaded = loadNativeAttentionDraft(lease.scope);
      if (!loaded.ok) { lease.storageError = loaded.error; lease.error = messages.storage; publish(lease); return; }
      lease.draft = loaded.draft;
    } else {
      const saved = saveNativeAttentionDraft(lease.scope, lease.draft);
      if (!saved.ok) { lease.storageError = saved.error; lease.error = messages.storage; publish(lease); return; }
    }
    lease.storageError = null; lease.storageOperation = null; lease.error = null; publish(lease);
  }

  const key = scope === null ? null : nativeAttentionDraftKey(scope);
  const visible = snapshot?.key === key ? snapshot : null;
  const authorized = visible !== null && authority !== null && expiresAt !== null && Number.isFinite(expiresAt)
    && expiresAt > now && sessionFor(authority, visible.draft.scope) !== null && !visible.authorityDenied;
  return {
    authority, authorized, state: visible?.draft ?? null, original: visible?.draft.original ?? null,
    error: visible?.error ?? null, storageError: visible?.storageError ?? null,
    busy: visible?.busy ?? false, pendingKey: visible?.pendingKey ?? null,
    canAcknowledge: authorized && visible !== null && !visible.busy && !visible.storageError && !unresolvedNativeAttentionDraft(visible.draft),
    acknowledge, lookupOriginal, retryStorage,
    onSessionAuthorized: async (session: LocalSession) => {
      if (session.workspace_id !== workspaceId) return;
      const lease = owner.current;
      const result = await authorityQuery.refetch();
      if (!lease || !current(lease) || !result.isSuccess || !result.data || sessionFor(result.data, lease.scope) === null) return;
      lease.authorityDenied = false; lease.error = lease.storageError ? messages.storage : null;
      publish(lease);
    },
  };
}

function sessionFor(authority: BusinessCommandAuthority, scope: NativeAttentionScope): LocalSession | null {
  return authority.kind === "product" && authority.session.workspace_id === scope.workspaceId
    && authority.session.principal_id === scope.principalId && Date.parse(authority.session.expires_at) > Date.now()
    ? authority.session : null;
}
