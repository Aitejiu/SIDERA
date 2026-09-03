import {
  Check,
  CircleAlert,
  Pause,
  Play,
  ShieldCheck,
  X,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";

import { ApiProblem } from "@/shared/api/client";
import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import {
  approveCollaborationRelease,
  pauseCollaborationRelease,
  rejectCollaborationRelease,
  resumeAutomaticCollaboration,
} from "./api";
import { GovernanceActionDialog } from "./GovernanceActionDialog";
import type {
  ActiveCollaborationProposal,
  CollaborationReleaseState,
  GovernanceAction,
  GovernanceMode,
  WorkerCollaborationSummary,
} from "./types";

export interface WorkerGovernancePanelProps {
  workspaceId: string;
  workerItemId: string;
  workerItemVersion: number;
  targetPlanGeneration: number;
  taskPath: string;
  canIntervene: boolean;
  collaboration: WorkerCollaborationSummary | null | undefined;
  onRefresh: () => Promise<void>;
}

type ActionButtonCopy = {
  label: string;
  icon: LucideIcon;
  tone: "neutral" | "danger";
};

const actionButtonCopy: Record<GovernanceAction, ActionButtonCopy> = {
  pause: { label: "暂停自动放行", icon: Pause, tone: "danger" },
  approve: { label: "批准 Proposal", icon: Check, tone: "neutral" },
  reject: { label: "驳回 Proposal", icon: X, tone: "danger" },
  resume_automatic: { label: "恢复自动放行", icon: Play, tone: "neutral" },
};

export function WorkerGovernancePanel({
  workspaceId,
  workerItemId,
  workerItemVersion,
  targetPlanGeneration,
  taskPath,
  canIntervene,
  collaboration,
  onRefresh,
}: WorkerGovernancePanelProps) {
  const [activeAction, setActiveAction] = useState<GovernanceAction | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  if (collaboration === null || collaboration === undefined || !collaboration.enabled) return null;

  const mode = collaboration.governance_mode ?? "automatic";
  const releaseState = collaboration.release_state ?? "unreleased";
  const availableActions = collaboration.available_actions ?? [];
  const proposal = collaboration.active_proposal ?? null;
  const status = governanceStatus(mode, releaseState, collaboration.attention_state);

  const openAction = (action: GovernanceAction) => {
    setErrorMessage(null);
    setActiveAction(action);
  };

  const closeAction = () => {
    if (!isSubmitting) {
      setActiveAction(null);
      setErrorMessage(null);
    }
  };

  const submitAction = async (reason: string) => {
    if (activeAction === null || isSubmitting) return;
    setIsSubmitting(true);
    setErrorMessage(null);
    try {
      if (activeAction === "pause") {
        await pauseCollaborationRelease(
          workspaceId,
          workerItemId,
          workerItemVersion,
          targetPlanGeneration,
          reason || null,
        );
      } else if (activeAction === "approve" && proposal !== null) {
        await approveCollaborationRelease(workspaceId, proposal.proposal_id, proposal.resource_version, reason || null);
      } else if (activeAction === "reject" && proposal !== null) {
        await rejectCollaborationRelease(workspaceId, proposal.proposal_id, proposal.resource_version, reason || null);
      } else if (activeAction === "resume_automatic" && collaboration.active_hold_id && collaboration.hold_resource_version !== null && collaboration.hold_resource_version !== undefined) {
        await resumeAutomaticCollaboration(
          workspaceId,
          collaboration.active_hold_id,
          collaboration.hold_resource_version,
          reason || null,
        );
      } else {
        setErrorMessage("当前协同状态缺少操作所需的版本信息，请先重新读取。");
        return;
      }
      await onRefresh();
      setActiveAction(null);
    } catch (error) {
      setErrorMessage(governanceErrorMessage(error));
      if (error instanceof ApiProblem && error.status === 409) {
        await onRefresh().catch(() => undefined);
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <>
      <section className="worker-governance worker-item-sidebar__section" aria-labelledby="worker-governance-title">
        <div className="worker-governance__heading">
          <h3 id="worker-governance-title"><ShieldCheck size={15} aria-hidden="true" />协同放行控制</h3>
          <StatusMark kind={status.kind} label={status.label} />
        </div>
        <div className="worker-governance__facts">
          <div><span>模式</span><strong>{modeLabel(mode)}</strong></div>
          <div><span>放行</span><strong>{releaseStateLabel(releaseState)}</strong></div>
        </div>
        {collaboration.attention_state === "decision_required" ? (
          <div className="worker-governance__attention" role="status">
            <CircleAlert size={14} aria-hidden="true" />
            <span>这个 WorkerItem 的放行决策需要你确认。</span>
          </div>
        ) : null}
        {mode === "paused" && collaboration.hold_reason ? (
          <div className="worker-governance__hold">
            <span className="eyebrow">暂停原因</span>
            <strong>{collaboration.hold_reason}</strong>
          </div>
        ) : null}
        {proposal ? <ProposalSummary proposal={proposal} /> : collaboration.proposal_summary ? (
          <div className="worker-governance__proposal">
            <span className="eyebrow">Proposal</span>
            <strong>{collaboration.proposal_summary}</strong>
          </div>
        ) : (
          <span className="muted-copy">当前没有待处理 Proposal</span>
        )}
        {availableActions.length > 0 ? (
          <div className="worker-governance__actions" aria-label="可用治理动作">
            {availableActions.map((action) => {
              const copy = actionButtonCopy[action];
              const Icon = copy.icon;
              const enabled = canIntervene && actionRequirementsMet(action, proposal, collaboration);
              return (
                <button
                  className={copy.tone === "danger" ? "quiet-button quiet-button--danger" : "quiet-button"}
                  type="button"
                  key={action}
                  onClick={() => openAction(action)}
                  disabled={!enabled || isSubmitting}
                  title={!canIntervene ? "当前 WorkerItem 只读" : undefined}
                >
                  <Icon size={14} aria-hidden="true" />{copy.label}
                </button>
              );
            })}
          </div>
        ) : (
          <span className="muted-copy">服务端当前没有可执行的治理动作</span>
        )}
        {!canIntervene && availableActions.length > 0 ? <span className="muted-copy">当前工作区只读，治理动作已禁用。</span> : null}
      </section>
      {activeAction ? (
        <GovernanceActionDialog
          action={activeAction}
          taskPath={taskPath}
          proposal={proposal}
          errorMessage={errorMessage}
          isSubmitting={isSubmitting}
          onClose={closeAction}
          onSubmit={(reason) => void submitAction(reason)}
        />
      ) : null}
    </>
  );
}

function ProposalSummary({ proposal }: { proposal: ActiveCollaborationProposal }) {
  return (
    <div className="worker-governance__proposal">
      <div className="worker-governance__proposal-heading">
        <span className="eyebrow">当前 Proposal</span>
        <span className="mono-label">Proposal V{proposal.resource_version} · 请求 V{proposal.target_request_version}</span>
      </div>
      <strong>{proposal.summary}</strong>
      <span>{proposal.reason_code} · 创建于 {formatDateTime(proposal.created_at)}</span>
    </div>
  );
}

function actionRequirementsMet(
  action: GovernanceAction,
  proposal: ActiveCollaborationProposal | null,
  collaboration: WorkerCollaborationSummary,
): boolean {
  if ((action === "approve" || action === "reject") && proposal === null) return false;
  if (action === "resume_automatic") {
    return collaboration.active_hold_id !== null
      && collaboration.active_hold_id !== undefined
      && collaboration.hold_resource_version !== null
      && collaboration.hold_resource_version !== undefined;
  }
  return true;
}

function governanceStatus(
  mode: GovernanceMode,
  releaseState: CollaborationReleaseState,
  attentionState: WorkerCollaborationSummary["attention_state"],
): { kind: StatusMarkKind; label: string } {
  if (attentionState === "decision_required") return { kind: "stale", label: "需要确认" };
  if (mode === "paused") return { kind: "stale", label: "已暂停" };
  if (releaseState === "released") return { kind: "completed", label: "已放行" };
  return { kind: "planned", label: "自动放行" };
}

function modeLabel(mode: GovernanceMode): string {
  return mode === "paused" ? "已暂停" : "自动放行";
}

function releaseStateLabel(state: CollaborationReleaseState): string {
  return {
    unreleased: "尚未放行",
    proposed: "等待确认",
    released: "已放行",
  }[state];
}

function formatDateTime(value: string): string {
  return value.replace("T", " ").slice(0, 16);
}

function governanceErrorMessage(error: unknown): string {
  if (error instanceof ApiProblem) {
    if (error.status === 409) return `${error.detail}，已重新读取当前协同状态。`;
    return error.detail;
  }
  return error instanceof Error ? error.message : "治理操作未能提交，请稍后重试。";
}
