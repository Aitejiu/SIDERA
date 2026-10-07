import { AlertTriangle, ArrowLeft, MessageCircle, X } from "lucide-react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiProblem } from "@/shared/api/client";
import { useWorkspaceStreamOptional } from "@/app/WorkspaceStreamProvider";
import { LocalSessionButton } from "@/features/local-session/LocalSessionDialog";
import { readLocalSession, type LocalSession } from "@/features/local-session/api";
import { listProfiles, readDefaults, readProfile } from "@/features/agents/api";
import type { ProfileRef, ProfileView } from "@/features/agents/types";
import { buildNativeCommand } from "@/features/assignments/commands";
import { toPlanningAssigneeChoices } from "@/features/assignments/proposalAssignments";
import {
  type AssigneeValue,
  type CommandRequest as NativeCommandRequest,
  type CommandResponse as NativeCommandResponse,
  type ProposalAssignmentTarget,
} from "@/features/assignments/types";
import { assignmentTargetKey as assignmentKey } from "@/features/assignments/api";
import { ProposalAssignees } from "@/features/assignments/ProposalAssignees";

import {
  buildAnswerPlanningClarificationCommand,
  buildCancelPlanningRequestCommand,
  buildConfirmPlanningProposalCommand,
  buildRejectPlanningProposalCommand,
  buildRequestProposalRevisionCommand,
  executePlanningCommand,
  getPlanningRequest,
  getPlanningProposal,
  listPlanningBriefs,
  listPlanningClarifications,
  listPlanningRuns,
  planningBriefsQueryKey,
  planningClarificationsQueryKey,
  planningRequestQueryKey,
  planningProposalQueryKey,
  planningRunsQueryKey,
} from "./api";
import { ClarificationPanel } from "./ClarificationPanel";
import { PlanningInspector } from "./PlanningInspector";
import { PlanningRunStatus } from "./PlanningRunStatus";
import { ProposalFeedbackComposer } from "./ProposalFeedbackComposer";
import { ProposalView } from "./ProposalView";
import { detectPlanningMode, reviewInitialNativeProposal } from "./nativePlanningReview";
import { loadPlanningDraft, planningDraftKey, savePlanningDraft } from "./planningConfirmationDraft";
import type { PlanningConfirmationDraft, PlanningDraftScope } from "./planningConfirmationDraft";
import { useNativePlanningCommand } from "./useNativePlanningCommand";
import type { PlanningClarificationAnswer, PlanningCommandRequest, PlanningCommandResponse } from "./types";
import type { ProposalReviewBinding } from "./ProposalActions";
import type { PlanningMode } from "./nativePlanningReview";

type Confirmation = "cancel" | "reject" | null;

export function PlanningPage() {
  const {
    workspaceId = "",
    planningRequestId = "",
  } = useParams<{ workspaceId: string; planningRequestId: string }>();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const stream = useWorkspaceStreamOptional();
  const [confirmation, setConfirmation] = useState<Confirmation>(null);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackLockRevision, setFeedbackLockRevision] = useState<number | null>(null);
  const [pendingCommand, setPendingCommand] = useState<string | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [proposalViewNonce, setProposalViewNonce] = useState(0);

  const requestQuery = useQuery({
    queryKey: planningRequestQueryKey(workspaceId, planningRequestId),
    queryFn: ({ signal }) => getPlanningRequest(workspaceId, planningRequestId, signal),
    enabled: Boolean(workspaceId && planningRequestId),
  });
  const runsQuery = useQuery({
    queryKey: planningRunsQueryKey(workspaceId, planningRequestId),
    queryFn: ({ signal }) => listPlanningRuns(workspaceId, planningRequestId, signal),
    enabled: Boolean(workspaceId && planningRequestId),
  });
  const briefsQuery = useQuery({
    queryKey: planningBriefsQueryKey(workspaceId, planningRequestId),
    queryFn: ({ signal }) => listPlanningBriefs(workspaceId, planningRequestId, signal),
    enabled: Boolean(workspaceId && planningRequestId),
  });
  const clarificationsQuery = useQuery({
    queryKey: planningClarificationsQueryKey(workspaceId, planningRequestId),
    queryFn: ({ signal }) => listPlanningClarifications(workspaceId, planningRequestId, signal),
    enabled: Boolean(workspaceId && planningRequestId),
  });
  const proposalRevision = requestQuery.data?.current_proposal_revision ?? null;
  const proposalQuery = useQuery({
    queryKey: proposalRevision === null
      ? ["planning-proposal", workspaceId, planningRequestId, "none"] as const
      : planningProposalQueryKey(workspaceId, planningRequestId, proposalRevision),
    queryFn: ({ signal }) => getPlanningProposal(workspaceId, planningRequestId, proposalRevision ?? 0, signal),
    enabled: requestQuery.data?.status === "proposal_ready" && proposalRevision !== null,
  });

  const requestData = requestQuery.data;
  const planningMode = requestData === undefined
    ? "unavailable"
    : detectPlanningMode(requestData, runsQuery.data?.items ?? []);
  const hasNativeHint = requestData?.profile?.execution_mode === "native_profile"
    || (requestData?.current_run_id !== null && requestData?.current_run_id !== undefined
      && runsQuery.data?.items.some((run) => run.id === requestData.current_run_id && run.execution_mode === "native_profile") === true);
  const sessionQuery = useQuery({
    queryKey: ["local-session", workspaceId],
    queryFn: ({ signal }) => readLocalSession(signal),
    enabled: Boolean(workspaceId && requestData?.kind === "create" && hasNativeHint),
    retry: false,
  });
  const session = sessionQuery.data ?? null;
  const currentSessionValid = isValidLocalSession(session, workspaceId);
  const scope = useMemo<PlanningDraftScope | null>(() => (
    requestData?.kind === "create" && currentSessionValid && session !== null
      ? {
        kind: "create",
        workspace_id: workspaceId,
        request_id: planningRequestId,
        principal_id: session.principal_id,
      }
      : null
  ), [currentSessionValid, planningRequestId, requestData?.kind, session, workspaceId]);
  const scopeKey = scope === null ? "" : planningDraftKey(scope);
  const currentScopeKey = useRef(scopeKey);
  useLayoutEffect(() => {
    currentScopeKey.current = scopeKey;
  }, [scopeKey]);
  const draftQuery = useQuery({
    queryKey: ["native-planning-draft", scopeKey],
    queryFn: () => {
      if (scope === null) return { draft: null, failure: null };
      const loaded = loadPlanningDraft(scope);
      if (!loaded.ok) return { draft: null, failure: loaded.reason };
      return { draft: loaded.draft ?? emptyPlanningDraft(scope), failure: null };
    },
    enabled: scope !== null,
    staleTime: Infinity,
    retry: false,
  });
  const activeDraft = scopeKey === "" ? null : draftQuery.data?.draft ?? null;
  const activeDraftFailure = scopeKey === "" ? null : draftQuery.data?.failure ?? null;
  const catalogQuery = useQuery({
    queryKey: ["agent-profiles", workspaceId],
    queryFn: ({ signal }) => listProfiles(workspaceId, signal),
    enabled: Boolean(workspaceId && planningMode === "native_profile"),
    retry: false,
  });
  const defaultsQuery = useQuery({
    queryKey: ["agent-defaults", workspaceId],
    queryFn: () => readDefaults(workspaceId),
    enabled: Boolean(workspaceId && planningMode === "native_profile"),
    retry: false,
  });
  const selectedProfileRefs = useMemo(() => {
    const refs = new Map<string, ProfileRef>();
    for (const [, value] of activeDraft?.choices ?? []) {
      if (value.kind === "profile") {
        refs.set(profileRefKey(value.profileRef), value.profileRef);
      }
    }
    return [...refs.values()];
  }, [activeDraft]);
  const selectedProfileKeys = selectedProfileRefs.map(profileRefKey);
  const profileReadQueries = useQueries({
    queries: selectedProfileRefs.map((ref) => ({
      queryKey: ["agent-profile", ref.workspace_id, ref.profile_id, ref.revision],
      queryFn: ({ signal }: { signal: AbortSignal }) => readProfile(ref.workspace_id, ref.profile_id, ref.revision, signal),
      enabled: planningMode === "native_profile",
      retry: false,
    })),
  });
  const catalogProfiles = (catalogQuery.data?.items ?? []).filter((item) => (
    item.profile.ref.workspace_id === workspaceId
    && item.status.profile_id === item.profile.ref.profile_id
  ));
  const nativeProfiles: ProfileView[] = [
    ...catalogProfiles.filter((item) => !selectedProfileKeys.includes(profileRefKey(item.profile.ref))),
    ...profileReadQueries.flatMap((query, index) => {
      const expected = selectedProfileRefs[index];
      const value = query.data;
      return expected !== undefined && value !== undefined && profileViewMatches(value, expected) ? [value] : [];
    }),
  ];
  const nativeReview = requestData !== undefined && proposalQuery.data !== undefined
    ? reviewInitialNativeProposal({
      workspaceId,
      requestId: planningRequestId,
      request: requestData,
      proposal: proposalQuery.data,
      runs: runsQuery.data?.items ?? [],
    })
    : null;

  const saveEditorDraft = useCallback((next: PlanningConfirmationDraft): boolean => {
    if (scope === null || scopeKey === "" || next.workspace_id !== scope.workspace_id
      || next.request_id !== scope.request_id || next.principal_id !== scope.principal_id) return false;
    const result = savePlanningDraft(scope, next);
    queryClient.setQueryData(["native-planning-draft", scopeKey], {
      draft: next,
      failure: result.ok ? null : result.reason,
    });
    return result.ok;
  }, [queryClient, scope, scopeKey]);

  const saveObservedDraft = useCallback((next: PlanningConfirmationDraft): boolean => {
    if (scope === null || scopeKey === "" || next.workspace_id !== scope.workspace_id
      || next.request_id !== scope.request_id || next.principal_id !== scope.principal_id) return false;
    const result = savePlanningDraft(scope, next);
    if (!result.ok) {
      queryClient.setQueryData<{ draft: PlanningConfirmationDraft | null; failure: string | null }>(
        ["native-planning-draft", scopeKey],
        (previous) => ({ draft: previous?.draft ?? next, failure: result.reason }),
      );
      return false;
    }
    queryClient.setQueryData(["native-planning-draft", scopeKey], { draft: next, failure: null });
    return true;
  }, [queryClient, scope, scopeKey]);

  const nativeCommand = useNativePlanningCommand({
    scope,
    session: currentSessionValid ? session : null,
    draft: activeDraft,
    onDraftChange: saveObservedDraft,
    onApplied: handleNativeApplied,
  });

  useEffect(() => {
    if (planningMode !== "native_profile" || scope === null || scopeKey === ""
      || activeDraft === null || activeDraft.kind !== "create" || activeDraftFailure !== null
      || activeDraft.review !== null || activeDraft.original !== null || activeDraft.status !== "idle"
      || nativeReview === null || !nativeReview.ok
      || !catalogQuery.isFetched || !defaultsQuery.isFetched) return;

    const choices = new Map(activeDraft.choices);
    const defaults = defaultsQuery.data;
    const defaultProfile = defaults?.workspace_id === workspaceId && defaults.main_profile_id !== null
      ? catalogProfiles.find((item) => item.status.enabled
        && item.profile.ref.workspace_id === workspaceId
        && item.profile.ref.profile_id === defaults.main_profile_id)
      : undefined;
    if (defaultProfile !== undefined) {
      const ref = defaultProfile.profile.ref;
      for (const target of nativeReview.targets) {
        const key = assignmentKey(target.kind, target.key);
        if (target.strategy !== "reuse" && !choices.has(key)) {
          choices.set(key, {
            kind: "profile",
            id: ref.profile_id,
            profileRef: { ...ref },
          });
        }
      }
    }
    saveEditorDraft({
      ...activeDraft,
      review: nativeReview.binding,
      choices: [...choices.entries()],
    });
  }, [
    activeDraft,
    activeDraftFailure,
    catalogProfiles,
    catalogQuery.isFetched,
    defaultsQuery.data,
    defaultsQuery.isFetched,
    nativeReview,
    planningMode,
    saveEditorDraft,
    scope,
    scopeKey,
    workspaceId,
  ]);

  useEffect(() => {
    const cursor = requestData?.last_change_cursor;
    if (stream !== null && cursor !== undefined) {
      stream.seedSnapshotCursor(cursor);
    }
  }, [requestData?.last_change_cursor, stream]);

  if (requestData === undefined) {
    if (requestQuery.isPending) {
      return <PlanningPageState title="正在读取规划请求" detail="正在恢复请求状态和历史记录。" />;
    }
    return (
      <PlanningPageState
        title="无法读取规划请求"
        detail="控制面没有返回可用的请求状态。"
        error
        onRetry={() => void requestQuery.refetch()}
      />
    );
  }

  const request = requestData;
  const activeRun = request.active_run ?? runsQuery.data?.items.at(-1) ?? null;
  const previousRun = activeRun === null
    ? null
    : runsQuery.data?.items.filter((run) => run.id !== activeRun.id).at(-1) ?? null;
  const clarifications = request.pending_clarifications.length > 0
    ? request.pending_clarifications
    : clarificationsQuery.data?.items.filter((item) => item.answer === null) ?? [];
  const latestBrief = briefsQuery.data?.items.at(-1);
  const canCancel = request.status === "submitted"
    || request.status === "planning"
    || request.status === "waiting_for_user";
  const canRevise = request.status === "proposal_ready"
    && request.current_proposal_revision !== null
    && request.current_proposal_content_hash !== null;
  const isTerminal = ["materialized", "rejected", "cancelled"].includes(request.status);

  const choices = new Map(activeDraft?.choices ?? []);
  const assignmentProblem = planningMode === "native_profile" && nativeReview?.ok === true
    && currentSessionValid && session !== null
    ? readAssigneeProblem(nativeReview.targets, choices, session, nativeProfiles)
    : null;
  const unresolvedNativeCommand = nativeCommand.unresolved;
  const appliedConfirmation = nativeCommand.status === "applied"
    && nativeCommand.original?.command_type === "ConfirmPlanningProposal";
  const ownerControlsDisabled = !currentSessionValid || scope === null || activeDraft === null
    || activeDraftFailure !== null || unresolvedNativeCommand || appliedConfirmation;
  const nativeConfirmDisabledReason = (planningMode !== "native_profile"
    ? "无法核实当前规划模式"
    : !currentSessionValid || scope === null
      ? "需要当前工作区的有效本地会话"
      : activeDraftFailure
        ? activeDraftFailure
        : activeDraft === null
          ? "正在恢复本地确认草稿"
          : nativeReview === null || !nativeReview.ok
            ? nativeReview?.reason ?? "正在读取方案事实"
            : activeDraft.review === null
              ? "正在准备方案审阅事实"
              : unresolvedNativeCommand
                ? "请先核对原始命令结果"
                : appliedConfirmation
                  ? "原始确认已生效，不能再次确认"
                  : assignmentProblem) ?? undefined;

  const responsibilityContent = planningMode === "native_profile"
    && nativeReview?.ok === true && activeDraft !== null && activeDraft.review !== null
    ? (
      <ProposalAssignees
        workspaceId={workspaceId}
        principalId={session?.principal_id ?? ""}
        targets={nativeReview.targets}
        profiles={nativeProfiles}
        choices={choices}
        reusedBindings={new Map()}
        onChange={(key, value) => {
          if (scope === null || activeDraft === null) return;
          const nextChoices = new Map(activeDraft.choices);
          if (value === null) nextChoices.delete(key);
          else nextChoices.set(key, value);
          saveEditorDraft({ ...activeDraft, choices: [...nextChoices.entries()] });
          setCommandError(null);
        }}
        disabled={ownerControlsDisabled}
      />
    )
    : undefined;

  async function handleNativeApplied(original: NativeCommandRequest, result: NativeCommandResponse | null) {
    const appliedScopeKey = scopeKey;
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: planningRequestQueryKey(workspaceId, planningRequestId) }),
      queryClient.invalidateQueries({ queryKey: planningRunsQueryKey(workspaceId, planningRequestId) }),
      queryClient.invalidateQueries({ queryKey: planningBriefsQueryKey(workspaceId, planningRequestId) }),
      queryClient.invalidateQueries({ queryKey: planningClarificationsQueryKey(workspaceId, planningRequestId) }),
    ]);
    if (currentScopeKey.current !== appliedScopeKey) return;
    if (original.command_type === "ConfirmPlanningProposal") {
      if (result === null) {
        setCommandError("确认已处理，但没有可验证的 Root Task 映射。");
        return;
      }
      const rootTaskId = readRootTaskId(result.data);
      if (rootTaskId === null) {
        setCommandError("确认已处理，但没有可验证的 Root Task 映射。");
        return;
      }
      navigate(`/workspaces/${workspaceId}/tasks/${encodeURIComponent(rootTaskId)}`);
      return;
    }
    setConfirmation(null);
    if (original.command_type === "RequestProposalRevision") setFeedbackOpen(false);
  }

  async function submitNativePlanningCommand(
    commandType: string,
    expectedVersion: number,
    payload: Record<string, unknown>,
  ) {
    if (planningMode !== "native_profile" || scope === null || scope.kind !== "create"
      || activeDraft === null || activeDraft.kind !== "create"
      || !currentSessionValid || session === null || activeDraftFailure !== null) {
      setCommandError(activeDraftFailure ?? "需要当前工作区的有效本地会话");
      return;
    }
    if (nativeCommand.unresolved) {
      setCommandError("请先核对原始命令结果");
      return;
    }
    try {
      const original = buildNativeCommand({
        session,
        workspaceId,
        commandType,
        targetType: "planning_request",
        targetId: planningRequestId,
        expectedVersion,
        payload,
      });
      setCommandError(null);
      await nativeCommand.submitOriginal(original);
    } catch {
      setCommandError("当前本地会话或命令内容不可用");
    }
  }

  async function runCommand(command: PlanningCommandRequest, commandName: string): Promise<PlanningCommandResponse | null> {
    setPendingCommand(commandName);
    setCommandError(null);
    try {
      const response = await executePlanningCommand(command);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: planningRequestQueryKey(workspaceId, planningRequestId) }),
        queryClient.invalidateQueries({ queryKey: planningRunsQueryKey(workspaceId, planningRequestId) }),
        queryClient.invalidateQueries({ queryKey: planningBriefsQueryKey(workspaceId, planningRequestId) }),
        queryClient.invalidateQueries({ queryKey: planningClarificationsQueryKey(workspaceId, planningRequestId) }),
      ]);
      return response;
    } catch (error) {
      setCommandError(readErrorMessage(error));
      if (
        commandName === "RequestProposalRevision"
        && error instanceof ApiProblem
        && ["version_conflict", "stale_version", "expected_version_conflict"].includes(error.code)
        && request.current_proposal_revision !== null
      ) {
        setFeedbackLockRevision(request.current_proposal_revision);
      }
      return null;
    } finally {
      setPendingCommand(null);
    }
  }

  async function submitClarifications(answers: PlanningClarificationAnswer[]) {
    if (planningMode !== "legacy") {
      await submitNativePlanningCommand("AnswerPlanningClarification", request.version, { answers });
      return;
    }
    await runCommand(
      buildAnswerPlanningClarificationCommand({
        workspaceId,
        requestId: planningRequestId,
        expectedVersion: request.version,
        answers,
      }),
      "AnswerPlanningClarification",
    );
  }

  async function submitFeedback(feedback: string) {
    if (!canRevise || request.current_proposal_revision === null || request.current_proposal_content_hash === null) {
      setCommandError("当前没有可修改的方案");
      return;
    }
    if (planningMode !== "legacy") {
      await submitNativePlanningCommand("RequestProposalRevision", request.version, {
        current_proposal_revision: request.current_proposal_revision,
        current_proposal_content_hash: request.current_proposal_content_hash,
        feedback,
      });
      return;
    }
    const succeeded = (await runCommand(
      buildRequestProposalRevisionCommand({
        workspaceId,
        requestId: planningRequestId,
        expectedVersion: request.version,
        payload: {
          current_proposal_revision: request.current_proposal_revision,
          current_proposal_content_hash: request.current_proposal_content_hash,
          feedback,
        },
      }),
      "RequestProposalRevision",
    )) !== null;
    if (succeeded) {
      setFeedbackOpen(false);
    }
  }

  async function confirmProposal(binding: ProposalReviewBinding) {
    if (planningMode === "native_profile") {
      if (scope === null || activeDraft === null || activeDraft.review === null
        || nativeReview?.ok !== true || !currentSessionValid || session === null
        || activeDraftFailure !== null || nativeCommand.unresolved || appliedConfirmation
        || !sameReviewBinding(binding, activeDraft.review)
        || !sameReviewBinding(binding, nativeReview.binding)) {
        setCommandError(activeDraftFailure ?? "方案或本地会话已变化，请重新审阅后再确认");
        return;
      }
      const problem = readAssigneeProblem(nativeReview.targets, choices, session, nativeProfiles);
      if (problem !== null) {
        setCommandError(problem);
        return;
      }
      try {
        const assigneeBindings = toPlanningAssigneeChoices({
          targets: nativeReview.targets,
          choices,
          session,
          permittedProfiles: nativeProfiles,
          reusedBindings: new Map(),
        });
        const original = buildNativeCommand({
          session,
          workspaceId,
          commandType: "ConfirmPlanningProposal",
          targetType: "planning_request",
          targetId: planningRequestId,
          expectedVersion: binding.requestVersion,
          payload: {
            proposal_revision: binding.proposalRevision,
            proposal_content_hash: binding.proposalContentHash,
            profile_id: binding.profileId,
            profile_version: binding.profileVersion,
            assignee_bindings: assigneeBindings,
          },
        });
        setCommandError(null);
        await nativeCommand.submitOriginal(original);
      } catch {
        setCommandError("负责人选择或本地会话不可用");
      }
      return;
    }
    if (planningMode !== "legacy") {
      setCommandError("无法核实当前规划模式");
      return;
    }
    const response = await runCommand(
      buildConfirmPlanningProposalCommand({
        workspaceId,
        requestId: planningRequestId,
        expectedVersion: binding.requestVersion,
        payload: {
          proposal_revision: binding.proposalRevision,
          proposal_content_hash: binding.proposalContentHash,
          profile_id: binding.profileId,
          profile_version: binding.profileVersion,
        },
      }),
      "ConfirmPlanningProposal",
    );
    if (response === null) return;
    const rootTaskId = readRootTaskId(response.data);
    if (rootTaskId === null) {
      setCommandError("确认响应缺少 Root Task 映射，未自动跳转。");
      return;
    }
    navigate(`/workspaces/${workspaceId}/tasks/${encodeURIComponent(rootTaskId)}`);
  }

  async function revisitProposal() {
    if (planningMode === "legacy") {
      setProposalViewNonce((current) => current + 1);
      void proposalQuery.refetch();
      return;
    }
    if (planningMode !== "native_profile" || scope === null || scope.kind !== "create"
      || activeDraft === null || activeDraft.kind !== "create"
      || !currentSessionValid || nativeCommand.unresolved || appliedConfirmation) {
      setCommandError("当前无法重新审阅方案事实");
      return;
    }
    try {
      const nextRequest = await getPlanningRequest(workspaceId, planningRequestId);
      const nextRuns = await listPlanningRuns(workspaceId, planningRequestId);
      if (nextRequest.status !== "proposal_ready" || nextRequest.current_proposal_revision === null) {
        setCommandError("当前没有可重新审阅的方案");
        return;
      }
      const nextProposal = await getPlanningProposal(workspaceId, planningRequestId, nextRequest.current_proposal_revision);
      const reviewed = reviewInitialNativeProposal({
        workspaceId,
        requestId: planningRequestId,
        request: nextRequest,
        proposal: nextProposal,
        runs: nextRuns.items,
      });
      if (!reviewed.ok) {
        setCommandError(reviewed.reason);
        return;
      }

      const compatibleKeys = new Set(reviewed.targets.map((target) => assignmentKey(target.kind, target.key)));
      const retained = new Map(activeDraft.choices.filter(([key]) => compatibleKeys.has(key)));
      saveEditorDraft({ ...activeDraft, review: reviewed.binding, choices: [...retained.entries()] });
      queryClient.setQueryData(planningRequestQueryKey(workspaceId, planningRequestId), nextRequest);
      queryClient.setQueryData(planningRunsQueryKey(workspaceId, planningRequestId), nextRuns);
      queryClient.setQueryData(planningProposalQueryKey(workspaceId, planningRequestId, nextProposal.revision), nextProposal);
      setFeedbackLockRevision(null);
      setCommandError(null);
      setProposalViewNonce((current) => current + 1);
    } catch {
      setCommandError("无法重新读取方案事实");
    }
  }

  async function confirmAction() {
    if (confirmation === "cancel") {
      if (planningMode !== "legacy") {
        await submitNativePlanningCommand("CancelPlanningRequest", request.version, {});
        return;
      }
      const succeeded = (await runCommand(
        buildCancelPlanningRequestCommand({
          workspaceId,
          requestId: planningRequestId,
          expectedVersion: request.version,
        }),
        "CancelPlanningRequest",
      )) !== null;
      if (succeeded) setConfirmation(null);
      return;
    }
    if (confirmation === "reject" && request.current_proposal_revision !== null && request.current_proposal_content_hash !== null) {
      if (planningMode !== "legacy") {
        await submitNativePlanningCommand("RejectPlanningProposal", request.version, {
          current_proposal_revision: request.current_proposal_revision,
          current_proposal_content_hash: request.current_proposal_content_hash,
          reason: "用户拒绝当前方案",
        });
        return;
      }
      const succeeded = (await runCommand(
        buildRejectPlanningProposalCommand({
          workspaceId,
          requestId: planningRequestId,
          expectedVersion: request.version,
          payload: {
            current_proposal_revision: request.current_proposal_revision,
            current_proposal_content_hash: request.current_proposal_content_hash,
            reason: "用户拒绝当前方案",
          },
        }),
        "RejectPlanningProposal",
      )) !== null;
      if (succeeded) setConfirmation(null);
    }
  }

  return (
    <section className="planning-page" aria-labelledby="planning-request-title">
      <div className="breadcrumb planning-breadcrumb">
        <Link to={`/workspaces/${workspaceId}/tasks`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span>/</span><span>规划请求</span>
      </div>
      <header className="planning-page__heading">
        <div>
          <span className="eyebrow">PLANNING REQUEST</span>
          <h1 id="planning-request-title">规划请求</h1>
          {latestBrief ? <p className="planning-page__objective">{latestBrief.objective}</p> : null}
        </div>
        <div className="planning-page__heading-state">
          <span className={`planning-status planning-status--${request.status}`}>{requestStatusLabel(request.status)}</span>
          <Link
            className="quiet-button"
            to={`/workspaces/${workspaceId}/chat?context_ref=planning_request:${planningRequestId}`}
          >
            <MessageCircle size={14} aria-hidden="true" />与 Main Agent 讨论
          </Link>
          <span className="mono-label">请求 {planningRequestId.slice(0, 8)} · V{request.version}</span>
        </div>
      </header>

      {commandError || nativeCommand.error || activeDraftFailure ? (
        <div className="planning-command-error" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          {commandError ?? nativeCommand.error ?? activeDraftFailure}
        </div>
      ) : null}
      {request.kind === "create" && planningMode !== "legacy" && !currentSessionValid ? (
        <div className="planning-command-error" role="status">
          <span>需要当前工作区的有效本地会话</span>
          <LocalSessionButton
            workspaceId={workspaceId}
            onAuthorized={(nextSession) => queryClient.setQueryData(["local-session", workspaceId], nextSession)}
          />
        </div>
      ) : null}
      {nativeCommand.original !== null ? (
        <div className="planning-command-error" role="group" aria-label="原始命令恢复">
          <span>{planningCommandStatusLabel(nativeCommand.status)}</span>
          {nativeCommand.unresolved ? (
            <button
              className="quiet-button"
              type="button"
              disabled={!currentSessionValid || nativeCommand.busy}
              onClick={() => void nativeCommand.lookupOriginal()}
            >
              {nativeCommand.busy ? "正在查询" : "核对原始命令"}
            </button>
          ) : null}
        </div>
      ) : null}
      <div className="planning-page__layout">
        <div className="planning-page__main">
          <PlanningRunStatus
            run={activeRun}
            workspaceId={workspaceId}
            previousRun={previousRun}
            onCancel={canCancel && activeRun !== null && !unresolvedNativeCommand
              && (planningMode === "legacy" || currentSessionValid)
              ? () => setConfirmation("cancel")
              : undefined}
          />

          {request.status === "waiting_for_user" && !unresolvedNativeCommand ? (
            <ClarificationPanel
              clarifications={clarifications}
              onSubmit={(answers) => void submitClarifications(answers)}
              submitting={pendingCommand === "AnswerPlanningClarification" || nativeCommand.busy}
            />
          ) : request.status === "waiting_for_user" && unresolvedNativeCommand ? (
            <div className="planning-command-error" role="status">请先核对原始命令结果</div>
          ) : null}

          {canRevise && feedbackOpen ? (
            <ProposalFeedbackComposer
              key={`${planningRequestId}:${request.current_proposal_revision}`}
              workspaceId={workspaceId}
              requestId={planningRequestId}
              proposalRevision={request.current_proposal_revision!}
              onSubmit={(feedback) => void submitFeedback(feedback)}
              submitting={pendingCommand === "RequestProposalRevision" || nativeCommand.busy}
              disabled={feedbackLockRevision === request.current_proposal_revision
                || (planningMode !== "legacy" && (nativeActionDisabled(planningMode, currentSessionValid, scope, activeDraft, activeDraftFailure, nativeCommand.unresolved) || nativeCommand.busy))}
            />
          ) : null}

          {request.status === "proposal_ready" && request.current_proposal_revision !== null ? (
            proposalQuery.data ? (
              <ProposalView
                key={proposalViewNonce}
                proposal={proposalQuery.data}
                requestVersion={request.version}
                profileId={request.planning_profile_id}
                profileVersion={request.planning_profile_version}
                onConfirm={(binding) => void confirmProposal(binding)}
                onRevisit={() => void revisitProposal()}
                {...(planningMode === "legacy" ? {} : {
                  ...(activeDraft?.review == null ? {} : { reviewedBinding: activeDraft.review }),
                  ...(nativeConfirmDisabledReason === undefined ? {} : { disabledReason: nativeConfirmDisabledReason }),
                  ...(responsibilityContent === undefined ? {} : { responsibilityContent }),
                })}
                pending={pendingCommand === "ConfirmPlanningProposal"
                  || (planningMode !== "legacy" && (nativeCommand.busy || nativeCommand.unresolved))}
              />
            ) : proposalQuery.isPending ? (
              <div className="planning-proposal__loading" role="status">正在读取方案结构</div>
            ) : (
              <div className="planning-proposal__loading planning-proposal__loading--error" role="alert">
                <strong>方案结构读取失败</strong>
                <span>方案内容没有完整返回，确认前需要重新读取。</span>
                <button className="quiet-button" type="button" onClick={() => void proposalQuery.refetch()}>
                  重新读取方案
                </button>
              </div>
            )
          ) : null}

          <div className="planning-page__brief">
            <div className="planning-section-heading">
              <div>
                <span className="eyebrow">REQUEST HISTORY</span>
                <h2>需求上下文</h2>
              </div>
              <span className="mono-label">Brief R{request.current_brief_revision}</span>
            </div>
            <div className="planning-brief-summary">
              <strong>{latestBrief?.objective ?? "需求目标待读取"}</strong>
              <span>{latestBrief?.acceptance_criteria.length ?? 0} 条验收条件 · {latestBrief?.requested_outputs.length ?? 0} 项期望产物</span>
            </div>
          </div>

          {!isTerminal ? (
            <div className="planning-page__actions">
              {canRevise && !feedbackOpen ? (
                <button
                  className="quiet-button"
                  type="button"
                  disabled={planningMode !== "legacy" && nativeActionDisabled(planningMode, currentSessionValid, scope, activeDraft, activeDraftFailure, nativeCommand.unresolved)}
                  onClick={() => setFeedbackOpen(true)}
                >
                  要求修改
                </button>
              ) : null}
              {canRevise ? (
                <button
                  className="quiet-button quiet-button--danger"
                  type="button"
                  disabled={planningMode !== "legacy" && nativeActionDisabled(planningMode, currentSessionValid, scope, activeDraft, activeDraftFailure, nativeCommand.unresolved)}
                  onClick={() => setConfirmation("reject")}
                >
                  拒绝方案
                </button>
              ) : null}
              {canCancel && activeRun === null ? (
                <button
                  className="quiet-button quiet-button--danger"
                  type="button"
                  disabled={planningMode !== "legacy" && nativeActionDisabled(planningMode, currentSessionValid, scope, activeDraft, activeDraftFailure, nativeCommand.unresolved)}
                  onClick={() => setConfirmation("cancel")}
                >
                  取消规划
                </button>
              ) : null}
            </div>
          ) : <div className="planning-readonly-note">只读历史</div>}
        </div>
        <PlanningInspector request={request} />
      </div>

      {confirmation ? (
        <ConfirmationDialog
          kind={confirmation}
          pending={pendingCommand === (confirmation === "cancel" ? "CancelPlanningRequest" : "RejectPlanningProposal") || nativeCommand.busy}
          disabled={planningMode !== "legacy" && nativeActionDisabled(planningMode, currentSessionValid, scope, activeDraft, activeDraftFailure, nativeCommand.unresolved)}
          onClose={() => setConfirmation(null)}
          onConfirm={() => void confirmAction()}
        />
      ) : null}
    </section>
  );
}

function PlanningPageState({
  title,
  detail,
  error = false,
  onRetry,
}: {
  title: string;
  detail: string;
  error?: boolean;
  onRetry?: () => void;
}) {
  return (
    <section className="planning-page" aria-labelledby="planning-state-title">
      <div className="breadcrumb">工作区 / 任务 / 规划请求</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">PLANNING REQUEST</span>
          <h1 id="planning-state-title">{title}</h1>
        </div>
      </div>
      <div className={`planning-page__state${error ? " planning-page__state--error" : ""}`} role={error ? "alert" : "status"}>
        <strong>{title}</strong>
        <p>{detail}</p>
        {onRetry ? <button className="quiet-button" type="button" onClick={onRetry}>重试读取</button> : null}
      </div>
    </section>
  );
}

function ConfirmationDialog({
  kind,
  pending,
  disabled = false,
  onClose,
  onConfirm,
}: {
  kind: Exclude<Confirmation, null>;
  pending: boolean;
  disabled?: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const isCancel = kind === "cancel";
  const title = isCancel ? "确认取消规划" : "确认拒绝方案";
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
        "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
      );
      if (!focusable || focusable.length === 0) return;
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      previouslyFocused?.focus();
    };
  }, [onClose]);

  return (
    <div className="planning-dialog-backdrop">
      <div
        ref={dialogRef}
        className="planning-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="planning-dialog-title"
        aria-describedby="planning-dialog-description"
      >
        <div className="planning-dialog__heading">
          <h2 id="planning-dialog-title">{title}</h2>
          <button ref={closeButtonRef} className="icon-button" type="button" aria-label="关闭确认窗口" title="关闭确认窗口" onClick={onClose}>
            <X size={15} aria-hidden="true" />
          </button>
        </div>
        <p id="planning-dialog-description">{isCancel ? "这会停止当前 PlanningRequest 的后续运行。" : "这会结束当前方案审计，不会删除已有任务。"}</p>
        <div className="planning-dialog__actions">
          <button className="quiet-button" type="button" onClick={onClose} disabled={pending || disabled}>返回</button>
          <button className="quiet-button quiet-button--danger" type="button" onClick={onConfirm} disabled={pending || disabled}>
            {pending ? "正在提交" : isCancel ? "确认取消" : "确认拒绝"}
          </button>
        </div>
      </div>
    </div>
  );
}

function requestStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    submitted: "已提交",
    planning: "规划中",
    waiting_for_user: "等待你的回答",
    proposal_ready: "方案待确认",
    failed: "规划失败",
    blocked: "需要运维恢复",
    cancelled: "已取消",
    rejected: "已拒绝",
    materialized: "已转为任务",
  };
  return labels[status] ?? "规划请求";
}

function readErrorMessage(error: unknown): string {
  if (error instanceof ApiProblem) return error.detail;
  return error instanceof Error ? error.message : "操作未能完成，请稍后重试。";
}

function readRootTaskId(data: unknown): string | null {
  if (typeof data !== "object" || data === null || Array.isArray(data)) return null;
  const value = (data as Record<string, unknown>).root_task_id;
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function isValidLocalSession(session: LocalSession | null, workspaceId: string): session is LocalSession {
  if (session === null || session.workspace_id !== workspaceId
    || session.session_id.trim() === "" || session.principal_id.trim() === "") return false;
  const expiry = Date.parse(session.expires_at);
  return Number.isFinite(expiry) && expiry > Date.now();
}

function emptyPlanningDraft(scope: PlanningDraftScope): PlanningConfirmationDraft {
  return {
    ...scope,
    schema_version: 1,
    review: null,
    choices: [],
    original: null,
    status: "idle",
  };
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

function sameReviewBinding(left: ProposalReviewBinding, right: ProposalReviewBinding): boolean {
  return left.requestVersion === right.requestVersion
    && left.proposalRevision === right.proposalRevision
    && left.proposalContentHash === right.proposalContentHash
    && left.profileId === right.profileId
    && left.profileVersion === right.profileVersion;
}

function readAssigneeProblem(
  targets: readonly ProposalAssignmentTarget[],
  choices: ReadonlyMap<string, AssigneeValue>,
  session: LocalSession,
  profiles: readonly ProfileView[],
): string | null {
  for (const target of targets) {
    if (target.strategy === "reuse") continue;
    const value = choices.get(assignmentKey(target.kind, target.key));
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
      reusedBindings: new Map(),
    });
    return null;
  } catch {
    return "负责人选择无效";
  }
}

function nativeActionDisabled(
  mode: PlanningMode,
  sessionValid: boolean,
  scope: PlanningDraftScope | null,
  draft: PlanningConfirmationDraft | null,
  draftFailure: string | null,
  unresolved: boolean,
): boolean {
  return mode !== "native_profile" || !sessionValid || scope === null
    || draft === null || draftFailure !== null || unresolved;
}

function planningCommandStatusLabel(status: PlanningConfirmationDraft["status"]): string {
  const labels: Record<PlanningConfirmationDraft["status"], string> = {
    idle: "没有待恢复的命令",
    pending: "原始命令处理中",
    unknown: "原始命令结果未知",
    applied: "原始命令已生效",
    rejected: "原始命令已拒绝",
  };
  return labels[status];
}
