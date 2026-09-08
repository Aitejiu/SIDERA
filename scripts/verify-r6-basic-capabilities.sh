#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
CONTROL_PLANE_ROOT="$REPO_ROOT/services/control-plane"
KNOWLEDGE_ROOT="$REPO_ROOT/services/knowledge-service"
AKASHIC_ROOT="$REPO_ROOT/integrations/akashic"
DSH_ROOT="$REPO_ROOT/integrations/dsh-worker"
WEB_ROOT="$REPO_ROOT/apps/web"
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-basic-capabilities.XXXXXX")

MANIFEST_PATH=${SIDERA_BASIC_ACCEPTANCE_MANIFEST:-}
RECORD_PATH=${SIDERA_BASIC_CAPABILITY_RECORD:-}
CHECKS_FILE="$TEMP_DIR/checks.tsv"
PROTECTED_BEFORE="$TEMP_DIR/protected-before.txt"
PROTECTED_AFTER="$TEMP_DIR/protected-after.txt"
EVIDENCE_DIR=${SIDERA_BASIC_ACCEPTANCE_EVIDENCE_DIR:-"$TEMP_DIR/evidence"}
STEP_COUNT=0
LOCAL_FAILED=0
DATABASE_READY=0
REAL_MODE=0
mkdir -p "$EVIDENCE_DIR"
: >"$CHECKS_FILE"

while (($# > 0)); do
  case "$1" in
    --manifest)
      shift
      (($# > 0)) || { printf 'basic capabilities: --manifest requires a path\n' >&2; exit 2; }
      MANIFEST_PATH=$1
      ;;
    --record)
      shift
      (($# > 0)) || { printf 'basic capabilities: --record requires a path\n' >&2; exit 2; }
      RECORD_PATH=$1
      ;;
    *)
      printf 'basic capabilities: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -n "$RECORD_PATH" && "$RECORD_PATH" != /* ]]; then
  RECORD_PATH="$PWD/$RECORD_PATH"
fi

cleanup() {
  if [[ "${SIDERA_KEEP_BASIC_GATE_DIAGNOSTICS:-0}" == "1" ]]; then
    printf 'BASIC_GATE_DIAGNOSTICS=%s\n' "$TEMP_DIR" >&2
  else
    rm -rf "$TEMP_DIR"
  fi
}
trap cleanup EXIT HUP INT TERM

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
    -u SIDERA_RUN_REAL_BASIC \
    -u SIDERA_RUN_REAL_NOTIFICATION_GATE \
    -u SIDERA_RUN_REAL_RESULT_INPUTS \
    -u SIDERA_RUN_REAL_RESULT_INTEGRATION \
    -u SIDERA_RUN_REAL_R5_3 \
    -u SIDERA_RUN_REAL_OPENCODE \
    -u TOGETHER_API_KEY \
    -u XAI_API_KEY \
    "$@"
}

run_step() {
  local label=$1
  shift
  local output_file="$TEMP_DIR/step-${STEP_COUNT}.log"
  STEP_COUNT=$((STEP_COUNT + 1))
  output_file="$TEMP_DIR/step-${STEP_COUNT}.log"
  printf 'basic capabilities: %s\n' "$label"
  if "$@" >"$output_file" 2>&1; then
    if grep -Eq 'no tests ran|(^|[^0-9])0 passed([^0-9]|$)' "$output_file" \
      || { ! grep -Eq '[0-9]+ passed' "$output_file" \
        && grep -Eq '[0-9]+ skipped' "$output_file"; }; then
      LOCAL_FAILED=1
      printf '%s\t125\tFAIL\n' "$label" >>"$CHECKS_FILE"
      printf 'basic capabilities: %s: FAIL (zero or skipped-only tests)\n' "$label" >&2
      return
    fi
    printf '%s\t0\tPASS\n' "$label" >>"$CHECKS_FILE"
    printf 'basic capabilities: %s: PASS\n' "$label"
  else
    local status=$?
    LOCAL_FAILED=1
    printf '%s\t%s\tFAIL\n' "$label" "$status" >>"$CHECKS_FILE"
    printf 'basic capabilities: %s: FAIL (exit %s)\n' "$label" "$status" >&2
  fi
}

run_optional_step() {
  local label=$1
  shift
  if [[ "${SIDERA_SKIP_BASIC_OPTIONAL:-0}" == "1" ]]; then
    printf '%s\t0\tPASS\n' "$label" >>"$CHECKS_FILE"
    printf 'basic capabilities: %s: SKIPPED BY OPERATOR\n' "$label"
    return
  fi
  run_step "$label" "$@"
}

validate_database_url() {
  local value=${SIDERA_TEST_DATABASE_URL:-}
  [[ -n "$value" ]] || return 1
  python3 - "$value" <<'PY'
import re
import sys
from urllib.parse import unquote, urlsplit

parsed = urlsplit(sys.argv[1])
if parsed.scheme != "postgresql+psycopg":
    raise SystemExit(1)
if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit(1)
name = unquote(parsed.path.lstrip("/"))
if re.fullmatch(r"sidera_test(?:_[A-Za-z0-9]+)*", name) is None:
    raise SystemExit(1)
PY
}

check_protected_state() {
  git -C "$REPO_ROOT" status --short -- .superpowers references \
    docs/superpowers/specs/2026-09-05-sidera-r6-3-4-scope-proposal.md >"$2" \
    && cmp -s "$1" "$2"
}

if ! git -C "$REPO_ROOT" status --short -- .superpowers references \
  docs/superpowers/specs/2026-09-05-sidera-r6-3-4-scope-proposal.md >"$PROTECTED_BEFORE"; then
  LOCAL_FAILED=1
fi

DIRTY_SCOPE=$(safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
  python -m sidera_control_plane.local_gate_record classify \
  --repository "$REPO_ROOT" 2>"$TEMP_DIR/dirty-scope.log") || DIRTY_SCOPE=unknown
printf 'BASIC_DIRTY_SCOPE=%s\n' "$DIRTY_SCOPE"
if [[ "$DIRTY_SCOPE" == "dirty" || "$DIRTY_SCOPE" == "unknown" ]]; then
  LOCAL_FAILED=1
fi

if validate_database_url; then
  DATABASE_READY=1
  export SIDERA_DATABASE_URL="$SIDERA_TEST_DATABASE_URL"
  export SIDERA_KNOWLEDGE_DATABASE_URL="${SIDERA_KNOWLEDGE_TEST_DATABASE_URL:-postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_knowledge_test}"
  printf 'BASIC_DATABASE_CHECK=PASS\n'
else
  printf 'BASIC_DATABASE_CHECK=FAIL\n' >&2
  LOCAL_FAILED=1
fi

run_step 'shell syntax' bash -n \
  "$REPO_ROOT/scripts/r6-basic-stack.sh" \
  "$REPO_ROOT/scripts/r6-basic-acceptance.py" \
  "$REPO_ROOT/scripts/verify-r6-basic-capabilities.sh"
if [[ "${SIDERA_BASIC_CAPABILITY_TEST_MODE:-0}" == "1" ]]; then
  printf 'isolated stack stub\t0\tPASS\n' >>"$CHECKS_FILE"
  printf 'basic capabilities: isolated stack stub: PASS (test harness already ran it)\n'
else
  run_step 'isolated stack stub' bash "$REPO_ROOT/scripts/tests/test-r6-basic-stack.sh"
fi
run_step 'basic acceptance harness and scenario units' safe_local_env \
  uv run --project "$CONTROL_PLANE_ROOT" pytest -q \
  "$CONTROL_PLANE_ROOT/tests/unit/test_basic_acceptance_harness.py" \
  "$CONTROL_PLANE_ROOT/tests/unit/test_basic_planning_scenario.py" \
  "$CONTROL_PLANE_ROOT/tests/unit/test_basic_result_scenario.py" \
  "$CONTROL_PLANE_ROOT/tests/unit/test_basic_user_flow.py" \
  "$CONTROL_PLANE_ROOT/tests/unit/test_basic_intervention_scenario.py" \
  "$CONTROL_PLANE_ROOT/tests/unit/test_basic_revision_scenario.py" \
  "$CONTROL_PLANE_ROOT/tests/unit/test_basic_recovery_scenario.py" \
  "$CONTROL_PLANE_ROOT/tests/unit/test_basic_second_adapter_scenario.py" \
  "$CONTROL_PLANE_ROOT/tests/unit/test_basic_notification_scenario.py"
run_step 'control-plane unit tests' safe_local_env \
  uv run --project "$CONTROL_PLANE_ROOT" pytest -q "$CONTROL_PLANE_ROOT/tests/unit"
run_step 'control-plane contract and provider-free E2E' safe_local_env \
  uv run --project "$CONTROL_PLANE_ROOT" pytest -q \
  "$CONTROL_PLANE_ROOT/tests/contract" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_1_fake_transition_loop.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_2_fake_handoff.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_1_collaboration_runtime.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_1_collaboration_faults.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_2_governance.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_2_governance_faults.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_3_notification_gateway.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_3_notification_faults.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_4_result_input_loop.py" \
  "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_3_4_result_input_faults.py"
if (( DATABASE_READY == 1 )); then
  run_step 'control-plane integration tests' safe_local_env \
    uv run --project "$CONTROL_PLANE_ROOT" pytest -m integration -q \
    "$CONTROL_PLANE_ROOT/tests/integration"
else
  printf 'control-plane integration tests: NOT RUN (database unavailable)\n' >&2
fi
run_step 'control-plane Ruff' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
  ruff check "$CONTROL_PLANE_ROOT/src" "$CONTROL_PLANE_ROOT/tests" "$CONTROL_PLANE_ROOT/migrations"
run_step 'control-plane Pyright' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
  pyright --project "$CONTROL_PLANE_ROOT/pyproject.toml"

run_step 'knowledge service unit tests' safe_local_env \
  uv run --project "$KNOWLEDGE_ROOT" pytest -q "$KNOWLEDGE_ROOT/tests/unit"
if (( DATABASE_READY == 1 )); then
  run_step 'knowledge service integration tests' safe_local_env \
    uv run --project "$KNOWLEDGE_ROOT" pytest -m integration -q \
    "$KNOWLEDGE_ROOT/tests/integration"
else
  printf 'knowledge service integration tests: NOT RUN (database unavailable)\n' >&2
fi
run_step 'knowledge service Ruff and Pyright' safe_local_env \
  uv run --project "$KNOWLEDGE_ROOT" ruff check "$KNOWLEDGE_ROOT/src" "$KNOWLEDGE_ROOT/tests"
run_step 'knowledge service Pyright' safe_local_env \
  uv run --project "$KNOWLEDGE_ROOT" pyright --project "$KNOWLEDGE_ROOT/pyproject.toml"

run_step 'Akashic provider-free tests' safe_local_env env \
  PYTHONPATH="$AKASHIC_ROOT:$REPO_ROOT/akashic-agent" \
  uv run --project "$AKASHIC_ROOT" pytest -q "$AKASHIC_ROOT/tests"
run_step 'Akashic Ruff' safe_local_env uv run --project "$AKASHIC_ROOT" \
  ruff check "$AKASHIC_ROOT"
run_step 'Akashic Pyright' safe_local_env uv run --project "$AKASHIC_ROOT" \
  pyright --project "$AKASHIC_ROOT/pyproject.toml" "$AKASHIC_ROOT/sidera_task_bridge"

run_step 'DSH ACP host unit tests' safe_local_env npm --prefix "$DSH_ROOT" test
run_step 'Web unit tests' safe_local_env npm --prefix "$WEB_ROOT" test
run_step 'Web lint' safe_local_env npm --prefix "$WEB_ROOT" run lint
run_step 'Web build' safe_local_env npm --prefix "$WEB_ROOT" run build

GENERATED_OPENAPI="$TEMP_DIR/openapi.json"
GENERATED_TYPES="$TEMP_DIR/schema.d.ts"
run_step 'OpenAPI export' safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
  python -m sidera_control_plane.api.openapi_contract --output "$GENERATED_OPENAPI"
run_step 'OpenAPI drift' cmp -s "$WEB_ROOT/openapi.json" "$GENERATED_OPENAPI"
run_step 'TypeScript contract generation' safe_local_env npm --prefix "$WEB_ROOT" exec -- \
  openapi-typescript "$GENERATED_OPENAPI" -o "$GENERATED_TYPES"
run_step 'TypeScript contract drift' cmp -s \
  "$WEB_ROOT/src/shared/api/schema.d.ts" "$GENERATED_TYPES"

if [[ "${SIDERA_RUN_WEB_E2E:-1}" == "1" ]]; then
  run_step 'Web basic read-only three-viewport E2E' safe_local_env env \
    CI=1 \
    SIDERA_E2E_CONTROL_PLANE_PORT="${SIDERA_E2E_CONTROL_PLANE_PORT:-18100}" \
    SIDERA_E2E_KNOWLEDGE_SERVICE_PORT="${SIDERA_E2E_KNOWLEDGE_SERVICE_PORT:-18120}" \
    SIDERA_E2E_MAIN_AGENT_PORT="${SIDERA_E2E_MAIN_AGENT_PORT:-18110}" \
    SIDERA_E2E_WEB_PORT="${SIDERA_E2E_WEB_PORT:-18417}" \
    npm --prefix "$WEB_ROOT" run test:e2e -- \
    tests/e2e/basic-capability-readonly.spec.ts \
    --project=desktop-chromium --project=tablet-chromium --project=mobile-chromium
else
  printf 'Web basic read-only three-viewport E2E: NOT RUN\n' >&2
  LOCAL_FAILED=1
fi

run_step 'protected worktree state' check_protected_state "$PROTECTED_BEFORE" "$PROTECTED_AFTER"
run_step 'git diff check' safe_local_env git -C "$REPO_ROOT" diff --check

if [[ "${SIDERA_RUN_REAL_BASIC:-0}" == "1" ]]; then
  REAL_MODE=1
fi

REAL_SCENARIOS=(
  profile
  result-inputs
  result-integration
  user-flow
  intervention
  revision
  recovery
  second-adapter
  notification
)

real_var() {
  printf 'REAL_%s' "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"
}

set_real_status() {
  local scenario=$1
  local status=$2
  local variable
  variable=$(real_var "$scenario")
  printf -v "$variable" '%s' "$status"
}

for scenario in "${REAL_SCENARIOS[@]}"; do
  set_real_status "$scenario" "NOT_RUN"
done
REAL_PROFILE_CONFIG=NOT_RUN
REAL_READONLY_BROWSER=NOT_RUN

if (( REAL_MODE == 1 )); then
  if [[ -z "$MANIFEST_PATH" || ! -f "$MANIFEST_PATH" ]]; then
    REAL_PROFILE_CONFIG=INCOMPLETE
    for scenario in "${REAL_SCENARIOS[@]}"; do
      set_real_status "$scenario" "INCOMPLETE"
    done
    REAL_READONLY_BROWSER=INCOMPLETE
  else
    CONFIG_OUTPUT="$TEMP_DIR/effective-config.json"
    LOCAL_RECORD_VALID=1
    LOCAL_RECORD_PATH=$(python3 - "$MANIFEST_PATH" <<'PY'
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get(
        "local_gate_record"
    )
except (OSError, json.JSONDecodeError):
    value = None
print(value if isinstance(value, str) else "")
PY
)
    if [[ -n "$LOCAL_RECORD_PATH" && -f "$LOCAL_RECORD_PATH" ]]; then
      CURRENT_FINGERPRINT=$(safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
        python -m sidera_control_plane.local_gate_record fingerprint \
        --repository "$REPO_ROOT" 2>"$TEMP_DIR/fingerprint.log") || CURRENT_FINGERPRINT=""
      if ! python3 - "$LOCAL_RECORD_PATH" "$CURRENT_FINGERPRINT" <<'PY'
import json
import sys
from pathlib import Path

try:
    record = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
if record.get("status") != "PASS":
    raise SystemExit(1)
if record.get("dirty_scope") == "dirty":
    raise SystemExit(1)
if record.get("implementation_fingerprint") != sys.argv[2]:
    raise SystemExit(1)
PY
      then
        LOCAL_RECORD_VALID=0
      fi
    elif [[ "${SIDERA_REQUIRE_BASIC_LOCAL_RECORD:-0}" == "1" ]]; then
      LOCAL_RECORD_VALID=0
    fi
    if (( LOCAL_RECORD_VALID == 1 )) && safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
      python "$REPO_ROOT/scripts/r6-effective-config.py" \
      --manifest "$MANIFEST_PATH" --json >"$CONFIG_OUTPUT" 2>"$TEMP_DIR/effective-config.log"; then
      REAL_PROFILE_CONFIG=PASS
    else
      REAL_PROFILE_CONFIG=INCOMPLETE
    fi

    if [[ "$REAL_PROFILE_CONFIG" == "PASS" ]]; then
      for scenario in "${REAL_SCENARIOS[@]}"; do
        variable=$(real_var "$scenario")
        evidence_file="$EVIDENCE_DIR/${scenario}.json"
        scenario_output="$TEMP_DIR/real-${scenario}.json"
        if safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
          python "$REPO_ROOT/scripts/r6-basic-acceptance.py" \
          --manifest "$MANIFEST_PATH" --scenario "$scenario" --mode real --json \
          >"$scenario_output" 2>"$TEMP_DIR/real-${scenario}.log"; then
          status=$(python3 - "$scenario_output" <<'PY'
import json
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
for line in reversed(lines):
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(value, dict):
        print(value.get("status", "INCOMPLETE"))
        break
else:
    print("INCOMPLETE")
PY
)
        else
          status=$(python3 - "$scenario_output" <<'PY'
import json
import sys
from pathlib import Path

try:
    lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
except OSError:
    print("INCOMPLETE")
else:
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            print(value.get("status", "INCOMPLETE"))
            break
    else:
        print("INCOMPLETE")
PY
)
        fi
        case "$status" in
          PASS|FAIL|INCOMPLETE|NOT_RUN) ;;
          *) status=INCOMPLETE ;;
        esac
        set_real_status "$scenario" "$status"
        cp "$scenario_output" "$evidence_file" 2>/dev/null || true
      done
    else
      for scenario in "${REAL_SCENARIOS[@]}"; do
        set_real_status "$scenario" "INCOMPLETE"
      done
    fi

    if [[ "${SIDERA_RUN_REAL_BASIC_BROWSER:-0}" == "1" ]]; then
      if safe_local_env env SIDERA_RUN_REAL_BASIC=1 \
        npm --prefix "$WEB_ROOT" run test:e2e -- \
        --config=playwright.real-basic.config.ts --project=basic-real-desktop; then
        REAL_READONLY_BROWSER=PASS
      else
        REAL_READONLY_BROWSER=FAIL
      fi
    fi
  fi
fi

printf 'BASIC_LOCAL_GATE=%s\n' "$([[ "$LOCAL_FAILED" == "0" ]] && printf PASS || printf FAIL)"
printf 'REAL_PROFILE_CONFIG=%s\n' "$REAL_PROFILE_CONFIG"
for scenario in "${REAL_SCENARIOS[@]}"; do
  variable=$(real_var "$scenario")
  eval "status=\${$variable}"
  printf '%s=%s\n' "$variable" "$status"
done
printf 'REAL_READONLY_BROWSER=%s\n' "$REAL_READONLY_BROWSER"
printf 'BASIC_LOCAL_CHECK_COUNT=%s\n' "$STEP_COUNT"

BASIC_STATUS=PASS
if (( LOCAL_FAILED != 0 )); then
  BASIC_STATUS=FAIL
elif [[ "$REAL_PROFILE_CONFIG" != "PASS" || "$REAL_READONLY_BROWSER" == "NOT_RUN" ]]; then
  BASIC_STATUS=INCOMPLETE
else
  for scenario in "${REAL_SCENARIOS[@]}"; do
    variable=$(real_var "$scenario")
    eval "status=\${$variable}"
    if [[ "$status" == "FAIL" ]]; then
      BASIC_STATUS=FAIL
      break
    fi
    if [[ "$status" != "PASS" ]]; then
      BASIC_STATUS=INCOMPLETE
    fi
  done
fi

if [[ -n "$RECORD_PATH" ]]; then
  record_status=FAIL
  [[ "$LOCAL_FAILED" == "0" ]] && record_status=PASS
  safe_local_env uv run --project "$CONTROL_PLANE_ROOT" \
    python -m sidera_control_plane.local_gate_record write \
    --output "$RECORD_PATH" \
    --repository "$REPO_ROOT" \
    --database-url "${SIDERA_TEST_DATABASE_URL:-postgresql+psycopg://unavailable@127.0.0.1/sidera_test}" \
    --checks-file "$CHECKS_FILE" \
    --status "$record_status" \
    --dirty-scope "$DIRTY_SCOPE" \
    --commit "$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || printf '%040d' 0)" \
    || BASIC_STATUS=FAIL
fi

printf 'BASIC_CAPABILITY_ACCEPTANCE=%s\n' "$BASIC_STATUS"
if [[ "$BASIC_STATUS" == "FAIL" ]]; then
  exit 1
fi
if [[ "$BASIC_STATUS" == "INCOMPLETE" ]]; then
  exit 2
fi
exit 0
