import type { ExecutionInput } from "./types";

export function ExecutionInputHistory({ inputs }: { inputs: ExecutionInput[] }) {
  const orderedInputs = [...inputs].reverse();

  return (
    <section className="execution-input-history" aria-labelledby="execution-input-history-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">INTERVENTION LOG</span>
          <h2 id="execution-input-history-title">用户介入记录</h2>
        </div>
        <span className="composer-state">{inputs.length} 条</span>
      </div>
      {orderedInputs.length === 0 ? (
        <p className="empty-state">当前 WorkItem 还没有用户介入。</p>
      ) : (
        <ol className="execution-input-history__list">
          {orderedInputs.map((input) => (
            <li key={input.id} className="execution-input-history__item">
              <div className="execution-input-history__meta">
                <strong>{inputStatusLabel(input.status)}</strong>
                <time dateTime={input.created_at}>{formatTime(input.created_at)}</time>
              </div>
              <span>{input.text_length} 字{input.attachment_count > 0 ? ` · ${input.attachment_count} 个附件` : ""}</span>
              <code>{input.client_idempotency_key}</code>
              {input.failure_code ? <small>失败原因：{input.failure_code}</small> : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function inputStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    accepted: "已接受",
    queued: "已排队",
    dispatching: "投递中",
    delivered: "已投递",
    blocked: "已阻塞",
    failed: "投递失败",
    cancelled: "已取消",
    superseded: "已失效",
    canceled: "已取消",
    rejected: "已拒绝",
  };
  return labels[status] ?? status;
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
