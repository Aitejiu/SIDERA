import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Save } from "lucide-react";
import { useRef, useState } from "react";

import { ConfigurationNavigationGuard } from "./AgentSettings";
import { AgentApiProblem, readDefaults, writeDefaults } from "./api";
import type { Defaults, ProfileView } from "./types";

const roles = ["main_profile_id", "planner_profile_id", "worker_profile_id"] as const;
const labels = { main_profile_id: "Main", planner_profile_id: "Planner", worker_profile_id: "Worker" };
type Selection = Pick<Defaults, typeof roles[number]>;
const selection = (value: Defaults): Selection => ({ main_profile_id: value.main_profile_id, planner_profile_id: value.planner_profile_id, worker_profile_id: value.worker_profile_id });

function valid(value: Defaults, workspaceId: string): boolean {
  return value.workspace_id === workspaceId && Number.isInteger(value.version) && value.version >= 0
    && roles.every(role => value[role] === null || (typeof value[role] === "string" && value[role].length > 0));
}

export function DefaultRoleControls({ workspaceId, profiles, metadataUnavailable = false }: { workspaceId: string; profiles: ProfileView[]; metadataUnavailable?: boolean }) {
  const query = useQuery({ queryKey: ["agent-defaults", workspaceId], queryFn: async () => {
    const value = await readDefaults(workspaceId);
    if (!valid(value, workspaceId)) throw new AgentApiProblem(0, "defaults_mismatch");
    return value;
  } });
  if (query.isPending) return <p role="status">正在读取默认安排</p>;
  if (query.isError) return <p role="alert">默认安排不可用</p>;
  return <DefaultsEditor key={workspaceId} workspaceId={workspaceId} initial={query.data} profiles={profiles} metadataUnavailable={metadataUnavailable} />;
}

function DefaultsEditor({ workspaceId, initial, profiles, metadataUnavailable }: { workspaceId: string; initial: Defaults; profiles: ProfileView[]; metadataUnavailable: boolean }) {
  const [baseline, setBaseline] = useState(initial), [draft, setDraft] = useState(selection(initial));
  const [pending, setPending] = useState(false), [unsettled, setUnsettled] = useState(false), [conflicted, setConflicted] = useState(false);
  const [observed, setObserved] = useState<Defaults | null>(null), [error, setError] = useState<string | null>(null), [saved, setSaved] = useState(false);
  const submitting = useRef(false);
  const choices = profiles.filter(item => item.profile.ref.workspace_id === workspaceId && item.status.enabled);
  const dirty = roles.some(role => draft[role] !== baseline[role]);
  async function save() {
    if (submitting.current || unsettled || conflicted || metadataUnavailable || !dirty) return;
    const body = { ...draft, expected_version: baseline.version };
    submitting.current = true; setPending(true); setUnsettled(true); setError(null); setSaved(false); setObserved(null);
    try {
      const next = await writeDefaults(workspaceId, body);
      if (!valid(next, workspaceId) || next.version !== body.expected_version + 1 || roles.some(role => next[role] !== body[role])) throw new AgentApiProblem(0, "defaults_mismatch");
      setBaseline(next); setDraft(selection(next)); setUnsettled(false); setSaved(true);
    } catch (failure) {
      if (failure instanceof AgentApiProblem && [403, 409, 422].includes(failure.status)) {
        setUnsettled(false); setConflicted(failure.status === 409);
        setError(failure.status === 409 ? "默认安排版本已变化" : "默认安排保存被拒绝");
      } else setError("默认安排保存结果未确认");
    } finally { submitting.current = false; setPending(false); }
  }
  async function observe() {
    if (submitting.current) return;
    submitting.current = true; setPending(true);
    try {
      const next = await readDefaults(workspaceId);
      if (!valid(next, workspaceId)) throw new AgentApiProblem(0, "defaults_mismatch");
      setObserved(next);
    } catch { setError("默认安排当前状态不可用"); }
    finally { submitting.current = false; setPending(false); }
  }
  return <section className="agent-defaults" aria-labelledby="agent-defaults-title">
    <ConfigurationNavigationGuard dirty={dirty} unsettled={unsettled} />
    <h2 id="agent-defaults-title">默认安排 · V{baseline.version}</h2>
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset className="agent-form-fields" disabled={pending || unsettled || conflicted || metadataUnavailable}>
        {roles.map(role => <label key={role}>默认 {labels[role]}<select aria-label={`默认 ${labels[role]}`} value={draft[role] ?? ""} onChange={event => { setDraft({ ...draft, [role]: event.target.value || null }); setSaved(false); }}>
          <option value="">未设置</option>
          {draft[role] !== null && !choices.some(item => item.profile.ref.profile_id === draft[role]) ? <option value={draft[role]} disabled>当前选择不可用 · {draft[role]}</option> : null}
          {choices.map(item => <option key={item.profile.ref.profile_id} value={item.profile.ref.profile_id}>{item.profile.draft.name} · R{item.profile.ref.revision}</option>)}
        </select></label>)}
        <button className="quiet-button" type="submit" disabled={!dirty}><Save size={14} />保存默认安排</button>
      </fieldset>
    </form>
    {metadataUnavailable ? <p role="alert">Profile 目录不可用</p> : null}
    {saved ? <p role="status">默认安排已保存 · 仅用于新安排</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {unsettled || conflicted ? <button className="quiet-button" disabled={pending} onClick={() => void observe()}><RefreshCw size={14} />读取默认安排当前状态</button> : null}
    {observed ? <div className="agent-head-review"><p>当前默认安排 · V{observed.version}</p><button className="quiet-button" disabled={pending} onClick={() => { setBaseline(observed); setDraft(selection(observed)); setObserved(null); setUnsettled(false); setConflicted(false); setError(null); setSaved(false); }}>采用当前默认安排</button></div> : null}
  </section>;
}
