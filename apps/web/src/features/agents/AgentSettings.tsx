import { AlertCircle, Eye, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useBeforeUnload, useBlocker } from "react-router-dom";

import { useWorkspaceTabs } from "@/app/WorkspaceTabs";
import { loadTabs, reduceTabs, registerDirectRoute, saveTabs, type ConfigurationRecovery } from "@/app/workspaceTabsState";
import { CredentialPicker } from "./CredentialPicker";
import { ResourceBindings } from "./ResourceBindings";
import { AgentApiProblem, readConfigurationRequest, readProfile, saveProfile } from "./api";
import { confirmed, sameRef, type ConfigField, type ConfigurationResult, type ProfileDraft, type ProfileRef, type ProfileView, type ResourceDraft, type ResourceView, type RuntimeDescriptor, type RuntimeSnapshot } from "./types";

type SaveIntent = { operationId: string; result: ConfigurationResult | null };

export function AgentSettings({ workspaceId, profile, runtimes, descriptors, onSaved, capability, resources = [], resourcesPending = false, resourcesError = false }: {
  workspaceId: string;
  profile: ProfileView | null;
  runtimes: RuntimeSnapshot[];
  descriptors: RuntimeDescriptor[];
  onSaved: (profile: ProfileView) => void;
  capability?: "instructions" | ResourceDraft["kind"];
  resources?: ResourceView[];
  resourcesPending?: boolean;
  resourcesError?: boolean;
}) {
  const tabs = useWorkspaceTabs();
  const tabId = `agent:${profile?.profile.ref.profile_id ?? "new"}` as const;
  const [recovery] = useState(() => tabs.state.tabs.find(tab => tab.id === tabId)?.configurationRecovery);
  const [draft, setDraft] = useState<ProfileDraft>(() => profile?.profile.draft ?? emptyProfile(workspaceId));
  const [baseline, setBaseline] = useState(draft);
  const [expected, setExpected] = useState<ProfileRef | undefined>(profile?.profile.ref);
  const [intent, setIntent] = useState<SaveIntent | null>(() => recovery ? { operationId: recovery.operationId, result: null } : null);
  const [pending, setPending] = useState(recovery !== undefined);
  const [error, setError] = useState<string | null>(null);
  const [head, setHead] = useState<ProfileView | null>(null);
  const [credentialsOpen, setCredentialsOpen] = useState(draft.secret_refs.length > 0);
  const [credentialGuard, setCredentialGuard] = useState({ dirty: false, unsettled: false });
  const [resourceUnsettled, setResourceUnsettled] = useState(false);
  const mounted = useRef(false);
  const submitting = useRef(false);
  const restored = useRef(false);
  const runtime = runtimes.find(item => item.ref.runtime_id === draft.runtime_ref.runtime_id);
  const descriptor = descriptors.find(item => item.harness_id === runtime?.harness_id);
  const unsettled = intent !== null && (intent.result === null || !confirmed(intent.result) && intent.result.state !== "rejected");
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const conflicted = intent?.result?.state === "rejected";

  const register = tabs.registerDirectRoute;
  const remember = useCallback((value: ConfigurationRecovery | undefined) => {
    const state = loadTabs(workspaceId);
    const current = state.tabs.find(tab => tab.id === tabId);
    if (current === undefined) throw new AgentApiProblem(0, "recovery_storage_unavailable");
    const tab = { ...current };
    if (value === undefined) delete tab.configurationRecovery;
    else tab.configurationRecovery = value;
    saveTabs(reduceTabs(state, registerDirectRoute(tab)));
    register(tab);
  }, [register, tabId, workspaceId]);

  const profileId = expected?.profile_id;
  const settle = useCallback(async (value: ConfigurationResult, known?: ConfigurationRecovery, parent?: ProfileRef) => {
    if (value.target_ref.workspace_id !== workspaceId || !("profile_id" in value.target_ref)
      || known?.target_ref !== undefined && !sameRef(value.target_ref, known.target_ref)
      || profileId !== undefined && value.target_ref.profile_id !== profileId
      || parent !== undefined && value.target_ref.revision !== parent.revision + 1) {
      throw new AgentApiProblem(0, "configuration_result_mismatch");
    }
    if (!mounted.current) return;
    remember({ operationId: value.operationId, target_ref: value.target_ref });
    if (!confirmed(value)) { setIntent({ operationId: value.operationId, result: value }); return; }
    if (value.result_ref === null || !sameRef(value.target_ref, value.result_ref) || !("profile_id" in value.result_ref)) {
      throw new AgentApiProblem(0, "configuration_result_mismatch");
    }
    const saved = await readProfile(workspaceId, value.result_ref.profile_id, value.result_ref.revision);
    if (!sameRef(saved.profile.ref, value.result_ref)) throw new AgentApiProblem(0, "configuration_result_mismatch");
    if (!mounted.current) return;
    setIntent({ operationId: value.operationId, result: value });
    setDraft(saved.profile.draft); setBaseline(saved.profile.draft); setExpected(saved.profile.ref);
    remember(undefined);
    onSaved(saved);
  }, [onSaved, profileId, remember, workspaceId]);

  useEffect(() => {
    mounted.current = true;
    if (recovery !== undefined && !restored.current) {
      restored.current = true;
      submitting.current = true;
      void readConfigurationRequest(workspaceId, recovery.operationId)
        .then(value => settle(value, recovery))
        .catch(() => { if (mounted.current) setError("保存结果未确认"); })
        .finally(() => { submitting.current = false; if (mounted.current) setPending(false); });
    }
    return () => { mounted.current = false; };
  }, [workspaceId, recovery, settle]);

  function edit(value: Partial<ProfileDraft>) {
    if (pending || unsettled) return;
    setDraft(previous => ({ ...previous, ...value }));
    if (intent?.result && confirmed(intent.result)) setIntent(null);
    setError(null);
  }

  async function save() {
    if (submitting.current || unsettled || credentialGuard.unsettled || resourceUnsettled || conflicted || !dirty) return;
    const operationId = crypto.randomUUID();
    const original = structuredClone(draft);
    try { remember({ operationId }); }
    catch { setError("无法保留原保存请求"); return; }
    submitting.current = true; setPending(true); setError(null); setIntent({ operationId, result: null });
    try { await settle(await saveProfile(workspaceId, operationId, original, expected), undefined, expected); }
    catch { if (mounted.current) setError("保存结果未确认"); }
    finally { submitting.current = false; if (mounted.current) setPending(false); }
  }

  async function queryOriginal() {
    if (submitting.current || intent === null) return;
    submitting.current = true; setPending(true); setError(null);
    try { await settle(await readConfigurationRequest(workspaceId, intent.operationId), loadTabs(workspaceId).tabs.find(tab => tab.id === tabId)?.configurationRecovery); }
    catch { if (mounted.current) setError("保存结果未确认"); }
    finally { submitting.current = false; if (mounted.current) setPending(false); }
  }

  async function reviewHead() {
    if (submitting.current || expected === undefined) return;
    submitting.current = true; setPending(true);
    try {
      const value = await readProfile(workspaceId, expected.profile_id);
      if (value.profile.ref.workspace_id !== workspaceId || value.profile.ref.profile_id !== expected.profile_id) throw new AgentApiProblem(0, "configuration_result_mismatch");
      if (mounted.current) setHead(value);
    } catch { if (mounted.current) setError("无法读取当前版本"); }
    finally { submitting.current = false; if (mounted.current) setPending(false); }
  }

  return <section className="agent-settings" aria-label="Profile 设置">
    <ConfigurationNavigationGuard dirty={dirty || credentialGuard.dirty} unsettled={unsettled || credentialGuard.unsettled || resourceUnsettled} />
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset disabled={pending || unsettled} className="agent-form-fields">
        {capability === undefined ? <><label className="worker-runtime-field"><span>名称</span><input required value={draft.name} maxLength={200} onChange={event => edit({ name: event.target.value })} /></label>
        <label className="worker-runtime-field"><span>描述</span><input value={draft.description} onChange={event => edit({ description: event.target.value })} /></label>
        <label className="worker-runtime-field"><span>运行环境</span><select required value={draft.runtime_ref.runtime_id} onChange={event => edit({ runtime_ref: { workspace_id: workspaceId, runtime_id: event.target.value } })}><option value="">选择运行环境</option>{runtimes.map(item => <option key={item.ref.runtime_id} value={item.ref.runtime_id}>{item.harness_id} · {item.actual_version} · {item.enabled ? "已启用" : "已停用"}</option>)}</select></label>
        </> : null}
        {capability === undefined || capability === "instructions" ? <label className="worker-runtime-field agent-form-fields__wide"><span>指令</span><textarea rows={9} value={draft.instructions} onChange={event => edit({ instructions: event.target.value })} /></label> : null}
        {capability === undefined ? <>{descriptor?.fields.map(field => <NativeField key={field.key} field={field} draft={draft} onChange={edit} />)}
        {descriptor === undefined ? <p role="status">运行环境配置元数据不可用</p> : null}
        <label className="worker-runtime-field"><span>并行安排上限</span><input type="number" min={1} required value={draft.max_active} onChange={event => edit({ max_active: Number(event.target.value) })} /></label>
        <EnvironmentFields value={draft.environment} onChange={environment => edit({ environment })} /></> : null}
        {capability !== undefined && capability !== "instructions" ? <ResourceBindings workspaceId={workspaceId} kind={capability} value={draft.resource_refs} resources={resources} pending={resourcesPending} failed={resourcesError} onChange={resource_refs => edit({ resource_refs })} onUnsettled={setResourceUnsettled} /> : null}
        {capability === undefined || capability === "instructions" ? <div className="agent-form-fields__wide"><button className="quiet-button" type="button" disabled={credentialGuard.dirty || credentialGuard.unsettled} onClick={() => setCredentialsOpen(previous => !previous)}><Plus size={14} />{credentialsOpen ? "收起凭据引用" : "配置凭据引用"}</button>{credentialsOpen ? <CredentialPicker workspaceId={workspaceId} value={draft.secret_refs} onChange={secret_refs => edit({ secret_refs })} onGuardChange={setCredentialGuard} /> : null}</div> : null}
      </fieldset>
      <div className="agent-form-actions"><button className="quiet-button" type="submit" disabled={pending || unsettled || credentialGuard.unsettled || resourceUnsettled || conflicted || !dirty}><Save size={14} />{pending ? "保存中" : "保存 Profile"}</button></div>
    </form>
    {intent?.result && confirmed(intent.result) && !error ? <p className="agent-success" role="status">{intent.result.state === "reconciled" ? "已找到原配置 · 仅用于新安排" : "配置已保存 · 仅用于新安排"}</p> : null}
    {unsettled && !pending ? <p role="status">保存结果未确认</p> : null}
    {unsettled ? <button className="quiet-button" disabled={pending} onClick={() => void queryOriginal()}><RefreshCw size={14} />查询原保存结果</button> : null}
    {conflicted ? <div className="agent-conflict"><p role="alert">{intent.result?.reason_code === "revision_conflict" || intent.result?.reason_code === "version_conflict" ? "配置版本已变化" : "保存被拒绝"}</p><button className="quiet-button" disabled={pending} onClick={() => void reviewHead()}><Eye size={14} />查看当前版本</button></div> : null}
    {head ? <div className="agent-head-review"><h3>当前版本 R{head.profile.ref.revision}</h3><p>{head.profile.draft.name}</p><button className="quiet-button" disabled={pending} onClick={() => { setExpected(head.profile.ref); setIntent(null); setHead(null); remember(undefined); }}>以当前版本重新确认</button></div> : null}
    {error && !(unsettled && error === "保存结果未确认") ? <p role="alert"><AlertCircle size={14} />{error}</p> : null}
  </section>;
}

function emptyProfile(workspaceId: string): ProfileDraft {
  return { name: "", description: "", runtime_ref: { workspace_id: workspaceId, runtime_id: "" }, instructions: "", resource_refs: [], model_id: null, thinking: null, max_active: 1, environment: {}, secret_refs: [], native_settings: {} };
}

export function NativeField({ field, draft, onChange }: { field: ConfigField; draft: ProfileDraft; onChange: (value: Partial<ProfileDraft>) => void }) {
  const id = useId();
  const key = field.key.replace(/^native_settings\./, "");
  const value = key === "model_id" ? draft.model_id : key === "thinking" ? draft.thinking : draft.native_settings[key];
  const disabled = field.support.status !== "supported";
  const accessible = { "aria-labelledby": `${id}-name`, "aria-describedby": disabled ? `${id}-reason` : undefined };
  function change(next: string | number | boolean) {
    if (key === "model_id" || key === "thinking") { onChange({ [key]: next === "" ? null : String(next) }); return; }
    const settings = { ...draft.native_settings };
    if (next === "") delete settings[key];
    else settings[key] = next;
    onChange({ native_settings: settings });
  }
  const scalar = typeof value === "string" || typeof value === "number" ? value : "";
  return <label className="worker-runtime-field">
    <span id={`${id}-name`}>{field.label}</span>
    {field.kind === "toggle" ? <input {...accessible} type="checkbox" checked={value === true} disabled={disabled} onChange={event => change(event.target.checked)} />
      : field.kind === "select" ? <select {...accessible} value={String(scalar)} disabled={disabled} onChange={event => change(event.target.value)}><option value="">继承运行环境默认值</option>{field.options.map(option => <option key={option} value={option}>{option}</option>)}</select>
        : <input {...accessible} type={field.kind === "number" ? "number" : "text"} value={scalar} placeholder="继承运行环境默认值" disabled={disabled} min={field.minimum ?? undefined} max={field.maximum ?? undefined} onChange={event => change(field.kind === "number" && event.target.value !== "" ? Number(event.target.value) : event.target.value)} />}
    {disabled ? <small id={`${id}-reason`}>{field.support.reason_code}</small> : null}
  </label>;
}

export function EnvironmentFields({ value, onChange }: { value: Record<string, string>; onChange: (value: Record<string, string>) => void }) {
  const rows = Object.entries(value);
  return <fieldset className="agent-environment agent-form-fields__wide">
    <legend>环境变量</legend>
    {rows.map(([name, item], index) => <div className="agent-environment__row" key={index}>
      <input aria-label={`变量名称 ${index + 1}`} required pattern="[A-Za-z_][A-Za-z0-9_]*" value={name} onChange={event => onChange(Object.fromEntries(rows.map((row, i) => i === index ? [event.target.value, item] : row)))} />
      <input aria-label={`变量值 ${index + 1}`} value={item} onChange={event => onChange(Object.fromEntries(rows.map((row, i) => i === index ? [name, event.target.value] : row)))} />
      <button className="icon-button" type="button" title="移除变量" aria-label={`移除变量 ${index + 1}`} onClick={() => onChange(Object.fromEntries(rows.filter((_, i) => i !== index)))}><Trash2 size={14} /></button>
    </div>)}
    <button className="quiet-button" type="button" disabled={Object.hasOwn(value, "")} onClick={() => onChange({ ...value, "": "" })}><Plus size={14} />添加变量</button>
  </fieldset>;
}

export function ConfigurationNavigationGuard({ dirty, unsettled }: { dirty: boolean; unsettled: boolean }) {
  const tabs = useWorkspaceTabs();
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (dirty || unsettled)
    && `${currentLocation.pathname}${currentLocation.search}` !== `${nextLocation.pathname}${nextLocation.search}`);
  const original = useRef<typeof blocker | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  useBeforeUnload(useCallback((event: BeforeUnloadEvent) => {
    if (dirty || unsettled) { event.preventDefault(); event.returnValue = ""; }
  }, [dirty, unsettled]));
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (original.current === null) original.current = blocker;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { previous?.focus(); };
  }, [blocker]);
  function stay() {
    original.current?.reset?.();
    original.current = null;
    tabs.cancelPendingNavigation();
  }
  function leave() { original.current?.proceed?.(); original.current = null; }
  if (blocker.state !== "blocked") return null;
  return <div className="worker-runtime-dialog-backdrop"><div ref={dialog} className="worker-runtime-dialog" role="dialog" aria-modal="true" aria-labelledby="configuration-leave-title" onKeyDown={event => {
    if (event.key === "Escape") { event.preventDefault(); stay(); }
    if (event.key !== "Tab") return;
    const buttons = dialog.current?.querySelectorAll<HTMLButtonElement>("button");
    const first = buttons?.[0], last = buttons?.[1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}>
    <h2 id="configuration-leave-title">{unsettled ? "保存结果未确认" : "未保存的配置"}</h2>
    <p>{unsettled ? "原保存请求仍需确认。" : "本页的修改尚未保存。"}</p>
    <div className="worker-runtime-dialog__actions"><button className="quiet-button" onClick={stay}>留在此页</button><button className="quiet-button" onClick={leave}>离开</button></div>
  </div></div>;
}
