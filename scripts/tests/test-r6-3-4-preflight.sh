#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PREFLIGHT="$ROOT/scripts/r6-3-4-preflight.sh"
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-3-4-preflight-test.XXXXXX")
trap 'rm -rf "$TEMP_DIR"' EXIT

FAKE_PYTHON="$TEMP_DIR/fake-python"
FAKE_ARGS="$TEMP_DIR/args"
FAKE_STATUS="$TEMP_DIR/status"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" > "$FAKE_ARGS"' \
  'status=$(<"$FAKE_STATUS")' \
  'if [[ "$status" == "1" ]]; then' \
  '  printf "%s\n" "provider_token=sk-r6-3-4-secret" >&2' \
  '  exit 1' \
  'fi' \
  'if [[ "$status" == "0" ]]; then' \
  '  printf "%s\n" '"'"'{"status":"ready"}'"'"'' \
  'else' \
  '  printf "%s\n" '"'"'{"status":"not_ready","checks":[{"code":"result_inputs_disabled"}]}'"'"'' \
  'fi' \
  'exit "$status"' >"$FAKE_PYTHON"
chmod +x "$FAKE_PYTHON"

fail() {
  printf 'r6-3-4 preflight test failed: %s\n' "$1" >&2
  exit 1
}

before_status=$(git -C "$ROOT" status --short)

run_case() {
  local expected_status="$1"
  local output status
  printf '%s\n' "$expected_status" >"$FAKE_STATUS"
  set +e
  output=$(
    SIDERA_R6_3_4_PYTHON_BIN="$FAKE_PYTHON" \
      FAKE_ARGS="$FAKE_ARGS" \
      FAKE_STATUS="$FAKE_STATUS" \
      bash "$PREFLIGHT"
  )
  status=$?
  set -e
  [[ "$status" -eq "$expected_status" ]] || fail "expected exit $expected_status, got $status"
  [[ "$(<"$FAKE_ARGS")" == "-m sidera_control_plane.collaboration_readiness --json" ]] \
    || fail "preflight invoked the wrong readiness arguments"
  printf '%s' "$output"
}

ready_output=$(run_case 0)
[[ "$ready_output" == '{"status":"ready"}' ]] || fail "ready output was not preserved"

not_ready_output=$(run_case 2)
[[ "$not_ready_output" == *'result_inputs_disabled'* ]] \
  || fail "not-ready output did not preserve the disabled feature code"

set +e
failure_output=$(
  printf '%s\n' 1 >"$FAKE_STATUS"
  SIDERA_R6_3_4_PYTHON_BIN="$FAKE_PYTHON" \
    FAKE_ARGS="$FAKE_ARGS" \
    FAKE_STATUS="$FAKE_STATUS" \
    bash "$PREFLIGHT" 2>&1
)
failure_status=$?
set -e
[[ "$failure_status" -eq 1 ]] || fail "check failure did not return exit 1"
[[ "$failure_output" == *'preflight_check_failed'* ]] \
  || fail "check failure did not return the stable error code"
[[ "$failure_output" != *'sk-r6-3-4-secret'* ]] \
  || fail "preflight leaked fake provider credentials"

after_status=$(git -C "$ROOT" status --short)
[[ "$before_status" == "$after_status" ]] || fail "preflight mutated the repository"

printf '%s\n' 'r6-3-4 preflight tests passed'
