import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { buildNativeCommand, lookupNativeCommand, submitNativeCommand } from "../assignments/commands";
import type { GovernanceAction } from "../collaboration/types";
import type { LocalSession } from "../local-session/api";
import { readBusinessCommandAuthority, type BusinessCommandAuthority } from "../local-session/businessAuthority";
import { getWorkerItemWorkspace } from "../worker-items/api";
import type { WorkerWorkspace } from "../worker-items/types";
import {
  getWorkerResultInputs, resultInputActionPayload, resultInputActions, retryContextAssemblyAction,
  type ResultInputAction, type WorkerResultInputsView,
} from "./api";
import {
  emptyNativeWorkerResultActionDraft, freezeWorkerResultActionReview, loadNativeWorkerResultActionDraft,
  nativeWorkerResultActionDraftKey, reviewWorkerGovernanceAction, reviewWorkerResultActionResult,
  sameWorkerResultActionPayload, saveNativeWorkerResultActionDraft, workerResultActionFingerprint,
  type NativeWorkerResultActionDraft, type WorkerResultActionReview,
  type WorkerResultActionScope, type WorkerResultStorageError,
} from "./nativeWorkerResultActions";

type OwnerLease = {
  active: boolean;
  abort: AbortController;
  key: string;
  scope: WorkerResultActionScope;
  draft: NativeWorkerResultActionDraft;
  busy: boolean;
  error: string | null;
  storageError: WorkerResultStorageError | null;
  storageOperation: "load" | "save" | null;
  authorityDenied: boolean;
};
type Snapshot = Pick<OwnerLease, "key" | "draft" | "busy" | "error" | "storageError" | "authorityDenied">;
const messages = {
  authority: "当前操作身份未确认，请重新授权本地会话。",
  changed: "操作依据已变化，请重新打开并确认当前操作。",
  source: "当前操作依据暂不可用，请重新读取。",
  storage: "本地操作记录未能确认，请重试读取或保存。",
  lookup: "原操作结果暂未确认，请稍后再次查询。",
  refresh: "操作已处理，当前事实暂未刷新。",
};

export function useNativeWorkerResultActions({ workspaceId, workerItemId, workspace, resultInputs, onRefresh }: {
  workspaceId: string;
  workerItemId: string;
  workspace: WorkerWorkspace;
  resultInputs: WorkerResultInputsView | undefined;
  onRefresh: () => Promise<void>;
}) {
  const authorityQuery = useQuery({
    queryKey: ["native-worker-result-authority", workspaceId, workerItemId],
    queryFn: ({ signal }) => readBusinessCommandAuthority(workspaceId, { signal }),
    enabled: Boolean(workspaceId && workerItemId), retry: false,
  });
  const authority = authorityQuery.data ?? null;
  const [now, setNow] = useState(Date.now);
  const expiresAt = authority?.kind === "product" ? Date.parse(authority.session.expires_at) : null;
  useEffect(() => {
    if (expiresAt === null || !Number.isFinite(expiresAt) || expiresAt <= now) return;
    const timer = setTimeout(() => setNow(Date.now()),
      Math.min(Math.max(0, expiresAt - Date.now()), 2_147_483_647));
    return () => clearTimeout(timer);
  }, [expiresAt, now]);
  const principal = authority?.kind === "product" ? authority.session.principal_id : "";
  const rootTaskId = workspace.worker_item.root_task_id;
  const sourceWorkspaceId = workspace.workspace_id;
  const sourceWorkerItemId = workspace.worker_item.id;
  const scope = useMemo<WorkerResultActionScope | null>(() => principal.length > 0
    && sourceWorkspaceId === workspaceId && sourceWorkerItemId === workerItemId ? {
      workspace_id: workspaceId, root_task_id: rootTaskId, worker_item_id: workerItemId, principal_id: principal,
    } : null, [principal, rootTaskId, sourceWorkerItemId, sourceWorkspaceId, workerItemId, workspaceId]);
  const owner = useRef<OwnerLease | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const current = useCallback((lease: OwnerLease) => lease.active && owner.current === lease, []);
  const publish = useCallback((lease: OwnerLease) => {
    if (current(lease)) setSnapshot({
      key: lease.key, draft: lease.draft, busy: lease.busy, error: lease.error,
      storageError: lease.storageError, authorityDenied: lease.authorityDenied,
    });
  }, [current]);

  useLayoutEffect(() => {
    if (scope === null) { owner.current = null; return; }
    const loaded = loadNativeWorkerResultActionDraft(scope);
    const lease: OwnerLease = {
      active: true, abort: new AbortController(), key: nativeWorkerResultActionDraftKey(scope), scope,
      draft: loaded.ok ? loaded.draft : emptyNativeWorkerResultActionDraft(scope), busy: false,
      error: loaded.ok ? null : messages.storage, storageError: loaded.ok ? null : loaded.error,
      storageOperation: loaded.ok ? null : "load", authorityDenied: false,
    };
    owner.current = lease;
    queueMicrotask(() => publish(lease));
    return () => { lease.active = false; lease.abort.abort(); if (owner.current === lease) owner.current = null; };
  }, [publish, scope]);

  useLayoutEffect(() => {
    const lease = owner.current;
    if (lease && authority && sessionFor(authority, lease.scope) && lease.authorityDenied) {
      lease.authorityDenied = false; lease.error = lease.storageError ? messages.storage : null;
      queueMicrotask(() => publish(lease));
    }
  }, [authority, publish]);

  function persist(lease: OwnerLease, draft: NativeWorkerResultActionDraft) {
    if (!current(lease)) return;
    lease.draft = draft;
    const saved = saveNativeWorkerResultActionDraft(lease.scope, draft);
    lease.storageError = saved.ok ? null : saved.error;
    lease.storageOperation = saved.ok ? null : "save";
    if (!saved.ok) lease.error = messages.storage;
    publish(lease);
  }

  async function refresh(lease: OwnerLease) {
    if (!current(lease)) return;
    try {
      await onRefresh();
      if (!current(lease)) return;
    } catch {
      if (!current(lease)) return;
      lease.error = lease.storageError ? messages.storage : messages.refresh; publish(lease);
    }
  }

  async function freshSession(lease: OwnerLease): Promise<LocalSession | null> {
    const reviewed = await readBusinessCommandAuthority(lease.scope.workspace_id, {
      expectedPrincipalId: lease.scope.principal_id, signal: lease.abort.signal,
    });
    if (!current(lease)) return null;
    const session = sessionFor(reviewed, lease.scope);
    if (session === null) { lease.authorityDenied = true; lease.error = messages.authority; publish(lease); }
    return session;
  }

  async function submit(review: WorkerResultActionReview | null, entry: "governance" | "result-input"): Promise<boolean> {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || lease.storageError || lease.authorityDenied || unresolved(lease.draft)) return false;
    if (review === null) { lease.error = messages.changed; publish(lease); return false; }
    if (entry === "result-input" && !matchingInputReview(resultInputs, lease.scope, review)) {
      lease.error = messages.changed; publish(lease); return false;
    }
    lease.busy = true; lease.error = null; publish(lease);
    let posted = false;
    try {
      const session = await freshSession(lease);
      if (!current(lease) || session === null) return false;
      const freshWorkspace = await getWorkerItemWorkspace(lease.scope.workspace_id, lease.scope.worker_item_id, lease.abort.signal);
      if (!current(lease)) return false;
      if (!matchingWorkspace(freshWorkspace, lease.scope)) { lease.error = messages.changed; publish(lease); return false; }
      if (entry === "governance") {
        const canonical = reviewWorkerGovernanceAction(freshWorkspace, review.action.action as GovernanceAction);
        if (!canonical || workerResultActionFingerprint({ action: canonical, reason: review.reason }) !== workerResultActionFingerprint(review)) {
          lease.error = messages.changed; publish(lease); return false;
        }
      } else {
        const freshInputs = await getWorkerResultInputs(lease.scope.workspace_id, lease.scope.worker_item_id, { signal: lease.abort.signal });
        if (!current(lease)) return false;
        if (!matchingInputReview(freshInputs, lease.scope, review)) { lease.error = messages.changed; publish(lease); return false; }
      }
      if (!current(lease) || sessionFor({ kind: "product", session }, lease.scope) === null) {
        if (current(lease)) { lease.authorityDenied = true; lease.error = messages.authority; publish(lease); }
        return false;
      }
      const original = buildNativeCommand({
        session, workspaceId: lease.scope.workspace_id, commandType: review.action.command_type,
        targetType: review.action.target_type as Parameters<typeof buildNativeCommand>[0]["targetType"],
        targetId: review.action.target_id, expectedVersion: review.action.expected_version,
        payload: resultInputActionPayload(review.action, review.reason),
      });
      const pending: NativeWorkerResultActionDraft = {
        ...emptyNativeWorkerResultActionDraft(lease.scope), review, original, status: "pending",
      };
      const saved = saveNativeWorkerResultActionDraft(lease.scope, pending);
      if (!saved.ok) {
        lease.storageError = saved.error; lease.storageOperation = "save"; lease.error = messages.storage; publish(lease);
        return false;
      }
      lease.draft = pending; publish(lease);
      posted = true;
      const response = await submitNativeCommand(original);
      if (!current(lease)) return false;
      const result = reviewWorkerResultActionResult(original, response);
      persist(lease, { ...pending, ...result });
      if (result.status === "applied") {
        await refresh(lease);
        if (!current(lease)) return false;
      }
      return true;
    } catch {
      if (!current(lease)) return false;
      if (posted) {
        lease.error = messages.lookup;
        persist(lease, { ...lease.draft, status: "unknown", aggregate_id: null, aggregate_version: null, outcome: null });
      } else { lease.error = messages.source; publish(lease); }
      return posted;
    } finally {
      if (current(lease)) { lease.busy = false; publish(lease); }
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
      if (journal.payload !== undefined && !sameWorkerResultActionPayload(journal.payload, original.payload)) {
        lease.error = messages.lookup; publish(lease); return;
      }
      let next = lease.draft;
      if (journal.status === "applied") {
        const result = journal.result == null ? {
          status: "applied" as const, aggregate_id: null, aggregate_version: null, outcome: null,
        } : reviewWorkerResultActionResult(original, journal.result);
        if (result.status !== "applied" || lease.draft.status === "rejected"
          || (lease.draft.aggregate_id !== null && result.aggregate_id !== null
            && (lease.draft.aggregate_id !== result.aggregate_id || lease.draft.aggregate_version !== result.aggregate_version
              || lease.draft.outcome !== result.outcome))) {
          lease.error = messages.lookup; publish(lease); return;
        }
        next = lease.draft.outcome === null ? { ...next, ...result } : next;
      } else if (journal.status === "rejected" && next.status !== "applied") {
        next = { ...next, status: "rejected", aggregate_id: null, aggregate_version: null, outcome: null, rejection_code: "command_rejected" };
      } else if (journal.status === "pending" && !["applied", "rejected"].includes(next.status)) {
        next = { ...next, status: "pending" };
      } else if (!["pending", "applied", "rejected"].includes(journal.status)) {
        lease.error = messages.lookup; publish(lease); return;
      }
      persist(lease, next);
      if (["applied", "rejected"].includes(next.status)) {
        await refresh(lease);
        if (!current(lease)) return;
      }
    } catch {
      if (current(lease)) { lease.error = messages.lookup; publish(lease); }
    } finally {
      if (current(lease)) { lease.busy = false; publish(lease); }
    }
  }

  function retryStorage() {
    const lease = owner.current;
    if (!lease || !current(lease) || lease.busy || !lease.storageError) return;
    if (lease.storageOperation === "load") {
      const loaded = loadNativeWorkerResultActionDraft(lease.scope);
      if (!loaded.ok) { lease.storageError = loaded.error; lease.error = messages.storage; publish(lease); return; }
      lease.draft = loaded.draft;
    } else {
      const saved = saveNativeWorkerResultActionDraft(lease.scope, lease.draft);
      if (!saved.ok) { lease.storageError = saved.error; lease.error = messages.storage; publish(lease); return; }
    }
    lease.storageError = null; lease.storageOperation = null; lease.error = null; publish(lease);
  }

  const key = scope === null ? null : nativeWorkerResultActionDraftKey(scope);
  const visible = snapshot?.key === key ? snapshot : null;
  const authorized = visible !== null && authority !== null && expiresAt !== null && Number.isFinite(expiresAt)
    && expiresAt > now && sessionFor(authority, visible.draft.scope) !== null && !visible.authorityDenied;
  return {
    authority, authorized, state: visible?.draft ?? null, error: visible?.error ?? null,
    storageError: visible?.storageError ?? null, busy: visible?.busy ?? false,
    canSubmit: authorized && visible !== null && !visible.busy && !visible.storageError && !unresolved(visible.draft),
    onAction: (action: ResultInputAction, reason: string | null = null) => submit(freezeWorkerResultActionReview(action, reason), "result-input"),
    onGovernanceAction: (action: GovernanceAction, reason: string) => submit(
      (() => { const descriptor = reviewWorkerGovernanceAction(workspace, action); return descriptor ? freezeWorkerResultActionReview(descriptor, reason) : null; })(), "governance",
    ),
    lookupOriginal, retryStorage,
    onSessionAuthorized: (session: LocalSession) => { if (session.workspace_id === workspaceId) void authorityQuery.refetch(); },
  };
}

function sessionFor(authority: BusinessCommandAuthority, scope: WorkerResultActionScope): LocalSession | null {
  return authority.kind === "product" && authority.session.workspace_id === scope.workspace_id
    && authority.session.principal_id === scope.principal_id && Date.parse(authority.session.expires_at) > Date.now()
    ? authority.session : null;
}

function unresolved(draft: NativeWorkerResultActionDraft): boolean {
  return draft.status === "pending" || draft.status === "unknown" || (draft.status === "applied" && draft.outcome === null);
}

function matchingWorkspace(workspace: WorkerWorkspace, scope: WorkerResultActionScope): boolean {
  return workspace.workspace_id === scope.workspace_id && workspace.worker_item.id === scope.worker_item_id
    && workspace.worker_item.root_task_id === scope.root_task_id;
}

function matchingInputReview(view: WorkerResultInputsView | undefined, scope: WorkerResultActionScope, review: WorkerResultActionReview): boolean {
  if (!view || view.is_historical || view.workspace_id !== scope.workspace_id || view.root_task_id !== scope.root_task_id
    || view.worker_item_id !== scope.worker_item_id) return false;
  if (review.action.command_type === "RetryContextAssembly") {
    const retry = retryContextAssemblyAction(view);
    if (view.delivery.consumer_type !== "worker_item" || view.delivery.consumer_id !== scope.worker_item_id
      || retry === null || workerResultActionFingerprint({ action: retry, reason: review.reason }) !== workerResultActionFingerprint(review)) return false;
  }
  return resultInputActions(view).some(action => workerResultActionFingerprint({ action, reason: review.reason }) === workerResultActionFingerprint(review));
}
