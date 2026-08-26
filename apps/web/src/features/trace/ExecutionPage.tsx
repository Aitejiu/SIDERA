import { ArrowLeft, Code2, PackageOpen, RefreshCcw, SearchCode, X } from "lucide-react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";

import { StatusMark } from "@/shared/ui/StatusMark";

import { contextPackageByExecutionQueryKey, getContextPackageByExecution } from "../context/api";
import {
  executionQueryKey,
  executionSessionQueryKey,
  getExecution,
  getExecutionSession,
  getWorkerTrace,
  workerTraceQueryKey,
} from "./api";
import { ArtifactViewer } from "./ArtifactViewer";
import { TraceDetails } from "./TraceDetails";
import { TraceLedger } from "./TraceLedger";
import { TraceTiming } from "./TraceTiming";
import { WorkerTracePage } from "./WorkerTracePage";
import type { WorkerTraceEvent } from "./types";

export function ExecutionPage() {
  const {
    workspaceId = "",
    rootTaskId = "",
    executionId = "",
    sessionId = "",
  } = useParams<{ workspaceId: string; rootTaskId: string; executionId: string; sessionId: string }>();
  const [selectedSequence, setSelectedSequence] = useState<number | null>(null);
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
  const [traceOpen, setTraceOpen] = useState(false);

  const traceQuery = useInfiniteQuery({
    queryKey: workerTraceQueryKey(workspaceId, executionId, sessionId, rootTaskId),
    initialPageParam: undefined as number | undefined,
    queryFn: ({ signal, pageParam }) => getWorkerTrace(workspaceId, rootTaskId, executionId, sessionId, {
      ...(pageParam === undefined ? {} : { beforeSequence: pageParam }),
      signal,
    }),
    getNextPageParam: () => undefined,
    getPreviousPageParam: (firstPage) => firstPage.previous_before_sequence ?? undefined,
    enabled: Boolean(workspaceId && rootTaskId && executionId && sessionId && sessionId !== "unknown"),
  });
  const sessionQuery = useQuery({
    queryKey: executionSessionQueryKey(workspaceId, sessionId),
    queryFn: ({ signal }) => getExecutionSession(workspaceId, sessionId, signal),
    enabled: Boolean(workspaceId && sessionId && sessionId !== "unknown"),
  });
  const executionQuery = useQuery({
    queryKey: executionQueryKey(workspaceId, executionId),
    queryFn: ({ signal }) => getExecution(workspaceId, executionId, signal),
    enabled: Boolean(workspaceId && executionId),
  });
  const contextPackageQuery = useQuery({
    queryKey: contextPackageByExecutionQueryKey(workspaceId, executionId),
    queryFn: ({ signal }) => getContextPackageByExecution(workspaceId, executionId, signal),
    enabled: Boolean(workspaceId && executionId),
    retry: false,
  });
  const trace = useMemo(() => {
    const pages = traceQuery.data?.pages ?? [];
    const [firstPage] = pages;
    if (firstPage === undefined) return undefined;
    return { ...firstPage, events: pages.flatMap((page) => page.events) };
  }, [traceQuery.data?.pages]);
  const selectedEvent = useMemo(() => {
    if (trace === undefined || trace.events.length === 0) return null;
    return trace.events.find((event) => event.sequence === selectedSequence) ?? trace.events.at(-1) ?? null;
  }, [selectedSequence, trace]);
  const selectEvent = (event: WorkerTraceEvent) => setSelectedSequence(event.sequence);

  if (executionQuery.data?.worker_item_id) {
    return <Navigate replace to={`/workspaces/${workspaceId}/worker-items/${executionQuery.data.worker_item_id}`} />;
  }

  return (
    <section className="execution-page" aria-labelledby="execution-page-title">
      <div className="breadcrumb trace-breadcrumb">
        <Link to={`/workspaces/${workspaceId}/tasks/${rootTaskId}`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span>/</span><span>WorkItem</span><span>/</span><span>Execution</span>
      </div>
      <header className="execution-page__heading">
        <div>
          <span className="eyebrow">EXECUTION / WORK ITEM ACTIVITY</span>
          <h1 id="execution-page-title">执行步骤</h1>
          <p className="trace-address-line"><code>{executionId}</code> / <code>{sessionId}</code></p>
        </div>
        <div className="trace-page-heading__state">
          <StatusMark kind={statusKind(executionQuery.data?.status ?? trace?.status ?? "connecting")} />
          <span className="mono-label">{trace?.current_action ?? "等待执行活动"}</span>
        </div>
      </header>

      <div className="execution-page__actions">
        <span className="execution-page__scope"><Code2 size={14} aria-hidden="true" />旧 Execution 仅保留执行步骤与诊断信息；介入请进入 WorkerItem 工作区。</span>
        {contextPackageQuery.data ? (
          <Link
            className="quiet-button"
            to={`/workspaces/${encodeURIComponent(workspaceId)}/context-packages/${encodeURIComponent(contextPackageQuery.data.package_id)}`}
          >
            <PackageOpen size={14} aria-hidden="true" />查看本次运行上下文
          </Link>
        ) : null}
        <button className="quiet-button" type="button" onClick={() => setTraceOpen(true)} disabled={trace === undefined}><SearchCode size={14} aria-hidden="true" />查看 Agent Trace</button>
      </div>

      {executionQuery.data?.worker_item_id === null ? (
        <div className="execution-legacy-notice" role="status">
          <strong>旧 Execution 只读</strong>
          <span>这个执行记录没有唯一的 WorkerItem 映射，不能在此发送输入或停止运行。</span>
        </div>
      ) : null}

      {traceQuery.isError ? <div className="trace-provider-warning" role="alert"><RefreshCcw size={16} aria-hidden="true" /><span>执行步骤暂不可用，Trace Provider 返回错误。</span><button className="quiet-button" type="button" onClick={() => void traceQuery.refetch()}>重试</button></div> : null}

      {trace ? (
        <div className="execution-primary-grid">
          <div className="trace-main-column">
            <TraceTiming timing={trace.timing} />
            <TraceLedger
              title="执行步骤"
              events={trace.events}
              selectedSequence={selectedEvent?.sequence ?? null}
              onSelect={selectEvent}
              onLoadOlder={() => void traceQuery.fetchPreviousPage()}
              hasOlder={traceQuery.hasPreviousPage === true}
              isLoadingOlder={traceQuery.isFetchingPreviousPage}
              followTail={true}
              onFollowTail={() => undefined}
              onPauseFollow={() => undefined}
            />
            <TraceDetails event={selectedEvent} onArtifactSelect={setSelectedArtifactId} />
            <ArtifactViewer workspaceId={workspaceId} artifactId={selectedArtifactId} />
          </div>
          <aside className="execution-side-column" aria-label="执行信息">
            <div className="trace-side-card">
              <span className="eyebrow">LEGACY EXECUTION</span>
              <strong>{trace.status}</strong>
              <span>{trace.current_action ?? "暂无当前动作"}</span>
              <span className="mono-label">Worker {trace.worker_id?.slice(0, 8) ?? "未分配"} · Session V{sessionQuery.data?.version ?? "--"}</span>
            </div>
          </aside>
        </div>
      ) : traceQuery.isPending ? <div className="trace-loading" role="status"><RefreshCcw size={16} />读取执行步骤</div> : null}

      {traceOpen ? (
        <div className="trace-dialog-backdrop">
          <div className="trace-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-trace-dialog-title">
            <div className="trace-dialog__bar"><strong id="agent-trace-dialog-title">Agent Trace</strong><button className="icon-button" type="button" aria-label="关闭 Agent Trace" title="关闭 Agent Trace" onClick={() => setTraceOpen(false)}><X size={16} aria-hidden="true" /></button></div>
            <WorkerTracePage embedded />
          </div>
        </div>
      ) : null}
    </section>
  );
}

function statusKind(status: string): "connecting" | "live" | "stale" | "resyncing" | "offline" | "planned" | "running" | "completed" | "failed" {
  if (status === "succeeded" || status === "completed") return "completed";
  if (status === "failed" || status === "canceled") return "failed";
  if (status === "offline") return "offline";
  if (status === "stale") return "stale";
  if (status === "connecting") return "connecting";
  return "running";
}
