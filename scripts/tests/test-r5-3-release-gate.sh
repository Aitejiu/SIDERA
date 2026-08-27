#!/bin/bash
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r5-3-release-gate.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

FAKE_BIN="$TMP/bin"
ENV_FILE="$TMP/r5-3.local.env"
RUNTIME_DIR="$TMP/runtime"
LOG_FILE="$TMP/commands.log"
OUTPUT_FILE="$TMP/gate-output.log"
PREFLIGHT="$TMP/preflight.sh"
STACK="$TMP/stack.sh"
DETERMINISTIC="$TMP/deterministic.sh"
FAULT="$TMP/fault.sh"
REAL_HELPER="$TMP/real-helper.sh"

mkdir -p "$FAKE_BIN"

fail() {
  printf 'r5-3 release gate test: %s\n' "$1" >&2
  exit 1
}

assert_contains() {
  grep -F -- "$1" "$2" >/dev/null 2>&1 || fail "expected text is missing: $3"
}

assert_not_contains() {
  if grep -F -- "$1" "$2" >/dev/null 2>&1; then
    fail "unexpected text is present: $3"
  fi
}

write_executable() {
  local path=$1
  shift
  printf '%s\n' "$@" > "$path"
  chmod 700 "$path"
}

write_env() {
  printf '%s\n' \
    'SIDERA_DATABASE_URL=postgresql://sidera@127.0.0.1:55432/sidera_r53_runtime' \
    'SIDERA_R5_3_TEST_DATABASE_URL=postgresql://sidera@127.0.0.1:55432/sidera_r53_cp_test' \
    'SIDERA_KNOWLEDGE_DATABASE_URL=postgresql://sidera@127.0.0.1:55432/sidera_r53_knowledge_runtime' \
    'SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL=postgresql://sidera@127.0.0.1:55432/sidera_r53_knowledge_test' \
    'SIDERA_WORKSPACE_ID=20000000-0000-0000-0000-000000000001' \
    'SIDERA_R5_3_GATE_WORKSPACE_ID=20000000-0000-0000-0000-000000000099' \
    'SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN=fake-knowledge-control-plane-token' \
    'SIDERA_CONTROL_PLANE_URL=http://127.0.0.1:8000' \
    'SIDERA_KNOWLEDGE_BASE_URL=http://127.0.0.1:8020' \
    'SIDERA_MAIN_AGENT_GATEWAY_URL=http://127.0.0.1:8010' \
    'SIDERA_WEB_URL=http://127.0.0.1:4173' \
    'SIDERA_CONTEXT_PACKAGE_CUTOVER_AT=2026-08-27T00:00:00+08:00' \
    'SIDERA_PLANNING_PROFILE_ID=10000000-0000-0000-0000-000000000001' \
    'SIDERA_PLANNING_PROFILE_VERSION=1' \
    'SIDERA_OPENCODE_EXECUTABLE=/usr/bin/true' \
    'SIDERA_OPENCODE_EXPECTED_VERSION=1.18.19' \
    'SIDERA_OPENCODE_MODEL=opencode-go/MiMo-V2.5' \
    'SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=300' > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

write_fakes() {
  write_executable "$PREFLIGHT" \
    '#!/bin/sh' \
    'printf "preflight\\n" >> "$SIDERA_R5_3_GATE_LOG"' \
    'exit 0'

  write_executable "$STACK" \
    '#!/bin/sh' \
    'printf "stack:%s\\n" "$*" >> "$SIDERA_R5_3_GATE_LOG"' \
    'if [ "${1:-}" = "status" ]; then' \
    '  printf "%s\\n" "knowledge-service running ready" "control-plane running ready" "context-package-worker running ready" "knowledge-candidate-worker running ready" "planning-worker running ready" "activation-worker running ready" "scheduler-worker running ready" "gateway-worker running ready" "main-agent-gateway running ready" "web running ready"' \
    'fi' \
    'exit 0'

  write_executable "$DETERMINISTIC" \
    '#!/bin/sh' \
    'printf "deterministic:%s\\n" "$*" >> "$SIDERA_R5_3_GATE_LOG"' \
    'exit 0'

  write_executable "$FAULT" \
    '#!/bin/sh' \
    'printf "fault:%s\\n" "$*" >> "$SIDERA_R5_3_GATE_LOG"' \
    'exit 0'

  write_executable "$REAL_HELPER" \
    '#!/bin/sh' \
    'printf "helper:%s\\n" "$*" >> "$SIDERA_R5_3_GATE_LOG"' \
    'exit 0'

  write_executable "$FAKE_BIN/uv" \
    '#!/bin/sh' \
    'printf "uv:%s:real-main=%s:real-opencode=%s:provider=%s:model=%s:token=%s:knowledge-token=%s:assignment-lease=%s:assignment-renew=%s\\n" "$*" "${SIDERA_RUN_REAL_MAIN_AGENT:-}" "${SIDERA_RUN_REAL_OPENCODE:-}" "${SIDERA_REAL_MAIN_AGENT_PROVIDER:+set}" "${SIDERA_OPENCODE_MODEL:+set}" "${SIDERA_AKASHIC_WORKSPACE_TOKEN:+set}" "${SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN:+set}" "${SIDERA_ASSIGNMENT_LEASE_SECONDS:+set}" "${SIDERA_ASSIGNMENT_LEASE_RENEW_INTERVAL_SECONDS:+set}" >> "$SIDERA_R5_3_GATE_LOG"' \
    'case "${SIDERA_R5_3_GATE_FAIL_MATCH:-}" in' \
    '  knowledge-unit) case "$*" in *services/knowledge-service/tests/unit*) exit 42 ;; esac ;;' \
    '  *) ;;' \
    'esac' \
    'exit 0'

  write_executable "$FAKE_BIN/npm" \
    '#!/bin/sh' \
    'printf "npm:%s:real-main=%s:helper=%s:token=%s\\n" "$*" "${SIDERA_RUN_REAL_MAIN_AGENT:-}" "${SIDERA_REAL_GATE_HELPER:+set}" "${SIDERA_AKASHIC_WORKSPACE_TOKEN:+set}" >> "$SIDERA_R5_3_GATE_LOG"' \
    'case "$*" in *real-main-agent.spec.ts*) "$SIDERA_REAL_GATE_HELPER" restart-gateway ;; esac' \
    'exit 0'

  write_executable "$FAKE_BIN/git" \
    '#!/bin/sh' \
    'printf "git:%s\\n" "$*" >> "$SIDERA_R5_3_GATE_LOG"' \
    'case "$*" in' \
    '  *"rev-parse HEAD:akashic-agent"*) printf "akashic-tree\\n" ;;' \
    '  *"status --short"*) ;;' \
    '  *"ls-files"*) ;;' \
    'esac' \
    'exit 0'
}

write_env
write_fakes

if output=$(
  SIDERA_R5_3_ENV_FILE="$ENV_FILE" \
    /bin/bash "$ROOT/scripts/r5-3-release-gate.sh" 2>&1
); then
  fail "release gate should require the real R5.3 flag"
fi
case "$output" in
  *'SIDERA_RUN_REAL_R5_3=1'*) ;;
  *) printf '%s\n' "$output" >&2; fail "real flag error was unclear" ;;
esac

if output=$(
  SIDERA_RUN_REAL_R5_3=1 \
    SIDERA_R5_3_ENV_FILE="$TMP/missing.env" \
    /bin/bash "$ROOT/scripts/r5-3-release-gate.sh" 2>&1
); then
  fail "missing env file should fail"
fi
assert_contains 'env file is missing' <(printf '%s\n' "$output") 'missing env diagnostic'

export PATH="$FAKE_BIN:/usr/bin:/bin"
export SIDERA_R5_3_ENV_FILE="$ENV_FILE"
export SIDERA_R5_3_RUNTIME_DIR="$RUNTIME_DIR"
export SIDERA_R5_3_PREFLIGHT_SCRIPT="$PREFLIGHT"
export SIDERA_R5_3_STACK_SCRIPT="$STACK"
export SIDERA_R5_3_DETERMINISTIC_LOOP="$DETERMINISTIC"
export SIDERA_R5_3_FAULT_LOOP="$FAULT"
export SIDERA_R5_3_UV_COMMAND="$FAKE_BIN/uv"
export SIDERA_R5_3_NPM_COMMAND="$FAKE_BIN/npm"
export SIDERA_R5_3_GIT_COMMAND="$FAKE_BIN/git"
export SIDERA_R5_3_REAL_PROVIDER="test-provider"
export SIDERA_R5_3_REAL_GATE_HELPER="$REAL_HELPER"
export SIDERA_REAL_MAIN_AGENT_PROVIDER="test-provider"
export SIDERA_REAL_GATE_HELPER="$REAL_HELPER"
export SIDERA_AKASHIC_WORKSPACE_TOKEN="token-must-not-enter-gate-children"
export SIDERA_ASSIGNMENT_LEASE_SECONDS=300
export SIDERA_ASSIGNMENT_LEASE_RENEW_INTERVAL_SECONDS=30
export SIDERA_R5_3_GATE_LOG="$LOG_FILE"

if output=$(
  SIDERA_RUN_REAL_R5_3=1 \
    env -u SIDERA_R5_3_REAL_PROVIDER -u SIDERA_REAL_MAIN_AGENT_PROVIDER \
    /bin/bash "$ROOT/scripts/r5-3-release-gate.sh" 2>&1
); then
  fail "missing provider should fail"
fi
assert_contains 'missing: SIDERA_REAL_MAIN_AGENT_PROVIDER' <(printf '%s\n' "$output") 'missing provider diagnostic'

if output=$(
  SIDERA_RUN_REAL_R5_3=1 \
    env -u SIDERA_R5_3_REAL_GATE_HELPER -u SIDERA_REAL_GATE_HELPER \
    /bin/bash "$ROOT/scripts/r5-3-release-gate.sh" 2>&1
); then
  fail "missing helper should fail"
fi
assert_contains 'missing: SIDERA_REAL_GATE_HELPER' <(printf '%s\n' "$output") 'missing helper diagnostic'

sed '/^SIDERA_OPENCODE_MODEL=/d' "$ENV_FILE" > "$TMP/missing-model.env"
chmod 600 "$TMP/missing-model.env"
if output=$(
  SIDERA_RUN_REAL_R5_3=1 \
    SIDERA_R5_3_ENV_FILE="$TMP/missing-model.env" \
    /bin/bash "$ROOT/scripts/r5-3-release-gate.sh" 2>&1
); then
  fail "missing model should fail"
fi
assert_contains 'missing: SIDERA_OPENCODE_MODEL' <(printf '%s\n' "$output") 'missing model diagnostic'

: > "$LOG_FILE"
if ! SIDERA_RUN_REAL_R5_3=1 \
  /bin/bash "$ROOT/scripts/r5-3-release-gate.sh" > "$OUTPUT_FILE" 2>&1; then
  cat "$OUTPUT_FILE" >&2
  fail "healthy fake release gate should pass"
fi
assert_contains 'r5-3 release gate: passed' "$OUTPUT_FILE" 'release success'
assert_not_contains 'token-must-not-enter-gate-children' "$LOG_FILE" 'provider token leakage'
assert_contains 'assignment-lease=:assignment-renew=' "$LOG_FILE" 'runtime lease settings leakage'
assert_contains 'real-main=1' "$LOG_FILE" 'real Main Agent flag propagation'
assert_contains 'real-opencode=1' "$LOG_FILE" 'real OpenCode flag propagation'
assert_contains 'provider=set' "$LOG_FILE" 'provider identity propagation'
assert_contains 'model=set' "$LOG_FILE" 'model propagation'
assert_contains 'token=' "$LOG_FILE" 'token absence marker'
assert_contains 'test_r5_1_real_planning_loop.py -q:real-main=:real-opencode=1:provider=set:model=set:token=:knowledge-token=set' "$LOG_FILE" 'real planning Knowledge token injection'
assert_contains 'helper:restart-gateway' "$LOG_FILE" 'real helper propagation'
assert_contains '--workspace-id 20000000-0000-0000-0000-000000000099' "$LOG_FILE" 'deterministic gate workspace propagation'

previous=0
for label in \
  'preflight' \
  'stack health' \
  'knowledge unit' \
  'knowledge integration' \
  'knowledge ruff' \
  'knowledge pyright' \
  'knowledge alembic' \
  'knowledge OpenAPI drift' \
  'knowledge outbox once' \
  'knowledge outbox recovery' \
  'control-plane unit' \
  'control-plane integration' \
  'control-plane ruff' \
  'control-plane pyright' \
  'control-plane alembic' \
  'control-plane OpenAPI drift' \
  'Akashic plugin tests' \
  'Akashic plugin ruff' \
  'Akashic plugin pyright' \
  'Akashic tool contract' \
  'Gateway tests' \
  'Gateway ruff' \
  'Gateway pyright' \
  'Web unit' \
  'Web lint' \
  'Web build' \
  'Web deterministic E2E' \
  'deterministic R5.3 vertical loop' \
  'fault recovery matrix' \
  'real Main Agent/Planner/Worker loop' \
  'real Web knowledge/context journey' \
  'secret/body/database isolation scan' \
  'repo boundary identity'; do
  line=$(grep -n -F "r5-3 release gate: $label" "$OUTPUT_FILE" | head -n 1 | cut -d: -f1)
  [ -n "$line" ] || fail "missing gate label: $label"
  [ "$line" -gt "$previous" ] || fail "gate order is incorrect at: $label"
  previous=$line
done

: > "$LOG_FILE"
if output=$(
  SIDERA_RUN_REAL_R5_3=1 \
    SIDERA_R5_3_GATE_FAIL_MATCH=knowledge-unit \
    /bin/bash "$ROOT/scripts/r5-3-release-gate.sh" 2>&1
); then
  fail "a failed command should fail the release gate"
fi
case "$output" in
  *'failed at knowledge unit'*) ;;
  *) printf '%s\n' "$output" >&2; fail "failed label was not reported" ;;
esac
case "$output" in
  *'control-plane unit'*) fail "gate continued after a failed command" ;;
esac
case "$output" in
  *'r5-3 release gate: passed'*) fail "failed gate reported release success" ;;
esac

printf 'r5-3 release gate tests passed\n'
