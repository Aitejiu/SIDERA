import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import {
  knowledgeProxyTarget,
  resolveE2eRuntime,
  rewriteKnowledgeProxyPath,
} from "./src/e2eConfig.js";

const { controlPlanePort, knowledgeServicePort, mainAgentPort } = resolveE2eRuntime(process.env);

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
      "/healthz": `http://127.0.0.1:${controlPlanePort}`,
      "/knowledge-api": {
        target: knowledgeProxyTarget(knowledgeServicePort),
        rewrite: rewriteKnowledgeProxyPath,
        configure: (proxy) => {
          const token = process.env.SIDERA_WEB_KNOWLEDGE_TOKEN;
          if (token !== undefined && token.length > 0) {
            proxy.on("proxyReq", (proxyRequest) => {
              proxyRequest.setHeader("Authorization", `Bearer ${token}`);
            });
          }
        },
      },
      "/main-agent": `http://127.0.0.1:${mainAgentPort}`,
    },
  },
  preview: {
    host: "127.0.0.1",
  },
});
