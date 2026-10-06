import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, RefreshCw, ShieldOff, Unlink } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { CredentialWriteDialog } from "./CredentialWriteDialog";
import { AgentApiProblem, listCredentials, revokeCredential } from "./api";
import type { Credential, SecretBinding } from "./types";

export function CredentialPicker({ workspaceId, value, onChange, onGuardChange }: {
  workspaceId: string; value: SecretBinding[]; onChange: (value: SecretBinding[]) => void;
  onGuardChange?: (value: { dirty: boolean; unsettled: boolean }) => void;
}) {
  const client = useQueryClient();
  const credentials = useQuery({ queryKey: ["agent-credentials", workspaceId], queryFn: () => listCredentials(workspaceId) });
  const [selected, setSelected] = useState(""), [destination, setDestination] = useState("");
  const [writing, setWriting] = useState(false), [revoking, setRevoking] = useState<Credential | null>(null);
  const [revokeOpen, setRevokeOpen] = useState(false), [revokeObservation, setRevokeObservation] = useState<Credential | null>(null);
  const [writeGuard, setWriteGuard] = useState({ dirty: false, unsettled: false });
  const [pending, setPending] = useState(false), [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<string | null>(null), [notice, setNotice] = useState<string | null>(null);
  const submitting = useRef(false);
  const own = credentials.data?.filter(item => item.workspace_id === workspaceId);
  useEffect(() => {
    onGuardChange?.({ dirty: writeGuard.dirty, unsettled: writeGuard.unsettled || uncertain });
  }, [onGuardChange, writeGuard.dirty, writeGuard.unsettled, uncertain]);
  function bind() {
    const credential = own?.find(item => item.credential_id === selected);
    if (credential?.status !== "active") { setError("凭据引用不可用"); return; }
    if (!/^[A-Za-z_][A-Za-z0-9_.-]*$/.test(destination)) { setError("注入目标无效"); return; }
    if (value.some(item => item.destination.toLowerCase() === destination.toLowerCase())) { setError("注入目标已绑定"); return; }
    onChange([...value, { credential_ref: { workspace_id: workspaceId, credential_id: credential.credential_id }, destination }]);
    setDestination(""); setSelected(""); setError(null);
  }
  async function revoke() {
    if (submitting.current || uncertain || revoking === null) return;
    const original = revoking;
    submitting.current = true; setPending(true); setUncertain(true); setError(null);
    onGuardChange?.({ dirty: writeGuard.dirty, unsettled: true });
    try {
      const result = await revokeCredential(workspaceId, original.credential_id, original.version);
      if (result.credential.workspace_id !== workspaceId || result.credential.credential_id !== original.credential_id || result.credential.status !== "revoked" || typeof result.cleanup_pending !== "boolean") throw new AgentApiProblem(0, "credential_result_mismatch");
      client.setQueryData<Credential[]>(["agent-credentials", workspaceId], previous => previous?.map(item => item.credential_id === original.credential_id ? result.credential : item));
      setUncertain(false); setRevoking(null); setRevokeOpen(false); setNotice(result.cleanup_pending ? "凭据已撤销 · 清理待确认" : "凭据已撤销");
    } catch { setError("撤销结果未确认"); }
    finally { submitting.current = false; setPending(false); }
  }
  function closeRevoke() {
    if (submitting.current) return;
    setRevokeOpen(false);
    if (!uncertain) { setRevoking(null); setRevokeObservation(null); }
  }
  async function observeOriginalRevoke() {
    if (submitting.current || revoking === null) return;
    const original = revoking;
    submitting.current = true; setPending(true);
    try {
      const items = await listCredentials(workspaceId);
      const current = items.find(item => item.workspace_id === original.workspace_id && item.credential_id === original.credential_id);
      if (current === undefined) throw new AgentApiProblem(0, "credential_result_mismatch");
      setRevokeObservation(current);
    } catch { setError("原撤销目标当前状态未确认"); }
    finally { submitting.current = false; setPending(false); }
  }
  return <fieldset className="agent-environment agent-form-fields__wide"><legend>凭据引用</legend>
    {credentials.isError ? <p role="alert">凭据元数据不可用</p> : credentials.isPending ? <p role="status">正在读取凭据元数据</p> : own?.length === 0 ? <p>尚无凭据</p> : null}
    {value.map(binding => <div className="agent-binding-row" key={binding.destination}><span>{own?.find(item => item.credential_id === binding.credential_ref.credential_id)?.alias ?? binding.credential_ref.credential_id} · {binding.destination}</span><span className="agent-row-meta">{binding.credential_ref.workspace_id !== workspaceId ? "工作区不匹配" : own?.find(item => item.credential_id === binding.credential_ref.credential_id)?.status ?? "凭据状态未确认"}</span><button className="icon-button" type="button" title="解除绑定" aria-label={`解除 ${binding.destination} 绑定`} onClick={() => onChange(value.filter(item => item !== binding))}><Unlink size={14} /></button></div>)}
    <div className="agent-form-fields">
      <label className="worker-runtime-field"><span>凭据引用</span><select value={selected} disabled={credentials.isPending || credentials.isError} onChange={event => setSelected(event.target.value)}><option value="">选择安全引用</option>{own?.map(item => <option key={item.credential_id} value={item.credential_id} disabled={item.status !== "active"}>{item.alias} · {item.status}</option>)}</select></label>
      <label className="worker-runtime-field"><span>注入目标</span><input value={destination} autoComplete="off" onChange={event => setDestination(event.target.value)} /></label>
    </div>
    <div className="agent-form-actions"><button className="quiet-button" type="button" disabled={credentials.isError || credentials.isPending || !selected || !destination} onClick={bind}><Plus size={14} />绑定凭据</button><button className="quiet-button" type="button" onClick={() => setWriting(true)}><KeyRound size={14} />新建凭据</button><button className="icon-button" type="button" title="刷新凭据元数据" aria-label="刷新凭据元数据" onClick={() => void credentials.refetch()}><RefreshCw size={14} /></button></div>
    <details className="agent-credential-management"><summary>凭据管理</summary>{own?.map(item => <div className="agent-binding-row" key={item.credential_id}><span>{item.alias} · {item.status} · V{item.version}</span><button className="icon-button" type="button" title="撤销凭据" aria-label={`撤销 ${item.alias}`} disabled={item.status !== "active" || uncertain || pending} onClick={() => { setRevoking(item); setRevokeOpen(true); }}><ShieldOff size={14} /></button></div>)}</details>
    {revoking && revokeOpen ? <div className="agent-head-review" role="dialog" aria-modal="true" aria-label="撤销凭据"><p>{revoking.alias} · V{revoking.version}</p><button className="quiet-button" type="button" disabled={pending || uncertain} onClick={() => void revoke()}>确认撤销</button><button className="quiet-button" type="button" disabled={pending} onClick={closeRevoke}>关闭</button></div> : null}
    {revoking && uncertain ? <div className="agent-head-review"><span>原撤销目标 · {revoking.alias} · V{revoking.version}</span><button className="quiet-button" type="button" disabled={pending} onClick={() => void observeOriginalRevoke()}>读取原撤销凭据当前状态</button><button className="quiet-button" type="button" onClick={() => setRevokeOpen(true)}>查看原撤销凭据</button>{revokeObservation ? <p>原撤销目标 · {revokeObservation.alias} · {revokeObservation.status} · V{revokeObservation.version}</p> : null}</div> : null}
    {notice ? <p role="status">{notice}</p> : null}{error ? <p role="alert">{error}</p> : null}
    <CredentialWriteDialog workspaceId={workspaceId} open={writing} onClose={() => setWriting(false)} onCompleted={() => { void credentials.refetch(); }} onGuardChange={setWriteGuard} />
  </fieldset>;
}
