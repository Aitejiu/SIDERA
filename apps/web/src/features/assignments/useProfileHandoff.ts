import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { listProfiles, readProfile } from "@/features/agents/api";
import type { ProfileView } from "@/features/agents/types";
import { readLocalSession, type LocalSession } from "@/features/local-session/api";
import {
  getHandoffProposal,
  getHandoffTransition,
  HandoffReadUnavailableError,
  workerWorkspaceQueryKey,
} from "@/features/worker-items/api";
import {
  isNativeHandoffProposal,
  isNativeHandoffTransition,
  type NativeHandoffProposal,
  type NativeHandoffTransition,
} from "@/features/worker-items/types";
import {
  assignmentQueryKey,
  nativeWorkerContextQueryKey,
  readAssignment,
  readNativeWorkerContext,
} from "./api";
import { buildNativeCommand, lookupNativeCommand, submitNativeCommand } from "./commands";
import {
  handoffDraftKey,
  loadHandoffDraft,
  mapHandoffActionResult,
  reviewHandoffSource,
  reviewPreparedHandoff,
  sameHandoffSourceReview,
  samePreparedHandoff,
  saveHandoffDraft,
  type HandoffActionName,
  type HandoffActionState,
  type HandoffDraft,
  type HandoffScope,
  type HandoffSourceReview,
  type HandoffSourceReviewResult,
} from "./profileHandoff";
import type {
  AgentBindingView,
  AssigneeValue,
  CommandRequest,
  CommandStatusResponse,
  NativeWorkerContextView,
} from "./types";
import type { WorkerWorkspace } from "@/features/worker-items/types";

export interface UseProfileHandoffOptions {
  workspace: WorkerWorkspace;
  initialTarget: AssigneeValue | null;
  onRefresh: () => Promise<void>;
}

interface DraftEntry {
  loaded: boolean;
  draft: HandoffDraft | null;
  storageError: string | null;
  storageFailure: "read" | "write" | null;
}

const IDLE_ACTION: HandoffActionState = {
  original: null,
  status: "idle",
  rejectionCode: null,
};
const SAFE_REJECTIONS = new Set([
  "assignment_forbidden",
  "handoff_required",
  "not_found",
  "permission_denied",
  "validation_error",
  "version_conflict",
  "unknown",
]);
const REJECTION_MESSAGES: Record<string, string> = {
  assignment_forbidden: "当前会话不能执行此交接",
  handoff_required: "当前来源仍需要完成交接复核",
  not_found: "交接对象已不可用",
  permission_denied: "当前会话无权执行此交接",
  validation_error: "交接信息未通过验证",
  version_conflict: "交接事实已变化，请重新读取",
  unknown: "原始交接请求被拒绝，请重新读取事实",
};
const TERMINAL_PROPOSAL_STATUSES = new Set(["cancelled", "expired", "stale"]);

export function useProfileHandoff({ workspace, initialTarget, onRefresh }: UseProfileHandoffOptions) {
  const queryClient = useQueryClient();
  const workspaceId = workspace.workspace_id;
  const workerItemId = workspace.worker_item.id;
  const sessionQueryKey = useMemo(
    () => ["profile-handoff-session", workspaceId, workerItemId] as const,
    [workspaceId, workerItemId],
  );
  const sessionQuery = useQuery({
    queryKey: sessionQueryKey,
    queryFn: ({ signal }) => readLocalSession(signal),
    retry: false,
  });
  const session = sessionQuery.data ?? null;
  const scope = useMemo<HandoffScope | null>(() => {
    if (session === null || session.workspace_id !== workspaceId
      || session.session_id.trim().length === 0 || session.principal_id.trim().length === 0
      || workerItemId.trim().length === 0) return null;
    return { workspace_id: workspaceId, principal_id: session.principal_id, worker_item_id: workerItemId };
  }, [session, workspaceId, workerItemId]);
  const scopeKey = scope === null ? null : handoffDraftKey(scope);
  const [reviewNow, setReviewNow] = useState(() => Date.now());
  const sessionAuthorized = isAuthorizedSession(session, workspaceId, reviewNow);
  const [initialTargetSnapshot] = useState<AssigneeValue | null>(
    () => initialTarget === null ? null : cloneTarget(initialTarget),
  );

  const [draftEntries, setDraftEntries] = useState<Record<string, DraftEntry>>({});
  const draftEntriesRef = useRef<Record<string, DraftEntry>>({});
  const [busyKeys, setBusyKeys] = useState<Record<string, true>>({});
  const busyKeysRef = useRef(new Set<string>());
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [resolvedMappings, setResolvedMappings] = useState<Record<string, true>>({});
  const activeScopeKeyRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    activeScopeKeyRef.current = scopeKey;
    return () => {
      if (activeScopeKeyRef.current === scopeKey) activeScopeKeyRef.current = null;
    };
  }, [scopeKey]);

  const draftEntry = scopeKey === null ? undefined : draftEntries[scopeKey];
  const draft = draftEntry?.draft ?? null;
  const busy = scopeKey !== null && busyKeys[scopeKey] === true;
  const target = draft?.target ?? null;

  const assignmentKey = scope === null
    ? ["agent-assignment", workspaceId, "worker_item", workerItemId, ""] as const
    : [...assignmentQueryKey(workspaceId, "worker_item", workerItemId), scope.principal_id] as const;
  const assignmentQuery = useQuery({
    queryKey: assignmentKey,
    queryFn: ({ signal }) => readAssignment(workspaceId, "worker_item", workerItemId, signal),
    enabled: scope !== null && sessionAuthorized,
    retry: false,
  });
  const nativeContextKey = scope === null
    ? ["native-worker-context", workspaceId, workerItemId, ""] as const
    : [...nativeWorkerContextQueryKey(workspaceId, workerItemId), scope.principal_id] as const;
  const nativeContextQuery = useQuery({
    queryKey: nativeContextKey,
    queryFn: ({ signal }) => readNativeWorkerContext(workspaceId, workerItemId, signal),
    enabled: scope !== null && sessionAuthorized,
    retry: false,
  });
  const profilesQueryKey = scope === null
    ? ["profile-handoff-profiles", workspaceId, "", workerItemId] as const
    : ["profile-handoff-profiles", workspaceId, scope.principal_id, workerItemId] as const;
  const profilesQuery = useQuery({
    queryKey: profilesQueryKey,
    queryFn: ({ signal }) => listProfiles(workspaceId, signal),
    enabled: scope !== null && sessionAuthorized,
    retry: false,
  });

  const selectedProfileRef = target?.kind === "profile" ? target.profileRef : null;
  const selectedProfileKey = selectedProfileRef === null || scope === null
    ? ["profile-handoff-target", workspaceId, "", workerItemId, "", 0] as const
    : ["profile-handoff-target", workspaceId, scope.principal_id, workerItemId,
      selectedProfileRef.profile_id, selectedProfileRef.revision] as const;
  const selectedProfileQuery = useQuery({
    queryKey: selectedProfileKey,
    queryFn: ({ signal }) => {
      if (selectedProfileRef === null) throw new HandoffReadUnavailableError();
      return readProfile(workspaceId, selectedProfileRef.profile_id, selectedProfileRef.revision, signal);
    },
    enabled: scope !== null && sessionAuthorized && selectedProfileRef !== null,
    retry: false,
  });

  const currentReviewResult = useMemo<HandoffSourceReviewResult | null>(() => {
    if (scope === null || !sessionAuthorized || !assignmentQuery.isSuccess
      || !nativeContextQuery.isSuccess || nativeContextQuery.data === undefined) return null;
    if (!sameBinding(assignmentQuery.data, nativeContextQuery.data.binding)) {
      return { ok: false, reason: "当前 WorkerItem 的归属读取尚未对齐" };
    }
    return reviewHandoffSource({
      workspace,
      nativeContext: nativeContextQuery.data,
      principalId: scope.principal_id,
    });
  }, [
    scope,
    sessionAuthorized,
    assignmentQuery.isSuccess,
    assignmentQuery.data,
    nativeContextQuery.isSuccess,
    nativeContextQuery.data,
    workspace,
  ]);

  const proposalId = draft?.prepared?.proposal.id ?? null;
  const proposalQueryKey = scope === null || proposalId === null
    ? ["native-profile-handoff-proposal", workspaceId, "", workerItemId, ""] as const
    : ["native-profile-handoff-proposal", workspaceId, scope.principal_id, workerItemId, proposalId] as const;
  const proposalQuery = useQuery({
    queryKey: proposalQueryKey,
    queryFn: async ({ signal }) => {
      if (scope === null || proposalId === null) throw new HandoffReadUnavailableError();
      const value = await getHandoffProposal(scope.workspace_id, proposalId, signal);
      if (!isNativeHandoffProposal(value)
        || value.workspace_id !== scope.workspace_id || value.id !== proposalId
        || value.worker_item_id !== scope.worker_item_id) throw new HandoffReadUnavailableError();
      return value;
    },
    enabled: scope !== null && sessionAuthorized && proposalId !== null,
    retry: false,
  });
  const transitionId = draft?.transition_id ?? null;
  const transitionQueryKey = scope === null || transitionId === null
    ? ["native-profile-handoff-transition", workspaceId, "", workerItemId, ""] as const
    : ["native-profile-handoff-transition", workspaceId, scope.principal_id, workerItemId, transitionId] as const;
  const transitionQuery = useQuery({
    queryKey: transitionQueryKey,
    queryFn: async ({ signal }) => {
      if (scope === null || transitionId === null || proposalId === null) {
        throw new HandoffReadUnavailableError();
      }
      const value = await getHandoffTransition(scope.workspace_id, transitionId, signal);
      if (!isNativeHandoffTransition(value)
        || value.workspace_id !== scope.workspace_id || value.id !== transitionId
        || value.worker_item_id !== scope.worker_item_id || value.proposal_id !== proposalId) {
        throw new HandoffReadUnavailableError();
      }
      return value;
    },
    enabled: scope !== null && sessionAuthorized && transitionId !== null,
    retry: false,
  });
  const { refetch: refetchAssignment } = assignmentQuery;
  const { refetch: refetchNativeContext } = nativeContextQuery;
  const { refetch: refetchProposal } = proposalQuery;
  const { refetch: refetchTransition } = transitionQuery;

  const putDraftEntry = useCallback((key: string, entry: DraftEntry) => {
    const next = { ...draftEntriesRef.current, [key]: entry };
    draftEntriesRef.current = next;
    setDraftEntries(next);
  }, []);
  const putBusy = useCallback((key: string, value: boolean) => {
    if (value) busyKeysRef.current.add(key);
    else busyKeysRef.current.delete(key);
    setBusyKeys((previous) => {
      const next = { ...previous };
      if (value) next[key] = true;
      else delete next[key];
      return next;
    });
  }, []);
  const setMessage = useCallback((key: string, message: string | null) => {
    setMessages((previous) => {
      if (previous[key] === message) return previous;
      const next = { ...previous };
      if (message === null) delete next[key];
      else next[key] = message;
      return next;
    });
  }, []);
  const isCurrentScope = useCallback((key: string) => activeScopeKeyRef.current === key, []);

  const writeDraft = useCallback((
    targetScope: HandoffScope,
    key: string,
    nextDraft: HandoffDraft,
    updateOnStorageFailure: boolean,
  ): boolean => {
    const saved = saveHandoffDraft(targetScope, nextDraft);
    if (!saved.ok) {
      const previous = draftEntriesRef.current[key];
      putDraftEntry(key, {
        loaded: true,
        draft: updateOnStorageFailure ? nextDraft : previous?.draft ?? null,
        storageError: saved.reason,
        storageFailure: "write",
      });
      return false;
    }
    putDraftEntry(key, {
      loaded: true,
      draft: nextDraft,
      storageError: null,
      storageFailure: null,
    });
    return true;
  }, [putDraftEntry]);

  useEffect(() => {
    if (scope === null || scopeKey === null || draftEntriesRef.current[scopeKey]?.loaded === true) return;
    const loaded = loadHandoffDraft(scope);
    if (!loaded.ok) {
      putDraftEntry(scopeKey, {
        loaded: true,
        draft: null,
        storageError: loaded.reason,
        storageFailure: "read",
      });
      return;
    }
    const nextDraft = loaded.draft ?? createDraft(scope, initialTargetSnapshot);
    putDraftEntry(scopeKey, {
      loaded: true,
      draft: nextDraft,
      storageError: null,
      storageFailure: null,
    });
  }, [scope, scopeKey, initialTargetSnapshot, putDraftEntry]);

  useEffect(() => {
    if (scope === null || scopeKey === null || draftEntry?.loaded !== true || draft === null
      || draftEntry.storageError !== null || draft.source_review !== null
      || draft.prepared !== null || !allActionsIdle(draft)
      || currentReviewResult?.ok !== true) return;
    writeDraft(scope, scopeKey, { ...draft, source_review: currentReviewResult.review }, false);
  }, [scope, scopeKey, draftEntry?.loaded, draftEntry?.storageError, draft,
    currentReviewResult, writeDraft]);

  const currentProposal = proposalQuery.data;
  const currentTransition = transitionQuery.data;
  const sessionExpiry = session?.expires_at;
  const proposalExpiry = currentProposal?.expires_at;
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const sample = () => {
      const now = Date.now();
      setReviewNow(now);
      const next = [sessionExpiry, proposalExpiry]
        .map((value) => value === undefined ? Number.NaN : Date.parse(value))
        .filter((deadline) => Number.isFinite(deadline) && deadline > now);
      if (next.length > 0) {
        timer = setTimeout(sample, Math.min(Math.min(...next) - now + 1, 2_147_483_647));
      }
    };
    timer = setTimeout(sample, 0);
    return () => clearTimeout(timer);
  }, [scopeKey, sessionExpiry, proposalExpiry]);
  const sourceReview = draft?.source_review ?? (currentReviewResult?.ok ? currentReviewResult.review : null);
  const sourceIsCurrent = draft?.source_review !== null && draft?.source_review !== undefined
    && currentReviewResult?.ok === true
    && sameHandoffSourceReview(draft.source_review, currentReviewResult.review);
  const targetProfile = selectedProfileQuery.data;
  const targetReady = scope !== null && target !== null
    && targetIsAllowed(target, scope, draft?.source_review ?? null)
    && (target.kind === "human" || selectedProfileQuery.isSuccess
      && profileIsExactAndEnabled(targetProfile, target, scope));
  const proposalIsCanonicalPrepared = useMemo(() => {
    if (scope === null || draft?.prepared === null || draft?.prepared === undefined
      || draft.source_review === null || draft.target === null
      || currentReviewResult?.ok !== true || !sameHandoffSourceReview(draft.source_review, currentReviewResult.review)
      || !isNativeHandoffProposal(currentProposal) || nativeContextQuery.data === undefined) return false;
    const reviewed = reviewPreparedHandoff({
      review: draft.source_review,
      proposal: currentProposal,
      workspace,
      nativeContext: nativeContextQuery.data,
      target: draft.target,
      now: reviewNow,
    });
    return reviewed.ok && samePreparedHandoff(draft.prepared.proposal, reviewed.anchor.proposal);
  }, [scope, draft, currentReviewResult, currentProposal, nativeContextQuery.data, workspace, reviewNow]);

  const storageError = draftEntry?.storageError ?? null;
  const hasOutstandingLookup = draft !== null && scopeKey !== null
    && originalLookupAction(draft, resolvedMappings, scopeKey) !== null;
  const canEditTarget = scope !== null && sessionAuthorized && draftEntry?.loaded === true
    && storageError === null && draft !== null && draft.prepared === null
    && draft.source_review !== null && sourceIsCurrent && currentReviewResult?.ok === true
    && allActionsIdle(draft) && !hasOutstandingLookup && !busy;
  const canPrepare = scope !== null && sessionAuthorized && draftEntry?.loaded === true
    && storageError === null && draft !== null && draft.prepared === null
    && draft.source_review !== null && sourceIsCurrent && currentReviewResult?.ok === true
    && targetReady && draft.reason.trim().length > 0 && draft.instruction.trim().length > 0
    && allActionsIdle(draft) && !hasOutstandingLookup && !busy;
  const canConfirm = scope !== null && sessionAuthorized && draftEntry?.loaded === true
    && storageError === null && draft !== null && draft.prepared?.proposal.status === "prepared"
    && draft.actions.prepare.status === "applied" && draft.actions.confirm.status === "idle"
    && draft.actions.cancel.status === "idle" && sourceIsCurrent && targetReady
    && proposalIsCanonicalPrepared && !hasOutstandingLookup && !busy;
  const canCancel = canConfirm && draft?.transition_id === null;
  const mappingResolved = scopeKey !== null && draft !== null
    && originalLookupAction(draft, resolvedMappings, scopeKey) === null;
  const canStartNew = scope !== null && sessionAuthorized && draftEntry?.loaded === true
    && storageError === null && draft !== null && mappingResolved && !busy
    && knownTerminalOutcome(draft, currentProposal, currentTransition, resolvedMappings, scopeKey);

  const profiles = useMemo(() => {
    const items = profilesQuery.data?.items ?? [];
    if (targetProfile === undefined || !validProfileIdentity(targetProfile, workspaceId)) return items;
    if (items.some((profile) => sameProfileRef(profile.profile.ref, targetProfile.profile.ref))) return items;
    return [...items, targetProfile];
  }, [profilesQuery.data?.items, targetProfile, workspaceId]);

  const refreshAuthorization = useCallback(async (
    targetScope: HandoffScope,
    key: string,
  ): Promise<LocalSession | null> => {
    if (!isCurrentScope(key)) return null;
    const value = await readLocalSession();
    if (!isCurrentScope(key) || !isAuthorizedSession(value, targetScope.workspace_id)
      || value.principal_id !== targetScope.principal_id) return null;
    queryClient.setQueryData(sessionQueryKey, value);
    return value;
  }, [isCurrentScope, queryClient, sessionQueryKey]);

  const onSessionAuthorized = useCallback((nextSession: LocalSession) => {
    queryClient.setQueryData(
      ["profile-handoff-session", workspaceId, workerItemId],
      nextSession,
    );
  }, [queryClient, workspaceId, workerItemId]);

  const refreshReview = useCallback(async (
    targetScope: HandoffScope,
    key: string,
  ): Promise<HandoffSourceReviewResult | null> => {
    if (!isCurrentScope(key)) return null;
    await onRefresh();
    if (!isCurrentScope(key)) return null;
    const [assignmentResult, contextResult] = await Promise.all([
      refetchAssignment({ throwOnError: true }),
      refetchNativeContext({ throwOnError: true }),
    ]);
    if (!isCurrentScope(key) || assignmentResult.status !== "success"
      || contextResult.status !== "success") return null;
    const refreshedWorkspace = queryClient.getQueryData<WorkerWorkspace>(
      workerWorkspaceQueryKey(targetScope.workspace_id, targetScope.worker_item_id),
    ) ?? workspace;
    return reviewFacts(refreshedWorkspace, assignmentResult.data, contextResult.data, targetScope);
  }, [isCurrentScope, onRefresh, refetchAssignment, refetchNativeContext,
    queryClient, workspace]);

  const refreshAfterAction = useCallback(async (capturedScope: HandoffScope, key: string) => {
    if (!isCurrentScope(key)) return;
    const queryKeys: Array<readonly unknown[]> = [
      [...assignmentQueryKey(capturedScope.workspace_id, "worker_item", capturedScope.worker_item_id), capturedScope.principal_id],
      [...nativeWorkerContextQueryKey(capturedScope.workspace_id, capturedScope.worker_item_id), capturedScope.principal_id],
      workerWorkspaceQueryKey(capturedScope.workspace_id, capturedScope.worker_item_id),
    ];
    await Promise.all(queryKeys.map((queryKey) => queryClient.invalidateQueries({ queryKey, exact: true })));
    if (isCurrentScope(key)) await onRefresh();
  }, [isCurrentScope, queryClient, onRefresh]);

  const markMappingResolved = useCallback((key: string, action: HandoffActionName) => {
    const resolvedKey = `${key}:${action}`;
    setResolvedMappings((previous) => previous[resolvedKey] === true
      ? previous
      : { ...previous, [resolvedKey]: true });
  }, []);

  const submitAndMap = useCallback(async (
    capturedScope: HandoffScope,
    capturedKey: string,
    pending: HandoffDraft,
    action: HandoffActionName,
    original: CommandRequest,
  ) => {
    try {
      const response = await submitNativeCommand(original);
      if (!isCurrentScope(capturedKey)) return;
      if (!isRecord(response) || response.command_id !== original.command_id) {
        const latest = draftEntriesRef.current[capturedKey]?.draft ?? pending;
        const unknown: HandoffDraft = {
          ...latest,
          actions: {
            ...latest.actions,
            [action]: { ...latest.actions[action], status: "unknown", rejectionCode: null },
          },
        };
        writeDraft(capturedScope, capturedKey, unknown, true);
        setMessage(capturedKey, "提交结果尚未确认，可查询原始操作");
        return;
      }
      const mapped = mapOriginalResult(pending, action, response, capturedScope);
      const next = applyActionResult(pending, action, mapped);
      if (mapped.ok) {
        queryClient.setQueryData(proposalQueryKeyFor(capturedScope, mapped.proposal.id), mapped.proposal);
        if (mapped.transition !== null) {
          queryClient.setQueryData(transitionQueryKeyFor(capturedScope, mapped.transition.id), mapped.transition);
        }
        if (action === "cancel") markMappingResolved(capturedKey, action);
      }
      writeDraft(capturedScope, capturedKey, next, true);
      if (!mapped.ok) setMessage(capturedKey, "原始操作已处理，但结果映射暂不可用");
      try {
        await refreshAfterAction(capturedScope, capturedKey);
      } catch {
        if (isCurrentScope(capturedKey)) setMessage(capturedKey, "操作已确认，但最新事实读取失败，请重试");
      }
    } catch {
      if (!isCurrentScope(capturedKey)) return;
      const latest = draftEntriesRef.current[capturedKey]?.draft ?? pending;
      const unknown: HandoffDraft = {
        ...latest,
        actions: {
          ...latest.actions,
          [action]: { ...latest.actions[action], status: "unknown", rejectionCode: null },
        },
      };
      writeDraft(capturedScope, capturedKey, unknown, true);
      setMessage(capturedKey, "提交结果尚未确认，可查询原始操作");
    }
  }, [isCurrentScope, queryClient, writeDraft, setMessage, refreshAfterAction,
    markMappingResolved]);

  const setTarget = useCallback((nextTarget: AssigneeValue | null) => {
    if (scope === null || scopeKey === null || busyKeysRef.current.has(scopeKey)) return;
    const entry = draftEntriesRef.current[scopeKey];
    const latest = entry?.draft;
    if (entry?.loaded !== true || entry.storageError !== null || latest === null || latest === undefined
      || latest.prepared !== null || !allActionsIdle(latest)
      || nextTarget !== null && !targetShapeAllowed(nextTarget, scope)) return;
    writeDraft(scope, scopeKey, {
      ...latest,
      target: nextTarget === null ? null : cloneTarget(nextTarget),
    }, true);
    setMessage(scopeKey, null);
  }, [scope, scopeKey, writeDraft, setMessage]);

  const setReason = useCallback((reason: string) => {
    if (scope === null || scopeKey === null || busyKeysRef.current.has(scopeKey)) return;
    const entry = draftEntriesRef.current[scopeKey];
    const latest = entry?.draft;
    if (entry?.loaded !== true || entry.storageError !== null || latest === null || latest === undefined
      || latest.prepared !== null || !allActionsIdle(latest)) return;
    writeDraft(scope, scopeKey, { ...latest, reason }, true);
  }, [scope, scopeKey, writeDraft]);

  const setInstruction = useCallback((instruction: string) => {
    if (scope === null || scopeKey === null || busyKeysRef.current.has(scopeKey)) return;
    const entry = draftEntriesRef.current[scopeKey];
    const latest = entry?.draft;
    if (entry?.loaded !== true || entry.storageError !== null || latest === null || latest === undefined
      || latest.prepared !== null || !allActionsIdle(latest)) return;
    writeDraft(scope, scopeKey, { ...latest, instruction }, true);
  }, [scope, scopeKey, writeDraft]);

  const prepare = useCallback(async () => {
    if (!canPrepare || scope === null || scopeKey === null || draft === null
      || currentReviewResult?.ok !== true || draft.target === null
      || busyKeysRef.current.has(scopeKey)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    const capturedDraft = draft;
    const capturedTarget = draft.target;
    putBusy(capturedKey, true);
    setMessage(capturedKey, null);
    try {
      const currentSession = await refreshAuthorization(capturedScope, capturedKey);
      if (currentSession === null || !isCurrentScope(capturedKey)) {
        setMessage(capturedKey, "需要重新确认当前用户会话");
        return;
      }
      const latestReview = await refreshReview(capturedScope, capturedKey);
      if (!isCurrentScope(capturedKey)) return;
      if (latestReview?.ok !== true || capturedDraft.source_review === null
        || !sameHandoffSourceReview(capturedDraft.source_review, latestReview.review)) {
        setMessage(capturedKey, "来源事实已变化，请重新读取并复核");
        return;
      }
      if (!(await verifyTarget(capturedTarget, capturedScope, latestReview.review))) {
        if (isCurrentScope(capturedKey)) setMessage(capturedKey, "所选目标 Profile 版本不可用");
        return;
      }
      if (!isCurrentScope(capturedKey)) return;
      const normalizedReason = capturedDraft.reason.trim();
      const normalizedInstruction = capturedDraft.instruction.trim();
      const original = buildNativeCommand({
        session: currentSession,
        workspaceId: capturedScope.workspace_id,
        commandType: "PrepareProfileHandoff",
        targetType: "worker_item",
        targetId: capturedScope.worker_item_id,
        expectedVersion: latestReview.review.worker_item_version,
        payload: {
          proposal_schema_version: 2,
          context: contextFromReview(latestReview.review),
          target_assignee: { kind: capturedTarget.kind, id: capturedTarget.id },
          target_profile_ref: capturedTarget.kind === "profile"
            ? capturedTarget.profileRef
            : null,
          handoff_reason: normalizedReason,
          handoff_instruction: normalizedInstruction,
          input_plan: {},
          proposal_ttl_seconds: 900,
        },
      });
      const pending: HandoffDraft = {
        ...capturedDraft,
        source_review: latestReview.review,
        reason: normalizedReason,
        instruction: normalizedInstruction,
        actions: {
          ...capturedDraft.actions,
          prepare: { original, status: "pending", rejectionCode: null },
        },
      };
      if (!writeDraft(capturedScope, capturedKey, pending, false)) {
        setMessage(capturedKey, "原始交接请求未能保存，因此没有提交");
        return;
      }
      if (!isCurrentScope(capturedKey)) return;
      await submitAndMap(capturedScope, capturedKey, pending, "prepare", original);
    } catch {
      if (isCurrentScope(capturedKey)) setMessage(capturedKey, "交接请求尚未确认，可查询原始操作");
    } finally {
      putBusy(capturedKey, false);
    }
  }, [canPrepare, scope, scopeKey, draft, currentReviewResult, putBusy, setMessage,
    refreshAuthorization, isCurrentScope, refreshReview, writeDraft, submitAndMap]);

  const confirm = useCallback(async () => {
    if (!canConfirm || scope === null || scopeKey === null || draft?.prepared === null
      || draft?.prepared === undefined || draft.source_review === null || busyKeysRef.current.has(scopeKey)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    const capturedDraft = draft;
    const anchor = draft.prepared;
    putBusy(capturedKey, true);
    setMessage(capturedKey, null);
    try {
      const currentSession = await refreshAuthorization(capturedScope, capturedKey);
      if (currentSession === null || !isCurrentScope(capturedKey)) {
        setMessage(capturedKey, "需要重新确认当前用户会话");
        return;
      }
      const latestReview = await refreshReview(capturedScope, capturedKey);
      if (!isCurrentScope(capturedKey)) return;
      const latestProposal = await refetchProposal({ throwOnError: true });
      if (!isCurrentScope(capturedKey)) return;
      const latestContext = await refetchNativeContext({ throwOnError: true });
      if (!isCurrentScope(capturedKey)) return;
      const latestWorkspace = queryClient.getQueryData<WorkerWorkspace>(
        workerWorkspaceQueryKey(capturedScope.workspace_id, capturedScope.worker_item_id),
      ) ?? workspace;
      if (latestReview?.ok !== true || !sameHandoffSourceReview(anchor.source_review, latestReview.review)
        || latestProposal.status !== "success" || !isNativeHandoffProposal(latestProposal.data)
        || latestContext.status !== "success" || latestContext.data.binding === null
        || !samePreparedAtCurrentFacts(anchor, latestProposal.data, capturedDraft.target,
          latestReview.review, latestWorkspace, latestContext.data)
        || capturedDraft.target === null
        || !(await verifyTarget(capturedDraft.target, capturedScope, anchor.source_review))) {
        if (isCurrentScope(capturedKey)) setMessage(capturedKey, "交接方案或来源事实已变化，请重新读取");
        return;
      }
      if (!isCurrentScope(capturedKey)) return;
      const original = buildNativeCommand({
        session: currentSession,
        workspaceId: capturedScope.workspace_id,
        commandType: "ConfirmProfileHandoff",
        targetType: "handoff_proposal",
        targetId: anchor.proposal.id,
        expectedVersion: anchor.proposal.version,
        payload: {
          proposal_id: anchor.proposal.id,
          expected_proposal_version: anchor.proposal.version,
          expected_proposal_hash: anchor.proposal.proposal_hash,
          expected_worker_item_version: anchor.proposal.source_worker_item_version,
        },
      });
      const pending: HandoffDraft = {
        ...capturedDraft,
        actions: {
          ...capturedDraft.actions,
          confirm: { original, status: "pending", rejectionCode: null },
        },
      };
      if (!writeDraft(capturedScope, capturedKey, pending, false)) {
        setMessage(capturedKey, "原始确认请求未能保存，因此没有提交");
        return;
      }
      if (!isCurrentScope(capturedKey)) return;
      await submitAndMap(capturedScope, capturedKey, pending, "confirm", original);
    } catch {
      if (isCurrentScope(capturedKey)) setMessage(capturedKey, "确认结果尚未确认，可查询原始操作");
    } finally {
      putBusy(capturedKey, false);
    }
  }, [canConfirm, scope, scopeKey, draft, putBusy, setMessage, refreshAuthorization,
    isCurrentScope, refreshReview, refetchProposal, refetchNativeContext,
    queryClient, workspace, writeDraft, submitAndMap]);

  const cancel = useCallback(async () => {
    if (!canCancel || scope === null || scopeKey === null || draft?.prepared === null
      || draft?.prepared === undefined || busyKeysRef.current.has(scopeKey)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    const capturedDraft = draft;
    const anchor = draft.prepared;
    putBusy(capturedKey, true);
    setMessage(capturedKey, null);
    try {
      const currentSession = await refreshAuthorization(capturedScope, capturedKey);
      if (currentSession === null || !isCurrentScope(capturedKey)) {
        setMessage(capturedKey, "需要重新确认当前用户会话");
        return;
      }
      const latestReview = await refreshReview(capturedScope, capturedKey);
      if (!isCurrentScope(capturedKey)) return;
      const latestProposal = await refetchProposal({ throwOnError: true });
      if (!isCurrentScope(capturedKey)) return;
      const latestContext = await refetchNativeContext({ throwOnError: true });
      if (!isCurrentScope(capturedKey)) return;
      const latestWorkspace = queryClient.getQueryData<WorkerWorkspace>(
        workerWorkspaceQueryKey(capturedScope.workspace_id, capturedScope.worker_item_id),
      ) ?? workspace;
      if (latestReview?.ok !== true || !sameHandoffSourceReview(anchor.source_review, latestReview.review)
        || latestProposal.status !== "success" || !isNativeHandoffProposal(latestProposal.data)
        || latestContext.status !== "success" || latestContext.data.binding === null
        || !samePreparedAtCurrentFacts(anchor, latestProposal.data, capturedDraft.target,
          latestReview.review, latestWorkspace, latestContext.data)
        || capturedDraft.target === null
        || !(await verifyTarget(capturedDraft.target, capturedScope, anchor.source_review))) {
        if (isCurrentScope(capturedKey)) setMessage(capturedKey, "交接方案或来源事实已变化，请重新读取");
        return;
      }
      if (!isCurrentScope(capturedKey)) return;
      const original = buildNativeCommand({
        session: currentSession,
        workspaceId: capturedScope.workspace_id,
        commandType: "CancelWorkerHandoff",
        targetType: "handoff_proposal",
        targetId: anchor.proposal.id,
        expectedVersion: anchor.proposal.version,
        payload: { reason: "Cancel before target activation" },
      });
      const pending: HandoffDraft = {
        ...capturedDraft,
        actions: {
          ...capturedDraft.actions,
          cancel: { original, status: "pending", rejectionCode: null },
        },
      };
      if (!writeDraft(capturedScope, capturedKey, pending, false)) {
        setMessage(capturedKey, "原始取消请求未能保存，因此没有提交");
        return;
      }
      if (!isCurrentScope(capturedKey)) return;
      await submitAndMap(capturedScope, capturedKey, pending, "cancel", original);
    } catch {
      if (isCurrentScope(capturedKey)) setMessage(capturedKey, "取消结果尚未确认，可查询原始操作");
    } finally {
      putBusy(capturedKey, false);
    }
  }, [canCancel, scope, scopeKey, draft, putBusy, setMessage, refreshAuthorization,
    isCurrentScope, refreshReview, refetchProposal, refetchNativeContext,
    queryClient, workspace, writeDraft, submitAndMap]);

  const lookupOriginal = useCallback(async () => {
    if (scope === null || scopeKey === null || !sessionAuthorized || busyKeysRef.current.has(scopeKey)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    const currentDraft = draftEntriesRef.current[capturedKey]?.draft;
    const action = currentDraft === null || currentDraft === undefined
      ? null
      : originalLookupAction(currentDraft, resolvedMappings, capturedKey);
    const original = action === null ? null : currentDraft?.actions[action].original ?? null;
    if (currentDraft === null || currentDraft === undefined || action === null || original === null) return;
    putBusy(capturedKey, true);
    setMessage(capturedKey, null);
    try {
      const currentSession = await refreshAuthorization(capturedScope, capturedKey);
      if (currentSession === null || !isCurrentScope(capturedKey)) {
        setMessage(capturedKey, "需要重新确认当前用户会话");
        return;
      }
      const status = await lookupNativeCommand(original);
      if (!isCurrentScope(capturedKey)) return;
      if (!matchesOriginalStatus(status, original)) {
        const latest = draftEntriesRef.current[capturedKey]?.draft ?? currentDraft;
        writeDraft(capturedScope, capturedKey, setActionStatus(latest, action, "unknown", null), true);
        setMessage(capturedKey, "原始操作状态尚未确认");
        return;
      }
      if (status.status === "pending") {
        const latest = draftEntriesRef.current[capturedKey]?.draft ?? currentDraft;
        writeDraft(capturedScope, capturedKey, setActionStatus(
          latest, action, currentDraft.actions[action].status === "pending" ? "pending" : "unknown", null,
        ), true);
        setMessage(capturedKey, "原始操作仍在处理中");
        return;
      }
      if (status.status === "rejected") {
        const rejectionCode = safeRejectionCode(status.error_code);
        const latest = draftEntriesRef.current[capturedKey]?.draft ?? currentDraft;
        writeDraft(capturedScope, capturedKey, setActionStatus(latest, action, "rejected", rejectionCode), true);
        setMessage(capturedKey, REJECTION_MESSAGES[rejectionCode] ?? "原始交接请求被拒绝，请重新读取事实");
        return;
      }
      const result = status.result;
      const mapped = result === null || result === undefined
        ? { ok: false as const, reason: "mapping_unavailable" as const }
        : mapOriginalResult(currentDraft, action, result, capturedScope);
      const latest = draftEntriesRef.current[capturedKey]?.draft ?? currentDraft;
      const applied = applyActionResult(setActionStatus(latest, action, "applied", null), action, mapped);
      if (mapped.ok) {
        queryClient.setQueryData(proposalQueryKeyFor(capturedScope, mapped.proposal.id), mapped.proposal);
        if (mapped.transition !== null) {
          queryClient.setQueryData(transitionQueryKeyFor(capturedScope, mapped.transition.id), mapped.transition);
        }
        if (action === "cancel") markMappingResolved(capturedKey, action);
        setMessage(capturedKey, null);
      } else {
        setMessage(capturedKey, "原始操作已处理，但结果映射暂不可用");
      }
      writeDraft(capturedScope, capturedKey, applied, true);
      if (mapped.ok) {
        try {
          await refreshAfterAction(capturedScope, capturedKey);
        } catch {
          if (isCurrentScope(capturedKey)) setMessage(capturedKey, "操作已确认，但最新事实读取失败，请重试");
        }
      }
    } catch {
      if (!isCurrentScope(capturedKey)) return;
      const latest = draftEntriesRef.current[capturedKey]?.draft ?? currentDraft;
      writeDraft(capturedScope, capturedKey, setActionStatus(latest, action, "unknown", null), true);
      setMessage(capturedKey, "原始操作状态尚未确认，请重试读取");
    } finally {
      putBusy(capturedKey, false);
    }
  }, [scope, scopeKey, sessionAuthorized, resolvedMappings, putBusy, setMessage,
    refreshAuthorization, isCurrentScope, writeDraft, queryClient, markMappingResolved,
    refreshAfterAction]);

  const retryDraftStorage = useCallback(() => {
    if (scope === null || scopeKey === null) return;
    const entry = draftEntriesRef.current[scopeKey];
    if (entry?.storageFailure === "write" && entry.draft !== null) {
      writeDraft(scope, scopeKey, entry.draft, true);
      return;
    }
    const loaded = loadHandoffDraft(scope);
    if (!loaded.ok) {
      putDraftEntry(scopeKey, {
        loaded: true,
        draft: null,
        storageError: loaded.reason,
        storageFailure: "read",
      });
      return;
    }
    putDraftEntry(scopeKey, {
      loaded: true,
      draft: loaded.draft ?? createDraft(scope, initialTargetSnapshot),
      storageError: null,
      storageFailure: null,
    });
  }, [scope, scopeKey, writeDraft, putDraftEntry, initialTargetSnapshot]);

  const revisit = useCallback(async () => {
    if (scope === null || scopeKey === null || !sessionAuthorized || busyKeysRef.current.has(scopeKey)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    const currentDraft = draftEntriesRef.current[capturedKey]?.draft;
    if (currentDraft === null || currentDraft === undefined) return;
    putBusy(capturedKey, true);
    setMessage(capturedKey, null);
    try {
      const currentSession = await refreshAuthorization(capturedScope, capturedKey);
      if (currentSession === null || !isCurrentScope(capturedKey)) {
        setMessage(capturedKey, "需要重新确认当前用户会话");
        return;
      }
      const result = await refreshReview(capturedScope, capturedKey);
      if (!isCurrentScope(capturedKey)) return;
      if (result === null) {
        setMessage(capturedKey, "来源事实暂不可用，请重试读取");
        return;
      }
      if (currentDraft.prepared === null && allActionsIdle(currentDraft) && result.ok) {
        writeDraft(capturedScope, capturedKey, {
          ...currentDraft,
          source_review: result.review,
          target: currentDraft.target !== null && targetShapeAllowed(currentDraft.target, capturedScope)
            ? currentDraft.target
            : null,
        }, true);
      }
      await refetchProposal();
      if (transitionId !== null) await refetchTransition();
    } catch {
      if (isCurrentScope(capturedKey)) setMessage(capturedKey, "来源事实暂不可用，请重试读取");
    } finally {
      putBusy(capturedKey, false);
    }
  }, [scope, scopeKey, sessionAuthorized, putBusy, setMessage, refreshAuthorization,
    isCurrentScope, refreshReview, writeDraft, refetchProposal,
    refetchTransition, transitionId]);

  const startNew = useCallback(async () => {
    if (!canStartNew || scope === null || scopeKey === null || draft === null
      || busyKeysRef.current.has(scopeKey)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    const previousTarget = draft.target;
    putBusy(capturedKey, true);
    setMessage(capturedKey, null);
    try {
      const currentSession = await refreshAuthorization(capturedScope, capturedKey);
      if (currentSession === null || !isCurrentScope(capturedKey)) {
        setMessage(capturedKey, "需要重新确认当前用户会话");
        return;
      }
      const latestReview = await refreshReview(capturedScope, capturedKey);
      if (!isCurrentScope(capturedKey)) return;
      const next = createDraft(capturedScope, previousTarget);
      if (latestReview?.ok === true) next.source_review = latestReview.review;
      writeDraft(capturedScope, capturedKey, next, true);
      setResolvedMappings((previous) => Object.fromEntries(
        Object.entries(previous).filter(([key]) => !key.startsWith(`${capturedKey}:`)),
      ));
    } catch {
      if (isCurrentScope(capturedKey)) setMessage(capturedKey, "来源事实暂不可用，请重试读取");
    } finally {
      putBusy(capturedKey, false);
    }
  }, [canStartNew, scope, scopeKey, draft, putBusy, setMessage,
    refreshAuthorization, isCurrentScope, refreshReview, writeDraft]);

  const lookupAction = scopeKey === null || draft === null
    ? null
    : originalLookupAction(draft, resolvedMappings, scopeKey);
  const originalLookupNeeded = lookupAction !== null;
  const original = lookupAction === null || draft === null ? null : draft.actions[lookupAction].original;
  const error = scopeKey === null ? null : messages[scopeKey] ?? storageError;
  const isStale = draft?.source_review !== null && draft?.source_review !== undefined
    && currentReviewResult?.ok === true
    && !sameHandoffSourceReview(draft.source_review, currentReviewResult.review);

  return {
    scope,
    session,
    sessionAuthorized,
    sourceReview,
    currentReviewResult,
    target,
    setTarget,
    reason: draft?.reason ?? "",
    setReason,
    instruction: draft?.instruction ?? "",
    setInstruction,
    profiles,
    selectedProfile: targetProfile,
    prepared: draft?.prepared ?? null,
    proposal: isNativeHandoffProposal(currentProposal) ? currentProposal : null,
    transitionId,
    transition: isNativeHandoffTransition(currentTransition) ? currentTransition : null,
    actions: draft?.actions ?? { prepare: IDLE_ACTION, confirm: IDLE_ACTION, cancel: IDLE_ACTION },
    original,
    originalLookupNeeded,
    lookupAction,
    status: lookupAction === null ? null : draft?.actions[lookupAction].status ?? null,
    busy,
    error,
    storageError,
    isStale,
    canEditTarget,
    canPrepare,
    canConfirm,
    canCancel,
    canLookupOriginal: originalLookupNeeded && original !== null && sessionAuthorized && !busy,
    canStartNew,
    prepare,
    confirm,
    cancel,
    revisit,
    startNew,
    lookupOriginal,
    retryDraftStorage,
    onSessionAuthorized,
  };
}

function createDraft(scope: HandoffScope, initialTarget: AssigneeValue | null): HandoffDraft {
  const target = initialTarget !== null && targetShapeAllowed(initialTarget, scope)
    ? cloneTarget(initialTarget)
    : null;
  return {
    schema_version: 1,
    scope,
    source_review: null,
    target,
    reason: "",
    instruction: "",
    prepared: null,
    transition_id: null,
    actions: { prepare: { ...IDLE_ACTION }, confirm: { ...IDLE_ACTION }, cancel: { ...IDLE_ACTION } },
  };
}

function reviewFacts(
  workspace: WorkerWorkspace,
  assignment: AgentBindingView | null,
  nativeContext: NativeWorkerContextView,
  scope: HandoffScope,
): HandoffSourceReviewResult {
  if (!sameBinding(assignment, nativeContext.binding)) {
    return { ok: false, reason: "当前 WorkerItem 的归属读取尚未对齐" };
  }
  return reviewHandoffSource({ workspace, nativeContext, principalId: scope.principal_id });
}

function sameBinding(left: AgentBindingView | null | undefined, right: AgentBindingView | null): boolean {
  if (left === null || left === undefined || right === null) return left === right;
  return left.workspace_id === right.workspace_id
    && left.target_type === right.target_type
    && left.target_id === right.target_id
    && left.version === right.version
    && left.assignee.kind === right.assignee.kind
    && left.assignee.id === right.assignee.id
    && sameProfileRef(left.profile_ref, right.profile_ref);
}

function samePreparedAtCurrentFacts(
  anchor: NonNullable<HandoffDraft["prepared"]>,
  proposal: NativeHandoffProposal,
  target: AssigneeValue | null,
  review: HandoffSourceReview,
  workspace: WorkerWorkspace,
  nativeContext: NativeWorkerContextView,
): boolean {
  if (target === null) return false;
  const current = reviewPreparedHandoff({
    review,
    proposal,
    workspace,
    nativeContext,
    target,
    now: Date.now(),
  });
  return current.ok && samePreparedHandoff(anchor.proposal, current.anchor.proposal);
}

function mapOriginalResult(
  draft: HandoffDraft,
  action: HandoffActionName,
  response: unknown,
  scope: HandoffScope,
) {
  if (draft.source_review === null || draft.target === null) {
    return { ok: false as const, reason: "mapping_unavailable" as const };
  }
  return mapHandoffActionResult({
    action,
    original: draft.actions[action].original as CommandRequest,
    response,
    scope,
    source_review: draft.source_review,
    target: draft.target,
    prepared: draft.prepared,
  });
}

function applyActionResult(
  draft: HandoffDraft,
  action: HandoffActionName,
  result: ReturnType<typeof mapHandoffActionResult>,
): HandoffDraft {
  const actionState = { ...draft.actions[action], status: "applied" as const, rejectionCode: null };
  if (!result.ok) {
    return { ...draft, actions: { ...draft.actions, [action]: actionState } };
  }
  if (action === "prepare") {
    if (draft.source_review === null || draft.target === null) {
      return { ...draft, actions: { ...draft.actions, prepare: actionState } };
    }
    return {
      ...draft,
      prepared: {
        proposal: result.proposal,
        source_review: draft.source_review,
        target: draft.target,
      },
      actions: { ...draft.actions, prepare: actionState },
    };
  }
  if (action === "confirm") {
    return {
      ...draft,
      transition_id: result.transition?.id ?? null,
      actions: { ...draft.actions, confirm: actionState },
    };
  }
  return { ...draft, actions: { ...draft.actions, cancel: actionState } };
}

function setActionStatus(
  draft: HandoffDraft,
  action: HandoffActionName,
  status: HandoffActionState["status"],
  rejectionCode: string | null,
): HandoffDraft {
  return {
    ...draft,
    actions: {
      ...draft.actions,
      [action]: { ...draft.actions[action], status, rejectionCode },
    },
  };
}

function matchesOriginalStatus(
  value: CommandStatusResponse,
  original: CommandRequest,
): value is CommandStatusResponse {
  if (!isRecord(value)) return false;
  return value.command_id === original.command_id
    && value.command_type === original.command_type
    && value.target_type === original.target_type
    && value.target_id === original.target_id
    && value.expected_version === original.expected_version
    && ["pending", "applied", "rejected"].includes(String(value.status));
}

function safeRejectionCode(value: unknown): string {
  return typeof value === "string" && SAFE_REJECTIONS.has(value) ? value : "unknown";
}

function originalLookupAction(
  draft: HandoffDraft,
  resolved: Record<string, true>,
  scopeKey: string,
): HandoffActionName | null {
  for (const action of ["prepare", "confirm", "cancel"] as const) {
    const state = draft.actions[action];
    const unresolvedStatus = state.status === "pending" || state.status === "unknown";
    const missingMapping = state.status === "applied" && (
      action === "prepare" ? draft.prepared === null
        : action === "confirm" ? draft.transition_id === null
          : resolved[`${scopeKey}:${action}`] !== true
    );
    if (state.original !== null && (unresolvedStatus || missingMapping)) return action;
  }
  return null;
}

function knownTerminalOutcome(
  draft: HandoffDraft,
  proposal: NativeHandoffProposal | undefined,
  transition: NativeHandoffTransition | undefined,
  resolved: Record<string, true>,
  scopeKey: string,
): boolean {
  if (draft.prepared === null) {
    return draft.actions.prepare.status === "rejected"
      && draft.actions.confirm.status === "idle" && draft.actions.cancel.status === "idle";
  }
  if (TERMINAL_PROPOSAL_STATUSES.has(proposal?.status ?? draft.prepared.proposal.status)) {
    return draft.actions.prepare.status === "applied"
      && draft.actions.confirm.status === "idle"
      && (draft.actions.cancel.status === "idle"
        || draft.actions.cancel.status === "applied"
          && resolved[`${scopeKey}:cancel`] === true);
  }
  return draft.transition_id !== null && transition?.status === "converged"
    && draft.actions.confirm.status === "applied";
}

function allActionsIdle(draft: HandoffDraft): boolean {
  return draft.actions.prepare.status === "idle" && draft.actions.prepare.original === null
    && draft.actions.confirm.status === "idle" && draft.actions.confirm.original === null
    && draft.actions.cancel.status === "idle" && draft.actions.cancel.original === null;
}

function contextFromReview(review: HandoffSourceReview) {
  return {
    worker_item_id: review.worker_item_id,
    execution_mode: review.execution_mode,
    execution_id: review.execution_id,
    session_id: review.session_id,
    session_version: review.session_version,
    ...(review.session_last_sequence === undefined ? {} : { session_last_sequence: review.session_last_sequence }),
    worker_id: review.worker_id,
    binding_version: review.binding_version,
    run_id: review.run_id,
    plan_generation: review.plan_generation,
    context_version: review.context_version,
  };
}

function targetIsAllowed(
  target: AssigneeValue,
  scope: HandoffScope,
  review: HandoffSourceReview | null,
): boolean {
  if (!targetShapeAllowed(target, scope)) return false;
  if (target.kind === "human") return target.id === scope.principal_id;
  return review === null || !(target.profileRef.workspace_id === review.source_profile_ref.workspace_id
    && target.profileRef.profile_id === review.source_profile_ref.profile_id
    && target.profileRef.revision === review.source_profile_ref.revision);
}

function targetShapeAllowed(target: AssigneeValue, scope: HandoffScope): boolean {
  if (target.kind === "human") return target.id === scope.principal_id && target.profileRef === null;
  return target.profileRef.workspace_id === scope.workspace_id
    && target.profileRef.profile_id === target.id
    && Number.isSafeInteger(target.profileRef.revision) && target.profileRef.revision > 0;
}

function verifyTarget(target: AssigneeValue, scope: HandoffScope, review: HandoffSourceReview): Promise<boolean> {
  if (!targetIsAllowed(target, scope, review)) return Promise.resolve(false);
  if (target.kind === "human") return Promise.resolve(true);
  return readProfile(scope.workspace_id, target.id, target.profileRef.revision)
    .then((profile) => profileIsExactAndEnabled(profile, target, scope))
    .catch(() => false);
}

function profileIsExactAndEnabled(
  profile: ProfileView | undefined,
  target: AssigneeValue,
  scope: HandoffScope,
): boolean {
  return target.kind === "profile" && profile !== undefined
    && profile.profile.ref.workspace_id === scope.workspace_id
    && profile.profile.ref.profile_id === target.id
    && profile.profile.ref.revision === target.profileRef.revision
    && profile.status.profile_id === target.id
    && profile.status.enabled === true;
}

function validProfileIdentity(profile: ProfileView, workspaceId: string): boolean {
  return profile.profile.ref.workspace_id === workspaceId
    && profile.profile.ref.profile_id.trim().length > 0
    && Number.isSafeInteger(profile.profile.ref.revision)
    && profile.profile.ref.revision > 0;
}

function sameProfileRef(
  left: AgentBindingView["profile_ref"],
  right: AgentBindingView["profile_ref"],
): boolean {
  return left === null || right === null
    ? left === right
    : left.workspace_id === right.workspace_id
      && left.profile_id === right.profile_id
      && left.revision === right.revision;
}

function cloneTarget(target: AssigneeValue): AssigneeValue {
  return target.kind === "human"
    ? { ...target }
    : { ...target, profileRef: { ...target.profileRef } };
}

function isAuthorizedSession(session: LocalSession | null, workspaceId: string, now = Date.now()): session is LocalSession {
  if (session === null || session.workspace_id !== workspaceId
    || session.session_id.trim().length === 0 || session.principal_id.trim().length === 0) return false;
  const expiresAt = Date.parse(session.expires_at);
  return Number.isFinite(expiresAt) && expiresAt > now;
}

function proposalQueryKeyFor(scope: HandoffScope, proposalId: string) {
  return ["native-profile-handoff-proposal", scope.workspace_id, scope.principal_id,
    scope.worker_item_id, proposalId] as const;
}

function transitionQueryKeyFor(scope: HandoffScope, transitionId: string) {
  return ["native-profile-handoff-transition", scope.workspace_id, scope.principal_id,
    scope.worker_item_id, transitionId] as const;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
