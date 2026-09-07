import { useState, type FormEvent } from "react";

import type { ResultAction, TaskResultOverview } from "./api";

type Criterion = TaskResultOverview["criteria"][number];

export function ReworkForm({
  view,
  action,
  workerItemId,
  pending = false,
  error,
  onSubmit,
  onCancel,
}: {
  view: TaskResultOverview;
  action: ResultAction;
  workerItemId: string;
  pending?: boolean;
  error?: string | null;
  onSubmit: (input: { issueKeys: string[]; reason: string }) => void | Promise<void>;
  onCancel: () => void;
}) {
  const [issueKeys, setIssueKeys] = useState<string[]>(() => initialIssueKeys(view.criteria));
  const [reason, setReason] = useState("");
  const hasReason = reason.trim().length > 0;

  function toggleIssue(key: string) {
    setIssueKeys((current) => current.includes(key)
      ? current.filter((item) => item !== key)
      : [...current, key]);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!hasReason || pending) return;
    void onSubmit({ issueKeys, reason: reason.trim() });
  }

  return (
    <form className="result-operation-form" aria-label="局部返工" onSubmit={submit}>
      <div className="result-operation-form__heading">
        <div>
          <span className="eyebrow">SCOPED REWORK</span>
          <h4>局部返工</h4>
        </div>
        <span className="mono-label">RESULT V{action.expected_result_version}</span>
      </div>
      <p className="result-operation-form__scope">
        当前 WorkerItem {shortId(workerItemId)} · 只创建原工作空间的新执行轮次
      </p>
      <fieldset>
        <legend>返工来源</legend>
        {view.criteria.length > 0 ? view.criteria.map((criterion) => (
          <label key={criterion.criterion_key}>
            <input
              type="checkbox"
              checked={issueKeys.includes(criterion.criterion_key)}
              onChange={() => toggleIssue(criterion.criterion_key)}
            />
            <span>{criterion.text}</span>
          </label>
        )) : <span className="muted-copy">当前没有逐条检查标准，请在说明中写明问题来源。</span>}
      </fieldset>
      <label>
        <span>返工说明</span>
        <textarea
          aria-label="返工说明"
          value={reason}
          rows={3}
          maxLength={2000}
          onChange={(event) => setReason(event.target.value)}
          placeholder="说明失败来源、期望修复和不应扩大的范围"
        />
      </label>
      {error ? <div className="result-operation-form__error" role="alert">{error}</div> : null}
      <div className="result-operation-form__actions">
        <button className="quiet-button" type="button" onClick={onCancel} disabled={pending}>取消</button>
        <button className="primary-button" type="submit" disabled={!hasReason || pending} aria-busy={pending}>
          {pending ? "正在提交" : "提交局部返工"}
        </button>
      </div>
    </form>
  );
}

function initialIssueKeys(criteria: readonly Criterion[]): string[] {
  return criteria
    .filter((criterion) => ["failed", "unknown", "interrupted", "blocked"].includes(criterion.observed_status))
    .map((criterion) => criterion.criterion_key);
}

function shortId(value: string): string {
  return value.slice(0, 8);
}
