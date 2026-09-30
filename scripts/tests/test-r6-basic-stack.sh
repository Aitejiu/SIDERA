#!/usr/bin/env bash
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
UV_BIN=${SIDERA_UV_BIN:-uv}
TMP=$(mktemp -d /tmp/srt.XXXXXX)
TMP_REAL=$(CDPATH= cd -- "$TMP" && pwd -P)
RUNTIME="$TMP/runtime"
GUARD_RUNTIME="$TMP/guard-runtime"
BAD_GUARD_RUNTIME="$TMP/bad-guard-runtime"
REPOSITORY="$TMP/repository"
MANIFEST="$TMP/manifest.json"
GUARD_MANIFEST="$TMP/guard-manifest.json"
BAD_GUARD_MANIFEST="$TMP/bad-guard-manifest.json"
READ_ONLY_MANIFEST="$TMP/read-only-manifest.json"
CAPTURE="$TMP/capture"
GUARD_CAPTURE="$TMP/guard-capture"
INTERACTIVE_RUNTIME="$TMP/interactive-runtime"
INTERACTIVE_UNREADY_RUNTIME="$TMP/interactive-unready-runtime"
INTERACTIVE_CAPTURE="$TMP/interactive-capture"
READ_ONLY_CAPTURE="$TMP/read-only-capture"
BOOTSTRAP_RUNTIME="$TMP/bootstrap-runtime"
BOOTSTRAP_CAPTURE="$TMP/bootstrap-capture"
FAKE_BIN="$TMP/bin"
STUB="$FAKE_BIN/r6-stub"
UNREADY_STUB="$FAKE_BIN/r6-unready-stub"
FAKE_AKASHIC_PID=

cleanup() {
  if [ -n "${ARGV_RUNTIME:-}" ] && [ -f "${ARGV_MANIFEST:-}" ]; then
    env -u SIDERA_R6_BASIC_TEST_MODE -u SIDERA_R6_BASIC_STUB_COMMAND \
      SIDERA_R6_BASIC_MODE=interactive \
      SIDERA_R6_BASIC_RUNTIME_DIR="$ARGV_RUNTIME" \
      "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$ARGV_MANIFEST" \
      >/dev/null 2>&1 || true
  fi
  SIDERA_R6_BASIC_RUNTIME_DIR="$RUNTIME" \
    SIDERA_R6_BASIC_TEST_MODE=1 \
    "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$MANIFEST" >/dev/null 2>&1 || true
  if [ -f "$GUARD_MANIFEST" ]; then
    SIDERA_R6_BASIC_RUNTIME_DIR="$GUARD_RUNTIME" \
      SIDERA_R6_BASIC_TEST_MODE=1 \
      "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$GUARD_MANIFEST" >/dev/null 2>&1 || true
    SIDERA_R6_BASIC_MODE=interactive \
      SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_RUNTIME" \
      SIDERA_R6_BASIC_TEST_MODE=1 \
      "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$GUARD_MANIFEST" >/dev/null 2>&1 || true
    SIDERA_R6_BASIC_MODE=interactive \
      SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_UNREADY_RUNTIME" \
      SIDERA_R6_BASIC_TEST_MODE=1 \
      SIDERA_R6_BASIC_STUB_COMMAND="$UNREADY_STUB" \
      "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$GUARD_MANIFEST" >/dev/null 2>&1 || true
  fi
  if [ -f "$BAD_GUARD_MANIFEST" ]; then
    SIDERA_R6_BASIC_RUNTIME_DIR="$BAD_GUARD_RUNTIME" \
      SIDERA_R6_BASIC_TEST_MODE=1 \
      "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$BAD_GUARD_MANIFEST" >/dev/null 2>&1 || true
  fi
  if [ -f "$READ_ONLY_MANIFEST" ]; then
    env SIDERA_R6_BASIC_RUNTIME_DIR="$TMP/read-only-runtime" SIDERA_R6_BASIC_TEST_MODE=1 "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$READ_ONLY_MANIFEST" >/dev/null 2>&1 || true
  fi
  if [ -n "$FAKE_AKASHIC_PID" ]; then
    kill "$FAKE_AKASHIC_PID" >/dev/null 2>&1 || true
    wait "$FAKE_AKASHIC_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "${OCCUPIED_PID:-}" ]; then
    kill "$OCCUPIED_PID" >/dev/null 2>&1 || true
    wait "$OCCUPIED_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "${REUSED_PID:-}" ]; then
    kill "$REUSED_PID" >/dev/null 2>&1 || true
    wait "$REUSED_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "${UNREADY_PID:-}" ]; then
    kill "$UNREADY_PID" >/dev/null 2>&1 || true
    wait "$UNREADY_PID" >/dev/null 2>&1 || true
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
if [ "$component" = akashic-acceptance ] \
  && [ "${SIDERA_R6_BASIC_TEST_AKASHIC_BRIDGE_READY:-0}" = 1 ]; then
  printf '%s\n' '插件已加载: sidera-task-bridge'
fi
trap 'if [ "${SIDERA_R6_BASIC_STUB_IGNORE_TERM_COMPONENT:-}" != "$component" ]; then exit 0; fi' TERM
while :; do
  sleep 1
done
SH
chmod +x "$STUB"

cat > "$UNREADY_STUB" <<'SH'
#!/bin/sh
set -eu
component=${1:?component}
capture=${SIDERA_R6_BASIC_TEST_CAPTURE:?capture}
mkdir -p "$capture"
env | sort > "$capture/$component.env"
printf '%s\n' "{\"component\":\"$component\",\"diagnostic\":\"never_ready\"}"
trap 'exit 0' TERM
while :; do
  sleep 1
done
SH
chmod +x "$UNREADY_STUB"

start_fake_akashic() {
  runtime_dir=$1
  socket_path="$runtime_dir/akashic-workspace/akashic.sock"
  mkdir -p "$(dirname "$socket_path")"
  python3 - "$socket_path" <<'PY' &
import json
import socket
import sys
from pathlib import Path

server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
socket_path = Path(sys.argv[1])
try:
    socket_path.unlink()
except FileNotFoundError:
    pass
server.bind(str(socket_path))
server.listen()
while True:
    connection, _ = server.accept()
    with connection:
        stream = connection.makefile("rwb", buffering=0)
        for raw in iter(stream.readline, b""):
            message = json.loads(raw)
            request_id = message.get("id")
            if request_id is None:
                continue
            if message.get("method") == "initialize":
                result = {"protocolVersion": "1.0"}
            elif message.get("method") == "server/status":
                result = {"ready": True}
            else:
                result = {}
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            stream.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
PY
  FAKE_AKASHIC_PID=$!
  i=0
  while [ ! -S "$socket_path" ] && [ "$i" -lt 100 ]; do
    sleep .01
    i=$((i + 1))
  done
  [ -S "$socket_path" ] || fail 'fake Akashic control socket did not become available'
}

stop_fake_akashic() {
  [ -n "$FAKE_AKASHIC_PID" ] || return 0
  kill "$FAKE_AKASHIC_PID" >/dev/null 2>&1 || true
  wait "$FAKE_AKASHIC_PID" >/dev/null 2>&1 || true
  FAKE_AKASHIC_PID=
}

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
export SIDERA_CREDENTIAL_STORE_READY=1
export SIDERA_ACCEPTANCE_SCENARIO='revision'
export SIDERA_ACCEPTANCE_GROUP='B'
export SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=300
export SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS=300
export SIDERA_R6_BASIC_PLANNING_PROFILE_ID='10000000-0000-0000-0000-000000000105'
export SIDERA_R6_BASIC_PLANNING_PROFILE_VERSION=7
export DEEPSEEK_API_KEY='must-not-reach-child'
export AKASHIC_PLUGIN_HOME="$TMP/ambient-akashic-plugin-home"

bash -n "$ROOT/scripts/r6-basic-stack.sh" || fail 'stack script must have valid bash syntax'
STACK_SOURCE=$(cat "$ROOT/scripts/r6-basic-stack.sh")
assert_contains 'python "$AKA_ROOT/main.py"' "$STACK_SOURCE" \
  'Akashic must launch from an absolute script path independent of caller cwd'
assert_contains 'akashic_bridge_ready' "$STACK_SOURCE" \
  'Akashic readiness must verify its protocol and SIDERA bridge capability'
python3 - "$ROOT/scripts/r6-basic-stack.sh" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
start = source.index("ready() {")
end = source.index("write_identity() {", start)
readiness = source[start:end]
if 'akashic_plugin_gate >/dev/null 2>&1 || return 1' not in readiness:
    raise SystemExit("acceptance Akashic readiness must require the private plugin gate")
if 'akashic_server_ready && return 0' not in readiness:
    raise SystemExit("Akashic readiness must verify the live Core socket")
PY
assert_contains 'r6_akashic_credentials.py' "$STACK_SOURCE" \
  'acceptance startup must sync its declared main-agent env credential into the isolated Akashic model store'
assert_contains '"SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT": str(Path(runtime) / "akashic-workspace" / "akashic.sock")' "$STACK_SOURCE" \
  'main-agent gateway must receive the isolated Akashic control socket'
assert_contains '"SIDERA_GATEWAY_SYNC_WORKSPACE_ID": str(manifest["workspace_id"])' "$STACK_SOURCE" \
  'gateway must receive the workspace whose active sessions it renews'
assert_contains 'run_component "$component" /bin/sh -c' "$STACK_SOURCE" \
  'web must launch Vite from its application root so its owner argv remains stable'
assert_contains 'web_port=$(url_port "$WEB_URL")' "$STACK_SOURCE" \
  'web port must be resolved before entering the shell wrapper'
assert_contains '"SIDERA_WEB_CONTROL_PLANE_PORT": url_port(control_url)' "$STACK_SOURCE" \
  'web must receive the isolated control-plane proxy port'
assert_contains '"SIDERA_WEB_KNOWLEDGE_SERVICE_PORT": url_port(knowledge_url)' "$STACK_SOURCE" \
  'web must receive the isolated knowledge-service proxy port'
assert_contains '"SIDERA_WEB_MAIN_AGENT_PORT": url_port(gateway_url)' "$STACK_SOURCE" \
  'web must receive the isolated Main Agent proxy port'
assert_contains '"VITE_SIDERA_WORKSPACE_ID": str(manifest["workspace_id"])' "$STACK_SOURCE" \
  'web must receive the selected workspace id'
assert_contains 'reconciler-worker' "$STACK_SOURCE" \
  'real acceptance stack must run the reconciler for expired leases and unknown outcomes'
assert_contains '--state-root "$RUN_ROOT/bootstrap-state"' "$STACK_SOURCE" \
  'bootstrap state must be scoped to the private run root'
python3 - "$ROOT/scripts/r6-basic-stack.sh" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
start = source.index("start_one() {")
ready = source.index('ready "$component"', start)
identity = source.index('write_identity "$component" "$pid" "$command" "$start"', start)
if ready > identity:
    raise SystemExit("component identity must be recorded after readiness settles the process argv")
prepare_start = source.index("prepare() {")
prepare_end = source.index("validate_acceptance_guard() {", prepare_start)
if '[ "$BASIC_MODE" = interactive ] && return 0' not in source[prepare_start:prepare_end]:
    raise SystemExit("interactive startup must preserve existing user-configured modules")
PY
assert_contains 'if ready "$component"; then' "$STACK_SOURCE" \
  'status must reuse the live readiness check for HTTP services'
COMPONENT_LINE=$(printf '%s\n' "$STACK_SOURCE" | sed -n 's/^COMPONENTS=//p')
assert_not_contains 'handoff-worker' "$COMPONENT_LINE" \
  'handoff must run inside the single gateway owner, not as a second process'

export SIDERA_R6_BASIC_TEST_AKASHIC_BRIDGE_READY=1
export SIDERA_R6_BASIC_TEST_AKASHIC_GATE=1
start_fake_akashic "$RUNTIME"
"$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST" > "$TMP/start.log"
STATUS=$("$ROOT/scripts/r6-basic-stack.sh" status --manifest "$MANIFEST")
for component in \
  knowledge-service control-plane basic-acceptance-heartbeat akashic-acceptance planning-worker \
  context-package-worker knowledge-candidate-worker activation-worker \
  scheduler-worker reconciler-worker gateway-worker plan-transition-worker \
  collaboration-worker notification-worker result-worker main-agent-gateway web; do
  assert_contains "$component: running/ready" "$STATUS" "component should be started"
done
assert_contains "AKASHIC_PLUGIN_HOME=$TMP_REAL/runtime/akashic-plugin-home" \
  "$(cat "$CAPTURE/akashic-acceptance.env")" \
  'acceptance Akashic must receive the runtime-scoped plugin home'
[ "$(stat -f '%Lp' "$RUNTIME/akashic-plugin-home")" = '700' ] \
  || fail 'runtime plugin-home directory must be owner-only'
for component in control-plane planning-worker gateway-worker web; do
  assert_not_contains 'AKASHIC_PLUGIN_HOME=' "$(cat "$CAPTURE/$component.env")" \
    "$component must not receive Akashic plugin-home configuration"
done
assert_not_contains "$TMP/ambient-akashic-plugin-home" \
  "$(cat "$CAPTURE/akashic-acceptance.env")" \
  'ambient plugin-home value must not override the runtime-scoped value'

TOKENS_PATH="$RUNTIME/tokens.json"
[ -f "$TOKENS_PATH" ] || fail 'basic stack must create a private capability-token file'
[ "$(stat -f '%Lp' "$TOKENS_PATH")" = '600' ] \
  || fail 'capability-token file must be owner-only'
CONTROL_TOKEN=$(sed -n 's/^SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN=//p' "$CAPTURE/control-plane.env")
KNOWLEDGE_CONTROL_TOKEN=$(sed -n 's/^SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN=//p' "$CAPTURE/knowledge-service.env")
[ -n "$CONTROL_TOKEN" ] || fail 'control plane must receive a knowledge capability token'
[ "$CONTROL_TOKEN" = "$KNOWLEDGE_CONTROL_TOKEN" ] \
  || fail 'control plane and knowledge service must share only the control-plane token'
assert_contains 'SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN=' "$(cat "$CAPTURE/knowledge-service.env")" \
  'knowledge service must receive its local-owner capability token'
assert_contains 'SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=' "$(cat "$CAPTURE/knowledge-service.env")" \
  'knowledge service must receive its main-agent capability token'
assert_contains 'SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=' "$(cat "$CAPTURE/akashic-acceptance.env")" \
  'Akashic must receive its matching main-agent knowledge capability token'
assert_contains 'SIDERA_WEB_KNOWLEDGE_TOKEN=' "$(cat "$CAPTURE/web.env")" \
  'web must receive its browser-proxy capability token'
KNOWLEDGE_MAIN_AGENT_TOKEN=$(sed -n 's/^SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=//p' "$CAPTURE/knowledge-service.env")
AKASHIC_KNOWLEDGE_TOKEN=$(sed -n 's/^SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=//p' "$CAPTURE/akashic-acceptance.env")
[ -n "$KNOWLEDGE_MAIN_AGENT_TOKEN" ] \
  || fail 'knowledge service must receive a main-agent token'
[ "$AKASHIC_KNOWLEDGE_TOKEN" = "$KNOWLEDGE_MAIN_AGENT_TOKEN" ] \
  || fail 'Akashic and knowledge service must share only the main-agent token'
KNOWLEDGE_OWNER_TOKEN=$(sed -n 's/^SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN=//p' "$CAPTURE/knowledge-service.env")
WEB_KNOWLEDGE_TOKEN=$(sed -n 's/^SIDERA_WEB_KNOWLEDGE_TOKEN=//p' "$CAPTURE/web.env")
[ -n "$KNOWLEDGE_OWNER_TOKEN" ] \
  || fail 'knowledge service must receive a local-owner token'
[ -n "$WEB_KNOWLEDGE_TOKEN" ] \
  || fail 'web proxy must receive a knowledge token'
[ "$WEB_KNOWLEDGE_TOKEN" = "$KNOWLEDGE_OWNER_TOKEN" ] \
  || fail 'web proxy and knowledge service must share the local-owner token'

BRIDGE_CONFIG="$RUNTIME/akashic-workspace/plugin-data/sidera-task-bridge-sidera/config.local.toml"
[ -f "$BRIDGE_CONFIG" ] || fail 'basic stack must materialize the SIDERA bridge config'
[ "$(stat -f '%Lp' "$BRIDGE_CONFIG")" = '600' ] \
  || fail 'SIDERA bridge config must be owner-only'
"$UV_BIN" run --project "$ROOT/services/control-plane" python - "$BRIDGE_CONFIG" <<'PY'
import sys
import tomllib
from pathlib import Path

config = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert config == {
    "workspace_id": "10000000-0000-0000-0000-000000000102",
    "planning_profile_id": "10000000-0000-0000-0000-000000000105",
    "planning_profile_version": 7,
    "control_plane_url": "http://127.0.0.1:18001",
    "web_base_url": "http://127.0.0.1:18004",
    "knowledge_base_url": "http://127.0.0.1:18002",
    "actor_type": "integration",
    "actor_id": "akashic-task-bridge",
}
assert not any("token" in key.lower() for key in config)
PY

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
stop_fake_akashic
unset SIDERA_R6_BASIC_TEST_AKASHIC_BRIDGE_READY

RUN_ROOT=$(python3 - "$MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["run_root"])
PY
)
LOCK_DIR="$RUN_ROOT/.sidera-maintenance.lock"
mkdir "$LOCK_DIR"
printf '{"pid":%s,"owner":"test-holder"}\n' "$$" > "$LOCK_DIR/owner.json"
if "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST" >/dev/null 2>&1; then
  rm -f "$LOCK_DIR/owner.json"
  rmdir "$LOCK_DIR"
  fail 'basic stack must refuse start while maintenance backup owns the run lock'
fi
[ ! -f "$RUNTIME/pids/control-plane.pid" ] || fail 'busy start must not launch a component'
rm -f "$LOCK_DIR/owner.json"
rmdir "$LOCK_DIR"

python3 - "$READ_ONLY_MANIFEST" "$MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

target, source = map(Path, sys.argv[1:])
payload = json.loads(source.read_text(encoding="utf-8"))
payload.update(
    {
        "run_id": "10000000-0000-0000-0000-000000000103",
        "workspace_id": "10000000-0000-0000-0000-000000000104",
        "run_root": str(target.parent / "read-only-run"),
        "restore_mode": "read_only",
    }
)
target.write_text(json.dumps(payload), encoding="utf-8")
PY
env SIDERA_R6_BASIC_RUNTIME_DIR="$TMP/read-only-runtime" SIDERA_R6_BASIC_TEST_CAPTURE="$READ_ONLY_CAPTURE" "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$READ_ONLY_MANIFEST" >/dev/null
READ_ONLY_STATUS=$(
  env SIDERA_R6_BASIC_RUNTIME_DIR="$TMP/read-only-runtime" "$ROOT/scripts/r6-basic-stack.sh" status --manifest "$READ_ONLY_MANIFEST"
)
for component in knowledge-service control-plane main-agent-gateway web; do
  assert_contains "$component: running/ready" "$READ_ONLY_STATUS" "read-only service should be started"
done
for component in basic-acceptance-heartbeat akashic-acceptance planning-worker scheduler-worker reconciler-worker notification-worker; do
  [ ! -f "$TMP/read-only-runtime/pids/$component.pid" ] || fail "read-only stack must not start $component"
done
if env SIDERA_R6_BASIC_RUNTIME_DIR="$TMP/read-only-runtime" "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$READ_ONLY_MANIFEST" --component planning-worker >/dev/null 2>&1; then
  fail 'read-only stack must reject worker start'
fi
READ_ONLY_CAPTURE_CONTENT=$(cat "$READ_ONLY_CAPTURE"/* 2>/dev/null || true)
assert_contains 'SIDERA_RESTORE_READ_ONLY=true' "$READ_ONLY_CAPTURE_CONTENT" 'read-only mode must reach service children'
assert_contains 'SIDERA_RESTORE_SCHEMA_VALIDATION=true' "$READ_ONLY_CAPTURE_CONTENT" 'read-only mode must request schema validation'
read_only_gateway_pid=$(cat "$TMP/read-only-runtime/pids/main-agent-gateway.pid")
env \
  SIDERA_R6_BASIC_RUNTIME_DIR="$TMP/read-only-runtime" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_STUB_COMMAND="$STUB" \
  SIDERA_R6_BASIC_TEST_CAPTURE="$READ_ONLY_CAPTURE" \
  "$ROOT/scripts/r6-basic-stack.sh" restart --manifest "$READ_ONLY_MANIFEST" >/dev/null
[ "$(cat "$TMP/read-only-runtime/pids/main-agent-gateway.pid")" != "$read_only_gateway_pid" ] \
  || fail 'read-only full-stack restart must remain available without an Akashic plugin gate'
read_only_gateway_pid=$(cat "$TMP/read-only-runtime/pids/main-agent-gateway.pid")
if env \
  SIDERA_R6_BASIC_RUNTIME_DIR="$TMP/read-only-runtime" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_STUB_COMMAND="$STUB" \
  SIDERA_R6_BASIC_TEST_CAPTURE="$READ_ONLY_CAPTURE" \
  "$ROOT/scripts/r6-basic-stack.sh" restart --manifest "$READ_ONLY_MANIFEST" \
    --component akashic-acceptance >/dev/null 2>&1; then
  fail 'read-only restore must reject Akashic restart before stopping anything'
fi
[ "$(cat "$TMP/read-only-runtime/pids/main-agent-gateway.pid")" = "$read_only_gateway_pid" ] \
  || fail 'rejected read-only Akashic restart must preserve running services'
env SIDERA_R6_BASIC_RUNTIME_DIR="$TMP/read-only-runtime" "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$READ_ONLY_MANIFEST" >/dev/null

start_fake_akashic "$BOOTSTRAP_RUNTIME"
BOOTSTRAP_STATUS=$(env \
  SIDERA_R6_BASIC_RUNTIME_DIR="$BOOTSTRAP_RUNTIME" \
  SIDERA_R6_BASIC_TEST_CAPTURE="$BOOTSTRAP_CAPTURE" \
  "$ROOT/scripts/r6-basic-stack.sh" start --bootstrap-akashic --manifest "$MANIFEST")
for component in knowledge-service control-plane akashic-acceptance main-agent-gateway; do
  assert_contains "$component: running/ready" "$BOOTSTRAP_STATUS" \
    "Akashic bootstrap must start $component"
done
for component in basic-acceptance-heartbeat planning-worker context-package-worker \
  knowledge-candidate-worker activation-worker scheduler-worker reconciler-worker \
  gateway-worker plan-transition-worker collaboration-worker notification-worker \
  result-worker web; do
  assert_not_contains "$component: running/ready" "$BOOTSTRAP_STATUS" \
    "Akashic bootstrap must not start $component"
  [ ! -f "$BOOTSTRAP_RUNTIME/pids/$component.pid" ] \
    || fail "Akashic bootstrap must not leave a pid for $component"
done
[ "$(find "$BOOTSTRAP_RUNTIME/pids" -maxdepth 1 -type f -name '*.pid' | wc -l | tr -d ' ')" = 4 ] \
  || fail 'Akashic bootstrap must own exactly four component processes'
BOOTSTRAP_AKASHIC_LOG=$(cat "$BOOTSTRAP_RUNTIME/logs/akashic-acceptance.log")
assert_not_contains '插件已加载: sidera-task-bridge' "$BOOTSTRAP_AKASHIC_LOG" \
  'Akashic bootstrap must become ready without a loaded bridge'
assert_contains "AKASHIC_PLUGIN_HOME=$TMP_REAL/bootstrap-runtime/akashic-plugin-home" \
  "$(cat "$BOOTSTRAP_CAPTURE/akashic-acceptance.env")" \
  'bootstrap Akashic must receive its runtime-scoped plugin home'

bootstrap_control_pid=$(cat "$BOOTSTRAP_RUNTIME/pids/control-plane.pid")
bootstrap_akashic_pid=$(cat "$BOOTSTRAP_RUNTIME/pids/akashic-acceptance.pid")
if env \
  SIDERA_R6_BASIC_RUNTIME_DIR="$BOOTSTRAP_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_TEST_CAPTURE="$BOOTSTRAP_CAPTURE" \
  SIDERA_R6_BASIC_TEST_AKASHIC_GATE=0 \
  "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST" >/dev/null 2>&1; then
  fail 'normal acceptance start must reject an uninstalled bridge after bootstrap'
fi
[ "$(cat "$BOOTSTRAP_RUNTIME/pids/control-plane.pid")" = "$bootstrap_control_pid" ] \
  || fail 'failed full-start preflight must preserve the bootstrap control plane'
[ "$(cat "$BOOTSTRAP_RUNTIME/pids/akashic-acceptance.pid")" = "$bootstrap_akashic_pid" ] \
  || fail 'failed full-start preflight must preserve the bootstrap Akashic process'
[ ! -f "$BOOTSTRAP_RUNTIME/pids/basic-acceptance-heartbeat.pid" ] \
  || fail 'failed full-start preflight must not start acceptance workers'
if env \
  SIDERA_R6_BASIC_RUNTIME_DIR="$BOOTSTRAP_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_TEST_AKASHIC_GATE=0 \
  "$ROOT/scripts/r6-basic-stack.sh" status --manifest "$MANIFEST" \
    --component akashic-acceptance > "$TMP/bootstrap-status.log" 2>&1; then
  fail 'bootstrap Akashic status must not report ready before bridge installation'
fi
assert_contains 'akashic-acceptance: running/unready' \
  "$(cat "$TMP/bootstrap-status.log")" \
  'bootstrap host must remain running but unready to the acceptance stack'

BOOTSTRAP_FULL_STATUS=$(env \
  SIDERA_R6_BASIC_RUNTIME_DIR="$BOOTSTRAP_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_TEST_CAPTURE="$BOOTSTRAP_CAPTURE" \
  SIDERA_R6_BASIC_TEST_AKASHIC_GATE=1 \
  "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$MANIFEST")
for component in basic-acceptance-heartbeat planning-worker context-package-worker \
  knowledge-candidate-worker activation-worker scheduler-worker reconciler-worker \
  gateway-worker plan-transition-worker collaboration-worker notification-worker \
  result-worker web; do
  assert_contains "$component: running/ready" "$BOOTSTRAP_FULL_STATUS" \
    "full acceptance start must proceed after the private bridge gate passes ($component)"
done

before_guarded_restart=$(cat "$BOOTSTRAP_RUNTIME/pids/control-plane.pid")
before_guarded_akashic_restart=$(cat "$BOOTSTRAP_RUNTIME/pids/akashic-acceptance.pid")
if env \
  SIDERA_R6_BASIC_RUNTIME_DIR="$BOOTSTRAP_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_TEST_CAPTURE="$BOOTSTRAP_CAPTURE" \
  SIDERA_R6_BASIC_TEST_AKASHIC_GATE=0 \
  "$ROOT/scripts/r6-basic-stack.sh" restart --manifest "$MANIFEST" >/dev/null 2>&1; then
  fail 'full acceptance restart must reject a mismatched or absent private receipt'
fi
[ "$(cat "$BOOTSTRAP_RUNTIME/pids/control-plane.pid")" = "$before_guarded_restart" ] \
  || fail 'failed restart preflight must not stop the control plane'
[ "$(cat "$BOOTSTRAP_RUNTIME/pids/akashic-acceptance.pid")" = "$before_guarded_akashic_restart" ] \
  || fail 'failed restart preflight must not stop the Akashic process'

SIDERA_R6_BASIC_RUNTIME_DIR="$BOOTSTRAP_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_TEST_CAPTURE="$BOOTSTRAP_CAPTURE" \
  SIDERA_R6_BASIC_TEST_AKASHIC_GATE=1 \
  "$ROOT/scripts/r6-basic-stack.sh" restart --manifest "$MANIFEST" >/dev/null
after_guarded_akashic_restart=$(cat "$BOOTSTRAP_RUNTIME/pids/akashic-acceptance.pid")
[ "$after_guarded_akashic_restart" != "$before_guarded_akashic_restart" ] \
  || fail 'full restart with a valid private bridge gate must replace the Akashic process'
BOOTSTRAP_RESTART_STATUS=$(SIDERA_R6_BASIC_RUNTIME_DIR="$BOOTSTRAP_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_TEST_AKASHIC_GATE=1 \
  "$ROOT/scripts/r6-basic-stack.sh" status --manifest "$MANIFEST")
assert_contains 'akashic-acceptance: running/ready' "$BOOTSTRAP_RESTART_STATUS" \
  'restarted Akashic must pass the stable private gate'

if "$ROOT/scripts/r6-basic-stack.sh" start --bootstrap-akashic \
  --manifest "$MANIFEST" --component control-plane >/dev/null 2>&1; then
  fail 'Akashic bootstrap must reject component-scoped start'
fi
bootstrap_control_pid=$(cat "$BOOTSTRAP_RUNTIME/pids/control-plane.pid")
if "$ROOT/scripts/r6-basic-stack.sh" restart --bootstrap-akashic \
  --manifest "$MANIFEST" >/dev/null 2>&1; then
  fail 'Akashic bootstrap must reject restart'
fi
[ "$(cat "$BOOTSTRAP_RUNTIME/pids/control-plane.pid")" = "$bootstrap_control_pid" ] \
  || fail 'rejected bootstrap restart must not stop the existing process'
if SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$TMP/bootstrap-interactive-runtime" \
  "$ROOT/scripts/r6-basic-stack.sh" start --bootstrap-akashic \
    --manifest "$MANIFEST" >/dev/null 2>&1; then
  fail 'Akashic bootstrap must reject interactive mode'
fi
if env SIDERA_R6_BASIC_RUNTIME_DIR="$TMP/bootstrap-read-only-runtime" \
  "$ROOT/scripts/r6-basic-stack.sh" start --bootstrap-akashic \
    --manifest "$READ_ONLY_MANIFEST" >/dev/null 2>&1; then
  fail 'Akashic bootstrap must reject read-only restore mode'
fi
SIDERA_R6_BASIC_RUNTIME_DIR="$BOOTSTRAP_RUNTIME" \
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
stop_fake_akashic

if SIDERA_R6_BASIC_STUB_COMMAND="$UNREADY_STUB" \
  "$ROOT/scripts/r6-basic-stack.sh" start \
    --manifest "$MANIFEST" \
    --component akashic-acceptance >/dev/null 2>&1; then
  fail 'an alive component that never becomes ready must fail startup'
fi
if [ -f "$RUNTIME/pids/akashic-acceptance.pid" ]; then
  UNREADY_PID=$(cat "$RUNTIME/pids/akashic-acceptance.pid")
fi
[ ! -f "$RUNTIME/pids/akashic-acceptance.pid" ] \
  || fail 'an unready component must not leave a pid or process behind'

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
expected_result_repository_ref=$("$UV_BIN" run --directory "$ROOT/services/control-plane" --frozen python - "$REPOSITORY" <<'PY'
import sys
from pathlib import Path

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref

print(opaque_repository_ref(Path(sys.argv[1])))
PY
)
assert_contains "SIDERA_RESULT_REPOSITORY_REF=$expected_result_repository_ref" "$capture_env" 'result storage lookup must use the repository capture ref'
assert_contains 'SIDERA_R6_BASIC_NOTIFICATION_BASELINE_REQUIRED=true' "$capture_env" 'baseline flag must reach workers'
assert_contains 'SIDERA_TEST_DB=' "$capture_env" 'selected database ref must reach workers'
assert_contains 'SIDERA_CREDENTIAL_STORE_READY=1' "$capture_env" 'credential-store readiness must reach workers'
assert_contains 'SIDERA_ACCEPTANCE_SCENARIO=revision' "$capture_env" 'acceptance scenario must reach workers'
assert_contains 'SIDERA_ACCEPTANCE_GROUP=B' "$capture_env" 'acceptance group must reach workers'
assert_contains 'SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=300' "$capture_env" 'planning ACP timeout must reach workers'
assert_contains 'SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS=300' "$capture_env" 'worker ACP timeout must reach workers'
assert_not_contains 'DEEPSEEK_API_KEY=' "$capture_env" 'ambient provider secrets must not reach workers'
assert_not_contains 'SIDERA_R6_BASIC_RUN_ID=' "$capture_env" 'simulation must not enable the acceptance guard'
assert_not_contains 'SIDERA_ACCEPTANCE_MANIFEST=' "$capture_env" 'simulation must not receive a guard manifest'
assert_not_contains 'SIDERA_ACCEPTANCE_GUARD_REQUIRED=' "$capture_env" 'simulation must not require a guard'

python3 - "$GUARD_MANIFEST" "$GUARD_RUNTIME" "$REPOSITORY" "$BASE_COMMIT" <<'PY'
import json
import sys
from pathlib import Path

manifest_path, run_root, repository, base_commit = sys.argv[1:]
run_root = Path(run_root)
run_root.mkdir(parents=True)
run_id = "10000000-0000-0000-0000-000000000201"
workspace_id = "10000000-0000-0000-0000-000000000202"
approval = run_root / "approval.json"
routes = [
    {
        "route_id": "main-fake",
        "role": "main_agent",
        "runtime": "fake-http",
        "provider": "fake",
        "model_id": "fake-main-v1",
        "endpoint_host": "127.0.0.1:18101",
        "credential_ref": "env:SIDERA_TEST_MAIN_AGENT_KEY",
        "config_version": 1,
        "fallbacks": [],
        "costs": {"main_agent_turns": 1},
    },
    {
        "route_id": "planner-fake",
        "role": "planner",
        "runtime": "fake-acp",
        "provider": "fake",
        "model_id": "fake-planner-v1",
        "endpoint_host": "127.0.0.1:18102",
        "credential_ref": "env:SIDERA_TEST_PLANNER_KEY",
        "config_version": 1,
        "fallbacks": [],
        "costs": {"planner_runs": 1, "adapter_prompts": 1},
    },
    {
        "route_id": "worker-fake",
        "role": "worker",
        "runtime": "fake-acp",
        "provider": "fake",
        "model_id": "fake-worker-v1",
        "endpoint_host": "127.0.0.1:18103",
        "credential_ref": "env:SIDERA_TEST_WORKER_KEY",
        "config_version": 1,
        "fallbacks": [],
        "costs": {"worker_sessions": 1, "adapter_prompts": 1},
    },
    {
        "route_id": "second-worker-fake",
        "role": "second_worker",
        "runtime": "fake-acp",
        "provider": "fake",
        "model_id": "fake-second-v1",
        "endpoint_host": "127.0.0.1:18104",
        "credential_ref": "env:SIDERA_TEST_SECOND_WORKER_KEY",
        "config_version": 1,
        "fallbacks": [],
        "costs": {"adapter_prompts": 1},
    },
]
fingerprints = {
    "implementation_fingerprint": "a" * 64,
    "runtime_fingerprint": "b" * 64,
}
approval.write_text(
    json.dumps(
        {
            "schema_version": 1,
            "approved": True,
            "run_id": run_id,
            "workspace_id": workspace_id,
            **fingerprints,
        }
    ),
    encoding="utf-8",
)
scenario_limits = {
    "main_agent_turns": 1,
    "planner_runs": 1,
    "worker_sessions": 1,
    "adapter_prompts": 2,
    "max_seconds": 60,
}
payload = {
    "schema_version": 2,
    "run_id": run_id,
    "workspace_id": workspace_id,
    "local_gate_record": str(Path(manifest_path).with_name("gate.json")),
    **fingerprints,
    "fingerprint_version": 2,
    "run_root": str(run_root),
    "maintenance_inventory_ref": str(run_root / "maintenance-inventory.json"),
    "repository": repository,
    "base_commit": base_commit,
    "database_ref": "env:SIDERA_TEST_DB",
    "knowledge_database_ref": "env:SIDERA_TEST_KNOWLEDGE_DB",
    "control_plane_url": "http://127.0.0.1:18111",
    "knowledge_url": "http://127.0.0.1:18112",
    "main_agent_gateway_url": "http://127.0.0.1:18113",
    "web_url": "http://127.0.0.1:18114",
    "profiles": [
        {
            "role": route["role"],
            "runtime": route["runtime"],
            "provider": route["provider"],
            "model_id": route["model_id"],
            "endpoint_host": route["endpoint_host"],
            "credential_ref": route["credential_ref"],
            "config_ref": "module:test/" + route["role"],
            "config_version": route["config_version"],
        }
        for route in routes
    ],
    "scenarios": {"profile": scenario_limits},
    "batch_limits": {
        "main_agent_turns": 1,
        "planner_runs": 1,
        "worker_sessions": 1,
        "adapter_prompts": 2,
    },
    "allowed_routes": routes,
    "approval_ref": str(approval),
    "resource_binding_mode": "persisted",
    "restore_mode": None,
}
Path(manifest_path).write_text(json.dumps(payload), encoding="utf-8")
components = [
    {
        "component_id": "control-plane-postgres",
        "kind": "postgres",
        "source_ref": "postgres:ref:env:SIDERA_TEST_DB",
        "restore_relative_path": "databases/control-plane",
        "schema_identity": "migration:control-plane",
        "required": True,
        "secret_policy": "private_data",
    },
    {
        "component_id": "knowledge-postgres",
        "kind": "postgres",
        "source_ref": "postgres:ref:env:SIDERA_TEST_KNOWLEDGE_DB",
        "restore_relative_path": "databases/knowledge",
        "schema_identity": "migration:knowledge-service",
        "required": True,
        "secret_policy": "private_data",
    },
]
(run_root / "maintenance-inventory.json").write_text(
    json.dumps(
        {
            "schema_version": 1,
            "run_id": run_id,
            "workspace_id": workspace_id,
            "code_commit": base_commit,
            **fingerprints,
            "writers": {
                "status": "stopped",
                "active_component_ids": [],
                "observed_at": "2026-09-18T00:00:00+00:00",
            },
            "persistence": {
                "status": "incomplete",
                "component_ids": [item["component_id"] for item in components],
                "unsupported_component_ids": [item["component_id"] for item in components],
            },
            "components": components,
            "source_identities": {
                "database_ref": "env:SIDERA_TEST_DB",
                "knowledge_database_ref": "env:SIDERA_TEST_KNOWLEDGE_DB",
            },
        }
    ),
    encoding="utf-8",
)
PY

export SIDERA_TEST_MAIN_AGENT_KEY='test-only-main-agent-credential'
export SIDERA_TEST_PLANNER_KEY='test-only-planner-credential'
export SIDERA_TEST_WORKER_KEY='test-only-worker-credential'
export SIDERA_TEST_SECOND_WORKER_KEY='test-only-second-worker-credential'

if env \
  SIDERA_R6_BASIC_MODE=typo \
  SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_TEST_CAPTURE="$INTERACTIVE_CAPTURE" \
  "$ROOT/scripts/r6-basic-stack.sh" start \
    --manifest "$GUARD_MANIFEST" \
    --component main-agent-gateway >/dev/null 2>&1; then
  invalid_mode_status=0
else
  invalid_mode_status=$?
fi
[ "$invalid_mode_status" -eq 64 ] \
  || fail "unknown basic stack mode must fail with exit 64 (got $invalid_mode_status)"
[ ! -f "$INTERACTIVE_RUNTIME/pids/main-agent-gateway.pid" ] \
  || fail 'invalid mode must not start a process'

cp "$GUARD_MANIFEST" "$BAD_GUARD_MANIFEST"
python3 - "$BAD_GUARD_MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["approval_ref"] = str(path.parent / "missing-approval.json")
path.write_text(json.dumps(payload), encoding="utf-8")
PY
if SIDERA_R6_BASIC_RUNTIME_DIR="$BAD_GUARD_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_TEST_CAPTURE="$TMP/bad-guard-capture" \
  "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$BAD_GUARD_MANIFEST" --component control-plane >/dev/null 2>&1; then
  SIDERA_R6_BASIC_RUNTIME_DIR="$BAD_GUARD_RUNTIME" \
    SIDERA_R6_BASIC_TEST_MODE=1 \
    "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$BAD_GUARD_MANIFEST" --component control-plane >/dev/null 2>&1 || true
  fail 'guarded stack must reject a missing approval before starting a consumer'
fi
[ ! -f "$BAD_GUARD_RUNTIME/pids/control-plane.pid" ] \
  || fail 'guarded stack must not leave a process identity after preflight rejection'

"$UV_BIN" run --directory "$ROOT/services/control-plane" --frozen python - "$GUARD_MANIFEST" <<'PY'
import sys
from pathlib import Path

from sidera_control_plane.basic_acceptance_manifest import load_manifest
from sidera_runtime_guard import RuntimeBudgetGuard, prepare_run

manifest_path = Path(sys.argv[1])
manifest = load_manifest(manifest_path)
assert manifest.schema_version == 2
prepared = prepare_run(manifest_path)
assert prepared["status"] == "prepared"
guard = RuntimeBudgetGuard(manifest_path)
assert guard.run_id == "10000000-0000-0000-0000-000000000201"
PY

export SIDERA_R6_BASIC_RUNTIME_DIR="$GUARD_RUNTIME"
export SIDERA_R6_BASIC_TEST_CAPTURE="$GUARD_CAPTURE"
export SIDERA_R6_BASIC_TEST_AKASHIC_BRIDGE_READY=1
start_fake_akashic "$GUARD_RUNTIME"
guard_manifest_resolved=$(python3 - "$GUARD_MANIFEST" <<'PY'
import sys
from pathlib import Path

print(Path(sys.argv[1]).expanduser().resolve())
PY
)
for guarded_component in control-plane planning-worker gateway-worker collaboration-worker main-agent-gateway akashic-acceptance; do
  "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$GUARD_MANIFEST" --component "$guarded_component" >/dev/null
  guard_capture_env=$(cat "$GUARD_CAPTURE/$guarded_component.env")
  assert_contains "SIDERA_R6_BASIC_RUN_ID=10000000-0000-0000-0000-000000000201" "$guard_capture_env" "acceptance run id must reach $guarded_component"
  assert_contains "SIDERA_ACCEPTANCE_MANIFEST=$guard_manifest_resolved" "$guard_capture_env" "absolute guard manifest must reach $guarded_component"
  assert_contains 'SIDERA_ACCEPTANCE_GUARD_REQUIRED=1' "$guard_capture_env" "guard requirement must reach $guarded_component"
  assert_contains 'SIDERA_ACCEPTANCE_PLANNER_ROUTE_ID=planner-fake' "$guard_capture_env" "planner route must reach $guarded_component"
  assert_contains 'SIDERA_ACCEPTANCE_WORKER_SESSION_ROUTE_ID=worker-fake' "$guard_capture_env" "worker session route must reach $guarded_component"
  assert_contains 'SIDERA_ACCEPTANCE_WORKER_PROMPT_ROUTE_ID=second-worker-fake' "$guard_capture_env" "worker prompt route must reach $guarded_component"
  assert_contains 'SIDERA_ACCEPTANCE_ROUTE_FACTS=' "$guard_capture_env" "resolved route facts must reach $guarded_component"
  assert_contains '"provider":"fake"' "$guard_capture_env" "route facts must be secret-free and provider-specific"
  case "$guarded_component" in
    akashic-acceptance)
      assert_contains 'SIDERA_TEST_MAIN_AGENT_KEY=test-only-main-agent-credential' "$guard_capture_env" 'main-agent credential must reach only its runtime process'
      assert_not_contains 'SIDERA_TEST_PLANNER_KEY=' "$guard_capture_env" 'main-agent process must not receive planner credentials'
      assert_not_contains 'SIDERA_TEST_WORKER_KEY=' "$guard_capture_env" 'main-agent process must not receive worker credentials'
      assert_not_contains 'SIDERA_TEST_SECOND_WORKER_KEY=' "$guard_capture_env" 'main-agent process must not receive second-worker credentials'
      ;;
    planning-worker)
      assert_contains 'SIDERA_TEST_PLANNER_KEY=test-only-planner-credential' "$guard_capture_env" 'planner credential must reach only its runtime process'
      assert_not_contains 'SIDERA_TEST_MAIN_AGENT_KEY=' "$guard_capture_env" 'planner process must not receive main-agent credentials'
      assert_not_contains 'SIDERA_TEST_WORKER_KEY=' "$guard_capture_env" 'planner process must not receive worker credentials'
      assert_not_contains 'SIDERA_TEST_SECOND_WORKER_KEY=' "$guard_capture_env" 'planner process must not receive second-worker credentials'
      ;;
    gateway-worker)
      assert_contains 'SIDERA_TEST_WORKER_KEY=test-only-worker-credential' "$guard_capture_env" 'worker credential must reach the worker runtime process'
      assert_contains 'SIDERA_TEST_SECOND_WORKER_KEY=test-only-second-worker-credential' "$guard_capture_env" 'second-worker credential must reach the worker runtime process'
      assert_not_contains 'SIDERA_TEST_MAIN_AGENT_KEY=' "$guard_capture_env" 'worker process must not receive main-agent credentials'
      assert_not_contains 'SIDERA_TEST_PLANNER_KEY=' "$guard_capture_env" 'worker process must not receive planner credentials'
      ;;
    *)
      assert_not_contains 'SIDERA_TEST_MAIN_AGENT_KEY=' "$guard_capture_env" "$guarded_component must not receive main-agent credentials"
      assert_not_contains 'SIDERA_TEST_PLANNER_KEY=' "$guard_capture_env" "$guarded_component must not receive planner credentials"
      assert_not_contains 'SIDERA_TEST_WORKER_KEY=' "$guard_capture_env" "$guarded_component must not receive worker credentials"
      assert_not_contains 'SIDERA_TEST_SECOND_WORKER_KEY=' "$guard_capture_env" "$guarded_component must not receive second-worker credentials"
      ;;
  esac
  if [ "$guarded_component" = collaboration-worker ]; then
    assert_contains 'SIDERA_COLLABORATION_WORKSPACE_ID=10000000-0000-0000-0000-000000000202' "$guard_capture_env" "collaboration workspace must reach the collaboration worker"
  fi
  assert_not_contains 'DEEPSEEK_API_KEY=' "$guard_capture_env" "route facts must not inherit ambient provider keys"
  "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$GUARD_MANIFEST" --component "$guarded_component" >/dev/null
done
stop_fake_akashic

interactive_components="knowledge-service control-plane basic-worker-heartbeat akashic-acceptance planning-worker context-package-worker knowledge-candidate-worker activation-worker scheduler-worker reconciler-worker gateway-worker plan-transition-worker collaboration-worker notification-worker result-worker main-agent-gateway web"
export SIDERA_R6_BASIC_TEST_AKASHIC_BRIDGE_READY=1
start_fake_akashic "$INTERACTIVE_RUNTIME"
SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_STUB_COMMAND="$STUB" \
  SIDERA_R6_BASIC_TEST_CAPTURE="$INTERACTIVE_CAPTURE" \
  "$ROOT/scripts/r6-basic-stack.sh" start --manifest "$GUARD_MANIFEST" >/dev/null
interactive_status=$(SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_RUNTIME" \
  "$ROOT/scripts/r6-basic-stack.sh" status --manifest "$GUARD_MANIFEST")
for interactive_component in $interactive_components; do
  assert_contains "$interactive_component: running/ready" "$interactive_status" \
    "interactive component should be started"
done
assert_not_contains 'basic-acceptance-heartbeat:' "$interactive_status" \
  'interactive status must not require the acceptance heartbeat'
[ ! -f "$INTERACTIVE_RUNTIME/pids/basic-acceptance-heartbeat.pid" ] \
  || fail 'interactive mode must not start the acceptance heartbeat'
interactive_heartbeat_env=$(cat "$INTERACTIVE_CAPTURE/basic-worker-heartbeat.env")
interactive_akashic_env=$(cat "$INTERACTIVE_CAPTURE/akashic-acceptance.env")
interactive_akashic_identity=$(printf '%s\n' "$interactive_akashic_env" \
  | grep -E '^(SIDERA_R6_BASIC_COMPONENT|SIDERA_R6_BASIC_RUNTIME_DIR|AKASHIC_PLUGIN_HOME)=' \
  | tr '\n' ' ')
assert_contains "AKASHIC_PLUGIN_HOME=$TMP_REAL/interactive-runtime/akashic-plugin-home" \
  "$interactive_akashic_env" \
  "interactive Akashic must use a run-local plugin home (captured: $interactive_akashic_identity)"
assert_contains 'SIDERA_R6_BASIC_RUN_ID=10000000-0000-0000-0000-000000000201' "$interactive_heartbeat_env" \
  'interactive worker heartbeat must select only workers from this run'
assert_contains 'SIDERA_R6_BASIC_HEARTBEAT_COMPONENT=basic-worker-heartbeat' "$interactive_heartbeat_env" \
  'interactive worker heartbeat must report its non-acceptance component identity'
assert_not_contains 'SIDERA_ACCEPTANCE_MANIFEST=' "$interactive_heartbeat_env" \
  'interactive worker heartbeat must not receive a guard manifest'
assert_not_contains 'SIDERA_ACCEPTANCE_GUARD_REQUIRED=' "$interactive_heartbeat_env" \
  'interactive worker heartbeat must not require an acceptance guard'
assert_not_contains 'SIDERA_ACCEPTANCE_ROUTE_FACTS=' "$interactive_heartbeat_env" \
  'interactive worker heartbeat must not receive acceptance route facts'
for interactive_guarded_component in control-plane planning-worker gateway-worker collaboration-worker main-agent-gateway; do
  interactive_capture_env=$(cat "$INTERACTIVE_CAPTURE/$interactive_guarded_component.env")
  assert_contains 'SIDERA_RESULT_INPUTS_ENABLED=1' "$interactive_capture_env" \
    "interactive mode must enable result-input contracts for $interactive_guarded_component"
  assert_contains 'SIDERA_RESULT_INTEGRATION_ENABLED=1' "$interactive_capture_env" \
    "interactive mode must enable result integration for $interactive_guarded_component"
  assert_not_contains 'SIDERA_R6_BASIC_RUN_ID=' "$interactive_capture_env" \
    "interactive mode must not inject a run id into $interactive_guarded_component"
  assert_not_contains 'SIDERA_ACCEPTANCE_MANIFEST=' "$interactive_capture_env" \
    "interactive mode must not inject a guard manifest into $interactive_guarded_component"
  assert_not_contains 'SIDERA_ACCEPTANCE_GUARD_REQUIRED=' "$interactive_capture_env" \
    "interactive mode must not require a guard in $interactive_guarded_component"
  assert_not_contains 'SIDERA_ACCEPTANCE_ROUTE_FACTS=' "$interactive_capture_env" \
    "interactive mode must not inject route facts into $interactive_guarded_component"
  for interactive_route_var in \
    SIDERA_ACCEPTANCE_MAIN_AGENT_ROUTE_ID \
    SIDERA_ACCEPTANCE_PLANNER_ROUTE_ID \
    SIDERA_ACCEPTANCE_WORKER_SESSION_ROUTE_ID \
    SIDERA_ACCEPTANCE_WORKER_PROMPT_ROUTE_ID \
    SIDERA_ACCEPTANCE_SECOND_WORKER_ROUTE_ID; do
    assert_not_contains "$interactive_route_var=" "$interactive_capture_env" \
      "interactive mode must not inject $interactive_route_var into $interactive_guarded_component"
  done
done
SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_RUNTIME" \
  "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$GUARD_MANIFEST" >/dev/null
stop_fake_akashic
unset SIDERA_R6_BASIC_TEST_AKASHIC_BRIDGE_READY

start_fake_akashic "$INTERACTIVE_UNREADY_RUNTIME"
if SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_UNREADY_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_STUB_COMMAND="$UNREADY_STUB" \
  SIDERA_R6_BASIC_TEST_CAPTURE="$INTERACTIVE_CAPTURE" \
  "$ROOT/scripts/r6-basic-stack.sh" start \
    --manifest "$GUARD_MANIFEST" \
    --component akashic-acceptance > "$TMP/interactive-akashic-readiness.log"; then
  assert_contains 'akashic-acceptance: running/ready' \
    "$(cat "$TMP/interactive-akashic-readiness.log")" \
    'interactive Akashic should become ready from its live control socket'
else
  fail 'interactive Akashic must use server/status readiness when its process log has no ready marker'
fi
assert_contains '"diagnostic":"never_ready"' \
  "$(cat "$INTERACTIVE_UNREADY_RUNTIME/logs/akashic-acceptance.log")" \
  'interactive readiness fixture must omit the structured ready marker'
SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_UNREADY_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_STUB_COMMAND="$UNREADY_STUB" \
  "$ROOT/scripts/r6-basic-stack.sh" stop \
    --manifest "$GUARD_MANIFEST" \
    --component akashic-acceptance >/dev/null
stop_fake_akashic

start_fake_akashic "$INTERACTIVE_RUNTIME"
SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_STUB_COMMAND="$STUB" \
  SIDERA_R6_BASIC_TEST_CAPTURE="$INTERACTIVE_CAPTURE" \
  "$ROOT/scripts/r6-basic-stack.sh" start \
    --manifest "$GUARD_MANIFEST" \
    --component basic-acceptance-heartbeat >/dev/null
if SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_STUB_COMMAND="$STUB" \
  SIDERA_R6_BASIC_TEST_CAPTURE="$INTERACTIVE_CAPTURE" \
  "$ROOT/scripts/r6-basic-stack.sh" restart --manifest "$GUARD_MANIFEST" >/dev/null 2>&1; then
  :
else
  fail 'interactive transition restart must succeed'
fi
[ ! -f "$INTERACTIVE_RUNTIME/pids/basic-acceptance-heartbeat.pid" ] \
  || fail 'interactive transition must stop the acceptance heartbeat'
if SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_RUNTIME" \
  SIDERA_R6_BASIC_TEST_MODE=1 \
  SIDERA_R6_BASIC_STUB_COMMAND="$STUB" \
  SIDERA_R6_BASIC_TEST_CAPTURE="$INTERACTIVE_CAPTURE" \
  "$ROOT/scripts/r6-basic-stack.sh" start \
    --manifest "$GUARD_MANIFEST" \
    --component basic-acceptance-heartbeat >/dev/null 2>&1; then
  fail 'interactive mode must refuse a direct acceptance heartbeat start'
fi
[ ! -f "$INTERACTIVE_RUNTIME/pids/basic-acceptance-heartbeat.pid" ] \
  || fail 'interactive heartbeat refusal must not leave a pid'
SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$INTERACTIVE_RUNTIME" \
  "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$GUARD_MANIFEST" >/dev/null

ARGV_MANIFEST="$TMP/argv-manifest.json"
ARGV_RUNTIME="$TMP/argv-runtime"
ARGV_LOG="$FAKE_BIN/argv-uv.log"
ARGV_UV="$FAKE_BIN/argv-uv"
ARGV_CURL="$FAKE_BIN/argv-curl"
ARGV_PORTS="$TMP/argv-ports"
python3 - "$MANIFEST" "$ARGV_MANIFEST" "$ARGV_PORTS" <<'PY'
import json
import socket
import sys
from pathlib import Path

source, target, ports_path = sys.argv[1:]
reservations = []
for _ in range(2):
    reservation = socket.socket()
    reservation.bind(("127.0.0.1", 0))
    reservations.append(reservation)
ports = [reservation.getsockname()[1] for reservation in reservations]
manifest = json.loads(Path(source).read_text(encoding="utf-8"))
manifest["control_plane_url"] = f"http://127.0.0.1:{ports[0]}"
manifest["main_agent_gateway_url"] = f"http://127.0.0.1:{ports[1]}"
Path(target).write_text(json.dumps(manifest), encoding="utf-8")
Path(ports_path).write_text("\n".join(str(port) for port in ports), encoding="utf-8")
for reservation in reservations:
    reservation.close()
PY
ARGV_CONTROL_PORT=$(sed -n '1p' "$ARGV_PORTS")
ARGV_GATEWAY_PORT=$(sed -n '2p' "$ARGV_PORTS")
cat > "$ARGV_UV" <<'EOF'
#!/bin/sh
fake_bin=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
printf '%s:adapter_kind=%s\n' "$*" "${SIDERA_MAIN_AGENT_ADAPTER_KIND:-<unset>}" >> "$fake_bin/argv-uv.log"
trap 'exit 0' TERM INT HUP
while :; do sleep 1; done
EOF
printf '%s\n' '#!/bin/sh' 'exit 0' > "$ARGV_CURL"
chmod +x "$ARGV_UV" "$ARGV_CURL"

run_argv_stack_component() {
  component=$1
  env -u SIDERA_R6_BASIC_TEST_MODE -u SIDERA_R6_BASIC_STUB_COMMAND \
    SIDERA_R6_BASIC_MODE=interactive \
    SIDERA_R6_BASIC_RUNTIME_DIR="$ARGV_RUNTIME" \
    SIDERA_UV_BIN="$ARGV_UV" \
    SIDERA_CURL_BIN="$ARGV_CURL" \
    "$ROOT/scripts/r6-basic-stack.sh" start \
    --manifest "$ARGV_MANIFEST" --component "$component" \
    > "$TMP/argv-$component.out"
}

run_argv_stack_component control-plane
run_argv_stack_component main-agent-gateway
ARGV_CONTENT=$(cat "$ARGV_LOG")
assert_contains 'run --project '"$ROOT"'/services/main-agent-gateway --extra akashic --extra demo uvicorn sidera_main_agent_gateway.api.app:create_app --factory --host 127.0.0.1 --port '"$ARGV_GATEWAY_PORT"':adapter_kind=<unset>' \
  "$ARGV_CONTENT" 'complete-product Gateway command must include both extras and retain its HTTP arguments'
assert_contains 'run --project '"$ROOT"'/services/control-plane uvicorn sidera_control_plane.api.app:create_app --factory --host 127.0.0.1 --port '"$ARGV_CONTROL_PORT"':adapter_kind=<unset>' \
  "$ARGV_CONTENT" 'Control Plane launch must retain its original argv without Gateway extras'
CONTROL_ARGV=$(printf '%s\n' "$ARGV_CONTENT" | grep -F 'services/control-plane')
assert_not_contains '--extra' "$CONTROL_ARGV" \
  'Control Plane must not receive Gateway extras'
assert_not_contains 'adapter_kind=unconfigured' "$ARGV_CONTENT" \
  'complete-product startup must not silently force an unconfigured Gateway'
env -u SIDERA_R6_BASIC_TEST_MODE -u SIDERA_R6_BASIC_STUB_COMMAND \
  SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$ARGV_RUNTIME" \
  "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$ARGV_MANIFEST" \
  --component main-agent-gateway >/dev/null
env -u SIDERA_R6_BASIC_TEST_MODE -u SIDERA_R6_BASIC_STUB_COMMAND \
  SIDERA_R6_BASIC_MODE=interactive \
  SIDERA_R6_BASIC_RUNTIME_DIR="$ARGV_RUNTIME" \
  "$ROOT/scripts/r6-basic-stack.sh" stop --manifest "$ARGV_MANIFEST" \
  --component control-plane >/dev/null

printf 'PASS: r6 basic stack lifecycle, ownership, env isolation, and rollback\n'
