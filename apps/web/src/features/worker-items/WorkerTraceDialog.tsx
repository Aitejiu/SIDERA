import { AlertTriangle, RefreshCcw, X } from "lucide-react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { LocalSessionDialog } from "@/features/local-session/LocalSessionDialog";
import {
  getWorkerTrace,
  workerTraceQueryKey,
} from "../trace/api";
import { TraceReader } from "../trace/TraceReader";
import { TraceSummary } from "../trace/TraceSummary";
import { NativeTraceReadState, NativeTraceView } from "../trace/NativeTraceView";
import { nativeTracePinKey, type TraceSelectionAddress } from "../trace/nativeTrace";
import { useWorkerTraceSelection } from "../trace/useWorkerTraceSelection";
import type { Execution, ExecutionSession, WorkerTraceEvent } from "../trace/types";
import type { WorkerActivity } from "./types";

type TraceDescriptor = NonNullable<WorkerActivity["trace_descriptor"]>;

type Props = {
  workspaceId: string;
  onClose: () => void;
} & ({ descriptor: TraceDescriptor; selection?: never } | { descriptor?: never; selection: TraceSelectionAddress });

export function WorkerTraceDialog(props: Props) {
  const { workspaceId, onClose } = props;
  const selectedAddress = props.descriptor === undefined ? props.selection : {
    rootTaskId: props.descriptor.root_task_id,
    executionId: props.descriptor.execution_id,
    sessionId: props.descriptor.session_id,
  };
  const address = { ...selectedAddress,
    sessionId: selectedAddress.sessionId && selectedAddress.sessionId !== "unknown" ? selectedAddress.sessionId : null };
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

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

  return <dialog ref={dialogRef} className="trace-dialog worker-trace-dialog" aria-modal="true" aria-labelledby="worker-trace-dialog-title"
    onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <div className="trace-dialog__bar">
      <div><strong id="worker-trace-dialog-title">Agent Trace</strong></div>
      <button ref={closeButtonRef} className="icon-button" type="button" aria-label="关闭 Agent Trace" title="关闭 Agent Trace" onClick={onClose}>
        <X size={16} aria-hidden="true" />
      </button>
    </div>
    <div className="worker-trace-dialog__body">
      <SelectedTraceDialog key={JSON.stringify([workspaceId, address])} workspaceId={workspaceId} address={address} />
    </div>
  </dialog>;
}

function SelectedTraceDialog({ workspaceId, address }: { workspaceId: string; address: TraceSelectionAddress }) {
  const selected = useWorkerTraceSelection({ workspaceId, address });
  const [authorizationOpen, setAuthorizationOpen] = useState(false);
  const selection = selected.selection;
  return <>
    <NativeTraceReadState loading={selected.loading} error={selected.error} onRetry={() => void selected.retry()}
      {...(selected.authorizationRequired ? { onAuthorize: () => setAuthorizationOpen(true) } : {})} />
    {selection?.mode === "native" ? <NativeTraceView key={`${selected.scopeKey}:${nativeTracePinKey(selection.pin)}`}
      pin={selection.pin} executionStatus={selected.execution?.status} />
      : selection?.mode === "legacy" && selected.execution ? <LegacyTraceDialog key={`${selected.scopeKey}:legacy`}
        workspaceId={workspaceId} address={address} execution={selected.execution} session={selection.session}
        principalId={selected.localSession?.principal_id ?? ""} /> : null}
    {authorizationOpen ? <LocalSessionDialog workspaceId={workspaceId} onClose={() => setAuthorizationOpen(false)}
      onAuthorized={() => { setAuthorizationOpen(false); void selected.retry(); }} /> : null}
  </>;
}

function LegacyTraceDialog({ workspaceId, address, execution, session, principalId }: {
  workspaceId: string;
  address: TraceSelectionAddress;
  execution: Execution;
  session: ExecutionSession;
  principalId: string;
}) {
  const [selectedSequence, setSelectedSequence] = useState<number | null>(null);
  const [followTail, setFollowTail] = useState(true);

  const traceQuery = useInfiniteQuery({
    queryKey: [...workerTraceQueryKey(
      workspaceId,
      address.executionId,
      session.id,
      address.rootTaskId,
    ), principalId, "legacy"],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ signal, pageParam }) => getWorkerTrace(
      workspaceId,
      address.rootTaskId,
      address.executionId,
      session.id,
      {
        ...(pageParam === undefined ? {} : { beforeSequence: pageParam }),
        signal,
      },
    ),
    getNextPageParam: () => undefined,
    getPreviousPageParam: (firstPage) => firstPage.previous_before_sequence ?? undefined,
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
    <>
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
              <TraceSummary trace={trace} executionStatus={execution.status} sessionVersion={session.version} rootTaskId={address.rootTaskId} />
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
    </>
  );
}
