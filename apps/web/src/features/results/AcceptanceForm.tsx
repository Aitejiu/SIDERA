import { useState, type FormEvent } from "react";

import type { ResultAction, TaskResultOverview, AcceptanceCriterionDecision } from "./api";

type Criterion = TaskResultOverview["criteria"][number];
type Decision = AcceptanceCriterionDecision["decision"];

export function AcceptanceForm({
  view,
  action,
  onSubmit,
  pending = false,
}: {
  view: TaskResultOverview;
  action: ResultAction;
  onSubmit: (criteria: AcceptanceCriterionDecision[]) => void | Promise<void>;
  pending?: boolean;
}) {
  const [draft, setDraft] = useState<Record<string, DraftDecision>>(() => initialDraft(view.criteria));

  const updateDecision = (criterionKey: string, decision: string) => {
    setDraft((current) => ({
      ...current,
      [criterionKey]: {
        decision: isDecision(decision) ? decision : "",
        reason: current[criterionKey]?.reason ?? "",
      },
    }));
  };

  const updateReason = (criterionKey: string, reason: string) => {
    setDraft((current) => ({
      ...current,
      [criterionKey]: {
        decision: current[criterionKey]?.decision ?? "",
        reason,
      },
    }));
  };

  const complete = view.criteria.length > 0 && view.criteria.every((criterion) => {
    const value = draft[criterion.criterion_key];
    return value?.decision !== ""
      && value?.decision !== undefined
      && (value.decision !== "exception_accepted" || value.reason.trim() !== "");
  });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!complete) return;
    await onSubmit(view.criteria.map((criterion) => {
      const value = draft[criterion.criterion_key]!;
      return {
        criterion_key: criterion.criterion_key,
        decision: value.decision as Decision,
        reason: value.reason.trim() === "" ? null : value.reason.trim(),
      };
    }));
  }

  return (
    <form className="result-acceptance-form" onSubmit={(event) => void submit(event)}>
      <div className="result-acceptance-form__heading">
        <div>
          <span className="eyebrow">USER DECISION</span>
          <h3>逐条确认</h3>
        </div>
        <span className="mono-label">RESULT V{action.expected_result_version}</span>
      </div>
      <p className="result-acceptance-form__hint">检查结论只提供事实，最终决定由你逐条提交。</p>
      <p className="result-acceptance-form__policy">
        {view.apply_policy?.apply_on_accept ? "接受后按计划自动应用" : "默认不自动应用"}
      </p>
      <div className="result-acceptance-form__criteria">
        {view.criteria.map((criterion) => (
          <CriterionDecision
            key={criterion.criterion_key}
            criterion={criterion}
            value={draft[criterion.criterion_key] ?? { decision: "", reason: "" }}
            onDecisionChange={(decision) => updateDecision(criterion.criterion_key, decision)}
            onReasonChange={(reason) => updateReason(criterion.criterion_key, reason)}
          />
        ))}
      </div>
      <button className="primary-button" type="submit" disabled={!complete || pending} aria-busy={pending}>
        {pending ? "正在提交验收" : "接受成果"}
      </button>
    </form>
  );
}

function CriterionDecision({
  criterion,
  value,
  onDecisionChange,
  onReasonChange,
}: {
  criterion: Criterion;
  value: DraftDecision;
  onDecisionChange: (value: string) => void;
  onReasonChange: (value: string) => void;
}) {
  const exceptionAllowed = criterion.observed_status === "failed"
    && !["unknown", "interrupted", "blocked"].includes(criterion.verification_status);
  const manualAllowed = criterion.mode === "manual";
  const passedAllowed = criterion.observed_status === "passed";
  const decisionLabel = `${criterion.text} 的决定`;
  return (
    <fieldset className="result-acceptance-criterion">
      <legend>{criterion.text}</legend>
      <div className="result-acceptance-criterion__observation">
        <span>检查结论</span>
        <strong className={`result-acceptance-status result-acceptance-status--${criterion.observed_status}`}>
          {criterionStatusLabel(criterion.observed_status)}
        </strong>
        <small>{criterion.verification_status}</small>
      </div>
      <label>
        <span>决定</span>
        <select
          aria-label={decisionLabel}
          value={value.decision}
          onChange={(event) => onDecisionChange(event.target.value)}
        >
          <option value="">选择决定</option>
          <option value="accept_passed" disabled={!passedAllowed}>按检查通过</option>
          <option value="manual_passed" disabled={!manualAllowed}>人工确认通过</option>
          <option value="exception_accepted" disabled={!exceptionAllowed}>接受例外</option>
        </select>
      </label>
      {value.decision === "exception_accepted" ? (
        <label>
          <span>例外原因</span>
          <textarea
            aria-label="例外原因"
            value={value.reason}
            rows={2}
            onChange={(event) => onReasonChange(event.target.value)}
          />
        </label>
      ) : null}
    </fieldset>
  );
}

type DraftDecision = { decision: Decision | ""; reason: string };

function initialDraft(criteria: readonly Criterion[]): Record<string, DraftDecision> {
  return Object.fromEntries(criteria.map((criterion) => [criterion.criterion_key, {
    decision: isDecision(criterion.decision) ? criterion.decision : "",
    reason: criterion.decision_reason ?? "",
  }])) as Record<string, DraftDecision>;
}

function isDecision(value: string | null): value is Decision {
  return value === "accept_passed"
    || value === "manual_passed"
    || value === "exception_accepted";
}

function criterionStatusLabel(value: string): string {
  return {
    passed: "已通过",
    failed: "未通过",
    pending: "待检查",
  }[value] ?? value;
}
