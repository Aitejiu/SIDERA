import { ArrowLeft, GitCompareArrows, History } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { AsyncState } from "@/shared/ui/AsyncState";

import { getPlanDiff, planDiffQueryKey } from "./api";

const markers = {
  unchanged: "=",
  patched: "~",
  added: "+",
  removed: "-",
} as const;

export function PlanDiffPage() {
  const { workspaceId, rootTaskId, left, right } = useParams<{
    workspaceId: string;
    rootTaskId: string;
    left: string;
    right: string;
  }>();
  const resolvedWorkspaceId = workspaceId ?? "";
  const resolvedRootTaskId = rootTaskId ?? "";
  const leftGeneration = Number(left);
  const rightGeneration = Number(right);
  const query = useQuery({
    queryKey: planDiffQueryKey(
      resolvedWorkspaceId,
      resolvedRootTaskId,
      leftGeneration,
      rightGeneration,
    ),
    queryFn: ({ signal }) => getPlanDiff(
      resolvedWorkspaceId,
      resolvedRootTaskId,
      leftGeneration,
      rightGeneration,
      signal,
    ),
    enabled: Number.isInteger(leftGeneration) && Number.isInteger(rightGeneration),
  });

  return (
    <section className="plan-page" aria-labelledby="plan-diff-title">
      <div className="breadcrumb">
        <Link to={`/workspaces/${resolvedWorkspaceId}/tasks/${resolvedRootTaskId}`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span>/</span>计划差异
      </div>
      <div className="page-heading plan-page__heading">
        <div>
          <span className="eyebrow">READ-ONLY PLAN COMPARISON</span>
          <h1 id="plan-diff-title">计划差异</h1>
          <p>Generation {leftGeneration} → Generation {rightGeneration}。比较结果只提供复用和上下文补丁建议。</p>
        </div>
        <Link className="quiet-button" to={`/workspaces/${resolvedWorkspaceId}/tasks/${resolvedRootTaskId}/plan/${rightGeneration}`}>
          <History size={14} aria-hidden="true" />查看目标版本
        </Link>
      </div>
      <AsyncState
        isLoading={query.isPending}
        isError={query.isError}
        errorMessage="无法读取计划差异"
        retryLabel="重试读取差异"
        onRetry={() => void query.refetch()}
      >
        {query.data ? (
          <>
            <div className="plan-diff-legend">
              <span><strong>=</strong> 可复用</span>
              <span><strong>~</strong> Context Patch 建议</span>
              <span><strong>+</strong> 新增</span>
              <span><strong>-</strong> 后续不再调度</span>
              <span><strong>R</strong> Portable Replay 候选</span>
            </div>
            <div className="plan-diff-table">
              {query.data.entries.map((entry) => {
                const marker = entry.portable_replay_candidate
                  ? "R"
                  : markers[entry.change];
                return (
                  <div className="plan-diff-row" key={entry.work_item_key}>
                    <span className={`plan-diff-marker plan-diff-marker--${entry.change}`}>{marker}</span>
                    <div className="plan-diff-entry">
                      <strong>{entry.work_item_key}</strong>
                      <span>
                        {entry.change === "unchanged"
                          ? "稳定 WorkItem，可复用既有运行上下文。"
                          : entry.change === "patched"
                          ? `建议由主 agent 注入 Context Patch：${entry.changed_fields.join(", ") || "任务上下文"}。`
                          : entry.change === "added"
                          ? "新增节点，等待后续调度。"
                          : "从未来调度中退出；当前运行不受影响。"}
                      </span>
                      {entry.runtime_execution_id ? <span className="mono-label">EXECUTION {entry.runtime_execution_id.slice(0, 8)}</span> : null}
                    </div>
                    {entry.portable_replay_candidate ? (
                      <span className="plan-diff-advisory"><GitCompareArrows size={14} aria-hidden="true" />Portable Replay 候选</span>
                    ) : entry.context_patch_candidate ? (
                      <span className="plan-diff-advisory">Context Patch</span>
                    ) : entry.reuse_candidate ? (
                      <span className="plan-diff-advisory">复用建议</span>
                    ) : null}
                  </div>
                );
              })}
            </div>
            <p className="plan-readonly-note">此页是历史证据与建议视图，不提供 Apply、Stop 或 Reassign 操作。</p>
          </>
        ) : null}
      </AsyncState>
    </section>
  );
}
