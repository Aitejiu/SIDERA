import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Eye, MessageSquare, Play, RefreshCw } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";

import { useWorkspaceTabs } from "@/app/WorkspaceTabs";
import { LocalSessionButton } from "@/features/local-session/LocalSessionDialog";
import { AgentCapabilities } from "./AgentCapabilities";
import { AgentSettings } from "./AgentSettings";
import { ProfileTestDialog } from "./ProfileTestDialog";
import { listDescriptors, listProfileWork, listRuntimes, readProfile, readWorkRun, validateProfile } from "./api";
import type { ProfileView, ProfileWorkRow, RuntimeSnapshot } from "./types";

export function AgentDetailPage() {
  const { workspaceId = "", profileId = "" } = useParams<{ workspaceId: string; profileId: string }>();
  const [params] = useSearchParams();
  const creating = profileId === "new";
  const active = creating ? "settings" : params.get("view") ?? "overview";
  const client = useQueryClient();
  const tabs = useWorkspaceTabs();
  const [createdProfile, setCreatedProfile] = useState<ProfileView | null>(null);
  const [editorRoute, setEditorRoute] = useState({ profileId, key: 0 });
  if (editorRoute.profileId !== profileId) {
    const promoted = editorRoute.profileId === "new"
      && createdProfile?.profile.ref.profile_id === profileId;
    setEditorRoute({ profileId, key: editorRoute.key + (promoted ? 0 : 1) });
  }
  const completedCreateId = useRef<string | null>(null);
  const { open: openTab, close: closeTab, state: tabState } = tabs;
  const register = tabs.registerDirectRoute;
  const location = useLocation();
  const href = location.pathname + location.search;
  const recovery = tabs.state.tabs.find(tab => tab.id === `agent:${profileId}`)?.configurationRecovery;
  const profile = useQuery({ queryKey: ["agent-profile", workspaceId, profileId], queryFn: ({ signal }) => readProfile(workspaceId, profileId, undefined, signal), enabled: !creating });
  const runtimes = useQuery({ queryKey: ["agent-runtimes", workspaceId], queryFn: () => listRuntimes(workspaceId) });
  const descriptors = useQuery({ queryKey: ["agent-descriptors", workspaceId], queryFn: () => listDescriptors(workspaceId) });
  const onSaved = useCallback((value: ProfileView) => {
    client.setQueryData(["agent-profile", workspaceId, value.profile.ref.profile_id], value);
    void client.invalidateQueries({ queryKey: ["agent-profiles", workspaceId] });
    if (creating) setCreatedProfile(value);
  }, [client, workspaceId, creating]);
  useEffect(() => {
    if (createdProfile === null
      || completedCreateId.current === createdProfile.profile.ref.profile_id) return;
    const value = createdProfile;
    const id = `agent:${value.profile.ref.profile_id}` as const;
    if (creating) {
      openTab({
        id,
        kind: "agent",
        title: value.profile.draft.name,
        description: value.profile.draft.description,
        href: `/workspaces/${workspaceId}/agents/${value.profile.ref.profile_id}?view=settings`,
        closeable: true,
      });
    } else if (profileId === value.profile.ref.profile_id && tabState.activeTabId === id) {
      closeTab("agent:new");
      completedCreateId.current = value.profile.ref.profile_id;
    }
  }, [createdProfile, creating, profileId, workspaceId, openTab, closeTab, tabState.activeTabId]);
  const title = profile.data?.profile.draft.name;
  const description = profile.data?.profile.draft.description;
  useEffect(() => {
    if (title === undefined) return;
    register({ id: `agent:${profileId}`, kind: "agent", title, ...(description === undefined ? {} : { description }), href, closeable: true,
      ...(recovery ? { configurationRecovery: recovery } : {}) });
  }, [register, profileId, href, title, description, recovery]);

  const metadataPending = runtimes.isPending || descriptors.isPending;
  const metadataError = runtimes.isError || descriptors.isError;
  const runtime = runtimes.data?.items.find(item => item.ref.runtime_id === profile.data?.profile.draft.runtime_ref.runtime_id);
  return <section className="agent-page" aria-labelledby="profile-title">
    <div className="breadcrumb"><Link to={`/workspaces/${workspaceId}/agents`}><ArrowLeft size={13} />智能体</Link></div>
    <div className="page-heading agent-page-heading"><div><h1 id="profile-title">{creating ? "新建 Profile" : title ?? "Profile"}</h1>{profile.data ? <p className="agent-identity"><span className="mono-label">{profile.data.profile.ref.profile_id}</span><strong>R{profile.data.profile.ref.revision}</strong><span>{profile.data.status.enabled ? "已启用" : "已停用"}</span></p> : null}</div>
      <LocalSessionButton workspaceId={workspaceId} onAuthorized={() => { void profile.refetch(); void runtimes.refetch(); void descriptors.refetch(); }} />
    </div>
    {!creating ? <nav className="agent-detail-tabs" aria-label="Profile 视图">{[["overview", "概览"], ["work", "工作"], ["capabilities", "能力"], ["settings", "设置"]].map(([key, label]) => <Link key={key} to={`?view=${key}`} aria-current={active === key ? "page" : undefined}>{label}</Link>)}</nav> : null}
    {!creating && profile.isPending ? <p role="status">正在读取 Profile</p> : !creating && profile.isError ? <p role="alert">Profile 不可用</p>
      : active === "settings" ? metadataPending ? <p role="status">正在读取配置元数据</p> : metadataError ? <p role="alert">配置元数据不可用</p> : <AgentSettings key={editorRoute.key} workspaceId={workspaceId} profile={creating ? null : profile.data ?? null} runtimes={runtimes.data.items} descriptors={descriptors.data} onSaved={onSaved} />
        : profile.data ? active === "work" ? <ProfileWork workspaceId={workspaceId} profileId={profileId} />
          : active === "capabilities" ? metadataPending ? <p role="status">正在读取配置元数据</p> : metadataError ? <p role="alert">配置元数据不可用</p> : <AgentCapabilities workspaceId={workspaceId} profile={profile.data} runtimes={runtimes.data.items} descriptors={descriptors.data} onSaved={onSaved} />
            : <ProfileOverview profile={profile.data} runtime={runtime} runtimeUnavailable={runtimes.isError} /> : null}
  </section>;
}

function ProfileOverview({ profile, runtime, runtimeUnavailable }: { profile: ProfileView; runtime: RuntimeSnapshot | undefined; runtimeUnavailable: boolean }) {
  const queryClient = useQueryClient();
  const validation = useQuery({ queryKey: ["agent-validation", profile.profile.ref], queryFn: () => validateProfile(profile.profile.ref) });
  const [test, setTest] = useState<{ profile: ProfileView; runtime: RuntimeSnapshot | undefined } | null>(null);
  const workspaceId = profile.profile.ref.workspace_id;
  const profileId = profile.profile.ref.profile_id;
  return <section className="agent-overview"><p>{profile.profile.draft.description}</p><dl className="agent-facts">
    <dt>配置状态</dt><dd>{validation.isPending ? "正在确认配置" : validation.isError ? "配置状态未确认" : profile.status.enabled && validation.data.valid ? "配置可用" : "配置不可用"}</dd>
    <dt>运行环境</dt><dd>{runtime ? <Link to={`/workspaces/${runtime.ref.workspace_id}/runtimes/${runtime.ref.runtime_id}`}>{runtime.harness_id} · {runtime.actual_version}</Link> : runtimeUnavailable ? "运行环境不可用" : "运行环境未找到"}</dd>
    <dt>协议</dt><dd>{runtime?.protocol_state === "ready" ? "协议已就绪" : runtime?.protocol_state === "unavailable" ? "协议不可用" : "协议未确认"}</dd>
    <dt>认证</dt><dd>{runtime?.auth_state === "ready" ? "认证已就绪" : runtime?.auth_state === "failed" ? "认证失败" : "认证未确认"}</dd>
    <dt>模型配置</dt><dd>{profile.profile.draft.model_id ?? "继承运行环境默认值"}</dd>
    <dt>并行安排上限</dt><dd>{profile.profile.draft.max_active}</dd>
  </dl>{validation.data?.issues.length ? <ul className="agent-issues">{validation.data.issues.map((issue, index) => <li key={index}>{issue.field} · {issue.code}</li>)}</ul> : null}
    <div className="agent-form-actions"><button className="quiet-button" disabled={!profile.status.enabled || test !== null} onClick={() => setTest({ profile, runtime })}><Play size={14} />测试已保存 Profile</button><Link className="quiet-button" to={`/workspaces/${profile.profile.ref.workspace_id}/chat?${new URLSearchParams({ profile_id: profile.profile.ref.profile_id, profile_revision: String(profile.profile.ref.revision) })}`}><MessageSquare size={14} />使用此 Profile 聊天</Link></div>
    <ProfileRecentRuns workspaceId={workspaceId} profileId={profileId} />
    {test ? <ProfileTestDialog profile={test.profile} runtime={test.runtime} onClose={() => {
      setTest(null);
      void queryClient.invalidateQueries({ queryKey: ["agent-work", workspaceId, profileId, null] });
    }} /> : null}
  </section>;
}

type RecentRunView = {
  runId: string;
  title: string;
  updatedAt: string;
  profileRevision: number;
  stateLabel: string;
  href: string | null;
  links: { href: string; label: string }[];
};

function ProfileRecentRuns({ workspaceId, profileId }: { workspaceId: string; profileId: string }) {
  const work = useQuery({
    queryKey: ["agent-work", workspaceId, profileId, null],
    queryFn: () => listProfileWork(workspaceId, profileId),
  });
  const headingId = `profile-recent-runs-${profileId}`;
  const section = (content: ReactNode) => <section className="agent-work" aria-labelledby={headingId}>
    <div className="agent-section-heading"><h2 id={headingId}>最近运行</h2><Link to="?view=work">全部工作</Link></div>
    {content}
  </section>;

  if (work.isPending) return section(<p role="status">正在读取运行记录</p>);
  if (work.isError || !work.data) return section(<p role="alert">运行记录不可用</p>);

  const taskRuns: RecentRunView[] = work.data.task_work.items
    .filter(item => item.run_id != null && item.workspace_id === workspaceId
      && item.profile_ref.workspace_id === workspaceId && item.profile_ref.profile_id === profileId)
    .map(item => {
      const links = [
        ...(item.worker_item_id ? [{ href: `/workspaces/${workspaceId}/worker-items/${item.worker_item_id}`, label: "打开 WorkerItem" }] : []),
        ...(item.root_task_id ? [{ href: `/workspaces/${workspaceId}/tasks/${item.root_task_id}`, label: "打开任务" }] : []),
        ...(item.planning_request_id ? [{ href: `/workspaces/${workspaceId}/planning/${item.planning_request_id}`, label: "打开规划请求" }] : []),
      ];
      return {
        runId: item.run_id!,
        title: item.title,
        updatedAt: item.updated_at,
        profileRevision: item.profile_ref.revision,
        stateLabel: `任务状态 ${item.status}`,
        href: links[0]?.href ?? null,
        links: links.slice(1),
      };
    });
  const conversationRuns: RecentRunView[] = work.data.conversations
    .filter(item => item.run_id != null && item.profile_ref.workspace_id === workspaceId
      && item.profile_ref.profile_id === profileId)
    .map(item => ({
      runId: item.run_id!,
      title: item.title,
      updatedAt: item.updated_at,
      profileRevision: item.profile_ref.revision,
      stateLabel: `Stop 状态 ${item.stop_state ?? "未确认"}`,
      href: `/workspaces/${workspaceId}/chat/${item.conversation_id}`,
      links: [],
    }));
  const seenRunIds = new Set<string>();
  const recentRuns = [...taskRuns, ...conversationRuns]
    .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))
    .filter(item => {
      if (seenRunIds.has(item.runId)) return false;
      seenRunIds.add(item.runId);
      return true;
    })
    .slice(0, 3);

  return section(recentRuns.length === 0 ? <p>近期暂无运行记录</p> : <div>
    {recentRuns.map(item => <article className="agent-work-row" key={item.runId}>
      <div>
        {item.href ? <Link to={item.href}>{item.title}</Link> : <strong>{item.title}</strong>}
        <span className="agent-row-meta">Profile R{item.profileRevision} · {item.stateLabel}</span>
        {item.links.length ? <div className="agent-work-links">{item.links.map(link => <Link key={link.href} to={link.href}>{link.label}</Link>)}</div> : null}
      </div>
      <div><span className="mono-label">{item.runId}</span><time className="agent-row-meta" dateTime={item.updatedAt}>{new Date(item.updatedAt).toLocaleString()}</time></div>
    </article>)}
  </div>);
}

function ProfileWork({ workspaceId, profileId }: { workspaceId: string; profileId: string }) {
  const [cursor, setCursor] = useState<string | undefined>();
  const work = useQuery({ queryKey: ["agent-work", workspaceId, profileId, cursor ?? null], queryFn: () => listProfileWork(workspaceId, profileId, cursor) });
  if (work.isPending) return <p role="status">正在读取工作记录</p>;
  if (work.isError) return <div role="alert"><p>工作记录不可用</p><button className="quiet-button" onClick={() => void work.refetch()}><RefreshCw size={14} />重试读取</button></div>;
  return <section className="agent-work" aria-label="Profile 工作">
    {work.data.task_work.items.length === 0 && work.data.conversations.length === 0 ? <p>暂无工作记录</p> : null}
    {work.data.task_work.items.map(item => <WorkRow key={`${item.kind}:${item.target_type ?? ""}:${item.work_id}`} item={item} />)}
    {work.data.conversations.map(item => <article className="agent-work-row" key={item.conversation_id}><div><Link to={`/workspaces/${workspaceId}/chat/${item.conversation_id}`}>{item.title}</Link><span className="agent-row-meta">Profile R{item.profile_ref.revision} · {item.stop_state ?? "无 Stop 记录"}</span></div><span className="mono-label">{item.run_id ?? "Run 未确认"}</span></article>)}
    {work.data.task_work.next_cursor ? <button className="quiet-button" onClick={() => setCursor(work.data.task_work.next_cursor ?? undefined)}>下一页工作</button> : null}
  </section>;
}

function WorkRow({ item }: { item: ProfileWorkRow }) {
  const [requested, setRequested] = useState(false);
  const summary = useQuery({ queryKey: ["agent-original-run", item.workspace_id, item.kind, item.target_type, item.work_id], queryFn: () => readWorkRun(item.workspace_id, item.kind, item.work_id, item.kind === "assignment" ? item.target_type ?? undefined : undefined), enabled: requested });
  const reason = summary.data?.reason_code;
  return <article className="agent-work-row"><div><strong>{item.title}</strong><span className="agent-row-meta">Profile R{item.profile_ref.revision}{item.binding_version === null || item.binding_version === undefined ? "" : ` · 绑定 V${item.binding_version}`}</span><span>{item.status}</span><div className="agent-work-links">
    {item.root_task_id ? <Link to={`/workspaces/${item.workspace_id}/tasks/${item.root_task_id}`}>打开任务</Link> : null}
    {item.worker_item_id ? <Link to={`/workspaces/${item.workspace_id}/worker-items/${item.worker_item_id}`}>打开 WorkerItem</Link> : null}
    {item.planning_request_id ? <Link to={`/workspaces/${item.workspace_id}/planning/${item.planning_request_id}`}>打开规划请求</Link> : null}
  </div></div><button className="quiet-button" disabled={summary.isFetching} onClick={() => { if (requested) void summary.refetch(); else setRequested(true); }}><Eye size={14} />读取原 Run</button>
    {requested ? summary.isPending ? <p role="status">正在读取原 Run</p> : summary.isError ? <p role="alert">原 Run 不可用</p>
      : !summary.data.available || !summary.data.run ? <p role="status">{reason === "run_admission_unconfirmed" ? "原 Run 接纳未确认" : reason === "run_not_admitted" ? "尚未接纳 Run" : reason === "ambiguous_run" ? "原 Run 不唯一" : "原 Run 不可用"}</p>
        : <dl className="agent-facts agent-run-facts"><dt>Run</dt><dd className="mono-label">{summary.data.run_id}</dd><dt>状态</dt><dd>{summary.data.run.state}</dd><dt>选定模型</dt><dd>{summary.data.run.selected_model ?? "继承运行环境默认值"}</dd><dt>观察到的模型</dt><dd>{summary.data.run.observed_model ?? "模型未观察到"}</dd><dt>运行环境版本</dt><dd>{summary.data.run.actual_runtime_version}</dd></dl> : null}
  </article>;
}
