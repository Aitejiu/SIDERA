import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { MutableRefObject } from "react";

import { listProfiles, readDefaults, readProfile } from "@/features/agents/api";
import type { ProfileRef, ProfileView } from "@/features/agents/types";
import { assignmentQueryKey, assignmentTargetKey, readAssignment } from "@/features/assignments/api";
import { buildNativeCommand } from "@/features/assignments/commands";
import { extractRevisionAssignmentTargets, toPlanningAssigneeChoices } from "@/features/assignments/proposalAssignments";
import type {
  AgentBindingView,
  AssigneeValue,
  CommandRequest,
  CommandResponse,
  ProposalAssignmentTarget,
} from "@/features/assignments/types";
import { readLocalSession } from "@/features/local-session/api";
import type { LocalSession } from "@/features/local-session/api";
import { getTask, taskQueryKey } from "@/features/tasks/api";
import type { TaskView } from "@/features/tasks/types";
import { useNativePlanningCommand } from "./useNativePlanningCommand";
import type { PlanningMode } from "./nativePlanningReview";
import { detectPlanningMode } from "./nativePlanningReview";
import {
  getPlanRevisionDiff,
  getPlanningRequest,
  listPlanningRuns,
  planRevisionDiffQueryKey,
  planningRequestQueryKey,
  planningRunsQueryKey,
} from "./api";
import { reviewNativeRevision } from "./nativeRevisionReview";
import type { RevisionReviewBinding } from "./nativeRevisionReview";
import {
  loadPlanningDraft,
  planningDraftKey,
  savePlanningDraft,
} from "./planningConfirmationDraft";
import type {
  PlanningConfirmationDraft,
  RevisionPlanningDraftScope,
} from "./planningConfirmationDraft";
import { AssignmentUnavailableError } from "@/features/assignments/types";
import type { PlanningRequest, PlanningRun, RevisionDiff } from "./types";
import type { ReviseFailedPlanningRequestPayload } from "./types";
import { clearAppliedFailedPlanningFeedbackDraft } from "./failedPlanningFeedback.js";

export type NativeRevisionOutcome = "none" | "command_applied" | "plan_enabled" | "base_conflicted";

type DraftResult = {
  draft: PlanningConfirmationDraft | null;
  failure: string | null;
};

const DRAFT_QUERY_PREFIX = "native-planning-draft";
const SESSION_QUERY_PREFIX = "local-session";
const INVALID_SESSION = "需要当前工作区的有效本地会话";
const INVALID_REVIEW = "无法核实当前 Revision 事实";
const INVALID_DRAFT = "本地确认草稿不可用";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function useNativePlanRevision({
  workspaceId,
  rootTaskId,
  requestId,
  request,
  diff,
  rootTask,
  runs,
  onRefresh,
}: {
  workspaceId: string;
  rootTaskId: string;
  requestId: string;
  request: PlanningRequest | undefined;
  diff: RevisionDiff | undefined;
  rootTask: TaskView | undefined;
  runs: readonly PlanningRun[] | undefined;
  onRefresh: () => Promise<void>;
}) {
  const queryClient = useQueryClient();
  const currentRequest = request;
  const currentDiff = diff;
  const currentRootTask = rootTask;
  const currentRuns = runs;

  const storedNativeOriginal = hasPersistedRevisionOriginal(workspaceId, rootTaskId, requestId);
  const mode = readRevisionMode({
    workspaceId,
    requestId,
    request: currentRequest,
    runs: currentRuns,
    storedNativeOriginal,
  });
  const nativeHint = storedNativeOriginal
    || currentRequest?.profile?.execution_mode === "native_profile"
    || currentRuns?.some((run) => run.workspace_id === workspaceId
      && run.planning_request_id === requestId && run.execution_mode === "native_profile") === true;
  const sessionQuery = useQuery({
    queryKey: [SESSION_QUERY_PREFIX, workspaceId],
    queryFn: ({ signal }) => readLocalSession(signal),
    enabled: Boolean(workspaceId && nativeHint),
    retry: false,
  });
  const session = sessionQuery.data ?? null;
  const sessionValid = isValidSession(session, workspaceId);
  const scope = useMemo<RevisionPlanningDraftScope | null>(() => (
    sessionValid && session !== null && currentRequest?.kind === "revision"
      && currentRequest.workspace_id === workspaceId && currentRequest.id === requestId
      && nonblank(rootTaskId)
      ? {
        kind: "revision",
        workspace_id: workspaceId,
        request_id: requestId,
        principal_id: session.principal_id,
        root_task_id: rootTaskId,
      }
      : null
  ), [currentRequest, requestId, rootTaskId, session, sessionValid, workspaceId]);
  const scopeKey = scope === null ? "" : planningDraftKey(scope);
  const currentScopeKey = useRef(scopeKey);
  const eligibleSuggestionKeys = useRef(new Map<string, Set<string>>());
  const touchedChoiceKeys = useRef(new Map<string, Set<string>>());
  const [errors, setErrors] = useState<ReadonlyMap<string, string>>(() => new Map());
  const [busyRevisitKeys, setBusyRevisitKeys] = useState<ReadonlySet<string>>(() => new Set());
  const [outcomes, setOutcomes] = useState<ReadonlyMap<string, NativeRevisionOutcome>>(() => new Map());

  useLayoutEffect(() => {
    currentScopeKey.current = scopeKey;
  }, [scopeKey]);

  const draftQuery = useQuery({
    queryKey: [DRAFT_QUERY_PREFIX, scopeKey],
    queryFn: (): DraftResult => {
      if (scope === null) return { draft: null, failure: null };
      const loaded = loadPlanningDraft(scope);
      if (!loaded.ok) return { draft: null, failure: loaded.reason };
      return { draft: loaded.draft ?? emptyRevisionDraft(scope), failure: null };
    },
    enabled: scope !== null,
    staleTime: Infinity,
    retry: false,
  });
  const activeDraft = scopeKey === "" ? null : draftQuery.data?.draft ?? null;
  const draftFailure = scopeKey === "" ? null : draftQuery.data?.failure ?? null;

  const catalogQuery = useQuery({
    queryKey: ["agent-profiles", workspaceId],
    queryFn: ({ signal }) => listProfiles(workspaceId, signal),
    enabled: Boolean(workspaceId && mode === "native_profile" && sessionValid),
    retry: false,
  });
  const defaultsQuery = useQuery({
    queryKey: ["agent-defaults", workspaceId],
    queryFn: () => readDefaults(workspaceId),
    enabled: Boolean(workspaceId && mode === "native_profile" && sessionValid),
    retry: false,
  });

  const targets = useMemo<readonly ProposalAssignmentTarget[]>(() => {
    if (!nativeHint || mode === "legacy" || currentDiff === undefined) return [];
    try {
      return extractRevisionAssignmentTargets(currentDiff);
    } catch {
      return [];
    }
  }, [currentDiff, mode, nativeHint]);
  const reuseTargets = useMemo(() => targets.filter((target) => target.strategy === "reuse"), [targets]);
  const canReadAssignments = nativeHint && mode !== "legacy" && Boolean(workspaceId);
  const assignmentQueries = useQueries({
    queries: reuseTargets.map((target) => ({
      queryKey: assignmentQueryKey(workspaceId, target.kind, target.existingId ?? ""),
      queryFn: ({ signal }: { signal: AbortSignal }) => {
        if (target.existingId === null) throw new AssignmentUnavailableError();
        return readAssignment(workspaceId, target.kind, target.existingId, signal);
      },
      enabled: canReadAssignments && target.existingId !== null,
      retry: false,
    })),
  });
  const reusedBindings = useMemo(() => {
    const values = new Map<string, AgentBindingView>();
    reuseTargets.forEach((target, index) => {
      const value = assignmentQueries[index]?.data;
      if (target.existingId !== null && value !== undefined && value !== null) {
        values.set(assignmentTargetKey(target.kind, target.existingId), value);
      }
    });
    return values;
  }, [assignmentQueries, reuseTargets]);
  const reuseReadsReady = reuseTargets.every((_target, index) => assignmentQueries[index]?.isSuccess === true);

  const reviewResult = useMemo(() => {
    if (mode !== "native_profile" || currentRequest === undefined || currentDiff === undefined
      || currentRootTask === undefined || currentRuns === undefined || !reuseReadsReady) return null;
    return reviewNativeRevision({
      workspaceId,
      rootTaskId,
      requestId,
      request: currentRequest,
      diff: currentDiff,
      rootTask: currentRootTask,
      runs: currentRuns,
      reusedBindings,
    });
  }, [currentDiff, currentRequest, currentRootTask, currentRuns, mode, requestId, reuseReadsReady, reusedBindings, rootTaskId, workspaceId]);
  const freshReview = reviewResult?.ok === true ? reviewResult.binding : null;

  const saveEditorDraft = useCallback((next: PlanningConfirmationDraft): boolean => {
    if (scope === null || scopeKey === "" || currentScopeKey.current !== scopeKey
      || next.kind !== "revision" || next.workspace_id !== scope.workspace_id
      || next.request_id !== scope.request_id || next.principal_id !== scope.principal_id
      || next.root_task_id !== scope.root_task_id) return false;
    const saved = savePlanningDraft(scope, next);
    if (!saved.ok) {
      queryClient.setQueryData<DraftResult>([DRAFT_QUERY_PREFIX, scopeKey], (previous) => ({
        draft: previous?.draft ?? next,
        failure: saved.reason,
      }));
      return false;
    }
    queryClient.setQueryData<DraftResult>([DRAFT_QUERY_PREFIX, scopeKey], { draft: next, failure: null });
    return true;
  }, [queryClient, scope, scopeKey]);

  const saveObservedDraft = useCallback((next: PlanningConfirmationDraft): boolean => {
    if (scope === null || scopeKey === "" || currentScopeKey.current !== scopeKey
      || next.kind !== "revision" || next.workspace_id !== scope.workspace_id
      || next.request_id !== scope.request_id || next.principal_id !== scope.principal_id
      || next.root_task_id !== scope.root_task_id) return false;
    const saved = savePlanningDraft(scope, next);
    if (!saved.ok) {
      queryClient.setQueryData<DraftResult>([DRAFT_QUERY_PREFIX, scopeKey], (previous) => ({
        draft: previous?.draft ?? next,
        failure: saved.reason,
      }));
      return false;
    }
    queryClient.setQueryData<DraftResult>([DRAFT_QUERY_PREFIX, scopeKey], { draft: next, failure: null });
    return true;
  }, [queryClient, scope, scopeKey]);

  useEffect(() => {
    if (mode !== "native_profile" || scope === null || scopeKey === ""
      || activeDraft?.kind !== "revision" || activeDraft.review !== null
      || !(activeDraft.original === null && activeDraft.status === "idle"
        || activeDraft.original?.command_type === "ReviseFailedPlanningRequest" && activeDraft.status === "applied")
      || reviewResult?.ok !== true) return;
    eligibleSuggestionKeys.current.set(scopeKey, new Set(reviewResult.targets
      .filter((target) => target.strategy !== "reuse")
      .map((target) => assignmentTargetKey(target.kind, target.key))));
    saveEditorDraft({ ...activeDraft, review: reviewResult.binding });
  }, [activeDraft, mode, reviewResult, saveEditorDraft, scope, scopeKey]);

  const catalogProfiles = useMemo(() => (catalogQuery.data?.items ?? []).filter((item) => (
    item.profile.ref.workspace_id === workspaceId
    && item.status.profile_id === item.profile.ref.profile_id
  )), [catalogQuery.data, workspaceId]);
  const profileRefs = useMemo(() => {
    const refs = new Map<string, ProfileRef>();
    for (const [, value] of activeDraft?.choices ?? []) {
      if (value.kind === "profile") refs.set(profileRefKey(value.profileRef), value.profileRef);
    }
    for (const value of reusedBindings.values()) {
      if (value.profile_ref !== null) refs.set(profileRefKey(value.profile_ref), value.profile_ref);
    }
    return [...refs.values()];
  }, [activeDraft, reusedBindings]);
  const profileReadQueries = useQueries({
    queries: profileRefs.map((ref) => ({
      queryKey: ["agent-profile", ref.workspace_id, ref.profile_id, ref.revision],
      queryFn: ({ signal }: { signal: AbortSignal }) => readProfile(ref.workspace_id, ref.profile_id, ref.revision, signal),
      enabled: Boolean(nativeHint && mode !== "legacy" && sessionValid),
      retry: false,
    })),
  });
  const profiles = useMemo<ProfileView[]>(() => {
    const selectedProfileKeys = new Set(profileRefs.map(profileRefKey));
    return [
      ...catalogProfiles.filter((item) => !selectedProfileKeys.has(profileRefKey(item.profile.ref))),
      ...profileReadQueries.flatMap((query, index) => {
        const expected = profileRefs[index];
        const value = query.data;
        return expected !== undefined && value !== undefined && profileViewMatches(value, expected) ? [value] : [];
      }),
    ];
  }, [catalogProfiles, profileReadQueries, profileRefs]);
  const choices = useMemo(() => new Map(activeDraft?.choices ?? []), [activeDraft]);

  const suggestionProfile = useMemo(() => {
    const defaults = defaultsQuery.data;
    if (defaults?.workspace_id !== workspaceId || defaults.main_profile_id === null) return undefined;
    return catalogProfiles.find((item) => item.status.enabled
      && item.profile.ref.workspace_id === workspaceId
      && item.profile.ref.profile_id === defaults.main_profile_id);
  }, [catalogProfiles, defaultsQuery.data, workspaceId]);

  useEffect(() => {
    if (mode !== "native_profile" || scopeKey === "" || activeDraft?.kind !== "revision"
      || activeDraft.review === null || catalogQuery.isPending || defaultsQuery.isPending) return;
    const eligible = eligibleSuggestionKeys.current.get(scopeKey);
    if (eligible === undefined || eligible.size === 0 || suggestionProfile === undefined) return;
    const touched = touchedChoiceKeys.current.get(scopeKey) ?? new Set<string>();
    const nextChoices = new Map(activeDraft.choices);
    let changed = false;
    const ref = suggestionProfile.profile.ref;
    for (const key of eligible) {
      if (!nextChoices.has(key) && !touched.has(key)) {
        nextChoices.set(key, {
          kind: "profile",
          id: ref.profile_id,
          profileRef: { ...ref },
        });
        changed = true;
      }
      eligible.delete(key);
    }
    if (eligible.size === 0) eligibleSuggestionKeys.current.delete(scopeKey);
    if (changed) saveEditorDraft({ ...activeDraft, choices: [...nextChoices.entries()] });
  }, [activeDraft, catalogQuery.isPending, defaultsQuery.isPending, mode, saveEditorDraft, scopeKey, suggestionProfile]);

  const command = useNativePlanningCommand({
    scope,
    session: sessionValid ? session : null,
    draft: activeDraft,
    onDraftChange: saveObservedDraft,
    onApplied: async (original, result) => {
      await handleApplied(original, result);
    },
  });

  const setErrorFor = useCallback((key: string, value: string | null) => {
    setErrors((previous) => {
      const next = new Map(previous);
      if (value === null) next.delete(key);
      else next.set(key, value);
      return next;
    });
  }, []);
  const setOutcomeFor = useCallback((key: string, value: NativeRevisionOutcome) => {
    setOutcomes((previous) => {
      const next = new Map(previous);
      next.set(key, value);
      return next;
    });
  }, []);

  const missingReuseOwner = reuseTargets.find((target) => target.existingId === null
    || !reusedBindings.has(assignmentTargetKey(target.kind, target.existingId)));
  const ownerProblem = sessionValid && session !== null && targets.length > 0
    ? readAssigneeProblem(targets, choices, session, profiles, reusedBindings)
    : null;
  const missingOwnerReason = missingReuseOwner !== undefined
    ? `${missingReuseOwner.label} 的已保存负责人不可用`
    : ownerProblem;
  const reviewIsCurrent = freshReview !== null && activeDraft?.kind === "revision"
    && activeDraft.review !== null && sameRevisionReview(activeDraft.review, freshReview);
  const sourceFactsReady = mode === "native_profile" && currentRequest !== undefined
    && currentDiff !== undefined && currentRootTask !== undefined && currentRuns !== undefined && reuseReadsReady;
  const stale = activeDraft?.kind === "revision" && activeDraft.review !== null
    && sourceFactsReady && !reviewIsCurrent;
  const appliedRevision = command.status === "applied"
    && command.original?.command_type === "ConfirmPlanRevision";
  const submitOriginal = command.submitOriginal;
  const outcome = scopeKey === "" ? "none" : outcomes.get(scopeKey) ?? "none";

  const disabledReason = mode !== "native_profile"
    ? nativeHint ? "无法核实当前原生规划模式" : "当前不是原生规划模式"
    : !sessionValid || scope === null
      ? INVALID_SESSION
      : draftFailure
        ? draftFailure
        : activeDraft === null
          ? "正在恢复本地确认草稿"
          : reviewResult === null
            ? INVALID_REVIEW
            : reviewResult.ok === false
              ? reviewResult.reason
              : activeDraft.review === null
                ? "正在准备 Revision 审阅事实"
                : stale
                  ? "方案版本已变化，请重新查看最新 Revision。"
                  : missingOwnerReason ?? (command.unresolved
                    ? "请先核对原始命令结果"
                    : appliedRevision
                      ? "原始确认已生效，不能再次确认"
                      : command.busy || busyRevisitKeys.has(scopeKey)
                        ? "正在处理 Revision"
                        : undefined);
  const canConfirm = mode === "native_profile" && sessionValid && scope !== null
    && draftFailure === null && activeDraft?.kind === "revision"
    && activeDraft.review !== null && reviewIsCurrent && !command.unresolved
    && !command.busy && !busyRevisitKeys.has(scopeKey) && !appliedRevision
    && missingOwnerReason === null;

  const failedFeedbackDisabledReason = mode !== "native_profile" ? "无法核实当前原生规划模式"
    : !sessionValid || scope === null ? INVALID_SESSION
      : draftFailure ?? (activeDraft === null ? "正在恢复本地确认草稿"
        : command.busy || command.unresolved ? "请先核对原始命令结果" : undefined);
  const submitFailedFeedback = useCallback(async (expectedVersion: number, payload: ReviseFailedPlanningRequestPayload) => {
    if (mode !== "native_profile" || scope === null || session === null || !isValidSession(session, workspaceId)
      || activeDraft?.kind !== "revision" || draftFailure !== null || command.busy || command.unresolved) return;
    try {
      const original = buildNativeCommand({ session, workspaceId, commandType: "ReviseFailedPlanningRequest",
        targetType: "planning_request", targetId: requestId, expectedVersion, payload });
      setErrorFor(scopeKey, null);
      await submitOriginal(original);
    } catch { setErrorFor(scopeKey, "当前本地会话或命令内容不可用"); }
  }, [activeDraft, command.busy, command.unresolved, draftFailure, mode, requestId, scope, scopeKey, session, setErrorFor, submitOriginal, workspaceId]);

  const onChoiceChange = useCallback((key: string, value: AssigneeValue | null) => {
    if (scopeKey === "" || scope === null || activeDraft?.kind !== "revision"
      || activeDraft.review === null || command.unresolved || command.busy || appliedRevision) return;
    const target = targets.find((item) => assignmentTargetKey(item.kind, item.key) === key);
    if (target === undefined || target.strategy === "reuse") return;
    const touched = touchedChoiceKeys.current.get(scopeKey) ?? new Set<string>();
    touched.add(key);
    touchedChoiceKeys.current.set(scopeKey, touched);
    const nextChoices = new Map(activeDraft.choices);
    if (value === null) nextChoices.delete(key);
    else nextChoices.set(key, value);
    if (saveEditorDraft({ ...activeDraft, choices: [...nextChoices.entries()] })) setErrorFor(scopeKey, null);
  }, [activeDraft, appliedRevision, command.busy, command.unresolved, saveEditorDraft, scope, scopeKey, setErrorFor, targets]);

  const confirm = useCallback(async () => {
    if (!canConfirm || scope === null || session === null || !sessionValid
      || activeDraft?.kind !== "revision" || freshReview === null) {
      if (scopeKey !== "") setErrorFor(scopeKey, disabledReason ?? INVALID_REVIEW);
      return;
    }
    try {
      const payload = {
        expected_proposal_revision: freshReview.proposalRevision,
        expected_proposal_content_hash: freshReview.proposalContentHash,
        expected_diff_hash: freshReview.diffHash,
        expected_base_snapshot_id: freshReview.baseSnapshotId,
        expected_base_content_hash: freshReview.baseContentHash,
        profile_id: freshReview.profileId,
        profile_version: freshReview.profileVersion,
        assignee_bindings: toPlanningAssigneeChoices({
          targets,
          choices,
          session,
          permittedProfiles: profiles,
          reusedBindings,
        }),
      };
      const original = buildNativeCommand({
        session,
        workspaceId,
        commandType: "ConfirmPlanRevision",
        targetType: "planning_request",
        targetId: requestId,
        expectedVersion: freshReview.requestVersion,
        payload,
      });
      setErrorFor(scopeKey, null);
      await submitOriginal(original);
    } catch {
      setErrorFor(scopeKey, "负责人选择或本地会话不可用");
    }
  }, [activeDraft, canConfirm, choices, disabledReason, freshReview, profiles, requestId, reusedBindings, scope, scopeKey, session, sessionValid, setErrorFor, submitOriginal, targets, workspaceId]);

  const revisit = useCallback(async () => {
    const activeScope = scope;
    const key = scopeKey;
    if (activeScope === null || key === "" || !sessionValid || session === null
      || mode !== "native_profile" || activeDraft?.kind !== "revision"
      || command.unresolved || command.busy || appliedRevision) {
      if (key !== "") setErrorFor(key, command.unresolved
        ? "请先核对原始命令结果"
        : "当前无法重新审阅 Revision 事实");
      return;
    }
    setBusyRevisitKeys((previous) => new Set(previous).add(key));
    setErrorFor(key, null);
    try {
      const [nextRequest, nextRunsPage, nextDiff, nextRootTask, nextProfiles, nextDefaults] = await Promise.all([
        getPlanningRequest(activeScope.workspace_id, activeScope.request_id),
        listPlanningRuns(activeScope.workspace_id, activeScope.request_id),
        getPlanRevisionDiff(activeScope.workspace_id, activeScope.request_id),
        getTask(activeScope.workspace_id, activeScope.root_task_id),
        listProfiles(activeScope.workspace_id).catch(() => null),
        readDefaults(activeScope.workspace_id).catch(() => null),
      ]);
      if (currentScopeKey.current !== key) return;
      const nextRuns = nextRunsPage.items;
      const nextTargets = extractRevisionAssignmentTargets(nextDiff);
      const nextReuseTargets = nextTargets.filter((target) => target.strategy === "reuse");
      const bindings = await Promise.all(nextReuseTargets.map(async (target) => {
        if (target.existingId === null) throw new AssignmentUnavailableError();
        return {
          target,
          binding: await readAssignment(activeScope.workspace_id, target.kind, target.existingId),
        };
      }));
      if (currentScopeKey.current !== key) return;
      const nextReusedBindings = new Map<string, AgentBindingView>();
      for (const { target, binding: nextBinding } of bindings) {
        const targetId = target.existingId;
        if (targetId !== null && nextBinding !== null) {
          nextReusedBindings.set(assignmentTargetKey(target.kind, targetId), nextBinding);
          queryClient.setQueryData(assignmentQueryKey(activeScope.workspace_id, target.kind, targetId), nextBinding);
        } else if (targetId !== null) {
          queryClient.setQueryData(assignmentQueryKey(activeScope.workspace_id, target.kind, targetId), null);
        }
      }
      const reviewed = reviewNativeRevision({
        workspaceId: activeScope.workspace_id,
        rootTaskId: activeScope.root_task_id,
        requestId: activeScope.request_id,
        request: nextRequest,
        diff: nextDiff,
        rootTask: nextRootTask,
        runs: nextRuns,
        reusedBindings: nextReusedBindings,
      });
      queryClient.setQueryData(planningRequestQueryKey(activeScope.workspace_id, activeScope.request_id), nextRequest);
      queryClient.setQueryData(planningRunsQueryKey(activeScope.workspace_id, activeScope.request_id), nextRunsPage);
      queryClient.setQueryData(planRevisionDiffQueryKey(activeScope.workspace_id, activeScope.request_id), nextDiff);
      queryClient.setQueryData(taskQueryKey(activeScope.workspace_id, activeScope.root_task_id), nextRootTask);
      if (nextProfiles !== null) queryClient.setQueryData(["agent-profiles", activeScope.workspace_id], nextProfiles);
      if (nextDefaults !== null) queryClient.setQueryData(["agent-defaults", activeScope.workspace_id], nextDefaults);
      if (!reviewed.ok) {
        setErrorFor(key, reviewed.reason);
        await onRefresh();
        return;
      }

      const priorTargets = activeDraft.review === null
        ? new Set<string>()
        : targetKeysFromFingerprint(activeDraft.review.targetsFingerprint);
      const compatibleNewTargets = reviewed.targets.filter((target) => target.strategy !== "reuse");
      const compatibleKeys = new Set(compatibleNewTargets.map((target) => assignmentTargetKey(target.kind, target.key)));
      const retained = new Map(activeDraft.choices.filter(([choiceKey]) => compatibleKeys.has(choiceKey)));
      const eligible = new Set([...compatibleKeys].filter((targetKey) => !priorTargets.has(targetKey)));
      const touched = touchedChoiceKeys.current.get(key);
      if (touched !== undefined) for (const targetKey of touched) eligible.delete(targetKey);
      eligibleSuggestionKeys.current.set(key, eligible);
      if (!saveEditorDraft({ ...activeDraft, review: reviewed.binding, choices: [...retained.entries()] })) {
        setErrorFor(key, INVALID_DRAFT);
        return;
      }
      setOutcomeFor(key, "none");
      await onRefresh();
      if (currentScopeKey.current === key) setErrorFor(key, null);
    } catch {
      if (currentScopeKey.current === key) setErrorFor(key, "无法重新读取 Revision 事实或负责人绑定");
    } finally {
      setBusyRevisitKeys((previous) => {
        const next = new Set(previous);
        next.delete(key);
        return next;
      });
    }
  }, [activeDraft, appliedRevision, command.busy, command.unresolved, mode, onRefresh, queryClient, saveEditorDraft, scope, scopeKey, session, sessionValid, setErrorFor, setOutcomeFor]);

  const onSessionAuthorized = useCallback((nextSession: LocalSession) => {
    queryClient.setQueryData([SESSION_QUERY_PREFIX, workspaceId], nextSession);
  }, [queryClient, workspaceId]);

  return {
    scope,
    failedFeedbackDisabledReason,
    submitFailedFeedback,
    mode,
    session,
    targets,
    profiles,
    choices,
    reusedBindings,
    onChoiceChange,
    confirm,
    revisit,
    onSessionAuthorized,
    canConfirm,
    disabledReason,
    error: scopeKey === "" ? null : errors.get(scopeKey) ?? command.error ?? draftFailure,
    command,
    outcome,
    stale,
    missingOwnerReason,
    reviewedBinding: activeDraft?.kind === "revision" ? activeDraft.review : null,
    nativeHint,
    sessionValid,
    revisitBusy: scopeKey !== "" && busyRevisitKeys.has(scopeKey),
    draft: activeDraft,
  };

  async function handleApplied(original: CommandRequest, result: CommandResponse | null): Promise<void> {
    if (original.command_type === "ReviseFailedPlanningRequest") {
      if (scope === null || scopeKey === "" || currentScopeKey.current !== scopeKey) return;
      const cleared = clearAppliedFailedPlanningFeedbackDraft(scope, original);
      if (!cleared.ok) setErrorFor(scopeKey, cleared.reason);
      await onRefresh();
      return;
    }
    return applyRevisionOutcome({
      original,
      result,
      scope,
      scopeKey,
      currentScopeKey,
      activeDraft,
      currentDiff,
      queryClient,
      onRefresh,
      setErrorFor,
      setOutcomeFor,
    });
  }
}

async function applyRevisionOutcome({
  original,
  result,
  scope,
  scopeKey,
  currentScopeKey,
  activeDraft,
  currentDiff,
  queryClient,
  onRefresh,
  setErrorFor,
  setOutcomeFor,
}: {
  original: CommandRequest;
  result: CommandResponse | null;
  scope: RevisionPlanningDraftScope | null;
  scopeKey: string;
  currentScopeKey: MutableRefObject<string>;
  activeDraft: PlanningConfirmationDraft | null;
  currentDiff: RevisionDiff | undefined;
  queryClient: ReturnType<typeof useQueryClient>;
  onRefresh: () => Promise<void>;
  setErrorFor: (key: string, error: string | null) => void;
  setOutcomeFor: (key: string, outcome: NativeRevisionOutcome) => void;
}): Promise<void> {
  if (scope === null || scopeKey === "" || original.command_type !== "ConfirmPlanRevision"
    || currentScopeKey.current !== scopeKey) return;
  const data = isRecord(result?.data) ? result.data : null;
  if (data?.status === "conflicted" && data.reason === "stale_base"
    && data.planning_request_id === scope.request_id) {
    setOutcomeFor(scopeKey, "base_conflicted");
    try {
      await onRefresh();
    } catch {
      if (currentScopeKey.current === scopeKey) setErrorFor(scopeKey, "无法刷新冲突后的 Revision 状态");
    }
    return;
  }

  setOutcomeFor(scopeKey, "command_applied");
  try {
    await onRefresh();
  } catch {
    if (currentScopeKey.current === scopeKey) setErrorFor(scopeKey, "命令已处理，但页面状态未能刷新");
    return;
  }
  if (currentScopeKey.current !== scopeKey || data === null) return;
  const review = activeDraft?.kind === "revision" ? activeDraft.review : null;
  if (review === null || review === undefined) return;
  const payload = original.payload;
  const expectedGeneration = currentDiff?.diff_hash === review.diffHash
    ? currentDiff.target_descriptor_preview.target_generation
    : review.baseGeneration + 1;
  if (data.planning_request_id !== scope.request_id || data.root_task_id !== scope.root_task_id
    || data.proposal_revision !== payload.expected_proposal_revision
    || data.proposal_content_hash !== payload.expected_proposal_content_hash
    || data.diff_hash !== payload.expected_diff_hash || data.profile_id !== payload.profile_id
    || data.profile_version !== payload.profile_version
    || !UUID.test(String(data.plan_snapshot_id)) || !UUID.test(String(data.plan_transition_id))
    || !positiveInteger(data.plan_generation) || data.plan_generation !== expectedGeneration) return;

  const canonical = queryClient.getQueryData<PlanningRequest>(
    planningRequestQueryKey(scope.workspace_id, scope.request_id),
  );
  if (canonical?.kind !== "revision" || canonical.status !== "materialized"
    || canonical.id !== scope.request_id || canonical.workspace_id !== scope.workspace_id
    || !positiveInteger(original.expected_version)
    || canonical.version <= original.expected_version
    || canonical.planning_profile_id !== payload.profile_id
    || canonical.planning_profile_version !== payload.profile_version
    || canonical.current_proposal_revision !== payload.expected_proposal_revision
    || canonical.current_proposal_content_hash !== payload.expected_proposal_content_hash
    || canonical.revision_base?.workspace_id !== scope.workspace_id
    || canonical.revision_base.root_task_id !== scope.root_task_id
    || canonical.revision_base.plan_snapshot_id !== payload.expected_base_snapshot_id
    || canonical.revision_base.plan_content_hash !== payload.expected_base_content_hash
    || canonical.revision_base.plan_generation !== review.baseGeneration
    || canonical.revision_base.root_task_version !== review.baseRootTaskVersion
    || canonical.revision_base.schema_version !== 1) return;
  setOutcomeFor(scopeKey, "plan_enabled");
}

function readRevisionMode({
  workspaceId,
  requestId,
  request,
  runs,
  storedNativeOriginal,
}: {
  workspaceId: string;
  requestId: string;
  request: PlanningRequest | undefined;
  runs: readonly PlanningRun[] | undefined;
  storedNativeOriginal: boolean;
}): PlanningMode {
  if (request === undefined || !canonicalRunList(workspaceId, requestId, runs)) return "unavailable";
  const hasNativeRun = runs.some((run) => run.execution_mode === "native_profile");
  const historicalLegacyShape = request.kind === "revision"
    && request.workspace_id === workspaceId && request.id === requestId
    && request.profile === null && request.current_run_id === null && request.active_run === null;
  const mode = historicalLegacyShape ? "legacy" : detectPlanningMode(request, runs);
  return mode === "legacy" && (storedNativeOriginal || hasNativeRun) ? "unavailable" : mode;
}

function canonicalRunList(
  workspaceId: string,
  requestId: string,
  runs: readonly PlanningRun[] | undefined,
): runs is readonly PlanningRun[] {
  if (!Array.isArray(runs)) return false;
  const seen = new Set<string>();
  for (const run of runs) {
    if (!isRecord(run) || !nonblank(run.id)
      || run.workspace_id !== workspaceId || run.planning_request_id !== requestId
      || (run.execution_mode !== "legacy" && run.execution_mode !== "native_profile")
      || !positiveInteger(run.version) || seen.has(run.id)) return false;
    seen.add(run.id);
  }
  return true;
}

function hasPersistedRevisionOriginal(workspaceId: string, rootTaskId: string, requestId: string): boolean {
  if (typeof window === "undefined") return true;
  const prefix = `sidera.native-planning.confirmation.v1:${encodeURIComponent(workspaceId)}:${encodeURIComponent(requestId)}:`;
  const suffix = `:revision:${encodeURIComponent(rootTaskId)}`;
  try {
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (key === null || !key.startsWith(prefix) || !key.endsWith(suffix)) continue;
      const serialized = window.localStorage.getItem(key);
      if (serialized === null) continue;
      let value: unknown;
      try { value = JSON.parse(serialized); } catch { return true; }
      if (isRecord(value) && value.kind === "revision" && value.workspace_id === workspaceId
        && value.root_task_id === rootTaskId && value.request_id === requestId
        && isRecord(value.original) && (value.original.command_type === "ConfirmPlanRevision"
          || value.original.command_type === "ReviseFailedPlanningRequest")) return true;
    }
    return false;
  } catch {
    return true;
  }
}

function emptyRevisionDraft(scope: RevisionPlanningDraftScope): PlanningConfirmationDraft {
  return {
    ...scope,
    schema_version: 1,
    review: null,
    choices: [],
    original: null,
    status: "idle",
  };
}

function isValidSession(session: LocalSession | null, workspaceId: string): session is LocalSession {
  if (session === null || session.workspace_id !== workspaceId
    || !nonblank(session.session_id) || !nonblank(session.principal_id)) return false;
  const expiry = Date.parse(session.expires_at);
  return Number.isFinite(expiry) && expiry > Date.now();
}

function profileRefKey(ref: ProfileRef): string {
  return JSON.stringify([ref.workspace_id, ref.profile_id, ref.revision]);
}

function profileViewMatches(value: ProfileView, ref: ProfileRef): boolean {
  return value.profile.ref.workspace_id === ref.workspace_id
    && value.profile.ref.profile_id === ref.profile_id
    && value.profile.ref.revision === ref.revision
    && value.status.profile_id === ref.profile_id;
}

function sameRevisionReview(left: RevisionReviewBinding, right: RevisionReviewBinding): boolean {
  return left.requestVersion === right.requestVersion
    && left.proposalRevision === right.proposalRevision
    && left.proposalContentHash === right.proposalContentHash
    && left.profileId === right.profileId
    && left.profileVersion === right.profileVersion
    && left.rootTaskId === right.rootTaskId
    && left.diffHash === right.diffHash
    && left.baseSnapshotId === right.baseSnapshotId
    && left.baseContentHash === right.baseContentHash
    && left.baseGeneration === right.baseGeneration
    && left.baseRootTaskVersion === right.baseRootTaskVersion
    && left.targetsFingerprint === right.targetsFingerprint
    && left.reusedBindingsFingerprint === right.reusedBindingsFingerprint;
}

function targetKeysFromFingerprint(value: string): Set<string> {
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.flatMap((target) => isRecord(target)
      && (target.kind === "subtask" || target.kind === "worker_item")
      && nonblank(target.key)
      ? [assignmentTargetKey(target.kind, target.key)]
      : []));
  } catch {
    return new Set();
  }
}

function readAssigneeProblem(
  targets: readonly ProposalAssignmentTarget[],
  choices: ReadonlyMap<string, AssigneeValue>,
  session: LocalSession,
  profiles: readonly ProfileView[],
  reusedBindings: ReadonlyMap<string, AgentBindingView>,
): string | null {
  for (const target of targets) {
    if (target.strategy === "reuse") continue;
    const value = choices.get(assignmentTargetKey(target.kind, target.key));
    if (value === undefined) return `${target.label} 尚未分配负责人`;
    if (value.kind === "human") {
      if (value.id !== session.principal_id || value.profileRef !== null) return `${target.label} 的负责人不可用`;
      continue;
    }
    const exactProfile = profiles.find((profile) => profile.status.enabled
      && profile.profile.ref.workspace_id === value.profileRef.workspace_id
      && profile.profile.ref.profile_id === value.profileRef.profile_id
      && profile.profile.ref.revision === value.profileRef.revision
      && value.profileRef.workspace_id === session.workspace_id
      && value.id === value.profileRef.profile_id);
    if (exactProfile === undefined) return `${target.label} 的 Profile R${value.profileRef.revision} 不可用`;
  }
  try {
    toPlanningAssigneeChoices({
      targets,
      choices,
      session,
      permittedProfiles: profiles,
      reusedBindings,
    });
    return null;
  } catch {
    return "负责人选择无效";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function positiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) > 0;
}
