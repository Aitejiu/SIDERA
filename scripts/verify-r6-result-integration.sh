#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
CONTROL_PLANE_ROOT="$REPO_ROOT/services/control-plane"
AKASHIC_ROOT="$REPO_ROOT/integrations/akashic"
WEB_ROOT="$REPO_ROOT/apps/web"
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-result-integration-gate.XXXXXX")

LOCAL_FAILED=0
REAL_INTEGRATION_ACCEPTANCE=NOT_RUN
RESULT_INTEGRATION_DATABASE_CHECK=FAIL
STEP_COUNT=0
PRESERVE_TEMP=0

cleanup() {
  if ((PRESERVE_TEMP != 0)); then
    printf 'RESULT_INTEGRATION_DIAGNOSTICS=retained\n' >&2
  else
    rm -rf "$TEMP_DIR"
  fi
}

trap cleanup EXIT HUP INT TERM

mark_local_failure() {
  LOCAL_FAILED=1
  PRESERVE_TEMP=1
}

run_step() {
  local label=$1
  shift
  STEP_COUNT=$((STEP_COUNT + 1))
  local output_file="$TEMP_DIR/step-${STEP_COUNT}.log"
  printf 'r6 result integration: %s\n' "$label"
  if "$@" >"$output_file" 2>&1; then
    printf 'r6 result integration: %s: PASS\n' "$label"
  else
    local step_rc=$?
    printf 'r6 result integration: %s: FAIL (exit %s)\n' "$label" "$step_rc" >&2
    mark_local_failure
  fi
}

safe_local_env() {
  env \
    -u ANTHROPIC_API_KEY \
    -u ANTHROPIC_AUTH_TOKEN \
    -u ANTHROPIC_BASE_URL \
    -u AWS_ACCESS_KEY_ID \
    -u AWS_SECRET_ACCESS_KEY \
    -u AZURE_OPENAI_API_KEY \
    -u AZURE_OPENAI_ENDPOINT \
    -u CEREBRAS_API_KEY \
    -u COHERE_API_KEY \
    -u DEEPSEEK_API_KEY \
    -u FIREWORKS_API_KEY \
    -u GOOGLE_API_KEY \
    -u GROQ_API_KEY \
    -u MISTRAL_API_KEY \
    -u OPENAI_API_BASE \
    -u OPENAI_API_KEY \
    -u OPENAI_BASE_URL \
    -u OPENROUTER_API_KEY \
    -u OPENCODE_API_KEY \
    -u OPENCODE_CONFIG_CONTENT \
    -u OPENCODE_CONFIG_DIR \
    -u PERPLEXITYAI_API_KEY \
    -u SIDERA_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_E2E_MAIN_AGENT_TOKEN \
    -u SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN \
    -u SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_OPENCODE_CONFIG_CONTENT \
    -u SIDERA_OPENCODE_EXECUTABLE \
    -u SIDERA_OPENCODE_EXPECTED_VERSION \
    -u SIDERA_OPENCODE_MODEL \
    -u SIDERA_PLANNING_OPENCODE_EXECUTABLE \
    -u SIDERA_PLANNING_OPENCODE_EXPECTED_VERSION \
    -u SIDERA_PLANNING_OPENCODE_MODEL \
    -u SIDERA_REAL_RESULT_PROVIDER \
    -u SIDERA_REAL_RESULT_MODEL \
    -u SIDERA_REAL_RESULT_RUN_ID \
    -u SIDERA_REAL_RESULT_ENDPOINT_HOST \
    -u SIDERA_REAL_RESULT_CREDENTIAL_SOURCE \
    -u SIDERA_REAL_RESULT_REPOSITORY \
    -u SIDERA_REAL_RESULT_FALLBACKS \
    -u SIDERA_REAL_RESULT_APPROVED_FALLBACKS \
    -u SIDERA_REAL_RESULT_LOCAL_GATE_FILE \
    -u SIDERA_REAL_RESULT_MAX_EXECUTIONS \
    -u SIDERA_REAL_RESULT_WALL_SECONDS \
    -u SIDERA_RUN_REAL_RESULT_INTEGRATION \
    -u SIDERA_REAL_RESULT_INTEGRATION_AUTHORIZED \
    -u SIDERA_RESULT_REAL_GATE_PREFLIGHT \
    -u SIDERA_RESULT_INTEGRATION_REAL_PHASE \
    -u SIDERA_RUN_REAL_RESULT_INPUTS \
    -u SIDERA_RESULT_INPUTS_REAL_APPROVED \
    -u TOGETHER_API_KEY \
    -u XAI_API_KEY \
    SIDERA_RESULT_INPUTS_ENABLED=false \
    "$@"
}

validate_test_database() {
  if [[ -z "${SIDERA_TEST_DATABASE_URL:-}" ]]; then
    return 1
  fi
  python3 - <<'PY'
import os
import re
import sys
from urllib.parse import unquote, urlsplit

value = os.environ.get("SIDERA_TEST_DATABASE_URL", "")
parsed = urlsplit(value)
if parsed.scheme != "postgresql+psycopg":
    raise SystemExit(1)
if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit(1)
database_name = unquote(parsed.path.lstrip("/"))
if not re.fullmatch(r"sidera_test(?:_[A-Za-z0-9]+)*", database_name):
    raise SystemExit(1)
PY
}

classify_dirty_scope() {
  local status_output
  status_output=$(git -C "$REPO_ROOT" status --porcelain=v1 -z --untracked-files=all 2>/dev/null) || return 1
  if [[ -z "$status_output" ]]; then
    printf '%s\n' 'clean'
    return 0
  fi
  while IFS= read -r -d '' line; do
    local path="${line:3}"
    case "$path" in
      .superpowers/*|references/*|docs/superpowers/specs/2026-09-05-sidera-r6-3-4-scope-proposal.md)
        ;;
      *)
        printf '%s\n' 'dirty'
        return 0
        ;;
    esac
  done <<<"$status_output"
  printf '%s\n' 'protected-only'
}

check_protected_state() {
  local before=$1
  local after=$2
  git -C "$REPO_ROOT" status --short -- .superpowers references \
    docs/superpowers/specs/2026-09-05-sidera-r6-3-4-scope-proposal.md >"$after" \
    || return 1
  cmp -s "$before" "$after"
}

compare_files() {
  local expected=$1
  local actual=$2
  cmp -s "$expected" "$actual"
}

COMMIT=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null) || COMMIT=unknown
DIRTY_SCOPE=$(classify_dirty_scope) || DIRTY_SCOPE=unknown
PROTECTED_STATE_BEFORE="$TEMP_DIR/protected-before.txt"
PROTECTED_STATE_AFTER="$TEMP_DIR/protected-after.txt"
if ! git -C "$REPO_ROOT" status --short -- .superpowers references \
  docs/superpowers/specs/2026-09-05-sidera-r6-3-4-scope-proposal.md \
  >"$PROTECTED_STATE_BEFORE" 2>/dev/null; then
  mark_local_failure
fi

REAL_RUN_FLAG=${SIDERA_RUN_REAL_RESULT_INTEGRATION:-}
REAL_APPROVED_FLAG=${SIDERA_REAL_RESULT_INTEGRATION_AUTHORIZED:-}
if [[ -n "$REAL_RUN_FLAG" || -n "$REAL_APPROVED_FLAG" ]]; then
  if [[ "$REAL_RUN_FLAG" == "1" && "$REAL_APPROVED_FLAG" == "1" ]]; then
    REAL_INTEGRATION_ACCEPTANCE=INCOMPLETE
  else
    REAL_INTEGRATION_ACCEPTANCE=INCOMPLETE
    mark_local_failure
  fi
fi

if validate_test_database; then
  RESULT_INTEGRATION_DATABASE_CHECK=PASS
  export SIDERA_DATABASE_URL="$SIDERA_TEST_DATABASE_URL"
else
  printf 'r6 result integration: database validation: FAIL\n' >&2
  mark_local_failure
fi

if (( LOCAL_FAILED == 0 )); then
  run_step 'migration upgrade' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    alembic -c "$CONTROL_PLANE_ROOT/alembic.ini" upgrade head
  run_step 'migration check' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    alembic -c "$CONTROL_PLANE_ROOT/alembic.ini" check

  run_step 'control-plane unit tests' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pytest "$CONTROL_PLANE_ROOT/tests/unit" \
    --ignore="$CONTROL_PLANE_ROOT/tests/unit/test_opencode_planner.py" -q
  run_step 'OpenCode planner unit tests' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pytest "$CONTROL_PLANE_ROOT/tests/unit/test_opencode_planner.py" -q
  run_step 'control-plane integration tests' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pytest -m integration "$CONTROL_PLANE_ROOT/tests/integration" -q
  run_step 'provider-free result integration E2E' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pytest -q \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_result_integration_flow.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_result_integration_faults.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_2_fake_handoff.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_1_collaboration_runtime.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_1_collaboration_faults.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_4_result_input_loop.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_4_result_input_faults.py"
  run_step 'real result entry guard' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pytest -q \
    "$CONTROL_PLANE_ROOT/tests/unit/test_real_result_integration_guard.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_real_opencode_result_integration.py"
  run_step 'control-plane Ruff' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    ruff check "$CONTROL_PLANE_ROOT/src" "$CONTROL_PLANE_ROOT/tests" "$CONTROL_PLANE_ROOT/migrations"
  run_step 'control-plane Pyright' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pyright --project "$CONTROL_PLANE_ROOT/pyproject.toml"

  run_step 'Akashic tests' safe_local_env env \
    PYTHONPATH="$AKASHIC_ROOT:$REPO_ROOT/akashic-agent" \
    uv run --project "$AKASHIC_ROOT" pytest "$AKASHIC_ROOT/tests" -q
  run_step 'Akashic Ruff' safe_local_env uv run --project "$AKASHIC_ROOT" \
    ruff check "$AKASHIC_ROOT"
  run_step 'Akashic Pyright' safe_local_env uv run --project "$AKASHIC_ROOT" \
    pyright --project "$AKASHIC_ROOT/pyproject.toml" "$AKASHIC_ROOT/sidera_task_bridge"

  run_step 'Web unit tests' safe_local_env npm --prefix "$WEB_ROOT" test
  run_step 'Web lint' safe_local_env npm --prefix "$WEB_ROOT" run lint
  run_step 'Web build' safe_local_env npm --prefix "$WEB_ROOT" run build

  mkdir -p "$TEMP_DIR/contracts"
  run_step 'OpenAPI export' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    python -m sidera_control_plane.api.openapi_contract \
    --output "$TEMP_DIR/contracts/openapi.json"
  run_step 'tracked OpenAPI drift' compare_files \
    "$WEB_ROOT/openapi.json" "$TEMP_DIR/contracts/openapi.json"
  run_step 'generated TypeScript contract' safe_local_env npm --prefix "$WEB_ROOT" exec -- \
    openapi-typescript "$TEMP_DIR/contracts/openapi.json" \
    -o "$TEMP_DIR/contracts/schema.d.ts"
  run_step 'tracked TypeScript contract drift' compare_files \
    "$WEB_ROOT/src/shared/api/schema.d.ts" "$TEMP_DIR/contracts/schema.d.ts"

  run_step 'result preflight shell syntax' bash -n \
    "$REPO_ROOT/scripts/r6-result-integration-preflight.sh"
  run_step 'result preflight disabled smoke' safe_local_env env \
    SIDERA_DATABASE_URL="$SIDERA_TEST_DATABASE_URL" \
    bash "$REPO_ROOT/scripts/r6-result-integration-preflight.sh"
  run_step 'total gate shell syntax' bash -n "$0"
  run_step 'Web result integration three-viewport E2E' safe_local_env env \
    CI=1 \
    SIDERA_E2E_CONTROL_PLANE_PORT="${SIDERA_E2E_CONTROL_PLANE_PORT:-18100}" \
    SIDERA_E2E_KNOWLEDGE_SERVICE_PORT="${SIDERA_E2E_KNOWLEDGE_SERVICE_PORT:-18120}" \
    SIDERA_E2E_WEB_PORT="${SIDERA_E2E_WEB_PORT:-18418}" \
    npm --prefix "$WEB_ROOT" run test:e2e -- \
    tests/e2e/result-integration.spec.ts \
    --project=desktop-chromium \
    --project=tablet-chromium \
    --project=mobile-chromium
  run_step 'protected worktree state' check_protected_state \
    "$PROTECTED_STATE_BEFORE" "$PROTECTED_STATE_AFTER"
  run_step 'git diff check' safe_local_env git -C "$REPO_ROOT" diff --check
else
  printf 'r6 result integration: local checks not started because database/authorization guard failed\n' >&2
fi

printf 'RESULT_INTEGRATION_COMMIT=%s\n' "$COMMIT"
printf 'RESULT_INTEGRATION_DIRTY_SCOPE=%s\n' "$DIRTY_SCOPE"
printf 'RESULT_INTEGRATION_DATABASE_CHECK=%s\n' "$RESULT_INTEGRATION_DATABASE_CHECK"
printf 'RESULT_INTEGRATION_CHECK_COUNT=%s\n' "$STEP_COUNT"
if (( LOCAL_FAILED == 0 )); then
  printf 'RESULT_INTEGRATION_LOCAL_GATE=PASS\n'
else
  printf 'RESULT_INTEGRATION_LOCAL_GATE=FAIL\n'
fi
printf 'REAL_INTEGRATION_ACCEPTANCE=%s\n' "$REAL_INTEGRATION_ACCEPTANCE"

if (( LOCAL_FAILED != 0 )) || [[ "$REAL_INTEGRATION_ACCEPTANCE" == "FAIL" || "$REAL_INTEGRATION_ACCEPTANCE" == "INCOMPLETE" ]]; then
  exit 1
fi
