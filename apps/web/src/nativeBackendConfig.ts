import { resolveE2eRuntime, type E2eEnvironment } from "./e2eConfig.js";

type BackendProxy = { target: string; changeOrigin: false };

function nativeTarget(value: string | undefined): string {
  const invalid = () => new Error("SIDERA_WEB_PRODUCT_URL must be an explicit http://127.0.0.1:port root URL");
  if (value === undefined) throw invalid();
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw invalid();
  }
  const port = Number(value.match(/:([0-9]+)\/?$/)?.[1]);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1"
    || url.username !== "" || url.password !== "" || url.search !== "" || url.hash !== ""
    || url.pathname !== "/" || !Number.isInteger(port) || port < 1 || port > 65535
    || ![`http://127.0.0.1:${port}`, `http://127.0.0.1:${port}/`].includes(value)) {
    throw invalid();
  }
  return `http://127.0.0.1:${port}`;
}

export function resolveBackendProxyTargets(environment: E2eEnvironment): Record<string, BackendProxy> {
  const mode = environment.SIDERA_WEB_BACKEND ?? "legacy";
  if (mode !== "legacy" && mode !== "native") throw new Error("SIDERA_WEB_BACKEND must be legacy or native");
  if (mode === "native") {
    const target = nativeTarget(environment.SIDERA_WEB_PRODUCT_URL);
    return Object.fromEntries(["/api", "/healthz", "/main-agent", "/product", "/agents", "/model-config"]
      .map((prefix) => [prefix, { target, changeOrigin: false }]));
  }
  const { controlPlanePort, mainAgentPort } = resolveE2eRuntime(environment);
  return {
    "/api": { target: `http://127.0.0.1:${controlPlanePort}`, changeOrigin: false },
    "/healthz": { target: `http://127.0.0.1:${controlPlanePort}`, changeOrigin: false },
    "/main-agent": { target: `http://127.0.0.1:${mainAgentPort}`, changeOrigin: false },
  };
}
