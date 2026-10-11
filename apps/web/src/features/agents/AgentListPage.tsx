import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, Search, ServerCog, Users } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { LocalSessionButton } from "@/features/local-session/LocalSessionDialog";
import { AgentApiProblem, listProfiles, listProfileWork, listRuntimes, validateProfile } from "./api";
import { DefaultRoleControls } from "./DefaultRoleControls";
import type { ProfileView, RuntimeSnapshot } from "./types";

export function AgentListPage() {
  const { workspaceId = "" } = useParams<{ workspaceId: string }>();
  const client = useQueryClient();
  const profiles = useQuery({ queryKey: ["agent-profiles", workspaceId], queryFn: ({ signal }) => listProfiles(workspaceId, signal) });
  const runtimes = useQuery({ queryKey: ["agent-runtimes", workspaceId], queryFn: () => listRuntimes(workspaceId) });
  const [search, setSearch] = useState("");
  const [enabled, setEnabled] = useState("all");
  const [showDefaults, setShowDefaults] = useState(false);
  const items = profiles.data?.items.filter(item => (enabled === "all" || item.status.enabled === (enabled === "enabled"))
    && `${item.profile.draft.name} ${item.profile.draft.description}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())) ?? [];

  return <section className="agent-page" aria-labelledby="agents-title">
    <div className="page-heading agent-page-heading"><h1 id="agents-title">智能体</h1><div className="agent-page-actions">
      <LocalSessionButton workspaceId={workspaceId} onAuthorized={() => { void client.invalidateQueries({ queryKey: ["agent-profiles", workspaceId] }); void client.invalidateQueries({ queryKey: ["agent-runtimes", workspaceId] }); }} />
      <button className="quiet-button" disabled={showDefaults} onClick={() => setShowDefaults(true)}><Users size={14} />默认安排</button>
      <Link className="quiet-button" to={`/workspaces/${workspaceId}/runtimes`}><ServerCog size={14} />运行环境</Link>
      <Link className="quiet-button" to={`/workspaces/${workspaceId}/agents/new`}><Plus size={14} />新建 Profile</Link>
    </div></div>
    {showDefaults ? <DefaultRoleControls workspaceId={workspaceId} profiles={profiles.data?.items ?? []} metadataUnavailable={profiles.isError || profiles.isPending} /> : null}
    <div className="agent-list-controls"><label className="agent-search"><Search size={14} aria-hidden="true" /><input type="search" aria-label="搜索 Profile" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <label>启用状态<select aria-label="启用状态" value={enabled} onChange={event => setEnabled(event.target.value)}><option value="all">全部</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></label>
      <button className="icon-button" title="刷新目录" aria-label="刷新目录" disabled={profiles.isFetching || runtimes.isFetching} onClick={() => { void profiles.refetch(); void runtimes.refetch(); }}><RefreshCw size={14} /></button>
    </div>
    {profiles.isPending ? <p role="status">正在读取 Profile 目录</p> : profiles.isError ? <p role="alert">{profiles.error instanceof AgentApiProblem && profiles.error.code === "local_session_required" ? "需要本地会话" : profiles.error instanceof AgentApiProblem && profiles.error.code === "permission_denied" ? "当前会话无权读取此工作区" : "Profile 目录不可用"}</p>
      : profiles.data.items.length === 0 ? <p className="agent-empty">尚无 Profile</p>
        : items.length === 0 ? <p className="agent-empty">没有匹配的 Profile</p>
          : <div className="agent-table-wrap"><table className="agent-table"><thead><tr><th>Profile</th><th>运行环境</th><th>配置状态</th><th>最近工作</th></tr></thead><tbody>{items.map(item => <ProfileRow key={item.profile.ref.profile_id} item={item} runtime={runtimes.data?.items.find(runtime => runtime.ref.runtime_id === item.profile.draft.runtime_ref.runtime_id)} runtimePending={runtimes.isPending} runtimeUnavailable={runtimes.isError} />)}</tbody></table></div>}
  </section>;
}

function ProfileRow({ item, runtime, runtimePending, runtimeUnavailable }: { item: ProfileView; runtime: RuntimeSnapshot | undefined; runtimePending: boolean; runtimeUnavailable: boolean }) {
  const { profile, status } = item;
  const validation = useQuery({ queryKey: ["agent-validation", profile.ref], queryFn: () => validateProfile(profile.ref) });
  const work = useQuery({ queryKey: ["agent-work", profile.ref.workspace_id, profile.ref.profile_id, null], queryFn: () => listProfileWork(profile.ref.workspace_id, profile.ref.profile_id) });
  const latest = [...(work.data?.task_work.items ?? []), ...(work.data?.conversations ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
  return <tr>
    <td><Link to={`/workspaces/${profile.ref.workspace_id}/agents/${profile.ref.profile_id}`}>{profile.draft.name}</Link><span className="agent-row-meta">R{profile.ref.revision} · {status.enabled ? "已启用" : "已停用"}</span><span className="agent-row-description">{profile.draft.description}</span></td>
    <td>{runtime ? `${runtime.harness_id} · ${runtime.actual_version}` : runtimePending ? "正在读取运行环境" : runtimeUnavailable ? "运行环境不可用" : "运行环境未找到"}</td>
    <td>{validation.isPending ? "正在确认配置" : validation.isError ? "配置状态未确认" : status.enabled && validation.data.valid ? "配置可用" : "配置不可用"}</td>
    <td>{work.isPending ? "正在读取工作" : work.isError ? "工作记录不可用" : latest ? <><span>{latest.title}</span><time className="agent-row-meta" dateTime={latest.updated_at}>{new Date(latest.updated_at).toLocaleString()}</time></> : "暂无工作记录"}</td>
  </tr>;
}
