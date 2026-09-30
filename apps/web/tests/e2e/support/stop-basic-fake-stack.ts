import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

export default function stopBasicFakeStack(): void {
  const manifestPath = process.env.SIDERA_BASIC_FAKE_MANIFEST;
  if (manifestPath === undefined || manifestPath.trim() === "") {
    return;
  }

  const webRoot = resolve(import.meta.dirname, "../../..");
  const repositoryRoot = resolve(webRoot, "../..");
  const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
  const python = resolve(controlPlaneRoot, ".venv/bin/python");
  const environment: NodeJS.ProcessEnv = {
    PATH: process.env.PATH ?? "/opt/homebrew/bin:/usr/bin:/bin",
    LANG: process.env.LANG ?? "C",
    LC_ALL: process.env.LC_ALL ?? "C",
    TMPDIR: process.env.TMPDIR,
    PGPASSFILE: process.env.PGPASSFILE,
  };

  execFileSync(
    python,
    [
      "-m",
      "sidera_control_plane.demo.cli",
      "stop",
      "--manifest",
      manifestPath,
    ],
    {
      cwd: controlPlaneRoot,
      env: environment,
      stdio: "ignore",
    },
  );
}
