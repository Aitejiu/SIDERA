import { Clock3 } from "lucide-react";

import { formatTraceDuration, traceIsError, traceStatus } from "./tracePresentation";
import type { WorkerTrace } from "./types";

export function TraceSummary({ trace, executionStatus, sessionVersion, rootTaskId }: {
  trace: WorkerTrace;
  executionStatus: string | undefined;
  sessionVersion: number | undefined;
  rootTaskId: string;
}) {
  // Page timing is not whole-session wall time; missing measurements are not zero.
  const measured = trace.events.filter((event) => event.duration_ms !== null);
  const duration = measured.length === 0 ? null : measured.reduce((total, event) => total + (event.duration_ms ?? 0), 0);
  const usage = [...trace.events].reverse().find((event) => event.usage !== null)?.usage;
  const status = executionStatus ?? trace.status;
  return (
    <div className="trace-summary">
      <div className="trace-summary__line">
        <span className={`trace-summary__status${traceIsError(status, "") ? " trace-event-error" : ""}`}><span aria-hidden="true" />{traceStatus(status)}</span>
        <span className="trace-summary__metric" title="已加载记录中已提供的耗时之和，不代表整个会话的墙钟时间"><Clock3 size={14} aria-hidden="true" />已记录耗时 {formatTraceDuration(duration)}</span>
        <span className="trace-summary__usage">{usage ? `最新用量 输入 ${usage.input_tokens ?? "未提供"} / 输出 ${usage.output_tokens ?? "未提供"}` : "用量未提供"}</span>
        <details className="trace-session-details">
          <summary>会话信息</summary>
          <dl className="trace-facts">
            <div><dt>Root Task</dt><dd><code>{rootTaskId}</code></dd></div>
            <div><dt>Execution</dt><dd><code>{trace.execution_id}</code></dd></div>
            <div><dt>Session</dt><dd><code>{trace.session_id}</code></dd></div>
            <div><dt>Worker</dt><dd><code>{trace.worker_id ?? "未分配"}</code></dd></div>
            <div><dt>Plan generation</dt><dd>{trace.plan_generation ?? "未提供"}</dd></div>
            <div><dt>Session version</dt><dd>{sessionVersion ?? "未提供"}</dd></div>
            <div><dt>Checkpoint</dt><dd>{trace.checkpoint_sequence ?? "未提供"}</dd></div>
            <div><dt>更新时间</dt><dd>{trace.freshness_at ?? "未提供"}</dd></div>
          </dl>
        </details>
      </div>
    </div>
  );
}
