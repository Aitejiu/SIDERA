#!/usr/bin/env node

/**
 * A deliberately small DSH process boundary.
 *
 * SIDERA supplies an explicit DSH command, argv, cwd, and environment. This
 * host owns only the child process and forwards ACP JSONL frames; it never
 * imports DSH modules and never inherits ambient provider credentials.
 */

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const MAX_STDERR_BYTES = 64 * 1024;

function required(name) {
  const value = process.env[name];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${name} is required`);
  }
  return value;
}

function parseArgs() {
  const raw = process.env.DSH_ACP_ARGS ?? "[]";
  const value = JSON.parse(raw);
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error("DSH_ACP_ARGS must be a JSON string array");
  }
  return value;
}

function parseEnvironment() {
  const raw = process.env.DSH_PROCESS_ENV_JSON ?? "{}";
  const value = JSON.parse(raw);
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("DSH_PROCESS_ENV_JSON must be a JSON object");
  }
  const entries = Object.entries(value);
  if (entries.some(([key, item]) => !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || typeof item !== "string")) {
    throw new Error("DSH_PROCESS_ENV_JSON contains an invalid environment entry");
  }
  return Object.fromEntries(entries);
}

function boundedStderr(child) {
  let remaining = MAX_STDERR_BYTES;
  child.stderr.on("data", (chunk) => {
    if (remaining <= 0) return;
    const text = String(chunk);
    const output = text.slice(0, remaining);
    remaining -= Buffer.byteLength(output, "utf8");
    process.stderr.write(output);
  });
}

function validateFrame(line) {
  const frame = JSON.parse(line);
  if (frame === null || typeof frame !== "object" || Array.isArray(frame)) {
    throw new Error("DSH child emitted a non-object ACP frame");
  }
  return frame;
}

function main() {
  const command = required("DSH_ACP_COMMAND");
  const cwd = required("DSH_ACP_CWD");
  const child = spawn(command, parseArgs(), {
    cwd,
    env: parseEnvironment(),
    stdio: ["pipe", "pipe", "pipe"],
    detached: true,
  });
  boundedStderr(child);

  const childLines = createInterface({ input: child.stdout });
  childLines.on("line", (line) => {
    try {
      validateFrame(line);
      process.stdout.write(`${line}\n`);
    } catch (error) {
      process.stderr.write(`DSH ACP protocol error: ${error instanceof Error ? error.message : "invalid frame"}\n`);
      child.kill("SIGTERM");
      process.exitCode = 65;
    }
  });
  child.on("error", (error) => {
    process.stderr.write(`DSH child could not start: ${error.name}\n`);
    process.exitCode = 69;
  });
  child.on("close", (code, signal) => {
    process.stdin.destroy();
    if (signal === null && code !== 0) process.exitCode = code ?? 1;
  });

  const input = createInterface({ input: process.stdin });
  input.on("line", (line) => {
    try {
      validateFrame(line);
      child.stdin.write(`${line}\n`);
    } catch (error) {
      process.stderr.write(`SIDERA ACP input error: ${error instanceof Error ? error.message : "invalid frame"}\n`);
      process.exitCode = 65;
    }
  });
  input.on("close", () => {
    child.stdin.end();
  });

  const stop = () => {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
  };
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);
}

try {
  main();
} catch (error) {
  process.stderr.write(`DSH ACP host configuration error: ${error instanceof Error ? error.message : "invalid configuration"}\n`);
  process.exitCode = 64;
}
