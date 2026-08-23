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

const OUTCOMES = new Set<ToolOutcome>([
  "ok",
  "needs_user",
  "failed",
  "unavailable",
  "conflict",
  "result_unknown",
]);

export function parseToolResult(item: MainAgentItem): ParsedToolResult | null {
  if (item.type !== "toolCall") return null;
  const toolName = stringValue(item.data.name ?? item.data.toolName);
  const preview = stringValue(item.data.resultPreview ?? item.data.result_preview);
  if (toolName === null || !toolName.startsWith("sidera_") || preview === null) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(preview) as unknown;
  } catch {
    return null;
  }
  if (!isRecord(parsed)) return null;
  const outcome = parsed.outcome;
  const summary = parsed.summary;
  if (!isOutcome(outcome) || typeof summary !== "string" || summary.trim() === "") return null;
  const resourceRef = parsed.resource_ref;
  const webUrl = parsed.web_url;
  if (resourceRef !== undefined && resourceRef !== null && typeof resourceRef !== "string") return null;
  if (webUrl !== undefined && webUrl !== null && typeof webUrl !== "string") return null;
  const data = parsed.data === undefined
    ? {}
    : isRecord(parsed.data)
      ? parsed.data
      : null;
  if (data === null) return null;
  const normalizedResourceRef = typeof resourceRef === "string" && resourceRef !== ""
    ? resourceRef
    : null;
  const normalizedWebUrl = typeof webUrl === "string" && webUrl !== ""
    ? webUrl
    : null;
  return {
    toolName,
    outcome,
    summary,
    resourceRef: normalizedResourceRef,
    webUrl: normalizedWebUrl,
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
    ok: "已完成",
    needs_user: "需要处理",
    failed: "失败",
    unavailable: "暂不可用",
    conflict: "版本冲突",
    result_unknown: "等待确认",
  };
  return labels[outcome];
}

function isOutcome(value: unknown): value is ToolOutcome {
  return typeof value === "string" && OUTCOMES.has(value as ToolOutcome);
}

function stringValue(value: JsonValue | undefined): string | null {
  return typeof value === "string" ? value : null;
}

function isRecord(value: unknown): value is Record<string, JsonValue> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
