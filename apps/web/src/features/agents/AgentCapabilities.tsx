import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useCallback } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { AgentSettings } from "./AgentSettings";
import { ResourceEditor } from "./ResourceEditor";
import { listResources, readResource, validateProfile } from "./api";
import type { ProfileView, ResourceDraft, ResourceView, RuntimeDescriptor, RuntimeSnapshot } from "./types";

const VIEWS = [{ key: "instructions", name: "指令" }, { key: "skill", name: "Skills" }, { key: "mcp", name: "MCP" }, { key: "integration", name: "Integrations" }] as const;

export function AgentCapabilities({ workspaceId, profile, runtimes, descriptors, onSaved }: {
  workspaceId: string; profile: ProfileView; runtimes: RuntimeSnapshot[]; descriptors: RuntimeDescriptor[]; onSaved: (value: ProfileView) => void;
}) {
  const [params] = useSearchParams(), client = useQueryClient();
  const view = VIEWS.find(item => item.key === params.get("capability"))?.key ?? "instructions";
  const resourceId = params.get("resource"), revision = params.has("revision") ? Number(params.get("revision")) : undefined;
  const validRevision = revision === undefined || Number.isInteger(revision) && revision > 0;
  const resources = useQuery({ queryKey: ["agent-resources", workspaceId], queryFn: () => listResources(workspaceId), enabled: view !== "instructions" });
  const pins = useQueries({ queries: profile.profile.draft.resource_refs.map(ref => ({ queryKey: ["agent-resource", workspaceId, ref.resource_id, ref.revision], queryFn: () => readResource(workspaceId, ref.resource_id, ref.revision), enabled: ref.workspace_id === workspaceId })) });
  const edited = useQuery({ queryKey: ["agent-resource", workspaceId, resourceId, revision ?? null], queryFn: () => readResource(workspaceId, resourceId ?? "", revision), enabled: resourceId !== null && resourceId !== "new" && validRevision });
  const validation = useQuery({ queryKey: ["agent-validation", profile.profile.ref], queryFn: () => validateProfile(profile.profile.ref) });
  const runtime = runtimes.find(item => item.ref.runtime_id === profile.profile.draft.runtime_ref.runtime_id);
  const support = view === "instructions" ? null : runtime?.capabilities[view];
  const onResourceSaved = useCallback((value: ResourceView) => {
    client.setQueryData(["agent-resource", workspaceId, value.resource.ref.resource_id, value.resource.ref.revision], value);
    void client.invalidateQueries({ queryKey: ["agent-resources", workspaceId] });
  }, [client, workspaceId]);
  const combined = new Map<string, ResourceView>();
  for (const item of [...resources.data?.items ?? [], ...pins.flatMap(pin => pin.data ? [pin.data] : [])]) combined.set(`${item.resource.ref.workspace_id}:${item.resource.ref.resource_id}:${item.resource.ref.revision}`, item);
  const resourceMode = view !== "instructions" && resourceId !== null;
  const kind: ResourceDraft["kind"] = view === "instructions" ? "skill" : view;
  const settings = <AgentSettings key={`${profile.profile.ref.profile_id}:${view}`} workspaceId={workspaceId} profile={profile} runtimes={runtimes} descriptors={descriptors} onSaved={onSaved} capability={view} resources={[...combined.values()]} resourcesPending={resources.isPending && view !== "instructions" || pins.some(pin => pin.isPending)} resourcesError={resources.isError || pins.some(pin => pin.isError)} />;
  return <section className="agent-capabilities" aria-label="Profile 能力">
    <nav className="agent-detail-tabs" aria-label="能力类型">{VIEWS.map(item => <Link key={item.key} to={`?view=capabilities&capability=${item.key}`} aria-current={view === item.key && !resourceMode ? "page" : undefined}>{item.name}</Link>)}</nav>
    {view !== "instructions" ? <div className="agent-capability-support"><strong>{support?.status === "supported" ? "运行环境声明支持" : support?.status === "unsupported" ? "运行环境不支持" : "能力支持未确认"}</strong><span className="agent-row-meta">{support?.reason_code ?? "capability_not_reported"}</span></div> : null}
    {validation.isError ? <p role="alert">结构校验不可用</p> : validation.data?.issues.length ? <ul className="agent-issues">{validation.data.issues.map((issue, index) => <li key={index}>{issue.field} · {issue.code}</li>)}</ul> : null}
    {resourceMode ? resourceId === "new" ? <ResourceEditor key={`new:${kind}`} workspaceId={workspaceId} kind={kind} resource={null} onSaved={onResourceSaved} />
      : !validRevision ? <p role="alert">资源版本无效</p> : edited.isPending ? <p role="status">正在读取资源</p> : edited.isError || edited.data.resource.ref.workspace_id !== workspaceId || edited.data.resource.ref.resource_id !== resourceId || edited.data.resource.draft.kind !== kind ? <p role="alert">资源不可用</p>
        : <ResourceEditor key={`${resourceId}:${revision ?? "head"}`} workspaceId={workspaceId} resource={edited.data} kind={kind} onSaved={onResourceSaved} />
      : <>{view !== "instructions" ? <Link className="quiet-button" to={`?view=capabilities&capability=${view}&resource=new`}><Plus size={14} />新建{VIEWS.find(item => item.key === view)?.name}资源</Link> : null}{settings}</>}
  </section>;
}
