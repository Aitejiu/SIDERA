import { AlertTriangle, ArrowLeft, CircleDot, ExternalLink, RefreshCcw } from "lucide-react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  executionQueryKey,
  executionSessionQueryKey,
  getExecution,
  getExecutionSession,
  getWorkspaceCapabilities,
  getWorkerTrace,
  workerTraceQueryKey,
} from "./api";
import { TraceReader } from "./TraceReader";
import { TraceSummary } from "./TraceSummary";
import type { WorkerTraceEvent } from "./types";

export function WorkerTracePage({ embedded = false }: { embedded?: boolean } = {}) {
  const {
    workspaceId = "",
    rootTaskId = "",
    executionId = "",
    sessionId = "",
  } = useParams<{ workspaceId: string; rootTaskId: string; executionId: string; sessionId: string }>();
  const [selectedSequence, setSelectedSequence] = useState<number | null>(null);
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
  const trace = useMemo(() => {
    const pages = traceQuery.data?.pages ?? [];
    const latestPage = pages.at(-1);
    if (latestPage === undefined) return undefined;
    return { ...latestPage, events: pages.flatMap((page) => page.events) };
  }, [traceQuery.data?.pages]);
  const selectedEvent = useMemo(() => {
    if (trace === undefined || trace.events.length === 0) return null;
    return (followTail ? trace.events.at(-1) : trace.events.find((event) => event.sequence === selectedSequence)) ?? trace.events.at(-1) ?? null;
  }, [followTail, selectedSequence, trace]);
  const escalationRequested = selectedEvent !== null && `${selectedEvent.title} ${selectedEvent.summary}`.includes("scope_escalation_requested");
  const capabilitiesQuery = useQuery({
    queryKey: ["workspace-capabilities", workspaceId],
    queryFn: ({ signal }) => getWorkspaceCapabilities(workspaceId, signal),
    enabled: escalationRequested,
  });
  const mainAgent = capabilitiesQuery.data?.main_agent;
  const mainAgentEntry = isRecord(mainAgent) && typeof mainAgent.entry_uri === "string" ? mainAgent.entry_uri : null;

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
    <section className={`worker-trace-page${embedded ? " worker-trace-page--embedded" : ""}`} aria-label="Worker Trace 页面">
      {!embedded ? <div className="breadcrumb trace-breadcrumb">
        <Link to={`/workspaces/${workspaceId}/tasks`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span>/</span><Link to={`/workspaces/${workspaceId}/tasks/${rootTaskId}`}>Root Task</Link>
        <span>/</span><span>WorkItem</span><span>/</span><span>Execution</span><span>/</span><span>Session</span>
      </div> : null}

      <header className="trace-page-heading">
        <div>
          <h1 id="worker-trace-title">Worker 轨迹</h1>
        </div>
        <Link className="quiet-button" to={`/workspaces/${workspaceId}/tasks/${rootTaskId}/executions/${executionId}/sessions/${sessionId}`}>返回执行步骤</Link>
      </header>

      {traceQuery.isError ? (
        <div className="trace-provider-warning" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          <div><strong>Trace Provider 暂不可用</strong><span>当前 Task 状态仍来自 SIDERA Execution：{executionQuery.data?.status ?? "读取中"}。已知 trace 范围暂不可用。</span></div>
          <button className="quiet-button" type="button" onClick={() => void traceQuery.refetch()}><RefreshCcw size={14} aria-hidden="true" />重试</button>
        </div>
      ) : null}

      {trace ? (
        <>
          <TraceSummary trace={trace} executionStatus={executionQuery.data?.status} sessionVersion={sessionQuery.data?.version} rootTaskId={rootTaskId} />
          {escalationRequested ? (
            <div className="scope-escalation" role="status">
              <CircleDot size={16} aria-hidden="true" />
              <span>这个 scope 需要整体调整，请回到 Main Agent 处理。</span>
              {mainAgentEntry ? <a href={mainAgentEntry} target="_blank" rel="noreferrer">与 Main Agent 对话<ExternalLink size={13} aria-hidden="true" /></a> : <span className="muted-copy">Main Agent 未配置入口</span>}
            </div>
          ) : null}
          <TraceReader
            workspaceId={workspaceId}
            events={trace.events}
            selectedSequence={selectedEvent?.sequence ?? null}
            onSelect={selectEvent}
            onLoadOlder={() => void traceQuery.fetchPreviousPage()}
            hasOlder={traceQuery.hasPreviousPage === true}
            isLoadingOlder={traceQuery.isFetchingPreviousPage}
            followTail={followTail}
            onFollowTail={followLatest}
            onPauseFollow={() => { setSelectedSequence(selectedEvent?.sequence ?? null); setFollowTail(false); }}
          />
        </>
      ) : traceQuery.isPending ? (
        <div className="trace-loading" role="status"><RefreshCcw size={16} />读取 Worker trace</div>
      ) : null}
    </section>
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
