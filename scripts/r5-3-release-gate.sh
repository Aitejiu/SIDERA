#!/bin/bash
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${SIDERA_R5_3_ENV_FILE:-"$REPO_ROOT/.sidera/r5-3.local.env"}
RUNTIME_DIR=${SIDERA_R5_3_RUNTIME_DIR:-"$REPO_ROOT/.sidera/runtime"}

PREFLIGHT_SCRIPT=${SIDERA_R5_3_PREFLIGHT_SCRIPT:-"$SCRIPT_DIR/r5-3-preflight.sh"}
STACK_SCRIPT=${SIDERA_R5_3_STACK_SCRIPT:-"$SCRIPT_DIR/r5-3-stack.sh"}
DETERMINISTIC_LOOP=${SIDERA_R5_3_DETERMINISTIC_LOOP:-"$SCRIPT_DIR/r5-3-vertical-loop.py"}
FAULT_LOOP=${SIDERA_R5_3_FAULT_LOOP:-"$SCRIPT_DIR/r5-3-vertical-loop.py"}
SHELL_COMMAND=${SIDERA_R5_3_SHELL_COMMAND:-/bin/bash}

UV_COMMAND=${SIDERA_R5_3_UV_COMMAND:-uv}
NPM_COMMAND=${SIDERA_R5_3_NPM_COMMAND:-npm}
GIT_COMMAND=${SIDERA_R5_3_GIT_COMMAND:-git}

CONTROL_PLANE_ROOT="$REPO_ROOT/services/control-plane"
KNOWLEDGE_ROOT="$REPO_ROOT/services/knowledge-service"
GATEWAY_ROOT="$REPO_ROOT/services/main-agent-gateway"
AKASHIC_ROOT="$REPO_ROOT/integrations/akashic"
WEB_ROOT="$REPO_ROOT/apps/web"

fail() {
  printf 'r5-3 release gate: %s\n' "$1" >&2
  exit 1
}

require_value() {
  [ -n "${2:-}" ] || fail "required configuration is missing: $1"
}

if [ "${SIDERA_RUN_REAL_R5_3:-}" != "1" ]; then
  printf 'r5-3 release gate: SIDERA_RUN_REAL_R5_3=1 is required; deterministic results are not release evidence\n' >&2
  exit 2
fi

[ -f "$ENV_FILE" ] || fail "env file is missing"
[ ! -L "$ENV_FILE" ] || fail "env file must not be a symlink"

read_env_value() {
  local wanted_key=$1 line
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$wanted_key="*)
        printf '%s\n' "${line#*=}"
        return 0
        ;;
    esac
  done < "$ENV_FILE"
  return 1
}

config_value() {
  read_env_value "$1"
}

resolve_command() {
  local value=$1 label=$2 resolved
  case "$value" in
    */*)
      [ -x "$value" ] || fail "$label is not executable"
      printf '%s\n' "$value"
      ;;
    *)
      resolved=$(command -v "$value" 2>/dev/null || true)
      [ -n "$resolved" ] || fail "$label is unavailable"
      printf '%s\n' "$resolved"
      ;;
  esac
}

CP_TEST_DATABASE=$(config_value SIDERA_R5_3_TEST_DATABASE_URL || true)
KNOWLEDGE_TEST_DATABASE=$(config_value SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL || true)
WORKSPACE_ID=$(config_value SIDERA_WORKSPACE_ID || true)
CONTROL_PLANE_URL=$(config_value SIDERA_CONTROL_PLANE_URL || true)
KNOWLEDGE_BASE_URL=$(config_value SIDERA_KNOWLEDGE_BASE_URL || true)
MAIN_AGENT_GATEWAY_URL=$(config_value SIDERA_MAIN_AGENT_GATEWAY_URL || true)
WEB_URL=$(config_value SIDERA_WEB_URL || true)
PROFILE_ID=$(config_value SIDERA_PLANNING_PROFILE_ID || true)
PROFILE_VERSION=$(config_value SIDERA_PLANNING_PROFILE_VERSION || true)
OPENCODE_EXECUTABLE=$(config_value SIDERA_OPENCODE_EXECUTABLE || true)
OPENCODE_EXPECTED_VERSION=$(config_value SIDERA_OPENCODE_EXPECTED_VERSION || true)
OPENCODE_MODEL=$(config_value SIDERA_OPENCODE_MODEL || true)
OPENCODE_TIMEOUT=$(config_value SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS || true)

REAL_PROVIDER=${SIDERA_R5_3_REAL_PROVIDER:-${SIDERA_REAL_MAIN_AGENT_PROVIDER:-}}
REAL_GATE_HELPER=${SIDERA_R5_3_REAL_GATE_HELPER:-${SIDERA_REAL_GATE_HELPER:-}}

require_value SIDERA_R5_3_TEST_DATABASE_URL "$CP_TEST_DATABASE"
require_value SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL "$KNOWLEDGE_TEST_DATABASE"
require_value SIDERA_WORKSPACE_ID "$WORKSPACE_ID"
require_value SIDERA_CONTROL_PLANE_URL "$CONTROL_PLANE_URL"
require_value SIDERA_KNOWLEDGE_BASE_URL "$KNOWLEDGE_BASE_URL"
require_value SIDERA_MAIN_AGENT_GATEWAY_URL "$MAIN_AGENT_GATEWAY_URL"
require_value SIDERA_WEB_URL "$WEB_URL"
require_value SIDERA_PLANNING_PROFILE_ID "$PROFILE_ID"
require_value SIDERA_PLANNING_PROFILE_VERSION "$PROFILE_VERSION"
require_value SIDERA_OPENCODE_EXECUTABLE "$OPENCODE_EXECUTABLE"
require_value SIDERA_OPENCODE_EXPECTED_VERSION "$OPENCODE_EXPECTED_VERSION"
require_value SIDERA_OPENCODE_MODEL "$OPENCODE_MODEL"
require_value SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS "$OPENCODE_TIMEOUT"
require_value SIDERA_REAL_MAIN_AGENT_PROVIDER "$REAL_PROVIDER"
require_value SIDERA_REAL_GATE_HELPER "$REAL_GATE_HELPER"
[ -x "$REAL_GATE_HELPER" ] || fail "SIDERA_REAL_GATE_HELPER is not executable"

SHELL_COMMAND=$(resolve_command "$SHELL_COMMAND" shell)
UV_COMMAND=$(resolve_command "$UV_COMMAND" uv)
NPM_COMMAND=$(resolve_command "$NPM_COMMAND" npm)
GIT_COMMAND=$(resolve_command "$GIT_COMMAND" git)
[ -x "$PREFLIGHT_SCRIPT" ] || fail "preflight script is not executable"
[ -x "$STACK_SCRIPT" ] || fail "stack script is not executable"
[ -x "$DETERMINISTIC_LOOP" ] || fail "deterministic loop is not executable"
[ -x "$FAULT_LOOP" ] || fail "fault loop is not executable"

CONTROL_PLANE_PORT=${CONTROL_PLANE_URL##*:}
CONTROL_PLANE_PORT=${CONTROL_PLANE_PORT%%/*}
KNOWLEDGE_PORT=${KNOWLEDGE_BASE_URL##*:}
KNOWLEDGE_PORT=${KNOWLEDGE_PORT%%/*}
MAIN_AGENT_PORT=${MAIN_AGENT_GATEWAY_URL##*:}
MAIN_AGENT_PORT=${MAIN_AGENT_PORT%%/*}
WEB_PORT=${WEB_URL##*:}
WEB_PORT=${WEB_PORT%%/*}

# The release gate itself never needs a capability or provider token. The
# protected env file is consumed by the preflight/stack, while child checks
# receive only the non-secret configuration needed by their contract.
unset \
  SIDERA_AKASHIC_WORKSPACE_TOKEN \
  SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN \
  SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN \
  SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN \
  SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN \
  DEEPSEEK_API_KEY \
  OPENAI_API_KEY \
  ANTHROPIC_API_KEY \
  OPENCODE_API_KEY

export \
  SIDERA_DATABASE_URL="$CP_TEST_DATABASE" \
  SIDERA_R5_3_TEST_DATABASE_URL="$CP_TEST_DATABASE" \
  SIDERA_KNOWLEDGE_DATABASE_URL="$KNOWLEDGE_TEST_DATABASE" \
  SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL="$KNOWLEDGE_TEST_DATABASE" \
  SIDERA_WORKSPACE_ID="$WORKSPACE_ID" \
  SIDERA_CONTROL_PLANE_URL="$CONTROL_PLANE_URL" \
  SIDERA_KNOWLEDGE_BASE_URL="$KNOWLEDGE_BASE_URL" \
  SIDERA_MAIN_AGENT_GATEWAY_URL="$MAIN_AGENT_GATEWAY_URL" \
  SIDERA_WEB_URL="$WEB_URL" \
  SIDERA_PLANNING_PROFILE_ID="$PROFILE_ID" \
  SIDERA_PLANNING_PROFILE_VERSION="$PROFILE_VERSION" \
  SIDERA_OPENCODE_EXECUTABLE="$OPENCODE_EXECUTABLE" \
  SIDERA_OPENCODE_EXPECTED_VERSION="$OPENCODE_EXPECTED_VERSION" \
  SIDERA_OPENCODE_MODEL="$OPENCODE_MODEL" \
  SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS="$OPENCODE_TIMEOUT" \
  SIDERA_RUN_REAL_MAIN_AGENT=1 \
  SIDERA_RUN_REAL_OPENCODE=1 \
  SIDERA_ALLOW_E2E_SEED=1 \
  SIDERA_REAL_MAIN_AGENT_PROVIDER="$REAL_PROVIDER" \
  SIDERA_REAL_GATE_HELPER="$REAL_GATE_HELPER" \
  SIDERA_R5_3_ENV_FILE="$ENV_FILE" \
  SIDERA_R5_3_RUNTIME_DIR="$RUNTIME_DIR"

run_safe() {
  /usr/bin/env \
    -u SIDERA_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN \
    -u SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN \
    -u SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN \
    -u DEEPSEEK_API_KEY \
    -u OPENAI_API_KEY \
    -u ANTHROPIC_API_KEY \
    -u OPENCODE_API_KEY \
    -u SIDERA_RUN_REAL_R5_3 \
    -u SIDERA_RUN_REAL_MAIN_AGENT \
    -u SIDERA_RUN_REAL_OPENCODE \
    -u SIDERA_RUN_OPENCODE_E2E \
    "$@"
}

run_step() {
  local label=$1
  shift
  printf 'r5-3 release gate: %s\n' "$label"
  case "${1:-}" in
    verify_stack_health|verify_knowledge_openapi|verify_control_plane_openapi|run_deterministic_loop|run_fault_loop|run_real_vertical_loop|run_real_web_journey|verify_secret_body_database_isolation|verify_repo_boundary)
      if "$@" >/dev/null 2>&1; then
        return 0
      fi
      ;;
    *)
      if run_safe "$@" >/dev/null 2>&1; then
        return 0
      fi
      ;;
  esac
  printf 'r5-3 release gate: failed at %s\n' "$label" >&2
  exit 1
}

verify_stack_health() {
  local status_output expected
  if ! status_output=$(run_safe \
    SIDERA_R5_3_ENV_FILE="$ENV_FILE" \
    SIDERA_R5_3_RUNTIME_DIR="$RUNTIME_DIR" \
    "$SHELL_COMMAND" "$STACK_SCRIPT" status 2>/dev/null); then
    return 1
  fi
  for expected in \
    'knowledge-service running ready' \
    'control-plane running ready' \
    'context-package-worker running ready' \
    'knowledge-candidate-worker running ready' \
    'planning-worker running ready' \
    'activation-worker running ready' \
    'scheduler-worker running ready' \
    'gateway-worker running ready' \
    'main-agent-gateway running ready' \
    'web running ready'; do
    case "$status_output" in
      *"$expected"*) ;;
      *) return 1 ;;
    esac
  done
}

verify_knowledge_openapi() {
  run_safe SIDERA_KNOWLEDGE_DATABASE_URL="$KNOWLEDGE_TEST_DATABASE" \
    "$UV_COMMAND" run --project "$KNOWLEDGE_ROOT" python \
    -m sidera_knowledge.api.openapi_contract \
    --output "$WEB_ROOT/knowledge-openapi.json" || return 1
  run_safe "$NPM_COMMAND" --prefix "$WEB_ROOT" run knowledge-api:generate || return 1
  run_safe "$GIT_COMMAND" -C "$REPO_ROOT" diff --exit-code -- \
    apps/web/knowledge-openapi.json \
    apps/web/src/shared/api/knowledge-schema.d.ts
}

verify_control_plane_openapi() {
  run_safe SIDERA_DATABASE_URL="$CP_TEST_DATABASE" \
    "$UV_COMMAND" run --project "$CONTROL_PLANE_ROOT" python \
    -m sidera_control_plane.api.openapi_contract \
    --output "$WEB_ROOT/openapi.json" || return 1
  run_safe "$NPM_COMMAND" --prefix "$WEB_ROOT" run api:generate || return 1
  run_safe "$GIT_COMMAND" -C "$REPO_ROOT" diff --exit-code -- \
    apps/web/openapi.json \
    apps/web/src/shared/api/schema.d.ts
}

run_deterministic_loop() {
  run_safe "$DETERMINISTIC_LOOP" \
    --mode deterministic \
    --env-file "$ENV_FILE" \
    --control-plane-url "$CONTROL_PLANE_URL" \
    --knowledge-base-url "$KNOWLEDGE_BASE_URL" \
    --workspace-id "$WORKSPACE_ID" \
    --deadline-seconds "${SIDERA_R5_3_VERTICAL_DEADLINE_SECONDS:-300}"
}

run_fault_loop() {
  run_safe "$FAULT_LOOP" \
    --mode fault \
    --env-file "$ENV_FILE" \
    --control-plane-url "$CONTROL_PLANE_URL" \
    --knowledge-base-url "$KNOWLEDGE_BASE_URL" \
    --workspace-id "$WORKSPACE_ID" \
    --deadline-seconds "${SIDERA_R5_3_FAULT_DEADLINE_SECONDS:-300}"
}

run_real_vertical_loop() {
  run_safe SIDERA_DATABASE_URL="$CP_TEST_DATABASE" \
    SIDERA_RUN_REAL_OPENCODE=1 \
    SIDERA_RUN_OPENCODE_E2E=1 \
    "$UV_COMMAND" run --project "$CONTROL_PLANE_ROOT" pytest -m integration \
    "$CONTROL_PLANE_ROOT/tests/integration/test_r5_1_real_planning_loop.py" -q || return 1
  run_safe SIDERA_DATABASE_URL="$CP_TEST_DATABASE" \
    SIDERA_RUN_REAL_MAIN_AGENT=1 \
    "$UV_COMMAND" run --project "$GATEWAY_ROOT" pytest -m integration \
    "$GATEWAY_ROOT/tests/integration/test_real_vertical_loop.py" -q
}

run_real_web_journey() {
  run_safe \
    SIDERA_E2E_CONTROL_PLANE_PORT="$CONTROL_PLANE_PORT" \
    SIDERA_E2E_KNOWLEDGE_SERVICE_PORT="$KNOWLEDGE_PORT" \
    SIDERA_E2E_WEB_PORT="$WEB_PORT" \
    SIDERA_RUN_REAL_MAIN_AGENT=1 \
    "$NPM_COMMAND" --prefix "$WEB_ROOT" run test:e2e -- \
    --project=desktop-chromium tests/e2e/real-main-agent.spec.ts
}

verify_secret_body_database_isolation() {
  local tracked_paths
  tracked_paths=$(run_safe "$GIT_COMMAND" -C "$REPO_ROOT" ls-files -- \
    '.sidera' '.sidera/**' '.env' '.env.*' 'config/local' 'config/local/**' \
    'candidate-spool' 'candidate-spool/**' || true)
  [ -z "$tracked_paths" ] || return 1

  run_safe SIDERA_KNOWLEDGE_DATABASE_URL="$KNOWLEDGE_TEST_DATABASE" \
    "$UV_COMMAND" run --project "$KNOWLEDGE_ROOT" pytest -m integration \
    "$KNOWLEDGE_ROOT/tests/integration/test_database_schema.py" -q || return 1
  run_safe SIDERA_DATABASE_URL="$CP_TEST_DATABASE" \
    "$UV_COMMAND" run --project "$CONTROL_PLANE_ROOT" pytest -m integration \
    "$CONTROL_PLANE_ROOT/tests/integration/test_r5_3_database_schema.py" -q
}

verify_repo_boundary() {
  local tracked_paths tree status_output
  run_safe "$GIT_COMMAND" -C "$REPO_ROOT" diff --quiet -- akashic-agent || return 1
  status_output=$(run_safe "$GIT_COMMAND" -C "$REPO_ROOT" status --short -- akashic-agent)
  [ -z "$status_output" ] || return 1
  tracked_paths=$(run_safe "$GIT_COMMAND" -C "$REPO_ROOT" ls-files -- \
    '.sidera' '.sidera/**' '.env' '.env.*' 'config/local' 'config/local/**' \
    'references' 'references/**' '.superpowers' '.superpowers/**')
  [ -z "$tracked_paths" ] || return 1
  tree=$(run_safe "$GIT_COMMAND" -C "$REPO_ROOT" rev-parse HEAD:akashic-agent)
  [ -n "$tree" ]
}

run_step preflight \
  SIDERA_R5_3_ENV_FILE="$ENV_FILE" \
  SIDERA_R5_3_RUNTIME_DIR="$RUNTIME_DIR" \
  "$SHELL_COMMAND" "$PREFLIGHT_SCRIPT" \
  --env-file "$ENV_FILE" --runtime-dir "$RUNTIME_DIR"
run_step 'stack health' verify_stack_health

run_step 'knowledge unit' \
  SIDERA_KNOWLEDGE_DATABASE_URL="$KNOWLEDGE_TEST_DATABASE" \
  "$UV_COMMAND" run --project "$KNOWLEDGE_ROOT" pytest \
  "$KNOWLEDGE_ROOT/tests/unit" -q
run_step 'knowledge integration' \
  SIDERA_KNOWLEDGE_DATABASE_URL="$KNOWLEDGE_TEST_DATABASE" \
  "$UV_COMMAND" run --project "$KNOWLEDGE_ROOT" pytest -m integration \
  "$KNOWLEDGE_ROOT/tests/integration" -q
run_step 'knowledge ruff' \
  "$UV_COMMAND" run --project "$KNOWLEDGE_ROOT" ruff check \
  "$KNOWLEDGE_ROOT/src" "$KNOWLEDGE_ROOT/tests"
run_step 'knowledge pyright' \
  "$UV_COMMAND" run --project "$KNOWLEDGE_ROOT" pyright \
  "$KNOWLEDGE_ROOT/src" "$KNOWLEDGE_ROOT/tests"
run_step 'knowledge alembic' \
  SIDERA_KNOWLEDGE_DATABASE_URL="$KNOWLEDGE_TEST_DATABASE" \
  "$UV_COMMAND" run --project "$KNOWLEDGE_ROOT" alembic \
  -c "$KNOWLEDGE_ROOT/alembic.ini" check
run_step 'knowledge OpenAPI drift' verify_knowledge_openapi
run_step 'knowledge outbox once' \
  SIDERA_KNOWLEDGE_DATABASE_URL="$KNOWLEDGE_TEST_DATABASE" \
  SIDERA_KNOWLEDGE_WORKSPACE_ID="$WORKSPACE_ID" \
  "$UV_COMMAND" run --project "$KNOWLEDGE_ROOT" python \
  -m sidera_knowledge.knowledge_outbox_worker --once
run_step 'knowledge outbox recovery' \
  SIDERA_KNOWLEDGE_DATABASE_URL="$KNOWLEDGE_TEST_DATABASE" \
  "$UV_COMMAND" run --project "$KNOWLEDGE_ROOT" pytest -m integration \
  "$KNOWLEDGE_ROOT/tests/integration/test_candidate_concurrency.py" \
  "$KNOWLEDGE_ROOT/tests/integration/test_outbox_recovery.py" -q

run_step 'control-plane unit' \
  "$UV_COMMAND" run --project "$CONTROL_PLANE_ROOT" pytest \
  "$CONTROL_PLANE_ROOT/tests/unit" -q
run_step 'control-plane integration' \
  SIDERA_DATABASE_URL="$CP_TEST_DATABASE" \
  "$UV_COMMAND" run --project "$CONTROL_PLANE_ROOT" pytest -m integration \
  "$CONTROL_PLANE_ROOT/tests/integration" -q
run_step 'control-plane ruff' \
  "$UV_COMMAND" run --project "$CONTROL_PLANE_ROOT" ruff check \
  "$CONTROL_PLANE_ROOT/src" "$CONTROL_PLANE_ROOT/tests"
run_step 'control-plane pyright' \
  "$UV_COMMAND" run --project "$CONTROL_PLANE_ROOT" pyright \
  "$CONTROL_PLANE_ROOT/src" "$CONTROL_PLANE_ROOT/tests"
run_step 'control-plane alembic' \
  SIDERA_DATABASE_URL="$CP_TEST_DATABASE" \
  "$UV_COMMAND" run --project "$CONTROL_PLANE_ROOT" alembic \
  -c "$CONTROL_PLANE_ROOT/alembic.ini" check
run_step 'control-plane OpenAPI drift' verify_control_plane_openapi

run_step 'Akashic plugin tests' \
  PYTHONPATH="$AKASHIC_ROOT:$REPO_ROOT/akashic-agent" \
  "$UV_COMMAND" run --project "$AKASHIC_ROOT" pytest \
  "$AKASHIC_ROOT/tests" -q
run_step 'Akashic plugin ruff' \
  "$UV_COMMAND" run --project "$AKASHIC_ROOT" ruff check "$AKASHIC_ROOT"
run_step 'Akashic plugin pyright' \
  "$UV_COMMAND" run --project "$AKASHIC_ROOT" pyright \
  -p "$AKASHIC_ROOT"
run_step 'Akashic tool contract' \
  PYTHONPATH="$AKASHIC_ROOT:$REPO_ROOT/akashic-agent" \
  "$UV_COMMAND" run --project "$AKASHIC_ROOT" pytest \
  "$AKASHIC_ROOT/tests/test_contracts.py" \
  "$AKASHIC_ROOT/tests/test_plugin_contract.py" \
  "$AKASHIC_ROOT/tests/test_tools.py" -q

run_step 'Gateway tests' \
  "$UV_COMMAND" run --project "$GATEWAY_ROOT" pytest \
  "$GATEWAY_ROOT/tests/unit" "$GATEWAY_ROOT/tests/integration" -q
run_step 'Gateway ruff' \
  "$UV_COMMAND" run --project "$GATEWAY_ROOT" ruff check "$GATEWAY_ROOT"
run_step 'Gateway pyright' \
  "$UV_COMMAND" run --project "$GATEWAY_ROOT" pyright "$GATEWAY_ROOT"

run_step 'Web unit' "$NPM_COMMAND" --prefix "$WEB_ROOT" test
run_step 'Web lint' "$NPM_COMMAND" --prefix "$WEB_ROOT" run lint
run_step 'Web build' "$NPM_COMMAND" --prefix "$WEB_ROOT" run build
run_step 'Web deterministic E2E' \
  SIDERA_E2E_CONTROL_PLANE_PORT="$CONTROL_PLANE_PORT" \
  SIDERA_E2E_KNOWLEDGE_SERVICE_PORT="$KNOWLEDGE_PORT" \
  SIDERA_E2E_WEB_PORT="$WEB_PORT" \
  "$NPM_COMMAND" --prefix "$WEB_ROOT" run test:e2e

run_step 'deterministic R5.3 vertical loop' run_deterministic_loop
run_step 'fault recovery matrix' run_fault_loop
run_step 'real Main Agent/Planner/Worker loop' run_real_vertical_loop
run_step 'real Web knowledge/context journey' run_real_web_journey
run_step 'secret/body/database isolation scan' verify_secret_body_database_isolation
run_step 'repo boundary identity' verify_repo_boundary

printf 'r5-3 release gate: passed\n'
