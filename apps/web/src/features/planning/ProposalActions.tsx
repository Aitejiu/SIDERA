import { CheckCircle2, RefreshCw } from "lucide-react";
import { useState } from "react";

export interface ProposalReviewBinding {
  requestVersion: number;
  proposalRevision: number;
  proposalContentHash: string;
  profileId: string;
  profileVersion: number;
}

export function ProposalActions({
  binding,
  onConfirm,
  onRevisit,
  reviewedBinding,
  disabledReason,
  pending = false,
}: {
  binding: ProposalReviewBinding;
  onConfirm: (binding: ProposalReviewBinding) => void | Promise<void>;
  onRevisit?: () => void;
  reviewedBinding?: ProposalReviewBinding;
  disabledReason?: string;
  pending?: boolean;
}) {
  const [capturedBinding] = useState(binding);
  const reviewAnchor = reviewedBinding ?? capturedBinding;
  const isStale = !sameBinding(reviewAnchor, binding);
  const disabled = pending || isStale || disabledReason !== undefined;
  return (
    <section className="planning-proposal__actions" aria-labelledby="proposal-actions-title">
      <div>
        <span className="eyebrow">CONFIRMATION GATE</span>
        <h2 id="proposal-actions-title">确认后启用新的活动计划</h2>
        <p>新计划会立即成为活动目标，Worker 随后异步同步</p>
        <p>确认会按当前审阅的 revision 创建 Root Task、Subtask、WorkerItem 和 ExecutionRequest。</p>
      </div>
      {disabledReason !== undefined ? (
        <div className="planning-proposal__stale" id="proposal-disabled-reason" role="alert">
          <strong>{disabledReason}</strong>
        </div>
      ) : null}
      {isStale ? (
        <div className="planning-proposal__stale" id="proposal-stale-reason" role="alert">
          <strong>方案版本已变化，请重新查看后再确认。</strong>
          {onRevisit ? <button className="quiet-button" type="button" onClick={onRevisit}><RefreshCw size={14} aria-hidden="true" />重新查看最新方案</button> : null}
        </div>
      ) : null}
      <div className="planning-proposal__action-row">
        <span className="mono-label">审阅 V{reviewAnchor.proposalRevision} · 请求 V{reviewAnchor.requestVersion}</span>
        <button
          className="primary-button"
          type="button"
          disabled={disabled}
          aria-describedby={[
            disabledReason !== undefined ? "proposal-disabled-reason" : null,
            isStale ? "proposal-stale-reason" : null,
          ].filter((id): id is string => id !== null).join(" ") || undefined}
          aria-busy={pending}
          onClick={() => onConfirm(reviewAnchor)}
        >
          <CheckCircle2 size={15} aria-hidden="true" />
          {pending ? "正在创建任务" : "确认并创建任务"}
        </button>
      </div>
    </section>
  );
}

function sameBinding(left: ProposalReviewBinding, right: ProposalReviewBinding): boolean {
  return left.requestVersion === right.requestVersion
    && left.proposalRevision === right.proposalRevision
    && left.proposalContentHash === right.proposalContentHash
    && left.profileId === right.profileId
    && left.profileVersion === right.profileVersion;
}
