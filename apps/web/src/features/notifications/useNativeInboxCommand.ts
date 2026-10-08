import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { lookupNativeCommand, submitNativeCommand } from "../assignments/commands";
import type { LocalSession } from "../local-session/api";
import { readBusinessCommandAuthority, type BusinessCommandAuthority } from "../local-session/businessAuthority";
import { getNotificationThread, type NotificationThreadDetail } from "./api";
import {
  buildNativeInboxOriginal, emptyNativeInboxDraft, freezeNativeInboxIntent, loadNativeInboxDraft,
  nativeInboxDraftKey, reviewNativeInboxJournal, reviewNativeInboxResult, saveNativeInboxDraft,
  unresolvedNativeInboxDraft, type NativeInboxDraft, type NativeInboxIntent, type NativeInboxScope, type NativeInboxStorageError,
} from "./nativeInboxCommand";

type Owner = {
  active: boolean; abort: AbortController; key: string; scope: NativeInboxScope; draft: NativeInboxDraft;
  busy: boolean; pendingLabel: string | null; error: string | null; storageError: NativeInboxStorageError | null;
  storageOperation: "load" | "save" | null; authorityDenied: boolean;
};
type Snapshot = Pick<Owner, "key" | "draft" | "busy" | "pendingLabel" | "error" | "storageError" | "authorityDenied">;
const messages = {
  authority: "当前操作身份未确认，请重新授权本地会话。",
  changed: "通知事实已变化，请刷新后重新确认。",
  source: "当前通知事实暂不可用，请重新读取。",
  storage: "本地操作记录未能确认，请重试读取或保存。",
  lookup: "原操作结果暂未确认，请查询原操作。",
  refresh: "操作已处理，当前通知事实暂未刷新。",
};

export function useNativeInboxCommand({ workspaceId, onRefresh }: { workspaceId: string; onRefresh: () => Promise<void> }) {
  const query = useQuery({ queryKey: ["native-inbox-authority", workspaceId],
    queryFn: ({ signal }) => readBusinessCommandAuthority(workspaceId, { signal }), enabled: Boolean(workspaceId), retry: false });
  const authority = query.data ?? null;
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [now, setNow] = useState(Date.now);
  const expiresAt = authority?.kind === "product" ? Date.parse(authority.session.expires_at) : null;
  useEffect(() => {
    if (expiresAt === null || !Number.isFinite(expiresAt) || expiresAt <= now) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.min(Math.max(0, expiresAt - Date.now()), 2_147_483_647));
    return () => clearTimeout(timer);
  }, [expiresAt, now]);
  // A denied observation keeps the already-owned original available for reauthorization.
  const principalId = authority?.kind === "product" ? authority.session.principal_id
    : snapshot?.draft.scope.workspaceId === workspaceId ? snapshot.draft.scope.principalId : "";
  const scope = useMemo<NativeInboxScope | null>(() => workspaceId && principalId ? { workspaceId, principalId } : null, [workspaceId, principalId]);
  const owner = useRef<Owner | null>(null);
  const current = useCallback((lease: Owner) => lease.active && owner.current === lease, []);
  const publish = useCallback((lease: Owner) => {
    if (current(lease)) setSnapshot({ key: lease.key, draft: lease.draft, busy: lease.busy, pendingLabel: lease.pendingLabel,
      error: lease.error, storageError: lease.storageError, authorityDenied: lease.authorityDenied });
  }, [current]);

  useLayoutEffect(() => {
    if (scope === null) { owner.current = null; return; }
    const loaded = loadNativeInboxDraft(scope);
    const lease: Owner = {
      active: true, abort: new AbortController(), key: nativeInboxDraftKey(scope), scope,
      draft: loaded.ok ? loaded.draft : emptyNativeInboxDraft(scope), busy: false, pendingLabel: null,
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

  function persist(lease: Owner, draft: NativeInboxDraft) {
    if (!current(lease)) return;
    lease.draft = Object.freeze(draft);
    const saved = saveNativeInboxDraft(lease.scope, draft);
    lease.storageError = saved.ok ? null : saved.error;
    lease.storageOperation = saved.ok ? null : "save";
    if (!saved.ok) lease.error = messages.storage;
    publish(lease);
  }
  async function freshSession(lease: Owner): Promise<LocalSession | null> {
    const reviewed = await readBusinessCommandAuthority(lease.scope.workspaceId, { expectedPrincipalId: lease.scope.principalId, signal: lease.abort.signal });
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
    catch { if (current(lease)) { lease.error = lease.storageError ? messages.storage : messages.refresh; publish(lease); } }
  }

  async function perform(value: NativeInboxIntent): Promise<boolean> {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || lease.storageError || lease.authorityDenied || unresolvedNativeInboxDraft(lease.draft)
      || authority === null || sessionFor(authority, lease.scope) === null) return false;
    const intent = freezeNativeInboxIntent(value);
    if (!intent) { lease.error = messages.changed; publish(lease); return false; }
    lease.busy = true; lease.pendingLabel = actionLabel(intent); lease.error = null; publish(lease);
    let posted = false;
    try {
      const session = await freshSession(lease);
      if (!current(lease) || session === null) return false;
      const detail = await getNotificationThread(lease.scope.workspaceId, intent.threadId, { signal: lease.abort.signal });
      if (!current(lease)) return false;
      if (!matchesFacts(detail, lease.scope, intent)) { lease.error = messages.changed; publish(lease); return false; }
      if (sessionFor({ kind: "product", session }, lease.scope) === null) {
        lease.authorityDenied = true; lease.error = messages.authority; publish(lease); return false;
      }
      const original = buildNativeInboxOriginal(lease.scope, intent, session);
      if (!current(lease)) return false;
      const pending: NativeInboxDraft = Object.freeze({ ...emptyNativeInboxDraft(lease.scope), intent, original, status: "pending" });
      const saved = saveNativeInboxDraft(lease.scope, pending);
      if (!saved.ok) {
        lease.storageError = saved.error; lease.storageOperation = "load"; lease.error = messages.storage; publish(lease); return false;
      }
      lease.draft = pending; publish(lease);
      if (!current(lease)) return false;
      posted = true;
      const response = await submitNativeCommand(original);
      if (!current(lease)) return false;
      const reviewed = reviewNativeInboxResult(pending, response);
      if (reviewed.outcome === null) lease.error = messages.lookup;
      persist(lease, reviewed);
      if (reviewed.status === "applied") await refresh(lease);
      if (!current(lease)) return false;
      return reviewed.outcome !== null;
    } catch {
      if (!current(lease)) return false;
      if (posted) {
        lease.error = messages.lookup;
        persist(lease, { ...lease.draft, status: "unknown", outcome: null, rejection_code: null });
      } else { lease.error = messages.source; publish(lease); }
      return false;
    } finally { if (current(lease)) { lease.busy = false; lease.pendingLabel = null; publish(lease); } }
  }

  async function lookupOriginal() {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || !lease.draft.original) return;
    const original = lease.draft.original;
    lease.busy = true; lease.error = null; publish(lease);
    try {
      const session = await freshSession(lease);
      if (!current(lease) || session === null) return;
      const journal = await lookupNativeCommand(original, lease.abort.signal);
      if (!current(lease)) return;
      const reviewed = reviewNativeInboxJournal(lease.draft, journal);
      if (!reviewed || (lease.draft.status === "applied" && reviewed.status === "rejected") || (lease.draft.status === "rejected" && reviewed.status === "applied")) {
        lease.error = messages.lookup; publish(lease); return;
      }
      const terminal = lease.draft.outcome !== null || lease.draft.status === "rejected";
      const next = terminal || (lease.draft.status === "applied" && reviewed.status === "pending") ? lease.draft : reviewed;
      if (next.status === "applied" && next.outcome === null) lease.error = messages.lookup;
      persist(lease, next);
      if (next.status === "applied" || next.status === "rejected") await refresh(lease);
      if (!current(lease)) return;
    } catch { if (current(lease)) { lease.error = messages.lookup; publish(lease); } }
    finally { if (current(lease)) { lease.busy = false; publish(lease); } }
  }
  function retryStorage() {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || !lease.storageError) return;
    if (lease.storageOperation === "load") {
      const loaded = loadNativeInboxDraft(lease.scope);
      if (!loaded.ok) { lease.storageError = loaded.error; lease.error = messages.storage; publish(lease); return; }
      lease.draft = loaded.draft;
    } else {
      const saved = saveNativeInboxDraft(lease.scope, lease.draft);
      if (!saved.ok) { lease.storageError = saved.error; lease.error = messages.storage; publish(lease); return; }
    }
    lease.storageError = null; lease.storageOperation = null; lease.error = null; publish(lease);
  }

  const key = scope === null ? null : nativeInboxDraftKey(scope);
  const visible = snapshot?.key === key ? snapshot : null;
  const authorized = visible !== null && authority !== null && expiresAt !== null && Number.isFinite(expiresAt) && expiresAt > now
    && sessionFor(authority, visible.draft.scope) !== null && !visible.authorityDenied;
  return { authority, authorized, state: visible?.draft ?? null, original: visible?.draft.original ?? null,
    error: visible?.error ?? null, storageError: visible?.storageError ?? null, busy: visible?.busy ?? false,
    pendingLabel: visible?.pendingLabel ?? (visible && unresolvedNativeInboxDraft(visible.draft) && visible.draft.intent ? actionLabel(visible.draft.intent) : null),
    canPerform: authorized && visible !== null && !visible.busy && !visible.storageError && !unresolvedNativeInboxDraft(visible.draft),
    perform, lookupOriginal, retryStorage,
    onSessionAuthorized: async (session: LocalSession) => {
      if (session.workspace_id !== workspaceId) return;
      const lease = owner.current;
      const result = await query.refetch();
      if (!lease || !current(lease)) return;
      if (!result.isSuccess || !result.data || sessionFor(result.data, lease.scope) === null) {
        lease.authorityDenied = true; lease.error = messages.authority; publish(lease); return;
      }
      lease.authorityDenied = false; lease.error = lease.storageError ? messages.storage : null; publish(lease);
    },
  };
}

function sessionFor(authority: BusinessCommandAuthority, scope: NativeInboxScope): LocalSession | null {
  return authority.kind === "product" && authority.session.workspace_id === scope.workspaceId && authority.session.principal_id === scope.principalId
    && Number.isFinite(Date.parse(authority.session.expires_at)) && Date.parse(authority.session.expires_at) > Date.now() ? authority.session : null;
}
function matchesFacts(detail: NotificationThreadDetail, scope: NativeInboxScope, intent: NativeInboxIntent): boolean {
  const thread = detail.thread;
  if (thread.workspace_id !== scope.workspaceId || thread.id !== intent.threadId || thread.version !== intent.threadVersion || thread.latest_fact_id !== intent.latestFactId) return false;
  if (intent.kind === "mark_read") return detail.available_commands.includes("mark_read");
  if (intent.kind === "archive") return detail.available_commands.includes("archive") && thread.business_status === "resolved" && thread.organization_status === "active";
  const selected = detail.deliveries.find(delivery => delivery.delivery_id === intent.deliveryId);
  return selected?.status === "exhausted" && selected.route_id === intent.routeId && selected.route_version === intent.routeVersion;
}
function actionLabel(intent: NativeInboxIntent): string {
  return intent.kind === "mark_read" ? "标记已读" : intent.kind === "archive" ? "归档线程" : "重新投递";
}
