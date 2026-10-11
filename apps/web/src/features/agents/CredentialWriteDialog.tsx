import { RefreshCw, Save, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { AgentApiProblem, readCredentialWrite, recoverCredentialWrite, writeCredential } from "./api";
import type { Credential, CredentialWrite } from "./types";

export function CredentialWriteDialog({ workspaceId, open, onClose, onCompleted, onGuardChange }: {
  workspaceId: string; open: boolean; onClose: () => void; onCompleted: (value: Credential) => void;
  onGuardChange?: (value: { dirty: boolean; unsettled: boolean }) => void;
}) {
  const [alias, setAlias] = useState(""), [secret, setSecret] = useState("");
  const [operationId, setOperationId] = useState<string | null>(null);
  const [result, setResult] = useState<CredentialWrite | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false), dialog = useRef<HTMLDivElement>(null), mounted = useRef(false);
  const unsettled = operationId !== null && result?.status !== "completed" && result?.status !== "failed";
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { onGuardChange?.({ dirty: secret.length > 0, unsettled }); }, [onGuardChange, secret.length, unsettled]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.current?.querySelector<HTMLElement>("input:not([disabled]), button:not([disabled])")?.focus();
    return () => { previous?.focus(); };
  }, [open]);
  function close() {
    if (submitting.current) return;
    setSecret("");
    if (result?.status === "completed" || result?.status === "failed") {
      setOperationId(null); setResult(null); setAlias(""); setError(null);
    }
    onClose();
  }
  function publish(value: CredentialWrite, original: string) {
    if (value.workspace_id !== workspaceId || value.operation_id !== original
      || value.credential?.workspace_id !== workspaceId || typeof value.credential.credential_id !== "string"
      || !["pending", "completed", "failed", "unknown"].includes(value.status)
      || result !== null && value.credential.credential_id !== result.credential.credential_id
      || value.status === "completed" && value.credential.status !== "active") throw new AgentApiProblem(0, "credential_result_mismatch");
    if (!mounted.current) return;
    setResult(value); setError(null);
    if (value.status === "completed") onCompleted(value.credential);
  }
  async function save() {
    if (submitting.current || operationId !== null || alias.trim().length === 0 || secret.length === 0) return;
    const original = crypto.randomUUID(), submitted = secret;
    submitting.current = true; setPending(true); setOperationId(original); setSecret(""); setError(null);
    onGuardChange?.({ dirty: false, unsettled: true });
    try { publish(await writeCredential(workspaceId, { operation_id: original, alias: alias.trim(), secret: submitted, replaces_id: null }), original); }
    catch { if (mounted.current) setError("凭据写入结果未确认"); }
    finally { submitting.current = false; if (mounted.current) { setPending(false); setSecret(""); } }
  }
  async function observe(recover: boolean) {
    if (submitting.current || operationId === null) return;
    submitting.current = true; setPending(true);
    try { publish(await (recover ? recoverCredentialWrite(workspaceId, operationId) : readCredentialWrite(workspaceId, operationId)), operationId); }
    catch { if (mounted.current) setError("凭据写入结果未确认"); }
    finally { submitting.current = false; if (mounted.current) setPending(false); }
  }
  if (!open) return null;
  return createPortal(<div className="worker-runtime-dialog-backdrop"><div className="worker-runtime-dialog" role="dialog" aria-modal="true" aria-labelledby="credential-write-title" ref={dialog} onKeyDown={event => {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key !== "Tab") return;
    const fields = Array.from(dialog.current?.querySelectorAll<HTMLElement>("input:not([disabled]), button:not([disabled])") ?? []);
    const first = fields[0], last = fields.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}>
    <div className="worker-runtime-dialog__heading"><h2 id="credential-write-title">保存凭据</h2><button className="icon-button" type="button" title="关闭凭据" aria-label="关闭凭据" disabled={pending} onClick={close}><X size={16} /></button></div>
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <label className="worker-runtime-field"><span>凭据别名</span><input value={alias} maxLength={100} required disabled={operationId !== null} onChange={event => setAlias(event.target.value)} /></label>
      <label className="worker-runtime-field"><span>密钥值</span><input type="password" autoComplete="off" value={secret} required disabled={operationId !== null} onChange={event => setSecret(event.target.value)} /></label>
      <div className="worker-runtime-dialog__actions"><button className="quiet-button" type="submit" disabled={pending || operationId !== null || !secret || !alias.trim()}><Save size={14} />保存凭据</button></div>
    </form>
    {result?.status === "completed" ? <><p className="agent-success" role="status">凭据已保存</p><p>{result.credential.alias}</p></> : result?.status === "failed" ? <p role="alert">凭据写入失败</p> : unsettled ? <p role="status">凭据写入结果未确认</p> : null}
    {unsettled ? <div className="worker-runtime-dialog__actions"><button className="quiet-button" type="button" disabled={pending} onClick={() => void observe(false)}><RefreshCw size={14} />查询原凭据写入</button><button className="quiet-button" type="button" disabled={pending} onClick={() => void observe(true)}><RefreshCw size={14} />恢复原凭据写入</button></div> : null}
    {error && !unsettled ? <p role="alert">{error}</p> : null}
  </div></div>, document.body);
}
