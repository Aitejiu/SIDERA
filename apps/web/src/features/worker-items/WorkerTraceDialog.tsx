import { AlertTriangle, RefreshCcw, X } from "lucide-react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { StatusMark } from "@/shared/ui/StatusMark";

import {
  executionQueryKey,
  executionSessionQueryKey,
  getExecution,
  getExecutionSession,
  getWorkerTrace,
  workerTraceQueryKey,
} from "../trace/api";
import { ArtifactViewer } from "../trace/ArtifactViewer";
import { TraceDetails } from "../trace/TraceDetails";
import { TraceLedger } from "../trace/TraceLedger";
import { TraceTiming } from "../trace/TraceTiming";
import type { WorkerTraceEvent } from "../trace/types";
import type { WorkerActivity } from "./types";

type TraceDescriptor = NonNullable<WorkerActivity["trace_descriptor"]>;

export function WorkerTraceDialog({
  workspaceId,
  descriptor,
  onClose,
}: {
  workspaceId: string;
  descriptor: TraceDescriptor;
  onClose: () => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [selectedSequence, setSelectedSequence] = useState<number | null>(null);
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
  const [followTail, setFollowTail] = useState(true);

  const traceQuery = useInfiniteQuery({
    queryKey: workerTraceQueryKey(
      workspaceId,
      descriptor.execution_id,
      descriptor.session_id,
      descriptor.root_task_id,
    ),
    initialPageParam: undefined as number | undefined,
    queryFn: ({ signal, pageParam }) => getWorkerTrace(
      workspaceId,
      descriptor.root_task_id,
      descriptor.execution_id,
      descriptor.session_id,
      {
        ...(pageParam === undefined ? {} : { beforeSequence: pageParam }),
        signal,
      },
    ),
    getNextPageParam: () => undefined,
    getPreviousPageParam: (firstPage) => firstPage.previous_before_sequence ?? undefined,
    enabled: Boolean(
      workspaceId
      && descriptor.root_task_id
      && descriptor.execution_id
      && descriptor.session_id
      && descriptor.session_id !== "unknown",
    ),
  });
  const sessionQuery = useQuery({
    queryKey: executionSessionQueryKey(workspaceId, descriptor.session_id),
    queryFn: ({ signal }) => getExecutionSession(workspaceId, descriptor.session_id, signal),
    enabled: Boolean(workspaceId && descriptor.session_id && descriptor.session_id !== "unknown"),
  });
  const executionQuery = useQuery({
    queryKey: executionQueryKey(workspaceId, descriptor.execution_id),
    queryFn: ({ signal }) => getExecution(workspaceId, descriptor.execution_id, signal),
    enabled: Boolean(workspaceId && descriptor.execution_id),
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

  useEffect(() => {
    const previousActiveElement = document.activeElement;
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (previousActiveElement instanceof HTMLElement) previousActiveElement.focus();
    };
  }, [onClose]);

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
    <div className="trace-dialog-backdrop">
      <div className="trace-dialog worker-trace-dialog" role="dialog" aria-modal="true" aria-labelledby="worker-trace-dialog-title">
        <div className="trace-dialog__bar">
          <div>
            <strong id="worker-trace-dialog-title">Agent Trace</strong>
            <span className="worker-trace-dialog__address">
              <code>{descriptor.execution_id}</code> / <code>{descriptor.session_id}</code>
            </span>
          </div>
          <button
            ref={closeButtonRef}
            className="icon-button"
            type="button"
            aria-label="关闭 Agent Trace"
            title="关闭 Agent Trace"
            onClick={onClose}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <div className="worker-trace-dialog__body">
          {traceQuery.isError ? (
            <div className="trace-provider-warning" role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <span>Agent Trace 暂不可用</span>
              <button className="quiet-button" type="button" onClick={() => void traceQuery.refetch()}>
                <RefreshCcw size={14} aria-hidden="true" />重试
              </button>
            </div>
          ) : null}
          {trace ? (
            <div className="trace-primary-grid">
              <div className="trace-main-column">
                <TraceTiming timing={trace.timing} />
                <TraceLedger
                  title="Agent Trace"
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
              <aside className="trace-side-column" aria-label="Trace 诊断信息">
                <div className="trace-side-card">
                  <span className="eyebrow">EXACT SESSION</span>
                  <StatusMark kind={statusKind(executionQuery.data?.status ?? trace.status)} />
                  <strong>{trace.current_action ?? "暂无当前动作"}</strong>
                  <span>Worker {trace.worker_id?.slice(0, 8) ?? "未分配"}</span>
                  <span>Session V{sessionQuery.data?.version ?? "--"}</span>
                  <span>Plan generation {trace.plan_generation ?? "--"}</span>
                </div>
              </aside>
            </div>
          ) : traceQuery.isPending ? (
            <div className="trace-loading" role="status"><RefreshCcw size={16} />读取 Agent Trace</div>
          ) : null}
        </div>
      </div>
    </div>
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
