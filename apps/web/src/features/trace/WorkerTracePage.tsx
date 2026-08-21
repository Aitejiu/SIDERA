import { AlertTriangle, ArrowLeft, CircleDot, ExternalLink, RefreshCcw } from "lucide-react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { StatusMark } from "@/shared/ui/StatusMark";

import {
  executionInputsQueryKey,
  executionQueryKey,
  executionSessionQueryKey,
  getExecution,
  getExecutionSession,
  getWorkspaceCapabilities,
  getWorkerTrace,
  listExecutionInputs,
  workerTraceQueryKey,
} from "./api";
import { ArtifactViewer } from "./ArtifactViewer";
import { InterventionComposer } from "./InterventionComposer";
import { TraceDetails } from "./TraceDetails";
import { TraceLedger } from "./TraceLedger";
import { TraceTiming } from "./TraceTiming";
import type { ExecutionContextAddress, WorkerTraceEvent } from "./types";

export function WorkerTracePage() {
  const {
    workspaceId = "",
    rootTaskId = "",
    executionId = "",
    sessionId = "",
  } = useParams<{ workspaceId: string; rootTaskId: string; executionId: string; sessionId: string }>();
  const [selectedSequence, setSelectedSequence] = useState<number | null>(null);
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
  const [followTail, setFollowTail] = useState(true);

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
  const inputsQuery = useQuery({
    queryKey: executionInputsQueryKey(workspaceId, sessionId),
    queryFn: ({ signal }) => listExecutionInputs(workspaceId, sessionId, signal),
    enabled: Boolean(workspaceId && sessionId && sessionId !== "unknown"),
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
  const context = useMemo<ExecutionContextAddress | null>(() => {
    const branch = trace?.context_branch;
    if (trace === undefined || branch === null || branch === undefined || trace.worker_id === null) return null;
    return {
      plan_snapshot_id: branch.plan_snapshot_id,
      root_task_id: branch.root_task_id,
      work_item_key: branch.work_item_key,
      worker_slot_key: branch.worker_slot_key,
      worker_id: trace.worker_id,
      execution_id: branch.current_execution_id,
      session_id: branch.current_session_id,
    };
  }, [trace]);
  const escalationRequested = selectedEvent !== null && `${selectedEvent.title} ${selectedEvent.summary}`.includes("scope_escalation_requested");
  const capabilitiesQuery = useQuery({
    queryKey: ["workspace-capabilities", workspaceId],
    queryFn: ({ signal }) => getWorkspaceCapabilities(workspaceId, signal),
    enabled: escalationRequested,
  });
  const mainAgent = capabilitiesQuery.data?.main_agent;
  const mainAgentEntry = isRecord(mainAgent) && typeof mainAgent.entry_uri === "string" ? mainAgent.entry_uri : null;

  const refreshAll = async () => {
    await Promise.all([
      traceQuery.refetch(),
      sessionQuery.refetch(),
      executionQuery.refetch(),
      inputsQuery.refetch(),
    ]);
  };

  const selectEvent = (event: WorkerTraceEvent) => {
    setSelectedSequence(event.sequence);
    if (trace !== undefined && event.sequence !== trace.events.at(-1)?.sequence) {
      setFollowTail(false);
    }
  };

  const followLatest = () => {
    setFollowTail(true);
    setSelectedSequence(trace?.events.at(-1)?.sequence ?? null);
  };

  return (
    <section className="worker-trace-page" aria-label="Worker Trace 页面">
      <div className="breadcrumb trace-breadcrumb">
        <Link to={`/workspaces/${workspaceId}/tasks`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span>/</span><Link to={`/workspaces/${workspaceId}/tasks/${rootTaskId}`}>Root Task</Link>
        <span>/</span><span>WorkItem</span><span>/</span><span>Execution</span><span>/</span><span>Session</span>
      </div>

      <header className="trace-page-heading">
        <div>
          <span className="eyebrow">WORKER TRACE · EXACT SESSION</span>
          <h1 id="worker-trace-title">Worker 轨迹</h1>
          <p className="trace-address-line"><code>{rootTaskId}</code> / <code>{executionId}</code> / <code>{sessionId}</code></p>
        </div>
        <div className="trace-page-heading__state">
          <StatusMark kind={statusKind(executionQuery.data?.status ?? trace?.status ?? "connecting")} />
          <span className="mono-label">{trace?.freshness_at ? `新鲜度 ${formatTime(trace.freshness_at)}` : "等待 trace"}</span>
        </div>
      </header>

      {executionQuery.data ? (
        <div className="trace-execution-strip">
          <span><small>Worker</small><strong>{trace?.worker_id?.slice(0, 8) ?? "未分配"}</strong></span>
          <span><small>当前动作</small><strong>{trace?.current_action ?? "暂无"}</strong></span>
          <span><small>Plan generation</small><strong>{trace?.plan_generation ?? "--"}</strong></span>
          <span><small>Checkpoint</small><strong>{trace?.checkpoint_sequence ?? "--"}</strong></span>
          <span><small>Session version</small><strong>{sessionQuery.data?.version ?? "--"}</strong></span>
        </div>
      ) : null}

      {traceQuery.isError ? (
        <div className="trace-provider-warning" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          <div><strong>Trace Provider 暂不可用</strong><span>当前 Task 状态仍来自 SIDERA Execution：{executionQuery.data?.status ?? "读取中"}。已知 trace 范围暂不可用。</span></div>
          <button className="quiet-button" type="button" onClick={() => void traceQuery.refetch()}><RefreshCcw size={14} aria-hidden="true" />重试</button>
        </div>
      ) : null}

      {trace ? (
        <>
          <div className="trace-primary-grid">
            <div className="trace-main-column">
              <TraceTiming timing={trace.timing} />
              {escalationRequested ? (
                <div className="scope-escalation" role="status">
                  <CircleDot size={16} aria-hidden="true" />
                  <span>这个 scope 需要整体调整，请回到 Main Agent 处理。</span>
                  {mainAgentEntry ? <a href={mainAgentEntry} target="_blank" rel="noreferrer">与 Main Agent 对话<ExternalLink size={13} aria-hidden="true" /></a> : <span className="muted-copy">Main Agent 未配置入口</span>}
                </div>
              ) : null}
              <TraceLedger
                events={trace.events}
                selectedSequence={selectedEvent?.sequence ?? null}
                onSelect={selectEvent}
                onLoadOlder={() => void traceQuery.fetchPreviousPage()}
                hasOlder={traceQuery.hasPreviousPage === true}
                isLoadingOlder={traceQuery.isFetchingPreviousPage}
                followTail={followTail}
                onFollowTail={followLatest}
                onPauseFollow={() => setFollowTail(false)}
              />
              <TraceDetails event={selectedEvent} onArtifactSelect={setSelectedArtifactId} />
              <ArtifactViewer workspaceId={workspaceId} artifactId={selectedArtifactId} />
            </div>
            <aside className="trace-side-column" aria-label="Worker 介入面板">
              <div className="trace-side-card">
                <span className="eyebrow">WORKER STATUS</span>
                <strong>{trace.status}</strong>
                <span>{trace.current_action ?? "暂无当前动作"}</span>
              </div>
              <InterventionComposer
                workspaceId={workspaceId}
                rootTaskId={rootTaskId}
                context={context}
                contextBranch={trace.context_branch}
                session={sessionQuery.data}
                execution={executionQuery.data}
                inputs={inputsQuery.data ?? []}
                traceFresh={traceQuery.isSuccess}
                onRefresh={refreshAll}
              />
            </aside>
          </div>
        </>
      ) : traceQuery.isPending ? (
        <div className="trace-loading" role="status"><RefreshCcw size={16} />读取 Worker trace</div>
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

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString("zh-CN");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
