import { useQuery } from "@tanstack/react-query";
import { Play, RefreshCw, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { createThread, getThread, getTurn, startTurn, stopThread } from "@/features/chat/api";
import type { MainAgentThread, MainAgentTurn, ThreadStop } from "@/features/chat/types";
import { ConfigurationNavigationGuard } from "./AgentSettings";
import { validateProfile } from "./api";
import { sameRef, type ProfileView, type RuntimeSnapshot } from "./types";

export function ProfileTestDialog({ profile: currentProfile, runtime, onClose }: { profile: ProfileView; runtime: RuntimeSnapshot | undefined; onClose: () => void }) {
  const [profile] = useState(currentProfile);
  const ref = profile.profile.ref;
  const validation = useQuery({ queryKey: ["agent-validation", ref], queryFn: () => validateProfile(ref) });
  const [text, setText] = useState(""), [thread, setThread] = useState<MainAgentThread | null>(null), [turn, setTurn] = useState<MainAgentTurn | null>(null);
  const [pending, setPending] = useState(false), [unsettled, setUnsettled] = useState(false), [creationUnknown, setCreationUnknown] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [stop, setStop] = useState<ThreadStop | null>(null), [error, setError] = useState<string | null>(null);
  const intent = useRef<{ operationId: string; text: string } | null>(null), submitting = useRef(false), dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.current?.querySelector<HTMLElement>("button, textarea")?.focus();
    return () => { previous?.focus(); };
  }, []);
  function validThread(value: MainAgentThread): boolean {
    return typeof value.id === "string" && value.id.length > 0 && value.profileRef !== undefined && sameRef(value.profileRef, ref);
  }
  function validTurn(value: MainAgentTurn, original: MainAgentThread): boolean {
    return value.threadId === original.id && value.operationId === intent.current?.operationId
      && value.profileRef !== undefined && sameRef(value.profileRef, ref)
      && typeof value.id === "string" && value.id.length > 0;
  }
  async function start() {
    if (submitting.current || intent.current !== null || !text.trim()) return;
    const original = { operationId: crypto.randomUUID(), text };
    intent.current = original;
    setAttempted(true);
    submitting.current = true; setPending(true); setUnsettled(true); setError(null);
    let created: MainAgentThread | null = null;
    try {
      const candidate = await createThread({ contextRefs: [], title: `Profile test · R${ref.revision}`, profileRef: ref });
      if (!validThread(candidate) || !Array.isArray(candidate.turns) || candidate.turns.length !== 0 || candidate.runId != null) throw new Error("original_thread_mismatch");
      created = candidate;
      setThread(created);
    } catch { setCreationUnknown(true); setError("测试会话创建结果未确认"); }
    if (created !== null) {
      try {
        const next = await startTurn(created.id, original.text, original.operationId);
        if (!validTurn(next, created)) throw new Error("original_turn_mismatch");
        setTurn(next);
      } catch { setError("测试结果未确认"); }
    }
    submitting.current = false; setPending(false);
  }
  async function observe() {
    if (submitting.current || thread === null || stop?.cleanup_confirmed) return;
    submitting.current = true; setPending(true);
    try {
      let next: MainAgentTurn;
      if (turn !== null) {
        next = await getTurn(thread.id, turn.id);
        if (next.id !== turn.id) throw new Error("original_turn_mismatch");
      } else {
        const current = await getThread(thread.id);
        if (!validThread(current) || current.id !== thread.id) throw new Error("original_thread_mismatch");
        const matches = current.turns.filter(item => item.operationId === intent.current?.operationId);
        if (matches.length !== 1 || matches[0] === undefined) throw new Error("original_turn_unavailable");
        next = matches[0];
      }
      if (!validTurn(next, thread)) throw new Error("original_turn_mismatch");
      setTurn(next); setError(null);
    } catch { setError("原测试结果不可用"); }
    finally { submitting.current = false; setPending(false); }
  }
  async function stopOriginal(closeAfter = false) {
    if (submitting.current || thread === null) return;
    submitting.current = true; setPending(true); setUnsettled(true);
    try {
      const result = await stopThread(thread.id);
      if (result.conversation_id !== thread.id || typeof result.cleanup_confirmed !== "boolean"
        || (result.cleanup_confirmed && result.stop_state !== "stopped")) throw new Error("original_stop_mismatch");
      setStop(result);
      if (result.cleanup_confirmed) {
        setUnsettled(false); setError(null);
        if (closeAfter) onClose();
      } else setError("原停止结果未确认");
    } catch { setError("原停止结果未确认"); }
    finally { submitting.current = false; setPending(false); }
  }
  function close() {
    if (submitting.current || creationUnknown) return;
    if (thread === null || stop?.cleanup_confirmed) onClose();
    else void stopOriginal(true);
  }
  return <><ConfigurationNavigationGuard dirty={text.length > 0 && !attempted} unsettled={unsettled} />{createPortal(<div className="worker-runtime-dialog-backdrop"><div className="worker-runtime-dialog agent-profile-test" role="dialog" aria-modal="true" aria-label="测试 Profile" ref={dialog} onKeyDown={event => {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key !== "Tab") return;
    const fields = Array.from(dialog.current?.querySelectorAll<HTMLElement>("textarea:not([disabled]), button:not([disabled])") ?? []);
    const first = fields[0], last = fields.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}>
    <div className="worker-runtime-dialog__heading"><h2>测试 Profile</h2><button className="icon-button" title="关闭测试" aria-label="关闭测试" disabled={pending || creationUnknown} onClick={close}><X size={16} /></button></div>
    <p>{profile.profile.draft.name} · <strong>Profile R{ref.revision}</strong></p>
    <p>{validation.isPending ? "正在读取结构验证" : validation.isError ? "结构验证不可用" : validation.data.valid ? "结构验证通过" : "结构验证存在问题"}</p>
    {validation.data?.issues.map((issue, index) => <p key={index}>{issue.field} · {issue.code}</p>)}
    <p>运行环境元数据 · {runtime ? `${runtime.harness_id} · ${runtime.actual_version}` : "不可用"}</p>
    <dl className="agent-facts"><dt>配置模型</dt><dd>{profile.profile.draft.model_id ?? "继承运行环境默认值"}</dd><dt>回复模型</dt><dd>模型未观察到</dd></dl>
    <form onSubmit={event => { event.preventDefault(); void start(); }}><label className="worker-runtime-field">测试文本<textarea aria-label="测试文本" value={text} disabled={pending || attempted} maxLength={100000} onChange={event => setText(event.target.value)} /></label><button className="quiet-button" type="submit" disabled={pending || attempted || !text.trim()}><Play size={14} />开始测试</button></form>
    {thread ? <p className="mono-label">会话 {thread.id}</p> : null}
    {turn ? <><p>测试状态 · {turn.status}</p>{turn.finalResponse ? <p className="agent-test-reply">{turn.finalResponse}</p> : null}</> : null}
    {error ? <p role="alert">{error}</p> : null}
    {thread && !stop?.cleanup_confirmed ? <div className="agent-form-actions"><button className="quiet-button" disabled={pending} onClick={() => void observe()}><RefreshCw size={14} />读取原测试结果</button><button className="quiet-button" disabled={pending} onClick={() => void stopOriginal()}><Square size={14} />{stop !== null || error === "原停止结果未确认" ? "查询原停止结果" : "停止本次测试"}</button></div> : null}
    {stop?.cleanup_confirmed ? <p role="status">本次测试清理已确认</p> : null}
  </div></div>, document.body)}</>;
}
