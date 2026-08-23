import { useEffect, useState } from "react";

export function ProposalFeedbackComposer({
  workspaceId,
  requestId,
  proposalRevision,
  onSubmit,
  submitting = false,
  disabled = false,
}: {
  workspaceId: string;
  requestId: string;
  proposalRevision: number;
  onSubmit: (feedback: string) => void;
  submitting?: boolean;
  disabled?: boolean | undefined;
}) {
  const draftKey = planningProposalDraftKey(workspaceId, requestId, proposalRevision);
  const [feedback, setFeedback] = useState(() => readDraft(draftKey));

  useEffect(() => {
    saveDraft(draftKey, feedback);
  }, [draftKey, feedback]);

  return (
    <section className="planning-feedback-composer" aria-labelledby="planning-feedback-title">
      <div className="planning-section-heading">
        <div>
          <span className="eyebrow">PROPOSAL FEEDBACK</span>
          <h2 id="planning-feedback-title">告诉 Planner 怎么调整</h2>
        </div>
        <span className="mono-label">方案 R{proposalRevision}</span>
      </div>
      <label className="planning-field">
        <span>修改意见</span>
        <textarea
          aria-label="修改意见"
          value={feedback}
          onChange={(event) => setFeedback(event.target.value)}
          rows={4}
          placeholder="例如：保留现有 Worker 上下文，只调整新增的子任务"
          readOnly={disabled}
        />
      </label>
      {disabled ? <p className="planning-form-error">方案版本已变化，请刷新后再提交这份修改意见。</p> : null}
      <button
        className="quiet-button"
        type="button"
        onClick={() => onSubmit(feedback.trim())}
        disabled={feedback.trim().length === 0 || submitting || disabled}
      >
        {submitting ? "正在提交" : "提交修改意见"}
      </button>
    </section>
  );
}

export function planningProposalDraftKey(
  workspaceId: string,
  requestId: string,
  proposalRevision: number,
): string {
  return `sidera:planning-proposal-draft:${workspaceId}:${requestId}:${proposalRevision}`;
}

function readDraft(key: string): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function saveDraft(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // A private browsing/storage failure must not discard the in-memory draft.
  }
}
