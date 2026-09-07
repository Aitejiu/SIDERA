#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
GATE="$ROOT/scripts/verify-r6-3-4-result-inputs.sh"
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-3-4-gate-test.XXXXXX")
trap 'rm -rf "$TEMP_DIR"' EXIT

FAKE_BIN="$TEMP_DIR/bin"
FAKE_LOG="$TEMP_DIR/fake.log"
mkdir -p "$FAKE_BIN"

fail() {
  printf 'r6-3-4 gate test failed: %s\n' "$1" >&2
  exit 1
}

cat >"$FAKE_BIN/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'real_flags=%s/%s args=%s\n' \
  "${SIDERA_RUN_REAL_RESULT_INPUTS:-unset}" \
  "${SIDERA_RESULT_INPUTS_REAL_APPROVED:-unset}" \
  "$*" >>"$FAKE_LOG"

if [[ -n "${FAKE_FAIL_ON:-}" && "$*" == *"$FAKE_FAIL_ON"* ]]; then
  exit 7
fi

for ((index = 1; index <= $#; index += 1)); do
  if [[ "${!index}" == "--output" ]]; then
    next=$((index + 1))
    if [[ "$*" == *"sidera_control_plane.api.openapi_contract"* ]]; then
      cp "$FAKE_REPO_ROOT/apps/web/openapi.json" "${!next}"
    else
      printf '{}\n' >"${!next}"
    fi
  fi
done
EOF

cat >"$FAKE_BIN/npm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'npm args=%s\n' "$*" >>"$FAKE_LOG"
if [[ "$*" == *"openapi-typescript"* ]]; then
  output=""
  for ((index = 1; index <= $#; index += 1)); do
    if [[ "${!index}" == "-o" ]]; then
      next=$((index + 1))
      output=${!next}
    fi
  done
  [[ -n "$output" ]] || exit 8
  cp "$FAKE_REPO_ROOT/apps/web/src/shared/api/schema.d.ts" "$output"
fi
EOF

cat >"$FAKE_BIN/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *" status "* || "$*" == *" status"* ]]; then
  printf '%s\n' '?? .superpowers/' '?? references/'
fi
EOF

chmod +x "$FAKE_BIN/uv" "$FAKE_BIN/npm" "$FAKE_BIN/git"

run_gate() {
  PATH="$FAKE_BIN:$PATH" \
    FAKE_LOG="$FAKE_LOG" \
    FAKE_REPO_ROOT="$ROOT" \
    SIDERA_TEST_DATABASE_URL="${SIDERA_TEST_DATABASE_URL:-postgresql+psycopg://tester@127.0.0.1:55432/sidera_test}" \
    bash "$GATE" "$@"
}

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

output=$(run_gate 2>&1)
assert_contains "$output" 'LOCAL_GATE=PASS'
assert_contains "$output" 'REAL_INPUT_ACCEPTANCE=NOT_RUN'
assert_contains "$(<"$FAKE_LOG")" 'real_flags=unset/unset'

set +e
missing_output=$(env -u SIDERA_TEST_DATABASE_URL PATH="$FAKE_BIN:$PATH" \
  FAKE_LOG="$FAKE_LOG" FAKE_REPO_ROOT="$ROOT" bash "$GATE" 2>&1)
missing_status=$?
set -e
[[ "$missing_status" -ne 0 ]] || fail "missing database URL unexpectedly passed"
assert_contains "$missing_output" 'LOCAL_GATE=FAIL'
assert_contains "$missing_output" 'REAL_INPUT_ACCEPTANCE=NOT_RUN'

set +e
unsafe_output=$(PATH="$FAKE_BIN:$PATH" FAKE_LOG="$FAKE_LOG" FAKE_REPO_ROOT="$ROOT" \
  SIDERA_TEST_DATABASE_URL='postgresql+psycopg://tester@127.0.0.1:55432/sidera_development' \
  bash "$GATE" 2>&1)
unsafe_status=$?
set -e
[[ "$unsafe_status" -ne 0 ]] || fail "non-test database unexpectedly passed"
assert_contains "$unsafe_output" 'LOCAL_GATE=FAIL'
assert_not_contains "$unsafe_output" 'migration upgrade: PASS'

set +e
failure_output=$(PATH="$FAKE_BIN:$PATH" FAKE_LOG="$FAKE_LOG" FAKE_REPO_ROOT="$ROOT" \
  FAKE_FAIL_ON='test_r6_3_4_result_input_loop.py' bash "$GATE" 2>&1)
failure_status=$?
set -e
[[ "$failure_status" -ne 0 ]] || fail "fake test failure unexpectedly passed"
assert_contains "$failure_output" 'LOCAL_GATE=FAIL'
assert_contains "$failure_output" 'REAL_INPUT_ACCEPTANCE=NOT_RUN'

set +e
skipped_web_output=$(PATH="$FAKE_BIN:$PATH" FAKE_LOG="$FAKE_LOG" FAKE_REPO_ROOT="$ROOT" \
  SIDERA_SKIP_WEB_E2E=1 bash "$GATE" 2>&1)
skipped_web_status=$?
set -e
[[ "$skipped_web_status" -ne 0 ]] || fail "SIDERA_SKIP_WEB_E2E unexpectedly passed"
assert_contains "$skipped_web_output" 'LOCAL_GATE=FAIL'

before_status=$(git -C "$ROOT" status --short)
after_status=$(git -C "$ROOT" status --short)
[[ "$before_status" == "$after_status" ]] || fail "gate fixture test changed repository state"

printf '%s\n' 'r6-3-4 result-input gate tests passed'
