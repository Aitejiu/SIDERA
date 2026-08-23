import { useMemo, useState } from "react";

import type { PlanningClarificationAnswer, PlanningClarificationPage } from "./types";

type Clarification = PlanningClarificationPage["items"][number];

export function ClarificationPanel({
  clarifications,
  onSubmit,
  submitting = false,
}: {
  clarifications: Clarification[];
  onSubmit: (answers: PlanningClarificationAnswer[]) => void;
  submitting?: boolean;
}) {
  const blocking = clarifications.filter((clarification) => clarification.blocking && clarification.answer === null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const allAnswered = useMemo(
    () => blocking.length > 0 && blocking.every((clarification) => (answers[clarification.id] ?? "").trim().length > 0),
    [answers, blocking],
  );

  if (clarifications.length === 0) {
    return null;
  }

  return (
    <section className="planning-clarification-panel" aria-labelledby="planning-clarification-title">
      <div className="planning-section-heading">
        <div>
          <span className="eyebrow">INPUT REQUIRED</span>
          <h2 id="planning-clarification-title">需要你的回答</h2>
        </div>
        <span className="mono-label">一次提交全部阻塞问题</span>
      </div>
      <div className="planning-clarification-list">
        {clarifications.map((clarification) => (
          <ClarificationField
            key={clarification.id}
            clarification={clarification}
            value={answers[clarification.id] ?? ""}
            onChange={(value) => setAnswers((current) => ({ ...current, [clarification.id]: value }))}
          />
        ))}
      </div>
      <button
        className="quiet-button"
        type="button"
        onClick={() => onSubmit(blocking.map((clarification) => ({
          clarification_id: clarification.id,
          answer: answers[clarification.id]!.trim(),
        })))}
        disabled={!allAnswered || submitting}
      >
        {submitting ? "正在提交" : "提交澄清答复"}
      </button>
    </section>
  );
}

function ClarificationField({
  clarification,
  value,
  onChange,
}: {
  clarification: Clarification;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="planning-clarification-field">
      <div className="planning-clarification-field__question">
        <strong>{clarification.question}</strong>
        {clarification.blocking ? <span className="mono-label">阻塞</span> : <span className="mono-label">参考</span>}
        <p>{clarification.rationale}</p>
      </div>
      {clarification.answer_type === "single_choice" ? (
        <label className="planning-field">
          <span className="sr-only">{clarification.question}</span>
          <select aria-label={clarification.question} value={value} onChange={(event) => onChange(event.target.value)}>
            <option value="">请选择</option>
            {clarification.options.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        </label>
      ) : clarification.answer_type === "boolean" ? (
        <label className="planning-field">
          <span className="sr-only">{clarification.question}</span>
          <select aria-label={clarification.question} value={value} onChange={(event) => onChange(event.target.value)}>
            <option value="">请选择</option>
            <option value="true">是</option>
            <option value="false">否</option>
          </select>
        </label>
      ) : (
        <label className="planning-field">
          <span className="sr-only">澄清答复</span>
          <textarea
            aria-label="澄清答复"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            rows={3}
            placeholder="写下你的决定"
          />
        </label>
      )}
    </div>
  );
}
