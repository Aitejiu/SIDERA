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
  pending = false,
}: {
  binding: ProposalReviewBinding;
  onConfirm: (binding: ProposalReviewBinding) => void | Promise<void>;
  onRevisit?: () => void;
  pending?: boolean;
}) {
  const [reviewedBinding] = useState(binding);
  const isStale = !sameBinding(reviewedBinding, binding);
  return (
    <section className="planning-proposal__actions" aria-labelledby="proposal-actions-title">
      <div>
        <span className="eyebrow">CONFIRMATION GATE</span>
        <h2 id="proposal-actions-title">确认后才会创建任务事实</h2>
        <p>确认会按当前审阅的 revision 创建 Root Task、Subtask、WorkerItem 和 ExecutionRequest。</p>
      </div>
      {isStale ? (
        <div className="planning-proposal__stale" role="alert">
          <strong>方案版本已变化，请重新查看后再确认。</strong>
          {onRevisit ? <button className="quiet-button" type="button" onClick={onRevisit}><RefreshCw size={14} aria-hidden="true" />重新查看最新方案</button> : null}
        </div>
      ) : null}
      <div className="planning-proposal__action-row">
        <span className="mono-label">审阅 V{reviewedBinding.proposalRevision} · 请求 V{reviewedBinding.requestVersion}</span>
        <button
          className="primary-button"
          type="button"
          disabled={pending || isStale}
          onClick={() => onConfirm(reviewedBinding)}
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
