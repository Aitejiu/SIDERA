import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { lookupNativeCommand, submitNativeCommand } from "@/features/assignments/commands";
import { readBusinessCommandAuthority, type BusinessCommandAuthority } from "@/features/local-session/businessAuthority";
import { getWorkerItemWorkspace, workerWorkspaceQueryKey } from "@/features/worker-items/api";

import { buildNativeResultOperationCommand, getTaskResults, taskResultsQueryKey, type NativeResultOperationInput, type ResultAction, type TaskResultOverview } from "./api";
import { loadNativeResultOperationDraft, nativeResultOperationDraftKey, nativeResultOperationStatusMatches,
  reviewNativeResultOperationResult, saveNativeResultOperationDraft, type NativeResultOperationDraft,
  type NativeResultOperationReview, type NativeResultOperationScope } from "./nativeResultOperations";

export interface UseNativeResultOperationsOptions {
  workspaceId: string;
  rootTaskId: string;
  workerItemId: string;
  view: TaskResultOverview | undefined;
  canOperateResult: boolean;
  onRefresh: () => void | Promise<void>;
}

type Outcome = ReturnType<typeof reviewNativeResultOperationResult>["outcome"];
type DraftEntry = { draft: NativeResultOperationDraft | null; storageError: string | null;
  retryWrite: boolean; mappingAvailable: boolean; outcome: Outcome };

const REVIEW_CHANGED = "结果操作已变化，请重新审阅";
const OUTCOME_UNKNOWN = "结果操作尚未确认，请查询原始操作";
const AUTH_UNAVAILABLE = "当前本地会话未获授权，请确认会话后重试";
const MAPPING_UNAVAILABLE = "原始结果操作已处理，操作映射尚未确认";

export function useNativeResultOperations({ workspaceId, rootTaskId, workerItemId, view, canOperateResult, onRefresh }: UseNativeResultOperationsOptions) {
  const queryClient = useQueryClient();
  const authorityKey = useMemo(() => ["native-result-operation-authority", workspaceId, rootTaskId, workerItemId] as const,
    [workspaceId, rootTaskId, workerItemId]);
  const query = useQuery({ queryKey: authorityKey,
    queryFn: ({ signal }) => readBusinessCommandAuthority(workspaceId, { signal }),
    enabled: Boolean(workspaceId && rootTaskId && workerItemId), retry: false });
  const authority: BusinessCommandAuthority = query.data ?? { kind: "unavailable", reason: "host_unavailable" };
  const session = authority.kind === "product" ? authority.session : null;
  const principalId = session?.principal_id ?? "";
  const [now, setNow] = useState(Date.now);
  const expiresAt = session === null ? null : Date.parse(session.expires_at);
  useEffect(() => {
    if (expiresAt === null || expiresAt <= now) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.min(Math.max(0, expiresAt - Date.now()), 2_147_483_647));
    return () => clearTimeout(timer);
  }, [expiresAt, now]);
  const authorized = session !== null && session.workspace_id === workspaceId && expiresAt !== null && expiresAt > now;
  const scope = useMemo<NativeResultOperationScope | null>(() => principalId === "" ? null : {
    workspace_id: workspaceId, root_task_id: rootTaskId, worker_item_id: workerItemId, principal_id: principalId,
  }, [workspaceId, rootTaskId, workerItemId, principalId]);
  const scopeKey = scope === null ? null : nativeResultOperationDraftKey(scope);
  const ownerKey = scopeKey ?? JSON.stringify([workspaceId, rootTaskId, workerItemId]);
  const lease = useMemo(() => ({ key: ownerKey }), [ownerKey]);
  const activeLease = useRef<typeof lease | null>(null);
  const latest = useRef({ view, canOperateResult });
  useLayoutEffect(() => {
    activeLease.current = lease;
    return () => { if (activeLease.current === lease) activeLease.current = null; };
  }, [lease]);
  useLayoutEffect(() => { latest.current = { view, canOperateResult }; }, [view, canOperateResult]);

  const [entries, setEntries] = useState<Record<string, DraftEntry>>({});
  const entriesRef = useRef<Record<string, DraftEntry>>({});
  const [busyLease, setBusyLease] = useState<typeof lease | null>(null);
  const locks = useRef(new Set<string>());
  const [messages, setMessages] = useState<Record<string, string | null>>({});
  const entry = scopeKey === null ? undefined : entries[scopeKey];
  const draft = entry?.draft ?? null;
  const busy = busyLease === lease;
  const canSubmit = authorized && canOperateResult && view?.workspace_id === workspaceId && view.root_task_id === rootTaskId
    && entry !== undefined && entry.storageError === null && !busy
    && (draft === null || draft.status === "rejected" || (draft.status === "applied" && entry.mappingAvailable));
  const putEntry = useCallback((key: string, value: DraftEntry) => {
    entriesRef.current = { ...entriesRef.current, [key]: value };
    setEntries(entriesRef.current);
  }, []);
  const message = (key: string, value: string | null) => setMessages(previous => ({ ...previous, [key]: value }));
  function live(): boolean { return activeLease.current === lease; }

  useEffect(() => {
    if (scope === null || scopeKey === null || entriesRef.current[scopeKey] !== undefined) return;
    const loaded = loadNativeResultOperationDraft(scope);
    putEntry(scopeKey, { draft: loaded.ok ? loaded.draft : null, storageError: loaded.ok ? null : loaded.reason,
      retryWrite: false, mappingAvailable: false, outcome: null });
  }, [scope, scopeKey, putEntry]);

  const onSessionAuthorized = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: authorityKey, exact: true });
  }, [queryClient, authorityKey]);

  function persist(next: NativeResultOperationDraft, mappingAvailable: boolean, outcome: Outcome, beforePost = false): boolean {
    if (!live()) return false;
    const key = nativeResultOperationDraftKey(next.scope);
    const saved = saveNativeResultOperationDraft(next.scope, next);
    const previous = entriesRef.current[key];
    putEntry(key, { draft: saved.ok || !beforePost ? next : previous?.draft ?? null,
      storageError: saved.ok ? null : saved.reason, retryWrite: !saved.ok && !beforePost,
      mappingAvailable: saved.ok || !beforePost ? mappingAvailable : previous?.mappingAvailable ?? false,
      outcome: saved.ok || !beforePost ? outcome : previous?.outcome ?? null });
    return saved.ok;
  }

  function retryStorage() {
    if (!live() || scope === null || scopeKey === null || locks.current.has(scopeKey)) return;
    const current = entriesRef.current[scopeKey];
    if (current?.retryWrite && current.draft !== null) {
      persist(current.draft, current.mappingAvailable, current.outcome);
      return;
    }
    const loaded = loadNativeResultOperationDraft(scope);
    const retainOutcome = !loaded.ok || (loaded.draft !== null && current?.draft !== undefined && current.draft !== null
      && sameReview(loaded.draft.review, current.draft.review)
      && JSON.stringify(loaded.draft.original) === JSON.stringify(current.draft.original));
    putEntry(scopeKey, { draft: loaded.ok ? loaded.draft : current?.draft ?? null,
      storageError: loaded.ok ? null : loaded.reason, retryWrite: false,
      mappingAvailable: retainOutcome && (current?.mappingAvailable ?? false), outcome: retainOutcome ? current?.outcome ?? null : null });
  }

  async function freshSession(captured: NativeResultOperationScope) {
    const fresh = await readBusinessCommandAuthority(captured.workspace_id, { expectedPrincipalId: captured.principal_id });
    if (!live()) return null;
    queryClient.setQueryData(authorityKey, fresh);
    return fresh.kind === "product" ? fresh.session : null;
  }

  async function refreshOutcome(key: string) {
    if (!live()) return;
    try {
      await queryClient.invalidateQueries({ queryKey: taskResultsQueryKey(workspaceId, rootTaskId), exact: true });
      if (!live()) return;
      await queryClient.invalidateQueries({ queryKey: workerWorkspaceQueryKey(workspaceId, workerItemId), exact: true });
      if (!live()) return;
      await onRefresh();
      if (!live()) return;
    } catch {
      if (live()) message(key, "结果操作已确认，当前成果暂未刷新");
    }
  }

  async function submit(action: ResultAction, input: NativeResultOperationInput) {
    if (!live() || scope === null || scopeKey === null || !canSubmit || locks.current.has(scopeKey)) return;
    const capturedScope = scope;
    const key = scopeKey;
    locks.current.add(key); setBusyLease(lease); message(key, null);
    try {
      const review = snapshotReview(action);
      const frozenInput = JSON.parse(JSON.stringify(input)) as NativeResultOperationInput;
      const previous = entriesRef.current[key];
      if (!actionMatchesView(review, view, workspaceId, rootTaskId)
        || (previous?.draft?.status === "applied" && sameReview(previous.draft.review, review))) {
        message(key, REVIEW_CHANGED); return;
      }
      const currentSession = await freshSession(capturedScope);
      if (!live()) return;
      if (currentSession === null) { message(key, AUTH_UNAVAILABLE); return; }
      const canonical = await getTaskResults(workspaceId, rootTaskId);
      if (!live()) return;
      if (!actionMatchesView(review, canonical, workspaceId, rootTaskId)
        || !actionMatchesView(review, latest.current.view, workspaceId, rootTaskId)) {
        message(key, REVIEW_CHANGED); return;
      }
      const worker = await getWorkerItemWorkspace(workspaceId, workerItemId);
      if (!live()) return;
      if (worker.workspace_id !== workspaceId || worker.worker_item.id !== workerItemId
        || worker.worker_item.root_task_id !== rootTaskId || ["retired", "paused"].includes(worker.worker_item.status)
        || ["context_not_acknowledged", "projection_stale"].includes(worker.read_only_reason ?? "")
        || !latest.current.canOperateResult || !actionMatchesView(review, latest.current.view, workspaceId, rootTaskId)) {
        message(key, REVIEW_CHANGED); return;
      }
      if (Date.parse(currentSession.expires_at) <= Date.now()) { message(key, AUTH_UNAVAILABLE); return; }
      const original = buildNativeResultOperationCommand(workspaceId, rootTaskId, workerItemId,
        { ...review, action: action.action }, frozenInput, currentSession);
      const pending: NativeResultOperationDraft = { schema_version: 1, scope: capturedScope, review, original, status: "pending" };
      if (!persist(pending, false, null, true)) { message(key, "原始结果操作未能保存，因此没有提交"); return; }
      if (!live()) return;
      let response: unknown;
      try {
        response = await submitNativeCommand(original);
        if (!live()) return;
      } catch {
        if (!live()) return;
        persist({ ...pending, status: "unknown" }, false, null); message(key, OUTCOME_UNKNOWN); return;
      }
      const outcome = reviewNativeResultOperationResult(pending, response);
      persist({ ...pending, status: outcome.status }, outcome.mappingAvailable, outcome.outcome);
      message(key, outcome.status === "unknown" ? OUTCOME_UNKNOWN : outcome.mappingAvailable ? null : MAPPING_UNAVAILABLE);
      if (outcome.status === "applied") await refreshOutcome(key);
      if (!live()) return;
      return outcome.status === "applied";
    } catch {
      if (live()) message(key, REVIEW_CHANGED);
    } finally {
      locks.current.delete(key);
      if (live()) setBusyLease(null);
    }
  }

  async function lookupOriginal() {
    if (!live() || !authorized || scope === null || scopeKey === null || locks.current.has(scopeKey)) return;
    const current = entriesRef.current[scopeKey];
    if (current?.draft === undefined || current.draft === null) return;
    const frozen = current.draft;
    const key = scopeKey;
    locks.current.add(key); setBusyLease(lease); message(key, null);
    try {
      const currentSession = await freshSession(frozen.scope);
      if (!live()) return;
      if (currentSession === null) { message(key, AUTH_UNAVAILABLE); return; }
      const status = await lookupNativeCommand(frozen.original);
      if (!live()) return;
      if (!nativeResultOperationStatusMatches(frozen, status)
        || ((frozen.status === "applied" || frozen.status === "rejected") && frozen.status !== status.status)) {
        message(key, OUTCOME_UNKNOWN); return;
      }
      const outcome = status.status === "applied" ? reviewNativeResultOperationResult(frozen, status.result) : null;
      const mappingAvailable = outcome?.mappingAvailable === true || (status.status === "applied" && current.mappingAvailable);
      persist({ ...frozen, status: status.status }, mappingAvailable, outcome?.outcome ?? (status.status === "applied" ? current.outcome : null));
      message(key, status.status === "rejected" ? "原始结果操作已被拒绝，请重新审阅"
        : status.status === "pending" ? OUTCOME_UNKNOWN : mappingAvailable ? null : MAPPING_UNAVAILABLE);
      if (status.status !== "pending") await refreshOutcome(key);
      if (!live()) return;
    } catch {
      if (live()) message(key, OUTCOME_UNKNOWN);
    } finally {
      locks.current.delete(key);
      if (live()) setBusyLease(null);
    }
  }

  return { authority, session, authorized, canSubmit, draft, original: draft?.original ?? null,
    status: draft?.status ?? "idle", mappingAvailable: entry?.mappingAvailable ?? false, outcome: entry?.outcome ?? null,
    busy, error: scopeKey === null ? authority.kind === "unavailable" ? AUTH_UNAVAILABLE : null : messages[scopeKey] ?? (!authorized ? AUTH_UNAVAILABLE : null),
    storageError: entry?.storageError ?? null, submit, lookupOriginal, retryStorage, onSessionAuthorized };
}

function snapshotReview(action: ResultAction): NativeResultOperationReview {
  return Object.freeze({ command_type: action.command_type, target_type: action.target_type, target_id: action.target_id,
    expected_version: action.expected_version, expected_result_version: action.expected_result_version,
    candidate_id: action.candidate_id, candidate_hash: action.candidate_hash, plan_content_hash: action.plan_content_hash,
    expected_fingerprint: action.expected_fingerprint, requires_reason: action.requires_reason,
    allowed_worker_item_ids: Object.freeze([...action.allowed_worker_item_ids]) as string[],
    replacement_result_ids: Object.freeze([...action.replacement_result_ids]) as string[] });
}

function sameReview(left: NativeResultOperationReview, right: NativeResultOperationReview): boolean {
  return (Object.keys(left) as (keyof NativeResultOperationReview)[]).every(key => {
    const a = left[key]; const b = right[key];
    return Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((item, index) => item === b[index]) : a === b;
  });
}

function actionMatchesView(review: NativeResultOperationReview, view: TaskResultOverview | undefined, workspaceId: string, rootTaskId: string): boolean {
  if (view === undefined || view.workspace_id !== workspaceId || view.root_task_id !== rootTaskId
    || view.result_version !== review.expected_result_version
    || !view.available_actions.some(action => sameReview(review, snapshotReview(action)))) return false;
  if (review.candidate_id === null && review.candidate_hash === null) return review.command_type === "AuthorizeAdditionalRework";
  const candidate = view.current_candidate;
  return candidate !== null && candidate.candidate_id === review.candidate_id && candidate.candidate_hash === review.candidate_hash
    && (review.plan_content_hash === null || candidate.plan_content_hash === review.plan_content_hash)
    && (review.expected_fingerprint === null || candidate.candidate_hash === review.expected_fingerprint);
}
