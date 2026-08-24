#!/bin/bash
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${SIDERA_R5_2_ENV_FILE:-"$REPO_ROOT/.sidera/r5-2.local.env"}
RUNTIME_DIR=${SIDERA_R5_2_RUNTIME_DIR:-"$REPO_ROOT/.sidera/runtime"}

fail() {
  printf 'r5-2 release gate: %s\n' "$1" >&2
  exit 1
}

if [ "${SIDERA_RUN_REAL_MAIN_AGENT:-}" != "1" ]; then
  printf 'r5-2 release gate: SIDERA_RUN_REAL_MAIN_AGENT=1 is required; collection is not release evidence\n' >&2
  exit 2
fi

[ -f "$ENV_FILE" ] || fail "env file is missing"

ALLOWED_KEYS='SIDERA_DATABASE_URL|SIDERA_WORKSPACE_ID|SIDERA_PLANNING_PROFILE_ID|SIDERA_PLANNING_PROFILE_VERSION|SIDERA_CONTROL_PLANE_URL|SIDERA_WEB_URL|SIDERA_MAIN_AGENT_GATEWAY_URL|SIDERA_AKASHIC_ENDPOINT|SIDERA_AKASHIC_WORKSPACE_TOKEN|SIDERA_AKASHIC_WORKSPACE|SIDERA_AKASHIC_CONFIG|SIDERA_OPENCODE_EXECUTABLE|SIDERA_OPENCODE_EXPECTED_VERSION|SIDERA_OPENCODE_MODEL'
SEEN_KEYS=''

is_allowed_key() {
  case "|$ALLOWED_KEYS|" in
    *"|$1|"*) return 0 ;;
    *) return 1 ;;
  esac
}

set_config_value() {
  local key=$1 value=$2
  case "$key" in
    SIDERA_DATABASE_URL) SIDERA_DATABASE_URL=$value ;;
    SIDERA_WORKSPACE_ID) SIDERA_WORKSPACE_ID=$value ;;
    SIDERA_PLANNING_PROFILE_ID) SIDERA_PLANNING_PROFILE_ID=$value ;;
    SIDERA_PLANNING_PROFILE_VERSION) SIDERA_PLANNING_PROFILE_VERSION=$value ;;
    SIDERA_CONTROL_PLANE_URL) SIDERA_CONTROL_PLANE_URL=$value ;;
    SIDERA_WEB_URL) SIDERA_WEB_URL=$value ;;
    SIDERA_MAIN_AGENT_GATEWAY_URL) SIDERA_MAIN_AGENT_GATEWAY_URL=$value ;;
    SIDERA_AKASHIC_ENDPOINT) SIDERA_AKASHIC_ENDPOINT=$value ;;
    SIDERA_AKASHIC_WORKSPACE) SIDERA_AKASHIC_WORKSPACE=$value ;;
    SIDERA_AKASHIC_CONFIG) SIDERA_AKASHIC_CONFIG=$value ;;
    SIDERA_OPENCODE_EXECUTABLE) SIDERA_OPENCODE_EXECUTABLE=$value ;;
    SIDERA_OPENCODE_EXPECTED_VERSION) SIDERA_OPENCODE_EXPECTED_VERSION=$value ;;
    SIDERA_OPENCODE_MODEL) SIDERA_OPENCODE_MODEL=$value ;;
    SIDERA_AKASHIC_WORKSPACE_TOKEN) : ;;
  esac
}

load_config() {
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    case "$line" in
      export\ *|*'$('**|*'`'*|*';'*|*'|'*) fail "env file contains unsupported shell syntax" ;;
    esac
    case "$line" in
      *=*)
        key=${line%%=*}
        value=${line#*=}
        ;;
      *) fail "env file contains a line without KEY=VALUE" ;;
    esac
    is_allowed_key "$key" || fail "env file contains an unknown key"
    case "|$SEEN_KEYS|" in
      *"|$key|"*) fail "env file contains a duplicate key" ;;
    esac
    SEEN_KEYS="$SEEN_KEYS|$key"
    set_config_value "$key" "$value"
  done < "$ENV_FILE"
}

require_value() {
  [ -n "${2:-}" ] || fail "required configuration is missing: $1"
}

load_config
require_value SIDERA_DATABASE_URL "${SIDERA_DATABASE_URL:-}"
require_value SIDERA_WORKSPACE_ID "${SIDERA_WORKSPACE_ID:-}"
require_value SIDERA_PLANNING_PROFILE_ID "${SIDERA_PLANNING_PROFILE_ID:-}"
require_value SIDERA_CONTROL_PLANE_URL "${SIDERA_CONTROL_PLANE_URL:-}"
require_value SIDERA_MAIN_AGENT_GATEWAY_URL "${SIDERA_MAIN_AGENT_GATEWAY_URL:-}"
require_value SIDERA_WEB_URL "${SIDERA_WEB_URL:-}"

case "${SIDERA_REAL_MAIN_AGENT_PROVIDER:-}" in
  '') fail "SIDERA_REAL_MAIN_AGENT_PROVIDER is missing" ;;
esac
case "${SIDERA_REAL_GATE_HELPER:-}" in
  '') fail "SIDERA_REAL_GATE_HELPER is missing" ;;
esac
[ -x "$SIDERA_REAL_GATE_HELPER" ] || fail "SIDERA_REAL_GATE_HELPER is not executable"

# The release gate never needs the provider token. The stack and preflight read
# it from the protected env file and explicitly scope it to Gateway only.
unset SIDERA_AKASHIC_WORKSPACE_TOKEN SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN
export SIDERA_DATABASE_URL SIDERA_WORKSPACE_ID SIDERA_PLANNING_PROFILE_ID
export SIDERA_PLANNING_PROFILE_VERSION SIDERA_CONTROL_PLANE_URL SIDERA_WEB_URL
export SIDERA_MAIN_AGENT_GATEWAY_URL
export SIDERA_R5_2_ENV_FILE="$ENV_FILE" SIDERA_R5_2_RUNTIME_DIR="$RUNTIME_DIR"

run_step() {
  local label=$1
  shift
  printf 'r5-2 release gate: %s\n' "$label"
  if "$@"; then
    return 0
  fi
  printf 'r5-2 release gate: failed at %s\n' "$label" >&2
  exit 1
}

verify_stack_health() {
  local status_output expected
  if ! status_output=$(SIDERA_R5_2_ENV_FILE="$ENV_FILE" \
    SIDERA_R5_2_RUNTIME_DIR="$RUNTIME_DIR" \
    "$SCRIPT_DIR/r5-2-stack.sh" status 2>&1); then
    printf '%s\n' "$status_output" >&2
    return 1
  fi
  printf '%s\n' "$status_output"
  for expected in \
    'control-plane: running/healthy' \
    'planning-worker: running' \
    'main-agent-gateway: running/healthy' \
    'web: running/healthy'; do
    case "$status_output" in
      *"$expected"*) ;;
      *) printf 'missing healthy component state: %s\n' "$expected" >&2; return 1 ;;
    esac
  done
}

verify_repo_boundary() {
  local akashic_status tracked_paths
  if ! git -C "$REPO_ROOT" diff --quiet -- akashic-agent; then
    printf 'akashic-agent has a tracked source diff\n' >&2
    return 1
  fi
  akashic_status=$(git -C "$REPO_ROOT" status --short -- akashic-agent 2>/dev/null || true)
  [ -z "$akashic_status" ] || {
    printf 'akashic-agent has an uncommitted worktree change\n' >&2
    return 1
  }
  tracked_paths=$(git -C "$REPO_ROOT" ls-files -- \
    '.sidera' '.sidera/**' '.env' '.env.*' 'config/local' 'config/local/**' \
    'references' 'references/**' '.superpowers' '.superpowers/**')
  [ -z "$tracked_paths" ] || {
    printf 'machine-local or handoff path is tracked\n' >&2
    return 1
  }
  printf 'akashic-agent tree: '
  git -C "$REPO_ROOT" rev-parse HEAD:akashic-agent
}

run_step preflight env -u SIDERA_AKASHIC_WORKSPACE_TOKEN \
  -u SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN \
  bash "$SCRIPT_DIR/r5-2-preflight.sh" --env-file "$ENV_FILE"
run_step 'stack health' verify_stack_health

run_step 'control-plane unit' uv run --project "$REPO_ROOT/services/control-plane" \
  pytest "$REPO_ROOT/services/control-plane/tests/unit" -q
run_step 'control-plane integration' uv run --project "$REPO_ROOT/services/control-plane" \
  pytest -m integration "$REPO_ROOT/services/control-plane/tests/integration" -q
run_step 'control-plane ruff' uv run --project "$REPO_ROOT/services/control-plane" \
  ruff check "$REPO_ROOT/services/control-plane/src" "$REPO_ROOT/services/control-plane/tests"
run_step 'control-plane pyright' uv run --project "$REPO_ROOT/services/control-plane" \
  pyright "$REPO_ROOT/services/control-plane/src" "$REPO_ROOT/services/control-plane/tests"
run_step 'control-plane alembic' uv run --project "$REPO_ROOT/services/control-plane" \
  alembic -c "$REPO_ROOT/services/control-plane/alembic.ini" check

run_step 'Akashic plugin tests' env \
  PYTHONPATH="$REPO_ROOT/integrations/akashic:$REPO_ROOT/akashic-agent" \
  uv run --project "$REPO_ROOT/akashic-agent" pytest \
  "$REPO_ROOT/integrations/akashic/tests" -q
run_step 'Akashic plugin ruff' uv run --project "$REPO_ROOT/akashic-agent" \
  ruff check "$REPO_ROOT/integrations/akashic"
run_step 'Akashic plugin pyright' uv run --project "$REPO_ROOT/akashic-agent" \
  pyright -p "$REPO_ROOT/integrations/akashic"

run_step 'Gateway tests' uv run --project "$REPO_ROOT/services/main-agent-gateway" \
  pytest "$REPO_ROOT/services/main-agent-gateway/tests" -q
run_step 'Gateway ruff' uv run --project "$REPO_ROOT/services/main-agent-gateway" \
  ruff check "$REPO_ROOT/services/main-agent-gateway"
run_step 'Gateway pyright' uv run --project "$REPO_ROOT/services/main-agent-gateway" \
  pyright "$REPO_ROOT/services/main-agent-gateway"

run_step 'Web unit' npm --prefix "$REPO_ROOT/apps/web" test
run_step 'Web lint' npm --prefix "$REPO_ROOT/apps/web" run lint
run_step 'Web build' npm --prefix "$REPO_ROOT/apps/web" run build
run_step 'Web deterministic E2E' npm --prefix "$REPO_ROOT/apps/web" run test:e2e

run_step 'real Gateway loop' uv run --project "$REPO_ROOT/services/main-agent-gateway" \
  pytest "$REPO_ROOT/services/main-agent-gateway/tests/integration/test_real_vertical_loop.py" -q
run_step 'real Web journey' npm --prefix "$REPO_ROOT/apps/web" run test:e2e -- \
  tests/e2e/real-main-agent.spec.ts --project=desktop-chromium

run_step 'Akashic host identity' verify_repo_boundary
printf 'r5-2 release gate: passed\n'
