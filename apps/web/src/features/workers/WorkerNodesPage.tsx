import { RefreshCw, ServerCog } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import { listWorkers, workersQueryKey } from "./api";
import type { Worker } from "./types";

export function WorkerNodesPage() {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? "";
  const query = useQuery({
    queryKey: workersQueryKey(workspaceId),
    queryFn: ({ signal }) => listWorkers(workspaceId, signal),
    enabled: workspaceId.length > 0,
  });

  return (
    <section className="worker-nodes-page" aria-labelledby="worker-nodes-title">
      <div className="breadcrumb">工作区 / 执行节点</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">WORKER RUNTIME CONTROL</span>
          <h1 id="worker-nodes-title">执行节点</h1>
        </div>
        <div className="page-heading__actions">
          <button
            className="icon-button"
            type="button"
            onClick={() => void query.refetch()}
            aria-label="刷新执行节点"
            title="刷新执行节点"
          >
            <RefreshCw size={16} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="worker-nodes-intro">
        <div>
          <ServerCog size={16} aria-hidden="true" />
          <span>新执行会从这里选择可用的运行环境。</span>
        </div>
        <span className="mono-label">{query.data?.length ?? 0} NODES</span>
      </div>

      {query.isPending ? <WorkerNodesLoading /> : null}
      {query.isError ? (
        <div className="worker-nodes-state worker-nodes-state--error" role="alert">
          <strong>无法读取执行节点</strong>
          <p>控制面暂时没有返回节点状态。</p>
          <button className="quiet-button" type="button" onClick={() => void query.refetch()}>
            重试读取
          </button>
        </div>
      ) : null}
      {!query.isPending && !query.isError && query.data?.length === 0 ? (
        <div className="worker-nodes-state" role="status">
          <span className="eyebrow">WORKSPACE EMPTY</span>
          <strong>当前工作区还没有执行节点</strong>
          <p>注册一个基础设施 Worker 后，它会出现在这里。</p>
        </div>
      ) : null}
      {!query.isPending && !query.isError && query.data && query.data.length > 0 ? (
        <WorkerNodeTable workspaceId={workspaceId} workers={query.data} />
      ) : null}
    </section>
  );
}

function WorkerNodesLoading() {
  return (
    <div className="worker-nodes-state worker-nodes-state--loading" data-testid="worker-nodes-loading">
      <span className="worker-nodes-loading__bar" />
      <span className="worker-nodes-loading__bar" />
      <span className="worker-nodes-loading__bar" />
      <span className="mono-label">正在读取执行节点</span>
    </div>
  );
}

function WorkerNodeTable({ workspaceId, workers }: { workspaceId: string; workers: Worker[] }) {
  return (
    <div className="worker-node-table" aria-label="执行节点列表">
      <div className="worker-node-table__header" aria-hidden="true">
        <span>节点名</span>
        <span>Adapter type</span>
        <span>健康</span>
        <span>容量</span>
        <span>运行配置</span>
        <span>最近 heartbeat</span>
      </div>
      <div className="worker-node-table__rows">
        {workers.map((worker) => {
          const health = healthFor(worker.status);
          return (
            <Link
              className="worker-node-row"
              to={`/workspaces/${workspaceId}/workers/${worker.id}`}
              key={worker.id}
              aria-label={worker.display_name}
            >
              <span className="worker-node-cell worker-node-cell--identity" data-label="节点名">
                <strong>{worker.display_name}</strong>
                <span className="mono-label">{worker.id.slice(0, 8)}</span>
              </span>
              <span className="worker-node-cell worker-node-cell--adapter" data-label="Adapter type">
                <span>{worker.adapter_type}</span>
              </span>
              <span className="worker-node-cell worker-node-cell--health" data-label="健康">
                <StatusMark kind={health.kind} label={health.label} />
              </span>
              <span className="worker-node-cell worker-node-cell--capacity" data-label="容量">
                <strong>{worker.current_load} / {worker.capacity}</strong>
                <span className="mono-label">{worker.current_load >= worker.capacity ? "已满" : "可接新执行"}</span>
              </span>
              <span className="worker-node-cell worker-node-cell--runtime" data-label="运行配置">
                <RuntimeStatus status={worker.runtime_configuration_status} />
                {worker.runtime_configuration_version !== null ? (
                  <span className="mono-label">V{worker.runtime_configuration_version}</span>
                ) : null}
              </span>
              <span className="worker-node-cell worker-node-cell--heartbeat" data-label="最近 heartbeat">
                {formatDateTime(worker.last_heartbeat_at)}
              </span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}

export function RuntimeStatus({ status }: { status: string }) {
  return <span className={`worker-runtime-status worker-runtime-status--${status}`}>{runtimeStatusLabel(status)}</span>;
}

export function runtimeStatusLabel(status: string): string {
  if (status === "ready") return "配置可用";
  if (status === "missing") return "未配置";
  if (status === "invalid") return "配置失效";
  return "状态未知";
}

function healthFor(status: string): { kind: StatusMarkKind; label: string } {
  if (status === "idle") return { kind: "live", label: "空闲" };
  if (status === "reserved" || status === "busy") return { kind: "running", label: "执行中" };
  if (status === "draining") return { kind: "stale", label: "排空中" };
  if (status === "unhealthy") return { kind: "failed", label: "不健康" };
  return { kind: "offline", label: "离线" };
}

export function formatDateTime(value: string | null): string {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
