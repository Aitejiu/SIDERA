#!/bin/bash
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ORIGINAL_PATH=$PATH
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r5-3-stack-test.XXXXXX")
FAKE_BIN=$TMP/bin
ENV_FILE=$TMP/r5-3.local.env
RUNTIME_DIR=$TMP/runtime
AKASHIC_DIR=$TMP/akashic
mkdir -p "$FAKE_BIN" "$RUNTIME_DIR/candidate-spool" "$AKASHIC_DIR/workspace" "$TMP/capture"
chmod 700 "$RUNTIME_DIR" "$RUNTIME_DIR/candidate-spool" "$AKASHIC_DIR" "$AKASHIC_DIR/workspace"
touch "$AKASHIC_DIR/config.toml"
chmod 600 "$AKASHIC_DIR/config.toml"

cleanup() {
  PATH="$FAKE_BIN:$ORIGINAL_PATH" \
    SIDERA_R5_3_ENV_FILE="$ENV_FILE" \
    SIDERA_R5_3_RUNTIME_DIR="$RUNTIME_DIR" \
    bash "$ROOT/scripts/r5-3-stack.sh" stop >/dev/null 2>&1 || true
  if [ -n "${UNRELATED_PID:-}" ]; then
    kill "$UNRELATED_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT HUP INT TERM

fail() {
  printf 'r5-3 stack test: %s\n' "$1" >&2
  exit 1
}

assert_file() {
  [ -f "$1" ] || fail "expected file is missing: $2"
}

assert_contains() {
  grep -F "$1" "$2" >/dev/null 2>&1 || fail "expected text is missing: $3"
}

assert_not_contains() {
  if grep -F "$1" "$2" >/dev/null 2>&1; then
    fail "unexpected text is present: $3"
  fi
}

assert_pid_alive() {
  kill -0 "$1" >/dev/null 2>&1 || fail "expected process is not alive: $2"
}

assert_pid_stopped() {
  if kill -0 "$1" >/dev/null 2>&1; then
    fail "process is still alive: $2"
  fi
}

pid_for() {
  sed -n '1p' "$RUNTIME_DIR/pids/$1.pid"
}

write_env() {
  printf '%s\n' \
    'SIDERA_DATABASE_URL=postgresql://127.0.0.1:55432/sidera' \
    'SIDERA_R5_3_TEST_DATABASE_URL=postgresql://127.0.0.1:55432/sidera_r5_3_test' \
    'SIDERA_KNOWLEDGE_DATABASE_URL=postgresql://127.0.0.1:55432/sidera_knowledge' \
    'SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL=postgresql://127.0.0.1:55432/sidera_knowledge_r5_3_test' \
    'SIDERA_WORKSPACE_ID=00000000-0000-0000-0000-000000000001' \
    'SIDERA_CONTROL_PLANE_URL=http://127.0.0.1:8000' \
    'SIDERA_KNOWLEDGE_BASE_URL=http://127.0.0.1:8020' \
    'SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN=fake-knowledge-local-owner-token' \
    'SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN=fake-knowledge-control-plane-token' \
    'SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=fake-knowledge-main-agent-token' \
    'SIDERA_MAIN_AGENT_GATEWAY_URL=http://127.0.0.1:8010' \
    'SIDERA_WEB_URL=http://127.0.0.1:4173' \
    'SIDERA_CONTEXT_PACKAGE_CUTOVER_AT=2026-08-26T00:00:00Z' \
    "SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR=$RUNTIME_DIR/candidate-spool" \
    'SIDERA_AKASHIC_ENDPOINT=127.0.0.1:9000' \
    'SIDERA_AKASHIC_WORKSPACE_TOKEN=fake-akashic-workspace-token' \
    "SIDERA_AKASHIC_WORKSPACE=$AKASHIC_DIR/workspace" \
    "SIDERA_AKASHIC_CONFIG=$AKASHIC_DIR/config.toml" \
    "SIDERA_OPENCODE_EXECUTABLE=$TMP/opencode" \
    'SIDERA_OPENCODE_EXPECTED_VERSION=1.18.19' \
    'SIDERA_OPENCODE_MODEL=opencode-go/MiMo-V2.5' \
    'SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=300' \
    'SIDERA_PLANNING_PROFILE_ID=00000000-0000-0000-0000-000000000002' \
    'SIDERA_PLANNING_PROFILE_VERSION=1' > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

write_fake_commands() {
  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--prefix" ] && [ "${2:-}" = "postgresql@16" ]; then' \
    '  printf "/opt/homebrew/opt/postgresql@16\n"' \
    '  exit 0' \
    'fi' \
    'exit 1' > "$FAKE_BIN/brew"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then' \
    '  printf "v24.19.0\n"' \
    'else' \
    '  printf "v24.19.0\n"' \
    'fi' > "$FAKE_BIN/node"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then' \
    '  printf "npm 11.6.0\n"' \
    'else' \
    '  printf "npm 11.6.0\n"' \
    'fi' > "$FAKE_BIN/npm-version"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then' \
    '  printf "Python 3.12.14\n"' \
    '  exit 0' \
    'fi' \
    'if [ "${1:-}" = "-c" ]; then' \
    '  shift 2' \
    '  launch_env=$1' \
    '  shift' \
    '  command_name=$1' \
    '  shift' \
    '  for key in SIDERA_DATABASE_URL SIDERA_KNOWLEDGE_DATABASE_URL SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR SIDERA_CONTEXT_PACKAGE_CUTOVER_AT SIDERA_KNOWLEDGE_BASE_URL SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID SIDERA_MAIN_AGENT_WEB_ORIGIN SIDERA_WEB_KNOWLEDGE_TOKEN SIDERA_PLANNING_OPENCODE_EXECUTABLE SIDERA_PLANNING_OPENCODE_MODEL SIDERA_PLANNING_OPENCODE_EXPECTED_VERSION SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS DEEPSEEK_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY OPENCODE_API_KEY; do' \
    '    unset "$key"' \
    '  done' \
    '  while IFS= read -r line || [ -n "$line" ]; do' \
    '    case "$line" in' \
    '      *=*) key=${line%%=*}; value=${line#*=}; export "$key=$value" ;;' \
    '    esac' \
    '  done < "$launch_env"' \
    '  rm -f "$launch_env"' \
    '  exec "$command_name" "$@"' \
    'fi' \
    'exit 1' > "$FAKE_BIN/python3"

  printf '%s\n' \
    '#!/bin/sh' \
    'printf "uv 0.6.0\n"' > "$FAKE_BIN/uv-version"

  printf '%s\n' \
    '#!/bin/sh' \
    'printf "\n"' > "$FAKE_BIN/lsof"

  printf '%s\n' \
    '#!/bin/sh' \
    'exit 0' > "$FAKE_BIN/pg_isready"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then' \
    '  printf "opencode 1.18.19\n"' \
    '  exit 0' \
    'fi' \
    'exit 0' > "$TMP/opencode"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then' \
    '  printf "curl 8.0.0\n"' \
    '  exit 0' \
    'fi' \
    'url=""' \
    'for argument in "$@"; do' \
    '  case "$argument" in' \
    '    http://*) url=$argument ;;' \
    '  esac' \
    'done' \
    'fake_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)' \
    'case "$url" in' \
    '  */main-agent/v1/health) grep -F "main-agent-gateway" "$fake_root/start.log" >/dev/null 2>&1 || exit 7; printf "{\"provider\":\"available\"}\n" ;;' \
    '  */health/ready) grep -F "knowledge-service" "$fake_root/start.log" >/dev/null 2>&1 || exit 7; printf "{}\n" ;;' \
    '  */healthz) grep -F "control-plane" "$fake_root/start.log" >/dev/null 2>&1 || exit 7; printf "{}\n" ;;' \
    '  */) grep -F "web" "$fake_root/start.log" >/dev/null 2>&1 || exit 7; printf "{}\n" ;;' \
    '  *) printf "{}\n" ;;' \
    'esac' > "$FAKE_BIN/curl"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then' \
    '  printf "npm 11.6.0\n"' \
    '  exit 0' \
    'fi' \
    'fake_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)' \
    'case "$*" in' \
    '  *"run dev"*)' \
    '    component=web' \
    '    out="$fake_root/capture/$component.env"' \
    '    : > "$out"' \
    '    for key in SIDERA_DATABASE_URL SIDERA_KNOWLEDGE_DATABASE_URL SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR SIDERA_CONTEXT_PACKAGE_CUTOVER_AT SIDERA_KNOWLEDGE_BASE_URL SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID SIDERA_MAIN_AGENT_WEB_ORIGIN SIDERA_WEB_KNOWLEDGE_TOKEN SIDERA_PLANNING_OPENCODE_EXECUTABLE SIDERA_PLANNING_OPENCODE_MODEL SIDERA_PLANNING_OPENCODE_EXPECTED_VERSION SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS DEEPSEEK_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY OPENCODE_API_KEY; do' \
    '      value=$(printenv "$key" 2>/dev/null || true)' \
    '      [ -n "$value" ] || value="<unset>"' \
    '      printf "%s=%s\n" "$key" "$value" >> "$out"' \
    '    done' \
    '    printf "%s\n" "$component" >> "$fake_root/start.log"' \
    '    printf "{\"component\":\"%s\",\"status\":\"ready\"}\n" "$component"' \
    '    trap "exit 0" TERM INT HUP' \
    '    while :; do sleep 1; done' \
    '    ;;' \
    'esac' > "$FAKE_BIN/npm"

  printf '%s\n' \
    '#!/bin/sh' \
    'fake_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)' \
    'if [ "${1:-}" = "--version" ]; then' \
    '  printf "uv 0.6.0\n"' \
    '  exit 0' \
    'fi' \
    'case "$*" in' \
    '  *"python --version"*) printf "Python 3.12.14\n"; exit 0 ;;' \
    '  *services/knowledge-service*"alembic"*"current"*) printf "0001_knowledge_foundation (head)\n"; exit 0 ;;' \
    '  *services/knowledge-service*"alembic"*"heads"*) printf "0001_knowledge_foundation (head)\n"; exit 0 ;;' \
    '  *"alembic"*"current"*) printf "0016_r5_3_work_context_packages (head)\n"; exit 0 ;;' \
    '  *"alembic"*"heads"*) printf "0016_r5_3_work_context_packages (head)\n"; exit 0 ;;' \
    'esac' \
    'component=""' \
    'case "$*" in' \
    '  *sidera_knowledge.api.app:app*) component=knowledge-service ;;' \
    '  *sidera_control_plane.api.app:create_app*) component=control-plane ;;' \
    '  *sidera_control_plane.context_package_worker*) component=context-package-worker ;;' \
    '  *sidera_control_plane.knowledge_candidate_worker*) component=knowledge-candidate-worker ;;' \
    '  *sidera_control_plane.planning_worker*) component=planning-worker ;;' \
    '  *sidera_control_plane.activation_worker*) component=activation-worker ;;' \
    '  *sidera_control_plane.scheduler_worker*) component=scheduler-worker ;;' \
    '  *sidera_control_plane.gateway_worker*) component=gateway-worker ;;' \
    '  *sidera_main_agent_gateway.api.app:create_app*) component=main-agent-gateway ;;' \
    'esac' \
    '[ -n "$component" ] || exit 0' \
    'if [ -f "$fake_root/fail-component" ] && [ "$(sed -n "1p" "$fake_root/fail-component")" = "$component" ]; then' \
    '  exit 42' \
    'fi' \
    'out="$fake_root/capture/$component.env"' \
    ': > "$out"' \
    'for key in SIDERA_DATABASE_URL SIDERA_KNOWLEDGE_DATABASE_URL SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR SIDERA_CONTEXT_PACKAGE_CUTOVER_AT SIDERA_KNOWLEDGE_BASE_URL SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID SIDERA_MAIN_AGENT_WEB_ORIGIN SIDERA_WEB_KNOWLEDGE_TOKEN SIDERA_PLANNING_OPENCODE_EXECUTABLE SIDERA_PLANNING_OPENCODE_MODEL SIDERA_PLANNING_OPENCODE_EXPECTED_VERSION SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS DEEPSEEK_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY OPENCODE_API_KEY; do' \
    '  value=$(printenv "$key" 2>/dev/null || true)' \
    '  [ -n "$value" ] || value="<unset>"' \
    '  printf "%s=%s\n" "$key" "$value" >> "$out"' \
    'done' \
    'printf "%s\n" "$component" >> "$fake_root/start.log"' \
    'printf "{\"component\":\"%s\",\"status\":\"ready\"}\n" "$component"' \
    'trap "exit 0" TERM INT HUP' \
    'while :; do sleep 1; done' > "$FAKE_BIN/uv"

  chmod +x "$FAKE_BIN"/* "$TMP/opencode"
}

stack() {
  PATH="$FAKE_BIN:$ORIGINAL_PATH" \
    SIDERA_R5_3_ENV_FILE="$ENV_FILE" \
    SIDERA_R5_3_RUNTIME_DIR="$RUNTIME_DIR" \
    DEEPSEEK_API_KEY=deepseek-secret \
    OPENAI_API_KEY=openai-secret \
    ANTHROPIC_API_KEY=anthropic-secret \
    OPENCODE_API_KEY=opencode-secret \
    bash "$ROOT/scripts/r5-3-stack.sh" "$@"
}

write_env
write_fake_commands

stack start > "$TMP/start.out"

EXPECTED_ORDER=$TMP/expected-order
printf '%s\n' \
  knowledge-service \
  control-plane \
  context-package-worker \
  knowledge-candidate-worker \
  planning-worker \
  activation-worker \
  scheduler-worker \
  gateway-worker \
  main-agent-gateway \
  web > "$EXPECTED_ORDER"
sed -n '1,10p' "$TMP/start.log" > "$TMP/actual-order"
cmp -s "$EXPECTED_ORDER" "$TMP/actual-order" || fail "component startup order is incorrect"
if find "$RUNTIME_DIR/launch-env" -type f -print | grep . >/dev/null 2>&1; then
  fail 'launch environment file was not consumed'
fi

for component in knowledge-service control-plane context-package-worker knowledge-candidate-worker planning-worker activation-worker scheduler-worker gateway-worker main-agent-gateway web; do
  assert_file "$RUNTIME_DIR/pids/$component.pid" "$component pid"
  assert_file "$RUNTIME_DIR/identity/$component.identity" "$component identity"
  assert_file "$RUNTIME_DIR/logs/$component.log" "$component log"
  assert_contains "component=$component" "$RUNTIME_DIR/identity/$component.identity" "$component identity component"
  assert_contains "repo=$ROOT" "$RUNTIME_DIR/identity/$component.identity" "$component identity repo"
  assert_contains "marker=sidera-r5-3:$component" "$RUNTIME_DIR/identity/$component.identity" "$component identity marker"
  assert_contains 'status":"ready' "$RUNTIME_DIR/logs/$component.log" "$component readiness"
  assert_pid_alive "$(pid_for "$component")" "$component"
done

for private_dir in "$RUNTIME_DIR" "$RUNTIME_DIR/pids" "$RUNTIME_DIR/logs" "$RUNTIME_DIR/identity" "$RUNTIME_DIR/launch-env"; do
  mode=$(stat -f '%Lp' "$private_dir" 2>/dev/null || stat -c '%a' "$private_dir")
  [ "$mode" = 700 ] || fail "runtime directory is not 0700: $private_dir"
done

STATUS=$TMP/status.out
stack status > "$STATUS"
assert_contains 'knowledge-service running ready' "$STATUS" 'knowledge service status'
assert_contains 'control-plane running ready' "$STATUS" 'control plane status'
assert_contains 'planning-worker running ready' "$STATUS" 'planning worker status'
assert_contains 'main-agent-gateway running ready' "$STATUS" 'main agent status'
assert_contains 'web running ready' "$STATUS" 'web status'

PLANNING_PID=$(pid_for planning-worker)
stack logs planning-worker > "$TMP/planning.log"
assert_contains 'planning-worker' "$TMP/planning.log" 'planning logs'

PIDS_BEFORE=$TMP/pids-before
for component in knowledge-service control-plane context-package-worker knowledge-candidate-worker planning-worker activation-worker scheduler-worker gateway-worker main-agent-gateway web; do
  printf '%s=%s\n' "$component" "$(pid_for "$component")" >> "$PIDS_BEFORE"
done
stack start > "$TMP/start-again.out"
for component in knowledge-service control-plane context-package-worker knowledge-candidate-worker planning-worker activation-worker scheduler-worker gateway-worker main-agent-gateway web; do
  before=$(sed -n "s/^$component=//p" "$PIDS_BEFORE")
  after=$(pid_for "$component")
  [ "$before" = "$after" ] || fail "idempotent start replaced $component"
done

stack restart planning-worker > "$TMP/restart.out"
NEW_PLANNING_PID=$(pid_for planning-worker)
[ "$NEW_PLANNING_PID" != "$PLANNING_PID" ] || fail 'restart did not replace planning worker pid'
assert_pid_stopped "$PLANNING_PID" 'old planning worker'
assert_pid_alive "$NEW_PLANNING_PID" 'new planning worker'

KS_CAPTURE=$TMP/capture/knowledge-service.env
CP_CAPTURE=$TMP/capture/control-plane.env
PLANNING_CAPTURE=$TMP/capture/planning-worker.env
GATEWAY_CAPTURE=$TMP/capture/main-agent-gateway.env
WEB_CAPTURE=$TMP/capture/web.env
assert_contains 'SIDERA_KNOWLEDGE_DATABASE_URL=postgresql://127.0.0.1:55432/sidera_knowledge' "$KS_CAPTURE" 'knowledge database isolation'
assert_contains 'SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN=fake-knowledge-local-owner-token' "$KS_CAPTURE" 'knowledge owner capability'
assert_contains 'SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN=fake-knowledge-control-plane-token' "$KS_CAPTURE" 'knowledge control plane capability'
assert_contains 'SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=fake-knowledge-main-agent-token' "$KS_CAPTURE" 'knowledge main agent capability'
assert_contains 'SIDERA_DATABASE_URL=<unset>' "$KS_CAPTURE" 'knowledge control plane database isolation'
assert_contains 'SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN=<unset>' "$CP_CAPTURE" 'control plane owner capability isolation'
assert_contains 'SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=<unset>' "$CP_CAPTURE" 'control plane main agent capability isolation'
assert_contains 'SIDERA_DATABASE_URL=postgresql://127.0.0.1:55432/sidera' "$CP_CAPTURE" 'control plane database'
assert_contains 'SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN=fake-knowledge-control-plane-token' "$CP_CAPTURE" 'control plane capability'
assert_contains 'SIDERA_PLANNING_OPENCODE_MODEL=opencode-go/MiMo-V2.5' "$PLANNING_CAPTURE" 'planning model configuration'
assert_contains 'SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=300' "$PLANNING_CAPTURE" 'planning request timeout'
assert_contains 'SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT=127.0.0.1:9000' "$GATEWAY_CAPTURE" 'gateway endpoint'
assert_contains 'SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN=fake-akashic-workspace-token' "$GATEWAY_CAPTURE" 'gateway workspace token'
assert_contains 'SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN=<unset>' "$GATEWAY_CAPTURE" 'gateway control plane isolation'
assert_contains 'SIDERA_WEB_KNOWLEDGE_TOKEN=fake-knowledge-local-owner-token' "$WEB_CAPTURE" 'web server-side knowledge capability'
assert_contains 'SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN=<unset>' "$WEB_CAPTURE" 'web direct capability isolation'

for secret in deepseek-secret openai-secret anthropic-secret opencode-secret fake-knowledge-local-owner-token fake-knowledge-control-plane-token fake-knowledge-main-agent-token fake-akashic-workspace-token; do
  if grep -R -F "$secret" "$RUNTIME_DIR" >/dev/null 2>&1; then
    fail 'secret leaked into runtime identity or logs'
  fi
done

printf '%s\n' knowledge-candidate-worker > "$TMP/fail-component"
stack stop > "$TMP/stop-before-failure.out"
if stack start > "$TMP/failure.out" 2>&1; then
  fail 'mid-start failure unexpectedly succeeded'
fi
for component in knowledge-service control-plane context-package-worker knowledge-candidate-worker; do
  if [ -f "$RUNTIME_DIR/pids/$component.pid" ] || [ -f "$RUNTIME_DIR/identity/$component.identity" ]; then
    fail "mid-start rollback left state for $component"
  fi
done
if find "$RUNTIME_DIR/pids" "$RUNTIME_DIR/identity" -type f -maxdepth 1 -print | grep . >/dev/null 2>&1; then
  fail 'mid-start rollback left pid or identity files'
fi
rm -f "$TMP/fail-component"

sleep 30 &
UNRELATED_PID=$!
printf '%s\n' "$UNRELATED_PID" > "$RUNTIME_DIR/pids/planning-worker.pid"
printf '%s\n' \
  "pid=$UNRELATED_PID" \
  'component=planning-worker' \
  "repo=$ROOT" \
  'marker=sidera-r5-3:planning-worker' \
  'started_at=2026-08-26T00:00:00Z' \
  'command=sidera_control_plane.planning_worker' > "$RUNTIME_DIR/identity/planning-worker.identity"
stack start > "$TMP/stale-start.out"
assert_pid_alive "$UNRELATED_PID" 'unrelated process after stale quarantine'
find "$RUNTIME_DIR/pids" "$RUNTIME_DIR/identity" -name 'planning-worker.*stale.*' -type f -print | grep . >/dev/null 2>&1 \
  || fail 'stale planning worker state was not quarantined'

stack stop > "$TMP/stop.out"
stack stop > "$TMP/stop-again.out"
for component in knowledge-service control-plane context-package-worker knowledge-candidate-worker planning-worker activation-worker scheduler-worker gateway-worker main-agent-gateway web; do
  if [ -f "$RUNTIME_DIR/pids/$component.pid" ] || [ -f "$RUNTIME_DIR/identity/$component.identity" ]; then
    fail "stop left state for $component"
  fi
done
if stack unknown >/dev/null 2>&1; then
  fail 'unknown stack action unexpectedly succeeded'
fi
if stack logs unknown >/dev/null 2>&1; then
  fail 'unknown stack component unexpectedly succeeded'
fi

printf 'r5-3 stack tests passed\n'
