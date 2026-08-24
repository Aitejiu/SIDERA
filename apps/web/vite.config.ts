import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { resolveE2eRuntime } from "./src/e2eConfig.js";

const { controlPlanePort } = resolveE2eRuntime(process.env);

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": `http://127.0.0.1:${controlPlanePort}`,
      "/main-agent": "http://127.0.0.1:8010",
    },
  },
  preview: {
    host: "127.0.0.1",
  },
});
