import { Check, CircleAlert, CircleX, ExternalLink, HelpCircle, LoaderCircle, RotateCcw } from "lucide-react";
import { Link } from "react-router-dom";

import type { JsonValue, MainAgentItem } from "./types";

export type ToolOutcome = "ok" | "needs_user" | "failed" | "unavailable" | "conflict" | "result_unknown";

export interface ParsedToolResult {
  toolName: string;
  outcome: ToolOutcome;
  summary: string;
  resourceRef: string | null;
  webUrl: string | null;
  data: Record<string, JsonValue>;
}

export function parseToolResult(item: MainAgentItem): ParsedToolResult | null {
  if (item.type !== "toolCall") return null;
  const data = item.data;
  const toolName = data.name;
  const outcome = data.outcome;
  if (data.receiptSource !== "sidera.product.task-receipt.v1"
    || (toolName !== "sidera_submit_goal" && toolName !== "sidera_request_revision")
    || !isUuid(data.receiptId) || item.id !== `product-receipt:${data.receiptId}`
    || !isUuid(data.logicalRequestId) || !isUuid(data.commandId)
    || !isUuid(data.runId) || !isUuid(data.responseId)
    || (outcome !== "applied" && outcome !== "rejected" && outcome !== "unknown")
    || data.verified !== (outcome === "applied")
    || typeof data.status !== "string" || !/^[a-z][a-z0-9_]{0,127}$/.test(data.status)
    || !isUuidOrNull(data.rootTaskId) || !isUuidOrNull(data.planningRequestId)
    || (data.webUrl !== null && typeof data.webUrl !== "string")
    || (data.errorCode !== null && typeof data.errorCode !== "string")) return null;
  if (outcome !== "applied" && (data.rootTaskId !== null || data.planningRequestId !== null || data.webUrl !== null)) return null;
  const resourceRef = data.rootTaskId !== null ? `task:${data.rootTaskId}`
    : data.planningRequestId !== null ? `planning_request:${data.planningRequestId}` : null;
  return {
    toolName,
    outcome: outcome === "applied" ? "ok" : outcome === "rejected" ? "failed" : "result_unknown",
    summary: outcome === "applied" ? `原命令已应用：${data.status}`
      : outcome === "rejected" ? `原命令已拒绝：${data.errorCode ?? data.status}` : "原命令结果待确认",
    resourceRef,
    webUrl: data.webUrl,
    data,
  };
}

export function ToolResultCard({
  result,
  workspaceId,
}: {
  result: ParsedToolResult;
  workspaceId: string;
}) {
  const deepLink = result.webUrl !== null && isSafeDeepLink(result.webUrl, workspaceId)
    ? result.webUrl
    : null;
  return (
    <article className={`tool-result-card tool-result-card--${result.outcome}`}>
      <div className="tool-result-card__heading">
        <span className="tool-result-card__icon"><OutcomeIcon outcome={result.outcome} /></span>
        <span className="tool-result-card__name">{result.toolName}</span>
        <span className="tool-result-card__outcome">{outcomeLabel(result.outcome)}</span>
      </div>
      <p>{result.summary}</p>
      {result.resourceRef !== null ? (
        <div className="tool-result-card__resource">
          <code>{result.resourceRef}</code>
          {deepLink ? (
            <Link to={deepLink} className="tool-result-card__link">
              查看对象 <ExternalLink size={13} aria-hidden="true" />
            </Link>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

export function isSafeDeepLink(value: string, workspaceId: string): boolean {
  try {
    const url = new URL(value, window.location.origin);
    const workspacePrefix = `/workspaces/${encodeURIComponent(workspaceId)}/`;
    return url.origin === window.location.origin
      && url.pathname.startsWith(workspacePrefix);
  } catch {
    return false;
  }
}

function OutcomeIcon({ outcome }: { outcome: ToolOutcome }) {
  if (outcome === "ok") return <Check size={14} aria-hidden="true" />;
  if (outcome === "needs_user") return <HelpCircle size={14} aria-hidden="true" />;
  if (outcome === "unavailable") return <LoaderCircle size={14} aria-hidden="true" />;
  if (outcome === "result_unknown") return <RotateCcw size={14} aria-hidden="true" />;
  if (outcome === "conflict") return <CircleAlert size={14} aria-hidden="true" />;
  return <CircleX size={14} aria-hidden="true" />;
}

function outcomeLabel(outcome: ToolOutcome): string {
  const labels: Record<ToolOutcome, string> = {
    ok: "已应用",
    needs_user: "需要处理",
    failed: "失败",
    unavailable: "暂不可用",
    conflict: "版本冲突",
    result_unknown: "等待确认",
  };
  return labels[outcome];
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function isUuidOrNull(value: unknown): value is string | null {
  return value === null || isUuid(value);
}
