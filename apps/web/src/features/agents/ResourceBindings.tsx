import { useQueryClient } from "@tanstack/react-query";
import { Link2, Power, Unlink } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { AgentApiProblem, readResource, setResourceEnabled } from "./api";
import { sameRef, type ResourceDraft, type ResourceRef, type ResourceStatus, type ResourceView } from "./types";

export function ResourceBindings({ workspaceId, kind, value, resources, pending, failed, onChange, onUnsettled }: {
  workspaceId: string; kind: ResourceDraft["kind"]; value: ResourceRef[]; resources: ResourceView[];
  pending: boolean; failed: boolean; onChange: (value: ResourceRef[]) => void; onUnsettled: (value: boolean) => void;
}) {
  const unsettled = useRef(new Map<string, boolean>());
  const reportUnsettled = useCallback((resourceId: string, value: boolean) => {
    unsettled.current.set(resourceId, value);
    onUnsettled([...unsettled.current.values()].some(Boolean));
  }, [onUnsettled]);
  const own = resources.filter(item => item.resource.ref.workspace_id === workspaceId && item.resource.draft.kind === kind && Number.isInteger(item.resource.ref.revision) && item.resource.ref.revision > 0);
  const heads = own.filter(item => !own.some(other => other.resource.ref.resource_id === item.resource.ref.resource_id && other.resource.ref.revision > item.resource.ref.revision));
  return <div className="agent-form-fields__wide agent-resource-bindings">
    {pending ? <p role="status">正在读取资源</p> : failed ? <p role="alert">资源元数据不可用</p> : heads.length === 0 ? <p>尚无此类资源</p> : null}
    {value.map(ref => {
      const pinned = own.find(item => sameRef(item.resource.ref, ref));
      if (pinned === undefined) return null;
      return <div className="agent-binding-row" key={ref.resource_id}><strong>{pinned.resource.draft.name} · 当前绑定 R{ref.revision}</strong><button className="icon-button" type="button" title="解除资源绑定" aria-label={`解除 ${pinned.resource.draft.name} R${ref.revision} 绑定`} onClick={() => onChange(value.filter(item => item !== ref))}><Unlink size={14} /></button></div>;
    })}
    {heads.map(resource => <div className="agent-resource-row" key={resource.resource.ref.resource_id}>
      <div><strong>{resource.resource.draft.name} · R{resource.resource.ref.revision}</strong><span className="agent-row-meta mono-label">{resource.resource.ref.resource_id}</span></div>
      <div className="agent-form-actions"><button className="quiet-button" type="button" disabled={!resource.status.enabled || failed || value.some(ref => sameRef(ref, resource.resource.ref))} aria-label={`绑定 ${resource.resource.draft.name} R${resource.resource.ref.revision}`} onClick={() => onChange([...value.filter(item => item.resource_id !== resource.resource.ref.resource_id), resource.resource.ref])}><Link2 size={14} />绑定 R{resource.resource.ref.revision}</button>
        <Link className="quiet-button" to={`?view=capabilities&capability=${kind}&resource=${resource.resource.ref.resource_id}&revision=${resource.resource.ref.revision}`} aria-label={`编辑 ${resource.resource.draft.name} R${resource.resource.ref.revision}`}>编辑资源</Link>
        <ResourceStatusAction resource={resource} onUnsettled={reportUnsettled} />
      </div>
    </div>)}
  </div>;
}

function ResourceStatusAction({ resource, onUnsettled }: { resource: ResourceView; onUnsettled: (resourceId: string, value: boolean) => void }) {
  const client = useQueryClient();
  const [status, setStatus] = useState(resource.status), [observed, setObserved] = useState<ResourceStatus | null>(null);
  const [uncertain, setUncertain] = useState(false), [pending, setPending] = useState(false), [error, setError] = useState<string | null>(null);
  const submitting = useRef(false), ref = resource.resource.ref;
  const identity = `${ref.workspace_id}:${ref.resource_id}`;
  async function mutate() {
    if (submitting.current || uncertain) return;
    const original = status;
    submitting.current = true; setPending(true); setUncertain(true); onUnsettled(identity, true); setError(null);
    try {
      const next = await setResourceEnabled(ref, !original.enabled, original.version);
      if (next.resource_id !== ref.resource_id || next.enabled !== !original.enabled || next.version !== original.version + 1) throw new AgentApiProblem(0, "resource_status_mismatch");
      setStatus(next); setUncertain(false); onUnsettled(identity, false);
      client.setQueryData(["agent-resource", ref.workspace_id, ref.resource_id, ref.revision], { ...resource, status: next });
    } catch { setError("资源状态修改未确认"); }
    finally { submitting.current = false; setPending(false); }
  }
  async function observe() {
    if (submitting.current) return;
    submitting.current = true; setPending(true);
    try {
      const next = await readResource(ref.workspace_id, ref.resource_id, ref.revision);
      if (!sameRef(next.resource.ref, ref)) throw new AgentApiProblem(0, "resource_status_mismatch");
      setObserved(next.status);
    } catch { setError("资源当前状态不可用"); }
    finally { submitting.current = false; setPending(false); }
  }
  return <><button className="quiet-button" type="button" disabled={pending || uncertain} onClick={() => void mutate()} aria-label={`${status.enabled ? "停用" : "启用"} ${resource.resource.draft.name}`}><Power size={14} />{status.enabled ? "停用" : "启用"}</button>
    {uncertain ? <button className="quiet-button" type="button" disabled={pending} onClick={() => void observe()}>读取资源当前状态</button> : null}
    {observed ? <div className="agent-head-review"><p>当前状态 · {observed.enabled ? "已启用" : "已停用"} · V{observed.version}</p><button className="quiet-button" type="button" onClick={() => { setStatus(observed); setObserved(null); setUncertain(false); onUnsettled(identity, false); setError(null); }}>采用当前状态继续</button></div> : null}
    {error ? <p role="alert">{error}</p> : null}
  </>;
}
