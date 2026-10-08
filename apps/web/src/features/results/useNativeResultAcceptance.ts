import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { lookupNativeCommand, submitNativeCommand } from "@/features/assignments/commands";
import { readBusinessCommandAuthority, type BusinessCommandAuthority } from "@/features/local-session/businessAuthority";
import { ApiProblem } from "@/shared/api/client";

import { buildNativeTaskAcceptanceCommand, getTaskResultAcceptanceBasis, getTaskResults, type AcceptanceCriterionDecision, type ResultAction, type TaskResultOverview } from "./api";
import { loadNativeAcceptanceDraft, nativeAcceptanceDraftKey, nativeAcceptanceStatusMatches, reviewNativeAcceptanceResult, saveNativeAcceptanceDraft, type NativeAcceptanceDraft, type NativeAcceptanceReview, type NativeAcceptanceScope } from "./nativeResultAcceptance";

export interface UseNativeResultAcceptanceOptions {
  workspaceId: string;
  rootTaskId: string;
  view?: TaskResultOverview | undefined;
  onRefresh: () => void | Promise<void>;
}

type DraftEntry = {
  draft: NativeAcceptanceDraft | null;
  storageError: string | null;
  retryWrite: boolean;
  mappingAvailable: boolean;
};

const REVIEW_CHANGED = "成果已变化，请重新审阅";
const OUTCOME_UNKNOWN = "验收结果尚未确认，请查询原始验收";
const AUTH_UNAVAILABLE = "当前本地会话未获授权，请确认会话后重试";

export function useNativeResultAcceptance({ workspaceId, rootTaskId, view, onRefresh }: UseNativeResultAcceptanceOptions) {
  const queryClient = useQueryClient();
  const authorityKey = useMemo(() => ["native-result-acceptance-authority", workspaceId, rootTaskId] as const, [workspaceId, rootTaskId]);
  const authorityQuery = useQuery({
    queryKey: authorityKey,
    queryFn: ({ signal }) => readBusinessCommandAuthority(workspaceId, { signal }),
    enabled: Boolean(workspaceId && rootTaskId),
    retry: false,
  });
  const authority: BusinessCommandAuthority = authorityQuery.data ?? { kind: "unavailable", reason: "host_unavailable" };
  const session = authority.kind === "product" ? authority.session : null;
  const [now, setNow] = useState(Date.now);
  const expiresAt = session === null ? null : Date.parse(session.expires_at);
  useEffect(() => {
    if (expiresAt === null || expiresAt <= now) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.min(Math.max(0, expiresAt - Date.now()), 2_147_483_647));
    return () => clearTimeout(timer);
  }, [expiresAt, now]);
  const principalId = session?.principal_id ?? "";
  const scope = useMemo<NativeAcceptanceScope | null>(() => principalId === "" ? null : {
    workspace_id: workspaceId, root_task_id: rootTaskId, principal_id: principalId,
  }, [workspaceId, rootTaskId, principalId]);
  const scopeKey = scope === null ? null : nativeAcceptanceDraftKey(scope);
  const lease = useMemo(() => ({ key: scopeKey }), [scopeKey]);
  const activeLease = useRef<typeof lease | null>(null);
  const latestView = useRef(view);
  useLayoutEffect(() => {
    activeLease.current = lease;
    return () => { if (activeLease.current === lease) activeLease.current = null; };
  }, [lease]);
  useLayoutEffect(() => { latestView.current = view; }, [view]);

  const [entries, setEntries] = useState<Record<string, DraftEntry>>({});
  const entriesRef = useRef<Record<string, DraftEntry>>({});
  const [busyLease, setBusyLease] = useState<typeof lease | null>(null);
  const locks = useRef(new Set<string>());
  const [messages, setMessages] = useState<Record<string, string | null>>({});
  const entry = scopeKey === null ? undefined : entries[scopeKey];
  const draft = entry?.draft ?? null;
  const busy = busyLease === lease;
  const authorized = session !== null && session.workspace_id === workspaceId && expiresAt !== null && expiresAt > now;
  const intent = reviewIntent(view, workspaceId, rootTaskId);
  const canAccept = authorized && intent !== null && entry !== undefined && entry.storageError === null
    && (draft === null || draft.status === "rejected") && !busy;

  const putEntry = useCallback((key: string, value: DraftEntry) => {
    entriesRef.current = { ...entriesRef.current, [key]: value };
    setEntries(entriesRef.current);
  }, []);
  const message = (key: string, value: string | null) => setMessages(previous => ({ ...previous, [key]: value }));

  useEffect(() => {
    if (scope === null || scopeKey === null || entriesRef.current[scopeKey] !== undefined) return;
    const loaded = loadNativeAcceptanceDraft(scope);
    putEntry(scopeKey, {
      draft: loaded.ok ? loaded.draft : null,
      storageError: loaded.ok ? null : loaded.reason,
      retryWrite: false,
      mappingAvailable: false,
    });
  }, [scope, scopeKey, putEntry]);

  const onSessionAuthorized = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: authorityKey, exact: true });
  }, [queryClient, authorityKey]);

  function live(): boolean { return activeLease.current === lease; }

  function persist(next: NativeAcceptanceDraft, mappingAvailable: boolean, beforePost = false): boolean {
    if (!live()) return false;
    const key = nativeAcceptanceDraftKey(next.scope);
    const saved = saveNativeAcceptanceDraft(next.scope, next);
    putEntry(key, {
      draft: saved.ok || !beforePost ? next : entriesRef.current[key]?.draft ?? null,
      storageError: saved.ok ? null : saved.reason,
      retryWrite: !saved.ok && !beforePost,
      mappingAvailable,
    });
    return saved.ok;
  }

  function retryStorage() {
    if (!live() || scope === null || scopeKey === null || locks.current.has(scopeKey)) return;
    const current = entriesRef.current[scopeKey];
    if (current?.retryWrite && current.draft !== null) {
      persist(current.draft, current.mappingAvailable);
      return;
    }
    const loaded = loadNativeAcceptanceDraft(scope);
    putEntry(scopeKey, {
      draft: loaded.ok ? loaded.draft : current?.draft ?? null,
      storageError: loaded.ok ? null : loaded.reason,
      retryWrite: false,
      mappingAvailable: false,
    });
  }

  async function freshSession(captured: NativeAcceptanceScope) {
    const fresh = await readBusinessCommandAuthority(captured.workspace_id, { expectedPrincipalId: captured.principal_id });
    if (!live()) return null;
    queryClient.setQueryData(authorityKey, fresh);
    if (fresh.kind !== "product") return null;
    return fresh.session;
  }

  async function refreshOutcome(key: string) {
    if (!live()) return;
    try {
      await onRefresh();
      if (!live()) return;
    } catch {
      if (live()) message(key, "验收操作已确认，当前成果暂未刷新");
    }
  }

  async function submit(choices: AcceptanceCriterionDecision[]) {
    if (!live() || scope === null || scopeKey === null || !canAccept || locks.current.has(scopeKey)) return;
    const capturedScope = scope;
    const key = scopeKey;
    locks.current.add(key);
    setBusyLease(lease);
    message(key, null);
    try {
      const frozen = reviewIntent(view, workspaceId, rootTaskId);
      const criteria = normalizeCriteria(choices, view);
      if (frozen === null || criteria === null) { message(key, REVIEW_CHANGED); return; }
      const currentSession = await freshSession(capturedScope);
      if (!live()) return;
      if (currentSession === null) { message(key, AUTH_UNAVAILABLE); return; }
      const canonical = await getTaskResults(workspaceId, rootTaskId);
      if (!live()) return;
      if (!sameIntent(frozen, reviewIntent(canonical, workspaceId, rootTaskId))
        || !sameIntent(frozen, reviewIntent(latestView.current, workspaceId, rootTaskId))) {
        message(key, REVIEW_CHANGED);
        return;
      }
      const basis = await getTaskResultAcceptanceBasis(workspaceId, rootTaskId, frozen.review.candidate_id, criteria);
      if (!live()) return;
      if (!sameIntent(frozen, reviewIntent(latestView.current, workspaceId, rootTaskId))
        || basis.candidate_id !== frozen.review.candidate_id || basis.candidate_hash !== frozen.review.candidate_hash
        || basis.expected_result_version !== frozen.review.result_version
        || !/^[0-9a-f]{64}$/.test(basis.acceptance_basis_hash)) {
        message(key, REVIEW_CHANGED);
        return;
      }
      if (Date.parse(currentSession.expires_at) <= Date.now()) { message(key, AUTH_UNAVAILABLE); return; }
      const original = buildNativeTaskAcceptanceCommand(workspaceId, frozen.action, {
        candidate_id: frozen.review.candidate_id,
        candidate_hash: frozen.review.candidate_hash,
        acceptance_basis_hash: basis.acceptance_basis_hash,
        expected_result_version: frozen.review.result_version,
        criteria,
      }, currentSession);
      const pending: NativeAcceptanceDraft = { schema_version: 1, scope: capturedScope, review: frozen.review, original, status: "pending" };
      if (!persist(pending, false, true)) { message(key, "原始验收未能保存，因此没有提交"); return; }
      if (!live()) return;
      let response: unknown;
      try {
        response = await submitNativeCommand(original);
        if (!live()) return;
      } catch {
        if (!live()) return;
        persist({ ...pending, status: "unknown" }, false);
        message(key, OUTCOME_UNKNOWN);
        return;
      }
      const outcome = reviewNativeAcceptanceResult(pending, response);
      persist({ ...pending, status: outcome.status }, outcome.mappingAvailable);
      message(key, outcome.status === "unknown" ? OUTCOME_UNKNOWN
        : outcome.mappingAvailable ? null : "原始验收已处理，验收映射尚未确认");
      if (outcome.status === "applied") await refreshOutcome(key);
      if (!live()) return;
    } catch (cause) {
      if (live()) message(key, cause instanceof ApiProblem && cause.status === 409 ? REVIEW_CHANGED : "验收依据暂不可用，请重新读取成果");
    } finally {
      locks.current.delete(key);
      if (live()) setBusyLease(null);
    }
  }

  async function lookupOriginal() {
    if (!live() || !authorized || scope === null || scopeKey === null || locks.current.has(scopeKey)) return;
    const current = entriesRef.current[scopeKey];
    if (current?.draft === null || current?.draft === undefined) return;
    const frozen = current.draft;
    const key = scopeKey;
    locks.current.add(key);
    setBusyLease(lease);
    message(key, null);
    try {
      const currentSession = await freshSession(frozen.scope);
      if (!live()) return;
      if (currentSession === null) { message(key, AUTH_UNAVAILABLE); return; }
      const status = await lookupNativeCommand(frozen.original);
      if (!live()) return;
      if (!nativeAcceptanceStatusMatches(frozen, status)
        || ((frozen.status === "applied" || frozen.status === "rejected") && frozen.status !== status.status)) {
        message(key, OUTCOME_UNKNOWN);
        return;
      }
      const mappingAvailable = status.status === "applied"
        && reviewNativeAcceptanceResult(frozen, status.result).mappingAvailable;
      persist({ ...frozen, status: status.status }, mappingAvailable);
      message(key, status.status === "rejected" ? "原始验收已被拒绝，请重新审阅"
        : status.status === "pending" ? OUTCOME_UNKNOWN
          : mappingAvailable ? null : "原始验收已处理，验收映射尚未确认");
      if (status.status !== "pending") await refreshOutcome(key);
      if (!live()) return;
    } catch {
      if (live()) message(key, OUTCOME_UNKNOWN);
    } finally {
      locks.current.delete(key);
      if (live()) setBusyLease(null);
    }
  }

  return {
    authority,
    session,
    authorized,
    draft,
    original: draft?.original ?? null,
    status: draft?.status ?? "idle",
    mappingAvailable: entry?.mappingAvailable ?? false,
    busy,
    error: scopeKey === null ? null : messages[scopeKey] ?? null,
    storageError: entry?.storageError ?? null,
    canAccept,
    onSessionAuthorized,
    retryStorage,
    submit,
    lookupOriginal,
  };
}

type ReviewedIntent = { action: ResultAction; review: NativeAcceptanceReview };

function reviewIntent(view: TaskResultOverview | undefined, workspaceId: string, rootTaskId: string): ReviewedIntent | null {
  if (view === undefined || view.workspace_id !== workspaceId || view.root_task_id !== rootTaskId
    || view.acceptance !== null || view.current_candidate === null
    || !["pending", "ready", "running", "waiting"].includes(view.task_status)
    || !Number.isSafeInteger(view.task_version) || view.task_version < 1
    || !Number.isSafeInteger(view.result_version) || (view.result_version ?? 0) < 1) return null;
  const candidate = view.current_candidate;
  const action = view.available_actions.find(item => item.command_type === "CompleteTask");
  if (action === undefined || action.target_type !== "task" || action.target_id !== rootTaskId
    || action.expected_version !== view.task_version || action.expected_result_version !== view.result_version
    || action.candidate_id !== candidate.candidate_id || action.candidate_hash !== candidate.candidate_hash
    || action.plan_content_hash !== candidate.plan_content_hash
    || (action.expected_fingerprint !== null && action.expected_fingerprint !== candidate.candidate_hash)
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(candidate.candidate_id)
    || !/^[0-9a-f]{64}$/.test(candidate.candidate_hash) || !/^[0-9a-f]{64}$/.test(candidate.plan_content_hash)) return null;
  return {
    action: { ...action },
    review: {
      root_task_id: rootTaskId, task_version: view.task_version, task_status: view.task_status,
      result_version: view.result_version as number, candidate_id: candidate.candidate_id,
      candidate_hash: candidate.candidate_hash, plan_content_hash: candidate.plan_content_hash,
    },
  };
}

function sameIntent(left: ReviewedIntent, right: ReviewedIntent | null): boolean {
  return right !== null && left.review.root_task_id === right.review.root_task_id
    && left.review.task_version === right.review.task_version && left.review.task_status === right.review.task_status
    && left.review.result_version === right.review.result_version && left.review.candidate_id === right.review.candidate_id
    && left.review.candidate_hash === right.review.candidate_hash && left.review.plan_content_hash === right.review.plan_content_hash
    && left.action.expected_fingerprint === right.action.expected_fingerprint;
}

function normalizeCriteria(choices: AcceptanceCriterionDecision[], view: TaskResultOverview | undefined): AcceptanceCriterionDecision[] | null {
  if (view === undefined || choices.length === 0 || choices.length !== view.criteria.length
    || new Set(choices.map(choice => choice.criterion_key)).size !== choices.length
    || choices.some(choice => !view.criteria.some(criterion => criterion.criterion_key === choice.criterion_key)
      || !["accept_passed", "manual_passed", "exception_accepted"].includes(choice.decision)
      || (choice.reason !== null && typeof choice.reason !== "string")
      || (choice.decision === "exception_accepted" && (choice.reason ?? "").trim().length === 0))) return null;
  const criteria = choices.map(choice => Object.freeze({
    criterion_key: choice.criterion_key, decision: choice.decision, reason: choice.reason?.trim() || null,
  }));
  Object.freeze(criteria);
  return criteria;
}
