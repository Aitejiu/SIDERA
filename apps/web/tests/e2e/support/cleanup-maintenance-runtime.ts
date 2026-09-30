import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

export default function cleanupMaintenanceRuntime(): void {
  const manifestPath = process.env.SIDERA_BASIC_MAINTENANCE_MANIFEST;
  if (manifestPath === undefined || manifestPath.trim() === "") {
    return;
  }

  const webRoot = resolve(import.meta.dirname, "../../..");
  const repositoryRoot = resolve(webRoot, "../..");
  const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
  const python = resolve(controlPlaneRoot, ".venv/bin/python");
  const maintenanceScript = resolve(
    controlPlaneRoot,
    "tests/e2e/maintenance_readonly_runtime.py",
  );
  const environment: NodeJS.ProcessEnv = {
    PATH: process.env.PATH ?? "/opt/homebrew/bin:/usr/bin:/bin",
    LANG: process.env.LANG ?? "C",
    LC_ALL: process.env.LC_ALL ?? "C",
    TMPDIR: process.env.TMPDIR,
    PGPASSFILE: process.env.PGPASSFILE,
    SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL:
      process.env.SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL,
  };

  execFileSync(
    python,
    [maintenanceScript, "cleanup", "--manifest", manifestPath],
    { cwd: controlPlaneRoot, env: environment, stdio: "ignore" },
  );
}
