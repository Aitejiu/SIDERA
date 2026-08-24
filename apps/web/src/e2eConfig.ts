export type E2eEnvironment = Readonly<Record<string, string | undefined>>;

export type E2eRuntime = {
  controlPlanePort: number;
  webPort: number;
};

export function resolveE2eRuntime(environment: E2eEnvironment): E2eRuntime {
  return {
    controlPlanePort: parsePort(environment.SIDERA_E2E_CONTROL_PLANE_PORT, 8000),
    webPort: parsePort(environment.SIDERA_E2E_WEB_PORT, 4173),
  };
}

function parsePort(value: string | undefined, fallback: number): number {
  if (value === undefined || value.trim() === "") return fallback;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`E2E port must be an integer between 1 and 65535: ${value}`);
  }
  return port;
}
