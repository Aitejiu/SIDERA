#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
CONTROL_PLANE_ROOT="$REPO_ROOT/services/control-plane"
AKASHIC_ROOT="$REPO_ROOT/integrations/akashic"
WEB_ROOT="$REPO_ROOT/apps/web"
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-3-3-gate.XXXXXX")

cleanup() {
  rm -rf "$TEMP_DIR"
}

trap cleanup EXIT HUP INT TERM

fail() {
  printf 'r6-3-3 notification gateway: %s\n' "$1" >&2
  exit 1
}

run_step() {
  local label=$1
  shift
  printf 'r6-3-3 notification gateway: %s\n' "$label"
  "$@"
}

safe_env() {
  env \
    -u ANTHROPIC_API_KEY \
    -u DEEPSEEK_API_KEY \
    -u OPENAI_API_KEY \
    -u OPENCODE_API_KEY \
    -u OPENCODE_CONFIG_CONTENT \
    -u SIDERA_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN \
    "$@"
}

SIDERA_TEST_DATABASE_URL=${SIDERA_TEST_DATABASE_URL:-}
[ -n "$SIDERA_TEST_DATABASE_URL" ] \
  || fail "SIDERA_TEST_DATABASE_URL is required and must point to a disposable PostgreSQL database"

export SIDERA_DATABASE_URL="$SIDERA_TEST_DATABASE_URL"

LOCAL_STATE_BEFORE="$TEMP_DIR/local-state-before.txt"
LOCAL_STATE_AFTER="$TEMP_DIR/local-state-after.txt"
git -C "$REPO_ROOT" status --short -- .superpowers references >"$LOCAL_STATE_BEFORE"

run_step 'migration upgrade' safe_env uv run --project "$CONTROL_PLANE_ROOT" \
  alembic -c "$CONTROL_PLANE_ROOT/alembic.ini" upgrade head
run_step 'migration check' safe_env uv run --project "$CONTROL_PLANE_ROOT" \
  alembic -c "$CONTROL_PLANE_ROOT/alembic.ini" check
run_step 'control-plane notification vertical' safe_env uv run --project "$CONTROL_PLANE_ROOT" \
  pytest -q \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_3_notification_gateway.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_3_notification_faults.py"
run_step 'control-plane notification integration' safe_env uv run --project "$CONTROL_PLANE_ROOT" \
  pytest -q \
  "$CONTROL_PLANE_ROOT/tests/integration/test_notification_reconciliation.py" \
  "$CONTROL_PLANE_ROOT/tests/integration/test_notification_delivery_concurrency.py" \
  "$CONTROL_PLANE_ROOT/tests/integration/test_notification_operations.py" \
  "$CONTROL_PLANE_ROOT/tests/integration/test_notification_resync.py" \
  "$CONTROL_PLANE_ROOT/tests/integration/test_notification_query_api.py" \
  "$CONTROL_PLANE_ROOT/tests/integration/test_notification_origin_binding.py"
run_step 'control-plane static checks' safe_env uv run --project "$CONTROL_PLANE_ROOT" \
  ruff check "$CONTROL_PLANE_ROOT/src" "$CONTROL_PLANE_ROOT/tests" "$CONTROL_PLANE_ROOT/migrations"
run_step 'control-plane type checks' safe_env uv run --project "$CONTROL_PLANE_ROOT" \
  pyright --project "$CONTROL_PLANE_ROOT/pyproject.toml"

run_step 'Akashic notification vertical' safe_env env \
  PYTHONPATH="$AKASHIC_ROOT:$REPO_ROOT/akashic-agent" \
  uv run --project "$AKASHIC_ROOT" pytest -q \
  "$AKASHIC_ROOT/tests/test_notification_gateway.py" \
  "$AKASHIC_ROOT/tests/test_notification_vertical.py"
run_step 'Akashic static checks' safe_env uv run --project "$AKASHIC_ROOT" \
  ruff check "$AKASHIC_ROOT"
run_step 'Akashic type checks' safe_env uv run --project "$AKASHIC_ROOT" \
  pyright --project "$AKASHIC_ROOT/pyproject.toml" "$AKASHIC_ROOT/sidera_task_bridge"

run_step 'Web unit tests' safe_env npm --prefix "$WEB_ROOT" test
run_step 'Web lint' safe_env npm --prefix "$WEB_ROOT" run lint
run_step 'Web build' safe_env npm --prefix "$WEB_ROOT" run build

OPENAPI_TYPESCRIPT="$WEB_ROOT/node_modules/.bin/openapi-typescript"
[ -x "$OPENAPI_TYPESCRIPT" ] \
  || fail "Web openapi-typescript executable is missing; install apps/web dependencies first"
mkdir -p "$TEMP_DIR/contracts"
run_step 'generated contract drift' safe_env bash -c '
  set -euo pipefail
  output_dir=$1
  web_root=$2
  openapi_typescript=$3
  "$openapi_typescript" "$web_root/openapi.json" -o "$output_dir/schema.d.ts"
  cmp -s "$output_dir/schema.d.ts" "$web_root/src/shared/api/schema.d.ts" \
    || { diff -u "$web_root/src/shared/api/schema.d.ts" "$output_dir/schema.d.ts"; exit 1; }
' bash "$TEMP_DIR/contracts" "$WEB_ROOT" "$OPENAPI_TYPESCRIPT"

if [ "${SIDERA_RUN_WEB_E2E:-1}" = "1" ]; then
  run_step 'Web notification E2E' safe_env env \
    SIDERA_E2E_CONTROL_PLANE_PORT="${SIDERA_E2E_CONTROL_PLANE_PORT:-18012}" \
    SIDERA_E2E_KNOWLEDGE_SERVICE_PORT="${SIDERA_E2E_KNOWLEDGE_SERVICE_PORT:-18022}" \
    SIDERA_E2E_MAIN_AGENT_PORT="${SIDERA_E2E_MAIN_AGENT_PORT:-18013}" \
    SIDERA_E2E_WEB_PORT="${SIDERA_E2E_WEB_PORT:-18418}" \
    npm --prefix "$WEB_ROOT" run test:e2e -- tests/e2e/notification-inbox.spec.ts
else
  printf 'SKIPPED: Web notification E2E disabled by SIDERA_RUN_WEB_E2E\n'
fi

if [ "${SIDERA_RUN_REAL_NOTIFICATION_GATE:-0}" = "1" ]; then
  [ -n "${SIDERA_BASIC_NOTIFICATION_MANIFEST:-}" ] \
    || fail "SIDERA_BASIC_NOTIFICATION_MANIFEST is required when the real notification gate is enabled"
  [ -f "$SIDERA_BASIC_NOTIFICATION_MANIFEST" ] \
    || fail "SIDERA_BASIC_NOTIFICATION_MANIFEST does not point to a file"
  run_step 'fixed real Akashic/Telegram gate' safe_env env \
    PYTHONPATH="$AKASHIC_ROOT:$REPO_ROOT/akashic-agent" \
    SIDERA_RUN_REAL_NOTIFICATION_GATE=1 \
    SIDERA_BASIC_NOTIFICATION_MANIFEST="$SIDERA_BASIC_NOTIFICATION_MANIFEST" \
    uv run --project "$AKASHIC_ROOT" pytest -q \
    "$AKASHIC_ROOT/tests/test_basic_real_notification.py"
else
  printf 'SKIPPED: fixed real Akashic/Telegram gate not enabled\n'
fi

git -C "$REPO_ROOT" status --short -- .superpowers references >"$LOCAL_STATE_AFTER"
cmp -s "$LOCAL_STATE_BEFORE" "$LOCAL_STATE_AFTER" \
  || fail '.superpowers/ or references/ changed during the gate'

printf 'r6-3-3 notification gateway: passed\n'
