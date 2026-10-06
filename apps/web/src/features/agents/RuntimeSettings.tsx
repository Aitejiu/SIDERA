import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Check, Eye, RefreshCw, Save, Search, ServerCog } from "lucide-react";
import { useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { LocalSessionButton } from "@/features/local-session/LocalSessionDialog";
import { ConfigurationNavigationGuard } from "./AgentSettings";
import { AgentApiProblem, discoverRuntimes, listDescriptors, listRuntimes, readRuntime, refreshRuntime, registerRuntime, setRuntimeEnabled, setRuntimeLimits } from "./api";
import type { RuntimeCandidate, RuntimeDescriptor, RuntimeSnapshot } from "./types";

export function RuntimeSettings() {
  const { workspaceId = "", runtimeId } = useParams<{ workspaceId: string; runtimeId: string }>();
  const runtimes = useQuery({ queryKey: ["agent-runtimes", workspaceId], queryFn: () => listRuntimes(workspaceId), enabled: runtimeId === undefined });
  const runtime = useQuery({ queryKey: ["agent-runtime", workspaceId, runtimeId], queryFn: () => readRuntime({ workspace_id: workspaceId, runtime_id: runtimeId ?? "" }), enabled: runtimeId !== undefined });
  const descriptors = useQuery({ queryKey: ["agent-descriptors", workspaceId], queryFn: () => listDescriptors(workspaceId), enabled: runtimeId === undefined });
  return <section className="agent-page" aria-labelledby="runtimes-title">
    <div className="breadcrumb"><Link to={`/workspaces/${workspaceId}/agents`}><ArrowLeft size={13} />智能体</Link>{runtimeId ? <Link to={`/workspaces/${workspaceId}/runtimes`}>运行环境</Link> : null}</div>
    <div className="page-heading agent-page-heading"><h1 id="runtimes-title">运行环境</h1><LocalSessionButton workspaceId={workspaceId} onAuthorized={() => { if (runtimeId) void runtime.refetch(); else { void runtimes.refetch(); void descriptors.refetch(); } }} /></div>
    {runtimeId !== undefined ? runtime.isPending ? <p role="status">正在读取运行环境</p> : runtime.isError ? <p role="alert">运行环境不可用</p> : <RuntimeEditor key={runtimeId} initial={runtime.data} />
      : <>
        {runtimes.isPending ? <p role="status">正在读取运行环境目录</p> : runtimes.isError ? <p role="alert">运行环境目录不可用</p> : runtimes.data.items.length === 0 ? <p>尚无运行环境</p>
          : <div className="agent-runtime-list">{runtimes.data.items.map(item => <article className="agent-runtime-row" key={item.ref.runtime_id}><Link to={`/workspaces/${workspaceId}/runtimes/${item.ref.runtime_id}`}><ServerCog size={16} />{item.harness_id} · {item.actual_version}</Link><RuntimeFacts runtime={item} /></article>)}</div>}
        {descriptors.isPending ? <p role="status">正在读取 Harness 元数据</p> : descriptors.isError ? <p role="alert">Harness 元数据不可用</p> : <RuntimeRegistration workspaceId={workspaceId} descriptors={descriptors.data} onRegistered={() => { void runtimes.refetch(); }} />}
      </>}
  </section>;
}

function RuntimeFacts({ runtime }: { runtime: RuntimeSnapshot }) {
  return <dl className="agent-runtime-facts"><dt>安装</dt><dd>{runtime.installed ? "已安装" : "未安装"}</dd><dt>启用</dt><dd>{runtime.enabled ? "已启用" : "已停用"}</dd>
    <dt>协议</dt><dd>{runtime.protocol_state === "ready" ? "协议已就绪" : runtime.protocol_state === "unavailable" ? "协议不可用" : "协议未确认"}</dd>
    <dt>认证</dt><dd>{runtime.auth_state === "ready" ? "认证已就绪" : runtime.auth_state === "failed" ? "认证失败" : "认证未确认"}</dd>
    <dt>共享容量</dt><dd>活跃 {runtime.limits.max_active ?? "未确认"} · 会话 {runtime.limits.max_sessions ?? "未确认"}</dd>
    <dt>检查时间</dt><dd><time dateTime={runtime.checked_at}>{new Date(runtime.checked_at).toLocaleString()}</time></dd>
  </dl>;
}

function RuntimeRegistration({ workspaceId, descriptors, onRegistered }: { workspaceId: string; descriptors: RuntimeDescriptor[]; onRegistered: () => void }) {
  const [harnessId, setHarnessId] = useState("");
  const [executable, setExecutable] = useState("");
  const [maxActive, setMaxActive] = useState(1);
  const [maxSessions, setMaxSessions] = useState(2);
  const [candidates, setCandidates] = useState<RuntimeCandidate[] | null>(null);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [registered, setRegistered] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const writing = useRef(false);
  const dirty = !registered && (harnessId !== "" || executable !== "" || maxActive !== 1 || maxSessions !== 2);
  function edit(change: () => void) { if (pending || uncertain) return; change(); setRegistered(false); setError(null); }

  async function discover() {
    if (writing.current || uncertain) return;
    writing.current = true; setPending(true); setError(null);
    try { setCandidates(await discoverRuntimes(workspaceId)); }
    catch { setError("运行环境发现不可用"); }
    finally { writing.current = false; setPending(false); }
  }

  async function register() {
    if (writing.current || uncertain || registered) return;
    writing.current = true; setPending(true); setUncertain(true); setError(null);
    try {
      const value = await registerRuntime(workspaceId, { harness_id: harnessId, executable, max_active: maxActive, max_sessions: maxSessions });
      if (value.ref.workspace_id !== workspaceId || value.harness_id !== harnessId) throw new AgentApiProblem(0, "runtime_result_mismatch");
      setUncertain(false); setRegistered(true); onRegistered();
    } catch (cause) {
      const denied = cause instanceof AgentApiProblem && [403, 409, 422].includes(cause.status);
      setUncertain(!denied); setError(denied ? cause.message : "登记结果未确认");
    } finally { writing.current = false; setPending(false); }
  }

  return <section className="agent-runtime-register" aria-labelledby="register-runtime-title"><ConfigurationNavigationGuard dirty={dirty} unsettled={uncertain} />
    <div className="agent-section-heading"><h2 id="register-runtime-title">登记运行环境</h2><button className="quiet-button" disabled={pending || uncertain} onClick={() => void discover()}><Search size={14} />发现运行环境</button></div>
    {candidates !== null ? candidates.length === 0 ? <p>未发现候选运行环境</p> : <ul className="agent-runtime-candidates">{candidates.map(item => <li key={`${item.harness_id}:${item.executable}`}><span>{item.harness_id}</span><code>{item.executable}</code><button className="quiet-button" disabled={pending || uncertain} onClick={() => edit(() => { setHarnessId(item.harness_id); setExecutable(item.executable); })} aria-label={`选择 ${item.executable}`}><Check size={14} />选择</button></li>)}</ul> : null}
    <form onSubmit={event => { event.preventDefault(); void register(); }}><fieldset className="agent-form-fields" disabled={pending || uncertain}>
      <label className="worker-runtime-field"><span>Harness</span><select required value={harnessId} onChange={event => edit(() => setHarnessId(event.target.value))}><option value="">选择 Harness</option>{descriptors.map(item => <option key={item.harness_id} value={item.harness_id}>{item.display_name}</option>)}</select></label>
      <label className="worker-runtime-field"><span>可执行文件绝对路径</span><input required pattern="/.*" value={executable} onChange={event => edit(() => setExecutable(event.target.value))} /></label>
      <label className="worker-runtime-field"><span>活跃 Run 上限</span><input type="number" min={1} required value={maxActive} onChange={event => edit(() => setMaxActive(Number(event.target.value)))} /></label>
      <label className="worker-runtime-field"><span>原生会话上限</span><input type="number" min={1} required value={maxSessions} onChange={event => edit(() => setMaxSessions(Number(event.target.value)))} /></label>
    </fieldset><div className="agent-form-actions"><button className="quiet-button" type="submit" disabled={pending || uncertain || registered}><Save size={14} />登记运行环境</button></div></form>
    {registered ? <p className="agent-success" role="status">登记结果已返回</p> : null}{error ? <p role="alert">{error}</p> : null}
    {uncertain ? <button className="quiet-button" disabled={pending} onClick={() => onRegistered()}><RefreshCw size={14} />读取当前目录</button> : null}
  </section>;
}

function RuntimeEditor({ initial }: { initial: RuntimeSnapshot }) {
  const [saved, setSaved] = useState(initial);
  const [maxActive, setMaxActive] = useState<number | "">(initial.limits.max_active ?? "");
  const [maxSessions, setMaxSessions] = useState<number | "">(initial.limits.max_sessions ?? "");
  const [observed, setObserved] = useState<RuntimeSnapshot | null>(null);
  const [pending, setPending] = useState(false);
  const [unsettled, setUnsettled] = useState(false);
  const [conflicted, setConflicted] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const writing = useRef(false);
  const dirty = maxActive !== saved.limits.max_active || maxSessions !== saved.limits.max_sessions;

  function exact(value: RuntimeSnapshot) {
    if (value.ref.workspace_id !== initial.ref.workspace_id || value.ref.runtime_id !== initial.ref.runtime_id) throw new AgentApiProblem(0, "runtime_result_mismatch");
    return value;
  }

  async function mutate(kind: "limits" | "status" | "refresh") {
    if (writing.current || unsettled || conflicted || kind !== "limits" && dirty) return;
    if (kind === "limits" && (maxActive === "" || maxSessions === "" || !dirty)) return;
    writing.current = true; setPending(true); setUnsettled(true); setMessage(null); setObserved(null);
    try {
      const value = exact(kind === "limits" && maxActive !== "" && maxSessions !== "" ? await setRuntimeLimits(saved.ref, maxActive, maxSessions, saved.version)
        : kind === "status" ? await setRuntimeEnabled(saved.ref, !saved.enabled, saved.version) : await refreshRuntime(saved.ref));
      setSaved(value); setMaxActive(value.limits.max_active ?? ""); setMaxSessions(value.limits.max_sessions ?? "");
      setUnsettled(false); setConflicted(false);
      setMessage(kind === "limits" ? "容量已保存" : kind === "status" ? "状态已保存" : "运行环境已刷新");
    } catch (cause) {
      const conflict = cause instanceof AgentApiProblem && (cause.code === "revision_conflict" || cause.code === "version_conflict");
      const denied = cause instanceof AgentApiProblem && [403, 404, 409, 422].includes(cause.status);
      setConflicted(conflict); setUnsettled(!denied); setMessage(conflict ? "配置版本已变化" : denied ? cause.message : "操作结果未确认");
    } finally { writing.current = false; setPending(false); }
  }

  async function observe() {
    if (writing.current) return;
    writing.current = true; setPending(true);
    try { setObserved(exact(await readRuntime(initial.ref))); }
    catch { setMessage(unsettled ? "操作结果未确认" : "当前状态未确认"); }
    finally { writing.current = false; setPending(false); }
  }

  return <section className="agent-runtime-detail"><ConfigurationNavigationGuard dirty={dirty} unsettled={unsettled} />
    <div className="agent-section-heading"><div><h2>{saved.harness_id} · {saved.actual_version}</h2><span className="mono-label">{saved.ref.runtime_id} · V{saved.version}</span></div><button className="quiet-button" disabled={pending || unsettled || conflicted || dirty} onClick={() => void mutate("refresh")}><RefreshCw size={14} />检查运行环境</button></div>
    <p className="mono-label agent-runtime-path">{saved.executable}</p><RuntimeFacts runtime={saved} />
    <form onSubmit={event => { event.preventDefault(); void mutate("limits"); }}><fieldset className="agent-form-fields" disabled={pending || unsettled}>
      <label className="worker-runtime-field"><span>活跃 Run 上限</span><input type="number" required min={1} value={maxActive} onChange={event => setMaxActive(event.target.value === "" ? "" : Number(event.target.value))} /></label>
      <label className="worker-runtime-field"><span>原生会话上限</span><input type="number" required min={1} value={maxSessions} onChange={event => setMaxSessions(event.target.value === "" ? "" : Number(event.target.value))} /></label>
    </fieldset><div className="agent-form-actions"><button type="submit" className="quiet-button" disabled={pending || unsettled || conflicted || !dirty}><Save size={14} />保存容量</button><button type="button" className="quiet-button" disabled={pending || unsettled || conflicted || dirty} onClick={() => void mutate("status")}>{saved.enabled ? "停用运行环境" : "启用运行环境"}</button></div></form>
    {message ? <p role={conflicted || unsettled ? "alert" : "status"}>{message}</p> : null}
    <button className="quiet-button" disabled={pending} onClick={() => void observe()}><Eye size={14} />读取当前状态</button>
    {observed ? <section className="agent-runtime-observation"><h3>当前已读取 · V{observed.version}</h3><RuntimeFacts runtime={observed} />{unsettled || conflicted ? <button className="quiet-button" disabled={pending} onClick={() => { setSaved(observed); setObserved(null); setUnsettled(false); setConflicted(false); setMessage(null); }}>以读取版本重新确认</button> : null}</section> : null}
    <h3>能力支持</h3><dl className="agent-facts">{Object.entries(saved.capabilities).map(([name, support]) => <div className="agent-fact-row" key={name}><dt>{name}</dt><dd>{support.status} · {support.reason_code}</dd></div>)}</dl>
  </section>;
}
