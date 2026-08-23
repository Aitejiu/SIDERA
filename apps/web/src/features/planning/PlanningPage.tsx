import { AlertTriangle, ArrowLeft, X } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiProblem } from "@/shared/api/client";

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
import type { PlanningClarificationAnswer, PlanningCommandRequest, PlanningCommandResponse } from "./types";

type Confirmation = "cancel" | "reject" | null;

export function PlanningPage() {
  const {
    workspaceId = "",
    planningRequestId = "",
  } = useParams<{ workspaceId: string; planningRequestId: string }>();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
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

  async function confirmProposal(binding: {
    requestVersion: number;
    proposalRevision: number;
    proposalContentHash: string;
    profileId: string;
    profileVersion: number;
  }) {
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

  async function confirmAction() {
    if (confirmation === "cancel") {
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
          <span className="mono-label">请求 {planningRequestId.slice(0, 8)} · V{request.version}</span>
        </div>
      </header>

      {commandError ? <div className="planning-command-error" role="alert"><AlertTriangle size={15} aria-hidden="true" />{commandError}</div> : null}
      <div className="planning-page__layout">
        <div className="planning-page__main">
          <PlanningRunStatus
            run={activeRun}
            onCancel={canCancel && activeRun !== null ? () => setConfirmation("cancel") : undefined}
          />

          {request.status === "waiting_for_user" ? (
            <ClarificationPanel
              clarifications={clarifications}
              onSubmit={(answers) => void submitClarifications(answers)}
              submitting={pendingCommand === "AnswerPlanningClarification"}
            />
          ) : null}

          {canRevise && feedbackOpen ? (
            <ProposalFeedbackComposer
              key={`${planningRequestId}:${request.current_proposal_revision}`}
              workspaceId={workspaceId}
              requestId={planningRequestId}
              proposalRevision={request.current_proposal_revision!}
              onSubmit={(feedback) => void submitFeedback(feedback)}
              submitting={pendingCommand === "RequestProposalRevision"}
              disabled={feedbackLockRevision === request.current_proposal_revision}
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
                onRevisit={() => {
                  setProposalViewNonce((current) => current + 1);
                  void proposalQuery.refetch();
                }}
                pending={pendingCommand === "ConfirmPlanningProposal"}
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
                <button className="quiet-button" type="button" onClick={() => setFeedbackOpen(true)}>
                  要求修改
                </button>
              ) : null}
              {canRevise ? (
                <button className="quiet-button quiet-button--danger" type="button" onClick={() => setConfirmation("reject")}>
                  拒绝方案
                </button>
              ) : null}
              {canCancel && activeRun === null ? (
                <button className="quiet-button quiet-button--danger" type="button" onClick={() => setConfirmation("cancel")}>
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
          pending={pendingCommand === (confirmation === "cancel" ? "CancelPlanningRequest" : "RejectPlanningProposal")}
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
  onClose,
  onConfirm,
}: {
  kind: Exclude<Confirmation, null>;
  pending: boolean;
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
          <button className="quiet-button" type="button" onClick={onClose} disabled={pending}>返回</button>
          <button className="quiet-button quiet-button--danger" type="button" onClick={onConfirm} disabled={pending}>
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
