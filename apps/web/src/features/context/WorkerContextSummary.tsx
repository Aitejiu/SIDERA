import { useQueries } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert, ExternalLink, PackageOpen } from "lucide-react";
import { Link } from "react-router-dom";

import { contextPackageByExecutionQueryKey, getContextPackageByExecution } from "./api";
import type { ContextPackage, ContextPackageAcknowledgement } from "./types";
import type { WorkerExecution } from "../worker-items/types";

export function WorkerContextSummary({
  workspaceId,
  executions,
}: {
  workspaceId: string;
  executions: readonly WorkerExecution[];
}) {
  const packageQueries = useQueries({
    queries: executions.map((execution) => ({
      queryKey: contextPackageByExecutionQueryKey(workspaceId, execution.execution_id),
      queryFn: ({ signal }: { signal: AbortSignal }) => getContextPackageByExecution(
        workspaceId,
        execution.execution_id,
        signal,
      ),
      enabled: Boolean(workspaceId && execution.execution_id),
      retry: false,
    })),
  });

  return (
    <section className="worker-context-summary" aria-label="WorkerItem 上下文">
      <div className="worker-context-summary__heading">
        <div>
          <span className="eyebrow">CONTEXT PACKAGES</span>
          <h3><PackageOpen size={15} aria-hidden="true" />运行上下文</h3>
        </div>
        <span className="mono-label">{executions.length} RUNS</span>
      </div>
      {executions.length === 0 ? <p className="muted-copy">还没有执行记录</p> : null}
      <div className="worker-context-summary__list">
        {executions.map((execution, index) => {
          const query = packageQueries[index];
          const contextPackage = query?.data as ContextPackage | undefined;
          const label = `执行尝试 #${execution.attempt} · ${index === 0 ? "当前" : "历史"}`;
          return (
            <article className="worker-context-row" key={execution.execution_id}>
              <div className="worker-context-row__heading">
                <strong>{label}</strong>
                <span>{execution.status}</span>
              </div>
              {query?.isPending ? <span className="muted-copy">读取上下文包</span> : null}
              {query?.isError ? (
                <span className="worker-context-row__unavailable">上下文包暂不可用</span>
              ) : null}
              {contextPackage ? (
                <>
                  <span className="worker-context-row__package">
                    Package {packageStatusLabel(contextPackage.status)} · {shortHash(contextPackage.package_hash)}
                  </span>
                  <span className={`worker-context-row__ack${isAcknowledged(contextPackage, execution.execution_id) ? " is-acknowledged" : " is-pending"}`}>
                    {isAcknowledged(contextPackage, execution.execution_id)
                      ? <CheckCircle2 size={13} aria-hidden="true" />
                      : <CircleAlert size={13} aria-hidden="true" />}
                    context V{contextPackage.context_version ?? "--"} · {isAcknowledged(contextPackage, execution.execution_id) ? "已确认" : "未确认"}
                  </span>
                  <Link
                    className="worker-context-row__link"
                    to={`/workspaces/${encodeURIComponent(workspaceId)}/context-packages/${encodeURIComponent(contextPackage.package_id)}`}
                    aria-label={`查看本次运行上下文：执行尝试 #${execution.attempt}`}
                  >
                    查看本次运行上下文 <ExternalLink size={13} aria-hidden="true" />
                  </Link>
                </>
              ) : null}
            </article>
          );
        })}
      </div>
    </section>
  );
}

function isAcknowledged(contextPackage: ContextPackage, executionId: string): boolean {
  return contextPackage.acknowledgements.some((acknowledgement) => isMatchingAcknowledgement(acknowledgement, contextPackage, executionId));
}

function isMatchingAcknowledgement(
  acknowledgement: ContextPackageAcknowledgement,
  contextPackage: ContextPackage,
  executionId: string,
): boolean {
  return acknowledgement.consumer_id === executionId
    && acknowledgement.package_id === contextPackage.package_id
    && acknowledgement.package_hash === contextPackage.package_hash;
}

function packageStatusLabel(status: string): string {
  if (status === "active") return "active";
  if (status === "revoked") return "revoked";
  if (status === "corrupt") return "corrupt";
  return status;
}

function shortHash(value: string): string {
  return value.slice(0, 8);
}
