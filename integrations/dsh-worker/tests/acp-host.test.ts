import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const HOST = join(ROOT, "src", "acp-host.ts");

async function mockCommand(directory) {
  const script = join(directory, "mock-dsh.mjs");
  await writeFile(script, `
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";
const record = process.env.MOCK_RECORD;
appendFileSync(record, JSON.stringify({ deepseek: process.env.DEEPSEEK_API_KEY ?? null, model: process.env.DSH_MODEL ?? null }) + "\\n");
let prompts = 0;
const input = createInterface({ input: process.stdin });
input.on("line", (line) => {
  const request = JSON.parse(line);
  if (request.method === "session/prompt") prompts += 1;
  let result = {};
  if (request.method === "initialize") result = { protocolVersion: 1, capabilities: {} };
  if (request.method === "session/new") result = { sessionId: "dsh-session-1" };
  if (request.method === "session/prompt") result = { stopReason: "end_turn", committed: true, prompts };
  if (request.method === "session/cancel") result = { cancelled: true };
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }) + "\\n");
});
`, "utf8");
  return script;
}

async function startHost() {
  const directory = await mkdtemp(join("/tmp", "sidera-dsh-host-"));
  const record = join(directory, "record.jsonl");
  const mock = await mockCommand(directory);
  const host = spawn(process.execPath, [HOST], {
    cwd: directory,
    env: {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      DSH_ACP_COMMAND: process.execPath,
      DSH_ACP_ARGS: JSON.stringify([mock]),
      DSH_ACP_CWD: directory,
      DSH_PROCESS_ENV_JSON: JSON.stringify({
        PATH: process.env.PATH ?? "/usr/bin:/bin",
        MOCK_RECORD: record,
        DSH_MODEL: "configured-model",
      }),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const output = createInterface({ input: host.stdout });
  const frames = [];
  output.on("line", (line) => frames.push(JSON.parse(line)));
  return { directory, record, host, frames };
}

function send(host, id, method, params = {}) {
  host.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
}

async function waitFor(frames, id) {
  for (;;) {
    const frame = frames.find((candidate) => candidate.id === id);
    if (frame !== undefined) return frame;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

test("forwards the ACP lifecycle without a startup model request", async () => {
  const { directory, record, host, frames } = await startHost();
  try {
    send(host, 1, "initialize");
    send(host, 2, "session/new", { cwd: directory });
    assert.deepEqual((await waitFor(frames, 1)).result, { protocolVersion: 1, capabilities: {} });
    assert.deepEqual((await waitFor(frames, 2)).result, { sessionId: "dsh-session-1" });
    assert.equal(frames.some((frame) => frame.id === undefined), false);
    send(host, 3, "session/prompt", { sessionId: "dsh-session-1", prompt: [{ type: "text", text: "hello" }] });
    assert.deepEqual((await waitFor(frames, 3)).result, { stopReason: "end_turn", committed: true, prompts: 1 });
    send(host, 4, "session/cancel", { sessionId: "dsh-session-1" });
    assert.deepEqual((await waitFor(frames, 4)).result, { cancelled: true });
    const env = await readFile(record, "utf8");
    assert.match(env, /"deepseek":null/);
    assert.match(env, /"model":"configured-model"/);
  } finally {
    host.kill("SIGTERM");
    await once(host, "close");
  }
});

test("does not inherit an ambient provider key into the DSH process", async () => {
  const previous = process.env.DEEPSEEK_API_KEY;
  process.env.DEEPSEEK_API_KEY = `ambient-${randomUUID()}`;
  const { host, record, frames } = await startHost();
  try {
    send(host, 1, "initialize");
    await waitFor(frames, 1);
  } finally {
    host.kill("SIGTERM");
    await once(host, "close");
    if (previous === undefined) delete process.env.DEEPSEEK_API_KEY;
    else process.env.DEEPSEEK_API_KEY = previous;
  }
  const env = await readFile(record, "utf8");
  assert.match(env, /"deepseek":null/);
});
