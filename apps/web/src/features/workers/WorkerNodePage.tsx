import { ArrowLeft, Settings2 } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import { getWorker, workerQueryKey } from "./api";
import { formatDateTime, runtimeStatusLabel } from "./WorkerNodesPage";
import { WorkerRuntimeDialog } from "./WorkerRuntimeDialog";
import type { ModuleSummary, Worker } from "./types";

export function WorkerNodePage() {
  const { workspaceId = "", workerId = "" } = useParams<{
    workspaceId: string;
    workerId: string;
  }>();
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const query = useQuery({
    queryKey: workerQueryKey(workspaceId, workerId),
    queryFn: ({ signal }) => getWorker(workspaceId, workerId, signal),
    enabled: Boolean(workspaceId && workerId),
  });

  return (
    <section className="worker-node-page" aria-labelledby="worker-node-title">
      {query.isPending ? (
        <div className="worker-node-detail-state" role="status">正在读取执行节点详情</div>
      ) : null}
      {query.isError ? (
        <div className="worker-node-detail-state worker-node-detail-state--error" role="alert">
          <strong>无法读取执行节点详情</strong>
          <p>控制面没有返回这个节点的最新事实。</p>
          <button className="quiet-button" type="button" onClick={() => void query.refetch()}>
            重试读取
          </button>
        </div>
      ) : null}
      {query.data ? (
        <WorkerNodeContent
          workspaceId={workspaceId}
          worker={query.data}
          onOpenRuntime={() => setIsDialogOpen(true)}
        />
      ) : null}
      {isDialogOpen && query.data ? (
        <WorkerRuntimeDialog
          workspaceId={workspaceId}
          worker={query.data}
          onClose={() => setIsDialogOpen(false)}
          onSaved={async () => {
            setIsDialogOpen(false);
            await query.refetch();
          }}
        />
      ) : null}
    </section>
  );
}

function WorkerNodeContent({
  workspaceId,
  worker,
  onOpenRuntime,
}: {
  workspaceId: string;
  worker: Worker;
  onOpenRuntime: () => void;
}) {
  const health = healthFor(worker.status);
  return (
    <>
      <div className="breadcrumb worker-node-page__breadcrumb">
        <Link to={`/workspaces/${workspaceId}/workers`}>
          <ArrowLeft size={13} aria-hidden="true" />执行节点
        </Link>
        <span aria-hidden="true">/</span>
        <span>{worker.display_name}</span>
      </div>
      <header className="worker-node-page__heading">
        <div>
          <span className="eyebrow">EXECUTION NODE / RUNTIME</span>
          <h1 id="worker-node-title">{worker.display_name}</h1>
          <p>{worker.adapter_type} · {worker.id.slice(0, 8)}</p>
        </div>
        <div className="worker-node-page__actions">
          <StatusMark kind={health.kind} label={health.label} />
          <button className="quiet-button" type="button" onClick={onOpenRuntime}>
            <Settings2 size={14} aria-hidden="true" />
            调整运行配置
          </button>
        </div>
      </header>

      <div className="worker-node-page__layout">
        <section className="worker-node-facts" aria-label="执行节点事实">
          <div className="worker-node-section-heading">
            <div>
              <span className="eyebrow">NODE FACTS</span>
              <h2>节点事实</h2>
            </div>
            <span className="mono-label">WORKER V{worker.version}</span>
          </div>
          <dl className="worker-fact-grid">
            <Fact label="健康" value={health.label} />
            <Fact label="期望状态" value={worker.desired_state} />
            <Fact label="容量" value={`${worker.current_load} / ${worker.capacity}`} />
            <Fact label="能力" value={worker.capabilities.join(" · ")} />
            <Fact label="最近 heartbeat" value={formatDateTime(worker.last_heartbeat_at)} />
            <Fact label="启动延迟" value={`${worker.startup_latency_ms} ms`} />
          </dl>
        </section>

        <section className="worker-node-runtime" aria-label="运行配置事实">
          <div className="worker-node-section-heading">
            <div>
              <span className="eyebrow">RUNTIME BINDING</span>
              <h2>运行配置</h2>
            </div>
            <span className={`worker-runtime-status worker-runtime-status--${worker.runtime_configuration_status}`}>
              {runtimeStatusLabel(worker.runtime_configuration_status)}
            </span>
          </div>
          <div className="worker-module-facts">
            <ModuleFact label="Adapter" module={worker.adapter_module_summary} />
            <ModuleFact label="工作区提供器" module={worker.workspace_module_summary} />
          </div>
          <dl className="worker-fact-grid worker-fact-grid--runtime">
            <Fact
              label="配置版本"
              value={worker.runtime_configuration_version === null ? "尚未配置" : `V${worker.runtime_configuration_version}`}
            />
            <Fact label="配置时间" value={formatDateTime(worker.runtime_configured_at)} />
          </dl>
          {worker.runtime_configuration_reason ? (
            <p className="worker-node-runtime__reason">{worker.runtime_configuration_reason}</p>
          ) : null}
        </section>
      </div>
    </>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function ModuleFact({ label, module }: { label: string; module: ModuleSummary | null }) {
  return (
    <div className="worker-module-fact">
      <span className="mono-label">{label}</span>
      {module ? (
        <>
          <strong>{module.instance_name}</strong>
          <span>{module.provider}</span>
          <span className="mono-label">CONFIG V{module.config_version} · {module.instance_lifecycle}</span>
        </>
      ) : (
        <strong className="worker-module-fact__empty">尚未绑定</strong>
      )}
    </div>
  );
}

function healthFor(status: string): { kind: StatusMarkKind; label: string } {
  if (status === "idle") return { kind: "live", label: "空闲" };
  if (status === "reserved" || status === "busy") return { kind: "running", label: "执行中" };
  if (status === "draining") return { kind: "stale", label: "排空中" };
  if (status === "unhealthy") return { kind: "failed", label: "不健康" };
  return { kind: "offline", label: "离线" };
}
