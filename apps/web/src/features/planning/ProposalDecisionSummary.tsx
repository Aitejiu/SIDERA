import type { ProposalViewModel } from "./proposalTypes";

export function ProposalDecisionSummary({ summary }: { summary: ProposalViewModel["decisionSummary"] }) {
  return (
    <section className="planning-proposal__summary" aria-labelledby="proposal-summary-title">
      <div className="planning-section-heading">
        <div>
          <span className="eyebrow">DECISION SUMMARY</span>
          <h2 id="proposal-summary-title">方案理解</h2>
        </div>
        <span className="mono-label">先审阅，再确认</span>
      </div>
      <p className="planning-proposal__understanding">{summary.understanding}</p>
      <div className="planning-proposal__summary-grid">
        <SummaryList title="假设" items={summary.assumptions} empty="没有额外假设" />
        <SummaryList title="风险" items={summary.risks} empty="规划器未标记风险" tone="risk" />
        <SummaryList title="范围内" items={summary.scopeIncludes} empty="未声明范围" />
        <SummaryList title="范围外" items={summary.scopeExcludes} empty="没有排除项" />
      </div>
      <dl className="planning-proposal__estimate" aria-label="方案估算">
        <div><dt>预计 Token</dt><dd>{summary.estimatedTokens.toLocaleString()}</dd></div>
        <div><dt>预计成本</dt><dd>{formatMicrounits(summary.estimatedCostMicrounits)}</dd></div>
        <div><dt>预计运行</dt><dd>{formatDuration(summary.estimatedRuntimeSeconds)}</dd></div>
        <div><dt>并发上限</dt><dd>{summary.estimatedConcurrency}</dd></div>
      </dl>
    </section>
  );
}

function SummaryList({
  title,
  items,
  empty,
  tone,
}: {
  title: string;
  items: string[];
  empty: string;
  tone?: "risk";
}) {
  return (
    <div className={`planning-proposal__summary-list${tone === "risk" ? " planning-proposal__summary-list--risk" : ""}`}>
      <span>{title}</span>
      {items.length > 0 ? <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul> : <p>{empty}</p>}
    </div>
  );
}

function formatMicrounits(value: number): string {
  return value === 0 ? "未估算" : `${value.toLocaleString()} μu`;
}

function formatDuration(seconds: number): string {
  if (seconds <= 0) return "未估算";
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
