#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
CONTROL_PLANE_ROOT="$REPO_ROOT/services/control-plane"
AKASHIC_ROOT="$REPO_ROOT/integrations/akashic"
WEB_ROOT="$REPO_ROOT/apps/web"
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-3-4-gate.XXXXXX")

LOCAL_FAILED=0
REAL_INPUT_ACCEPTANCE=NOT_RUN
PRESERVE_TEMP=0

cleanup() {
  if (( PRESERVE_TEMP != 0 )); then
    printf 'r6-3-4 result inputs: diagnostic artifacts preserved at %s\n' "$TEMP_DIR" >&2
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
  printf 'r6-3-4 result inputs: %s\n' "$label"
  if "$@"; then
    printf 'r6-3-4 result inputs: %s: PASS\n' "$label"
  else
    local status=$?
    printf 'r6-3-4 result inputs: %s: FAIL (exit %s)\n' "$label" "$status" >&2
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
    -u SIDERA_REAL_RESULT_INPUT_PROVIDER \
    -u SIDERA_REAL_RESULT_INPUT_ENDPOINT_HOST \
    -u SIDERA_REAL_RESULT_INPUT_ADAPTER_CONFIG_VERSION \
    -u SIDERA_REAL_RESULT_INPUT_FALLBACKS \
    -u SIDERA_REAL_RESULT_INPUT_APPROVED_FALLBACKS \
    -u SIDERA_REAL_RESULT_INPUT_BUDGET_MICROUNITS \
    -u SIDERA_RUN_REAL_RESULT_INPUTS \
    -u SIDERA_RESULT_INPUTS_REAL_APPROVED \
    -u TOGETHER_API_KEY \
    -u XAI_API_KEY \
    SIDERA_RESULT_INPUTS_ENABLED=false \
    "$@"
}

safe_real_env() {
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
    -u TOGETHER_API_KEY \
    -u XAI_API_KEY \
    SIDERA_RUN_REAL_RESULT_INPUTS=1 \
    SIDERA_RESULT_INPUTS_REAL_APPROVED=1 \
    "$@"
}

validate_test_database() {
  python3 - <<'PY'
import os
import re
import sys
from urllib.parse import unquote, urlsplit

value = os.environ.get("SIDERA_TEST_DATABASE_URL", "")
if not value:
    print("SIDERA_TEST_DATABASE_URL is required", file=sys.stderr)
    raise SystemExit(1)

parsed = urlsplit(value)
if parsed.scheme != "postgresql+psycopg":
    print("SIDERA_TEST_DATABASE_URL must use postgresql+psycopg", file=sys.stderr)
    raise SystemExit(1)
if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
    print("SIDERA_TEST_DATABASE_URL must use a loopback host", file=sys.stderr)
    raise SystemExit(1)

database_name = unquote(parsed.path.lstrip("/"))
if not re.fullmatch(r"sidera_test(?:_[A-Za-z0-9]+)*", database_name):
    print("SIDERA_TEST_DATABASE_URL must target a sidera_test database", file=sys.stderr)
    raise SystemExit(1)
PY
}

compare_files() {
  local expected=$1
  local actual=$2
  if cmp -s "$expected" "$actual"; then
    return 0
  fi
  printf 'generated file drift: %s differs from %s\n' "$actual" "$expected" >&2
  return 1
}

check_worktree_state() {
  local before=$1
  local after=$2
  git -C "$REPO_ROOT" status --short -- .superpowers references >"$after" || return 1
  cmp -s "$before" "$after" || {
    printf '.superpowers/ or references/ changed during the gate\n' >&2
    return 1
  }
}

SIDERA_TEST_DATABASE_URL=${SIDERA_TEST_DATABASE_URL:-}
if ! validate_test_database; then
  mark_local_failure
else
  export SIDERA_DATABASE_URL="$SIDERA_TEST_DATABASE_URL"
fi

LOCAL_STATE_BEFORE="$TEMP_DIR/local-state-before.txt"
LOCAL_STATE_AFTER="$TEMP_DIR/local-state-after.txt"
if ! git -C "$REPO_ROOT" status --short -- .superpowers references >"$LOCAL_STATE_BEFORE"; then
  printf 'could not capture protected worktree state\n' >&2
  mark_local_failure
fi

if (( LOCAL_FAILED == 0 )); then
  run_step 'migration upgrade' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    alembic -c "$CONTROL_PLANE_ROOT/alembic.ini" upgrade head
  run_step 'migration check' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    alembic -c "$CONTROL_PLANE_ROOT/alembic.ini" check

  run_step 'control-plane unit tests' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pytest "$CONTROL_PLANE_ROOT/tests/unit" -q
  run_step 'control-plane integration tests' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pytest -m integration "$CONTROL_PLANE_ROOT/tests/integration" -q
  run_step 'control-plane provider-free E2E' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pytest -q \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_2_fake_handoff.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_1_collaboration_runtime.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_1_collaboration_faults.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_4_result_input_loop.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_4_result_input_faults.py"
  run_step 'Task32 real gate guard' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    pytest -q \
    "$CONTROL_PLANE_ROOT/tests/unit/test_result_input_real_gate_guard.py" \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_4_real_opencode_inputs.py"
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

  run_step 'R6.3.4 preflight shell syntax' bash -n \
    "$REPO_ROOT/scripts/r6-3-4-preflight.sh"
  run_step 'R6.3.4 preflight fixture tests' bash \
    "$REPO_ROOT/scripts/tests/test-r6-3-4-preflight.sh"
  run_step 'R6.3.4 gate fixture syntax' bash -n "$SCRIPT_DIR/verify-r6-3-4-result-inputs.sh"
  if [[ "${SIDERA_SKIP_WEB_E2E:-0}" == "1" ]]; then
    printf 'Web E2E was explicitly skipped; this is not a passing local gate\n' >&2
    mark_local_failure
  else
    run_step 'Web provider-free three-viewport E2E' safe_local_env env \
      CI=1 \
      SIDERA_E2E_CONTROL_PLANE_PORT="${SIDERA_E2E_CONTROL_PLANE_PORT:-18072}" \
      SIDERA_E2E_KNOWLEDGE_SERVICE_PORT="${SIDERA_E2E_KNOWLEDGE_SERVICE_PORT:-18082}" \
      SIDERA_E2E_MAIN_AGENT_PORT="${SIDERA_E2E_MAIN_AGENT_PORT:-18073}" \
      SIDERA_E2E_WEB_PORT="${SIDERA_E2E_WEB_PORT:-18428}" \
      npm --prefix "$WEB_ROOT" run test:e2e -- \
      tests/e2e/r6-3-1-collaboration.spec.ts \
      tests/e2e/r6-3-2-governance.spec.ts \
      tests/e2e/notification-inbox.spec.ts \
      tests/e2e/r6-3-4-result-inputs.spec.ts \
      --project=desktop-chromium \
      --project=tablet-chromium \
      --project=mobile-chromium
  fi

  run_step 'protected worktree state' check_worktree_state \
    "$LOCAL_STATE_BEFORE" "$LOCAL_STATE_AFTER"
else
  printf 'r6-3-4 result inputs: local checks not started because database validation failed\n' >&2
fi

if [[ "${SIDERA_RUN_REAL_RESULT_INPUTS:-}" == "1" \
  && "${SIDERA_RESULT_INPUTS_REAL_APPROVED:-}" == "1" ]]; then
  if (( LOCAL_FAILED != 0 )); then
    printf 'SKIPPED: real OpenCode acceptance requires a passing local gate\n'
  else
    printf 'r6-3-4 result inputs: authorized real OpenCode acceptance\n'
    if safe_real_env uv run --project "$CONTROL_PLANE_ROOT" pytest -q \
      "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_4_real_opencode_inputs.py"; then
      REAL_INPUT_ACCEPTANCE=PASS
    else
      real_status=$?
      printf 'r6-3-4 result inputs: real OpenCode acceptance: FAIL (exit %s)\n' \
        "$real_status" >&2
      REAL_INPUT_ACCEPTANCE=FAIL
      PRESERVE_TEMP=1
    fi
  fi
else
  printf 'SKIPPED: real OpenCode acceptance requires both explicit authorization flags\n'
fi

if (( LOCAL_FAILED == 0 )); then
  printf 'LOCAL_GATE=PASS\n'
else
  printf 'LOCAL_GATE=FAIL\n'
fi
printf 'REAL_INPUT_ACCEPTANCE=%s\n' "$REAL_INPUT_ACCEPTANCE"

if (( LOCAL_FAILED != 0 )) || [[ "$REAL_INPUT_ACCEPTANCE" == "FAIL" ]]; then
  exit 1
fi
