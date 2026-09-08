#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
GATE="$ROOT/scripts/verify-r6-result-integration.sh"
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-result-gate-test.XXXXXX")
trap 'rm -rf "$TEMP_DIR"' EXIT

FAKE_BIN="$TEMP_DIR/bin"
FAKE_LOG="$TEMP_DIR/fake.log"
mkdir -p "$FAKE_BIN"

fail() {
  printf 'r6 result integration gate test failed: %s\n' "$1" >&2
  exit 1
}

cat >"$FAKE_BIN/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'uv real=%s/%s deepseek=%s opencode=%s args=%s\n' \
  "${SIDERA_RUN_REAL_RESULT_INTEGRATION:-unset}" \
  "${SIDERA_REAL_RESULT_INTEGRATION_AUTHORIZED:-unset}" \
  "${DEEPSEEK_API_KEY:-unset}" \
  "${OPENCODE_API_KEY:-unset}" \
  "$*" >>"$FAKE_LOG"

if [[ "$*" == *"result_readiness"* ]]; then
  if [[ "${SIDERA_RESULT_REAL_GATE_PREFLIGHT:-0}" == "1" ]]; then
    printf '%s\n' '{"enabled":false,"ready":false,"real_result_gate":{"error_code":"real_run_not_authorized","status":"blocked"}}'
    exit 2
  fi
  if [[ "${SIDERA_RESULT_INTEGRATION_ENABLED:-}" == "true" ]]; then
    printf '%s\n' '{"enabled":true,"ready":false,"checks":[],"missing_capabilities":[]}'
    exit 1
  else
    printf '%s\n' '{"enabled":false,"ready":false,"checks":[],"missing_capabilities":[]}'
  fi
  exit 0
fi

if [[ "$*" == *"sidera_control_plane.api.openapi_contract"* ]]; then
  for ((index = 1; index <= $#; index += 1)); do
    if [[ "${!index}" == "--output" ]]; then
      next=$((index + 1))
      cp "$FAKE_REPO_ROOT/apps/web/openapi.json" "${!next}"
    fi
  done
fi
EOF

cat >"$FAKE_BIN/npm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'npm deepseek=%s opencode=%s args=%s\n' \
  "${DEEPSEEK_API_KEY:-unset}" \
  "${OPENCODE_API_KEY:-unset}" \
  "$*" >>"$FAKE_LOG"
for ((index = 1; index <= $#; index += 1)); do
  if [[ "${!index}" == "-o" ]]; then
    next=$((index + 1))
    cp "$FAKE_REPO_ROOT/apps/web/src/shared/api/schema.d.ts" "${!next}"
  fi
done
EOF

cat >"$FAKE_BIN/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'git args=%s\n' "$*" >>"$FAKE_LOG"
if [[ "$*" == *"rev-parse --short HEAD"* ]]; then
  printf '%s\n' 'r6gate0'
elif [[ "$*" == *"status --porcelain=v1 -z"* ]]; then
  printf '%s\0' '?? .superpowers/' '?? references/'
fi
EOF

chmod +x "$FAKE_BIN/uv" "$FAKE_BIN/npm" "$FAKE_BIN/git"

assert_contains() {
  local haystack=$1
  local needle=$2
  [[ "$haystack" == *"$needle"* ]] || fail "expected output to contain: $needle"
}

assert_not_contains() {
  local haystack=$1
  local needle=$2
  [[ "$haystack" != *"$needle"* ]] || fail "expected output not to contain: $needle"
}

run_gate() {
  PATH="$FAKE_BIN:$PATH" \
    FAKE_LOG="$FAKE_LOG" \
    FAKE_REPO_ROOT="$ROOT" \
    SIDERA_TEST_DATABASE_URL="${SIDERA_TEST_DATABASE_URL:-postgresql+psycopg://tester@127.0.0.1:55432/sidera_test}" \
    bash "$GATE" "$@"
}

output=$(DEEPSEEK_API_KEY=sk-test-deepseek OPENCODE_API_KEY=sk-test-opencode run_gate)
assert_contains "$output" 'RESULT_INTEGRATION_COMMIT=r6gate0'
assert_contains "$output" 'RESULT_INTEGRATION_DIRTY_SCOPE=protected-only'
assert_contains "$output" 'RESULT_INTEGRATION_DATABASE_CHECK=PASS'
assert_contains "$output" 'RESULT_INTEGRATION_LOCAL_GATE=PASS'
assert_contains "$output" 'REAL_INTEGRATION_ACCEPTANCE=NOT_RUN'
assert_not_contains "$output" 'sk-test-'
fake_output=$(<"$FAKE_LOG")
assert_contains "$fake_output" 'real=unset/unset'
assert_contains "$fake_output" 'deepseek=unset'
assert_contains "$fake_output" 'opencode=unset'

set +e
unsafe_output=$(SIDERA_TEST_DATABASE_URL='postgresql+psycopg://tester@example.com:5432/sidera_development' run_gate 2>&1)
unsafe_rc=$?
set -e
[[ "$unsafe_rc" -ne 0 ]] || fail "unsafe database unexpectedly passed"
assert_contains "$unsafe_output" 'RESULT_INTEGRATION_DATABASE_CHECK=FAIL'
assert_contains "$unsafe_output" 'RESULT_INTEGRATION_LOCAL_GATE=FAIL'
assert_not_contains "$unsafe_output" 'migration upgrade: PASS'

set +e
unauthorized_output=$(SIDERA_RUN_REAL_RESULT_INTEGRATION=1 run_gate 2>&1)
unauthorized_rc=$?
set -e
[[ "$unauthorized_rc" -ne 0 ]] || fail "partial real authorization unexpectedly passed"
assert_contains "$unauthorized_output" 'REAL_INTEGRATION_ACCEPTANCE=INCOMPLETE'
assert_contains "$unauthorized_output" 'RESULT_INTEGRATION_LOCAL_GATE=FAIL'
assert_not_contains "$unauthorized_output" 'real=1/1'

printf '%s\n' 'r6 result integration gate tests passed'
