import { LogIn, LogOut, RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { AgentApiProblem } from "@/features/agents/api";
import { exchangeLocalSession, logoutLocalSession, readLocalSession, type LocalSession } from "./api";

export function LocalSessionDialog({ workspaceId, onClose, onAuthorized }: {
  workspaceId: string;
  onClose: () => void;
  onAuthorized: (session: LocalSession) => void;
}) {
  const [session, setSession] = useState<LocalSession | null>(null);
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(false);
  const dialog = useRef<HTMLDivElement>(null);
  const submitting = useRef(false);

  const publishSession = useCallback((value: LocalSession, notify: boolean) => {
    if (!mounted.current) return;
    setSession(value);
    setDenied(false);
    setError(null);
    if (notify) onAuthorized(value);
  }, [onAuthorized]);
  const publishObservationError = useCallback((cause: unknown) => {
    if (!mounted.current) return;
    const absent = cause instanceof AgentApiProblem
      && cause.status === 401 && cause.code === "local_session_required";
    if (absent) { setSession(null); setDenied(false); }
    else { setDenied(true); setError("会话状态未确认"); }
  }, []);

  async function observe() {
    await readLocalSession().then(value => publishSession(value, true), publishObservationError);
  }

  useEffect(() => {
    mounted.current = true;
    const abort = new AbortController();
    void readLocalSession(abort.signal)
      .then(value => publishSession(value, false), publishObservationError)
      .finally(() => { if (mounted.current) setLoading(false); });
    return () => { mounted.current = false; abort.abort(); };
  }, [publishSession, publishObservationError]);

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.current?.querySelector<HTMLElement>("input, button, a")?.focus();
    return () => { previous?.focus(); };
  }, []);

  function close() { setToken(""); onClose(); }

  async function exchange() {
    if (submitting.current || denied || !token) return;
    submitting.current = true;
    setPending(true);
    setDenied(true);
    setError(null);
    try {
      await exchangeLocalSession(token);
      if (mounted.current) setToken("");
      await observe();
    } catch {
      if (mounted.current) { setToken(""); setError("会话结果尚未确认"); }
      await observe();
    } finally {
      submitting.current = false;
      if (mounted.current) { setPending(false); setToken(""); }
    }
  }

  async function logout() {
    if (submitting.current || denied) return;
    submitting.current = true;
    setPending(true);
    setError(null);
    try {
      await logoutLocalSession();
      if (mounted.current) { setSession(null); setDenied(false); }
    } catch {
      if (mounted.current) setError("退出结果尚未确认");
      await observe();
    } finally { submitting.current = false; if (mounted.current) setPending(false); }
  }

  async function query() {
    if (submitting.current) return;
    submitting.current = true;
    setPending(true);
    setError(null);
    try { await observe(); }
    finally { submitting.current = false; if (mounted.current) setPending(false); }
  }

  return <div className="worker-runtime-dialog-backdrop">
    <div className="worker-runtime-dialog" role="dialog" aria-modal="true" aria-labelledby="local-session-title" ref={dialog}
      onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); close(); }
        if (event.key !== "Tab") return;
        const fields = Array.from(dialog.current?.querySelectorAll<HTMLElement>("input:not([disabled]), button:not([disabled]), a[href]") ?? []);
        const first = fields[0], last = fields.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
      <div className="worker-runtime-dialog__heading">
        <h2 id="local-session-title">本地会话</h2>
        <button className="icon-button" onClick={close} aria-label="关闭本地会话" title="关闭本地会话"><X size={16} /></button>
      </div>
      {loading ? <p role="status">正在读取会话</p> : session ? <>
        {denied ? <p role="status">上次确认的会话 · 会话状态未确认</p> : null}
        <dl className="agent-facts"><dt>身份</dt><dd>{session.principal_id}</dd><dt>工作区</dt><dd className="mono-label">{session.workspace_id}</dd><dt>有效期</dt><dd>{new Date(session.expires_at).toLocaleString()}</dd></dl>
        {session.workspace_id !== workspaceId ? <Link className="quiet-button" to={`/workspaces/${session.workspace_id}/agents`}>打开已授权工作区</Link> : null}
        <button className="quiet-button" disabled={pending || denied} onClick={() => void logout()}><LogOut size={14} />退出本地会话</button>
      </> : !denied ? <form onSubmit={event => { event.preventDefault(); void exchange(); }}>
        <label className="worker-runtime-field"><span>CLI 令牌</span><input type="password" autoComplete="off" value={token} disabled={pending} maxLength={4096} onChange={event => setToken(event.target.value)} /></label>
        <div className="worker-runtime-dialog__actions"><button className="quiet-button" type="submit" disabled={pending || token.length === 0}><LogIn size={14} />连接</button></div>
      </form> : null}
      {!loading && denied ? <button className="quiet-button" disabled={pending} onClick={() => void query()}><RefreshCw size={14} />查询当前会话</button> : null}
      {error ? <p role="alert" className="worker-runtime-dialog__error">{error}</p> : null}
    </div>
  </div>;
}

export function LocalSessionButton({ workspaceId, onAuthorized }: { workspaceId: string; onAuthorized: (session: LocalSession) => void }) {
  const [open, setOpen] = useState(false);
  return <>
    <button className="quiet-button" onClick={() => setOpen(true)}><LogIn size={14} />本地会话</button>
    {open ? <LocalSessionDialog workspaceId={workspaceId} onClose={() => setOpen(false)} onAuthorized={onAuthorized} /> : null}
  </>;
}
