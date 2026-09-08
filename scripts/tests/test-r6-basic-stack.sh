#!/usr/bin/env bash
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-stack-test.XXXXXX")
RUNTIME="$TMP/runtime"
REPOSITORY="$TMP/repository"
MANIFEST="$TMP/manifest.json"
CAPTURE="$TMP/capture"
FAKE_BIN="$TMP/bin"
STUB="$FAKE_BIN/r6-stub"

cleanup() {
  SIDERA_R6_BASIC_RUNTIME_DIR="$RUNTIME" \
    SIDERA_R6_BASIC_TEST_MODE=1 \
    "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$MANIFEST" >/dev/null 2>&1 || true
  if [ -n "${OCCUPIED_PID:-}" ]; then
    kill "$OCCUPIED_PID" >/dev/null 2>&1 || true
    wait "$OCCUPIED_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "${REUSED_PID:-}" ]; then
    kill "$REUSED_PID" >/dev/null 2>&1 || true
    wait "$REUSED_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

assert_contains() {
  needle=$1
  haystack=$2
  message=$3
  printf '%s\n' "$haystack" | grep -F -- "$needle" >/dev/null 2>&1 \
    || fail "$message (missing: $needle)"
}

assert_not_contains() {
  needle=$1
  haystack=$2
  message=$3
  if printf '%s\n' "$haystack" | grep -F -- "$needle" >/dev/null 2>&1; then
    fail "$message (found: $needle)"
  fi
}

mkdir -p "$FAKE_BIN" "$CAPTURE" "$REPOSITORY"
git -C "$REPOSITORY" init -q
git -C "$REPOSITORY" config user.name 'SIDERA stack test'
git -C "$REPOSITORY" config user.email 'stack-test@sidera.local'
printf 'acceptance fixture\n' > "$REPOSITORY/README.md"
git -C "$REPOSITORY" add README.md
git -C "$REPOSITORY" commit -qm 'fixture'
BASE_COMMIT=$(git -C "$REPOSITORY" rev-parse HEAD)

cat > "$STUB" <<'SH'
#!/bin/sh
set -eu
component=${1:?component}
capture=${SIDERA_R6_BASIC_TEST_CAPTURE:?capture}
if [ "${SIDERA_R6_BASIC_STUB_FAIL_COMPONENT:-}" = "$component" ]; then
  printf '%s\n' "{\"component\":\"$component\",\"status\":\"error\",\"diagnostic\":\"stub_start_failed\"}" >&2
  exit 42
fi
mkdir -p "$capture"
env | sort > "$capture/$component.env"
printf '%s\n' "{\"component\":\"$component\",\"status\":\"ready\",\"heartbeat\":true,\"diagnostic\":\"stub\"}"
trap 'if [ "${SIDERA_R6_BASIC_STUB_IGNORE_TERM_COMPONENT:-}" != "$component" ]; then exit 0; fi' TERM
while :; do
  sleep 1
done
SH
chmod +x "$STUB"

python3 - "$MANIFEST" "$REPOSITORY" "$BASE_COMMIT" <<'PY'
import json
import sys
from pathlib import Path

manifest_path, repository, base_commit = sys.argv[1:]
payload = {
    "schema_version": 1,
    "run_id": "10000000-0000-0000-0000-000000000101",
    "workspace_id": "10000000-0000-0000-0000-000000000102",
    "local_gate_record": str(Path(manifest_path).with_name("gate.json")),
    "runtime_fingerprint": "a" * 64,
    "run_root": str(Path(manifest_path).parent / "run"),
    "repository": repository,
    "base_commit": base_commit,
    "database_ref": "env:SIDERA_TEST_DB",
    "knowledge_database_ref": "env:SIDERA_TEST_KNOWLEDGE_DB",
    "control_plane_url": "http://127.0.0.1:18001",
    "knowledge_url": "http://127.0.0.1:18002",
    "main_agent_gateway_url": "http://127.0.0.1:18003",
    "web_url": "http://127.0.0.1:18004",
    "profiles": [
        {"role": "main_agent", "runtime": "stub", "provider": "stub", "model_id": "stub", "endpoint_host": "127.0.0.1:18005", "credential_ref": "none", "config_ref": "module:test/main", "config_version": 1},
        {"role": "planner", "runtime": "stub", "provider": "stub", "model_id": "stub", "endpoint_host": "127.0.0.1:18005", "credential_ref": "none", "config_ref": "module:test/planner", "config_version": 1},
        {"role": "worker", "runtime": "stub", "provider": "stub", "model_id": "stub", "endpoint_host": "127.0.0.1:18005", "credential_ref": "none", "config_ref": "module:test/worker", "config_version": 1},
        {"role": "second_worker", "runtime": "stub", "provider": "stub", "model_id": "stub", "endpoint_host": "127.0.0.1:18005", "credential_ref": "none", "config_ref": "module:test/second", "config_version": 1},
    ],
    "scenarios": {"user-flow": {"main_agent_turns": 1, "planner_runs": 1, "worker_sessions": 3, "adapter_prompts": 3, "max_seconds": 60}},
}
Path(manifest_path).write_text(json.dumps(payload), encoding="utf-8")
PY

export SIDERA_R6_BASIC_RUNTIME_DIR="$RUNTIME"
export SIDERA_R6_BASIC_TEST_MODE=1
export SIDERA_R6_BASIC_STUB_COMMAND="$STUB"
export SIDERA_R6_BASIC_TEST_CAPTURE="$CAPTURE"
export SIDERA_TEST_DB='postgresql+psycopg://test@127.0.0.1:55432/sidera_test'
export SIDERA_TEST_KNOWLEDGE_DB='postgresql+psycopg://test@127.0.0.1:55432/sidera_knowledge_test'
export DEEPSEEK_API_KEY='must-not-reach-child'

bash -n "$ROOT/scripts/r6-basic-stack.sh" || fail 'stack script must have valid bash syntax'

"$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST" > "$TMP/start.log"
STATUS=$("$ROOT/scripts/r6-basic-stack.sh" status --manifest "$MANIFEST")
for component in \
  knowledge-service control-plane akashic-acceptance planning-worker \
  context-package-worker knowledge-candidate-worker activation-worker \
  scheduler-worker gateway-worker plan-transition-worker handoff-worker \
  collaboration-worker notification-worker result-worker main-agent-gateway web; do
  assert_contains "$component: running/ready" "$STATUS" "component should be started"
done

if "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST" >/dev/null 2>&1; then
  fail 'duplicate start must fail'
fi

before_pid=$(cat "$RUNTIME/pids/notification-worker.pid")
control_plane_pid_before_restart=$(cat "$RUNTIME/pids/control-plane.pid")
"$ROOT/scripts/r6-basic-stack.sh" restart --manifest "$MANIFEST" --component notification-worker >/dev/null
after_pid=$(cat "$RUNTIME/pids/notification-worker.pid")
[ "$before_pid" != "$after_pid" ] || fail 'single-component restart must replace its pid'
[ "$(cat "$RUNTIME/pids/control-plane.pid")" = "$control_plane_pid_before_restart" ] \
  || fail 'single-component restart must not touch unrelated components'

"$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$MANIFEST" >/dev/null

"$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST" --component control-plane >/dev/null
export SIDERA_R6_BASIC_STUB_FAIL_COMPONENT=planning-worker
if "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST" >/dev/null 2>&1; then
  fail 'component startup failure must fail the stack start'
fi
[ -f "$RUNTIME/pids/control-plane.pid" ] || fail 'pre-existing control-plane must not be rolled back'
[ ! -f "$RUNTIME/pids/planning-worker.pid" ] || fail 'failed component must not leave a pid'
[ ! -f "$RUNTIME/pids/knowledge-service.pid" ] || fail 'newly started components must be rolled back'
unset SIDERA_R6_BASIC_STUB_FAIL_COMPONENT
"$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$MANIFEST" >/dev/null

python3 - <<'PY' &
import socket
import time

sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 18002))
sock.listen(1)
while True:
    time.sleep(1)
PY
OCCUPIED_PID=$!
sleep 0.2
if "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST" --component knowledge-service >/dev/null 2>&1; then
  fail 'occupied port must reject start'
fi
kill "$OCCUPIED_PID" >/dev/null 2>&1 || true
wait "$OCCUPIED_PID" >/dev/null 2>&1 || true
unset OCCUPIED_PID

"$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST" --component control-plane >/dev/null
sh -c 'exec sleep 30' &
REUSED_PID=$!
printf '%s\n' "$REUSED_PID" > "$RUNTIME/pids/notification-worker.pid"
cat > "$RUNTIME/identity/notification-worker.identity" <<EOF
pid=$REUSED_PID
run_id=10000000-0000-0000-0000-000000000101
workspace_id=10000000-0000-0000-0000-000000000102
component=notification-worker
marker=sidera-r6-basic:10000000-0000-0000-0000-000000000101:notification-worker
process_start=not-the-current-start
argv_hash=not-the-current-argv
command=expected-r6-basic-test-command
EOF
if "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$MANIFEST" --component notification-worker >/dev/null 2>&1; then
  fail 'stop must refuse a reused or untrusted pid'
fi
kill -0 "$REUSED_PID" >/dev/null 2>&1 || fail 'untrusted pid must remain alive'

capture_env=$(cat "$CAPTURE/control-plane.env")
assert_contains 'SIDERA_R6_BASIC_REPOSITORY=' "$capture_env" 'repository config must reach workers'
assert_contains 'SIDERA_R6_BASIC_NOTIFICATION_BASELINE_REQUIRED=true' "$capture_env" 'baseline flag must reach workers'
assert_contains 'SIDERA_TEST_DB=' "$capture_env" 'selected database ref must reach workers'
assert_not_contains 'DEEPSEEK_API_KEY=' "$capture_env" 'ambient provider secrets must not reach workers'

printf 'PASS: r6 basic stack lifecycle, ownership, env isolation, and rollback\n'
