import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Eye, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { useWorkspaceTabs } from "@/app/WorkspaceTabs";
import { loadTabs, reduceTabs, registerDirectRoute, saveTabs, type ConfigurationRecovery } from "@/app/workspaceTabsState";
import { ConfigurationNavigationGuard, EnvironmentFields } from "./AgentSettings";
import { CredentialPicker } from "./CredentialPicker";
import { AgentApiProblem, listConnections, listProductToolConnections, readConfigurationRequest, readResource, saveResource } from "./api";
import { confirmed, sameRef, type ConfigurationResult, type ResourceDraft, type ResourceRef, type ResourceView } from "./types";

type Mode = "inline" | "source" | "connection" | "stdio" | "http";
type Intent = { operationId: string; result: ConfigurationResult | null };

export function ResourceEditor({ workspaceId, resource, kind, onSaved, onCancel }: {
  workspaceId: string; resource: ResourceView | null; kind: ResourceDraft["kind"];
  onSaved: (value: ResourceView) => void; onCancel?: () => void;
}) {
  const tabs = useWorkspaceTabs();
  const tabId = tabs.state.activeTabId;
  const [recovery] = useState(() => tabs.state.tabs.find(tab => tab.id === tabId)?.configurationRecovery);
  const [draft, setDraft] = useState<ResourceDraft>(() => resource?.resource.draft ?? { name: "", kind, settings: emptySettings(defaultMode(kind)), secret_refs: [] });
  const [baseline, setBaseline] = useState(draft);
  const [expected, setExpected] = useState<ResourceRef | undefined>(resource?.resource.ref);
  const [mode, setMode] = useState<Mode>(() => resource ? resourceMode(resource.resource.draft) : defaultMode(kind));
  const [intent, setIntent] = useState<Intent | null>(() => recovery ? { operationId: recovery.operationId, result: null } : null);
  const [pending, setPending] = useState(recovery !== undefined);
  const [error, setError] = useState<string | null>(null);
  const [head, setHead] = useState<ResourceView | null>(null);
  const [credentialGuard, setCredentialGuard] = useState({ dirty: false, unsettled: false });
  const mounted = useRef(false), submitting = useRef(false), restored = useRef(false);
  const connections = useQuery({ queryKey: ["agent-connections", workspaceId], queryFn: () => listConnections(workspaceId), enabled: mode === "connection" });
  const toolConnections = useQuery({ queryKey: ["product-tool-connections", workspaceId], queryFn: () => listProductToolConnections(workspaceId), enabled: kind === "mcp" && mode === "connection" });
  const connectionMetadata = [...(connections.isSuccess ? connections.data : []), ...(kind === "mcp" && toolConnections.isSuccess ? toolConnections.data : [])];
  const connectionMetadataUnavailable = !connections.isSuccess && (kind !== "mcp" || !toolConnections.isSuccess);
  function eligibleConnection(id: unknown) {
    const matches = connectionMetadata.filter(item => item.connection_id === id);
    return !connectionMetadataUnavailable && matches.length === 1 && matches[0]?.workspace_id === workspaceId && matches[0]?.availability === "configured";
  }
  const connectionOptions = connectionMetadata.filter(item => item.workspace_id === workspaceId).filter((item, index, items) => items.findIndex(other => other.connection_id === item.connection_id) === index);
  const unsettled = intent !== null && (intent.result === null || !confirmed(intent.result) && intent.result.state !== "rejected");
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const conflicted = intent?.result?.state === "rejected";
  const register = tabs.registerDirectRoute;
  const remember = useCallback((value: ConfigurationRecovery | undefined) => {
    const state = loadTabs(workspaceId);
    const current = state.tabs.find(tab => tab.id === tabId);
    if (current?.kind !== "agent") throw new AgentApiProblem(0, "recovery_storage_unavailable");
    const tab = { ...current };
    if (value === undefined) delete tab.configurationRecovery;
    else tab.configurationRecovery = value;
    saveTabs(reduceTabs(state, registerDirectRoute(tab)));
    register(tab);
  }, [register, tabId, workspaceId]);
  const resourceId = expected?.resource_id;
  const settle = useCallback(async (value: ConfigurationResult, known?: ConfigurationRecovery, parent?: ResourceRef) => {
    if (!("resource_id" in value.target_ref) || value.target_ref.workspace_id !== workspaceId
      || known?.target_ref !== undefined && !sameRef(value.target_ref, known.target_ref)
      || resourceId !== undefined && value.target_ref.resource_id !== resourceId
      || parent !== undefined && value.target_ref.revision !== parent.revision + 1) throw new AgentApiProblem(0, "configuration_result_mismatch");
    if (!mounted.current) return;
    remember({ operationId: value.operationId, target_ref: value.target_ref });
    if (!confirmed(value)) { setIntent({ operationId: value.operationId, result: value }); return; }
    if (value.result_ref === null || !("resource_id" in value.result_ref) || !sameRef(value.result_ref, value.target_ref)) throw new AgentApiProblem(0, "configuration_result_mismatch");
    const saved = await readResource(workspaceId, value.result_ref.resource_id, value.result_ref.revision);
    if (!sameRef(saved.resource.ref, value.result_ref) || saved.resource.draft.kind !== kind) throw new AgentApiProblem(0, "configuration_result_mismatch");
    if (!mounted.current) return;
    setDraft(saved.resource.draft); setBaseline(saved.resource.draft); setExpected(saved.resource.ref); setMode(resourceMode(saved.resource.draft));
    setIntent({ operationId: value.operationId, result: value }); remember(undefined); onSaved(saved);
  }, [kind, onSaved, remember, resourceId, workspaceId]);
  useEffect(() => {
    mounted.current = true;
    if (recovery !== undefined && !restored.current) {
      restored.current = true; submitting.current = true;
      void readConfigurationRequest(workspaceId, recovery.operationId).then(value => settle(value, recovery))
        .catch(() => { if (mounted.current) setError("资源保存结果未确认"); })
        .finally(() => { submitting.current = false; if (mounted.current) setPending(false); });
    }
    return () => { mounted.current = false; };
  }, [recovery, settle, workspaceId]);
  function edit(value: Partial<ResourceDraft>) {
    if (pending || unsettled) return;
    setDraft(previous => ({ ...previous, ...value })); setError(null);
    if (intent?.result && confirmed(intent.result)) setIntent(null);
  }
  function setting(key: string, value: ResourceDraft["settings"][string]) { edit({ settings: { ...draft.settings, [key]: value } }); }
  async function save() {
    if (submitting.current || unsettled || credentialGuard.dirty || credentialGuard.unsettled || conflicted || !dirty) return;
    if (mode === "connection" && !eligibleConnection(draft.settings.connection_id)) { setError("受信连接不可用"); return; }
    const operationId = crypto.randomUUID(), original = structuredClone(draft);
    try { remember({ operationId }); } catch { setError("无法保留原资源保存请求"); return; }
    submitting.current = true; setPending(true); setIntent({ operationId, result: null }); setError(null);
    try { await settle(await saveResource(workspaceId, operationId, original, expected), undefined, expected); }
    catch { if (mounted.current) setError("资源保存结果未确认"); }
    finally { submitting.current = false; if (mounted.current) setPending(false); }
  }
  async function queryOriginal() {
    if (submitting.current || intent === null) return;
    submitting.current = true; setPending(true);
    try { await settle(await readConfigurationRequest(workspaceId, intent.operationId), loadTabs(workspaceId).tabs.find(tab => tab.id === tabId)?.configurationRecovery); setError(null); }
    catch { if (mounted.current) setError("资源保存结果未确认"); }
    finally { submitting.current = false; if (mounted.current) setPending(false); }
  }
  async function reviewHead() {
    if (expected === undefined || submitting.current) return;
    submitting.current = true; setPending(true);
    try {
      const value = await readResource(workspaceId, expected.resource_id);
      if (value.resource.ref.workspace_id !== workspaceId || value.resource.ref.resource_id !== expected.resource_id || value.resource.draft.kind !== kind) throw new AgentApiProblem(0, "configuration_result_mismatch");
      if (mounted.current) setHead(value);
    } catch { if (mounted.current) setError("资源当前版本不可用"); }
    finally { submitting.current = false; if (mounted.current) setPending(false); }
  }
  const source = objectSetting(draft.settings.source_ref), command = stringArray(draft.settings.command);
  return <section className="agent-settings" aria-label="资源编辑">
    <ConfigurationNavigationGuard dirty={dirty || credentialGuard.dirty} unsettled={unsettled || credentialGuard.unsettled} />
    {onCancel ? <button className="quiet-button" disabled={pending || unsettled || dirty} onClick={onCancel}><ArrowLeft size={14} />返回资源列表</button> : null}
    {expected ? <p className="agent-identity"><span className="mono-label">{expected.resource_id}</span><strong>R{expected.revision}</strong></p> : null}
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset disabled={pending || unsettled} className="agent-form-fields">
        <label className="worker-runtime-field"><span>资源名称</span><input required maxLength={200} value={draft.name} onChange={event => edit({ name: event.target.value })} /></label>
        {kind !== "integration" ? <label className="worker-runtime-field"><span>资源模式</span><select value={mode} onChange={event => { const next = modes(kind).find(item => item.key === event.target.value); if (next) { setMode(next.key); edit({ settings: emptySettings(next.key) }); } }}>{modes(kind).map(item => <option key={item.key} value={item.key}>{item.name}</option>)}</select></label> : null}
        {mode === "inline" ? <label className="worker-runtime-field agent-form-fields__wide"><span>Skill 正文</span><textarea required rows={9} value={text(draft.settings.body)} onChange={event => setting("body", event.target.value)} /></label> : null}
        {mode === "source" ? <>
          {([["provider", "来源提供方"], ["key", "来源键"], ["version", "来源版本"]] as const).map(([key, label]) => <label className="worker-runtime-field" key={key}><span>{label}</span><input required value={text(source[key])} onChange={event => setting("source_ref", { ...source, [key]: event.target.value })} /></label>)}
          <label className="worker-runtime-field"><span>SHA256</span><input required pattern="[a-fA-F0-9]{64}" value={text(draft.settings.content_hash)} onChange={event => setting("content_hash", event.target.value)} /></label>
        </> : null}
        {mode === "connection" ? <label className="worker-runtime-field agent-form-fields__wide"><span>受信连接</span><select aria-label="受信连接" required value={text(draft.settings.connection_id)} disabled={connectionMetadataUnavailable} onChange={event => setting("connection_id", event.target.value)}><option value="">选择连接</option>{connectionOptions.map(item => <option key={item.connection_id} value={item.connection_id} disabled={!eligibleConnection(item.connection_id)}>{item.name}{"revision" in item ? ` · R${item.revision}` : ""} · {item.availability}</option>)}</select>{connections.isError ? <small role="alert">连接元数据不可用</small> : null}{kind === "mcp" && toolConnections.isError ? <small role="alert">任务工具连接元数据不可用</small> : null}</label> : null}
        {mode === "stdio" ? <>
          <label className="worker-runtime-field agent-form-fields__wide"><span>可执行文件绝对路径</span><input required value={command[0] ?? ""} onChange={event => setting("command", [event.target.value, ...command.slice(1)])} /></label>
          <div className="agent-form-fields__wide">{command.slice(1).map((item, index) => <div className="agent-environment__row" key={index}><label className="worker-runtime-field"><span>参数 {index + 1}</span><input required value={item} onChange={event => setting("command", command.map((value, i) => i === index + 1 ? event.target.value : value))} /></label><button className="icon-button" type="button" aria-label={`移除参数 ${index + 1}`} title="移除参数" onClick={() => setting("command", command.filter((_, i) => i !== index + 1))}><Trash2 size={14} /></button></div>)}<button className="quiet-button" type="button" onClick={() => setting("command", [...command, ""])}><Plus size={14} />添加参数</button></div>
          <EnvironmentFields value={stringMap(draft.settings.environment)} onChange={value => setting("environment", value)} />
        </> : null}
        {mode === "http" ? <><label className="worker-runtime-field agent-form-fields__wide"><span>MCP 地址</span><input type="url" required value={text(draft.settings.url)} onChange={event => setting("url", event.target.value)} /></label><HeaderFields value={stringMap(draft.settings.headers)} onChange={value => setting("headers", value)} /></> : null}
        <CredentialPicker workspaceId={workspaceId} value={draft.secret_refs} onChange={secret_refs => edit({ secret_refs })} onGuardChange={setCredentialGuard} />
      </fieldset>
      <div className="agent-form-actions"><button className="quiet-button" type="submit" disabled={pending || unsettled || credentialGuard.dirty || credentialGuard.unsettled || conflicted || !dirty || mode === "connection" && !eligibleConnection(draft.settings.connection_id)}><Save size={14} />保存资源</button></div>
    </form>
    {intent?.result && confirmed(intent.result) && !error ? <p className="agent-success" role="status">{intent.result.state === "reconciled" ? "已找到原资源 · 现有绑定未改变" : "资源已保存 · 现有绑定未改变"}</p> : null}
    {unsettled ? <><p role="status">资源保存结果未确认</p><button className="quiet-button" disabled={pending} onClick={() => void queryOriginal()}><RefreshCw size={14} />查询原资源保存结果</button></> : null}
    {conflicted ? <div className="agent-conflict"><p role="alert">资源保存被拒绝 · {intent.result?.reason_code}</p><button className="quiet-button" disabled={pending} onClick={() => void reviewHead()}><Eye size={14} />查看资源当前版本</button></div> : null}
    {head ? <div className="agent-head-review"><h3>当前资源 R{head.resource.ref.revision}</h3><button className="quiet-button" disabled={pending} onClick={() => { setExpected(head.resource.ref); setIntent(null); setHead(null); remember(undefined); }}>以当前资源版本重新确认</button></div> : null}
    {error && !(unsettled && error === "资源保存结果未确认") ? <p role="alert">{error}</p> : null}
  </section>;
}

function defaultMode(kind: ResourceDraft["kind"]): Mode { return kind === "skill" ? "inline" : "connection"; }
function modes(kind: ResourceDraft["kind"]): { key: Mode; name: string }[] { return kind === "skill" ? [{ key: "inline", name: "内联正文" }, { key: "source", name: "已授权来源" }] : [{ key: "connection", name: "受信连接" }, { key: "stdio", name: "Stdio" }, { key: "http", name: "HTTP" }]; }
function resourceMode(draft: ResourceDraft): Mode { return draft.kind === "skill" ? "source_ref" in draft.settings ? "source" : "inline" : draft.settings.transport === "stdio" ? "stdio" : draft.settings.transport === "http" ? "http" : "connection"; }
function emptySettings(mode: Mode): ResourceDraft["settings"] { return mode === "inline" ? { body: "" } : mode === "source" ? { source_ref: { provider: "", key: "", version: "" }, content_hash: "" } : mode === "stdio" ? { transport: "stdio", command: [""], environment: {} } : mode === "http" ? { transport: "http", url: "", headers: {} } : { connection_id: "" }; }
function text(value: unknown): string { return typeof value === "string" ? value : ""; }
function stringArray(value: unknown): string[] { return Array.isArray(value) && value.every(item => typeof item === "string") ? value : []; }
function objectSetting(value: ResourceDraft["settings"][string]): ResourceDraft["settings"] { return typeof value === "object" && value !== null && !Array.isArray(value) ? value : {}; }
function stringMap(value: ResourceDraft["settings"][string]): Record<string, string> { return Object.fromEntries(Object.entries(objectSetting(value)).filter((row): row is [string, string] => typeof row[1] === "string")); }

function HeaderFields({ value, onChange }: { value: Record<string, string>; onChange: (value: Record<string, string>) => void }) {
  const rows = Object.entries(value);
  return <fieldset className="agent-environment agent-form-fields__wide"><legend>非秘密 Headers</legend>{rows.map(([name, item], index) => <div className="agent-environment__row" key={index}>
    <input aria-label={`Header 名称 ${index + 1}`} required value={name} onChange={event => onChange(Object.fromEntries(rows.map((row, i) => i === index ? [event.target.value, item] : row)))} />
    <input aria-label={`Header 值 ${index + 1}`} value={item} onChange={event => onChange(Object.fromEntries(rows.map((row, i) => i === index ? [name, event.target.value] : row)))} />
    <button className="icon-button" type="button" title="移除 Header" aria-label={`移除 Header ${index + 1}`} onClick={() => onChange(Object.fromEntries(rows.filter((_, i) => i !== index)))}><Trash2 size={14} /></button>
  </div>)}<button className="quiet-button" type="button" disabled={Object.hasOwn(value, "")} onClick={() => onChange({ ...value, "": "" })}><Plus size={14} />添加 Header</button></fieldset>;
}
