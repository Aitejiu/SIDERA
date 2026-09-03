import { AlertTriangle, Check, ChevronDown, CircleDashed, Minus, RefreshCcw } from "lucide-react";
import { Link } from "react-router-dom";

import type { CollaborationDecision } from "./types";

export function DecisionHistory({
  decisions,
  isLoading,
  isError,
  hasNextPage,
  isLoadingNextPage,
  onLoadMore,
  onRetry,
}: {
  decisions: readonly CollaborationDecision[];
  isLoading: boolean;
  isError: boolean;
  hasNextPage: boolean;
  isLoadingNextPage: boolean;
  onLoadMore: () => void;
  onRetry: () => void;
}) {
  return (
    <section className="collaboration-history" aria-labelledby="collaboration-history-title">
      <div className="section-heading collaboration-history__heading">
        <div>
          <span className="eyebrow">DECISION LEDGER</span>
          <h2 id="collaboration-history-title">协同决策</h2>
        </div>
        <span className="mono-label">只读记录</span>
      </div>

      {isLoading ? <div className="collaboration-history__state" role="status">正在读取协同记录</div> : null}
      {isError ? (
        <div className="collaboration-history__state collaboration-history__state--error" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <strong>无法读取协同记录</strong>
          <button className="quiet-button" type="button" onClick={onRetry}>
            <RefreshCcw size={14} aria-hidden="true" />重试读取协同记录
          </button>
        </div>
      ) : null}

      {!isLoading && !isError && decisions.length === 0 ? (
        <div className="collaboration-history__state">当前计划代还没有协同决策。</div>
      ) : null}

      <div className="collaboration-history__list">
        {decisions.map((decision) => (
          <DecisionRow decision={decision} key={decision.decision_id} />
        ))}
      </div>

      {!isLoading && !isError && decisions.length > 0 ? (
        <footer className="collaboration-history__footer">
          {hasNextPage ? (
            <button
              className="quiet-button"
              type="button"
              onClick={onLoadMore}
              disabled={isLoadingNextPage}
            >
              <ChevronDown size={14} aria-hidden="true" />
              {isLoadingNextPage ? "读取中" : "加载更早记录"}
            </button>
          ) : (
            <span className="mono-label">没有更多协同记录</span>
          )}
        </footer>
      ) : null}
    </section>
  );
}

function DecisionRow({ decision }: { decision: CollaborationDecision }) {
  return (
    <article
      className="collaboration-decision"
      aria-label={`第 ${decision.sequence} 次协同决策`}
    >
      <div className="collaboration-decision__marker" aria-hidden="true">
        {decision.outcome === "intents" ? <Check size={15} /> : <Minus size={15} />}
      </div>
      <div className="collaboration-decision__body">
        <header className="collaboration-decision__header">
          <div>
            <strong>{outcomeLabel(decision.outcome)}</strong>
            <span className="collaboration-decision__reason">{decision.reason_code}</span>
          </div>
          <time dateTime={decision.created_at}>{formatDateTime(decision.created_at)}</time>
        </header>
        <p className="collaboration-decision__summary">{decision.summary}</p>

        <div className="collaboration-decision__targets">
          <span className="eyebrow">影响的子任务</span>
          {decision.target_paths.length > 0 ? (
            <div className="collaboration-decision__links">
              {decision.target_paths.map((target) => (
                <Link key={target.href} to={target.href}>{target.title}</Link>
              ))}
            </div>
          ) : (
            <span className="collaboration-decision__muted">没有目标子任务</span>
          )}
        </div>

        {decision.applications.length > 0 ? (
          <div className="collaboration-decision__applications">
            {decision.applications.map((application) => {
              const lineage = governanceLineage(application);
              return (
                <span className={`collaboration-application collaboration-application--${application.status}`} key={application.intent_id}>
                  {applicationStatusIcon(application.status)}
                  <span>{lineage?.label ?? applicationStatusLabel(application.status)}</span>
                  {lineage?.audit ? <span className="collaboration-application__audit">{lineage.audit}</span> : null}
                </span>
              );
            })}
          </div>
        ) : null}

        <details className="collaboration-decision__diagnostics">
          <summary>查看诊断</summary>
          <dl>
            <div><dt>Projection ID</dt><dd>{decision.projection_id}</dd></div>
            <div><dt>Projection hash</dt><dd>{decision.projection_hash}</dd></div>
            {decision.applications.flatMap((application) => application.error_code === null ? [] : [application.error_code]).map((errorCode) => (
              <div key={errorCode}><dt>错误代码</dt><dd>{errorCode}</dd></div>
            ))}
          </dl>
        </details>
      </div>
    </article>
  );
}

function outcomeLabel(outcome: CollaborationDecision["outcome"]): string {
  return outcome === "intents" ? "产生调度意图" : "无变更";
}

function applicationStatusLabel(status: CollaborationDecision["applications"][number]["status"]): string {
  switch (status) {
    case "applied":
      return "已应用";
    case "rejected":
      return "已拒绝";
    case "stale":
      return "已过期";
    case "duplicate":
      return "重复跳过";
    case "pending":
      return "处理中";
  }
}

function applicationStatusIcon(status: CollaborationDecision["applications"][number]["status"]) {
  if (status === "applied") return <Check size={13} aria-hidden="true" />;
  if (status === "pending") return <CircleDashed size={13} aria-hidden="true" />;
  return <AlertTriangle size={13} aria-hidden="true" />;
}

function governanceLineage(
  application: CollaborationDecision["applications"][number],
): { label: string; audit: string | null } | null {
  const audit = auditMetadata(application);
  if (application.status === "duplicate" || application.result_code === "governance_proposal_duplicate") {
    return { label: "重复跳过", audit: null };
  }
  if (
    application.resolution_result === "stale"
    || application.status === "stale"
    || application.proposal_status === "stale"
  ) {
    return { label: "已过期", audit };
  }
  if (application.resolution_action === "approve" && application.resolution_result === "applied") {
    return { label: "已批准", audit };
  }
  if (application.resolution_action === "reject" && application.resolution_result === "applied") {
    return { label: "已驳回", audit };
  }
  if (
    application.proposal_status === "pending"
    && application.resolution_action === null
    && application.resolution_result === null
  ) {
    return { label: "已延后放行", audit: null };
  }
  return null;
}

function auditMetadata(
  application: CollaborationDecision["applications"][number],
): string | null {
  const values = [application.resolution_actor, application.resolution_transport]
    .filter((value): value is string => value !== null && value !== undefined && value.length > 0);
  return values.length > 0 ? values.join(" · ") : null;
}

function formatDateTime(value: string): string {
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
