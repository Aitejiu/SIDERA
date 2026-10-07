import { AlertTriangle, Clock3, KeyRound, RefreshCcw } from "lucide-react";
import { useState } from "react";

import { NativeTraceReader } from "./NativeTraceReader";
import { NativeTraceUnavailableError, type NativeTracePin } from "./nativeTrace";
import { useNativeTrace } from "./useNativeTrace";

export function NativeTraceView({ pin, executionStatus }: {
  pin: NativeTracePin;
  executionStatus: string | undefined;
}) {
  const trace = useNativeTrace({ pin });
  const [selectedSequence, setSelectedSequence] = useState<number | null>(null);
  const [followTail, setFollowTail] = useState(true);
  const selected = followTail ? trace.events.at(-1)?.sequence ?? null : selectedSequence;
  return <>
    <div className="trace-summary">
      <div className="trace-summary__line">
        <span className="trace-summary__status">Execution 状态：{executionStatus ?? "未提供"}</span>
        <span>Profile R{pin.profileRef.revision}</span>
        <span className="trace-summary__metric"><Clock3 size={14} aria-hidden="true" />耗时未提供</span>
        <span className="trace-summary__usage">用量未提供</span>
        <details className="trace-session-details"><summary>原始执行</summary>
          <dl className="trace-facts">
            <div><dt>Profile</dt><dd><code>{pin.profileRef.profile_id}</code> · R{pin.profileRef.revision}</dd></div>
            <div><dt>Run</dt><dd><code>{pin.runId}</code></dd></div>
            <div><dt>Execution</dt><dd><code>{pin.executionId}</code></dd></div>
            <div><dt>WorkerItem</dt><dd><code>{pin.workerItemId}</code></dd></div>
          </dl>
        </details>
      </div>
    </div>
    <NativeTraceReadState loading={trace.loading} error={trace.error} onRetry={() => void trace.retry()} />
    {trace.loaded ? <NativeTraceReader events={trace.events} selectedSequence={selected}
      onSelect={(event) => { setSelectedSequence(event.sequence); setFollowTail(false); }}
      onLoadNext={() => void trace.readNext()} hasNext={trace.hasNext} isLoadingNext={trace.isLoadingNext}
      followTail={followTail} onFollowTail={() => { setFollowTail(true); setSelectedSequence(trace.events.at(-1)?.sequence ?? null); }}
      onPauseFollow={() => { setSelectedSequence(selected); setFollowTail(false); }} /> : null}
  </>;
}

export function NativeTraceReadState({ loading, error, onRetry, onAuthorize }: {
  loading: boolean;
  error: NativeTraceUnavailableError | null;
  onRetry: () => void;
  onAuthorize?: () => void;
}) {
  if (error) return <div className="trace-provider-warning" role="alert">
    <AlertTriangle size={17} aria-hidden="true" />
    <span>{error.kind === "denied" ? "无权读取 Agent Trace"
      : error.kind === "unsupported" ? "此执行不支持 Agent Trace" : "Agent Trace 暂不可用"}</span>
    <button className="quiet-button" type="button" onClick={onRetry}><RefreshCcw size={14} aria-hidden="true" />重试</button>
    {onAuthorize ? <button className="quiet-button" type="button" onClick={onAuthorize}>
      <KeyRound size={14} aria-hidden="true" />授权本地会话
    </button> : null}
  </div>;
  return loading ? <div className="trace-loading" role="status"><RefreshCcw size={16} aria-hidden="true" />读取 Agent Trace</div> : null;
}
