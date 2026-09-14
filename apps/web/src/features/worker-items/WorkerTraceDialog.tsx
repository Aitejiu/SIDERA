import { AlertTriangle, RefreshCcw, X } from "lucide-react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  executionQueryKey,
  executionSessionQueryKey,
  getExecution,
  getExecutionSession,
  getWorkerTrace,
  workerTraceQueryKey,
} from "../trace/api";
import { TraceReader } from "../trace/TraceReader";
import { TraceSummary } from "../trace/TraceSummary";
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
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [selectedSequence, setSelectedSequence] = useState<number | null>(null);
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
    const latestPage = pages.at(-1);
    if (latestPage === undefined) return undefined;
    return { ...latestPage, events: pages.flatMap((page) => page.events) };
  }, [traceQuery.data?.pages]);
  const selectedEvent = useMemo(() => {
    if (trace === undefined || trace.events.length === 0) return null;
    return (followTail ? trace.events.at(-1) : trace.events.find((event) => event.sequence === selectedSequence)) ?? trace.events.at(-1) ?? null;
  }, [followTail, selectedSequence, trace]);

  useEffect(() => {
    const previousActiveElement = document.activeElement;
    const dialog = dialogRef.current;
    if (dialog === null) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    closeButtonRef.current?.focus();
    return () => {
      dialog.close?.();
      if (previousActiveElement instanceof HTMLElement) previousActiveElement.focus();
    };
  }, []);

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
    <dialog ref={dialogRef} className="trace-dialog worker-trace-dialog" aria-modal="true" aria-labelledby="worker-trace-dialog-title" onCancel={(event) => { event.preventDefault(); onClose(); }}>
        <div className="trace-dialog__bar">
          <div>
            <strong id="worker-trace-dialog-title">Agent Trace</strong>
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
            <>
              <TraceSummary trace={trace} executionStatus={executionQuery.data?.status} sessionVersion={sessionQuery.data?.version} rootTaskId={descriptor.root_task_id} />
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
            <div className="trace-loading" role="status"><RefreshCcw size={16} />读取 Agent Trace</div>
          ) : null}
        </div>
    </dialog>
  );
}
