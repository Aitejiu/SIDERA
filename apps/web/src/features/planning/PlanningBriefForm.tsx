import { Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiProblem } from "@/shared/api/client";

import {
  buildSubmitPlanningRequestCommand,
  listPlanningProfiles,
  planningProfilesQueryKey,
  submitPlanningRequest,
} from "./api";
import type { PlanningProfile, SubmitPlanningRequestPayload } from "./types";

export function PlanningBriefForm() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const resolvedWorkspaceId = workspaceId ?? "";
  const navigate = useNavigate();
  const [objective, setObjective] = useState("");
  const [constraints, setConstraints] = useState([""]);
  const [acceptanceCriteria, setAcceptanceCriteria] = useState([""]);
  const [requestedOutputs, setRequestedOutputs] = useState(["任务计划"]);
  const [contextRefs, setContextRefs] = useState([""]);
  const [profileId, setProfileId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [validation, setValidation] = useState<ValidationErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);

  const profilesQuery = useQuery({
    queryKey: planningProfilesQueryKey(resolvedWorkspaceId),
    queryFn: ({ signal }) => listPlanningProfiles(resolvedWorkspaceId, signal),
    enabled: resolvedWorkspaceId.length > 0,
  });
  const profiles = profilesQuery.data?.items.filter((profile) => profile.user_selectable) ?? [];
  const selectedProfile = profiles.find((profile) => profile.id === profileId)
    ?? profiles.find((profile) => (
      profile.capabilities.proposal_schema_versions.length === 1
      && profile.capabilities.proposal_schema_versions[0] === 2
    ))
    ?? profiles.find((profile) => profile.capabilities.proposal_schema_versions.length === 1)
    ?? profiles[0];

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextValidation: ValidationErrors = {};
    const objectiveValue = objective.trim();
    const acceptanceValues = compact(acceptanceCriteria);
    const outputValues = compact(requestedOutputs);
    if (objectiveValue.length === 0) {
      nextValidation.objective = "请填写需求目标";
    }
    if (acceptanceValues.length === 0) {
      nextValidation.acceptanceCriteria = "至少填写一条验收条件";
    }
    if (outputValues.length === 0) {
      nextValidation.requestedOutputs = "至少填写一种期望产物";
    }
    if (selectedProfile === undefined) {
      nextValidation.profile = "当前工作区没有可用的规划配置";
    }
    setValidation(nextValidation);
    setSubmitError(null);
    if (Object.keys(nextValidation).length > 0 || selectedProfile === undefined) {
      return;
    }

    const payload: SubmitPlanningRequestPayload = {
      profile_id: selectedProfile.id,
      profile_version: selectedProfile.version,
      objective: objectiveValue,
      constraints: compact(constraints),
      acceptance_criteria: acceptanceValues,
      context_refs: compact(contextRefs),
      requested_outputs: outputValues,
      source_type: "web",
      source_ref: null,
    };

    setSubmitting(true);
    try {
      const response = await submitPlanningRequest(
        buildSubmitPlanningRequestCommand({
          workspaceId: resolvedWorkspaceId,
          payload,
        }),
      );
      const requestId = readPlanningRequestId(response.data);
      if (requestId === null) {
        throw new Error("响应缺少规划请求编号");
      }
      navigate(`/workspaces/${resolvedWorkspaceId}/planning/${requestId}`);
    } catch (error) {
      setSubmitError(readErrorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="planning-brief-page" aria-labelledby="planning-brief-title">
      <div className="breadcrumb">
        <Link to={`/workspaces/${resolvedWorkspaceId}/tasks`}>工作区 / 任务</Link> / 新建任务
      </div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">PLANNING REQUEST</span>
          <h1 id="planning-brief-title">新建任务</h1>
        </div>
        <span className="mono-label">先说清目标，再生成执行方案</span>
      </div>

      <form className="planning-brief-form" onSubmit={handleSubmit} noValidate>
        <div className="planning-brief-form__main">
          <FormSection
            title="任务目标"
            description="描述希望最终交付的结果。"
          >
            <label className="planning-field planning-field--wide">
              <span>需求目标</span>
              <textarea
                value={objective}
                onChange={(event) => setObjective(event.target.value)}
                rows={5}
                placeholder="例如：为现有控制面增加可恢复的规划请求流程"
                aria-invalid={validation.objective !== undefined}
              />
            </label>
            {validation.objective ? <FieldError>{validation.objective}</FieldError> : null}
          </FormSection>

          <div className="planning-form-columns">
            <ListField
              label="约束"
              values={constraints}
              setValues={setConstraints}
              addLabel="添加约束"
              removeLabel="删除约束"
              placeholder="例如：保持已有 worker 上下文可恢复"
            />
            <ListField
              label="验收条件"
              values={acceptanceCriteria}
              setValues={setAcceptanceCriteria}
              addLabel="添加验收条件"
              removeLabel="删除验收条件"
              placeholder="例如：用户能够看到每个子任务的真实状态"
              error={validation.acceptanceCriteria}
            />
          </div>

          <div className="planning-form-columns">
            <ListField
              label="期望产物"
              values={requestedOutputs}
              setValues={setRequestedOutputs}
              addLabel="添加期望产物"
              removeLabel="删除期望产物"
              placeholder="例如：可执行的任务计划"
              error={validation.requestedOutputs}
            />
            <ListField
              label="上下文引用"
              values={contextRefs}
              setValues={setContextRefs}
              addLabel="添加上下文引用"
              removeLabel="删除上下文引用"
              placeholder="例如：docs/architecture.md 或一个 URL"
            />
          </div>
        </div>

        <aside className="planning-brief-form__aside" aria-label="规划配置">
          <FormSection title="规划配置" description="选择本次需求使用的规划能力。">
            {profilesQuery.isPending ? (
              <p className="planning-form-note">正在读取可用配置…</p>
            ) : profilesQuery.isError ? (
              <p className="planning-form-error" role="alert">无法读取规划配置，请稍后重试。</p>
            ) : (
              <>
                <label className="planning-field">
                  <span>规划配置</span>
                  <select
                    value={selectedProfile?.id ?? ""}
                    onChange={(event) => setProfileId(event.target.value)}
                    disabled={profiles.length === 0}
                    aria-invalid={validation.profile !== undefined}
                  >
                    {profiles.map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profile.display_name}
                      </option>
                    ))}
                  </select>
                </label>
                {selectedProfile ? <ProfileSummary profile={selectedProfile} /> : null}
                {validation.profile ? <FieldError>{validation.profile}</FieldError> : null}
              </>
            )}
          </FormSection>
          <div className="planning-brief-form__submit">
            {submitError ? <p className="planning-form-error" role="alert">{submitError}</p> : null}
            <button
              className="quiet-button planning-submit"
              type="submit"
              disabled={submitting || profilesQuery.isPending}
            >
              {submitting ? "正在提交" : profilesQuery.isPending ? "正在读取配置" : "提交需求"}
            </button>
          </div>
        </aside>
      </form>
    </section>
  );
}

function FormSection({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="planning-form-section">
      <div className="planning-form-section__heading">
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
      {children}
    </section>
  );
}

function ListField({
  label,
  values,
  setValues,
  addLabel,
  removeLabel,
  placeholder,
  error,
}: {
  label: string;
  values: string[];
  setValues: (values: string[]) => void;
  addLabel: string;
  removeLabel: string;
  placeholder: string;
  error?: string | undefined;
}) {
  return (
    <section className="planning-form-section planning-list-field">
      <div className="planning-form-section__heading">
        <h2>{label}</h2>
        <button
          className="icon-button"
          type="button"
          onClick={() => setValues([...values, ""])}
          aria-label={addLabel}
          title={addLabel}
        >
          <Plus size={15} aria-hidden="true" />
        </button>
      </div>
      <div className="planning-list-field__items">
        {values.map((value, index) => (
          <div className="planning-list-field__row" key={`${label}-${index}`}>
            <label className="planning-field">
              <span className="sr-only">{label}</span>
              <input
                aria-label={label}
                value={value}
                onChange={(event) => {
                  const next = [...values];
                  next[index] = event.target.value;
                  setValues(next);
                }}
                placeholder={placeholder}
              />
            </label>
            <button
              className="icon-button planning-list-field__remove"
              type="button"
              onClick={() => {
                if (values.length === 1) {
                  setValues([""]);
                  return;
                }
                setValues(values.filter((_, itemIndex) => itemIndex !== index));
              }}
              aria-label={removeLabel}
              title={removeLabel}
            >
              <Trash2 size={14} aria-hidden="true" />
            </button>
          </div>
        ))}
      </div>
      {error ? <FieldError>{error}</FieldError> : null}
    </section>
  );
}

function ProfileSummary({ profile }: { profile: PlanningProfile }) {
  return (
    <div className="planning-profile-summary">
      <strong>{profile.display_name}</strong>
      <span>{profile.planner_provider} · v{profile.version}</span>
      <span>上下文上限 {formatContextLimit(profile.capabilities.max_context_bytes)}</span>
    </div>
  );
}

function FieldError({ children }: { children: ReactNode }) {
  return <p className="planning-form-error">{children}</p>;
}

type ValidationErrors = {
  objective?: string;
  acceptanceCriteria?: string;
  requestedOutputs?: string;
  profile?: string;
};

function compact(values: readonly string[]): string[] {
  return values.map((value) => value.trim()).filter((value) => value.length > 0);
}

function readPlanningRequestId(data: unknown): string | null {
  if (!isRecord(data) || typeof data.planning_request_id !== "string" || data.planning_request_id.length === 0) {
    return null;
  }
  return data.planning_request_id;
}

function readErrorMessage(error: unknown): string {
  if (error instanceof ApiProblem) {
    return error.detail;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "提交需求失败，请稍后重试。";
}

function formatContextLimit(bytes: number): string {
  if (bytes >= 1_000_000) {
    return `${Math.round(bytes / 1_000_000)} MB`;
  }
  return `${Math.round(bytes / 1_000)} KB`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
