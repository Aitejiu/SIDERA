import {
  Check,
  CircleAlert,
  Pause,
  Play,
  RotateCcw,
  X,
  type LucideIcon,
} from "lucide-react";
import { createPortal } from "react-dom";
import { useEffect, useRef, useState, type FormEvent } from "react";

import type { WorkerResultInputsView } from "../result-inputs/api";
import type { ActiveCollaborationProposal, GovernanceAction } from "./types";

export interface GovernanceActionDialogProps {
  action: GovernanceAction;
  taskPath: string;
  proposal: ActiveCollaborationProposal | null;
  resultInputs?: WorkerResultInputsView;
  errorMessage: string | null;
  isSubmitting: boolean;
  onClose: () => void;
  onSubmit: (reason: string) => void;
}

type ActionCopy = {
  title: string;
  submitLabel: string;
  reasonLabel: string;
  reasonPlaceholder: string;
  icon: LucideIcon;
  tone: "neutral" | "danger";
  requiresReason: boolean;
};

const actionCopy: Record<GovernanceAction, ActionCopy> = {
  pause: {
    title: "暂停自动放行",
    submitLabel: "确认暂停自动放行",
    reasonLabel: "暂停原因",
    reasonPlaceholder: "说明为什么要先暂停这次自动放行",
    icon: Pause,
    tone: "danger",
    requiresReason: true,
  },
  approve: {
    title: "批准协同放行",
    submitLabel: "批准这次放行",
    reasonLabel: "批准说明",
    reasonPlaceholder: "可补充这次批准的判断依据",
    icon: Check,
    tone: "neutral",
    requiresReason: false,
  },
  reject: {
    title: "驳回协同放行",
    submitLabel: "驳回这次放行",
    reasonLabel: "驳回原因",
    reasonPlaceholder: "说明还需要补齐什么信息或证据",
    icon: X,
    tone: "danger",
    requiresReason: true,
  },
  resume_automatic: {
    title: "恢复自动放行",
    submitLabel: "确认恢复自动放行",
    reasonLabel: "恢复说明",
    reasonPlaceholder: "可补充为什么现在可以恢复自动放行",
    icon: Play,
    tone: "neutral",
    requiresReason: false,
  },
};

export function GovernanceActionDialog({
  action,
  taskPath,
  proposal,
  resultInputs,
  errorMessage,
  isSubmitting,
  onClose,
  onSubmit,
}: GovernanceActionDialogProps) {
  const copy = actionCopy[action];
  const Icon = copy.icon;
  const dialogRef = useRef<HTMLElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const [reason, setReason] = useState("");
  const canSubmit = !isSubmitting && (!copy.requiresReason || reason.trim().length > 0);

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.focus();
    return () => previousFocusRef.current?.focus();
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isSubmitting) {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isSubmitting, onClose]);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (canSubmit) onSubmit(reason.trim());
  };

  return createPortal(
    <div className="governance-dialog-backdrop">
      <section
        className={`governance-dialog governance-dialog--${copy.tone}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="governance-action-dialog-title"
        aria-busy={isSubmitting}
        ref={dialogRef}
        tabIndex={-1}
      >
        <div className="governance-dialog__heading">
          <div>
            <span className="eyebrow">RELEASE GOVERNANCE</span>
            <h2 id="governance-action-dialog-title"><Icon size={17} aria-hidden="true" />{copy.title}</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭治理操作"
            title="关闭治理操作"
            onClick={onClose}
            disabled={isSubmitting}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <div className="governance-dialog__scope">
          <span className="eyebrow">影响范围</span>
          <strong>{taskPath}</strong>
          <span>只处理当前 WorkerItem 的协同放行，不会调整整体 Worker 排布。</span>
        </div>

        {proposal ? (
          <div className="governance-dialog__proposal">
            <span className="eyebrow">当前 Proposal</span>
            <strong>Proposal {proposal.proposal_id} · V{proposal.resource_version}</strong>
            <span>{proposal.summary}</span>
          </div>
        ) : null}

        {action === "approve" && proposal?.input_selection_id && proposal.input_selection_hash ? (
          <FixedInputSelection
            proposal={proposal}
            resultInputs={resultInputs}
          />
        ) : null}

        {action === "pause" ? (
          <div className="governance-dialog__warning" role="note">
            <CircleAlert size={15} aria-hidden="true" />
            <span>暂停自动放行不会停止当前 Worker 正在运行的 Execution。</span>
          </div>
        ) : null}
        {action === "resume_automatic" ? (
          <div className="governance-dialog__warning" role="note">
            <RotateCcw size={15} aria-hidden="true" />
            <span>恢复后，待处理 Proposal 会变为 stale，Runtime 会重新评估。</span>
          </div>
        ) : null}
        {action === "approve" ? (
          <div className="governance-dialog__warning" role="note">
            <Check size={15} aria-hidden="true" />
            <span>批准只影响这次 Proposal 的放行结果，不会改变任务或 Worker 排布。</span>
          </div>
        ) : null}

        <form onSubmit={submit}>
          <label className="governance-dialog__field">
            <span>{copy.reasonLabel}{copy.requiresReason ? "" : "（可选）"}</span>
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              aria-label={copy.reasonLabel}
              placeholder={copy.reasonPlaceholder}
              rows={3}
              disabled={isSubmitting}
            />
          </label>
          {errorMessage ? (
            <div className="governance-dialog__error" role="alert">
              <CircleAlert size={14} aria-hidden="true" />
              <span>{errorMessage}</span>
            </div>
          ) : null}
          {isSubmitting ? <div className="governance-dialog__pending" role="status">正在提交治理操作</div> : null}
          <div className="governance-dialog__actions">
            <button className="quiet-button" type="button" onClick={onClose} disabled={isSubmitting}>取消</button>
            <button className={copy.tone === "danger" ? "quiet-button quiet-button--danger" : "primary-button"} type="submit" disabled={!canSubmit}>
              <Icon size={14} aria-hidden="true" />{isSubmitting ? "提交中" : copy.submitLabel}
            </button>
          </div>
        </form>
      </section>
    </div>,
    document.body,
  );
}

function FixedInputSelection({
  proposal,
  resultInputs,
}: {
  proposal: ActiveCollaborationProposal;
  resultInputs: WorkerResultInputsView | undefined;
}) {
  const selectionId = proposal.input_selection_id;
  const selectionHash = proposal.input_selection_hash;
  if (!selectionId || !selectionHash) return null;
  const sources = resultInputs?.groups
    .flatMap((group) => group.selected_sources)
    .filter((source, index, all) => all.findIndex((item) => item.worker_item_key === source.worker_item_key) === index) ?? [];
  return (
    <div className="governance-dialog__input-selection">
      <span className="eyebrow">FIXED INPUT SELECTION</span>
      <strong>Selection {selectionId.slice(0, 8)} · Proposal V{proposal.resource_version}</strong>
      <code>{selectionHash.slice(0, 12)}…</code>
      <span>批准时会重新校验这个 Selection；来源完成变化不会自动替换本次采用结果。</span>
      {sources.length > 0 ? (
        <ul>
          {sources.map((source) => <li key={source.worker_item_key}>{source.worker_item_title} · {source.subtask_path.join(" / ")}</li>)}
        </ul>
      ) : <span>当前弹窗未加载来源明细，仅显示固定 Selection 引用。</span>}
    </div>
  );
}
