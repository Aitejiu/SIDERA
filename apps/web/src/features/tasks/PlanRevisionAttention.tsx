import { ArrowUpRight, GitBranch, TriangleAlert } from "lucide-react";
import { Link } from "react-router-dom";

import type { components } from "@/shared/api/schema";

type PlanRevisionAttentionProjection = components["schemas"]["PlanRevisionAttentionProjectionResponse"];

export function PlanRevisionAttention({
  attention,
  rootTaskTitle,
}: {
  attention: PlanRevisionAttentionProjection | null | undefined;
  rootTaskTitle: string;
}) {
  if (attention === null || attention === undefined) {
    return null;
  }

  const scope = attention.subtask_path.length > 0
    ? attention.subtask_path.join(" / ")
    : "根任务计划";

  return (
    <section className="plan-revision-attention" aria-labelledby="plan-revision-attention-title">
      <div className="plan-revision-attention__marker" aria-hidden="true">
        <TriangleAlert size={18} />
      </div>
      <div className="plan-revision-attention__body">
        <div className="plan-revision-attention__heading">
          <div>
            <span className="eyebrow"><GitBranch size={12} aria-hidden="true" />PLAN REVISION</span>
            <h2 id="plan-revision-attention-title">计划调整待处理</h2>
          </div>
          <span className="plan-revision-attention__status">{revisionAttentionStatusLabel(attention.status)}</span>
        </div>
        <strong>{rootTaskTitle} / {scope}</strong>
        <p>{attention.summary}</p>
        <span className="mono-label">请求 {attention.planning_request_id.slice(0, 8)} · {formatOccurredAt(attention.occurred_at)}</span>
      </div>
      <Link className="quiet-button plan-revision-attention__action" to={attention.deep_link}>
        <ArrowUpRight size={14} aria-hidden="true" />查看计划调整
      </Link>
    </section>
  );
}

function revisionAttentionStatusLabel(status: string): string {
  switch (status) {
    case "proposal_ready":
    case "waiting_for_user":
      return "等待确认";
    case "conflicted":
      return "基准冲突";
    case "failed":
      return "生成失败";
    default:
      return status;
  }
}

function formatOccurredAt(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return "时间未知";
  }
  return new Date(timestamp).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
