import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Save, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { ApiProblem } from "@/shared/api/client";

import {
  buildConfigureWorkerRuntimeCommand,
  configureWorkerRuntime,
  listModules,
  modulesQueryKey,
  workerQueryKey,
  workersQueryKey,
} from "./api";
import type { ModuleSummary, Worker } from "./types";

export function WorkerRuntimeDialog({
  workspaceId,
  worker,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  worker: Worker;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}) {
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLDivElement>(null);
  const [adapterModuleId, setAdapterModuleId] = useState(worker.adapter_module_summary?.id ?? "");
  const [workspaceModuleId, setWorkspaceModuleId] = useState(worker.workspace_module_summary?.id ?? "");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const adapterQuery = useQuery({
    queryKey: modulesQueryKey(workspaceId, "worker_adapter"),
    queryFn: ({ signal }) => listModules(workspaceId, "worker_adapter", signal),
  });
  const workspaceQuery = useQuery({
    queryKey: modulesQueryKey(workspaceId, "workspace_provider"),
    queryFn: ({ signal }) => listModules(workspaceId, "workspace_provider", signal),
  });
  const adapterModules = (adapterQuery.data ?? []).filter(
    (module) => isActiveModule(module) && module.provider === worker.adapter_type,
  );
  const workspaceModules = (workspaceQuery.data ?? []).filter(isActiveModule);
  const mutation = useMutation({
    mutationFn: async () => {
      const adapterModule = adapterModules.find((module) => module.id === adapterModuleId);
      const workspaceModule = workspaceModules.find((module) => module.id === workspaceModuleId);
      if (!adapterModule || !workspaceModule) {
        throw new Error("请选择可用的 Adapter 和工作区提供器");
      }
      return configureWorkerRuntime(
        buildConfigureWorkerRuntimeCommand({
          workspaceId,
          workerId: worker.id,
          expectedVersion: worker.runtime_configuration_version,
          payload: {
            adapter_module_instance_id: adapterModule.id,
            adapter_module_config_version: adapterModule.config_version,
            workspace_module_instance_id: workspaceModule.id,
            workspace_module_config_version: workspaceModule.config_version,
          },
        }),
      );
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: workersQueryKey(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: workerQueryKey(workspaceId, worker.id) }),
        queryClient.invalidateQueries({ queryKey: modulesQueryKey(workspaceId, "worker_adapter") }),
        queryClient.invalidateQueries({ queryKey: modulesQueryKey(workspaceId, "workspace_provider") }),
      ]);
      await onSaved();
    },
    onError: (cause) => {
      if (cause instanceof ApiProblem && cause.status === 409) {
        setErrorMessage("配置已变化，请刷新后重新确认");
        return;
      }
      setErrorMessage(cause instanceof Error ? cause.message : "保存运行配置失败，请重试");
    },
  });

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = () => Array.from(
      dialog.querySelectorAll<HTMLElement>(
        "button:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex='-1'])",
      ),
    );
    focusable()[0]?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    dialog.addEventListener("keydown", handleKeyDown);
    return () => dialog.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const isLoading = adapterQuery.isPending || workspaceQuery.isPending;
  const modulesError = adapterQuery.isError || workspaceQuery.isError;
  return (
    <div className="worker-runtime-dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <div
        className="worker-runtime-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="worker-runtime-dialog-title"
        ref={dialogRef}
      >
        <div className="worker-runtime-dialog__heading">
          <div>
            <span className="eyebrow">NEW EXECUTIONS ONLY</span>
            <h2 id="worker-runtime-dialog-title">调整运行配置</h2>
            <p>{worker.display_name} · {worker.adapter_type}</p>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭运行配置" title="关闭运行配置">
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <p className="worker-runtime-dialog__notice">
          该修改只影响之后分配的新执行；当前执行继续使用原绑定配置。
        </p>
        {errorMessage ? (
          <div className="worker-runtime-dialog__error" role="alert">
            <AlertCircle size={15} aria-hidden="true" />
            <span>{errorMessage}</span>
          </div>
        ) : null}
        {isLoading ? <p className="worker-runtime-dialog__state">正在读取可用模块</p> : null}
        {modulesError ? <p className="worker-runtime-dialog__state worker-runtime-dialog__state--error">无法读取可用模块，请重试。</p> : null}
        {!isLoading && !modulesError ? (
          <form onSubmit={(event) => { event.preventDefault(); setErrorMessage(null); mutation.mutate(); }}>
            <div className="worker-runtime-dialog__fields">
              <label className="worker-runtime-field">
                <span>Adapter 模块</span>
                <select
                  aria-label="Adapter 模块"
                  value={adapterModuleId}
                  onChange={(event) => setAdapterModuleId(event.target.value)}
                  disabled={mutation.isPending}
                >
                  <option value="">选择 Adapter 模块</option>
                  {adapterModules.map((module) => <ModuleOption key={module.id} module={module} />)}
                </select>
                <small>仅显示 provider 与节点 adapter type 一致的 active 实例。</small>
              </label>
              <label className="worker-runtime-field">
                <span>工作区提供器</span>
                <select
                  aria-label="工作区提供器"
                  value={workspaceModuleId}
                  onChange={(event) => setWorkspaceModuleId(event.target.value)}
                  disabled={mutation.isPending}
                >
                  <option value="">选择工作区提供器</option>
                  {workspaceModules.map((module) => <ModuleOption key={module.id} module={module} />)}
                </select>
                <small>只允许 Definition 与 Instance 都为 active 的实例。</small>
              </label>
            </div>
            <div className="worker-runtime-dialog__actions">
              <button className="quiet-button" type="button" onClick={onClose} disabled={mutation.isPending}>取消</button>
              <button className="quiet-button worker-runtime-dialog__save" type="submit" disabled={mutation.isPending || adapterModules.length === 0 || workspaceModules.length === 0}>
                <Save size={14} aria-hidden="true" />
                {mutation.isPending ? "保存中" : "保存运行配置"}
              </button>
            </div>
          </form>
        ) : null}
      </div>
    </div>
  );
}

function ModuleOption({ module }: { module: ModuleSummary }) {
  return <option value={module.id}>{module.instance_name} · {module.provider} · V{module.config_version}</option>;
}

function isActiveModule(module: ModuleSummary): boolean {
  return module.definition_lifecycle === "active" && module.instance_lifecycle === "active";
}
